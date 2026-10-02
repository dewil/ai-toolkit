#!/usr/bin/env python3
"""Independent FR-VEC01..04 black-box CLI contract; stdlib, synthetic backend."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'memory-mcp.py'
TMP = '/data/ai-canon-deploy-tmp'


class MemoryMCPTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'FR-VEC01 missing feature: scripts/memory-mcp.py')
        self.tmp = tempfile.TemporaryDirectory(prefix='memory-mcp-test-', dir=TMP)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.backend = self.dir / 'backend.py'
        self.marker = self.dir / 'calls.jsonl'

    def run_rpc(self, messages, backend=None, timeout=30):
        backend = backend or self.backend
        result = subprocess.run([sys.executable, str(SCRIPT), '--backend', str(backend),
                                 '--timeout', str(timeout)], input=''.join(json.dumps(m) + '\n' for m in messages),
                                capture_output=True, text=True, cwd=self.dir,
                                env={**os.environ, 'TMPDIR': TMP}, timeout=timeout + 8)
        self.assertEqual(result.returncode, 0, 'stdio must exit cleanly at EOF: ' + result.stderr)
        try:
            responses = [json.loads(line) for line in result.stdout.splitlines()]
        except ValueError:
            self.fail('FR-VEC02 stdout contains non-JSON output')
        for response in responses:
            self.assertEqual(response.get('jsonrpc'), '2.0')
        return responses, result

    def request(self, method, params=None, rid=1):
        return {'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params or {}}

    def call(self, arguments, rid=1, name='memory_search'):
        return self.request('tools/call', {'name': name, 'arguments': arguments}, rid)

    def decoded(self, response):
        result = response['result']
        self.assertEqual(result['content'][0]['type'], 'text')
        return result, json.loads(result['content'][0]['text'])

    def assert_error(self, response):
        if 'error' in response:
            return
        result, value = self.decoded(response)
        self.assertTrue(result.get('isError'))
        self.assertEqual(value['status'], 'error')
        self.assertIsInstance(value['error'], str)
        self.assertIsInstance(value['message'], str)

    def test_initialize_versions_and_negotiation(self):
        for requested in ('2024-11-05', '2025-03-26', '2025-06-18', '2099-01-01'):
            with self.subTest(version=requested):
                responses, _ = self.run_rpc([self.request('initialize', {'protocolVersion': requested,
                    'capabilities': {}, 'clientInfo': {'name': 'blind-test', 'version': '1'}})])
                result = responses[0]['result']
                self.assertEqual(result['protocolVersion'], requested if requested != '2099-01-01' else '2025-06-18')
                self.assertIn('tools', result['capabilities'])
                self.assertIn('name', result['serverInfo'])

    def test_tool_surface_and_notifications(self):
        responses, _ = self.run_rpc([{'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'method': 'ping'}, self.request('tools/list', rid=7), self.request('ping', rid=8)])
        self.assertEqual([r['id'] for r in responses], [7, 8])
        tools = responses[0]['result']['tools']
        self.assertEqual([t['name'] for t in tools], ['memory_search'])
        schema = tools[0]['inputSchema']
        self.assertEqual(schema['type'], 'object')
        self.assertEqual(set(schema['properties']), {'query', 'project', 'limit'})
        self.assertEqual(set(schema['required']), {'query', 'project'})
        self.assertIs(schema['additionalProperties'], False)
        self.assertEqual(schema['properties']['query']['type'], 'string')
        self.assertEqual(schema['properties']['query']['maxLength'], 4000)
        self.assertEqual(schema['properties']['project']['type'], 'string')
        self.assertEqual(schema['properties']['project']['maxLength'], 256)
        self.assertEqual(schema['properties']['limit']['type'], 'integer')
        self.assertEqual(schema['properties']['limit']['minimum'], 1)
        self.assertEqual(schema['properties']['limit']['maximum'], 10)

    def test_invalid_arguments_and_main_survival(self):
        valid = {'query': 'remember', 'project': 'Eng'}
        invalid = [{}, {'query': 'remember'}, {'project': 'Eng'},
            *[{**valid, 'query': v} for v in ('', '   ', 'a' * 4001, 3, None)],
            *[{**valid, 'project': v} for v in ('', '  ', 'a' * 257, 'Eng\n', 4, None)],
            *[{**valid, 'limit': v} for v in (True, False, 0, 11, 1.5, '5', None)],
            *[{**valid, key: 'injected'} for key in ('backend', 'url', 'command', 'path', 'model')]]
        for args in invalid:
            with self.subTest(args=args):
                responses, _ = self.run_rpc([self.call(args), self.request('ping', rid=2)])
                self.assertEqual([r['id'] for r in responses], [1, 2])
                self.assert_error(responses[0])
                self.assertIn('result', responses[1])
        self.assertFalse(self.marker.exists(), 'invalid requests must not invoke backend')

    def test_unknown_method_and_tool(self):
        for request in (self.request('admin/reindex'), self.call({'query': 'x', 'project': 'Eng'}, name='index')):
            responses, _ = self.run_rpc([request, self.request('ping', rid=2)])
            self.assert_error(responses[0])
            self.assertIn('result', responses[1])

    def test_cli_requires_trusted_backend_and_bounded_timeout(self):
        for args in ([], ['--backend', 'relative.py'], ['--backend', str(self.backend), '--timeout', '0'],
                     ['--backend', str(self.backend), '--timeout', '61'],
                     ['--backend', str(self.backend), '--timeout', '1.5']):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(SCRIPT), *args], input='', capture_output=True,
                                        text=True, timeout=5, env={**os.environ, 'TMPDIR': TMP})
                self.assertNotEqual(result.returncode, 0)

    def synthetic(self, mode='ok'):
        payload = {'project': 'Eng', 'file': 'fact.md', 'path': '/metadata/never-read',
                   'description': 'known fact', 'text': 'hello'}
        points = [{'score': 0.75, 'payload': payload}]
        if mode == 'empty': points = []
        if mode == 'mismatch': payload['project'] = 'Other'
        if mode == 'missing': del payload['text']
        if mode == 'scalar': payload['text'] = 7
        if mode == 'boolscore': points[0]['score'] = True
        if mode == 'nan': points[0]['score'] = float('nan')
        if mode == 'long':
            for field in ('file', 'path', 'description', 'text'): payload[field] = 'я' * 5000
        source = """import json, sys, time
COLLECTION = 'synthetic'
MARKER = %r
MODE = %r
POINTS = %r
SECRET = 'FAKE_TEST_KEY_DO_NOT_LEAK_42'
def record(kind, **kwargs):
    with open(MARKER, 'a') as f: f.write(json.dumps(dict(kind=kind, **kwargs)) + '\\n')
def embed(texts):
    record('embed', texts=texts)
    if MODE == 'sleep': time.sleep(4)
    if MODE == 'exit': sys.exit(SECRET)
    if MODE == 'raise': raise RuntimeError('HTTP BODY ' + SECRET)
    if MODE == 'oversize': print(SECRET * 15000)
    if MODE == 'noise':
        print(SECRET)
        print(SECRET, file=sys.stderr)
    return [[0.1, 0.2]]
def bm25_query(query):
    record('bm25', query=query)
    return {'indices': [1], 'values': [0.5]}
def qdrant(path, method='GET', body=None, quiet=False):
    record('qdrant', path=path, method=method, body=body)
    if MODE == 'malformed': return {'result': {}}
    return {'result': {'points': POINTS}}
def log_query(*args, **kwargs):
    record('forbidden_log_query')
    raise AssertionError('must not write query logs')
""" % (str(self.marker), mode, points)
        # repr(nan) is not valid Python literal without this alias.
        self.backend.write_text('nan = float("nan")\n' + source)

    def test_search_exact_filter_hybrid_and_readonly(self):
        self.synthetic()
        client = self.dir / 'client'
        client.mkdir()
        fact = client / 'fact.md'
        fact.write_text('authoritative fresh fact')
        before = {p.relative_to(client): p.read_bytes() for p in client.rglob('*') if p.is_file()}
        responses, _ = self.run_rpc([self.call({'query': 'remember', 'project': 'Eng', 'limit': 2})])
        result, value = self.decoded(responses[0])
        self.assertFalse(result.get('isError', False))
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(value['project'], 'Eng')
        self.assertEqual(value['matches'], [{'score': 0.75, 'project': 'Eng', 'file': 'fact.md',
            'path': '/metadata/never-read', 'description': 'known fact', 'text': 'hello'}])
        calls = [json.loads(line) for line in self.marker.read_text().splitlines()]
        self.assertEqual([c['kind'] for c in calls].count('embed'), 1)
        self.assertEqual(next(c for c in calls if c['kind'] == 'embed')['texts'], ['remember'])
        self.assertEqual(next(c for c in calls if c['kind'] == 'bm25')['query'], 'remember')
        q = next(c for c in calls if c['kind'] == 'qdrant')
        self.assertEqual(q['path'], '/collections/synthetic/points/query')
        self.assertEqual(q['method'], 'POST')
        self.assertEqual(q['body']['limit'], 2)
        filters = []
        def walk(obj):
            if isinstance(obj, dict):
                if obj.get('key') == 'project': filters.append(obj)
                if 'limit' in obj: self.assertTrue(type(obj['limit']) is int and 2 <= obj['limit'] <= 40)
                for v in obj.values(): walk(v)
            elif isinstance(obj, list):
                for v in obj: walk(v)
        walk(q['body'])
        self.assertTrue(filters, 'missing exact project filter')
        for f in filters: self.assertEqual(f['match'], {'value': 'Eng'})
        self.assertEqual(q['body']['query'], {'fusion': 'rrf'})
        self.assertEqual(len(q['body']['prefetch']), 2)
        self.assertNotIn('forbidden_log_query', [c['kind'] for c in calls])
        self.assertEqual(before, {p.relative_to(client): p.read_bytes() for p in client.rglob('*') if p.is_file()})

    def test_empty_is_success_and_default_limit(self):
        self.synthetic('empty')
        responses, _ = self.run_rpc([self.call({'query': 'x', 'project': 'Eng'})])
        result, value = self.decoded(responses[0])
        self.assertFalse(result.get('isError', False))
        self.assertEqual(value, {'status': 'ok', 'project': 'Eng', 'matches': []})
        calls = [json.loads(x) for x in self.marker.read_text().splitlines()]
        self.assertEqual(next(c for c in calls if c['kind'] == 'qdrant')['body']['limit'], 5)

    def test_bad_payload_never_becomes_empty(self):
        for mode in ('mismatch', 'missing', 'scalar', 'boolscore', 'nan', 'malformed'):
            with self.subTest(mode=mode):
                self.synthetic(mode)
                responses, _ = self.run_rpc([self.call({'query': 'x', 'project': 'Eng'}), self.request('ping', rid=2)])
                self.assertTrue(responses[0]['result'].get('isError'))
                self.assert_error(responses[0])
                self.assertIn('result', responses[1])

    def test_output_caps_unicode(self):
        self.synthetic('long')
        responses, _ = self.run_rpc([self.call({'query': 'x', 'project': 'Eng'})])
        result, value = self.decoded(responses[0])
        self.assertFalse(result.get('isError', False))
        match = value['matches'][0]
        self.assertEqual(set(match), {'score', 'project', 'file', 'path', 'description', 'text'})
        for field, cap in {'file': 256, 'path': 2048, 'description': 1024, 'text': 4000}.items():
            self.assertLessEqual(len(match[field]), cap)
            self.assertTrue(match[field])
            self.assertEqual(set(match[field]), {'я'})

    def test_sensitive_failures_import_exit_and_main_survival(self):
        for mode in ('raise', 'exit', 'noise', 'oversize', 'import'):
            with self.subTest(mode=mode):
                self.synthetic(mode)
                if mode == 'import': self.backend.write_text("raise RuntimeError('FAKE_TEST_KEY_DO_NOT_LEAK_42 HTTP BODY')\n")
                responses, process = self.run_rpc([self.call({'query': 'x', 'project': 'Eng'}), self.request('ping', rid=2)])
                self.assertNotIn('FAKE_TEST_KEY_DO_NOT_LEAK_42', process.stdout + process.stderr)
                self.assertNotIn('Traceback', process.stdout + process.stderr)
                self.assertTrue(responses[0]['result'].get('isError'))
                self.assert_error(responses[0])
                self.assertIn('result', responses[1])

    def test_worker_timeout_is_bounded_and_main_survives(self):
        self.synthetic('sleep')
        start = time.monotonic()
        responses, _ = self.run_rpc([self.call({'query': 'x', 'project': 'Eng'}), self.request('ping', rid=2)], timeout=1)
        self.assertLess(time.monotonic() - start, 3)
        self.assertTrue(responses[0]['result'].get('isError'))
        self.assert_error(responses[0])
        self.assertIn('result', responses[1])

    def test_notification_tool_never_executes_backend(self):
        self.synthetic()
        notification = self.call({'query': 'x', 'project': 'Eng'})
        del notification['id']
        responses, _ = self.run_rpc([notification, {'jsonrpc': '2.0', 'method': 'unknown'}, self.request('ping')])
        self.assertEqual(len(responses), 1)
        self.assertFalse(self.marker.exists())

    def test_malformed_protocol_and_duplicate_keys(self):
        cases = [('not-json', -32700),
                 ('{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}', -32700),
                 ('[]', -32600), ('{"jsonrpc":"1.0","id":1,"method":"ping"}', -32600)]
        for raw, code in cases:
            with self.subTest(raw=raw):
                process = subprocess.run([sys.executable, str(SCRIPT), '--backend', str(self.backend)],
                    input=raw + '\n' + json.dumps(self.request('ping', rid=3)) + '\n',
                    capture_output=True, text=True, timeout=5, env={**os.environ, 'TMPDIR': TMP})
                responses = [json.loads(x) for x in process.stdout.splitlines()]
                self.assertEqual(responses[0]['error']['code'], code)
                self.assertEqual(responses[1]['id'], 3)
        responses, _ = self.run_rpc([self.request('ping'), self.request('ping')])
        self.assertEqual([r['id'] for r in responses], [1, 1])

    def test_oversize_line_ends_connection_without_echo(self):
        raw = 'FAKE_TEST_KEY_DO_NOT_LEAK_42' + 'x' * 65537
        process = subprocess.run([sys.executable, str(SCRIPT), '--backend', str(self.backend)],
            input=raw + '\n' + json.dumps(self.request('ping')) + '\n', capture_output=True,
            text=True, timeout=5, env={**os.environ, 'TMPDIR': TMP})
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, '')
        self.assertNotIn('FAKE_TEST_KEY_DO_NOT_LEAK_42', process.stderr)


if __name__ == '__main__':
    unittest.main()
