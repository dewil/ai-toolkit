"""Independent migration preservation and recovery regressions."""
import argparse
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

    def test_journal_missing_destination_parent_is_rejected_before_replay(self):
        # Obtain a valid recorded plan, then remove one operation. The assertion
        # concerns the public recovery CLI and all original project bytes.
        spec = importlib.util.spec_from_file_location('migration_integrity_fixture', SCRIPT)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        args = argparse.Namespace(bundle=self.bundle, source_base=None,
                                  project_id=PROJECT_ID, types='coding,wiki',
                                  adapters='claude,codex,kimi')
        journal = migration.migration_plan(args, self.root)
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
