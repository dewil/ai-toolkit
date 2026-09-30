"""Регрессии ревью: проверки без сети, только stdlib."""
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error

import test_telegram_inputs as tg
import test_redmine as rm
import test_mymeet_pull_guard as mm
import test_chrome_cookies as cc
import test_session_cost as sc

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewFixes(unittest.TestCase):
    def test_empty_normalized_username_refused_before_client(self):
        for username in ('', '@', '@@@'):
            for send in (False, True):
                with self.subTest(username=username, send=send):
                    cls = tg.client_class()
                    with tg.patched_env(tg.tgs_one.tgs, cls), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(asyncio.run(tg.tgs_one.amain(
                            tg.send_one_args(username=username, send=send))), 2)
                    self.assertEqual(cls.instances, [])
            cls = tg.client_class()
            with tg.patched_env(tg.tgp.tgs, cls), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(asyncio.run(tg.tgp.amain(1, '/tmp/mirror', username)), 2)
            self.assertEqual(cls.instances, [])

    def test_prefixed_usernames_match_in_both_scripts(self):
        cls = tg.client_class(username='Somebody')
        with tg.patched_env(tg.tgs_one.tgs, cls), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(tg.tgs_one.amain(tg.send_one_args(username='@@somebody'))), 0)
        with tempfile.TemporaryDirectory() as tmp, tg.patched_env(tg.tgp.tgs, cls), \
                mock.patch.object(tg.tgp.tgs, 'process_chat', mock.AsyncMock(return_value=(0, None))) as pull, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(tg.tgp.amain(1, tmp, '@somebody')), 0)
            pull.assert_awaited_once()

    def test_redmine_access_lost_is_not_closed_or_retried(self):
        for status in (403, 404):
            with self.subTest(status=status):
                p = rm._Project()
                self.addCleanup(p.close)
                p.put(p.snap, rm._snapshot({2551: [rm._issue(101, 2551, 'Task')]}))
                m = p.load_snapshot_module()
                m.load_auth = lambda: dict(redmine_url=rm.URL, api_key='k', use_curl=False)
                m.fetch_user_issues = lambda *args: []
                error = urllib.error.HTTPError(rm.URL, status, 'denied', {}, None)
                with mock.patch.object(m, 'fetch_json', side_effect=error) as fetch, \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(m.main(), 0)
                    data = json.loads(p.snap.read_text())
                    self.assertEqual(data['access_lost'], [101])
                    self.assertTrue(all(not u['issues'] for u in data['users'].values()))
                    code, output = p.deltas()
                    self.assertEqual(code, 0)
                    self.assertIn('Выбыли из доступа', output)
                    for wrong in ('Закрыты', 'Статус неизвестен', 'Изменений нет'):
                        self.assertNotIn(wrong, output)
                    self.assertEqual(m.main(), 0)
                    fetch.assert_called_once()

    def test_redmine_curl_preserves_http_status(self):
        m = load('redmine-snapshot')
        for status in (403, 404):
            with mock.patch.object(m.subprocess, 'run', return_value=mock.Mock(stdout=f'{{}}\n{status}'.encode())):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    m.fetch_json(rm.URL, 'k', True)
                self.assertEqual(error.exception.code, status)

    def test_unknown_assignee_comes_from_previous_issue(self):
        p = rm._Project()
        self.addCleanup(p.close)
        for previous_uid in (2551, None):
            p.put(p.prev, rm._snapshot({2551: [rm._issue(101, previous_uid, 'Task')]}))
            issue = dict(rm._issue(101, 2982, 'Task'), status_unknown=True)
            p.put(p.snap, rm._snapshot({2982: [issue]}))
            code, output = p.deltas()
            self.assertEqual(code, 0)
            self.assertIn('был у Иванов', output)
            self.assertNotIn('None', output)
            self.assertNotIn('был у Петров', output)

    def test_mymeet_bad_curl_status_is_oserror(self):
        with mock.patch.object(mm.mymeet.subprocess, 'run', return_value=mock.Mock(stdout=b'bad reply')):
            with self.assertRaisesRegex(OSError, 'некорректный ответ curl'):
                mm.mymeet._curl_bytes({'api_key': 'k'}, 'https://example.invalid', 30)

    def test_mymeet_pull_place_meeting_401_returns_2(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                mm.mymeet, 'place_meeting', side_effect=urllib.error.HTTPError(
                    'https://example.invalid', 401, 'Unauthorized', {}, None)) as place:
            code, _ = mm.run(Path(tmp), ['--pull', 'm1'], iter([mm.meeting('m1', 'processed')]),
                             lambda *args: self.fail('download should be mocked'))
            self.assertEqual(code, 2)
            place.assert_called_once()

    def test_cookie_symlink_returns_1_and_preserves_target(self):
        fixture = cc.DumpSafe()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        target = fixture.dir / 'target'
        target.write_bytes(b'original')
        fixture.out.symlink_to(target)
        code, output = fixture._dump(['a'], '--force')
        self.assertEqual(code, 1)
        self.assertIn('симлинк', output)
        self.assertEqual(target.read_bytes(), b'original')
        self.assertTrue(fixture.out.is_symlink())
        self.assertFalse(fixture.prev.exists())

    def test_slide_link_urls_and_image_alt(self):
        m = load('md-pptx')
        for source, expected in [('[текст](https://example.invalid)', 'текст (https://example.invalid)'),
                                 ('[https://example.invalid](https://example.invalid)', 'https://example.invalid'),
                                 ('![alt](p)', 'alt'),
                                 ('`[текст](url)`', '[текст](url)')]:
            with self.subTest(source=source):
                self.assertEqual(''.join(t for t, _ in m.runs_of(source)), expected)

    def test_invalid_child_json_returns_3_in_text_and_json(self):
        fixture = sc.SessionCostQ86()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write(fixture.child, ['{bad'])
        for as_json in (False, True):
            result = fixture.cli('--file', fixture.main, as_json=as_json)
            self.assertEqual(result.returncode, 3)
            if as_json:
                self.assertEqual(json.loads(result.stdout)['invalid_lines'], 1)
            else:
                self.assertIn('Сводка неполная', result.stdout)


class PublicationAudit(unittest.TestCase):
    def test_snippet_deduplicates_blobs_and_keeps_renamed_paths(self):
        skill = (ROOT / 'skills/repo-publication/SKILL.md').read_text()
        snippet = skill.split("python3 - \"$@\" <<'PY'\n", 1)[1].split("\nPY\n}", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True,
                                      capture_output=True, text=True).stdout

            git('init')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (root / 'plain.txt').write_text('needle\n')
            git('add', '.')
            git('commit', '-m', 'first')
            git('branch', 'kept')
            git('mv', 'plain.txt', 'credentials.txt')
            git('commit', '-m', 'rename')
            git('commit', '--allow-empty', '-m', 'same blob')
            for name in ('node_modules', 'venv', '.venv', 'dist', 'build'):
                (root / name).mkdir()
                (root / name / 'ignored.txt').write_text('needle\n')
            (root / 'credentials.txt').unlink()

            def audit(*args):
                result = subprocess.run(['python3', '-', *args], input=snippet,
                                        cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return [json.loads(line) for line in result.stdout.splitlines()]

            rows = audit('needle')
            self.assertEqual(rows[-1], {'tree_count': 0, 'history_count': 1})
            self.assertEqual(len(rows), 2)
            self.assertEqual(set(rows[0]), {'file', 'line', 'count'})
            names = audit('--names', 'credentials')
            self.assertEqual(names[-1]['history_count'], 1)
            self.assertEqual(names[0]['file'], 'credentials.txt')
            self.assertEqual(audit('absent')[-1], {'tree_count': 0, 'history_count': 0})


if __name__ == '__main__':
    unittest.main()
