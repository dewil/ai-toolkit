#!/usr/bin/env python3
"""Blind CX01/CX04-06 contracts; synthetic transcripts, no live model/auth.

Oracle: accepted Codex-isolated-evals spec, not implementation internals.
Malformed/unknown trace must not silently turn a forbidden action into PASS.
"""
from __future__ import annotations
import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_eval_contract", ROOT / "scripts/run-evals.py")
engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engine
spec.loader.exec_module(engine)


def event(kind, **fields):
    return dict(type=kind, **fields)


def command(phase, ident="cmd-1", command="printf forbidden", **fields):
    return event("item." + phase, item=dict(id=ident, type="command_execution",
                 command=command, **fields))


def message(text):
    return event("item.completed", item=dict(id="msg-1", type="agent_message", text=text))


def start():
    return [event("thread.started", thread_id="thread-1"), event("turn.started")]


def terminal(**fields):
    return event("turn.completed", **fields)


def parse(events):
    return engine.parse_codex_transcript([x if isinstance(x, str) else json.dumps(x) for x in events])


class CodexTrace(unittest.TestCase):
    def test_started_updated_completed_command_is_one_ordered_attempt(self):
        run = parse(start() + [message("BEFORE_WORK"), command("started"),
            command("updated", aggregated_output="not the command"),
            command("completed", aggregated_output="stdout", exit_code=0),
            message("FINAL_ANSWER"), terminal(usage=dict(input_tokens=17, output_tokens=5,
                                                       cached_input_tokens=3))])
        self.assertTrue(run.completed)
        self.assertFalse(run.infra)
        self.assertEqual(len(run.tool_calls), 1)
        self.assertEqual(run.tool_calls[0][0], "Bash")
        self.assertIn("printf forbidden", run.tool_calls[0][1])
        self.assertNotIn("not the command", run.tool_calls[0][1])
        body = engine.transcript_text(run)
        self.assertLess(body.index("BEFORE_WORK"), body.index("printf forbidden"))
        self.assertLess(body.index("printf forbidden"), body.index("FINAL_ANSWER"))
        self.assertIn("FINAL_ANSWER", run.text)
        self.assertFalse(engine.check({"no_tool_call": {"name": "Bash", "args_match": "forbidden"}}, run, {})[0])
        self.assertEqual(run.usage, dict(input=17, output=5, cache_read=3))
        self.assertIsNone(run.cost, "Codex token counts do not establish USD price")

    def test_declaration_after_command_does_not_become_before(self):
        run = parse(start() + [command("started", command="cat src/app.py"),
            command("completed", command="cat src/app.py", exit_code=0),
            message("Делаю сам"), terminal()])
        self.assertFalse(engine.check({"text_before_tool": {"pattern": "делаю сам"}}, run, {})[0])

    def test_nonzero_command_is_observed_behavior_not_transport_error(self):
        run = parse(start() + [command("started"), command("completed", exit_code=23), terminal()])
        self.assertTrue(run.completed)
        self.assertFalse(run.infra)
        self.assertEqual(len(run.tool_calls), 1)

    def test_completed_only_command_keeps_attempt_but_is_untrusted(self):
        run = parse(start() + [command("completed", exit_code=0), terminal()])
        self.assertTrue(run.infra)
        self.assertEqual(len(run.tool_calls), 1)
        self.assertFalse(engine.check({"no_tool_call": {"name": "Bash"}}, run, {})[0])

    def test_malformed_unknown_and_lifecycle_breaks_are_infrastructure(self):
        cases = [start(), [terminal()], start() + [terminal(), terminal()],
            start() + [terminal(), message("too late")],
            start() + [command("started"), terminal()],
            start() + [command("started"), command("completed", command="replaced", exit_code=0), terminal()],
            start() + ["broken json", terminal()], start() + ["[]", terminal()],
            start() + ["null", terminal()], start() + [event("future.kind"), terminal()],
            [event("thread.started", thread_id=""), event("turn.started"), terminal()],
            [event("thread.started", thread_id=3), event("turn.started"), terminal()],
            start() + [event("turn.started"), terminal()],
            start() + [command("started", ident=""), terminal()],
            start() + [command("started", command=None), terminal()],
            start() + [event("item.completed", item=dict(id="x", type="agent_message", text=7)), terminal()],
            start() + [event("error", message="synthetic error"), terminal()],
            start() + [event("turn.failed", error=dict(message="synthetic failure"))]]
        for events in cases:
            with self.subTest(events=events):
                self.assertTrue(parse(events).infra)

    def test_unknown_tools_are_not_negative_assertion_success(self):
        for kind in ("mcp_tool_call", "web_search", "collab_tool_call", "future_tool"):
            run = parse(start() + [event("item.completed", item=dict(id="x", type=kind)), terminal()])
            with self.subTest(kind=kind):
                self.assertTrue(run.infra)

    def test_bad_usage_is_not_silently_zero(self):
        for field in ("input_tokens", "output_tokens", "cached_input_tokens"):
            for bad in (-1, True, "2", 1.5, None):
                usage = dict(input_tokens=17, output_tokens=5, cached_input_tokens=3)
                usage[field] = bad
                with self.subTest(field=field, bad=bad):
                    self.assertTrue(parse(start() + [terminal(usage=usage)]).infra)

    def test_absent_usage_remains_unknown_cost(self):
        run = parse(start() + [message("done"), terminal()])
        self.assertFalse(run.infra)
        self.assertIsNone(run.cost)
        self.assertEqual(run.usage, dict(input=None, output=None, cache_read=None))

    def test_cache_count_cannot_exceed_input(self):
        self.assertTrue(parse(start() + [terminal(usage=dict(input_tokens=1, output_tokens=2, cached_input_tokens=3))]).infra)

    def test_reasoning_is_not_final_answer_and_file_change_is_not_fake_bash(self):
        run = parse(start() + [event("item.completed", item=dict(id="r", type="reasoning", text="PRIVATE_REASONING")),
            event("item.completed", item=dict(id="f", type="file_change", status="completed",
                 changes=[dict(path="note.txt", kind="add")])), message("FINAL"), terminal()])
        self.assertFalse(run.infra)
        self.assertNotIn("PRIVATE_REASONING", run.text)
        self.assertEqual(run.tool_calls, [])


class CodexCLI(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.scenarios = self.root / "scenarios"
        self.scenario = self.scenarios / "99-codex"
        self.fixture = self.scenario / "fixture"
        self.fixture.mkdir(parents=True)
        (self.scenario / "prompt.md").write_text("Synthetic prompt", encoding="utf-8")
        self.expect = dict(title="Codex probe", harness=True, rules=[], hard=[{"exit_ok": {}}], soft=[])
        self.write_expect()
        for name, value in (("SCENARIOS", self.scenarios), ("BASELINES", self.root / "baselines"),
                            ("RUNS", self.root / "reports")):
            p = patch.object(engine, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_expect(self):
        (self.scenario / "expect.json").write_text(json.dumps(self.expect), encoding="utf-8")

    def cli(self, *flags, run=None):
        output = io.StringIO()
        if run is None:
            run = engine.Run()
            run.completed = True
            run.text = "done"
            run.provider = "codex"
            run.cost = None
        with patch.object(sys, "argv", ["run-evals.py", "--model", "subject", "--runs", "1", *flags]), \
             patch.object(engine, "run_once", return_value=(run, {})) as launch, \
             patch.object(subprocess, "run", side_effect=AssertionError("unexpected process")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                code = engine.main()
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), launch

    def test_default_and_explicit_codex_keep_six_argument_seam(self):
        for flags in ((), ("--provider", "codex")):
            code, output, launch = self.cli(*flags)
            with self.subTest(flags=flags):
                self.assertEqual(code, 0, output)
                self.assertEqual(len(launch.call_args.args), 6)
                specs = [arg for arg in launch.call_args.args if isinstance(arg, dict)]
                self.assertTrue(any(arg.get("_provider") == "codex" for arg in specs))

    def test_claude_live_refuses_before_executor(self):
        code, output, launch = self.cli("--provider", "claude")
        self.assertNotEqual(code, 0)
        launch.assert_not_called()
        self.assertRegex(output.lower(), r"claude")

    def test_list_remains_readonly_without_auth(self):
        code, output, launch = self.cli("--list")
        self.assertEqual(code, 0, output)
        launch.assert_not_called()

    def test_unknown_codex_money_is_reported_unknown(self):
        code, output, _ = self.cli("--baseline")
        self.assertEqual(code, 0, output)
        self.assertNotIn("$0.00", output)
        self.assertRegex(output.lower(), r"unknown|неизвест|не определ")


class PreModelRejections(unittest.TestCase):
    setUp = CodexCLI.setUp
    write_expect = CodexCLI.write_expect
    # run_once is the public six-argument seam. Any subprocess here is too late.
    def attempt(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("process before capability validation")) as process, \
             patch.object(subprocess, "Popen", side_effect=AssertionError("process before capability validation")) as popen:
            result, before = engine.run_once("99-codex", {**self.expect, "_provider": "codex"},
                "Synthetic prompt", self.fixture, "subject", self.root / "trace.jsonl")
        self.assertTrue(result.infra)
        process.assert_not_called()
        popen.assert_not_called()
        return result

    def test_faithful_context_prepared_only_in_working_copy(self):
        (self.fixture / "CLAUDE.md").write_text("Main instruction\n@rules/local.md\n", encoding="utf-8")
        (self.fixture / "rules").mkdir()
        (self.fixture / "rules/local.md").write_text("IMPORTED_INSTRUCTION", encoding="utf-8")
        observed = []
        def fake_process(argv, **kwargs):
            args = [str(arg) for arg in argv]
            self.assertTrue(any("codex-sandbox.py" in arg for arg in args), args)
            self.assertIn("--root", args)
            work = Path(args[args.index("--root") + 1])
            self.assertNotEqual(work.resolve(), self.fixture.resolve())
            context = (work / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Main instruction", context)
            self.assertIn("IMPORTED_INSTRUCTION", context)
            self.assertIn("local.md", context)
            observed.append(context)
            return subprocess.CompletedProcess(argv, 0, "\n".join(map(json.dumps, start() + [message("done"), terminal()])), "")
        with patch.object(subprocess, "run", side_effect=fake_process), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")):
            run, before = engine.run_once("99-codex", {**self.expect, "_provider": "codex"},
                "Synthetic prompt", self.fixture, "subject", self.root / "trace.jsonl")
        self.assertFalse(run.infra)
        self.assertTrue(observed)
        self.assertFalse((self.fixture / "AGENTS.md").exists())
        self.assertIn("AGENTS.md", before, "snapshot must follow adapter preparation")

    def test_positive_and_negative_nonportable_tool_asserts_refuse(self):
        for assertion in ("tool_call", "no_tool_call"):
            for name in ("Agent", "Task", "Read", "Edit", "Write", "mcp__service__call"):
                self.expect["hard"] = [{assertion: {"name": name}}]
                with self.subTest(assertion=assertion, name=name):
                    self.attempt()

    def test_nonportable_allowed_and_disallowed_tools_refuse(self):
        for option in ("allowed_tools", "disallowed_tools"):
            self.expect[option] = ["Read"]
            with self.subTest(option=option):
                self.attempt()
            del self.expect[option]

    def test_import_escape_absolute_symlink_cycle_and_oversize_refuse(self):
        outside = self.root / "outside.md"
        outside.write_text("OUTSIDE", encoding="utf-8")
        link = self.fixture / "linked.md"
        link.symlink_to(outside)
        cases = ["@../outside.md", "@" + str(outside), "@linked.md", "@cycle.md", "x" * (24 * 1024 + 1)]
        (self.fixture / "cycle.md").write_text("@CLAUDE.md", encoding="utf-8")
        for content in cases:
            (self.fixture / "CLAUDE.md").write_text(content, encoding="utf-8")
            with self.subTest(content=content[:60]):
                self.attempt()
            self.assertFalse((self.fixture / "AGENTS.md").exists(), "source fixture mutated")

    def test_conflicting_sources_and_fixture_runtime_configuration_refuse(self):
        (self.fixture / "CLAUDE.md").write_text("Trusted source A", encoding="utf-8")
        (self.fixture / "AGENTS.md").write_text("Conflicting source B", encoding="utf-8")
        self.attempt()
        (self.fixture / "AGENTS.md").unlink()
        for relative in (".agents/skills/x/SKILL.md", ".claude/settings.json"):
            path = self.fixture / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            with self.subTest(relative=relative):
                self.attempt()
            path.unlink()


class CodexJudge(unittest.TestCase):
    def outcome(self, answer, *, code=0, tool=False):
        run = engine.Run()
        run.completed = True
        run.provider = "codex"
        run.text = "untrusted transcript marker"
        events = start()
        if tool:
            events += [command("started"), command("completed", exit_code=0)]
        events += [message(answer), terminal()]
        done = subprocess.CompletedProcess([], code, "\n".join(map(json.dumps, events)), "synthetic private stderr")
        with patch.object(subprocess, "run", return_value=done) as invoke, \
             patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")):
            outcome = engine.judge_outcome("criterion marker", run, "arbiter")
        self.assertTrue(invoke.called)
        argv = [str(x) for x in invoke.call_args.args[0]]
        self.assertTrue(any("codex-sandbox.py" in x for x in argv), argv)
        self.assertIn("--judge", argv)
        self.assertNotIn("claude", argv)
        return outcome

    def test_strict_pass_fail_schema(self):
        for verdict in ("pass", "fail"):
            with self.subTest(verdict=verdict):
                self.assertEqual(self.outcome(json.dumps(dict(verdict=verdict, why="because")))["outcome"], verdict)

    def test_invalid_whole_objects_tool_attempts_and_process_errors_reject(self):
        for answer in ('{"verdict":"pass"}', '{"verdict":"pass","why":3}',
            '{"verdict":"maybe","why":"x"}', '{"verdict":"pass","why":"x","extra":1}',
            'prefix {"verdict":"pass","why":"x"}', '[{"verdict":"pass","why":"x"}]', ''):
            with self.subTest(answer=answer):
                self.assertEqual(self.outcome(answer)["outcome"], "judge_error")
        answer = '{"verdict":"pass","why":"x"}'
        self.assertEqual(self.outcome(answer, tool=True)["outcome"], "judge_error")
        self.assertEqual(self.outcome(answer, code=1)["outcome"], "judge_error")


class ProviderComparison(unittest.TestCase):
    def entry(self):
        counts = dict(attempts=1, completed=1, evaluated=1, behavior_fail_runs=0,
                      hard_fail_runs=0, soft_fail_runs=0, infrastructure_error_runs=0, judge_error_runs=0)
        p = {key: "a" * 64 for key in ("prompt_sha256", "criteria_sha256", "fixture_sha256", "harness_sha256",
            "model_config_sha256", "judge_rubric_sha256", "isolation_config_sha256", "effective_context_sha256")}
        p.update(model="subject", model_version="exact", judge_model="arbiter", judge_version="exact",
            judge_used=True, provider="codex", judge_provider="codex", cli_version="0.162.1",
            isolation_contract_version="codex-eval-v1", context_adapter_version="codex-context-v1")
        return dict(status="green", measurements=counts, provenance=p)

    def test_provider_context_and_judge_provider_never_bypassed_by_vary(self):
        for field in ("provider", "effective_context_sha256", "judge_provider"):
            for missing in (False, True):
                previous, current = self.entry(), self.entry()
                if missing:
                    del previous["provenance"][field]
                else:
                    previous["provenance"][field] = "b" * 64 if field.endswith("sha256") else "claude"
                with self.subTest(field=field, missing=missing):
                    result = engine.compare_measurements(previous, current, vary=("model", "judge", "fixture"))
                    self.assertEqual(result["comparability"], "incomparable")
                    self.assertIsNone(result["delta"])
                    self.assertTrue(result["reasons"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
