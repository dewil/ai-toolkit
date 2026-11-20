#!/usr/bin/env python3
"""Offline startup regressions: dialog FloodWait and unavailable Telegram."""
import asyncio
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from telegram_owner_telethon import Telegram
from telegram_session_owner import Disconnected, FloodWait
from test_telegram_session_owner_fakes import Base, run, write_file


class RpcFloodWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class DialogClient:
    """Model a paginated async generator and Telethon's RPC retry settings."""
    def __init__(self, seconds=25, *, error=None, request_retries=0):
        self.flood_sleep_threshold = 0
        self._request_retries = request_retries
        self.pending_wait = seconds
        self.error = error
        self.settings = []
        self.sleeps = []
        self.disconnected = False
        self.entities = [types.SimpleNamespace(id=123), types.SimpleNamespace(id=456)]
        self.yielded = []

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        self.disconnected = True

    async def iter_dialogs(self):
        self.settings.append((self.flood_sleep_threshold, self._request_retries))
        self.yielded.append(self.entities[0])
        yield types.SimpleNamespace(entity=self.entities[0])
        if self.pending_wait is not None:
            seconds = self.pending_wait
            if seconds > self.flood_sleep_threshold:
                raise RpcFloodWait(seconds)
            self.sleeps.append(seconds)
            # Telethon can sleep with zero retries, but still raises afterwards.
            if self._request_retries == 0:
                raise RpcFloodWait(seconds)
        if self.error is not None:
            raise self.error
        self.yielded.append(self.entities[1])
        yield types.SimpleNamespace(entity=self.entities[1])


def adapter(client):
    # Same adapter seam as test_telegram_owner_cli_local; no auth/session IO.
    telegram = Telegram.__new__(Telegram)
    telegram.client = client
    telegram.errors = types.SimpleNamespace(FloodWaitError=RpcFloodWait)
    telegram._dialogs, telegram._uploads = {}, {}
    return telegram


class DialogStartup(unittest.TestCase):
    def test_dialog_loading_restores_settings_after_success(self):
        for seconds in (None, 25, 120):
            for retries in (0, 7):
                with self.subTest(seconds=seconds, retries=retries):
                    client = DialogClient(seconds, request_retries=retries)
                    telegram = adapter(client)
                    run(telegram.connect())
                    self.assertEqual(client.sleeps, [] if seconds is None else [seconds])
                    self.assertEqual(client.settings, [(120, 3)])
                    self.assertEqual(telegram._dialogs, {e.id: e for e in client.entities})
                    self.assertEqual(client.flood_sleep_threshold, 0)
                    self.assertEqual(client._request_retries, retries)

    def test_long_flood_wait_keeps_seconds_and_restores_settings(self):
        for retries in (0, 7):
            with self.subTest(retries=retries):
                client = DialogClient(121, request_retries=retries)
                with self.assertRaises(FloodWait) as error:
                    run(adapter(client).connect())
                self.assertEqual(error.exception.seconds, 121)
                self.assertEqual(client.sleeps, [])
                self.assertEqual(client.settings, [(120, 3)])
                self.assertEqual(client.flood_sleep_threshold, 0)
                self.assertEqual(client._request_retries, retries)

    def test_iterator_flood_wait_propagates_after_partial_page(self):
        for seconds in (25, 120):
            for retries in (0, 7):
                with self.subTest(seconds=seconds, retries=retries):
                    client = DialogClient(None, error=RpcFloodWait(seconds),
                                          request_retries=retries)
                    with self.assertRaises(FloodWait) as error:
                        run(adapter(client).connect())
                    self.assertEqual(error.exception.seconds, seconds)
                    self.assertEqual(client.yielded, client.entities[:1])
                    self.assertEqual(client.settings, [(120, 3)])
                    self.assertEqual(client.flood_sleep_threshold, 0)
                    self.assertEqual(client._request_retries, retries)

    def test_dialog_failure_and_cancellation_restore_settings(self):
        for cause, expected in ((OSError("offline"), Disconnected),
                                (asyncio.CancelledError(), asyncio.CancelledError)):
            for retries in (0, 7):
                with self.subTest(cause=type(cause).__name__, retries=retries):
                    client = DialogClient(None, error=cause, request_retries=retries)
                    with self.assertRaises(expected):
                        run(adapter(client).connect())
                    self.assertEqual(client.settings, [(120, 3)])
                    self.assertEqual(client.flood_sleep_threshold, 0)
                    self.assertEqual(client._request_retries, retries)


class OwnerStartup(Base):
    def test_partial_dialog_flood_wait_returns_unavailable_and_releases_locks(self):
        for seconds in (25, 120, 121):
            for retries in (0, 7):
                with self.subTest(seconds=seconds, retries=retries):
                    client = DialogClient(None, error=RpcFloodWait(seconds),
                                          request_retries=retries)
                    owner = self.env.make_owner("a", tg=adapter(client))
                    owner._catch_up = AsyncMock()
                    with patch.object(owner, "_heartbeat", wraps=owner._heartbeat) as heartbeat:
                        self.assertEqual(run(owner.start()),
                                         {"code": "OWNER_UNAVAILABLE", "exit_code": 7})
                    owner._catch_up.assert_not_awaited()
                    self.assertNotIn(call("ready"), heartbeat.call_args_list)
                    self.assertFalse(owner._started)
                    self.assertEqual(client.yielded, client.entities[:1])
                    self.assertEqual(client.settings, [(120, 3)])
                    self.assertTrue(client.disconnected)
                    self.assertEqual(client.flood_sleep_threshold, 0)
                    self.assertEqual(client._request_retries, retries)
                    replacement = self.env.make_owner("a")
                    try:
                        self.assertEqual(run(replacement.start())["code"], "READY")
                    finally:
                        run(replacement.close())

    def test_disconnected_start_returns_unavailable(self):
        owner = self.env.make_owner("a")
        owner.fake.tg.raises["connect"] = Disconnected()
        self.assertEqual(run(owner.start()), {"code": "OWNER_UNAVAILABLE", "exit_code": 7})
        self.assertIn("disconnect", owner.fake.tg.names())

    def test_malformed_config_remains_invalid_before_factory(self):
        write_file(self.env.registry_path, "{broken json")
        owner = self.env.make_owner("a")
        self.assertEqual(run(owner.start()), {"code": "INVALID_CONFIG", "exit_code": 2})
        self.assertEqual(owner.fake.factory_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
