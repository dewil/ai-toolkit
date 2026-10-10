#!/usr/bin/env python3
"""Validate YAML frontmatter in the distributable canon catalog (developer tool)."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


class FrontmatterError(ValueError):
    pass


def load_bootstrap():
    root = Path(__file__).resolve().parents[1]
    path = root / 'scripts' / 'ai-bootstrap.py'
    spec = importlib.util.spec_from_file_location('canon_ai_bootstrap', path)
    if spec is None or spec.loader is None:
        raise FrontmatterError(f'cannot load manifest reader: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def catalog(root, bootstrap):
    try:
        raw_manifest = (root / 'manifest.yaml').read_text(encoding='utf-8')
    except (OSError, UnicodeError, ValueError) as error:
        raise FrontmatterError(f'manifest.yaml: cannot read manifest ({error.__class__.__name__})') from error

    # The bootstrap manifest parser validates installation destinations too.
    # Filter unrelated registered files before asking it to parse the full union.
    filtered = []
    for line in raw_manifest.splitlines():
        if line.startswith('  - '):
            name = line.strip()[2:].split(' #', 1)[0].strip()
            try:
                bootstrap.safe_relative(name)
            except ValueError as error:
                raise FrontmatterError(f'manifest.yaml: {error}') from error
            if not (name.startswith('rules/') and name.endswith('.md')
                    or name.startswith('agents/') and name.endswith('.md')
                    or name.startswith('skills/') and name.endswith('/SKILL.md')):
                filtered.append('# ignored non-metadata entry')
                continue
        filtered.append(line)
    try:
        sections = bootstrap.manifest('\n'.join(filtered))
    except ValueError as error:
        raise FrontmatterError(f'manifest.yaml: {error}') from error

    registered = set()
    for names in sections.values():
        for name in names:
            if (name.startswith('rules/') and name.endswith('.md')
                    or name.startswith('agents/') and name.endswith('.md')
                    or name.startswith('skills/') and name.endswith('/SKILL.md')):
                registered.add(name)

    actual = set()
    for category in ('rules', 'agents'):
        directory = root / category
        if directory.is_dir() and not directory.is_symlink():
            for path in directory.iterdir():
                if path.is_file() and path.name.endswith('.md'):
                    actual.add(path.relative_to(root).as_posix())
    skills = root / 'skills'
    if skills.is_dir() and not skills.is_symlink():
        for directory in skills.iterdir():
            if directory.is_dir() and not directory.is_symlink():
                path = directory / 'SKILL.md'
                if path.is_file():
                    actual.add(path.relative_to(root).as_posix())

    selected = registered | actual
    if not selected:
        raise FrontmatterError('metadata catalog is empty')
    for name in sorted(selected):
        path = root / name
        parts = Path(name).parts
        current = root
        symlink_directory = False
        for part in parts[:-1]:
            current = current / part
            symlink_directory |= current.is_symlink()
        if symlink_directory:
            if name in registered:
                raise FrontmatterError(f'{name}: registered metadata path crosses a symlink directory')
            selected.discard(name)
            continue
        if path.exists() or path.is_symlink():
            try:
                path.resolve(strict=True).relative_to(root.resolve())
            except (OSError, ValueError) as error:
                raise FrontmatterError(f'{name}: selected file resolves outside root') from error
    return sorted(selected)


def parse_frontmatter(path, relative, yaml):
    try:
        raw = path.read_bytes()
        text = raw.decode('utf-8-sig')
    except (OSError, UnicodeError) as error:
        raise FrontmatterError(f'{relative}: cannot read UTF-8 document ({error.__class__.__name__})') from error
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise FrontmatterError(f'{relative}:1: frontmatter must start with --- on the first line')
    end = next((i for i, line in enumerate(lines[1:], 1) if line == '---'), None)
    if end is None:
        raise FrontmatterError(f'{relative}:{len(lines) or 1}: frontmatter closing --- is missing')
    source = '\n'.join(lines[1:end])

    class UniqueSafeLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            if isinstance(node, yaml.MappingNode):
                explicit = set()
                merge_key = object()
                for key_node, _ in node.value:
                    if key_node.tag == 'tag:yaml.org,2002:merge':
                        key = merge_key
                    else:
                        key = self.construct_object(key_node, deep=deep)
                    try:
                        duplicate = key in explicit
                        explicit.add(key)
                    except TypeError as error:
                        raise yaml.constructor.ConstructorError(
                            'while constructing a mapping', node.start_mark,
                            'unhashable mapping key', key_node.start_mark,
                        ) from error
                    if duplicate:
                        label = '<<' if key is merge_key else repr(key)
                        raise yaml.constructor.ConstructorError(
                            'while constructing a mapping', node.start_mark,
                            f'duplicate key {label}', key_node.start_mark,
                        )
            return super().construct_mapping(node, deep=deep)
    try:
        docs = list(yaml.load_all(source, Loader=UniqueSafeLoader))
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        line = mark.line + 2 if mark is not None else 2
        problem = getattr(error, 'problem', None) or str(error).splitlines()[0]
        raise FrontmatterError(f'{relative}:{line}: invalid YAML: {problem}') from error
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise FrontmatterError(f'{relative}:2: frontmatter must contain exactly one mapping')
    values = docs[0]
    required = ['description']
    if relative.startswith(('skills/', 'agents/')):
        required.append('name')
    for key in required:
        value = values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise FrontmatterError(f'{relative}:2: {key} must be a nonempty string')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path,
                        default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        import yaml
    except ImportError:
        print('PyYAML is required; install with: pip install -r requirements-dev.txt',
              file=sys.stderr)
        return 2
    try:
        bootstrap = load_bootstrap()
        files = catalog(root, bootstrap)
    except (FrontmatterError, OSError, UnicodeError, ValueError) as error:
        print(f'Frontmatter check failed: {error}', file=sys.stderr)
        return 2

    errors = 0
    for name in files:
        try:
            parse_frontmatter(root / name, name, yaml)
        except FrontmatterError as error:
            print(error, file=sys.stderr)
            errors += 1
    if errors:
        print(f'Checked {len(files)} files: {errors} metadata error(s)', file=sys.stderr)
        return 1
    print(f'Checked {len(files)} metadata files: all valid')
    return 0


if __name__ == '__main__':
    sys.exit(main())
