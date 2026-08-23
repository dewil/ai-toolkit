#!/usr/bin/env python3
"""Сторож очереди канон-инбокса: раз в несколько часов смотрит
~/.claude/canon-inbox/ и пишет владельцу, если приехали НОВЫЕ брифы.

Инструмент для работы внутри самого toolkit-репо, поэтому живет в .claude/ и
каноном не является: раскатывать его в чужие проекты бессмысленно - там нет
ни клона канона, ни его очереди.

Уведомляет только о брифах, которых не видел раньше (состояние в
~/.cache/canon-inbox-watch/seen.json). Иначе каждые три часа приходило бы одно
и то же, и напоминание перестали бы читать - а вместе с ним и настоящее новое.

Три состояния очереди различаются намеренно (rules/silent-failure.md): папки
нет - это не то же самое, что папка пуста, а нечитаемая папка не то же самое,
что обе первые. В лог пишется всегда, наружу - только при новых брифах.

    python3 .claude/scripts/canon-inbox-watch.py            # проверить и уведомить
    python3 .claude/scripts/canon-inbox-watch.py --dry-run  # без отправки
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

INBOX = Path(os.environ.get("CANON_INBOX", Path.home() / ".claude" / "canon-inbox"))
STATE = Path.home() / ".cache" / "canon-inbox-watch" / "seen.json"
SENDER = Path("/data/git/claude-toolkit/scripts/telegram-send-one.py")
LOG_PREFIX = "canon-inbox-watch"
QUIET_FROM, QUIET_TO = 21, 9  # вне этих часов уведомление уходит беззвучно


def log(state: str, detail: str = "") -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"{stamp} {LOG_PREFIX}: {state}{(' - ' + detail) if detail else ''}")


def read_seen() -> set[str]:
    try:
        return set(json.loads(STATE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def write_seen(names: set[str]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(names), ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def notify(names: list[str], dry_run: bool) -> int:
    plural = "бриф" if len(names) == 1 else "брифов"
    listing = "\n".join(f"- {n}" for n in names[:10])
    tail = f"\n... и еще {len(names) - 10}" if len(names) > 10 else ""
    text = (
        f"Канон-инбокс: {len(names)} новых {plural}.\n{listing}{tail}\n\n"
        "Разобрать: открыть сессию в клоне канона и сказать \"разбери inbox\"."
    )
    if dry_run:
        print(text)
        return 0
    if not SENDER.exists():
        log("ОШИБКА", f"нет отправщика {SENDER}")
        return 1
    cmd = [sys.executable, str(SENDER), "me", "--text", text, "--send", "--no-pace-check"]
    hour = datetime.now().hour
    if hour >= QUIET_FROM or hour < QUIET_TO:
        cmd.append("--silent")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        # молчать нельзя: отчет "проверил" при неудачной отправке выглядит так
        # же, как успешный, и очередь копится незамеченной
        log("ОШИБКА ОТПРАВКИ", (res.stderr or res.stdout).strip()[:200])
        return res.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить очередь канон-инбокса.")
    parser.add_argument("--dry-run", action="store_true", help="показать, но не отправлять")
    args = parser.parse_args()

    if not INBOX.exists():
        # штатное состояние: очередь создается тем, кто кладет первый бриф
        log("ПАПКИ НЕТ", str(INBOX))
        return 0
    if not os.access(INBOX, os.R_OK | os.X_OK):
        log("НЕ ЧИТАЕТСЯ", f"{INBOX} - это не пустая очередь, разберись с правами")
        return 2

    current = {p.name for p in INBOX.glob("*.md") if p.is_file()}
    seen = read_seen()
    fresh = sorted(current - seen)

    if not current:
        log("ПУСТО")
    else:
        log("В ОЧЕРЕДИ", f"{len(current)}, из них новых {len(fresh)}")

    rc = 0
    if fresh:
        rc = notify(fresh, args.dry_run)
    # Запоминаем только успешно доложенное: сорвавшаяся отправка не должна
    # прятать бриф от следующего прогона
    if not args.dry_run and rc == 0:
        write_seen(current)
    return rc


if __name__ == "__main__":
    sys.exit(main())
