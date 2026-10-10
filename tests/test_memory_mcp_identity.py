"""Blind MI05 protocol tests: synthetic modules, no installed memory or services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'memory-mcp.py'
PID = '12345678-1234-4234-8234-123456789abc'
OTHER = '87654321-4321-4321-8321-cba987654321'
SCOPE = {'must': [{'key': 'project_id', 'match': {'value': PID}}]}


class MemoryIdentityMCP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memory-identity-mcp-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = self.root / 'stub.py'
        self.marker = self.root / 'calls.jsonl'

    def stub(self, version=2, resolver='ok', payload='ok', empty=False):
        resolved = {'status': 'ok', 'project': 'Eng', 'project_id': PID, 'filter': SCOPE}
        if resolver == 'extra': resolved['extra'] = 'untrusted'
        if resolver == 'wide': resolved['filter'] = {'should': SCOPE['must']}
        if resolver == 'wrong-id': resolved['project_id'] = OTHER
        if resolver == 'other-display': resolved['project'] = 'Canonical'
        if resolver == 'empty-display': resolved['project'] = ''
        if resolver == 'noncanonical': resolved['project_id'] = PID.upper()
        if resolver == 'long-display': resolved['project'] = 'Eng' + 'x' * 300
        if resolver in ('ambiguous_project', 'unknown_project', 'scope_unavailable',
                        'unsupported_scope', 'migration_required', 'arbitrary_secret_code'):
            resolved = {'status': 'error', 'error': resolver}
        point = {'score': .75, 'payload': {'project': 'Eng', 'project_id': PID,
                 'identity_schema': 2, 'relative_path': 'fact.md', 'file': 'fact.md',
                 'path': '/synthetic/fact.md', 'description': 'fact', 'text': 'body'}}
        if payload == 'wrong-id': point['payload']['project_id'] = OTHER
        if payload == 'long-id': point['payload']['project_id'] = PID + 'x'
        if payload == 'missing-id': del point['payload']['project_id']
        if payload == 'other-display': point['payload']['project'] = 'Other-copy'
        if payload == 'empty-display': point['payload']['project'] = ''
        if payload == 'long-display': point['payload']['project'] = 'Eng' + 'x' * 300
        if version in (None, 1):
            point['payload'].pop('project_id', None)
        source = '''import json, socket, urllib.request
COLLECTION = 'identity_fixture'
MARKER = %r
RESOLVED = %r
POINTS = %r
def forbidden(*a, **k): raise AssertionError('live network forbidden')
socket.create_connection = forbidden
urllib.request.urlopen = forbidden
def api_key(): return forbidden()
def record(kind, **kw):
    with open(MARKER, 'a') as f: f.write(json.dumps(dict(kind=kind, **kw)) + '\\n')
def resolve_memory_scope(project=None, project_id=None):
    record('resolve', project=project, project_id=project_id)
    return RESOLVED
def embed(texts):
    record('embed', texts=texts)
    return [[.1, .2]]
def bm25_query(query):
    record('bm25')
    return {'indices': [1], 'values': [.5]}
def qdrant(path, method='GET', body=None, quiet=False):
    record('qdrant', path=path, method=method, body=body)
    return {'result': {'points': POINTS, 'next_page_offset': None}}
''' % (str(self.marker), resolved, [] if empty else [point])
        if version is not None: source += '\nMEMORY_SCOPE_VERSION = %r\n' % version
        if resolver == 'missing': source += '\ndel resolve_memory_scope\n'
        if resolver == 'noncallable': source += '\nresolve_memory_scope = 1\n'
        if resolver == 'raise': source += '\ndef resolve_memory_scope(**kw): raise RuntimeError("PRIVATE_PATH_SHOULD_NOT_ESCAPE")\n'
        self.backend.write_text(source)
        self.marker.unlink(missing_ok=True)

    def rpc(self, arguments):
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                     'params': {'name': 'memory_search', 'arguments': arguments}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}]
        proc = subprocess.run([sys.executable, str(SCRIPT), '--backend', str(self.backend),
                               '--timeout', '5'], input=''.join(json.dumps(m) + '\n' for m in messages),
                              capture_output=True, text=True, timeout=12, cwd=self.root,
                              env={**os.environ, 'TMPDIR': tempfile.gettempdir()})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies], [1, 2])
        self.assertIn('result', replies[1])
        self.assertNotIn('PRIVATE_PATH_SHOULD_NOT_ESCAPE', proc.stdout + proc.stderr)
        reply = replies[0]
        if 'error' in reply:
            return {'status': 'error', 'error': reply['error']['code']}
        return json.loads(reply['result']['content'][0]['text'])

    def calls(self):
        return [json.loads(line) for line in self.marker.read_text().splitlines()] if self.marker.exists() else []

    def assert_no_search(self):
        self.assertFalse([c for c in self.calls() if c['kind'] in ('embed', 'bm25', 'qdrant')])

    def test_legacy_missing_or_one_version_preserves_result(self):
        for version in (None, 1):
            with self.subTest(version=version):
                self.stub(version)
                self.assertEqual(self.rpc({'query': 'x', 'project': 'Eng'}),
                    {'status': 'ok', 'project': 'Eng', 'matches': [{'score': .75, 'project': 'Eng',
                     'file': 'fact.md', 'path': '/synthetic/fact.md', 'description': 'fact', 'text': 'body'}]})
                self.assertNotIn('resolve', [c['kind'] for c in self.calls()])

    def test_tool_schema_advertises_both_selectors(self):
        self.stub()
        proc = subprocess.run([sys.executable, str(SCRIPT), '--backend', str(self.backend)],
            input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n',
            capture_output=True, text=True, timeout=8, cwd=self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        schema = json.loads(proc.stdout)['result']['tools'][0]['inputSchema']
        self.assertEqual(set(schema['properties']), {'query', 'project', 'project_id', 'limit'})
        self.assertEqual(schema['properties']['project_id']['type'], 'string')
        self.assertIn('query', schema['required'])
        self.assertNotIn('project', schema['required'])
        self.assertNotIn('project_id', schema['required'])
        self.assertIs(schema['additionalProperties'], False)
        def accepts(node, keys):
            if isinstance(node, bool): return node
            if not set(node.get('required', ())).issubset(keys): return False
            if 'allOf' in node and not all(accepts(n, keys) for n in node['allOf']): return False
            if 'anyOf' in node and not any(accepts(n, keys) for n in node['anyOf']): return False
            if 'oneOf' in node and sum(accepts(n, keys) for n in node['oneOf']) != 1: return False
            if 'not' in node and accepts(node['not'], keys): return False
            return True
        for keys, expected in [({'query'}, False), ({'query', 'project'}, True),
                               ({'query', 'project_id'}, True), ({'query', 'project', 'project_id'}, False)]:
            self.assertIs(accepts(schema, keys), expected, keys)

    def test_legacy_explicit_id_is_unsupported_without_search(self):
        for version in (None, 1):
            with self.subTest(version=version):
                self.stub(version)
                value = self.rpc({'query': 'x', 'project_id': PID})
                self.assertEqual(value.get('error'), 'unsupported_scope', value)
                self.assert_no_search()

    def test_new_selector_validation_before_worker(self):
        for args in ({'query': 'x'}, {'query': 'x', 'project': 'Eng', 'project_id': PID},
                     *[{'query': 'x', 'project_id': bad} for bad in ('', 'no-uuid', True, 4, None)]):
            with self.subTest(args=args):
                self.stub()
                self.assertEqual(self.rpc(args)['status'], 'error')
                self.assertEqual(self.calls(), [], 'invalid selectors must not start worker backend')

    def test_v2_exact_scope_in_all_hybrid_branches(self):
        for selector in ({'project': 'Eng'}, {'project_id': PID}):
            with self.subTest(selector=selector):
                self.stub()
                value = self.rpc({'query': 'x', **selector})
                self.assertEqual(value['status'], 'ok')
                self.assertEqual((value.get('project'), value.get('project_id')), ('Eng', PID))
                self.assertEqual(value['matches'][0]['project_id'], PID)
                calls = self.calls()
                self.assertEqual(calls[0]['kind'], 'resolve')
                self.assertEqual(calls[0].get('project'), selector.get('project'))
                self.assertEqual(calls[0].get('project_id'), selector.get('project_id'))
                query = next(c for c in calls if c['kind'] == 'qdrant')['body']
                self.assertEqual(query['filter'], SCOPE)
                self.assertEqual(query['query'], {'fusion': 'rrf'})
                self.assertEqual(len(query['prefetch']), 2)
                for branch in query['prefetch']: self.assertEqual(branch['filter'], SCOPE)

    def test_known_empty_is_stable_success(self):
        self.stub(empty=True)
        self.assertEqual(self.rpc({'query': 'x', 'project_id': PID}),
                         {'status': 'ok', 'project': 'Eng', 'project_id': PID, 'matches': []})

    def test_resolver_allowlist_preserves_errors_without_search(self):
        for code in ('ambiguous_project', 'unknown_project', 'scope_unavailable',
                     'unsupported_scope', 'migration_required'):
            with self.subTest(code=code):
                self.stub(resolver=code)
                self.assertEqual(self.rpc({'query': 'x', 'project': 'Eng'}).get('error'), code)
                self.assert_no_search()

    def test_invalid_capabilities_and_resolver_shapes_fail_closed(self):
        for version, mode in [(v, 'ok') for v in (0, 3, True, '2')] + [(2, m) for m in
                ('missing', 'noncallable', 'extra', 'wide', 'empty-display', 'noncanonical',
                 'long-display', 'raise', 'arbitrary_secret_code')]:
            with self.subTest(version=version, mode=mode):
                self.stub(version=version, resolver=mode)
                self.assertEqual(self.rpc({'query': 'x', 'project': 'Eng'}).get('error'), 'backend_failure')
                self.assert_no_search()

    def test_explicit_id_cannot_be_rebound_by_resolver(self):
        self.stub(resolver='wrong-id')
        self.assertEqual(self.rpc({'query': 'x', 'project_id': PID}).get('error'), 'backend_failure')
        self.assert_no_search()

    def test_retrieved_identity_and_display_validated_before_truncation(self):
        for mode in ('wrong-id', 'long-id', 'missing-id', 'empty-display', 'long-display'):
            with self.subTest(mode=mode):
                self.stub(payload=mode)
                self.assertEqual(self.rpc({'query': 'x', 'project_id': PID}).get('error'), 'backend_failure')

    def test_shared_id_copy_labels_are_metadata(self):
        self.stub(resolver='other-display', payload='other-display')
        value = self.rpc({'query': 'x', 'project': 'Eng'})
        self.assertEqual(value.get('status'), 'ok', value)
        self.assertEqual(value.get('project_id'), PID)
        self.assertEqual(value.get('project'), 'Canonical')
        self.assertEqual(value['matches'][0]['project'], 'Other-copy')
        self.assertEqual(value['matches'][0]['project_id'], PID)

    def test_trusted_backend_rewrite_ignores_same_stat_stale_bytecode(self):
        self.stub(version=1)
        # Seed a real initial cache during the first protocol request, including
        # when the adapter itself deliberately avoids writing bytecode caches.
        original = ('import py_compile\npy_compile.compile(__file__, doraise=True)\n' +
                    self.backend.read_text().replace("'text': 'body'", "'text': 'fresh_A'"))
        rewritten = original.replace("'text': 'fresh_A'", "'text': 'fresh_B'")
        self.assertNotEqual(original, rewritten)
        self.assertEqual(len(original.encode()), len(rewritten.encode()))
        fixed_mtime = 1_700_000_000
        self.backend.write_text(original)
        os.utime(self.backend, (fixed_mtime, fixed_mtime))
        first_stat = self.backend.stat()
        first = self.rpc({'query': 'x', 'project': 'Eng'})
        self.assertEqual(first.get('status'), 'ok', first)
        self.assertEqual(first['matches'][0]['text'], 'fresh_A')
        self.assertTrue(list(self.root.glob('__pycache__/stub.*.pyc')), 'fixture must seed initial bytecode')
        self.backend.write_text(rewritten)
        os.utime(self.backend, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
        second_stat = self.backend.stat()
        self.assertEqual((second_stat.st_size, second_stat.st_mtime_ns),
                         (first_stat.st_size, first_stat.st_mtime_ns))
        second = self.rpc({'query': 'x', 'project': 'Eng'})
        self.assertEqual(second.get('status'), 'ok', second)
        self.assertEqual(second['matches'][0]['text'], 'fresh_B', 'current trusted source must win over stale .pyc')


if __name__ == '__main__':
    unittest.main()
