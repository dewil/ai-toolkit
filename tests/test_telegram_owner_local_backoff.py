"""Owner: pause after a repeated local catch-up failure; overflow must not starve sends.

Written blind from docs/telegram-session-owner.md and the lead's decision of 22.11
(1: local catch-up failure backoff 15,30,60,120,240,300 s; 2: overflow catch-ups >= 5 s apart).
"""
import json
import logging
import re
import unittest

from test_telegram_session_owner_fakes import (
    Base, FakeStore, read_status, run, send_request, write_package,
)
from test_telegram_session_owner_mirror import rec

PAUSES = (15, 30, 60, 120, 240, 300)


class MirrorStore(FakeStore):
    """FakeStore: after arm (broken=True) the next mirror write (result.json) fails once."""

    def __init__(self):
        super().__init__()
        self.broken = False

    def atomic_write(self, path, data):
        if self.broken and str(path).endswith("result.json"):
            self.broken = False  # one failure per arming (replay of the journal may succeed)
            raise OSError("PRIVATE-DISK-DETAIL")
        return super().atomic_write(path, data)


class Capture(logging.Handler):
    """Collects owner log lines without failing when there are none."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class LocalBackoff(Base):
    def capture(self):
        handler = Capture()
        logger = logging.getLogger("telegram_session_owner")
        old = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(lambda: (logger.removeHandler(handler), logger.setLevel(old)))
        return handler

    def started(self):
        store = MirrorStore()
        owner = self.env.make_owner("a", store=store, destinations=[self.env.destination()])
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner, store

    def state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    def history_times(self, owner, mark_calls):
        """Monotonic times of history calls: wrap the fake's history."""
        tg, clock = owner.fake.tg, owner.fake.clock
        times = []
        tg.hooks["history"] = lambda: times.append(clock.monotonic())
        return times

    def go_degraded_with_broken_mirror(self, owner, store):
        """Mirror is in sync; then Telegram reports an edit of message 1 (recent window)
        and mirror writes break: every catch-up gets successful history/recent RPCs and
        then fails locally on the write."""
        tg = owner.fake.tg
        tg.history_data = [rec(1), rec(2)]
        tg.recent_data = [rec(1), rec(2)]
        tg.event_batches = [[{"kind": "overflow"}]]
        run(owner.step())
        run(owner.step())
        self.assertEqual(self.state(), "ready")
        owner.fake.clock.advance(10)
        tg.recent_data = [rec(1, text="edited"), rec(2)]
        # Re-armed after every recent read: the local error comes strictly after
        # successful history/recent RPCs, on every catch-up attempt.
        tg.hooks["recent"] = lambda: setattr(store, "broken", True)
        tg.event_batches = [[{"kind": "overflow"}]]
        run(owner.step())
        self.assertEqual(self.state(), "degraded")

    def test_persistent_local_catchup_failure_is_paused_10s(self):
        owner, store = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        self.go_degraded_with_broken_mirror(owner, store)
        mark = tg.names().count("history")
        logs = self.capture()
        for _ in range(100):
            run(owner.step())
            clock.advance(0.1)
        calls = tg.names().count("history") - mark
        self.assertLessEqual(calls, 2, "history calls in 10 s of persistent local failure")
        self.assertEqual(self.state(), "degraded")
        self.assertNotIn("disconnect", tg.names())
        pause_lines = [s for s in logs.lines
                       if re.search(r"local", s, re.I) and re.search(r"\b15\b", s)]
        self.assertTrue(pause_lines, logs.lines)
        self.assertNotIn("PRIVATE-DISK-DETAIL", "\n".join(logs.lines))

    def test_persistent_local_catchup_failure_pauses_grow_to_cap(self):
        owner, store = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        self.go_degraded_with_broken_mirror(owner, store)
        times = self.history_times(owner, 0)
        logs = self.capture()
        for _ in range(600):
            run(owner.step())
            clock.advance(1.0)
        self.assertLessEqual(len(times), 10, f"{len(times)} history calls in 600 s")
        self.assertGreaterEqual(len(times), 3, times)
        gaps = [b - a for a, b in zip(times, times[1:])]
        for gap, pause in zip(gaps, PAUSES):
            self.assertGreaterEqual(gap, pause - 0.001, (gaps, times))
        for a, b in zip(gaps, gaps[1:]):
            self.assertGreaterEqual(b, a - 1.001, gaps)
        logged = set()
        for s in logs.lines:
            if re.search(r"local", s, re.I):
                logged.update(int(n) for n in re.findall(r"\b\d+\b", s))
        self.assertTrue({15, 30} <= logged, logs.lines)

    def test_single_local_failure_returns_to_ready_without_15s_pause(self):
        owner, store = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        tg.history_data = [rec(1)]
        tg.event_batches = [[{"kind": "overflow"}]]
        run(owner.step())
        self.assertEqual(self.state(), "degraded")
        store.fail_on[("atomic_write", "result.json")] = ("before", OSError("x"))
        run(owner.step())  # catch-up hits the one-shot local failure
        self.assertEqual(self.state(), "degraded")
        start = clock.monotonic()
        for _ in range(3):
            clock.advance(0.1)
            run(owner.step())
        self.assertEqual(self.state(), "ready")
        self.assertLess(clock.monotonic() - start, 15)
        self.assertNotIn("disconnect", tg.names())

    def overflow_every_step(self, peer):
        owner, store = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        request = send_request(peer=peer, now=clock.utc())
        request["options"]["no_pace_check"] = True
        write_package(self.env.root("a"), request)
        tg.event_batches = [[{"kind": "overflow"}] for _ in range(50)]
        mark = tg.names().count("history")
        for _ in range(50):
            run(owner.step())
            clock.advance(0.1)
        status = read_status(self.env.root("a"), request["request_id"])
        self.assertEqual(status and status["state"], "sent")
        self.assertLessEqual(tg.names().count("history") - mark, 2)

    def test_frequent_overflow_does_not_starve_send_to_other_peer(self):
        self.overflow_every_step("me")

    def test_frequent_overflow_does_not_starve_send_to_mirrored_peer(self):
        self.overflow_every_step(1001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
