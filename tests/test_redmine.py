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


class SnapshotPartialFailure(unittest.TestCase):
    """REDMINE-FAIL-CLOSED: сбой второго исполнителя после успешного первого."""

    def test_second_user_failure_preserves_existing_pair(self):
        self.check_failure(existing=True)

    def test_second_user_failure_creates_no_first_pair(self):
        self.check_failure(existing=False)

    def check_failure(self, existing):
        from urllib.error import HTTPError, URLError
        from urllib.parse import parse_qs, urlparse
        for error in (HTTPError(URL, 503, "test service unavailable", {}, None),
                      URLError("test connection lost")):
            with self.subTest(error=type(error).__name__):
                p = _Project()
                self.addCleanup(p.close)
                if existing:
                    p.put(p.snap, _snapshot({2551: [_issue(101, 2551, "Снимок")]}))
                    p.put(p.prev, _snapshot({2551: [_issue(102, 2551, "База")]}))
                before = {path: path.read_bytes() if path.exists() else None for path in (p.snap, p.prev)}
                module = p.load_snapshot_module()
                seen = []

                def response(request, *args, **kwargs):
                    url = request if isinstance(request, str) else request.full_url
                    uid = parse_qs(urlparse(url).query)["assigned_to_id"][0]
                    seen.append(uid)
                    if uid == "2982":
                        raise error
                    self.assertEqual(uid, "2551")
                    data = {"issues": [{"id": 101, "subject": "Загружена",
                            "status": {"id": 1, "name": "New", "is_closed": False},
                            "assigned_to": {"id": 2551, "name": "Иванов"},
                            "priority": {"name": "Normal"}, "tracker": {"name": "Bug"},
                            "author": {"id": 1}, "created_on": "2026-09-29", "updated_on": "2026-09-30"}],
                            "total_count": 1, "offset": 0, "limit": 100}
                    return io.BytesIO(json.dumps(data).encode())

                err = io.StringIO()
                with mock.patch.object(module, "load_auth", return_value={
                        "redmine_url": URL, "api_key": "TEST", "use_curl": False}), \
                        mock.patch("urllib.request.urlopen", side_effect=response), \
                        mock.patch("socket.socket.connect", side_effect=AssertionError("Сеть запрещена")), \
                        mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
                    try:
                        code = module.main()
                    except SystemExit as exc:
                        code = exc.code
                self.assertEqual(seen, ["2551", "2982"])
                self.assertEqual(code, 1, err.getvalue())
                self.assertIn(str(error.reason), err.getvalue())
                after = {path: path.read_bytes() if path.exists() else None for path in before}
                self.assertEqual(after, before)


class SnapshotRootContract(unittest.TestCase):
    """REDMINE-ROOT: сбор и чтение одной пары независимо от cwd."""

    def test_absolute_normalized_root(self):
        self.check_root("absolute")

    def test_tilde_root(self):
        self.check_root("tilde")

    def test_relative_root_from_script_project(self):
        self.check_root("relative")

    def check_root(self, kind):
        p = _Project()
        self.addCleanup(p.close)
        home = p.root / "home"
        cwd = p.root / "elsewhere"
        home.mkdir()
        cwd.mkdir()
        target = (home if kind == "tilde" else p.root) / "storage"
        # Промежуточный каталог существует и для ОС, не нормализующей '..' сама.
        target.mkdir()
        (target / "nested").mkdir()
        raw = {"absolute": str(target / "nested/.."), "tilde": "~/storage/nested/..",
               "relative": "storage/nested/.."}[kind]
        cfg = p.root / ".redmine-snapshot.json"
        config = json.loads(cfg.read_text())
        config["tasks_root"] = raw
        cfg.write_text(json.dumps(config))
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(cwd)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            module = p.load_snapshot_module()
            with mock.patch.object(module, "load_auth", return_value={
                    "redmine_url": URL, "api_key": "TEST", "use_curl": False}), \
                    mock.patch.object(module, "fetch_user_issues", return_value=[]), \
                    mock.patch("socket.socket.connect", side_effect=AssertionError("Сеть запрещена")), \
                    mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(module.main(), 0)
                first = (target / SNAP_NAME).read_bytes()
                self.assertEqual(module.main(), 0)
            self.assertEqual((target / PREV_NAME).read_bytes(), first)
            result = subprocess.run([sys.executable, str(p.root / "scripts/redmine-deltas.py")],
                                    cwd=cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Изменений нет", result.stdout)
        for name in (SNAP_NAME, PREV_NAME):
            self.assertEqual(list(p.root.rglob(name)), [target / name])


def _deltas_stdout(project):
    result = subprocess.run(
        [sys.executable, str(project.root / "scripts/redmine-deltas.py")],
        cwd=project.root, capture_output=True, text=True, timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


class DeltasPositiveClasses(unittest.TestCase):
    """REDMINE-CLASSES: проверяем ссылки внутри каждой требуемой группы."""

    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.close)

    def output(self, before, after):
        self.p.put(self.p.prev, _snapshot(before))
        self.p.put(self.p.snap, _snapshot(after))
        code, out, err = _deltas_stdout(self.p)
        self.assertEqual(code, 0, err)
        return out

    def group(self, out, title):
        # Markdown-заголовки или отдельные жирные строки; не захватываем соседнюю группу.
        match = re.search(r"^(?:#+ |\*\*)[^\n]*" + re.escape(title)
                          + r"[^\n]*\n(.*?)(?=^(?:#+ |\*\*)|\Z)",
                          out, re.M | re.S)
        self.assertIsNotNone(match, out)
        return match.group(1)

    def assert_task(self, group):
        self.assertIn(f"{URL}/issues/101", group)
        self.assertIn("Задача-Контракт", group)

    def test_new_task_group(self):
        out = self.output({2551: []}, {2551: [_issue(101, 2551, "Задача-Контракт")]})
        self.assert_task(self.group(out, "Новые задачи"))

    def test_status_change_group(self):
        self.check_change(status=True, assignee=False)

    def test_assignee_change_group(self):
        self.check_change(status=False, assignee=True)

    def test_simultaneous_status_and_assignee_change_in_both_groups(self):
        self.check_change(status=True, assignee=True)

    def check_change(self, status, assignee):
        uid = 2982 if assignee else 2551
        old = _issue(101, 2551, "Задача-Контракт", "New")
        new = _issue(101, uid, "Задача-Контракт", "In Progress" if status else "New")
        after = {2551: [], 2982: []}
        after[uid] = [new]
        out = self.output({2551: [old], 2982: []}, after)
        for title, values, enabled in (("Смена статуса", ("New", "In Progress"), status),
                                       ("Смена исполнителя", ("Иванов", "Петров"), assignee)):
            if enabled:
                group = self.group(out, title)
                self.assert_task(group)
                for value in values:
                    self.assertIn(value, group)
        self.assertNotIn("Ушла из наблюдения", out)
        self.assertNotIn("Закрыты", out)

    def test_explicit_is_closed_transition(self):
        old = dict(_issue(101, 2551, "Задача-Контракт"), is_closed=False)
        new = dict(old, is_closed=True, status="Closed")
        out = self.output({2551: [old]}, {2551: [new]})
        self.assert_task(self.group(out, "Закрыты"))
        self.assertNotIn("ушла из наблюдения", out.lower())
        self.assertNotIn("ушли из наблюдения", out.lower())


class DeltasCollectionTitle(unittest.TestCase):
    """DELTA-TITLE: интервал между сборами, включая два сбора за день."""

    def test_same_day_collections(self):
        self.check_title("2026-09-30T09:00:00+00:00", "2026-09-30T14:00:00+00:00")

    def test_collections_several_days_apart(self):
        self.check_title("2026-09-24T09:00:00+00:00", "2026-09-30T14:00:00+00:00")

    def check_title(self, previous, current):
        p = _Project()
        self.addCleanup(p.close)
        for path, timestamp in ((p.prev, previous), (p.snap, current)):
            data = _snapshot({2551: [_issue(101, 2551, "Без изменений")]})
            data["generated_at"] = timestamp
            p.put(path, data)
        code, out, err = _deltas_stdout(p)
        self.assertEqual(code, 0, err)
        self.assertIn(previous, out)
        self.assertIn(current, out)
        self.assertIn("Изменений нет", out)
        self.assertIn("Дельты с прошлого сбора", out)
        self.assertNotIn("Дельты со вчера", out)
        self.assertNotIn("Дельты между снимками", out)


if __name__ == "__main__":
    unittest.main()
