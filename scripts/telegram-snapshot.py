#!/usr/bin/env python3
"""
Инкрементальный pull новых сообщений из Telegram-чатов проекта.

Тянет новые сообщения через Telethon (MTProto) с момента последнего id в
существующих result.json, дописывает их в конец, сохраняя совместимость
с форматом Telegram Desktop export. Перед записью копирует прежний JSON
в `result.prev.json` для расчета дельт.

Конфиг разделен на две части:
  - Общие credentials (api_id, api_hash, session) - в ~/.config/telegram-snapshot/auth.json,
    один раз на устройство, один аккаунт Telegram. Не коммитить.
  - Проектные чаты - в .telegram-snapshot.json в корне проекта (рядом со скриптом),
    {chats_root, chats: {label: chat_id}}. Не секрет, можно коммитить.

Запуск:
    python3 scripts/telegram-snapshot.py

Зависимости:
    pip3 install --user telethon
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.tl.types import (
        MessageEntityBold,
        MessageEntityCode,
        MessageEntityItalic,
        MessageEntityMention,
        MessageEntityMentionName,
        MessageEntityPre,
        MessageEntityStrike,
        MessageEntityTextUrl,
        MessageEntityUnderline,
        MessageEntityUrl,
        PeerChannel,
        PeerChat,
    )
except ImportError:
    sys.stderr.write(
        "telethon не установлен. Поставь: pip3 install --user telethon\n"
    )
    sys.exit(2)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = Path.home() / ".config" / "telegram-snapshot"
AUTH_PATH = AUTH_DIR / "auth.json"
PROJECT_CONFIG_PATH = PROJECT_ROOT / ".telegram-snapshot.json"

ENTITY_MAP = {
    MessageEntityBold: "bold",
    MessageEntityItalic: "italic",
    MessageEntityUnderline: "underline",
    MessageEntityStrike: "strikethrough",
    MessageEntityCode: "code",
    MessageEntityPre: "pre",
    MessageEntityUrl: "link",
    MessageEntityTextUrl: "text_link",
    MessageEntityMention: "mention",
    MessageEntityMentionName: "mention_name",
}


def load_auth() -> dict:
    if not AUTH_PATH.exists():
        sys.stderr.write(
            f"Нет общего конфига {AUTH_PATH}.\n"
            f"См. инструкцию: {AUTH_DIR}/README.md\n"
        )
        sys.exit(2)
    with AUTH_PATH.open(encoding="utf-8") as f:
        auth = json.load(f)
    missing = [k for k in ("api_id", "api_hash") if not auth.get(k)]
    if missing:
        sys.stderr.write(f"В {AUTH_PATH} не заполнены поля: {missing}\n")
        sys.exit(2)
    auth.setdefault("session_name", "default")
    return auth


def load_project_config() -> dict:
    if not PROJECT_CONFIG_PATH.exists():
        sys.stderr.write(
            f"Нет проектного конфига {PROJECT_CONFIG_PATH}.\n"
            f"Формат - см. ~/.config/telegram-snapshot/README.md (раздел \"Подключение нового проекта\").\n"
        )
        sys.exit(2)
    with PROJECT_CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("chats"):
        sys.stderr.write(f"В {PROJECT_CONFIG_PATH} не заполнено поле chats\n")
        sys.exit(2)
    cfg.setdefault("chats_root", "Встречи/чаты")
    return cfg


def chats_root_path(project_cfg: dict) -> Path:
    return PROJECT_ROOT / project_cfg["chats_root"]


def load_existing(result_path: Path) -> dict:
    with result_path.open(encoding="utf-8") as f:
        return json.load(f)


def last_message_id(data: dict) -> int:
    msgs = data.get("messages", [])
    if not msgs:
        return 0
    return max(m["id"] for m in msgs if isinstance(m.get("id"), int))


def entities_to_text_entities(text: str, entities) -> list[dict]:
    """Конвертирует Telethon entities в формат Telegram Desktop text_entities.

    Возвращает массив сегментов, покрывающих весь text. Несовпадения и
    непокрытые промежутки идут как {"type": "plain", "text": "..."}.
    """
    if not text:
        return []
    if not entities:
        return [{"type": "plain", "text": text}]

    out: list[dict] = []
    cursor = 0
    sorted_entities = sorted(entities, key=lambda e: (e.offset, e.length))
    for ent in sorted_entities:
        start = ent.offset
        end = ent.offset + ent.length
        if start < cursor:
            continue
        if start > cursor:
            out.append({"type": "plain", "text": text[cursor:start]})
        segment = text[start:end]
        etype = ENTITY_MAP.get(type(ent), "plain")
        item: dict = {"type": etype, "text": segment}
        if isinstance(ent, MessageEntityTextUrl):
            item["href"] = ent.url
        elif isinstance(ent, MessageEntityMentionName):
            item["user_id"] = f"user{ent.user_id}"
        out.append(item)
        cursor = end
    if cursor < len(text):
        out.append({"type": "plain", "text": text[cursor:]})
    return out


def text_to_string(text_entities: list[dict]) -> str | list:
    """Telegram Desktop хранит text как строку (если нет разметки)
    или как массив частей (если есть). Возвращаем то же поведение."""
    if not text_entities:
        return ""
    if len(text_entities) == 1 and text_entities[0]["type"] == "plain":
        return text_entities[0]["text"]
    parts: list = []
    for ent in text_entities:
        if ent["type"] == "plain":
            parts.append(ent["text"])
        else:
            parts.append({"type": ent["type"], "text": ent["text"], **({"href": ent["href"]} if "href" in ent else {})})
    return parts


def sender_name(sender) -> str:
    if sender is None:
        return ""
    if getattr(sender, "first_name", None) or getattr(sender, "last_name", None):
        parts = [getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or ""]
        return " ".join(p for p in parts if p).strip()
    if getattr(sender, "title", None):
        return sender.title
    if getattr(sender, "username", None):
        return sender.username
    return ""


def sender_id(msg) -> str:
    peer = msg.from_id or msg.peer_id
    if peer is None:
        return ""
    if isinstance(peer, PeerChannel):
        return f"channel{peer.channel_id}"
    if isinstance(peer, PeerChat):
        return f"chat{peer.chat_id}"
    uid = getattr(peer, "user_id", None) or getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None)
    return f"user{uid}" if uid else ""


async def fetch_new(client: TelegramClient, chat_id: int, min_id: int) -> list[dict]:
    """Тянет новые сообщения с id > min_id, возвращает их в формате TG Desktop."""
    out: list[dict] = []
    entity = await client.get_entity(chat_id)
    async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
        if min_id and msg.id <= min_id:
            continue
        record = await message_to_record(client, msg)
        if record:
            out.append(record)
    return out


async def message_to_record(client: TelegramClient, msg) -> dict | None:
    if msg.action is not None:
        return None

    text = msg.message or ""
    text_entities = entities_to_text_entities(text, msg.entities)
    text_field = text_to_string(text_entities)

    sender = await msg.get_sender() if msg.sender_id else None
    name = sender_name(sender)
    sid = sender_id(msg)

    rec: dict = {
        "id": msg.id,
        "type": "message",
        "date": msg.date.astimezone().strftime("%Y-%m-%dT%H:%M:%S"),
        "date_unixtime": str(int(msg.date.timestamp())),
        "from": name,
        "from_id": sid,
        "text": text_field,
        "text_entities": text_entities,
    }

    if msg.reply_to_msg_id:
        rec["reply_to_message_id"] = msg.reply_to_msg_id

    if msg.file:
        rec["file_name"] = msg.file.name or "(no name)"
        if msg.file.size is not None:
            rec["file_size"] = msg.file.size
        if msg.file.mime_type:
            rec["mime_type"] = msg.file.mime_type

    return rec


async def process_chat(client: TelegramClient, chats_root: Path, label: str, chat_id: int) -> tuple[int, str]:
    result_path = chats_root / label / "result.json"
    if not result_path.exists():
        sys.stderr.write(f"!! Нет {result_path}. Сделай первый экспорт через Telegram Desktop.\n")
        return 0, ""

    data = load_existing(result_path)
    min_id = last_message_id(data)

    new_msgs = await fetch_new(client, chat_id, min_id)
    if not new_msgs:
        last_date = data["messages"][-1]["date"] if data.get("messages") else "?"
        print(f"  {label}: новых нет (последнее {last_date})")
        return 0, last_date

    prev_path = result_path.with_name("result.prev.json")
    shutil.copy2(result_path, prev_path)

    data["messages"].extend(new_msgs)
    data["messages"].sort(key=lambda m: m["id"])

    with result_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    last_date = new_msgs[-1]["date"]
    print(f"  {label}: +{len(new_msgs)} (последнее {last_date})")
    return len(new_msgs), last_date


async def amain() -> int:
    auth = load_auth()
    project_cfg = load_project_config()
    chats_root = chats_root_path(project_cfg)
    session_path = str(AUTH_DIR / auth["session_name"])

    client = TelegramClient(session_path, auth["api_id"], auth["api_hash"])
    await client.start()

    print(f"snapshot {datetime.now(timezone.utc).isoformat()}")

    # Прогрев кеша диалогов: без этого get_entity(int_id) на свежей сессии
    # интерпретирует id как PeerUser. Перебор диалогов кеширует entity в session.
    async for _ in client.iter_dialogs():
        pass

    total = 0
    for label, chat_id in project_cfg["chats"].items():
        try:
            n, _ = await process_chat(client, chats_root, label, chat_id)
            total += n
        except Exception as exc:
            sys.stderr.write(f"!! {label} ({chat_id}): {exc}\n")
    await client.disconnect()
    print(f"\nOK: +{total} сообщений всего")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
