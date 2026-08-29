#!/usr/bin/env python3
"""Тесты asana-blockers.py - связи задач текстом в описании задачи Asana.
stdlib-only (unittest), сеть подменяется заглушками.

Запуск: python3 tests/test_asana_blockers.py

Ключевые инварианты: чужой текст под нашим заголовком не трогаем; повторный
прогон не меняет ничего; запись идет по перечитанному описанию, а не по снимку
предпросмотра; ошибка (ненайденная или неоднозначная задача, кривой конфиг) не
превращается в успешный прогон.

Каждый класс ниже, кроме TestBasics, закрывает находку состязательного ревью
(два раунда codex adversarial: потеря чужого текста, гонка на записи, 429,
нормализация имен, пустые тесты).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import re
import unicodedata
import unittest
import urllib.error
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("asana_blockers", SCRIPTS / "asana-blockers.py")
ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ab)

BLOCK = ab.MARK_START
END = ab.MARK_END


PROJECT = "123"


def task(gid, name, notes="", completed=False, html=None, assignee=("7", "Евгений")):
    t = {"gid": gid, "name": name, "notes": notes, "completed": completed,
         "permalink_url": f"https://app.asana.com/0/1/{gid}",
         "projects": [{"gid": PROJECT}],
         "html_notes": html if html is not None else f"<body>{notes}</body>"}
    if assignee:
        t["assignee"] = {"gid": assignee[0], "name": assignee[1]}
    return t


def link(gid):
    """Упоминание, как его пишет скрипт."""
    return '<a data-asana-gid="%s"/>' % gid



class Board:
    """Заглушка сети: доска в памяти, запись через PUT меняет ее же.

    Порядок вызовов пишется в calls: тест, проверяющий отсутствие записи,
    должен уметь покраснеть, если запись случится под другим методом.
    """

    def __init__(self, tasks):
        self.tasks = tasks
        self.puts: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.live_edit: dict[str, dict] = {}  # правка "человеком" между GET и PUT
        self.deleted: set[str] = set()
        self.bad_body: set[str] = set()  # 200 с неполным телом

    def get_all(self, path, token):
        return [dict(t, html_notes=self.canonize(t.get("html_notes") or ""))
                for t in self.tasks]

    def _find(self, gid):
        return next((t for t in self.tasks if t["gid"] == gid), None)

    @staticmethod
    def canonize(html):
        """Asana при чтении разворачивает короткое упоминание в полный якорь.

        Заглушка обязана это моделировать: пока она возвращала записанные байты
        как есть, тесты не видели, что скрипт перестает узнавать собственный блок
        после первой же записи (находка состязательного ревью)."""
        def expand(m):
            gid = m.group(1)
            return (f'<a href="https://app.asana.com/0/0/{gid}/f" data-asana-accessible="true" '
                    f'data-asana-dynamic="true" data-asana-type="task" '
                    f'data-asana-gid="{gid}">имя-{gid}</a>')
        return re.sub(r'<a data-asana-gid="(\d+)"\s*/>', expand, html)

    def request(self, method, url, token, payload=None):
        gid = url.rstrip("/").split("/tasks/")[-1].split("?")[0]
        self.calls.append((method, gid))
        if gid in self.deleted:
            raise SystemExit(f"Asana API 404 на {method} {url}")
        cur = self._find(gid)
        if method == "GET":
            if gid in self.bad_body:
                return {"data": None}
            cur.update(self.live_edit.pop(gid, {}))
            out = dict(cur)
            out["html_notes"] = self.canonize(out.get("html_notes") or "")
            return {"data": out}
        if method != "PUT":
            raise AssertionError(f"неожиданный метод записи: {method}")
        assert "html_notes" in payload, "пишем html_notes, а не notes"
        inner = payload["html_notes"]
        assert inner.startswith("<body>") and inner.endswith("</body>"), inner[:40]
        body = inner[len("<body>"):-len("</body>")]
        self.puts.append((gid, body))
        cur["html_notes"] = inner
        cur["notes"] = body
        return {"data": cur}


def run(board, chains, send=True, project=PROJECT):
    """Прогон run_board на заглушке. Возвращает (written, err, stdout)."""
    orig_get_all, orig_request = ab.get_all, ab._request
    ab.get_all, ab._request = board.get_all, board.request
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            written, err = ab.run_board("test", {"project": project, "chains": chains},
                                        "tok", send)
    finally:
        ab.get_all, ab._request = orig_get_all, orig_request
    return written, err, buf.getvalue()


def cfg(obj):
    """load_config на временном файле: возвращает конфиг либо текст SystemExit."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))
        path = f.name
    try:
        return ab.load_config(path)
    except SystemExit as e:
        return str(e)
    finally:
        Path(path).unlink()


class TestBasics(unittest.TestCase):
    def test_both_sides_written(self):
        board = Board([task("1", "A"), task("2", "B")])
        written, err, out = run(board, {"A": ["B"]})
        self.assertFalse(err)
        self.assertEqual(written, 2)
        notes = dict(board.puts)
        self.assertIn(ab.WAITS_HEAD, notes["1"])
        self.assertIn("- %s - %s, открыта" % (link("2"), link("7")), notes["1"])
        self.assertIn(ab.HOLDS_HEAD, notes["2"])
        self.assertIn("- %s - %s" % (link("1"), link("7")), notes["2"])
        self.assertNotIn("ЗАБЛОКИРОВАНА", notes["1"])

    def test_second_run_is_noop(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        written, err, out = run(board, {"A": ["B"]})
        self.assertEqual((written, err), (0, False))
        self.assertIn("задач с изменениями: 0", out)

    def test_existing_text_survives_and_stays_below(self):
        board = Board([task("1", "A", "важный текст\n\nвторой абзац"), task("2", "B")])
        run(board, {"A": ["B"]})
        self.assertTrue(dict(board.puts)["1"].endswith("важный текст\n\nвторой абзац"))

    def test_removed_chain_removes_block(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        written, err, out = run(board, {})
        self.assertEqual((written, err), (2, False))
        self.assertEqual(board.tasks[0]["notes"], "")
        self.assertIn("СНЯТЬ БЛОК СВЯЗЕЙ", out)

    def test_unknown_task_is_error_without_writes(self):
        board = Board([task("1", "A")])
        written, err, _ = run(board, {"A": ["Нет такой"]})
        self.assertTrue(err)
        self.assertEqual(board.puts, [])

    def test_completed_dependency_marked_closed(self):
        board = Board([task("1", "A"), task("2", "B", completed=True)])
        run(board, {"A": ["B"]})
        self.assertIn("- %s - %s, закрыта" % (link("2"), link("7")), dict(board.puts)["1"])


class TestForeignTextNotOurs(unittest.TestCase):
    """Находка 1: описание, начатое человеком словами 'СВЯЗИ ЗАДАЧИ', стиралось."""

    def test_manual_block_under_our_header_untouched(self):
        notes = f"{BLOCK}\nРучные договоренности заказчика\n{END}\nЭтот текст сохранить"
        board = Board([task("9", "C", notes)])
        written, err, _ = run(board, {})
        self.assertEqual((written, err), (0, False))
        self.assertEqual(board.tasks[0]["notes"], notes)
        self.assertEqual(board.puts, [])

    def test_mixed_block_untouched(self):
        """Наша строка плюс чужая внутри блока - блок все равно не наш."""
        notes = (f"{BLOCK}\nЗАБЛОКИРОВАНА: B (открыта)\nhttps://app.asana.com/0/1/2\n"
                 f"и заодно ждем счет от бухгалтерии\n{END}\nхвост")
        board = Board([task("9", "C", notes)])
        written, _, _ = run(board, {})
        self.assertEqual(written, 0)

    def test_marker_line_with_trailing_text_is_not_block_end(self):
        """'- - - ВАЖНО' не конец блока: иначе хвост строки съедался."""
        notes = f"{BLOCK}\nЗАБЛОКИРОВАНА: B (открыта)\n{END} ВАЖНО\nтекст"
        self.assertEqual(ab.split_block(notes), (None, notes))

    def test_task_outside_config_keeps_its_own_text(self):
        """Задача вне цепочек с обычным описанием не трогается вовсе."""
        board = Board([task("1", "A"), task("2", "B"), task("9", "C", "мое описание")])
        run(board, {"A": ["B"]})
        self.assertNotIn("9", dict(board.puts))


class TestConcurrentEdit(unittest.TestCase):
    """Находка 2: PUT перезаписывал описание снимком предпросмотра."""

    def test_edit_between_plan_and_write_survives(self):
        board = Board([task("1", "A"), task("2", "B")])
        board.live_edit["1"] = {"html_notes": "<body>ТЗ v2, согласованная цена</body>"}
        run(board, {"A": ["B"]})
        written = dict(board.puts)["1"]
        self.assertIn("ТЗ v2, согласованная цена", written)
        self.assertIn(ab.WAITS_HEAD, written)

    def test_write_skipped_if_someone_already_did_it(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        board.tasks[0]["html_notes"] = "<body></body>"  # снимок устареет
        board.puts.clear()
        written, err, _ = run(board, {"A": ["B"]})
        self.assertEqual(err, False)
        self.assertEqual(written, 1)
        self.assertIn(ab.WAITS_HEAD, board.tasks[0]["html_notes"])


class TestRetry(unittest.TestCase):
    """Находка 3: 429 обрывал запись на середине доски."""

    def test_429_is_retried(self):
        calls = []
        slept = []

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"data": {"ok": true}}'

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                             {"Retry-After": "1"}, io.BytesIO(b"limit"))
            return FakeResp()

        orig_open, orig_sleep = ab.urllib.request.urlopen, ab.time.sleep
        ab.urllib.request.urlopen = fake_urlopen
        ab.time.sleep = lambda s: slept.append(s)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                body = ab._request("GET", "https://app.asana.com/0/1/2/tasks/1", "tok")
        finally:
            ab.urllib.request.urlopen, ab.time.sleep = orig_open, orig_sleep
        self.assertEqual(body["data"]["ok"], True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(slept, [1])

    def test_permanent_error_exits(self):
        def fake_urlopen(req, timeout=None, context=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"no"))

        orig = ab.urllib.request.urlopen
        ab.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                ab._request("GET", "https://app.asana.com/0/1/2/tasks/1", "tok")
        finally:
            ab.urllib.request.urlopen = orig


class TestNameNormalization(unittest.TestCase):
    """Находка 4: краевой пробел в ключе давал одностороннюю связь и exit 0."""

    def test_padded_key_still_sets_both_sides(self):
        conf = cfg({"b": {"project": "123", "chains": {" A ": ["B"]}}})
        self.assertIsInstance(conf, dict)
        board = Board([task("1", "A"), task("2", "B")])
        written, err, _ = run(board, conf["b"]["chains"])
        self.assertEqual((written, err), (2, False))
        notes = dict(board.puts)
        self.assertIn(ab.WAITS_HEAD, notes["1"])
        self.assertIn(ab.HOLDS_HEAD, notes["2"])

    def test_padded_duplicate_key_is_config_error(self):
        msg = cfg({"b": {"project": "123", "chains": {"A": ["B"], " A ": ["C"]}}})
        self.assertIsInstance(msg, str)
        self.assertIn("встречается дважды", msg)

    def test_unicode_forms_match(self):
        """Составная и готовая формы 'é' - одна и та же задача."""
        composed, decomposed = "Café", "Café"
        conf = cfg({"b": {"project": "123", "chains": {"A": [composed]}}})
        board = Board([task("1", "A"), task("2", decomposed)])
        written, err, _ = run(board, conf["b"]["chains"])
        self.assertEqual((written, err), (2, False))

    def test_unicode_duplicate_on_board_is_collision(self):
        board = Board([task("1", "A"), task("2", "Café"), task("3", "Café")])
        written, err, _ = run(board, {"A": ["Café"]})
        self.assertTrue(err)
        self.assertEqual(board.puts, [])


class TestDuplicateTasksOutsideConfig(unittest.TestCase):
    """Находка 5: на одноименных задачах вне конфига оставался старый блок."""

    def test_stale_block_removed_from_every_duplicate(self):
        stale = f"{BLOCK}\nЗАБЛОКИРОВАНА: X (открыта)\nhttps://app.asana.com/0/1/2\n{END}\nхвост"
        board = Board([task("1", "A", stale), task("2", "A", stale)])
        written, err, _ = run(board, {})
        self.assertEqual((written, err), (2, False))
        self.assertEqual([t["notes"] for t in board.tasks], ["хвост", "хвост"])


class TestBadNames(unittest.TestCase):
    """Находка 6: имя с переводом строки и линейкой ломало идемпотентность."""

    def test_newline_in_name_rejected(self):
        msg = cfg({"b": {"project": "123", "chains": {"A": [f"bad\n{END}\nTAIL"]}}})
        self.assertIsInstance(msg, str)
        self.assertIn("перевод строки", msg)

    def test_unicode_line_separator_rejected(self):
        """Находка раунда 2: splitlines() режет не только по \\n и \\r."""
        for sep in ["\u2028", "\u2029", "\x0b", "\x0c", "\x85"]:
            msg = cfg({"b": {"project": "123", "chains": {"A": [f"bad{sep}TAIL"]}}})
            self.assertIsInstance(msg, str, repr(sep))
            self.assertIn("перевод строки", msg)

    def test_name_equal_to_header_is_allowed(self):
        """Имя внутри блока всегда стоит после префикса - маркером не станет."""
        self.assertIsInstance(cfg({"b": {"project": "123", "chains": {BLOCK: ["B"]}}}), dict)


class TestCRLF(unittest.TestCase):
    """Находка 7: блок с CRLF не опознавался, прогон дописывал второй."""

    def test_crlf_block_replaced_not_duplicated(self):
        crlf = f"{BLOCK}\r\nЗАБЛОКИРОВАНА: B (открыта)\r\nhttps://app.asana.com/0/1/2\r\n{END}\r\n\r\nхвост"
        board = Board([task("1", "A", crlf), task("2", "B")])
        run(board, {"A": ["B"]})
        written = dict(board.puts)["1"]
        self.assertEqual(written.count(BLOCK), 1)
        self.assertTrue(written.endswith("хвост"))


class TestConfigShape(unittest.TestCase):
    """Находки 8 и 10: дубли ключей JSON и project не-gid."""

    def test_duplicate_json_key_rejected(self):
        msg = cfg('{"b": {"project": "123", "chains": {"A": ["B"], "A": ["C"]}}}')
        self.assertIsInstance(msg, str)
        self.assertIn("повторяющийся ключ", msg)

    def test_duplicate_board_key_rejected(self):
        msg = cfg('{"b": {"project": "1", "chains": {}}, "b": {"project": "2", "chains": {}}}')
        self.assertIsInstance(msg, str)
        self.assertIn("повторяющийся ключ", msg)

    def test_project_must_be_gid(self):
        for bad in ["https://app.asana.com/0/123/list", "123?foo=bar", ""]:
            msg = cfg({"b": {"project": bad, "chains": {"A": ["B"]}}})
            self.assertIsInstance(msg, str, bad)
            self.assertIn("project", msg)

    def test_self_dependency_rejected(self):
        msg = cfg({"b": {"project": "123", "chains": {"A": ["A"]}}})
        self.assertIn("сама от себя", msg)

    def test_valid_config_is_normalized(self):
        conf = cfg({"b": {"project": " 123 ", "chains": {" A ": [" B "]}}})
        self.assertEqual(conf, {"b": {"project": "123", "chains": {"A": ["B"]}}})


class TestOwnershipStrictness(unittest.TestCase):
    """Находка раунда 2: мягкая проверка владения стирала правдоподобный текст."""

    def test_prefix_without_link_pair_is_not_ours(self):
        notes = (f"{BLOCK}\nБЛОКИРУЕТ: это ручное пояснение к запуску\n"
                 f"http вовсе не ссылка, а начало абзаца\n{END}\nСОХРАНИТЬ")
        self.assertEqual(ab.split_block(notes), (None, notes))

    def test_empty_block_is_not_ours(self):
        notes = f"{BLOCK}\n\n{END}\nСОХРАНИТЬ"
        self.assertEqual(ab.split_block(notes), (None, notes))

    def test_link_with_appended_note_is_not_ours(self):
        notes = (f"{BLOCK}\nБЛОКИРУЕТ: A\n"
                 f"https://app.asana.com/0/1/2 - не удалять, здесь спорный пункт\n"
                 f"{END}\nСОХРАНИТЬ")
        self.assertEqual(ab.split_block(notes), (None, notes))

    def test_own_block_is_still_recognized(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        block, rest = ab.split_block(board.tasks[0]["notes"])
        self.assertIsNotNone(block)
        self.assertEqual(rest, "")

    def test_manual_block_not_wiped_by_run(self):
        notes = (f"{BLOCK}\nБЛОКИРУЕТ: ручное пояснение\nтекст без ссылки\n"
                 f"{END}\nСОХРАНИТЬ")
        board = Board([task("9", "C", notes)])
        written, err, _ = run(board, {})
        self.assertEqual((written, err, board.puts), (0, False, []))


class TestWriteGuards(unittest.TestCase):
    """Находки раунда 2: что должно остановить запись после предпросмотра."""

    def test_incomplete_response_blocks_write(self):
        board = Board([task("1", "A", "ВАЖНО"), task("2", "B")])
        board.bad_body.add("1")
        written, err, _ = run(board, {"A": ["B"]})
        self.assertNotIn("1", dict(board.puts))
        self.assertTrue(err)
        self.assertEqual(board.tasks[0]["notes"], "ВАЖНО")

    def test_renamed_task_blocks_write(self):
        board = Board([task("1", "A"), task("2", "B")])
        board.live_edit["1"] = {"name": "A (переименована)"}
        written, err, _ = run(board, {"A": ["B"]})
        self.assertNotIn("1", dict(board.puts))
        self.assertTrue(err)

    def test_task_moved_off_board_blocks_write(self):
        board = Board([task("1", "A"), task("2", "B")])
        board.live_edit["1"] = {"projects": [{"gid": "999"}]}
        written, err, _ = run(board, {"A": ["B"]})
        self.assertNotIn("1", dict(board.puts))
        self.assertTrue(err)

    def test_deleted_task_stops_run_without_put(self):
        board = Board([task("1", "A"), task("2", "B")])
        board.deleted.add("1")
        with self.assertRaises(SystemExit):
            run(board, {"A": ["B"]})
        self.assertEqual(board.puts, [])

    def test_already_correct_description_is_not_rewritten(self):
        """Настоящая проверка ветки skip: нужный блок появился до записи."""
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        done = board.tasks[0]["html_notes"]
        board.tasks[0]["html_notes"] = "<body></body>"  # план по устаревшему снимку
        board.live_edit["1"] = {"html_notes": done}
        board.puts.clear()
        written, err, _ = run(board, {"A": ["B"]})
        self.assertEqual((written, err, board.puts), (0, False, []))
        self.assertIn(("GET", "1"), board.calls)


class TestRetryDelay(unittest.TestCase):
    """Находка раунда 2: Retry-After датой ронял процесс посреди записи."""

    def test_seconds_respected_above_minute(self):
        self.assertEqual(ab.retry_delay({"Retry-After": "120"}, 0), 120)

    def test_http_date_parsed(self):
        import email.utils, time as _t
        when = email.utils.formatdate(_t.time() + 45, usegmt=True)
        self.assertGreater(ab.retry_delay({"Retry-After": when}, 0), 30)

    def test_garbage_falls_back_to_backoff(self):
        self.assertEqual(ab.retry_delay({"Retry-After": "скоро"}, 1), 10)

    def test_absent_header(self):
        self.assertEqual(ab.retry_delay({}, 0), 5)

    def test_capped(self):
        self.assertEqual(ab.retry_delay({"Retry-After": "99999"}, 0), ab.MAX_WAIT)


class TestLeadingBlankLines(unittest.TestCase):
    """Находка раунда 2: пустые строки сверху требовали третьего прогона."""

    def test_converges_on_second_run(self):
        board = Board([task("1", "A", "\n\nважный текст"), task("2", "B")])
        run(board, {"A": ["B"]})
        written, err, _ = run(board, {"A": ["B"]})
        self.assertEqual((written, err), (0, False))


class TestAsanaCanonicalization(unittest.TestCase):
    """Находка ревью: Asana разворачивает наше короткое упоминание в полный
    якорь, и распознаватель перестал бы узнавать собственный блок - на каждом
    прогоне добавлялся бы еще один."""

    def test_expanded_mention_is_still_our_line(self):
        expanded = ('- <a href="https://app.asana.com/0/0/2/f" data-asana-type="task" '
                    'data-asana-gid="2">Прислать фото</a> - '
                    '<a data-asana-gid="7">Евгений</a>, открыта')
        self.assertTrue(ab.is_item_line(expanded))

    def test_second_run_after_canonization_is_noop(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        board.puts.clear()
        written, err, out = run(board, {"A": ["B"]})
        self.assertEqual((written, err, board.puts), (0, False, []))
        self.assertIn("задач с изменениями: 0", out)

    def test_no_second_block_appears(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        run(board, {"A": ["B"]})
        self.assertEqual(board.tasks[0]["html_notes"].count(BLOCK), 1)

    def test_block_is_removed_after_canonization(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        board.puts.clear()
        written, err, _ = run(board, {})
        self.assertEqual((written, err), (2, False))
        self.assertNotIn(BLOCK, board.tasks[0]["html_notes"])

    def test_manual_note_inside_item_line_makes_block_foreign(self):
        notes = (f"{BLOCK}\n{ab.WAITS_HEAD}\n"
                 '- <a data-asana-gid="2"/> - ручная оговорка: НЕ УДАЛЯТЬ\n'
                 f"{END}\nСОХРАНИТЬ")
        self.assertEqual(ab.split_block(notes), (None, notes))


class TestForeignBlockWarning(unittest.TestCase):
    """Находка ревью: блок под нашим заголовком с ручной правкой внутри мы
    своим не считаем, но новый ляжет сверху - на карточке две версии связей."""

    def test_preview_warns_about_foreign_block(self):
        foreign = (f"{BLOCK}\n{ab.WAITS_HEAD}\n"
                   '- <a data-asana-gid="2"/> - и еще ждем счет от бухгалтерии\n'
                   f"{END}\nхвост")
        board = Board([task("1", "A", html=f"<body>{foreign}</body>"), task("2", "B")])
        _w, _e, out = run(board, {"A": ["B"]}, send=False)
        self.assertIn("уже есть блок связей", out)

    def test_anchor_regex_does_not_swallow_text_between_mentions(self):
        """Самозакрывающийся якорь не должен съедать текст до следующего </a>."""
        text = '<a data-asana-gid="2"/> ВАЖНЫЙ ТЕКСТ <a href="x">имя</a> хвост'
        self.assertIn("ВАЖНЫЙ ТЕКСТ", ab.gid_form(text))
        self.assertIn("хвост", ab.gid_form(text))

    def test_no_warning_when_block_is_ours(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        _w, _e, out = run(board, {"A": ["B"]}, send=False)
        self.assertNotIn("уже есть блок связей", out)

    def test_missing_assignee_name_shows_gid_not_emptiness(self):
        board = Board([task("1", "A", assignee=("9", "Другой")),
                       task("2", "B", assignee=("7", None))])
        _w, _e, out = run(board, {"A": ["B"]}, send=False)
        self.assertIn("gid 7", out)
        self.assertNotIn("data-asana-gid", out)


class TestOldFormatMigration(unittest.TestCase):
    """На досках уже стоят блоки первой редакции. Не узнать свой старый блок
    значит дописать второй сверху вместо замены - и оставить на карточке две
    противоречивые версии связей."""

    def old_block(self, dep_name="B", url="https://app.asana.com/0/1/2"):
        return (f"{BLOCK}\nЗАБЛОКИРОВАНА: {dep_name} (открыта)\n{url}\n{END}")

    def test_manual_block_with_foreign_link_is_not_ours(self):
        """Ручной блок с термином и ссылкой на чужой сайт - текст человека."""
        notes = (f"{BLOCK}\nБЛОКИРУЕТ: сверить договор с юристами\n"
                 f"https://example.com/legal\n{END}\nСОХРАНИТЬ")
        self.assertEqual(ab.split_block(notes), (None, notes))

    def test_old_block_with_autolinked_url_is_recognized(self):
        """Asana превращает голый URL в якорь - старый блок выглядит иначе."""
        notes = (f"{BLOCK}\nЗАБЛОКИРОВАНА: B (открыта)\n"
                 '<a href="https://app.asana.com/0/1/2">https://app.asana.com/0/1/2</a>\n'
                 f"{END}\nхвост")
        block, rest = ab.split_block(notes)
        self.assertIsNotNone(block)
        self.assertEqual(rest.strip(), "хвост")

    def test_old_block_is_recognized_as_ours(self):
        block, rest = ab.split_block(self.old_block() + "\n\nхвост")
        self.assertIsNotNone(block)
        self.assertEqual(rest.strip(), "хвост")

    def test_old_block_is_replaced_not_duplicated(self):
        board = Board([task("1", "A", html=f"<body>{self.old_block()}\n\nхвост</body>"),
                       task("2", "B")])
        run(board, {"A": ["B"]})
        written = dict(board.puts)["1"]
        self.assertEqual(written.count(BLOCK), 1)
        self.assertIn(ab.WAITS_HEAD, written)
        self.assertNotIn("ЗАБЛОКИРОВАНА", written)
        self.assertTrue(written.endswith("хвост"))

    def test_old_block_without_links_in_config_is_removed(self):
        board = Board([task("9", "C", html=f"<body>{self.old_block('X')}\n\nхвост</body>")])
        written, err, _ = run(board, {})
        self.assertEqual((written, err), (1, False))
        self.assertEqual(board.tasks[0]["html_notes"], "<body>хвост</body>")

    def test_migration_is_idempotent(self):
        board = Board([task("1", "A", html=f"<body>{self.old_block()}</body>"),
                       task("2", "B")])
        run(board, {"A": ["B"]})
        board.puts.clear()
        written, err, _ = run(board, {"A": ["B"]})
        self.assertEqual((written, err, board.puts), (0, False, []))


class TestMentionsAndAssignee(unittest.TestCase):
    """Находка брифа: направление связи термином двусмысленно, а исполнителя
    в строке не было вовсе."""

    def test_direction_is_a_sentence_not_a_term(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        waits, holds = dict(board.puts)["1"], dict(board.puts)["2"]
        self.assertIn("ЖДЕТ", waits)
        self.assertIn("ДЕРЖИТ", holds)
        self.assertNotIn("БЛОКИРУЕТ:", holds)

    def test_task_and_assignee_are_mentions_not_text(self):
        board = Board([task("1", "A"), task("2", "B", assignee=("7", "Евгений"))])
        run(board, {"A": ["B"]})
        written = dict(board.puts)["1"]
        self.assertIn(link("2"), written)
        self.assertIn(link("7"), written)
        self.assertNotIn("Евгений", written)   # имя подставляет трекер
        self.assertNotIn("B", written.replace(BLOCK, ""))
        self.assertNotIn("https://", written)  # упоминание уже ссылка

    def test_unassigned_task_says_so(self):
        board = Board([task("1", "A"), task("2", "B", assignee=None)])
        run(board, {"A": ["B"]})
        self.assertIn("исполнитель не назначен", dict(board.puts)["1"])

    def test_status_only_where_we_are_waiting(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        self.assertIn(", открыта", dict(board.puts)["1"])
        self.assertNotIn("открыта", dict(board.puts)["2"])

    def test_preview_shows_names_not_gids(self):
        board = Board([task("1", "A"), task("2", "B", assignee=("7", "Евгений"))])
        _w, _e, out = run(board, {"A": ["B"]}, send=False)
        self.assertIn("Евгений", out)
        self.assertNotIn("data-asana-gid", out)


class TestFormattingPreserved(unittest.TestCase):
    """Раньше запись шла в notes и сплющивала чужое оформление, из-за чего
    приходилось держать отдельный гейт. С html_notes теряться нечему."""

    def test_existing_markup_survives_the_write(self):
        board = Board([task("1", "A", html="<body><ul><li>важный пункт</li></ul></body>"),
                       task("2", "B")])
        run(board, {"A": ["B"]})
        written = dict(board.puts)["1"]
        self.assertIn("<ul><li>важный пункт</li></ul>", written)
        self.assertTrue(written.startswith(ab.MARK_START))

    def test_formatting_added_after_preview_is_kept(self):
        board = Board([task("1", "A"), task("2", "B")])
        board.live_edit["1"] = {"html_notes": "<body><strong>важно</strong></body>"}
        written, err, _ = run(board, {"A": ["B"]})
        self.assertEqual(err, False)
        self.assertIn("<strong>важно</strong>", dict(board.puts)["1"])


class TestDryRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        board = Board([task("1", "A"), task("2", "B")])
        written, err, out = run(board, {"A": ["B"]}, send=False)
        self.assertEqual(board.puts, [])
        self.assertEqual((written, err), (2, False))
        self.assertIn("это предпросмотр", out)

    def test_removal_preview_shows_what_disappears(self):
        stale = f"{BLOCK}\nЗАБЛОКИРОВАНА: X (открыта)\nhttps://app.asana.com/0/1/2\n{END}\nхвост"
        board = Board([task("1", "A", stale)])
        _w, _e, out = run(board, {}, send=False)
        self.assertIn("ЗАБЛОКИРОВАНА: X", out)
        self.assertEqual(board.puts, [])


class Sections(unittest.TestCase):
    """Технический блок общий на два скрипта: свои секции заменяем, чужие
    известные сохраняем. Наивный общий реестр заголовков был бы ХУЖЕ прежнего
    состояния: узнав чужую секцию своим блоком, замена стерла бы ее."""

    def item(self, gid):
        return f'- <a data-asana-gid="{gid}"/>'

    def test_foreign_section_survives_own_rewrite(self):
        old = [ab.ORIGIN_HEAD, self.item("9"), ab.WAITS_HEAD, self.item("1")]
        out = ab.merge_sections(old, ab.OWN_HEADS, [ab.WAITS_HEAD, self.item("2")])
        self.assertIn(ab.ORIGIN_HEAD, out)
        self.assertIn(self.item("9"), out)
        self.assertIn(self.item("2"), out)
        self.assertNotIn(self.item("1"), out)

    def test_order_is_canonical_not_write_order(self):
        """Иначе два скрипта переставляли бы секции друг друга, и каждый прогон
        видел бы изменение - идемпотентности не было бы ни у одного."""
        a = ab.merge_sections([ab.ORIGIN_HEAD, self.item("9")],
                              ab.OWN_HEADS, [ab.WAITS_HEAD, self.item("1")])
        b = ab.merge_sections([ab.WAITS_HEAD, self.item("1")],
                              (ab.ORIGIN_HEAD,), [ab.ORIGIN_HEAD, self.item("9")])
        self.assertEqual(a, b)
        self.assertEqual(a.index(ab.WAITS_HEAD), 0)

    def test_empty_own_section_disappears_foreign_stays(self):
        out = ab.merge_sections([ab.WAITS_HEAD, self.item("1"), ab.ORIGIN_HEAD,
                                 self.item("9")], ab.OWN_HEADS, [])
        self.assertEqual(out, [ab.ORIGIN_HEAD, self.item("9")])

    def test_block_removed_when_nothing_left(self):
        self.assertEqual(ab.merge_sections([ab.WAITS_HEAD, self.item("1")],
                                           ab.OWN_HEADS, []), [])

    def test_unknown_heading_is_not_ours(self):
        """Расширить признак до "любая строка капсом" нельзя: описание, начатое
        человеком заглавными, было бы стерто как наш блок."""
        self.assertFalse(ab._is_ours_new("МОИ ЗАМЕТКИ:\n" + self.item("1")))

    def test_block_must_start_with_heading(self):
        self.assertFalse(ab._is_ours_new(self.item("1") + "\n" + ab.WAITS_HEAD
                                         + "\n" + self.item("2")))

    def test_duplicate_foreign_sections_merge_items(self):
        """Первая редакция "схлопывания" оставляла первую секцию и выбрасывала
        остальные - то есть чинила косметику ценой молчаливой потери связей."""
        dup = [ab.ORIGIN_HEAD, self.item("1"), ab.ORIGIN_HEAD, self.item("2")]
        out = ab.merge_sections(dup, ab.OWN_HEADS, [])
        self.assertEqual(out, [ab.ORIGIN_HEAD, self.item("1"), self.item("2")])

    def test_duplicate_items_are_not_doubled(self):
        dup = [ab.ORIGIN_HEAD, self.item("1"), ab.ORIGIN_HEAD, self.item("1")]
        self.assertEqual(ab.merge_sections(dup, ab.OWN_HEADS, []),
                         [ab.ORIGIN_HEAD, self.item("1")])

    def test_block_lines_strips_markers(self):
        txt = ab.MARK_START + "\n" + ab.WAITS_HEAD + "\n" + self.item("1") + "\n" + ab.MARK_END
        self.assertEqual(ab.block_lines(txt), [ab.WAITS_HEAD, self.item("1")])
        self.assertEqual(ab.block_lines(None), [])

    def test_round_trip_is_idempotent(self):
        lines = [ab.WAITS_HEAD, self.item("1"), ab.ORIGIN_HEAD, self.item("9")]
        once = ab.merge_sections(lines, ab.OWN_HEADS, [ab.WAITS_HEAD, self.item("1")])
        twice = ab.merge_sections(once, ab.OWN_HEADS, [ab.WAITS_HEAD, self.item("1")])
        self.assertEqual(once, twice)
        self.assertEqual(once, lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
