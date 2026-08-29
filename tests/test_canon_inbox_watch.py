#!/usr/bin/env python3
"""Тесты .claude/scripts/canon-inbox-watch.py - наблюдение за очередью канона.
stdlib-only (unittest), отправка и файловая система подменяются.

Запуск: python3 tests/test_canon_inbox_watch.py

Наблюдатель - инструмент самого toolkit-репо, не канон. Тесты ему нужны,
потому что он стал нести нагрузку: именно он замечает бриф, застрявший в
проекте. Ключевые инварианты: отсутствие очереди НЕ отменяет проверку проектов
(это и есть состояние первой потери); "не проверено" отличается от "чисто".
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WATCHER = REPO / ".claude" / "scripts" / "canon-inbox-watch.py"


def load(inbox: Path, roots: list[Path]):
    os.environ["CANON_INBOX"] = str(inbox)
    spec = importlib.util.spec_from_file_location("canon_inbox_watch", WATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PROJECT_ROOTS = roots
    mod.BRIEFER = REPO / "scripts" / "canon-brief.py"
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "inbox"
        self.roots = self.root / "roots"
        self.roots.mkdir()
        self.w = load(self.inbox, [self.roots])
        self.addCleanup(self.tmp.cleanup)

    def project(self, name="client", depth=""):
        p = self.roots / depth / name if depth else self.roots / name
        (p / "toolkit-log" / "upstream-pending").mkdir(parents=True)
        return p

    def brief(self, project: Path, name="lost.md", body="# бриф\n"):
        (project / "toolkit-log" / "upstream-pending" / name).write_text(body, encoding="utf-8")

    def run_main(self, argv=("--dry-run",)):
        import sys
        orig = sys.argv
        sys.argv = ["watch", *argv]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = self.w.main()
        finally:
            sys.argv = orig
        return rc, out.getvalue()


class MissingQueue(Base):
    def test_absent_inbox_still_checks_projects(self):
        """Самое вероятное состояние первой потери: бриф написали руками,
        доставить забыли, класть в очередь было некому. Прежняя версия тут
        выходила с нулем, не заглянув в проекты вовсе."""
        self.brief(self.project())
        rc, out = self.run_main()
        self.assertIn("ПАПКИ НЕТ", out)
        self.assertIn("РАСХОЖДЕНИЕ ПРОЕКТ/ОЧЕРЕДЬ", out)
        self.assertIn("client/lost.md (не доставлен)", out)

    def test_absent_inbox_and_no_projects_is_clean(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("ПАПКИ НЕТ", out)
        self.assertNotIn("РАСХОЖДЕНИЕ", out)


class Detection(Base):
    def test_delivered_brief_not_reported(self):
        p = self.project()
        self.brief(p)
        self.inbox.mkdir(parents=True)
        (self.inbox / "2026-09-01-client-lost.md").write_text("# бриф\n", encoding="utf-8")
        lost, broken = self.w.stranded()
        self.assertEqual((lost, broken), ([], []))

    def test_stranded_reported(self):
        self.brief(self.project())
        lost, broken = self.w.stranded()
        self.assertEqual(lost, ["client/lost.md (не доставлен)"])
        self.assertEqual(broken, [])

    def test_deep_project_found(self):
        self.brief(self.project(depth="a/b"))
        lost, _ = self.w.stranded()
        self.assertEqual(lost, ["client/lost.md (не доставлен)"])

    def test_two_projects_reported_separately(self):
        self.brief(self.project("alpha"), "one.md")
        self.brief(self.project("beta"), "two.md")
        lost, _ = self.w.stranded()
        self.assertEqual(lost, ["alpha/one.md (не доставлен)", "beta/two.md (не доставлен)"])


class NotChecked(Base):
    """"Не проверено" обязано отличаться от "чисто" (rules/silent-failure.md)."""

    def test_missing_briefer_is_reported_not_silent(self):
        self.brief(self.project())
        self.w.BRIEFER = Path("/nope/canon-brief.py")
        lost, broken = self.w.stranded()
        self.assertEqual(lost, [])
        self.assertTrue(broken)

    def test_unparsable_child_output_is_broken_not_clean(self):
        """Код 4 означает расхождение; ни одного имени в выводе - значит
        разобрать его не удалось, а не что все чисто."""
        self.project()
        import subprocess
        orig = self.w.subprocess.run
        self.w.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
            a[0], 4, stdout="что-то не то\n", stderr="")
        try:
            lost, broken = self.w.stranded()
        finally:
            self.w.subprocess.run = orig
        self.assertEqual(lost, [])
        self.assertTrue(broken)
        self.assertIn("rc=4", broken[0])

    def test_child_crash_is_broken(self):
        self.project()
        import subprocess
        orig = self.w.subprocess.run
        self.w.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, stdout="", stderr="бум")
        try:
            lost, broken = self.w.stranded()
        finally:
            self.w.subprocess.run = orig
        self.assertEqual(lost, [])
        self.assertTrue(broken)

    def test_broken_makes_run_nonzero(self):
        self.project()
        self.w.BRIEFER = Path("/nope/canon-brief.py")
        rc, out = self.run_main()
        self.assertNotEqual(rc, 0)
        self.assertIn("ПРОВЕРКА НЕ ВЫПОЛНЕНА", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
