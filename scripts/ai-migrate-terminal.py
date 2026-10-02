#!/usr/bin/env python3
"""Prepare an external terminal handoff for the pinned sibling migration engine."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

TOOLS = ('ai-bootstrap.py', 'ai-migrate.py', 'ai-migrate-terminal.py')
OPERATIONS = ('plan', 'apply', 'check', 'recover')
FIELDS = {'schema_version', 'root', 'project_id', 'operation', 'project_type',
          'adapters', 'source', 'tools_sha256'}


class Invalid(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def absolute_path(value):
    if not isinstance(value, str) or not value or '\0' in value or not Path(value).is_absolute():
        raise Invalid('Expected an absolute path')
    path = Path(value)
    if str(path) != value or '..' in path.parts or '.' in path.parts:
        raise Invalid('Expected a normalized absolute path')
    return path


def validate_identity(data, operation):
    if not isinstance(data, dict) or set(data) != FIELDS or type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise Invalid('Unsupported request schema or fields')
    root = absolute_path(data['root'])
    if root.resolve() != root or not root.is_dir():
        raise Invalid('Use the actual existing client root')
    project_id = data['project_id']
    if not isinstance(project_id, str) or str(uuid.UUID(project_id)) != project_id:
        raise Invalid('project-id must be a canonical UUID')
    if data['operation'] not in OPERATIONS or data['operation'] != operation:
        raise Invalid('CLI operation must match the request')
    return root


def bootstrap(tools):
    spec = importlib.util.spec_from_file_location('terminal_bootstrap', tools / 'ai-bootstrap.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_config(data, ab):
    for field, allowed in (('project_type', ab.TYPES), ('adapters', ab.ADAPTERS)):
        values = data[field]
        if (not isinstance(values, list) or not values or
                any(not isinstance(v, str) or v not in allowed for v in values) or
                len(values) != len(set(values))):
            raise Invalid('Invalid request ' + field)
    source = data['source']
    if not isinstance(source, dict):
        raise Invalid('Invalid request source')
    if source.get('kind') == 'http' and set(source) == {'kind', 'base'}:
        if not isinstance(source['base'], str):
            raise Invalid('Invalid pinned source')
        # The shared loader validates a pinned URL without fetching any content.
        ab.loader(argparse.Namespace(source_base=source['base'], bundle=None))
    elif source.get('kind') == 'bundle' and set(source) == {'kind', 'path'}:
        bundle = absolute_path(source['path'])
        if data['operation'] in ('plan', 'apply') and not bundle.is_dir():
            raise Invalid('Bundle directory does not exist')
        # An offline check/recover does not need the source directory to survive.
    else:
        raise Invalid('Invalid request source fields')


def receipts(tools):
    result = {}
    if tools.is_symlink() or not tools.is_dir():
        raise Invalid('Expected retained sibling tools directory')
    for name in TOOLS:
        path = tools / name
        if path.is_symlink() or not path.is_file():
            raise Invalid('Missing retained sibling tool: ' + name)
        result[name] = digest(path.read_bytes())
    return result


def verify_tools(data, tools):
    hashes = data['tools_sha256']
    if (not isinstance(hashes, dict) or set(hashes) != set(TOOLS) or
            any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v)
                for v in hashes.values())):
        raise Invalid('Invalid tool receipts')
    if receipts(tools) != hashes:
        raise Invalid('Retained tool receipt mismatch')
    if digest(Path(__file__).read_bytes()) != hashes['ai-migrate-terminal.py']:
        raise Invalid('Running terminal tool differs from the retained generation')


def prepare(args):
    root = Path(os.path.abspath(args.root))
    handoff = Path(args.handoff_dir).resolve()
    if handoff == root or root in handoff.parents or handoff in root.parents:
        raise Invalid('handoff-dir must be outside the client')
    if Path(args.handoff_dir).is_symlink() or (handoff.exists() and (not handoff.is_dir() or any(handoff.iterdir()))):
        raise Invalid('Refusing to overwrite handoff artifacts')
    own_tools = Path(__file__).resolve().parent
    hashes = receipts(own_tools)
    ab = bootstrap(own_tools)
    data = {'schema_version': 1, 'root': str(root), 'project_id': args.project_id,
            'operation': args.operation, 'project_type': args.types.split(','),
            'adapters': args.adapters.split(','), 'tools_sha256': hashes,
            'source': ({'kind': 'http', 'base': args.source_base} if args.source_base is not None
                       else {'kind': 'bundle', 'path': str(args.bundle.resolve())})}
    validate_identity(data, args.operation)
    validate_config(data, ab)
    # Read the fixed trio before creating anything, then retain exact bytes.
    payloads = {name: (own_tools / name).read_bytes() for name in TOOLS}
    if {name: digest(content) for name, content in payloads.items()} != hashes:
        raise Invalid('Sibling tools changed during prepare')
    handoff.mkdir(parents=True, exist_ok=True)
    tools = handoff / 'tools'
    tools.mkdir()
    for name, content in payloads.items():
        with (tools / name).open('xb') as output:
            output.write(content)
    request = handoff / 'request.json'
    with request.open('xb') as output:
        output.write(encoded(data))
    command = ['python3', str(tools / 'ai-migrate-terminal.py'), args.operation,
               '--request', str(request)]
    with (handoff / 'run.sh').open('x') as output:
        output.write('#!/usr/bin/env bash\nset -eu\nexec ' + shlex.join(command) + '\n')
    print('Run in an ordinary terminal:')
    print('bash ' + shlex.quote(str(handoff / 'run.sh')))


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Invalid('Duplicate JSON field')
        result[key] = value
    return result


def write_result(path, data):
    # Replace atomically so an interrupted invocation cannot leave a partial receipt.
    fd, temporary = tempfile.mkstemp(prefix='.result-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(encoded(data))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute(args):
    request = args.request.resolve()
    raw = request.read_bytes()
    data = json.loads(raw, object_pairs_hook=unique_fields)
    root = validate_identity(data, args.command)
    handoff = request.parent
    if handoff == root or root in handoff.parents or handoff in root.parents:
        raise Invalid('Request handoff must be outside the client')
    tools = handoff / 'tools'
    result = {'schema_version': 1, 'root': data['root'], 'project_id': data['project_id'],
              'operation': args.command, 'request_sha256': digest(raw),
              'status': 'running', 'check': None, 'exit_code': None, 'stage': 'validate'}
    result_path = handoff / 'result.json'
    try:
        # Receipts are verified before importing any retained engine code.
        verify_tools(data, tools)
        ab = bootstrap(tools)
        validate_config(data, ab)
        write_result(result_path, result)

        def step(operation):
            result['stage'] = operation
            write_result(result_path, result)
            command = [sys.executable, str(tools / 'ai-migrate.py'), operation,
                       '--root', data['root']]
            if operation in ('plan', 'apply'):
                source = data['source']
                command += (['--source-base', source['base']] if source['kind'] == 'http'
                            else ['--bundle', source['path']])
                command += ['--project-id', data['project_id'], '--types', ','.join(data['project_type']),
                            '--adapters', ','.join(data['adapters'])]
            completed = subprocess.run(command, capture_output=True, text=True)
            if completed.returncode:
                raise Invalid('Migration engine failed at ' + operation + '; artifacts retained')
            try:
                response = json.loads(completed.stdout, object_pairs_hook=unique_fields)
            except ValueError as error:
                raise Invalid('Invalid migration engine response at ' + operation) from error
            expected = {'plan': ('planned', 'up-to-date'), 'apply': ('migrated', 'up-to-date'),
                        'check': ('ok',), 'recover': ('recovered', 'no-transaction')}
            if not isinstance(response, dict) or response.get('status') not in expected[operation]:
                raise Invalid('Unexpected migration engine status at ' + operation)
            if operation == 'check':
                config = ab.json_file(root, ab.PROJECT)
                if (config.get('project_id') != data['project_id'] or
                        config.get('project_type') != sorted(data['project_type']) or
                        config.get('adapters') != sorted(data['adapters'])):
                    raise Invalid('Checked client identity/configuration differs from request')
            print(operation + ': ' + response['status'])
            if operation == 'plan':
                counts = {}
                for field in ('steps', 'retained', 'local_only'):
                    values = response.get(field, [])
                    if not isinstance(values, list):
                        raise Invalid('Invalid plan summary')
                    counts[field] = len(values)
                print('Client: ' + repr(data['root']) + '; project ID: ' + data['project_id'])
                source = data['source']
                print('Source: ' + source['kind'] + ' ' + repr(source.get('base', source.get('path'))))
                print('Types: ' + ','.join(data['project_type']) + '; adapters: ' + ','.join(data['adapters']))
                print('Backup: .ai-bootstrap/legacy; steps: ' + str(counts['steps']) +
                      '; retained: ' + str(counts['retained']) + '; local-only: ' + str(counts['local_only']))
            return response['status']

        if args.command in ('plan', 'apply'):
            step('plan')
        if args.command == 'plan':
            result['status'] = 'planned'
        else:
            status = step(args.command) if args.command != 'check' else None
            step('check')
            result['check'] = 'ok'
            result['status'] = status if args.command == 'apply' else ('recovered' if args.command == 'recover' else 'ok')
        result['exit_code'] = 0
        write_result(result_path, result)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        result.update(status='failed', check=None, exit_code=2)
        write_result(result_path, result)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('--root', type=Path, required=True)
    source = prepare_parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--source-base')
    source.add_argument('--bundle', type=Path)
    prepare_parser.add_argument('--project-id', required=True)
    prepare_parser.add_argument('--types', required=True)
    prepare_parser.add_argument('--adapters', required=True)
    prepare_parser.add_argument('--handoff-dir', type=Path, required=True)
    prepare_parser.add_argument('--operation', choices=OPERATIONS, required=True)
    for operation in OPERATIONS:
        commands.add_parser(operation).add_argument('--request', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            prepare(args)
        else:
            execute(args)
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        # Never forward raw engine output, request bodies or legacy contents.
        print('ai-migrate-terminal: operation failed; verify request, retained tools and engine prerequisites; artifacts retained', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
