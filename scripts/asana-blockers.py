#!/usr/bin/env python3
"""Связи задач (блокировки) текстом в описании задач Asana.

Зачем не нативные зависимости: `POST /tasks/{gid}/addDependencies` отдает
402 Payment Required - это премиум-фича. Текстом получается не хуже и даже
нагляднее: блок стоит первым в описании, виден и в карточке, и в предпросмотре.

Смысл приема: вместо ежедневного сдвига сроков задача честно говорит, чем
именно она держится. Тогда невыполненная задача не выглядит забытой - видно,
что она ждет чужого шага, и сдвиг срока перестает быть единственным способом
это показать (rules/tasks-tracking.md: просрочку молча не переносить).

Проставляются обе стороны связи:
  ЗАБЛОКИРОВАНА - в задаче, которая ждет;
  БЛОКИРУЕТ     - в задаче, которой ждут. Заказчику это показывает цену
                  молчания: его пункт держит чужую работу, а не просто висит.

Идемпотентен: старый блок распознается по маркеру и заменяется целиком, а не
дописывается вторым. Убранная из конфига цепочка снимает блок с описания -
иначе доска врала бы про связь, которой больше нет.

Токен (Personal Access Token, https://app.asana.com/0/my-apps) берется из:
  1. --auth <файл> вида {"token": "..."} (права 600);
  2. переменной окружения ASANA_TOKEN;
  3. файла ~/.config/asana/auth.json (дефолт).
В репозиторий и в заметки токен не кладем.

Дефолт - dry-run: печатается, что будет сделано. Реальная запись - с --send.

Цепочки живут в конфиге, а не в коде: досок обычно несколько, и правка связей
не должна быть правкой скрипта. Формат .asana-blockers.json:
  {
    "<ключ доски>": {
      "project": "<gid доски: последнее число в ее URL>",
      "chains": {
        "<задача, которая ждет>": ["<без чего ее не сделать>", "<и еще одна>"]
      }
    }
  }
Имена задач пишутся дословно как на доске: скрипт сверяет их и отказывается
работать, если не нашел или нашел два одинаковых. Молча пропустить связь хуже,
чем упасть - пропущенная связь неотличима от ее отсутствия.

Примеры:
  python3 scripts/asana-blockers.py --board shophack           # предпросмотр
  python3 scripts/asana-blockers.py --board shophack --send    # записать
  python3 scripts/asana-blockers.py                            # все доски конфига
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# Сборка python.org на macOS не видит системные CA - без certifi любой https
# падает с CERTIFICATE_VERIFY_FAILED. Опциональность - как в asana-comments.py.
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

API = "https://app.asana.com/api/1.0"
DEFAULT_AUTH = Path.home() / ".config" / "asana" / "auth.json"
# Конфиг лежит в корне проекта, скрипт - в scripts/ под ним.
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / ".asana-blockers.json"

# Маркеры видны заказчику: Asana показывает описание как есть, HTML-комментарии
# в нем не прячутся, а читаются служебным мусором. Поэтому границы блока -
# человеческий заголовок и линейка.
MARK_START = "СВЯЗИ ЗАДАЧИ"
MARK_END = "- - -"

FIELDS = "name,notes,html_notes,completed,permalink_url"
# Строки, которые пишем мы. Блок под нашим заголовком считается нашим, только
# если состоит из них: иначе человек, начавший описание словами "СВЯЗИ ЗАДАЧИ",
# потерял бы свой текст на первом же прогоне.
# Блок наш, только если состоит из пар "строка связи + голая ссылка" - ровно
# того, что мы генерируем. Более мягкая проверка (любая строка с нужным
# префиксом, любая начинающаяся с http) принимала за свой ручной текст и
# стирала его: человек пишет пояснение прямо к ссылке или начинает абзац
# словом "БЛОКИРУЕТ:".
BLOCKED_LINE = re.compile(r"^ЗАБЛОКИРОВАНА: .+ \((?:открыта|закрыта)\)$")
BLOCKS_LINE = re.compile(r"^БЛОКИРУЕТ: .+$")
BARE_URL = re.compile(r"^https?://\S+$")
# Невидимые символы уравниваем: иначе две визуально одинаковые задачи не дают
# коллизии, и связь молча уходит на ту из них, которая совпала побайтно.
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))
RETRY_CODES = {429, 500, 502, 503, 504}
RETRIES = 4
MAX_WAIT = 300


def retry_delay(headers, attempt: int) -> int:
    """Сколько ждать перед повтором. Retry-After бывает и числом, и HTTP-датой.

    Сырой int() на дате падал ValueError мимо контролируемого выхода - посреди
    записи, оставляя доску наполовину связанной. Верхняя граница нужна, чтобы
    не висеть вечно, но она заметно больше минуты: ранний повтор сам тратит
    квоту и продлевает блокировку.
    """
    raw = ""
    if headers:
        raw = str(headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return max(1, min(MAX_WAIT, int(raw)))
    if raw:
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(1, min(MAX_WAIT, int(when.timestamp() - time.time())))
        except (TypeError, ValueError):
            pass
    return min(MAX_WAIT, 2 ** attempt * 5)


def norm(s: str) -> str:
    """Имя задачи в сравнимом виде: NFC + обрезка краевых пробелов.

    Дословная сверка имен не должна ломаться о композитную форму (готовая "é"
    против "e" + акцент) - в Asana и в терминале они выглядят одинаково, и
    расхождение читалось бы как "задачи нет на доске".
    """
    return unicodedata.normalize("NFC", s).translate(ZERO_WIDTH).strip()


def load_token(auth_path: str | None) -> str:
    if auth_path:
        p = Path(auth_path).expanduser()
        if not p.exists():
            sys.exit(f"Нет файла с токеном: {p}")
        try:
            token = str(json.loads(p.read_text(encoding="utf-8")).get("token") or "").strip()
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"Не читается {p}: {e}")
        if not token:
            sys.exit(f"В {p} нет поля token")
        return token

    token = os.environ.get("ASANA_TOKEN", "").strip()
    if token:
        return token

    if DEFAULT_AUTH.exists():
        try:
            token = str(json.loads(DEFAULT_AUTH.read_text(encoding="utf-8")).get("token") or "").strip()
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"Не читается {DEFAULT_AUTH}: {e}")
        if token:
            return token

    sys.exit(
        "Нет токена Asana. Создай PAT на https://app.asana.com/0/my-apps и положи так:\n"
        f"  mkdir -p {DEFAULT_AUTH.parent} && chmod 700 {DEFAULT_AUTH.parent}\n"
        f'  printf \'{{"token": "ТОКЕН"}}\\n\' > {DEFAULT_AUTH} && chmod 600 {DEFAULT_AUTH}\n'
        "либо укажи --auth <файл>, либо экспортируй ASANA_TOKEN."
    )


def _request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    """Один HTTP-вызов. Возвращает разобранное тело целиком (data + next_page)."""
    data = json.dumps({"data": payload}).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            # 429 на бесплатном тарифе ловится буднично (лимит по домену), а
            # обрыв на середине записи оставляет половину доски со связями и
            # половину без - одностороннюю картину, которая врет сильнее, чем
            # ее отсутствие. Поэтому ждем и повторяем, а не падаем сразу.
            if e.code in RETRY_CODES and attempt < RETRIES - 1:
                wait = retry_delay(e.headers, attempt)
                print(f"Asana API {e.code}, ждем {wait} с и повторяем "
                      f"({attempt + 1}/{RETRIES - 1})", file=sys.stderr)
                time.sleep(wait)
                continue
            sys.exit(f"Asana API {e.code} на {method} {url}: {body}")
        except urllib.error.URLError as e:
            if attempt < RETRIES - 1:
                wait = 2 ** attempt * 5
                print(f"Сеть недоступна ({e.reason}), ждем {wait} с и повторяем "
                      f"({attempt + 1}/{RETRIES - 1})", file=sys.stderr)
                time.sleep(wait)
                continue
            sys.exit(f"Сеть недоступна: {e.reason}")
    raise AssertionError("недостижимо")


def get_all(path: str, token: str) -> list[dict]:
    """GET коллекции с разматыванием пагинации (next_page).

    Без этого доска длиннее страницы отдается усеченной, и задача со второй
    страницы выглядит как несуществующая - прогон падает на валидном конфиге.
    Тот же прием, что в asana-project.py и asana-comments.py.
    """
    sep = "&" if "?" in path else "?"
    url = f"{API}{path}{sep}limit=100"
    items: list[dict] = []
    while url:
        body = _request("GET", url, token)
        items.extend(body.get("data", []))
        url = (body.get("next_page") or {}).get("uri")
    return items


def _no_dupe_keys(pairs):
    """Повторяющийся ключ в JSON Python молча схлопывает: последний побеждает.

    Для конфига это тихая потеря связи (две цепочки под одним именем) или, что
    хуже, подмена gid доски у того же человекочитаемого ключа.
    """
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"повторяющийся ключ: {k!r}")
        out[k] = v
    return out


def load_config(path: str) -> dict:
    """Читает, проверяет и нормализует конфиг ДО первого сетевого вызова.

    Имена в возвращенном конфиге уже приведены norm(): дальше по коду сравнение
    и рендер идут по одной и той же форме. Прежняя версия нормализовала имена
    при сверке, но брала цепочку по сырому ключу - ключ с краевым пробелом
    проходил проверку, прямая сторона связи тихо не проставлялась, а обратная
    проставлялась, и прогон рапортовал успех.
    """
    p = Path(path).expanduser()
    if not p.exists():
        sys.exit(f"Нет конфига цепочек: {p}\nФормат - в шапке скрипта и в скилле asana-blockers.")
    try:
        boards = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=_no_dupe_keys)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        sys.exit(f"Не читается {p}: {e}")
    if not isinstance(boards, dict) or not boards:
        sys.exit(f"В {p} ожидается непустой объект вида {{\"<ключ доски>\": {{...}}}}")

    problems: list[str] = []
    clean: dict[str, dict] = {}
    for label, cfg in boards.items():
        where = f"доска '{label}'"
        if not isinstance(cfg, dict):
            problems.append(f"{where}: ожидается объект с полями project и chains")
            continue
        project = cfg.get("project")
        # gid - число: URL доски или строка с параметрами дала бы не тот запрос
        # (а на нескольких досках - уже после записи в предыдущие).
        if not isinstance(project, str) or not re.fullmatch(r"[0-9]+", project.strip()):
            problems.append(f"{where}: project - gid доски числом строкой "
                            f"(последнее число в ее URL), а не {project!r}")
        chains = cfg.get("chains")
        if not isinstance(chains, dict):
            problems.append(f"{where}: нет поля chains (объект 'задача' -> [зависимости])")
            continue

        norm_chains: dict[str, list[str]] = {}
        for who, needs in chains.items():
            reason = bad_name(who)
            if reason:
                problems.append(f"{where}: имя задачи {who!r} - {reason}")
                continue
            key = norm(who)
            if key in norm_chains:
                problems.append(f"{where}: имя '{key}' встречается дважды "
                                f"(различаются только пробелами или формой записи)")
                continue
            if not isinstance(needs, list) or not needs:
                problems.append(f"{where}: у '{who}' ожидается непустой список зависимостей")
                continue
            seen: list[str] = []
            for n in needs:
                reason = bad_name(n)
                if reason:
                    problems.append(f"{where}, '{who}': зависимость {n!r} - {reason}")
                    continue
                dep = norm(n)
                if dep == key:
                    problems.append(f"{where}: задача '{key}' зависит сама от себя")
                elif dep in seen:
                    problems.append(f"{where}, '{who}': зависимость повторяется: '{dep}'")
                else:
                    seen.append(dep)
            if seen:
                norm_chains[key] = seen
        clean[label] = {"project": (project or "").strip(), "chains": norm_chains}
    if problems:
        sys.exit("Конфиг цепочек неверен:\n  " + "\n  ".join(problems))
    return clean


def bad_name(name) -> str | None:
    """Почему имя задачи не годится для конфига (или None, если годится).

    Перевод строки внутри имени рвет блок изнутри: строка-разделитель, попавшая
    в имя, обрывает наш же блок на следующем прогоне, остаток читается как текст
    человека и дописывается снова - описание растет с каждым прогоном.
    """
    if not isinstance(name, str) or not name.strip():
        return "ожидается непустая строка"
    # splitlines() режет не только по \n и \r, но и по \v, \f, NEL, U+2028,
    # U+2029 - имя с любым из них рвет наш блок изнутри на следующем прогоне
    if len(name.splitlines()) != 1:
        return "перевод строки внутри имени недопустим"
    return None


def _is_ours(inner: str) -> bool:
    """Состоит ли содержимое блока ровно из наших пар "связь + ссылка"."""
    lines = [ln.strip() for ln in inner.split("\n") if ln.strip()]
    if not lines or len(lines) % 2:
        return False  # пустой блок мы не генерируем, непарный - тоже
    for head, url in zip(lines[::2], lines[1::2]):
        if not (BLOCKED_LINE.match(head) or BLOCKS_LINE.match(head)):
            return False
        if not BARE_URL.match(url):
            return False  # ссылка с дописанным пояснением - уже текст человека
    return True


def split_block(notes: str) -> tuple[str | None, str]:
    """Делит описание на наш блок (или None) и остальной текст.

    Блок ищется только в начале описания и только целиком - иначе регулярка
    съела бы кусок осмысленного текста, если в нем встретится такая же линейка.
    Дополнительно проверяется, что блок НАШ (см. _is_ours): описание, начатое
    человеком теми же словами, иначе было бы стерто.
    """
    m = re.match(r"^" + re.escape(MARK_START) + r"\n(.*?)\n" + re.escape(MARK_END) + r"(?:\n|$)\n*",
                 notes, flags=re.S)
    if not m or not _is_ours(m.group(1)):
        return None, notes
    return m.group(0), notes[m.end():]


def render(lines: list[str], rest: str) -> str:
    """Итоговое описание: блок связей сверху, прежний текст под ним.

    Ведущие пустые строки остатка срезаются: иначе каждый прогон добавлял бы к
    ним свои две, и "изменений нет" наступало бы только с третьего раза.
    """
    rest = rest.lstrip("\n")
    if not lines:
        return rest.rstrip("\n")
    block = MARK_START + "\n" + "\n\n".join(lines) + "\n" + MARK_END
    return (block + "\n\n" + rest).rstrip("\n")


def is_rich(task: dict) -> bool:
    """Оформлено ли описание задачи (списки, ссылки, жирный).

    Запись идет в поле notes - плоский текст, и Asana при этом сбрасывает
    html_notes. Оформление чужого описания на этом теряется, поэтому такие
    задачи предпросмотр помечает отдельно.
    """
    html = task.get("html_notes") or ""
    inner = re.sub(r"^<body>|</body>$", "", html.strip(), flags=re.I)
    return bool(re.search(r"<[a-zA-Z/]", inner))


def notes_of(task: dict) -> str:
    """Описание задачи в сравнимом виде: CRLF в LF.

    Asana отдает LF, но вставленный из письма или редактора текст приносит CRLF,
    и наш же блок в нем перестает опознаваться - прогон дописал бы второй.
    """
    return (task.get("notes") or "").replace("\r\n", "\n")


def build_lines(name: str, chains: dict, blocks: dict, by_name: dict) -> list[str]:
    """Строки блока для одной задачи: сперва чего ждет она, потом кого держит."""
    lines = []
    for n in chains.get(name, []):
        dep = by_name[n]
        state = "закрыта" if dep.get("completed") else "открыта"
        lines.append(f"ЗАБЛОКИРОВАНА: {n} ({state})\n{dep['permalink_url']}")
    for n in blocks.get(name, []):
        lines.append(f"БЛОКИРУЕТ: {n}\n{by_name[n]['permalink_url']}")
    return lines


def run_board(label: str, cfg: dict, token: str, send: bool) -> tuple[int, bool]:
    """Возвращает (сколько задач затронуто, была ли ошибка на этой доске)."""
    project, chains = cfg["project"], cfg["chains"]
    tasks = get_all(f"/projects/{project}/tasks?opt_fields={FIELDS}", token)

    mentioned = {n for who, needs in chains.items() for n in [who] + needs}
    by_name: dict[str, dict] = {}
    dupes: set[str] = set()
    for t in tasks:
        name = norm(t.get("name") or "")
        if name in by_name:
            dupes.add(name)
        by_name[name] = t

    unknown = sorted(mentioned - set(by_name))
    collisions = sorted(dupes & mentioned)
    if unknown or collisions:
        # Молча пропустить связь хуже, чем упасть: пропущенная связь на доске
        # неотличима от ее отсутствия (rules/silent-failure.md).
        if unknown:
            print("нет таких задач на доске (переименованы?):", file=sys.stderr)
            for n in unknown:
                print(f"   {n}", file=sys.stderr)
        if collisions:
            print("на доске несколько задач с таким именем - какую связывать, неоднозначно:",
                  file=sys.stderr)
            for n in collisions:
                print(f"   {n}", file=sys.stderr)
        print(f"доска '{label}' пропущена, ничего не записано", file=sys.stderr)
        return 0, True

    # обратная сторона связи: кого держит эта задача
    blocks: dict[str, list[str]] = {}
    for who, needs in chains.items():
        for n in needs:
            blocks.setdefault(n, []).append(who)

    # обход идет по списку задач, а не по by_name: одноименные задачи вне
    # цепочек схлопнулись бы, и на второй из них остался бы висеть старый блок
    plan: list[tuple[str, str, list[str], str, bool]] = []
    for t in tasks:
        name = norm(t.get("name") or "")
        lines = build_lines(name, chains, blocks, by_name)
        notes = notes_of(t)
        block_txt, rest = split_block(notes)
        if not lines and block_txt is None:
            continue  # ни связей, ни нашего блока - задача не наша, не трогаем
        fresh = render(lines, rest)
        if fresh != notes.rstrip("\n"):
            plan.append((t["gid"], name, lines, notes, is_rich(t)))

    print(f"задач с изменениями: {len(plan)}")
    for _gid, name, lines, notes, rich in plan:
        print(f"\n--- {name}")
        if not lines:
            print("    СНЯТЬ БЛОК СВЯЗЕЙ (связи убраны из конфига), остается только текст ниже него:")
            block_txt, _rest = split_block(notes)
            for line in (block_txt or "").splitlines():
                print(f"      | {line}")
        for line in lines:
            print(f"    {line.splitlines()[0]}")
        if rich:
            print("    внимание: описание оформлено (списки/ссылки/жирный) - "
                  "запись в notes сплющит его в плоский текст")

    if not send:
        if plan:
            print("\nэто предпросмотр; чтобы записать - повторить с --send")
        return len(plan), False

    written, skipped = 0, 0
    for gid, name, lines, notes, rich in plan:
        # Перечитываем задачу прямо перед записью: между предпросмотром и
        # записью ее мог поменять человек, а PUT перезаписывает поле целиком -
        # без этого чужая правка молча терялась бы. У Asana нет ни ETag, ни
        # условной записи, поэтому окно сужаем, а не закрываем; все, что
        # разошлось со снимком не по тексту, а по сути (имя, доска, появившееся
        # оформление), - повод не писать вовсе.
        cur = _request("GET", f"{API}/tasks/{gid}?opt_fields=notes,html_notes,name,projects",
                       token).get("data")
        if not isinstance(cur, dict) or not isinstance(cur.get("notes"), str):
            print(f"'{name}': неполный ответ Asana при перечитывании - пропускаем",
                  file=sys.stderr)
            skipped += 1
            continue
        if norm(cur.get("name") or "") != name:
            print(f"'{name}': задачу переименовали после предпросмотра - пропускаем",
                  file=sys.stderr)
            skipped += 1
            continue
        if project not in {(pr or {}).get("gid") for pr in cur.get("projects") or []}:
            print(f"'{name}': задачи больше нет на этой доске - пропускаем", file=sys.stderr)
            skipped += 1
            continue
        if is_rich(cur) and not rich:
            # оформление появилось после предпросмотра, а значит предупреждения
            # про сплющивание человек не видел и согласия на потерю не давал
            print(f"'{name}': описание оформили после предпросмотра - пропускаем, "
                  f"повтори предпросмотр", file=sys.stderr)
            skipped += 1
            continue
        cur_notes = notes_of(cur)
        if cur_notes.rstrip("\n") != notes.rstrip("\n"):
            print(f"описание '{name}' изменилось после предпросмотра - "
                  f"блок накладываем на новую версию", file=sys.stderr)
        _block, rest = split_block(cur_notes)
        fresh = render(lines, rest)
        if fresh == cur_notes.rstrip("\n"):
            continue  # кто-то уже привел описание к нужному виду
        _request("PUT", f"{API}/tasks/{gid}", token, {"notes": fresh})
        written += 1
    print(f"\nзаписано: {written}" + (f", пропущено: {skipped}" if skipped else ""))
    return written, bool(skipped)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--auth", help="файл с токеном (по умолчанию ASANA_TOKEN или "
                                   f"{DEFAULT_AUTH})")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="файл с цепочками по доскам")
    ap.add_argument("--board", help="какую доску обрабатывать (ключ из конфига); "
                                    "без флага - все, что описаны")
    ap.add_argument("--send", action="store_true", help="записать (без флага - предпросмотр)")
    args = ap.parse_args()

    boards = load_config(args.config)
    if args.board:
        if args.board not in boards:
            sys.exit(f"нет доски '{args.board}' в конфиге. Есть: {', '.join(boards)}")
        boards = {args.board: boards[args.board]}
    token = load_token(args.auth)

    failed: list[str] = []
    for label, cfg in boards.items():
        print(f"\n##### доска: {label}")
        _touched, err = run_board(label, cfg, token, args.send)
        if err:
            failed.append(label)
    if failed:
        # Ненулевой код нужен именно тут: в cron и в цепочке команд пропущенная
        # доска иначе неотличима от чистого прогона.
        print(f"\nдоски с ошибками: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
