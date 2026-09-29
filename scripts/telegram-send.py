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

Пустой --file или путь к несуществующему файлу - отказ с кодом 2 до
создания клиента, как в dry-run, так и с --send.

Второй предохранитель - гейт темпа: отправка слишком рано после предыдущей в
тот же чат отклоняется с кодом 3 ("нужно N сек, осталось M"). Серия сообщений
идет с паузами, а не пачкой: залп выдает автоматику вернее содержания текста.
Обход - --no-pace-check (когда на том конце не человек или есть срочность).

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
import math
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from telethon import TelegramClient
    from telethon import utils as telethon_utils
except ImportError:
    sys.stderr.write(
        "telethon не установлен. Поставь: pip3 install --user telethon\n"
    )
    sys.exit(2)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = Path.home() / ".config" / "telegram-snapshot"
AUTH_PATH = AUTH_DIR / "auth.json"

# Повтор подключения, когда общую .session держит другой процесс (см.
# connect_with_retry). Копия констант из telegram-snapshot.py.
LOCK_ATTEMPTS = 5
LOCK_DELAY = 15
PROJECT_CONFIG_PATH = PROJECT_ROOT / ".telegram-snapshot.json"

# Гейт темпа: не дает отправить серию сообщений пачкой. Живой человек не шлет
# три абзаца в одну секунду, и залп выдает автоматику вернее содержания текста
# (rules/outbound-timing.md, "Паузы внутри своей серии"). Правило существовало
# текстом и трижды не удержало поведение на живых клиентах - поэтому оно здесь,
# в механике, в точке действия.
PACE_STATE_PATH = Path.home() / ".cache" / "telegram-send" / "last-sent.json"
PACE_MIN_GAP = 40          # секунд - нижняя граница между своими сообщениями
PACE_CHARS_PER_MIN = 250   # столько знаков человек набирает за минуту
PACE_MAX_GAP = 180         # потолок: дольше трех минут гейт не держит
PACE_JITTER = 0.35         # разброс, чтобы паузы не легли ровной сеткой


def pace_norm_chat(target):
    """Один чат - один ключ, как бы его ни адресовали.

    Канал доступен и сырым id (123456), и marked (-100123456), и телетон
    резолвит оба в один peer - но строки ключа вышли бы разные, и гейт завел
    бы два независимых счетчика. Это штатный обход темпа без всякого флага,
    поэтому ключ считается по РЕЗОЛВНУТОМУ entity, а не по тому, что набрали
    в команде. Тип в ключе нужен, чтобы user 123 и channel 123 не слиплись.
    """
    ident = getattr(target, "id", None)
    if ident is None:
        return target                      # entity нет - берем как есть
    return f"{type(target).__name__}{ident}"


def pace_key(account: str, chat_id) -> str:
    return f"{account}:{pace_norm_chat(chat_id)}"


def pace_load() -> dict:
    try:
        with PACE_STATE_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def pace_base(prev_chars: int) -> float:
    """Расчетная пауза без разброса: минимум плюс время набора, но не выше потолка."""
    need = PACE_MIN_GAP + (prev_chars / PACE_CHARS_PER_MIN) * 60
    return min(need, PACE_MAX_GAP)


def pace_required(prev_chars: int) -> float:
    """Пауза после сообщения длиной prev_chars - время на его набор, с разбросом.

    Считается ОДИН раз, в момент отправки, и хранится в состоянии. Пересчет на
    каждой проверке дал бы плавающее число: агент, упершийся в гейт, повторял бы
    команду, пока случайный джиттер не выпадет поменьше, а сообщение "нужно N
    сек" называло бы каждый раз разное. Разброс тут не украшение - без него
    паузы легли бы ровной сеткой, а это та же подпись автомата, что и залп
    (rules/outbound-timing.md, "Ровная минута").
    """
    need = pace_base(prev_chars)
    # Разброс вниз от расчетного: так потолок остается потолком. Порядок
    # "сначала min, потом +jitter" давал бы 243 секунды при заявленных 180.
    return max(PACE_MIN_GAP, need * (1 - random.uniform(0, PACE_JITTER)))


def pace_scheduled_key(key: str) -> str:
    """Ключ реестра, под которым лежит СПИСОК отложенных для pace_key key.

    Отдельный от key верхнего уровня, и это не косметика: старая версия
    скрипта на другой машине, ничего не знающая про отложенные, в pace_record
    делает state[key] = {...} ЦЕЛИКОМ (полная замена записи, а не merge
    полей). Если бы список отложенных лежал вложенным полем той же записи
    (entry["scheduled"]), такая замена стирала бы его вместе с остальным -
    обычная отправка старой версией между двумя постановками в очередь на
    09:00 обнулила бы защиту, и третья постановка на 09:00 прошла бы мимо
    забытой первой. Ключ отдельного пространства имен старый писатель не
    трогает вовсе - state[key] и state[pace_scheduled_key(key)] независимы.
    """
    return f"scheduled:{key}"


def _pace_candidates(entry: dict | None, scheduled_list) -> list[tuple[float, float, bool]]:
    """(ts, required, is_scheduled) для каждой релевантной записи чата: сама
    верхнеуровневая (последняя ОБЫЧНАЯ отправка - прежний формат, ts/chars/
    required, ключ pace_key) и каждая еще не вычищенная отложенная из
    отдельного ключа pace_scheduled_key (список записей).

    Несколько отложенных не заменяют друг друга (см. pace_record) - сверяться
    нужно со всеми разом, иначе вторая отложенная пройдет мимо первой, про
    которую состояние "забыло". Мусор в записи (не число, inf/nan) молча
    отбрасывается - как отбрасывался раньше в pace_check, только теперь на
    уровне одной записи, а не всего чата.
    """
    out: list[tuple[float, float, bool]] = []

    def add(ts_raw, required_raw, chars_raw, is_scheduled: bool) -> None:
        try:
            ts = float(ts_raw)
            required = float(required_raw)
            if required <= 0:
                # Запись старого формата: паузу берем БЕЗ разброса, иначе она
                # пересчитывалась бы на каждой проверке - и повтором команды
                # можно было бы вымучить значение поменьше.
                required = pace_base(int(chars_raw or 0))
        except (TypeError, ValueError, OverflowError):
            return
        if math.isfinite(ts) and math.isfinite(required):
            out.append((ts, required, is_scheduled))

    if isinstance(entry, dict) and "ts" in entry:
        add(entry.get("ts", 0), entry.get("required", 0), entry.get("chars", 0), False)

    if isinstance(scheduled_list, list):
        for rec in scheduled_list:
            if isinstance(rec, dict):
                add(rec.get("ts", 0), rec.get("required", 0), rec.get("chars", 0), True)

    return out


def pace_check(account: str, chat_id, at: datetime | None = None) -> tuple[float, float]:
    """(сколько еще ждать, сколько требовалось - для самой строгой из
    конфликтующих записей) в секундах; (0.0, 0.0) - можно слать.

    at - момент, для которого проверяем темп: None - "сейчас" (обычная
    немедленная отправка), aware datetime - назначенное время доставки
    отложенной. Сверяется со ВСЕМИ записями чата разом (см. _pace_candidates),
    а не только с последней: обычная отправка прямо сейчас обязана видеть уже
    поставленные в очередь отложенные, а не только предыдущую обычную.
    """
    state = pace_load()
    key = pace_key(account, chat_id)
    entry = state.get(key)
    scheduled_list = state.get(pace_scheduled_key(key))
    candidates = _pace_candidates(entry if isinstance(entry, dict) else None, scheduled_list)
    if not candidates:
        return 0.0, 0.0

    target = None
    if at is not None:
        try:
            target = at.timestamp()
        except (OverflowError, OSError, ValueError):
            return 0.0, 0.0
        if not math.isfinite(target):
            return 0.0, 0.0

    now = time.time()
    worst_wait, worst_required = 0.0, 0.0
    for ts, required, is_scheduled in candidates:
        if target is not None:
            # Кандидат на конкретный момент доставки (отложенная отправка):
            # сравниваем с уже записанным временем в обе стороны - неважно,
            # раньше оно или позже кандидата, и неважно, что за запись
            # (обычная или тоже отложенная) - планируется доставка близко к
            # уже занятому времени, значит будет залп в момент доставки.
            diff = abs(target - ts)
        elif not is_scheduled:
            # Обычная проверка "сейчас" против прежней ОБЫЧНОЙ записи: старое
            # поведение байт в байт - съехавшие вперед часы (запись из бэкапа
            # или чужого будущего) не держат чат, это врут часы, а не человек
            # торопится.
            if ts > now:
                continue
            diff = now - ts
        else:
            # Обычная проверка "сейчас" против отложенной: ts - назначенное
            # время доставки, оно законно в будущем и это не перекос часов.
            # Сверяем в обе стороны - если через 30 секунд уйдет отложенное,
            # немедленная отправка сейчас все равно даст залп в момент доставки.
            diff = abs(now - ts)
        if diff >= required:
            continue
        wait = required - diff
        if wait > worst_wait:
            worst_wait, worst_required = wait, required
    return worst_wait, worst_required


def pace_record(
    account: str, chat_id, chars: int, *, at: datetime | None = None, scheduled: bool = False
) -> None:
    """Запомнить момент отправки (или назначенное время доставки для
    отложенной) и паузу, которую он требует. Сбой записи отправку не отменяет.

    Формат с обратной совместимостью. Верхнеуровневая запись под pace_key
    (ts/chars/required) - последняя ОБЫЧНАЯ отправка, ключ и семантика прежние
    байт в байт: старая версия скрипта на другой машине, ничего не знающая про
    отложенные, прочитает ее как раньше. Отложенные хранятся СПИСКОМ под
    ОТДЕЛЬНЫМ ключом pace_scheduled_key, а не вложенным полем той же записи -
    см. его докстринг про то, почему это важно (старый писатель делает
    state[pace_key] = {...} целиком, и вложенный список это не пережил бы).
    Несколько отложенных друг друга не заменяют - иначе постановки
    09:00 -> 12:00 -> 09:00 все проходят (третья мимо занятого 09:00, о
    котором состояние уже "забыло"), а обычная отправка стирала бы
    единственное свидетельство будущей доставки.

    at - для отложенной отправки: назначенное время доставки, а не момент
    постановки в очередь - гейт темпа должен видеть, КОГДА сообщение реально
    появится в чате, а не когда была вызвана команда. Без at - "сейчас"
    (обычная немедленная отправка).

    Список отложенных чистится от записей старше, чем "сейчас минус
    наибольший required среди оставшихся" - более старые уже ни с чем не
    могут конфликтовать ни при какой проверке.

    Состояние читается и пишется без блокировки: два параллельных прогона в
    разные чаты могут затереть записи друг друга. Для последовательной отправки
    (наш случай) это неважно, а платить за это локом в пути отправки не стоит.
    """
    try:
        PACE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state = pace_load()
        key = pace_key(account, chat_id)
        now_ts = time.time()

        if scheduled:
            sched_key = pace_scheduled_key(key)
            sched_list = state.get(sched_key)
            if not isinstance(sched_list, list):
                sched_list = []
            record = {
                "ts": at.timestamp() if at is not None else now_ts,
                "chars": int(chars),
                "required": pace_required(int(chars)),
            }
            sched_list.append(record)
            max_required = max(
                (float(r.get("required", 0)) for r in sched_list if isinstance(r, dict)),
                default=0.0,
            )
            cutoff = now_ts - max_required
            state[sched_key] = [
                r for r in sched_list
                if isinstance(r, dict) and float(r.get("ts", 0)) >= cutoff
            ]
        else:
            entry = state.get(key)
            if not isinstance(entry, dict):
                entry = {}
            entry["ts"] = at.timestamp() if at is not None else now_ts
            entry["chars"] = int(chars)
            entry["required"] = pace_required(int(chars))
            state[key] = entry

        # Уникальное имя + O_EXCL + O_NOFOLLOW: предсказуемый tmp можно
        # подменить симлинком и через нас усечь чужой файл.
        tmp = PACE_STATE_PATH.with_name(f"{PACE_STATE_PATH.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False)
            tmp.replace(PACE_STATE_PATH)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        sys.stderr.write(f"Предупреждение: не удалось записать состояние темпа ({exc}).\n")


def pace_guard(account: str, chat_id, skip: bool, *, at: datetime | None = None) -> int:
    """Проверка перед отправкой. 0 - можно слать, 3 - слишком рано.

    at - см. pace_check: None для обычной немедленной отправки, назначенное
    время доставки для отложенной.

    Возврат 3 - это отказ, а не ожидание: пауза должна быть решением
    отправителя, а не молчаливым сном скрипта внутри чужого прогона.
    """
    if skip:
        return 0
    wait, required = pace_check(account, chat_id, at=at)
    if wait <= 0:
        return 0
    sys.stderr.write(
        f"Слишком быстро после предыдущего сообщения в этот чат.\n"
        f"  нужно выждать: {required:.0f} сек, осталось: {wait:.0f} сек\n"
        f"  серия сообщений отправляется с паузами, а не пачкой - залп выдает автоматику\n"
        f"  подожди и повтори команду; обход - флаг --no-pace-check\n"
        f"  (обход уместен, когда на том конце не человек или есть срочность по существу)\n"
    )
    return 3


# Отложенная отправка на серверах Telegram (--schedule): сообщение ложится в
# очередь и уходит в назначенное время без участия отправителя - client.session
# для этого к моменту доставки уже не нужен. Лимит Telegram - около года вперед.
SCHEDULE_MAX_DAYS = 366
# Telegram отправляет немедленно, если до назначенного времени осталось меньше
# 10 секунд (документированное поведение) - запас в 120с держит дистанцию от
# этой границы заметно раньше, чем сеть и обвязка успеют ее съесть. Проверяется
# минимум дважды: здесь (до сети) и еще раз в amain() прямо перед send_message/
# send_file, потому что подключение и прогрев диалогов время тоже тратят. Для
# файла - еще и третий раз, после upload_file: сама загрузка файла тоже тратит
# время, и большой файл может успеть съесть весь оставшийся запас.
SCHEDULE_MIN_LEAD = 120
_SCHEDULE_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"[T ](?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)


class RoundMinuteRejected(ValueError):
    """Ровная минута (:00/:15/:30/:45, независимо от секунд) без --exact-minute.

    Подкласс ValueError - код, ловящий общий ValueError (старый контракт
    parse_schedule), продолжает его ловить. main() проверяет этот класс
    ПЕРЕД общим ValueError, чтобы вернуть отдельный код (6, а не 2): причина
    отказа другая - не разбор и не диапазон времени, а как метка выглядит
    получателю (rules/outbound-timing.md, "Ровная минута").
    """


def is_round_minute(dt: datetime) -> bool:
    """Ровная минута - подпись автомата (rules/outbound-timing.md, "Ровная
    минута"): минута кратна 15 (:00/:15/:30/:45), независимо от секунд.
    09:00:30 - ровная: видимая метка остается 09:00. Проверяем уже
    разобранное время доставки без перевода в UTC или пояс машины.
    """
    return dt.minute % 15 == 0


def suggest_non_round(dt: datetime) -> datetime:
    """Соседняя некруглая минута для подсказки в тексте отказа.

    Правило выбора: сдвиг ВПЕРЕД на 7 минут. 7 не делится на 15, поэтому
    результат математически не может попасть на другую круглую отметку - не
    нужно перебирать исключения или проверять результат повторно. Секунды
    обнуляются - подсказка называет минуту, а не секунду. Направление
    (вперед, а не назад) выбрано произвольно, но фиксировано: подсказка для
    одного и того же dt всегда одна и та же, а не случайная.
    """
    # Сдвиг по абсолютному времени, а не по настенному: у ZoneInfo арифметика
    # datetime идет по стенке и игнорирует fold, и в ночь перевода часов назад
    # "+7 минут" от второго 02:00 давало первое 02:07 - на 53 минуты раньше.
    shifted = (dt.astimezone(timezone.utc) + timedelta(minutes=7)).astimezone(dt.tzinfo)
    return shifted.replace(second=0, microsecond=0)


def load_schedule_zone(tz_name: str) -> ZoneInfo:
    """--schedule-tz: IANA-имя пояса получателя -> ZoneInfo. Пустая строка,
    неизвестное или кривое имя - ValueError с понятной строкой (до сети)."""
    name = (tz_name or "").strip()
    if not name:
        raise ValueError("--schedule-tz задан пустой строкой: жду IANA-имя пояса, например Europe/Paris")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"неизвестный часовой пояс {tz_name!r} в --schedule-tz: жду IANA-имя "
            f"вида Europe/Paris или America/New_York ({exc})"
        ) from exc


def parse_schedule(
    value: str, *, now: datetime | None = None, exact_minute: bool = False,
    tz_name: str | None = None,
) -> datetime:
    """--schedule: ISO-время с оффсетом или без. Без оффсета - зона машины.

    С tz_name (--schedule-tz) время без оффсета - настенное время в этом поясе,
    оффсет берется из zoneinfo на дату доставки; явный оффсет обязан совпасть
    с оффсетом пояса на этот момент. Возвращаемый dt тогда несет ZoneInfo,
    и все сообщения об ошибках печатают время получателя.

    Возвращает aware datetime. Ручной разбор регэкспом, а не
    datetime.fromisoformat: та до Python 3.11 не берет время без секунд
    ("09:30"), а нам нужна именно эта форма из примера в задаче.
    """
    raw = value.strip()
    m = _SCHEDULE_RE.match(raw)
    if not m:
        raise ValueError(
            f"не разобрать время {value!r}: жду ISO вида 2026-09-22T09:30 "
            f"или 2026-09-22T09:30+03:00"
        )
    y, mo, d = int(m["y"]), int(m["mo"]), int(m["d"])
    h, mi = int(m["h"]), int(m["mi"])
    s = int(m["s"]) if m["s"] else 0
    try:
        naive = datetime(y, mo, d, h, mi, s)
    except ValueError as exc:
        raise ValueError(f"некорректная дата/время {value!r}: {exc}") from exc

    zone = load_schedule_zone(tz_name) if tz_name is not None else None
    zone_label = f"в поясе {zone.key}" if zone is not None else "в зоне машины"

    tz_raw = m["tz"]
    if tz_raw is None:
        # Без оффсета время считается локальным временем машины - astimezone()
        # без аргумента интерпретирует наивный datetime так и подставляет зону
        # системы, не сдвигая часы. Дальше dry-run обязан напечатать оффсет
        # явно, чтобы рассинхрон зон был виден ДО --send.
        #
        # Переход на летнее/зимнее время дает в этой зоне час, у которого нет
        # решения (пропущенный при переводе вперед) или два решения
        # (повторенный при переводе назад) - astimezone() в обоих случаях
        # молча выберет одно по fold=0, а это ровно тот тихий выбор, который
        # нужно превратить в отказ. fold=0/fold=1 - единственные два
        # прочтения наивного времени; если они дают один и тот же оффсет,
        # переход тут ни при чем.
        #
        # С --schedule-tz то же самое, только в поясе получателя: fold=0/1 с
        # tzinfo=ZoneInfo, а "обратное преобразование" - через UTC обратно в
        # этот пояс.
        if zone is None:
            a0 = naive.replace(fold=0).astimezone()
            a1 = naive.replace(fold=1).astimezone()
        else:
            a0 = naive.replace(tzinfo=zone, fold=0).astimezone(timezone.utc).astimezone(zone)
            a1 = naive.replace(tzinfo=zone, fold=1).astimezone(timezone.utc).astimezone(zone)
        if a0.utcoffset() == a1.utcoffset():
            dt = a0
        else:
            wall0 = (a0.year, a0.month, a0.day, a0.hour, a0.minute, a0.second)
            wall_naive = (naive.year, naive.month, naive.day, naive.hour, naive.minute, naive.second)
            if wall0 != wall_naive:
                # Обратное преобразование (fold=0 -> UTC -> назад в зону
                # машины) поменяло настенное время - значит запрошенного
                # момента в этой зоне не бывает.
                raise ValueError(
                    f"несуществующее локальное время {naive.strftime('%Y-%m-%d %H:%M:%S')}: "
                    f"{zone_label} в этот момент переводят стрелки вперед "
                    f"(пропущенный час) - задай оффсет явно"
                )
            raise ValueError(
                f"неоднозначное локальное время {naive.strftime('%Y-%m-%d %H:%M:%S')}: "
                f"{zone_label} в этот момент переводят стрелки назад "
                f"(повторенный час) - задай оффсет явно"
            )
    elif tz_raw == "Z":
        dt = naive.replace(tzinfo=timezone.utc)
    else:
        sign = 1 if tz_raw[0] == "+" else -1
        digits = tz_raw[1:].replace(":", "")
        oh, om = int(digits[:2]), int(digits[2:])
        # timedelta нормализует минуты/часы вне обычного диапазона в
        # действительное время ("+03:99" молча стало бы "+04:39") - оффсет
        # это заявленный часовой пояс, а не арифметика, поэтому мусор в нем
        # отклоняем явно, а не подставляем то, что получилось после переноса.
        if not (0 <= om <= 59):
            raise ValueError(f"некорректный оффсет {tz_raw!r}: минуты вне 00-59")
        if not (0 <= oh <= 14):
            raise ValueError(f"некорректный оффсет {tz_raw!r}: часы вне 00-14 (реальный максимум UTC - +14:00)")
        dt = naive.replace(tzinfo=timezone(sign * timedelta(hours=oh, minutes=om)))

    if tz_raw is not None and zone is not None:
        # Явный оффсет плюс пояс: оффсет - утверждение о поясе, и оно обязано
        # совпасть с тем, что zoneinfo дает на этот момент. Расхождение - это
        # ровно тот случай, ради которого флаг заведен (команда с +02:00,
        # повторенная после перевода часов), поэтому отказ, а не молчаливый
        # выбор одного из двух.
        local = dt.astimezone(zone)
        if local.utcoffset() != dt.utcoffset():
            raise ValueError(
                f"оффсет {_format_offset(dt)} не совпадает с {zone.key} на "
                f"{local.strftime('%Y-%m-%d')} ({_format_offset(local)}) - убери "
                f"оффсет из --schedule или исправь его"
            )
        dt = local

    now = now or datetime.now(timezone.utc)
    if dt <= now:
        raise ValueError(f"время в прошлом: {format_schedule(dt)}")
    lead = (dt - now).total_seconds()
    if lead < SCHEDULE_MIN_LEAD:
        raise ValueError(
            f"время слишком близко к текущему моменту ({lead:.0f} сек, нужно "
            f"минимум {SCHEDULE_MIN_LEAD}): {format_schedule(dt)} - Telegram "
            f"отправляет немедленно, если до срока меньше 10 сек, а такой запас "
            f"слишком мал, чтобы доверять этому после подключения к сети"
        )
    if dt > now + timedelta(days=SCHEDULE_MAX_DAYS):
        raise ValueError(
            f"время дальше {SCHEDULE_MAX_DAYS} дней вперед: {format_schedule(dt)} "
            f"(лимит отложенной отправки в Telegram - около года)"
        )
    if is_round_minute(dt) and not exact_minute:
        hint = suggest_non_round(dt)
        raise RoundMinuteRejected(
            f"ровная минута {format_schedule(dt)}: читается как подпись автомата, "
            f"а не человека (rules/outbound-timing.md, \"Ровная минута\") - возьми "
            f"соседнюю некруглую, например {hint.strftime('%H:%M')} "
            f"({format_schedule(hint)}). Минуту назвал сам пользователь явно "
            f"('ровно в 9:00', 'к началу созвона') - обход --exact-minute."
        )
    return dt


def _format_offset(dt: datetime) -> str:
    offset = dt.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    oh, om = divmod(abs(total_minutes), 60)
    return f"{sign}{oh:02d}:{om:02d}"


def format_schedule(dt: datetime) -> str:
    """"2026-09-22 09:30:59 +03:00" - секунды печатаются всегда, а не только
    когда ненулевые: превью обязано совпадать с тем, что реально уйдет
    (send_message получает dt целиком, включая секунды из --schedule)."""
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {_format_offset(dt)}"


def describe_schedule(dt: datetime, tz_name: str | None = None) -> str:
    """Метка времени доставки для dry-run и строки результата.

    Без tz_name - ровно format_schedule(dt) (вывод прежний, байт в байт).
    С --schedule-tz - дважды: у получателя (с именем пояса) и в зоне машины,
    чтобы расхождение было видно до --send.
    """
    if tz_name is None:
        return format_schedule(dt)
    zone = load_schedule_zone(tz_name)
    return (
        f"{format_schedule(dt.astimezone(zone))} {zone.key} "
        f"(в зоне машины: {format_schedule(dt.astimezone())})"
    )


def _schedule_dates_match(a: datetime, b: datetime) -> bool:
    """Секундная точность: Telegram может округлить дату в очереди, наш dt -
    нет. Разница больше секунды - это уже не округление, а другое время."""
    try:
        return abs((a - b).total_seconds()) <= 1
    except (TypeError, OverflowError):
        return False


# Избранное: отложенное себе на срок в пределах окна Telegram в очереди
# отложенных не показывает, но доставляет (29.09.2026: +4 мин - нет, +1 день -
# есть). Граница точно не известна - окно взято с запасом от --remind (~3-4 мин).
SELF_UNLISTED_WINDOW = 600


async def verify_scheduled(
    client: TelegramClient, entity, msg_id: int, *,
    expected_date: datetime, expected_text: str,
) -> tuple[bool, Exception | None]:
    """Отложенное сообщение не появляется в обычной истории до срока - оно в
    очереди client.get_messages(entity, scheduled=True). Проверять его там же,
    иначе честно поставленное в очередь читалось бы как ненайденное.

    Совпадения по одному id недостаточно: id из обычной истории и id из
    очереди отложенных живут в разных пространствах, и случайное совпадение
    читалось бы как найденное сообщение. Поэтому сверяем еще назначенную дату
    доставки и текст (подпись - для файла).

    Возврат (найдено, исключение) - НЕ bool. И пустая очередь, и упавший
    запрос - оба неопределенный результат снаружи (см. amain): разница только
    в диагностике, которую вызывающий код печатает пользователю.
    """
    try:
        pending = await client.get_messages(entity, scheduled=True)
    except Exception as exc:
        return False, exc
    for m in pending:
        if getattr(m, "id", None) != msg_id:
            continue
        m_date = getattr(m, "date", None)
        if m_date is None or not _schedule_dates_match(m_date, expected_date):
            continue
        m_text = getattr(m, "message", None)
        if m_text is None:
            m_text = getattr(m, "text", None)
        if (m_text or "") != (expected_text or ""):
            continue
        return True, None
    return False, None


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


def external_cancel() -> bool:
    """True, если отменяют саму текущую таску (Ctrl+C через Runner и т.п.) -
    такую отмену глотать нельзя. Отличается по task.cancelling() (py3.11+);
    на py<3.11, где cancelling нет, консервативно считаем отмену внешней.
    Копия хелпера из telegram-snapshot.py: скрипт намеренно самодостаточный."""
    task = asyncio.current_task()
    return task is None or not hasattr(task, "cancelling") or bool(task.cancelling())


async def disconnect_quietly(client) -> None:
    """Best-effort закрытие клиента: своей ошибкой ничего не рвет.

    telethon при отключении пишет состояние в ту же sqlite-сессию
    (_save_states_and_entities), поэтому пока сессию держит другой процесс,
    disconnect падает тем же "database is locked". В cleanup это опаснее самой
    блокировки: в finally оно подменяет исходное исключение своим, а после
    успешной отправки превращает доставленное сообщение в ненулевой код возврата.
    Копия хелпера из telegram-snapshot.py: скрипт намеренно самодостаточный.
    """
    try:
        await client.disconnect()
    except asyncio.CancelledError:
        # CancelledError - BaseException: без этой ветки отмена футур telethon
        # в cleanup рвала бы finally и глушила итог прогона.
        # Внешнюю отмену самой таски (Ctrl+C) не глотаем.
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
    скрипт другого проекта, наш connect() падает изнутри telethon с
    sqlite3.OperationalError: database is locked. Чужой процесс дорабатывает сам
    и отпускает сессию, поэтому лечится ожиданием, а не починкой (убивать процесс
    или удалять .session нельзя - см. скилл telegram-snapshot).

    Оборачивать ТОЛЬКО подключение. Отправку сообщения оборачивать нельзя:
    повтор после успешного send_message даст получателю дубль.
    Копия хелпера из telegram-snapshot.py: скрипт намеренно самодостаточный.
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
    # getattr, не args.schedule: старые вызовы amain() (тесты, чужой код) не
    # знают об этом поле, и им не нужно его заводить, чтобы остаться рабочими.
    schedule_dt = getattr(args, "schedule", None)
    schedule_tz = getattr(args, "schedule_tz", None)

    if args.file is not None and not args.file:
        sys.stderr.write("--file задан пустой строкой - проверь переменную с путем.\n")
        return 2

    file_path = None
    if args.file:
        file_path = Path(args.file).expanduser().resolve()
        if not file_path.is_file():
            sys.stderr.write(f"Файл не найден: {file_path}\n")
            return 2

    text = read_text(args, allow_empty=args.file is not None)

    auth = load_auth(entry["account"])
    session_path = str(AUTH_DIR / auth["session_name"])
    client = TelegramClient(session_path, auth["api_id"], auth["api_hash"], **client_kwargs(auth))

    # Без interactive=True (то есть connect(), не start()): на неготовой или
    # отозванной сессии start() уходит в интерактивный логин (ввод телефона и
    # кода) и в автоматизации зависает.
    # Авторизация - предусловие, настраивается скиллом telegram-snapshot.
    await connect_with_retry(client)
    if not await client.is_user_authorized():
        await disconnect_quietly(client)
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

        if not args.send:
            print("DRY-RUN (без --send отправка не сделана)")
            print(f"  -> \"{title}\" ({kind}, id={chat_id})")
            if schedule_dt is not None:
                print(f"  отложено до: {describe_schedule(schedule_dt, schedule_tz)}")
                if is_round_minute(schedule_dt):
                    # Дошло сюда только через --exact-minute - без флага
                    # parse_schedule() отказала бы раньше, до сети.
                    print("  ровная минута: разрешена явно")
            # от какого аккаунта уйдет - часть гейта: при нескольких номерах
            # ошибиться отправителем так же легко, как чатом
            print(f"  от аккаунта: {entry['account']}")
            print(f"  тема: {topic_id if topic_id is not None else '-'}   ответ на: {reply_id if reply_id is not None else '-'}   звук: {'нет' if args.silent else 'да'}")
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
            wait, required = pace_check(entry["account"], entity, at=schedule_dt)
            if wait > 0:
                print(f"  темп: рано - после прошлого сообщения нужно {required:.0f} сек, осталось {wait:.0f}")
            return 0

        rc = pace_guard(entry["account"], entity, args.no_pace_check, at=schedule_dt)
        if rc:
            return rc

        if schedule_dt is not None:
            # Второй раз, а не полагаясь на проверку из parse_schedule: connect,
            # is_user_authorized и прогрев диалогов (iter_dialogs) уже потратили
            # время, и запас, который был достаточным на старте, мог схлопнуться.
            lead = (schedule_dt - datetime.now(timezone.utc)).total_seconds()
            if lead < SCHEDULE_MIN_LEAD:
                sys.stderr.write(
                    f"До назначенного времени осталось {lead:.0f} сек - меньше "
                    f"{SCHEDULE_MIN_LEAD} (минимальный запас). Подключение и "
                    f"прогрев диалогов съели время; Telegram отправляет "
                    f"немедленно, если до срока меньше 10 сек, а такой запас "
                    f"граничит с этим. Отправка отменена, ничего не отправлено - "
                    f"повтори с более поздним временем.\n"
                )
                return 2

        reply_to = build_reply_to(topic_id, reply_id)
        if file_path:
            file_to_send = str(file_path)
            file_attrs, file_mime = None, None
            if schedule_dt is not None:
                # Атрибуты (имя, длительность и т.п.) вычисляются ИЗ ИСХОДНОГО
                # файла ДО загрузки: после upload_file в send_file уходит
                # InputFile-хендл, у которого содержимого уже не прочитать, и
                # собственная попытка Telethon извлечь их из хендла молча
                # проваливается (для аудио - нулевая длительность вместо
                # настоящей). Явные attributes/mime_type в send_file эту
                # попытку переопределяют.
                file_attrs, file_mime = telethon_utils.get_attributes(
                    str(file_path), force_document=True,
                )
                # Telethon грузит файл ВНУТРИ send_file, до постановки в
                # очередь - большой файл может съесть весь запас между второй
                # проверкой выше и фактической отправкой, и Telegram отправит
                # немедленно (граница - 10 сек). Грузим отдельно и проверяем
                # запас еще раз, уже после загрузки.
                file_to_send = await client.upload_file(str(file_path))
                lead = (schedule_dt - datetime.now(timezone.utc)).total_seconds()
                if lead < SCHEDULE_MIN_LEAD:
                    sys.stderr.write(
                        f"До назначенного времени осталось {lead:.0f} сек после "
                        f"загрузки файла - меньше {SCHEDULE_MIN_LEAD} (минимальный "
                        f"запас). Отправка отменена, ничего не отправлено - "
                        f"повтори с более поздним временем или файлом поменьше.\n"
                    )
                    return 2
            # файл с подписью-текстом; force_document - имя и расширение как есть
            sent = await client.send_file(
                entity, file_to_send, caption=text, reply_to=reply_to,
                force_document=True, parse_mode=None, silent=args.silent,
                schedule=schedule_dt, attributes=file_attrs, mime_type=file_mime,
            )
        else:
            sent = await client.send_message(
                entity, text, reply_to=reply_to, parse_mode=None, silent=args.silent,
                schedule=schedule_dt,
            )
        pace_record(
            entry["account"], entity, len(text or ""),
            at=schedule_dt, scheduled=schedule_dt is not None,
        )
        if schedule_dt is not None:
            # Отложенное лежит в очереди, а не в обычной истории - см.
            # verify_scheduled. id из send_message тут же валиден только внутри
            # этой очереди и сменится, когда Telegram доставит сообщение.
            # Сверяем с тем, что вернула САМА отправка (sent.date, sent.message),
            # а не с нашим входом (schedule_dt, text): при --html или подписи
            # к файлу Telegram сохраняет уже разобранный текст, и сравнение с
            # сырым HTML на входе не совпало бы никогда.
            found, verify_exc = await verify_scheduled(
                client, entity, sent.id, expected_date=sent.date, expected_text=sent.message,
            )
            if (not found and verify_exc is None and getattr(entity, "is_self", False)
                    and (schedule_dt - datetime.now(timezone.utc)).total_seconds() <= SELF_UNLISTED_WINDOW):
                # Избранное: отложенное себе на близкий срок Telegram доставляет как
                # напоминание, но в очереди отложенных его не показывает (проверено
                # 29.09.2026: срок +4 мин - в очереди нет, доставлено в срок; +1 день -
                # в очереди есть). Сверить нечем - говорим прямо, а не ложным кодом 4.
                # Только близкий срок: дальше окна запись в очереди видна, и "не
                # найдено" там - настоящий сбой, код 4.
                print(
                    f"OK: поставлено на {describe_schedule(schedule_dt, schedule_tz)} - \"{title}\" (id {sent.id}); "
                    f"сверка очереди для Избранного на близком сроке невозможна - "
                    f"Telegram такие напоминания в ней не показывает"
                )
                return 0
            if not found:
                # Неподтвержденная постановка - НЕ успех: id, который вернул
                # send_message, доказывает лишь то, что запрос приняли, а не
                # то, что сообщение легло в очередь под назначенное время
                # (rules/silent-failure.md, "Успех отправки относится к факту
                # передачи, а не к продукту"). Автоповтора нет - решение,
                # слать ли снова, за человеком.
                reason = (
                    f"причина: {type(verify_exc).__name__}: {verify_exc}"
                    if verify_exc is not None else "не нашлось в очереди отложенных"
                )
                sys.stderr.write(
                    f"НЕОПРЕДЕЛЕННО: отправка вернула id {sent.id}, но в очереди "
                    f"отложенных не найдено - проверь \"Отложенные\" руками, "
                    f"повторно не отправляй ({reason}).\n"
                )
                return 4
            print(
                f"OK: поставлено в очередь на {describe_schedule(schedule_dt, schedule_tz)} "
                f"- \"{title}\" (id {sent.id})"
            )
        else:
            print(f"OK: отправлено в \"{title}\" (id сообщения {sent.id})")
        return 0
    finally:
        await disconnect_quietly(client)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Отправка сообщения в Telegram-чат проекта от имени пользователя."
    )
    parser.add_argument("--to", required=True, help="label чата из .telegram-snapshot.json")
    parser.add_argument("--text", help="текст сообщения; если опущен - читается из stdin")
    parser.add_argument("--file", help="путь к файлу-вложению; текст уходит подписью к нему")
    parser.add_argument("--send", action="store_true", help="реально отправить (без флага - dry-run)")
    parser.add_argument("--no-pace-check", action="store_true", dest="no_pace_check",
                        help="не проверять паузу после предыдущего сообщения в этот чат. "
                             "По умолчанию скрипт отказывает, если серия идет пачкой: "
                             "залп сообщений выдает автоматику (rules/outbound-timing.md). "
                             "Обход уместен, когда на том конце не человек или есть срочность")
    parser.add_argument("--topic", type=int, help="id корня форумной темы (по умолчанию - из конфига, если задан)")
    parser.add_argument("--reply-to", type=int, dest="reply_to", help="id сообщения, на которое отвечаем")
    parser.add_argument("--silent", action="store_true",
                        help="отправить без звука (получателю придет беззвучное уведомление). "
                             "Нужно, когда отправляем вне рабочего окна получателя - см. "
                             "rules/outbound-timing.md: звук ночью выдает автомат, но метка "
                             "времени остается видимой, поэтому это не обход правила")
    parser.add_argument("--schedule",
                        help="отложить до времени, ISO с оффсетом или без (без оффсета - "
                             "зона машины): 2026-09-22T09:30+03:00 или 2026-09-22T09:30. "
                             "Сообщение ляжет в очередь на серверах Telegram и уйдет само - "
                             "см. rules/outbound-timing.md и скилл telegram-send. Прошлое "
                             "и дальше года вперед отклоняются сразу, до сети")
    parser.add_argument("--schedule-tz", dest="schedule_tz", metavar="IANA",
                        help="пояс получателя по имени (Europe/Paris) для --schedule: "
                             "время без оффсета читается как настенное в этом поясе, "
                             "оффсет берется на дату доставки (переход часов учтен); "
                             "явный оффсет обязан совпасть с поясом. Получатель в "
                             "поясе с переходом часов - всегда по имени, а не числом")
    parser.add_argument("--exact-minute", action="store_true", dest="exact_minute",
                        help="разрешить ровную минуту (:00/:15/:30/:45, независимо от секунд) в "
                             "--schedule. Без флага такая минута отклоняется до сети "
                             "(код 6) - она читается как подпись автомата, а не человека "
                             "(rules/outbound-timing.md, \"Ровная минута\"). Обход - "
                             "только когда минуту назвал сам пользователь явно "
                             "('ровно в 9:00', 'к началу созвона')")
    args = parser.parse_args()
    if args.schedule_tz is not None and args.schedule is None:
        sys.stderr.write("--schedule-tz без --schedule: пояс задается только вместе со временем доставки\n")
        return 2
    if args.schedule is not None:
        # Разбор и все проверки - ДО asyncio.run/сети: ошибка зоны, прошлое
        # время или ровная минута видны немедленно, а не после подключения к
        # Telegram. RoundMinuteRejected ловится раньше общего ValueError,
        # которому она подкласс, - иначе гейт вернул бы тот же код 2, что и
        # разбор/диапазон, и код 6 отличить было бы нечем.
        try:
            args.schedule = parse_schedule(
                args.schedule, exact_minute=args.exact_minute, tz_name=args.schedule_tz
            )
        except RoundMinuteRejected as exc:
            sys.stderr.write(f"{exc}\n")
            return 6
        except ValueError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
