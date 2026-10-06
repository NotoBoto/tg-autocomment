"""
Автокомментатор для Telegram-канала.
Ловит новый пост -> генерирует комментарий через Claude Code (подписка) -> берёт случайную
картинку из папки -> публикует комментарий в обсуждении поста.
"""
import asyncio
import json
import logging
import os
import random
import shutil
from pathlib import Path

from telethon import TelegramClient, events, errors

BASE = Path(__file__).parent
cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(BASE / "log.txt", encoding="utf-8")],
)
log = logging.getLogger("autocomment")

SYSTEM_PROMPT = (BASE / cfg["prompt_file"]).read_text(encoding="utf-8")
# Страховка поверх вашего промпта: модель сама отказывается от тяжёлых тем
SYSTEM_PROMPT += (
    "\n\nВАЖНО: если пост о смерти, трагедии, катастрофе, болезни, войне "
    "или другой тяжёлой теме — ответь ровно одним словом SKIP."
)

IMAGE_EXT = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp", ".mp4"}
# Промпт передаём файлом: многострочный текст в аргументах ломается в cmd.exe
SYSTEM_PROMPT_FILE = BASE / "_system_prompt.txt"
SYSTEM_PROMPT_FILE.write_text(SYSTEM_PROMPT, encoding="utf-8")

DONE_FILE = BASE / "done_posts.txt"
done_posts = set(DONE_FILE.read_text().split()) if DONE_FILE.exists() else set()

# На Windows предпочитаем настоящий .exe, а не обёртку .cmd
CLAUDE_BIN = shutil.which("claude.exe") or shutil.which("claude")
if not CLAUDE_BIN:
    raise SystemExit("Не найден Claude Code. Установи: npm install -g @anthropic-ai/claude-code")

# Чтобы Claude Code не ушёл на платный API вместо подписки
CLAUDE_ENV = {k: v for k, v in os.environ.items()
              if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
tg = TelegramClient(str(BASE / cfg["session_name"]), cfg["api_id"], cfg["api_hash"])


CONFIRM_MODE = cfg.get("confirm_before_post", True)


def ask_mode() -> bool:
    default = "1" if cfg.get("confirm_before_post", True) else "2"
    print("\nРежим работы:")
    print("  1 — подтверждать каждый коммент вручную")
    print("  2 — постить автоматически (со случайной задержкой)")
    while True:
        ans = input(f"Выбери 1 или 2 [Enter = {default}]: ").strip() or default
        if ans in ("1", "2"):
            return ans == "1"
        print("Нужно ввести 1 или 2")


SEE_IMAGES = cfg.get("send_post_images", False)


def ask_yes_no(question: str, default: bool) -> bool:
    d = "y" if default else "n"
    while True:
        ans = input(f"{question} (y/n) [Enter = {d}]: ").strip().lower() or d
        if ans in ("y", "д", "да", "yes"):
            return True
        if ans in ("n", "н", "нет", "no"):
            return False
        print("Нужно ввести y или n")


MEDIA_DIR = BASE / "_post_media"
MAX_POST_IMAGES = cfg.get("max_post_images", 4)
seen_groups: set[int] = set()


async def collect_post(msg):
    """Возвращает (id для коммента, текст, список сообщений с медиа). Склеивает альбомы."""
    if not msg.grouped_id:
        return msg.id, msg.raw_text or "", [msg]
    await asyncio.sleep(2)  # даём долететь остальным частям альбома
    around = await tg.get_messages(msg.chat_id, ids=list(range(msg.id - 10, msg.id + 11)))
    parts = sorted((m for m in around if m and m.grouped_id == msg.grouped_id),
                   key=lambda m: m.id)
    text = next((m.raw_text for m in parts if m.raw_text), "")
    return parts[0].id, text, parts


async def download_post_images(parts, folder: Path) -> list[Path]:
    """Качает фото (и превью видео) из поста во временную папку поста."""
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for m in parts:
        if len(files) >= MAX_POST_IMAGES:
            break
        target = folder / f"img{len(files) + 1}.jpg"
        try:
            if m.photo:
                path = await tg.download_media(m, file=str(target))
            elif m.video or m.gif:
                path = await tg.download_media(m, file=str(target), thumb=-1)
            else:
                continue
            if path:
                files.append(Path(path))
        except Exception as e:
            log.warning("Не удалось скачать медиа из #%s: %s", m.id, e)
    return files


_image_bag: list[Path] = []


def list_images() -> list[Path]:
    folder = BASE / cfg["images_dir"]
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXT)


def pick_image():
    """Перемешанная «колода»: каждая картинка выпадет по разу, потом новый круг."""
    global _image_bag
    if random.random() > cfg.get("attach_image_chance", 1.0):
        return None
    if not _image_bag:
        _image_bag = list_images()
        random.shuffle(_image_bag)
    return _image_bag.pop() if _image_bag else None


def is_sensitive(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in cfg.get("skip_keywords", []))


def decode_any(data: bytes) -> str:
    """Windows-консоль может отдавать ошибки в cp866/cp1251 — пробуем все."""
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


async def generate_comment(post_text: str, images: list[Path] | None = None) -> str | None:
    images = images or []
    user_msg = f"Текст поста:\n\n{post_text or '(текста нет, только медиа)'}"
    args = [CLAUDE_BIN, "-p",
            "--system-prompt-file", str(SYSTEM_PROMPT_FILE),
            "--model", cfg.get("model", "sonnet"),
            "--output-format", "text"]
    cwd = None
    if images:
        names = ", ".join(p.name for p in images)
        user_msg += (f"\n\nК посту приложены картинки: {names}. Открой каждую "
                     "инструментом Read, посмотри, что на них, и учти это в комменте. "
                     "В ответе выведи только сам комментарий.")
        args += ["--allowedTools", "Read", "--max-turns", str(len(images) + 2)]
        cwd = str(images[0].parent)
    else:
        args += ["--max-turns", "1"]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=CLAUDE_ENV,
        cwd=cwd,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(user_msg.encode("utf-8")),
            timeout=cfg.get("claude_timeout_sec", 120) + 30 * len(images),
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("Claude Code не ответил вовремя")
    if proc.returncode != 0:
        msg = (decode_any(err) or decode_any(out)).strip()
        raise RuntimeError(msg or f"код выхода {proc.returncode}")
    text = decode_any(out).strip()
    if not text or text.upper().startswith("SKIP"):
        return None
    return text


async def ask_confirm(comment: str, image) -> bool:
    print("\n" + "=" * 60)
    print(comment)
    print(f"[картинка: {image.name if image else 'нет'}]")
    print("=" * 60)
    ans = await asyncio.to_thread(input, "Постить? (y/n): ")
    return ans.strip().lower() in ("y", "д", "да", "yes")


@tg.on(events.NewMessage(chats=cfg["channel"]))
async def on_new_post(event):
    msg = event.message
    if msg.grouped_id:
        if msg.grouped_id in seen_groups:   # альбом уже обрабатывается
            return
        seen_groups.add(msg.grouped_id)

    post_id, post_text, parts = await collect_post(msg)
    key = f"{event.chat_id}:{post_id}"
    if key in done_posts:
        return
    log.info("Новый пост #%s: %s", post_id, post_text[:80].replace("\n", " "))

    has_media = any(m.photo or m.video or m.gif for m in parts)
    if not post_text.strip() and not (SEE_IMAGES and has_media):
        log.info("Пустой пост, пропуск")
        return
    if is_sensitive(post_text):
        log.info("Чувствительная тема (ключевое слово), пропуск")
        return

    post_images = []
    if SEE_IMAGES and has_media:
        post_images = await download_post_images(parts, MEDIA_DIR / str(post_id))
        log.info("Картинок из поста передано: %d", len(post_images))

    try:
        comment = await generate_comment(post_text, post_images)
    except Exception as e:
        log.error("Ошибка Claude Code: %s", e)
        return
    finally:
        if post_images:
            shutil.rmtree(MEDIA_DIR / str(post_id), ignore_errors=True)
    if comment is None:
        log.info("Модель вернула SKIP, пропуск")
        return

    image = pick_image()

    if CONFIRM_MODE:
        if not await ask_confirm(comment, image):
            log.info("Отклонено вручную")
            return
    else:
        delay = random.randint(cfg["delay_min_sec"], cfg["delay_max_sec"])
        log.info("Жду %s сек перед публикацией", delay)
        await asyncio.sleep(delay)

    try:
        await tg.send_message(
            event.chat_id,
            comment,
            comment_to=post_id,
            file=str(image) if image else None,
        )
        log.info("Комментарий опубликован под #%s (картинка: %s)",
                 post_id, image.name if image else "нет")
        done_posts.add(key)
        with DONE_FILE.open("a") as f:
            f.write(key + "\n")
    except errors.FloodWaitError as e:
        log.warning("FloodWait: Telegram просит подождать %s сек", e.seconds)
    except errors.MsgIdInvalidError:
        log.error("У поста нет обсуждения (комментарии выключены)")
    except Exception as e:
        log.error("Не удалось отправить: %s", e)


async def main():
    global CONFIRM_MODE, SEE_IMAGES
    CONFIRM_MODE = ask_mode()
    SEE_IMAGES = ask_yes_no("Передавать нейронке картинки из постов?",
                            cfg.get("send_post_images", False))
    await tg.start()  # при первом запуске спросит телефон и код
    me = await tg.get_me()
    log.info("Вошли как %s, слушаю канал @%s", me.first_name, cfg["channel"])
    log.info("Режим: %s, картинки постов: %s",
             "с подтверждением" if CONFIRM_MODE else "автоматический",
             "да" if SEE_IMAGES else "нет")
    imgs = list_images()
    log.info("Картинок найдено: %d -> %s", len(imgs), ", ".join(p.name for p in imgs))
    skipped = [p.name for p in (BASE / cfg["images_dir"]).iterdir()
               if p.is_file() and p.suffix.lower() not in IMAGE_EXT]
    if skipped:
        log.warning("Пропущены (неподдерживаемый формат): %s", ", ".join(skipped))
    await tg.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
