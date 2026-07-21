#!/usr/bin/env python3
"""Тесты разбора конфигов telegram-скриптов. stdlib-only (unittest).

Запуск: python3 tests/test_telegram_config.py

Покрывают мульти-аккаунт: секцию accounts в auth.json и поле account в записи
чата. Проверяются чистые функции (load_auth, chat_entry) - сеть и telethon для
этого не нужны, telethon подменяется заглушкой.

load_auth и chat_entry намеренно продублированы в telegram-snapshot.py и
telegram-send.py (скрипты самодостаточны). Поэтому каждый кейс гоняется по
ОБЕИМ копиям: тест ловит их расхождение, если правку внесли только в одну.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _stub_telethon() -> None:
    """Заглушка telethon: скрипты импортируют его на верхнем уровне и без него
    делают sys.exit(2), а тесты в этом репо stdlib-only."""
    try:
        import telethon  # noqa: F401
        return
    except ImportError:
        pass

    def make(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        # любое имя из telethon.tl.types отдаем как пустой класс-заглушку
        mod.__getattr__ = lambda attr: type(attr, (), {})  # type: ignore[method-assign]
        return mod

    telethon = make("telethon")
    tl = make("telethon.tl")
    tl_types = make("telethon.tl.types")
    telethon.tl = tl
    tl.types = tl_types
    sys.modules.update(
        {"telethon": telethon, "telethon.tl": tl, "telethon.tl.types": tl_types}
    )


def load_module(filename: str, name: str):
    _stub_telethon()
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SNAPSHOT = load_module("telegram-snapshot.py", "tg_snapshot")
SEND = load_module("telegram-send.py", "tg_send")
DELTAS = load_module("telegram-deltas.py", "tg_deltas")

# обе копии хелперов должны вести себя одинаково
BOTH = (("snapshot", SNAPSHOT), ("send", SEND))


@contextlib.contextmanager
def auth_file(mod, payload: dict):
    """Подменяет mod.AUTH_PATH временным auth.json."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "auth.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        original = mod.AUTH_PATH
        mod.AUTH_PATH = path
        try:
            yield
        finally:
            mod.AUTH_PATH = original


def expect_exit(testcase, mod, payload: dict, account: str):
    """load_auth должен завершиться кодом 2, stderr гасим."""
    with auth_file(mod, payload):
        with contextlib.redirect_stderr(io.StringIO()):
            with testcase.assertRaises(SystemExit) as caught:
                mod.load_auth(account)
    testcase.assertEqual(caught.exception.code, 2)


FLAT = {"api_id": 1, "api_hash": "h", "proxy": "socks5://127.0.0.1:7890"}
MULTI = {
    "api_id": 1,
    "api_hash": "h",
    "proxy": "socks5://127.0.0.1:7890",
    "accounts": {"default": {}, "cv": {"session_name": "cv"}},
}


class LoadAuth(unittest.TestCase):
    def test_flat_format_reads_as_default_account(self):
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, FLAT):
                auth = mod.load_auth()
                self.assertEqual(auth["api_id"], 1)
                self.assertEqual(auth["session_name"], "default")

    def test_flat_format_keeps_explicit_session_name(self):
        payload = {**FLAT, "session_name": "legacy"}
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, payload):
                self.assertEqual(mod.load_auth()["session_name"], "legacy")

    def test_flat_format_rejects_named_account(self):
        """Старый конфиг не знает про аккаунты - просить cv бессмысленно."""
        for name, mod in BOTH:
            with self.subTest(mod=name):
                expect_exit(self, mod, FLAT, "cv")

    def test_accounts_resolve_each(self):
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, MULTI):
                self.assertEqual(mod.load_auth("default")["session_name"], "default")
                self.assertEqual(mod.load_auth("cv")["session_name"], "cv")

    def test_session_name_defaults_to_account_name(self):
        payload = {"api_id": 1, "api_hash": "h", "accounts": {"cv": {}}}
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, payload):
                self.assertEqual(mod.load_auth("cv")["session_name"], "cv")

    def test_account_inherits_top_level_keys(self):
        """Иначе при переезде на accounts молча теряются общие api_id и proxy."""
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, MULTI):
                auth = mod.load_auth("cv")
                self.assertEqual(auth["api_id"], 1)
                self.assertEqual(auth["api_hash"], "h")
                self.assertEqual(auth["proxy"], "socks5://127.0.0.1:7890")

    def test_account_overrides_inherited_keys(self):
        payload = {
            "api_id": 1, "api_hash": "h",
            "accounts": {"cv": {"api_id": 2, "proxy": "socks5://10.0.0.1:1080"}},
        }
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, payload):
                auth = mod.load_auth("cv")
                self.assertEqual(auth["api_id"], 2)
                self.assertEqual(auth["proxy"], "socks5://10.0.0.1:1080")

    def test_unknown_account_exits(self):
        for name, mod in BOTH:
            with self.subTest(mod=name):
                expect_exit(self, mod, MULTI, "нет-такого")

    def test_missing_credentials_exit(self):
        payload = {"accounts": {"cv": {"session_name": "cv"}}}
        for name, mod in BOTH:
            with self.subTest(mod=name):
                expect_exit(self, mod, payload, "cv")

    def test_accounts_section_does_not_leak_into_auth(self):
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, MULTI):
                self.assertNotIn("accounts", mod.load_auth("cv"))


class ChatEntry(unittest.TestCase):
    def test_short_form_gets_default_account(self):
        for name, mod in BOTH:
            with self.subTest(mod=name):
                entry = mod.chat_entry(12345)
                self.assertEqual(entry["id"], 12345)
                self.assertEqual(entry["account"], "default")
                self.assertIsNone(entry["topic_id"])

    def test_dict_form_with_account(self):
        for name, mod in BOTH:
            with self.subTest(mod=name):
                entry = mod.chat_entry({"id": 7, "account": "cv"})
                self.assertEqual(entry["account"], "cv")

    def test_dict_form_without_account_defaults(self):
        for name, mod in BOTH:
            with self.subTest(mod=name):
                self.assertEqual(mod.chat_entry({"id": 7})["account"], "default")

    def test_topic_id_still_parsed(self):
        for name, mod in BOTH:
            with self.subTest(mod=name):
                self.assertEqual(mod.chat_entry({"id": 7, "topic_id": 42})["topic_id"], 42)

    def test_missing_id_raises(self):
        for name, mod in BOTH:
            with self.subTest(mod=name):
                with self.assertRaises(ValueError):
                    mod.chat_entry({"topic_id": 1})


class DeltasCompat(unittest.TestCase):
    """telegram-deltas.py намеренно НЕ поддерживает аккаунты (работает с готовым
    JSON, сессия ему не нужна). Но он читает тот же .telegram-snapshot.json,
    поэтому обязан молча пережить незнакомый ключ account."""

    def test_deltas_ignores_account_key(self):
        entry = DELTAS.chat_entry({"id": 7, "topic_id": 42, "account": "cv"})
        self.assertEqual(entry, {"id": 7, "topic_id": 42})

    def test_deltas_short_form_unchanged(self):
        self.assertEqual(DELTAS.chat_entry(7), {"id": 7, "topic_id": None})


if __name__ == "__main__":
    unittest.main(verbosity=2)
