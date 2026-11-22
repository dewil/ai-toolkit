#!/usr/bin/env python3
"""RED: отсчет обрыва и пауза переподключения сбрасываются только после первого
целиком успешного рабочего шага в ready, а не в момент перехода в ready
(решение ведущего 22.11).

1. Полуживое соединение (connect и догон проходят, следующий вызов шага всегда
   падает Disconnected): за 600 с не больше 12 connect (рост пауз до 300 с).
2. Короткий обрыв 50 с и стабильная работа: remind отправлен (scheduled); после
   10 минут стабильной работы новый обрыв снова начинается с паузы 15 с.

Только публичный шов (порты из фейков).
Запуск: python3 tests/test_telegram_owner_reconnect_reset.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeRNG, FakeTelegram, import_owner, read_status, run, send_request, write_package,
)
from test_telegram_owner_regressions import DownTelegram  # noqa: E402

MAX_CONNECTS_600S = 12


class HalfAlive(FakeTelegram):
    """После half_alive=True вызовы из fail_calls всегда падают Disconnected,
    остальное (connect, догон, disconnect) проходит."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.clock = None
        self.half_alive = False
        self.fail_calls: set[str] = set()

    def _log(self, *a):
        if self.half_alive and a[0] in self.fail_calls:
            self.calls.append(a)
            raise import_owner().Disconnected()
        return super()._log(*a)


class ResetOnlyAfterWork(Base):
    def make(self, tg, **kw):
        o = self.env.make_owner("a", tg=tg, **kw)
        tg.clock = o.fake.clock
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        return o

    def connects_after(self, tg, mark):
        return sum(1 for c in tg.calls[mark:] if c[0] == "connect")

    def test_half_alive_events_fail_limits_connects(self):
        tg = HalfAlive("a")
        o = self.make(tg)
        clock = o.fake.clock
        tg.half_alive, tg.fail_calls = True, {"events"}
        mark = len(tg.calls)
        for _ in range(600):
            run(o.step())
            clock.advance(1)
        n = self.connects_after(tg, mark)
        self.assertLessEqual(n, MAX_CONNECTS_600S,
                             f"connect за 600 с: {n}; пауза не растет при полуживом соединении")
        self.assertGreaterEqual(n, 3, f"переподключения вообще не идут: {n}")

    def test_half_alive_resolve_fail_with_queued_send_limits_connects(self):
        tg = HalfAlive("a")
        o = self.make(tg)
        clock, root = o.fake.clock, self.env.root("a")
        tg.half_alive, tg.fail_calls = True, {"resolve"}
        req = send_request("a", peer="me", text="Привет", now=clock.utc())
        write_package(root, req)
        mark = len(tg.calls)
        for _ in range(600):
            run(o.step())
            clock.advance(1)
        n = self.connects_after(tg, mark)
        self.assertLessEqual(n, MAX_CONNECTS_600S,
                             f"connect за 600 с: {n}; пауза не растет при падении resolve")
        self.assertGreaterEqual(n, 3, f"переподключения вообще не идут: {n}")

    def test_remind_survives_outage_then_next_outage_starts_at_15s(self):
        tg = DownTelegram("a")
        o = self.make(tg, rng=FakeRNG(randint=1))
        clock, root = o.fake.clock, self.env.root("a")
        tg.drop_on, tg.drop_for = "resolve", 50
        req = send_request("a", peer="me", text="Напомнить", now=clock.utc(),
                           schedule={"at": None, "timezone": None, "exact_minute": False, "remind": True})
        write_package(root, req)
        st = None
        for _ in range(1500):
            run(o.step())
            clock.advance(0.1)
            st = read_status(root, req["request_id"])
            if st and st["state"] not in ("queued", "sending"):
                break
        self.assertIsNotNone(st)
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("scheduled", "SCHEDULED", 0), st)
        # 10 минут стабильной работы
        for _ in range(600):
            run(o.step())
            clock.advance(1)
        # новый обрыв: первые две попытки разделены паузой 15 с
        tg.attempts.clear()
        t0 = clock.utc()
        tg.down_until = t0 + 120
        for _ in range(400):
            run(o.step())
            clock.advance(0.5)
        at = [t - t0 for t in tg.attempts]
        self.assertGreaterEqual(len(at), 2, f"попытки: {at}")
        self.assertLessEqual(at[1] - at[0], 16.0, f"первая пауза после стабильной работы: {at[:3]}")
        self.assertGreaterEqual(at[1] - at[0], 14.5, f"первая пауза после стабильной работы: {at[:3]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
