"""Independent memory CLI contract tests. All clients and facts are synthetic."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'memory-write.py'
PROJECT_ID = '22aaab19-84df-42b0-9f1a-51aa4fbb3425'
BODY = 'Synthetic private fact sentinel.\nSecond line: Москва.\n'
DESCRIPTION = 'Synthetic private description sentinel'

# Inject faults at contractual stdlib publication boundaries, not private APIs.
FAULT_DRIVER = r'''
import builtins, os, pathlib, runpy, sys
mode, script, root = sys.argv[1:4]
sys.argv = [script] + sys.argv[4:]
original_replace = os.replace
memory = pathlib.Path(root) / '.AI' / 'memory'
def replacement(src, dst, *args, **kwargs):
    if pathlib.Path(dst).name == 'alpha.md':
        if mode == 'before':
            os._exit(71)
        result = original_replace(src, dst, *args, **kwargs)
        if mode == 'after':
            os._exit(72)
        if mode == 'edit':
            (memory / 'MEMORY.md').write_bytes(b'MANUAL_INDEX_CHANGE\n')
            (memory / 'hook-triggered').write_text('yes')
        return result
    return original_replace(src, dst, *args, **kwargs)
os.replace = replacement
if mode == 'unsupported':
    original_import = builtins.__import__
    def importer(name, *args, **kwargs):
        if name == 'fcntl':
            raise ImportError('synthetic unavailable flock')
        return original_import(name, *args, **kwargs)
    builtins.__import__ = importer
runpy.run_path(script, run_name='__main__')
'''


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux flock contract')
class MemoryWriteContract(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'memory-write CLI implementation is required')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'client'
        self.memory = self.root / '.AI' / 'memory'
        self.memory.mkdir(parents=True)
        (self.root / '.AI' / 'project.json').write_text(json.dumps({
            'schema_version': 1, 'layout_version': 'AI-LAYOUT-1',
            'project_id': PROJECT_ID}), encoding='utf-8')
        self.index = self.memory / 'MEMORY.md'
        self.original = '# Existing memory\n\n- [Existing](existing.md)\n<!-- Keep exactly -->'.encode()
        self.index.write_bytes(self.original)
        self.index.chmod(0o640)

    def args(self, name='alpha', description=DESCRIPTION, root=None):
        return ['--root', str(root or self.root), '--name', name,
                '--description', description]

    def run_cli(self, name='alpha', description=DESCRIPTION, body=BODY,
                root=None, fault=None):
        args = self.args(name, description, root)
        command = [sys.executable, str(SCRIPT)] + args
        if fault:
            command = [sys.executable, '-c', FAULT_DRIVER, fault,
                       str(SCRIPT), str(root or self.root)] + args
        return subprocess.run(command, input=body, text=True,
                              capture_output=True, timeout=15)

    def success(self, result, name='alpha'):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(set(data), {'status', 'filename'})
        self.assertIsInstance(data['status'], str)
        self.assertTrue(data['status'])
        self.assertEqual(Path(data['filename']).name, name + '.md')
        self.assertFalse(Path(data['filename']).is_absolute())
        self.assertNotIn('..', Path(data['filename']).parts)
        self.assertNotIn(BODY.strip(), result.stdout + result.stderr)
        self.assertNotIn(DESCRIPTION, result.stdout + result.stderr)
        return data

    def rejected(self, result):
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertTrue(output.strip(), 'failure must be diagnostic')
        self.assertNotIn(BODY.strip(), output)
        self.assertNotIn(DESCRIPTION, output)

    def pointers(self, name='alpha'):
        return re.findall(r'\]\((?:\./)?' + re.escape(name) + r'\.md\)',
                          self.index.read_text())

    def assert_complete(self, name='alpha', body=BODY):
        data = (self.memory / (name + '.md')).read_text()
        self.assertTrue(data.startswith('---\n'))
        self.assertIn('\n---\n', data[4:])
        self.assertIn('description:', data)
        self.assertTrue(data.endswith(body), data)
        self.assertEqual(len(self.pointers(name)), 1, 'lost or duplicated memory pointer')

    def test_cli_complete_unicode_and_preserves_index_bytes_and_modes(self):
        self.success(self.run_cli())
        self.assert_complete()
        self.assertTrue(self.index.read_bytes().startswith(self.original))
        self.assertEqual(stat.S_IMODE(self.index.stat().st_mode), 0o640)
        for name in ('alpha.md', '.write.lock'):
            self.assertEqual(stat.S_IMODE((self.memory / name).stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.memory.glob('*.md')),
                         ['MEMORY.md', 'alpha.md'])
        result = subprocess.run([sys.executable, str(SCRIPT), '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        for option in ('--root', '--name', '--description'):
            self.assertIn(option, result.stdout)

    def test_retry_and_conflict_do_not_modify_existing_artifacts(self):
        self.success(self.run_cli())
        fact = (self.memory / 'alpha.md').read_bytes()
        index = self.index.read_bytes()
        self.assertEqual(self.success(self.run_cli())['status'], 'unchanged')
        self.assertEqual((self.memory / 'alpha.md').read_bytes(), fact)
        self.assertEqual(self.index.read_bytes(), index)
        for kwargs in ({'body': BODY + 'Changed.\n'}, {'description': 'Changed description'}):
            self.rejected(self.run_cli(**kwargs))
            self.assertEqual((self.memory / 'alpha.md').read_bytes(), fact)
            self.assertEqual(self.index.read_bytes(), index)

    def test_existing_fact_without_pointer_is_repaired_once(self):
        self.success(self.run_cli())
        self.index.write_bytes(self.original)
        self.assertEqual(self.success(self.run_cli())['status'], 'index-repaired')
        self.assert_complete()
        self.assertEqual(self.success(self.run_cli())['status'], 'unchanged')
        self.assert_complete()

    def test_hostile_description_is_one_safe_link(self):
        description = 'Quote " : # [label](evil.md) \\ tail'
        self.success(self.run_cli(description=description))
        self.assert_complete()
        suffix = self.index.read_bytes()[len(self.original):].decode()
        self.assertEqual(len(suffix.strip().splitlines()), 1)
        self.assertNotIn('](evil.md)', suffix)
        frontmatter = (self.memory / 'alpha.md').read_text().split('\n---\n', 1)[0]
        self.assertEqual(frontmatter.count('\ndescription:'), 1)

    def test_invalid_inputs_leave_artifacts_unchanged(self):
        for kwargs in ({'name': '../escape'}, {'name': 'UPPER'}, {'name': 'a/b'},
                       {'name': ''}, {'name': 'a' * 101},
                       {'description': 'first\nsecond'}, {'description': 'first\rsecond'},
                       {'description': ''}, {'body': ''}, {'body': ' \n\t'}):
            with self.subTest(kwargs=kwargs):
                self.rejected(self.run_cli(**kwargs))
                self.assertEqual(self.index.read_bytes(), self.original)
                self.assertFalse((self.memory / 'alpha.md').exists())
                self.assertFalse((self.root / 'escape.md').exists())

    def test_invalid_root_config_and_missing_index_do_not_bootstrap(self):
        config = self.root / '.AI' / 'project.json'
        for content in ('not JSON', '{}', '{"project_id":"invalid"}',
                        '{"project_id":null}'):
            config.write_text(content)
            self.rejected(self.run_cli())
            self.assertEqual(self.index.read_bytes(), self.original)
        config.unlink()
        self.rejected(self.run_cli())
        missing = self.base / 'missing'
        self.rejected(self.run_cli(root=missing))
        self.assertFalse(missing.exists())
        config.write_text(json.dumps({'project_id': PROJECT_ID}))
        self.index.unlink()
        self.rejected(self.run_cli())
        self.assertFalse(self.index.exists())

    def test_internal_symlinks_are_rejected_without_external_writes(self):
        for relative in ('.AI', '.AI/memory', '.AI/memory/MEMORY.md',
                         '.AI/memory/alpha.md', '.AI/memory/.write.lock'):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                outside = Path(directory) / 'outside'
                path = self.root / relative
                backup = path.with_name(path.name + '.saved')
                directory_target = relative in ('.AI', '.AI/memory')
                if path.exists():
                    path.rename(backup)
                if directory_target:
                    outside.mkdir()
                    (outside / 'sentinel').write_bytes(b'OUTSIDE')
                else:
                    outside.write_bytes(b'OUTSIDE')
                path.symlink_to(outside, target_is_directory=directory_target)
                try:
                    self.rejected(self.run_cli())
                    if directory_target:
                        self.assertEqual(list(outside.iterdir()), [outside / 'sentinel'])
                    else:
                        self.assertEqual(outside.read_bytes(), b'OUTSIDE')
                finally:
                    path.unlink()
                    if backup.exists():
                        backup.rename(path)

    def test_nonregular_index_fact_and_lock_are_rejected(self):
        for name in ('MEMORY.md', 'alpha.md', '.write.lock'):
            with self.subTest(path=name):
                path = self.memory / name
                backup = path.with_name(name + '.saved')
                if path.exists():
                    path.rename(backup)
                path.mkdir()
                try:
                    self.rejected(self.run_cli())
                    self.assertEqual(list(path.iterdir()), [])
                finally:
                    path.rmdir()
                    if backup.exists():
                        backup.rename(path)

    def test_root_symlink_alias_is_accepted(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.success(self.run_cli(root=alias))
        self.assert_complete()

    def test_unavailable_flock_refuses_before_writes(self):
        self.rejected(self.run_cli(fault='unsupported'))
        self.assertEqual(self.index.read_bytes(), self.original)
        self.assertEqual(list(self.memory.iterdir()), [self.index])

    def test_crash_before_fact_and_retry(self):
        result = self.run_cli(fault='before')
        self.assertEqual(result.returncode, 71, 'publication hook did not trigger')
        self.assertFalse((self.memory / 'alpha.md').exists())
        self.assertEqual(self.index.read_bytes(), self.original)
        self.assertEqual(list(self.memory.glob('*.md')), [self.index])
        self.success(self.run_cli())
        self.assert_complete()

    def test_crash_after_complete_fact_and_retry_repairs_pointer(self):
        result = self.run_cli(fault='after')
        self.assertEqual(result.returncode, 72, 'publication hook did not trigger')
        self.assertTrue((self.memory / 'alpha.md').read_text().endswith(BODY))
        self.assertEqual(self.index.read_bytes(), self.original)
        self.assertEqual(self.success(self.run_cli())['status'], 'index-repaired')
        self.assert_complete()
        self.success(self.run_cli())
        self.assert_complete()

    def test_unexpected_manual_index_edit_is_not_overwritten(self):
        result = self.run_cli(fault='edit')
        self.assertTrue((self.memory / 'hook-triggered').exists(), 'CAS hook did not trigger')
        self.rejected(result)
        self.assertEqual(self.index.read_bytes(), b'MANUAL_INDEX_CHANGE\n')
        self.assertTrue((self.memory / 'alpha.md').read_text().endswith(BODY))
        self.assertEqual(self.success(self.run_cli())['status'], 'index-repaired')
        self.assertTrue(self.index.read_bytes().startswith(b'MANUAL_INDEX_CHANGE\n'))
        self.assert_complete()

    def test_writer_waits_on_shared_lock_then_reads_current_index(self):
        import fcntl
        lock = self.memory / '.write.lock'
        with lock.open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            process = subprocess.Popen([sys.executable, str(SCRIPT)] + self.args(),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            process.stdin.write(BODY)
            process.stdin.close()
            process.stdin = None
            time.sleep(0.25)
            self.assertIsNone(process.poll(), 'writer ignored shared flock')
            fresh = self.original + b'\n- [Fresh under lock](fresh.md)\n'
            self.index.write_bytes(fresh)
            fcntl.flock(held, fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=10)
        self.success(subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr))
        self.assertTrue(self.index.read_bytes().startswith(fresh), 'writer used stale index')
        self.assert_complete()

    def test_parallel_real_writers_preserve_every_unique_pointer(self):
        # Every launching session has already seen the old index. Helpers must reread
        # under their common lock, rather than trusting any earlier observation.
        launcher = r'''
import pathlib, subprocess, sys, time
script, root, name, gate, ready = sys.argv[1:]
(pathlib.Path(root) / '.AI/memory/MEMORY.md').read_bytes()
pathlib.Path(ready).touch()
while not pathlib.Path(gate).exists():
    time.sleep(.005)
result = subprocess.run([sys.executable, script, '--root', root, '--name', name,
                         '--description', 'Concurrent ' + name],
                        input='Body ' + name + '\n', text=True, capture_output=True)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
'''
        for iteration in range(3):
            gate = self.base / ('gate-' + str(iteration))
            processes = []
            for i in range(6):
                name = 'parallel-%d-%d' % (iteration, i)
                ready = self.base / (name + '.ready')
                process = subprocess.Popen([sys.executable, '-c', launcher, str(SCRIPT),
                                            str(self.root), name, str(gate), str(ready)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.addCleanup(lambda p=process: p.kill() if p.poll() is None else None)
                processes.append((name, ready, process))
            deadline = time.monotonic() + 10
            while not all(ready.exists() for _, ready, _ in processes):
                self.assertLess(time.monotonic(), deadline, 'launchers did not reach barrier')
                time.sleep(.01)
            gate.touch()
            for name, _, process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.success(subprocess.CompletedProcess(process.args, process.returncode,
                                                         stdout, stderr), name)
            self.assertTrue(self.index.read_bytes().startswith(self.original))
            for completed_iteration in range(iteration + 1):
                for i in range(6):
                    name = 'parallel-%d-%d' % (completed_iteration, i)
                    self.assert_complete(name, 'Body ' + name + '\n')


if __name__ == '__main__':
    unittest.main()
