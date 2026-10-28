"""Coordinator safety probes, distinct from frozen blind criteria.

Synthetic filesystem/loopback only; production credentials are never read.
"""
import copy
import contextlib
import io
import sys
import http.server
import json
from pathlib import Path
import threading
import types
import unittest
from unittest import mock

import test_asana_native_dependencies as contract
SUBJECT, MIXED_HTML, TOKEN = contract.SUBJECT, contract.MIXED_HTML, contract.TOKEN
cfg, owned_state = contract.cfg, contract.owned_state


class NativeSafety(unittest.TestCase):
    def setUp(self):
        self.h = contract.NativeContractTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def mixed(self):
        self.h.api.tasks['20']['html_notes'] = MIXED_HTML
        self.h.api.set_edge('20', '21')
        return self.h.plan(cleanup=True)

    def args(self):
        config = self.h.directory / 'config-root' / 'cfg.json'
        config.parent.mkdir()
        config.write_text('{}')
        return types.SimpleNamespace(state=str(self.h.state_path),
            plan=str(self.h.directory / 'plan.json'), config=str(config),
            send=False, expect_plan=None, cleanup_text=False)

    def test_cleanup_post_readback_loss_is_error_and_pending(self):
        plan = self.mixed()
        original = self.h.api.__call__
        def corrupt(method, path, token, payload=None):
            result = original(method, path, token, payload)
            if method == 'PUT':
                self.h.api.tasks['20']['html_notes'] = '<body>lost</body>'
            return result
        with mock.patch.object(SUBJECT, 'native_request', side_effect=corrupt):
            with self.assertRaises(SUBJECT.NativeError):
                self.h.apply(plan)
        mark = self.h.disk_state()['scopes']['1:10:100']['cleanup']['20']
        self.assertEqual(mark['state'], 'pending')

    def test_cleanup_reread_after_confirmation_preserves_new_manual_edit(self):
        plan = self.mixed()
        original = self.h.api.__call__
        reads = 0
        injected = False
        def edit_during_confirmation(method, path, token, payload=None):
            nonlocal reads, injected
            if method == 'GET' and path.split('?')[0] == '/tasks/21/dependents':
                reads += 1
                if reads == 3:
                    self.h.api.tasks['20']['html_notes'] += '<p>new manual edit</p>'
                    injected = True
            return original(method, path, token, payload)
        with mock.patch.object(SUBJECT, 'native_request', side_effect=edit_during_confirmation):
            with self.assertRaises(SUBJECT.NativeError) as caught:
                self.h.apply(plan)
        self.assertTrue(injected)
        self.assertEqual(caught.exception.code, 'drift')
        self.assertIn('new manual edit', self.h.api.tasks['20']['html_notes'])
        self.assertFalse(any(m == 'PUT' for m, _, _ in self.h.api.mutations))

    def test_cleanup_wrong_post_readback_task_does_not_become_verified(self):
        plan = self.mixed()
        original = self.h.api.__call__
        def wrong_task(method, path, token, payload=None):
            result = original(method, path, token, payload)
            if method == 'GET' and path.startswith('/tasks/20?') and any(m == 'PUT' for m, _, _ in self.h.api.mutations):
                result['data']['gid'] = '21'
            return result
        with mock.patch.object(SUBJECT, 'native_request', side_effect=wrong_task):
            with self.assertRaises(SUBJECT.NativeError):
                self.h.apply(plan)
        self.assertEqual(self.h.disk_state()['scopes']['1:10:100']['cleanup']['20']['state'], 'pending')

    def test_targeted_reread_malformed_metadata_is_safe_api_error(self):
        for field, value in (('projects', None), ('projects', [None]),
                             ('projects', [{'gid': None}]), ('workspace', None)):
            with self.subTest(field=field, value=value):
                plan = self.mixed()
                original = self.h.api.__call__
                reads, armed = 0, False
                def malformed(method, path, token, payload=None):
                    nonlocal reads, armed
                    result = original(method, path, token, payload)
                    if method == 'GET' and path.split('?')[0] == '/tasks/21/dependents':
                        reads += 1
                        if reads == 3:
                            armed = True
                    if armed and method == 'GET' and path.startswith('/tasks/20?'):
                        result['data'][field] = copy.deepcopy(value)
                    return result
                with mock.patch.object(SUBJECT, 'native_request', side_effect=malformed):
                    with self.assertRaises(SUBJECT.NativeError) as caught:
                        self.h.apply(plan)
                self.assertTrue(armed)
                self.assertEqual(caught.exception.code, 'api_data')
                self.assertFalse(any(m == 'PUT' for m, _, _ in self.h.api.mutations))

    def test_cleanup_timeout_keeps_pending_without_compensation(self):
        plan = self.mixed()
        self.h.api.failure = 'timeout'
        with self.assertRaises(SUBJECT.NativeError):
            self.h.apply(plan)
        self.assertEqual([m for m, _, _ in self.h.api.mutations], ['PUT'])
        self.assertEqual(self.h.disk_state()['scopes']['1:10:100']['cleanup']['20']['state'], 'pending')

    def test_cleanup_intent_failure_prevents_put(self):
        plan = self.mixed()
        self.h.fail_write_at = 1
        with self.assertRaises(SUBJECT.NativeError):
            self.h.apply(plan)
        self.assertEqual(self.h.api.mutations, [])

    def test_unknown_name_refuses_instead_of_removing_owned(self):
        self.h.seed(owned_state())
        self.h.api.set_edge('20', '21')
        with self.assertRaises(SUBJECT.NativeError):
            self.h.plan(cfg({'Renamed': ['B']}))
        self.assertEqual(self.h.api.mutations, [])

    def test_multihomed_task_cannot_remove_or_cleanup(self):
        self.h.seed(owned_state())
        self.h.api.set_edge('20', '21')
        self.h.api.tasks['20']['projects'].append({'gid': '200'})
        with self.assertRaises(SUBJECT.NativeError):
            self.h.plan(cfg({}))
        self.h.api.tasks['20']['html_notes'] = MIXED_HTML
        with self.assertRaises(SUBJECT.NativeError):
            self.h.plan(cleanup=True)
        self.assertEqual(self.h.api.mutations, [])

    def test_plan_file_cannot_overwrite_unrelated_file_or_lock(self):
        args = self.args()
        p = Path(args.plan)
        p.write_text('KEEP THIS DATA')
        with self.assertRaises(SUBJECT.NativeError):
            SUBJECT._native_cli('board', cfg(), TOKEN, args)
        self.assertEqual(p.read_text(), 'KEEP THIS DATA')
        unrelated = {'schema_version': 1, 'scope': contract.SCOPE, 'label': 'board', 'important_data': 'KEEP'}
        p.write_text(json.dumps(unrelated))
        with self.assertRaises(SUBJECT.NativeError):
            SUBJECT._native_cli('board', cfg(), TOKEN, args)
        self.assertEqual(json.loads(p.read_text()), unrelated)
        args.plan = args.state + '.lock'
        with self.assertRaises(SUBJECT.NativeError):
            SUBJECT._native_cli('board', cfg(), TOKEN, args)
        self.assertFalse(Path(args.plan).exists())

    def test_state_inside_another_ai_project_is_refused(self):
        project = self.h.directory / 'other'
        (project / '.AI').mkdir(parents=True)
        (project / '.AI/project.json').write_text('{}')
        with self.assertRaises(SUBJECT.NativeError):
            self.h.apply(self.h.plan(), path=project / 'state.json')
        self.assertEqual(self.h.api.mutations, [])

    def test_hardlinked_lock_is_not_accepted(self):
        target = self.h.directory / 'other.txt'
        target.write_text('KEEP')
        Path(str(self.h.state_path) + '.lock').hardlink_to(target)
        with self.assertRaises(SUBJECT.NativeError):
            self.h.apply(self.h.plan())
        self.assertEqual(target.read_text(), 'KEEP')
        self.assertEqual(self.h.api.mutations, [])

    def cli(self, args, mode, send=False, digest=None):
        argv = ['asana-blockers.py', '--config', args.config, '--board', 'board']
        if mode is not None:
            argv += ['--mode', mode, '--state', args.state, '--plan', args.plan]
        if send:
            argv += ['--send', '--expect-plan', digest]
        with mock.patch.object(SUBJECT.sys, 'argv', argv), mock.patch.object(
                SUBJECT, 'load_token', return_value=TOKEN), contextlib.redirect_stdout(
                io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return SUBJECT.main()

    def test_cli_native_preview_then_bound_apply(self):
        args = self.args()
        Path(args.config).write_text(json.dumps({'board': cfg()}))
        self.assertEqual(self.cli(args, 'native'), 0)
        self.assertEqual(self.h.api.mutations, [])
        plan = json.loads(Path(args.plan).read_text())
        self.assertEqual(self.cli(args, 'native'), 0)
        self.assertEqual(json.loads(Path(args.plan).read_text()), plan)
        self.assertEqual(self.cli(args, 'native', True, plan['digest']), 0)
        self.assertEqual(self.h.api.deps['20'], {'21'})

    def test_cli_auto_402_does_not_call_text_engine(self):
        args = self.args()
        Path(args.config).write_text(json.dumps({'board': cfg()}))
        self.assertEqual(self.cli(args, 'auto'), 0)
        plan = json.loads(Path(args.plan).read_text())
        self.h.api.failure = 402
        with mock.patch.object(SUBJECT, 'run_board', side_effect=AssertionError('text fallback')):
            self.assertEqual(self.cli(args, 'auto', True, plan['digest']), 1)
        self.assertEqual([m for m, _, _ in self.h.api.mutations], ['POST'])

    def test_cli_legacy_config_routes_only_to_existing_text_engine(self):
        args = self.args()
        legacy = {'board': {'project': '100', 'chains': {'A': ['B']}}}
        Path(args.config).write_text(json.dumps(legacy))
        with mock.patch.object(SUBJECT, 'run_board', return_value=(0, False)) as text:
            self.assertEqual(self.cli(args, None), 0)
        self.assertEqual(text.call_count, 1)
        self.assertEqual(self.h.api.calls, [])

    def test_nullable_or_missing_attachment_download_url_is_strict_not_failure(self):
        self.h.api.attachments['20'] = ['500']
        self.h.api.downloads['500'] = None
        self.assertEqual(self.h.plan()['add'], [['20', '21']])
        self.h.api.overrides[('GET', '/attachments/500?opt_fields=gid,download_url')] = {'data': {'gid': '500'}}
        self.assertEqual(self.h.plan()['add'], [['20', '21']])

    def test_inactive_uncertain_history_outside_board_does_not_block_add(self):
        state = owned_state('pending_add')
        state['scopes']['1:10:100']['edges'] = {'20>99': 'pending_add'}
        self.h.seed(state)
        plan = self.h.plan()
        self.assertEqual(plan['add'], [['20', '21']])
        self.assertEqual(plan['remove'], [])
        self.assertEqual(self.h.disk_state(), state)

    def test_wrong_attachment_url_type_or_gid_is_rejected(self):
        self.h.api.attachments['20'] = ['500']
        self.h.api.downloads['500'] = {'wrong': 'type'}
        with self.assertRaises(SUBJECT.NativeError):
            self.h.plan()
        key = ('GET', '/attachments/500?opt_fields=gid,download_url')
        self.h.api.overrides[key] = {'data': {'gid': '501', 'download_url': None}}
        with self.assertRaises(SUBJECT.NativeError):
            self.h.plan()
        self.assertEqual(self.h.api.mutations, [])

    def test_owned_present_edge_outside_board_cannot_be_removed(self):
        self.h.api.add_task('99', 'External')
        self.h.api.set_edge('20', '99')
        state = owned_state()
        state['scopes']['1:10:100']['edges'] = {'20>99': 'owned'}
        with self.assertRaises(SUBJECT.NativeError) as caught:
            self.h.plan(cfg({}), state=state)
        self.assertEqual(caught.exception.code, 'scope')
        self.assertEqual(self.h.api.mutations, [])

    def test_attachment_pagination_cannot_change_parent_filter(self):
        key = ('GET', '/attachments?parent=20&limit=100')
        self.h.api.overrides[key] = self.h.api.envelope([], {'uri':
            'https://app.asana.com/api/1.0/attachments?parent=21&limit=100&offset=x'})
        with self.assertRaises(SUBJECT.NativeError):
            self.h.plan()
        self.assertEqual(self.h.api.mutations, [])

    def test_incomplete_plan_or_json_array_has_safe_error(self):
        with self.assertRaises(SUBJECT.NativeError):
            self.h.apply({}, expected='0' * 64)
        args = self.args()
        args.send, args.expect_plan = True, '0' * 64
        Path(args.plan).write_text('[]')
        with self.assertRaises(SUBJECT.NativeError):
            SUBJECT._native_cli('board', cfg(), TOKEN, args)
        self.assertEqual(self.h.api.mutations, [])

    def test_dotdot_alias_cannot_bypass_cwd_guard(self):
        work = self.h.directory / 'work'
        other = self.h.directory / 'other'
        work.mkdir(); other.mkdir()
        with mock.patch.object(SUBJECT.Path, 'cwd', return_value=work):
            with self.assertRaises(SUBJECT.NativeError):
                SUBJECT._native_path(other / '..' / 'work' / 'state.json')

    def test_mixed_board_native_requires_selection_before_any_text_write(self):
        args = self.args()
        value = {'textboard': {'project': '100', 'chains': {}}, 'nativeboard': cfg()}
        Path(args.config).write_text(json.dumps(value))
        argv = ['asana-blockers.py', '--config', args.config, '--send']
        with mock.patch.object(SUBJECT.sys, 'argv', argv), mock.patch.object(
                SUBJECT, 'load_token', return_value=TOKEN), mock.patch.object(
                SUBJECT, 'run_board', return_value=(0, False)) as text, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                SUBJECT.main()
        text.assert_not_called()
        self.assertEqual(self.h.api.calls, [])

    def test_directory_fsync_failure_does_not_claim_atomic_rollback(self):
        p = self.h.directory / 'writer.json'
        state = owned_state()
        fsync = SUBJECT.os.fsync
        count = 0
        def fail_after_replace(fd):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic directory durability error')
            fsync(fd)
        with mock.patch.object(SUBJECT.os, 'fsync', side_effect=fail_after_replace):
            with self.assertRaises(SUBJECT.NativeError):
                SUBJECT.write_native_state(str(p), state)
        self.assertEqual(json.loads(p.read_text()), state)


class NativeTransport(unittest.TestCase):
    def test_redirect_does_not_send_authorization_to_other_origin(self):
        seen = []
        class Sink(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":{}}')
            def log_message(self, *_):
                pass
        sink = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        destination = 'http://127.0.0.1:%s/capture' % sink.server_port
        class Redirect(Sink):
            def do_GET(self):
                self.send_response(302); self.send_header('Location', destination); self.end_headers()
        source = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        threads = []
        for server in (sink, source):
            t = threading.Thread(target=server.serve_forever, daemon=True); t.start(); threads.append(t)
        try:
            with mock.patch.object(SUBJECT, 'API', 'http://127.0.0.1:%s' % source.server_port):
                with self.assertRaises(SUBJECT.NativeError) as caught:
                    SUBJECT.native_request('GET', '/users/me', TOKEN)
            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(seen, [])
            self.assertNotIn(TOKEN, str(caught.exception))
        finally:
            for server in (source, sink):
                server.shutdown(); server.server_close()
            for t in threads:
                t.join(timeout=2)


    def test_truncated_http_response_becomes_safe_native_error(self):
        class Truncated(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Length', '100'); self.end_headers()
                self.wfile.write(b'{"data":'); self.close_connection = True
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Truncated)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with mock.patch.object(SUBJECT, 'API', 'http://127.0.0.1:%s' % server.server_port):
                with self.assertRaises(SUBJECT.NativeError) as caught:
                    SUBJECT.native_request('GET', '/users/me', TOKEN)
            self.assertEqual(caught.exception.code, 'network')
            self.assertNotIn(TOKEN, str(caught.exception))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
