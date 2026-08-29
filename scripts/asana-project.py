#!/usr/bin/env python3
"""Разворачивание и наполнение доски Asana из JSON-плана.

Зачем: поштучный канал (ручной REST, а раньше Asana MCP) заводит задачи по
одной и с подтверждением каждой - для разворачивания доски из готового плана
(roadmap, итоги встречи: задачи по секциям, у каждой исполнитель и срок) это
десятки шагов. Тут: один JSON -> проект + секции + задачи одной командой.
Единичные операции - прямой REST тем же токеном (граница - в скилле
asana-project; Asana MCP снят 17.08.2026 и не используется).

Токен (Personal Access Token, https://app.asana.com/0/my-apps) берется из:
  1. --auth <файл> вида {"token": "..."} (права 600);
  2. переменной окружения ASANA_TOKEN;
  3. файла ~/.config/asana/auth.json (дефолт).
Несколько аккаунтов - несколько файлов, выбираются флагом --auth.
В репозиторий и в заметки токен не кладем.

Дефолт - dry-run: печатается, что будет сделано. Реальная запись - с --send.

Подкоманды:
  workspaces - показать аккаунт и его воркспейсы (узнать workspace gid)
  create     - создать проект с секциями и задачами с нуля. НЕ идемпотентна:
               повторный прогон создаст второй проект - досыпка через tasks
  tasks      - досыпать задачи в существующий проект: секции переиспользуются
               по имени, задачи узнаются по имени (дублей нет), срок
               существующей задачи подтягивается к плану; поле related у задачи
               пишет в ее описание блок "откуда выросла эта работа"
  move       - перенести существующие задачи в секцию
  complete   - пакетно поставить "выполнено" (НЕОБРАТИМО)
  summary    - часы из имен задач секции построчно и итогом (только чтение)

Вместе они закрывают отчетный период: tasks -> move -> summary -> complete.

Примеры:
  python3 scripts/asana-project.py workspaces --auth ~/.config/asana/auth-work.json
  python3 scripts/asana-project.py create --plan план.json          # предпросмотр
  python3 scripts/asana-project.py create --plan план.json --send
  python3 scripts/asana-project.py tasks --plan досыпка.json --send
  python3 scripts/asana-project.py move --plan перенос.json --send
  python3 scripts/asana-project.py summary --project 12345 --section "Отчет ..."
  python3 scripts/asana-project.py complete --plan закрытие.json --send

Формат плана для create:
  {
    "workspace": "33179052486267",
    "name": "ShopHack",
    "notes": "описание проекта",
    "sections": [
      {"name": "Этап 0", "tasks": [
        {"name": "Задача", "notes": "детали",
         "assignee": "user@example.com", "due_on": "2026-08-01"}
      ]}
    ]
  }

Формат плана для tasks - то же, но вместо workspace/name/notes один ключ
"project" с gid существующего проекта (последнее число в URL доски).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        sys.exit(f"Asana API {e.code} на {method} {url}: {body}")
    except urllib.error.URLError as e:
        sys.exit(f"Сеть недоступна: {e.reason}")


def call(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    return _request(method, f"{API}{path}", token, payload).get("data", {})


def get_all(path: str, token: str) -> list[dict]:
    """GET коллекции с разматыванием пагинации (next_page).

    Без явного limit Asana может отдать усеченный список: на большой доске
    дедуп по имени тогда молча не увидит часть задач, и повторный прогон
    наплодит дубли. Тот же прием, что у fetch_stories в asana-comments.py.
    """
    sep = "&" if "?" in path else "?"
    url = f"{API}{path}{sep}limit=100"
    items: list[dict] = []
    while url:
        body = _request("GET", url, token)
        items.extend(body.get("data", []))
        url = (body.get("next_page") or {}).get("uri")
    return items


def require(plan: dict, keys: tuple[str, ...], where: str) -> None:
    missing = [k for k in keys if not plan.get(k)]
    if missing:
        sys.exit(f"В плане {where} нет обязательных полей: {', '.join(missing)}")


DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_plan(sections: list, where: str) -> None:
    """Отказ ДО первого сетевого вызова - иначе падение на середине заливки
    оставляет общую доску в промежуточном состоянии.

    Повтор имени задачи внутри плана - отказ, а не тихий дедуп: send свел бы
    две разных строки плана в одну задачу (вторая превратилась бы в синк
    срока первой), и dry-run обещал бы секцию/исполнителя, которых не будет.
    """
    seen: dict[str, str] = {}
    problems: list[str] = []
    for s in sections:
        raw_sname = s.get("name")
        # тип проверяем до strip: числовое имя проходило str()-проверку,
        # а send потом падал на .strip() у int - посреди заливки и мимо recovery
        if not isinstance(raw_sname, str) or not raw_sname.strip():
            problems.append(f"секция без имени-строки: {raw_sname!r}")
            continue
        sname = raw_sname.strip()
        for t in s.get("tasks", []):
            raw_name = t.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                problems.append(f"задача без имени-строки в секции '{sname}': {raw_name!r}")
                continue
            name = raw_name.strip()
            if name in seen:
                problems.append(
                    f"имя '{name}' повторяется (секции '{seen[name]}' и '{sname}') - "
                    "какую из строк исполнять, неоднозначно"
                )
            seen[name] = sname
            rel = t.get("related")
            if rel is not None:
                if not isinstance(rel, list):
                    problems.append(f"'{name}': related - ожидается список имен или gid")
                else:
                    for r in rel:
                        if not isinstance(r, str) or not r.strip():
                            problems.append(f"'{name}': в related непустая строка ожидается, а не {r!r}")
            due = t.get("due_on")
            if due:
                ok = isinstance(due, str) and DUE_RE.match(due)
                if ok:
                    try:
                        datetime.strptime(due, "%Y-%m-%d")
                    except ValueError:
                        ok = False
                if not ok:
                    problems.append(f"'{name}': due_on {due!r} - не существующая дата YYYY-MM-DD")
    if problems:
        sys.exit(f"План {where} не будет исполнен - исправь и повтори:\n  - "
                 + "\n  - ".join(problems))


def load_plan(path: str) -> dict:
    fp = Path(path)
    if not fp.exists():
        sys.exit(f"Нет файла плана: {fp}")
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"План {fp} - не валидный JSON: {e}")


def task_line(t: dict) -> str:
    due = f"  (до {t['due_on']})" if t.get("due_on") else ""
    return f"{t['name']}  ->  {t.get('assignee', 'без исполнителя')}{due}"


def task_payload(t: dict, pgid: str, sgid: str) -> dict:
    payload = {
        "name": t["name"].strip(),
        "notes": t.get("notes", ""),
        "projects": [pgid],
        "memberships": [{"project": pgid, "section": sgid}],
    }
    if t.get("assignee"):
        payload["assignee"] = t["assignee"]
    if t.get("due_on"):
        payload["due_on"] = t["due_on"]
    return payload


def blocks_mod():
    """Каркас технического блока из asana-blockers.py - соседа по scripts/.

    Подгружается лениво и только той командой, которой блок нужен: проект мог
    взять из канона один скрипт из двух, и падать на импорте у create/tasks
    из-за отсутствующего соседа неправильно. Копию каркаса тут не держим -
    две копии разошлись бы на первом же фиксе распознавания блока, а цена
    расхождения - стертое описание задачи.
    """
    path = Path(__file__).resolve().with_name("asana-blockers.py")
    if not path.exists():
        sys.exit(
            f"нет {path.name} рядом с {Path(__file__).name} - он нужен для блока "
            "связей в описании (поле related).\n"
            "Оба скрипта живут в scripts/ проекта; возьми недостающий из канона "
            "(scripts/asana-blockers.py, см. skills/asana-blockers/SKILL.md)."
        )
    spec = importlib.util.spec_from_file_location("_asana_blockers", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GID_ONLY = re.compile(r"^\d+$")

# Имя задачи и часы в нем: у заказчика конвенция своя, поэтому это только
# дефолт-пример, переопределяемый флагом --hours-re. Берется первое число перед
# "ч"/"h"; \b не дает поймать "часа" и "hotfix".
# Ретроспективная проверка слева обязательна: без нее "(20-25ч)" давало 25,
# "1,5,5ч" - 5,5, а "1e3ч" - 3. Все три - мусор, который молча уходил в сумму
# счета, потому что число распозналось и предупреждения не было.
HOURS_RE = r"(?<![\w.,\-\u2010-\u2015\u2212])(\d+(?:[.,]\d+)?)\s*(?:ч|h)\b"
# Верхняя граница здравого смысла: больше - это не часы, а опечатка или
# идентификатор. Без нее длинная строка цифр давала inf и отравляла итог.
MAX_HOURS = Decimal("100000")
# Шаг округления часов. Все значения приводятся к нему при разборе, поэтому
# сумма показанных строк всегда равна показанному итогу.
HOURS_STEP = Decimal("0.01")


def fmt_hours(v: Decimal) -> str:
    """Часы по-русски: 23,5 и 20 (а не 23.5 и 20.0)."""
    q = v.quantize(HOURS_STEP, rounding=ROUND_HALF_UP)
    return format(q.normalize(), "f").replace(".", ",")


def parse_hours(name: str, rx) -> Decimal | None:
    """Часы из имени задачи или None, если не распознано.

    Decimal, а не float: сумма уходит в денежный документ, а двоичная дробь
    там дает расхождение с ручной проверкой на ровном месте (0,005 x 3 в
    float печатается как 0,01, а не 0,015).
    """
    m = rx.search(name)
    if not m:
        return None
    raw = m.group(1) if m.lastindex else None
    if not isinstance(raw, str):
        # пользовательская регулярка с необязательной группой роняла разбор
        return None
    try:
        v = Decimal(raw.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not v.is_finite() or v < 0 or v > MAX_HOURS:
        return None
    # Квантуем СРАЗУ, а не только при печати: иначе видимые строки и итог
    # расходятся (три строки по 0,01 при итоге 0,02), и сверка глазами в
    # документе, по которому выставляется счет, перестает сходиться
    q = v.quantize(HOURS_STEP, rounding=ROUND_HALF_UP)
    if q == 0 and v != 0:
        # 0,004ч распозналось бы как честный ноль: двести таких задач дали бы
        # в счете 0 вместо часа, и предупреждение бы не сработало
        return None
    return q


def compile_hours_re(pattern: str):
    try:
        rx = re.compile(pattern)
    except re.error as e:
        sys.exit(f"--hours-re не компилируется: {e}")
    if rx.groups < 1:
        sys.exit("--hours-re должна иметь группу захвата с числом часов, "
                 f"например {HOURS_RE!r}")
    return rx


def board_index(tasks: list[dict]) -> tuple[dict[str, dict], set[str], dict[str, dict]]:
    """Индексы доски: по имени, множество неоднозначных имен, по gid."""
    by_name: dict[str, dict] = {}
    dupes: set[str] = set()
    by_gid: dict[str, dict] = {}
    for t in tasks:
        name = str(t.get("name") or "").strip()
        if name:
            if name in by_name:
                dupes.add(name)
            by_name[name] = t
        if t.get("gid"):
            by_gid[str(t["gid"])] = t
    # by_gid строится по СЫРОМУ списку, а не по by_name: иначе задача, чье имя
    # совпало с чужим и оказалась не последней, недостижима и по своему gid -
    # то есть совет "укажи gid нужной" не работал ровно там, где он нужен
    return by_name, dupes, by_gid


def resolve_ref(raw, by_name: dict, dupes: set, by_gid: dict) -> tuple[dict | None, str | None]:
    """Одна ссылка (имя или gid) -> (задача, причина отказа).

    Единственное место, где решается, что такое строка из цифр. Раньше таких
    мест было три (move/complete, проверка related, применение related), и они
    расходились: одна и та же ссылка в одном месте резолвилась, в другом
    отвергалась, в третьем указывала на чужую задачу.
    """
    if not isinstance(raw, str):
        return None, f"{raw!r}: ожидается строка - имя задачи или gid"
    ref = raw.strip()
    if not ref:
        return None, "пустая ссылка на задачу"
    named = None if ref in dupes else by_name.get(ref)
    if GID_ONLY.match(ref):
        found = by_gid.get(ref)
        if found and ref in dupes:
            # строка - и gid одной задачи, и имя сразу нескольких других
            return None, (f"'{ref}': это и gid задачи '{found.get('name')}', и имя, "
                          "которое носят несколько задач - неоднозначно")
        if found and named and str(named.get("gid")) != ref:
            # Задача, НАЗВАННАЯ числом, и чужая задача с таким gid - две разных
            # задачи. Молча предпочесть одну значит перенести или закрыть не то
            return None, (f"'{ref}': это и gid задачи '{found.get('name')}', и имя "
                          f"задачи с gid {named.get('gid')} - какую из них, "
                          "неоднозначно; переименуй одну из них")
        if found:
            return found, None
        if ref in dupes:
            return None, f"'{ref}': на доске несколько задач с таким именем"
        if named:
            return named, None
        return None, f"gid {ref}: такой задачи на этой доске нет"
    if ref in dupes:
        return None, (f"'{ref}': на доске несколько задач с таким именем - "
                      "укажи gid нужной")
    if named:
        return named, None
    return None, f"'{ref}': нет такой задачи на доске"


def resolve_refs(refs: list[str], tasks: list[dict], where: str) -> list[dict]:
    """Ссылки на задачи (имя или gid) -> задачи доски. Отказ до записи.

    Неоднозначность и промах не угадываются: выбор наугад тут означает перенос
    или закрытие не той задачи на общей доске. Ошибки собираются по всем
    ссылкам сразу - иначе исправление идет по одной за прогон.
    """
    by_name, dupes, by_gid = board_index(tasks)
    out: list[dict] = []
    problems: list[str] = []
    for raw in refs:
        found, why = resolve_ref(raw, by_name, dupes, by_gid)
        if found is None:
            ref = raw.strip() if isinstance(raw, str) else None
            if ref and why and why.endswith("нет такой задачи на доске"):
                near = [n for n in by_name if ref.lower() in n.lower()][:5]
                if near:
                    why += "; похожие: " + ", ".join(f"'{n}'" for n in near)
            if ref and ref in dupes:
                # перечисляем кандидатов: без них совет "укажи gid" не исполним
                gids = ", ".join(sorted(str(t.get("gid")) for t in tasks
                                        if str(t.get("name") or "").strip() == ref))
                why += f" (gid {gids})"
            problems.append(why or "не резолвится")
            continue
        out.append(found)
    if problems:
        sys.exit(f"{where}: не будет исполнено - исправь и повтори:\n  - "
                 + "\n  - ".join(problems))
    return out


def sections_index(pgid: str, token: str) -> dict[str, list[str]]:
    """Имя секции -> список gid. Список, а не gid: Asana разрешает одноименные
    секции, и словарь "имя -> gid" молча оставлял бы последнюю из ответа."""
    idx: dict[str, list[str]] = {}
    for sec in get_all(f"/projects/{pgid}/sections", token):
        idx.setdefault(str(sec.get("name") or "").strip(), []).append(sec["gid"])
    return idx


def need_section(pgid: str, name: str, token: str) -> str:
    """gid секции по имени. Секцию тут НЕ создаем: промах в имени вероятнее
    намерения завести секцию именно этой командой, а секции заводит tasks."""
    idx = sections_index(pgid, token)
    gids = idx.get(name.strip()) or []
    if len(gids) > 1:
        sys.exit(f"на доске {pgid} несколько секций с именем '{name}' "
                 f"(gid {', '.join(gids)}) - в какую писать, неоднозначно. "
                 "Переименуй одну из них и повтори.")
    if not gids:
        have = ", ".join(f"'{n}'" for n in sorted(idx)) or "их вообще нет"
        sys.exit(f"на доске {pgid} нет секции '{name}'. Есть: {have}.\n"
                 "Секции заводит подкоманда tasks - заведи ее там и повтори.")
    return gids[0]


# Секция и статус нужны и move (откуда переносим), и complete (что уже закрыто).
BOARD_FIELDS = ("name,completed,memberships.project.gid,"
                "memberships.section.name,memberships.section.gid")


def section_of(task: dict, pgid: str) -> tuple[str | None, str | None]:
    """(gid, имя) секции задачи в ЭТОМ проекте. Задача бывает в нескольких."""
    for m in task.get("memberships") or []:
        if str((m.get("project") or {}).get("gid") or "") == pgid:
            sec = m.get("section") or {}
            if sec.get("gid"):
                return str(sec["gid"]), str(sec.get("name") or "")
    return None, None


def dedupe(tasks: list[dict]) -> list[dict]:
    """Одна и та же задача, названная в плане дважды, обрабатывается один раз."""
    seen: set[str] = set()
    out = []
    for t in tasks:
        gid = str(t.get("gid"))
        if gid not in seen:
            seen.add(gid)
            out.append(t)
    return out


def cmd_move(args) -> int:
    """Перенести существующие задачи в секцию.

    Отдельно от tasks: tasks заводит новые задачи, а тут задачи уже есть и
    меняется только их место на доске. Идемпотентно - перенос в ту же секцию
    это "без изменений", а не ошибка.
    """
    token = load_token(args.auth)
    plan = load_plan(args.plan)
    require(plan, ("project", "section", "tasks"), args.plan)
    pgid = str(plan["project"])
    refs = plan["tasks"]
    if not isinstance(refs, list) or not refs:
        sys.exit(f"В плане {args.plan} поле tasks - непустой список имен или gid")
    sgid = need_section(pgid, str(plan["section"]), token)
    board = get_all(f"/projects/{pgid}/tasks?opt_fields={BOARD_FIELDS}", token)
    targets = dedupe(resolve_refs(refs, board, args.plan))

    moves = []
    for t in targets:
        cur_gid, cur_name = section_of(t, pgid)
        moves.append((t, cur_gid == sgid, cur_name or "вне секций"))

    if not args.send:
        print("DRY-RUN (без --send ничего не перенесено)")
        print(f"  проект: {pgid}, секция назначения: {plan['section']}")
        for t, same, cur in moves:
            if same:
                print(f"    - {t['name']}  БЕЗ ИЗМЕНЕНИЙ (уже в этой секции)")
            else:
                print(f"    - {t['name']}: {cur} -> {plan['section']}")
        return 0

    moved = skipped = 0
    try:
        for t, same, cur in moves:
            if same:
                skipped += 1
                continue
            call("POST", f"/sections/{sgid}/addTask", token, {"task": t["gid"]})
            moved += 1
            print(f"    > {t['name']}: {cur} -> {plan['section']}")
    except BaseException:
        # Запись не транзакционна: часть задач уже в новой секции. Повтор
        # безопасен (перенесенные станут "без изменений"), но знать, где
        # оборвалось, надо - молчаливый обрыв неотличим от штатного конца.
        # Счетчик - ПОДТВЕРЖДЕННО перенесенные: исход последнего запроса
        # неизвестен, ответ мог потеряться уже после применения
        todo_n = sum(1 for _t, same, _c in moves if not same)
        sys.stderr.write(
            f"прогон оборван: подтвержденно перенесено {moved} из {todo_n}, "
            "исход последнего запроса неизвестен. Повтор того же плана "
            "безопасен - уже перенесенные будут пропущены как "
            "\"без изменений\"\n")
        raise
    print(f"OK: перенесено {moved}, без изменений {skipped}")
    return 0


def cmd_complete(args) -> int:
    """Пакетно поставить "выполнено".

    Необратимо для этого скрипта: снятие галки он не делает - на общей доске
    массовое переоткрытие задач опаснее, чем разовое снятие галки руками.
    """
    token = load_token(args.auth)
    plan = load_plan(args.plan)
    require(plan, ("project",), args.plan)
    pgid = str(plan["project"])
    by_section = bool(plan.get("section"))
    by_tasks = bool(plan.get("tasks"))
    if by_section == by_tasks:
        sys.exit(f"В плане {args.plan} должно быть ровно одно из двух: "
                 '"section" (закрыть всю секцию) или "tasks" (перечень задач)')

    if by_section:
        sgid = need_section(pgid, str(plan["section"]), token)
        targets = get_all(f"/sections/{sgid}/tasks?opt_fields=name,completed", token)
        where = f"секция '{plan['section']}'"
    else:
        refs = plan["tasks"]
        if not isinstance(refs, list) or not refs:
            sys.exit(f"В плане {args.plan} поле tasks - непустой список имен или gid")
        board = get_all(f"/projects/{pgid}/tasks?opt_fields={BOARD_FIELDS}", token)
        targets = dedupe(resolve_refs(refs, board, args.plan))
        where = f"перечень из {len(targets)} задач"

    todo = [t for t in targets if not t.get("completed")]
    done = len(targets) - len(todo)

    if not args.send:
        print("DRY-RUN (без --send ничего не закрыто)")
        print(f"  проект: {pgid}, {where}")
        print(f"  БУДЕТ ЗАКРЫТО задач: {len(todo)} (уже закрыто и пропускается: {done})")
        for t in todo:
            print(f"    - {t['name']}")
        if todo:
            print("  закрытие необратимо: снятие галки этот скрипт не делает")
        if by_section and todo:
            pinned = json.dumps({"project": pgid, "tasks": [t["gid"] for t in todo]},
                                ensure_ascii=False)
            print("  закрыть этот набор - планом по gid (форма по секции пишет "
                  "только через него):")
            print(f"    {pinned}")
        return 0

    if by_section and not todo:
        print(f"OK: закрывать нечего, все {done} задач секции уже закрыты")
        return 0
    if by_section:
        # Форма по секции набор НЕ фиксирует: --send перечитал бы доску заново
        # и закрыл то, что добавили в секцию после предпросмотра, - на общей
        # доске это чужая задача, закрытая без спроса. Инвариант "dry-run
        # показывает ровно то, что сделает send" тут не выполняется в принципе,
        # поэтому запись идет только по перечню gid из предпросмотра.
        sys.exit("форма {project, section} - только предпросмотр: между ним и "
                 "записью состав секции может измениться, и закроется чужая "
                 "задача.\nПрогони без --send, возьми напечатанный план по gid "
                 "и закрой им.")

    closed = 0
    try:
        for t in todo:
            call("PUT", f"/tasks/{t['gid']}", token, {"completed": True})
            closed += 1
            print(f"    v {t['name']}")
    except BaseException:
        sys.stderr.write(
            f"прогон оборван: подтвержденно закрыто {closed} из {len(todo)}, "
            "исход последнего запроса неизвестен.\n"
            "Повтор того же плана по gid безопасен - закрытые будут пропущены\n")
        raise
    print(f"OK: закрыто {closed}, уже было закрыто {done}")
    return 0


def cmd_summary(args) -> int:
    """Часы по секции построчно и итогом.

    Сумма из имен задач попадает в денежный документ; пока ее складывает
    человек, расхождение доски с приложением находится случайно.
    """
    token = load_token(args.auth)
    pgid = str(args.project)
    sgid = need_section(pgid, args.section, token)
    rx = compile_hours_re(args.hours_re)
    tasks = get_all(f"/sections/{sgid}/tasks?opt_fields=name,completed", token)

    total = Decimal(0)
    unparsed = []
    print(f"секция '{args.section}' (проект {pgid}), задач: {len(tasks)}")
    for t in tasks:
        name = str(t.get("name") or "")
        mark = "x" if t.get("completed") else " "
        hours = parse_hours(name, rx)
        if hours is None:
            unparsed.append(name)
            print(f"  [{mark}]      ? | {name}")
        else:
            total += hours
            print(f"  [{mark}] {fmt_hours(hours):>6} | {name}")
    print(f"  итого: {fmt_hours(total)}")
    if unparsed:
        # Молчаливый пропуск строки означал бы заниженную сумму в счете, и
        # отличить ее от честной было бы нечем (rules/silent-failure.md).
        print(f"  ВНИМАНИЕ: часы не распознаны в {len(unparsed)} задачах - "
              f"в сумму они НЕ вошли. Проверь имена или задай --hours-re")
    return 0


def cmd_workspaces(args) -> int:
    token = load_token(args.auth)
    me = call("GET", "/users/me", token)
    print(f"аккаунт: {me.get('name')} | {me.get('email')}")
    for w in me.get("workspaces", []):
        print(f"  {w['gid']}  {w['name']}")
    return 0


def cmd_create(args) -> int:
    token = load_token(args.auth)
    plan = load_plan(args.plan)
    require(plan, ("workspace", "name"), args.plan)

    workspace = plan["workspace"]
    sections = plan.get("sections", [])
    validate_plan(sections, args.plan)
    total = sum(len(s.get("tasks", [])) for s in sections)

    if not args.send:
        print("DRY-RUN (без --send ничего не создано)")
        print(f"  воркспейс: {workspace}")
        print(f"  проект: {plan['name']}")
        print(f"  секций: {len(sections)}, задач: {total}")
        for s in sections:
            print(f"  [{s['name']}]")
            for t in s.get("tasks", []):
                print(f"    - {task_line(t)}")
        return 0

    try:
        project = call("POST", "/projects", token, {
            "workspace": workspace,
            "name": plan["name"],
            "notes": plan.get("notes", ""),
            "default_view": "board",
        })
    except SystemExit:
        # исход POST неизвестен: проект мог создаться, а ответ - потеряться.
        # Слепой повтор create тогда даст второй проект
        sys.stderr.write(
            f"создание проекта оборвалось, исход неизвестен. Прежде чем повторять "
            f"create - проверь на доске, не появился ли проект '{plan['name']}'; "
            f"появился - доздай задачи через tasks по его gid\n"
        )
        raise
    pgid = project["gid"]
    print(f"проект создан: {plan['name']} (gid {pgid})")
    print(f"  {project.get('permalink_url', '')}")

    created = 0
    try:
        for s in sections:
            section = call("POST", f"/projects/{pgid}/sections", token, {"name": s["name"].strip()})
            sgid = section["gid"]
            print(f"  секция: {s['name']}")
            for t in s.get("tasks", []):
                call("POST", "/tasks", token, task_payload(t, pgid, sgid))
                created += 1
    except SystemExit:
        # проект уже существует - повтор create создал бы второй такой же.
        # Идемпотентная досыпка того же плана - это tasks по gid выше
        sys.stderr.write(
            f"прогон оборван на середине, проект уже создан (gid {pgid}). "
            f'НЕ повторяй create - доздай оставшееся через tasks с планом {{"project": "{pgid}", ...}}\n'
        )
        raise
    print(f"OK: задач создано {created}")
    return 0


# Карта, а не список: "callable или строка" на каждый атрибут пропускала
# зеркальную подмену (merge_sections строкой, ORIGIN_HEAD функцией) и падала
# TypeError уже после создания задач.
REQUIRED_BLOCK_STR = ("ORIGIN_HEAD", "MARK_START")
REQUIRED_BLOCK_FUNC = ("merge_sections", "parse_sections", "block_lines", "split_block",
                       "render", "body_of", "wrap", "mention", "gid_form")


def related_jobs(sections: list) -> list[dict]:
    """Задачи плана, у которых поле related задано (в том числе пустым)."""
    return [t for s_ in sections for t in s_.get("tasks", [])
            if isinstance(t.get("related"), list)]


def related_index(board: list, plan_names: set) -> tuple[dict, set, dict]:
    """Индекс для резолва related: доска ПЛЮС задачи, создаваемые этим прогоном.

    Задачи плана попадают в тот же индекс, а не в отдельную ветку-фолбэк.
    Фолбэк был вторым решателем и отменял отказ резолвера: ссылка "2026",
    отвергнутая как неоднозначная (gid чужой задачи против имени), проходила
    только потому, что "2026" есть в плане, - и связь уходила на чужую задачу.
    """
    by_name, dupes, by_gid = board_index(board)
    for n in plan_names:
        if n and n not in by_name:
            by_name[n] = {"name": n}  # gid появится после цикла создания
    return by_name, dupes, by_gid


def related_targets(t: dict, idx: tuple) -> tuple[list, list[str]]:
    """Ссылки одной задачи -> ([задача], проблемы).

    Один резолвер и для проверки, и для применения: расхождение между ними
    означало бы, что предпросмотр обещает не то, что произойдет, а ссылка,
    принятая проверкой, указывает на другую задачу при записи.
    """
    by_name, dupes, by_gid = idx
    name = t["name"].strip()
    out: list = []
    problems: list[str] = []
    for raw in t["related"]:
        found, why = resolve_ref(raw, by_name, dupes, by_gid)
        if found is None:
            problems.append(f"'{name}': related {why}")
            continue
        if str(found.get("name") or "").strip() == name:
            problems.append(f"'{name}': ссылается сама на себя")
            continue
        if found not in out:
            out.append(found)
    return out, problems


def check_related(sections: list, board: list, token: str) -> object | None:
    """Полный резолв related ДО первой записи. Возвращает модуль блока.

    Отдельно от применения именно ради порядка: применение идет после цикла
    создания задач, и промах, найденный там, оставлял бы доску с уже
    созданными задачами и без связей. Тут же проверяются версия соседнего
    скрипта и формат уже лежащих в описаниях блоков - обе эти ошибки в
    прежней редакции всплывали после половины заливки.
    """
    jobs = related_jobs(sections)
    if not jobs:
        return None
    ab = blocks_mod()
    missing = [a for a in REQUIRED_BLOCK_STR if not isinstance(getattr(ab, a, None), str)]
    missing += [a for a in REQUIRED_BLOCK_FUNC if not callable(getattr(ab, a, None))]
    if missing:
        sys.exit(
            "рядом лежит несовместимый asana-blockers.py: в нем нет или не того "
            f"вида {', '.join(missing)}.\nОбнови его из канона "
            "(scripts/asana-blockers.py) и повтори - поле related без этого не применить.")

    plan_names = {t["name"].strip() for s_ in sections for t in s_.get("tasks", [])}
    idx = related_index(board, plan_names)
    by_name, dupes, _by_gid = idx
    problems: list[str] = []
    owners: list[dict] = []
    for t in jobs:
        name = t["name"].strip()
        if name in dupes:
            problems.append(f"'{name}': на доске несколько задач с таким именем - "
                            "в какую писать блок связей, неоднозначно")
            continue
        _refs, why = related_targets(t, idx)
        problems.extend(why)
        owner = by_name.get(name)
        if owner and owner.get("gid"):
            owners.append(owner)
    if problems:
        sys.exit("related не будет применен - исправь и повтори:\n  - "
                 + "\n  - ".join(problems))

    for owner in owners:
        cur = call("GET", f"/tasks/{owner['gid']}?opt_fields=html_notes", token)
        block_txt, _rest = ab.split_block(ab.body_of(cur))
        if block_txt is not None and not ab.parse_sections(ab.block_lines(block_txt)):
            sys.exit(
                f"'{owner.get('name')}': в описании блок связей СТАРОГО формата - "
                "asana-project не умеет его сливать, а перезапись стерла бы "
                "блокировки.\nПрогони сначала asana-blockers (он перепишет блок в "
                "новый формат) и повтори.")
    return ab


def apply_related(sections: list, board: list, known: dict, token: str,
                  send: bool, ab) -> None:
    """Секция "откуда выросла эта работа" в описаниях задач с полем related.

    Каркас блока (маркеры, распознавание своего, посекционное слияние) берется
    из asana-blockers: блок в описании один на всех, и каждый скрипт
    перезаписывает только свои секции, сохраняя чужие. Задача БЕЗ ключа
    related не трогается вовсе; пустой список - это "связей больше нет", он
    снимает нашу секцию (иначе последнюю связь нельзя было бы убрать).
    """
    jobs = related_jobs(sections)
    if not jobs or ab is None:
        return
    plan_names = {t["name"].strip() for s_ in sections for t in s_.get("tasks", [])}
    idx = related_index(board, plan_names)

    for t in jobs:
        name = t["name"].strip()
        owner = known.get(name)
        refs, _ = related_targets(t, idx)
        # у задачи, создаваемой этим прогоном, gid появляется после цикла и
        # лежит в known - берем оттуда по имени
        resolved = [r if r.get("gid") else known.get(str(r.get("name") or ""), {})
                    for r in refs]
        gids = [str(r.get("gid")) if r.get("gid") else None for r in resolved]
        shown_by_gid = {str(r["gid"]): str(r.get("name") or "") for r in resolved if r.get("gid")}

        if owner is None or not owner.get("gid"):
            if not refs:
                print(f"    ~ {name}  БЛОК СВЯЗЕЙ: без изменений")
            else:
                print(f"    ~ {name}  БЛОК СВЯЗЕЙ: будет добавлен ({len(refs)} связ.) "
                      "- после создания задач")
            continue
        if any(g is None for g in gids):
            print(f"    ~ {name}  БЛОК СВЯЗЕЙ: будет добавлен ({len(refs)} связ.) "
                  "- после создания задач")
            continue

        lines = [ab.ORIGIN_HEAD] + [f"- {ab.mention(g)}" for g in gids] if gids else []
        cur = call("GET", f"/tasks/{owner['gid']}?opt_fields=html_notes", token)
        notes = ab.body_of(cur)
        block_txt, rest = ab.split_block(notes)
        if block_txt is not None and not ab.parse_sections(ab.block_lines(block_txt)):
            # backstop: то же проверено в check_related до записей, но описание
            # могли поменять между проверкой и применением
            sys.exit(
                f"'{name}': в описании блок связей СТАРОГО формата - asana-project "
                "не умеет его сливать, а перезапись стерла бы блокировки.\n"
                "Прогони сначала asana-blockers и повтори.")
        merged = ab.merge_sections(ab.block_lines(block_txt), (ab.ORIGIN_HEAD,), lines)
        fresh = ab.render(merged, rest)
        if ab.gid_form(fresh) == ab.gid_form(notes):
            print(f"    ~ {name}  БЛОК СВЯЗЕЙ: без изменений")
            continue
        shown = ", ".join(shown_by_gid.get(g, f"gid {g}") for g in gids) if gids else "снять секцию"
        if not send:
            print(f"    ~ {name}  БЛОК СВЯЗЕЙ -> {shown}")
            if block_txt is None and ab.MARK_START in notes:
                print("       внимание: в описании уже есть блок связей, который мы "
                      "не считаем своим - новый ляжет сверху, старый останется")
            continue
        call("PUT", f"/tasks/{owner['gid']}", token, {"html_notes": ab.wrap(fresh)})
        print(f"    ~ {name}  блок связей -> {shown}")


def plan_effect(t: dict, old: dict | None) -> tuple[str, dict]:
    """Что сделает send с задачей t при состоянии old: (действие, новое состояние).

    Единственный источник решения и для dry-run, и для send - иначе
    предпросмотр обещает не то, что произойдет. Действия: "create",
    "update" (подтянуть due_on), "skip". Намеренно НЕ делаем: стирание срока,
    когда в плане даты нет, а на доске есть (план может опускать даты
    существующих задач), перенос между секциями, смену исполнителя.
    """
    if old is None:
        return "create", {"due_on": t.get("due_on")}
    if t.get("due_on") and old.get("due_on") != t["due_on"]:
        return "update", {**old, "due_on": t["due_on"]}
    return "skip", old


def cmd_tasks(args) -> int:
    """Досыпать задачи в СУЩЕСТВУЮЩИЙ проект, с исполнителями и по секциям.

    Отдельно от create: доска обычно уже заведена, и в нее подкидывают задачи
    после каждой встречи. Секции переиспользуются по имени, недостающие
    создаются. Исполнитель задается почтой - Asana принимает ее наравне с gid,
    отдельно резолвить пользователей не нужно.
    """
    token = load_token(args.auth)
    plan = load_plan(args.plan)
    require(plan, ("project",), args.plan)
    pgid = str(plan["project"])
    sections = plan.get("sections", [])
    validate_plan(sections, args.plan)
    total = sum(len(s.get("tasks", [])) for s in sections)

    # ключи - стрипнутые имена: Asana может триммить имя при создании, и тогда
    # хвостовой пробел в плане плодил бы дубль на каждом прогоне
    sec_idx = sections_index(pgid, token)
    ambiguous = sorted(n for n, g in sec_idx.items() if len(g) > 1
                       and n in {x["name"].strip() for x in sections})
    if ambiguous:
        sys.exit("на доске несколько секций с одним именем, а план их называет: "
                 + ", ".join(f"'{n}'" for n in ambiguous)
                 + ".\nВ какую писать - неоднозначно; переименуй лишние и повтори.")
    existing = {n: g[0] for n, g in sec_idx.items()}
    board = get_all(f"/projects/{pgid}/tasks?opt_fields=name,due_on", token)
    known = {str(t.get("name") or "").strip(): t for t in board}
    # Полный резолв related до первой записи: промах, найденный после цикла
    # создания, оставил бы доску с новыми задачами и без связей
    ab = check_related(sections, board, token)

    if not args.send:
        print("DRY-RUN (без --send ничего не создано)")
        print(f"  проект: {pgid}, секций в плане: {len(sections)}, задач: {total}")
        sim = dict(known)
        sim_existing = set(existing)  # секция может повторяться - вторая строка уже "есть"
        for s in sections:
            sname = s["name"].strip()
            mark = "есть" if sname in sim_existing else "будет создана"
            sim_existing.add(sname)
            print(f"  [{s['name']}] ({mark})")
            for t in s.get("tasks", []):
                name = t["name"].strip()
                old = sim.get(name)
                action, state = plan_effect(t, old)
                if action == "update":
                    # печатаем только то, что реально произойдет: у существующей
                    # задачи подтянется срок, секция/исполнитель/notes не тронутся
                    print(f"    - {name}  СРОК: {old.get('due_on') or 'нет'} -> "
                          f"{t['due_on']} (остальное из плана не применяется)")
                elif action == "skip":
                    print(f"    - {name}  ПРОПУСК (уже есть, без изменений)")
                else:
                    print(f"    - {task_line(t)}")
                sim[name] = state
        apply_related(sections, board, sim, token, send=False, ab=ab)
        return 0

    created = skipped = updated = 0
    for s in sections:
        sname = s["name"].strip()
        sgid = existing.get(sname)
        if not sgid:
            sgid = call("POST", f"/projects/{pgid}/sections", token, {"name": sname})["gid"]
            existing[sname] = sgid
            print(f"  секция создана: {s['name']}")
        for t in s.get("tasks", []):
            name = t["name"].strip()
            old = known.get(name)
            action, state = plan_effect(t, old)
            if action == "update":
                call("PUT", f"/tasks/{old['gid']}", token, {"due_on": t["due_on"]})
                updated += 1
                print(f"    ~ {name}  срок -> {t['due_on']}")
            elif action == "skip":
                skipped += 1
            else:
                made = call("POST", "/tasks", token, task_payload(t, pgid, sgid))
                state = {**state, "gid": made.get("gid")}
                created += 1
                print(f"    + {name}  ->  {t.get('assignee', '-')}")
            known[name] = state
    apply_related(sections, board, known, token, send=True, ab=ab)
    print(f"OK: создано {created}, срок обновлен у {updated}, без изменений {skipped}")
    return 0


def main() -> int:
    # --auth живет в подкомандах (parents), НЕ в главном парсере: до Python 3.13
    # дефолт подкоманды затирает уже разобранное значение главного, и
    # "--auth x tasks" молча терял бы файл токена. Одна позиция - после
    # подкоманды - зато без сюрпризов.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--auth", help="файл с токеном {\"token\": \"...\"}; иначе ASANA_TOKEN или ~/.config/asana/auth.json")
    parser = argparse.ArgumentParser(description="Разворачивание и наполнение доски Asana из JSON-плана.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ws = sub.add_parser("workspaces", help="показать аккаунт и его воркспейсы", parents=[common])
    p_ws.set_defaults(func=cmd_workspaces)

    p_cr = sub.add_parser("create", parents=[common],
                          help="создать проект с секциями и задачами (НЕ идемпотентна - досыпка через tasks)")
    p_cr.add_argument("--plan", required=True, help="путь к JSON-плану")
    p_cr.add_argument("--send", action="store_true", help="реально создать (без флага - dry-run)")
    p_cr.set_defaults(func=cmd_create)

    p_tk = sub.add_parser("tasks", parents=[common],
                          help="досыпать задачи в существующий проект (идемпотентно по имени задачи)")
    p_tk.add_argument("--plan", required=True, help="путь к JSON: {project, sections:[{name, tasks:[{name, notes, assignee, due_on}]}]}")
    p_tk.add_argument("--send", action="store_true", help="реально создать (без флага - dry-run)")
    p_tk.set_defaults(func=cmd_tasks)

    p_mv = sub.add_parser("move", parents=[common],
                          help="перенести существующие задачи в секцию (идемпотентно)")
    p_mv.add_argument("--plan", required=True,
                      help='путь к JSON: {project, section, tasks:["имя или gid", ...]}')
    p_mv.add_argument("--send", action="store_true", help="реально перенести (без флага - dry-run)")
    p_mv.set_defaults(func=cmd_move)

    p_cm = sub.add_parser("complete", parents=[common],
                          help="пакетно поставить \"выполнено\" (НЕОБРАТИМО: снятие галки не делает)")
    p_cm.add_argument("--plan", required=True,
                      help='путь к JSON: {project, section} либо {project, tasks:[...]}')
    p_cm.add_argument("--send", action="store_true", help="реально закрыть (без флага - dry-run)")
    p_cm.set_defaults(func=cmd_complete)

    p_sm = sub.add_parser("summary", parents=[common],
                          help="часы по секции построчно и итогом (только чтение)")
    p_sm.add_argument("--project", required=True, help="gid доски")
    p_sm.add_argument("--section", required=True, help="имя секции")
    p_sm.add_argument("--hours-re", default=HOURS_RE, dest="hours_re",
                      help=f"регулярка часов с группой захвата (по умолчанию {HOURS_RE!r})")
    p_sm.set_defaults(func=cmd_summary)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
