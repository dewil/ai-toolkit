#!/usr/bin/env python3
"""Blind contract tests for TOOLKIT-EVAL-BASELINE-20261009; no live LLMs."""
from __future__ import annotations

import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("eval_evidence_under_test", ROOT / "scripts/run-evals.py")
engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engine
spec.loader.exec_module(engine)


def measurements(attempts=10, failures=1, hard=None, soft=0, infra=0, judge=0):
    return dict(attempts=attempts, completed=attempts-infra,
                evaluated=attempts-infra-judge, behavior_fail_runs=failures,
                hard_fail_runs=failures if hard is None else hard,
                soft_fail_runs=soft, infrastructure_error_runs=infra,
                judge_error_runs=judge)


def provenance():
    p = {key: hashlib.sha256(key.encode()).hexdigest() for key in (
        "prompt_sha256", "criteria_sha256", "fixture_sha256", "harness_sha256",
        "model_config_sha256", "judge_rubric_sha256")}
    p.update(model="subject", model_version="subject-exact-1", judge_model="arbiter",
             judge_version="arbiter-exact-1", judge_used=True)
    return p


def entry(failures=1):
    return dict(status="yellow", stamp="synthetic", runs=10,
                hard_fail_runs=failures, soft_fail_runs=0,
                measurements=measurements(failures=failures), provenance=provenance())


class MeasurementsContract(unittest.TestCase):
    def test_valid_overlap_and_excluded_runs(self):
        for counts in (measurements(), measurements(failures=3, hard=2, soft=2),
                       measurements(attempts=4, failures=0, infra=2, judge=2)):
            with self.subTest(counts=counts):
                engine.validate_measurements(counts)

    def test_invalid_types_missing_counts_and_contradictions(self):
        mutations = [(key, bad) for key in measurements() for bad in (-1, True, 1.5, "1", None)]
        mutations += [("attempts", 0), ("completed", 9), ("evaluated", 9),
                      ("behavior_fail_runs", 0), ("behavior_fail_runs", 2),
                      ("hard_fail_runs", 11), ("soft_fail_runs", 11)]
        for key, bad in mutations:
            data = measurements()
            data[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                engine.validate_measurements(data)
        for key in measurements():
            data = measurements()
            del data[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                engine.validate_measurements(data)

    def test_same_yellow_rates_and_percentage_point_delta(self):
        result = engine.compare_measurements(entry(1), entry(4))
        self.assertEqual(result["comparability"], "comparable")
        for name, failures, rate in (("baseline", 1, .1), ("current", 4, .4)):
            self.assertEqual(result[name]["failures"], failures)
            self.assertEqual(result[name]["evaluated"], 10)
            self.assertAlmostEqual(result[name]["rate"], rate)
        self.assertAlmostEqual(result["delta"], .3)

    def test_legacy_unknown_is_not_zero(self):
        result = engine.compare_measurements({"status": "yellow", "stamp": "old"}, entry())
        self.assertIsNone(result["baseline"]["failures"])
        self.assertIsNone(result["baseline"]["evaluated"])
        self.assertIsNone(result["baseline"]["rate"])
        self.assertIsNone(result["delta"])
        self.assertEqual(result["comparability"], "unknown")

    def test_zero_evaluated_is_unknown_rate(self):
        current = entry(0)
        current["measurements"] = measurements(attempts=3, failures=0, infra=2, judge=1)
        result = engine.compare_measurements(entry(), current)
        self.assertEqual(result["current"]["evaluated"], 0)
        self.assertIsNone(result["current"]["rate"])
        self.assertIsNone(result["delta"])

    def test_each_undeclared_axis_blocks_delta_with_field_reason(self):
        axes = {"model": ("model", "model_version", "model_config_sha256"),
                "judge": ("judge_model", "judge_version", "judge_rubric_sha256"),
                "prompt": ("prompt_sha256",), "criteria": ("criteria_sha256",),
                "fixture": ("fixture_sha256",), "harness": ("harness_sha256",)}
        for axis, fields in axes.items():
            for field in fields:
                current = entry(4)
                current["provenance"][field] = "b"*64 if field.endswith("sha256") else "changed"
                with self.subTest(axis=axis, field=field):
                    result = engine.compare_measurements(entry(), current)
                    self.assertEqual(result["comparability"], "incomparable")
                    self.assertIsNone(result["delta"])
                    self.assertIn(field, " ".join(result["reasons"]))
                    self.assertEqual(result["current"]["failures"], 4)
                    permitted = engine.compare_measurements(entry(), current, vary=(axis,))
                    self.assertEqual(permitted["comparability"], "comparable")
                    self.assertAlmostEqual(permitted["delta"], .3)

    def test_declared_axis_does_not_cover_unrelated_drift(self):
        current = entry(4)
        current["provenance"].update(fixture_sha256="b"*64, criteria_sha256="c"*64)
        self.assertEqual(engine.compare_measurements(entry(), current, vary=("fixture",))["comparability"],
                         "incomparable")
        self.assertAlmostEqual(engine.compare_measurements(entry(), current,
                               vary=("fixture", "criteria"))["delta"], .3)

    def test_unknown_version_limits_even_declared_comparison(self):
        previous, current = entry(), entry(4)
        previous["provenance"]["model_version"] = "unknown"
        current["provenance"]["model_version"] = "unknown"
        result = engine.compare_measurements(previous, current, vary=("model",))
        self.assertEqual(result["comparability"], "limited")
        self.assertTrue(result["reasons"])
        self.assertAlmostEqual(result["delta"], .3)

    def test_unused_judge_fields_do_not_create_drift(self):
        previous, current = entry(), entry(4)
        for e in (previous, current):
            e["provenance"]["judge_used"] = False
            for key in ("judge_model", "judge_version", "judge_rubric_sha256"):
                e["provenance"][key] = None
        self.assertEqual(engine.compare_measurements(previous, current)["comparability"], "comparable")


class BaselineValidation(unittest.TestCase):
    def test_merge_preserves_legacy_and_complete_unselected_entry(self):
        old = {"status": "green", "stamp": "old", "extra": {"nested": [1, 2]}}
        original = {"scenarios": {"untouched": old, "updated": entry()}}
        before = copy.deepcopy(original)
        merged = engine.merge_baseline(original, {"updated": entry(4)}, "now")
        self.assertEqual(merged["untouched"], old)
        self.assertEqual(merged["updated"]["measurements"], measurements(failures=4))
        self.assertEqual(merged["updated"]["provenance"], provenance())
        self.assertEqual(merged["updated"]["stamp"], "now")
        self.assertEqual(original, before)

    def test_empty_merge_is_loud(self):
        with self.assertRaises((ValueError, SystemExit)):
            engine.merge_baseline({"scenarios": {"keep": entry()}}, {}, "now")

    def test_malformed_new_baseline_is_loud_not_missing(self):
        documents = []
        for version in (True, 0, 3, "2"):
            documents.append(dict(schema_version=version, scenarios={"probe": entry()}))
        for counts in ({}, {**measurements(), "completed": 0},
                       {**measurements(), "attempts": True}):
            e = entry()
            e["measurements"] = counts
            documents.append(dict(schema_version=2, scenarios={"probe": e}))
        for bad in ([], "wrong", {**provenance(), "prompt_sha256": 17}):
            e = entry()
            e["provenance"] = bad
            documents.append(dict(schema_version=2, scenarios={"probe": e}))
        for field, value in (("prompt_sha256", "bad-hash"), ("judge_used", "yes"),
                             ("model", None), ("judge_version", False)):
            e = entry()
            e["provenance"][field] = value
            documents.append(dict(schema_version=2, scenarios={"probe": e}))
        with tempfile.TemporaryDirectory() as temp, patch.object(engine, "BASELINES", Path(temp)):
            for document in documents:
                engine.baseline_path("subject").write_text(json.dumps(document), encoding="utf-8")
                with self.subTest(document=document), self.assertRaises((ValueError, SystemExit)):
                    engine.load_baseline("subject")


class JudgeContract(unittest.TestCase):
    def test_fail_pass_and_unavailable_are_distinct_and_tuple_compatible(self):
        run = engine.Run()
        run.completed = True
        for response, expected in (({"verdict": "pass", "why": "ok"}, "pass"),
                                   ({"verdict": "fail", "why": "behavior"}, "fail"),
                                   ({"verdict": "unexpected", "why": "bad"}, "judge_error")):
            done = type("Done", (), {"returncode": 0,
                        "stdout": json.dumps({"result": json.dumps(response)}), "stderr": ""})()
            with self.subTest(expected=expected), patch.object(engine.subprocess, "run", return_value=done):
                outcome = engine.judge_outcome("criterion", run, "arbiter")
                self.assertEqual(outcome["outcome"], expected)
                self.assertIsInstance(outcome["reason"], str)
                self.assertEqual(engine.judge("criterion", run, "arbiter")[0], expected == "pass")
        with patch.object(engine.subprocess, "run", side_effect=OSError("synthetic unavailable")):
            self.assertEqual(engine.judge_outcome("criterion", run, "arbiter")["outcome"], "judge_error")

    def test_empty_malformed_and_nonzero_judge_responses_are_errors(self):
        run = engine.Run()
        run.completed = True
        for stdout, code in (("", 0), ("not-json", 0), ("{}", 0),
                             (json.dumps({"result": "not-json"}), 0),
                             (json.dumps({"result": '{"verdict":"pass","why":"ok"}'}), 1)):
            done = type("Done", (), dict(returncode=code, stdout=stdout, stderr="private synthetic data"))()
            with self.subTest(stdout=stdout, code=code), patch.object(engine.subprocess, "run", return_value=done):
                self.assertEqual(engine.judge_outcome("criterion", run, "arbiter")["outcome"], "judge_error")


class CLIRoundtrip(unittest.TestCase):
    """Real scenario loading, argv parsing, baseline and report filesystem writes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scenarios = self.root / "scenarios"
        self.scenarios.mkdir()
        self.baselines = self.root / "new" / "nested" / "baselines"
        self.reports = self.root / "reports"
        for name, value in (("SCENARIOS", self.scenarios), ("BASELINES", self.baselines),
                            ("RUNS", self.reports)):
            patcher = patch.object(engine, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scenario = self.scenarios / "99-probe"
        self.scenario.mkdir()
        (self.scenario / "prompt.md").write_text("Synthetic task; marker PROMPT_PAYLOAD", encoding="utf-8")
        self.fixture = self.scenario / "fixture"
        self.fixture.mkdir()
        (self.fixture / "note.txt").write_text("FIXTURE_PAYLOAD", encoding="utf-8")
        self.expect = dict(title="Synthetic probe", harness=True, rules=[],
                           hard=[{"exit_ok": {}}], soft=[])
        self.write_expect()

    def write_expect(self):
        (self.scenario / "expect.json").write_text(json.dumps(self.expect), encoding="utf-8")

    def run_object(self, failed=False, infra=""):
        r = engine.Run()
        r.completed = not bool(infra)
        r.infra = infra
        r.is_error = failed
        r.text = "TRANSCRIPT_PAYLOAD"
        return r

    def cli(self, runs, *flags, judge_results=None):
        output = io.StringIO()
        argv = ["run-evals.py", "--model", "subject", "--runs", str(len(runs)), *flags]
        with patch.object(sys, "argv", argv), \
             patch.object(engine, "run_once", side_effect=[(r, {}) for r in runs]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            if judge_results is None:
                code = engine.main()
            else:
                with patch.object(engine, "judge_outcome", side_effect=judge_results):
                    code = engine.main()
        return code, output.getvalue()

    def baseline(self, model="subject"):
        return json.loads(engine.baseline_path(model).read_text(encoding="utf-8"))

    def all_report_text(self, output):
        texts = [output]
        if self.reports.exists():
            for p in self.reports.rglob("*"):
                if p.is_file() and p.suffix in (".md", ".txt", ".json"):
                    texts.append(p.read_text(encoding="utf-8"))
        return "\n".join(texts)

    def test_schema_counts_provenance_roundtrip_and_partial_legacy(self):
        self.baselines.mkdir(parents=True)
        legacy = dict(status="red", stamp="legacy", extra={"retain": [1, 3]})
        engine.baseline_path("subject").write_text(json.dumps({"scenarios": {"untouched": legacy}}), encoding="utf-8")
        code, _ = self.cli([self.run_object(failed=i == 0) for i in range(10)], "--baseline")
        self.assertEqual(code, 0)
        saved = self.baseline()
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["scenarios"]["untouched"], legacy)
        current = saved["scenarios"]["99-probe"]
        self.assertEqual(current["status"], "yellow")
        self.assertEqual(current["runs"], 10)
        self.assertEqual(current["hard_fail_runs"], 1)
        self.assertEqual(current["measurements"], measurements())
        p = current["provenance"]
        for key in ("prompt_sha256", "criteria_sha256", "fixture_sha256", "harness_sha256", "model_config_sha256"):
            self.assertRegex(p[key], r"^[0-9a-f]{64}$")
        self.assertEqual(p["model"], "subject")
        self.assertEqual(p["model_version"], "unknown")
        self.assertFalse(p["judge_used"])
        loaded = engine.load_baseline("subject")
        self.assertEqual(loaded["scenarios"]["99-probe"], current)
        serialized = json.dumps(saved)
        for payload in ("PROMPT_PAYLOAD", "FIXTURE_PAYLOAD", "TRANSCRIPT_PAYLOAD", str(self.root)):
            self.assertNotIn(payload, serialized)

    def test_new_parent_directory_created_for_baseline(self):
        self.assertFalse(self.baselines.exists())
        self.cli([self.run_object()], "--baseline")
        self.assertEqual(self.baseline()["schema_version"], 2)

    def test_same_color_report_shows_both_counts_and_30_percentage_points(self):
        self.cli([self.run_object(failed=i == 0) for i in range(10)], "--baseline")
        code, output = self.cli([self.run_object(failed=i < 4) for i in range(10)])
        self.assertEqual(code, 0)
        report = self.all_report_text(output)
        self.assertRegex(report, r"1\s*/\s*10")
        self.assertRegex(report, r"4\s*/\s*10")
        self.assertRegex(report, r"\+30(?:[.,]0+)?\s*(?:pp|п\.?\s*п\.?|percentage\s*points)")

    def test_compare_model_and_repeated_vary_do_not_change_execution(self):
        self.cli([self.run_object()], "--baseline")
        previous = self.baseline()
        if "provenance" in previous["scenarios"]["99-probe"]:
            previous["scenarios"]["99-probe"]["provenance"]["model"] = "previous"
        engine.baseline_path("previous").write_text(json.dumps(previous), encoding="utf-8")
        code, _ = self.cli([self.run_object()], "--compare", "previous",
                           "--vary", "fixture", "--vary", "criteria")
        self.assertEqual(code, 0)

    def test_invalid_baseline_aborts_without_overwriting_existing_bytes(self):
        self.baselines.mkdir(parents=True)
        document = dict(schema_version=2, scenarios={"99-probe": entry()})
        document["scenarios"]["99-probe"]["measurements"]["attempts"] = True
        target = engine.baseline_path("subject")
        original = json.dumps(document).encode()
        target.write_bytes(original)
        with self.assertRaises((ValueError, SystemExit)):
            self.cli([self.run_object()], "--baseline")
        self.assertEqual(target.read_bytes(), original)

    def test_multiple_failing_asserts_count_one_behavior_run(self):
        self.expect["hard"] = [{"exit_ok": {}}, {"file_exists": {"path": "missing.txt"}}]
        self.expect["soft"] = [{"in_output_any": {"texts": ["absent"]}}]
        self.write_expect()
        code, _ = self.cli([self.run_object(failed=True)], "--baseline")
        self.assertEqual(code, 1)
        e = self.baseline()["scenarios"]["99-probe"]
        self.assertEqual(e["status"], "red")
        self.assertEqual(e["measurements"], measurements(attempts=1, failures=1, hard=1, soft=1))

    def test_infra_and_repeated_judge_errors_are_one_excluded_run_each(self):
        self.expect["soft"] = [{"judge": "first criterion"}, {"judge": "second criterion"}]
        self.write_expect()
        errors = [dict(outcome="judge_error", reason="JUDGE_PRIVATE_PAYLOAD")]*2
        code, output = self.cli([self.run_object(infra="STDERR_PRIVATE_PAYLOAD"),
                                 self.run_object()], "--baseline", judge_results=errors)
        self.assertEqual(code, 1)  # Existing gate: one infra hard failure among two is red.
        e = self.baseline()["scenarios"]["99-probe"]
        self.assertEqual(e["status"], "red")
        self.assertEqual(e["hard_fail_runs"], 1)
        self.assertEqual(e["soft_fail_runs"], 1)
        self.assertEqual(e["measurements"], measurements(attempts=2, failures=0, infra=1, judge=1))
        result = engine.compare_measurements(e, e)
        self.assertIsNone(result["current"]["rate"])
        saved = json.dumps(self.baseline())
        for payload in ("STDERR_PRIVATE_PAYLOAD", "JUDGE_PRIVATE_PAYLOAD", "TRANSCRIPT_PAYLOAD"):
            self.assertNotIn(payload, saved)
        report = self.all_report_text(output).lower()
        self.assertRegex(report, r"infra|инфра")
        self.assertRegex(report, r"judge|судь")
        self.assertRegex(report, r"unknown|неизвест|не определ")

    def test_observed_fail_with_judge_error_is_not_fully_evaluated(self):
        self.expect["soft"] = [{"judge": "failing"}, {"judge": "unavailable"}]
        self.write_expect()
        outcomes = [dict(outcome="fail", reason="behavior"),
                    dict(outcome="judge_error", reason="unavailable")]
        code, _ = self.cli([self.run_object()], "--baseline", judge_results=outcomes)
        self.assertEqual(code, 0)  # Existing soft-only failure remains yellow.
        e = self.baseline()["scenarios"]["99-probe"]
        self.assertEqual(e["status"], "yellow")
        self.assertEqual(e["soft_fail_runs"], 1)
        self.assertEqual(e["measurements"], measurements(attempts=1, failures=0, judge=1))

    def test_fixture_digest_does_not_follow_external_symlink_content(self):
        outside = self.root / "outside.txt"
        outside.write_text("EXTERNAL_PAYLOAD_A", encoding="utf-8")
        (self.fixture / "external-link").symlink_to(outside)
        self.cli([self.run_object()], "--baseline")
        first = self.baseline()["scenarios"]["99-probe"]["provenance"]["fixture_sha256"]
        outside.write_text("EXTERNAL_PAYLOAD_B", encoding="utf-8")
        self.cli([self.run_object()], "--baseline")
        self.assertEqual(self.baseline()["scenarios"]["99-probe"]["provenance"]["fixture_sha256"], first)
        (self.fixture / "note.txt").write_text("changed fixture", encoding="utf-8")
        self.cli([self.run_object()], "--baseline")
        self.assertNotEqual(self.baseline()["scenarios"]["99-probe"]["provenance"]["fixture_sha256"], first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
