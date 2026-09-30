#!/usr/bin/env python3
"""Тесты входа telegram-send.py, telegram-send-one.py и telegram-pull-one.py:
пустой/несуществующий --file и сверка ожидаемого username у безымянного чата.
stdlib-only (unittest), без сети.

Запуск: python3 tests/test_telegram_inputs.py

Критерии: INV-MSG-03 (пустой --file, username безымянного чата).
Требование: INV-MSG-03 (отказ до сети, код 2; один код несовпадения username
в send-one и pull-one).

Тесты написаны вслепую по спеке и публичному контракту (--help, сигнатуры
amain, обвязка test_telegram_schedule.py): сообщения об ошибках проверяются
только на непустоту и упоминание файла, точные формулировки спека не задает.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

def _stub_telethon() -> None:
    """telethon в тестах не нужен: есть настоящий - берем его, нет - заглушка
    с модулями-пустышками (telegram-snapshot.py при неудачном импорте делает
    sys.exit(2))."""
    try:
        import telethon  # noqa: F401
        return
    except ImportError:
        pass

    def make(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr: type(attr, (), {})  # type: ignore[method-assign]
        return mod

    telethon = make("telethon")
    tl = make("telethon.tl")
    tl_types = make("telethon.tl.types")
    utils = make("telethon.utils")
    utils.get_attributes = lambda *a, **k: ([], None)
    telethon.tl, telethon.utils = tl, utils
    tl.types = tl_types
    sys.modules.update({"telethon": telethon, "telethon.tl": tl,
                        "telethon.tl.types": tl_types, "telethon.utils": utils})


_stub_telethon()


def _load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tgs = _load("telegram-send.py", "tginp_send")
tgs_one = _load("telegram-send-one.py", "tginp_send_one")
tgp = _load("telegram-pull-one.py", "tginp_pull_one")


async def _noop_connect(client, **kw):
    return None


async def _noop_disconnect(client):
    return None


class RecordingClient:
    """Клиент-заглушка: фиксирует, что его создали, и что через него отправили.
    Чат резолвится в сущность с заданным username (None - безымянный)."""

    instances: list = []
    username = None
    has_username_attr = True

    def __init__(self, *a, **k):
        self.sent_method = None
        self.calls: list[str] = []
        type(self).instances.append(self)

    def _entity(self, ident=111):
        ent = types.SimpleNamespace(id=ident, title="Чат", first_name="Чат", last_name=None)
        if self.has_username_attr:
            ent.username = self.username
        return ent

    async def is_user_authorized(self):
        return True

    def iter_dialogs(self):
        outer = self

        async def gen():
            yield types.SimpleNamespace(entity=outer._entity())
        return gen()

    async def get_entity(self, ident):
        return self._entity(ident)

    async def get_me(self):
        return types.SimpleNamespace(id="me", title="Избранное", username=self.username)

    async def upload_file(self, path):
        return f"uploaded:{path}"

    async def send_message(self, entity, text, **kwargs):
        self.sent_method = "send_message"
        return types.SimpleNamespace(id=1, date=None, message=text)

    async def send_file(self, entity, path, **kwargs):
        self.sent_method = "send_file"
        return types.SimpleNamespace(id=1, date=None, message=kwargs.get("caption", ""))

    def iter_messages(self, *a, **k):
        self.calls.append("iter_messages")

        async def gen():
            return
            yield  # пустая лента: асинхронный генератор без сообщений
        return gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        # любой другой вызов клиента (выкачка, скачивание и т.п.) - записываем
        if name.startswith("__"):
            raise AttributeError(name)
        self.calls.append(name)
        return mock.AsyncMock(return_value=[])


def client_class(username=None, has_username_attr=True):
    return type("Client", (RecordingClient,), {
        "instances": [], "username": username, "has_username_attr": has_username_attr,
    })


@contextlib.contextmanager
def patched_env(target, client_cls, extra=()):
    """Подмена тяжелых зависимостей в модуле target (для send-one - в его tgs).
    Общую логику не трогаем - ее и проверяем."""
    patches = {
        "load_project_config": lambda: {"chats": {"чат": tgs.chat_entry(111)}},
        "load_auth": lambda account="default": {"session_name": "s", "api_id": 1, "api_hash": "h"},
        "client_kwargs": lambda auth: {},
        "TelegramClient": client_cls,
        "connect_with_retry": _noop_connect,
        "disconnect_quietly": _noop_disconnect,
    }
    originals = {k: getattr(target, k) for k in patches if hasattr(target, k)}
    for k, v in patches.items():
        if hasattr(target, k):
            setattr(target, k, v)
    tmp = tempfile.TemporaryDirectory()
    pace_orig = getattr(target, "PACE_STATE_PATH", None)
    if pace_orig is not None:
        target.PACE_STATE_PATH = Path(tmp.name) / "last-sent.json"
    try:
        yield
    finally:
        for k, v in originals.items():
            setattr(target, k, v)
        if pace_orig is not None:
            target.PACE_STATE_PATH = pace_orig
        tmp.cleanup()


def run_main(mod, argv):
    """main() модуля с заданным argv -> (код, stdout+stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mod.main()
            except SystemExit as e:  # argparse/sys.exit внутри
                code = e.code
    return code, out.getvalue() + err.getvalue()


SEND_BASE = ["telegram-send.py", "--to", "чат", "--text", "привет", "--no-pace-check"]
SEND_ONE_BASE = ["telegram-send-one.py", "111", "--text", "привет", "--no-pace-check"]


class EmptyOrMissingFileRefused(unittest.TestCase):
    """Пустой --file и путь к несуществующему файлу: отказ до сети, код 2,
    понятная строка, ничего не отправлено.

    Требование: INV-MSG-03
    """

    def _check(self, mod, target, base, file_value, extra=()):
        cls = client_class()
        with patched_env(target, cls):
            code, out = run_main(mod, [*base, "--file", file_value, *extra])
        self.assertEqual(code, 2, out)
        self.assertRegex(out.lower(), r"файл|file", "нужна понятная строка про --file")
        self.assertEqual(cls.instances, [], "до сети дойти не должно (клиент создан)")
        return cls

    def test_send_empty_file_with_send_flag(self):
        """--file "" в send при --send: код 2, клиент не создан. Требование: INV-MSG-03"""
        self._check(tgs, tgs, SEND_BASE, "", ["--send"])

    def test_send_one_empty_file_with_send_flag(self):
        """--file "" в send-one при --send: код 2, клиент не создан. Требование: INV-MSG-03"""
        self._check(tgs_one, tgs_one.tgs, SEND_ONE_BASE, "", ["--send"])

    def test_send_missing_file_with_send_flag(self):
        """Несуществующий файл в send: код 2, клиент не создан. Требование: INV-MSG-03"""
        with tempfile.TemporaryDirectory() as tmp:
            self._check(tgs, tgs, SEND_BASE, str(Path(tmp) / "нет-такого.txt"), ["--send"])

    def test_send_one_missing_file_with_send_flag(self):
        """Несуществующий файл в send-one: код 2, клиент не создан. Требование: INV-MSG-03"""
        with tempfile.TemporaryDirectory() as tmp:
            self._check(tgs_one, tgs_one.tgs, SEND_ONE_BASE, str(Path(tmp) / "нет-такого.txt"), ["--send"])

    def test_send_empty_file_dry_run_also_refused(self):
        """Без --send (dry-run) пустой --file в send тоже отказ 2: молча читать его как
        "файла нет" нельзя нигде (проза спеки, отказ не привязан к --send).
        Требование: INV-MSG-03"""
        self._check(tgs, tgs, SEND_BASE, "")

    def test_send_one_empty_file_dry_run_also_refused(self):
        """То же для send-one в dry-run. Требование: INV-MSG-03"""
        self._check(tgs_one, tgs_one.tgs, SEND_ONE_BASE, "")

    def test_send_missing_file_dry_run_also_refused(self):
        """Несуществующий файл в dry-run send: код 2. Требование: INV-MSG-03"""
        with tempfile.TemporaryDirectory() as tmp:
            self._check(tgs, tgs, SEND_BASE, str(Path(tmp) / "нет-такого.txt"))

    def test_send_one_missing_file_dry_run_also_refused(self):
        """Несуществующий файл в dry-run send-one: код 2. Требование: INV-MSG-03"""
        with tempfile.TemporaryDirectory() as tmp:
            self._check(tgs_one, tgs_one.tgs, SEND_ONE_BASE, str(Path(tmp) / "нет-такого.txt"))


class ExistingFileStillWorks(unittest.TestCase):
    """Контроль обвязки и защита от перегиба: настоящий файл по-прежнему
    отправляется, код 0.

    Требование: INV-MSG-03
    """

    def test_send_existing_file_goes_out(self):
        """Существующий файл в send уходит вложением. Требование: INV-MSG-03"""
        cls = client_class()
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "файл.txt"
            f.write_text("данные", encoding="utf-8")
            with patched_env(tgs, cls):
                code, out = run_main(tgs, [*SEND_BASE, "--file", str(f), "--send"])
        self.assertEqual(code, 0, out)
        self.assertEqual([c.sent_method for c in cls.instances], ["send_file"])

    def test_send_one_existing_file_goes_out(self):
        """Существующий файл в send-one уходит вложением. Требование: INV-MSG-03"""
        cls = client_class()
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "файл.txt"
            f.write_text("данные", encoding="utf-8")
            with patched_env(tgs_one.tgs, cls):
                code, out = run_main(tgs_one, [*SEND_ONE_BASE, "--file", str(f), "--send"])
        self.assertEqual(code, 0, out)
        self.assertEqual([c.sent_method for c in cls.instances], ["send_file"])


def send_one_args(**overrides) -> types.SimpleNamespace:
    base = dict(
        chat_id="111", username=None, text="привет", file=None, voice=False,
        send=False, no_pace_check=True, topic=None, reply_to=None,
        account="default", silent=False, html=False, schedule=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class SendOneUsernameMismatch(unittest.TestCase):
    """send-one: ожидаемый username задан, у чата его нет или он другой -
    стоп до отправки, код 2 (тот же, что у pull-one).

    Требование: INV-MSG-03
    """

    def _run(self, cls, **kw):
        out, err = io.StringIO(), io.StringIO()
        with patched_env(tgs_one.tgs, cls):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = asyncio.run(tgs_one.amain(send_one_args(**kw)))
        return code, out.getvalue() + err.getvalue()

    def test_nameless_chat_attr_none_refused_with_2(self):
        """Чат без username (username=None), ожидаемый задан: код 2, отправки нет.
        Требование: INV-MSG-03"""
        cls = client_class(username=None)
        code, out = self._run(cls, username="somebody", send=True)
        self.assertEqual(code, 2, out)
        self.assertEqual([c.sent_method for c in cls.instances], [None] * len(cls.instances))

    def test_nameless_chat_no_attr_refused_with_2(self):
        """У сущности нет самого атрибута username (базовая группа): тоже код 2, отправки нет.
        Требование: INV-MSG-03"""
        cls = client_class(has_username_attr=False)
        code, out = self._run(cls, username="somebody", send=True)
        self.assertEqual(code, 2, out)
        self.assertEqual([c.sent_method for c in cls.instances], [None] * len(cls.instances))

    def test_named_chat_other_username_refused_with_2(self):
        """Несовпадение у именного чата - тоже 2 (спека: код несовпадения один, 2;
        сейчас send-one отдает 1). Требование: INV-MSG-03"""
        cls = client_class(username="other")
        code, out = self._run(cls, username="somebody", send=True)
        self.assertEqual(code, 2, out)
        self.assertEqual([c.sent_method for c in cls.instances], [None] * len(cls.instances))

    def test_matching_username_passes(self):
        """Контроль: совпавший username не отказывается (dry-run, код 0).
        Требование: INV-MSG-03"""
        cls = client_class(username="somebody")
        code, out = self._run(cls, username="somebody")
        self.assertEqual(code, 0, out)

    def test_no_expected_username_nameless_chat_passes(self):
        """Контроль: ожидаемый username не задан - безымянный чат допустим (код 0).
        Требование: INV-MSG-03"""
        cls = client_class(username=None)
        code, out = self._run(cls, username=None)
        self.assertEqual(code, 0, out)


class PullOneUsernameMismatch(unittest.TestCase):
    """pull-one: ожидаемый username задан, у чата его нет - код 2, выкачки нет.

    Требование: INV-MSG-03
    """

    def _run(self, cls, expected):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = str(Path(tmp) / "зеркало")
            out, err = io.StringIO(), io.StringIO()
            with patched_env(tgp.tgs, cls):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = asyncio.run(tgp.amain(123, out_path, expected))
            written = sorted(p.name for p in Path(out_path).rglob("*")) if Path(out_path).exists() else []
        return code, out.getvalue() + err.getvalue(), written

    def _assert_no_pull(self, cls, written):
        fetched = [c for i in cls.instances for c in i.calls
                   if c in ("iter_messages", "get_messages", "download_media")]
        self.assertEqual(fetched, [], "выкачка началась")
        self.assertNotIn("result.json", written)

    def test_nameless_chat_attr_none_refused_with_2(self):
        """Чат без username (None), ожидаемый задан: код 2, выкачки нет.
        Требование: INV-MSG-03"""
        cls = client_class(username=None)
        code, out, written = self._run(cls, "somebody")
        self.assertEqual(code, 2, out)
        self._assert_no_pull(cls, written)

    def test_nameless_chat_no_attr_refused_with_2(self):
        """У сущности нет атрибута username: код 2, выкачки нет. Требование: INV-MSG-03"""
        cls = client_class(has_username_attr=False)
        code, out, written = self._run(cls, "somebody")
        self.assertEqual(code, 2, out)
        self._assert_no_pull(cls, written)

    def test_named_chat_other_username_refused_with_2(self):
        """Контроль обвязки: несовпадение у именного чата - 2 (уже так по спеке "сейчас").
        Требование: INV-MSG-03"""
        cls = client_class(username="other")
        code, out, written = self._run(cls, "somebody")
        self.assertEqual(code, 2, out)
        self._assert_no_pull(cls, written)


if __name__ == "__main__":
    unittest.main()
