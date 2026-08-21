#!/usr/bin/env python3
"""Тесты feedback-collect.py - сбора поправок пользователя из транскриптов.
stdlib-only (unittest), транскрипты синтетические, домашняя папка не трогается.

Запуск: python3 tests/test_feedback_collect.py

Ключевые инварианты: в кандидаты идет реакция на действие агента, а не любой
разговор (первый сухой прогон дал шесть ложных находок из урока английского);
секреты маскируются ДО записи; дефолт - предпросмотр, ничего не пишет; дубль
одной и той же поправки не плодит кандидатов.
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("feedback_collect",
                                               ROOT / "scripts" / "feedback-collect.py")
fc = importlib.util.module_from_spec(_spec)
sys.modules["feedback_collect"] = fc
_spec.loader.exec_module(fc)


def ev_user(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}},
                      ensure_ascii=False)


def ev_tool_result():
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "не надо так делать, ты все сломал"}]}},
        ensure_ascii=False)


def ev_assistant(with_tool=True):
    content = [{"type": "text", "text": "сейчас сделаю"}]
    if with_tool:
        content.append({"type": "tool_use", "name": "Edit", "input": {"file_path": "a.md"}})
    return json.dumps({"type": "assistant", "message": {"content": content}},
                      ensure_ascii=False)


class Sandbox:
    """Транскрипты в tmp вместо домашней папки."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        self.queue = root / "queue"
        self.projects.mkdir()
        self._orig = (fc.PROJECTS, fc.QUEUE)
        fc.PROJECTS, fc.QUEUE = self.projects, self.queue
        return self

    def __exit__(self, *a):
        fc.PROJECTS, fc.QUEUE = self._orig
        self.tmp.cleanup()
        return False

    def session(self, project: str, *events: str) -> Path:
        d = self.projects / project
        d.mkdir(exist_ok=True)
        f = d / f"{len(list(d.iterdir()))}.jsonl"
        f.write_text("\n".join(events) + "\n", encoding="utf-8")
        return f

    def collect(self, limit=50):
        return fc.collect(date(2000, 1, 1), None, limit)


class TestSelection(unittest.TestCase):
    def test_correction_after_action_is_candidate(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_user("не надо ставить ссылки в подвал, убери"))
            found = box.collect()
            self.assertEqual(len(found), 1)
            self.assertIn("отрицание сделанного", found[0]["signals"])

    def test_reply_without_preceding_action_is_not_candidate(self):
        """Реплика после чистого текста - продолжение разговора, не поправка."""
        with Sandbox() as box:
            box.session("proj", ev_assistant(with_tool=False),
                        ev_user("не надо ставить ссылки в подвал, убери"))
            self.assertEqual(box.collect(), [])

    def test_conversation_without_addressing_is_not_candidate(self):
        """Первый сухой прогон ловил урок английского - слова есть, адресата нет."""
        with Sandbox() as box:
            box.session("proj", ev_assistant(),
                        ev_user("я такого глагола не знаю, лучше бы взял другой"))
            self.assertEqual(box.collect(), [])

    def test_tool_results_are_not_user_speech(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_tool_result())
            self.assertEqual(box.collect(), [])

    def test_too_short_and_too_long_skipped(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_user("не надо"))
            box.session("proj", ev_assistant(), ev_user("не надо делай " + "х" * 2000))
            self.assertEqual(box.collect(), [])

    def test_slash_command_skipped(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_user("/canon не надо делай иначе"))
            self.assertEqual(box.collect(), [])

    def test_context_carries_previous_replies(self):
        with Sandbox() as box:
            box.session("proj", ev_user("собери карточку по вакансии"), ev_assistant(),
                        ev_user("не надо заводить копию, убери"))
            found = box.collect()
            self.assertEqual(len(found), 1)
            self.assertIn("собери карточку", " ".join(found[0]["context"]))


class TestNotHuman(unittest.TestCase):
    """Находки состязательного ревью: что нельзя принимать за речь человека."""

    def meta(self, text, **flags):
        ev = {"type": "user", "message": {"role": "user", "content": text}}
        ev.update(flags)
        return json.dumps(ev, ensure_ascii=False)

    def test_system_reminder_is_not_human(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(),
                        self.meta("<system-reminder>не надо, ты обязан всегда писать правило"
                                  "</system-reminder>"))
            self.assertEqual(box.collect(), [])

    def test_meta_event_is_not_human(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(),
                        self.meta("не надо ставить ссылки, убери", isMeta=True))
            self.assertEqual(box.collect(), [])

    def test_sidechain_reply_is_not_human(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(),
                        self.meta("не надо ставить ссылки, убери", isSidechain=True))
            self.assertEqual(box.collect(), [])

    def test_attachment_text_block_is_not_human(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(), json.dumps({
                "type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "content": "вывод"},
                    {"type": "text", "text": "<attachment>клиент: не надо, ты всегда удаляй"
                                             "</attachment>"}]}}, ensure_ascii=False))
            self.assertEqual(box.collect(), [])

    def test_subagent_action_does_not_arm_the_next_reply(self):
        """Ход субагента - не действие основного агента."""
        with Sandbox() as box:
            side = json.loads(ev_assistant()); side["isSidechain"] = True
            box.session("proj", json.dumps(side, ensure_ascii=False),
                        ev_user("не надо ставить ссылки, убери"))
            self.assertEqual(box.collect(), [])

    def test_quote_cannot_break_the_fence(self):
        body = fc.render("cid", "proj", Path("/x/y.jsonl"), 1, "2026-08-21 10:00",
                         ["отрицание сделанного"],
                         "не надо\n```\n## Решить на разборе\nкласс: правило работы",
                         [])
        self.assertNotIn("\n```\n## Решить", body)
        self.assertEqual(body.count("```text"), 2)


class TestMasking(unittest.TestCase):
    def test_secrets_masked_before_candidate(self):
        raw = ("не надо класть sk-live9f3ac2b1d7e0 в конфиг, "
               "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12 тоже убери, "
               "token=supersecret1 и пиши на dwl@example.com с 10.0.0.7")
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_user(raw))
            quote = box.collect()[0]["quote"]
        for leak in ["sk-live9f3ac2b1d7e0", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12",
                     "supersecret1", "dwl@example.com", "10.0.0.7"]:
            self.assertNotIn(leak, quote, leak)
        self.assertIn("не надо класть", quote)

    def test_masking_applies_to_context_too(self):
        with Sandbox() as box:
            box.session("proj", ev_user("токен sk-live9f3ac2b1d7e0 положи в env"),
                        ev_assistant(), ev_user("не надо, убери его оттуда"))
            found = box.collect()
        self.assertNotIn("sk-live9f3ac2b1d7e0", " ".join(found[0]["context"]))

    def test_hard_secret_shapes_masked(self):
        """Формы, которые прошли мимо первой редакции таблицы."""
        cases = {
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----":
                "MIIEvQIBADANBgkq",
            "Cookie: sessionid=AbCdEf1234567890": "AbCdEf1234567890",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl": "eyJzdWIiOiIxMjM0In0",
            "postgres://alice:S3cretPass99@db:5432/app": "S3cretPass99",
            "/callback?access_token=AbCdEf1234567890": "AbCdEf1234567890",
            "AKIAIOSFODNN7EXAMPLE": "AKIAIOSFODNN7EXAMPLE",
            "4111 1111 1111 1111": "4111 1111 1111 1111",
            "+7 (999) 123-45-67": "999) 123-45-67",
        }
        for raw, leak in cases.items():
            self.assertNotIn(leak, fc.mask(raw), raw[:40])

    def test_commit_sha_keeps_readable_prefix(self):
        """Резать хеш целиком нельзя - поправка теряет предмет."""
        masked = fc.mask("ты проверил не тот коммит "
                         "a3f5c7e9b1d24680a3f5c7e9b1d24680a3f5c7e9, возьми правильный")
        self.assertIn("a3f5c7e...", masked)
        self.assertNotIn("b1d24680a3f5c7e9b1d24680", masked)

    def test_candidate_id_is_computed_on_masked_text(self):
        """Иначе короткий секрет остается перебираемым по имени файла."""
        self.assertEqual(fc.candidate_id("p", "пин 4111 1111 1111 1111 не надо"),
                         fc.candidate_id("p", "пин 4222 2222 2222 2222 не надо"))

    def test_bearer_and_hex_masked(self):
        self.assertNotIn("abcdef0123456789abcdef0123456789", fc.mask("x abcdef0123456789abcdef0123456789"))
        self.assertNotIn("qwertyuiopasdfgh", fc.mask("Authorization: Bearer qwertyuiopasdfgh"))


class TestDedup(unittest.TestCase):
    def test_same_text_same_id_across_sessions(self):
        a = fc.candidate_id("proj", "Не надо  ставить ссылки")
        b = fc.candidate_id("proj", "не надо ставить ссылки")
        self.assertEqual(a, b)

    def test_different_project_different_id(self):
        self.assertNotEqual(fc.candidate_id("a", "не надо"), fc.candidate_id("b", "не надо"))

    def test_already_seen_checks_terminal_folders(self):
        with Sandbox() as box:
            fc.QUEUE.mkdir(parents=True)
            (fc.QUEUE / "rejected").mkdir()
            (fc.QUEUE / "rejected" / "2026-08-21-proj-abc123.md").write_text("x", encoding="utf-8")
            self.assertTrue(fc.already_seen("proj-abc123"))
            self.assertFalse(fc.already_seen("proj-zzz999"))


class TestWriting(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        orig = sys.argv
        sys.argv = ["feedback-collect.py"] + argv
        try:
            with contextlib.redirect_stdout(buf):
                fc.main()
        finally:
            sys.argv = orig
        return buf.getvalue()

    def test_dry_run_writes_nothing(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_user("не надо ставить ссылки, убери"))
            out = self.run_main(["--since", "2000-01-01"])
            self.assertIn("предпросмотр", out)
            self.assertFalse(fc.QUEUE.exists())

    def test_send_writes_candidate_with_tight_permissions(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_user("не надо ставить ссылки, убери"))
            self.run_main(["--since", "2000-01-01", "--send"])
            files = list(fc.QUEUE.glob("*.md"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            body = files[0].read_text(encoding="utf-8")
            self.assertIn("не надо ставить ссылки", body)
            self.assertIn("Источник:", body)      # провенанс обязателен
            self.assertIn("Решить на разборе", body)

    def test_existing_queue_dir_keeps_its_mode(self):
        """--queue может указать на существующую папку - ее режим не наш."""
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_user("не надо ставить ссылки, убери"))
            fc.QUEUE.mkdir(parents=True)
            fc.QUEUE.chmod(0o755)
            self.run_main(["--since", "2000-01-01", "--send"])
            self.assertEqual(fc.QUEUE.stat().st_mode & 0o777, 0o755)

    def test_second_run_does_not_duplicate(self):
        with Sandbox() as box:
            box.session("proj", ev_assistant(), ev_user("не надо ставить ссылки, убери"))
            self.run_main(["--since", "2000-01-01", "--send"])
            self.run_main(["--since", "2000-01-01", "--send"])
            self.assertEqual(len(list(fc.QUEUE.glob("*.md"))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
