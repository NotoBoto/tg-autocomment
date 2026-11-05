"""Вход в Telegram с облачным паролем (2FA): python -m pytest tests"""
import asyncio
import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.flood_sleep_threshold = 60
        self.threshold_during_call = None

    async def sign_in(self, phone=None, code=None, password=None):
        self.threshold_during_call = self.flood_sleep_threshold
        if self.error:
            raise self.error

    async def get_me(self):
        return SimpleNamespace(first_name="Тестер")

    async def disconnect(self):
        pass


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(core, "list_images", lambda cfg: [])
    e = core.Engine({"id": "pytest_login", "name": "Тест"})
    e._channels_lock = asyncio.Lock()

    async def watch():
        e._watched = {"@channel": object()}

    e._watch_channels = watch
    e._watched_titles = lambda: "@channel"
    yield e
    e.loop.call_soon_threadsafe(e.loop.stop)


def events(engine, kind):
    out = []
    while True:
        try:
            k, data = engine.events.get_nowait()
        except queue.Empty:
            return out
        if k == kind:
            out.append(data)


def test_wrong_password(engine):
    engine.client = FakeClient(errors.PasswordHashInvalidError(request=None))
    engine.sign_in_password("неверный").result(timeout=5)
    msgs = events(engine, "login_error")
    assert len(msgs) == 1 and "Неверный облачный пароль" in msgs[0]
    assert engine.client.threshold_during_call == 0   # FloodWait не ждём молча
    assert engine.client.flood_sleep_threshold == 60   # и вернули как было


def test_flood_wait(engine):
    engine.client = FakeClient(errors.FloodWaitError(request=None, capture=125))
    engine.sign_in_password("пароль").result(timeout=5)
    msgs = events(engine, "login_error")
    assert len(msgs) == 1 and "подождать 3 мин" in msgs[0]
    assert engine.client.flood_sleep_threshold == 60


def test_right_password(engine):
    engine.client = FakeClient()
    engine.sign_in_password("верный").result(timeout=5)
    assert events(engine, "me") == ["Тестер"]


def test_no_client(engine):
    engine.client = None
    assert engine.sign_in_password("пароль") is None


@pytest.mark.parametrize("sec,text", [(40, "40 с"), (60, "1 мин"), (125, "3 мин"),
                                      (3600, "1 ч"), (7500, "2 ч 5 мин")])
def test_wait_text(sec, text):
    assert core.wait_text(sec) == text
