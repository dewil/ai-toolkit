#!/usr/bin/env python3
"""RED: регрессии владельца Telegram-сессии (решение ведущего 22.11).

1. Частота переподключения: первые 2 минуты обрыва - попытка каждые 15 с, далее
   пауза растет 30..300 с; remind при обрыве 50 с отправляется, а не отклоняется.
2. Догон зеркала читает историю страницами (limit порта history) и фиксирует
   курсор после каждой страницы: таймаут страницы не теряет прочитанное.

Только публичный шов (порты Telegram/Clock/Store из фейков), без внутренних имен.
Бюджет history подменяется через RPC_TIMEOUTS (как в test_telegram_owner_timeouts).

Запуск: python3 tests/test_telegram_owner_regressions.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeRNG, FakeTelegram, import_owner, read_status, run, send_request, write_package,
)


class DownTelegram(FakeTelegram):
    """Обрыв связи: пока clock.utc() < down_until, любой вызов (кроме disconnect) -> Disconnected."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.clock = None
        self.down_until = 0.0
        self.attempts: list[float] = []  # моменты connect во время обрыва
        self.drop_on = None  # имя вызова, на котором связь обрывается (один раз)
        self.drop_for = 0.0

    def _is_down(self):
        return self.clock is not None and self.clock.utc() < self.down_until

    def _log(self, *a):
        self.calls.append(a)
        if self.drop_on == a[0]:
            self.drop_on = None
            self.down_until = self.clock.utc() + self.drop_for
        if a[0] == "connect" and self._is_down():
            self.attempts.append(self.clock.utc())
        if a[0] != "disconnect" and self._is_down():
            raise import_owner().Disconnected()
        return super()._log(*a)

    async def send(self, request, peer, uploads):
        self.send_results = [{"message_ids": [], "scheduled_ids": [900],
                              "at": int(request["schedule"]["at"])}]
        ack = await super().send(request, peer, uploads)
        self.scheduled_data = [{"id": 900, "at": ack["at"], "text": request["text"]}]
        return ack


class Reconnect(Base):
    def make(self, **kw):
        tg = DownTelegram("a")
        o = self.env.make_owner("a", tg=tg, **kw)
        tg.clock = o.fake.clock
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        return o, tg

    def test_remind_survives_50s_outage(self):
        o, tg = self.make(rng=FakeRNG(randint=1))  # срок now+181, минимальный запас
        clock, root = o.fake.clock, self.env.root("a")
        # связь рвется на resolve заявки: срок remind уже назначен, отправка идет после восстановления
        tg.drop_on, tg.drop_for = "resolve", 50
        req = send_request("a", peer="me", text="Напомнить", now=clock.utc(),
                           schedule={"at": None, "timezone": None, "exact_minute": False, "remind": True})
        write_package(root, req)
        st = None
        for _ in range(1500):  # до 150 с моделируемого времени, шаг 0,1 с
            run(o.step())
            clock.advance(0.1)
            st = read_status(root, req["request_id"])
            if st and st["state"] not in ("queued", "sending"):
                break
        self.assertIsNotNone(st)
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("scheduled", "SCHEDULED", 0), st)

    def test_reconnect_cadence_over_600s_outage(self):
        o, tg = self.make()
        clock = o.fake.clock
        t0 = clock.utc()
        tg.down_until = t0 + 600
        # событие -> Disconnected -> degraded, дальше попытки по расписанию
        for _ in range(1400):
            run(o.step())
            clock.advance(0.5)
        at = [t - t0 for t in tg.attempts]
        early = [t for t in at if t <= 120.5]
        self.assertGreaterEqual(len(early), 7, f"попытки в первые 120 с: {at}")
        late = [t for t in at if t > 120.5]
        gaps = [b - a for a, b in zip([max(early)] + late, late)] if early else []
        self.assertTrue(gaps, f"нет попыток после 120 с: {at}")
        self.assertTrue(all(g >= 29.5 for g in gaps), f"долбежка после 120 с: {gaps}")
        self.assertEqual(gaps, sorted(gaps), f"пауза не должна уменьшаться: {gaps}")
        self.assertGreaterEqual(max(gaps), 239.5, f"пауза не выросла до 240-300: {gaps}")
        self.assertLessEqual(max(gaps), 300.5, gaps)


def rec(i):
    return {"id": i, "type": "message", "date": "2026-11-19T10:00:00",
            "date_unixtime": str(1797674400 + i), "from": "Иван", "from_id": "user1",
            "text": f"msg {i}", "text_entities": []}


class PagedTelegram(FakeTelegram):
    """history уважает limit (порт: after_id, limit), стоит реального времени
    PER_MESSAGE на сообщение; ведет журнал выданных страниц."""

    PER_MESSAGE = 0.0005

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pages: list[tuple[int, list[int]]] = []  # (after_id, выданные id)
        self.fail_on_call: int | None = None  # номер вызова history (с 1), падающего Disconnected
        self._n = 0

    async def history(self, peer, *, after_id, limit):
        self._n += 1
        self._log("history", peer, after_id, limit)
        if self.fail_on_call == self._n:
            raise import_owner().Disconnected()
        batch = [m for m in self.history_data if m["id"] > after_id][:limit]
        await asyncio.sleep(self.PER_MESSAGE * len(batch))
        self.pages.append((after_id, [m["id"] for m in batch]))
        return batch


class PagedCatchup(Base):
    BUDGET = 0.8      # бюджет history (с), подменяет 240 с
    MAX_PAGE = 1000   # страница должна укладываться в бюджет: 1000 * 0.0005 = 0.5 с
    TOTAL = 6000      # вся история: 3 с > бюджета

    def setUp(self):
        super().setUp()
        self.mod = import_owner()
        p = patch.dict(self.mod.RPC_TIMEOUTS, {"history": self.BUDGET})
        p.start()
        self.addCleanup(p.stop)
        self.mirror = self.env.projects / "chat1" / "result.json"

    def mirror_ids(self):
        if not self.mirror.exists():
            return []
        return [m["id"] for m in json.loads(self.mirror.read_text(encoding="utf-8"))["messages"]]

    def new_owner(self, tg, clock=None):
        kw = {"clock": clock} if clock else {}
        return self.env.make_owner("a", tg=tg, destinations=[self.env.destination("d1", 1001, "chat1")], **kw)

    def start_until_ready(self, tg, attempts=6):
        """Рестарты как у systemd: падение start -> новый экземпляр с теми же файлами."""
        for n in range(attempts):
            o = self.new_owner(tg)
            r = run(o.start())
            if r["code"] == "READY":
                self.addCleanup(lambda o=o: run(o.close()))
                return o, n
        return None, attempts

    def assert_paged(self, tg):
        limits = [c[3] for c in tg.calls if c[0] == "history"]
        self.assertTrue(limits)
        self.assertTrue(all(isinstance(x, int) and 0 < x <= self.MAX_PAGE for x in limits),
                        f"история читается страницами не больше {self.MAX_PAGE}: {limits[:5]}")

    def assert_no_redownload(self, tg):
        # каждая выданная страница (по after_id) не более 2 раз: допустим повтор одной незавершенной
        seen: dict[int, int] = {}
        for after, ids in tg.pages:
            if ids:
                seen[after] = seen.get(after, 0) + 1
        repeated = {a: n for a, n in seen.items() if n > 1}
        self.assertLessEqual(len(repeated), 1, f"повторно скачаны страницы: {repeated}")
        self.assertTrue(all(n <= 2 for n in repeated.values()), repeated)

    def test_long_history_reaches_ready_and_mirrors_everything(self):
        tg = PagedTelegram("a")
        tg.history_data = [rec(i) for i in range(1, self.TOTAL + 1)]
        o, n = self.start_until_ready(tg)
        self.assertIsNotNone(o, "владелец не дошел до ready: история не продвигается страницами")
        self.assertEqual(self.mirror_ids(), list(range(1, self.TOTAL + 1)))
        self.assert_paged(tg)
        self.assert_no_redownload(tg)

    def test_failed_page_keeps_cursor_and_resumes_after_it(self):
        tg = PagedTelegram("a")
        tg.history_data = [rec(i) for i in range(1, 2501)]
        tg.fail_on_call = 3  # третья страница падает
        o = self.new_owner(tg)
        run(o.start())  # исход (READY/недоступен) не важен: проверяем зеркало и курсор
        try:
            run(o.close())  # снять lock, если start вернул READY
        except Exception:  # noqa: BLE001 - после неудачного start закрывать нечего
            pass
        got = [i for _, ids in tg.pages for i in ids]
        self.assertTrue(got, "ни одной страницы не прочитано до сбоя")
        mirrored = set(self.mirror_ids())
        self.assertTrue(set(got) <= mirrored,
                        f"курсор не зафиксирован после страницы: прочитано {len(got)}, в зеркале {len(mirrored)}")
        # второй старт продолжает с зафиксированного, не с нуля
        before = len(tg.pages)
        o2, _ = self.start_until_ready(tg)
        self.assertIsNotNone(o2)
        resumed = [a for a, _ in tg.pages[before:]]
        self.assertTrue(resumed and min(resumed) >= max(got) - self.MAX_PAGE,
                        f"продолжение не с курсора: after_id={resumed[:3]}, max прочитанного={max(got)}")
        self.assertEqual(self.mirror_ids(), list(range(1, 2501)))
        self.assert_no_redownload(tg)

    def test_catchup_after_reconnect_is_paged(self):
        tg = PagedTelegram("a")
        tg.history_data = [rec(i) for i in range(1, 11)]
        o = self.new_owner(tg)
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        tg.history_data += [rec(i) for i in range(11, self.TOTAL + 1)]
        tg.raises["events"] = self.mod.Disconnected()
        clock = o.fake.clock
        ready = False
        for _ in range(60):
            run(o.step())
            clock.advance(15)
            hb = json.loads((self.env.root("a") / "heartbeat.json").read_bytes())
            if hb["state"] == "ready" and len(self.mirror_ids()) == self.TOTAL:
                ready = True
                break
        self.assertTrue(ready, f"после переподключения зеркало не догнано: {len(self.mirror_ids())}")
        self.assert_paged(tg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
