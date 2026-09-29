"""Проверки неполных данных Redmine и отказов при замене снимков."""

import io
import json
from pathlib import Path
import unittest
from unittest import mock

from test_redmine import _Project, _issue, _snapshot


class RedmineEdgeCases(unittest.TestCase):
    def setUp(self):
        self.project = _Project()
        self.addCleanup(self.project.close)
        self.old = _snapshot({2551: [_issue(101, 2551, "Старая задача")]})
        self.older = _snapshot({2551: [_issue(102, 2551, "Прежняя база")]})
        self.project.put(self.project.snap, self.old)
        self.project.put(self.project.prev, self.older)
        self.module = self.project.load_snapshot_module()
        self.module.load_auth = lambda: {
            "redmine_url": "https://rm.example.com", "api_key": "k", "use_curl": False,
        }
        self.real_fetch_user_issues = self.module.fetch_user_issues
        self.module.fetch_user_issues = lambda auth, pid, uid: []

    def run_snapshot(self, response=None):
        response = response if response is not None else OSError("API недоступен")
        kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
        with mock.patch.object(self.module, "fetch_json", **kwargs), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            return self.module.main()

    def test_removed_user_tasks_are_reported_even_when_snapshots_match(self):
        self.project.put(self.project.prev, self.old)
        config = self.project.root / ".redmine-snapshot.json"
        data = json.loads(config.read_text(encoding="utf-8"))
        data["users"].pop("2551")
        config.write_text(json.dumps(data), encoding="utf-8")
        code, output = self.project.deltas()
        self.assertEqual(code, 0, output)
        self.assertIn("выбыли вместе с исполнителем Иванов", output)
        self.assertNotIn("Изменений нет", output)

    def test_bad_refetch_response_keeps_both_files(self):
        before = self.project.snap.read_bytes(), self.project.prev.read_bytes()
        self.assertEqual(self.run_snapshot({"issue": {"id": 101}}), 1)
        self.assertEqual((self.project.snap.read_bytes(), self.project.prev.read_bytes()), before)

    def test_closed_name_without_is_closed_is_unknown(self):
        raw = {
            "id": 101, "subject": "Старая задача", "status": {"name": "Closed"},
            "priority": {"name": "Normal"}, "tracker": {"name": "Bug"},
            "author": {"id": 1}, "assigned_to": {"id": 2551},
            "updated_on": "2026-09-30", "created_on": "2026-09-29",
        }
        self.assertEqual(self.run_snapshot({"issue": raw}), 0)
        current = json.loads(self.project.snap.read_text(encoding="utf-8"))
        issue = current["users"]["2551"]["issues"][0]
        self.assertIsNone(issue["is_closed"])
        code, output = self.project.deltas()
        self.assertEqual(code, 0, output)
        self.assertIn("Статус неизвестен (нет признака закрытия)", output)
        self.assertNotIn("Закрыты", output)

    def test_missing_total_count_rejects_collection_and_keeps_files(self):
        before = self.project.snap.read_bytes(), self.project.prev.read_bytes()
        self.module.fetch_user_issues = self.real_fetch_user_issues
        with mock.patch.object(self.module, "fetch_json", return_value={"issues": []}), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                self.module.main()
            self.assertEqual(stopped.exception.code, 1)
        self.assertEqual((self.project.snap.read_bytes(), self.project.prev.read_bytes()), before)

    def test_failed_prev_replace_keeps_pair(self):
        self._check_replace_failure(self.project.prev)

    def test_failed_current_replace_restores_prev(self):
        self._check_replace_failure(self.project.snap)

    def _check_replace_failure(self, target):
        before = self.project.snap.read_bytes(), self.project.prev.read_bytes()
        real_replace = Path.replace

        def replace(path, destination):
            if destination == target:
                raise OSError("сбой замены")
            return real_replace(path, destination)

        with mock.patch.object(Path, "replace", replace), \
                mock.patch.object(self.module, "fetch_json", side_effect=OSError("API недоступен")), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(OSError):
                self.module.main()
        self.assertEqual((self.project.snap.read_bytes(), self.project.prev.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
