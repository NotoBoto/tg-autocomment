"""
Окно автокомментатора. Запуск: start.bat или python app.py
"""
import csv
import ctypes
import logging
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import webbrowser
import zlib
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
            "Своя модель": "openai_compat", "Без нейросети": "pastes"}
DEFAULT_MODEL = "(по умолчанию)"
PROXY_MODES = {"Системный (из VPN)": "system", "Свой": "custom", "Без прокси": "none"}
MODE_CONFIRM, MODE_AUTO = "Подтверждать вручную", "Публиковать сами"
PUBLISH_MODES = {"С задержкой": "delayed", "Моментально": "instant"}

REPORT_PERIODS = {"Сегодня": 0, "Вчера": 1, "7 дней": 7, "30 дней": 30, "Всё время": None}
# Очистка отчёта: удалить записи старше стольких дней; None — свой срок, 0 — все
CLEAR_PERIODS = {"Старше дня": 1, "Недели": 7, "Месяца": 30, "Своё": None, "Все записи": 0}
CLEAR_UNITS = {"дней": 1, "часов": 1 / 24}
REPORT_MAX_ROWS = 2000   # больше в таблице не показываем — тормозит; выгрузка берёт все
ALL_ACCOUNTS, ALL_CHANNELS = "Все аккаунты", "Все каналы"
# Что попадает в «Копировать отчёт» и «Текст (.txt)» — выбирается в окне «Что включать…»
REPORT_FIELDS = {"summary": "Заголовок и итоги", "number": "Номер по порядку", "time": "Дата и время",
                 "channel": "Канал и номер поста", "account": "Аккаунт (если их несколько)",
                 "post": "Начало поста", "comment": "Текст комментария", "link": "Ссылка на комментарий"}
REPORT_PRESETS = {"Всё": list(REPORT_FIELDS), "Комментарии и ссылки": ["comment", "link"],
                  "Только ссылки": ["link"]}
PASTE_SEP = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.M)   # пасты многострочные — разделяются строкой «---»

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


def fmt_left(seconds: float) -> str:
    """Обратный отсчёт: 0:45, 12:05, 1:02:03."""
    s = max(0, int(seconds + 0.999))
    h, m = divmod(s // 60, 60)
    return f"{h}:{m:02}:{s % 60:02}" if h else f"{m}:{s % 60:02}"


def parse_duration(text: str) -> float | None:
    """Сколько добавить: «20» — минуты, «1,5» — полторы минуты, «1:30» — час тридцать. None — не разобрать."""
    t = text.strip().lower().replace(",", ".")
    try:
        if ":" in t:
            h, m = t.split(":", 1)
            sec = (int(h or 0) * 60 + int(m or 0)) * 60
        else:
            sec = float(t) * 60
    except ValueError:
        return None
    return sec if 0 < sec <= 7 * 24 * 3600 else None


def open_path(p: Path):
    os.startfile(str(p))  # Windows


HOTKEY_ACTIONS = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}   # V, C, X, A


def hotkey_any_layout(e):
    """Ctrl+V/C/X/A в любой раскладке. tkinter узнаёт сочетание по букве (keysym), а в русской раскладке
    на той же клавише «м», «с», «ч», «ф» — и вставка молча не срабатывает. Смотрим на физическую
    клавишу (keycode — код клавиши Windows) и вызываем нужное действие сами."""
    action = HOTKEY_ACTIONS.get(e.keycode)
    if action and e.keysym.lower() not in ("v", "c", "x", "a"):   # латиница — tkinter справится сам
        e.widget.event_generate(action)
        return "break"


def fix_hotkeys_any_layout(root):
    root.bind_all("<Control-KeyPress>", hotkey_any_layout, add="+")


def repaint_after_scroll(sf: ctk.CTkScrollableFrame):
    """customtkinter на Windows при прокрутке иногда оставляет «огрызки» виджетов (зависит от масштаба
    экрана и видеокарты): виджеты переезжают, а освободившееся место не перерисовывается.
    Через миг после любого сдвига (колесо, ползунок, клавиши) просим Windows перерисовать всю область."""
    if sys.platform != "win32":
        return
    canvas = sf._parent_canvas
    pending = {"id": None}
    flags = 0x0001 | 0x0004 | 0x0080 | 0x0100   # RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW

    def redraw():
        pending["id"] = None
        ctypes.windll.user32.RedrawWindow(canvas.winfo_id(), None, None, flags)

    def on_scroll(*args):
        sf._scrollbar.set(*args)   # как было у customtkinter
        if pending["id"]:
            canvas.after_cancel(pending["id"])
        pending["id"] = canvas.after(40, redraw)
    canvas.configure(yscrollcommand=on_scroll)


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
    Меню pystray работает в своём потоке — действия передаём в окно через app.call_soon."""

    def __init__(self, app: "App"):
        import pystray
        self.app = app
        item = pystray.MenuItem
        self.icon = pystray.Icon("tg_autocomment", tray_image(STATUS["stopped"][1]), "TG Автокомментатор",
                                 pystray.Menu(
                                     item("Открыть", lambda: app.call_soon(app.show_window), default=True),
                                     item(lambda i: "■  Остановить" if app.status != "stopped" else "▶  Запустить",
                                          lambda: app.call_soon(app.toggle_run)),
                                     pystray.Menu.SEPARATOR,
                                     item("Выход", lambda: app.call_soon(app.quit_app))))
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
        self.ui_q: queue.Queue = queue.Queue()   # задания для окна из фоновых потоков (call_soon)
        first_run = not core.CONFIG_FILE.exists()
        # Не self.config: это имя метода tkinter
        self.conf = core.load_config()
        resume = self.conf.pop("resume_profiles", None) or []   # записано перед автообновлением
        self.save()   # заодно переводит старый config.json на профили
        self.engines: dict[str, Engine] = {p["id"]: Engine(p) for p in self.conf["profiles"]}
        self.update_show_names()
        self.login_dialog: LoginDialog | None = None
        self.login_waiting: list[str] = []   # профили, ждущие окна входа (оно одно на всех)
        self.selected: str | None = None     # Pending.uid
        self.shown_comment: dict[str, str] = {}
        self.comment_uid: str | None = None   # чей комментарий сейчас в поле (Pending.uid)
        self.posted_count = 0
        self.status = "stopped"   # общий статус; нужен сразу — меню трея читает его из своего потока

        self.title("TG Автокомментатор")
        self.geometry("1120x760")
        self.minsize(940, 640)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        blank = Image.new("RGBA", (1, 1), (0, 0, 0, 0))   # «нет картинки» для подписей (см. show_image)
        self.blank_img = ctk.CTkImage(light_image=blank, dark_image=blank, size=(1, 1))
        self._build_header()
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._build_queue_tab(self.tabs.add("Очередь"))
        self._build_settings_tab(self.tabs.add("Настройки"))
        self._build_prompt_tab(self.tabs.add("Промпт"))
        self._build_report_tab(self.tabs.add("Отчёт"))
        self._build_log_tab(self.tabs.add("Журнал"))
        self.tabs.configure(command=self.on_tab)
        for sf in (self.settings_sf, self.list_frame):
            repaint_after_scroll(sf)
        fix_hotkeys_any_layout(self)
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
        if first_run:
            self.show_welcome()
        else:   # чего-то не хватает для запуска — тоже покажем подсказки (проверка идёт в фоне)
            self.validate_async([self.cfg], lambda res: res[0][1] and self.show_welcome())
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
            # После обновления — только аккаунты, работавшие до него (старые версии их не записывали)
            only = set(resume) if updated and resume else None
            self.after(run_delay * 1000, lambda: self.auto_run(only))
        self.update_info: updater.Update | None = None
        self.updating = False
        self.after(15_000, self.check_updates)
        self.after(100, self.poll)
        self.after(1000, self.tick_schedule)

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
        return (not self.all_pending() and not self.prompt_dirty() and not self.login_dialog
                and not any(e.busy_auto() for e in self.engines.values()))

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
            self.call_soon(lambda: self.update_lbl.configure(text=f"Скачиваю версию {u.version}… {int(x * 100)}%"))

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
            running = [pid for pid, e in self.engines.items() if e.status != "stopped"]
            tray = self.state() == "withdrawn"
            log.info("Устанавливаю версию %s — программа перезапустится", u.version)
            self.conf["resume_profiles"] = running
            self.save()
            self.shutdown()
            try:
                updater.install(setup, run=bool(running), tray=tray)
            except Exception as e:   # антивирус удалил файл и т. п. — возвращаем всё, как было
                log.error("Не удалось запустить установщик обновления: %s", e)
                self.conf.pop("resume_profiles", None)
                self.save()
                self.restore_after_failed_update(running)
                self.update_lbl.configure(text=f"Не удалось установить обновление: {e}")
                return
            self.destroy()
        self.in_thread(work, done)

    def restore_after_failed_update(self, running: list[str]):
        self.updating = False
        try:
            self.tray = Tray(self)
        except Exception as e:
            self.tray = None
            log.warning("Значок в трее недоступен: %s", e)
        for pid in running:
            if pid in self.engines:
                self.engines[pid].start()
        self.show_update_bar()
        self.update_btn.configure(state="normal", text="Повторить")
        self.render_status()

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
                        self.call_soon(self.show_window)
            except OSError:
                if srv.fileno() < 0:   # сокет закрыт — слушать больше нечего
                    return
                time.sleep(1)          # временная ошибка — не крутим цикл вхолостую

    def auto_run(self, only: set[str] | None = None):
        """Запуск без участия человека (вход в Windows, после обновления): без окон с вопросами —
        проблемы пишем в журнал. only — запустить только эти профили, иначе все с «Запускать со всеми»."""
        targets = [p for p in self.conf["profiles"]
                   if (p["id"] in only if only else p["enabled"]) and self.engines[p["id"]].status == "stopped"]

        def done(results):
            started, skipped = [], []
            for p, problems in results:
                e = self.engines.get(p["id"])
                if not e or e.status != "stopped":
                    continue
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
        self.validate_async(targets, done)

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
                state="normal", text="■  Остановить этот" if active else "▶  Запустить только этот",
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
        e.close()
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
        self.profile_run_btn.configure(state="disabled", text="Проверяю…")

        def done(results):
            self.render_profile_bar()
            p, problems = results[0]
            if p["id"] not in self.engines:   # профиль удалили, пока шла проверка
                return
            if problems:
                if p["id"] != self.conf["current"]:
                    self.switch_profile(p["id"])
                messagebox.showwarning("Не всё настроено", "Перед запуском:\n\n• " + "\n• ".join(problems))
                self.show_problem_tab(problems)
                return
            if self.engines[p["id"]].status == "stopped":
                self.engines[p["id"]].start()
        self.validate_async([self.cfg], done)

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
        self.latest_btn = ctk.CTkButton(bar, text="Взять последний пост канала", width=200,
                                        fg_color=GRAY, hover_color=GRAY_HOVER,
                                        command=self.take_latest)
        self.latest_btn.pack(side="right", padx=10)
        # Из какого канала брать последний пост (виден, только если каналов несколько)
        self.latest_chan = ctk.CTkOptionMenu(bar, values=["—"], width=170)
        self.load_header_switches()
        self.update_channel_menu()

    def load_header_switches(self):
        """Режим — настройка открытого профиля."""
        self.mode.set(MODE_CONFIRM if self.cfg["confirm_before_post"] else MODE_AUTO)

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
            where = core.channel_label(chans[0]) if len(chans) == 1 else f"каналов: {len(chans)}"
            text = f"Работает · {running[0].me_name} · {where}"
        self.status_dot.configure(text_color=color)
        self.status_lbl.configure(text=text)
        active = st != "stopped"
        self.start_btn.configure(
            state="normal", text="■  Остановить" if active else "▶  Запустить",
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

    def show_welcome(self):
        self.tabs.set("Настройки")
        self.welcome.pack(fill="x", padx=4, pady=(0, 10), before=self.settings_first)

    def validate_async(self, profiles: list[dict], done):
        """core.validate в фоне: он запускает CLI нейросетей (до десятков секунд), окно не должно висеть.
        done([(профиль, проблемы), …]) вызывается потом в окне."""
        snaps = [dict(p) for p in profiles]   # поток работает с копиями — окно может их менять
        self.in_thread(lambda: [(p, core.validate(sn)) for p, sn in zip(profiles, snaps)], done)

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
        self.start_btn.configure(state="disabled", text="Проверяю…")

        def done(results):
            self.render_status()
            for p, problems in results:
                if problems and p["id"] in self.engines:
                    if p["id"] != self.conf["current"]:
                        self.switch_profile(p["id"])
                    where = f" аккаунта «{p['name']}»" if multi else ""
                    messagebox.showwarning("Не всё настроено",
                                           f"Перед запуском{where}:\n\n• " + "\n• ".join(problems))
                    self.show_problem_tab(problems)
                    return
            for p, _ in results:
                e = self.engines.get(p["id"])
                if e and e.status == "stopped":
                    e.start()
        self.validate_async(targets, done)

    def on_mode(self, value):
        self.cfg["confirm_before_post"] = value == MODE_CONFIRM
        self.save()
        if self.cfg["confirm_before_post"]:
            self.engine.hold_scheduled()   # «подтверждать вручную» — значит, сами больше ничего не публикуем
        log.info("Режим%s: %s", f" «{self.cfg['name']}»" if len(self.engines) > 1 else "", value.lower())
        self.render_queue()

    def take_latest(self):
        self.tabs.set("Очередь")
        chans = core.channels(self.cfg)
        self.engine.take_latest_post(self.latest_chan.get() if len(chans) > 1 else None)

    def update_channel_menu(self):
        chans = [core.channel_label(c) for c in core.channels(self.cfg)]
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

    def take_by_link(self):
        link = self.link_entry.get().strip()
        if not link:
            return
        try:
            core.parse_post_link(link)
        except ValueError as e:
            self.link_msg.configure(text=str(e), text_color=RED)
            return
        if self.engine.status != "running":
            self.link_msg.configure(text=f"Сначала запустите аккаунт «{self.cfg['name']}»", text_color=RED)
            return
        self.link_msg.configure(text="Беру пост… (если что-то не так — причина появится внизу окна)",
                                text_color="gray")
        self.after(8000, lambda: self.link_msg.configure(text=""))
        self.link_entry.delete(0, "end")
        self.engine.take_post_by_link(link)

    def _build_queue_tab(self, tab):
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        # Взять конкретный пост по ссылке — канал может и не быть в списке
        lb = ctk.CTkFrame(tab, fg_color="transparent")
        lb.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        lb.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(lb, text="Пост по ссылке:").grid(row=0, column=0, padx=(0, 8))
        self.link_entry = ctk.CTkEntry(lb, placeholder_text="t.me/канал/123 — любой канал, не обязательно из списка; "
                                                            "закрытый — t.me/c/…, если аккаунт в нём состоит")
        self.link_entry.grid(row=0, column=1, sticky="ew")
        self.link_entry.bind("<Return>", lambda e: self.take_by_link())
        ctk.CTkButton(lb, text="Взять", width=90, command=self.take_by_link).grid(row=0, column=2, padx=(8, 0))
        self.link_msg = ctk.CTkLabel(lb, text="", text_color="gray", anchor="w")
        self.link_msg.grid(row=1, column=1, columnspan=2, sticky="w")

        self.list_frame = ctk.CTkScrollableFrame(tab, width=260, label_text="Очередь")
        self.queue_btns: dict[str, ctk.CTkButton] = {}   # uid → кнопка в списке (для обратного отсчёта)
        self.list_frame.grid(row=1, column=0, sticky="ns", padx=(0, 10))

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
        self.comment_box.bind("<KeyRelease>", lambda e: self.save_draft())
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

        # Таймер автопубликации (режим «Публиковать сами», с задержкой)
        self.sched = ctk.CTkFrame(d, fg_color=("gray88", "gray18"), corner_radius=8)
        self.sched_lbl = ctk.CTkLabel(self.sched, text="", anchor="w", font=ctk.CTkFont(size=14, weight="bold"))
        self.sched_lbl.grid(row=0, column=0, columnspan=7, sticky="w", padx=12, pady=(8, 2))
        ctk.CTkLabel(self.sched, text="Добавить время:").grid(row=1, column=0, padx=(12, 6), pady=(2, 10))
        self.sched_btns = []
        for i, (label, sec) in enumerate((("+5 мин", 300), ("+15 мин", 900), ("+1 час", 3600))):
            b = ctk.CTkButton(self.sched, text=label, width=74, fg_color=GRAY, hover_color=GRAY_HOVER,
                              command=lambda s=sec: self.postpone(s))
            b.grid(row=1, column=1 + i, padx=2, pady=(2, 10))
            self.sched_btns.append(b)
        self.postpone_entry = ctk.CTkEntry(self.sched, width=110, placeholder_text="мин или ч:мм")
        self.postpone_entry.grid(row=1, column=4, padx=(10, 2), pady=(2, 10))
        self.postpone_entry.bind("<Return>", lambda e: self.postpone_custom())
        b = ctk.CTkButton(self.sched, text="Добавить", width=90, command=self.postpone_custom)
        b.grid(row=1, column=5, padx=2, pady=(2, 10))
        self.sched_btns += [b, self.postpone_entry]
        self.sched_msg = ctk.CTkLabel(self.sched, text="", text_color=RED)
        self.sched_msg.grid(row=1, column=6, padx=(8, 12), pady=(2, 10), sticky="w")

        actions = ctk.CTkFrame(d, fg_color="transparent")
        actions.grid(row=8, column=0, columnspan=2, sticky="ew")
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
        # В поле может быть ещё не его текст — тогда чужой черновик ему не записываем
        if p and not p.busy and self.comment_uid == p.uid:
            p.comment = self.comment_box.get("1.0", "end-1c")
            self.shown_comment[p.uid] = p.comment   # это уже показано — не вставлять заново

    def select(self, key):
        self.save_draft()
        self.selected = key
        self.render_queue()

    def render_queue(self):
        pending = self.all_pending()
        if self.selected not in pending:
            self.selected = next(iter(pending), None)

        for w in self.list_frame.winfo_children():
            w.destroy()
        self.queue_btns = {}
        for key, p in pending.items():
            b = self.queue_btns[key] = ctk.CTkButton(
                self.list_frame, text=self.queue_text(p), anchor="w",
                height=56, corner_radius=8,
                fg_color=BTN_COLOR if key == self.selected else ("gray80", "gray25"),
                text_color=("white", "white") if key == self.selected else ("gray10", "gray90"),
                command=lambda k=key: self.select(k),
            )
            b.pack(fill="x", pady=3)
            # перенос длинного текста внутри кнопки
            b._text_label.configure(wraplength=220, justify="left")

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
                       "Хотите проверить промпт? Нажмите «Взять последний пост канала»\n"
                       "или вставьте ссылку на любой пост в поле сверху.")
            else:
                no_ai = self.cfg.get("backend") == "pastes"
                instant = (self.cfg.get("publish_mode") == "instant"
                           and (no_ai or self.cfg.get("instant_comments")))
                msg = (("Автоматический режим, моментально: под новым постом сразу появится случайная паста."
                        if instant and no_ai else
                        "Автоматический режим, моментально: под новым постом сразу появится заготовка,\n"
                        "а потом её текст заменится комментарием нейросети." if instant else
                        "Автоматический режим: комментарии публикуются сами после случайной паузы.\n"
                        "Пока пауза идёт, комментарий виден здесь — можно добавить время,\n"
                        "опубликовать сразу или отменить.")
                       + "\nЧто происходит — во вкладке «Журнал».")
            self.empty.configure(text=msg)
            self.empty.grid(row=1, column=1, sticky="nsew")
            return
        self.empty.grid_forget()
        self.detail.grid(row=1, column=1, sticky="nsew")
        self.show_detail(p)

    def queue_text(self, p: Pending) -> str:
        multi = len(self.engines) > 1
        if p.busy:
            state = "⏳ в работе…"
        elif p.error:
            state = "⚠ ошибка"
        elif p.publish_at:
            state = f"⏱ через {fmt_left(p.publish_at - time.time())}"
        else:
            state = "✓ готов"
        snippet = (p.post_text or "(только медиа)").replace("\n", " ")[:60]
        if (multi or len(core.channels(self.owner(p).cfg)) > 1) and p.channel:
            snippet = f"{p.channel} · {snippet}"
        who = f"{self.owner(p).cfg['name']} · " if multi else ""
        return f"{who}#{p.post_id}  {state}\n{snippet}"

    def sched_text(self, p: Pending) -> str:
        at = time.strftime("%H:%M:%S", time.localtime(p.publish_at))
        return f"⏱  Опубликуется сам через {fmt_left(p.publish_at - time.time())}  (в {at})"

    def tick_schedule(self):
        """Раз в секунду — обратный отсчёт в списке и в карточке, без перерисовки всей очереди."""
        try:
            pending = self.all_pending()
            for uid, b in self.queue_btns.items():
                p = pending.get(uid)
                if p and p.publish_at and not p.busy:
                    text = self.queue_text(p)
                    if b.cget("text") != text:
                        b.configure(text=text)
            p = self.current()
            if p and p.publish_at and not p.busy:
                self.sched_lbl.configure(text=self.sched_text(p))
        except Exception:
            log.exception("Ошибка в окне программы")
        finally:
            self.after(1000, self.tick_schedule)

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

        # Не затираем ручные правки, если в поле этот же комментарий и текст от модели не менялся
        if (self.comment_uid != p.uid or self.shown_comment.get(p.uid) != p.comment
                or self.comment_box.get("1.0", "end-1c") == ""):
            self.comment_box.configure(state="normal")
            self.comment_box.delete("1.0", "end")
            self.comment_box.insert("1.0", p.comment)
            self.shown_comment[p.uid] = p.comment
            self.comment_uid = p.uid
        self.show_image(p)

        if p.busy:
            self.progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=4)
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_forget()
        if p.busy and self.owner(p).no_ai:
            self.err_lbl.configure(text="⏳ Подбираю пасту…", text_color="gray")
        elif p.busy:
            name = core.BACKEND_NAMES.get(self.owner(p).cfg.get("backend"), "Нейросеть")
            self.err_lbl.configure(text=f"⏳ {name} пишет комментарий… (обычно 10–40 секунд)",
                                   text_color="gray")
        else:
            self.err_lbl.configure(text=p.error, text_color=RED)
        if p.publish_at:
            self.sched_lbl.configure(text=self.sched_text(p))
            self.sched_msg.configure(text="")
            self.sched.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 8))
            self.publish_btn.configure(text="✓  Опубликовать сейчас")
            self.skip_btn.configure(text="✕  Отменить публикацию")
        else:
            self.sched.grid_forget()
            self.publish_btn.configure(text="✓  Опубликовать")
            self.skip_btn.configure(text="Пропустить пост")
        st = "disabled" if p.busy else "normal"
        for b in (self.publish_btn, self.skip_btn, self.regen_btn, *self.img_btns, *self.sched_btns):
            b.configure(state=st)
        self.comment_box.configure(state=st)
        no_ai = self.owner(p).no_ai
        self.wish.grid_remove() if no_ai else self.wish.grid()
        self.regen_btn.configure(text="↻  Другая паста" if no_ai else "↻  Новый вариант")
        if not p.comment and not p.busy:
            self.publish_btn.configure(state="disabled")

    def show_image(self, p: Pending):
        img = thumbnail(p.image, 220)
        self._img_ref = img
        if img:
            self.img_preview.configure(image=img, text="")
        else:
            # Не image=None: у customtkinter после этого следующая картинка падает
            # с «image "pyimageN" doesn't exist» — ставим прозрачную заглушку
            self.img_preview.configure(image=self.blank_img,
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
            # Публикуем только текст этого поста: если в поле почему-то чужой — берём его собственный
            text = (self.comment_box.get("1.0", "end-1c") if self.comment_uid == p.uid else p.comment).strip()
            if not text:
                messagebox.showwarning("Пусто", "Комментарий пустой")
                return
            self.owner(p).publish(p.key, text, p.image)

    def skip(self):
        if p := self.current():
            self.owner(p).skip(p.key)

    def postpone(self, seconds: float):
        if p := self.current():
            self.save_draft()
            self.owner(p).postpone(p.key, seconds)

    def postpone_custom(self):
        seconds = parse_duration(self.postpone_entry.get())
        if seconds is None:
            self.sched_msg.configure(text="Минуты (20) или часы:минуты (1:30)")
            return
        self.sched_msg.configure(text="")
        self.postpone_entry.delete(0, "end")
        self.postpone(seconds)

    # ------------------------------------------------------------------ настройки

    def _build_settings_tab(self, tab):
        sf = self.settings_sf = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        sf.pack(fill="both", expand=True)
        self.fields: dict[str, ctk.CTkEntry] = {}

        self.welcome = ctk.CTkFrame(sf, fg_color=("#e3f0ff", "#1d2f44"))
        ctk.CTkLabel(self.welcome, justify="left", anchor="w", wraplength=900, text=(
            "👋  Добро пожаловать! Для начала работы:\n"
            "0. В блоке «Нейросеть» выберите способ: Claude Code, Antigravity или ChatGPT (вход в аккаунт, "
            "кнопка «Установить и войти»), API-ключ Claude/Gemini/OpenAI, свою модель (LM Studio, Ollama) "
            "или «Без нейросети» — тогда программа постит случайные пасты из вашего списка\n"
            + ("1. API ID и API Hash указывать не нужно — в программу встроен свой ключ\n"
               if core.BUILTIN_TELEGRAM_APP else
               "1. Получите API ID и API Hash на my.telegram.org (раздел «API development tools»)\n")
            + 
            "2. Укажите канал, под постами которого нужно комментировать\n"
            "3. Заполните вкладку «Промпт» — инструкцию для нейросети (для «Без нейросети» промпт не нужен: "
            "пасты вводятся тут же, в блоке «Нейросеть»)\n"
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
        builtin = core.BUILTIN_TELEGRAM_APP is not None
        field(tg, 2, "api_id", "API ID", "необязательно — пусто: встроенный ключ" if builtin else "",
              width=200, placeholder_text="встроенный" if builtin else "")
        hash_e = field(tg, 3, "api_hash", "API Hash", show="•",
                       placeholder_text="встроенный" if builtin else "")
        ctk.CTkButton(tg, text="показать", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: hash_e.configure(show="" if hash_e.cget("show") else "•")
                      ).grid(row=3, column=2, sticky="w", padx=10)
        ctk.CTkButton(tg, text=("Свой ключ (по желанию) → my.telegram.org" if builtin
                                else "Где взять API ID и Hash → my.telegram.org"), fg_color="transparent",
                      text_color=("#1f6aa5", "#5aa9e6"), hover=False, anchor="w",
                      command=lambda: webbrowser.open("https://my.telegram.org/apps")
                      ).grid(row=4, column=1, sticky="w")
        ctk.CTkLabel(tg, text="Каналы").grid(row=5, column=0, sticky="nw", padx=14, pady=8)
        self.channels_box = ctk.CTkTextbox(tg, height=116)
        self.channels_box.grid(row=5, column=1, sticky="ew", padx=4, pady=5)
        ctk.CTkLabel(tg, text="по одному на строку:\nusername, @username\nили ссылка t.me/…\n"
                              "закрытый — приглашение\nt.me/+… или ссылка на\nего пост t.me/c/…",
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

        # --- без нейросети: случайные пасты из списка ---
        ps = sub("Нейросеть не нужна: под постом публикуется случайная паста из списка — по очереди в "
                 "случайном порядке, без повторов, пока не кончится круг. Промпт не используется, "
                 "«↻ Другая паста» в очереди берёт другую. Длинная паста (больше 1024 символов) уходит без "
                 "картинки — в подпись она не помещается. Стоп-слова работают как обычно.")
        ctk.CTkLabel(ps, text="Пасты").grid(row=1, column=0, sticky="nw", padx=14, pady=8)
        self.pastes_box = ctk.CTkTextbox(ps, height=220, wrap="word")
        self.pastes_box.grid(row=1, column=1, sticky="ew", padx=4, pady=5)
        ctk.CTkLabel(ps, text="паста может быть\nв несколько строк;\nмежду пастами —\nстрока ---",
                     text_color="gray", justify="left").grid(row=1, column=2, sticky="nw", padx=10, pady=5)
        self.pastes_count = ctk.CTkLabel(ps, text="", text_color="gray")
        self.pastes_count.grid(row=2, column=1, sticky="w", padx=4)
        self.pastes_box.bind("<KeyRelease>", lambda e: self.update_pastes_count())

        self.backend_frames = {"claude_code": cc, "api": api, "gemini_cli": gc, "gemini_api": ga,
                               "codex": cx, "openai_api": oa, "openai_compat": lm, "pastes": ps}

        # Общее для всех нейросетей — без нейросети не нужно, прячется в show_backend
        self.ai_common = ctk.CTkFrame(ai, fg_color="transparent")
        self.ai_common.grid_columnconfigure(0, minsize=LABEL_W)
        self.see_sw = ctk.CTkSwitch(self.ai_common, text="Нейронка смотрит картинки поста")
        self.see_sw.grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(8, 4))
        field(self.ai_common, 1, "max_post_images", "Сколько картинок поста показывать",
              "если включено «смотрит картинки»", width=80)
        field(self.ai_common, 2, "claude_timeout_sec", "Таймаут ответа, сек", width=80)
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
        # Поле, а не слайдер: точное значение, и прокрутка колёсиком его случайно не сдвинет
        chance_row = ctk.CTkFrame(im, fg_color="transparent")
        chance_row.grid(row=4, column=1, columnspan=2, sticky="w", padx=4, pady=(5, 12))
        self.chance = ctk.CTkEntry(chance_row, width=60, justify="right")
        self.chance.pack(side="left")
        ctk.CTkLabel(chance_row, text="%   0 — никогда, 100 — к каждому комментарию",
                     text_color="gray").pack(side="left", padx=(6, 0))

        au = section("Автоматический режим", "Как публиковать, когда в шапке выбрано «Публиковать сами»")
        au.grid_columnconfigure(0, minsize=LABEL_W)
        ctk.CTkLabel(au, text="Публикация").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        self.publish_mode = ctk.CTkSegmentedButton(au, values=list(PUBLISH_MODES),
                                                   command=lambda v: self.show_publish_mode())
        self.publish_mode.grid(row=2, column=1, sticky="w", padx=4)

        def sub_au(hint):
            f = ctk.CTkFrame(au, fg_color="transparent")
            f.grid_columnconfigure(0, minsize=LABEL_W)
            f.grid_columnconfigure(1, weight=1)
            f.hint = ctk.CTkLabel(f, text=hint, text_color="gray", justify="left", wraplength=900)
            f.hint.grid(row=0, column=0, columnspan=3, sticky="w", padx=14)
            return f

        dl = self.delay_frame = sub_au("")   # подсказка — в show_publish_mode, она зависит от нейросети
        field(dl, 1, "delay_min_sec", "Пауза от, сек", width=80)
        field(dl, 2, "delay_max_sec", "Пауза до, сек", width=80)
        ins = sub_au("Под новым постом сразу появляется заготовка — так комментарий окажется среди первых. "
                     "Когда нейросеть напишет настоящий, текст заготовки заменится им (в Telegram будет "
                     "пометка «изменено»). Если нейросеть ответит SKIP — заготовка удалится")
        ctk.CTkLabel(ins, text="Заготовки").grid(row=1, column=0, sticky="nw", padx=14, pady=8)
        self.instant_box = ctk.CTkTextbox(ins, height=110)
        self.instant_box.grid(row=1, column=1, sticky="ew", padx=4, pady=(5, 12))
        ctk.CTkLabel(ins, text="по одной на строку,\nберётся случайная", text_color="gray",
                     justify="left").grid(row=1, column=2, sticky="nw", padx=10, pady=5)
        # Без нейросети заготовки не нужны: паста готова сразу
        ins_pastes = sub_au("Паста публикуется сразу под новым постом — без паузы и без очереди")
        self.publish_frames = {"delayed": dl, "instant": ins, "instant_pastes": ins_pastes}

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
        self._settings_msg_job = None
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
        threading.Thread(target=lambda: (r := work(), self.call_soon(lambda: done(r))), daemon=True).start()

    def call_soon(self, fn):
        """Выполнить fn в потоке окна. Можно звать из любого потока: tkinter из чужих потоков
        вызывать нельзя (до запуска mainloop это падает), поэтому кладём в очередь — её разбирает poll()."""
        self.ui_q.put(fn)

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
        if current == "pastes":
            self.ai_common.grid_forget()
        else:
            self.ai_common.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.show_publish_mode()   # подсказки публикации зависят от того, есть ли нейросеть

    def show_publish_mode(self):
        current = PUBLISH_MODES.get(self.publish_mode.get(), "delayed")
        no_ai = BACKENDS.get(self.backend.get()) == "pastes"
        if current == "instant" and no_ai:
            current = "instant_pastes"
        self.delay_frame.hint.configure(text=(
            ("Паста публикуется" if no_ai else "Нейросеть пишет комментарий, и он публикуется")
            + " после случайной паузы — так выглядит естественнее. Пока пауза идёт, комментарий виден "
              "в «Очереди». Пауза 0 — публикуется сразу, без очереди"))
        for key, f in self.publish_frames.items():
            if key == current:
                f.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 6))
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
        self.see_sw.select() if c["send_post_images"] else self.see_sw.deselect()
        self.update_price()
        self.chance.delete(0, "end")
        self.chance.insert(0, f"{float(c['attach_image_chance']) * 100:g}")
        self.keywords.delete("1.0", "end")
        self.keywords.insert("1.0", "\n".join(c["skip_keywords"]))
        self.publish_mode.set(next(k for k, v in PUBLISH_MODES.items() if v == c.get("publish_mode", "delayed")))
        self.show_publish_mode()
        self.instant_box.delete("1.0", "end")
        self.instant_box.insert("1.0", "\n".join(c["instant_comments"]))
        self.pastes_box.delete("1.0", "end")
        self.pastes_box.insert("1.0", "\n---\n".join(c.get("pastes", [])))
        self.update_pastes_count()
        self.update_img_count()

    def read_pastes(self) -> list[str]:
        """Пасты из поля; по краям убираем только пустые строки — отступы внутри пасты сохраняются."""
        pastes = []
        for chunk in PASTE_SEP.split(self.pastes_box.get("1.0", "end-1c")):
            lines = [line.rstrip() for line in chunk.splitlines()]
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                pastes.append("\n".join(lines))
        return pastes

    def update_pastes_count(self):
        n = len(self.read_pastes())
        self.pastes_count.configure(text=f"Паст: {n}" if n else "Список пуст — добавьте хотя бы одну пасту",
                                    text_color="gray" if n else RED)

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
        new["send_post_images"] = bool(self.see_sw.get())
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
        try:
            chance = float(self.chance.get().strip().rstrip("%").replace(",", ".") or "nan")
        except ValueError:
            chance = float("nan")
        if not 0 <= chance <= 100:   # nan сюда тоже не пройдёт
            messagebox.showerror("Настройки", "Шанс приложить картинку — число от 0 до 100")
            return False
        new["attach_image_chance"] = round(chance / 100, 4)
        new["skip_keywords"] = [w.strip() for w in self.keywords.get("1.0", "end").splitlines() if w.strip()]
        new["publish_mode"] = PUBLISH_MODES.get(self.publish_mode.get(), "delayed")
        new["instant_comments"] = [w.strip() for w in self.instant_box.get("1.0", "end").splitlines() if w.strip()]
        new["pastes"] = self.read_pastes()
        if new["backend"] == "pastes" and not new["pastes"]:
            messagebox.showerror("Настройки", "Для режима без нейросети нужна хотя бы одна паста")
            return False
        too_long = [i for i, x in enumerate(new["pastes"], 1) if len(x) > core.MESSAGE_LIMIT]
        if too_long:
            messagebox.showerror("Настройки", f"Telegram не примет комментарий длиннее {core.MESSAGE_LIMIT} "
                                              f"символов — сократите пасты №{', '.join(map(str, too_long))}")
            return False
        if (new["publish_mode"] == "instant" and not new["instant_comments"] and not new["confirm_before_post"]
                and new["backend"] != "pastes"):
            messagebox.showerror("Настройки", "Для моментальной публикации нужна хотя бы одна заготовка")
            return False

        # Эти настройки нужны только при подключении к Telegram — на ходу их не сменить
        restart_keys = {"api_id": "API ID", "api_hash": "API hash", "session_name": "файл сессии",
                        "proxy_mode": "прокси", "proxy": "прокси"}
        needs_restart = [] if self.engine.status == "stopped" else list(dict.fromkeys(
            label for k, label in restart_keys.items() if new[k] != self.cfg[k]))
        channels_changed = new["channels"] != self.cfg["channels"]
        if new["images_dir"] != self.cfg["images_dir"]:
            self.engine.reset_deck()
        self.cfg.update(new)   # тот же словарь, что у движка — изменения применяются сразу
        self.save()
        if channels_changed:
            self.engine.update_channels()
        self.update_channel_menu()
        self.load_settings_form()
        self.render_queue()   # подсказка пустой очереди зависит от режима публикации
        if not silent:
            self.welcome.pack_forget()
            if self._settings_msg_job:
                self.after_cancel(self._settings_msg_job)
                self._settings_msg_job = None
            if needs_restart:   # висит до следующего сохранения — легко пропустить
                self.settings_msg.configure(
                    text=f"Сохранено ✓  Нужен перезапуск аккаунта («Остановить» → «Запустить»), чтобы применить: "
                         f"{', '.join(needs_restart)}", text_color=RED)
            else:
                self.settings_msg.configure(text="Сохранено ✓", text_color=GREEN)
                self._settings_msg_job = self.after(6000, lambda: self.settings_msg.configure(text=""))
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

    # ------------------------------------------------------------------ отчёт

    def _build_report_tab(self, tab):
        self.report_reader = core.ReportReader()
        self.report: list[dict] = []        # все записи из comments.jsonl
        self.report_rows: list[dict] = []   # прошедшие фильтры, новые первыми
        self.report_by_id: dict[str, dict] = {}   # строки таблицы (iid — id записи) → запись
        self.report_stale = True
        self._search_job = None
        self.clear_dlg = None
        self.fields_dlg = None

        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x")
        self.rep_period = ctk.CTkSegmentedButton(top, values=list(REPORT_PERIODS),
                                                 command=lambda v: self.render_report())
        self.rep_period.set("Сегодня")
        self.rep_period.pack(side="left")
        self.rep_account = ctk.CTkOptionMenu(top, values=[ALL_ACCOUNTS], width=160,
                                             command=lambda v: self.render_report())
        self.rep_account.pack(side="left", padx=(12, 4))
        self.rep_channel = ctk.CTkOptionMenu(top, values=[ALL_CHANNELS], width=170,
                                             command=lambda v: self.render_report())
        self.rep_channel.pack(side="left", padx=4)
        self.rep_search = ctk.CTkEntry(top, placeholder_text="Поиск по тексту…")
        self.rep_search.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.rep_search.bind("<KeyRelease>", lambda e: self.search_report_soon())

        self.rep_summary = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=1040)
        self.rep_summary.pack(fill="x", pady=(8, 4))

        table = ctk.CTkFrame(tab, fg_color="transparent")
        table.pack(fill="both", expand=True)
        cols = {"time": ("Когда", 95), "account": ("Аккаунт", 120), "channel": ("Канал", 150),
                "post": ("Пост", 60), "source": ("Откуда текст", 190), "comment": ("Комментарий", 380)}
        self.rep_tree = ttk.Treeview(table, columns=list(cols), show="headings", style="Report.Treeview",
                                     selectmode="browse")
        for key, (title, width) in cols.items():
            self.rep_tree.heading(key, text=title, anchor="w")
            self.rep_tree.column(key, width=width, minwidth=50, stretch=key == "comment", anchor="w")
        bar = ctk.CTkScrollbar(table, command=self.rep_tree.yview)
        self.rep_tree.configure(yscrollcommand=bar.set)
        self.rep_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.rep_tree.bind("<<TreeviewSelect>>", lambda e: self.show_report_detail())
        self.rep_tree.bind("<Double-1>", lambda e: self.open_report_link())

        # Подробности и кнопки закреплены внизу: при длинной сводке сжимается таблица, а не они
        self.rep_detail = ctk.CTkTextbox(tab, height=140, wrap="word")
        self.rep_detail.pack(side="bottom", fill="x", pady=(6, 0), before=table)
        self.rep_detail.tag_config("head", foreground="gray")
        self.rep_detail.configure(state="disabled")

        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", pady=(6, 0), before=self.rep_detail)
        ctk.CTkButton(bottom, text="Excel (CSV)", width=110,
                      command=self.export_report).pack(side="right")
        ctk.CTkButton(bottom, text="Текст (.txt)", width=110,
                      command=self.save_report_text).pack(side="right", padx=(8, 6))
        ctk.CTkButton(bottom, text="Копировать отчёт", width=150,
                      command=self.copy_report_text).pack(side="right")
        ctk.CTkButton(bottom, text="Что включать…", width=120, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.report_fields_dialog).pack(side="right", padx=(0, 6))
        ctk.CTkButton(bottom, text="Обновить", width=100, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.load_report).pack(side="right", padx=8)
        ctk.CTkButton(bottom, text="Очистить…", width=100, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.clear_report).pack(side="right")
        self.rep_open_btn = ctk.CTkButton(bottom, text="Открыть в Telegram", width=170,
                                          command=self.open_report_link, state="disabled")
        self.rep_open_btn.pack(side="left")
        self.rep_copy_btn = ctk.CTkButton(bottom, text="Копировать комментарий", width=190, fg_color=GRAY,
                                          hover_color=GRAY_HOVER, command=self.copy_report_comment,
                                          state="disabled")
        self.rep_copy_btn.pack(side="left", padx=8)
        self.style_report()

    def on_tab(self):
        if self.tabs.get() == "Отчёт":
            self.style_report()   # тема Windows могла смениться
            if self.report_stale:
                self.load_report()

    def search_report_soon(self):
        """Поиск — когда перестали печатать, а не на каждую букву."""
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(300, self.render_report)

    def style_report(self):
        """Таблица — ttk, у неё свои цвета: подстраиваем под светлую/тёмную тему окна."""
        dark = ctk.get_appearance_mode() == "Dark"
        bg, fg, head, sel = (("#242424", "#dce4ee", "#333333", "#1f538d") if dark else
                             ("#ffffff", "#1a1a1a", "#e4e4e4", "#3a7ebf"))
        scale = self._get_window_scaling()
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Report.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                        rowheight=int(24 * scale), borderwidth=0, font=("Segoe UI", 10),
                        bordercolor=bg, lightcolor=bg, darkcolor=bg)
        style.configure("Report.Treeview.Heading", background=head, foreground=fg, relief="flat",
                        font=("Segoe UI", 10, "bold"))
        style.map("Report.Treeview", background=[("selected", sel)], foreground=[("selected", "white")])
        style.map("Report.Treeview.Heading", background=[("active", head)])
        self.rep_tree.tag_configure("deleted", foreground="gray")

    def load_report(self):
        try:
            self.report = self.report_reader.load()
        except OSError as e:
            log.warning("Не удалось прочитать отчёт: %s", e)
            self.report = []
        self.report_stale = False
        names = sorted({r.get("profile_name", "") for r in self.report} - {""})
        chans = sorted({r.get("channel", "") for r in self.report} - {""})
        for menu, every, values in ((self.rep_account, ALL_ACCOUNTS, names),
                                    (self.rep_channel, ALL_CHANNELS, chans)):
            menu.configure(values=[every, *values])
            if menu.get() not in values:
                menu.set(every)
        self.render_report()

    def report_filtered(self) -> list[dict]:
        days = REPORT_PERIODS.get(self.rep_period.get())
        start = end = None
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if days == 1:   # «Вчера» — только вчерашний день
            start, end = today - timedelta(days=1), today
        elif days is not None:   # «Сегодня» и «N дней» — N календарных дней, считая сегодняшний
            start = today - timedelta(days=max(days - 1, 0))
        acc, chan = self.rep_account.get(), self.rep_channel.get()
        q = self.rep_search.get().strip().lower()
        rows = []
        for r in self.report:
            t = datetime.fromisoformat(r["time"])   # проверено при чтении (core.parse_report_line)
            if (start and t < start) or (end and t >= end):
                continue
            if (acc != ALL_ACCOUNTS and r.get("profile_name") != acc
                    or chan != ALL_CHANNELS and r.get("channel") != chan):
                continue
            if q and q not in f"{r.get('comment', '')}\n{r.get('post_text', '')}".lower():
                continue
            rows.append(r)
        rows.sort(key=lambda r: r["time"], reverse=True)   # новые сверху
        return rows

    def render_report(self):
        self._search_job = None
        rows = self.report_rows = self.report_filtered()
        tree = self.rep_tree
        # Таблица обновляется и сама, после каждого комментария, — выбранная строка и прокрутка
        # не должны слетать, пока человек читает
        selected, scroll = tree.selection(), tree.yview()[0]
        tree.delete(*tree.get_children())
        shown = rows[:REPORT_MAX_ROWS]
        self.report_by_id = {r["id"]: r for r in shown}
        for r in shown:
            t = datetime.fromisoformat(r["time"])
            when = t.strftime("%H:%M:%S" if t.date() == datetime.now().date() else "%d.%m %H:%M")
            comment = " ".join(r.get("comment", "").split())
            if r.get("deleted"):
                comment = f"[{r['deleted']}] {comment}"
            tree.insert("", "end", iid=r["id"], tags=("deleted",) if r.get("deleted") else (), values=(
                when, r.get("profile_name", ""), r.get("channel", ""),
                f"#{r.get('post_id', '')}", r.get("source", ""), comment[:300]))
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])
        tree.yview_moveto(scroll)
        summary = self.report_summary(rows)
        if len(rows) > REPORT_MAX_ROWS:
            summary += (f"\nВ таблице — последние {REPORT_MAX_ROWS} из {len(rows)}: уточните период или фильтр. "
                        "Копирование и выгрузка берут все")
        self.rep_summary.configure(text=summary)
        self.show_report_detail()

    def report_summary(self, rows: list[dict]) -> str:
        live = [r for r in rows if not r.get("deleted")]
        if not rows:
            return ("За этот период комментариев нет." if self.report else
                    "Здесь появятся все опубликованные комментарии — сколько, где и какие.")
        posts = len({(r.get("chat_id"), r.get("post_id")) for r in live})
        text = f"Опубликовано комментариев: {len(live)}  ·  под постами: {posts}"
        if len(live) < len(rows):
            text += f"  ·  удалено заготовок: {len(rows) - len(live)}"

        def top(field):
            c = Counter(r.get(field) or "—" for r in live)
            return ",  ".join(f"{k} — {v}" for k, v in c.most_common())
        text += f"\nПо каналам: {top('channel')}"
        if len({r.get('profile_name') for r in live}) > 1:
            text += f"\nПо аккаунтам: {top('profile_name')}"
        text += f"\nОткуда текст: {top('source')}"
        return text

    def report_current(self) -> dict | None:
        sel = self.rep_tree.selection()
        return self.report_by_id.get(sel[0]) if sel else None

    def show_report_detail(self):
        r = self.report_current()
        box = self.rep_detail
        box.configure(state="normal")
        box.delete("1.0", "end")
        if r:
            t = datetime.fromisoformat(r["time"]).strftime("%d.%m.%Y %H:%M:%S")
            who = r.get("profile_name", "") + (f" ({r['account']})" if r.get("account") else "")
            head = f"{t}  ·  {who}  ·  {r.get('channel', '')} #{r.get('post_id', '')}  ·  {r.get('source', '')}"
            if r.get("image"):
                head += f"  ·  картинка {r['image']}"
            box.insert("end", head + "\n", "head")
            if r.get("deleted"):
                box.insert("end", f"Комментарий {r['deleted']}\n", "head")
            box.insert("end", "\nКомментарий:\n" + r.get("comment", "") + "\n")
            if r.get("placeholder"):
                box.insert("end", f"\nСначала была заготовка: {r['placeholder']}\n", "head")
            box.insert("end", "\nПост:\n" + (r.get("post_text") or "(текста нет, только медиа)"), "head")
        else:
            box.insert("end", "Выберите комментарий в таблице — здесь будет его полный текст и пост. "
                              "Двойной щелчок открывает комментарий в Telegram.", "head")
        box.configure(state="disabled")
        st = "normal" if r else "disabled"
        self.rep_open_btn.configure(state=st if r and r.get("link") else "disabled")
        self.rep_copy_btn.configure(state=st)

    def open_report_link(self):
        r = self.report_current()
        if r and r.get("link"):
            webbrowser.open(r["link"])

    def copy_report_comment(self):
        if r := self.report_current():
            self.clipboard_clear()
            self.clipboard_append(r.get("comment", ""))
            self.statusbar.configure(text="Комментарий скопирован", text_color="gray")

    def report_fields(self) -> set[str]:
        saved = self.conf.get("report_fields")
        fields = {f for f in saved if f in REPORT_FIELDS} if saved is not None else set(REPORT_FIELDS)
        return fields or set(REPORT_FIELDS)

    def report_text(self, rows: list[dict], fields: set[str] | None = None) -> str:
        """Отчёт обычным текстом — чтобы переслать: сводка и комментарии по порядку времени.
        Только опубликованные (удалённые заготовки не в счёт) и без «откуда текст».
        fields — что включать (ключи REPORT_FIELDS), по умолчанию — выбранное в «Что включать…»."""
        fields = self.report_fields() if fields is None else fields
        live = [r for r in reversed(rows) if not r.get("deleted")]
        lines = []
        if "summary" in fields:
            period = self.rep_period.get()
            lines.append(f"Отчёт о комментариях — {period.lower()}"
                         + ("" if period in ("Сегодня", "Вчера") else f" (на {datetime.now():%d.%m.%Y})"))
            if period == "Сегодня":
                lines[0] += f", {datetime.now():%d.%m.%Y}"
            elif period == "Вчера":
                lines[0] += f", {datetime.now() - timedelta(days=1):%d.%m.%Y}"
            for menu, every, title in ((self.rep_account, ALL_ACCOUNTS, "Аккаунт"),
                                       (self.rep_channel, ALL_CHANNELS, "Канал")):
                if menu.get() != every:
                    lines.append(f"{title}: {menu.get()}")
            posts = len({(r.get("chat_id"), r.get("post_id")) for r in live})
            lines.append(f"Всего комментариев: {len(live)}, под постами: {posts}")
            by_chan = Counter(r.get("channel") or "—" for r in live)
            if len(by_chan) > 1:
                lines.append("По каналам: " + ", ".join(f"{k} — {v}" for k, v in by_chan.most_common()))
        many_accounts = len({r.get("profile_name") for r in live}) > 1
        entries = []
        for i, r in enumerate(live, 1):
            head = []
            if "time" in fields:
                head.append(datetime.fromisoformat(r["time"]).strftime("%d.%m %H:%M"))
            if "channel" in fields:
                head.append(f"{r.get('channel', '')} · пост #{r.get('post_id', '')}")
            if "account" in fields and many_accounts:
                head.append(r.get("profile_name", ""))
            entry = [" · ".join(head)] if head else []
            post = " ".join((r.get("post_text") or "").split())
            if "post" in fields and post:
                entry.append(f"Пост: {post[:120]}{'…' if len(post) > 120 else ''}")
            if "comment" in fields:
                entry.append(r.get("comment", ""))
            if "link" in fields and r.get("link"):
                entry.append(r["link"])
            if not entry:   # например, «только ссылки», а у записи ссылки нет
                continue
            if "number" in fields:
                entry[0] = f"{i}. {entry[0]}"
            entries.append("\n".join(entry))
        # По строке на запись (например, одни ссылки) — сплошным списком, иначе записи через пустую строку
        compact = all("\n" not in e for e in entries)
        body = ("\n" if compact else "\n\n").join(entries)
        return "\n\n".join(x for x in ("\n".join(lines), body) if x) + "\n"

    def report_fields_dialog(self):
        """Окно «Что включать»: галочки, готовые наборы и пример того, что получится."""
        if self.fields_dlg and self.fields_dlg.winfo_exists():
            self.fields_dlg.lift()
            self.fields_dlg.focus()
            return
        dlg = self.fields_dlg = ctk.CTkToplevel(self)
        dlg.title("Что включать в отчёт")
        dlg.resizable(False, False)
        dlg.transient(self)
        ctk.CTkLabel(dlg, anchor="w", justify="left", wraplength=520, text=(
            "Для «Копировать отчёт» и «Текст (.txt)». Excel (CSV) выгружает всё.")).pack(
            fill="x", padx=20, pady=(18, 8))
        presets = ctk.CTkFrame(dlg, fg_color="transparent")
        presets.pack(fill="x", padx=20)
        ctk.CTkLabel(presets, text="Готовые наборы:").pack(side="left")
        boxes = ctk.CTkFrame(dlg, fg_color="transparent")
        boxes.pack(fill="x", padx=20, pady=(10, 0))
        chosen = self.report_fields()
        vars_ = {}
        for i, (key, title) in enumerate(REPORT_FIELDS.items()):
            v = vars_[key] = ctk.BooleanVar(dlg, key in chosen)
            ctk.CTkCheckBox(boxes, text=title, variable=v, command=lambda: update()).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 24), pady=3)
        ctk.CTkLabel(dlg, text="Пример (первые записи текущего отчёта):", anchor="w",
                     text_color="gray").pack(fill="x", padx=20, pady=(12, 2))
        preview = ctk.CTkTextbox(dlg, width=520, height=180, wrap="word")
        preview.pack(padx=20)
        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(10, 18))
        warn = ctk.CTkLabel(btns, text="", text_color=RED)
        warn.pack(side="left")
        ctk.CTkButton(btns, text="Готово", width=110, command=dlg.destroy).pack(side="right")

        def apply_preset(name):
            for key, v in vars_.items():
                v.set(key in REPORT_PRESETS[name])
            update()

        for name in REPORT_PRESETS:
            ctk.CTkButton(presets, text=name, width=0, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=lambda n=name: apply_preset(n)).pack(side="left", padx=(8, 0))

        def update():
            fields = {k for k, v in vars_.items() if v.get()}
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            if not fields - {"summary", "number"}:   # одни номера или итоги без записей — пусто
                warn.configure(text="Выберите, что выводить по каждому комментарию")
                preview.configure(state="disabled")
                return
            warn.configure(text="")
            self.conf["report_fields"] = [k for k in REPORT_FIELDS if k in fields]
            self.save()
            if self.report_rows:   # начало настоящего отчёта — с теми же итогами и номерами
                text = self.report_text(self.report_rows, fields)
                cut = text[:1200].rsplit("\n", 1)[0] + "\n…" if len(text) > 1200 else text
            else:
                cut = "За выбранный период комментариев нет — пример появится, когда они будут"
            preview.insert("1.0", cut)
            preview.configure(state="disabled")

        update()

    def copy_report_text(self):
        rows = self.report_rows
        if not rows:
            messagebox.showinfo("Отчёт", "Нечего копировать — за выбранный период комментариев нет")
            return
        text = self.report_text(rows)
        self.clipboard_clear()
        self.clipboard_append(text)
        note = ("  ·  длиннее 4096 символов — в одно сообщение Telegram не влезет, лучше «Текст (.txt)»"
                if len(text) > 4096 else "")
        self.statusbar.configure(text=f"Отчёт скопирован — вставьте его в сообщение{note}", text_color="gray")

    def save_report_text(self):
        rows = self.report_rows
        if not rows:
            messagebox.showinfo("Отчёт", "Нечего сохранять — за выбранный период комментариев нет")
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить отчёт", defaultextension=".txt", filetypes=[("Текст", "*.txt")],
            initialfile=f"Комментарии {datetime.now():%Y-%m-%d}.txt")
        if not path:
            return
        try:
            Path(path).write_text(self.report_text(rows), encoding="utf-8-sig")   # BOM — для Блокнота
        except OSError as e:
            messagebox.showerror("Отчёт", f"Не удалось сохранить: {e}")
            return
        if messagebox.askyesno("Отчёт", "Отчёт сохранён. Открыть файл?"):
            open_path(Path(path))

    def clear_report(self):
        """Окно очистки: удалить записи старше дня, недели, месяца, своего срока — или все."""
        if self.clear_dlg and self.clear_dlg.winfo_exists():   # уже открыто — повторный щелчок
            self.clear_dlg.lift()
            self.clear_dlg.focus()
            return
        if not self.report:
            messagebox.showinfo("Отчёт", "Отчёт и так пуст")
            return
        dlg = self.clear_dlg = ctk.CTkToplevel(self)
        dlg.title("Очистить отчёт")
        dlg.resizable(False, False)
        dlg.transient(self)
        ctk.CTkLabel(dlg, text=f"В отчёте записей: {len(self.report)}. Удалить:", anchor="w").pack(
            fill="x", padx=20, pady=(18, 6))
        period = ctk.CTkSegmentedButton(dlg, values=list(CLEAR_PERIODS), command=lambda v: update())
        period.set("Месяца")
        period.pack(anchor="w", padx=20)
        own = ctk.CTkFrame(dlg, fg_color="transparent")
        ctk.CTkLabel(own, text="Старше").pack(side="left")
        amount_var = ctk.StringVar(dlg, "3")   # trace ловит и ввод, и вставку мышью
        amount = ctk.CTkEntry(own, width=70, justify="right", textvariable=amount_var)
        amount.pack(side="left", padx=6)
        amount_var.trace_add("write", lambda *_: update())
        unit = ctk.CTkOptionMenu(own, values=list(CLEAR_UNITS), width=90, command=lambda v: update())
        unit.pack(side="left")
        info = ctk.CTkLabel(dlg, text="", anchor="w", justify="left", wraplength=440)
        btns = ctk.CTkFrame(dlg, fg_color="transparent")   # снизу вверх: кнопки, под ними предупреждение
        btns.pack(side="bottom", fill="x", padx=20, pady=(6, 18))
        ctk.CTkLabel(dlg, justify="left", wraplength=440, text_color="gray", text=(
            "Удалённое не вернуть — при необходимости сначала сохраните отчёт в Excel или текстом. "
            "На комментарии в Telegram очистка не влияет.")).pack(side="bottom", fill="x", padx=20, pady=(12, 0))
        ctk.CTkButton(btns, text="Отмена", width=90, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=dlg.destroy).pack(side="right")
        delete_btn = ctk.CTkButton(btns, text="Удалить", width=170, fg_color=RED, hover_color="#a33")
        delete_btn.pack(side="right", padx=8)
        state = {"before": None}   # граница удаления из update(); None — удалить нечего

        def cutoff() -> datetime | None:
            """Граница: записи раньше неё удаляются. datetime.max — все; None — срок введён с ошибкой."""
            days = CLEAR_PERIODS[period.get()]
            if days is None:   # своё значение
                try:
                    n = float(amount.get().strip().replace(",", "."))
                except ValueError:
                    return None
                if not n >= 0:   # отрицательное и nan
                    return None
                days = n * CLEAR_UNITS[unit.get()]
            elif days == 0:
                return datetime.max
            return datetime.now() - timedelta(days=days)

        def update():
            if period.get() == "Своё":
                own.pack(anchor="w", padx=20, pady=(10, 0), after=period)
            else:
                own.pack_forget()
            before = cutoff()
            if before is None:
                state["before"] = None
                info.configure(text="Введите срок числом, например 3 или 1,5", text_color=RED)
                delete_btn.configure(state="disabled", text="Удалить")
                return
            n = sum(1 for r in self.report if datetime.fromisoformat(r["time"]) < before)
            state["before"] = before if n else None
            if before is datetime.max:
                text = f"Будут удалены все записи: {n}"
            else:
                text = (f"Будут удалены записи до {before:%d.%m.%Y %H:%M}: {n}" if n else
                        f"Записей до {before:%d.%m.%Y %H:%M} нет — удалять нечего")
            info.configure(text=text, text_color=("gray10", "gray90"))
            delete_btn.configure(state="normal" if n else "disabled",
                                 text=f"Удалить {n} шт." if n else "Удалить")

        def do():
            before = state["before"]
            if before is None:
                return
            dlg.destroy()
            try:
                n = core.clear_report(None if before is datetime.max else before)
            except OSError as e:
                messagebox.showerror("Отчёт", f"Не удалось очистить: {e}")
                return
            self.load_report()
            self.statusbar.configure(text=f"Из отчёта удалено записей: {n}", text_color="gray")

        delete_btn.configure(command=do)
        info.pack(fill="x", padx=20, pady=(12, 0), after=period)
        update()
        dlg.update_idletasks()
        dlg.geometry(f"+{self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2}"
                     f"+{self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3}")
        dlg.after(100, dlg.grab_set)   # CTkToplevel показывается не сразу — модальность чуть позже

    def export_report(self):
        rows = self.report_rows
        if not rows:
            messagebox.showinfo("Отчёт", "Нечего выгружать — за выбранный период комментариев нет")
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить отчёт", defaultextension=".csv", filetypes=[("Таблица CSV", "*.csv")],
            initialfile=f"Комментарии {datetime.now():%Y-%m-%d}.csv")
        if not path:
            return
        cols = {"Дата и время": lambda r: datetime.fromisoformat(r["time"]).strftime("%d.%m.%Y %H:%M:%S"),
                "Профиль": lambda r: r.get("profile_name", ""), "Аккаунт": lambda r: r.get("account", ""),
                "Канал": lambda r: r.get("channel", ""), "Пост": lambda r: r.get("post_id", ""),
                "Ссылка": lambda r: r.get("link", ""), "Откуда текст": lambda r: r.get("source", ""),
                "Комментарий": lambda r: r.get("comment", ""), "Картинка": lambda r: r.get("image", ""),
                "Статус": lambda r: r.get("deleted") or "опубликован",
                "Заготовка": lambda r: r.get("placeholder", ""), "Текст поста": lambda r: r.get("post_text", "")}
        def cell(v) -> str:
            # Excel считает формулой всё, что начинается с = + - @ (а «@канал» — у каждого публичного
            # канала): будет #ИМЯ? или, хуже, исполнится формула из чужого поста. Невидимый
            # пробел нулевой ширины в начале делает значение просто текстом
            s = str(v)
            return "\u200b" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s
        try:
            # utf-8-sig и «;» — так CSV сразу правильно открывается в русском Excel
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(cols)
                for r in reversed(rows):   # в файле — по порядку времени
                    w.writerow([cell(get(r)) for get in cols.values()])
        except OSError as e:
            messagebox.showerror("Отчёт", f"Не удалось сохранить: {e}")
            return
        if messagebox.askyesno("Отчёт", f"Сохранено строк: {len(rows)}.\nОткрыть файл?"):
            open_path(Path(path))

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
        """Разбирает задания из фоновых потоков, журнал и события движков. Ошибка в одном
        обработчике пишется в журнал и не останавливает остальные — иначе окно застынет."""
        try:
            while not self.ui_q.empty():
                self.guarded(self.ui_q.get_nowait())
            while not self.log_q.empty():
                self.add_log(self.log_q.get_nowait())
            for e in list(self.engines.values()):
                while not e.events.empty():
                    kind, data = e.events.get_nowait()
                    if e.cfg["id"] in self.engines:   # профиль могли удалить
                        self.guarded(lambda: self.handle(e, kind, data))
        finally:
            self.after(100, self.poll)

    def guarded(self, fn):
        try:
            fn()
        except Exception:
            log.exception("Ошибка в окне программы")

    def report_callback_exception(self, exc, val, tb):
        """Ошибки в обработчиках кнопок tkinter — в журнал и log.txt (в .exe без консоли иначе пропадают)."""
        log.error("Ошибка в окне программы", exc_info=(exc, val, tb))

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
            if data.publish_at:   # автопубликация по таймеру: без звука и уведомлений
                self.render_queue()
                return
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
        elif kind == "report":
            self.report_stale = True
            if self.tabs.get() == "Отчёт":
                self.load_report()

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
        pending = list(self.all_pending().values())
        scheduled = sum(1 for p in pending if p.publish_at)
        waiting = len(pending) - scheduled
        if waiting and not messagebox.askyesno(
                "Выход", f"В очереди {waiting} непроверенных комментариев. Выйти?"):
            return
        if scheduled and not messagebox.askyesno(
                "Выход", f"Ждут автопубликации по таймеру: {scheduled}. "
                         "После выхода они не будут опубликованы. Выйти?"):
            return
        busy = sum(e.busy_auto() for e in self.engines.values())
        if busy and not messagebox.askyesno(
                "Выход", f"Нейросеть ещё пишет комментарии: {busy}. "
                         "После выхода они не будут опубликованы, а уже опубликованные заготовки "
                         "моментального режима не заменятся комментарием нейросети. Выйти?"):
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
