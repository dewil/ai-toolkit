"""Independent regressions for review F1-F3 and clarified INV-COST-05.

Synthetic CLI evidence only; no model, auth, native-session, or network reads.
Criteria fixed before writing against session-cost SHA256 ada3b126be3101083eac
1f191ed92df8056502fa4e0119b4802b227b6d165e63. The author may change production
concurrently; a RED claim requires matching before/after baseline hashes.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/session-cost.py"
PRIVATE = "SYNTHETIC_REVIEW_ERROR_BODY_MUST_NOT_BE_EMITTED"


def thread():
    return {"type": "thread.started", "thread_id": "review-synthetic-thread"}


def started():
    return {"type": "turn.started"}


def completed():
    return {"type": "turn.completed", "usage": {
        "input_tokens": 11, "output_tokens": 3, "cached_input_tokens": 7}}


def error():
    return {"type": "error", "message": PRIVATE, "error": {"body": PRIVATE}}


class ReceiptReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="receipt-review-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root / "events.jsonl"

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                cwd=self.root, capture_output=True, text=True,
                                timeout=15)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        return result

    def receipt(self, events, *, extra=()):
        self.events.write_text("".join(json.dumps(event) + "\n" for event in events),
                               encoding="utf-8")
        return self.cli("--codex-events", "--file", self.events,
                        "--run-id", "review-run", "--role", "reviewer", *extra)

    def payload(self, result, code=3):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def assert_unknown(self, record):
        self.assertEqual(record["status"], "unknown")
        self.assertIsNone(record["tokens"])
        self.assertTrue(record["reason"].strip())

    def assert_measured(self, record):
        self.assertEqual(record["status"], "measured")
        self.assertEqual(record["tokens"], {
            "input": 11, "output": 3, "cache_read": 7,
            "cache_write": None, "reasoning": None})

    def assert_bad(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip())

    def ledger(self, records):
        path = self.root / "ledger.json"
        path.write_text(json.dumps({"schema_version": 1,
                                    "task_id": "REVIEW-SYNTHETIC",
                                    "records": records}), encoding="utf-8")
        return path

    def test_every_event_requires_nonempty_string_type_after_valid_usage(self):
        invalid = [{"message": PRIVATE}] + [
            {"type": value, "message": PRIVATE}
            for value in (None, True, 17, [], {}, "", " \t ")]
        for event in invalid:
            with self.subTest(event_type=event.get("type")):
                self.assert_bad(self.receipt([thread(), started(), completed(), event]))

    def test_invalid_event_before_thread_is_not_successful_unknown(self):
        self.assert_bad(self.receipt([{"type": False, "message": PRIVATE}]))

    def test_error_after_completed_turn_adds_unknown_and_keeps_usage(self):
        events = [thread(), started(), completed(), error()]
        payload = self.payload(self.receipt(events))
        self.assertEqual(payload["coverage"], "partial")
        self.assertEqual(len(payload["records"]), 2)
        self.assert_measured(payload["records"][0])
        self.assert_unknown(payload["records"][1])
        self.assertEqual(payload, self.payload(self.receipt(events)),
                         "same captured launch must keep stable error IDs")

    def test_error_inside_turn_then_completed_preserves_usage_and_unknown(self):
        payload = self.payload(self.receipt([thread(), started(), error(), completed()]))
        measured = [r for r in payload["records"] if r["status"] == "measured"]
        missing = [r for r in payload["records"] if r["status"] == "unknown"]
        self.assertEqual(len(measured), 1)
        self.assertEqual(len(missing), 1)
        self.assert_measured(measured[0])
        self.assert_unknown(missing[0])

    def test_error_inside_failed_turn_is_one_unknown_not_double_counted(self):
        payload = self.payload(self.receipt([thread(), started(), completed(),
            started(), error(), {"type": "turn.failed", "error": PRIVATE}]))
        self.assertEqual(len(payload["records"]), 2)
        self.assert_measured(payload["records"][0])
        self.assert_unknown(payload["records"][1])

    def test_error_inside_unfinished_turn_is_one_unknown(self):
        payload = self.payload(self.receipt([thread(), started(), error()]))
        self.assertEqual(len(payload["records"]), 1)
        self.assert_unknown(payload["records"][0])

    def test_error_outside_turn_before_thread_does_not_hide_later_usage(self):
        payload = self.payload(self.receipt([error(), thread(), started(), completed()]))
        self.assertEqual(len(payload["records"]), 2)
        self.assertEqual({r["status"] for r in payload["records"]}, {"unknown", "measured"})
        self.assertEqual(len({r["id"] for r in payload["records"]}), 2)

    def test_error_only_launch_is_unknown_with_known_metadata(self):
        payload = self.payload(self.receipt([error()], extra=(
            "--model", "synthetic-model", "--vendor", "synthetic-vendor")))
        self.assertEqual(len(payload["records"]), 1)
        self.assert_unknown(payload["records"][0])
        self.assertEqual(payload["records"][0]["model"], "synthetic-model")
        self.assertEqual(payload["records"][0]["vendor"], "synthetic-vendor")

    def test_error_outside_already_failed_turn_is_a_separate_unknown(self):
        payload = self.payload(self.receipt([thread(), started(),
            {"type": "turn.failed", "error": PRIVATE}, error()]))
        self.assertEqual(len(payload["records"]), 2,
                         "an error outside the active turn has its own receipt")
        for record in payload["records"]:
            self.assert_unknown(record)
        self.assertEqual(len({r["id"] for r in payload["records"]}), 2)

    def test_error_and_success_receipts_can_be_deduplicated_by_ledger(self):
        payload = self.payload(self.receipt([thread(), started(), error(), completed()]))
        records = payload["records"]
        report = self.payload(self.cli("--task-ledger", self.ledger(records + records), "--json"))
        self.assertEqual(report["measured_tokens"], 14)
        self.assertEqual(report["unknown_records"], 1)

    def test_known_money_for_unknown_tokens_remains_visible_in_markdown(self):
        for kind in ("actual", "estimate"):
            with self.subTest(kind=kind):
                record = {"id": "unknown-money", "role": "reviewer",
                    "vendor": "synthetic-vendor", "model": "synthetic-model",
                    "platform": "codex", "access": "api", "status": "unknown",
                    "source": "synthetic-review", "tokens": None,
                    "reason": "usage unsupported", "money": {
                        "amount": "0.25", "currency": "USD", "kind": kind,
                        "source": "synthetic independent receipt", "as_of": "2026-10-11"}}
                path = self.ledger([record])
                result = self.cli("--task-ledger", path)
                self.assertEqual(result.returncode, 3)
                for value in ("0.25", "USD", kind):
                    self.assertIn(value, result.stdout)
                report = self.payload(self.cli("--task-ledger", path, "--json"))
                self.assertIsNone(report["rows"][0]["total"])
                self.assertIsNone(report["rows"][0]["measured_total"])

    def test_explicit_empty_or_whitespace_identity_metadata_is_rejected(self):
        for flag in ("--model", "--vendor", "--role", "--run-id"):
            for value in ("", " ", "\t\n"):
                with self.subTest(flag=flag, value=repr(value)):
                    self.assert_bad(self.receipt([thread(), started(), completed()],
                                                extra=(flag, value)))

    def test_omitted_optional_identity_remains_valid_unknown(self):
        payload = self.payload(self.receipt([thread(), started(), completed()]), code=0)
        record = payload["records"][0]
        self.assertEqual(record["model"], "unknown")
        self.assertEqual(record["vendor"], "unknown")
        self.assertEqual(record["access"], "unknown")
        report = self.payload(self.cli("--task-ledger", self.ledger([record]), "--json"), code=0)
        self.assertEqual(report["measured_tokens"], 14)


if __name__ == "__main__":
    unittest.main()
