#!/usr/bin/env python3
"""
Расчет дельт между текущим и предыдущим snapshot задач Redmine.

Сравнивает <tasks_root>/_redmine-snapshot.json и _redmine-snapshot.prev.json,
выводит markdown-блок "Дельты с прошлого сбора" для вставки в план дейлика / статус.

Интервал - от предыдущего успешного сбора до текущего, а не календарные сутки.
При нескольких сборах за день сравниваются два последних успешных сбора.

Различает:
- закрытые задачи (закрыты по статусу или отсутствуют в старом формате снимка);
- задачи, ушедшие к исполнителю вне users конфига;
- задачи с неизвестным статусом после неудачного дозапроса;
- задачи исполнителя, удаленного из проектного конфига;
- новые задачи (появились у кого-то, не было нигде раньше);
- смена статуса (id есть в обоих snapshot, status изменился);
- смена исполнителя (id есть, assigned_to_id поменялся).

Путь к снапшотам берется из tasks_root проектного конфига
.redmine-snapshot.json (тот же, что у redmine-snapshot.py). Ссылки на issue
строятся по redmine_url, записанному в сам snapshot - секретный auth.json
этому скрипту не нужен.

Коды возврата: 0 - дельты рассчитаны, 2 - снимка нет, 3 - нет базы сравнения.

Запуск:
    python3 scripts/redmine-deltas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_CONFIG_PATH = PROJECT_ROOT / ".redmine-snapshot.json"


def load_project_config() -> dict:
    if not PROJECT_CONFIG_PATH.exists():
        sys.stderr.write(
            f"Нет проектного конфига {PROJECT_CONFIG_PATH}.\n"
            "Сначала собери snapshot - см. redmine-snapshot.py.\n"
        )
        sys.exit(2)
    with PROJECT_CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("tasks_root", "tasks")
    return cfg


def load(p: Path) -> dict:
    if not p.exists():
        return {"users": {}}
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def index_by_issue_id(snapshot: dict) -> dict[int, tuple[str, dict]]:
    """Возвращает {issue_id: (user_id, issue_dict)}."""
    out: dict[int, tuple[str, dict]] = {}
    for uid, payload in snapshot.get("users", {}).items():
        for issue in payload.get("issues", []):
            assigned = issue.get("assigned_to_id")
            out[issue["id"]] = (str(uid if assigned is None else assigned), issue)
    return out


def user_name(snapshot: dict, uid: str) -> str:
    return snapshot.get("users", {}).get(uid, {}).get("name", f"user {uid}")


def link(redmine_url: str, iid: int) -> str:
    return f"[#{iid}]({redmine_url}/issues/{iid})"


def format_issue_short(issue: dict) -> str:
    v = issue.get("fixed_version") or "-"
    c = issue.get("category") or "-"
    subj = issue.get("subject", "").strip()
    return f"({v}/{c}) {subj}"


def resolve_root(raw: str) -> Path:
    """Зеркально redmine-snapshot.py: абсолютный tasks_root берется как есть,
    "~" раскрывается. Расхождение с записывающим скриптом означало бы, что
    дельты читают другой файл и вечно показывают устаревшее."""
    p = Path(str(raw)).expanduser()
    return p.resolve() if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def main() -> int:
    cfg = load_project_config()
    tasks_root = resolve_root(cfg["tasks_root"])
    snapshot_path = tasks_root / "_redmine-snapshot.json"
    prev_path = tasks_root / "_redmine-snapshot.prev.json"

    if not snapshot_path.exists():
        print(f"снимка нет: {snapshot_path}", file=sys.stderr)
        return 2
    if not prev_path.exists():
        print("база для сравнения отсутствует, дельты не посчитаны", file=sys.stderr)
        return 3

    cur = load(snapshot_path)
    prev = load(prev_path)
    redmine_url = (cur.get("redmine_url") or prev.get("redmine_url") or "").rstrip("/")

    cur_idx = index_by_issue_id(cur)
    prev_idx = index_by_issue_id(prev)

    cur_ids = set(cur_idx)
    prev_ids = set(prev_idx)

    watched = {str(uid) for uid in cfg["users"]}
    removed_issues = {
        issue["id"]: user.get("name", f"user {uid}")
        for uid, user in prev.get("users", {}).items() if uid not in watched
        for issue in user.get("issues", [])
    }
    access_lost = set(cur.get("access_lost", [])) & prev_ids
    departed = {iid for iid in prev_ids - cur_ids - access_lost if iid in removed_issues}
    closed = sorted(prev_ids - cur_ids - departed - access_lost)
    appeared = sorted(cur_ids - prev_ids)
    common = cur_ids & prev_ids
    unknown = {iid for iid, (_, issue) in cur_idx.items()
               if issue.get("status_unknown")}
    appeared = sorted(set(appeared) - set(unknown))
    common -= unknown

    left = []
    for iid in sorted(common):
        cur_uid, cur_issue = cur_idx[iid]
        prev_uid, prev_issue = prev_idx[iid]
        if cur_issue.get("is_closed") and not prev_issue.get("is_closed"):
            closed.append(iid)
        elif iid in removed_issues and cur_uid not in watched:
            departed.add(iid)
        elif not cur_issue.get("is_closed") and cur_uid not in watched and prev_uid in watched:
            left.append(iid)
    common -= set(closed) | set(left) | departed

    status_changes = []
    assignee_changes = []
    for iid in sorted(common):
        cur_uid, cur_issue = cur_idx[iid]
        prev_uid, prev_issue = prev_idx[iid]
        if cur_issue["status"] != prev_issue["status"]:
            status_changes.append((iid, prev_uid, cur_uid, prev_issue, cur_issue))
        if cur_uid != prev_uid:
            assignee_changes.append((iid, prev_uid, cur_uid, cur_issue))

    print(f"### Дельты с прошлого сбора (snapshot {cur.get('generated_at', '?')})\n")
    print(f"_prev: {prev.get('generated_at', 'нет')}_\n")

    if access_lost:
        print(f"**Выбыли из доступа ({len(access_lost)}):**\n")
        for iid in sorted(access_lost):
            print(f"- {link(redmine_url, iid)} {format_issue_short(prev_idx[iid][1])}")
        print()

    if closed:
        print(f"**Закрыты / ушли из открытых ({len(closed)}):**\n")
        for iid in sorted(closed):
            uid, issue = prev_idx[iid]
            print(f"- {link(redmine_url, iid)} {format_issue_short(issue)} - был у {user_name(prev, uid)}, статус был {issue['status']}")
        print()

    for reason in ("дозапрос не удался", "нет признака закрытия"):
        group = sorted(iid for iid in unknown
                       if cur_idx[iid][1].get("status_unknown_reason", "нет признака закрытия") == reason)
        if not group:
            continue
        print(f"**Статус неизвестен ({reason}) ({len(group)}):**\n")
        for iid in group:
            _, issue = cur_idx[iid]
            uid, _ = prev_idx.get(iid, cur_idx[iid])
            print(f"- {link(redmine_url, iid)} {format_issue_short(issue)} - был у {user_name(prev, uid)}")
        print()

    if departed:
        print(f"**Выбыли вместе с исполнителем ({len(departed)}):**\n")
        for iid in sorted(departed):
            issue = cur_idx[iid][1] if iid in cur_idx else prev_idx[iid][1]
            print(f"- {link(redmine_url, iid)} {format_issue_short(issue)} - "
                  f"выбыли вместе с исполнителем {removed_issues[iid]}")
        print()

    if left:
        print(f"**Ушла из наблюдения ({len(left)}):**\n")
        for iid in left:
            uid, issue = cur_idx[iid]
            print(f"- {link(redmine_url, iid)} {format_issue_short(issue)} - у {user_name(cur, uid)}, {issue['status']}")
        print()

    if appeared:
        print(f"**Новые задачи ({len(appeared)}):**\n")
        for iid in appeared:
            uid, issue = cur_idx[iid]
            print(f"- {link(redmine_url, iid)} {format_issue_short(issue)} - у {user_name(cur, uid)}, {issue['status']}")
        print()

    if status_changes:
        print(f"**Смена статуса ({len(status_changes)}):**\n")
        for iid, _, cur_uid, prev_issue, cur_issue in status_changes:
            print(
                f"- {link(redmine_url, iid)} {format_issue_short(cur_issue)} - "
                f"{prev_issue['status']} -> **{cur_issue['status']}** "
                f"(у {user_name(cur, cur_uid)})"
            )
        print()

    if assignee_changes:
        print(f"**Смена исполнителя ({len(assignee_changes)}):**\n")
        for iid, prev_uid, cur_uid, issue in assignee_changes:
            print(
                f"- {link(redmine_url, iid)} {format_issue_short(issue)} - "
                f"{user_name(prev, prev_uid)} -> **{user_name(cur, cur_uid)}**"
            )
        print()

    if not (access_lost or closed or unknown or departed or left or appeared or status_changes or assignee_changes):
        print("_Изменений нет._\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
