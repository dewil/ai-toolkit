"""Overflow/local catch-up and canonical short-update filtering, offline."""
import json
import types
import unittest
from collections import deque

from test_telegram_session_owner_fakes import (
    Base, FakeTelegram, import_owner, read_status, refresh_request, run,
    send_request, write_package,
)
from test_telegram_session_owner_mirror import rec
from telegram_owner_telethon import Telegram


class ShortUpdates(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = Telegram.__new__(Telegram)
        a = self.adapter
        a.account = "a"
        a.client = types.SimpleNamespace(is_connected=lambda: True)
        names = ("UpdateDeleteChannelMessages", "UpdateDeleteMessages", "UpdateNewMessage",
                 "UpdateNewChannelMessage", "UpdateEditMessage", "UpdateEditChannelMessage",
                 "UpdatesTooLong", "UpdateChannelTooLong", "UpdateShortMessage", "UpdateShortChatMessage")
        a.types = types.SimpleNamespace(**{name: type(name, (), {}) for name in names})
        a.config = {"destinations": [{"peer": 5408957055}]}
        a._aliases = {5408957055: {"account": "a", "kind": "Chat", "id": 5408957055}}
        a._updates = deque()
        a._overflow, a._seq = False, 0

    async def short_events(self, name, **attrs):
        update = getattr(self.adapter.types, name)()
        for key, value in attrs.items():
            setattr(update, key, value)
        self.adapter._updates = deque([update])
        return await self.adapter.events()

    async def test_mirrored_basic_group_short_message_causes_overflow(self):
        events = await self.short_events("UpdateShortChatMessage", chat_id=5408957055, from_id=7, id=10)
        self.assertEqual([event["kind"] for event in events], ["overflow"])

    async def test_short_update_matches_resolved_entity_for_named_destination(self):
        self.adapter.config["destinations"][0]["peer"] = "alp"
        self.adapter._aliases = {"alp": {"account": "a", "kind": "Chat", "id": 5408957055}}
        events = await self.short_events("UpdateShortChatMessage", chat_id=5408957055, from_id=7, id=10)
        self.assertEqual([event["kind"] for event in events], ["overflow"])

    async def test_equal_id_of_different_peer_kind_is_ignored(self):
        events = await self.short_events("UpdateShortMessage", user_id=5408957055, id=10)
        self.assertEqual(events, [])

    async def test_cached_send_recipient_is_not_a_mirrored_destination(self):
        self.adapter._aliases[7] = {"account": "a", "kind": "User", "id": 7}
        events = await self.short_events("UpdateShortMessage", user_id=7, id=10)
        self.assertEqual(events, [])

    async def test_queue_overflow_still_requests_catchup(self):
        for _ in range(1001):
            await self.adapter._enqueue(object())
        self.assertEqual(await self.adapter.events(), [{"kind": "overflow"}])


class Catchup(Base):
    def started(self):
        owner = self.env.make_owner("a", destinations=[self.env.destination()])
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def state(self):
        return json.loads((self.env.root("a") / "heartbeat.json").read_bytes())["state"]

    def overflow(self, owner):
        owner.fake.tg.event_batches = [[{"kind": "overflow"}]]
        run(owner.step())
        self.assertEqual(self.state(), "degraded")

    def queued_send(self, owner):
        request = send_request(now=owner.fake.clock.utc())
        request["options"]["no_pace_check"] = True
        write_package(self.env.root("a"), request)
        return request

    def test_catchup_stays_degraded_until_mirrors_are_durable_then_sends(self):
        owner = self.started()
        tg = owner.fake.tg
        self.overflow(owner)
        request = self.queued_send(owner)
        tg.history_data = [rec(101)]
        for operation in ("history", "recent"):
            tg.hooks[operation] = lambda: self.assertEqual(self.state(), "degraded")
        mark = len(tg.calls)
        run(owner.step())
        self.assertEqual(self.state(), "ready")
        self.assertEqual(tg.names()[mark:mark + 2], ["history", "recent"])
        self.assertNotIn("connect", tg.names()[mark:])
        self.assertNotIn("disconnect", tg.names()[mark:])
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "sent")

    def test_repeated_overflows_do_not_introduce_reconnect_pause(self):
        owner = self.started()
        tg = owner.fake.tg
        mark = len(tg.calls)
        for _ in range(3):
            self.overflow(owner)
            run(owner.step())
            self.assertEqual(self.state(), "ready")
            owner.fake.clock.advance(0.1)
        names = tg.names()[mark:]
        self.assertEqual(names.count("history"), 3)
        self.assertNotIn("connect", names)
        self.assertNotIn("disconnect", names)

    def test_local_event_error_logs_local_reason_without_details_or_reconnect(self):
        owner = self.started()
        tg = owner.fake.tg
        tg.raises["events"] = ValueError("PRIVATE-LOCAL-DETAIL")
        with self.assertLogs("telegram_session_owner", level="INFO") as logs:
            run(owner.step())
            self.assertEqual(self.state(), "degraded")
            run(owner.step())
        text = "\n".join(logs.output)
        self.assertIn("state=degraded reason=local", text)
        self.assertNotIn("PRIVATE-LOCAL-DETAIL", text)
        self.assertEqual(self.state(), "ready")
        self.assertEqual(tg.names().count("history"), 2)
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())

    def test_local_catchup_error_retries_without_reconnect_or_15_second_pause(self):
        owner = self.started()
        tg = owner.fake.tg
        self.overflow(owner)
        tg.raises["history"] = OSError("PRIVATE-MIRROR-DETAIL")
        run(owner.step())
        self.assertEqual(self.state(), "degraded")
        run(owner.step())
        self.assertEqual(self.state(), "ready")
        self.assertEqual(tg.names().count("connect"), 1)
        self.assertNotIn("disconnect", tg.names())

    def test_network_failure_during_overflow_catchup_uses_network_backoff(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        self.overflow(owner)
        request = self.queued_send(owner)
        tg.raises["history"] = import_owner().Disconnected("PRIVATE-NETWORK-DETAIL")
        run(owner.step())
        self.assertEqual(self.state(), "degraded")
        mark = len(tg.calls)
        clock.advance(14)
        run(owner.step())
        self.assertEqual(len(tg.calls), mark)
        clock.advance(1)
        run(owner.step())
        self.assertEqual(tg.names()[mark:mark + 4], ["disconnect", "connect", "history", "recent"])
        self.assertEqual(self.state(), "ready")
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["state"], "sent")
        self.assertEqual(len(tg.sends()), 1)

    def test_floodwait_during_overflow_catchup_waits_without_reconnect(self):
        owner = self.started()
        tg, clock = owner.fake.tg, owner.fake.clock
        self.overflow(owner)
        tg.raises["history"] = import_owner().FloodWait(25)
        run(owner.step())
        mark = len(tg.calls)
        clock.advance(24)
        run(owner.step())
        self.assertEqual(len(tg.calls), mark)
        self.assertEqual(self.state(), "degraded")
        clock.advance(1)
        run(owner.step())
        self.assertEqual(self.state(), "ready")
        self.assertEqual(tg.names()[mark:mark + 2], ["history", "recent"])
        self.assertNotIn("connect", tg.names()[mark:])
        self.assertNotIn("disconnect", tg.names()[mark:])

    def test_refresh_floodwait_transition_logs_network_subtype(self):
        owner = self.started()
        request = refresh_request(now=owner.fake.clock.utc())
        write_package(self.env.root("a"), request)
        owner.fake.tg.raises["history"] = import_owner().FloodWait(25)
        with self.assertLogs("telegram_session_owner", level="WARNING") as logs:
            run(owner.step())
        transitions = [line for line in logs.output if "OWNER_STATE" in line]
        self.assertEqual(len(transitions), 1)
        self.assertIn("reason=network subtype=FLOOD_WAIT", transitions[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
