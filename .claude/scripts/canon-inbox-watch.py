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

Плюс вторая проверка - **застрявшие брифы**: те, что лежат в проектах в
toolkit-log/upstream-pending/, но до очереди не доехали. Пустая очередь сама по
себе не значит "нечего разбирать": за три недели так потерялось три брифа из
девяти, и заметил это человек, а не автоматика. Канон закрывает это скриптом
scripts/canon-brief.py (одна команда пишет обе копии) и сверкой в синке; здесь
- наблюдение, которое не зависит ни от того, ни от другого.

Корни проектов перечислены константой PROJECT_ROOTS: это инструмент конкретной
машины, а не канон, поэтому машинные пути тут уместны. Реестр ~/.claude/projects
для этого не годится - имена там кодируют путь с потерями (слеши, пробелы и
не-ASCII становятся одним дефисом), и обратное восстановление было бы угадыванием.

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
PROJECT_ROOTS = [Path.home() / n for n in ("Work", "HR", "Home", "Courses")]
PENDING = Path("toolkit-log") / "upstream-pending"
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


def block(title: str, names: list[str]) -> str:
    listing = "\n".join(f"- {safe(n)}" for n in names[:10])
    tail = f"\n... и еще {len(names) - 10}" if len(names) > 10 else ""
    return f"{title}\n{listing}{tail}"


def notify(names: list[str], dry_run: bool, lost: list[str] | None = None) -> int:
    lost = lost or []
    parts = []
    if names:
        plural = "бриф" if len(names) == 1 else "брифов"
        parts.append(block(f"Канон-инбокс: {len(names)} новых {plural}.", names))
    if lost:
        # Застрявший бриф до очереди не доехал и разбором не найдется: это не
        # напоминание, а сообщение о потере
        parts.append(block(
            f"РАСХОЖДЕНИЕ ПРОЕКТ/ОЧЕРЕДЬ: {len(lost)}.", lost))
    text = "\n\n".join(parts) + (
        "\n\nРазобрать: открыть сессию в клоне канона и сказать \"разбери inbox\"."
    )
    if lost:
        text += ("\nРазобрать: в проекте python3 scripts/canon-brief.py check "
                 "(доставка - --redeliver, локальная копия - --restore-local, "
                 "разошедшиеся версии сверить руками)")
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


BRIEFER = Path("/data/git/claude-toolkit/scripts/canon-brief.py")


def safe(text: str) -> str:
    """Имя для построчного вывода: управляющие символы обезврежены.

    Строки уходят в уведомление списком, и перевод строки внутри имени
    подделал бы лишний пункт.
    """
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def project_roots() -> list[Path]:
    """Корни проектов, которые вообще что-то выносили в канон."""
    found = []
    for root in PROJECT_ROOTS:
        if not root.is_dir():
            continue
        # глубина ограничена: проекты лежат на 1-3 уровня ниже корня, а полный
        # обход vault на десятки тысяч файлов делать незачем. Проект глубже -
        # не найдется, и это осознанный предел, а не проверенная пустота
        for depth in ("*", "*/*", "*/*/*"):
            # маркер - toolkit-log, а не upstream-pending: проект, у которого
            # запись в pending не удалась (код 3), иначе не проверялся бы вовсе
            found += [tl.parent for tl in root.glob(f"{depth}/toolkit-log")
                      if tl.is_dir()]
    return sorted(set(found))


def stranded() -> tuple[list[str], list[str]]:
    """(застрявшие брифы, проекты с невыполненной проверкой).

    Сверку делает сам canon-brief.py, а не копия его логики здесь: правила
    совпадения (слаг проекта, хвост имени, терминальные папки) живут в одном
    месте, иначе две реализации разойдутся и наблюдение начнет врать в обе
    стороны. Проект, который проверить не удалось, возвращается отдельно -
    "не проверено" это не "чисто" (rules/silent-failure.md).
    """
    if not BRIEFER.is_file():
        return [], [f"нет {BRIEFER}"]
    lost, broken = [], []
    for root in project_roots():
        res = subprocess.run(
            [sys.executable, str(BRIEFER), "check", "--project-root", str(root)],
            capture_output=True, text=True, errors="replace")
        if res.returncode == 0:
            continue
        if res.returncode != 4:
            broken.append(f"{root.name}: rc={res.returncode} "
                          f"{(res.stderr or res.stdout).strip()[:120]}")
            continue
        found_any = False
        section = None
        for line in res.stdout.splitlines():
            head = line.strip()
            if head.startswith("НЕ доставлено") or "НЕ доставлено:" in head:
                section = "lost"
            elif head.startswith("в очереди есть"):
                section = "local"
            elif head.startswith("копии разошлись"):
                section = "diverged"
            elif head.startswith("- ") and section:
                found_any = True
                label = {"lost": "не доставлен", "local": "нет копии в проекте",
                         "diverged": "версии разошлись"}[section]
                lost.append(f"{safe(root.name)}/{safe(head[2:])} ({label})")
        if not found_any:
            # код 4 означает расхождение; ни одного имени в выводе - значит
            # разобрать его не удалось, а не что все чисто
            broken.append(f"{safe(root.name)}: rc=4, но перечня имен в выводе нет")
    return sorted(lost), broken


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить очередь канон-инбокса.")
    parser.add_argument("--dry-run", action="store_true", help="показать, но не отправлять")
    args = parser.parse_args()

    # Проверка проектов НЕ зависит от состояния очереди: отсутствующая очередь
    # и есть самое вероятное состояние первой потери - бриф написали руками,
    # доставить забыли, класть в очередь было некому
    queue_rc = 0
    current: set[str] = set()
    fresh: list[str] = []
    if not INBOX.exists():
        # штатное состояние: очередь создается тем, кто кладет первый бриф
        log("ПАПКИ НЕТ", str(INBOX))
    elif not os.access(INBOX, os.R_OK | os.X_OK):
        log("НЕ ЧИТАЕТСЯ", f"{INBOX} - это не пустая очередь, разберись с правами")
        queue_rc = 2
    else:
        current = {p.name for p in INBOX.glob("*.md") if p.is_file()}
        fresh = sorted(current - read_seen())
        log("ПУСТО" if not current
            else "В ОЧЕРЕДИ", "" if not current
            else f"{len(current)}, из них новых {len(fresh)}")

    lost, broken = stranded()
    if lost:
        log("РАСХОЖДЕНИЕ ПРОЕКТ/ОЧЕРЕДЬ", f"{len(lost)}: " + ", ".join(lost))
    if broken:
        # Непроверенный проект молчит так же, как чистый - назовем его вслух
        log("ПРОВЕРКА НЕ ВЫПОЛНЕНА", "; ".join(broken))

    rc = queue_rc
    if fresh or lost:
        rc = notify(fresh, args.dry_run, lost) or rc
    if broken and rc == 0:
        rc = 2
    # Запоминаем только успешно доложенное: сорвавшаяся отправка не должна
    # прятать бриф от следующего прогона
    if not args.dry_run and rc == 0:
        write_seen(current)
    return rc


if __name__ == "__main__":
    sys.exit(main())
