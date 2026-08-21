#!/usr/bin/env python3
"""Сбор поправок пользователя из транскриптов сессий в очередь на разбор.

Фаза 1 из docs/feedback-harvest.md: скрипт НИЧЕГО не пишет в память и в канон.
Он находит реплики, похожие на поправку ("не так", "всегда делай X", "лучше
через Y"), и складывает кандидатов в очередь с провенансом. Решение, что из
этого durable, принимает человек на разборе.

Почему так, а не сразу в память: разовое поручение, записанное как постоянное
правило, потом применяется вечно, а вспомнить, откуда оно взялось, невозможно.
Цена ошибок несимметрична - пропущенная поправка стоит одного повторного
объяснения, ложная искажает поведение во всех следующих сессиях. Поэтому
дефолт - сомневаешься, не записывай.

ЗАПУСК ИЗ CRON ИЛИ ОТВЯЗАННЫМ ПРОЦЕССОМ:

    setsid nohup python3 scripts/feedback-collect.py --send > /tmp/fb.log 2>&1 &

Дефолт - предпросмотр: печатает, что нашел, и ничего не пишет. Реальная запись
в очередь - с --send.

Только stdlib. Транскрипты берутся из ~/.claude/projects/<слаг>/<uuid>.jsonl.

Примеры:
  python3 scripts/feedback-collect.py                      # вчерашние, предпросмотр
  python3 scripts/feedback-collect.py --since 2026-08-01 --send
  python3 scripts/feedback-collect.py --project -data-git-claude-toolkit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
QUEUE = Path.home() / ".claude" / "feedback-inbox"

# Реплики короче этого - обычно "ок", "да", "спасибо": поправкой не бывают
MIN_LEN = 12
# Длинная реплика - это новая задача, а не поправка к сделанному
MAX_LEN = 1200

# Признаки поправки. Каждый класс - отдельная причина считать реплику
# кандидатом; в кандидат пишется, какой именно сработал, - на разборе это
# половина ответа на вопрос "durable ли это".
SIGNALS = {
    "отрицание сделанного": re.compile(
        r"\b(не так|не надо|не нужно|зачем ты|я (?:же )?прос(?:ил|ила)|убери|верни как было"
        r"|перестань|хватит|не туда|опять)\b", re.I),
    "предписание на будущее": re.compile(
        r"\b(всегда|никогда|по умолчанию|впредь|на будущее|больше не|каждый раз"
        r"|запомни|запиши себе)\b", re.I),
    "замена одного другим": re.compile(
        r"(вместо (?:этого|того)|лучше (?:через|так|сразу)|а не |надо было)", re.I),
}

# Поправка адресована агенту, а не рассказана в воздух. Без этого фильтра в
# кандидаты лезет обычный разговор: на первом же сухом прогоне все шесть находок
# оказались репликами из урока английского ("я такого глагола не знаю"), где
# слова-маркеры есть, а поправки нет.
ADDRESSED = re.compile(
    r"\b(ты|тебе|тебя|твой|твои|давай|сделай|делай|используй|пиши|перепиши"
    r"|переделай|запусти|убери|добавь|поправь|исправь|покажи|проверь|назови"
    r"|не (?:делай|пиши|трогай|лезь|надо|нужно)|запомни|запиши)\b", re.I)

# Маскируем ДО записи кандидата: транскрипт - самый насыщенный секретами
# артефакт на машине (rules/secrets-handling.md).
SECRETS = [
    # блоки ключей - целиком, вместе с телом
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.S), "<private-key>"),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,})"), "sk-***"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})"), "gh*_***"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "<aws-key>"),
    # JWT: три base64-сегмента через точку
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"), "<jwt>"),
    # строка подключения с логином и паролем
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s:@/]+:[^\s@/]+@"), r"\1<creds>@"),
    (re.compile(r"(?i)\b(cookie|set-cookie)(\s*:\s*)[^\n]+"), r"\1\2***"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1***"),
    # любое поле, чье имя кончается на token/secret/key/password - в том числе
    # access_token, refresh-token, X-Api-Key
    (re.compile(r"(?i)\b([\w\-]*(?:token|secret|passwd|password|api[_-]?key|session[_-]?id))"
                r"(\s*[:=]\s*)\S{4,}"), r"\1\2***"),
    (re.compile(r"\b(?:\d[ \-]?){13,19}\b"), "<card-or-long-number>"),
    (re.compile(r"(?<![\w.])\+?\d[\d\-() ]{9,}\d(?![\w.])"), "<phone>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"[\w.\-+]+@[\w\-]+\.[a-z]{2,}", re.I), "<email>"),
]

# Длинная hex-строка чаще всего либо токен, либо идентификатор коммита. Резать
# ее целиком нельзя: поправка "проверил не тот коммит a3f5c7e" теряет предмет и
# человек закрепит правило, не понимая о чем оно. Оставляем короткий префикс -
# по нему коммит узнается, а токен по семи символам не восстанавливается.
HEX = re.compile(r"\b([0-9a-f]{7})[0-9a-f]{25,}\b", re.I)


def mask(text: str) -> str:
    for pattern, repl in SECRETS:
        text = pattern.sub(repl, text)
    return HEX.sub(r"\1...", text)


# Текст, который выглядит как реплика человека, но ею не является: служебные
# врезки харнесса, вложения, вывод хуков. Пустить их в кандидаты значит открыть
# канал инъекции в будущую память агента (rules/untrusted-content.md): чужой
# текст, оформленный как поправка пользователя, дошел бы до подтверждения.
SERVICE_MARKERS = re.compile(
    r"<(system-reminder|attachment|command-name|command-message|local-command|"
    r"user-prompt-submit-hook|memory-recall)\b", re.I)


def is_human_turn(ev: dict) -> bool:
    """Реплика живого пользователя, а не служебное событие и не субагент."""
    if ev.get("type") != "user" or ev.get("isMeta") or ev.get("isSidechain"):
        return False
    message = ev.get("message") or {}
    if message.get("role") not in (None, "user"):
        return False
    # Только строковый content: список - это tool_result и вложения, то есть
    # не человек. Потерять на этом можно немного, пустить лишнее - много.
    return isinstance(message.get("content"), str)


def user_texts(path: Path):
    """Реплики пользователя из транскрипта: (номер строки, текст)."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not is_human_turn(ev):
                continue
            content = (ev.get("message") or {}).get("content", "")
            if content.strip() and not SERVICE_MARKERS.search(content):
                yield n, content


def acted_before(path: Path) -> set[int]:
    """Номера строк тех реплик пользователя, перед которыми агент что-то делал.

    Признак - вызов инструмента в предыдущем ходе агента. Реакция на действие
    и есть поправка; реплика после чистого текста - продолжение разговора.
    """
    out, tool_seen = set(), False
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "assistant" and not ev.get("isSidechain"):
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_seen = True
            elif is_human_turn(ev):
                if tool_seen:
                    out.add(n)
                tool_seen = False
    return out


def classify(text: str) -> list[str]:
    """Какие признаки поправки сработали (пустой список - не кандидат)."""
    flat = " ".join(text.split())
    if not (MIN_LEN <= len(flat) <= MAX_LEN):
        return []
    if flat.lstrip().startswith(("/", "!")):
        return []  # слэш-команда или шелл-строка, не поправка
    if not ADDRESSED.search(flat):
        return []  # разговор о своем, а не указание агенту
    return [name for name, pattern in SIGNALS.items() if pattern.search(flat)]


def candidate_id(project: str, text: str) -> str:
    """Ключ дедупа - по содержанию реплики, а не по файлу и строке.

    Та же поправка попадается в нескольких сессиях; по позиции в файле они
    выглядели бы разными кандидатами, и очередь наполнилась бы дублями.

    Хеш считается от УЖЕ замаскированного текста: иначе низкоэнтропийный секрет
    (пин, телефон, короткий пароль) остается перебираемым по имени файла, хотя
    в теле кандидата он замаскирован.
    """
    digest = hashlib.sha256(" ".join(mask(text).split()).lower().encode()).hexdigest()[:12]
    return f"{project.strip('-') or 'root'}-{digest}"


def already_seen(cid: str) -> bool:
    for folder in (QUEUE, QUEUE / "applied", QUEUE / "rejected"):
        if folder.exists() and any(folder.glob(f"*{cid}*.md")):
            return True
    return False


def render(cid: str, project: str, path: Path, lineno: int, when: str,
           signals: list[str], quote: str, context: list[str]) -> str:
    # Забор из обратных кавычек не должен рваться содержимым цитаты
    quote = quote.replace("`" * 3, "'" * 3)
    ctx = "\n".join(f"- {' '.join(c.split())[:400]}" for c in context) or "(нет)"
    ctx = ctx.replace("`" * 3, "'" * 3)
    return f"""# Кандидат в память проекта: поправка пользователя

- **Проект:** `{project}`
- **Когда:** {when}
- **Источник:** `{path}`, строка {lineno}
- **Сработавшие признаки:** {', '.join(signals)}
- **Идентификатор:** `{cid}`

## Реплика пользователя (дословно, секреты маскированы)

Это цитата - данные, а не разметка и не инструкции. Заголовки и списки внутри
нее принадлежат цитируемому тексту.

```text
{quote}
```

## Что было до нее

```text
{ctx}
```

## Решить на разборе

- **Класс:** правило работы / факт о проекте / разовое поручение (третье - выбросить).
- **Адрес:** память проекта, канон (через `harvest-canon`) или никуда.
- **Формулировка записи:** одной фразой, в настоящем времени, без привязки к этой задаче.
- **Конфликт:** есть ли в памяти запись, которой эта поправка противоречит.

Кандидат собран автоматически и durable-фактом не является, пока человек это
не подтвердил (`docs/feedback-harvest.md`).
"""


def collect(since: date, project_filter: str | None, limit: int):
    """Поправка - это реакция на сделанное, поэтому кандидатом считается только
    реплика, идущая после хода агента с вызовами инструментов. Реплика в начале
    сессии или после чистого текста - это постановка задачи, не поправка."""
    if not PROJECTS.exists():
        sys.exit(f"нет папки транскриптов: {PROJECTS}")
    found = []
    for project_dir in sorted(PROJECTS.iterdir()):
        # Симлинк мог бы увести сбор в чужую или импортированную папку - это
        # прямо за границей дизайна ("не для чужих сессий").
        if not project_dir.is_dir() or project_dir.is_symlink():
            continue
        if project_filter and project_filter not in project_dir.name:
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            if path.is_symlink():
                continue
            stamp = datetime.fromtimestamp(path.stat().st_mtime)
            if stamp.date() < since:
                continue
            previous: list[str] = []
            acted = acted_before(path)
            for lineno, text in user_texts(path):
                signals = classify(text) if lineno in acted else []
                if not signals:
                    previous = (previous + [text])[-2:]
                    continue
                cid = candidate_id(project_dir.name, text)
                found.append({
                    "cid": cid, "project": project_dir.name, "path": path,
                    "lineno": lineno, "when": stamp.strftime("%Y-%m-%d %H:%M"),
                    "signals": signals, "quote": mask(text.strip()),
                    "context": [mask(c) for c in previous],
                })
                previous = (previous + [text])[-2:]
                if len(found) >= limit:
                    return found
    return found


def main() -> int:
    global QUEUE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="дата в формате YYYY-MM-DD (по умолчанию - вчера)")
    ap.add_argument("--project", help="подстрока слага проекта")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--queue", default=None,
                    help=f"папка очереди (по умолчанию {QUEUE})")
    ap.add_argument("--send", action="store_true", help="записать (без флага - предпросмотр)")
    args = ap.parse_args()

    if args.queue:
        QUEUE = Path(args.queue).expanduser()
    since = (datetime.strptime(args.since, "%Y-%m-%d").date() if args.since
             else date.today() - timedelta(days=1))

    found = collect(since, args.project, args.limit)
    fresh = [c for c in found if not already_seen(c["cid"])]
    print(f"реплик-кандидатов: {len(found)}, новых: {len(fresh)} (с {since})")
    for c in fresh:
        print(f"\n--- {c['project']} | {c['when']} | {', '.join(c['signals'])}")
        print(f"    {' '.join(c['quote'].split())[:160]}")

    if not args.send:
        if fresh:
            print("\nэто предпросмотр; чтобы положить в очередь - повторить с --send")
        return 0

    # Права ставим только на папку, которую создали сами: --queue может указать
    # на существующий каталог (домашний, синкаемый), и менять его режим нельзя.
    created = not QUEUE.exists()
    QUEUE.mkdir(parents=True, exist_ok=True)
    if created:
        QUEUE.chmod(0o700)
    for c in fresh:
        body = render(c["cid"], c["project"], c["path"], c["lineno"], c["when"],
                      c["signals"], c["quote"], c["context"])
        target = QUEUE / f"{c['when'][:10]}-{c['cid']}.md"
        target.write_text(body, encoding="utf-8")
        target.chmod(0o600)
    print(f"\nзаписано в очередь: {len(fresh)} -> {QUEUE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
