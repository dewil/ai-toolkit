#!/usr/bin/env python3
"""Тесты UNION-классификатора canon-delta.py (этап 8, §3 п.4). stdlib-only.

Запуск: python3 tests/test_classify.py
Грузит canon-delta.py через importlib (имя с дефисом), проверяет чистую
функцию classify() на fixture с временным деревом на диске. Покрывает все
классы + разграничение intent/membership (T1) + инвалидацию resolution (R6).
"""
from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "canon-delta.py"
_spec = importlib.util.spec_from_file_location("canon_delta", _MOD_PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)


def blob(content: str) -> str:
    return cd.git_blob_sha(content.encode("utf-8"))


class ClassifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def mkfile(self, rel: str, content: str, ex: bool = False) -> str:
        # rel канонический; на диске файл живет по fs_path (маппинг .claude/)
        p = cd.fs_path(self.root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        if ex:
            p.chmod(0o755)
        else:
            p.chmod(0o644)
        return blob(content)

    def mksymlink(self, rel: str, target: str) -> None:
        p = cd.fs_path(self.root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)

    def descriptor(self, files: dict, membership: dict) -> dict:
        return {"schema_version": 1, "min_cli_version": 1,
                "files": files, "membership": membership, "plugin_source": None}

    def intent(self, ptype=("universal",), skip=(), local=(), overrides=()) -> dict:
        # project_type тут = типы БЕЗ universal (universal добавляется в classify)
        return {"project_type": list(ptype), "track": "stable",
                "skip_sync": list(skip), "local_only": list(local),
                "overrides": list(overrides)}

    def state(self, file_hashes: dict, records=()) -> dict:
        s = cd.empty_state()
        s["file_hashes"] = file_hashes
        s["resolution_records"] = list(records)
        return s

    def klass_of(self, results: list[dict], path: str) -> str | None:
        for r in results:
            if r["path"] == path:
                return r["klass"]
        return None

    # --- базовые классы ---

    def test_up_to_date(self) -> None:
        h = self.mkfile("rules/a.md", "body")
        desc = self.descriptor({"rules/a.md": {"blob_sha": h, "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": h, "mode": "100644"}})
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.UP_TO_DATE)

    def test_outdated(self) -> None:
        base = self.mkfile("rules/a.md", "old")  # local == base (не трогали)
        up = blob("new-upstream")
        desc = self.descriptor({"rules/a.md": {"blob_sha": up, "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": base, "mode": "100644"}})
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.OUTDATED)

    def test_local_edit(self) -> None:
        # local != base, base == upstream (upstream не двигался)
        self.mkfile("rules/a.md", "локально изменено")
        same = blob("оригинал")
        desc = self.descriptor({"rules/a.md": {"blob_sha": same, "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": same, "mode": "100644"}})
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.LOCAL_EDIT)

    def test_conflict(self) -> None:
        # local != base И upstream двинулся (base != upstream, local != upstream)
        self.mkfile("rules/a.md", "локальная правка")
        desc = self.descriptor({"rules/a.md": {"blob_sha": blob("upstream new"), "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": blob("base old"), "mode": "100644"}})
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.CONFLICT)

    def test_new(self) -> None:
        # applicable, не в state, файла нет на диске
        desc = self.descriptor({"rules/a.md": {"blob_sha": blob("x"), "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        r = cd.classify(self.intent(), self.state({}), desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.NEW)

    def test_untracked_collision(self) -> None:
        # applicable, не в state, НО файл существует на диске (находка 4)
        self.mkfile("rules/a.md", "уже лежит локально")
        desc = self.descriptor({"rules/a.md": {"blob_sha": blob("x"), "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        r = cd.classify(self.intent(), self.state({}), desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.UNTRACKED_COLLISION)

    def test_missing_local(self) -> None:
        # в state, applicable, файла нет на диске
        desc = self.descriptor({"rules/a.md": {"blob_sha": blob("x"), "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": blob("x"), "mode": "100644"}})
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.MISSING_LOCAL)

    def test_symlink_conflict(self) -> None:
        self.mksymlink("rules/a.md", "/etc/passwd")
        desc = self.descriptor({"rules/a.md": {"blob_sha": blob("x"), "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": blob("x"), "mode": "100644"}})
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.CONFLICT)

    def test_mode_only_drift_not_green(self) -> None:
        # содержимое совпало с upstream, но потерян +x -> НЕ up-to-date
        h = self.mkfile("scripts/t.py", "print(1)", ex=False)  # 100644 на диске
        desc = self.descriptor({"scripts/t.py": {"blob_sha": h, "mode": "100755"}},
                               {"scripts/t.py": ["universal"]})
        st = self.state({"scripts/t.py": {"sha": h, "mode": "100755"}})
        r = cd.classify(self.intent(), st, desc, self.root)
        # local=(h,100644) != upstream=(h,100755); local==? base=(h,100755) нет -> conflict/outdated
        self.assertNotEqual(self.klass_of(r, "scripts/t.py"), cd.UP_TO_DATE)

    # --- resolution-records ---

    def test_resolved_local(self) -> None:
        lh = self.mkfile("rules/a.md", "мой вариант")
        uh = blob("upstream вариант")
        desc = self.descriptor({"rules/a.md": {"blob_sha": uh, "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": blob("base"), "mode": "100644"}},
                        records=[{"path": "rules/a.md", "base_sha": blob("base"),
                                  "local_sha": lh, "upstream_sha": uh, "release": "c1"}])
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.RESOLVED_LOCAL)

    def test_resolution_invalidated_on_upstream_move(self) -> None:
        # record был для upstream U1, но канон сдвинул путь на U2 -> record не матчит -> conflict (R6)
        lh = self.mkfile("rules/a.md", "мой вариант")
        u2 = blob("upstream ДВИНУЛСЯ")
        desc = self.descriptor({"rules/a.md": {"blob_sha": u2, "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": blob("base"), "mode": "100644"}},
                        records=[{"path": "rules/a.md", "base_sha": blob("base"),
                                  "local_sha": lh, "upstream_sha": blob("upstream U1"),
                                  "release": "c1"}])
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/a.md"), cd.CONFLICT)

    # --- scope: retired / removed / managed-but-excluded (T1) ---

    def test_removed_upstream(self) -> None:
        # путь в state, глобально ОТСУТСТВУЕТ в descriptor -> removed-upstream (эскалация)
        desc = self.descriptor({}, {})
        st = self.state({"rules/gone.md": {"sha": blob("x"), "mode": "100644"}})
        r = cd.classify(self.intent(), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/gone.md"), cd.REMOVED_UPSTREAM)

    def test_retired_from_scope(self) -> None:
        # путь в state, ЖИВ в descriptor, но membership не пересекает type-set проекта
        # (проект wiki, а путь только в coding) -> retired-from-scope
        desc = self.descriptor({"rules/c.md": {"blob_sha": blob("x"), "mode": "100644"}},
                               {"rules/c.md": ["coding"]})
        st = self.state({"rules/c.md": {"sha": blob("x"), "mode": "100644"}})
        r = cd.classify(self.intent(ptype=["wiki"]), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/c.md"), cd.RETIRED_FROM_SCOPE)

    def test_managed_but_excluded_intent_first(self) -> None:
        # путь выпал бы из scope, НО он в overrides -> managed-but-excluded, НЕ retire
        # (порядок проверки: intent-исключение первым)
        desc = self.descriptor({"rules/c.md": {"blob_sha": blob("x"), "mode": "100644"}},
                               {"rules/c.md": ["coding"]})
        st = self.state({"rules/c.md": {"sha": blob("x"), "mode": "100644"}})
        r = cd.classify(self.intent(ptype=["wiki"], overrides=["rules/c.md"]), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/c.md"), cd.MANAGED_BUT_EXCLUDED)

    def test_skip_sync_excluded_not_retire(self) -> None:
        # путь В type-set проекта, но в skip_sync -> managed-but-excluded (не тянем, не retire)
        h = self.mkfile("skills/redmine/SKILL.md", "x")
        desc = self.descriptor({"skills/redmine/SKILL.md": {"blob_sha": h, "mode": "100644"}},
                               {"skills/redmine/SKILL.md": ["universal"]})
        st = self.state({"skills/redmine/SKILL.md": {"sha": h, "mode": "100644"}})
        r = cd.classify(self.intent(skip=["skills/redmine/SKILL.md"]), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "skills/redmine/SKILL.md"), cd.MANAGED_BUT_EXCLUDED)

    def test_local_only_ignored(self) -> None:
        # local_only путь, которого нет в descriptor - не всплывает как removed/new
        self.mkfile("rules/mine.md", "local")
        desc = self.descriptor({}, {})
        st = self.state({})
        # local_only не в state и не в descriptor -> managed-but-excluded (числится исключением)
        r = cd.classify(self.intent(local=["rules/mine.md"]), st, desc, self.root)
        self.assertEqual(self.klass_of(r, "rules/mine.md"), cd.MANAGED_BUT_EXCLUDED)

    def test_type_set_membership(self) -> None:
        # путь coding не применим проекту wiki (и не в state) -> вообще не в результатах
        desc = self.descriptor({"rules/c.md": {"blob_sha": blob("x"), "mode": "100644"}},
                               {"rules/c.md": ["coding"]})
        r = cd.classify(self.intent(ptype=["wiki"]), self.state({}), desc, self.root)
        self.assertIsNone(self.klass_of(r, "rules/c.md"))

    # --- планировщик + release-wide gate ---

    def test_plan_release_ready_when_no_conflicts(self) -> None:
        # outdated + new, без escalate -> release_ready
        self.mkfile("rules/a.md", "old")
        desc = self.descriptor(
            {"rules/a.md": {"blob_sha": blob("new"), "mode": "100644"},
             "rules/b.md": {"blob_sha": blob("bnew"), "mode": "100644"}},
            {"rules/a.md": ["universal"], "rules/b.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": blob("old"), "mode": "100644"}})
        plan = cd.plan_actions(cd.classify(self.intent(), st, desc, self.root))
        self.assertTrue(plan["release_ready"])
        self.assertEqual(len(plan["apply"]), 2)  # a=outdated, b=new
        self.assertEqual(len(plan["escalate"]), 0)

    def test_plan_blocked_by_conflict(self) -> None:
        self.mkfile("rules/a.md", "локальная правка")
        desc = self.descriptor({"rules/a.md": {"blob_sha": blob("upstream"), "mode": "100644"}},
                               {"rules/a.md": ["universal"]})
        st = self.state({"rules/a.md": {"sha": blob("base"), "mode": "100644"}})
        plan = cd.plan_actions(cd.classify(self.intent(), st, desc, self.root))
        self.assertFalse(plan["release_ready"])
        self.assertEqual(len(plan["escalate"]), 1)

    def test_plan_retire_does_not_block_release(self) -> None:
        # retired-from-scope не блокирует release-gate (не escalate)
        desc = self.descriptor({"rules/c.md": {"blob_sha": blob("x"), "mode": "100644"}},
                               {"rules/c.md": ["coding"]})
        st = self.state({"rules/c.md": {"sha": blob("x"), "mode": "100644"}})
        plan = cd.plan_actions(cd.classify(self.intent(ptype=["wiki"]), st, desc, self.root))
        self.assertTrue(plan["release_ready"])
        self.assertEqual(len(plan["retire"]), 1)

    # --- fast-path ---

    def test_fast_path_true(self) -> None:
        h = self.mkfile("rules/a.md", "body")
        st = self.state({"rules/a.md": {"sha": h, "mode": "100644"}})
        st["applied_release"] = {"commit_sha": "abc123", "manifest_digest": "d"}
        self.assertTrue(cd.fast_path(st, "abc123", self.root))

    def test_fast_path_false_local_drift(self) -> None:
        self.mkfile("rules/a.md", "ИЗМЕНЕНО локально")
        st = self.state({"rules/a.md": {"sha": blob("body"), "mode": "100644"}})
        st["applied_release"] = {"commit_sha": "abc123", "manifest_digest": "d"}
        self.assertFalse(cd.fast_path(st, "abc123", self.root))

    def test_fast_path_false_wrong_commit(self) -> None:
        h = self.mkfile("rules/a.md", "body")
        st = self.state({"rules/a.md": {"sha": h, "mode": "100644"}})
        st["applied_release"] = {"commit_sha": "OLD", "manifest_digest": "d"}
        self.assertFalse(cd.fast_path(st, "NEW", self.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
