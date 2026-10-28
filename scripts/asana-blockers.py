#!/usr/bin/env python3
"""Связи задач Asana: text и явно включаемые native dependencies.

Старые конфиги используют text. Явный --mode native/auto включает API
dependencies, отдельный внешний journal и digest-bound предпросмотр.
HTTP402 не запускает текстовую запись: нужен новый text-план и согласование.
Общий режим зависит от workspace/операции, не от аккаунта целиком.

Смысл приема: вместо ежедневного сдвига сроков задача честно говорит, чем
именно она держится. Тогда невыполненная задача не выглядит забытой - видно,
что она ждет чужого шага, и сдвиг срока перестает быть единственным способом
это показать (rules/tasks-tracking.md: просрочку молча не переносить).

Проставляются обе стороны связи:
  ЗАБЛОКИРОВАНА - в задаче, которая ждет;
  БЛОКИРУЕТ     - в задаче, которой ждут. Заказчику это показывает цену
                  молчания: его пункт держит чужую работу, а не просто висит.

Идемпотентен: старый блок распознается по маркеру и заменяется целиком, а не
дописывается вторым. Убранная из конфига цепочка снимает блок с описания -
иначе доска врала бы про связь, которой больше нет.

Токен (Personal Access Token, https://app.asana.com/0/my-apps) берется из:
  1. --auth <файл> вида {"token": "..."} (права 600);
  2. переменной окружения ASANA_TOKEN;
  3. файла ~/.config/asana/auth.json (дефолт).
В репозиторий и в заметки токен не кладем.

Дефолт - dry-run: печатается, что будет сделано. Реальная запись - с --send.

Цепочки живут в конфиге, а не в коде: досок обычно несколько, и правка связей
не должна быть правкой скрипта. Формат .asana-blockers.json:
  {
    "<ключ доски>": {
      "project": "<gid доски: последнее число в ее URL>",
      "chains": {
        "<задача, которая ждет>": ["<без чего ее не сделать>", "<и еще одна>"]
      }
    }
  }
Имена задач пишутся дословно как на доске: скрипт сверяет их и отказывается
работать, если не нашел или нашел два одинаковых. Молча пропустить связь хуже,
чем упасть - пропущенная связь неотличима от ее отсутствия.

Примеры:
  python3 scripts/asana-blockers.py --board shophack           # предпросмотр
  python3 scripts/asana-blockers.py --board shophack --send    # записать
  python3 scripts/asana-blockers.py                            # все доски конфига
"""

from __future__ import annotations

import argparse
import copy
import contextvars
import stat
try:
    import fcntl
except ImportError:  # Legacy text remains usable where Linux locks are absent.
    fcntl = None
import hashlib
import http.client
import socket
import tempfile
import urllib.parse
import json
import os
import html as html_lib
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# Сборка python.org на macOS не видит системные CA - без certifi любой https
# падает с CERTIFICATE_VERIFY_FAILED. Опциональность - как в asana-comments.py.
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

API = "https://app.asana.com/api/1.0"
DEFAULT_AUTH = Path.home() / ".config" / "asana" / "auth.json"
# Конфиг лежит в корне проекта, скрипт - в scripts/ под ним.
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / ".asana-blockers.json"

# Маркеры видны заказчику: Asana показывает описание как есть, HTML-комментарии
# в нем не прячутся, а читаются служебным мусором. Поэтому границы блока -
# человеческий заголовок и линейка.
MARK_START = "СВЯЗИ ЗАДАЧИ"
MARK_END = "- - -"

# assignee.gid просим явно: упоминание собирается из него, и полагаться на то,
# что Asana доложит gid к запрошенному assignee.name, тут нельзя - промах даст
# упоминание из None, то есть блок без исполнителя при живом исполнителе.
FIELDS = "name,notes,html_notes,completed,permalink_url,assignee.gid,assignee.name"
# Строки, которые пишем мы. Блок под нашим заголовком считается нашим, только
# если состоит из них: иначе человек, начавший описание словами "СВЯЗИ ЗАДАЧИ",
# потерял бы свой текст на первом же прогоне.
# Блок наш, только если состоит из пар "строка связи + голая ссылка" - ровно
# того, что мы генерируем. Более мягкая проверка (любая строка с нужным
# префиксом, любая начинающаяся с http) принимала за свой ручной текст и
# стирала его: человек пишет пояснение прямо к ссылке или начинает абзац
# словом "БЛОКИРУЕТ:".
# Направление связи пишется целым предложением, а не термином. Термин
# ("ЗАБЛОКИРОВАНА", "БЛОКИРУЕТ") читается в обе стороны одинаково правдоподобно,
# и на живом пользователе это подтвердилось: "непонятно, кто блокирует - та
# задача эту или эта ту". Ошибка при этом не видна - строка выглядит осмысленной
# при любом прочтении.
WAITS_HEAD = "ЭТА ЗАДАЧА ЖДЕТ - ее нельзя сделать, пока не закрыты:"
HOLDS_HEAD = "ЭТА ЗАДАЧА ДЕРЖИТ - они не сдвинутся, пока не закрыта эта:"
# Связь-происхождение: у отчетной задачи за выполненные работы блокировок нет
# по определению, а показать, из чего работа выросла, надо. Пишет эту секцию не
# этот скрипт, а asana-project (он подгружает отсюда каркас блока).
ORIGIN_HEAD = "СВЯЗАННЫЕ ЗАДАЧИ - откуда выросла эта работа:"
# Свои секции - только их этот скрипт перезаписывает; чужие известные сохраняет.
OWN_HEADS = (WAITS_HEAD, HOLDS_HEAD)
# Реестр всех заголовков технического блока. Узнавать надо ВСЕ, включая чужие:
# не узнав секцию соседа, скрипт не считает блок своим и кладет свой сверху -
# на карточке оказываются два блока. Узнав, но не сохранив, - стирает чужую
# работу. Отсюда две вещи сразу: общий реестр и посекционное слияние.
# Порядок в кортеже - канонический порядок секций в блоке: он же задает вывод,
# поэтому два скрипта, пишущие по очереди, не переставляют секции друг друга
# туда-сюда (иначе каждый прогон видел бы изменение и писал заново).
KNOWN_HEADS = (WAITS_HEAD, HOLDS_HEAD, ORIGIN_HEAD)
# Строка связи нового формата: пункт списка с упоминаниями. Опознавать ее по
# точному виду тега нельзя: мы отправляем короткое `<a data-asana-gid="2"/>`, а
# Asana при чтении разворачивает его в полный якорь с href, набором data-атрибутов
# и подставленным именем. Проверка идет от обратного: убираем все якоря и
# смотрим, что осталось - должен остаться только наш каркас строки.
# Самозакрывающаяся форма проверяется ПЕРВОЙ, а парная не должна начинаться
# с самозакрывающегося тега: иначе `<a .../>` совпадает с открывающей частью
# парного варианта и `.*?</a>` съедает весь текст до следующего якоря -
# вместе с чужими правками между ними.
ANCHOR = re.compile(r"<a\b[^>]*/>|<a\b[^>]*(?<!/)>.*?</a>", re.S)
GID_ATTR = re.compile(r'data-asana-gid="\d+"')
ITEM_REST = re.compile(r"^-\s*(?:-\s*(?:исполнитель не назначен)?)?\s*"
                       r"(?:,\s*(?:открыта|закрыта))?$")
# Старый формат (термин плюс голая ссылка) остается известен ЧТЕНИЮ: на досках
# уже лежат блоки в нем, и не узнать свой блок значит дописать второй сверху.
# Записываем только в новом.
OLD_BLOCKED = re.compile(r"^ЗАБЛОКИРОВАНА: .+ \((?:открыта|закрыта)\)$")
OLD_BLOCKS = re.compile(r"^БЛОКИРУЕТ: .+$")
OLD_TASK_URL = re.compile(r"^https?://(?:app\.)?asana\.com/\S+$")
# Невидимые символы уравниваем: иначе две визуально одинаковые задачи не дают
# коллизии, и связь молча уходит на ту из них, которая совпала побайтно.
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))
RETRY_CODES = {429, 500, 502, 503, 504}
RETRIES = 4
MAX_WAIT = 300


def retry_delay(headers, attempt: int) -> int:
    """Сколько ждать перед повтором. Retry-After бывает и числом, и HTTP-датой.

    Сырой int() на дате падал ValueError мимо контролируемого выхода - посреди
    записи, оставляя доску наполовину связанной. Верхняя граница нужна, чтобы
    не висеть вечно, но она заметно больше минуты: ранний повтор сам тратит
    квоту и продлевает блокировку.
    """
    raw = ""
    if headers:
        raw = str(headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return max(1, min(MAX_WAIT, int(raw)))
    if raw:
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(1, min(MAX_WAIT, int(when.timestamp() - time.time())))
        except (TypeError, ValueError):
            pass
    return min(MAX_WAIT, 2 ** attempt * 5)


def norm(s: str) -> str:
    """Имя задачи в сравнимом виде: NFC + обрезка краевых пробелов.

    Дословная сверка имен не должна ломаться о композитную форму (готовая "é"
    против "e" + акцент) - в Asana и в терминале они выглядят одинаково, и
    расхождение читалось бы как "задачи нет на доске".
    """
    return unicodedata.normalize("NFC", s).translate(ZERO_WIDTH).strip()


def load_token(auth_path: str | None) -> str:
    if auth_path is not None:
        if not auth_path.strip():
            sys.exit("Пустой путь --auth: укажи файл с токеном")
        p = Path(auth_path).expanduser()
        if not p.exists():
            sys.exit(f"Нет файла с токеном: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"Не читается {p}: {e}")
        value = data.get("token") if isinstance(data, dict) else None
        token = value.strip() if isinstance(value, str) else ""
        if not token:
            sys.exit(f"В {p} нет поля token")
        return token

    token = os.environ.get("ASANA_TOKEN", "").strip()
    if token:
        return token

    if DEFAULT_AUTH.exists():
        try:
            data = json.loads(DEFAULT_AUTH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"Не читается {DEFAULT_AUTH}: {e}")
        value = data.get("token") if isinstance(data, dict) else None
        token = value.strip() if isinstance(value, str) else ""
        if token:
            return token

    sys.exit(
        "Нет токена Asana. Создай PAT на https://app.asana.com/0/my-apps и положи так:\n"
        f"  mkdir -p {DEFAULT_AUTH.parent} && chmod 700 {DEFAULT_AUTH.parent}\n"
        f'  printf \'{{"token": "ТОКЕН"}}\\n\' > {DEFAULT_AUTH} && chmod 600 {DEFAULT_AUTH}\n'
        "либо укажи --auth <файл>, либо экспортируй ASANA_TOKEN."
    )


def _request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    """Один HTTP-вызов. Возвращает разобранное тело целиком (data + next_page)."""
    data = json.dumps({"data": payload}).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            # 429 на бесплатном тарифе ловится буднично (лимит по домену), а
            # обрыв на середине записи оставляет половину доски со связями и
            # половину без - одностороннюю картину, которая врет сильнее, чем
            # ее отсутствие. Поэтому ждем и повторяем, а не падаем сразу.
            if e.code in RETRY_CODES and attempt < RETRIES - 1:
                wait = retry_delay(e.headers, attempt)
                print(f"Asana API {e.code}, ждем {wait} с и повторяем "
                      f"({attempt + 1}/{RETRIES - 1})", file=sys.stderr)
                time.sleep(wait)
                continue
            sys.exit(f"Asana API {e.code} на {method} {url}: {body}")
        except urllib.error.URLError as e:
            if attempt < RETRIES - 1:
                wait = 2 ** attempt * 5
                print(f"Сеть недоступна ({e.reason}), ждем {wait} с и повторяем "
                      f"({attempt + 1}/{RETRIES - 1})", file=sys.stderr)
                time.sleep(wait)
                continue
            sys.exit(f"Сеть недоступна: {e.reason}")
    raise AssertionError("недостижимо")


def get_all(path: str, token: str) -> list[dict]:
    """GET коллекции с разматыванием пагинации (next_page).

    Без этого доска длиннее страницы отдается усеченной, и задача со второй
    страницы выглядит как несуществующая - прогон падает на валидном конфиге.
    Тот же прием, что в asana-project.py и asana-comments.py.
    """
    sep = "&" if "?" in path else "?"
    url = f"{API}{path}{sep}limit=100"
    items: list[dict] = []
    while url:
        body = _request("GET", url, token)
        items.extend(body.get("data", []))
        url = (body.get("next_page") or {}).get("uri")
    return items


def _no_dupe_keys(pairs):
    """Повторяющийся ключ в JSON Python молча схлопывает: последний побеждает.

    Для конфига это тихая потеря связи (две цепочки под одним именем) или, что
    хуже, подмена gid доски у того же человекочитаемого ключа.
    """
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"повторяющийся ключ: {k!r}")
        out[k] = v
    return out


def load_config(path: str) -> dict:
    """Читает, проверяет и нормализует конфиг ДО первого сетевого вызова.

    Имена в возвращенном конфиге уже приведены norm(): дальше по коду сравнение
    и рендер идут по одной и той же форме. Прежняя версия нормализовала имена
    при сверке, но брала цепочку по сырому ключу - ключ с краевым пробелом
    проходил проверку, прямая сторона связи тихо не проставлялась, а обратная
    проставлялась, и прогон рапортовал успех.
    """
    p = Path(path).expanduser()
    if not p.exists():
        sys.exit(f"Нет конфига цепочек: {p}\nФормат - в шапке скрипта и в скилле asana-blockers.")
    try:
        boards = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=_no_dupe_keys)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        sys.exit(f"Не читается {p}: {e}")
    if not isinstance(boards, dict) or not boards:
        sys.exit(f"В {p} ожидается непустой объект вида {{\"<ключ доски>\": {{...}}}}")

    problems: list[str] = []
    clean: dict[str, dict] = {}
    for label, cfg in boards.items():
        where = f"доска '{label}'"
        if not isinstance(cfg, dict):
            problems.append(f"{where}: ожидается объект с полями project и chains")
            continue
        project = cfg.get("project")
        # gid - число: URL доски или строка с параметрами дала бы не тот запрос
        # (а на нескольких досках - уже после записи в предыдущие).
        if not isinstance(project, str) or not re.fullmatch(r"[0-9]+", project.strip()):
            problems.append(f"{where}: project - gid доски числом строкой "
                            f"(последнее число в ее URL), а не {project!r}")
        chains = cfg.get("chains")
        if not isinstance(chains, dict):
            problems.append(f"{where}: нет поля chains (объект 'задача' -> [зависимости])")
            continue

        norm_chains: dict[str, list[str]] = {}
        for who, needs in chains.items():
            reason = bad_name(who)
            if reason:
                problems.append(f"{where}: имя задачи {who!r} - {reason}")
                continue
            key = norm(who)
            if key in norm_chains:
                problems.append(f"{where}: имя '{key}' встречается дважды "
                                f"(различаются только пробелами или формой записи)")
                continue
            if not isinstance(needs, list) or not needs:
                problems.append(f"{where}: у '{who}' ожидается непустой список зависимостей")
                continue
            seen: list[str] = []
            for n in needs:
                reason = bad_name(n)
                if reason:
                    problems.append(f"{where}, '{who}': зависимость {n!r} - {reason}")
                    continue
                dep = norm(n)
                if dep == key:
                    problems.append(f"{where}: задача '{key}' зависит сама от себя")
                elif dep in seen:
                    problems.append(f"{where}, '{who}': зависимость повторяется: '{dep}'")
                else:
                    seen.append(dep)
            if seen:
                norm_chains[key] = seen
        clean[label] = {"project": (project or "").strip(), "chains": norm_chains}
        if "mode" in cfg:
            if cfg["mode"] not in ("auto", "native", "text"):
                problems.append(f"{where}: mode должен быть auto/native/text")
            else:
                clean[label]["mode"] = cfg["mode"]
    if problems:
        sys.exit("Конфиг цепочек неверен:\n  " + "\n  ".join(problems))
    return clean


def bad_name(name) -> str | None:
    """Почему имя задачи не годится для конфига (или None, если годится).

    Перевод строки внутри имени рвет блок изнутри: строка-разделитель, попавшая
    в имя, обрывает наш же блок на следующем прогоне, остаток читается как текст
    человека и дописывается снова - описание растет с каждым прогоном.
    """
    if not isinstance(name, str) or not name.strip():
        return "ожидается непустая строка"
    # splitlines() режет не только по \n и \r, но и по \v, \f, NEL, U+2028,
    # U+2029 - имя с любым из них рвет наш блок изнутри на следующем прогоне
    if len(name.splitlines()) != 1:
        return "перевод строки внутри имени недопустим"
    return None


def is_item_line(line: str) -> bool:
    """Пункт связи: одно-два упоминания и наш каркас вокруг них.

    Форма якоря значения не имеет (короткая наша или развернутая от Asana), но
    ручная приписка внутри строки делает ее чужой - там текст человека.
    """
    if not GID_ATTR.search(line):
        return False
    return bool(ITEM_REST.match(ANCHOR.sub("", line).strip()))


def _is_ours_new(inner: str) -> bool:
    """Блок нового формата: заголовки из реестра и пункты с упоминаниями.

    Первой строкой обязан идти заголовок. Требование не косметическое: без него
    блок может начинаться с пункта, не относящегося ни к одной секции, и разбор
    на секции перестает быть полным - такой пункт пришлось бы либо молча
    выбросить при слиянии, либо приписать чужой секции.
    """
    lines = [ln.strip() for ln in inner.split("\n") if ln.strip()]
    if not lines or lines[0] not in KNOWN_HEADS:
        return False
    heads = 0
    for ln in lines:
        if ln in KNOWN_HEADS:
            heads += 1
        elif not is_item_line(ln):
            return False
    return len(lines) > heads


def parse_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Строки блока -> [(заголовок, пункты)]. Вход - только опознанный блок."""
    out: list[tuple[str, list[str]]] = []
    for ln in lines:
        text = ln.strip()
        if not text:
            continue
        if text in KNOWN_HEADS:
            out.append((text, []))
        elif out:
            out[-1][1].append(text)
    return out


def merge_sections(old_lines: list[str], own_heads: tuple[str, ...],
                   own_lines: list[str]) -> list[str]:
    """Свои секции заменить, чужие известные сохранить, порядок - канонический.

    Пустая своя секция означает "связей этого типа больше нет" и просто
    исчезает; если после этого не осталось ни одной секции, блока не будет
    вовсе - это и есть снятие блока.
    """
    kept: list[tuple[str, list[str]]] = []
    for head, items in parse_sections(old_lines):
        # Одноименные секции внутри блока сводим в одну, СКЛЕИВАЯ пункты.
        # Первая редакция этой правки оставляла первую секцию и выбрасывала
        # остальные - то есть чинила косметику ценой молчаливой потери связей
        # из второго экземпляра.
        if head in own_heads:
            continue
        same = next((k for k in kept if k[0] == head), None)
        if same is None:
            kept.append((head, list(items)))
            continue
        # Дедуп по УПОМИНАНИЯМ, а не по тексту строки: короткий тег, который
        # пишем мы, и развернутый якорь, который отдает Asana, - одна и та же
        # связь, но побайтно разные строки
        have = {gid_form(i) for i in same[1]}
        for i in items:
            if gid_form(i) not in have:
                have.add(gid_form(i))
                same[1].append(i)
    kept += [sec for sec in parse_sections(own_lines) if sec[1]]
    kept.sort(key=lambda sec: KNOWN_HEADS.index(sec[0]))
    out: list[str] = []
    for head, items in kept:
        out.append(head)
        out.extend(items)
    return out


def block_lines(block_txt: str | None) -> list[str]:
    """Внутренние строки нашего блока (без маркеров) - вход для merge_sections."""
    if not block_txt:
        return []
    lines = block_txt.strip("\n").split("\n")
    if lines and lines[0].strip() == MARK_START:
        lines = lines[1:]
    while lines and lines[-1].strip() in ("", MARK_END):
        lines = lines[:-1]
    return [ln for ln in (x.strip() for x in lines) if ln]


def gid_form(text: str) -> str:
    """Описание в сравнимом виде: любое упоминание сводится к <@gid>."""
    def one(m):
        found = GID_ATTR.search(m.group(0))
        return f"<@{found.group(0)}>" if found else m.group(0)
    return ANCHOR.sub(one, text).strip("\n")


def plain(line: str) -> str:
    """Строка без разметки: Asana сама превращает голый URL в якорь, а символы
    вроде & хранит сущностью, поэтому старый блок в html_notes выглядит иначе,
    чем мы его писали."""
    return html_lib.unescape(ANCHOR.sub(lambda m: re.sub(r"<[^>]+>", "", m.group(0)),
                                        line)).strip()


def _is_ours_old(inner: str) -> bool:
    """Блок первой редакции: пары "термин + ссылка на задачу Asana"."""
    lines = [plain(ln) for ln in inner.split("\n") if ln.strip()]
    if not lines or len(lines) % 2:
        return False
    for head, url in zip(lines[::2], lines[1::2]):
        if not (OLD_BLOCKED.match(head) or OLD_BLOCKS.match(head)):
            return False
        # Ссылка обязана вести в Asana: любой URL принимать нельзя, иначе под
        # определение попадает ручной блок вида "БЛОКИРУЕТ: сверить договор" со
        # ссылкой на чужой сайт - и он будет стерт как наш.
        if not OLD_TASK_URL.match(url):
            return False
    return True


def _is_ours(inner: str) -> bool:
    """Наш ли блок - в любом из двух форматов.

    Старый узнается ради миграции: на досках уже стоят блоки первой редакции, и
    если перестать их опознавать, прогон допишет второй блок сверху вместо
    замены. Перезаписан он будет уже в новом формате.
    """
    return _is_ours_new(inner) or _is_ours_old(inner)


def split_block(notes: str) -> tuple[str | None, str]:
    """Делит описание на наш блок (или None) и остальной текст.

    Блок ищется только в начале описания и только целиком - иначе регулярка
    съела бы кусок осмысленного текста, если в нем встретится такая же линейка.
    Дополнительно проверяется, что блок НАШ (см. _is_ours): описание, начатое
    человеком теми же словами, иначе было бы стерто.
    """
    m = re.match(r"^" + re.escape(MARK_START) + r"\n(.*?)\n" + re.escape(MARK_END) + r"(?:\n|$)\n*",
                 notes, flags=re.S)
    if not m or not _is_ours(m.group(1)):
        return None, notes
    return m.group(0), notes[m.end():]


def render(lines: list[str], rest: str) -> str:
    """Итоговое описание: блок связей сверху, прежний текст под ним.

    Ведущие пустые строки остатка срезаются: иначе каждый прогон добавлял бы к
    ним свои две, и "изменений нет" наступало бы только с третьего раза.
    """
    rest = rest.lstrip("\n")
    if not lines:
        return rest.rstrip("\n")
    block = MARK_START + "\n" + "\n".join(lines) + "\n" + MARK_END
    return (block + "\n\n" + rest).rstrip("\n")


BODY = re.compile(r"^\s*<body>(.*)</body>\s*$", re.S | re.I)


def body_of(task: dict) -> str:
    """Содержимое описания задачи без обертки <body>, CRLF приведены к LF.

    Работаем с html_notes, а не с notes: во-первых, упоминания живут только там
    (в плоском тексте они превратились бы в сырой тег), во-вторых, запись
    плоского notes сбрасывала бы оформление чужого описания - тот самый дефект,
    который в первой редакции приходилось закрывать отдельным гейтом.
    """
    html = (task.get("html_notes") or "").replace("\r\n", "\n")
    m = BODY.match(html)
    return (m.group(1) if m else html).strip("\n")


def wrap(inner: str) -> str:
    """Обратная обертка для записи: Asana принимает html_notes только целиком."""
    return f"<body>{inner}</body>"


def readable(text: str, names: dict[str, str]) -> str:
    """Упоминания в человеческие имена - для предпросмотра.

    Предпросмотр тут единственный канал контроля перед записью, а строку из
    gid-ов проверить глазами невозможно: записываем упоминания, показываем имена.
    """
    return re.sub(r'<a data-asana-gid="(\d+)"\s*/>',
                  lambda m: names.get(m.group(1), f"gid {m.group(1)}"), text)


def mention(gid: str) -> str:
    """Упоминание Asana. Имя подставляет трекер, поэтому оно всегда актуально."""
    return f'<a data-asana-gid="{gid}"/>'


def who(task: dict) -> str:
    """Исполнитель задачи упоминанием - или честная строка про его отсутствие.

    Разница между "ничей" и "мой" важнее аккуратности строки: увидев блокировку,
    человек первым делом хочет знать, ждет он своего шага или чужого.
    """
    assignee = task.get("assignee") or {}
    gid = assignee.get("gid")
    return mention(gid) if gid else "исполнитель не назначен"


def build_lines(name: str, chains: dict, blocks: dict, by_name: dict) -> list[str]:
    """Строки блока для одной задачи: сперва чего ждет она, потом кого держит.

    Статус (открыта/закрыта) ставится только тем, кого ждем: у задач, которые
    держим мы, он избыточен и мешает читать.
    """
    lines = []
    waits = [by_name[n] for n in chains.get(name, [])]
    holds = [by_name[n] for n in blocks.get(name, [])]
    if waits:
        lines.append(WAITS_HEAD)
        for dep in waits:
            state = "закрыта" if dep.get("completed") else "открыта"
            lines.append(f"- {mention(dep['gid'])} - {who(dep)}, {state}")
    if holds:
        lines.append(HOLDS_HEAD)
        for dep in holds:
            lines.append(f"- {mention(dep['gid'])} - {who(dep)}")
    return lines


def run_board(label: str, cfg: dict, token: str, send: bool) -> tuple[int, bool]:
    """Возвращает (сколько задач затронуто, была ли ошибка на этой доске)."""
    project, chains = cfg["project"], cfg["chains"]
    tasks = get_all(f"/projects/{project}/tasks?opt_fields={FIELDS}", token)

    mentioned = {n for who, needs in chains.items() for n in [who] + needs}
    by_name: dict[str, dict] = {}
    dupes: set[str] = set()
    for t in tasks:
        name = norm(t.get("name") or "")
        if name in by_name:
            dupes.add(name)
        by_name[name] = t

    unknown = sorted(mentioned - set(by_name))
    collisions = sorted(dupes & mentioned)
    if unknown or collisions:
        # Молча пропустить связь хуже, чем упасть: пропущенная связь на доске
        # неотличима от ее отсутствия (rules/silent-failure.md).
        if unknown:
            print("нет таких задач на доске (переименованы?):", file=sys.stderr)
            for n in unknown:
                print(f"   {n}", file=sys.stderr)
        if collisions:
            print("на доске несколько задач с таким именем - какую связывать, неоднозначно:",
                  file=sys.stderr)
            for n in collisions:
                print(f"   {n}", file=sys.stderr)
        print(f"доска '{label}' пропущена, ничего не записано", file=sys.stderr)
        return 0, True

    # обратная сторона связи: кого держит эта задача
    blocks: dict[str, list[str]] = {}
    for who, needs in chains.items():
        for n in needs:
            blocks.setdefault(n, []).append(who)

    # обход идет по списку задач, а не по by_name: одноименные задачи вне
    # цепочек схлопнулись бы, и на второй из них остался бы висеть старый блок
    # own - только наши секции, merged - весь блок вместе с чужими известными.
    # Храним оба: предпросмотр показывает итоговый блок, а запись пересобирает
    # его заново поверх свежего описания (чужая секция могла измениться).
    plan: list[tuple[str, str, list[str], list[str], str]] = []
    for t in tasks:
        name = norm(t.get("name") or "")
        own = build_lines(name, chains, blocks, by_name)
        notes = body_of(t)
        block_txt, rest = split_block(notes)
        if not own and block_txt is None:
            continue  # ни связей, ни нашего блока - задача не наша, не трогаем
        merged = merge_sections(block_lines(block_txt), OWN_HEADS, own)
        fresh = render(merged, rest)
        if gid_form(fresh) != gid_form(notes):
            plan.append((t["gid"], name, own, merged, notes))

    # Предпросмотр показывает имена, а не gid: это единственный канал контроля
    # перед записью, а строку из идентификаторов глазами не проверить.
    names = {t["gid"]: norm(t.get("name") or "") for t in tasks if t.get("name")}
    for t in tasks:
        assignee = t.get("assignee") or {}
        if assignee.get("gid") and assignee.get("name"):
            names[assignee["gid"]] = assignee["name"]

    print(f"задач с изменениями: {len(plan)}")
    for _gid, name, _own, lines, notes in plan:
        print(f"\n--- {name}")
        if not lines:
            print("    СНЯТЬ БЛОК СВЯЗЕЙ (связи убраны из конфига), остается только текст ниже него:")
            block_txt, _rest = split_block(notes)
            for line in readable(block_txt or "", names).splitlines():
                print(f"      | {line}")
        for line in lines:
            print(f"    {readable(line, names)}")
        # Блок под нашим заголовком, который мы своим не считаем, останется в
        # описании, а новый ляжет сверху - на карточке будет две версии связей.
        # Молча этого делать нельзя: предпросмотр тут единственный канал контроля.
        if lines and MARK_START in notes and split_block(notes)[0] is None:
            print("    внимание: в описании уже есть блок связей, который мы не считаем "
                  "своим (в нем есть ручные правки?) - новый ляжет сверху, старый останется")

    if not send:
        if plan:
            print("\nэто предпросмотр; чтобы записать - повторить с --send")
        return len(plan), False

    written, skipped = 0, 0
    for gid, name, own, _merged, notes in plan:
        # Перечитываем задачу прямо перед записью: между предпросмотром и
        # записью ее мог поменять человек, а PUT перезаписывает поле целиком -
        # без этого чужая правка молча терялась бы. У Asana нет ни ETag, ни
        # условной записи, поэтому окно сужаем, а не закрываем; все, что
        # разошлось со снимком не по тексту, а по сути (имя, доска), - повод
        # не писать вовсе.
        cur = _request("GET", f"{API}/tasks/{gid}?opt_fields=html_notes,name,projects",
                       token).get("data")
        if not isinstance(cur, dict) or not isinstance(cur.get("html_notes"), str):
            print(f"'{name}': неполный ответ Asana при перечитывании - пропускаем",
                  file=sys.stderr)
            skipped += 1
            continue
        if norm(cur.get("name") or "") != name:
            print(f"'{name}': задачу переименовали после предпросмотра - пропускаем",
                  file=sys.stderr)
            skipped += 1
            continue
        if project not in {(pr or {}).get("gid") for pr in cur.get("projects") or []}:
            print(f"'{name}': задачи больше нет на этой доске - пропускаем", file=sys.stderr)
            skipped += 1
            continue
        cur_notes = body_of(cur)
        if gid_form(cur_notes) != gid_form(notes):
            print(f"описание '{name}' изменилось после предпросмотра - "
                  f"блок накладываем на новую версию", file=sys.stderr)
        cur_block, rest = split_block(cur_notes)
        fresh = render(merge_sections(block_lines(cur_block), OWN_HEADS, own), rest)
        if gid_form(fresh) == gid_form(cur_notes):
            continue  # кто-то уже привел описание к нужному виду
        _request("PUT", f"{API}/tasks/{gid}", token, {"html_notes": wrap(fresh)})
        written += 1
    print(f"\nзаписано: {written}" + (f", пропущено: {skipped}" if skipped else ""))
    return written, bool(skipped)


# Native mode has a separate single-request transport: mutation retries and
# legacy response-body diagnostics are unsafe for an ownership journal.
class NativeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Asana native: {code}" if isinstance(code, int) else
                         "Asana native: " + (code if code in {
                             'timeout', 'network', 'api_data', 'state_invalid',
                             'state_io', 'drift', 'cycle', 'limit', 'scope',
                             'lock_busy', 'path', 'unavailable'} else 'unavailable'))


def _native_canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def _native_hash(value):
    return hashlib.sha256(_native_canonical(value)).hexdigest()


class _NativeNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NATIVE_BUDGET = contextvars.ContextVar('asana_native_read_budget', default=None)


def native_request(method, path, token, payload=None):
    parts = urllib.parse.urlsplit(path)
    if not path.startswith('/') or parts.scheme or parts.netloc or parts.fragment:
        raise NativeError('path')
    data = None if payload is None else json.dumps({'data': payload}).encode()
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + token,
                                          'Content-Type': 'application/json'})
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CTX), _NativeNoRedirect())
        with opener.open(req, timeout=30) as response:
            if response.url != API + path:
                raise NativeError('api_data')
            declared = response.headers.get('Content-Length')
            if declared is not None:
                declared = int(declared)
                if declared < 0:
                    raise NativeError('api_data')
                if declared > 2 * 1024 * 1024:
                    raise NativeError('limit')
            raw = response.read(2 * 1024 * 1024 + 1)
            if declared is not None and len(raw) < declared:
                raise NativeError('network')
            if len(raw) > 2 * 1024 * 1024:
                raise NativeError('limit')
            result = json.loads(raw or b'{}', object_pairs_hook=_no_dupe_keys)
            if not isinstance(result, dict):
                raise NativeError('api_data')
            return result
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise NativeError(code) from None
    except (TimeoutError, socket.timeout):
        raise NativeError('timeout') from None
    except urllib.error.URLError as exc:
        raise NativeError('timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'network') from None
    except (http.client.HTTPException, OSError):
        raise NativeError('network') from None
    except (ValueError, UnicodeError):
        raise NativeError('api_data') from None


class _NativeRead:
    def __init__(self, token):
        self.token, self.requests = token, 0

    def request(self, path):
        self.requests += 1
        budget = _NATIVE_BUDGET.get()
        if budget is not None:
            budget[0] += 1
        if (budget[0] if budget is not None else self.requests) > 10000:
            raise NativeError('limit')
        result = native_request('GET', path, self.token)
        if not isinstance(result, dict) or 'data' not in result:
            raise NativeError('api_data')
        return result

    def item(self, path):
        value = self.request(path)['data']
        if not isinstance(value, dict):
            raise NativeError('api_data')
        return value

    def collection(self, path, cap=1000):
        initial = urllib.parse.urlsplit(path)
        route = initial.path
        filters = [(k, v) for k, v in urllib.parse.parse_qsl(initial.query, keep_blank_values=True) if k not in ('limit', 'offset')]
        seen, result = set(), []
        while path:
            if path in seen:
                raise NativeError('api_data')
            seen.add(path)
            value = self.request(path)
            if not isinstance(value['data'], list):
                raise NativeError('api_data')
            result.extend(value['data'])
            if len(result) > cap or len(seen) > 100:
                raise NativeError('limit')
            nxt = value.get('next_page')
            if not nxt:
                break
            if not isinstance(nxt, dict) or not isinstance(nxt.get('uri'), str):
                raise NativeError('api_data')
            url = urllib.parse.urlsplit(nxt['uri'])
            if (url.scheme != 'https' or url.netloc != 'app.asana.com'
                    or url.path != '/api/1.0' + route or url.fragment
                    or [(k, v) for k, v in urllib.parse.parse_qsl(url.query, keep_blank_values=True) if k not in ('limit', 'offset')] != filters):
                raise NativeError('api_data')
            path = route + ('?' + url.query if url.query else '')
        return result


def _native_gid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
        raise NativeError('api_data')
    return value


def _native_ids(rows):
    ids = [_native_gid(x.get('gid')) for x in rows if isinstance(x, dict)]
    if len(ids) != len(rows) or len(ids) != len(set(ids)):
        raise NativeError('api_data')
    return sorted(ids)


def _native_state(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'scopes'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['scopes'], dict)):
        raise NativeError('state_invalid')
    for scope, row in value['scopes'].items():
        if (not isinstance(scope, str) or not re.fullmatch(r'[0-9]+:[0-9]+:[0-9]+', scope)
                or not isinstance(row, dict) or set(row) - {'edges', 'cleanup'}
                or not isinstance(row.get('edges'), dict)):
            raise NativeError('state_invalid')
        for edge, status in row['edges'].items():
            if (not isinstance(edge, str) or not re.fullmatch(r'[0-9]+>[0-9]+', edge)
                    or status not in ('owned', 'pending_add', 'pending_remove', 'uncertain')):
                raise NativeError('state_invalid')
        if not isinstance(row.get('cleanup', {}), dict):
            raise NativeError('state_invalid')
        for gid, mark in row.get('cleanup', {}).items():
            if (not isinstance(gid, str) or not re.fullmatch('[0-9]+', gid) or not isinstance(mark, dict)
                    or set(mark) != {'state', 'before', 'after'}
                    or mark['state'] not in ('pending', 'verified')
                    or any(not isinstance(mark[k], str) or not re.fullmatch('[0-9a-f]{64}', mark[k])
                           for k in ('before', 'after'))):
                raise NativeError('state_invalid')
    return value


def _native_path(path):
    p = Path(path)
    if not p.is_absolute() or '..' in p.parts or not p.parent.is_dir():
        raise NativeError('path')
    if any(x.is_symlink() for x in [p, *p.parents]):
        raise NativeError('path')
    if p.exists() and (not p.is_file() or p.stat().st_nlink != 1):
        raise NativeError('path')
    if (p.is_relative_to(Path.cwd().resolve()) or any(
            (parent / '.AI/project.json').is_file() or (parent / '.stfolder').exists()
            or (parent / '.obsidian').is_dir() for parent in p.parents)):
        raise NativeError('path')
    return p


def _native_read_state(path):
    if not path.exists():
        return {'schema_version': 1, 'scopes': {}}
    try:
        if path.stat().st_size > 1024 * 1024:
            raise NativeError('state_invalid')
        return _native_state(json.loads(path.read_text(), object_pairs_hook=_no_dupe_keys))
    except (OSError, ValueError):
        raise NativeError('state_invalid') from None


def _native_atomic(path, value):
    path = _native_path(path)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix='.asana-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_native_canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        temp = None
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        raise NativeError('state_io') from None
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def write_native_state(path, state):
    _native_state(state)
    _native_atomic(path, state)


_NATIVE_SIGNING = frozenset(('X-Amz-Algorithm', 'X-Amz-Credential', 'X-Amz-Date',
    'X-Amz-Expires', 'X-Amz-SignedHeaders', 'X-Amz-Signature', 'X-Amz-Security-Token'))
_NATIVE_URL = re.compile(r'https?://[^\s<>"\x27]+')


def _native_url_core(url):
    url = html_lib.unescape(url)
    p = urllib.parse.urlsplit(url)
    if p.fragment or p.scheme not in ('http', 'https'):
        return None
    fields = p.query.split('&')
    keys = [x.split('=', 1)[0] for x in fields]
    if len(keys) != len(set(keys)) or not (_NATIVE_SIGNING & set(keys)):
        return None
    query = '&'.join(x.split('=', 1)[0] + '=SIGNING' if x.split('=', 1)[0] in _NATIVE_SIGNING else x
                     for x in fields)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, query, ''))


def _native_description(task, read):
    if not isinstance(task, dict) or not isinstance(task.get('html_notes'), str):
        raise NativeError('api_data')
    _native_gid(task.get('gid'))
    html = task['html_notes']
    mappings, bindings = {}, []
    for row in read.collection('/attachments?parent=' + task['gid'] + '&limit=100', cap=100):
        gid = _native_gid(row.get('gid'))
        data = read.item('/attachments/' + gid + '?opt_fields=gid,download_url')
        if data.get('gid') != gid:
            raise NativeError('api_data')
        url = data.get('download_url')
        if url is None:
            bindings.append((gid, 'unavailable'))
            continue
        if not isinstance(url, str):
            raise NativeError('api_data')
        core = _native_url_core(url)
        bindings.append((gid, hashlib.sha256((core if core is not None else url).encode()).hexdigest()))
        if core is not None:
            if core in mappings and mappings[core] != gid:
                raise NativeError('api_data')
            mappings[core] = gid
    def replace(match):
        core = _native_url_core(match.group())
        if core is not None and core in mappings:
            return 'ATTACHMENT:' + mappings[core] + ':' + hashlib.sha256(core.encode()).hexdigest()
        return match.group()
    normalized = _NATIVE_URL.sub(replace, html)
    return hashlib.sha256(normalized.encode()).hexdigest(), sorted(bindings)


def _native_cleanup(task, desired):
    notes = body_of(task)
    block, rest = split_block(notes)
    if block is None:
        return None
    sections = parse_sections(block_lines(block))
    removable = []
    for head, items in sections:
        if head not in OWN_HEADS:
            continue
        edges = []
        for line in items:
            ids = re.findall(r'data-asana-gid="([0-9]+)"', line)
            if not ids:
                return None
            pair = (task['gid'], ids[0]) if head == WAITS_HEAD else (ids[0], task['gid'])
            edges.append(pair)
        if edges and all(e in desired for e in edges):
            removable.append(head)
    if not removable:
        return None
    remaining = [line for head, items in sections if head not in removable for line in [head, *items]]
    return wrap(render(remaining, rest))


def _native_snapshot(label, cfg, token, state, cleanup_text):
    _native_state(state)
    try:
        project = _native_gid(cfg['project']); chains = cfg['chains']
        if not isinstance(chains, dict):
            raise NativeError('scope')
    except (KeyError, TypeError):
        raise NativeError('scope') from None
    read = _NativeRead(token)
    account = _native_gid(read.item('/users/me?opt_fields=gid').get('gid'))
    project_data = read.item('/projects/' + project + '?opt_fields=gid,workspace.gid')
    if project_data.get('gid') != project:
        raise NativeError('scope')
    workspace = _native_gid((project_data.get('workspace') or {}).get('gid'))
    scope = {'account': account, 'workspace': workspace, 'project': project}
    key = ':'.join((account, workspace, project))
    board = read.collection('/projects/' + project + '/tasks?opt_fields=' + FIELDS + ',projects,workspace.gid')
    board_ids = _native_ids(board)
    names = {}
    for t in board:
        name = norm(t.get('name') or '')
        names.setdefault(name, []).append(t['gid'])
    desired = set()
    for name, needs in chains.items():
        if not isinstance(name, str) or not isinstance(needs, list) or not needs or any(not isinstance(n, str) for n in needs) or len(needs) != len(set(needs)):
            raise NativeError('scope')
        for dep in needs:
            if not isinstance(dep, str) or len(names.get(norm(name), [])) != 1 or len(names.get(norm(dep), [])) != 1:
                raise NativeError('scope')
            a, b = names[norm(name)][0], names[norm(dep)][0]
            if a == b:
                raise NativeError('cycle')
            desired.add((a, b))
    owned = state['scopes'].get(key, {}).get('edges', {})
    queue, tasks, deps, dependents, descriptions = list(board_ids), {}, {}, {}, {}
    while queue:
        gid = queue.pop()
        if gid in tasks:
            continue
        if len(tasks) >= 1000:
            raise NativeError('limit')
        t = read.item('/tasks/' + gid + '?opt_fields=gid,name,html_notes,projects,workspace.gid')
        if (t.get('gid') != gid or not isinstance(t.get('name'), str)
                or not isinstance(t.get('html_notes'), str)):
            raise NativeError('api_data')
        if (not isinstance(t.get('projects'), list) or any(not isinstance(x, dict) or not isinstance(x.get('gid'), str) for x in t['projects'])
                or not isinstance(t.get('workspace'), dict)):
            raise NativeError('api_data')
        if gid in board_ids and (project not in {x.get('gid') for x in t.get('projects', [])}
                or (t.get('workspace') or {}).get('gid') != workspace):
            raise NativeError('scope')
        tasks[gid] = t
        deps[gid] = _native_ids(read.collection('/tasks/' + gid + '/dependencies'))
        dependents[gid] = _native_ids(read.collection('/tasks/' + gid + '/dependents'))
        queue.extend(g for g in deps[gid] if g not in tasks)
        desc_hash, bindings = _native_description(t, read)
        descriptions[gid] = {'name': t['name'], 'projects': sorted(x.get('gid', '') for x in t.get('projects', [])),
                             'workspace': (t.get('workspace') or {}).get('gid'),
                             'description': desc_hash, 'attachments': bindings}
    existing = {(a, b) for a, bs in deps.items() for b in bs}
    add = desired - existing
    remove = {tuple(edge.split('>')) for edge, status in owned.items()
              if status == 'owned' and tuple(edge.split('>')) in existing - desired}
    for a, b in desired | remove:
        if a not in board_ids or b not in board_ids:
            raise NativeError('scope')
    for a, b in remove:
        if {x.get('gid') for x in tasks[a].get('projects', [])} != {project}:
            raise NativeError('scope')
    graph = {g: set(bs) for g, bs in deps.items()}
    incoming = {g: set(bs) for g, bs in dependents.items()}
    for a, b in remove:
        graph[a].discard(b); incoming[b].discard(a)
    for a, b in add:
        graph[a].add(b); incoming[b].add(a)
    if any(len(graph[g]) + len(incoming[g]) > 30 for g in graph):
        raise NativeError('limit')
    indegree = {g: 0 for g in graph}
    for bs in graph.values():
        for b in bs:
            indegree[b] += 1
    ready = [g for g, n in indegree.items() if n == 0]
    count = 0
    while ready:
        g = ready.pop(); count += 1
        for b in graph[g]:
            indegree[b] -= 1
            if indegree[b] == 0:
                ready.append(b)
    if count != len(graph):
        raise NativeError('cycle')
    cleanup = {}
    diagnostics = []
    if cleanup_text:
        for gid in board_ids:
            value = _native_cleanup(tasks[gid], desired)
            if value is not None:
                if {x.get('gid') for x in tasks[gid].get('projects', [])} != {project}:
                    raise NativeError('scope')
                cleanup[gid] = value
            elif MARK_START in body_of(tasks[gid]):
                diagnostics.append({'task': gid, 'code': 'text_retained'})
    snapshot = {'tasks': descriptions, 'deps': deps, 'dependents': dependents}
    plan = {'schema_version': 1, 'label': label, 'cfg': copy.deepcopy(cfg),
            'scope': scope, 'desired': sorted(map(list, desired)),
            'add': sorted(map(list, add)), 'remove': sorted(map(list, remove)),
            'cleanup': sorted(cleanup), 'cleanup_text': cleanup_text,
            'snapshots': snapshot, 'state_sha256': _native_hash(state), 'diagnostics': diagnostics}
    plan['digest'] = _native_hash(plan)
    return plan, tasks, cleanup


def build_native_plan(label, cfg, token, state, cleanup_text=False):
    budget = _NATIVE_BUDGET.set([0])
    try:
        return _native_snapshot(label, cfg, token, state, cleanup_text)[0]
    finally:
        _NATIVE_BUDGET.reset(budget)


def apply_native_plan(plan, token, state_path, expected_digest):
    required = {'schema_version', 'label', 'cfg', 'scope', 'desired', 'add', 'remove',
                'cleanup', 'cleanup_text', 'snapshots', 'state_sha256', 'diagnostics', 'digest'}
    if (not isinstance(plan, dict) or set(plan) != required
            or type(plan['schema_version']) is not int or plan['schema_version'] != 1
            or not isinstance(plan['label'], str) or not plan['label'].strip()
            or not isinstance(plan['cfg'], dict) or type(plan['cleanup_text']) is not bool):
        raise NativeError('drift')
    unsigned = {k: v for k, v in plan.items() if k != 'digest'}
    if expected_digest != plan.get('digest') or expected_digest != _native_hash(unsigned):
        raise NativeError('drift')
    if fcntl is None or not sys.platform.startswith('linux'):
        raise NativeError('unavailable')
    path = _native_path(state_path)
    try:
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise NativeError('path') from None
    budget = _NATIVE_BUDGET.set([0])
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise NativeError('path')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NativeError('lock_busy') from None
        state = _native_read_state(path)
        fresh, tasks, cleanup = _native_snapshot(plan['label'], plan['cfg'], token, state, plan['cleanup_text'])
        if fresh['digest'] != expected_digest:
            raise NativeError('drift')
        key = ':'.join(fresh['scope'][k] for k in ('account', 'workspace', 'project'))
        expected = copy.deepcopy(fresh['snapshots'])
        counts = dict(added=0, removed=0, cleaned=0)
        def persist():
            try:
                write_native_state(str(path), state)
            except OSError:
                raise NativeError('state_io') from None
        def check_snapshot():
            new, current_tasks, current_cleanup = _native_snapshot(fresh['label'], fresh['cfg'], token, state, fresh['cleanup_text'])
            if new['snapshots'] != expected or new['scope'] != fresh['scope']:
                raise NativeError('drift')
            return current_tasks, current_cleanup
        for mode in ('remove', 'add'):
            for a, b in fresh[mode]:
                check_snapshot()
                row = state['scopes'].setdefault(key, {'edges': {}})
                edge = a + '>' + b
                row['edges'][edge] = 'pending_' + mode
                persist()
                native_request('POST', '/tasks/' + a + '/' + mode + 'Dependencies', token, {'dependencies': [b]})
                read = _NativeRead(token)
                after_deps = _native_ids(read.collection('/tasks/' + a + '/dependencies'))
                after_in = _native_ids(read.collection('/tasks/' + b + '/dependents'))
                ds, ins = set(expected['deps'][a]), set(expected['dependents'][b])
                if mode == 'add':
                    ds.add(b); ins.add(a)
                else:
                    ds.discard(b); ins.discard(a)
                if after_deps != sorted(ds) or after_in != sorted(ins):
                    raise NativeError('drift')
                expected['deps'][a], expected['dependents'][b] = after_deps, after_in
                if mode == 'add':
                    row['edges'][edge] = 'owned'; counts['added'] += 1
                else:
                    row['edges'].pop(edge); counts['removed'] += 1
                persist()
        for gid in fresh['cleanup']:
            current, current_cleanup = check_snapshot()
            if gid not in current_cleanup:
                raise NativeError('drift')
            # Reconfirm both sides before removing the textual representation.
            read = _NativeRead(token)
            for a, b in fresh['desired']:
                if b not in _native_ids(read.collection('/tasks/' + a + '/dependencies')) or a not in _native_ids(read.collection('/tasks/' + b + '/dependents')):
                    raise NativeError('drift')
            html = current_cleanup[gid]
            row = state['scopes'].setdefault(key, {'edges': {}})
            before_hash = expected['tasks'][gid]['description']
            predicted = dict(current[gid], html_notes=html)
            after_hash, bindings = _native_description(predicted, read)
            row.setdefault('cleanup', {})[gid] = {'state': 'pending', 'before': before_hash, 'after': after_hash}
            persist()
            # Minimize the non-CAS window: confirmations and durable intent are
            # finished before this targeted reread of the description to write.
            latest = read.item('/tasks/' + gid + '?opt_fields=gid,name,html_notes,projects,workspace.gid')
            if latest.get('gid') != gid:
                raise NativeError('drift')
            latest_hash, latest_bindings = _native_description(latest, read)
            target = expected['tasks'][gid]
            if (latest_hash != target['description'] or latest_bindings != target['attachments']
                    or latest.get('name') != target['name']
                    or sorted(x.get('gid', '') for x in latest.get('projects', [])) != target['projects']
                    or (latest.get('workspace') or {}).get('gid') != target['workspace']):
                raise NativeError('drift')
            html = _native_cleanup(latest, set(map(tuple, fresh['desired'])))
            if html is None:
                raise NativeError('drift')
            current[gid] = latest
            native_request('PUT', '/tasks/' + gid, token, {'html_notes': html})
            actual = read.item('/tasks/' + gid + '?opt_fields=gid,name,html_notes,projects,workspace.gid')
            if actual.get('gid') != gid:
                raise NativeError('drift')
            actual_hash, actual_bindings = _native_description(actual, read)
            if (actual_hash != after_hash or actual_bindings != bindings
                    or actual.get('name') != current[gid]['name']
                    or actual.get('projects') != current[gid].get('projects')
                    or actual.get('workspace') != current[gid].get('workspace')):
                raise NativeError('drift')
            expected['tasks'][gid]['description'] = actual_hash
            row['cleanup'][gid]['state'] = 'verified'; persist(); counts['cleaned'] += 1
        return {'status': 'applied' if any(counts.values()) else 'unchanged', **counts}
    finally:
        os.close(fd)
        _NATIVE_BUDGET.reset(budget)


def _native_cli(label, cfg, token, args):
    if fcntl is None or not sys.platform.startswith('linux'):
        raise NativeError('unavailable')
    if not args.state or not args.plan:
        raise NativeError('path')
    state_path, plan_path = _native_path(args.state), _native_path(args.plan)
    config_root = Path(args.config).expanduser().resolve().parent
    if any(p.is_relative_to(config_root) for p in (state_path, plan_path)) or state_path == plan_path or plan_path == Path(str(state_path) + '.lock'):
        raise NativeError('path')
    if args.send:
        if not args.expect_plan:
            raise NativeError('drift')
        try:
            if plan_path.stat().st_size > 2 * 1024 * 1024:
                raise NativeError('limit')
            plan = json.loads(plan_path.read_text(), object_pairs_hook=_no_dupe_keys)
        except (OSError, ValueError):
            raise NativeError('drift') from None
        if not isinstance(plan, dict) or plan.get('label') != label or plan.get('cfg') != cfg or plan.get('cleanup_text') != args.cleanup_text:
            raise NativeError('drift')
        result = apply_native_plan(plan, token, state_path, args.expect_plan)
        print(json.dumps(result, ensure_ascii=False))
        return
    plan = build_native_plan(label, cfg, token, _native_read_state(state_path), args.cleanup_text)
    if plan_path.exists():
        try:
            if plan_path.stat().st_size > 2 * 1024 * 1024:
                raise NativeError('path')
            previous = json.loads(plan_path.read_text(), object_pairs_hook=_no_dupe_keys)
            if (not isinstance(previous, dict) or set(previous) != set(plan)
                    or previous.get('digest') != _native_hash({k: v for k, v in previous.items() if k != 'digest'})
                    or previous.get('schema_version') != 1 or previous.get('scope') != plan['scope'] or previous.get('label') != label):
                raise NativeError('path')
        except (ValueError, OSError, AttributeError):
            raise NativeError('path') from None
    _native_atomic(plan_path, plan)
    scope = plan['scope']
    print('account={account}, workspace={workspace}, project={project}'.format(**scope))
    names = {gid: row['name'] for gid, row in plan['snapshots']['tasks'].items()}
    for mode in ('add', 'remove'):
        for a, b in plan[mode]:
            print(f"{mode}: {names[a]} ЖДЕТ {names[b]}")
    for gid in plan['cleanup']:
        print('cleanup text:', names[gid])
    for item in plan['diagnostics']:
        print('diagnostic:', item['task'], item['code'])
    print('plan_sha256:', plan['digest'])
    print('Перед --send согласуйте этот набор; нужен --expect-plan. GET→POST/PUT не CAS: параллельные ручные изменения остаются риском.')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--auth", help="файл с токеном (по умолчанию ASANA_TOKEN или "
                                   f"{DEFAULT_AUTH})")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="файл с цепочками по доскам")
    ap.add_argument("--board", help="какую доску обрабатывать (ключ из конфига); "
                                    "без флага - все, что описаны")
    ap.add_argument("--send", action="store_true", help="записать (без флага - предпросмотр)")
    ap.add_argument("--mode", choices=("auto", "native", "text"))
    ap.add_argument("--state", help="абсолютный journal вне синка")
    ap.add_argument("--plan", help="абсолютный файл плана вне синка")
    ap.add_argument("--expect-plan", help="подтвержденный digest native плана")
    ap.add_argument("--cleanup-text", action="store_true", help="отдельно планировать миграцию текста")
    args = ap.parse_args()

    boards = load_config(args.config)
    if args.board:
        if args.board not in boards:
            sys.exit(f"нет доски '{args.board}' в конфиге. Есть: {', '.join(boards)}")
        boards = {args.board: boards[args.board]}
    if any((args.mode or cfg.get('mode', 'text')) != 'text' for cfg in boards.values()) and (not args.board or len(boards) != 1):
        ap.error('native/auto требуют ровно одну явно выбранную --board')
    token = load_token(args.auth)

    failed: list[str] = []
    for label, cfg in boards.items():
        print(f"\n##### доска: {label}")
        mode = args.mode or cfg.get("mode", "text")
        if mode == "text":
            if args.cleanup_text or args.state or args.plan or args.expect_plan:
                print("native параметры недопустимы для text", file=sys.stderr)
                failed.append(label)
                continue
            _touched, err = run_board(label, cfg, token, args.send)
        else:
            try:
                if not args.board or len(boards) != 1:
                    raise NativeError('scope')
                _native_cli(label, cfg, token, args)
                err = False
            except NativeError as exc:
                print(str(exc), file=sys.stderr)
                if exc.code == 402:
                    print("Native недоступен по тарифу: нужен новый --mode text dry-run и отдельное согласование; текст не записан", file=sys.stderr)
                err = True
        if err:
            failed.append(label)
    if failed:
        # Ненулевой код нужен именно тут: в cron и в цепочке команд пропущенная
        # доска иначе неотличима от чистого прогона.
        print(f"\nдоски с ошибками: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
