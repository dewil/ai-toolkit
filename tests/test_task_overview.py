"""Independent OV01–OV08 CLI criteria; synthetic roots, stdlib only.

Oracle: docs/2026-10-15-spec-task-overview.md, not reader implementation.
Risk probes target identity merging, guessed progress, unsafe IO, and silent
metadata corruption. Deliberately no fallback/mock reader when CLI is absent.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'task-overview.py'
PROJECT_A = '53610cb3-3289-44fc-a0e2-5019e3e8d924'
PROJECT_B = '77d02774-0bf0-4e66-9ec2-0c8604a51008'
PRIVATE = 'SYNTHETIC_PRIVATE_BODY_NEVER_EXPORT_23917'


def assignment(ident='author', **changes):
    item = dict(id=ident, role='implementer', phase='implementation',
                state='in_progress', provider='codex', model=None,
                model_evidence=None, binding=None)
    item.update(changes)
    return item


def metadata(ident='TASK-SYNTHETIC', status='in_progress', phase='implementation'):
    return dict(id=ident, status=status, priority='medium', ai_task=dict(
        schema_version=1, phase=phase, owner='author', assignments=[assignment()],
        updated_at='2026-10-15T09:00:00Z', updated_by='author',
        next_step=dict(action='Run checks', actor='author'), blocker=None,
        handoff=None, excluded_phases=[], evidence=[]))


# Observe contractual effects without knowing reader internals. Any subprocess
# event fails the child immediately, even if product code catches exceptions.
NO_CHILDREN = r'''
import json, os, runpy, sys
script = sys.argv[1]
forbidden = set(json.loads(sys.argv[2]))
sys.argv = [script] + sys.argv[3:]
def guard(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        if os.path.realpath(os.fsdecode(args[0])) in forbidden:
            os._exit(92)
    if event.startswith(('subprocess.', 'os.exec', 'os.spawn')) or event == 'os.system':
        os._exit(91)
sys.addaudithook(guard)
runpy.run_path(script, run_name='__main__')
'''


class TaskOverviewContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='overview-contract-', dir='/var/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.project('client-a', PROJECT_A)

    def project(self, name, project_id):
        root = self.base / name
        (root / '.AI').mkdir(parents=True)
        (root / '.AI' / 'project.json').write_text(json.dumps(dict(
            schema_version=1, layout_version='AI-LAYOUT-1', project_id=project_id)),
            encoding='utf-8')
        for folder in ('backlog', 'done'):
            (root / 'docs' / folder).mkdir(parents=True)
        return root

    def write(self, meta=None, name='task.md', folder='backlog', root=None, body=None):
        path = (root or self.root) / 'docs' / folder / name
        body = '# Synthetic task title\n\n' + PRIVATE + '\n' if body is None else body
        path.write_text('---\n' + json.dumps(meta or metadata(), ensure_ascii=False) +
                        '\n---\n' + body, encoding='utf-8')
        return path

    def cli(self, roots=None, json_output=True, audit=False, extra=(), forbid_reads=()):
        args = []
        for root in roots or [self.root]:
            args.extend(('--root', str(root)))
        if json_output:
            args.append('--json')
        args.extend(extra)
        command = [sys.executable, str(SCRIPT), *args]
        if audit or forbid_reads:
            forbidden = json.dumps([str(p.resolve()) for p in forbid_reads])
            command = [sys.executable, '-c', NO_CHILDREN, str(SCRIPT), forbidden, *args]
        return subprocess.run(command, cwd=self.base, capture_output=True,
                              text=True, timeout=15)

    def snapshot(self, roots=None, partial=False, **kwargs):
        result = self.cli(roots, **kwargs)
        self.assertEqual(result.returncode, 3 if partial else 0,
                         'Required CLI behavior missing: ' + result.stdout + result.stderr)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['coverage'], 'partial' if partial else 'complete')
        self.assertIsInstance(data['tasks'], list)
        self.assertIsInstance(data['diagnostics'], list)
        stamp = datetime.fromisoformat(data['as_of'].replace('Z', '+00:00'))
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        if partial:
            self.assertTrue(data['diagnostics'], 'partial source must remain diagnostic')
        for diagnostic in data['diagnostics']:
            self.assertIsInstance(diagnostic, dict)
            for key in ('code', 'message'):
                self.assertIsInstance(diagnostic[key], str)
                self.assertTrue(diagnostic[key])
        return data

    def only_task(self, data):
        self.assertEqual(len(data['tasks']), 1)
        return data['tasks'][0]

    def test_empty_root_is_complete_empty_snapshot(self):
        data = self.snapshot()
        self.assertEqual(data['tasks'], [])
        self.assertEqual(data['diagnostics'], [])

    def test_v1_file_exports_declared_progress_and_title_without_body(self):
        meta = metadata()
        self.write(meta)
        task = self.only_task(self.snapshot())
        for key, expected in dict(project_id=PROJECT_A, task_id=meta['id'],
                                  title='Synthetic task title', status='in_progress',
                                  phase='implementation', owner='author',
                                  updated_at=meta['ai_task']['updated_at'],
                                  blocker=None, next_step=meta['ai_task']['next_step']).items():
            self.assertEqual(task[key], expected)
        self.assertEqual(task['owner_ref'], 'docs/backlog/task.md')
        self.assertIsInstance(task['inconsistencies'], list)
        self.assertEqual(task['inconsistencies'], [])
        self.assertIsInstance(task['age_seconds'], (int, float))
        self.assertGreaterEqual(task['age_seconds'], 0)
        self.assertEqual(task['assignments'][0]['id'], 'author')
        self.assertIsNone(task['assignments'][0]['model'])

    def test_human_table_has_same_task_project_and_phase(self):
        self.write()
        result = self.cli(json_output=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ('Synthetic task title', 'implementation'):
            self.assertIn(text, result.stdout)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)

    def test_missing_root_is_partial_and_keeps_healthy_tasks(self):
        self.write()
        data = self.snapshot([self.root, self.base / 'missing'], partial=True)
        self.assertEqual(self.only_task(data)['task_id'], 'TASK-SYNTHETIC')

    def test_same_task_id_across_projects_is_not_merged(self):
        other = self.project('client-b', PROJECT_B)
        self.write()
        self.write(root=other)
        data = self.snapshot([self.root, other])
        self.assertEqual({(t['project_id'], t['task_id']) for t in data['tasks']},
                         {(PROJECT_A, 'TASK-SYNTHETIC'), (PROJECT_B, 'TASK-SYNTHETIC')})
        self.assertEqual(len(data['tasks']), 2)

    def test_same_project_id_in_different_physical_roots_is_conflict(self):
        other = self.project('client-copy', PROJECT_A)
        self.write(metadata('TASK-A'))
        self.write(metadata('TASK-B'), root=other)
        self.snapshot([self.root, other], partial=True)

    def test_repeated_physical_root_alias_is_deduplicated(self):
        self.write()
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.only_task(self.snapshot([self.root, alias, self.root]))

    def test_move_to_done_preserves_identity(self):
        path = self.write()
        before = self.only_task(self.snapshot())
        path.unlink()
        meta = metadata(status='done', phase='done')
        meta['ai_task']['next_step'] = None
        meta['ai_task']['assignments'][0]['state'] = 'finished'
        self.write(meta, folder='done')
        after = self.only_task(self.snapshot())
        self.assertEqual((before['project_id'], before['task_id']),
                         (after['project_id'], after['task_id']))
        self.assertEqual(after['status'], 'done')
        self.assertEqual(after['phase'], 'done')

    def test_duplicate_task_key_in_backlog_and_done_is_conflict(self):
        self.write()
        meta = metadata(status='done', phase='done')
        meta['ai_task']['next_step'] = None
        self.write(meta, folder='done')
        self.snapshot(partial=True)

    def test_legacy_yaml_json_and_plain_markdown_are_visible_unchanged(self):
        contents = {
            'yaml.md': '---\nid: LEGACY-YAML\nstatus: done\n---\n# Legacy YAML\n',
            'json.md': '---\n{"id":"LEGACY-JSON","status":"done"}\n---\n# Legacy JSON\n',
            'plain.md': '# Plain owner\n\nID: LEGACY-PLAIN\nСтатус: in_progress\n',
        }
        self.write(metadata('HEALTHY'))
        for name, text in contents.items():
            (self.root / 'docs' / 'backlog' / name).write_text(text)
        data = self.snapshot(partial=True)
        self.assertTrue(any(t['task_id'] == 'HEALTHY' for t in data['tasks']))
        serialized = json.dumps(data, ensure_ascii=False)
        for name, text in contents.items():
            self.assertIn(name, serialized, 'legacy owner must be visible by path/name')
            self.assertEqual((self.root / 'docs' / 'backlog' / name).read_text(), text)

    def test_invalid_json_duplicate_keys_and_unknown_schema_do_not_erase_healthy(self):
        self.write(metadata('HEALTHY'))
        invalid = {
            'broken.md': '---\n{"id":\n---\n' + PRIVATE,
            'duplicate.md': '---\n' + json.dumps(metadata()).replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1') + '\n---\n',
            'unclosed.md': '---\n' + json.dumps(metadata()) + '\n' + PRIVATE,
        }
        unknown = metadata('UNKNOWN')
        unknown['ai_task']['schema_version'] = 99
        self.write(unknown, name='unknown.md')
        for name, text in invalid.items():
            (self.root / 'docs' / 'backlog' / name).write_text(text)
        data = self.snapshot(partial=True)
        self.assertTrue(any(t['task_id'] == 'HEALTHY' for t in data['tasks']))
        report = json.dumps(data)
        for name in (*invalid, 'unknown.md'):
            self.assertIn(name, report)

    def test_unknown_ai_task_field_and_duplicate_assignment_ids_are_diagnostic(self):
        for kind in ('unknown_field', 'duplicate_assignment'):
            with self.subTest(kind=kind):
                meta = metadata()
                if kind == 'unknown_field':
                    meta['ai_task']['running_by_guess'] = True
                else:
                    meta['ai_task']['assignments'].append(assignment())
                self.write(meta)
                self.snapshot(partial=True)

    def test_directory_status_phase_inconsistency_is_never_silently_repaired(self):
        cases = [('done', 'done', 'backlog'), ('in_progress', 'implementation', 'done'),
                 ('in_progress', 'queue', 'backlog'), ('queued', 'done', 'backlog'),
                 ('done', 'review', 'done')]
        for status, phase, folder in cases:
            with self.subTest(status=status, phase=phase, folder=folder):
                meta = metadata(status=status, phase=phase)
                if status == 'done':
                    meta['ai_task']['next_step'] = None
                path = self.write(meta, folder=folder)
                original = path.read_bytes()
                data = self.snapshot(partial=True)
                for task in data['tasks']:
                    if task['task_id'] == meta['id']:
                        self.assertEqual((task['status'], task['phase']), (status, phase))
                self.assertEqual(path.read_bytes(), original)
                path.unlink()

    def test_waiting_and_blocked_require_reason_and_keep_current_phase(self):
        for status in ('waiting_user', 'blocked'):
            with self.subTest(status=status):
                meta = metadata(status=status, phase='review')
                self.write(meta)
                self.snapshot(partial=True)
                meta['ai_task']['blocker'] = dict(reason='Need decision', actor='user')
                meta['ai_task']['next_step'] = dict(action='Decide', actor='user')
                self.write(meta)
                task = self.only_task(self.snapshot())
                self.assertEqual(task['phase'], 'review')
                self.assertEqual(task['status'], status)
                self.assertEqual(task['blocker']['reason'], 'Need decision')
                self.assertEqual(task['next_step']['actor'], 'user')

    def test_cancelled_requires_reason_and_never_becomes_pass(self):
        meta = metadata(status='cancelled', phase='done')
        meta['ai_task']['next_step'] = None
        self.write(meta, folder='done')
        self.snapshot(partial=True)
        meta['ai_task']['blocker'] = dict(reason='Owner cancelled')
        self.write(meta, folder='done')
        task = self.only_task(self.snapshot())
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual(task['blocker']['reason'], 'Owner cancelled')

    def test_parallel_runs_and_all_assignment_states_do_not_complete_task(self):
        states = ('queued', 'in_progress', 'waiting_user', 'blocked', 'paused',
                  'finished', 'failed', 'cancelled', 'unknown')
        meta = metadata(phase='review')
        meta['ai_task']['assignments'] = [assignment('run-' + state, state=state,
            role='reviewer', phase='review') for state in states]
        meta['ai_task']['owner'] = meta['ai_task']['updated_by'] = 'coordinator'
        meta['ai_task']['next_step']['actor'] = 'unassigned'
        self.write(meta)
        task = self.only_task(self.snapshot())
        self.assertEqual(task['status'], 'in_progress')
        self.assertEqual({a['state'] for a in task['assignments']}, set(states))
        self.assertEqual(len(task['assignments']), len(states))

    def test_pending_handoff_is_preserved_but_accepted_uncleared_is_inconsistent(self):
        meta = metadata()
        handoff = dict(to='new-owner', at='2026-10-15T09:01:00+00:00', reason='Shift ends')
        meta['ai_task']['handoff'] = handoff
        self.write(meta)
        task = self.only_task(self.snapshot())
        self.assertEqual(task['owner'], 'author')
        self.assertEqual(task['handoff'], handoff)
        meta['ai_task']['owner'] = meta['ai_task']['updated_by'] = 'new-owner'
        self.write(meta)
        self.snapshot(partial=True)
        meta['ai_task']['handoff'] = None
        self.write(meta)
        self.assertEqual(self.only_task(self.snapshot())['owner'], 'new-owner')

    def test_timezone_and_required_types_are_validated(self):
        cases = [('updated_at', '2026-10-15T09:00:00'), ('owner', ''),
                 ('updated_by', None), ('assignments', {}),
                 ('next_step', None), ('phase', 'invented')]
        for field, value in cases:
            with self.subTest(field=field):
                meta = metadata()
                meta['ai_task'][field] = value
                self.write(meta)
                self.snapshot(partial=True)
        meta = metadata()
        meta['ai_task']['handoff'] = dict(to='next', at='2026-10-15T09:00:00', reason='handoff')
        self.write(meta)
        self.snapshot(partial=True)

    def test_duplicate_phase_exclusion_is_diagnostic_and_preserved(self):
        meta = metadata()
        exclusion = dict(phase='domain_audit', reason='No domain change', decision_ref='docs/decision.md')
        meta['ai_task']['excluded_phases'] = [exclusion, copy.deepcopy(exclusion)]
        self.write(meta)
        self.snapshot(partial=True)

    def test_reader_does_not_run_git_or_any_client_subprocess(self):
        meta = metadata()
        (self.root / 'docs' / 'checks.md').write_text('Synthetic report')
        meta['ai_task']['evidence'] = [dict(type='checks', result='pass',
            ref='docs/checks.md', revision='claimed-git-revision')]
        self.write(meta)
        self.snapshot(audit=True)

    def test_valid_sha_is_not_semantic_pass_and_changed_sha_is_diagnostic(self):
        evidence = self.root / 'docs' / 'checks.txt'
        evidence.write_bytes(b'synthetic check result\n')
        meta = metadata()
        meta['ai_task']['evidence'] = [dict(type='checks', result='pass',
            ref='docs/checks.txt', hashes=[dict(path='docs/checks.txt',
                sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())])]
        self.write(meta)
        task = self.only_task(self.snapshot())
        self.assertEqual(task['status'], 'in_progress')
        self.assertEqual(task['evidence'][0]['hash_status'], 'matched')
        evidence.write_bytes(b'different result\n')
        data = self.snapshot(partial=True)
        task = self.only_task(data)
        self.assertEqual(task['status'], 'in_progress')
        self.assertEqual(task['evidence'][0]['hash_status'], 'mismatch')

    def test_symlinks_cannot_read_task_or_evidence_outside_root(self):
        outside = self.base / 'outside.md'
        outside.write_text('---\n' + json.dumps(metadata('OUTSIDE')) + '\n---\n# Outside\n' + PRIVATE)
        (self.root / 'docs' / 'backlog' / 'escape.md').symlink_to(outside)
        self.write(metadata('HEALTHY'))
        data = self.snapshot(partial=True, forbid_reads=[outside])
        self.assertFalse(any(t['task_id'] == 'OUTSIDE' for t in data['tasks']))
        (self.root / 'docs' / 'backlog' / 'escape.md').unlink()
        evidence = self.root / 'docs' / 'evidence.txt'
        evidence.symlink_to(outside)
        meta = metadata('HEALTHY')
        meta['ai_task']['evidence'] = [dict(type='checks', result='pass',
            ref='docs/evidence.txt', hashes=[dict(path='docs/evidence.txt', sha256='a' * 64)])]
        self.write(meta)
        self.snapshot(partial=True, forbid_reads=[outside])

    def test_scan_scope_ignores_memory_env_settings_and_nested_projects(self):
        self.write(metadata('HEALTHY'))
        for relative in ('.AI/memory/hidden.md', 'archive/task.md',
                         'nested/docs/backlog/task.md', 'docs/backlog/nested/task.md'):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('---\n' + json.dumps(metadata('HIDDEN')) + '\n---\n' + PRIVATE)
        for name in ('.env', '.AI/settings.local.json'):
            (self.root / name).write_text(PRIVATE)
        forbidden = [self.root / p for p in ('.AI/memory/hidden.md', 'archive/task.md',
            'nested/docs/backlog/task.md', 'docs/backlog/nested/task.md',
            '.env', '.AI/settings.local.json')]
        data = self.snapshot(forbid_reads=forbidden)
        self.assertEqual(self.only_task(data)['task_id'], 'HEALTHY')

    def test_read_only_keeps_contents_and_file_metadata(self):
        task = self.write()
        paths = [task, self.root / '.AI' / 'project.json']
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                  for p in paths}
        files_before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.snapshot()
        self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                                 for p in paths})
        self.assertEqual(files_before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_title_falls_back_to_id_when_no_h1(self):
        self.write(body='## Subheading\nNo top level heading\n')
        self.assertEqual(self.only_task(self.snapshot())['title'], 'TASK-SYNTHETIC')

    def test_mtime_does_not_replace_explicit_timestamp_or_age(self):
        meta = metadata()
        meta['ai_task']['updated_at'] = '2000-01-01T00:00:00+00:00'
        path = self.write(meta)
        os.utime(path, None)
        task = self.only_task(self.snapshot())
        self.assertEqual(datetime.fromisoformat(task['updated_at'].replace('Z', '+00:00')),
                         datetime.fromisoformat(meta['ai_task']['updated_at']))
        self.assertGreater(task['age_seconds'], 20 * 365 * 86400)

    def test_unknown_binding_kind_keeps_assignment_unlinked(self):
        meta = metadata()
        meta['ai_task']['assignments'][0]['binding'] = dict(kind='future_provider', sid='opaque')
        self.write(meta)
        task = self.only_task(self.snapshot())
        run = task['assignments'][0]
        self.assertEqual(run['binding_status'], 'unlinked')
        self.assertEqual(run['observed_status'], 'unknown')
        self.assertIsNone(run['observed_at'])

    def test_file_and_evidence_size_limits_are_partial(self):
        self.write(metadata('HEALTHY'))
        huge = self.root / 'docs' / 'backlog' / 'huge.md'
        huge.write_text('---\n' + json.dumps(metadata('HUGE')) + '\n---\n' + 'x' * (1024 * 1024))
        data = self.snapshot(partial=True)
        self.assertTrue(any(t['task_id'] == 'HEALTHY' for t in data['tasks']))
        self.assertFalse(any(t['task_id'] == 'HUGE' for t in data['tasks']))
        huge.unlink()
        evidence = self.root / 'docs' / 'large-evidence.txt'
        evidence.write_bytes(b'x' * (1024 * 1024 + 1))
        meta = metadata('HEALTHY')
        meta['ai_task']['evidence'] = [dict(type='checks', result='pass',
            ref='docs/large-evidence.txt', hashes=[dict(path='docs/large-evidence.txt', sha256='a' * 64)])]
        self.write(meta)
        self.snapshot(partial=True)

    def test_file_count_limit_and_total_bytes_limit_are_partial(self):
        folder = self.root / 'docs' / 'backlog'
        for index in range(2001):
            (folder / ('legacy-%04d.md' % index)).write_text('# Legacy task\n')
        data = self.snapshot(partial=True)
        self.assertTrue(any(any(term in (d['code'] + ' ' + d['message']).lower()
                                for term in ('limit', 'too_many', 'exceed', 'budget', 'cap'))
                            for d in data['diagnostics']),
                        'file limit needs explicit diagnostic, besides legacy warnings')
        for path in folder.glob('*.md'):
            path.unlink()
        for index in range(36):
            self.write(metadata('TASK-%03d' % index), name='task-%03d.md' % index,
                       body='# Task\n' + 'x' * (950 * 1024))
        data = self.snapshot(partial=True)
        self.assertTrue(any(any(term in (d['code'] + ' ' + d['message']).lower()
                                for term in ('limit', 'too_many', 'exceed', 'budget', 'cap'))
                            for d in data['diagnostics']))

    def test_roots_and_evidence_count_limits_are_partial(self):
        self.write()
        roots = [self.root]
        for index in range(64):
            # Different project identity is part of each explicit synthetic root.
            roots.append(self.project('root-%02d' % index, '00000000-0000-4000-8000-%012d' % index))
        self.snapshot(roots, partial=True)
        meta = metadata()
        hashes = []
        for index in range(33):
            path = self.root / 'docs' / ('evidence-%02d.txt' % index)
            path.write_text('result')
            hashes.append(dict(path='docs/' + path.name,
                               sha256=hashlib.sha256(b'result').hexdigest()))
        meta['ai_task']['evidence'] = [dict(type='checks', result='pass',
                                          ref='docs/evidence-00.txt', hashes=hashes)]
        self.write(meta)
        self.snapshot(partial=True)

    def test_project_identity_and_task_directory_symlinks_cannot_escape(self):
        outside = self.project('outside-project', PROJECT_B)
        self.write(metadata('OUTSIDE'), root=outside)
        project_json = self.root / '.AI' / 'project.json'
        project_json.unlink()
        project_json.symlink_to(outside / '.AI' / 'project.json')
        self.snapshot(partial=True)
        project_json.unlink()
        project_json.write_text(json.dumps(dict(project_id=PROJECT_A)))
        backlog = self.root / 'docs' / 'backlog'
        backlog.rmdir()
        backlog.symlink_to(outside / 'docs' / 'backlog', target_is_directory=True)
        data = self.snapshot(partial=True)
        self.assertFalse(any(t['task_id'] == 'OUTSIDE' for t in data['tasks']))

    def test_evidence_path_traversal_and_absolute_hash_paths_are_diagnostic(self):
        outside = self.base / 'outside-evidence.txt'
        outside.write_text(PRIVATE)
        for path in ('../outside-evidence.txt', str(outside)):
            with self.subTest(path=path):
                meta = metadata()
                meta['ai_task']['evidence'] = [dict(type='checks', result='pass',
                    ref=path, hashes=[dict(path=path, sha256='a' * 64)])]
                self.write(meta)
                self.snapshot(partial=True)

    def test_invalid_assignment_and_hash_metadata_are_diagnostic(self):
        mutations = [lambda m: m['ai_task']['assignments'][0].update(state='running'),
                     lambda m: m['ai_task']['assignments'][0].update(model=''),
                     lambda m: m['ai_task'].update(evidence=[dict(type='checks',
                         result='pass', ref='docs/a.txt', hashes=[dict(path='docs/a.txt', sha256='wrong')])])]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                meta = metadata()
                mutate(meta)
                self.write(meta)
                self.snapshot(partial=True)

    def test_bad_arguments_exit_two(self):
        result = self.cli(extra=('--not-an-option',))
        self.assertEqual(result.returncode, 2)
        self.assertIn('--not-an-option', result.stderr)


if __name__ == '__main__':
    unittest.main()
