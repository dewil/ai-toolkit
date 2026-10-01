"""SDD-FIN-05: black-box contract for automatic isolated test execution."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts/run-tests.py'


class SddRunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(RUNNER.is_file(), 'scripts/run-tests.py implementation is required')
        self.temp = tempfile.TemporaryDirectory(prefix='sdd runner ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tests = self.root / 'project with spaces' / 'tests with spaces'
        self.tests.mkdir(parents=True)
        self.cwd = self.root / 'unrelated working directory'
        self.cwd.mkdir()

    def put(self, name, source):
        path = self.tests / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        return path

    def invoke(self, *args, script=RUNNER):
        return subprocess.run([sys.executable, str(script), *map(str, args)],
                              cwd=self.cwd, capture_output=True, text=True, timeout=20)

    def snapshot(self):
        return {p.relative_to(self.tests).as_posix(): p.read_bytes()
                for p in self.tests.rglob('*') if p.is_file()}

    def test_new_files_discovered_in_sorted_isolated_processes_with_expected_python_and_cwd(self):
        source = '''import builtins, json, os, sys
assert not hasattr(builtins, '_sdd_process_marker')
builtins._sdd_process_marker = True
print('CHILD ' + json.dumps({'name': os.path.basename(__file__), 'pid': os.getpid(),
                            'python': sys.executable, 'cwd': os.getcwd()}), flush=True)
'''
        self.put('test_z_new.py', source)
        self.put('test_a_first.py', source)
        self.put('helper.py', "raise RuntimeError('helper must not run')\n")
        self.put('nested/test_not_direct.py', "raise RuntimeError('nested must not run')\n")
        (self.tests / 'test_directory.py').mkdir()
        before = self.snapshot()
        result = self.invoke('--tests-dir', self.tests)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = [json.loads(line[6:]) for line in result.stdout.splitlines() if line.startswith('CHILD ')]
        self.assertEqual([r['name'] for r in records], ['test_a_first.py', 'test_z_new.py'])
        self.assertEqual(len({r['pid'] for r in records}), 2)
        for record in records:
            self.assertEqual(Path(record['python']).resolve(), Path(sys.executable).resolve())
            self.assertEqual(Path(record['cwd']), self.tests.parent)
            self.assertNotEqual(record['pid'], os.getpid())
        self.assertEqual(self.snapshot(), before)
        self.put('test_m_added_later.py', source)
        result = self.invoke('--tests-dir', self.tests)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = [json.loads(line[6:]) for line in result.stdout.splitlines() if line.startswith('CHILD ')]
        self.assertEqual([r['name'] for r in records], ['test_a_first.py', 'test_m_added_later.py', 'test_z_new.py'])

    def test_failed_child_is_named_and_stops_later_tests_without_hiding_output(self):
        self.put('test_a_fail.py', "import sys\nprint('VISIBLE_STDOUT', flush=True)\nprint('VISIBLE_STDERR', file=sys.stderr, flush=True)\nsys.exit(7)\n")
        self.put('test_z_never.py', "print('LATER_TEST_MUST_NOT_RUN')\n")
        before = self.snapshot()
        result = self.invoke('--tests-dir', self.tests)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('VISIBLE_STDOUT', result.stdout)
        self.assertIn('VISIBLE_STDERR', result.stderr)
        self.assertIn('test_a_fail.py', result.stdout + result.stderr)
        self.assertNotIn('LATER_TEST_MUST_NOT_RUN', result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_absent_empty_and_no_matching_tests_are_configuration_errors(self):
        for state in ('absent', 'empty', 'nonmatching', 'file'):
            with self.subTest(state=state):
                directory = self.root / state
                if state == 'file':
                    directory.write_text('not a directory')
                elif state != 'absent':
                    directory.mkdir()
                    if state == 'nonmatching':
                        (directory / 'helper.py').write_text('print("not a test")')
                result = self.invoke('--tests-dir', directory)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertTrue((result.stdout + result.stderr).strip(), 'Invalid test directory needs a reason')
                if state == 'absent':
                    self.assertFalse(directory.exists())

    def test_matching_symlinks_are_errors_and_are_never_executed(self):
        outside = self.root / 'external.py'
        outside.write_text("print('EXTERNAL_SYMLINK_EXECUTED')\n")
        for target in (outside, self.root / 'absent.py'):
            with self.subTest(target=target.name):
                link = self.tests / 'test_link.py'
                link.symlink_to(target)
                try:
                    result = self.invoke('--tests-dir', self.tests)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertNotIn('EXTERNAL_SYMLINK_EXECUTED', result.stdout + result.stderr)
                    self.assertIn('test_link.py', result.stdout + result.stderr)
                    self.assertTrue(link.is_symlink())
                finally:
                    link.unlink()

    def test_default_directory_is_relative_to_runner_repository_not_callers_cwd(self):
        repository = self.root / 'relocated repository'
        (repository / 'scripts').mkdir(parents=True)
        (repository / 'tests').mkdir()
        copy = repository / 'scripts/run-tests.py'
        shutil.copy2(RUNNER, copy)
        (repository / 'tests/test_default.py').write_text("print('DEFAULT_REPO_TEST')\n")
        (self.cwd / 'tests').mkdir()
        (self.cwd / 'tests/test_wrong.py').write_text("raise RuntimeError('wrong default directory')\n")
        result = self.invoke(script=copy)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('DEFAULT_REPO_TEST', result.stdout)


if __name__ == '__main__':
    unittest.main()
