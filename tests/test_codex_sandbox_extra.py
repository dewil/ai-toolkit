"""Additional checks for Codex sandbox preflight and environment isolation."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex-sandbox.py"


class SandboxExtra(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-sandbox-extra-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.codex_home = self.base / "codex-home"
        self.codex_home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.codex = self.bin / "codex"
        self.codex.write_text(
            "#!/usr/bin/python3\nimport json, os\nprint(json.dumps(dict(os.environ)))\n"
        )
        self.codex.chmod(0o755)
        if shutil.which("bwrap") is None:
            fake_bwrap = self.bin / "bwrap"
            fake_bwrap.write_text("#!/usr/bin/python3\nraise SystemExit(0)\n")
            fake_bwrap.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.codex_home),
                        PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                        TMPDIR=str(self.base), MY_API_TOKEN="must-not-leak",
                        AWS_SECRET_ACCESS_KEY="also-must-not-leak")

    def run_sandbox(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              env=self.env, capture_output=True, text=True, check=False)

    def test_rejects_home_ancestors_and_codex_home_descendants(self):
        child = self.codex_home / "sessions"
        child.mkdir()
        for root in (Path("/"), self.base, self.home, self.codex_home, child):
            with self.subTest(root=root):
                result = self.run_sandbox("audit", "--root", root, "--dry-run", "--")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("HOME or CODEX_HOME", result.stderr)

    def test_rejects_directory_with_nested_repository(self):
        (self.root / "other" / ".git").mkdir(parents=True)
        result = self.run_sandbox("audit", "--root", self.root, "--dry-run", "--")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("содержит другой проект", result.stderr)
        # git-файл (worktree, сабмодуль) проектом не считается
        (self.root / "other" / ".git").rmdir()
        (self.root / "other" / ".git").write_text("gitdir: /nowhere\n")
        result = self.run_sandbox("audit", "--root", self.root, "--dry-run", "--")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_secret_symlinks_before_launch(self):
        target = self.root / "plain"
        target.write_text("placeholder")
        for name, target_path, is_dir in ((".env", target, False),
                                          ("secrets", self.home, True),
                                          ("secrets", self.root / "missing", True)):
            link = self.root / name
            link.symlink_to(target_path, target_is_directory=is_dir)
            with self.subTest(name=name):
                result = self.run_sandbox("audit", "--root", self.root, "--dry-run", "--")
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"секретное имя - симлинк: {link}; удали или замени файлом",
                              result.stderr)
            link.unlink()

    def test_secret_environment_variable_is_absent_inside_bwrap(self):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            self.skipTest("bubblewrap is not installed")
        probe = subprocess.run([bwrap, "--ro-bind", "/", "/", "true"],
                               capture_output=True, check=False)
        if probe.returncode:
            self.skipTest("bubblewrap cannot create a namespace")
        result = self.run_sandbox("audit", "--root", self.root, "--")
        self.assertEqual(result.returncode, 0, result.stderr)
        inside = json.loads(result.stdout)
        self.assertNotIn("MY_API_TOKEN", inside)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", inside)
        self.assertNotIn("TMPDIR", inside)
        self.assertEqual(inside["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(inside["HOME"], str(self.home))
        self.assertIn("PATH", inside)

    def test_git_config_empty_source_is_removed(self):
        repo = self.base / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README").write_text("ready\n")
        subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "init"],
                       check=True)
        worktree = self.base / "worktree"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", "sandbox-extra",
                        str(worktree)], check=True)
        common = repo / ".git"

        dry = self.run_sandbox("implement", "--worktree", worktree, "--dry-run", "--")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        tokens = [shlex.split(line)[0] for line in dry.stdout.splitlines()]
        config_index = tokens.index(str(common / "config"))
        self.assertEqual(tokens[config_index - 1], "/dev/null")
        self.assertFalse(list(self.base.glob("codex-sandbox-empty-*")))

        # This bwrap stub records the real config source without entering a namespace.
        capture = self.base / "bwrap-args.json"
        fake_bwrap = self.bin / "bwrap"
        fake_bwrap.write_text(
            "#!/usr/bin/python3\nimport json, os, sys\n"
            "open(os.environ['BWRAP_CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))\n"
        )
        fake_bwrap.chmod(0o755)
        self.env["BWRAP_CAPTURE"] = str(capture)
        live = self.run_sandbox("implement", "--worktree", worktree, "--")
        self.assertEqual(live.returncode, 0, live.stderr)
        args = json.loads(capture.read_text())
        source = Path(args[args.index(str(common / "config")) - 1])
        self.assertTrue(source.name.startswith("codex-sandbox-empty-"))
        self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
