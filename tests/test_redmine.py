#!/usr/bin/env python3
"""Тесты redmine-deltas.py и redmine-snapshot.py: "пусто" отличается от "недоступно".
stdlib-only (unittest). Запуск: python3 tests/test_redmine.py

Живого Redmine нет: сеть подменена, снимки собраны руками по формату, который
пишет redmine-snapshot.py (users -> {name, total, issues[]}). Скрипты копируются
во временное дерево scripts/, потому что корень проекта они выводят из своего
расположения, и боевой .redmine-snapshot.json им не виден.

Требование: INV-TRK-11
"""
from __future__ import annotations

import builtins
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

SNAP_NAME = "_redmine-snapshot.json"
PREV_NAME = "_redmine-snapshot.prev.json"
URL = "https://rm.example.com"


def _issue(iid: int, uid: int, subject: str, status: str = "New") -> dict:
    return {
        "id": iid, "tracker": "Bug", "status": status, "subject": subject,
        "fixed_version": "", "category": "", "parent": None, "priority": "Normal",
        "author_id": 1, "assigned_to_id": uid,
        "updated_on": "2026-09-29T10:00:00Z", "created_on": "2026-09-01T10:00:00Z",
    }


def _snapshot(buckets: dict[int, list[dict]], names: dict[int, str] | None = None) -> dict:
    """buckets: {user_id: [issue, ...]} - формат users-> {name,total,issues}."""
    names = names or {2551: "Иванов", 2982: "Петров"}
    return {
        "generated_at": "2026-09-29T21:00:00+00:00",
        "redmine_url": URL,
        "project_id": 123,
        "users": {
            str(uid): {"name": names.get(uid, str(uid)), "total": len(iss), "issues": iss}
            for uid, iss in buckets.items()
        },
    }


class _Project:
    """Временный проект: копии скриптов, конфиг с абсолютным tasks_root."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        for name in ("redmine-deltas.py", "redmine-snapshot.py"):
            shutil.copy(SCRIPTS / name, self.root / "scripts" / name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        (self.root / ".redmine-snapshot.json").write_text(json.dumps({
            "tasks_root": str(self.tasks), "project_id": 123,
            "users": {"2551": "Иванов", "2982": "Петров"},
        }), encoding="utf-8")

    def close(self):
        self._tmp.cleanup()

    @property
    def snap(self) -> Path:
        return self.tasks / SNAP_NAME

    @property
    def prev(self) -> Path:
        return self.tasks / PREV_NAME

    def put(self, path: Path, data: dict):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def deltas(self) -> tuple[int, str]:
        r = subprocess.run(
            [sys.executable, str(self.root / "scripts" / "redmine-deltas.py")],
            cwd=self.root, capture_output=True, text=True, timeout=60,
        )
        return r.returncode, r.stdout + "\n" + r.stderr

    def load_snapshot_module(self):
        p = self.root / "scripts" / "redmine-snapshot.py"
        spec = importlib.util.spec_from_file_location("redmine_snapshot_under_test", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _group_of(out: str, needle: str) -> str | None:
    """Ближайший выше строки с needle заголовок группы: closed / left / new / status / assignee."""
    lines = out.splitlines()
    idx = next((i for i, ln in enumerate(lines) if needle in ln), None)
    if idx is None:
        return None
    for j in range(idx, -1, -1):
        low = lines[j].lower()
        if "ушла из наблюдения" in low or "ушли из наблюдения" in low:
            return "left"
        if "закрыт" in low:
            return "closed"
        if "нов" in low and j != idx:
            return "new"
        if "статус" in low and j != idx:
            return "status"
        if "исполнител" in low and j != idx:
            return "assignee"
    return None


class DeltasUnavailableVsEmpty(unittest.TestCase):
    """Требование: INV-TRK-11 - недоступные данные не выдаются за пустой результат."""

    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.close)

    def test_no_snapshot_is_error(self):
        """Требование: INV-TRK-11 (1) - нет снимка: код != 0, "снимка нет: <путь>", без "Изменений нет"."""
        rc, out = self.p.deltas()
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("Изменений нет", out)
        self.assertIn("снимка нет", out.lower())
        self.assertIn(str(self.p.tasks), out)

    def test_no_snapshot_even_if_prev_exists(self):
        """Требование: INV-TRK-11 (1) - prev без текущего снимка - все равно "снимка нет"."""
        self.p.put(self.p.prev, _snapshot({2551: [_issue(101, 2551, "Альфа")]}))
        rc, out = self.p.deltas()
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("Изменений нет", out)
        self.assertIn("снимка нет", out.lower())

    def test_no_prev_has_own_code_and_message(self):
        """Требование: INV-TRK-11 (2) - нет prev: отдельный ненулевой код и сообщение про базу сравнения."""
        rc_missing, _ = self.p.deltas()
        self.p.put(self.p.snap, _snapshot({2551: [_issue(101, 2551, "Уникальная-Тема-Х")]}))
        rc, out = self.p.deltas()
        self.assertNotEqual(rc, 0, out)
        self.assertNotEqual(rc, rc_missing, "код 'нет prev' должен отличаться от кода 'нет снимка'")
        low = out.lower()
        self.assertIn("база для сравнения отсутствует", low)
        self.assertIn("дельты не посчитаны", low)
        self.assertNotIn("Изменений нет", out)

    def test_no_prev_does_not_list_new_tasks(self):
        """Требование: INV-TRK-11 (2) - без prev все задачи не объявляются новыми."""
        self.p.put(self.p.snap, _snapshot({2551: [_issue(101, 2551, "Уникальная-Тема-Х")],
                                           2982: [_issue(102, 2982, "Уникальная-Тема-Y")]}))
        rc, out = self.p.deltas()
        self.assertNotIn("Уникальная-Тема-Х", out)
        self.assertNotIn("Уникальная-Тема-Y", out)
        self.assertNotIn("/issues/101", out)
        self.assertNotIn("/issues/102", out)

    def test_identical_snapshots_still_say_no_changes(self):
        """Требование: INV-TRK-11 - настоящая пустота (оба снимка есть, различий нет) остается "Изменений нет", код 0."""
        data = _snapshot({2551: [_issue(101, 2551, "Альфа")]})
        self.p.put(self.p.snap, data)
        self.p.put(self.p.prev, data)
        rc, out = self.p.deltas()
        self.assertEqual(rc, 0, out)
        self.assertIn("Изменений нет", out)


class DeltasLeftObservation(unittest.TestCase):
    """Требование: INV-TRK-11 - уход к ненаблюдаемому исполнителю не равен закрытию."""

    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.close)

    def test_reassign_to_outsider_is_left_not_closed(self):
        """Требование: INV-TRK-11 (3) - смена исполнителя на не из users: группа "ушла из наблюдения", не "закрыта"."""
        self.p.put(self.p.prev, _snapshot({2551: [_issue(101, 2551, "Задача-Ушедшая")]}))
        # в новом снимке задача несет исполнителя вне users конфига
        self.p.put(self.p.snap, _snapshot({2551: [_issue(101, 9999, "Задача-Ушедшая")]}))
        rc, out = self.p.deltas()
        self.assertEqual(rc, 0, out)
        self.assertIn("ушла из наблюдения", out.lower())
        self.assertEqual(_group_of(out, "Задача-Ушедшая"), "left", out)

    def test_real_close_stays_closed(self):
        """Требование: INV-TRK-11 (3) - контроль: задача, пропавшая совсем, по-прежнему "закрыта"."""
        self.p.put(self.p.prev, _snapshot({2551: [_issue(101, 2551, "Задача-Закрытая"),
                                                  _issue(103, 2551, "Задача-Остается")]}))
        self.p.put(self.p.snap, _snapshot({2551: [_issue(103, 2551, "Задача-Остается")]}))
        rc, out = self.p.deltas()
        self.assertEqual(rc, 0, out)
        self.assertEqual(_group_of(out, "Задача-Закрытая"), "closed", out)

    def test_reassign_between_observed_is_not_left(self):
        """Требование: INV-TRK-11 (3) - контроль: переход между наблюдаемыми не "ушла из наблюдения"."""
        self.p.put(self.p.prev, _snapshot({2551: [_issue(101, 2551, "Задача-Своя")], 2982: []}))
        self.p.put(self.p.snap, _snapshot({2551: [], 2982: [_issue(101, 2982, "Задача-Своя")]}))
        rc, out = self.p.deltas()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("ушла из наблюдения", out.lower())
        self.assertNotEqual(_group_of(out, "Задача-Своя"), "closed", out)


class _FailingWrites:
    """Имитация сбоя диска: любая запись в файл под dir пишет половину данных и падает OSError."""

    def __init__(self, directory: Path):
        self.dir = str(directory.resolve())

    def _under(self, file) -> bool:
        try:
            if isinstance(file, int):
                target = os.readlink(f"/proc/self/fd/{file}")
            else:
                target = str(Path(os.fspath(file)).resolve())
        except (OSError, TypeError, ValueError):
            return False
        return target == self.dir or target.startswith(self.dir + os.sep)

    def wrap(self, real_open):
        outer = self

        def fake_open(file, mode="r", *a, **kw):
            fh = real_open(file, mode, *a, **kw)
            if any(c in mode for c in "wxa+") and outer._under(file):
                return _Broken(fh)
            return fh
        return fake_open


class _Broken:
    def __init__(self, fh):
        self._fh = fh

    def write(self, data):
        half = data[: len(data) // 2]
        self._fh.write(half)
        self._fh.flush()
        raise OSError(28, "No space left on device (test)")

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._fh.close()
        except Exception:
            pass
        return False


class SnapshotWriteIsAtomic(unittest.TestCase):
    """Требование: INV-TRK-11 - снимок пишется атомарно, prev обновляется только после успеха."""

    OLD_SNAP = _snapshot({2551: [_issue(101, 2551, "Старый-снимок")]})
    OLD_PREV = _snapshot({2551: [_issue(101, 2551, "Старый-prev")]})

    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.close)
        self.mod = self.p.load_snapshot_module()
        self.mod.load_auth = lambda: {"redmine_url": URL, "api_key": "k", "use_curl": False}
        self.mod.fetch_user_issues = lambda auth, pid, uid: [{
            "id": 500 + int(uid) % 10, "subject": "Свежая", "status": {"id": 1, "name": "New"},
            "priority": {"id": 2, "name": "Normal"}, "tracker": {"id": 1, "name": "Bug"},
            "project": {"id": 123, "name": "P"}, "author": {"id": 1, "name": "A"},
            "assigned_to": {"id": int(uid), "name": "X"},
            "updated_on": "2026-09-30T10:00:00Z", "created_on": "2026-09-30T09:00:00Z",
        }]
        self.p.put(self.p.snap, self.OLD_SNAP)
        self.p.put(self.p.prev, self.OLD_PREV)

    def _run_silenced(self):
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            return self.mod.main()

    def test_write_failure_keeps_snapshot_and_prev(self):
        """Требование: INV-TRK-11 (4) - сбой записи нового снимка: прежний снимок и prev байт-в-байт нетронуты."""
        before_snap = self.p.snap.read_bytes()
        before_prev = self.p.prev.read_bytes()
        failing = _FailingWrites(self.p.tasks)
        with mock.patch.object(builtins, "open", failing.wrap(builtins.open)), \
                mock.patch("io.open", failing.wrap(io.open)):
            try:
                self._run_silenced()
            except (Exception, SystemExit):
                pass
        self.assertEqual(self.p.snap.read_bytes(), before_snap, "прежний снимок испорчен")
        self.assertEqual(self.p.prev.read_bytes(), before_prev, "prev затронут до успешной записи")

    def test_success_rotates_prev_and_writes_new(self):
        """Требование: INV-TRK-11 (4) - контроль: после успешного прогона в prev лежит прежний снимок, в снимке новый."""
        rc = self._run_silenced()
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.p.prev.read_text(encoding="utf-8")), self.OLD_SNAP)
        new = json.loads(self.p.snap.read_text(encoding="utf-8"))
        subjects = [i["subject"] for u in new["users"].values() for i in u["issues"]]
        self.assertIn("Свежая", subjects)

    def test_success_leaves_no_temp_files(self):
        """Требование: INV-TRK-11 (4) - после успешной записи в tasks_root только снимок и prev, без временных остатков."""
        self._run_silenced()
        self.assertEqual({f.name for f in self.p.tasks.iterdir()}, {SNAP_NAME, PREV_NAME})


if __name__ == "__main__":
    unittest.main()
