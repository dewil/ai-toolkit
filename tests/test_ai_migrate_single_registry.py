"""Synthetic Cactus acceptance cases for the retired split registry input."""
import json
import stat
import subprocess
import unittest

import test_ai_bootstrap as bootstrap
import test_ai_migrate as migrate_fixture


class AiMigrateSingleRegistry(unittest.TestCase):
    # Reuse the existing synthetic contract fixture without inheriting its suite.
    put = migrate_fixture.AiMigrationContract.put
    policy = migrate_fixture.AiMigrationContract.policy
    write_registry = migrate_fixture.AiMigrationContract.write_registry
    command = migrate_fixture.AiMigrationContract.command
    reject_unchanged = migrate_fixture.AiMigrationContract.reject_unchanged

    def setUp(self):
        migrate_fixture.AiMigrationContract.setUp(self)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)

    def add_opaque_auxiliary(self):
        self.auxiliary = {
            '.claude/canon.intent.yaml': (b'opaque intent\x00\xff\n', 0o640),
            '.claude/canon.state.json': (b'{"opaque":"old state","schema_version":2}\n', 0o600),
            '.claude/canon.ledger.json': (b'opaque ledger bytes\r\n', 0o644),
        }
        for path, (data, mode) in self.auxiliary.items():
            target = self.root / path
            self.put(target, data)
            target.chmod(mode)

    def test_monolithic_registry_migrates_and_archives_auxiliary_bytes(self):
        self.add_opaque_auxiliary()
        before_plan = bootstrap.snapshot(self.root)
        plan = json.loads(self.command('plan').stdout)
        self.assertIsInstance(plan, dict)
        self.assertEqual(bootstrap.snapshot(self.root), before_plan)

        self.command('apply')

        # Local tracked edits and personal content remain active and unchanged.
        self.assertEqual((self.root / '.AI/rules/coding.md').read_bytes(), self.legacy_rule)
        self.assertEqual((self.root / '.AI/memory/MEMORY.md').read_bytes(), self.legacy_memory)
        self.assertEqual((self.root / '.AI/skills/private/SKILL.md').read_bytes(), b'# Private skill\n')

        archive = self.root / '.ai-bootstrap/legacy'
        for path, (data, mode) in self.auxiliary.items():
            archived = archive / path
            self.assertEqual(archived.read_bytes(), data, path)
            self.assertEqual(stat.S_IMODE(archived.stat().st_mode), mode, path)
            self.assertFalse((self.root / path).exists(), path)
        state = json.loads((self.root / '.AI/canon/canon.state.json').read_text())
        self.assertIsInstance(state, dict)
        self.assertEqual(state.get('project_id'), bootstrap.PROJECT_ID)
        self.command('check')

    def test_split_only_inputs_still_refuse_without_mutation(self):
        (self.root / '.claude/canon.yaml').unlink()
        self.add_opaque_auxiliary()
        self.reject_unchanged()


if __name__ == '__main__':
    unittest.main(verbosity=2)
