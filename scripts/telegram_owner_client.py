"""Synchronous, local-only client for the Telegram session owner spool.

No credentials or Telegram libraries are loaded. All filesystem operations use
the supplied Store, including locks, bounded attachment copies and publication.
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

from telegram_session_owner import Store, _session

JSON_LIMIT = 64 * 1024
FILE_LIMIT = 64 * 1024 * 1024
REQUEST_LIMIT = 128 * 1024 * 1024
SPOOL_LIMIT = 512 * 1024 * 1024
PACKAGE_LIMIT = 100
STATUS_LIMIT = 10000
WEEK = 7 * 86400


class _Error(Exception):
    def __init__(self, code, exit_code=2):
        self.code, self.exit_code = code, exit_code


def _reply(request, code, exit_code=2, *, state="rejected", result=None):
    request = request if isinstance(request, dict) else {}
    if not exit_code and state == "rejected":
        state = "validated"
    return {"schema_version": 1, "request_id": request.get("request_id"),
            "operation": request.get("operation"), "account": request.get("account"),
            "state": state, "code": code, "exit_code": exit_code,
            "retry_after": None, "result": result or {},
            "next_step": "Проверьте локальную заявку и конфигурацию" if exit_code else None}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


def _account(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", value) is not None


def _path(value, code="INVALID_CONFIG"):
    if not isinstance(value, (str, Path)) or not str(value):
        raise _Error(code)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise _Error(code)
    return path


def _inspect(store, path):
    try:
        return store.inspect(path)
    except FileNotFoundError:
        return None


def _safe_parent(info):
    return (info["kind"] == "dir" and info["uid"] in (0, os.getuid())
            and info["acl_safe"]
            and (not info["mode"] & 0o022 or bool(info["mode"] & 0o1000)))


def _private(store, path, kind):
    info = store.inspect(path)
    if (info["kind"] != kind or info["uid"] != os.getuid()
            or info["mode"] != (0o700 if kind == "dir" else 0o600)
            or not info["acl_safe"]):
        raise _Error("UNSAFE_PATH")
    for parent in path.parents:
        if not _safe_parent(store.inspect(parent)):
            raise _Error("UNSAFE_PATH")
    return info


def _infrastructure(store, root):
    _private(store, root, "dir")
    # Heartbeat failures are availability failures, handled separately below.
    for name in ("tmp", "outbox", "status", "quota.lock", "owner.lock"):
        p = root / name
        if _inspect(store, p) is not None:
            _private(store, p, "dir" if name in ("tmp", "outbox", "status") else "file")
            if name in ("tmp", "outbox", "status"):
                for child in store.list(p):
                    try:
                        info = store.inspect(child)
                        _private(store, child, "dir" if info["kind"] == "dir" else "file")
                    except FileNotFoundError:
                        # Another writer can publish a tmp package or replace
                        # an atomic-write temporary between list and inspect.
                        continue


def _read_json(store, path, limit=JSON_LIMIT):
    before = store.inspect(path)
    if before["size"] > limit:
        raise _Error("LIMIT_EXCEEDED")
    data = _read_bytes(store, path, limit)
    after = store.inspect(path)
    if (before["dev"], before["ino"]) != (after["dev"], after["ino"]):
        raise _Error("UNSAFE_PATH")
    if len(data) > limit:
        raise _Error("LIMIT_EXCEEDED")
    return json.loads(data)


def _read_bytes(store, path, limit):
    # Keep the original duck-typed Store port usable; concrete Store additionally
    # bounds allocation while reading, even if the source grows after inspect.
    bounded = getattr(store, "read_bounded", None)
    try:
        data = bounded(path, max_bytes=limit) if bounded else store.read(path)
    except OSError as error:
        if error.errno == errno.EFBIG:
            raise _Error("LIMIT_EXCEEDED") from None
        raise
    if len(data) > limit:
        raise _Error("LIMIT_EXCEEDED")
    return data


def _ready(store, root, clock):
    try:
        path = root / "heartbeat.json"
        info = _private(store, path, "file")
        hb = _read_json(store, path)
        return (isinstance(hb, dict)
                and set(hb) == {"schema_version", "instance", "pid", "state"}
                and type(hb["schema_version"]) is int and hb["schema_version"] == 1
                and _uuid(hb["instance"]) and type(hb["pid"]) is int and hb["pid"] > 0
                and hb["state"] == "ready" and 0 <= clock.utc() - info["mtime"] <= 15)
    except (OSError, ValueError, TypeError, KeyError, _Error):
        return False


def _schedule(request, clock):
    schedule, options = request["schedule"], request["options"]
    if (set(schedule) != {"at", "timezone", "exact_minute", "remind"}
            or type(schedule["exact_minute"]) is not bool or type(schedule["remind"]) is not bool):
        raise _Error("INVALID_REQUEST")
    at, tz = schedule["at"], schedule["timezone"]
    if tz is not None and (not isinstance(tz, str) or not tz):
        raise _Error("INVALID_REQUEST")
    if schedule["remind"]:
        if request["target"]["peer"] != "me" or at is not None or tz is not None or options["silent"]:
            raise _Error("INVALID_REQUEST")
        return
    if at is None:
        if tz is not None or schedule["exact_minute"]:
            raise _Error("INVALID_REQUEST")
        return
    try:
        zone = ZoneInfo(tz) if tz is not None else None
        if _number(at):
            dt = datetime.fromtimestamp(at, timezone.utc)
        elif isinstance(at, str):
            dt = datetime.fromisoformat(at)
            if dt.tzinfo is None:
                if zone is None:
                    raise ValueError
                choices = {dt.replace(tzinfo=zone, fold=fold).timestamp() for fold in (0, 1)
                           if datetime.fromtimestamp(dt.replace(tzinfo=zone, fold=fold).timestamp(),
                                                     zone).replace(tzinfo=None) == dt}
                if len(choices) != 1:
                    raise ValueError
                dt = datetime.fromtimestamp(choices.pop(), zone)
            elif zone is not None and dt.utcoffset() != dt.astimezone(zone).utcoffset():
                raise ValueError
        else:
            raise ValueError
        if not 120 <= dt.timestamp() - clock.utc() <= 366 * 86400:
            raise ValueError
    except (ValueError, OverflowError, OSError, KeyError):
        raise _Error("INVALID_REQUEST") from None
    if dt.minute % 15 == 0 and not schedule["exact_minute"]:
        raise _Error("ROUND_MINUTE", 6)


def _validate(request, clock):
    if not isinstance(request, dict) or not _uuid(request.get("request_id")):
        raise _Error("INVALID_REQUEST")
    try:
        if (type(request["schema_version"]) is not int or request["schema_version"] != 1
                or not _account(request["account"])):
            raise _Error("INVALID_REQUEST")
        created, expires = request["created_at"], request["expires_at"]
        if (not _number(created) or not _number(expires) or created > clock.utc() + 5
                or not created < expires <= created + WEEK):
            raise _Error("INVALID_REQUEST")
        if clock.utc() >= expires:
            raise _Error("REQUEST_EXPIRED")
        if request["operation"] == "refresh":
            if request["checkpoint"] is not True:
                raise _Error("INVALID_REQUEST")
            if "destination_id" in request:
                if not isinstance(request["destination_id"], str) or not request["destination_id"] or "selector" in request:
                    raise _Error("INVALID_REQUEST")
            else:
                selector = request["selector"]
                if (not isinstance(selector, dict) or set(selector) != {"peer", "dest"}
                        or type(selector["peer"]) is not int):
                    raise _Error("INVALID_REQUEST")
                _path(selector["dest"], "INVALID_REQUEST")
            files = []
        elif request["operation"] == "send":
            target, options = request["target"], request["options"]
            if (set(target) != {"peer", "expected_username", "topic_id", "reply_to"}
                    or not (type(target["peer"]) is int or
                            isinstance(target["peer"], str) and bool(target["peer"]))
                    or target["expected_username"] is not None and not isinstance(target["expected_username"], str)
                    or any(v is not None and (type(v) is not int or v <= 0)
                           for v in (target["topic_id"], target["reply_to"]))
                    or set(options) != {"silent", "html", "voice", "no_pace_check"}
                    or any(type(v) is not bool for v in options.values())
                    or not isinstance(request["text"], str) or not isinstance(request["files"], list)):
                raise _Error("INVALID_REQUEST")
            files = request["files"]
            for f in files:
                if (not isinstance(f, dict) or set(f) != {"source", "name", "size", "sha256"}
                        or not isinstance(f["name"], str) or f["name"] in ("", ".", "..")
                        or Path(f["name"]).name != f["name"] or type(f["size"]) is not int or f["size"] < 0
                        or not isinstance(f["sha256"], str) or re.fullmatch(r"[a-fA-F0-9]{64}", f["sha256"]) is None):
                    raise _Error("INVALID_REQUEST")
                _path(f["source"], "INVALID_REQUEST")
            _schedule(request, clock)
            if (not request["text"] and not files or options["voice"] and len(files) != 1
                    or len(files) >= 2 and (request["schedule"]["at"] is not None
                                            or request["schedule"]["remind"] or options["voice"] or options["html"])):
                raise _Error("INVALID_REQUEST")
        else:
            raise _Error("INVALID_REQUEST")
        data = _json(request)
        if (len(data) > JSON_LIMIT or len(files) > 10 or any(f["size"] > FILE_LIMIT for f in files)
                or sum(f["size"] for f in files) > REQUEST_LIMIT
                or len(request.get("text", "").encode("utf-16-le")) // 2 > (1024 if files else 4096)):
            raise _Error("LIMIT_EXCEEDED")
        return data, files
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise _Error("INVALID_REQUEST") from None


def _status(store, root, rid):
    path = root / "status" / f"{rid}.json"
    if _inspect(store, path) is None:
        return None
    _private(store, path, "file")
    value = _read_json(store, path)
    if not isinstance(value, dict) or value.get("request_id") != rid:
        raise _Error("INVALID_REQUEST")
    return value


def status(owner_root: Path, request_id: str, *, clock) -> dict:
    request = {"request_id": request_id}
    try:
        if not _uuid(request_id):
            raise _Error("INVALID_REQUEST")
        root, store = _path(owner_root), Store()
        _infrastructure(store, root)
        return _status(store, root, request_id) or _reply(request, "NOT_FOUND", state="absent")
    except _Error as error:
        return _reply(request, error.code, error.exit_code)
    except (OSError, ValueError, TypeError):
        return _reply(request, "INVALID_CONFIG")


def _existing(store, root, request, digest):
    rid = request["request_id"]
    st = _status(store, root, rid)
    if st is not None:
        if st.get("request_hash") != digest:
            raise _Error("REQUEST_ID_CONFLICT")
        return True, st
    path = root / "outbox" / rid / "request.json"
    if _inspect(store, path) is not None:
        if hashlib.sha256(_json(_read_json(store, path))).hexdigest() != digest:
            raise _Error("REQUEST_ID_CONFLICT")
        return True, None
    return False, None


def _wait(store, root, request, digest, clock, deadline):
    while True:
        st = _status(store, root, request["request_id"])
        if st is not None:
            if st.get("request_hash") != digest:
                raise _Error("REQUEST_ID_CONFLICT")
            if st.get("state") not in ("queued", "sending"):
                return st
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            reply = dict(st) if st is not None else _reply(request, "WAIT_TIMEOUT", 4, state="queued")
            reply.update(code="WAIT_TIMEOUT", exit_code=4,
                         next_step="Запросите status с прежним request_id")
            return reply
        clock.sleep_sync(min(0.1, remaining))


def _quota_lock(store, root, clock, deadline):
    # The OS lock must get a real two-second opportunity even with FakeClock.
    real_deadline = time.monotonic() + 2
    while True:
        try:
            return store.lock(root / "quota.lock")
        except BlockingIOError:
            remaining = max(real_deadline - time.monotonic(), deadline - clock.monotonic())
            if remaining <= 0:
                raise _Error("WAIT_TIMEOUT", 4) from None
            delay = min(0.1, remaining)
            started = time.monotonic()
            clock.sleep_sync(delay)
            # Pace real retries without doubling a real Clock's sleep.
            unslept = delay - (time.monotonic() - started)
            if unslept > 0:
                time.sleep(unslept)


def _usage(store, root):
    packages, used, pending_ids = [], 0, set()
    for name in ("tmp", "outbox"):
        directory = root / name
        if _inspect(store, directory) is None:
            continue
        paths = store.list(directory)
        sizes = {}
        for p in paths:
            info = store.inspect(p)
            if (info["kind"] not in ("dir", "file") or info["uid"] != os.getuid()
                    or info["mode"] != (0o700 if info["kind"] == "dir" else 0o600)
                    or not info["acl_safe"]):
                raise _Error("UNSAFE_PATH")
            package = directory / p.relative_to(directory).parts[0]
            if p.parent == directory and info["kind"] == "dir":
                packages.append(p)
            elif info["kind"] == "file":
                sizes[package] = sizes.get(package, 0) + info["size"]
        for p in paths:
            if p.parent != directory or p not in packages:
                continue
            actual = sizes.get(p, 0)
            reserve = p / ".reservation.json"
            reserved = 0
            if _inspect(store, reserve) is not None:
                reservation = _read_json(store, reserve)
                reserved = reservation["bytes"]
                if type(reserved) is not int or reserved < 0 or not _uuid(reservation["request_id"]):
                    raise _Error("INVALID_CONFIG")
                pending_ids.add(reservation["request_id"])
            used += max(actual, reserved)
    statuses = root / "status"
    status_ids = set()
    if _inspect(store, statuses) is not None:
        for p in store.list(statuses):
            if p.parent == statuses and p.suffix == ".json":
                status_ids.add(p.stem)
                # A reservation already includes one maximum-size status.
                if p.stem not in pending_ids:
                    used += store.inspect(p)["size"]
    # Published legacy packages also reserve their future status slot.
    pending_ids.update(p.name.split(".")[0] for p in packages)
    return len(packages), used, len(status_ids | pending_ids)


def _quota(store, root, request, data, files):
    packages, used, statuses = _usage(store, root)
    # Reserve JSON, immutable copies, maximum status and reservation metadata.
    size = len(data) + sum(f["size"] for f in files) + JSON_LIMIT + 1024
    if packages >= PACKAGE_LIMIT or used + size > SPOOL_LIMIT or statuses >= STATUS_LIMIT:
        raise _Error("QUEUE_FULL")
    return size


def _ensure_directory(store, path):
    # Only the immediate parent is guaranteed to get mode 0700 by the Store
    # port. Create spool levels separately so recursive mkdir implementations
    # cannot leave intermediate directories with umask-dependent permissions.
    if _inspect(store, path) is None:
        marker = path / f".{uuid.uuid4()}.init"
        try:
            store.atomic_write(marker, b"")
        finally:
            if _inspect(store, marker) is not None:
                store.remove(marker)
    _private(store, path, "dir")


def submit(owner_root: Path, request: dict, *, clock, timeout: float = 30,
           store: Store | None = None) -> dict:
    store = Store() if store is None else store
    package = writer = None
    published = False
    try:
        if not _number(timeout):
            raise _Error("INVALID_REQUEST")
        started = clock.monotonic()
        deadline = started + max(0, timeout)
        # Clarifications 5a/5b give quota acquisition at least two real seconds;
        # waiting for the result keeps the original Clock deadline.
        quota_deadline = started + max(2, timeout)
        root = _path(owner_root)
        try:
            store.inspect(root)
        except (FileNotFoundError, PermissionError):
            return _reply(request, "OWNER_UNAVAILABLE", 7, state="unavailable")
        _infrastructure(store, root)
        # Dedupe precedes TTL and readiness: an accepted id keeps its outcome.
        if not isinstance(request, dict) or not _uuid(request.get("request_id")):
            raise _Error("INVALID_REQUEST")
        try:
            digest = hashlib.sha256(_json(request)).hexdigest()
        except (TypeError, ValueError, UnicodeError):
            raise _Error("INVALID_REQUEST") from None
        exists, st = _existing(store, root, request, digest)
        if exists:
            return _wait(store, root, request, digest, clock, deadline)
        data, files = _validate(request, clock)
        if not _ready(store, root, clock):
            return _reply(request, "OWNER_UNAVAILABLE", 7, state="unavailable")
        sources = _sources(store, files)
        # Hold the shared quota lock throughout reservation/copy/publication.
        # This also serializes rollback with every other accepting process.
        with _quota_lock(store, root, clock, quota_deadline):
            exists, st = _existing(store, root, request, digest)
            if not exists:
                _validate(request, clock)
                if not _ready(store, root, clock):
                    return _reply(request, "OWNER_UNAVAILABLE", 7, state="unavailable")
                size = _quota(store, root, request, data, files)
                package = root / "tmp" / f"{request['request_id']}.{uuid.uuid4()}"
                try:
                    _ensure_directory(store, root / "tmp")
                    # The tmp parent is already private. quota.lock prevents
                    # cleanup from seeing a package before its writer lock.
                    store.atomic_write(package / ".writer.lock", b"")
                    writer = store.lock(package / ".writer.lock")
                    store.atomic_write(package / ".reservation.json",
                                       _json({"request_id": request["request_id"], "bytes": size}))
                    store.atomic_write(package / "request.json", data)
                    for index, (spec, before) in enumerate(zip(files, sources)):
                        copied = store.snapshot(Path(spec["source"]), package / "files" / str(index),
                                                max_bytes=FILE_LIMIT)
                        if (copied["size"] != spec["size"] or copied["sha256"].lower() != spec["sha256"].lower()
                                or (copied["dev"], copied["ino"]) != (before["dev"], before["ino"])):
                            raise _Error("ATTACHMENT_DENIED")
                    outbox = root / "outbox"
                    _ensure_directory(store, outbox)
                    store.publish(package, outbox / request["request_id"])
                    published = True
                except OSError as error:
                    # A failure after rename must never remove the accepted id.
                    published = _inspect(store, root / "outbox" / request["request_id"]) is not None
                    if not published:
                        raise _Error("LIMIT_EXCEEDED" if error.errno == errno.EFBIG else
                                     "ATTACHMENT_DENIED" if files else "INVALID_REQUEST") from None
                finally:
                    try:
                        if not published and package is not None and _inspect(store, package) is not None:
                            store.remove(package)
                    finally:
                        if writer is not None:
                            writer.__exit__(None, None, None)
        return _wait(store, root, request, digest, clock, deadline)
    except _Error as error:
        return _reply(request, error.code, error.exit_code,
                      state="queued" if error.code == "WAIT_TIMEOUT" else "rejected")
    except (OSError, ValueError, TypeError, KeyError):
        return _reply(request, "INVALID_CONFIG")


def refresh(owner_root: Path, request: dict, *, clock, timeout: float = 30) -> dict:
    if not isinstance(request, dict) or request.get("operation") != "refresh":
        return _reply(request, "INVALID_REQUEST")
    return submit(owner_root, request, clock=clock, timeout=timeout)


def _realpath(store, path):
    info = _inspect(store, path)
    if info is not None:
        return Path(info["realpath"])
    return _realpath(store, path.parent) / path.name


def _registry(store, path):
    try:
        path = _path(path)
        data = _read_json(store, path)
        accounts = data["accounts"]
        if not isinstance(accounts, dict) or not accounts:
            raise ValueError
        roots, inodes = set(), set()
        for account, entry in accounts.items():
            if not _account(account) or not isinstance(entry, dict):
                raise ValueError
            paths = {k: _path(entry.get(k)) for k in ("auth_path", "session_path", "owner_root")}
            root = _realpath(store, paths["owner_root"])
            info = _inspect(store, paths["owner_root"])
            inode = (info["dev"], info["ino"]) if info else None
            if root in roots or inode is not None and inode in inodes:
                raise ValueError
            roots.add(root)
            if inode is not None:
                inodes.add(inode)
        return {"path": path, "accounts": accounts}
    except (OSError, ValueError, TypeError, KeyError, _Error):
        raise _Error("INVALID_CONFIG") from None


def load_installation_config(owner_config: Path, *, store: Store) -> dict:
    """Read and validate routing/registry metadata, never auth/session contents.

    Invalid configuration raises ValueError with a safe, stable code.
    """
    try:
        config = _read_json(store, _path(owner_config))
        registry = _registry(store, config["installation_config"])
        managed = config["managed_accounts"]
        if not isinstance(managed, dict) or not managed:
            raise ValueError
        for account, root in managed.items():
            if not _account(account) or account not in registry["accounts"]:
                raise ValueError
            if _realpath(store, _path(root)) != _realpath(store, Path(registry["accounts"][account]["owner_root"])):
                raise ValueError
        return registry
    except (OSError, ValueError, TypeError, KeyError, _Error):
        raise ValueError("INVALID_CONFIG") from None


def _deny_index(store, registry):
    protected = {registry["path"], Path.home() / ".config" / "telegram-snapshot"}
    for entry in registry["accounts"].values():
        session = Path(entry["session_path"])
        protected.update((Path(entry["auth_path"]).parent, Path(entry["owner_root"]),
                          session, _session(session)))
        for s in (session, _session(session)):
            protected.update(Path(str(s) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    paths, inodes = set(), set()
    try:
        for p in protected:
            real = _realpath(store, p)
            paths.update((p, real))
            for tree in {p, real}:
                info = _inspect(store, tree)
                if info is None:
                    continue
                children = store.list(tree) if info["kind"] == "dir" else []
                for child in [tree, *children]:
                    meta = store.inspect(child)
                    paths.update((child, Path(meta["realpath"])))
                    inodes.add((meta["dev"], meta["ino"]))
        return paths, inodes
    except (OSError, ValueError, KeyError):
        raise _Error("INVALID_CONFIG") from None


def _sources(store, files, deny=None):
    result, all_metadata = [], []
    for spec in files:
        path = Path(spec["source"])
        try:
            components = [path, *path.parents]
            metadata = [(p, store.inspect(p)) for p in components]
            info = metadata[0][1]
            if info["kind"] != "file" or any(m["kind"] != "dir" for p, m in metadata[1:]):
                raise _Error("ATTACHMENT_DENIED")
            if deny is not None:
                paths, inodes = deny
                real = Path(info["realpath"])
                if (any(p == path or p in path.parents or p == real or p in real.parents for p in paths)
                        or any((m["dev"], m["ino"]) in inodes for p, m in metadata)):
                    raise _Error("ATTACHMENT_DENIED")
            if any(re.fullmatch(r".*\.session(?:-.*)?", p.name) for p in components):
                raise _Error("ATTACHMENT_DENIED")
            result.append(info)
            all_metadata.append(metadata)
        except OSError:
            raise _Error("ATTACHMENT_DENIED") from None
    # Validate all source identities before checking any permissions or sizes.
    for path_metadata in all_metadata:
        for index, (p, meta) in enumerate(path_metadata):
            safe = (_safe_parent(meta) if index else
                    meta["uid"] == os.getuid() and not meta["mode"] & 0o022 and meta["acl_safe"])
            if not safe:
                raise _Error("UNSAFE_PATH")
    for spec, info in zip(files, result):
        if info["mode"] & 0o400 == 0:
            raise _Error("ATTACHMENT_DENIED")
        if info["size"] > FILE_LIMIT:
            raise _Error("LIMIT_EXCEEDED")
        if info["size"] != spec["size"]:
            raise _Error("ATTACHMENT_DENIED")
    if sum(i["size"] for i in result) > REQUEST_LIMIT:
        raise _Error("LIMIT_EXCEEDED")
    return result


def _local_deny(request, registry, store):
    if request["account"] not in registry["accounts"]:
        raise _Error("ACCOUNT_NOT_MANAGED")
    return _sources(store, request.get("files", []), _deny_index(store, registry))


def check_local_denylist(request: dict, installation_config: Path, *, store: Store) -> dict:
    try:
        registry = _registry(store, installation_config)
        _local_deny(request, registry, store)
        return _reply(request, "LOCAL_CHECK_OK", 0)
    except _Error as error:
        return _reply(request, error.code, error.exit_code)
    except (OSError, ValueError, KeyError, TypeError):
        return _reply(request, "INVALID_REQUEST")


def read_last_account_status(owner_root: Path, account: str, *, store: Store) -> dict | None:
    root = _path(owner_root)
    directory = root / "status"
    if _inspect(store, directory) is None:
        return None
    candidates = []
    for path in store.list(directory):
        if path.parent != directory or path.suffix != ".json" or not _uuid(path.stem):
            continue
        _private(store, path, "file")
        st = _read_json(store, path)
        if (isinstance(st, dict) and st.get("account") == account and _number(st.get("updated_at"))
                and st.get("request_id") == path.stem):
            candidates.append(st)
    return max(candidates, key=lambda s: (s["updated_at"], s["request_id"]), default=None)


def check_local_pace(request: dict, last_status: dict | None, *, clock) -> dict:
    result = {"account": request.get("account"), "request_id": None, "updated_at": None,
              "age_seconds": None, "message": "адресат не проверен; данные темпа отсутствуют",
              "resolve": "resolve отсутствует"}
    try:
        if last_status is not None and last_status.get("account") == request["account"]:
            result.update(request_id=last_status.get("request_id"), updated_at=last_status.get("updated_at"))
            if _number(result["updated_at"]):
                result["age_seconds"] = clock.utc() - result["updated_at"]
            peer = request.get("target", {}).get("peer")
            pace = last_status.get("pace", {})
            for key, gate in pace.items():
                if peer != key and peer not in gate.get("aliases", []):
                    continue
                at = gate.get("next_allowed_at")
                if not _number(at):
                    continue
                result["message"] = "адресат не проверен; использованы сохраненные данные темпа"
                schedule, options = request.get("schedule", {}), request.get("options", {})
                if (request.get("operation") == "send" and not options.get("no_pace_check")
                        and not schedule.get("remind") and schedule.get("at") is None and clock.utc() < at):
                    reply = _reply(request, "PACE_TOO_EARLY", 2, result=result)
                    reply["retry_after"] = at - clock.utc()
                    return reply
        return _reply(request, "LOCAL_CHECK_OK", 0, result=result)
    except (KeyError, TypeError, AttributeError):
        return _reply(request, "INVALID_REQUEST", result=result)


def dry_run(owner_root: Path, request: dict, *, installation_config: Path,
            clock, store: Store) -> dict:
    report = {"message": "адресат не проверен; данные темпа отсутствуют", "resolve": "resolve отсутствует"}
    try:
        root = _path(owner_root)
        registry = _registry(store, installation_config)
        try:
            store.inspect(root)
        except (FileNotFoundError, PermissionError):
            return _reply(request, "OWNER_UNAVAILABLE", 7, state="unavailable", result=report)
        _infrastructure(store, root)
        if not isinstance(request, dict) or request.get("account") not in registry["accounts"]:
            raise _Error("ACCOUNT_NOT_MANAGED")
        configured = Path(registry["accounts"][request["account"]]["owner_root"])
        a, b = store.inspect(root), store.inspect(configured)
        if a["realpath"] != b["realpath"] or (a["dev"], a["ino"]) != (b["dev"], b["ino"]):
            raise _Error("INVALID_CONFIG")
        if not _ready(store, root, clock):
            return _reply(request, "OWNER_UNAVAILABLE", 7, state="unavailable", result=report)
        data, files = _validate(request, clock)
        _local_deny(request, registry, store)
        # Read-only integrity check. No snapshots, locks or reservations.
        for spec in files:
            before = store.inspect(Path(spec["source"]))
            data = _read_bytes(store, Path(spec["source"]), FILE_LIMIT)
            after = store.inspect(Path(spec["source"]))
            if len(data) > FILE_LIMIT:
                raise _Error("LIMIT_EXCEEDED")
            if ((before["dev"], before["ino"], before["size"]) !=
                    (after["dev"], after["ino"], after["size"])
                    or len(data) != spec["size"] or hashlib.sha256(data).hexdigest() != spec["sha256"].lower()):
                raise _Error("ATTACHMENT_DENIED")
        last = read_last_account_status(root, request["account"], store=store)
        return check_local_pace(request, last, clock=clock)
    except _Error as error:
        return _reply(request, error.code, error.exit_code, result=report)
    except (OSError, ValueError, KeyError, TypeError):
        return _reply(request, "INVALID_CONFIG", result=report)
