#!/usr/bin/env python3
"""RED: overflow ведет только к догону зеркал, без переподключения (решение ведущего 22.11).

1. Overflow не вызывает disconnect/connect; ожидающий пакет уходит в пределах
   нескольких шагов (без паузы 15 с). Сетевой сбой по-прежнему ведет к disconnect+connect.
2. Адаптер: короткие обновления (UpdateShortMessage/UpdateShortChatMessage) дают
   overflow только для зеркалируемого peer; UpdatesTooLong/ChannelTooLong - overflow всегда.
3. Причина перехода в degraded видна в журнале telegram_session_owner:
   overflow - слово "overflow"; Disconnected - "DISCONNECTED" или "network".

Запуск: python3 tests/test_telegram_owner_overflow.py
"""
from __future__ import annotations

import sys
import types
import unittest
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeTelegram, NOW, import_owner, read_status, run, send_request, write_package,
)
from test_telegram_session_owner_mirror import rec, write_mirror  # noqa: E402
from telegram_owner_telethon import Telegram  # noqa: E402

OVERFLOW = {"kind": "overflow", "peer": None, "record": None, "ids": [], "revision": [NOW, 1]}
STEPS = 10  # "несколько шагов", шаг = 0.1 с фейковых часов (< 15 с паузы переподключения)


class OverflowOwner(Base):
    def started(self, tg):
        dest = self.env.destination("d1", 1001, "chat1")
        o = self.env.make_owner("a", tg=tg, destinations=[dest])
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        return o

    def test_overflow_does_not_reconnect_and_catches_up(self):
        mirror = self.env.projects / "chat1" / "result.json"
        write_mirror(mirror, [100])
        tg = FakeTelegram("a")
        o = self.started(tg)
        clock = o.fake.clock
        mark = len(tg.calls)
        tg.history_data = [rec(101)]
        tg.event_batches = [[dict(OVERFLOW)]]
        for _ in range(STEPS):
            run(o.step())
            clock.advance(0.1)
        after = [c[0] for c in tg.calls[mark:]]
        self.assertNotIn("disconnect", after, after)
        self.assertNotIn("connect", after, after)
        self.assertIn("history", after, "после overflow нет догона")

    def test_queued_send_goes_out_within_a_few_steps_after_overflow(self):
        tg = FakeTelegram("a")
        o = self.started(tg)
        clock, root = o.fake.clock, self.env.root("a")
        tg.event_batches = [[dict(OVERFLOW)]]
        req = send_request("a", peer="me", text="Привет", now=clock.utc())
        req["options"]["no_pace_check"] = True
        write_package(root, req)
        for _ in range(STEPS):
            run(o.step())
            clock.advance(0.1)
        self.assertNotIn("disconnect", tg.names(), "переподключение из-за overflow")
        self.assertNotIn("connect", tg.names()[1:], "переподключение из-за overflow")
        self.assertEqual(len(tg.sends()), 1, f"пакет не отправлен за {STEPS} шагов; calls={tg.names()}")
        st = read_status(root, req["request_id"])
        self.assertEqual(st["state"], "sent", st)

    def test_network_failure_still_disconnects_and_connects(self):
        tg = FakeTelegram("a")
        o = self.started(tg)
        mark = len(tg.calls)
        tg.raises["events"] = import_owner().Disconnected()
        for _ in range(3):
            run(o.step())
            o.fake.clock.advance(15)
        after = [c[0] for c in tg.calls[mark:]]
        self.assertIn("disconnect", after, after)
        self.assertIn("connect", after, after)


class DegradedReason(Base):
    def degraded_lines(self, tg, setup):
        o = self.env.make_owner("a", tg=tg)
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        setup(tg)
        with self.assertLogs("telegram_session_owner", level="DEBUG") as logs:
            for _ in range(3):
                run(o.step())
                o.fake.clock.advance(0.1)
        return [s for s in logs.output if "OWNER_STATE" in s and "degraded" in s.lower()]

    def test_overflow_reason_in_degraded_transition(self):
        lines = self.degraded_lines(FakeTelegram("a"), lambda tg: tg.event_batches.append([dict(OVERFLOW)]))
        self.assertTrue(lines, "нет записи перехода в degraded после overflow")
        self.assertTrue(any("overflow" in s.lower() for s in lines), lines)

    def test_disconnected_reason_in_degraded_transition(self):
        def setup(tg):
            tg.raises["events"] = import_owner().Disconnected()
        lines = self.degraded_lines(FakeTelegram("a"), setup)
        self.assertTrue(lines, "нет записи перехода в degraded после Disconnected")
        self.assertTrue(any("DISCONNECTED" in s or "network" in s.lower() for s in lines), lines)

    def test_exception_text_not_logged(self):
        def setup(tg):
            tg.raises["events"] = import_owner().Disconnected("SECRET-DETAIL-42")
        o = self.env.make_owner("a", tg=FakeTelegram("a"))
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        setup(o.fake.tg)
        with self.assertLogs("telegram_session_owner", level="DEBUG") as logs:
            run(o.step())
        self.assertNotIn("SECRET-DETAIL-42", "".join(logs.output))


UPDATE_NAMES = ("UpdateDeleteChannelMessages", "UpdateDeleteMessages", "UpdateNewMessage",
                "UpdateNewChannelMessage", "UpdateEditMessage", "UpdateEditChannelMessage",
                "UpdatesTooLong", "UpdateChannelTooLong", "UpdateShortMessage", "UpdateShortChatMessage")


class AdapterShortUpdates(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        a = Telegram.__new__(Telegram)
        a.account = "a"
        a.client = types.SimpleNamespace(is_connected=lambda: True)
        a.types = types.SimpleNamespace(**{n: type(n, (), {}) for n in UPDATE_NAMES})
        a.config = {"destinations": [{"destination_id": "d1", "project_root": "/PROJECT",
                                      "peer": 1, "project_config": {}}]}
        a._aliases = {1: {"account": "a", "kind": "User", "id": 1}}
        a._overflow, a._seq = False, 0
        self.a = a

    async def events_for(self, update):
        self.a._updates = deque([update])
        return await self.a.events()

    def short(self, name, **attrs):
        u = getattr(self.a.types, name)()
        for k, v in attrs.items():
            setattr(u, k, v)
        return u

    def kinds(self, result):
        return [e.get("kind") for e in result]

    async def test_updates_too_long_is_overflow(self):
        self.assertIn("overflow", self.kinds(await self.events_for(self.short("UpdatesTooLong"))))

    async def test_channel_too_long_is_overflow(self):
        u = self.short("UpdateChannelTooLong", channel_id=5)
        self.assertIn("overflow", self.kinds(await self.events_for(u)))

    async def test_short_message_from_mirrored_peer_is_overflow(self):
        u = self.short("UpdateShortMessage", user_id=1, id=10)
        self.assertIn("overflow", self.kinds(await self.events_for(u)))

    async def test_short_message_from_foreign_peer_is_ignored(self):
        u = self.short("UpdateShortMessage", user_id=999, id=10)
        result = await self.events_for(u)
        self.assertNotIn("overflow", self.kinds(result), result)

    async def test_short_chat_message_from_foreign_chat_is_ignored(self):
        u = self.short("UpdateShortChatMessage", chat_id=777, from_id=999, id=10)
        result = await self.events_for(u)
        self.assertNotIn("overflow", self.kinds(result), result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
