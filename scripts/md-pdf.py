#!/usr/bin/env python3
"""Конвертация markdown -> PDF через Chrome headless. Без зависимостей.

Пайплайн: md -> HTML (встроенный мини-конвертер + дефолтные стили) ->
Chrome headless print-to-pdf. Нужен только установленный Google Chrome.

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
import html
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

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


def md_to_html(md: str) -> tuple[str, str]:
    """Возвращает (title из первого H1 или '', html-тело)."""
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


def main() -> int:
    ap = argparse.ArgumentParser(description="markdown -> PDF через Chrome headless")
    ap.add_argument("src", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=None)
    ap.add_argument("--css", type=pathlib.Path, default=None, help="заменить дефолтные стили")
    ap.add_argument("--title", default=None, help="иначе - первый H1 или имя файла")
    ap.add_argument("--author", default=None, help="записать /Author в метаданные PDF")
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
    body = embed_images(body, args.src.parent)
    title = args.title or title or args.src.stem
    doc = (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{css}</style></head>"
        f"<body>{body}</body></html>"
    )

    with tempfile.TemporaryDirectory() as tmp:
        html_path = pathlib.Path(tmp) / "doc.html"
        pdf_path = pathlib.Path(tmp) / "doc.pdf"
        html_path.write_text(doc, encoding="utf-8")
        subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        data = pdf_path.read_bytes()
        if args.author:
            data = add_author(data, args.author)
        out.write_bytes(data)

    print(f"ok: {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
