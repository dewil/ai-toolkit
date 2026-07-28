#!/usr/bin/env python3
"""Тесты csv-xlsx.py - CSV/TSV -> .xlsx на stdlib. stdlib-only (unittest).

Запуск: python3 tests/test_csv_xlsx.py

Ключевые инварианты: валидный OOXML-пакет; типизация ячеек консервативна
(ведущие нули и длинные id остаются текстом - молчаливая порча данных хуже
"числа как текст"); формулы по умолчанию текст (CSV injection); шапка жирная
и закрепленная; свойства файла наши (rules/document-metadata.md).
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
import xml.dom.minidom
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("csv_xlsx", SCRIPTS / "csv-xlsx.py")
cx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cx)


def build(rows, *, header=True, formulas=False, author="Автор", title="Т",
          name="Лист1"):
    return cx.build([(name, rows)], author, title, header=header, formulas=formulas)


def part(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name).decode("utf-8")


def sheet(rows, **kw) -> str:
    return part(build(rows, **kw), "xl/worksheets/sheet1.xml")


class Package(unittest.TestCase):
    def test_all_parts_are_wellformed_xml(self):
        data = cx.build(
            [("А", [["x"]]), ("Б", [["y"]])], "Автор", "Т", header=True, formulas=False
        )
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIsNone(z.testzip())
            for name in z.namelist():
                xml.dom.minidom.parseString(z.read(name))
            names = set(z.namelist())
        for required in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                         "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                         "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml",
                         "docProps/core.xml"):
            self.assertIn(required, names)

    def test_content_types_cover_every_sheet(self):
        data = cx.build([("А", [["x"]]), ("Б", [["y"]])], "a", "t",
                        header=False, formulas=False)
        ct = part(data, "[Content_Types].xml")
        self.assertIn("/xl/worksheets/sheet1.xml", ct)
        self.assertIn("/xl/worksheets/sheet2.xml", ct)

    def test_workbook_rels_reference_sheets_and_styles(self):
        data = cx.build([("А", [["x"]]), ("Б", [["y"]])], "a", "t",
                        header=False, formulas=False)
        rels = part(data, "xl/_rels/workbook.xml.rels")
        self.assertIn('Target="worksheets/sheet2.xml"', rels)
        self.assertIn('Target="styles.xml"', rels)

    def test_author_and_title_in_core_props(self):
        core = part(build([["x"]], author="И. Иванов", title="Отчет"), "docProps/core.xml")
        self.assertIn("<dc:creator>И. Иванов</dc:creator>", core)
        self.assertIn("<dc:title>Отчет</dc:title>", core)

    def test_quote_in_sheet_name_stays_valid_xml(self):
        data = cx.build([('Лист "х"', [["a"]])], "a", "t", header=False, formulas=False)
        xml.dom.minidom.parseString(part(data, "xl/workbook.xml"))


class CellTyping(unittest.TestCase):
    def test_int_and_float_are_numbers(self):
        h = sheet([["п"], ["1500"], ["-7.5"]])
        self.assertIn("<v>1500</v>", h)
        self.assertIn("<v>-7.5</v>", h)

    def test_leading_zero_stays_text(self):
        """007 числом молча потерял бы ведущие нули."""
        h = sheet([["п"], ["007"]])
        self.assertNotIn("<v>007</v>", h)
        self.assertIn("<t>007</t>", h)

    def test_sixteen_digits_stay_text(self):
        """Потолок точности double - 15 цифр; дальше Excel молча обнуляет хвост."""
        h = sheet([["п"], ["1234567890123456"]])
        self.assertNotIn("<v>1234567890123456</v>", h)
        h15 = sheet([["п"], ["123456789012345"]])
        self.assertIn("<v>123456789012345</v>", h15)

    def test_valid_date_becomes_serial_with_date_style(self):
        h = sheet([["п"], ["2026-08-01"]])
        self.assertIn(f'<c r="A2" s="{cx.XF_DATE}"><v>46235</v></c>', h)

    def test_impossible_date_stays_text(self):
        h = sheet([["п"], ["2026-02-30"]])
        self.assertIn("<t>2026-02-30</t>", h)

    def test_formula_is_text_by_default(self):
        """CSV injection: =HYPERLINK из внешнего файла не должен ожить."""
        h = sheet([["п"], ["=SUM(A1:A3)"]])
        self.assertNotIn("<f>", h)
        self.assertIn("<t>=SUM(A1:A3)</t>", h)

    def test_formula_written_with_flag(self):
        h = sheet([["п"], ["=SUM(A1:A3)"]], formulas=True)
        self.assertIn("<f>SUM(A1:A3)</f>", h)

    def test_formulas_flag_sets_full_calc_on_load(self):
        """Формулы пишутся без кэша значений - пересчет при открытии обязателен."""
        wb = part(build([["п"], ["=1+1"]], formulas=True), "xl/workbook.xml")
        self.assertIn("fullCalcOnLoad", wb)
        wb_plain = part(build([["п"], ["текст"]]), "xl/workbook.xml")
        self.assertNotIn("calcPr", wb_plain)

    def test_header_cells_never_typed(self):
        """Шапка "2026-01-01" или "=X" - названия колонок, не данные."""
        h = sheet([["2026-01-01", "=X"], ["a", "b"]], formulas=True)
        self.assertIn("<t>2026-01-01</t>", h)
        self.assertIn("<t>=X</t>", h)

    def test_padded_string_preserves_space(self):
        h = sheet([["п"], [" отступ "]])
        self.assertIn('xml:space="preserve"', h)
        self.assertIn("<t xml:space=\"preserve\"> отступ </t>", h)

    def test_control_chars_stripped_everywhere(self):
        h = sheet([["п"], ["до\x07после\x00"]])
        self.assertNotIn("\x07", h)
        self.assertIn("<t>допосле</t>", h)

    def test_ragged_rows_kept_as_is(self):
        h = sheet([["A", "B"], ["одна"]])
        self.assertIn('<row r="2"><c r="A2"', h)
        self.assertNotIn('<c r="B2"', h)


class PrecisionAndLimits(unittest.TestCase):
    """Блокеры adversarial-ревью: молчаливое искажение чисел и дат."""

    def test_sixteen_significant_digits_stay_text(self):
        """1.0000000000000001 числом молча стала бы 1."""
        h = sheet([["п"], ["1.0000000000000001"], ["0.12345678901234567890"]])
        self.assertNotIn("<v>1.0000000000000001</v>", h)
        self.assertNotIn("<v>0.12345678901234567890</v>", h)
        self.assertIn("<t>1.0000000000000001</t>", h)

    def test_fifteen_significant_digits_still_number(self):
        h = sheet([["п"], ["1.0000000000001"]])
        self.assertIn("<v>1.0000000000001</v>", h)

    def test_leading_zero_float_and_negative_zero_stay_text(self):
        """07.5 стала бы 7.5, -0 - нулем: представление меняется молча."""
        h = sheet([["п"], ["07.5"], ["-0"]])
        self.assertIn("<t>07.5</t>", h)
        self.assertIn("<t>-0</t>", h)

    def test_dates_before_march_1900_stay_text(self):
        """База 1899-12-30 до 1900-03-01 дает съехавшие серийники
        (leap-year-баг Excel), а до 1900 - отрицательные."""
        h = sheet([["п"], ["1900-01-01"], ["1900-02-28"], ["1899-12-31"]])
        for v in ("1900-01-01", "1900-02-28", "1899-12-31"):
            self.assertIn(f"<t>{v}</t>", h)
        self.assertNotIn('s="2"', h)

    def test_march_1900_still_date(self):
        h = sheet([["п"], ["1900-03-01"]])
        self.assertIn(f's="{cx.XF_DATE}"', h)

    def test_control_char_before_equals_not_a_formula(self):
        """\x01=HYPERLINK(...) визуально не формула - оживала после санитайза."""
        h = sheet([["п"], ["\x01=HYPERLINK(\"https://x\")"]], formulas=True)
        self.assertNotIn("<f>", h)

    def test_too_many_columns_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            cx.check_limits("Л", [["x"] * 16_385])
        self.assertIn("16384", str(caught.exception).replace("_", ""))

    def test_too_long_cell_rejected_with_address(self):
        with self.assertRaises(SystemExit) as caught:
            cx.check_limits("Л", [["ok", "y" * 32_768]])
        self.assertIn("B1", str(caught.exception))


class AdversarialRound2(unittest.TestCase):
    def test_negative_zero_float_stays_text(self):
        """-0.0 обходила защиту "-0": Excel показал бы 0 без минуса."""
        h = sheet([["п"], ["-0.0"], ["-0.000"]])
        self.assertIn("<t>-0.0</t>", h)
        self.assertIn("<t>-0.000</t>", h)
        self.assertNotIn("<v>-0.0</v>", h)

    def test_negative_half_still_number(self):
        h = sheet([["п"], ["-0.5"]])
        self.assertIn("<v>-0.5</v>", h)

    def test_subnormal_below_excel_range_stays_text(self):
        value = "0." + "0" * 308 + "1"
        h = sheet([["п"], [value]])
        self.assertIn(f"<t>{value}</t>", h)

    def test_significant_digits_boundary_15_16(self):
        """Граница точности: ровно 15 значащих - число, 16 - текст."""
        self.assertIsNotNone(cx.numeric_or_none("123456789.012345"))
        self.assertIsNone(cx.numeric_or_none("1234567890.123456"))

    def test_bare_cr_counts_as_line_break(self):
        """254 голых CR проходили: XML нормализует CR в LF у потребителя."""
        with self.assertRaises(SystemExit):
            cx.check_limits("Л", [["a" + "\r" * 254]])

    def test_formula_length_limit_with_flag(self):
        long_formula = "=" + "A1+" * 2731 + "A1"  # > 8192 символов тела
        with self.assertRaises(SystemExit) as caught:
            cx.check_limits("Л", [[long_formula]], formulas=True)
        self.assertIn("формула", str(caught.exception))
        cx.check_limits("Л", [[long_formula]])  # без флага это текст - лимит ячейки

    def test_build_itself_enforces_limits(self):
        """Лимиты не обходятся прямым вызовом build (мимо CLI)."""
        with self.assertRaises(SystemExit):
            cx.build([("Л", [["x"] * 16_385])], "a", "t", header=False, formulas=False)


class HeaderAndLayout(unittest.TestCase):
    def test_header_bold_and_frozen(self):
        h = sheet([["Имя"], ["Иван"]])
        self.assertIn(f'<c r="A1" t="inlineStr" s="{cx.XF_BOLD}">', h)
        self.assertIn('state="frozen"', h)
        self.assertIn('topLeftCell="A2"', h)

    def test_no_header_mode(self):
        h = sheet([["Иван"], ["Оля"]], header=False)
        self.assertNotIn(f's="{cx.XF_BOLD}"', h)
        self.assertNotIn("frozen", h)

    def test_column_width_from_content_with_ceiling(self):
        h = sheet([["к"], ["x" * 200]])
        self.assertIn('width="60"', h)
        self.assertNotIn('width="202"', h)


class SheetNames(unittest.TestCase):
    def test_forbidden_chars_replaced(self):
        self.assertEqual(cx.sheet_name("отчет [v1]: а/б", set()), "отчет -v1-- а-б")

    def test_length_capped_at_31(self):
        self.assertEqual(len(cx.sheet_name("x" * 40, set())), 31)

    def test_collision_gets_suffix_case_insensitive(self):
        taken: set[str] = set()
        first = cx.sheet_name("Data", taken)
        second = cx.sheet_name("data", taken)
        self.assertEqual(first, "Data")
        self.assertEqual(second, "data-2")

    def test_empty_stem_fallback(self):
        self.assertEqual(cx.sheet_name("", set()), "Лист")


class ColLetter(unittest.TestCase):
    def test_known_anchors(self):
        for idx, expected in ((0, "A"), (25, "Z"), (26, "AA"), (27, "AB"), (51, "AZ"), (52, "BA")):
            self.assertEqual(cx.col_letter(idx), expected)


class ReadRows(unittest.TestCase):
    def test_bom_does_not_poison_first_header_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.csv"
            p.write_bytes("﻿Имя,Сумма\nИван,1\n".encode("utf-8"))
            rows = cx.read_rows(p, None)
        self.assertEqual(rows[0][0], "Имя")

    def test_tsv_by_extension_and_explicit_delimiter(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "a.tsv"
            t.write_text("a\tb\n", encoding="utf-8")
            self.assertEqual(cx.read_rows(t, None), [["a", "b"]])
            c = Path(tmp) / "b.csv"
            c.write_text("a;b\n", encoding="utf-8")
            self.assertEqual(cx.read_rows(c, ";"), [["a", "b"]])

    def test_quoted_comma_stays_in_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.csv"
            p.write_text('x,"строка, с запятой"\n', encoding="utf-8")
            self.assertEqual(cx.read_rows(p, None)[0][1], "строка, с запятой")


class Cli(unittest.TestCase):
    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "csv-xlsx.py"), *argv],
            capture_output=True, text=True,
        )

    def test_empty_input_exits_with_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "пусто.csv"
            p.write_text("\n\n", encoding="utf-8")
            r = self.run_cli(str(p))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("пустой вход", r.stderr)

    def test_happy_path_builds_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "данные.csv"
            p.write_text("a,b\n1,2\n", encoding="utf-8")
            r = self.run_cli(str(p), "--author", "Т. Тестов")
            out = Path(tmp) / "данные.xlsx"
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(out.exists())
            self.assertIn("листов: 1", r.stdout)
            core = part(out.read_bytes(), "docProps/core.xml")
            self.assertIn("Т. Тестов", core)

    def test_missing_input_exits(self):
        r = self.run_cli("/nope/данные.csv")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("нет исходника", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
