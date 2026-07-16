#!/usr/bin/env python3
"""Fault-injection тесты транзакционного WAL canon-delta.py (этап 8, §6/§10).

Запуск: python3 tests/test_wal.py
Грузит canon-delta.py через importlib. Проверяет crash-матрицу: kill процесса
в каждой точке протокола (после материализации стейджа до flip; после flip до
rename; между rename файлов; после rename до state.json) -> recovery приводит
систему в консистентное состояние без потери/затирания данных. Плюс CAS/TOCTOU,
prepare-recovery (roll-back), идемпотентность recovery, backup-namespace по pass-id.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "canon-delta.py"
_spec = importlib.util.spec_from_file_location("canon_delta", _MOD_PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)


def blob(content: str) -> str:
    return cd.git_blob_sha(content.encode("utf-8"))


def fault_at(target: str):
    def f(name: str) -> None:
        if name == target:
            raise cd.CrashSim(name)
    return f


class WalBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir(parents=True)
        self.state_path = self.root / ".claude" / "canon.state.json"

        # descriptor c2: keep=uptodate, mod=outdated, new=new, gone=missing-local
        self.descriptor = {
            "schema_version": 1,
            "manifest_digest": "digest-c2",
            "files": {
                "rules/keep.md": {"blob_sha": blob("keep\n"), "mode": "100644"},
                "rules/mod.md": {"blob_sha": blob("new-mod\n"), "mode": "100644"},
                "rules/new.md": {"blob_sha": blob("new-file\n"), "mode": "100644"},
                "rules/gone.md": {"blob_sha": blob("gone-up\n"), "mode": "100644"},
            },
            "membership": {p: ["universal"] for p in (
                "rules/keep.md", "rules/mod.md", "rules/new.md", "rules/gone.md")},
            "min_cli_version": 1,
            "plugin_source": None,
        }
        self.blob_source = cd.DictBlobSource({
            blob("keep\n"): b"keep\n",
            blob("new-mod\n"): b"new-mod\n",
            blob("new-file\n"): b"new-file\n",
            blob("gone-up\n"): b"gone-up\n",
            blob("old-mod\n"): b"old-mod\n",
        })
        self.intent = {"project_type": [], "track": "stable",
                       "skip_sync": [], "local_only": [], "overrides": []}
        self.state = cd.empty_state()
        self.state["applied_release"] = {"commit_sha": "c1", "manifest_digest": "digest-c1"}
        self.state["rollout_record"] = ["c1"]
        self.state["file_hashes"] = {
            "rules/keep.md": {"sha": blob("keep\n"), "mode": "100644"},
            "rules/mod.md": {"sha": blob("old-mod\n"), "mode": "100644"},
            "rules/gone.md": {"sha": blob("gone-base\n"), "mode": "100644"},
        }
        self.state["membership"] = {
            "rules/keep.md": ["universal"],
            "rules/mod.md": ["universal"],
            "rules/gone.md": ["universal"],
        }
        # диск: keep + mod присутствуют (== base), new + gone отсутствуют
        self._write("rules/keep.md", "keep\n")
        self._write("rules/mod.md", "old-mod\n")
        cd.save_state(self.state_path, self.state)
        self.target = "c2"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rel: str, content: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def _read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def _exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def _load_state(self) -> dict:
        return cd.load_state(self.state_path)

    def _plan_journal(self, scope: str, pass_id: str = "P1") -> dict:
        results = cd.classify(self.intent, self.state, self.descriptor, self.root)
        plan = cd.plan_actions(results)
        target_release = {"commit_sha": self.target, "manifest_digest": self.descriptor["manifest_digest"]}
        return cd.build_journal(plan["apply"], self.state, target_release, scope, pass_id, self.descriptor)

    def _assert_all_applied(self) -> None:
        self.assertEqual(self._read("rules/mod.md"), "new-mod\n")
        self.assertEqual(self._read("rules/new.md"), "new-file\n")
        self.assertEqual(self._read("rules/gone.md"), "gone-up\n")

    def _assert_untouched(self) -> None:
        self.assertEqual(self._read("rules/mod.md"), "old-mod\n")
        self.assertFalse(self._exists("rules/new.md"))
        self.assertFalse(self._exists("rules/gone.md"))

    def _assert_release_moved(self) -> None:
        st = self._load_state()
        self.assertEqual(st["applied_release"]["commit_sha"], "c2")
        self.assertEqual(st["file_hashes"]["rules/mod.md"]["sha"], blob("new-mod\n"))
        self.assertEqual(st["file_hashes"]["rules/new.md"]["sha"], blob("new-file\n"))
        self.assertEqual(st["file_hashes"]["rules/gone.md"]["sha"], blob("gone-up\n"))
        self.assertEqual(st["rollout_record"][-1], "c2")

    def _no_wal(self) -> None:
        self.assertIsNone(cd.read_journal(self.root))
        self.assertFalse((self.root / ".claude" / cd.STAGE_DIR / "P1").exists())
        self.assertFalse((self.root / ".claude" / cd.BAK_DIR / "P1").exists())


class HappyPathTest(WalBase):
    def test_apply_release_full(self) -> None:
        res = cd.apply_release(self.root, self.intent, self.state, self.descriptor,
                               self.target, self.blob_source, self.state_path)
        self.assertEqual(res["scope"], cd.SCOPE_RELEASE)
        self.assertEqual(res["aborted"], [])
        self._assert_all_applied()
        self.assertEqual(self._read("rules/keep.md"), "keep\n")  # up-to-date не трогали
        self._assert_release_moved()
        self.assertIsNone(cd.read_journal(self.root))

    def test_second_pass_is_fast_path(self) -> None:
        cd.apply_release(self.root, self.intent, self.state, self.descriptor,
                         self.target, self.blob_source, self.state_path)
        st = self._load_state()
        # уже на target, дерево совпадает -> fast-path True (0 работы)
        self.assertTrue(cd.fast_path(st, self.target, self.root))


class CrashMatrixTest(WalBase):
    def test_crash_in_prepare_staged_rolls_back(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        with self.assertRaises(cd.CrashSim):
            cd.prepare(self.root, journal, self.blob_source, fault=fault_at("staged"))
        # журнал phase=prepare, ни один финальный rename не сделан
        self.assertEqual(cd.read_journal(self.root)["header"]["phase"], cd.PHASE_PREPARE)
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self._assert_untouched()
        self.assertEqual(self._load_state()["applied_release"]["commit_sha"], "c1")
        self._no_wal()

    def test_crash_in_prepare_preflip_rolls_back(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        with self.assertRaises(cd.CrashSim):
            cd.prepare(self.root, journal, self.blob_source, fault=fault_at("pre-flip"))
        self.assertEqual(cd.read_journal(self.root)["header"]["phase"], cd.PHASE_PREPARE)
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self._assert_untouched()
        self._no_wal()

    def test_crash_after_flip_rolls_forward(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        committed = cd.prepare(self.root, journal, self.blob_source)  # phase=committed
        # crash сразу после flip: recover без commit
        self.assertEqual(committed["header"]["phase"], cd.PHASE_COMMITTED)
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-forward")
        self._assert_all_applied()
        self._assert_release_moved()
        self._no_wal()

    def test_crash_mid_rename_rolls_forward(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        committed = cd.prepare(self.root, journal, self.blob_source)
        with self.assertRaises(cd.CrashSim):
            cd.commit(self.root, committed, self.state, self.state_path,
                      self.blob_source, fault=fault_at("post-rename:rules/mod.md"))
        # state.json не сохранен (crash до pre-state)
        self.assertEqual(self._load_state()["applied_release"]["commit_sha"], "c1")
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-forward")
        self._assert_all_applied()
        self._assert_release_moved()
        self._no_wal()

    def test_crash_pre_state_rolls_forward(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        committed = cd.prepare(self.root, journal, self.blob_source)
        with self.assertRaises(cd.CrashSim):
            cd.commit(self.root, committed, self.state, self.state_path,
                      self.blob_source, fault=fault_at("pre-state"))
        # все файлы на диске применены, но state.json еще старый
        self._assert_all_applied()
        self.assertEqual(self._load_state()["applied_release"]["commit_sha"], "c1")
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-forward")
        self._assert_release_moved()
        self._no_wal()

    def test_recovery_idempotent(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)
        first = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(first["status"], "rolled-forward")
        second = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(second["status"], "clean")  # журнал уже очищен
        self._assert_all_applied()
        self._assert_release_moved()


class CasToctouTest(WalBase):
    def test_toctou_edit_aborts_path_no_clobber(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        committed = cd.prepare(self.root, journal, self.blob_source)
        # человек правит mod.md в окне между prepare и commit
        self._write("rules/mod.md", "user-edit\n")
        new_state, aborted = cd.commit(self.root, committed, self.state,
                                       self.state_path, self.blob_source)
        aborted_paths = [e["path"] for e in aborted]
        self.assertIn("rules/mod.md", aborted_paths)
        # правку не затерли
        self.assertEqual(self._read("rules/mod.md"), "user-edit\n")
        # прочие применены
        self.assertEqual(self._read("rules/new.md"), "new-file\n")
        self.assertEqual(self._read("rules/gone.md"), "gone-up\n")
        st = self._load_state()
        # release-указатель НЕ сдвинут (есть aborted), mod base не обновлен
        self.assertEqual(st["applied_release"]["commit_sha"], "c1")
        self.assertEqual(st["file_hashes"]["rules/mod.md"]["sha"], blob("old-mod\n"))
        self.assertEqual(st["file_hashes"]["rules/new.md"]["sha"], blob("new-file\n"))


class PrepareRecoveryDefensiveTest(WalBase):
    """Оборонительные ветки _recover_prepare (§6): частичное состояние при phase=prepare."""

    def test_created_partial_is_removed(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)  # phase=committed, стейдж есть
        # искусственно вернуть phase=prepare и положить created-файл == new (частичный)
        journal["header"]["phase"] = cd.PHASE_PREPARE
        cd.write_journal(self.root, journal)
        self._write("rules/new.md", "new-file\n")  # как будто создали, потом crash
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self.assertFalse(self._exists("rules/new.md"))  # created откатан удалением
        self.assertEqual(res["escalated"], [])

    def test_modified_partial_restored_from_backup(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)  # создал backup mod.md=old
        journal["header"]["phase"] = cd.PHASE_PREPARE
        cd.write_journal(self.root, journal)
        self._write("rules/mod.md", "new-mod\n")  # частично применился до crash
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self.assertEqual(self._read("rules/mod.md"), "old-mod\n")  # откат из backup

    def test_foreign_edit_not_touched_escalated(self) -> None:
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)
        journal["header"]["phase"] = cd.PHASE_PREPARE
        cd.write_journal(self.root, journal)
        self._write("rules/mod.md", "foreign\n")  # ни base, ни new -> чужая правка
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self.assertEqual(self._read("rules/mod.md"), "foreign\n")  # не тронут
        self.assertIn("rules/mod.md", res["escalated"])


class BackupNamespaceTest(WalBase):
    def test_backup_path_namespaced_by_pass_id(self) -> None:
        j1 = self._plan_journal(cd.SCOPE_RELEASE, pass_id="PASS-A")
        mod1 = next(e for e in j1["files"] if e["path"] == "rules/mod.md")
        self.assertEqual(mod1["backup_path"], ".claude/.canon-bak/PASS-A/rules/mod.md")
        j2 = self._plan_journal(cd.SCOPE_RELEASE, pass_id="PASS-B")
        mod2 = next(e for e in j2["files"] if e["path"] == "rules/mod.md")
        # разные pass-id -> разные namespace бэкапов (не сталкиваются при повторном apply)
        self.assertNotEqual(mod1["backup_path"], mod2["backup_path"])

    def test_mode_bit_preserved_on_apply(self) -> None:
        # добавить исполняемый канон-файл, проверить +x после материализации
        self.descriptor["files"]["scripts/x.sh"] = {"blob_sha": blob("#!/bin/sh\n"), "mode": "100755"}
        self.descriptor["membership"]["scripts/x.sh"] = ["universal"]
        self.blob_source._m[blob("#!/bin/sh\n")] = b"#!/bin/sh\n"
        cd.apply_release(self.root, self.intent, self.state, self.descriptor,
                         self.target, self.blob_source, self.state_path)
        local = cd.read_local(self.root, "scripts/x.sh")
        self.assertEqual(local["mode"], "100755")
        self.assertEqual(self._load_state()["file_hashes"]["scripts/x.sh"]["mode"], "100755")


class AdversarialRegressionTest(WalBase):
    """Регресс-тесты на находки codex-adversarial (NO-GO v1 WAL). Каждый закрывает
    конкретный крэш-сценарий/гонку: mode-identity в recovery, no-clobber, backup-guard,
    recover-first под единым flock."""

    def _flip_to_prepare(self) -> None:
        j = cd.read_journal(self.root)
        j["header"]["phase"] = cd.PHASE_PREPARE
        cd.write_journal(self.root, j)

    def _add_exec_modify(self):
        # exec-файл (100755) как modify: base=old-x(755), upstream=new-x(755)
        self.descriptor["files"]["scripts/x.sh"] = {"blob_sha": blob("new-x\n"), "mode": "100755"}
        self.descriptor["membership"]["scripts/x.sh"] = ["universal"]
        self.blob_source._m[blob("new-x\n")] = b"new-x\n"
        self.state["file_hashes"]["scripts/x.sh"] = {"sha": blob("old-x\n"), "mode": "100755"}
        p = self.root / "scripts" / "x.sh"
        p.parent.mkdir(exist_ok=True)
        p.write_text("old-x\n", encoding="utf-8")
        p.chmod(0o755)
        cd.save_state(self.state_path, self.state)
        return p

    def test_f1_modify_exec_restore_preserves_mode(self) -> None:
        # post-image по ПОЛНОЙ identity (new-x, 755) -> откат из backup обязан вернуть
        # И байты old-x, И mode 755 (атомарно, одной atomic_write_bytes). Находка 1.
        p = self._add_exec_modify()
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)
        self._flip_to_prepare()
        p.write_text("new-x\n", encoding="utf-8")
        p.chmod(0o755)  # полный new-identity (new-x, 755)
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self.assertEqual(p.read_text(), "old-x\n")   # байты откачены
        self.assertTrue(os.access(p, os.X_OK))       # mode 755 восстановлен

    def test_f1b_new_bytes_wrong_mode_escalates_not_clobbered(self) -> None:
        # файл с байтами new но mode 644 (entry ждет 755) - НЕ наш post-image по паре
        # (sha,mode) -> эскалация, НЕ откат/затирание. Находка 1 (identity-полнота).
        p = self._add_exec_modify()
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)
        self._flip_to_prepare()
        p.write_text("new-x\n", encoding="utf-8")
        p.chmod(0o644)  # байты new, mode чужой
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self.assertIn("scripts/x.sh", res["escalated"])
        self.assertEqual(p.read_text(), "new-x\n")  # не тронут

    def test_f2_foreign_create_different_mode_not_deleted(self) -> None:
        # phase=prepare, чужой файл с НАШИМИ байтами но mode 755 (мы ставили 644) ->
        # это НЕ наш created, удалять нельзя. Находка 2.
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)
        self._flip_to_prepare()
        p = self.root / "rules" / "new.md"
        p.write_text("new-file\n", encoding="utf-8")
        p.chmod(0o755)
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self.assertIn("rules/new.md", res["escalated"])
        self.assertTrue(p.exists())  # чужой файл НЕ удален

    def test_f5_no_clobber_primitive(self) -> None:
        stage = self.root / "s"
        stage.write_text("stage", encoding="utf-8")
        final = self.root / "f"
        final.write_text("existing", encoding="utf-8")
        self.assertFalse(cd._place_no_clobber(stage, final))  # link падает, файл существует
        self.assertEqual(final.read_text(), "existing")       # не затерт

    def test_f5_create_foreign_file_aborts_no_clobber(self) -> None:
        # foreign создал new.md ДО commit -> CAS(create=absent) валится -> abort, не clobber
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        committed = cd.prepare(self.root, journal, self.blob_source)
        (self.root / "rules" / "new.md").write_text("foreign\n", encoding="utf-8")
        _, aborted = cd.commit(self.root, committed, self.state, self.state_path, self.blob_source)
        self.assertIn("rules/new.md", [e["path"] for e in aborted])
        self.assertEqual(self._read("rules/new.md"), "foreign\n")  # не затерт

    def test_f6_prepare_recovery_missing_backup_escalates(self) -> None:
        # backup исчез (crash до snapshot_backup), foreign сделал файл == new ->
        # recovery НЕ падает на чтении backup, а эскалирует (no-touch). Находка 6.
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)
        (self.root / ".claude" / ".canon-bak" / "P1" / "rules" / "mod.md").unlink()
        self._flip_to_prepare()
        self._write("rules/mod.md", "new-mod\n")
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-back")
        self.assertIn("rules/mod.md", res["escalated"])
        self.assertEqual(self._read("rules/mod.md"), "new-mod\n")  # не тронут, без crash

    def test_retire_wal_ized_survives_committed_recovery(self) -> None:
        # retire-путь снимается из state АТОМАРНО с commit (в journal.header) -> крэш
        # после flip доигрывается recovery, retire не теряется (новый blocker).
        self.descriptor["files"]["rules/coding.md"] = {"blob_sha": blob("c\n"), "mode": "100644"}
        self.descriptor["membership"]["rules/coding.md"] = ["coding"]  # не universal
        self.state["file_hashes"]["rules/coding.md"] = {"sha": blob("c\n"), "mode": "100644"}
        cd.save_state(self.state_path, self.state)
        results = cd.classify(self.intent, self.state, self.descriptor, self.root)
        plan = cd.plan_actions(results)
        self.assertIn("rules/coding.md", [r["path"] for r in plan["retire"]])
        tr = {"commit_sha": self.target, "manifest_digest": self.descriptor["manifest_digest"]}
        journal = cd.build_journal(plan["apply"], self.state, tr, cd.SCOPE_RELEASE,
                                   "P1", self.descriptor, ["rules/coding.md"])
        cd.prepare(self.root, journal, self.blob_source)  # committed, commit не сделан
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-forward")
        st = self._load_state()
        self.assertNotIn("rules/coding.md", st["file_hashes"])  # retire применен
        self.assertTrue(any(r["path"] == "rules/coding.md" for r in st["retirement_records"]))
        self._assert_all_applied()

    def test_uptodate_stale_base_reconciled(self) -> None:
        # up-to-date путь (local==upstream) со stale base в state -> apply_release
        # освежает file_hashes, иначе fast-path навсегда false (находка 7, вектор 4).
        self.state["file_hashes"]["rules/keep.md"] = {"sha": blob("STALE\n"), "mode": "100644"}
        cd.save_state(self.state_path, self.state)
        cd.apply_release(self.root, self.intent, self.state, self.descriptor,
                         self.target, self.blob_source, self.state_path)
        st = self._load_state()
        self.assertEqual(st["file_hashes"]["rules/keep.md"]["sha"], blob("keep\n"))  # base освежен
        self.assertTrue(cd.fast_path(st, self.target, self.root))  # fast-path заработал

    def test_decision_record_idempotent_under_replay(self) -> None:
        # _finalize_state под повторным recovery не дублирует decision (находка 8-risk)
        decision = {"path": "x", "outcome": "accept-upstream", "release": "c2", "upstream_sha": "s"}
        journal = {"header": {"scope": cd.SCOPE_PER_PATH, "decision": decision}}
        st1 = cd._finalize_state(cd.empty_state(), journal, [], [])
        st2 = cd._finalize_state(st1, journal, [], [])
        self.assertEqual(len(st2["decision_records"]), 1)

    def test_committed_recovery_abort_records_durable_conflict(self) -> None:
        # committed WAL, путь foreign-правлен после rename -> recovery aborts, но НЕ
        # теряет намерение: durable recovery_conflict + WAL очищен терминально (§10).
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        committed = cd.prepare(self.root, journal, self.blob_source)
        with self.assertRaises(cd.CrashSim):
            cd.commit(self.root, committed, self.state, self.state_path,
                      self.blob_source, fault=fault_at("post-rename:rules/mod.md"))
        self._write("rules/mod.md", "foreign\n")  # чужая правка уже примененного файла
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "rolled-forward-conflict")
        st = self._load_state()
        conflicts = [c["path"] for c in st["recovery_conflicts"]]
        self.assertIn("rules/mod.md", conflicts)           # намерение сохранено durable
        self.assertEqual(self._read("rules/mod.md"), "foreign\n")  # правка не затерта
        self.assertEqual(st["applied_release"]["commit_sha"], "c1")  # релиз не двинут
        self._no_wal()                                     # WAL терминально очищен

    def test_f5_exdev_fallback_temp_link_publishes_whole(self) -> None:
        # ФС без hardlink stage->final (EXDEV): fallback temp+link публикует final
        # ЦЕЛИКОМ (не частично, не O_EXCL-пустой). Находка O_EXCL-blocker.
        import unittest.mock as mock
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        committed = cd.prepare(self.root, journal, self.blob_source)
        real_link = os.link

        def fake_link(src, dst, **kw):
            if ".canon-stage" in str(src):   # только stage->final эмулируем как EXDEV
                raise OSError(18, "EXDEV")
            return real_link(src, dst)

        with mock.patch.object(cd.os, "link", side_effect=fake_link):
            _, aborted = cd.commit(self.root, committed, self.state, self.state_path, self.blob_source)
        self.assertEqual(aborted, [])
        self.assertEqual(self._read("rules/new.md"), "new-file\n")   # create через fallback
        self.assertEqual(self._read("rules/gone.md"), "gone-up\n")   # restore через fallback
        self._assert_release_moved()

    def test_f3_apply_release_terminalizes_live_committed_wal(self) -> None:
        # живой committed-WAL от прерванного sync: новый apply_release ОБЯЗАН сперва
        # его добить (recover-first под единым flock), а не затереть. Находки 3,4.
        journal = self._plan_journal(cd.SCOPE_RELEASE)
        cd.prepare(self.root, journal, self.blob_source)  # committed, но commit не сделан
        res = cd.apply_release(self.root, self.intent, self.state, self.descriptor,
                               self.target, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "up-to-date")  # хвост доигран, потом no-op
        self._assert_all_applied()
        self._assert_release_moved()
        self._no_wal()


class ResolveTest(unittest.TestCase):
    """resolution-API: accept-upstream (per-path WAL) + keep-local (record) + R6."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir(parents=True)
        self.state_path = self.root / ".claude" / "canon.state.json"
        # conf.md: base=v1, локально правлен, upstream ушел на v2 -> conflict
        self.descriptor = {
            "schema_version": 1, "manifest_digest": "d2",
            "files": {"rules/conf.md": {"blob_sha": blob("upstream-v2\n"), "mode": "100644"}},
            "membership": {"rules/conf.md": ["universal"]},
            "min_cli_version": 1, "plugin_source": None,
        }
        self.blob_source = cd.DictBlobSource({
            blob("upstream-v2\n"): b"upstream-v2\n",
            blob("upstream-v3\n"): b"upstream-v3\n",
        })
        self.intent = {"project_type": [], "track": "stable",
                       "skip_sync": [], "local_only": [], "overrides": []}
        self.state = cd.empty_state()
        self.state["applied_release"] = {"commit_sha": "c1", "manifest_digest": "d1"}
        self.state["file_hashes"] = {"rules/conf.md": {"sha": blob("base-v1\n"), "mode": "100644"}}
        self.state["membership"] = {"rules/conf.md": ["universal"]}
        (self.root / "rules").mkdir()
        (self.root / "rules" / "conf.md").write_text("local-edit\n", encoding="utf-8")
        cd.save_state(self.state_path, self.state)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _klass(self, state) -> str:
        res = cd.classify(self.intent, state, self.descriptor, self.root)
        return next(r["klass"] for r in res if r["path"] == "rules/conf.md")

    def test_baseline_is_conflict(self) -> None:
        self.assertEqual(self._klass(self.state), cd.CONFLICT)

    def test_keep_local_then_resolved_then_r6(self) -> None:
        st = cd.resolve_keep_local(self.root, self.descriptor, "rules/conf.md", "c2", self.state_path)
        # запись есть, файл не тронут
        self.assertEqual((self.root / "rules" / "conf.md").read_text(), "local-edit\n")
        self.assertEqual(self._klass(st), cd.RESOLVED_LOCAL)
        # канон двигает путь дальше (v2 -> v3): record больше не матчит (R6)
        self.descriptor["files"]["rules/conf.md"]["blob_sha"] = blob("upstream-v3\n")
        self.assertEqual(self._klass(st), cd.CONFLICT)

    def test_accept_upstream_per_path(self) -> None:
        res = cd.resolve_accept_upstream(self.root, self.descriptor,
                                         "rules/conf.md", "c2", self.blob_source, self.state_path)
        self.assertEqual(res["aborted"], [])
        # файл перезаписан upstream-байтами
        self.assertEqual((self.root / "rules" / "conf.md").read_text(), "upstream-v2\n")
        st = cd.load_state(self.state_path)
        self.assertEqual(st["file_hashes"]["rules/conf.md"]["sha"], blob("upstream-v2\n"))
        # per-path: applied_release НЕ двинут
        self.assertEqual(st["applied_release"]["commit_sha"], "c1")
        self.assertEqual(st["decision_records"][-1]["outcome"], "accept-upstream")
        # после accept путь = up-to-date
        self.assertEqual(self._klass(st), cd.UP_TO_DATE)
        # WAL очищен
        self.assertIsNone(cd.read_journal(self.root))


class LeftoversTest(WalBase):
    """Остаточные не-блокеры codex-r4 части (a), закрываются в части (c) T27:
    GC осиротевших temp + lifecycle recovery_conflicts."""

    def _mk_temp(self, rel_dir: str, base: str, age_s: float) -> Path:
        d = self.root / rel_dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f".{base}.tmp.{'a' * 32}"
        p.write_text("orphan", encoding="utf-8")
        old = time.time() - age_s
        os.utime(p, (old, old))
        return p

    def test_orphan_temps_gc_on_recover(self) -> None:
        stale_claude = self._mk_temp(".claude", "canon.state.json", 7200)
        stale_rule = self._mk_temp("rules", "mod.md", 7200)
        fresh = self._mk_temp(".claude", "canon.intent.yaml", 10)
        alien = self.root / "rules" / ".unrelated-dotfile"
        alien.write_text("keep me", encoding="utf-8")
        res = cd.recover(self.root, self.blob_source, self.state_path)
        self.assertEqual(res["status"], "clean")
        self.assertFalse(stale_claude.exists(), "сирота в .claude не убрана")
        self.assertFalse(stale_rule.exists(), "сирота в каталоге правила не убрана")
        self.assertTrue(fresh.exists(), "свежий temp удален (живой писатель?)")
        self.assertTrue(alien.exists(), "чужой dot-файл удален GC")

    def test_orphan_temps_gc_on_apply(self) -> None:
        stale = self._mk_temp(".claude", "canon.state.json", 7200)
        cd.apply_release(self.root, self.intent, self.state, self.descriptor,
                         self.target, self.blob_source, self.state_path)
        self.assertFalse(stale.exists(), "успешный apply не почистил сироту")

    def test_recovery_conflict_cleared_on_clean_replay(self) -> None:
        self.state["recovery_conflicts"] = [
            {"path": "rules/mod.md", "release": {"commit_sha": "c1"},
             "reason": "committed-recovery-abort"},
            {"path": "rules/other.md", "release": {"commit_sha": "c1"},
             "reason": "committed-recovery-abort"},
        ]
        cd.save_state(self.state_path, self.state)
        res = cd.apply_release(self.root, self.intent, self.state, self.descriptor,
                               self.target, self.blob_source, self.state_path)
        self.assertEqual(res["aborted"], [])
        st = self._load_state()
        paths = [x["path"] for x in st.get("recovery_conflicts", [])]
        self.assertNotIn("rules/mod.md", paths,
                         "чистый re-apply пути не снял recovery_conflict")
        self.assertIn("rules/other.md", paths,
                      "запись НЕприменявшегося пути снята огульно")


if __name__ == "__main__":
    unittest.main(verbosity=2)
