#!/usr/bin/env python3
"""Independent regressions from security review; no model calls or real auth.

Actual CLI probes use only --dry-run/--preflight and synthetic mode-600 auth.
These tests supplement, and never weaken, the frozen blind RED tests.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import select
import struct
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_eval_independent_review", ROOT / "scripts/run-evals.py")
engine = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = engine
SPEC.loader.exec_module(engine)


class ContextBoundaryReview(unittest.TestCase):
    def test_dangling_generated_agents_never_writes_outside_fixture(self):
        with tempfile.TemporaryDirectory(prefix="review-context-") as name:
            base = Path(name)
            fixture = base / "fixture"
            fixture.mkdir()
            outside = base / "outside-created-by-bug"
            (fixture / "CLAUDE.md").write_text("SYNTHETIC_POLICY\n", encoding="utf-8")
            (fixture / "AGENTS.md").symlink_to(outside)
            try:
                with self.assertRaises((ValueError, OSError)):
                    engine.prepare_codex_fixture(fixture, {})
            finally:
                self.assertFalse(outside.exists(), "context preparation wrote outside fixture")

    def test_dangling_source_is_not_silently_empty_context(self):
        for source in ("AGENTS.md", "CLAUDE.md"):
            with self.subTest(source=source), tempfile.TemporaryDirectory(prefix="review-source-") as name:
                fixture = Path(name)
                (fixture / source).symlink_to(fixture / "missing-target")
                with self.assertRaises((ValueError, OSError)):
                    engine.prepare_codex_fixture(fixture, {})

    def test_import_symlink_parent_is_rejected_even_when_target_inside_fixture(self):
        with tempfile.TemporaryDirectory(prefix="review-import-") as name:
            fixture = Path(name)
            actual = fixture / "actual"
            actual.mkdir()
            (actual / "policy.md").write_text("SYNTHETIC_IMPORTED_POLICY\n", encoding="utf-8")
            (fixture / "alias").symlink_to(actual, target_is_directory=True)
            (fixture / "CLAUDE.md").write_text("@alias/policy.md\n", encoding="utf-8")
            with self.assertRaises((ValueError, OSError)):
                engine.prepare_codex_fixture(fixture, {})


class TraceAndTimeoutReview(unittest.TestCase):
    def test_timeout_with_partial_bytes_returns_infrastructure_instead_of_crashing(self):
        with tempfile.TemporaryDirectory(prefix="review-timeout-") as name:
            base = Path(name)
            fixture = base / "fixture"
            fixture.mkdir()
            timeout = subprocess.TimeoutExpired(["synthetic-wrapper"], 1,
                output=b'{"type":"thread.started","thread_id":"synthetic"}\n')
            with patch.object(engine.subprocess, "run", side_effect=timeout):
                run, _ = engine.run_once("review-timeout", {"_provider": "codex"},
                    "synthetic", fixture, "synthetic-model", base / "trace.jsonl")
            self.assertTrue(run.infra)
            self.assertEqual(run.provider, "codex")

    def test_nonzero_startup_has_safe_process_reason_beyond_missing_terminal(self):
        with tempfile.TemporaryDirectory(prefix="review-startup-reason-") as name:
            base = Path(name)
            fixture = base / "fixture"
            fixture.mkdir()
            result = subprocess.CompletedProcess(["synthetic-wrapper"], 2, stdout="",
                stderr="native strict config validation failed SYNTHETIC_SECRET_NEVER_SURFACE")
            with patch.object(engine.subprocess, "run", return_value=result):
                run, _ = engine.run_once("review-startup", {"_provider": "codex"},
                    "synthetic", fixture, "synthetic-model", base / "trace.jsonl")
            self.assertTrue(run.infra)
            self.assertNotEqual(run.infra, engine.parse_codex_transcript([]).infra,
                "nonzero startup discarded the wrapper/process failure reason")
            self.assertNotIn("SYNTHETIC_SECRET_NEVER_SURFACE", run.infra)

    def test_judge_timeout_uses_parent_owned_private_state_and_cleanup_grace(self):
        created_states = []
        def interrupted_wrapper(argv, **kwargs):
            self.assertIn("--state-dir", argv,
                          "judge auth state is unowned if wrapper is killed")
            self.assertIn("--timeout", argv,
                          "judge wrapper needs its own shorter cleanup deadline")
            state = Path(argv[argv.index("--state-dir") + 1])
            inner_timeout = float(argv[argv.index("--timeout") + 1])
            self.assertGreaterEqual(kwargs["timeout"], inner_timeout + 35,
                                    "outer judge deadline precedes wrapper cleanup")
            state.mkdir(parents=True, exist_ok=True)
            (state / "auth.json").write_text("SYNTHETIC_JUDGE_AUTH", encoding="utf-8")
            created_states.append(state)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial")
        run = engine.Run(provider="codex", completed=True, text="synthetic")
        with patch.object(engine.subprocess, "run", side_effect=interrupted_wrapper):
            result = engine.codex_judge_outcome("synthetic criterion", run, "synthetic-model")
        self.assertEqual(result["outcome"], "judge_error")
        self.assertTrue(created_states)
        self.assertTrue(all(not state.exists() for state in created_states),
                        "judge caller left synthetic private auth state after timeout")

    def test_startup_runtime_error_cannot_turn_green_after_completed_turn(self):
        events = [
            {"type": "thread.started", "thread_id": "synthetic"},
            {"type": "item.completed", "item": {"id": "startup-error", "type": "error",
                "message": "Code Mode unavailable: required host disabled"}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "final", "type": "agent_message",
                "text": "synthetic final text"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1,
                                                   "cached_input_tokens": 0}},
        ]
        run = engine.parse_codex_transcript([json.dumps(event) for event in events])
        self.assertTrue(run.completed)
        self.assertTrue(run.infra, "startup runtime error vanished after successful turn terminal")

    def test_generic_no_tool_assertion_cannot_pass_after_observed_file_change(self):
        assertion = {"no_tool_call": {}}
        try:
            engine.codex_scenario_supported({"hard": [assertion], "soft": []})
        except ValueError:
            return  # Explicit unsupported before API is an allowed safe outcome.
        events = [
            {"type": "thread.started", "thread_id": "synthetic"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "f", "type": "file_change",
                "status": "completed", "changes": [{"path": "forbidden.txt", "kind": "update"}]}},
            {"type": "turn.completed"},
        ]
        run = engine.parse_codex_transcript([json.dumps(event) for event in events])
        if run.infra:
            return  # An unsupported tool trace must not become a behavioral PASS.
        self.assertFalse(engine.check(assertion, run, {})[0],
                         "observed file-changing tool vanished from generic negative assertion")


class RequiredHostRefusals(unittest.TestCase):
    def test_cli_version_uses_normalized_version_without_stderr_warning_values(self):
        with tempfile.TemporaryDirectory(prefix="review-version-warning-") as name:
            base = Path(name)
            fixture = base / "fixture"
            fixture.mkdir()
            binary_dir = base / "bin"
            binary_dir.mkdir()
            auth = base / "auth.json"
            auth.write_text('{"synthetic":true}', encoding="utf-8")
            auth.chmod(0o600)
            warning = "SYNTHETIC_VERSION_WARNING_NEVER_EXPORT " + str(base / "private-state")
            called = base / "inference-called"
            def executable(path, source):
                path.write_text("#!" + sys.executable + "\n" + source, encoding="utf-8")
                path.chmod(0o755)
            executable(binary_dir / "bwrap", "raise SystemExit(0)\n")
            executable(binary_dir / "codex-code-mode-host", "print('codex-code-mode-host --listen stdio')\n")
            executable(binary_dir / "codex", "import pathlib,sys\n"
                "if '--version' in sys.argv:\n print('codex-cli 0.162.1'); print(" + repr(warning) + ",file=sys.stderr); raise SystemExit(0)\n"
                "if '--help' in sys.argv: print('--no-daemon --ask-for-approval --strict-config --ignore-rules --ephemeral --skip-git-repo-check --json --output-schema --permission-profile'); raise SystemExit(0)\n"
                "pathlib.Path(" + repr(str(called)) + ").write_text('UNEXPECTED'); raise SystemExit(98)\n")
            done = subprocess.run([sys.executable, str(ROOT / "scripts/codex-sandbox.py"), "--mode", "eval",
                "--root", str(fixture), "--model", "synthetic", "--auth-file", str(auth), "--dry-run"],
                capture_output=True, text=True, timeout=10,
                env={"PATH": str(binary_dir), "HOME": str(base / "home"), "LANG": "C.UTF-8"})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["cli_version"], "codex-cli 0.162.1")
            self.assertNotIn("SYNTHETIC_VERSION_WARNING_NEVER_EXPORT", done.stdout + done.stderr)
            self.assertFalse(called.exists())

    def test_missing_or_invalid_bundled_host_refuses_before_inference_with_safe_reason(self):
        for broken_protocol in (False, True):
            with self.subTest(broken_protocol=broken_protocol), tempfile.TemporaryDirectory(prefix="review-host-refusal-") as name:
                base = Path(name)
                fixture = base / "fixture"
                fixture.mkdir()
                binary_dir = base / "bin"
                binary_dir.mkdir()
                auth = base / "auth.json"
                auth.write_text('{"synthetic":"HOST_AUTH_NEVER_PRINT"}', encoding="utf-8")
                auth.chmod(0o600)
                called = base / "inference-called"
                def executable(path, source):
                    path.write_text("#!" + sys.executable + "\n" + source, encoding="utf-8")
                    path.chmod(0o755)
                executable(binary_dir / "bwrap", "import os,sys\n"
                    "if '--' in sys.argv:\n i=sys.argv.index('--'); os.execv(sys.argv[i+1],sys.argv[i+1:])\n")
                executable(binary_dir / "codex", "import pathlib,sys\n"
                    "if '--version' in sys.argv: print('codex-cli 0.162.1'); raise SystemExit(0)\n"
                    "if '--help' in sys.argv: print('--no-daemon --ask-for-approval --strict-config --ignore-rules --ephemeral --skip-git-repo-check --json --output-schema --permission-profile'); raise SystemExit(0)\n"
                    "if 'sandbox' in sys.argv: raise SystemExit(0)\n"
                    "if '--output-schema' in sys.argv: print('Failed to read output schema file', file=sys.stderr); raise SystemExit(1)\n"
                    + "pathlib.Path(" + repr(str(called)) + ").write_text('UNEXPECTED'); raise SystemExit(98)\n")
                if broken_protocol:
                    executable(binary_dir / "codex-code-mode-host", "import sys\n"
                        "if '--help' in sys.argv: print('codex-code-mode-host --listen stdio'); raise SystemExit(0)\n"
                        "print('HOST_PROTOCOL_SECRET_NEVER_PRINT',file=sys.stderr); raise SystemExit(97)\n")
                env = {"PATH": str(binary_dir), "HOME": str(base / "home"), "LANG": "C.UTF-8"}
                flags = ("--preflight",) if broken_protocol else ("--dry-run", "--preflight")
                for flag in flags:
                    done = subprocess.run([sys.executable, str(ROOT / "scripts/codex-sandbox.py"),
                        "--mode", "eval", "--root", str(fixture), "--model", "synthetic-model",
                        "--auth-file", str(auth), flag], input="synthetic", capture_output=True,
                        text=True, env=env, timeout=10)
                    self.assertNotEqual(done.returncode, 0, "required bundled runtime was not checked")
                    self.assertRegex(done.stderr.lower(), r"code.?mode.*host|host.*(?:unavailable|readiness|protocol)")
                    self.assertNotIn("HOST_PROTOCOL_SECRET_NEVER_PRINT", done.stdout + done.stderr)
                    self.assertNotIn("HOST_AUTH_NEVER_PRINT", done.stdout + done.stderr)
                    self.assertFalse(called.exists(), "inference attempted during offline refusal")


@unittest.skipUnless(sys.platform == "linux" and shutil.which("codex") and shutil.which("bwrap"),
                     "actual Linux Codex/bubblewrap unavailable; OS acceptance remains NOTRUN")
class NativeWrapperReview(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="codex-review-")
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.fixture = self.base / "fixture"
        self.fixture.mkdir()
        self.auth = self.base / "synthetic-auth.json"
        self.auth.write_text('{"synthetic":"AUTH_REVIEW_CANARY_NEVER_PRINT"}\n', encoding="utf-8")
        self.auth.chmod(0o600)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(self.base / "empty-home"), "LANG": "C.UTF-8",
                    "SYNTHETIC_PARENT_SECRET": "ENV_REVIEW_CANARY_NEVER_PRINT"}

    def invoke(self, flag, *, judge=False, proxy=None):
        env = dict(self.env)
        if proxy is not None:
            env["HTTPS_PROXY"] = proxy
        argv = [sys.executable, str(ROOT / "scripts/codex-sandbox.py"), "--mode", "eval",
                "--root", str(self.fixture), "--model", "synthetic-no-model-call",
                "--auth-file", str(self.auth), flag]
        if judge:
            argv.append("--judge")
        original = self.auth.read_bytes()
        done = subprocess.run(argv, input="synthetic offline only", capture_output=True,
                              text=True, env=env, timeout=60)
        self.assertEqual(self.auth.read_bytes(), original, "source auth changed")
        for marker in ("AUTH_REVIEW_CANARY_NEVER_PRINT", "ENV_REVIEW_CANARY_NEVER_PRINT",
                       "PROXY_REVIEW_CANARY_NEVER_PRINT"):
            self.assertNotIn(marker, done.stdout + done.stderr)
        self.assertNotIn(str(self.auth), done.stdout, "dry-run exposed source auth path")
        return done

    def test_actual_dryrun_supports_global_and_exec_flags(self):
        done = self.invoke("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        body = json.loads(done.stdout)
        self.assertEqual(body["provider"], "codex")
        self.assertIn("0.162.1", body["cli_version"])
        self.assertIn("--no-daemon", body["argv"])
        self.assertIn("--ask-for-approval", body["argv"])
        self.assertNotIn("--sandbox", body["argv"])
        self.assertEqual(tomllib.loads(body["config"])["default_permissions"], "eval")

    def test_eval_keeps_a_native_command_capability_while_judge_disables_commands(self):
        configurations = {}
        for judge in (False, True):
            done = self.invoke("--dry-run", judge=judge)
            self.assertEqual(done.returncode, 0, done.stderr)
            configurations[judge] = tomllib.loads(json.loads(done.stdout)["config"])["features"]
        self.assertTrue(configurations[False].get("shell_tool", True) or
                        configurations[False].get("unified_exec", True),
                        "executor disabled every native command capability")
        self.assertFalse(configurations[True]["shell_tool"])
        self.assertFalse(configurations[True]["unified_exec"])

    def test_actual_strict_exec_config_reaches_missing_schema_without_network(self):
        done = self.invoke("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        manifest = json.loads(done.stdout)
        state = self.base / "strict-state"
        state.mkdir(mode=0o700)
        (state / "auth.json").write_bytes(self.auth.read_bytes())
        (state / "auth.json").chmod(0o600)
        (state / "config.toml").write_text(manifest["config"], encoding="utf-8")
        argv = list(manifest["argv"])
        self.assertIn("--strict-config", argv)
        argv.remove("--share-net")  # No API network even if startup ordering changes.
        mapped_state = False
        for index, word in enumerate(argv[:-2]):
            if word == "--bind" and argv[index + 2] == "/state/codex":
                argv[index + 1] = str(state)
                mapped_state = True
        self.assertTrue(mapped_state, "public manifest omitted private state mount")
        self.assertEqual(argv[-1], "-")
        argv[-1:-1] = ["--output-schema", "/deliberately-missing-review-schema.json"]
        check = subprocess.run(argv, input="SYNTHETIC_STDIN", capture_output=True,
                               text=True, env=self.env, timeout=5)
        self.assertNotEqual(check.returncode, 0, "missing-schema sentinel must stop before model")
        self.assertIn("Failed to read output schema file", check.stderr,
                      "strict executor rejected config before the pre-model schema sentinel")
        self.assertNotIn("unknown configuration field", check.stderr)
        self.assertNotIn("AUTH_REVIEW_CANARY_NEVER_PRINT", check.stdout + check.stderr)

    def test_executor_enables_required_local_code_mode_host(self):
        for judge in (False, True):
            with self.subTest(judge=judge):
                done = self.invoke("--dry-run", judge=judge)
                self.assertEqual(done.returncode, 0, done.stderr)
                config = tomllib.loads(json.loads(done.stdout)["config"])
                self.assertIs(config["features"].get("code_mode_host"), True,
                              "selected native model requires local Code Mode host")

    def test_bundled_code_mode_host_runs_from_trusted_outer_runtime_without_network(self):
        done = self.invoke("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        manifest = json.loads(done.stdout)
        config = tomllib.loads(manifest["config"])
        binaries = [Path(name) for name, mode in config["permissions"]["eval"]["filesystem"].items()
                    if name.startswith("/") and Path(name).name == "codex" and mode == "read"]
        self.assertEqual(len(binaries), 1, "trusted native runtime path unavailable")
        host = binaries[0].parent / "codex-code-mode-host"
        self.assertTrue(host.is_file(), "installed bundled Code Mode host unavailable")
        state = self.base / "bundled-host-state"
        state.mkdir(mode=0o700)
        argv = list(manifest["argv"])
        argv.remove("--share-net")
        for index, word in enumerate(argv[:-2]):
            if word == "--bind" and argv[index + 2] == "/state/codex":
                argv[index + 1] = str(state)
        boundary = argv.index("--")
        argv = [*argv[:boundary + 1], str(host), "--help"]
        check = subprocess.run(argv, input="", capture_output=True, text=True,
                               env=self.env, timeout=5)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("codex-code-mode-host", check.stdout)
        self.assertIn("stdio", check.stdout)

    def test_actual_bundled_host_readiness_cell_has_no_raw_io(self):
        done = self.invoke("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        manifest = json.loads(done.stdout)
        config = tomllib.loads(manifest["config"])
        native = next(Path(name) for name, mode in config["permissions"]["eval"]["filesystem"].items()
                      if name.startswith("/") and Path(name).name == "codex" and mode == "read")
        state = self.base / "readiness-state"
        state.mkdir(mode=0o700)
        argv = list(manifest["argv"])
        argv.remove("--share-net")
        for index, word in enumerate(argv[:-2]):
            if word == "--bind" and argv[index + 2] == "/state/codex":
                argv[index + 1] = str(state)
        argv = [*argv[:argv.index("--") + 1], str(native.parent / "codex-code-mode-host")]
        process = subprocess.Popen(argv, env=self.env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def send(value):
            payload = json.dumps(value, separators=(",", ":")).encode()
            process.stdin.write(struct.pack("<I", len(payload)) + payload)
            process.stdin.flush()
        def read_exact(length):
            result = b""
            while len(result) < length:
                self.assertTrue(select.select([process.stdout], [], [], 3)[0], "host protocol timed out")
                part = os.read(process.stdout.fileno(), length - len(result))
                self.assertTrue(part, "host closed before readiness result")
                result += part
            return result
        def receive():
            size = struct.unpack("<I", read_exact(4))[0]
            self.assertLessEqual(size, 1024 * 1024)
            return json.loads(read_exact(size))
        try:
            send({"type": "connection/hello", "supportedVersions": [1],
                  "requiredCapabilities": [], "optionalCapabilities": []})
            self.assertEqual(receive()["type"], "connection/ready")
            send({"type": "operation/request", "id": 1,
                  "request": {"method": "session/open", "sessionId": "review"}})
            self.assertEqual(receive()["result"]["status"], "ok")
            source = ("const r={positive:2+3};for(const n of ['node:fs','node:net','file:///state/codex/auth.json'])"
                      "{try{await import(n);r[n]='UNSAFE';}catch(e){r[n]=String(e);}}"
                      "r.globals={process:typeof process,require:typeof require,fetch:typeof fetch,Deno:typeof Deno,"
                      "Bun:typeof Bun,WebSocket:typeof WebSocket,XMLHttpRequest:typeof XMLHttpRequest,"
                      "Worker:typeof Worker,Buffer:typeof Buffer};r.toolKeys=Object.keys(tools);"
                      "r.toolCount=ALL_TOOLS.length;text(JSON.stringify(r));")
            send({"type": "operation/request", "id": 2, "request": {"method": "session/execute",
                  "sessionId": "review", "request": {"tool_call_id": "synthetic", "source": source,
                  "enabled_tools": [], "max_output_tokens": 2000}}})
            result = None
            for _ in range(6):
                frame = receive()
                if frame.get("type") == "execute/initialResponse" and frame.get("id") == 2:
                    result = frame["result"]["value"]["Result"]
                    break
            self.assertIsNotNone(result)
            self.assertIsNone(result["error_text"])
            value = json.loads(result["content_items"][0]["text"])
            self.assertEqual(value["positive"], 5)
            self.assertTrue(all(v == "undefined" for v in value["globals"].values()))
            for module in ("node:fs", "node:net", "file:///state/codex/auth.json"):
                self.assertEqual(value[module], "unsupported import in exec")
            self.assertEqual(value["toolKeys"], [])
            self.assertEqual(value["toolCount"], 0)
        finally:
            process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

    def test_actual_cli_version_is_exact_and_stable_across_temporary_homes(self):
        versions = []
        for _ in range(2):
            done = self.invoke("--dry-run")
            self.assertEqual(done.returncode, 0, done.stderr)
            versions.append(json.loads(done.stdout)["cli_version"])
        self.assertEqual(versions, ["codex-cli 0.162.1", "codex-cli 0.162.1"],
                         "ephemeral helper warnings polluted stable CLI identity")

    def test_actual_preflight_accepts_real_network_denial_and_cleans_fixture(self):
        before = sorted(p.relative_to(self.fixture).as_posix() for p in self.fixture.rglob("*"))
        done = self.invoke("--preflight")
        self.assertEqual(done.returncode, 0, done.stderr)
        after = sorted(p.relative_to(self.fixture).as_posix() for p in self.fixture.rglob("*"))
        self.assertEqual(after, before, "preflight left a model-visible canary artifact")

    def test_judge_dryrun_profile_and_schema_use_native_paths(self):
        done = self.invoke("--dry-run", judge=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        body = json.loads(done.stdout)
        config = tomllib.loads(body["config"])
        profile = config["permissions"]["judge"]
        self.assertEqual(profile["extends"], ":read-only")
        self.assertEqual(profile["filesystem"][":workspace_roots"]["."], "read")
        for value in profile.get("workspace_roots", {}).values():
            self.assertIsInstance(value, bool)
        self.assertFalse(profile["network"]["enabled"])
        for feature in ("shell_tool", "unified_exec"):
            self.assertIs(config["features"][feature], False)
        index = body["argv"].index("--output-schema")
        self.assertEqual(body["argv"][index + 1], "/state/codex/output-schema.json")

    def test_actual_judge_preflight_has_valid_readonly_native_profile(self):
        done = self.invoke("--preflight", judge=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(list(self.fixture.iterdir()), [])

    def test_proxy_origin_only_rejects_credentials_path_query_and_fragment(self):
        for proxy in (
            "http://user:PROXY_REVIEW_CANARY_NEVER_PRINT@proxy.invalid:3128",
            "http://proxy.invalid:3128/?auth=PROXY_REVIEW_CANARY_NEVER_PRINT",
            "http://proxy.invalid:3128/%74%6f%6b%65%6e/PROXY_REVIEW_CANARY_NEVER_PRINT",
            "http://proxy.invalid:3128/path",
            "http://proxy.invalid:3128/#PROXY_REVIEW_CANARY_NEVER_PRINT",
        ):
            with self.subTest(proxy_kind=proxy.split("proxy.invalid")[-1].split("CANARY")[0]):
                done = self.invoke("--dry-run", proxy=proxy)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("proxy", done.stderr.lower(), "unsupported proxy not rejected at proxy gate")

    def test_proxy_plain_origin_dryrun_is_supported_and_redacted(self):
        proxy = "http://proxy.invalid:3128/"
        done = self.invoke("--dry-run", proxy=proxy)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(proxy, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
