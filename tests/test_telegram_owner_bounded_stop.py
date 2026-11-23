#!/usr/bin/env python3
"""Пакет 5: ограниченная штатная остановка Telegram owner (слепые тесты по спеке).

Источник ожиданий: docs/2026-11-24-spec-telegram-owner-pkg5.md, критерии P5-N и
уточнения У-1..У-5 (Tstop=20 с; ожидание ACK начатого send до 5 с; scheduled без
завершенной verification - unknown; exit 0 при чистом завершении, ненулевой при
невыполненном обязательном cleanup; сбой best-effort heartbeat - не провал).

Только фейковые порты и временный каталог. Без Telegram и боевых credentials.
Останов подается тем же asyncio.Event, который выставляет обработчик сигнала.
Реальные интервалы коротки (долгие ожидания фейков не используются), кроме
сценариев "send без ACK": там owner вправе ждать до 5 с, потолок теста 10 с.
"""
import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_telegram_session_owner_fakes import (
    Base, FakeTelegram, import_owner, read_status, run, send_request,
    write_package,
)
from test_telegram_session_owner_mirror import rec

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
TEMPLATE = TESTS_DIR.parent / "templates" / "telegram-session-owner.service"


class Paged(FakeTelegram):
    """history честно режет страницы по limit (базовый фейк отдает все сразу)."""

    async def history(self, peer, *, after_id, limit):
        self._log("history", peer, after_id, limit)
        return [m for m in self.history_data if m["id"] > after_id][:limit]


def hang(tg, name, entered):
    """Метод адаптера, который после входа никогда не возвращается."""
    async def never(*args, **kwargs):
        tg._log(name)
        entered.set()
        await asyncio.Future()
    setattr(tg, name, never)


def after(entered, stop):
    async def trigger():
        await asyncio.wait_for(entered.wait(), 5)
        stop.set()
    return trigger


class StopCase(Base):
    def setUp(self):
        super().setUp()
        self.mod = import_owner()

    # --- обвязка -----------------------------------------------------------
    def owner(self, tg=None, **kw):
        return self.env.make_owner("a", tg=tg, **kw)

    def serve(self, owner, stop, trigger=None, limit=3.0):
        """_serve до выхода; (код, реальные секунды от stop до возврата)."""
        async def main():
            task = asyncio.ensure_future(self.mod._serve(owner, stop, Mock()))
            if trigger is not None:
                await trigger()
            began = time.monotonic()
            try:
                code = await asyncio.wait_for(asyncio.shield(task), limit)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                self.fail(f"serve не завершился за {limit} с после stop")
            elapsed = time.monotonic() - began
            await asyncio.sleep(0.05)
            left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            self.assertEqual(left, [], "после выхода остались фоновые задачи")
            return code, elapsed
        return run(main())

    def request(self, **over):
        req = send_request("a", **over)
        req["options"]["no_pace_check"] = True
        write_package(self.env.root("a"), req)
        return req

    def status(self, req):
        return read_status(self.env.root("a"), req["request_id"])

    def heartbeat_state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    def restart(self):
        """Новый owner на том же спуле: start + один step. Без Telegram-сети."""
        rep = self.env.make_owner("a")
        self.assertEqual(run(rep.start())["code"], "READY")
        self.addCleanup(lambda: run(rep.close()))
        run(rep.step())
        return rep

    def assert_locks_free(self):
        rep = self.env.make_owner("a")
        try:
            self.assertEqual(run(rep.start())["code"], "READY")
        finally:
            run(rep.close())


class StartAndStop(StopCase):
    def test_p5_2_stop_before_serve_never_connects(self):
        owner = self.owner()
        stop = asyncio.Event()
        stop.set()
        code, _ = self.serve(owner, stop)
        self.assertEqual(code, 0)
        self.assertNotIn("connect", owner.fake.tg.names())
        self.assert_locks_free()

    def test_p5_2_stop_interrupts_hung_connect(self):
        owner = self.owner()
        entered, stop = asyncio.Event(), asyncio.Event()
        hang(owner.fake.tg, "connect", entered)
        code, _ = self.serve(owner, stop, after(entered, stop))
        self.assertEqual(code, 0)
        names = owner.fake.tg.names()
        self.assertNotIn("events", names)
        self.assertNotIn("history", names)
        self.assertIn("disconnect", names)
        self.assertNotEqual(self.heartbeat_state() if (self.env.root("a") / "heartbeat.json").exists() else "none", "ready")
        self.assert_locks_free()

    def test_p5_2_stop_interrupts_hung_startup_history(self):
        tg = Paged()
        tg.history_data = [rec(i) for i in range(1, 51)]
        owner = self.owner(tg, destinations=[self.env.destination()])
        entered, stop = asyncio.Event(), asyncio.Event()
        hang(tg, "history", entered)
        code, _ = self.serve(owner, stop, after(entered, stop))
        self.assertEqual(code, 0)
        self.assertNotIn("recent", tg.names())
        self.assertNotIn("events", tg.names())
        self.assert_locks_free()


class Catchup(StopCase):
    def test_p5_3_stop_between_pages_stops_catchup_and_resumes_from_disk(self):
        tg = Paged()
        tg.history_data = [rec(i) for i in range(1, 251)]
        owner = self.owner(tg, destinations=[self.env.destination()])
        stop = asyncio.Event()
        tg.hooks["history"] = lambda: stop.set()  # stop приходит на первой странице
        code, _ = self.serve(owner, stop)
        self.assertEqual(code, 0)
        self.assertEqual(tg.names().count("history"), 1, "вторая страница после stop")
        self.assertNotIn("recent", tg.names())
        path = self.env.projects / "chat1" / "result.json"
        ids = ([m["id"] for m in json.loads(path.read_bytes())["messages"]]
               if path.exists() else [])
        self.assertEqual(ids, list(range(1, len(ids) + 1)), "на диске не префикс страниц")
        self.assertLessEqual(len(ids), 100)
        # новый owner продолжает с устойчивой границы и доводит зеркало до конца
        tg2 = Paged()
        tg2.history_data = list(tg.history_data)
        rep = self.env.make_owner("a", tg=tg2, destinations=[self.env.destination()])
        self.addCleanup(lambda: run(rep.close()))
        self.assertEqual(run(rep.start())["code"], "READY")
        first = [c for c in tg2.calls if c[0] == "history"][0]
        self.assertEqual(first[2], len(ids))
        self.assertEqual([m["id"] for m in json.loads(path.read_bytes())["messages"]],
                         list(range(1, 251)))

    def test_p5_3_hung_recent_is_interrupted(self):
        tg = Paged()
        tg.history_data = [rec(i) for i in range(1, 31)]
        owner = self.owner(tg, destinations=[self.env.destination()])
        entered, stop = asyncio.Event(), asyncio.Event()
        hang(tg, "recent", entered)
        code, _ = self.serve(owner, stop, after(entered, stop))
        self.assertEqual(code, 0)
        self.assertNotIn("events", tg.names())
        self.assert_locks_free()


class NoNewWork(StopCase):
    def test_p5_4_stop_interrupts_hung_events_without_new_send(self):
        owner = self.owner()
        req = self.request(now=owner.fake.clock.utc())
        entered, stop = asyncio.Event(), asyncio.Event()
        hang(owner.fake.tg, "events", entered)
        code, _ = self.serve(owner, stop, after(entered, stop))
        self.assertEqual(code, 0)
        self.assertEqual(owner.fake.tg.sends(), [])
        st = self.status(req)
        self.assertTrue(st is None or st["state"] == "queued", st)
        self.assert_locks_free()

    def test_p5_6_stop_during_events_gives_no_send_after_release(self):
        """Регрессия Q2: stop в events, после возврата events send не начинается."""
        owner = self.owner()
        req = self.request(now=owner.fake.clock.utc())
        tg, entered, stop, release = owner.fake.tg, asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def events():
            tg._log("events")
            entered.set()
            await release.wait()
            return []
        tg.events = events

        async def trigger():
            await asyncio.wait_for(entered.wait(), 5)
            stop.set()
            release.set()
        code, _ = self.serve(owner, stop, trigger)
        self.assertEqual(code, 0)
        self.assertEqual(tg.sends(), [])
        st = self.status(req)
        self.assertTrue(st is None or st["state"] == "queued", st)

    def test_p5_6_stop_during_resolve_gives_no_send_and_request_survives(self):
        owner = self.owner()
        req = self.request(now=owner.fake.clock.utc())
        stop = asyncio.Event()
        owner.fake.tg.hooks["resolve"] = lambda: stop.set()
        code, _ = self.serve(owner, stop)
        self.assertEqual(code, 0)
        self.assertEqual(owner.fake.tg.sends(), [])
        st = self.status(req)
        self.assertTrue(st is None or st["state"] == "queued", st)
        # прерванная подготовка повторяема: тот же UUID уходит ровно один раз
        rep = self.restart()
        self.assertEqual(self.status(req)["state"], "sent")
        self.assertEqual(len(rep.fake.tg.sends()), 1)

    def test_p5_5_stop_interrupts_hung_resolve_keeps_request_retryable(self):
        owner = self.owner()
        req = self.request(now=owner.fake.clock.utc())
        entered, stop = asyncio.Event(), asyncio.Event()
        hang(owner.fake.tg, "resolve", entered)
        code, _ = self.serve(owner, stop, after(entered, stop))
        self.assertEqual(code, 0)
        self.assertEqual(owner.fake.tg.sends(), [])
        st = self.status(req)
        self.assertTrue(st is None or st["state"] == "queued", st)  # не rejected/unknown
        rep = self.restart()
        self.assertEqual(self.status(req)["state"], "sent")
        self.assertEqual(len(rep.fake.tg.sends()), 1)


class SendInFlight(StopCase):
    def test_p5_8_hung_send_without_ack_becomes_unknown_within_bound(self):
        owner = self.owner()
        req = self.request(now=owner.fake.clock.utc())
        entered, stop = asyncio.Event(), asyncio.Event()
        hang(owner.fake.tg, "send", entered)
        code, elapsed = self.serve(owner, stop, after(entered, stop), limit=10.0)
        self.assertLess(elapsed, 8.0, "ожидание ACK превысило 5 с + запас")
        self.assertEqual(code, 0)
        st = self.status(req)
        self.assertEqual((st["state"], st["code"]), ("unknown", "SEND_UNCERTAIN"))
        self.assertEqual(len(owner.fake.tg.sends()), 1)
        rep = self.restart()
        self.assertEqual(rep.fake.tg.sends(), [], "повторная отправка после restart")
        self.assertEqual(self.status(req)["state"], "unknown")
        self.assert_locks_free()

    def test_p5_8_ack_arriving_within_grace_after_stop_is_recorded_as_sent(self):
        """У-2: начатый send получает ожидание ACK, а не немедленную отмену."""
        owner = self.owner()
        req = self.request(now=owner.fake.clock.utc())
        tg, entered, stop = owner.fake.tg, asyncio.Event(), asyncio.Event()

        async def slow_send(request, peer, uploads):
            tg._log("send", request, peer, list(uploads))
            entered.set()
            await asyncio.sleep(0.3)
            return {"message_ids": [777], "scheduled_ids": [], "at": None}
        tg.send = slow_send
        code, _ = self.serve(owner, stop, after(entered, stop), limit=10.0)
        self.assertEqual(code, 0)
        st = self.status(req)
        self.assertEqual(st["state"], "sent", st)
        self.assertEqual(st["result"]["message_ids"], [777])
        self.assertEqual(len(tg.sends()), 1)

    def test_p5_9_ack_simultaneous_with_stop_is_not_downgraded(self):
        owner = self.owner()
        req = self.request(now=owner.fake.clock.utc())
        stop = asyncio.Event()
        owner.fake.tg.hooks["send"] = lambda: stop.set()
        owner.fake.tg.send_results = [{"message_ids": [555], "scheduled_ids": [], "at": None}]
        code, _ = self.serve(owner, stop)
        self.assertEqual(code, 0)
        st = self.status(req)
        self.assertEqual(st["state"], "sent", st)
        self.assertEqual(st["result"]["message_ids"], [555])
        self.assertEqual(len(owner.fake.tg.sends()), 1)

    def test_p5_7_unknown_write_failure_leaves_sending_and_restart_never_resends(self):
        owner = self.owner()
        req = self.request(now=owner.fake.clock.utc())
        entered, stop = asyncio.Event(), asyncio.Event()
        hang(owner.fake.tg, "send", entered)
        store = owner.fake.store
        original = store.atomic_write

        def write(path, data):
            if Path(path).parent.name == "status":
                try:
                    state = json.loads(data).get("state")
                except ValueError:
                    state = None
                if state in ("unknown", "rejected", "queued"):
                    raise OSError("disk")
            original(path, data)

        with patch.object(store, "atomic_write", side_effect=write):
            self.serve(owner, stop, after(entered, stop), limit=10.0)
        self.assertEqual(self.status(req)["state"], "sending")
        rep = self.restart()
        st = self.status(req)
        self.assertEqual((st["state"], st["code"]), ("unknown", "SEND_UNCERTAIN"))
        self.assertEqual(rep.fake.tg.sends(), [])


class Scheduled(StopCase):
    def scheduled_request(self, owner):
        at = owner.fake.clock.utc() + 600
        req = self.request(now=owner.fake.clock.utc(),
                           schedule={"at": at, "timezone": None, "exact_minute": False,
                                     "remind": False})
        owner.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [900], "at": at}]
        return req, at

    def test_p5_10_stop_after_ack_before_verification_is_unknown_with_ids(self):
        owner = self.owner()
        req, at = self.scheduled_request(owner)
        stop = asyncio.Event()
        owner.fake.tg.hooks["send"] = lambda: stop.set()
        code, _ = self.serve(owner, stop, limit=10.0)
        self.assertEqual(code, 0)
        self.assertNotIn("scheduled", owner.fake.tg.names(), "verification начата после stop")
        st = self.status(req)
        self.assertEqual(st["state"], "unknown", st)
        self.assertEqual(st["result"]["scheduled_ids"], [900])
        self.assertEqual(len(owner.fake.tg.sends()), 1)
        rep = self.restart()
        self.assertEqual(rep.fake.tg.sends(), [])

    def test_p5_10_stop_during_verification_is_unknown_with_ids_and_no_resend(self):
        owner = self.owner()
        req, at = self.scheduled_request(owner)
        entered, stop = asyncio.Event(), asyncio.Event()
        hang(owner.fake.tg, "scheduled", entered)
        code, elapsed = self.serve(owner, stop, after(entered, stop), limit=10.0)
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 8.0)
        st = self.status(req)
        self.assertEqual(st["state"], "unknown", st)
        self.assertEqual(st["result"]["scheduled_ids"], [900])
        self.assertEqual(len(owner.fake.tg.sends()), 1)
        rep = self.restart()
        self.assertEqual(rep.fake.tg.sends(), [])


class CleanupAndExit(StopCase):
    def test_p5_13_failing_disconnect_is_nonzero_and_locks_released(self):
        owner = self.owner()
        stop = asyncio.Event()
        ready = asyncio.Event()
        owner.fake.tg.hooks["events"] = lambda: ready.set()

        async def trigger():
            await asyncio.wait_for(ready.wait(), 5)
            owner.fake.tg.raises["disconnect"] = OSError("disconnect")
            stop.set()
        with self.assertLogs("telegram_session_owner", level="ERROR"):
            code, _ = self.serve(owner, stop, trigger)
        self.assertNotEqual(code, 0, "невыполненный cleanup выдан за чистый выход")
        self.assert_locks_free()

    def test_p5_13_hung_disconnect_is_bounded_nonzero_and_locks_released(self):
        owner = self.owner()
        stop, ready, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        owner.fake.tg.hooks["events"] = lambda: ready.set()

        async def trigger():
            await asyncio.wait_for(ready.wait(), 5)
            hang(owner.fake.tg, "disconnect", entered)
            stop.set()
        code, elapsed = self.serve(owner, stop, trigger, limit=10.0)
        self.assertLess(elapsed, 8.0)
        self.assertNotEqual(code, 0)
        self.assert_locks_free()

    def test_p5_13_heartbeat_failure_on_stopping_is_not_cleanup_failure(self):
        owner = self.owner()
        stop, ready = asyncio.Event(), asyncio.Event()
        store = owner.fake.store
        original = store.atomic_write
        owner.fake.tg.hooks["events"] = lambda: ready.set()

        def write(path, data):
            if Path(path).name == "heartbeat.json":
                try:
                    if json.loads(data).get("state") == "stopping":
                        raise OSError("disk")
                except ValueError:
                    pass
            original(path, data)

        async def trigger():
            await asyncio.wait_for(ready.wait(), 5)
            stop.set()
        with patch.object(store, "atomic_write", side_effect=write):
            with self.assertLogs("telegram_session_owner", level="WARNING"):
                code, _ = self.serve(owner, stop, trigger)
        self.assertEqual(code, 0)
        self.assertIn("disconnect", owner.fake.tg.names())
        self.assert_locks_free()


class Waits(StopCase):
    def test_p5_15_stop_interrupts_idle_wait(self):
        owner = self.owner()
        stop = asyncio.Event()

        async def trigger():
            await asyncio.sleep(0.3)
            stop.set()
        code, elapsed = self.serve(owner, stop, trigger)
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2.0)

    def test_p5_15_stop_interrupts_pause_after_failed_start(self):
        """После неудачного start owner ждет паузу (60 с); stop не ждет ее."""
        owner = self.owner()
        owner.fake.tg.raises["connect"] = import_owner().FloodWait(600)
        stop = asyncio.Event()

        async def trigger():
            while not owner.fake.tg.calls:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)
            stop.set()
        code, elapsed = self.serve(owner, stop, trigger, limit=5.0)
        self.assertLess(elapsed, 3.0)
        self.assertNotIn("send", owner.fake.tg.names())
        self.assert_locks_free()


HARNESS = r"""
import asyncio, json, os, signal, sys, tempfile
from pathlib import Path
from unittest.mock import Mock
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from test_telegram_session_owner_fakes import Env, FakeTelegram, import_owner

mode = sys.argv[3]

async def main():
    mod = import_owner()
    base = Path(tempfile.mkdtemp())
    os.chmod(base, 0o700)
    env = Env(base)
    tg = FakeTelegram("a")
    async def never(*a, **k):
        tg._log(mode)
        print("ENTERED", flush=True)
        await asyncio.Future()
    setattr(tg, mode, never)
    owner = env.make_owner("a", tg=tg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, stop.set)
    code = await mod._serve(owner, stop, Mock())
    await asyncio.sleep(0.05)
    left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    print("RESULT " + json.dumps({"code": code, "left": len(left)}), flush=True)
    return code

sys.exit(asyncio.run(main()))
"""


class RealSignals(unittest.TestCase):
    """P5-16: настоящий процесс получает SIGTERM/SIGINT и выходит сам."""

    def run_signalled(self, mode, signals):
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.Popen(
                [sys.executable, "-c", HARNESS, str(TESTS_DIR), str(SCRIPTS_DIR), mode],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
            try:
                deadline = time.monotonic() + 15
                seen = ""
                while "ENTERED" not in seen:
                    left = deadline - time.monotonic()
                    self.assertGreater(left, 0, f"harness не дошел до {mode}")
                    ready, _, _ = select.select([proc.stdout], [], [], left)
                    if not ready:
                        continue
                    line = proc.stdout.readline()
                    if not line:
                        self.fail("harness завершился до входа: " + proc.stderr.read()[-500:])
                    seen += line
                sent = time.monotonic()
                for i, sig in enumerate(signals):
                    if i:
                        time.sleep(0.1)
                    proc.send_signal(sig)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.fail(f"процесс не вышел за 10 с после {signals}")
                elapsed = time.monotonic() - sent
                out = seen + proc.stdout.read()
                result = [json.loads(l[7:]) for l in out.splitlines() if l.startswith("RESULT ")]
                self.assertTrue(result, out + proc.stderr.read()[-500:])
                return proc.returncode, result[0], elapsed
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_p5_16_sigterm_during_hung_connect_exits_zero_without_tasks(self):
        rc, result, elapsed = self.run_signalled("connect", [signal.SIGTERM])
        self.assertEqual((rc, result["code"], result["left"]), (0, 0, 0))
        self.assertLess(elapsed, 8.0)

    def test_p5_16_sigint_during_hung_events_exits_zero_without_tasks(self):
        rc, result, elapsed = self.run_signalled("events", [signal.SIGINT])
        self.assertEqual((rc, result["code"], result["left"]), (0, 0, 0))
        self.assertLess(elapsed, 8.0)

    def test_p5_16_repeated_signal_is_idempotent(self):
        rc, result, _ = self.run_signalled("events", [signal.SIGTERM, signal.SIGTERM])
        self.assertEqual((rc, result["code"], result["left"]), (0, 0, 0))


class UnitCompat(unittest.TestCase):
    def test_p5_19_unit_keeps_stop_timeout_and_restart_policy(self):
        text = TEMPLATE.read_text()
        for line in ("TimeoutStopSec=30", "Restart=on-failure", "RestartSec=15",
                     "StartLimitIntervalSec=300", "StartLimitBurst=5"):
            self.assertIn(line, text.replace("30s", "30") if "TimeoutStopSec" in line else text)
        self.assertNotIn("WatchdogSec", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
