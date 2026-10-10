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
