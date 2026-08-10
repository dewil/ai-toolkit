#!/usr/bin/env python3
"""Конвертация markdown -> PDF через Chrome headless. Без зависимостей.

Пайплайн: md -> HTML (встроенный мини-конвертер + дефолтные стили) ->
Chrome headless + CDP (Page.printToPDF). Нужен только установленный Google Chrome.

Использование:
    python3 scripts/md-pdf.py note.md                  # рядом появится note.pdf
    python3 scripts/md-pdf.py note.md --out /path/x.pdf
    python3 scripts/md-pdf.py note.md --css custom.css --title "Отчет"
    python3 scripts/md-pdf.py note.md --author "Имя"   # /Author в метаданные PDF

Chrome сам /Author не пишет - при --author поле дописывается инкрементальным
обновлением Info-словаря готового PDF (см. add_author).

Поддерживаемый markdown: YAML-frontmatter (пропускается), заголовки H1-H4,
абзацы (соседние текстовые строки склеиваются в один абзац - мягкий перенос,
как в стандартном markdown), плоские списки (- и 1.), **bold**, *italic*,
`code`, fenced-блоки ```...```, цитаты "> ", ссылки [текст](url) и голые URL,
картинки ![alt](путь) (локальные встраиваются base64 data-URI), разделитель ---,
GFM-таблицы. Контракт строгий: КАЖДАЯ строка таблицы начинается с "|", вторая
строка обязана быть разделителем |---| и совпадать с шапкой по числу ячеек,
иначе блок таблицей не считается и уходит абзацем. Ширина строк тела приводится
к шапке: недостающие ячейки добиваются пустыми, лишние склеиваются в последнюю
колонку с предупреждением в stderr (молча терять текст нельзя - документ уходит
наружу). \\| в ячейке экранирует разделитель. Таблица не может
начаться внутри абзаца - строка с "|" посреди текста остается текстом.
Содержимое `code` выводится буквально: разметка внутри него не разбирается.
Вложенные списки, выравнивание колонок (:--:), жесткий перенос (два пробела в
конце строки) и таблицы без ведущего "|" не поддерживаются - при необходимости
дорабатывать конвертер, не менять формат исходника.
"""

import argparse
import base64
import datetime
import html
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

# Порядок важен: сначала точные пути (дешевая проверка существования файла),
# потом PATH - страховка для нестандартных установок (snap, свой префикс).
# Flatpak сюда не попадает: он экспортирует не chromium, а org.chromium.Chromium
# через flatpak run - такую установку задают через MD_PDF_CHROME.
# Первым все равно идет MD_PDF_CHROME - см. find_chrome.
CHROME_CANDIDATES = (
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)
CHROME_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def find_chrome() -> str:
    """Путь к Chrome: MD_PDF_CHROME -> типовые пути -> PATH. Пусто, если не нашли."""
    env = os.environ.get("MD_PDF_CHROME")
    if env:
        # which() пропускает абсолютный путь как есть, но дает задать
        # MD_PDF_CHROME=google-chrome именем команды
        return shutil.which(env) or env

    for path in CHROME_CANDIDATES:
        if pathlib.Path(path).exists():
            return path

    for name in CHROME_COMMANDS:
        found = shutil.which(name)
        if found:
            return found

    return ""


CHROME = find_chrome()

DEFAULT_CSS = """
@page { size: A4; margin: 18mm 17mm; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.45; color: #1c1e21;
}
h1 { font-size: 17pt; margin: 0 0 3mm; }
h2 { font-size: 13pt; margin: 5mm 0 2mm; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 4mm 0 1.5mm; page-break-after: avoid; }
h4 { font-size: 10.5pt; margin: 3mm 0 1mm; page-break-after: avoid; }
p { margin: 1.5mm 0; }
ul, ol { margin: 1.5mm 0 2mm 5mm; }
li { margin: .8mm 0; page-break-inside: avoid; }
blockquote { margin: 2mm 0 2mm 3mm; padding-left: 3mm; border-left: 2px solid #bbb; color: #444; }
pre { background: #f4f4f4; padding: 2.5mm; margin: 2mm 0; font-size: 9pt; white-space: pre-wrap; page-break-inside: avoid; }
code { font-family: "SF Mono", Menlo, monospace; font-size: .92em; background: #f4f4f4; padding: 0 .3em; }
pre code { padding: 0; background: none; }
hr { border: none; border-top: 1px solid #ccc; margin: 4mm 0; }
table { border-collapse: collapse; margin: 2mm 0 3mm; font-size: 9.5pt; }
th, td { border: 1px solid #ccc; padding: 1.2mm 2mm; text-align: left; vertical-align: top; }
th { background: #f2f2f2; page-break-after: avoid; }
tr { page-break-inside: avoid; }
a { color: #1a5276; text-decoration: none; }
img { max-width: 100%; }
"""

# Стили --separators. Как и PHOTO_CSS, дописываются ПОСЛЕ используемого CSS.
# Черта под заголовком секции и тонкая черта над должностью: в многостраничном
# документе они держат ритм страницы лучше, чем одни отступы.
SEPARATORS_CSS = """
h2 {
  color: #1a5276;
  border-bottom: 1.2px solid #1a5276;
  padding-bottom: .8mm;
}
h4 {
  padding-top: 2mm;
  border-top: 1px solid #c9d4dc;
}
/* первый h4 сразу после h3 - без черты, иначе две линии подряд */
h3 + h4 { border-top: none; padding-top: 0; }
"""


# Стили --photo. Дописываются ПОСЛЕ используемого CSS (в том числе после
# пользовательского --css): флаг работает с чужими стилями без знания про
# класс, а кто хочет переопределить - пишет более специфичный селектор.
PHOTO_CSS = """
img.photo {{
  float: right;
  width: {width};
  height: {height};
  /* у фото на входе произвольные пропорции - без cover портрет растягивается */
  object-fit: cover;
  border-radius: 1.5mm;
  margin: 0 0 3mm 5mm;
  shape-outside: margin-box;
}}
"""


def resolve_photo(photo: pathlib.Path, src_dir: pathlib.Path) -> pathlib.Path:
    """Путь фото: как задан (абсолютный или от cwd), иначе от каталога исходника.
    Нет нигде - падаем до запуска Chrome, а не собираем PDF молча без фото."""
    for cand in (photo, src_dir / photo):
        if cand.is_file():
            resolved = cand.resolve()
            if '"' in str(resolved):
                # src вставляется в HTML-атрибут без эскейпа (embed_images ищет
                # src="([^"]+)" буквально) - кавычка в пути дала бы битый <img>
                sys.exit(f"кавычка в пути фото не поддерживается: {resolved} - переименуйте файл")
            return resolved
    sys.exit(f"нет файла фото: {photo} (искали от текущего каталога и рядом с исходником)")


def inline(text: str) -> str:
    """Inline-разметка одной строки в HTML.

    Готовые фрагменты (содержимое `code`, собранные ссылки) прячутся в stash
    под сентинелы \\x02N\\x03 и возвращаются в самом конце. Это не украшение, а
    единственный способ не дать последующим regex залезть внутрь уже
    разобранного: без stash разметка внутри кода разбиралась повторно
    ("`[x](y)`" терял скобки и url), а autolink дописывал вторую ссылку внутрь
    href уже готовой первой.
    """
    # сентинелы, пришедшие из исходника, вычищаем: иначе чужой индекс подставит
    # не тот фрагмент, а несуществующий уронит скрипт с IndexError
    s = html.escape(text.replace("\x02", "").replace("\x03", ""))
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x02{len(stash) - 1}\x03"

    s = re.sub(r"`([^`]+)`", lambda m: keep(f"<code>{m.group(1)}</code>"), s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    # \x02\x03 исключены из КАЖДОГО класса URL-символов ниже: сентинел кода,
    # попавший в адрес ("[x](https://e/`path`)"), иначе уезжает внутрь href и
    # разворачивается там в <code> - ссылка выглядит целой, а ведет в никуда.
    # Текст ссылки сентинел содержать может: "[`код`](url)" - нормальная
    # разметка, и <code> внутри метки корректен. А вот alt картинки - атрибут,
    # тег в нем недопустим, поэтому сентинелы исключены и оттуда.
    s = re.sub(r"!\[([^\]\x02\x03]*)\]\(([^)\s\x02\x03]+)\)", r'<img src="\2" alt="\1">', s)
    # собранную ссылку прячем целиком: иначе autolink ниже найдет URL внутри ее
    # текста и выдаст вложенный <a> - у части метки оказывался чужой адрес
    s = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s\x02\x03]+)\)",
        lambda m: keep(f'<a href="{m.group(2)}">{m.group(1)}</a>'),
        s,
    )
    s = re.sub(r"(?<![\"'>=\w])(https?://[^\s<)\x02\x03]+)", r'<a href="\1">\1</a>', s)

    # разворачиваем по кругу: спрятанная ссылка может содержать сентинел кода
    for _ in range(len(stash) + 1):
        unstashed = re.sub(r"\x02(\d+)\x03", lambda m: stash[int(m.group(1))], s)
        if unstashed == s:
            break
        s = unstashed
    return s


FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


def strip_html_comments(md: str) -> tuple[str, int]:
    """Вырезает HTML-комментарии вне кода. Возвращает (текст, сколько вырезано).

    Автор исходника прячет в `<!-- ... -->` служебное, потому что ни один
    markdown-рендерер комментарии не показывает. Наш разбор их не знал и
    выводил абзацем - служебные заметки уезжали в документ, уходящий наружу.

    Код не трогаем: там комментарий - пример, а не заметка автора, и вырезать
    его значило бы испортить документацию. "Код" здесь определяется **теми же
    правилами, что и в основном разборе ниже**, иначе защита оказывается уже
    обещанной: fenced-блок опознается по `.strip()` (то есть с отступом тоже) и
    по обоим видам забора, а inline-код прячется до вырезания. Первая версия
    этой функции резала по сегментам `^``` ... ^```` и молча съедала комментарий
    из блока с отступом, из `~~~`-забора и из одинарных бэктиков.
    """
    out: list[str] = []
    total = 0
    fence: str | None = None   # символ открытого забора, пока блок не закрыт
    in_comment = False         # многострочный комментарий продолжается

    for line in md.split("\n"):
        stripped = line.strip()
        m = FENCE_RE.match(stripped)

        if fence is not None:            # внутри блока кода - отдаем как есть
            out.append(line)
            if m and stripped[0] == fence:
                fence = None
            continue
        if m and not in_comment:         # забор открывается только вне комментария
            fence = stripped[0]
            out.append(line)
            continue

        # Inline-код прячем: `<!-- ... -->` в бэктиках - тоже пример, не заметка.
        stash: list[str] = []

        def keep(mo: re.Match) -> str:
            stash.append(mo.group(0))
            return f"\x00{len(stash) - 1}\x00"

        text = line if in_comment else re.sub(r"`[^`]*`", keep, line)

        i = 0
        while True:
            if in_comment:
                end = text.find("-->", i)
                if end == -1:
                    text = text[:i]
                    break
                text = text[:i] + text[end + 3:]
                in_comment = False
                continue
            start = text.find("<!--", i)
            if start == -1:
                break
            total += 1
            end = text.find("-->", start + 4)
            if end == -1:
                text = text[:start]
                in_comment = True
                break
            text = text[:start] + text[end + 3:]
            i = start

        for idx, chunk in enumerate(stash):
            text = text.replace(f"\x00{idx}\x00", chunk)
        out.append(text)

    return "\n".join(out), total


def md_to_html(md: str) -> tuple[str, str]:
    """Возвращает (title из первого H1 или '', html-тело)."""
    md, stripped = strip_html_comments(md)
    if stripped:
        print(f"md-pdf: вырезано HTML-комментариев: {stripped} "
              f"(в документ они не попадают)", file=sys.stderr)
    lines = md.splitlines()
    # пропустить YAML-frontmatter
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1 :]
                break

    title = ""
    out: list[str] = []
    mode = ""  # "", "ul", "ol", "quote", "pre", "table", "p"
    tbl: list[str] = []   # сырые строки таблицы - до валидации
    para: list[str] = []  # сырые строки абзаца - до склейки

    def split_row(row: str) -> list[str]:
        """Ячейки строки таблицы: снимает ровно один крайний |, уважает \\|.

        Экранирование разбирается подменой на сентинелы, а не через lookbehind
        (?<!\\): тот смотрит только на один предыдущий символ и на входе
        "\\\\|" (литерал бэкслеша + НАСТОЯЩИЙ разделитель) ошибочно склеивал
        соседние ячейки. После подмены любой оставшийся | - разделитель.
        """
        s = row.strip()
        if s.startswith("|"):
            s = s[1:]
        s = s.replace("\\\\", "\x00").replace("\\|", "\x01")
        if s.endswith("|"):
            s = s[:-1]
        return [
            c.replace("\x01", "|").replace("\x00", "\\").strip() for c in s.split("|")
        ]

    def is_delim_row(cells: list[str]) -> bool:
        """Строка-разделитель GFM: только ячейки вида ---, :--, --:, :-:."""
        return bool(cells) and all(re.fullmatch(r":?-+:?", c) for c in cells)

    def flush_table() -> None:
        """Буфер tbl -> <table>, но только если это валидная GFM-таблица.

        Валидность определяет ВТОРАЯ строка: она обязана быть разделителем и
        совпадать с шапкой по числу ячеек. Иначе блок - не таблица (формула
        |x|, текст с чертой), и строки уходят ОДНИМ абзацем: молча терять их
        нельзя, а разбивать по строкам - значит ломать мягкий перенос.
        """
        rows = [split_row(r) for r in tbl]
        if len(rows) < 2 or not is_delim_row(rows[1]) or len(rows[0]) != len(rows[1]):
            out.append(f"<p>{inline(' '.join(r.strip() for r in tbl))}</p>")
            return
        header = rows[0]
        width = len(header)
        out.append("<table>")
        out.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in header) + "</tr>")
        # разделитель отсеиваем ПО ПОЗИЦИИ (строка 1), а не по виду строки:
        # иначе легитимная ячейка-прочерк "| - |" исчезает из документа
        for cells in rows[2:]:
            if len(cells) > width:
                # лишние ячейки НЕ отбрасываем: молча потерянный текст в
                # документе, ушедшем заказчику, хуже склейки в последней
                # колонке. Про расхождение с шапкой предупреждаем в stderr
                extra = cells[width - 1 :]
                print(
                    f"warning: строка таблицы шире шапки ({len(cells)} > {width}); "
                    f"лишнее склеено в последнюю колонку: {' | '.join(extra)}",
                    file=sys.stderr,
                )
                # inline() применяем к каждой бывшей ячейке ОТДЕЛЬНО и только
                # потом склеиваем: иначе разметка, разорванная границей ячеек
                # ("| [текст | ](url) |"), после склейки соберется и подменит
                # содержимое - в документе появится ссылка, которой не было
                done = [inline(c) for c in cells[: width - 1]]
                done.append(" ".join(inline(c) for c in extra))
            else:
                done = [inline(c) for c in cells + [""] * (width - len(cells))]
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in done) + "</tr>")
        out.append("</table>")

    def flush_para() -> None:
        """Склеенный абзац. inline() применяется ПОСЛЕ склейки - иначе разметка,
        разорванная переносом строки (**bold\\ntext**), не соберется обратно."""
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def close() -> None:
        nonlocal mode
        if mode == "ul":
            out.append("</ul>")
        elif mode == "ol":
            out.append("</ol>")
        elif mode == "quote":
            out.append("</blockquote>")
        elif mode == "pre":
            out.append("</code></pre>")
        elif mode == "table":
            flush_table()
            tbl.clear()
        elif mode == "p":
            flush_para()
        mode = ""

    for raw in lines:
        line = raw.rstrip()
        if mode == "pre":
            if line.strip().startswith("```"):
                out.append("</code></pre>")
                mode = ""
            else:
                out.append(html.escape(raw))
            continue
        if line.strip().startswith("```"):
            close()
            out.append("<pre><code>")
            mode = "pre"
            continue
        if not line:
            close()
            continue
        m = re.match(r"(#{1,4}) (.*)", line)
        if m:
            close()
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1 and not title:
                title = text
            out.append(f"<h{level}>{inline(text)}</h{level}>")
        elif re.fullmatch(r"-{3,}", line.strip()):
            close()
            out.append("<hr>")
        elif mode != "p" and line.lstrip().startswith("|"):
            # таблица не может прервать абзац (так же в GFM): иначе строка
            # вида "|x| - модуль числа" посреди текста рвала бы его на куски
            if mode != "table":
                close()
                mode = "table"
            tbl.append(line)
        elif line.startswith("- ") or line.startswith("* "):
            if mode != "ul":
                close()
                out.append("<ul>")
                mode = "ul"
            out.append(f"<li>{inline(line[2:])}</li>")
        elif re.match(r"\d+\. ", line):
            if mode != "ol":
                close()
                out.append("<ol>")
                mode = "ol"
            text = re.sub(r"^\d+\. ", "", line)
            out.append(f"<li>{inline(text)}</li>")
        elif line.startswith("> "):
            if mode != "quote":
                close()
                out.append("<blockquote>")
                mode = "quote"
            out.append(f"<p>{inline(line[2:])}</p>")
        else:
            # мягкий перенос: копим сырые строки, склеиваем и размечаем в flush_para
            if mode != "p":
                close()
                mode = "p"
            para.append(line.strip())
    close()
    return title, "\n".join(out)


def embed_images(body: str, base: pathlib.Path) -> str:
    """Локальные <img src> заменяет на base64 data-URI (file:// в печать не попадет)."""

    def repl(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        p = (base / src).resolve()
        if not p.exists():
            return m.group(0)
        ext = p.suffix.lstrip(".").lower().replace("jpg", "jpeg")
        b64 = base64.b64encode(p.read_bytes()).decode()
        return m.group(0).replace(src, f"data:image/{ext};base64,{b64}")

    return re.sub(r'<img[^>]+src="([^"]+)"', repl, body)


def add_author(pdf: bytes, author: str) -> bytes:
    """Дописывает /Author в Info-словарь инкрементальным обновлением PDF.

    Chrome (Skia) пишет только Title/Creator/Producer/даты. Рассчитано на
    классическую структуру (trailer + xref-таблица); если структура другая -
    возвращает PDF без изменений.
    """
    t = re.search(
        rb"trailer\s*<<(.*?)>>\s*startxref\s+(\d+)\s+%%EOF\s*$", pdf[-2048:], re.S
    )
    info_ref = re.search(rb"/Info (\d+) \d+ R", t.group(1)) if t else None
    if not (t and info_ref):
        return pdf
    num = int(info_ref.group(1))
    obj = re.search(rb"(?s)(?:^|\n)%d 0 obj\s*(<<.*?>>)\s*endobj" % num, pdf)
    size = re.search(rb"/Size (\d+)", t.group(1))
    root = re.search(rb"/Root (\d+ \d+ R)", t.group(1))
    if not (obj and size and root):
        return pdf

    # hex-строка UTF-16BE с BOM (как Skia пишет /Title) - не-ASCII корректен,
    # экранирование не нужно
    hexstr = b"<FEFF" + author.encode("utf-16-be").hex().upper().encode() + b">"
    new_dict = obj.group(1)[:-2] + b"\n/Author " + hexstr + b">>"
    out = pdf if pdf.endswith(b"\n") else pdf + b"\n"
    obj_off = len(out)
    out += b"%d 0 obj\n" % num + new_dict + b"\nendobj\n"
    xref_off = len(out)
    out += b"xref\n%d 1\n%010d 00000 n \n" % (num, obj_off)
    out += (
        b"trailer\n<</Size " + size.group(1)
        + b"\n/Root " + root.group(1)
        + b"\n/Info %d 0 R" % num
        + b"\n/Prev " + t.group(2)
        + b">>\nstartxref\n%d\n%%%%EOF\n" % xref_off
    )
    return out


# --- Печать через CDP -------------------------------------------------------
# Раньше печатали CLI-флагом --print-to-pdf. Он не умеет колонтитулы вовсе:
# кастомный header/footer есть только у Page.printToPDF, а CSS-обходов нет -
# Chrome не поддерживает margin-боксы @page (@bottom-center и counters).
# Мини-клиент CDP - тот же, что в chrome-cookies.py (websocket RFC 6455 на
# голых сокетах, без внешних зависимостей); скрипты канона самодостаточны.

def _ws_connect(url: str, timeout: float = 300.0) -> socket.socket:
    m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
    if not m:
        raise RuntimeError(f"неожиданный ws-url: {url}")
    host, port, path = m.group(1), int(m.group(2)), m.group(3)
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(
        (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("ws handshake: соединение закрыто")
        resp += chunk
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        raise RuntimeError("ws handshake: upgrade отклонен")
    return s


def _ws_send(s: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    mask = os.urandom(4)
    ln = len(payload)
    hdr = bytes([0x80 | opcode])
    if ln < 126:
        hdr += bytes([0x80 | ln])
    elif ln < 65536:
        hdr += bytes([0x80 | 126]) + ln.to_bytes(2, "big")
    else:
        hdr += bytes([0x80 | 127]) + ln.to_bytes(8, "big")
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_recv_exact(s: socket.socket, n: int) -> bytes:
    # список + join, а не buf += chunk: PDF приезжает одним кадром в десятки
    # мегабайт, и конкатенация в цикле дает квадратичное копирование
    parts, got = [], 0
    while got < n:
        chunk = s.recv(min(1 << 20, n - got))
        if not chunk:
            raise RuntimeError("ws: соединение закрыто")
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def _ws_recv_msg(s: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        h = _ws_recv_exact(s, 2)
        fin, opcode = h[0] & 0x80, h[0] & 0x0F
        ln = h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(_ws_recv_exact(s, 2), "big")
        elif ln == 127:
            ln = int.from_bytes(_ws_recv_exact(s, 8), "big")
        if h[1] & 0x80:
            mask = _ws_recv_exact(s, 4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(_ws_recv_exact(s, ln)))
        else:
            payload = _ws_recv_exact(s, ln)
        if opcode == 0x9:
            _ws_send(s, payload, opcode=0xA)
            continue
        if opcode == 0xA:
            # unsolicited Pong разрешен RFC 6455 и приходить может в любой
            # момент, в том числе между фрагментами. Без этой ветки он попадал
            # бы в данные и ронял json.loads (и обрывал сборку фрагментов).
            continue
        if opcode == 0x8:
            raise RuntimeError("ws: закрыто со стороны Chrome")
        chunks.append(payload)
        if fin:
            return b"".join(chunks)


def _cdp(s: socket.socket, msg_id: int, method: str, params: dict | None = None) -> dict:
    _ws_send(s, json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode())
    while True:
        msg = json.loads(_ws_recv_msg(s))
        if msg.get("id") == msg_id:
            if "error" in msg:
                raise RuntimeError(f"CDP {method}: {msg['error']}")
            return msg.get("result", {})


LOAD_TIMEOUT = 30.0    # сколько ждем полной загрузки документа перед печатью
PRINT_TIMEOUT = 300.0  # сколько ждем сам PDF: на длинном документе это минуты

FOOTER_STYLE = ("font-size:8px;color:#7a7a7a;width:100%;padding:0 15mm;"
                # box-sizing обязателен: стили страницы в шаблон колонтитула не
                # наследуются, и при content-box ширина стала бы 100%+30mm,
                # а "центр" уехал бы вправо на половину падинга
                "box-sizing:border-box;"
                "font-family:-apple-system,Helvetica,Arial,sans-serif;")


def expand_placeholders(tpl: str, today: str) -> str:
    """{page}/{pages}/{date} -> подстановки Chrome и текущая дата.

    Chrome подставляет номера сам, но только в спаны с классами pageNumber и
    totalPages; писать их руками - лишняя церемония для вызывающего.

    Пользовательский текст экранируется ДО подстановки: колонтитул объявлен
    текстом, а не разметкой, и "<b>черновик</b>" должен напечататься как есть,
    а не сломать оболочку шаблона.
    """
    return (html.escape(tpl)
            .replace("{page}", '<span class="pageNumber"></span>')
            .replace("{pages}", '<span class="totalPages"></span>')
            .replace("{date}", html.escape(today)))


def print_params(header: str = "", footer: str = "") -> dict:
    """Параметры Page.printToPDF. Вынесено отдельно, чтобы проверять на входах,
    а не грепом по исходнику.

    Поля и размер берем из @page используемого CSS (preferCSSPageSize): без
    флагов PDF обязан остаться таким же, каким был на прежней CLI-печати.
    Бриф предлагал задавать marginTop/marginBottom числами - тогда результат
    разошелся бы с текущими документами, а колонтитул и так рисуется внутри
    поля страницы (18 мм по умолчанию, запаса хватает).
    """
    params = {
        "printBackground": True,
        "preferCSSPageSize": True,
        "displayHeaderFooter": bool(header or footer),
    }
    if params["displayHeaderFooter"]:
        # пустой шаблон Chrome подменяет своим дефолтом (URL документа и
        # системная дата), поэтому отсутствующую половину гасим пустым спаном
        params["headerTemplate"] = _tpl(header)
        params["footerTemplate"] = _tpl(footer)
    return params


def _tpl(text: str) -> str:
    return (f'<div style="{FOOTER_STYLE}text-align:center">{text}</div>'
            if text else "<span></span>")


def cdp_print(chrome: str, html_path: pathlib.Path,
              header: str = "", footer: str = "") -> bytes:
    """html -> байты PDF через headless Chrome и Page.printToPDF."""
    with tempfile.TemporaryDirectory() as profile:
        # stderr в файл, а не в PIPE: трубу никто не вычитывает во время
        # ожидания, и болтливый Chrome (verbose-логи, ошибки D-Bus) забил бы
        # ее буфер и встал бы намертво. Из файла причину падения читаем так же
        err_path = pathlib.Path(profile) / "chrome-stderr.log"
        err_file = err_path.open("wb")
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--remote-debugging-port=0", f"--user-data-dir={profile}",
             "--no-first-run", html_path.as_uri()],
            stdout=subprocess.DEVNULL, stderr=err_file,
        )
        try:
            # порт Chrome пишет в файл профиля; --remote-debugging-port=0 просит
            # свободный порт, поэтому заранее он неизвестен
            port_file = pathlib.Path(profile) / "DevToolsActivePort"
            for _ in range(100):
                if port_file.exists() and port_file.read_text().strip():
                    break
                if proc.poll() is not None:
                    # Chrome упал сразу (sandbox, policy, неизвестный флаг) -
                    # ждать 10 секунд и жаловаться на отсутствие файла порта
                    # значит прятать настоящую причину
                    err_file.flush()
                    err = err_path.read_bytes().decode(errors="replace").strip()
                    raise RuntimeError(
                        f"Chrome завершился сразу (код {proc.returncode}): "
                        f"{err[-500:] or 'без вывода'}")
                time.sleep(0.1)
            else:
                raise RuntimeError("Chrome не открыл DevToolsActivePort за 10 с")
            port = int(port_file.read_text().splitlines()[0])

            page = None
            for _ in range(50):
                # свой opener без прокси: системный HTTP_PROXY увел бы запрос
                # к 127.0.0.1 в прокси, и discovery падал бы в корпоративной сети
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
                        targets = json.load(r)
                except (OSError, ValueError):
                    # порт уже записан, но слушатель еще не поднялся - это
                    # штатная гонка старта, а не отказ: пробуем снова
                    time.sleep(0.1)
                    continue
                page = next((x for x in targets if x.get("type") == "page"
                             and x.get("url", "").startswith("file:")), None)
                if page:
                    break
                time.sleep(0.1)
            if not page:
                raise RuntimeError("CDP: вкладка с документом не найдена")

            s = _ws_connect(page["webSocketDebuggerUrl"])
            try:
                # Ждем полной загрузки (шрифты, картинки) И непустой верстки.
                # Молча печатать по истечении ожидания нельзя: недогруженный
                # документ дает пустой PDF, а вызывающий видит бодрое "ok" -
                # поймано на длинном документе, где 5 секунд не хватало.
                # Дедлайн по часам: считать итерации нельзя - каждая делает
                # сетевой вызов неизвестной длительности, и "300 раз по 0.1 с"
                # не равно 30 секундам.
                deadline = time.monotonic() + LOAD_TIMEOUT
                # На время ожидания ставим сокету КОРОТКИЙ таймаут: с общим
                # (300 с, он нужен самой печати) один зависший Runtime.evaluate
                # висел бы впятеро дольше заявленного дедлайна, а проверка
                # времени делается только между вызовами
                s.settimeout(5.0)
                i = 0
                while True:
                    # fonts.ready обязателен: на лениво загружаемом @font-face
                    # печать по одному readyState уходит на fallback-шрифте, а
                    # это другие метрики, переносы и разбиение на страницы
                    r = _cdp(s, 100 + i, "Runtime.evaluate", {
                        # fonts.ready, а не fonts.status: статус означает лишь
                        # "сейчас ничего не грузится" и бывает loaded после
                        # ошибки, а promise разрешается после перерасчета верстки
                        "expression": "(document.fonts ? document.fonts.ready : Promise.resolve())"
                                      ".then(() => 'loaded|' + document.readyState + '|' +"
                                      " (document.body ? document.body.scrollHeight : 0))",
                        "awaitPromise": True,
                        "returnByValue": True})
                    fonts, state, height = (
                        str(r.get("result", {}).get("value", "")).split("|") + ["", "", ""])[:3]
                    if (fonts == "loaded" and state == "complete"
                            and height.isdigit() and int(height) > 0):
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            f"документ не загрузился за {LOAD_TIMEOUT:.0f} с "
                            f"(readyState={state!r}, шрифты={fonts!r}) - печать "
                            f"отменена, чтобы не выдать пустой PDF за готовый")
                    i += 1
                    time.sleep(0.1)
                s.settimeout(PRINT_TIMEOUT)   # печать большого документа - минуты
                res = _cdp(s, 1, "Page.printToPDF", print_params(header, footer))
            finally:
                s.close()
            return base64.b64decode(res["data"])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()      # без reap остается zombie в долгоживущем процессе
            err_file.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="markdown -> PDF через Chrome headless")
    ap.add_argument("src", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=None)
    ap.add_argument("--css", type=pathlib.Path, default=None, help="заменить дефолтные стили")
    ap.add_argument("--title", default=None, help="иначе - первый H1 или имя файла")
    ap.add_argument("--author", default=None, help="записать /Author в метаданные PDF")
    ap.add_argument("--photo", type=pathlib.Path, default=None,
                    help="фото в правом верхнем углу первой страницы (для резюме); "
                         "markdown-исходник не трогается")
    ap.add_argument("--photo-width", default="30mm", help="ширина фото (по умолчанию 30mm)")
    ap.add_argument("--photo-height", default="38mm", help="высота фото (по умолчанию 38mm, пропорция 3x4)")
    ap.add_argument("--footer", default=None,
                    help="нижний колонтитул; плейсхолдеры {page}, {pages}, {date}. "
                         "Типовой случай: --footer \"стр. {page}/{pages}\"")
    ap.add_argument("--header", default=None, help="верхний колонтитул, те же плейсхолдеры")
    ap.add_argument("--separators", action="store_true",
                    help="горизонтальные разделители: черта под H2 и тонкая черта над H4")
    args = ap.parse_args()

    if not args.src.exists():
        sys.exit(f"нет исходника: {args.src}")
    if not CHROME or not pathlib.Path(CHROME).exists():
        sys.exit(
            "не найден Chrome. Установите Google Chrome или Chromium, "
            "либо задайте его через MD_PDF_CHROME - полным путем "
            "(/путь/к/chrome) или именем команды из PATH"
        )

    out = args.out or args.src.with_suffix(".pdf")
    css = args.css.read_text(encoding="utf-8") if args.css else DEFAULT_CSS
    title, body = md_to_html(args.src.read_text(encoding="utf-8"))
    if args.photo:
        photo_path = resolve_photo(args.photo, args.src.parent)
        # абсолютный src без эскейпа: embed_images ищет src="([^"]+)" буквально
        # и заменит его на data-URI (существование файла уже проверено)
        body = f'<img class="photo" src="{photo_path}">' + body
        css += PHOTO_CSS.format(width=args.photo_width, height=args.photo_height)
    if args.separators:
        css += SEPARATORS_CSS
    body = embed_images(body, args.src.parent)
    title = args.title or title or args.src.stem
    doc = (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{css}</style></head>"
        f"<body>{body}</body></html>"
    )

    today = datetime.date.today().strftime("%d.%m.%Y")
    footer = expand_placeholders(args.footer, today) if args.footer else ""
    header = expand_placeholders(args.header, today) if args.header else ""

    with tempfile.TemporaryDirectory() as tmp:
        html_path = pathlib.Path(tmp) / "doc.html"
        html_path.write_text(doc, encoding="utf-8")
        try:
            data = cdp_print(CHROME, html_path, header=header, footer=footer)
        except RuntimeError as exc:
            # остальной скрипт сообщает об ошибках человеческой строкой,
            # трейсбек из печати выбивался бы из этого стиля
            sys.exit(f"печать не удалась: {exc}")
        out.parent.mkdir(parents=True, exist_ok=True)
        if args.author:
            data = add_author(data, args.author)
        out.write_bytes(data)

    print(f"ok: {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
