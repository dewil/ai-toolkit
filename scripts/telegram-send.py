#!/usr/bin/env python3
"""
Отправка сообщения в Telegram-чат проекта от имени пользователя.

Шлет текст в чат через ту же инфраструктуру, что telegram-snapshot.py:
общая авторизация (~/.config/telegram-snapshot/auth.json + .session) и
проектный конфиг .telegram-snapshot.json как справочник {label: chat_id}.
Адресат задается ТОЛЬКО по label из конфига - ни @username, ни телефона.
Чтобы написать новому адресату, сначала добавь его в .telegram-snapshot.json.

Личка и группа отправляются одинаково - client.send_message(entity, text):
личка это User-entity, группа - Channel/Chat, код не различает.

Защита от случайной отправки: без --send скрипт делает DRY-RUN - резолвит
адресата и печатает, что и куда уйдет, НО не отправляет. Реальная отправка -
только с флагом --send.

Текст уходит ДОСЛОВНО (parse_mode=None): markdown не парсится, символы
_ * ` в тексте не искажаются.

Запуск:
    # превью (ничего не отправляет):
    python3 scripts/telegram-send.py --to "с командой" --text "Привет"
    # реальная отправка:
    python3 scripts/telegram-send.py --to "с командой" --text "Привет" --send
    # многострочный текст из stdin:
    python3 scripts/telegram-send.py --to "с командой" --send <<'EOF'
    Первая строка
    Вторая строка
    EOF
    # в форумную тему:
    python3 scripts/telegram-send.py --to "бот" --topic 30127 --text "..." --send
    # ответом на сообщение (ответ попадет в тему этого сообщения):
    python3 scripts/telegram-send.py --to "бот" --reply-to 4821 --text "..." --send
    # файл-вложение (текст уходит подписью к файлу):
    python3 scripts/telegram-send.py --to "с командой" --file "путь/к/архиву.zip" --text "..." --send

Зависимости:
    pip3 install --user telethon
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

try:
    from telethon import TelegramClient
except ImportError:
    sys.stderr.write(
        "telethon не установлен. Поставь: pip3 install --user telethon\n"
    )
    sys.exit(2)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = Path.home() / ".config" / "telegram-snapshot"
AUTH_PATH = AUTH_DIR / "auth.json"
PROJECT_CONFIG_PATH = PROJECT_ROOT / ".telegram-snapshot.json"


def load_auth(account: str = "default") -> dict:
    """Конфиг одного аккаунта из auth.json.

    Два формата. Плоский (исторический): api_id/api_hash/session_name/proxy в
    корне - трактуется как единственный аккаунт "default". Новый: секция
    accounts {"<имя>": {...}} для нескольких номеров. Ключи верхнего уровня
    наследуются аккаунтом, если он их не переопределил - иначе при переезде на
    accounts молча потерялись бы общие api_id/api_hash и proxy.

    session_name по умолчанию равен имени аккаунта: у каждого аккаунта свой
    .session-файл, поэтому аккаунты не дерутся за одну сессию.

    Дефолт account="default" сохраняет контракт для telegram-send-one.py,
    который зовет load_auth() без аргументов.
    """
    if not AUTH_PATH.exists():
        sys.stderr.write(
            f"Нет общего конфига {AUTH_PATH}.\n"
            "Настрой авторизацию - см. скилл telegram-snapshot.\n"
        )
        sys.exit(2)
    with AUTH_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)

    accounts = raw.get("accounts")
    if accounts is not None and not isinstance(accounts, dict):
        sys.stderr.write(
            f"В {AUTH_PATH} поле accounts должно быть объектом вида "
            f"{{\"имя\": {{...}}}}\n"
        )
        sys.exit(2)
    if accounts:
        if account not in accounts:
            sys.stderr.write(
                f"В {AUTH_PATH} нет аккаунта \"{account}\". "
                f"Доступные: {', '.join(sorted(accounts))}\n"
            )
            sys.exit(2)
        bad = sorted(n for n, cfg in accounts.items() if not isinstance(cfg, dict))
        if bad:
            sys.stderr.write(
                f"В {AUTH_PATH} настройки аккаунта должны быть объектом; "
                f"не так у: {', '.join(bad)}\n"
            )
            sys.exit(2)
        # session_name НАМЕРЕННО не наследуется от верхнего уровня: конфиг, где
        # он был задан до появления accounts, посадил бы все аккаунты на один
        # .session - то есть на одну авторизованную сессию, и изоляции бы не было
        inherited = {
            k: v for k, v in raw.items() if k not in ("accounts", "session_name")
        }
        auth = {**inherited, **accounts[account]}
        auth.setdefault("session_name", account)

        sessions = {n: (cfg.get("session_name") or n) for n, cfg in accounts.items()}
        clash = sorted(
            n for n, s in sessions.items() if n != account and s == sessions[account]
        )
        if clash:
            sys.stderr.write(
                f"В {AUTH_PATH} аккаунт \"{account}\" делит session_name "
                f"\"{sessions[account]}\" с: {', '.join(clash)}. "
                f"У каждого аккаунта должен быть свой .session-файл.\n"
            )
            sys.exit(2)
    else:
        if account != "default":
            sys.stderr.write(
                f"В {AUTH_PATH} нет секции accounts - доступен только \"default\", "
                f"а запрошен \"{account}\".\n"
            )
            sys.exit(2)
        auth = dict(raw)
        auth.setdefault("session_name", "default")

    missing = [k for k in ("api_id", "api_hash") if not auth.get(k)]
    if missing:
        sys.stderr.write(
            f"В {AUTH_PATH} у аккаунта \"{account}\" не заполнены поля: {missing}\n"
        )
        sys.exit(2)
    return auth


def client_kwargs(auth: dict) -> dict:
    """Опциональный per-device прокси из auth.json: "proxy": "socks5://127.0.0.1:7890".

    Нужен там, где прямой доступ к Telegram API режется (RU-датацентры, DPI).
    Для socks-схем требуется пакет python-socks. Без поля proxy - прямое подключение.
    Копия хелпера из telegram-snapshot.py: скрипт намеренно самодостаточный.
    """
    proxy = auth.get("proxy")
    if not proxy:
        return {}
    from urllib.parse import urlparse
    u = urlparse(proxy)
    if not (u.scheme and u.hostname and u.port):
        sys.stderr.write(f"Некорректный proxy в {AUTH_PATH}: {proxy!r} (жду scheme://host:port)\n")
        sys.exit(2)
    return {"proxy": (u.scheme, u.hostname, u.port)}


def chat_entry(value) -> dict:
    """Нормализует значение из chats к {"id": int, "topic_id": int|None}.

    Короткая форма "label": <id> и расширенная "label": {"id", "topic_id",
    "account"}. topic_id (если задан) используется как тема по умолчанию при
    отправке - сообщение уйдет в эту форумную тему, если --topic не задан явно.

    account - имя аккаунта из auth.json (по умолчанию "default"): сообщение
    уходит от того аккаунта, которому принадлежит чат. Ключ необязательный,
    поэтому старые конфиги читаются без изменений.
    """
    if isinstance(value, dict):
        if "id" not in value:
            raise ValueError("в расширенной записи чата нет поля id")
        topic = value.get("topic_id")
        return {
            "id": int(value["id"]),
            "topic_id": int(topic) if topic is not None else None,
            "account": str(value.get("account") or "default"),
        }
    return {"id": int(value), "topic_id": None, "account": "default"}


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
    return cfg


async def resolve_entity(client: TelegramClient, chat_id: int, dialog_entities: dict):
    """Возвращает entity чата по unmarked-id.

    Резолвим строго через карту диалогов (dialog_entities), а НЕ через
    client.get_entity(chat_id): в некоторых сессиях локальный entity-cache
    оказывается битым и get_entity на голый int возвращает чужой чат с тем
    же магическим id. Карта строится из свежих серверных entity в iter_dialogs -
    у них корректный access_hash. get_entity оставлен только как фолбэк для
    чатов, которых нет в списке диалогов (архивные/скрытые).
    """
    entity = dialog_entities.get(chat_id)
    if entity is not None:
        return entity
    return await client.get_entity(chat_id)


def entity_title(entity, fallback) -> str:
    """Человекочитаемое имя чата для превью/подтверждения."""
    if getattr(entity, "title", None):
        return entity.title
    parts = [getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or ""]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    if getattr(entity, "username", None):
        return f"@{entity.username}"
    return str(fallback)


def build_reply_to(topic_id, reply_id):
    """reply_to (int | None) для high-level client.send_message.

    Отдаем именно id - Telethon сам обернет его в InputReplyToMessage:
    - reply_id задан -> ответ на это сообщение; если оно внутри форумной
      темы, ответ попадет в ту же тему (тред выводится из отвечаемого);
    - иначе topic_id -> постинг в форумную тему (reply на корень темы);
    - иначе None.

    Передавать готовый InputReplyToMessage в send_message нельзя: reply_to
    проходит через utils.get_message_id(), который принимает только
    int/Message и падает TypeError на InputReplyToMessage. Отдельный
    top_msg_id high-level API не поддерживает - для "ответа в конкретной
    теме" достаточно reply на сообщение внутри этой темы.
    """
    if reply_id is not None:
        return reply_id
    if topic_id is not None:
        return topic_id
    return None


def read_text(args, allow_empty: bool = False) -> str:
    """Текст сообщения из --text или stdin.

    allow_empty=True нужен для отправки файла без подписи (--file): Telegram
    разрешает документ с пустым caption. Дефолт False сохраняет прежний
    контракт - его использует telegram-send-one.py, вызывающий read_text(args).
    """
    if args.text is not None:
        text = args.text
    else:
        if sys.stdin.isatty():
            if allow_empty:
                return ""
            sys.stderr.write("Нет текста: задай --text или передай его через stdin.\n")
            sys.exit(2)
        text = sys.stdin.read()
    text = text.rstrip("\n")
    if not text.strip():
        if allow_empty:
            return ""
        sys.stderr.write("Пустой текст сообщения (--text или stdin).\n")
        sys.exit(2)
    return text


async def amain(args) -> int:
    project_cfg = load_project_config()
    chats = project_cfg["chats"]

    if args.to not in chats:
        available = "\n".join(f"  - {label}" for label in chats)
        sys.stderr.write(
            f"Нет чата с label \"{args.to}\" в {PROJECT_CONFIG_PATH}.\n"
            f"Доступные labels:\n{available}\n"
            f"Чтобы написать новому адресату - сначала добавь его в .telegram-snapshot.json.\n"
        )
        return 2

    entry = chats[args.to]
    chat_id = entry["id"]
    topic_id = args.topic if args.topic is not None else entry["topic_id"]
    reply_id = args.reply_to

    text = read_text(args, allow_empty=bool(args.file))

    auth = load_auth(entry["account"])
    session_path = str(AUTH_DIR / auth["session_name"])
    client = TelegramClient(session_path, auth["api_id"], auth["api_hash"], **client_kwargs(auth))

    # Не client.start(): на неготовой/отозванной сессии он уходит в
    # интерактивный логин (ввод телефона/кода) и в автоматизации зависает.
    # Авторизация - предусловие, настраивается скиллом telegram-snapshot.
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        sys.stderr.write(
            f"Сессия не авторизована ({session_path}.session).\n"
            f"Настрой авторизацию - см. скилл telegram-snapshot.\n"
        )
        return 2

    try:
        # Прогрев: строим карту {unmarked_id -> свежий entity}. Нужна и для
        # резолва (get_entity на голый int на свежей сессии трактует его как
        # PeerUser), и как авторитетный источник entity вместо битого кеша.
        dialog_entities: dict = {}
        async for d in client.iter_dialogs():
            eid = getattr(d.entity, "id", None)
            if eid is not None:
                dialog_entities[eid] = d.entity

        try:
            entity = await resolve_entity(client, chat_id, dialog_entities)
        except Exception as exc:
            sys.stderr.write(f"Не удалось найти чат \"{args.to}\" (id {chat_id}): {exc}\n")
            return 1

        title = entity_title(entity, chat_id)
        kind = type(entity).__name__
        lines = text.split("\n")

        file_path = None
        if args.file:
            file_path = Path(args.file).expanduser().resolve()
            if not file_path.is_file():
                sys.stderr.write(f"Файл не найден: {file_path}\n")
                return 2

        if not args.send:
            print("DRY-RUN (без --send отправка не сделана)")
            print(f"  -> \"{title}\" ({kind}, id={chat_id})")
            # от какого аккаунта уйдет - часть гейта: при нескольких номерах
            # ошибиться отправителем так же легко, как чатом
            print(f"  от аккаунта: {entry['account']}")
            print(f"  тема: {topic_id if topic_id is not None else '-'}   ответ на: {reply_id if reply_id is not None else '-'}")
            if file_path:
                # полный резолвленный путь и точный размер: dry-run - это
                # предохранитель "тот ли файл", по одному имени его не проверить
                print(f"  файл: {file_path} ({file_path.stat().st_size} байт)")
            if text:
                print(f"  {'подпись' if file_path else 'текст'} ({len(lines)} строк):")
                for ln in lines:
                    print(f"  | {ln}")
            else:
                print("  подпись: нет (файл уйдет без текста)")
            return 0

        reply_to = build_reply_to(topic_id, reply_id)
        if file_path:
            # файл с подписью-текстом; force_document - имя и расширение как есть
            sent = await client.send_file(
                entity, str(file_path), caption=text, reply_to=reply_to,
                force_document=True, parse_mode=None,
            )
        else:
            sent = await client.send_message(entity, text, reply_to=reply_to, parse_mode=None)
        print(f"OK: отправлено в \"{title}\" (id сообщения {sent.id})")
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Отправка сообщения в Telegram-чат проекта от имени пользователя."
    )
    parser.add_argument("--to", required=True, help="label чата из .telegram-snapshot.json")
    parser.add_argument("--text", help="текст сообщения; если опущен - читается из stdin")
    parser.add_argument("--file", help="путь к файлу-вложению; текст уходит подписью к нему")
    parser.add_argument("--send", action="store_true", help="реально отправить (без флага - dry-run)")
    parser.add_argument("--topic", type=int, help="id корня форумной темы (по умолчанию - из конфига, если задан)")
    parser.add_argument("--reply-to", type=int, dest="reply_to", help="id сообщения, на которое отвечаем")
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
