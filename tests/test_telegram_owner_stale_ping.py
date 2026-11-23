#!/usr/bin/env python3
"""RED: перед connect() при переподключении владелец сбрасывает незавершенный
ping отправителя Telethon (client._sender._ping), иначе первая же keepalive-
проверка рвет новое соединение (docs/telegram-session-owner.md,
"Зависший keepalive после разрыва").

Клиент здесь - фейк Telethon; значение _ping на момент connect() запоминается.
Запуск: python3 tests/test_telegram_owner_stale_ping.py
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import Base, import_owner, run  # noqa: E402
from telegram_owner_telethon import Telegram  # noqa: E402

STALE = 987654321


class PingClient:
    """Фейковый TelegramClient: _sender._ping выставляется снаружи (обрыв без pong)."""
    def __init__(self, sender="present"):
        if sender == "present":
            self._sender = types.SimpleNamespace(_ping=None)
        elif sender == "no_ping_attr":
            self._sender = types.SimpleNamespace()
        # sender == "absent": поля _sender нет вовсе
        self.ping_at_connect = []
        self.connects = 0
        self.flood_sleep_threshold = 0
        self._request_retries = 5

    async def connect(self):
        self.connects += 1
        s = getattr(self, "_sender", None)
        self.ping_at_connect.append(getattr(s, "_ping", "<no field>"))

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        pass  # как в Telethon: _ping не трогает

    async def iter_dialogs(self):
        return
        yield  # pragma: no cover


def adapter(client):
    t = Telegram.__new__(Telegram)
    t.client = client
    t.errors = types.SimpleNamespace(FloodWaitError=type("FW", (Exception,), {}))
    t._dialogs, t._uploads = {}, {}
    return t


class StalePing(Base):
    def test_stale_ping_cleared_before_connect_on_reconnect(self):
        client = PingClient()
        owner = self.env.make_owner("a", tg=adapter(client))
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        mod = import_owner()
        client._sender._ping = STALE
        tg = owner.fake.tg
        async def down():
            raise mod.Disconnected()
        tg.events = down
        run(owner.step())
        owner.fake.clock.advance(15)
        del tg.events
        before = client.connects
        run(owner.step())
        self.assertGreater(client.connects, before, "переподключения не было")
        self.assertIsNone(client.ping_at_connect[-1],
                          f"на момент connect() _sender._ping={client.ping_at_connect[-1]!r}")

    def test_client_without_sender_or_ping_reconnects_without_error(self):
        for kind in ("absent", "no_ping_attr"):
            with self.subTest(sender=kind):
                client = PingClient(kind)
                owner = self.env.make_owner("a", tg=adapter(client))
                self.assertEqual(run(owner.start())["code"], "READY")
                mod = import_owner()

                async def down():
                    raise mod.Disconnected()
                tg = owner.fake.tg
                tg.events = down
                run(owner.step())
                owner.fake.clock.advance(15)
                del tg.events
                before = client.connects
                run(owner.step())
                self.assertGreater(client.connects, before)
                run(owner.close())


if __name__ == "__main__":
    unittest.main(verbosity=2)
