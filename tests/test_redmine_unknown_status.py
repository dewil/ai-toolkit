"""Неудачный дозапрос сохраняет снимок и не объявляет задачу закрытой."""

import io
import json
import unittest
from unittest import mock

from test_redmine import _Project, _issue, _snapshot


class SnapshotUnknownStatus(unittest.TestCase):
    def test_failed_refetch_is_unknown_in_new_snapshot_and_deltas(self):
        project = _Project()
        self.addCleanup(project.close)
        old = _snapshot({2551: [_issue(101, 2551, "Пропавшая задача")]})
        project.put(project.snap, old)
        module = project.load_snapshot_module()
        module.load_auth = lambda: {
            "redmine_url": "https://rm.example.com", "api_key": "k", "use_curl": False,
        }
        module.fetch_user_issues = lambda auth, pid, uid: []

        with mock.patch.object(module, "fetch_json", side_effect=OSError("API недоступен")) as fetch, \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(module.main(), 0)
        fetch.assert_called_once()

        self.assertEqual(json.loads(project.prev.read_text(encoding="utf-8")), old)
        current = json.loads(project.snap.read_text(encoding="utf-8"))
        issue = current["users"]["2551"]["issues"][0]
        self.assertEqual(issue["id"], 101)
        self.assertEqual(issue["status"], "статус неизвестен")
        self.assertTrue(issue["status_unknown"])

        code, output = project.deltas()
        self.assertEqual(code, 0, output)
        self.assertIn("Статус неизвестен (дозапрос не удался)", output)
        self.assertEqual(output.count("Пропавшая задача"), 1, output)
        self.assertNotIn("Закрыты", output)
        self.assertNotIn("Ушла из наблюдения", output)


if __name__ == "__main__":
    unittest.main()
