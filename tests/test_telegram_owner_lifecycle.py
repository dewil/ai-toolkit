"""Lifecycle failure paths beyond the independent start acceptance tests."""
import json
import os
import unittest
import uuid
from unittest.mock import patch

from test_telegram_session_owner_fakes import (
    Base, FakeStore, FakeTelegram, import_owner, read_status, run,
    send_request, write_package,
    write_file, mkdirs,
)


class StaleTransport(FakeTelegram):
    """A failed transport must be disconnected before connect can recover."""
    stale = False

    async def connect(self):
        if self.stale:
            self._log("connect")
            raise import_owner().Disconnected()
        await super().connect()

    async def disconnect(self):
        await super().disconnect()
        self.stale = False

    async def events(self):
        if self.stale:
            self._log("events")
            raise import_owner().Disconnected()
        return await super().events()


class Reconnect(Base):
    def started(self, **kwargs):
        owner = self.env.make_owner("a", **kwargs)
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    def test_idle_listing_is_cached_and_sweep_has_30_second_interval(self):
        owner = self.started()
        root = self.env.root("a")
        for name in ("tmp", "status", "outbox"):
            mkdirs(root / name)
        # A directory must outlive the two-second timestamp settling window
        # before unchanged metadata can prove that its listing is cached.
        outbox = root / "outbox"
        old = outbox.stat().st_mtime - 3
        os.utime(outbox, (old, old))
        store = owner.fake.store
        with patch.object(store, "list", wraps=store.list) as listing:
            run(owner.step())  # первый проход уборки без ожидания
            self.assertCountEqual([c.args[0] for c in listing.call_args_list],
                                  [root / name for name in ("tmp", "status", "outbox")])
            listing.reset_mock()
            for _ in range(20):
                run(owner.step())
            owner.fake.clock.advance(29.9)
            run(owner.step())
            listing.assert_not_called()
            owner.fake.clock.advance(0.1)
            run(owner.step())
            self.assertCountEqual([c.args[0] for c in listing.call_args_list],
                                  [root / name for name in ("tmp", "status")])

    def test_same_owner_restart_discards_terminal_cache_and_recovers_sending(self):
        owner = self.started()
        req = send_request("a")
        root = self.env.root("a")
        write_package(root, req)
        run(owner.step())
        self.assertEqual(read_status(root, req["request_id"])["state"], "sent")
        run(owner.close())
        # A durable sending marker restored while the owner is stopped must
        # override its previous in-memory terminal observation.
        status = read_status(root, req["request_id"])
        status.update(state="sending", code="SENDING", retain_until=None)
        owner.fake.store.atomic_write(root / "status" / f"{req['request_id']}.json",
                                     json.dumps(status).encode())
        self.assertEqual(run(owner.start())["code"], "READY")
        run(owner.step())
        self.assertEqual(read_status(root, req["request_id"])["state"], "unknown")
        self.assertEqual(len(owner.fake.tg.sends()), 1)

    def test_step_resets_stale_transport_and_catches_up_before_ready(self):
        tg = StaleTransport()
        owner = self.started(tg=tg, destinations=[self.env.destination()])
        tg.stale = True
        run(owner.step())
        self.assertEqual(self.state(), "degraded")
        tg.calls.clear()
        for name in ("connect", "history", "recent"):
            tg.hooks[name] = lambda: self.assertEqual(self.state(), "degraded")
        owner.fake.clock.advance(14)
        run(owner.step())
        self.assertEqual(tg.calls, [])
        owner.fake.clock.advance(1)
        run(owner.step())
        self.assertEqual(self.state(), "ready")
        self.assertEqual(tg.names()[:4], ["disconnect", "connect", "history", "recent"])
        self.assertEqual(owner.fake.clock.async_sleeps, [])

    def test_failed_reconnect_has_capped_exponential_pause_and_resets_after_success(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        tg.raises["events"] = import_owner().Disconnected()
        run(owner.step())
        for delay in (15,) * 8 + (30, 60, 120, 240, 300, 300):
            count = len(tg.calls)
            clock.advance(delay - 1)
            for _ in range(5):
                run(owner.step())
            self.assertEqual(len(tg.calls), count, f"network called before {delay}s")
            clock.advance(1)
            tg.raises["connect"] = import_owner().Disconnected()
            run(owner.step())
            self.assertGreater(len(tg.calls), count)
            self.assertEqual(self.state(), "degraded")
        clock.advance(300)
        run(owner.step())
        self.assertEqual(self.state(), "ready")
        tg.raises["events"] = import_owner().Disconnected()
        run(owner.step())
        count = tg.names().count("connect")
        clock.advance(15)
        run(owner.step())
        self.assertEqual(tg.names().count("connect"), count + 1)
        self.assertEqual(self.state(), "ready")
        self.assertEqual(clock.async_sleeps, [])

    def test_catchup_failure_keeps_degraded_and_floodwait_is_respected(self):
        owner = self.started(destinations=[self.env.destination()])
        tg, clock = owner.fake.tg, owner.fake.clock
        tg.raises["events"] = import_owner().Disconnected()
        run(owner.step())
        clock.advance(15)
        tg.raises["history"] = import_owner().FloodWait(75)
        run(owner.step())
        self.assertEqual(self.state(), "degraded")
        count = len(tg.calls)
        clock.advance(74)
        run(owner.step())
        self.assertEqual(len(tg.calls), count)
        clock.advance(1)
        run(owner.step())
        self.assertEqual(self.state(), "ready")

    def test_persistent_sweep_error_cannot_reconnect_on_every_step(self):
        owner = self.started()
        status = self.env.root("a") / "status" / f"{uuid.uuid4()}.json"
        mkdirs(status.parent)
        write_file(status, "{broken")
        for _ in range(20):
            run(owner.step())
        self.assertEqual(owner.fake.tg.names().count("connect"), 2)
        owner.fake.clock.advance(14)
        run(owner.step())
        self.assertEqual(owner.fake.tg.names().count("connect"), 2)
        owner.fake.clock.advance(1)
        run(owner.step())
        self.assertEqual(owner.fake.tg.names().count("connect"), 2)
        owner.fake.clock.advance(15)  # новый проход уборки: не чаще одного в 30 с
        run(owner.step())
        self.assertEqual(owner.fake.tg.names().count("connect"), 3)

    def check_uncertain_after_reconnect(self, fail_unknown):
        owner = self.started(tg=StaleTransport())
        tg, clock, store = owner.fake.tg, owner.fake.clock, owner.fake.store
        request = send_request(text="private-body", now=clock.utc())
        request["options"]["no_pace_check"] = True
        write_package(self.env.root("a"), request)
        tg.send_results = [import_owner().Disconnected()]
        original = store.atomic_write

        def write(path, data):
            if (fail_unknown and path.parent.name == "status"
                    and json.loads(data)["state"] == "unknown"):
                raise OSError("private-error")
            original(path, data)

        with patch.object(store, "atomic_write", side_effect=write):
            run(owner.step())
        before = read_status(self.env.root("a"), request["request_id"])
        self.assertEqual(before["state"], "sending" if fail_unknown else "unknown")
        tg.stale = True
        count = len(tg.sends())
        clock.advance(15)
        for _ in range(3):
            run(owner.step())
        self.assertEqual(self.state(), "ready")
        self.assertEqual(len(tg.sends()), count)
        status = read_status(self.env.root("a"), request["request_id"])
        self.assertEqual((status["state"], status["code"]), ("unknown", "SEND_UNCERTAIN"))

    def test_unknown_is_never_sent_again_after_reconnect(self):
        self.check_uncertain_after_reconnect(False)

    def test_durable_sending_is_never_sent_again_after_reconnect(self):
        self.check_uncertain_after_reconnect(True)

    def test_state_changes_logged_once_without_error_details(self):
        owner = self.env.make_owner("a")
        with self.assertLogs("telegram_session_owner", level="INFO") as logs:
            self.assertEqual(run(owner.start())["code"], "READY")
            owner.fake.tg.raises["events"] = import_owner().Disconnected("private-error")
            run(owner.step())
            owner.fake.clock.advance(5)
            run(owner.step())
            owner.fake.clock.advance(10)
            run(owner.step())
            run(owner.close())
        transitions = [line for line in logs.output if "OWNER_STATE" in line]
        self.assertEqual(len(transitions), 4)
        for state, line in zip(("ready", "degraded", "ready", "stopping"), transitions):
            self.assertIn(f"state={state}", line)
        self.assertNotIn("private-error", "\n".join(logs.output))


class LifecycleFailures(Base):
    def test_ready_write_failure_after_effect_stops_and_unlocks(self):
        store = FakeStore()
        store.fail_on[("atomic_write", "heartbeat")] = ("after", OSError("disk"))
        owner = self.env.make_owner("a", store=store)
        self.assertEqual(run(owner.start())["exit_code"], 2)
        heartbeat = json.loads((self.env.root("a") / "heartbeat.json").read_bytes())
        self.assertEqual(heartbeat["state"], "stopping")
        self.assertFalse(owner.fake.tg.connected)
        replacement = self.env.make_owner("a")
        try:
            self.assertEqual(run(replacement.start())["code"], "READY")
        finally:
            run(replacement.close())

    def test_disconnect_failure_does_not_escape_start_or_retain_locks(self):
        owner = self.env.make_owner("a")
        owner.fake.tg.raises["connect"] = OSError("connect")
        owner.fake.tg.raises["disconnect"] = OSError("disconnect")
        with self.assertLogs("telegram_session_owner", level="ERROR"):
            self.assertEqual(run(owner.start())["exit_code"], 2)
        replacement = self.env.make_owner("a")
        try:
            self.assertEqual(run(replacement.start())["code"], "READY")
        finally:
            run(replacement.close())

    def test_persistent_heartbeat_failure_returns_without_sleep_or_network_spin(self):
        store = FakeStore()
        owner = self.env.make_owner("a", store=store)
        self.assertEqual(run(owner.start())["code"], "READY")
        original = store.atomic_write

        def failed(path, data):
            raise OSError("disk")

        store.atomic_write = failed
        owner.fake.clock.advance(5)
        try:
            with self.assertLogs("telegram_session_owner", level="ERROR"):
                run(owner.step())
                count = len(owner.fake.tg.calls)
                run(owner.step())
            self.assertEqual(len(owner.fake.tg.calls), count)
            self.assertEqual(owner.fake.clock.async_sleeps, [])
        finally:
            store.atomic_write = original
            run(owner.close())

    def test_store_lock_refuses_inode_replaced_during_acquisition(self):
        path = self.base / "session"
        path.write_bytes(b"old")
        old_link = self.base / "old-inode"
        os.link(path, old_link)
        replacement = self.base / "replacement"
        replacement.write_bytes(b"new")
        real_flock = import_owner().fcntl.flock

        def replace_after_lock(fd, operation):
            real_flock(fd, operation)
            os.replace(replacement, path)

        with patch("telegram_session_owner.fcntl.flock", replace_after_lock):
            with self.assertRaises(OSError):
                import_owner().Store().lock(path)
        # The rejected lock must not leak a descriptor/lock on the old inode.
        with import_owner().Store().lock(old_link):
            self.assertEqual(old_link.read_bytes(), b"old")
        with import_owner().Store().lock(path):
            self.assertEqual(path.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main(verbosity=2)
