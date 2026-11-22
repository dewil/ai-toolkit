"""Owner: a local failure inside the current local pause must not escalate it.

Written blind from docs/telegram-session-owner.md and the lead's decision of 22.11:
a failure before the current local pause expires neither grows the pause nor logs a
new OWNER_RECOVERY_WAIT; growth and logging only follow a failure of a retry made
after the pause expired.
"""
import json
import logging
import re
import unittest
from unittest.mock import patch

from test_telegram_session_owner_fakes import (
    Base, read_status, run, send_request, write_package,
)
import test_telegram_owner_local_backoff as lb
from test_telegram_owner_local_backoff import PAUSES

LOGGER = "telegram_session_owner"


class PauseEscalation(Base):
    def capture(self):
        handler = lb.Capture()
        logger = logging.getLogger(LOGGER)
        old = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(lambda: (logger.removeHandler(handler), logger.setLevel(old)))
        return handler

    def started(self):
        owner = self.env.make_owner("a")
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    @staticmethod
    def waits(logs):
        return [s for s in logs.lines
                if "OWNER_RECOVERY_WAIT" in s and "reason=LOCAL" in s]

    def drive(self, owner, seconds):
        clock = owner.fake.clock
        for _ in range(int(round(seconds / 0.1))):
            run(owner.step())
            clock.advance(0.1)

    def recover_time(self, owner, t0, limit=60):
        """Step until ready; return monotonic seconds since t0 (or None)."""
        clock = owner.fake.clock
        for _ in range(int(limit / 0.1)):
            run(owner.step())
            if self.state() == "ready":
                return clock.monotonic() - t0
            clock.advance(0.1)
        return None

    def test_persistent_unknown_write_failure_one_log_line_and_fast_recovery(self):
        owner = self.started()
        tg, clock, store = owner.fake.tg, owner.fake.clock, owner.fake.store
        request = send_request(now=clock.utc())
        write_package(self.env.root("a"), request)
        tg.raises["send"] = RuntimeError("private")
        original = store.atomic_write

        def fail_unknown(path, data):
            if path.parent.name == "status" and json.loads(data)["state"] == "unknown":
                raise OSError("private")
            return original(path, data)

        logs = self.capture()
        t0 = clock.monotonic()
        with patch.object(store, "atomic_write", side_effect=fail_unknown):
            self.drive(owner, 10)
            waits = self.waits(logs)
            self.assertLessEqual(len(waits), 1, waits)
            self.assertTrue(waits and waits[0].endswith("retry_in=15"), waits)
            self.assertEqual(len(tg.sends()), 1)
        elapsed = self.recover_time(owner, t0)
        self.assertIsNotNone(elapsed, "owner never returned to ready")
        self.assertLessEqual(elapsed, 16.2, "return to ready after the failure is lifted")
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "unknown")
        self.assertEqual(len(tg.sends()), 1)

    def test_persistent_heartbeat_write_failure_one_log_line_and_fast_recovery(self):
        owner = self.started()
        clock, store = owner.fake.clock, owner.fake.store
        original = store.atomic_write

        def fail_heartbeat(path, data):
            if path.name == "heartbeat.json":
                raise OSError("private")
            return original(path, data)

        logs = self.capture()
        t0 = clock.monotonic()
        with patch.object(store, "atomic_write", side_effect=fail_heartbeat):
            self.drive(owner, 10)
            waits = self.waits(logs)
            self.assertLessEqual(len(waits), 1, waits)
        elapsed = self.recover_time(owner, t0)
        self.assertIsNotNone(elapsed, "owner never returned to ready")
        self.assertLessEqual(elapsed, 16.2, "return to ready after the failure is lifted")


class ControlGrowth(Base):
    """Control: persistent local catch-up failure still grows pauses, one line per pause."""

    # Reuse helpers only (not the inherited tests of LocalBackoff).
    capture = lb.LocalBackoff.capture
    started = lb.LocalBackoff.started
    state = lb.LocalBackoff.state
    go_degraded_with_broken_mirror = lb.LocalBackoff.go_degraded_with_broken_mirror

    def test_control_pauses_grow_one_line_per_pause(self):
        owner, store = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        self.go_degraded_with_broken_mirror(owner, store)
        logs = self.capture()
        for _ in range(1200):
            run(owner.step())
            clock.advance(0.5)
        waits = [s for s in logs.lines if "OWNER_RECOVERY_WAIT" in s and "reason=LOCAL" in s]
        values = [int(re.search(r"retry_in=(\d+)", s).group(1)) for s in waits]
        self.assertEqual(values[:len(PAUSES)], list(PAUSES), waits)
        self.assertLessEqual(len(values), 10, waits)


if __name__ == "__main__":
    unittest.main(verbosity=2)
