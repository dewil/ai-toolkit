#!/usr/bin/env python3
"""Самодостаточный pre-commit для проверки staged-изменений через gitleaks.

CLI:
  python3 scripts/gitleaks-hook.py status  [--repo <путь>]...
  python3 scripts/gitleaks-hook.py install [--repo <путь>]...

Без --repo используется текущий git-корень. Каталог хуков определяет git
rev-parse --git-path hooks, включая core.hooksPath и linked worktree.
Бинарник скрипт не устанавливает. Чужой shell-хук сохраняется вне маркеров.

Коды: 0 - install выполнен / status: хук и бинарник есть во всех репозиториях;
1 - status: чего-то не хватает; 2 - ошибка CLI, git или файловой системы;
3 - install: чужой pre-commit не shell, нужна ручная интеграция.
При нескольких репозиториях ошибка 2 имеет приоритет над 3 и 1.
Хук: 0 - проверка прошла, SKIP_GITLEAKS=1 или нет бинарника;
ненулевой код сканера - коммит блокируется.
"""

import argparse
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile


START = b"# >>> canon gitleaks >>>"
END = b"# <<< canon gitleaks <<<"
LINUX_INSTALL = (
    '(set -eu; case "$(uname -m)" in x86_64) arch=x64;; '
    'aarch64|arm64) arch=arm64;; *) echo "Unsupported architecture" >&2; exit 1;; esac; '
    'tmp=$(mktemp -d); trap \'rm -rf "$tmp"\' EXIT; '
    'curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/'
    'gitleaks_8.30.1_linux_${arch}.tar.gz" -o "$tmp/gitleaks.tgz"; '
    'tar -xzf "$tmp/gitleaks.tgz" -C "$tmp" gitleaks; '
    'mkdir -p "$HOME/.local/bin"; install -m 755 "$tmp/gitleaks" "$HOME/.local/bin/gitleaks")'
)
HOOK_BODY = r'''(
    if [ "${SKIP_GITLEAKS:-}" = 1 ]; then
        printf '%s\n' 'gitleaks пропущен (SKIP_GITLEAKS=1)' >&2
        exit 0
    fi
    if ! command -v gitleaks >/dev/null 2>&1; then
        printf '%s\n' 'ВНИМАНИЕ: gitleaks не найден в PATH!' \
            'Секреты не проверены. Коммит разрешен без проверки.' \
            __INSTALL_WARNING__ >&2
        exit 0
    fi
    version=$(gitleaks version 2>/dev/null) || version=
    major= minor=
    for part in $version; do
        part=${part#v}
        case "$part" in
            [0-9]*.[0-9]*)
                major=${part%%.*}
                part=${part#*.}
                minor=${part%%.*}
                break
                ;;
        esac
    done
    case "$major:$minor" in
        *[!0-9:]*|:*|*:)
            printf '%s\n' 'gitleaks: не удалось определить версию; проверь gitleaks version' >&2
            exit 1
            ;;
    esac
    if [ "$major" -gt 8 ] || { [ "$major" -eq 8 ] && [ "$minor" -ge 19 ]; }; then
        set -- git --pre-commit --staged --redact
    else
        set -- protect --staged --redact
    fi
    if gitleaks "$@"; then
        exit 0
    else
        result=$?
        printf '%s\n' 'gitleaks: коммит заблокирован.' \
            'Ложное срабатывание: добавь запись в .gitleaksignore с комментарием почему.' \
            'Срочный обход: SKIP_GITLEAKS=1 git commit ...' >&2
        exit "$result"
    fi
) || exit $?
'''.replace("__INSTALL_WARNING__", " \\\n            ".join(shlex.quote(line) for line in (
    "Linux: " + LINUX_INSTALL,
    'Добавь ~/.local/bin в PATH: export PATH="$HOME/.local/bin:$PATH"',
    "macOS: brew install gitleaks",
    "Windows: winget install gitleaks",
    "Проверка: gitleaks version",
)))
BLOCK = START + b"\n" + HOOK_BODY.encode() + END
BLOCK_PATTERN = re.compile(
    rb"^" + re.escape(START) + rb"\r?\n.*?^" + re.escape(END) + rb"(?=\r?$)",
    re.MULTILINE | re.DOTALL,
)


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise ValueError(f"{repo}: не git-репозиторий или ошибка git")
    return os.fsdecode(result.stdout).rstrip("\n")


def hook_path(repo):
    root = Path(git(repo, "rev-parse", "--show-toplevel"))
    hooks = Path(git(root, "rev-parse", "--git-path", "hooks"))
    return (hooks if hooks.is_absolute() else root / hooks) / "pre-commit"


def is_shell(data):
    if not data.startswith(b"#!") or b"\0" in data:
        return False
    try:
        words = shlex.split(data.splitlines()[0][2:].decode("utf-8"))
    except (ValueError, UnicodeError):
        return False
    if not words:
        return False
    if Path(words[0]).name == "env":
        words = words[1:]
        if words and words[0] == "-S":
            words = words[1:]
    return bool(words) and Path(words[0]).name in {"sh", "bash"}


def hook_status(path, data):
    if data is None:
        return "нет"
    if not is_shell(data):
        return "чужой-не-shell"
    matches = list(BLOCK_PATTERN.finditer(data))
    if not matches:
        return "устарел" if START in data or END in data else "нет"
    shebang_end = data.find(b"\n") + 1
    if (len(matches) == 1 and matches[0].group() == BLOCK
            and matches[0].start() == shebang_end
            and path.stat().st_mode & stat.S_IXUSR):
        return "стоит"
    return "устарел"


def install(path, data):
    if data is not None and not is_shell(data):
        print("чужой pre-commit не shell: добавь вызов вручную", file=sys.stderr)
        print("v8.19+: gitleaks git --pre-commit --staged --redact", file=sys.stderr)
        print("v8 до 8.19: gitleaks protect --staged --redact", file=sys.stderr)
        return 3
    original = data
    if data is None:
        data = b"#!/bin/sh\n"
    matches = list(BLOCK_PATTERN.finditer(data))
    if len(matches) > 1 or data.count(START) != len(matches) or data.count(END) != len(matches):
        raise ValueError(f"{path}: повреждены или повторяются маркеры gitleaks")
    if matches:
        match = matches[0]
        shebang_end = data.find(b"\n") + 1
        if match.group() == BLOCK and match.start() != shebang_end:
            # Ранее установленный блок в конце не работает после чужого exit 0.
            without_block = data[:match.start()] + data[match.end():]
            shebang, newline, rest = without_block.partition(b"\n")
            updated = shebang + (newline or b"\n") + BLOCK + b"\n" + rest
        else:
            updated = data[:match.start()] + BLOCK + data[match.end():]
    else:
        shebang, newline, rest = data.partition(b"\n")
        updated = shebang + (newline or b"\n") + BLOCK + b"\n" + rest
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if original is not None else 0o644
    mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if updated != original:
        # Сохраняем цель симлинка и подставляем готовый файл целиком.
        target = path.resolve()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(updated)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    else:
        path.chmod(mode)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "install"))
    parser.add_argument("--repo", action="append", type=Path)
    args = parser.parse_args()
    code = 0
    if args.command == "status":
        version = "нет"
        if shutil.which("gitleaks"):
            try:
                result = subprocess.run(["gitleaks", "version"], capture_output=True, text=True)
                version = result.stdout.strip() if result.returncode == 0 else ""
                version = version or "не удалось определить версию"
                if result.returncode or not result.stdout.strip():
                    code = 1
            except OSError:
                version = "не удалось определить версию"
                code = 1
        else:
            code = 1
        print(f"gitleaks: {version}", flush=True)
    for repo in args.repo or [Path.cwd()]:
        try:
            path = hook_path(repo)
            data = path.read_bytes() if path.exists() else None
            if args.command == "install":
                result = install(path, data)
                if result and code != 2:
                    code = result
                data = path.read_bytes() if path.exists() else None
            status = hook_status(path, data)
            print(f"{repo}: хук: {status}")
            if args.command == "status" and status != "стоит" and code == 0:
                code = 1
        except (OSError, ValueError) as error:
            print(f"Ошибка: {error}", file=sys.stderr)
            code = 2
    return code


if __name__ == "__main__":
    sys.exit(main())
