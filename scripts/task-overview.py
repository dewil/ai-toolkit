#!/usr/bin/env python3
"""Bounded, read-only projection of Markdown task owners (stdlib, Linux/Python 3.11+).

Public API: read_snapshot(roots, *, now=None). Roots are supplied by the caller;
authorization and native session observations belong to the embedding application.
No project code, subprocess, transcript or persistent registry is used.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid


MAX_FILE_BYTES = 1024 * 1024
MAX_FILES_PER_ROOT = 2000
MAX_ROOTS = 64
MAX_EVIDENCE_FILES = 32
MAX_TOTAL_BYTES = 32 * 1024 * 1024
PHASES = frozenset(('queue', 'requirements', 'domain_audit', 'spec', 'criteria',
                    'implementation', 'review', 'checks', 'release', 'rollout',
                    'acceptance', 'done'))
STATUSES = frozenset(('queued', 'in_progress', 'waiting_user', 'blocked',
                      'paused', 'done', 'cancelled'))
RUN_STATES = STATUSES - {'done'} | {'finished', 'failed', 'unknown'}
TASK_FIELDS = frozenset(('schema_version', 'phase', 'owner', 'assignments',
                         'updated_at', 'updated_by', 'next_step', 'blocker',
                         'handoff', 'excluded_phases', 'evidence'))
ASSIGNMENT_FIELDS = frozenset(('id', 'role', 'phase', 'state', 'provider', 'model',
                               'model_evidence', 'binding'))


class ReadFailure(Exception):
    """Only static codes cross the IO boundary; raw exceptions may hold secrets."""


def _string(value):
    return isinstance(value, str) and bool(value.strip())


def _time(value):
    if not _string(value) or not re.match(r'^\d{4}-\d\d-\d\dT', value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _utc(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReadFailure('duplicate_json_key')
            result[key] = value
        return result

    def nonfinite(_):
        raise ReadFailure('invalid_json')

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, RecursionError):
        raise ReadFailure('invalid_json') from None


def _relative(value, *, evidence=False):
    """Reject ambiguous paths and sensitive resources before touching the disk."""
    if not _string(value) or '\\' in value or '\x00' in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        return False
    if evidence and any(p.startswith('.') or p.lower() in ('auth', 'privateconfigs')
                        for p in path.parts):
        return False
    return True


class _Root:
    """Pin a root descriptor, then open every component without following links.

    Checking resolve() followed by open() would permit a symlink swap between the
    two operations. Descriptor-relative opens also bound reads during such swaps.
    """
    def __init__(self, path, budget):
        self.budget = budget
        try:
            canonical = Path(path).resolve(strict=True)
            self.fd = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self.fd)
            self.identity = (info.st_dev, info.st_ino)
        except (OSError, ValueError, RuntimeError, TypeError):
            raise ReadFailure('root_unavailable') from None

    def close(self):
        os.close(self.fd)

    def open(self, relative, directory=False):
        if not _relative(relative):
            raise ReadFailure('unsafe_path')
        current = os.dup(self.fd)
        try:
            parts = relative.split('/')
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = next_fd
            info = os.fstat(current)
            if not directory and not stat.S_ISREG(info.st_mode):
                raise ReadFailure('not_regular_file')
            result, current = current, None
            return result
        except OSError:
            raise ReadFailure('resource_unavailable_or_symlink') from None
        finally:
            if current is not None:
                os.close(current)

    def read(self, relative):
        fd = self.open(relative)
        try:
            size = os.fstat(fd).st_size
            if size > MAX_FILE_BYTES:
                raise ReadFailure('file_size_limit')
            remaining = MAX_TOTAL_BYTES - self.budget[0]
            if size > remaining or remaining <= 0:
                raise ReadFailure('total_bytes_limit')
            # One extra byte detects concurrent growth, while remaining budget
            # bounds the actual read even when stat was stale.
            limit = min(MAX_FILE_BYTES + 1, remaining)
            chunks = []
            count = 0
            while count < limit:
                chunk = os.read(fd, min(65536, limit - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
            self.budget[0] += count
            if count > MAX_FILE_BYTES:
                raise ReadFailure('file_size_limit')
            if os.fstat(fd).st_size > count:
                raise ReadFailure('total_bytes_limit' if limit == remaining else 'file_size_limit')
            return b''.join(chunks)
        except OSError:
            raise ReadFailure('read_failed') from None
        finally:
            os.close(fd)

    def files(self, folder, allowance):
        fd = self.open(folder, directory=True)
        try:
            names = []
            exceeded = False
            scanned = 0
            with os.scandir(fd) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_FILES_PER_ROOT:
                        exceeded = True
                        break
                    if entry.name.endswith('.md'):
                        if len(names) >= allowance:
                            exceeded = True
                            break
                        names.append(entry.name)
            return sorted(names), exceeded
        except OSError:
            raise ReadFailure('directory_unavailable') from None
        finally:
            os.close(fd)


def _header(text):
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        return None, text
    for index in range(1, len(lines)):
        if lines[index] == '---':
            return '\n'.join(lines[1:index]), '\n'.join(lines[index + 1:])
    raise ReadFailure('unclosed_frontmatter')


def _legacy_id(text):
    match = re.search(r'(?m)^(?:id|ID):\s*[\"\']?([A-Za-z0-9][A-Za-z0-9_.-]*)[\"\']?\s*$', text)
    return match.group(1) if match else None


def _title(body, fallback):
    fence = None
    for line in body.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        if line.startswith('# '):
            return ''.join(c for c in line[2:].strip()[:256] if c.isprintable()) or fallback
    return fallback


def _validate(meta, folder, now):
    """Validate declarations; never infer progress, applicability or acceptance."""
    issues = []
    def issue(code):
        if code not in issues:
            issues.append(code)
    data = meta['ai_task']
    if set(data) - TASK_FIELDS:
        issue('unsupported_ai_task_field')
    if TASK_FIELDS - set(data):
        issue('missing_ai_task_field')
    for field in ('owner', 'updated_by'):
        if not _string(data.get(field)):
            issue('invalid_' + field)
    if data.get('owner') != data.get('updated_by'):
        issue('owner_writer_mismatch')
    phase, status = data.get('phase'), meta.get('status')
    if not isinstance(phase, str) or phase not in PHASES:
        issue('invalid_phase')
    if not isinstance(status, str) or status not in STATUSES:
        issue('invalid_status')
    if (status in ('done', 'cancelled') and (phase != 'done' or folder != 'done') or
            status == 'queued' and (phase == 'done' or folder != 'backlog') or
            status in ('in_progress', 'waiting_user', 'blocked', 'paused') and
            (phase in ('queue', 'done') or folder != 'backlog')):
        issue('status_phase_directory_mismatch')
    stamp = _time(data.get('updated_at'))
    if stamp is None:
        issue('invalid_updated_at')
    elif stamp > now:
        issue('future_updated_at')
    blocker = data.get('blocker')
    if blocker is not None and (not isinstance(blocker, dict) or
            not _string(blocker.get('reason')) or
            ('actor' in blocker and not _string(blocker['actor'])) or
            set(blocker) - {'reason', 'actor'}):
        issue('invalid_blocker')
    if status in ('waiting_user', 'blocked', 'cancelled') and (
            not isinstance(blocker, dict) or not _string(blocker.get('reason'))):
        issue('missing_blocker_reason')
    step = data.get('next_step')
    if not (step is None and status in ('done', 'cancelled')) and (
            not isinstance(step, dict) or set(step) != {'action', 'actor'} or
            not all(_string(step.get(k)) for k in ('action', 'actor'))):
        issue('invalid_next_step')
    handoff = data.get('handoff')
    if handoff is not None:
        if (not isinstance(handoff, dict) or set(handoff) != {'to', 'at', 'reason'} or
                not all(_string(handoff.get(k)) for k in ('to', 'at', 'reason')) or
                _time(handoff.get('at')) is None):
            issue('invalid_handoff')
        elif handoff['to'] == data.get('owner'):
            issue('accepted_handoff_not_cleared')
    runs = data.get('assignments')
    if not isinstance(runs, list):
        issue('invalid_assignments')
        runs = []
    ids = set()
    for run in runs:
        if not isinstance(run, dict) or set(run) != ASSIGNMENT_FIELDS:
            issue('invalid_assignment')
            continue
        if not all(_string(run.get(k)) for k in ('id', 'role', 'provider')):
            issue('invalid_assignment')
        if (not isinstance(run['phase'], str) or run['phase'] not in PHASES or
                not isinstance(run['state'], str) or run['state'] not in RUN_STATES):
            issue('invalid_assignment_state_or_phase')
        for field in ('model', 'model_evidence'):
            if run[field] is not None and not _string(run[field]):
                issue('invalid_assignment_' + field)
        if _string(run['id']):
            if run['id'] in ids:
                issue('duplicate_assignment_id')
            ids.add(run['id'])
        binding = run['binding']
        if binding is not None:
            if not isinstance(binding, dict) or not _string(binding.get('kind')):
                issue('invalid_binding')
            elif binding['kind'] == 'codex_session':
                try:
                    if set(binding) != {'kind', 'sid'} or not isinstance(binding['sid'], str):
                        raise ValueError
                    uuid.UUID(binding['sid'])
                except (ValueError, KeyError, AttributeError):
                    issue('invalid_binding')
    excluded = data.get('excluded_phases')
    if not isinstance(excluded, list):
        issue('invalid_excluded_phases')
        excluded = []
    phases = set()
    for item in excluded:
        if (not isinstance(item, dict) or set(item) != {'phase', 'reason', 'decision_ref'} or
                not all(_string(item.get(k)) for k in ('phase', 'reason', 'decision_ref')) or
                item['phase'] not in PHASES):
            issue('invalid_phase_exclusion')
            continue
        if item['phase'] in phases:
            issue('duplicate_phase_exclusion')
        phases.add(item['phase'])
    if isinstance(phase, str) and phase in phases:
        issue('current_phase_excluded')
    return issues, stamp


def _evidence(items, root, issue):
    if not isinstance(items, list):
        issue('invalid_evidence')
        return []
    result = []
    file_count = 0
    for item in items:
        if (not isinstance(item, dict) or
                not all(_string(item.get(k)) for k in ('type', 'result', 'ref')) or
                item['result'] not in ('pass', 'fail', 'notrun', 'unknown') or
                set(item) - {'type', 'result', 'ref', 'revision', 'hashes', 'verifier'}):
            issue('invalid_evidence')
            continue
        evidence = {k: item[k] for k in ('type', 'result', 'ref')}
        evidence.update(hash_status='unknown', revision_status='unknown', navigable=False)
        external = bool(re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', item['ref']))
        if not external and not _relative(item['ref'], evidence=True):
            issue('unsafe_evidence_ref')
        elif not external:
            try:
                fd = root.open(item['ref'])
                os.close(fd)
                evidence['navigable'] = True
            except ReadFailure as exc:
                issue(str(exc))
        if 'revision' in item:
            if not _string(item['revision']):
                issue('invalid_evidence_revision')
            else:
                evidence['revision'] = item['revision']
        if 'verifier' in item:
            verifier = item['verifier']
            if (not isinstance(verifier, dict) or set(verifier) != {'model', 'model_evidence'} or
                    any(v is not None and not _string(v) for v in verifier.values())):
                issue('invalid_evidence_verifier')
            else:
                evidence['verifier'] = verifier
        hashes = item.get('hashes', [])
        if not isinstance(hashes, list):
            issue('invalid_evidence_hashes')
            hashes = []
        checked = []
        outcomes = []
        for hashed in hashes:
            file_count += 1
            if file_count > MAX_EVIDENCE_FILES:
                issue('evidence_file_count_limit')
                outcomes.append('unknown')
                break
            if (not isinstance(hashed, dict) or set(hashed) != {'path', 'sha256'} or
                    not _string(hashed.get('path')) or not isinstance(hashed.get('sha256'), str) or
                    not re.fullmatch(r'[0-9a-fA-F]{64}', hashed['sha256'])):
                issue('invalid_evidence_hash')
                outcomes.append('unknown')
                continue
            outcome = 'unknown'
            if not _relative(hashed['path'], evidence=True):
                issue('unsafe_evidence_path')
            else:
                try:
                    actual = hashlib.sha256(root.read(hashed['path'])).hexdigest()
                    outcome = 'matched' if actual == hashed['sha256'].lower() else 'mismatch'
                    if outcome == 'mismatch':
                        issue('evidence_hash_mismatch')
                except ReadFailure as exc:
                    issue(str(exc))
            checked.append({**hashed, 'hash_status': outcome})
            outcomes.append(outcome)
        if 'hashes' in item:
            evidence['hashes'] = checked
        if outcomes:
            evidence['hash_status'] = ('mismatch' if 'mismatch' in outcomes else
                                       'matched' if all(x == 'matched' for x in outcomes) else 'unknown')
        result.append(evidence)
    return result


def read_snapshot(roots, *, now=None):
    """Return a v1 file snapshot. Partial failures retain all healthy sources.

    now optionally supplies a timezone-aware datetime for deterministic callers.
    Native observation fields always remain explicitly unlinked/unknown here.
    """
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')
    now = now.astimezone(timezone.utc)
    tasks, diagnostics = [], []
    budget, physical, projects = [0], set(), {}

    def diagnostic(code, project_id=None, task_id=None, path=None):
        item = {'code': code, 'message': code.replace('_', ' ')}
        for key, value in (('project_id', project_id), ('task_id', task_id), ('path', path)):
            if value is not None:
                item[key] = value
        diagnostics.append(item)

    for index, path in enumerate(roots):
        if index >= MAX_ROOTS:
            diagnostic('root_count_limit')
            break
        root = None
        try:
            root = _Root(path, budget)
            identity = _json(root.read('.AI/project.json').decode('utf-8'))
            if not isinstance(identity, dict) or not _string(identity.get('project_id')):
                raise ReadFailure('invalid_project_identity')
            project_id = identity['project_id']
            # Identity must be successfully reread before alias deduplication.
            if root.identity in physical:
                continue
            physical.add(root.identity)
            if project_id in projects:
                diagnostic('project_identity_conflict', project_id)
            projects[project_id] = projects.get(project_id, 0) + 1
            file_count = 0
            for folder in ('backlog', 'done'):
                directory = 'docs/' + folder
                try:
                    names, exceeded = root.files(directory, MAX_FILES_PER_ROOT - file_count)
                    if exceeded:
                        diagnostic('task_file_count_limit', project_id, path=directory)
                    file_count += len(names)
                except ReadFailure as exc:
                    diagnostic(str(exc), project_id, path=directory)
                    continue
                for name in names:
                    relative = directory + '/' + name
                    try:
                        text = root.read(relative).decode('utf-8')
                        header, body = _header(text)
                        meta = None
                        if header is not None and header.lstrip().startswith(('{', '[')):
                            meta = _json(header)
                            if not isinstance(meta, dict):
                                raise ReadFailure('invalid_frontmatter_object')
                        if meta is None or 'ai_task' not in meta:
                            ident = meta.get('id') if meta else _legacy_id(header or text)
                            diagnostic('status_format_not_connected', project_id,
                                       ident if _string(ident) else None, relative)
                            continue
                        data = meta['ai_task']
                        if not isinstance(data, dict):
                            raise ReadFailure('invalid_ai_task')
                        if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
                            raise ReadFailure('unsupported_task_schema')
                        if not _string(meta.get('id')):
                            raise ReadFailure('invalid_task_id')
                        task_id = meta['id']
                        issues, stamp = _validate(meta, folder, now)
                        def issue(code):
                            if code not in issues:
                                issues.append(code)
                        evidence = _evidence(data.get('evidence'), root, issue)
                        if any(code.startswith(('invalid_', 'missing_')) for code in issues):
                            for code in issues:
                                diagnostic(code, project_id, task_id, relative)
                            continue
                        runs = []
                        for run in data.get('assignments', []) if isinstance(data.get('assignments'), list) else []:
                            if not isinstance(run, dict):
                                continue
                            declared = {k: run.get(k) for k in ASSIGNMENT_FIELDS}
                            binding = declared['binding']
                            if isinstance(binding, dict):
                                declared['binding'] = {k: binding[k] for k in ('kind', 'sid') if k in binding}
                            declared.update(binding_status='unlinked', observed_status='unknown',
                                            observed_at=None, source='task_metadata',
                                            relation_source='declared_binding' if binding else 'none',
                                            confirmed_model=None, model_source=None)
                            runs.append(declared)
                        task = dict(project_id=project_id, task_id=task_id,
                                    title=_title(body, task_id), owner_ref=relative,
                                    inconsistencies=issues,
                                    age_seconds=max(0, (now - stamp).total_seconds()) if stamp else None,
                                    assignments=runs, evidence=evidence)
                        for key in ('phase', 'owner', 'updated_at', 'updated_by', 'next_step',
                                    'blocker', 'handoff', 'excluded_phases'):
                            task[key] = data.get(key)
                        # Keep the declared timestamp spelling; as_of is normalized UTC.
                        task['status'] = meta.get('status')
                        tasks.append(task)
                        for code in issues:
                            diagnostic(code, project_id, task_id, relative)
                    except (ReadFailure, UnicodeError) as exc:
                        diagnostic(str(exc) if isinstance(exc, ReadFailure) else 'invalid_utf8',
                                   project_id, path=relative)
        except (ReadFailure, UnicodeError) as exc:
            diagnostic(str(exc) if isinstance(exc, ReadFailure) else 'invalid_utf8',
                       path='.AI/project.json' if root else None)
        finally:
            if root is not None:
                root.close()
    keys = {}
    for task in tasks:
        key = task['project_id'], task['task_id']
        keys.setdefault(key, []).append(task)
    for (project_id, task_id), group in keys.items():
        code = 'duplicate_task_key' if len(group) > 1 else None
        for task in group:
            for problem in (code, 'project_identity_conflict' if projects[project_id] > 1 else None):
                if problem and problem not in task['inconsistencies']:
                    task['inconsistencies'].append(problem)
                    diagnostic(problem, project_id, task_id, task['owner_ref'])
    tasks.sort(key=lambda t: (t['project_id'], t['task_id'], t['owner_ref']))
    return dict(schema_version=1, as_of=_utc(now),
                coverage='partial' if diagnostics else 'complete',
                tasks=tasks, diagnostics=diagnostics)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='append', required=True, help='explicit client root (repeatable)')
    parser.add_argument('--json', action='store_true', help='emit v1 JSON instead of a human table')
    args = parser.parse_args(argv)
    snapshot = read_snapshot(args.root)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, allow_nan=False))
    else:
        def display(value):
            text = 'unknown' if value is None else str(value)
            return ''.join(c if c.isprintable() else ' ' for c in text)
        print('PROJECT\tTASK\tTITLE\tPHASE\tSTATUS\tOWNER\tUPDATED / BY\tAGE (s)\tASSIGNMENTS\tBLOCKER\tNEXT STEP\tHANDOFF\tEVIDENCE\tEXCLUSIONS\tFILE')
        for task in snapshot['tasks']:
            step = task['next_step']
            next_step = (str(step.get('actor')) + ': ' + str(step.get('action'))
                         if isinstance(step, dict) else '')
            runs = '; '.join('{id}: {role}, {provider}/{model}, {phase}/{state}, observation unknown'.format(
                **{k: display(v) for k, v in run.items()}) for run in task['assignments'])
            checks = '; '.join(item['type'] + ': ' + item['result'] + ', hash ' + item['hash_status'] +
                               ', ' + item['ref'] for item in task['evidence'])
            cells = (task['project_id'], task['task_id'], task['title'], task['phase'],
                     task['status'], task['owner'], task['updated_at'] + ' / ' + task['updated_by'],
                     task['age_seconds'], runs, task['blocker'], next_step, task['handoff'],
                     checks, task['excluded_phases'], task['owner_ref'])
            print('\t'.join(display(c) for c in cells))
        for item in snapshot['diagnostics']:
            print('! ' + item['code'] + (' [' + display(item['path']) + ']' if 'path' in item else ''))
        print('Coverage: ' + snapshot['coverage'])
    return 3 if snapshot['coverage'] == 'partial' else 0


if __name__ == '__main__':
    raise SystemExit(main())
