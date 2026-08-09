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


if __name__ == "__main__":
    unittest.main(verbosity=2)
