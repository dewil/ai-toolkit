#!/usr/bin/env python3
"""Тесты мини-конвертера markdown -> HTML из md-pdf.py. stdlib-only (unittest).

Запуск: python3 tests/test_md_pdf.py

Покрывают два блока: GFM-таблицы и мягкий перенос абзацев. Каждый кейс -
регресс на находку внешнего аудита кода (codex, 2026-07-21). Самая тяжелая из
них: строка данных с ячейкой-прочерком "| - |" отсеивалась как разделитель и
бесследно исчезала из готового PDF.
"""
from __future__ import annotations

import importlib.util
import unittest
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

    def test_ragged_rows_normalized_to_header_width(self):
        h = body("| A | B |\n|---|---|\n| x |\n| y | z | extra |\n")
        self.assertIn("<tr><td>x</td><td></td></tr>", h)
        self.assertIn("<tr><td>y</td><td>z</td></tr>", h)
        self.assertNotIn("extra", h)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
