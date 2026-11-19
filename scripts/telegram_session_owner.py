"""Local filesystem port and lifecycle of one Telegram session owner.

The Telegram connection is supplied by the caller; importing this module never
loads credentials, opens a session, or imports Telethon.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import uuid


class FloodWait(Exception):
    """The failed RPC had no effect."""

    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


class Disconnected(Exception):
    pass


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
                "ino": st.st_ino, "mtime": st.st_mtime}

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
        self._heartbeat_at = None
        self._retry_at = 0
        self._instance = str(uuid.uuid4())

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

    async def _catch_up(self):
        """Run the startup history boundary; mirror merging is a separate slice."""
        self._history = []
        seen = set()
        for destination in self.config["destinations"]:
            peer = await self._telegram.resolve(self.account, destination["peer"])
            key = (peer["kind"], peer["id"])
            if key in seen:
                continue
            seen.add(key)
            history = await self._telegram.history(peer, after_id=0, limit=None)
            self._history.append((peer, history))

    def _heartbeat(self, state):
        now = self.clock.monotonic()
        if state != self._state or self._heartbeat_at is None or now - self._heartbeat_at >= 5:
            data = {"schema_version": 1, "instance": self._instance,
                    "pid": os.getpid(), "state": state}
            self.store.atomic_write(self.root / "heartbeat.json",
                                    json.dumps(data).encode("utf-8"))
            self._state, self._heartbeat_at = state, now

    async def start(self) -> dict:
        if self._started:
            return {"code": "READY", "exit_code": 0}
        try:
            self._validate()
            self._acquire(self.root / "owner.lock")
            self._acquire(self.session_path)
            self._telegram = self.telegram_factory()
            await self._telegram.connect()
            await self._catch_up()
            self._heartbeat("ready")
            self._started = True
            return {"code": "READY", "exit_code": 0}
        except BlockingIOError:
            code = "OWNER_ALREADY_RUNNING"
        except _StartError as error:
            code = str(error)
        except Exception:
            code = "INVALID_CONFIG"
        try:
            await self.close()
        except Exception:
            logging.getLogger(__name__).error("OWNER_START_CLEANUP_FAILED")
        return {"code": code, "exit_code": 2}

    async def step(self) -> bool:
        """Maintain readiness without sleeping; request processing follows later."""
        if not self._started:
            return False
        now = self.clock.monotonic()
        progress = False
        try:
            if now >= self._retry_at:
                if self._state == "degraded":
                    await self._telegram.connect()
                    await self._catch_up()
                    self._heartbeat("ready")
                    progress = True
                events = await self._telegram.events()
                if events:
                    # Keep the batch until the mirror slice supplies its merge.
                    self._history.append((None, events))
                    progress = True
                if any(event["kind"] == "overflow" for event in events):
                    self._heartbeat("degraded")
            self._heartbeat(self._state)
        except FloodWait as error:
            self._retry_at = self.clock.monotonic() + error.seconds
            self._degraded()
        except Exception:
            self._retry_at = self.clock.monotonic() + 15
            self._degraded()
        return progress

    def _degraded(self):
        try:
            self._heartbeat("degraded")
        except OSError:
            self._state = "degraded"
            self._heartbeat_at = None
            logging.getLogger(__name__).error("OWNER_HEARTBEAT_FAILED")

    async def close(self) -> None:
        try:
            if self._locks:
                try:
                    self._heartbeat("stopping")
                finally:
                    if self._telegram is not None:
                        await self._telegram.disconnect()
            elif self._telegram is not None:
                await self._telegram.disconnect()
        finally:
            self._telegram = None
            self._started = False
            unlock_error = None
            while self._locks:
                try:
                    self._locks.pop().__exit__(None, None, None)
                except Exception as error:
                    if unlock_error is None:
                        unlock_error = error
            if unlock_error is not None:
                raise unlock_error


def main(argv: list[str] | None = None) -> int:
    """CLI wiring and the real Telegram adapter are implemented in slice 5."""
    logging.getLogger(__name__).error("INVALID_CONFIG: Telegram CLI adapter unavailable")
    return 2
