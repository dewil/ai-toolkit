#!/usr/bin/env python3
"""Тесты canon-migrate.py (этап 8, §6 миграция R12). stdlib-only.

Запуск: python3 tests/test_migrate.py
Проверяет расщепление canon.yaml -> intent/state/ledger, перехеширование
git-blob-sha (не перенос старого sha256), дедуп pending и КЛЮЧЕВОЙ bootstrap-инвариант:
после миграции classify против дескриптора из тех же локальных байт = up-to-date.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SCR = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cd = _load("canon_delta", _SCR / "canon-delta.py")
cm = _load("canon_migrate", _SCR / "canon-migrate.py")


OLD_CANON = """\
project_type: [wiki, coding]
canon:
  repo: https://github.com/dewil/claude-toolkit
  synced_at: 2026-07-14
files:
  - rules/a.md
  - scripts/tool.py
  - rules/ghost.md          # нет локально -> пропустится
file_hashes:
  rules/a.md: 1111111111111111111111111111111111111111111111111111111111111111
  scripts/tool.py: 2222222222222222222222222222222222222222222222222222222222222222
"""


class MigrateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir(parents=True)
        (self.root / ".claude" / "canon.yaml").write_text(OLD_CANON, encoding="utf-8")
        (self.root / "rules").mkdir()
        (self.root / "rules" / "a.md").write_text("rule A body\n", encoding="utf-8")
        (self.root / "scripts").mkdir()
        tool = self.root / "scripts" / "tool.py"
        tool.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        tool.chmod(0o755)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _migrate(self, **kw):
        ns = type("NS", (), {"root": str(self.root), "canon": None,
                             "harvester_ledger": None, "force": False})()
        for k, v in kw.items():
            setattr(ns, k, v)
        return cm.cmd_migrate(ns)

    def _intent(self):
        return cd.load_intent(self.root / ".claude" / "canon.intent.yaml")

    def _state(self):
        return cd.load_state(self.root / ".claude" / "canon.state.json")

    def test_intent_fields(self) -> None:
        self._migrate()
        it = self._intent()
        self.assertEqual(it["project_type"], ["wiki", "coding"])
        self.assertEqual(it["track"], "stable")

    def test_state_rehashed_git_blob_not_sha256(self) -> None:
        self._migrate()
        st = self._state()
        fh = st["file_hashes"]
        # git-blob-sha локального файла, НЕ старый sha256 из canon.yaml
        self.assertEqual(fh["rules/a.md"]["sha"], cd.git_blob_sha(b"rule A body\n"))
        self.assertNotEqual(fh["rules/a.md"]["sha"], "1" * 64)
        self.assertEqual(fh["scripts/tool.py"]["mode"], "100755")  # +x перенесен
        # applied_release не выставлен (пин узнается на первом sync)
        self.assertIsNone(st["applied_release"])

    def test_missing_file_skipped(self) -> None:
        out = json.loads(_capture(self._migrate))
        self.assertIn("rules/ghost.md", out["skipped"])
        self.assertNotIn("rules/ghost.md", self._state()["file_hashes"])

    def test_bootstrap_invariant_all_uptodate(self) -> None:
        """После миграции дескриптор из ТЕХ ЖЕ локальных байт -> все up-to-date
        (не untracked-collision). База = локальные файлы на момент synced_at."""
        self._migrate()
        intent, state = self._intent(), self._state()
        descriptor = {
            "schema_version": 1, "manifest_digest": "d",
            "files": {
                "rules/a.md": {"blob_sha": cd.git_blob_sha(b"rule A body\n"), "mode": "100644"},
                "scripts/tool.py": {"blob_sha": cd.git_blob_sha(b"#!/usr/bin/env python3\n"), "mode": "100755"},
            },
            "membership": {"rules/a.md": ["wiki"], "scripts/tool.py": ["coding"]},
            "min_cli_version": 1, "plugin_source": None,
        }
        results = cd.classify(intent, state, descriptor, self.root)
        klasses = {r["path"]: r["klass"] for r in results}
        self.assertEqual(klasses["rules/a.md"], cd.UP_TO_DATE)
        self.assertEqual(klasses["scripts/tool.py"], cd.UP_TO_DATE)

    def test_refuse_without_force(self) -> None:
        self._migrate()
        with self.assertRaises(SystemExit):
            self._migrate()  # intent/state уже есть -> die без --force
        # с --force проходит
        self.assertEqual(self._migrate(force=True), 0)

    def test_pending_dedup_with_harvester(self) -> None:
        # старый canon.yaml с upstream_pending + внешний harvester-ledger
        (self.root / ".claude" / "canon.yaml").write_text(
            OLD_CANON + "upstream_pending:\n  - toolkit-log/pending/brief-x.md\n", encoding="utf-8")
        (self.root / "toolkit-log" / "pending").mkdir(parents=True)
        brief = self.root / "toolkit-log" / "pending" / "brief-x.md"
        brief.write_text("brief content X\n", encoding="utf-8")
        import hashlib
        cid = hashlib.sha256(b"brief content X\n").hexdigest()
        hp = self.root / "harvester.json"
        hp.write_text(json.dumps({"upstream_pending": [
            {"candidate_id": cid, "brief_path": "toolkit-log/pending/brief-x.md"}]}), encoding="utf-8")
        self._migrate(harvester_ledger=str(hp), force=True)
        ledger = json.loads((self.root / ".claude" / "canon.ledger.json").read_text())
        # один и тот же бриф из двух источников -> одна запись (дедуп по candidate-id)
        self.assertEqual(len(ledger["upstream_pending"]), 1)
        self.assertEqual(ledger["upstream_pending"][0]["source"], "harvester")


def _capture(fn):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main(verbosity=2)
