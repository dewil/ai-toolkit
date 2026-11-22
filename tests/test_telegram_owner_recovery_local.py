"""Recovery cadence and durable history pages through the offline ports."""
import asyncio
import json
import types
import unittest
from unittest.mock import patch

from test_telegram_session_owner_fakes import (
    Base, FakeTelegram, import_owner, read_status, refresh_request, run,
    send_request, write_package,
)
from test_telegram_session_owner_mirror import rec, topic_rec, write_mirror
from telegram_owner_telethon import Telegram


class Pages(FakeTelegram):
    def __init__(self):
        super().__init__("a")
        self.history_calls = 0
        self.stall_on = None

    async def history(self, peer, *, after_id, limit):
        self.history_calls += 1
        self._log("history", peer, after_id, limit)
        if self.history_calls == self.stall_on:
            await asyncio.Future()
        return [m for m in self.history_data if m["id"] > after_id][:limit]


class RawPages(Pages):
    """A raw Telegram page can contain filtered services or old topic ids."""
    async def history(self, peer, *, after_id, limit):
        self.history_calls += 1
        self._log("history", peer, after_id, limit)
        if self.history_calls == self.stall_on:
            await asyncio.Future()
        raw = [(mid, row) for mid, row in self.history_data if mid > after_id][:limit]
        # List-compatible adapter pages expose scanning progress when raw ids
        # differ from the exported records' ids.
        return import_owner().HistoryPage(
            [row for _, row in raw if row is not None],
            next_id=raw[-1][0] if raw else after_id, complete=len(raw) < limit)


class Recovery(Base):
    def started(self):
        owner = self.env.make_owner("a")
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def test_send_failure_is_accounted_once_and_never_retried(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                owner = self.started()
                tg, clock = owner.fake.tg, owner.fake.clock
                request = send_request(now=clock.utc())
                request["options"]["no_pace_check"] = True
                write_package(self.env.root("a"), request)

                async def fail(*args):
                    tg._log("send")
                    if timeout:
                        await asyncio.Future()
                    raise import_owner().Disconnected()

                with patch.object(tg, "send", fail), patch.dict(
                        import_owner().RPC_TIMEOUTS, {"send": 0.01}):
                    with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
                        run(owner.step())
                waits = [s for s in logs.output if "OWNER_RECOVERY_WAIT" in s]
                self.assertEqual(len(waits), 1, waits)
                self.assertTrue(waits[0].endswith("retry_in=15"), waits)
                self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "unknown")
                connects = tg.names().count("connect")
                clock.advance(14)
                run(owner.step())
                self.assertEqual(tg.names().count("connect"), connects)
                clock.advance(1)
                run(owner.step())
                self.assertEqual(tg.names().count("connect"), connects + 1)
                self.assertEqual(len(tg.sends()), 1)
                run(owner.close())

    def test_two_minutes_are_elapsed_time_not_failure_count(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        tg.raises["events"] = import_owner().Disconnected()
        run(owner.step())
        clock.advance(120)
        tg.raises["connect"] = import_owner().Disconnected()
        with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
            run(owner.step())
        self.assertTrue(any(s.endswith("retry_in=30") for s in logs.output), logs.output)

    def test_ready_without_successful_step_preserves_outage_and_backoff(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        tg.raises["events"] = import_owner().Disconnected()
        run(owner.step())
        clock.advance(120)
        # connect/catch-up reach ready, but failed work continues the outage.
        tg.raises["events"] = import_owner().Disconnected()
        run(owner.step())
        for delay in (30, 60, 120, 240, 300, 300):
            connects = tg.names().count("connect")
            clock.advance(delay - 1)
            run(owner.step())
            self.assertEqual(tg.names().count("connect"), connects)
            tg.raises["events"] = import_owner().Disconnected()
            clock.advance(1)
            run(owner.step())
            self.assertEqual(tg.names().count("connect"), connects + 1)


class DurablePages(Base):
    def setUp(self):
        super().setUp()
        self.path = self.env.projects / "chat1" / "result.json"
        self.dest = self.env.destination("d1", 1001, "chat1")

    def owner(self, tg, **kwargs):
        owner = self.env.make_owner("a", tg=tg, destinations=kwargs.pop("destinations", [self.dest]), **kwargs)
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def test_page_timeout_resumes_durable_cursor_and_preserves_bootstrap_boundary(self):
        tg = Pages()
        tg.history_data = [rec(i) for i in range(1, 251)]
        tg.stall_on = 3
        owner = self.owner(tg)
        with patch.dict(import_owner().RPC_TIMEOUTS, {"history": 0.01}):
            self.assertEqual(run(owner.start())["code"], "OWNER_UNAVAILABLE")
        self.assertEqual([m["id"] for m in json.loads(self.path.read_bytes())["messages"]], list(range(1, 201)))
        self.assertNotIn("recent", tg.names())
        before = len(tg.calls)
        owner = self.owner(tg)
        self.assertEqual(run(owner.start())["code"], "READY")
        history = [c for c in tg.calls[before:] if c[0] == "history"]
        self.assertEqual(history[0][2:], (200, 100))
        tg.history_data.append(rec(251))
        request = refresh_request(now=owner.fake.clock.utc())
        write_package(self.env.root("a"), request)
        run(owner.step())
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "refreshed")
        prev = json.loads(self.path.with_name("result.prev.json").read_bytes())
        self.assertEqual([m["id"] for m in prev["messages"]], list(range(1, 251)))

    def test_each_page_resets_watchdog_even_when_whole_catchup_exceeds_budget(self):
        tg = Pages()
        tg.history_data = [rec(i) for i in range(1, 351)]
        owner = self.owner(tg)
        clock = owner.fake.clock
        exits = []
        watchdog = import_owner().HeartbeatWatchdog(clock, terminate=exits.append)
        owner._watchdog = watchdog

        def page():
            clock.advance(110)
            watchdog.check()

        tg.hooks["history"] = page
        began = clock.monotonic()
        self.assertEqual(run(owner.start())["code"], "READY")
        self.assertGreater(clock.monotonic() - began, 300)
        self.assertEqual(exits, [])
        self.assertEqual([c[2:] for c in tg.calls if c[0] == "history"],
                         [(0, 100), (100, 100), (200, 100), (300, 100)])

    def test_shared_peer_pages_preserve_order_dedup_and_recent_window(self):
        second = self.env.projects / "chat2" / "result.json"
        write_mirror(self.path, range(1, 121))
        write_mirror(second, range(1, 51))
        tg = Pages()
        tg.history_data = [rec(i) for i in range(1, 251)]
        tg.recent_data = [rec(i) for i in range(201, 251) if i != 225]
        tg.recent_data[0] = rec(201, "edited", edited_unixtime="1900000000")
        owner = self.owner(tg, destinations=[self.dest, self.env.destination("d2", 1001, "chat2")])
        self.assertEqual(run(owner.start())["code"], "READY")
        self.assertEqual([c[2:] for c in tg.calls if c[0] == "history"],
                         [(50, 100), (150, 100), (250, 100)])
        self.assertEqual(tg.names().count("recent"), 1)
        for path in (self.path, second):
            messages = json.loads(path.read_bytes())["messages"]
            self.assertEqual([m["id"] for m in messages], list(range(1, 251)))
            self.assertEqual(messages[200]["text"], "edited")
            self.assertEqual(messages[200]["edit_history"][0]["text"], "msg 201")
            self.assertEqual([m["id"] for m in messages if m.get("deleted")], [225])

    def test_topic_rename_after_page_boundary_survives_restart(self):
        tg = RawPages()
        tg.history_data = [(1, {"kind": "topic", "record": topic_rec(1), "revision": [1, 0]})]
        tg.history_data += [(i, rec(i)) for i in range(2, 101)]
        tg.history_data += [(101, {"kind": "topic", "record": {"id": 1, "title": "new"}, "revision": [2, 0]})]
        tg.history_data += [(i, rec(i)) for i in range(102, 202)]
        tg.stall_on = 2
        owner = self.owner(tg)
        with patch.dict(import_owner().RPC_TIMEOUTS, {"history": 0.01}):
            self.assertEqual(run(owner.start())["code"], "OWNER_UNAVAILABLE")
        owner = self.owner(tg)
        self.assertEqual(run(owner.start())["code"], "READY")
        self.assertEqual(json.loads(self.path.read_bytes())["topics"][0]["title"], "new")

    def test_filtered_page_is_checkpointed_before_next_page_timeout(self):
        tg = RawPages()
        tg.history_data = [(i, rec(i) if i <= 100 or i > 200 else None) for i in range(1, 251)]
        tg.stall_on = 3
        owner = self.owner(tg)
        with patch.dict(import_owner().RPC_TIMEOUTS, {"history": 0.01}):
            self.assertEqual(run(owner.start())["code"], "OWNER_UNAVAILABLE")
        before = len(tg.calls)
        owner = self.owner(tg)
        self.assertEqual(run(owner.start())["code"], "READY")
        self.assertEqual([c[2] for c in tg.calls[before:] if c[0] == "history"], [200])
        self.assertEqual([m["id"] for m in json.loads(self.path.read_bytes())["messages"]],
                         list(range(1, 101)) + list(range(201, 251)))


class AdapterPages(Base):
    def test_filtered_service_messages_do_not_end_history_before_its_tail(self):
        telegram = Telegram.__new__(Telegram)
        telegram._entity = lambda peer: None
        telegram.errors = types.SimpleNamespace(FloodWaitError=type("RpcFloodWait", (Exception,), {}))
        messages = [types.SimpleNamespace(id=i, action="service" if i % 2 else None)
                    for i in range(1, 206)]
        reads = []

        async def iterate(entity, *, min_id, limit, **kwargs):
            reads.append((min_id, limit))
            for msg in [m for m in messages if m.id > min_id][:limit]:
                yield msg

        async def record(msg):
            return rec(msg.id) if msg.action is None else None

        async def media(*args):
            pass

        telegram.client = types.SimpleNamespace(iter_messages=iterate)
        telegram._record, telegram._media = record, media
        tg = FakeTelegram("a")
        tg.history = telegram.history
        owner = self.env.make_owner("a", tg=tg, destinations=[self.env.destination("d1", 1001, "chat1")])
        self.addCleanup(lambda: run(owner.close()))
        self.assertEqual(run(owner.start())["code"], "READY")
        path = self.env.projects / "chat1" / "result.json"
        self.assertEqual([m["id"] for m in json.loads(path.read_bytes())["messages"]], list(range(2, 205, 2)))
        self.assertEqual(reads, [(0, 100), (100, 100), (200, 100)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
