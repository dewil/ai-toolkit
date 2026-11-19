#!/usr/bin/env python3
"""RED: зеркала result.json, события, catch-up, refresh/checkpoint (F4-F10, F2 pull-one, F21 overflow).

Курсор = максимальный id в result.json зеркала (как у текущего snapshot, last_message_id) -
см. вопросы к спеке.

Запуск: python3 tests/test_telegram_session_owner_mirror.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeStore, FakeTelegram, NOW, import_owner, read_status, refresh_request, run, write_package,
)

PEER = {"account": "a", "kind": "User", "id": 1001, "username": None}


def rec(i, text=None, **extra):
    r = {"id": i, "type": "message", "date": "2026-11-19T10:00:%02d" % (i % 60),
         "date_unixtime": str(1797674400 + i), "from": "Иван", "from_id": "user1",
         "text": text if text is not None else f"msg {i}", "text_entities": []}
    r.update(extra)
    return r


def ev(kind, record=None, ids=None, rev=(NOW, 1), peer=PEER):
    e = {"kind": kind, "record": record, "ids": ids or [], "revision": list(rev)}
    if peer is not None:
        e["peer"] = peer
    return e


def write_mirror(path: Path, ids, chat_id=1001, **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"name": "T", "type": "personal_chat", "id": chat_id, "topics": [],
            "messages": [rec(i) for i in ids]}
    data.update(extra)
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def ids_of(path: Path):
    return [m["id"] for m in load(path)["messages"]]


class MirrorCase(Base):
    def setUp(self):
        super().setUp()
        self.mirror = self.env.projects / "chat1" / "result.json"

    def owner(self, dests=None, **kw):
        dests = dests if dests is not None else [self.env.destination("d1", 1001, "chat1")]
        o = self.env.make_owner("a", destinations=dests, **kw)
        return o

    def started(self, **kw):
        o = self.owner(**kw)
        r = run(o.start())
        self.assertEqual(r["code"], "READY", r)
        self.addCleanup(lambda: run(o.close()))
        return o

    def step_all(self, o, n=6):
        for _ in range(n):
            if not run(o.step()):
                break

    def refresh(self, o, did="d1", **over):
        req = refresh_request("a", did, now=o.fake.clock.utc())
        req.update(over)
        write_package(self.env.root("a"), req)
        self.step_all(o)
        return read_status(self.env.root("a"), req["request_id"])


class Dest(MirrorCase):
    def test_relative_dest_from_project_root(self):  # F4
        o = self.owner()
        o.fake.tg.history_data = [rec(1)]
        run(o.start())
        self.addCleanup(lambda: run(o.close()))
        self.assertEqual(ids_of(self.mirror), [1])

    def test_absolute_dest(self):  # F4
        target = self.env.mirrors / "abs" / "chat"
        o = self.owner([self.env.destination("d1", 1001, str(target))])
        o.fake.tg.history_data = [rec(1)]
        run(o.start())
        self.addCleanup(lambda: run(o.close()))
        self.assertEqual(ids_of(target / "result.json"), [1])

    def test_same_basename_isolated(self):  # F4
        d1 = self.env.destination("d1", 1001, "x/chat")
        d2 = self.env.destination("d2", 2002, "y/chat")
        tg = FakeTelegram("a")
        tg.history_data = [rec(1)]
        o = self.owner([d1, d2], tg=tg)
        run(o.start())
        self.addCleanup(lambda: run(o.close()))
        self.assertTrue((self.env.projects / "x" / "chat" / "result.json").exists())
        self.assertTrue((self.env.projects / "y" / "chat" / "result.json").exists())
        self.assertEqual(load(self.env.projects / "x" / "chat" / "result.json")["id"], 1001)
        self.assertEqual(load(self.env.projects / "y" / "chat" / "result.json")["id"], 2002)

    def test_conflicting_peer_for_same_dest_rejected(self):  # F4
        d1 = self.env.destination("d1", 1001, "chat1")
        d2 = self.env.destination("d2", 2002, "chat1")
        o = self.owner([d1, d2])
        r = run(o.start())
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_relative_dest_escaping_project_rejected(self):  # F4
        o = self.owner([self.env.destination("d1", 1001, "../outside")])
        r = run(o.start())
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))

    def test_foreign_or_broken_json_not_overwritten(self):  # F7/F4
        write_mirror(self.mirror, [1, 2], chat_id=999)
        before = self.mirror.read_bytes()
        o = self.owner()
        o.fake.tg.history_data = [rec(3)]
        run(o.start())
        self.addCleanup(lambda: run(o.close()))
        self.step_all(o)
        self.assertEqual(self.mirror.read_bytes(), before)
        self.mirror.write_text("{broken")
        o2 = self.owner()
        run(o2.start())
        self.addCleanup(lambda: run(o2.close()))
        self.assertEqual(self.mirror.read_text(), "{broken")


class Cursor(MirrorCase):
    def test_cursor_history_events_each_id_once(self):  # F5
        write_mirror(self.mirror, [98, 99, 100])
        tg = FakeTelegram("a")
        tg.history_data = [rec(101), rec(102)]
        tg.event_batches = [[ev("new", rec(102), rev=(NOW, 1)), ev("new", rec(103), rev=(NOW, 2))]]
        o = self.started(tg=tg)
        self.step_all(o)
        self.assertEqual(ids_of(self.mirror), [98, 99, 100, 101, 102, 103])
        hist = [c for c in tg.calls if c[0] == "history"]
        self.assertEqual(hist[0][2], 100)  # after_id = курсор

    def test_bootstrap_header_and_history_from_zero(self):  # F7/F9
        tg = FakeTelegram("a")
        tg.history_data = [rec(1), rec(2)]
        o = self.started(tg=tg)
        data = load(self.mirror)
        self.assertEqual((data["type"], data["id"]), ("personal_chat", 1001))
        self.assertEqual(set(data) >= {"name", "type", "id", "topics", "messages"}, True)
        self.assertEqual([c for c in tg.calls if c[0] == "history"][0][2], 0)

    def test_peer_read_once_per_destination(self):  # спека: peer читается один раз
        o = self.started()
        self.step_all(o)
        self.assertEqual(len([c for c in o.fake.tg.calls if c[0] == "resolve" and c[2] == 1001]), 1)


class Format(MirrorCase):
    def test_record_fields_preserved_verbatim(self):  # F7
        write_mirror(self.mirror, [1])
        special = [
            rec(2, text="Привет, мир! ёж 🚀"),
            rec(3, text=[{"type": "bold", "text": "жирный"}, " обычный"]),
            rec(4, topic_id=77),
            rec(5, reply_to_message_id=2),
            rec(6, file_name="doc.pdf", file_size=10, mime_type="application/pdf"),
            rec(7, edited="2026-11-19T11:00:00", edited_unixtime="1797678000"),
        ]
        tg = FakeTelegram("a")
        tg.history_data = special
        o = self.started(tg=tg)
        got = {m["id"]: m for m in load(self.mirror)["messages"]}
        for r in special:
            self.assertEqual(got[r["id"]], r)
        raw = self.mirror.read_text(encoding="utf-8")
        self.assertIn("Привет", raw)  # ensure_ascii=False как у текущего зеркала

    def test_edit_event_updates_text_and_edit_history(self):  # F7/F8
        write_mirror(self.mirror, [100])
        tg = FakeTelegram("a")
        tg.event_batches = [[ev("edit", rec(100, text="новый", edited="2026-11-19T11:00:00",
                                            edited_unixtime="1797678000"), rev=(NOW, 1))]]
        o = self.started(tg=tg)
        self.step_all(o)
        m = load(self.mirror)["messages"][0]
        self.assertEqual(m["text"], "новый")
        self.assertEqual(m["edit_history"][0]["text"], "msg 100")

    def test_delete_event_marks_deleted_keeps_record(self):  # F7
        write_mirror(self.mirror, [100, 101])
        tg = FakeTelegram("a")
        tg.event_batches = [[ev("delete", ids=[101], rev=(NOW, 1))]]
        o = self.started(tg=tg)
        self.step_all(o)
        msgs = {m["id"]: m for m in load(self.mirror)["messages"]}
        self.assertTrue(msgs[101]["deleted"])
        self.assertFalse(msgs[100].get("deleted"))

    def test_media_false_no_upload_download(self):  # F7 "Media=false без загрузки"
        d = self.env.destination("d1", 1001, "chat1")
        d["project_config"] = {"media": False}
        tg = FakeTelegram("a")
        tg.history_data = [rec(1, file_name="a.bin")]
        o = self.started(dests=[d], tg=tg)
        self.assertEqual(tg.uploads(), [])


class Revisions(MirrorCase):
    def edit(self, text, rev):
        return ev("edit", rec(100, text=text, edited="2026-11-19T11:00:00",
                              edited_unixtime=str(int(rev[0]))), rev=rev)

    def run_batches(self, batches):
        write_mirror(self.mirror, [100])
        tg = FakeTelegram("a")
        tg.event_batches = batches
        o = self.started(tg=tg)
        self.step_all(o, n=10)
        return load(self.mirror)["messages"][0]

    def test_out_of_order_revision_older_ignored(self):  # F8
        m = self.run_batches([[self.edit("B", (NOW, 2))], [self.edit("A", (NOW, 1))]])
        self.assertEqual(m["text"], "B")

    def test_same_second_ordered_by_sequence_in_one_batch(self):  # F8
        m = self.run_batches([[self.edit("C", (NOW, 3)), self.edit("B", (NOW, 2))]])
        self.assertEqual(m["text"], "C")

    def test_equal_revision_is_duplicate(self):  # F8
        m = self.run_batches([[self.edit("B", (NOW, 2))], [self.edit("B", (NOW, 2))]])
        self.assertEqual(m["text"], "B")
        self.assertEqual(len(m.get("edit_history", [])), 1)

    def test_newer_unix_time_wins_over_higher_sequence(self):  # F8
        m = self.run_batches([[self.edit("old", (NOW, 9))], [self.edit("new", (NOW + 1, 1))]])
        self.assertEqual(m["text"], "new")

    def test_duplicate_delete_safe_and_peerless_delete_ignored(self):  # F8
        write_mirror(self.mirror, [100, 101])
        tg = FakeTelegram("a")
        tg.event_batches = [[ev("delete", ids=[101], rev=(NOW, 1)),
                             ev("delete", ids=[101], rev=(NOW, 2)),
                             ev("delete", ids=[100], rev=(NOW, 3), peer=None)]]
        o = self.started(tg=tg)
        self.step_all(o)
        msgs = {m["id"]: m for m in load(self.mirror)["messages"]}
        self.assertTrue(msgs[101]["deleted"])
        self.assertFalse(msgs[100].get("deleted"))

    def test_event_for_other_peer_or_account_ignored(self):  # F8/F25
        write_mirror(self.mirror, [100])
        before = self.mirror.read_bytes()
        tg = FakeTelegram("a")
        other = {"account": "a", "kind": "User", "id": 555, "username": None}
        wrong_acc = {"account": "b", "kind": "User", "id": 1001, "username": None}
        tg.event_batches = [[ev("new", rec(101), peer=other), ev("new", rec(102), peer=wrong_acc)]]
        o = self.started(tg=tg)
        self.step_all(o)
        self.assertEqual(ids_of(self.mirror), [100])


class CatchUp(MirrorCase):
    def test_long_downtime_beyond_edit_window_pulls_all_by_id(self):  # F9
        write_mirror(self.mirror, [100])
        tg = FakeTelegram("a")
        tg.history_data = [rec(i) for i in range(101, 181)]  # 80 > EDIT_WINDOW=50
        tg.recent_data = tg.history_data[-50:]
        o = self.started(tg=tg)
        self.assertEqual(ids_of(self.mirror), list(range(100, 181)))

    def test_empty_recent_deletes_nothing(self):  # F9
        write_mirror(self.mirror, list(range(1, 11)))
        tg = FakeTelegram("a")
        tg.recent_data = []
        o = self.started(tg=tg)
        self.refresh(o)
        self.assertFalse(any(m.get("deleted") for m in load(self.mirror)["messages"]))

    def test_missing_in_window_marked_outside_untouched(self):  # F9
        write_mirror(self.mirror, list(range(1, 61)))
        tg = FakeTelegram("a")
        tg.recent_data = [rec(i) for i in range(11, 61) if i != 30]  # окно 50, 30 пропал
        o = self.started(tg=tg)
        self.refresh(o)
        msgs = {m["id"]: m for m in load(self.mirror)["messages"]}
        self.assertTrue(msgs[30].get("deleted"))
        self.assertFalse(msgs[5].get("deleted"))  # вне окна: полнота не обещается
        self.assertEqual(sum(1 for m in msgs.values() if m.get("deleted")), 1)
        rcalls = [c for c in tg.calls if c[0] == "recent"]
        self.assertTrue(all(c[2] == 50 for c in rcalls))

    def test_overflow_event_triggers_reconcile(self):  # F21
        write_mirror(self.mirror, [100])
        tg = FakeTelegram("a")
        tg.event_batches = [[{"kind": "overflow", "peer": None, "record": None, "ids": [], "revision": [NOW, 1]}]]
        o = self.started(tg=tg)
        before = len([c for c in tg.calls if c[0] == "history"])
        tg.history_data = [rec(101)]
        self.step_all(o)
        self.assertGreater(len([c for c in tg.calls if c[0] == "history"]), before)
        self.assertEqual(o.fake.clock.async_sleeps, [])
        self.assertIn(101, ids_of(self.mirror))


class Checkpoint(MirrorCase):
    def prev(self):
        p = self.mirror.with_name("result.prev.json")
        return p

    def test_events_do_not_rotate_prev(self):  # F10/Q2
        write_mirror(self.mirror, [100])
        tg = FakeTelegram("a")
        tg.event_batches = [[ev("new", rec(101))]]
        o = self.started(tg=tg)
        self.step_all(o)
        self.assertFalse(self.prev().exists())

    def test_refresh_result_shape(self):  # F2/F10
        write_mirror(self.mirror, [100])
        o = self.started()
        st = self.refresh(o)
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("refreshed", "REFRESHED", 0))
        self.assertEqual(set(st["result"]) >= {"destination_id", "cursor", "checkpoint_id", "updated_at"}, True)
        self.assertEqual(st["result"]["destination_id"], "d1")
        self.assertEqual(st["result"]["cursor"], 100)

    def test_second_checkpoint_prev_is_previous_boundary(self):  # F10
        write_mirror(self.mirror, [100])
        tg = FakeTelegram("a")
        o = self.started(tg=tg)
        tg.history_data = [rec(101)]
        self.refresh(o)
        self.assertEqual(max(ids_of(self.mirror)), 101)
        tg.event_batches = [[ev("new", rec(102), rev=(NOW + 1, 1))]]
        o.fake.clock.advance(100)
        self.refresh(o)
        self.assertEqual(max(m["id"] for m in load(self.prev())["messages"]), 101)
        self.assertEqual(max(ids_of(self.mirror)), 102)

    def test_repeat_checkpoint_does_not_shift_window(self):  # F10
        write_mirror(self.mirror, [100])
        tg = FakeTelegram("a")
        o = self.started(tg=tg)
        tg.history_data = [rec(101)]
        self.refresh(o)
        tg.event_batches = [[ev("new", rec(102), rev=(NOW + 1, 1))]]
        o.fake.clock.advance(100)
        self.refresh(o)
        prev_before = self.prev().read_bytes()
        o.fake.clock.advance(100)
        self.refresh(o)  # нет нового: повтор
        self.assertEqual(self.prev().read_bytes(), prev_before)

    def test_crash_between_pair_writes_recovers_consistent_pair(self):  # F10
        write_mirror(self.mirror, [100])
        store = FakeStore()
        tg = FakeTelegram("a")
        o = self.started(store=store, tg=tg)
        tg.history_data = [rec(101)]
        self.refresh(o)
        tg.event_batches = [[ev("new", rec(102), rev=(NOW + 1, 1))]]
        store.fail_on[("atomic_write", "result.json")] = ("before", OSError("crash"))
        o.fake.clock.advance(100)
        req = refresh_request("a", "d1", now=o.fake.clock.utc())
        write_package(self.env.root("a"), req)
        try:
            self.step_all(o)
        except OSError:
            pass
        run(o.close())
        o2 = self.owner(tg=tg, clock=o.fake.clock)
        run(o2.start())
        self.addCleanup(lambda: run(o2.close()))
        st = self.refresh(o2)
        self.assertEqual(st["state"], "refreshed")
        cur = load(self.mirror)
        prv = load(self.prev())
        self.assertGreaterEqual(max(m["id"] for m in cur["messages"]), 102)
        self.assertLessEqual(max(m["id"] for m in prv["messages"]), max(m["id"] for m in cur["messages"]))

    def test_refresh_unregistered_destination(self):  # F2
        o = self.started()
        st = self.refresh(o, did="nope")
        self.assertEqual((st["state"], st["code"], st["exit_code"]), ("rejected", "DESTINATION_NOT_REGISTERED", 2))

    def test_refresh_foreign_account_rejected(self):  # F25
        o = self.started()
        req = refresh_request("b", "d1")
        write_package(self.env.root("a"), req)
        self.step_all(o)
        st = read_status(self.env.root("a"), req["request_id"])
        self.assertEqual((st["code"], st["exit_code"]), ("ACCOUNT_NOT_MANAGED", 2))


class Selector(MirrorCase):
    def sel_refresh(self, o, peer, dest):
        req = refresh_request("a", "x", now=o.fake.clock.utc())
        del req["destination_id"]
        req["selector"] = {"peer": peer, "dest": str(dest)}
        write_package(self.env.root("a"), req)
        self.step_all(o)
        return read_status(self.env.root("a"), req["request_id"])

    def test_registered_account_peer_dest_refreshes(self):  # F2 pull-one
        write_mirror(self.mirror, [100])
        o = self.started()
        st = self.sel_refresh(o, 1001, self.mirror.parent)
        self.assertEqual((st["code"], st["exit_code"]), ("REFRESHED", 0))
        self.assertEqual(st["result"]["destination_id"], "d1")

    def test_unregistered_path_or_peer_denied_no_mirror_created(self):  # F2 pull-one/Q6
        o = self.started()
        arbitrary = self.base / "anywhere" / "chat"
        st = self.sel_refresh(o, 1001, arbitrary)
        self.assertEqual((st["code"], st["exit_code"]), ("DESTINATION_NOT_REGISTERED", 2))
        self.assertIn("Зарегистрируйте account/chat_id/путь в destinations owner", st["next_step"])
        self.assertFalse(arbitrary.exists())
        st = self.sel_refresh(o, 9999, self.mirror.parent)
        self.assertEqual(st["code"], "DESTINATION_NOT_REGISTERED")

    def test_ambiguous_selector_invalid_config(self):  # F2
        d1 = self.env.destination("d1", 1001, "chat1")
        d2 = self.env.destination("d2", 1001, "chat1")
        o = self.owner([d1, d2])
        r = run(o.start())
        if r["code"] == "READY":
            self.addCleanup(lambda: run(o.close()))
            st = self.sel_refresh(o, 1001, self.mirror.parent)
            self.assertEqual((st["code"], st["exit_code"]), ("INVALID_CONFIG", 2))
        else:
            self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))


class Crash(MirrorCase):
    def test_failure_at_any_atomic_write_never_corrupts_or_loses(self):  # F6
        for n in range(1, 8):
            for when in ("before", "after"):
                with self.subTest(n=n, when=when):
                    mirror = self.env.projects / f"c{n}{when}" / "result.json"
                    write_mirror(mirror, [100])
                    store = FakeStore()
                    real = store.atomic_write
                    cnt = {"n": 0}

                    def flaky(path, data, real=real, cnt=cnt, n=n, when=when, store=store):
                        p = str(path)
                        if "heartbeat" in p or "/status/" in p:
                            return real(path, data)
                        cnt["n"] += 1
                        if cnt["n"] == n and when == "before":
                            raise OSError("crash before")
                        real(path, data)
                        if cnt["n"] == n and when == "after":
                            raise OSError("crash after")

                    store.atomic_write = flaky
                    dest = self.env.destination("d1", 1001, str(mirror.parent))
                    tg = FakeTelegram("a")
                    tg.history_data = [rec(101), rec(102)]
                    tg.event_batches = [[ev("new", rec(103))]]
                    o = self.env.make_owner("a", destinations=[dest], store=store, tg=tg)
                    try:
                        run(o.start())
                        self.step_all(o)
                    except OSError:
                        pass
                    try:
                        run(o.close())
                    except OSError:
                        pass
                    json.loads(mirror.read_text(encoding="utf-8"))  # целый JSON в любой точке
                    # перезапуск без сбоев: ничего не потеряно, дублей нет
                    tg2 = FakeTelegram("a")
                    tg2.history_data = [rec(101), rec(102), rec(103)]
                    store.release_all()
                    o2 = self.env.make_owner("a", destinations=[dest], tg=tg2)
                    r = run(o2.start())
                    self.assertEqual(r["code"], "READY", r)
                    run(o2.close())
                    self.assertEqual(ids_of(mirror), [100, 101, 102, 103])

    def test_second_destination_failure_keeps_first_updated(self):  # F6
        d1 = self.env.destination("d1", 1001, "c1")
        d2 = self.env.destination("d2", 2002, "c2")
        store = FakeStore()
        store.fail_on[("atomic_write", "c2")] = ("before", OSError("disk"))
        tg = FakeTelegram("a")
        tg.history_data = [rec(1)]
        o = self.env.make_owner("a", destinations=[d1, d2], store=store, tg=tg)
        try:
            run(o.start())
        except OSError:
            pass
        run(o.close())
        self.assertEqual(ids_of(self.env.projects / "c1" / "result.json"), [1])
        c2 = self.env.projects / "c2" / "result.json"
        self.assertTrue(not c2.exists() or json.loads(c2.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
