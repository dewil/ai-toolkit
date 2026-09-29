"""Проверки готовности и обрыва списка для mymeet-snapshot (без сети)."""

import contextlib
import importlib.util
import io
import json
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mymeet-snapshot.py"
SPEC = importlib.util.spec_from_file_location("mymeet_pull_guard", SCRIPT)
mymeet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mymeet)

CFG = {
    "meetings_root": "Встречи",
    "review_file": "Встречи/_review.txt",
    "rules": [{"match": ["дейли"], "dest": "{YYYY}"}],
}


def meeting(mid, status=None):
    m = {"id": mid, "name": "дейли " + mid, "date": "2026-09-30T10:00:00"}
    if status is not None:
        m["status"] = status
    return m


def run(root, argv, meetings, download):
    out = io.StringIO()
    with patch.object(mymeet, "PROJECT_ROOT", root), \
         patch.object(mymeet, "load_auth", return_value={"api_key": "k"}), \
         patch.object(mymeet, "load_project_config", return_value=CFG), \
         patch.object(mymeet, "iter_meetings", side_effect=lambda auth: meetings), \
         patch.object(mymeet, "download_md", side_effect=download), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = mymeet.main(argv)
    return code, out.getvalue()


def test_only_explicitly_ready_statuses_are_downloaded():
    for status in ("queued", "processing", "in_progress", "pending", "failed", "error", None):
        for argv in (["--pull", "m1"], ["--all"]):
            with tempfile.TemporaryDirectory() as tmp:
                called = []
                code, output = run(Path(tmp), argv, iter([meeting("m1", status)]),
                                   lambda auth, mid: called.append(mid))
                assert code == 0
                assert "не готова: m1, статус " in output
                assert called == []
                assert list(Path(tmp).rglob("*.md")) == []


def test_page_failure_preserves_downloaded_meeting_and_reports_partial():
    def pages():
        yield meeting("m1", "processed")
        raise urllib.error.HTTPError("http://example.invalid", 401, "Unauthorized", {}, None)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        code, output = run(root, ["--all"], pages(),
                           lambda auth, mid: "**Транскрипт:**\n0:00:00 запись\n")
        assert code == 1
        assert "ЧАСТИЧНО:" in output
        assert "список встреч:" in output
        assert "OK:" not in output
        index = json.loads((root / "Встречи" / "_mymeet-index.json").read_text())
        assert "m1" in index["meetings"]
        assert (root / index["meetings"]["m1"]["file"]).exists()


# CI запускает файлы напрямую (python3 tests/test_*.py), а функции pytest
# unittest не видит - без обертки файл молча проходил бы с нулем тестов.
import unittest


class PullGuard(unittest.TestCase):
    """Требование: INV-TRK-MYMEET"""

    def test_only_explicitly_ready_statuses_are_downloaded(self):
        test_only_explicitly_ready_statuses_are_downloaded()

    def test_page_failure_preserves_downloaded_meeting_and_reports_partial(self):
        test_page_failure_preserves_downloaded_meeting_and_reports_partial()


if __name__ == "__main__":
    unittest.main()
