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
import urllib.error
import urllib.request
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


class UploadFields(unittest.TestCase):
    """Поля чанков загрузки. Отдельно - localTime на финализации: 2026-07-28 без
    него приходил голый 500, 2026-08-04 сервер принял и без него (вендор починил
    молча). Поле шлем всегда: в OpenAPI оно не обязательное, значит контракт его
    не защищает и сломаться может так же тихо."""

    def _fields(self, n=0, total=1, last=True, **kw):
        base = dict(session_id="s", upload_id="u", n=n, total=total,
                    filename="rec.mp3", template="default-meeting", last=last,
                    name="Встреча", speakers=None, digest=None, size=None)
        base.update(kw)
        return mymeet.upload_fields(**base)

    def test_finalizing_chunk_carries_local_time(self):
        f = self._fields()
        self.assertIn("localTime", f)
        # ISO 8601 со смещением - иначе сервер отвечает 500
        self.assertRegex(f["localTime"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_data_chunks_without_local_time(self):
        """Нефинализирующим чанкам поле не нужно - встреча создается на финале."""
        self.assertNotIn("localTime", self._fields(n=0, total=3, last=False))

    def test_meeting_name_only_on_finalize(self):
        self.assertEqual(self._fields()["meeting_name"], "Встреча")
        self.assertNotIn("meeting_name", self._fields(last=False, total=2))

    def test_required_protocol_fields_present(self):
        f = self._fields(n=1, total=3, last=False)
        for key in ("id", "chunk_number", "chunk_total", "filename", "template_name"):
            self.assertIn(key, f)

    def test_checksum_only_when_given(self):
        self.assertNotIn("expected_sha256", self._fields())
        f = self._fields(digest="abc", size=10)
        self.assertEqual(f["expected_sha256"], "abc")
        self.assertEqual(f["expected_file_size"], 10)


class Multipart(unittest.TestCase):
    def test_file_part_present_even_when_empty(self):
        """Финализирующий маркер несет пустое поле file; без самого поля - 400."""
        body, ctype = mymeet._multipart({"id": "x"}, "rec.mp3", b"")
        self.assertIn(b'name="file"; filename="rec.mp3"', body)
        self.assertIn("boundary=", ctype)

    def test_fields_and_body_separated_by_boundary(self):
        body, ctype = mymeet._multipart({"a": 1, "b": "два"}, "f.bin", b"\x00\x01")
        boundary = ctype.split("boundary=")[1]
        self.assertEqual(body.count(f"--{boundary}\r\n".encode()), 3)  # 2 поля + file
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        self.assertIn("два".encode(), body)
        self.assertIn(b"\x00\x01", body)


class UploadGates(unittest.TestCase):
    def test_unknown_template_rejected_before_network(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with tempfile.NamedTemporaryFile(suffix=".mp3") as fh:
                code = mymeet.cmd_upload({}, None, Path(fh.name), "нет-такого",
                                         None, None, False)
        self.assertEqual(code, 2)
        self.assertIn("неизвестный шаблон", err.getvalue())

    def test_missing_file_rejected(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = mymeet.cmd_upload({}, None, Path("/нет/такого.mp3"),
                                     "default-meeting", None, None, False)
        self.assertEqual(code, 2)
        self.assertIn("нет файла", err.getvalue())


class UploadSafety(unittest.TestCase):
    """Регрессы на находки состязательного ревью: утечка ключа через редирект,
    оплата транскрибации огрызка, дубль встречи при потерянном ответе."""

    def test_filename_cannot_break_headers(self):
        body, _ = mymeet._multipart({}, 'q"uote\r\nX-Evil: 1.mp3', b"x")
        head = body.split(b"\r\n\r\n")[0].decode()
        # опасен не текст "X-Evil", а РАЗРЫВ строки, который делает его
        # отдельным заголовком части, и лишняя кавычка, закрывающая filename
        disposition = [ln for ln in head.split("\r\n") if "Content-Disposition" in ln][0]
        self.assertIn("X-Evil", disposition)          # осталось текстом внутри имени
        self.assertEqual(disposition.count('"'), 4)   # name="file" + filename="..."

    def test_empty_filename_replaced(self):
        self.assertEqual(mymeet._safe_filename("   "), "upload.bin")

    def test_opener_actually_uses_no_redirect(self):
        """Мало иметь обработчик - им должен пользоваться реальный opener,
        через который уходит запрос с ключом."""
        self.assertTrue(any(isinstance(h, mymeet._NoRedirect)
                            for h in mymeet._OPENER.handlers),
                        [type(h).__name__ for h in mymeet._OPENER.handlers])

    def test_redirect_handler_refuses(self):
        """С ключом в заголовке идти за редиректом нельзя: urllib утащил бы
        X-API-KEY на чужой origin."""
        h = mymeet._NoRedirect()
        req = urllib.request.Request("https://api.example/api/video")
        with self.assertRaises(urllib.error.HTTPError):
            h.redirect_request(req, io.BytesIO(b""), 302, "Found", {},
                               "https://evil.example/")

    def test_short_read_aborts_before_finalize(self):
        """Файл обрезали во время загрузки - финализировать нельзя: оплатили бы
        транскрибацию куска как полной записи."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "rec.mp3"
            # размер заведомо больше буфера чтения (8 КБ): на мелком файле
            # обрезку не увидеть - Python отдал бы остаток из буфера
            f.write_bytes(b"x" * 300_000)
            orig_chunk, orig_post = mymeet.CHUNK_SIZE, mymeet.api_post_multipart

            def cut_after_first(auth, path, fields, filename, blob):
                if fields["chunk_number"] == 0:
                    f.write_bytes(b"x" * 50_000)   # кто-то обрезал файл
                return 200, b"{}"

            mymeet.CHUNK_SIZE, mymeet.api_post_multipart = 50_000, cut_after_first
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    code = mymeet.cmd_upload({}, None, f, "default-meeting", None, None, False)
            finally:
                mymeet.CHUNK_SIZE, mymeet.api_post_multipart = orig_chunk, orig_post
        self.assertEqual(code, 1)
        self.assertIn("изменился во время загрузки", err.getvalue())

    def test_lost_meeting_id_warns_about_duplicate(self):
        """Ответ финализации потерян: повтор создаст вторую платную встречу -
        предупреждение обязано это назвать."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "rec.mp3"
            f.write_bytes(b"x" * 100)
            orig = mymeet.api_post_multipart
            mymeet.api_post_multipart = lambda *a, **k: (200, b"{}")   # без meeting_id
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    code = mymeet.cmd_upload({}, None, f, "default-meeting", None, None, False)
            finally:
                mymeet.api_post_multipart = orig
        self.assertEqual(code, 1)
        self.assertIn("ПРОВЕРЬТЕ workspace", err.getvalue())
        self.assertIn("спишет минуты еще раз", err.getvalue())

    def test_checksum_covers_multichunk(self):
        """Хеш считается по прочитанным блокам, а не только для одночанковых."""
        src = MYMEET.read_text(encoding="utf-8")
        self.assertIn("sha.update(blob)", src)
        self.assertIn("read_total != size", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
