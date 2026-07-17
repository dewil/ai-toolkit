#!/usr/bin/env python3
"""End-to-end интеграция этапа 8: build-lock -> canon-delta sync -> recover.

Запуск: python3 tests/test_integration.py
Поднимает настоящий git-репозиторий как канон-зеркало, издает descriptor через
build-lock.py, применяет его в чистый проект командой `canon-delta.py sync`
(материализация блобов через `git cat-file blob` из зеркала), проверяет диск +
state, второй прогон = fast-path up-to-date. Отдельно - recover-CLI на хвосте WAL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
BUILD_LOCK = SCRIPTS / "build-lock.py"
CANON_DELTA = SCRIPTS / "canon-delta.py"


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, env=env).stdout


MANIFEST = """\
universal:
  - rules/a.md
  - rules/b.md
  - scripts/tool.py
"""


class IntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.mirror = base / "canon"       # git-зеркало канона
        self.project = base / "project"    # целевой проект
        self.lock = base / "canon.lock.json"
        self.mirror.mkdir()
        (self.project / ".claude").mkdir(parents=True)

        git(self.mirror, "init", "-q")
        (self.mirror / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
        (self.mirror / "rules").mkdir()
        (self.mirror / "rules" / "a.md").write_text("canon A v1\n", encoding="utf-8")
        (self.mirror / "rules" / "b.md").write_text("canon B v1\n", encoding="utf-8")
        (self.mirror / "scripts").mkdir()
        tool = self.mirror / "scripts" / "tool.py"
        tool.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")
        tool.chmod(0o755)
        git(self.mirror, "add", "-A")
        git(self.mirror, "commit", "-q", "-m", "canon v1")
        self.commit = git(self.mirror, "rev-parse", "HEAD").strip()

        # intent проекта (только universal)
        (self.project / ".claude" / "canon.intent.yaml").write_text(
            "project_type: []\ntrack: stable\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build_lock(self) -> None:
        env = dict(os.environ, CANON_REPO_ROOT=str(self.mirror))
        r = subprocess.run([sys.executable, str(BUILD_LOCK), "--output", str(self.lock)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def sync(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CANON_DELTA), "--root", str(self.project), "sync",
             "--lock", str(self.lock), "--mirror", str(self.mirror), "--target", self.commit],
            capture_output=True, text=True)

    def state(self) -> dict:
        return json.loads((self.project / ".claude" / "canon.state.json").read_text())

    def test_fresh_apply_then_fast_path(self) -> None:
        self.build_lock()
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "applied")
        # блобы материализованы из зеркала на диск проекта по fs_rel-маппингу:
        # rules/ -> под .claude/, scripts/ -> в корень проекта
        self.assertEqual((self.project / ".claude" / "rules" / "a.md").read_text(), "canon A v1\n")
        self.assertEqual((self.project / ".claude" / "rules" / "b.md").read_text(), "canon B v1\n")
        self.assertFalse((self.project / "rules").exists())  # корень не замусорен
        self.assertEqual((self.project / "scripts" / "tool.py").read_text(),
                         "#!/usr/bin/env python3\nprint('hi')\n")
        # +x бит перенесен из descriptor.mode
        self.assertTrue(os.access(self.project / "scripts" / "tool.py", os.X_OK))
        st = self.state()
        self.assertEqual(st["applied_release"]["commit_sha"], self.commit)
        self.assertEqual(st["rollout_record"][-1], self.commit)
        # WAL очищен
        self.assertFalse((self.project / ".claude" / ".canon-journal.json").exists())

        # второй прогон - fast-path, 0 работы
        r2 = self.sync()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(json.loads(r2.stdout)["status"], "up-to-date")

    def test_outdated_ff_after_canon_moves(self) -> None:
        self.build_lock()
        self.assertEqual(self.sync().returncode, 0)
        # канон двигает rules/a.md -> новая ревизия, пере-издать lock
        (self.mirror / "rules" / "a.md").write_text("canon A v2\n", encoding="utf-8")
        git(self.mirror, "add", "-A")
        git(self.mirror, "commit", "-q", "-m", "canon v2")
        self.commit = git(self.mirror, "rev-parse", "HEAD").strip()
        self.build_lock()
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.project / ".claude" / "rules" / "a.md").read_text(), "canon A v2\n")
        self.assertEqual(self.state()["applied_release"]["commit_sha"], self.commit)

    def test_recover_cli_finishes_committed_journal(self) -> None:
        self.build_lock()
        # ставим committed-журнал вручную (симуляция crash после flip), затем recover CLI
        env = dict(os.environ)
        # применяем через python API проще: но CLI-путь важнее - используем sync с прерыванием
        # эмулируем: sync применит все, потом мы вернем committed-журнал и запустим recover
        self.assertEqual(self.sync().returncode, 0)
        # искусственно оставить committed-журнал (как будто clear не успел)
        journal = {
            "header": {"release_identity": self.commit,
                       "release": {"commit_sha": self.commit, "manifest_digest": "x"},
                       "phase": "committed", "scope": "release", "pass_id": "PX"},
            "files": [],
        }
        (self.project / ".claude" / ".canon-journal.json").write_text(
            json.dumps(journal), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(CANON_DELTA), "--root", str(self.project), "recover",
             "--mirror", str(self.mirror)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "rolled-forward")
        self.assertFalse((self.project / ".claude" / ".canon-journal.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
