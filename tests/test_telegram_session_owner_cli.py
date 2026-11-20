#!/usr/bin/env python3
"""RED/GREEN: пять CLI в owner-режиме и direct (F2, F3, F25, F26, Q5).

Direct без owner-конфига - поведение прежнее (эти тесты могут быть зелеными сразу).
Owner-режим - RED, пока CLI не подключены к telegram_owner_client.

Запуск: python3 tests/test_telegram_session_owner_cli.py
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import mkdirs, write_file  # noqa: E402
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _stub_telethon() -> None:
    try:
        import telethon  # noqa: F401
        return
    except ImportError:
        pass

    def make(name):
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr: type(attr, (), {})  # type: ignore[method-assign]
        return mod

    telethon = make("telethon")
    tl = make("telethon.tl")
    tl_types = make("telethon.tl.types")
    telethon.tl = tl
    tl.types = tl_types
    sys.modules.update({"telethon": telethon, "telethon.tl": tl, "telethon.tl.types": tl_types})


def load_script(filename, name):
    _stub_telethon()
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DirectReached(Exception):
    pass


class AuthTouched(AssertionError):
    pass


SPECS = {
    # имя: (файл, как собрать argv)
    "send": ("telegram-send.py", lambda ctx: ["--to", "chat", "--text", "hi", "--send"]),
    "send-one": ("telegram-send-one.py", lambda ctx: ["1001", "--text", "hi", "--send"]),
    "pull-one": ("telegram-pull-one.py", lambda ctx: ["1001", str(ctx["dest"])]),
    "snapshot": ("telegram-snapshot.py", lambda ctx: []),
}
FLAG_CLIS = ("send", "send-one", "pull-one")  # где спека однозначно допускает --owner-*


class Ctx:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        os.chmod(tmp, 0o700)
        self.dest = tmp / "dest"
        self.roots = {}
        for acc in ("a", "b", "default"):
            r = tmp / "owners" / acc
            mkdirs(r)
            os.chmod(r, 0o700)
            self.roots[acc] = r
        self.cfg_path = tmp / ".telegram-snapshot.json"

    def project(self, chats=None, **fields):
        data = {"chats": chats or {"chat": {"id": 1001, "account": "default", "dest": str(self.dest)}}}
        data.update(fields)
        write_file(self.cfg_path, json.dumps(data))
        return self.cfg_path

    def owner_config(self, mapping=None, name="owners.json"):
        mapping = mapping if mapping is not None else {k: str(v) for k, v in self.roots.items()}
        p = self.tmp / name
        write_file(p, json.dumps({"managed_accounts": mapping}))
        return p


def run_cli(name, ctx: Ctx, argv_extra=(), argv=None):
    """Возвращает (exit_code, stdout, stderr, direct_reached)."""
    filename, build = SPECS[name]
    mod = load_script(filename, "cli_" + name.replace("-", "_") + uuid.uuid4().hex[:6])
    tgs = getattr(mod, "tgs", mod)
    for target in {id(mod): mod, id(tgs): tgs}.values():
        if hasattr(target, "PROJECT_CONFIG_PATH"):
            target.PROJECT_CONFIG_PATH = ctx.cfg_path
        reached = {"v": False}

        def fake_client(*a, **kw):
            reached["v"] = True
            raise DirectReached()

        def fake_auth(*a, **kw):
            raise AuthTouched("load_auth вызван до маршрута owner")

        if hasattr(target, "TelegramClient"):
            target.TelegramClient = fake_client
        if hasattr(target, "load_auth"):
            target.load_auth = (lambda *a, **kw: {"api_id": 1, "api_hash": "x", "session_name": "s"}) \
                if name_is_direct(ctx) else fake_auth
        if hasattr(target, "client_kwargs"):
            target.client_kwargs = lambda auth: {}
    args = list(argv if argv is not None else build({"dest": ctx.dest})) + list(argv_extra)
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = [filename] + args
    code = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mod.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except DirectReached:
                code = "direct"
    finally:
        sys.argv = old
    return code, out.getvalue(), err.getvalue(), reached["v"]


_DIRECT = {"v": False}


def name_is_direct(ctx):
    return _DIRECT["v"]


class FakeOwnerThread(threading.Thread):
    """Протокольный двойник owner: отвечает на пакеты в outbox терминальным status."""

    def __init__(self, root: Path):
        super().__init__(daemon=True)
        self.root, self.seen, self._halt = root, [], threading.Event()

    def run(self):
        outbox, status = self.root / "outbox", self.root / "status"
        done = set()
        while not self._halt.is_set():
            if outbox.exists():
                for d in outbox.iterdir():
                    rf = d / "request.json"
                    if d.name in done or not rf.exists():
                        continue
                    req = json.loads(rf.read_text())
                    self.seen.append(req)
                    done.add(d.name)
                    mkdirs(status)
                    refresh = req["operation"] == "refresh"
                    reply = {"schema_version": 1, "request_id": req["request_id"],
                             "operation": req["operation"],
                             "state": "refreshed" if refresh else "sent",
                             "code": "REFRESHED" if refresh else "SENT", "exit_code": 0,
                             "retry_after": None,
                             "result": ({"destination_id": req.get("destination_id", "d"), "cursor": 1,
                                         "checkpoint_id": "c", "updated_at": time.time()} if refresh else
                                        {"message_ids": [1], "scheduled_ids": [], "at": None,
                                         "verification": "api_ack"}),
                             "request_hash": hashlib.sha256(json.dumps(
                                 req, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")).hexdigest(),
                             "accepted_at": time.time(),
                             "updated_at": time.time(), "retain_until": time.time() + 86400}
                    tmp = status / f".{d.name}.tmp"
                    write_file(tmp, json.dumps(reply))
                    os.replace(tmp, status / f"{req['request_id']}.json")
            time.sleep(0.02)

    def stop(self):
        self._halt.set()
        self.join(2)


def heartbeat_now(root: Path, state="ready"):
    p = root / "heartbeat.json"
    write_file(p, json.dumps({"schema_version": 1, "instance": str(uuid.uuid4()), "pid": os.getpid(),
                              "state": state}))
    os.chmod(p, 0o600)


class CliCase(unittest.TestCase):
    def setUp(self):
        _DIRECT["v"] = False
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ctx = Ctx(Path(self._t.name))


class DirectUnchanged(CliCase):
    def test_no_owner_fields_goes_direct(self):  # Q5/F2
        _DIRECT["v"] = True
        for name in ("send", "send-one", "pull-one"):
            with self.subTest(name):
                self.ctx.project()
                code, out, err, reached = run_cli(name, self.ctx)
                self.assertTrue(reached or code == "direct", f"{name}: direct-клиент не создан: {code} {err[-300:]}")

    def test_snapshot_no_owner_fields_goes_direct(self):  # Q5/F2
        _DIRECT["v"] = True
        self.ctx.project()
        code, out, err, reached = run_cli("snapshot", self.ctx)
        self.assertTrue(reached or code == "direct", f"{code} {err[-300:]}")


class OwnerRouting(CliCase):
    def assert_no_direct(self, reached, err):
        self.assertFalse(reached, "создан direct-клиент в owner-режиме")
        self.assertNotIn("AuthTouched", err)

    def test_project_owner_root_unavailable_is_7_all_clis(self):  # F2/F3/Q1
        for name in SPECS:
            with self.subTest(name):
                self.ctx.project(owner_root=str(self.ctx.roots["default"]))
                code, out, err, reached = run_cli(name, self.ctx)
                self.assertEqual(code, 7, f"{name}: {out[-200:]} {err[-300:]}")
                self.assert_no_direct(reached, err)
                self.assertFalse((self.ctx.roots["default"] / "outbox").exists() and
                                 any((self.ctx.roots["default"] / "outbox").iterdir()))

    def test_owner_config_unavailable_is_7(self):  # F2
        for name in SPECS:
            with self.subTest(name):
                self.ctx.project(owner_config=str(self.ctx.owner_config()))
                code, out, err, reached = run_cli(name, self.ctx)
                self.assertEqual(code, 7, f"{name}: {err[-300:]}")
                self.assert_no_direct(reached, err)

    def test_account_not_in_map_is_2(self):  # F25
        cfg = self.ctx.owner_config({"a": str(self.ctx.roots["a"])})  # нет default
        self.ctx.project(owner_config=str(cfg))
        for name in SPECS:
            with self.subTest(name):
                code, out, err, reached = run_cli(name, self.ctx)
                self.assertEqual(code, 2, f"{name}: {err[-300:]}")
                self.assert_no_direct(reached, err)

    def test_both_fields_in_one_source_is_2(self):  # F2
        self.ctx.project(owner_root=str(self.ctx.roots["default"]), owner_config=str(self.ctx.owner_config()))
        for name in SPECS:
            with self.subTest(name):
                code, out, err, reached = run_cli(name, self.ctx)
                self.assertEqual(code, 2, f"{name}: {err[-300:]}")
                self.assert_no_direct(reached, err)

    def test_empty_field_missing_config_broken_json_is_2(self):  # F2
        variants = {
            "empty_root": {"owner_root": ""},
            "missing_config_file": {"owner_config": str(self.ctx.tmp / "nope.json")},
        }
        for label, fields in variants.items():
            with self.subTest(label):
                self.ctx.project(**fields)
                code, _, err, reached = run_cli("send-one", self.ctx)
                self.assertEqual(code, 2, err[-300:])
                self.assert_no_direct(reached, err)
        broken = self.ctx.tmp / "broken.json"
        write_file(broken, "{nope")
        self.ctx.project(owner_config=str(broken))
        code, _, err, reached = run_cli("send-one", self.ctx)
        self.assertEqual(code, 2)

    def test_shared_root_for_two_accounts_forbidden_incl_symlink_alias(self):  # F2/F1
        alias = self.ctx.tmp / "owners" / "alias"
        alias.symlink_to(self.ctx.roots["a"])
        cfg = self.ctx.owner_config({"a": str(self.ctx.roots["a"]), "default": str(alias)})
        self.ctx.project(owner_config=str(cfg))
        code, _, err, reached = run_cli("send-one", self.ctx)
        self.assertEqual(code, 2, err[-300:])

    def test_cli_flag_replaces_both_project_fields(self):  # F2: оба поля в проекте, CLI дает одно -> валидно
        self.ctx.project(owner_root=str(self.ctx.roots["default"]), owner_config=str(self.ctx.owner_config()))
        for name in FLAG_CLIS:
            with self.subTest(name):
                code, _, err, reached = run_cli(
                    name, self.ctx, argv_extra=["--owner-root", str(self.ctx.roots["default"])])
                self.assertEqual(code, 7, f"{name}: {err[-300:]}")  # маршрут CLI, не 2

    def test_cli_both_flags_is_2_even_if_project_has_one(self):  # F2
        self.ctx.project(owner_root=str(self.ctx.roots["default"]))
        for name in FLAG_CLIS:
            with self.subTest(name):
                code, _, err, _ = run_cli(name, self.ctx, argv_extra=[
                    "--owner-root", str(self.ctx.roots["default"]),
                    "--owner-config", str(self.ctx.owner_config())])
                self.assertEqual(code, 2, f"{name}: {err[-300:]}")

    def test_cli_owner_config_over_project_owner_root_and_back(self):  # F2
        self.ctx.project(owner_root=str(self.ctx.roots["a"]))
        cfg = self.ctx.owner_config({"a": str(self.ctx.roots["a"])})  # нет default
        code, _, err, _ = run_cli("send-one", self.ctx, argv_extra=["--owner-config", str(cfg)])
        self.assertEqual(code, 2, "CLI owner_config заменил проектный owner_root: default нет в карте")
        self.ctx.project(owner_config=str(cfg))
        code, _, err, _ = run_cli("send-one", self.ctx, argv_extra=["--owner-root", str(self.ctx.roots["default"])])
        self.assertEqual(code, 7, err[-300:])  # CLI owner_root заменил проектный owner_config

    def test_pull_one_unregistered_account_peer_dest_via_owner(self):  # F2/Q6
        owner = FakeOwnerThread(self.ctx.roots["default"])
        heartbeat_now(self.ctx.roots["default"])
        owner.start()
        self.addCleanup(owner.stop)
        self.ctx.project(owner_root=str(self.ctx.roots["default"]))
        code, out, err, reached = run_cli("pull-one", self.ctx)
        self.assertEqual(code, 0, err[-300:])
        (req,) = owner.seen
        self.assertEqual(req["operation"], "refresh")
        self.assertEqual(req["selector"]["peer"], 1001)
        self.assertTrue(os.path.isabs(req["selector"]["dest"]))
        self.assertNotIn("text", req)
        self.assertNotIn("files", req)


def tree(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class DryRun(CliCase):
    """Решение dwl 19.11 (6): dry-run в owner-режиме - локально, без сети и записей в спул:
    проверяет заявку/лимиты, печатает "адресат не проверен"; 0 при пригодной заявке, 2 при отказе."""

    ARGV = {"send": ["--to", "chat", "--text", "hi"], "send-one": ["1001", "--text", "hi"]}

    def setup_owner(self, **project):
        root = self.ctx.roots["default"]
        heartbeat_now(root)
        owner = FakeOwnerThread(root)
        owner.start()
        self.addCleanup(owner.stop)
        self.ctx.project(owner_root=str(root), **project)
        return root, owner

    def test_dry_run_ok_prints_unverified_addressee_no_side_effects(self):  # F2/F20
        root, owner = self.setup_owner()
        before = tree(root)
        for name, argv in self.ARGV.items():
            with self.subTest(name):
                code, out, err, reached = run_cli(name, self.ctx, argv=argv)
                self.assertEqual(code, 0, err[-300:])
                self.assertFalse(reached)
                self.assertIn("адресат не проверен", (out + err).lower())
                self.assertNotIn("AuthTouched", err)
        self.assertEqual(tree(root), before)  # ни tmp/outbox/status/quota.lock, ни темпа
        self.assertEqual(owner.seen, [])  # пакет не публиковался

    def test_dry_run_local_check_failure_is_2_without_side_effects(self):  # F20/F22
        root, owner = self.setup_owner()
        before = tree(root)
        for name, argv in (("send", ["--to", "chat", "--text", "я" * 5000]),
                           ("send-one", ["1001", "--text", "я" * 5000])):
            with self.subTest(name):
                code, out, err, reached = run_cli(name, self.ctx, argv=argv)
                self.assertEqual(code, 2, err[-300:])
                self.assertFalse(reached)
        self.assertEqual(tree(root), before)
        self.assertEqual(owner.seen, [])

    def test_dry_run_does_not_check_addressee_over_network(self):  # F20: без resolve/SSH/auth
        root, owner = self.setup_owner()
        code, out, err, reached = run_cli("send-one", self.ctx, argv=["1001", "--text", "hi"])
        self.assertEqual(code, 0, err[-300:])
        self.assertFalse(reached)  # TelegramClient не создан
        self.assertNotIn("AuthTouched", err)


class AccountRouting(CliCase):
    def two_owners(self):
        threads = {}
        for acc in ("a", "b"):
            heartbeat_now(self.ctx.roots[acc])
            threads[acc] = FakeOwnerThread(self.ctx.roots[acc])
            threads[acc].start()
            self.addCleanup(threads[acc].stop)
        return threads

    def test_send_takes_account_from_chat_entry(self):  # F25
        t = self.two_owners()
        self.ctx.project(chats={"chat": {"id": 1001, "account": "b"}},
                         owner_config=str(self.ctx.owner_config()))
        code, out, err, _ = run_cli("send", self.ctx)
        self.assertEqual(code, 0, err[-300:])
        self.assertEqual(t["a"].seen, [])
        (req,) = t["b"].seen
        self.assertEqual((req["operation"], req["account"], req["text"]), ("send", "b", "hi"))
        self.assertEqual(req["target"]["peer"], 1001)
        ids = UUID_RE.findall(out + err)
        self.assertIn(req["request_id"], ids)  # id печатается

    def test_send_one_account_flag_and_default(self):  # F25
        t = self.two_owners()
        heartbeat_now(self.ctx.roots["default"])
        td = FakeOwnerThread(self.ctx.roots["default"])
        td.start()
        self.addCleanup(td.stop)
        self.ctx.project(owner_config=str(self.ctx.owner_config()))
        code, _, err, _ = run_cli("send-one", self.ctx, argv_extra=["--account", "a"])
        self.assertEqual(code, 0, err[-300:])
        self.assertEqual((len(t["a"].seen), len(t["b"].seen), len(td.seen)), (1, 0, 0))
        code, _, err, _ = run_cli("send-one", self.ctx)
        self.assertEqual(code, 0, err[-300:])
        self.assertEqual(len(td.seen), 1)  # отсутствие account = default

    def test_json_flag_prints_reply(self):  # CLI контракт
        t = self.two_owners()
        self.ctx.project(chats={"chat": {"id": 1001, "account": "a"}},
                         owner_config=str(self.ctx.owner_config()))
        code, out, err, _ = run_cli("send", self.ctx, argv_extra=["--json"])
        self.assertEqual(code, 0, err[-300:])
        reply = json.loads(out)
        self.assertEqual((reply["state"], reply["code"], reply["exit_code"]), ("sent", "SENT", 0))


class SnapshotPartialFailure(CliCase):
    def test_a_unavailable_b_refreshed_total_7_report_visible(self):  # F26
        heartbeat_now(self.ctx.roots["b"])
        tb = FakeOwnerThread(self.ctx.roots["b"])
        tb.start()
        self.addCleanup(tb.stop)
        chats = {"ca": {"id": 1001, "account": "a", "dest": str(self.ctx.tmp / "ma")},
                 "cb": {"id": 2002, "account": "b", "dest": str(self.ctx.tmp / "mb")}}
        self.ctx.project(chats=chats, owner_config=str(self.ctx.owner_config(
            {"a": str(self.ctx.roots["a"]), "b": str(self.ctx.roots["b"])})))
        code, out, err, reached = run_cli("snapshot", self.ctx, argv=["--json"])
        self.assertEqual(code, 7, err[-300:])
        self.assertFalse(reached)
        report = json.loads(out)
        self.assertEqual(report["exit_code"], 7)
        by = {r["account"]: r for r in report["results"]}
        self.assertEqual((by["a"]["reply"]["code"], by["a"]["reply"]["exit_code"]), ("OWNER_UNAVAILABLE", 7))
        self.assertEqual((by["b"]["reply"]["code"], by["b"]["reply"]["exit_code"]), ("REFRESHED", 0))
        self.assertEqual(by["b"]["peer"], 2002)
        self.assertTrue(os.path.isabs(by["b"]["dest"]))
        self.assertFalse((self.ctx.roots["a"] / "outbox").exists() and
                         any((self.ctx.roots["a"] / "outbox").iterdir()))
        (req,) = tb.seen
        self.assertEqual((req["operation"], req["account"]), ("refresh", "b"))
        self.assertEqual(req["selector"], {"peer": 2002, "dest": str(self.ctx.tmp / "mb")})

    def test_all_ok_total_zero(self):  # F26 "иначе ... либо 0"
        for acc in ("a", "b"):
            heartbeat_now(self.ctx.roots[acc])
            th = FakeOwnerThread(self.ctx.roots[acc])
            th.start()
            self.addCleanup(th.stop)
        chats = {"ca": {"id": 1001, "account": "a", "dest": str(self.ctx.tmp / "ma")},
                 "cb": {"id": 2002, "account": "b", "dest": str(self.ctx.tmp / "mb")}}
        self.ctx.project(chats=chats, owner_config=str(self.ctx.owner_config(
            {"a": str(self.ctx.roots["a"]), "b": str(self.ctx.roots["b"])})))
        code, out, err, _ = run_cli("snapshot", self.ctx, argv=["--json"])
        self.assertEqual(code, 0, err[-300:])
        self.assertEqual(json.loads(out)["exit_code"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
