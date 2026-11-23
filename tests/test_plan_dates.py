#!/usr/bin/env python3
"""Тесты plan-dates.py - механика плана работ (recalc, prune, lint, all).
stdlib-only (unittest). Скрипт запускается подпроцессом на синтетических плане
и данных во временном каталоге, с фиксированным --now.

Запуск: python3 tests/test_plan_dates.py

Пишутся по спеке docs/plan-dates.md, без чтения реализации. Эталоны
(медиана, второе по величине, округление вверх, граница 24 ч) посчитаны
вручную по спеке и записаны литералами.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "plan-dates.py"
MSK = timezone(timedelta(hours=3))
NOW = "2026-11-23T12:00:00+03:00"
NOW_DT = datetime(2026, 11, 23, 12, 0, tzinfo=MSK)

BLK_HEAD = "| Ждем | Что | Держит | С | Снимется | Где | Примечание |"
WORK_HEAD = "| Кто | Что | Для | Начато | Готово | Где | Примечание |"
RISK_HEAD = "| Чей | Уровень | Риск | Чем грозит | Признак | Мера | С |"
HOLD_HEAD = "| Держатель | Обычно, дн | Затяжно, дн | Замеров, шт | Открыто, шт |"
HOLD_SEP = "| --- | --- | --- | --- | --- |"

BLK_ROW = "| dwl | Решение | этап 1 | 22.11 | 24.11 | [ссылка](x) | - |"
WORK_ROW = "| Claude2 | Работа | этап 1 | 22.11 01:20 | 24.11 | ветка | - |"
RISK_ROWS = [
    "| Claude2 | высокий | Риск один | грозит | признак | мера | 20.11 |",
    "| Claude2 | средний | Риск два | грозит | признак | мера | 20.11 |",
]
HOLD_ROW = "| dwl | 1 | 3 | 0 (по умолчанию: мало данных) | 0 |"


def tbl(head: str, rows: list[str]) -> list[str]:
    ncols = head.count("|") - 1
    return [head, "|" + " --- |" * ncols] + rows


def plan_text(
    header=None,
    blk_n=1,
    blk_rows=None,
    work_n=1,
    work_rows=None,
    risk_n=None,
    risk_rows=None,
    done_items=None,
    holders_lines=None,
    dict_lines=None,
    backlog=None,
    updated_line="Обновлено: 23.11 11:00 MSK. Ведет Тест.",
) -> str:
    """Чистый план: на нем lint дает код 0 без ERROR/WARN при NOW."""
    if header is None:
        header = ["", updated_line, "", "Цель: проверка."]
    blk_rows = [BLK_ROW] if blk_rows is None else blk_rows
    work_rows = [WORK_ROW] if work_rows is None else work_rows
    risk_rows = list(RISK_ROWS) if risk_rows is None else risk_rows
    risk_n = len(risk_rows) if risk_n is None else risk_n
    done_items = ["- 23.11 10:00 - сделано"] if done_items is None else done_items
    if holders_lines is None:
        holders_lines = ["", "Пояснение до таблицы.", ""] + tbl(HOLD_HEAD, [HOLD_ROW]) + ["", "После таблицы."]
    dict_lines = ["", "- термин - слово."] if dict_lines is None else dict_lines
    backlog = ["", "- заметка"] if backlog is None else backlog
    lines = ["# План работ Тест"] + header
    lines += [f"## Блокировки - кого ждем - {blk_n}", ""] + tbl(BLK_HEAD, blk_rows) + [""]
    lines += [f"## В работе сейчас - {work_n}", ""] + tbl(WORK_HEAD, work_rows) + [""]
    lines += [f"## Риски - {risk_n}", ""] + tbl(RISK_HEAD, risk_rows) + [""]
    lines += ["## Дальше", "", "1. [наша очередь] что-то.", ""]
    lines += ["## Бэклог (не срочно)"] + backlog + [""]
    lines += ["## Готово за сутки"] + done_items + [""]
    lines += ["## Держатели"] + holders_lines + [""]
    lines += ["## Словарь"] + dict_lines
    return "\n".join(lines) + "\n"


def cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def holder_rows(text: str) -> list[list[str]]:
    """Строки данных первой таблицы раздела '## Держатели'."""
    lines = text.split("\n")
    i = lines.index("## Держатели")
    while not lines[i].startswith("| Держатель"):
        i += 1
    i += 2
    out = []
    while i < len(lines) and lines[i].startswith("|"):
        out.append(cells(lines[i]))
        i += 1
    return out


def s(asked: str, answered: str | None = None) -> dict:
    d = {"what": "x", "asked": asked}
    if answered is not None:
        d["answered"] = answered
    return d


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def day_samples(durations_days: list[float], base: datetime) -> list[dict]:
    """Замеры с заданными длительностями; answered растет на сутки с индексом."""
    out = []
    for k, d in enumerate(durations_days):
        ans = base + timedelta(days=k)
        out.append(s(iso(ans - timedelta(minutes=round(d * 1440))), iso(ans)))
    return out


def recalc_data() -> dict:
    base = datetime(2026, 10, 1, 12, 0, tzinfo=MSK)
    # 11 закрытых замеров: самый ранний по answered (k=0) - 40 суток, остальные
    # 10 - "последние 10". Порядок в файле перемешан.
    durs = {0: 40.0, 1: 3.3, 2: 0.8, 3: 5.5, 4: 1.6, 5: 0.2, 6: 2.4, 7: 1.1, 8: 1.6, 9: 2.0, 10: 0.5}
    big = []
    for k in [4, 9, 2, 7, 1, 0, 8, 3, 10, 5, 6]:
        ans = base + timedelta(days=k)
        big.append(s(iso(ans - timedelta(minutes=round(durs[k] * 1440))), iso(ans)))
    five = day_samples([0.3, 0.6, 1.2, 2.5, 7.0], datetime(2026, 11, 1, 12, 0, tzinfo=MSK))
    five.append(s("2026-11-05T12:00+03:00"))  # не закрыт
    five.append(s("2026-11-10T12:00+03:00", "2026-11-09T12:00+03:00"))  # answered < asked
    exact = day_samples([2, 2, 2, 2, 2], datetime(2026, 11, 1, 12, 0, tzinfo=MSK))
    minone = day_samples([0.125] * 5, datetime(2026, 11, 1, 12, 0, tzinfo=MSK))
    few = day_samples([1, 1, 1, 1], datetime(2026, 11, 1, 12, 0, tzinfo=MSK))
    few.append(s("2026-11-07T12:00+03:00"))  # 4 закрытых + 1 открытый
    nodef = day_samples([1, 1], datetime(2026, 11, 1, 12, 0, tzinfo=MSK))
    return {
        "holders": {
            "few": {"default": [2, 5], "samples": few},
            "five": {"label": "Пять (тест)", "samples": five},
            "nodef": {"label": "Без нормы", "samples": nodef},
            "big": {"label": "Большой", "default": [1, 3], "samples": big},
            "exact": {"samples": exact},
            "minone": {"samples": minone},
        },
        "open_waits": [
            {"id": "a", "holder": "few", "since": "2026-11-22T17:05+03:00", "where": "PR 1"},
            {"id": "b", "holder": "big", "since": "2026-11-22T17:05+03:00", "where": "PR 2"},
            {"id": "c", "holder": "big", "since": "2026-11-21T10:00+03:00", "where": "PR 3"},
            {"id": "d", "holder": "big", "since": "2026-11-20T10:00+03:00", "where": "PR 4"},
        ],
    }


STALE_HOLDERS = (
    ["", "Пояснение до таблицы.", "", "Второй абзац - не трогать.", ""]
    + tbl(HOLD_HEAD, ["| Устарел | 9 | 9 | 9 | 9 |", "| Старый два | 8 | 8 | 8 | 8 |"])
    + ["", "После таблицы.", "", "| Другая | таблица |", "| --- | --- |", "| Устарел | 7 |"]
)

CLEAN_DATA = {"holders": {"dwl": {"default": [1, 3], "samples": []}}, "open_waits": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "plans").mkdir()
        (self.root / "data").mkdir()
        self.plan = self.root / "plans" / "plan.md"
        self.data = self.root / "data" / "sroki.json"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        plans = self.root / "plans"
        if plans.exists():
            plans.chmod(0o755)
        self.tmp.cleanup()

    def put_plan(self, text: str) -> str:
        self.plan.write_bytes(text.encode("utf-8"))
        return text

    def put_data(self, obj) -> None:
        self.data.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def run_cmd(self, command: str, write=False, now=NOW, plan=None, data=None, extra=()):
        self.assertTrue(SCRIPT.is_file(), "нет scripts/plan-dates.py")
        args = [sys.executable, str(SCRIPT), command,
                "--plan", str(plan or self.plan), "--data", str(data or self.data)]
        if now is not None:
            args += ["--now", now]
        if write:
            args.append("--write")
        args += list(extra)
        return subprocess.run(args, capture_output=True, text=True, cwd=str(self.root))

    def plan_bytes(self) -> bytes:
        return self.plan.read_bytes()

    @staticmethod
    def findings(proc):
        lines = proc.stdout.splitlines()
        errs = [l for l in lines if l.startswith("ERROR:")]
        warns = [l for l in lines if l.startswith("WARN:")]
        return errs, warns


class RecalcTests(Base):
    def recalc(self, text=None, write=True):
        self.put_plan(text if text is not None else plan_text(holders_lines=STALE_HOLDERS))
        self.put_data(recalc_data())
        proc = self.run_cmd("recalc", write=write)
        return proc

    def rows(self) -> dict[str, list[str]]:
        text = self.plan_bytes().decode("utf-8")
        return {r[0]: r for r in holder_rows(text)}

    def test_median_and_second_largest_of_last_10_by_answered(self):
        # последние 10 по answered: медиана 1.6 -> 2, второе по величине 3.3 -> 4.
        # Среднее/максимум/окно "все 11" дали бы 6/6, окно "по порядку файла" - иное.
        proc = self.recalc()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = self.rows()["Большой"]
        self.assertEqual(r[1], "2")
        self.assertEqual(r[2], "4")

    def test_five_samples_threshold_and_only_closed_counted(self):
        # 5 закрытых + открытый + answered<asked: замеров 5, норма по данным.
        # Медиана 1.2 -> 2 (среднее 2.32 дало бы 3), второе по величине 2.5 -> 3.
        self.recalc()
        r = self.rows()["Пять (тест)"]
        self.assertEqual(r[1], "2")
        self.assertEqual(r[2], "3")
        self.assertRegex(r[3], r"^5\b")
        self.assertNotIn("по умолчанию", r[3])

    def test_integer_days_are_not_rounded_up_further(self):
        self.recalc()
        r = self.rows()["exact"]
        self.assertEqual((r[1], r[2]), ("2", "2"))

    def test_minimum_one_day(self):
        self.recalc()
        r = self.rows()["minone"]
        self.assertEqual((r[1], r[2]), ("1", "1"))

    def test_few_samples_use_holder_default(self):
        # 4 закрытых (+1 открытый) < 5: default [2, 5], пометка "мало данных".
        self.recalc()
        r = self.rows()["few"]
        self.assertEqual((r[1], r[2]), ("2", "5"))
        self.assertEqual(r[3], "4 (по умолчанию: мало данных)")

    def test_default_is_1_3_when_not_given(self):
        self.recalc()
        r = self.rows()["Без нормы"]
        self.assertEqual((r[1], r[2]), ("1", "3"))
        self.assertEqual(r[3], "2 (по умолчанию: мало данных)")

    def test_label_or_key_in_first_column(self):
        self.recalc()
        names = set(self.rows())
        self.assertTrue({"few", "Пять (тест)", "Без нормы", "Большой", "exact", "minone"} <= names)
        self.assertNotIn("five", names)
        self.assertNotIn("big", names)

    def test_open_count_from_open_waits(self):
        self.recalc()
        rows = self.rows()
        self.assertEqual(rows["Большой"][4], "3")
        self.assertEqual(rows["few"][4], "1")
        self.assertEqual(rows["exact"][4], "0")

    def test_row_order_is_holders_key_order_and_stale_rows_dropped(self):
        self.recalc()
        text = self.plan_bytes().decode("utf-8")
        order = [r[0] for r in holder_rows(text)]
        self.assertEqual(order, ["few", "Пять (тест)", "Без нормы", "Большой", "exact", "minone"])
        self.assertNotIn("Устарел |", text.split("## Держатели")[1].split("После таблицы.")[0])

    def test_text_outside_table_is_byte_identical(self):
        text = plan_text(holders_lines=STALE_HOLDERS)
        self.put_plan(text)
        self.put_data(recalc_data())
        before = self.plan_bytes()
        proc = self.run_cmd("recalc", write=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = self.plan_bytes()
        self.assertNotEqual(before, after)
        tb = text.encode("utf-8")
        head_end = tb.index(HOLD_HEAD.encode()) + len(HOLD_HEAD.encode())
        stale_last = "| Старый два | 8 | 8 | 8 | 8 |\n"
        tail_start = tb.index(stale_last.encode()) + len(stale_last.encode())
        self.assertTrue(after.startswith(before[:head_end]))
        self.assertTrue(after.endswith(before[tail_start:]))
        # вторая таблица раздела и ее "Устарел | 7" на месте
        self.assertIn("| Устарел | 7 |", after.decode("utf-8"))

    def test_no_final_newline_and_other_tables_preserved(self):
        text = plan_text(holders_lines=STALE_HOLDERS).rstrip("\n")
        self.put_plan(text)
        self.put_data(recalc_data())
        proc = self.run_cmd("recalc", write=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = self.plan_bytes().decode("utf-8")
        before_other = text[: text.index("## Держатели")]
        self.assertTrue(after.startswith(before_other))
        tail = text[text.index("После таблицы."):]  # хвост после таблицы: вторая таблица и словарь
        self.assertTrue(tail.endswith("- термин - слово."))
        self.assertFalse(after.endswith("\n"), "финального перевода строки не было - не должно появиться")
        self.assertTrue(after.endswith(tail))

    def test_without_write_file_unchanged_and_reports(self):
        self.put_plan(plan_text(holders_lines=STALE_HOLDERS))
        self.put_data(recalc_data())
        before = self.plan_bytes()
        proc = self.run_cmd("recalc", write=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.plan_bytes(), before)
        self.assertTrue(proc.stdout.strip(), "без --write нужно напечатать, что изменилось бы")

    def test_missing_holders_section_exit_2(self):
        text = plan_text().replace("## Держатели", "## Другое")
        self.put_plan(text)
        self.put_data(recalc_data())
        before = self.plan_bytes()
        proc = self.run_cmd("recalc", write=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.plan_bytes(), before)

    def test_naive_times_are_moscow(self):
        # asked +03:00, answered без пояса = МСК: ровно 2 суток -> 2/2.
        # Прочтение без пояса как UTC дало бы 2.125 суток -> 3.
        data = {"holders": {"tz": {"samples": [
            s(f"2026-11-{10 + k:02d}T00:00+03:00", f"2026-11-{12 + k:02d}T00:00") for k in range(5)
        ]}}, "open_waits": []}
        self.put_plan(plan_text(holders_lines=["", *tbl(HOLD_HEAD, ["| x | 0 | 0 | 0 | 0 |"])]))
        self.put_data(data)
        proc = self.run_cmd("recalc", write=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        r = holder_rows(self.plan_bytes().decode("utf-8"))[0]
        self.assertEqual((r[0], r[1], r[2]), ("tz", "2", "2"))


class PruneTests(Base):
    def prune(self, items, now=NOW, write=True, **kw):
        self.put_plan(plan_text(done_items=items, **kw))
        self.put_data(CLEAN_DATA)
        return self.run_cmd("prune", write=write, now=now)

    def expect(self, items, expected_items, now=NOW, **kw):
        proc = self.prune(items, now=now, **kw)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.plan_bytes().decode("utf-8"), plan_text(done_items=expected_items, **kw))

    def test_leading_stamp_older_than_24h_removed_fresh_kept(self):
        old = "- 22.11 11:59 - старый"
        edge = "- 22.11 12:00 - ровно сутки"
        fresh = "- 22.11 12:01 - свежий"
        self.expect([old, edge, fresh], [edge, fresh])

    def test_exactly_24h_stays(self):
        edge = "- 22.11 12:00 - ровно сутки"
        self.expect([edge], [edge])

    def test_trailing_parenthesised_stamp(self):
        old = "- сделано (оценка 1-2, факт 1; 22.11 11:00)"
        fresh = "- сделано (оценка 1-2, факт 1; 23.11 09:00)"
        self.expect([old, fresh], [fresh])

    # Уточнение 13: отметка в скобках, после скобки - точка (шаблон ранбука)
    RB_OLD = "- Этап сделан (работа 1-2, факт 1, ожидания 0 дн; 20.11 10:00)."
    RB_FRESH = "- Этап сделан (работа 1-2, факт 1, ожидания 0 дн; 23.11 10:00)."

    def test_trailing_stamp_with_dot_old_removed_fresh_kept(self):
        self.expect([self.RB_OLD, self.RB_FRESH], [self.RB_FRESH])

    def test_trailing_stamp_with_dot_exactly_24h_stays(self):
        edge = "- Этап (факт 1; 22.11 12:00)."
        self.expect([edge], [edge])

    def test_trailing_stamp_with_dot_and_trailing_spaces(self):
        old = self.RB_OLD + "  "
        fresh = self.RB_FRESH + "  "
        self.expect([old, fresh], [fresh])

    def test_last_parenthesised_stamp_wins(self):
        item = "- что (заметка 20.11 10:00) итог (факт; 23.11 09:00)"
        self.expect([item], [item])
        item2 = "- что (заметка 23.11 10:00) итог (факт; 20.11 09:00)"
        self.expect([item2], [])

    def test_continuation_lines_removed_with_item_only(self):
        items = [
            "- 20.11 10:00 - старый",
            "  продолжение с пробелами",
            "\tпродолжение с табом",
            "- 23.11 10:00 - свежий",
            "  его продолжение",
            "- 20.11 11:00 - еще старый",
            "- 23.11 11:00 - последний свежий",
        ]
        expected = [
            "- 23.11 10:00 - свежий",
            "  его продолжение",
            "- 23.11 11:00 - последний свежий",
        ]
        self.expect(items, expected)

    def test_item_without_time_stays_with_continuation(self):
        items = ["- без времени", "  продолжение", "- 20.11 10:00 - старый"]
        self.expect(items, ["- без времени", "  продолжение"])

    def test_stamp_in_middle_is_not_readable(self):
        item = "- сделал 20.11 10:00 что-то"
        self.expect([item], [item])

    def test_text_after_list_stays(self):
        items = ["- 20.11 10:00 - старый", "", "Хранятся 24 часа с момента закрытия."]
        self.expect(items, ["", "Хранятся 24 часа с момента закрытия."])

    def test_only_done_section_touched(self):
        backlog = ["", "- старая заметка (01.01 10:00)", "- 01.01 10:00 - тоже в бэклоге"]
        self.expect(["- 20.11 10:00 - старый"], [], backlog=backlog)

    def test_timezone_of_now_is_respected(self):
        # 2026-11-23T09:00Z = 12:00 МСК: 22.11 12:00 МСК - ровно 24 ч, остается.
        item = "- 22.11 12:00 - сутки"
        self.expect([item], [item], now="2026-11-23T09:00:00+00:00")
        # на минуту позже - старше суток, удаляется.
        self.expect([item], [], now="2026-11-23T09:01:00+00:00")

    def test_future_date_beyond_a_day_is_previous_year(self):
        # 25.11 при now 23.11 12:00 - в будущем больше суток -> 2025 год -> старый.
        self.expect(["- 25.11 10:00 - прошлогодний"], [])

    def test_future_date_within_a_day_is_current_year(self):
        item = "- 24.11 10:00 - чуть вперед"
        self.expect([item], [item])

    def test_year_boundary(self):
        now = "2026-01-02T12:00:00+03:00"
        old = "- 31.12 10:00 - прошлый год, 26 ч"
        fresh = "- 01.01 13:00 - 23 ч"
        self.expect([old, fresh], [fresh], now=now)

    def test_without_write_file_unchanged(self):
        items = ["- 20.11 10:00 - старый"]
        proc = self.prune(items, write=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.plan_bytes().decode("utf-8"), plan_text(done_items=items))
        self.assertTrue(proc.stdout.strip())

    def test_nothing_to_prune_file_identical(self):
        items = ["- 23.11 10:00 - свежий", "- без времени"]
        before = self.put_plan(plan_text(done_items=items)).encode("utf-8")
        self.put_data(CLEAN_DATA)
        proc = self.run_cmd("prune", write=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.plan_bytes(), before)


class LintTests(Base):
    def lint(self, text=None, data=None, now=NOW, command="lint"):
        self.put_plan(text if text is not None else plan_text())
        self.put_data(data if data is not None else CLEAN_DATA)
        return self.run_cmd(command, now=now)

    def assertErrors(self, proc):
        errs, _ = self.findings(proc)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertGreaterEqual(len(errs), 1, proc.stdout)

    def assertClean(self, proc):
        errs, warns = self.findings(proc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((errs, warns), ([], []), proc.stdout)

    def assertOneWarn(self, proc):
        errs, warns = self.findings(proc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(errs, [], proc.stdout)
        self.assertEqual(len(warns), 1, proc.stdout)

    # --- контроль и формат
    def test_clean_plan_exit_0(self):
        self.assertClean(self.lint())

    def test_finding_line_format(self):
        proc = self.lint(plan_text(work_n=5, updated_line="Обновлено: 20.11 11:00 MSK."))
        lines = [l for l in proc.stdout.splitlines() if l.startswith(("ERROR", "WARN"))]
        self.assertGreaterEqual(len(lines), 2)
        for l in lines:
            self.assertRegex(l, r"^(ERROR|WARN): \S")

    # --- ERROR
    def test_error_no_updated_line(self):
        text = plan_text(header=["", "Цель: проверка."])
        self.assertErrors(self.lint(text))

    def test_error_two_updated_lines(self):
        text = plan_text(header=["", "Обновлено: 23.11 11:00 MSK.", "Обновлено: 23.11 10:00 MSK.", "Цель."])
        self.assertErrors(self.lint(text))

    def test_error_updated_line_outside_header(self):
        text = plan_text(header=["", "Цель: проверка."], dict_lines=["", "Обновлено: 23.11 11:00 MSK."])
        self.assertErrors(self.lint(text))

    def test_error_row_with_fewer_columns(self):
        text = plan_text(work_rows=["| Claude2 | Работа | этап 1 | 22.11 01:20 | 24.11 | ветка |"])
        self.assertErrors(self.lint(text))

    def test_error_row_with_more_columns(self):
        text = plan_text(work_rows=[WORK_ROW[:-1] + "| лишняя |"])
        self.assertErrors(self.lint(text))

    def test_error_duplicate_row_in_table(self):
        text = plan_text(risk_rows=[RISK_ROWS[0], RISK_ROWS[0]])
        self.assertErrors(self.lint(text))

    def test_same_row_in_different_tables_is_ok(self):
        row = "| a | b | c | d | e | f | g |"
        text = plan_text(blk_rows=[row], work_rows=[row])
        self.assertClean(self.lint(text))

    def test_error_blocking_empty_snimetsya(self):
        text = plan_text(blk_rows=["| dwl | Решение | этап 1 | 22.11 |  | [ссылка](x) | - |"])
        self.assertErrors(self.lint(text))

    def test_error_blocking_empty_gde(self):
        text = plan_text(blk_rows=["| dwl | Решение | этап 1 | 22.11 | 24.11 |  | - |"])
        self.assertErrors(self.lint(text))

    def test_blocking_empty_note_is_ok(self):
        text = plan_text(blk_rows=["| dwl | Решение | этап 1 | 22.11 | 24.11 | [ссылка](x) |  |"])
        self.assertClean(self.lint(text))

    def test_nothing_waiting_row_exempt_from_snimetsya_gde(self):
        text = plan_text(blk_n=0, blk_rows=["| - | ничего не ждем |  |  |  |  |  |"])
        self.assertClean(self.lint(text))

    def test_error_inwork_empty_gotovo(self):
        text = plan_text(work_rows=["| Claude2 | Работа | этап 1 | 22.11 01:20 |  | ветка | - |"])
        self.assertErrors(self.lint(text))

    def test_inwork_empty_note_is_ok(self):
        text = plan_text(work_rows=["| Claude2 | Работа | этап 1 | 22.11 01:20 | 24.11 | ветка |  |"])
        self.assertClean(self.lint(text))

    def test_error_blocking_count_mismatch(self):
        self.assertErrors(self.lint(plan_text(blk_n=2)))

    def test_error_inwork_count_mismatch(self):
        self.assertErrors(self.lint(plan_text(work_n=3)))

    def test_error_risks_count_mismatch(self):
        self.assertErrors(self.lint(plan_text(risk_n=5)))

    def test_nothing_waiting_row_not_counted_in_n(self):
        # N считает строки без "ничего не ждем": N=1 при единственной такой строке - ошибка.
        text = plan_text(blk_n=1, blk_rows=["| - | ничего не ждем |  |  |  |  |  |"])
        self.assertErrors(self.lint(text))

    def test_error_done_item_without_time(self):
        self.assertErrors(self.lint(plan_text(done_items=["- 23.11 10:00 - ок", "- без времени"])))

    def test_error_done_item_stamp_in_middle(self):
        self.assertErrors(self.lint(plan_text(done_items=["- сделал 23.11 10:00 что-то"])))

    # --- WARN
    def test_warn_updated_older_than_24h(self):
        text = plan_text(updated_line="Обновлено: 22.11 11:59 MSK.")
        self.assertOneWarn(self.lint(text))

    def test_updated_exactly_24h_no_warn(self):
        text = plan_text(updated_line="Обновлено: 22.11 12:00 MSK.")
        self.assertClean(self.lint(text))

    def test_warn_done_item_older_than_24h_leading(self):
        text = plan_text(done_items=["- 22.11 11:59 - старый"])
        self.assertOneWarn(self.lint(text))

    def test_warn_done_item_older_than_24h_trailing(self):
        text = plan_text(done_items=["- старый (итог; 22.11 11:59)"])
        self.assertOneWarn(self.lint(text))

    def test_done_item_exactly_24h_no_warn(self):
        self.assertClean(self.lint(plan_text(done_items=["- 22.11 12:00 - сутки"])))

    def wait_data(self, holder_cfg, since: datetime):
        return {"holders": {"root": holder_cfg},
                "open_waits": [{"id": "w1", "holder": "root", "since": iso(since), "where": "PR #1"}]}

    def test_warn_burns_when_wait_exceeds_default_zatyazhno(self):
        data = self.wait_data({"samples": []}, NOW_DT - timedelta(days=5))
        proc = self.lint(data=data)
        self.assertOneWarn(proc)
        self.assertIn("горит", proc.stdout)

    def test_wait_within_default_zatyazhno_not_burning(self):
        data = self.wait_data({"samples": []}, NOW_DT - timedelta(days=2))
        self.assertClean(self.lint(data=data))

    def test_custom_default_used_for_burning(self):
        cfg = {"default": [2, 5], "samples": []}
        self.assertClean(self.lint(data=self.wait_data(cfg, NOW_DT - timedelta(days=4))))
        proc = self.lint(data=self.wait_data(cfg, NOW_DT - timedelta(days=6)))
        self.assertOneWarn(proc)
        self.assertIn("горит", proc.stdout)

    def test_burning_uses_data_not_table_in_plan(self):
        # по данным затяжно = 4 (второе по величине 3.3 из 10 последних);
        # в таблице плана стоит 99 - ее читать нельзя.
        big = recalc_data()["holders"]["big"]
        holders = ["", *tbl(HOLD_HEAD, ["| root | 1 | 99 | 11 | 1 |"])]
        text = plan_text(holders_lines=holders)
        data = self.wait_data(big, NOW_DT - timedelta(days=6))
        proc = self.lint(text, data=data)
        self.assertOneWarn(proc)
        self.assertIn("горит", proc.stdout)
        data = self.wait_data(big, NOW_DT - timedelta(days=1))
        self.assertClean(self.lint(text, data=data))

    # Уточнение 13 (lint)
    def test_trailing_stamp_with_dot_is_readable_fresh_clean(self):
        item = "- Этап сделан (работа 1-2, факт 1, ожидания 0 дн; 23.11 10:00)."
        self.assertClean(self.lint(plan_text(done_items=[item])))

    def test_trailing_stamp_with_dot_and_spaces_is_readable(self):
        item = "- Этап сделан (работа 1-2, факт 1, ожидания 0 дн; 23.11 10:00).  "
        self.assertClean(self.lint(plan_text(done_items=[item])))

    def test_trailing_stamp_with_dot_old_is_warn_not_error(self):
        item = "- Этап сделан (работа 1-2, факт 1, ожидания 0 дн; 20.11 10:00)."
        self.assertOneWarn(self.lint(plan_text(done_items=[item])))

    def test_burning_boundary_exactly_zatyazhno_not_burning(self):
        # норма "затяжно" по умолчанию = 3 дн; возраст ровно 3 дн - не горит (строго больше)
        data = self.wait_data({"samples": []}, NOW_DT - timedelta(days=3))
        proc = self.lint(data=data)
        self.assertClean(proc)
        self.assertNotIn("горит", proc.stdout)

    def test_burning_boundary_just_over_zatyazhno_burns(self):
        data = self.wait_data({"samples": []}, NOW_DT - timedelta(days=3, minutes=1))
        proc = self.lint(data=data)
        self.assertOneWarn(proc)
        self.assertIn("горит", proc.stdout)
        self.assertIn("w1", proc.stdout)

    def test_warn_more_than_7_risks(self):
        rows = [f"| Claude2 | средний | Риск {i} | грозит | признак | мера | 20.11 |" for i in range(8)]
        self.assertOneWarn(self.lint(plan_text(risk_rows=rows)))

    def test_7_risks_no_warn(self):
        rows = [f"| Claude2 | средний | Риск {i} | грозит | признак | мера | 20.11 |" for i in range(7)]
        self.assertClean(self.lint(plan_text(risk_rows=rows)))

    def test_warn_header_more_than_5_nonempty_lines(self):
        header = ["", "Обновлено: 23.11 11:00 MSK.", "Строка 2.", "Строка 3.", "Строка 4.", "", "Строка 5.", "Строка 6."]
        self.assertOneWarn(self.lint(plan_text(header=header)))

    def test_header_5_nonempty_lines_no_warn(self):
        header = ["", "Обновлено: 23.11 11:00 MSK.", "Строка 2.", "Строка 3.", "Строка 4.", "", "Строка 5."]
        self.assertClean(self.lint(plan_text(header=header)))

    def test_warnings_alone_exit_0(self):
        proc = self.lint(plan_text(updated_line="Обновлено: 20.11 11:00 MSK."))
        self.assertEqual(proc.returncode, 0)


class AllTests(Base):
    def test_prune_runs_before_lint(self):
        items = ["- 20.11 10:00 - старый", "- 23.11 10:00 - свежий"]
        text = plan_text(done_items=items)
        self.put_plan(text)
        self.put_data(CLEAN_DATA)
        # контроль: lint один видит старый элемент
        proc = self.run_cmd("lint")
        _, warns = self.findings(proc)
        self.assertEqual(len(warns), 1, proc.stdout)
        # all --write: элемент удален prune до lint, предупреждения нет
        proc = self.run_cmd("all", write=True)
        errs, warns = self.findings(proc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((errs, warns), ([], []), proc.stdout)
        self.assertEqual(self.plan_bytes().decode("utf-8"),
                         plan_text(done_items=["- 23.11 10:00 - свежий"]))

    def test_all_write_applies_recalc_and_prune(self):
        self.put_plan(plan_text(holders_lines=STALE_HOLDERS, done_items=["- 20.11 10:00 - старый"]))
        self.put_data(recalc_data())
        proc = self.run_cmd("all", write=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = self.plan_bytes().decode("utf-8")
        self.assertNotIn("старый", text)
        rows = {r[0]: r for r in holder_rows(text)}
        self.assertEqual((rows["Большой"][1], rows["Большой"][2]), ("2", "4"))

    def test_all_without_write_file_unchanged(self):
        text = plan_text(holders_lines=STALE_HOLDERS, done_items=["- 20.11 10:00 - старый"])
        self.put_plan(text)
        self.put_data(recalc_data())
        proc = self.run_cmd("all")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.plan_bytes().decode("utf-8"), text)

    def test_all_exit_1_on_lint_error(self):
        self.put_plan(plan_text(work_n=4))
        self.put_data(CLEAN_DATA)
        proc = self.run_cmd("all", write=True)
        errs, _ = self.findings(proc)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertGreaterEqual(len(errs), 1)


class CallErrorTests(Base):
    def test_missing_plan_file_exit_2(self):
        self.put_data(CLEAN_DATA)
        for cmd in ("recalc", "prune", "lint", "all"):
            with self.subTest(cmd=cmd):
                proc = self.run_cmd(cmd, plan=self.root / "plans" / "net.md")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_missing_data_file_exit_2(self):
        self.put_plan(plan_text())
        for cmd in ("recalc", "lint", "all"):
            with self.subTest(cmd=cmd):
                proc = self.run_cmd(cmd, data=self.root / "data" / "net.json")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_broken_json_exit_2(self):
        self.put_plan(plan_text())
        self.data.write_text("{ не json", encoding="utf-8")
        for cmd in ("recalc", "lint", "all"):
            with self.subTest(cmd=cmd):
                before = self.plan_bytes()
                proc = self.run_cmd(cmd, write=True)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(self.plan_bytes(), before)

    def test_unknown_command_exit_2(self):
        self.put_plan(plan_text())
        self.put_data(CLEAN_DATA)
        proc = self.run_cmd("bogus")
        self.assertEqual(proc.returncode, 2)

    def test_missing_plan_argument_exit_2(self):
        self.put_data(CLEAN_DATA)
        self.assertTrue(SCRIPT.is_file(), "нет scripts/plan-dates.py")
        proc = subprocess.run([sys.executable, str(SCRIPT), "lint", "--data", str(self.data), "--now", NOW],
                              capture_output=True, text=True, cwd=str(self.root))
        self.assertEqual(proc.returncode, 2)


class WriteTests(Base):
    def test_write_is_complete_and_leaves_no_temp_files(self):
        self.put_plan(plan_text(holders_lines=STALE_HOLDERS, done_items=["- 20.11 10:00 - старый"]))
        self.put_data(recalc_data())
        plans = self.root / "plans"
        before_names = sorted(p.name for p in plans.iterdir())
        proc = self.run_cmd("all", write=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(p.name for p in plans.iterdir()), before_names)
        text = self.plan_bytes().decode("utf-8")  # корректный UTF-8, целый файл
        self.assertTrue(text.startswith("# План работ Тест\n"))
        tail = plan_text(holders_lines=STALE_HOLDERS)
        tail = tail[tail.index("После таблицы."):]
        self.assertTrue(text.endswith(tail), "хвост после таблицы (вторая таблица, словарь) цел")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root обходит права каталога")
    def test_unwritable_directory_exit_2_original_intact(self):
        text = plan_text(holders_lines=STALE_HOLDERS)
        self.put_plan(text)
        self.put_data(recalc_data())
        before = self.plan_bytes()
        plans = self.root / "plans"
        plans.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            proc = self.run_cmd("recalc", write=True)
        finally:
            plans.chmod(0o755)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(self.plan_bytes(), before)
        self.assertEqual(sorted(p.name for p in plans.iterdir()), ["plan.md"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
