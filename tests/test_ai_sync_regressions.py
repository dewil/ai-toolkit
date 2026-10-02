"""Regressions for defects found during implementation; blind tests unchanged."""
import hashlib
import shutil
import subprocess
import sys
import unittest

from test_ai_sync import SCRIPT, SyncFixture, tree
from test_ai_sync_recovery import WRAPPER


class AiSyncRegressions(SyncFixture):
    def test_new_skill_installs_through_missing_parent_directory(self):
        path = 'skills/new-skill/SKILL.md'
        self.add_upstream(path, b'# New skill\nNEW_DIRECTORY_GENERATION\n')
        self.assertFalse(self.destination(path).parent.exists())
        self.apply()
        self.assertEqual(self.destination(path).read_bytes(), (self.bundle / path).read_bytes())
        self.assert_base(path, (self.bundle / path).read_bytes())
        self.cli('check')

    def test_new_skill_parent_and_file_recover_offline_after_interruption(self):
        path = 'skills/new-skill/nested/SKILL.md'
        self.add_upstream(path, b'# Nested skill\nOFFLINE_DIRECTORY_GENERATION\n')
        plan = self.plan()
        result = subprocess.run([sys.executable, '-c', WRAPPER, str(SCRIPT), str(self.root),
                                 str(self.bundle), plan['plan_sha256'],
                                 self.destination(path).relative_to(self.root).as_posix(), 'after', 'fail'],
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('FAULT_POINT', result.stdout)
        self.assertTrue((self.root / '.ai-bootstrap/sync.json').is_file())
        self.assertEqual(self.read_json(self.state_path), self.initial_state)
        self.assertIn(b'OFFLINE_DIRECTORY_GENERATION', self.destination(path).read_bytes())
        shutil.rmtree(self.bundle)
        self.cli('recover')
        self.cli('check')
        self.assertFalse((self.root / '.ai-bootstrap/sync.json').exists())
        before = tree(self.root)
        self.cli('recover')
        self.assertEqual(tree(self.root), before)

    def test_source_mode_change_invalidates_reviewed_bundle(self):
        self.upstream('rules/coding.md', b'REVIEWED_UPDATE\n')
        plan = self.plan()
        source = self.bundle / 'rules/wiki.md'
        source.chmod(0o755 if source.stat().st_mode & 0o777 != 0o755 else 0o644)
        self.reject_apply(plan['plan_sha256'])

    def test_published_migration_receipt_proves_original_start_prefix(self):
        import test_ai_migrate as migration
        fixture = migration.AiMigrationContract()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.command('apply')
        self.root, self.bundle, self.sources = fixture.root, fixture.bundle, fixture.sources
        self.state_path = self.root / '.AI/canon/canon.state.json'
        self.intent_path = self.root / '.AI/canon/canon.intent.yaml'
        state = self.read_json(self.state_path)
        # Restore the genuine old published receipt from the pinned fixture template.
        state['bootstrap_files']['.AI/START.md'] = hashlib.sha256((self.bundle / 'templates/ai/START.md').read_bytes()).hexdigest()
        import json
        self.state_path.write_text(json.dumps(state))
        start = self.root / '.AI/START.md'
        original = start.read_bytes()
        self.plan()
        for altered in (b'MANUAL_PREFIX\n' + original, original.replace(b'Local project sources', b'OWNER CATALOGUE')):
            start.write_bytes(altered)
            self.cli('plan', reject=True)
        start.write_bytes(original)
        self.apply()
        self.cli('check')

    def test_migrate_start_receipt_is_for_installed_catalogue(self):
        import test_ai_migrate as migration
        fixture = migration.AiMigrationContract()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.command('apply')
        state = self.read_json(fixture.root / '.AI/canon/canon.state.json')
        installed = (fixture.root / '.AI/START.md').read_bytes()
        self.assertEqual(state['bootstrap_files']['.AI/START.md'], hashlib.sha256(installed).hexdigest())
        fixture.command('check')


if __name__ == '__main__':
    unittest.main()
