"""Blind NSFS regressions from the task contract; synthetic mountinfo only.

Oracle: Linux v6.6.37 fs/nsfs.c ns_dname and namespace ns_ops names.
No live mount operations and no inspection of the production parser.
"""
import json
from pathlib import Path
import runpy
import subprocess
import unittest

import test_ai_bootstrap as bootstrap
import test_ai_migrate as migration
import test_ai_migration_environment as environment

kernel_escape = environment.kernel_escape
mountinfo = environment.mountinfo


NAMESPACE_NAMES = (
    'mnt', 'net', 'uts', 'ipc', 'pid', 'pid_for_children', 'user',
    'cgroup', 'time', 'time_for_children',
)


def namespace_mount(root='net:[4026531840]', point='/run/netns/synthetic', fs='nsfs'):
    return f'190 1 0:4 {root} {kernel_escape(point)} rw - {fs} nsfs rw\n'


class NamespaceMountParserContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Execute the script to access public seams; do not read its implementation.
        cls.api = runpy.run_path(str(bootstrap.SCRIPT), run_name='blind_nsfs_contract')

    def preflight(self, info, root=Path('/synthetic/client')):
        function = self.api['mount_preflight']
        original = function.__globals__['read_mountinfo']
        function.__globals__['read_mountinfo'] = lambda: info
        try:
            return function(root)
        finally:
            function.__globals__['read_mountinfo'] = original

    def test_kernel_namespace_names_and_unsigned_decimal_inode_allow_outside_mount(self):
        for name in NAMESPACE_NAMES:
            for inode in ('0', '4026531840', '18446744073709551615'):
                with self.subTest(name=name, inode=inode):
                    self.preflight(namespace_mount(f'{name}:[{inode}]'))

    def test_absolute_nsfs_root_remains_valid(self):
        self.preflight(namespace_mount('/'))
        self.preflight(namespace_mount('/absolute/root'))

    def test_namespace_root_requires_nsfs_filesystem(self):
        for filesystem in ('tmpfs', 'ext4', 'overlay'):
            with self.subTest(filesystem=filesystem):
                with self.assertRaises(self.api['Invalid']):
                    self.preflight(namespace_mount(fs=filesystem))

    def test_malformed_namespace_roots_fail_closed(self):
        for root in (
            'anything:[1]', 'net:[-1]', 'net:[+1]', 'net:[]', 'net:[1a]',
            'net:[1.0]', 'net:[١]', 'net:[1', 'net:1]', 'net:[1]suffix',
            'net:[1]/child', '../net:[1]', 'relative', 'NET:[1]',
            r'net:\1331\135', r'net:[1]\999', '/root/../escape',
        ):
            with self.subTest(root=root):
                with self.assertRaises(self.api['Invalid']):
                    self.preflight(namespace_mount(root))

    def test_mountpoint_is_absolute_even_for_valid_namespace_root(self):
        for point in ('relative', 'run/netns/synthetic', '../outside', 'net:[1]'):
            with self.subTest(point=point):
                with self.assertRaises(self.api['Invalid']):
                    self.preflight(namespace_mount(point=point))

    def test_malformed_fields_and_escapes_still_fail_closed(self):
        valid = namespace_mount()
        fixtures = (
            '', 'malformed\n', valid.replace('190 1 ', 'id 1 '),
            valid.replace('190 1 ', '190 parent '),
            valid.replace('0:4', 'bad:device'), valid.replace(' - ', ' '),
            valid.replace(' rw - ', ' - '),
            valid.replace('/run/netns/synthetic', r'/run/netns/\999bad'),
            valid.replace('/run/netns/synthetic', r'/run/netns/\041bad'),
            valid.replace('/run/netns/synthetic', '/run/../escape'),
            valid + 'broken second record\n',
        )
        for info in fixtures:
            with self.subTest(info=info):
                with self.assertRaises(self.api['Invalid']):
                    self.preflight(info)

    def test_allowed_kernel_path_escapes_do_not_break_namespace_mount(self):
        self.preflight(namespace_mount(point='/outside/space tab\tnewline\nslash\\literal'))

    def test_valid_namespace_mounts_still_protect_target_tree(self):
        root = Path('/synthetic/client')
        for relative in ('.agents', '.agents/skills', '.git', '.claude',
                         'AGENTS.md', 'CLAUDE.md', 'docs/dev'):
            point = root / relative
            with self.subTest(point=point):
                with self.assertRaises(self.api['Invalid']) as error:
                    self.preflight(namespace_mount(point=point), root)
                self.assertIn(str(point), str(error.exception))


class NamespaceMountConsumersContract(unittest.TestCase):
    setUp = migration.AiMigrationContract.setUp
    put = migration.AiMigrationContract.put
    policy = migration.AiMigrationContract.policy
    write_registry = migration.AiMigrationContract.write_registry
    execute = environment.MigrationEnvironmentContract.execute
    blocked = environment.MigrationEnvironmentContract.blocked

    def test_bootstrap_and_migrate_accept_unrelated_namespace_mount_on_plan(self):
        legacy = self.root
        for script in (bootstrap.SCRIPT, migration.SCRIPT):
            with self.subTest(script=Path(script).name):
                self.root = self.base / 'fresh-bootstrap' if script == bootstrap.SCRIPT else legacy
                self.root.mkdir(exist_ok=True)
                if script == bootstrap.SCRIPT:
                    subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
                before = bootstrap.snapshot(self.root)
                result = self.execute(namespace_mount(), script=script)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsInstance(json.loads(result.stdout), dict)
                self.assertEqual(bootstrap.snapshot(self.root), before)
        self.root = legacy

    def test_bootstrap_and_migrate_block_namespace_mount_at_protected_target(self):
        for script in (bootstrap.SCRIPT, migration.SCRIPT):
            for verb in ('plan', 'apply', 'check', 'recover'):
                with self.subTest(script=Path(script).name, verb=verb):
                    point = self.root / '.agents'
                    self.blocked(namespace_mount(point=point), verb, point, script=script)

    def test_bootstrap_plan_apply_check_in_ordinary_root_with_namespace_mount(self):
        self.root = self.base / 'ordinary-bootstrap'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        info = mountinfo('/', 'rw', 'overlay') + namespace_mount()
        for verb in ('plan', 'apply', 'check'):
            with self.subTest(verb=verb):
                result = self.execute(info, verb, script=bootstrap.SCRIPT)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertTrue((self.root / '.AI/project.json').is_file())


if __name__ == '__main__':
    unittest.main()
