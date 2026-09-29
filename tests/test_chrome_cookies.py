#!/usr/bin/env python3
"""Тесты chrome-cookies.py - бэкап и восстановление куки через CDP. stdlib-only.

Запуск: python3 tests/test_chrome_cookies.py

Покрывается защита dump от перезаписи чужого бэкапа: у каждой машины своя пара
портов, дефолт скрипта - 9222, и забытый --port уводит дамп к чужому браузеру,
а результат ложится в общий файл по домену. Проверка зеркальна той, что в
restore защищает чужой браузер от нашей сессии.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("chrome_cookies", SCRIPTS / "chrome-cookies.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


class DumpTargetConflict(unittest.TestCase):
    def test_other_profile_is_conflict(self):
        existing = {"source": {"profile": "AAA", "browser": "Chrome/1"}, "cookies": []}
        msg = cc.dump_target_conflict(existing, "BBB")
        self.assertIsNotNone(msg)
        self.assertIn("AAA", msg)
        self.assertIn("BBB", msg)

    def test_same_profile_is_allowed(self):
        existing = {"source": {"profile": "AAA"}, "cookies": []}
        self.assertIsNone(cc.dump_target_conflict(existing, "AAA"))

    def test_no_existing_file_is_allowed(self):
        self.assertIsNone(cc.dump_target_conflict(None, "AAA"))

    def test_legacy_dump_without_source_is_allowed(self):
        # Дамп старого формата - голый список кук, метки источника нет:
        # сравнивать не с чем, блокировать нечего.
        self.assertIsNone(cc.dump_target_conflict([{"name": "a"}], "AAA"))

    def test_existing_without_profile_is_allowed(self):
        self.assertIsNone(cc.dump_target_conflict({"source": {"browser": "Chrome/1"}}, "AAA"))

    def test_unknown_current_profile_is_allowed(self):
        # Профиль текущего браузера не определился: блокировать запись своего же
        # бэкапа по такому поводу нельзя - в отличие от restore, где отказ
        # защищает чужой браузер и потому fail-closed оправдан.
        self.assertIsNone(cc.dump_target_conflict({"source": {"profile": "AAA"}}, None))


class DumpForceFlag(unittest.TestCase):
    def test_dump_parser_has_force(self):
        import contextlib
        import io
        import sys
        buf = io.StringIO()
        argv, sys.argv = sys.argv, ["chrome-cookies.py", "dump", "--help"]
        try:
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
                cc.main()
        finally:
            sys.argv = argv
        self.assertIn("--force", buf.getvalue())


def _cookie(name, domain=".example.com", value="v"):
    return {"name": name, "value": value, "domain": domain, "path": "/",
            "expires": -1, "httpOnly": True, "secure": True}


class _FakeSock:
    def close(self):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


class DumpSafe(unittest.TestCase):
    """dump: предыдущая копия .prev, --require-cookie, общие флаги после подкоманды.

    Требование: INV-DATA-18
    Сеть замокана на уровне функций скрипта: browser_ws, _ws_connect, _cdp,
    profile_id, browser_version. Форма ответа _cdp неизвестна из контракта,
    поэтому фейк отдает набор кук и на верхнем уровне, и под "result".
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "example.com.json"
        self.prev = Path(str(self.out) + ".prev")
        self.cookies = []
        self.ports = []

    def _run(self, *argv):
        """Запускает main() с моками; возвращает (код, stdout+stderr)."""
        import contextlib
        import io
        import sys
        from unittest import mock

        cookies = self.cookies

        def fake_browser_ws(port):
            self.ports.append(port)
            return "ws://127.0.0.1:%s/devtools/browser/x" % port

        def fake_cdp(s, msg_id, method, params=None):
            return {"cookies": list(cookies), "result": {"cookies": list(cookies)}}

        buf = io.StringIO()
        old_argv, sys.argv = sys.argv, ["chrome-cookies.py", *argv]
        rc = None
        try:
            with mock.patch.object(cc, "browser_ws", fake_browser_ws), \
                    mock.patch.object(cc, "_ws_connect", lambda *a, **k: _FakeSock()), \
                    mock.patch.object(cc, "_cdp", fake_cdp), \
                    mock.patch.object(cc, "profile_id", lambda *a, **k: "AAA"), \
                    mock.patch.object(cc, "browser_version",
                                      lambda port: {"Browser": "Chrome/1",
                                                    "webSocketDebuggerUrl": "ws://127.0.0.1/x"}), \
                    contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    rc = cc.main()
                except SystemExit as e:
                    rc = e.code
        finally:
            sys.argv = old_argv
        if rc is None:
            rc = 0
        return rc, buf.getvalue()

    def _dump(self, names, *extra):
        self.cookies = [_cookie(n) for n in names]
        return self._run("dump", "--domain", "example.com", "--out", str(self.out), *extra)

    def test_repeat_dump_keeps_previous_set_in_prev(self):
        """Требование: INV-DATA-18 (критерий 1)"""
        rc, _ = self._dump(["a", "b"])
        self.assertEqual(rc, 0)
        first = self.out.read_bytes()
        rc, _ = self._dump(["c", "d"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.prev.exists(), ".prev не создан")
        self.assertEqual(self.prev.read_bytes(), first)
        self.assertNotEqual(self.out.read_bytes(), first)
        self.assertIn('"c"', self.out.read_text(encoding="utf-8"))

    def test_prev_rotates_each_dump(self):
        """Требование: INV-DATA-18 (критерий 1)"""
        self._dump(["a"])
        self._dump(["b"])
        second = self.out.read_bytes()
        self._dump(["c"])
        self.assertEqual(self.prev.read_bytes(), second)

    def test_no_temp_files_left_after_dump(self):
        """Требование: INV-DATA-18 (атомарная запись)"""
        self._dump(["a"])
        self._dump(["b"])
        self.assertEqual({p.name for p in self.dir.iterdir()},
                         {self.out.name, self.prev.name})

    def test_files_are_mode_600_even_with_open_umask(self):
        """Требование: INV-DATA-18 (критерий 4)"""
        import os
        old = os.umask(0)
        try:
            self._dump(["a"])
            self._dump(["b"])
        finally:
            os.umask(old)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.prev.stat().st_mode & 0o777, 0o600)

    def test_prev_is_600_even_if_old_backup_was_644(self):
        """Требование: INV-DATA-18 (критерий 4)"""
        import os
        self._dump(["a"])
        os.chmod(self.out, 0o644)
        self._dump(["b"])
        self.assertEqual(self.prev.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)

    def test_require_cookie_missing_leaves_backup_and_prev_untouched(self):
        """Требование: INV-DATA-18 (критерий 2)"""
        self._dump(["a"])
        self._dump(["b"])
        main_before, prev_before = self.out.read_bytes(), self.prev.read_bytes()
        rc, output = self._dump(["x", "y"], "--require-cookie", "li_at")
        self.assertNotEqual(rc, 0)
        self.assertIn("сессия не жива: нет куки li_at", output)
        self.assertEqual(self.out.read_bytes(), main_before)
        self.assertEqual(self.prev.read_bytes(), prev_before)

    def test_require_cookie_missing_creates_no_files(self):
        """Требование: INV-DATA-18 (критерий 2)"""
        rc, _ = self._dump(["x"], "--require-cookie", "li_at")
        self.assertNotEqual(rc, 0)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.prev.exists())

    def test_require_cookie_missing_code_is_distinct(self):
        """Требование: INV-DATA-18 (отдельный код; трактовка "не 0/1/2" - см. вопросы)"""
        rc, _ = self._dump(["x"], "--require-cookie", "li_at")
        self.assertNotIn(rc, (0, 1, 2))

    def test_require_cookie_present_writes_normally(self):
        """Требование: INV-DATA-18"""
        self._dump(["a"])
        first = self.out.read_bytes()
        rc, _ = self._dump(["li_at", "b"], "--require-cookie", "li_at")
        self.assertEqual(rc, 0)
        self.assertIn("li_at", self.out.read_text(encoding="utf-8"))
        self.assertEqual(self.prev.read_bytes(), first)

    def test_require_cookie_repeatable_any_missing_refuses(self):
        """Требование: INV-DATA-18 (флаг повторяемый)"""
        self._dump(["a"])
        before = self.out.read_bytes()
        rc, output = self._dump(["li_at"], "--require-cookie", "li_at",
                                "--require-cookie", "JSESSIONID")
        self.assertNotEqual(rc, 0)
        self.assertIn("нет куки JSESSIONID", output)
        self.assertEqual(self.out.read_bytes(), before)

    def test_require_cookie_repeatable_all_present_ok(self):
        """Требование: INV-DATA-18 (флаг повторяемый)"""
        rc, _ = self._dump(["li_at", "JSESSIONID"], "--require-cookie", "li_at",
                           "--require-cookie", "JSESSIONID")
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())

    def test_port_after_subcommand_accepted(self):
        """Требование: INV-DATA-18 (критерий 3)"""
        self.cookies = [_cookie("a")]
        rc, _ = self._run("dump", "--port", "9223", "--domain", "example.com",
                          "--out", str(self.out))
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.exists())
        self.assertEqual([int(p) for p in self.ports], [9223])

    def test_port_before_and_after_subcommand_equivalent(self):
        """Требование: INV-DATA-18 (критерий 3)"""
        self.cookies = [_cookie("a")]
        rc1, _ = self._run("--port", "9223", "dump", "--domain", "example.com",
                           "--out", str(self.out))
        content1 = self.out.read_bytes()
        ports1 = [int(p) for p in self.ports]
        self.out.unlink()
        if self.prev.exists():
            self.prev.unlink()
        self.ports.clear()
        rc2, _ = self._run("dump", "--port", "9223", "--domain", "example.com",
                           "--out", str(self.out))
        self.assertEqual(rc1, rc2)
        self.assertEqual(ports1, [int(p) for p in self.ports])
        self.assertEqual(content1, self.out.read_bytes())

    def test_port_after_subcommand_for_list(self):
        """Требование: INV-DATA-18 (общие флаги и до, и после подкоманды)"""
        rc, _ = self._run("list", "--port", "9223")
        self.assertNotEqual(rc, 2, "argparse отверг --port после подкоманды")


if __name__ == "__main__":
    unittest.main(verbosity=2)
