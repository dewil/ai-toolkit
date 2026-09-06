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

Инкремент по id не видит правок и удалений старых сообщений, поэтому на
каждом прогоне дополнительно перечитываются последние EDIT_WINDOW (50,
переменная TELEGRAM_SNAPSHOT_EDIT_WINDOW) сообщений: отредактированное
обновляется, прежний текст уходит в `edit_history`, удаленное помечается
`deleted: true` (из зеркала не стирается). Счетчики правок и удалений
печатаются всегда, в том числе нулевые.

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
    chats_root может быть абсолютным - так зеркала уводятся из синкаемой
    папки; по умолчанию берется хранилище вне синка (TELEGRAM_SNAPSHOT_STORE,
    дефолт ~/.local/share/telegram-snapshot/chats). Необязательные ключи
    media_cache и media_ttl_hours перекрывают путь и TTL медиа-кэша.

Запуск:
    python3 scripts/telegram-snapshot.py

Зависимости:
    pip3 install --user telethon
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import os
import shutil
import sqlite3
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

# Повтор подключения, когда общую .session держит другой процесс (см.
# connect_with_retry). Чужой снапшот обычно отпускает сессию за 30-60 секунд.
LOCK_ATTEMPTS = 5
LOCK_DELAY = 15

def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


# Зеркала чатов живут ВНЕ синкаемого дерева проекта: папка проекта едет на все
# устройства пользователя и в облачные бэкапы, а зеркало личной переписки -
# сотни мегабайт чужих сообщений (docs-maintenance.md, "Технические артефакты
# в синкаемой папке"). Хранилище задается переменной окружения на устройстве,
# дефолт - XDG-каталог данных.
MIRROR_STORE = _env_path(
    "TELEGRAM_SNAPSHOT_STORE", Path.home() / ".local" / "share" / "telegram-snapshot" / "chats"
)
if not MIRROR_STORE.is_absolute():
    sys.exit(
        f"TELEGRAM_SNAPSHOT_STORE={str(MIRROR_STORE)!r}: нужен абсолютный путь. "
        "Относительный считается от рабочего каталога и легко превращает "
        "хранилище вне синка в папку внутри проекта"
    )
LEGACY_CHATS_ROOT = "Встречи/чаты"

# Вложения качаются в локальный НЕсинкаемый кэш (не в vault/проект - там бывают
# секреты клиентов, а папки проекта синкаются в облако). result.json остается
# текстовым (медиа туда НЕ пишем); корреляция сообщение<->файл по <msg_id> в
# имени файла кэша + метаданные file_name/mime в самой записи. TTL: на каждом
# запуске чистим файлы старше него. Оба значения перекрываются переменными
# окружения и ключами проектного конфига (media_cache, media_ttl_hours):
# копий скрипта на машине много, локальная правка констант не переживет /canon.
MEDIA_CACHE = _env_path("TELEGRAM_SNAPSHOT_MEDIA", Path.home() / ".cache" / "telegram-snapshot" / "media")
MEDIA_TTL_HOURS = int(os.environ.get("TELEGRAM_SNAPSHOT_MEDIA_TTL", "168"))
MEDIA_CACHE_DEFAULT = MEDIA_CACHE
MEDIA_TTL_DEFAULT = MEDIA_TTL_HOURS
# Сколько последних сообщений перечитывать на каждом прогоне ради правок и
# удалений: инкремент по id их не видит (см. fetch_recent / merge_edits).
EDIT_WINDOW = int(os.environ.get("TELEGRAM_SNAPSHOT_EDIT_WINDOW", "50"))
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


def load_auth(account: str = "default") -> dict:
    """Конфиг одного аккаунта из auth.json.

    Два формата. Плоский (исторический): api_id/api_hash/session_name/proxy в
    корне - трактуется как единственный аккаунт "default". Новый: секция
    accounts {"<имя>": {...}} для нескольких номеров. Ключи верхнего уровня
    наследуются аккаунтом, если он их не переопределил - иначе при переезде на
    accounts молча потерялись бы общие api_id/api_hash и proxy.

    session_name по умолчанию равен имени аккаунта: у каждого аккаунта свой
    .session-файл, поэтому аккаунты не дерутся за одну сессию.

    Дефолт account="default" сохраняет контракт для telegram-pull-one.py и
    telegram-send-one.py, которые зовут load_auth() без аргументов.
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


def external_cancel() -> bool:
    """True, если отменяют саму текущую таску (Ctrl+C через Runner и т.п.) -
    такую отмену глотать нельзя. Отличается по task.cancelling() (py3.11+);
    на py<3.11, где cancelling нет, консервативно считаем отмену внешней."""
    task = asyncio.current_task()
    return task is None or not hasattr(task, "cancelling") or bool(task.cancelling())


async def disconnect_quietly(client) -> None:
    """Best-effort закрытие клиента: своей ошибкой ничего не рвет.

    telethon при отключении пишет состояние в ту же sqlite-сессию
    (_save_states_and_entities), поэтому пока сессию держит другой процесс,
    disconnect падает тем же "database is locked". В cleanup это опаснее самой
    блокировки: в finally оно подменяет исходное исключение своим, а после
    успешной отправки превращает доставленное сообщение в ненулевой код возврата.
    """
    try:
        await client.disconnect()
    except asyncio.CancelledError:
        # CancelledError - BaseException: без этой ветки отмена футур telethon
        # в cleanup рвала бы finally и глушила итог прогона (ЧАСТИЧНО/OK).
        # Внешнюю отмену самой таски (Ctrl+C) не глотаем - см. amain.
        if external_cancel():
            raise
        sys.stderr.write("disconnect не отработал (CancelledError)\n")
    except Exception as exc:
        sys.stderr.write(f"disconnect не отработал ({type(exc).__name__}: {exc})\n")


async def connect_with_retry(
    client, *, interactive: bool = False, attempts: int = LOCK_ATTEMPTS, delay: float = LOCK_DELAY
):
    """Подключение с повтором, если общую .session держит другой процесс.

    Авторизация одна на устройство, а .session - это sqlite: пока с ней работает
    скрипт другого проекта, наш connect()/start() падает изнутри telethon с
    sqlite3.OperationalError: database is locked. Чужой процесс дорабатывает сам
    и отпускает сессию, поэтому лечится ожиданием, а не починкой (убивать процесс
    или удалять .session нельзя - см. скилл telegram-snapshot).

    interactive=True - путь первого логина (client.start() спросит номер и код);
    иначе client.connect() без интерактива. Повтор в этом режиме переигрывает
    весь start(), включая логин, - на неавторизованной сессии ввод спросят снова.

    Оборачивать ТОЛЬКО подключение. Отправку сообщения оборачивать нельзя:
    повтор после успешного send_message даст получателю дубль.
    """
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await (client.start() if interactive else client.connect())
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower() or attempt == attempts:
                raise
            # Соединение могло уже подняться (сессия падает при записи после
            # хендшейка) - иначе следующая попытка оставит сокет висеть.
            # Закрываем через disconnect_quietly: на живом локе сам disconnect
            # падает тем же locked и без глушения оборвал бы ретрай
            await disconnect_quietly(client)
            sys.stderr.write(
                f".session занята другим процессом (попытка {attempt}/{attempts}), "
                f"повтор через {delay:g}с\n"
            )
            await asyncio.sleep(delay)


def chat_entry(value) -> dict:
    """Нормализует значение из chats к {"id", "topic_id", "account"}.

    Поддерживаются две формы записи чата:
      - короткая:  "label": 4715985727
      - расширенная: "label": {"id": 4715985727, "topic_id": 123, "account": "cv"}

    topic_id используется telegram-deltas.py для отбора сообщений только
    нужной форумной темы (например, топика бота). На саму выкачку не
    влияет - snapshot всегда тянет чат целиком.

    account - имя аккаунта из auth.json (по умолчанию "default"): чат тянется
    той сессией, которой он принадлежит. Ключ необязательный, поэтому старые
    конфиги читаются без изменений.

    dest - путь папки чата от корня проекта, для чатов подпроектов
    (клиент-зонтик): зеркало живет в папке подпроекта, а не в общем
    chats_root/<label>. Без dest поведение прежнее.

    media=false - тянуть чат без вложений (только текст и метаданные). Нужно
    для чатов с потоком картинок (мем-флудилки, новостные каналы): полезной
    нагрузки в них нет, а скачивание тысяч файлов роняет прогон. По умолчанию
    true - старые конфиги читаются без изменений.
    """
    if isinstance(value, dict):
        if "id" not in value:
            raise ValueError("в расширенной записи чата нет поля id")
        topic = value.get("topic_id")
        media = value.get("media", True)
        if not isinstance(media, bool):
            # Строго bool: truthiness превращал бы "false" из JSON в True -
            # защита от потока вложений молча не работала бы.
            raise ValueError(f"поле media должно быть true/false, получено {media!r}")
        return {
            "id": int(value["id"]),
            # int обязателен: topic_id сравнивается с числовым полем сообщения,
            # и строковый "42" из конфига молча не совпал бы ни с чем
            "topic_id": int(topic) if topic is not None else None,
            "account": str(value.get("account") or "default"),
            "dest": str(value["dest"]) if value.get("dest") else None,
            "media": media,
        }
    return {"id": int(value), "topic_id": None, "account": "default", "dest": None, "media": True}


def resolve_dest(dest: str) -> Path:
    """dest из конфига -> абсолютный путь.

    Абсолютный путь разрешен и является штатным способом увести зеркало из
    синкаемого дерева. Относительный по-прежнему обязан остаться внутри
    проекта: опечатка вида "../..." молча писала бы зеркало мимо и проекта,
    и хранилища. Абсолютный путь - осознанное указание, а не промах пальца.
    """
    p = Path(dest).expanduser()
    if p.is_absolute():
        return p.resolve()
    resolved = (PROJECT_ROOT / p).resolve()
    if not resolved.is_relative_to(PROJECT_ROOT.resolve()):
        sys.exit(f"dest {dest!r}: относительный путь выходит за пределы проекта")
    return resolved


def resolve_label_target(chats_root: Path, label: str) -> Path:
    """chats_root/<label> с проверкой границ.

    Label подставляется в путь как есть, а chats_root теперь бывает где угодно:
    абсолютный label или label с ".." увел бы зеркало мимо хранилища молча.
    """
    target = (chats_root / label).resolve()
    root = chats_root.resolve()
    if not target.is_relative_to(root):
        sys.exit(f"label {label!r}: выходит за пределы chats_root ({chats_root})")
    if target == root:
        sys.exit(f"label {label!r}: схлопывается в сам chats_root - зеркало легло бы в корень хранилища")
    return target


def check_unique_targets(chats: dict, chats_root: Path) -> None:
    """Целевые папки всех чатов обязаны быть разными: два чата в одном
    result.json (одинаковые dest, или dest поверх chats_root/<чужой label>)
    молча смешивали бы истории и теряли сообщения по чужому max id.

    Ключ сравнения - casefold: на case-insensitive ФС (APFS) "Client/X" и
    "Client/x" - одна физическая папка; на case-sensitive такие пути-двойники
    все равно патология конфига, строгость дешевле детекта ФС."""
    targets: dict[str, str] = {}
    for label, entry in chats.items():
        tgt = resolve_dest(entry["dest"]) if entry.get("dest") else resolve_label_target(chats_root, label)
        warn_if_inside_project(tgt, f"зеркало чата {label!r}")
        key = str(tgt).casefold()
        if key in targets:
            sys.exit(
                f"чаты {targets[key]!r} и {label!r} указывают в одну папку "
                f"{tgt} - разведите dest в .telegram-snapshot.json"
            )
        targets[key] = label


def ensure_result_belongs_to(data: dict, chat_id: int, result_path: Path) -> None:
    """Зеркало принадлежит одному чату: инкремент от чужого max id молча
    пропускал бы историю (или дописывал сообщения под чужой шапкой)."""
    existing_id = data.get("id")
    if existing_id is not None and int(existing_id) != int(chat_id):
        raise RuntimeError(
            f"в {result_path} лежит зеркало другого чата "
            f"(id {existing_id}, ожидали {chat_id}) - проверьте dest/label"
        )


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
    if "chats_root" not in cfg:
        cfg["chats_root"] = default_chats_root()
    apply_media_settings(cfg)
    return cfg


def project_store_slug() -> str:
    """Имя папки проекта плюс хвост хеша полного пути.

    Одного basename недостаточно: /work/client-a/app и /work/client-b/app дали
    бы одно хранилище, снапшот второго уперся бы в чужой id, а дельты id не
    проверяют - и показали бы переписку одного клиента как чат другого.
    """
    root = PROJECT_ROOT.resolve()
    return f"{root.name}-{hashlib.sha1(str(root).encode('utf-8')).hexdigest()[:8]}"


def legacy_has_mirrors() -> bool:
    """Legacy-папка перехватывает дефолт, только если в ней ЕСТЬ зеркала.

    Проверять существование мало: пустую папку создает и синк, и сам
    пользователь, и тогда прогон уводит выкачку обратно в синкаемое дерево -
    ровно то, от чего уходим.
    """
    legacy = PROJECT_ROOT / LEGACY_CHATS_ROOT
    if not legacy.is_dir():
        return False
    # ровно те глубины, на которых зеркала и лежат: <root>/<label>/result.json и
    # <root>/<группа>/<label>/result.json. Полный rglob стоил бы обхода всего
    # дерева и считал бы зеркалом любой result.json в архивной подпапке
    return any(legacy.glob("*/result.json")) or any(legacy.glob("*/*/result.json"))


def warn_if_inside_project(path: Path, what: str) -> None:
    """Зеркало внутри проекта - не отказ, но и не молчание."""
    if path.resolve().is_relative_to(PROJECT_ROOT.resolve()):
        sys.stderr.write(
            f"внимание: {what} пишется внутрь проекта ({path}) - если папка проекта синкается,\n"
            "выгрузка уедет на все устройства и в бэкапы, и исключение задним числом этого не отменит\n"
            '(docs-maintenance.md, "Технические артефакты в синкаемой папке").\n'
        )


def default_chats_root() -> str:
    """Куда писать зеркала, если проект не сказал явно.

    Дефолт - хранилище вне синка. Но у проекта, заведенного до этого
    изменения, зеркала уже лежат в `Встречи/чаты`: молча сменив путь, скрипт
    начал бы качать историю заново в другое место, а старая копия осталась бы
    в синке - ровно то, от чего уходим. Поэтому существующая legacy-папка
    выигрывает у дефолта, а пользователю печатается, как перенести.
    """
    legacy = PROJECT_ROOT / LEGACY_CHATS_ROOT
    if legacy_has_mirrors():
        sys.stderr.write(
            f"зеркала лежат в синкаемой папке проекта ({legacy}).\n"
            f"Перенести: mv {str(legacy)!r} {str(MIRROR_STORE / project_store_slug())!r} и прописать\n"
            f'  "chats_root": "{MIRROR_STORE / project_store_slug()}"\n'
            f"в {PROJECT_CONFIG_PATH}. Пока читаю старый путь.\n"
        )
        return LEGACY_CHATS_ROOT
    return str(MIRROR_STORE / project_store_slug())


def apply_media_settings(cfg: dict) -> None:
    """Проектный конфиг перекрывает переменные окружения и дефолты.

    Значения валидируются, потому что кэш чистится РЕКУРСИВНО по TTL: пара
    media_cache="~" + media_ttl_hours=-1 сделала бы из проектного конфига
    примитив удаления всего домашнего каталога (cutoff уезжает в будущее, под
    "старше TTL" попадает каждый файл). Сброс к дефолтам обязателен: второй
    конфиг в том же процессе иначе унаследовал бы путь от первого.
    """
    global MEDIA_CACHE, MEDIA_TTL_HOURS
    MEDIA_CACHE, MEDIA_TTL_HOURS = MEDIA_CACHE_DEFAULT, MEDIA_TTL_DEFAULT
    raw = cfg.get("media_cache")
    if raw:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            sys.exit(f"media_cache {str(raw)!r}: нужен абсолютный путь (относительный зависит от cwd)")
        MEDIA_CACHE = validated_cache_dir(path, "media_cache")
    ttl = cfg.get("media_ttl_hours")
    if ttl is not None:
        if isinstance(ttl, bool):
            sys.exit(f"media_ttl_hours: ожидалось целое число часов, получено {ttl!r}")
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            sys.exit(f"media_ttl_hours: ожидалось число, получено {cfg['media_ttl_hours']!r}")
        if ttl < 1:
            sys.exit(
                f"media_ttl_hours={ttl}: нужно целое >= 1. Кэш чистится рекурсивно, "
                "а TTL <= 0 сдвигает границу в будущее и удаляет все содержимое"
            )
        MEDIA_TTL_HOURS = ttl


def validated_cache_dir(path: Path, what: str) -> Path:
    """Каталог, который можно рекурсивно чистить, не унеся чужое."""
    resolved = path.resolve()
    home = Path.home().resolve()
    if resolved == Path(resolved.anchor) or resolved == home or home.is_relative_to(resolved):
        sys.exit(
            f"{what} {str(path)!r}: нельзя указывать корень ФС, домашний каталог или его родителя - "
            "содержимое этой папки чистится по TTL рекурсивно"
        )
    if resolved.is_relative_to(PROJECT_ROOT.resolve()):
        sys.exit(f"{what} {str(path)!r}: внутри проекта - вложения уедут в синк, укажите путь вне его")
    allowed = (MIRROR_STORE.resolve(), (Path.home() / ".cache").resolve())
    if not any(resolved.is_relative_to(a) for a in allowed):
        sys.exit(
            f"{what} {str(path)!r}: разрешены только каталоги внутри {allowed[0]} или {allowed[1]}. "
            "Папка чистится по TTL рекурсивно, поэтому произвольный путь (в том числе через симлинк) "
            "означал бы удаление чужих файлов"
        )
    return resolved


def chats_root_path(project_cfg: dict) -> Path:
    """chats_root -> абсолютный путь. Абсолютный в конфиге берется как есть."""
    raw = Path(str(project_cfg["chats_root"])).expanduser()
    root = raw.resolve() if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()
    warn_if_inside_project(root, "chats_root")
    return root


def load_existing(result_path: Path) -> dict:
    with result_path.open(encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: Path, data: dict) -> None:
    """Записывает JSON атомарно через временный файл + os.replace.

    Защита от битого файла при падении посреди json.dump (Ctrl+C, OOM, ...).

    Имя временного файла включает pid. При фиксированном имени два
    одновременных прогона (крон и ручной запуск) писали в один и тот же .tmp и
    подменяли друг другу недописанный файл - в result.json мог попасть битый
    JSON. Разные имена этого не допускают; потеря обновления остается
    возможной (побеждает тот, кто сделал replace последним), но это
    восстанавливается следующим инкрементальным pull, а битый файл - нет.
    """
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)  # не оставлять мусор при падении
        raise


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

    Смещения и длины entities Telegram считает в единицах UTF-16, а не в
    символах: эмодзи вне BMP занимает две единицы, и срез Python-строки по
    offset после него выделяет не тот текст (ревью 06.09.2026: "😀AB" с жирной
    A размечалось как жирная B). Поэтому режем UTF-16-представление.
    """
    if not text:
        return []
    if not entities:
        return [{"type": "plain", "text": text}]

    u16 = text.encode("utf-16-le")
    total = len(u16) // 2

    def unit_slice(a: int, b: int) -> str:
        return u16[a * 2:b * 2].decode("utf-16-le")

    out: list[dict] = []
    cursor = 0
    sorted_entities = sorted(entities, key=lambda e: (e.offset, e.length))
    try:
        for ent in sorted_entities:
            start = ent.offset
            end = min(ent.offset + ent.length, total)
            if start < cursor or start >= total:
                continue
            if start > cursor:
                out.append({"type": "plain", "text": unit_slice(cursor, start)})
            segment = unit_slice(start, end)
            etype = ENTITY_MAP.get(type(ent), "plain")
            item: dict = {"type": etype, "text": segment}
            if isinstance(ent, MessageEntityTextUrl):
                item["href"] = ent.url
            elif isinstance(ent, MessageEntityMentionName):
                item["user_id"] = f"user{ent.user_id}"
            out.append(item)
            cursor = end
        if cursor < total:
            out.append({"type": "plain", "text": unit_slice(cursor, total)})
    except UnicodeDecodeError:
        # Telegram границы пар суррогатов не режет; если срез все же попал в
        # середину пары - разметка недостоверна, отдаем сообщение целиком plain
        return [{"type": "plain", "text": text}]
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


def cleanup_old_media(ttl_hours: int | None = None) -> int:
    """Удаляет файлы медиа-кэша старше ttl_hours (по mtime). Возвращает число удаленных.

    Значение по умолчанию берется в момент вызова, а не в момент импорта:
    проектный конфиг перекрывает MEDIA_TTL_HOURS уже после загрузки модуля,
    и defaults-аргумент застыл бы на дефолте, молча игнорируя настройку.
    """
    if ttl_hours is None:
        ttl_hours = MEDIA_TTL_HOURS
    if ttl_hours < 1:
        sys.exit(f"TTL медиа-кэша {ttl_hours}: рекурсивная чистка с такой границей удалила бы все")
    cache = MEDIA_CACHE.resolve() if MEDIA_CACHE.exists() else MEDIA_CACHE
    home = Path.home().resolve()
    if cache == Path(cache.anchor) or cache == home or home.is_relative_to(cache):
        sys.exit(f"медиа-кэш {MEDIA_CACHE}: корень, дом или его родитель - чистить рекурсивно нельзя")
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


async def fetch_new(client: TelegramClient, entity, min_id: int,
                    download_media: bool = True) -> tuple[list[dict], list[dict], dict, list[str]]:
    """Тянет новые сообщения с id > min_id.

    Возвращает (messages, new_topics, topic_edits, downloaded_media) - сообщения
    в формате TG Desktop, свежесозданные темы, накопленные правки названий
    (root_id -> new_title) и имена скачанных в кэш вложений.

    download_media=False пропускает скачивание вложений (media=false в записи
    чата): метаданные файлов в сообщениях остаются, самих файлов в кэше нет.
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
            name = await download_message_media(client, msg, chat_media_dir) if download_media else None
            if name:
                downloaded.append(name)
    return out, new_topics, topic_edits, downloaded


async def fetch_recent(client: TelegramClient, entity, limit: int) -> tuple[dict[int, dict], set[int]]:
    """Перечитывает последние `limit` сообщений чата ради правок и удалений.

    Инкремент по id (fetch_new) не видит ни отредактированного, ни удаленного
    сообщения: прогон отвечал "новых нет", а текст в зеркале уже не совпадал с
    тем, что у собеседника (HR, 18.08.2026: пять правок за три прогона, одна
    содержательная). Возвращает (records_by_id, seen_ids): записи обычных
    сообщений по id и множество ВСЕХ id в окне, включая service-сообщения -
    их в зеркале нет, но для сверки удалений они "присутствуют".
    """
    records: dict[int, dict] = {}
    seen: set[int] = set()
    async for msg in client.iter_messages(entity, limit=limit):
        seen.add(msg.id)
        if msg.action is not None:
            continue
        rec = await message_to_record(client, msg)
        if rec:
            records[msg.id] = rec
    return records, seen


EDIT_TEXT_KEYS = ("text", "text_entities", "edited", "edited_unixtime")
EDIT_MEDIA_KEYS = ("file_name", "file_size", "mime_type")


def merge_edits(messages: list[dict], fresh: dict[int, dict], seen_ids: set[int],
                complete: bool = False) -> tuple[int, int]:
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
            if new_e < old_e or (new_e == old_e and not changed):
                continue
            if new_e == 0:
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


async def process_chat(client: TelegramClient, chats_root: Path, label: str, chat_id: int, dialog_entities: dict, dest_dir: Path | None = None, download_media: bool = True) -> tuple[int, str]:
    result_path = (dest_dir if dest_dir is not None else chats_root / label) / "result.json"
    entity = await resolve_entity(client, chat_id, dialog_entities)

    removed = cleanup_old_media()
    if removed:
        print(f"  медиа-кэш: удалено файлов старше {MEDIA_TTL_HOURS}ч: {removed}")

    migrated = False
    repaired = False
    if result_path.exists():
        data = load_existing(result_path)
        ensure_result_belongs_to(data, chat_id, result_path)
        if data.get("id") is None:
            # legacy-зеркало без id в шапке: принадлежность не проверить -
            # предупреждаем и дописываем id, чтобы следующая запись сделала
            # зеркало проверяемым (самолечение)
            sys.stderr.write(
                f"  {label}: в шапке {result_path.name} нет id - принадлежность "
                f"зеркала не проверена, дописываю id {chat_id}\n"
            )
            data["id"] = chat_id
        is_bootstrap = False

        if "topics" not in data:
            pre_path = result_path.with_name("result.pre-migration.json")
            shutil.copy2(result_path, pre_path)
            try:
                data, stats = migrate_legacy(data)
            except Exception as exc:
                # Наверх, а не return 0: молчаливый успешный возврат маскировал
                # пропущенный чат под обработанный - прогон печатал OK: и код 0.
                raise RuntimeError(
                    f"миграция legacy формата упала ({exc}); "
                    f"файл не изменен, бэкап в {pre_path.name}"
                ) from exc
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

    new_msgs, new_topics, topic_edits, downloaded_media = await fetch_new(client, entity, min_id, download_media)

    # Второй запрос - последние EDIT_WINDOW сообщений: правки и удаления, которых
    # инкремент по id не видит. Сверяются только уже зазеркаленные сообщения
    # (новые еще не в data["messages"] и в окно попадают как есть). На bootstrap
    # сверять не с чем.
    edits = deletions = 0
    edits_checked = False
    if not is_bootstrap and EDIT_WINDOW > 0:
        try:
            fresh, seen_ids = await fetch_recent(client, entity, EDIT_WINDOW)
        except Exception as exc:  # noqa: BLE001 - сеть; инкремент выше уже забран и должен сохраниться
            sys.stderr.write(f"  {label}: правки и удаления не проверены ({type(exc).__name__}: {exc})\n")
        else:
            if seen_ids:
                # сверяются и уже зазеркаленные, и только что забранные: новое
                # сообщение могло быть правлено или удалено между двумя запросами
                edits, deletions = merge_edits(
                    data["messages"] + new_msgs, fresh, seen_ids,
                    complete=len(seen_ids) < EDIT_WINDOW,
                )
                edits_checked = True
            else:
                sys.stderr.write(f"  {label}: окно сверки пустое - правки и удаления не проверены\n")

    for t in new_topics:
        topics_by_id[t["id"]] = t
    for root_id, new_title in topic_edits.items():
        if root_id in topics_by_id:
            topics_by_id[root_id]["title"] = new_title

    data["topics"] = sorted(topics_by_id.values(), key=lambda t: t["id"])

    has_changes = bool(new_msgs or new_topics or topic_edits)
    needs_save = is_bootstrap or migrated or repaired or has_changes or edits or deletions

    if not needs_save:
        last_date = data["messages"][-1]["date"] if data.get("messages") else "?"
        # нули печатаются намеренно: "проверил, правок нет" должно отличаться
        # от "не проверял" (silent-failure.md)
        note = "правок: 0, удалений: 0" if edits_checked else "правки не проверены"
        print(f"  {label}: новых нет, {note} (последнее {last_date})")
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
    edits_note = "" if is_bootstrap else (f"правок: {edits}, удалений: {deletions}, " if edits_checked else "правки не проверены, ")
    print(f"  {label}: {prefix}+{len(new_msgs)} ({edits_note}последнее {last_date}, тем: {len(data['topics'])})")
    if downloaded_media:
        print(f"  {label}: вложений скачано в кэш: {len(downloaded_media)} -> {MEDIA_CACHE / str(chat_id)}")
    return len(new_msgs), last_date


async def amain() -> int:
    project_cfg = load_project_config()
    chats_root = chats_root_path(project_cfg)

    check_unique_targets(project_cfg["chats"], chats_root)

    print(f"snapshot {datetime.now(timezone.utc).isoformat()}")

    # Чаты группируем по аккаунту и обходим аккаунты ПОСЛЕДОВАТЕЛЬНО, каждый
    # своим клиентом. Одновременно живых клиентов нет, поэтому за session-файлы
    # никто не дерется (у аккаунтов они и так разные - см. load_auth).
    by_account: dict[str, list] = {}
    for label, entry in project_cfg["chats"].items():
        by_account.setdefault(entry["account"], []).append((label, entry))

    total = 0
    failed: list[str] = []
    for account in sorted(by_account):
        auth = load_auth(account)
        session_path = str(AUTH_DIR / auth["session_name"])
        client = TelegramClient(
            session_path, auth["api_id"], auth["api_hash"], **client_kwargs(auth)
        )
        try:
            # start() внутри try: он же и подключает, а упасть может уже после
            # (например, прерванный интерактивный логин нового аккаунта) -
            # в цикле по аккаунтам это утекло бы соединением
            await connect_with_retry(client, interactive=True)
            if len(by_account) > 1:
                print(f"\n[аккаунт {account}]")

            # Карта {unmarked_id -> свежий entity}. Нужна и для прогрева
            # (get_entity на свежей сессии иначе трактует int как PeerUser), и -
            # главное - как авторитетный источник entity вместо битого кеша
            # (см. resolve_entity). Строится ЗАНОВО на каждый аккаунт: entity
            # принадлежит конкретной сессии (свой access_hash), и переиспользование
            # чужой карты дало бы резолв не того чата.
            dialog_entities: dict = {}
            async for d in client.iter_dialogs():
                eid = getattr(d.entity, "id", None)
                if eid is not None:
                    dialog_entities[eid] = d.entity

            entries = by_account[account]
            for i, (label, entry) in enumerate(entries):
                chat_id = entry["id"]
                try:
                    n, _ = await process_chat(
                        client, chats_root, label, chat_id, dialog_entities,
                        dest_dir=resolve_dest(entry["dest"]) if entry.get("dest") else None,
                        download_media=entry.get("media", True),
                    )
                    total += n
                except asyncio.CancelledError:
                    # CancelledError - BaseException, мимо except Exception ниже.
                    # Telethon отменяет свои футуры при разрыве соединения под
                    # нагрузкой скачивания - это локальная беда одного чата, а не
                    # команда остановить прогон. Но внешнюю отмену (Ctrl+C через
                    # Runner в py3.11+) глушить нельзя - см. external_cancel().
                    if external_cancel():
                        raise
                    failed.append(label)
                    sys.stderr.write(
                        f"!! {label} ({chat_id}): прерван (CancelledError - обычно "
                        f"разрыв соединения при скачивании), чат пропущен\n"
                    )
                    # Отмена обычно значит, что соединение умерло. Без реконнекта
                    # остаток чатов аккаунта посыпался бы каскадом ConnectionError.
                    # Пересоздаем соединение БЕЗУСЛОВНО: is_connected() у telethon
                    # отражает "пользователь просил связь", а не живость транспорта,
                    # и после разрыва спокойно отдает True.
                    reconnect_exc: BaseException | None = None
                    try:
                        await disconnect_quietly(client)
                        await connect_with_retry(client)
                    except asyncio.CancelledError as exc2:
                        # нестабильная сессия отменяет и сам реконнект - это
                        # коррелированный отказ, а не внешняя отмена
                        if external_cancel():
                            raise
                        reconnect_exc = exc2
                    except Exception as exc2:
                        reconnect_exc = exc2
                    if reconnect_exc is not None:
                        rest = [l for l, _ in entries[i + 1:]]
                        failed.extend(rest)
                        sys.stderr.write(
                            f"!! аккаунт {account}: реконнект не удался "
                            f"({type(reconnect_exc).__name__}: {reconnect_exc}); "
                            f"пропущены чаты: {', '.join(rest) or '-'}\n"
                        )
                        break
                except Exception as exc:
                    failed.append(label)
                    sys.stderr.write(f"!! {label} ({chat_id}): {exc} - чат пропущен\n")
        finally:
            await disconnect_quietly(client)

    if failed:
        # Не "OK:": этот маркер закреплен за полностью штатным завершением
        # (см. скилл telegram-snapshot, раздел про лок и завершение).
        print(f"\nЧАСТИЧНО: +{total} сообщений; провалено чатов: {len(failed)} ({', '.join(failed)})")
        return 1
    print(f"\nOK: +{total} сообщений всего")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
