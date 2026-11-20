"""Additional offline checks for slice 5; independent suites stay unchanged."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from telegram_session_owner import FloodWait, SendUncertain, Store
import telegram_owner_client as client
from telegram_owner_telethon import Telegram
import test_telegram_session_owner_cli as fixture


class ProtocolOwner(threading.Thread):
    """A spool peer that binds every reply to the actual request hash."""
    def __init__(self, root):
        super().__init__(daemon=True)
        self.root, self.seen, self.halt = root, [], threading.Event()

    def run(self):
        done = set()
        while not self.halt.is_set():
            for path in self.root.glob("outbox/*/request.json"):
                request = json.loads(path.read_bytes())
                rid = request["request_id"]
                if rid in done:
                    continue
                done.add(rid)
                self.seen.append(request)
                refresh = request["operation"] == "refresh"
                reply = client._reply(request, "REFRESHED" if refresh else "SENT", 0,
                                      state="refreshed" if refresh else "sent")
                reply["request_hash"] = hashlib.sha256(client._json(request)).hexdigest()
                Store().atomic_write(self.root / "status" / f"{rid}.json", client._json(reply))
            self.halt.wait(0.01)

    def stop(self):
        self.halt.set()
        self.join(2)


class CliLocal(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = fixture.Ctx(Path(tmp.name))
        self.owners = {}

    def owner(self, account="default"):
        root = self.ctx.roots[account]
        fixture.heartbeat_now(root)
        owner = ProtocolOwner(root)
        owner.start()
        self.addCleanup(owner.stop)
        self.owners[account] = owner
        return owner

    def test_send_one_pull_snapshot_use_a_hash_bound_reply(self):
        owner = self.owner()
        self.ctx.project(owner_root=str(owner.root))
        for name in fixture.SPECS:
            with self.subTest(name=name):
                code, out, err, reached = fixture.run_cli(name, self.ctx, ["--json"])
                self.assertEqual(code, 0, (out, err))
                self.assertFalse(reached)
                result = json.loads(out)
                reply = result["results"][0]["reply"] if name == "snapshot" else result
                request = owner.seen[-1]
                self.assertEqual(reply["request_hash"], hashlib.sha256(client._json(request)).hexdigest())
                self.assertIn(request["request_id"], err)

    def test_snapshot_partial_report_keeps_success_and_total_7(self):
        owner = self.owner("b")
        self.ctx.project(owner_config=str(self.ctx.owner_config()), chats={
            "one": {"id": 1001, "account": "a", "dest": str(self.ctx.tmp / "a")},
            "two": {"id": 2002, "account": "b", "dest": str(self.ctx.tmp / "b")}})
        code, out, _, reached = fixture.run_cli("snapshot", self.ctx, ["--json"])
        self.assertEqual(code, 7)
        self.assertFalse(reached)
        rows = json.loads(out)["results"]
        self.assertEqual([r["reply"]["code"] for r in rows], ["OWNER_UNAVAILABLE", "REFRESHED"])
        self.assertEqual(owner.seen[0]["selector"]["peer"], 2002)

    def test_repeat_id_reuses_envelope_and_rejects_changed_text(self):
        owner = self.owner()
        self.ctx.project(owner_root=str(owner.root))
        rid = str(uuid.uuid4())
        for _ in range(2):
            code, out, err, _ = fixture.run_cli("send-one", self.ctx, ["--request-id", rid, "--json"])
            self.assertEqual(code, 0, (out, err))
        self.assertEqual(len(owner.seen), 1)
        code, out, _, _ = fixture.run_cli("send-one", self.ctx, ["--request-id", rid, "--json"],
                                        argv=["1001", "--text", "changed", "--send"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["code"], "REQUEST_ID_CONFLICT")

    def test_5d_fallback_is_readonly_and_explicit(self):
        owner = self.owner()
        self.ctx.project(owner_root=str(owner.root))
        before = fixture.tree(owner.root)
        code, out, _, _ = fixture.run_cli("send-one", self.ctx, ["--json"], argv=["1001", "--text", "hi"])
        self.assertEqual(code, 0)
        self.assertIn("denylist соседних экземпляров не проверен", json.loads(out)["result"]["message"])
        self.assertEqual(fixture.tree(owner.root), before)
        self.assertFalse(owner.seen)

    def test_5d_copied_registry_denies_other_account_credentials(self):
        owner = self.owner()
        accounts = {}
        for account in ("default", "a"):
            private = self.ctx.tmp / "private" / account
            private.mkdir(parents=True, mode=0o700)
            auth = private / "auth.json"
            auth.write_text('{}')
            auth.chmod(0o600)
            accounts[account] = {"auth_path": str(auth), "session_path": str(private / "session"),
                                 "owner_root": str(self.ctx.roots[account])}
        Store().atomic_write(owner.root / "installation.json", client._json({"accounts": accounts}))
        self.ctx.project(owner_root=str(owner.root))
        code, out, _, _ = fixture.run_cli("send-one", self.ctx, ["--json"], argv=[
            "1001", "--text", "hi", "--file", accounts["a"]["auth_path"]])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["code"], "ATTACHMENT_DENIED")

    def test_remind_keeps_null_at_until_owner_chooses_it(self):
        owner = self.owner()
        self.ctx.project(owner_root=str(owner.root))
        code, _, _, _ = fixture.run_cli("send-one", self.ctx, argv=["me", "--text", "hi", "--remind", "--send"])
        self.assertEqual(code, 0)
        self.assertTrue(owner.seen[0]["schedule"]["remind"])
        self.assertIsNone(owner.seen[0]["schedule"]["at"])

    def test_snapshot_cli_overrides_conflicting_project_fields(self):
        owner = self.owner()
        self.ctx.project(owner_root=str(owner.root), owner_config=str(self.ctx.owner_config()))
        code, out, err, _ = fixture.run_cli("snapshot", self.ctx, ["--owner-root", str(owner.root), "--json"])
        self.assertEqual(code, 0, (out, err))

    def test_bad_snapshot_entry_does_not_hide_later_success(self):
        owner = self.owner()
        self.ctx.project(owner_root=str(owner.root), chats={
            "bad": {"account": "default"}, "good": {"id": 1001, "dest": str(self.ctx.dest)}})
        code, out, _, _ = fixture.run_cli("snapshot", self.ctx, ["--json"])
        self.assertEqual(code, 2)
        self.assertEqual([r["reply"]["code"] for r in json.loads(out)["results"]],
                         ["INVALID_CONFIG", "REFRESHED"])

    def test_instance_mismatch_fails_before_importing_telethon(self):
        config = self.ctx.tmp / "instance.json"
        Store().atomic_write(config, client._json({"account": "a"}))
        command = [sys.executable, str(fixture.SCRIPTS / "telegram-session-owner.py"),
                   "--config", str(config), "--instance", "b"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("INVALID_CONFIG", result.stderr)
        self.assertNotIn("telethon", result.stderr.lower())

    def test_owner_route_does_not_import_telethon_in_a_fresh_process(self):
        self.ctx.project(owner_root=str(self.ctx.roots["default"]))
        for name, (filename, build) in fixture.SPECS.items():
            script = f'''
import importlib.util, sys
from pathlib import Path
class Deny:
    def find_spec(self, fullname, *args):
        if fullname.startswith("telethon"):
            raise AssertionError("Telethon import in owner client")
sys.meta_path.insert(0, Deny())
spec=importlib.util.spec_from_file_location("cli", {str(fixture.SCRIPTS / filename)!r})
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
target=getattr(mod,"tgs",mod)
target.PROJECT_CONFIG_PATH=Path({str(self.ctx.cfg_path)!r})
sys.argv=[{filename!r}]+{build({"dest": self.ctx.dest})!r}
raise SystemExit(mod.main())
'''
            with self.subTest(name=name):
                # Path in argv is always converted to a plain string by build.
                result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
                self.assertEqual(result.returncode, 7, result.stderr)


class AdapterLocal(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = Telegram.__new__(Telegram)
        self.adapter.account = "a"
        self.adapter.client = types.SimpleNamespace(send_message=AsyncMock(), send_file=AsyncMock(),
                                                    upload_file=AsyncMock(return_value="handle"),
                                                    is_connected=lambda: True)
        self.adapter._uploads = {}
        self.adapter._entities = {("User", 1): "entity"}
        self.peer = {"account": "a", "kind": "User", "id": 1}
        class RpcFlood(Exception):
            seconds = 19
        self.RpcFlood = RpcFlood
        self.adapter.errors = types.SimpleNamespace(FloodWaitError=RpcFlood)
        self.adapter.utils = types.SimpleNamespace(get_attributes=Mock(return_value=(["attrs"], "audio/ogg")))
        self.request = {"text": "hi", "options": {"silent": True, "html": False, "voice": False},
                        "target": {"topic_id": 10, "reply_to": 11}, "schedule": {"at": None}}

    async def test_no_rpc_retry_on_uncertain_send(self):
        self.adapter.client.send_message.side_effect = OSError("opaque")
        with self.assertRaises(SendUncertain):
            await self.adapter.send(self.request, self.peer, [])
        self.adapter.client.send_message.assert_awaited_once()

    async def test_floodwait_is_returned_to_owner_without_sleep(self):
        self.adapter.client.send_message.side_effect = self.RpcFlood()
        with self.assertRaises(FloodWait) as error:
            await self.adapter.send(self.request, self.peer, [])
        self.assertEqual(error.exception.seconds, 19)
        self.adapter.client.send_message.assert_awaited_once()

    async def test_scheduled_html_ack_preserves_server_text_and_time(self):
        from datetime import datetime, timezone
        at = int(time.time()) + 300
        self.request["schedule"]["at"] = at
        self.request["options"]["html"] = True
        self.request["text"] = "<b>hi</b>"
        self.adapter.client.send_message.return_value = types.SimpleNamespace(id=33,
                                      date=datetime.fromtimestamp(at, timezone.utc), message="hi")
        ack = await self.adapter.send(self.request, self.peer, [])
        self.assertEqual(ack["scheduled_ids"], [33])
        self.assertEqual(ack["message_ids"], [])
        self.assertEqual(ack["text"], "hi")
        self.assertEqual(ack["at"], at)
        kwargs = self.adapter.client.send_message.call_args.kwargs
        self.assertEqual((kwargs["reply_to"], kwargs["silent"], kwargs["parse_mode"]), (11, True, "html"))

    async def test_voice_metadata_is_computed_from_durable_copy(self):
        self.request["options"]["voice"] = True
        self.adapter._uploads["token"] = ("handle", Path("/durable/file.ogg"))
        self.adapter.client.send_file.return_value = types.SimpleNamespace(id=5, message="hi")
        await self.adapter.send(self.request, self.peer, ["token"])
        kwargs = self.adapter.client.send_file.call_args.kwargs
        self.assertTrue(kwargs["voice_note"])
        self.assertFalse(kwargs["force_document"])
        self.assertEqual(kwargs["attributes"], ["attrs"])
        self.assertEqual(self.adapter.utils.get_attributes.call_args.args, ("/durable/file.ogg",))
        self.assertFalse(self.adapter._uploads)

    async def test_partial_album_keeps_known_ids_and_discards_handles(self):
        self.adapter._uploads = {"a": ("h1", Path("/copy/a")), "b": ("h2", Path("/copy/b"))}
        self.adapter.client.send_file.return_value = [types.SimpleNamespace(id=7)]
        with self.assertRaises(SendUncertain) as error:
            await self.adapter.send(self.request, self.peer, ["a", "b"])
        self.assertEqual(error.exception.message_ids, [7])
        self.assertFalse(self.adapter._uploads)
        self.assertTrue(self.adapter.client.send_file.call_args.kwargs["force_document"])
        self.assertEqual(self.adapter.client.send_file.call_args.kwargs["caption"], ["hi", ""])

    async def test_queue_overflow_is_bounded_and_observable(self):
        from collections import deque
        self.adapter._updates = deque(range(1000))
        self.adapter._overflow = False
        await self.adapter._enqueue("new")
        self.assertTrue(self.adapter._overflow)
        self.assertEqual(list(self.adapter._updates), ["new"])

    async def test_recent_does_not_download_media(self):
        self.adapter._read = AsyncMock(return_value=[])
        await self.adapter.recent(self.peer, limit=50)
        self.assertFalse(self.adapter._read.call_args.kwargs["download_media"])

    async def test_factory_disables_hidden_rpc_retries_and_interactive_auth(self):
        snapshot = types.SimpleNamespace(load_auth=Mock(return_value={"api_id": 1, "api_hash": "fake"}),
                                         client_kwargs=Mock(return_value={}))
        telethon = types.ModuleType("telethon")
        telethon.TelegramClient = Mock()
        telethon.events = types.SimpleNamespace(Raw=lambda: "raw")
        telethon.errors = self.adapter.errors
        telethon.utils = self.adapter.utils
        tl = types.ModuleType("telethon.tl")
        tl.types = types.SimpleNamespace()
        tl.functions = types.SimpleNamespace()
        cfg = {"account": "a", "auth_path": "/private/auth.json", "session_path": "/private/a"}
        with patch.dict(sys.modules, {"telethon": telethon, "telethon.tl": tl}), \
             patch("telegram_owner_telethon._snapshot", return_value=snapshot):
            adapter = Telegram(cfg)
        kwargs = telethon.TelegramClient.call_args.kwargs
        self.assertEqual(kwargs["request_retries"], 0)
        self.assertEqual(kwargs["flood_sleep_threshold"], 0)
        self.assertFalse(kwargs["auto_reconnect"])
        self.assertEqual(telethon.TelegramClient.call_args.args[0], "/private/a")
        adapter.client.start.assert_not_called()
        adapter.client.connect.assert_not_called()

    async def test_unqualified_delete_never_guesses_a_peer(self):
        from collections import deque
        names = ("UpdateDeleteChannelMessages", "UpdateDeleteMessages", "UpdateNewMessage",
                 "UpdateNewChannelMessage", "UpdateEditMessage", "UpdateEditChannelMessage",
                 "UpdatesTooLong", "UpdateChannelTooLong", "UpdateShortMessage", "UpdateShortChatMessage")
        self.adapter.types = types.SimpleNamespace(**{n: type(n, (), {}) for n in names})
        update = self.adapter.types.UpdateDeleteMessages()
        update.messages = [5]
        self.adapter._updates = deque([update])
        self.adapter._overflow, self.adapter._seq = False, 0
        self.assertEqual(await self.adapter.events(), [])

    async def test_forum_create_is_a_topic_record_with_revision(self):
        from datetime import datetime, timezone
        record = {"id": 7, "title": "Topic", "date": "2026-01-01T00:00:00", "date_unixtime": "1", "icon_color": None}
        self.adapter.snapshot = types.SimpleNamespace(extract_topic_meta=lambda m: record)
        msg = types.SimpleNamespace(edit_date=None, date=datetime(2026, 1, 1, tzinfo=timezone.utc))
        result = await self.adapter._record(msg, sequence=10)
        self.assertEqual(result["kind"], "topic")
        self.assertEqual(result["record"], record)
        self.assertEqual(result["revision"], [msg.date.timestamp(), 10])

    async def test_resolve_refreshes_username_instead_of_trusting_cached_alias(self):
        self.adapter._aliases = {1: {"username": "old"}}
        self.adapter._dialogs = {1: "cached-user"}
        self.adapter.client.get_entity = AsyncMock(return_value=types.SimpleNamespace(id=1, username="new"))
        self.adapter._peer = lambda entity: {"account": "a", "kind": "User", "id": entity.id,
                                             "username": entity.username}
        peer = await self.adapter.resolve("a", 1)
        self.assertEqual(peer["username"], "new")
        self.adapter.client.get_entity.assert_awaited_once_with("cached-user")


if __name__ == "__main__":
    unittest.main(verbosity=2)
