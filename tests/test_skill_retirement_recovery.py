"""FR-SKR06 supplemental crash/tamper regressions at the existing public mutation seam."""
import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from test_ai_sync import SCRIPT, SyncFixture, tree
from test_ai_sync_recovery import WRAPPER

SOURCE = 'skills/sample/SKILL.md'
RETIRE_WRAPPER = WRAPPER.replace("'--expect-plan', digest]", "'--expect-plan', digest, '--retire-source', 'skills/sample/SKILL.md']")

class SkillRetirementRecovery(SyncFixture):
    def prepare(self):
        self.original = self.destination(SOURCE).read_bytes()
        self.original_start = (self.root / '.AI/START.md').read_bytes()
        self.archive = '.ai-bootstrap/retired/' + hashlib.sha256(self.original).hexdigest() + '/' + SOURCE
        self.remove_upstream(SOURCE)
        self.plan_ = json.loads(self.cli('plan', ['--retire-source', SOURCE]).stdout)

    def interrupt_at(self, point):
        p = subprocess.run([sys.executable, '-c', RETIRE_WRAPPER, str(SCRIPT), str(self.root),
                            str(self.bundle), self.plan_['plan_sha256'], point, 'after', 'fail'],
                           capture_output=True, text=True, timeout=20)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('FAULT_POINT', p.stdout, p.stderr)
        self.journal = self.root / '.ai-bootstrap/sync.json'
        self.assertEqual(json.loads(self.journal.read_text())['schema_version'], 2)

    def recovered(self):
        shutil.rmtree(self.bundle)
        self.cli('recover')
        self.cli('check')
        self.assertFalse(self.destination(SOURCE).exists())
        self.assertEqual((self.root / self.archive).read_bytes(), self.original)
        before = tree(self.root)
        self.cli('recover')
        self.assertEqual(before, tree(self.root))

    def test_recover_after_backup_before_deletion(self):
        self.prepare(); self.interrupt_at(self.archive)
        self.assertTrue(self.destination(SOURCE).exists())
        self.assertEqual((self.root / ".AI/START.md").read_bytes(), self.original_start)
        self.recovered()

    def test_recover_after_deletion_before_state(self):
        self.prepare(); self.interrupt_at('.AI/' + SOURCE)
        self.assertIn(SOURCE, self.read_json(self.state_path)['source_files'])
        self.recovered()

    def test_recover_after_state_before_journal_removal(self):
        self.prepare(); self.interrupt_at('.AI/canon/canon.state.json')
        self.recovered()

    def test_recover_after_start_and_output(self):
        # Each case gets a new fixture to exercise distinct replay points.
        for point in ('.AI/START.md', 'AGENTS.md'):
            with self.subTest(point=point):
                case = SkillRetirementRecovery()
                case.setUp()
                try:
                    case.prepare(); case.interrupt_at(point); case.recovered()
                finally:
                    case.doCleanups()

    def test_existing_wrong_backup_and_already_retired_drift_refuse(self):
        self.prepare()
        backup = self.root / self.archive
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b'wrong')
        before = tree(self.root)
        self.cli('plan', ['--retire-source', SOURCE], reject=True)
        self.assertEqual(tree(self.root), before)
        backup.write_bytes(self.original)
        backup.chmod(0o644)  # Exact archived descriptor, independent of host umask.
        plan = json.loads(self.cli('plan', ['--retire-source', SOURCE]).stdout)
        self.cli('apply', ['--retire-source', SOURCE, '--expect-plan', plan['plan_sha256']])
        backup.write_bytes(b'post-retirement drift')
        before = tree(self.root)
        self.cli('plan', ['--retire-source', SOURCE], reject=True)
        self.assertEqual(tree(self.root), before)

    def test_backup_drift_blocks_without_writes(self):
        self.prepare(); self.interrupt_at('.AI/' + SOURCE)
        (self.root / self.archive).write_bytes(b'changed backup')
        before = tree(self.root)
        self.cli('recover', reject=True)
        self.assertEqual(before, tree(self.root))

    def test_recreated_source_is_not_overwritten(self):
        self.prepare(); self.interrupt_at('.AI/' + SOURCE)
        self.destination(SOURCE).write_bytes(b'user replacement')
        before = tree(self.root)
        self.cli('recover', reject=True)
        self.assertEqual(before, tree(self.root))

    def test_rehashed_journal_cannot_delete_private_file(self):
        self.prepare(); self.interrupt_at(self.archive)
        j = json.loads(self.journal.read_text())
        for action in j['actions']:
            if action['after'] is None:
                action['path'] = '.AI/memory/MEMORY.md'
        unsigned = {k:v for k,v in j.items() if k != 'plan_sha256'}
        j['plan_sha256'] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, ensure_ascii=False,
                                                   separators=(',', ':')).encode()).hexdigest()
        self.journal.write_text(json.dumps(j))
        before = tree(self.root)
        self.cli('recover', reject=True)
        self.assertEqual(before, tree(self.root))

if __name__ == '__main__':
    unittest.main()
