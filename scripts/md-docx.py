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
    python3 scripts/md-docx.py cv.md --photo photo.jpg --separators   # резюме

Свойства файла задаются всегда (rules/document-metadata.md): автор из
--author, иначе из DOCX_AUTHOR, иначе DEFAULT_AUTHOR; заголовок - первый H1
исходника, иначе имя файла; даты - текущие; comments/keywords/category/
subject пустые. Следов инструмента сборки в свойствах не остается.

Поддерживаемый markdown - тот же, что у md-pdf.py (разбор общий): заголовки
H1-H4, абзацы с мягким переносом, плоские списки, **bold**, *italic*, `code`,
fenced-блоки, цитаты, ссылки, картинки ![alt](путь), разделитель ---,
GFM-таблицы.

Картинки идут в документ настоящими картинками (PNG/JPEG/GIF): файл кладется
в word/media, размер берется из его пиксельных размеров при 96 dpi и ужимается
до ширины полосы набора. --photo ставит фото в правый верхний угол первой
страницы с обтеканием текстом - как у md-pdf.py. --separators включает черту
под заголовками секций и тонкую черту над должностями (для резюме).

Фото заполняет рамку --photo-width x --photo-height целиком: лишнее срезается
симметрично (a:srcRect), как object-fit: cover в PDF-ветке. Пересжатия файла
при этом не происходит - кадрируется отображение, а не сама картинка.

Отличия от PDF-ветки:
- Картинка из сети (http://...) не скачивается - в документ идет "[alt]".
  То же при нечитаемом или неизвестном формате файла.
- Нумерованные списки нумеруются заново с 1 (Word ведет счет сам), исходный
  номер первого пункта не сохраняется.
- Выравнивание колонок таблиц (:--:) не переносится, как и в PDF-ветке.
"""

import argparse
import datetime
import hashlib
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

IMAGE_MIME = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif"}


def content_types(image_exts) -> str:
    """[Content_Types].xml. Расширения картинок объявляются Default'ами: без
    записи про png/jpeg Word считает пакет поврежденным и отказывается открыть."""
    defaults = "".join(
        f'<Default Extension="{ext}" ContentType="{IMAGE_MIME[ext]}"/>'
        for ext in sorted(image_exts)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
{defaults}
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

# Черта под заголовком секции и тонкая черта над должностью - зеркало
# горизонтальных разделителей в PDF-ветке (h2 { border-bottom }, h4
# { border-top }). w:sz у границы - в восьмых долях пункта: 8 = 1pt, 6 = 0.75pt.
# Порядок внутри w:pPr задан схемой (CT_PPr): keepNext -> pBdr -> spacing,
# перестановка делает документ невалидным.
H2_BORDER = '<w:pBdr><w:bottom w:val="single" w:sz="8" w:space="2" w:color="1A5276"/></w:pBdr>'
H2_COLOR = '<w:color w:val="1A5276"/>'
H4_BORDER = '<w:pBdr><w:top w:val="single" w:sz="6" w:space="4" w:color="C9D4DC"/></w:pBdr>'
# Снятие черты у первой должности сразу после названия компании - иначе две
# линии подряд. Прямое форматирование абзаца, а не стиль: в docx нет селектора
# "соседний элемент", которым это решается в CSS (h3 + h4).
H4_NO_BORDER = '<w:pBdr><w:top w:val="none" w:sz="0" w:space="0" w:color="auto"/></w:pBdr>'


# Стили заданы явно, а не наследуются от дефолтов Word: документ уходит наружу,
# и выглядеть он должен одинаково у любого получателя.
def styles_xml(separators: bool = False) -> str:
    h2_bdr = H2_BORDER if separators else ""
    h2_color = H2_COLOR if separators else ""
    h4_bdr = H4_BORDER if separators else ""
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
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
<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/>{h2_bdr}<w:spacing w:before="260" w:after="100"/></w:pPr>
<w:rPr><w:b/>{h2_color}<w:sz w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="23"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading4"><w:name w:val="heading 4"/>
<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/>{h4_bdr}<w:spacing w:before="160" w:after="60"/></w:pPr>
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

# Нумераций столько, сколько нумерованных списков в документе, плюс одна на
# маркеры: abstractNum описывает ВИД списка (0 - маркер, 1 - цифры), а w:num -
# отдельный экземпляр счета. Word перезапускает нумерацию с единицы на каждом
# w:num, и только на нем: w:start внутри abstractNum на это не влияет, потому
# что abstractNum общий. Один numId на все нумерованные списки давал Word'у
# один список, разорванный на куски, - второй список начинался с 5, третий с 11.
# Настоящие списки Word (а не символ в тексте) нужны потому, что документ идет
# на правки: получатель дописывает пункт, и нумерация продолжается сама.
BULLET_NUMID = 1        # маркированные списки делят один numId: счета у них нет
FIRST_OL_NUMID = 2      # нумерованные начинаются отсюда, по одному на список
NUMBERING_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/>
<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>
<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>
"""


def numbering_xml(ol_count: int) -> str:
    """numbering.xml под фактическое число нумерованных списков в документе."""
    nums = [f'<w:num w:numId="{BULLET_NUMID}"><w:abstractNumId w:val="0"/></w:num>']
    for i in range(ol_count):
        nums.append(f'<w:num w:numId="{FIRST_OL_NUMID + i}">'
                    f'<w:abstractNumId w:val="1"/></w:num>')
    return NUMBERING_HEAD + "\n" + "\n".join(nums) + "\n</w:numbering>"

# A4 (11906x16838 twips) с полями примерно как в DEFAULT_CSS у md-pdf.py
SECT_PR = (
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1021" w:right="964" w:bottom="1021" w:left="964"'
    ' w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
)


# Полоса набора: ширина страницы минус поля, в EMU (1 twip = 635 EMU).
CONTENT_WIDTH_EMU = (11906 - 964 * 2) * 635
EMU_PER_PX = 9525  # 96 dpi: 914400 EMU в дюйме / 96
UNITS_EMU = {"mm": 36000, "cm": 360000, "in": 914400, "pt": 12700, "px": EMU_PER_PX}


def clean(text: str) -> str:
    """Экранирование для XML-текста плюс срез символов, запрещенных в XML."""
    return escape(BAD_CHARS.sub("", text))


def to_emu(value: str) -> int:
    """Размер с единицей ("30mm", "1.2in", "90px") в EMU. Без единицы - мм."""
    m = re.fullmatch(r"\s*([\d.]+)\s*(mm|cm|in|pt|px)?\s*", value)
    if not m:
        sys.exit(f"не разобран размер: {value!r} (ожидается вида 30mm, 1.2in, 90px)")
    return int(float(m.group(1)) * UNITS_EMU[m.group(2) or "mm"])


def image_size(data: bytes):
    """Пиксельные размеры картинки из ее заголовка: (формат, ширина, высота).

    Формат определяется по сигнатуре, а не по расширению файла: .jpg с PNG
    внутри - обычное дело после пересохранения, и Word на несовпадении типа
    части и содержимого спотыкается. Не разобрали - None, вызывающий вернется
    к текстовому "[alt]".

    Своего декодера не заводим - читаем только заголовки, поэтому Pillow не
    нужен (бинарная зависимость ломается на машинах без готовых wheels).
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return "png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            # SOFn несет размеры кадра; C4/C8/CC - таблицы Хаффмана и arithmetic
            # coding, они тоже в диапазоне C0-CF, но размеров не содержат
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (
                    "jpeg",
                    int.from_bytes(data[i + 7 : i + 9], "big"),
                    int.from_bytes(data[i + 5 : i + 7], "big"),
                )
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2  # маркеры без полезной нагрузки - длины у них нет
                continue
            i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
        return None
    return None


def fit(iw: int, ih: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Вписывает картинку iw x ih (EMU) в рамку, сохраняя пропорции."""
    if iw <= 0 or ih <= 0:
        return box_w, box_h
    k = min(box_w / iw, box_h / ih)
    return max(1, int(iw * k)), max(1, int(ih * k))


def cover_crop(iw: int, ih: int, box_w: int, box_h: int) -> str:
    """a:srcRect для заполнения рамки без искажения - аналог object-fit: cover.

    Картинка растянулась бы на рамку целиком (a:stretch), поэтому "кадрирование"
    делается выбором видимой части исходника: лишнее срезается симметрично с
    двух сторон - как object-position: center в PDF-ветке. Пустая строка, если
    пропорции совпали и резать нечего.

    Единица a:srcRect - тысячная доля процента, поэтому 100000 = вся сторона.
    """
    if iw <= 0 or ih <= 0 or box_w <= 0 or box_h <= 0:
        return ""
    if iw * box_h > ih * box_w:  # картинка шире рамки - режем бока
        keep = (ih * box_w) / (box_h * iw)
        side = round((1 - keep) / 2 * 100000)
        return f'<a:srcRect l="{side}" r="{side}"/>' if side else ""
    keep = (iw * box_h) / (box_w * ih)  # выше рамки - режем верх и низ
    side = round((1 - keep) / 2 * 100000)
    return f'<a:srcRect t="{side}" b="{side}"/>' if side else ""


def _pic(rid: str, num: int, alt: str, cx: int, cy: int, crop: str = "") -> str:
    """Общая для inline и anchor часть: сама картинка внутри graphic-фрейма.

    Порядок внутри a:blipFill задан схемой (CT_BlipFillProperties):
    blip -> srcRect -> stretch.
    """
    name = f"Picture {num}"
    return (
        f'<wp:docPr id="{num}" name="{name}" descr="{escape(alt)}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData'
        ' uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{num}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/>{crop}'
        "<a:stretch><a:fillRect/></a:stretch></pic:blipFill>"
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic>"
    )


def drawing_inline(rid: str, num: int, alt: str, cx: int, cy: int) -> str:
    """Картинка в строке текста (обычное ![alt](src))."""
    return (
        '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f"{_pic(rid, num, alt, cx, cy)}</wp:inline></w:drawing></w:r>"
    )


def drawing_anchor(rid: str, num: int, alt: str, cx: int, cy: int, crop: str = "") -> str:
    """Плавающая картинка в правом верхнем углу с обтеканием (это и есть --photo).

    Порядок дочерних элементов wp:anchor задан схемой (CT_Anchor):
    simplePos -> positionH -> positionV -> extent -> effectExtent -> wrap ->
    docPr -> cNvGraphicFramePr -> graphic.
    """
    return (
        '<w:r><w:drawing><wp:anchor distT="0" distB="114300" distL="114300" distR="0"'
        ' simplePos="0" relativeHeight="251658240" behindDoc="0" locked="0"'
        ' layoutInCell="1" allowOverlap="0">'
        '<wp:simplePos x="0" y="0"/>'
        '<wp:positionH relativeFrom="margin"><wp:align>right</wp:align></wp:positionH>'
        '<wp:positionV relativeFrom="margin"><wp:align>top</wp:align></wp:positionV>'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        '<wp:wrapSquare wrapText="bothSides"/>'
        f"{_pic(rid, num, alt, cx, cy, crop)}</wp:anchor></w:drawing></w:r>"
    )


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

    def __init__(self, src_dir: pathlib.Path | None = None, separators: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.src_dir = src_dir or pathlib.Path(".")
        self.separators = separators
        self.body: list[str] = []
        self.rels: list[tuple[str, str]] = []  # (rId, url) для гиперссылок
        self.media: list[tuple[str, str, bytes]] = []  # (rId, имя в word/media, байты)
        self.media_index: dict[bytes, str] = {}  # sha256 картинки -> rId, чтобы не дублировать
        self.pic_seq = 0           # сквозной номер рисунка для docPr id
        self.sink = self.body      # куда падают готовые параграфы
        self.runs: list[str] = []  # runs текущего параграфа
        self.style = "Normal"
        self.prev_style = ""       # стиль предыдущего абзаца - для h3 + h4
        self.ppr_extra = ""        # прямое форматирование текущего абзаца
        self.numid = 0             # нумерация ТЕКУЩЕГО абзаца: 1 - bullet, 2 - decimal
        self.list_num = 0          # нумерация открытого списка; 0 - список не открыт
        self.ol_count = 0          # сколько нумерованных списков встретилось
        self.fmt: list[str] = []   # активные inline-стили: b, i, code
        self.links: list[str] = []  # стек rId открытых гиперссылок
        self.in_pre = False
        self.in_quote = False
        self.cells: list[str] = []  # готовые <w:tc> текущей строки
        self.rows: list[str] = []   # готовые <w:tr> текущей таблицы
        self.width = 0              # число колонок по шапке

    # --- сборка ---

    def rel_id(self) -> str:
        """Общий счетчик связей: гиперссылки и картинки живут в одном
        document.xml.rels, и совпавший rId увел бы картинку по чужому адресу."""
        return f"rId{len(self.rels) + len(self.media) + 10}"

    def add_media(self, data: bytes, fmt: str) -> tuple[str, int]:
        """Байты картинки -> (rId связи, номер для docPr).

        Один и тот же файл кладется в пакет один раз (логотип на каждой
        странице не должен утяжелять документ в N раз), а номер рисунка все
        равно свой: docPr id обязан быть уникальным для каждого вхождения.
        """
        key = hashlib.sha256(data).digest()
        if key not in self.media_index:
            rid = self.rel_id()
            # расширение по сигнатуре, а не по имени файла: Default в
            # [Content_Types].xml объявляется именно по нему
            self.media.append((rid, f"image{len(self.media) + 1}.{fmt}", data))
            self.media_index[key] = rid
        self.pic_seq += 1
        return self.media_index[key], self.pic_seq

    def image(self, src: str, alt: str) -> bool:
        """Кладет картинку в word/media и дописывает run. False - не вышло."""
        if src.startswith(("http://", "https://", "data:")):
            return False
        path = pathlib.Path(src)
        if not path.is_absolute():
            path = self.src_dir / path
        try:
            data = path.read_bytes()
        except OSError:
            return False
        info = image_size(data)
        if not info:
            return False
        fmt, px_w, px_h = info
        cx, cy = px_w * EMU_PER_PX, px_h * EMU_PER_PX
        if cx > CONTENT_WIDTH_EMU:
            cx, cy = fit(cx, cy, CONTENT_WIDTH_EMU, cy)
        rid, num = self.add_media(data, fmt)
        self.runs.append(drawing_inline(rid, num, alt, cx, cy))
        return True

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
            self.style, self.numid, self.ppr_extra = "Normal", 0, ""
            return
        ppr = [f'<w:pStyle w:val="{self.style}"/>']
        if self.numid:
            ppr.append(f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{self.numid}"/></w:numPr>')
        ppr.append(self.ppr_extra)
        self.sink.append(f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{''.join(self.runs)}</w:p>")
        self.runs = []
        if self.sink is self.body:
            # ячейки таблицы в счет не идут: "предыдущий абзац" для h3 + h4 -
            # предыдущий абзац документа, а не последняя ячейка внутри таблицы
            self.prev_style = self.style
        self.style, self.numid = ("Code", 0) if self.in_pre else ("Normal", 0)
        self.ppr_extra = ""

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
            if tag == "ul":
                self.list_num = BULLET_NUMID
            else:
                # свой numId на каждый список - иначе Word считает их одним
                # списком и продолжает счет сквозь весь документ
                self.ol_count += 1
                self.list_num = FIRST_OL_NUMID + self.ol_count - 1
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
            rid = self.rel_id()
            self.rels.append((rid, a["href"]))
            self.links.append(rid)
        elif tag == "hr":
            self.hr()
        elif tag == "img":
            # не встроилась (сеть, нет файла, незнакомый формат) - alt лучше
            # пустоты: получатель увидит, что здесь была картинка
            if not (a.get("src") and self.image(a["src"], a.get("alt") or "")):
                self.run(f"[{a.get('alt') or 'изображение'}]")
        elif tag in self.BLOCKS:
            self.runs = []
            if tag.startswith("h"):
                self.style = f"Heading{tag[1]}"
                if tag == "h4" and self.separators and self.prev_style == "Heading3":
                    self.ppr_extra = H4_NO_BORDER
            elif tag == "li":
                self.style = "ListParagraph"
                # нумерацию берем у открытого списка на КАЖДОМ пункте: flush()
                # обнуляет self.numid после каждого абзаца (иначе она протекла
                # бы в следующий), поэтому без этой строки маркер получает
                # только первый пункт списка, а остальные идут без маркера
                self.numid = self.list_num
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
            self.list_num = 0
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


def add_photo(
    parser: "DocxBody", body: str, photo: pathlib.Path, width: str, height: str
) -> str:
    """Плавающее фото в правом верхнем углу первой страницы.

    Якорь обязан жить внутри абзаца, поэтому run вставляется первым в первый
    абзац документа (обычно это H1 с именем). Если документ начинается не с
    абзаца (например, сразу таблицей) - заводим свой пустой абзац: вставлять
    якорь в ячейку нельзя, оттуда он не всплывет к полю страницы.
    """
    data = photo.read_bytes()
    info = image_size(data)
    if not info:
        sys.exit(f"не разобран формат фото: {photo} (ожидается PNG, JPEG или GIF)")
    fmt, px_w, px_h = info
    # рамка заполняется целиком, лишнее кадрируется - как object-fit: cover в
    # PDF-ветке, иначе портрет в квадратной рамке уезжает от макета PDF
    cx, cy = to_emu(width), to_emu(height)
    crop = cover_crop(px_w, px_h, cx, cy)
    rid, num = parser.add_media(data, fmt)
    run = drawing_anchor(rid, num, "фото", cx, cy, crop)

    if body.startswith("<w:p>"):
        head, sep, tail = body.partition("</w:pPr>")
        if sep:
            return head + sep + run + tail
    return f'<w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr>{run}</w:p>{body}'


def resolve_photo(photo: pathlib.Path, src_dir: pathlib.Path) -> pathlib.Path:
    """Путь фото: как задан (абсолютный или от cwd), иначе от каталога исходника.
    Нет нигде - падаем сразу, а не собираем документ молча без фото."""
    for cand in (photo, src_dir / photo):
        if cand.is_file():
            return cand
    sys.exit(f"нет файла фото: {photo} (искали от текущего каталога и рядом с исходником)")


def build(
    body_html: str,
    author: str,
    title: str,
    src_dir: pathlib.Path | None = None,
    separators: bool = False,
    photo: pathlib.Path | None = None,
    photo_width: str = "30mm",
    photo_height: str = "38mm",
) -> bytes:
    """Собирает байты .docx из HTML, который вернул md_to_html."""
    parser = DocxBody(src_dir, separators)
    parser.feed(body_html)
    body = parser.result()

    if photo:
        body = add_photo(parser, body, photo, photo_width, photo_height)

    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        # пространства имен графики объявлены на корне, а не на каждом
        ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
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
    for rid, name, _ in parser.media:
        rels.append(
            f'<Relationship Id="{rid}"'
            ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"'
            f' Target="media/{name}"/>'
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
    exts = {name.rsplit(".", 1)[1] for _, name, _ in parser.media}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types(exts))
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", styles_xml(separators))
        z.writestr("word/numbering.xml", numbering_xml(parser.ol_count))
        for _, name, data in parser.media:
            # картинки уже сжаты своим кодеком - deflate только тратит время
            z.writestr(f"word/media/{name}", data, zipfile.ZIP_STORED)
        z.writestr("docProps/core.xml", core_props(author, title))
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description="markdown -> .docx, только stdlib")
    ap.add_argument("src", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=None)
    ap.add_argument("--title", default=None, help="иначе - первый H1 или имя файла")
    ap.add_argument("--author", default=None, help="иначе DOCX_AUTHOR, иначе " + DEFAULT_AUTHOR)
    ap.add_argument("--photo", type=pathlib.Path, default=None,
                    help="фото в правом верхнем углу первой страницы (для резюме); "
                         "markdown-исходник не трогается")
    ap.add_argument("--photo-width", default="30mm", help="ширина рамки фото (по умолчанию 30mm)")
    ap.add_argument("--photo-height", default="38mm",
                    help="высота рамки фото (по умолчанию 38mm, пропорция 3x4)")
    ap.add_argument("--separators", action="store_true",
                    help="горизонтальные разделители: черта под H2 и тонкая черта над H4")
    args = ap.parse_args()

    if not args.src.exists():
        sys.exit(f"нет исходника: {args.src}")

    out = args.out or args.src.with_suffix(".docx")
    author = args.author or os.environ.get("DOCX_AUTHOR") or DEFAULT_AUTHOR
    h1, body_html = load_md_to_html()(args.src.read_text(encoding="utf-8"))
    title = args.title or plain_title(h1) or args.src.stem
    photo = resolve_photo(args.photo, args.src.parent) if args.photo else None
    data = build(
        body_html, author, title,
        src_dir=args.src.parent,
        separators=args.separators,
        photo=photo,
        photo_width=args.photo_width,
        photo_height=args.photo_height,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    print(f"ok: {out} ({out.stat().st_size // 1024} KB, автор: {author})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
