#!/usr/bin/env python3
"""RED: telegram_owner_client - готовность, публикация, ожидание, лимиты
(F3, F11, F12, F13, F22 квота/лимиты). Запуск: python3 tests/test_telegram_session_owner_client.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeClock, NOW, import_client, refresh_request, req_hash, send_request,
    mkdirs, write_file, write_heartbeat, write_package,
)


def terminal(rid, req, state="sent", code="SENT", exit_code=0, result=None):
    return {
        "schema_version": 1, "request_id": rid, "operation": req["operation"],
        "state": state, "code": code, "exit_code": exit_code, "retry_after": None,
        "result": result if result is not None else
        {"message_ids": [7], "scheduled_ids": [], "at": None, "verification": "api_ack"},
        "request_hash": req_hash(req), "accepted_at": NOW, "updated_at": NOW,
        "retain_until": NOW + 7 * 86400,
    }


def put_status(root: Path, reply: dict):
    d = root / "status"
    d.mkdir(exist_ok=True, mode=0o700)
    p = d / f"{reply['request_id']}.json"
    p.write_text(json.dumps(reply))
    os.chmod(p, 0o600)


class Readiness(Base):
    def setUp(self):
        super().setUp()
        self.c = import_client()
        self.root = self.env.root("a")
        self.clock = FakeClock()

    def submit(self, **kw):
        req = send_request("a", now=self.clock.utc())
        kw.setdefault("timeout", 1)
        return req, self.c.submit(self.root, req, clock=self.clock, **kw)

    def published(self, req):
        return (self.root / "outbox" / req["request_id"] / "request.json").exists()

    def test_age_boundaries_0_and_15_allow_publication(self):  # F3
        for age in (0.0, 15.0):
            with self.subTest(age=age):
                write_heartbeat(self.root, age=age, now=self.clock.utc())
                req, r = self.submit()
                self.assertTrue(self.published(req))
                self.assertNotEqual(r["exit_code"], 7)

    def test_unavailable_before_publication_is_7(self):  # F3, Q1
        cases = {
            "stale": lambda: write_heartbeat(self.root, age=15.5, now=self.clock.utc()),
            "future": lambda: write_heartbeat(self.root, age=-1.0, now=self.clock.utc()),
            "degraded": lambda: write_heartbeat(self.root, state="degraded", now=self.clock.utc()),
            "stopping": lambda: write_heartbeat(self.root, state="stopping", now=self.clock.utc()),
            "missing": lambda: None,
            "bad_json": lambda: write_file(self.root / "heartbeat.json", "{not json"),
            "bad_version": lambda: write_heartbeat(self.root, schema_version=2, now=self.clock.utc()),
            "extra_field": lambda: write_heartbeat(self.root, extra=1, now=self.clock.utc()),
            "bad_pid": lambda: write_heartbeat(self.root, pid=0, now=self.clock.utc()),
            "bad_state": lambda: write_heartbeat(self.root, state="weird", now=self.clock.utc()),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                hb = self.root / "heartbeat.json"
                if hb.exists():
                    hb.unlink()
                mutate()
                req, r = self.submit()
                self.assertEqual((r["state"], r["code"], r["exit_code"]),
                                 ("unavailable", "OWNER_UNAVAILABLE", 7))
                self.assertFalse(self.published(req))
                self.assertEqual(list((self.root / "outbox").glob("*")) if (self.root / "outbox").exists() else [], [])
                self.assertEqual(self.clock.sleeps, [])  # без сна

    def test_status_never_needs_heartbeat(self):  # F13
        req = send_request("a")
        put_status(self.root, terminal(req["request_id"], req))
        r = self.c.status(self.root, req["request_id"], clock=self.clock)
        self.assertEqual((r["state"], r["code"], r["exit_code"]), ("sent", "SENT", 0))
        self.assertEqual(self.clock.sleeps, [])

    def test_status_unknown_id_not_found(self):  # F13
        r = self.c.status(self.root, str(uuid.uuid4()), clock=self.clock)
        self.assertEqual((r["state"], r["code"], r["exit_code"]), ("absent", "NOT_FOUND", 2))

    def test_status_invalid_id_checked_before_heartbeat(self):  # спека: id проверяется до heartbeat
        r = self.c.status(self.root, "../../etc/passwd", clock=self.clock)
        self.assertEqual(r["exit_code"], 2)


class WaitTimeout(Base):
    def setUp(self):
        super().setUp()
        self.c = import_client()
        self.root = self.env.root("a")
        self.clock = FakeClock()
        write_heartbeat(self.root, now=self.clock.utc())

    def test_dead_owner_wait_timeout_4_queued_no_status_change(self):  # F13
        req = send_request("a")
        r = self.c.submit(self.root, req, clock=self.clock, timeout=0.35)
        self.assertEqual((r["state"], r["code"], r["exit_code"]), ("queued", "WAIT_TIMEOUT", 4))
        self.assertTrue((self.root / "outbox" / req["request_id"] / "request.json").exists())
        self.assertFalse((self.root / "status" / f"{req['request_id']}.json").exists())
        self.assertTrue(all(0 < s <= 0.1 + 1e-9 for s in self.clock.sleeps))
        self.assertAlmostEqual(sum(self.clock.sleeps), 0.35, places=6)
        self.assertEqual(self.clock.async_sleeps, [])

    def test_timeout_keeps_last_sending_state(self):  # F13
        req = send_request("a")
        sending = terminal(req["request_id"], req, state="sending", code="SENDING", exit_code=0, result={})
        r_state = {}

        def on_sleep(n):
            if n == 1:
                put_status(self.root, sending)

        self.clock.on_sleep = on_sleep
        r = self.c.submit(self.root, req, clock=self.clock, timeout=0.5)
        self.assertEqual((r["state"], r["code"], r["exit_code"]), ("sending", "WAIT_TIMEOUT", 4))
        self.assertEqual(json.loads((self.root / "status" / f"{req['request_id']}.json").read_text()), sending)

    def test_terminal_appearing_during_wait_returned(self):  # F13
        req = send_request("a")
        done = terminal(req["request_id"], req)
        self.clock.on_sleep = lambda n: put_status(self.root, done) if n == 2 else None
        r = self.c.submit(self.root, req, clock=self.clock, timeout=5)
        self.assertEqual((r["state"], r["code"], r["exit_code"]), ("sent", "SENT", 0))
        self.assertEqual(len(self.clock.sleeps), 2)

    def test_status_read_before_deadline_check(self):  # F13: terminal уже есть при timeout=0
        req = send_request("a")
        self.c.submit(self.root, req, clock=self.clock, timeout=0)  # публикует
        put_status(self.root, terminal(req["request_id"], req))
        r = self.c.submit(self.root, req, clock=self.clock, timeout=0)
        self.assertEqual(r["code"], "SENT")
        self.assertEqual(self.clock.sleeps, [])

    def test_zero_timeout_no_status_no_sleep(self):  # F13
        req = send_request("a")
        r = self.c.submit(self.root, req, clock=self.clock, timeout=0)
        self.assertEqual((r["code"], r["exit_code"]), ("WAIT_TIMEOUT", 4))
        self.assertEqual(self.clock.sleeps, [])

    def test_republished_id_after_owner_lost_is_wait_timeout_not_7(self):  # F3/F13
        req = send_request("a")
        self.c.submit(self.root, req, clock=self.clock, timeout=0)
        os.remove(self.root / "heartbeat.json")
        r = self.c.submit(self.root, req, clock=self.clock, timeout=0.2)
        self.assertEqual((r["code"], r["exit_code"]), ("WAIT_TIMEOUT", 4))
        outbox = [p.name for p in (self.root / "outbox").iterdir()]
        self.assertEqual(outbox, [req["request_id"]])  # без второго пакета


class PublishAtomic(Base):
    def setUp(self):
        super().setUp()
        self.c = import_client()
        self.root = self.env.root("a")
        self.clock = FakeClock()
        write_heartbeat(self.root, now=self.clock.utc())

    def test_package_built_in_tmp_then_renamed_to_outbox(self):  # F11
        req = send_request("a")
        self.c.submit(self.root, req, clock=self.clock, timeout=0)
        out = self.root / "outbox" / req["request_id"] / "request.json"
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["request_id"], req["request_id"])
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "outbox" / req["request_id"]).stat().st_mode & 0o777, 0o700)
        left = list((self.root / "tmp").iterdir()) if (self.root / "tmp").exists() else []
        self.assertEqual(left, [])

    def test_same_id_same_hash_after_terminal_returns_status_no_resend(self):  # F12
        req = send_request("a")
        put_status(self.root, terminal(req["request_id"], req))
        r = self.c.submit(self.root, req, clock=self.clock, timeout=0)
        self.assertEqual((r["code"], r["exit_code"]), ("SENT", 0))
        self.assertFalse((self.root / "outbox" / req["request_id"]).exists())

    def test_same_id_other_payload_conflict_status_untouched(self):  # F12
        req = send_request("a")
        st = terminal(req["request_id"], req)
        put_status(self.root, st)
        other = dict(req, text="Другой текст")
        r = self.c.submit(self.root, other, clock=self.clock, timeout=0)
        self.assertEqual((r["state"], r["code"], r["exit_code"]), ("rejected", "REQUEST_ID_CONFLICT", 2))
        self.assertEqual(json.loads((self.root / "status" / f"{req['request_id']}.json").read_text()), st)
        self.assertFalse((self.root / "outbox" / req["request_id"]).exists())

    def test_expired_request_rejected_2(self):  # F11/спека
        req = send_request("a", expires_at=int(NOW) - 1, created_at=int(NOW) - 100)
        r = self.c.submit(self.root, req, clock=self.clock, timeout=0)
        self.assertEqual((r["code"], r["exit_code"]), ("REQUEST_EXPIRED", 2))

    def test_invalid_envelope_rejected_before_publication(self):  # F11
        bad = []
        r1 = send_request("a"); del r1["text"]; bad.append(r1)
        r2 = send_request("a"); r2["schema_version"] = 2; bad.append(r2)
        r3 = send_request("a"); r3["options"]["unknown"] = True; bad.append(r3)
        r4 = send_request("a", created_at=int(NOW) + 60); bad.append(r4)
        r5 = send_request("a", expires_at=int(NOW) + 8 * 86400); bad.append(r5)
        r6 = send_request("a"); r6["target"]["topic_id"] = 0; bad.append(r6)
        r7 = send_request("a"); r7["request_id"] = "not-a-uuid"; bad.append(r7)
        for i, req in enumerate(bad):
            with self.subTest(i=i):
                r = self.c.submit(self.root, req, clock=self.clock, timeout=0)
                self.assertEqual(r["exit_code"], 2)
                self.assertIn(r["code"], ("INVALID_REQUEST", "REQUEST_EXPIRED"))
                outbox = self.root / "outbox"
                self.assertEqual(list(outbox.iterdir()) if outbox.exists() else [], [])


class Limits(Base):
    def setUp(self):
        super().setUp()
        self.c = import_client()
        self.root = self.env.root("a")
        self.clock = FakeClock()
        write_heartbeat(self.root, now=self.clock.utc())

    def sub(self, req):
        return self.c.submit(self.root, req, clock=self.clock, timeout=0)

    def test_text_utf16_limit(self):  # F22 4096 единиц UTF-16
        ok = self.sub(send_request("a", text="\U0001F600" * 2048))  # 4096 единиц
        self.assertNotEqual(ok["code"], "LIMIT_EXCEEDED")
        over = self.sub(send_request("a", text="\U0001F600" * 2048 + "x"))
        self.assertEqual((over["code"], over["exit_code"]), ("LIMIT_EXCEEDED", 2))

    def test_caption_limit_1024_with_files(self):  # F22
        f = self.base / "f.txt"
        write_file(f, b"x")
        import hashlib
        spec = {"source": str(f), "name": "f.txt", "size": 1,
                "sha256": hashlib.sha256(b"x").hexdigest()}
        r = self.sub(send_request("a", text="я" * 1025, files=[spec]))
        self.assertEqual((r["code"], r["exit_code"]), ("LIMIT_EXCEEDED", 2))

    def test_eleven_files_limit(self):  # F20/F22
        import hashlib
        specs = []
        for i in range(11):
            f = self.base / f"f{i}.txt"
            write_file(f, b"x")
            specs.append({"source": str(f), "name": f.name, "size": 1,
                          "sha256": hashlib.sha256(b"x").hexdigest()})
        r = self.sub(send_request("a", files=specs))
        self.assertEqual((r["code"], r["exit_code"]), ("LIMIT_EXCEEDED", 2))

    def test_request_json_over_64k_limit(self):  # F22
        r = self.sub(send_request("a", text="x" * 70000))
        self.assertEqual((r["code"], r["exit_code"]), ("LIMIT_EXCEEDED", 2))

    def test_queue_full_at_100_packages_status_still_readable(self):  # F22
        for _ in range(100):
            write_package(self.root, send_request("a"))
        req = send_request("a")
        r = self.sub(req)
        self.assertEqual((r["state"], r["code"], r["exit_code"]), ("rejected", "QUEUE_FULL", 2))
        self.assertFalse((self.root / "outbox" / req["request_id"]).exists())
        rid = str(uuid.uuid4())
        st = self.c.status(self.root, rid, clock=self.clock)
        self.assertEqual(st["code"], "NOT_FOUND")

    def test_tmp_counts_toward_quota(self):  # F22
        for _ in range(100):
            write_package(self.root, send_request("a"), where="tmp")
        r = self.sub(send_request("a"))
        self.assertEqual(r["code"], "QUEUE_FULL")


class QuotaRace(Base):
    """Спека: quota.lock общий, резерв атомарен - параллельные приемы не тратят один остаток (уточнение 15)."""

    def setUp(self):
        super().setUp()
        self.c = import_client()
        self.root = self.env.root("a")
        self.clock = FakeClock()
        write_heartbeat(self.root, now=self.clock.utc())

    def race(self, requests):
        import threading
        barrier = threading.Barrier(len(requests))
        results = [None] * len(requests)

        def work(i, req):
            barrier.wait()
            results[i] = self.c.submit(self.root, req, clock=FakeClock(), timeout=0)

        threads = [threading.Thread(target=work, args=(i, r)) for i, r in enumerate(requests)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return results

    def spool_count(self):
        n = 0
        for d in ("outbox", "tmp"):
            if (self.root / d).exists():
                n += len(list((self.root / d).iterdir()))
        return n

    def test_race_for_last_package_slots_never_exceeds_quota(self):  # F22
        for _ in range(98):
            write_package(self.root, send_request("a"))
        results = self.race([send_request("a") for _ in range(8)])
        accepted = [r for r in results if r["code"] != "QUEUE_FULL"]
        self.assertEqual(len(accepted), 2, [r["code"] for r in results])
        self.assertEqual(sum(1 for r in results if r["code"] == "QUEUE_FULL"), 6)
        self.assertEqual(self.spool_count(), 100)

    def test_race_for_last_bytes_only_one_fits(self):  # F22: 512 MiB спула
        big = self.root / "outbox" / str(uuid.uuid4())
        mkdirs(big)
        with open(big / "payload.bin", "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.truncate(512 * 1024 * 1024 - 1536 * 1024)  # остается 1.5 MiB
        import hashlib
        reqs = []
        for i in range(2):
            f = self.base / f"att{i}.bin"
            write_file(f, b"a" * (1024 * 1024))
            spec = {"source": str(f), "name": f.name, "size": 1024 * 1024,
                    "sha256": hashlib.sha256(b"a" * (1024 * 1024)).hexdigest()}
            reqs.append(send_request("a", files=[spec]))
        results = self.race(reqs)
        codes = sorted(r["code"] for r in results)
        self.assertEqual(codes.count("QUEUE_FULL"), 1, codes)

    def test_rejection_releases_reservation(self):  # F22: отказ снимает резерв
        pkgs = [write_package(self.root, send_request("a")) for _ in range(100)]
        for _ in range(3):
            self.assertEqual(self.c.submit(self.root, send_request("a"), clock=self.clock,
                                           timeout=0)["code"], "QUEUE_FULL")
        import shutil
        shutil.rmtree(pkgs[0])
        r = self.c.submit(self.root, send_request("a"), clock=self.clock, timeout=0)
        self.assertNotEqual(r["code"], "QUEUE_FULL")  # освободилось ровно одно место


if __name__ == "__main__":
    unittest.main(verbosity=2)
