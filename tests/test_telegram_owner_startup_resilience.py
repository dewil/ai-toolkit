#!/usr/bin/env python3
"""RED: reliable owner startup and the server-side FloodWait deadline (ST-1..ST-14).

Written from the spec only (docs/2026-11-24-spec-telegram-owner-startup-resilience.md,
"Уточнения" У-1..У-6 take priority). Real OS locks on a temporary mirror, a virtual
clock for deadlines, fake network ports; no Telegram, no production credentials.

Contract used (У-1..У-5): a temporary startup failure (busy mirror lock, ENOSPC/EIO by
place and errno, Disconnected, FloodWait) is exit 7 OWNER_UNAVAILABLE; exit 2 stays for
config, safety and a real second owner. Retry goes through the process exit: cleanup ->
interruptible protective pause -> non-zero exit -> systemd RestartSec=15 (not modelled
inside the pause). Pause lower bound: max(t_start + 60, t_error + F).
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_owner_startup_floodwait import DialogClient, RpcFloodWait, adapter  # noqa: E402
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeClock, FakeTelegram, import_owner, mkdirs, run, write_file, write_heartbeat,
)
from test_telegram_session_owner_mirror import ids_of, rec, write_mirror  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FLOOR = 60          # minimal protective uptime from the start of an attempt
RESTART_SEC = 15    # unit: RestartSec
BURST, WINDOW = 5, 300  # unit: StartLimitBurst / StartLimitIntervalSec

HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_SH if sys.argv[2] == "sh" else fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


# --------------------------------------------------------------------------- helpers

class VStop:
    """Stop event on the virtual (monotonic) clock."""

    def __init__(self, clock, set_at=None):
        self.clock, self.set_at = clock, set_at

    def is_set(self):
        return self.set_at is not None and self.clock.monotonic() >= self.set_at

    def set(self):
        self.set_at = self.clock.monotonic()

    async def wait(self):
        if self.set_at is None:
            await asyncio.Future()
        self.clock.advance(max(0.0, self.set_at - self.clock.monotonic()))
        return True


@contextlib.contextmanager
def virtual_time(clock, stop, watchdog=None, *, stall=None, limit=5000.0):
    """Waits on `stop` and asyncio.sleep advance the FakeClock instead of real time.

    After every virtual advance the watchdog is polled (as its thread would).
    stall: the first wait "takes" this many seconds without any wake-up before
    returning (a stuck loop/cleanup); then stop is set.
    """
    t0 = clock.monotonic()
    real_wait_for, real_sleep = asyncio.wait_for, asyncio.sleep
    state = {"stalled": False}

    def tick():
        if clock.monotonic() - t0 > limit:
            raise RuntimeError("pause never ended (virtual %.0fs)" % limit)
        if watchdog is not None:
            watchdog.check()

    def stalled():
        if stall is not None and not state["stalled"]:
            state["stalled"] = True
            clock.advance(stall)
            tick()
            stop.set()
            return True
        return False

    def is_stop_wait(awaitable):
        frame = getattr(awaitable, "cr_frame", None)
        return (frame is not None and frame.f_locals.get("self") is stop
                and awaitable.cr_code.co_name == "wait")

    async def wait_for(awaitable, timeout=None):
        if not is_stop_wait(awaitable):
            return await real_wait_for(awaitable, timeout)
        awaitable.close()
        if stalled():
            return True
        remaining = None if stop.set_at is None else stop.set_at - clock.monotonic()
        if remaining is not None and (timeout is None or remaining <= timeout):
            clock.advance(max(0.0, remaining))
            tick()
            return True
        if timeout is None:
            raise RuntimeError("unbounded wait without stop")
        clock.advance(timeout)
        tick()
        raise asyncio.TimeoutError

    async def sleep(delay, result=None):
        if not stalled():
            clock.advance(delay)
            tick()
        await real_sleep(0)
        return result

    real_clock_sleep = clock.sleep

    async def clock_sleep(seconds):
        await real_clock_sleep(seconds)
        tick()

    with patch.object(asyncio, "wait_for", wait_for), patch.object(asyncio, "sleep", sleep), \
            patch.object(clock, "sleep", clock_sleep):
        yield


@contextlib.contextmanager
def store_fault(mod, method, match, err):
    """Real Store, one operation raises OSError(err) for matching paths."""
    real = getattr(mod.Store, method)
    static = isinstance(inspect.getattr_static(mod.Store, method), staticmethod)

    def fault(path):
        if match(Path(path)):
            raise OSError(err, os.strerror(err))

    if static:
        def wrapper(path, *a, **kw):
            fault(path)
            return real(path, *a, **kw)
    else:
        def wrapper(self, path, *a, **kw):
            fault(path)
            return real(self, path, *a, **kw)
    with patch.object(mod.Store, method, wrapper):
        yield


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@contextlib.contextmanager
def captured_logs():
    logger = logging.getLogger("telegram_session_owner")
    handler, old = Capture(), logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)


def heartbeat_states(mod):
    """Context manager that records every state written to heartbeat.json."""
    real = mod.Store.atomic_write
    states = []

    def wrapper(self, path, data, *a, **kw):
        if Path(path).name == "heartbeat.json":
            try:
                states.append(json.loads(data)["state"])
            except Exception:  # noqa: BLE001 - recording only
                states.append("?")
        return real(self, path, data, *a, **kw)

    return states, patch.object(mod.Store, "atomic_write", wrapper)


class Case(Base):
    def setUp(self):
        super().setUp()
        self.mod = import_owner()
        self.dest = [self.env.destination("d1", 1001, "chat1")]
        self.mirror_dir = self.env.projects / "chat1"
        self.mirror_lock = self.mirror_dir / ".telegram-owner.lock"

    def owner(self, *, real_store=True, **kw):
        if real_store:
            kw["store"] = self.mod.Store()
        kw.setdefault("destinations", self.dest)
        return self.env.make_owner("a", **kw)

    def hold(self, path, mode="ex"):
        mkdirs(Path(path).parent)
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(path), mode],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertEqual(proc.stdout.readline().strip(), "locked")
        return proc

    def release(self, proc):
        proc.kill()
        proc.wait()

    def serve(self, owner, stop, watchdog=None, **kw):
        with virtual_time(owner.fake.clock, stop, watchdog, **kw):
            return run(self.mod._serve(owner, stop, watchdog if watchdog is not None else Mock()))

    def heartbeat_state(self):
        path = self.env.root("a") / "heartbeat.json"
        return json.loads(path.read_bytes())["state"] if path.exists() else None

    def assert_flock_free(self, path):
        if not Path(path).exists():
            return
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def assert_flock_held(self, path):
        fd = os.open(path, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def assert_owner_resources_free(self, owner):
        self.assertEqual(owner._locks, [])
        self.assert_flock_free(self.env.root("a") / "owner.lock")
        self.assert_flock_free(self.env.accounts["a"]["session_path"])

    def assert_next_start_ready(self, **kw):
        again = self.owner(**kw)
        try:
            self.assertEqual(run(again.start())["code"], "READY")
        finally:
            run(again.close())

    def temp_result(self, result):
        self.assertEqual((result["code"], result["exit_code"]), ("OWNER_UNAVAILABLE", 7), result)


# ---------------------------------------------------------------- ST-1 busy mirror

class BusyMirror(Case):
    def test_busy_mirror_is_temporary_not_second_owner(self):  # ST-1, INV-ST-1/2
        for mode in ("sh", "ex"):
            with self.subTest(holder=mode):
                holder = self.hold(self.mirror_lock, mode)
                try:
                    owner = self.owner()
                    self.temp_result(run(owner.start()))
                    self.assertFalse(owner._started)
                    self.assertNotEqual(self.heartbeat_state(), "ready")
                    self.assert_owner_resources_free(owner)
                    self.assert_flock_held(self.mirror_lock)  # foreign lock untouched (ST-12)
                finally:
                    self.release(holder)
                self.assert_next_start_ready()

    def test_busy_mirror_pauses_before_nonzero_exit(self):  # ST-1/ST-5
        holder = self.hold(self.mirror_lock, "ex")
        owner = self.owner()
        clock = owner.fake.clock
        t_start = clock.monotonic()
        self.assertEqual(self.serve(owner, VStop(clock)), 7)
        self.assertGreaterEqual(clock.monotonic(), t_start + FLOOR)
        self.assert_owner_resources_free(owner)
        self.release(holder)
        self.assert_next_start_ready()


# ------------------------------------------------------------- ST-2 ENOSPC / EIO

POINTS = {
    "config": ("read", lambda p: p.name == "installation.json", True),
    "owner_lock": ("lock", lambda p: p.name == "owner.lock", True),
    "session_lock": ("lock", lambda p: p.name == "a.session", True),
    "journal": ("atomic_write", lambda p: p.name == ".telegram-owner-journal.json", False),
    "result": ("atomic_write", lambda p: p.name == "result.json", False),
    "heartbeat": ("atomic_write", lambda p: p.name == "heartbeat.json", False),
}


class LocalErrno(Case):
    def start_with(self, point, err, **kw):
        method, match, _ = POINTS[point]
        owner = self.owner(**kw)
        owner.fake.tg.history_data = [rec(1)]
        with store_fault(self.mod, method, match, err):
            return owner, run(owner.start())

    def test_enospc_and_eio_are_temporary_and_recoverable(self):  # ST-2
        for point, (_, _, before_factory) in POINTS.items():
            for err in (errno.ENOSPC, errno.EIO):
                with self.subTest(point=point, errno=errno.errorcode[err]):
                    owner, result = self.start_with(point, err)
                    self.temp_result(result)
                    if before_factory:
                        self.assertEqual(owner.fake.factory_calls, [])
                    self.assertFalse(owner._started)
                    self.assertNotEqual(self.heartbeat_state(), "ready")
                    self.assert_owner_resources_free(owner)
                    self.assert_next_start_ready()  # fault removed -> ready, no leaked locks

    def test_other_errnos_and_bare_oserror_stay_permanent(self):  # ST-2/ST-3, У-4
        for point in ("config", "journal"):
            for err in (errno.EACCES, errno.EROFS, errno.EDQUOT):
                with self.subTest(point=point, errno=errno.errorcode[err]):
                    owner, result = self.start_with(point, err)
                    self.assertEqual(result["exit_code"], 2, result)
                    self.assert_owner_resources_free(owner)

    def test_main_eio_reading_private_config_is_temporary(self):  # ST-2 (main before Owner)
        cfg = self.base / "owner-config.json"
        write_file(cfg, json.dumps(self.env.config("a", self.dest)))
        program = f"""
import builtins, errno, io, os, sys
sys.path.insert(0, {str(SCRIPTS)!r})
import telegram_session_owner as owner
CFG = {str(cfg)!r}
def hit(p):
    try:
        p = os.fspath(p)
    except TypeError:
        return False
    return p == CFG or os.path.basename(p) == os.path.basename(CFG)
def guard(real):
    def wrapper(p, *a, **kw):
        if hit(p):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real(p, *a, **kw)
    return wrapper
os.open, builtins.open, io.open = guard(os.open), guard(builtins.open), guard(io.open)
print(owner.main(['--instance', 'a', '--config', CFG]))
"""
        proc = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                              timeout=30, env={**os.environ, "HOME": str(self.base)})
        self.assertEqual(proc.stdout.strip().splitlines()[-1:], ["7"], proc.stderr)
        self.assertNotIn("Input/output error", proc.stderr)
        self.assertNotIn(str(self.base), proc.stderr)


# -------------------------------------------------------- ST-3 permanent failures

class PermanentFailures(Case):
    def assert_permanent(self, owner, result):
        self.assertEqual(result["exit_code"], 2, result)
        self.assertNotEqual(result["code"], "OWNER_UNAVAILABLE")
        self.assertEqual(owner.fake.factory_calls, [])
        self.assert_owner_resources_free(owner)

    def test_broken_registry_json(self):  # ST-3
        write_file(self.env.registry_path, "{broken json")
        owner = self.owner()
        self.assert_permanent(owner, run(owner.start()))

    def test_unknown_account_in_registry(self):  # ST-3
        owner = self.owner(account="ghost")
        self.assert_permanent(owner, run(owner.start()))

    def test_unsafe_session_permissions(self):  # ST-3
        os.chmod(self.env.accounts["a"]["session_path"], 0o666)
        owner = self.owner()
        self.assert_permanent(owner, run(owner.start()))

    def test_foreign_mirror_is_not_overwritten_nor_made_temporary(self):  # ST-3
        write_mirror(self.mirror_dir / "result.json", [1, 2], chat_id=999)
        before = (self.mirror_dir / "result.json").read_bytes()
        owner = self.owner()
        owner.fake.tg.history_data = [rec(3)]
        result = run(owner.start())
        try:
            self.assertNotEqual(result["exit_code"], 7, result)
        finally:
            run(owner.close())
        self.assertEqual((self.mirror_dir / "result.json").read_bytes(), before)


# ------------------------------------------------------------ ST-4 second owner

class SecondOwner(Case):
    def test_held_owner_lock_is_second_owner_and_keeps_heartbeat(self):  # ST-4/ST-11
        first = self.owner()
        self.assertEqual(run(first.start())["code"], "READY")
        beat = (self.env.root("a") / "heartbeat.json").read_bytes()
        second = self.owner()
        result = run(second.start())
        self.assertEqual((result["code"], result["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        self.assertEqual(second.fake.factory_calls, [])
        self.assertEqual((self.env.root("a") / "heartbeat.json").read_bytes(), beat)
        run(first.close())
        self.assert_next_start_ready()

    def test_foreign_process_on_session_inode_via_hardlink_each_attempt(self):  # ST-4
        link = self.base / "alias.session"
        os.link(self.env.accounts["a"]["session_path"], link)
        holder = self.hold(link, "ex")
        for _ in range(2):  # every new attempt keeps the same order and verdict
            owner = self.owner()
            result = run(owner.start())
            self.assertEqual((result["code"], result["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
            self.assertEqual(owner.fake.factory_calls, [])
        holder.kill()
        holder.wait()
        self.assertTrue(Path(self.env.accounts["a"]["session_path"]).exists())
        self.assert_next_start_ready()


# ------------------------------------------- ST-5 supervisor model (5 starts / 300 s)

class Supervisor(Case):
    RELEASE_AFTER = 700.0  # longer than one StartLimit window

    def supervise(self, cause):
        clock = FakeClock()
        t0 = clock.monotonic()
        holder = self.hold(self.mirror_lock, "ex") if cause == "busy" else None
        starts, codes, connects = [], [], []
        for _ in range(40):
            now = clock.monotonic()
            active = now - t0 < self.RELEASE_AFTER
            if holder is not None and not active:
                self.release(holder)
                holder = None
            owner = self.owner(clock=clock)
            tg = owner.fake.tg
            tg.hooks["connect"] = lambda: connects.append(clock.monotonic())
            if cause == "disconnected" and active:
                tg.raises["connect"] = self.mod.Disconnected()
            if cause == "floodwait" and active:
                tg.raises["connect"] = self.mod.FloodWait(600)
            stop = VStop(clock, None if active else now + 1)
            starts.append(now)
            fault = (store_fault(self.mod, "read", lambda p: p.name == "installation.json",
                                 errno.ENOSPC) if cause == "enospc" and active
                     else contextlib.nullcontext())
            with fault:
                code = self.serve(owner, stop)
            codes.append(code)
            if code == 0:
                break
            if code != 7:
                break
            clock.advance(RESTART_SEC)
        self.assertEqual(codes[-1], 0, f"never became ready; exit codes {codes}")
        self.assertEqual(set(codes[:-1]), {7} if len(codes) > 1 else set(), codes)
        for s in starts:
            self.assertLessEqual(sum(s <= x < s + WINDOW for x in starts), BURST, starts)
        return starts, connects

    def test_busy_mirror_held_longer_than_window(self):  # ST-5
        self.supervise("busy")

    def test_enospc_held_longer_than_window(self):  # ST-5
        self.supervise("enospc")

    def test_disconnected_held_longer_than_window(self):  # ST-5
        self.supervise("disconnected")

    def test_floodwait_never_connects_before_server_deadline(self):  # ST-5/ST-6
        starts, connects = self.supervise("floodwait")
        failed = starts[:-1]
        self.assertGreaterEqual(len(failed), 2)
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, 600, f"next connect {b - a:.0f}s after a 600s FloodWait")


# --------------------------------------------- ST-6 / ST-8 server deadline timing

class FloodDeadline(Case):
    def flood(self, F, C, K, *, wall_jump=0.0, stop_after=None):
        owner = self.owner(real_store=False)
        clock, tg = owner.fake.clock, owner.fake.tg
        seen = {}

        def on_connect():
            clock.advance(C)
            clock._utc += wall_jump  # wall clock changes must not matter
            seen["t_error"] = clock.monotonic()

        def on_disconnect():
            clock.advance(K)
            seen["t_clean"] = clock.monotonic()

        tg.hooks["connect"], tg.hooks["disconnect"] = on_connect, on_disconnect
        tg.raises["connect"] = self.mod.FloodWait(F)
        t_start = clock.monotonic()
        stop = VStop(clock, None if stop_after is None else t_start + stop_after)
        code = self.serve(owner, stop)
        return owner, code, t_start, seen

    def check_window(self, F, C, K, **kw):
        owner, code, t_start, seen = self.flood(F, C, K, **kw)
        self.assertEqual(code, 7)
        end = owner.fake.clock.monotonic()
        deadline = seen["t_error"] + F
        lower = max(t_start + FLOOR, deadline)
        self.assertGreaterEqual(end, lower, "next attempt before the server deadline/floor")
        upper = max(lower, seen.get("t_clean", seen["t_error"])) + 10
        self.assertLessEqual(end, upper, "FloodWait counted again from the end of cleanup")
        self.assertEqual(owner.fake.tg.names().count("connect"), 1)
        self.assertNotIn("history", owner.fake.tg.names())

    def test_matrix_of_flood_seconds_connect_and_cleanup_times(self):  # ST-6
        for F, C, K in ((25, 0, 0), (120, 0, 0), (121, 0, 0), (600, 0, 0),
                        (600, 40, 15), (600, 230, 15), (25, 0, 100)):
            with self.subTest(F=F, connect=C, cleanup=K):
                self.check_window(F, C, K)

    def test_wall_clock_changes_do_not_move_monotonic_deadline(self):  # ST-8
        for jump in (-3600.0, 3600.0):
            with self.subTest(wall_jump=jump):
                self.check_window(600, 40, 15, wall_jump=jump)

    def test_deadline_survives_failed_cleanup_with_original_category(self):  # ST-12
        owner = self.owner(real_store=False)
        clock, tg = owner.fake.clock, owner.fake.tg
        seen = {}
        tg.hooks["connect"] = lambda: (clock.advance(40), seen.update(t_error=clock.monotonic()))
        tg.hooks["disconnect"] = lambda: clock.advance(15)
        tg.raises["connect"] = self.mod.FloodWait(600)
        tg.raises["disconnect"] = OSError("private-disconnect-diagnostic")
        with captured_logs() as lines:
            code = self.serve(owner, VStop(clock))
        self.assertEqual(code, 7)
        self.assertGreaterEqual(clock.monotonic(), seen["t_error"] + 600)
        self.assertNotIn("private-disconnect-diagnostic", "\n".join(lines))

    def test_partial_dialog_page_flood_wait_keeps_deadline(self):  # ST-6 (real adapter)
        class Timed(DialogClient):
            def __init__(self, clock, seen, *a, **kw):
                super().__init__(*a, **kw)
                self.clock, self.seen = clock, seen

            async def iter_dialogs(self):
                async for dialog in super().iter_dialogs():
                    yield dialog
                    self.clock.advance(40)
                    self.seen["t_error"] = self.clock.monotonic()

        clock, seen = FakeClock(), {}
        client = Timed(clock, seen, None, error=RpcFloodWait(600))
        owner = self.owner(real_store=False, tg=adapter(client), clock=clock)
        owner._catch_up = Mock()
        t_start = clock.monotonic()
        self.assertEqual(self.serve(owner, VStop(clock)), 7)
        self.assertGreaterEqual(clock.monotonic(), max(t_start + FLOOR, seen["t_error"] + 600))
        self.assertTrue(client.disconnected)

    def test_external_timeout_inside_internal_sleep_restores_settings(self):  # ST-7
        class Sleeping(DialogClient):
            async def iter_dialogs(self):
                self.settings.append((self.flood_sleep_threshold, self._request_retries))
                yield type("D", (), {"entity": self.entities[0]})()
                await asyncio.Future()  # Telethon-internal flood sleep

        for retries in (0, 7):
            with self.subTest(retries=retries):
                client = Sleeping(None, request_retries=retries)
                with self.assertRaises((asyncio.TimeoutError, self.mod.Disconnected)):
                    run(asyncio.wait_for(adapter(client).connect(), timeout=0.05))
                self.assertEqual(client.flood_sleep_threshold, 0)
                self.assertEqual(client._request_retries, retries)


# ------------------------------------------------------------------- ST-9 stop

class StopDuringPause(Case):
    def disconnected(self, owner):
        owner.fake.tg.raises["connect"] = self.mod.Disconnected()

    def flooded(self, owner):
        owner.fake.tg.raises["connect"] = self.mod.FloodWait(600)

    def test_stop_interrupts_pause_without_new_rpc(self):  # ST-9
        for name, arm in (("floor", self.disconnected), ("floodwait", self.flooded)):
            with self.subTest(pause=name):
                owner = self.owner()
                arm(owner)
                clock = owner.fake.clock
                set_at = clock.monotonic() + 10
                self.assertEqual(self.serve(owner, VStop(clock, set_at)), 0)
                self.assertLessEqual(clock.monotonic(), set_at + 5, "stop waited out the pause")
                self.assertEqual(owner.fake.tg.names().count("connect"), 1)
                for rpc in ("history", "recent", "events", "send"):
                    self.assertNotIn(rpc, owner.fake.tg.names())
                self.assert_owner_resources_free(owner)
                self.assert_next_start_ready()

    def test_already_set_stop_exits_zero_and_releases(self):  # ST-9
        owner = self.owner()
        self.flooded(owner)
        clock = owner.fake.clock
        self.assertEqual(self.serve(owner, VStop(clock, clock.monotonic())), 0)
        self.assertLessEqual(owner.fake.tg.names().count("connect"), 1)
        self.assertLessEqual(clock.monotonic() - 1000.0, 5)
        self.assert_owner_resources_free(owner)


# ---------------------------------------------------------------- ST-10 watchdog

class WatchdogVsPause(Case):
    def flood_owner(self):
        owner = self.owner(real_store=False)
        owner.fake.tg.raises["connect"] = self.mod.FloodWait(600)
        return owner

    def test_permitted_long_pause_is_not_terminated(self):  # ST-10
        owner = self.flood_owner()
        clock, exits = owner.fake.clock, []
        watchdog = self.mod.HeartbeatWatchdog(clock, terminate=exits.append)
        t_start = clock.monotonic()
        self.assertEqual(self.serve(owner, VStop(clock), watchdog), 7)
        self.assertGreaterEqual(clock.monotonic(), t_start + 600)  # the pause really happened
        self.assertEqual(exits, [])

    def test_hang_inside_pause_still_fires_watchdog(self):  # ST-10 control
        owner = self.flood_owner()
        clock, exits = owner.fake.clock, []
        watchdog = self.mod.HeartbeatWatchdog(clock, terminate=exits.append)
        with self.assertLogs("telegram_session_owner", level="ERROR"):
            self.serve(owner, VStop(clock), watchdog, stall=301)
        self.assertEqual(exits, [1])


# --------------------------------------------------------------- ST-11 heartbeat

class HeartbeatDuringFailure(Case):
    def scenarios(self):
        def disconnected(o):
            o.fake.tg.raises["connect"] = self.mod.Disconnected()

        def flood(o):
            o.fake.tg.raises["connect"] = self.mod.FloodWait(600)

        yield "disconnected", disconnected, contextlib.nullcontext()
        yield "floodwait", flood, contextlib.nullcontext()
        yield "enospc", (lambda o: None), store_fault(
            self.mod, "atomic_write", POINTS["journal"][1], errno.ENOSPC)

    def test_failed_attempt_never_publishes_ready_over_stale_ready(self):  # ST-11
        for name, arm, fault in self.scenarios():
            with self.subTest(failure=name):
                write_heartbeat(self.env.root("a"), age=0.0, now=1_800_000_000.0, state="ready")
                owner = self.owner()
                owner.fake.tg.history_data = [rec(1)]
                arm(owner)
                states, recorder = heartbeat_states(self.mod)
                with recorder, fault:
                    self.assertEqual(self.serve(owner, VStop(owner.fake.clock)), 7)
                self.assertNotIn("ready", states)
                self.assertNotEqual(self.heartbeat_state(), "ready")


# ------------------------------------------------------------------ ST-12 safety

class MirrorSafety(Case):
    def test_failed_commit_keeps_committed_progress(self):  # ST-12
        path = self.mirror_dir / "result.json"
        write_mirror(path, [1, 2])
        owner = self.owner()
        owner.fake.tg.history_data = [rec(3)]
        with store_fault(self.mod, "atomic_write", POINTS["journal"][1], errno.ENOSPC):
            self.temp_result(run(owner.start()))
        self.assertTrue({1, 2} <= set(ids_of(path)))
        self.assert_owner_resources_free(owner)
        again = self.owner()
        again.fake.tg.history_data = [rec(3)]
        try:
            self.assertEqual(run(again.start())["code"], "READY")
            for _ in range(6):
                run(again.step())
        finally:
            run(again.close())
        ids = ids_of(path)
        self.assertTrue({1, 2} <= set(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_restart_after_failed_start_does_not_resend_unknown_send(self):  # ST-12/ST-14
        owner = self.owner()
        self.assertEqual(run(owner.start())["code"], "READY")
        run(owner.close())
        self.assertEqual(owner.fake.tg.sends(), [])


# ------------------------------------------------------------ ST-13 observability

class Observability(Case):
    SECRET = "PRIVATE-DETAIL"

    def run_category(self, name):
        holder = None
        fault = contextlib.nullcontext()
        owner = self.owner()
        if name == "busy_mirror":
            holder = self.hold(self.mirror_lock, "ex")
        elif name == "enospc":
            fault = store_fault(self.mod, "read", POINTS["config"][1], errno.ENOSPC)
        elif name == "disconnected":
            owner.fake.tg.raises["connect"] = self.mod.Disconnected()
        elif name == "floodwait":
            owner.fake.tg.raises["connect"] = self.mod.FloodWait(600)
        elif name == "second_owner":
            holder = self.hold(self.env.root("a") / "owner.lock", "ex")
        with captured_logs() as lines, fault:
            code = self.serve(owner, VStop(owner.fake.clock))
        if holder is not None:
            self.release(holder)
        return code, lines

    def test_categories_are_distinguishable_and_clean(self):  # ST-13
        expected = {"busy_mirror": 7, "enospc": 7, "disconnected": 7, "floodwait": 7,
                    "second_owner": 2}
        seen = {}
        for name, code_expected in expected.items():
            with self.subTest(category=name):
                code, lines = self.run_category(name)
                self.assertEqual(code, code_expected)
                text = "\n".join(lines)
                self.assertNotIn(self.SECRET, text)
                self.assertNotIn(str(self.base), text)
                self.assertLessEqual(len(lines), 6, "log line per pause tick")
                seen[name] = sorted({re.sub(r"\d+", "N", line) for line in lines})
        names = list(seen)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertNotEqual(seen[a], seen[b], f"{a} and {b} indistinguishable in log")

    def test_stop_zero_differs_from_failure_codes(self):  # ST-13
        owner = self.owner()
        clock = owner.fake.clock
        self.assertEqual(self.serve(owner, VStop(clock, clock.monotonic() + 1)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
