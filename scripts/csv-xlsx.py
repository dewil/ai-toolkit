#!/usr/bin/env python3
"""CSV/TSV -> .xlsx без зависимостей (stdlib: zipfile + XML руками).

Зачем: отдать таблицу заказчику/команде в формате, который откроют Excel и
Numbers, не поднимая openpyxl (канонические скрипты - stdlib-only). Несколько
входных файлов - несколько листов одной книги.

Что делает с данными:
  - первая строка каждого файла - шапка: жирная и закрепленная (--no-header,
    если шапки нет);
  - целые и десятичные числа пишутся числами (сортировка и формулы в Excel
    работают), НО консервативно: "007", "8 903...", id длиннее 15 цифр
    остаются текстом - ведущие нули и точность теряются молча, это хуже,
    чем "число как текст";
  - даты YYYY-MM-DD пишутся датами (настоящими, с датным форматом ячейки);
  - ячейки, начинающиеся с "=", по умолчанию ТЕКСТ. Флаг --formulas включает
    запись формулами (с пересчетом при открытии). Это осознанный дефолт:
    CSV из внешнего источника с "=HYPERLINK(...)" не должен превращаться в
    живую формулу без явного намерения (CSV injection);
  - ширина колонок - по содержимому (с потолком);
  - control-символы, запрещенные в XML, вычищаются.

Свойства файла - по rules/document-metadata.md: автор из --author, иначе
XLSX_AUTHOR, иначе DEFAULT_AUTHOR; заголовок из --title, иначе имя выхода.

Примеры:
  python3 scripts/csv-xlsx.py data.csv                          # data.xlsx рядом
  python3 scripts/csv-xlsx.py a.csv b.tsv --out отчет.xlsx      # два листа
  python3 scripts/csv-xlsx.py план.csv --formulas --author "Имя"
  python3 scripts/csv-xlsx.py dump.csv --delimiter ";"
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import pathlib
import re
import sys
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

DEFAULT_AUTHOR = "dwl"  # личный канон; в форке замени на свой (rules/addressing.md)

# Символы, запрещенные в XML 1.0 (как в md-docx.py)
BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# целое без ведущего нуля ("007" - текст) и без "-0" (Excel покажет 0);
# дробь без ведущего нуля в целой части ("07.5" стала бы 7.5)
INT_RE = re.compile(r"^(0|-?[1-9]\d*)$")
FLOAT_RE = re.compile(r"^-?(0|[1-9]\d*)\.\d+$")


def numeric_or_none(value: str) -> str | None:
    """Значение для <v>, если число представимо в Excel БЕЗ искажения.

    Excel хранит 15 значащих цифр: "1.0000000000000001" молча стала бы 1,
    длинный id - обнулила бы хвост. Все, что не влезает в 15 значащих цифр,
    остается текстом - это и есть обещанная консервативность.
    """
    if not (INT_RE.match(value) or FLOAT_RE.match(value)):
        return None
    digits = value.lstrip("-").replace(".", "").lstrip("0")
    if len(digits) > 15:
        return None
    if value.startswith("-") and not digits:
        return None  # "-0"/"-0.000": Excel покажет 0, минус пропадет молча
    if digits and abs(float(value)) < 2.2250738585072014e-308:
        return None  # субнормальное: ниже диапазона Excel, честнее текстом
    return value
SHEET_BAD = re.compile(r"[\[\]:*?/\\]")

EXCEL_EPOCH = datetime(1899, 12, 30)


def clean(text: str) -> str:
    return escape(BAD_CHARS.sub("", text))


def sheet_name(stem: str, taken: set[str]) -> str:
    """Имя листа по правилам Excel: без []:*?/\\, непустое, до 31 символа,
    уникальное в книге (коллизия -> суффикс -2, -3)."""
    base = SHEET_BAD.sub("-", stem).strip("'").strip() or "Лист"
    base = base[:31]
    name, n = base, 2
    while name.lower() in taken:
        suffix = f"-{n}"
        name = base[: 31 - len(suffix)] + suffix
        n += 1
    taken.add(name.lower())
    return name


def col_letter(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def date_serial(value: str) -> int | None:
    """Серийный номер даты Excel или None, если дату нельзя записать честно.

    База 1899-12-30 верна только с 1900-03-01: раньше вмешивается исторический
    leap-year-баг Excel (фиктивное 29.02.1900), и серийники съезжают на день.
    Такие даты - и вообще все до марта 1900 - остаются текстом.
    """
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if dt < datetime(1900, 3, 1):
        return None
    return (dt - EXCEL_EPOCH).days


# индексы стилей в styles.xml (порядок cellXfs ниже)
XF_PLAIN, XF_BOLD, XF_DATE = 0, 1, 2


def cell_xml(ref: str, raw: str, *, bold: bool, formulas: bool) -> str:
    """Одна ячейка: тип выбирается по содержимому (см. докстринг модуля)."""
    value = BAD_CHARS.sub("", raw)
    style = f' s="{XF_BOLD}"' if bold else ""

    # формулой становится только значение, которое НАЧИНАЛОСЬ с "=" до
    # санитайза: иначе "\x01=..." визуально не формула, а оживает
    if not bold and formulas and raw.startswith("=") and len(value) > 1:
        return f'<c r="{ref}"{style}><f>{clean(value[1:])}</f></c>'
    num = None if bold else numeric_or_none(value)
    if num is not None:
        return f'<c r="{ref}"{style}><v>{num}</v></c>'
    if not bold and DATE_RE.match(value):
        serial = date_serial(value)
        if serial is not None:
            return f'<c r="{ref}" s="{XF_DATE}"><v>{serial}</v></c>'
    if value == "":
        return ""
    # inline string: без sharedStrings проще и достаточно
    space = ' xml:space="preserve"' if value != value.strip() else ""
    return f'<c r="{ref}" t="inlineStr"{style}><is><t{space}>{clean(value)}</t></is></c>'


def sheet_xml(rows: list[list[str]], *, header: bool, formulas: bool) -> str:
    """worksheet: закрепленная шапка, автоширина, ячейки по типам."""
    widths: dict[int, int] = {}
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths.get(i, 0), *(len(line) for line in cell.split("\n")))

    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{min(max(w + 2, 8), 60)}" customWidth="1"/>'
        for i, w in sorted(widths.items())
    )
    freeze = (
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        if header and rows
        else '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
    )

    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            cell_xml(f"{col_letter(i)}{r}", cell, bold=header and r == 1, formulas=formulas)
            for i, cell in enumerate(row)
        )
        body.append(f'<row r="{r}">{cells}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{freeze}"
        + (f"<cols>{cols}</cols>" if cols else "")
        + f'<sheetData>{"".join(body)}</sheetData>'
        "</worksheet>"
    )


STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    # numFmtId 164 - первая пользовательская зона; формат даты явный, а не
    # локалезависимый встроенный
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    "<cellXfs count=\"3\">"
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)

ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    "</Relationships>"
)


def content_types(sheet_count: int) -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{overrides}"
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )


def workbook_xml(names: list[str], *, formulas: bool) -> str:
    sheets = "".join(
        f'<sheet name={quoteattr(BAD_CHARS.sub("", n))} sheetId="{i}" r:id="rId{i}"/>'
        for i, n in enumerate(names, start=1)
    )
    # fullCalcOnLoad: формулы пишутся без кэшированных значений - Excel
    # пересчитает книгу при открытии
    calc = '<calcPr fullCalcOnLoad="1"/>' if formulas else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets>{calc}"
        "</workbook>"
    )


def workbook_rels(sheet_count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}"
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


def core_props(author: str, title: str) -> str:
    """docProps/core.xml по rules/document-metadata.md (как в md-docx.py)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{clean(title)}</dc:title>"
        f"<dc:creator>{clean(author)}</dc:creator>"
        f"<cp:lastModifiedBy>{clean(author)}</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


# Жесткие пределы Excel: за ними лист либо не откроется, либо Excel молча
# порежет при repair. Отказ с адресом ячейки честнее.
MAX_ROWS, MAX_COLS, MAX_CELL, MAX_CELL_LF = 1_048_576, 16_384, 32_767, 253
MAX_FORMULA = 8_192
LINE_BREAK = re.compile(r"\r\n|\r|\n")  # XML нормализует CR в LF - считаем как Excel


def check_limits(name: str, rows: list[list[str]], *, formulas: bool = False) -> None:
    if len(rows) > MAX_ROWS:
        sys.exit(f"лист '{name}': {len(rows)} строк, максимум Excel - {MAX_ROWS}")
    for r, row in enumerate(rows, start=1):
        if len(row) > MAX_COLS:
            sys.exit(f"лист '{name}', строка {r}: {len(row)} колонок, максимум Excel - {MAX_COLS}")
        for i, cell in enumerate(row):
            if len(cell) > MAX_CELL:
                sys.exit(f"лист '{name}', ячейка {col_letter(i)}{r}: "
                         f"{len(cell)} символов, максимум Excel - {MAX_CELL}")
            breaks = len(LINE_BREAK.findall(cell))
            if breaks > MAX_CELL_LF:
                sys.exit(f"лист '{name}', ячейка {col_letter(i)}{r}: "
                         f"{breaks} переносов строки, максимум Excel - {MAX_CELL_LF}")
            if formulas and cell.startswith("=") and len(cell) - 1 > MAX_FORMULA:
                sys.exit(f"лист '{name}', ячейка {col_letter(i)}{r}: "
                         f"формула длиннее {MAX_FORMULA} символов")


def read_rows(path: pathlib.Path, delimiter: str | None) -> list[list[str]]:
    """CSV/TSV -> строки. Разделитель: явный флаг, иначе по расширению."""
    sep = delimiter or ("\t" if path.suffix.lower() == ".tsv" else ",")
    # utf-8-sig: BOM от Excel-экспортов молча портил бы первую ячейку шапки
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [list(row) for row in csv.reader(fh, delimiter=sep)]


def build(sheets: list[tuple[str, list[list[str]]]], author: str, title: str,
          *, header: bool, formulas: bool) -> bytes:
    names = [n for n, _ in sheets]
    for n, rows in sheets:
        check_limits(n, rows, formulas=formulas)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types(len(sheets)))
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("xl/workbook.xml", workbook_xml(names, formulas=formulas))
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels(len(sheets)))
        z.writestr("xl/styles.xml", STYLES)
        for i, (_, rows) in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml",
                       sheet_xml(rows, header=header, formulas=formulas))
        z.writestr("docProps/core.xml", core_props(author, title))
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description="CSV/TSV -> .xlsx, только stdlib")
    ap.add_argument("src", type=pathlib.Path, nargs="+", help="входные CSV/TSV (файл = лист)")
    ap.add_argument("--out", type=pathlib.Path, default=None,
                    help="иначе - имя первого входа с .xlsx")
    ap.add_argument("--title", default=None, help="иначе - имя выходного файла")
    ap.add_argument("--author", default=None, help="иначе XLSX_AUTHOR, иначе " + DEFAULT_AUTHOR)
    ap.add_argument("--delimiter", default=None, help="разделитель; иначе по расширению (.tsv - таб)")
    ap.add_argument("--no-header", action="store_true", help="первая строка - данные, не шапка")
    ap.add_argument("--formulas", action="store_true",
                    help='ячейки с "=" писать формулами (по умолчанию - текстом, см. докстринг)')
    args = ap.parse_args()

    for src in args.src:
        if not src.exists():
            sys.exit(f"нет исходника: {src}")

    out = args.out or args.src[0].with_suffix(".xlsx")
    author = args.author or os.environ.get("XLSX_AUTHOR") or DEFAULT_AUTHOR
    title = args.title or out.stem

    taken: set[str] = set()
    sheets = []
    for src in args.src:
        rows = read_rows(src, args.delimiter)
        if not any(any(cell.strip() for cell in row) for row in rows):
            sys.exit(f"пустой вход: {src} (ни одной непустой ячейки)")
        sheets.append((sheet_name(src.stem, taken), rows))

    data = build(sheets, author, title, header=not args.no_header, formulas=args.formulas)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    total = sum(len(r) for _, r in sheets)
    print(f"ok: {out} ({out.stat().st_size // 1024} KB, листов: {len(sheets)}, "
          f"строк: {total}, автор: {author})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
