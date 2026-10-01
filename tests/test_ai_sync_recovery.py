"""Independent FR-AIS05/06 crash, replay, full prevalidation and lock checks."""
import fcntl
import json
import select
import shutil
import subprocess
import sys
import unittest

import test_ai_bootstrap as bootstrap
from test_ai_sync import SCRIPT, SyncFixture, tree

WRAPPER = '''import runpy, sys
script, root, bundle, digest, point, timing, mode = sys.argv[1:8]
main = runpy.run_path(script, run_name='sync_fault_test')['main']
globals_ = main.__globals__
real_install = globals_['install_action']
fired = False
def injected(root, action):
    global fired
    matches = not fired and action['path'] == point
    def fault():
        global fired
        fired = True
        print('FAULT_POINT', flush=True)
        if mode == 'pause':
            sys.stdin.readline()
        else:
            raise OSError('independent sync interruption')
    if matches and timing == 'before':
        fault()
    result = real_install(root, action)
    if matches and timing == 'after':
        fault()
    return result
globals_['install_action'] = injected
sys.exit(main(['apply', '--root', root, '--bundle', bundle, '--expect-plan', digest]))
'''


class AiSyncRecovery(SyncFixture):
    def fault_args(self, point='.AI/rules/coding.md', timing='after', mode='fail'):
        self.upstream('rules/coding.md', b'# Coding\nCRASH_GENERATION\n')
        self.upstream('rules/wiki.md', b'# Wiki\nCRASH_GENERATION\n')
        digest = self.plan()['plan_sha256']
        return [sys.executable, '-c', WRAPPER, str(SCRIPT), str(self.root), str(self.bundle),
                digest, point, timing, mode]

    def interrupt(self, point='.AI/rules/coding.md', timing='after'):
        result = subprocess.run(self.fault_args(point, timing), capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('FAULT_POINT', result.stdout, 'documented mutation seam not reached')
        self.journal = self.root / '.ai-bootstrap/sync.json'
        self.assertTrue(self.journal.is_file())
        self.assertEqual(self.read_json(self.state_path), self.initial_state, 'state must be committed last')
        self.cli('check', reject=True)

    def assert_recovered(self):
        self.cli('recover')
        self.assertFalse(self.journal.exists())
        for path in ['rules/coding.md', 'rules/wiki.md']:
            self.assertIn(b'CRASH_GENERATION', self.destination(path).read_bytes())
        self.cli('check')
        before = tree(self.root)
        self.cli('recover')
        self.assertEqual(tree(self.root), before)

    def test_05_offline_recovery_after_canon_step(self):
        self.interrupt()
        shutil.rmtree(self.bundle)
        self.assert_recovered()

    def test_05_offline_recovery_before_generated(self):
        self.interrupt('AGENTS.md', 'before')
        shutil.rmtree(self.bundle)
        self.assert_recovered()

    def test_05_offline_recovery_before_state(self):
        self.interrupt('.AI/canon/canon.state.json', 'before')
        shutil.rmtree(self.bundle)
        self.assert_recovered()

    def test_05_recovery_preserves_installed_pending_and_retained_input_drift(self):
        self.interrupt()
        for target in [self.destination('rules/coding.md'), self.destination('rules/wiki.md'),
                       self.destination('commands/review.md')]:
            with self.subTest(path=target.name):
                original = target.read_bytes()
                target.write_bytes(b'POST_CRASH_USER_DRIFT\n')
                before = tree(self.root)
                self.cli('recover', reject=True)
                self.assertEqual(tree(self.root), before)
                target.write_bytes(original)
        self.assert_recovered()

    def test_05_entire_journal_validated_before_first_write(self):
        self.interrupt()
        original = self.journal.read_bytes()
        outside = self.base / 'outside-victim'
        outside.write_bytes(b'EXTERNAL_USER_DATA\n')
        for change in ['schema', 'kind', 'root', 'uuid', 'traversal', 'private', 'state-order', 'hash']:
            with self.subTest(change=change):
                data = json.loads(original)
                if change == 'schema':
                    data['schema_version'] = 999
                elif change == 'kind':
                    data['kind'] = 'ai-migrate'
                elif change == 'root':
                    data['root'] = str(self.base / 'wrong-client')
                elif change == 'uuid':
                    data['project_id'] = '00000000-0000-4000-8000-000000000000'
                elif change in ['traversal', 'private']:
                    data['actions'][-1]['path'] = '../outside-victim' if change == 'traversal' else '.AI/memory/MEMORY.md'
                elif change == 'state-order':
                    data['actions'] = list(reversed(data['actions']))
                else:
                    data['plan_sha256'] = '0' * 64
                self.journal.write_text(json.dumps(data))
                before = tree(self.base)
                self.cli('recover', reject=True)
                self.assertEqual(tree(self.base), before)
        self.journal.write_bytes(original)
        self.assert_recovered()

    def test_05_late_action_payload_and_mode_tamper_prevalidated(self):
        self.interrupt()
        original = self.journal.read_bytes()
        # Mutate recorded descriptors recursively without relying on their key layout.
        # At least one after descriptor must expose the promised hash and mode.
        def corrupt(value, field):
            if isinstance(value, dict):
                for key in list(value):
                    if key == field:
                        value[key] = '0' * 64 if field == 'sha256' else '100777'
                        return True
                    if corrupt(value[key], field):
                        return True
            elif isinstance(value, list):
                return any(corrupt(item, field) for item in reversed(value))
            return False
        for field in ['sha256', 'mode']:
            with self.subTest(field=field):
                data = json.loads(original)
                self.assertTrue(corrupt(data['actions'][-1]['after'], field))
                self.journal.write_text(json.dumps(data))
                before = tree(self.root)
                self.cli('recover', reject=True)
                self.assertEqual(tree(self.root), before)
        self.journal.write_bytes(original)

    def test_06_bootstrap_and_migrate_refuse_pending_sync(self):
        self.interrupt()
        for name, verbs in [('ai-bootstrap.py', ['check', 'build', 'recover', 'apply']),
                            ('ai-migrate.py', ['check', 'recover', 'apply'])]:
            for verb in verbs:
                with self.subTest(tool=name, verb=verb):
                    args = [sys.executable, str(SCRIPT.with_name(name)), verb, '--root', str(self.root)]
                    if verb == 'apply':
                        args += ['--bundle', str(self.bundle), '--project-id', bootstrap.PROJECT_ID,
                                 '--types', 'coding,wiki', '--adapters', 'claude,codex,kimi']
                    before = tree(self.root)
                    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(tree(self.root), before)

    def test_06_foreign_bootstrap_or_migration_journals_never_replayed(self):
        for journal in ['journal.json', 'migration.json']:
            with self.subTest(journal=journal):
                path = self.root / '.ai-bootstrap' / journal
                path.write_text(json.dumps({'schema_version': 999, 'kind': 'foreign'}))
                for verb in ['plan', 'check', 'recover']:
                    before = tree(self.root)
                    self.cli(verb, reject=True)
                    self.assertEqual(tree(self.root), before)
                self.reject_apply('0' * 64)
                path.unlink()

    def test_06_shared_exclusive_writer_lock_with_other_tools(self):
        first = subprocess.Popen(self.fault_args(mode='pause'), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([first.stdout], [], [], 15)
            self.assertTrue(readable, 'first sync writer did not acquire lock/reach seam')
            self.assertEqual(first.stdout.readline().strip(), 'FAULT_POINT')
            for name, verb in [('ai-sync.py', 'recover'), ('ai-bootstrap.py', 'build'),
                               ('ai-migrate.py', 'recover')]:
                before = tree(self.root)
                result = subprocess.run([sys.executable, str(SCRIPT.with_name(name)), verb,
                                         '--root', str(self.root)], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(tree(self.root), before)
            stdout, stderr = first.communicate('continue\n', timeout=20)
            self.assertEqual(first.returncode, 0, stdout + stderr)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()
        self.cli('check')

    def test_06_healthy_root_apply_and_recover_use_existing_exclusive_lock(self):
        self.upstream('rules/coding.md', b'# Coding\nLOCKED_UPDATE\n')
        plan = self.plan()
        lock = self.root / '.ai-bootstrap/lock'
        lock.parent.mkdir(exist_ok=True)
        with lock.open('a+b') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse((self.root / '.ai-bootstrap/sync.json').exists())
            self.reject_apply(plan['plan_sha256'])
            before = tree(self.root)
            self.cli('recover', reject=True)
            self.assertEqual(tree(self.root), before)
            self.assertFalse((self.root / '.ai-bootstrap/sync.json').exists())
        self.apply(plan)
        self.cli('check')

    def test_06_healthy_root_shared_reader_lock_blocks_sync_exclusive_writer(self):
        self.upstream('rules/coding.md', b'# Coding\nEXCLUSIVE_UPDATE\n')
        plan = self.plan()
        lock = self.root / '.ai-bootstrap/lock'
        lock.parent.mkdir(exist_ok=True)
        with lock.open('a+b') as held:
            fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertFalse((self.root / '.ai-bootstrap/sync.json').exists())
            self.reject_apply(plan['plan_sha256'])
        self.apply(plan)
        self.cli('check')

    def test_06_external_writer_drift_at_step_is_preserved(self):
        args = self.fault_args(point='.AI/rules/wiki.md', timing='before', mode='pause')
        first = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([first.stdout], [], [], 15)
            self.assertTrue(readable)
            self.assertEqual(first.stdout.readline().strip(), 'FAULT_POINT')
            self.destination('rules/wiki.md').write_bytes(b'UNCOOPERATIVE_WRITER\n')
            stdout, stderr = first.communicate('continue\n', timeout=20)
            self.assertNotEqual(first.returncode, 0, stdout + stderr)
            self.assertEqual(self.destination('rules/wiki.md').read_bytes(), b'UNCOOPERATIVE_WRITER\n')
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()


if __name__ == '__main__':
    unittest.main()
