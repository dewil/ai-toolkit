#!/usr/bin/env python3
"""Тесты asana-project.py - разворачивание доски Asana из JSON-плана.
stdlib-only (unittest), сеть подменяется заглушками.

Запуск: python3 tests/test_asana_project.py

Ключевые инварианты: dry-run показывает ровно то, что сделает --send (общий
plan_effect); идемпотентность по имени задачи (повторный прогон и дубль внутри
плана не плодят задачи); срок подтягивается к плану, но не стирается; на
большой доске дедуп видит все задачи (пагинация next_page).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("asana_project", SCRIPTS / "asana-project.py")
ap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap)


class Recorder:
    """Заглушка call/get_all: помнит вызовы, отдает заготовленное состояние доски."""

    def __init__(self, sections=None, tasks=None):
        self.calls: list[tuple] = []
        self.sections = sections or []
        self.tasks = tasks or []
        self._n = 0

    def call(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        self._n += 1
        return {"gid": f"new{self._n}"}

    def get_all(self, path, token):
        self.calls.append(("GETALL", path, None))
        return self.sections if "/sections" in path else self.tasks

    def sent(self, method, path_part):
        return [c for c in self.calls if c[0] == method and path_part in c[1]]


@contextlib.contextmanager
def patched(rec: Recorder, plan: dict):
    """Подменяет сеть и токен, кладет план во временный файл."""
    with tempfile.TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        orig = (ap.call, ap.get_all, ap.load_token)
        ap.call, ap.get_all, ap.load_token = rec.call, rec.get_all, lambda a: "T"
        try:
            yield plan_path
        finally:
            ap.call, ap.get_all, ap.load_token = orig


def run_tasks(rec, plan, send):
    with patched(rec, plan) as plan_path:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ap.cmd_tasks(argparse.Namespace(auth=None, plan=str(plan_path), send=send))
    return out.getvalue()


def run_create(rec, plan, send):
    with patched(rec, plan) as plan_path:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ap.cmd_create(argparse.Namespace(auth=None, plan=str(plan_path), send=send))
    return out.getvalue()


def tasks_plan(*tasks, section="Этап 1"):
    return {"project": "P1", "sections": [{"name": section, "tasks": list(tasks)}]}


BOARD_X_AUG1 = dict(
    sections=[{"name": "Этап 1", "gid": "s1"}],
    tasks=[{"name": "X", "gid": "t1", "due_on": "2026-08-01"}],
)


class TokenLoading(unittest.TestCase):
    @contextlib.contextmanager
    def env(self, value):
        orig = os.environ.get("ASANA_TOKEN")
        os.environ["ASANA_TOKEN"] = value
        try:
            yield
        finally:
            if orig is None:
                os.environ.pop("ASANA_TOKEN", None)
            else:
                os.environ["ASANA_TOKEN"] = orig

    def test_auth_flag_beats_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.json"
            p.write_text('{"token": "FROM-FILE"}', encoding="utf-8")
            with self.env("FROM-ENV"):
                self.assertEqual(ap.load_token(str(p)), "FROM-FILE")

    def test_env_beats_default_file(self):
        with self.env("FROM-ENV"):
            self.assertEqual(ap.load_token(None), "FROM-ENV")

    def test_missing_auth_file_exits(self):
        with self.assertRaises(SystemExit):
            ap.load_token("/nope/auth.json")

    def test_auth_file_without_token_field_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.json"
            p.write_text("{}", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    ap.load_token(str(p))


class Pagination(unittest.TestCase):
    """get_all обязан разматывать next_page: усеченный список задач ломает
    дедуп по имени, и повторный прогон плодит дубли молча."""

    def with_pages(self, pages, path):
        seen_urls = []

        def fake_request(method, url, token, payload=None):
            seen_urls.append(url)
            return pages[len(seen_urls) - 1]

        orig = ap._request
        ap._request = fake_request
        try:
            items = ap.get_all(path, "T")
        finally:
            ap._request = orig
        return items, seen_urls

    def test_unwinds_next_page(self):
        items, urls = self.with_pages(
            [
                {"data": [{"name": "A"}, {"name": "B"}], "next_page": {"uri": "https://x/page2"}},
                {"data": [{"name": "C"}], "next_page": None},
            ],
            "/projects/P1/tasks?opt_fields=name,due_on",
        )
        self.assertEqual([i["name"] for i in items], ["A", "B", "C"])
        self.assertEqual(urls[1], "https://x/page2")
        # у пути уже есть query - limit дописывается через &
        self.assertIn("opt_fields=name,due_on&limit=100", urls[0])

    def test_limit_appended_without_query(self):
        _, urls = self.with_pages([{"data": [], "next_page": None}], "/projects/P1/sections")
        self.assertIn("/sections?limit=100", urls[0])


class TasksDrySendParity(unittest.TestCase):
    def test_removed_due_is_not_cleared_and_dry_run_says_skip(self):
        """Блокер-класс из md-pdf: dry-run обещал изменение, send его не делал.
        В плане у задачи нет даты, на доске есть - это ПРОПУСК, не СРОК."""
        dry = run_tasks(Recorder(**BOARD_X_AUG1), tasks_plan({"name": "X"}), send=False)
        self.assertIn("ПРОПУСК", dry)
        self.assertNotIn("СРОК", dry)

        rec = Recorder(**BOARD_X_AUG1)
        run_tasks(rec, tasks_plan({"name": "X"}), send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])
        self.assertEqual(rec.sent("POST", "/tasks"), [])

    def test_due_change_shown_and_sent(self):
        plan = tasks_plan({"name": "X", "due_on": "2026-08-10"})
        dry = run_tasks(Recorder(**BOARD_X_AUG1), plan, send=False)
        self.assertIn("СРОК: 2026-08-01 -> 2026-08-10", dry)

        rec = Recorder(**BOARD_X_AUG1)
        run_tasks(rec, plan, send=True)
        puts = rec.sent("PUT", "/tasks/t1")
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][2], {"due_on": "2026-08-10"})


class TasksIdempotency(unittest.TestCase):
    def test_same_task_same_due_skipped(self):
        rec = Recorder(**BOARD_X_AUG1)
        out = run_tasks(rec, tasks_plan({"name": "X", "due_on": "2026-08-01"}), send=True)
        self.assertEqual(rec.sent("POST", "/tasks"), [])
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])
        self.assertIn("без изменений 1", out)

    def test_duplicate_name_in_plan_rejected_before_any_write(self):
        """Блокер ревью: send сводил две разных строки плана в одну задачу
        (вторая молча становилась синком срока первой, ее секция/исполнитель/
        notes терялись), а dry-run обещал полноценное создание обеих."""
        rec = Recorder(sections=[{"name": "Этап 1", "gid": "s1"}])
        plan = tasks_plan(
            {"name": "Новая", "assignee": "a@example.com", "due_on": "2026-08-01"},
            {"name": "Новая", "assignee": "b@example.com", "due_on": "2026-08-10"},
        )
        with self.assertRaises(SystemExit) as caught:
            run_tasks(rec, plan, send=True)
        self.assertIn("Новая", str(caught.exception))
        self.assertIn("повторяется", str(caught.exception))
        self.assertEqual(rec.sent("POST", "/tasks"), [], "до записи дойти не должно")

    def test_trailing_space_in_plan_matches_existing_task(self):
        """Asana может триммить имя при создании - хвостовой пробел в плане
        не должен плодить дубль на каждом прогоне."""
        rec = Recorder(**BOARD_X_AUG1)
        run_tasks(rec, tasks_plan({"name": "X ", "due_on": "2026-08-01"}), send=True)
        self.assertEqual(rec.sent("POST", "/tasks"), [])
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_existing_section_reused_missing_created(self):
        rec = Recorder(sections=[{"name": "Этап 1", "gid": "s1"}])
        plan = {"project": "P1", "sections": [
            {"name": "Этап 1", "tasks": [{"name": "A"}]},
            {"name": "Этап 2", "tasks": [{"name": "B"}]},
        ]}
        run_tasks(rec, plan, send=True)
        self.assertEqual(len(rec.sent("POST", "/sections")), 1)
        self.assertEqual(rec.sent("POST", "/sections")[0][2], {"name": "Этап 2"})


class Assignee(unittest.TestCase):
    def test_tasks_sends_assignee_by_email(self):
        rec = Recorder(sections=[{"name": "Этап 1", "gid": "s1"}])
        run_tasks(rec, tasks_plan({"name": "A", "assignee": "user@example.com"}), send=True)
        payload = rec.sent("POST", "/tasks")[0][2]
        self.assertEqual(payload["assignee"], "user@example.com")

    def test_tasks_without_assignee_omits_field(self):
        rec = Recorder(sections=[{"name": "Этап 1", "gid": "s1"}])
        run_tasks(rec, tasks_plan({"name": "A"}), send=True)
        self.assertNotIn("assignee", rec.sent("POST", "/tasks")[0][2])

    def test_create_sends_assignee_too(self):
        """Регресс: create молча терял исполнителей, которые tasks отправлял."""
        rec = Recorder()
        plan = {"workspace": "W", "name": "Доска", "sections": [
            {"name": "Этап 1", "tasks": [{"name": "A", "assignee": "user@example.com"}]},
        ]}
        run_create(rec, plan, send=True)
        self.assertEqual(rec.sent("POST", "/tasks")[0][2]["assignee"], "user@example.com")

    def test_create_dry_run_shows_assignee(self):
        plan = {"workspace": "W", "name": "Доска", "sections": [
            {"name": "Этап 1", "tasks": [{"name": "A", "assignee": "user@example.com"}]},
        ]}
        out = run_create(Recorder(), plan, send=False)
        self.assertIn("user@example.com", out)


class PlanValidation(unittest.TestCase):
    def test_create_without_workspace_exits_with_message(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(err):
                run_create(Recorder(), {"name": "Доска"}, send=False)
        self.assertIn("workspace", str(caught.exception))

    def test_tasks_without_project_exits_with_message(self):
        with self.assertRaises(SystemExit) as caught:
            run_tasks(Recorder(), {"sections": []}, send=False)
        self.assertIn("project", str(caught.exception))

    def test_bad_due_format_rejected_before_any_write(self):
        """Кривая дата проходила dry-run и роняла send посреди заливки."""
        rec = Recorder(sections=[{"name": "Этап 1", "gid": "s1"}])
        with self.assertRaises(SystemExit) as caught:
            run_tasks(rec, tasks_plan({"name": "A", "due_on": "01.08.2026"}), send=True)
        self.assertIn("YYYY-MM-DD", str(caught.exception))
        self.assertEqual(rec.sent("POST", "/tasks"), [])

    def test_task_without_name_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            run_tasks(Recorder(), tasks_plan({"notes": "безымянная"}), send=True)
        self.assertIn("без имени", str(caught.exception))

    def test_non_string_names_rejected_before_any_write(self):
        """Блокер раунда 2: int-имя проходило валидацию через str(), а send
        падал на .strip() посреди заливки - AttributeError мимо recovery."""
        rec = Recorder()
        plan = {"project": "P1", "sections": [
            {"name": 123, "tasks": [{"name": "A"}]},
            {"name": "Этап 1", "tasks": [{"name": 456}]},
        ]}
        with self.assertRaises(SystemExit) as caught:
            run_tasks(rec, plan, send=True)
        msg = str(caught.exception)
        self.assertIn("123", msg)
        self.assertIn("456", msg)
        self.assertEqual([c for c in rec.calls if c[0] != "GETALL"], [])

    def test_impossible_calendar_date_rejected(self):
        """30 февраля проходило regex и уронило бы send на API посреди заливки."""
        with self.assertRaises(SystemExit) as caught:
            run_tasks(Recorder(), tasks_plan({"name": "A", "due_on": "2026-02-30"}), send=True)
        self.assertIn("2026-02-30", str(caught.exception))

    def test_missing_plan_file_exits_cleanly(self):
        with patched(Recorder(), {}):
            with self.assertRaises(SystemExit) as caught:
                ap.cmd_tasks(argparse.Namespace(auth=None, plan="/nope/plan.json", send=False))
        self.assertIn("Нет файла плана", str(caught.exception))


class HonestPreview(unittest.TestCase):
    """Блокер ревью: одноименная ручная задача на доске. Send подтянет ей
    только срок - dry-run не должен обещать исполнителя и секцию из плана."""

    def test_update_line_does_not_promise_assignee(self):
        plan = tasks_plan({"name": "X", "assignee": "alice@example.com",
                           "due_on": "2026-08-10"})
        dry = run_tasks(Recorder(**BOARD_X_AUG1), plan, send=False)
        line = next(l for l in dry.splitlines() if "СРОК" in l)
        self.assertNotIn("alice@example.com", line)
        self.assertIn("остальное из плана не применяется", line)

    def test_repeated_section_second_occurrence_marked_existing(self):
        """Секция дважды в плане: send создаст один раз - dry-run не должен
        дважды обещать создание."""
        rec = Recorder()
        plan = {"project": "P1", "sections": [
            {"name": "Этап 9", "tasks": [{"name": "A"}]},
            {"name": "Этап 9", "tasks": [{"name": "B"}]},
        ]}
        dry = run_tasks(rec, plan, send=False)
        marks = [l for l in dry.splitlines() if "[Этап 9]" in l]
        self.assertIn("будет создана", marks[0])
        self.assertIn("есть", marks[1])

        rec2 = Recorder()
        run_tasks(rec2, plan, send=True)
        self.assertEqual(len(rec2.sent("POST", "/sections")), 1)


class CreateRecovery(unittest.TestCase):
    def test_partial_create_failure_prints_recovery_hint(self):
        """Блокер ревью: create упал на середине - повтор create создал бы
        второй проект. Скрипт обязан сказать, что проект уже есть и как
        доздать через tasks."""

        class FailingRecorder(Recorder):
            def call(self, method, path, token, payload=None):
                if method == "POST" and path == "/tasks" and len(self.sent("POST", "/tasks")) >= 1:
                    self.calls.append((method, path, payload))
                    raise SystemExit("Asana API 400 на POST /tasks: bad assignee")
                return super().call(method, path, token, payload)

        rec = FailingRecorder()
        plan = {"workspace": "W", "name": "Доска", "sections": [
            {"name": "Этап 1", "tasks": [{"name": "A"}, {"name": "B"}]},
        ]}
        err = io.StringIO()
        with patched(rec, plan) as plan_path:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    ap.cmd_create(argparse.Namespace(auth=None, plan=str(plan_path), send=True))
        self.assertIn("НЕ повторяй create", err.getvalue())
        self.assertIn("new1", err.getvalue(), "gid созданного проекта должен быть в подсказке")

    def test_project_post_failure_prints_unknown_outcome_hint(self):
        """Блокер раунда 2: обрыв на самом POST /projects - исход неизвестен
        (проект мог создаться), слепой повтор create дал бы второй проект."""

        class ProjectFails(Recorder):
            def call(self, method, path, token, payload=None):
                self.calls.append((method, path, payload))
                if method == "POST" and path == "/projects":
                    raise SystemExit("Сеть недоступна: timeout")
                return {"gid": "x"}

        rec = ProjectFails()
        plan = {"workspace": "W", "name": "Доска", "sections": []}
        err = io.StringIO()
        with patched(rec, plan) as plan_path:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    ap.cmd_create(argparse.Namespace(auth=None, plan=str(plan_path), send=True))
        self.assertIn("исход неизвестен", err.getvalue())
        self.assertIn("Доска", err.getvalue())


class TokenEdgeCases(unittest.TestCase):
    def test_null_token_treated_as_missing(self):
        """{"token": null} превращался в строку "None" и уходил как Bearer."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.json"
            p.write_text('{"token": null}', encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                ap.load_token(str(p))
        self.assertIn("нет поля token", str(caught.exception))


class CliParsing(unittest.TestCase):
    def test_auth_accepted_after_subcommand(self):
        """Грабля argparse (до 3.13): --auth в главном парсере молча терялся,
        если указан до подкоманды. Теперь флаг живет в подкомандах."""
        import subprocess
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "asana-project.py"),
             "tasks", "--plan", "/nope.json", "--auth", "/nope-auth.json"],
            capture_output=True, text=True,
        )
        self.assertNotIn("unrecognized", r.stderr)
        self.assertIn("Нет файла с токеном", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
