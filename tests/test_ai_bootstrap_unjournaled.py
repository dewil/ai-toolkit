"""Fail-closed diagnostics for bootstrap artifacts without a journal."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'ai-bootstrap.py'
SAFE_TMPDIR = Path(tempfile.gettempdir())
PROJECT_ID = '22aaab19-84df-42b0-9f1a-51aa4fbb3425'
MANDATORY = ['rules/secrets-handling.md', 'rules/untrusted-content.md',
             'rules/memory-persistence.md', 'rules/agent-portability.md']


def tree(root):
    """Snapshot every entry, including lock files and symlink targets as links."""
    if not root.exists() and not root.is_symlink():
        return None
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ('link', os.readlink(path))
        elif path.is_dir():
            result[relative] = ('dir',)
        else:
            result[relative] = ('file', path.read_bytes())
    return result


class UnjournaledBootstrapDiagnostics(unittest.TestCase):
    def setUp(self):
        SAFE_TMPDIR.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SAFE_TMPDIR)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'client'
        self.root.mkdir()
        self.bundle = self.base / 'bundle'
        self.bundle.mkdir()
        self.sources = {p: ('# ' + p + '\nMandatory contract text.\n').encode()
                        for p in MANDATORY}
        self.sources.update({'rules/coding.md': b'# Coding\nUnique coding rule.\n',
                             'skills/sample/SKILL.md': b'# Sample skill\n',
                             'agents/reviewer.md': b'# Reviewer\n',
                             'commands/review.md': b'# Review command\n'})
        for path, content in self.sources.items():
            self.put(self.bundle / path, content)
        manifest = 'universal:\n' + ''.join('  - ' + p + '\n' for p in MANDATORY)
        manifest += ('  - skills/sample/SKILL.md\n  - agents/reviewer.md\n'
                     '  - commands/review.md\ncoding:\n  - rules/coding.md\n')
        self.put(self.bundle / 'manifest.yaml', manifest)
        for name, content in [('START.md', '# Start\n'), ('project.md', 'project\n'),
                              ('MEMORY.md', 'memory\n')]:
            self.put(self.bundle / 'templates/ai' / name, content)
        self.put(self.bundle / 'templates/ai/context-policy.json',
                 json.dumps({'mandatory': MANDATORY, 'max_bytes': 30000}))

    def put(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)

    def command(self, verb, extra=(), expected=None, timeout=15):
        args = [sys.executable, str(SCRIPT), verb, '--root', str(self.root)]
        if verb in ('plan', 'apply'):
            args += ['--bundle', str(self.bundle), '--project-id', PROJECT_ID,
                     '--types', 'coding', '--adapters', 'claude,codex,kimi']
        result = subprocess.run(args + list(extra), capture_output=True, text=True,
                                timeout=timeout,
                                env={**os.environ, 'TMPDIR': str(SAFE_TMPDIR)})
        if expected is not None:
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def assert_diagnostics(self, output):
        # Check ideas conveyed by the contract without locking user-facing prose.
        lower = output.lower()
        self.assertIn('journal', lower, output)
        self.assertTrue(any(word in lower for word in ('read-only', 'read only', 'inspect', 'inventory')),
                        output)
        self.assertTrue(any(word in lower for word in ('preserv', 'untouched', 'not alter', 'not modify')),
                        output)
        self.assertTrue(any(word in lower for word in ('replay', 'recovery')),
                        output)
        self.assertTrue(any(word in lower for word in ('manual', 'decision', 'operator')),
                        output)

    def test_lock_only_plan_and_apply_explain_safe_next_step_without_migration_claim(self):
        directory = self.root / '.ai-bootstrap'
        directory.mkdir()
        lock = directory / 'lock'
        lock.write_bytes(b'opaque lock bytes\x00LOCK_CONTENT_SENTINEL')
        before = tree(self.root)

        for verb in ('plan', 'apply'):
            with self.subTest(verb=verb):
                result = self.command(verb, expected=2)
                self.assert_diagnostics(result.stdout + result.stderr)
                self.assertNotIn('migration', (result.stdout + result.stderr).lower())
                self.assertNotIn('lock_content_sentinel', (result.stdout + result.stderr).lower())
                self.assertEqual(tree(self.root), before)

        checked = self.command('check')
        self.assertNotIn('migration', (checked.stdout + checked.stderr).lower())
        self.assertEqual(tree(self.root), before)

    def test_unknown_sidecar_recovers_with_no_transaction_plus_limits_and_no_writes(self):
        directory = self.root / '.ai-bootstrap'
        directory.mkdir()
        (directory / 'lock').write_bytes(b'lock\x00LOCK_CONTENT_SENTINEL')
        nested = directory / 'opaque' / 'operator-note.bin'
        nested.parent.mkdir()
        nested.write_bytes(b'UNKNOWN_CONTENT_SENTINEL\x00including arbitrary data')
        before = tree(self.root)

        for verb in ('plan', 'apply'):
            with self.subTest(verb=verb):
                result = self.command(verb, expected=2)
                self.assert_diagnostics(result.stdout + result.stderr)
                self.assertNotIn('sentinel', (result.stdout + result.stderr).lower())
                self.assertEqual(tree(self.root), before)

        result = self.command('recover', expected=0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload.get('status'), 'no-transaction')
        self.assert_diagnostics(result.stdout + result.stderr)
        self.assertNotIn('sentinel', (result.stdout + result.stderr).lower())
        self.assertEqual(tree(self.root), before)

    def test_empty_and_healthy_clients_keep_existing_plan_and_recover_contracts(self):
        empty_before = tree(self.root)
        plan = json.loads(self.command('plan', expected=0).stdout)
        self.assertEqual(plan.get('status'), 'planned')
        recovered = json.loads(self.command('recover', expected=0).stdout)
        self.assertEqual(recovered.get('status'), 'no-transaction')
        self.assertEqual(tree(self.root), empty_before)

        self.command('apply', expected=0)
        healthy_before = tree(self.root)
        healthy_plan_result = self.command('plan', expected=0)
        healthy_plan = json.loads(healthy_plan_result.stdout)
        healthy_recover_result = self.command('recover', expected=0)
        healthy_recover = json.loads(healthy_recover_result.stdout)
        self.assertEqual(healthy_plan.get('status'), 'up-to-date')
        self.assertEqual(healthy_plan.get('changes'), [])
        self.assertEqual(healthy_recover.get('status'), 'no-transaction')
        self.assertEqual(healthy_recover_result.stdout, '{"status": "no-transaction"}\n')
        self.assertEqual(healthy_recover_result.stderr, '')
        self.assertEqual(tree(self.root), healthy_before)
        self.assertIsInstance(plan, dict)
        self.assertIsInstance(healthy_plan, dict)
        self.assertNotIn('diagnostic', json.dumps(healthy_recover).lower())

    def test_non_directory_bootstrap_entry_fails_closed_without_touching_target(self):
        target = self.base / 'outside'
        target.mkdir()
        self.put(target / 'marker', b'outside bytes')
        for kind in ('file', 'symlink', 'dangling-symlink'):
            with self.subTest(kind=kind):
                entry = self.root / '.ai-bootstrap'
                if entry.exists() or entry.is_symlink():
                    if entry.is_dir() and not entry.is_symlink():
                        import shutil
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                if kind == 'file':
                    entry.write_bytes(b'opaque directory collision')
                elif kind == 'dangling-symlink':
                    entry.symlink_to(self.base / 'missing-target')
                else:
                    entry.symlink_to(target, target_is_directory=True)
                before = tree(self.base)
                for verb in ('plan', 'apply'):
                    result = self.command(verb, expected=2)
                    self.assertTrue((result.stdout + result.stderr).strip())
                    self.assertEqual(tree(self.base), before)
                recovered = self.command('recover', expected=2)
                self.assertTrue((recovered.stdout + recovered.stderr).strip())
                self.assertEqual(tree(self.base), before)

    def test_foreign_journal_remains_owned_by_its_existing_validation(self):
        directory = self.root / '.ai-bootstrap'
        directory.mkdir()
        journal = directory / 'journal.json'
        journal.write_text(json.dumps({'schema_version': 999, 'kind': 'foreign'}))
        before = tree(self.root)

        for verb in ('plan', 'check', 'recover', 'apply'):
            with self.subTest(verb=verb):
                result = self.command(verb)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(tree(self.root), before)
                self.assertNotIn('read-only inventory', (result.stdout + result.stderr).lower())

    def test_recorded_journal_is_replayed_before_orphan_artifact_diagnostics(self):
        wrapper = '''import os, pathlib, runpy, sys
script, root, bundle = sys.argv[1:4]
real_replace = os.replace
def fail_state(src, dst, *args, **kwargs):
    if pathlib.Path(dst).absolute() == pathlib.Path(root).absolute() / '.AI/canon/canon.state.json':
        raise OSError('injected state commit failure')
    return real_replace(src, dst, *args, **kwargs)
os.replace = fail_state
sys.argv = [script, 'apply', '--root', root, '--bundle', bundle,
            '--project-id', '22aaab19-84df-42b0-9f1a-51aa4fbb3425',
            '--types', 'coding', '--adapters', 'claude,codex,kimi']
runpy.run_path(script, run_name='__main__')
'''
        failed = subprocess.run([sys.executable, '-c', wrapper, str(SCRIPT), str(self.root),
                                  str(self.bundle)], capture_output=True, text=True, timeout=15,
                                 env={**os.environ, 'TMPDIR': str(SAFE_TMPDIR)})
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        journal = self.root / '.ai-bootstrap/journal.json'
        self.assertTrue(journal.is_file())
        before = tree(self.root)
        recovered = self.command('recover', expected=0)
        self.assertEqual(json.loads(recovered.stdout).get('status'), 'recovered')
        self.assertNotIn('read-only inventory', (recovered.stdout + recovered.stderr).lower())
        self.assertNotEqual(tree(self.root), before)
        self.assertEqual(json.loads(self.command('check', expected=0).stdout).get('status'), 'ok')

    def test_busy_lock_is_never_removed_and_operation_fails_without_waiting(self):
        directory = self.root / '.ai-bootstrap'
        directory.mkdir()
        lock_path = directory / 'lock'
        lock_path.write_bytes(b'busy lock bytes')
        with lock_path.open('r+b') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = tree(self.root)
            result = self.command('apply', expected=2, timeout=5)
            self.assertEqual(tree(self.root), before)
            self.assertEqual(lock_path.read_bytes(), b'busy lock bytes')
            self.assertNotIn('stale', (result.stdout + result.stderr).lower())
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)


if __name__ == '__main__':
    unittest.main()
