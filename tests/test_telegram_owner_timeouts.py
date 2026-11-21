#!/usr/bin/env python3
"""Offline regression tests for stalled RPCs and the process watchdog."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

from test_telegram_session_owner_fakes import (
    Base, FakeClock, import_owner, read_status, refresh_request, run,
    send_request, write_package,
)


class Timeouts(Base):
    def setUp(self):
        super().setUp()
        self.mod = import_owner()
        self.budgets = patch.object(self.mod, "RPC_TIMEOUTS", dict.fromkeys(
            ("connect", "disconnect", "resolve", "history", "recent", "events",
             "upload", "send", "scheduled"), 0.01), create=True)
        self.budgets.start()
        self.addCleanup(self.budgets.stop)

    def started(self):
        owner = self.env.make_owner("a", destinations=[self.env.destination()])
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    def hung(self, tg, operation):
        async def never(*args, **kwargs):
            tg._log(operation)
            await asyncio.Future()
        return patch.object(tg, operation, never)

    def step(self, owner):
        # The outer deadline makes a missing production deadline fail quickly.
        return run(asyncio.wait_for(owner.step(), timeout=0.5))

    def test_hung_events_times_out_degrades_and_recovers(self):
        owner = self.started()
        with self.hung(owner.fake.tg, "events"):
            with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
                self.step(owner)
        self.assertEqual(self.state(), "degraded")
        self.assertTrue(any("OWNER_RPC_TIMEOUT operation=events" in s for s in logs.output))
        owner.fake.clock.advance(15)
        self.step(owner)
        self.assertEqual(self.state(), "ready")
        self.assertFalse(owner._stepping)

    def test_hung_recovery_calls_remain_degraded_then_recover(self):
        owner = self.started()
        for operation in ("connect", "history", "recent"):
            with self.subTest(operation=operation):
                owner.fake.tg.raises["events"] = self.mod.Disconnected()
                self.step(owner)
                owner.fake.clock.advance(15)
                with self.hung(owner.fake.tg, operation):
                    self.step(owner)
                self.assertEqual(self.state(), "degraded")
                owner.fake.clock.advance(30)
                self.step(owner)
                self.assertEqual(self.state(), "ready")

    def check_reconnect_after_failed_disconnect(self, *, hung):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        tg.raises["events"] = self.mod.Disconnected()
        self.step(owner)
        clock.advance(15)
        tg.calls.clear()
        for operation in ("connect", "history", "recent"):
            tg.hooks[operation] = lambda: self.assertEqual(self.state(), "degraded")
        if hung:
            failure = self.hung(tg, "disconnect")
        else:
            async def raising():
                tg._log("disconnect")
                raise OSError("private-disconnect-diagnostic")
            failure = patch.object(tg, "disconnect", raising)
        with failure, self.assertLogs("telegram_session_owner", level="WARNING") as logs:
            self.step(owner)
        self.assertEqual(tg.names()[:4], ["disconnect", "connect", "history", "recent"])
        self.assertEqual(self.state(), "ready")
        self.assertTrue(any(s.endswith("OWNER_RECONNECT_DISCONNECT_FAILED") for s in logs.output))
        self.assertNotIn("private-disconnect-diagnostic", "\n".join(logs.output))
        if hung:
            self.assertTrue(any("OWNER_RPC_TIMEOUT operation=disconnect" in s for s in logs.output))

    def test_raising_disconnect_does_not_block_reconnect(self):
        self.check_reconnect_after_failed_disconnect(hung=False)

    def test_hung_disconnect_does_not_block_reconnect(self):
        self.check_reconnect_after_failed_disconnect(hung=True)

    def test_hung_refresh_is_queued_and_cannot_publish_partial_checkpoint(self):
        owner = self.started()
        request = refresh_request(now=owner.fake.clock.utc())
        write_package(self.env.root("a"), request)
        with self.hung(owner.fake.tg, "history"):
            self.step(owner)
        self.assertEqual(self.state(), "degraded")
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "queued")
        owner.fake.clock.advance(15)
        self.step(owner)
        self.assertEqual(self.state(), "ready")
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "refreshed")

    def test_hung_send_is_unknown_and_never_retried(self):
        owner = self.started()
        request = send_request(now=owner.fake.clock.utc())
        request["options"]["no_pace_check"] = True
        write_package(self.env.root("a"), request)
        with self.hung(owner.fake.tg, "send"):
            self.step(owner)
        status = read_status(self.env.root("a"), request["request_id"])
        self.assertEqual((status["state"], status["code"]), ("unknown", "SEND_UNCERTAIN"))
        self.assertEqual(self.state(), "degraded")
        owner.fake.clock.advance(15)
        for _ in range(3):
            self.step(owner)
        self.assertEqual(self.state(), "ready")
        self.assertEqual(len(owner.fake.tg.sends()), 1)

    def test_hung_resolve_is_queued_before_send_and_can_retry(self):
        owner = self.started()
        request = send_request(now=owner.fake.clock.utc())
        request["options"]["no_pace_check"] = True
        write_package(self.env.root("a"), request)
        with self.hung(owner.fake.tg, "resolve"):
            self.step(owner)
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "queued")
        self.assertEqual(owner.fake.tg.sends(), [])
        owner.fake.clock.advance(15)
        self.step(owner)
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "sent")
        self.assertEqual(len(owner.fake.tg.sends()), 1)

    def test_hung_schedule_verification_keeps_ids_unknown_without_resend(self):
        owner = self.started()
        request = send_request(now=owner.fake.clock.utc())
        request["options"]["no_pace_check"] = True
        request["schedule"]["at"] = owner.fake.clock.utc() + 600
        owner.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [900],
                                       "at": request["schedule"]["at"]}]
        write_package(self.env.root("a"), request)
        with self.hung(owner.fake.tg, "scheduled"):
            self.step(owner)
        status = read_status(self.env.root("a"), request["request_id"])
        self.assertEqual(status["state"], "unknown")
        self.assertEqual(status["result"]["scheduled_ids"], [900])
        self.assertEqual(self.state(), "degraded")
        owner.fake.clock.advance(15)
        self.step(owner)
        self.assertEqual(len(owner.fake.tg.sends()), 1)

    def test_hung_start_returns_unavailable_and_unlocks(self):
        owner = self.env.make_owner("a")
        with self.hung(owner.fake.tg, "connect"):
            reply = run(asyncio.wait_for(owner.start(), timeout=0.5))
        self.assertEqual(reply, {"code": "OWNER_UNAVAILABLE", "exit_code": 7})
        self.assertEqual(owner._locks, [])

    def test_hung_close_is_bounded_and_unlocks(self):
        owner = self.started()
        with self.hung(owner.fake.tg, "disconnect"):
            with self.assertRaises(self.mod.Disconnected):
                run(asyncio.wait_for(owner.close(), timeout=0.5))
        self.assertEqual(owner._locks, [])

    def test_fast_start_failure_pauses_before_systemd_restart(self):
        owner = self.env.make_owner("a")
        clock = owner.fake.clock
        starts, delays = [], []
        original_wait_for = asyncio.wait_for

        class Stop:
            def is_set(self):
                return False

            async def wait(self):
                await asyncio.Future()

        async def timed_wait(awaitable, timeout):
            if timeout > 1:  # Only the restart pause; RPC budgets are 0.01.
                awaitable.close()
                delays.append(timeout)
                clock.advance(timeout)
                raise asyncio.TimeoutError
            return await original_wait_for(awaitable, timeout=timeout)

        with patch.object(asyncio, "wait_for", timed_wait):
            for _ in range(8):
                starts.append(clock.monotonic())
                owner.fake.tg.raises["connect"] = self.mod.Disconnected()
                self.assertEqual(run(self.mod._serve(owner, Stop(), Mock())), 7)
                clock.advance(15)  # Unit's RestartSec.
        self.assertEqual(delays, [60] * 8)
        for start in starts:
            self.assertLessEqual(sum(start <= s < start + 300 for s in starts), 5)

    def test_serve_keeps_watchdog_armed_during_hung_step(self):
        owner = self.env.make_owner("a")
        exits = []
        watchdog = self.mod.HeartbeatWatchdog(owner.fake.clock, terminate=exits.append)
        stop = asyncio.Event()

        async def stuck_step():
            owner.fake.clock.advance(300)
            watchdog.check()
            stop.set()

        owner.step = stuck_step
        with self.assertLogs("telegram_session_owner", level="ERROR"):
            self.assertEqual(run(self.mod._serve(owner, stop, watchdog)), 0)
        self.assertEqual(exits, [1])

    def test_long_reconnect_step_resets_watchdog_after_each_rpc(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        tg.raises["events"] = self.mod.Disconnected()
        self.step(owner)
        clock.advance(15)
        exits, completed_rpcs = [], []
        watchdog = self.mod.HeartbeatWatchdog(clock, terminate=exits.append)
        stop = asyncio.Event()
        began = clock.monotonic()

        def rpc(operation):
            clock.advance(0.03)
            watchdog.check()
            completed_rpcs.append(operation)
            if operation == "events":
                stop.set()

        for operation in ("disconnect", "connect", "history", "recent", "events"):
            tg.hooks[operation] = lambda op=operation: rpc(op)
        with patch.object(self.mod, "WATCHDOG_TIMEOUT", 0.05), patch.dict(
                self.mod.RPC_TIMEOUTS, dict.fromkeys(self.mod.RPC_TIMEOUTS, 0.04)):
            self.assertEqual(run(self.mod._serve(owner, stop, watchdog)), 0)
        self.assertEqual(completed_rpcs[:5], ["disconnect", "connect", "history", "recent", "events"])
        self.assertGreater(clock.monotonic() - began, 0.05)
        self.assertEqual(exits, [])

    def test_rpc_exception_and_timeout_reset_watchdog(self):
        owner = self.started()
        clock, tg = owner.fake.clock, owner.fake.tg
        for hung in (False, True):
            with self.subTest(hung=hung):
                exits = []
                watchdog = self.mod.HeartbeatWatchdog(clock, terminate=exits.append)
                owner._watchdog = watchdog

                async def failed():
                    clock.advance(0.03)
                    if hung:
                        await asyncio.Future()
                    raise self.mod.Disconnected()

                with patch.object(tg, "events", failed), patch.object(self.mod, "WATCHDOG_TIMEOUT", 0.05):
                    with self.assertRaises(self.mod.Disconnected):
                        run(owner._rpc("events"))
                    clock.advance(0.03)
                    watchdog.check()
                self.assertEqual(exits, [])

    def test_single_hung_rpc_still_triggers_watchdog(self):
        owner = self.env.make_owner("a")
        clock, exits = owner.fake.clock, []
        watchdog = self.mod.HeartbeatWatchdog(clock, terminate=exits.append)

        async def scenario():
            entered, stop = asyncio.Event(), asyncio.Event()

            async def never():
                entered.set()
                await asyncio.Future()

            with patch.object(owner.fake.tg, "events", never):
                serving = asyncio.create_task(self.mod._serve(owner, stop, watchdog))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=0.5)
                    clock.advance(0.06)
                    with self.assertLogs("telegram_session_owner", level="ERROR"):
                        watchdog.check()
                    self.assertEqual(exits, [1])
                finally:
                    serving.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await serving

        with patch.object(self.mod, "WATCHDOG_TIMEOUT", 0.05):
            run(scenario())

    def test_serve_waits_after_repeated_early_step_errors(self):
        owner = self.env.make_owner("a")
        stop = asyncio.Event()
        waits = []
        original_wait_for = asyncio.wait_for

        async def timed_wait(awaitable, timeout):
            if timeout == 0.1:
                awaitable.close()
                waits.append(timeout)
                owner.fake.clock.advance(timeout)
                if len(waits) == 5:
                    stop.set()
                raise asyncio.TimeoutError
            return await original_wait_for(awaitable, timeout=timeout)

        with patch.object(owner, "_sweep", side_effect=OSError("private-disk-error")) as sweep:
            with patch.object(asyncio, "wait_for", timed_wait):
                self.assertEqual(run(self.mod._serve(owner, stop, Mock())), 0)
        self.assertEqual(sweep.call_count, 5)
        self.assertEqual(waits, [0.1] * 5)
        self.assertEqual(owner.fake.tg.names(), ["connect", "disconnect"])


class TransportDeadlines(unittest.TestCase):
    def test_transport_and_authorization_have_short_deadlines(self):
        import telegram_owner_telethon as adapter

        async def never():
            await asyncio.Future()

        async def success():
            return True

        for operation in ("connect", "is_user_authorized"):
            with self.subTest(operation=operation):
                telegram = adapter.Telegram.__new__(adapter.Telegram)
                telegram.errors = types.SimpleNamespace(FloodWaitError=type("Flood", (Exception,), {}))
                telegram.client = types.SimpleNamespace(connect=success, is_user_authorized=success)
                setattr(telegram.client, operation, never)
                with patch.object(adapter, "CONNECT_TIMEOUT", 0.01):
                    with self.assertLogs("telegram_owner_telethon", level="WARNING") as logs:
                        with self.assertRaises(import_owner().Disconnected):
                            run(asyncio.wait_for(telegram.connect(), timeout=0.5))
                self.assertIn("OWNER_RPC_TIMEOUT", logs.output[0])


class Watchdog(unittest.TestCase):
    def test_only_completion_resets_deadline(self):
        clock, exits = FakeClock(), []
        watchdog = import_owner().HeartbeatWatchdog(clock, terminate=exits.append)
        clock.advance(299)
        watchdog.check()
        self.assertEqual(exits, [])
        watchdog.completed()
        clock.advance(299)
        watchdog.check()
        self.assertEqual(exits, [])
        clock.advance(1)
        with self.assertLogs("telegram_session_owner", level="ERROR") as logs:
            watchdog.check()
        self.assertEqual(exits, [1])
        self.assertIn("OWNER_WATCHDOG_TIMEOUT", logs.output[0])

    def test_watchdog_exits_process_with_blocked_main_thread(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        program = """
import threading
from telegram_session_owner import HeartbeatWatchdog
class Clock:
    now = 0
    def monotonic(self):
        return self.now
clock = Clock()
with HeartbeatWatchdog(clock, interval=0.01):
    clock.now = 300
    threading.Event().wait(10)
raise AssertionError('watchdog did not exit')
"""
        result = subprocess.run([sys.executable, "-c", program], cwd=scripts,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("OWNER_WATCHDOG_TIMEOUT", result.stderr)

    def test_watchdog_stops_when_context_exits(self):
        clock, exits = FakeClock(), []
        with import_owner().HeartbeatWatchdog(clock, terminate=exits.append) as watchdog:
            self.assertTrue(watchdog.thread.is_alive())
        self.assertFalse(watchdog.thread.is_alive())
        self.assertEqual(exits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
