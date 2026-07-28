#!/usr/bin/env python3
"""Тесты md-pptx.py - markdown -> .pptx на stdlib. stdlib-only (unittest).

Запуск: python3 tests/test_md_pptx.py

Ключевые инварианты: валидный OOXML-пакет (все части well-formed, rels
сходятся); раскладка markdown по слайдам (H1 - титул один раз, ## - слайд,
--- - слайд без заголовка, пустые не плодятся); буллеты двух уровней;
инлайн-разметка ранами; санитайз XML; свойства файла наши.
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
_spec = importlib.util.spec_from_file_location("md_pptx", SCRIPTS / "md-pptx.py")
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)


def deck(md: str):
    return mp.parse_md(md)


def built(md: str, author="Автор", title=None) -> bytes:
    h1, slides = mp.parse_md(md)
    return mp.build(slides, author, title or h1 or "Т")


def part(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name).decode("utf-8")


class Parser(unittest.TestCase):
    def test_h1_becomes_title_slide_with_subtitle(self):
        h1, slides = deck("# Доклад\n\nПодзаголовок\n\n## Раздел\n\n- пункт\n")
        self.assertEqual(h1, "Доклад")
        self.assertTrue(slides[0].get("is_title"))
        self.assertEqual(slides[0]["paras"][0][1][0][0], "Подзаголовок")
        self.assertEqual(slides[1]["title"], "Раздел")

    def test_second_h1_is_regular_slide(self):
        """Титул один: второй H1 не должен плодить второй титульный."""
        _, slides = deck("# Один\n\n# Два\n")
        self.assertTrue(slides[0].get("is_title"))
        self.assertFalse(slides[1].get("is_title"))
        self.assertEqual(slides[1]["title"], "Два")

    def test_hr_starts_untitled_slide(self):
        _, slides = deck("## А\n\nтекст\n\n---\n\nВопросы?\n")
        self.assertEqual(len(slides), 2)
        self.assertIsNone(slides[1]["title"])
        self.assertEqual(slides[1]["paras"][0][1][0][0], "Вопросы?")

    def test_consecutive_hr_do_not_create_empty_slides(self):
        _, slides = deck("## А\n\nтекст\n\n---\n\n---\n\nконец\n")
        self.assertEqual(len(slides), 2)

    def test_content_before_any_heading(self):
        _, slides = deck("просто текст\n")
        self.assertEqual(len(slides), 1)
        self.assertIsNone(slides[0]["title"])

    def test_bullets_two_levels(self):
        _, slides = deck("## С\n\n- верх\n  - вложенный\n1. нумерованный\n")
        paras = slides[0]["paras"]
        self.assertEqual(paras[0][0], 0)
        self.assertEqual(paras[1][0], 1)
        self.assertEqual(paras[2][0], 0)
        self.assertEqual(paras[2][1][0][0], "нумерованный")

    def test_h3_becomes_bold_line(self):
        _, slides = deck("## С\n\n### Подраздел\n")
        lvl, runs = slides[0]["paras"][0]
        self.assertIsNone(lvl)
        self.assertEqual(runs, [("Подраздел", "b")])

    def test_empty_input(self):
        h1, slides = deck("\n\n")
        self.assertIsNone(h1)
        self.assertEqual(slides, [])


class AdversarialRound1(unittest.TestCase):
    """Регрессы на находки состязательного ревью."""

    def test_pres_props_part_present_and_linked(self):
        """Блокер: presProps - обязательная часть по ISO 29500; python-pptx
        файл без нее читал, а строгий потребитель вправе чинить."""
        data = built("## С\n\n- п\n")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn("ppt/presProps.xml", z.namelist())
        self.assertIn("presProps.xml", part(data, "ppt/_rels/presentation.xml.rels"))
        self.assertIn("/ppt/presProps.xml", part(data, "[Content_Types].xml"))

    def test_fenced_code_is_not_structure(self):
        """## и --- внутри ``` перекраивали презентацию."""
        md = "## Код\n\n```\n## не заголовок\n---\nx = 1\n```\n"
        _, slides = deck(md)
        self.assertEqual(len(slides), 1)
        texts = [runs[0][0] for _, runs in slides[0]["paras"]]
        self.assertIn("## не заголовок", texts)
        self.assertIn("---", texts)
        self.assertIn("x = 1", texts)
        styles = {runs[0][1] for _, runs in slides[0]["paras"]}
        self.assertEqual(styles, {"c"}, "содержимое кода - моноширинным")

    def test_hashtag_line_is_kept_as_text(self):
        """#hashtag и ##без-пробела теряли решетки и превращались в жирные."""
        _, slides = deck("## С\n\n#hashtag\n##не заголовок\n")
        texts = [runs[0] for _, runs in slides[0]["paras"]]
        self.assertIn(("#hashtag", ""), texts)
        self.assertIn(("##не заголовок", ""), texts)
        self.assertEqual(len(slides), 1)

    def test_escaped_markers_stay_literal(self):
        runs = mp.runs_of(r"до \*лит\* после")
        self.assertEqual(runs, [("до *лит* после", "")])

    def test_title_slide_keeps_bullet_markers(self):
        """Буллеты подтитула молча теряли маркеры и вложенность."""
        data = built("# Т\n\n- родитель\n  - ребенок\n")
        s1 = part(data, "ppt/slides/slide1.xml")
        self.assertIn('char="•"', s1)
        self.assertIn('char="-"', s1)

    def test_run_with_edge_space_preserved(self):
        data = built("## С\n\nдо **жирный** после\n")
        s1 = part(data, "ppt/slides/slide1.xml")
        self.assertIn('xml:space="preserve"', s1)


class AdversarialRound2(unittest.TestCase):
    def test_fence_closer_must_match_char_and_length(self):
        """~~~ закрывала ```-блок, тройной бэктик закрывал четверной."""
        md = "## С\n\n````\n~~~\n```\ncode\n````\nпосле\n"
        _, slides = deck(md)
        self.assertEqual(len(slides), 1)
        texts = [runs[0][0] for _, runs in slides[0]["paras"]]
        self.assertIn("~~~", texts)
        self.assertIn("```", texts)
        self.assertIn("code", texts)
        self.assertIn("после", texts)

    def test_fence_closer_with_trailing_text_is_content(self):
        md = "## С\n\n```\n```oops\nx\n```\n"
        _, slides = deck(md)
        texts = [runs[0][0] for _, runs in slides[0]["paras"]]
        self.assertIn("```oops", texts)

    def test_fence_preserves_blank_lines_and_indent(self):
        """Пустые строки выбрасывались, отступы съедались - код портился."""
        md = "## С\n\n```\ndef f():\n\n    return 1\n```\n"
        _, slides = deck(md)
        paras = slides[0]["paras"]
        texts = [runs[0][0] if runs else "" for _, runs in paras]
        self.assertIn("    return 1", texts, "отступ кода обязан уцелеть")
        self.assertEqual(len(paras), 3, "пустая строка кода - тоже строка")

    def test_heading_unescapes_markers(self):
        r"""## \*T\* показывал бэкслеши: заголовки идут мимо runs_of."""
        _, slides = deck("## \\*T\\*\n\nтекст\n")
        self.assertEqual(slides[0]["title"], "*T*")

    def test_code_span_keeps_backslash_literal(self):
        r"""Внутри `кода` бэкслеш - литерал, преобработка эскейпов его съедала."""
        runs = mp.runs_of("до `a\\*b` после")
        self.assertIn(("a\\*b", "c"), runs)


class Runs(unittest.TestCase):
    def test_bold_italic_code_mix(self):
        runs = mp.runs_of("до **жирный** и *курсив* и `код` после")
        self.assertIn(("жирный", "b"), runs)
        self.assertIn(("курсив", "i"), runs)
        self.assertIn(("код", "c"), runs)
        self.assertEqual(runs[0], ("до ", ""))

    def test_unpaired_marker_stays_literal(self):
        runs = mp.runs_of("2 * 3 = 6")
        self.assertEqual(runs, [("2 * 3 = 6", "")])


class SlideXmlAndPackage(unittest.TestCase):
    MD = "# Т\n\nподтитул\n\n## Слайд\n\n- **пункт**\n  - вложенный\n\nабзац\n"

    def test_all_parts_wellformed_and_linked(self):
        data = built(self.MD)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIsNone(z.testzip())
            names = set(z.namelist())
            for name in names:
                xml.dom.minidom.parseString(z.read(name))
        self.assertIn("ppt/slides/slide2.xml", names)
        self.assertIn("ppt/slides/_rels/slide2.xml.rels", names)
        ct = part(data, "[Content_Types].xml")
        self.assertIn("/ppt/slides/slide2.xml", ct)
        pres_rels = part(data, "ppt/_rels/presentation.xml.rels")
        self.assertIn("slides/slide2.xml", pres_rels)
        self.assertIn("slideMasters/slideMaster1.xml", pres_rels)

    def test_presentation_lists_every_slide(self):
        pres = part(built(self.MD), "ppt/presentation.xml")
        self.assertEqual(pres.count("<p:sldId "), 2)

    def test_title_slide_layout(self):
        s1 = part(built(self.MD), "ppt/slides/slide1.xml")
        self.assertIn('sz="4000"', s1)
        self.assertIn("подтитул", s1)
        self.assertIn('algn="ctr"', s1)

    def test_section_slide_title_and_bullets(self):
        s2 = part(built(self.MD), "ppt/slides/slide2.xml")
        self.assertIn('sz="2800" b="1"', s2)
        self.assertIn('char="•"', s2)
        self.assertIn('char="-"', s2)
        self.assertIn('b="1"', s2)

    def test_plain_paragraph_has_no_bullet(self):
        s2 = part(built(self.MD), "ppt/slides/slide2.xml")
        self.assertIn("<a:buNone/>", s2)

    def test_autofit_enabled(self):
        """Слайд не резиновый - переполнение должно ужиматься, не обрезаться."""
        self.assertIn("<a:normAutofit/>", part(built(self.MD), "ppt/slides/slide2.xml"))

    def test_escaping_and_control_chars(self):
        data = built("## X\n\nтекст <b> & Co\x07\n")
        s1 = part(data, "ppt/slides/slide1.xml")
        self.assertIn("текст &lt;b&gt; &amp; Co", s1)
        self.assertNotIn("\x07", s1)

    def test_author_and_title_in_core_props(self):
        core = part(built(self.MD, author="И. Иванов"), "docProps/core.xml")
        self.assertIn("<dc:creator>И. Иванов</dc:creator>", core)
        self.assertIn("<dc:title>Т</dc:title>", core)


class Cli(unittest.TestCase):
    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "md-pptx.py"), *argv],
            capture_output=True, text=True,
        )

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "доклад.md"
            src.write_text("# Т\n\n## С\n\n- п\n", encoding="utf-8")
            r = self.run_cli(str(src), "--author", "Т. Тестов")
            self.assertEqual(r.returncode, 0, r.stderr)
            out = Path(tmp) / "доклад.pptx"
            self.assertTrue(out.exists())
            self.assertIn("слайдов: 2", r.stdout)
            self.assertIn("Т. Тестов", part(out.read_bytes(), "docProps/core.xml"))

    def test_empty_source_exits_with_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "пусто.md"
            src.write_text("\n", encoding="utf-8")
            r = self.run_cli(str(src))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("пустой исходник", r.stderr)

    def test_missing_source_exits(self):
        r = self.run_cli("/nope/д.md")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("нет исходника", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
