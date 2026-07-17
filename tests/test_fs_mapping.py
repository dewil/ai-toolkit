"""Маппинг canonical->project путей (фикс path-mapping, найден на этапе 8c).

Канонический путь = путь в дереве toolkit (rules/, agents/, skills/, commands/,
scripts/). В проекте канон живет под .claude/, КРОМЕ scripts/ - они в корне
(конвенция vault и CLAUDE-проектов; раньше знание жило в LLM-промпте /canon).
Идентичность (журнал, state.file_hashes, membership, резолюции) остается
канонической - маппинг применяется только на ФС-границе.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "canon-delta.py"
_spec = importlib.util.spec_from_file_location("canon_delta_fsmap", _MOD_PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)


def blob(content: str) -> str:
    return cd.git_blob_sha(content.encode("utf-8"))


class FsRelTest(unittest.TestCase):
    def test_regular_canon_goes_under_claude(self):
        self.assertEqual(cd.fs_rel("rules/typography-ru.md"), ".claude/rules/typography-ru.md")
        self.assertEqual(cd.fs_rel("agents/architect.md"), ".claude/agents/architect.md")
        self.assertEqual(cd.fs_rel("skills/md-pdf/SKILL.md"), ".claude/skills/md-pdf/SKILL.md")
        self.assertEqual(cd.fs_rel("commands/canon.md"), ".claude/commands/canon.md")

    def test_scripts_stay_at_project_root(self):
        self.assertEqual(cd.fs_rel("scripts/md-pdf.py"), "scripts/md-pdf.py")
        self.assertEqual(cd.fs_rel("scripts/telegram-snapshot.py"), "scripts/telegram-snapshot.py")

    def test_claude_prefixed_passes_through(self):
        # дефенсивно: уже-мапленный путь не двоится в .claude/.claude/
        self.assertEqual(cd.fs_rel(".claude/rules/x.md"), ".claude/rules/x.md")

    def test_fs_path_joins_root(self):
        root = Path("/tmp/proj")
        self.assertEqual(cd.fs_path(root, "rules/a.md"), root / ".claude/rules/a.md")
        self.assertEqual(cd.fs_path(root, "scripts/t.py"), root / "scripts/t.py")


class MappedIoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir(parents=True)
        self.state_path = self.root / ".claude" / "canon.state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _put(self, canonical: str, content: str) -> str:
        p = cd.fs_path(self.root, canonical)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        p.chmod(0o644)
        return blob(content)

    def test_read_local_looks_at_mapped_path(self):
        h = self._put("rules/a.md", "body")
        got = cd.read_local(self.root, "rules/a.md")
        self.assertTrue(got["exists"])
        self.assertEqual(got["sha"], h)
        # файл, лежащий по ЛИТЕРАЛЬНОМУ канон-пути в корне, канонам не считается
        (self.root / "rules").mkdir(exist_ok=True)
        (self.root / "rules" / "b.md").write_text("x", encoding="utf-8")
        self.assertFalse(cd.read_local(self.root, "rules/b.md")["exists"])

    def test_classify_finds_canon_under_claude(self):
        h = self._put("rules/a.md", "body")
        desc = {"schema_version": 1, "min_cli_version": 1,
                "files": {"rules/a.md": {"blob_sha": h, "mode": "100644"}},
                "membership": {"rules/a.md": ["universal"]},
                "plugin_source": None, "manifest_digest": "d" * 64}
        intent = {"project_type": ["universal"], "skip_sync": [],
                  "local_only": [], "overrides": []}
        state = cd.empty_state()
        state["file_hashes"] = {"rules/a.md": {"sha": h, "mode": "100644"}}
        res = cd.classify(intent, state, desc, self.root)
        klass = {r["path"]: r["klass"] for r in res}
        self.assertEqual(klass["rules/a.md"], cd.UP_TO_DATE)

    def test_apply_places_files_at_mapped_paths(self):
        rule_sha, script_sha = blob("R"), blob("S")
        desc = {"schema_version": 1, "min_cli_version": 1,
                "files": {"rules/a.md": {"blob_sha": rule_sha, "mode": "100644"},
                          "scripts/t.py": {"blob_sha": script_sha, "mode": "100755"}},
                "membership": {"rules/a.md": ["universal"], "scripts/t.py": ["universal"]},
                "plugin_source": None, "manifest_digest": "d" * 64}
        intent = {"project_type": ["universal"], "skip_sync": [],
                  "local_only": [], "overrides": []}
        state = cd.empty_state()
        src = cd.DictBlobSource({rule_sha: b"R", script_sha: b"S"})
        cd.apply_release(self.root, intent, state, desc, "c" * 40, src, self.state_path)
        self.assertEqual((self.root / ".claude/rules/a.md").read_text(), "R")
        self.assertEqual((self.root / "scripts/t.py").read_text(), "S")
        self.assertFalse((self.root / "rules").exists())  # корень не замусорен

    def test_recovery_rollback_restores_mapped_path(self):
        # modify с крэшем после rename -> recovery prepare-фазы откатывает
        # пре-имидж по МАПЛЕННОМУ пути
        old_sha = self._put("rules/a.md", "old")
        new_sha = blob("new")
        state = cd.empty_state()
        state["file_hashes"] = {"rules/a.md": {"sha": old_sha, "mode": "100644"}}
        desc = {"files": {"rules/a.md": {"blob_sha": new_sha, "mode": "100644"}},
                "membership": {"rules/a.md": ["universal"]}}
        items = [{"path": "rules/a.md", "klass": cd.OUTDATED}]
        journal = cd.build_journal(items, state, {"commit_sha": "c" * 40,
                                                  "manifest_digest": "d" * 64},
                                   "release", cd._new_pass_id(), desc)
        src = cd.DictBlobSource({new_sha: b"new"})
        with self.assertRaises(cd.CrashSim):
            cd.prepare(self.root, journal, src,
                       fault=self._fault_at("pre-flip"))
        # журнал остался в prepare -> recovery откатывает, файл цел по мапленному пути
        cd.recover(self.root, src, self.state_path)
        self.assertEqual((self.root / ".claude/rules/a.md").read_text(), "old")
        self.assertIsNone(cd.read_journal(self.root))

    @staticmethod
    def _fault_at(target: str):
        def f(name: str) -> None:
            if name == target:
                raise cd.CrashSim(name)
        return f


if __name__ == "__main__":
    unittest.main()
