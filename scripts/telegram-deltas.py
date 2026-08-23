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
import hashlib
import json
import os
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
    """dest из конфига -> абсолютный путь (зеркально telegram-snapshot.py:
    тот же конфиг обязан читаться из той же папки).

    Абсолютный путь разрешен - им зеркала уводятся из синкаемой папки.
    Относительный обязан остаться внутри проекта: опечатка "../.." писала бы
    мимо. Расхождение с telegram-snapshot.py здесь означало бы, что deltas
    ищет зеркала не там, где их пишет снапшот, и молча выдает пустые дельты."""
    p = Path(dest).expanduser()
    if p.is_absolute():
        return p.resolve()
    resolved = (PROJECT_ROOT / p).resolve()
    if not resolved.is_relative_to(PROJECT_ROOT.resolve()):
        sys.exit(f"dest {dest!r}: относительный путь выходит за пределы проекта")
    return resolved


MIRROR_STORE = (
    Path(os.environ["TELEGRAM_SNAPSHOT_STORE"]).expanduser()
    if os.environ.get("TELEGRAM_SNAPSHOT_STORE")
    else Path.home() / ".local" / "share" / "telegram-snapshot" / "chats"
)
if not MIRROR_STORE.is_absolute():
    sys.exit(
        f"TELEGRAM_SNAPSHOT_STORE={str(MIRROR_STORE)!r}: нужен абсолютный путь. "
        "Относительный считается от рабочего каталога и легко превращает "
        "хранилище вне синка в папку внутри проекта"
    )
LEGACY_CHATS_ROOT = "Встречи/чаты"


def project_store_slug() -> str:
    """Зеркально telegram-snapshot.py: basename плюс хвост хеша полного пути,
    иначе два проекта с одинаковым именем папки делят одно хранилище."""
    root = PROJECT_ROOT.resolve()
    return f"{root.name}-{hashlib.sha1(str(root).encode('utf-8')).hexdigest()[:8]}"


def legacy_has_mirrors() -> bool:
    """Зеркально telegram-snapshot.py: пустая папка дефолт не перехватывает.
    Иначе созданная синком пустая директория переключала бы дельты на legacy,
    пока снапшот пишет в хранилище - и дельты молча показывали бы ноль."""
    legacy = PROJECT_ROOT / LEGACY_CHATS_ROOT
    if not legacy.is_dir():
        return False
    return any(legacy.glob("*/result.json")) or any(legacy.glob("*/*/result.json"))


def default_chats_root() -> str:
    """Зеркально telegram-snapshot.py: дефолт - хранилище вне синка, но уже
    существующая legacy-папка проекта выигрывает. Логика обязана совпадать с
    той, что в снапшоте, иначе deltas читает не ту папку и молча отдает ноль
    новых сообщений вместо ошибки."""
    if legacy_has_mirrors():
        return LEGACY_CHATS_ROOT
    return str(MIRROR_STORE / project_store_slug())


def chats_root_path(project_cfg: dict) -> Path:
    raw = Path(str(project_cfg["chats_root"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()


def resolve_label_target(chats_root: Path, label: str) -> Path:
    """Зеркально telegram-snapshot.py: label не должен уводить за chats_root."""
    target = (chats_root / label).resolve()
    root = chats_root.resolve()
    if not target.is_relative_to(root):
        sys.exit(f"label {label!r}: выходит за пределы chats_root ({chats_root})")
    if target == root:
        sys.exit(f"label {label!r}: схлопывается в сам chats_root - зеркало легло бы в корень хранилища")
    return target


def check_unique_targets(chats: dict, chats_root: Path) -> None:
    """Дубли целевых папок - невалидный конфиг и здесь тоже (зеркально
    telegram-snapshot.py): deltas запускается независимо от снапшота, и без
    своей проверки читал бы одну папку под двумя labels."""
    targets: dict[str, str] = {}
    for label, entry in chats.items():
        tgt = resolve_dest(entry["dest"]) if entry.get("dest") else resolve_label_target(chats_root, label)
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
    if "chats_root" not in cfg:
        cfg["chats_root"] = default_chats_root()
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
    chats_root = chats_root_path(project_cfg)
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
