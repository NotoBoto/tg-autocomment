"""Опрос каналов при моментальной публикации: python -m pytest tests"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

CHAN = types.Channel(id=111, title="Канал", photo=types.ChatPhotoEmpty(), date=None, access_hash=5,
                     username="chan")
PID = -1000000000111


def post(msg_id, age=1):
    m = types.Message(id=msg_id, peer_id=types.PeerChannel(111), message=f"пост {msg_id}",
                      date=datetime.now(timezone.utc) - timedelta(seconds=age))
    return m


class FakeClient:
    def __init__(self):
        self.top = 10
        self.msgs = {}
        self.history_calls = 0

    async def __call__(self, req, flood_sleep_threshold=None):
        return SimpleNamespace(dialogs=[SimpleNamespace(peer=types.PeerChannel(111), top_message=self.top)])

    async def get_messages(self, ent, min_id, limit):
        self.history_calls += 1
        return sorted((m for i, m in self.msgs.items() if i > min_id), key=lambda m: -m.id)[:limit]


@pytest.fixture
def engine(monkeypatch):
    e = core.Engine({"id": "pytest_poll", "name": "Тест", "poll_channels": True, "publish_mode": "instant",
                     "confirm_before_post": False})
    e.client = FakeClient()
    e._watched = {"@chan": CHAN}
    handled = []

    async def handle(msg, manual, polled=False):
        handled.append((msg.id, polled))
    monkeypatch.setattr(e, "_handle_post", handle)
    e.handled = handled
    return e


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_poll_takes_only_new_posts_once(engine):
    async def go():
        last = {}
        await engine._poll_once(last, report_missing=True)   # запомнили, что уже вышло
        assert last == {PID: 10} and engine.client.history_calls == 0
        engine.client.top = 12
        engine.client.msgs = {11: post(11), 12: post(12)}
        await engine._poll_once(last, report_missing=False)
        await asyncio.sleep(0)
        assert engine.handled == [(11, True), (12, True)]
        await engine._poll_once(last, report_missing=False)   # ничего нового
        await asyncio.sleep(0)
        assert len(engine.handled) == 2
        # Уведомление о том же посте опоздало — второй раз не берём
        await engine._on_new_post(SimpleNamespace(message=post(12)))
        assert len(engine.handled) == 2
    run(go())


def test_push_first_then_poll_skips(engine):
    async def go():
        last = {}
        await engine._poll_once(last, report_missing=True)
        await engine._on_new_post(SimpleNamespace(message=post(11)))
        assert engine.handled == [(11, False)]
        engine.client.top = 11
        engine.client.msgs = {11: post(11)}
        await engine._poll_once(last, report_missing=False)
        await asyncio.sleep(0)
        assert engine.handled == [(11, False)]
    run(go())


def test_old_posts_after_outage_are_skipped(engine):
    async def go():
        last = {}
        await engine._poll_once(last, report_missing=True)
        engine.client.top = 11
        engine.client.msgs = {11: post(11, age=3600)}
        await engine._poll_once(last, report_missing=False)
        await asyncio.sleep(0)
        assert engine.handled == []
    run(go())


def test_polling_only_with_instant_auto(engine):
    assert engine._polling_on()
    engine.cfg["publish_mode"] = "delayed"
    assert not engine._polling_on()
    engine.cfg["publish_mode"] = "instant"
    engine.cfg["confirm_before_post"] = True
    assert not engine._polling_on()


def test_pace_slows_on_flood_and_recovers():
    p = core.PollPace(0.5)
    assert p.flood(0) == 1.0
    assert p.flood(1) == 2.0
    assert not p.calm(1 + p.CALM_SEC - 1)     # ещё рано
    assert p.calm(1 + p.CALM_SEC) and p.cur == 1.0
    assert p.calm(1 + 2 * p.CALM_SEC) and p.cur == 0.5
    assert not p.calm(1 + 3 * p.CALM_SEC) and p.cur == 0.5   # ниже заданного не опускаемся


def test_pace_caps_and_resets_on_new_setting():
    p = core.PollPace(0.2)
    for _ in range(20):
        p.flood(0)
    assert p.cur == p.MAX_SEC
    p.set_base(1)
    assert p.cur == 1 and p.base == 1
    big = core.PollPace(120)   # заданный больше потолка — реже заданного не станет и чаще тоже
    assert big.flood(0) == 120


def test_interval_below_one_second_allowed(engine):
    engine.cfg["poll_interval_sec"] = 0.3
    assert engine._poll_interval() == 0.3
    engine.cfg["poll_interval_sec"] = 0.1
    assert engine._poll_interval() == 0.1
    engine.cfg["poll_interval_sec"] = 0.01
    assert engine._poll_interval() == core.POLL_MIN_SEC
