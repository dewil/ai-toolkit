#!/usr/bin/env python3
"""Тесты build-lock.py (этап 8, часть a). stdlib-only (unittest + git-fixture).

Запуск: python3 tests/test_build_lock.py
Создаёт временный git-репо с контролируемым manifest+деревом, прогоняет
build-lock через CANON_REPO_ROOT и проверяет descriptor + server-side gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BUILD_LOCK = Path(__file__).resolve().parent.parent / "scripts" / "build-lock.py"


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, env=env,
    ).stdout


def run_build_lock(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CANON_REPO_ROOT"] = str(repo)
    return subprocess.run(
        [sys.executable, str(BUILD_LOCK), *args],
        capture_output=True, text=True, env=env,
    )


MANIFEST = """\
# шапка-докблок, игнорируется парсером
universal:
  - rules/a.md            # правило A
  - scripts/tool.py       # исполняемый скрипт

coding:
  - rules/a.md            # тот же путь в двух секциях (membership множественная)

wiki:
  - skills/demo/SKILL.md  # скилл-папка, раскрывается в файлы
"""


class BuildLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        (self.repo / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
        (self.repo / "rules").mkdir()
        (self.repo / "rules" / "a.md").write_text("rule A body\n", encoding="utf-8")
        (self.repo / "scripts").mkdir()
        tool = self.repo / "scripts" / "tool.py"
        tool.write_text("#!/usr/bin/env python3\nprint('x')\n", encoding="utf-8")
        tool.chmod(0o755)
        skill = self.repo / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("skill entry\n", encoding="utf-8")
        (skill / "helper.py").write_text("# helper\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build(self) -> dict:
        out = self.repo / "canon.lock.json"
        r = run_build_lock(self.repo, "--output", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_descriptor_shape(self) -> None:
        d = self._build()
        self.assertEqual(d["schema_version"], 1)
        self.assertEqual(d["min_cli_version"], 1)
        self.assertIsNone(d["plugin_source"])
        self.assertIn("manifest_digest", d)
        self.assertEqual(len(d["manifest_digest"]), 64)
        # commit_sha НЕ пишется в файл (identity - ambient, дизайн §2, находка 1)
        self.assertNotIn("commit_sha", d)
        self.assertNotIn("rev", d)
        self.assertNotIn("built", d)

    def test_files_and_mode(self) -> None:
        d = self._build()
        files = d["files"]
        self.assertIn("rules/a.md", files)
        self.assertEqual(files["rules/a.md"]["mode"], "100644")
        self.assertEqual(len(files["rules/a.md"]["blob_sha"]), 40)
        # исполняемый бит взят из git-tree (mode), не из blob
        self.assertEqual(files["scripts/tool.py"]["mode"], "100755")

    def test_skill_expanded(self) -> None:
        d = self._build()
        # скилл раскрыт в ВСЕ файлы папки, не только SKILL.md
        self.assertIn("skills/demo/SKILL.md", d["files"])
        self.assertIn("skills/demo/helper.py", d["files"])
        # оба наследуют секцию записи skills/demo/SKILL.md
        self.assertEqual(d["membership"]["skills/demo/helper.py"], ["wiki"])

    def test_membership_multiple(self) -> None:
        d = self._build()
        # rules/a.md в universal и coding -> обе секции, sorted в порядке SECTIONS
        self.assertEqual(d["membership"]["rules/a.md"], ["universal", "coding"])

    def test_digest_deterministic(self) -> None:
        d1 = self._build()
        d2 = self._build()
        self.assertEqual(d1["manifest_digest"], d2["manifest_digest"])
        self.assertEqual(d1["files"], d2["files"])

    def test_gate_ok(self) -> None:
        out = self.repo / "canon.lock.json"
        self._build()
        r = run_build_lock(self.repo, "--check", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("GATE OK", r.stderr)

    def test_gate_fail_on_drift(self) -> None:
        # издать lock, затем изменить файл и recommit -> gate ловит расхождение
        out = self.repo / "canon.lock.json"
        self._build()
        (self.repo / "rules" / "a.md").write_text("rule A CHANGED\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "drift")
        r = run_build_lock(self.repo, "--check", str(out))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("GATE FAIL", r.stderr)

    def test_missing_path_fails(self) -> None:
        # путь в манифесте, которого нет в дереве -> отказ (не молчаливый пропуск)
        (self.repo / "manifest.yaml").write_text(
            MANIFEST + "  - rules/ghost.md\n", encoding="utf-8"
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "ghost")
        r = run_build_lock(self.repo, "--output", str(self.repo / "l.json"))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("ghost", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
