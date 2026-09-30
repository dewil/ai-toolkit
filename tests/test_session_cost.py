"""Q-86: black-box CLI tests for spec.md acceptance criteria 6–11.

Only the documented transcript format, CLI and JSON interface are used.
All transcripts and HOME are temporary; filesystem failures and network are
mocked in the child process. Run with pytest or directly with unittest.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "session-cost.py"
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")

# Invoke the real __main__, mocking only OS boundaries. Patching both open
# variants covers Path.open; scandir/listdir covers directory enumeration.
RUNNER = r'''
import builtins
import contextlib
import io
import os
import runpy
import socket
import sys
from unittest import mock

script, denied, *args = sys.argv[1:]
sys.argv = [script, *args]
def guard(original):
    def call(path, *args, **kwargs):
        if denied and isinstance(path, (str, bytes, os.PathLike)):
            name = os.path.abspath(os.fsdecode(path))
            if name == denied or name.startswith(denied + os.sep):
                raise PermissionError(13, "Permission denied", name)
        return original(path, *args, **kwargs)
    return call
with contextlib.ExitStack() as stack:
    for module, name in ((builtins, "open"), (io, "open"),
                         (os, "scandir"), (os, "listdir")):
        stack.enter_context(mock.patch.object(module, name, guard(getattr(module, name))))
    stack.enter_context(mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
    stack.enter_context(mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")))
    runpy.run_path(script, run_name="__main__")
'''


def assistant(mid="A", tokens=(1, 2, 3, 4), kind="assistant"):
    message = {"role": "assistant"}
    if mid is not None:
        message["id"] = mid
    if tokens is not None:
        message["usage"] = dict(zip(USAGE_KEYS, tokens))
    return {"type": kind, "message": message}


class SessionCostQ86(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(self.project.resolve()))
        self.transcripts = self.home / ".claude" / "projects" / encoded
        self.main = self.transcripts / "session-one.jsonl"
        self.child = self.transcripts / "session-one" / "subagents" / "agent-one.jsonl"
        self.write(self.main, [assistant(), assistant(), assistant("B", (10, 20, 30, 40))])

    def write(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" if not isinstance(r, str)
                                else r + "\n" for r in records), encoding="utf-8")
        return path

    def cli(self, *args, session=None, denied=None, as_json=True):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return subprocess.run(
            [sys.executable, "-c", RUNNER, str(SCRIPT), str(denied or ""),
             *map(str, args), *(["--json"] if as_json else [])],
            cwd=self.project, env=env, capture_output=True, text=True, timeout=15)

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)  # no stripping warnings from stdout

    def totals(self, data, messages, tokens):
        self.assertEqual(data["messages"], messages)
        self.assertEqual(data["tokens"], dict(zip(TOKEN_KEYS, tokens)))
        self.assertEqual(data["work_tokens"], tokens[1] + tokens[3])
        self.assertEqual(data["grand_total"], sum(tokens))

    def files(self, data, expected):
        self.assertCountEqual(data["files"], list(map(str, expected)))

    def diagnostics(self, data, invalid=0, missing=0):
        self.assertEqual(data["invalid_lines"], invalid)
        self.assertEqual(data["messages_without_id"], missing)

    def groups(self, data, main_tokens, sub_tokens, main_messages, sub_messages,
               main_files, sub_files):
        self.totals(data["main"], main_messages, main_tokens)
        self.totals(data["subagents"], sub_messages, sub_tokens)
        self.totals(data, main_messages + sub_messages,
                    tuple(a + b for a, b in zip(main_tokens, sub_tokens)))
        self.files(data["main"], main_files)
        self.files(data["subagents"], sub_files)
        self.files(data, [*main_files, *sub_files])
        for key in ("invalid_lines", "messages_without_id"):
            self.assertEqual(data[key], data["main"][key] + data["subagents"][key])

    def warning(self, stderr, path, number, reason):
        self.assertIn(str(path), stderr)
        self.assertRegex(stderr, rf"\b{number}\b")
        self.assertRegex(stderr.lower(), reason)

    def test_unique_messages_keep_existing_totals(self):
        """Q-86 / 6: regression control for existing fields and formulae."""
        self.write(self.main, [assistant(), assistant("B", (10, 20, 30, 40))])
        result = self.cli("--file", self.main)
        data = self.success(result)
        self.totals(data, 2, (11, 22, 33, 44))
        self.files(data, [self.main])
        self.assertEqual(result.stderr, "")

    def test_blank_lines_and_service_records_keep_existing_totals(self):
        """Q-86 / 10: regression control without usage on service records."""
        self.write(self.main, [assistant(), "", "   ", {"type": "progress"},
                               assistant("unused", None)])
        result = self.cli("--file", self.main)
        self.totals(self.success(result), 1, (1, 2, 3, 4))
        self.assertEqual(result.stderr, "")

    def test_duplicate_id_counted_once(self):
        """Q-86 / 6: exact fixture and unchanged formulae."""
        data = self.success(self.cli("--file", self.main))
        self.totals(data, 2, (11, 22, 33, 44))
        self.files(data, [self.main])

    def test_repeat_does_not_modify_transcripts(self):
        """Q-86 / 6, 8: repeatability and read-only inputs, including children."""
        self.write(self.child, [assistant("A", (5, 6, 7, 8))])
        before = {p.relative_to(self.transcripts): p.read_bytes()
                  for p in self.transcripts.rglob("*") if p.is_file()}
        first = self.success(self.cli("--file", self.main))
        second = self.success(self.cli("--file", self.main))
        self.assertEqual(first, second)
        after = {p.relative_to(self.transcripts): p.read_bytes()
                 for p in self.transcripts.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_last_nonempty_usage_replaces_whole_record(self):
        """Q-86 / 7: last usage wins, later absent/empty usage does not erase it."""
        for tail in (assistant(tokens=None), {"type": "assistant", "message": {"id": "A", "usage": {}}}):
            with self.subTest(tail=tail):
                self.write(self.main, [assistant(), assistant(tokens=(2, 4, 6, 8)), tail])
                self.totals(self.success(self.cli("--file", self.main)), 1, (2, 4, 6, 8))

    def test_nonassistant_and_service_records_ignored(self):
        """Q-86 / 7, 10: only assistant usage counts; service records ignored."""
        self.write(self.main, [assistant(), assistant("X", (90, 90, 90, 90), "user"),
                               {"type": "system", "message": {"usage": {"input_tokens": 900}}},
                               {"type": "progress"}, assistant("unused", None), "", "   "])
        result = self.cli("--file", self.main)
        self.totals(self.success(result), 1, (1, 2, 3, 4))
        self.assertEqual(result.stderr, "")

    def test_messages_without_id_are_separate_and_warn(self):
        """Q-86 / 7: missing IDs are counted separately with diagnostics."""
        self.write(self.main, [assistant(None), assistant(None)])
        result = self.cli("--file", self.main)
        data = self.success(result)
        self.totals(data, 2, (2, 4, 6, 8))
        self.diagnostics(data, missing=2)
        self.diagnostics(data["main"], missing=2)
        self.diagnostics(data["subagents"])
        self.warning(result.stderr, self.main, 2, r"id|идентификатор|дедуп")

    def test_subagent_has_independent_id_scope_and_totals(self):
        """Q-86 / 8: identical A in main and child remains independent."""
        self.write(self.child, [assistant("A", (5, 6, 7, 8))])
        data = self.success(self.cli("--file", self.main))
        self.groups(data, (11, 22, 33, 44), (5, 6, 7, 8), 2, 1, [self.main], [self.child])
        for group in (data, data["main"], data["subagents"]):
            self.diagnostics(group)

    def test_text_displays_main_subagents_and_total(self):
        """Q-86 / 8, 9: separate text sections, including zero child usage."""
        for with_child in (False, True):
            with self.subTest(with_child=with_child):
                if with_child:
                    self.write(self.child, [assistant("A", (5, 6, 7, 8))])
                result = self.cli("--file", self.main, as_json=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                text = result.stdout.lower()
                self.assertRegex(text, r"основн|\bmain\b")
                self.assertRegex(text, r"субагент|subagents?")
                self.assertRegex(text, r"общ|итог|total")
                for label in ("input", "output", "cache_read", "cache_write", "work"):
                    self.assertIn(label, text)
                self.assertRegex(text, rf"\b{136 if with_child else 110}\b")

    def test_selection_modes_include_only_selected_children(self):
        """Q-86 / 9: explicit file, session, env and CWD/project agree."""
        self.write(self.child, [assistant("A", (5, 6, 7, 8))])
        self.write(self.transcripts / "other.jsonl", [assistant("A", (100, 100, 100, 100))])
        self.write(self.transcripts / "other" / "subagents" / "foreign.jsonl", [assistant()])
        for args, session in ((("--file", self.main), None),
                              (("--session", "session-one"), None),
                              (("--project", self.project, "--session", "session-one"), None),
                              ((), "session-one")):
            with self.subTest(args=args, session=session):
                data = self.success(self.cli(*args, session=session))
                self.groups(data, (11, 22, 33, 44), (5, 6, 7, 8), 2, 1, [self.main], [self.child])

    def test_all_sessions_include_each_transcript_once(self):
        """Q-86 / 9: dedup scope is each transcript, including two children."""
        self.write(self.child, [assistant("A", (5, 6, 7, 8)), assistant("A", (5, 6, 7, 8))])
        other = self.write(self.transcripts / "other.jsonl", [assistant()])
        child2 = self.write(self.transcripts / "other" / "subagents" / "two.jsonl", [assistant()])
        child3 = self.write(self.child.parent / "three.jsonl", [assistant()])
        data = self.success(self.cli("--all-sessions"))
        self.groups(data, (12, 24, 36, 48), (7, 10, 13, 16), 3, 3,
                    [self.main, other], [self.child, child2, child3])

    def test_missing_subagent_directory_is_zero_without_warning(self):
        """Q-86 / 9: absence is not an access failure."""
        self.write(self.main, [assistant()])
        result = self.cli("--file", self.main)
        data = self.success(result)
        self.assertEqual(result.stderr, "")
        self.groups(data, (1, 2, 3, 4), (0, 0, 0, 0), 1, 0, [self.main], [])
        self.diagnostics(data["subagents"])

    def test_invalid_json_is_counted_with_valid_stdout(self):
        """Q-86 / 10: two malformed lines; blanks/service records excluded."""
        with self.main.open("a", encoding="utf-8") as stream:
            stream.write('{broken\n{"unfinished":\n\n   \n{"type":"progress"}\n')
        result = self.cli("--file", self.main)
        self.assertEqual(result.returncode, 3, result.stderr)
        data = json.loads(result.stdout)
        self.diagnostics(data, invalid=2)
        self.diagnostics(data["main"], invalid=2)
        self.diagnostics(data["subagents"])
        self.totals(data, 2, (11, 22, 33, 44))
        self.warning(result.stderr, self.main, 2, r"json|бит|invalid|повреж|разбор")

    def test_diagnostics_sum_across_groups(self):
        """Q-86 / 7, 10: invalid lines and missing IDs sum across groups."""
        self.write(self.main, [assistant(), assistant(), assistant("B", (10, 20, 30, 40)),
                               assistant(None), "{bad", "", {"type": "progress"}])
        self.write(self.child, [assistant(None, (5, 6, 7, 8)), "{bad"])
        result = self.cli("--file", self.main)
        self.assertEqual(result.returncode, 3, result.stderr)
        data = json.loads(result.stdout)
        self.diagnostics(data, invalid=2, missing=2)
        self.diagnostics(data["main"], invalid=1, missing=1)
        self.diagnostics(data["subagents"], invalid=1, missing=1)
        self.groups(data, (12, 24, 36, 48), (5, 6, 7, 8), 3, 1, [self.main], [self.child])
        for path in (self.main, self.child):
            self.warning(result.stderr, path, 1, r"json|бит|invalid|повреж|разбор")
            self.warning(result.stderr, path, 1, r"id|идентификатор|дедуп")

    def assert_failure(self, result, path):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "No successful partial summary on error")
        self.assertIn(str(path), result.stderr)
        self.assertTrue(result.stderr.strip())

    def test_missing_explicit_file_fails(self):
        """Q-86 / 11: absent explicit main file is an error."""
        missing = self.transcripts / "missing.jsonl"
        self.assert_failure(self.cli("--file", missing), missing)

    def test_unreadable_main_file_fails(self):
        """Q-86 / 11: existing but unreadable main file, even under root."""
        self.assert_failure(self.cli("--file", self.main, denied=self.main), self.main)

    def test_unreadable_child_file_fails_without_partial_summary(self):
        """Q-86 / 11: readable main plus unreadable existing child."""
        self.write(self.child, [assistant()])
        self.assert_failure(self.cli("--file", self.main, denied=self.child), self.child)

    def test_unreadable_child_directory_fails_without_partial_summary(self):
        """Q-86 / 11: glob must not silently turn denied directory into zero."""
        self.write(self.child, [assistant()])
        self.assert_failure(self.cli("--file", self.main, denied=self.child.parent), self.child.parent)


if __name__ == "__main__":
    unittest.main()
