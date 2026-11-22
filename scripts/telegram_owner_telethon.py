"""Telethon implementation of the owner's Telegram port.

Construction happens inside the locked factory. No interactive authorization
or hidden RPC retries. Only dialog loading permits bounded FloodWait sleeps;
the durable owner controls backoff for other operations.
"""
from __future__ import annotations

from collections import deque
from contextlib import redirect_stderr
from datetime import datetime, timezone
import asyncio
import importlib.util
import io
import logging
from pathlib import Path
import time
import uuid

from telegram_session_owner import Disconnected, FloodWait, HistoryPage, SendUncertain


DIALOG_FLOOD_SLEEP_THRESHOLD = 120
DIALOG_REQUEST_RETRIES = 3
CONNECT_TIMEOUT = 30


def _snapshot():
    spec = importlib.util.spec_from_file_location("owner_snapshot", Path(__file__).with_name("telegram-snapshot.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_telethon()
    return module


class Telegram:
    def __init__(self, config):
        from telethon import TelegramClient, events, errors, utils
        from telethon.tl import types, functions

        self.config, self.account = config, config["account"]
        self.types, self.functions = types, functions
        self.errors, self.utils = errors, utils
        # Legacy auth validation may include a malformed proxy in its error.
        # Discard that diagnostic; the owner reports only INVALID_CONFIG.
        try:
            with redirect_stderr(io.StringIO()):
                self.snapshot = _snapshot()
                self.snapshot.AUTH_PATH = Path(config["auth_path"])
                self.snapshot.AUTH_DIR = Path(config["auth_path"]).parent
                auth = self.snapshot.load_auth(self.account)
                kwargs = self.snapshot.client_kwargs(auth)
        except SystemExit:
            raise ValueError("INVALID_CONFIG") from None
        self.client = TelegramClient(str(config["session_path"]), auth["api_id"], auth["api_hash"],
                                     **kwargs,
                                     request_retries=0, connection_retries=0,
                                     auto_reconnect=False, flood_sleep_threshold=0,
                                     raise_last_call_error=True)
        self._entities, self._dialogs, self._aliases = {}, {}, {}
        self._updates, self._uploads = deque(), {}
        self._overflow = False
        self._seq = 0
        self._media_cleanup = {}
        self._media_errors = {}
        self.client.add_event_handler(self._enqueue, events.Raw())

    async def _call(self, awaitable, *, sending=False, timeout=None, operation=None):
        try:
            if timeout is not None:
                return await asyncio.wait_for(awaitable, timeout=timeout)
            return await awaitable
        except asyncio.TimeoutError:
            logging.getLogger(__name__).warning(
                "OWNER_RPC_TIMEOUT operation=%s timeout=%s", operation, timeout)
            if sending:
                raise SendUncertain() from None
            raise Disconnected() from None
        except self.errors.FloodWaitError as exc:
            raise FloodWait(exc.seconds) from None
        except Exception:
            if sending:
                raise SendUncertain() from None
            raise Disconnected() from None

    async def _enqueue(self, update):
        # Raw includes forum service messages, unlike NewMessage's filter.
        if len(self._updates) >= 1000:
            self._updates.clear()
            self._overflow = True
        self._updates.append(update)

    async def connect(self):
        await self._call(self.client.connect(), timeout=CONNECT_TIMEOUT, operation="transport_connect")
        if not await self._call(self.client.is_user_authorized(), timeout=CONNECT_TIMEOUT,
                                operation="is_user_authorized"):
            raise Disconnected()
        # Dialog loading is the only place that may sleep on FloodWait: a large
        # account always hits it on GetDialogs. Telethon sleeps only when a retry
        # is allowed, so both the threshold and the retries are raised here and
        # restored afterwards. A partial dialog list is never accepted.
        retries = self.client._request_retries
        self.client.flood_sleep_threshold = DIALOG_FLOOD_SLEEP_THRESHOLD
        self.client._request_retries = DIALOG_REQUEST_RETRIES
        try:
            async for dialog in self.client.iter_dialogs():
                self._dialogs[dialog.entity.id] = dialog.entity
        except self.errors.FloodWaitError as exc:
            raise FloodWait(exc.seconds) from None
        except Exception:
            raise Disconnected() from None
        finally:
            self.client.flood_sleep_threshold = 0
            self.client._request_retries = retries

    async def disconnect(self):
        try:
            await self.client.disconnect()
        finally:
            self._uploads.clear()

    def _peer(self, entity):
        kind = ("Channel" if isinstance(entity, self.types.Channel) else
                "Chat" if isinstance(entity, self.types.Chat) else "User")
        peer = {"account": self.account, "kind": kind, "id": entity.id,
                "username": getattr(entity, "username", None)}
        self._entities[(kind, entity.id)] = entity
        return peer

    async def resolve(self, account, peer):
        if account != self.account:
            raise ValueError("ACCOUNT_NOT_MANAGED")
        # Tokens from an abandoned upload batch are not reused across requests.
        self._uploads.clear()
        if peer == "me":
            entity = await self._call(self.client.get_me())
        elif peer in self._dialogs:
            # Preserve the dialog's peer kind, but refresh mutable username.
            entity = await self._call(self.client.get_entity(self._dialogs[peer]))
        else:
            entity = await self._call(self.snapshot.resolve_entity(self.client, peer, self._dialogs))
        self._aliases[peer] = self._peer(entity)
        return dict(self._aliases[peer])

    def _entity(self, peer):
        return self._entities[(peer["kind"], peer["id"])]

    async def _record(self, msg, sequence=0):
        t = msg.edit_date or msg.date
        revision = [t.timestamp(), sequence]
        topic = self.snapshot.extract_topic_meta(msg)
        if topic is None and isinstance(msg.action, self.types.MessageActionTopicEdit):
            root = self.snapshot.topic_id_from_reply(msg.reply_to)
            if root and msg.action.title is not None:
                topic = {"id": root, "title": msg.action.title}
        if topic is not None:
            return {"kind": "topic", "record": topic, "revision": revision}
        record = await self.snapshot.message_to_record(self.client, msg)
        return record

    def _media_configs(self, peer):
        # One history read feeds all destinations of this canonical peer.
        for dest in self.config["destinations"]:
            resolved = self._aliases.get(dest["peer"])
            if resolved and all(resolved[k] == peer[k] for k in ("kind", "id")):
                yield dest

    async def _media(self, peer, msg):
        done = set()
        for dest in self._media_configs(peer):
            destination_id = dest["destination_id"]
            if destination_id in self._media_errors:
                continue
            cfg = dest["project_config"]
            if cfg.get("media", True) is False:
                continue
            reason = "MEDIA_SETTINGS_INVALID"
            try:
                # Legacy helpers can exit with config values in their diagnostics.
                # Refuse this destination's media without stopping text catch-up.
                with redirect_stderr(io.StringIO()):
                    self.snapshot.PROJECT_ROOT = Path(dest["project_root"])
                    self.snapshot.apply_media_settings(cfg)
                    # Account is part of the namespace: equal ids in other sessions
                    # cannot collide. The helper preserves message filenames.
                    cache = self.snapshot.MEDIA_CACHE / self.account / f"{peer['kind']}-{peer['id']}"
                    if cache in done:
                        continue
                    last = self._media_cleanup.get(cache)
                    if last is None or time.monotonic() - last >= 3600:
                        reason = "MEDIA_CLEANUP_REFUSED"
                        old_cache = self.snapshot.MEDIA_CACHE
                        try:
                            self.snapshot.MEDIA_CACHE = cache
                            self.snapshot.cleanup_old_media()
                            self._media_cleanup[cache] = time.monotonic()
                        finally:
                            self.snapshot.MEDIA_CACHE = old_cache
            except SystemExit:
                self._media_errors[destination_id] = reason
                index = self.config["destinations"].index(dest) + 1
                logging.getLogger(__name__).warning("%s destination_index=%d", reason, index)
                continue
            done.add(cache)
            await self.snapshot.download_message_media(self.client, msg, cache, safe_errors=True)

    async def _read(self, peer, *, download_media=True, history_page=False, **kwargs):
        records = []
        scanned, next_id = 0, kwargs.get("min_id", 0)
        try:
            async for msg in self.client.iter_messages(self._entity(peer), **kwargs):
                if history_page:
                    scanned += 1
                    next_id = max(next_id, msg.id)
                record = await self._record(msg)
                if record is not None:
                    records.append(record)
                if download_media and msg.action is None:
                    await self._media(peer, msg)
            if history_page:
                return HistoryPage(records, next_id=next_id, complete=scanned < kwargs["limit"])
            return records
        except self.errors.FloodWaitError as exc:
            raise FloodWait(exc.seconds) from None
        except Exception:
            raise Disconnected() from None

    async def history(self, peer, *, after_id, limit):
        # Export filtering and topic-root ids cannot determine raw page progress.
        return await self._read(peer, min_id=after_id, reverse=True, limit=limit,
                                history_page=limit is not None, wait_time=0)

    async def recent(self, peer, *, limit):
        return await self._read(peer, limit=limit, wait_time=0, download_media=False)

    def _update_peer(self, value):
        if value is None:
            return None
        for name, kind in (("channel_id", "Channel"), ("chat_id", "Chat"), ("user_id", "User")):
            ident = getattr(value, name, None)
            if ident is not None:
                return {"account": self.account, "kind": kind, "id": ident, "username": None}
        return None

    async def events(self):
        if not self.client.is_connected():
            raise Disconnected()
        result = []
        if self._overflow:
            result.append({"kind": "overflow"})
            self._overflow = False
        batch = [self._updates.popleft() for _ in range(min(100, len(self._updates)))]
        for update in batch:
            self._seq += 1
            sequence = getattr(update, "pts", None) or self._seq
            if isinstance(update, self.types.UpdateDeleteChannelMessages):
                result.append({"kind": "delete", "peer": {"account": self.account, "kind": "Channel",
                               "id": update.channel_id, "username": None}, "ids": update.messages,
                               "revision": [time.time(), sequence]})
            elif isinstance(update, self.types.UpdateDeleteMessages):
                # Telegram gives no peer: never guess one from a message id.
                continue
            elif isinstance(update, (self.types.UpdateNewMessage, self.types.UpdateNewChannelMessage,
                                     self.types.UpdateEditMessage, self.types.UpdateEditChannelMessage)):
                msg = update.message
                # Raw messages need the normal Telethon client/entity context.
                if hasattr(msg, "_finish_init"):
                    msg._finish_init(self.client, {}, None)
                peer = self._update_peer(msg.peer_id)
                if peer is None or (peer["kind"], peer["id"]) not in self._entities:
                    continue
                try:
                    record = await self._record(msg, sequence)
                    if record is None:
                        continue
                    if record.get("kind") == "topic":
                        result.append({**record, "peer": peer})
                    else:
                        edited = isinstance(update, (self.types.UpdateEditMessage, self.types.UpdateEditChannelMessage))
                        result.append({"kind": "edit" if edited else "new", "peer": peer, "record": record,
                                       "revision": [(msg.edit_date or msg.date).timestamp(), sequence]})
                    await self._media(peer, msg)
                except Exception:
                    # A conversion failure loses no durable cursor: force catch-up.
                    result.append({"kind": "overflow"})
            elif isinstance(update, (self.types.UpdatesTooLong, self.types.UpdateChannelTooLong)):
                result.append({"kind": "overflow"})
            elif isinstance(update, (self.types.UpdateShortMessage, self.types.UpdateShortChatMessage)):
                # Short updates lack the full message context. Catch up only
                # mirrored destinations, using their resolved kind/id (including
                # basic Chat groups configured with a positive numeric peer).
                peer = self._update_peer(update)
                if peer is not None and any(self._media_configs(peer)):
                    result.append({"kind": "overflow"})
        return result

    async def upload(self, path):
        handle = await self._call(self.client.upload_file(str(path)))
        token = str(uuid.uuid4())
        self._uploads[token] = (handle, Path(path))
        return token

    async def send(self, request, peer, uploads):
        at = request["schedule"]["at"]
        schedule = datetime.fromtimestamp(at, timezone.utc) if at is not None else None
        opts, target = request["options"], request["target"]
        kwargs = {"reply_to": target["reply_to"] or target["topic_id"],
                  "silent": opts["silent"], "parse_mode": "html" if opts["html"] else None,
                  "schedule": schedule}
        try:
            if uploads:
                handles = [self._uploads[token][0] for token in uploads]
                if len(handles) > 1:
                    rpc = self.client.send_file(self._entity(peer), handles,
                                                caption=[request["text"]] + [""] * (len(handles) - 1),
                                                force_document=True, **kwargs)
                else:
                    attrs, mime = self.utils.get_attributes(str(self._uploads[uploads[0]][1]),
                                                           force_document=not opts["voice"], voice_note=opts["voice"])
                    rpc = self.client.send_file(self._entity(peer), handles[0], caption=request["text"],
                                                attributes=attrs, mime_type=mime,
                                                force_document=not opts["voice"], voice_note=opts["voice"], **kwargs)
            else:
                rpc = self.client.send_message(self._entity(peer), request["text"], **kwargs)
            sent = await self._call(rpc, sending=True)
            messages = sent if isinstance(sent, list) else [sent]
            ids = [msg.id for msg in messages if msg is not None]
            if len(ids) != max(1, len(uploads)):
                raise SendUncertain(ids)
            return {"message_ids": ids if at is None else [], "scheduled_ids": ids if at is not None else [],
                    "at": messages[0].date.timestamp() if at is not None else None,
                    "text": messages[0].message}
        finally:
            for token in uploads:
                self._uploads.pop(token, None)

    async def scheduled(self, peer):
        response = await self._call(self.client(self.functions.messages.GetScheduledHistoryRequest(
            peer=self._entity(peer), hash=0)))
        return [{"id": msg.id, "at": msg.date.timestamp(), "text": msg.message}
                for msg in response.messages]
