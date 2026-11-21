"""Additional send invariants with explicit permissions and consistent clocks."""
from datetime import datetime, timezone
import hashlib
import json
import os
import unittest

from test_telegram_session_owner_fakes import (
    Base, FakeRNG, FakeStore, FloodWait, NOW, import_client, import_owner,
    read_status, req_hash, run, send_request, write_file, write_heartbeat, write_package,
)


class SendLocal(Base):
    def owner(self, **kwargs):
        owner = self.env.make_owner(**kwargs)
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def deliver(self, owner, request):
        write_package(self.env.root("a"), request)
        run(owner.step())
        return read_status(self.env.root("a"), request["request_id"])

    def schedule(self, at, **over):
        result = {"at": at, "timezone": None, "exact_minute": False, "remind": False}
        result.update(over)
        return result

    def attachment(self, path):
        data = path.read_bytes()
        return {"source": str(path), "name": path.name, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def test_short_text_uses_formula_and_status_snapshots_all_peers(self):
        owner = self.owner(rng=FakeRNG(uniform=0))
        first = self.deliver(owner, send_request(text="Проверка"))
        self.assertAlmostEqual(first["pace"]["User:1"]["next_allowed_at"], NOW + 41.92)
        second = self.deliver(owner, send_request(peer=42))
        self.assertEqual(set(second["pace"]), {"User:1", "User:42"})
        denied = self.deliver(owner, send_request())
        self.assertEqual((denied["code"], denied["exit_code"]), ("PACE_TOO_EARLY", 3))
        self.assertAlmostEqual(denied["retry_after"], 41.92)
        self.assertEqual(owner.fake.rng.uniform_calls, [(0, 0.35), (0, 0.35)])
        # The local client consumes the owner's saved gate without another RNG.
        write_heartbeat(self.env.root("a"))
        reply = import_client().dry_run(self.env.root("a"), send_request(),
                                        installation_config=self.env.registry_path,
                                        clock=owner.fake.clock, store=owner.fake.store)
        self.assertEqual(reply["code"], "PACE_TOO_EARLY")

    def test_round_minute_includes_nonzero_seconds(self):
        owner = self.owner()
        reply = self.deliver(owner, send_request(schedule=self.schedule(NOW + 3607)))
        self.assertEqual((reply["code"], reply["exit_code"]), ("ROUND_MINUTE", 6))
        self.assertEqual(owner.fake.tg.sends(), [])

    def test_schedule_bounds_with_matching_ack_and_verification(self):
        owner = self.owner()
        for index, lead in enumerate((119, 120, 366 * 86400, 366 * 86400 + 1)):
            with self.subTest(lead=lead):
                at = NOW + lead
                owner.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [900], "at": at}]
                owner.fake.tg.scheduled_data = [{"id": 900, "at": at, "text": "Проверка"}]
                reply = self.deliver(owner, send_request(peer=100 + index,
                    schedule=self.schedule(at, exact_minute=True)))
                self.assertEqual(reply["state"], "scheduled" if lead in (120, 366 * 86400) else "rejected")

    def test_iana_normalized_only_in_rpc_copy(self):
        owner = self.owner()
        at = datetime(2027, 1, 16, 9, 7, tzinfo=timezone.utc).timestamp()
        request = send_request(schedule=self.schedule("2027-01-16T10:07:00", timezone="Europe/Berlin"))
        owner.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [901], "at": at}]
        owner.fake.tg.scheduled_data = [{"id": 901, "at": at, "text": request["text"]}]
        reply = self.deliver(owner, request)
        self.assertEqual(reply["result"]["verification"], "scheduled_verified")
        outgoing = owner.fake.tg.sends()[0][1]
        self.assertEqual(outgoing["schedule"]["at"], at)
        self.assertIsNone(outgoing["schedule"]["timezone"])
        saved = json.loads((self.env.root("a") / "outbox" / request["request_id"] / "request.json").read_bytes())
        self.assertEqual(saved, request)
        self.assertEqual(reply["request_hash"], req_hash(request))

    def test_flood_retry_status_utc_and_monotonic_deadline(self):
        owner = self.owner()
        owner.fake.tg.send_results = [FloodWait(30)]
        request = send_request()
        reply = self.deliver(owner, request)
        self.assertEqual(reply["state"], "queued")
        self.assertTrue(reply["retry_after_utc"].endswith("Z"))
        self.assertEqual(datetime.fromisoformat(reply["retry_after_utc"]).timestamp(), NOW + 30)
        owner.fake.clock._utc += 1000  # wall-clock jump cannot end the RPC embargo
        calls = len(owner.fake.tg.calls)
        run(owner.step())
        self.assertEqual(len(owner.fake.tg.calls), calls)
        owner.fake.clock._mono += 30
        run(owner.step())
        self.assertEqual(read_status(self.env.root("a"), request["request_id"])["code"], "SENT")
        self.assertEqual(owner.fake.rng.uniform_calls, [(0, 0.35)])

    def test_expiry_during_flood_is_local_and_does_not_wait_for_rpc_deadline(self):
        owner = self.owner()
        owner.fake.tg.send_results = [FloodWait(60)]
        request = send_request(expires_at=NOW + 20)
        self.deliver(owner, request)
        calls = len(owner.fake.tg.calls)
        owner.fake.clock.advance(20)
        run(owner.step())
        reply = read_status(self.env.root("a"), request["request_id"])
        self.assertEqual((reply["code"], reply["exit_code"]), ("REQUEST_EXPIRED", 2))
        self.assertEqual(len(owner.fake.tg.calls), calls)

    def test_remind_api_ack_succeeds_without_queue_verification(self):
        owner = self.owner()
        owner.fake.tg.send_results = [{"message_ids": [], "scheduled_ids": [902], "at": NOW + 181}]
        owner.fake.tg.hooks["send"] = lambda: owner.fake.clock.advance(2)
        reply = self.deliver(owner, send_request(schedule=self.schedule(None, remind=True)))
        self.assertEqual((reply["state"], reply["code"], reply["exit_code"]),
                         ("scheduled", "SCHEDULED", 0))
        self.assertEqual(reply["result"], {"message_ids": [], "scheduled_ids": [902],
                                         "at": NOW + 181, "verification": "api_ack"})
        self.assertEqual(reply["retain_until"], NOW + 181 + 7 * 86400)
        run(owner.step())
        self.assertEqual(len(owner.fake.tg.sends()), 1)

    def test_remind_api_ack_window_boundaries_and_slow_response(self):
        for jitter, response_delay in ((1, 0), (59, 0), (1, 70)):
            with self.subTest(jitter=jitter, response_delay=response_delay):
                owner = self.owner(rng=FakeRNG(randint=jitter))
                at = NOW + 180 + jitter
                owner.fake.tg.send_results = [{"scheduled_ids": [902], "at": at}]
                owner.fake.tg.hooks["send"] = lambda: owner.fake.clock.advance(response_delay)
                # A queue read is unnecessary even when it would fail.
                owner.fake.tg.raises["scheduled"] = RuntimeError("unavailable")
                reply = self.deliver(owner, send_request(schedule=self.schedule(None, remind=True)))
                self.assertEqual((reply["state"], reply["code"], reply["exit_code"]),
                                 ("scheduled", "SCHEDULED", 0))
                self.assertEqual(reply["result"]["verification"], "api_ack")
                self.assertEqual(reply["result"]["at"], at)
                run(owner.close())

    def test_remind_api_ack_allows_processing_to_minimum_send_lead(self):
        for processing_delay in (1, 60, 61):
            with self.subTest(processing_delay=processing_delay):
                owner = self.owner(rng=FakeRNG(randint=1))
                owner.fake.tg.hooks["resolve"] = lambda: owner.fake.clock.advance(processing_delay)
                owner.fake.tg.send_results = [{"scheduled_ids": [902], "at": NOW + 181}]
                reply = self.deliver(owner, send_request(schedule=self.schedule(None, remind=True)))
                self.assertEqual((reply["code"], reply["exit_code"]), ("SCHEDULED", 0))
                self.assertEqual(reply["result"]["verification"], "api_ack")
                self.assertEqual(reply["result"]["at"], NOW + 181)
                run(owner.close())

    def test_remind_without_scheduled_ids_stays_uncertain(self):
        for ack in ({"message_ids": [501], "scheduled_ids": [], "at": NOW + 181},
                    {"message_ids": [501], "at": NOW + 181}):
            with self.subTest(ack=ack):
                owner = self.owner()
                owner.fake.tg.send_results = [ack]
                reply = self.deliver(owner, send_request(schedule=self.schedule(None, remind=True)))
                self.assertEqual((reply["state"], reply["code"], reply["exit_code"]),
                                 ("unknown", "SEND_UNCERTAIN", 4))
                self.assertIsNone(reply["retain_until"])
                run(owner.close())

    def test_remind_ack_with_different_at_stays_uncertain(self):
        for ack_at in (NOW + 180, NOW + 240):
            with self.subTest(ack_at=ack_at):
                owner = self.owner()
                owner.fake.tg.send_results = [{"scheduled_ids": [902], "at": ack_at}]
                reply = self.deliver(owner, send_request(schedule=self.schedule(None, remind=True)))
                self.assertEqual((reply["code"], reply["exit_code"]), ("SEND_UNCERTAIN", 4))
                run(owner.close())

    def test_remind_outside_ack_window_keeps_queue_verification(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                owner = self.owner(rng=FakeRNG(randint=59))
                # At send time the saved schedule is 240s away after a clock correction.
                owner.fake.tg.hooks["resolve"] = lambda: owner.fake.clock.advance(-1)
                owner.fake.tg.send_results = [{"scheduled_ids": [902], "at": NOW + 239}]
                if verified:
                    owner.fake.tg.scheduled_data = [{"id": 902, "at": NOW + 239, "text": "Проверка"}]
                reply = self.deliver(owner, send_request(schedule=self.schedule(None, remind=True)))
                self.assertEqual((reply["code"], reply["exit_code"]),
                                 ("SCHEDULED", 0) if verified else ("SEND_UNCERTAIN", 4))
                if verified:
                    self.assertEqual(reply["result"]["verification"], "scheduled_verified")
                run(owner.close())

    def test_remind_processing_below_minimum_send_lead_still_rejected(self):
        owner = self.owner()
        owner.fake.tg.hooks["resolve"] = lambda: owner.fake.clock.advance(62)
        owner.fake.tg.send_results = [{"scheduled_ids": [902], "at": NOW + 181}]
        reply = self.deliver(owner, send_request(schedule=self.schedule(None, remind=True)))
        self.assertEqual((reply["code"], reply["exit_code"]), ("INVALID_REQUEST", 2))
        self.assertEqual(owner.fake.tg.sends(), [])

    def test_non_remind_in_remind_window_stays_uncertain_without_queue(self):
        for lead in (181, 210, 239):
            with self.subTest(lead=lead):
                owner = self.owner()
                at = NOW + lead
                owner.fake.tg.send_results = [{"scheduled_ids": [902], "at": at}]
                request = send_request(schedule=self.schedule(at, exact_minute=True))
                request["options"]["no_pace_check"] = True
                reply = self.deliver(owner, request)
                self.assertEqual((reply["state"], reply["code"], reply["exit_code"]),
                                 ("unknown", "SEND_UNCERTAIN", 4))
                run(owner.close())

    def test_source_replaced_after_snapshot_cannot_be_uploaded(self):
        store = FakeStore()
        owner = self.owner(store=store)
        source = write_file(self.base / "source.txt", b"data", mode=0o644)
        original = store.snapshot

        def snapshot(path, dest, *, max_bytes):
            result = original(path, dest, max_bytes=max_bytes)
            replacement = write_file(self.base / "replacement.txt", b"data")
            os.replace(replacement, path)
            return result

        store.snapshot = snapshot
        reply = self.deliver(owner, send_request(files=[self.attachment(source)]))
        self.assertEqual(reply["code"], "ATTACHMENT_DENIED")
        self.assertEqual(owner.fake.tg.uploads(), [])

    def test_changed_envelope_id_cannot_select_status_path(self):
        owner = self.owner()
        request = send_request()
        package = write_package(self.env.root("a"), request)
        changed = dict(request, request_id="../../outside")
        write_file(package / "request.json", json.dumps(changed))
        run(owner.step())
        reply = read_status(self.env.root("a"), request["request_id"])
        self.assertEqual(reply["code"], "INVALID_REQUEST")
        self.assertEqual(owner.fake.tg.sends(), [])

    def test_sweep_is_bounded_and_rotates_without_starving_send(self):
        owner = self.owner()
        stale = []
        for index in range(40):
            request = send_request()
            stale.append(request["request_id"])
            write_package(self.env.root("a"), request)
            owner.fake.store.atomic_write(self.env.root("a") / "status" / f"{request['request_id']}.json",
                json.dumps({"request_id": request["request_id"], "state": "sent", "retain_until": NOW}).encode())
        reply = self.deliver(owner, send_request(peer=42))
        self.assertEqual(reply["code"], "SENT")
        self.assertTrue(any((self.env.root("a") / "outbox" / rid).exists() for rid in stale))
        for _ in range(4):
            owner.fake.clock.advance(30)  # ротация на очередном проходе уборки
            run(owner.step())
        self.assertTrue(all(not (self.env.root("a") / "outbox" / rid).exists() for rid in stale))

    def test_real_store_client_package_owner_send_and_terminal_status(self):
        store = import_owner().Store()
        owner = self.owner(store=store)
        source = write_file(self.base / "document.txt", b"document", mode=0o644)
        request = send_request(files=[self.attachment(source)])
        write_heartbeat(self.env.root("a"))
        client = import_client()
        pending = client.submit(self.env.root("a"), request, clock=owner.fake.clock,
                                timeout=0, store=store)
        self.assertEqual(pending["code"], "WAIT_TIMEOUT")
        run(owner.step())
        reply = client.status(self.env.root("a"), request["request_id"], clock=owner.fake.clock)
        self.assertEqual(reply["code"], "SENT")
        self.assertEqual(reply["request_hash"], req_hash(request))
        copy = owner.fake.tg.uploads()[0][1]
        self.assertEqual(copy.read_bytes(), b"document")
        self.assertNotEqual(copy, source)

    def test_successful_rpc_ids_survive_failed_final_status(self):
        store = FakeStore()
        owner = self.owner(store=store)

        def fail_final_status():
            store.fail_on[("atomic_write", "status/")] = ("before", OSError("disk"))

        owner.fake.tg.hooks["send"] = fail_final_status
        request = send_request()
        reply = self.deliver(owner, request)
        self.assertEqual(reply["state"], "unknown")
        self.assertEqual(reply["result"]["message_ids"], [501])
        run(owner.step())
        self.assertEqual(len(owner.fake.tg.sends()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
