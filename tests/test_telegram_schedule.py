#!/usr/bin/env python3
"""Тесты серверной отложенной отправки (--schedule) в telegram-send.py и
telegram-send-one.py. stdlib-only (unittest), без сети.

Запуск: python3 tests/test_telegram_schedule.py

Отложенное сообщение (Telethon: send_message(..., schedule=dt)) ложится в
очередь на серверах Telegram и уходит само, в назначенный момент. До срока оно
НЕ появляется в обычной истории чата - оно доступно только через
get_messages(entity, scheduled=True). Раньше в обоих скриптах отдельной
послеотправочной проверки не было вовсе (только эхо sent.id из send_message);
для отложенных этого достаточно, но id, который вернул send_message, валиден
только внутри очереди отложенных и сменится после доставки - поэтому
verify_scheduled ищет именно там, а не в обычной истории (rules/silent-failure.md,
форма 3 - "проверка не того слоя": иначе честно поставленное в очередь
читалось бы как ненайденное).
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# telethon в тестах не нужен и не установлен - подсовываем заглушку до импорта.
# get_attributes с дефолтным возвратом: тесты, которым безразличны атрибуты
# файла (все, кроме PreloadFileAttributesBeforeUpload), не должны падать на
# распаковке неотконфигурированного MagicMock (unpacking бросает ValueError).
# Если telethon в sys.modules уже настоящий (другой тестовый файл импортировал
# его раньше в этом же прогоне pytest) - setdefault здесь no-op, и это тоже
# штатно: реальный get_attributes на настоящем временном файле отрабатывает
# сам.
_telethon_stub = mock.MagicMock()
_telethon_stub.utils.get_attributes.return_value = ([], None)
sys.modules.setdefault("telethon", _telethon_stub)

_spec = importlib.util.spec_from_file_location("tgsend_sched", SCRIPTS / "telegram-send.py")
tgs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tgs)

_spec_one = importlib.util.spec_from_file_location("tgsend_one_sched", SCRIPTS / "telegram-send-one.py")
tgs_one = importlib.util.module_from_spec(_spec_one)
_spec_one.loader.exec_module(tgs_one)


# ---------------------------------------------------------------------------
# Разбор и форматирование времени - чистые функции, без клиента и без сети.
# ---------------------------------------------------------------------------

class ParseSchedule(unittest.TestCase):
    NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # 09:30 - ровная минута (гейт RoundMinuteGate ниже) и не то, что проверяют
    # тесты этого класса (разбор оффсета/зоны/DST) - exact_minute=True снимает
    # гейт, не трогая проверяемое поведение.

    def test_with_offset(self):
        dt = tgs.parse_schedule("2026-09-22T09:30+03:00", now=self.NOW, exact_minute=True)
        self.assertEqual(dt, datetime(2026, 9, 22, 9, 30, tzinfo=timezone(timedelta(hours=3))))

    def test_without_offset_uses_local_machine_zone(self):
        # Наивное время трактуется как локальное - тот же результат, что дает
        # astimezone() на эквивалентном naive datetime.
        expected = datetime(2026, 9, 22, 9, 30).astimezone()
        dt = tgs.parse_schedule("2026-09-22T09:30", now=self.NOW, exact_minute=True)
        self.assertEqual(dt, expected)
        self.assertIsNotNone(dt.tzinfo)

    def test_seconds_are_optional(self):
        dt = tgs.parse_schedule("2026-09-22T09:30:15+03:00", now=self.NOW)
        self.assertEqual(dt.second, 15)
        dt2 = tgs.parse_schedule("2026-09-22T09:30+03:00", now=self.NOW, exact_minute=True)
        self.assertEqual(dt2.second, 0)

    def test_space_separator_accepted(self):
        dt = tgs.parse_schedule("2026-09-22 09:30+03:00", now=self.NOW, exact_minute=True)
        self.assertEqual((dt.hour, dt.minute), (9, 30))

    def test_z_suffix_is_utc(self):
        dt = tgs.parse_schedule("2026-09-22T09:30Z", now=self.NOW, exact_minute=True)
        self.assertEqual(dt.utcoffset(), timedelta(0))

    def test_negative_offset(self):
        dt = tgs.parse_schedule("2026-09-22T09:30-05:00", now=self.NOW, exact_minute=True)
        self.assertEqual(dt.utcoffset(), -timedelta(hours=5))

    def test_garbage_rejected(self):
        with self.assertRaises(ValueError):
            tgs.parse_schedule("завтра утром", now=self.NOW)

    def test_incomplete_string_rejected(self):
        with self.assertRaises(ValueError):
            tgs.parse_schedule("2026-09-22", now=self.NOW)

    def test_past_time_rejected(self):
        now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError) as cm:
            tgs.parse_schedule("2026-09-22T09:00+00:00", now=now)
        self.assertIn("прошл", str(cm.exception))

    def test_equal_to_now_rejected(self):
        # Граница: "прямо сейчас" - не время в будущем, ставить в очередь нечего.
        now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            tgs.parse_schedule("2026-09-22T10:00+00:00", now=now)

    def test_far_future_rejected(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValueError) as cm:
            tgs.parse_schedule("2027-06-01T09:00+00:00", now=now)
        self.assertIn("год", str(cm.exception))

    def test_within_a_year_accepted(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        dt = tgs.parse_schedule("2026-12-31T09:00+00:00", now=now, exact_minute=True)
        self.assertEqual(dt.year, 2026)

    def test_too_close_to_now_rejected(self):
        # Telegram шлет немедленно, если до срока меньше 10 сек (документировано) -
        # запас в SCHEDULE_MIN_LEAD (120с) должен отказывать заметно раньше этой
        # границы, а не подходить к ней вплотную.
        now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError) as cm:
            tgs.parse_schedule("2026-09-22T10:01+00:00", now=now)  # +60с
        self.assertIn(str(tgs.SCHEDULE_MIN_LEAD), str(cm.exception))

    def test_exactly_min_lead_accepted(self):
        now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
        dt = tgs.parse_schedule(
            "2026-09-22T10:02+00:00", now=now  # +120с ровно
        )
        self.assertEqual(dt.minute, 2)

    def test_offset_minutes_over_59_rejected(self):
        # "+03:99" сейчас нормализуется timedelta в действительное время
        # (+04:39) вместо отказа - оффсет с минутами вне 00-59 отклоняем.
        with self.assertRaises(ValueError):
            tgs.parse_schedule("2026-09-22T09:30+03:99", now=self.NOW)

    def test_offset_hours_over_14_rejected(self):
        # Реальный максимум оффсета UTC - +14:00; "-0099" сейчас
        # нормализуется вместо отказа.
        with self.assertRaises(ValueError):
            tgs.parse_schedule("2026-09-22T09:30-0099", now=self.NOW)

    def test_offset_hours_exactly_14_accepted(self):
        dt = tgs.parse_schedule("2026-09-22T09:30+14:00", now=self.NOW, exact_minute=True)
        self.assertEqual(dt.utcoffset(), timedelta(hours=14))

    def test_send_one_reuses_parse_schedule_via_import(self):
        # send-one не дублирует парсер - зовет tgs.parse_schedule через свой
        # импорт telegram-send.py (тот же код, отдельный объект модуля).
        dt = tgs_one.tgs.parse_schedule("2026-09-22T09:30+03:00", now=self.NOW, exact_minute=True)
        self.assertEqual(dt, tgs.parse_schedule("2026-09-22T09:30+03:00", now=self.NOW, exact_minute=True))


class DstTransitions(unittest.TestCase):
    """Пункт 5 задачи: время без оффсета в зоне с переходом на летнее/зимнее
    время - несуществующее (пропущенный час) или неоднозначное (повторенный
    час) локальное время молча не выбирается, отказ с явным требованием
    задать оффсет. TZ=Europe/Berlin: переход на летнее в 2027-03-28 02:00
    (02:00-03:00 не существует), переход на зимнее в 2026-10-25 03:00
    (02:00-03:00 повторяется)."""

    def setUp(self):
        self._orig_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig_tz
        time.tzset()

    def test_nonexistent_spring_forward_rejected(self):
        with self.assertRaises(ValueError) as cm:
            tgs.parse_schedule("2027-03-28T02:30", now=ParseSchedule.NOW)
        self.assertIn("оффсет", str(cm.exception))

    def test_ambiguous_fall_back_rejected(self):
        with self.assertRaises(ValueError) as cm:
            tgs.parse_schedule("2026-10-25T02:30", now=ParseSchedule.NOW)
        self.assertIn("оффсет", str(cm.exception))

    def test_explicit_offset_bypasses_dst_ambiguity(self):
        # С явным оффсетом неоднозначности нет вовсе - оффсет снимает вопрос.
        # 02:30 - ровная минута, к DST отношения не имеющая - exact_minute=True.
        dt = tgs.parse_schedule("2026-10-25T02:30+02:00", now=ParseSchedule.NOW, exact_minute=True)
        self.assertEqual(dt.utcoffset(), timedelta(hours=2))

    def test_ordinary_local_time_still_works_in_dst_zone(self):
        # Время вне окна перехода в той же зоне не задето новой проверкой.
        dt = tgs.parse_schedule("2026-06-15T09:30", now=ParseSchedule.NOW, exact_minute=True)
        self.assertEqual(dt.utcoffset(), timedelta(hours=2))  # летнее (CEST)


class RoundMinuteGate(unittest.TestCase):
    """Гейт на ровную минуту в --schedule (rules/outbound-timing.md, "Ровная
    минута"): :00/:15/:30/:45 с секундами :00 отклоняются кодом 6 (проверяется
    на уровне main() в RoundMinuteGateCLI ниже), обход - exact_minute=True."""

    NOW = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)

    def test_round_minute_00_rejected_with_hint(self):
        with self.assertRaises(tgs.RoundMinuteRejected) as cm:
            tgs.parse_schedule("2026-09-22T09:00+00:00", now=self.NOW)
        self.assertIn("09:07", str(cm.exception))

    def test_round_minute_is_still_a_value_error(self):
        # Старый контракт (main() ловил ValueError) не должен сломаться.
        with self.assertRaises(ValueError):
            tgs.parse_schedule("2026-09-22T09:00+00:00", now=self.NOW)

    def test_non_round_minute_09_07_accepted(self):
        dt = tgs.parse_schedule("2026-09-22T09:07+00:00", now=self.NOW)
        self.assertEqual(dt.minute, 7)

    def test_quarter_hour_marks_all_rejected(self):
        for value in (
            "2026-09-22T09:15+00:00", "2026-09-22T09:30+00:00", "2026-09-22T09:45+00:00",
        ):
            with self.subTest(value=value):
                with self.assertRaises(tgs.RoundMinuteRejected):
                    tgs.parse_schedule(value, now=self.NOW)

    def test_exact_minute_flag_bypasses_gate(self):
        dt = tgs.parse_schedule("2026-09-22T09:00+00:00", now=self.NOW, exact_minute=True)
        self.assertEqual((dt.minute, dt.second), (0, 0))

    def test_nonzero_seconds_is_not_a_round_minute(self):
        # Пункт задачи: 09:00:30 - секунды ненулевые, не ровная минута, проходит.
        dt = tgs.parse_schedule("2026-09-22T09:00:30+00:00", now=self.NOW)
        self.assertEqual(dt.second, 30)

    def test_past_check_precedes_round_minute_gate(self):
        now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError) as cm:
            tgs.parse_schedule("2026-09-22T09:00+00:00", now=now)
        self.assertNotIsInstance(cm.exception, tgs.RoundMinuteRejected)
        self.assertIn("прошл", str(cm.exception))

    def test_min_lead_check_precedes_round_minute_gate(self):
        now = datetime(2026, 9, 22, 8, 58, 30, tzinfo=timezone.utc)  # запас 90с < 120
        with self.assertRaises(ValueError) as cm:
            tgs.parse_schedule("2026-09-22T09:00+00:00", now=now)
        self.assertNotIsInstance(cm.exception, tgs.RoundMinuteRejected)
        self.assertIn(str(tgs.SCHEDULE_MIN_LEAD), str(cm.exception))

    def test_max_days_check_precedes_round_minute_gate(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValueError) as cm:
            tgs.parse_schedule("2027-06-01T09:00+00:00", now=now)
        self.assertNotIsInstance(cm.exception, tgs.RoundMinuteRejected)
        self.assertIn("год", str(cm.exception))

    def test_suggestion_is_deterministic_forward_7_minutes(self):
        dt = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(tgs.suggest_non_round(dt), dt + timedelta(minutes=7))
        self.assertEqual(tgs.suggest_non_round(dt), tgs.suggest_non_round(dt))

    def test_suggestion_never_lands_on_a_round_minute(self):
        for minute in (0, 15, 30, 45):
            dt = datetime(2026, 9, 22, 9, minute, tzinfo=timezone.utc)
            self.assertFalse(tgs.is_round_minute(tgs.suggest_non_round(dt)))

    def test_send_one_reuses_gate_via_import(self):
        with self.assertRaises(tgs_one.tgs.RoundMinuteRejected):
            tgs_one.tgs.parse_schedule("2026-09-22T09:00+00:00", now=self.NOW)


class RoundMinuteGateCLI(unittest.TestCase):
    """Гейт на уровне main()/CLI: код возврата 6, действует одинаково с
    --send и без него - проверка идет до asyncio.run, до сети."""

    def _next_round_iso(self):
        now = datetime.now(timezone.utc)
        candidate = now.replace(second=0, microsecond=0)
        candidate += timedelta(minutes=(15 - candidate.minute % 15) or 15)
        while (candidate - now).total_seconds() < tgs.SCHEDULE_MIN_LEAD + 60:
            candidate += timedelta(minutes=15)
        return candidate, candidate.strftime("%Y-%m-%dT%H:%M") + "+00:00"

    def test_send_py_rejects_round_minute_with_code_6(self):
        _, iso = self._next_round_iso()
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["telegram-send.py", "--to", "чат", "--schedule", iso]):
            with contextlib.redirect_stderr(err):
                code = tgs.main()
        self.assertEqual(code, 6)
        self.assertIn("ровн", err.getvalue().lower())

    def test_send_py_rejects_round_minute_with_code_6_in_dry_run_too(self):
        # Без --send - тот же отказ, тот же код: проверка не зависит от режима.
        _, iso = self._next_round_iso()
        err = io.StringIO()
        with mock.patch.object(
            sys, "argv", ["telegram-send.py", "--to", "чат", "--text", "привет", "--schedule", iso],
        ):
            with contextlib.redirect_stderr(err):
                code = tgs.main()
        self.assertEqual(code, 6)

    def test_send_py_rejects_round_minute_with_code_6_with_send_flag(self):
        _, iso = self._next_round_iso()
        err = io.StringIO()
        argv = ["telegram-send.py", "--to", "чат", "--text", "привет", "--schedule", iso, "--send"]
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stderr(err):
                code = tgs.main()
        self.assertEqual(code, 6)

    def test_send_py_exact_minute_flag_lets_main_proceed_past_the_gate(self):
        _, iso = self._next_round_iso()
        argv = ["telegram-send.py", "--to", "чат", "--exact-minute", "--schedule", iso]
        with mock.patch.object(tgs, "amain", mock.AsyncMock(return_value=0)):
            with mock.patch.object(sys, "argv", argv):
                code = tgs.main()
        self.assertEqual(code, 0)

    def test_send_one_rejects_round_minute_with_code_6(self):
        _, iso = self._next_round_iso()
        err = io.StringIO()
        argv = ["telegram-send-one.py", "111", "--text", "привет", "--schedule", iso]
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stderr(err):
                code = tgs_one.main()
        self.assertEqual(code, 6)

    def test_send_one_exact_minute_flag_lets_main_proceed_past_the_gate(self):
        _, iso = self._next_round_iso()
        argv = ["telegram-send-one.py", "111", "--exact-minute", "--schedule", iso]
        with mock.patch.object(tgs_one, "amain", mock.AsyncMock(return_value=0)):
            with mock.patch.object(sys, "argv", argv):
                code = tgs_one.main()
        self.assertEqual(code, 0)


class RoundMinuteDryRunNote(unittest.TestCase):
    """Пометка "ровная минута: разрешена явно" в dry-run - только когда
    schedule_dt реально ровный (сюда он мог попасть только через
    --exact-minute, гейт main() иначе отказал бы раньше)."""

    ROUND = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
    NON_ROUND = datetime(2026, 9, 22, 9, 7, tzinfo=timezone.utc)

    def test_send_py_prints_note_for_round_minute(self):
        with patched_send_env(tgs, tgs, FakeClient):
            args = send_args(schedule=self.ROUND)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asyncio.run(tgs.amain(args))
        self.assertIn("ровная минута: разрешена явно", out.getvalue())

    def test_send_py_no_note_for_non_round_minute(self):
        with patched_send_env(tgs, tgs, FakeClient):
            args = send_args(schedule=self.NON_ROUND)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asyncio.run(tgs.amain(args))
        self.assertNotIn("ровная минута", out.getvalue())

    def test_send_one_prints_note_for_round_minute(self):
        with patched_send_env(tgs_one, tgs_one.tgs, FakeClient):
            args = send_one_args(schedule=self.ROUND)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asyncio.run(tgs_one.amain(args))
        self.assertIn("ровная минута: разрешена явно", out.getvalue())


class FormatSchedule(unittest.TestCase):
    def test_positive_offset(self):
        dt = datetime(2026, 9, 22, 9, 30, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(tgs.format_schedule(dt), "2026-09-22 09:30:00 +03:00")

    def test_negative_offset(self):
        dt = datetime(2026, 9, 22, 9, 30, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(tgs.format_schedule(dt), "2026-09-22 09:30:00 -05:00")

    def test_utc_zero_offset(self):
        dt = datetime(2026, 9, 22, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(tgs.format_schedule(dt), "2026-09-22 09:30:00 +00:00")

    def test_half_hour_offset(self):
        dt = datetime(2026, 9, 22, 9, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(tgs.format_schedule(dt), "2026-09-22 09:30:00 +05:30")

    def test_seconds_always_printed(self):
        # Превью обязано совпадать с тем, что реально уйдет: секунды печатаются
        # всегда, а не только когда они ненулевые.
        dt = datetime(2026, 9, 22, 9, 30, 59, tzinfo=timezone.utc)
        self.assertEqual(tgs.format_schedule(dt), "2026-09-22 09:30:59 +00:00")


# ---------------------------------------------------------------------------
# Общая обвязка для amain() обоих скриптов: конфиг, авторизация и клиент -
# все заглушки, сеть не трогаем.
# ---------------------------------------------------------------------------

class FakeClient:
    """Достаточно amain(): резолв чата через iter_dialogs, отправка, очередь
    отложенных (get_messages(scheduled=True))."""

    def __init__(self, *a, **k):
        self.sent_method = None
        self.sent_kwargs = None
        self._scheduled: list = []
        self.next_id = 900

    async def is_user_authorized(self):
        return True

    def iter_dialogs(self):
        async def gen():
            yield types.SimpleNamespace(entity=types.SimpleNamespace(id=111, title="Чат"))
        return gen()

    async def get_entity(self, ident):
        return types.SimpleNamespace(id=ident, title="Чат")

    async def get_me(self):
        return types.SimpleNamespace(id="me", title="Избранное")

    async def upload_file(self, path):
        # Стенд-ин InputFile: production-код лишь передает то, что вернул
        # upload_file, дальше в send_file - конкретное значение тестам не важно.
        self.uploaded_path = path
        return f"uploaded:{path}"

    async def _record_send(self, method: str, *, text: str = "", **kwargs) -> types.SimpleNamespace:
        self.sent_method = method
        self.sent_kwargs = kwargs
        self.next_id += 1
        # date/message - как у настоящего Message из Telethon: verify_scheduled
        # сверяет по ним, а не только по id.
        msg = types.SimpleNamespace(id=self.next_id, date=kwargs.get("schedule"), message=text)
        if kwargs.get("schedule") is not None:
            self._scheduled.append(msg)
        return msg

    async def send_message(self, entity, text, **kwargs):
        return await self._record_send("send_message", text=text, **kwargs)

    async def send_file(self, entity, path, **kwargs):
        return await self._record_send("send_file", text=kwargs.get("caption", ""), **kwargs)

    async def get_messages(self, entity, scheduled=False):
        return list(self._scheduled) if scheduled else []


class NotFoundInQueueClient(FakeClient):
    """Отправка "прошла", но очередь отложенных пуста - имитация расхождения,
    которое verify_scheduled обязана поймать и показать предупреждением."""

    async def get_messages(self, entity, scheduled=False):
        return []


class QueueQueryFailsClient(FakeClient):
    """Запрос очереди отложенных падает - второй источник неопределенного
    результата наравне с пустой очередью (пункт 2 задачи)."""

    async def get_messages(self, entity, scheduled=False):
        if scheduled:
            raise RuntimeError("очередь отложенных недоступна")
        return []


class WrongDateInQueueClient(FakeClient):
    """id совпал, но дата в очереди - не назначенная: id из обычной истории и
    из очереди живут в разных пространствах, совпадение по одному id может
    быть ложным - verify_scheduled обязана отличить это от настоящей находки."""

    async def get_messages(self, entity, scheduled=False):
        if not scheduled:
            return []
        wrong = types.SimpleNamespace(
            id=self._scheduled[-1].id if self._scheduled else 901,
            date=datetime(2099, 1, 1, tzinfo=timezone.utc),
            message=self._scheduled[-1].message if self._scheduled else "",
        )
        return [wrong]


class HtmlStrippingClient(FakeClient):
    """Имитация того, что реально делает Telegram с parse_mode="html": в
    очереди и в ответе send_message лежит уже РАЗОБРАННЫЙ текст (без тегов),
    а не наш сырой HTML-вход. verify_scheduled обязана сверяться с тем, что
    вернула отправка (sent.message), а не с сырым --text."""

    async def _record_send(self, method: str, *, text: str = "", **kwargs) -> types.SimpleNamespace:
        plain = re.sub(r"<[^>]+>", "", text)
        return await super()._record_send(method, text=plain, **kwargs)


async def _noop_connect(client, **kw):
    return None


async def _noop_disconnect(client):
    return None


@contextlib.contextmanager
def patched_send_env(mod, tgs_mod, client_factory):
    """Подменяет тяжелые зависимости amain() в mod; общие функции (parse_schedule,
    format_schedule, verify_scheduled, pace_*) остаются настоящими - их и
    проверяем."""
    patches = {
        "load_project_config": lambda: {"chats": {"чат": tgs_mod.chat_entry(111)}},
        "load_auth": lambda account="default": {"session_name": "s", "api_id": 1, "api_hash": "h"},
        "client_kwargs": lambda auth: {},
        "TelegramClient": client_factory,
        "connect_with_retry": _noop_connect,
        "disconnect_quietly": _noop_disconnect,
    }
    originals = {k: getattr(tgs_mod, k) for k in patches}
    for k, v in patches.items():
        setattr(tgs_mod, k, v)
    tmp = tempfile.TemporaryDirectory()
    pace_orig = tgs_mod.PACE_STATE_PATH
    tgs_mod.PACE_STATE_PATH = Path(tmp.name) / "last-sent.json"
    try:
        yield
    finally:
        for k, v in originals.items():
            setattr(tgs_mod, k, v)
        tgs_mod.PACE_STATE_PATH = pace_orig
        tmp.cleanup()


def send_args(**overrides) -> types.SimpleNamespace:
    base = dict(
        to="чат", text="привет", file=None, send=False, no_pace_check=True,
        topic=None, reply_to=None, silent=False, schedule=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def send_one_args(**overrides) -> types.SimpleNamespace:
    base = dict(
        chat_id="111", username=None, text="привет", file=None, voice=False,
        send=False, no_pace_check=True, topic=None, reply_to=None,
        account="default", silent=False, html=False, schedule=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


DATETIME_2026_09_22_0930_MSK = datetime(2026, 9, 22, 9, 30, tzinfo=timezone(timedelta(hours=3)))


class DryRunShowsSchedule(unittest.TestCase):
    def test_send_py_prints_schedule_line(self):
        with patched_send_env(tgs, tgs, FakeClient):
            args = send_args(schedule=DATETIME_2026_09_22_0930_MSK)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = asyncio.run(tgs.amain(args))
        self.assertEqual(code, 0)
        self.assertIn("отложено до: 2026-09-22 09:30:00 +03:00", out.getvalue())

    def test_send_py_no_schedule_no_line(self):
        with patched_send_env(tgs, tgs, FakeClient):
            args = send_args(schedule=None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asyncio.run(tgs.amain(args))
        self.assertNotIn("отложено до:", out.getvalue())

    def test_send_one_prints_schedule_line(self):
        with patched_send_env(tgs_one, tgs_one.tgs, FakeClient):
            args = send_one_args(schedule=DATETIME_2026_09_22_0930_MSK)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = asyncio.run(tgs_one.amain(args))
        self.assertEqual(code, 0)
        self.assertIn("отложено до: 2026-09-22 09:30:00 +03:00", out.getvalue())


class SendPassesScheduleKwarg(unittest.TestCase):
    """Мутация "schedule потерялся по дороге" обязана уронить эти тесты -
    иначе флаг доходит только до dry-run печати, а реальный send_message о
    нем не узнает."""

    def test_send_py_text_message(self):
        client_holder = {}

        def factory(*a, **k):
            c = FakeClient(*a, **k)
            client_holder["client"] = c
            return c

        with patched_send_env(tgs, tgs, factory):
            args = send_args(send=True, schedule=DATETIME_2026_09_22_0930_MSK)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = asyncio.run(tgs.amain(args))
        self.assertEqual(code, 0)
        client = client_holder["client"]
        self.assertEqual(client.sent_method, "send_message")
        self.assertEqual(client.sent_kwargs["schedule"], DATETIME_2026_09_22_0930_MSK)

    def test_send_py_file_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "файл.txt"
            f.write_text("данные", encoding="utf-8")
            client_holder = {}

            def factory(*a, **k):
                c = FakeClient(*a, **k)
                client_holder["client"] = c
                return c

            with patched_send_env(tgs, tgs, factory):
                args = send_args(send=True, file=str(f), text="подпись",
                                  schedule=DATETIME_2026_09_22_0930_MSK)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = asyncio.run(tgs.amain(args))
        self.assertEqual(code, 0)
        client = client_holder["client"]
        self.assertEqual(client.sent_method, "send_file")
        self.assertEqual(client.sent_kwargs["schedule"], DATETIME_2026_09_22_0930_MSK)

    def test_send_one_text_message(self):
        client_holder = {}

        def factory(*a, **k):
            c = FakeClient(*a, **k)
            client_holder["client"] = c
            return c

        with patched_send_env(tgs_one, tgs_one.tgs, factory):
            args = send_one_args(send=True, schedule=DATETIME_2026_09_22_0930_MSK)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = asyncio.run(tgs_one.amain(args))
        self.assertEqual(code, 0)
        client = client_holder["client"]
        self.assertEqual(client.sent_method, "send_message")
        self.assertEqual(client.sent_kwargs["schedule"], DATETIME_2026_09_22_0930_MSK)

    def test_without_schedule_kwarg_is_none(self):
        """Флаг не задан - schedule=None уходит явно (тот же контракт, что у
        silent), а не выпадает из вызова вовсе."""
        client_holder = {}

        def factory(*a, **k):
            c = FakeClient(*a, **k)
            client_holder["client"] = c
            return c

        with patched_send_env(tgs, tgs, factory):
            args = send_args(send=True, schedule=None)
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(tgs.amain(args))
        self.assertIsNone(client_holder["client"].sent_kwargs["schedule"])


class PostSendVerification(unittest.TestCase):
    """Пункт 3 задачи: до срока отложенное сообщение не появляется в обычной
    истории - обычный send_message его не покажет: id из ответа валиден
    только внутри очереди отложенных. verify_scheduled ищет ИМЕННО в
    get_messages(entity, scheduled=True), а не в обычной истории."""

    def test_verify_scheduled_finds_message_in_queue(self):
        client = FakeClient()
        sent = asyncio.run(client.send_message(None, "текст", schedule=DATETIME_2026_09_22_0930_MSK))
        found, exc = asyncio.run(tgs.verify_scheduled(
            client, None, sent.id,
            expected_date=DATETIME_2026_09_22_0930_MSK, expected_text="текст",
        ))
        self.assertTrue(found)
        self.assertIsNone(exc)

    def test_verify_scheduled_rejects_id_collision_with_wrong_date(self):
        """id из обычной истории и из очереди живут в разных пространствах -
        совпадение по одному id может быть ложным, поэтому сверяется и дата."""
        client = WrongDateInQueueClient()
        sent = asyncio.run(client.send_message(None, "текст", schedule=DATETIME_2026_09_22_0930_MSK))
        found, exc = asyncio.run(tgs.verify_scheduled(
            client, None, sent.id,
            expected_date=DATETIME_2026_09_22_0930_MSK, expected_text="текст",
        ))
        self.assertFalse(found)
        self.assertIsNone(exc)

    def test_verify_scheduled_rejects_text_mismatch(self):
        client = FakeClient()
        sent = asyncio.run(client.send_message(None, "текст", schedule=DATETIME_2026_09_22_0930_MSK))
        found, exc = asyncio.run(tgs.verify_scheduled(
            client, None, sent.id,
            expected_date=DATETIME_2026_09_22_0930_MSK, expected_text="другой текст",
        ))
        self.assertFalse(found)
        self.assertIsNone(exc)

    def test_verify_scheduled_reports_query_exception(self):
        client = QueueQueryFailsClient()
        sent = asyncio.run(client.send_message(None, "текст", schedule=DATETIME_2026_09_22_0930_MSK))
        found, exc = asyncio.run(tgs.verify_scheduled(
            client, None, sent.id,
            expected_date=DATETIME_2026_09_22_0930_MSK, expected_text="текст",
        ))
        self.assertFalse(found)
        self.assertIsInstance(exc, RuntimeError)

    def test_verify_scheduled_absent_from_plain_history(self):
        """Обычная история (scheduled=False) для отложенного пуста - тем самым
        показывается, ПОЧЕМУ нужна отдельная ветка проверки, а не общий перечит."""
        client = FakeClient()
        sent = asyncio.run(client.send_message(None, "текст", schedule=DATETIME_2026_09_22_0930_MSK))
        plain_history = asyncio.run(client.get_messages(None, scheduled=False))
        self.assertNotIn(sent, plain_history)

    def test_send_py_reports_queued_status_in_stdout(self):
        with patched_send_env(tgs, tgs, FakeClient):
            args = send_args(send=True, schedule=DATETIME_2026_09_22_0930_MSK)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asyncio.run(tgs.amain(args))
        text = out.getvalue()
        self.assertIn("поставлено в очередь", text)
        self.assertIn("2026-09-22 09:30:00 +03:00", text)
        self.assertNotIn("предупреждение", text)

    def test_send_py_warns_when_missing_from_queue(self):
        """Пункт 2 задачи: расхождение (отправка "прошла", а в очереди отложенных
        пусто) - НЕОПРЕДЕЛЕННЫЙ результат, не успех: отдельный код возврата,
        не rc=0 с предупреждением (было раньше) - тихий отказ по
        rules/silent-failure.md."""
        with patched_send_env(tgs, tgs, NotFoundInQueueClient):
            args = send_args(send=True, schedule=DATETIME_2026_09_22_0930_MSK)
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = asyncio.run(tgs.amain(args))
        self.assertEqual(code, 4)
        self.assertIn("НЕОПРЕДЕЛЕННО", err.getvalue())
        self.assertIn("не нашлось в очереди отложенных", err.getvalue())
        self.assertIn("повторно не отправляй", err.getvalue())
        self.assertNotIn("OK:", out.getvalue())

    def test_send_py_reports_query_exception_reason(self):
        """Упавший запрос очереди - тоже неопределенный результат, причина
        исключения печатается, а не глушится."""
        with patched_send_env(tgs, tgs, QueueQueryFailsClient):
            args = send_args(send=True, schedule=DATETIME_2026_09_22_0930_MSK)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = asyncio.run(tgs.amain(args))
        self.assertEqual(code, 4)
        self.assertIn("НЕОПРЕДЕЛЕННО", err.getvalue())
        self.assertIn("очередь отложенных недоступна", err.getvalue())

    def test_send_one_html_verification_uses_returned_text_not_raw_input(self):
        """Пункт 2 задачи: verify_scheduled сравнивал с сырым --text (HTML),
        а в очереди лежит уже разобранный Telegram-ом текст - сравнение
        никогда бы не совпало. Правка сверяет с sent.message (тем, что
        вернула сама отправка)."""
        with patched_send_env(tgs_one, tgs_one.tgs, HtmlStrippingClient):
            args = send_one_args(send=True, html=True, text="<b>Привет</b>",
                                  schedule=DATETIME_2026_09_22_0930_MSK)
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = asyncio.run(tgs_one.amain(args))
        self.assertEqual(code, 0)
        self.assertIn("поставлено в очередь", out.getvalue())
        self.assertNotIn("НЕОПРЕДЕЛЕННО", err.getvalue())

    def test_send_one_reports_queued_status(self):
        with patched_send_env(tgs_one, tgs_one.tgs, FakeClient):
            args = send_one_args(send=True, schedule=DATETIME_2026_09_22_0930_MSK)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asyncio.run(tgs_one.amain(args))
        self.assertIn("поставлено в очередь", out.getvalue())

    def test_plain_send_keeps_old_message(self):
        """Регресс: без --schedule формулировка остается прежней, "OK: отправлено"."""
        with patched_send_env(tgs, tgs, FakeClient):
            args = send_args(send=True, schedule=None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asyncio.run(tgs.amain(args))
        self.assertIn("OK: отправлено в", out.getvalue())
        self.assertNotIn("поставлено в очередь", out.getvalue())


class MinLeadSecondCheck(unittest.TestCase):
    """Пункт 1 задачи: запас до срока проверяется ДВАЖДЫ - при разборе
    аргумента (ParseSchedule.test_too_close_to_now_rejected) и еще раз прямо
    перед отправкой, потому что подключение и прогрев диалогов съедают время.
    Здесь схема доставки в очередь настроена в обход parse_schedule (как и в
    остальных amain-тестах файла), поэтому это именно вторая проверка."""

    def test_send_py_refuses_when_lead_too_small_at_send_time(self):
        from datetime import datetime as _dt, timezone as _tz
        too_close = _dt.now(_tz.utc) + timedelta(seconds=5)
        with patched_send_env(tgs, tgs, FakeClient):
            args = send_args(send=True, schedule=too_close)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = asyncio.run(tgs.amain(args))
        self.assertNotEqual(code, 0)
        self.assertIn(str(tgs.SCHEDULE_MIN_LEAD), err.getvalue())

    def test_send_py_does_not_send_when_lead_too_small(self):
        from datetime import datetime as _dt, timezone as _tz
        too_close = _dt.now(_tz.utc) + timedelta(seconds=5)
        client_holder = {}

        def factory(*a, **k):
            c = FakeClient(*a, **k)
            client_holder["client"] = c
            return c

        with patched_send_env(tgs, tgs, factory):
            args = send_args(send=True, schedule=too_close)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                asyncio.run(tgs.amain(args))
        self.assertIsNone(client_holder["client"].sent_method)

    def test_send_one_refuses_when_lead_too_small_at_send_time(self):
        from datetime import datetime as _dt, timezone as _tz
        too_close = _dt.now(_tz.utc) + timedelta(seconds=5)
        with patched_send_env(tgs_one, tgs_one.tgs, FakeClient):
            args = send_one_args(send=True, schedule=too_close)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = asyncio.run(tgs_one.amain(args))
        self.assertNotEqual(code, 0)
        self.assertIn(str(tgs.SCHEDULE_MIN_LEAD), err.getvalue())


class _MutableNow:
    """Управляемые "текущие часы" для теста: upload_file должен успеть съесть
    часть запаса до назначенного времени, не дожидаясь реальных секунд."""

    def __init__(self, value: datetime):
        self.value = value


def _patched_datetime(clock: _MutableNow):
    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.value if tz is None else clock.value.astimezone(tz)

    return _FakeDatetime


class UploadEatsLead(unittest.TestCase):
    """Пункт 1 задачи: Telethon грузит файл ВНУТРИ send_file, до постановки в
    очередь - большой файл может съесть весь запас, который был достаточным
    на предыдущей проверке. Правка грузит файл отдельно (client.upload_file)
    и проверяет запас еще раз ПОСЛЕ загрузки, до самой отправки."""

    def test_send_py_aborts_when_upload_eats_the_lead(self):
        clock = _MutableNow(datetime.now(timezone.utc))
        # Хватает с запасом на первых двух проверках (парсинг тут не при чем -
        # schedule_dt подставляется готовым, как и в остальных amain-тестах).
        schedule_dt = clock.value + timedelta(seconds=tgs.SCHEDULE_MIN_LEAD + 30)

        class SlowUploadClient(FakeClient):
            async def upload_file(self, path):
                # Загрузка "съела" 20 из 30 секунд запаса - меньше минимума.
                clock.value = clock.value + timedelta(seconds=tgs.SCHEDULE_MIN_LEAD + 20)
                return "uploaded-handle"

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "большой.bin"
            f.write_bytes(b"x")
            client_holder = {}

            def factory(*a, **k):
                c = SlowUploadClient(*a, **k)
                client_holder["client"] = c
                return c

            with mock.patch.object(tgs, "datetime", _patched_datetime(clock)):
                with patched_send_env(tgs, tgs, factory):
                    args = send_args(send=True, file=str(f), text="подпись", schedule=schedule_dt)
                    err = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                        code = asyncio.run(tgs.amain(args))
        self.assertEqual(code, 2)
        self.assertIsNone(client_holder["client"].sent_method)
        self.assertIn(str(tgs.SCHEDULE_MIN_LEAD), err.getvalue())


class PreloadFileAttributesBeforeUpload(unittest.TestCase):
    """Пункт 2 задачи: после upload_file в send_file уходит InputFile, из
    которого Telethon не может прочитать содержимое файла заново - для
    голосового вложения получается DocumentAttributeAudio(voice=True,
    duration=0) вместо настоящей длительности. Атрибуты вычисляются из
    ИСХОДНОГО файла (telethon.utils.get_attributes) ДО upload_file и уходят в
    send_file явным keyword-параметром attributes. Тесты проверяют только
    проводку - что вычисленные атрибуты доходят до send_file нетронутыми и что
    get_attributes зовется по исходному пути, а не по хендлу."""

    def test_send_one_voice_keeps_real_duration_after_preload(self):
        fake_audio = types.SimpleNamespace(voice=True, duration=42)
        get_attrs = mock.Mock(return_value=([fake_audio], "audio/ogg"))
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "голос.ogg"
            f.write_bytes(b"x")
            client_holder = {}

            def factory(*a, **k):
                c = FakeClient(*a, **k)
                client_holder["client"] = c
                return c

            with mock.patch.object(
                tgs_one.tgs, "telethon_utils",
                types.SimpleNamespace(get_attributes=get_attrs), create=True,
            ):
                with patched_send_env(tgs_one, tgs_one.tgs, factory):
                    schedule_dt = datetime.now(timezone.utc) + timedelta(days=1)
                    args = send_one_args(send=True, file=str(f), voice=True,
                                          text="", schedule=schedule_dt)
                    with contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()):
                        code = asyncio.run(tgs_one.amain(args))
        self.assertEqual(code, 0)
        client = client_holder["client"]
        attrs = client.sent_kwargs["attributes"]
        self.assertEqual(attrs, [fake_audio])
        self.assertTrue(attrs[0].voice)
        self.assertGreater(attrs[0].duration, 0)
        # Вызван с исходным путем и voice_note=True - ДО upload_file, значит
        # по реальному файлу, а не по InputFile-хендлу без содержимого.
        call_args, call_kwargs = get_attrs.call_args
        self.assertEqual(Path(call_args[0]).name, "голос.ogg")
        self.assertTrue(call_kwargs.get("voice_note"))

    def test_send_py_document_gets_filename_attribute_after_preload(self):
        fake_name = types.SimpleNamespace(file_name="файл.txt")
        get_attrs = mock.Mock(return_value=([fake_name], "text/plain"))
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "файл.txt"
            f.write_text("данные", encoding="utf-8")
            client_holder = {}

            def factory(*a, **k):
                c = FakeClient(*a, **k)
                client_holder["client"] = c
                return c

            with mock.patch.object(
                tgs, "telethon_utils",
                types.SimpleNamespace(get_attributes=get_attrs), create=True,
            ):
                with patched_send_env(tgs, tgs, factory):
                    schedule_dt = datetime.now(timezone.utc) + timedelta(days=1)
                    args = send_args(send=True, file=str(f), text="подпись",
                                      schedule=schedule_dt)
                    with contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()):
                        code = asyncio.run(tgs.amain(args))
        self.assertEqual(code, 0)
        client = client_holder["client"]
        attrs = client.sent_kwargs["attributes"]
        self.assertEqual(attrs, [fake_name])
        self.assertEqual(attrs[0].file_name, "файл.txt")
        get_attrs.assert_called_once()


class PaceByDeliveryTime(unittest.TestCase):
    """Пункт 3 задачи: для отложенной отправки в реестр темпа пишется
    НАЗНАЧЕННОЕ время доставки (а не момент постановки в очередь), и проверка
    темпа сравнивает его с записанными временами того же адресата - в обе
    стороны: обычная немедленная отправка тоже обязана учитывать уже
    поставленные в очередь отложенные, иначе выйдет синхронный залп."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(tgs, "PACE_STATE_PATH", Path(self.tmp.name) / "last-sent.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_scheduled_far_apart_both_allowed(self):
        dt1 = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 12, 2, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt1), 0)
        tgs.pace_record("acc", 42, 300, at=dt1, scheduled=True)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt2), 0)

    def test_two_scheduled_close_together_second_refused(self):
        dt1 = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
        dt2 = dt1 + timedelta(seconds=5)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt1), 0)
        tgs.pace_record("acc", 42, 300, at=dt1, scheduled=True)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt2), 3)

    def test_scheduled_soon_then_immediate_refused(self):
        # "отложенное на через 30 секунд + немедленное сейчас" из задачи.
        soon = datetime.now(timezone.utc) + timedelta(seconds=30)
        tgs.pace_record("acc", 42, 300, at=soon, scheduled=True)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 3)

    def test_immediate_then_scheduled_soon_refused(self):
        # Симметрично: немедленная отправка уже записана, а следующее
        # отложенное попадает слишком близко к ней по времени доставки.
        tgs.pace_record("acc", 42, 300)
        soon = datetime.now(timezone.utc) + timedelta(seconds=5)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=soon), 3)

    def test_record_marks_scheduled_entries_with_delivery_time(self):
        # Формат: отложенные лежат СПИСКОМ под ОТДЕЛЬНЫМ ключом реестра (см.
        # pace_scheduled_key), а не переписывают верхнеуровневые ts/chars/
        # required (те - только для последней ОБЫЧНОЙ отправки, которой тут
        # не было вовсе - поэтому ключа "acc:42" в реестре нет совсем).
        dt = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
        tgs.pace_record("acc", 42, 100, at=dt, scheduled=True)
        state = json.loads(tgs.PACE_STATE_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("acc:42", state)
        scheduled = state["scheduled:acc:42"]
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]["ts"], dt.timestamp())

    def test_legacy_immediate_record_format_unchanged(self):
        # Обычная (неотложенная) запись - байт в байт прежний формат: без
        # поля scheduled, ts - момент отправки, а не что-то из будущего.
        before = time.time()
        tgs.pace_record("acc", 42, 100)
        entry = json.loads(tgs.PACE_STATE_PATH.read_text(encoding="utf-8"))["acc:42"]
        self.assertNotIn("scheduled", entry)
        self.assertGreaterEqual(entry["ts"], before)

    def test_amain_scheduled_close_together_second_refused_by_pace(self):
        """Сквозная проверка через amain(): pace_guard/pace_record внутри
        реально получают at=schedule_dt и scheduled=True."""
        dt1 = datetime.now(timezone.utc) + timedelta(days=1)
        dt2 = dt1 + timedelta(seconds=5)
        with patched_send_env(tgs, tgs, FakeClient):
            args1 = send_args(send=True, schedule=dt1, no_pace_check=False)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code1 = asyncio.run(tgs.amain(args1))
            args2 = send_args(send=True, schedule=dt2, no_pace_check=False)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code2 = asyncio.run(tgs.amain(args2))
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 3)
        self.assertIn("слишком быстро", err.getvalue().lower())


class MultipleScheduledEntries(unittest.TestCase):
    """Пункт 3 задачи: pace_record заменял единственную запись, поэтому
    постановки 09:00 -> 12:00 -> 09:00 все проходили (третья не видит первую,
    затертую второй), и обычная отправка стирала единственное свидетельство
    будущей доставки. Правка хранит отложенные списком, не заменяя друг друга
    и не трогая верхнеуровневую запись обычной отправки."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(tgs, "PACE_STATE_PATH", Path(self.tmp.name) / "last-sent.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_0900_1200_0900_third_refused(self):
        dt_0900 = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
        dt_1200 = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt_0900), 0)
        tgs.pace_record("acc", 42, 300, at=dt_0900, scheduled=True)
        # Второе, далекое от первого, проходит - и не должно вытеснить первое.
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt_1200), 0)
        tgs.pace_record("acc", 42, 300, at=dt_1200, scheduled=True)
        # Третье - снова на 09:00: со старой (единственной) записью состояние
        # "помнило" бы только 12:00, и это прошло бы. Обязано отказать.
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt_0900), 3)

    def test_regular_send_does_not_erase_scheduled_entries(self):
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        tgs.pace_record("acc", 42, 300, at=dt, scheduled=True)
        tgs.pace_record("acc", 42, 50)  # обычная отправка сейчас, тот же чат
        state = json.loads(tgs.PACE_STATE_PATH.read_text(encoding="utf-8"))
        scheduled = state["scheduled:acc:42"]
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]["ts"], dt.timestamp())
        self.assertEqual(state["acc:42"]["chars"], 50)  # обычная запись тоже на месте
        self.assertNotIn("scheduled", state["acc:42"])  # список не внутри этой записи

    def test_scheduled_far_plus_immediate_now_passes(self):
        now = time.time()
        tgs.PACE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tgs.PACE_STATE_PATH.write_text(json.dumps({
            "scheduled:acc:42": [{"ts": now + 86400 * 10, "chars": 300, "required": 180}],
        }), encoding="utf-8")
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 0)

    def test_scheduled_in_three_minutes_plus_immediate_now_refused(self):
        now = time.time()
        tgs.PACE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tgs.PACE_STATE_PATH.write_text(json.dumps({
            "scheduled:acc:42": [{"ts": now + 180, "chars": 1000, "required": 200}],
        }), encoding="utf-8")
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 3)

    def test_old_writer_does_not_erase_scheduled_list(self):
        """Старая версия pace_record делает state[key] = {...} ЦЕЛИКОМ - если
        бы список отложенных лежал вложенным полем той же записи, эта замена
        стирала бы его вместе с остальным. Отдельный ключ реестра (см.
        pace_scheduled_key) старый писатель не трогает вовсе."""

        def old_writer_record_immediate(account, chat_id, chars):
            state = tgs.pace_load()
            key = tgs.pace_key(account, chat_id)
            state[key] = {
                "ts": time.time(),
                "chars": int(chars),
                "required": tgs.pace_required(int(chars)),
            }
            tgs.PACE_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

        dt_0900 = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
        dt_1200 = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt_0900), 0)
        tgs.pace_record("acc", 42, 300, at=dt_0900, scheduled=True)

        # Старый писатель делает обычную отправку между постановками.
        old_writer_record_immediate("acc", 42, 50)

        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt_1200), 0)
        tgs.pace_record("acc", 42, 300, at=dt_1200, scheduled=True)

        # Снова 09:00: если бы старый писатель стер список, состояние
        # "забыло" бы про первую запись, и это прошло бы.
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False, at=dt_0900), 3)

    def test_legacy_entry_without_scheduled_key_still_reads(self):
        tgs.PACE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tgs.PACE_STATE_PATH.write_text(json.dumps({
            "acc:42": {"ts": time.time(), "chars": 300, "required": 100},
        }), encoding="utf-8")
        self.assertEqual(tgs.pace_guard("acc", 42, skip=False), 3)

    def test_old_style_reader_still_sees_plain_ts_field(self):
        """Смоделированное "старое" чтение - функция, которая знает только
        про ts/chars/required и ни разу не заглядывает в "scheduled": она
        обязана видеть прежний ts (последней ОБЫЧНОЙ отправки) и не падать,
        даже когда рядом лежит список отложенных, записанный новым кодом."""

        def old_style_read(state: dict, key: str) -> tuple[float, int, float]:
            entry = state[key]
            return entry["ts"], entry["chars"], entry["required"]

        tgs.pace_record("acc", 42, 111)  # обычная отправка
        tgs.pace_record(
            "acc", 42, 222,
            at=datetime.now(timezone.utc) + timedelta(days=1), scheduled=True,
        )
        state = json.loads(tgs.PACE_STATE_PATH.read_text(encoding="utf-8"))
        ts, chars, required = old_style_read(state, "acc:42")
        self.assertEqual(chars, 111)  # обычная запись, не отложенная
        self.assertGreater(ts, 0)
        self.assertGreater(required, 0)

    def test_stale_scheduled_entries_are_pruned(self):
        # Доставка давно прошла и намного дальше, чем требовала ее собственная
        # пауза - такая запись уже ни с чем не конфликтует и вычищается.
        stale = time.time() - 10_000
        tgs.PACE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tgs.PACE_STATE_PATH.write_text(json.dumps({
            "scheduled:acc:42": [{"ts": stale, "chars": 10, "required": 40}],
        }), encoding="utf-8")
        tgs.pace_record(
            "acc", 42, 300,
            at=datetime.now(timezone.utc) + timedelta(days=1), scheduled=True,
        )
        scheduled = json.loads(tgs.PACE_STATE_PATH.read_text(encoding="utf-8"))["scheduled:acc:42"]
        self.assertEqual(len(scheduled), 1)
        self.assertGreater(scheduled[0]["ts"], time.time())


class ScheduleKwargInSource(unittest.TestCase):
    """AST-проверка по образцу SilentFlag (test_telegram_config.py): schedule
    обязан быть ЛИТЕРАЛЬНЫМ keyword-аргументом в обоих вызовах отправки в
    обоих скриптах - иначе флаг тихо перестанет что-либо делать."""

    def test_reaches_both_send_calls_in_both_scripts(self):
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
                        self.assertIn("schedule", kw, f"{name}: {meth} без schedule")


if __name__ == "__main__":
    unittest.main(verbosity=2)
