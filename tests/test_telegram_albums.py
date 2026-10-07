"""Blind document-album acceptance; synthetic files/client only, no auth/network."""
import asyncio
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_inputs import tgs, tgs_one, RecordingClient


class Captured(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1


class Albums(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = []
        for i in range(11):
            p = self.root / ('synthetic-%s.pdf' % i)
            p.write_bytes(b'synthetic document' + bytes([i]))
            self.files.append(str(p))

    def exercise(self, route, files, *, cli=False, send=True, caption='Caption _raw_',
                 result=None, send_error=False, pace_error=False, disconnect_error=False,
                 blocked=False, skip=False, **changes):
        module = tgs if route == 'label' else tgs_one
        core = module if route == 'label' else module.tgs
        output, error = Captured(), io.StringIO()
        events, calls = [], []
        ids = [73101, 73102] if result is None else result

        class Client(RecordingClient):
            instances = []
            username = 'synthetic'

            async def send_file(self, entity, file, **kwargs):
                calls.append(('file', file, kwargs))
                if send_error:
                    raise RuntimeError('PRIVATE_ERROR_SENTINEL')
                if isinstance(file, list):
                    return [SimpleNamespace(id=i) for i in ids]
                return SimpleNamespace(id=73101, date=None, message=caption)

            async def send_message(self, entity, text, **kwargs):
                calls.append(('text', text, kwargs))
                return SimpleNamespace(id=73101, date=None, message=text)

            async def disconnect(self):
                events.append('disconnect')
                if disconnect_error:
                    raise RuntimeError('PRIVATE_ERROR_SENTINEL')

        async def connect(client, **kwargs):
            events.append('connect')

        async def disconnect(client):
            await client.disconnect()

        def guard(account, entity, bypass, **kwargs):
            events.append(('guard', bypass))
            return 0 if bypass or not blocked else 3

        def record(*args, **kwargs):
            events.append(('record', output.getvalue(), output.flushes))
            if pace_error:
                raise OSError('PRIVATE_ERROR_SENTINEL')

        args = SimpleNamespace(to='synthetic', chat_id='111', account='default',
                               username='synthetic', text=caption, file=files, send=send,
                               topic=777, reply_to=888, silent=True, no_pace_check=skip,
                               schedule=None, schedule_tz=None, exact_minute=False,
                               html=False, voice=False, remind=False)
        for name, value in changes.items():
            setattr(args, name, value)
        with contextlib.ExitStack() as stack:
            for name, value in {
                'TelegramClient': Client, 'connect_with_retry': connect,
                'disconnect_quietly': core.disconnect_quietly if disconnect_error else disconnect,
                'client_kwargs': lambda auth: {},
                'load_auth': lambda account='default': events.append('auth') or {
                    'session_name': 'synthetic', 'api_id': 1, 'api_hash': 'synthetic'},
                'load_project_config': lambda: {'chats': {'synthetic': core.chat_entry(111)}},
                'pace_guard': guard, 'pace_record': record,
                'pace_check': lambda *a, **k: (0, 0),
                'PACE_STATE_PATH': self.root / 'pace.json',
            }.items():
                stack.enter_context(patch.object(core, name, value))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(error))
            try:
                if cli:
                    argv = ['synthetic'] + (['--to', 'synthetic'] if route == 'label' else ['111'])
                    argv += ['--text', caption, '--topic', '777', '--reply-to', '888', '--silent']
                    if send:
                        argv.append('--send')
                    if skip:
                        argv.append('--no-pace-check')
                    for f in files or []:
                        argv += ['--file', f]
                    stack.enter_context(patch.object(sys, 'argv', argv))
                    code = module.main()
                else:
                    code = asyncio.run(module.amain(args))
            except SystemExit as exc:
                code = exc.code
            except Exception as exc:
                code = ('escaped', type(exc).__name__)
        return code, output.getvalue(), error.getvalue(), calls, events, len(Client.instances)

    def each_route(self):
        return ('label', 'explicit')

    def test_repeated_file_cli_preserves_order_one_album(self):
        for route in self.each_route():
            with self.subTest(route=route):
                rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], cli=True)
                self.assertEqual(rc, 0, (out, err))
                self.assertEqual(len(calls), 1, 'TARGET: one send call for whole album')
                self.assertEqual(calls[0][1], self.files[:2], 'TARGET: repeated --file must preserve all files')
                self.assertEqual(clients, 1)
                self.assertEqual(events.count('connect'), 1)
                kwargs = calls[0][2]
                self.assertTrue(kwargs['force_document'])
                self.assertIsNone(kwargs['parse_mode'])
                self.assertTrue(kwargs['silent'])
                self.assertEqual(kwargs['reply_to'], 888)
                captions = kwargs['caption']
                self.assertIsInstance(captions, list)
                self.assertEqual(captions[0], 'Caption _raw_')
                self.assertTrue(all(not v for v in captions[1:]))
                records = [e for e in events if isinstance(e, tuple) and e[0] == 'record']
                self.assertEqual(len(records), 1)
                for i in [73101, 73102]:
                    self.assertIn(str(i), records[0][1], 'TARGET: IDs emitted before pace record')
                self.assertGreater(records[0][2], 0, 'TARGET: IDs flushed before side effects')

    def test_amain_album_and_ten_boundary(self):
        for route in self.each_route():
            with self.subTest(route=route):
                ids = list(range(73101, 73111))
                rc, out, err, calls, events, clients = self.exercise(route, self.files[:10], result=ids)
                self.assertEqual(rc, 0, (out, err))
                self.assertEqual(calls[0][1], self.files[:10])
                for i in ids:
                    self.assertIn(str(i), out)

    def test_invalid_packages_refused_before_auth(self):
        link = self.root / 'hardlink.pdf'
        os.link(self.files[0], link)
        symlink = self.root / 'alias.pdf'
        symlink.symlink_to(self.files[0])
        packages = [[self.files[0], ''], [self.files[0], str(self.root / 'missing')],
                    [self.files[0], str(self.root)], self.files[:11],
                    [self.files[0], self.files[0]], [self.files[0], str(link)],
                    [self.files[0], str(symlink)]]
        for route in self.each_route():
            for files in packages:
                with self.subTest(route=route, files=files):
                    rc, out, err, calls, events, clients = self.exercise(route, files)
                    self.assertEqual(rc, 2, (out, err))
                    self.assertNotIn('auth', events, 'TARGET: full validation before auth')
                    self.assertEqual(clients, 0)
                    self.assertEqual(calls, [])
                    self.assertTrue(err.strip())

    def test_caption_utf16_limit_and_encoding(self):
        for route in self.each_route():
            for caption in ['😀' * 513, '\ud800']:
                with self.subTest(route=route, caption_kind=len(caption)):
                    rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], caption=caption)
                    self.assertEqual(rc, 2, (out, err))
                    self.assertNotIn('auth', events)
                    self.assertEqual(calls, [])
            rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], caption='😀' * 512)
            self.assertEqual(rc, 0, (out, err))

    def test_unreadable_album_file_before_auth(self):
        original_open = Path.open
        unreadable = Path(self.files[1])

        def file_open(path, *args, **kwargs):
            if path == unreadable:
                raise PermissionError('synthetic unreadable file')
            return original_open(path, *args, **kwargs)

        original_access = os.access
        with patch.object(Path, 'open', file_open), patch.object(
                os, 'access', lambda path, *a, **k: False if Path(path) == unreadable
                else original_access(path, *a, **k)):
            for route in self.each_route():
                rc, out, err, calls, events, clients = self.exercise(route, self.files[:2])
                self.assertEqual(rc, 2, (out, err))
                self.assertNotIn('auth', events)
                self.assertEqual(calls, [])

    def test_unsupported_album_modes_before_auth(self):
        from datetime import datetime, timedelta, timezone
        for route in self.each_route():
            modes = [{'schedule': datetime.now(timezone.utc) + timedelta(days=1)}]
            if route == 'explicit':
                modes += [{'voice': True}, {'html': True}]
            for mode in modes:
                with self.subTest(route=route, mode=mode):
                    rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], **mode)
                    self.assertEqual(rc, 2, (out, err))
                    self.assertNotIn('auth', events)
                    self.assertEqual(calls, [])

    def test_dry_run_exact_package_without_send(self):
        for route in self.each_route():
            rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], cli=True, send=False)
            self.assertEqual(rc, 0, (out, err))
            self.assertEqual(calls, [])
            self.assertIn(self.files[0], out, 'TARGET: dry-run must show first file too')
            self.assertIn(self.files[1], out)
            self.assertLess(out.index(self.files[0]), out.index(self.files[1]))
            for f in self.files[:2]:
                self.assertIn(str(Path(f).stat().st_size), out)
            for value in ['777', '888', 'default', 'Caption _raw_']:
                self.assertIn(value, out)

    def test_bad_or_partial_ack_unknown_without_ok_and_one_record(self):
        for route in self.each_route():
            for result in [[], [73101], [73101, 73101], [True, 73102], [0, 73102], [-1, 73102], ['73101', 73102], [73101, 73102, 73103]]:
                with self.subTest(route=route, result=result):
                    rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], result=result, cli=True)
                    self.assertEqual(rc, 4, (out, err))
                    self.assertNotIn('OK:', out)
                    self.assertEqual(len(calls), 1, 'TARGET: no auto retry after unknown')
                    self.assertEqual(len([e for e in events if isinstance(e, tuple) and e[0] == 'record']), 1)
                    if result == [73101]:
                        self.assertIn('73101', out, 'TARGET: partial returned ID remains visible')

    def test_send_exception_unknown_safe_no_retry(self):
        for route in self.each_route():
            rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], send_error=True, cli=True)
            self.assertEqual(rc, 4, (out, err))
            self.assertEqual(len(calls), 1)
            self.assertNotIn('PRIVATE_ERROR_SENTINEL', out + err)
            self.assertIn('RuntimeError', out + err)
            self.assertNotIn('OK:', out)

    def test_postsend_failures_keep_ids_unknown(self):
        for route in self.each_route():
            for fault in [{'pace_error': True}, {'disconnect_error': True}]:
                with self.subTest(route=route, fault=fault):
                    rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], cli=True, **fault)
                    self.assertEqual(rc, 4, (out, err))
                    for i in [73101, 73102]:
                        self.assertIn(str(i), out)
                    self.assertNotIn('PRIVATE_ERROR_SENTINEL', out + err)
                    self.assertEqual(len(calls), 1)

    def test_pacing_block_and_authorized_bypass(self):
        for route in self.each_route():
            rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], blocked=True)
            self.assertEqual(rc, 3, (out, err))
            self.assertEqual(calls, [])
            rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], blocked=True, skip=True)
            self.assertEqual(rc, 0, (out, err))
            self.assertEqual(len(calls), 1)

    def test_legacy_str_none_and_single_list_regression(self):
        for route in self.each_route():
            for files in [self.files[0], [self.files[0]], None, []]:
                with self.subTest(route=route, files=files):
                    rc, out, err, calls, events, clients = self.exercise(route, files)
                    self.assertEqual(rc, 0, (out, err))
                    self.assertEqual(len(calls), 1)
                    if files:
                        self.assertEqual(calls[0][1], self.files[0])
                    else:
                        self.assertEqual(calls[0][0], 'text')

    def test_existing_str_none_single_controls(self):
        for route in self.each_route():
            for file in [self.files[0], None]:
                rc, out, err, calls, events, clients = self.exercise(route, file)
                self.assertEqual(rc, 0, (out, err))
                self.assertEqual(len(calls), 1)

    def test_single_voice_html_controls(self):
        for mode in [{'voice': True}, {'html': True}]:
            rc, out, err, calls, events, clients = self.exercise('explicit', self.files[0], **mode)
            self.assertEqual(rc, 0, (out, err))
            self.assertEqual(calls[0][2]['voice_note'], bool(mode.get('voice')))
            self.assertEqual(calls[0][2]['parse_mode'], 'html' if mode.get('html') else None)

    def test_topic_routing_and_recipient_guard(self):
        for route in self.each_route():
            rc, out, err, calls, events, clients = self.exercise(route, self.files[:2], reply_to=None)
            self.assertEqual(rc, 0, (out, err))
            self.assertEqual(calls[0][2]['reply_to'], 777)
        rc, out, err, calls, events, clients = self.exercise('explicit', self.files[:2], username='different')
        self.assertEqual(rc, 2, (out, err))
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
