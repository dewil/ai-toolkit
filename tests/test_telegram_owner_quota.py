#!/usr/bin/env python3
"""RED: квота очереди владельца считает только ожидающие пакеты (решение 22.11).
PACKAGE_LIMIT=100 относится к пакетам без статуса, queued, sending. Терминальные
(sent, scheduled, refreshed, rejected, unknown) в предел не входят.
Запуск: python3 tests/test_telegram_owner_quota.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeClock, NOW, import_client, refresh_request, req_hash, send_request,
    write_heartbeat, write_package,
)

TERMINAL = {
    "sent": ("SENT", 0, NOW + 7 * 86400),
    "scheduled": ("SCHEDULED", 0, NOW + 14 * 86400),
    "refreshed": ("REFRESHED", 0, NOW + 7 * 86400),
    "rejected": ("LIMIT_EXCEEDED", 2, NOW + 7 * 86400),
    "unknown": ("SEND_UNCERTAIN", 4, None),
}
PENDING = {"queued": "QUEUED", "sending": "SENDING"}


def put_status(root: Path, req: dict, state: str, code: str, exit_code: int, retain):
    reply = {
        "schema_version": 1, "request_id": req["request_id"], "operation": req["operation"],
        "state": state, "code": code, "exit_code": exit_code, "retry_after": None,
        "result": {"message_ids": [7], "scheduled_ids": [], "at": None, "verification": "api_ack"},
        "request_hash": req_hash(req), "accepted_at": NOW, "updated_at": NOW,
        "retain_until": retain,
    }
    d = root / "status"
    d.mkdir(exist_ok=True, mode=0o700)
    p = d / f"{req['request_id']}.json"
    p.write_text(json.dumps(reply))
    os.chmod(p, 0o600)


class Quota(Base):
    def setUp(self):
        super().setUp()
        self.c = import_client()
        self.root = self.env.root("a")
        self.clock = FakeClock()
        write_heartbeat(self.root, now=self.clock.utc())

    def add_pending(self, n, with_status=None):
        for i in range(n):
            req = send_request("a")
            write_package(self.root, req)
            kind = with_status or ("none" if i % 3 == 0 else ("queued" if i % 3 == 1 else "sending"))
            if kind in PENDING:
                put_status(self.root, req, kind, PENDING[kind], 0, None)

    def add_terminal(self, n, state=None):
        states = list(TERMINAL)
        for i in range(n):
            st = state or states[i % len(states)]
            req = refresh_request("a") if st == "refreshed" else send_request("a")
            write_package(self.root, req)
            code, ec, retain = TERMINAL[st]
            put_status(self.root, req, st, code, ec, retain)

    def submit(self, req):
        return self.c.submit(self.root, req, clock=self.clock, timeout=0)

    def assertFull(self, r):
        self.assertEqual((r["state"], r["code"], r["exit_code"]), ("rejected", "QUEUE_FULL", 2))

    def assertAccepted(self, req, r):
        self.assertNotEqual(r["code"], "QUEUE_FULL", r)
        self.assertTrue((self.root / "outbox" / req["request_id"] / "request.json").exists())

    # --- терминальные не засчитываются (RED до правки) ---
    def test_100_terminal_mixed_then_new_send_accepted(self):
        self.add_terminal(100)
        req = send_request("a")
        self.assertAccepted(req, self.submit(req))

    def test_100_terminal_each_state_then_new_send_accepted(self):
        for st in TERMINAL:
            with self.subTest(st):
                self.setUp()
                self.add_terminal(100, st)
                req = send_request("a")
                self.assertAccepted(req, self.submit(req))

    def test_100_terminal_then_new_refresh_accepted(self):
        self.add_terminal(100)
        req = refresh_request("a")
        self.assertAccepted(req, self.submit(req))

    def test_99_pending_plus_50_terminal_accepted(self):
        self.add_pending(99)
        self.add_terminal(50)
        req = send_request("a")
        self.assertAccepted(req, self.submit(req))

    def test_99_pending_plus_50_terminal_refresh_accepted(self):
        self.add_pending(99)
        self.add_terminal(50)
        req = refresh_request("a")
        self.assertAccepted(req, self.submit(req))

    # --- ожидающие засчитываются (GREEN и сейчас) ---
    def test_100_pending_is_queue_full(self):
        self.add_pending(100)
        req = send_request("a")
        self.assertFull(self.submit(req))
        self.assertFalse((self.root / "outbox" / req["request_id"]).exists())

    def test_100_pending_refresh_is_queue_full(self):
        self.add_pending(100)
        req = refresh_request("a")
        self.assertFull(self.submit(req))
        self.assertFalse((self.root / "outbox" / req["request_id"]).exists())

    def test_100_pending_plus_50_terminal_is_queue_full(self):
        self.add_pending(100)
        self.add_terminal(50)
        self.assertFull(self.submit(send_request("a")))

    def test_100_pending_plus_50_terminal_refresh_is_queue_full(self):
        self.add_pending(100)
        self.add_terminal(50)
        self.assertFull(self.submit(refresh_request("a")))

    def test_100_pending_each_kind_is_queue_full(self):
        for kind in ("none", "queued", "sending"):
            with self.subTest(kind):
                self.setUp()
                self.add_pending(100, kind)
                self.assertFull(self.submit(send_request("a")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
