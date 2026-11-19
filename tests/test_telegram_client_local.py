"""Local client checks complementing the unchanged independent RED suite."""
from __future__ import annotations

from contextlib import ExitStack
import errno
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unittest

from test_telegram_session_owner_fakes import (
    Base, FakeClock, FakeStore, NOW, import_client, import_owner, refresh_request,
    req_hash, send_request, write_heartbeat, write_package,
)


class ClientLocal(Base):
    def setUp(self):
        super().setUp()
        self.client = import_client()
        self.root = self.env.root("a")
        self.clock = FakeClock()
        self.store = FakeStore()
        write_heartbeat(self.root)

    def attachment(self, path=None):
        path = path or self.base / "document.txt"
        path.write_bytes(b"document")
        path.chmod(0o644)
        return {"source": str(path), "name": path.name, "size": 8,
                "sha256": hashlib.sha256(b"document").hexdigest()}

    def dry(self, request):
        return self.client.dry_run(self.root, request,
                                   installation_config=self.env.registry_path,
                                   clock=self.clock, store=self.store)

    def put_status(self, request, **over):
        st = {"schema_version": 1, "request_id": request["request_id"],
              "account": request["account"], "operation": request["operation"],
              "request_hash": req_hash(request), "updated_at": NOW, "pace": {}}
        st.update(over)
        self.store.atomic_write(self.root / "status" / f"{request['request_id']}.json",
                                json.dumps(st).encode())
        return st

    def test_dry_run_is_read_only_and_reports_missing_pace(self):
        request = send_request("a", files=[self.attachment()])
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        reply = self.dry(request)
        self.assertEqual(reply["exit_code"], 0, reply)
        self.assertIn("адресат не проверен", reply["result"]["message"])
        self.assertIn("данные темпа отсутствуют", reply["result"]["message"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(self.store.log, [])

    def test_dry_run_requires_ready_heartbeat(self):
        (self.root / "heartbeat.json").unlink()
        self.assertEqual(self.dry(send_request())["code"], "OWNER_UNAVAILABLE")
        self.assertEqual(self.store.log, [])

    def test_dry_run_refuses_wrong_registry_root(self):
        self.env.accounts["a"]["owner_root"] = str(self.env.root("b"))
        self.env.write_registry()
        self.assertEqual(self.dry(send_request())["code"], "INVALID_CONFIG")

    def test_registry_loader_checks_all_entries_without_reading_auth(self):
        config = self.base / "client.json"
        config.write_text(json.dumps({"managed_accounts": {"a": str(self.root)},
                                     "installation_config": str(self.env.registry_path)}))
        original = self.store.read

        def guarded(path):
            self.assertNotIn(Path(path).name, ("auth.json", "a.session", "b.session"))
            return original(path)

        self.store.read = guarded
        result = self.client.load_installation_config(config, store=self.store)
        self.assertEqual(result["path"], self.env.registry_path)
        self.assertEqual(set(result["accounts"]), {"a", "b"})
        self.env.accounts["b"].pop("session_path")
        self.env.write_registry()
        with self.assertRaisesRegex(ValueError, "INVALID_CONFIG"):
            self.client.load_installation_config(config, store=self.store)

    def test_denylist_protects_other_account_hardlink_and_session_alias(self):
        alias = self.base / "alias.txt"
        os.link(self.env.accounts["b"]["auth_path"], alias)
        spec = {"source": str(alias), "name": alias.name, "size": 2,
                "sha256": hashlib.sha256(b"{}").hexdigest()}
        self.assertEqual(self.dry(send_request(files=[spec]))["code"], "ATTACHMENT_DENIED")
        spec = self.attachment(self.base / "alias2.txt")
        secret = Path(self.env.accounts["b"]["session_path"])
        info = self.store.inspect(secret)
        self.store.inspect_override[Path(spec["source"])] = {"dev": info["dev"], "ino": info["ino"]}
        self.assertEqual(self.dry(send_request(files=[spec]))["code"], "ATTACHMENT_DENIED")

    def test_source_parent_permissions_and_hash_are_checked(self):
        spec = self.attachment()
        self.store.inspect_override[Path(spec["source"])] = {"mode": 0o666}
        self.assertEqual(self.dry(send_request(files=[spec]))["code"], "UNSAFE_PATH")
        self.store.inspect_override.pop(Path(spec["source"]))
        spec["sha256"] = "0" * 64
        self.assertEqual(self.dry(send_request(files=[spec]))["code"], "ATTACHMENT_DENIED")

    def test_shared_writable_ancestor_requires_sticky_and_trusted_owner(self):
        directory = self.base / "sources"
        directory.mkdir(mode=0o700)
        spec = self.attachment(directory / "document.txt")
        for uid in (0, os.getuid(), os.getuid() + 1):
            for mode in (0o777, 0o770, 0o1777, 0o1770):
                with self.subTest(uid=uid, mode=oct(mode)):
                    self.store.inspect_override[directory] = {"uid": uid, "mode": mode}
                    safe = uid in (0, os.getuid()) and bool(mode & 0o1000)
                    reply = self.dry(send_request(files=[spec]))
                    self.assertEqual(reply["code"], "LOCAL_CHECK_OK" if safe else "UNSAFE_PATH")

    def test_sticky_does_not_relax_source_root_or_acl_checks(self):
        spec = self.attachment()
        cases = ((Path(spec["source"]), {"mode": 0o1666}),
                 (self.root, {"mode": 0o1777}),
                 (self.base, {"mode": 0o1777, "acl_safe": False}))
        for path, override in cases:
            with self.subTest(path=path):
                self.store.inspect_override[path] = override
                self.assertEqual(self.dry(send_request(files=[spec]))["code"], "UNSAFE_PATH")
                self.store.inspect_override.pop(path)

    def test_owner_ancestor_requires_sticky_and_trusted_owner(self):
        for mode, uid, safe in ((0o777, os.getuid(), False),
                                (0o1777, os.getuid() + 1, False),
                                (0o1777, 0, True), (0o1777, os.getuid(), True)):
            with self.subTest(mode=oct(mode), uid=uid):
                self.store.inspect_override[self.root.parent] = {"mode": mode, "uid": uid}
                self.assertEqual(self.dry(send_request())["code"],
                                 "LOCAL_CHECK_OK" if safe else "UNSAFE_PATH")

    def test_latest_status_uses_timestamp_then_id_and_ignores_other_account(self):
        req1 = send_request(rid="00000000-0000-4000-8000-000000000001")
        req2 = send_request(rid="00000000-0000-4000-8000-000000000002")
        self.put_status(req1)
        expected = self.put_status(req2)
        self.put_status(send_request("b"), updated_at=NOW + 100)
        result = self.client.read_last_account_status(self.root, "a", store=self.store)
        self.assertEqual(result, expected)
        self.put_status(req1, updated_at=NOW + 1)
        self.assertEqual(self.client.read_last_account_status(self.root, "a", store=self.store)["request_id"], req1["request_id"])

    def test_dry_run_pace_boundary_snapshot_and_bypass(self):
        request = send_request()
        previous = self.put_status(send_request(), pace={"User:123": {"aliases": ["me", 123],
                                                                   "next_allowed_at": NOW + 40}})
        reply = self.dry(request)
        self.assertEqual((reply["code"], reply["exit_code"], reply["retry_after"]), ("PACE_TOO_EARLY", 2, 40))
        self.assertEqual(reply["result"]["request_id"], previous["request_id"])
        self.clock.advance(40)
        write_heartbeat(self.root, now=self.clock.utc())
        self.assertEqual(self.dry(request)["exit_code"], 0)
        request["options"]["no_pace_check"] = True
        self.clock.advance(-40)
        write_heartbeat(self.root, now=self.clock.utc())
        self.assertEqual(self.dry(request)["exit_code"], 0)

    def test_busy_quota_waits_with_clock_and_does_not_publish(self):
        lock = self.store.lock(self.root / "quota.lock")
        try:
            for timeout in (0, 0.25, 2, 3):
                with self.subTest(timeout=timeout):
                    clock = FakeClock()
                    started = time.monotonic()
                    reply = self.client.submit(self.root, send_request(), clock=clock,
                                               timeout=timeout, store=self.store)
                    self.assertGreaterEqual(time.monotonic() - started, 2)
                    self.assertEqual((reply["code"], reply["exit_code"]), ("WAIT_TIMEOUT", 4))
                    self.assertGreaterEqual(sum(clock.sleeps) + 1e-9, timeout)
                    self.assertTrue(all(0 < delay <= 0.1 for delay in clock.sleeps))
                    for name in ("tmp", "outbox", "status"):
                        self.assertFalse((self.root / name).exists())
        finally:
            lock.__exit__(None, None, None)

    def test_quota_unlock_after_short_timeout_still_publishes(self):
        for timeout in (0, 0.25):
            with self.subTest(timeout=timeout), ExitStack() as held:
                clock = FakeClock()
                held.enter_context(self.store.lock(self.root / "quota.lock"))
                clock.on_sleep = lambda n: held.close() if n == 15 else None
                request = send_request()
                reply = self.client.submit(self.root, request, clock=clock,
                                           timeout=timeout, store=self.store)
                self.assertEqual((reply["code"], reply["exit_code"]), ("WAIT_TIMEOUT", 4))
                self.assertAlmostEqual(sum(clock.sleeps), 1.5)
                self.assertTrue((self.root / "outbox" / request["request_id"]).is_dir())
                self.assertFalse((self.root / "status").exists())

    def test_full_quota_after_unlock_overrides_short_timeout(self):
        for _ in range(100):
            write_package(self.root, send_request())
        for timeout in (0, 0.25):
            with self.subTest(timeout=timeout), ExitStack() as held:
                clock = FakeClock()
                held.enter_context(self.store.lock(self.root / "quota.lock"))
                clock.on_sleep = lambda n: held.close() if n == 15 else None
                reply = self.client.submit(self.root, send_request(), clock=clock,
                                           timeout=timeout, store=self.store)
                self.assertEqual((reply["code"], reply["exit_code"]), ("QUEUE_FULL", 2))
                self.assertAlmostEqual(sum(clock.sleeps), 1.5)
                self.assertEqual(len(list((self.root / "outbox").iterdir())), 100)
                self.assertFalse((self.root / "tmp").exists())
                self.assertFalse((self.root / "status").exists())

    def test_quota_unlock_and_result_share_one_deadline(self):
        lock = self.store.lock(self.root / "quota.lock")
        self.clock.on_sleep = lambda n: lock.__exit__(None, None, None) if n == 2 else None
        request = send_request()
        reply = self.client.submit(self.root, request, clock=self.clock, timeout=3, store=self.store)
        self.assertEqual(reply["code"], "WAIT_TIMEOUT")
        self.assertAlmostEqual(sum(self.clock.sleeps), 3)
        self.assertTrue((self.root / "outbox" / request["request_id"]).is_dir())

    def test_writer_lock_observed_through_publish(self):
        original = self.store.publish

        def publish(source, dest):
            with self.assertRaises(BlockingIOError):
                FakeStore().lock(source / ".writer.lock")
            self.assertTrue((source / ".reservation.json").exists())
            original(source, dest)
            with self.assertRaises(BlockingIOError):
                FakeStore().lock(dest / ".writer.lock")

        self.store.publish = publish
        request = send_request()
        reply = self.client.submit(self.root, request, clock=self.clock, timeout=0, store=self.store)
        self.assertEqual(reply["code"], "WAIT_TIMEOUT")
        with self.store.lock(self.root / "outbox" / request["request_id"] / ".writer.lock"):
            pass

    def test_failed_copy_releases_reservation_under_quota_lock(self):
        self.store.fail_on[("atomic_write", "request.json")] = ("after", OSError(errno.ENOSPC, "disk"))
        result = self.client.submit(self.root, send_request(), clock=self.clock, timeout=0, store=self.store)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(list((self.root / "tmp").iterdir()), [])
        self.assertEqual(self.client.submit(self.root, send_request(), clock=self.clock,
                                            timeout=0, store=self.store)["code"], "WAIT_TIMEOUT")

    def test_error_after_publish_keeps_accepted_package(self):
        original = self.store.publish

        def publish(source, dest):
            original(source, dest)
            raise OSError(errno.EIO, "fsync")

        self.store.publish = publish
        request = send_request()
        reply = self.client.submit(self.root, request, clock=self.clock, timeout=0, store=self.store)
        self.assertEqual(reply["code"], "WAIT_TIMEOUT")
        self.assertTrue((self.root / "outbox" / request["request_id"] / "request.json").exists())

    def test_spool_temporary_removed_after_listing_does_not_refuse_submit(self):
        temporary = self.root / "status" / ".atomic-write.tmp"
        self.store.atomic_write(temporary, b"")
        original = self.store.list

        def listing(path):
            paths = original(path)
            if temporary in paths:
                self.store.remove(temporary)
            return paths

        self.store.list = listing
        result = self.client.submit(self.root, send_request(), clock=self.clock, timeout=0, store=self.store)
        self.assertEqual(result["code"], "WAIT_TIMEOUT")

    def test_refresh_uses_same_spool(self):
        request = refresh_request()
        reply = self.client.refresh(self.root, request, clock=self.clock, timeout=0)
        self.assertEqual(reply["code"], "WAIT_TIMEOUT")
        self.assertEqual(json.loads((self.root / "outbox" / request["request_id"] / "request.json").read_bytes()), request)

    def race_requests(self, requests):
        barrier = threading.Barrier(len(requests))
        results = []
        attempts = set()
        guard = threading.Lock()

        class ObservedStore(import_owner().Store):
            def lock(self, path, *, shared=False):
                if Path(path).name == "quota.lock":
                    with guard:
                        ident = threading.get_ident()
                        first = ident not in attempts
                        attempts.add(ident)
                    if first:
                        # Synchronize inside the Store operation, before flock.
                        barrier.wait(timeout=5)
                return super().lock(path, shared=shared)

        store = ObservedStore()

        class Clock:
            def utc(self):
                return NOW

            def monotonic(self):
                import time
                return time.monotonic()

            def sleep_sync(self, seconds):
                import time
                time.sleep(seconds)

        def submit(request):
            results.append(self.client.submit(self.root, request, clock=Clock(), timeout=0, store=store))

        threads = [threading.Thread(target=submit, args=(request,)) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(attempts), len(requests))
        return results

    def test_concurrent_reservation_with_waits_preserves_last_slots(self):
        for _ in range(98):
            write_package(self.root, send_request())
        results = self.race_requests([send_request() for _ in range(4)])
        self.assertEqual(sorted(r["code"] for r in results), ["QUEUE_FULL", "QUEUE_FULL", "WAIT_TIMEOUT", "WAIT_TIMEOUT"])
        self.assertEqual(len(list((self.root / "outbox").iterdir())), 100)

    def test_concurrent_reservation_with_waits_preserves_last_bytes(self):
        package = write_package(self.root, send_request())
        payload = package / "payload.bin"
        with payload.open("wb") as stream:
            stream.truncate(512 * 1024 * 1024 - 1536 * 1024)
        payload.chmod(0o600)
        requests = []
        for index in range(2):
            path = self.base / f"attachment-{index}.bin"
            path.write_bytes(b"x" * (1024 * 1024))
            requests.append(send_request(files=[{"source": str(path), "name": path.name,
                                                 "size": 1024 * 1024,
                                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]))
        results = self.race_requests(requests)
        self.assertEqual(sorted(r["code"] for r in results), ["QUEUE_FULL", "WAIT_TIMEOUT"])
        self.assertEqual(len(list((self.root / "outbox").iterdir())), 2)

    def test_real_store_bounded_read_refuses_oversized_content(self):
        path = self.base / "large.json"
        path.write_bytes(b"x" * 1025)
        with self.assertRaises(OSError) as error:
            import_owner().Store().read_bounded(path, max_bytes=1024)
        self.assertEqual(error.exception.errno, errno.EFBIG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
