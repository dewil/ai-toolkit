#!/usr/bin/env python3
"""Конвертация markdown -> .docx. Без зависимостей (только stdlib).

Пара к md-pdf.py: PDF - для чтения и рассылки, docx - когда документ пойдет
на правки (получатель редактирует прямо в файле или включает рецензирование).

Пайплайн: md -> HTML (мини-конвертер md_to_html из md-pdf.py, чтобы разбор
markdown жил в одном месте) -> OOXML-пакет через zipfile. python-docx и lxml
сознательно не используются: docx - это zip с XML, а бинарная зависимость
ломается на машинах без готовых wheels и не приезжает вместе с файлом синка.

Использование:
    python3 scripts/md-docx.py note.md                    # рядом появится note.docx
    python3 scripts/md-docx.py note.md --out /path/x.docx
    python3 scripts/md-docx.py note.md --author "Имя Фамилия"
    python3 scripts/md-docx.py note.md --title "Отчет"

Свойства файла задаются всегда (rules/document-metadata.md): автор из
--author, иначе из DOCX_AUTHOR, иначе DEFAULT_AUTHOR; заголовок - первый H1
исходника, иначе имя файла; даты - текущие; comments/keywords/category/
subject пустые. Следов инструмента сборки в свойствах не остается.

Поддерживаемый markdown - тот же, что у md-pdf.py (разбор общий): заголовки
H1-H4, абзацы с мягким переносом, плоские списки, **bold**, *italic*, `code`,
fenced-блоки, цитаты, ссылки, разделитель ---, GFM-таблицы.

Отличия от PDF-ветки:
- Картинки не встраиваются: вместо ![alt](src) в документ идет "[alt]". Для
  иллюстрированного документа собирай PDF.
- Нумерованные списки нумеруются заново с 1 (Word ведет счет сам), исходный
  номер первого пункта не сохраняется.
- Выравнивание колонок таблиц (:--:) не переносится, как и в PDF-ветке.
"""

import argparse
import datetime
import html.parser
import importlib.util
import io
import os
import pathlib
import re
import sys
import zipfile
from xml.sax.saxutils import escape, quoteattr

DEFAULT_AUTHOR = "dwl"  # личный канон; в форке замени на свой (rules/addressing.md)

# Символы, запрещенные в XML 1.0: управляющие плюс неперсонажи U+FFFE/U+FFFF
# (в UTF-8 они кодируются штатно, но XML-парсер на них падает). Попадут в
# document.xml - Word откажется открыть файл, причем без внятной причины
# ("содержимое повреждено").
BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

# Разметка, которую надо снять с заголовка перед записью в свойства файла:
# md_to_html отдает текст H1 до inline-обработки, поэтому "# [ТЗ](url)" пришел
# бы в dc:title сырым - а правило требует осмысленный заголовок
MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Снимаем только ПАРНЫЕ маркеры и только те, что разбирает наш парсер: сносить
# все звездочки и бэктики нельзя - в заголовке бывает буквальное "/v1/users/*"
# или непарный бэктик, а подчеркивание у нас emphasis вообще не считается
# (в идентификаторах вроде user_auth его надо сохранить).
MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
MD_CODE = re.compile(r"`([^`]+)`")

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

# Стили заданы явно, а не наследуются от дефолтов Word: документ уходит наружу,
# и выглядеть он должен одинаково у любого получателя.
STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="21"/>
</w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>
<w:pPr><w:spacing w:before="40" w:after="40" w:line="276" w:lineRule="auto"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="0" w:after="180"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="34"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="260" w:after="100"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="23"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading4"><w:name w:val="heading 4"/>
<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="160" w:after="60"/></w:pPr>
<w:rPr><w:b/><w:i/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/>
<w:basedOn w:val="Normal"/><w:pPr>
<w:pBdr><w:left w:val="single" w:sz="12" w:space="8" w:color="BBBBBB"/></w:pBdr>
<w:ind w:left="360"/></w:pPr>
<w:rPr><w:i/><w:color w:val="444444"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/>
<w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:fill="F4F4F4"/>
<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>
<w:ind w:left="180"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="18"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="20" w:after="20"/><w:contextualSpacing/></w:pPr></w:style>
<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/>
<w:rPr><w:color w:val="1A5276"/><w:u w:val="single"/></w:rPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>
<w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="4" w:color="CCCCCC"/><w:left w:val="single" w:sz="4" w:color="CCCCCC"/>
<w:bottom w:val="single" w:sz="4" w:color="CCCCCC"/><w:right w:val="single" w:sz="4" w:color="CCCCCC"/>
<w:insideH w:val="single" w:sz="4" w:color="CCCCCC"/><w:insideV w:val="single" w:sz="4" w:color="CCCCCC"/>
</w:tblBorders></w:tblPr></w:style>
</w:styles>"""

# Две нумерации: numId=1 - маркированный список, numId=2 - нумерованный.
# Настоящие списки Word (а не символ в тексте) нужны потому, что документ идет
# на правки: получатель дописывает пункт, и нумерация продолжается сама.
NUMBERING = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/>
<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>
<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"""

# A4 (11906x16838 twips) с полями примерно как в DEFAULT_CSS у md-pdf.py
SECT_PR = (
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1021" w:right="964" w:bottom="1021" w:left="964"'
    ' w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
)


def clean(text: str) -> str:
    """Экранирование для XML-текста плюс срез символов, запрещенных в XML."""
    return escape(BAD_CHARS.sub("", text))


def plain_title(text: str) -> str:
    """Снимает markdown-разметку с заголовка для свойств файла."""
    t = MD_LINK.sub(r"\1", text)
    t = MD_BOLD.sub(r"\1", t)
    t = MD_ITALIC.sub(r"\1", t)
    t = MD_CODE.sub(r"\1", t)
    return t.strip()


def load_md_to_html():
    """Импортирует md_to_html из соседнего md-pdf.py (имя с дефисом - только importlib).

    Разбор markdown намеренно не дублируется: у md-pdf.py он покрыт тестами и
    содержит неочевидные решения (когда строка с "|" не таблица, как считать
    ширину строк, где применять inline). Две копии разошлись бы на первом фиксе.
    """
    path = pathlib.Path(__file__).resolve().with_name("md-pdf.py")
    if not path.exists():
        sys.exit(
            f"нет {path.name} рядом с {pathlib.Path(__file__).name} - он нужен для разбора markdown.\n"
            "Оба скрипта живут в scripts/ проекта; возьми недостающий из канона "
            "(scripts/md-pdf.py, см. skills/md-pdf/SKILL.md)."
        )
    spec = importlib.util.spec_from_file_location("_md_pdf", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.md_to_html


class DocxBody(html.parser.HTMLParser):
    """HTML от md_to_html -> куски body.xml.

    HTML тут не произвольный, а ровно тот, что генерирует md_to_html: плоские
    блоки, вложенность только table/tr/td, pre/code и blockquote/p. Поэтому
    хватает стека inline-стилей и одного приемника параграфов (body или ячейка).
    """

    BLOCKS = {"h1", "h2", "h3", "h4", "p", "li", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body: list[str] = []
        self.rels: list[tuple[str, str]] = []  # (rId, url) для гиперссылок
        self.sink = self.body      # куда падают готовые параграфы
        self.runs: list[str] = []  # runs текущего параграфа
        self.style = "Normal"
        self.numid = 0             # 1 - bullet, 2 - decimal, 0 - не список
        self.fmt: list[str] = []   # активные inline-стили: b, i, code
        self.links: list[str] = []  # стек rId открытых гиперссылок
        self.in_pre = False
        self.in_quote = False
        self.cells: list[str] = []  # готовые <w:tc> текущей строки
        self.rows: list[str] = []   # готовые <w:tr> текущей таблицы
        self.width = 0              # число колонок по шапке

    # --- сборка ---

    def run(self, text: str) -> None:
        if not text:
            return
        # Порядок внутри w:rPr задан схемой (CT_RPr): rStyle, rFonts, b, i, sz.
        # Перестановка не заметна на глаз, но делает документ schema-invalid -
        # строгий потребитель (валидатор Open XML, конвертер) объявит его битым.
        props = []
        if self.links:
            props.append('<w:rStyle w:val="Hyperlink"/>')
        mono = "code" in self.fmt or self.in_pre
        if mono:
            props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
        if "b" in self.fmt:
            props.append("<w:b/>")
        if "i" in self.fmt:
            props.append("<w:i/>")
        if mono:
            props.append('<w:sz w:val="18"/>')
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
        # xml:space="preserve" обязателен: без него Word схлопывает пробелы на
        # границах run'ов, и "**жирный** текст" склеивается в "жирныйтекст"
        r = f'<w:r>{rpr}<w:t xml:space="preserve">{clean(text)}</w:t></w:r>'
        # rStyle красит ссылку, но кликабельной ее делает только обертка
        # w:hyperlink с r:id - без нее связь в document.xml.rels висит впустую.
        # Ссылки - стек, а не одно поле: парсер может выдать вложенные <a>
        # (голый URL внутри текста явной ссылки), и после закрытия внутренней
        # обертка внешней обязана продолжиться
        if self.links:
            r = f'<w:hyperlink r:id="{self.links[-1]}">{r}</w:hyperlink>'
        self.runs.append(r)

    def flush(self) -> None:
        """Закрыть текущий параграф. Пустой параграф не выбрасываем внутри pre и
        в ячейке таблицы: там он несет пустую строку и обязателен по схеме."""
        if not self.runs and not self.in_pre and self.sink is self.body:
            self.style, self.numid = "Normal", 0
            return
        ppr = [f'<w:pStyle w:val="{self.style}"/>']
        if self.numid:
            ppr.append(f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{self.numid}"/></w:numPr>')
        self.sink.append(f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{''.join(self.runs)}</w:p>")
        self.runs = []
        self.style, self.numid = ("Code", 0) if self.in_pre else ("Normal", 0)

    def cell(self, header: bool) -> None:
        """Ячейка из накопленных runs. Пустой <w:p/> обязателен - <w:tc> без
        параграфа делает файл невалидным.

        Жирность шапки ставится при открытии <th> (см. handle_starttag), а не
        здесь: сюда управление приходит, когда runs ячейки уже собраны.
        """
        paras: list[str] = []
        saved, self.sink = self.sink, paras
        self.style = "Normal"
        self.flush()
        self.sink = saved
        if not paras:
            paras.append('<w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr></w:p>')
        shd = '<w:shd w:val="clear" w:fill="F2F2F2"/>' if header else ""
        self.cells.append(f"<w:tc><w:tcPr>{shd}</w:tcPr>{''.join(paras)}</w:tc>")

    def hr(self) -> None:
        """Разделитель - параграф с нижней границей."""
        self.flush()
        self.body.append(
            '<w:p><w:pPr><w:pStyle w:val="Normal"/>'
            '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="CCCCCC"/>'
            "</w:pBdr></w:pPr></w:p>"
        )

    def drop_fmt(self, style: str) -> None:
        """Снимает именно этот стиль, а не последний добавленный.

        Общий парсер выполняет regex bold раньше italic, поэтому на входе
        "***оба** курсив*" он отдает перекрещенный HTML
        (<strong><em>..</strong>..</em>). pop() с конца снял бы там 'i' по
        закрытию </strong>, и остаток абзаца ушел бы жирным.
        """
        for i in range(len(self.fmt) - 1, -1, -1):
            if self.fmt[i] == style:
                del self.fmt[i]
                return

    # --- HTMLParser ---

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        if tag in ("strong", "b"):
            self.fmt.append("b")
        elif tag in ("em", "i"):
            self.fmt.append("i")
        elif tag == "code" and not self.in_pre:
            self.fmt.append("code")
        elif tag == "pre":
            self.flush()
            self.in_pre = True
            self.style = "Code"
        elif tag == "blockquote":
            self.flush()
            self.in_quote = True
        elif tag in ("ul", "ol"):
            self.flush()
            self.numid = 1 if tag == "ul" else 2
        elif tag == "table":
            self.flush()
            self.rows, self.width = [], 0
        elif tag == "tr":
            self.cells = []
        elif tag in ("th", "td"):
            self.runs = []
            if tag == "th":
                self.fmt.append("b")
        elif tag == "a" and a.get("href"):
            rid = f"rId{len(self.rels) + 10}"
            self.rels.append((rid, a["href"]))
            self.links.append(rid)
        elif tag == "hr":
            self.hr()
        elif tag == "img":
            # картинки в docx не поддержаны - см. docstring; alt лучше пустоты
            self.run(f"[{a.get('alt') or 'изображение'}]")
        elif tag in self.BLOCKS:
            self.runs = []
            if tag.startswith("h"):
                self.style = f"Heading{tag[1]}"
            elif tag == "li":
                self.style = "ListParagraph"
            elif tag == "p":
                self.style = "Quote" if self.in_quote else "Normal"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("strong", "b", "em", "i"):
            self.drop_fmt("b" if tag in ("strong", "b") else "i")
        elif tag == "code" and not self.in_pre:
            self.drop_fmt("code")
        elif tag == "pre":
            self.flush()
            self.in_pre = False
            self.style = "Normal"
        elif tag == "blockquote":
            self.flush()
            self.in_quote = False
        elif tag in ("ul", "ol"):
            self.flush()
            self.numid = 0
        elif tag == "a":
            if self.links:
                self.links.pop()
        elif tag in ("th", "td"):
            if tag == "th" and self.fmt:
                self.fmt.pop()
            self.cell(header=(tag == "th"))
        elif tag == "tr":
            if not self.width:
                self.width = len(self.cells)
            self.rows.append(f"<w:tr>{''.join(self.cells)}</w:tr>")
            self.cells = []
        elif tag == "table":
            grid = "".join(
                f'<w:gridCol w:w="{9978 // max(1, self.width)}"/>' for _ in range(self.width)
            )
            self.body.append(
                '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
                '<w:tblW w:w="0" w:type="auto"/></w:tblPr>'
                f"<w:tblGrid>{grid}</w:tblGrid>{''.join(self.rows)}</w:tbl>"
                # пустой параграф после таблицы: две таблицы подряд Word
                # склеивает в одну, а этого исходник не просил
                '<w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr></w:p>'
            )
            self.rows, self.width = [], 0
        elif tag in self.BLOCKS:
            self.flush()

    def handle_startendtag(self, tag: str, attrs) -> None:
        # <hr/> и <hr> должны давать одно и то же: md_to_html пишет второй
        # вариант, и он приходит в handle_starttag, а не сюда
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.in_pre:
            # внутри pre переводы строк значимы: каждая строка - свой параграф
            lines = data.split("\n")
            for i, part in enumerate(lines):
                if i:
                    self.flush()
                self.run(part)
            return
        if not data.strip():
            # Пробел между inline-тегами значим: в "**Срок** *оплаты*" он
            # приходит отдельным чанком, и выброси его - слова склеятся
            # ("Срокоплаты"). А перевод строки между блоками не значим: им
            # md_to_html сшивает блоки, и в документ он попадать не должен.
            if self.runs and "\n" not in data:
                self.run(" ")
            return
        self.run(data.strip("\n"))

    def result(self) -> str:
        self.flush()
        return "".join(self.body)


def core_props(author: str, title: str) -> str:
    """docProps/core.xml по rules/document-metadata.md: автор и заголовок наши,
    даты текущие, остальные поля пустые - никаких следов инструмента сборки."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties'
        ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{clean(title)}</dc:title>"
        f"<dc:creator>{clean(author)}</dc:creator>"
        f"<cp:lastModifiedBy>{clean(author)}</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "<cp:revision>1</cp:revision><dc:subject></dc:subject>"
        "<dc:description></dc:description><cp:keywords></cp:keywords>"
        "<cp:category></cp:category></cp:coreProperties>"
    )


def build(body_html: str, author: str, title: str) -> bytes:
    """Собирает байты .docx из HTML, который вернул md_to_html."""
    parser = DocxBody()
    parser.feed(body_html)
    body = parser.result()

    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{body}{SECT_PR}</w:body></w:document>"
    )
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>',
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>',
    ]
    for rid, url in parser.rels:
        rels.append(
            f'<Relationship Id="{rid}"'
            ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"'
            # URL тоже чистим от запрещенных в XML символов: quoteattr
            # экранирует синтаксис, но U+FFFE оставляет - и rels не парсятся
            f" Target={quoteattr(BAD_CHARS.sub('', url))} TargetMode=\"External\"/>"
        )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels)
        + "</Relationships>"
    )

    buf = io.BytesIO()
    # [Content_Types].xml первым элементом архива - так его кладут все офисные
    # пакеты, и часть ридеров на это рассчитывает
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/numbering.xml", NUMBERING)
        z.writestr("docProps/core.xml", core_props(author, title))
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description="markdown -> .docx, только stdlib")
    ap.add_argument("src", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=None)
    ap.add_argument("--title", default=None, help="иначе - первый H1 или имя файла")
    ap.add_argument("--author", default=None, help="иначе DOCX_AUTHOR, иначе " + DEFAULT_AUTHOR)
    args = ap.parse_args()

    if not args.src.exists():
        sys.exit(f"нет исходника: {args.src}")

    out = args.out or args.src.with_suffix(".docx")
    author = args.author or os.environ.get("DOCX_AUTHOR") or DEFAULT_AUTHOR
    h1, body_html = load_md_to_html()(args.src.read_text(encoding="utf-8"))
    title = args.title or plain_title(h1) or args.src.stem
    data = build(body_html, author, title)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    print(f"ok: {out} ({out.stat().st_size // 1024} KB, автор: {author})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
