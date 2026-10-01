"""FR-AIS07 pinned HTTPS acceptance with synthetic transport and no network."""
import contextlib
import io
import json
import runpy
import unittest
from unittest import mock
import urllib.error

from test_ai_sync import SCRIPT, SyncFixture, tree
from test_ai_bootstrap_transport import Response

BASE = 'https://raw.githubusercontent.com/example/toolkit/' + 'a' * 40


class AiSyncTransport(SyncFixture):
    def setUp(self):
        super().setUp()
        self.upstream('rules/coding.md', b'# Coding\r\nPINNED_UPSTREAM\r\n')
        self.files = {p.relative_to(self.bundle).as_posix(): p.read_bytes()
                      for p in self.bundle.rglob('*') if p.is_file()}
        self.calls = []
        self.main = runpy.run_path(str(SCRIPT), run_name='sync_transport_test')['main']

    def response(self, url, **kwargs):
        self.assertTrue(url.startswith(BASE + '/'), url)
        self.assertGreater(kwargs.get('timeout', 0), 0)
        relative = url[len(BASE) + 1:]
        self.assertIn(relative, self.files, 'unregistered source requested')
        self.calls.append(relative)
        return Response(self.files[relative], url)

    def invoke(self, verb='plan', extra=(), opener=None, source=BASE):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [verb, '--root', str(self.root), '--source-base', source] + list(extra)
        with mock.patch('urllib.request.urlopen', side_effect=opener or self.response):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = self.main(args)
                except SystemExit as error:
                    code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_07_pinned_source_plan_and_apply_preserve_exact_bytes(self):
        before = tree(self.root)
        code, out, err = self.invoke()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(tree(self.root), before)
        plan = json.loads(out)
        required = set(self.sources) | {'manifest.yaml', 'templates/ai/START.md'}
        self.assertTrue(required <= set(self.calls))
        code, out, err = self.invoke('apply', ['--expect-plan', plan['plan_sha256']])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.destination('rules/coding.md').read_bytes(), self.files['rules/coding.md'])
        self.assertIn(BASE, json.dumps(self.read_json(self.state_path)['source']))
        self.cli('check')

    def test_07_each_package_download_failure_before_any_writes(self):
        for failed in list(self.sources) + ['manifest.yaml', 'templates/ai/START.md']:
            with self.subTest(source=failed):
                before = tree(self.root)
                def unavailable(url, **kwargs):
                    if url == BASE + '/' + failed:
                        raise urllib.error.URLError('synthetic source unavailable')
                    return self.response(url, **kwargs)
                code, out, err = self.invoke(opener=unavailable)
                self.assertNotEqual(code, 0, out + err)
                self.assertEqual(tree(self.root), before)

    def test_07_redirects_refused(self):
        for target in [BASE.replace('a' * 40, 'b' * 40), 'https://example.invalid/untrusted',
                       BASE.replace('https:', 'http:')]:
            with self.subTest(target=target):
                before = tree(self.root)
                def redirect(url, **kwargs):
                    response = self.response(url, **kwargs)
                    if url.endswith('/rules/coding.md'):
                        response.url = target + '/rules/coding.md'
                    return response
                code, out, err = self.invoke(opener=redirect)
                self.assertNotEqual(code, 0, out + err)
                self.assertEqual(tree(self.root), before)

    def test_07_unpinned_sources_refused_before_network(self):
        for source in [BASE.replace('a' * 40, 'main'), BASE.replace('https:', 'http:'),
                       BASE.replace('a' * 40, 'shortsha'), BASE + '?token=fixture']:
            with self.subTest(source=source):
                before = tree(self.root)
                with mock.patch('urllib.request.urlopen', side_effect=AssertionError('unpinned source fetched')):
                    # invoke overrides urlopen, so pass the same refusing opener explicitly.
                    code, out, err = self.invoke(source=source,
                        opener=lambda *args, **kw: self.fail('unpinned source fetched'))
                self.assertNotEqual(code, 0, out + err)
                self.assertEqual(tree(self.root), before)

    def test_07_oversized_manifest_and_source_refused(self):
        for oversized in ['manifest.yaml', 'rules/coding.md']:
            with self.subTest(source=oversized):
                before = tree(self.root)
                def response(url, **kwargs):
                    if url == BASE + '/' + oversized:
                        return Response(b'x' * (20 * 1024 * 1024), url)
                    return self.response(url, **kwargs)
                code, out, err = self.invoke(opener=response)
                self.assertNotEqual(code, 0, out + err)
                self.assertEqual(tree(self.root), before)


if __name__ == '__main__':
    unittest.main()
