#!/usr/bin/env python3
"""
Отправка сообщения в ОДИН Telegram-чат по id, минуя .telegram-snapshot.json.

Симметрия к telegram-pull-one.py: для сценариев, где адресат вне общего
конфига (разовый чат, клиент трека поддержки), id задается прямо в командной
строке. Вся логика (авторизация, прогрев диалогов, резолв entity, dry-run,
отправка) переиспользуется из telegram-send.py через импорт - здесь только
тонкая обертка: взять id, опционально сверить username, отправить.

Гейт отправки тот же, что в telegram-send.py: без --send печатается DRY-RUN и
ничего не уходит; отправка только с --send.

Запуск:
    python3 scripts/telegram-send-one.py <chat_id> [expected_username] --text "..."
    python3 scripts/telegram-send-one.py <chat_id> [expected_username] --send <<'EOF'
    многострочный текст
    EOF

Пример:
    python3 scripts/telegram-send-one.py 123456789 somebody --text "привет" --send
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("tgsend", _HERE / "telegram-send.py")
tgs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tgs)


SAVED = "me"  # Telegram-адрес собственного чата ("Избранное")


async def amain(args) -> int:
    # "me" - собственное "Избранное": туда уходят отчеты самому себе, и туда не
    # применимы гейты про живого получателя (outbound-timing.md: "на том конце
    # нет человека" - точнее, там сам отправитель)
    chat_id = SAVED if str(args.chat_id).strip().lower() == SAVED else int(args.chat_id)
    # None и "" различаются: --file "$VAR" с пустой переменной - это заданный
    # файловый режим, а не его отсутствие; молча уйти текстом было бы враньем
    if args.file is not None and not args.file:
        sys.stderr.write("--file задан пустой строкой - проверь переменную с путем.\n")
        return 2
    text = tgs.read_text(args, allow_empty=args.file is not None)

    file_path = None
    if args.file:
        # проверка ДО соединения: падать раньше, чем занимать общую сессию
        file_path = Path(args.file).expanduser().resolve()
        if not file_path.is_file():
            sys.stderr.write(f"Файл не найден: {file_path}\n")
            return 2
        if text and len(text) > 1024:
            # лимит Telegram на подпись к файлу; длинный текст + файл - это
            # два сообщения (сначала --text без файла, потом файл с короткой
            # подписью), резать сами не пытаемся
            sys.stderr.write(
                f"Подпись к файлу длиннее лимита Telegram в 1024 символа "
                f"({len(text)}). Отправь текст и файл двумя сообщениями.\n"
            )
            return 2

    auth = tgs.load_auth(args.account)
    session_path = str(tgs.AUTH_DIR / auth["session_name"])
    client = tgs.TelegramClient(session_path, auth["api_id"], auth["api_hash"], **tgs.client_kwargs(auth))

    await tgs.connect_with_retry(client)
    if not await client.is_user_authorized():
        await tgs.disconnect_quietly(client)
        sys.stderr.write(
            f"Сессия не авторизована ({session_path}.session).\n"
            f"Настрой авторизацию - см. скилл telegram-snapshot.\n"
        )
        return 2

    try:
        # Прогрев диалогов (как в telegram-snapshot/-send): карта свежих entity,
        # иначе resolve голого int на свежей сессии трактует его как PeerUser.
        dialog_entities: dict = {}
        async for d in client.iter_dialogs():
            eid = getattr(d.entity, "id", None)
            if eid is not None:
                dialog_entities[eid] = d.entity

        try:
            entity = (await client.get_me()) if chat_id == SAVED else await tgs.resolve_entity(
                client, chat_id, dialog_entities
            )
        except Exception as exc:
            sys.stderr.write(f"Не удалось найти чат id {chat_id}: {exc}\n")
            return 1

        # Опциональная сверка username - страховка от отправки не тому адресату.
        actual_username = (getattr(entity, "username", "") or "")
        if args.username:
            want = args.username.lstrip("@").lower()
            if actual_username.lower() != want:
                sys.stderr.write(
                    f"username чата id {chat_id} = @{actual_username or '?'}, "
                    f"ожидали @{want}. Стоп, ничего не отправлено.\n"
                )
                return 1

        title = tgs.entity_title(entity, chat_id)
        kind = type(entity).__name__
        lines = text.split("\n")

        if not args.send:
            print("DRY-RUN (без --send отправка не сделана)")
            print(f"  -> \"{title}\" (@{actual_username or '?'}, {kind}, id={chat_id})")
            print(f"  ответ на: {args.reply_to if args.reply_to is not None else '-'}   формат: {'html' if args.html else 'сырой текст'}   аккаунт: {args.account}   звук: {'нет' if args.silent else 'да'}")
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
            wait, required = tgs.pace_check(args.account, entity)
            if wait > 0:
                print(f"  темп: рано - после прошлого сообщения нужно {required:.0f} сек, осталось {wait:.0f}")
            return 0

        rc = tgs.pace_guard(args.account, entity, args.no_pace_check)
        if rc:
            return rc

        reply_to = tgs.build_reply_to(args.topic, args.reply_to)
        parse_mode = "html" if args.html else None
        if file_path:
            # voice_note - проигрываемое голосовое; force_document отправил бы
            # тот же ogg вложением, которое в дороге не послушать
            sent = await client.send_file(
                entity, str(file_path), caption=text, reply_to=reply_to,
                force_document=not args.voice, voice_note=args.voice,
                parse_mode=parse_mode, silent=args.silent,
            )
        else:
            sent = await client.send_message(
                entity, text, reply_to=reply_to, parse_mode=parse_mode, silent=args.silent,
            )
        tgs.pace_record(args.account, entity, len(text or ""))
        print(f"OK: отправлено в \"{title}\" (id сообщения {sent.id})")
        return 0
    finally:
        await tgs.disconnect_quietly(client)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Отправка сообщения в один Telegram-чат по id, минуя .telegram-snapshot.json."
    )
    parser.add_argument("chat_id", help="числовой id чата (как в telegram-pull-one.py) "
                                        "или \"me\" - собственное Избранное")
    parser.add_argument("username", nargs="?", default=None,
                        help="ожидаемый username для сверки (без @); при несовпадении - стоп")
    parser.add_argument("--text", help="текст сообщения; если опущен - читается из stdin")
    parser.add_argument("--file", help="путь к файлу-вложению; текст уходит подписью к нему "
                                       "(лимит Telegram - 1024 символа, длиннее - двумя сообщениями)")
    parser.add_argument("--voice", action="store_true",
                        help="отправить файл голосовым сообщением (ogg/opus), а не вложением. "
                             "Телеграм проигрывает такое прямо в ленте - нужно, когда получатель "
                             "слушает на ходу и читать не может")
    parser.add_argument("--send", action="store_true", help="реально отправить (без флага - dry-run)")
    parser.add_argument("--no-pace-check", action="store_true", dest="no_pace_check",
                        help="не проверять паузу после предыдущего сообщения в этот чат "
                             "(см. тот же флаг в telegram-send.py)")
    parser.add_argument("--topic", type=int, help="id корня форумной темы (для форум-чатов)")
    parser.add_argument("--reply-to", type=int, dest="reply_to",
                        help="id сообщения, на которое отвечаем")
    parser.add_argument("--account", default="default",
                        help="имя аккаунта из auth.json (по умолчанию default)")
    parser.add_argument("--silent", action="store_true",
                        help="отправить без звука (получателю придет беззвучное уведомление). "
                             "Нужно, когда отправляем вне рабочего окна получателя - см. "
                             "rules/outbound-timing.md: звук ночью выдает автомат, но метка "
                             "времени остается видимой, поэтому это не обход правила")
    parser.add_argument("--html", action="store_true",
                        help="слать как HTML (жирный/код/ссылки). Без флага - сырой текст. "
                             "HTML, а не MarkdownV2: тот требует экранировать точки/дефисы/скобки, "
                             "на русском тексте это грабли. Спецсимволы < > & в HTML-режиме экранируй сам.")
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
