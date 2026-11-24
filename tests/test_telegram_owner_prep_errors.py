#!/usr/bin/env python3
"""RED: постоянные ошибки подготовки заявки send (resolve/upload до send), пакет 3.

Спека: docs/2026-11-24-spec-telegram-owner-pkg3.md, критерии P3-1..P3-13, уточнения У-1..У-5.
Тесты написаны по спеке, без чтения реализации owner и адаптера.

Шов: настоящий Telegram-адаптер (Telegram.__new__, как в test_telegram_owner_cli_local) с
фейковым client и Telethon-подобным errors; FakeTelegram перенаправляет в него только
"плохие" peer/файлы, остальные вызовы - фейк. Telethon в окружении не нужен: классы
ошибок смоделированы по именам публичного API (UsernameInvalidError,
UsernameNotOccupiedError, FilePartsInvalidError, FilePartSizeInvalidError, FloodWaitError).

Запуск: python3 tests/test_telegram_owner_prep_errors.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from telegram_owner_telethon import Telegram  # noqa: E402
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeTelegram, NOW, import_client, import_owner, mkdirs, read_status, req_hash, run,
    send_request, write_file, write_package,
)
from test_telegram_session_owner_send import fspec  # noqa: E402

SECRET = "SECRET-MARKER-9f3a"
BAD_PEER = 777       # неизвестный adapter-у peer (fallback поиска)
KNOWN_PEER = 1       # peer, известный adapter-у по диалогам (обновление entity)
HEALTHY_PEER = 42


class RpcError(Exception):
    code = 400
    message = "BAD_REQUEST"

    def __init__(self, text=SECRET):
        super().__init__(f"{self.message}: {text}")


class UsernameInvalidError(RpcError):
    message = "USERNAME_INVALID"


class UsernameNotOccupiedError(RpcError):
    message = "USERNAME_NOT_OCCUPIED"


class FilePartsInvalidError(RpcError):
    message = "FILE_PARTS_INVALID"


class FilePartSizeInvalidError(RpcError):
    message = "FILE_PART_SIZE_INVALID"


class SomeOtherBadRequestError(RpcError):
    message = "SOMETHING_ELSE_INVALID"


class FloodWaitError(RpcError):
    code = 420

    def __init__(self, seconds):
        super().__init__(str(seconds))
        self.seconds = seconds


ERRORS = types.SimpleNamespace(
    RPCError=RpcError, FloodWaitError=FloodWaitError, UsernameInvalidError=UsernameInvalidError,
    UsernameNotOccupiedError=UsernameNotOccupiedError, FilePartsInvalidError=FilePartsInvalidError,
    FilePartSizeInvalidError=FilePartSizeInvalidError, BadRequestError=SomeOtherBadRequestError)


def is_bad(path) -> bool:
    """Имена копий вложений в спуле не сохраняются - плохой файл узнается по содержимому."""
    try:
        return Path(str(path)).read_bytes() == b"BAD"
    except OSError:
        return False


def build_adapter(*, resolve_none=False, resolve_exc=None, me_exc=None, known_exc=None, upload_exc=None,
                  send_exc=None):
    a = Telegram.__new__(Telegram)
    a.account = "a"
    a.errors = ERRORS
    a._uploads, a._entities, a._aliases = {}, {}, {}
    a._dialogs = {KNOWN_PEER: "cached-entity"}
    a._entities = {("User", 1): "entity"}

    async def get_entity(ident):
        if known_exc is not None:
            raise known_exc
        return types.SimpleNamespace(id=KNOWN_PEER, username=None)

    async def upload_file(path, *args, **kwargs):
        if upload_exc is not None and is_bad(path):
            raise upload_exc
        return "handle"

    a.client = types.SimpleNamespace(
        get_me=AsyncMock(side_effect=me_exc) if me_exc is not None else AsyncMock(
            return_value=types.SimpleNamespace(id=1, username=None)),
        get_entity=get_entity, upload_file=upload_file, is_connected=lambda: True,
        send_message=AsyncMock(side_effect=send_exc) if send_exc is not None else AsyncMock(
            return_value=types.SimpleNamespace(id=9, message="x")),
        send_file=AsyncMock(side_effect=send_exc) if send_exc is not None else AsyncMock(
            return_value=types.SimpleNamespace(id=9)))
    a.snapshot = types.SimpleNamespace(
        resolve_entity=AsyncMock(return_value=None) if resolve_none else
        AsyncMock(side_effect=resolve_exc) if resolve_exc is not None else AsyncMock(
            return_value=types.SimpleNamespace(id=BAD_PEER, username=None)))
    a._peer = lambda entity: {"account": "a", "kind": "User", "id": entity.id,
                              "username": getattr(entity, "username", None)}
    return a


class HybridTelegram(FakeTelegram):
    """Фейк, который для выбранных peer/файлов/send ходит в настоящий адаптер."""

    def __init__(self, adapter, *, bad_peers=(), bad_files=(), real_send=False):
        super().__init__("a")
        self.adapter, self.bad_peers, self.bad_files, self.real_send = (
            adapter, set(bad_peers), set(bad_files), real_send)

    async def resolve(self, account, peer):
        if peer in self.bad_peers:
            self._log("resolve", account, peer)
            return await self.adapter.resolve(account, peer)
        return await super().resolve(account, peer)

    async def upload(self, path):
        self.n_up = getattr(self, "n_up", 0) + 1
        if self.bad_files and is_bad(path):
            self._log("upload", Path(path))
            return await self.adapter.upload(Path(path))
        self._log("upload", Path(path))
        token = f"tok{self.n_up}"
        self.tokens = getattr(self, "tokens", []) + [token]
        return token

    async def send(self, request, peer, uploads):
        if self.real_send:
            self._log("send", request, peer, list(uploads))
            return await self.adapter.send(request, peer, list(uploads))
        return await super().send(request, peer, uploads)


class PrepCase(Base):
    def owner(self, tg):
        o = self.env.make_owner("a", tg=tg)
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        return o

    def deliver(self, o, req, steps=6):
        write_package(self.env.root("a"), req)
        for _ in range(steps):
            if not run(o.step()):
                break
        return read_status(self.env.root("a"), req["request_id"])

    def file_req(self, name, peer="me", count=1, **over):
        d = self.base / "src"
        if not d.exists():
            mkdirs(d)
        specs = []
        for i in range(count):
            fname = name if i == count - 1 else f"ok{i}.bin"
            specs.append(fspec(write_file(d / fname, b"BAD" if i == count - 1 and
                                          fname.startswith("bad") else b"abc")))
        return send_request("a", peer=peer, files=specs, **over)

    def healthy(self, kind):
        if kind == "text":
            return send_request("a", peer=HEALTHY_PEER, text="Исправная")
        return self.file_req("okfile.bin", peer=HEALTHY_PEER)

    def heartbeat_state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    def assert_rejected(self, st, req, code):
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", code, 2))
        self.assertEqual(st["request_id"], req["request_id"])
        self.assertEqual(st["request_hash"], req_hash(req))
        self.assertFalse(st.get("retry_after"))
        self.assertFalse(st.get("retry_after_utc"))
        self.assertFalse((st.get("result") or {}).get("message_ids"))
        self.assertFalse((st.get("result") or {}).get("scheduled_ids"))
        self.assertNotIn(SECRET, json.dumps(st))

    def assert_no_reconnect(self, tg):
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())
        self.assertEqual(self.heartbeat_state(), "ready")

    def assert_healthy_goes_next(self, o, tg, kind, bad_rid):
        tg.bad_tokens = list(getattr(tg, "tokens", []))
        before_bad_prep = [c for c in tg.calls if c[0] in ("resolve", "upload")]
        req = self.healthy(kind)
        st = self.deliver(o, req)  # без продвижения FakeClock
        self.assertEqual((st["state"], st["code"]), ("sent", "SENT"))
        sends = tg.sends()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1]["request_id"], req["request_id"])
        self.assertEqual(len(sends[0][3]), 0 if kind == "text" else 1)
        self.assertFalse(set(sends[0][3]) & set(getattr(tg, "bad_tokens", ())))
        self.assert_no_reconnect(tg)
        # ошибочный UUID после сохраненного rejected повторно не готовится
        run(o.step())
        run(o.step())
        later = [c for c in tg.calls if c[0] in ("resolve", "upload") and c not in before_bad_prep]
        self.assertTrue(all(not (c[0] == "resolve" and c[2] == BAD_PEER) and
                            not (c[0] == "upload" and is_bad(c[1])) for c in later), later)
        self.assertEqual(read_status(self.env.root("a"), bad_rid)["state"], "rejected")


# --- P3-1, P3-3: resolve --------------------------------------------------------------

class ResolveRejected(PrepCase):
    def run_case(self, exc, *, peer, kind, bad_kind="text", known=False):
        ad = build_adapter(resolve_exc=None if known else exc, known_exc=exc if known else None)
        tg = HybridTelegram(ad, bad_peers={peer})
        o = self.owner(tg)
        bad = (send_request("a", peer=peer, text="Плохой") if bad_kind == "text"
               else self.file_req("bad.bin", peer=peer))
        st = self.deliver(o, bad)
        self.assert_rejected(st, bad, "PEER_UNRESOLVABLE")
        self.assertEqual(tg.sends(), [])
        self.assertEqual(tg.uploads(), [])
        self.assert_no_reconnect(tg)
        self.assert_healthy_goes_next(o, tg, kind, bad["request_id"])

    def test_unknown_peer_named_username_errors_then_next_text_and_file(self):
        for exc in (UsernameInvalidError(), UsernameNotOccupiedError()):
            for kind in ("text", "file"):
                for bad_kind in ("text", "file"):
                    with self.subTest(exc=type(exc).__name__, kind=kind, bad_kind=bad_kind):
                        self.setUp()
                        self.run_case(exc, peer=BAD_PEER, kind=kind, bad_kind=bad_kind)

    def test_known_entity_refresh_path(self):
        for exc in (UsernameInvalidError(), UsernameNotOccupiedError()):
            for kind in ("text", "file"):
                with self.subTest(exc=type(exc).__name__, kind=kind):
                    self.setUp()
                    self.run_case(exc, peer=KNOWN_PEER, kind=kind, known=True)

    def test_retain_until_is_one_week_and_status_readable_by_client(self):
        ad = build_adapter(resolve_exc=UsernameInvalidError())
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        o = self.owner(tg)
        bad = send_request("a", peer=BAD_PEER)
        st = self.deliver(o, bad)
        self.assertAlmostEqual(st["retain_until"], o.fake.clock.utc() + 7 * 86400, delta=5)
        got = import_client().status(self.env.root("a"), bad["request_id"], clock=o.fake.clock)
        self.assertEqual((got["state"], got["code"], got["exit_code"]),
                         ("rejected", "PEER_UNRESOLVABLE", 2))

    def test_same_uuid_same_body_returns_saved_outcome_other_body_conflicts(self):
        ad = build_adapter(resolve_exc=UsernameInvalidError())
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        o = self.owner(tg)
        bad = send_request("a", peer=BAD_PEER)
        self.deliver(o, bad)
        calls = len(tg.calls)
        client = import_client()
        again = client.submit(self.env.root("a"), bad, clock=o.fake.clock, timeout=0)
        self.assertEqual((again["state"], again["code"], again["exit_code"]),
                         ("rejected", "PEER_UNRESOLVABLE", 2))
        other = client.submit(self.env.root("a"), dict(bad, text="Другое"), clock=o.fake.clock,
                              timeout=0)
        self.assertEqual((other["code"], other["exit_code"]), ("REQUEST_ID_CONFLICT", 2))
        self.assertEqual(len(tg.calls), calls)

    def test_rejected_prep_does_not_log_secrets_or_network_recovery(self):
        ad = build_adapter(resolve_exc=UsernameInvalidError(f"{SECRET} @private_name /secret/path"))
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        o = self.owner(tg)
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old)))
        self.deliver(o, send_request("a", peer=BAD_PEER, text="Текст-сообщения-тест"))
        text = "\n".join(r.getMessage() for r in records)
        for forbidden in (SECRET, "private_name", "/secret/path", "Текст-сообщения-тест",
                          "OWNER_RECONNECT", "RECOVERY_WAIT"):
            self.assertNotIn(forbidden, text)

    def test_rejected_write_failure_is_not_success_no_send_no_reconnect(self):
        from test_telegram_session_owner_fakes import FakeStore
        store = FakeStore()
        ad = build_adapter(resolve_exc=UsernameInvalidError())
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        o = self.env.make_owner("a", tg=tg, store=store)
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        bad = send_request("a", peer=BAD_PEER)
        real = store.atomic_write
        failed = {"n": 0}

        def flaky(path, data):
            if bad["request_id"] in str(path) and b'"rejected"' in data and not failed["n"]:
                failed["n"] += 1
                raise OSError(28, "no space")
            return real(path, data)

        store.atomic_write = flaky
        write_package(self.env.root("a"), bad)
        try:
            run(o.step())
        except OSError:
            pass
        self.assertEqual(failed["n"], 1)
        self.assertEqual(tg.sends(), [])
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())
        st = read_status(self.env.root("a"), bad["request_id"])
        self.assertNotIn((st or {}).get("state"), ("sent", "scheduled"))
        for _ in range(6):  # локальное восстановление; затем повторная подготовка допустима
            o.fake.clock.advance(60)
            run(o.step())
        self.assertEqual(tg.sends(), [])
        self.assert_rejected(read_status(self.env.root("a"), bad["request_id"]), bad,
                             "PEER_UNRESOLVABLE")


# --- P3-1 (me), P3-9: что НЕ постоянно ------------------------------------------------

class LocalMiss(PrepCase):
    """У-6: fallback вернул None без исключения; peer нет в _dialogs и _entities."""

    def test_none_without_exception_is_peer_unresolvable(self):
        for kind in ("text", "file"):
            with self.subTest(kind=kind):
                self.setUp()
                ad = build_adapter(resolve_none=True)
                self.assertNotIn(BAD_PEER, ad._dialogs)
                self.assertFalse([k for k in ad._entities if BAD_PEER in k])
                tg = HybridTelegram(ad, bad_peers={BAD_PEER})
                o = self.owner(tg)
                bad = send_request("a", peer=BAD_PEER)
                st = self.deliver(o, bad)
                self.assert_rejected(st, bad, "PEER_UNRESOLVABLE")
                self.assertEqual(tg.sends(), [])
                self.assert_no_reconnect(tg)
                self.assert_healthy_goes_next(o, tg, kind, bad["request_id"])

    def test_network_error_from_fallback_is_not_permanent(self):
        for exc in (ConnectionError("down"), asyncio.TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()
                ad = build_adapter(resolve_exc=exc)
                tg = HybridTelegram(ad, bad_peers={BAD_PEER})
                o = self.owner(tg)
                st = self.deliver(o, send_request("a", peer=BAD_PEER))
                self.assertEqual((st["state"], st["code"]), ("queued", "QUEUED"))


class ReviewGaps(PrepCase):
    """Пробелы по сверке пакета 3: отмена, remind/pace, зеркало, FloodWait, None при известном peer."""

    def step_swallowing_cancel(self, o):
        try:
            return run(o.step())
        except (asyncio.CancelledError, KeyboardInterrupt):
            return None

    def tree(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*"))
                if p.is_file()}

    def test_cancel_during_resolve_is_not_rejected(self):
        ad = build_adapter(resolve_exc=asyncio.CancelledError())
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        o = self.owner(tg)
        bad = send_request("a", peer=BAD_PEER)
        write_package(self.env.root("a"), bad)
        self.step_swallowing_cancel(o)
        st = read_status(self.env.root("a"), bad["request_id"])
        self.assertNotEqual((st or {}).get("state"), "rejected")
        self.assertNotIn((st or {}).get("code"), ("PEER_UNRESOLVABLE", "UPLOAD_REJECTED"))
        self.assertEqual(tg.sends(), [])

    def test_cancel_during_upload_is_not_rejected_and_leaves_no_handles(self):
        ad = build_adapter(upload_exc=asyncio.CancelledError())
        tg = HybridTelegram(ad, bad_files={"bad.bin"})
        o = self.owner(tg)
        bad = self.file_req("bad.bin", count=2)
        write_package(self.env.root("a"), bad)
        self.step_swallowing_cancel(o)
        st = read_status(self.env.root("a"), bad["request_id"])
        self.assertNotEqual((st or {}).get("state"), "rejected")
        self.assertNotIn((st or {}).get("code"), ("PEER_UNRESOLVABLE", "UPLOAD_REJECTED"))
        self.assertEqual(tg.sends(), [])
        self.assertEqual(ad._uploads, {})

    def test_upload_rejection_leaves_adapter_without_handles(self):
        ad = build_adapter(upload_exc=FilePartsInvalidError())
        tg = HybridTelegram(ad, bad_files={"bad.bin"})
        o = self.owner(tg)
        bad = self.file_req("bad.bin", count=2)
        self.assert_rejected(self.deliver(o, bad), bad, "UPLOAD_REJECTED")
        self.assertEqual(ad._uploads, {})

    def test_remind_with_at_kept_and_other_peer_pace_untouched(self):
        ad = build_adapter(resolve_exc=UsernameInvalidError())
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        o = self.owner(tg)
        first = self.deliver(o, send_request("a", peer=HEALTHY_PEER))
        self.assertEqual(first["state"], "sent")
        pace = dict(first.get("pace") or {})
        at = NOW + 3600 + 7 * 60 + 7
        scheds = (
            {"at": at, "timezone": None, "exact_minute": True, "remind": False},
        )
        for sched in scheds:
            bad = send_request("a", peer=BAD_PEER, schedule=sched)
            st = self.deliver(o, bad)
            self.assert_rejected(st, bad, "PEER_UNRESOLVABLE")
            saved = json.loads((self.env.root("a") / "outbox" / bad["request_id"]
                                / "request.json").read_bytes())
            self.assertEqual(saved["schedule"], sched)
            for key, value in pace.items():
                self.assertEqual((st.get("pace") or {}).get(key, value), value)
        self.assertEqual(len(tg.sends()), 1)

    def test_rejected_does_not_touch_mirror_files(self):
        ad = build_adapter(resolve_exc=UsernameInvalidError())
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        tg.history_data = [{"id": 1, "type": "message", "date": "2026-11-19T10:00:01",
                            "date_unixtime": "1797674401", "from": "Иван", "from_id": "user1",
                            "text": "msg 1", "text_entities": []}]
        o = self.env.make_owner("a", tg=tg, destinations=[self.env.destination("d1", 1001, "chat1")])
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        before = self.tree(self.env.projects)
        bad = send_request("a", peer=BAD_PEER)
        self.assert_rejected(self.deliver(o, bad), bad, "PEER_UNRESOLVABLE")
        self.assertEqual(self.tree(self.env.projects), before)
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())

    def test_flood_wait_on_resolve_does_not_reconnect(self):
        ad = build_adapter(resolve_exc=FloodWaitError(30))
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        o = self.owner(tg)
        st = self.deliver(o, send_request("a", peer=BAD_PEER))
        self.assertEqual(st["state"], "queued")
        o.fake.clock.advance(31)
        ad.snapshot.resolve_entity = AsyncMock(return_value=types.SimpleNamespace(id=BAD_PEER, username=None))
        for _ in range(4):
            run(o.step())
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())

    def test_mirror_resolve_error_at_startup_gives_no_peer_unresolvable_status(self):
        tg = FakeTelegram("a")
        tg.raises["resolve"] = UsernameInvalidError()
        o = self.env.make_owner("a", tg=tg, destinations=[self.env.destination("d1", 1001, "chat1")])
        self.addCleanup(lambda: run(o.close()))
        req = send_request("a", peer=HEALTHY_PEER)
        write_package(self.env.root("a"), req)
        run(o.start())
        for _ in range(4):
            o.fake.clock.advance(20)
            run(o.step())
        status_dir = self.env.root("a") / "status"
        for p in (status_dir.glob("*.json") if status_dir.exists() else []):
            self.assertNotIn("PEER_UNRESOLVABLE", p.read_text())

    def test_none_but_peer_known_in_entities_is_not_permanent(self):
        ad = build_adapter(resolve_none=True)
        ad._entities[("User", BAD_PEER)] = "entity"
        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
        o = self.owner(tg)
        st = self.deliver(o, send_request("a", peer=BAD_PEER))
        self.assertNotEqual(st["state"], "rejected", st)
        self.assertNotEqual(st["code"], "PEER_UNRESOLVABLE")


class NotPermanent(PrepCase):
    def assert_not_rejected_prep(self, st):
        self.assertNotEqual(st["state"], "rejected", st)
        self.assertNotIn(st["code"], ("PEER_UNRESOLVABLE", "UPLOAD_REJECTED"))

    def test_me_is_never_declared_wrong_peer(self):
        for exc in (UsernameInvalidError(), UsernameNotOccupiedError(), ValueError("boom"),
                    SomeOtherBadRequestError()):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()
                ad = build_adapter(me_exc=exc)
                tg = HybridTelegram(ad, bad_peers={"me"})
                o = self.owner(tg)
                st = self.deliver(o, send_request("a", peer="me"))
                self.assert_not_rejected_prep(st)
                self.assertEqual(tg.sends(), [])

    def test_other_errors_on_resolve_do_not_get_new_permanent_code(self):
        for exc in (ValueError("boom"), ValueError("Cannot find any entity corresponding to 'x'"),
                    SomeOtherBadRequestError(), OSError(5, "EIO"), OSError(28, "ENOSPC")):
            with self.subTest(exc=repr(exc)):
                self.setUp()
                ad = build_adapter(resolve_exc=exc)
                tg = HybridTelegram(ad, bad_peers={BAD_PEER})
                o = self.owner(tg)
                st = self.deliver(o, send_request("a", peer=BAD_PEER))
                self.assert_not_rejected_prep(st)
                self.assertEqual(tg.sends(), [])

    def test_other_errors_on_upload_do_not_get_new_permanent_code(self):
        for exc in (ValueError("boom"), SomeOtherBadRequestError(), UsernameInvalidError(),
                    OSError(5, "EIO")):
            with self.subTest(exc=repr(exc)):
                self.setUp()
                ad = build_adapter(upload_exc=exc)
                tg = HybridTelegram(ad, bad_files={"bad.bin"})
                o = self.owner(tg)
                st = self.deliver(o, self.file_req("bad.bin"))
                self.assert_not_rejected_prep(st)
                self.assertEqual(tg.sends(), [])


# --- P3-4, P3-5: временные ------------------------------------------------------------

class TemporaryStaysTemporary(PrepCase):
    def test_transport_errors_on_resolve_keep_backoff(self):
        for exc in (ConnectionError("down"), asyncio.TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()
                ad = build_adapter(resolve_exc=exc)
                tg = HybridTelegram(ad, bad_peers={BAD_PEER})
                o = self.owner(tg)
                bad = send_request("a", peer=BAD_PEER)
                st = self.deliver(o, bad)
                self.assertEqual((st["state"], st["code"]), ("queued", "QUEUED"))
                mark = len(tg.calls)
                o.fake.clock.advance(14.5)
                run(o.step())
                self.assertEqual(len(tg.calls), mark, "RPC/connect до срока backoff")
                o.fake.clock.advance(0.5)
                ad.snapshot.resolve_entity = AsyncMock(
                    return_value=types.SimpleNamespace(id=BAD_PEER, username=None))
                for _ in range(4):
                    run(o.step())
                self.assertEqual(read_status(self.env.root("a"), bad["request_id"])["state"], "sent")

    def test_flood_wait_on_resolve_and_upload_is_temporary_with_monotonic_deadline(self):
        for seconds in (30, 600):
            for where in ("resolve", "upload"):
                with self.subTest(seconds=seconds, where=where):
                    self.setUp()
                    if where == "resolve":
                        ad = build_adapter(resolve_exc=FloodWaitError(seconds))
                        tg = HybridTelegram(ad, bad_peers={BAD_PEER})
                        bad = send_request("a", peer=BAD_PEER)
                    else:
                        ad = build_adapter(upload_exc=FloodWaitError(seconds))
                        tg = HybridTelegram(ad, bad_files={"bad.bin"})
                        bad = self.file_req("bad.bin")
                    o = self.owner(tg)
                    st = self.deliver(o, bad)
                    self.assertEqual(st["state"], "queued")
                    self.assertEqual(dt.datetime.fromisoformat(st["retry_after_utc"]).timestamp(),
                                     NOW + seconds)
                    o.fake.clock._utc += 5000  # скачок UTC не снимает запрет
                    mark = len(tg.calls)
                    run(o.step())
                    self.assertEqual(len(tg.calls), mark)
                    # исправный пакет другого peer не обнуляет паузу
                    self.deliver(o, self.healthy("text"))
                    self.assertEqual(len(tg.calls), mark)
                    self.assertEqual(tg.sends(), [])


# --- P3-2: upload ---------------------------------------------------------------------
# Объем зависит от подтверждения исполнителя (У-2): FILE_PARTS_INVALID/FILE_PART_SIZE_INVALID
# входят, только если на установленной Telethon upload_file отдает их наружу.

class UploadRejected(PrepCase):
    def test_upload_rejection_for_alp_and_me_then_next_request(self):
        for exc in (FilePartsInvalidError(), FilePartSizeInvalidError()):
            for peer in (1001, "me"):
                for kind in ("text", "file"):
                    with self.subTest(exc=type(exc).__name__, peer=peer, kind=kind):
                        self.setUp()
                        ad = build_adapter(upload_exc=exc)
                        tg = HybridTelegram(ad, bad_files={"bad.bin"})
                        o = self.owner(tg)
                        bad = self.file_req("bad.bin", peer=peer)
                        st = self.deliver(o, bad)
                        self.assert_rejected(st, bad, "UPLOAD_REJECTED")
                        self.assertEqual(tg.sends(), [])
                        self.assert_no_reconnect(tg)
                        self.assert_healthy_goes_next(o, tg, kind, bad["request_id"])

    def test_second_file_of_album_rejected_first_handle_not_reused(self):
        ad = build_adapter(upload_exc=FilePartsInvalidError())
        tg = HybridTelegram(ad, bad_files={"bad.bin"})
        o = self.owner(tg)
        bad = self.file_req("bad.bin", peer=1001, count=2)
        st = self.deliver(o, bad)
        self.assert_rejected(st, bad, "UPLOAD_REJECTED")
        self.assertEqual(len(tg.uploads()), 2)  # первый успешен, второй отказал
        self.assertEqual(tg.sends(), [])
        self.assert_no_reconnect(tg)
        self.assert_healthy_goes_next(o, tg, "file", bad["request_id"])
        self.assertEqual(len(tg.bad_tokens), 1)  # первый файл альбома загружен, но не переиспользован


# --- P3-6: граница send ---------------------------------------------------------------

class AfterSendBoundary(PrepCase):
    def assert_unknown_never_resent(self, o, tg, req):
        st = read_status(self.env.root("a"), req["request_id"])
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("unknown", "SEND_UNCERTAIN", 4))
        o.fake.clock.advance(400)
        for _ in range(4):
            run(o.step())
        self.assertEqual(len(tg.sends()), 1)
        self.assertEqual(read_status(self.env.root("a"), req["request_id"])["state"], "unknown")

    def test_port_level_errors_in_send_are_unknown(self):
        for exc in (ValueError("boom"), RuntimeError("x"), import_owner().Disconnected(),
                    asyncio.TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()
                tg = FakeTelegram("a")
                tg.send_results = [exc]
                o = self.owner(tg)
                req = send_request("a")
                self.deliver(o, req)
                self.assert_unknown_never_resent(o, tg, req)

    def test_adapter_send_errors_including_permanent_prep_codes_are_unknown(self):
        for exc in (UsernameInvalidError(), UsernameNotOccupiedError(), FilePartsInvalidError(),
                    FilePartSizeInvalidError(), ValueError("boom"), SomeOtherBadRequestError(),
                    ConnectionError("down")):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()
                ad = build_adapter(send_exc=exc)
                tg = HybridTelegram(ad, real_send=True)
                o = self.owner(tg)
                req = send_request("a")
                st = self.deliver(o, req)
                self.assertNotEqual(st["state"], "rejected", st)
                self.assert_unknown_never_resent(o, tg, req)

    def test_f21_flood_wait_from_send_stays_queued_with_retry(self):
        ad = build_adapter(send_exc=FloodWaitError(30))
        tg = HybridTelegram(ad, real_send=True)
        o = self.owner(tg)
        req = send_request("a")
        st = self.deliver(o, req)
        self.assertEqual(st["state"], "queued")
        self.assertEqual(dt.datetime.fromisoformat(st["retry_after_utc"]).timestamp(), NOW + 30)


# --- P3-8: границы операций -----------------------------------------------------------

class OtherOperations(PrepCase):
    def test_same_class_on_mirror_resolve_does_not_reject_spool_packages(self):
        tg = FakeTelegram("a")
        tg.raises["history"] = UsernameInvalidError()
        o = self.env.make_owner("a", tg=tg, destinations=[self.env.destination("d1", 1001, "chat1")])
        self.addCleanup(lambda: run(o.close()))
        req = send_request("a", peer=HEALTHY_PEER)
        write_package(self.env.root("a"), req)
        run(o.start())
        for _ in range(4):
            run(o.step())
        status_dir = self.env.root("a") / "status"
        for p in (status_dir.glob("*.json") if status_dir.exists() else []):
            body = p.read_text()
            self.assertNotIn("PEER_UNRESOLVABLE", body)
            self.assertNotIn("UPLOAD_REJECTED", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
