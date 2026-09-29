#!/usr/bin/env python3
"""Blind behavioral acceptance tests for documents/spec.md (Q-71..Q-73).

Only public CLI/functions are exercised; Chrome and network access are mocked.
Run: python3 -m pytest -q tests/test_documents_contract.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FORMATS = {"pdf": "md-pdf.py", "docx": "md-docx.py",
           "pptx": "md-pptx.py", "xlsx": "csv-xlsx.py"}
AUTHOR_VARS = ("DOC_AUTHOR", "DOCX_AUTHOR", "PPTX_AUTHOR", "XLSX_AUTHOR", "PDF_AUTHOR")
DC = "{http://purl.org/dc/elements/1.1/}"
CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MONO = {"consolas", "courier", "courier new", "menlo", "monaco", "liberation mono"}


def load_script(path):
    spec = importlib.util.spec_from_file_location("contract_" + path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def chrome_pdf():
    """Small classic-xref PDF with Chrome-shaped Info and no Author."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << >> >>",
               b"<< /Title (Fixture) /Creator (Chromium) /Producer (Skia/PDF) >>"]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 5\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 5 /Root 1 0 R /Info 4 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


def pdf_string(token):
    """Decode literal PDF escapes or hex, including UTF-16BE BOM."""
    if token.startswith(b"<"):
        raw = bytes.fromhex(token[1:-1].decode())
    else:
        def unescape(match):
            value = match[1]
            if re.fullmatch(rb"[0-7]{1,3}", value):
                return bytes([int(value, 8)])
            return {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                    b"f": b"\f", b"\n": b"", b"\r\n": b""}.get(value, value)
        raw = re.sub(rb"\\([0-7]{1,3}|\r\n|.)", unescape, token[1:-1], flags=re.S)
    return raw[2:].decode("utf-16-be") if raw.startswith(b"\xfe\xff") else raw.decode("latin1")


def active_pdf_author(data):
    """Resolve effective Info through startxref/Prev, never search stale bytes."""
    offset = int(re.findall(rb"startxref\s+(\d+)\s+%%EOF", data)[-1])
    entries, info, visited = {}, None, set()
    while offset not in visited:
        visited.add(offset)
        section = data[offset:]
        if not section.startswith(b"xref"):
            raise AssertionError("fixture expects a classic PDF xref table")
        table, tail = section.split(b"trailer", 1)
        lines = table.splitlines()[1:]
        index = 0
        while index < len(lines):
            if not lines[index].strip():
                index += 1
                continue
            first, count = map(int, lines[index].split())
            index += 1
            for number in range(first, first + count):
                pos, generation, state = lines[index].split()
                entries.setdefault((number, int(generation)), int(pos) if state == b"n" else None)
                index += 1
        trailer = re.search(rb"<<(.*?)>>", tail, re.S)[1]
        ref = re.search(rb"/Info\s+(\d+)\s+(\d+)\s+R", trailer)
        if info is None and ref:
            info = tuple(map(int, ref.groups()))
        prev = re.search(rb"/Prev\s+(\d+)", trailer)
        if not prev:
            break
        offset = int(prev[1])
    if info is None:
        return None
    start = entries[info]
    if start is None:
        raise AssertionError("Info references a free xref entry")
    obj = data[start:].split(b"endobj", 1)[0]
    match = re.search(rb"/Author\s*(<[^>]*>|\((?:\\(?:[0-7]{1,3}|\r\n|.)|[^\\)])*\))", obj, re.S)
    return pdf_string(match[1]) if match else None


def xml_parts(data, prefix):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [name for name in archive.namelist() if re.fullmatch(prefix, name)]
        names.sort(key=lambda name: int(re.search(r"(\d+)\.xml$", name)[1]) if re.search(r"\d+\.xml$", name) else 0)
        return [ET.fromstring(archive.read(name)) for name in names]


def slide_text(root):
    return "".join(node.text or "" for node in root.iter(A + "t"))


class HtmlRuns(HTMLParser):
    """Independent HTML -> per-character supported-style oracle."""
    def __init__(self, fragment):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.chars = []
        self.feed(fragment)

    def handle_starttag(self, tag, attrs):
        if tag in ("strong", "b", "em", "i", "code"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack.remove(tag)

    def handle_data(self, text):
        styles = (bool(set(self.stack) & {"strong", "b"}),
                  bool(set(self.stack) & {"em", "i"}), "code" in self.stack)
        self.chars.extend((char, styles) for char in text)


def pptx_chars(paragraph):
    chars = []
    for run in paragraph.findall(A + "r"):
        props = run.find(A + "rPr")
        attrs = props.attrib if props is not None else {}
        fonts = {node.get("typeface", "").lower() for node in props} if props is not None else set()
        style = (attrs.get("b") in ("1", "true"), attrs.get("i") in ("1", "true"), bool(fonts & MONO))
        chars.extend((char, style) for char in run.findtext(A + "t", ""))
    return chars


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def cli(self, fmt, source="## Slide\n\nbody\n", author=None, env=None, scripts=SCRIPTS, output=None):
        src = self.root / ("source.csv" if fmt == "xlsx" else "source.md")
        src.write_text(source, encoding="utf-8")
        before = src.read_bytes()
        out = output or self.root / ("result." + fmt)
        argv = [str(scripts / FORMATS[fmt]), str(src), "--out", str(out)]
        if author is not None:
            argv += ["--author", author]
        environment = {key: value for key, value in os.environ.items() if key not in AUTHOR_VARS}
        environment.update(env or {})
        stdout, stderr, captured_html = io.StringIO(), io.StringIO(), []

        def print_mock(chrome, html_path, *args, **kwargs):
            captured_html.append(Path(html_path).read_text(encoding="utf-8"))
            return chrome_pdf()

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            stack.enter_context(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
            stack.enter_context(mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network forbidden")))
            code = 0
            try:
                module = load_script(scripts / FORMATS[fmt])
                if fmt == "pdf":
                    stack.enter_context(mock.patch.object(module, "find_chrome", return_value=sys.executable))
                    stack.enter_context(mock.patch.object(module, "cdp_print", side_effect=print_mock))
                code = module.main() or 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                if isinstance(exc.code, str):
                    print(exc.code, file=sys.stderr)
        self.assertEqual(src.read_bytes(), before, "CLI rewrote its input")
        return code, stdout.getvalue(), stderr.getvalue(), out, captured_html

    def built(self, fmt, **kwargs):
        code, stdout, stderr, out, html = self.cli(fmt, **kwargs)
        self.assertEqual(code, 0, stderr)
        self.assertTrue(out.is_file(), stdout + stderr)
        return out.read_bytes(), stdout, stderr, html

    def assert_author(self, fmt, expected, **kwargs):
        data, _, _, _ = self.built(fmt, **kwargs)
        if fmt == "pdf":
            self.assertEqual(active_pdf_author(data), expected)
        else:
            core = xml_parts(data, r"docProps/core\.xml")[0]
            self.assertEqual(core.findtext(DC + "creator"), expected)
            self.assertEqual(core.findtext(CP + "lastModifiedBy"), expected)


class AuthorContract(CliCase):
    """Criteria 1, 2: priority and both metadata fields in actual output."""
    def test_flag_wins(self):
        for fmt in FORMATS:
            with self.subTest(format=fmt):
                self.assert_author(fmt, 'Флаг & <Co> «ёлка» — (A) \\ путь',
                                   author='Флаг & <Co> «ёлка» — (A) \\ путь',
                                   env={key: key + " value" for key in AUTHOR_VARS})

    def test_common_author_wins_without_flag(self):
        for fmt in FORMATS:
            with self.subTest(format=fmt):
                env = {key: key + " legacy" for key in AUTHOR_VARS}
                env["DOC_AUTHOR"] = 'Общий & <Автор> «ёлка» — тест'
                self.assert_author(fmt, env["DOC_AUTHOR"], env=env)

    def test_empty_flag_falls_back_to_common(self):
        for fmt in FORMATS:
            with self.subTest(format=fmt):
                env = {key: key + " legacy" for key in AUTHOR_VARS}
                env["DOC_AUTHOR"] = "Общий"
                self.assert_author(fmt, "Общий", author="", env=env)

    def test_legacy_fallback_absent_and_empty_common(self):
        for fmt in ("docx", "pptx", "xlsx"):
            for empty in (False, True):
                with self.subTest(format=fmt, empty=empty):
                    env = {key: "Чужой" for key in AUTHOR_VARS if key != "DOC_AUTHOR"}
                    env[fmt.upper() + "_AUTHOR"] = "Запасной & <Автор>"
                    if empty:
                        env["DOC_AUTHOR"] = ""
                    self.assert_author(fmt, "Запасной & <Автор>", author="" if empty else None, env=env)

    def test_default_with_absent_values(self):
        for fmt in FORMATS:
            with self.subTest(format=fmt):
                self.assert_author(fmt, "dwl")

    def test_default_with_empty_values(self):
        for fmt in FORMATS:
            with self.subTest(format=fmt):
                self.assert_author(fmt, "dwl", author="", env=dict.fromkeys(AUTHOR_VARS, ""))

    def test_foreign_legacy_variables_are_ignored(self):
        for fmt in FORMATS:
            for empty in (False, True):
                with self.subTest(format=fmt, empty=empty):
                    env = {key: "Чужой" for key in AUTHOR_VARS if key not in ("DOC_AUTHOR", fmt.upper() + "_AUTHOR")}
                    # PDF_AUTHOR is deliberately foreign even for PDF.
                    env["PDF_AUTHOR"] = "Не вводить PDF_AUTHOR"
                    if empty:
                        env["DOC_AUTHOR"] = ""
                        if fmt != "pdf":
                            env[fmt.upper() + "_AUTHOR"] = ""
                    self.assert_author(fmt, "dwl", author="" if empty else None, env=env)

    def test_nonempty_author_is_not_trimmed_or_normalized(self):
        for fmt in FORMATS:
            with self.subTest(format=fmt):
                self.assert_author(fmt, "  инженер-исследователь «ёлка» —  ",
                                   author="  инженер-исследователь «ёлка» —  ")

    def test_pdf_ascii_author_and_escaped_literal(self):
        self.assert_author("pdf", r"A (Team) \ B", author=r"A (Team) \ B")


class PptxSharedParsing(CliCase):
    """Criteria 4-7: observable sharing, artifact semantics, layout regression."""
    def test_comment_corpus_matches_shared_removal_count(self):
        common = load_script(SCRIPTS / "md-pdf.py")
        samples = [
            ("before <!-- PRIVATE1 --> after\n<!-- PRIVATE2\ncontinued -->\n", ()),
            ("`<!-- inline -->` <!-- PRIVATE1 -->\n", ("<!-- inline -->",)),
            ("plain body\n", ()),
        ]
        for fence in ("```", "~~~"):
            for indent in ("", "  "):
                for closed in (False, True):
                    body = "<!-- PRIVATE1 -->\n" + indent + fence + "html\n" + indent + "<!-- protected -->\n"
                    if closed:
                        body += indent + fence + "\n<!-- PRIVATE2 -->\n"
                    samples.append((body, ("<!-- protected -->",)))
        for body, protected in samples:
            with self.subTest(source=body):
                source = "## Comments\n\n" + body
                stripped, count = common.strip_html_comments(source)
                data, stdout, stderr, _ = self.built("pptx", source=source)
                visible = "".join(map(slide_text, xml_parts(data, r"ppt/slides/slide\d+\.xml")))
                self.assertNotIn("PRIVATE", visible)
                for text in protected:
                    self.assertIn(text, stripped)
                    self.assertIn(text, visible)
                if count:
                    self.assertRegex(stderr, rf"(?i)(?:комментар|comment)[^\n]*\b{count}\b|\b{count}\b[^\n]*(?:комментар|comment)")
                    self.assertNotRegex(stdout, r"(?i)комментар|comment")
                else:
                    self.assertEqual(stderr, "")

    def test_inline_artifact_matches_shared_html_text_and_styles(self):
        common = load_script(SCRIPTS / "md-pdf.py")
        cases = ["до **текст** после", "до *курсив* после", "до `код` после",
                 "a&nbsp;b &amp; c &#8212; &#x2014; &mdash;", "`&nbsp;`",
                 "`[x](y)`", "`**жирный** *курсив*`", "[видимый](https://example.invalid)",
                 "**жирный** и *курсив* и `код`"]
        for source in cases:
            with self.subTest(source=source):
                expected = HtmlRuns(common.inline(source)).chars
                data, _, _, _ = self.built("pptx", source="## Inline\n\n" + source + "\n")
                slide = xml_parts(data, r"ppt/slides/slide\d+\.xml")[0]
                paras = [p for p in slide.iter(A + "p") if slide_text(p) != "Inline" and slide_text(p)]
                self.assertEqual(len(paras), 1)
                self.assertEqual(pptx_chars(paras[0]), expected)

    def isolated_pair(self):
        directory = self.root / "isolated"
        directory.mkdir()
        for name in ("md-pdf.py", "md-pptx.py"):
            shutil.copyfile(SCRIPTS / name, directory / name)
        return directory

    def test_shared_inline_replacement_reaches_artifact(self):
        directory = self.isolated_pair()
        with (directory / "md-pdf.py").open("a", encoding="utf-8") as stream:
            stream.write('\n\ndef inline(text):\n    return "<strong>SHARED_INLINE_9f42</strong>"\n')
        data, _, _, _ = self.built("pptx", scripts=directory)
        roots = xml_parts(data, r"ppt/slides/slide\d+\.xml")
        self.assertIn("SHARED_INLINE_9f42", "".join(map(slide_text, roots)))
        self.assertTrue(any(pptx_chars(p) == HtmlRuns("<strong>SHARED_INLINE_9f42</strong>").chars
                            for root in roots for p in root.iter(A + "p")))

    def test_shared_comment_replacement_reaches_artifact_and_stderr(self):
        directory = self.isolated_pair()
        with (directory / "md-pdf.py").open("a", encoding="utf-8") as stream:
            stream.write('\n\ndef strip_html_comments(text):\n    return "## Shared\\n\\nSHARED_COMMENTS_71bd\\n", 37\n')
        data, stdout, stderr, _ = self.built("pptx", scripts=directory)
        self.assertIn("SHARED_COMMENTS_71bd", "".join(map(slide_text, xml_parts(data, r"ppt/slides/slide\d+\.xml"))))
        self.assertRegex(stderr, r"\b37\b")
        self.assertNotIn("37", stdout)

    def test_missing_dependency_exits_one_and_preserves_output(self):
        directory = self.isolated_pair()
        (directory / "md-pdf.py").unlink()
        for existing in (False, True):
            with self.subTest(existing=existing):
                out = self.root / ("existing.pptx" if existing else "new.pptx")
                original = b"previous presentation\x00\xff"
                if existing:
                    out.write_bytes(original)
                code, _, stderr, _, _ = self.cli("pptx", scripts=directory, output=out)
                self.assertEqual(code, 1, stderr)
                self.assertIn("md-pdf.py", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertRegex(stderr.lower(), r"разбор|markdown|парс|parser|inline|комментар")
                self.assertRegex(stderr.lower(), r"скопир|копир|скача|восстанов|copy|download|restore|install")
                self.assertRegex(stderr.lower(), r"scripts[/\\]|репозитор|канон|claude-toolkit|repository|canonical")
                if existing:
                    self.assertEqual(out.read_bytes(), original)
                else:
                    self.assertFalse(out.exists())

    def test_slide_order_table_bullet_levels_and_literal_code_unchanged(self):
        source = ("# Title\n\nSubtitle\n\n## First\n\n- parent\n  - child\n"
                  "\n| A | B |\n|---|---|\n| 1 | ё |\n\n---\n\n"
                  "```\n## literal\n[x](y) **raw**\n```\n\n## Last\n\nend\n")
        data, _, _, _ = self.built("pptx", source=source)
        slides = xml_parts(data, r"ppt/slides/slide\d+\.xml")
        self.assertEqual(len(slides), 4)
        for slide, label in zip(slides, ("Title", "First", "## literal", "Last")):
            self.assertIn(label, slide_text(slide))
        table = slides[1].find(".//" + A + "tbl")
        self.assertIsNotNone(table)
        self.assertEqual([[slide_text(cell) for cell in row.findall(A + "tc")]
                          for row in table.findall(A + "tr")], [["A", "B"], ["1", "ё"]])
        paras = {slide_text(p): p for p in slides[1].iter(A + "p")}
        for label, marker in (("parent", "•"), ("child", "-")):
            props = paras[label].find(A + "pPr")
            self.assertEqual(props.find(A + "buChar").get("char"), marker)
        self.assertGreater(int(paras["child"].find(A + "pPr").get("marL")),
                           int(paras["parent"].find(A + "pPr").get("marL")))
        code = next(p for p in slides[2].iter(A + "p") if slide_text(p) == "[x](y) **raw**")
        self.assertTrue(all(style[2] for _, style in pptx_chars(code)))


class TypographyRegression(CliCase):
    """Criterion 10: no new normalization/warnings/input writes."""
    def test_author_text_and_hyphens_survive_all_formats(self):
        text = "«ёлка» — всё; инженер-исследователь"
        for fmt in FORMATS:
            with self.subTest(format=fmt):
                source = "Text,Other\n" + text + ",value\n" if fmt == "xlsx" else "## Title\n\n" + text + "\n"
                data, stdout, stderr, rendered = self.built(fmt, source=source)
                self.assertEqual(stderr, "")
                self.assertNotRegex(stdout.lower(), r"типограф|typograph|предупрежд|warning")
                if fmt == "pdf":
                    self.assertEqual(len(rendered), 1)
                    visible = "".join(char for char, _ in HtmlRuns(rendered[0]).chars)
                    self.assertIn(text.replace("инженер-", "инженер\u2011"), visible)
                else:
                    pattern = {"docx": r"word/document\.xml", "pptx": r"ppt/slides/slide\d+\.xml",
                               "xlsx": r"xl/(?:worksheets/sheet\d+|sharedStrings)\.xml"}[fmt]
                    visible = "".join(node.text or "" for root in xml_parts(data, pattern)
                                      for node in root.iter() if node.tag.endswith("}t"))
                    self.assertIn(text, visible)
                    self.assertNotIn("\u2011", visible)


class PdfReaderFixture(unittest.TestCase):
    """Guard against false positives from stale Info and string encoding."""
    def test_literal_and_hex_utf16be(self):
        self.assertEqual(pdf_string(rb"(A \(B\) \\ C)"), "A (B) \\ C")
        raw = b"\xfe\xff" + "Автор & <Co>".encode("utf-16-be")
        self.assertEqual(pdf_string(b"<" + raw.hex().encode() + b">"), "Автор & <Co>")
        self.assertEqual(pdf_string(b"(" + b"".join(f"\\{byte:03o}".encode() for byte in raw) + b")"), "Автор & <Co>")

    def test_latest_xref_selects_effective_info(self):
        data = chrome_pdf()
        previous = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
        for author in (b"stale", b"effective"):
            obj_offset = len(data)
            data += b"4 0 obj\n<< /Author (" + author + b") >>\nendobj\n"
            xref = len(data)
            data += (f"xref\n4 1\n{obj_offset:010d} 00000 n \ntrailer\n"
                     f"<< /Size 5 /Root 1 0 R /Info 4 0 R /Prev {previous} >>\n"
                     f"startxref\n{xref}\n%%EOF\n").encode()
            previous = xref
        self.assertEqual(active_pdf_author(data), "effective")


if __name__ == "__main__":
    unittest.main(verbosity=2)
