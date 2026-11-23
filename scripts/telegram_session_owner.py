"""Local filesystem port and lifecycle of one Telegram session owner.

The Telegram connection is supplied by the caller; importing this module never
loads credentials, opens a session, or imports Telethon.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import asyncio
import ctypes
import errno
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import uuid

from telegram_mirror import Mirror


# Public adapter budgets include conversion/media work. Connect includes dialog
# pagination and its bounded FloodWait sleeps; transport/auth have a shorter
# deadline inside Telethon. RPC completions also reset the progress watchdog.
RPC_TIMEOUTS = {"connect": 240, "disconnect": 15, "resolve": 30,
                "history": 240, "recent": 60, "events": 60,
                "upload": 120, "send": 60, "scheduled": 30}
WATCHDOG_TIMEOUT = 300
STOP_TIMEOUT = 20
STOP_SEND_GRACE = 5
STOP_CANCEL_TIMEOUT = 5
STOP_DISCONNECT_TIMEOUT = 5
STOP_RESERVE = 5
HISTORY_PAGE_SIZE = 100
MIN_FAILURE_UPTIME = 60  # With RestartSec=15: at most 4 starts per 300s.
SWEEP_INTERVAL = 30
OUTBOX_SETTLE_SECONDS = 2
TERMINAL_STATES = frozenset(("sent", "scheduled", "refreshed", "rejected", "unknown"))


class HeartbeatWatchdog:
    """Exit even if the asyncio loop or cancellation of an RPC gets stuck."""

    def __init__(self, clock, *, terminate=None, interval=1):
        self.clock = clock
        self.terminate = os._exit if terminate is None else terminate
        self.interval = interval
        self._completed_at = clock.monotonic()
        self._stop_deadline = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="owner-heartbeat-watchdog")

    def completed(self):
        with self._lock:
            self._completed_at = self.clock.monotonic()

    def stopping(self, deadline):
        with self._lock:
            if self._stop_deadline is None:
                self._stop_deadline = deadline

    def check(self):
        with self._lock:
            elapsed = self.clock.monotonic() - self._completed_at
            stop_expired = (self._stop_deadline is not None
                            and time.monotonic() >= self._stop_deadline)
        if stop_expired:
            try:
                logging.getLogger(__name__).error("OWNER_STOP_TIMEOUT exit_code=1")
            finally:
                self.terminate(1)
            return
        if elapsed >= WATCHDOG_TIMEOUT:
            try:
                logging.getLogger(__name__).error(
                    "OWNER_WATCHDOG_TIMEOUT stalled_for=%.1f exit_code=1", elapsed)
            finally:
                # Do not await cleanup: it may be the stalled operation. The OS
                # releases locks; durable sending is recovered as unknown.
                self.terminate(1)

    def _run(self):
        while not self._stop.wait(self.interval):
            self.check()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.thread.join(timeout=2)


class FloodWait(Exception):
    """The failed RPC had no effect."""

    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


class Disconnected(Exception):
    pass


class HistoryPage(list):
    """Export rows plus progress through raw Telegram service-message ids."""

    def __init__(self, rows, *, next_id, complete):
        super().__init__(rows)
        self.next_id, self.complete = next_id, complete


class SendUncertain(Exception):
    def __init__(self, message_ids=None):
        super().__init__("SEND_UNCERTAIN")
        self.message_ids = list(message_ids or [])


class _Lock:
    def __init__(self, fd):
        self.fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return False


@contextmanager
def _directory(path: Path, *, create=False):
    """Open a directory through descriptors, refusing symlink components."""
    path = Path(path).absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part == "..":
                raise OSError(errno.EINVAL, "Unsafe path")
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class Store:
    """Concrete Linux filesystem adapter. Locks are acquired before returning."""

    def lock(self, path: Path, *, shared=False):
        path = Path(path)
        with _directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_NONBLOCK, 0o600, dir_fd=parent)
            try:
                held = os.fstat(fd)
                if not stat.S_ISREG(held.st_mode):
                    raise OSError(errno.EINVAL, "Unsafe lock file")
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                            | fcntl.LOCK_NB)
                current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                    raise OSError(errno.ESTALE, "Lock file changed")
            except BaseException:
                os.close(fd)
                raise
        return _Lock(fd)

    def inspect(self, path: Path) -> dict:
        path = Path(path)
        st = path.lstat()
        kind = ("file" if stat.S_ISREG(st.st_mode) else
                "dir" if stat.S_ISDIR(st.st_mode) else
                "symlink" if stat.S_ISLNK(st.st_mode) else "other")
        acl_safe = True
        # Extended ACLs grant permissions beyond the owner/mode contract.
        # Refuse them conservatively; an unreadable ACL is not evidence of safety.
        for name in ("system.posix_acl_access", "system.posix_acl_default"):
            try:
                os.getxattr(path, name, follow_symlinks=False)
            except OSError as error:
                if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                    raise
            else:
                acl_safe = False
        return {"realpath": str(path.resolve()), "uid": st.st_uid,
                "mode": stat.S_IMODE(st.st_mode), "acl_safe": acl_safe,
                "kind": kind, "size": st.st_size, "dev": st.st_dev,
                "ino": st.st_ino, "mtime": st.st_mtime,
                "mtime_ns": st.st_mtime_ns, "nlink": st.st_nlink}

    def list(self, path: Path) -> list[Path]:
        result = []
        with os.scandir(path) as entries:
            for entry in entries:
                p = Path(entry.path)
                result.append(p)
                if entry.is_dir(follow_symlinks=False):
                    result.extend(self.list(p))
        return sorted(result)

    def read(self, path: Path) -> bytes:
        return self.read_bounded(path, max_bytes=None)

    def read_bounded(self, path: Path, *, max_bytes: int | None) -> bytes:
        """Optional bounded read for clients validating JSON or local files."""
        path = Path(path)
        with _directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError(errno.EINVAL, "Unsafe file")
            if max_bytes is None:
                return stream.read()
            if os.fstat(stream.fileno()).st_size > max_bytes:
                raise OSError(errno.EFBIG, "Read limit exceeded")
            data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise OSError(errno.EFBIG, "Read limit exceeded")
            return data

    def atomic_write(self, path: Path, data: bytes) -> None:
        path = Path(path)
        with _directory(path.parent, create=True) as parent:
            tmp = f".{path.name}.{uuid.uuid4().hex}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(tmp, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def publish(self, source: Path, dest: Path) -> None:
        """Atomic rename with RENAME_NOREPLACE, including an empty destination."""
        source, dest = Path(source), Path(dest)
        with _directory(source.parent) as src, _directory(dest.parent) as dst:
            rename = ctypes.CDLL(None, use_errno=True).renameat2
            rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                               ctypes.c_char_p, ctypes.c_uint)
            rename.restype = ctypes.c_int
            if rename(src, os.fsencode(source.name), dst, os.fsencode(dest.name), 1):
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code))
            os.fsync(dst)
            os.fsync(src)

    def snapshot(self, source: Path, dest: Path, *, max_bytes: int) -> dict:
        source, dest = Path(source), Path(dest)
        with _directory(source.parent) as parent:
            fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise OSError(errno.EINVAL, "Unsafe source")
                if before.st_size > max_bytes:
                    raise OSError(errno.EFBIG, "Snapshot limit exceeded")
                with _directory(dest.parent, create=True) as output:
                    outfd = os.open(dest.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                    | os.O_NOFOLLOW, 0o600, dir_fd=output)
                    try:
                        digest, size = hashlib.sha256(), 0
                        with os.fdopen(outfd, "wb") as copy:
                            while True:
                                block = stream.read(min(65536, max_bytes - size + 1))
                                if not block:
                                    break
                                size += len(block)
                                if size > max_bytes:
                                    raise OSError(errno.EFBIG, "Snapshot limit exceeded")
                                digest.update(block)
                                copy.write(block)
                            after = os.fstat(stream.fileno())
                            current = os.stat(source.name, dir_fd=parent,
                                              follow_symlinks=False)
                            identity = lambda s: (s.st_dev, s.st_ino, s.st_size,
                                                  s.st_mtime_ns, s.st_ctime_ns)
                            if identity(before) != identity(after) or identity(after) != identity(current):
                                raise OSError(errno.ESTALE, "Source changed")
                            copy.flush()
                            os.fsync(copy.fileno())
                        os.fsync(output)
                    except BaseException:
                        os.unlink(dest.name, dir_fd=output)
                        raise
        return {"size": size, "sha256": digest.hexdigest(),
                "dev": before.st_dev, "ino": before.st_ino}

    def remove(self, path: Path) -> None:
        path = Path(path)
        with _directory(path.parent) as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(path.name, dir_fd=parent)
            else:
                os.unlink(path.name, dir_fd=parent)
            os.fsync(parent)


class _StartError(Exception):
    pass


def _absolute(value) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _StartError("INVALID_CONFIG")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise _StartError("INVALID_CONFIG")
    return path


def _session(path: Path) -> Path:
    return path if str(path).endswith(".session") else Path(str(path) + ".session")


class Owner:
    def __init__(self, config: dict, *, telegram_factory, clock, rng, store: Store):
        self.config = config
        self.telegram_factory, self.clock, self.rng, self.store = telegram_factory, clock, rng, store
        self._locks = []
        self._telegram = None
        self._started = False
        self._state = None
        self._degraded_reason = ("local", None)
        self._heartbeat_at = None
        self._retry_at = 0
        self._retry_delay = 15
        self._local_retry_delay = 15
        self._local_pause_until = 0
        self._step_recovering = False
        self._step_local_failed = False
        self._step_local_paused = False
        self._outage_since = None
        self._reconnect_at = 0
        self._reconnect_needed = False
        self._instance = str(uuid.uuid4())
        self._pace = {}
        self._stepping = False
        self._queue_cursor = ""
        self._terminal_ids = set()
        self._outbox_stamp = None
        self._outbox_paths = ()
        self._sweep_at = 0
        self._sweep_cursor = {"tmp": "", "status": ""}
        self._destinations = []
        self._mirrors = []
        self._pending_events = []
        self._overflow_pending = False
        self._overflow_catchup_at = 0
        self._watchdog = None
        self._stop_event = None
        self._stop_waiter = None
        self._stop_deadline = None
        self._stop_cleanup_failed = False
        self._cancel_spent = 0
        self._closing = False

    def _stopping(self):
        if self._stop_event is not None and self._stop_event.is_set():
            self._begin_stop()
        return self._stop_deadline is not None

    def _begin_stop(self):
        if self._stop_deadline is not None:
            return
        self._stop_deadline = time.monotonic() + STOP_TIMEOUT
        if self._watchdog is not None:
            self._watchdog.stopping(self._stop_deadline)
        logging.getLogger(__name__).info("OWNER_STOP_REQUESTED timeout=%s", STOP_TIMEOUT)
        if self._locks:
            try:
                self._heartbeat("stopping")
            except Exception:
                logging.getLogger(__name__).warning("OWNER_STOP_HEARTBEAT_FAILED")

    def _check_stop(self):
        if self._stopping():
            raise asyncio.CancelledError

    def _stop_remaining(self, reserve=0):
        return max(0, self._stop_deadline - time.monotonic() - reserve)

    def _stop_cleanup_budget(self, limit):
        # Borrow a small slice of the reserve so cooperative cleanup can run
        # across event-loop turns even when its normal share is exhausted.
        return min(self._stop_remaining(),
                   max(0.5, min(limit, self._stop_remaining(STOP_RESERVE))))

    async def _cancel_rpc(self, task):
        task.cancel()
        if self._stopping():
            began = time.monotonic()
            budget = self._stop_cleanup_budget(
                max(0, STOP_CANCEL_TIMEOUT - self._cancel_spent))
            done, _ = await asyncio.wait({task}, timeout=budget)
            self._cancel_spent += time.monotonic() - began
            if not done:
                # A live RPC must never outlive ownership. Exit the process
                # instead of unlocking underneath a cancellation-resistant send.
                logging.getLogger(__name__).error("OWNER_STOP_CANCEL_STALLED exit_code=1")
                os._exit(1)
        try:
            await task
        except (Exception, asyncio.CancelledError):
            pass

    def _optional_inspect(self, path):
        try:
            return self.store.inspect(path)
        except FileNotFoundError:
            return None

    def _safe(self, path, kind, *, missing=False):
        info = self._optional_inspect(path)
        if info is None and missing:
            return None
        mode = 0o700 if kind == "dir" else 0o600
        if (info is None or info["kind"] != kind or info["uid"] != os.getuid()
                or info["mode"] != mode or not info["acl_safe"]):
            raise _StartError("UNSAFE_PATH")
        # Ancestors may be shared (e.g. /tmp); infrastructure itself is private.
        for parent in path.parents:
            if self.store.inspect(parent)["kind"] != "dir":
                raise _StartError("UNSAFE_PATH")
        return info

    def _realpath(self, path):
        info = self._optional_inspect(path)
        if info is not None:
            return Path(info["realpath"])
        return self._realpath(path.parent) / path.name

    def _validate(self):
        config = self.config
        if not isinstance(config, dict):
            raise _StartError("INVALID_CONFIG")
        account = config.get("account")
        if (not isinstance(account, str) or re.fullmatch(r"[a-z0-9_-]{1,32}", account) is None
                or config.get("instance") != account):
            raise _StartError("INVALID_CONFIG")
        self.account = account
        self.root = _absolute(config.get("owner_root"))
        self.session_path = _session(_absolute(config.get("session_path")))
        auth = _absolute(config.get("auth_path"))
        registry = _absolute(config.get("installation_config"))
        try:
            accounts = json.loads(self.store.read(registry))["accounts"]
            if not isinstance(accounts, dict) or account not in accounts:
                raise ValueError
            roots = set()
            inodes = set()
            for name, entry in accounts.items():
                if re.fullmatch(r"[a-z0-9_-]{1,32}", name) is None or not isinstance(entry, dict):
                    raise ValueError
                paths = {key: _absolute(entry.get(key)) for key in
                         ("auth_path", "session_path", "owner_root")}
                root_info = self._optional_inspect(paths["owner_root"])
                real = str(self._realpath(paths["owner_root"]))
                inode = (root_info["dev"], root_info["ino"]) if root_info else None
                if real in roots or (inode is not None and inode in inodes):
                    raise ValueError
                roots.add(real)
                if inode is not None:
                    inodes.add(inode)
                if name == account:
                    expected = {"auth_path": auth, "session_path": self.session_path,
                                "owner_root": self.root}
                    for key, value in paths.items():
                        value = _session(value) if key == "session_path" else value
                        if value != expected[key]:
                            raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise _StartError("INVALID_CONFIG") from None
        self._safe(registry, "file")
        self._safe(self.root, "dir")
        self._safe(auth, "file")
        self._safe(auth.parent, "dir")
        self._safe(self.session_path.parent, "dir")
        # Check all existing spool resources before the Telegram factory.
        for path in self.store.list(self.root):
            info = self.store.inspect(path)
            self._safe(path, "dir" if info["kind"] == "dir" else "file")
        destinations = config.get("destinations")
        if not isinstance(destinations, list) or any(not isinstance(d, dict) for d in destinations):
            raise _StartError("INVALID_CONFIG")
        self._destinations = []
        ids, paths, inodes = set(), set(), set()
        for entry in destinations:
            did = entry.get("destination_id")
            peer = entry.get("peer")
            if (not isinstance(did, str) or not did or did in ids
                    or not (type(peer) is int and peer > 0 or isinstance(peer, str) and peer)
                    or not isinstance(entry.get("project_config"), dict)):
                raise _StartError("INVALID_CONFIG")
            project = _absolute(entry.get("project_root"))
            dest = entry.get("dest")
            if not isinstance(dest, str) or not dest:
                raise _StartError("INVALID_CONFIG")
            dest = Path(dest)
            path = dest if dest.is_absolute() else project / dest
            if ".." in path.parts:
                raise _StartError("INVALID_CONFIG")
            path = self._realpath(path)
            if not dest.is_absolute() and not path.is_relative_to(self._realpath(project)):
                raise _StartError("INVALID_CONFIG")
            if path in paths:
                raise _StartError("INVALID_CONFIG")
            existing = self._optional_inspect(path)
            inode = (existing["dev"], existing["ino"]) if existing else None
            if inode is not None and inode in inodes:
                raise _StartError("INVALID_CONFIG")
            # Mirrors may be readable by the project, but never writable by
            # another UID/group or reached through a substituted symlink.
            for ancestor in (project, *path.parents, path):
                info = self._optional_inspect(ancestor)
                if info is not None and info["kind"] != "dir":
                    raise _StartError("UNSAFE_PATH")
            ids.add(did)
            paths.add(path)
            if inode is not None:
                inodes.add(inode)
            self._destinations.append({**entry, "path": path})

    def _acquire(self, path):
        before = self._safe(path, "file", missing=True)
        try:
            lock = self.store.lock(path)
        except OSError as error:
            if isinstance(error, BlockingIOError):
                raise
            raise _StartError("UNSAFE_PATH") from None
        self._locks.append(lock)
        after = self._safe(path, "file")
        if before is not None and (before["dev"], before["ino"]) != (after["dev"], after["ino"]):
            raise _StartError("UNSAFE_PATH")

    async def _rpc(self, operation, *args, **kwargs):
        if not self._closing:
            self._check_stop()
        timeout = RPC_TIMEOUTS[operation]
        if self._closing and self._stopping():
            timeout = min(timeout, STOP_DISCONNECT_TIMEOUT,
                          self._stop_cleanup_budget(STOP_DISCONNECT_TIMEOUT))

        async def invoke():
            # The task may first run after the signal callback, even though it
            # was created before stop. Check at the actual adapter boundary.
            if not self._closing:
                self._check_stop()
            return await getattr(self._telegram, operation)(*args, **kwargs)

        rpc = asyncio.create_task(invoke())
        # Keep the adapter task itself: wait_for may otherwise wait forever for
        # its cancellation, or cancel it twice while it is joining children.
        task = asyncio.ensure_future(asyncio.wait_for(asyncio.shield(rpc), timeout=timeout))
        try:
            if self._stop_waiter is None:
                return await task
            done, _ = await asyncio.wait({task, self._stop_waiter},
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            self._begin_stop()
            if self._closing:
                timeout = min(timeout, STOP_DISCONNECT_TIMEOUT,
                              self._stop_cleanup_budget(STOP_DISCONNECT_TIMEOUT))
                done, _ = await asyncio.wait({task}, timeout=timeout)
                if task in done:
                    return task.result()
                await self._cancel_rpc(rpc)
                raise asyncio.TimeoutError
            elif operation == "send":
                # Only an already started send may continue after stop. Keep
                # the cancellation, disconnect and reserve shares available.
                grace = min(STOP_SEND_GRACE, self._stop_remaining(
                    STOP_CANCEL_TIMEOUT + STOP_DISCONNECT_TIMEOUT + STOP_RESERVE))
                done, _ = await asyncio.wait({task}, timeout=grace)
                if task in done:
                    return task.result()
            raise asyncio.CancelledError
        except asyncio.CancelledError:
            await self._cancel_rpc(rpc)
            raise
        except asyncio.TimeoutError:
            await self._cancel_rpc(rpc)
            # Only fixed operation names and budgets: no peers, paths, request
            # bodies or adapter exception text belong in the journal.
            logging.getLogger(__name__).warning(
                "OWNER_RPC_TIMEOUT operation=%s timeout=%s", operation, timeout)
            if operation == "send":
                self._network_failure(Disconnected())
                raise SendUncertain() from None
            raise Disconnected() from None
        finally:
            task.cancel()
            try:
                await task
            except (Exception, asyncio.CancelledError):
                pass
            # A step can contain many bounded RPCs. Only a stalled RPC (including
            # cancellation) or work without RPC/step completion should time out.
            if self._watchdog is not None:
                self._watchdog.completed()

    async def _catch_up(self):
        """Drain history before merging queued events, once per canonical peer."""
        if not self._mirrors:
            resolved = {}
            for destination in self._destinations:
                target = destination["peer"]
                if target not in resolved:
                    peer = await self._rpc("resolve", self.account, target)
                    if (peer.get("account") != self.account or peer.get("kind") not in ("User", "Chat", "Channel")
                            or type(peer.get("id")) is not int or peer["id"] <= 0):
                        raise _StartError("INVALID_CONFIG")
                    resolved[target] = peer
                self._mirrors.append(Mirror(destination, resolved[target], self.store))
        await self._sync_mirrors(self._mirrors)
        if self._pending_events:
            self._apply_events()
            # An orphan still absent from a completed history/recent check
            # cannot be synthesized. All other buffered events were merged.
            self._pending_events.clear()

    async def _sync_mirrors(self, mirrors):
        groups = {}
        for mirror in mirrors:
            key = (mirror.peer["kind"], mirror.peer["id"])
            groups.setdefault(key, []).append(mirror)
        for group in groups.values():
            self._check_stop()
            cursors = []
            boundaries = []
            for mirror in group:
                self._check_stop()
                with mirror.locked():
                    data, state = mirror.load()
                    cursor = mirror.cursor(data)
                    catchup = state.get("catchup", {})
                    cursors.append(catchup.get("after_id", cursor))
                    boundaries.append(catchup.get("from_id", cursor))
            after_id = min(cursors)
            while True:
                history = await self._rpc("history", group[0].peer,
                                          after_id=after_id, limit=HISTORY_PAGE_SIZE)
                complete = getattr(history, "complete", len(history) < HISTORY_PAGE_SIZE)
                next_id = getattr(history, "next_id", None)
                cursors = []
                for mirror, boundary in zip(group, boundaries):
                    with mirror.locked():
                        data, state = mirror.load()
                        if self._optional_inspect(mirror.result) is None:
                            state["bootstrapping"] = True
                        if mirror.merge_history(data, state, history, [], boundary):
                            raise ValueError("MIRROR_TOPIC_ROOT_MISSING")
                        cursor = next_id if next_id is not None else mirror.cursor(data)
                        # Raw service ids can exceed exported topic/root ids.
                        # Keep scanning progress and the original merge boundary
                        # durable without changing the export's cursor semantics.
                        state["catchup"] = {"after_id": cursor, "from_id": boundary}
                        # Every completed page survives a later history/recent
                        # timeout, including initial population from cursor 0.
                        mirror.commit(data, state)
                        cursors.append(cursor)
                if complete:
                    break
                cursor = min(cursors)
                if cursor <= after_id:
                    raise ValueError("MIRROR_HISTORY_NO_PROGRESS")
                after_id = cursor
            recent = await self._rpc("recent", group[0].peer, limit=50)
            for mirror in group:
                with mirror.locked():
                    data, state = mirror.load()
                    if mirror.merge_history(data, state, [], recent, mirror.cursor(data)):
                        raise ValueError("MIRROR_TOPIC_ROOT_MISSING")
                    # Keep the original full-bootstrap checkpoint boundary,
                    # even if initial history spanned several process starts.
                    if state.pop("bootstrapping", False):
                        state["boundary"] = deepcopy(data)
                    state.pop("catchup", None)
                    mirror.commit(data, state)

    def _apply_events(self):
        if any(e["kind"] == "overflow" for e in self._pending_events):
            self._overflow_pending = True
        needs_catchup = False
        orphans = []
        for mirror in self._mirrors:
            self._check_stop()
            batch = [e for e in self._pending_events if e.get("peer") is not None
                     and all(e["peer"].get(k) == mirror.peer[k] for k in ("account", "kind", "id"))
                     and e["kind"] in ("new", "edit", "delete", "topic")]
            if not batch:
                continue
            with mirror.locked():
                data, state = mirror.load()
                before = deepcopy((data, state))
                for event in batch:
                    if mirror.merge(data, state, event):
                        needs_catchup = True
                        if event not in orphans:
                            orphans.append(event)
                if (data, state) != before:
                    mirror.commit(data, state)
        self._pending_events = orphans
        return needs_catchup

    async def _refresh(self, request, st):
        import telegram_owner_client as client

        if "destination_id" in request:
            matches = [m for m in self._mirrors if m.destination["destination_id"] == request["destination_id"]]
        else:
            selector = request["selector"]
            path = self._realpath(Path(selector["dest"]))
            matches = [m for m in self._mirrors if m.peer["id"] == selector["peer"]
                       and m.directory == path]
        if len(matches) > 1:
            raise client._Error("INVALID_CONFIG")
        if not matches:
            raise client._Error("DESTINATION_NOT_REGISTERED")
        mirror = matches[0]
        with mirror.locked():
            _, state = mirror.load()
            saved = state.get("refreshes", {}).get(request["request_id"])
        if saved is not None:
            self._write_status(request, st, "refreshed", "REFRESHED", result=saved)
            return
        await self._sync_mirrors(matches)
        self._check_stop()
        with mirror.locked():
            data, state = mirror.load()
            result = mirror.checkpoint(data, state, request["request_id"], self.clock.utc())
            # Confirm the complete durable pair before publishing its Reply.
            mirror.load()
        self._write_status(request, st, "refreshed", "REFRESHED", result=result)

    def _set_state(self, state):
        if state != self._state:
            logger = logging.getLogger(__name__)
            if state == "degraded":
                reason, subtype = self._degraded_reason
                if subtype is None:
                    logger.warning("OWNER_STATE previous=%s state=%s reason=%s",
                                   self._state, state, reason)
                else:
                    logger.warning("OWNER_STATE previous=%s state=%s reason=%s subtype=%s",
                                   self._state, state, reason, subtype)
            else:
                logger.info("OWNER_STATE previous=%s state=%s", self._state, state)
            self._state = state

    def _heartbeat(self, state):
        if state != "stopping":
            self._check_stop()
        now = self.clock.monotonic()
        if state != self._state or self._heartbeat_at is None or now - self._heartbeat_at >= 5:
            data = {"schema_version": 1, "instance": self._instance,
                    "pid": os.getpid(), "state": state}
            self.store.atomic_write(self.root / "heartbeat.json",
                                    json.dumps(data).encode("utf-8"))
            self._set_state(state)
            self._heartbeat_at = now

    async def start(self) -> dict:
        self._check_stop()
        if self._started:
            return {"code": "READY", "exit_code": 0}
        try:
            self._validate()
            self._acquire(self.root / "owner.lock")
            self._acquire(self.session_path)
            # 5d: publish a non-secret registry for local clients before ready.
            registry = self._installation()
            self.store.atomic_write(self.root / "installation.json",
                                    json.dumps({"accounts": registry["accounts"]}).encode("utf-8"))
            self.store.atomic_write(self.root / "instance.json", json.dumps({
                key: str(self.config[key]) for key in
                ("auth_path", "session_path", "owner_root")}).encode("utf-8"))
            if self._optional_inspect(self.root / "heartbeat.json") is not None:
                self._degraded_reason = ("local", None)
                self._heartbeat("degraded")
            self._load_pace()
            self._telegram = self.telegram_factory()
            await self._rpc("connect")
            await self._catch_up()
            self._heartbeat("ready")
            self._reconnect_needed = False
            # A new ownership interval always recovers from durable statuses.
            self._terminal_ids.clear()
            self._outbox_stamp = None
            self._outbox_paths = ()
            self._sweep_at = self.clock.monotonic()
            self._started = True
            return {"code": "READY", "exit_code": 0}
        except BlockingIOError:
            code = "OWNER_ALREADY_RUNNING"
        except _StartError as error:
            code = str(error)
        except (Disconnected, FloodWait):
            code = "OWNER_UNAVAILABLE"
        except Exception:
            code = "INVALID_CONFIG"
        try:
            await self.close()
        except Exception:
            logging.getLogger(__name__).error("OWNER_START_CLEANUP_FAILED")
        return {"code": code, "exit_code": 7 if code == "OWNER_UNAVAILABLE" else 2}

    async def step(self) -> bool:
        """Do bounded maintenance and at most one send, without sleeping."""
        if not self._started or self._stepping:
            return False
        self._check_stop()
        self._stepping = True
        self._step_network_failed = False
        self._step_recovering = self._state == "degraded"
        self._step_local_failed = False
        self._step_local_paused = False
        progress = False
        try:
            self._heartbeat(self._state)
            now = self.clock.monotonic()
            if now >= self._sweep_at:
                # Include unsuccessful/busy sweeps in the rate limit.
                self._sweep_at = now + SWEEP_INTERVAL
                progress = self._sweep()
            if self.clock.monotonic() >= self._retry_at:
                if self._state == "degraded":
                    self._step_recovering = True
                    if (self._overflow_pending
                            and self.clock.monotonic() < self._overflow_catchup_at):
                        return await self._step_send(allow_rpc=False) or progress
                    if self._reconnect_needed:
                        if self.clock.monotonic() < self._reconnect_at:
                            return await self._step_send(allow_rpc=False) or progress
                        self._reconnect_at = self.clock.monotonic() + 15
                        # A stale transport may still report connected. Reset it
                        # only after a network failure, never for overflow/local IO.
                        logging.getLogger(__name__).info("OWNER_RECONNECT")
                        try:
                            await self._rpc("disconnect")
                        except Exception:
                            # Best effort: a broken old transport must not prevent
                            # connect. The RPC deadline bounds cooperative cleanup.
                            logging.getLogger(__name__).warning("OWNER_RECONNECT_DISCONNECT_FAILED")
                        await self._rpc("connect")
                        self._reconnect_needed = False
                    if self._overflow_pending:
                        self._overflow_catchup_at = self.clock.monotonic() + 5
                    await self._catch_up()
                    self._overflow_pending = False
                    self._heartbeat("ready")
                    progress = True
                events = await self._rpc("events")
                if events:
                    self._pending_events.extend(deepcopy(events))
                    progress = True
                needs_catchup = self._apply_events()
                if (needs_catchup or self._overflow_pending
                        and self.clock.monotonic() >= self._overflow_catchup_at):
                    self._degraded("overflow" if self._overflow_pending else "local")
                else:
                    progress = await self._step_send() or progress
                self._heartbeat(self._state)
                # Sending can handle a network failure without raising here.
                # Only a complete working step ends the current outage.
                if (self._state == "ready" and not self._step_network_failed
                        and not self._step_local_failed):
                    self._outage_since = None
                    self._retry_delay = 15
                    self._local_retry_delay = 15
                    self._local_pause_until = 0
            else:
                # Local recovery/expiry must continue during account backoff.
                progress = await self._step_send(allow_rpc=False) or progress
        except BlockingIOError:
            pass
        except Exception as error:
            self._network_failure(error)
        finally:
            self._stepping = False
        return progress

    @staticmethod
    def _flood_seconds(error):
        # Duck-typed Telegram adapters may translate their own FloodWait class.
        if isinstance(error, FloodWait) or type(error).__name__ == "FloodWait":
            return max(0, float(error.seconds))
        return None

    def _network_failure(self, error):
        seconds = self._flood_seconds(error)
        if seconds is None and not isinstance(error, Disconnected):
            # Local mirror/spool errors need catch-up on the live transport.
            self._local_failure()
            return
        self._step_network_failed = True
        now = self.clock.monotonic()
        if seconds is None:
            if self._outage_since is None:
                self._outage_since = now
            if now - self._outage_since < 120:
                delay = 15
            else:
                delay = max(30, self._retry_delay)
                self._retry_delay = min(300, delay * 2)
        else:
            delay = seconds
        self._retry_at = max(self._retry_at, now + delay)
        if seconds is None:
            self._reconnect_needed = True
            self._degraded("network", subtype="DISCONNECTED")
        reason = "FLOOD_WAIT" if seconds is not None else "DISCONNECTED"
        # Never log exception text: adapter/IO diagnostics may contain secrets.
        logging.getLogger(__name__).warning(
            "OWNER_RECOVERY_WAIT reason=%s retry_in=%s", reason, delay)

    def _local_failure(self, *, pause=False):
        self._step_local_failed = True
        now = self.clock.monotonic()
        # Maintenance can fail on every tick while RPCs are paused. Only a
        # failure after the local deadline starts and logs the next pause.
        if now < self._local_pause_until:
            return
        # A failed operation and its failed status write belong to one step.
        if (not self._step_local_paused
                and (pause or self._state == "degraded" or self._step_recovering)):
            delay = self._local_retry_delay
            self._local_retry_delay = min(300, delay * 2)
            self._local_pause_until = now + delay
            self._retry_at = max(self._retry_at, self._local_pause_until)
            self._step_local_paused = True
            logging.getLogger(__name__).warning(
                "OWNER_RECOVERY_WAIT reason=LOCAL retry_in=%g", self._retry_at - now)
        self._degraded()

    def _load_pace(self):
        import telegram_owner_client as client

        path = self.root / "pace.json"
        if self._optional_inspect(path) is not None:
            client._private(self.store, path, "file")
            self._pace = client._read_json(self.store, path)
            if not isinstance(self._pace, dict):
                raise _StartError("INVALID_CONFIG")
        else:
            last = client.read_last_account_status(self.root, self.account, store=self.store)
            self._pace = deepcopy(last.get("pace", {})) if last else {}

    def _save_pace(self):
        import telegram_owner_client as client

        data = client._json(self._pace)
        if len(data) > client.JSON_LIMIT:
            raise client._Error("QUEUE_FULL")
        self.store.atomic_write(self.root / "pace.json", data)

    def _write_status(self, request, previous, state, code, exit_code=0, *, result=None,
                      retry_after=None, retry_after_utc=None):
        import telegram_owner_client as client

        now = self.clock.utc()
        st = client._reply(request, code, exit_code, state=state,
                           result=deepcopy(result if result is not None else previous.get("result", {})))
        st.update(request_id=previous["request_id"], account=self.account)
        st.update(request_hash=previous["request_hash"],
                  accepted_at=previous.get("accepted_at", now), updated_at=now,
                  retain_until=None, pace=deepcopy(self._pace), retry_after=retry_after)
        if state in ("sent", "refreshed", "rejected"):
            st["retain_until"] = now + client.WEEK
        elif state == "scheduled":
            st["retain_until"] = st["result"]["at"] + client.WEEK
        if state == "unknown":
            st["next_step"] = "Проверьте результат вручную; автоматический повтор запрещен"
        if code == "DESTINATION_NOT_REGISTERED":
            st["next_step"] = "Зарегистрируйте account/chat_id/путь в destinations owner"
        if retry_after_utc is not None:
            st["retry_after_utc"] = retry_after_utc
        for key in ("pace_reservation",):
            if key in previous:
                st[key] = deepcopy(previous[key])
        data = client._json(st)
        if len(data) > client.JSON_LIMIT:
            raise client._Error("QUEUE_FULL")
        with self.store.lock(self.root / "quota.lock"):
            self.store.atomic_write(self.root / "status" / f"{st['request_id']}.json", data)
            if state in TERMINAL_STATES:
                self._terminal_ids.add(st["request_id"])
            else:
                self._terminal_ids.discard(st["request_id"])
            # A terminal status occupies its actual bytes; no future copies
            # need reservation. Retained bodies remain charged by _usage.
            if state not in ("queued", "sending"):
                reserve = self.root / "outbox" / st["request_id"] / ".reservation.json"
                if self._optional_inspect(reserve) is not None:
                    self.store.remove(reserve)
        return st

    def _sweep(self):
        """Rotate bounded batches so retained entries cannot starve cleanup."""
        import telegram_owner_client as client

        progress = False
        with self.store.lock(self.root / "quota.lock"):
            for name in ("tmp", "status"):
                directory = self.root / name
                if self._optional_inspect(directory) is None:
                    continue
                paths = [p for p in self.store.list(directory) if p.parent == directory]
                paths.sort(key=lambda p: p.name)
                cursor = self._sweep_cursor[name]
                paths = [p for p in paths if p.name > cursor] + [p for p in paths if p.name <= cursor]
                for path in paths[:16]:
                    self._sweep_cursor[name] = path.name
                    try:
                        if name == "tmp":
                            parts = path.name.split(".")
                            if len(parts) != 2 or not all(client._uuid(part) for part in parts):
                                # Only protocol packages are eligible for cleanup.
                                # Spool quota validation still inspects these entries.
                                continue
                            before = client._private(self.store, path, "dir")
                            if self.clock.utc() - before["mtime"] <= 3600:
                                continue
                            with self.store.lock(path / ".writer.lock"):
                                after = client._private(self.store, path, "dir")
                                if (before["dev"], before["ino"]) != (after["dev"], after["ino"]):
                                    continue
                                self.store.remove(path)
                        else:
                            if path.suffix != ".json" or not client._uuid(path.stem):
                                continue
                            st = client._status(self.store, self.root, path.stem)
                            until = st.get("retain_until")
                            if (st.get("state") not in ("sent", "scheduled", "refreshed", "rejected")
                                    or not client._number(until) or self.clock.utc() < until):
                                continue
                            package = self.root / "outbox" / path.stem
                            if self._optional_inspect(package) is not None:
                                client._private(self.store, package, "dir")
                                self.store.remove(package)
                            self.store.remove(path)
                            self._terminal_ids.discard(path.stem)
                        progress = True
                    except BlockingIOError:
                        continue
                    except (OSError, client._Error, ValueError, KeyError, TypeError):
                        # Status stays authoritative until outbox deletion and
                        # fsync succeed. Terminal entries can never be resent.
                        self._local_failure()
        return progress

    def _next_package(self):
        import telegram_owner_client as client

        directory = self.root / "outbox"
        info = self._optional_inspect(directory)
        if info is None:
            self._outbox_stamp = None
            self._outbox_paths = ()
            self._terminal_ids.clear()
            return None
        stamp = (info["dev"], info["ino"], info["mtime"],
                 info.get("mtime_ns"), info.get("nlink"))
        # ext4 can publish another package within the same directory timestamp
        # tick. Compare with real wall time: injected Clock UTC is unrelated to
        # filesystem mtimes. A young (or future) stamp is not yet a cache key.
        young = time.time() - info["mtime"] < OUTBOX_SETTLE_SECONDS
        if stamp != self._outbox_stamp or young:
            paths = sorted(p for p in self.store.list(directory)
                           if p.parent == directory and client._uuid(p.name))
            self._terminal_ids.intersection_update(p.name for p in paths)
            self._outbox_paths = paths
            # Recheck once more even if the next step runs after the settling
            # window: a young snapshot cannot establish a durable cache key.
            self._outbox_stamp = None if young else stamp
        paths = [p for p in self._outbox_paths if p.name not in self._terminal_ids]
        paths = ([p for p in paths if p.name > self._queue_cursor]
                 + [p for p in paths if p.name <= self._queue_cursor])
        for path in paths:
            st = client._status(self.store, self.root, path.name)
            if st is None or st.get("state") in ("queued", "sending"):
                self._queue_cursor = path.name
                return path, st
            # Only the owner writes statuses. Protocol clients never revive a
            # terminal UUID; a new send is published as a new outbox entry.
            if st.get("state") in TERMINAL_STATES:
                self._terminal_ids.add(path.name)
        return None

    def _installation(self):
        import telegram_owner_client as client

        registry = client._registry(self.store, self.config["installation_config"])
        client._private(self.store, registry["path"], "file")
        own = registry["accounts"].get(self.account)
        if own is None:
            raise client._Error("INVALID_CONFIG")
        for key in ("owner_root", "auth_path", "session_path"):
            configured, registered = Path(self.config[key]), Path(own[key])
            if key == "session_path":
                configured, registered = _session(configured), _session(registered)
            if client._realpath(self.store, configured) != client._realpath(self.store, registered):
                raise client._Error("INVALID_CONFIG")
        return registry

    def _accept(self, package, request, files, registry):
        """Independently validate originals and replace client copies under quota."""
        import telegram_owner_client as client

        sources = client._local_deny(request, registry, self.store)
        packages, used, statuses = client._usage(self.store, self.root)
        own_size = sum(self.store.inspect(p)["size"] for p in self.store.list(package)
                       if self.store.inspect(p)["kind"] == "file")
        reserve = package / ".reservation.json"
        if self._optional_inspect(reserve) is not None:
            own_size = max(own_size, client._read_json(self.store, reserve)["bytes"])
        required = len(client._json(request)) + sum(f["size"] for f in files) + client.JSON_LIMIT + 1024
        if (statuses > client.STATUS_LIMIT or used - own_size + required > client.SPOOL_LIMIT
                or client._queue_full(self.store, self.root, packages,
                                      limit=client.PACKAGE_LIMIT + 1)):
            raise client._Error("QUEUE_FULL")
        self.store.atomic_write(reserve, client._json({"request_id": request["request_id"], "bytes": required}))
        copies = package / "files"
        if self._optional_inspect(copies) is not None:
            self.store.remove(copies)
        uploads = []
        for index, (spec, before) in enumerate(zip(files, sources)):
            path = copies / str(index)
            try:
                copied = self.store.snapshot(Path(spec["source"]), path, max_bytes=client.FILE_LIMIT)
                after = self.store.inspect(Path(spec["source"]))
            except OSError as error:
                raise client._Error("LIMIT_EXCEEDED" if error.errno == errno.EFBIG
                                    else "ATTACHMENT_DENIED") from None
            if (copied["size"] != spec["size"] or copied["sha256"].lower() != spec["sha256"].lower()
                    or (copied["dev"], copied["ino"]) != (before["dev"], before["ino"])
                    or (after["dev"], after["ino"], after["size"]) !=
                       (before["dev"], before["ino"], before["size"])):
                raise client._Error("ATTACHMENT_DENIED")
            uploads.append(path)
        return uploads

    def _pace_wait(self, key, at):
        now = self.clock.utc()
        wait = 0
        gate = self._pace.get(key, {})
        for rec in gate.get("records", []):
            if at is None and not rec["scheduled"] and rec["at"] > now:
                continue
            diff = abs((now if at is None else at) - rec["at"])
            wait = max(wait, rec["gap"] - diff)
        if "records" not in gate and at is None:
            wait = max(wait, gate.get("next_allowed_at", now) - now)
        return max(0, wait)

    def _reserve_pace(self, request, peer, st):
        key = f"{peer['kind']}:{peer['id']}"
        reservation = st.get("pace_reservation")
        if reservation is None:
            gap = max(40, min(40 + len(request["text"]) / 250 * 60, 180)
                      * (1 - self.rng.uniform(0, 0.35)))
            reservation = {"key": key, "record": {"request_id": request["request_id"],
                           "at": st["result"]["at"] if st["result"]["at"] is not None else self.clock.utc(),
                           "gap": gap, "scheduled": st["result"]["at"] is not None}}
            st["pace_reservation"] = reservation
        else:
            reservation["record"]["at"] = (st["result"]["at"] if st["result"]["at"] is not None
                                            else self.clock.utc())
        gate = self._pace.setdefault(key, {"aliases": [], "records": []})
        aliases = [request["target"]["peer"], peer["id"]]
        if peer.get("username"):
            aliases.extend((peer["username"], "@" + peer["username"].lstrip("@")))
        for alias in aliases:
            if alias not in gate["aliases"]:
                gate["aliases"].append(alias)
        records = gate.setdefault("records", [])
        records[:] = [r for r in records if r["request_id"] != request["request_id"]
                      and r["at"] + r["gap"] > self.clock.utc()]
        records.append(deepcopy(reservation["record"]))
        gate["next_allowed_at"] = max(r["at"] + r["gap"] for r in records)

    def _recover_pace(self, snapshot):
        """A crashed request cannot replace newer gates from other requests."""
        for key, old in snapshot.items():
            gate = self._pace.setdefault(key, {"aliases": [], "records": []})
            for alias in old.get("aliases", []):
                if alias not in gate["aliases"]:
                    gate["aliases"].append(alias)
            records = gate.setdefault("records", [])
            known = {r["request_id"] for r in records}
            records.extend(deepcopy(r) for r in old.get("records", []) if r["request_id"] not in known)
            gate["next_allowed_at"] = max(gate.get("next_allowed_at", 0), old.get("next_allowed_at", 0))
        self._save_pace()

    async def _step_send(self, *, allow_rpc=True):
        import telegram_owner_client as client

        self._check_stop()
        selected = self._next_package()
        if selected is None:
            return False
        package, previous = selected
        request = {"request_id": package.name, "operation": "send", "account": self.account}
        st = previous or {"request_id": package.name, "request_hash": None, "accepted_at": self.clock.utc(),
                         "result": {"message_ids": [], "scheduled_ids": [],
                                                           "at": None, "verification": "api_ack"}}
        possible_send = False
        pace_before = None
        try:
            # Recovery precedes expiry/schema checks. The durable sending
            # marker reserves the outcome even if request.json was damaged.
            if previous and previous["state"] == "sending":
                try:
                    self._recover_pace(previous.get("pace", {}))
                except Exception:
                    self._local_failure(pause=True)
                else:
                    self._uncertain(request, st)
                return True
            with self.store.lock(self.root / "quota.lock"):
                client._private(self.store, package, "dir")
                client._private(self.store, package / "request.json", "file")
                registry = self._installation()
                request = client._read_json(self.store, package / "request.json")
                digest = hashlib.sha256(client._json(request)).hexdigest()
                if previous and previous["request_hash"] != digest:
                    return False
                st["request_hash"] = digest
                if not isinstance(request, dict):
                    raise client._Error("INVALID_REQUEST")
                if request.get("request_id") != package.name:
                    raise client._Error("INVALID_REQUEST")
                if request.get("account") != self.account:
                    raise client._Error("ACCOUNT_NOT_MANAGED")
                if not allow_rpc:
                    if client._number(request.get("expires_at")) and self.clock.utc() >= request["expires_at"]:
                        raise client._Error("REQUEST_EXPIRED")
                    return False
                _, files = client._validate(request, self.clock)
                if request["operation"] == "send":
                    at = client._schedule(request, self.clock)
                    if request["schedule"]["remind"]:
                        at = st["result"].get("at")
                        if at is None:
                            at = math.floor(self.clock.utc() + 180 + self.rng.randint(1, 59))
                    st["result"]["at"] = at
                    copies = self._accept(package, request, files, registry)
            if request["operation"] == "refresh":
                await self._refresh(request, st)
                return True
            if request["schedule"]["remind"]:
                # This must be durable before upload, including on retry.
                st = self._write_status(request, st, "queued", "QUEUED")
            peer = await self._rpc("resolve", self.account, request["target"]["peer"])
            self._check_stop()
            if (peer.get("account") != self.account or peer.get("kind") not in ("User", "Chat", "Channel")
                    or type(peer.get("id")) is not int or peer["id"] <= 0):
                raise client._Error("INVALID_REQUEST")
            expected = request["target"]["expected_username"]
            if expected is not None and expected.lstrip("@").casefold() != (peer.get("username") or "").lstrip("@").casefold():
                raise client._Error("USERNAME_MISMATCH")
            key = f"{peer['kind']}:{peer['id']}"
            if not request["options"]["no_pace_check"] and not request["schedule"]["remind"]:
                wait = self._pace_wait(key, at)
                if wait > 0:
                    self._write_status(request, st, "rejected", "PACE_TOO_EARLY", 3, retry_after=wait)
                    return True
            uploads = [await self._rpc("upload", path) for path in copies]
            self._check_stop()
            if self.clock.utc() >= request["expires_at"]:
                raise client._Error("REQUEST_EXPIRED")
            if at is not None and not 120 <= at - self.clock.utc() <= 366 * 86400:
                raise client._Error("INVALID_REQUEST")
            outgoing = deepcopy(request)
            outgoing["schedule"].update(at=at, timezone=None)
            pace_before = deepcopy(self._pace)
            self._reserve_pace(request, peer, st)
            st = self._write_status(request, st, "sending", "SENDING")
            possible_send = True
            self._save_pace()
            sent_at = self.clock.utc()
            ack = await self._rpc("send", outgoing, peer, uploads)
            result = {"message_ids": ack.get("message_ids", []),
                      "scheduled_ids": ack.get("scheduled_ids", []), "at": at, "verification": "api_ack"}
            st["result"] = result
            ids = result["scheduled_ids"] if at is not None else result["message_ids"]
            if (not isinstance(ids, list) or len(ids) != max(1, len(files))
                    or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids)):
                raise SendUncertain(result["message_ids"])
            if at is not None:
                if ack.get("at", at) != at:
                    raise SendUncertain(result["message_ids"])
                # Remind's +181..239s may be absent from Saved Messages' queue.
                # Allow processing down to the existing 120s minimum send lead.
                if not (request["schedule"]["remind"] and 120 <= at - sent_at <= 239):
                    self._check_stop()
                    verification_request = deepcopy(request)
                    verification_request["text"] = ack.get("text", request["text"])
                    result["verification"] = await self._verify_schedule(verification_request, peer, result)
            self._write_status(request, st, "scheduled" if at is not None else "sent",
                               "SCHEDULED" if at is not None else "SENT", result=result)
        except asyncio.CancelledError:
            if possible_send:
                self._uncertain(request, st)
            raise
        except BlockingIOError:
            if possible_send:
                self._uncertain(request, st)
            return False
        except Exception as error:
            seconds = self._flood_seconds(error)
            if possible_send and (seconds is None or st["result"].get("scheduled_ids")):
                if isinstance(error, SendUncertain) and error.message_ids:
                    st["result"]["message_ids"] = error.message_ids
                if isinstance(error, Disconnected) or seconds is not None:
                    self._network_failure(error)
                self._uncertain(request, st)
            elif (seconds is not None or isinstance(error, Disconnected)
                  or request.get("operation") == "refresh" and not isinstance(error, client._Error)):
                self._network_failure(error)
                if request.get("operation") == "refresh":
                    if seconds is not None:
                        self._degraded("network", subtype="FLOOD_WAIT")
                    else:
                        self._degraded()
                if pace_before is not None:
                    self._pace = pace_before
                    self._save_pace()
                retry = None if seconds is None else datetime.fromtimestamp(
                    self.clock.utc() + seconds, timezone.utc).isoformat().replace("+00:00", "Z")
                self._write_status(request, st, "queued", "QUEUED", retry_after_utc=retry)
            else:
                if pace_before is not None:
                    self._pace = pace_before
                    self._save_pace()
                if isinstance(error, client._Error):
                    code, exit_code = error.code, error.exit_code
                else:
                    code, exit_code = "INVALID_REQUEST", 2
                self._write_status(request, st, "rejected", code, exit_code)
        return True

    def _uncertain(self, request, st):
        try:
            self._write_status(request, st, "unknown", "SEND_UNCERTAIN", 4)
        except Exception:
            # The prior sending marker is sufficient to prevent another RPC.
            if self._stopping():
                self._stop_cleanup_failed = True
                logging.getLogger(__name__).error("OWNER_STOP_STATUS_FAILED")
            self._local_failure(pause=True)

    async def _verify_schedule(self, request, peer, result):
        unavailable = False
        try:
            scheduled = await self._rpc("scheduled", peer)
        except Exception as error:
            if isinstance(error, Disconnected) or self._flood_seconds(error) is not None:
                self._network_failure(error)
            scheduled, unavailable = [], True
        rows = [row for row in scheduled if row.get("id") in result["scheduled_ids"]]
        if rows:
            if (len(rows) != len(result["scheduled_ids"])
                    or {row["id"] for row in rows} != set(result["scheduled_ids"])
                    or any(row.get("at") != result["at"] or row.get("text") != request["text"] for row in rows)):
                raise SendUncertain(result["message_ids"])
            return "scheduled_verified"
        unavailable = unavailable or not rows
        if (unavailable and request["target"]["peer"] == "me" and not request["schedule"]["remind"]
                and 0 < result["at"] - self.clock.utc() <= 180):
            return "unavailable_saved_near"
        raise SendUncertain(result["message_ids"])

    def _degraded(self, reason="local", *, subtype=None):
        if self._stopping():
            return
        self._degraded_reason = (reason, subtype)
        try:
            self._heartbeat("degraded")
        except OSError:
            self._set_state("degraded")
            self._heartbeat_at = None
            logging.getLogger(__name__).error("OWNER_HEARTBEAT_FAILED")

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            if self._locks:
                try:
                    if not self._stopping():
                        self._heartbeat("stopping")
                except Exception:
                    if not self._stopping():
                        raise
                    logging.getLogger(__name__).warning("OWNER_STOP_HEARTBEAT_FAILED")
                finally:
                    if self._telegram is not None:
                        await self._close_transport()
            elif self._telegram is not None:
                await self._close_transport()
        finally:
            self._telegram = None
            self._started = False
            self._mirrors = []
            unlock_error = None
            while self._locks:
                try:
                    self._locks.pop().__exit__(None, None, None)
                except Exception as error:
                    self._stop_cleanup_failed = True
                    if unlock_error is None:
                        unlock_error = error
            self._closing = False
            if unlock_error is not None:
                raise unlock_error

    async def _close_transport(self):
        try:
            await self._rpc("disconnect")
        except (Exception, asyncio.CancelledError):
            if not self._stopping():
                raise
            logging.getLogger(__name__).warning("OWNER_STOP_DISCONNECT_FAILED")


async def _serve(owner, stop, watchdog):
    owner._watchdog = watchdog
    owner._stop_event = stop
    started_at = owner.clock.monotonic()

    async def stopping():
        await stop.wait()
        owner._begin_stop()

    owner._stop_waiter = asyncio.create_task(stopping())
    try:
        owner._check_stop()
        reply = await owner.start()
        watchdog.completed()
        if reply["exit_code"]:
            import sys
            print(reply["code"], file=sys.stderr)
            if reply["code"] == "OWNER_UNAVAILABLE":
                # A long outage must not exhaust StartLimitBurst=5/300.
                # Watchdog exits already take >=300s; fast startup failures
                # need this floor before systemd's additional RestartSec=15.
                delay = max(0, MIN_FAILURE_UPTIME - (owner.clock.monotonic() - started_at))
                if delay:
                    logging.getLogger(__name__).warning("OWNER_RESTART_WAIT retry_in=%.1f", delay)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                if stop.is_set():
                    return 0
            return reply["exit_code"]
        while not stop.is_set():
            await owner.step()
            watchdog.completed()
            # Bounded idle wait; signals wake it immediately.
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
        return 0
    except asyncio.CancelledError:
        if not owner._stopping():
            raise
    finally:
        try:
            await owner.close()
        except Exception:
            if not owner._stopping():
                raise
            owner._stop_cleanup_failed = True
            logging.getLogger(__name__).error("OWNER_STOP_CLEANUP_FAILED")
        finally:
            owner._stop_waiter.cancel()
            try:
                await owner._stop_waiter
            except asyncio.CancelledError:
                pass
            owner._stop_waiter = None
        if owner._stopping() and owner._stop_cleanup_failed:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one configured instance; the factory is reached only after locks."""
    import argparse
    import random
    import signal
    import sys
    from telegram_owner_cli import Clock

    parser = argparse.ArgumentParser(description="Владелец одной Telegram-сессии")
    parser.add_argument("--instance", required=True)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    logger = logging.getLogger(__name__)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    os.umask(0o077)
    try:
        store = Store()
        # Config is private infrastructure too; bound reads before decoding.
        import telegram_owner_client as client
        client._private(store, args.config.absolute(), "file")
        config = client._read_json(store, args.config.absolute())
        config["instance"] = args.instance
    except (OSError, ValueError, KeyError, TypeError, client._Error):
        print("INVALID_CONFIG", file=sys.stderr)
        return 2

    async def run():
        from telegram_owner_telethon import Telegram
        clock = Clock()
        owner = Owner(config, telegram_factory=lambda: Telegram(config),
                      clock=clock, rng=random.Random(), store=store)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def request_stop():
            stop.set()
            owner._begin_stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop)
        try:
            with HeartbeatWatchdog(clock) as watchdog:
                return await _serve(owner, stop, watchdog)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    return asyncio.run(run())
