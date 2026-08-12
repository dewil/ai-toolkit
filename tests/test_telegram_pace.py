#!/usr/bin/env python3
"""Тесты гейта темпа в telegram-send.py. stdlib-only (unittest), без сети.

Запуск: python3 tests/test_telegram_pace.py

Гейт не дает отправить серию сообщений пачкой: живой человек не шлет три абзаца
в одну секунду. Правило про это существовало текстом и трижды не удержало
поведение на живых клиентах - поэтому оно продублировано механикой в точке
действия (rules/outbound-timing.md, "Паузы внутри своей серии").

Ключевые инварианты: пауза считается по длине предыдущего сообщения и
ФИКСИРУЕТСЯ при записи (пересчет на каждой проверке позволял бы вымучить
меньшее значение повтором команды); отказ, а не сон; разброс есть, но границы
соблюдаются; обход только явным флагом.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# telethon в тестах не нужен и не установлен - подсовываем заглушку до импорта.
sys.modules.setdefault("telethon", mock.MagicMock())
_spec = importlib.util.spec_from_file_location("tgsend", SCRIPTS / "telegram-send.py")
tgs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tgs)


class PaceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(tgs, "PACE_STATE_PATH", Path(self.tmp.name) / "last-sent.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self) -> dict:
        return json.loads(tgs.PACE_STATE_PATH.read_text(encoding="utf-8"))


class Required(PaceBase):
    def test_grows_with_length(self):
        short = min(tgs.pace_required(0) for _ in range(50))
        long = min(tgs.pace_required(600) for _ in range(50))
        self.assertGreater(long, short)

    def test_floor_and_ceiling(self):
        # Потолок - это потолок: прежний порядок "сначала min, потом +jitter"
        # давал 243 секунды при заявленных 180, и тест это освящал.
        for chars in (0, 100, 5000):
            for _ in range(50):
                need = tgs.pace_required(chars)
                self.assertGreaterEqual(need, tgs.PACE_MIN_GAP)
                self.assertLessEqual(need, tgs.PACE_MAX_GAP + 1e-6)

    def test_jitter_survives_at_ceiling(self):
        # У длинных сообщений расчет упирается в потолок - разброс обязан
        # остаться, иначе паузы лягут ровной сеткой ровно там, где серия длинная.
        values = {round(tgs.pace_required(5000), 4) for _ in range(40)}
        self.assertGreater(len(values), 1)

    def test_jitter_gives_spread_not_constant(self):
        # Ровные интервалы читаются как расписание так же, как залп.
        values = {round(tgs.pace_required(300), 4) for _ in range(40)}
        self.assertGreater(len(values), 1)


class CheckAndGuard(PaceBase):
    def test_empty_state_allows_send(self):
        self.assertEqual(tgs.pace_check("acc", 42), (0.0, 0.0))
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 0)

    def test_right_after_send_refuses(self):
        tgs.pace_record("acc", 42, 300)
        wait, required = tgs.pace_check("acc", 42)
        self.assertGreater(wait, 0)
        self.assertGreater(required, 0)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 3)

    def test_after_waiting_allows(self):
        tgs.pace_record("acc", 42, 300)
        entry = self.state()["acc:42"]
        entry["ts"] = time.time() - entry["required"] - 1
        tgs.PACE_STATE_PATH.write_text(json.dumps({"acc:42": entry}), encoding="utf-8")
        self.assertEqual(tgs.pace_check("acc", 42)[0], 0.0)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 0)

    def test_required_is_frozen_at_record_time(self):
        # Пересчет на каждой проверке позволил бы повторять команду, пока
        # случайный джиттер не выпадет поменьше, и показывал бы каждый раз
        # другое "нужно N сек".
        tgs.pace_record("acc", 42, 400)
        first = tgs.pace_check("acc", 42)[1]
        for _ in range(20):
            self.assertEqual(tgs.pace_check("acc", 42)[1], first)

    def test_other_chat_not_affected(self):
        tgs.pace_record("acc", 42, 300)
        self.assertEqual(tgs.pace_guard("acc", 999, skip=False), 0)

    def test_other_account_not_affected(self):
        tgs.pace_record("acc", 42, 300)
        self.assertEqual(tgs.pace_guard("other", 42, skip=False), 0)

    def test_skip_flag_bypasses(self):
        tgs.pace_record("acc", 42, 300)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=True), 0)


class HostileState(PaceBase):
    """Мусор в состоянии не должен ни держать чат вечно, ни валить скрипт."""

    def write(self, entry):
        tgs.PACE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tgs.PACE_STATE_PATH.write_text(json.dumps({"acc:42": entry}), encoding="utf-8")

    def test_future_timestamp_does_not_block(self):
        # Часы съехали назад или состояние из бэкапа: держать чат на величину
        # сдвига нельзя - врут часы, а не человек торопится.
        self.write({"ts": time.time() + 3600, "required": 40})
        self.assertEqual(tgs.pace_check("acc", 42), (0.0, 0.0))
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 0)

    def test_infinite_required_does_not_block_forever(self):
        self.write({"ts": time.time(), "required": float("inf")})
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 0)

    def test_nan_does_not_block(self):
        self.write({"ts": time.time(), "required": float("nan")})
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 0)

    def test_huge_int_does_not_crash(self):
        self.write({"ts": 10 ** 400, "chars": 10})
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 0)

    def test_legacy_required_is_stable_across_checks(self):
        # Без зафиксированной паузы прежняя версия рандомила ее на каждой
        # проверке - повтором команды можно было вымучить значение поменьше.
        self.write({"ts": time.time(), "chars": 300})
        first = tgs.pace_check("acc", 42)[1]
        for _ in range(20):
            self.assertEqual(tgs.pace_check("acc", 42)[1], first)


class ChatKey(PaceBase):
    def test_same_entity_gives_one_key_regardless_of_how_addressed(self):
        # Канал адресуют и сырым id, и marked (-100...). Ключ считается по
        # резолвнутому entity, поэтому оба пути дают один счетчик - иначе это
        # штатный обход темпа без всякого флага.
        class Channel:
            def __init__(self, ident): self.id = ident
        self.assertEqual(tgs.pace_key("acc", Channel(1234567890)),
                         tgs.pace_key("acc", Channel(1234567890)))

    def test_same_id_different_entity_types_do_not_collide(self):
        class Channel:
            def __init__(self, ident): self.id = ident
        class User:
            def __init__(self, ident): self.id = ident
        self.assertNotEqual(tgs.pace_key("acc", Channel(123)), tgs.pace_key("acc", User(123)))

    def test_different_chats_still_differ(self):
        self.assertNotEqual(tgs.pace_key("acc", 111), tgs.pace_key("acc", 222))


class TempFile(PaceBase):
    def test_tmp_name_is_unique_and_cleaned(self):
        tgs.pace_record("acc", 42, 100)
        leftovers = list(tgs.PACE_STATE_PATH.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_state_file_not_group_readable(self):
        tgs.pace_record("acc", 42, 100)
        self.assertEqual(tgs.PACE_STATE_PATH.stat().st_mode & 0o077, 0)


class StateFile(PaceBase):
    def test_record_writes_ts_chars_required(self):
        tgs.pace_record("acc", 42, 250)
        entry = self.state()["acc:42"]
        self.assertEqual(entry["chars"], 250)
        self.assertGreater(entry["ts"], 0)
        self.assertGreater(entry["required"], 0)

    def test_broken_state_does_not_block(self):
        # Битый файл не должен останавливать отправку: гейт - предохранитель
        # темпа, а не хранилище, и его поломка не повод молчать в чат.
        tgs.PACE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tgs.PACE_STATE_PATH.write_text("{не json", encoding="utf-8")
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 0)

    def test_legacy_entry_without_required_still_works(self):
        # Записи, сделанные до фиксации паузы: required считается на лету.
        tgs.PACE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tgs.PACE_STATE_PATH.write_text(
            json.dumps({"acc:42": {"ts": time.time(), "chars": 300}}), encoding="utf-8")
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 3)

    def test_record_survives_unwritable_dir(self):
        tgs.PACE_STATE_PATH = Path("/proc/nonexistent-dir/last-sent.json")
        tgs.pace_record("acc", 42, 100)   # не должно бросать


if __name__ == "__main__":
    unittest.main(verbosity=2)
