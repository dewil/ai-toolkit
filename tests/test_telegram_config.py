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

import ast
import asyncio
import contextlib
import importlib.util
import io
import json
import sqlite3
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
SENDONE = load_module("telegram-send-one.py", "tg_send_one")

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

    def test_top_level_session_name_not_inherited(self):
        """Блокер: конфиг, где session_name задан до появления accounts, иначе
        сажает ВСЕ аккаунты на один .session - то есть на одну авторизованную
        сессию, и изоляции аккаунтов нет вовсе."""
        payload = {
            "api_id": 1, "api_hash": "h", "session_name": "legacy",
            "accounts": {"default": {}, "cv": {}},
        }
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, payload):
                self.assertEqual(mod.load_auth("default")["session_name"], "default")
                self.assertEqual(mod.load_auth("cv")["session_name"], "cv")

    def test_account_may_set_own_session_name(self):
        payload = {"api_id": 1, "api_hash": "h", "accounts": {"cv": {"session_name": "mine"}}}
        for name, mod in BOTH:
            with self.subTest(mod=name), auth_file(mod, payload):
                self.assertEqual(mod.load_auth("cv")["session_name"], "mine")

    def test_colliding_session_names_rejected(self):
        payload = {
            "api_id": 1, "api_hash": "h",
            "accounts": {"a": {"session_name": "same"}, "b": {"session_name": "same"}},
        }
        for name, mod in BOTH:
            with self.subTest(mod=name):
                expect_exit(self, mod, payload, "a")

    def test_non_dict_accounts_rejected(self):
        payload = {"api_id": 1, "api_hash": "h", "accounts": ["metadata"]}
        for name, mod in BOTH:
            with self.subTest(mod=name):
                expect_exit(self, mod, payload, "default")

    def test_non_dict_account_config_rejected(self):
        payload = {"api_id": 1, "api_hash": "h", "accounts": {"cv": "oops"}}
        for name, mod in BOTH:
            with self.subTest(mod=name):
                expect_exit(self, mod, payload, "cv")

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

    def test_topic_id_coerced_to_int(self):
        """Копии расходились: одна отдавала "42", другая 42. topic_id сравнивается
        с числовым полем сообщения, поэтому строка молча не совпала бы ни с чем."""
        for name, mod in BOTH:
            with self.subTest(mod=name):
                self.assertEqual(mod.chat_entry({"id": 7, "topic_id": "42"})["topic_id"], 42)

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
        self.assertEqual(entry, {"id": 7, "topic_id": 42, "dest": None})

    def test_deltas_short_form_unchanged(self):
        self.assertEqual(DELTAS.chat_entry(7), {"id": 7, "topic_id": None, "dest": None})

    def test_deltas_topic_id_coerced_to_int(self):
        self.assertEqual(DELTAS.chat_entry({"id": 7, "topic_id": "42"})["topic_id"], 42)


class PerChatDest(unittest.TestCase):
    """Ключ dest: зеркало чата подпроекта живет в его папке, не в chats_root."""

    def test_snapshot_and_deltas_parse_dest(self):
        for name, mod in (("snapshot", SNAPSHOT), ("deltas", DELTAS)):
            with self.subTest(mod=name):
                self.assertEqual(
                    mod.chat_entry({"id": 7, "dest": "проект/чаты/X"})["dest"],
                    "проект/чаты/X")
                self.assertIsNone(mod.chat_entry(7)["dest"])
                self.assertIsNone(mod.chat_entry({"id": 7})["dest"])

    def test_send_chat_entry_survives_dest_key(self):
        entry = SEND.chat_entry({"id": 7, "dest": "проект/чаты/X"})
        self.assertEqual(entry["id"], 7)

    def test_resolve_dest_stays_inside_project(self):
        for name, mod in (("snapshot", SNAPSHOT), ("deltas", DELTAS)):
            with self.subTest(mod=name):
                got = mod.resolve_dest("подпроект/чаты/Лариса")
                self.assertEqual(got, (mod.PROJECT_ROOT / "подпроект/чаты/Лариса").resolve())

    def test_resolve_dest_rejects_absolute(self):
        for name, mod in (("snapshot", SNAPSHOT), ("deltas", DELTAS)):
            with self.subTest(mod=name):
                with self.assertRaises(SystemExit):
                    mod.resolve_dest("/tmp/чужое")

    def test_resolve_dest_rejects_escape(self):
        for name, mod in (("snapshot", SNAPSHOT), ("deltas", DELTAS)):
            with self.subTest(mod=name):
                with self.assertRaises(SystemExit):
                    mod.resolve_dest("../соседний-проект/чаты")

    def test_process_chat_accepts_dest_dir(self):
        import inspect
        self.assertIn("dest_dir", inspect.signature(SNAPSHOT.process_chat).parameters)

    def test_duplicate_dest_targets_rejected(self):
        chats = {
            "A": {"id": 1, "dest": "подпроект/чаты/X"},
            "B": {"id": 2, "dest": "подпроект/чаты/X"},
        }
        with self.assertRaises(SystemExit) as cm:
            SNAPSHOT.check_unique_targets(chats, SNAPSHOT.PROJECT_ROOT / "чаты")
        self.assertIn("одну папку", str(cm.exception))

    def test_dest_over_foreign_label_rejected(self):
        chats = {
            "B": {"id": 2, "dest": None},
            "A": {"id": 1, "dest": "чаты/B"},
        }
        with self.assertRaises(SystemExit):
            SNAPSHOT.check_unique_targets(chats, SNAPSHOT.PROJECT_ROOT / "чаты")

    def test_case_insensitive_duplicate_rejected(self):
        """APFS сложил бы Client/X и Client/x в одну физическую папку."""
        chats = {
            "A": {"id": 1, "dest": "Клиент/чаты/X"},
            "B": {"id": 2, "dest": "Клиент/чаты/x"},
        }
        with self.assertRaises(SystemExit):
            SNAPSHOT.check_unique_targets(chats, SNAPSHOT.PROJECT_ROOT / "чаты")

    def test_deltas_checks_unique_targets_too(self):
        """deltas запускается независимо от снапшота - дубли ловит сам."""
        chats = {
            "A": {"id": 1, "dest": "подпроект/чаты/X"},
            "B": {"id": 2, "dest": "подпроект/чаты/X"},
        }
        with self.assertRaises(SystemExit):
            DELTAS.check_unique_targets(chats, DELTAS.PROJECT_ROOT / "чаты")

    def test_distinct_targets_pass(self):
        chats = {
            "A": {"id": 1, "dest": "подпроект/чаты/A"},
            "B": {"id": 2, "dest": None},
        }
        SNAPSHOT.check_unique_targets(chats, SNAPSHOT.PROJECT_ROOT / "чаты")

    def test_foreign_mirror_id_rejected(self):
        with self.assertRaises(RuntimeError) as cm:
            SNAPSHOT.ensure_result_belongs_to(
                {"id": 111, "messages": []}, 222, Path("x/result.json"))
        self.assertIn("другого чата", str(cm.exception))
        SNAPSHOT.ensure_result_belongs_to({"id": 222}, 222, Path("x"))
        SNAPSHOT.ensure_result_belongs_to({"messages": []}, 222, Path("x"))

    def test_deltas_emit_chat_reads_from_dest_dir(self):
        """Мутация 'dest_dir игнорируется' обязана уронить этот тест: в
        chats_root/label лежит пустое зеркало, новое сообщение - только в dest."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chats_root = root / "чаты"
            (chats_root / "X").mkdir(parents=True)
            (chats_root / "X" / "result.json").write_text(
                '{"messages": []}', encoding="utf-8")
            dest = root / "подпроект" / "чаты" / "X"
            dest.mkdir(parents=True)
            (dest / "result.json").write_text(json.dumps({"messages": [
                {"id": 1, "type": "message", "date": "2026-07-01T10:00:00", "text": "старое"},
                {"id": 2, "type": "message", "date": "2026-07-02T10:00:00", "text": "новое из dest"},
            ]}), encoding="utf-8")
            (dest / "result.prev.json").write_text(json.dumps({"messages": [
                {"id": 1, "type": "message", "date": "2026-07-01T10:00:00", "text": "старое"},
            ]}), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                n = DELTAS.emit_chat("X", "X", chats_root, 24, None, dest)
        self.assertEqual(n, 1)
        self.assertIn("новое из dest", out.getvalue())


class MediaFlag(unittest.TestCase):
    """Ключ media: чат без вложений (мем-флудилка роняла прогон скачиванием)."""

    def test_default_true_both_forms(self):
        self.assertTrue(SNAPSHOT.chat_entry(7)["media"])
        self.assertTrue(SNAPSHOT.chat_entry({"id": 7})["media"])

    def test_false_parsed(self):
        self.assertFalse(SNAPSHOT.chat_entry({"id": 7, "media": False})["media"])

    def test_non_bool_media_rejected(self):
        """"false" строкой из JSON через truthiness давал True - защита от
        потока вложений молча не работала бы. Только настоящий bool."""
        for bad in ("false", "true", 0, 1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    SNAPSHOT.chat_entry({"id": 7, "media": bad})

    def test_deltas_ignores_media_key(self):
        """deltas работает с готовым JSON - выкачки нет, ключ просто переживает."""
        self.assertEqual(
            DELTAS.chat_entry({"id": 7, "media": False}),
            {"id": 7, "topic_id": None, "dest": None})

    def test_send_survives_media_key(self):
        self.assertEqual(SEND.chat_entry({"id": 7, "media": False})["id"], 7)

    def test_fetch_new_plumbing(self):
        """download_media=False - download_message_media не вызывается вовсе;
        с дефолтом True - вызывается. Мутация 'флаг игнорируется' роняет тест."""
        calls: list[int] = []

        async def fake_download(client, msg, chat_media_dir):
            calls.append(msg.id)
            return "f.jpg"

        async def fake_record(client, msg):
            return {"id": msg.id}

        def make_client():
            msg = types.SimpleNamespace(id=5, action=None, reply_to=None)

            class Client:
                def iter_messages(self, entity, min_id=0, reverse=True):
                    async def gen():
                        yield msg
                    return gen()

            return Client()

        entity = types.SimpleNamespace(id=1)
        orig_dl = SNAPSHOT.download_message_media
        orig_rec = SNAPSHOT.message_to_record
        SNAPSHOT.download_message_media = fake_download
        SNAPSHOT.message_to_record = fake_record
        try:
            out, _, _, downloaded = asyncio.run(
                SNAPSHOT.fetch_new(make_client(), entity, 0, download_media=False))
            self.assertEqual([m["id"] for m in out], [5])
            self.assertEqual(calls, [])
            self.assertEqual(downloaded, [])
            _, _, _, downloaded = asyncio.run(
                SNAPSHOT.fetch_new(make_client(), entity, 0))
            self.assertEqual(calls, [5])
            self.assertEqual(downloaded, ["f.jpg"])
        finally:
            SNAPSHOT.download_message_media = orig_dl
            SNAPSHOT.message_to_record = orig_rec


class PartialFailure(unittest.TestCase):
    """Отказ одного чата не роняет прогон (в т.ч. CancelledError от разрыва
    соединения - он BaseException и мимо except Exception), но виден в коде
    возврата. Внешняя отмена (Ctrl+C) при этом не глушится."""

    @contextlib.contextmanager
    def _patched_amain(self, effects: dict):
        """Подменяет тяжелые зависимости amain; effects: label -> число новых
        сообщений или исключение, которое кидает process_chat этого чата."""
        cfg = {
            "chats": {label: SNAPSHOT.chat_entry(i + 1)
                      for i, label in enumerate(effects)},
            "chats_root": "чаты",
        }
        calls: list[str] = []

        async def fake_process_chat(client, chats_root, label, chat_id,
                                    dialog_entities, dest_dir=None,
                                    download_media=True):
            calls.append(label)
            eff = effects[label]
            if isinstance(eff, BaseException):
                raise eff
            return eff, "2026-08-01T00:00:00"

        class Client:
            def __init__(self, *a, **k):
                pass

            def is_connected(self):
                return True

            def iter_dialogs(self):
                async def gen():
                    return
                    yield
                return gen()

        async def fake_connect(client, interactive=True):
            pass

        async def fake_disconnect(client):
            pass

        patches = {
            "load_project_config": lambda: cfg,
            "load_auth": lambda account="default": {
                "session_name": "s", "api_id": 1, "api_hash": "h"},
            "client_kwargs": lambda auth: {},
            "TelegramClient": Client,
            "connect_with_retry": fake_connect,
            "disconnect_quietly": fake_disconnect,
            "process_chat": fake_process_chat,
        }
        originals = {k: getattr(SNAPSHOT, k) for k in patches}
        for k, v in patches.items():
            setattr(SNAPSHOT, k, v)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                yield calls, out, err
        finally:
            for k, v in originals.items():
                setattr(SNAPSHOT, k, v)

    def test_all_ok_returns_zero(self):
        with self._patched_amain({"A": 2, "B": 3}) as (calls, out, _):
            code = asyncio.run(SNAPSHOT.amain())
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["A", "B"])
        self.assertIn("OK: +5", out.getvalue())

    def test_media_flag_reaches_process_chat(self):
        """media: false из записи чата доезжает до process_chat как
        download_media=False; у остальных остается True."""
        seen: dict[str, bool] = {}

        async def recording_process_chat(client, chats_root, label, chat_id,
                                         dialog_entities, dest_dir=None,
                                         download_media=True):
            seen[label] = download_media
            return 0, "d"

        with self._patched_amain({"мемы": 0, "рабочий": 0}):
            SNAPSHOT.load_project_config = lambda: {
                "chats": {
                    "мемы": SNAPSHOT.chat_entry({"id": 1, "media": False}),
                    "рабочий": SNAPSHOT.chat_entry(2),
                },
                "chats_root": "чаты",
            }
            SNAPSHOT.process_chat = recording_process_chat
            code = asyncio.run(SNAPSHOT.amain())
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"мемы": False, "рабочий": True})

    def test_exception_isolated_and_exit_nonzero(self):
        with self._patched_amain({"A": RuntimeError("boom"), "B": 3}) as (calls, out, err):
            code = asyncio.run(SNAPSHOT.amain())
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["A", "B"])
        self.assertIn("!! A", err.getvalue())
        self.assertIn("ЧАСТИЧНО", out.getvalue())
        self.assertNotIn("OK:", out.getvalue())

    def test_cancelled_error_isolated(self):
        """Инцидент 2026-08-01: CancelledError из download_media ронял ВЕСЬ
        прогон - следующие чаты не обновлялись."""
        with self._patched_amain(
                {"мемы": asyncio.CancelledError(), "рабочий": 4}) as (calls, _, err):
            code = asyncio.run(SNAPSHOT.amain())
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["мемы", "рабочий"])
        self.assertIn("!! мемы", err.getvalue())

    def test_external_cancel_reraised(self):
        """Ctrl+C (отмена самой таски) не глушится изоляцией по чатам."""
        async def cancelling_effect_runner():
            task = asyncio.ensure_future(SNAPSHOT.amain())
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(task.cancelled())

        effects = {"A": asyncio.CancelledError(), "B": 1}

        # process_chat чата A отменяет свою же таску перед CancelledError -
        # как это делает Runner на SIGINT
        async def fake_process_chat(client, chats_root, label, chat_id,
                                    dialog_entities, dest_dir=None,
                                    download_media=True):
            calls.append(label)
            eff = effects[label]
            if isinstance(eff, BaseException):
                asyncio.current_task().cancel()
                raise eff
            return eff, "d"

        calls: list[str] = []
        with self._patched_amain(effects) as (_, __, ___):
            SNAPSHOT.process_chat = fake_process_chat
            asyncio.run(cancelling_effect_runner())
        self.assertEqual(calls, ["A"])  # B не обрабатывался - отмена прошла наверх

    def test_dead_client_fails_fast_not_cascade(self):
        """CancelledError от мертвого соединения: реконнект не удался - остаток
        чатов аккаунта помечается провалом сразу, а не сыплется каскадом.
        Клиент при этом УТВЕРЖДАЕТ is_connected()=True (телетоновский
        _user_connected живости транспорта не отражает) - реконнект обязан
        идти безусловно, не доверяя этому флагу."""
        effects = {"A": asyncio.CancelledError(), "B": 1, "C": 2}
        with self._patched_amain(effects) as (calls, out, err):
            async def connect(client, *, interactive=False, **kw):
                if not interactive:
                    raise ConnectionError("нет сети")
            SNAPSHOT.connect_with_retry = connect
            code = asyncio.run(SNAPSHOT.amain())
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["A"])
        self.assertIn("реконнект не удался", err.getvalue())
        # пропущенные чаты входят в итоговый счетчик провалов, не только в stderr
        self.assertIn("провалено чатов: 3 (A, B, C)", out.getvalue())

    def test_reconnect_cancelled_isolated_too(self):
        """Отмена самого реконнекта (коррелированный отказ нестабильной сессии) -
        тоже провал остатка аккаунта с ЧАСТИЧНО, а не тихий выход из amain."""
        effects = {"A": asyncio.CancelledError(), "B": 1}
        with self._patched_amain(effects) as (calls, out, err):
            async def connect(client, *, interactive=False, **kw):
                if not interactive:
                    raise asyncio.CancelledError()
            SNAPSHOT.connect_with_retry = connect
            code = asyncio.run(SNAPSHOT.amain())
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["A"])
        self.assertIn("ЧАСТИЧНО", out.getvalue())

    def test_dead_client_reconnects_and_continues(self):
        effects = {"A": asyncio.CancelledError(), "B": 3}
        reconnects = []
        with self._patched_amain(effects) as (calls, _, __):
            async def connect(client, *, interactive=False, **kw):
                if not interactive:
                    reconnects.append(1)
            SNAPSHOT.connect_with_retry = connect
            code = asyncio.run(SNAPSHOT.amain())
        self.assertEqual(code, 1)  # чат A все равно провален
        self.assertEqual(calls, ["A", "B"])
        self.assertEqual(reconnects, [1])


class MigrationFailure(unittest.TestCase):
    """Провал legacy-миграции - провал чата, а не молчаливый успех: раньше
    process_chat возвращал (0, "") и прогон печатал OK:/код 0 при пропущенном
    чате (находка adversarial-ревью)."""

    def test_migration_failure_raises_and_keeps_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat_dir = root / "X"
            chat_dir.mkdir()
            payload = {"id": 1, "messages": [
                {"id": 5, "type": "message", "date": "2026-08-01T00:00:00"}]}
            (chat_dir / "result.json").write_text(
                json.dumps(payload), encoding="utf-8")

            def boom(data):
                raise ValueError("boom")

            orig_cleanup = SNAPSHOT.cleanup_old_media
            orig_migrate = SNAPSHOT.migrate_legacy
            SNAPSHOT.cleanup_old_media = lambda: 0
            SNAPSHOT.migrate_legacy = boom
            try:
                entity = types.SimpleNamespace(id=1, title="X")
                with self.assertRaises(RuntimeError) as cm:
                    asyncio.run(SNAPSHOT.process_chat(
                        None, root, "X", 1, {1: entity}))
                self.assertIn("миграция", str(cm.exception))
                got = json.loads(
                    (chat_dir / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(got, payload)
            finally:
                SNAPSHOT.cleanup_old_media = orig_cleanup
                SNAPSHOT.migrate_legacy = orig_migrate


class PullOneNoMedia(unittest.TestCase):
    """--no-media у telegram-pull-one: у него нет конфига с записями чатов,
    выключатель вложений - только флагом."""

    def test_amain_accepts_download_media(self):
        import inspect
        pull_one = load_module("telegram-pull-one.py", "tg_pull_one")
        self.assertIn("download_media", inspect.signature(pull_one.amain).parameters)
        src = (SCRIPTS / "telegram-pull-one.py").read_text(encoding="utf-8")
        self.assertIn("--no-media", src)
        self.assertIn("not args.no_media", src)


class SilentFlag(unittest.TestCase):
    """--silent: беззвучная отправка (rules/outbound-timing.md - средний путь
    между "отправить" и "отложить"). Проверяем три вещи: флаг объявлен в обоих
    скриптах, доезжает до send_message И send_file (вложение будит так же, как
    текст), и виден в dry-run - гейт показывает пользователю, что уйдет."""

    def test_flag_declared_in_both(self):
        for name in ("telegram-send.py", "telegram-send-one.py"):
            with self.subTest(script=name):
                src = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn('"--silent"', src)

    def test_reaches_both_send_calls(self):
        """Мутация 'silent не пробросили' обязана уронить тест - иначе флаг
        молча ничего не делает, а пользователь уверен, что отправил тихо."""
        for name in ("telegram-send.py", "telegram-send-one.py"):
            with self.subTest(script=name):
                src = (SCRIPTS / name).read_text(encoding="utf-8")
                tree = ast.parse(src)
                calls = {}
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    if isinstance(fn, ast.Attribute) and fn.attr in ("send_message", "send_file"):
                        kw = {k.arg for k in node.keywords}
                        calls.setdefault(fn.attr, []).append(kw)
                self.assertEqual(set(calls), {"send_message", "send_file"},
                                 f"{name}: ожидались оба вызова отправки")
                for meth, variants in calls.items():
                    for kw in variants:
                        self.assertIn("silent", kw, f"{name}: {meth} без silent")

    def test_dry_run_shows_sound(self):
        for name in ("telegram-send.py", "telegram-send-one.py"):
            with self.subTest(script=name):
                src = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("звук:", src)
                self.assertIn("args.silent", src)


class SendOneFile(unittest.TestCase):
    """--file в telegram-send-one: гейты отрабатывают до захвата общей сессии."""

    def _run(self, **kw):
        args = types.SimpleNamespace(
            chat_id="123", username=None, text="привет", send=False,
            topic=None, reply_to=None, account="default", html=False, file=None,
            silent=False)
        for k, v in kw.items():
            setattr(args, k, v)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = asyncio.run(SENDONE.amain(args))
        return code, err.getvalue()

    def test_missing_file_exits_before_session(self):
        code, err = self._run(file="/нет/такого/файла.pdf")
        self.assertEqual(code, 2)
        self.assertIn("Файл не найден", err)

    def test_caption_over_1024_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "док.pdf"
            f.write_bytes(b"%PDF-1.4")
            code, err = self._run(file=str(f), text="х" * 1025)
        self.assertEqual(code, 2)
        self.assertIn("1024", err)

    def test_empty_file_flag_rejected_not_degraded_to_text(self):
        """--file "$VAR" с пустой переменной - заданный файловый режим, а не
        его отсутствие: молчаливая отправка одним текстом - вранье."""
        code, err = self._run(file="")
        self.assertEqual(code, 2)
        self.assertIn("пустой строкой", err)

    def test_file_check_precedes_auth(self):
        """Проверка файла стоит в исходнике ДО load_auth - падаем раньше,
        чем трогаем сессию."""
        src = (SCRIPTS / "telegram-send-one.py").read_text(encoding="utf-8")
        self.assertLess(src.index("Файл не найден"), src.index("load_auth"))


class DisconnectSwallowsCancel(unittest.TestCase):
    """CancelledError в cleanup: без перехвата отмена футур telethon в finally
    глушила бы итог прогона (ЧАСТИЧНО/OK). Обе копии хелпера."""

    def test_cancelled_error_swallowed_with_warning(self):
        for name, mod in BOTH:
            with self.subTest(mod=name):
                client = FakeClient(0, disconnect_error=asyncio.CancelledError())
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    asyncio.run(mod.disconnect_quietly(client))
                self.assertIn("CancelledError", err.getvalue())


class AtomicWrite(unittest.TestCase):
    """Имя временного файла было фиксированным (`<файл>.tmp`), поэтому два
    одновременных прогона писали в один и тот же .tmp и подменяли друг другу
    недописанный файл. Теперь имя содержит pid, а мусор чистится при падении."""

    def test_writes_json_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            SNAPSHOT.atomic_write_json(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
            self.assertEqual([f.name for f in Path(tmp).iterdir()], ["result.json"])

    def test_does_not_clobber_foreign_temp(self):
        """Регресс: на старой схеме имен этот файл был бы перезаписан и исчез."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            foreign = path.with_name("result.json.tmp")
            foreign.write_text("чужой недописанный", encoding="utf-8")
            SNAPSHOT.atomic_write_json(path, {"a": 1})
            self.assertTrue(foreign.exists(), "чужой .tmp не должен быть тронут")
            self.assertEqual(foreign.read_text(encoding="utf-8"), "чужой недописанный")

    def test_failed_write_cleans_up_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text('{"old": true}', encoding="utf-8")
            with self.assertRaises(TypeError):
                SNAPSHOT.atomic_write_json(path, {"bad": object()})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual([f.name for f in Path(tmp).iterdir()], ["result.json"])


LOCKED = "database is locked"


class FakeClient:
    """Клиент, у которого connect/start падают первые `fail` раз."""

    def __init__(
        self,
        fail: int,
        error: BaseException | None = None,
        disconnect_error: BaseException | None = None,
    ):
        self.fail = fail
        self.error = error or sqlite3.OperationalError(LOCKED)
        # cleanup у telethon сам пишет в сессию, то есть на живом локе падает
        self.disconnect_error = disconnect_error
        self.calls: list[str] = []

    async def _attempt(self, kind: str):
        self.calls.append(kind)
        if self.fail > 0:
            self.fail -= 1
            raise self.error
        return f"{kind}-ok"

    async def connect(self):
        return await self._attempt("connect")

    async def start(self):
        return await self._attempt("start")

    async def disconnect(self):
        self.calls.append("disconnect")
        if self.disconnect_error is not None:
            raise self.disconnect_error


@contextlib.contextmanager
def capture_sleeps():
    """Подменяет asyncio.sleep, чтобы тест не ждал и видел длину паузы."""
    calls: list[float] = []
    original = asyncio.sleep

    async def fake(seconds):
        calls.append(seconds)

    asyncio.sleep = fake
    try:
        yield calls
    finally:
        asyncio.sleep = original


class SessionRetry(unittest.TestCase):
    """connect_with_retry: общую .session держит другой процесс - ждем, не чиним.

    Гоняется по обеим копиям хелпера (snapshot и send) - см. шапку файла.
    """

    def test_retries_until_session_free(self):
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(fail=2)
                with capture_sleeps() as sleeps:
                    with contextlib.redirect_stderr(io.StringIO()):
                        result = asyncio.run(mod.connect_with_retry(client, delay=7))
                self.assertEqual(result, "connect-ok")
                self.assertEqual(client.calls.count("connect"), 3)
                self.assertEqual(sleeps, [7, 7], "между попытками должна быть пауза")

    def test_disconnects_between_attempts(self):
        """Соединение могло подняться до падения сессии - иначе утечет сокет."""
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(fail=1)
                with capture_sleeps():
                    with contextlib.redirect_stderr(io.StringIO()):
                        asyncio.run(mod.connect_with_retry(client, delay=0))
                self.assertEqual(client.calls, ["connect", "disconnect", "connect"])

    def test_exhausted_attempts_raise_original_error(self):
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(fail=99)
                with capture_sleeps() as sleeps:
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            asyncio.run(mod.connect_with_retry(client, attempts=3, delay=0))
                self.assertIn(LOCKED, str(caught.exception))
                self.assertEqual(client.calls.count("connect"), 3)
                self.assertEqual(len(sleeps), 2, "после последней попытки не ждем")

    def test_other_sqlite_error_is_not_retried(self):
        """Ретрай только на блокировку: "no such table" повтором не лечится."""
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(
                    fail=99, error=sqlite3.OperationalError("no such table: sessions")
                )
                with capture_sleeps() as sleeps:
                    with self.assertRaises(sqlite3.OperationalError):
                        asyncio.run(mod.connect_with_retry(client, delay=0))
                self.assertEqual(client.calls, ["connect"])
                self.assertEqual(sleeps, [])

    def test_cleanup_failure_does_not_abort_retry(self):
        """Блокер: disconnect на живом локе падает тем же locked (telethon пишет
        состояние в ту же sqlite) - и раньше обрывал ретрай на первой попытке."""
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(
                    fail=2, disconnect_error=sqlite3.OperationalError(LOCKED)
                )
                with capture_sleeps() as sleeps:
                    with contextlib.redirect_stderr(io.StringIO()):
                        result = asyncio.run(mod.connect_with_retry(client, delay=0))
                self.assertEqual(result, "connect-ok")
                self.assertEqual(client.calls.count("connect"), 3)
                self.assertEqual(len(sleeps), 2)

    def test_cleanup_failure_does_not_mask_original_error(self):
        """Наверх идет locked, а не то, чем упал cleanup."""
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(fail=99, disconnect_error=RuntimeError("cleanup"))
                with capture_sleeps():
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            asyncio.run(mod.connect_with_retry(client, attempts=2, delay=0))
                self.assertIn(LOCKED, str(caught.exception))

    def test_interactive_uses_start(self):
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(fail=0)
                result = asyncio.run(mod.connect_with_retry(client, interactive=True))
                self.assertEqual(result, "start-ok")
                self.assertEqual(client.calls, ["start"])

    def test_zero_attempts_still_connects_once(self):
        """Не отдать молча None вместо подключения: одна попытка - минимум."""
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(fail=0)
                result = asyncio.run(mod.connect_with_retry(client, attempts=0))
                self.assertEqual(result, "connect-ok")
                self.assertEqual(client.calls, ["connect"])


class DisconnectQuietly(unittest.TestCase):
    """Cleanup не должен рвать вызывающего своей ошибкой, но и молчать не должен."""

    def test_swallows_error_and_reports_it(self):
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(
                    fail=0, disconnect_error=sqlite3.OperationalError(LOCKED)
                )
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    asyncio.run(mod.disconnect_quietly(client))
                self.assertEqual(client.calls, ["disconnect"])
                self.assertIn(LOCKED, err.getvalue())

    def test_silent_on_success(self):
        for label, mod in BOTH:
            with self.subTest(label):
                client = FakeClient(fail=0)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    asyncio.run(mod.disconnect_quietly(client))
                self.assertEqual(err.getvalue(), "")


class RetryScopedToConnect(unittest.TestCase):
    """Ретрай стоит строго на подключении: повтор отправки дал бы дубль.

    Сторожа разбирают AST, а не текст: проверка по подстрокам обходилась лишними
    скобками, другим отступом, переименованной переменной и переносом строки.
    """

    CLIENT_SCRIPTS = (
        "telegram-snapshot.py",
        "telegram-pull-one.py",
        "telegram-send.py",
        "telegram-send-one.py",
    )
    SEND_SCRIPTS = ("telegram-send.py", "telegram-send-one.py")
    CONNECT_METHODS = {"connect", "start"}
    # неидемпотентные вызовы: повтор после успеха дает получателю дубль
    SEND_METHODS = {"send_message", "send_file"}

    @staticmethod
    def _tree(name: str) -> ast.Module:
        return ast.parse((SCRIPTS / name).read_text(encoding="utf-8"), filename=name)

    @staticmethod
    def _method_calls(node: ast.AST, methods: set[str]) -> list[ast.Call]:
        return [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in methods
        ]

    @staticmethod
    def _functions(tree: ast.Module):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield node

    @classmethod
    def _helper_calls(cls, tree: ast.Module, helper: str) -> list[ast.Call]:
        """Точки ПРИМЕНЕНИЯ хелпера: локальные и через модуль (tgs.<helper>).

        Определение хелпера сюда не попадает - это FunctionDef, а не Call.
        """
        return cls._method_calls(tree, {helper}) + [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == helper
        ]

    def _assert_routed_through(self, helper: str, methods: set[str]) -> None:
        for name in self.CLIENT_SCRIPTS:
            tree = self._tree(name)
            for func in self._functions(tree):
                for call in self._method_calls(func, methods):
                    self.assertEqual(
                        func.name,
                        helper,
                        f"{name}:{call.lineno} - {ast.unparse(call)} мимо {helper}",
                    )
            # без этой сверки тест зеленел бы и на скрипте, где вызовы исчезли:
            # запрет "только внутри хелпера" сам по себе выполняется и пустым файлом
            self.assertGreater(
                len(self._helper_calls(tree, helper)),
                0,
                f"{name}: ни одного вызова {helper}",
            )

    def test_connect_only_inside_helper(self):
        """Новая точка подключения мимо хелпера снова начнет падать на локе."""
        self._assert_routed_through("connect_with_retry", self.CONNECT_METHODS)

    def test_disconnect_only_inside_quiet_helper(self):
        """Блокер: cleanup на живом локе падает тем же locked - в finally он
        подменял исходное исключение, а после отправки давал ненулевой exit."""
        self._assert_routed_through("disconnect_quietly", {"disconnect"})

    def test_helper_wraps_nothing_but_connect(self):
        """Отправка не должна попасть внутрь вызова connect_with_retry."""
        for name in self.CLIENT_SCRIPTS:
            tree = self._tree(name)
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                target = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if target != "connect_with_retry":
                    continue
                for arg in list(call.args) + [kw.value for kw in call.keywords]:
                    inner = self._method_calls(arg, self.SEND_METHODS)
                    self.assertEqual(
                        inner, [], f"{name}:{call.lineno} - отправка под ретраем"
                    )

    def test_send_calls_are_outside_retry(self):
        """Сами вызовы отправки существуют и стоят вне хелпера."""
        for name in self.SEND_SCRIPTS:
            tree = self._tree(name)
            found = 0
            for func in self._functions(tree):
                for call in self._method_calls(func, self.SEND_METHODS):
                    found += 1
                    self.assertNotEqual(
                        func.name, "connect_with_retry", f"{name}:{call.lineno}"
                    )
            self.assertGreater(found, 0, f"{name}: вызовов отправки не найдено")


if __name__ == "__main__":
    unittest.main(verbosity=2)
