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


def chat_entry(value) -> dict:
    """Нормализует значение из chats к {"id": int, "topic_id": int|None}.

    Короткая форма "label": <id> и расширенная "label": {"id", "topic_id"}.
    topic_id (если задан) - корень форумной темы; дельты по этому чату
    отбирают только сообщения этой темы (топик бота и т.п.).
    """
    if isinstance(value, dict):
        if "id" not in value:
            raise ValueError("в расширенной записи чата нет поля id")
        topic = value.get("topic_id")
        # int обязателен: topic_id сравнивается с числовым полем сообщения,
        # и строковый "42" из конфига молча не совпал бы ни с чем
        return {
            "id": int(value["id"]),
            "topic_id": int(topic) if topic is not None else None,
            "dest": str(value["dest"]) if value.get("dest") else None,
        }
    return {"id": int(value), "topic_id": None, "dest": None}


def resolve_dest(dest: str) -> Path:
    """dest из конфига -> абсолютный путь, строго внутри проекта (зеркально
    telegram-snapshot.py: тот же конфиг обязан читаться из той же папки)."""
    p = Path(dest)
    if p.is_absolute():
        sys.exit(f"dest {dest!r}: абсолютный путь не допускается - укажите путь от корня проекта")
    resolved = (PROJECT_ROOT / p).resolve()
    if not resolved.is_relative_to(PROJECT_ROOT.resolve()):
        sys.exit(f"dest {dest!r}: выходит за пределы проекта")
    return resolved


def check_unique_targets(chats: dict, chats_root: Path) -> None:
    """Дубли целевых папок - невалидный конфиг и здесь тоже (зеркально
    telegram-snapshot.py): deltas запускается независимо от снапшота, и без
    своей проверки читал бы одну папку под двумя labels."""
    targets: dict[str, str] = {}
    for label, entry in chats.items():
        tgt = resolve_dest(entry["dest"]) if entry.get("dest") else (chats_root / label).resolve()
        key = str(tgt).casefold()
        if key in targets:
            sys.exit(
                f"чаты {targets[key]!r} и {label!r} указывают в одну папку "
                f"{tgt} - разведите dest в .telegram-snapshot.json"
            )
        targets[key] = label


def load_project_config() -> dict:
    if not PROJECT_CONFIG_PATH.exists():
        sys.stderr.write(
            f"Нет проектного конфига {PROJECT_CONFIG_PATH}.\n"
            "Формат - см. скилл telegram-snapshot.\n"
        )
        sys.exit(2)
    with PROJECT_CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("chats"):
        sys.stderr.write(f"В {PROJECT_CONFIG_PATH} не заполнено поле chats\n")
        sys.exit(2)
    try:
        cfg["chats"] = {label: chat_entry(v) for label, v in cfg["chats"].items()}
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"В {PROJECT_CONFIG_PATH} некорректная запись chats: {exc}\n")
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


def filter_new(cur: list[dict], prev: list[dict], hours_fallback: int,
               topic_id: int | None = None) -> list[dict]:
    if topic_id is not None:
        cur = [m for m in cur if m.get("topic_id") == topic_id]
        prev = [m for m in prev if m.get("topic_id") == topic_id]
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


PERSONAL_PREFIX = "1-1/"


def emit_chat(label: str, display: str, chats_root, hours: int,
              topic_id: int | None = None, dest_dir: Path | None = None) -> int:
    chat_dir = dest_dir if dest_dir is not None else chats_root / label
    cur = load_messages(chat_dir / "result.json")
    prev = load_messages(chat_dir / "result.prev.json")

    new = filter_new(cur, prev, hours, topic_id)
    suffix = "" if prev else f" (нет .prev, окно {hours}ч)"
    if topic_id is not None:
        suffix += f" (топик {topic_id})"
    print(f"**{display}: {len(new)}**{suffix}\n")
    if not new:
        print("_Без изменений._\n")
        return 0
    for m in new:
        print(format_msg(m))
    print()
    return len(new)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24, help="Окно, если нет .prev.json")
    args = p.parse_args()

    project_cfg = load_project_config()
    chats_root = PROJECT_ROOT / project_cfg["chats_root"]
    check_unique_targets(project_cfg["chats"], chats_root)
    labels = list(project_cfg["chats"].keys())
    group_labels = [l for l in labels if not l.startswith(PERSONAL_PREFIX)]
    personal_labels = [l for l in labels if l.startswith(PERSONAL_PREFIX)]

    print(f"### Новое в чатах ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n")

    chats = project_cfg["chats"]

    grand_total = 0
    for label in group_labels:
        grand_total += emit_chat(label, label, chats_root, args.hours,
                                 chats[label].get("topic_id"),
                                 resolve_dest(chats[label]["dest"]) if chats[label].get("dest") else None)

    if personal_labels:
        print("#### 1-1\n")
        for label in personal_labels:
            display = label[len(PERSONAL_PREFIX):]
            grand_total += emit_chat(label, display, chats_root, args.hours,
                                     chats[label].get("topic_id"),
                                     resolve_dest(chats[label]["dest"]) if chats[label].get("dest") else None)

    if grand_total == 0:
        print("_Изменений нет._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
