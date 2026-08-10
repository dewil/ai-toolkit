#!/usr/bin/env python3
"""markdown -> .pptx без зависимостей (stdlib: zipfile + XML руками).

Зачем: собрать презентацию из заметки/плана, не поднимая python-pptx
(канонические скрипты - stdlib-only). Формат 16:9.

Раскладка markdown по слайдам:
  # H1        - титульный слайд (заголовок + абзацы до первого ## как подтитул)
  ## H2       - новый слайд с заголовком
  ---         - новый слайд без заголовка (продолжение мысли)
  - пункт     - буллет; отступ в два+ пробела - вложенный уровень
  1. пункт    - тот же буллет (нумерация не сохраняется намеренно: на слайде
                порядок и так виден, а перенумерация при правках - лишний шов)
  абзац       - текст без буллета
  **b** *i* `c` - жирный / курсив / моноширинный внутри строки

Слайд не резиновый: при переполнении текст ужимается (autofit), но это
костыль - длинную секцию дели на несколько ##. Картинки не поддерживаются
намеренно (v1); появится нужда - обсуждай расширение, не встраивай молча.

Свойства файла - по rules/document-metadata.md: автор из --author, иначе
PPTX_AUTHOR, иначе DEFAULT_AUTHOR; заголовок из --title, иначе первый H1,
иначе имя файла.

Примеры:
  python3 scripts/md-pptx.py доклад.md                    # доклад.pptx рядом
  python3 scripts/md-pptx.py план.md --out слайды.pptx --author "Имя"
"""

from __future__ import annotations

import argparse
import io
import os
import pathlib
import re
import sys
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

DEFAULT_AUTHOR = "dwl"  # личный канон; в форке замени на свой (rules/addressing.md)

BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
# инлайн-разметка: жирный / курсив / код (как в md-pdf.py, без ссылок - на
# слайде URL читают глазами, кликать некуда при печати/показе)
INLINE = re.compile(r"(\*\*.+?\*\*|(?<!\*)\*[^*\n]+\*(?!\*)|`[^`]+`)")

EMU_W, EMU_H = 12192000, 6858000  # 16:9


def clean(text: str) -> str:
    return escape(BAD_CHARS.sub("", text))


ESCAPED = re.compile(r"\\([*`_])")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
_SENT = "\x02"
_STASH = "\x03"
_CODE = {"*": "s", "`": "b", "_": "u"}
_DECODE = {_SENT + v: k for k, v in _CODE.items()}


def unescape_md(text: str) -> str:
    r"""\* -> * для текста, который не идет через runs_of (заголовки)."""
    return ESCAPED.sub(lambda m: m.group(1), text)


def runs_of(text: str) -> list[tuple[str, str]]:
    r"""Строка -> [(текст, стиль)], стиль: "" | "b" | "i" | "c".

    Код-спаны вырезаются ПЕРВЫМИ: внутри `кода` бэкслеш и маркеры - литералы.
    Потом кодируются экранированные маркеры (\* и т.п.), потом жирный/курсив.
    Вложенная разметка (**a *b* c**) не поддерживается: внешний маркер
    выигрывает, внутренние остаются литералами.
    """
    text = text.replace(_SENT, "").replace(_STASH, "")
    stash: list[str] = []

    def keep(m: re.Match) -> str:
        stash.append(m.group(1))
        return f"{_STASH}{len(stash) - 1}{_STASH}"

    text = CODE_SPAN.sub(keep, text)
    text = ESCAPED.sub(lambda m: _SENT + _CODE[m.group(1)], text)

    def decode(t: str) -> str:
        for enc, char in _DECODE.items():
            t = t.replace(enc, char)
        return t.replace(_SENT, "")

    runs: list[tuple[str, str]] = []

    def emit(piece: str, style: str) -> None:
        # восстановить код-спаны, разрезав кусок по сентинелам стеша
        for j, frag in enumerate(re.split(f"{_STASH}(\\d+){_STASH}", piece)):
            if j % 2:
                runs.append((stash[int(frag)], "c"))
            elif frag:
                runs.append((decode(frag), style))

    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            emit(part[2:-2], "b")
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            emit(part[1:-1], "i")
        else:
            emit(part, "")
    return runs


BULLET_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
# строка markdown-таблицы: начинается и заканчивается трубой, внутри есть еще одна
TABLE_ROW_RE = re.compile(r"^\|.*\|$")


COMMENT_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


def strip_html_comments(text: str) -> tuple[str, int]:
    """Вырезает HTML-комментарии вне кода. Возвращает (текст, счетчик).

    Дубль такой же функции из md-pdf.py, и это осознанно: парсер здесь свой
    (слайды, а не поток), зависимости от md-pdf.py у этого скрипта нет, и
    заводить ее ради одной функции хуже, чем повторить. Расхождением это не
    грозит: границы кода обе версии берут из своего парсера, а они здесь и
    там одинаковые (оба вида забора, отступ по `.strip()`, inline-бэктики).
    """
    out: list[str] = []
    total = 0
    fence: str | None = None
    in_comment = False

    for line in text.split("\n"):
        stripped = line.strip()
        m = COMMENT_FENCE_RE.match(stripped)

        if fence is not None:
            out.append(line)
            if m and stripped[0] == fence:
                fence = None
            continue
        if m and not in_comment:
            fence = stripped[0]
            out.append(line)
            continue

        stash: list[str] = []

        def keep(mo: re.Match) -> str:
            stash.append(mo.group(0))
            return f"\x00{len(stash) - 1}\x00"

        body = line if in_comment else re.sub(r"`[^`]*`", keep, line)

        i = 0
        while True:
            if in_comment:
                end = body.find("-->", i)
                if end == -1:
                    body = body[:i]
                    break
                body = body[:i] + body[end + 3:]
                in_comment = False
                continue
            start = body.find("<!--", i)
            if start == -1:
                break
            total += 1
            end = body.find("-->", start + 4)
            if end == -1:
                body = body[:start]
                in_comment = True
                break
            body = body[:start] + body[end + 3:]
            i = start

        for idx, chunk in enumerate(stash):
            body = body.replace(f"\x00{idx}\x00", chunk)
        out.append(body)

    return "\n".join(out), total


def parse_md(text: str) -> tuple[str | None, list[dict]]:
    """markdown -> (титул H1 | None, слайды).

    Слайд: {"title": str|None, "paras": [(lvl, runs)]}, lvl: None - абзац,
    0/1 - уровень буллета.
    """
    text, stripped = strip_html_comments(text)
    if stripped:
        print(f"md-pptx: вырезано HTML-комментариев: {stripped} "
              f"(в презентацию они не попадают)", file=sys.stderr)
    title: str | None = None
    slides: list[dict] = []
    current: dict | None = None

    def ensure(slide_title: str | None) -> dict:
        nonlocal current
        current = {"title": slide_title, "paras": []}
        slides.append(current)
        return current

    fence = None  # (символ, длина) открывшего маркера
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if fence is not None:
            # внутри кода: закрывает только ТОТ ЖЕ символ той же или большей
            # длины и без хвоста (CommonMark); все прочее - строки кода как
            # есть, с отступами и пустыми строками
            m = re.fullmatch(r"(`{3,}|~{3,})", stripped)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
                continue
            if current is None:
                ensure(None)
            current["paras"].append((None, [(line.rstrip(), "c")]))
            continue
        if not stripped:
            continue
        m = re.match(r"^(`{3,}|~{3,})", stripped)
        if m:  # открытие fence (инфо-строка после маркера допустима)
            fence = (m.group(1)[0], len(m.group(1)))
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            if title is None and current is None:
                title = unescape_md(stripped[2:].strip())
                ensure(None)  # титульный: paras станут подтитулом
                current["is_title"] = True
                current["title"] = title
                continue
            # второй H1 трактуем как обычный слайд - не плодим титулы
            ensure(unescape_md(stripped[2:].strip()))
            continue
        if stripped.startswith("## "):
            ensure(unescape_md(stripped[3:].strip()))
            continue
        h3 = re.match(r"^#{3,6}\s+(.+)$", stripped)
        if h3:  # H3+ - жирная строка внутри слайда
            if current is None:
                ensure(None)
            current["paras"].append((None, [(unescape_md(h3.group(1).strip()), "b")]))
            continue
        # "#hashtag" / "##без пробела" - не заголовки: обычный текст, не терять
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            ensure(None)
            continue
        if TABLE_ROW_RE.match(stripped):
            if current is None:
                ensure(None)
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # строка-разделитель (|---|---|) только помечает шапку, в тело не идет
            if all(re.fullmatch(r":?-{1,}:?", c) for c in cells if c):
                if current["paras"] and current["paras"][-1][0] == "table":
                    current["paras"][-1][1]["header"] = True
                continue
            if current["paras"] and current["paras"][-1][0] == "table":
                current["paras"][-1][1]["rows"].append(cells)
            else:
                current["paras"].append(("table", {"rows": [cells], "header": False}))
            continue
        m = BULLET_RE.match(line)
        if current is None:
            ensure(None)
        if m:
            lvl = 1 if len(m.group(1)) >= 2 else 0
            current["paras"].append((lvl, runs_of(m.group(2))))
        else:
            current["paras"].append((None, runs_of(stripped)))

    # пустые слайды (подряд идущие ---) не рисуем
    slides = [s for s in slides if s.get("is_title") or s["title"] or s["paras"]]
    return title, slides


def run_xml(text: str, style: str, size: int) -> str:
    props = f' lang="ru-RU" sz="{size}"'
    if style == "b":
        props += ' b="1"'
    elif style == "i":
        props += ' i="1"'
    font = '<a:latin typeface="Consolas"/>' if style == "c" else ""
    space = ' xml:space="preserve"' if text != text.strip() else ""
    return f"<a:r><a:rPr{props}>{font}</a:rPr><a:t{space}>{clean(text)}</a:t></a:r>"


def para_xml(lvl: int | None, runs: list[tuple[str, str]], size: int) -> str:
    if lvl is None:
        ppr = "<a:pPr><a:buNone/></a:pPr>"
    else:
        indent = f' marL="{342900 * (lvl + 1)}" indent="-342900"'
        char = "•" if lvl == 0 else "-"
        ppr = f'<a:pPr{indent}><a:buFont typeface="Arial"/><a:buChar char="{char}"/></a:pPr>'
    body = "".join(run_xml(t, s, size) for t, s in runs) or "<a:endParaRPr/>"
    return f"<a:p>{ppr}{body}</a:p>"


def textbox(shape_id: int, name: str, x: int, y: int, cx: int, cy: int,
            paras: str, *, anchor: str = "t") -> str:
    return (
        "<p:sp>"
        f'<p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
        '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square" anchor="{anchor}"><a:normAutofit/></a:bodyPr>'
        f"<a:lstStyle/>{paras}</p:txBody></p:sp>"
    )


ROW_H = 370000          # высота строки таблицы, EMU (~0.4 см при 14pt)
PARA_H = 330000         # оценка высоты текстовой строки 18pt с интервалом
TABLE_FONT = 1400


def table_xml(shape_id: int, spec: dict, x: int, y: int, cx: int) -> tuple[str, int]:
    """graphicFrame с таблицей. Возвращает (xml, занятая высота).

    Границы и заливка шапки задаются в каждой ячейке явно (`a:tcPr`), а не
    через `tableStyleId`: тема таблиц - отдельная часть пакета, без нее
    PowerPoint показал бы таблицу вообще без линий.
    """
    rows = spec["rows"]
    ncols = max(len(r) for r in rows)
    colw = cx // ncols
    grid = "".join(f'<a:gridCol w="{colw}"/>' for _ in range(ncols))
    line = ('<a:lnL w="12700" cap="flat"><a:solidFill><a:srgbClr val="9E9E9E"/>'
            '</a:solidFill></a:lnL>')
    borders = line + line.replace("lnL", "lnR") + line.replace("lnL", "lnT") \
        + line.replace("lnL", "lnB")
    trs = []
    for i, row in enumerate(rows):
        head = spec["header"] and i == 0
        cells = list(row) + [""] * (ncols - len(row))
        tcs = []
        for text in cells:
            runs = runs_of(text)
            if head:
                runs = [(t, "b") for t, _ in runs] or [("", "b")]
            para = ('<a:p><a:pPr><a:buNone/></a:pPr>'
                    + ("".join(run_xml(t, s, TABLE_FONT) for t, s in runs)
                       or "<a:endParaRPr/>") + "</a:p>")
            fill = ('<a:solidFill><a:srgbClr val="EFEFEF"/></a:solidFill>'
                    if head else "")
            tcs.append(
                f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>{para}</a:txBody>'
                f'<a:tcPr marL="45720" marR="45720" anchor="ctr">{borders}{fill}</a:tcPr></a:tc>'
            )
        trs.append(f'<a:tr h="{ROW_H}">{"".join(tcs)}</a:tr>')
    height = ROW_H * len(rows)
    xml = (
        "<p:graphicFrame>"
        f'<p:nvGraphicFramePr><p:cNvPr id="{shape_id}" name="Table {shape_id}"/>'
        '<p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr>'
        "<p:nvPr/></p:nvGraphicFramePr>"
        f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{height}"/></p:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        f'<a:tbl><a:tblPr firstRow="{1 if spec["header"] else 0}"/>'
        f"<a:tblGrid>{grid}</a:tblGrid>{''.join(trs)}</a:tbl>"
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )
    return xml, height


def slide_xml(slide: dict) -> str:
    shapes = []
    if slide.get("is_title"):
        shapes.append(textbox(
            2, "Title", EMU_W // 10, EMU_H * 3 // 10, EMU_W * 8 // 10, EMU_H * 2 // 10,
            f'<a:p><a:pPr algn="ctr"><a:buNone/></a:pPr>{"".join(run_xml(t, s or "b", 4000) for t, s in [(slide["title"], "")])}</a:p>',
            anchor="b",
        ))
        if slide["paras"]:
            sub = "".join(
                para_xml(lvl, runs, 2000) if lvl is not None else (
                    '<a:p><a:pPr algn="ctr"><a:buNone/></a:pPr>'
                    + ("".join(run_xml(t, s, 2000) for t, s in runs) or "<a:endParaRPr/>")
                    + "</a:p>"
                )
                for lvl, runs in slide["paras"]
            )
            shapes.append(textbox(3, "Subtitle", EMU_W // 10, EMU_H * 52 // 100,
                                  EMU_W * 8 // 10, EMU_H * 2 // 10, sub))
    else:
        body_top = EMU_H // 5
        if slide["title"]:
            shapes.append(textbox(
                2, "Title", EMU_W // 20, EMU_H // 20, EMU_W * 9 // 10, EMU_H // 8,
                f'<a:p><a:pPr><a:buNone/></a:pPr>{run_xml(slide["title"], "b", 2800)}</a:p>',
            ))
        else:
            body_top = EMU_H // 10
        # текст и таблицы идут сверху вниз в порядке исходника: копим текстовые
        # абзацы, а на каждой таблице сбрасываем накопленное отдельным блоком
        left, width = EMU_W // 20, EMU_W * 9 // 10
        bottom = EMU_H - EMU_H // 20
        cursor, shape_id, buf = body_top, 4, []

        def flush_text(height: int) -> None:
            nonlocal cursor, shape_id, buf
            if not buf:
                return
            shapes.append(textbox(shape_id, f"Body {shape_id}", left, cursor,
                                  width, max(height, ROW_H), "".join(buf)))
            cursor += height
            shape_id += 1
            buf = []

        for lvl, payload in slide["paras"]:
            if lvl == "table":
                flush_text(len(buf) * PARA_H)
                xml, used = table_xml(shape_id, payload, left, cursor, width)
                shapes.append(xml)
                cursor += used + PARA_H // 2   # воздух под таблицей
                shape_id += 1
            else:
                buf.append(para_xml(lvl, payload, 1800))
        # хвост текста забирает остаток слайда - ему и переносить при переполнении
        flush_text(max(bottom - cursor, PARA_H))

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cSld><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        "<p:grpSpPr/>"
        f"{''.join(shapes)}"
        "</p:spTree></p:cSld></p:sld>"
    )


# Минимальная тема: обязательные clrScheme/fontScheme/fmtScheme. Константа,
# в содержимое слайдов не влияет (текстбоксы стилизуются сами).
THEME = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Минимум">'
    '<a:themeElements>'
    '<a:clrScheme name="М"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
    '<a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
    '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
    '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
    '<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    "</a:clrScheme>"
    '<a:fontScheme name="М"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
    '<a:fmtScheme name="М">'
    '<a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
    '<a:lnStyleLst><a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
    '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
    '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
    "</a:fmtScheme></a:themeElements></a:theme>"
)

CLR_MAP = ('<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
           'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
           'accent6="accent6" hlink="hlink" folHlink="folHlink"/>')

PRES_PROPS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:presentationPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
)

MASTER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    "<p:cSld><p:spTree>"
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    "<p:grpSpPr/></p:spTree></p:cSld>"
    f"{CLR_MAP}"
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    "</p:sldMaster>"
)

LAYOUT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    "<p:cSld><p:spTree>"
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    "<p:grpSpPr/></p:spTree></p:cSld>"
    "</p:sldLayout>"
)

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def rels(pairs: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>'
        for rid, rtype, target in pairs
    )
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG_REL}">{body}</Relationships>')


def content_types(slide_count: int) -> str:
    slides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, slide_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
        f"{slides}"
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )


def presentation_xml(slide_count: int) -> str:
    slide_ids = "".join(
        f'<p:sldId id="{255 + i}" r:id="rId{1 + i}"/>' for i in range(1, slide_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        f'<p:sldSz cx="{EMU_W}" cy="{EMU_H}"/><p:notesSz cx="{EMU_H}" cy="{EMU_W}"/>'
        "</p:presentation>"
    )


def core_props(author: str, title: str) -> str:
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


def build(slides: list[dict], author: str, title: str) -> bytes:
    n = len(slides)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types(n))
        z.writestr("_rels/.rels", rels([
            ("rId1", f"{REL}/officeDocument", "ppt/presentation.xml"),
            ("rId2", f"{PKG_REL.replace('/relationships', '/relationships/metadata/core-properties')}",
             "docProps/core.xml"),
        ]))
        z.writestr("ppt/presentation.xml", presentation_xml(n))
        z.writestr("ppt/_rels/presentation.xml.rels", rels(
            [("rId1", f"{REL}/slideMaster", "slideMasters/slideMaster1.xml")]
            + [(f"rId{1 + i}", f"{REL}/slide", f"slides/slide{i}.xml")
               for i in range(1, n + 1)]
            + [(f"rId{n + 2}", f"{REL}/presProps", "presProps.xml")]
        ))
        z.writestr("ppt/presProps.xml", PRES_PROPS)
        z.writestr("ppt/slideMasters/slideMaster1.xml", MASTER)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", rels([
            ("rId1", f"{REL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", f"{REL}/theme", "../theme/theme1.xml"),
        ]))
        z.writestr("ppt/slideLayouts/slideLayout1.xml", LAYOUT)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", rels([
            ("rId1", f"{REL}/slideMaster", "../slideMasters/slideMaster1.xml"),
        ]))
        z.writestr("ppt/theme/theme1.xml", THEME)
        for i, slide in enumerate(slides, start=1):
            z.writestr(f"ppt/slides/slide{i}.xml", slide_xml(slide))
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", rels([
                ("rId1", f"{REL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ]))
        z.writestr("docProps/core.xml", core_props(author, title))
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description="markdown -> .pptx, только stdlib")
    ap.add_argument("src", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=None)
    ap.add_argument("--title", default=None, help="иначе - первый H1 или имя файла")
    ap.add_argument("--author", default=None, help="иначе PPTX_AUTHOR, иначе " + DEFAULT_AUTHOR)
    args = ap.parse_args()

    if not args.src.exists():
        sys.exit(f"нет исходника: {args.src}")

    out = args.out or args.src.with_suffix(".pptx")
    author = args.author or os.environ.get("PPTX_AUTHOR") or DEFAULT_AUTHOR
    h1, slides = parse_md(args.src.read_text(encoding="utf-8"))
    if not slides:
        sys.exit(f"пустой исходник: {args.src} (ни заголовков, ни текста)")
    title = args.title or h1 or args.src.stem

    data = build(slides, author, title)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"ok: {out} ({out.stat().st_size // 1024} KB, слайдов: {len(slides)}, автор: {author})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
