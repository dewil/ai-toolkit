"""Pinned HTTPS delivery contract, exercised without contacting the network."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ai-bootstrap.py'
BASE = 'https://raw.githubusercontent.com/example/toolkit/' + 'a' * 40
PROJECT_ID = '22aaab19-84df-42b0-9f1a-51aa4fbb3425'


class Response(io.BytesIO):
    def __init__(self, content, url):
        super().__init__(content)
        self.url = url


class BootstrapHttpsTransport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'client'
        self.root.mkdir()
        (self.root / 'README.md').write_bytes(b'user README\r\n')
        self.main = runpy.run_path(str(SCRIPT), run_name='bootstrap_transport_test')['main']
        self.rule = '# Mandatory\r\nПравило безопасности.\r\n'.encode()
        self.files = {
            'manifest.yaml': b'universal:\n  - rules/required.md\ncoding:\n  - scripts/example.py\n',
            'rules/required.md': self.rule,
            'scripts/example.py': b'#!/usr/bin/env python3\nprint("fixture")\n',
            'templates/ai/START.md': b'# Start\nRead installed rules.\n',
            'templates/ai/project.md': b'PRIVATE_PROJECT\n',
            'templates/ai/MEMORY.md': b'PRIVATE_MEMORY\n',
            'templates/ai/context-policy.json': json.dumps({
                'mandatory': ['rules/required.md'], 'max_bytes': 30000}).encode(),
        }
        self.calls = []

    def snapshot(self):
        return {p.relative_to(self.root).as_posix():
                ('dir',) if p.is_dir() else ('file', p.read_bytes())
                for p in self.root.rglob('*')}

    def response(self, url, **kwargs):
        self.assertTrue(url.startswith(BASE + '/'), url)
        self.assertGreater(kwargs.get('timeout', 0), 0)
        self.calls.append(url)
        relative = url[len(BASE) + 1:]
        self.assertIn(relative, self.files, 'unregistered source requested')
        return Response(self.files[relative], url)

    def invoke(self, command='apply', opener=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('urllib.request.urlopen', side_effect=opener or self.response):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = self.main([command, '--root', str(self.root), '--source-base', BASE,
                                    '--project-id', PROJECT_ID, '--types', 'coding',
                                    '--adapters', 'claude,codex,kimi'])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_pinned_https_apply_fetches_declared_files_and_preserves_bytes(self):
        result, stdout, stderr = self.invoke()
        self.assertEqual(result, 0, stdout + stderr)
        self.assertEqual(set(self.calls), {BASE + '/' + p for p in self.files})
        self.assertEqual((self.root / '.AI/rules/required.md').read_bytes(), self.rule)
        self.assertEqual((self.root / 'scripts/example.py').read_bytes(), self.files['scripts/example.py'])
        self.assertEqual((self.root / 'README.md').read_bytes(), b'user README\r\n')
        state = json.loads((self.root / '.AI/canon/canon.state.json').read_text())
        self.assertIn(BASE, json.dumps(state))
        for entry in ['AGENTS.md', 'CLAUDE.md']:
            content = (self.root / entry).read_bytes()
            self.assertNotIn(b'PRIVATE_PROJECT', content)
            self.assertNotIn(b'PRIVATE_MEMORY', content)

    def test_pinned_https_plan_downloads_complete_package_without_writes(self):
        before = self.snapshot()
        result, stdout, stderr = self.invoke('plan')
        self.assertEqual(result, 0, stdout + stderr)
        self.assertIsInstance(json.loads(stdout), dict)
        self.assertEqual(set(self.calls), {BASE + '/' + p for p in self.files})
        self.assertEqual(self.snapshot(), before)

    def test_download_failures_at_each_source_leave_project_untouched(self):
        for failed in self.files:
            with self.subTest(source=failed):
                before = self.snapshot()
                def unavailable(url, **kwargs):
                    if url == BASE + '/' + failed:
                        raise urllib.error.URLError('synthetic download failure')
                    return self.response(url, **kwargs)
                result, stdout, stderr = self.invoke(opener=unavailable)
                self.assertEqual(result, 2, stdout + stderr)
                self.assertTrue(stderr.strip())
                self.assertEqual(self.snapshot(), before)

    def test_redirected_response_is_refused_before_installation(self):
        for redirected in [BASE.replace('a' * 40, 'b' * 40),
                           'https://example.invalid/untrusted',
                           BASE.replace('https:', 'http:')]:
            with self.subTest(destination=redirected):
                before = self.snapshot()
                def redirect(url, **kwargs):
                    response = self.response(url, **kwargs)
                    if url.endswith('/rules/required.md'):
                        response.url = redirected + '/rules/required.md'
                    return response
                result, stdout, stderr = self.invoke(opener=redirect)
                self.assertEqual(result, 2, stdout + stderr)
                self.assertIn('redirect', stderr.lower())
                self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
