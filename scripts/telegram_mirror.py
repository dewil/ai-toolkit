"""Shared Telegram export format and durable owner mirror operations.

Pure snapshot helpers are shared with the direct exporter; no Telegram imports.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import uuid

EDIT_TEXT_KEYS = ("text", "text_entities", "edited", "edited_unixtime")
EDIT_MEDIA_KEYS = ("file_name", "file_size", "mime_type")


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


def merge_edits(messages: list[dict], fresh: dict[int, dict], seen_ids: set[int],
                complete: bool = False, *, revision_ordered: bool = False) -> tuple[int, int]:
    """Сверяет зеркало с перечитанным окном. Чистая функция, без сети.

    - правка: в API `edited_unixtime` новее, чем в зеркале (или в зеркале его
      нет), либо равен ему при другом содержимом (две правки в одну секунду) -
      обновляются текст и метаданные вложения (EDIT_TEXT_KEYS, EDIT_MEDIA_KEYS),
      а прежние значения, если отличались, уходят в `edit_history`: правка
      задним числом должна быть видна, а не затирать прошлое молча. Сам файл
      замененного вложения заново не скачивается - только метаданные;
    - удаление: id внутри окна, которого в API больше нет, получает
      `deleted: true`; из зеркала запись не удаляется. Нижняя граница окна -
      `min(seen_ids)`; `complete=True` (история короче окна, API отдал меньше,
      чем просили) опускает ее до нуля - иначе удаление начала короткого чата
      не видно никогда.

    Пустое окно - "ничего не известно", а не "все удалено": (0, 0). Сообщения
    ниже окна не трогаются. Повторный прогон по тому же окну ничего не меняет.
    Возвращает (edits, deletions).
    """
    if not seen_ids:
        return 0, 0
    floor = 0 if complete else min(seen_ids)
    edits = deletions = 0
    for m in messages:
        mid = m.get("id")
        if not isinstance(mid, int) or mid < floor:
            continue
        if mid in fresh:
            f = fresh[mid]
            new_e = int(f.get("edited_unixtime") or 0)
            old_e = int(m.get("edited_unixtime") or 0)
            changed = [k for k in EDIT_TEXT_KEYS[:2] + EDIT_MEDIA_KEYS if k in f and f.get(k) != m.get(k)]
            if not revision_ordered and (new_e < old_e or (new_e == old_e and not changed)):
                continue
            if new_e == 0 and not revision_ordered:
                # сообщение никем не правилось (нет edit_date), а содержимое
                # разошлось - это дрейф нашего конвертера против Desktop-экспорта,
                # не правка: верную запись зеркала не трогаем
                continue
            if changed:
                entry = {"edited": m.get("edited") or m.get("date"),
                         "text": m.get("text", ""), "text_entities": m.get("text_entities", [])}
                for k in EDIT_MEDIA_KEYS:
                    if k in changed:
                        entry[k] = m.get(k)
                m.setdefault("edit_history", []).append(entry)
            for key in EDIT_TEXT_KEYS + EDIT_MEDIA_KEYS:
                if key in f:
                    m[key] = f[key]
            edits += 1
        elif mid not in seen_ids and m.get("type") == "message" and not m.get("deleted"):
            m["deleted"] = True
            deletions += 1
    return edits, deletions


def _json(data):
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _id(value):
    return type(value) is int and value > 0


def revision(record):
    return [int(record.get("edited_unixtime") or record.get("date_unixtime") or 0), 0]


class Mirror:
    """One registered destination; all filesystem effects use the injected Store.

    The journal commits result, revisions and the checkpoint boundary together.
    A failed pair write remains unavailable to readers until replay under lock.
    No cursor is stored: every history boundary comes from the durable export.
    """

    def __init__(self, destination, peer, store):
        self.destination, self.peer, self.store = destination, peer, store
        self.directory = destination["path"]
        self.result = self.directory / "result.json"
        self.state_path = self.directory / ".telegram-owner-state.json"
        self.journal = self.directory / ".telegram-owner-journal.json"
        self.identity = {k: peer[k] for k in ("account", "kind", "id")}

    def _read(self, path):
        try:
            value = json.loads(self.store.read(path))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise ValueError("MIRROR_INVALID")
        return value

    @contextmanager
    def locked(self):
        try:
            self.store.inspect(self.directory)
        except FileNotFoundError:
            # atomic_write creates private parents, including with umask 0002.
            self.store.atomic_write(self.directory / ".telegram-owner-init", b"")
        with self.store.lock(self.directory / ".telegram-owner.lock"):
            yield

    def _validate(self, data):
        if (not isinstance(data, dict)
                or data.get("id") is not None and (not _id(data["id"]) or data["id"] != self.peer["id"])
                or not isinstance(data.get("name"), str) or not isinstance(data.get("type"), str)
                or not isinstance(data.get("messages"), list)
                or "topics" in data and not isinstance(data["topics"], list)):
            raise ValueError("MIRROR_INVALID")
        for name in ("messages", "topics"):
            records = data.get(name, [])
            if any(not isinstance(r, dict) or not _id(r.get("id")) for r in records):
                raise ValueError("MIRROR_INVALID")
            if len({r["id"] for r in records}) != len(records):
                raise ValueError("MIRROR_INVALID")

    def load(self):
        data = self._read(self.result)
        if data is not None:
            self._validate(data)  # Never replace a broken/foreign mirror, even on replay.
        pending = self._read(self.journal)
        if pending is not None:
            self._validate(pending["result"])
            if pending["state"]["peer"] != self.identity:
                raise ValueError("MIRROR_INVALID")
            self._finish(pending)
            data = pending["result"]
        if data is None:
            kind = self.peer["kind"]
            chat_type = self.peer.get("type") or (
                "personal_chat" if kind == "User" else "private_group" if kind == "Chat"
                else "public_supergroup" if self.peer.get("username") else "private_supergroup")
            data = {"name": self.peer.get("name") or self.destination["destination_id"],
                    "type": chat_type, "id": self.peer["id"], "topics": [], "messages": []}
            if self.peer.get("username"):
                data["username"] = self.peer["username"]
        elif "topics" not in data or data.get("id") is None:
            backup = self.directory / "result.pre-migration.json"
            if self._read(backup) is None:
                self.store.atomic_write(backup, self.store.read(self.result))
            data, _ = migrate_legacy(data)
            data["id"] = self.peer["id"]
        repaired = deepcopy(data)
        repaired, stats = repair_legacy_migration(repaired)
        if stats["messages_repaired"]:
            backup = self.directory / "result.pre-repair.json"
            if self._read(backup) is None:
                self.store.atomic_write(backup, self.store.read(self.result))
            data = repaired
        state = self._read(self.state_path)
        if state is None:
            state = {"peer": self.identity, "revisions": {"messages": {}, "topics": {}},
                     "boundary": deepcopy(data), "checkpoint_id": None}
        elif state["peer"] != self.identity:
            raise ValueError("MIRROR_INVALID")
        return data, state

    def _finish(self, pending):
        if pending["prev"] is not None:
            self.store.atomic_write(self.directory / "result.prev.json", _json(pending["prev"]))
        self.store.atomic_write(self.result, _json(pending["result"]))
        self.store.atomic_write(self.state_path, _json(pending["state"]))
        self.store.remove(self.journal)

    def commit(self, data, state, *, prev=None):
        pending = {"result": data, "state": state, "prev": prev}
        self.store.atomic_write(self.journal, _json(pending))
        self._finish(pending)

    @staticmethod
    def cursor(data):
        return last_message_id(data)

    def merge(self, data, state, event):
        """Return True if an orphan topic patch requires catch-up."""
        kind = event["kind"]
        name = "topics" if kind == "topic" else "messages"
        records = {r["id"]: r for r in data[name]}
        versions = state["revisions"][name]
        record = event.get("record")
        ids = event.get("ids", []) if kind == "delete" else [record["id"]]
        rev = event.get("revision") or revision(record or {})
        for mid in ids:
            if not _id(mid):
                raise ValueError("MIRROR_INVALID_EVENT")
            old = records.get(mid)
            old_rev = versions.get(str(mid), revision(old or {}))
            if list(rev) <= old_rev:
                continue
            if kind == "delete":
                if old is None:
                    old = {"id": mid, "type": "message", "text": "",
                           "date": "", "date_unixtime": str(int(rev[0]))}
                    records[mid] = old
                old["deleted"] = True
            elif kind == "topic":
                if old is None and not {"id", "title", "date", "date_unixtime", "icon_color"} <= record.keys():
                    return True
                records[mid] = {**(old or {}), **deepcopy(record)}
            elif old is None:
                records[mid] = deepcopy(record)
            else:
                # Keep snapshot's edit_history/media policy. Explicit event
                # revisions order multiple changes inside the same second.
                merge_edits([old], {mid: record}, {mid}, revision_ordered=kind == "edit")
                for key, value in record.items():
                    if key not in EDIT_TEXT_KEYS + EDIT_MEDIA_KEYS + ("edit_history",):
                        old[key] = deepcopy(value)
            versions[str(mid)] = list(rev)
        data[name] = sorted(records.values(), key=lambda r: r["id"])
        return False

    def merge_history(self, data, state, history, recent, cursor):
        needs_catchup = False
        for row in history:
            record = row["record"] if row.get("kind") == "topic" else row
            if record["id"] <= cursor:
                continue
            event = row if row.get("kind") == "topic" else {
                "kind": "new", "record": row, "revision": revision(row)}
            needs_catchup = self.merge(data, state, event) or needs_catchup
        history_boundary = self.cursor(data)
        message_ids = {m["id"] for m in data["messages"]}
        seen = set()
        for row in recent:
            record = row["record"] if row.get("kind") == "topic" else row
            seen.add(record["id"])
            if record["id"] > history_boundary:
                # The recent window cannot advance past the history boundary.
                continue
            if row.get("kind") != "topic" and record["id"] not in message_ids:
                continue
            event = row if row.get("kind") == "topic" else {
                "kind": "new", "record": row, "revision": revision(row)}
            needs_catchup = self.merge(data, state, event) or needs_catchup
        if seen:
            # Owner only promises reconciliation inside the returned window.
            # An empty window says nothing about deletions.
            floor = min(seen)
            missing = [m for m in data["messages"] if m["id"] >= floor and m["id"] not in seen]
            merge_edits(missing, {}, seen)
        return needs_catchup

    def checkpoint(self, data, state, request_id, updated_at):
        boundary = state["boundary"]
        prev = None
        if self.cursor(data) > self.cursor(boundary):
            prev = boundary
            state["boundary"] = deepcopy(data)
            state["checkpoint_id"] = str(uuid.uuid4())
        elif state["checkpoint_id"] is None:
            state["checkpoint_id"] = str(uuid.uuid4())
        result = {"destination_id": self.destination["destination_id"],
                  "cursor": self.cursor(data), "checkpoint_id": state["checkpoint_id"],
                  "updated_at": updated_at}
        # A crash after committing the pair but before status must not let the
        # same request move the boundary again when newer events have arrived.
        state["refreshes"] = {rid: reply for rid, reply in state.get("refreshes", {}).items()
                              if reply["updated_at"] >= updated_at - 7 * 86400}
        state["refreshes"][request_id] = result
        self.commit(data, state, prev=prev)
        return result


@contextmanager
def read_pair(directory):
    """Lock owner mirrors for deltas; legacy/direct mirrors need no new files."""
    lock_path = directory / ".telegram-owner.lock"
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        if (directory / ".telegram-owner-journal.json").exists():
            raise RuntimeError("Checkpoint не завершен; повторите refresh у owner")
        yield
    finally:
        os.close(fd)

