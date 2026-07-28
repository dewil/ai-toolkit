#!/usr/bin/env python3
"""Разворачивание и наполнение доски Asana из JSON-плана.

Зачем: официальный Asana MCP заводит задачи по одной и с интерактивным
подтверждением каждой - для разворачивания доски из готового плана (roadmap,
итоги встречи: задачи по секциям, у каждой исполнитель и срок) это десятки
шагов. Тут: один JSON -> проект + секции + задачи одной командой. Единичные
операции остаются за MCP (граница - в скилле asana-project).

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
               существующей задачи подтягивается к плану

Примеры:
  python3 scripts/asana-project.py workspaces --auth ~/.config/asana/auth-work.json
  python3 scripts/asana-project.py create --plan план.json          # предпросмотр
  python3 scripts/asana-project.py create --plan план.json --send
  python3 scripts/asana-project.py tasks --plan досыпка.json --send

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
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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
    existing = {str(s.get("name") or "").strip(): s["gid"]
                for s in get_all(f"/projects/{pgid}/sections", token)}
    known = {str(t.get("name") or "").strip(): t
             for t in get_all(f"/projects/{pgid}/tasks?opt_fields=name,due_on", token)}

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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
