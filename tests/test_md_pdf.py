#!/usr/bin/env python3
"""Тесты мини-конвертера markdown -> HTML из md-pdf.py. stdlib-only (unittest).

Запуск: python3 tests/test_md_pdf.py

Покрывают два блока: GFM-таблицы и мягкий перенос абзацев. Каждый кейс -
регресс на находку внешнего аудита кода (codex, 2026-07-21). Самая тяжелая из
них: строка данных с ячейкой-прочерком "| - |" отсеивалась как разделитель и
бесследно исчезала из готового PDF.
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

MD_PDF = Path(__file__).resolve().parent.parent / "scripts" / "md-pdf.py"
_spec = importlib.util.spec_from_file_location("md_pdf", MD_PDF)
md_pdf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(md_pdf)


def body(md: str) -> str:
    """HTML-тело без заголовка."""
    return md_pdf.md_to_html(md)[1]


class Tables(unittest.TestCase):
    def test_basic_table(self):
        h = body("| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("<tr><th>A</th><th>B</th></tr>", h)
        self.assertIn("<tr><td>1</td><td>2</td></tr>", h)
        self.assertEqual(h.count("<table>"), 1)

    def test_dash_cell_row_survives(self):
        """Прочерк - обычный способ написать "нет данных"; строка обязана уцелеть."""
        h = body("| Value |\n|---|\n| - |\n| ok |\n")
        self.assertIn("<tr><td>-</td></tr>", h)
        self.assertIn("<tr><td>ok</td></tr>", h)
        self.assertEqual(h.count("<tr>"), 3)

    def test_colon_only_cells_survive(self):
        """Те же символы, что у разделителя, но в строке данных."""
        h = body("| V |\n|---|\n| : |\n| :- |\n")
        self.assertIn("<td>:</td>", h)
        self.assertIn("<td>:-</td>", h)

    def test_no_delimiter_is_not_a_table(self):
        h = body("| not a table\n")
        self.assertNotIn("<table>", h)
        self.assertIn("<p>| not a table</p>", h)

    def test_prose_starting_with_pipe_is_paragraph(self):
        h = body("|x| denotes absolute value.\n")
        self.assertNotIn("<table>", h)
        self.assertIn("<p>|x| denotes absolute value.</p>", h)

    def test_short_row_padded_to_header_width(self):
        h = body("| A | B |\n|---|---|\n| x |\n")
        self.assertIn("<tr><td>x</td><td></td></tr>", h)

    def test_wide_row_keeps_text_in_last_column(self):
        """Лишние ячейки склеиваются в последнюю колонку, а не отбрасываются.

        Регресс на находку adversarial-ревью (codex, 2026-07-26): строка шире
        шапки молча теряла хвост, и текст исчезал из документа, ушедшего
        заказчику. Склейка уродливее, чем ровная таблица, но заметна - в
        отличие от пропажи.
        """
        h = body("| A | B |\n|---|---|\n| y | z | штраф 10% |\n")
        self.assertIn("<tr><td>y</td><td>z штраф 10%</td></tr>", h)
        self.assertIn("штраф 10%", h)

    def test_wide_row_does_not_activate_markup_across_cells(self):
        """Склейка лишних ячеек не должна собирать разметку, разорванную
        границей ячеек: иначе в документе появится ссылка, которой не было."""
        h = body("| A | B |\n|---|---|\n| x | [НЕ ССЫЛКА | ](https://evil.example) |\n")
        self.assertIn("[НЕ ССЫЛКА ](", h)
        self.assertNotIn('<a href="https://evil.example">НЕ ССЫЛКА', h)

    def test_escaped_pipe_in_cell(self):
        h = body("| Expr |\n|---|\n| a \\| b |\n")
        self.assertIn("<td>a | b</td>", h)

    def test_escaped_backslash_before_pipe_splits(self):
        """Удвоенный бэкслеш экранирует сам себя, значит следующий | - настоящий
        разделитель. Lookbehind на один символ здесь ошибался и склеивал ячейки."""
        h = body("| A | B | C |\n|---|---|---|\n| x \\\\| y | z |\n")
        self.assertIn("<td>x \\</td><td>y</td><td>z</td>", h)

    def test_empty_trailing_cell_preserved(self):
        h = body("| A | B |\n|---|---|\n| x ||\n")
        self.assertIn("<tr><td>x</td><td></td></tr>", h)

    def test_adjacent_tables_keep_all_data(self):
        """Без пустой строки это один table-блок (GFM), но данные не теряются."""
        h = body("| A |\n|---|\n| one |\n| B |\n|---|\n| two |\n")
        for value in ("one", "B", "---", "two"):
            self.assertIn(f"<td>{value}</td>", h)

    def test_alignment_delimiters_accepted(self):
        h = body("| A | B |\n|:---|---:|\n| 1 | 2 |\n")
        self.assertIn("<table>", h)
        self.assertIn("<tr><td>1</td><td>2</td></tr>", h)

    def test_cells_are_html_escaped(self):
        h = body("| A |\n|---|\n| <script> |\n")
        self.assertIn("&lt;script&gt;", h)
        self.assertNotIn("<script>", h)

    def test_header_delimiter_width_mismatch_is_not_a_table(self):
        """По GFM шапка и разделитель обязаны совпадать по числу ячеек."""
        h = body("| A | B |\n|---|\n| x | y |\n")
        self.assertNotIn("<table>", h)

    def test_invalid_block_becomes_one_paragraph(self):
        """Не таблица -> один абзац, а не абзац на строку: иначе рвется перенос."""
        h = body("| not a table\n| second line\n")
        self.assertEqual(h.count("<p>"), 1)
        self.assertIn("| not a table | second line", h)

    def test_table_without_trailing_pipe(self):
        h = body("| A | B\n|---|---\n| 1 | 2\n")
        self.assertIn("<tr><th>A</th><th>B</th></tr>", h)
        self.assertIn("<tr><td>1</td><td>2</td></tr>", h)


class SoftWrap(unittest.TestCase):
    def test_lines_join_into_one_paragraph(self):
        h = body("Первая строка\nвторая строка\nтретья.\n")
        self.assertEqual(h, "<p>Первая строка вторая строка третья.</p>")

    def test_inline_markup_across_line_break(self):
        """inline() обязан применяться ПОСЛЕ склейки, иначе разметка не соберется."""
        h = body("This is **bold\ntext**.\n")
        self.assertIn("<strong>bold text</strong>", h)

    def test_link_across_line_break(self):
        h = body("See [the\nsite](https://example.com).\n")
        self.assertIn('<a href="https://example.com">the site</a>', h)

    def test_blank_line_splits_paragraphs(self):
        h = body("Первый.\n\nВторой.\n")
        self.assertEqual(h.count("<p>"), 2)


class ModeInteraction(unittest.TestCase):
    def test_table_after_heading_and_list(self):
        h = body("# H\n\n- item\n\n| A |\n|---|\n| 1 |\n")
        self.assertIn("<h1>H</h1>", h)
        self.assertIn("<li>item</li>", h)
        self.assertIn("</ul>", h)
        self.assertIn("<table>", h)

    def test_paragraph_after_table_closes_it(self):
        h = body("| A |\n|---|\n| 1 |\n\nтекст после\n")
        self.assertIn("</table>", h)
        self.assertIn("<p>текст после</p>", h)
        self.assertLess(h.index("</table>"), h.index("<p>текст после</p>"))

    def test_pipes_inside_fenced_block_stay_code(self):
        h = body("```\n| A |\n|---|\n```\n")
        self.assertNotIn("<table>", h)
        self.assertIn("<pre><code>", h)

    def test_table_cannot_interrupt_paragraph(self):
        """По GFM таблица не прерывает абзац. Иначе строка "|x| - модуль числа"
        посреди текста рвала бы абзац на три куска."""
        h = body("before\n| first\nafter\n")
        self.assertNotIn("<table>", h)
        self.assertEqual(h, "<p>before | first after</p>")

    def test_hr_still_works(self):
        h = body("до\n\n---\n\nпосле\n")
        self.assertIn("<hr>", h)
        self.assertNotIn("<table>", h)

    def test_quote_not_merged_into_paragraph(self):
        h = body("> цитата\n\nабзац\n")
        self.assertIn("<blockquote>", h)
        self.assertIn("</blockquote>", h)
        self.assertIn("<p>абзац</p>", h)


class InlineCode(unittest.TestCase):
    """Регресс на находку adversarial-ревью (codex, 2026-07-26): содержимое
    `code` повторно разбиралось как markdown, и "`[x](y)`" теряло скобки с url."""

    def test_link_inside_code_stays_literal(self):
        h = body("`[literal](https://wrong.example)`\n")
        self.assertIn("<code>[literal](https://wrong.example)</code>", h)
        self.assertNotIn("<a href", h)

    def test_bold_inside_code_stays_literal(self):
        self.assertIn("<code>a **b** c</code>", body("`a **b** c`\n"))

    def test_bare_url_inside_code_not_linked(self):
        h = body("`см. https://e.com/x`\n")
        self.assertIn("<code>см. https://e.com/x</code>", h)
        self.assertNotIn("<a href", h)

    def test_code_glued_to_url_stays_outside_href(self):
        """Сентинел кода, приклеившийся к URL, не должен попасть внутрь href.
        Регресс на дефект, внесенный самим stash-механизмом (раунд 2)."""
        h = body("https://e.test/`path`\n")
        self.assertIn('<a href="https://e.test/">', h)
        self.assertIn("<code>path</code>", h)
        self.assertNotIn("<code>", h[: h.index("</a>")])

    def test_code_in_explicit_link_url_stays_out_of_href(self):
        """Сентинел кода не должен уезжать в href явной ссылки: там он
        разворачивался в <code> внутри атрибута, и ссылка вела в никуда
        (блокер раунда 3). Ссылка не собирается - текст остается буквальным."""
        h = body("[x](https://e.test/`path`)\n")
        self.assertNotIn("<code>", h[h.index("<a href") : h.index(">", h.index("<a href"))])
        self.assertNotIn('href="https://e.test/<code>', h)
        self.assertIn("<code>path</code>", h)

    def test_code_in_link_text_is_kept(self):
        """Обратная сторона: в ТЕКСТЕ ссылки код законен и должен работать."""
        self.assertIn('<a href="https://e.com"><code>код</code></a>', body("[`код`](https://e.com)\n"))

    def test_code_never_lands_in_img_attributes(self):
        """alt и src - атрибуты, тега в них быть не должно."""
        for md in ("![`alt`](pic.png)\n", "![alt](pic`x`.png)\n"):
            h = body(md)
            self.assertNotIn('alt="<code>', h)
            self.assertNotIn('src="pic<code>', h)

    def test_plain_image_still_works(self):
        self.assertIn('<img src="pic.png" alt="схема">', body("![схема](pic.png)\n"))

    def test_url_inside_link_label_keeps_single_target(self):
        """Вся метка явной ссылки ведет на один заданный адрес: autolink не
        должен создавать вложенный <a> внутри уже собранной ссылки."""
        h = body("[документ https://old.example конец](https://new.example)\n")
        self.assertEqual(h.count("<a href"), 1)
        self.assertIn('<a href="https://new.example">документ https://old.example конец</a>', h)

    def test_code_and_link_side_by_side(self):
        h = body("`код` и [ссылка](https://e.com)\n")
        self.assertIn("<code>код</code>", h)
        self.assertIn('<a href="https://e.com">ссылка</a>', h)

    def test_sentinel_injection_from_source(self):
        """Сентинелы подмены, пришедшие из исходника, не должны ни подставлять
        чужой фрагмент, ни ронять скрипт (IndexError). Регресс на регрессию,
        внесенную фиксом самого stash-механизма."""
        self.assertEqual(body("\x021\x03 без кода\n"), "<p>1 без кода</p>")
        h = body("текст \x020\x03 и `код`\n")
        self.assertEqual(h.count("<code>"), 1)
        self.assertIn("<code>код</code>", h)

    def test_sentinel_chars_do_not_leak(self):
        h = body("`код` текст\n")
        self.assertNotIn("\x02", h)
        self.assertNotIn("\x03", h)

class FindChrome(unittest.TestCase):
    """Резолв Chrome: скрипт раскатан и на macOS, и на Linux."""

    @contextlib.contextmanager
    def resolver(self, *, env=None, candidates=(), on_path=()):
        """Изолирует find_chrome от реальной машины: env, кандидаты, PATH."""
        original = (md_pdf.CHROME_CANDIDATES, shutil.which)
        md_pdf.CHROME_CANDIDATES = tuple(candidates)
        # резолв обязан вернуть ДРУГУЮ строку, иначе тест на env-имя зеленеет
        # и без which: имя команды совпало бы с ожидаемым значением
        shutil.which = lambda name: f"/resolved/{name}" if name in on_path else None
        patch = {"MD_PDF_CHROME": env} if env else {}
        with unittest.mock.patch.dict(os.environ, patch, clear=not env):
            try:
                yield
            finally:
                md_pdf.CHROME_CANDIDATES, shutil.which = original

    def test_env_wins_over_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "my-chrome"
            fake.write_text("", encoding="utf-8")
            with self.resolver(env=str(fake), candidates=("/usr/bin/google-chrome",)):
                self.assertEqual(md_pdf.find_chrome(), str(fake))

    def test_env_as_command_name_resolves_through_path(self):
        """MD_PDF_CHROME=google-chrome раньше давал "не найден Chrome"."""
        with self.resolver(env="google-chrome", on_path=("google-chrome",)):
            self.assertEqual(md_pdf.find_chrome(), "/resolved/google-chrome")

    def test_env_kept_verbatim_when_not_in_path(self):
        """Несуществующий путь из env не подменяется - иначе ошибка соврет."""
        with self.resolver(env="/nope/chrome"):
            self.assertEqual(md_pdf.find_chrome(), "/nope/chrome")

    def test_first_existing_candidate_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "first", Path(tmp) / "second"
            second.write_text("", encoding="utf-8")
            with self.resolver(candidates=("/nope/chrome", str(first), str(second))):
                self.assertEqual(md_pdf.find_chrome(), str(second))

    def test_falls_back_to_path(self):
        """Страховка для snap/flatpak и нестандартного префикса."""
        with self.resolver(candidates=("/nope/chrome",), on_path=("chromium",)):
            self.assertEqual(md_pdf.find_chrome(), "/resolved/chromium")

    def test_empty_when_nothing_found(self):
        with self.resolver(candidates=("/nope/chrome",)):
            self.assertEqual(md_pdf.find_chrome(), "")

    def test_linux_paths_are_covered(self):
        """Регресс: до правки в списке был только путь macOS."""
        self.assertIn("/usr/bin/google-chrome", md_pdf.CHROME_CANDIDATES)
        self.assertIn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                      md_pdf.CHROME_CANDIDATES)


class Photo(unittest.TestCase):
    """--photo: фото в углу первой страницы без правки markdown-исходника."""

    def test_resolve_photo_next_to_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "cv"
            src_dir.mkdir()
            (src_dir / "фото.jpg").write_bytes(b"\xff\xd8\xff")
            got = md_pdf.resolve_photo(Path("фото.jpg"), src_dir)
        self.assertEqual(got, (src_dir / "фото.jpg").resolve())

    def test_resolve_photo_as_given_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "фото.jpg"
            p.write_bytes(b"\xff\xd8\xff")
            got = md_pdf.resolve_photo(p, Path(tmp) / "нет-такой")
        self.assertEqual(got, p.resolve())

    def test_resolve_photo_missing_exits_before_chrome(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as cm:
                md_pdf.resolve_photo(Path("нет.jpg"), Path(tmp))
        self.assertIn("нет файла фото", str(cm.exception))

    def test_quote_in_photo_path_rejected(self):
        """src вставляется в атрибут без эскейпа - кавычка дала бы битый <img>."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'фо"то.jpg'
            p.write_bytes(b"\xff\xd8\xff")
            with self.assertRaises(SystemExit) as cm:
                md_pdf.resolve_photo(p, Path(tmp))
        self.assertIn("кавычка", str(cm.exception))

    def test_photo_css_appends_dimensions(self):
        css = md_pdf.PHOTO_CSS.format(width="30mm", height="38mm")
        self.assertIn("width: 30mm", css)
        self.assertIn("height: 38mm", css)
        self.assertIn("object-fit: cover", css)
        self.assertIn("float: right", css)

    def test_absolute_photo_src_embedded_regardless_of_base(self):
        """Ключевое свойство вставки: абсолютный src втягивается в data-URI,
        даже когда base (каталог исходника) - другая папка."""
        with tempfile.TemporaryDirectory() as tmp:
            p = (Path(tmp) / "фото.jpg").resolve()
            p.write_bytes(b"\xff\xd8\xff")
            body = f'<img class="photo" src="{p}"><h1>CV</h1>'
            out = md_pdf.embed_images(body, Path(tmp) / "совсем-другая")
        self.assertIn("data:image/jpeg;base64,", out)
        self.assertNotIn(str(p), out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
