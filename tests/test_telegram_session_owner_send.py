#!/usr/bin/env python3
"""RED: Owner.step - обработка заявок send (F11-F22, F25, F26).

Запуск: python3 tests/test_telegram_session_owner_send.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeClock, FakeRNG, FakeStore, FakeTelegram, FloodWait, NOW, import_owner, read_status,
    run, send_request, write_package,
)


def iso(unix: float) -> str:
    return dt.datetime.fromtimestamp(unix, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def to_unix(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return dt.datetime.fromisoformat(v).timestamp()


def fspec(path: Path) -> dict:
    data = Path(path).read_bytes()
    return {"source": str(path), "name": Path(path).name, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


class OwnerCase(Base):
    def make(self, acc="a", **kw):
        o = self.env.make_owner(acc, **kw)
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        return o

    def deliver(self, owner, req, acc="a", steps=6):
        root = self.env.root(acc)
        write_package(root, req)
        for _ in range(steps):
            if not run(owner.step()):
                break
        return read_status(root, req["request_id"])

    def deliver_ok(self, owner, text="Проверка", **kw):
        req = send_request(kw.pop("acc", "a"), text=text, **kw)
        st = self.deliver(owner, req)
        return req, st


class Basics(OwnerCase):
    def test_send_me_one_id_sent_status(self):  # F25/F12
        o = self.make()
        req, st = self.deliver_ok(o)
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("sent", "SENT", 0))
        self.assertEqual(st["result"]["verification"], "api_ack")
        self.assertEqual(len(st["result"]["message_ids"]), 1)
        self.assertEqual(len(o.fake.tg.sends()), 1)
        self.assertEqual(st["request_hash"], hashlib.sha256(json.dumps(
            req, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())

    def test_step_idle_returns_false(self):
        o = self.make()
        self.assertFalse(run(o.step()))

    def test_tmp_package_ignored_until_published(self):  # F11
        o = self.make()
        req = send_request("a")
        d = write_package(self.env.root("a"), req, where="tmp", nonce=str(uuid.uuid4()))
        self.assertFalse(run(o.step()))
        self.assertEqual(o.fake.tg.sends(), [])
        (self.env.root("a") / "outbox").mkdir(exist_ok=True, mode=0o700)
        os.rename(d, self.env.root("a") / "outbox" / req["request_id"])
        self.assertTrue(run(o.step()))
        self.assertEqual(len(o.fake.tg.sends()), 1)

    def test_invalid_schema_rejected_without_send(self):  # F11
        o = self.make()
        bad = send_request("a")
        del bad["target"]
        st = self.deliver(o, bad)
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", "INVALID_REQUEST", 2))
        self.assertEqual(o.fake.tg.sends(), [])

    def test_request_json_symlink_rejected(self):  # F11
        o = self.make()
        req = send_request("a")
        outside = self.base / "outside.json"
        outside.write_text(json.dumps(req))
        d = self.env.root("a") / "outbox" / req["request_id"]
        d.mkdir(parents=True, mode=0o700)
        (d / "request.json").symlink_to(outside)
        run(o.step())
        st = read_status(self.env.root("a"), req["request_id"])
        self.assertTrue(st is None or st["exit_code"] == 2)
        self.assertEqual(o.fake.tg.sends(), [])

    def test_file_sources_dotdot_missing_and_hash_mismatch_rejected(self):  # F11/F20
        o = self.make()
        good = self.base / "ok.txt"
        good.write_bytes(b"data")
        bad_specs = {
            "missing": dict(fspec(good), source=str(self.base / "nope.txt"), name="nope.txt"),
            "dotdot": dict(fspec(good), source=str(self.base / "x" / ".." / "ok.txt")),
            "relative": dict(fspec(good), source="ok.txt"),
            "bad_sha": dict(fspec(good), sha256="0" * 64),
            "bad_size": dict(fspec(good), size=99),
            "name_not_basename": dict(fspec(good), name="d/ok.txt"),
        }
        for name, spec in bad_specs.items():
            with self.subTest(name):
                st = self.deliver(o, send_request("a", files=[spec]))
                self.assertEqual(st["exit_code"], 2)
                self.assertEqual(o.fake.tg.sends(), [])
                self.assertEqual(o.fake.tg.uploads(), [])

    def test_symlink_source_rejected(self):  # F11/F22
        o = self.make()
        real = self.base / "real.txt"
        real.write_bytes(b"x")
        link = self.base / "link.txt"
        link.symlink_to(real)
        st = self.deliver(o, send_request("a", files=[fspec(link)]))
        self.assertEqual((st["code"], st["exit_code"]), ("ATTACHMENT_DENIED", 2))
        self.assertEqual(o.fake.tg.sends(), [])

    def test_upload_reads_copy_inside_package_not_source(self):  # спека Files
        o = self.make()
        f = self.base / "doc.txt"
        f.write_bytes(b"payload")
        st = self.deliver(o, send_request("a", files=[fspec(f)]))
        self.assertEqual(st["code"], "SENT")
        path = o.fake.tg.uploads()[0][1]
        self.assertNotEqual(path, f)
        self.assertTrue(str(path).startswith(str(self.env.root("a"))))

    def test_internal_copy_denied_as_source(self):  # F22
        o = self.make()
        inner = self.env.root("a") / "spool.txt"
        inner.write_bytes(b"x")
        st = self.deliver(o, send_request("a", files=[fspec(inner)]))
        self.assertEqual((st["code"], st["exit_code"]), ("ATTACHMENT_DENIED", 2))


class AccountIsolation(OwnerCase):
    def test_foreign_account_in_package_rejected_without_send(self):  # F25
        o = self.make("a")
        st = self.deliver(o, send_request("b"))
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", "ACCOUNT_NOT_MANAGED", 2))
        self.assertEqual(o.fake.tg.sends(), [])

    def test_spool_and_telegram_of_selected_account_only(self):  # F25/F26
        oa, ob = self.make("a"), self.make("b")
        req = send_request("b")
        write_package(self.env.root("b"), req)
        run(oa.step())
        run(ob.step())
        self.assertEqual(oa.fake.tg.sends(), [])
        self.assertEqual(len(ob.fake.tg.sends()), 1)
        self.assertIsNone(read_status(self.env.root("a"), req["request_id"]))

    def test_flood_wait_of_a_does_not_block_b(self):  # F26
        oa, ob = self.make("a"), self.make("b")
        oa.fake.tg.send_results = [FloodWait(600)]
        write_package(self.env.root("a"), send_request("a"))
        run(oa.step())
        req_b = send_request("b")
        st_b = self.deliver(ob, req_b, acc="b")
        self.assertEqual(st_b["code"], "SENT")


class Pace(OwnerCase):
    def test_second_send_same_peer_pace_too_early_with_retry_after(self):  # F14/F15
        o = self.make(rng=FakeRNG(uniform=0.0))
        self.deliver_ok(o)
        r2, st = self.deliver_ok(o, text="второе")
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", "PACE_TOO_EARLY", 3))
        self.assertAlmostEqual(st["retry_after"], 40.0, delta=0.5)
        self.assertEqual(len(o.fake.tg.sends()), 1)
        self.assertEqual(o.fake.clock.async_sleeps, [])  # без сна/переноса
        self.assertEqual(o.fake.clock.sleeps, [])

    def test_gap_0_chars_is_40_regardless_of_rng(self):  # F15
        o = self.make(rng=FakeRNG(uniform=0.35))
        self.deliver_ok(o, text="")
        o.fake.clock.advance(39)
        _, st = self.deliver_ok(o, text="x")
        self.assertEqual(st["code"], "PACE_TOO_EARLY")
        o.fake.clock.advance(2)
        _, st = self.deliver_ok(o, text="y")
        self.assertEqual(st["code"], "SENT")

    def test_gap_600_chars_cap_180_and_rng_once(self):  # F15
        rng = FakeRNG(uniform=0.0)
        o = self.make(rng=rng)
        self.deliver_ok(o, text="а" * 600)
        self.assertEqual(rng.uniform_calls, [(0, 0.35)])
        o.fake.clock.advance(179)
        _, st = self.deliver_ok(o, text="x")
        self.assertEqual(st["code"], "PACE_TOO_EARLY")
        o.fake.clock.advance(2)
        _, st = self.deliver_ok(o, text="y")
        self.assertEqual(st["code"], "SENT")

    def test_gap_600_chars_with_max_jitter_is_117(self):  # F15
        o = self.make(rng=FakeRNG(uniform=0.35))
        self.deliver_ok(o, text="а" * 600)
        o.fake.clock.advance(118)
        _, st = self.deliver_ok(o, text="x")
        self.assertEqual(st["code"], "SENT")

    def test_pace_survives_owner_restart(self):  # F15
        o = self.make(rng=FakeRNG())
        self.deliver_ok(o)
        run(o.close())
        o2 = self.env.make_owner("a", clock=o.fake.clock, rng=FakeRNG())
        run(o2.start())
        self.addCleanup(lambda: run(o2.close()))
        req = send_request("a", text="после рестарта")
        st = self.deliver(o2, req)
        self.assertEqual(st["code"], "PACE_TOO_EARLY")

    def test_bypass_option_skips_gate(self):  # F15
        o = self.make()
        self.deliver_ok(o)
        req = send_request("a", text="срочно")
        req["options"]["no_pace_check"] = True
        st = self.deliver(o, req)
        self.assertEqual(st["code"], "SENT")

    def test_different_peers_and_kinds_independent(self):  # F14
        tg = FakeTelegram("a")
        tg.peer_map = {"me": {"kind": "User", "id": 1}, 42: {"kind": "User", "id": 42},
                       "chan": {"kind": "Channel", "id": 42}}
        o = self.make(tg=tg)
        for peer in ("me", 42, "chan"):
            st = self.deliver(o, send_request("a", peer=peer))
            self.assertEqual(st["code"], "SENT", peer)

    def test_peer_aliases_share_gate(self):  # F14: me и username/числовой id одного peer
        tg = FakeTelegram("a")
        tg.peer_map = {"me": {"kind": "User", "id": 7, "username": "owner_me"},
                       7: {"kind": "User", "id": 7, "username": "owner_me"},
                       "owner_me": {"kind": "User", "id": 7, "username": "owner_me"}}
        o = self.make(tg=tg)
        self.assertEqual(self.deliver(o, send_request("a", peer="me"))["code"], "SENT")
        for alias in (7, "owner_me"):
            st = self.deliver(o, send_request("a", peer=alias))
            self.assertEqual(st["code"], "PACE_TOO_EARLY", alias)

    def test_unknown_outcome_reserves_pace(self):  # F15
        tg = FakeTelegram("a")
        tg.send_results = [import_owner().SendUncertain(message_ids=[])]
        o = self.make(tg=tg)
        st = self.deliver(o, send_request("a"))
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("unknown", "SEND_UNCERTAIN", 4))
        st2 = self.deliver(o, send_request("a", text="ещё"))
        self.assertEqual(st2["code"], "PACE_TOO_EARLY")
        self.assertEqual(len(tg.sends()), 1)


class Schedule(OwnerCase):
    def sched(self, at, **sch):
        s = {"at": at, "timezone": None, "exact_minute": False, "remind": False}
        s.update(sch)
        return s

    def test_lead_boundaries_and_round_minute(self):  # F16
        o = self.make()
        o.fake.clock.advance(0)
        cases = [
            (iso(NOW + 119), {}, ("rejected", 2)),
            (iso(NOW + 120), {}, ("scheduled", 0)),
            (iso(NOW - 60), {}, ("rejected", 2)),
            (iso(NOW + 366 * 86400), {"exact_minute": True}, ("scheduled", 0)),
            (iso(NOW + 366 * 86400 + 1), {"exact_minute": True}, ("rejected", 2)),
        ]
        for at, sch, (state, code) in cases:
            with self.subTest(at=at):
                o.fake.tg.scheduled_data = [{"id": 900, "at": to_unix(at), "text": "Проверка"}]
                o.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [900], "at": to_unix(at)}]
                o.fake.clock.advance(300)
                req = send_request("a", schedule=self.sched(at, **sch))
                req["created_at"] = int(o.fake.clock.utc())
                req["expires_at"] = req["created_at"] + 86400
                # at пересчитан относительно текущих часов теста
                shift = o.fake.clock.utc() - NOW
                req["schedule"]["at"] = iso(to_unix(at) + shift)
                st = self.deliver(o, req)
                self.assertEqual(st["state"], state)
                self.assertEqual(st["exit_code"], code)

    def test_round_minute_without_exact_is_6(self):  # F16
        o = self.make()
        at = iso(NOW + 3600)  # 09:00:00 UTC - ровная минута
        st = self.deliver(o, send_request("a", schedule=self.sched(at)))
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", "ROUND_MINUTE", 6))
        self.assertEqual(o.fake.tg.sends(), [])
        at2 = iso(NOW + 3600 + 7 * 60)
        o.fake.tg.scheduled_data = [{"id": 1, "at": to_unix(at2), "text": "Проверка"}]
        o.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [1], "at": to_unix(at2)}]
        st = self.deliver(o, send_request("a", schedule=self.sched(at2)))
        self.assertEqual(st["code"], "SCHEDULED")

    def test_iana_dst_offset_conversion(self):  # F16
        o = self.make()
        # 2027-03-28 02:30 Europe/Berlin не существует (переход на летнее время)
        st = self.deliver(o, send_request("a", schedule=self.sched("2027-03-28T02:30:00", timezone="Europe/Berlin")))
        self.assertEqual((st["state"], st["exit_code"]), ("rejected", 2))
        # явный оффсет, не совпавший с поясом
        st = self.deliver(o, send_request("a", schedule=self.sched("2027-02-20T10:07:00+05:00", timezone="Europe/Berlin")))
        self.assertEqual(st["exit_code"], 2)
        self.assertEqual(o.fake.tg.sends(), [])
        # unknown zone
        st = self.deliver(o, send_request("a", schedule=self.sched("2027-02-20T10:07:00", timezone="Mars/Base")))
        self.assertEqual(st["exit_code"], 2)

    def test_lead_lost_after_upload_rejected_no_send(self):  # F17
        o = self.make()
        f = self.base / "a.txt"
        f.write_bytes(b"x")
        at = iso(NOW + 125)
        o.fake.tg.hooks["upload"] = lambda: o.fake.clock.advance(10)  # запас потерян
        req = send_request("a", files=[fspec(f)], schedule=self.sched(at))
        st = self.deliver(o, req)
        self.assertEqual((st["state"], st["exit_code"]), ("rejected", 2))
        self.assertEqual(o.fake.tg.sends(), [])  # без мгновенной отправки

    def test_lead_lost_in_spool_rejected(self):  # F17
        o = self.make()
        req = send_request("a", schedule=self.sched(iso(NOW + 125)))
        write_package(self.env.root("a"), req)
        o.fake.clock.advance(60)
        run(o.step())
        st = read_status(self.env.root("a"), req["request_id"])
        self.assertEqual((st["state"], st["exit_code"]), ("rejected", 2))
        self.assertEqual(o.fake.tg.sends(), [])

    def test_scheduled_mismatch_text_or_date_unknown(self):  # F19
        for bad in ({"id": 900, "at": NOW + 3607.0, "text": "Другой"},
                    {"id": 900, "at": NOW + 7200.0, "text": "Проверка"},
                    None):
            with self.subTest(bad=bad):
                o = self.make()
                at = iso(NOW + 3607)
                o.fake.tg.scheduled_data = [bad] if bad else []
                o.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [900], "at": NOW + 3607.0}]
                st = self.deliver(o, send_request("a", peer=77, schedule=self.sched(at)))
                self.assertEqual((st["state"], st["code"], st["exit_code"]), ("unknown", "SEND_UNCERTAIN", 4))
                run(o.close())

    def test_scheduled_verified(self):  # F19
        o = self.make()
        at = iso(NOW + 3607)
        o.fake.tg.scheduled_data = [{"id": 900, "at": NOW + 3607.0, "text": "Проверка"}]
        o.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [900], "at": NOW + 3607.0}]
        st = self.deliver(o, send_request("a", peer=77, schedule=self.sched(at)))
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("scheduled", "SCHEDULED", 0))
        self.assertEqual(st["result"]["scheduled_ids"], [900])

    def test_near_me_unverifiable_saved(self):  # F19
        o = self.make()
        at = iso(NOW + 125)
        o.fake.tg.scheduled_data = []
        o.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [901], "at": NOW + 125.0}]
        st = self.deliver(o, send_request("a", peer="me", schedule=self.sched(at)))
        self.assertEqual((st["state"], st["exit_code"]), ("scheduled", 0))
        self.assertEqual(st["result"]["verification"], "unavailable_saved_near")


class Remind(OwnerCase):
    def remind_req(self, **over):
        return send_request("a", peer="me", text="Напомнить",
                            schedule={"at": None, "timezone": None, "exact_minute": False, "remind": True}, **over)

    def scheduled_at(self, o):
        (_, request, _peer, _up), = o.fake.tg.sends()
        return to_unix(request["schedule"]["at"])

    def test_rng_1_and_59_give_181_and_239_once(self):  # F18
        for r, expected in ((1, 181), (59, 239)):
            with self.subTest(r=r):
                rng = FakeRNG(randint=r)
                o = self.make(rng=rng)
                o.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [5], "at": NOW + expected}]
                o.fake.tg.scheduled_data = [{"id": 5, "at": NOW + expected, "text": "Напомнить"}]
                st = self.deliver(o, self.remind_req())
                self.assertEqual(st["exit_code"], 0, st)
                self.assertEqual(self.scheduled_at(o), NOW + expected)
                self.assertEqual(rng.randint_calls, [(1, 59)])
                run(o.close())

    def test_remind_bypasses_pace_and_round_minute(self):  # F18
        o = self.make(rng=FakeRNG(randint=30))
        self.deliver_ok(o)  # занимает темп me
        o.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [5], "at": NOW + 210}]
        st = self.deliver(o, self.remind_req())
        self.assertNotEqual(st["code"], "PACE_TOO_EARLY")
        self.assertNotEqual(st["code"], "ROUND_MINUTE")

    def test_remind_invalid_combinations_rejected(self):  # F18
        o = self.make(rng=FakeRNG(randint=1))
        bad = {
            "not_me": send_request("a", peer=42, schedule={"at": None, "timezone": None, "exact_minute": False, "remind": True}),
            "silent": dict(self.remind_req(), options={"silent": True, "html": False, "voice": False, "no_pace_check": False}),
            "with_at": send_request("a", peer="me", schedule={"at": iso(NOW + 500), "timezone": None, "exact_minute": False, "remind": True}),
            "with_tz": send_request("a", peer="me", schedule={"at": None, "timezone": "Europe/Berlin", "exact_minute": False, "remind": True}),
        }
        for name, req in bad.items():
            with self.subTest(name):
                st = self.deliver(o, req)
                self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", "INVALID_REQUEST", 2))
        self.assertEqual(o.fake.tg.sends(), [])

    def test_repeat_same_id_does_not_shift_time(self):  # F18
        rng = FakeRNG(randint=1)
        o = self.make(rng=rng)
        o.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [5], "at": NOW + 181}]
        o.fake.tg.scheduled_data = [{"id": 5, "at": NOW + 181, "text": "Напомнить"}]
        req = self.remind_req()
        self.deliver(o, req)
        o.fake.clock.advance(50)
        write_package(self.env.root("a"), req)  # повтор того же id
        run(o.step())
        self.assertEqual(len(o.fake.tg.sends()), 1)
        self.assertEqual(len(rng.randint_calls), 1)


class AlbumAndLimits(OwnerCase):
    def files(self, n):
        out = []
        for i in range(n):
            f = self.base / f"f{i}.txt"
            f.write_bytes(f"d{i}".encode())
            out.append(fspec(f))
        return out

    def test_file_counts(self):  # F20
        for n, code in ((1, "SENT"), (2, "SENT"), (10, "SENT"), (11, "LIMIT_EXCEEDED")):
            with self.subTest(n=n):
                o = self.make(rng=FakeRNG())
                st = self.deliver(o, send_request("a", files=self.files(n)))
                self.assertEqual(st["code"], code)
                if code == "SENT":
                    self.assertEqual(len(o.fake.tg.uploads()), n)
                else:
                    self.assertEqual(o.fake.tg.sends(), [])
                run(o.close())

    def test_album_incompatible_flags(self):  # F20
        o = self.make()
        for name, over in (("html", {"html": True}), ("voice", {"voice": True})):
            with self.subTest(name):
                req = send_request("a", files=self.files(2))
                req["options"].update(over)
                st = self.deliver(o, req)
                self.assertEqual((st["code"], st["exit_code"]), ("INVALID_REQUEST", 2))
        req = send_request("a", files=self.files(2),
                           schedule={"at": iso(NOW + 3607), "timezone": None, "exact_minute": False, "remind": False})
        st = self.deliver(o, req)
        self.assertEqual((st["code"], st["exit_code"]), ("INVALID_REQUEST", 2))
        self.assertEqual(o.fake.tg.sends(), [])

    def test_partial_album_unknown_keeps_known_ids(self):  # F20
        tg = FakeTelegram("a")
        tg.send_results = [import_owner().SendUncertain(message_ids=[11, 12])]
        o = self.make(tg=tg)
        st = self.deliver(o, send_request("a", files=self.files(3)))
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("unknown", "SEND_UNCERTAIN", 4))
        self.assertEqual(st["result"]["message_ids"], [11, 12])

    def test_username_mismatch_no_send(self):  # F20
        tg = FakeTelegram("a")
        tg.peer_map = {42: {"kind": "User", "id": 42, "username": "bob"}}
        o = self.make(tg=tg)
        req = send_request("a", peer=42)
        req["target"]["expected_username"] = "alice"
        st = self.deliver(o, req)
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", "USERNAME_MISMATCH", 2))
        self.assertEqual(tg.sends(), [])

    def test_username_match_sends(self):  # F20
        tg = FakeTelegram("a")
        tg.peer_map = {42: {"kind": "User", "id": 42, "username": "bob"}}
        o = self.make(tg=tg)
        req = send_request("a", peer=42)
        req["target"]["expected_username"] = "bob"
        self.assertEqual(self.deliver(o, req)["code"], "SENT")

    def test_file_over_64mib_limit(self):  # F22
        big = self.base / "big.bin"
        with open(big, "wb") as fh:
            fh.truncate(64 * 1024 * 1024 + 1)
        spec = {"source": str(big), "name": "big.bin", "size": 64 * 1024 * 1024 + 1, "sha256": "0" * 64}
        o = self.make()
        st = self.deliver(o, send_request("a", files=[spec]))
        self.assertEqual((st["code"], st["exit_code"]), ("LIMIT_EXCEEDED", 2))
        self.assertEqual(o.fake.tg.uploads(), [])


class Denylist(OwnerCase):
    def setUp(self):
        super().setUp()
        self.home = self.base / "home"
        (self.home / ".config" / "telegram-snapshot").mkdir(parents=True)
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old else os.environ.pop("HOME", None))

    def denied(self, o, path, spec=None):
        st = self.deliver(o, send_request("a", files=[spec or fspec(path)]))
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", "ATTACHMENT_DENIED", 2), str(path))
        self.assertEqual(o.fake.tg.uploads(), [])
        self.assertEqual(o.fake.tg.sends(), [])
        o.fake.clock.advance(1000)

    def test_control_ordinary_file_allowed(self):  # F22 (не вакуум)
        o = self.make()
        f = self.base / "ok.txt"
        f.write_bytes(b"fine")
        self.assertEqual(self.deliver(o, send_request("a", files=[fspec(f)]))["code"], "SENT")

    def test_own_and_neighbour_secrets_denied(self):  # F22
        o = self.make()
        for acc in ("a", "b"):
            self.denied(o, Path(self.env.accounts[acc]["auth_path"]))
            self.denied(o, Path(self.env.accounts[acc]["session_path"]))
        self.denied(o, self.env.registry_path)
        (self.env.root("b") / "note.txt").write_text("x")
        self.denied(o, self.env.root("b") / "note.txt")
        self.denied(o, self.env.root("a") / "heartbeat.json")
        cfgdir = self.home / ".config" / "telegram-snapshot"
        (cfgdir / "auth.json").write_text("{}")
        self.denied(o, cfgdir / "auth.json")
        stray = self.base / "x.session"
        stray.write_bytes(b"s")
        self.denied(o, stray)
        stray2 = self.base / "x.session-journal"
        stray2.write_bytes(b"s")
        self.denied(o, stray2)

    def test_session_without_extension_and_sidecars(self):  # F22
        sdir = Path(self.env.accounts["b"]["session_path"]).parent
        self.env.accounts["b"]["session_path"] = str(sdir / "b")
        self.env.write_registry()
        for n in ("b.session", "b.session-journal", "b.session-wal"):
            (sdir / n).write_bytes(b"s")
        o = self.make()
        for n in ("b.session", "b.session-journal", "b.session-wal"):
            self.denied(o, sdir / n)

    def test_hardlink_outside_tree_denied(self):  # F22
        o = self.make()
        link = self.base / "innocent.txt"
        os.link(self.env.accounts["b"]["session_path"], link)
        self.denied(o, link)

    def test_bindmount_other_realpath_same_devino_denied(self):  # F22 Fake Store
        store = FakeStore()
        o = self.make(store=store)
        decoy = self.base / "decoy.txt"
        decoy.write_bytes(b"payload")
        st = os.stat(self.env.accounts["b"]["auth_path"])
        store.inspect_override[decoy] = {"dev": st.st_dev, "ino": st.st_ino,
                                         "realpath": str(self.base / "mnt" / "x.txt")}
        self.denied(o, decoy)

    def test_registry_update_and_new_file_seen_before_acceptance(self):  # F22
        o = self.make()
        newdir = self.base / "c"
        newdir.mkdir(mode=0o700)
        csec = newdir / "auth.json"
        csec.write_text("{}")
        ok = self.deliver(o, send_request("a", files=[fspec(csec)]))
        self.assertEqual(ok["code"], "SENT")  # c еще не в реестре
        o.fake.clock.advance(1000)
        self.env.write_registry(extra={"c": {"auth_path": str(csec), "session_path": str(newdir / "c.session"),
                                               "owner_root": str(newdir / "root")}})
        self.denied(o, csec)
        late = self.env.root("b") / "created-later.bin"
        late.write_bytes(b"z")
        self.denied(o, late)

    def test_unreadable_registry_rejects_without_send(self):  # F22
        o = self.make()
        f = self.base / "ok.txt"
        f.write_bytes(b"x")
        self.env.registry_path.write_text("{garbage")
        st = self.deliver(o, send_request("a", files=[fspec(f)]))
        self.assertEqual(st["exit_code"], 2)
        self.assertEqual(o.fake.tg.sends(), [])


class Retention(OwnerCase):
    def test_old_tmp_cleaned_fresh_kept(self):  # F22
        o = self.make()
        old = write_package(self.env.root("a"), send_request("a"), where="tmp")
        fresh = write_package(self.env.root("a"), send_request("a"), where="tmp")
        os.utime(old, (NOW - 3601, NOW - 3601))
        os.utime(old / "request.json", (NOW - 3601, NOW - 3601))
        run(o.step())
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_terminal_ttl_7_days_unknown_retained(self):  # F22 / Q4
        o = self.make()
        sent_req, st1 = self.deliver_ok(o)
        o.fake.tg.send_results = [import_owner().SendUncertain(message_ids=[])]
        o.fake.clock.advance(100)
        unk_req = send_request("a", text="u", now=o.fake.clock.utc())
        st2 = self.deliver(o, unk_req)
        self.assertEqual(st2["state"], "unknown")
        o.fake.clock.advance(8 * 86400)
        run(o.step())
        self.assertFalse((self.env.root("a") / "outbox" / sent_req["request_id"]).exists())
        self.assertTrue((self.env.root("a") / "outbox" / unk_req["request_id"]).exists())
        self.assertIsNotNone(read_status(self.env.root("a"), unk_req["request_id"]))


class Crash(OwnerCase):
    def test_status_sending_found_after_restart_becomes_unknown_no_resend(self):  # F13
        o = self.make()
        req = send_request("a")
        write_package(self.env.root("a"), req)
        sdir = self.env.root("a") / "status"
        sdir.mkdir(exist_ok=True, mode=0o700)
        (sdir / f"{req['request_id']}.json").write_text(json.dumps({
            "schema_version": 1, "request_id": req["request_id"], "operation": "send",
            "state": "sending", "code": "SENDING", "exit_code": 0, "retry_after": None, "result": {},
            "request_hash": hashlib.sha256(json.dumps(req, sort_keys=True, ensure_ascii=False,
                                                      separators=(",", ":")).encode()).hexdigest(),
            "accepted_at": NOW, "updated_at": NOW, "retain_until": NOW + 86400}))
        run(o.step())
        st = read_status(self.env.root("a"), req["request_id"])
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("unknown", "SEND_UNCERTAIN", 4))
        self.assertEqual(o.fake.tg.sends(), [])

    def test_crash_after_send_before_final_status_not_resent(self):  # F13/F12
        store = FakeStore()
        o = self.env.make_owner("a", store=store)
        run(o.start())
        req = send_request("a")
        write_package(self.env.root("a"), req)
        # второй atomic_write status (итог после send) падает; эффект send сохранен
        calls = {"n": 0}
        real = store.atomic_write

        def flaky(path, data):
            if "status" in str(path):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("disk")
            return real(path, data)

        store.atomic_write = flaky
        try:
            run(o.step())
        except OSError:
            pass
        run(o.close())
        o2 = self.env.make_owner("a", tg=o.fake.tg, clock=o.fake.clock)
        run(o2.start())
        self.addCleanup(lambda: run(o2.close()))
        run(o2.step())
        st = read_status(self.env.root("a"), req["request_id"])
        self.assertIn(st["state"], ("unknown", "sent"))
        self.assertEqual(len(o.fake.tg.sends()), 1)

    def test_crash_before_sending_allows_continuation(self):  # F13
        o = self.make()
        req = send_request("a")
        write_package(self.env.root("a"), req)
        run(o.step())
        self.assertEqual(len(o.fake.tg.sends()), 1)

    def test_parallel_same_id_two_owner_restarts_single_send(self):  # F12
        o = self.make()
        req, st = self.deliver_ok(o)
        run(o.close())
        o2 = self.env.make_owner("a", tg=o.fake.tg, clock=o.fake.clock)
        run(o2.start())
        self.addCleanup(lambda: run(o2.close()))
        write_package(self.env.root("a"), req)  # тот же id и конверт
        run(o2.step())
        self.assertEqual(len(o.fake.tg.sends()), 1)
        self.assertEqual(read_status(self.env.root("a"), req["request_id"])["state"], "sent")

    def test_same_id_other_hash_conflict_keeps_status(self):  # F12
        o = self.make()
        req, st = self.deliver_ok(o)
        write_package(self.env.root("a"), dict(req, text="иное"))
        run(o.step())
        self.assertEqual(read_status(self.env.root("a"), req["request_id"]), st)
        self.assertEqual(len(o.fake.tg.sends()), 1)

    def test_send_uncertain_status_unknown_4(self):  # F13
        tg = FakeTelegram("a")
        tg.send_results = [import_owner().SendUncertain(message_ids=[])]
        o = self.make(tg=tg)
        st = self.deliver(o, send_request("a"))
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("unknown", "SEND_UNCERTAIN", 4))
        self.assertEqual(set(st) >= {"next_step"}, True)
        self.assertNotIn("Traceback", json.dumps(st))

    def test_generic_exception_after_possible_send_is_unknown(self):  # F13 "неопределенный сбой"
        tg = FakeTelegram("a")
        tg.send_results = [RuntimeError("boom secret-token")]
        o = self.make(tg=tg)
        st = self.deliver(o, send_request("a"))
        self.assertEqual((st["state"], st["exit_code"]), ("unknown", 4))
        self.assertNotIn("secret-token", json.dumps(st))
        self.assertEqual(len(tg.sends()), 1)


class Backoff(OwnerCase):
    def test_flood_wait_no_resend_before_deadline_no_step_sleep(self):  # F21
        tg = FakeTelegram("a")
        tg.send_results = [FloodWait(30)]
        o = self.make(tg=tg)
        req = send_request("a")
        write_package(self.env.root("a"), req)
        run(o.step())
        n = len(tg.sends())
        for _ in range(3):
            run(o.step())
        self.assertEqual(len(tg.sends()), n)
        self.assertEqual(o.fake.clock.async_sleeps, [])
        self.assertNotEqual((read_status(self.env.root("a"), req["request_id"]) or {}).get("state"), "unknown")
        o.fake.clock.advance(31)
        run(o.step())
        self.assertEqual(read_status(self.env.root("a"), req["request_id"])["state"], "sent")
        self.assertEqual(len(tg.sends()), n + 1)

    def test_disconnected_in_events_does_not_raise_or_spin(self):  # F21
        o = self.make()
        o.fake.tg.raises["events"] = import_owner().Disconnected()
        try:
            run(o.step())
        except Exception as exc:  # noqa: BLE001
            self.fail(f"step поднял {exc!r}")
        self.assertEqual(o.fake.clock.async_sleeps, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
