"""Bounded package loading contract; synthetic bundles and mocked HTTPS only."""
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ai-bootstrap.py'
BASE = 'https://raw.githubusercontent.com/example/toolkit/' + 'a' * 40
PROJECT_ID = '22aaab19-84df-42b0-9f1a-51aa4fbb3425'


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url


class PackageBound(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'client'
        self.root.mkdir()
        self.bundle = self.base / 'bundle'
        self.bundle.mkdir()
        spec = importlib.util.spec_from_file_location('ai_bootstrap_package_bound_test', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.main = self.module.__dict__
        mandatory = ['rules/secrets-handling.md', 'rules/untrusted-content.md',
                     'rules/memory-persistence.md', 'rules/agent-portability.md']
        manifest = 'universal:\n' + ''.join('  - ' + p + '\n' for p in mandatory)
        manifest += 'coding:\n  - rules/coding.md\n  - scripts/helper.py\n'
        self.sources = {'manifest.yaml': manifest.encode(),
                        'rules/coding.md': b'# Coding\nUnique coding rule.\n',
                        'scripts/helper.py': b'H' * 3000,
                        'templates/ai/START.md': b'# Start\nRead .AI/project.md and .AI/memory/MEMORY.md.\n',
                        'templates/ai/project.md': b'PRIVATE_PROJECT_SENTINEL\n',
                        'templates/ai/MEMORY.md': b'PRIVATE_MEMORY_SENTINEL\n'}
        self.sources.update({p: ('# ' + p + '\nMandatory contract text.\n').encode() for p in mandatory})
        self.sources['templates/ai/context-policy.json'] = json.dumps({
            'mandatory': mandatory, 'max_bytes': 2048}).encode()
        for name, data in self.sources.items():
            self.put(self.bundle / name, data)

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def limits(self, package, file_limit=4096):
        # Patch the loader's module globals with deliberately small limits.
        self.main['main'].__globals__['MAX_PACKAGE'] = package
        self.main['main'].__globals__['MAX_FILE'] = file_limit

    def invoke(self, command='plan', remote=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        calls = []
        def opener(url, **kwargs):
            calls.append(url)
            relative = url[len(BASE) + 1:]
            if relative not in self.sources:
                raise AssertionError('unexpected request: ' + relative)
            return Response(self.sources[relative], url)
        argv = [command, '--root', str(self.root)]
        if remote:
            argv += ['--source-base', BASE]
        else:
            argv += ['--bundle', str(self.bundle)]
        argv += ['--project-id', PROJECT_ID, '--types', 'coding', '--adapters', 'claude']
        with mock.patch('urllib.request.urlopen', side_effect=opener), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.main['main'](argv)
        return code, stdout.getvalue(), stderr.getvalue(), calls

    def test_rejects_aggregate_overflow_before_requesting_next_remote_file(self):
        self.limits(1)
        code, out, err, calls = self.invoke(remote=True)
        self.assertEqual(code, 2, out + err)
        self.assertEqual(calls, [BASE + '/manifest.yaml', BASE + '/rules/agent-portability.md'])
        self.assertFalse(any(p.exists() for p in self.root.iterdir()))

    def test_source_and_templates_share_one_package_budget(self):
        self.limits(20)
        # Sources consume the whole aggregate cap; templates must share that same cap.
        source_total = sum(len(v) for k, v in self.sources.items() if not k.startswith('templates/ai/') and k != 'manifest.yaml')
        self.limits(source_total)
        code, out, err, _ = self.invoke()
        self.assertEqual(code, 2, out + err)
        self.assertFalse(any(self.root.iterdir()))

    def test_exact_package_limit_is_allowed_and_valid_outputs_are_unchanged(self):
        total = sum(len(v) for k, v in self.sources.items() if k != 'manifest.yaml')
        self.limits(total)
        code, out, err, _ = self.invoke(command='apply')
        self.assertEqual(code, 0, out + err)
        self.assertEqual((self.root / '.AI/rules/coding.md').read_bytes(), self.sources['rules/coding.md'])
        self.assertEqual((self.root / '.AI/START.md').read_bytes(), self.sources['templates/ai/START.md'])
        self.assertEqual((self.root / '.AI/project.md').read_bytes(), self.sources['templates/ai/project.md'])
        self.assertEqual((self.root / '.AI/memory/MEMORY.md').read_bytes(), self.sources['templates/ai/MEMORY.md'])

    def test_source_file_limit_remains_independent(self):
        self.sources['scripts/helper.py'] = b'R' * 2049
        self.put(self.bundle / 'scripts/helper.py', b'R' * 2049)
        self.limits(10000, file_limit=2048)
        code, out, err, _ = self.invoke()
        self.assertEqual(code, 2, out + err)
        self.assertFalse(any(self.root.iterdir()))

    def test_rejected_remote_package_does_not_write_client(self):
        self.limits(1)
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        code, out, err, _ = self.invoke(command='apply', remote=True)
        self.assertEqual(code, 2, out + err)
        after = {p.relative_to(self.root).as_posix(): p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_oversize_regular_bundle_source_reads_at_most_file_limit_plus_one(self):
        self.limits(10000, file_limit=2048)
        path = self.bundle / 'scripts/helper.py'
        self.put(path, b'X' * 5000)
        real_open = Path.open
        reads = []
        class LimitedReader:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def read(self, size=-1):
                reads.append(size)
                return self.wrapped.read(size)
            def __enter__(self):
                self.wrapped.__enter__()
                return self
            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
        def tracked_open(file, *args, **kwargs):
            opened = real_open(file, *args, **kwargs)
            if Path(file) == path:
                return LimitedReader(opened)
            return opened
        with mock.patch.object(Path, 'open', tracked_open):
            code, out, err, _ = self.invoke()
        self.assertEqual(code, 2, out + err)
        self.assertTrue(reads, 'expected bounded read of source')
        self.assertTrue(all(size == 2049 for size in reads), reads)


if __name__ == '__main__':
    unittest.main()
