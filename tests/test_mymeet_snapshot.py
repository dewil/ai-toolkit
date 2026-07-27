#!/usr/bin/env python3
"""Тесты обрезки служебной шапки mymeet. stdlib-only (unittest).

Запуск: python3 tests/test_mymeet_snapshot.py

Сервис кладет перед транскриптом свои выводы (супер краткое содержание,
саммари по темам, задачи), сделанные без контекста проекта. Скрипт их не
сохраняет - см. rules/meeting-transcripts.md. Ключевое требование к обрезке:
не потерять транскрипт, если формат сервиса изменится.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

MYMEET = Path(__file__).resolve().parent.parent / "scripts" / "mymeet-snapshot.py"
_spec = importlib.util.spec_from_file_location("mymeet_snapshot", MYMEET)
mymeet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mymeet)

REAL = """**Техническое планирование и стратегия сбора данных**

**Супер краткое содержание**:
- Обсудили текущий статус освоения ТЗ
- Сергей предложил начать с локального развертывания


**Саммари по темам**:
## Обсуждение понимания ТЗ и архитектуры проекта
- Женя прочитал ТЗ полтора раза [1:58]

## Парсинг данных и интеграция с API
- Сергей предлагает развернуть локально


**Задачи:**
- Развернуть локальную инфраструктуру в docker
- Изучить юридическую сторону парсинга


**Транскрипт:**
0:00:00 **Дмитрий:**
Так, Сергей, привет.
0:00:12 **Сергей:**
Все хорошо, отлично.
"""


class StripServiceBlocks(unittest.TestCase):
    def test_transcript_kept_whole(self):
        out, found = mymeet.strip_service_blocks(REAL)
        self.assertTrue(found)
        self.assertIn("0:00:00 **Дмитрий:**", out)
        self.assertIn("Все хорошо, отлично.", out)
        self.assertTrue(out.rstrip().endswith("Все хорошо, отлично."))

    def test_service_blocks_removed(self):
        out, _ = mymeet.strip_service_blocks(REAL)
        for block in ("Супер краткое содержание", "Саммари по темам", "**Задачи:**"):
            self.assertNotIn(block, out)
        # выводы сервиса уходят вместе с блоками, а не только их заголовки
        self.assertNotIn("Развернуть локальную инфраструктуру", out)
        self.assertNotIn("Женя прочитал ТЗ", out)

    def test_meeting_title_kept(self):
        """Заголовок несет тему встречи - имя файла хранит только дату."""
        out, _ = mymeet.strip_service_blocks(REAL)
        self.assertTrue(out.startswith("**Техническое планирование"))

    def test_unknown_format_kept_intact(self):
        """Сменился формат - сохраняем целиком: резать по догадке нельзя."""
        other = "**Встреча**\n\nсовсем другой формат\n0:00:01 Иван: привет\n"
        out, found = mymeet.strip_service_blocks(other)
        self.assertFalse(found)
        self.assertEqual(out, other)

    def test_marker_at_start_survives(self):
        out, found = mymeet.strip_service_blocks("**Транскрипт:**\n0:00:00 текст\n")
        self.assertTrue(found)
        self.assertIn("0:00:00 текст", out)

    def test_empty_input(self):
        out, found = mymeet.strip_service_blocks("")
        self.assertFalse(found)
        self.assertEqual(out, "")

    def test_only_first_marker_matters(self):
        """Слово "Транскрипт" в теле реплики не должно резать второй раз."""
        md = REAL + "\n0:05:00 **Женя:**\nПришли **Транскрипт:** пожалуйста.\n"
        out, _ = mymeet.strip_service_blocks(md)
        self.assertEqual(out.count("0:00:00 **Дмитрий:**"), 1)
        self.assertIn("Пришли **Транскрипт:** пожалуйста.", out)

    def test_marker_inside_reply_does_not_cut(self):
        """Блокер состязательного ревью: маркер внутри реплики - не граница.

        Формат сменился, транскрипт идет сразу, а в одной из реплик встречаются
        те же слова. Резать по ним - выбросить все сказанное до этой реплики.
        """
        md = (
            "**Встреча**\n\n"
            "0:00:00 **Иван:**\nВажная первая часть разговора.\n"
            "0:07:00 **Анна:**\nВ документе написано **Транскрипт:** пришлет Женя.\n"
            "0:08:00 **Иван:**\nПродолжаем обсуждение.\n"
        )
        out, found = mymeet.strip_service_blocks(md)
        self.assertFalse(found, "маркер не на своей строке - структура не распознана")
        self.assertEqual(out, md)
        self.assertIn("Важная первая часть разговора.", out)

    def test_standalone_marker_inside_started_transcript_does_not_cut(self):
        """Блокер раунда 2: маркер отдельной строкой, но транскрипт уже идет.

        Реплики ДО маркера - признак, что это не заголовок секции, а строка
        участника. Резать по нему значит выбросить начало разговора.
        """
        md = (
            "**Встреча**\n\n"
            "0:00:00 **Иван:**\nВажная первая часть.\n"
            "0:07:00 **Анна:**\n**Транскрипт:**\n"
            "0:08:00 **Иван:**\nПродолжаем.\n"
        )
        out, found = mymeet.strip_service_blocks(md)
        self.assertFalse(found)
        self.assertEqual(out, md)
        self.assertIn("Важная первая часть.", out)

    def test_marker_without_timecoded_body_is_not_trusted(self):
        """Маркер есть, реплик нет - режем шапку ради пустого хвоста."""
        md = REAL.split("**Транскрипт:**")[0] + "**Транскрипт:**\n\n"
        out, found = mymeet.strip_service_blocks(md)
        self.assertFalse(found)
        self.assertEqual(out, md)

    def test_marker_with_short_timecode_form(self):
        """Таймкод бывает без часов: "12:34 Имя:"."""
        md = "**Встреча**\n\n**Задачи:**\n- раз\n\n**Транскрипт:**\n12:34 Иван: привет\n"
        out, found = mymeet.strip_service_blocks(md)
        self.assertTrue(found)
        self.assertNotIn("**Задачи:**", out)
        self.assertIn("12:34 Иван: привет", out)

    def test_crlf_body_survives(self):
        md = REAL.replace("\n", "\r\n")
        out, found = mymeet.strip_service_blocks(md)
        self.assertTrue(found)
        self.assertIn("Так, Сергей, привет.", out)
        self.assertNotIn("Супер краткое содержание", out)

    def test_idempotent_on_already_stripped(self):
        """Повторный прогон по уже обрезанному тексту ничего не теряет."""
        once, _ = mymeet.strip_service_blocks(REAL)
        twice, found = mymeet.strip_service_blocks(once)
        self.assertTrue(found)
        self.assertEqual(twice, once)


class PlaceMeetingWrite(unittest.TestCase):
    """Запись файла встречи: атомарность и явная перезапись."""

    @contextlib.contextmanager
    def project(self, existing: str | None = None):
        """Временный проект с подмененным PROJECT_ROOT и заглушкой скачивания."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_root, original_dl = mymeet.PROJECT_ROOT, mymeet.download_md
            mymeet.PROJECT_ROOT = root
            mymeet.download_md = lambda auth, mid: REAL
            dest = root / "Встречи" / "2026" / "07"
            if existing is not None:
                dest.mkdir(parents=True)
                (dest / "2026-07-27.md").write_text(existing, encoding="utf-8")
            try:
                yield root, dest
            finally:
                mymeet.PROJECT_ROOT, mymeet.download_md = original_root, original_dl

    CFG = {"rules": [{"match": ["дейли"], "dest": "Встречи/{YYYY}/{MM}"}]}
    MEETING = {"id": "m1", "name": "дейли команды", "date": "2026-07-27T10:00:00"}

    def place(self, root, index):
        return mymeet.place_meeting(
            {}, self.CFG, root, index, dict(self.MEETING)
        )

    def test_writes_file_and_leaves_no_temp(self):
        with self.project() as (root, dest):
            with contextlib.redirect_stderr(io.StringIO()):
                rel = self.place(root, {})
            self.assertEqual(rel, str(Path("Встречи/2026/07/2026-07-27.md")))
            self.assertIn("0:00:00 **Дмитрий:**", (root / rel).read_text(encoding="utf-8"))
            self.assertEqual([f.name for f in dest.iterdir()], ["2026-07-27.md"])

    def test_overwrite_of_known_meeting_warns(self):
        """Точечный --pull перезаписывает молча - под путем из индекса могло
        оказаться уже не наше содержимое (после ручного переноса файлов)."""
        with self.project(existing="старое содержимое") as (root, dest):
            index = {"meetings": {"m1": {"file": "Встречи/2026/07/2026-07-27.md"}}}
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.place(root, index)
            self.assertIn("перезаписываю", err.getvalue())
            self.assertIn("2026-07-27.md", err.getvalue())

    def test_overwrite_keeps_previous_version(self):
        """Блокер раунда 2: сервис отдает более короткую версию, и она
        атомарно заменяла полный raw без возможности вернуться."""
        with self.project(existing="полный старый транскрипт" * 50) as (root, dest):
            index = {"meetings": {"m1": {"file": "Встречи/2026/07/2026-07-27.md"}}}
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.place(root, index)
            backup = dest / "2026-07-27.prev.md"
            self.assertTrue(backup.exists(), "прежнее содержимое должно уцелеть")
            self.assertIn("полный старый транскрипт", backup.read_text(encoding="utf-8"))
            self.assertIn("было", err.getvalue())
            self.assertIn("стало", err.getvalue())

    def test_new_meeting_does_not_clobber_foreign_file(self):
        """Файл на месте, но встречи нет в индексе - это чужой файл, берем -2."""
        with self.project(existing="чужой документ") as (root, dest):
            with contextlib.redirect_stderr(io.StringIO()):
                rel = self.place(root, {})
            self.assertTrue(rel.endswith("2026-07-27-2.md"))
            self.assertEqual(
                (dest / "2026-07-27.md").read_text(encoding="utf-8"), "чужой документ"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
