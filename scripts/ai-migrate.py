#!/usr/bin/env python3
"""Explicit, recoverable legacy client migration. Linux/POSIX, Python 3.11+."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

spec = importlib.util.spec_from_file_location('ai_bootstrap', Path(__file__).with_name('ai-bootstrap.py'))
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)
Invalid = ab.Invalid
JOURNAL = '.ai-bootstrap/migration.json'
BACKUP = '.ai-bootstrap/legacy'
MOVES = {p: BACKUP + '/' + p for p in ('.claude', 'CLAUDE.md', 'AGENTS.md', 'docs/dev')}
GROUPS = {'rules': 'rules', 'skills': 'skills', 'agents': 'roles', 'commands': 'commands', 'memory': 'memory'}
REGISTRIES = {'canon.yaml', 'canon.intent.yaml', 'canon.state.json', 'canon.ledger.json'}


def tree(root, rel):
    """Receipt for an archive input: bytes, paths and permissions, no symlinks."""
    desc = ab.descriptor(root, rel)
    if desc is None:
        return None
    if desc['kind'] == 'file':
        return desc
    if desc['kind'] != 'dir':
        raise Invalid(f'Symlink archive input: {rel}')
    entries = {}
    folder = ab.checked(root, rel)
    for path in sorted(folder.rglob('*')):
        name = path.relative_to(root).as_posix()
        item = ab.descriptor(root, name)
        if item['kind'] not in ('dir', 'file'):
            raise Invalid(f'Symlink in legacy input: {name}')
        entries[path.relative_to(folder).as_posix()] = item
    ab.validate_paths([p for p, d in entries.items() if d['kind'] == 'file'])
    # Empty directory case collisions also matter.
    components = {}
    for p in entries:
        key = ab.path_key(p)
        if key in components and components[key] != p:
            raise Invalid(f'Case/Unicode legacy collision: {p}')
        components[key] = p
    return {'kind': 'tree', 'sha256': ab.sha(ab.encoded(entries))}


def legacy_registry(data):
    text = data.decode('utf-8')
    if text.lstrip().startswith(('{', '[')):
        raise Invalid('Unsupported legacy registry schema')
    files, hashes, exclusions = [], {}, set()
    current = None
    for raw in text.splitlines():
        line = raw.split(' #', 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if not line.startswith((' ', '\t')):
            key, sep, value = line.partition(':')
            if not sep:
                raise Invalid('Invalid legacy registry')
            current = key
            if key in ('schema_version', 'schema', 'layout_version'):
                raise Invalid('Versioned legacy registry requires a separate adapter')
        elif current == 'files' and line.strip().startswith('- '):
            files.append(line.strip()[2:].strip().strip('\"\''))
        elif current == 'file_hashes':
            path, sep, digest = line.strip().partition(':')
            digest = digest.strip().strip('\"\'')
            if not sep or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', digest):
                raise Invalid('Unsupported legacy baseline hash')
            hashes[path.strip('\"\'')] = digest
        elif current in ('local_only', 'overrides', 'skip_sync') and line.strip().startswith('- '):
            exclusions.add(line.strip()[2:].strip().strip('\"\''))
    if not files:
        raise Invalid('Legacy registry has no files list')
    for path in set(files) | set(hashes) | exclusions:
        ab.destination(path)
    ab.validate_paths(files)
    return hashes, exclusions


def payload(step):
    return base64.b64decode(step['data'], validate=True)


def migration_plan(args, root):
    ab.private_tracked(root)
    if ab.descriptor(root, JOURNAL) is not None or ab.descriptor(root, ab.JOURNAL) is not None:
        raise Invalid('Unfinished transaction: use the matching recover procedure')
    with tempfile.TemporaryDirectory(prefix='ai-migrate-plan-') as temp:
        fresh_actions, fresh_state = ab.package(args, Path(temp) / 'client')
    config = json.loads(payload(next(a for a in fresh_actions if a['path'] == ab.PROJECT)))
    if ab.descriptor(root, '.AI') is not None:
        state = check_migration(root)
        if (not state.get('migration') or ab.json_file(root, ab.PROJECT) != config or
                state['source'] != fresh_state['source'] or state['source_files'] != fresh_state['source_files']):
            raise Invalid('Existing .AI is not this migration configuration')
        return None
    if not root.is_dir() or ab.descriptor(root, '.claude') != {'kind': 'dir'}:
        raise Invalid('Migration requires an existing legacy .claude directory')
    if ab.descriptor(root, '.agents') is not None:
        raise Invalid('Existing .agents requires a separate migration plan')
    if ab.descriptor(root, BACKUP) is not None:
        raise Invalid('Existing migration backup; refusing to overwrite')
    for name in REGISTRIES - {'canon.yaml'}:
        if ab.descriptor(root, '.claude/' + name) is not None:
            raise Invalid('Split legacy registry is not supported by this migration adapter')
    hashes, exclusions = legacy_registry(ab.read_file(root, '.claude/canon.yaml'))
    context = ab.read_file(root, 'CLAUDE.md').decode('utf-8')
    archive_inputs = {p: tree(root, p) for p in MOVES if ab.descriptor(root, p) is not None}
    files = {a['path']: (payload(a), a['after']['mode']) for a in fresh_actions
             if a['after']['kind'] == 'file' and a['path'] not in ('.gitignore', ab.STATE)}
    local_files, retained = set(), []
    for group, dest in GROUPS.items():
        folder = root / '.claude' / group
        if not folder.exists():
            continue
        for path in folder.rglob('*'):
            if not path.is_file():
                continue
            relative = path.relative_to(folder).as_posix()
            old_rel = path.relative_to(root).as_posix()
            canon = group + '/' + relative
            target = '.AI/' + dest + '/' + relative
            data = ab.read_file(root, old_rel)
            mode = ab.descriptor(root, old_rel)['mode']
            if not 0 <= mode <= 0o777:
                raise Invalid(f'Unsupported legacy permissions: {old_rel}')
            known_clean = hashes.get(canon) in (ab.sha(data), ab.blob(data), hashlib.sha1(data).hexdigest())
            if group == 'memory' or target not in files or not known_clean or canon in exclusions:
                files[target] = (data, mode)
                if group != 'memory':
                    if canon not in fresh_state['source_files']:
                        local_files.add(canon)
                    else:
                        retained.append(canon)
    # Native CLI-local files survive at their original routes; registries are archived.
    links = dict(ab.LINKS)
    for path in (root / '.claude').rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(root / '.claude').as_posix()
        top = rel.partition('/')[0]
        if top in GROUPS or rel in REGISTRIES:
            continue
        target = '.claude/' + rel
        if top == 'agent-memory':
            target = '.AI/memory/' + rel
            if target in files:
                raise Invalid(f'Agent memory collision: {target}')
            links['.claude/agent-memory'] = '../.AI/memory/agent-memory'
        files[target] = (ab.read_file(root, '.claude/' + rel), ab.descriptor(root, '.claude/' + rel)['mode'])
    # Existing root scripts are updated only with a proven clean baseline.
    for canon in fresh_state['source_files']:
        if not canon.startswith('scripts/') or ab.descriptor(root, canon) is None:
            continue
        data = ab.read_file(root, canon)
        if hashes.get(canon) not in (ab.sha(data), ab.blob(data), hashlib.sha1(data).hexdigest()) or canon in exclusions:
            files[canon] = (data, ab.descriptor(root, canon)['mode'])
            retained.append(canon)
        files[BACKUP + '/files/' + canon] = (data, ab.descriptor(root, canon)['mode'])
    if 'docs/dev' in archive_inputs:
        for path in (root / 'docs/dev').rglob('*'):
            if path.is_file():
                rel = 'docs/' + path.relative_to(root / 'docs/dev').as_posix()
                if ab.descriptor(root, rel) is not None or rel in files:
                    raise Invalid(f'Docs flatten collision: {rel}')
                files[rel] = (ab.read_file(root, path.relative_to(root).as_posix()), ab.descriptor(root, path.relative_to(root).as_posix())['mode'])
    for old, new in GROUPS.items():
        context = context.replace('.claude/' + old + '/', '.AI/' + new + '/')
    context = context.replace('docs/dev/', 'docs/')
    files['.AI/project.md'] = (context.encode(), 0o600)
    if 'AGENTS.md' in archive_inputs:
        if '.AI/memory/legacy-agent-context.md' in files:
            raise Invalid('Memory collision: legacy-agent-context.md')
        files['.AI/memory/legacy-agent-context.md'] = (ab.read_file(root, 'AGENTS.md'), 0o600)
        # Preserve legacy memory bytes exactly; add a separate context route instead.
        files['.AI/project.md'] = (context.encode() + b'\nRead .AI/memory/legacy-agent-context.md for prior agent instructions.\n', 0o600)
    start, mode = files['.AI/START.md']
    local_catalog = '\n'.join(f'- [{ab.destination(p)}]({ab.destination(p)[4:]})' for p in sorted(local_files))
    files['.AI/START.md'] = (start + ('\n## Local project sources\n' + local_catalog + '\n').encode(), mode)
    fresh_state['local_files'] = sorted(local_files)
    intent = json.loads(files[ab.INTENT][0])
    intent['local_only'] = sorted(local_files)
    intent['overrides'] = sorted(set(retained))
    files[ab.INTENT] = (ab.encoded(intent), 0o644)
    sources = {p: files[ab.destination(p)][0] for p in list(fresh_state['source_files']) + sorted(local_files)}
    outputs, receipt = ab.context(config, sources, files['.AI/START.md'][0], json.loads(files[ab.POLICY][0]))
    for p, data in outputs.items():
        files[p] = (data, 0o644)
    fresh_state['context'] = receipt
    fresh_state['migration'] = {'schema_version': 1, 'backup': BACKUP, 'retained': sorted(set(retained)), 'archives': archive_inputs}
    before_ignore = ab.descriptor(root, '.gitignore')
    ignore = ab.read_file(root, '.gitignore') if before_ignore else b''
    if before_ignore:
        files[BACKUP + '/.gitignore'] = (ignore, before_ignore['mode'])
    files['.gitignore'] = (ignore + ab.IGNORE.encode(), 0o644)
    files[ab.STATE] = (ab.encoded(fresh_state), 0o644)
    ab.validate_paths(files)
    # Include empty legacy directories as well as file parents.
    dirs = {'.AI/' + p for p in GROUPS.values()} | {'docs/backlog', 'docs/done', '.agents', '.claude'}
    for path in (root / '.claude').rglob('*'):
        if path.is_dir():
            rel = path.relative_to(root / '.claude').as_posix()
            top, sep, tail = rel.partition('/')
            if top in GROUPS:
                dirs.add('.AI/' + GROUPS[top] + ('/' + tail if sep else ''))
    for rel in list(files) + list(links) + list(dirs):
        dirs.update(str(p) for p in PurePosixPath(rel).parents if str(p) not in ('.', ab.TXN))
    for rel in dirs:
        if rel in files or rel in links:
            raise Invalid(f'File/directory collision: {rel}')
        old = ab.descriptor(root, rel)
        if old is not None and old != {'kind': 'dir'}:
            raise Invalid(f'Directory collision: {rel}')
    steps = []
    # Ensure backup parents before archive moves.
    pre_dirs = sorted({BACKUP, BACKUP + '/docs'}, key=lambda p: (p.count('/'), p))
    for rel in pre_dirs:
        steps.append({'kind': 'dir', 'path': rel, 'before': None, 'after': {'kind': 'dir'}})
    for src, desc in archive_inputs.items():
        steps.append({'kind': 'move', 'path': src, 'destination': MOVES[src], 'receipt': desc})
    for rel in sorted(dirs - set(pre_dirs), key=lambda p: (p.count('/'), p)):
        before = None if rel == '.claude' or rel.startswith('.claude/') else ab.descriptor(root, rel)
        steps.append({'kind': 'dir', 'path': rel, 'before': before, 'after': {'kind': 'dir'}})
    ordered_files = sorted(files.items(), key=lambda item: (0 if item[0].startswith(BACKUP + '/') else 1 if item[0] == '.gitignore' else 2, item[0]))
    for rel, (data, mode) in ordered_files:
        if rel == ab.STATE:
            continue
        moved = rel.startswith('.claude/') or rel in archive_inputs
        before = None if moved else ab.descriptor(root, rel)
        if before is not None and not (rel == '.gitignore' or rel.startswith('scripts/')):
            raise Invalid(f'Would overwrite user file: {rel}')
        steps.append({'kind': 'file', **ab.file_action(rel, data, before, mode)})
    for rel, target in links.items():
        steps.append({'kind': 'link', 'path': rel, 'before': None, 'after': {'kind': 'link', 'target': target}})
    steps.append({'kind': 'file', **ab.file_action(ab.STATE, files[ab.STATE][0])})
    journal = {'schema_version': 1, 'steps': steps, 'state': fresh_state}
    validate_journal(journal)
    preflight(root, journal)
    return journal


def validate_journal(journal):
    if not isinstance(journal, dict) or journal.get('schema_version') != 1:
        raise Invalid('Unknown migration journal schema')
    steps = journal.get('steps')
    if not isinstance(steps, list) or not steps or any(not isinstance(s, dict) for s in steps) or steps[-1].get('path') != ab.STATE or steps[-1].get('kind') != 'file':
        raise Invalid('Migration state must be committed last')
    state = journal.get('state')
    ab.validate_state(state)
    seen, moves, directories = set(), set(), {ab.TXN}
    installation_started = False
    file_steps = {}
    for step in steps:
        rel = step.get('path')
        ab.safe_relative(rel)
        kind = step.get('kind')
        if kind == 'move':
            if rel not in MOVES or step.get('destination') != MOVES[rel] or rel in moves:
                raise Invalid('Unsafe migration archive move')
            if installation_started:
                raise Invalid('Archive moves must precede installation')
            if str(PurePosixPath(step['destination']).parent) not in directories:
                raise Invalid('Missing archive destination parent')
            moves.add(rel)
            desc = step.get('receipt', {})
            if not isinstance(desc, dict) or desc.get('kind') not in ('file', 'tree') or not re.fullmatch(r'[0-9a-f]{64}', str(desc.get('sha256', ''))):
                raise Invalid('Invalid archive receipt')
            continue
        if ab.path_key(rel) in seen:
            raise Invalid('Duplicate migration target')
        seen.add(ab.path_key(rel))
        if not rel.startswith(BACKUP):
            installation_started = True
        parent = str(PurePosixPath(rel).parent)
        if parent != '.' and parent not in directories:
            raise Invalid(f'Missing/late journal parent: {rel}')
        allowed = (rel.startswith(('.AI/', '.claude/', 'scripts/', 'docs/', BACKUP + '/')) or
                   rel in ('.AI', '.claude', '.agents', '.agents/skills', 'scripts', 'docs', BACKUP, 'CLAUDE.md', 'AGENTS.md', '.gitignore'))
        if not allowed or rel == 'docs/dev' or rel.startswith('docs/dev/'):
            raise Invalid(f'Unsafe migration target: {rel}')
        after = step.get('after', {})
        before = step.get('before')
        if not isinstance(after, dict) or kind != after.get('kind') or kind not in ('file', 'dir', 'link'):
            raise Invalid('Invalid migration descriptor')
        if before is not None and (not isinstance(before, dict) or before.get('kind') != kind):
            raise Invalid('Invalid migration before receipt')
        if kind == 'dir':
            if after != {'kind': 'dir'} or before not in (None, {'kind': 'dir'}):
                raise Invalid('Invalid migration directory descriptor')
            directories.add(rel)
        if kind == 'link':
            links = {**ab.LINKS, '.claude/agent-memory': '../.AI/memory/agent-memory'}
            if rel not in links or links[rel] != after.get('target') or before is not None:
                raise Invalid('Unsafe compatibility link')
        elif kind == 'file':
            if type(after.get('mode')) is not int or not 0 <= after['mode'] <= 0o777 or ab.sha(payload(step)) != after.get('sha256'):
                raise Invalid('Invalid migration bytes/mode')
            if before is not None and rel != '.gitignore' and not rel.startswith('scripts/'):
                raise Invalid('Unexpected migration overwrite')
            file_steps[rel] = step
    if '.claude' not in moves or 'CLAUDE.md' not in moves:
        raise Invalid('Missing mandatory legacy archives')
    required_files = {ab.PROJECT, ab.POLICY, ab.INTENT, ab.STATE, '.AI/START.md', '.AI/project.md', '.AI/memory/MEMORY.md'}
    if not required_files <= set(file_steps):
        raise Invalid('Missing mandatory migration metadata/memory file')
    if json.loads(payload(file_steps[ab.STATE])) != state:
        raise Invalid('Migration state differs from final bytes')
    required_dirs = {'.AI/' + p for p in GROUPS.values()} | {'docs/backlog', 'docs/done', '.claude', '.agents'}
    if not required_dirs <= directories:
        raise Invalid('Missing mandatory migration directory')
    planned_links = {s['path']: s['after']['target'] for s in steps if s['kind'] == 'link'}
    if any(planned_links.get(p) != target for p, target in ab.LINKS.items()):
        raise Invalid('Missing mandatory compatibility link')
    recorded_archives = {s['path']: s['receipt'] for s in steps if s['kind'] == 'move'}
    migration = state.get('migration')
    if not isinstance(migration, dict) or migration.get('schema_version') != 1 or migration.get('backup') != BACKUP:
        raise Invalid('Invalid migration metadata')
    retained = migration.get('retained')
    if not isinstance(retained, list) or any(not isinstance(p, str) or p not in state['source_files'] for p in retained):
        raise Invalid('Invalid retained source list')
    if migration.get('archives') != recorded_archives:
        raise Invalid('Archive receipts differ from state')
    config = json.loads(payload(file_steps[ab.PROJECT]))
    ab.validate_state(state, config)
    local_files = state.get('local_files', [])
    if not isinstance(local_files, list) or set(local_files) & set(state['source_files']):
        raise Invalid('Invalid local-only source metadata')
    ab.validate_paths(list(state['source_files']) + local_files)
    source_paths = list(state['source_files']) + local_files
    if any(ab.destination(p) not in file_steps for p in source_paths):
        raise Invalid('Missing migration source payload')
    sources = {p: payload(file_steps[ab.destination(p)]) for p in source_paths}
    outputs, receipt = ab.context(config, sources, payload(file_steps['.AI/START.md']), json.loads(payload(file_steps[ab.POLICY])))
    if receipt != state['context'] or any(payload(file_steps[p]) != data for p, data in outputs.items()):
        raise Invalid('Migration context receipt differs from source generation')


def preflight(root, journal):
    validate_journal(journal)
    ab.private_tracked(root)
    pending = set()
    for step in journal['steps']:
        if step['kind'] != 'move':
            continue
        backup = tree(root, step['destination'])
        if backup is None:
            if tree(root, step['path']) != step['receipt']:
                raise Invalid(f'Legacy input changed: {step["path"]}')
            pending.add(step['path'])
        elif backup != step['receipt']:
            raise Invalid(f'Backup was edited: {step["destination"]}')
        elif step['path'] == 'docs/dev' and ab.descriptor(root, 'docs/dev') is not None:
            raise Invalid('Legacy docs recreated during migration')
    file_steps = {s['path']: s for s in journal['steps'] if s['kind'] == 'file'}
    state = journal['state']
    declared_moves = {s['path'] for s in journal['steps'] if s['kind'] == 'move'}
    if 'docs/dev' not in declared_moves and ab.descriptor(root, 'docs/dev') is not None:
        raise Invalid('Missing archive of existing docs/dev')
    if 'AGENTS.md' not in declared_moves and ab.descriptor(root, 'AGENTS.md') is not None:
        desired = file_steps.get('AGENTS.md')
        if desired is None or ab.descriptor(root, 'AGENTS.md') != desired['after']:
            raise Invalid('Missing archive of existing AGENTS.md')
    legacy_prefix = '.claude' if '.claude' in pending else MOVES['.claude']
    legacy_dir = ab.checked(root, legacy_prefix)
    for path in legacy_dir.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(legacy_dir).as_posix()
        top, sep, tail = relative.partition('/')
        canon = top + '/' + tail
        target = None
        if top in GROUPS and (top == 'memory' or canon not in state['source_files'] or canon in state.get('local_files', []) or canon in state['migration']['retained']):
            target = '.AI/' + GROUPS[top] + '/' + tail
        elif top not in GROUPS and relative not in REGISTRIES:
            target = '.AI/memory/' + relative if top == 'agent-memory' else '.claude/' + relative
        if target is not None:
            if target not in file_steps or payload(file_steps[target]) != ab.read_file(root, legacy_prefix + '/' + relative):
                raise Invalid(f'Incomplete legacy preservation: {relative}')
    # Original root inputs and overwritten scripts have explicit byte-for-byte archives.
    for s in journal['steps']:
        if s['kind'] == 'file' and s['before'] is not None and s['before']['kind'] == 'file':
            archive = BACKUP + ('/files/' + s['path'] if s['path'].startswith('scripts/') else '/' + s['path'])
            if archive not in file_steps or ab.sha(payload(file_steps[archive])) != s['before']['sha256']:
                raise Invalid('Missing overwrite backup bytes')
    for step in journal['steps']:
        if step['kind'] == 'move':
            continue
        rel = step['path']
        if any(rel == p or rel.startswith(p + '/') for p in pending):
            continue
        current = ab.descriptor(root, rel)
        if current != step['before'] and current != step['after']:
            raise Invalid(f'Migration conflict: {rel}; preserved')


def install_step(root, step):
    """A single replayable step; fault tests may interrupt after a successful call."""
    rel = step['path']
    if step['kind'] == 'move':
        if tree(root, step['destination']) == step['receipt']:
            return
        if tree(root, rel) != step['receipt'] or ab.descriptor(root, step['destination']) is not None:
            raise Invalid(f'Concurrent archive edit: {rel}')
        source, dest = ab.checked(root, rel), ab.checked(root, step['destination'])
        os.rename(source, dest)
        ab.sync_dir(source.parent)
        ab.sync_dir(dest.parent)
        return
    current = ab.descriptor(root, rel)
    if current == step['after']:
        return
    if current != step['before']:
        raise Invalid(f'Concurrent migration edit: {rel}')
    path = ab.checked(root, rel)
    if step['kind'] == 'dir':
        path.mkdir()
        ab.sync_dir(path.parent)
    elif step['kind'] == 'file':
        ab.atomic_bytes(path, payload(step), step['after']['mode'])
    else:
        path.symlink_to(step['after']['target'], target_is_directory=True)
        ab.sync_dir(path.parent)


def finish(root, journal):
    preflight(root, journal)
    for step in journal['steps']:
        install_step(root, step)
    state = ab.load_state(root)
    ab.assert_layout(root)
    ab.assert_links(root)
    ab.assert_outputs(root, state)
    if ab.current_context(root, state)[1] != state['context']:
        raise Invalid('Sources changed during migration; journal retained')
    ab.private_tracked(root)
    ab.private_ignored(root)
    ab.checked(root, JOURNAL).unlink()
    ab.sync_dir(root / ab.TXN)


def check_migration(root):
    state = ab.check(root)
    migration = state.get('migration')
    if not isinstance(migration, dict) or migration.get('schema_version') != 1 or migration.get('backup') != BACKUP:
        raise Invalid('This client has no supported completed migration')
    archives = migration.get('archives')
    if not isinstance(archives, dict) or not {'.claude', 'CLAUDE.md'} <= set(archives):
        raise Invalid('Invalid migration archive metadata')
    for path, expected in archives.items():
        if path not in MOVES or tree(root, MOVES[path]) != expected:
            raise Invalid(f'Migration archive missing/changed: {path}')
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'apply', 'recover', 'check'))
    parser.add_argument('--root', required=True, type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--bundle', type=Path)
    source.add_argument('--source-base')
    parser.add_argument('--project-id')
    parser.add_argument('--types', default='')
    parser.add_argument('--adapters', default='claude,codex,kimi')
    args = parser.parse_args(argv)
    root = args.root.absolute()
    try:
        if args.root.is_symlink():
            raise Invalid('Use the actual client root')
        if args.command in ('plan', 'apply'):
            journal = migration_plan(args, root)
            if journal is None:
                print(json.dumps({'status': 'up-to-date'}))
                return 0
            if args.command == 'plan':
                print(json.dumps({'status': 'planned', 'project_id': journal['state']['project_id'],
                                  'source': journal['state']['source'],
                                  'backup': BACKUP, 'retained': journal['state']['migration']['retained'],
                                  'local_only': journal['state']['local_files'],
                                  'steps': [{'kind': s['kind'], 'path': s['path']} for s in journal['steps']]}, ensure_ascii=False))
                return 0
            with ab.writer(root):
                if ab.descriptor(root, JOURNAL) is not None or ab.descriptor(root, ab.JOURNAL) is not None:
                    raise Invalid('Unfinished transaction; recover first')
                preflight(root, journal)
                ab.atomic_bytes(root / JOURNAL, ab.encoded(journal))
                finish(root, journal)
            print(json.dumps({'status': 'migrated', 'backup': BACKUP}))
        elif args.command == 'recover':
            if ab.descriptor(root, JOURNAL) is None:
                print(json.dumps({'status': 'no-transaction'}))
                return 0
            journal = ab.json_file(root, JOURNAL)
            preflight(root, journal)
            with ab.writer(root):
                finish(root, ab.json_file(root, JOURNAL))
            print(json.dumps({'status': 'recovered', 'backup': BACKUP}))
        else:
            check_migration(root)
            print(json.dumps({'status': 'ok'}))
        return 0
    except (Invalid, OSError, ValueError, KeyError, TypeError) as error:
        print(f'ai-migrate: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
