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


def task(gid, name, notes="", completed=False, html=None):
    return {"gid": gid, "name": name, "notes": notes, "completed": completed,
            "permalink_url": f"https://app.asana.com/0/1/{gid}",
            "projects": [{"gid": PROJECT}],
            "html_notes": html if html is not None else f"<body>{notes}</body>"}


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
        return self.tasks

    def _find(self, gid):
        return next((t for t in self.tasks if t["gid"] == gid), None)

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
            return {"data": dict(cur)}
        if method != "PUT":
            raise AssertionError(f"неожиданный метод записи: {method}")
        self.puts.append((gid, payload["notes"]))
        cur["notes"] = payload["notes"]
        cur["html_notes"] = f"<body>{payload['notes']}</body>"
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
        self.assertIn("ЗАБЛОКИРОВАНА: B (открыта)", notes["1"])
        self.assertIn("БЛОКИРУЕТ: A", notes["2"])

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
        self.assertIn("ЗАБЛОКИРОВАНА: B (закрыта)", dict(board.puts)["1"])


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
        notes = (f"{BLOCK}\nЗАБЛОКИРОВАНА: B (открыта)\nhttps://x\n"
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
        board.live_edit["1"] = {"notes": "ТЗ v2, согласованная цена",
                                "html_notes": "<body>ТЗ v2, согласованная цена</body>"}
        run(board, {"A": ["B"]})
        written = dict(board.puts)["1"]
        self.assertIn("ТЗ v2, согласованная цена", written)
        self.assertIn("ЗАБЛОКИРОВАНА: B", written)

    def test_write_skipped_if_someone_already_did_it(self):
        board = Board([task("1", "A"), task("2", "B")])
        run(board, {"A": ["B"]})
        board.tasks[0]["notes"] = ""  # снимок предпросмотра устареет
        board.puts.clear()
        written, err, _ = run(board, {"A": ["B"]})
        self.assertEqual(err, False)
        self.assertEqual(written, 1)
        self.assertIn("ЗАБЛОКИРОВАНА: B", board.tasks[0]["notes"])


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
                body = ab._request("GET", "https://x/tasks/1", "tok")
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
                ab._request("GET", "https://x/tasks/1", "tok")
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
        self.assertIn("ЗАБЛОКИРОВАНА: B", notes["1"])
        self.assertIn("БЛОКИРУЕТ: A", notes["2"])

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
        stale = f"{BLOCK}\nЗАБЛОКИРОВАНА: X (открыта)\nhttps://x\n{END}\nхвост"
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
        crlf = f"{BLOCK}\r\nЗАБЛОКИРОВАНА: B (открыта)\r\nhttps://x\r\n{END}\r\n\r\nхвост"
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

    def test_new_formatting_blocks_write(self):
        board = Board([task("1", "A"), task("2", "B")])
        board.live_edit["1"] = {"html_notes": "<body><strong>важно</strong></body>",
                                "notes": ""}
        written, err, _ = run(board, {"A": ["B"]})
        self.assertNotIn("1", dict(board.puts))
        self.assertTrue(err)  # пропуск не должен выглядеть чистым прогоном

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
        done = board.tasks[0]["notes"]
        board.tasks[0]["notes"] = ""  # план построится по устаревшему снимку
        board.tasks[0]["html_notes"] = "<body></body>"
        board.live_edit["1"] = {"notes": done, "html_notes": f"<body>{done}</body>"}
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


class TestRichText(unittest.TestCase):
    def test_formatted_description_is_flagged(self):
        board = Board([task("1", "A", "текст", html="<body><ul><li>раз</li></ul></body>"),
                       task("2", "B")])
        _w, _e, out = run(board, {"A": ["B"]}, send=False)
        self.assertIn("описание оформлено", out)

    def test_plain_description_not_flagged(self):
        board = Board([task("1", "A", "текст"), task("2", "B")])
        _w, _e, out = run(board, {"A": ["B"]}, send=False)
        self.assertNotIn("описание оформлено", out)


class TestDryRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        board = Board([task("1", "A"), task("2", "B")])
        written, err, out = run(board, {"A": ["B"]}, send=False)
        self.assertEqual(board.puts, [])
        self.assertEqual((written, err), (2, False))
        self.assertIn("это предпросмотр", out)

    def test_removal_preview_shows_what_disappears(self):
        stale = f"{BLOCK}\nЗАБЛОКИРОВАНА: X (открыта)\nhttps://x\n{END}\nхвост"
        board = Board([task("1", "A", stale)])
        _w, _e, out = run(board, {}, send=False)
        self.assertIn("ЗАБЛОКИРОВАНА: X", out)
        self.assertEqual(board.puts, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
