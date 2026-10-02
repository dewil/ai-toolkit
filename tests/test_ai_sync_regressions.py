"""Regressions for defects found during implementation; blind tests unchanged."""
import argparse
import base64
import hashlib
import json
import runpy
import shutil
import subprocess
import sys
import unittest

from test_ai_sync import SCRIPT, SyncFixture, tree
from test_ai_sync_recovery import WRAPPER


class AiSyncRegressions(SyncFixture):
    def coherent_source_overwrite_journal(self, path, current):
        # A coherent future generation does not establish ownership of an
        # existing file. Rehash every dependent receipt so refusal must come
        # from ownership validation, rather than a stale digest/context.
        module = runpy.run_path(str(SCRIPT), run_name='sync_ownership_regression')
        ab = module['ab']
        target = ab.destination(path)
        self.put(self.root / target, current)
        self.upstream('rules/coding.md', b'# Reviewed coding update\n')
        journal = module['proposal'](argparse.Namespace(bundle=self.bundle, source_base=None), self.root)
        pending = {action['path']: action for action in journal['actions']}
        state = json.loads(base64.b64decode(pending[ab.STATE]['data']))
        payload = b'# FORGED_OVERWRITE_UNKNOWN_OWNER\n'
        state['source_files'][path] = {'path': target, 'sha256': ab.sha(payload),
                                      'blob_sha': ab.blob(payload), 'mode': '100644'}
        sources = {}
        for canonical, receipt in state['source_files'].items():
            if canonical == path:
                sources[canonical] = payload
            elif receipt['path'] in pending:
                sources[canonical] = base64.b64decode(pending[receipt['path']]['data'])
            else:
                sources[canonical] = ab.read_file(self.root, receipt['path'])
        start_path = module['START']
        start = (base64.b64decode(pending[start_path]['data']) if start_path in pending
                 else ab.read_file(self.root, start_path))
        outputs, context = ab.context(ab.json_file(self.root, ab.PROJECT), sources, start,
                                      ab.json_file(self.root, ab.POLICY))
        state['context'] = context
        pending[target] = ab.file_action(target, payload, ab.descriptor(self.root, target))
        for output, body in outputs.items():
            pending[output] = ab.file_action(output, body, ab.descriptor(self.root, output))
        pending[ab.STATE] = ab.file_action(ab.STATE, ab.encoded(state), ab.descriptor(self.root, ab.STATE))
        journal['actions'] = sorted(pending.values(), key=lambda action:
                                    (action['after']['kind'] != 'dir', action['path'] == ab.STATE, action['path']))
        journal['inputs'].pop(target, None)
        journal['effective_sha256'] = {p: ab.sha(body) for p, body in sorted(sources.items())}
        journal['plan_sha256'] = module['digest']({k: v for k, v in journal.items() if k != 'plan_sha256'})
        return module, journal

    def test_recovery_rejects_rehashed_journal_claiming_existing_unknown_file(self):
        current = b'# User-owned existing content\n'
        module, journal = self.coherent_source_overwrite_journal('rules/unknown-existing.md', current)
        self.put(self.root / module['JOURNAL'], module['ab'].encoded(journal))
        before = tree(self.root)
        self.cli('recover', reject=True)
        self.assertEqual(tree(self.root), before)
        self.assertEqual(self.destination('rules/unknown-existing.md').read_bytes(), current)

    def test_recovery_rejects_coherent_overwrite_of_tracked_local_edit(self):
        path = 'rules/wiki.md'
        current = self.sources[path] + b'LEGITIMATE_OWNER_EDIT\n'
        module, journal = self.coherent_source_overwrite_journal(path, current)
        self.put(self.root / module['JOURNAL'], module['ab'].encoded(journal))
        before = tree(self.root)
        self.cli('recover', reject=True)
        self.assertEqual(tree(self.root), before)
        self.assertEqual(self.destination(path).read_bytes(), current)
        self.assert_base(path, self.sources[path])

    def test_recovery_requires_integer_versions_in_original_and_future_state(self):
        for side in ('original', 'future'):
            for key, value in (('schema_version', 2.0), ('layout_version', True), ('layout_version', 1.0)):
                with self.subTest(side=side, key=key, value=value):
                    fixture = SyncFixture()
                    fixture.setUp()
                    try:
                        module = runpy.run_path(str(SCRIPT), run_name='sync_state_version_regression')
                        ab = module['ab']
                        fixture.upstream('rules/coding.md', b'# Version test update\n')
                        journal = module['proposal'](argparse.Namespace(bundle=fixture.bundle, source_base=None), fixture.root)
                        action = journal['actions'][-1]
                        if side == 'original':
                            old = json.loads(base64.b64decode(journal['state_before']))
                            old[key] = value
                            body = ab.encoded(old)
                            fixture.state_path.write_bytes(body)
                            journal['state_before'] = base64.b64encode(body).decode()
                            action['before'] = ab.descriptor(fixture.root, ab.STATE)
                        else:
                            future = json.loads(base64.b64decode(action['data']))
                            future[key] = value
                            journal['actions'][-1] = ab.file_action(ab.STATE, ab.encoded(future), action['before'])
                        journal['plan_sha256'] = module['digest']({k: v for k, v in journal.items() if k != 'plan_sha256'})
                        fixture.put(fixture.root / module['JOURNAL'], ab.encoded(journal))
                        before = tree(fixture.root)
                        fixture.cli('recover', reject=True)
                        self.assertEqual(tree(fixture.root), before)
                    finally:
                        fixture.doCleanups()

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
