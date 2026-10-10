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
