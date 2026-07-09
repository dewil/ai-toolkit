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
абзацы, плоские списки (- и 1.), **bold**, *italic*, `code`, fenced-блоки
```...```, цитаты "> ", ссылки [текст](url) и голые URL, разделитель ---.
Вложенные списки и таблицы не поддерживаются - при необходимости дорабатывать
конвертер, не менять формат исходника.
"""

import argparse
import base64
import html
import os
import pathlib
import re
import subprocess
import sys
import tempfile

CHROME = os.environ.get(
    "MD_PDF_CHROME",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

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
a { color: #1a5276; text-decoration: none; }
img { max-width: 100%; }
"""


def inline(text: str) -> str:
    s = html.escape(text)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"(?<![\"'>=\w])(https?://[^\s<)]+)", r'<a href="\1">\1</a>', s)
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
    mode = ""  # "", "ul", "ol", "quote", "pre"

    def close() -> None:
        nonlocal mode
        if mode == "ul":
            out.append("</ul>")
        elif mode == "ol":
            out.append("</ol>")
        elif mode == "quote":
            out.append("</blockquote>")
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
            close()
            out.append(f"<p>{inline(line)}</p>")
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

    esc = author.encode().replace(b"\\", rb"\\").replace(b"(", rb"\(").replace(b")", rb"\)")
    new_dict = obj.group(1)[:-2] + b"\n/Author (" + esc + b")>>"
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
    if not pathlib.Path(CHROME).exists():
        sys.exit("не найден Chrome (путь можно задать через MD_PDF_CHROME)")

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
