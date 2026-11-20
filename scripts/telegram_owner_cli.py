"""Local CLI routing. Importing this module never imports Telethon or auth."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

import telegram_owner_client as client
from telegram_session_owner import Store


class Clock:
    utc = staticmethod(time.time)
    monotonic = staticmethod(time.monotonic)
    sleep_sync = staticmethod(time.sleep)

    async def sleep(self, seconds):
        import asyncio
        await asyncio.sleep(seconds)


def arguments(parser, *, request_id=True):
    parser.add_argument("--owner-config", default=None, help="карта account -> owner_root")
    parser.add_argument("--owner-root", default=None, help="спул одного account")
    parser.add_argument("--json", action="store_true", help="машинный ответ owner")
    if request_id:
        parser.add_argument("--request-id", help="UUIDv4 заявки; повтор требует того же конверта")
    parser.add_argument("--timeout", type=float, default=30, help="срок ожидания ответа owner")


def project(path):
    """Read routing hints; direct commands retain their own config validation."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        return {}


class Route:
    def __init__(self, args, cfg, base):
        self.base, self.store = Path(base), Store()
        cli = {k: getattr(args, k, None) for k in ("owner_config", "owner_root")}
        source = cli if any(v is not None for v in cli.values()) else cfg
        selected = [k for k in cli if k in source and source[k] is not None]
        # A null project field is configured too, and invalid, never direct.
        if source is cfg:
            selected = [k for k in cli if k in source]
        self.enabled = bool(selected)
        self.config_path = self.registry = self.root = self.managed = None
        if not selected:
            return
        if len(selected) != 1:
            raise client._Error("INVALID_CONFIG")
        key = selected[0]
        value = source[key]
        if not isinstance(value, str) or not value.strip():
            raise client._Error("INVALID_CONFIG")
        path = Path(value).expanduser()
        path = path if path.is_absolute() else self.base / path
        if key == "owner_root":
            self.root = path
            return
        self.config_path = path
        try:
            config = client._read_json(self.store, path)
            self.managed = config["managed_accounts"]
            if not isinstance(self.managed, dict) or not self.managed:
                raise ValueError
            paths, inodes = set(), set()
            for account, root in self.managed.items():
                if not client._account(account):
                    raise ValueError
                root = client._path(root)
                real = client._realpath(self.store, root)
                info = client._inspect(self.store, root)
                inode = (info["dev"], info["ino"]) if info else None
                if real in paths or inode is not None and inode in inodes:
                    raise ValueError
                paths.add(real)
                if inode is not None:
                    inodes.add(inode)
            if "installation_config" in config:
                self.registry = client.load_installation_config(path, store=self.store)["path"]
        except (OSError, ValueError, KeyError, TypeError):
            raise client._Error("INVALID_CONFIG") from None

    def account_root(self, account):
        if not client._account(account):
            raise client._Error("INVALID_CONFIG")
        if self.managed is not None:
            if account not in self.managed:
                raise client._Error("ACCOUNT_NOT_MANAGED")
            return Path(self.managed[account])
        return self.root

    def installation(self, root):
        return self.registry if self.registry is not None else root / "installation.json"


def emit(reply, args):
    if getattr(args, "json", False):
        print(json.dumps(reply, ensure_ascii=False))
    else:
        print(f"{reply.get('request_id') or '-'} {reply['code']}")
        result = reply.get("result", {})
        if result.get("message"):
            print(result["message"])
        if result.get("updated_at") is not None:
            print(f"account={result.get('account')} status={result.get('request_id')} "
                  f"updated_at={result['updated_at']} age_seconds={result.get('age_seconds')}")
        if reply.get("next_step"):
            print(reply["next_step"])
    return reply["exit_code"]


def error(args, code="INVALID_CONFIG", request=None, exit_code=2):
    reply = client._reply(request or {}, code, exit_code)
    return emit(reply, args)


def envelope(args, operation, account):
    now = time.time()
    rid = getattr(args, "request_id", None) or str(uuid.uuid4())
    if not client._uuid(rid):
        raise client._Error("INVALID_REQUEST")
    return {"schema_version": 1, "request_id": rid, "created_at": now,
            "expires_at": now + 7 * 86400, "operation": operation, "account": account}


def _repeat(root, request, store):
    """Reuse timestamps of an explicitly repeated id, while checking all inputs."""
    path = root / "outbox" / request["request_id"] / "request.json"
    previous = client._read_json(store, path)
    for key in ("created_at", "expires_at"):
        request[key] = previous[key]
    return request


def execute(route, request, args, *, dry=False):
    root = route.account_root(request["account"])
    if getattr(args, "request_id", None):
        path = root / "outbox" / request["request_id"] / "request.json"
        if client._inspect(route.store, path) is not None:
            _repeat(root, request, route.store)
    # stdout stays a single JSON object; the id is available before any wait.
    print(request["request_id"], file=sys.stderr, flush=True)
    if dry:
        registry = route.installation(root)
        if route.registry is not None or client._inspect(route.store, registry) is not None:
            return client.dry_run(root, request, installation_config=registry,
                                  clock=Clock(), store=route.store)
        return client.dry_run(root, request, installation_config=None,
                              clock=Clock(), store=route.store)
    return client.submit(root, request, clock=Clock(), timeout=args.timeout, store=route.store)


def send(route, args, *, account, peer, topic=None, username=None):
    request = envelope(args, "send", account)
    try:
        text = args.text
        if text is None:
            text = "" if sys.stdin.isatty() else sys.stdin.read().rstrip("\n")
        if not text.strip():
            text = ""
        values = args.file or []
        if any(not isinstance(v, str) or not v.strip() or ".." in Path(v).parts for v in values):
            raise client._Error("INVALID_REQUEST")
        if (len(values) > 10 or len(text.encode("utf-16-le")) // 2 > (1024 if values else 4096)):
            raise client._Error("LIMIT_EXCEEDED")
        files = []
        for value in values:
            # Keep symlinks visible to validation, never resolve them.
            path = Path(os.path.abspath(value))
            info = route.store.inspect(path)
            files.append({"source": str(path), "name": path.name, "size": info["size"], "sha256": "0" * 64})
        request.update(target={"peer": peer, "expected_username": username,
                               "topic_id": topic, "reply_to": args.reply_to}, text=text, files=files,
                       options={k: bool(getattr(args, k, False)) for k in
                                ("silent", "html", "voice", "no_pace_check")},
                       schedule={"at": args.schedule, "timezone": args.schedule_tz,
                                 "exact_minute": args.exact_minute, "remind": bool(getattr(args, "remind", False))})
        if not text and not files or username is not None and not username.lstrip("@").strip():
            raise client._Error("INVALID_REQUEST")
        client._validate(request, Clock())
        # Denylist and sizes precede reading bytes. Owner checks again at accept.
        root = route.account_root(account)
        registry_path = route.installation(root)
        if route.registry is not None or client._inspect(route.store, registry_path) is not None:
            client._local_deny(request, client._registry(route.store, registry_path), route.store)
        else:
            client._local_deny(request, client._own_registry(root, account, route.store), route.store)
        for spec in files:
            path = Path(spec["source"])
            before = route.store.inspect(path)
            data = client._read_bytes(route.store, path, client.FILE_LIMIT)
            after = route.store.inspect(path)
            if len(data) > client.FILE_LIMIT:
                raise client._Error("LIMIT_EXCEEDED")
            if ((before["dev"], before["ino"], before["size"]) !=
                    (after["dev"], after["ino"], after["size"]) or len(data) != spec["size"]):
                raise client._Error("ATTACHMENT_DENIED")
            spec["sha256"] = hashlib.sha256(data).hexdigest()
        reply = execute(route, request, args, dry=not args.send)
    except client._Error as exc:
        return error(args, exc.code, request, exc.exit_code)
    except (OSError, ValueError, TypeError, KeyError):
        return error(args, "ATTACHMENT_DENIED", request)
    return emit(reply, args)


def refresh_request(args, account, peer, dest):
    request = envelope(args, "refresh", account)
    request.update(selector={"peer": peer, "dest": str(dest)}, checkpoint=True)
    return request
