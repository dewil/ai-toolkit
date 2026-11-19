#!/usr/bin/env python3
"""Общие фейки для tests/test_telegram_session_owner*.py (stdlib-only).

Только описанный в спеке шов: порты Clock/RNG/Telegram/Store. Реализации owner
нет - импорт модулей owner/клиента дает ImportError, пока их не написали.

Запуск: python3 tests/test_telegram_session_owner_fakes.py (самопроверка фейков).
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat as statmod
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

NOW = 1_800_000_000.0


def import_owner():
    import telegram_session_owner  # noqa: PLC0415
    return telegram_session_owner


def import_client():
    import telegram_owner_client  # noqa: PLC0415
    return telegram_owner_client


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    """utc/monotonic управляемы; sleep_sync без реального сна продвигает оба."""

    def __init__(self, utc: float = NOW, mono: float = 1000.0):
        self._utc, self._mono = utc, mono
        self.sleeps: list[float] = []
        self.async_sleeps: list[float] = []
        self.on_sleep = None  # callable(n_sleeps_so_far)

    def utc(self) -> float:
        return self._utc

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._utc += seconds
        self._mono += seconds

    def sleep_sync(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))

    async def sleep(self, seconds: float) -> None:
        self.async_sleeps.append(seconds)
        self.advance(seconds)


class FakeRNG:
    def __init__(self, uniform=0.0, randint=None):
        self.uniform_value, self.randint_value = uniform, randint
        self.uniform_calls: list[tuple] = []
        self.randint_calls: list[tuple] = []

    def uniform(self, low, high):
        self.uniform_calls.append((low, high))
        return min(max(self.uniform_value, low), high)

    def randint(self, low, high):
        self.randint_calls.append((low, high))
        v = low if self.randint_value is None else self.randint_value
        return min(max(v, low), high)


class FloodWait(Exception):
    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


def _eid(m: dict) -> int:
    """id элемента history: у service-элемента темы он внутри record."""
    return m["id"] if "id" in m else m.get("record", {}).get("id", 0)


class FakeTelegram:
    """Telegram-порт: записывает вызовы, отдает заданные ответы."""

    def __init__(self, account: str = "a"):
        self.account = account
        self.calls: list[tuple] = []
        self.connected = False
        self.history_data: list[dict] = []
        self.recent_data: list[dict] = []
        self.event_batches: list[list[dict]] = []
        self.scheduled_data: list[dict] = []
        self.send_results: list = []  # значения или исключения
        self.next_id = 500
        self.users: dict = {}
        self.peer_map: dict = {}   # peer-аргумент -> dict {kind,id,username}
        self.hooks: dict = {}      # имя вызова -> callable()
        self.raises: dict = {}     # имя вызова -> исключение (одноразово)

    def _log(self, *a):
        self.calls.append(a)
        h = self.hooks.get(a[0])
        if h:
            h()
        exc = self.raises.pop(a[0], None)
        if exc is not None:
            raise exc

    def names(self):
        return [c[0] for c in self.calls]

    async def connect(self):
        self._log("connect")
        self.connected = True

    async def disconnect(self):
        self._log("disconnect")
        self.connected = False

    async def resolve(self, account, peer):
        self._log("resolve", account, peer)
        if peer in self.peer_map:
            return {"account": account, "username": None, **self.peer_map[peer]}
        kind = "User"
        pid = peer if isinstance(peer, int) else 1
        uname = peer if isinstance(peer, str) and peer != "me" else self.users.get(peer)
        return {"account": account, "kind": kind, "id": pid, "username": uname}

    async def history(self, peer, *, after_id, limit):
        self._log("history", peer, after_id, limit)
        return [m for m in self.history_data if _eid(m) > after_id]

    async def recent(self, peer, *, limit):
        self._log("recent", peer, limit)
        return list(self.recent_data)[-limit:]

    async def events(self):
        self._log("events")
        return self.event_batches.pop(0) if self.event_batches else []

    async def upload(self, path):
        self._log("upload", Path(path))
        return f"upload:{Path(path).name}"

    async def send(self, request, peer, uploads):
        self._log("send", request, peer, list(uploads))
        if self.send_results:
            r = self.send_results.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r
        self.next_id += 1
        return {"message_ids": [self.next_id], "scheduled_ids": [], "at": None}

    async def scheduled(self, peer):
        self._log("scheduled", peer)
        return list(self.scheduled_data)

    def sends(self):
        return [c for c in self.calls if c[0] == "send"]

    def uploads(self):
        return [c for c in self.calls if c[0] == "upload"]


class FakeStore:
    """Store-порт поверх реальной ФС в tmp: настоящие flock/rename, плюс
    инжекция сбоев и подмена inspect (bind-mount: другой realpath, тот же dev/ino).

    fail_on: {(op, path_substring): (when, exc)} when='before'|'after'.
    """

    def __init__(self):
        self.fail_on: dict = {}
        self.inspect_override: dict = {}  # Path -> dict частичных полей
        self.log: list[tuple] = []
        self._held: list = []

    def _maybe_fail(self, op, path, when):
        for (o, sub), (w, exc) in list(self.fail_on.items()):
            if o == op and sub in str(path) and w == when:
                del self.fail_on[(o, sub)]
                raise exc

    class _Lock:
        def __init__(self, fd, path, shared):
            self.fd, self.path, self.shared = fd, path, shared

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
            return False

        close = __exit__

    def lock(self, path, *, shared=False):
        path = Path(path)
        self.log.append(("lock", path, shared))
        self._maybe_fail("lock", path, "before")
        flags = os.O_RDWR | os.O_CREAT
        fd = os.open(path, flags, 0o600)  # без O_TRUNC
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise
        lk = self._Lock(fd, path, shared)
        self._held.append(lk)
        return lk

    def release_all(self):
        for lk in self._held:
            try:
                lk.close()
            except OSError:
                pass
        self._held.clear()

    def inspect(self, path):
        path = Path(path)
        self._maybe_fail("inspect", path, "before")
        st = os.lstat(path)  # FileNotFoundError для отсутствующего
        m = st.st_mode
        kind = ("symlink" if statmod.S_ISLNK(m) else "dir" if statmod.S_ISDIR(m)
                else "file" if statmod.S_ISREG(m) else "other")
        info = {"realpath": str(Path(os.path.realpath(path))), "uid": st.st_uid,
                "mode": statmod.S_IMODE(m), "acl_safe": True, "kind": kind,
                "size": st.st_size, "dev": st.st_dev, "ino": st.st_ino, "mtime": st.st_mtime}
        for p, ov in self.inspect_override.items():
            if Path(p) == path:
                info.update(ov)
        return info

    def list(self, path):
        path = Path(path)
        out = []
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                out.append(Path(root) / n)
        return sorted(out)

    def read(self, path):
        self._maybe_fail("read", path, "before")
        return Path(path).read_bytes()

    def atomic_write(self, path, data):
        path = Path(path)
        self.log.append(("atomic_write", path))
        self._maybe_fail("atomic_write", path, "before")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        self._maybe_fail("atomic_write", path, "after")  # эффект сохранен

    def publish(self, source, dest):
        self.log.append(("publish", Path(source), Path(dest)))
        if os.path.lexists(dest):
            raise FileExistsError(errno.EEXIST, "exists", str(dest))
        os.rename(source, dest)

    def snapshot(self, source, dest, *, max_bytes):
        size = os.stat(source).st_size
        if size > max_bytes:
            raise OSError(errno.EFBIG, "too big")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        st = os.stat(source)
        return {"size": size, "sha256": hashlib.sha256(Path(dest).read_bytes()).hexdigest(),
                "dev": st.st_dev, "ino": st.st_ino}

    def remove(self, path):
        self.log.append(("remove", Path(path)))
        self._maybe_fail("remove", path, "before")
        p = Path(path)
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def req_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def send_request(account="a", text="Проверка", peer="me", rid=None, now=NOW, **over):
    r = {
        "schema_version": 1,
        "request_id": rid or str(uuid.uuid4()),
        "created_at": int(now),
        "expires_at": int(now) + 7 * 86400,
        "operation": "send",
        "account": account,
        "target": {"peer": peer, "expected_username": None, "topic_id": None, "reply_to": None},
        "text": text,
        "files": [],
        "options": {"silent": False, "html": False, "voice": False, "no_pace_check": False},
        "schedule": {"at": None, "timezone": None, "exact_minute": False, "remind": False},
    }
    r.update(over)
    return r


def refresh_request(account="a", destination_id="d1", rid=None, now=NOW):
    return {
        "schema_version": 1, "request_id": rid or str(uuid.uuid4()),
        "created_at": int(now), "expires_at": int(now) + 7 * 86400,
        "operation": "refresh", "account": account,
        "destination_id": destination_id, "checkpoint": True,
    }


class Env:
    """Каталог установки: account -> owner_root/session/auth, реестр, конфиг экземпляра."""

    def __init__(self, base: Path, accounts=("a", "b")):
        self.base = Path(base)
        self.accounts = {}
        for acc in accounts:
            root = self.base / "owners" / acc
            root.mkdir(parents=True, mode=0o700)
            os.chmod(root, 0o700)
            sdir = self.base / "sessions" / acc
            sdir.mkdir(parents=True, mode=0o700)
            os.chmod(sdir, 0o700)
            adir = self.base / "auth" / acc
            adir.mkdir(parents=True, mode=0o700)
            os.chmod(adir, 0o700)
            self.accounts[acc] = {
                "owner_root": str(root),
                "auth_path": str(adir / "auth.json"),
                "session_path": str(sdir / f"{acc}.session"),
            }
            Path(self.accounts[acc]["auth_path"]).write_text("{}")
            os.chmod(self.accounts[acc]["auth_path"], 0o600)
            fd = os.open(self.accounts[acc]["session_path"], os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        self.registry_path = self.base / "installation.json"
        self.write_registry()
        self.projects = self.base / "project"
        self.projects.mkdir()
        self.mirrors = self.base / "mirrors"
        self.mirrors.mkdir()

    def write_registry(self, extra=None):
        reg = {"accounts": {k: dict(v) for k, v in self.accounts.items()}}
        if extra:
            reg["accounts"].update(extra)
        self.registry_path.write_text(json.dumps(reg))
        os.chmod(self.registry_path, 0o600)

    def root(self, acc) -> Path:
        return Path(self.accounts[acc]["owner_root"])

    def config(self, acc="a", destinations=None, **over):
        cfg = {
            "instance": acc,
            "account": acc,
            "owner_root": self.accounts[acc]["owner_root"],
            "auth_path": self.accounts[acc]["auth_path"],
            "session_path": self.accounts[acc]["session_path"],
            "installation_config": str(self.registry_path),
            "destinations": destinations or [],
        }
        cfg.update(over)
        for k in [k for k, v in cfg.items() if v is ...]:
            del cfg[k]  # Ellipsis = ключ отсутствует
        return cfg

    def destination(self, did="d1", peer=1001, dest="chat1", account=None):
        return {"destination_id": did, "project_root": str(self.projects),
                "project_config": {}, "peer": peer, "dest": dest}

    def make_owner(self, acc="a", *, tg=None, clock=None, rng=None, store=None,
                   destinations=None, factory_calls=None, **over):
        mod = import_owner()
        tg = tg or FakeTelegram(acc)
        clock = clock or FakeClock()
        rng = rng or FakeRNG()
        store = store or FakeStore()
        calls = factory_calls if factory_calls is not None else []

        def factory(*a, **kw):
            calls.append((a, kw))
            return tg

        owner = mod.Owner(self.config(acc, destinations, **over), telegram_factory=factory,
                          clock=clock, rng=rng, store=store)
        owner.fake = type("F", (), {"tg": tg, "clock": clock, "rng": rng, "store": store,
                                    "factory_calls": calls})
        return owner


def write_package(root: Path, request: dict, *, where="outbox", nonce=None):
    """Кладет пакет в спул: outbox/<id>/request.json либо tmp/<id>.<nonce>/..."""
    rid = request["request_id"]
    d = root / where / (rid if where == "outbox" else f"{rid}.{nonce or uuid.uuid4()}")
    d.mkdir(parents=True, exist_ok=True)
    (d / "request.json").write_text(json.dumps(request, ensure_ascii=False))
    for p in (root / where, d, d / "request.json"):
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
    return d


def read_status(root: Path, rid: str):
    p = root / "status" / f"{rid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def write_heartbeat(root: Path, *, age=0.0, now=NOW, state="ready", **over):
    hb = {"schema_version": 1, "instance": str(uuid.uuid4()), "pid": 123, "state": state}
    hb.update(over)
    p = root / "heartbeat.json"
    p.write_text(json.dumps(hb))
    os.chmod(p, 0o600)
    os.utime(p, (now - age, now - age))
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        os.chmod(self.base, 0o700)
        self.env = Env(self.base)


class FakesSelfCheck(Base):
    def test_fake_clock_sleep_advances(self):
        c = FakeClock()
        c.sleep_sync(0.1)
        self.assertAlmostEqual(c.monotonic(), 1000.1)

    def test_fake_store_lock_is_exclusive_per_inode(self):
        s1, s2 = FakeStore(), FakeStore()
        p = Path(self.env.accounts["a"]["session_path"])
        s1.lock(p)
        with self.assertRaises(BlockingIOError):
            s2.lock(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
