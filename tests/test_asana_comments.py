#!/usr/bin/env python3
"""Тесты отбора ленты в asana-comments.py. stdlib-only (unittest).

Запуск: python3 tests/test_asana_comments.py

Покрывают select_stories - единственную логику скрипта, проверяемую без сети
(остальное это HTTP к Asana). Ключевой кейс - регресс на срез: stories[-last:]
при last=0 возвращал ВЕСЬ список, потому что в Python -0 == 0.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "asana-comments.py"
_spec = importlib.util.spec_from_file_location("asana_comments", SCRIPT)
asana = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asana)


def story(day: str, subtype: str = "comment_added", text: str = "") -> dict:
    return {
        "created_at": f"2026-07-{day}T12:00:00.000Z",
        "resource_subtype": subtype,
        "text": text or f"комментарий {day}",
        "created_by": {"name": "кто-то"},
    }


# Asana отдает ленту от старых к новым - порядок здесь такой же
LENTA = [
    story("18"),
    story("19", subtype="assigned"),
    story("20"),
    story("21"),
    story("22"),
]


def select(**kw):
    params = {"system": False, "since": None, "last": 10, "all_": False}
    params.update(kw)
    return asana.select_stories(LENTA, **params)


class Filters(unittest.TestCase):
    def test_system_events_excluded_by_default(self):
        got, total = select()
        self.assertEqual(total, 4)
        self.assertTrue(all(s["resource_subtype"] == "comment_added" for s in got))

    def test_system_flag_includes_events(self):
        got, total = select(system=True)
        self.assertEqual(total, 5)
        self.assertIn("assigned", [s["resource_subtype"] for s in got])

    def test_since_is_inclusive_on_the_day(self):
        got, _ = select(since="2026-07-21")
        self.assertEqual([s["text"] for s in got], ["комментарий 21", "комментарий 22"])

    def test_since_excludes_earlier(self):
        got, total = select(since="2026-07-22")
        self.assertEqual(total, 1)
        self.assertEqual(got[0]["text"], "комментарий 22")


class Slice(unittest.TestCase):
    def test_last_takes_newest_from_the_end(self):
        got, total = select(last=2)
        self.assertEqual([s["text"] for s in got], ["комментарий 21", "комментарий 22"])
        self.assertEqual(total, 4, "total считается до среза")

    def test_last_zero_returns_nothing(self):
        """Регресс: stories[-0:] отдавал весь список, то есть "ноль последних"
        молча превращалось во "все"."""
        got, total = select(last=0)
        self.assertEqual(got, [])
        self.assertEqual(total, 4)

    def test_negative_last_returns_nothing(self):
        got, _ = select(last=-5)
        self.assertEqual(got, [])

    def test_last_larger_than_available(self):
        got, _ = select(last=100)
        self.assertEqual(len(got), 4)

    def test_all_ignores_last(self):
        got, total = select(all_=True, last=1)
        self.assertEqual(len(got), 4)
        self.assertEqual(total, 4)

    def test_all_with_system_returns_everything(self):
        got, total = select(all_=True, system=True)
        self.assertEqual(len(got), 5)
        self.assertEqual(total, 5)


class Combined(unittest.TestCase):
    def test_since_then_last(self):
        got, total = select(since="2026-07-20", last=1)
        self.assertEqual(total, 3)
        self.assertEqual([s["text"] for s in got], ["комментарий 22"])

    def test_empty_input(self):
        got, total = asana.select_stories([], system=False, since=None, last=10, all_=False)
        self.assertEqual((got, total), ([], 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
