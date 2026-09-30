#!/usr/bin/env python3
"""
Код возврата 3 - сводка неполная: invalid_lines > 0.
session-cost.py - подсчет токенов Claude Code сессии из транскрипта.

Claude Code пишет транскрипт каждой сессии в
`~/.claude/projects/<encoded-project>/<session-id>.jsonl`. У каждого
assistant-сообщения есть `message.usage` с полями input_tokens,
output_tokens, cache_creation_input_tokens (запись кэша),
cache_read_input_tokens (чтение кэша). Скрипт учитывает последний непустой usage по message.id в каждом
транскрипте и включает субагентов сессии.

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


def summary(paths: list[Path], messages: int, tokens: dict,
            invalid_lines: int = 0, messages_without_id: int = 0) -> dict:
    return {
        "files": [str(p) for p in paths],
        "messages": messages,
        "tokens": tokens,
        "work_tokens": tokens["output"] + tokens["cache_write"],
        "grand_total": sum(tokens.values()),
        "invalid_lines": invalid_lines,
        "messages_without_id": messages_without_id,
    }


def unique_paths(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(p.resolve() for p in paths))


def sum_usage(paths: list[Path]) -> dict:
    paths = unique_paths(paths)
    totals = {k: 0 for k in USAGE_FIELDS}
    messages = invalid_lines = messages_without_id = 0
    for path in paths:
        by_id = {}
        without_id = []
        invalid = 0
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict) or not usage:
                    continue
                message_id = message.get("id")
                if not message_id:
                    without_id.append(usage)
                else:
                    by_id[message_id] = usage
        if invalid:
            sys.stderr.write(
                f"{path}: invalid_lines={invalid} - битые JSON-строки пропущены, "
                "сводка неполная.\n"
            )
        if without_id:
            sys.stderr.write(
                f"{path}: messages_without_id={len(without_id)} - сообщения без ID "
                "учтены отдельно, дедупликация для них невозможна.\n"
            )
        invalid_lines += invalid
        messages_without_id += len(without_id)
        messages += len(by_id) + len(without_id)
        for usage in [*by_id.values(), *without_id]:
            for key, field in USAGE_FIELDS.items():
                totals[key] += usage.get(field, 0) or 0
    return summary(paths, messages, totals, invalid_lines, messages_without_id)


def subagent_paths(paths: list[Path]) -> list[Path]:
    found = []
    for path in paths:
        directory = path.with_suffix("") / "subagents"
        # stat/iterdir сохраняют ошибки доступа; glob может их скрыть.
        try:
            directory.stat()
        except FileNotFoundError:
            continue
        found.extend(sorted(p for p in directory.iterdir() if p.suffix == ".jsonl"))
    return unique_paths(found)


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

    try:
        paths = unique_paths(resolve_paths(args))
        children = subagent_paths(paths)
        main_paths = [p for p in paths if p not in children]
        main_result = sum_usage(main_paths)
        subagents = sum_usage(children)
    except (OSError, UnicodeError) as exc:
        sys.stderr.write(f"Ошибка чтения транскриптов: {exc}\n")
        return 1

    tokens = {k: main_result["tokens"][k] + subagents["tokens"][k]
              for k in USAGE_FIELDS}
    result = summary(
        main_paths + children,
        main_result["messages"] + subagents["messages"], tokens,
        main_result["invalid_lines"] + subagents["invalid_lines"],
        main_result["messages_without_id"] + subagents["messages_without_id"],
    )
    result.update(main=main_result, subagents=subagents)
    exit_code = 3 if result["invalid_lines"] else 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code

    for label, group in (("Основной контекст (main)", main_result),
                         ("Субагенты (subagents)", subagents),
                         ("Общий итог", result)):
        t = group["tokens"]
        print(f"{label}: messages={group['messages']}, "
              f"input={t['input']}, output={t['output']}, "
              f"cache_read={t['cache_read']}, cache_write={t['cache_write']}, "
              f"work={group['work_tokens']}, grand_total={group['grand_total']}")
        print(f"  invalid_lines={group['invalid_lines']}, "
              f"messages_without_id={group['messages_without_id']}")
    if result["invalid_lines"]:
        print("Сводка неполная: битые JSON-строки пропущены.")
    if result["messages_without_id"]:
        print("Дедупликация ограничена: сообщения без ID учтены отдельно.")
    print("work = output + cache_write; cache_read - перечитывание контекста.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
