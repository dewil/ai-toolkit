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
from decimal import Decimal
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
        self.notes: dict[str, str] = {}
        self._n = 0

    def call(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        self._n += 1
        return {"gid": f"new{self._n}"}

    def get_all(self, path, token):
        self.calls.append(("GETALL", path, None))
        # /projects/X/sections - секции, /sections/X/tasks - задачи секции:
        # подстрокой "/sections" их не различить
        base = path.split("?")[0]
        return self.sections if base.endswith("/sections") else self.tasks

    def call_notes(self, method, path, token, payload=None):
        """Вариант call, отвечающий на чтение описания задачи."""
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/tasks/"):
            gid = path.split("/")[2].split("?")[0]
            return {"gid": gid, "html_notes": self.notes.get(gid, "<body></body>")}
        self._n += 1
        return {"gid": f"new{self._n}"}

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


def member(pgid, sgid, sname):
    return [{"project": {"gid": pgid}, "section": {"gid": sgid, "name": sname}}]


def run_cmd(fn, rec, plan=None, notes=None, **ns):
    """Прогон подкоманды на заглушенной сети. Возвращает stdout."""
    if notes:
        rec.notes.update(notes)
    with patched(rec, plan or {}) as plan_path:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(argparse.Namespace(auth=None, plan=str(plan_path), **ns))
    return out.getvalue()


BOARD_MOVE = dict(
    sections=[{"name": "В работе", "gid": "s1"}, {"name": "Отчет", "gid": "s2"}],
    tasks=[
        {"name": "A", "gid": "t1", "memberships": member("P1", "s1", "В работе")},
        {"name": "B", "gid": "t2", "memberships": member("P1", "s2", "Отчет")},
    ],
)


class Move(unittest.TestCase):
    """Перенос существующей задачи в секцию. До этой команды шаг делался
    прямым REST - мимо dry-run, то есть самое рискованное действие (правка
    чужой задачи на общей доске) шло без единственного канала контроля."""

    def plan(self, *names, section="Отчет"):
        return {"project": "P1", "section": section, "tasks": list(names)}

    def test_dry_run_shows_transition_and_writes_nothing(self):
        rec = Recorder(**BOARD_MOVE)
        out = run_cmd(ap.cmd_move, rec, self.plan("A"), send=False)
        self.assertIn("A: В работе -> Отчет", out)
        self.assertEqual(rec.sent("POST", "addTask"), [])

    def test_send_moves(self):
        rec = Recorder(**BOARD_MOVE)
        run_cmd(ap.cmd_move, rec, self.plan("A"), send=True)
        self.assertEqual(rec.sent("POST", "addTask"),
                         [("POST", "/sections/s2/addTask", {"task": "t1"})])

    def test_already_in_target_section_is_no_change(self):
        """Идемпотентность той же модели, что у tasks: повтор ничего не делает."""
        rec = Recorder(**BOARD_MOVE)
        out = run_cmd(ap.cmd_move, rec, self.plan("B"), send=True)
        self.assertIn("без изменений 1", out)
        self.assertEqual(rec.sent("POST", "addTask"), [])

    def test_gid_reference_works(self):
        """gid у Asana числовой - по этому признаку ссылка и отличается от имени."""
        rec = Recorder(sections=BOARD_MOVE["sections"], tasks=[
            {"name": "A", "gid": "1001", "memberships": member("P1", "s1", "В работе")}])
        run_cmd(ap.cmd_move, rec, self.plan("1001"), send=True)
        self.assertEqual(rec.sent("POST", "addTask"),
                         [("POST", "/sections/s2/addTask", {"task": "1001"})])

    def test_unknown_gid_refused(self):
        rec = Recorder(**BOARD_MOVE)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_move, rec, self.plan("999999"), send=True)
        self.assertIn("999999", str(e.exception))
        self.assertEqual(rec.sent("POST", "addTask"), [])

    def test_duplicate_reference_moves_once(self):
        rec = Recorder(**BOARD_MOVE)
        run_cmd(ap.cmd_move, rec, self.plan("A", "A"), send=True)
        self.assertEqual(len(rec.sent("POST", "addTask")), 1)

    def test_ambiguous_name_refuses_with_gids(self):
        """Выбор наугад тут означает перенос не той задачи на общей доске."""
        board = dict(BOARD_MOVE)
        board["tasks"] = BOARD_MOVE["tasks"] + [
            {"name": "A", "gid": "t3", "memberships": member("P1", "s1", "В работе")}]
        rec = Recorder(**board)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_move, rec, self.plan("A"), send=True)
        self.assertIn("несколько задач", str(e.exception))
        self.assertIn("t1", str(e.exception))
        self.assertEqual(rec.sent("POST", "addTask"), [])

    def test_missing_task_refuses_before_write(self):
        rec = Recorder(**BOARD_MOVE)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_move, rec, self.plan("Нет такой"), send=True)
        self.assertIn("нет такой задачи", str(e.exception).lower())
        self.assertEqual(rec.sent("POST", "addTask"), [])

    def test_all_problems_listed_at_once(self):
        rec = Recorder(**BOARD_MOVE)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_move, rec, self.plan("Нет", "Тоже нет"), send=True)
        self.assertIn("Нет", str(e.exception))
        self.assertIn("Тоже нет", str(e.exception))

    def test_missing_section_refuses_and_lists_existing(self):
        """Секцию тут не создаем: промах в имени вероятнее намерения."""
        rec = Recorder(**BOARD_MOVE)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_move, rec, self.plan("A", section="Отчёт"), send=True)
        self.assertIn("Отчет", str(e.exception))
        self.assertEqual(rec.sent("POST", "addTask"), [])

    def test_task_outside_any_section(self):
        board = dict(BOARD_MOVE)
        board["tasks"] = [{"name": "A", "gid": "t1", "memberships": []}]
        rec = Recorder(**board)
        out = run_cmd(ap.cmd_move, rec, self.plan("A"), send=False)
        self.assertIn("вне секций -> Отчет", out)

    def test_section_of_other_project_ignored(self):
        """Задача видна в нескольких проектах; секция берется из нашего."""
        board = dict(BOARD_MOVE)
        board["tasks"] = [{"name": "A", "gid": "t1",
                           "memberships": member("OTHER", "s9", "Чужая")
                                          + member("P1", "s1", "В работе")}]
        rec = Recorder(**board)
        out = run_cmd(ap.cmd_move, rec, self.plan("A"), send=False)
        self.assertIn("A: В работе -> Отчет", out)


BOARD_DONE = dict(
    sections=[{"name": "Отчет", "gid": "s2"}],
    tasks=[
        {"name": "A", "gid": "t1", "completed": False, "memberships": member("P1", "s2", "Отчет")},
        {"name": "B", "gid": "t2", "completed": True, "memberships": member("P1", "s2", "Отчет")},
    ],
)


class Complete(unittest.TestCase):
    """Пакетное закрытие - необратимая операция на общей доске."""

    def test_dry_run_counts_and_writes_nothing(self):
        rec = Recorder(**BOARD_DONE)
        out = run_cmd(ap.cmd_complete, rec, {"project": "P1", "section": "Отчет"}, send=False)
        self.assertIn("БУДЕТ ЗАКРЫТО задач: 1", out)
        self.assertIn("уже закрыто и пропускается: 1", out)
        self.assertIn("необратимо", out)
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_send_closes_only_open(self):
        rec = Recorder(**BOARD_DONE)
        run_cmd(ap.cmd_complete, rec, {"project": "P1", "tasks": ["A", "B"]}, send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"),
                         [("PUT", "/tasks/t1", {"completed": True})])

    def test_repeat_run_is_no_op(self):
        board = dict(BOARD_DONE)
        board["tasks"] = [dict(t, completed=True) for t in BOARD_DONE["tasks"]]
        rec = Recorder(**board)
        out = run_cmd(ap.cmd_complete, rec, {"project": "P1", "section": "Отчет"}, send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])
        self.assertIn("закрывать нечего", out)

    def test_by_task_list(self):
        rec = Recorder(**BOARD_DONE)
        run_cmd(ap.cmd_complete, rec, {"project": "P1", "tasks": ["A"]}, send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"),
                         [("PUT", "/tasks/t1", {"completed": True})])

    def test_both_selectors_refused(self):
        """Неоднозначный план на необратимой операции не исполняется."""
        rec = Recorder(**BOARD_DONE)
        with self.assertRaises(SystemExit):
            run_cmd(ap.cmd_complete, rec,
                    {"project": "P1", "section": "Отчет", "tasks": ["A"]}, send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_no_selector_refused(self):
        rec = Recorder(**BOARD_DONE)
        with self.assertRaises(SystemExit):
            run_cmd(ap.cmd_complete, rec, {"project": "P1"}, send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_unknown_task_refuses_before_any_write(self):
        rec = Recorder(**BOARD_DONE)
        with self.assertRaises(SystemExit):
            run_cmd(ap.cmd_complete, rec, {"project": "P1", "tasks": ["A", "Нет"]}, send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])


class Hours(unittest.TestCase):
    """Сумма часов уходит в денежный документ - молчаливый пропуск строки
    занижает счет и неотличим от честной суммы (rules/silent-failure.md)."""

    def rx(self, pattern=None):
        return ap.compile_hours_re(pattern or ap.HOURS_RE)

    def test_comma_and_dot_decimals(self):
        self.assertEqual(ap.parse_hours("Работа (dwl 23,5ч)", self.rx()), 23.5)
        self.assertEqual(ap.parse_hours("Работа (dwl 23.5ч)", self.rx()), 23.5)
        self.assertEqual(ap.parse_hours("Работа (dwl 20ч)", self.rx()), 20.0)

    def test_latin_h(self):
        self.assertEqual(ap.parse_hours("Task (2h)", self.rx()), 2.0)

    def test_word_boundary_avoids_false_positives(self):
        """"3 часа" и "2 hotfix" - не часы: иначе сумма растет из описаний."""
        self.assertIsNone(ap.parse_hours("Правки за 3 часа", self.rx()))
        self.assertIsNone(ap.parse_hours("Релиз 2 hotfix", self.rx()))

    def test_no_hours_returns_none(self):
        self.assertIsNone(ap.parse_hours("Без часов", self.rx()))

    def test_format_is_russian(self):
        self.assertEqual(ap.fmt_hours(Decimal("104.5")), "104,5")
        self.assertEqual(ap.fmt_hours(Decimal("20")), "20")

    def test_decimal_sum_is_exact(self):
        """float давал расхождение с ручной проверкой в документе, по которому
        выставляется счет."""
        self.assertEqual(ap.parse_hours("Работа (100,5ч)", self.rx())
                         + ap.parse_hours("Правки (4ч)", self.rx()), Decimal("104.5"))

    def test_rows_and_total_agree(self):
        """Округление шло только при печати, поэтому три строки по 0,01 давали
        итог 0,02: сверка глазами в счете не сходилась."""
        rows = [ap.parse_hours(f"Работа {i} (0,005ч)", self.rx()) for i in range(3)]
        # 0,005 округляется вверх до 0,01 в каждой строке, значит итог 0,03 -
        # и он же виден глазами. Проверяется конкретное значение: сравнение
        # итога с суммой строк проходило бы и при округлении вниз
        self.assertEqual([ap.fmt_hours(r) for r in rows], ["0,01"] * 3)
        self.assertEqual(ap.fmt_hours(sum(rows, Decimal(0))), "0,03")

    def test_optional_capture_group_does_not_crash(self):
        """--hours-re с необязательной группой ронял разбор AttributeError."""
        self.assertIsNone(ap.parse_hours("ч", ap.compile_hours_re(r"(\d+)?ч")))

    def test_typographic_dash_is_not_hours(self):
        """Проверка слева перечисляла только ASCII-дефис, и "20-25ч" с
        типографским тире снова давало 25."""
        for dash in ("\u2013", "\u2014", "\u2212"):
            with self.subTest(dash=dash):
                self.assertIsNone(ap.parse_hours(f"Работа (20{dash}25ч)", self.rx()))

    def test_garbage_before_number_is_not_hours(self):
        """Все три молча уходили в сумму счета как 25, 5,5 и 3."""
        for name in ("Работа (20-25ч)", "Работа 1,5,5ч", "Работа 1e3ч"):
            with self.subTest(name=name):
                self.assertIsNone(ap.parse_hours(name, self.rx()))

    def test_absurd_number_is_not_hours(self):
        """Длинная строка цифр давала inf и отравляла итог целиком."""
        self.assertIsNone(ap.parse_hours("Работа " + "9" * 400 + "ч", self.rx()))
        self.assertIsNone(ap.parse_hours("Задача 999999ч", self.rx()))

    def test_custom_regex_without_group_refused(self):
        with self.assertRaises(SystemExit) as e:
            ap.compile_hours_re(r"\d+ч")
        self.assertIn("группу захвата", str(e.exception))

    def test_broken_regex_refused(self):
        with self.assertRaises(SystemExit) as e:
            ap.compile_hours_re(r"(\d+")
        self.assertIn("не компилируется", str(e.exception))

    def test_summary_totals_and_flags_unparsed(self):
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[
            {"name": "Первая (dwl 100,5ч)", "gid": "t1", "completed": True},
            {"name": "Вторая (dwl 4ч)", "gid": "t2", "completed": False},
            {"name": "Без часов", "gid": "t3", "completed": False},
        ])
        with patched(rec, {}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ap.cmd_summary(argparse.Namespace(auth=None, project="P1",
                                                  section="Отчет", hours_re=ap.HOURS_RE))
        out = out.getvalue()
        self.assertIn("итого: 104,5", out)
        self.assertIn("? | Без часов", out)
        self.assertIn("ВНИМАНИЕ", out)
        self.assertIn("[x]", out)

    def test_summary_writes_nothing(self):
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[])
        with patched(rec, {}):
            with contextlib.redirect_stdout(io.StringIO()):
                ap.cmd_summary(argparse.Namespace(auth=None, project="P1",
                                                  section="Отчет", hours_re=ap.HOURS_RE))
        self.assertEqual([c for c in rec.calls if c[0] in ("POST", "PUT")], [])


# gid намеренно числовые, как у Asana: упоминание в блоке опознается по
# data-asana-gid="\d+", и на выдуманном "t1" механика молча не сработала бы
BOARD_REL = dict(
    sections=[{"name": "Отчет", "gid": "s2"}],
    tasks=[{"name": "Итог", "gid": "2001"}, {"name": "Исходная", "gid": "2002"}],
)


class Related(unittest.TestCase):
    """Блок "откуда выросла эта работа" общий каркасом с asana-blockers:
    две копии каркаса разошлись бы на первом фиксе, а цена расхождения -
    стертое описание задачи."""

    def plan(self, related):
        return {"project": "P1", "sections": [{"name": "Отчет", "tasks": [
            {"name": "Итог", "related": related}]}]}

    def go(self, rec, related, send, notes=None):
        rec.call = rec.call_notes
        return run_cmd(ap.cmd_tasks, rec, self.plan(related), notes=notes, send=send)

    def test_dry_run_shows_and_writes_nothing(self):
        rec = Recorder(**BOARD_REL)
        out = self.go(rec, ["Исходная"], send=False)
        self.assertIn("БЛОК СВЯЗЕЙ -> Исходная", out)
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_send_writes_origin_block(self):
        rec = Recorder(**BOARD_REL)
        self.go(rec, ["Исходная"], send=True)
        puts = rec.sent("PUT", "/tasks/2001")
        self.assertEqual(len(puts), 1)
        body = puts[0][2]["html_notes"]
        self.assertIn("СВЯЗАННЫЕ ЗАДАЧИ", body)
        self.assertIn('data-asana-gid="2002"', body)

    def test_second_run_is_no_op(self):
        rec = Recorder(**BOARD_REL)
        self.go(rec, ["Исходная"], send=True)
        written = rec.sent("PUT", "/tasks/2001")[0][2]["html_notes"]
        rec2 = Recorder(**BOARD_REL)
        out = self.go(rec2, ["Исходная"], send=True, notes={"2001": written})
        self.assertIn("без изменений", out)
        self.assertEqual(rec2.sent("PUT", "/tasks/2001"), [])

    def test_foreign_blocker_section_survives(self):
        """Секцию блокировок пишет соседний скрипт - стереть ее нельзя."""
        ab = load_blockers()
        existing = ab.wrap(ab.render([ab.WAITS_HEAD, f'- {ab.mention("2009")}'], "Текст человека"))
        rec = Recorder(**BOARD_REL)
        self.go(rec, ["Исходная"], send=True, notes={"2001": existing})
        body = rec.sent("PUT", "/tasks/2001")[0][2]["html_notes"]
        self.assertIn(ab.WAITS_HEAD, body)
        self.assertIn('data-asana-gid="2009"', body)
        self.assertIn("СВЯЗАННЫЕ ЗАДАЧИ", body)
        self.assertIn("Текст человека", body)

    def test_unknown_reference_refused_before_write(self):
        rec = Recorder(**BOARD_REL)
        with self.assertRaises(SystemExit) as e:
            self.go(rec, ["Нет такой"], send=True)
        self.assertIn("нет такой задачи", str(e.exception).lower())
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_self_reference_refused(self):
        rec = Recorder(**BOARD_REL)
        with self.assertRaises(SystemExit) as e:
            self.go(rec, ["Итог"], send=True)
        self.assertIn("сама на себя", str(e.exception))

    def test_task_without_related_is_untouched(self):
        """Пустой или отсутствующий ключ ничего не стирает."""
        rec = Recorder(**BOARD_REL)
        rec.call = rec.call_notes
        run_cmd(ap.cmd_tasks, rec, tasks_plan({"name": "Итог"}, section="Отчет"), send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_related_to_task_created_in_same_run(self):
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[])
        rec.call = rec.call_notes
        plan = {"project": "P1", "sections": [{"name": "Отчет", "tasks": [
            {"name": "Итог", "related": ["Новая"]}, {"name": "Новая"}]}]}
        run_cmd(ap.cmd_tasks, rec, plan, send=True)
        self.assertEqual(len(rec.sent("PUT", "/tasks/")), 1)

    def test_bad_related_type_refused_by_validation(self):
        rec = Recorder(**BOARD_REL)
        with self.assertRaises(SystemExit) as e:
            self.go(rec, "Исходная", send=True)
        self.assertIn("related", str(e.exception))


class AdversarialFindings(unittest.TestCase):
    """Регресс на находки состязательного ревью (codex adversarial, раунд 1).
    Каждый тест воспроизводит конкретный способ навредить общей доске."""

    def test_numeric_name_colliding_with_foreign_gid_refused(self):
        """Задача, НАЗВАННАЯ числом, и чужая задача с таким gid - две разных
        задачи. Раньше молча побеждала трактовка "это gid", и переносилась
        чужая."""
        board = [{"gid": "999", "name": "2026", "memberships": member("P1", "s1", "В работе")},
                 {"gid": "2026", "name": "Чужая", "memberships": member("P1", "s1", "В работе")}]
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=board)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_move, rec, {"project": "P1", "section": "Отчет",
                                       "tasks": ["2026"]}, send=True)
        self.assertIn("неоднозначно", str(e.exception))
        self.assertEqual(rec.sent("POST", "addTask"), [])

    def test_numeric_name_without_collision_resolves(self):
        """Без коллизии числовое имя должно находиться, а не считаться промахом."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}],
                       tasks=[{"name": "2026", "gid": "999",
                               "memberships": member("P1", "s1", "В работе")}])
        run_cmd(ap.cmd_move, rec, {"project": "P1", "section": "Отчет",
                                   "tasks": ["2026"]}, send=True)
        self.assertEqual(rec.sent("POST", "addTask"),
                         [("POST", "/sections/s2/addTask", {"task": "999"})])

    def test_duplicate_section_names_refused(self):
        """Одноименные секции схлопывались в словаре, побеждала последняя из
        ответа API - запись уходила в непредсказуемую из двух."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"},
                                 {"name": "Отчет", "gid": "s3"}],
                       tasks=BOARD_MOVE["tasks"])
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_move, rec, {"project": "P1", "section": "Отчет",
                                       "tasks": ["A"]}, send=True)
        self.assertIn("несколько секций", str(e.exception))
        self.assertEqual(rec.sent("POST", "addTask"), [])

    def test_non_string_reference_refused_not_crashed(self):
        """null и число в списке роняли прогон AttributeError мимо отказа."""
        rec = Recorder(**BOARD_MOVE)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_move, rec, {"project": "P1", "section": "Отчет",
                                       "tasks": [None]}, send=True)
        self.assertIn("ожидается строка", str(e.exception))

    def test_related_resolved_before_any_task_is_created(self):
        """Мутационно значимый тест: прежняя версия резолвила related ПОСЛЕ
        цикла создания, и плохая ссылка обнаруживалась, когда задачи уже
        созданы. Задача тут именно новая - иначе тесту нечего было бы писать
        и он проходил бы на сломанной реализации."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[])
        rec.call = rec.call_notes
        plan = {"project": "P1", "sections": [{"name": "Отчет", "tasks": [
            {"name": "Новая", "related": ["Нет такой"]}]}]}
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, plan, send=True)
        self.assertIn("нет такой задачи", str(e.exception).lower())
        self.assertEqual(rec.sent("POST", "/tasks"), [])

    def test_outdated_neighbour_refused_before_any_write(self):
        """Старый asana-blockers рядом давал AttributeError после половины
        заливки - худший момент узнать, что сосед не той версии."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[])
        rec.call = rec.call_notes
        orig = ap.blocks_mod
        ap.blocks_mod = lambda: argparse.Namespace(mention=lambda g: g)
        try:
            with self.assertRaises(SystemExit) as e:
                run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [
                    {"name": "Отчет", "tasks": [{"name": "Новая", "related": ["Новая2"]},
                                                {"name": "Новая2"}]}]}, send=True)
        finally:
            ap.blocks_mod = orig
        self.assertIn("несовместимый", str(e.exception))
        self.assertEqual(rec.sent("POST", "/tasks"), [])

    def test_related_to_duplicate_board_name_refused(self):
        """Индекс задач по имени оставлял последнюю одноименную - связь
        уходила на произвольную из двух."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[
            {"name": "Итог", "gid": "2001"},
            {"name": "Исходная", "gid": "2002"},
            {"name": "Исходная", "gid": "2003"}])
        rec.call = rec.call_notes
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                    "tasks": [{"name": "Итог", "related": ["Исходная"]}]}]}, send=True)
        self.assertIn("несколько задач", str(e.exception))
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_empty_related_clears_section(self):
        """Иначе последнюю связь снять было нечем: секция оставалась навсегда."""
        ab = load_blockers()
        existing = ab.wrap(ab.render([ab.ORIGIN_HEAD, f'- {ab.mention("2002")}'], "Текст"))
        rec = Recorder(**BOARD_REL)
        rec.call = rec.call_notes
        rec.notes["2001"] = existing
        out = run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                "tasks": [{"name": "Итог", "related": []}]}]}, send=True)
        body = rec.sent("PUT", "/tasks/2001")[0][2]["html_notes"]
        self.assertNotIn(ab.ORIGIN_HEAD, body)
        self.assertIn("Текст", body)
        self.assertIn("снять секцию", out)

    def test_old_format_block_refused_not_silently_wiped(self):
        """Блок первой редакции узнается как наш, но на секции не разбирается:
        слияние оставляло от него пустоту и стирало записанные блокировки."""
        ab = load_blockers()
        old = (ab.MARK_START + "\nЗАБЛОКИРОВАНА: Исходная (открыта)\n"
               "https://app.asana.com/0/1/2002\n" + ab.MARK_END + "\n\nТекст человека")
        rec = Recorder(**BOARD_REL)
        rec.call = rec.call_notes
        rec.notes["2001"] = ab.wrap(old)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                    "tasks": [{"name": "Итог", "related": ["Исходная"]}]}]}, send=True)
        self.assertIn("СТАРОГО формата", str(e.exception))
        self.assertEqual(rec.sent("PUT", "/tasks/2001"), [])

    def test_dry_run_and_send_agree_on_duplicate_pending_reference(self):
        """Дедуп по gid считал два None разными задачами: dry-run обещал две
        связи там, где send делает одну. Проверяются ОБА прогона - тест только
        на dry-run прошел бы и на реализации, где send пишет две."""
        plan = {"project": "P1", "sections": [{"name": "Отчет", "tasks": [
            {"name": "Итог", "related": ["Новая", "Новая"]}, {"name": "Новая"}]}]}
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[])
        rec.call = rec.call_notes
        out = run_cmd(ap.cmd_tasks, rec, plan, send=False)
        self.assertIn("(1 связ.)", out)

        rec2 = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[])
        rec2.call = rec2.call_notes
        run_cmd(ap.cmd_tasks, rec2, plan, send=True)
        body = rec2.sent("PUT", "/tasks/")[0][2]["html_notes"]
        self.assertEqual(body.count("data-asana-gid"), 1)

    def test_old_block_refused_before_creating_anything(self):
        """Отказ на старом блоке случался в apply_related - после того как
        цикл уже создал задачи. План тут намеренно создает новую задачу."""
        ab = load_blockers()
        old_block = (ab.MARK_START + "\nЗАБЛОКИРОВАНА: Исходная (открыта)\n"
                     "https://app.asana.com/0/1/2002\n" + ab.MARK_END)
        rec = Recorder(**BOARD_REL)
        rec.call = rec.call_notes
        rec.notes["2001"] = ab.wrap(old_block)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                    "tasks": [{"name": "Новая"},
                              {"name": "Итог", "related": ["Исходная"]}]}]}, send=True)
        self.assertIn("СТАРОГО формата", str(e.exception))
        self.assertEqual(rec.sent("POST", "/tasks"), [])

    def test_explicit_gid_of_non_last_duplicate_resolves(self):
        """Индекс gid строился из уже схлопнутого индекса имен, и совет
        "укажи gid нужной" не работал для дубля, оказавшегося не последним."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[
            {"name": "Итог", "gid": "2001"},
            {"name": "Исходная", "gid": "2002"},
            {"name": "Исходная", "gid": "2003"}])
        rec.call = rec.call_notes
        run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                "tasks": [{"name": "Итог", "related": ["2002"]}]}]}, send=True)
        body = rec.sent("PUT", "/tasks/2001")[0][2]["html_notes"]
        self.assertIn('data-asana-gid="2002"', body)

    def test_duplicate_owner_name_refused(self):
        """Блок уходил в произвольную из одноименных задач."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[
            {"name": "Итог", "gid": "2001"}, {"name": "Итог", "gid": "2004"},
            {"name": "Исходная", "gid": "2002"}])
        rec.call = rec.call_notes
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                    "tasks": [{"name": "Итог", "related": ["Исходная"]}]}]}, send=True)
        self.assertIn("неоднозначно", str(e.exception))
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_related_by_numeric_name_from_plan(self):
        """Проверка и применение расходились на числовых ссылках: одна считала
        цифры только gid, другая переводила их через имя."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[])
        rec.call = rec.call_notes
        run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                "tasks": [{"name": "Итог", "related": ["2026"]}, {"name": "2026"}]}]},
                send=True)
        puts = rec.sent("PUT", "/tasks/")
        self.assertEqual(len(puts), 1)
        # gid созданных задач заглушка выдает как new1/new2 - важно, что
        # ссылка ведет на созданную "2026", а не на произвольную задачу
        created = {c[2]["name"]: f"new{i + 1}"
                   for i, c in enumerate(rec.sent("POST", "/tasks"))}
        self.assertIn(f'data-asana-gid="{created["2026"]}"', puts[0][2]["html_notes"])

    def test_duplicate_section_in_plan_refused_by_tasks(self):
        """cmd_tasks строил собственный индекс секций и молча брал последнюю
        одноименную - задача уходила в непредсказуемую из двух."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"},
                                 {"name": "Отчет", "gid": "s3"}], tasks=[])
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, tasks_plan({"name": "Новая"}, section="Отчет"),
                    send=True)
        self.assertIn("несколько секций", str(e.exception))
        self.assertEqual(rec.sent("POST", "/tasks"), [])

    def test_numeric_plan_name_colliding_with_board_gid_refused(self):
        """Фолбэк "строка есть в плане" отменял отказ резолвера: ссылка "2026",
        отвергнутая как неоднозначная, проходила и вела на чужую задачу."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[
            {"name": "Итог", "gid": "2001"}, {"name": "Чужая", "gid": "2026"}])
        rec.call = rec.call_notes
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                    "tasks": [{"name": "Итог", "related": ["2026"]},
                              {"name": "2026"}]}]}, send=True)
        self.assertIn("неоднозначно", str(e.exception))
        self.assertEqual(rec.sent("POST", "/tasks"), [])

    def test_duplicate_board_name_not_bypassed_by_plan(self):
        """Тот же фолбэк обходил и проверку на дубли имен."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[
            {"name": "Итог", "gid": "2001"},
            {"name": "Исходная", "gid": "2002"}, {"name": "Исходная", "gid": "2003"}])
        rec.call = rec.call_notes
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                    "tasks": [{"name": "Итог", "related": ["Исходная"]},
                              {"name": "Исходная"}]}]}, send=True)
        self.assertIn("несколько задач", str(e.exception))
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_wrong_typed_neighbour_api_refused(self):
        """Проверка "callable или строка" на каждый атрибут пропускала
        зеркальную подмену и падала TypeError после создания задач."""
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[])
        rec.call = rec.call_notes
        fake = argparse.Namespace(ORIGIN_HEAD=lambda: "x", MARK_START=lambda: "x")
        for fn in ("merge_sections", "parse_sections", "block_lines", "split_block",
                   "render", "body_of", "wrap", "mention", "gid_form"):
            setattr(fake, fn, "не функция")
        orig = ap.blocks_mod
        ap.blocks_mod = lambda: fake
        try:
            with self.assertRaises(SystemExit) as e:
                run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                        "tasks": [{"name": "Итог", "related": ["Новая"]},
                                  {"name": "Новая"}]}]}, send=True)
        finally:
            ap.blocks_mod = orig
        self.assertIn("несовместимый", str(e.exception))
        self.assertEqual(rec.sent("POST", "/tasks"), [])

    def test_tiny_hours_are_flagged_not_rounded_to_zero(self):
        """0,004ч распозналось бы как честный ноль: двести таких задач дали бы
        в счете 0 вместо часа, и предупреждение бы не сработало."""
        rx = ap.compile_hours_re(ap.HOURS_RE)
        self.assertIsNone(ap.parse_hours("Работа (0,004ч)", rx))
        self.assertEqual(ap.parse_hours("Работа (0ч)", rx), Decimal("0"))

    def test_abort_hint_names_confirmed_count(self):
        """Подсказка утверждала больше, чем известно: исход последнего запроса
        неизвестен, ответ мог потеряться уже после применения."""
        rec = Recorder(**BOARD_MOVE)
        def boom(method, path, token, payload=None):
            rec.calls.append((method, path, payload))
            raise json.JSONDecodeError("битый ответ", "", 0)
        rec.call = boom
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(json.JSONDecodeError):
            run_cmd(ap.cmd_move, rec, {"project": "P1", "section": "Отчет",
                                       "tasks": ["A"]}, send=True)
        self.assertIn("подтвержденно перенесено 0", err.getvalue())
        self.assertIn("исход последнего запроса неизвестен", err.getvalue())

    def test_dry_run_by_section_offers_pinned_plan(self):
        """Форма {project, section} перечитывает доску на --send: между показом
        и записью в секцию могли добавить чужую задачу, и она закроется тоже."""
        rec = Recorder(**BOARD_DONE)
        out = run_cmd(ap.cmd_complete, rec, {"project": "P1", "section": "Отчет"}, send=False)
        self.assertIn("планом по gid", out)
        self.assertIn('"tasks": ["t1"]', out)

    def test_pinned_plan_closes_exactly_that_set(self):
        """Задача, добавленная в секцию после предпросмотра, по закрепленному
        плану не закрывается."""
        # gid числовые, как в реальном закрепленном плане
        rec = Recorder(sections=[{"name": "Отчет", "gid": "s2"}], tasks=[
            {"name": "A", "gid": "3001", "completed": False,
             "memberships": member("P1", "s2", "Отчет")},
            {"name": "Чужая", "gid": "3009", "completed": False,
             "memberships": member("P1", "s2", "Отчет")}])
        run_cmd(ap.cmd_complete, rec, {"project": "P1", "tasks": ["3001"]}, send=True)
        self.assertEqual(rec.sent("PUT", "/tasks/"),
                         [("PUT", "/tasks/3001", {"completed": True})])

    def test_section_form_send_refused(self):
        """Форма по секции перечитывала доску на --send и закрывала то, чего в
        предпросмотре не было. Документацией это не лечится: инвариант
        "dry-run показывает ровно то, что сделает send" тут не выполним в
        принципе, поэтому запись идет только по перечню gid."""
        rec = Recorder(**BOARD_DONE)
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_complete, rec, {"project": "P1", "section": "Отчет"}, send=True)
        self.assertIn("только предпросмотр", str(e.exception))
        self.assertEqual(rec.sent("PUT", "/tasks/"), [])

    def test_self_reference_by_gid_refused(self):
        rec = Recorder(**BOARD_REL)
        rec.call = rec.call_notes
        with self.assertRaises(SystemExit) as e:
            run_cmd(ap.cmd_tasks, rec, {"project": "P1", "sections": [{"name": "Отчет",
                    "tasks": [{"name": "Итог", "related": ["2001"]}]}]}, send=True)
        self.assertIn("сама на себя", str(e.exception))


def load_blockers():
    spec = importlib.util.spec_from_file_location("_ab", SCRIPTS / "asana-blockers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
