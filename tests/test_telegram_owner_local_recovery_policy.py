"""Recovery deadlines and deferred overflow through the offline owner ports."""
import json
import unittest
from unittest.mock import patch

from test_telegram_session_owner_fakes import (
    Base, import_owner, read_status, refresh_request, run, send_request, write_package,
)


class LocalRecoveryPolicy(Base):
    def started(self):
        owner = self.env.make_owner("a", destinations=[self.env.destination()])
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    def overflow(self, owner):
        owner.fake.tg.event_batches = [[{"kind": "overflow"}]]
        run(owner.step())

    def fail_catchup(self, owner, delay, *, operation="history"):
        owner.fake.tg.raises[operation] = ValueError("PRIVATE-MIRROR-DETAIL")
        with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
            run(owner.step())
        self.assertTrue(any(s.endswith(f"reason=LOCAL retry_in={delay}")
                            for s in logs.output), logs.output)
        self.assertNotIn("PRIVATE-MIRROR-DETAIL", "\n".join(logs.output))
        self.assertEqual(self.state(), "degraded")

    def wait_without_rpc(self, owner, seconds):
        mark = len(owner.fake.tg.calls)
        owner.fake.clock.advance(seconds)
        run(owner.step())
        self.assertEqual(len(owner.fake.tg.calls), mark)

    def test_exact_local_deadlines_cap_and_reset_after_complete_ready_step(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        self.overflow(owner)
        for delay in (15, 30, 60, 120, 240, 300, 300):
            self.fail_catchup(owner, delay)
            self.wait_without_rpc(owner, delay - 0.25)
            clock.advance(0.25)
        run(owner.step())
        self.assertEqual(self.state(), "ready")
        clock.advance(5)
        self.overflow(owner)
        self.fail_catchup(owner, 15)
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())

    def test_successful_catchup_then_failed_ready_work_does_not_reset_local_delay(self):
        owner = self.started()
        self.overflow(owner)
        self.fail_catchup(owner, 15)
        owner.fake.clock.advance(15)
        # History/recent succeed and publish ready, but this step still fails.
        self.fail_catchup(owner, 30, operation="events")
        self.wait_without_rpc(owner, 29)
        owner.fake.clock.advance(1)
        run(owner.step())
        self.assertEqual(self.state(), "ready")

    def test_repeated_local_refresh_failure_after_catchup_is_paused(self):
        owner = self.started()
        request = refresh_request(now=owner.fake.clock.utc())
        write_package(self.env.root("a"), request)
        with patch.object(owner, "_refresh", side_effect=OSError("PRIVATE-REFRESH")):
            run(owner.step())  # First local cause schedules immediate recovery.
            self.assertEqual(self.state(), "degraded")
            for delay in (15, 30):
                with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
                    run(owner.step())  # Catch-up succeeds, refresh fails locally.
                self.assertTrue(any(s.endswith(f"reason=LOCAL retry_in={delay}")
                                    for s in logs.output), logs.output)
                self.assertNotIn("PRIVATE-REFRESH", "\n".join(logs.output))
                self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "queued")
                self.wait_without_rpc(owner, delay - 1)
                owner.fake.clock.advance(1)
        run(owner.step())
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "refreshed")

    def test_queue_expiry_continues_without_rpc_during_local_pause(self):
        owner = self.started()
        self.overflow(owner)
        self.fail_catchup(owner, 15)
        request = send_request(now=owner.fake.clock.utc(), expires_at=owner.fake.clock.utc() + 5)
        write_package(self.env.root("a"), request)
        self.wait_without_rpc(owner, 5)
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["code"], "REQUEST_EXPIRED")

    def test_refresh_and_queued_status_errors_in_one_step_schedule_one_pause(self):
        owner = self.started()
        self.overflow(owner)
        request = refresh_request(now=owner.fake.clock.utc())
        write_package(self.env.root("a"), request)
        owner.fake.store.fail_on[("atomic_write", request["request_id"] + ".json")] = (
            "before", OSError("PRIVATE-STATUS"))
        with patch.object(owner, "_refresh", side_effect=OSError("PRIVATE-REFRESH")):
            with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
                run(owner.step())
        waits = [s for s in logs.output if "OWNER_RECOVERY_WAIT" in s]
        self.assertEqual(len(waits), 1, waits)
        self.assertTrue(waits[0].endswith("reason=LOCAL retry_in=15"), waits)
        self.wait_without_rpc(owner, 14)
        owner.fake.clock.advance(1)
        run(owner.step())
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "refreshed")

    def test_local_error_preserves_network_deadline_and_outage_clock(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        tg.raises["events"] = import_owner().Disconnected()
        run(owner.step())
        # A local failure during the existing wait moves the deadline later.
        clock.advance(1)
        owner.fake.store.fail_on[("atomic_write", "heartbeat.json")] = ("before", OSError("private"))
        # Force a due heartbeat naturally (five seconds since its last write).
        clock.advance(4)
        with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
            run(owner.step())
        self.assertTrue(any(s.endswith("reason=LOCAL retry_in=15") for s in logs.output), logs.output)
        self.wait_without_rpc(owner, 14)
        # Jump past 120 s since the original outage, then fail reconnect locally.
        clock.advance(101)
        tg.raises["connect"] = OSError("private")
        with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
            run(owner.step())
        self.assertTrue(any(s.endswith("reason=LOCAL retry_in=30") for s in logs.output), logs.output)
        clock.advance(30)
        tg.raises["connect"] = import_owner().Disconnected()
        with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
            run(owner.step())
        self.assertTrue(any(s.endswith("reason=DISCONNECTED retry_in=30") for s in logs.output), logs.output)
        clock.advance(30)
        run(owner.step())
        self.assertEqual(self.state(), "ready")

    def test_unknown_write_failure_keeps_later_floodwait_or_local_deadline(self):
        for seconds in (5, 80):
            with self.subTest(seconds=seconds):
                owner = self.started()
                tg, clock, store = owner.fake.tg, owner.fake.clock, owner.fake.store
                request = send_request(now=clock.utc(), schedule={
                    "at": clock.utc() + 600, "timezone": None,
                    "exact_minute": False, "remind": False})
                request["options"]["no_pace_check"] = True
                write_package(self.env.root("a"), request)
                tg.send_results = [{"scheduled_ids": [900], "at": clock.utc() + 600}]
                tg.raises["scheduled"] = import_owner().FloodWait(seconds)
                original = store.atomic_write

                def fail_unknown(path, data):
                    if path.parent.name == "status" and json.loads(data)["state"] == "unknown":
                        raise OSError("PRIVATE-UNKNOWN-DETAIL")
                    return original(path, data)

                with patch.object(store, "atomic_write", side_effect=fail_unknown):
                    with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
                        run(owner.step())
                delay = max(15, seconds)
                self.assertTrue(any(s.endswith(f"reason=LOCAL retry_in={delay}")
                                    for s in logs.output), logs.output)
                self.assertNotIn("PRIVATE-UNKNOWN-DETAIL", "\n".join(logs.output))
                self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "sending")
                self.wait_without_rpc(owner, delay - 1)
                self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "unknown")
                clock.advance(1)
                run(owner.step())
                self.assertEqual(self.state(), "ready")
                self.assertEqual(len(tg.sends()), 1)
                self.assertEqual(tg.names().count("connect"), 1)
                run(owner.close())

    def test_persistent_unknown_write_failure_keeps_sending_without_resend(self):
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

        with patch.object(store, "atomic_write", side_effect=fail_unknown):
            run(owner.step())
            self.wait_without_rpc(owner, 1)
            self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "sending")
        self.wait_without_rpc(owner, 1)
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "unknown")
        self.assertEqual(len(tg.sends()), 1)

    def test_deferred_overflow_survives_without_new_events_and_sends_until_due(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        times = []
        tg.hooks["history"] = lambda: times.append(clock.monotonic())
        self.overflow(owner)
        run(owner.step())
        self.assertEqual(len(times), 1)
        clock.advance(4.75)
        request = send_request(peer=1001, now=clock.utc())
        write_package(self.env.root("a"), request)
        self.overflow(owner)
        self.assertEqual(self.state(), "ready")
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "sent")
        self.assertEqual(len(times), 1)
        # A wall-clock jump cannot release the deferred monotonic deadline.
        clock._utc += 1000
        run(owner.step())
        self.assertEqual(len(times), 1)
        clock.advance(0.25)
        run(owner.step())
        run(owner.step())
        self.assertEqual(times, [times[0], times[0] + 5])
        self.assertEqual(self.state(), "ready")

    def test_short_floodwait_during_overflow_does_not_bypass_five_second_interval(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        times = []
        tg.hooks["history"] = lambda: times.append(clock.monotonic())
        self.overflow(owner)
        tg.raises["history"] = import_owner().FloodWait(1)
        run(owner.step())
        self.wait_without_rpc(owner, 4.75)
        clock.advance(0.25)
        run(owner.step())
        self.assertEqual(times, [times[0], times[0] + 5])
        self.assertEqual(self.state(), "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
