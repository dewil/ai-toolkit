#!/usr/bin/env python3
"""
session-cost.py - подсчет токенов Claude Code сессии из транскрипта.

Claude Code пишет транскрипт каждой сессии в
`~/.claude/projects/<encoded-project>/<session-id>.jsonl`. У каждого
assistant-сообщения есть `message.usage` с полями input_tokens,
output_tokens, cache_creation_input_tokens (запись кэша),
cache_read_input_tokens (чтение кэша). Скрипт суммирует их за сессию.

Зачем: заполнять токен-строку в смете кейса (часы + токены, без денег) без
ручного `/cost`. `/usage` дает только % лимита, а не сырые токены - тут сырые.

Кодировка имени проекта: Claude Code берет абсолютный путь рабочей папки и
заменяет каждый не-alnum символ на '-' (`/Users/x/My.Proj` ->
`-Users-x-My-Proj`; кириллица и пробелы - тоже по дефису на символ).

Текущая сессия определяется по env CLAUDE_CODE_SESSION_ID (его ставит Claude
Code). Если переменной нет - падаем на свежайшую по mtime с предупреждением
(при параллельных сессиях это ненадежно - тогда --session явно).

Использование:
    session-cost.py                      # текущая сессия (CLAUDE_CODE_SESSION_ID) в проекте по CWD
    session-cost.py --session <id>       # конкретная сессия (по имени файла без .jsonl)
    session-cost.py --file <path.jsonl>  # конкретный файл транскрипта
    session-cost.py --project <dir>      # другой проект (путь к рабочей папке)
    session-cost.py --all-sessions       # суммировать все сессии проекта
    session-cost.py --json               # машиночитаемый вывод

Оговорка по интерпретации: cache_read обычно доминирует - это перечитывание
постоянного контекста (CLAUDE.md, правила, память, схемы тулов) на каждом
ходу, а не "работа по задаче". Показатель реального труда - output (и отчасти
cache_write). Скрипт это помечает в выводе.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

USAGE_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}


def encode_project(path: Path) -> str:
    """Абсолютный путь рабочей папки -> имя папки в ~/.claude/projects.

    Правило Claude Code: каждый символ вне [A-Za-z0-9] заменяется на '-'
    (без схлопывания). Кириллица и пробелы тоже -> по '-' на символ.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path.resolve()))


def project_dir(project_path: Path) -> Path:
    return PROJECTS_DIR / encode_project(project_path)


def newest_jsonl(directory: Path) -> Path | None:
    files = sorted(directory.glob("*.jsonl"), key=os.path.getmtime)
    return files[-1] if files else None


def sum_usage(paths: list[Path]) -> dict:
    totals = {k: 0 for k in USAGE_FIELDS}
    messages = 0
    for path in paths:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = (obj.get("message") or {}).get("usage") or {}
                if not usage:
                    continue
                messages += 1
                for key, field in USAGE_FIELDS.items():
                    totals[key] += usage.get(field, 0) or 0
    return {"totals": totals, "messages": messages}


def resolve_paths(args) -> list[Path]:
    if args.file:
        p = Path(args.file)
        if not p.exists():
            sys.exit(f"Файл не найден: {p}")
        return [p]

    pdir = project_dir(Path(args.project) if args.project else Path.cwd())
    if not pdir.exists():
        sys.exit(
            f"Нет папки транскриптов проекта: {pdir}\n"
            f"Проверь путь (--project) или укажи файл напрямую (--file)."
        )

    if args.session:
        p = pdir / f"{args.session}.jsonl"
        if not p.exists():
            sys.exit(f"Нет сессии {args.session} в {pdir}")
        return [p]

    if args.all_sessions:
        files = sorted(pdir.glob("*.jsonl"), key=os.path.getmtime)
        if not files:
            sys.exit(f"В {pdir} нет транскриптов.")
        return files

    # Надежный якорь текущей сессии - env CLAUDE_CODE_SESSION_ID (его ставит
    # Claude Code). "Свежайший по mtime" ненадежен: при параллельных сессиях в
    # одном проекте схватит чужую, которую записали последней.
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if env_sid:
        p = pdir / f"{env_sid}.jsonl"
        if p.exists():
            return [p]
        sys.stderr.write(
            f"CLAUDE_CODE_SESSION_ID={env_sid}, но {p.name} в проекте нет - "
            f"падаю на свежайшую по mtime.\n"
        )

    latest = newest_jsonl(pdir)
    if latest is None:
        sys.exit(f"В {pdir} нет транскриптов.")
    sys.stderr.write(
        "ВНИМАНИЕ: беру свежайшую сессию по mtime (нет CLAUDE_CODE_SESSION_ID). "
        "Если параллельно открыты другие сессии Claude Code в этом проекте - это "
        "может быть НЕ текущая, тогда укажи --session явно.\n"
    )
    return [latest]


def main() -> int:
    ap = argparse.ArgumentParser(description="Подсчет токенов Claude Code сессии из транскрипта.")
    ap.add_argument("--file", help="путь к конкретному .jsonl транскрипта")
    ap.add_argument("--session", help="id сессии (имя файла без .jsonl) в текущем/указанном проекте")
    ap.add_argument("--project", help="путь к рабочей папке проекта (по умолчанию - CWD)")
    ap.add_argument("--all-sessions", action="store_true", help="суммировать все сессии проекта")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    args = ap.parse_args()

    paths = resolve_paths(args)
    result = sum_usage(paths)
    t = result["totals"]
    work = t["output"] + t["cache_write"]
    grand = sum(t.values())

    if args.json:
        print(json.dumps({
            "files": [str(p) for p in paths],
            "messages": result["messages"],
            "tokens": t,
            "work_tokens": work,
            "grand_total": grand,
        }, ensure_ascii=False, indent=2))
        return 0

    label = paths[0].name if len(paths) == 1 else f"{len(paths)} сессий проекта"
    print(f"Транскрипт: {label}")
    print(f"Assistant-сообщений с usage: {result['messages']}")
    print("Токены:")
    print(f"  output (генерация)      : {t['output']:>14,}")
    print(f"  cache_write (новый ctx) : {t['cache_write']:>14,}")
    print(f"  cache_read (перечитыв.) : {t['cache_read']:>14,}   <- в основном постоянный контекст, не работа по задаче")
    print(f"  input (свежий)          : {t['input']:>14,}")
    print(f"  --")
    print(f"  work (output+cache_write): {work:>13,}   <- показатель реального труда для сметы")
    print(f"  всего с кэшем            : {grand:>13,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
