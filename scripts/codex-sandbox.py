#!/usr/bin/env python3
"""Run Codex in a Linux bubblewrap filesystem sandbox (stdlib only).

CLI:
    codex-sandbox.py audit --root PROJECT [--dry-run] -- [codex exec arguments]
    codex-sandbox.py implement --worktree PATH [--dry-run] -- [codex exec arguments]

Arguments after -- are forwarded unchanged; exec -C is supplied by this script.
Stdin is inherited. Only the listed runtime and proxy variables enter the
sandbox. CODEX_HOME must name an existing directory. HOME and /tmp start empty;
networking is shared.
Exit codes: 0 for dry-run, 2 for preflight/usage errors, otherwise Codex's exit
code. There is no unsandboxed fallback. Dry-run prints one shell-quoted argument
per line and does not run bubblewrap or Codex.
Dry-run uses /dev/null as the empty git config source; a live run uses a
temporary regular file and removes it on exit.
"""

import argparse
import fnmatch
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
    "id_ed25519*", "id_ecdsa*", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv"}
ALLOWED_ENV = (
    "PATH", "LANG", "LC_ALL", "TERM", "TZ", "HTTP_PROXY", "HTTPS_PROXY",
    "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)


class PreflightError(Exception):
    """An actionable failure before starting Codex."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PreflightError(message)


def git_common_dir(root, required):
    """Resolve linked-worktree metadata without reading its config ourselves."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        if required:
            raise PreflightError("git is required to validate the worktree") from None
        return None
    if result.returncode == 0 and result.stdout.strip():
        common = (root / result.stdout.strip()).resolve()
        if common.is_dir() and common != root and root not in common.parents:
            metadata = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel", "--absolute-git-dir"],
                capture_output=True, text=True, check=False,
            )
            paths = metadata.stdout.splitlines()
            if metadata.returncode == 0 and len(paths) == 2:
                top, git_dir = (Path(path).resolve() for path in paths)
                if git_dir != common and (not required or top == root):
                    return common
    if required:
        raise PreflightError("implement requires a linked worktree, not the main checkout")
    return None


def secret_paths(root):
    """Inspect names only; never traverse excluded directories or symlinks."""
    def fail(error):
        raise error

    def secret_name(name):
        return name != ".env.example" and any(
            fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_PATTERNS
        )

    def reject_link(path):
        if path.is_symlink():
            raise PreflightError(f"секретное имя - симлинк: {path}; удали или замени файлом")

    for base, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        dirs.sort()
        # Вложенный git-репозиторий значит, что объект - общий каталог (vault, /data/sync), а не проект.
        if Path(base) != Path(root) and (
                os.path.isdir(os.path.join(base, ".git"))):
            raise PreflightError(
                f"объект содержит другой проект ({base}); "
                "укажи корень одного проекта, а не общий каталог")
        for name in dirs[:]:
            if name in SKIP_DIRS:
                dirs.remove(name)
            elif name == "secrets":
                dirs.remove(name)
                path = Path(base) / name
                reject_link(path)
                yield path, True
            elif secret_name(name):
                reject_link(Path(base) / name)
        for name in sorted(files):
            if secret_name(name) or name == "secrets":
                path = Path(base) / name
                reject_link(path)
                if secret_name(name):
                    yield path, False


def command(args, forwarded, empty_file):
    if sys.platform != "linux":
        raise PreflightError("Linux is required for bubblewrap")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise PreflightError("bwrap not found in PATH; install bubblewrap")
    root = Path(args.root if args.mode == "audit" else args.worktree).resolve()
    if not root.is_dir():
        raise PreflightError(f"object directory does not exist: {root}")
    codex = shutil.which("codex")
    if not codex:
        raise PreflightError("codex not found in PATH")
    codex = Path(os.path.abspath(codex))
    home_value = os.environ.get("CODEX_HOME")
    if not home_value or not Path(home_value).is_dir():
        raise PreflightError("CODEX_HOME must name an existing directory")
    codex_home = Path(home_value).resolve()
    home = Path(os.path.expanduser("~")).resolve()
    if (root == Path("/") or root == home or root in home.parents
            or root == codex_home or root in codex_home.parents
            or codex_home in root.parents):
        raise PreflightError(f"object directory overlaps HOME or CODEX_HOME: {root}")
    common = git_common_dir(root, args.mode == "implement")
    masked = list(secret_paths(root))
    if not args.dry_run:
        probe = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "true"],
            stdin=subprocess.DEVNULL, capture_output=True, check=False,
        )
        if probe.returncode:
            raise PreflightError(
                "bubblewrap cannot create a namespace; check bubblewrap installation and namespace permissions"
            )

    cmd = [bwrap, "--die-with-parent", "--unshare-all", "--share-net", "--clearenv"]
    for name in ALLOWED_ENV:
        if name in os.environ:
            cmd.extend(["--setenv", name, os.environ[name]])

    def bind(source, destination=None, writable=False):
        cmd.extend(["--bind" if writable else "--ro-bind", str(source),
                    str(source if destination is None else destination)])

    bind("/usr")
    for name in ("bin", "lib", "lib64", "sbin"):
        path = Path("/") / name
        if path.is_symlink():
            cmd.extend(["--symlink", os.readlink(path), str(path)])
        elif path.exists():
            bind(path)
    for name in ("ssl", "ca-certificates", "resolv.conf", "hosts", "passwd", "localtime"):
        path = Path("/etc") / name
        if path.exists():
            bind(path)
    cmd.extend(["--tmpfs", "/tmp", "--tmpfs", str(home), "--proc", "/proc", "--dev", "/dev"])

    # npm entrypoints are symlinks into a package containing JS and native assets.
    # Mount only that package, never the surrounding home or node_modules tree.
    executable = codex.resolve(strict=True)
    package = next((p for p in executable.parents if (p / "package.json").is_file()), None)
    if package is not None:
        bind(package)
        # Recent npm releases keep the native binary in an optional package.
        for native in (package / "node_modules" / "@openai").glob("codex-linux-*"):
            bind(native.resolve(), native)
        for native in package.parent.glob("codex-linux-*"):
            bind(native.resolve(), native)
    else:
        bind(executable)
    if codex != executable:
        if not str(codex).startswith("/usr/"):
            cmd.extend(["--symlink", str(executable), str(codex)])
    node = shutil.which("node")
    if package is not None and node:
        node_path = Path(os.path.abspath(node))
        if not str(node_path).startswith("/usr/"):
            bind(node_path.resolve(), node_path)

    bind(root, writable=args.mode == "implement")
    if common is not None:
        bind(common)
        bind(empty_file, common / "config")
    bind(codex_home, writable=True)
    cmd.extend(["--setenv", "CODEX_HOME", str(codex_home), "--setenv", "HOME", str(home)])
    for path, directory in masked:
        if directory:
            cmd.extend(["--tmpfs", str(path)])
        else:
            bind("/dev/null", path)
    if masked:
        print(f"masked {len(masked)} secret paths: " + ", ".join(
            repr(str(path)) for path, _ in masked
        ), file=sys.stderr)
    cmd.extend(["--chdir", str(root), "--", str(codex), "exec", "-C", str(root), *forwarded])
    return cmd


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        separator = argv.index("--") if "--" in argv else len(argv)
        parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        modes = parser.add_subparsers(dest="mode", required=True, parser_class=Parser)
        for mode, flag in (("audit", "--root"), ("implement", "--worktree")):
            sub = modes.add_parser(mode)
            sub.add_argument(flag, required=True)
            sub.add_argument("--dry-run", action="store_true")
        args = parser.parse_args(argv[:separator])
        if args.dry_run:
            cmd = command(args, argv[separator + 1:], "/dev/null")
            print("\n".join(shlex.quote(arg) for arg in cmd))
            return 0
        with tempfile.NamedTemporaryFile(prefix="codex-sandbox-empty-") as empty:
            cmd = command(args, argv[separator + 1:], empty.name)
            result = subprocess.run(cmd, check=False)
            return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except (PreflightError, OSError, RuntimeError) as error:
        print("codex-sandbox: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
