"""Local IO retries grow backoff only at the local deadline, through real steps."""
import json
import logging
import unittest
from unittest.mock import patch

from test_telegram_session_owner_fakes import (
    Base, import_owner, read_status, run, send_request, write_package,
)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class PauseDeadline(Base):
    def started(self):
        owner = self.env.make_owner("a")
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        logs = Capture()
        logger = logging.getLogger("telegram_session_owner")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(logs)
        self.addCleanup(lambda: (logger.removeHandler(logs), logger.setLevel(old_level)))
        return owner, logs

    def waits(self, logs):
        return [line for line in logs.lines if "OWNER_RECOVERY_WAIT reason=LOCAL" in line]

    def advance_inside_pause(self, owner, logs, seconds):
        before = list(logs.lines)
        calls = list(owner.fake.tg.calls)
        owner.fake.clock.advance(seconds)
        run(owner.step())
        self.assertEqual(logs.lines, before, "failure inside the local pause logged again")
        self.assertEqual(owner.fake.tg.calls, calls, "RPC inside the pause")

    def assert_ready(self):
        heartbeat = json.loads((self.env.root("a") / "heartbeat.json").read_bytes())
        self.assertEqual(heartbeat["state"], "ready")

    def test_heartbeat_failure_at_deadline_grows_once_then_recovers(self):
        owner, logs = self.started()
        clock, store = owner.fake.clock, owner.fake.store
        original = store.atomic_write

        def fail_heartbeat(path, data):
            if path.name == "heartbeat.json":
                raise OSError("PRIVATE-HEARTBEAT")
            return original(path, data)

        clock.advance(5)
        with patch.object(store, "atomic_write", side_effect=fail_heartbeat):
            run(owner.step())  # First local cause still allows immediate recovery.
            self.assertEqual(self.waits(logs), [])
            run(owner.step())
            self.assertEqual(self.waits(logs), ["OWNER_RECOVERY_WAIT reason=LOCAL retry_in=15"])
            self.advance_inside_pause(owner, logs, 14.75)
            clock.advance(0.25)
            run(owner.step())
            self.assertEqual(self.waits(logs), [
                "OWNER_RECOVERY_WAIT reason=LOCAL retry_in=15",
                "OWNER_RECOVERY_WAIT reason=LOCAL retry_in=30",
            ])
            self.advance_inside_pause(owner, logs, 29.75)
        clock.advance(0.25)
        run(owner.step())
        self.assert_ready()
        self.assertEqual(owner.fake.tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", owner.fake.tg.names())

    def test_unknown_failure_at_deadline_grows_once_without_resending(self):
        owner, logs = self.started()
        tg, clock, store = owner.fake.tg, owner.fake.clock, owner.fake.store
        request = send_request(now=clock.utc())
        write_package(self.env.root("a"), request)
        tg.raises["send"] = RuntimeError("PRIVATE-SEND")
        original = store.atomic_write

        def fail_unknown(path, data):
            if path.parent.name == "status" and json.loads(data)["state"] == "unknown":
                raise OSError("PRIVATE-UNKNOWN")
            return original(path, data)

        with patch.object(store, "atomic_write", side_effect=fail_unknown):
            run(owner.step())
            self.assertEqual(self.waits(logs), ["OWNER_RECOVERY_WAIT reason=LOCAL retry_in=15"])
            self.advance_inside_pause(owner, logs, 14.75)
            clock.advance(0.25)
            run(owner.step())
            self.assertEqual(self.waits(logs), [
                "OWNER_RECOVERY_WAIT reason=LOCAL retry_in=15",
                "OWNER_RECOVERY_WAIT reason=LOCAL retry_in=30",
            ])
            self.advance_inside_pause(owner, logs, 29.75)
            self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "sending")
            self.assertEqual(len(tg.sends()), 1)
        clock.advance(0.25)
        run(owner.step())
        self.assert_ready()
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "unknown")
        self.assertEqual(len(tg.sends()), 1)
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())

    def test_local_deadline_is_independent_of_longer_floodwait(self):
        owner, logs = self.started()
        tg, clock, store = owner.fake.tg, owner.fake.clock, owner.fake.store
        request = send_request(now=clock.utc(), schedule={
            "at": clock.utc() + 600, "timezone": None,
            "exact_minute": False, "remind": False,
        })
        write_package(self.env.root("a"), request)
        tg.send_results = [{"scheduled_ids": [900], "at": clock.utc() + 600}]
        tg.raises["scheduled"] = import_owner().FloodWait(80)
        original = store.atomic_write

        def fail_unknown(path, data):
            if path.parent.name == "status" and json.loads(data)["state"] == "unknown":
                raise OSError("PRIVATE-UNKNOWN")
            return original(path, data)

        with patch.object(store, "atomic_write", side_effect=fail_unknown):
            run(owner.step())
            self.assertEqual(self.waits(logs), ["OWNER_RECOVERY_WAIT reason=LOCAL retry_in=80"])
            calls = list(tg.calls)
            for delay, effective_wait in ((15, 65), (30, 60)):
                self.advance_inside_pause(owner, logs, delay - 0.25)
                clock.advance(0.25)
                run(owner.step())
                self.assertEqual(self.waits(logs)[-1],
                                 f"OWNER_RECOVERY_WAIT reason=LOCAL retry_in={effective_wait}")
                self.assertEqual(tg.calls, calls)
            self.assertEqual(len(self.waits(logs)), 3)
        # The last local deadline (105 s) is later than the server's 80 s.
        self.advance_inside_pause(owner, logs, 59.75)
        clock.advance(0.25)
        run(owner.step())
        self.assert_ready()
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "unknown")
        self.assertEqual(len(tg.sends()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
