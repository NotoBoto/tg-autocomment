"""
Окно автокомментатора. Запуск: start.bat или python app.py
"""
import logging
import os
import queue
import shutil
import socket
import sys
import threading
import webbrowser
import zlib
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import qrcode
from PIL import Image

import core
import updater
from core import BASE, Engine, Pending

GREEN, GREEN_HOVER = "#2e9e5b", "#257f49"
GRAY, GRAY_HOVER = ("#9aa0a6", "#4a4d52"), ("#80868b", "#5f6368")
RED = "#d9534f"
STATUS = {
    "stopped": ("Остановлен", "#9aa0a6"),
    "connecting": ("Подключаюсь…", "#f0ad4e"),
    "login": ("Нужен вход в Telegram", "#f0ad4e"),
    "running": ("Работает", "#2e9e5b"),
}
BTN_COLOR = ctk.ThemeManager.theme["CTkButton"]["fg_color"]
BTN_HOVER = ctk.ThemeManager.theme["CTkButton"]["hover_color"]
LABEL_W = 270  # ширина колонки подписей в блоке «Нейросеть»
BACKENDS = {"Claude Code": "claude_code", "Claude API": "api",
            "Antigravity (Google)": "gemini_cli", "Gemini API": "gemini_api",
            "ChatGPT (Codex)": "codex", "OpenAI API": "openai_api",
            "Своя модель": "openai_compat"}
DEFAULT_MODEL = "(по умолчанию)"
PROXY_MODES = {"Системный (из VPN)": "system", "Свой": "custom", "Без прокси": "none"}
MODE_CONFIRM, MODE_AUTO = "Подтверждать вручную", "Публиковать сами"

log = logging.getLogger("autocomment")


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(record)


def setup_logging() -> queue.Queue:
    q = queue.Queue()
    fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s")
    file_h = logging.FileHandler(BASE / "log.txt", encoding="utf-8")
    file_h.setFormatter(fmt)
    gui_h = QueueHandler(q)
    log.setLevel(logging.INFO)
    log.addHandler(file_h)
    log.addHandler(gui_h)
    # Служебный шум Telethon («Got difference…») — только предупреждения и в файл
    tl = logging.getLogger("telethon")
    tl.setLevel(logging.WARNING)
    tl.addHandler(file_h)
    return q


def thumbnail(path: Path | None, box: int) -> ctk.CTkImage | None:
    if not path or path.suffix.lower() == ".mp4" or not path.exists():
        return None
    try:
        img = Image.open(path)
        img.thumbnail((box, box))
        return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
    except Exception:
        return None


def open_path(p: Path):
    os.startfile(str(p))  # Windows


# Две копии программы мешали бы друг другу (одни и те же сессии Telegram, двойные комментарии).
# Первая копия слушает локальный порт; вторая просит её показать окно и выходит.
INSTANCE_HELLO, INSTANCE_REPLY = b"tg-autocomment:show", b"tg-autocomment:ok"
INSTANCE_PORT = 20000 + zlib.crc32(str(BASE).encode()) % 20000   # своя у каждой папки с программой


def single_instance() -> socket.socket | None | bool:
    """Сокет первой копии; False — программа уже запущена (окно ей показано); None — проверка невозможна."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as c:
            c.sendall(INSTANCE_HELLO)
            if c.recv(64) == INSTANCE_REPLY:
                return False
    except OSError:
        pass
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen()
        return srv
    except OSError:   # порт занят чем-то другим — просто работаем без проверки
        srv.close()
        return None


# ======================================================================
#  Значок в трее
# ======================================================================

APP_COLOR = "#2b7bd0"   # цвет значка программы (assets/icon.ico рисуется из tray_image)


def tray_image(color: str, size: int = 64) -> Image.Image:
    """Облачко комментария на круге цвета статуса."""
    from PIL import ImageDraw
    k = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size - 1, size - 1), fill=color)
    d.rounded_rectangle((14 * k, 17 * k, 50 * k, 41 * k), radius=7 * k, fill="white")
    d.polygon([(20 * k, 40 * k), (20 * k, 50 * k), (30 * k, 40 * k)], fill="white")
    return img


class Tray:
    """Значок в трее: окно можно закрыть, а программа продолжит работать.
    Меню pystray работает в своём потоке — действия передаём в окно через app.after."""

    def __init__(self, app: "App"):
        import pystray
        self.app = app
        item = pystray.MenuItem
        self.icon = pystray.Icon("tg_autocomment", tray_image(STATUS["stopped"][1]), "TG Автокомментатор",
                                 pystray.Menu(
                                     item("Открыть", lambda: app.after(0, app.show_window), default=True),
                                     item(lambda i: "■  Остановить" if app.status != "stopped" else "▶  Запустить",
                                          lambda: app.after(0, app.toggle_run)),
                                     pystray.Menu.SEPARATOR,
                                     item("Выход", lambda: app.after(0, app.quit_app))))
        self.color = STATUS["stopped"][1]
        self.icon.run_detached()

    def update(self, status: str, tip: str):
        color = STATUS[status][1]
        if color != self.color:
            self.icon.icon, self.color = tray_image(color), color
        self.icon.title = tip[:127]   # у Windows ограничение длины подсказки
        self.icon.update_menu()

    def notify(self, text: str):
        try:
            self.icon.notify(text, "TG Автокомментатор")
        except Exception:
            pass

    def stop(self):
        self.icon.stop()


# ======================================================================
#  Окно входа в Telegram
# ======================================================================

class LoginDialog(ctk.CTkToplevel):
    def __init__(self, app: "App", engine: Engine):
        super().__init__(app)
        self.app, self.engine = app, engine
        multi = len(app.engines) > 1
        self.title(f"Вход в Telegram — {engine.cfg['name']}" if multi else "Вход в Telegram")
        self.geometry("440x560")
        self.resizable(False, False)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.after(100, self.grab_set)

        head = (f"Профиль «{engine.cfg['name']}»: войдите в аккаунт,\nот имени которого он будет комментировать"
                if multi else "Войдите в аккаунт, от имени которого\nбудут публиковаться комментарии")
        ctk.CTkLabel(self, text=head, font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(18, 8))

        self.tabs = ctk.CTkTabview(self, height=380)
        self.tabs.pack(fill="both", expand=True, padx=16)
        qr_tab = self.tabs.add("По QR-коду")
        ph_tab = self.tabs.add("По номеру")

        # --- QR ---
        ctk.CTkLabel(qr_tab, text="Telegram на телефоне → Настройки → Устройства →\n"
                                  "Подключить устройство, и наведите камеру на код",
                     justify="center").pack(pady=(6, 8))
        self.qr_label = ctk.CTkLabel(qr_tab, text="", width=240, height=240)
        self.qr_label.pack()
        self.qr_btn = ctk.CTkButton(qr_tab, text="Обновить QR-код", command=self.show_qr)
        self.qr_btn.pack(pady=10)

        # --- телефон ---
        ctk.CTkLabel(ph_tab, text="Номер телефона в международном формате").pack(anchor="w", pady=(10, 2))
        self.phone = ctk.CTkEntry(ph_tab, placeholder_text="+7 900 123-45-67")
        self.phone.pack(fill="x")
        self.code_btn = ctk.CTkButton(ph_tab, text="Получить код", command=self.send_code)
        self.code_btn.pack(fill="x", pady=8)
        self.code_frame = ctk.CTkFrame(ph_tab, fg_color="transparent")
        ctk.CTkLabel(self.code_frame, text="Код из Telegram (придёт в чат «Telegram»)").pack(anchor="w")
        self.code = ctk.CTkEntry(self.code_frame, placeholder_text="12345")
        self.code.pack(fill="x")
        self.code.bind("<Return>", lambda e: self.sign_in_code())
        ctk.CTkButton(self.code_frame, text="Войти", command=self.sign_in_code).pack(fill="x", pady=8)
        ctk.CTkLabel(ph_tab, text="Код не приходит? Используйте вход по QR-коду.",
                     text_color="gray").pack(side="bottom", pady=6)

        # --- общий блок пароля 2FA ---
        self.pw_frame = ctk.CTkFrame(self)
        ctk.CTkLabel(self.pw_frame, text="На аккаунте включён облачный пароль (2FA)").pack(pady=(8, 2))
        self.pw = ctk.CTkEntry(self.pw_frame, show="•", placeholder_text="Облачный пароль")
        self.pw.pack(fill="x", padx=12)
        self.pw.bind("<Return>", lambda e: self.sign_in_pw())
        ctk.CTkButton(self.pw_frame, text="Войти", command=self.sign_in_pw).pack(fill="x", padx=12, pady=8)

        self.err = ctk.CTkLabel(self, text="", text_color=RED, wraplength=400)
        self.err.pack(pady=(4, 10))
        self.after(300, self.show_qr)

    # действия
    def show_qr(self):
        self.err.configure(text="")
        self.engine.start_qr()

    def send_code(self):
        if not self.phone.get().strip():
            self.err.configure(text="Введите номер телефона")
            return
        self.err.configure(text="")
        self.code_btn.configure(state="disabled", text="Отправляю…")
        self.engine.send_code(self.phone.get())

    def sign_in_code(self):
        self.err.configure(text="")
        self.engine.sign_in_code(self.code.get())

    def sign_in_pw(self):
        self.err.configure(text="")
        self.engine.sign_in_password(self.pw.get())

    def cancel(self):
        self.engine.cancel_login()
        self.close()

    def close(self):
        self.grab_release()
        self.destroy()
        self.app.login_dialog = None
        self.app.after(300, self.app.next_login)   # следующий аккаунт, которому нужен вход

    # события от движка
    def on_event(self, kind, data):
        if kind == "qr":
            qr = qrcode.QRCode(border=2)
            qr.add_data(data)
            qr.make()
            m = qr.get_matrix()
            img = Image.new("L", (len(m), len(m)), 255)
            img.putdata([0 if v else 255 for row in m for v in row])
            img = img.resize((240, 240), Image.NEAREST)
            self.qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(240, 240))
            self.qr_label.configure(image=self.qr_img)
        elif kind == "code_sent":
            self.code_btn.configure(state="normal", text="Отправить код ещё раз")
            self.code_frame.pack(fill="x", pady=(4, 0))
            self.code.focus()
        elif kind == "password_needed":
            self.pw_frame.pack(fill="x", padx=16, pady=(8, 0), before=self.err)
            self.pw.focus()
        elif kind == "login_error":
            self.code_btn.configure(state="normal", text="Получить код")
            self.err.configure(text=data)


# ======================================================================
#  Блоки настроек нейросети
# ======================================================================

def describe_claude(st):
    if not st.get("loggedIn"):
        return None
    plan = st.get("subscriptionType") or ""
    text = f"Вход в Claude выполнен ✓  {st.get('email', '')}" + (f"  ·  подписка {plan}" if plan else "")
    if st.get("authMethod") not in (None, "claude.ai"):
        return text + "  ·  ⚠ вход не по подписке — запросы могут оплачиваться по API", "#e0a030"
    return text, GREEN


class CliBlock:
    """Строка «установлена ли программа / выполнен ли вход» + кнопка, которая делает недостающее."""

    def __init__(self, app: "App", parent, row, name, account, status, install, login, describe, hint,
                 relogin_hint=None):
        self.app, self.name, self.account = app, name, account
        self.status_fn, self.install_fn, self.login_fn = status, install, login
        self.describe, self.hint, self.relogin_hint = describe, hint, relogin_hint
        self.waiting = 0
        self.seen_logout = False
        self.state = {"installed": False}
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row, column=0, columnspan=3, sticky="ew", padx=14, pady=(4, 4))
        self.lbl = ctk.CTkLabel(f, text=f"{name}: проверяю…", text_color="gray")
        self.lbl.pack(side="left")
        # Почта аккаунта скрыта, пока не нажать «показать»
        self.reveal_email = False
        self.email_btn = ctk.CTkButton(f, text="показать", width=70, height=24, fg_color="transparent",
                                       text_color=("#1f6aa5", "#5aa9e6"), hover=False,
                                       command=self.toggle_email)
        self.btn = ctk.CTkButton(f, text="", width=200, command=self.action)
        self.btn_visible = False
        app.after(200, self.refresh)

    def toggle_email(self):
        self.reveal_email = not self.reveal_email
        self.show(self.state)

    def refresh(self):
        self.app.in_thread(self.status_fn, self.show)

    def show(self, st):
        self.state = st
        if not st["installed"]:
            text, color, btn = f"{self.name} не установлен", RED, "Установить и войти"
        elif not st.get("loggedIn"):
            text, color, btn = f"{self.name} установлен, но вход не выполнен", "#e0a030", f"Войти в {self.account}"
        else:
            (text, color), btn = self.describe(st), "Войти другим аккаунтом"
        email = st.get("email") if st.get("loggedIn") else ""
        if email and not self.reveal_email:
            text = text.replace(email, "••••••••@••••")
        self.lbl.configure(text=text, text_color=color)
        if email:
            self.email_btn.configure(text="скрыть" if self.reveal_email else "показать")
            self.email_btn.pack(side="left", padx=(4, 0), after=self.lbl)
        else:
            self.email_btn.pack_forget()
        logged = st.get("loggedIn")
        self.btn.configure(text=btn, state="normal",
                           fg_color=GRAY if logged else BTN_COLOR,
                           hover_color=GRAY_HOVER if logged else BTN_HOVER)
        if not self.btn_visible:
            self.btn.pack(side="right")
            self.btn_visible = True

    def action(self):
        self.btn.configure(state="disabled")
        if not self.state["installed"]:
            self.lbl.configure(text=f"Скачиваю и устанавливаю {self.name}… (1–5 минут)", text_color="gray")
            log.info("Устанавливаю %s…", self.name)
            self.app.in_thread(self.install_fn, self.after_install)
        else:
            self.start_login()

    def after_install(self, res):
        ok, out = res
        if not ok:
            log.error("Установка %s не удалась:\n%s", self.name, out[-1500:])
            messagebox.showerror(self.name, f"Не получилось установить {self.name}.\n"
                                            "Подробности во вкладке «Журнал».")
            self.refresh()
            return
        log.info("%s установлен", self.name)
        self.start_login()

    def vpn_ok(self) -> bool:
        """Вход идёт из окна самой CLI — подсказку про VPN нужно дать до него, потом будет поздно."""
        sp = core.system_proxy()
        if sp and sp.startswith("http"):
            return True
        where = (f"Системный прокси {sp} — SOCKS, а {self.name} понимает только HTTP-прокси."
                 if sp else "Системный прокси не найден.")
        return messagebox.askokcancel(f"Вход в {self.account}", (
            f"{where}\n\n{self.account} недоступен в некоторых странах (например, в России), и вход оттуда "
            "без VPN не пройдёт — Google, OpenAI и Anthropic проверяют страну.\n\n"
            "Если вы в такой стране — нажмите «Отмена», включите VPN с сервером в США или Европе "
            "(режим TUN или системный HTTP-прокси) и повторите вход.\n\n"
            "Если VPN в режиме TUN уже включён или ваша страна поддерживается — нажмите «ОК»."))

    def start_login(self):
        relogin = bool(self.state.get("loggedIn"))
        if not self.vpn_ok():
            self.refresh()
            return
        try:
            self.login_fn()
        except Exception as e:
            messagebox.showerror(self.name, f"Не удалось открыть вход: {e}")
            self.refresh()
            return
        messagebox.showinfo(f"Вход в {self.account}",
                            self.relogin_hint if relogin and self.relogin_hint else self.hint)
        self.lbl.configure(text="Жду завершения входа… Когда войдёте — нажмите «Готово — проверить»",
                           text_color="gray")
        self.waiting += 1            # номер текущего ожидания: старые циклы проверки сами завершатся
        self.seen_logout = not relogin
        self.btn.configure(text="Готово — проверить", state="normal", command=self.finish_wait,
                           fg_color=BTN_COLOR, hover_color=BTN_HOVER)
        self.wait_login(self.waiting, tries=100)   # ~5 минут

    def finish_wait(self):
        self.waiting += 1            # останавливаем фоновое ожидание
        self.btn.configure(state="disabled", command=self.action)
        self.lbl.configure(text="Проверяю…", text_color="gray")

        def done(st):
            self.show(st)
            if st.get("loggedIn"):
                log.info("Вход в %s выполнен", self.account)
        self.app.in_thread(self.status_fn, done)

    def wait_login(self, wait_id, tries):
        def done(st):
            if wait_id != self.waiting:
                return               # ожидание уже закончено кнопкой или новым входом
            if not st.get("loggedIn"):
                self.seen_logout = True
            elif self.seen_logout or st != self.state:
                self.waiting += 1
                self.btn.configure(command=self.action)
                self.show(st)
                log.info("Вход в %s выполнен", self.account)
                return
            if tries > 0:
                self.app.after(3000, lambda: self.wait_login(wait_id, tries - 1))
            else:
                self.waiting += 1
                self.btn.configure(command=self.action)
                self.show(st)
        self.app.in_thread(self.status_fn, done)


class KeyBlock:
    """Поле API-ключа + «показать» + «Проверить ключ» + ссылка, где взять ключ."""

    def __init__(self, app: "App", parent, key_field, placeholder, link_text, url,
                 model_widget, model_field, check_fn):
        self.app, self.key_field, self.model_field = app, key_field, model_field
        self.model_widget, self.check_fn = model_widget, check_fn
        ctk.CTkLabel(parent, text="API-ключ").grid(row=1, column=0, sticky="w", padx=14, pady=5)
        self.entry = e = ctk.CTkEntry(parent, show="•", placeholder_text=placeholder)
        e.grid(row=1, column=1, sticky="ew", padx=4)
        app.fields[key_field] = e
        kb = ctk.CTkFrame(parent, fg_color="transparent")
        kb.grid(row=1, column=2, sticky="w", padx=6)
        ctk.CTkButton(kb, text="показать", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: e.configure(show="" if e.cget("show") else "•")
                      ).pack(side="left", padx=2)
        self.btn = ctk.CTkButton(kb, text="Проверить ключ", width=130, command=self.check)
        self.btn.pack(side="left", padx=2)
        ctk.CTkButton(parent, text=link_text, fg_color="transparent",
                      text_color=("#1f6aa5", "#5aa9e6"), hover=False, anchor="w",
                      command=lambda: webbrowser.open(url)).grid(row=2, column=1, sticky="w")
        self.lbl = ctk.CTkLabel(parent, text="", text_color="gray")
        self.lbl.grid(row=2, column=2, sticky="w", padx=10)

    def check(self):
        cfg = dict(self.app.cfg, **{self.key_field: self.entry.get().strip(),
                                    self.model_field: self.model_widget.get().strip()})
        self.btn.configure(state="disabled")
        self.lbl.configure(text="Проверяю…", text_color="gray")

        def done(res):
            ok, msg, *models = res
            self.btn.configure(state="normal")
            self.lbl.configure(text=msg, text_color=GREEN if ok else RED)
            if models and models[0]:   # проверка вернула список доступных моделей
                self.model_widget.configure(values=models[0])
        self.app.in_thread(lambda: self.check_fn(cfg), done)


# ======================================================================
#  Главное окно
# ======================================================================

class App(ctk.CTk):
    def __init__(self, hidden=False, run_delay: int | None = None, updated=False,
                 instance: socket.socket | None = None):
        """hidden — сразу в трей; run_delay — через сколько секунд самим начать работу
        (автозапуск с Windows, продолжение после обновления); updated — запуск после обновления."""
        super().__init__()
        if hidden:
            self.withdraw()   # до первой отрисовки — окно не мелькнёт
        icon = core.RES / "assets" / "icon.ico"
        if icon.exists():
            self.iconbitmap(str(icon))
        self.log_q = setup_logging()
        first_run = not core.CONFIG_FILE.exists()
        # Не self.config: это имя метода tkinter
        self.conf = core.load_config()
        self.save()   # заодно переводит старый config.json на профили
        self.engines: dict[str, Engine] = {p["id"]: Engine(p) for p in self.conf["profiles"]}
        self.update_show_names()
        self.login_dialog: LoginDialog | None = None
        self.login_waiting: list[str] = []   # профили, ждущие окна входа (оно одно на всех)
        self.selected: str | None = None     # Pending.uid
        self.shown_comment: dict[str, str] = {}
        self.posted_count = 0
        self.status = "stopped"   # общий статус; нужен сразу — меню трея читает его из своего потока

        self.title("TG Автокомментатор")
        self.geometry("1120x760")
        self.minsize(940, 640)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_header()
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._build_queue_tab(self.tabs.add("Очередь"))
        self._build_settings_tab(self.tabs.add("Настройки"))
        self._build_prompt_tab(self.tabs.add("Промпт"))
        self._build_log_tab(self.tabs.add("Журнал"))
        self.statusbar = ctk.CTkLabel(self, text="", anchor="w", text_color="gray")
        self.statusbar.pack(fill="x", padx=16, pady=(0, 6))

        self.tray_hinted = False
        try:
            self.tray: Tray | None = Tray(self)
        except Exception as e:   # нет pystray или трей недоступен — крестик просто закрывает программу
            self.tray = None
            log.warning("Значок в трее недоступен: %s", e)
            self.tray_sw.configure(state="disabled")
        self.render_status()
        self.render_queue()
        if first_run or core.validate(self.cfg):
            self.tabs.set("Настройки")
            self.welcome.pack(fill="x", padx=4, pady=(0, 10), before=self.settings_first)
        if instance:
            threading.Thread(target=self.listen_instances, args=(instance,), daemon=True).start()
        # Программу перенесли в другую папку или сменили Python — обновляем команду автозапуска
        if core.get_autostart() not in (None, core.autostart_command()):
            try:
                core.set_autostart(True)
            except OSError as e:
                log.warning("Не удалось обновить автозапуск: %s", e)
        if hidden and not self.tray:
            self.deiconify()
        if updated:
            log.info("Программа обновлена до версии %s", core.VERSION)
            if self.tray:
                self.tray.notify(f"Программа обновлена до версии {core.VERSION}")
        if run_delay is not None:
            log.info("Начну работу через %d сек", run_delay)
            self.after(run_delay * 1000, self.auto_run)
        self.update_info: updater.Update | None = None
        self.updating = False
        self.after(15_000, self.check_updates)
        self.after(100, self.poll)

    # ------------------------------------------------------------------ обновления

    UPDATE_EVERY_MS = 6 * 3600 * 1000

    def check_updates(self, manual=False):
        """Фоновая проверка GitHub Releases: при запуске и раз в 6 часов; manual — по кнопке."""
        if not manual:
            self.after(self.UPDATE_EVERY_MS, self.check_updates)
        if self.updating:
            return
        if manual:
            self.update_check_lbl.configure(text="Проверяю…", text_color="gray")

        def work():
            try:
                return updater.check(), None
            except Exception as e:
                return None, e

        def done(res):
            u, err = res
            if err:
                log.warning("Не удалось проверить обновления: %s", err)
                if manual:
                    self.update_check_lbl.configure(text="Не удалось проверить — нет связи с GitHub", text_color=RED)
                return
            if not u:
                if manual:
                    self.update_check_lbl.configure(text="У вас последняя версия ✓", text_color=GREEN)
                return
            if manual:
                self.update_check_lbl.configure(text=f"Доступна версия {u.version}", text_color=GREEN)
            fresh = not self.update_info or self.update_info.version != u.version
            self.update_info = u
            if fresh:
                log.info("Доступна новая версия %s", u.version)
            if core.FROZEN and u.url and self.conf["auto_update"] and not manual and self.can_update_quietly():
                self.start_update()
                return
            self.show_update_bar()
            if fresh and self.tray and self.state() == "withdrawn":
                self.tray.notify(f"Доступна версия {u.version} — откройте окно, чтобы обновить")
        self.in_thread(work, done)

    def can_update_quietly(self) -> bool:
        """Сами ставим обновление, только если ничего не прервём: очередь пуста, промпт сохранён, нет входа."""
        return not self.all_pending() and not self.prompt_dirty() and not self.login_dialog

    def show_update_bar(self):
        u = self.update_info
        can_install = core.FROZEN and u.url
        self.update_lbl.configure(text=f"Доступна новая версия {u.version} (у вас {core.VERSION})")
        self.update_btn.configure(text="Обновить сейчас" if can_install else "Скачать",
                                  state="normal", command=self.start_update if can_install
                                  else lambda: webbrowser.open(u.page))
        self.update_bar.pack(fill="x", padx=12, pady=(0, 4), after=self.header_top)

    def start_update(self):
        u = self.update_info
        if not u or self.updating:
            return
        self.updating = True
        self.show_update_bar()
        self.update_btn.configure(state="disabled")
        self.update_lbl.configure(text=f"Скачиваю версию {u.version}…")
        log.info("Скачиваю обновление %s…", u.version)

        def progress(x):
            self.after(0, lambda: self.update_lbl.configure(text=f"Скачиваю версию {u.version}… {int(x * 100)}%"))

        def work():
            try:
                return updater.download(u, progress), None
            except Exception as e:
                return None, e

        def done(res):
            setup, err = res
            if err:
                self.updating = False
                log.error("Не удалось скачать обновление: %s", err)
                self.update_lbl.configure(text=f"Не удалось скачать обновление: {err}")
                self.update_btn.configure(state="normal", text="Повторить")
                return
            run = any(e.status != "stopped" for e in self.engines.values())
            tray = self.state() == "withdrawn"
            log.info("Устанавливаю версию %s — программа перезапустится", u.version)
            self.shutdown()
            updater.install(setup, run=run, tray=tray)
            self.destroy()
        self.in_thread(work, done)

    def shutdown(self):
        """Останавливает все аккаунты и убирает значок — перед выходом и перед обновлением."""
        stops = [e._submit(e._stop()) for e in self.engines.values()]
        for fut in stops:
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        shutil.rmtree(core.MEDIA_DIR, ignore_errors=True)
        if self.tray:
            self.tray.stop()

    def listen_instances(self, srv: socket.socket):
        """Повторный запуск программы: показываем это окно вместо второй копии."""
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(2)
                    if conn.recv(64) == INSTANCE_HELLO:
                        conn.sendall(INSTANCE_REPLY)
                        self.after(0, self.show_window)
            except OSError:
                continue

    def auto_run(self):
        """Запуск после входа в Windows: без окон с вопросами — проблемы пишем в журнал."""
        started, skipped = [], []
        for p in self.conf["profiles"]:
            e = self.engines[p["id"]]
            if not p["enabled"] or e.status != "stopped":
                continue
            problems = core.validate(p)
            if problems:
                skipped.append(p["name"])
                log.warning("Автозапуск: «%s» не запущен — %s", p["name"], "; ".join(problems))
                continue
            e.start()
            started.append(p["name"])
        if started:
            log.info("Автозапуск: работаю — %s", ", ".join(started))
        if skipped and self.tray:
            self.tray.notify(f"Не запущены: {', '.join(skipped)} — подробности во вкладке «Журнал»")

    # ------------------------------------------------------------------ профили

    @property
    def cfg(self) -> dict:
        """Настройки профиля, открытого в окне (тот же словарь, что у его движка)."""
        return self.profile(self.conf["current"])

    @property
    def engine(self) -> Engine:
        return self.engines[self.conf["current"]]

    def profile(self, pid: str) -> dict:
        return next(p for p in self.conf["profiles"] if p["id"] == pid)

    def save(self):
        core.save_config(self.conf)

    def update_show_names(self):
        for e in self.engines.values():
            e.show_name = len(self.engines) > 1

    def _build_profile_bar(self):
        pb = ctk.CTkFrame(self)
        pb.pack(fill="x", padx=12, pady=(4, 0))
        ctk.CTkLabel(pb, text="Аккаунт:").pack(side="left", padx=(12, 6), pady=8)
        self.profile_menu = ctk.CTkOptionMenu(pb, values=["—"], width=200, command=self.on_profile_menu)
        self.profile_menu.pack(side="left")
        self.profile_state = ctk.CTkLabel(pb, text="", text_color="gray")
        self.profile_state.pack(side="left", padx=12)
        self.del_btn = ctk.CTkButton(pb, text="Удалить", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                                     command=self.delete_profile)
        self.del_btn.pack(side="right", padx=(4, 10))
        ctk.CTkButton(pb, text="Переименовать", width=120, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.rename_profile).pack(side="right", padx=4)
        ctk.CTkButton(pb, text="+ Добавить аккаунт", width=150,
                      command=self.add_profile).pack(side="right", padx=4)
        # Только при нескольких профилях
        self.profile_run_btn = ctk.CTkButton(pb, text="", width=150, command=self.toggle_profile)
        self.enabled_sw = ctk.CTkSwitch(pb, text="Запускать со всеми", command=self.on_enabled)

    def render_profile_bar(self):
        names = [p["name"] for p in self.conf["profiles"]]
        self.profile_menu.configure(values=names)
        self.profile_menu.set(self.cfg["name"])
        e = self.engine
        text, color = STATUS[e.status]
        if e.status == "running" and e.me_name:
            text += f" · {e.me_name}"
        self.profile_state.configure(text=f"● {text}", text_color=color)
        multi = len(names) > 1
        self.del_btn.configure(state="normal" if multi else "disabled")
        if multi:
            active = e.status != "stopped"
            self.profile_run_btn.configure(
                text="■  Остановить этот" if active else "▶  Запустить только этот",
                fg_color=RED if active else GREEN, hover_color="#b94541" if active else GREEN_HOVER)
            self.enabled_sw.select() if self.cfg["enabled"] else self.enabled_sw.deselect()
            self.profile_run_btn.pack(side="left", padx=(0, 8), after=self.profile_state)
            self.enabled_sw.pack(side="left", after=self.profile_run_btn)
        else:
            self.profile_run_btn.pack_forget()
            self.enabled_sw.pack_forget()

    def leave_profile(self) -> bool:
        """Перед переключением: сохраняем форму настроек и спрашиваем про промпт."""
        if not self.apply_settings(silent=True):
            return False
        if self.prompt_dirty() and messagebox.askyesno(
                "Промпт", f"Промпт профиля «{self.cfg['name']}» изменён, но не сохранён. Сохранить?"):
            self.save_prompt()
        return True

    def on_profile_menu(self, name):
        pid = next(p["id"] for p in self.conf["profiles"] if p["name"] == name)
        if pid == self.conf["current"]:
            return
        if not self.leave_profile():
            self.profile_menu.set(self.cfg["name"])
            return
        self.switch_profile(pid)

    def switch_profile(self, pid):
        self.conf["current"] = pid
        self.save()
        self.load_settings_form()
        self.load_prompt()
        self.load_header_switches()
        self.update_channel_menu()
        self.render_status()
        self.render_queue()

    def ask_name(self, title, text, initial="") -> str | None:
        d = ctk.CTkInputDialog(title=title, text=text)
        if initial:
            d.after(150, lambda: d._entry.insert(0, initial))
        name = (d.get_input() or "").strip()
        if not name:
            return None
        if any(p["name"] == name for p in self.conf["profiles"]):
            messagebox.showwarning(title, f"Профиль «{name}» уже есть — выберите другое название")
            return None
        return name

    def add_profile(self):
        if not self.leave_profile():
            return
        name = self.ask_name("Новый аккаунт", "Название профиля (например, имя аккаунта):")
        if not name:
            return
        base = self.cfg
        p = core.new_profile(self.conf, base, name)
        self.engines[p["id"]] = Engine(p)
        self.update_show_names()
        self.switch_profile(p["id"])
        log.info("Добавлен профиль «%s»", name)
        messagebox.showinfo("Новый аккаунт", (
            f"Настройки и промпт скопированы из «{base['name']}» — поменяйте, что нужно, "
            "во вкладках «Настройки» и «Промпт».\n\n"
            "Затем нажмите «▶ Запустить только этот» (или общий «▶ Запустить») — откроется вход "
            "в Telegram для нового аккаунта."))

    def rename_profile(self):
        name = self.ask_name("Переименовать", "Новое название профиля:", self.cfg["name"])
        if not name:
            return
        self.cfg["name"] = name
        self.save()
        self.render_profile_bar()
        self.render_queue()

    def delete_profile(self):
        if len(self.engines) < 2:
            return
        p, e = self.cfg, self.engine
        waiting = len(e.pending)
        if not messagebox.askyesno("Удалить профиль", f"Удалить профиль «{p['name']}»?"
                                   + (f"\n\nВ очереди от этого аккаунта: {waiting} — они пропадут." if waiting else "")):
            return
        try:
            e._submit(e._stop()).result(timeout=5)
        except Exception:
            pass
        session = BASE / f"{p['session_name']}.session"
        if session.exists() and messagebox.askyesno("Файл сессии", (
                f"Удалить и файл сессии {session.name}?\n\n"
                "Он даёт полный доступ к аккаунту Telegram. Если аккаунт больше не нужен — лучше удалить; "
                "чтобы вернуть его, придётся снова войти.")):
            for f in (session, session.with_name(session.name + "-journal")):
                f.unlink(missing_ok=True)
        core.done_file(p).unlink(missing_ok=True)
        if self.login_dialog and self.login_dialog.engine is e:
            self.login_dialog.close()
        self.login_waiting = [x for x in self.login_waiting if x != p["id"]]
        self.conf["profiles"].remove(p)
        del self.engines[p["id"]]
        self.update_show_names()
        log.info("Профиль «%s» удалён", p["name"])
        self.switch_profile(self.conf["profiles"][0]["id"])

    def on_enabled(self):
        self.cfg["enabled"] = bool(self.enabled_sw.get())
        self.save()

    def toggle_profile(self):
        """Запуск/остановка только открытого профиля — не трогая остальные."""
        e = self.engine
        if e.status != "stopped":
            e.stop()
            return
        if not self.prepare_start():
            return
        problems = core.validate(self.cfg)
        if problems:
            messagebox.showwarning("Не всё настроено", "Перед запуском:\n\n• " + "\n• ".join(problems))
            self.show_problem_tab(problems)
            return
        e.start()

    def next_login(self):
        """Окно входа одно — аккаунты, которым нужен вход, проходят его по очереди."""
        while self.login_waiting and not self.login_dialog:
            e = self.engines.get(self.login_waiting.pop(0))
            if e and e.status == "login":
                self.login_dialog = LoginDialog(self, e)

    # ------------------------------------------------------------------ шапка

    def _build_header(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 4))
        self.header_top = top
        ctk.CTkLabel(top, text="TG Автокомментатор",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkLabel(top, text=f"v{core.VERSION}", text_color="gray").pack(side="left", padx=(6, 0), pady=(6, 0))

        # Полоса «доступна новая версия» — показывается под заголовком, когда есть обновление
        self.update_bar = ctk.CTkFrame(self, fg_color=("#e3f0ff", "#1d2f44"))
        self.update_lbl = ctk.CTkLabel(self.update_bar, text="")
        self.update_lbl.pack(side="left", padx=12, pady=6)
        ctk.CTkButton(self.update_bar, text="Позже", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.update_bar.pack_forget).pack(side="right", padx=(4, 10))
        ctk.CTkButton(self.update_bar, text="Что нового", width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: webbrowser.open(self.update_info.page)).pack(side="right", padx=4)
        self.update_btn = ctk.CTkButton(self.update_bar, text="Обновить сейчас", width=150, fg_color=GREEN,
                                        hover_color=GREEN_HOVER)
        self.update_btn.pack(side="right", padx=4)
        self.status_dot = ctk.CTkLabel(top, text="●", font=ctk.CTkFont(size=18))
        self.status_dot.pack(side="left", padx=(18, 4))
        self.status_lbl = ctk.CTkLabel(top, text="")
        self.status_lbl.pack(side="left")
        self.start_btn = ctk.CTkButton(top, text="", width=150, height=36,
                                       font=ctk.CTkFont(size=14, weight="bold"),
                                       command=self.toggle_run)
        self.start_btn.pack(side="right")
        self.counter_lbl = ctk.CTkLabel(top, text="", text_color="gray")
        self.counter_lbl.pack(side="right", padx=14)

        self._build_profile_bar()
        bar = ctk.CTkFrame(self)
        bar.pack(fill="x", padx=12, pady=(4, 6))
        ctk.CTkLabel(bar, text="Режим:").pack(side="left", padx=(12, 6), pady=8)
        self.mode = ctk.CTkSegmentedButton(bar, values=[MODE_CONFIRM, MODE_AUTO],
                                           command=self.on_mode)
        self.mode.pack(side="left")
        self.see_sw = ctk.CTkSwitch(bar, text="Нейронка смотрит картинки поста",
                                    command=self.on_see_images)
        self.see_sw.pack(side="left", padx=20)
        self.latest_btn = ctk.CTkButton(bar, text="Взять последний пост канала", width=200,
                                        fg_color=GRAY, hover_color=GRAY_HOVER,
                                        command=self.take_latest)
        self.latest_btn.pack(side="right", padx=10)
        # Из какого канала брать последний пост (виден, только если каналов несколько)
        self.latest_chan = ctk.CTkOptionMenu(bar, values=["—"], width=170)
        self.load_header_switches()
        self.update_channel_menu()

    def load_header_switches(self):
        """Режим и «смотрит картинки» — настройки открытого профиля."""
        self.mode.set(MODE_CONFIRM if self.cfg["confirm_before_post"] else MODE_AUTO)
        self.see_sw.select() if self.cfg["send_post_images"] else self.see_sw.deselect()

    def render_status(self):
        """Общий статус по всем аккаунтам: самый «требующий внимания» из их статусов."""
        engines = list(self.engines.values())
        states = {e.status for e in engines}
        st = next((s for s in ("login", "connecting", "running") if s in states), "stopped")
        text, color = STATUS[st]
        running = [e for e in engines if e.status == "running"]
        if st == "running" and len(engines) > 1:
            text = f"Работает · аккаунтов: {len(running)} из {len(engines)}"
        elif st == "running" and running[0].me_name:
            chans = core.channels(self.cfg)
            where = f"@{chans[0]}" if len(chans) == 1 else f"каналов: {len(chans)}"
            text = f"Работает · {running[0].me_name} · {where}"
        self.status_dot.configure(text_color=color)
        self.status_lbl.configure(text=text)
        active = st != "stopped"
        self.start_btn.configure(
            text="■  Остановить" if active else "▶  Запустить",
            fg_color=RED if active else GREEN,
            hover_color="#b94541" if active else GREEN_HOVER)
        here = "normal" if self.engine.status == "running" else "disabled"
        self.latest_btn.configure(state=here)
        self.latest_chan.configure(state=here)
        self.status = st
        self.render_profile_bar()
        self.update_tray()

    def update_tray(self):
        if not getattr(self, "tray", None):
            return
        n = len(self.all_pending())
        tip = f"TG Автокомментатор — {self.status_lbl.cget('text')}" + (f" · в очереди: {n}" if n else "")
        self.tray.update(self.status, tip)

    def prepare_start(self) -> bool:
        if not self.apply_settings(silent=True):
            return False
        if self.prompt_dirty() and messagebox.askyesno(
                "Промпт", "Промпт изменён, но не сохранён. Сохранить перед запуском?"):
            self.save_prompt()
        return True

    def show_problem_tab(self, problems):
        self.tabs.set("Промпт" if any("Промпт" in p for p in problems) and len(problems) == 1
                      else "Настройки")

    def toggle_run(self):
        """Общая кнопка: запускает все профили с «Запускать со всеми» или останавливает все."""
        if self.status != "stopped":
            for e in self.engines.values():
                if e.status != "stopped":
                    e.stop()
            return
        if not self.prepare_start():
            return
        targets = [p for p in self.conf["profiles"] if p["enabled"]]
        if not targets:
            messagebox.showwarning("Запуск", "Ни у одного аккаунта не включено «Запускать со всеми»")
            return
        multi = len(self.conf["profiles"]) > 1
        for p in targets:
            problems = core.validate(p)
            if problems:
                if p["id"] != self.conf["current"]:
                    self.switch_profile(p["id"])
                where = f" аккаунта «{p['name']}»" if multi else ""
                messagebox.showwarning("Не всё настроено",
                                       f"Перед запуском{where}:\n\n• " + "\n• ".join(problems))
                self.show_problem_tab(problems)
                return
        for p in targets:
            self.engines[p["id"]].start()

    def on_mode(self, value):
        self.cfg["confirm_before_post"] = value == MODE_CONFIRM
        self.save()
        log.info("Режим%s: %s", f" «{self.cfg['name']}»" if len(self.engines) > 1 else "", value.lower())
        self.render_queue()

    def on_see_images(self):
        self.cfg["send_post_images"] = bool(self.see_sw.get())
        self.save()

    def take_latest(self):
        self.tabs.set("Очередь")
        chans = core.channels(self.cfg)
        self.engine.take_latest_post(self.latest_chan.get() if len(chans) > 1 else None)

    def update_channel_menu(self):
        chans = ["@" + c for c in core.channels(self.cfg)]
        if len(chans) > 1:
            self.latest_chan.configure(values=chans)
            if self.latest_chan.get() not in chans:
                self.latest_chan.set(chans[0])
            self.latest_chan.pack(side="right", before=self.latest_btn)
            self.latest_btn.configure(text="Взять последний пост из")
        else:
            self.latest_chan.pack_forget()
            self.latest_btn.configure(text="Взять последний пост канала")

    # ------------------------------------------------------------------ очередь

    def _build_queue_tab(self, tab):
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        self.list_frame = ctk.CTkScrollableFrame(tab, width=260, label_text="Ждут решения")
        self.list_frame.grid(row=0, column=0, sticky="ns", padx=(0, 10))

        self.empty = ctk.CTkLabel(tab, text="", font=ctk.CTkFont(size=15),
                                  text_color="gray", justify="center")
        self.detail = ctk.CTkFrame(tab, fg_color="transparent")
        d = self.detail
        d.grid_columnconfigure(0, weight=1)

        self.post_title = ctk.CTkLabel(d, text="", font=ctk.CTkFont(size=14, weight="bold"), anchor="w")
        self.post_title.grid(row=0, column=0, columnspan=2, sticky="w")
        self.post_box = ctk.CTkTextbox(d, height=110, wrap="word")
        self.post_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 10))

        ctk.CTkLabel(d, text="Комментарий (можно править)", anchor="w",
                     font=ctk.CTkFont(weight="bold")).grid(row=2, column=0, sticky="w")
        self.comment_box = ctk.CTkTextbox(d, wrap="word", font=ctk.CTkFont(size=14))
        self.comment_box.grid(row=3, column=0, sticky="nsew", pady=(2, 4))
        d.grid_rowconfigure(3, weight=1)

        img_col = ctk.CTkFrame(d)
        img_col.grid(row=2, column=1, rowspan=2, sticky="ns", padx=(10, 0))
        ctk.CTkLabel(img_col, text="Картинка к комментарию",
                     font=ctk.CTkFont(weight="bold")).pack(pady=(8, 4), padx=10)
        self.img_preview = ctk.CTkLabel(img_col, text="", width=220, height=220,
                                        fg_color=("gray85", "gray20"), corner_radius=8)
        self.img_preview.pack(padx=10)
        self.img_name = ctk.CTkLabel(img_col, text="", text_color="gray", wraplength=220)
        self.img_name.pack(pady=2)
        row = ctk.CTkFrame(img_col, fg_color="transparent")
        row.pack(pady=(2, 10), padx=10)
        self.img_btns = [
            ctk.CTkButton(row, text="Другая", width=70, command=self.next_image),
            ctk.CTkButton(row, text="Файл…", width=70, command=self.choose_image),
            ctk.CTkButton(row, text="Без", width=60, fg_color=GRAY,
                          hover_color=GRAY_HOVER, command=self.no_image),
        ]
        for b in self.img_btns:
            b.pack(side="left", padx=2)

        self.progress = ctk.CTkProgressBar(d, mode="indeterminate", height=6)
        self.err_lbl = ctk.CTkLabel(d, text="", text_color=RED, anchor="w", justify="left",
                                    wraplength=640)
        self.err_lbl.grid(row=5, column=0, columnspan=2, sticky="w")

        wish = ctk.CTkFrame(d, fg_color="transparent")
        wish.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        wish.grid_columnconfigure(0, weight=1)
        self.wish = ctk.CTkEntry(wish, placeholder_text="Пожелание к новому варианту: покороче, "
                                                        "разгон 4, стиль древнерусский… (необязательно)")
        self.wish.grid(row=0, column=0, sticky="ew")
        self.wish.bind("<Return>", lambda e: self.regenerate())
        self.regen_btn = ctk.CTkButton(wish, text="↻  Новый вариант", width=150, command=self.regenerate)
        self.regen_btn.grid(row=0, column=1, padx=(8, 0))

        actions = ctk.CTkFrame(d, fg_color="transparent")
        actions.grid(row=7, column=0, columnspan=2, sticky="ew")
        self.publish_btn = ctk.CTkButton(actions, text="✓  Опубликовать", height=42,
                                         font=ctk.CTkFont(size=15, weight="bold"),
                                         fg_color=GREEN, hover_color=GREEN_HOVER, command=self.publish)
        self.publish_btn.pack(side="left", fill="x", expand=True)
        self.skip_btn = ctk.CTkButton(actions, text="Пропустить пост", height=42, width=170,
                                      fg_color=GRAY, hover_color=GRAY_HOVER, command=self.skip)
        self.skip_btn.pack(side="left", padx=(8, 0))

    def all_pending(self) -> dict[str, Pending]:
        """Очередь всех аккаунтов вместе, по Pending.uid."""
        return {p.uid: p for e in list(self.engines.values()) for p in list(e.pending.values())}

    def current(self) -> Pending | None:
        return self.all_pending().get(self.selected) if self.selected else None

    def owner(self, p: Pending) -> Engine:
        return self.engines[p.profile]

    def save_draft(self):
        p = self.current()
        if p and not p.busy:
            p.comment = self.comment_box.get("1.0", "end-1c")

    def select(self, key):
        self.save_draft()
        self.selected = key
        self.render_queue()

    def render_queue(self):
        pending = self.all_pending()
        if self.selected not in pending:
            self.selected = next(iter(pending), None)

        multi = len(self.engines) > 1
        for w in self.list_frame.winfo_children():
            w.destroy()
        for key, p in pending.items():
            state = "⏳ генерирую…" if p.busy else ("⚠ ошибка" if p.error else "✓ готов")
            snippet = (p.post_text or "(только медиа)").replace("\n", " ")[:60]
            if (multi or len(core.channels(self.owner(p).cfg)) > 1) and p.channel:
                snippet = f"{p.channel} · {snippet}"
            who = f"{self.owner(p).cfg['name']} · " if multi else ""
            ctk.CTkButton(
                self.list_frame, text=f"{who}#{p.post_id}  {state}\n{snippet}", anchor="w",
                height=56, corner_radius=8,
                fg_color=BTN_COLOR if key == self.selected else ("gray80", "gray25"),
                text_color=("white", "white") if key == self.selected else ("gray10", "gray90"),
                command=lambda k=key: self.select(k),
            ).pack(fill="x", pady=3)
            # перенос длинного текста внутри кнопки
            btns = self.list_frame.winfo_children()
            btns[-1]._text_label.configure(wraplength=220, justify="left")

        n = len(pending)
        self.title(f"({n}) TG Автокомментатор" if n else "TG Автокомментатор")
        self.update_tray()

        p = self.current()
        if not p:
            self.detail.grid_forget()
            if self.status == "stopped":
                msg = "Нажмите «▶ Запустить», чтобы начать следить за каналом."
            elif self.cfg["confirm_before_post"]:
                msg = ("Новых постов пока нет.\n\nКогда в канале выйдет пост, здесь появится "
                       "готовый комментарий —\nего можно поправить, перегенерировать или опубликовать.\n\n"
                       "Хотите проверить промпт? Нажмите «Взять последний пост канала».")
            else:
                msg = ("Автоматический режим: комментарии публикуются сами.\n"
                       "Что происходит — во вкладке «Журнал».")
            self.empty.configure(text=msg)
            self.empty.grid(row=0, column=1, sticky="nsew")
            return
        self.empty.grid_forget()
        self.detail.grid(row=0, column=1, sticky="nsew")
        self.show_detail(p)

    def show_detail(self, p: Pending):
        e = self.owner(p)
        who = f"  ·  от имени «{e.cfg['name']}»" + (f" ({e.me_name})" if e.me_name else "")
        self.post_title.configure(text=f"Пост #{p.post_id}" + (f"  ·  {p.channel}" if p.channel else "")
                                       + (who if len(self.engines) > 1 else "")
                                       + (f"  ·  нейронка видит {len(p.post_images)} карт." if p.post_images else ""))
        self.post_box.configure(state="normal")
        self.post_box.delete("1.0", "end")
        self.post_box.insert("1.0", p.post_text or "(текста нет, только медиа)")
        self.post_box.configure(state="disabled")

        # Не затираем ручные правки, если текст от модели не менялся
        if self.shown_comment.get(p.uid) != p.comment or self.comment_box.get("1.0", "end-1c") == "":
            self.comment_box.configure(state="normal")
            self.comment_box.delete("1.0", "end")
            self.comment_box.insert("1.0", p.comment)
            self.shown_comment[p.uid] = p.comment
        self.show_image(p)

        if p.busy:
            self.progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=4)
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_forget()
        if p.busy:
            name = core.BACKEND_NAMES.get(self.owner(p).cfg.get("backend"), "Нейросеть")
            self.err_lbl.configure(text=f"⏳ {name} пишет комментарий… (обычно 10–40 секунд)",
                                   text_color="gray")
        else:
            self.err_lbl.configure(text=p.error, text_color=RED)
        st = "disabled" if p.busy else "normal"
        for b in (self.publish_btn, self.skip_btn, self.regen_btn, *self.img_btns):
            b.configure(state=st)
        self.comment_box.configure(state=st)
        if not p.comment and not p.busy:
            self.publish_btn.configure(state="disabled")

    def show_image(self, p: Pending):
        img = thumbnail(p.image, 220)
        self._img_ref = img
        if img:
            self.img_preview.configure(image=img, text="")
        else:
            self.img_preview.configure(image=None,
                                       text="видео" if p.image else "без картинки")
        self.img_name.configure(text=p.image.name if p.image else "")

    def next_image(self):
        if p := self.current():
            p.image = self.owner(p).pick_image(force=True)
            if not p.image:
                messagebox.showinfo("Картинки", "В папке картинок пусто. Укажите папку в «Настройках».")
            self.show_image(p)

    def choose_image(self):
        if p := self.current():
            f = filedialog.askopenfilename(
                title="Картинка к комментарию", initialdir=core.images_path(self.owner(p).cfg),
                filetypes=[("Картинки и видео", " ".join("*" + e for e in core.IMAGE_EXT))])
            if f:
                p.image = Path(f)
                self.show_image(p)

    def no_image(self):
        if p := self.current():
            p.image = None
            self.show_image(p)

    def regenerate(self):
        if p := self.current():
            self.owner(p).regenerate(p.key, self.wish.get())
            self.wish.delete(0, "end")

    def publish(self):
        if p := self.current():
            text = self.comment_box.get("1.0", "end-1c").strip()
            if not text:
                messagebox.showwarning("Пусто", "Комментарий пустой")
                return
            self.owner(p).publish(p.key, text, p.image)

    def skip(self):
        if p := self.current():
            self.owner(p).skip(p.key)

    # ------------------------------------------------------------------ настройки

    def _build_settings_tab(self, tab):
        sf = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        sf.pack(fill="both", expand=True)
        self.fields: dict[str, ctk.CTkEntry] = {}

        self.welcome = ctk.CTkFrame(sf, fg_color=("#e3f0ff", "#1d2f44"))
        ctk.CTkLabel(self.welcome, justify="left", anchor="w", wraplength=900, text=(
            "👋  Добро пожаловать! Для начала работы:\n"
            "0. В блоке «Нейросеть» выберите способ: Claude Code, Antigravity или ChatGPT (вход в аккаунт, "
            "кнопка «Установить и войти»), API-ключ Claude/Gemini/OpenAI или свою модель (LM Studio, Ollama)\n"
            "1. Получите API ID и API Hash на my.telegram.org (раздел «API development tools»)\n"
            "2. Укажите канал, под постами которого нужно комментировать\n"
            "3. Заполните вкладку «Промпт» — инструкцию для нейросети\n"
            "4. Нажмите «Сохранить», затем «▶ Запустить» — программа предложит войти в Telegram\n"
            "Нужно несколько аккаунтов? «+ Добавить аккаунт» вверху — у каждого свои настройки и промпт"
        )).pack(padx=14, pady=10, anchor="w")

        def section(title, hint=None):
            f = ctk.CTkFrame(sf)
            f.pack(fill="x", padx=4, pady=6)
            f.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=15, weight="bold")).grid(
                row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 0))
            if hint:
                ctk.CTkLabel(f, text=hint, text_color="gray", justify="left").grid(
                    row=1, column=0, columnspan=3, sticky="w", padx=14)
            return f

        def field(f, row, key, label, hint="", width=None, **kw):
            ctk.CTkLabel(f, text=label).grid(row=row, column=0, sticky="w", padx=14, pady=5)
            e = ctk.CTkEntry(f, width=width or 300, **kw)
            e.grid(row=row, column=1, sticky="w" if width else "ew", padx=4, pady=5)
            if hint:
                ctk.CTkLabel(f, text=hint, text_color="gray").grid(row=row, column=2, sticky="w", padx=10)
            self.fields[key] = e
            return e

        tg = section("Telegram")
        self.settings_first = tg
        field(tg, 2, "api_id", "API ID", width=200)
        hash_e = field(tg, 3, "api_hash", "API Hash", show="•")
        ctk.CTkButton(tg, text="показать", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: hash_e.configure(show="" if hash_e.cget("show") else "•")
                      ).grid(row=3, column=2, sticky="w", padx=10)
        ctk.CTkButton(tg, text="Где взять API ID и Hash → my.telegram.org", fg_color="transparent",
                      text_color=("#1f6aa5", "#5aa9e6"), hover=False, anchor="w",
                      command=lambda: webbrowser.open("https://my.telegram.org/apps")
                      ).grid(row=4, column=1, sticky="w")
        ctk.CTkLabel(tg, text="Каналы").grid(row=5, column=0, sticky="nw", padx=14, pady=8)
        self.channels_box = ctk.CTkTextbox(tg, height=86)
        self.channels_box.grid(row=5, column=1, sticky="ew", padx=4, pady=5)
        ctk.CTkLabel(tg, text="по одному на строку:\nusername, @username\nили ссылка t.me/…",
                     text_color="gray", justify="left").grid(row=5, column=2, sticky="nw", padx=10, pady=5)
        field(tg, 6, "session_name", "Имя файла сессии", "менять не нужно", width=200)
        ctk.CTkLabel(tg, text="Прокси").grid(row=7, column=0, sticky="w", padx=14, pady=5)
        self.proxy_mode = ctk.CTkSegmentedButton(tg, values=list(PROXY_MODES),
                                                 command=lambda v: self.show_proxy())
        self.proxy_mode.grid(row=7, column=1, sticky="w", padx=4)
        self.proxy_entry = ctk.CTkEntry(tg, placeholder_text="socks5://127.0.0.1:10808, http://… "
                                                             "или ссылка t.me/proxy?…")
        self.fields["proxy"] = self.proxy_entry
        self.proxy_hint = ctk.CTkLabel(tg, text="", text_color="gray", justify="left", wraplength=640)
        self.proxy_hint.grid(row=9, column=1, columnspan=2, sticky="w", padx=4, pady=(0, 10))

        ai = section("Нейросеть")
        ai.grid_columnconfigure(0, minsize=LABEL_W)
        ctk.CTkLabel(ai, text="Как подключаться").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        self.backend = ctk.CTkSegmentedButton(ai, values=list(BACKENDS),
                                              command=lambda v: self.show_backend())
        self.backend.grid(row=2, column=1, columnspan=2, sticky="w", padx=4)

        def sub(hint):
            f = ctk.CTkFrame(ai, fg_color="transparent")
            f.grid_columnconfigure(1, weight=1)
            f.grid_columnconfigure(0, minsize=LABEL_W)
            ctk.CTkLabel(f, text=hint, text_color="gray", justify="left", wraplength=900).grid(
                row=0, column=0, columnspan=3, sticky="w", padx=14)
            return f

        def model_row(f, row, widget, hint):
            ctk.CTkLabel(f, text="Модель").grid(row=row, column=0, sticky="w", padx=14, pady=5)
            widget.grid(row=row, column=1, sticky="w", padx=4)
            lbl = ctk.CTkLabel(f, text=hint, text_color="gray")
            lbl.grid(row=row, column=2, sticky="w", padx=10)
            return lbl

        # --- Claude Code по подписке ---
        cc = sub("Работает через программу Claude Code и вашу подписку Pro/Max — "
                 "за запросы отдельно не платите.")
        self.model = ctk.CTkSegmentedButton(cc, values=core.MODELS)
        model_row(cc, 1, self.model, "sonnet — оптимально, opus — умнее и медленнее, haiku — быстрее")
        self.claude_block = CliBlock(self, cc, row=2, name="Claude Code", account="Claude",
                                     status=core.claude_status, install=core.install_claude,
                                     login=core.open_claude_login, describe=describe_claude, hint=(
            "Откроется браузер (или окно с ссылкой) — войдите в аккаунт Claude с подпиской "
            "Pro/Max и нажмите «Authorize».\n\n"
            "Если в чёрном окне попросят вставить код — скопируйте его со страницы в браузере, "
            "вставьте туда и нажмите Enter.\n\nПрограмма сама заметит, когда вход завершится."))

        # --- Anthropic API по ключу ---
        api = sub("Запросы идут напрямую в API Anthropic и оплачиваются с баланса "
                  "по токенам. Claude Code и подписка не нужны.")
        self.api_model = ctk.CTkOptionMenu(api, values=core.API_MODELS, width=220,
                                           command=lambda v: self.update_price())
        KeyBlock(self, api, "api_key", "sk-ant-…", "Где взять ключ → console.anthropic.com",
                 "https://console.anthropic.com/settings/keys", self.api_model, "api_model",
                 core.check_api_key)
        self.price_lbl = model_row(api, 3, self.api_model, "")

        # --- Antigravity CLI через Google-аккаунт (преемник Gemini CLI) ---
        gc = sub("Работает через Antigravity CLI — новую программу Google вместо Gemini CLI — и ваш "
                 "Google-аккаунт; ключ не нужен. Модели Gemini; лимиты зависят от аккаунта "
                 "(бесплатный, AI Pro или Ultra).")
        self.gemini_model = ctk.CTkComboBox(gc, values=[DEFAULT_MODEL], width=260)
        model_row(gc, 1, self.gemini_model, "список моделей появится после входа")
        self.gemini_block = CliBlock(self, gc, row=2, name="Antigravity CLI", account="Google",
                                     status=core.agy_status, install=core.install_agy,
                                     login=core.open_agy_login, describe=self.describe_agy, hint=(
            "Откроется окно Antigravity CLI и браузер — войдите в Google-аккаунт и разрешите доступ.\n\n"
            "Если браузер не открылся — скопируйте ссылку из чёрного окна в браузер, а полученный "
            "код вставьте обратно в окно и нажмите Enter.\n\n"
            "Когда вход завершится, окно Antigravity можно закрыть и нажать «Готово — проверить»."),
            relogin_hint=(
            "Откроется окно Antigravity — в нём ещё старый аккаунт.\n\n"
            "1. Введите /logout и нажмите Enter.\n"
            "2. Войдите в нужный Google-аккаунт (Antigravity сам предложит вход или откроет браузер).\n"
            "3. Закройте окно и нажмите в программе «Готово — проверить».\n\n"
            "Какой аккаунт сейчас активен, видно в окне Antigravity по команде /usage."))

        # --- Gemini API по ключу ---
        ga = sub("Запросы идут в Gemini API по ключу из Google AI Studio. У ключа есть бесплатный "
                 "лимит, сверх него — оплата по токенам. Gemini CLI не нужен.")
        self.gemini_api_model = ctk.CTkComboBox(ga, values=[core.GEMINI_API_DEFAULT], width=260)
        KeyBlock(self, ga, "gemini_api_key", "AIza…", "Где взять ключ → aistudio.google.com",
                 "https://aistudio.google.com/apikey", self.gemini_api_model, "gemini_api_model",
                 core.check_gemini_key)
        model_row(ga, 3, self.gemini_api_model, "список моделей подгрузится после «Проверить ключ»")

        # --- ChatGPT через Codex CLI по подписке ---
        cx = sub("Работает через Codex CLI — программу OpenAI — и ваш аккаунт ChatGPT с любым тарифом, "
                 "включая бесплатный; ключ не нужен. Расходует лимиты Codex из тарифа: на Free и Go они "
                 "маленькие, для постоянной работы лучше Plus и выше.")
        self.codex_model = ctk.CTkComboBox(cx, values=[DEFAULT_MODEL], width=260)
        model_row(cx, 1, self.codex_model, "список моделей появится после входа")
        self.codex_block = CliBlock(self, cx, row=2, name="Codex CLI", account="ChatGPT",
                                    status=core.codex_status, install=core.install_codex,
                                    login=core.open_codex_login, describe=self.describe_codex, hint=(
            "Откроется браузер — войдите в аккаунт ChatGPT и разрешите доступ для Codex.\n\n"
            "Если браузер не открылся — скопируйте ссылку из чёрного окна в браузер.\n\n"
            "Когда вход завершится, нажмите в программе «Готово — проверить»."),
            relogin_hint=(
            "Программа вышла из текущего аккаунта ChatGPT и открыла вход заново.\n\n"
            "Войдите в браузере в нужный аккаунт, затем нажмите «Готово — проверить»."))

        # --- OpenAI API по ключу ---
        oa = sub("Запросы идут напрямую в OpenAI API и оплачиваются с баланса по токенам "
                 "(отдельно от подписки ChatGPT). Codex не нужен.")
        self.openai_api_model = ctk.CTkComboBox(oa, values=[core.OPENAI_API_DEFAULT], width=260)
        KeyBlock(self, oa, "openai_api_key", "sk-…", "Где взять ключ → platform.openai.com",
                 "https://platform.openai.com/api-keys", self.openai_api_model, "openai_api_model",
                 core.check_openai_key)
        model_row(oa, 3, self.openai_api_model, "список моделей подгрузится после «Проверить ключ»")

        # --- своя модель: любой OpenAI-совместимый сервер ---
        lm = sub("Своя нейросеть на OpenAI-совместимом сервере: локально — LM Studio, Ollama, llama.cpp, vLLM "
                 "(бесплатно, без интернета и VPN), или облачные OpenRouter, DeepSeek и т. п. Наш промпт "
                 "большой — у модели должен быть контекст от 16–32 тыс. токенов; картинки поста понимают "
                 "только модели с vision.")
        ctk.CTkLabel(lm, text="Адрес сервера").grid(row=1, column=0, sticky="w", padx=14, pady=5)
        url_e = ctk.CTkEntry(lm, placeholder_text="http://localhost:1234/v1")
        url_e.grid(row=1, column=1, sticky="ew", padx=4)
        self.fields["compat_base_url"] = url_e
        presets = ctk.CTkFrame(lm, fg_color="transparent")
        presets.grid(row=1, column=2, sticky="w", padx=6)

        def preset(u):
            url_e.delete(0, "end")
            url_e.insert(0, u)
        for name, u in core.COMPAT_PRESETS.items():
            ctk.CTkButton(presets, text=name, width=90, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=lambda u=u: preset(u)).pack(side="left", padx=2)
        ctk.CTkLabel(lm, text="API-ключ").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        key_e = ctk.CTkEntry(lm, show="•", placeholder_text="необязательно — локальным серверам не нужен")
        key_e.grid(row=2, column=1, sticky="ew", padx=4)
        self.fields["compat_api_key"] = key_e
        self.compat_model = ctk.CTkComboBox(lm, values=[""], width=260)
        model_row(lm, 3, self.compat_model, "список подгрузится после «Проверить подключение»")
        chk = ctk.CTkFrame(lm, fg_color="transparent")
        chk.grid(row=4, column=1, columnspan=2, sticky="w", padx=4, pady=(2, 4))
        self.compat_btn = ctk.CTkButton(chk, text="Проверить подключение", width=180, command=self.check_compat)
        self.compat_btn.pack(side="left")
        self.compat_lbl = ctk.CTkLabel(chk, text="", text_color="gray")
        self.compat_lbl.pack(side="left", padx=10)

        self.backend_frames = {"claude_code": cc, "api": api, "gemini_cli": gc, "gemini_api": ga,
                               "codex": cx, "openai_api": oa, "openai_compat": lm}

        field(ai, 4, "max_post_images", "Сколько картинок поста показывать", "если включено «смотрит картинки»", width=80)
        field(ai, 5, "claude_timeout_sec", "Таймаут ответа, сек", width=80)
        ctk.CTkFrame(ai, height=8, fg_color="transparent").grid(row=6, column=0)

        im = section("Картинки к комментариям",
                     "Берутся по очереди в случайном порядке, без повторов, пока не кончится круг")
        ctk.CTkLabel(im, text="Папка").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        e = ctk.CTkEntry(im)
        e.grid(row=2, column=1, sticky="ew", padx=4)
        self.fields["images_dir"] = e
        btns = ctk.CTkFrame(im, fg_color="transparent")
        btns.grid(row=2, column=2, sticky="w", padx=6)
        ctk.CTkButton(btns, text="Выбрать…", width=90, command=self.browse_images).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="Открыть", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.open_images).pack(side="left", padx=2)
        self.img_count = ctk.CTkLabel(im, text="", text_color="gray")
        self.img_count.grid(row=3, column=1, sticky="w", padx=4)
        ctk.CTkLabel(im, text="Прикладывать картинку").grid(row=4, column=0, sticky="w", padx=14, pady=(5, 12))
        self.chance_lbl = ctk.CTkLabel(im, text="", width=50)
        self.chance = ctk.CTkSlider(im, from_=0, to=100, number_of_steps=20,
                                    command=lambda v: self.chance_lbl.configure(text=f"{int(v)}%"))
        self.chance.grid(row=4, column=1, sticky="ew", padx=4, pady=(5, 12))
        self.chance_lbl.grid(row=4, column=2, sticky="w", padx=10, pady=(5, 12))

        au = section("Автоматический режим", "Случайная пауза перед публикацией — выглядит естественнее")
        field(au, 2, "delay_min_sec", "Пауза от, сек", width=80)
        field(au, 3, "delay_max_sec", "Пауза до, сек", width=80)

        fl = section("Стоп-слова",
                     "Посты с этими словами пропускаются без генерации. По одному на строку; "
                     "достаточно начала слова («погиб» поймает «погибли»)")
        self.keywords = ctk.CTkTextbox(fl, height=150)
        self.keywords.grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(6, 12))

        wn = section("Программа", "Общее для всех аккаунтов")
        self.tray_sw = ctk.CTkSwitch(wn, text="При закрытии окна сворачивать в трей — программа продолжает "
                                              "работать, выход через значок в трее",
                                     command=self.on_tray_switch)
        if self.conf["close_to_tray"]:
            self.tray_sw.select()
        self.tray_sw.grid(row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 4))
        self.autostart_sw = ctk.CTkSwitch(wn, text=f"Запускать вместе с Windows — сразу в трей, через "
                                                   f"{core.AUTO_RUN_DELAY} сек начинать работу "
                                                   "(аккаунты с «Запускать со всеми»)",
                                          command=self.on_autostart_switch)
        if core.get_autostart():
            self.autostart_sw.select()
        self.autostart_sw.grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=4)
        self.auto_update_sw = ctk.CTkSwitch(wn, text="Устанавливать обновления автоматически — когда очередь "
                                                     "пуста; после обновления работа продолжится сама",
                                            command=self.on_auto_update_switch)
        if self.conf["auto_update"]:
            self.auto_update_sw.select()
        self.auto_update_sw.grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=4)
        if not core.FROZEN:
            self.auto_update_sw.configure(state="disabled",
                                          text="Автообновление — только в установленной версии (из установщика)")
        ver = ctk.CTkFrame(wn, fg_color="transparent")
        ver.grid(row=5, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 12))
        ctk.CTkLabel(ver, text=f"Версия {core.VERSION}").pack(side="left")
        ctk.CTkButton(ver, text="Проверить обновления", width=170, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: self.check_updates(manual=True)).pack(side="left", padx=12)
        self.update_check_lbl = ctk.CTkLabel(ver, text="", text_color="gray")
        self.update_check_lbl.pack(side="left")

        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(bottom, text="Сохранить настройки", height=38, width=220,
                      command=self.apply_settings).pack(side="right")
        self.settings_msg = ctk.CTkLabel(bottom, text="", text_color=GREEN)
        self.settings_msg.pack(side="right", padx=12)
        self.load_settings_form()

    def describe_agy(self, st):
        if st.get("models"):   # заодно подставляем список моделей аккаунта
            self.gemini_model.configure(values=[DEFAULT_MODEL] + st["models"])
        n = len(st.get("models") or [])
        return (f"Вход через Google выполнен ✓  ·  доступно моделей: {n}  ·  "
                "остаток лимита — команда /usage в окне agy"), GREEN

    def describe_codex(self, st):
        if st.get("models"):
            self.codex_model.configure(values=[DEFAULT_MODEL] + st["models"])
        plan = st.get("plan") or ""
        text = f"Вход в ChatGPT выполнен ✓  {st.get('email', '')}" + (f"  ·  тариф {plan}" if plan else "")
        if st.get("authMethod") == "api":
            return text + "  ·  ⚠ вход по API-ключу — запросы оплачиваются с баланса API", "#e0a030"
        return text, GREEN

    def in_thread(self, work, done):
        """work() в фоне, done(result) — потом в окне."""
        import threading
        threading.Thread(target=lambda: (r := work(), self.after(0, lambda: done(r))),
                         daemon=True).start()

    # --- выбор способа подключения к нейросети ---

    def check_compat(self):
        cfg = dict(self.cfg, compat_base_url=self.fields["compat_base_url"].get().strip(),
                   compat_api_key=self.fields["compat_api_key"].get().strip(),
                   compat_model=self.compat_model.get().strip())
        self.compat_btn.configure(state="disabled")
        self.compat_lbl.configure(text="Проверяю…", text_color="gray")

        def done(res):
            ok, msg, models = res
            self.compat_btn.configure(state="normal")
            self.compat_lbl.configure(text=msg, text_color=GREEN if ok else RED)
            if models:
                self.compat_model.configure(values=models)
                if self.compat_model.get().strip() not in models:
                    self.compat_model.set(models[0])
        self.in_thread(lambda: core.check_compat(cfg), done)

    def on_tray_switch(self):
        self.conf["close_to_tray"] = bool(self.tray_sw.get())
        self.save()

    def on_auto_update_switch(self):
        self.conf["auto_update"] = bool(self.auto_update_sw.get())
        self.save()

    def on_autostart_switch(self):
        on = bool(self.autostart_sw.get())
        try:
            core.set_autostart(on)
        except OSError as e:
            messagebox.showerror("Автозапуск", f"Не удалось изменить автозапуск Windows: {e}")
            self.autostart_sw.toggle()
            return
        log.info("Автозапуск с Windows %s", "включён" if on else "выключен")

    def show_proxy(self):
        mode = PROXY_MODES.get(self.proxy_mode.get(), "system")
        if mode == "custom":
            self.proxy_entry.grid(row=8, column=1, sticky="ew", padx=4, pady=5)
            hint = ("Только для Telegram этого аккаунта: SOCKS5, HTTP или MTProxy (секрет dd…; "
                    "ee… не поддерживается). Нейросети ходят через системный прокси или VPN.")
        else:
            self.proxy_entry.grid_forget()
            sp = core.system_proxy()
            if mode == "none":
                hint = "Подключение напрямую. Подходит, если Telegram не заблокирован или VPN в режиме TUN."
            elif sp and sp.startswith("http"):
                hint = f"Найден системный прокси {sp} — через него пойдут Telegram и нейросети."
            elif sp:
                hint = (f"Найден системный прокси {sp} — через него пойдут Telegram и нейросети по API-ключу. "
                        "Claude Code, Codex и Antigravity понимают только HTTP-прокси — для них нужен VPN в режиме TUN.")
            else:
                hint = ("Системный прокси сейчас не включён — подключение напрямую. Если Telegram заблокирован: "
                        "включите VPN в режиме TUN или в режиме «системный прокси» (Proxy) "
                        "и перезапустите аккаунт.")
        self.proxy_hint.configure(text=hint)

    def show_backend(self):
        current = BACKENDS.get(self.backend.get(), "claude_code")
        for key, f in self.backend_frames.items():
            if key == current:
                f.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
            else:
                f.grid_forget()

    def update_price(self):
        m = self.api_model.get()
        self.price_lbl.configure(text=f"цена за 1M токенов (вход / выход): {core.API_PRICES.get(m, '?')}"
                                      "  ·  промпт кэшируется")

    def load_settings_form(self):
        c = self.cfg
        for key, e in self.fields.items():
            e.delete(0, "end")
            e.insert(0, str(c.get(key, "")))
        self.channels_box.delete("1.0", "end")
        self.channels_box.insert("1.0", "\n".join(core.channels(c)))
        self.proxy_mode.set(next(k for k, v in PROXY_MODES.items() if v == c.get("proxy_mode", "system")))
        self.show_proxy()
        self.model.set(c["model"])
        self.api_model.set(c.get("api_model") or core.API_MODELS[0])
        self.gemini_model.set(c.get("gemini_model") or DEFAULT_MODEL)
        self.gemini_api_model.set(c.get("gemini_api_model") or core.GEMINI_API_DEFAULT)
        self.codex_model.set(c.get("codex_model") or DEFAULT_MODEL)
        self.openai_api_model.set(c.get("openai_api_model") or core.OPENAI_API_DEFAULT)
        self.compat_model.set(c.get("compat_model") or "")
        self.backend.set(next(k for k, v in BACKENDS.items() if v == c.get("backend", "claude_code")))
        self.show_backend()
        self.update_price()
        self.chance.set(float(c["attach_image_chance"]) * 100)
        self.chance_lbl.configure(text=f"{int(float(c['attach_image_chance']) * 100)}%")
        self.keywords.delete("1.0", "end")
        self.keywords.insert("1.0", "\n".join(c["skip_keywords"]))
        self.update_img_count()

    def update_img_count(self):
        n = len(core.list_images(self.cfg))
        self.img_count.configure(text=f"Найдено картинок: {n}" if n else "В папке нет подходящих картинок",
                                 text_color="gray" if n else RED)

    def browse_images(self):
        d = filedialog.askdirectory(title="Папка с картинками", initialdir=core.images_path(self.cfg))
        if d:
            try:
                d = str(Path(d).relative_to(BASE))
            except ValueError:
                pass
            e = self.fields["images_dir"]
            e.delete(0, "end")
            e.insert(0, d)
            self.apply_settings(silent=True)

    def open_images(self):
        p = core.images_path(self.cfg)
        p.mkdir(parents=True, exist_ok=True)
        open_path(p)

    def apply_settings(self, silent=False) -> bool:
        f = {k: e.get().strip() for k, e in self.fields.items()}
        new = dict(self.cfg)
        try:
            for k in ("max_post_images", "claude_timeout_sec", "delay_min_sec", "delay_max_sec"):
                new[k] = int(f[k])
        except ValueError:
            messagebox.showerror("Настройки", "Паузы, таймаут и число картинок должны быть целыми числами")
            return False
        if new["delay_min_sec"] > new["delay_max_sec"]:
            new["delay_min_sec"], new["delay_max_sec"] = new["delay_max_sec"], new["delay_min_sec"]
        new["api_id"] = int(f["api_id"]) if f["api_id"].isdigit() else f["api_id"]
        new["api_hash"] = f["api_hash"]
        new["channels"] = core.channels({"channels": self.channels_box.get("1.0", "end").splitlines()})
        new["session_name"] = f["session_name"] or "my_account"
        new["proxy_mode"] = PROXY_MODES.get(self.proxy_mode.get(), "system")
        new["proxy"] = f["proxy"]
        twin = next((p for p in self.conf["profiles"]
                     if p is not self.cfg and p["session_name"] == new["session_name"]), None)
        if twin:
            messagebox.showerror("Настройки", f"Файл сессии «{new['session_name']}» уже занят профилем "
                                              f"«{twin['name']}» — у каждого аккаунта должен быть свой")
            return False
        new["images_dir"] = f["images_dir"] or "images"
        new["model"] = self.model.get() or "sonnet"
        new["backend"] = BACKENDS.get(self.backend.get(), "claude_code")
        new["api_key"] = f["api_key"]
        new["api_model"] = self.api_model.get()
        new["gemini_api_key"] = f["gemini_api_key"]
        gm = self.gemini_model.get().strip()
        new["gemini_model"] = "" if gm in ("", DEFAULT_MODEL) else gm
        new["gemini_api_model"] = self.gemini_api_model.get().strip() or core.GEMINI_API_DEFAULT
        new["openai_api_key"] = f["openai_api_key"]
        cm = self.codex_model.get().strip()
        new["codex_model"] = "" if cm in ("", DEFAULT_MODEL) else cm
        new["openai_api_model"] = self.openai_api_model.get().strip() or core.OPENAI_API_DEFAULT
        new["compat_base_url"] = f["compat_base_url"]
        new["compat_api_key"] = f["compat_api_key"]
        new["compat_model"] = self.compat_model.get().strip()
        new["attach_image_chance"] = round(self.chance.get() / 100, 2)
        new["skip_keywords"] = [w.strip() for w in self.keywords.get("1.0", "end").splitlines() if w.strip()]

        restart_keys = ("api_id", "api_hash", "channels", "session_name", "proxy_mode", "proxy")
        needs_restart = self.engine.status != "stopped" and any(new[k] != self.cfg[k] for k in restart_keys)
        if new["images_dir"] != self.cfg["images_dir"]:
            self.engine.reset_deck()
        self.cfg.update(new)   # тот же словарь, что у движка — изменения применяются сразу
        self.save()
        self.update_channel_menu()
        self.load_settings_form()
        if not silent:
            self.welcome.pack_forget()
            msg = "Сохранено ✓"
            if needs_restart:
                msg += "  Telegram-настройки применятся после перезапуска"
            self.settings_msg.configure(text=msg)
            self.after(6000, lambda: self.settings_msg.configure(text=""))
        return True

    # ------------------------------------------------------------------ промпт

    def _build_prompt_tab(self, tab):
        ctk.CTkLabel(tab, justify="left", anchor="w", text_color="gray", wraplength=1000, text=(
            "Инструкция для нейросети: кто вы, какой стиль, что продвигать. Нейросеть получает её "
            "вместе с текстом поста и должна вернуть только текст комментария.\n"
            "Правило «на тяжёлые темы ответь SKIP» добавляется автоматически. "
            "Пожелания из поля на вкладке «Очередь» приходят строкой «ПОЖЕЛАНИЯ: …»."
        )).pack(fill="x", pady=(0, 6))
        self.prompt_box = ctk.CTkTextbox(tab, wrap="word", font=ctk.CTkFont(size=13))
        self.prompt_box.pack(fill="both", expand=True)
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(bottom, text="Сохранить промпт", height=38, width=200,
                      command=self.save_prompt).pack(side="right")
        ctk.CTkButton(bottom, text="Отменить изменения", height=38, fg_color=GRAY,
                      hover_color=GRAY_HOVER, command=self.load_prompt).pack(side="right", padx=8)
        self.prompt_msg = ctk.CTkLabel(bottom, text="", text_color="gray")
        self.prompt_msg.pack(side="left")
        self.prompt_box.bind("<KeyRelease>", lambda e: self.update_prompt_msg())
        self.load_prompt()

    def prompt_saved_text(self) -> str:
        p = core.prompt_path(self.cfg)
        if p.exists():
            return p.read_text(encoding="utf-8")
        example = core.RES / "prompt.example.txt"
        return example.read_text(encoding="utf-8") if example.exists() else ""

    def load_prompt(self):
        self.prompt_box.delete("1.0", "end")
        self.prompt_box.insert("1.0", self.prompt_saved_text())
        self.update_prompt_msg()

    def prompt_dirty(self) -> bool:
        p = core.prompt_path(self.cfg)
        saved = p.read_text(encoding="utf-8") if p.exists() else None
        return self.prompt_box.get("1.0", "end-1c") != saved

    def save_prompt(self):
        core.prompt_path(self.cfg).write_text(self.prompt_box.get("1.0", "end-1c"), encoding="utf-8")
        self.update_prompt_msg("Сохранено ✓ — применяется к следующему комментарию")

    def update_prompt_msg(self, extra=""):
        n = len(self.prompt_box.get("1.0", "end-1c"))
        dirty = "  ·  есть несохранённые изменения" if self.prompt_dirty() else ""
        self.prompt_msg.configure(text=f"{n} символов{dirty}" + (f"  ·  {extra}" if extra else ""))

    # ------------------------------------------------------------------ журнал

    def _build_log_tab(self, tab):
        self.log_box = ctk.CTkTextbox(tab, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_config("WARNING", foreground="#e0a030")
        self.log_box.tag_config("ERROR", foreground=RED)
        self.log_box.configure(state="disabled")
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(bottom, text="Очистить", width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.clear_log).pack(side="right")
        ctk.CTkButton(bottom, text="Открыть log.txt", width=140, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: open_path(BASE / "log.txt")).pack(side="right", padx=8)

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def add_log(self, rec: logging.LogRecord):
        import time
        line = f"{time.strftime('%H:%M:%S', time.localtime(rec.created))}  {rec.getMessage()}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line, rec.levelname if rec.levelno >= logging.WARNING else ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.statusbar.configure(text=rec.getMessage().splitlines()[0][:160],
                                 text_color=RED if rec.levelno >= logging.ERROR else "gray")

    # ------------------------------------------------------------------ события

    def poll(self):
        while not self.log_q.empty():
            self.add_log(self.log_q.get_nowait())
        for e in list(self.engines.values()):
            while not e.events.empty():
                kind, data = e.events.get_nowait()
                if e.cfg["id"] in self.engines:   # профиль могли удалить
                    self.handle(e, kind, data)
        self.after(100, self.poll)

    def handle(self, e: Engine, kind, data):
        own_dialog = self.login_dialog if self.login_dialog and self.login_dialog.engine is e else None
        if kind == "status":
            self.render_status()
            if data == "stopped":
                self.login_waiting = [x for x in self.login_waiting if x != e.cfg["id"]]
                if own_dialog:
                    own_dialog.close()
            self.render_queue()
        elif kind == "login_needed":
            if self.state() == "withdrawn":   # окно в трее — без него вход не пройти
                self.show_window()
                if self.tray:
                    self.tray.notify(f"Аккаунту «{e.cfg['name']}» нужен вход в Telegram")
            self.login_waiting.append(e.cfg["id"])
            self.next_login()
        elif kind in ("qr", "code_sent", "password_needed", "login_error"):
            if own_dialog:
                own_dialog.on_event(kind, data)
        elif kind == "me":
            if own_dialog:
                own_dialog.close()
            self.render_status()
        elif kind == "pending":
            if self.selected is None:
                self.selected = data.uid
            self.tabs.set("Очередь")
            self.render_queue()
            self.bell()
            if self.tray and self.state() == "withdrawn":
                who = f"«{e.cfg['name']}» · " if len(self.engines) > 1 else ""
                self.tray.notify(f"{who}Новый пост {data.channel} ждёт подтверждения")
        elif kind == "pending_update":
            self.render_queue()
        elif kind == "pending_done":
            key, result = data
            uid = f"{e.cfg['id']}|{key}"
            self.shown_comment.pop(uid, None)
            if self.selected == uid:
                self.selected = None
            self.render_queue()
            if result == "published":
                self.statusbar.configure(text="✅ Комментарий опубликован", text_color=GREEN)
        elif kind == "posted":
            self.posted_count += 1
            self.counter_lbl.configure(text=f"Опубликовано за сессию: {self.posted_count}")

    def on_close(self):
        """Крестик: в трей (программа продолжает работать) или выход — по настройке."""
        if self.tray and self.conf["close_to_tray"]:
            self.save_draft()
            self.withdraw()
            if not self.tray_hinted:
                self.tray_hinted = True
                self.tray.notify("Программа продолжает работать в трее. Открыть — щелчок по значку, "
                                 "выйти — правая кнопка → «Выход».")
            return
        self.quit_app()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def quit_app(self):
        self.show_window()   # вопросы ниже должны быть видны, даже если окно было в трее
        waiting = len(self.all_pending())
        if waiting and not messagebox.askyesno(
                "Выход", f"В очереди {waiting} непроверенных комментариев. Выйти?"):
            return
        if self.prompt_dirty() and messagebox.askyesno("Промпт", "Сохранить изменения в промпте?"):
            self.save_prompt()
        self.shutdown()
        self.destroy()


def run():
    instance = single_instance()
    if instance is False:   # уже запущена — она сама покажет окно
        return
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    args = sys.argv[1:]
    autostart = core.AUTOSTART_ARG in args
    # После обновления установщик передаёт --run=1 (продолжить работу) и --tray=1 (окно было в трее)
    resume = "--run=1" in args
    try:
        App(hidden=autostart or "--tray=1" in args,
            run_delay=core.AUTO_RUN_DELAY if autostart else 3 if resume else None,
            updated="--updated" in args, instance=instance).mainloop()
    except Exception as e:
        logging.getLogger("autocomment").exception("Сбой")
        messagebox.showerror("TG Автокомментатор", f"Программа упала:\n{e}\n\nПодробности в log.txt")
        raise


if __name__ == "__main__":
    run()
