#!/usr/bin/env python3
"""RED пакета 4: orphan-события зеркала не запускают догон на каждом шаге и не
лишают исправную заявку отправки (спека docs/2026-11-24-spec-telegram-owner-pkg4.md,
критерии P4-1..P4-14 с учетом У-1..У-4).

Только офлайн: FakeTelegram, FakeClock, настоящий Store во временном каталоге.
Запуск: python3 tests/test_telegram_owner_orphan_catchup.py
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeClock, FakeTelegram, NOW, import_client, import_owner, read_status, refresh_request, run, send_request, write_package,
)
from test_telegram_session_owner_mirror import ev, ids_of, load, rec, topic_rec, write_mirror  # noqa: E402

DELTA = 5.0   # У-1: интервал фонового догона на аккаунт
TICK = 0.1
EPS = 1e-6
OVERFLOW = {"kind": "overflow", "peer": None, "record": None, "ids": [], "revision": [NOW, 1]}


def orphan(n, same_revision=False):
    """Неполный topic edit без корня 77: поле title, revision растет либо постоянна."""
    return ev("topic", {"id": 77, "title": f"T{n}"}, rev=(NOW, 1 if same_revision else n + 1))


class OrphanBase(Base):
    def setUp(self):
        super().setUp()
        self.path = self.env.projects / "chat1" / "result.json"

    def start(self, destinations=None):
        dests = destinations or [self.env.destination("d1", 1001, "chat1")]
        o = self.env.make_owner("a", destinations=dests)
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        return o

    @staticmethod
    def sstate(root, rid):
        st = read_status(root, rid)
        return st["state"] if st else None

    def state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    def spy_history(self, o):
        times = []
        o.fake.tg.hooks["history"] = lambda: times.append(o.fake.clock.monotonic())
        return times

    def queue_send(self, o, peer):
        req = send_request("a", peer=peer, text="Привет", now=o.fake.clock.utc())
        req["options"]["no_pace_check"] = True
        write_package(self.env.root("a"), req)
        return req["request_id"]

    def stream(self, o, ticks, *, same_revision=False, on_tick=None):
        """Каждый тик - новый orphan; возвращает состояния heartbeat после шагов."""
        states = []
        for n in range(ticks):
            o.fake.tg.event_batches = [[orphan(n, same_revision)]]
            run(o.step())
            states.append(self.state())
            if on_tick:
                on_tick(n)
            o.fake.clock.advance(TICK)
        return states

    def assert_spacing(self, times):
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, DELTA - EPS, f"циклы ближе {DELTA} с: {times}")


class OrphanStreamBounded(OrphanBase):
    def check_stream(self, same_revision):
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        times = self.spy_history(o)
        mark = len(tg.calls)
        states = self.stream(o, 600, same_revision=same_revision)
        self.assertLessEqual(len(times), 1 + int(60 / DELTA), f"догонов за 60 с: {len(times)}")
        self.assert_spacing(times)
        self.assertNotIn("disconnect", tg.names()[mark:])
        self.assertNotIn("connect", tg.names()[mark:])
        self.assertEqual(set(states), {"ready"}, "heartbeat не ready между сверками (У-3)")

    def test_60s_orphan_stream_rising_revision(self):  # P4-1
        self.check_stream(False)

    def test_60s_orphan_stream_same_revision(self):  # P4-1
        self.check_stream(True)

    def test_interval_counts_from_attempt_start_after_failed_attempt(self):  # P4-3, У-1
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        times = self.spy_history(o)
        tg.raises["history"] = import_owner().FloodWait(1)
        self.stream(o, 120)
        self.assertGreaterEqual(len(times), 2, times)
        self.assert_spacing(times)

    def test_wall_clock_jump_does_not_release_interval(self):  # P4-3
        write_mirror(self.path, [100])
        o = self.start()
        times = self.spy_history(o)

        def jump(n):
            if n % 10 == 0:
                o.fake.clock._utc += 1000 if n % 20 == 0 else -1000
        self.stream(o, 300, on_tick=jump)
        self.assert_spacing(times)
        self.assertLessEqual(len(times), 1 + int(30 / DELTA))

    def test_mixed_orphan_and_overflow_share_one_limiter(self):  # P4-6
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        times = self.spy_history(o)
        for n in range(600):
            tg.event_batches = [[orphan(n)] if n % 2 else [dict(OVERFLOW)]]
            run(o.step())
            o.fake.clock.advance(TICK)
        self.assertLessEqual(len(times), 1 + int(60 / DELTA), times)
        self.assert_spacing(times)
        self.assertNotIn("disconnect", tg.names())
        self.assertEqual(tg.names().count("connect"), 1)


class OrphanDoesNotBlockSend(OrphanBase):
    def check_send(self, peer):
        write_mirror(self.path, [100])
        o = self.start()
        tg, root = o.fake.tg, self.env.root("a")
        rid = self.queue_send(o, peer)
        self.stream(o, 30)  # 3 с, orphan на каждом шаге
        st = read_status(root, rid)
        self.assertEqual(st and st["state"], "sent", f"{st}; calls={tg.names()}")
        self.assertEqual(len(tg.sends()), 1)

    def test_sent_to_mirrored_peer_during_orphan_stream(self):  # P4-2
        self.check_send(1001)

    def test_sent_to_me_during_orphan_stream(self):  # P4-2
        self.check_send("me")

    def test_sent_within_second_available_step_after_sync(self):  # P4-2
        write_mirror(self.path, [100])
        o = self.start()
        tg, root = o.fake.tg, self.env.root("a")
        rid = self.queue_send(o, "me")
        for n in range(3):
            tg.event_batches = [[orphan(n)]]
            run(o.step())
            o.fake.clock.advance(TICK)
        self.assertEqual(self.sstate(root, rid), "sent", tg.names())

    def test_at_most_one_send_per_step_under_orphan_stream(self):  # P4-2
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        for _ in range(2):
            self.queue_send(o, "me")
        for n in range(20):
            before = len(tg.sends())
            tg.event_batches = [[orphan(n)]]
            run(o.step())
            self.assertLessEqual(len(tg.sends()) - before, 1)
            o.fake.clock.advance(TICK)
        self.assertEqual(len(tg.sends()), 2)

    def test_orphan_of_one_peer_does_not_block_other_peer_and_me(self):  # P4-10
        write_mirror(self.path, [100])
        d2 = self.env.destination("d2", 2002, "chat2")
        write_mirror(self.env.projects / "chat2" / "result.json", [200], chat_id=2002)
        o = self.start([self.env.destination("d1", 1001, "chat1"), d2])
        tg, root = o.fake.tg, self.env.root("a")
        r2, rme = self.queue_send(o, 2002), self.queue_send(o, "me")
        self.stream(o, 30)
        self.assertEqual(self.sstate(root, r2), "sent", tg.names())
        self.assertEqual(self.sstate(root, rme), "sent", tg.names())


class SingleSignalsRecover(OrphanBase):
    def test_single_orphan_is_resolved_by_background_catchup(self):  # P4-4
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        tg.recent_data = [{"kind": "topic", "record": topic_rec(77), "revision": [NOW - 1, 0]}]
        mark = len(tg.calls)
        tg.event_batches = [[ev("topic", {"id": 77, "title": "Renamed"})]]
        for _ in range(int(2 * DELTA / TICK)):
            run(o.step())
            o.fake.clock.advance(TICK)
        self.assertIn("history", tg.names()[mark:], "одиночный orphan без догона")
        self.assertEqual(load(self.path)["topics"], [topic_rec(77, "Renamed")])
        self.assertEqual(self.state(), "ready")
        self.assertNotIn("disconnect", tg.names()[mark:])

    def test_single_orphan_without_root_does_not_spin_recovery(self):  # P4-4, У-2
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        times = self.spy_history(o)
        tg.event_batches = [[ev("topic", {"id": 77, "title": "Renamed"})]]
        for _ in range(600):
            run(o.step())
            o.fake.clock.advance(TICK)
        self.assertLessEqual(len(times), 2, times)
        self.assertEqual(load(self.path)["topics"], [])
        self.assertEqual(self.state(), "ready")

    def test_single_overflow_catches_up_and_deferred_one_is_served_without_events(self):  # P4-6
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        times = self.spy_history(o)
        tg.history_data = [rec(101)]
        tg.event_batches = [[dict(OVERFLOW)]]
        run(o.step())
        run(o.step())
        self.assertEqual(ids_of(self.path), [100, 101])
        o.fake.clock.advance(1)
        tg.history_data.append(rec(102))
        tg.event_batches = [[dict(OVERFLOW)]]
        run(o.step())
        n = len(times)
        for _ in range(int(2 * DELTA / TICK)):
            o.fake.clock.advance(TICK)
            run(o.step())
        self.assertGreater(len(times), n, "отложенный overflow не обслужен без новых events")
        self.assertEqual(ids_of(self.path), [100, 101, 102])
        self.assertNotIn("disconnect", tg.names())
        self.assertEqual(tg.names().count("connect"), 1)

    def test_explicit_refresh_bypasses_interval(self):  # P4-11, У-4
        write_mirror(self.path, [100])
        o = self.start()
        tg, root = o.fake.tg, self.env.root("a")
        tg.event_batches = [[dict(OVERFLOW)]]
        run(o.step())
        run(o.step())  # фоновый догон выполнен, срок Δ еще не наступил
        o.fake.clock.advance(1)
        tg.history_data = [rec(101)]
        req = refresh_request("a", "d1", now=o.fake.clock.utc())
        write_package(root, req)
        for _ in range(3):
            run(o.step())
        self.assertEqual(self.sstate(root, req["request_id"]), "refreshed")
        self.assertEqual(ids_of(self.path), [100, 101])

    def test_startup_catchup_is_not_delayed(self):  # P4-3, У-4
        write_mirror(self.path, [100])
        o = self.env.make_owner("a", destinations=[self.env.destination("d1", 1001, "chat1")])
        o.fake.tg.history_data = [rec(101)]
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        self.assertEqual(ids_of(self.path), [100, 101])


class NetworkDeadlinesWithOrphan(OrphanBase):
    def wait_without_rpc(self, o, seconds):
        step = 0.25
        for _ in range(int(round(seconds / step))):
            mark = len(o.fake.tg.calls)
            o.fake.tg.event_batches = [[orphan(0)]]
            run(o.step())
            self.assertEqual(o.fake.tg.calls[mark:], [], "RPC до сетевого срока")
            o.fake.clock.advance(step)

    def test_floodwait_during_orphan_catchup_blocks_rpc_and_send(self):  # P4-9
        for seconds in (1, 25):
            with self.subTest(seconds=seconds):
                self.setUp()
                write_mirror(self.path, [100])
                o = self.start()
                tg, root = o.fake.tg, self.env.root("a")
                times = self.spy_history(o)
                tg.event_batches = [[orphan(0)]]
                tg.raises["history"] = import_owner().FloodWait(seconds)
                rid = self.queue_send(o, "me")
                run(o.step())
                for _ in range(2):
                    if times:
                        break
                    o.fake.clock.advance(TICK)
                    run(o.step())
                self.assertTrue(times, "догон по orphan не начат")
                t0 = times[0]
                wait = max(seconds, DELTA) - (o.fake.clock.monotonic() - t0)
                self.wait_without_rpc(o, wait - 0.25)
                self.assertNotEqual(self.sstate(root, rid), "sent")
                for _ in range(int(3 * DELTA / TICK)):
                    o.fake.clock.advance(TICK)
                    run(o.step())
                self.assertEqual(self.sstate(root, rid), "sent")
                self.assert_spacing(times)
                self.assertEqual(tg.names().count("connect"), 1)
                self.assertNotIn("disconnect", tg.names())
                run(o.close())

    def test_disconnected_with_pending_orphan_still_reconnects_after_deadline(self):  # P4-9
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        tg.event_batches = [[orphan(0)]]
        run(o.step())
        mark = len(tg.calls)
        tg.raises["events"] = import_owner().Disconnected()
        run(o.step())
        self.wait_without_rpc(o, 14)
        for _ in range(8):
            o.fake.clock.advance(1)
            run(o.step())
        after = tg.names()[mark:]
        self.assertIn("disconnect", after, after)
        self.assertIn("connect", after, after)
        self.assertEqual(self.state(), "ready")


class LocalReasonOverDeferredOverflow(OrphanBase):
    def test_local_retry_not_held_by_deferred_overflow(self):  # P4-7
        o = self.start()
        tg, clock, root = o.fake.tg, o.fake.clock, self.env.root("a")
        tg.event_batches = [[dict(OVERFLOW)]]
        run(o.step())
        run(o.step())              # догон в t, следующий срок t+5
        clock.advance(1)
        tg.event_batches = [[dict(OVERFLOW)]]
        run(o.step())              # overflow отложен до t+5
        tg.raises["events"] = ValueError("PRIVATE")
        run(o.step())              # локальная причина, retry уже разрешен
        self.assertEqual(self.state(), "degraded")
        rid = self.queue_send(o, "me")
        mark = len(tg.calls)
        for _ in range(2):         # без продвижения часов: t+1 < t+5
            run(o.step())
        self.assertTrue(tg.calls[mark:], "локальное восстановление ждет чужой overflow")
        self.assertEqual(self.sstate(root, rid), "sent", tg.names())
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())


class GapsFromReview(OrphanBase):
    def history_peers(self, tg, mark=0):
        return [c[1]["id"] for c in tg.calls[mark:] if c[0] == "history"]

    def two_dests(self, same_peer=False):
        write_mirror(self.path, [100])
        p2 = 1001 if same_peer else 2002
        write_mirror(self.env.projects / "chat2" / "result.json", [200], chat_id=p2)
        return self.start([self.env.destination("d1", 1001, "chat1"),
                           self.env.destination("d2", p2, "chat2")])

    def test_public_submit_between_syncs_is_accepted_and_sent(self):  # P4-2, В3
        write_mirror(self.path, [100])
        # mtime heartbeat - реальное время ФС, поэтому часы owner и клиента близки к реальным
        clock = FakeClock(utc=time.time())
        o = self.env.make_owner("a", destinations=[self.env.destination("d1", 1001, "chat1")],
                                clock=clock)
        self.assertEqual(run(o.start())["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        client, root = import_client(), self.env.root("a")
        replies = {}
        for n in range(40):
            o.fake.tg.event_batches = [[orphan(n)]]
            run(o.step())
            if n == 20:
                for peer in (1001, "me"):
                    req = send_request("a", peer=peer, text="Привет", now=clock.utc())
                    req["options"]["no_pace_check"] = True
                    replies[peer] = (req, client.submit(root, req, clock=clock, timeout=0))
            clock.advance(TICK)
        for peer, (req, reply) in replies.items():
            self.assertNotEqual(reply.get("code"), "OWNER_UNAVAILABLE", (peer, reply))
            self.assertEqual(self.sstate(root, req["request_id"]), "sent", (peer, reply))
        self.assertEqual(len(o.fake.tg.sends()), 2)

    def test_heartbeat_ready_after_explicit_refresh_with_pending_orphan(self):  # В3, У-4
        write_mirror(self.path, [100])
        o = self.start()
        root = self.env.root("a")
        o.fake.tg.event_batches = [[orphan(0)]]
        run(o.step())
        req = refresh_request("a", "d1", now=o.fake.clock.utc())
        write_package(root, req)
        for n in range(3):
            o.fake.tg.event_batches = [[orphan(n + 1)]]
            run(o.step())
            o.fake.clock.advance(TICK)
        self.assertEqual(self.sstate(root, req["request_id"]), "refreshed")
        self.assertEqual(self.state(), "ready")

    def test_refresh_shifts_background_interval(self):  # У-4
        write_mirror(self.path, [100])
        o = self.start()
        tg, clock, root = o.fake.tg, o.fake.clock, self.env.root("a")
        times = self.spy_history(o)
        tg.event_batches = [[dict(OVERFLOW)]]
        run(o.step())
        run(o.step())
        clock.advance(3)
        req = refresh_request("a", "d1", now=clock.utc())
        write_package(root, req)
        for _ in range(3):
            run(o.step())
        self.assertEqual(self.sstate(root, req["request_id"]), "refreshed")
        r = times[-1]
        mark = len(times)
        clock.advance(0.5)
        tg.event_batches = [[dict(OVERFLOW)]]
        while clock.monotonic() < r + DELTA - 0.3:
            run(o.step())
            clock.advance(TICK)
        self.assertEqual(len(times), mark, f"фоновый догон раньше r+Δ: {times}, r={r}")
        while clock.monotonic() < r + DELTA + 1:
            run(o.step())
            clock.advance(TICK)
        self.assertGreater(len(times), mark, "отложенный overflow не обслужен после Δ")
        self.assertGreaterEqual(times[mark] - r, DELTA - EPS)

    def test_refresh_of_one_peer_keeps_overflow_for_other_peer(self):  # У-4
        o = self.two_dests()
        tg, clock, root = o.fake.tg, o.fake.clock, self.env.root("a")
        tg.event_batches = [[dict(OVERFLOW)]]
        run(o.step())
        run(o.step())
        clock.advance(1)
        tg.event_batches = [[dict(OVERFLOW)]]
        run(o.step())
        req = refresh_request("a", "d1", now=clock.utc())
        write_package(root, req)
        run(o.step())
        self.assertEqual(self.sstate(root, req["request_id"]), "refreshed")
        mark = len(tg.calls)
        for _ in range(int(2 * DELTA / TICK)):
            clock.advance(TICK)
            run(o.step())
        self.assertIn(2002, self.history_peers(tg, mark), "overflow второго peer погашен refresh первого")

    def test_same_peer_destinations_get_pending_and_orphan_does_not_block_send(self):  # P4-10
        o = self.two_dests(same_peer=True)
        tg, root = o.fake.tg, self.env.root("a")
        tg.recent_data = [{"kind": "topic", "record": topic_rec(77), "revision": [NOW - 1, 0]}]
        rid = self.queue_send(o, 1001)
        rme = self.queue_send(o, "me")
        tg.event_batches = [[ev("topic", {"id": 77, "title": "Renamed"})]]
        for _ in range(int(2 * DELTA / TICK)):
            run(o.step())
            o.fake.clock.advance(TICK)
        self.assertEqual(self.sstate(root, rid), "sent")
        self.assertEqual(self.sstate(root, rme), "sent")
        for path in (self.path, self.env.projects / "chat2" / "result.json"):
            self.assertEqual(load(path)["topics"], [topic_rec(77, "Renamed")], path)

    def test_floodwait_on_events_at_interval_boundary_blocks_rpc(self):  # P4-9
        for seconds in (1, 10):
            with self.subTest(seconds=seconds):
                self.setUp()
                write_mirror(self.path, [100])
                o = self.start()
                tg, clock = o.fake.tg, o.fake.clock
                times = self.spy_history(o)
                tg.event_batches = [[dict(OVERFLOW)]]
                run(o.step())
                run(o.step())
                t = times[0]
                while clock.monotonic() < t + DELTA - 0.75:
                    clock.advance(TICK)
                tg.event_batches = [[dict(OVERFLOW)]]
                run(o.step())          # overflow отложен до t+Δ
                tg.raises["events"] = import_owner().FloodWait(seconds)
                clock.advance(0.75)
                run(o.step())          # FloodWait ровно на границе Δ
                start = clock.monotonic()
                n = len(tg.calls)
                while clock.monotonic() < start + seconds - 0.2:
                    clock.advance(0.1)
                    run(o.step())
                self.assertEqual(tg.calls[n:], [], "RPC раньше срока FloodWait")
                for _ in range(100):
                    clock.advance(0.1)
                    run(o.step())
                self.assertGreater(len(times), 1, "overflow не обслужен после срока")
                self.assertNotIn("disconnect", tg.names())
                run(o.close())


if __name__ == "__main__":
    unittest.main(verbosity=2)
