#!/usr/bin/env python3
"""
Локальный snapshot открытых задач проекта из Redmine.

Скачивает все открытые задачи команды (список исполнителей - в проектном
конфиге) и сохраняет в <tasks_root>/_redmine-snapshot.json. Перед записью
архивирует предыдущий snapshot как _redmine-snapshot.prev.json - он нужен
для расчета дельт между сборками (см. redmine-deltas.py).

Конфиг разделен на две части:
  - Общие credentials (redmine_url, api_key) - в
    ~/.config/redmine-snapshot/auth.json, один раз на устройство. Не коммитить.
  - Проектные параметры - в .redmine-snapshot.json в корне проекта,
    {tasks_root, project_id, users: {uid: name}}. Не секрет, можно коммитить.

По умолчанию запросы идут через urllib. Если Redmine стоит за корпоративным
CA, который Python не видит (но он есть в системном хранилище / macOS
keychain) - в auth.json выставить "use_curl": true, и запросы пойдут через
curl, который этот CA подхватывает.

Запуск:
    python3 scripts/redmine-snapshot.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = Path.home() / ".config" / "redmine-snapshot"
AUTH_PATH = AUTH_DIR / "auth.json"
PROJECT_CONFIG_PATH = PROJECT_ROOT / ".redmine-snapshot.json"


def load_auth() -> dict:
    if not AUTH_PATH.exists():
        sys.stderr.write(
            f"Нет общего конфига {AUTH_PATH}.\n"
            "Настрой доступ - см. скилл redmine-snapshot.\n"
        )
        sys.exit(2)
    with AUTH_PATH.open(encoding="utf-8") as f:
        auth = json.load(f)
    missing = [k for k in ("redmine_url", "api_key") if not auth.get(k)]
    if missing:
        sys.stderr.write(f"В {AUTH_PATH} не заполнены поля: {missing}\n")
        sys.exit(2)
    auth["redmine_url"] = auth["redmine_url"].rstrip("/")
    auth.setdefault("use_curl", False)
    return auth


def load_project_config() -> dict:
    if not PROJECT_CONFIG_PATH.exists():
        sys.stderr.write(
            f"Нет проектного конфига {PROJECT_CONFIG_PATH}.\n"
            "Формат - см. скилл redmine-snapshot, шаг \"Подключение нового проекта\".\n"
        )
        sys.exit(2)
    with PROJECT_CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("project_id"):
        sys.stderr.write(f"В {PROJECT_CONFIG_PATH} не заполнено поле project_id\n")
        sys.exit(2)
    if not cfg.get("users"):
        sys.stderr.write(f"В {PROJECT_CONFIG_PATH} не заполнено поле users\n")
        sys.exit(2)
    cfg.setdefault("tasks_root", "tasks")
    return cfg


def fetch_json(url: str, api_key: str, use_curl: bool) -> dict:
    if use_curl:
        # Redmine за корпоративным CA: urllib его не видит, curl берет
        # сертификат из системного хранилища / macOS keychain.
        # Ключ - через stdin-конфиг (-K -), не argv: в argv он виден в ps и
        # утекает в строку CalledProcessError при ошибке curl.
        result = subprocess.run(
            [
                "curl", "-sS", "--fail",
                "-A", "Mozilla/5.0 (redmine-snapshot)",
                "-K", "-",
                url,
            ],
            input=f'header = "X-Redmine-API-Key: {api_key}"\n'.encode(),
            check=True,
            capture_output=True,
            timeout=30,
        )
        return json.loads(result.stdout)
    req = urllib.request.Request(
        url,
        headers={
            "X-Redmine-API-Key": api_key,
            "User-Agent": "redmine-snapshot",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_user_issues(auth: dict, project_id, user_id) -> list[dict]:
    """Все открытые задачи исполнителя с пейджингом по 100."""
    issues: list[dict] = []
    offset = 0
    while True:
        params = {
            "assigned_to_id": user_id,
            "status_id": "open",
            "limit": 100,
            "offset": offset,
            "sort": "updated_on:desc",
        }
        url = (
            f"{auth['redmine_url']}/projects/{project_id}/issues.json?"
            + urllib.parse.urlencode(params)
        )
        data = fetch_json(url, auth["api_key"], auth["use_curl"])
        batch = data.get("issues", [])
        issues.extend(batch)
        total = data.get("total_count", len(issues))
        offset += len(batch)
        if not batch or offset >= total:
            break
    return issues


def slim(issue: dict) -> dict:
    return {
        "id": issue["id"],
        "tracker": issue["tracker"]["name"],
        "status": issue["status"]["name"],
        "subject": issue.get("subject", ""),
        "fixed_version": (issue.get("fixed_version") or {}).get("name", ""),
        "category": (issue.get("category") or {}).get("name", ""),
        "parent": (issue.get("parent") or {}).get("id"),
        "priority": issue["priority"]["name"],
        "author_id": issue["author"]["id"],
        "assigned_to_id": (issue.get("assigned_to") or {}).get("id"),
        "updated_on": issue["updated_on"],
        "created_on": issue["created_on"],
    }


def resolve_root(raw: str) -> Path:
    """tasks_root -> абсолютный путь. Абсолютный в конфиге берется как есть:
    снимок задач - техническое зеркало, и его штатное место вне синкаемой
    папки проекта (docs-maintenance.md, "Технические артефакты в синкаемой
    папке"). Относительный по-прежнему считается от корня проекта."""
    p = Path(str(raw)).expanduser()
    root = p.resolve() if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    if root.is_relative_to(PROJECT_ROOT.resolve()):
        sys.stderr.write(
            f"внимание: снимок задач пишется внутрь проекта ({root}) - если папка синкается,\n"
            "он уедет на все устройства; вынести можно абсолютным tasks_root "
            '(docs-maintenance.md, "Технические артефакты в синкаемой папке").\n'
        )
    return root


def main() -> int:
    auth = load_auth()
    cfg = load_project_config()
    project_id = cfg["project_id"]
    tasks_root = resolve_root(cfg["tasks_root"])
    snapshot_path = tasks_root / "_redmine-snapshot.json"
    prev_path = tasks_root / "_redmine-snapshot.prev.json"

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "redmine_url": auth["redmine_url"],
        "project_id": project_id,
        "users": {},
    }

    errors = 0
    for uid, name in cfg["users"].items():
        try:
            issues = fetch_user_issues(auth, project_id, uid)
        except Exception as exc:
            print(f"!! {name} ({uid}): {exc}", file=sys.stderr)
            errors += 1
            continue
        snapshot["users"][str(uid)] = {
            "name": name,
            "total": len(issues),
            "issues": [slim(i) for i in issues],
        }
        print(f"   {name} ({uid}): {len(issues)} задач")

    # Fail-closed: снепшот без части сотрудников выдал бы их задачи за
    # "закрытые" в redmine-deltas (closed = prev_ids - cur_ids). Лучше
    # сохранить последнюю валидную пару, чем записать неполный снепшот.
    if errors:
        print(
            f"\nПРЕРВАНО: {errors} сотрудник(ов) не загрузились - снепшот не "
            f"записан (иначе их задачи попадут в дельты как закрытые). "
            f"Устрани ошибку и повтори.",
            file=sys.stderr,
        )
        sys.exit(1)

    tasks_root.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists():
        shutil.copy2(snapshot_path, prev_path)

    with snapshot_path.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    total = sum(u["total"] for u in snapshot["users"].values())
    print(f"\nOK: {total} задач у {len(snapshot['users'])} сотрудников")
    print(f"    -> {snapshot_path}")
    if prev_path.exists():
        print(f"    prev: {prev_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
