"""Additional crash, revision and catch-up checks for the mirror implementation."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (
    Base, FakeTelegram, NOW, read_status, refresh_request, run, write_file, write_package,
)
from test_telegram_session_owner_mirror import ev, ids_of, load, rec, topic_rec, write_mirror
from telegram_mirror import read_pair
from telegram_session_owner import Disconnected, Store


class MirrorLocal(Base):
    def setUp(self):
        super().setUp()
        self.path = self.env.projects / "chat1" / "result.json"
        self.dest = self.env.destination("d1", 1001, "chat1")

    def start(self, **kw):
        owner = self.env.make_owner("a", destinations=kw.pop("destinations", [self.dest]), **kw)
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def heartbeat(self):
        return load(self.env.root("a") / "heartbeat.json")["state"]

    def refresh(self, owner, request=None):
        request = request or refresh_request("a", "d1", now=owner.fake.clock.utc())
        write_package(self.env.root("a"), request)
        run(owner.step())
        return read_status(self.env.root("a"), request["request_id"])

    def test_revisions_survive_restart_and_reject_stale_edits_and_topics(self):
        write_mirror(self.path, [100])
        o = self.start()
        o.fake.tg.event_batches = [[ev("edit", rec(100, "B"), rev=(NOW, 2)),
                                    ev("topic", topic_rec(77, "B"), rev=(NOW, 2))]]
        run(o.step())
        before = self.path.read_bytes()
        run(o.close())
        tg = FakeTelegram("a")
        tg.event_batches = [[ev("edit", rec(100, "A"), rev=(NOW, 1)),
                             ev("topic", {"id": 77, "title": "A"}, rev=(NOW, 1))]]
        other = self.start(tg=tg)
        run(other.step())
        self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_revision_orders_edits_even_with_older_record_timestamp(self):
        write_mirror(self.path, [100])
        o = self.start()
        o.fake.tg.event_batches = [
            [ev("edit", rec(100, "B", edited_unixtime=str(int(NOW))), rev=(NOW, 1))],
            [ev("edit", rec(100, "C", edited_unixtime=str(int(NOW - 1))), rev=(NOW, 2))],
        ]
        run(o.step())
        run(o.step())
        msg = load(self.path)["messages"][0]
        self.assertEqual(msg["text"], "C")
        self.assertEqual(msg["edited_unixtime"], str(int(NOW - 1)))
        self.assertEqual([h["text"] for h in msg["edit_history"]], ["msg 100", "B"])

    def test_recent_cannot_skip_history_or_invent_edits_from_converter_drift(self):
        write_mirror(self.path, [100])
        tg = FakeTelegram("a")
        tg.history_data = [rec(101)]
        tg.recent_data = [rec(100, "converter drift"), rec(101), rec(103)]
        o = self.start(tg=tg)
        self.assertEqual(ids_of(self.path), [100, 101])
        self.assertEqual(load(self.path)["messages"][0], rec(100))
        tg.history_data = [rec(102), rec(103)]
        tg.recent_data = []
        self.refresh(o)
        self.assertEqual(ids_of(self.path), [100, 101, 102, 103])

    def test_reconnect_remains_degraded_through_history_and_recent(self):
        write_mirror(self.path, [100])
        o = self.start()
        tg = o.fake.tg
        tg.raises["events"] = Disconnected()
        run(o.step())
        tg.history_data = [rec(101), rec(102)]
        tg.event_batches = [[ev("new", rec(103))]]
        states = []
        tg.hooks["history"] = lambda: states.append(self.heartbeat())
        tg.hooks["recent"] = lambda: states.append(self.heartbeat())
        network = len(tg.calls)
        run(o.step())
        self.assertEqual(len(tg.calls), network)
        o.fake.clock.advance(15)
        run(o.step())
        self.assertEqual(states, ["degraded", "degraded"])
        self.assertEqual(self.heartbeat(), "ready")
        self.assertEqual([c[2] for c in tg.calls if c[0] == "history"], [100, 100])
        self.assertEqual(ids_of(self.path), [100, 101, 102, 103])

    def test_event_arriving_during_history_is_merged_after_boundary(self):
        write_mirror(self.path, [100])
        tg = FakeTelegram("a")
        tg.history_data = [rec(101), rec(102)]
        tg.hooks["history"] = lambda: tg.event_batches.append([ev("new", rec(103))])
        o = self.start(tg=tg)
        self.assertEqual(ids_of(self.path), [100, 101, 102])
        run(o.step())
        self.assertEqual(ids_of(self.path), [100, 101, 102, 103])

    def test_shared_peer_history_read_once_from_lowest_destination_cursor(self):
        write_mirror(self.path, [100])
        second = self.env.projects / "chat2" / "result.json"
        write_mirror(second, [90])
        tg = FakeTelegram("a")
        tg.history_data = [rec(95), rec(101)]
        self.start(tg=tg, destinations=[self.dest, self.env.destination("d2", 1001, "chat2")])
        self.assertEqual(len([c for c in tg.calls if c[0] == "resolve"]), 1)
        self.assertEqual([c[2] for c in tg.calls if c[0] == "history"], [90])
        self.assertEqual(ids_of(self.path), [100, 101])
        self.assertEqual(ids_of(second), [90, 95, 101])

    def test_failed_event_write_retains_batch_for_other_destination(self):
        write_mirror(self.path, [100])
        second = self.env.projects / "chat2" / "result.json"
        write_mirror(second, [100])
        o = self.start(destinations=[self.dest, self.env.destination("d2", 1001, "chat2")])
        o.fake.store.fail_on[("atomic_write", "chat1/result.json")] = ("before", OSError("disk"))
        o.fake.tg.event_batches = [[ev("new", rec(101))]]
        run(o.step())
        self.assertEqual(self.heartbeat(), "degraded")
        self.assertEqual(ids_of(self.path), [100])
        with self.assertRaises(RuntimeError):
            with read_pair(self.path.parent):
                pass
        o.fake.clock.advance(15)
        run(o.step())
        self.assertEqual(ids_of(self.path), [100, 101])
        self.assertEqual(ids_of(second), [100, 101])
        with read_pair(self.path.parent):
            self.assertEqual(ids_of(self.path), [100, 101])

    def test_committed_checkpoint_replayed_after_status_loss_without_rotating_again(self):
        write_mirror(self.path, [100])
        o = self.start()
        o.fake.tg.history_data = [rec(101)]
        real_write = o.fake.store.atomic_write
        failed = []

        def lose_status(path, data):
            if path.parent.name == "status" and json.loads(data).get("state") == "refreshed" and not failed:
                failed.append(True)
                raise OSError("status lost")
            real_write(path, data)

        o.fake.store.atomic_write = lose_status
        request = refresh_request("a", "d1")
        self.assertEqual(self.refresh(o, request)["state"], "queued")
        state = load(self.path.parent / ".telegram-owner-state.json")
        saved = copy.deepcopy(state["refreshes"][request["request_id"]])
        o.fake.tg.event_batches = [[ev("new", rec(102))]]
        o.fake.clock.advance(15)
        run(o.step())
        status = read_status(self.env.root("a"), request["request_id"])
        self.assertEqual(status["state"], "refreshed")
        self.assertEqual(status["result"], saved)
        self.assertEqual(ids_of(self.path.with_name("result.prev.json")), [100])
        self.assertEqual(ids_of(self.path), [100, 101, 102])
        self.refresh(o)
        self.assertEqual(ids_of(self.path.with_name("result.prev.json")), [100, 101])

    def test_orphan_rename_waits_for_catchup_then_applies_to_real_root(self):
        write_mirror(self.path, [100])
        o = self.start()
        o.fake.tg.event_batches = [[ev("topic", {"id": 77, "title": "Renamed"})]]
        run(o.step())
        # Спека пакета 4, У-3: между фоновыми сверками heartbeat ready.
        self.assertEqual(self.heartbeat(), "ready")
        self.assertEqual(load(self.path)["topics"], [])
        o.fake.tg.recent_data = [{"kind": "topic", "record": topic_rec(77), "revision": [NOW - 1, 0]}]
        o.fake.tg.raises["recent"] = Disconnected()
        run(o.step())
        self.assertEqual(self.heartbeat(), "degraded")
        o.fake.clock.advance(15)
        run(o.step())
        self.assertEqual(self.heartbeat(), "ready")
        self.assertEqual(load(self.path)["topics"], [topic_rec(77, "Renamed")])

    def test_legacy_backup_and_permissions_with_real_store(self):
        write_mirror(self.path, [100])
        data = load(self.path)
        del data["topics"]
        data["messages"].insert(0, {"id": 77, "type": "service", "action": "topic_created",
                                    "title": "Legacy", "date": "d", "date_unixtime": "1"})
        write_file(self.path, json.dumps(data))
        before = self.path.read_bytes()
        o = self.start(store=Store())
        self.assertEqual(self.path.with_name("result.pre-migration.json").read_bytes(), before)
        self.assertEqual(load(self.path)["topics"][0]["title"], "Legacy")
        o.fake.tg.event_batches = [[ev("new", rec(101))]]
        run(o.step())
        for path in self.path.parent.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_deltas_shared_lock_excludes_owner_pair_write(self):
        self.start(store=Store())
        with read_pair(self.path.parent):
            with self.assertRaises(BlockingIOError):
                Store().lock(self.path.parent / ".telegram-owner.lock")
        with Store().lock(self.path.parent / ".telegram-owner.lock"):
            pass

    def test_restart_replaces_old_ready_before_catchup(self):
        o = self.start()
        run(o.close())
        heartbeat = self.env.root("a") / "heartbeat.json"
        data = load(heartbeat)
        data["state"] = "ready"
        write_file(heartbeat, json.dumps(data))
        tg = FakeTelegram("a")
        states = []
        tg.hooks["history"] = lambda: states.append(self.heartbeat())
        self.start(tg=tg)
        self.assertEqual(states, ["degraded"])
        self.assertEqual(self.heartbeat(), "ready")

    def test_invalid_json_shapes_are_preserved(self):
        write_mirror(self.path, [100])
        for raw in ("null", "[]", '{"messages":[]}',
                    '{"name":"T","type":"personal_chat","id":true,"messages":[]}'):
            with self.subTest(raw=raw):
                write_file(self.path, raw)
                o = self.env.make_owner("a", destinations=[self.dest])
                self.assertNotEqual(run(o.start())["code"], "READY")
                self.assertEqual(self.path.read_text(), raw)
                run(o.close())

    def test_edit_only_checkpoint_preserves_boundary_and_identifier(self):
        write_mirror(self.path, [100])
        o = self.start()
        o.fake.tg.history_data = [rec(101)]
        first = self.refresh(o)
        prev = self.path.with_name("result.prev.json").read_bytes()
        o.fake.tg.event_batches = [[ev("edit", rec(101, "changed"))]]
        second = self.refresh(o)
        self.assertEqual(self.path.with_name("result.prev.json").read_bytes(), prev)
        self.assertEqual(first["result"]["checkpoint_id"], second["result"]["checkpoint_id"])
        self.assertEqual(load(self.path)["messages"][-1]["text"], "changed")

    def test_deleted_maximum_id_stays_cursor_after_restart(self):
        write_mirror(self.path, [100])
        o = self.start()
        o.fake.tg.event_batches = [[ev("delete", ids=[100])]]
        run(o.step())
        run(o.close())
        tg = FakeTelegram("a")
        self.start(tg=tg)
        self.assertEqual([c[2] for c in tg.calls if c[0] == "history"], [100])
        self.assertTrue(load(self.path)["messages"][0]["deleted"])

    def test_unknown_tmp_names_do_not_toggle_heartbeat_or_get_deleted(self):
        o = self.start()
        directory = self.env.root("a") / "tmp"
        (directory / "unknown-directory").mkdir(parents=True)
        write_file(directory / "unrelated.txt", "leave intact")
        write_file(directory / "unknown-directory" / "payload", "leave intact")
        heartbeat = o._heartbeat
        transitions = []
        def track(state):
            transitions.append(state)
            return heartbeat(state)
        o._heartbeat = track
        for _ in range(3):
            o.fake.clock.advance(3601)
            run(o.step())
            self.assertEqual(self.heartbeat(), "ready")
        self.assertNotIn("degraded", transitions)
        self.assertEqual((directory / "unrelated.txt").read_text(), "leave intact")
        self.assertEqual((directory / "unknown-directory" / "payload").read_text(), "leave intact")


if __name__ == "__main__":
    unittest.main(verbosity=2)
