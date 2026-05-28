#!/usr/bin/env python3
"""
Дельты по чатам Telegram между текущим и предыдущим snapshot.

Сравнивает Встречи/чаты/<label>/result.json и .prev.json, выводит markdown-блок
"Новое в чатах со вчера" для вставки в план дейлика. По умолчанию учитывает
все сообщения с id больше max(prev). Если .prev.json нет, берет окно за
последние N часов (по умолчанию 24).

Запуск:
    python3 scripts/telegram-deltas.py
    python3 scripts/telegram-deltas.py --hours 48
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_CONFIG_PATH = PROJECT_ROOT / ".telegram-snapshot.json"


def load_project_config() -> dict:
    if not PROJECT_CONFIG_PATH.exists():
        sys.stderr.write(
            f"Нет проектного конфига {PROJECT_CONFIG_PATH}.\n"
            f"Формат - см. ~/.config/telegram-snapshot/README.md.\n"
        )
        sys.exit(2)
    with PROJECT_CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("chats"):
        sys.stderr.write(f"В {PROJECT_CONFIG_PATH} не заполнено поле chats\n")
        sys.exit(2)
    cfg.setdefault("chats_root", "Встречи/чаты")
    return cfg


def load_messages(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return json.load(f).get("messages", [])


def text_to_str(text) -> str:
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for p in text:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(p.get("text", ""))
        return "".join(parts)
    return ""


def parse_dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def filter_new(cur: list[dict], prev: list[dict], hours_fallback: int) -> list[dict]:
    prev_ids = {m["id"] for m in prev if isinstance(m.get("id"), int)}
    if prev_ids:
        return [m for m in cur if m["id"] not in prev_ids and m.get("type") == "message"]
    cutoff = datetime.now() - timedelta(hours=hours_fallback)
    out = []
    for m in cur:
        if m.get("type") != "message":
            continue
        dt = parse_dt(m.get("date", ""))
        if dt and dt >= cutoff:
            out.append(m)
    return out


def format_msg(m: dict) -> str:
    dt = parse_dt(m.get("date", ""))
    when = dt.strftime("%m-%d %H:%M") if dt else m.get("date", "?")
    who = m.get("from") or "?"
    text = text_to_str(m.get("text", "")).strip()
    text = " ".join(text.split())
    if len(text) > 200:
        text = text[:200] + "..."
    reply = " ↩" if m.get("reply_to_message_id") else ""
    if not text and (m.get("file_name") or m.get("photo")):
        text = f"[файл: {m.get('file_name', 'photo')}]"
    return f"- {when} **{who}**{reply}: {text}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24, help="Окно, если нет .prev.json")
    args = p.parse_args()

    project_cfg = load_project_config()
    chats_root = PROJECT_ROOT / project_cfg["chats_root"]
    labels = list(project_cfg["chats"].keys())

    print(f"### Новое в чатах ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n")

    grand_total = 0
    for label in labels:
        chat_dir = chats_root / label
        cur = load_messages(chat_dir / "result.json")
        prev = load_messages(chat_dir / "result.prev.json")

        new = filter_new(cur, prev, args.hours)
        grand_total += len(new)

        suffix = "" if prev else f" (нет .prev, окно {args.hours}ч)"
        print(f"**{label}: {len(new)}**{suffix}\n")
        if not new:
            print("_Без изменений._\n")
            continue
        for m in new:
            print(format_msg(m))
        print()

    if grand_total == 0:
        print("_Изменений нет._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
