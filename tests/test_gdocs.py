#!/usr/bin/env python3
"""Тесты gdocs.py - ведение живого документа в Google Docs.
stdlib-only (unittest), сеть не трогается: на вход подается ответ Docs API.

Запуск: python3 tests/test_gdocs.py

Проверяют то, что в самом документе видно только человеку: расхождение текста
и разметки (заголовок по тексту, обычный по стилю - и наоборот), унаследованный
кегль, дыры в нумерации и ссылки в никуда. Все эти состояния API отдает как
исправные.

Отдельно закреплен инвариант dry-run == send: план правки строится одной
функцией (normalize_plan / blanks_plan), и предпросмотр не может разойтись с
действием.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("gdocs", SCRIPTS / "gdocs.py")
gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gd)


def para(text, style="NORMAL_TEXT", sized=False, start=None, end=None):
    """Абзац в том виде, в каком его отдает documents.get."""
    run = {"textRun": {"content": text + "\n",
                       "textStyle": {"fontSize": {"magnitude": 16}} if sized else {}}}
    return {"paragraph": {"elements": [run],
                          "paragraphStyle": {"namedStyleType": style}},
            "startIndex": start if start is not None else 1,
            "endIndex": end if end is not None else 1 + len(text) + 1}


def sized_para(text, magnitude, style="NORMAL_TEXT"):
    return {"paragraph": {"paragraphStyle": {"namedStyleType": style},
            "elements": [{"textRun": {"content": text + "\n",
                          "textStyle": {"fontSize": {"magnitude": magnitude}}}}]}}


def doc(*elements, title="T"):
    """Документ с последовательными индексами - как их считает Docs."""
    out, idx = [], 1
    for el in elements:
        if "paragraph" in el:
            # абзац бывает и без textRun - картинка, разрыв, сноска
            length = len("".join((r.get("textRun") or {}).get("content", "")
                                 for r in el["paragraph"]["elements"])) or 2
            el = dict(el, startIndex=idx, endIndex=idx + length)
            idx += length
        out.append(el)
    return {"title": title, "body": {"content": out}}


def paras(*elements):
    return gd.paragraphs(doc(*elements))


class Heading(unittest.TestCase):
    """Заголовок опознается по ТЕКСТУ, а не по разметке: разметка и есть то,
    что врет - она наследуется от точки вставки."""

    def head(self, text):
        return gd.Para(text, 1, 2, "NORMAL_TEXT", []).heading()

    def test_levels(self):
        self.assertEqual(self.head("1. ОБЩИЕ ПОЛОЖЕНИЯ"), ("1", 1))
        self.assertEqual(self.head("10.7. ЭТАП СДАЧИ"), ("10.7", 2))
        self.assertEqual(self.head("1.2.3. ГЛУБОКИЙ"), ("1.2.3", 3))

    def test_lowercase_is_not_heading(self):
        """Иначе обычный нумерованный пункт уехал бы в структуру документа."""
        self.assertIsNone(self.head("1. Общие положения"))
        self.assertIsNone(self.head("2. текст пункта"))

    def test_plain_text_is_not_heading(self):
        for t in ("ПРОСТО КАПСОМ", "см. 10.7 для деталей", "", "10.7"):
            with self.subTest(t=t):
                self.assertIsNone(self.head(t))


class Normalize(unittest.TestCase):
    def test_body_styled_as_heading_is_caught(self):
        """Главный дефект: заменил заголовок на "заголовок + тело" - тело
        унаследовало стиль и уехало в структуру."""
        plan = gd.normalize_plan(paras(para("Обычный текст.", "HEADING_1")))
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][2], "NORMAL_TEXT")

    def test_heading_without_markup_is_caught(self):
        plan = gd.normalize_plan(paras(para("1. РАЗДЕЛ", "NORMAL_TEXT")))
        self.assertEqual([(p.text, want) for p, _w, want in plan],
                         [("1. РАЗДЕЛ", "HEADING_1")])

    def test_inherited_font_size_is_caught(self):
        """Абзац уже обычный, но с кеглем заголовка - 16pt вместо 11."""
        plan = gd.normalize_plan(paras(para("Тело.", "NORMAL_TEXT", sized=True)))
        self.assertEqual(len(plan), 1)
        self.assertIn("кегль", plan[0][1])

    def test_correct_document_needs_nothing(self):
        self.assertEqual(gd.normalize_plan(paras(
            para("1. РАЗДЕЛ", "HEADING_1"),
            para("1.1. ПОДРАЗДЕЛ", "HEADING_2"),
            para("Тело подраздела."),
            para(""))), [])

    def test_blank_paragraph_ignored(self):
        """Пустые строки - зона blanks, не normalize: чинить их стилем значит
        драться двум командам за один абзац."""
        self.assertEqual(gd.normalize_plan(paras(para("", "HEADING_1"))), [])

    def test_deep_level_gets_heading3(self):
        plan = gd.normalize_plan(paras(para("1.2.3. ГЛУБОКИЙ", "NORMAL_TEXT")))
        self.assertEqual(plan[0][2], "HEADING_3")


class Blanks(unittest.TestCase):
    """Единого правила нет: привести весь документ к одному виду значит
    испортить половину. Тип раздела определяется по его подпунктам."""

    def test_blocky_section_wants_blank_before_subheading(self):
        extra, missing = gd.blanks_plan(paras(
            para("1. РАЗДЕЛ", "HEADING_1"),
            para("1.1. ПЕРВЫЙ", "HEADING_2"),
            para("Тело первого."),
            para("1.2. ВТОРОЙ", "HEADING_2"),
            para("Тело второго.")))
        self.assertEqual(extra, [])
        self.assertEqual([p.text for p in missing], ["1.2. ВТОРОЙ"])

    def test_list_section_wants_no_blanks(self):
        extra, missing = gd.blanks_plan(paras(
            para("2. ПЕРЕЧЕНЬ", "HEADING_1"),
            para("2.1. ПУНКТ", "HEADING_2"),
            para(""),
            para("2.2. ПУНКТ", "HEADING_2")))
        self.assertEqual(missing, [])
        self.assertEqual(len(extra), 1)

    def test_section_without_subpoints_untouched(self):
        self.assertEqual(gd.blanks_plan(paras(
            para("3. РАЗДЕЛ", "HEADING_1"), para("Просто текст."))), ([], []))


class Check(unittest.TestCase):
    def test_gap_in_numbering(self):
        problems = gd.check_report(paras(
            para("10. ЭТАПЫ"), para("10.5. А"), para("10.6. Б"), para("10.8. Г")))
        self.assertTrue(any("10.7" in p for p in problems), problems)

    def test_dead_cross_reference(self):
        problems = gd.check_report(paras(
            para("1. РАЗДЕЛ"), para("Текст, см. 10.7 для деталей.")))
        self.assertTrue(any("ссылка в никуда" in p for p in problems), problems)

    def test_live_reference_is_not_reported(self):
        problems = gd.check_report(paras(
            para("10. ЭТАПЫ"), para("10.7. ЭТАП"), para("Текст, см. 10.7.")))
        self.assertEqual([p for p in problems if "ссылка" in p], [])

    def test_duplicate_number(self):
        problems = gd.check_report(paras(
            para("1. РАЗДЕЛ"), para("1.1. А"), para("1.1. Б")))
        self.assertTrue(any("повтор" in p for p in problems), problems)

    def test_clean_document(self):
        self.assertEqual(gd.check_report(paras(
            para("1. РАЗДЕЛ"), para("1.1. А"), para("1.2. Б"))), [])


class Snapshot(unittest.TestCase):
    def test_heading_levels(self):
        md = gd.to_markdown(doc(para("1. РАЗДЕЛ", "HEADING_1"),
                                para("1.1. ПОД", "HEADING_2"),
                                para("Текст.")))
        self.assertIn("## 1. РАЗДЕЛ", md)
        self.assertIn("### 1.1. ПОД", md)
        self.assertIn("\nТекст.", md)

    def test_table_rendered(self):
        table = {"table": {"tableRows": [
            {"tableCells": [{"content": [para("Этап")]}, {"content": [para("Срок")]}]},
            {"tableCells": [{"content": [para("Сдача")]}, {"content": [para("01.09")]}]}]}}
        md = gd.to_markdown(doc(para("1. РАЗДЕЛ", "HEADING_1"), table))
        self.assertIn("| Этап | Срок |", md)
        self.assertIn("| Сдача | 01.09 |", md)

    def test_pipe_in_cell_escaped(self):
        table = {"table": {"tableRows": [
            {"tableCells": [{"content": [para("a|b")]}, {"content": [para("c")]}]}]}}
        self.assertIn(r"a\|b", gd.to_markdown(doc(table)))


class TableParagraphs(unittest.TestCase):
    def test_paragraphs_inside_tables_are_seen(self):
        """Иначе normalize пропустил бы текст в таблицах, а check - ссылки в них."""
        table = {"table": {"tableRows": [
            {"tableCells": [{"content": [para("Текст, см. 9.9.")]}]}]}}
        found = [p.text for p in gd.paragraphs(doc(table))]
        self.assertEqual(found, ["Текст, см. 9.9."])
        self.assertTrue(any("9.9" in x for x in gd.check_report(gd.paragraphs(doc(table)))))


def obj_para(kind="inlineObjectElement", start=None, end=None):
    """Абзац без текста, но с объектом: картинка, разрыв, сноска."""
    return {"paragraph": {"elements": [{kind: {}}],
                          "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}},
            "startIndex": start or 1, "endIndex": end or 3}


class AdversarialFindings(unittest.TestCase):
    """Регресс на находки состязательного ревью."""

    def test_image_paragraph_is_not_blank(self):
        """Абзац из картинки текста не имеет, и blanks удалял его как пустую
        строку. Тем же путем терялись разрыв страницы и сноска."""
        for kind in ("inlineObjectElement", "pageBreak", "horizontalRule",
                     "footnoteReference"):
            with self.subTest(kind=kind):
                pp = gd.paragraphs(doc(obj_para(kind)))[0]
                self.assertFalse(pp.blank)

    def test_image_between_subheadings_not_deleted(self):
        extra, _ = gd.blanks_plan(gd.paragraphs(doc(
            para("2. ПЕРЕЧЕНЬ", "HEADING_1"),
            para("2.1. ПУНКТ", "HEADING_2"),
            obj_para(),
            para("2.2. ПУНКТ", "HEADING_2"))))
        self.assertEqual(extra, [])

    def test_title_and_subtitle_untouched(self):
        """Их ставит человек, эвристикой по тексту они не выводятся -
        принудив их к NORMAL_TEXT, мы снесли бы титул."""
        for style in ("TITLE", "SUBTITLE"):
            with self.subTest(style=style):
                self.assertEqual(
                    gd.normalize_plan(paras(para("Договор оказания услуг", style))), [])

    def test_intentional_font_size_survives(self):
        """Цитата 12pt и цена 18pt - оформление человека, стирать нельзя."""
        for mag in (9, 12, 18, 26):
            with self.subTest(mag=mag):
                pp = gd.paragraphs(doc(sized_para("Текст.", mag)))
                self.assertEqual(gd.normalize_plan(pp), [])

    def test_heading_size_leftover_is_cleared(self):
        pp = gd.paragraphs(doc(sized_para("Текст.", 16)))
        plan = gd.normalize_plan(pp)
        self.assertEqual(len(plan), 1)
        self.assertIn("кегль", plan[0][1])

    def test_mixed_sizes_are_left_alone(self):
        """Разные размеры внутри абзаца - точно ручное оформление."""
        el = {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
              "elements": [
                  {"textRun": {"content": "ВАЖНО: ",
                               "textStyle": {"fontSize": {"magnitude": 16}}}},
                  {"textRun": {"content": "текст\n",
                               "textStyle": {"fontSize": {"magnitude": 12}}}}]}}
        self.assertEqual(gd.normalize_plan(gd.paragraphs(doc(el))), [])

    def test_false_headings_rejected(self):
        """Каждая из этих строк уезжала в структуру документа как заголовок."""
        for t in ("1. 2026", "1. $100", "1. 01.09.2026",
                  "1. " + "СТОРОНЫ ОБЯЗАНЫ ПЕРЕДАТЬ АКТ " * 4):
            with self.subTest(t=t):
                self.assertIsNone(gd.Para(t, 1, 2, "NORMAL_TEXT", []).heading())

    def test_real_headings_still_recognized(self):
        for t in ("1. ОБЩИЕ ПОЛОЖЕНИЯ", "10.7. ЭТАП СДАЧИ", "1. API"):
            with self.subTest(t=t):
                self.assertIsNotNone(gd.Para(t, 1, 2, "NORMAL_TEXT", []).heading())

    def test_two_blank_lines_collapse_in_one_run(self):
        """Раньше убиралась одна за прогон, и второй запуск делал новую
        запись - то есть идемпотентности не было."""
        body = paras(para("1. РАЗДЕЛ", "HEADING_1"), para("1.1. А", "HEADING_2"),
                     para("Тело."), para(""), para(""), para("1.2. Б", "HEADING_2"))
        extra, missing = gd.blanks_plan(body)
        self.assertEqual(len(extra), 1)
        self.assertEqual(missing, [])

    def test_reference_with_word_punkt(self):
        problems = gd.check_report(paras(para("1. РАЗДЕЛ"),
                                         para("Текст, см. пункт 99.1.")))
        self.assertTrue(any("ссылка в никуда" in x for x in problems), problems)

    def test_huge_number_does_not_hang(self):
        """range(1, 1000000001) вешал прогон и печатал отчет на миллиард строк."""
        problems = gd.check_report(paras(
            para("1. РАЗДЕЛ"), para("1.1. А"), para("1.1000000000. Б")))
        self.assertTrue(any("разброс" in x for x in problems), problems)
        self.assertLess(len(problems), 5)

    def test_leading_zero_is_same_number(self):
        problems = gd.check_report(paras(
            para("1. РАЗДЕЛ"), para("1.1. А"), para("01.1. Б")))
        self.assertTrue(any("повтор" in x for x in problems), problems)

    def test_write_binds_revision(self):
        """Без requiredRevisionId правка ложится поверх чужой."""
        seen = {}
        orig = gd.api
        gd.api = lambda tok, path, method="GET", payload=None, params=None: seen.update(payload or {})
        try:
            gd.write("t", "doc", [{"x": 1}], "rev-42")
        finally:
            gd.api = orig
        self.assertEqual(seen["writeControl"], {"requiredRevisionId": "rev-42"})

    def test_floating_object_paragraph_is_not_blank(self):
        """Плавающая картинка висит на АБЗАЦЕ, а не на элементе: по элементам
        такой абзац выглядит пустым, и blanks удалил бы его вместе с ней."""
        el = {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                            "positionedObjectIds": ["obj-1"],
                            "elements": [{"textRun": {"content": "\n"}}]}}
        self.assertFalse(gd.paragraphs(doc(el))[0].blank)

    def test_inherited_size_with_unsized_newline_run(self):
        """Самый частый вид: кегль есть у текста и отсутствует у финального
        перевода строки. Полное совпадение размеров тут промахивалось."""
        el = {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
              "elements": [
                  {"textRun": {"content": "текст",
                               "textStyle": {"fontSize": {"magnitude": 16}}}},
                  {"textRun": {"content": "\n", "textStyle": {}}}]}}
        plan = gd.normalize_plan(gd.paragraphs(doc(el)))
        self.assertEqual(len(plan), 1)
        self.assertIn("кегль", plan[0][1])

    def test_heading_text_under_title_style_is_fixed(self):
        """Защита ручного титула не должна создавать слепую зону: текст-
        заголовок под TITLE - это как раз унаследованный стиль."""
        plan = gd.normalize_plan(paras(para("1. РАЗДЕЛ", "TITLE")))
        self.assertEqual([w for _p, _why, w in plan], ["HEADING_1"])

    def test_manual_title_still_protected(self):
        self.assertEqual(gd.normalize_plan(paras(para("Договор услуг", "TITLE"))), [])

    def test_write_without_revision_refuses(self):
        """Незащищенная запись выглядит так же, как защищенная."""
        with self.assertRaises(SystemExit):
            gd.write("t", "doc", [{"x": 1}], None)

    def test_nearest_blank_is_kept(self):
        """run собран от подпункта назад: убирать надо дальние, а не ближние."""
        body = paras(para("1. РАЗДЕЛ", "HEADING_1"), para("1.1. А", "HEADING_2"),
                     para("Тело."), para("дальняя"), para(""), para(""),
                     para("1.2. Б", "HEADING_2"))
        extra, _ = gd.blanks_plan(body)
        self.assertEqual(len(extra), 1)

    def test_snapshot_marks_dropped_content(self):
        """Молча потерянная картинка делает диф снимка ложным."""
        md = gd.to_markdown(doc(para("1. РАЗДЕЛ", "HEADING_1"), obj_para()))
        self.assertIn("inlineObjectElement", md)

    def test_snapshot_marks_toc_and_tabs(self):
        d = doc(para("1. РАЗДЕЛ", "HEADING_1"))
        d["body"]["content"].append({"tableOfContents": {"content": []}})
        d["tabs"] = [{"tabProperties": {}}, {"tabProperties": {}}]
        md = gd.to_markdown(d)
        self.assertIn("оглавление", md)
        self.assertIn("НЕПОЛНЫЙ СНИМОК", md)

    def test_ragged_table_is_padded(self):
        table = {"table": {"tableRows": [
            {"tableCells": [{"content": [para("A")]}, {"content": [para("B")]}]},
            {"tableCells": [{"content": [para("C")]}]}]}}
        md = gd.to_markdown(doc(table))
        rows = [l for l in md.splitlines() if l.startswith("|")]
        self.assertTrue(all(r.count("|") == rows[0].count("|") for r in rows), rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
