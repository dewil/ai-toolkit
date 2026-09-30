#!/usr/bin/env python3
"""Черные ящики по spec-codex-sandbox (2026-09-30), без настоящего codex/сети.

Запуск: python3 tests/test_codex_sandbox.py или pytest tests/test_codex_sandbox.py.
Dry-run/отказы используют fake bwrap; только LiveSandbox требует рабочий bwrap.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "codex-sandbox.py"
GIT = shutil.which("git")
BWRAP = shutil.which("bwrap")

# Только probe разрешен: dry-run не должен запускать итоговую команду.
FAKE_BWRAP = '''import json, os, sys
with open(os.environ["BWRAP_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--ro-bind", "/", "/", "true"]:
    sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
sys.exit(97)
'''

FAKE_CODEX = '''import json, os, pathlib, subprocess, sys
home = pathlib.Path(os.environ["CODEX_HOME"])
(home / "called").write_text("yes")
result = {"argv": sys.argv[1:], "stdin": sys.stdin.read(),
          "proxy": {k: os.environ.get(k) for k in
                    ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}}
if (home / "checks.json").exists():
    checks = json.loads((home / "checks.json").read_text())
    root = pathlib.Path(checks["root"])
    result["inside"] = (root / "visible.txt").read_text()
    result["outside"] = [pathlib.Path(p).exists() for p in checks["outside"]]
    try:
        (root / "written.txt").write_text("written")
        result["write"] = True
    except OSError:
        result["write"] = False
    if checks.get("git"):
        git = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             text=True, capture_output=True)
        result["git_code"] = git.returncode
        result["head"] = git.stdout.strip()
        result["config"] = pathlib.Path(checks["config"]).read_text()
(home / "result.json").write_text(json.dumps(result))
print(json.dumps(result))
sys.exit(int((home / "exit-code").read_text()))
'''


class SandboxFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-sandbox-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "project with spaces"
        self.home = self.base / "home"
        self.codex_home = self.home / ".codex"
        self.bin = self.codex_home / "bin"
        self.root.mkdir()
        self.bin.mkdir(parents=True)
        self.env = {"PATH": str(self.bin), "HOME": str(self.home),
                    "CODEX_HOME": str(self.codex_home), "LC_ALL": "C.UTF-8",
                    "BWRAP_LOG": str(self.base / "bwrap.jsonl"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        for name, target in (("git", GIT), ("python3", sys.executable),
                             ("true", shutil.which("true"))):
            if target:
                (self.bin / name).symlink_to(target)
        self.executable("bwrap", FAKE_BWRAP)
        self.executable("codex", FAKE_CODEX)
        (self.codex_home / "exit-code").write_text("0")

    def executable(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    def invoke(self, mode="audit", target=None, args=(), dry=False, stdin="", python_args=()):
        self.assertTrue(SCRIPT.is_file(), f"Отсутствует реализация: {SCRIPT}")
        target = target or self.root
        command = [sys.executable, *python_args, str(SCRIPT), mode,
                   "--root" if mode == "audit" else "--worktree", str(target)]
        if dry:
            command.append("--dry-run")
        return subprocess.run(command + ["--", *args], env=self.env, cwd=self.base,
                              input=stdin, text=True, capture_output=True, timeout=20)

    def git(self, *args, root=None):
        self.assertIsNotNone(GIT, "Для git-fixture требуется git")
        return subprocess.run([GIT, "-C", str(root or self.root), *args], env=self.env,
                              text=True, capture_output=True, check=True, timeout=10).stdout.strip()

    def worktree(self):
        self.git("init", "-q")
        (self.root / "visible.txt").write_text("visible")
        self.git("add", "visible.txt")
        self.git("commit", "-qm", "fixture")
        worktree = self.base / "worktree with spaces"
        self.git("worktree", "add", "-qb", "fixture", str(worktree))
        return worktree, self.root / ".git"

    def no_codex(self):
        self.assertFalse((self.codex_home / "called").exists(), "codex запускался")

    def dry_command(self, **kwargs):
        result = self.invoke(dry=True, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.no_codex()
        log = Path(self.env["BWRAP_LOG"])
        if log.exists():
            for line in log.read_text().splitlines():
                self.assertEqual(json.loads(line), ["--ro-bind", "/", "/", "true"],
                                 "dry-run запустил не только проверку bwrap")
        lines = result.stdout.strip().splitlines()
        self.assertGreater(len(lines), 1, "Команда dry-run должна быть построчной")
        # Допускаем argv по одному элементу на строку и shell-quoted строки опций.
        if "--ro-bind" in lines:
            tokens = [shlex.split(s)[0] if s.startswith(("'", '"')) else s for s in lines]
        else:
            tokens = shlex.split(result.stdout.replace("\\\n", "\n"))
        self.assertEqual(Path(tokens[0]).name, "bwrap")
        return tokens, result.stderr

    def assert_sequence(self, tokens, *expected):
        self.assertTrue(any(tokens[i:i + len(expected)] == list(expected)
                            for i in range(len(tokens))), f"Нет {expected!r} в {tokens!r}")

    def mounts(self, tokens):
        return [(tokens[i], tokens[i + 1], tokens[i + 2])
                for i in range(len(tokens) - 2)
                if tokens[i] in ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try",
                                 "--dev-bind", "--dev-bind-try")]

    def rejected(self, **kwargs):
        result = self.invoke(**kwargs)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertTrue(result.stderr.strip())
        self.no_codex()
        return result.stderr.lower()


class DryRunSandbox(SandboxFixture):
    def test_audit_mounts_only_allowlisted_paths(self):
        tokens, _ = self.dry_command()
        self.assert_sequence(tokens, "--ro-bind", str(self.root), str(self.root))
        self.assert_sequence(tokens, "--bind", str(self.codex_home), str(self.codex_home))
        self.assert_sequence(tokens, "--tmpfs", str(self.home))
        self.assert_sequence(tokens, "--tmpfs", "/tmp")
        self.assert_sequence(tokens, "--proc", "/proc")
        self.assert_sequence(tokens, "--dev", "/dev")
        self.assert_sequence(tokens, "-C", str(self.root))
        self.assertIn("--share-net", tokens)
        self.assertIn("exec", tokens)
        allowed = {"/usr", "/bin", "/sbin", "/lib", "/lib64", "/dev/null",
                   "/etc/ssl", "/etc/ca-certificates", "/etc/resolv.conf", "/etc/hosts",
                   "/etc/passwd", "/etc/localtime", str(self.root), str(self.codex_home),
                   str(self.bin), str(self.bin / "codex")}
        for option, source, destination in self.mounts(tokens):
            self.assertIn(source, allowed, f"Лишний mount: {option} {source} {destination}")
            if source != "/dev/null":
                self.assertEqual(destination, source, "Mount меняет разрешенную область видимости")
            if source in {"/usr", "/bin", "/sbin", "/lib", "/lib64"} or source.startswith("/etc/"):
                self.assertIn(option, ("--ro-bind", "--ro-bind-try"))
        self.assertNotIn(("--bind", str(self.root), str(self.root)), self.mounts(tokens))

    def test_implement_worktree_and_common_git_config(self):
        worktree, common = self.worktree()
        tokens, _ = self.dry_command(mode="implement", target=worktree)
        self.assert_sequence(tokens, "--bind", str(worktree), str(worktree))
        self.assert_sequence(tokens, "--ro-bind", str(common), str(common))
        self.assert_sequence(tokens, "-C", str(worktree))
        mounts = self.mounts(tokens)
        config_mounts = [mount for mount in mounts if mount[2] == str(common / "config")]
        self.assertEqual(len(config_mounts), 1, "Общий git config должен быть перекрыт")
        config_mount = config_mounts[0]
        self.assertEqual(config_mount[0], "--ro-bind")
        self.assertEqual(Path(config_mount[1]).read_bytes(), b"", "Вместо config нужен пустой файл")
        self.assertLess(mounts.index(("--ro-bind", str(common), str(common))),
                        mounts.index(config_mount))
        self.assertFalse(any(source == str(self.root) for _, source, _ in mounts))

    def test_secret_patterns_skipped_directories_and_summary_without_reads(self):
        names = [".env", ".env.local", ".env.production", "tls.pem", "tls.key", "tls.p12",
                 "tls.pfx", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub",
                 "id_ecdsa", "id_ecdsa.pub", ".netrc", ".npmrc", ".pypirc", ".git-credentials"]
        masked = []
        for prefix in (Path(), Path("nested dir")):
            for name in names:
                path = self.root / prefix / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("SECRET_CONTENT_MUST_NOT_BE_READ")
                masked.append(path)
        secret_dir = self.root / "secrets"
        secret_dir.mkdir()
        (secret_dir / "payload.txt").write_text("secret")
        untouched = [self.root / ".env.example", self.root / "nested dir" / ".env.example",
                     self.root / "ordinary.txt"]
        for directory in (".git", "node_modules", ".venv", "venv"):
            untouched.append(self.root / directory / ".env")
        for path in untouched:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public-or-skipped")
        # Audit hook наблюдает только открытие секретов в тестируемом процессе,
        # не импортирует его функции и не зависит от устройства реализации.
        guard = '''import os, runpy, sys
sys.argv = sys.argv[1:]
def guard(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.path.realpath(os.fsdecode(args[0]))
        if path in forbidden:
            raise AssertionError("Secret content opened: " + path)
forbidden = set(__import__("json").loads(os.environ["FORBIDDEN_READS"]))
sys.addaudithook(guard)
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        self.env["FORBIDDEN_READS"] = json.dumps([str(p) for p in masked])
        tokens, stderr = self.dry_command(python_args=("-c", guard))
        mounts = self.mounts(tokens)
        for path in masked:
            self.assertIn(("--ro-bind", "/dev/null", str(path)), mounts)
            self.assertLess(mounts.index(("--ro-bind", str(self.root), str(self.root))),
                            mounts.index(("--ro-bind", "/dev/null", str(path))))
            self.assertIn(str(path), stderr)
        self.assert_sequence(tokens, "--tmpfs", str(secret_dir))
        self.assertIn(str(secret_dir), stderr)
        for path in untouched:
            self.assertFalse(any(destination == str(path) for _, _, destination in mounts))
            self.assertNotIn(str(path), stderr)
        self.assertEqual(len(stderr.strip().splitlines()), 1, stderr)
        self.assertRegex(stderr, rf"\b{len(masked) + 1}\b")
        self.assertNotIn("SECRET_CONTENT_MUST_NOT_BE_READ", stderr)


class Refusals(SandboxFixture):
    def test_missing_bwrap_recommends_bubblewrap(self):
        (self.bin / "bwrap").unlink()
        self.assertIn("bubblewrap", self.rejected())

    def test_failed_namespace_probe_never_falls_back(self):
        self.env["PROBE_EXIT"] = "1"
        self.assertRegex(self.rejected(), r"bwrap|bubblewrap|namespace|пространств")
        calls = [json.loads(s) for s in Path(self.env["BWRAP_LOG"]).read_text().splitlines()]
        self.assertEqual(calls, [["--ro-bind", "/", "/", "true"]])

    def test_missing_object(self):
        self.rejected(target=self.base / "absent")

    def test_implement_rejects_main_checkout(self):
        self.worktree()
        self.rejected(mode="implement")

    def test_implement_rejects_plain_directory(self):
        self.rejected(mode="implement")

    def test_missing_codex(self):
        (self.bin / "codex").unlink()
        self.assertIn("codex", self.rejected())

    def test_missing_codex_home(self):
        self.env["CODEX_HOME"] = str(self.base / "absent-home")
        self.assertRegex(self.rejected(), r"codex|home")

    def test_unset_codex_home(self):
        del self.env["CODEX_HOME"]
        self.assertRegex(self.rejected(), r"codex|home")


class LiveSandbox(SandboxFixture):
    def setUp(self):
        super().setUp()
        if not BWRAP:
            self.skipTest("bwrap отсутствует в PATH")
        try:
            probe = subprocess.run([BWRAP, "--ro-bind", "/", "/", "true"],
                                   capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.skipTest(f"Проба bwrap недоступна: {exc}")
        if probe.returncode:
            self.skipTest(f"bwrap не поднимает пространство имен: {probe.stderr.strip()}")
        (self.bin / "bwrap").unlink()
        (self.bin / "bwrap").symlink_to(BWRAP)

    def result(self):
        self.assertTrue((self.codex_home / "called").exists(), "Заглушка codex не запущена")
        return json.loads((self.codex_home / "result.json").read_text())

    def test_arguments_stdin_exit_code_and_proxy(self):
        args = ["-s", "read-only", "-c", 'key="value with spaces"', "resume", "session-id",
                "", "строка\nс переносом", "$(false); * 'quoted'", "--", "-literal"]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            self.env[key] = "localhost" if key == "NO_PROXY" else "http://127.0.0.1:9"
        for code in (0, 23):
            with self.subTest(code=code):
                (self.codex_home / "exit-code").write_text(str(code))
                result = self.invoke(args=args, stdin="План из stdin\nстрока 2\n")
                self.assertEqual(result.returncode, code, result.stderr)
                observed = self.result()
                argv = observed["argv"]
                self.assertEqual(argv[0], "exec")
                self.assertEqual(argv.count("-C"), 1)
                index = argv.index("-C")
                self.assertEqual(argv[index + 1], str(self.root))
                self.assertEqual(argv[1:index] + argv[index + 2:], args)
                self.assertEqual(observed["stdin"], "План из stdin\nстрока 2\n")
                self.assertEqual(observed["proxy"], {k: self.env[k] for k in observed["proxy"]})

    def check_isolation(self, mode, root, common=None):
        outside = [self.base / "outside-tmp.txt", self.home / "outside-home.txt"]
        for path in outside:
            path.write_text("outside")
        (root / "visible.txt").write_text("visible")
        checks = {"root": str(root), "outside": [str(p) for p in outside]}
        if common:
            checks.update(git=True, config=str(common / "config"))
        (self.codex_home / "checks.json").write_text(json.dumps(checks))
        result = self.invoke(mode=mode, target=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = self.result()
        self.assertEqual(observed["inside"], "visible")
        self.assertEqual(observed["outside"], [False, False])
        self.assertEqual(observed["write"], mode == "implement")
        self.assertEqual((root / "written.txt").exists(), mode == "implement")
        if common:
            self.assertEqual(observed["git_code"], 0)
            self.assertEqual(observed["head"], self.git("rev-parse", "HEAD", root=root))
            self.assertEqual(observed["config"], "")

    def test_audit_visibility_and_read_only(self):
        self.check_isolation("audit", self.root)

    def test_implement_visibility_and_writable_worktree(self):
        worktree, common = self.worktree()
        self.check_isolation("implement", worktree, common)


class SkillTemplates(unittest.TestCase):
    def test_all_launch_templates_use_sandbox(self):
        for skill, mode, flag in (("codex-audit", "audit", "--root"),
                                  ("codex-implement", "implement", "--worktree")):
            with self.subTest(skill=skill):
                text = (REPO / "skills" / skill / "SKILL.md").read_text()
                blocks = re.findall(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", text, re.M | re.S)
                code = "\n".join(blocks).replace("\\\n", " ")
                direct = re.findall(r"[^\n]*\bcodex\s+exec\b[^\n]*", code)
                self.assertEqual(direct, [], "Остался прямой запуск в шаблоне")
                launches = [line for line in code.splitlines()
                            if "codex-sandbox.py" in line and not line.lstrip().startswith("#")]
                self.assertTrue(launches, "Нет шаблонов запуска через codex-sandbox.py")
                for line in launches:
                    self.assertRegex(line, rf"codex-sandbox\.py[\"']?\s+{mode}\b")
                    self.assertIn(flag, line)
                    self.assertRegex(line, r"\s--\s")
                self.assertTrue(any(re.search(r"\bresume\b", line) for line in launches),
                                "Нет шаблона resume через песочницу")
                self.assertTrue(any(not re.search(r"\bresume\b", line) for line in launches),
                                "Нет обычного запуска через песочницу")
                self.assertIn("setsid", text)
                # implement ссылается на общий протокол отвязки audit.
                if mode == "audit":
                    self.assertTrue(any("setsid" in block and "codex-sandbox.py" in block
                                        for block in blocks), "Отвязанный шаблон не использует обертку")
                self.assertIn("bubblewrap", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
