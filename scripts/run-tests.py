#!/usr/bin/env python3
"""Run every direct test_*.py in a separate Python process (stdlib only)."""
from __future__ import annotations

import argparse
from pathlib import Path
import stat
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tests-dir', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'tests')
    args = parser.parse_args(argv)
    try:
        directory = args.tests_dir.absolute()
        tests = []
        for path in sorted(directory.iterdir()):
            if not (path.name.startswith('test_') and path.name.endswith('.py')):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f'Unsupported test file: {path.name}')
            tests.append(path)
        if not tests:
            raise ValueError(f'No test_*.py files in {directory}')
    except (OSError, ValueError) as error:
        print(f'Test discovery failed: {error}', file=sys.stderr)
        return 2

    for path in tests:
        print(f'Running {path.name}', flush=True)
        try:
            result = subprocess.run([sys.executable, str(path)], cwd=directory.parent)
        except OSError as error:
            print(f'{path.name}: cannot launch test: {error}', file=sys.stderr)
            return 1
        if result.returncode:
            print(f'{path.name}: failed (exit {result.returncode})', file=sys.stderr)
            return 1
    print(f'Passed {len(tests)} test modules', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
