#!/usr/bin/env python3
"""
Инкрементальный pull новых сообщений из Telegram-чатов проекта.

Тянет новые сообщения через Telethon (MTProto) с момента последнего id в
существующих result.json, дописывает их в конец. Если result.json нет -
делает bootstrap: создает шапку чата (name/type/id) через get_entity,
тянет всю историю с min_id=0.

Формат записи - расширенный Telegram Desktop export. В шапке добавлено
поле `topics: [{id, title, date, ...}]` с метаданными форумных тем; у
каждого сообщения внутри форум-темы - опциональное `topic_id` (корень
темы) и отделенное от него `reply_to_message_id` (только настоящие
реплаи, не "цепляние к шапке").

Перед записью существующий JSON копируется в `result.prev.json` для
расчета дельт (telegram-deltas.py).

Если найден старый формат (сырой TG Desktop экспорт без `topics[]` в
шапке) - делается миграция: топик-события извлекаются в `topics[]`,
по reply-цепочкам проставляется `topic_id`, исходный файл сохраняется
в `result.pre-migration.json` (один раз, перед первой миграцией -
перезаписывается на каждой повторной попытке миграции). При ошибке
миграции файл не трогается, чат пропускается.

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
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.tl.types import (
        Channel,
        Chat,
        MessageActionTopicCreate,
        MessageActionTopicEdit,
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
        MessageMediaWebPage,
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

# Вложения качаются в локальный НЕсинкаемый кэш (не в vault/проект - там бывают
# секреты клиентов, а папки проекта синкаются в облако). result.json остается
# текстовым (медиа туда НЕ пишем); корреляция сообщение<->файл по <msg_id> в
# имени файла кэша + метаданные file_name/mime в самой записи. TTL: на каждом
# запуске чистим файлы старше суток.
MEDIA_CACHE = Path.home() / ".cache" / "telegram-snapshot" / "media"
MEDIA_TTL_HOURS = 24
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
            "Настрой авторизацию - см. скилл telegram-snapshot.\n"
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


def chat_entry(value) -> dict:
    """Нормализует значение из chats к {"id": int, "topic_id": int|None}.

    Поддерживаются две формы записи чата:
      - короткая:  "label": 4715985727
      - расширенная: "label": {"id": 4715985727, "topic_id": 123}

    topic_id используется telegram-deltas.py для отбора сообщений только
    нужной форумной темы (например, топика бота). На саму выкачку не
    влияет - snapshot всегда тянет чат целиком.
    """
    if isinstance(value, dict):
        if "id" not in value:
            raise ValueError("в расширенной записи чата нет поля id")
        return {"id": int(value["id"]), "topic_id": value.get("topic_id")}
    return {"id": int(value), "topic_id": None}


def load_project_config() -> dict:
    if not PROJECT_CONFIG_PATH.exists():
        sys.stderr.write(
            f"Нет проектного конфига {PROJECT_CONFIG_PATH}.\n"
            "Формат - см. скилл telegram-snapshot, шаг \"Подключение нового проекта\".\n"
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


def chats_root_path(project_cfg: dict) -> Path:
    return PROJECT_ROOT / project_cfg["chats_root"]


def load_existing(result_path: Path) -> dict:
    with result_path.open(encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: Path, data: dict) -> None:
    """Записывает JSON атомарно через временный файл + os.replace.

    Защита от битого файла при падении посреди json.dump (Ctrl+C, OOM, ...).
    """
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def last_message_id(data: dict) -> int:
    """High-water mark для инкрементального pull.

    Учитываем id всех messages и id всех topic-корней из шапки: иначе
    service-only хвост (свежий topic_create / topic_rename без сообщений
    под ним) будет тянуться каждый запуск, потому что max(messages.id)
    их не покрывает (topic-сообщения после миграции/обработки в messages
    не лежат - они только в topics[]).
    """
    ids: list[int] = []
    for m in data.get("messages", []):
        if isinstance(m.get("id"), int):
            ids.append(m["id"])
    for t in data.get("topics", []):
        if isinstance(t.get("id"), int):
            ids.append(t["id"])
    return max(ids) if ids else 0


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


def extract_topic_meta(msg) -> dict | None:
    """Если service-сообщение создает форумную тему - возвращает ее метаданные."""
    if msg.action is None:
        return None
    if isinstance(msg.action, MessageActionTopicCreate):
        return {
            "id": msg.id,
            "title": msg.action.title,
            "date": msg.date.astimezone().strftime("%Y-%m-%dT%H:%M:%S"),
            "date_unixtime": str(int(msg.date.timestamp())),
            "icon_color": getattr(msg.action, "icon_color", None),
        }
    return None


def topic_id_from_reply(reply_to) -> int | None:
    """Корень темы из MessageReplyHeader. None если сообщение вне форумной темы."""
    if reply_to is None:
        return None
    if not getattr(reply_to, "forum_topic", False):
        return None
    return getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)


def migrate_legacy(data: dict) -> tuple[dict, dict]:
    """Конвертирует сырой Telegram Desktop экспорт в расширенный формат.

    Что делает:
    1. Собирает метаданные топиков из service-сообщений `topic_created`
       в шапку `topics[]`.
    2. Применяет `topic_edited` к названиям соответствующих топиков.
    3. Проставляет каждому сообщению `topic_id`, поднимаясь по цепочке
       `reply_to_message_id` до сообщения, чей id есть в topic-корнях.
    4. Если `reply_to_message_id` указывал ровно на корень темы -
       очищает поле (цепляние к шапке выражается через `topic_id`).
    5. Удаляет `topic_created` / `topic_edited` service-сообщения из
       `messages[]` - вся инфа теперь в `topics[]`.

    Идемпотентна: если в data уже есть ключ "topics" - возвращает data
    как есть с нулевой статистикой. Защита от повторного вызова на уже
    мигрированном файле (затёр бы topics[] нулевым набором, если бы
    service-сообщений в messages не осталось).

    Возвращает (mutated_data, stats).
    """
    if "topics" in data:
        return data, {
            "topics_extracted": 0,
            "topic_edits_applied": 0,
            "messages_with_topic_id": 0,
        }

    msgs = data.get("messages", [])

    topics_by_id: dict[int, dict] = {}
    for m in msgs:
        if m.get("type") == "service" and m.get("action") == "topic_created":
            topic_entry: dict = {
                "id": m["id"],
                "title": m.get("title", ""),
                "date": m.get("date", ""),
                "date_unixtime": m.get("date_unixtime", ""),
            }
            # icon_color в TG Desktop экспорте бывает null или отсутствует;
            # bootstrap-режим всегда кладёт ключ (через getattr с None default),
            # держим тот же контракт - проставляем None если ключа нет.
            topic_entry["icon_color"] = m.get("icon_color")
            topics_by_id[m["id"]] = topic_entry

    topic_edits_applied = 0
    for m in msgs:
        if m.get("type") == "service" and m.get("action") == "topic_edited":
            new_title = m.get("new_title") or m.get("title")
            rid = m.get("reply_to_message_id")
            if rid and rid in topics_by_id and new_title:
                topics_by_id[rid]["title"] = new_title
                topic_edits_applied += 1

    # Снимок reply-цепочки до мутирующего цикла: ниже мы удаляем
    # reply_to_message_id у прямых ответов на корень топика, и если читать
    # это поле из живых объектов, последующие "reply на reply" теряют путь
    # к корню и остаются без topic_id.
    reply_chain: dict[int, int | None] = {
        m["id"]: m.get("reply_to_message_id")
        for m in msgs if isinstance(m.get("id"), int)
    }
    topic_root_ids = set(topics_by_id.keys())

    messages_with_topic_id = 0
    for m in msgs:
        if m.get("type") != "message":
            continue
        rid = m.get("reply_to_message_id")
        if rid is None:
            continue
        visited: set[int] = set()
        cur_rid = rid
        topic_root: int | None = None
        while cur_rid is not None and cur_rid not in visited:
            visited.add(cur_rid)
            if cur_rid in topic_root_ids:
                topic_root = cur_rid
                break
            cur_rid = reply_chain.get(cur_rid)
        if topic_root is not None:
            m["topic_id"] = topic_root
            messages_with_topic_id += 1
            if rid == topic_root:
                del m["reply_to_message_id"]

    cleaned_msgs = [
        m for m in msgs
        if not (
            m.get("type") == "service"
            and m.get("action") in ("topic_created", "topic_edited")
        )
    ]
    sorted_topics = sorted(topics_by_id.values(), key=lambda t: t["id"])

    # Перестраиваем data так, чтобы topics шли ДО messages (как в bootstrap-формате).
    new_data: dict = {}
    for k in ("name", "type", "id"):
        if k in data:
            new_data[k] = data[k]
    new_data["topics"] = sorted_topics
    new_data["messages"] = cleaned_msgs
    for k, v in data.items():
        if k not in new_data:
            new_data[k] = v
    data.clear()
    data.update(new_data)

    return data, {
        "topics_extracted": len(topics_by_id),
        "topic_edits_applied": topic_edits_applied,
        "messages_with_topic_id": messages_with_topic_id,
    }


def repair_legacy_migration(data: dict) -> tuple[dict, dict]:
    """Чинит снапшоты, мигрированные старой багнутой версией migrate_legacy.

    Баг: в старой реализации reply-цепочка читалась из живых объектов,
    которые мутировались в том же цикле (del reply_to_message_id у direct
    reply на корень). Из-за этого "reply на reply" внутри топика теряли
    путь к корню и оставались без topic_id - ~60% сообщений в форумном чате.

    Эта функция прогоняется на уже мигрированных снапшотах (где есть
    topics[]) и достраивает topic_id для потеряшек, поднимаясь по
    reply_to_message_id через СНИМОК цепочки. Признает корнем:
    - id из topics[] (исходные корни),
    - сообщение, у которого уже проставлен topic_id (шорткат через
      уже починенных соседей).

    Идемпотентна: на здоровом снапшоте возвращает messages_repaired=0.
    """
    msgs = data.get("messages") or []
    topics = data.get("topics") or []
    if not topics or not msgs:
        return data, {"messages_repaired": 0}

    topic_root_ids: set[int] = {
        t["id"] for t in topics if isinstance(t.get("id"), int)
    }
    if not topic_root_ids:
        return data, {"messages_repaired": 0}

    reply_chain: dict[int, int | None] = {
        m["id"]: m.get("reply_to_message_id")
        for m in msgs if isinstance(m.get("id"), int)
    }
    known_topic: dict[int, int] = {
        m["id"]: m["topic_id"]
        for m in msgs
        if isinstance(m.get("id"), int) and isinstance(m.get("topic_id"), int)
    }

    repaired = 0
    for m in msgs:
        if m.get("type") != "message":
            continue
        if "topic_id" in m:
            continue
        rid = m.get("reply_to_message_id")
        if rid is None:
            continue
        visited: set[int] = set()
        cur_rid: int | None = rid
        topic_root: int | None = None
        while cur_rid is not None and cur_rid not in visited:
            visited.add(cur_rid)
            if cur_rid in topic_root_ids:
                topic_root = cur_rid
                break
            if cur_rid in known_topic:
                topic_root = known_topic[cur_rid]
                break
            cur_rid = reply_chain.get(cur_rid)
        if topic_root is not None:
            m["topic_id"] = topic_root
            known_topic[m["id"]] = topic_root
            repaired += 1
            if rid == topic_root:
                del m["reply_to_message_id"]

    return data, {"messages_repaired": repaired}


async def download_message_media(client: TelegramClient, msg, chat_media_dir: Path) -> str | None:
    """Скачивает вложение сообщения в несинкаемый кэш. Возвращает имя файла или None.

    result.json НЕ трогаем - метаданные (file_name/mime) уже пишет
    message_to_record, здесь только байты на диск, чтобы их мог прочитать агент
    (скрин - через Read визуально, файл - сканером). Превью ссылок (webpage)
    вложением не считаем. Имя файла несет <msg_id> для корреляции с сообщением.
    """
    media = getattr(msg, "media", None)
    if not media or isinstance(media, MessageMediaWebPage):
        return None
    base = msg.file.name if (msg.file and msg.file.name) else None
    ext = (msg.file.ext if msg.file else "") or ".bin"
    fname = f"{msg.id}_{base}" if base else f"{msg.id}{ext}"
    dest = chat_media_dir / fname
    if dest.exists():
        return dest.name
    chat_media_dir.mkdir(parents=True, exist_ok=True)
    try:
        await client.download_media(msg, file=str(dest))
        return dest.name
    except Exception as exc:  # noqa: BLE001 - best-effort, не роняем pull из-за одного файла
        sys.stderr.write(f"медиа msg {msg.id}: не скачалось ({exc})\n")
        return None


def cleanup_old_media(ttl_hours: int = MEDIA_TTL_HOURS) -> int:
    """Удаляет файлы медиа-кэша старше ttl_hours (по mtime). Возвращает число удаленных."""
    if not MEDIA_CACHE.exists():
        return 0
    cutoff = time.time() - ttl_hours * 3600
    removed = 0
    for f in MEDIA_CACHE.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


async def fetch_new(client: TelegramClient, entity, min_id: int) -> tuple[list[dict], list[dict], dict, list[str]]:
    """Тянет новые сообщения с id > min_id.

    Возвращает (messages, new_topics, topic_edits, downloaded_media) - сообщения
    в формате TG Desktop, свежесозданные темы, накопленные правки названий
    (root_id -> new_title) и имена скачанных в кэш вложений.
    """
    out: list[dict] = []
    new_topics: list[dict] = []
    topic_edits: dict = {}
    downloaded: list[str] = []
    chat_media_dir = MEDIA_CACHE / str(getattr(entity, "id", "unknown"))
    async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
        if min_id and msg.id <= min_id:
            continue
        if msg.action is not None:
            tmeta = extract_topic_meta(msg)
            if tmeta:
                new_topics.append(tmeta)
            elif isinstance(msg.action, MessageActionTopicEdit) and getattr(msg.action, "title", None):
                root_id = topic_id_from_reply(msg.reply_to)
                if root_id is not None:
                    topic_edits[root_id] = msg.action.title
            continue
        record = await message_to_record(client, msg)
        if record:
            out.append(record)
            name = await download_message_media(client, msg, chat_media_dir)
            if name:
                downloaded.append(name)
    return out, new_topics, topic_edits, downloaded


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

    if msg.edit_date:
        rec["edited"] = msg.edit_date.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        rec["edited_unixtime"] = str(int(msg.edit_date.timestamp()))

    # Привязка к форумной теме (если есть) и настоящий реплай разводятся:
    # topic_id - корень темы; reply_to_message_id - реальный ответ.
    topic_id = topic_id_from_reply(msg.reply_to)
    if topic_id is not None:
        rec["topic_id"] = topic_id

    if msg.reply_to is not None:
        rmsg = getattr(msg.reply_to, "reply_to_msg_id", None)
        if rmsg and rmsg != topic_id:
            rec["reply_to_message_id"] = rmsg

    if msg.file:
        rec["file_name"] = msg.file.name or "(no name)"
        if msg.file.size is not None:
            rec["file_size"] = msg.file.size
        if msg.file.mime_type:
            rec["mime_type"] = msg.file.mime_type

    return rec


def chat_type_from_entity(entity) -> str:
    """Маппит Telethon entity на тип чата в формате Telegram Desktop export."""
    if isinstance(entity, Channel):
        has_username = bool(getattr(entity, "username", None))
        if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
            return "public_supergroup" if has_username else "private_supergroup"
        if getattr(entity, "broadcast", False):
            return "public_channel" if has_username else "private_channel"
        return "public_supergroup" if has_username else "private_supergroup"
    if isinstance(entity, Chat):
        # basic group (не super-)
        return "private_group"
    # User / приватный 1-1 диалог
    return "personal_chat"


async def resolve_entity(client: TelegramClient, chat_id: int, dialog_entities: dict):
    """Возвращает entity чата по unmarked-id.

    Резолвим строго через карту диалогов (dialog_entities), а НЕ через
    client.get_entity(chat_id): в некоторых сессиях локальный entity-cache
    оказывается битым и get_entity на голый int возвращает чужой чат с тем
    же магическим id (наблюдалось: один и тот же id резолвился то одним
    чатом, то другим). Карта строится из свежих серверных entity в iter_dialogs -
    у них корректный access_hash. get_entity оставлен только как фолбэк для
    чатов, которых нет в списке диалогов (архивные/скрытые).
    """
    entity = dialog_entities.get(chat_id)
    if entity is not None:
        return entity
    return await client.get_entity(chat_id)


async def process_chat(client: TelegramClient, chats_root: Path, label: str, chat_id: int, dialog_entities: dict) -> tuple[int, str]:
    result_path = chats_root / label / "result.json"
    entity = await resolve_entity(client, chat_id, dialog_entities)

    removed = cleanup_old_media()
    if removed:
        print(f"  медиа-кэш: удалено файлов старше {MEDIA_TTL_HOURS}ч: {removed}")

    migrated = False
    repaired = False
    if result_path.exists():
        data = load_existing(result_path)
        is_bootstrap = False

        if "topics" not in data:
            pre_path = result_path.with_name("result.pre-migration.json")
            shutil.copy2(result_path, pre_path)
            try:
                data, stats = migrate_legacy(data)
            except Exception as exc:
                sys.stderr.write(
                    f"!! {label}: миграция legacy формата упала ({exc}). "
                    f"Файл не изменен, бэкап в {pre_path.name}, чат пропущен.\n"
                )
                return 0, ""
            migrated = True
            print(
                f"  {label}: миграция legacy -> new "
                f"(топиков: {stats['topics_extracted']}, "
                f"topic_id проставлено: {stats['messages_with_topic_id']}, "
                f"переименований применено: {stats['topic_edits_applied']}). "
                f"Бэкап: {pre_path.name}"
            )
        else:
            # Снапшот уже мигрирован. Проверяем не пострадал ли он от старого
            # бага migrate_legacy (reply-on-reply внутри топика без topic_id).
            # Чиним прямо на месте, чтобы не потерять инкременты, накопленные
            # после первой миграции.
            data, repair_stats = repair_legacy_migration(data)
            if repair_stats["messages_repaired"] > 0:
                pre_repair_path = result_path.with_name("result.pre-repair.json")
                if not pre_repair_path.exists():
                    shutil.copy2(result_path, pre_repair_path)
                repaired = True
                print(
                    f"  {label}: починка topic_id после старого бага миграции "
                    f"(восстановлено: {repair_stats['messages_repaired']}). "
                    f"Бэкап: {pre_repair_path.name}"
                )

        min_id = last_message_id(data)
    else:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "name": getattr(entity, "title", "") or label,
            "type": chat_type_from_entity(entity),
            "id": chat_id,
            "topics": [],
            "messages": [],
        }
        min_id = 0
        is_bootstrap = True
        print(f"  {label}: bootstrap (нет result.json, тянем всю историю)")

    data.setdefault("topics", [])
    topics_by_id = {t["id"]: t for t in data["topics"]}

    new_msgs, new_topics, topic_edits, downloaded_media = await fetch_new(client, entity, min_id)

    for t in new_topics:
        topics_by_id[t["id"]] = t
    for root_id, new_title in topic_edits.items():
        if root_id in topics_by_id:
            topics_by_id[root_id]["title"] = new_title

    data["topics"] = sorted(topics_by_id.values(), key=lambda t: t["id"])

    has_changes = bool(new_msgs or new_topics or topic_edits)
    needs_save = is_bootstrap or migrated or repaired or has_changes

    if not needs_save:
        last_date = data["messages"][-1]["date"] if data.get("messages") else "?"
        print(f"  {label}: новых нет (последнее {last_date})")
        return 0, last_date

    # baseline для дельт обновляем ТОЛЬКО при реальных новых сообщениях.
    # Без has_changes (например, при идемпотентной миграции без новых) prev.json
    # оставляем как есть, чтобы не задвоить окно "новое" на следующих запусках.
    # При bootstrap baseline не нужен - не с чем сравнивать.
    if not is_bootstrap and has_changes:
        prev_path = result_path.with_name("result.prev.json")
        atomic_write_json(prev_path, data)

    data["messages"].extend(new_msgs)
    data["messages"].sort(key=lambda m: m["id"])

    atomic_write_json(result_path, data)

    last_date = new_msgs[-1]["date"] if new_msgs else (data["messages"][-1]["date"] if data.get("messages") else "?")
    prefix = "bootstrap " if is_bootstrap else ""
    print(f"  {label}: {prefix}+{len(new_msgs)} (последнее {last_date}, тем: {len(data['topics'])})")
    if downloaded_media:
        print(f"  {label}: вложений скачано в кэш: {len(downloaded_media)} -> {MEDIA_CACHE / str(chat_id)}")
    return len(new_msgs), last_date


async def amain() -> int:
    auth = load_auth()
    project_cfg = load_project_config()
    chats_root = chats_root_path(project_cfg)
    session_path = str(AUTH_DIR / auth["session_name"])

    client = TelegramClient(session_path, auth["api_id"], auth["api_hash"])
    await client.start()

    print(f"snapshot {datetime.now(timezone.utc).isoformat()}")

    # Перебор диалогов: строим карту {unmarked_id -> свежий entity}. Нужна
    # и для прогрева (get_entity на свежей сессии иначе трактует int как
    # PeerUser), и - главное - как авторитетный источник entity вместо
    # битого локального кеша (см. resolve_entity).
    dialog_entities: dict = {}
    async for d in client.iter_dialogs():
        eid = getattr(d.entity, "id", None)
        if eid is not None:
            dialog_entities[eid] = d.entity

    total = 0
    for label, entry in project_cfg["chats"].items():
        chat_id = entry["id"]
        try:
            n, _ = await process_chat(client, chats_root, label, chat_id, dialog_entities)
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
