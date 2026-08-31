#!/usr/bin/env python3
"""Тесты сборщика markdown -> .docx из md-docx.py. stdlib-only (unittest).

Запуск: python3 tests/test_md_docx.py

Проверяют то, что ломает docx молча: Word не открывает файл с невалидным XML
или ячейкой таблицы без параграфа, причем сообщает лишь "содержимое повреждено".
Плюс метаданные (rules/document-metadata.md) - документ уходит наружу, и
свойства файла получатель видит в один клик.

Кейс с висячими hyperlink-rels - регресс на дефект, найденный при первом же
прогоне: связи в document.xml.rels создавались, а обертка w:hyperlink в тело
не писалась, и ссылка не была кликабельной.
"""
from __future__ import annotations

import base64
import importlib.util
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


md_docx = _load("md-docx.py", "md_docx")
md_pdf = _load("md-pdf.py", "md_pdf")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DC = "{http://purl.org/dc/elements/1.1/}"
DCTERMS = "{http://purl.org/dc/terms/}"


def pack(md: str, author: str = "tester", title: str = "T") -> zipfile.ZipFile:
    """md -> собранный .docx, открытый как zip."""
    body_html = md_pdf.md_to_html(md)[1]
    return zipfile.ZipFile(BytesIO(md_docx.build(body_html, author, title)))


def document(md: str) -> ET.Element:
    return ET.fromstring(pack(md).read("word/document.xml"))


def text_of(el: ET.Element) -> str:
    return "".join(t.text or "" for t in el.iter(W + "t"))


class Package(unittest.TestCase):
    def test_all_parts_are_valid_xml(self):
        z = pack("# Заголовок\n\nтекст\n")
        for name in z.namelist():
            with self.subTest(part=name):
                ET.fromstring(z.read(name))  # ParseError = Word не откроет файл

    def test_required_parts_present(self):
        names = set(pack("текст\n").namelist())
        for part in (
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/_rels/document.xml.rels",
            "word/styles.xml",
            "word/numbering.xml",
            "docProps/core.xml",
        ):
            self.assertIn(part, names)

    def test_content_types_first_in_archive(self):
        self.assertEqual(pack("текст\n").namelist()[0], "[Content_Types].xml")

    def test_empty_source_still_valid(self):
        ET.fromstring(pack("").read("word/document.xml"))


class Metadata(unittest.TestCase):
    def test_author_and_title_written(self):
        core = ET.fromstring(pack("текст\n", author="dwl", title="Отчет").read("docProps/core.xml"))
        self.assertEqual(core.find(DC + "creator").text, "dwl")
        self.assertEqual(core.find(DC + "title").text, "Отчет")

    def test_no_tool_traces_and_empty_optional_fields(self):
        core = pack("текст\n").read("docProps/core.xml").decode()
        self.assertNotIn("python-docx", core)
        self.assertNotIn("md-docx", core)
        for empty in ("<dc:subject></dc:subject>", "<cp:keywords></cp:keywords>"):
            self.assertIn(empty, core)

    def test_dates_are_current_not_template(self):
        """Дата обязана быть свежей, а не любой не-2013: константа 2020-01-01
        прошла бы проверку "нет 2013" и оставила дефект на месте."""
        import datetime

        core = ET.fromstring(pack("текст\n").read("docProps/core.xml"))
        created = core.find(DCTERMS + "created").text
        stamp = datetime.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        delta = abs((datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds())
        self.assertLess(delta, 300, f"дата сборки не текущая: {created}")
        self.assertEqual(created, core.find(DCTERMS + "modified").text)

    def test_author_is_escaped(self):
        core = pack("текст\n", author='Иванов & <Co>').read("docProps/core.xml").decode()
        self.assertIn("Иванов &amp; &lt;Co&gt;", core)


class Blocks(unittest.TestCase):
    def test_heading_styles(self):
        doc = document("# Один\n\n## Два\n\n#### Четыре\n")
        styles = [p.find(f"{W}pPr/{W}pStyle").get(W + "val") for p in doc.iter(W + "p")]
        for expected in ("Heading1", "Heading2", "Heading4"):
            self.assertIn(expected, styles)

    def test_soft_wrap_joins_paragraph(self):
        doc = document("первая строка\nвторая строка\n")
        paras = [text_of(p) for p in doc.iter(W + "p") if text_of(p)]
        self.assertEqual(paras, ["первая строка вторая строка"])

    def test_bold_and_italic_runs(self):
        doc = document("**жирный** и *курсив*\n")
        runs = list(doc.iter(W + "r"))
        self.assertTrue(any(r.find(f"{W}rPr/{W}b") is not None for r in runs))
        self.assertTrue(any(r.find(f"{W}rPr/{W}i") is not None for r in runs))

    def test_spaces_preserved_around_inline_markup(self):
        doc = document("до **жирного** после\n")
        self.assertEqual(
            [text_of(p) for p in doc.iter(W + "p") if text_of(p)],
            ["до жирного после"],
        )

    def test_lists_use_real_numbering(self):
        doc = document("- один\n- два\n\n1. первый\n2. второй\n")
        numids = sorted({n.get(W + "val") for n in doc.iter(W + "numId")})
        self.assertEqual(numids, ["1", "2"])  # 1 - bullet, 2 - decimal

    def _list_paragraphs(self, doc):
        """Абзацы со стилем списка -> [(numId или None)] в порядке документа."""
        out = []
        for para in doc.iter(W + "p"):
            style = para.find(f"{W}pPr/{W}pStyle")
            if style is not None and style.get(W + "val") == "ListParagraph":
                num = para.find(f"{W}pPr/{W}numPr/{W}numId")
                out.append(num.get(W + "val") if num is not None else None)
        return out

    def test_every_bullet_item_numbered(self):
        """Дефект, доживший до Word: numId обнулялся в flush() после первого
        пункта, и маркер получал только он. Стиль оставался списочным, поэтому
        отступы выглядели правильно - проверять надо каждый пункт, а не наличие
        numId в документе."""
        doc = document("- один\n- два\n- три\n")
        self.assertEqual(self._list_paragraphs(doc), ["1", "1", "1"])

    def test_every_ordered_item_numbered(self):
        doc = document("1. первый\n2. второй\n3. третий\n")
        self.assertEqual(self._list_paragraphs(doc), ["2", "2", "2"])

    def test_list_type_switches_between_blocks(self):
        """Маркированный -> абзац -> нумерованный -> маркированный: тип списка
        не залипает и не протекает через промежуточный абзац."""
        doc = document("- а\n- б\n\nтекст\n\n1. раз\n2. два\n\n- в\n- г\n")
        self.assertEqual(self._list_paragraphs(doc), ["1", "1", "2", "2", "1", "1"])

    def test_numbering_does_not_leak_outside_lists(self):
        """Обратная сторона починки: нумерация не должна протекать в абзацы,
        цитаты, заголовки и ячейки таблицы после списка."""
        md = ("- пункт\n- еще\n\nобычный абзац\n\n> цитата\n\n"
              "## заголовок\n\n| a | b |\n| - | - |\n| 1 | 2 |\n")
        doc = document(md)
        for para in doc.iter(W + "p"):
            style = para.find(f"{W}pPr/{W}pStyle")
            name = style.get(W + "val") if style is not None else None
            if name == "ListParagraph":
                continue
            self.assertIsNone(para.find(f"{W}pPr/{W}numPr"),
                              f"нумерация протекла в абзац стиля {name}")

    def test_quote_style(self):
        doc = document("> цитата\n")
        styles = [p.find(f"{W}pPr/{W}pStyle").get(W + "val") for p in doc.iter(W + "p")]
        self.assertIn("Quote", styles)

    def test_code_block_keeps_lines_separate(self):
        doc = document("```\nстрока один\nстрока два\n```\n")
        code = [text_of(p) for p in doc.iter(W + "p")
                if p.find(f"{W}pPr/{W}pStyle").get(W + "val") == "Code"]
        self.assertIn("строка один", code)
        self.assertIn("строка два", code)

    def test_missing_image_falls_back_to_alt(self):
        # файла нет - документ собирается, но получатель видит, что тут была
        # картинка; молчание было бы хуже отсутствия
        self.assertIn("[схема]", text_of(document("![схема](нет-такого.png)\n")))


class Tables(unittest.TestCase):
    def test_shape_matches_source(self):
        doc = document("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n")
        tbl = next(doc.iter(W + "tbl"))
        rows = list(tbl.iter(W + "tr"))
        self.assertEqual(len(rows), 3)
        # ширину проверяем у ВСЕХ строк, не только у шапки: проверка одной
        # шапки пропускала потерю ячеек в строках тела
        for i, row in enumerate(rows):
            self.assertEqual(len(list(row.iter(W + "tc"))), 2, f"строка {i}")
        self.assertEqual(len(list(tbl.iter(W + "gridCol"))), 2)

    def test_wide_row_text_not_lost(self):
        doc = document("| A | B |\n|---|---|\n| y | z | штраф 10% |\n")
        self.assertIn("штраф 10%", "".join(text_of(tc) for tc in doc.iter(W + "tc")))

    def test_every_cell_has_paragraph(self):
        # <w:tc> без <w:p> - файл невалиден, Word откажется открыть
        doc = document("| A | B |\n|---|---|\n| 1 | |\n")
        for tc in doc.iter(W + "tc"):
            self.assertTrue(list(tc.iter(W + "p")))

    def test_dash_cell_survives(self):
        # регресс той же природы, что в test_md_pdf: ячейка-прочерк не должна
        # приниматься за строку-разделитель и исчезать
        doc = document("| A |\n|---|\n| - |\n")
        self.assertIn("-", [text_of(tc) for tc in doc.iter(W + "tc")])

    def test_two_tables_not_merged(self):
        doc = document("| A |\n|---|\n| x |\n\n| B |\n|---|\n| y |\n")
        self.assertEqual(len(list(doc.iter(W + "tbl"))), 2)

    def test_header_cells_bold(self):
        doc = document("| A |\n|---|\n| x |\n")
        rows = list(next(doc.iter(W + "tbl")).iter(W + "tr"))
        self.assertIsNotNone(rows[0].find(f".//{W}rPr/{W}b"), "шапка должна быть жирной")
        self.assertIsNone(rows[1].find(f".//{W}rPr/{W}b"), "тело таблицы жирным быть не должно")


class Hyperlinks(unittest.TestCase):
    def test_no_dangling_rels(self):
        z = pack("[текст](https://example.com/a?x=1&y=2)\n")
        doc = ET.fromstring(z.read("word/document.xml"))
        rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        declared = {r.get("Id") for r in rels if "hyperlink" in r.get("Type")}
        used = {h.get(R + "id") for h in doc.iter(W + "hyperlink")}
        self.assertTrue(declared)
        self.assertEqual(declared, used)

    def test_url_escaped_in_rels(self):
        rels = pack("[t](https://e.com/?a=1&b=2)\n").read("word/_rels/document.xml.rels").decode()
        self.assertIn("&amp;", rels)
        self.assertNotIn("?a=1&b=2", rels)


class Sanitizing(unittest.TestCase):
    def test_control_chars_stripped(self):
        doc = document("текст \x00\x01\x08 внутри\n")
        ET.fromstring(ET.tostring(doc))  # уже распарсилось, значит XML валиден
        self.assertNotIn("\x00", text_of(doc))

    def test_xml_special_chars_escaped(self):
        raw = pack("a < b & c > d\n").read("word/document.xml").decode()
        self.assertIn("a &lt; b &amp; c &gt; d", raw)


class ParserReuse(unittest.TestCase):
    def test_pipe_formula_stays_text(self):
        # разбор общий с md-pdf.py: "|x| - модуль" не таблица
        doc = document("Формула |x| - модуль числа.\n")
        self.assertEqual(len(list(doc.iter(W + "tbl"))), 0)
        self.assertIn("|x|", text_of(doc))

    def test_missing_md_pdf_exits_with_message(self):
        # если рядом нет md-pdf.py, скрипт обязан сказать это внятно, а не упасть
        # ImportError посреди сборки документа
        import unittest.mock as mock

        with mock.patch.object(md_docx.pathlib.Path, "exists", return_value=False):
            with self.assertRaises(SystemExit) as cm:
                md_docx.load_md_to_html()
        self.assertIn("md-pdf.py", str(cm.exception))


# Канонический порядок дочерних элементов по схеме OOXML (подмножество, которое
# использует скрипт). Перестановка не видна глазом и проходит проверку
# well-formedness, но делает документ schema-invalid: строгий потребитель
# объявляет содержимое поврежденным или запускает repair.
PPR_ORDER = [
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl",
    "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs", "spacing", "ind",
    "contextualSpacing", "jc",
]
RPR_ORDER = [
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike",
    "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u",
]


class ListNumbering(unittest.TestCase):
    """Нумерация списков. Один numId на все нумерованные списки Word понимал
    как ОДИН список, разорванный на куски: второй список начинался с 5, третий
    с 11. Заметил это получатель документа при вычитке, не разработчик."""

    THREE = ("1. раз\n2. два\n\nТекст между.\n\n1. снова раз\n2. снова два\n\n"
             "- маркер\n- еще\n\n1. третий список\n")

    def numids(self, md):
        """numId по абзацам документа и объявленные в numbering.xml."""
        z = pack(md)
        doc = ET.fromstring(z.read("word/document.xml"))
        used = [e.get(W + "val") for e in doc.iter(W + "numId")]
        num = ET.fromstring(z.read("word/numbering.xml"))
        declared = [e.get(W + "numId") for e in num.iter(W + "num")]
        return used, declared

    def test_each_ordered_list_gets_own_numid(self):
        used, _ = self.numids(self.THREE)
        # три нумерованных списка -> три разных numId, маркеры отдельно
        ordered = [u for u in used if u != "1"]
        self.assertEqual(len(set(ordered)), 3, used)

    def test_bullets_share_one_numid(self):
        used, _ = self.numids(self.THREE)
        self.assertEqual(used.count("1"), 2, used)

    def test_every_used_numid_is_declared(self):
        """Главный инвариант: ссылка на необъявленный numId - это документ,
        который Word открывает со сбитой или пропавшей нумерацией."""
        used, declared = self.numids(self.THREE)
        self.assertTrue(set(used) <= set(declared), (used, declared))

    def test_no_lists_declares_only_bullets(self):
        """Без списков лишние w:num не пишем - в файле не должно быть мусора,
        на который никто не ссылается."""
        used, declared = self.numids("просто текст\n")
        self.assertEqual(used, [])
        self.assertEqual(declared, ["1"])

    def test_single_list_starts_from_first_ol_numid(self):
        used, declared = self.numids("1. раз\n2. два\n")
        self.assertEqual(set(used), {"2"})
        self.assertEqual(declared, ["1", "2"])

    def test_numbering_xml_is_valid_and_abstract_kept(self):
        """abstractNum описывает ВИД списка и остается в двух экземплярах;
        размножается только w:num - он и дает перезапуск счета."""
        num = ET.fromstring(pack(self.THREE).read("word/numbering.xml"))
        self.assertEqual(len(list(num.iter(W + "abstractNum"))), 2)
        for e in num.iter(W + "num"):
            self.assertIsNotNone(e.find(W + "abstractNumId"))


class SchemaOrder(unittest.TestCase):
    """Регресс на находки adversarial-ревью (codex, 2026-07-26): порядок в
    w:pPr стилей Quote/Code и в w:rPr рана со ссылкой нарушал схему."""

    def _check(self, root: ET.Element, container: str, order: list[str], where: str) -> None:
        for node in root.iter(W + container):
            tags = [c.tag.replace(W, "") for c in node if c.tag.replace(W, "") in order]
            idx = [order.index(t) for t in tags]
            self.assertEqual(idx, sorted(idx), f"{where}: {container} в порядке {tags}")

    def test_document_element_order(self):
        md = ("# H\n\n**жирный** [ссылка](https://e.com) `код`\n\n> цитата\n\n"
              "- пункт\n\n1. пункт\n\n---\n\n| [A](https://e.com) |\n|---|\n| x |\n")
        doc = document(md)
        self._check(doc, "pPr", PPR_ORDER, "document.xml")
        self._check(doc, "rPr", RPR_ORDER, "document.xml")

    def test_styles_element_order(self):
        styles = ET.fromstring(pack("текст\n").read("word/styles.xml"))
        self._check(styles, "pPr", PPR_ORDER, "styles.xml")
        self._check(styles, "rPr", RPR_ORDER, "styles.xml")

    def test_link_in_header_cell_puts_rstyle_first(self):
        doc = document("| [Ссылка](https://e.com) |\n|---|\n| значение |\n")
        rpr = doc.find(f".//{W}rPr")
        self.assertEqual([c.tag.replace(W, "") for c in rpr][0], "rStyle")


class InlineFixes(unittest.TestCase):
    """Регресс на находки adversarial-ревью (codex, 2026-07-26)."""

    def test_space_between_adjacent_inline_tags(self):
        # "**Срок** *оплаты*" давало "Срокоплаты": пробел приходит отдельным
        # чанком, и strip() выбрасывал его целиком
        doc = document("**Срок** *оплаты* `сегодня` [здесь](https://e.com)\n")
        self.assertEqual(
            [text_of(p) for p in doc.iter(W + "p") if text_of(p)],
            ["Срок оплаты сегодня здесь"],
        )

    def test_block_newlines_do_not_leak_as_text(self):
        # обратная сторона предыдущего: перевод строки между блоками в документ
        # попадать не должен
        doc = document("первый абзац\n\nвторой абзац\n")
        self.assertEqual(
            [text_of(p) for p in doc.iter(W + "p") if text_of(p)],
            ["первый абзац", "второй абзац"],
        )

    def test_crossed_markup_keeps_styles_separate(self):
        # общий парсер отдает перекрещенный HTML (<strong><em>..</strong>..</em>);
        # pop() с конца уносил курсив и красил остаток жирным
        doc = document("***оба** только-курсив* хвост\n")
        runs = {text_of(r): (r.find(f"{W}rPr/{W}b") is not None,
                             r.find(f"{W}rPr/{W}i") is not None)
                for r in doc.iter(W + "r")}
        self.assertEqual(runs["оба"], (True, True))
        self.assertEqual(runs[" только-курсив"], (False, True))
        self.assertEqual(runs[" хвост"], (False, False))

    def test_hr_produces_border_paragraph(self):
        # HTMLParser отдает <hr> в handle_starttag, а не handle_startendtag -
        # разделитель молча исчезал из документа
        doc = document("до\n\n---\n\nпосле\n")
        self.assertTrue(
            any(p.find(f"{W}pPr/{W}pBdr") is not None for p in doc.iter(W + "p")),
            "разделитель --- потерян",
        )

    def test_link_label_with_url_has_single_target(self):
        """Метка ссылки, содержащая URL, целиком ведет на заданный адрес.

        В раунде 1 парсер отдавал здесь вложенные <a>, и часть метки получала
        чужой target; фикс в md-pdf.py убрал вложенность в корне, поэтому здесь
        проверяется итоговый инвариант, а не механика стека.
        """
        z = pack("[документ https://old.example конец](https://new.example)\n")
        doc = ET.fromstring(z.read("word/document.xml"))
        links = list(doc.iter(W + "hyperlink"))
        rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        self.assertEqual({targets[h.get(R + "id")] for h in links}, {"https://new.example"})
        self.assertIn("конец", "".join(text_of(h) for h in links))

    def test_link_stack_resumes_outer_on_nested_html(self):
        """Страховка самого механизма: если на вход все же придут вложенные <a>,
        после закрытия внутренней обертка внешней обязана продолжиться."""
        parser = md_docx.DocxBody()
        parser.feed('<p><a href="https://outer.example">до '
                    '<a href="https://inner.example">внутри</a> после</a></p>')
        doc = ET.fromstring(
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<w:body>{parser.result()}</w:body></w:document>"
        )
        pairs = [(text_of(h), h.get(R + "id")) for h in doc.iter(W + "hyperlink")]
        self.assertEqual(pairs[0][1], pairs[-1][1], "внешняя ссылка не продолжилась")
        self.assertIn(" после", [t for t, _ in pairs])

    def test_literal_code_keeps_brackets(self):
        # содержимое `code` не должно повторно разбираться как markdown
        doc = document("`[literal](https://wrong.example)`\n")
        self.assertIn("[literal](https://wrong.example)", text_of(doc))


class Sanitizing2(unittest.TestCase):
    def test_noncharacters_stripped(self):
        # U+FFFE/U+FFFF кодируются в UTF-8 штатно, но XML-парсер на них падает
        doc = document("текст \ufffe\uffff внутри\n")
        self.assertNotIn("\ufffe", text_of(doc))
        self.assertNotIn("\uffff", text_of(doc))

    def test_noncharacter_in_url_does_not_break_rels(self):
        """clean() чистит текст, но URL шел в rels через quoteattr без чистки -
        и document.xml.rels перестал парситься (раунд 2)."""
        z = pack("[ссылка](https://example.test/\ufffe)\n")
        ET.fromstring(z.read("word/_rels/document.xml.rels"))

    def test_noncharacter_in_author_and_title(self):
        z = zipfile.ZipFile(BytesIO(md_docx.build("<w:p/>", "автор\ufffe", "тема\uffff")))
        ET.fromstring(z.read("docProps/core.xml"))  # ParseError = дефект вернулся


class TitleForProperties(unittest.TestCase):
    def test_markdown_stripped_from_title(self):
        # md_to_html отдает текст H1 до inline-обработки, поэтому в свойства
        # файла шел сырой "[ТЗ](url)" вместо осмысленного заголовка
        self.assertEqual(md_docx.plain_title("[ТЗ на портал](https://wiki/tz)"), "ТЗ на портал")
        self.assertEqual(md_docx.plain_title("**Отчет** за `июль`"), "Отчет за июль")
        self.assertEqual(md_docx.plain_title("Обычный заголовок"), "Обычный заголовок")

    def test_unpaired_markers_kept(self):
        """Снимаются только парные маркеры: буквальные "*" и одиночный бэктик
        в заголовке должны уцелеть (раунд 2)."""
        self.assertEqual(md_docx.plain_title("API /v1/users/*"), "API /v1/users/*")
        self.assertEqual(md_docx.plain_title("Запрос `SELECT"), "Запрос `SELECT")

    def test_underscores_in_identifiers_kept(self):
        """Подчеркивание наш парсер emphasis не считает, а в заголовке бывают
        идентификаторы: "Модуль user_auth" не должен стать "userauth"."""
        self.assertEqual(md_docx.plain_title("Модуль user_auth"), "Модуль user_auth")
        self.assertEqual(md_docx.plain_title("**API** get_user_by_id"), "API get_user_by_id")


# минимальный валидный PNG 1x1 (сигнатура + IHDR + IDAT + IEND)
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def pack_with(md: str, tmpdir: Path, **kw) -> zipfile.ZipFile:
    """md -> .docx с указанием каталога источника (для картинок) и опций."""
    body_html = md_pdf.md_to_html(md)[1]
    return zipfile.ZipFile(BytesIO(
        md_docx.build(body_html, "tester", "T", src_dir=tmpdir, **kw)))


class Images(unittest.TestCase):
    """Класс ошибок здесь - "Word молча объявляет файл поврежденным", поэтому
    проверки структурные: части пакета, объявленные типы, уникальность id."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pic.png").write_bytes(PNG_1x1)

    def test_image_embedded_as_part(self):
        z = pack_with("![схема](pic.png)\n", self.tmp)
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        self.assertEqual(len(media), 1, z.namelist())
        self.assertEqual(z.read(media[0]), PNG_1x1)

    def test_extension_declared_in_content_types(self):
        """Без Default для расширения Word не откроет пакет вовсе."""
        z = pack_with("![схема](pic.png)\n", self.tmp)
        ct = z.read("[Content_Types].xml").decode()
        self.assertIn('Extension="png"', ct)

    def test_rids_unique_across_images_and_links(self):
        """Пойманный дефект: у картинок и гиперссылок общий rels-файл, а
        счетчики были раздельные - картинка и ссылка получили один rId."""
        z = pack_with("![схема](pic.png)\n\n[ссылка](https://example.com)\n", self.tmp)
        rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        ids = [r.get("Id") for r in rels]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertGreaterEqual(len(ids), 2)

    def test_docpr_ids_unique(self):
        z = pack_with("![a](pic.png)\n\n![b](pic.png)\n\n![c](pic.png)\n", self.tmp)
        doc = z.read("word/document.xml").decode()
        import re as _re
        ids = _re.findall(r'<wp:docPr id="(\d+)"', doc)
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_same_file_stored_once(self):
        z = pack_with("![a](pic.png)\n\n![b](pic.png)\n", self.tmp)
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        self.assertEqual(len(media), 1, media)

    def test_remote_image_falls_back_to_alt(self):
        z = pack_with("![внешняя](https://example.com/x.png)\n", self.tmp)
        doc = ET.fromstring(z.read("word/document.xml"))
        self.assertIn("[внешняя]", text_of(doc))
        self.assertFalse([n for n in z.namelist() if n.startswith("word/media/")])

    def test_unknown_format_falls_back_to_alt(self):
        (self.tmp / "fake.png").write_bytes(b"not an image at all")
        z = pack_with("![битая](fake.png)\n", self.tmp)
        doc = ET.fromstring(z.read("word/document.xml"))
        self.assertIn("[битая]", text_of(doc))

    def test_jpeg_declared_by_own_extension(self):
        """Бриф просит Default на КАЖДОЕ расширение: у png и jpg разные типы,
        и Word спотыкается на несовпадении объявленного типа с содержимым."""
        # минимальный валидный JPEG: SOI + APP0 + SOF0(1x1) + EOI - размеры
        # лежат в SOF0, огрызок без него image_size() честно не распознает
        jpeg = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/wAALCAABAAEBAREA/9k=")
        (self.tmp / "photo.jpg").write_bytes(jpeg)
        z = pack_with("![фото](photo.jpg)\n", self.tmp)
        ct = z.read("[Content_Types].xml").decode()
        # расширение части - по СИГНАТУРЕ файла (jpeg), а не по имени исходника
        # (.jpg): Word спотыкается на несовпадении объявленного типа с содержимым,
        # а ".jpg с PNG внутри" - обычное дело после пересохранения
        self.assertIn('Extension="jpeg" ContentType="image/jpeg"', ct)
        self.assertTrue([n for n in z.namelist() if n.endswith(".jpeg")], z.namelist())

    def test_all_parts_valid_xml_with_image(self):
        z = pack_with("![схема](pic.png)\n", self.tmp)
        for name in z.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                ET.fromstring(z.read(name))


class Separators(unittest.TestCase):
    def test_flag_adds_borders(self):
        with tempfile.TemporaryDirectory() as tmp:
            z = pack_with("## секция\n", Path(tmp), separators=True)
            self.assertIn("w:pBdr", z.read("word/styles.xml").decode())

    def test_default_has_no_heading_borders(self):
        """По умолчанию выключено: черта под каждым H2 уместна в резюме,
        но не в ТЗ и не в протоколе встречи."""
        with tempfile.TemporaryDirectory() as tmp:
            styles = pack_with("## секция\n", Path(tmp)).read("word/styles.xml").decode()
        heading2 = styles.split('w:styleId="Heading2"')[1].split("</w:style>")[0]
        self.assertNotIn("w:pBdr", heading2)


class Photo(unittest.TestCase):
    def test_photo_anchored_with_requested_box(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            (tmpdir / "me.png").write_bytes(PNG_1x1)
            z = pack_with("# Имя\n\nтекст\n", tmpdir, photo=tmpdir / "me.png")
            doc = z.read("word/document.xml").decode()
        self.assertIn("wp:anchor", doc)
        # 30mm x 38mm в EMU (36000 на мм)
        self.assertIn(f'cx="{30 * 36000}" cy="{38 * 36000}"', doc)

    def test_photo_crops_instead_of_squeezing(self):
        """Квадратная картинка в рамку 30x38 - кадрируется srcRect, а не
        вписывается с искажением пропорций (иначе docx и PDF разъезжаются)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            (tmpdir / "me.png").write_bytes(PNG_1x1)
            z = pack_with("# Имя\n", tmpdir, photo=tmpdir / "me.png")
            doc = z.read("word/document.xml").decode()
        # тег целиком с атрибутами: подстрока "a:srcRect" совпала бы
        # и с опечаткой в имени тега
        self.assertRegex(doc, r"<a:srcRect [^>]*(?:t|b|l|r)=\"\d+\"")

if __name__ == "__main__":
    unittest.main(verbosity=2)
