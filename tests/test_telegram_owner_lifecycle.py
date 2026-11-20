"""Lifecycle failure paths beyond the independent start acceptance tests."""
import json
import os
import unittest
from unittest.mock import patch

from test_telegram_session_owner_fakes import Base, FakeStore, import_owner, run


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
