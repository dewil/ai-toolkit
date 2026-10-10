#!/usr/bin/env python3
"""Blind native-wrapper CLI refusal contracts; fake executables, synthetic auth.

Real OS canary acceptance is reviewed separately; these do not establish isolation.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/codex-sandbox.py"


class EvalWrapperCLI(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.fixture = self.base / "fixture"
        self.fixture.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.auth = self.base / "approved-synthetic-auth.json"
        self.auth.write_text('{"synthetic_canary":"SYNTHETIC_AUTH_CONTENT"}', encoding="utf-8")
        self.auth.chmod(0o600)
        self.log = self.base / "model-called"
        self.env = dict(PATH=str(self.bin), HOME=str(self.base / "empty-home"),
                        LANG="C.UTF-8", PARENT_CANARY_SECRET="SYNTHETIC_PARENT_VALUE")

    def executable(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def invoke(self, *flags, root=None):
        original = self.auth.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), "--mode", "eval", "--root",
            str(root or self.fixture), "--model", "synthetic-model", "--auth-file", str(self.auth), *flags],
            env=self.env, input="synthetic prompt", capture_output=True, text=True, timeout=20)
        self.assertEqual(self.auth.read_bytes(), original)
        self.assertFalse(self.log.exists(), "live/model invocation in offline test")
        for secret in ("SYNTHETIC_AUTH_CONTENT", "SYNTHETIC_PARENT_VALUE"):
            self.assertNotIn(secret, result.stdout + result.stderr)
        return result

    def test_missing_bubblewrap_fails_before_model_for_dryrun_and_preflight(self):
        self.executable("codex", "raise SystemExit(99)\n")
        for mode in ("--dry-run", "--preflight"):
            with self.subTest(mode=mode):
                done = self.invoke(mode)
                self.assertNotEqual(done.returncode, 0)
                self.assertRegex(done.stderr.lower(), r"bubblewrap|bwrap")
                self.assertNotIn("invalid choice", done.stderr)

    def test_failed_preflight_never_runs_model(self):
        self.executable("bwrap", "raise SystemExit(97)\n")
        self.executable("codex", "import pathlib,sys\n"
            "if '--version' in sys.argv: print('codex-cli 0.162.1'); raise SystemExit(0)\n"
            "if '--help' in sys.argv: print('--no-daemon --ask-for-approval --strict-config --ignore-rules --ephemeral --skip-git-repo-check --json'); raise SystemExit(0)\n"
            f"pathlib.Path({str(self.log)!r}).write_text('CALLED')\nraise SystemExit(98)\n")
        done = self.invoke("--preflight")
        self.assertNotEqual(done.returncode, 0)
        self.assertRegex(done.stderr.lower(), r"bwrap|namespace|preflight|runtime|native|bubblewrap")

    def test_mode_eval_rejects_forwarded_overrides(self):
        for forwarded in (("--", "--dangerously-bypass-approvals-and-sandbox"),
                          ("--", "-c", 'permissions.eval.network.enabled=true'),
                          ("--", "--search")):
            with self.subTest(forwarded=forwarded):
                done = self.invoke("--dry-run", *forwarded)
                self.assertNotEqual(done.returncode, 0)

    def test_symlink_fixture_is_refused(self):
        link = self.base / "fixture-alias"
        link.symlink_to(self.fixture, target_is_directory=True)
        done = self.invoke("--dry-run", root=link)
        self.assertNotEqual(done.returncode, 0)
        self.assertRegex(done.stderr.lower(), r"symlink|символ|ссылк")


if __name__ == "__main__":
    unittest.main(verbosity=2)
