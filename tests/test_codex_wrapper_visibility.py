import errno
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/codex-sandbox.py"
PREFIX = "FIXTURE:"
PROXIES = {
    "HTTP_PROXY": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:10",
    "ALL_PROXY": "socks5://127.0.0.1:11",
    "NO_PROXY": "localhost,127.0.0.1,.invalid",
}
CLI_ARGS = ["--fixture-exit", "23"]
STDIN = "synthetic stdin\n"


def run(argv, env, stdin=STDIN):
    return subprocess.run(
        [str(arg) for arg in argv],
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def diagnostic(result):
    return (
        f"exit={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def require_live(case):
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
    if bwrap is None:
        case.skipTest("NOTRUN: real bwrap unavailable")
    try:
        result = run(
            [
                bwrap, "--die-with-parent", "--unshare-all",
                "--ro-bind", "/", "/",
                "--", "/usr/bin/true",
            ],
            case.env,
            stdin="",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        case.skipTest(f"NOTRUN: real bwrap capability probe: {exc}")
    if result.returncode:
        case.skipTest(
            "NOTRUN: real bwrap capability probe failed: "
            + diagnostic(result)
        )


def invoke(case, dry=False, env=None):
    argv = [
        sys.executable, "-B", str(SCRIPT),
        "audit", "--root", str(case.project),
    ]
    if dry:
        argv.append("--dry-run")
    return run(argv + ["--"] + CLI_ARGS, env or case.env)


def report(case, result, sandbox):
    case.assertEqual(result.returncode, 23, diagnostic(result))
    records = [
        json.loads(line[len(PREFIX):])
        for line in result.stdout.splitlines()
        if line.startswith(PREFIX)
    ]
    case.assertEqual(len(records), 1, diagnostic(result))
    data = records[0]
    case.assertEqual(data["proxy"], PROXIES)
    case.assertEqual(data["stdin"], STDIN)
    case.assertEqual(data["codex_home"], str(case.codex_home))
    case.assertEqual(data["home_marker"], "checked-home")
    case.assertEqual(data["argv"][:3], ["exec", "-C", str(case.project)])
    case.assertEqual(data["argv"][-len(CLI_ARGS):], CLI_ARGS)
    case.assertEqual(data["outside_visible"], not sandbox)
    case.assertEqual(data["sibling_visible"], not sandbox)
    if sandbox:
        case.assertEqual(data["write_errno"], errno.EROFS)
        case.assertFalse(case.write_probe.exists())
    else:
        case.assertEqual(data["write_errno"], 0)
        case.assertEqual(case.write_probe.read_text(), "unexpected write")
    return data


def assert_mount_scope(case, result):
    case.assertEqual(result.returncode, 0, diagnostic(result))
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    case.assertTrue(lines, diagnostic(result))
    argv = shlex.split("\n".join(lines))
    case.assertEqual(Path(argv[0]).name, "bwrap")
    mounts = [
        (argv[i], argv[i + 1], argv[i + 2])
        for i in range(len(argv) - 2)
        if argv[i] in {
            "--bind", "--ro-bind", "--dev-bind",
            "--bind-try", "--ro-bind-try", "--dev-bind-try",
        }
    ]
    case.assertIn(
        ("--ro-bind", str(case.package), str(case.package)), mounts
    )
    case.assertIn(
        ("--ro-bind", str(case.project), str(case.project)), mounts
    )
    case.assertIn(("--ro-bind", "/usr", "/usr"), mounts)

    # Reject exposing any host ancestor of either hidden fixture.
    # Explicit package/PROJECT/CODEX_HOME binds do not contain these files.
    for _, source, _ in mounts:
        source = Path(source)
        if not source.is_absolute():
            continue
        source = source.resolve()
        for hidden in (case.outside, case.sibling):
            case.assertFalse(
                source == hidden or source in hidden.parents,
                f"overbroad host bind: {source} exposes {hidden}",
            )
    case.assertNotIn(PREFIX, result.stdout)
    case.assertFalse(case.write_probe.exists())


class CodexWrapperVisibilityTests(unittest.TestCase):
    def setUp(self):
        if not SCRIPT.is_file():
            self.fail(f"PUBLIC CLI missing: {SCRIPT}; not a documentation RED")
        if not Path("/usr/bin/python3").is_file():
            self.skipTest("NOTRUN: fixture requires /usr/bin/python3")

        temp = tempfile.TemporaryDirectory(prefix="codex-visibility-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.codex_home = self.home / "checked-codex-home"
        self.package = self.home / "node_modules" / "synthetic-codex"
        self.bin = self.project / "bin"
        for directory in (
            self.codex_home, self.package / "bin", self.bin
        ):
            directory.mkdir(parents=True)
        (self.package / "package.json").write_text(
            '{"name":"synthetic-codex","version":"0.0.0"}'
        )
        self.sibling = (
            self.home / "node_modules" / "unrelated-package" / "marker"
        )
        self.sibling.parent.mkdir()
        self.sibling.write_text("hidden sibling")
        self.outside = self.home / "outside-marker"
        self.outside.write_text("hidden home file")
        (self.codex_home / "marker").write_text("checked-home")
        self.write_probe = self.project / "write-probe"

        self.backend = self.package / "bin" / "codex.py"
        fixture = {
            "outside": str(self.outside),
            "sibling": str(self.sibling),
            "write_probe": str(self.write_probe),
            "proxy_keys": list(PROXIES),
        }
        self.backend.write_text(
            "#!/usr/bin/python3\n"
            "import json, os, pathlib, sys\n"
            f"fixture = {fixture!r}\n"
            "write_errno = 0\n"
            "try:\n"
            "    pathlib.Path(fixture['write_probe']).write_text("
            "'unexpected write')\n"
            "except OSError as exc:\n"
            "    write_errno = exc.errno\n"
            "home = pathlib.Path(os.environ['CODEX_HOME'])\n"
            "data = {\n"
            "    'argv': sys.argv[1:],\n"
            "    'stdin': sys.stdin.read(),\n"
            "    'proxy': {k: os.environ.get(k)"
            " for k in fixture['proxy_keys']},\n"
            "    'codex_home': str(home),\n"
            "    'home_marker': (home / 'marker').read_text(),\n"
            "    'outside_visible':"
            " pathlib.Path(fixture['outside']).exists(),\n"
            "    'sibling_visible':"
            " pathlib.Path(fixture['sibling']).exists(),\n"
            "    'write_errno': write_errno,\n"
            "}\n"
            f"print({PREFIX!r} + json.dumps(data), flush=True)\n"
            "sys.exit(23)\n"
        )
        self.backend.chmod(0o755)
        self.codex = self.bin / "codex"
        self.codex.symlink_to(self.backend)

        # Explicit environment: no inherited credentials or real CLI settings.
        self.env = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "LANG": "C",
            **PROXIES,
        }

    def test_direct_symlink_dry_run_has_narrow_mounts(self):
        require_live(self)  # Honest skip if the helper's probe cannot run.
        assert_mount_scope(self, invoke(self, dry=True))

    def test_direct_symlink_live_preserves_isolation_and_io(self):
        require_live(self)
        report(self, invoke(self), sandbox=True)

    def test_wrapper_works_on_host_but_backend_is_hidden(self):
        require_live(self)
        self.codex.unlink()
        backend = shlex.quote(str(self.backend))
        self.codex.write_text(
            "#!/bin/sh\n"
            f"if [ ! -x {backend} ]; then\n"
            "  echo 'SYNTHETIC_BACKEND_HIDDEN' >&2\n"
            "  exit 126\n"
            "fi\n"
            f"exec {backend} \"$@\"\n"
        )
        self.codex.chmod(0o755)

        self.assertTrue(self.backend.is_file())
        self.assertTrue(os.access(self.backend, os.X_OK))
        host = run(
            [self.codex, "exec", "-C", self.project, *CLI_ARGS], self.env
        )
        report(self, host, sandbox=False)
        self.write_probe.unlink()

        live = invoke(self)
        self.assertEqual(live.returncode, 126, diagnostic(live))
        self.assertIn("SYNTHETIC_BACKEND_HIDDEN", live.stderr)
        self.assertNotIn(PREFIX, live.stdout)
        self.assertFalse(self.write_probe.exists())

    def test_task_symlink_restores_only_fixture_backend(self):
        require_live(self)
        self.codex.unlink()
        backend = shlex.quote(str(self.backend))
        wrapper_text = f"#!/bin/sh\nexec {backend} \"$@\"\n"
        self.codex.write_text(wrapper_text)
        self.codex.chmod(0o755)

        # This fixture wrapper only execs the backend; it adds no policy.
        host = run(
            [self.codex, "exec", "-C", self.project, *CLI_ARGS], self.env
        )
        report(self, host, sandbox=False)
        self.write_probe.unlink()

        parent_path = os.environ.get("PATH")
        original_env = self.env.copy()
        task_bin = self.project / "task-bin"
        task_bin.mkdir()
        link = task_bin / "codex"
        link.symlink_to(self.backend)
        child_env = {
            **self.env,
            "PATH": f"{task_bin}:{self.env['PATH']}",
        }
        try:
            assert_mount_scope(self, invoke(self, dry=True, env=child_env))
            report(self, invoke(self, env=child_env), sandbox=True)
        finally:
            link.unlink()

        self.assertFalse(os.path.lexists(link))
        self.assertEqual(self.codex.read_text(), wrapper_text)
        self.assertEqual(self.env, original_env)
        self.assertEqual(os.environ.get("PATH"), parent_path)


if __name__ == "__main__":
    unittest.main()
