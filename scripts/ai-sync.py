#!/usr/bin/env python3
"""Plan, apply and recover updates to an existing .AI client (stdlib, POSIX)."""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import uuid

# Cache the pinned helper pair once; installed scripts may change during replay.
spec = importlib.util.spec_from_file_location('ai_sync_bootstrap', Path(__file__).with_name('ai-bootstrap.py'))
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)
Invalid = ab.Invalid
JOURNAL = '.ai-bootstrap/sync.json'
START = '.AI/START.md'
CORE = ('schema_version', 'layout_version', 'project_id', 'project_type', 'adapters')


def digest(value):
    return ab.sha(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())


def refuse_other(root):
    for path in (ab.JOURNAL, ab.MIGRATION_JOURNAL):
        if ab.descriptor(root, path) is not None:
            raise Invalid('Pending bootstrap/migration transaction')


def regular(root, path):
    desc = ab.descriptor(root, path)
    if desc is not None and (desc['kind'] != 'file' or desc['mode'] & ~0o777):
        raise Invalid(f'Unsafe regular file/mode: {path}')
    return desc


def metadata(root, outputs=True):
    config = ab.json_file(root, ab.PROJECT)
    state = ab.load_state(root)
    if any(type(state.get(k)) is not int for k in ('schema_version', 'layout_version')):
        raise Invalid('Invalid state versions')
    for key, allowed in (('project_type', ab.TYPES), ('adapters', ab.ADAPTERS)):
        entries = config.get(key)
        if (not isinstance(entries, list) or any(not isinstance(p, str) or p not in allowed for p in entries)
                or len(entries) != len(set(entries))):
            raise Invalid(f'Invalid project {key}')
    if type(config.get('schema_version')) is not int or type(config.get('layout_version')) is not int:
        raise Invalid('Invalid project versions')
    intent = ab.json_file(root, ab.INTENT)
    if any(type(intent.get(k)) is not int for k in ('schema_version', 'layout_version')):
        raise Invalid('Invalid intent versions')
    if any(intent.get(k) != config.get(k) for k in CORE):
        raise Invalid('Intent identity/configuration differs')
    exclusions = []
    for key in ('local_only', 'skip_sync', 'overrides'):
        entries = intent.get(key)
        if not isinstance(entries, list) or any(not isinstance(p, str) for p in entries) or len(entries) != len(set(entries)):
            raise Invalid(f'Invalid intent {key}')
        for p in entries:
            ab.destination(p)
            if any(c in p for c in '*?[]'):
                raise Invalid('Exclusions are exact paths')
        exclusions.extend(entries)
    if len(exclusions) != len(set(exclusions)):
        raise Invalid('Overlapping exclusions')
    locals_ = state.get('local_files', [])
    if len(locals_) != len(set(locals_)) or not set(locals_) <= set(intent['local_only']):
        raise Invalid('Registered local ownership cannot be forgotten')
    if set(intent['local_only']) & set(state['source_files']):
        raise Invalid('Local ownership overlaps source state')
    if not set(intent['overrides']) <= set(state['source_files']):
        raise Invalid('Override requires a known upstream base')
    ab.validate_paths(list(state['source_files']) + exclusions)
    ab.validate_paths([ab.destination(p) for p in set(state['source_files']) | set(exclusions)])
    for path in set(state['source_files']) | set(intent['local_only']):
        if regular(root, ab.destination(path)) is None:
            raise Invalid(f'Local deletion: {path}')
    sync = state.get('sync')
    if sync is not None and (not isinstance(sync, dict) or type(sync.get('schema_version')) is not int or sync.get('schema_version') != 1):
        raise Invalid('Unknown sync metadata')
    expected = sync.get('managed_start_sha256') if sync is not None else state.get('bootstrap_files', {}).get(START)
    start_bytes = ab.read_file(root, START)
    intact = ab.sha(start_bytes) == expected
    # Published migration appended this deterministic catalogue without updating
    # its bootstrap receipt. Prove the original prefix, never bless raw current bytes.
    if not intact and sync is None and isinstance(state.get('migration'), dict):
        catalogue = '\n'.join(f'- [{ab.destination(p)}]({ab.destination(p)[4:]})' for p in sorted(locals_))
        suffix = ('\n## Local project sources\n' + catalogue + '\n').encode()
        intact = start_bytes.endswith(suffix) and ab.sha(start_bytes[:-len(suffix)]) == expected
    if not re.fullmatch('[0-9a-f]{64}', str(expected)) or not intact:
        raise Invalid('Managed START was edited')
    ab.assert_layout(root)
    ab.assert_links(root)
    if outputs:
        ab.assert_outputs(root, state)
    ab.private_tracked(root)
    ab.private_ignored(root)
    return config, intent, state


@contextlib.contextmanager
def reader(root):
    import fcntl
    path = ab.checked(root, ab.TXN + '/lock')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Invalid('Unsafe lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Invalid('Another writer is active') from error
        yield
    finally:
        os.close(fd)


def healthy(root):
    refuse_other(root)
    if ab.descriptor(root, JOURNAL) is not None:
        raise Invalid('Pending sync: recover first')
    metadata(root)
    return ab.check(root)


def proposal(args, root):
    refuse_other(root)
    if ab.descriptor(root, JOURNAL) is not None:
        raise Invalid('Pending sync: recover first')
    config, intent, old = metadata(root)
    get, origin = ab.loader(args)
    raw = get('manifest.yaml')
    sections = ab.manifest(raw.decode())
    if any(t not in sections for t in config['project_type']):
        raise Invalid('Selected type absent from manifest')
    selected = sorted({p for t in ['universal'] + config['project_type'] for p in sections[t]})
    ab.validate_paths(selected)
    upstream = {p: get(p) for p in selected}
    template = get('templates/ai/START.md')
    if sum(map(len, upstream.values())) + len(raw) + len(template) > ab.MAX_PACKAGE:
        raise Invalid('Source package too large')
    if args.bundle:
        bundle = Path(args.bundle).resolve()
        for path in selected + ['manifest.yaml', 'templates/ai/START.md']:
            regular(bundle, path)
    all_paths = sorted(set(selected) | set(old['source_files']) | set(intent['local_only']))
    ab.validate_paths(all_paths)
    ab.validate_paths([ab.destination(p) for p in all_paths])
    state = copy.deepcopy(old)
    effective, changes, conflicts, file_changes = {}, [], [], {}
    for path in all_paths:
        target = ab.destination(path)
        cur = regular(root, target)
        base = old['source_files'].get(path)
        data = ab.read_file(root, target) if cur else None
        new = upstream.get(path)
        reason = None
        if path in intent['local_only']:
            cls = 'local-only'
            if new is not None:
                reason = 'ownership-conflict'
        elif path in intent['skip_sync']:
            cls = 'skipped'
        elif path in intent['overrides']:
            cls = 'override'
        elif base is None:
            cls = 'new'
            if cur is not None:
                reason = 'ownership-conflict'
        elif new is None:
            cls = 'removed-upstream'
        elif cur['sha256'] == base['sha256'] == ab.sha(new):
            cls = 'unchanged'
        elif cur['sha256'] == ab.sha(new):
            cls = 'converged'
        elif cur['sha256'] == base['sha256']:
            cls = 'update'
        elif ab.sha(new) == base['sha256']:
            cls = 'local-edit'
        else:
            cls, reason = 'conflict', 'upstream-and-local-edit'
        changes.append({'canonical': path, 'path': target, 'class': cls})
        if reason:
            conflicts.append({'canonical': path, 'path': target, 'reason': reason})
        if not reason and cls in ('new', 'update', 'converged', 'unchanged'):
            data = new
            state['source_files'][path] = {'path': target, 'sha256': ab.sha(new), 'blob_sha': ab.blob(new),
                                          'mode': '100755' if path.startswith('scripts/') else '100644'}
            mode = 0o755 if path.startswith('scripts/') else 0o644
            if cur != {'kind': 'file', 'sha256': ab.sha(new), 'mode': mode}:
                file_changes[target] = (new, mode)
        if data is not None and (base is not None or path in intent['local_only'] or cls == 'new'):
            effective[path] = data
    state['local_files'] = sorted(intent['local_only'])
    catalogue = '\n'.join(f'- [{ab.destination(p)}]({ab.destination(p)[4:]})' for p in effective if not p.startswith('scripts/'))
    text = template.decode()
    start = (text.replace('{{CATALOG}}', catalogue) if '{{CATALOG}}' in text else text + '\n' + catalogue + '\n').encode()
    outputs, ctx = ab.context(config, effective, start, ab.json_file(root, ab.POLICY))
    canonical = {p: {'sha256': ab.sha(v), 'blob_sha': ab.blob(v)} for p, v in upstream.items()}
    origin['manifest_sha256'] = ab.sha(raw)
    origin['bundle_sha256'] = digest({'sources': canonical, 'start_template': ab.sha(template)})
    state['source'], state['context'] = origin, ctx
    state['sync'] = {**state.get('sync', {}), 'schema_version': 1, 'managed_start_sha256': ab.sha(start)}
    file_changes.update({START: (start, 0o644), **{p: (v, 0o644) for p, v in outputs.items()}})
    # Preserve absence of optional metadata on an otherwise identical generation.
    if not state['local_files'] and 'local_files' not in old:
        del state['local_files']
    file_changes[ab.STATE] = (ab.encoded(state), 0o644)
    generation_changed = ab.read_file(root, ab.STATE) != ab.encoded(state)
    actions, dirs = [], set()
    for path, (data, mode) in file_changes.items():
        if ab.descriptor(root, path) == {'kind': 'file', 'sha256': ab.sha(data), 'mode': mode}:
            continue
        for parent in PurePosixPath(path).parents:
            if str(parent) != '.':
                dirs.add(str(parent))
    for path in sorted(dirs, key=lambda p: (p.count('/'), p)):
        before = ab.descriptor(root, path)
        if before is None:
            actions.append({'path': path, 'before': None, 'after': {'kind': 'dir'}})
        elif before != {'kind': 'dir'}:
            raise Invalid('Unsafe action parent')
    for path in sorted(file_changes, key=lambda p: (p == ab.STATE, p)):
        data, mode = file_changes[path]
        before = ab.descriptor(root, path)
        if before != {'kind': 'file', 'sha256': ab.sha(data), 'mode': mode} or (generation_changed and path in outputs):
            actions.append(ab.file_action(path, data, before, mode))
    mutated = {a['path'] for a in actions}
    inputs = {ab.PROJECT, ab.INTENT, ab.POLICY, ab.STATE, START, *old['context']['outputs'], *ab.LINKS,
              *(ab.destination(p) for p in effective)}
    guards = {p: ab.descriptor(root, p) for p in sorted(inputs - mutated)}
    value = {'schema_version': 1, 'kind': 'ai-sync', 'root': str(root), 'project_id': config['project_id'],
             'actions': actions, 'inputs': guards, 'state_before': base64.b64encode(ab.read_file(root, ab.STATE)).decode(), 'changes': changes, 'conflicts': conflicts,
             'source': origin, 'source_modes': ({p: ab.descriptor(Path(args.bundle).resolve(), p) for p in selected} if args.bundle else {}), 'effective_sha256': {p: ab.sha(v) for p, v in effective.items()}}
    value['plan_sha256'] = digest(value)
    return value


def valid_descriptor(desc, directory=False):
    if desc == {'kind': 'dir'} and directory:
        return
    if (not isinstance(desc, dict) or set(desc) != {'kind', 'sha256', 'mode'} or desc['kind'] != 'file'
            or not re.fullmatch('[0-9a-f]{64}', str(desc['sha256'])) or type(desc['mode']) is not int
            or desc['mode'] & ~0o777):
        raise Invalid('Invalid file descriptor')


def validate_future(root, journal):
    if (not isinstance(journal, dict) or type(journal.get('schema_version')) is not int or journal.get('schema_version') != 1
            or journal.get('kind') != 'ai-sync' or journal.get('root') != str(root)
            or journal.get('project_id') != ab.json_file(root, ab.PROJECT).get('project_id')):
        raise Invalid('Foreign sync journal')
    unsigned = {k: v for k, v in journal.items() if k != 'plan_sha256'}
    if digest(unsigned) != journal.get('plan_sha256') or journal.get('conflicts') != []:
        raise Invalid('Journal binding/conflicts invalid')
    actions = journal.get('actions')
    if not isinstance(actions, list) or not actions or actions[-1].get('path') != ab.STATE:
        raise Invalid('State must be last')
    pending = {}
    for a in actions:
        path = ab.safe_relative(a['path'])
        if path in pending:
            raise Invalid('Duplicate action')
        pending[path] = a
        valid_descriptor(a['after'], True)
        if a['before'] is not None:
            valid_descriptor(a['before'])
        if a['after']['kind'] == 'dir':
            if a['before'] is not None or set(a) != {'path', 'before', 'after'}:
                raise Invalid('Invalid parent creation')
        else:
            data = base64.b64decode(a['data'], validate=True)
            if len(data) > ab.MAX_FILE or ab.sha(data) != a['after']['sha256']:
                raise Invalid('Invalid action payload')
    def future(path):
        if path in pending:
            return base64.b64decode(pending[path]['data'], validate=True)
        return ab.read_file(root, path)
    state = json.loads(future(ab.STATE))
    config = ab.json_file(root, ab.PROJECT)
    ab.validate_state(state, config)
    old_bytes = base64.b64decode(journal['state_before'], validate=True)
    old = json.loads(old_bytes)
    ab.validate_state(old, config)
    if pending[ab.STATE]['before']['sha256'] != ab.sha(old_bytes):
        raise Invalid('Original state receipt differs')
    mutable = {'source', 'source_files', 'local_files', 'context', 'sync'}
    if {k: v for k, v in old.items() if k not in mutable} != {k: v for k, v in state.items() if k not in mutable}:
        raise Invalid('Protected state metadata changed')
    if not set(old['source_files']) <= set(state['source_files']):
        raise Invalid('Tracked source was forgotten')
    intent = ab.json_file(root, ab.INTENT)
    if state.get('local_files', []) != sorted(intent['local_only']):
        raise Invalid('Future local ownership differs')
    for p, receipt in state['source_files'].items():
        if receipt != old['source_files'].get(p):
            data = future(ab.destination(p))
            if receipt['sha256'] != ab.sha(data) or receipt['blob_sha'] != ab.blob(data):
                raise Invalid('New upstream receipt differs')
    if state['project_id'] != journal['project_id'] or state['source'] != journal['source']:
        raise Invalid('Future state binding differs')
    sources = {p: future(ab.destination(p)) for p in sorted(set(state['source_files']) | set(state.get('local_files', [])))}
    if {p: ab.sha(v) for p, v in sources.items()} != journal['effective_sha256']:
        raise Invalid('Future source receipts differ')
    if state.get('sync', {}).get('schema_version') != 1 or state['sync'].get('managed_start_sha256') != ab.sha(future(START)):
        raise Invalid('Future START receipt differs')
    outputs, ctx = ab.context(config, sources, future(START), ab.json_file(root, ab.POLICY))
    if ctx != state['context'] or any(future(p) != v for p, v in outputs.items()):
        raise Invalid('Future context differs')
    intent = ab.json_file(root, ab.INTENT)
    forbidden = set(intent['local_only']) | set(intent['skip_sync']) | set(intent['overrides'])
    allowed = {ab.destination(p) for p in state['source_files'] if p not in forbidden} | {START, ab.STATE, *outputs}
    parents = {str(parent) for p in allowed for parent in PurePosixPath(p).parents if str(parent) != '.'}
    ab.validate_paths(pending)
    expected_order = sorted((p for p in pending if pending[p]['after']['kind'] == 'dir'), key=lambda p: (p.count('/'), p))
    expected_order += sorted((p for p in pending if pending[p]['after']['kind'] == 'file'), key=lambda p: (p == ab.STATE, p))
    if list(pending) != expected_order:
        raise Invalid('Invalid action order')
    for path, a in pending.items():
        if a['after']['kind'] == 'dir':
            if path not in parents:
                raise Invalid('Unapproved parent')
        elif path not in allowed or a['after']['mode'] != (0o755 if path.startswith('scripts/') else 0o644):
            raise Invalid('Unapproved action')
    guards = journal.get('inputs')
    if not isinstance(guards, dict) or set(guards) & set(pending):
        raise Invalid('Invalid guards')
    expected_inputs = {ab.PROJECT, ab.INTENT, ab.POLICY, ab.STATE, START, *outputs, *ab.LINKS,
                       *(ab.destination(p) for p in sources)} - set(pending)
    if set(guards) != expected_inputs:
        raise Invalid('Missing/unapproved input guard')
    for path, desc in guards.items():
        if ab.descriptor(root, path) != desc:
            raise Invalid(f'Input drift: {path}')
    for a in actions:
        if ab.descriptor(root, a['path']) not in (a['before'], a['after']):
            raise Invalid(f'Action drift: {a["path"]}')
    ab.assert_links(root)
    ab.private_tracked(root)
    ab.private_ignored(root)
    return state


def install_action(root, action):
    """Single mutating step; documented seam for independent crash tests."""
    path, after = action['path'], action['after']
    if ab.descriptor(root, path) != action['before']:
        raise Invalid(f'Concurrent edit: {path}')
    target = ab.checked(root, path)
    if after['kind'] == 'dir':
        target.mkdir()
        ab.sync_dir(target.parent)
    else:
        ab.atomic_bytes(target, base64.b64decode(action['data'], validate=True), after['mode'])
    if ab.descriptor(root, path) != after:
        raise Invalid(f'Post-write drift: {path}')


def finish(root, journal, replay=False):
    validate_future(root, journal)
    for action in journal['actions']:
        # Recheck retained inputs before each step, including the state commit.
        for path, desc in journal['inputs'].items():
            if ab.descriptor(root, path) != desc:
                raise Invalid(f'Input drift: {path}')
        for previous in journal['actions']:
            if ab.descriptor(root, previous['path']) not in (previous['before'], previous['after']):
                raise Invalid(f'Action drift: {previous["path"]}')
        if ab.descriptor(root, action['path']) != action['after'] or (not replay and action['before'] == action['after']):
            install_action(root, action)
    metadata(root)
    state = ab.load_state(root)
    if ab.current_context(root, state)[1] != state['context']:
        raise Invalid('Final context differs')
    (root / JOURNAL).unlink()
    ab.sync_dir(root / ab.TXN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'apply', 'check', 'recover'))
    parser.add_argument('--root', required=True, type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--bundle', type=Path)
    source.add_argument('--source-base')
    parser.add_argument('--expect-plan')
    args = parser.parse_args(argv)
    root = args.root.absolute()
    try:
        if args.root.is_symlink() or not root.is_dir():
            raise Invalid('Use an existing actual client root')
        refuse_other(root)
        if args.command == 'plan':
            p = proposal(args, root)
            print(json.dumps({'status': 'planned' if p['actions'] else 'up-to-date', 'applicable': not p['conflicts'],
                              'plan_sha256': p['plan_sha256'], 'changes': p['changes'], 'conflicts': p['conflicts']}, ensure_ascii=False))
        elif args.command == 'apply':
            if not re.fullmatch('[0-9a-f]{64}', str(args.expect_plan)):
                raise Invalid('--expect-plan SHA256 is required')
            with ab.writer(root):
                p = proposal(args, root)
                if p['plan_sha256'] != args.expect_plan or p['conflicts']:
                    raise Invalid('Stale or conflicting plan')
                if p['actions']:
                    validate_future(root, p)
                    ab.atomic_bytes(root / JOURNAL, ab.encoded(p))
                    finish(root, p)
                else:
                    healthy(root)
            print(json.dumps({'status': 'applied' if p['actions'] else 'up-to-date'}))
        elif args.command == 'check':
            with reader(root):
                healthy(root)
            print(json.dumps({'status': 'ok'}))
        else:
            with ab.writer(root):
                refuse_other(root)
                if ab.descriptor(root, JOURNAL) is None:
                    healthy(root)
                else:
                    finish(root, ab.json_file(root, JOURNAL), replay=True)
            print(json.dumps({'status': 'recovered'}))
        return 0
    except (Invalid, OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        print(f'ai-sync: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
