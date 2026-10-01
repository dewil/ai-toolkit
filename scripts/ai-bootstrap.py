#!/usr/bin/env python3
"""New client bootstrap and recoverable context builder. Python 3.11+, stdlib.

No legacy migration, automatic provider configuration, or vector-service writes.
Use an explicit bundle for development; production sources are pinned HTTPS URLs.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import unicodedata
import urllib.request
import uuid

VERSION = 2
LAYOUT = 1
STATE = '.AI/canon/canon.state.json'
PROJECT = '.AI/project.json'
INTENT = '.AI/canon/canon.intent.yaml'
POLICY = '.AI/canon/context-policy.json'
TXN = '.ai-bootstrap'
JOURNAL = TXN + '/journal.json'
MIGRATION_JOURNAL = TXN + '/migration.json'
TYPES = {'coding', 'wiki', 'management', 'education', 'documentation'}
ADAPTERS = {'claude', 'codex', 'kimi'}
TEMPLATES = ('START.md', 'project.md', 'MEMORY.md', 'context-policy.json')
LINKS = {'.claude/' + a: '../.AI/' + b for a, b in (
    ('rules', 'rules'), ('skills', 'skills'), ('agents', 'roles'),
    ('commands', 'commands'), ('memory', 'memory'))}
LINKS['.agents/skills'] = '../.AI/skills'
IGNORE = ('\n# ai-bootstrap: private and local files\n/.AI/memory/\n'
          '/.AI/project.md\n/.AI/canon/\n/.ai-bootstrap/\n'
          '/.claude/settings.local.json\n')
MAX_FILE = 4 * 1024 * 1024
MAX_PACKAGE = 32 * 1024 * 1024


class Invalid(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()


def path_key(path):
    return unicodedata.normalize('NFC', path).casefold()


def safe_relative(rel):
    if not isinstance(rel, str) or not rel or len(rel) > 1024:
        raise Invalid('Invalid relative path')
    if '\\' in rel or ':' in rel or any(ord(c) < 32 for c in rel):
        raise Invalid(f'Unsafe path: {rel!r}')
    parts = rel.split('/')
    if rel.startswith('/') or any(p in ('', '.', '..') for p in parts):
        raise Invalid(f'Unsafe path: {rel!r}')
    if any(p.rstrip(' .') != p or p.split('.')[0].upper() in
           {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
            *(f'LPT{i}' for i in range(1, 10))} for p in parts):
        raise Invalid(f'Nonportable path: {rel!r}')
    return rel


def destination(rel):
    safe_relative(rel)
    top, sep, tail = rel.partition('/')
    if not sep or top not in ('rules', 'skills', 'agents', 'commands', 'scripts'):
        raise Invalid(f'Not a canon source: {rel}')
    return rel if top == 'scripts' else '.AI/' + ('roles' if top == 'agents' else top) + '/' + tail


def checked(root, rel):
    """Never follow an intermediate symlink, even one resolving inside root."""
    safe_relative(rel)
    current = root
    parts = PurePosixPath(rel).parts
    for index, part in enumerate(parts):
        if current.is_dir():
            for sibling in current.iterdir():
                if sibling.name != part and path_key(sibling.name) == path_key(part):
                    raise Invalid(f'Case/Unicode collision with existing path: {sibling}')
        current /= part
        if index < len(parts) - 1 and (current.is_symlink() or (current.exists() and not current.is_dir())):
            raise Invalid(f'Unsafe parent: {current}')
    return root / rel


def descriptor(root, rel):
    path = checked(root, rel)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return {'kind': 'link', 'target': os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        return {'kind': 'dir'}
    if not stat.S_ISREG(info.st_mode):
        raise Invalid(f'Unsupported filesystem object: {rel}')
    return {'kind': 'file', 'sha256': sha(path.read_bytes()), 'mode': stat.S_IMODE(info.st_mode)}


def read_file(root, rel):
    desc = descriptor(root, rel)
    if not desc or desc['kind'] != 'file':
        raise Invalid(f'Missing regular source: {rel}')
    return checked(root, rel).read_bytes()


def json_file(root, rel):
    try:
        value = json.loads(read_file(root, rel))
    except (ValueError, UnicodeError) as error:
        raise Invalid(f'Invalid JSON: {rel}: {error}') from error
    if not isinstance(value, dict):
        raise Invalid(f'Expected object: {rel}')
    return value


def selection(value, allowed, label):
    entries = value.split(',') if value else []
    if any(v not in allowed for v in entries):
        raise Invalid(f'Unknown {label}: {value}')
    return sorted(set(entries))


def manifest(text):
    sections, current = {}, None
    for line in text.splitlines():
        item = line.strip()
        if not item or item.startswith('#'):
            continue
        if line == item and re.fullmatch(r'[a-z]+:', item):
            current = item[:-1]
            if current not in TYPES | {'universal'} or current in sections:
                raise Invalid(f'Unknown/duplicate manifest section: {current}')
            sections[current] = []
        elif line.startswith('  - ') and current:
            name = item[2:].split(' #', 1)[0].strip()
            destination(name)
            sections[current].append(name)
        else:
            raise Invalid(f'Unsupported manifest line: {line!r}')
    if 'universal' not in sections:
        raise Invalid('Manifest has no universal section')
    return sections


def loader(args):
    if args.source_base:
        base = args.source_base.rstrip('/')
        if not re.fullmatch(r'https://raw\.githubusercontent\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[0-9a-fA-F]{40}', base):
            raise Invalid('source-base must be HTTPS GitHub raw URL pinned to a 40-character commit SHA')

        def get(rel):
            safe_relative(rel)
            url = base + '/' + rel
            with urllib.request.urlopen(url, timeout=30) as response:
                if response.url != url:
                    raise Invalid('Source redirect is not the pinned URL')
                data = response.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise Invalid(f'Source too large: {rel}')
            return data

        return get, {'kind': 'https', 'base': base, 'commit_sha': base.rsplit('/', 1)[1]}
    if args.bundle:
        bundle = Path(args.bundle).resolve()
        if not bundle.is_dir():
            raise Invalid('Bundle directory does not exist')

        def get(rel):
            data = read_file(bundle, rel)
            if len(data) > MAX_FILE:
                raise Invalid(f'Source too large: {rel}')
            return data

        # Local machine path must not leak into generated public context.
        return get, {'kind': 'bundle'}
    raise Invalid('Choose --source-base or an explicit --bundle')


def validate_paths(paths):
    seen = {}
    components = {}
    for path in paths:
        safe_relative(path)
        key = path_key(path)
        if key in seen and seen[key] != path:
            raise Invalid(f'Case/Unicode path collision: {seen[key]}, {path}')
        seen[key] = path
        for component in [PurePosixPath(path), *PurePosixPath(path).parents]:
            if str(component) == '.':
                continue
            normalized = path_key(str(component))
            if normalized in components and components[normalized] != str(component):
                raise Invalid(f'Case/Unicode directory collision: {components[normalized]}, {component}')
            components[normalized] = str(component)
    for key, path in seen.items():
        for parent in PurePosixPath(key).parents:
            if str(parent) in seen:
                raise Invalid(f'File/parent collision: {path}')


def context(config, sources, start, policy):
    mandatory = policy.get('mandatory')
    limit = policy.get('max_bytes')
    if (policy.get('schema_version', 1) != 1 or
            not isinstance(mandatory, list) or not mandatory or
            any(not isinstance(v, str) for v in mandatory) or
            len(mandatory) != len(set(mandatory)) or
            type(limit) is not int or not 1 <= limit <= MAX_FILE):
        raise Invalid('Invalid context policy')
    text = ('<!-- generated by ai-bootstrap; edit .AI sources, then build -->\n'
            '# AI-LAYOUT-1\n\n'
            'Корень проекта - клиентский зонтик. Перед работой прочитай .AI/START.md, '
            '.AI/project.md, .AI/memory/MEMORY.md и действующую Markdown-задачу. '
            'История другой сессии не требуется. Полные обязательные нормы ниже.\n\n')
    for canon_path in mandatory:
        if not canon_path.startswith('rules/') or canon_path not in sources:
            raise Invalid(f'Missing mandatory rule: {canon_path}')
        text += f'## Источник: {destination(canon_path)}\n\n' + sources[canon_path].decode('utf-8') + '\n\n'
    data = text.encode()
    if len(data) > limit:
        raise Invalid(f'Context exceeds budget: {len(data)} > {limit} bytes')
    outputs = {}
    if set(config['adapters']) & {'codex', 'kimi'}:
        outputs['AGENTS.md'] = data
    if 'claude' in config['adapters']:
        outputs['CLAUDE.md'] = data
    fingerprint = sha(encoded({'config': config, 'policy': policy, 'start': sha(start),
                               'sources': {p: sha(v) for p, v in sources.items()}}))
    return outputs, {'inputs_sha256': fingerprint,
                     'outputs': {p: sha(v) for p, v in outputs.items()}}


def file_action(rel, data, before=None, mode=0o644):
    return {'path': rel, 'before': before,
            'after': {'kind': 'file', 'sha256': sha(data), 'mode': mode},
            'data': base64.b64encode(data).decode()}


def private_tracked(root):
    if not root.exists():
        return
    try:
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--',
                                 '.AI/memory', '.AI/project.md', '.AI/canon', TXN,
                                 '.claude/settings.local.json'],
                                capture_output=True, timeout=15)
    except FileNotFoundError:
        return
    if result.returncode == 0 and result.stdout:
        raise Invalid('Private bootstrap paths are already tracked by Git; reconcile before bootstrap')
    if result.returncode not in (0, 128):
        raise Invalid('Cannot inspect Git tracking')


def private_ignored(root):
    """Verify effective Git rules; without Git, require the bootstrap guard lines."""
    probes = ('.AI/memory/MEMORY.md', '.AI/project.md', STATE,
              TXN + '/journal.json', '.claude/settings.local.json')
    try:
        git_root = subprocess.run(['git', '-C', str(root), 'rev-parse', '--is-inside-work-tree'],
                                  capture_output=True, timeout=15)
        if git_root.returncode == 0 and git_root.stdout.strip() == b'true':
            for path in probes:
                result = subprocess.run(['git', '-C', str(root), 'check-ignore', '--no-index', '-q', '--', path],
                                        capture_output=True, timeout=15)
                if result.returncode != 0:
                    raise Invalid(f'Private path is not ignored: {path}')
            return
    except FileNotFoundError:
        pass
    text = read_file(root, '.gitignore').decode()
    if any(line not in text.splitlines() for line in IGNORE.splitlines() if line.startswith('/')):
        raise Invalid('Private ignore protection is missing')


def package(args, root):
    try:
        project_id = str(uuid.UUID(args.project_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise Invalid('project-id must be a UUID') from error
    types = selection(args.types, TYPES, 'project type')
    adapters = selection(args.adapters, ADAPTERS, 'adapter')
    if not adapters:
        raise Invalid('At least one adapter is required')
    get, origin = loader(args)
    raw_manifest = get('manifest.yaml')
    sections = manifest(raw_manifest.decode('utf-8'))
    if any(t not in sections for t in types):
        raise Invalid('Selected type is absent from manifest')
    selected = sorted({p for t in ['universal'] + types for p in sections[t]})
    validate_paths(selected)
    validate_paths([destination(p) for p in selected])
    sources = {p: get(p) for p in selected}
    templates = {name: get('templates/ai/' + name) for name in TEMPLATES}
    if sum(map(len, sources.values())) + sum(map(len, templates.values())) > MAX_PACKAGE:
        raise Invalid('Source package too large')
    config = {'schema_version': VERSION, 'layout_version': LAYOUT, 'project_id': project_id,
              'project_type': types, 'adapters': adapters}
    catalogue = '\n'.join(f'- [{destination(p)}]({destination(p)[4:]})' for p in selected if not p.startswith('scripts/'))
    start = templates['START.md'].decode().replace('{{CATALOG}}', catalogue).encode()
    policy = json.loads(templates['context-policy.json'])
    if not isinstance(policy, dict):
        raise Invalid('Context policy must be an object')
    outputs, ctx = context(config, sources, start, policy)
    canonical = {p: {'path': destination(p), 'sha256': sha(v), 'blob_sha': blob(v),
                     'mode': '100755' if p.startswith('scripts/') else '100644'}
                 for p, v in sources.items()}
    origin['manifest_sha256'] = sha(raw_manifest)
    origin['bundle_sha256'] = sha(encoded({'sources': canonical,
                                          'templates': {k: sha(v) for k, v in templates.items()}}))
    state = {'schema_version': VERSION, 'layout_version': LAYOUT, 'project_id': project_id,
             'source': origin, 'source_files': canonical, 'context': ctx}
    intent = {**config, 'local_only': [], 'skip_sync': [], 'overrides': []}
    files = {destination(p): data for p, data in sources.items()}
    files.update({PROJECT: encoded(config), '.AI/project.md': templates['project.md'],
                  '.AI/START.md': start, '.AI/memory/MEMORY.md': templates['MEMORY.md'],
                  POLICY: encoded(policy), INTENT: encoded(intent), **outputs})
    state['bootstrap_files'] = {p: sha(v) for p, v in files.items()}
    files[STATE] = encoded(state)
    private_tracked(root)

    if (root / JOURNAL).exists():
        raise Invalid('Unfinished transaction: run recover')
    if (root / '.AI').exists() or (root / '.AI').is_symlink():
        current = load_state(root)
        if (json_file(root, PROJECT) != config or current['source'] != origin or
                current['source_files'] != canonical or current.get('bootstrap_files') != state['bootstrap_files']):
            raise Invalid('Existing .AI is not the same bootstrap; migration/sync required')
        check(root)
        # Never reclassify local source edits as a new upstream base.
        if any(sha(read_file(root, destination(p))) != v['sha256'] for p, v in canonical.items()):
            raise Invalid('Local source changed; refusing bootstrap over it')
        return [], state
    for rel in ('.claude', '.agents', 'AGENTS.md', 'CLAUDE.md', TXN):
        if descriptor(root, rel) is not None:
            raise Invalid(f'Existing agent path requires migration: {rel}')
    for rel in files:
        if descriptor(root, rel) is not None:
            raise Invalid(f'Would overwrite existing file: {rel}')
    ignore_before = descriptor(root, '.gitignore')
    if ignore_before and ignore_before['kind'] != 'file':
        raise Invalid('Existing .gitignore must be a regular file')
    ignore = read_file(root, '.gitignore') if ignore_before else b''
    # Place rules last, so existing negations do not override them.
    files['.gitignore'] = ignore + IGNORE.encode()
    dirs = {'.AI/' + n for n in ('rules', 'skills', 'roles', 'commands', 'memory')}
    dirs.update(('docs/backlog', 'docs/done'))
    for rel in list(files) + list(LINKS) + list(dirs):
        dirs.update(str(p) for p in PurePosixPath(rel).parents if str(p) != '.')
    actions = []
    for rel in sorted(dirs, key=lambda p: (p.count('/'), p)):
        old = descriptor(root, rel)
        if old is None:
            actions.append({'path': rel, 'before': None, 'after': {'kind': 'dir'}})
        elif old != {'kind': 'dir'}:
            raise Invalid(f'Would replace directory: {rel}')
    # Ignore private paths before installing their contents. Parents are harmless.
    actions.append(file_action('.gitignore', files.pop('.gitignore'), ignore_before))
    for rel, data in sorted(files.items()):
        if rel != STATE:
            actions.append(file_action(rel, data, mode=0o755 if rel.startswith('scripts/') else 0o644))
    for rel, target in LINKS.items():
        actions.append({'path': rel, 'before': None, 'after': {'kind': 'link', 'target': target}})
    actions.append(file_action(STATE, files[STATE]))
    return actions, state


def validate_state(state, config=None):
    if not isinstance(state, dict):
        raise Invalid('Invalid state object')
    if state.get('schema_version') != VERSION or state.get('layout_version') != LAYOUT:
        raise Invalid('Unknown state/layout version')
    files = state.get('source_files')
    if not isinstance(files, dict) or not files:
        raise Invalid('Invalid source_files')
    validate_paths(files)
    for path, data in files.items():
        if (not isinstance(data, dict) or data.get('path') != destination(path) or
                not re.fullmatch(r'[0-9a-f]{64}', str(data.get('sha256', ''))) or
                not re.fullmatch(r'[0-9a-f]{40}', str(data.get('blob_sha', ''))) or
                data.get('mode') not in ('100644', '100755')):
            raise Invalid(f'Invalid source state: {path}')
    if config is None:
        return state
    if (not isinstance(config, dict) or config.get('schema_version') != VERSION or config.get('layout_version') != LAYOUT or
            not isinstance(config.get('adapters'), list) or not config['adapters'] or
            not all(a in ADAPTERS for a in config['adapters'])):
        raise Invalid('Invalid project schema/adapters')
    try:
        if str(uuid.UUID(config['project_id'])) != state.get('project_id'):
            raise Invalid('Project identity differs from state')
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise Invalid('Invalid project identity') from error
    local_files = state.get('local_files', [])
    if not isinstance(local_files, list) or any(not isinstance(p, str) for p in local_files) or set(local_files) & set(files):
        raise Invalid('Invalid local-only files')
    validate_paths(list(files) + local_files)
    for path in local_files:
        destination(path)
    ctx = state.get('context')
    expected = ({'AGENTS.md'} if set(config['adapters']) & {'codex', 'kimi'} else set())
    if 'claude' in config['adapters']:
        expected.add('CLAUDE.md')
    if (not isinstance(ctx, dict) or not isinstance(ctx.get('outputs'), dict) or
            set(ctx['outputs']) != expected or
            not re.fullmatch(r'[0-9a-f]{64}', str(ctx.get('inputs_sha256', ''))) or
            any(not re.fullmatch(r'[0-9a-f]{64}', str(v)) for v in ctx['outputs'].values())):
        raise Invalid('Invalid context state')
    return state


def load_state(root):
    return validate_state(json_file(root, STATE), json_file(root, PROJECT))


def current_context(root, state):
    sources = {p: read_file(root, data['path']) for p, data in state['source_files'].items()}
    sources.update({p: read_file(root, destination(p)) for p in state.get('local_files', [])})
    return context(json_file(root, PROJECT), sources, read_file(root, '.AI/START.md'), json_file(root, POLICY))


def assert_outputs(root, state):
    for rel, expected in state['context']['outputs'].items():
        if sha(read_file(root, rel)) != expected:
            raise Invalid(f'Manually edited output: {rel}; reconcile with .AI sources')


def assert_links(root):
    for rel, target in LINKS.items():
        if descriptor(root, rel) != {'kind': 'link', 'target': target}:
            raise Invalid(f'Compatibility link changed: {rel}')


def assert_layout(root):
    for rel in ('.AI/project.md', '.AI/memory/MEMORY.md', INTENT):
        read_file(root, rel)
    intent = json_file(root, INTENT)
    if intent.get('schema_version') != VERSION or intent.get('layout_version') != LAYOUT:
        raise Invalid('Unknown intent/layout version')
    for rel in ('.AI/rules', '.AI/skills', '.AI/roles', '.AI/commands', '.AI/memory',
                'docs/backlog', 'docs/done'):
        if descriptor(root, rel) != {'kind': 'dir'}:
            raise Invalid(f'Missing layout directory: {rel}')


def refuse_migration(root):
    if descriptor(root, MIGRATION_JOURNAL) is not None:
        raise Invalid('Unfinished migration: use ai-migrate.py recover')


def check(root):
    refuse_migration(root)
    if (root / JOURNAL).exists() or (root / JOURNAL).is_symlink():
        raise Invalid('Unfinished transaction: run recover')
    state = load_state(root)
    assert_layout(root)
    assert_links(root)
    assert_outputs(root, state)
    _, ctx = current_context(root, state)
    if ctx != state['context']:
        raise Invalid('Context is stale; run build')
    private_tracked(root)
    private_ignored(root)
    return state


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data, mode=0o600):
    temp = path.parent / ('.' + path.name + '.ai-' + uuid.uuid4().hex)
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


@contextlib.contextmanager
def writer(root):
    if os.name != 'posix':
        raise Invalid('This bootstrap writer requires POSIX; this platform adapter is not validated yet')
    import fcntl
    if not root.is_dir():
        raise Invalid('Project root must already exist')
    folder = checked(root, TXN)
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise Invalid('Unsafe transaction directory')
    folder.mkdir(mode=0o700, exist_ok=True)
    lock = folder / 'lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Invalid('Unsafe lock file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Invalid('Another bootstrap/context writer is active') from error
        yield
    finally:
        os.close(fd)


def allowed_action(rel, desc):
    safe_relative(rel)
    if rel in ('.AI', '.claude', '.agents', 'scripts', 'docs', 'docs/backlog', 'docs/done'):
        return desc['kind'] == 'dir'
    if rel in LINKS:
        return desc == {'kind': 'link', 'target': LINKS[rel]}
    if rel in ('AGENTS.md', 'CLAUDE.md', '.gitignore'):
        return desc['kind'] == 'file'
    return rel.startswith(('.AI/', 'scripts/')) and desc['kind'] in ('file', 'dir')


def validate_journal(journal):
    if journal.get('schema_version') != VERSION or journal.get('layout_version') != LAYOUT:
        raise Invalid('Unknown journal version')
    actions = journal.get('actions')
    if not isinstance(actions, list) or not actions:
        raise Invalid('Invalid journal actions')
    seen = set()
    for action in actions:
        if not isinstance(action, dict):
            raise Invalid('Invalid action')
        rel, after = action.get('path'), action.get('after')
        if not isinstance(after, dict) or after.get('kind') not in ('file', 'dir', 'link'):
            raise Invalid('Invalid target descriptor')
        if not allowed_action(rel, after) or path_key(rel) in seen:
            raise Invalid(f'Unsafe/duplicate journal destination: {rel}')
        seen.add(path_key(rel))
        before = action.get('before')
        if before is not None and (not isinstance(before, dict) or before.get('kind') != 'file' or
                                   rel not in ('AGENTS.md', 'CLAUDE.md', '.gitignore', STATE)):
            raise Invalid('Unexpected journal overwrite')
        if after['kind'] == 'file':
            try:
                data = base64.b64decode(action['data'], validate=True)
            except (ValueError, KeyError, TypeError) as error:
                raise Invalid('Invalid journal payload') from error
            if sha(data) != after.get('sha256') or after.get('mode') not in (0o644, 0o755):
                raise Invalid('Journal payload differs from descriptor')
    if actions[-1]['path'] != STATE:
        raise Invalid('State must be committed last')
    # Validate the future state before *any* mutation, not only after applying it.
    try:
        state = json.loads(base64.b64decode(actions[-1]['data']))
        validate_state(state)
    except (ValueError, KeyError, TypeError) as error:
        raise Invalid('Invalid journal future state') from error


def validate_future(root, journal):
    """Validate effective metadata and outputs without applying the journal."""
    validate_journal(journal)
    pending = {a['path']: a for a in journal['actions']}

    def future_bytes(rel):
        action = pending.get(rel)
        if action is None:
            return read_file(root, rel)
        if action['after']['kind'] != 'file':
            raise Invalid(f'Future metadata is not a file: {rel}')
        return base64.b64decode(action['data'])

    state = json.loads(future_bytes(STATE))
    validate_state(state, json.loads(future_bytes(PROJECT)))
    policy = json.loads(future_bytes(POLICY))
    if not isinstance(policy, dict) or policy.get('schema_version', 1) != 1:
        raise Invalid('Unknown future context policy version')
    for rel, expected in state['context']['outputs'].items():
        if sha(future_bytes(rel)) != expected:
            raise Invalid(f'Future output differs from receipt: {rel}')


def finish(root, journal):
    validate_future(root, journal)
    # Preflight all changes, so one conflict cannot cause additional writes.
    for action in journal['actions']:
        cur = descriptor(root, action['path'])
        if cur != action['before'] and cur != action['after']:
            raise Invalid(f'Transaction conflict: {action["path"]}; preserved, journal retained')
    for action in journal['actions']:
        rel = action['path']
        after = action['after']
        cur = descriptor(root, rel)
        if cur == after:
            continue
        if cur != action['before']:
            raise Invalid(f'Concurrent edit: {rel}; journal retained')
        path = checked(root, rel)
        if after['kind'] == 'dir':
            path.mkdir()
            sync_dir(path.parent)
        elif after['kind'] == 'link':
            path.symlink_to(after['target'], target_is_directory=True)
            sync_dir(path.parent)
        else:
            atomic_bytes(path, base64.b64decode(action['data']), after['mode'])
    # Validate the receipt before declaring the generation committed.
    state = load_state(root)
    assert_outputs(root, state)
    assert_links(root)
    (root / JOURNAL).unlink()
    sync_dir(root / TXN)


def transact(root, actions):
    if not actions:
        return
    journal = {'schema_version': VERSION, 'layout_version': LAYOUT, 'actions': actions}
    validate_journal(journal)
    if descriptor(root, JOURNAL) is not None:
        raise Invalid('Unfinished transaction: run recover')
    atomic_bytes(root / JOURNAL, encoded(journal))
    finish(root, journal)


def build(root):
    refuse_migration(root)
    # Preflight before acquiring a writer, to preserve invalid trees unchanged.
    if descriptor(root, JOURNAL) is not None:
        raise Invalid('Unfinished transaction: run recover')
    state = load_state(root)
    assert_links(root)
    assert_outputs(root, state)
    assert_layout(root)
    private_tracked(root)
    private_ignored(root)
    outputs, ctx = current_context(root, state)
    if ctx == state['context']:
        return False
    with writer(root):
        # Re-read after lock acquisition; never apply a pre-lock plan blindly.
        if descriptor(root, JOURNAL) is not None:
            raise Invalid('Unfinished transaction: run recover')
        state = load_state(root)
        assert_outputs(root, state)
        outputs, ctx = current_context(root, state)
        actions = [file_action(p, data, descriptor(root, p)) for p, data in sorted(outputs.items())]
        state['context'] = ctx
        actions.append(file_action(STATE, encoded(state), descriptor(root, STATE)))
        transact(root, actions)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'apply', 'build', 'check', 'recover'))
    parser.add_argument('--root', required=True, type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--bundle', type=Path)
    source.add_argument('--source-base')
    parser.add_argument('--project-id')
    parser.add_argument('--types', default='')
    parser.add_argument('--adapters', default='claude,codex,kimi')
    args = parser.parse_args(argv)
    if args.root.is_symlink():
        parser.error('Use the actual client root, not a symlink')
    root = args.root.absolute()
    try:
        refuse_migration(root)
        if args.command in ('plan', 'apply'):
            actions, state = package(args, root)
            if args.command == 'plan':
                print(json.dumps({'status': 'planned' if actions else 'up-to-date',
                                  'project_id': state['project_id'],
                                  'changes': [{'path': a['path'], 'kind': a['after']['kind']} for a in actions]},
                                 ensure_ascii=False))
                return 0
            if not root.is_dir():
                raise Invalid('Project root must already exist')
            if actions:
                with writer(root):
                    if descriptor(root, JOURNAL) is not None:
                        raise Invalid('Unfinished transaction: run recover')
                    # Compare every planned before-version after acquiring the lock.
                    for a in actions:
                        if descriptor(root, a['path']) != a['before']:
                            raise Invalid(f'Project changed since plan: {a["path"]}')
                    transact(root, actions)
            print(json.dumps({'status': 'applied' if actions else 'up-to-date'}))
        elif args.command == 'check':
            # Schema incompatibility is not merely stale context.
            if descriptor(root, JOURNAL) is not None:
                validate_journal(json_file(root, JOURNAL))
                print('Unfinished transaction: run recover', file=sys.stderr)
                return 1
            load_state(root)
            try:
                check(root)
            except Invalid as error:
                print(str(error), file=sys.stderr)
                return 1
            print(json.dumps({'status': 'ok'}))
        elif args.command == 'build':
            changed = build(root)
            print(json.dumps({'status': 'built' if changed else 'up-to-date'}))
        else:
            if descriptor(root, JOURNAL) is None:
                print(json.dumps({'status': 'no-transaction'}))
                return 0
            journal = json_file(root, JOURNAL)
            validate_future(root, journal)
            with writer(root):
                finish(root, json_file(root, JOURNAL))
            print(json.dumps({'status': 'recovered'}))
        return 0
    except (Invalid, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f'ai-bootstrap: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
