#!/usr/bin/env python3
"""Контракт gitleaks-hook (INV-DATA-01..03), без импорта реализации.

Запуск: python3 tests/test_gitleaks_hook.py
Настоящий git, временные репозитории, gitleaks-заглушка; сети нет.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gitleaks-hook.py"
START = "# >>> canon gitleaks >>>"
END = "# <<< canon gitleaks <<<"
GIT = shutil.which("git")


class HookTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(GIT, "Для тестов нужен настоящий git")
        self.tmp = tempfile.TemporaryDirectory(prefix="gitleaks tests ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Только локальные утилиты: нет ни настоящего gitleaks, ни загрузчиков.
        for name in ("git", "sh", "bash", "env", "dirname", "basename", "cat",
                     "sed", "awk", "grep", "cut", "tr", "head", "tail", "sort",
                     "uname", "printf", "readlink", "wc", "expr", "test"):
            executable = shutil.which(name)
            if executable:
                (self.bin / name).symlink_to(executable)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("GIT_") and k not in ("SKIP_GITLEAKS", "ENV", "BASH_ENV")}
        self.env.update(PATH=str(self.bin), HOME=str(self.root),
                        XDG_CONFIG_HOME=str(self.root / "config"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8")
        self.log = self.root / "gitleaks.jsonl"
        self.repo = self.new_repo("project")

    def git(self, repo, *args, check=True, env=None):
        return subprocess.run(
            [GIT, "-C", str(repo), *args], env=env or self.env,
            capture_output=True, text=True, check=check, timeout=20,
        )

    def new_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q", "--template=")
        self.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "initial")
        return repo

    def cli(self, command, *repos, cwd=None, script=SCRIPT):
        self.assertTrue(script.is_file(), f"Нет реализации: {script}")
        args = [sys.executable, str(script), command]
        for repo in repos:
            args.extend(["--repo", str(repo)])
        return subprocess.run(args, cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def install(self, *repos, **kwargs):
        result = self.cli("install", *repos, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.hook(repos[0] if repos else self.repo)

    def hook(self, repo):
        path = Path(self.git(repo, "rev-parse", "--git-path", "hooks").stdout.strip())
        return (path if path.is_absolute() else repo / path) / "pre-commit"

    def write_hook(self, body):
        hook = self.hook(self.repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(body)
        hook.chmod(0o644)
        return hook

    def stub(self, version="8.30.1", code=0):
        stub = self.bin / "gitleaks"
        stub.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"with open({str(self.log)!r}, 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:] == ['version']:\n"
            f"    print({version!r})\n"
            "    sys.exit(0)\n"
            f"sys.exit({code})\n", encoding="utf-8")
        stub.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def commit(self, *, skip=False):
        path = self.repo / "staged.txt"
        path.write_text(path.read_text() + "change\n" if path.exists() else "change\n")
        self.git(self.repo, "add", "staged.txt")
        env = dict(self.env)
        if skip:
            env["SKIP_GITLEAKS"] = "1"
        return self.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-m", "checked", check=False, env=env)

    def assert_status(self, result, code, status):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertRegex(result.stdout, rf"хук:\s*{re.escape(status)}(?=\s|$)")

    def test_install_creates_executable_self_contained_idempotent_hook(self):
        hook = self.install(self.repo)
        original = hook.read_bytes()
        self.assertRegex(original.decode(), r"\A#![^\n]*\b(?:sh|bash)\b")
        self.assertEqual(original.count(START.encode()), 1)
        self.assertEqual(original.count(END.encode()), 1)
        self.assertLess(original.index(START.encode()), original.index(END.encode()))
        self.assertNotIn(b"gitleaks-hook.py", original)
        self.assertNotIn(str(ROOT).encode(), original)
        self.assertTrue(os.access(hook, os.X_OK))
        self.install(self.repo)
        self.assertEqual(hook.read_bytes(), original)

    def test_default_repo_is_git_root_from_subdirectory(self):
        subdir = self.repo / "nested"
        subdir.mkdir()
        hook = self.install(cwd=subdir)
        self.assertTrue(hook.is_file())
        self.stub()
        self.assert_status(self.cli("status", cwd=subdir), 0, "стоит")

    def test_foreign_shell_content_preserved_and_block_after_shebang(self):
        for shebang in (b"#!/bin/sh", b"#!/bin/bash", b"#!/usr/bin/env bash"):
            with self.subTest(shebang=shebang):
                original = shebang + b"\n# foreign hook\nprintf 'foreign hook ran\\n' >&2\n"
                hook = self.write_hook(original)
                self.install(self.repo)
                body = hook.read_bytes()
                # Вставка в конец пропускала сканер, если чужой хук завершался exit 0.
                self.assertTrue(body.startswith(shebang + b"\n" + START.encode() + b"\n"))
                self.assertEqual(body[body.index(END.encode()) + len(END):], original[len(shebang):])
                self.assertEqual(body.count(START.encode()), 1)
                self.assertTrue(os.access(hook, os.X_OK))
                self.stub()
                result = self.commit()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("foreign hook ran", result.stderr)

    def test_foreign_exit_zero_cannot_bypass_gitleaks_finding(self):
        original = b"#!/bin/sh\nset -e\nprintf 'foreign hook ran\\n' >&2\nexit 0\n"
        hook = self.write_hook(original)
        self.stub(code=1)
        before = self.git(self.repo, "rev-parse", "HEAD").stdout
        self.install(self.repo)
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").stdout, before)
        self.assertNotIn("foreign hook ran", result.stderr)
        self.assertIn(".gitleaksignore", result.stderr)
        self.assertTrue(hook.read_bytes().endswith(original.split(b"\n", 1)[1]))

    def test_previously_appended_block_is_reported_stale_and_moved(self):
        hook = self.install(self.repo)
        block = hook.read_bytes().split(b"\n", 1)[1].rstrip(b"\n")
        foreign = b"#!/bin/sh\nset -e\nexit 0\n"
        hook.write_bytes(foreign + block + b"\n")
        self.stub(code=1)
        self.assert_status(self.cli("status", self.repo), 1, "устарел")
        self.install(self.repo)
        self.assertTrue(hook.read_bytes().startswith(b"#!/bin/sh\n" + START.encode()))
        self.assertEqual(hook.read_bytes().count(START.encode()), 1)
        self.assertNotEqual(self.commit().returncode, 0)

    def test_stale_block_replaced_without_changing_surroundings(self):
        hook = self.install(self.repo)
        fresh = hook.read_text()
        block = fresh[fresh.index(START):fresh.index(END) + len(END)]
        prefix = "#!/bin/sh\n# foreign prefix\nprintf 'before\\n' >&2\n"
        suffix = "\n# foreign suffix\nprintf 'after\\n' >&2\n"
        hook.write_text(prefix + START + "\n# obsolete\nfalse\n" + END + suffix)
        hook.chmod(0o644)
        self.install(self.repo)
        self.assertEqual(hook.read_bytes(), (prefix + block + suffix).encode())
        self.assertTrue(os.access(hook, os.X_OK))

    def test_foreign_non_shell_untouched_with_manual_command(self):
        for original in (b"#!/usr/bin/env python3\nprint('foreign')\n", b"\x7fELF\x00\xff", b"echo no-shebang\n"):
            with self.subTest(original=original):
                hook = self.write_hook(original)
                mode = hook.stat().st_mode
                result = self.cli("install", self.repo)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertEqual(hook.read_bytes(), original)
                self.assertEqual(hook.stat().st_mode, mode)
                output = result.stdout + result.stderr
                self.assertIn("чужой pre-commit не shell: добавь вызов вручную", output)
                self.assertRegex(output, r"gitleaks\s+(?:git\s+--pre-commit|protect)\s+--staged\s+--redact")

    def test_core_hooks_path_relative_and_absolute(self):
        for path in ("custom hooks", str(self.root / "absolute hooks")):
            with self.subTest(path=path):
                self.git(self.repo, "config", "core.hooksPath", path)
                hook = self.install(self.repo)
                self.assertTrue(hook.is_file())
                self.assertTrue(os.access(hook, os.X_OK))
                self.stub(code=1)
                self.assertNotEqual(self.commit().returncode, 0)
        self.assertFalse((self.repo / ".git/hooks/pre-commit").exists())

    def test_linked_worktree_uses_git_hooks_location(self):
        worktree = self.root / "linked worktree"
        self.git(self.repo, "worktree", "add", "-q", "-b", "linked", str(worktree))
        self.assertTrue((worktree / ".git").is_file())
        hook = self.install(worktree)
        self.assertTrue(hook.is_file())
        self.repo = worktree
        self.stub(code=1)
        self.assertNotEqual(self.commit().returncode, 0)
        self.assert_status(self.cli("status", worktree), 0, "стоит")

    def test_install_multiple_repositories(self):
        other = self.new_repo("other repo")
        self.install(self.repo, other)
        for repo in (self.repo, other):
            self.assertTrue(os.access(self.hook(repo), os.X_OK))

    def test_missing_binary_warns_loudly_but_commit_succeeds(self):
        self.install(self.repo)
        self.assertIsNone(shutil.which("gitleaks", path=self.env["PATH"]))
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gitleaks", result.stderr.lower())
        self.assertGreaterEqual(len([line for line in result.stderr.splitlines() if line.strip()]), 2)
        self.assertRegex(result.stderr.lower(), r"warn|warning|внимани|предупрежден")
        # Команду только показывают, сетевые загрузчики в PATH отсутствуют.
        self.assertRegex(result.stderr, r"brew install gitleaks|winget install gitleaks|(?:curl|wget)[^\n]*github\.com")

    def test_finding_blocks_commit_with_actionable_hints(self):
        self.stub(code=1)
        self.install(self.repo)
        before = self.git(self.repo, "rev-parse", "HEAD").stdout
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").stdout, before)
        self.assertIn(".gitleaksignore", result.stderr)
        self.assertRegex(result.stderr.lower(), r"комментар|comment")
        self.assertIn("SKIP_GITLEAKS=1 git commit", result.stderr)

    def test_skip_bypasses_scanner_and_reports_to_stderr(self):
        self.stub(code=1)
        self.install(self.repo)
        self.log.unlink(missing_ok=True)
        result = self.commit(skip=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gitleaks пропущен (SKIP_GITLEAKS=1)", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_version_selects_staged_redacted_command(self):
        self.install(self.repo)
        for version, expected in (
            ("8.30.1", ["git", "--pre-commit", "--staged", "--redact"]),
            ("8.19.0", ["git", "--pre-commit", "--staged", "--redact"]),
            ("8.18.0", ["protect", "--staged", "--redact"]),
        ):
            with self.subTest(version=version):
                self.stub(version)
                self.log.unlink(missing_ok=True)
                result = self.commit()
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.calls()
                self.assertIn(["version"], calls)
                self.assertEqual([args for args in calls if args != ["version"]], [expected])

    def test_hook_survives_removal_of_installer(self):
        self.assertTrue(SCRIPT.is_file(), f"Нет реализации: {SCRIPT}")
        copy = self.root / "installer.py"
        shutil.copyfile(SCRIPT, copy)
        self.install(self.repo, script=copy)
        copy.unlink()
        self.stub(code=1)
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(["git", "--pre-commit", "--staged", "--redact"], self.calls())
        self.assertIn(".gitleaksignore", result.stderr)

    def test_status_ready(self):
        self.stub()
        self.install(self.repo)
        result = self.cli("status", self.repo)
        self.assert_status(result, 0, "стоит")
        self.assertRegex(result.stdout, r"gitleaks:\s*[^\n]*8\.30\.1")

    def test_status_missing_hook_does_not_install(self):
        self.stub()
        self.assert_status(self.cli("status", self.repo), 1, "нет")
        self.assertFalse(self.hook(self.repo).exists())

    def test_status_missing_binary_with_installed_hook(self):
        self.install(self.repo)
        result = self.cli("status", self.repo)
        self.assert_status(result, 1, "стоит")
        self.assertRegex(result.stdout, r"gitleaks:\s*нет")

    def test_status_foreign_and_stale_hooks_are_read_only(self):
        self.stub()
        for body, status in (
            ("#!/usr/bin/env python3\npass\n", "чужой-не-shell"),
            ("#!/bin/sh\n# foreign shell\n", "нет"),
            (f"#!/bin/sh\n{START}\n# old version\n{END}\n", "устарел"),
        ):
            with self.subTest(status=status):
                hook = self.write_hook(body.encode())
                self.assert_status(self.cli("status", self.repo), 1, status)
                self.assertEqual(hook.read_bytes(), body.encode())
                self.assertEqual(hook.stat().st_mode & 0o777, 0o644)

    def test_status_multiple_repositories_reports_each_and_aggregates(self):
        self.stub()
        other = self.new_repo("other repo")
        self.install(self.repo)
        result = self.cli("status", self.repo, other)
        self.assert_status(result, 1, "стоит")
        self.assert_status(result, 1, "нет")
        for repo in (self.repo, other):
            self.assertIn(str(repo), result.stdout)
        self.install(other)
        result = self.cli("status", self.repo, other)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(re.findall(r"хук:\s*стоит", result.stdout)), 2)

    def test_status_non_repository_is_error(self):
        self.stub()
        outside = self.root / "not a repo"
        outside.mkdir()
        result = self.cli("status", outside)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue((result.stdout + result.stderr).strip())


class DocumentationTest(unittest.TestCase):
    """Критерий 9: проверяем тексты в нужных разделах, не исполняем команды."""

    def section(self, text, heading):
        match = re.search(rf"(?mi)^##\s+{heading}[^\n]*\n", text)
        self.assertIsNotNone(match, f"Нет раздела {heading}")
        end = re.search(r"(?m)^##\s+", text[match.end():])
        return text[match.start():match.end() + end.start() if end else len(text)]

    def test_secrets_rule_documents_installation_and_limits(self):
        text = (ROOT / "rules/secrets-handling.md").read_text(encoding="utf-8")
        section = self.section(text, "Проверка до коммита")
        for literal in ("scripts/gitleaks-hook.py install", "brew install gitleaks",
                        "winget install gitleaks", "~/.local/bin", "gitleaks version", ".gitleaksignore"):
            with self.subTest(literal=literal):
                self.assertIn(literal, section)
        self.assertRegex(section, r"(?is)(?:curl|wget).*github\.com/.*releases/")
        self.assertRegex(section.lower(), r"кажд\w*\s+(?:git[- ]?)?репозитор")
        self.assertRegex(section.lower(), r"комментар")
        self.assertRegex(section.lower(), r"истори[\s\S]{0,180}(?:не провер|не скан)|(?:не провер|не скан)[\s\S]{0,180}истори")

    def test_canon_checks_in_step_one_and_lists_missing_items_in_plan(self):
        text = (ROOT / "migrations/ai-sync.prompt.md").read_text(encoding="utf-8")
        audit = self.section(text, r"1\. Аудит")
        plan = self.section(text, r"3\. Read-only план")
        self.assertIn("gitleaks-hook.py status", audit)
        self.assertRegex(audit.lower(), r"git[- ]кор|кор[\w-]*\s+проект")
        self.assertRegex(audit.lower(), r"зонтик|зонтич|подключенн|подключённ")
        self.assertRegex(plan.lower(), r"gitleaks[^\n]*(?:нет|не хватает|отсутств|не установлен)|(?:нет|не хватает|отсутств|не установлен)[^\n]*gitleaks")

    def test_canon_install_requires_ok_and_missing_binary_does_not_stop_sync(self):
        text = (ROOT / "migrations/ai-sync.prompt.md").read_text(encoding="utf-8")
        action = self.section(text, r"4\. Применение")
        self.assertIn("gitleaks-hook.py install", action)
        # Ограничения должны быть привязаны к gitleaks, а не к другому действию.
        paragraphs = [p for p in re.split(r"\n\s*\n", text) if "gitleaks" in p.lower()]
        local = "\n".join(paragraphs).lower()
        self.assertRegex(local, r"только[^\n]{0,100}после[^\n]{0,60}[\"«“]?ок")
        self.assertRegex(local, r"не (?:останавлива|блокиру|прерыва)")
        self.assertRegex(local, r"(?:бинарник|агент)[^\n]{0,100}не (?:став|устанавл)")
        self.assertRegex(local, r"команд[\w]*\s+(?:установки|для установки)|(?:дай|дает|даёт|покажи|выведи)[^\n]*команд")

    def test_publication_hook_does_not_replace_history_audit(self):
        text = (ROOT / "skills/repo-publication/SKILL.md").read_text(encoding="utf-8").lower()
        self.assertRegex(text, r"(?:хук|pre-commit)[^.\n]{0,180}не заменяет[^.\n]{0,180}(?:аудит|проверк)[^.\n]{0,80}истори")


if __name__ == "__main__":
    unittest.main(verbosity=2)
