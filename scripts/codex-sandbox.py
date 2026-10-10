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
import hashlib
import json
import fnmatch
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import platform


SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
    "id_ed25519*", "id_ecdsa*", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv"}
ALLOWED_ENV = (
    "PATH", "LANG", "LC_ALL", "TERM", "TZ", "HTTP_PROXY", "HTTPS_PROXY",
    "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)
EVAL_VERSION = "0.162.1"
EVAL_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


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


def _safe_proxy(name, value):
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname: return False
        if parsed.username is not None or parsed.password is not None: return False
        if any(word in (parsed.path + parsed.query + parsed.fragment).lower()
               for word in ("token", "secret", "key", "bearer", "password", "passwd")): return False
        return True
    except ValueError:
        return False


def _eval_env(home, state):
    env = {}
    env["PATH"] = "/usr/bin:/bin"
    env["HOME"] = "/home/eval"
    env["CODEX_HOME"] = "/state/codex"
    env["LANG"] = os.environ.get("LANG", "C.UTF-8")
    for name in EVAL_PROXY_ENV:
        value = os.environ.get(name)
        if value:
            if not _safe_proxy(name, value):
                raise PreflightError("credential-bearing or invalid proxy rejected")
            env[name] = value
    return env


def _auth_source(args):
    if args.auth_file:
        candidate = Path(args.auth_file)
    elif os.environ.get("CODEX_HOME"):
        candidate = Path(os.environ["CODEX_HOME"]) / "auth.json"
    else:
        candidate = Path.home() / ".codex" / "auth.json"
    try:
        st = candidate.lstat()
    except OSError:
        raise PreflightError("approved Codex auth.json is unavailable") from None
    if candidate.is_symlink() or not candidate.is_file() or st.st_mode & 0o077:
        raise PreflightError("auth.json must be a private regular file (mode 600)")
    return candidate


def _codex_config(binary, *, judge=False):
    profile = "judge" if judge else "eval"
    extends = ":read-only" if judge else ":workspace"
    fields = [
        'default_permissions = "' + profile + '"', 'approval_policy = "never"',
        'cli_auth_credentials_store = "file"', 'web_search = "disabled"',
        'allow_login_shell = false', 'history.persistence = "none"', "",
        f"[permissions.{profile}]", f'extends = "{extends}"', "",
        f"[permissions.{profile}.filesystem]", '":root" = "deny"',
        '":minimal" = "read"', json.dumps(binary) + ' = "read"',
        '"/state/codex" = "deny"',
    ]
    fields += ["", f"[permissions.{profile}.network]", "enabled = false", "",
        "[shell_environment_policy]", 'inherit = "none"', "experimental_use_profile = false", "",
        "[shell_environment_policy.set]", 'PATH = "/usr/bin:/bin"',
        'HOME = "/home/eval"', 'LANG = "C.UTF-8"', "", "[tools]", "view_image = false", "",
        "[features]"]
    for name in ("apps", "hooks", "plugins", "remote_plugin", "multi_agent", "multi_agent_v2",
        "browser_use", "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
        "computer_use", "image_generation", "view_image", "code_mode_host", "shell_snapshot",
        "skill_search", "skill_mcp_dependency_install", "workspace_dependencies", "daemon_auto_start"):
        fields.append(f"{name} = false")
    if judge:
        fields += ["", "[permissions.judge.workspace_roots]", '"." = "read"',
                   "", "[tools.shell_tool]", "enabled = false", "", "[tools.unified_exec]", "enabled = false"]
    return "\n".join(fields) + "\n"


def _native_binary(codex):
    package = next((p for p in codex.resolve().parents if (p / "package.json").is_file()), None)
    if package is None: return codex.resolve(strict=True)
    machine = platform.machine().lower()
    arch = "x86_64" if machine in ("x86_64", "amd64") else "aarch64" if machine in ("aarch64", "arm64") else None
    if arch is None: raise PreflightError("unsupported Codex runtime architecture")
    candidates = list(package.glob(f"node_modules/@openai/codex-linux-*/vendor/{arch}-unknown-linux-*/bin/codex"))
    if len(candidates) != 1 or not candidates[0].is_file():
        raise PreflightError("installed native Codex runtime is unavailable or ambiguous")
    return candidates[0].resolve(strict=True)


def _check_cli(codex, env, *, judge=False):
    try:
        version = subprocess.run([str(codex), "--version"], env=env, capture_output=True,
                                 text=True, timeout=10, check=False)
        help_result = subprocess.run([str(codex), "exec", "--help"], env=env, capture_output=True,
                                     text=True, timeout=10, check=False)
        sandbox_help = subprocess.run([str(codex), "sandbox", "--help"], env=env, capture_output=True,
                                      text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PreflightError("Codex CLI capability check failed") from None
    version_text = (version.stdout + version.stderr).strip()
    if version.returncode or EVAL_VERSION not in version_text:
        raise PreflightError("unsupported Codex CLI version")
    if help_result.returncode or sandbox_help.returncode:
        raise PreflightError("Codex CLI required command is unavailable")
    help_text = help_result.stdout + help_result.stderr
    for flag in ("--no-daemon", "--ask-for-approval", "--strict-config", "--ignore-rules",
                 "--ephemeral", "--skip-git-repo-check", "--json") + (("--output-schema",) if judge else ()):
        if flag not in help_text: raise PreflightError("Codex CLI required flag is unavailable")
    sandbox_text = sandbox_help.stdout + sandbox_help.stderr
    if "--permission-profile" not in sandbox_text and "-P" not in sandbox_text:
        raise PreflightError("Codex native permission profile is unavailable")
    return version_text


def _bwrap_eval(args, codex, binary, state, home, env, *, dry=False):
    root = Path(args.root).resolve(strict=True)
    bwrap = shutil.which("bwrap")
    if not bwrap: raise PreflightError("bwrap not found; isolated eval requires bubblewrap")
    cmd = [bwrap, "--die-with-parent", "--unshare-all", "--share-net", "--clearenv"]
    for key, value in env.items(): cmd.extend(["--setenv", key, value])
    # Bind only runtime roots and runtime metadata needed for Codex/API transport.
    cmd.extend(["--ro-bind", "/usr", "/usr"])
    for name in ("bin", "lib", "lib64", "sbin"):
        p = Path("/") / name
        if p.is_symlink(): cmd.extend(["--symlink", os.readlink(p), str(p)])
        elif p.exists(): cmd.extend(["--ro-bind", str(p), str(p)])
    for name in ("ssl", "ca-certificates", "resolv.conf", "hosts", "passwd", "localtime"):
        p = Path("/etc") / name
        if p.exists(): cmd.extend(["--ro-bind", str(p), str(p)])
    cmd.extend(["--tmpfs", "/tmp", "--dir", "/home", "--tmpfs", "/home/eval",
                "--dir", "/state", "--dir", "/workspace", "--proc", "/proc", "--dev", "/dev"])
    def bind_ro(source, destination=None):
        dest = Path(destination or source)
        parents = list(reversed(dest.parents))
        for parent in parents:
            if str(parent) in ("/", "."): continue
            cmd.extend(["--dir", str(parent)])
        cmd.extend(["--ro-bind", str(source), str(dest)])
    exe = codex.resolve(strict=True)
    package = next((p for p in exe.parents if (p / "package.json").is_file()), None)
    if package:
        bind_ro(package)
        for native in (package / "node_modules" / "@openai").glob("codex-linux-*"):
            bind_ro(native.resolve(), native)
        for native in package.parent.glob("codex-linux-*"):
            bind_ro(native.resolve(), native)
    else: bind_ro(exe)
    if codex != exe and not str(codex).startswith("/usr/"):
        parent = codex.parent
        for path in reversed(parent.parents):
            if str(path) not in ("/", "."): cmd.extend(["--dir", str(path)])
        cmd.extend(["--dir", str(parent)])
        cmd.extend(["--symlink", str(exe), str(codex)])
    node = shutil.which("node")
    if package and node:
        bind_ro(Path(node).resolve(strict=True))
    cmd.extend(["--ro-bind", str(root), "/workspace"] if args.judge else ["--bind", str(root), "/workspace"])
    cmd.extend(["--bind", str(state), "/state/codex", "--chdir", "/workspace"])
    # Temporary auth/config must remain private to the trusted CLI helper.
    if args.preflight:
        profile = "judge" if args.judge else "eval"
        code = "import errno,os,pathlib,socket,sys\n"
        if args.judge:
            code += "p=pathlib.Path('/workspace/.eval-canary')\n"
            code += "try: p.write_text('no'); raise SystemExit(34)\nexcept OSError: pass\n"
        else:
            code += "p=pathlib.Path('/workspace/.eval-canary'); p.write_text('ok'); assert p.read_text()=='ok'\n"
        code += "assert os.environ.get('SYNTHETIC_PARENT_SECRET') is None\n"
        code += "for q in ('/state/codex/auth.json','/proc/1/root/state/codex/auth.json'):\n try: open(q,'rb').read(); raise SystemExit(32)\n except (PermissionError,FileNotFoundError): pass\n"
        code += "for fam,typ,addr in ((socket.AF_INET,socket.SOCK_STREAM,('127.0.0.1',0)),(socket.AF_INET,socket.SOCK_DGRAM,('127.0.0.1',0)),(socket.AF_INET6,socket.SOCK_STREAM,('::1',0,0,0))):\n s=socket.socket(fam,typ)\n try:\n  s.bind(addr); raise SystemExit(33)\n except OSError as e:\n  if e.errno not in (errno.EPERM,errno.EACCES): raise\n finally:s.close()\n"
        return cmd + ["--", str(codex), "sandbox", "-P", profile, "-C", "/workspace", "--",
                      "/usr/bin/python3", "-c", code]
    native = ["--no-daemon", "--ask-for-approval", "never", "exec", "--strict-config",
              "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--json", "-C", "/workspace"]
    if args.model: native += ["-m", args.model]
    if args.judge:
        schema = state / "output-schema.json"
        schema.write_text(json.dumps({"type":"object","required":["verdict","why"],
          "additionalProperties":False,"properties":{"verdict":{"type":"string","enum":["pass","fail"]},
          "why":{"type":"string"}}}), encoding="utf-8")
        native += ["--output-schema", str(schema)]
    native.append("-")
    return cmd + ["--", str(codex), *native]


def eval_main(argv):
    parser = Parser(description="isolated Codex eval runner")
    parser.add_argument("--mode", choices=("eval",), required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--auth-file")
    parser.add_argument("--judge", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    original_root = Path(args.root)
    if original_root.is_symlink(): raise PreflightError("fixture root symlink is rejected")
    root = original_root.resolve(strict=True)
    if not root.is_dir() or root == Path("/"): raise PreflightError("fixture root must be an existing directory")
    bwrap = shutil.which("bwrap")
    if not bwrap: raise PreflightError("bwrap not found; isolated eval requires bubblewrap")
    codex = shutil.which("codex")
    if not codex: raise PreflightError("Codex CLI not found")
    codex = Path(os.path.abspath(codex))
    if args.auth_file:
        approved_auth = _auth_source(args)
    elif args.dry_run or args.preflight:
        approved_auth = None
    else:
        approved_auth = _auth_source(args)
    with tempfile.TemporaryDirectory(prefix="codex-eval-private-") as temp:
        private = Path(temp); state = private / "state"; state.mkdir(mode=0o700)
        home = private / "home"; home.mkdir(mode=0o700)
        auth_target = state / "auth.json"
        if approved_auth is None:
            auth_target.write_text('{"synthetic_offline_canary":true}\n', encoding="utf-8")
        else:
            shutil.copyfile(approved_auth, auth_target)
        os.chmod(auth_target, 0o600)
        native = _native_binary(codex)
        config = _codex_config(str(native), judge=args.judge)
        (state / "config.toml").write_text(config, encoding="utf-8"); os.chmod(state / "config.toml", 0o600)
        env = _eval_env(home, state)
        if not args.model.strip(): raise PreflightError("an explicit model is required")
        version = _check_cli(codex, env, judge=args.judge)
        cmd = _bwrap_eval(args, codex, codex, state, home, env, dry=args.dry_run)
        if args.dry_run:
            printable = list(cmd)
            for i, value in enumerate(printable[:-1]):
                if value == "--setenv" and printable[i + 1].upper() in EVAL_PROXY_ENV:
                    printable[i + 2] = "<redacted-approved-proxy>"
            print(json.dumps({"provider":"codex","mode":"judge" if args.judge else "eval",
              "cli_version":version,"argv":printable,"config":config}, ensure_ascii=False))
            return 0
        if args.preflight:
            probe = subprocess.run([bwrap, "--ro-bind", "/usr", "/usr", "true"], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15, check=False)
            if probe.returncode: raise PreflightError("bubblewrap namespace preflight failed")
            canary = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, check=False)
            if canary.returncode: raise PreflightError("native profile/offline canary preflight failed")
            return 0
        # Every live model execution first runs the same offline controls.
        probe = subprocess.run([bwrap, "--ro-bind", "/usr", "/usr", "true"], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False)
        if probe.returncode: raise PreflightError("bubblewrap namespace preflight failed")
        preflight_args = argparse.Namespace(**vars(args)); preflight_args.preflight = True
        preflight_cmd = _bwrap_eval(preflight_args, codex, codex, state, home, env)
        canary = subprocess.run(preflight_cmd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        if canary.returncode: raise PreflightError("native profile/offline canary preflight failed")
        live = _bwrap_eval(args, codex, codex, state, home, env)
        completed = subprocess.run(live, env=env, stdin=sys.stdin, stdout=sys.stdout,
                                   stderr=subprocess.DEVNULL, check=False)
        return completed.returncode if completed.returncode >= 0 else 128 - completed.returncode


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if "--mode" in argv:
            return eval_main(argv)
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
    except subprocess.TimeoutExpired:
        print("codex-sandbox: offline preflight timed out", file=sys.stderr)
        return 2
    except (PreflightError, OSError, RuntimeError) as error:
        print("codex-sandbox: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
