#!/usr/bin/env python3
"""
Локальный snapshot открытых задач проекта из Redmine.

Скачивает все открытые задачи команды (список исполнителей - в проектном
конфиге) и сохраняет в <tasks_root>/_redmine-snapshot.json. Пишет файлы через
временные копии: сперва prev, затем текущий снимок; при ошибке второго шага
восстанавливает прежний prev. _redmine-snapshot.prev.json нужен
для расчета дельт между сборками (см. redmine-deltas.py).
Пропавшие из выборки задачи дозапрашивает по id с текущим статусом
и исполнителем. При сетевой ошибке дозапроса сохраняет задачу с пометкой
"статус неизвестен". Некорректный ответ и ошибка загрузки исполнителя
оставляют прежнюю пару снимков.

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
import os
import tempfile
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
        if not isinstance(data, dict) or "total_count" not in data:
            raise ValueError("неполный ответ Redmine: нет total_count")
        total = data["total_count"]
        batch = data.get("issues")
        if type(total) is not int or total < 0 or not isinstance(batch, list):
            raise ValueError("неполный ответ Redmine: некорректная выборка задач")
        if offset + len(batch) > total or (not batch and offset < total):
            raise ValueError("неполный ответ Redmine: число задач не совпадает с total_count")
        issues.extend(batch)
        offset += len(batch)
        if offset >= total:
            break
    return issues


def slim(issue: dict) -> dict:
    closed = issue["status"].get("is_closed")
    if closed is not None and type(closed) is not bool:
        raise ValueError("некорректный признак status.is_closed")
    return {
        "id": issue["id"],
        "tracker": issue["tracker"]["name"],
        "status": issue["status"]["name"],
        "is_closed": closed,
        "status_unknown": closed is None,
        "status_unknown_reason": (
            "нет признака закрытия" if closed is None else ""
        ),
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

    old_bytes = snapshot_path.read_bytes() if snapshot_path.exists() else None
    old_prev_bytes = prev_path.read_bytes() if prev_path.exists() else None
    old = json.loads(old_bytes) if old_bytes is not None else {"users": {}}

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

    if not errors:
        current_ids = {
            issue["id"] for user in snapshot["users"].values() for issue in user["issues"]
        }
        for uid, user in old.get("users", {}).items():
            for issue in user.get("issues", []):
                if issue["id"] in current_ids:
                    continue
                try:
                    data = fetch_json(
                        f"{auth['redmine_url']}/issues/{issue['id']}.json",
                        auth["api_key"], auth["use_curl"],
                    )
                except (ValueError, TypeError) as exc:
                    print(f"ПРЕРВАНО: некорректный ответ для задачи #{issue['id']}: {exc}",
                          file=sys.stderr)
                    return 1
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    print(f"!! задача #{issue['id']}: {exc}", file=sys.stderr)
                    updated = {**issue, "status": "статус неизвестен",
                               "is_closed": None, "status_unknown": True,
                               "status_unknown_reason": "дозапрос не удался"}
                else:
                    try:
                        updated = slim(data["issue"])
                    except (KeyError, TypeError, ValueError) as exc:
                        print(f"ПРЕРВАНО: некорректный ответ для задачи #{issue['id']}: {exc}",
                              file=sys.stderr)
                        return 1
                bucket = snapshot["users"].setdefault(uid, {
                    "name": user["name"], "total": 0, "issues": [],
                })
                bucket["issues"].append(updated)
                bucket["total"] += 1
                current_ids.add(issue["id"])

    # Fail-closed: снепшот без части сотрудников выдал бы их задачи за
    # "закрытые" в redmine-deltas. Лучше
    # сохранить последнюю валидную пару, чем записать неполный снепшот.
    if errors:
        print(
            f"\nПРЕРВАНО: {errors} запрос(ов) не загрузились - снепшот не "
            f"записан (иначе их задачи попадут в дельты как закрытые). "
            f"Устрани ошибку и повтори.",
            file=sys.stderr,
        )
        sys.exit(1)

    tasks_root.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=tasks_root,
                                         delete=False) as f:
            new_path = Path(f.name)
            staged.append(new_path)
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if old_bytes is not None:
            with tempfile.NamedTemporaryFile(mode="wb", dir=tasks_root, delete=False) as f:
                old_path = Path(f.name)
                staged.append(old_path)
                f.write(old_bytes)
                f.flush()
                os.fsync(f.fileno())
            if old_prev_bytes is not None:
                with tempfile.NamedTemporaryFile(mode="wb", dir=tasks_root, delete=False) as f:
                    rollback_path = Path(f.name)
                    staged.append(rollback_path)
                    f.write(old_prev_bytes)
                    f.flush()
                    os.fsync(f.fileno())
            old_path.replace(prev_path)
        try:
            new_path.replace(snapshot_path)
        except Exception:
            if old_bytes is not None:
                if old_prev_bytes is not None:
                    rollback_path.replace(prev_path)
                else:
                    prev_path.unlink(missing_ok=True)
            raise
    finally:
        for path in staged:
            path.unlink(missing_ok=True)

    total = sum(u["total"] for u in snapshot["users"].values())
    print(f"\nOK: {total} задач у {len(snapshot['users'])} сотрудников")
    print(f"    -> {snapshot_path}")
    if prev_path.exists():
        print(f"    prev: {prev_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
