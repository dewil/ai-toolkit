#!/usr/bin/env python3
"""RED: пакет 2 - отмена батча events и медиа не теряют данные (спека 2026-11-24, P2-1..P2-12, У-1..У-5).

Критерии (docs/2026-11-24-spec-telegram-owner-pkg2.md):
- P2-1  отмена на первом/среднем/последнем элементе батча не теряет префикс, элемент и хвост;
- P2-2  через настоящий адаптер и owner._rpc("events"): timeout, внешняя отмена, повторная отмена, reconnect;
- P2-3  edit/delete/topic старше окна recent (50) доходят из сохраненного батча;
- P2-4  ревизии неизменны при повторе, нет отката и ложного удаления;
- P2-5  новые updates во время gate не затираются и не обгоняют старые изменения;
- P2-6  overflow: начальный, новый во время батча, их сочетание;
- P2-7  media=false: отмена в get_sender() не теряет событие, загрузка не вызывается;
- P2-8..P2-12  медиа: конечное имя = готовый файл; отмена/ошибка не оставляют усеченный кэш;
  повтор докачивает заново; общий helper direct/owner.

Без Telegram и боевых credentials. Внутренний шов адаптера - как в существующих тестах
(tests/test_telegram_owner_cli_local.py): _updates/_overflow/_seq/_aliases/_entities/_record/snapshot.

Запуск: python3 tests/test_telegram_owner_cancel_durability.py
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeTelegram, NOW, import_owner, run,
)
from test_telegram_session_owner_mirror import ids_of, load, rec, topic_rec, write_mirror  # noqa: E402
from test_telegram_session_owner_cli import load_script  # noqa: E402
from telegram_owner_telethon import Telegram  # noqa: E402

UPDATE_NAMES = ("UpdateDeleteChannelMessages", "UpdateDeleteMessages", "UpdateNewMessage",
                "UpdateNewChannelMessage", "UpdateEditMessage", "UpdateEditChannelMessage",
                "UpdatesTooLong", "UpdateChannelTooLong", "UpdateShortMessage", "UpdateShortChatMessage")
SERVICE_NAMES = ("MessageActionTopicEdit", "MessageActionTopicCreate")
CHAN = 1001
BASE = int(NOW)  # базовая метка для дат сообщений
WAIT = 3.0       # предельное ожидание gate, секунды


# ---------------------------------------------------------------- gate (без привязки к циклу)

class Gate:
    """Останавливает выбранный await до release(); флаги обычные, чтобы жить между asyncio.run."""

    def __init__(self, where, pred):
        self.where, self.pred = where, pred
        self.reached, self.opened = False, False

    def release(self):
        self.opened = True


class Gates:
    def __init__(self):
        self.items = []

    def add(self, where, pred):
        g = Gate(where, pred)
        self.items.append(g)
        return g

    async def hit(self, where, m):
        for g in self.items:
            if not g.opened and g.where == where and g.pred(m):
                g.reached = True
                deadline = time.monotonic() + WAIT
                while not g.opened:
                    if time.monotonic() > deadline:
                        raise AssertionError("gate не открыт")
                    await asyncio.sleep(0.001)


async def until(cond, what="gate (точка отмены не достигнута: батч не сохранен предыдущей отменой?)"):
    deadline = time.monotonic() + WAIT
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"не достигнут {what}")
        await asyncio.sleep(0.001)


def by_id(i):
    return lambda m: m.id == i


# ---------------------------------------------------------------- построение адаптера и updates

def msg(i, *, text=None, ts=None, topic=False, edited=False, peer=CHAN):
    when = datetime.fromtimestamp(ts if ts is not None else BASE + i, timezone.utc)
    return types.SimpleNamespace(
        id=i, action=None, peer_id=types.SimpleNamespace(channel_id=peer),
        edit_date=when if edited else None, date=when,
        text=text if text is not None else f"msg {i}", is_topic=topic,
        media=object(), file=types.SimpleNamespace(name="s.bin", size=4, ext=".bin"),
        photo=None, document=None, web_preview=None)


def fake_record_factory(gates):
    async def _record(m, seq, *a, **kw):
        await gates.hit("record", m)
        if m.is_topic:
            return {"kind": "topic", "record": topic_rec(m.id, m.text),
                    "revision": [m.date.timestamp(), seq]}
        extra = {}
        if m.edit_date is not None:
            extra = {"edited": "2026-11-19T11:00:00", "edited_unixtime": str(int(m.edit_date.timestamp()))}
        return rec(m.id, m.text, **extra)
    return _record


def fake_download_factory(gates):
    async def _download(*args, **kw):
        m = next(x for x in args if hasattr(x, "peer_id"))
        await gates.hit("media", m)
        return None
    return _download


def make_adapter(*, gates=None, project_config=None, snapshot=None, client=None, peer=CHAN,
                 real_record=False, dests=1):
    a = Telegram.__new__(Telegram)
    a.account = "a"
    a.client = client or types.SimpleNamespace(is_connected=lambda: True)
    a.types = types.SimpleNamespace(**{n: type(n, (), {}) for n in UPDATE_NAMES + SERVICE_NAMES})
    a.config = {"destinations": [
        {"destination_id": f"d{k + 1}", "project_root": "/PROJECT", "peer": peer,
         "project_config": dict(project_config or {})} for k in range(dests)]}
    a._aliases = {peer: {"account": "a", "kind": "Channel", "id": peer}}
    a._entities = {("Channel", peer): "entity"}
    a._media_errors, a._media_cleanup = {}, {}
    a._updates = deque()
    a._overflow, a._seq = False, 0
    a.gates = gates or Gates()
    snap = snapshot or load_script("telegram-snapshot.py", "cancel_durability_" + os.urandom(3).hex())
    if snapshot is None:
        snap.cleanup_old_media = Mock(return_value=0)
        snap.download_message_media = AsyncMock(side_effect=fake_download_factory(a.gates))
    a.snapshot = snap
    if not real_record:
        a._record = fake_record_factory(a.gates)
    return a


def upd(a, kind, **attrs):
    u = getattr(a.types, kind)()
    for k, v in attrs.items():
        setattr(u, k, v)
    return u


def u_new(a, i, **kw):
    return upd(a, "UpdateNewMessage", message=msg(i, **kw))


def u_edit(a, i, text, ts=None):
    return upd(a, "UpdateEditMessage", message=msg(i, text=text, ts=ts, edited=True))


def u_topic(a, i, title):
    return upd(a, "UpdateNewMessage", message=msg(i, text=title, topic=True))


def u_delete(a, ids, peer=CHAN):
    return upd(a, "UpdateDeleteChannelMessages", channel_id=peer, messages=list(ids))


def standard_batch(a):
    """new(101), delete(7), edit(8), topic(9), new(102) - порядок поступления."""
    return [u_new(a, 101), u_delete(a, [7]), u_edit(a, 8, "правка 8"), u_topic(a, 9, "Тема 9"), u_new(a, 102)]


def norm(e):
    """Сравнимая форма события. Время delete берется при обработке, поэтому сравнивается только sequence."""
    if e["kind"] == "overflow":
        return ("overflow",)
    if e["kind"] == "delete":
        return ("delete", tuple(e["ids"]), e["revision"][1])
    r = e["record"]
    return (e["kind"], r["id"], r.get("text", r.get("title")), tuple(e["revision"]))


async def drain(a, limit=8):
    out = []
    for _ in range(limit):
        got = await a.events()
        if not got:
            break
        out.extend(got)
    return out


async def cancelled_events(testcase, a, gate):
    """events() до gate, отмена, снятие gate. Возвращает (t0, t_cancel)."""
    t0 = time.time()
    task = asyncio.ensure_future(a.events())
    try:
        await until(lambda: gate.reached)
    except AssertionError:
        task.cancel()
        raise
    t_cancel = time.time()
    task.cancel()
    with testcase.assertRaises(asyncio.CancelledError):
        await task
    gate.release()
    return t0, t_cancel


async def expected_for(build):
    """Контроль: те же updates без отмены."""
    a = make_adapter()
    for u in build(a):
        await a._enqueue(u)
    return [norm(e) for e in await drain(a)]


# ================================================================== P2-1 / P2-4: батч, отмена, ревизии

class BatchCancel(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_at_first_middle_last_keeps_every_event_p2_1(self):
        """Отмена на record/media первого (101), среднего (8) и последнего (102) элемента."""
        expected = await expected_for(standard_batch)
        self.assertEqual(len(expected), 5, expected)
        for where in ("record", "media"):
            for mid in (101, 8, 102):
                with self.subTest(where=where, element=mid):
                    a = make_adapter()
                    for u in standard_batch(a):
                        await a._enqueue(u)
                    gate = a.gates.add(where, by_id(mid))
                    t0, t_cancel = await cancelled_events(self, a, gate)
                    got = await drain(a)
                    self.assertEqual(set(map(norm, got)), set(expected),
                                     "после отмены потеряны или изменены события батча")
                    # delete (элемент 2) подготовлен до отмены на 8/102: его время не должно быть новым
                    if mid != 101:
                        times = [e["revision"][0] for e in got if e["kind"] == "delete"]
                        self.assertTrue(times and all(t0 - 1 <= t <= t_cancel + 0.5 for t in times),
                                        f"revision delete пересчитана при повторе: {times}, окно {t0}..{t_cancel}")

    async def test_repeated_cancel_before_successful_return_p2_2(self):
        """Две отмены подряд в разных точках, затем успешная выдача всего батча."""
        expected = await expected_for(standard_batch)
        a = make_adapter()
        for u in standard_batch(a):
            await a._enqueue(u)
        g1 = a.gates.add("record", by_id(8))
        g2 = a.gates.add("media", by_id(102))
        await cancelled_events(self, a, g1)
        await cancelled_events(self, a, g2)
        got = await drain(a)
        self.assertEqual(set(map(norm, got)), set(expected))

    async def test_two_edits_same_second_keep_order_and_sequence_p2_4(self):
        def build(a):
            return [u_new(a, 101), u_edit(a, 8, "v1", ts=BASE + 50), u_edit(a, 8, "v2", ts=BASE + 50),
                    u_new(a, 102)]
        expected = await expected_for(build)
        a = make_adapter()
        for u in build(a):
            await a._enqueue(u)
        gate = a.gates.add("record", lambda m: m.text == "v2")
        await cancelled_events(self, a, gate)
        got = await drain(a)
        self.assertEqual(set(map(norm, got)), set(expected))
        edits = [e for e in got if e["kind"] == "edit"]
        order = [e["record"]["text"] for e in edits]
        self.assertLess(order.index("v1"), order.index("v2"), order)
        rev = {e["record"]["text"]: tuple(e["revision"]) for e in edits}
        self.assertLess(rev["v1"], rev["v2"], "правки одной секунды потеряли порядок ревизий")

    async def test_delete_without_pts_keeps_original_sequence_p2_4(self):
        """delete имеет только fallback sequence: повтор не создает новую редакцию."""
        def build(a):
            return [u_delete(a, [7]), u_new(a, 101), u_delete(a, [8])]
        expected = await expected_for(build)
        a = make_adapter()
        for u in build(a):
            await a._enqueue(u)
        gate = a.gates.add("record", by_id(101))
        await cancelled_events(self, a, gate)
        got = await drain(a)
        self.assertEqual(set(map(norm, got)), set(expected))
        deletes = {tuple(e["ids"]): e["revision"][1] for e in got if e["kind"] == "delete"}
        self.assertEqual(deletes, {(7,): 1, (8,): 3}, "sequence delete изменилась при повторе")

    async def test_unsupported_and_unqualified_updates_keep_policy_after_cancel(self):
        """Неподдерживаемые raw и UpdateDeleteMessages без peer по-прежнему не порождают событий."""
        a = make_adapter()
        await a._enqueue(upd(a, "UpdateDeleteMessages", messages=[5]))
        await a._enqueue(u_new(a, 101))
        gate = a.gates.add("record", by_id(101))
        await cancelled_events(self, a, gate)
        got = await drain(a)
        self.assertEqual([e["kind"] for e in got], ["new"], got)


# ================================================================== P2-5 / P2-6: новые updates и overflow

class NewUpdatesAndOverflow(unittest.IsolatedAsyncioTestCase):
    async def test_updates_arriving_during_gate_are_not_lost_or_reordered_p2_5(self):
        a = make_adapter()
        for u in standard_batch(a):
            await a._enqueue(u)
        gate = a.gates.add("record", by_id(8))
        task = asyncio.ensure_future(a.events())
        await until(lambda: gate.reached)
        await a._enqueue(u_edit(a, 8, "правка 8 новее", ts=BASE + 500))  # того же id, позже
        await a._enqueue(u_new(a, 103))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        gate.release()
        got = await drain(a)
        texts = [e["record"].get("text") for e in got if e["kind"] == "edit"]
        self.assertEqual(texts, ["правка 8", "правка 8 новее"], "правка id 8 потеряна или обогнана новой")
        self.assertIn(("new", 103), [(e["kind"], e["record"]["id"]) for e in got if e["kind"] == "new"])
        rev = {e["record"]["text"]: tuple(e["revision"]) for e in got if e["kind"] == "edit"}
        self.assertLess(rev["правка 8"], rev["правка 8 новее"])
        for i in (101, 102):
            self.assertIn(("new", i), [(e["kind"], e["record"]["id"]) for e in got if e["kind"] == "new"])

    async def test_two_destinations_of_one_peer_do_not_mix_p2_5(self):
        a = make_adapter(dests=2)
        for u in standard_batch(a):
            await a._enqueue(u)
        gate = a.gates.add("media", by_id(8))
        await cancelled_events(self, a, gate)
        got = await drain(a)
        kinds = sorted(norm(e)[0] for e in got)
        self.assertEqual(kinds.count("delete"), 1, "delete продублирован или потерян при двух назначениях")
        self.assertEqual(kinds.count("topic"), 1)
        for e in got:
            if e["kind"] != "overflow":
                self.assertEqual((e["peer"]["account"], e["peer"]["kind"], e["peer"]["id"]),
                                 ("a", "Channel", CHAN))

    async def test_initial_and_new_overflow_with_cancel_p2_6(self):
        expected = await expected_for(standard_batch)
        for init, new in ((True, False), (False, True), (True, True)):
            with self.subTest(initial=init, new_during_batch=new):
                a = make_adapter()
                a._overflow = init
                for u in standard_batch(a):
                    await a._enqueue(u)
                a._overflow = init  # _enqueue не должен был его менять, но фиксируем исходное условие
                gate = a.gates.add("record", by_id(8))
                task = asyncio.ensure_future(a.events())
                await until(lambda: gate.reached)
                if new:
                    for _ in range(1001):  # реальное переполнение очереди новых updates
                        await a._enqueue(object())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                gate.release()
                got = await drain(a)
                keys = set(map(norm, got))
                self.assertTrue(set(expected) - {("overflow",)} <= keys,
                                "выбранные события потеряны при overflow + отмена")
                self.assertIn(("overflow",), keys, "основание для догона (overflow) погашено отменой")
                self.assertLessEqual(len(a._updates), 1000, "очередь перестала быть ограниченной")
                self.assertEqual(await a.events(), [], "лишний повтор после полной выдачи")

    async def test_initial_overflow_alone_survives_cancel_p2_6(self):
        a = make_adapter()
        a._overflow = True
        await a._enqueue(u_new(a, 101))
        a._overflow = True
        gate = a.gates.add("record", by_id(101))
        await cancelled_events(self, a, gate)
        got = await drain(a)
        self.assertIn("overflow", [e["kind"] for e in got], "после отмены начальный overflow исчез")
        self.assertIn(("new", 101), [(e["kind"], e["record"]["id"]) for e in got if e["kind"] == "new"])

    async def test_real_queue_overflow_is_still_signalled_and_bounded_p2_6(self):
        a = make_adapter()
        for _ in range(1001):
            await a._enqueue(object())
        self.assertLessEqual(len(a._updates), 1000)
        self.assertIn({"kind": "overflow"}, await a.events())


# ================================================================== У-7: sequence = pts, fallback - счетчик

def with_pts(u, pts):
    u.pts = pts
    return u


class PtsSequence(unittest.IsolatedAsyncioTestCase):
    def build(self, a):
        return [with_pts(u_new(a, 101), 5001), with_pts(u_delete(a, [7]), 5002),
                with_pts(u_edit(a, 8, "правка 8"), 5003), u_new(a, 102),  # без pts - счетчик (4-й элемент)
                with_pts(u_edit(a, 8, "правка 8б"), 5004)]

    async def test_sequence_is_pts_when_present_and_counter_otherwise_u7(self):
        a = make_adapter()
        for u in self.build(a):
            await a._enqueue(u)
        got = await drain(a)
        seq = {}
        for e in got:
            key = ("delete", tuple(e["ids"])) if e["kind"] == "delete" else (e["kind"], e["record"]["id"],
                                                                         e["record"].get("text"))
            seq[key] = e["revision"][1]
        self.assertEqual(seq[("new", 101, "msg 101")], 5001)
        self.assertEqual(seq[("delete", (7,))], 5002)
        self.assertEqual(seq[("edit", 8, "правка 8")], 5003)
        self.assertEqual(seq[("edit", 8, "правка 8б")], 5004)
        self.assertEqual(seq[("new", 102, "msg 102")], 4, "без pts sequence - счетчик поступления")

    async def test_cancel_and_redelivery_keep_pts_and_counter_u7(self):
        expected = await expected_for(self.build)
        self.assertIn(("new", 101, "msg 101", (BASE + 101, 5001)), expected)
        for where, mid in (("record", 8), ("media", 102), ("record", 101)):
            with self.subTest(where=where, element=mid):
                a = make_adapter()
                for u in self.build(a):
                    await a._enqueue(u)
                gate = a.gates.add(where, by_id(mid))
                await cancelled_events(self, a, gate)
                got = await drain(a)
                self.assertEqual(set(map(norm, got)), set(expected),
                                 "sequence пересчитан при повторной выдаче (pts или счетчик)")

    async def test_counter_without_pts_is_not_shifted_by_retry_u7(self):
        def build(a):
            return [u_new(a, 101), u_edit(a, 8, "x"), u_new(a, 102)]
        expected = await expected_for(build)
        a = make_adapter()
        for u in build(a):
            await a._enqueue(u)
        gate = a.gates.add("record", by_id(8))
        await cancelled_events(self, a, gate)
        got = await drain(a)
        self.assertEqual(set(map(norm, got)), set(expected))
        self.assertEqual(sorted(e["revision"][1] for e in got), [1, 2, 3])


# ================================================================== P2-7: media=false и реальный конвертер

class MediaOffConversion(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_in_get_sender_with_media_false_redelivers_p2_7(self):
        gates = Gates()
        a = make_adapter(gates=gates, project_config={"media": False}, real_record=True)
        sender = types.SimpleNamespace(id=5, first_name="Иван", last_name=None, title=None,
                                       username=None, deleted=False)

        async def get_sender():
            await gates.hit("sender", None)
            return sender

        when = datetime.fromtimestamp(BASE + 8, timezone.utc)
        m = types.SimpleNamespace(
            id=8, action=None, message="текст правки", entities=None, sender_id=5, from_id=None, get_sender=get_sender,
            peer_id=types.SimpleNamespace(channel_id=CHAN), date=when, edit_date=when, reply_to=None,
            media=object(), photo=None, document=None, web_preview=None,
            file=types.SimpleNamespace(name="a.bin", size=4, mime_type="application/octet-stream", ext=".bin"))
        await a._enqueue(upd(a, "UpdateEditMessage", message=m))
        gate = gates.add("sender", lambda _m: True)
        await cancelled_events(self, a, gate)
        got = await drain(a)
        edits = [e for e in got if e["kind"] == "edit"]
        self.assertEqual(len(edits), 1, f"событие edit потеряно после отмены: {got}")
        r = edits[0]["record"]
        self.assertEqual((r["id"], r["file_name"], r["file_size"]), (8, "a.bin", 4))
        a.snapshot.download_message_media.assert_not_awaited()


# ================================================================== P2-2 / P2-3 / P2-5: настоящие границы owner

class OwnerTelegram(Telegram):
    """Настоящий events() адаптера; остальной порт - у FakeTelegram."""

    async def connect(self):
        await self.fake.connect()

    async def disconnect(self):
        await self.fake.disconnect()

    async def resolve(self, account, peer):
        return await self.fake.resolve(account, peer)

    async def history(self, peer, *, after_id, limit):
        return await self.fake.history(peer, after_id=after_id, limit=limit)

    async def recent(self, peer, *, limit):
        return await self.fake.recent(peer, limit=limit)

    async def upload(self, path):
        return await self.fake.upload(path)

    async def send(self, request, peer, uploads):
        return await self.fake.send(request, peer, uploads)

    async def scheduled(self, peer):
        return await self.fake.scheduled(peer)

    async def events(self):
        self.fake._log("events")
        return await super().events()


class OwnerBoundary(Base):
    def setUp(self):
        super().setUp()
        self.mod = import_owner()
        self.mirror = self.env.projects / "chat1" / "result.json"
        budgets = {k: 5.0 for k in ("connect", "disconnect", "resolve", "history", "recent",
                                    "events", "upload", "send", "scheduled")}
        budgets["events"] = 0.05
        p = patch.object(self.mod, "RPC_TIMEOUTS", budgets, create=True)
        p.start()
        self.addCleanup(p.stop)

    def start(self, mirror_ids, topics=(), old_dates=False):
        write_mirror(self.mirror, mirror_ids, chat_id=CHAN, topics=list(topics))
        if old_dates:  # записи зеркала старше времени обработки delete (оно берется при обработке)
            import json
            d = load(self.mirror)
            for m in d["messages"]:
                m["date_unixtime"] = str(1_700_000_000 + m["id"])
            for t in d["topics"]:
                t["date_unixtime"] = "1700000000"
            self.mirror.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        fake = FakeTelegram("a")
        fake.peer_map[CHAN] = {"kind": "Channel", "id": CHAN}
        self.gates = Gates()
        a = make_adapter(gates=self.gates)
        a.__class__ = OwnerTelegram
        a.fake = fake
        owner = self.env.make_owner("a", tg=a, destinations=[self.env.destination("d1", CHAN, "chat1")])
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        self.adapter, self.fake = a, fake
        return owner

    def feed(self, updates):
        for u in updates:
            run(self.adapter._enqueue(u))

    def step(self, owner):
        return run(asyncio.wait_for(owner.step(), timeout=2))

    def settle(self, owner, done, steps=8):
        for _ in range(steps):
            owner.fake.clock.advance(15)
            self.step(owner)
            if done():
                return True
        return done()

    def test_control_without_cancel_reaches_mirror(self):
        """Контроль стенда: те же updates без gate доходят до зеркала."""
        owner = self.start([100])
        a = self.adapter
        self.feed([u_new(a, 101), u_edit(a, 100, "правка 100"), u_new(a, 102)])
        self.assertTrue(self.settle(owner, lambda: ids_of(self.mirror) == [100, 101, 102]),
                        f"стенд не доставляет события: {ids_of(self.mirror)}")
        self.assertEqual(load(self.mirror)["messages"][0]["text"], "правка 100")

    def test_events_timeout_keeps_batch_until_mirror_commit_p2_2(self):
        owner = self.start([100])
        a = self.adapter
        self.feed([u_new(a, 101), u_edit(a, 100, "правка 100"), u_new(a, 102)])
        gate = self.gates.add("record", by_id(100))
        with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
            self.step(owner)
        self.assertTrue(gate.reached)
        self.assertTrue(any("OWNER_RPC_TIMEOUT operation=events" in s for s in logs.output), logs.output)
        gate.release()
        ok = self.settle(owner, lambda: ids_of(self.mirror) == [100, 101, 102])
        self.assertTrue(ok, f"сохраненный батч не дошел до зеркала: {ids_of(self.mirror)}")
        m = {x["id"]: x for x in load(self.mirror)["messages"]}
        self.assertEqual(m[100]["text"], "правка 100")

    def test_timeout_then_reconnect_and_catchup_path_p2_2(self):
        owner = self.start([100])
        a = self.adapter
        self.feed([u_new(a, 101), u_new(a, 102)])
        gate = self.gates.add("media", by_id(101))
        self.step(owner)
        self.assertTrue(gate.reached)
        gate.release()
        self.fake.raises["events"] = self.mod.Disconnected()
        mark = len(self.fake.calls)
        ok = self.settle(owner, lambda: ids_of(self.mirror) == [100, 101, 102])
        after = self.fake.names()[mark:]
        self.assertIn("disconnect", after)
        self.assertIn("connect", after)
        self.assertTrue(ok, f"после reconnect батч не доставлен: {ids_of(self.mirror)}")

    def test_external_task_cancel_twice_then_delivery_p2_2(self):
        owner = self.start([100])
        a = self.adapter
        self.feed([u_new(a, 101), u_new(a, 102)])
        g1 = self.gates.add("record", by_id(101))
        g2 = self.gates.add("media", by_id(102))

        async def scenario():
            for g in (g1, g2):
                t = asyncio.ensure_future(owner.step())
                await until(lambda: g.reached)
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
                g.release()
        run(scenario())
        ok = self.settle(owner, lambda: ids_of(self.mirror) == [100, 101, 102])
        self.assertTrue(ok, f"после внешних отмен события не дошли: {ids_of(self.mirror)}")

    def test_edit_after_restart_in_same_second_with_larger_pts_is_applied_u7(self):
        owner = self.start([100])
        a = self.adapter
        self.feed([with_pts(u_edit(a, 100, "v1", ts=BASE + 10), 5000)])
        self.assertTrue(self.settle(owner, lambda: load(self.mirror)["messages"][0]["text"] == "v1"))
        run(owner.close())
        fake = FakeTelegram("a")
        fake.peer_map[CHAN] = {"kind": "Channel", "id": CHAN}
        a2 = make_adapter(gates=Gates())
        a2.__class__ = OwnerTelegram
        a2.fake = fake
        owner2 = self.env.make_owner("a", tg=a2, destinations=[self.env.destination("d1", CHAN, "chat1")])
        self.assertEqual(run(owner2.start())["code"], "READY")
        self.addCleanup(lambda: run(owner2.close()))
        run(a2._enqueue(with_pts(u_edit(a2, 100, "v2", ts=BASE + 10), 5001)))  # та же секунда, pts больше
        ok = self.settle(owner2, lambda: load(self.mirror)["messages"][0]["text"] == "v2")
        self.assertTrue(ok, "правка той же секунды с большим pts после рестарта отброшена")

    def test_old_edit_delete_topic_below_recent_window_p2_3(self):
        owner = self.start(list(range(1, 81)), topics=[topic_rec(77, "Старое")], old_dates=True)
        a = self.adapter
        self.feed([u_edit(a, 5, "старая правка"), u_delete(a, [6]), u_topic(a, 77, "Новое")])
        gate = self.gates.add("record", by_id(5))
        self.step(owner)
        self.assertTrue(gate.reached)
        gate.release()

        def applied():
            d = load(self.mirror)
            m = {x["id"]: x for x in d["messages"]}
            return (m[5]["text"] == "старая правка" and m[6].get("deleted")
                    and {t["id"]: t["title"] for t in d["topics"]}.get(77) == "Новое")
        self.assertTrue(self.settle(owner, applied), "старые edit/delete/topic потеряны после отмены")
        d = load(self.mirror)
        m = {x["id"]: x for x in d["messages"]}
        self.assertEqual([x["text"] for x in m[5]["edit_history"]], ["msg 5"])
        self.assertEqual([i for i, x in m.items() if x.get("deleted")], [6], "ложное удаление")

    def test_newer_edit_during_gate_is_not_rolled_back_p2_4_p2_5(self):
        owner = self.start([100])
        a = self.adapter
        self.feed([u_edit(a, 100, "v1", ts=BASE + 10)])
        gate = self.gates.add("record", lambda m: m.text == "v1")
        self.step(owner)
        self.assertTrue(gate.reached)
        self.feed([u_edit(a, 100, "v2", ts=BASE + 20)])
        gate.release()
        self.assertTrue(self.settle(owner, lambda: load(self.mirror)["messages"][0]["text"] == "v2"),
                        "более новая правка не применена")
        for _ in range(3):  # дальнейшие шаги не откатывают текст и не дублируют историю
            owner.fake.clock.advance(15)
            self.step(owner)
        msg100 = load(self.mirror)["messages"][0]
        self.assertEqual(msg100["text"], "v2")
        history = [h["text"] for h in msg100["edit_history"]]
        self.assertEqual(len(history), len(set(history)), f"edit_history дублируется: {history}")
        self.assertEqual(history.count("msg 100"), 1)


# ================================================================== P2-8..P2-12: медиа

class FakeClient:
    """Клиент: download_media пишет в запрошенный путь по сценарию behavior(client, file)."""

    def __init__(self, behavior=None):
        self.behavior = behavior or ok_behavior
        self.calls, self.paths, self.reached = 0, [], False

    async def download_media(self, m, file=None, **kw):
        self.calls += 1
        self.paths.append(file)
        return await self.behavior(self, file)


async def ok_behavior(client, file, data=b"DATA"):
    Path(file).write_bytes(data)
    return file


async def hang_after(client, file, data=b"PART"):
    if data:
        Path(file).write_bytes(data)
    client.reached = True
    while True:
        await asyncio.sleep(0.005)


async def partial_then_oserror(client, file):
    Path(file).write_bytes(b"PART")
    raise OSError("PRIVATE-DETAIL /private/path")


async def full_then_cancel(client, file):
    Path(file).write_bytes(b"DATA")
    raise asyncio.CancelledError()


async def full_return_none(client, file):
    Path(file).write_bytes(b"DATA")
    return None


async def short_file(client, file):
    Path(file).write_bytes(b"DA")
    return file


async def missing_returned_path(client, file):
    return file + ".absent"


def media_msg(i=1, *, name="sample.bin", size=4, photo=False, media=True):
    return types.SimpleNamespace(
        id=i, media=object() if media else None,
        file=types.SimpleNamespace(name=name, size=size, ext=".jpg" if photo else ".bin"),
        photo=object() if photo else None, document=None, web_preview=None)


class MediaBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.snap = load_script("telegram-snapshot.py", "cancel_media_" + os.urandom(3).hex())
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.chat = Path(self._tmp.name) / "chat"
        self.cache_root = Path(self._tmp.name)
        self.final = self.chat / "1_sample.bin"

    def files(self, root=None):
        root = root or self.chat
        return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []

    async def dl(self, client, m=None):
        return await self.snap.download_message_media(client, m or media_msg(), self.chat)

    @contextlib.contextmanager
    def failing(self, *names, exc=OSError):
        """Ломает файловые операции публикации/удаления (все разумные способы)."""
        def boom(*a, **k):
            raise exc("injected")
        with contextlib.ExitStack() as st:
            for target in names:
                holder, attr = target
                st.enter_context(patch.object(holder, attr, boom))
            yield


class MediaCancel(MediaBase):
    async def cancel_run(self, behavior, wait_attr="reached"):
        client = FakeClient(behavior)
        task = asyncio.ensure_future(self.dl(client))
        await until(lambda: getattr(client, wait_attr), "downloader")
        self.assertFalse(self.final.exists(), "конечное имя видно во время загрузки")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        return client

    async def test_cancel_after_partial_leaves_no_ready_file_and_retry_redownloads_p2_8(self):
        client = await self.cancel_run(hang_after)
        self.assertFalse(self.final.exists())
        self.assertEqual(self.files(), [], "после кооперативной отмены остался файл (temp/частичный)")
        client.behavior = ok_behavior
        name = await self.dl(client)
        self.assertEqual(name, "1_sample.bin")
        self.assertEqual(client.calls, 2, "повтор не запустил загрузку заново")
        self.assertEqual(self.final.read_bytes(), b"DATA")
        self.assertEqual(self.files(), ["1_sample.bin"])

    async def test_cancel_before_first_bytes_p2_8(self):
        client = await self.cancel_run(lambda c, f: hang_after(c, f, data=b""))
        self.assertEqual(self.files(), [])
        client.behavior = ok_behavior
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(client.calls, 2)

    async def test_cancel_after_last_byte_before_publication_p2_8(self):
        client = FakeClient(full_then_cancel)
        with self.assertRaises(asyncio.CancelledError):
            await self.dl(client)
        self.assertFalse(self.final.exists(), "полный temp опубликован несмотря на отмену")
        self.assertEqual(self.files(), [])
        client.behavior = ok_behavior
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(client.calls, 2)

    async def test_cleanup_failure_does_not_mask_cancel_or_make_ready_media_p2_9(self):
        client = FakeClient(hang_after)
        task = asyncio.ensure_future(self.dl(client))
        await until(lambda: client.reached, "downloader")
        with self.failing((os, "unlink"), (os, "remove"), (Path, "unlink"), exc=PermissionError):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.final.exists(), "остаток после сбоя уборки стал конечным файлом")
        client.behavior = ok_behavior
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(client.calls, 2, "temp-остаток принят за cache hit")
        self.assertEqual(self.final.read_bytes(), b"DATA")


class MediaErrors(MediaBase):
    async def test_downloader_error_after_partial_best_effort_no_ready_file_p2_9(self):
        client = FakeClient(partial_then_oserror)
        self.assertIsNone(await self.dl(client))
        self.assertFalse(self.final.exists())
        self.assertEqual(self.files(), [], "после ошибки остался усеченный файл")
        client.behavior = ok_behavior
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(client.calls, 2, "повтор не докачал заново")
        self.assertEqual(self.final.read_bytes(), b"DATA")

    async def test_none_result_is_not_published_p2_9_p2_10(self):
        client = FakeClient(full_return_none)
        self.assertIsNone(await self.dl(client), "None downloader опубликован как успех")
        self.assertFalse(self.final.exists())
        self.assertEqual(self.files(), [])

    async def test_known_size_mismatch_is_not_published_p2_10(self):
        client = FakeClient(short_file)
        self.assertIsNone(await self.dl(client))
        self.assertFalse(self.final.exists())
        self.assertEqual(self.files(), [])

    async def test_missing_returned_path_is_not_published_p2_9(self):
        client = FakeClient(missing_returned_path)
        self.assertIsNone(await self.dl(client))
        self.assertFalse(self.final.exists())

    async def test_returned_path_differs_from_requested_is_published_from_returned_p2_10(self):
        async def other_path(client, file):
            p = Path(file + ".bin")
            p.write_bytes(b"DATA")
            return str(p)
        client = FakeClient(other_path)
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(self.final.read_bytes(), b"DATA")
        self.assertEqual(self.files(), ["1_sample.bin"], "остался лишний файл от возвращенного пути")

    async def test_publication_failure_gives_no_ready_file_p2_9(self):
        client = FakeClient(ok_behavior)
        with self.failing((os, "replace"), (os, "rename"), (Path, "replace"), (Path, "rename"),
                          (os, "link"), (shutil, "move")):
            try:
                result = await self.dl(client)
            except Exception:  # noqa: BLE001 - допустимо и исключение, главное: нет готового файла
                result = None
        self.assertIsNone(result)
        self.assertFalse(self.final.exists(), "при сбое публикации появился конечный файл")
        client.behavior = ok_behavior
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(client.calls, 2)


class MediaSuccess(MediaBase):
    async def test_final_absent_during_download_then_complete_and_cached_p2_10(self):
        seen = []

        async def observing(client, file):
            seen.append(self.final.exists())
            Path(file).write_bytes(b"DATA")
            seen.append(self.final.exists())
            return file
        client = FakeClient(observing)
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(seen, [False, False])
        self.assertEqual((self.final.read_bytes(), self.final.stat().st_size), (b"DATA", 4))
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(client.calls, 1, "готовая корректная копия не переиспользована")

    async def test_nameless_photo_none_media_and_zero_size_p2_10(self):
        client = FakeClient(ok_behavior)
        self.assertEqual(await self.dl(client, media_msg(2, name=None, photo=True)), "2.jpg")
        self.assertIsNone(await self.dl(client, media_msg(3, media=False)))
        self.assertEqual(client.calls, 1)

        async def empty(client_, file):
            Path(file).write_bytes(b"")
            return file
        zero = FakeClient(empty)
        self.assertEqual(await self.dl(zero, media_msg(4, name="e.bin", size=0)), "4_e.bin")
        self.assertEqual((self.chat / "4_e.bin").stat().st_size, 0)

    async def test_concurrent_attempts_use_distinct_temps_p2_11(self):
        started = []

        async def both(client, file):
            started.append(file)
            await until(lambda: len(started) == 2, "вторая попытка")
            Path(file).write_bytes(b"DATA")
            return file
        client = FakeClient(both)
        r = await asyncio.gather(self.dl(client), self.dl(client))
        self.assertEqual(len(set(started)), 2, "две попытки пишут в один temp-путь")
        self.assertNotIn(str(self.final), started)
        self.assertEqual(r[0], "1_sample.bin")
        self.assertEqual(self.final.read_bytes(), b"DATA")

    async def test_ttl_cleanup_does_not_destroy_active_attempt_p2_11(self):
        self.snap.MEDIA_CACHE = self.cache_root
        self.snap.MEDIA_TTL_HOURS = 1
        seen = {}

        async def slow(client, file):
            Path(file).write_bytes(b"DA")
            old = time.time() - 100 * 3600
            os.utime(file, (old, old))
            self.snap.cleanup_old_media()
            seen["alive"] = Path(file).exists()
            Path(file).write_bytes(b"DATA")
            return file
        client = FakeClient(slow)
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertTrue(seen["alive"], "TTL-уборка удалила файл активной попытки")
        self.assertEqual(self.final.read_bytes(), b"DATA")


class TempBoundaries(MediaBase):
    def outside(self, name="outside"):
        d = Path(self._tmp.name + "-" + name)
        d.mkdir()
        self.addCleanup(shutil.rmtree, d, True)
        return d

    async def test_symlinked_temp_root_is_refused_without_writes_u7(self):
        out_a, out_b = self.outside("a"), self.outside("b")
        self.chat.mkdir(parents=True)
        os.symlink(out_a, self.chat / ".telegram-media-tmp")        # temp-корень внутри каталога чата
        os.symlink(out_b, self.cache_root / ".telegram-media-tmp")  # и в корне кэша
        client = FakeClient(ok_behavior)
        result = await self.dl(client)
        self.assertIsNone(result, "загрузка опубликована через симлинк temp-корня")
        self.assertFalse(self.final.exists())
        self.assertEqual(list(out_a.rglob("*")) + list(out_b.rglob("*")), [], "запись вышла наружу по симлинку")

    async def test_cleanup_does_not_follow_symlinked_temp_root_u7(self):
        out = self.outside()
        victim = out / "victim.bin"
        victim.write_bytes(b"KEEP")
        old = time.time() - 100 * 3600
        os.utime(victim, (old, old))
        self.chat.mkdir(parents=True)
        os.symlink(out, self.chat / ".telegram-media-tmp")
        os.symlink(out, self.cache_root / ".telegram-media-tmp")
        self.snap.MEDIA_CACHE = self.cache_root
        self.snap.MEDIA_TTL_HOURS = 1
        self.snap.cleanup_old_media()
        self.assertEqual(victim.read_bytes(), b"KEEP", "уборка удалила файл вне каталога через симлинк")
        self.assertTrue(victim.exists())

    async def test_returned_path_outside_attempt_dir_is_not_published_or_moved_u7(self):
        out = self.outside()
        foreign = out / "foreign.bin"
        foreign.write_bytes(b"DATA")  # существующий файл снаружи, корректного размера

        async def returns_foreign(client, file):
            return str(foreign)
        client = FakeClient(returns_foreign)
        self.assertIsNone(await self.dl(client), "опубликован файл, возвращенный из-за пределов попытки")
        self.assertFalse(self.final.exists())
        self.assertEqual(foreign.read_bytes(), b"DATA", "внешний файл перемещен или изменен")
        self.assertEqual(self.files(), [])


class LegacyCache(MediaBase):
    async def test_prepared_truncated_file_with_known_size_is_replaced_p2_12(self):
        """У-3: известный размер не совпал - адресная инвалидация только этого файла."""
        self.chat.mkdir(parents=True)
        self.final.write_bytes(b"PART")
        neighbour = self.chat / "9_other.bin"
        neighbour.write_bytes(b"UNTOUCHED")

        async def full8(client, file):
            Path(file).write_bytes(b"COMPLETE")
            return file
        client = FakeClient(full8)
        name = await self.dl(client, media_msg(1, size=8))
        self.assertEqual(name, "1_sample.bin")
        self.assertEqual(client.calls, 1, "усеченный файл принят за cache hit")
        self.assertEqual(self.final.read_bytes(), b"COMPLETE")
        self.assertEqual(neighbour.read_bytes(), b"UNTOUCHED", "инвалидация затронула чужой файл")

    async def test_prepared_correct_file_with_known_size_is_reused_p2_12(self):
        self.chat.mkdir(parents=True)
        self.final.write_bytes(b"DATA")
        client = FakeClient(ok_behavior)
        self.assertEqual(await self.dl(client), "1_sample.bin")
        self.assertEqual(client.calls, 0)


# ================================================================== P2-11: общий helper для direct и owner

class DirectAndOwnerPaths(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.snap = load_script("telegram-snapshot.py", "cancel_direct_" + os.urandom(3).hex())
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.snap.MEDIA_CACHE = Path(self._tmp.name)
        self.cache = Path(self._tmp.name)

        async def rec_stub(client, m):
            return {"id": m.id}
        self.snap.message_to_record = rec_stub

    def direct_client(self, behavior):
        c = FakeClient(behavior)
        m = media_msg(5, name="s.bin")
        m.action, m.reply_to = None, None

        def iter_messages(entity, min_id=0, reverse=True):
            async def gen():
                yield m
            return gen()
        c.iter_messages = iter_messages
        return c

    def cache_files(self):
        return sorted(p.name for p in self.cache.rglob("*") if p.is_file())

    async def test_direct_fetch_new_error_and_retry_p2_11(self):
        c = self.direct_client(partial_then_oserror)
        out, _, _, downloaded = await self.snap.fetch_new(c, types.SimpleNamespace(id=77), 0)
        self.assertEqual(downloaded, [], "ошибочная загрузка попала в downloaded_media")
        self.assertEqual(self.cache_files(), [], "direct оставил усеченный файл")
        c.behavior = ok_behavior
        out, _, _, downloaded = await self.snap.fetch_new(c, types.SimpleNamespace(id=77), 0)
        self.assertEqual(downloaded, ["5_s.bin"])
        self.assertEqual(c.calls, 2)
        self.assertEqual(self.cache_files(), ["5_s.bin"])

    async def test_direct_fetch_new_cancel_leaves_no_ready_file_p2_11(self):
        c = self.direct_client(hang_after)
        task = asyncio.ensure_future(self.snap.fetch_new(c, types.SimpleNamespace(id=77), 0))
        await until(lambda: c.reached, "downloader")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertEqual(self.cache_files(), [], "после отмены direct остался готовый/усеченный файл")

    async def test_owner_events_cancel_in_download_redownloads_and_logs_no_details_p2_11(self):
        home_cache = Path.home() / ".cache"
        cache = Path(tempfile.mkdtemp(prefix="tgtest-owner-cancel-", dir=home_cache))
        self.addCleanup(shutil.rmtree, cache, True)
        client = FakeClient(hang_after)
        client.is_connected = lambda: True
        gates = Gates()
        a = make_adapter(gates=gates, project_config={"media_cache": str(cache)}, client=client,
                         snapshot=load_script("telegram-snapshot.py", "owner_media_" + os.urandom(3).hex()))
        a.snapshot.cleanup_old_media = Mock(return_value=0)
        a._record = fake_record_factory(gates)
        await a._enqueue(u_new(a, 5))
        records = []

        class H(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())
        h = H()
        logging.getLogger().addHandler(h)
        self.addCleanup(logging.getLogger().removeHandler, h)

        task = asyncio.ensure_future(a.events())
        await until(lambda: client.reached, "downloader")
        files = [p for p in cache.rglob("*") if p.is_file()]
        self.assertFalse([p for p in files if p.name == "5_s.bin"], "конечное имя видно во время загрузки")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([p.name for p in cache.rglob("*") if p.is_file()], [],
                         "owner оставил файл после отмены загрузки")
        client.behavior = partial_then_oserror
        got = await drain(a)
        self.assertEqual([(e["kind"], e["record"]["id"]) for e in got], [("new", 5)],
                         "событие потеряно после отмены загрузки медиа")
        self.assertEqual([p.name for p in cache.rglob("*") if p.is_file()], [],
                         "ошибка загрузки оставила усеченный файл")
        client.behavior = ok_behavior
        await a._enqueue(u_new(a, 5))
        await drain(a)
        self.assertEqual([p.name for p in cache.rglob("*") if p.is_file()], ["5_s.bin"])
        self.assertEqual(client.calls, 3, "повторы не загружали заново")
        text = "\n".join(records)
        self.assertNotIn("PRIVATE-DETAIL", text)
        self.assertNotIn("/private/path", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
