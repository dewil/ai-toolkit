#!/usr/bin/env python3
"""Тесты gsheets.py - прямой доступ к Sheets API без MCP. stdlib-only, без сети.

Запуск: python3 tests/test_gsheets.py

Сеть не трогаем: покрыты разбор учетных данных и валидация входа - то, что
ломается молча. Файл с refresh_token равносилен паролю, поэтому проверяется и
предупреждение о правах (rules/secrets-handling.md).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("gsheets", SCRIPTS / "gsheets.py")
gs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gs)

FULL = {"client_id": "cid", "client_secret": "sec", "refresh_token": "rt",
        "token_uri": "https://example.test/token"}


class Creds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = Path(self.tmp.name) / "auth.json"
        for name, value in (("AUTH", self.auth),
                            ("MCP_CRED_DIR", Path(self.tmp.name) / "mcp")):
            patcher = mock.patch.object(gs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_auth(self, data, mode=0o600):
        self.auth.write_text(json.dumps(data), encoding="utf-8")
        self.auth.chmod(mode)

    def test_full_config_loads(self):
        self.write_auth(FULL)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gs.load_creds()["refresh_token"], "rt")

    def test_missing_field_names_it(self):
        self.write_auth({k: v for k, v in FULL.items() if k != "refresh_token"})
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            gs.load_creds()
        self.assertIn("refresh_token", str(cm.exception))

    def test_broken_json_is_reported_not_traced(self):
        self.auth.write_text("{сломано", encoding="utf-8")
        self.auth.chmod(0o600)
        with self.assertRaises(SystemExit) as cm:
            gs.load_creds()
        self.assertIn("JSON", str(cm.exception))

    def test_loose_permissions_warn(self):
        # refresh_token = пароль: доступный группе файл утекает с домашним каталогом.
        self.write_auth(FULL, mode=0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gs.load_creds()
        self.assertIn("600", err.getvalue())

    def test_strict_permissions_are_silent(self):
        self.write_auth(FULL, mode=0o600)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gs.load_creds()
        self.assertEqual(err.getvalue(), "")

    def test_no_config_and_no_mcp_explains_what_to_do(self):
        with self.assertRaises(SystemExit) as cm:
            gs.load_creds()
        self.assertIn("client_id", str(cm.exception))

    def test_mcp_fallback_warns_where_it_took_them(self):
        gs.MCP_CRED_DIR.mkdir(parents=True)
        (gs.MCP_CRED_DIR / "acc.json").write_text(json.dumps(FULL), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            creds = gs.load_creds()
        self.assertEqual(creds["client_id"], "cid")
        self.assertIn("workspace-mcp", err.getvalue())

    def test_several_mcp_accounts_refuse_to_guess(self):
        gs.MCP_CRED_DIR.mkdir(parents=True)
        for name in ("a.json", "b.json"):
            (gs.MCP_CRED_DIR / name).write_text(json.dumps(FULL), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            gs.load_creds()
        self.assertIn("несколько аккаунтов", str(cm.exception))

    def test_oauth_states_is_not_an_account(self):
        gs.MCP_CRED_DIR.mkdir(parents=True)
        (gs.MCP_CRED_DIR / "oauth_states.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            gs.load_creds()
        self.assertIn("нет учетных данных", str(cm.exception))


class WriteInput(unittest.TestCase):
    def run_write(self, payload):
        with mock.patch.object(gs.sys, "stdin", io.StringIO(payload)):
            with self.assertRaises(SystemExit) as cm:
                gs.cmd_write("token", "sid", "A1")
        return str(cm.exception)

    def test_empty_stdin_explains_format(self):
        self.assertIn("JSON", self.run_write("   "))

    def test_broken_json_is_reported(self):
        self.assertIn("не JSON", self.run_write("[[1,2]"))

    def test_flat_list_rejected(self):
        # [1,2] вместо [[1,2]] - API принял бы это молча и записал не то.
        self.assertIn("массив строк", self.run_write("[1, 2]"))


class TokenErrors(unittest.TestCase):
    """Самый вероятный отказ - протухший refresh_token; он не должен давать трейсбек."""

    def raise_http(self, code, body):
        import urllib.error
        def boom(*a, **kw):
            raise urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body.encode()))
        return boom

    def test_invalid_grant_explains_how_to_fix(self):
        with mock.patch.object(gs.urllib.request, "urlopen",
                               self.raise_http(400, '{"error": "invalid_grant"}')):
            with self.assertRaises(SystemExit) as cm:
                gs.access_token(FULL)
        text = str(cm.exception)
        self.assertIn("invalid_grant", text)
        self.assertIn("протух", text)

    def test_other_http_error_is_reported(self):
        with mock.patch.object(gs.urllib.request, "urlopen", self.raise_http(500, "oops")):
            with self.assertRaises(SystemExit) as cm:
                gs.access_token(FULL)
        self.assertIn("500", str(cm.exception))

    def test_no_network_is_reported(self):
        import urllib.error
        def boom(*a, **kw):
            raise urllib.error.URLError("нет сети")
        with mock.patch.object(gs.urllib.request, "urlopen", boom):
            with self.assertRaises(SystemExit) as cm:
                gs.access_token(FULL)
        self.assertIn("не достучались", str(cm.exception))

    def test_response_without_token_is_reported(self):
        class R:
            def __enter__(self): return io.BytesIO(b'{"no": "token"}')
            def __exit__(self, *a): return False
        with mock.patch.object(gs.urllib.request, "urlopen", lambda *a, **kw: R()):
            with self.assertRaises(SystemExit) as cm:
                gs.access_token(FULL)
        self.assertIn("access_token", str(cm.exception))


class ArgOrder(unittest.TestCase):
    def test_incomplete_command_shows_usage_without_network(self):
        # На опечатке пользователь должен видеть usage, а не ошибку авторизации.
        def boom(*a, **kw):
            raise AssertionError("сеть не должна трогаться при неполной команде")
        with mock.patch.object(gs, "access_token", boom), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gs.main(["gsheets.py", "read"]), 1)
            self.assertEqual(gs.main(["gsheets.py", "sheets"]), 1)
            self.assertEqual(gs.main(["gsheets.py", "чепуха", "x", "y"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
