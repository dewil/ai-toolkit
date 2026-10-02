"""Independent FR-MTE01 tests; synthetic mountinfo, never mount live paths."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_ai_bootstrap as bootstrap
import test_ai_migrate as migration


INJECT = r'''
import json, runpy, sys
script, fixture, args = sys.argv[1:4]
ns = runpy.run_path(script, run_name='independent_mount_test')
main = ns['main']
target = main.__globals__['ab'] if 'ab' in main.__globals__ else None
data = json.loads(fixture)
def read_mountinfo():
    if data is None:
        raise OSError('independent mountinfo unavailable')
    return data
if target is None:
    main.__globals__['read_mountinfo'] = read_mountinfo
else:
    target.read_mountinfo = read_mountinfo
sys.exit(main(json.loads(args)))
'''


def kernel_escape(path):
    return str(path).replace('\\', '\\134').replace(' ', '\\040').replace('\t', '\\011').replace('\n', '\\012')


def mountinfo(path, options='ro', fs='tmpfs'):
    return '19 1 0:19 / ' + kernel_escape(path) + ' ' + options + ' - ' + fs + ' fixture ' + options + '\n'


class MigrationEnvironmentContract(unittest.TestCase):
    setUp = migration.AiMigrationContract.setUp
    put = migration.AiMigrationContract.put
    policy = migration.AiMigrationContract.policy
    write_registry = migration.AiMigrationContract.write_registry

    def execute(self, info, verb='plan', script=None, source=None):
        args = [verb, '--root', str(self.root)]
        if verb in ('plan', 'apply'):
            args += ['--bundle', str(source or self.bundle), '--project-id', bootstrap.PROJECT_ID,
                     '--types', 'coding,wiki', '--adapters', 'claude,codex,kimi']
        return subprocess.run([sys.executable, '-c', INJECT, str(script or migration.SCRIPT),
                               json.dumps(info), json.dumps(args)], capture_output=True, text=True)

    def blocked(self, info, verb='plan', path=None, source=None, script=None):
        before = bootstrap.snapshot(self.root)
        result = self.execute(info, verb, script, source)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        diagnostic = (result.stdout + result.stderr).lower()
        self.assertRegex(diagnostic, r'mount|environment|sandbox|сред', diagnostic)
        self.assertRegex(diagnostic, r'terminal|терминал', diagnostic)
        if path:
            self.assertIn(str(path).lower(), diagnostic)
        self.assertNotIn('traceback', diagnostic)
        self.assertEqual(bootstrap.snapshot(self.root), before)
        return result

    def test_relevant_exact_and_nested_mounts_block_all_operations(self):
        for relative in ['.agents', '.agents/skills', '.git', '.claude', 'CLAUDE.md',
                         'AGENTS.md', 'docs/dev', '.claude/memory/nested']:
            for verb in ['plan', 'apply', 'check', 'recover']:
                with self.subTest(path=relative, operation=verb):
                    path = self.root / relative
                    self.blocked(mountinfo(path), verb, path)

    def test_bind_and_rw_mount_are_also_environmental(self):
        for options, fs in [('rw', 'ext4'), ('ro', 'ext4'), ('rw', 'tmpfs')]:
            with self.subTest(options=options, fs=fs):
                path = self.root / '.claude/memory'
                self.blocked(mountinfo(path, options, fs), path=path)

    def test_escaped_space_backslash_root_and_nested_mount(self):
        moved = self.base / 'client space \\ literal'
        self.root.rename(moved)
        self.root = moved
        path = self.root / 'docs/dev/backlog'
        self.blocked(mountinfo(path), path=path)

    def test_root_ancestor_and_unrelated_mounts_allow_readonly_plan(self):
        info = (mountinfo('/', 'rw', 'overlay') + mountinfo(self.base, 'rw', 'ext4')
                + mountinfo(self.root, 'rw', 'ext4') + mountinfo(self.root / 'unrelated', 'ro'))
        before = bootstrap.snapshot(self.root)
        result = self.execute(info)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertEqual(bootstrap.snapshot(self.root), before)

    def test_unreadable_empty_and_malformed_fail_before_source_or_inventory(self):
        self.put(self.root / '.claude/canon.yaml', 'not a valid registry')
        missing_source = self.base / 'source-unavailable'
        for info in [None, '', 'malformed mountinfo\n',
                     '19 1 0:19 / /tmp rw -\n',
                     '19 1 0:19 / /tmp\\999bad rw - tmpfs tmpfs rw\n']:
            for verb in ['plan', 'apply', 'check', 'recover']:
                with self.subTest(info=info, operation=verb):
                    self.blocked(info, verb, source=missing_source)

    def test_environment_precedes_missing_package_and_unknown_registry(self):
        self.put(self.root / '.claude/canon.yaml', '{"schema_version":999}')
        path = self.root / '.agents'
        self.blocked(mountinfo(path), path=path, source=self.base / 'missing-package')

    def test_genuine_agents_keeps_ordinary_guard_and_user_bytes(self):
        self.put(self.root / '.agents/user-owned.txt', 'KEEP USER DATA')
        before = bootstrap.snapshot(self.root)
        result = self.execute(mountinfo('/', 'rw', 'overlay'))
        self.assertNotEqual(result.returncode, 0)
        diagnostic = (result.stdout + result.stderr).lower()
        self.assertIn('.agents', diagnostic)
        self.assertNotRegex(diagnostic, r'environment.*block|sandbox.*block|mountpoint')
        self.assertEqual(bootstrap.snapshot(self.root), before)

    def test_bootstrap_uses_same_environment_boundary(self):
        path = self.root / '.agents'
        for verb in ['plan', 'apply', 'check', 'recover']:
            with self.subTest(operation=verb):
                self.blocked(mountinfo(path), verb, path, script=bootstrap.SCRIPT)


if __name__ == '__main__':
    unittest.main()
