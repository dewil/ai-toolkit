#!/usr/bin/env python3
"""Связи задач (блокировки) текстом в описании задач Asana.

Зачем не нативные зависимости: `POST /tasks/{gid}/addDependencies` отдает
402 Payment Required - это премиум-фича. Текстом получается не хуже и даже
нагляднее: блок стоит первым в описании, виден и в карточке, и в предпросмотре.

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
    if auth_path:
        p = Path(auth_path).expanduser()
        if not p.exists():
            sys.exit(f"Нет файла с токеном: {p}")
        try:
            token = str(json.loads(p.read_text(encoding="utf-8")).get("token") or "").strip()
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"Не читается {p}: {e}")
        if not token:
            sys.exit(f"В {p} нет поля token")
        return token

    token = os.environ.get("ASANA_TOKEN", "").strip()
    if token:
        return token

    if DEFAULT_AUTH.exists():
        try:
            token = str(json.loads(DEFAULT_AUTH.read_text(encoding="utf-8")).get("token") or "").strip()
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"Не читается {DEFAULT_AUTH}: {e}")
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--auth", help="файл с токеном (по умолчанию ASANA_TOKEN или "
                                   f"{DEFAULT_AUTH})")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="файл с цепочками по доскам")
    ap.add_argument("--board", help="какую доску обрабатывать (ключ из конфига); "
                                    "без флага - все, что описаны")
    ap.add_argument("--send", action="store_true", help="записать (без флага - предпросмотр)")
    args = ap.parse_args()

    boards = load_config(args.config)
    if args.board:
        if args.board not in boards:
            sys.exit(f"нет доски '{args.board}' в конфиге. Есть: {', '.join(boards)}")
        boards = {args.board: boards[args.board]}
    token = load_token(args.auth)

    failed: list[str] = []
    for label, cfg in boards.items():
        print(f"\n##### доска: {label}")
        _touched, err = run_board(label, cfg, token, args.send)
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
