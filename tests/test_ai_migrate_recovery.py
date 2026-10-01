"""Independent crash/replay/locking checks for the migration transaction."""
import base64
import json
import select
import subprocess
import sys
import unittest

import test_ai_bootstrap as bootstrap
import test_ai_migrate as migration

WRAPPER = '''import runpy, sys
script, root, bundle, project_id, point, mode = sys.argv[1:7]
namespace = runpy.run_path(script, run_name='migration_fault_test')
main = namespace['main']
globals_ = main.__globals__
real_install = globals_['install_step']
fired = False
def injected(root, step):
    global fired
    result = real_install(root, step)
    if not fired and step['path'] == point:
        fired = True
        print('FAULT_POINT', flush=True)
        if mode == 'pause':
            sys.stdin.readline()
        else:
            raise OSError('independent post-step fault')
    return result
globals_['install_step'] = injected
sys.exit(main(['apply', '--root', root, '--bundle', bundle, '--project-id', project_id,
              '--types', 'coding,wiki', '--adapters', 'claude,codex,kimi']))
'''


class AiMigrationRecovery(unittest.TestCase):
    setUp = migration.AiMigrationContract.setUp
    put = migration.AiMigrationContract.put
    policy = migration.AiMigrationContract.policy
    write_registry = migration.AiMigrationContract.write_registry
    command = migration.AiMigrationContract.command

    def fault_args(self, point, mode='fail'):
        return [sys.executable, '-c', WRAPPER, str(migration.SCRIPT), str(self.root),
                str(self.bundle), bootstrap.PROJECT_ID, point, mode]

    def interrupt(self, point='AGENTS.md'):
        result = subprocess.run(self.fault_args(point), capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('FAULT_POINT', result.stdout)
        self.assertTrue((self.root / '.ai-bootstrap/migration.json').is_file())
        self.command('check', expected=2)

    def assert_success(self):
        self.command('check')
        self.assertFalse((self.root / '.ai-bootstrap/migration.json').exists())
        self.assertEqual((self.root / '.ai-bootstrap/legacy/CLAUDE.md').read_bytes(), self.legacy_context)
        self.assertEqual((self.root / '.AI/memory/MEMORY.md').read_bytes(), self.legacy_memory)
        self.assertEqual((self.root / '.AI/rules/coding.md').read_bytes(), self.legacy_rule)
        self.assertEqual((self.root / 'docs/backlog/task.md').read_bytes(), b'Task ID: TASK-17\n')
        before = bootstrap.snapshot(self.root)
        self.command('recover')
        self.assertEqual(bootstrap.snapshot(self.root), before)

    def test_recover_after_legacy_archive_preserves_original_backup(self):
        self.interrupt('.claude')
        backup = self.root / '.ai-bootstrap/legacy/.claude'
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / 'memory/MEMORY.md').read_bytes(), self.legacy_memory)
        self.command('recover')
        self.assert_success()

    def test_recover_after_first_entry_uses_recorded_bytes_without_bundle(self):
        self.interrupt()
        journal = json.loads((self.root / '.ai-bootstrap/migration.json').read_text())
        planned = {step['path']: base64.b64decode(step['data'])
                   for step in journal['steps'] if step['kind'] == 'file'}
        self.bundle.rename(self.base / 'unavailable-bundle')
        self.command('recover')
        for path in ['AGENTS.md', 'CLAUDE.md', '.AI/canon/canon.state.json']:
            self.assertEqual((self.root / path).read_bytes(), planned[path])
        self.assert_success()

    def test_recover_refuses_unexpected_generated_entry_edit_without_writes(self):
        self.interrupt()
        (self.root / 'AGENTS.md').write_bytes(b'USER_EDIT_AFTER_CRASH\n')
        before = bootstrap.snapshot(self.root)
        self.command('recover', expected=2)
        self.assertEqual(bootstrap.snapshot(self.root), before)

    def test_recover_refuses_edited_archived_data_without_writes(self):
        self.interrupt('.claude')
        (self.root / '.ai-bootstrap/legacy/.claude/memory/MEMORY.md').write_bytes(b'USER_EDIT_BACKUP\n')
        before = bootstrap.snapshot(self.root)
        self.command('recover', expected=2)
        self.assertEqual(bootstrap.snapshot(self.root), before)

    def test_invalid_journal_is_fully_validated_before_replaying_any_step(self):
        self.interrupt('.claude')
        path = self.root / '.ai-bootstrap/migration.json'
        original = json.loads(path.read_text())
        outside = self.base / 'outside-victim.txt'
        outside.write_bytes(b'EXTERNAL_USER_DATA')
        for attack in ['version', 'traversal', 'absolute', 'payload']:
            with self.subTest(attack=attack):
                journal = json.loads(json.dumps(original))
                if attack == 'version':
                    journal['schema_version'] = 999
                elif attack == 'payload':
                    journal['steps'][-1]['data'] = base64.b64encode(b'forged state').decode()
                else:
                    journal['steps'][-1]['path'] = '../outside-victim.txt' if attack == 'traversal' else str(outside)
                path.write_text(json.dumps(journal))
                before = bootstrap.snapshot(self.base)
                self.command('recover', expected=2)
                self.assertEqual(bootstrap.snapshot(self.base), before)

    def test_bootstrap_operations_refuse_migration_journal_without_writes(self):
        self.interrupt('.claude')
        for verb in ['check', 'build', 'recover']:
            with self.subTest(command=verb):
                before = bootstrap.snapshot(self.root)
                result = subprocess.run([sys.executable, str(bootstrap.SCRIPT), verb, '--root', str(self.root)],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('ai-migrate', result.stdout + result.stderr)
                self.assertIn('recover', result.stdout + result.stderr)
                self.assertEqual(bootstrap.snapshot(self.root), before)

    def test_migration_and_bootstrap_share_writer_lock(self):
        first = subprocess.Popen(self.fault_args('AGENTS.md', 'pause'), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([first.stdout], [], [], 15)
            self.assertTrue(readable, 'first writer did not reach fault point')
            self.assertEqual(first.stdout.readline().strip(), 'FAULT_POINT')
            before = bootstrap.snapshot(self.root)
            for script, verb in [(migration.SCRIPT, 'recover'), (bootstrap.SCRIPT, 'build')]:
                result = subprocess.run([sys.executable, str(script), verb, '--root', str(self.root)],
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(bootstrap.snapshot(self.root), before)
            stdout, stderr = first.communicate('continue\n', timeout=15)
            self.assertEqual(first.returncode, 0, stdout + stderr)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()
        self.assert_success()


if __name__ == '__main__':
    unittest.main()
