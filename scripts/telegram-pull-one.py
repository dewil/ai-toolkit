#!/usr/bin/env python3
"""
Снапшот одного Telegram-чата в произвольную папку.

В отличие от telegram-snapshot.py (который тянет весь список чатов из
.telegram-snapshot.json в общий chats_root), этот драйвер тянет ОДИН чат
по id в указанный путь. Для сценариев, где адресат вне общего конфига
(разовый чат, клиент трека поддержки): чат живет в своей папке, а не в
общем зеркале, и в ежедневные pull/дельты не попадает.

Вся логика (резолв entity, парсинг сообщений, инкремент, атомарная запись,
формат TG Desktop) переиспользуется из telegram-snapshot.py через импорт -
здесь только тонкая обертка: выбрать чат, задать путь, опционально сверить
username, поправить шапку личного чата.

Запуск:
    python3 scripts/telegram-pull-one.py <chat_id> <out_path> [expected_username]

Пример:
    python3 scripts/telegram-pull-one.py 123456789 "support/@somebody/чат" somebody

out_path - относительный путь от корня проекта; результат пишется в
<out_path>/result.json. Повторный запуск делает инкрементальный pull.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("tgs", _HERE / "telegram-snapshot.py")
tgs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tgs)


def display_name(entity) -> str:
    parts = [getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or ""]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    return getattr(entity, "title", None) or getattr(entity, "username", None) or ""


async def amain(chat_id: int, out_path: str, expected_username: str | None) -> int:
    auth = tgs.load_auth()
    session_path = str(tgs.AUTH_DIR / auth["session_name"])

    client = tgs.TelegramClient(session_path, auth["api_id"], auth["api_hash"])
    await client.start()

    # Карта диалогов - авторитетный источник entity (см. resolve_entity в
    # telegram-snapshot.py: get_entity на голый int бывает резолвит чужой чат).
    dialog_entities: dict = {}
    async for d in client.iter_dialogs():
        eid = getattr(d.entity, "id", None)
        if eid is not None:
            dialog_entities[eid] = d.entity

    entity = await tgs.resolve_entity(client, chat_id, dialog_entities)
    uname = getattr(entity, "username", None)
    print(f"resolved {chat_id}: {display_name(entity)!r} @{uname} (type {type(entity).__name__})")

    if expected_username and uname and uname.lower() != expected_username.lower():
        sys.stderr.write(
            f"!! username не совпал: ожидали @{expected_username}, получили @{uname}. "
            f"Прерываю, чтобы не выкачать чужой чат.\n"
        )
        await client.disconnect()
        return 2

    n, last_date = await tgs.process_chat(
        client, tgs.PROJECT_ROOT, out_path, chat_id, dialog_entities
    )

    await client.disconnect()

    # Шапку личного чата TG Desktop именует контактом, а не путем-лейблом.
    # process_chat при bootstrap кладет name = title|label; для User title нет,
    # поэтому правим name на имя контакта и дописываем username.
    result_path = tgs.PROJECT_ROOT / out_path / "result.json"
    if result_path.exists():
        data = tgs.load_existing(result_path)
        new_name = display_name(entity)
        changed = False
        if new_name and data.get("name") != new_name:
            data["name"] = new_name
            changed = True
        if uname and data.get("username") != uname:
            data["username"] = uname
            changed = True
        if changed:
            tgs.atomic_write_json(result_path, data)

    print(f"\nOK: +{n} сообщений, последнее {last_date}")
    print(f"    {result_path}")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        sys.stderr.write(
            "usage: telegram-pull-one.py <chat_id> <out_path> [expected_username]\n"
        )
        return 2
    chat_id = int(sys.argv[1])
    out_path = sys.argv[2]
    expected_username = sys.argv[3] if len(sys.argv) > 3 else None
    return asyncio.run(amain(chat_id, out_path, expected_username))


if __name__ == "__main__":
    sys.exit(main())
