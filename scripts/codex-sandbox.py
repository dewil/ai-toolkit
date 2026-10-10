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
import contextlib
import fcntl
import hashlib
import json
import fnmatch
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import platform
import re


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
        if not isinstance(value, str) or any(ch.isspace() or ord(ch) < 0x20 for ch in value): return False
        if "?" in value or "#" in value: return False
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname: return False
        if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None: return False
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment: return False
        if "%" in parsed.netloc: return False
        try: port = parsed.port
        except ValueError: return False
        if port is not None and not 1 <= port <= 65535: return False
        host = parsed.hostname
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host): return False
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
        'HOME = "/home/eval"', 'LANG = "C.UTF-8"', "", "[features]"]
    for name in ("apps", "hooks", "plugins", "remote_plugin", "multi_agent", "multi_agent_v2",
        "browser_use", "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
        "computer_use", "image_generation", "view_image", "code_mode_host", "shell_snapshot",
        "skill_search", "skill_mcp_dependency_install", "workspace_dependencies", "daemon_auto_start"):
        fields.append(f"{name} = false")
    if judge:
        fields += ["shell_tool = false", "unified_exec = false", "",
                   '[permissions.judge.filesystem.":workspace_roots"]', '"." = "read"']
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
        global_help = subprocess.run([str(codex), "--help"], env=env, capture_output=True,
                                     text=True, timeout=10, check=False)
        help_result = subprocess.run([str(codex), "exec", "--help"], env=env, capture_output=True,
                                     text=True, timeout=10, check=False)
        sandbox_help = subprocess.run([str(codex), "sandbox", "--help"], env=env, capture_output=True,
                                      text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PreflightError("Codex CLI capability check failed") from None
    version_text = (version.stdout + version.stderr).strip()
    if version.returncode or not re.search(r"(?:^|\s)codex-cli\s+" + re.escape(EVAL_VERSION) + r"(?:\s|$)", version_text):
        raise PreflightError("unsupported Codex CLI version")
    if global_help.returncode or help_result.returncode or sandbox_help.returncode:
        raise PreflightError("Codex CLI required command is unavailable")
    global_text = global_help.stdout + global_help.stderr
    for flag in ("--no-daemon", "--ask-for-approval"):
        if flag not in global_text: raise PreflightError("required global Codex CLI flag is unavailable")
    help_text = help_result.stdout + help_result.stderr
    exec_flags = ("--strict-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--json")
    if judge: exec_flags += ("--output-schema",)
    for flag in exec_flags:
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
            code += "import tempfile\n"
            code += "try:\n with tempfile.NamedTemporaryFile(dir='/workspace',prefix='.codex-eval-probe-'): raise SystemExit(34)\nexcept OSError: pass\n"
        else:
            code += "import tempfile\n"
            code += "with tempfile.NamedTemporaryFile(mode='w+',dir='/workspace',prefix='.codex-eval-probe-') as f:\n f.write('ok'); f.flush(); f.seek(0); assert f.read()=='ok'\n"
        host_canary = state.parent / "host-canary.txt"
        host_canary.write_text("SYNTHETIC_HOST_CANARY", encoding="utf-8")
        code += f"host=pathlib.Path({str(host_canary)!r})\n"
        code += "try: host.read_text(); raise SystemExit(35)\nexcept FileNotFoundError: pass\n"
        code += "try: host.write_text('changed'); raise SystemExit(36)\nexcept FileNotFoundError: pass\n"
        code += "if os.environ.get('SYNTHETIC_PARENT_SECRET') is not None: raise SystemExit(40)\n"
        code += "bad={'ALL_PROXY','NO_PROXY','all_proxy','no_proxy','SSH_AUTH_SOCK','ASANA_TOKEN','TELEGRAM_TOKEN','GH_TOKEN','GITHUB_TOKEN','OPENAI_API_KEY','AWS_SECRET_ACCESS_KEY','PARENT_CANARY_SECRET'}\n"
        code += "if any(k in bad or any(x in k.upper() for x in ('TOKEN','SECRET','API_KEY','PASSWORD','CREDENTIAL')) for k in os.environ): raise SystemExit(41)\n"
        code += "try:\n env=open('/proc/1/environ','rb').read(1024*1024)\nexcept OSError: env=b''\nif b'SYNTHETIC_PARENT_SECRET=' in env: raise SystemExit(42)\n"
        code += "for q in ('/state/codex/auth.json','/proc/1/root/state/codex/auth.json'):\n try: open(q,'rb').close(); raise SystemExit(32)\n except OSError: pass\n"
        code += "for fam,typ,addr in ((socket.AF_INET,socket.SOCK_STREAM,('127.0.0.1',0)),(socket.AF_INET,socket.SOCK_DGRAM,('127.0.0.1',0)),(socket.AF_INET6,socket.SOCK_STREAM,('::1',0,0,0)),(socket.AF_INET6,socket.SOCK_DGRAM,('::1',0,0,0))):\n s=None\n try:\n  s=socket.socket(fam,typ); s.bind(addr); raise SystemExit(33)\n except OSError as e:\n  if e.errno not in (errno.EPERM,errno.EACCES): raise\n finally:\n  if s is not None:s.close()\n"
        code += "try: os.fstat(int(sys.argv[1])); raise SystemExit(43)\nexcept OSError: pass\n"
        return cmd + ["--", str(codex), "sandbox", "-P", profile, "-C", "/workspace", "--",
                      "/usr/bin/python3", "-c", code, "64"]
    if args.judge:
        schema = state / "output-schema.json"
        schema.write_text(json.dumps({"type":"object","required":["verdict","why"],
          "additionalProperties":False,"properties":{"verdict":{"type":"string","enum":["pass","fail"]},
          "why":{"type":"string"}}}), encoding="utf-8")
    native = _native_exec_argv(args)
    return cmd + ["--", str(codex), *native]


@contextlib.contextmanager
def _private_workspace(args):
    if not args.state_dir:
        with tempfile.TemporaryDirectory(prefix="codex-eval-private-") as temp:
            yield Path(temp)
        return
    root = Path(args.state_dir)
    if root.is_symlink(): raise PreflightError("private state directory cannot be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = root.stat()
    if not root.is_dir() or st.st_mode & 0o077:
        raise PreflightError("private state directory must be mode 700")
    if any(root.iterdir()): raise PreflightError("private state directory must be empty")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_process_group(argv, env, timeout):
    process = subprocess.Popen(argv, env=env, stdin=sys.stdin, stdout=sys.stdout,
        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    try:
        code = process.wait(timeout=timeout)
        return code if code >= 0 else 128 - code
    except subprocess.TimeoutExpired:
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
        return 124


def _run_offline_canary(command, env, root, marker):
    prefix = ".codex-eval-probe-"
    if any(path.name.startswith(prefix) for path in root.iterdir()):
        raise PreflightError("fixture reserves the offline canary filename prefix")
    marker_fd = os.open(marker, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        inherited_fd = fcntl.fcntl(marker_fd, fcntl.F_DUPFD, 64)
        os.set_inheritable(inherited_fd, True)
    finally: os.close(marker_fd)
    cmd = list(command)
    # The last argv word is the synthetic descriptor id expected by the canary.
    cmd[-1] = str(inherited_fd)
    try:
        result = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            check=False, close_fds=True)
        return result
    finally:
        try: os.close(inherited_fd)
        except OSError: pass
        for path in root.iterdir():
            if path.name.startswith(prefix):
                try: path.unlink()
                except OSError: pass


def _strict_exec_startup_canary(args, codex, state, home, env):
    """Load exact generated exec config with no network and fail before inference."""
    exec_args = argparse.Namespace(**vars(args))
    exec_args.preflight = False
    exec_args.dry_run = False
    cmd = _bwrap_eval(exec_args, codex, codex, state, home, env)
    try: cmd.remove("--share-net")
    except ValueError: raise PreflightError("strict exec startup command lacks outer isolation") from None
    try:
        index = cmd.index("--output-schema")
        cmd[index + 1] = "/state/codex/.missing-strict-startup-schema.json"
    except ValueError:
        # The executor normally has no schema option; force the CLI to load
        # strict config, then stop at this deliberately absent schema before inference.
        try: prompt_index = cmd.index("-")
        except ValueError: raise PreflightError("strict exec startup command is malformed") from None
        cmd[prompt_index:prompt_index] = ["--output-schema", "/state/codex/.missing-strict-startup-schema.json"]
    try:
        result = subprocess.run(cmd, env=env, input="synthetic offline startup check", text=True,
            capture_output=True, timeout=30, check=False, close_fds=True)
    except (OSError, subprocess.TimeoutExpired):
        raise PreflightError("strict native exec startup validation failed") from None
    if result.returncode and "failed to read output schema file" in (result.stderr or "").lower():
        return
    raise PreflightError("strict native exec startup validation failed")


def _native_exec_argv(args):
    native = ["--no-daemon", "--ask-for-approval", "never", "exec", "--strict-config",
              "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--json", "-C", "/workspace"]
    if args.model: native += ["-m", args.model]
    if args.judge: native += ["--output-schema", "/state/codex/output-schema.json"]
    native.append("-")
    return native


def _write_metadata(args, version, config, native_binary):
    if not args.metadata_file: return
    metadata_path = Path(args.metadata_file)
    expected = Path(args.state_dir).parent / "metadata.json" if args.state_dir else None
    if expected is None or metadata_path != expected:
        raise PreflightError("metadata output must be adjacent to private state")
    normalized_config = config.replace(json.dumps(str(native_binary)), '"<native-codex-binary>"')
    argv = ["codex", *_native_exec_argv(args)]
    isolation_argv, model_argv = list(argv), list(argv)
    if "-m" in isolation_argv:
        index = isolation_argv.index("-m")
        isolation_argv[index + 1] = "<model>"
    doc = {"cli_version": version, "config_toml": normalized_config,
           "argv": model_argv, "isolation_argv": isolation_argv}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(metadata_path, flags, 0o600)
    try:
        view = memoryview(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally: os.close(fd)


def eval_main(argv):
    parser = Parser(description="isolated Codex eval runner")
    parser.add_argument("--mode", choices=("eval",), required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--auth-file")
    parser.add_argument("--state-dir")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--metadata-file")
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
    if args.timeout <= 0 or args.timeout > 3600: raise PreflightError("timeout must be between 0 and 3600 seconds")
    with _private_workspace(args) as private:
        state = private / "state"; state.mkdir(mode=0o700)
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
        check_env = dict(env, CODEX_HOME=str(state), HOME=str(home))
        version = _check_cli(codex, check_env, judge=args.judge)
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
            canary = _run_offline_canary(cmd, env, root, state.parent / "host-canary.txt")
            if canary.returncode: raise PreflightError(f"native profile/offline canary failed at control {canary.returncode}")
            marker = state.parent / "host-canary.txt"
            if marker.read_text(encoding="utf-8") != "SYNTHETIC_HOST_CANARY":
                raise PreflightError("host canary changed during native preflight")
            marker.unlink()
            _strict_exec_startup_canary(args, codex, state, home, env)
            return 0
        # Every live model execution first runs the same offline controls.
        preflight_args = argparse.Namespace(**vars(args)); preflight_args.preflight = True
        preflight_cmd = _bwrap_eval(preflight_args, codex, codex, state, home, env)
        canary = _run_offline_canary(preflight_cmd, env, root, state.parent / "host-canary.txt")
        if canary.returncode: raise PreflightError(f"native profile/offline canary failed at control {canary.returncode}")
        marker = state.parent / "host-canary.txt"
        if marker.read_text(encoding="utf-8") != "SYNTHETIC_HOST_CANARY":
            raise PreflightError("host canary changed during native preflight")
        marker.unlink()
        _strict_exec_startup_canary(args, codex, state, home, env)
        _write_metadata(args, version, config, native)
        live = _bwrap_eval(args, codex, codex, state, home, env)
        completed = _run_process_group(live, env, args.timeout)
        if completed == 124: print("codex-sandbox: isolated eval timed out", file=sys.stderr)
        return completed


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
