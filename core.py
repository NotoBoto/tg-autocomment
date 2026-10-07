"""
Ядро автокомментатора: настройки, генерация через Claude Code, работа с Telegram.
Telegram-клиент живёт в отдельном потоке со своим asyncio-циклом, а наружу (в окно)
отдаёт события через очередь.
"""
import asyncio
import base64
import itertools
import json
import logging
import mimetypes
import os
import queue
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from telethon import TelegramClient, connection, events, errors, utils

VERSION = "0.3.1"
REPO = "NotoBoto/tg-autocomment"   # отсюда берутся обновления (GitHub Releases)

# Установленная версия (.exe из установщика) хранит данные в %APPDATA% — обновление и
# переустановка их не трогают. Запуск из исходников — всё рядом с app.py, как раньше.
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))   # файлы внутри программы
BASE = (Path(os.environ.get("APPDATA") or Path.home()) / "TG Autocomment") if FROZEN else Path(__file__).parent
BASE.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE / "config.json"
LEGACY_DONE_FILE = BASE / "done_posts.txt"   # до профилей был один общий файл
MEDIA_DIR = BASE / "_post_media"

IMAGE_EXT = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp", ".mp4"}
MODELS = ["sonnet", "opus", "haiku"]          # для Claude Code
API_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
API_PRICES = {  # $ за 1M токенов: вход / выход
    "claude-opus-5-5": "$4 / $20", "claude-sonnet-5-5": "$2 / $10", "claude-haiku-4-5": "$1 / $5"}
GEMINI_API_DEFAULT = "gemini-flash-latest"

# Страховка поверх вашего промпта: модель сама отказывается от тяжёлых тем
SKIP_RULE = (
    "\n\nВАЖНО: если пост о смерти, трагедии, катастрофе, болезни, войне "
    "или другой тяжёлой теме — ответь ровно одним словом SKIP."
)

DEFAULTS = {
    "name": "Основной",            # название профиля (аккаунта) в окне
    "enabled": True,               # запускать ли профиль кнопкой «▶ Запустить»
    "api_id": "",
    "api_hash": "",
    "session_name": "my_account",
    "proxy_mode": "system",        # прокси для Telegram: system (из VPN/Windows) | custom | none
    "proxy": "",                   # для custom: socks5://…, http://… или ссылка MTProxy
    "channels": [],                # каналы, за которыми следим
    "backend": "claude_code",      # claude_code | api (Anthropic) | gemini_cli (Antigravity) | gemini_api | codex | openai_api | openai_compat
    "model": "sonnet",
    "api_key": "",
    "api_model": "claude-opus-5-5",
    "gemini_model": "",            # модель Antigravity; пусто — по умолчанию
    "gemini_api_key": "",
    "gemini_api_model": "gemini-flash-latest",
    "codex_model": "",             # модель Codex; пусто — по умолчанию для аккаунта
    "openai_api_key": "",
    "openai_api_model": "gpt-5.5",
    "compat_base_url": "http://localhost:1234/v1",   # своя модель: OpenAI-совместимый сервер (LM Studio…)
    "compat_api_key": "",          # большинству локальных серверов не нужен
    "compat_model": "",
    "prompt_file": "prompt.txt",
    "images_dir": "images",
    "attach_image_chance": 1.0,
    "delay_min_sec": 20,
    "delay_max_sec": 90,
    "confirm_before_post": True,
    "skip_keywords": ["погиб", "умер", "скончал", "теракт", "катастроф",
                      "траур", "соболезн", "жертв", "пожар", "убит"],
    "claude_timeout_sec": 120,
    "send_post_images": False,
    "max_post_images": 4,
}

log = logging.getLogger("autocomment")


# ---------- настройки ----------
# config.json: {"profiles": [профиль, …], "current": id профиля, открытого в окне,
#               "close_to_tray": bool, "auto_update": bool}.
# Профиль — один аккаунт Telegram со всеми своими настройками (ключи из DEFAULTS + "id").

def load_config() -> dict:
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("config.json повреждён (%s) — взяты значения по умолчанию", e)
    if "profiles" not in data:   # старый конфиг с одним аккаунтом → профиль «Основной»
        data = {"profiles": [dict(data, id="main")], "current": "main"}
        if LEGACY_DONE_FILE.exists() and not done_file({"id": "main"}).exists():
            LEGACY_DONE_FILE.rename(done_file({"id": "main"}))
    profiles = [normalize_profile(p) for p in data["profiles"]] or [normalize_profile({"id": "main"})]
    ids = [p["id"] for p in profiles]
    return {"profiles": profiles, "current": data.get("current") if data.get("current") in ids else ids[0],
            "close_to_tray": data.get("close_to_tray", True),   # крестик прячет окно в трей
            "auto_update": data.get("auto_update", True),       # ставить новые версии без вопросов
            # id профилей, работавших перед автообновлением, — их и запустить после него
            "resume_profiles": data.get("resume_profiles", [])}


def normalize_profile(p: dict) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(p)
    cfg["channels"] = channels(cfg)
    cfg.pop("channel", None)
    return cfg


def new_profile(config: dict, base: dict, name: str) -> dict:
    """Новый профиль — копия настроек base, но со своей сессией, промптом и списком сделанного."""
    # Случайный id: у удалённого профиля и нового не совпадут файлы (сделанные посты, промпт)
    pid = secrets.token_hex(3)
    p = dict(base, id=pid, name=name, enabled=True,
             session_name=f"account_{pid}", prompt_file=f"prompt_{pid}.txt")
    if prompt_path(base).exists():
        prompt_path(p).write_text(prompt_path(base).read_text(encoding="utf-8"), encoding="utf-8")
    config["profiles"].append(p)
    return p


def done_file(cfg) -> Path:
    return BASE / f"done_posts_{cfg['id']}.txt"


def channels(cfg) -> list[str]:
    """Список каналов; старый конфиг с одним "channel" тоже понимаем."""
    chans = cfg.get("channels") or ([cfg["channel"]] if cfg.get("channel") else [])
    return list(dict.fromkeys(c for c in (normalize_channel(c) for c in chans) if c))


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_channel(s: str) -> str:
    """Канал из списка в одном виде:
    публичный — 'name' (из '@name', 't.me/name', 't.me/name/123');
    закрытый — 't.me/c/<id>' (из ссылки на его пост или id -100…) или 't.me/+<hash>' (ссылка-приглашение)."""
    s = s.strip()
    m = re.search(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([A-Za-z0-9_-]+)", s)
    if m:
        return f"t.me/+{m.group(1)}"
    m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?c/(\d+)", s)
    if m:
        return f"t.me/c/{m.group(1)}"
    m = re.fullmatch(r"-100(\d+)", s)
    if m:
        return f"t.me/c/{m.group(1)}"
    m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]+)", s)
    if m:
        return m.group(1)
    return s.lstrip("@")


def channel_label(c: str) -> str:
    """Как показать канал из списка: '@name' у публичного, ссылка — у закрытого."""
    return c if c.startswith("t.me/") else "@" + c


def parse_post_link(s: str) -> tuple[str | int, int]:
    """Ссылка на пост → (канал, номер поста). Канал — username или id закрытого канала (-100…).
    Понимает t.me/канал/123, t.me/s/канал/123, t.me/c/<id>/123, посты в темах, tg://resolve и tg://privatepost."""
    from urllib.parse import parse_qs, urlsplit
    s = s.strip()
    if s.startswith("tg://"):
        u = urlsplit(s)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.netloc == "resolve" and q.get("domain") and q.get("post", "").isdigit():
            return q["domain"], int(q["post"])
        if u.netloc == "privatepost" and q.get("channel", "").isdigit() and q.get("post", "").isdigit():
            return int("-100" + q["channel"]), int(q["post"])
        raise ValueError("В ссылке tg:// нет канала или номера поста")
    m = re.search(r"(?:t\.me|telegram\.me|telegram\.dog)/(\S+)", s)
    if not m:
        raise ValueError("Это не ссылка на пост Telegram — нужна вида t.me/канал/123")
    path = [p for p in urlsplit("https://x/" + m.group(1)).path.split("/") if p]
    if path and path[0] == "s":
        path = path[1:]
    # Последнее число — сам пост; между ними может быть номер темы (t.me/канал/тема/пост)
    if len(path) >= 3 and path[0] == "c" and path[1].isdigit() and path[-1].isdigit():
        return int("-100" + path[1]), int(path[-1])
    if len(path) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", path[0]) and path[-1].isdigit():
        return path[0], int(path[-1])
    raise ValueError("В ссылке нет номера поста — нужна ссылка на конкретный пост: t.me/канал/123 "
                     "(в Telegram: правой кнопкой по посту → «Копировать ссылку»)")


def prompt_path(cfg) -> Path:
    return BASE / cfg["prompt_file"]


def images_path(cfg) -> Path:
    p = Path(cfg["images_dir"])
    return p if p.is_absolute() else BASE / p


def list_images(cfg) -> list[Path]:
    folder = images_path(cfg)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXT)


# Сюда ставит официальный установщик; PATH у уже запущенной программы об этом не знает
NATIVE_CLAUDE = Path.home() / ".local" / "bin" / "claude.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_claude() -> str | None:
    # На Windows предпочитаем настоящий .exe, а не обёртку .cmd
    found = shutil.which("claude.exe") or shutil.which("claude")
    if found:
        return found
    return str(NATIVE_CLAUDE) if NATIVE_CLAUDE.exists() else None


def claude_status() -> dict:
    """{'installed', 'loggedIn', 'email', 'authMethod', 'subscriptionType'}"""
    exe = find_claude()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "auth", "status", "--json"], capture_output=True,
                           timeout=30, env=cli_env(), creationflags=NO_WINDOW)
        info = json.loads(decode_any(r.stdout))
    except Exception:
        info = {"loggedIn": False}
    info["installed"] = True
    return info


def api_key(cfg) -> str | None:
    return str(cfg.get("api_key", "")).strip() or os.environ.get("ANTHROPIC_API_KEY")


def check_api_key(cfg) -> tuple[bool, str]:
    """Проверка ключа без траты токенов: запрашиваем описание выбранной модели."""
    import anthropic
    key = api_key(cfg)
    if not key:
        return False, "Ключ не указан"
    try:
        m = anthropic.Anthropic(api_key=key, timeout=20, max_retries=1).models.retrieve(
            cfg.get("api_model", API_MODELS[0]))
        return True, f"Ключ работает ✓  модель {m.display_name} доступна"
    except anthropic.AuthenticationError:
        return False, "Ключ не подходит"
    except anthropic.NotFoundError:
        return False, "Ключ работает, но модель недоступна для этого аккаунта"
    except anthropic.APIConnectionError:
        return False, "Нет связи с API — проверьте интернет"
    except anthropic.APIStatusError as e:
        return False, f"Ошибка API ({e.status_code}): {e.message}"


def install_claude() -> tuple[bool, str]:
    """Официальный установщик Claude Code для Windows (Node.js не нужен)."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "irm https://claude.ai/install.ps1 | iex"],
        capture_output=True, timeout=600, env=cli_env(), creationflags=NO_WINDOW)
    out = (decode_any(r.stdout) + "\n" + decode_any(r.stderr)).strip()
    return find_claude() is not None, out


def open_claude_login():
    """Открывает вход в отдельном окне консоли: там ссылка/браузер и, если нужно, поле для кода."""
    subprocess.Popen([find_claude(), "auth", "login", "--claudeai"],
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def validate(cfg) -> list[str]:
    """Список понятных проблем, мешающих запуску."""
    problems = []
    if not str(cfg.get("api_id", "")).strip().isdigit():
        problems.append("Не указан API ID (число с my.telegram.org)")
    if not str(cfg.get("api_hash", "")).strip():
        problems.append("Не указан API Hash (с my.telegram.org)")
    if not channels(cfg):
        problems.append("Не указан ни один канал")
    if cfg.get("proxy_mode") == "custom" and not str(cfg.get("proxy", "")).strip():
        problems.append("Выбран свой прокси, но адрес не указан")
    try:
        telegram_proxy(cfg)
    except ValueError as e:
        problems.append(f"Прокси: {e}")
    p = prompt_path(cfg)
    if not p.exists() or not p.read_text(encoding="utf-8").strip():
        problems.append("Промпт пустой — заполните вкладку «Промпт»")
    backend = cfg.get("backend")
    if backend == "api":
        if not api_key(cfg):
            problems.append("Не указан API-ключ Anthropic — «Настройки» → «Нейросеть»")
        return problems
    if backend == "gemini_api":
        if not gemini_key(cfg):
            problems.append("Не указан API-ключ Gemini — «Настройки» → «Нейросеть»")
        return problems
    if backend == "openai_api":
        if not openai_key(cfg):
            problems.append("Не указан API-ключ OpenAI — «Настройки» → «Нейросеть»")
        return problems
    if backend == "openai_compat":
        if not str(cfg.get("compat_base_url", "")).strip():
            problems.append("Не указан адрес сервера своей модели — «Настройки» → «Нейросеть»")
        if not str(cfg.get("compat_model", "")).strip():
            problems.append("Не выбрана модель — «Настройки» → «Нейросеть» → «Проверить подключение»")
        return problems
    if backend == "codex":
        st = codex_status()
        if not st["installed"]:
            problems.append("Не установлен Codex CLI — «Настройки» → «Установить и войти»")
        elif not st["loggedIn"]:
            problems.append("Не выполнен вход в ChatGPT — «Настройки» → «Войти в ChatGPT»")
        return problems
    if backend == "gemini_cli":
        st = agy_status()
        if not st["installed"]:
            problems.append("Не установлен Antigravity CLI — «Настройки» → «Установить и войти»")
        elif not st["loggedIn"]:
            problems.append("Не выполнен вход в Google для Antigravity — «Настройки» → «Войти в Google»")
        return problems
    st = claude_status()
    if not st["installed"]:
        problems.append("Не установлен Claude Code — «Настройки» → «Установить и войти»")
    elif not st.get("loggedIn"):
        problems.append("Не выполнен вход в Claude — «Настройки» → «Войти в Claude»")
    return problems


# ---------- прокси ----------
# Где Telegram и нейросети заблокированы, нужен VPN. В режиме TUN он программе не мешает,
# а в режиме «системный прокси» его надо передать явно: Telethon и CLI нейросетей
# настройки прокси Windows сами не читают (Python-SDK API читают — через httpx).

def system_proxy() -> str | None:
    """Прокси из переменных окружения или настроек Windows (его выставляет VPN-клиент)."""
    from urllib.request import getproxies
    p = getproxies()
    return p.get("https") or p.get("http") or p.get("socks") or p.get("all")


def parse_proxy(s: str) -> dict:
    """'socks5://user:pass@host:port', 'http://host:port', 'host:port' (= http)
    или ссылка MTProxy t.me/proxy?server=…&port=…&secret=… . Ошибка — ValueError с понятным текстом."""
    from urllib.parse import parse_qs, unquote, urlsplit
    s = s.strip()
    if "proxy?" in s:
        q = {k: v[0] for k, v in parse_qs(urlsplit(s).query).items()}
        try:
            host, port, secret = q["server"], int(q["port"]), q["secret"]
        except (KeyError, ValueError):
            raise ValueError("В ссылке MTProxy нет server, port или secret")
        try:
            raw = bytes.fromhex(secret)
        except ValueError:
            raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        if raw[:1] == b"\xee":
            raise ValueError("MTProxy с секретом ee… (FakeTLS) не поддерживается — "
                             "нужен прокси с секретом dd… или SOCKS5/HTTP")
        return {"type": "mtproxy", "host": host, "port": port, "secret": raw.hex()}
    u = urlsplit(s if "://" in s else "http://" + s)
    kind = {"socks": "socks5", "socks5h": "socks5", "https": "http"}.get(u.scheme.lower(), u.scheme.lower())
    if kind not in ("socks5", "socks4", "http"):
        raise ValueError(f"Неизвестный тип прокси «{u.scheme}» — нужен socks5://, http:// или ссылка MTProxy")
    try:
        port = u.port
    except ValueError:
        port = None
    if not u.hostname or not port:
        raise ValueError("В адресе прокси нет хоста или порта (пример: socks5://127.0.0.1:10808)")
    return {"type": kind, "host": u.hostname, "port": port,
            "user": unquote(u.username) if u.username else None,
            "password": unquote(u.password) if u.password else None}


def telegram_proxy(cfg) -> dict | None:
    """Прокси для Telegram по настройке профиля; None — подключаемся напрямую."""
    mode = cfg.get("proxy_mode", "system")
    if mode == "none":
        return None
    s = str(cfg.get("proxy", "")).strip() if mode == "custom" else system_proxy()
    return parse_proxy(s) if s else None


def proxy_label(p: dict | None) -> str:
    """Адрес прокси для журнала — без логина и пароля."""
    return f"{p['type']}://{p['host']}:{p['port']}" if p else "напрямую"


def cli_env(drop=()) -> dict:
    """Окружение для CLI нейросетей и установщиков: системный HTTP-прокси передаём переменными."""
    env = {k: v for k, v in os.environ.items() if k not in drop}
    if not any(k.upper() == "HTTPS_PROXY" for k in env):
        p = system_proxy()
        if p and p.startswith("http"):
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = p
            env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")   # вход через браузер идёт на localhost
    return env


# ---------- автозапуск с Windows ----------
# Запись в HKCU\…\Run: только для текущего пользователя, прав администратора не нужно.

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "TG Autocomment"
AUTOSTART_ARG = "--autostart"    # с ним окно стартует в трее и через AUTO_RUN_DELAY сек начинает работу
AUTO_RUN_DELAY = 30              # время на подключение VPN и сети после входа в Windows


def autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}" {AUTOSTART_ARG}'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")   # без чёрного окна консоли
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).parent / "app.py"}" {AUTOSTART_ARG}'


def get_autostart() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0]
    except OSError:
        return None


def set_autostart(on: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


BACKEND_NAMES = {"claude_code": "Claude Code", "api": "Claude API", "gemini_cli": "Antigravity",
                 "gemini_api": "Gemini API", "codex": "Codex", "openai_api": "OpenAI API",
                 "openai_compat": "Своя модель"}

# Так сервисы отвечают на запрос из страны, где они не работают
REGION_MARKERS = ("not currently available in your location", "location is not supported",
                  "unsupported_country", "unsupported country", "not available in your country",
                  "region, or territory not supported", "request not allowed")


def explain_ai_error(name: str, msg: str) -> str:
    """Текст ошибки нейросети для журнала и очереди; отказ по стране — с подсказкой про VPN."""
    msg = msg.removeprefix(f"{name}: ").strip()
    text = msg if msg.startswith(f"Ошибка {name}") else f"Ошибка {name}: {msg}"
    if any(m in msg.lower() for m in REGION_MARKERS):
        sp = system_proxy()
        route = f"через системный прокси {sp}" if sp else "без системного прокси (через VPN в режиме TUN или напрямую)"
        text += (f"\n{name} недоступен из страны, откуда пришёл запрос. Включите VPN с сервером там, где сервис "
                 f"работает (например, США или Европа) — в режиме TUN или «системный прокси» — и повторите. "
                 f"Сейчас запросы идут {route}. Не помогает — попробуйте другой сервер VPN или другой "
                 "способ подключения в «Настройках».")
    return text


def decode_any(data: bytes) -> str:
    """Windows-консоль может отдавать ошибки в cp866/cp1251 — пробуем все."""
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ---------- Antigravity CLI (Google, преемник Gemini CLI) ----------
# С 18.06.2026 Gemini CLI не обслуживает личные Google-аккаунты — вместо него agy.

AGY_DEFAULT = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
AGY_AGENT = "autocomment"


def find_agy() -> str | None:
    found = shutil.which("agy.exe") or shutil.which("agy")
    if found:
        return found
    return str(AGY_DEFAULT) if AGY_DEFAULT.exists() else None


def agy_status() -> dict:
    """`agy models` без входа сразу отвечает «Please sign in», после входа — списком моделей."""
    exe = find_agy()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "models"], capture_output=True, timeout=40,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        out = decode_any(r.stdout) + decode_any(r.stderr)
    except subprocess.TimeoutExpired:
        return {"installed": True, "loggedIn": False}
    if "sign in" in out.lower() or "authentication required" in out.lower():
        return {"installed": True, "loggedIn": False}
    # Строки вида «gemini-3.8-flash-medium<TAB>Gemini 3.8 Flash (Medium)»; есть и Claude, и GPT
    models = [line.split("\t")[0].strip() for line in out.splitlines() if "\t" in line]
    return {"installed": True, "loggedIn": r.returncode == 0, "models": list(dict.fromkeys(models))}


def install_agy() -> tuple[bool, str]:
    """Официальный установщик Antigravity CLI для Windows."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "irm https://antigravity.google/cli/install.ps1 | iex"],
        capture_output=True, timeout=600, env=cli_env(), creationflags=NO_WINDOW)
    out = (decode_any(r.stdout) + "\n" + decode_any(r.stderr)).strip()
    return find_agy() is not None, out


def open_agy_login():
    """Первый запуск agy без аргументов открывает браузер для входа в Google-аккаунт."""
    subprocess.Popen([find_agy()], cwd=str(Path.home()),
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


# ---------- Gemini API ----------

def gemini_key(cfg) -> str | None:
    return (str(cfg.get("gemini_api_key", "")).strip()
            or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def check_gemini_key(cfg) -> tuple[bool, str, list[str]]:
    """Проверка ключа без траты токенов: получаем список доступных моделей."""
    from google import genai
    from google.genai import errors as gerr
    key = gemini_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    try:
        client = genai.Client(api_key=key)
        models = sorted(m.name.removeprefix("models/") for m in client.models.list()
                        if "generateContent" in (m.supported_actions or []) and "gemini" in m.name)
    except gerr.APIError as e:
        if e.code in (400, 401, 403):
            return False, "Ключ не подходит", []
        return False, f"Ошибка API ({e.code}): {e.message}", []
    except Exception as e:
        return False, f"Нет связи с Gemini API: {e}", []
    model = cfg.get("gemini_api_model") or GEMINI_API_DEFAULT
    if model not in models:
        return True, f"Ключ работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Ключ работает ✓  доступно моделей: {len(models)}", models


# ---------- ChatGPT: Codex CLI по подписке и OpenAI API по ключу ----------

NPM_GLOBAL = Path(os.environ.get("APPDATA", "")) / "npm"
NODE_DEFAULT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs"
CODEX_DIR = Path.home() / ".codex"
# Лишние для комментариев возможности Codex: меньше служебного текста — меньше расход лимита
CODEX_FEATURES_OFF = ["apps", "plugins", "multi_agent", "image_generation", "browser_use",
                      "computer_use", "goals", "shell_tool", "unified_exec"]
OPENAI_API_DEFAULT = "gpt-5.5"


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    exe = NODE_DEFAULT / "node.exe"
    return str(exe) if exe.exists() else None


def find_codex() -> str | None:
    """Запускаем сам codex.exe из npm-пакета, минуя codex.cmd: cmd.exe портит кириллицу в аргументах."""
    roots = [NPM_GLOBAL / "node_modules" / "@openai"]
    if shutil.which("codex"):
        roots.append(Path(shutil.which("codex")).parent / "node_modules" / "@openai")
    for root in roots:
        for pattern in ("codex/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
                        "codex-win32-*/vendor/*/bin/codex.exe"):
            found = sorted(root.glob(pattern))
            if found:
                return str(found[0])
    return None


def _codex_account() -> dict:
    """Почта и тариф из id_token в ~/.codex/auth.json (сам токен никуда не передаём)."""
    try:
        auth = json.loads((CODEX_DIR / "auth.json").read_text(encoding="utf-8"))
        payload = auth["tokens"]["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        plan = claims.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type", "")
        return {"email": claims.get("email", ""), "plan": plan}
    except Exception:
        return {}


def codex_status() -> dict:
    exe = find_codex()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "login", "status"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        out = (decode_any(r.stdout) + decode_any(r.stderr)).lower()
    except Exception:
        return {"installed": True, "loggedIn": False}
    if r.returncode != 0 or "not logged in" in out:
        return {"installed": True, "loggedIn": False}
    st = {"installed": True, "loggedIn": True,
          "authMethod": "chatgpt" if "chatgpt" in out else "api", **_codex_account()}
    try:   # модели, доступные этому аккаунту
        r = subprocess.run([exe, "debug", "models"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        data = json.loads(decode_any(r.stdout))
        st["models"] = [m["slug"] for m in data.get("models", data)
                        if m.get("visibility") == "list" and "image" in (m.get("input_modalities") or [])]
    except Exception:
        pass
    return st


def install_codex() -> tuple[bool, str]:
    """Node.js (если нет) через winget, затем Codex CLI через npm."""
    out = ""
    if not find_node():
        r = subprocess.run(["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"],
                           capture_output=True, timeout=900, env=cli_env(), creationflags=NO_WINDOW)
        out += decode_any(r.stdout) + decode_any(r.stderr)
        if not find_node():
            return False, out + "\nНе удалось установить Node.js"
    npm = Path(find_node()).parent / "npm.cmd"
    r = subprocess.run([str(npm), "install", "-g", "@openai/codex"], capture_output=True,
                       timeout=900, env=cli_env(), creationflags=NO_WINDOW)
    out += decode_any(r.stdout) + decode_any(r.stderr)
    return find_codex() is not None, out.strip()


def open_codex_login():
    """Вход через аккаунт ChatGPT. Если уже вошли — сначала выходим, чтобы сменить аккаунт."""
    exe = find_codex()
    if codex_status().get("loggedIn"):
        subprocess.run([exe, "logout"], capture_output=True, timeout=30, env=cli_env(), creationflags=NO_WINDOW)
    subprocess.Popen([exe, "login"], cwd=str(Path.home()),
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def openai_key(cfg) -> str | None:
    return str(cfg.get("openai_api_key", "")).strip() or os.environ.get("OPENAI_API_KEY")


def check_openai_key(cfg) -> tuple[bool, str, list[str]]:
    """Проверка ключа без траты токенов: получаем список доступных моделей."""
    import openai
    key = openai_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    try:
        client = openai.OpenAI(api_key=key, timeout=20, max_retries=1)
        models = sorted(m.id for m in client.models.list()
                        if m.id.startswith(("gpt-", "o")) and not any(
                            x in m.id for x in ("audio", "realtime", "tts", "transcribe", "image", "embedding", "search")))
    except openai.AuthenticationError:
        return False, "Ключ не подходит", []
    except openai.APIConnectionError:
        return False, "Нет связи с OpenAI API — проверьте интернет", []
    except openai.APIStatusError as e:
        return False, f"Ошибка API ({e.status_code}): {e.message}", []
    model = cfg.get("openai_api_model") or OPENAI_API_DEFAULT
    if model not in models:
        return True, f"Ключ работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Ключ работает ✓  доступно моделей: {len(models)}", models


# ---------- своя модель: любой OpenAI-совместимый сервер ----------
# LM Studio, Ollama, llama.cpp, vLLM — локально; OpenRouter, DeepSeek и т. п. — в облаке.
# Запросы — классическим chat/completions: его понимают все такие серверы.

COMPAT_PRESETS = {"LM Studio": "http://localhost:1234/v1", "Ollama": "http://localhost:11434/v1"}


def is_local_url(url: str) -> bool:
    """Сервер на этом компьютере или в домашней сети — к нему ходим мимо системного прокси (VPN)."""
    import ipaddress
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def compat_client(cfg, asynchronous=False):
    import httpx
    import openai
    url = str(cfg.get("compat_base_url", "")).strip().rstrip("/")
    key = str(cfg.get("compat_api_key", "")).strip() or "not-needed"   # локальным серверам ключ не нужен
    local = is_local_url(url)
    if asynchronous:
        http = httpx.AsyncClient(trust_env=False) if local else None
        return openai.AsyncOpenAI(base_url=url, api_key=key, http_client=http, max_retries=1)
    http = httpx.Client(trust_env=False) if local else None
    return openai.OpenAI(base_url=url, api_key=key, http_client=http, timeout=20, max_retries=0)


def compat_connection_hint(url: str) -> str:
    if is_local_url(url):
        return (f"Нет связи с {url} — запущен ли сервер? LM Studio: вкладка Developer → Start Server; "
                "Ollama: программа должна быть запущена")
    return f"Нет связи с {url} — проверьте адрес и интернет (или VPN)"


def check_compat(cfg) -> tuple[bool, str, list[str]]:
    """Проверка подключения без генерации: запрашиваем список моделей сервера."""
    import openai
    url = str(cfg.get("compat_base_url", "")).strip()
    if not url:
        return False, "Адрес сервера не указан", []
    try:
        with compat_client(cfg) as client:
            models = sorted(m.id for m in client.models.list())
    except openai.APIConnectionError:
        return False, compat_connection_hint(url), []
    except openai.AuthenticationError:
        return False, "Сервер требует API-ключ (или ключ не подходит)", []
    except openai.NotFoundError:
        return False, "По этому адресу нет OpenAI-совместимого API — адрес обычно оканчивается на /v1", []
    except openai.APIStatusError as e:
        return False, f"Ошибка сервера ({e.status_code}): {e.message}", []
    except Exception as e:
        return False, f"Не удалось подключиться: {e}", []
    if not models:
        return True, "Сервер работает ✓, но моделей нет — загрузите модель (в LM Studio — «Load model»)", []
    model = str(cfg.get("compat_model", "")).strip()
    if model and model not in models:
        return True, f"Сервер работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Сервер работает ✓  моделей: {len(models)}", models


THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)   # «размышления» DeepSeek-R1, Qwen3 и т. п.


def strip_reasoning(text: str) -> str:
    """Убирает «размышления» модели из ответа. Незакрытый <think> — ответ обрезан, публиковать нельзя."""
    text = THINK_RE.sub("", text)
    low = text.lower()
    if "</think>" in low:   # открывающий тег бывает в шаблоне модели, а в ответе только закрывающий
        text = text[low.rindex("</think>") + len("</think>"):]
    elif "<think>" in low:
        raise RuntimeError("Модель не закончила размышлять — ответ обрезан. Увеличьте лимит длины ответа "
                           "и контекст модели или возьмите модель без режима размышлений")
    return text.strip()


# ---------- пост, ожидающий решения ----------

@dataclass
class Pending:
    key: str                 # уникален в очереди: «чат:пост», у повторных ручных — «чат:пост#N»
    chat_id: int
    post_id: int
    post_text: str
    channel: str = ""        # @username или название канала
    profile: str = ""        # id профиля, от имени которого комментарий
    post_images: list[Path] = field(default_factory=list)
    comment: str = ""
    image: Path | None = None
    busy: bool = False       # идёт генерация/отправка
    error: str = ""

    @property
    def uid(self) -> str:
        """Ключ, уникальный среди всех профилей (один пост может ждать у нескольких аккаунтов)."""
        return f"{self.profile}|{self.key}"

    @property
    def post_key(self) -> str:
        """Сам пост — «чат:пост»: по нему помним, под какими постами уже есть комментарий."""
        return f"{self.chat_id}:{self.post_id}"


# ---------- движок ----------

class ProfileLog(logging.LoggerAdapter):
    """Записи движка помечаются именем профиля, если профилей несколько."""

    def process(self, msg, kwargs):
        e = self.extra["engine"]
        return (f"[{e.cfg['name']}] {msg}" if e.show_name else msg), kwargs


class Engine:
    """
    Один профиль = один Engine: свой аккаунт Telegram, свой поток и своя очередь.
    Все публичные методы вызываются из потока окна и безопасны:
    работа уходит в asyncio-цикл фонового потока.
    События для окна кладутся в self.events:
      ("status", str)                  stopped / connecting / login / running
      ("login_needed", None)
      ("qr", url)                      новый QR для входа
      ("password_needed", None)        нужен облачный пароль 2FA
      ("login_error", text)
      ("code_sent", None)
      ("me", name)
      ("pending", Pending)             новый пост ждёт решения
      ("pending_update", Pending)
      ("pending_done", (key, result))  published / skipped
      ("posted", None)                 счётчик опубликованных
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.log = ProfileLog(log, {"engine": self})
        self.show_name = False   # окно включает, когда профилей больше одного
        pid = cfg["id"]
        self.done_file = done_file(cfg)
        self.media_dir = MEDIA_DIR / pid
        # Промпт передаём файлом: многострочный текст в аргументах ломается в cmd.exe
        self.system_prompt_file = BASE / f"_system_prompt_{pid}.txt"
        # Пустые рабочие папки: agy и Codex не видят файлы проекта
        self.agy_workspace = BASE / f"_agy_workspace_{pid}"
        self.codex_workspace = BASE / f"_codex_workspace_{pid}"
        self.events: queue.Queue = queue.Queue()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.client: TelegramClient | None = None
        self.pending: dict[str, Pending] = {}
        self.done_posts = set(self.done_file.read_text().split()) if self.done_file.exists() else set()
        self._image_bag: list[Path] = []
        self._seen_groups: set[int] = set()
        self._gen_lock: asyncio.Lock | None = None
        self._api_client = None
        self._api_client_key = None
        self._gemini_client = None
        self._gemini_client_key = None
        self._openai_client = None
        self._openai_client_key = None
        self._qr_task: asyncio.Task | None = None
        self._dialogs_loaded = False   # список чатов подгружен (нужен, чтобы найти закрытый канал по id)
        self.inflight: set[str] = set()   # ключи постов, которые сейчас обрабатываются
        self._manual_seq = itertools.count(2)   # номера повторных вариантов одного поста
        self._phone = ""
        self.channel_names: dict[int, str] = {}
        self.running = False
        self.status = "stopped"
        self.me_name = ""

    # --- служебное ---

    def _emit(self, kind, data=None):
        if kind == "status":
            self.status = data
        elif kind == "me":
            self.me_name = data
        self.events.put((kind, data))

    def _submit(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(self._report_crash)
        return fut

    def _report_crash(self, fut):
        if not fut.cancelled() and fut.exception():
            self.log.error("Ошибка: %s", fut.exception())

    # --- запуск / остановка / вход ---

    def start(self):
        self._submit(self._start())

    def stop(self):
        self._submit(self._stop())

    def close(self):
        """Окончательно: отключиться и остановить поток движка (профиль удаляют)."""
        try:
            self._submit(self._stop()).result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    async def _start(self):
        if self.client:
            await self._stop()
        cfg = self.cfg
        self._emit("status", "connecting")
        self._gen_lock = asyncio.Lock()
        self._dialogs_loaded = False   # новое подключение — список чатов загрузим заново при надобности
        try:
            proxy = telegram_proxy(cfg)
        except ValueError as e:
            self._emit("status", "stopped")
            self.log.error("Прокси: %s", e)
            return
        kw = {}
        if proxy and proxy["type"] == "mtproxy":
            kw = {"connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
                  "proxy": (proxy["host"], proxy["port"], proxy["secret"])}
        elif proxy:
            kw = {"proxy": {"proxy_type": proxy["type"], "addr": proxy["host"], "port": proxy["port"],
                            "username": proxy["user"], "password": proxy["password"], "rdns": True}}
        if proxy:
            self.log.info("Подключаюсь к Telegram через прокси %s", proxy_label(proxy))
        self.client = TelegramClient(str(BASE / cfg["session_name"]),
                                     int(cfg["api_id"]), str(cfg["api_hash"]).strip(), **kw)
        try:
            await self.client.connect()
        except Exception as e:
            self.client = None
            self._emit("status", "stopped")
            if proxy:
                self.log.error("Не удалось подключиться к Telegram через прокси %s: %s — проверьте, "
                               "что VPN включён и адрес прокси верный", proxy_label(proxy), e)
            else:
                self.log.error("Не удалось подключиться к Telegram: %s — если Telegram у вас "
                               "заблокирован, включите VPN (см. «Прокси» в «Настройках»)", e)
            return
        if await self.client.is_user_authorized():
            await self._after_login()
        else:
            self._emit("status", "login")
            self._emit("login_needed")

    async def _stop(self):
        if self._qr_task:
            self._qr_task.cancel()
            self._qr_task = None
        if self.client:
            await self.client.disconnect()
            self.client = None
        for p in list(self.pending.values()):
            self._finish(p, "skipped")
        if self.running:
            self.log.info("Остановлено")
        self.running = False
        self._emit("status", "stopped")

    def send_code(self, phone: str):
        async def go():
            self._phone = phone.strip()
            try:
                await self.client.send_code_request(self._phone)
                self._emit("code_sent")
            except Exception as e:
                self._emit("login_error", f"Не удалось отправить код: {e}")
        self._submit(go())

    def sign_in_code(self, code: str):
        async def go():
            try:
                await self.client.sign_in(self._phone, code.strip())
            except errors.SessionPasswordNeededError:
                self._emit("password_needed")
                return
            except Exception as e:
                self._emit("login_error", f"Код не подошёл: {e}")
                return
            await self._after_login()
        self._submit(go())

    def sign_in_password(self, password: str):
        async def go():
            try:
                await self.client.sign_in(password=password)
            except Exception as e:
                self._emit("login_error", f"Пароль не подошёл: {e}")
                return
            await self._after_login()
        self._submit(go())

    def start_qr(self):
        async def go():
            if not self.client:   # окно входа открыто, а подключения уже нет
                return
            if self._qr_task:
                self._qr_task.cancel()
            self._qr_task = asyncio.current_task()
            qr = await self.client.qr_login()
            while True:
                self._emit("qr", qr.url)
                try:
                    await qr.wait(timeout=30)
                    break
                except asyncio.TimeoutError:
                    await qr.recreate()
                except errors.SessionPasswordNeededError:
                    self._qr_task = None
                    self._emit("password_needed")
                    return
            self._qr_task = None
            await self._after_login()
        self._submit(go())

    def cancel_login(self):
        self.stop()

    async def _resolve_channel(self, name: str):
        """Канал по записи из списка (см. normalize_channel). ValueError — с понятной причиной."""
        from telethon.tl import functions, types
        if name.startswith("t.me/+"):   # ссылка-приглашение в закрытый канал
            try:
                inv = await self.client(functions.messages.CheckChatInviteRequest(name[len("t.me/+"):]))
            except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
                raise ValueError("ссылка-приглашение недействительна или устарела")
            if isinstance(inv, types.ChatInviteAlready):
                return inv.chat
            # Вступать сами не будем — это действие от имени аккаунта
            raise ValueError("аккаунт не состоит в этом канале — вступите в него по ссылке в Telegram "
                             "и перезапустите аккаунт")
        if name.startswith("t.me/c/"):   # закрытый канал по id
            peer = int("-100" + name[len("t.me/c/"):])
            try:
                return await self.client.get_entity(peer)
            except ValueError:
                pass
            # Telegram отдаёт закрытый канал по id, только если он есть в списке чатов аккаунта
            if not self._dialogs_loaded:
                await self.client.get_dialogs()
                self._dialogs_loaded = True
            try:
                return await self.client.get_entity(peer)
            except ValueError:
                raise ValueError("аккаунт не состоит в этом закрытом канале")
        return await self.client.get_entity(name)

    @staticmethod
    def _channel_title(ent) -> str:
        return "@" + ent.username if getattr(ent, "username", None) else getattr(ent, "title", "канал")

    async def _after_login(self):
        cfg = self.cfg
        me = await self.client.get_me()
        entities = []
        self.channel_names = {}
        for name in channels(cfg):
            try:
                ent = await self._resolve_channel(name)
            except Exception as e:
                self.log.error("Канал %s не найден — пропускаю: %s", channel_label(name), e)
                continue
            entities.append(ent)
            self.channel_names[utils.get_peer_id(ent)] = self._channel_title(ent)
        if not entities:
            self.log.error("Ни один канал не найден — проверьте список в «Настройках»")
            await self._stop()
            return
        self.client.add_event_handler(self._on_new_post, events.NewMessage(chats=entities))
        self.running = True
        self._emit("me", me.first_name)
        self._emit("status", "running")
        self.log.info("Вошли как %s, слушаю каналы: %s", me.first_name, ", ".join(self.channel_names.values()))
        self.log.info("Картинок для комментариев: %d", len(list_images(cfg)))

    # --- картинки ---

    def pick_image(self, force=False) -> Path | None:
        """Перемешанная «колода»: каждая картинка выпадет по разу, потом новый круг."""
        if not force and random.random() > self.cfg.get("attach_image_chance", 1.0):
            return None
        if not self._image_bag:
            self._image_bag = list_images(self.cfg)
            random.shuffle(self._image_bag)
        return self._image_bag.pop() if self._image_bag else None

    def reset_deck(self):
        self._image_bag = []

    # --- обработка постов ---

    async def _collect_post(self, msg):
        """Возвращает (id для коммента, текст, список сообщений с медиа). Склеивает альбомы."""
        if not msg.grouped_id:
            return msg.id, msg.raw_text or "", [msg]
        await asyncio.sleep(2)  # даём долететь остальным частям альбома
        around = await self.client.get_messages(msg.chat_id,
                                                ids=list(range(msg.id - 10, msg.id + 11)))
        parts = sorted((m for m in around if m and m.grouped_id == msg.grouped_id),
                       key=lambda m: m.id)
        text = next((m.raw_text for m in parts if m.raw_text), "")
        return parts[0].id, text, parts

    async def _download_post_images(self, parts, folder: Path) -> list[Path]:
        """Качает фото (и превью видео) из поста во временную папку поста."""
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for m in parts:
            if len(files) >= self.cfg.get("max_post_images", 4):
                break
            target = folder / f"img{len(files) + 1}.jpg"
            try:
                if m.photo:
                    path = await self.client.download_media(m, file=str(target))
                elif m.video or m.gif:
                    path = await self.client.download_media(m, file=str(target), thumb=-1)
                else:
                    continue
                if path:
                    files.append(Path(path))
            except Exception as e:
                self.log.warning("Не удалось скачать медиа из #%s: %s", m.id, e)
        return files

    def _is_sensitive(self, text: str) -> str | None:
        low = text.lower()
        return next((k for k in self.cfg.get("skip_keywords", []) if k and k.lower() in low), None)

    async def _on_new_post(self, event):
        await self._handle_post(event.message, manual=False)

    def take_latest_post(self, channel: str | None = None):
        """Взять последний пост канала — удобно, чтобы проверить промпт."""
        async def go():
            name = normalize_channel(channel or "") or channels(self.cfg)[0]
            try:
                ent = await self._resolve_channel(name)
            except (ValueError, errors.RPCError) as e:
                self.log.warning("Канал %s не найден: %s", channel_label(name), e)
                return
            msgs = await self.client.get_messages(ent, limit=1)
            if not msgs:
                self.log.warning("В канале %s нет постов", channel_label(name))
                return
            self.channel_names.setdefault(utils.get_peer_id(ent), self._channel_title(ent))
            await self._handle_post(msgs[0], manual=True)
        self._submit(go())

    def take_post_by_link(self, link: str):
        """Взять конкретный пост по ссылке — канал может и не быть в списке."""
        async def go():
            try:
                peer, post_id = parse_post_link(link)
            except ValueError as e:
                self.log.warning("Ссылка на пост: %s", e)
                return
            key = f"t.me/c/{str(peer)[4:]}" if isinstance(peer, int) else peer   # -100<id> → t.me/c/<id>
            try:
                ent = await self._resolve_channel(key)
            except (ValueError, errors.RPCError) as e:
                self.log.warning("Канал %s не найден: %s", channel_label(key), e)
                return
            msg = await self.client.get_messages(ent, ids=post_id)
            name = self._channel_title(ent)
            if not msg:
                self.log.warning("Пост #%s в %s не найден — удалён или ссылка неверная", post_id, name)
                return
            # У альбома сведения о комментариях есть не у каждой части — его не проверяем
            if not msg.grouped_id and not (msg.replies and msg.replies.comments):
                self.log.warning("Под постом #%s в %s нельзя комментировать — у канала выключены комментарии",
                                 post_id, name)
                return
            self.channel_names[utils.get_peer_id(ent)] = name
            await self._handle_post(msg, manual=True)
        self._submit(go())

    async def _handle_post(self, msg, manual: bool):
        cfg = self.cfg
        if msg.grouped_id and not manual:
            if msg.grouped_id in self._seen_groups:   # альбом уже обрабатывается
                return
            self._seen_groups.add(msg.grouped_id)

        post_id, post_text, parts = await self._collect_post(msg)
        key = f"{msg.chat_id}:{post_id}"
        queued = any(q.post_key == key for q in self.pending.values()) or key in self.inflight
        if not manual:
            # Сам по себе каждый пост комментируем один раз
            if queued or key in self.done_posts:
                return
        else:
            # Взятый вручную — всегда новый вариант, даже если комментарий уже есть или ждёт в очереди
            if key in self.done_posts:
                self.log.info("Под постом #%s уже есть ваш комментарий — готовлю ещё один", post_id)
            elif queued:
                self.log.info("Пост #%s уже в очереди — готовлю ещё один вариант", post_id)
            if queued or key in self.pending:
                key = f"{key}#{next(self._manual_seq)}"
        chan = self.channel_names.get(msg.chat_id, "")
        self.log.info("Новый пост %s #%s: %s", chan, post_id, post_text[:80].replace("\n", " "))

        see_images = cfg.get("send_post_images", False)
        has_media = any(m.photo or m.video or m.gif for m in parts)
        if not post_text.strip() and not (see_images and has_media):
            self.log.info("Пост #%s без текста — пропуск", post_id)
            return
        word = self._is_sensitive(post_text)
        if word and not manual:
            self.log.info("Пост #%s: стоп-слово «%s» — пропуск", post_id, word)
            return

        p = Pending(key=key, chat_id=msg.chat_id, post_id=post_id, post_text=post_text, channel=chan,
                    profile=self.cfg["id"])
        # Пока пост в работе (генерация, пауза перед автопубликацией), выход и автообновление ждут
        self.inflight.add(key)
        try:
            await self._comment_on(p, parts, manual or cfg.get("confirm_before_post", True),
                                   see_images and has_media)
        finally:
            self.inflight.discard(key)

    def busy_auto(self) -> int:
        """Сколько постов в работе вне очереди подтверждения — автопубликация ещё не закончена."""
        return len(self.inflight - self.pending.keys())

    async def _comment_on(self, p: Pending, parts, confirm: bool, with_images: bool):
        cfg, post_id = self.cfg, p.post_id
        if with_images:
            # Своя папка у каждого варианта: картинки двух вариантов одного поста не мешают друг другу
            folder = self.media_dir / p.key.replace(":", "_").replace("#", "_")
            p.post_images = await self._download_post_images(parts, folder)
            self.log.info("Картинок из поста для нейронки: %d", len(p.post_images))

        key, post_text = p.key, p.post_text
        if confirm:
            p.busy = True
            self.pending[key] = p
            self._emit("pending", p)

        try:
            p.comment = await self._generate(post_text, p.post_images) or ""
        except Exception as e:
            p.error = self._ai_error(e)
            self.log.error("%s", p.error)
        p.image = self.pick_image()

        if not p.comment and not p.error:
            self.log.info("Пост #%s: модель решила пропустить (SKIP)", post_id)
            if not confirm:
                self._cleanup(p)
                return
            p.error = "Модель ответила SKIP (тяжёлая тема). Можно перегенерировать или пропустить."

        if confirm:
            p.busy = False
            self._emit("pending_update", p)
            return

        if p.error:
            self._cleanup(p)
            return
        delay = random.randint(cfg["delay_min_sec"], max(cfg["delay_min_sec"], cfg["delay_max_sec"]))
        self.log.info("Пост #%s: жду %s сек перед публикацией", post_id, delay)
        await asyncio.sleep(delay)
        await self._send(p)
        self._cleanup(p)

    def _ai_error(self, e: Exception) -> str:
        return explain_ai_error(BACKEND_NAMES.get(self.cfg.get("backend"), "Claude Code"), str(e))

    async def _generate(self, post_text: str, images: list[Path], wish: str = "") -> str | None:
        async with self._gen_lock:   # по одному запросу к нейросети за раз
            system = prompt_path(self.cfg).read_text(encoding="utf-8") + SKIP_RULE
            user_msg = f"Текст поста:\n\n{post_text or '(текста нет, только медиа)'}"
            if wish.strip():
                user_msg += f"\n\nПОЖЕЛАНИЯ: {wish.strip()}"
            gen = {"api": self._gen_api, "gemini_cli": self._gen_gemini_cli,
                   "gemini_api": self._gen_gemini_api, "codex": self._gen_codex,
                   "openai_api": self._gen_openai_api,
                   "openai_compat": self._gen_compat}.get(self.cfg.get("backend"), self._gen_claude_code)
            text = await gen(system, user_msg, images)
            if not text or text.upper().startswith("SKIP"):
                return None
            return text

    async def _gen_claude_code(self, system: str, user_msg: str, images: list[Path]) -> str:
        cfg = self.cfg
        self.system_prompt_file.write_text(system, encoding="utf-8")
        args = [find_claude(), "-p",
                "--system-prompt-file", str(self.system_prompt_file),
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

        # Чтобы Claude Code не ушёл на платный API вместо подписки
        env = cli_env(drop=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env, cwd=cwd,
            creationflags=NO_WINDOW,  # без чёрного окна
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
        return decode_any(out).strip()

    async def _gen_gemini_cli(self, system: str, user_msg: str, images: list[Path]) -> str:
        """Antigravity CLI: наш промпт кладём как отдельного агента в пустую рабочую папку."""
        cfg = self.cfg
        ws = self.agy_workspace
        shutil.rmtree(ws, ignore_errors=True)
        agent_dir = ws / ".agents" / "agents" / AGY_AGENT
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.md").write_text(
            f"---\nname: {AGY_AGENT}\ndescription: Пишет один комментарий под пост в Telegram\n---\n"
            + system, encoding="utf-8")
        prompt = user_msg
        if images:
            for img in images:
                shutil.copy(img, ws / img.name)
            prompt += ("\n\nК посту приложены картинки — файлы " + ", ".join(p.name for p in images)
                       + " в текущей папке. Посмотри их и учти в комментарии.")
        prompt += "\n\nВыведи только сам текст комментария, без пояснений."

        timeout = cfg.get("claude_timeout_sec", 120) + 30 * len(images)
        args = [find_agy(), "--agent", AGY_AGENT, "--output-format", "json",
                "--disable-slash-commands", "--print-timeout", f"{timeout}s"]
        if cfg.get("gemini_model"):
            args += ["--model", cfg["gemini_model"]]
        args += ["-p", prompt]
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=cli_env(), cwd=str(ws), creationflags=NO_WINDOW)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 15)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Antigravity не ответил вовремя")
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        text_out, text_err = decode_any(out), decode_any(err)
        if "sign in" in (text_out + text_err).lower() or "authentication required" in (text_out + text_err).lower():
            raise RuntimeError("Не выполнен вход в Google для Antigravity — «Настройки» → «Войти в Google»")
        try:
            data = json.loads(text_out[text_out.index("{"):])
        except ValueError:
            raise RuntimeError((text_err or text_out).strip()[-500:] or f"код выхода {proc.returncode}")
        if data.get("status") != "SUCCESS":
            raise RuntimeError(f"Antigravity: {data.get('error') or data.get('status')}")
        return (data.get("response") or "").strip()

    async def _gen_gemini_api(self, system: str, user_msg: str, images: list[Path]) -> str:
        from google import genai
        from google.genai import errors as gerr, types
        cfg = self.cfg
        key = gemini_key(cfg)
        if self._gemini_client is None or self._gemini_client_key != key:
            self._gemini_client = genai.Client(api_key=key)
            self._gemini_client_key = key
        parts = [types.Part.from_bytes(data=img.read_bytes(),
                                       mime_type=mimetypes.guess_type(img.name)[0] or "image/jpeg")
                 for img in images]
        if images:
            user_msg += "\n\nК посту приложены картинки (выше) — учти, что на них. Выведи только сам комментарий."
        parts.append(user_msg)
        model = cfg.get("gemini_api_model") or GEMINI_API_DEFAULT
        try:
            resp = await asyncio.wait_for(
                self._gemini_client.aio.models.generate_content(
                    model=model, contents=parts,
                    config=types.GenerateContentConfig(
                        system_instruction=system, max_output_tokens=8192,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))),
                timeout=cfg.get("claude_timeout_sec", 120))
        except asyncio.TimeoutError:
            raise RuntimeError("Gemini API не ответил вовремя")
        except gerr.APIError as e:
            if e.code in (401, 403) or "API key" in str(e.message):
                raise RuntimeError("Ключ Gemini не подходит — проверьте его в «Настройках»")
            if e.code == 429:
                raise RuntimeError("Исчерпан лимит запросов Gemini API — подождите или проверьте тариф")
            if e.code == 404:
                raise RuntimeError(f"Модель {model} не найдена — выберите другую в «Настройках»")
            raise RuntimeError(f"Ошибка Gemini API ({e.code}): {e.message}")
        except Exception as e:
            raise RuntimeError(f"Нет связи с Gemini API: {e}")
        u = resp.usage_metadata
        if u:
            self.log.info("Gemini %s: вход %s ток., выход %s ток.", model,
                     u.prompt_token_count, (u.candidates_token_count or 0) + (u.thoughts_token_count or 0))
        if not resp.text:
            self.log.info("Gemini не вернул текст (сработал фильтр безопасности или пустой ответ)")
            return ""
        return resp.text.strip()

    async def _gen_codex(self, system: str, user_msg: str, images: list[Path]) -> str:
        """Codex CLI по подписке ChatGPT: наш промпт заменяет встроенные инструкции Codex."""
        cfg = self.cfg
        ws = self.codex_workspace
        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True)
        instr, last = ws / "instructions.md", ws / "last_message.txt"
        instr.write_text(system, encoding="utf-8")
        args = [find_codex(), "exec", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "read-only", "--color", "never", "-C", str(ws),
                "-c", f"model_instructions_file='{instr}'",
                "-c", "project_doc_max_bytes=0",          # не читать AGENTS.md
                "-c", "skills.include_instructions=false",
                "-c", 'web_search="disabled"',
                "-c", 'forced_login_method="chatgpt"',    # только подписка, не API-ключ
                "-o", str(last)]
        for feature in CODEX_FEATURES_OFF:
            args += ["--disable", feature]
        if cfg.get("codex_model"):
            args += ["-m", cfg["codex_model"]]
        args += [f"--image={img}" for img in images]
        if images:
            user_msg += "\n\nК посту приложены картинки — учти, что на них."
        user_msg += "\n\nВыведи только сам текст комментария, без пояснений."
        env = cli_env(drop=("OPENAI_API_KEY", "CODEX_API_KEY"))
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(ws), creationflags=NO_WINDOW)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(user_msg.encode("utf-8")),
                timeout=cfg.get("claude_timeout_sec", 120) + 30 * len(images))
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Codex не ответил вовремя")
        try:
            text = last.read_text(encoding="utf-8").strip() if last.exists() else ""
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        if proc.returncode != 0 or not text:
            msg = (decode_any(err) + decode_any(out)).strip()
            low = msg.lower()
            if "not logged in" in low or "401 unauthorized" in low:
                raise RuntimeError("Не выполнен вход в ChatGPT — «Настройки» → «Войти в ChatGPT»")
            if "usage limit" in low or "rate limit" in low:
                raise RuntimeError("Исчерпан лимит подписки ChatGPT для Codex — подождите сброса")
            lines = [l for l in msg.splitlines() if "error" in l.lower()]
            raise RuntimeError("Codex: " + (lines[-1] if lines else msg[-400:] or f"код выхода {proc.returncode}"))
        return text

    async def _gen_compat(self, system: str, user_msg: str, images: list[Path]) -> str:
        """Своя модель на OpenAI-совместимом сервере (LM Studio, Ollama…) через chat/completions."""
        import openai
        cfg = self.cfg
        url = str(cfg.get("compat_base_url", "")).strip().rstrip("/")
        sig = (url, cfg.get("compat_api_key", ""))
        if getattr(self, "_compat_sig", None) != sig:
            self._compat_client, self._compat_sig = compat_client(cfg, asynchronous=True), sig
        content: str | list = user_msg
        if images:   # картинки понимают только модели с vision; без них сервер вернёт ошибку
            content = [{"type": "text", "text": user_msg + "\n\nК посту приложены картинки — учти, что на них. "
                                                           "Выведи только сам комментарий."}]
            for img in images:
                mime = mimetypes.guess_type(img.name)[0] or "image/jpeg"
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"}})
        model = str(cfg.get("compat_model", "")).strip()
        try:
            resp = await self._compat_client.with_options(
                timeout=float(cfg.get("claude_timeout_sec", 120))).chat.completions.create(
                model=model, messages=[{"role": "system", "content": system},
                                       {"role": "user", "content": content}])
        except openai.APIConnectionError:
            raise RuntimeError(compat_connection_hint(url))
        except openai.APITimeoutError:
            raise RuntimeError("Модель не ответила вовремя — увеличьте «Таймаут ответа» или возьмите модель поменьше")
        except openai.AuthenticationError:
            raise RuntimeError("Сервер требует API-ключ (или ключ не подходит)")
        except openai.NotFoundError:
            raise RuntimeError(f"Модель {model} не найдена или не загружена — «Проверить подключение» в «Настройках»")
        except openai.APIStatusError as e:
            low = str(e.message).lower()
            if "context" in low and any(w in low for w in ("length", "window", "overflow", "exceed", "too long")):
                raise RuntimeError("Промпт не помещается в контекст модели — увеличьте длину контекста "
                                   "(LM Studio: настройки модели → Context Length, нужно 16–32 тыс. токенов и больше) "
                                   "или сократите промпт")
            raise RuntimeError(f"Ошибка сервера ({e.status_code}): {e.message}")
        u = resp.usage
        if u:
            self.log.info("Своя модель %s: вход %s ток., выход %s ток.", model, u.prompt_tokens, u.completion_tokens)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        return strip_reasoning(text)

    async def _gen_openai_api(self, system: str, user_msg: str, images: list[Path]) -> str:
        import openai
        cfg = self.cfg
        key = openai_key(cfg)
        if self._openai_client is None or self._openai_client_key != key:
            self._openai_client = openai.AsyncOpenAI(api_key=key)
            self._openai_client_key = key
        if images:
            user_msg += "\n\nК посту приложены картинки — учти, что на них. Выведи только сам комментарий."
        content = [{"type": "input_text", "text": user_msg}]
        for img in images:
            mime = mimetypes.guess_type(img.name)[0] or "image/jpeg"
            content.append({"type": "input_image",
                            "image_url": f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"})
        model = cfg.get("openai_api_model") or OPENAI_API_DEFAULT
        try:
            resp = await self._openai_client.with_options(
                timeout=float(cfg.get("claude_timeout_sec", 120))).responses.create(
                model=model, instructions=system, input=[{"role": "user", "content": content}])
        except openai.AuthenticationError:
            raise RuntimeError("Ключ OpenAI не подходит — проверьте его в «Настройках»")
        except openai.RateLimitError as e:
            raise RuntimeError(f"OpenAI: лимит или нет средств на балансе — {e.message}")
        except openai.NotFoundError:
            raise RuntimeError(f"Модель {model} недоступна — выберите другую в «Настройках»")
        except openai.APITimeoutError:
            raise RuntimeError("OpenAI API не ответил вовремя")
        except openai.APIConnectionError:
            raise RuntimeError("Нет связи с OpenAI API — проверьте интернет")
        except openai.APIStatusError as e:
            raise RuntimeError(f"Ошибка OpenAI API ({e.status_code}): {e.message}")
        u = resp.usage
        if u:
            cached = getattr(u.input_tokens_details, "cached_tokens", 0) if u.input_tokens_details else 0
            self.log.info("OpenAI %s: вход %s ток. (из кэша %s), выход %s ток.", model,
                     u.input_tokens, cached, u.output_tokens)
        return (resp.output_text or "").strip()

    async def _gen_api(self, system: str, user_msg: str, images: list[Path]) -> str:
        import anthropic
        cfg = self.cfg
        key = api_key(cfg)
        if self._api_client is None or self._api_client_key != key:
            self._api_client = anthropic.AsyncAnthropic(api_key=key)
            self._api_client_key = key
        content = []
        for img in images:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": mimetypes.guess_type(img.name)[0] or "image/jpeg",
                "data": base64.standard_b64encode(img.read_bytes()).decode()}})
        if images:
            user_msg += "\n\nК посту приложены картинки (выше) — учти, что на них. Выведи только сам комментарий."
        content.append({"type": "text", "text": user_msg})
        model = cfg.get("api_model", API_MODELS[0])
        kw = dict(
            model=model, max_tokens=16000,
            # Промпт большой и одинаковый — кэшируем, повторные запросы в разы дешевле
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
        )
        client = self._api_client.with_options(timeout=float(cfg.get("claude_timeout_sec", 120)))
        try:
            if model.startswith("claude-haiku"):
                resp = await client.messages.create(**kw)
            else:
                kw["output_config"] = {"effort": "medium"}
                try:
                    # Если фильтр модели откажет, API сам повторит запрос на подходящей модели
                    resp = await client.beta.messages.create(
                        **kw, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                except anthropic.BadRequestError as e:
                    if "fallback" not in str(e.message).lower():
                        raise
                    resp = await client.messages.create(**kw)
        except anthropic.AuthenticationError:
            raise RuntimeError("API-ключ не подходит — проверьте его в «Настройках»")
        except anthropic.PermissionDeniedError as e:
            raise RuntimeError(f"Нет доступа к модели {model}: {e.message}")
        except anthropic.RateLimitError:
            raise RuntimeError("Слишком много запросов к API — попробуйте чуть позже")
        except anthropic.BadRequestError as e:
            raise RuntimeError(f"API отклонил запрос: {e.message}")
        except anthropic.APITimeoutError:
            raise RuntimeError("API не ответил вовремя")
        except anthropic.APIConnectionError:
            raise RuntimeError("Нет связи с API Anthropic — проверьте интернет")
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Ошибка API ({e.status_code}): {e.message}")

        u = resp.usage
        self.log.info("API %s: вход %s ток. (из кэша %s), выход %s ток.", resp.model,
                 u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
                 u.cache_read_input_tokens or 0, u.output_tokens)
        if resp.stop_reason == "refusal":
            self.log.info("Модель отказалась отвечать на этот пост")
            return ""
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    async def _send(self, p: Pending) -> bool:
        for attempt in range(2):
            try:
                await self.client.send_message(
                    p.chat_id, p.comment, comment_to=p.post_id,
                    file=str(p.image) if p.image else None,
                )
                self.log.info("✅ Комментарий опубликован под #%s (картинка: %s)",
                         p.post_id, p.image.name if p.image else "нет")
                self.done_posts.add(p.post_key)
                with self.done_file.open("a") as f:
                    f.write(p.post_key + "\n")
                self._emit("posted")
                return True
            except errors.FloodWaitError as e:
                if attempt == 0 and e.seconds <= 300:
                    self.log.warning("Telegram просит подождать %s сек — подожду и повторю", e.seconds)
                    await asyncio.sleep(e.seconds + 1)
                    continue
                p.error = f"Telegram ограничил отправку на {e.seconds} сек"
            except errors.MsgIdInvalidError:
                p.error = "У поста нет обсуждения (комментарии выключены)"
            except Exception as e:
                p.error = f"Не удалось отправить: {e}"
            self.log.error("Пост #%s: %s", p.post_id, p.error)
            return False
        return False

    def _cleanup(self, p: Pending):
        if p.post_images:
            shutil.rmtree(p.post_images[0].parent, ignore_errors=True)

    # --- действия из окна над постом в очереди ---

    def publish(self, key: str, text: str, image: Path | None):
        async def go():
            p = self.pending.get(key)
            if not p or p.busy:
                return
            p.comment, p.image, p.busy, p.error = text.strip(), image, True, ""
            self._emit("pending_update", p)
            ok = await self._send(p)
            p.busy = False
            if ok:
                self._finish(p, "published")
            else:
                self._emit("pending_update", p)
        self._submit(go())

    def regenerate(self, key: str, wish: str = ""):
        async def go():
            p = self.pending.get(key)
            if not p or p.busy:
                return
            p.busy, p.error = True, ""
            self._emit("pending_update", p)
            try:
                text = await self._generate(p.post_text, p.post_images, wish)
                if text:
                    p.comment = text
                else:
                    p.error = "Модель ответила SKIP (тяжёлая тема)"
            except Exception as e:
                p.error = self._ai_error(e)
                self.log.error("%s", p.error)
            p.busy = False
            self._emit("pending_update", p)
        self._submit(go())

    def skip(self, key: str):
        async def go():
            p = self.pending.get(key)
            if p and not p.busy:
                self.log.info("Пост #%s пропущен вручную", p.post_id)
                self._finish(p, "skipped")
        self._submit(go())

    def _finish(self, p: Pending, result: str):
        self.pending.pop(p.key, None)
        self._cleanup(p)
        self._emit("pending_done", (p.key, result))
