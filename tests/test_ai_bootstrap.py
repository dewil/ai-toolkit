"""Independent FR-AIB contract tests; no network or production project fixtures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'ai-bootstrap.py'
PROJECT_ID = '22aaab19-84df-42b0-9f1a-51aa4fbb3425'
MANDATORY = ['rules/secrets-handling.md', 'rules/untrusted-content.md',
             'rules/memory-persistence.md', 'rules/agent-portability.md']


def snapshot(root):
    if not root.exists():
        return None
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative.startswith('.ai-bootstrap/') and 'lock' in path.name:
            continue
        if relative == '.ai-bootstrap' and path.is_dir() and not list(path.iterdir()):
            continue
        result[relative] = ('link', os.readlink(path)) if path.is_symlink() else (
            ('dir',) if path.is_dir() else ('file', path.read_bytes()))
    return result


class AiBootstrapContract(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "ai-bootstrap implementation is required")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'client'
        self.root.mkdir()
        self.bundle = self.base / 'bundle'
        self.bundle.mkdir()
        self.sources = {p: ('# ' + p + '\nMandatory contract text.\n').encode() for p in MANDATORY}
        self.sources.update({'rules/coding.md': b'# Coding\nUnique coding rule.\n',
                             'rules/wiki.md': b'# Wiki\nUnique wiki rule.\n',
                             'skills/sample/SKILL.md': b'# Sample skill\n',
                             'agents/reviewer.md': b'# Reviewer\n',
                             'commands/review.md': b'# Review command\n',
                             'scripts/helper.py': b'#!/usr/bin/env python3\nprint("hello")\n'})
        for path, content in self.sources.items():
            self.put(self.bundle / path, content)
        manifest = 'universal:\n' + ''.join('  - ' + p + '\n' for p in MANDATORY)
        manifest += '  - skills/sample/SKILL.md\n  - agents/reviewer.md\n  - commands/review.md\n  - scripts/helper.py\ncoding:\n  - rules/coding.md\n  - rules/secrets-handling.md\nwiki:\n  - rules/wiki.md\n'
        self.put(self.bundle / 'manifest.yaml', manifest)
        for name, content in [('START.md', '# Start\nRead .AI/project.md and .AI/memory/MEMORY.md.\n'),
                              ('project.md', 'PRIVATE_PROJECT_SENTINEL\n'),
                              ('MEMORY.md', 'PRIVATE_MEMORY_SENTINEL\n')]:
            self.put(self.bundle / 'templates/ai' / name, content)
        self.policy()

    def put(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)

    def policy(self, budget=30000):
        self.put(self.bundle / 'templates/ai/context-policy.json',
                 json.dumps({'mandatory': MANDATORY, 'max_bytes': budget}))

    def command(self, verb, extra=(), expected=0):
        args = [sys.executable, str(SCRIPT), verb, '--root', str(self.root)]
        if verb in ('plan', 'apply'):
            args += ['--bundle', str(self.bundle), '--project-id', PROJECT_ID,
                     '--types', 'coding,wiki', '--adapters', 'claude,codex,kimi']
        result = subprocess.run(args + list(extra), capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        if expected:
            self.assertTrue((result.stdout + result.stderr).strip())
        return result

    def reject_unchanged(self, extra=()):
        before = snapshot(self.root)
        self.command('apply', extra, expected=2)
        self.assertEqual(snapshot(self.root), before)

    def test_01_plan_is_json_and_read_only_even_for_missing_root(self):
        before = snapshot(self.base)
        json.loads(self.command('plan').stdout)
        self.assertEqual(snapshot(self.base), before)
        self.root = self.base / 'missing'
        json.loads(self.command('plan').stdout)
        self.assertFalse(self.root.exists())
        self.command('apply', expected=2)
        self.assertFalse(self.root.exists())

    def test_01_apply_installs_exact_union_mapping_and_metadata(self):
        self.command('apply')
        for source, content in self.sources.items():
            destination = source if source.startswith('scripts/') else '.AI/' + source.replace('agents/', 'roles/', 1)
            self.assertEqual((self.root / destination).read_bytes(), content)
        for name in ['project.json', 'project.md', 'START.md', 'memory/MEMORY.md',
                     'canon/context-policy.json', 'canon/canon.intent.yaml', 'canon/canon.state.json']:
            self.assertTrue((self.root / '.AI' / name).is_file(), name)
        project = json.loads((self.root / '.AI/project.json').read_text())
        self.assertEqual(project['project_id'], PROJECT_ID)
        self.assertEqual(set(project['adapters']), {'claude', 'codex', 'kimi'})
        for name in ['canon.intent.yaml', 'canon.state.json']:
            data = json.loads((self.root / '.AI/canon' / name).read_text())
            self.assertEqual(data['schema_version'], 2)
        for alias, target in [('rules', 'rules'), ('skills', 'skills'), ('agents', 'roles'),
                              ('commands', 'commands'), ('memory', 'memory')]:
            link = self.root / '.claude' / alias
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (self.root / '.AI' / target).resolve())
        self.assertEqual((self.root / '.agents/skills').resolve(), (self.root / '.AI/skills').resolve())
        for name in ['AGENTS.md', 'CLAUDE.md']:
            content = (self.root / name).read_text()
            for source in MANDATORY:
                self.assertIn(self.sources[source].decode(), content)
            self.assertIn('.AI/project.md', content)
            self.assertIn('.AI/memory/MEMORY.md', content)
            self.assertNotIn('PRIVATE_PROJECT_SENTINEL', content)
            self.assertNotIn('PRIVATE_MEMORY_SENTINEL', content)
        self.command('check')

    def test_02_apply_is_noop_and_changed_configuration_refused(self):
        self.command('apply')
        before = snapshot(self.root)
        self.command('apply')
        self.assertEqual(snapshot(self.root), before)
        self.reject_unchanged(['--types', 'wiki'])

    def test_02_invalid_selections_and_uuid_are_rejected(self):
        for extra in [('--types', 'unknown'), ('--adapters', 'deepseek'),
                      ('--adapters', 'unknown'), ('--project-id', 'not-a-uuid')]:
            with self.subTest(extra=extra):
                self.reject_unchanged(extra)

    def test_02_missing_mandatory_source_and_budget_reject_before_write(self):
        source = self.bundle / MANDATORY[0]
        source.unlink()
        self.reject_unchanged()
        self.put(source, self.sources[MANDATORY[0]])
        self.policy(1)
        self.reject_unchanged()

    def test_02_branch_source_is_rejected(self):
        before = snapshot(self.root)
        result = subprocess.run([sys.executable, str(SCRIPT), 'apply', '--root', str(self.root),
                                 '--source-base', 'https://raw.githubusercontent.com/example/repo/main',
                                 '--project-id', PROJECT_ID], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(snapshot(self.root), before)

    def test_03_existing_agent_paths_and_script_are_preserved(self):
        for relative in ['.AI', '.claude', '.agents', 'AGENTS.md', 'CLAUDE.md', 'scripts/helper.py']:
            with self.subTest(path=relative):
                self.root = self.base / ('client-' + relative.replace('/', '-'))
                self.root.mkdir()
                path = self.root / relative
                if relative.startswith('.'):
                    self.put(path / 'user.txt', 'user-owned')
                else:
                    self.put(path, 'user-owned')
                self.reject_unchanged()

    def test_03_unsafe_manifest_and_case_unicode_collisions(self):
        manifest = self.bundle / 'manifest.yaml'
        original = manifest.read_text()
        for paths in [('../escape.md',), ('/tmp/escape.md',), ('rules/../escape.md',),
                      ('rules/Case.md', 'rules/case.md'), ('rules/caf\u00e9.md', 'rules/cafe\u0301.md')]:
            with self.subTest(paths=paths):
                for path in paths:
                    if path.startswith('rules/') and '..' not in path:
                        self.put(self.bundle / path, 'collision')
                manifest.write_text(original + ''.join('  - ' + p + '\n' for p in paths))
                self.reject_unchanged()
        manifest.write_text(original)

    def test_03_destination_symlink_escape_preserved(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'scripts').symlink_to(outside, target_is_directory=True)
        before = snapshot(outside)
        self.reject_unchanged()
        self.assertEqual(snapshot(outside), before)

    def test_04_build_determinism_source_staleness_and_canon_baseline(self):
        self.command('apply')
        before = snapshot(self.root)
        self.command('build')
        self.assertEqual(snapshot(self.root), before)
        state_path = self.root / '.AI/canon/canon.state.json'
        baseline = hashlib.sha256(self.sources[MANDATORY[0]]).hexdigest()
        self.assertIn(baseline, state_path.read_text())
        path = self.root / '.AI' / MANDATORY[0]
        path.write_text(path.read_text() + '\nNEW_MANDATORY_FACT\n')
        self.command('check', expected=1)
        self.command('build')
        for name in ['AGENTS.md', 'CLAUDE.md']:
            self.assertIn('NEW_MANDATORY_FACT', (self.root / name).read_text())
        self.assertIn(baseline, state_path.read_text())
        self.command('check')
        before = snapshot(self.root)
        self.command('build')
        self.assertEqual(snapshot(self.root), before)

    def test_04_manual_output_blocks_build_without_loss(self):
        self.command('apply')
        (self.root / 'AGENTS.md').write_text('HAND_EDITED_ENTRY\n')
        self.command('check', expected=1)
        before = snapshot(self.root)
        self.command('build', expected=2)
        self.assertEqual(snapshot(self.root), before)

    def interrupted_build(self):
        self.command('apply')
        source = self.root / '.AI' / MANDATORY[0]
        source.write_text(source.read_text() + '\nRECOVER_GENERATION\n')
        wrapper = '''import os, pathlib, runpy, sys
script, root = sys.argv[1:3]
real_replace = os.replace
seen = []
def fail_second(src, dst, *args, **kwargs):
    target = pathlib.Path(dst).absolute()
    if target.parent == pathlib.Path(root).absolute() and target.name in ('AGENTS.md', 'CLAUDE.md'):
        seen.append(target.name)
        if len(seen) == 2:
            raise OSError('injected second input replacement failure')
    return real_replace(src, dst, *args, **kwargs)
os.replace = fail_second
sys.argv = [script, 'build', '--root', root]
runpy.run_path(script, run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-c', wrapper, str(SCRIPT), str(self.root)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, 'fault injection did not intercept replacement')
        self.command('check', expected=1)
        self.command('build', expected=2)

    def test_05_recover_replays_recorded_generation_and_is_idempotent(self):
        self.interrupted_build()
        source = self.root / '.AI' / MANDATORY[0]
        source.write_text(source.read_text() + '\nPOST_CRASH_SOURCE_CHANGE\n')
        self.command('recover')
        for name in ['AGENTS.md', 'CLAUDE.md']:
            content = (self.root / name).read_text()
            self.assertIn('RECOVER_GENERATION', content)
            self.assertNotIn('POST_CRASH_SOURCE_CHANGE', content)
        before = snapshot(self.root)
        self.command('recover')
        self.assertEqual(snapshot(self.root), before)
        self.command('check', expected=1)
        self.command('build')
        self.command('check')

    def test_05_recover_preserves_unexpected_manual_edit(self):
        self.interrupted_build()
        for name in ['AGENTS.md', 'CLAUDE.md']:
            (self.root / name).write_text('POST_CRASH_USER_EDIT\n')
        before = snapshot(self.root)
        self.command('recover', expected=2)
        self.assertEqual(snapshot(self.root), before)

    def test_06_unknown_state_schema_refuses_without_writes(self):
        self.command('apply')
        path = self.root / '.AI/canon/canon.state.json'
        state = json.loads(path.read_text())
        state['schema_version'] = 999
        path.write_text(json.dumps(state))
        before = snapshot(self.root)
        self.command('build', expected=2)
        self.assertEqual(snapshot(self.root), before)
        self.command('check', expected=2)

    def test_06_unsafe_state_destination_refuses_without_outside_write(self):
        self.command('apply')
        path = self.root / '.AI/canon/canon.state.json'
        state = json.loads(path.read_text())
        first = next(iter(state['source_files']))
        state['source_files'][first]['path'] = '../outside-victim.md'
        victim = self.base / 'outside-victim.md'
        victim.write_text('OUTSIDE_USER_FILE')
        path.write_text(json.dumps(state))
        before = snapshot(self.base)
        self.command('build', expected=2)
        self.assertEqual(snapshot(self.base), before)
        self.command('check', expected=2)

    def test_06_concurrent_build_cannot_start_second_transaction(self):
        self.command('apply')
        path = self.root / '.AI' / MANDATORY[0]
        path.write_text(path.read_text() + '\nCONCURRENT_GENERATION\n')
        wrapper = '''import os, pathlib, runpy, sys
script, root = sys.argv[1:3]
real_replace = os.replace
blocked = False
def wait_once(src, dst, *args, **kwargs):
    global blocked
    target = pathlib.Path(dst).absolute()
    if not blocked and target.parent == pathlib.Path(root).absolute() and target.name in ('AGENTS.md', 'CLAUDE.md'):
        blocked = True
        print('LOCK_HELD', flush=True)
        sys.stdin.readline()
    return real_replace(src, dst, *args, **kwargs)
os.replace = wait_once
sys.argv = [script, 'build', '--root', root]
runpy.run_path(script, run_name='__main__')
'''
        first = subprocess.Popen([sys.executable, '-c', wrapper, str(SCRIPT), str(self.root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        try:
            import select
            readable, _, _ = select.select([first.stdout], [], [], 10)
            self.assertTrue(readable, 'first writer did not reach replacement')
            self.assertEqual(first.stdout.readline().strip(), 'LOCK_HELD')
            before = snapshot(self.root)
            second = subprocess.run([sys.executable, str(SCRIPT), 'build', '--root', str(self.root)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
            self.assertEqual(snapshot(self.root), before)
            stdout, stderr = first.communicate('continue\n', timeout=10)
            self.assertEqual(first.returncode, 0, stdout + stderr)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()
        self.command('check')

    def test_07_gitignore_preserves_lines_and_ignores_private_files(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.put(self.root / '.gitignore', '# user ignore\nlocal-user-data/\n')
        self.put(self.root / 'README.md', 'User README stays byte-for-byte.\n')
        self.put(self.root / 'docs/design.md', 'Existing design.\n')
        self.command('apply')
        self.assertIn('# user ignore\nlocal-user-data/\n', (self.root / '.gitignore').read_text())
        self.assertEqual((self.root / 'README.md').read_text(), 'User README stays byte-for-byte.\n')
        self.assertEqual((self.root / 'docs/design.md').read_text(), 'Existing design.\n')
        for path in ['.AI/project.md', '.AI/memory/MEMORY.md', '.AI/canon/canon.state.json']:
            result = subprocess.run(['git', '-C', str(self.root), 'check-ignore', '-q', path])
            self.assertEqual(result.returncode, 0, path)

    def test_07_previously_tracked_private_destination_refused(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        path = self.root / '.AI/project.md'
        self.put(path, 'tracked private')
        subprocess.run(['git', '-C', str(self.root), 'add', '.AI/project.md'], check=True)
        path.unlink()
        path.parent.rmdir()
        self.reject_unchanged()


if __name__ == '__main__':
    unittest.main()
