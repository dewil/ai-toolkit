"""Independent migration preservation and recovery regressions."""
import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import subprocess
import unittest

from test_ai_bootstrap import PROJECT_ID, snapshot
from test_ai_migrate import AiMigrationContract, SCRIPT


class AiMigrationIntegrity(unittest.TestCase):
    setUp = AiMigrationContract.setUp
    put = AiMigrationContract.put
    policy = AiMigrationContract.policy
    write_registry = AiMigrationContract.write_registry
    command = AiMigrationContract.command

    def test_existing_memory_context_name_is_not_overwritten(self):
        self.put(self.root / '.claude/memory/legacy-agent-context.md',
                 b'Existing durable user memory must survive exactly.\n')
        for verb in ('plan', 'apply'):
            with self.subTest(verb=verb):
                before = snapshot(self.root)
                self.command(verb, expected=2)
                self.assertEqual(snapshot(self.root), before)

    def test_tracked_private_destination_rejected_before_archive(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        private = self.root / '.AI/project.md'
        self.put(private, b'already tracked private context\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.AI/project.md'], check=True)
        private.unlink()
        private.parent.rmdir()
        for verb in ('plan', 'apply'):
            with self.subTest(verb=verb):
                before = snapshot(self.root)
                self.command(verb, expected=2)
                self.assertEqual(snapshot(self.root), before)
                self.assertFalse((self.root / '.ai-bootstrap').exists())

    def valid_journal(self):
        spec = importlib.util.spec_from_file_location('migration_integrity_fixture', SCRIPT)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        args = argparse.Namespace(bundle=self.bundle, source_base=None,
                                  project_id=PROJECT_ID, types='coding,wiki',
                                  adapters='claude,codex,kimi')
        return migration.migration_plan(args, self.root)

    def assert_recovery_refuses_unchanged(self, journal):
        self.put(self.root / '.ai-bootstrap/migration.json', json.dumps(journal))
        before = snapshot(self.root)
        result = self.command('recover', expected=2)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / '.ai-bootstrap/legacy').exists())

    def test_malformed_steps_have_controlled_refusal_without_writes(self):
        valid = self.valid_journal()
        for malformed in (None, [], {'kind': 'dir', 'path': '.AI',
                                     'before': None, 'after': None}):
            with self.subTest(step=malformed):
                journal = copy.deepcopy(valid)
                journal['steps'][0] = malformed
                self.assert_recovery_refuses_unchanged(journal)

    def test_missing_nested_memory_step_refused_before_archive(self):
        journal = self.valid_journal()
        target = '.AI/memory/nested/fact.md'
        count = len(journal['steps'])
        journal['steps'] = [s for s in journal['steps'] if s.get('path') != target]
        self.assertEqual(len(journal['steps']), count - 1)
        self.assert_recovery_refuses_unchanged(journal)

    def test_omitted_docs_archive_refused_even_with_matching_forged_state(self):
        journal = self.valid_journal()
        journal['steps'] = [s for s in journal['steps']
                            if not (s['kind'] == 'move' and s['path'] == 'docs/dev')]
        del journal['state']['migration']['archives']['docs/dev']
        state_step = journal['steps'][-1]
        self.assertEqual(state_step['path'], '.AI/canon/canon.state.json')
        data = json.dumps(journal['state']).encode()
        state_step['data'] = base64.b64encode(data).decode()
        state_step['after']['sha256'] = hashlib.sha256(data).hexdigest()
        self.assert_recovery_refuses_unchanged(journal)

    def test_validate_journal_reports_invalid_for_incomplete_records(self):
        spec = importlib.util.spec_from_file_location('migration_validation_fixture', SCRIPT)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        valid = self.valid_journal()
        cases = {}
        unknown_link = copy.deepcopy(valid)
        unknown_link['steps'].insert(-1, {'kind': 'link', 'path': 'docs/notes',
                                         'before': None, 'after': {'kind': 'link'}})
        cases['unknown-link-without-target'] = unknown_link
        final_directory = copy.deepcopy(valid)
        final_directory['steps'][-1] = {'kind': 'dir', 'path': '.AI/canon/canon.state.json',
                                        'before': None, 'after': {'kind': 'dir'}}
        cases['final-state-is-directory'] = final_directory
        missing_source = copy.deepcopy(valid)
        target = next(iter(valid['state']['source_files'].values()))['path']
        missing_source['steps'] = [s for s in missing_source['steps'] if s['path'] != target]
        cases['missing-selected-source-payload'] = missing_source
        for name, journal in cases.items():
            with self.subTest(case=name):
                # JSON roundtrip excludes Python-only fixture values.
                journal = json.loads(json.dumps(journal))
                before = snapshot(self.root)
                with self.assertRaises(migration.Invalid):
                    migration.validate_journal(journal)
                self.assertEqual(snapshot(self.root), before)

    def test_journal_missing_destination_parent_is_rejected_before_replay(self):
        journal = self.valid_journal()
        original_count = len(journal['steps'])
        journal['steps'] = [step for step in journal['steps']
                            if not (step['kind'] == 'dir' and step['path'] == '.AI/rules')]
        self.assertEqual(len(journal['steps']), original_count - 1)
        self.put(self.root / '.ai-bootstrap/migration.json', json.dumps(journal))
        before = snapshot(self.root)
        self.command('recover', expected=2)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / '.ai-bootstrap/legacy').exists())


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(AiMigrationIntegrity)


if __name__ == '__main__':
    unittest.main()
