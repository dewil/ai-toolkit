#!/usr/bin/env python3
"""Ведение живого документа в Google Docs: нормализация, проверки, снимок.

Зачем отдельный инструмент. Документ, который правится программно и его же
читает заказчик, копит класс дефектов, **невидимых для API**: все вызовы
возвращают успех, а видит поломку только человек, открывший документ.
Главный источник - вставленный текст наследует форматирование точки вставки:
заменил абзац-заголовок на "заголовок + тело" - все тело стало заголовком и
уехало в структуру документа. Ответ API при этом ничем не отличается от
исправного (`rules/silent-failure.md`).

Отсюда устройство: правку вносит агент, а этот скрипт после каждой правки
приводит документ к инварианту и проверяет то, что глазами не проверишь.

Подкоманды:
  normalize - стили абзацев: заголовок только по шаблону "N. ЗАГЛАВНЫМИ",
              остальное обычный текст без унаследованного кегля
  blanks    - пустые строки между пунктами по правилу раздела
  check     - непрерывность нумерации и битые ссылки "см. N.M" (только отчет)
  snapshot  - выгрузка документа в markdown (только чтение)

Дефолт у normalize и blanks - dry-run: печатается, что будет сделано. Запись
с --send. check и snapshot не пишут никогда.

Учетные данные - общие с gsheets.py (`~/.config/gsheets/auth.json`), модуль
подгружается сбоку: два места, работающих с ключами, разошлись бы на первом
же фиксе. Нужна область `documents` у того же OAuth-клиента.

Примеры:
  python3 scripts/gdocs.py normalize <doc_id>
  python3 scripts/gdocs.py normalize <doc_id> --send
  python3 scripts/gdocs.py check <doc_id>
  python3 scripts/gdocs.py snapshot <doc_id> -o тз.md
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://docs.googleapis.com/v1/documents"

# Заголовок - строка вида "N. ЗАГЛАВНЫМИ" или "N.M. ЗАГЛАВНЫМИ". Шаблон, а не
# разметка, потому что именно разметка и врет: она наследуется от точки вставки
# и расходится с текстом. Текст - единственный надежный признак.
HEAD_RE = re.compile(r"^(\d+(?:\.\d+)*)\.\s+(\S.*?)\s*$")
# Перекрестная ссылка: номер живет в тексте, а не в разметке списка (см. скилл)
REF_RE = re.compile(r"(?:см\.|смотри)\s*(?:п(?:ункт[а-я]*)?\.?\s*)?"
                    r"(\d+(?:\.\d+)+)", re.I)
# Дыра шире этого - не дыра, а другая система нумерации (или опечатка в номере).
# Без границы "1.1" и "1.1000000000" в одном разделе materializовали список на
# миллиард элементов и вешали прогон.
MAX_GAP = 50
STYLE_BY_LEVEL = {1: "HEADING_1", 2: "HEADING_2"}
DEEP_STYLE = "HEADING_3"
# Заголовок короткий. Порог грубый, зато отсекает пункт договора капсом,
# который иначе уезжает в структуру документа вместе с оглавлением.
MAX_HEAD_LEN = 80
# Стили, которые ставит человек и которые эвристикой по тексту не выводятся.
# Трогать их нельзя: у документа с титульным листом мы бы снесли заголовок.
KEEP_STYLES = ("TITLE", "SUBTITLE")
# Кегли заголовков Docs: H1 20, H2 16, H3 14. Снимаем только их - намеренная
# цитата 12pt и цена 18pt переживают нормализацию.
HEAD_SIZES = (14.0, 16.0, 20.0)


def creds_module():
    """Хелперы авторизации из gsheets.py - соседа по scripts/."""
    path = Path(__file__).resolve().with_name("gsheets.py")
    if not path.exists():
        sys.exit(
            f"нет {path.name} рядом с {Path(__file__).name} - из него берется "
            "авторизация Google.\nОба скрипта живут в scripts/ проекта; возьми "
            "недостающий из канона (scripts/gsheets.py, см. rules/google-sheets-mcp.md)."
        )
    spec = importlib.util.spec_from_file_location("_gsheets", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("load_creds", "access_token"):
        if not callable(getattr(mod, attr, None)):
            sys.exit(f"в {path.name} нет {attr} - он устарел, обнови из канона")
    return mod


def api(token: str, path: str, method: str = "GET", payload: dict | None = None,
        params: dict | None = None) -> dict:
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "replace")[:500]
        hint = ""
        if e.code == 403 and "insufficient" in detail.lower():
            hint = ("\nу токена нет области documents - выдай ее тому же "
                    "OAuth-клиенту и обнови refresh_token")
        elif "revision" in detail.lower():
            hint = ("\nдокумент изменили между чтением и записью - НИЧЕГО из этого "
                    "запроса не применено. Повтори прогон с начала (предпросмотр, "
                    "затем --send); если это blanks, учти, что удаления "
                    "предыдущего запроса уже записаны и откату не подлежат")
        sys.exit(f"HTTP {e.code} от Docs API: {detail}{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"не достучались до Docs API: {e.reason}")


# --- модель документа --------------------------------------------------------

class Para:
    """Абзац документа в удобном виде. Индексы - как их понимает Docs API."""

    __slots__ = ("text", "start", "end", "style", "sizes", "objects", "in_table")

    def __init__(self, text, start, end, style, sizes, objects=False, in_table=False):
        self.text = text
        self.start = start
        self.end = end
        self.style = style
        self.sizes = sizes          # явные fontSize по всем run'ам абзаца
        self.objects = objects      # картинка, разрыв, сноска - что угодно не текст
        self.in_table = in_table

    @property
    def blank(self) -> bool:
        """Пуст ТОЛЬКО если в нем нет ни текста, ни объектов.

        Абзац из одной картинки текста не имеет, и наивная проверка по тексту
        объявляла его пустой строкой - после чего blanks его удалял. Тем же
        путем терялись разрыв страницы, горизонтальная линия и сноска.
        """
        return not self.text.strip() and not self.objects

    def heading(self) -> tuple[str, int] | None:
        """(номер, уровень) если текст выглядит заголовком, иначе None.

        Три условия, и каждое отсекает свой класс ложных срабатываний:
        строчных букв нет (обычный нумерованный пункт), есть хотя бы одна
        буква ("1. 2026" и "1. $100" не заголовки), длина в пределах разумного
        (пункт договора капсом на две строки - не заголовок).
        """
        m = HEAD_RE.match(self.text.strip())
        if not m:
            return None
        title = m.group(2)
        if any(ch.islower() for ch in title):
            return None
        if not any(ch.isalpha() for ch in title):
            return None
        if len(title) > MAX_HEAD_LEN:
            return None
        return m.group(1), m.group(1).count(".") + 1


def walk(content: list, in_table: bool = False):
    """Абзацы документа в порядке следования, включая абзацы внутри таблиц."""
    for el in content or []:
        if "paragraph" in el:
            p = el["paragraph"]
            runs = p.get("elements") or []
            text = "".join((r.get("textRun") or {}).get("content", "") for r in runs)
            sizes = [((r.get("textRun") or {}).get("textStyle") or {}).get("fontSize")
                     for r in runs if r.get("textRun")]
            objects = any(k for r in runs for k in r
                          if k not in ("textRun", "startIndex", "endIndex"))
            # Плавающая картинка висит на АБЗАЦЕ, а не на его элементе: по
            # элементам такой абзац выглядит пустым, и blanks удалил бы его
            # вместе с привязанным объектом
            objects = objects or bool(p.get("positionedObjectIds"))
            style = (p.get("paragraphStyle") or {}).get("namedStyleType", "NORMAL_TEXT")
            yield Para(text.rstrip("\n"), el["startIndex"], el["endIndex"],
                       style, sizes, objects, in_table)
        elif "table" in el:
            for row in el["table"].get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    yield from walk(cell.get("content"), in_table=True)


def load_doc(token: str, doc_id: str) -> dict:
    return api(token, doc_id)


def paragraphs(doc: dict) -> list[Para]:
    return list(walk((doc.get("body") or {}).get("content")))


# --- normalize ---------------------------------------------------------------

def normalize_plan(paras: list[Para]) -> list[tuple[Para, str, str]]:
    """[(абзац, что чиним, целевой стиль)] - расхождения текста и разметки."""
    out = []
    for p in paras:
        head = p.heading()
        if p.blank:
            continue
        if p.style in KEEP_STYLES and not head:
            # TITLE и SUBTITLE ставит человек, эвристикой по тексту они не
            # выводятся - принудив их к NORMAL_TEXT, мы снесли бы титул.
            # Но текст-заголовок под этим стилем - как раз унаследованный
            # стиль, и оставлять его значит создать слепую зону
            continue
        want = (STYLE_BY_LEVEL.get(head[1], DEEP_STYLE) if head else "NORMAL_TEXT")
        if p.style != want:
            why = ("текст не заголовок, а разметка заголовочная"
                   if not head else "заголовок без своей разметки")
            out.append((p, why, want))
        elif not head and inherited_size(p):
            out.append((p, f"унаследованный кегль {inherited_size(p):g}pt", want))
    return out


def inherited_size(p: Para) -> float | None:
    """Кегль, который абзац унаследовал от заголовка, или None.

    Снимаем НЕ любой явный размер: намеренная цитата 12pt, подпись 9pt и цена
    18pt - оформление человека, и стирать его нельзя. Признак наследования
    узкий: размер одинаков во всем абзаце И совпадает с кеглем заголовка Docs.
    Остаточный риск назван в скилле - намеренный 16pt тут не отличим.
    """
    # None у части run'ов - норма: завершающий перевод строки своего кегля
    # обычно не несет. Учитываем только заданные размеры; расходятся они между
    # собой - это ручное оформление, не трогаем
    vals = {(sz or {}).get("magnitude") for sz in p.sizes}
    vals.discard(None)
    if len(vals) != 1:
        return None
    only = vals.pop()
    if float(only) not in HEAD_SIZES:
        return None
    return float(only)


def write(token: str, doc_id: str, reqs: list, revision: str | None) -> dict:
    """batchUpdate, привязанный к прочитанной ревизии.

    Без `requiredRevisionId` правка ложится поверх чужой: между чтением и
    записью соавтор двигает индексы, и вставка попадает в середину чужого
    абзаца. С ним Docs отвечает ошибкой, и это правильный исход - лучше
    отказ, чем разорванный документ.
    """
    if not revision:
        # Незащищенная запись выглядит так же, как защищенная, и расходится с
        # документом только при чужой правке - молча идти на это нельзя
        sys.exit("Docs не вернул revisionId - записывать без привязки к ревизии "
                 "не буду: чужая правка легла бы поверх. Повтори прогон.")
    return api(token, f"{doc_id}:batchUpdate", "POST",
               {"requests": reqs, "writeControl": {"requiredRevisionId": revision}})


def cmd_normalize(token: str, doc_id: str, send: bool) -> int:
    doc = load_doc(token, doc_id)
    paras = paragraphs(doc)
    plan = normalize_plan(paras)
    if not plan:
        print(f"абзацев: {len(paras)}, расхождений стиля нет")
        return 0

    print(f"абзацев: {len(paras)}, к исправлению: {len(plan)}")
    for p, why, want in plan:
        print(f"  - {p.text[:60]!r}: {why} -> {want}")
    if not send:
        print("это предпросмотр; чтобы записать - повторить с --send")
        return 0

    # Смена стиля индексы не двигает, поэтому один батч безопасен - в отличие
    # от вставок и удалений, которые смещают все, что после них
    reqs = []
    for p, _why, want in plan:
        rng = {"startIndex": p.start, "endIndex": p.end}
        reqs.append({"updateParagraphStyle": {
            "range": rng, "paragraphStyle": {"namedStyleType": want},
            "fields": "namedStyleType"}})
        if want == "NORMAL_TEXT" and inherited_size(p):
            reqs.append({"updateTextStyle": {
                "range": rng, "textStyle": {}, "fields": "fontSize"}})
    write(token, doc_id, reqs, doc.get("revisionId"))
    print(f"OK: приведено абзацев {len(plan)}")
    return 0


# --- blanks ------------------------------------------------------------------

def sections(paras: list[Para]) -> list[tuple[Para, list[Para]]]:
    """Разделы верхнего уровня: (заголовок, все абзацы под ним до следующего)."""
    out: list[tuple[Para, list[Para]]] = []
    cur: tuple[Para, list[Para]] | None = None
    for p in paras:
        head = p.heading()
        if head and head[1] == 1:
            cur = (p, [])
            out.append(cur)
        elif cur is not None:
            cur[1].append(p)
    return out


def blanks_plan(paras: list[Para]) -> tuple[list[Para], list[Para]]:
    """(лишние пустые абзацы, абзацы, перед которыми пустой строки не хватает).

    Единого правила нет, и в этом ловушка: привести весь документ к одному
    виду значит испортить половину. Тип раздела определяется по тому, размечены
    ли его подпункты как заголовки блоков с телом.

    Считаем ПРОГОН пустых строк, а не только предыдущий абзац: иначе две
    пустые подряд убирались по одной за прогон, и второй запуск делал новую
    запись - то есть идемпотентности не было.
    """
    extra, missing = [], []
    for _head, body in sections(paras):
        subs = [p for p in body if p.heading()]
        if not subs:
            continue
        # блоки с телом: под подпунктом есть непустой текстовый абзац
        blocky = any(
            any(not q.blank and not q.heading() for q in body[i + 1:i + 3])
            for i, q0 in enumerate(body) if q0.heading())
        want = 1 if blocky else 0
        for i, p in enumerate(body):
            if not p.heading() or i == 0:
                continue
            run = []
            j = i - 1
            while j >= 0 and body[j].blank:
                run.append(body[j])
                j -= 1
            if len(run) > want:
                # run собран от подпункта НАЗАД, поэтому дальние от него - в
                # хвосте списка; их и убираем, оставляя ближайшие
                extra.extend(run[want:])
            elif len(run) < want:
                missing.append(p)
    return extra, missing


def cmd_blanks(token: str, doc_id: str, send: bool) -> int:
    doc = load_doc(token, doc_id)
    paras = paragraphs(doc)
    extra, missing = blanks_plan(paras)
    print(f"абзацев: {len(paras)}, лишних пустых: {len(extra)}, "
          f"не хватает пустых: {len(missing)}")
    for p in extra:
        print(f"  - убрать пустую строку перед {p.end}")
    for p in missing:
        print(f"  + пустая строка перед {p.text[:50]!r}")
    if not (extra or missing):
        return 0
    if not send:
        print("это предпросмотр; чтобы записать - повторить с --send")
        return 0

    # Удаления и вставки - РАЗНЫМИ запросами с перечитыванием между ними.
    # В одном батче операции применяются последовательно, и индексы, посчитанные
    # по исходному документу, после первого удаления уже врут: смешанный батч
    # рвал заголовки пополам.
    if extra:
        reqs = [{"deleteContentRange": {"range": {"startIndex": p.start,
                                                  "endIndex": p.end}}}
                for p in sorted(extra, key=lambda x: x.start, reverse=True)]
        write(token, doc_id, reqs, doc.get("revisionId"))
        print(f"удалено пустых строк: {len(extra)}")
        doc = load_doc(token, doc_id)
        recomputed, missing = blanks_plan(paragraphs(doc))
        if recomputed:
            # После удалений план не должен требовать новых удалений: если
            # требует, документ изменился под нами - молча дописывать нельзя
            sys.stderr.write("документ изменился между запросами - повтори "
                             "прогон, вставки в этот раз не делаю\n")
            return 2
    if missing:
        reqs = [{"insertText": {"location": {"index": p.start}, "text": "\n"}}
                for p in sorted(missing, key=lambda x: x.start, reverse=True)]
        write(token, doc_id, reqs, doc.get("revisionId"))
        print(f"добавлено пустых строк: {len(missing)}")
    return 0


# --- check -------------------------------------------------------------------

def check_report(paras: list[Para]) -> list[str]:
    """Дыры в нумерации и ссылки в никуда. Только отчет - чинит человек."""
    problems = []
    numbers = []
    for p in paras:
        head = p.heading()
        if head:
            numbers.append(head[0])
    known = {".".join(str(int(x)) for x in n.split(".")) for n in numbers}

    by_parent: dict[str, list[int]] = {}
    for num in numbers:
        parent, _, last = num.rpartition(".")
        # Ведущие нули убираем у обеих частей: иначе "01." и "1." расходятся
        # по разным родителям и дубль не виден
        parent = ".".join(str(int(x)) for x in parent.split(".")) if parent else ""
        try:
            by_parent.setdefault(parent, []).append(int(last))
        except ValueError:
            continue
    for parent, items in sorted(by_parent.items()):
        seq = sorted(items)
        where = f"{parent}." if parent else "верхний уровень"
        if seq[-1] - seq[0] > MAX_GAP:
            problems.append(f"разброс номеров ({where}): от {seq[0]} до {seq[-1]} - "
                            "похоже на опечатку в номере, дыры не считаю")
            continue
        gaps = [n for n in range(seq[0], seq[-1] + 1) if n not in seq]
        if gaps:
            problems.append(f"дыра в нумерации ({where}): нет "
                            + ", ".join(f"{parent + '.' if parent else ''}{n}"
                                        for n in gaps))
        dupes = sorted({n for n in seq if seq.count(n) > 1})
        if dupes:
            problems.append(f"повтор номера ({parent or 'верхний уровень'}): "
                            + ", ".join(str(n) for n in dupes))

    for p in paras:
        for ref in REF_RE.findall(p.text):
            if ".".join(str(int(x)) for x in ref.split(".")) not in known:
                problems.append(f"ссылка в никуда: 'см. {ref}' в "
                                f"{p.text.strip()[:60]!r}")
    return problems


def cmd_check(token: str, doc_id: str) -> int:
    paras = paragraphs(load_doc(token, doc_id))
    problems = check_report(paras)
    heads = sum(1 for p in paras if p.heading())
    print(f"абзацев: {len(paras)}, заголовков: {heads}")
    if not problems:
        print("нумерация непрерывна, ссылки на месте")
        return 0
    print(f"НАЙДЕНО: {len(problems)}")
    for x in problems:
        print(f"  - {x}")
    return 1


# --- snapshot ----------------------------------------------------------------

def to_markdown(doc: dict) -> str:
    """Документ в markdown, включая таблицы.

    Снимок рядом с проектом дает обычный diff: история версий Google для
    сравнения неудобна, а документ правит еще и заказчик.
    """
    out: list[str] = [f"# {doc.get('title', 'документ')}", ""]
    skipped: list[str] = []
    if doc.get("tabs"):
        # Многовкладочный документ: legacy-поле body относится только к первой
        # вкладке, и снимок молча оказался бы неполным
        skipped.append(f"вкладок в документе: {len(doc['tabs'])}, выгружена первая")
    for el in (doc.get("body") or {}).get("content") or []:
        if "tableOfContents" in el:
            out.append("<!-- оглавление (собирается Docs, в снимок не идет) -->")
            continue
        if "paragraph" in el:
            p = el["paragraph"]
            text = "".join((r.get("textRun") or {}).get("content", "")
                           for r in p.get("elements") or []).rstrip("\n")
            style = (p.get("paragraphStyle") or {}).get("namedStyleType", "NORMAL_TEXT")
            kinds = {k for r in p.get("elements") or [] for k in r
                     if k not in ("textRun", "startIndex", "endIndex")}
            if kinds and not text.strip():
                # Абзац из картинки или разрыва: пустая строка на его месте
                # сделала бы диф снимка ложным
                out.append(f"<!-- {', '.join(sorted(kinds))} -->")
                continue
            if not text.strip():
                out.append("")
                continue
            if style.startswith("HEADING_"):
                out.append("#" * (int(style[-1]) + 1) + " " + text.strip())
            else:
                out.append(text)
        elif "table" in el:
            rows = []
            for row in el["table"].get("tableRows") or []:
                cells = []
                for cell in row.get("tableCells") or []:
                    txt = " ".join(q.text.strip() for q in walk(cell.get("content"))
                                   if q.text.strip())
                    cells.append(txt.replace("|", "\\|"))
                rows.append(cells)
            if rows:
                # Docs разрешает разное число ячеек в строках; выравниваем,
                # иначе получается невалидная markdown-таблица
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                out.append("| " + " | ".join(rows[0]) + " |")
                out.append("|" + "---|" * width)
                for r in rows[1:]:
                    out.append("| " + " | ".join(r) + " |")
                out.append("")
    if skipped:
        out.append("")
        out.append("<!-- НЕПОЛНЫЙ СНИМОК: " + "; ".join(skipped) + " -->")
    return "\n".join(out).rstrip("\n") + "\n"


def cmd_snapshot(token: str, doc_id: str, out: str | None) -> int:
    doc = load_doc(token, doc_id)
    text = to_markdown(doc)
    if not out:
        sys.stdout.write(text)
        return 0
    Path(out).write_text(text, encoding="utf-8")
    print(f"снимок: {out} ({len(text.splitlines())} строк)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("normalize", "привести стили абзацев к тексту"),
                        ("blanks", "пустые строки по правилу раздела")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("doc_id")
        p.add_argument("--send", action="store_true",
                       help="реально записать (без флага - предпросмотр)")
    p = sub.add_parser("check", help="нумерация и перекрестные ссылки (только отчет)")
    p.add_argument("doc_id")
    p = sub.add_parser("snapshot", help="выгрузить документ в markdown")
    p.add_argument("doc_id")
    p.add_argument("-o", "--out", help="файл; без него - в stdout")
    args = ap.parse_args(argv)

    g = creds_module()
    token = g.access_token(g.load_creds())
    if args.cmd == "normalize":
        return cmd_normalize(token, args.doc_id, args.send)
    if args.cmd == "blanks":
        return cmd_blanks(token, args.doc_id, args.send)
    if args.cmd == "check":
        return cmd_check(token, args.doc_id)
    return cmd_snapshot(token, args.doc_id, args.out)


if __name__ == "__main__":
    sys.exit(main())
