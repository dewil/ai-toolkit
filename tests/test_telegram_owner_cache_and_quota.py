"""Quota read costs, retained disk usage and concrete outbox metadata."""
import os
import time
import unittest
from unittest.mock import patch

from test_telegram_owner_quota import put_status
from test_telegram_session_owner_fakes import (
    Base, FakeClock, FakeStore, NOW, import_client, import_owner,
    read_status, run, send_request, write_heartbeat, write_package,
)


class CountingStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.status_reads = 0

    def read(self, path):
        if path.parent.name == "status":
            self.status_reads += 1
        return super().read(path)


class QuotaAccounting(Base):
    def setUp(self):
        super().setUp()
        self.client = import_client()
        self.root = self.env.root("a")
        self.store = CountingStore()
        self.clock = FakeClock()
        write_heartbeat(self.root)

    def terminal(self, *, where="outbox"):
        req = send_request("a")
        package = write_package(self.root, req, where=where)
        put_status(self.root, req, "sent", "SENT", 0, NOW + 7 * 86400)
        return package

    def submit(self):
        return self.client.submit(self.root, send_request("a"), clock=self.clock,
                                  store=self.store, timeout=0)

    def test_terminal_status_reads_stop_when_room_is_proven(self):
        terminal, pending = 0, 0
        for n in (50, 99, 100, 150, 250):
            for _ in range(n - terminal):
                self.terminal()
            terminal = n
            self.store.status_reads = 0
            reply = self.submit()
            self.assertEqual(reply["state"], "queued")
            self.assertEqual(self.store.status_reads, max(0, n + pending - 99))
            pending += 1

    def test_below_limit_needs_no_status_reads(self):
        for _ in range(99):
            self.terminal()
        self.assertEqual(self.submit()["state"], "queued")
        self.assertEqual(self.store.status_reads, 0)

    def test_terminal_files_still_exhaust_disk_quota(self):
        package = self.terminal()
        self.store.inspect_override[package / "request.json"] = {
            "size": self.client.SPOOL_LIMIT}
        self.assertEqual(self.submit()["code"], "QUEUE_FULL")
        self.assertEqual(self.store.status_reads, 0)

    def test_terminal_reservations_still_exhaust_disk_quota(self):
        package = self.terminal()
        self.store.atomic_write(package / ".reservation.json", self.client._json({
            "request_id": package.name, "bytes": self.client.SPOOL_LIMIT}))
        self.assertEqual(self.submit()["code"], "QUEUE_FULL")

    def test_terminal_tmp_package_does_not_fill_last_queue_slot(self):
        for _ in range(99):
            write_package(self.root, send_request("a"))
        self.terminal(where="tmp")
        self.assertEqual(self.submit()["state"], "queued")

    def test_owner_accepts_send_with_100_retained_terminal_packages(self):
        owner = self.env.make_owner("a", store=self.store)
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        for _ in range(100):
            self.terminal()
        req = send_request("a")
        write_package(self.root, req)
        run(owner.step())
        self.assertEqual(read_status(self.root, req["request_id"])["state"], "sent")
        self.assertEqual(len(owner.fake.tg.sends()), 1)


class OutboxMetadata(Base):
    def start(self):
        owner = self.env.make_owner("a", store=import_owner().Store())
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        self.owner = owner
        self.root = self.env.root("a")
        self.old_ns = (int(time.time()) - 10) * 1_000_000_000
        req = self.publish("A")
        run(owner.step())
        self.assertEqual(read_status(self.root, req["request_id"])["state"], "sent")
        return req

    def publish(self, text, *, ns=None):
        req = send_request("a", text=text)
        req["options"]["no_pace_check"] = True
        write_package(self.root, req)
        ns = self.old_ns if ns is None else ns
        os.utime(self.root / "outbox", ns=(ns, ns))
        return req

    def assert_sent(self, req):
        run(self.owner.step())
        self.assertEqual(read_status(self.root, req["request_id"])["state"], "sent")
        self.assertEqual(len(self.owner.fake.tg.sends()), 2)

    def test_link_count_detects_publication_with_old_unchanged_mtime(self):
        self.start()
        self.assert_sent(self.publish("B"))

    def test_nanoseconds_detect_balanced_removal_and_publication(self):
        a = self.start()
        directory = self.root / "outbox"
        before = directory.stat()
        self.owner.store.remove(directory / a["request_id"])
        b = self.publish("B", ns=self.old_ns + 1)
        after = directory.stat()
        self.assertEqual(before.st_nlink, after.st_nlink)
        self.assertEqual(before.st_mtime, after.st_mtime)
        self.assertNotEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assert_sent(b)

    def test_young_listing_is_rechecked_after_settling_window(self):
        a = self.start()
        self.old_ns = int(time.time()) * 1_000_000_000
        directory = self.root / "outbox"
        os.utime(directory, ns=(self.old_ns, self.old_ns))
        wall_time = self.old_ns / 1_000_000_000
        with patch.object(import_owner().time, "time", return_value=wall_time + 1):
            run(self.owner.step())
        self.owner.store.remove(directory / a["request_id"])
        b = self.publish("B")  # same mtime, nanoseconds and link count
        with patch.object(import_owner().time, "time", return_value=wall_time + 3):
            self.assert_sent(b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
