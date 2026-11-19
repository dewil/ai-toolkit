#!/usr/bin/env python3
"""RED: Owner.start - конфиг, блокировки, экземпляры (F1, F22 права, F25, F26 независимость).

Запуск: python3 tests/test_telegram_session_owner_start.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_session_owner_fakes import (  # noqa: E402
    Base, FakeClock, FakeStore, FakeTelegram, NOW, import_owner, mkdirs, run, write_file, write_heartbeat,
)


def start(owner):
    return run(owner.start())


HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(60)
"""


class StartReady(Base):
    def test_import_has_no_side_effects_and_main_exists(self):  # F2/F25
        mod = import_owner()
        self.assertTrue(callable(mod.main))
        for n in ("Owner", "FloodWait", "Disconnected", "SendUncertain"):
            self.assertTrue(hasattr(mod, n), n)

    def test_ready_single_factory_connect(self):  # F25
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("READY", 0))
        self.assertEqual(len(o.fake.factory_calls), 1)
        self.assertIn("connect", o.fake.tg.names())
        run(o.close())
        self.assertIn("disconnect", o.fake.tg.names())

    def test_start_writes_heartbeat_four_fields_0600(self):  # F3
        o = self.env.make_owner("a")
        start(o)
        hb = self.env.root("a") / "heartbeat.json"
        data = json.loads(hb.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"schema_version", "instance", "pid", "state"})
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["state"], "ready")
        self.assertEqual(uuid.UUID(data["instance"]).version, 4)
        self.assertIsInstance(data["pid"], int)
        self.assertGreater(data["pid"], 0)
        self.assertEqual(oct(hb.stat().st_mode & 0o777), oct(0o600))

    def test_two_accounts_both_ready_isolated(self):  # F25
        oa, ob = self.env.make_owner("a"), self.env.make_owner("b")
        self.assertEqual(start(oa)["code"], "READY")
        self.assertEqual(start(ob)["code"], "READY")
        self.assertIsNot(oa.fake.tg, ob.fake.tg)
        self.assertTrue((self.env.root("a") / "heartbeat.json").exists())
        self.assertTrue((self.env.root("b") / "heartbeat.json").exists())

    def test_missing_session_created_empty_0600_without_truncate(self):  # F1
        p = Path(self.env.accounts["a"]["session_path"])
        p.unlink()
        o = self.env.make_owner("a")
        self.assertEqual(start(o)["code"], "READY")
        self.assertEqual(p.stat().st_size, 0)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_existing_session_not_truncated(self):  # F1
        p = Path(self.env.accounts["a"]["session_path"])
        write_file(p, b"SQLite format 3\x00data")
        o = self.env.make_owner("a")
        start(o)
        self.assertEqual(p.read_bytes(), b"SQLite format 3\x00data")


class InstanceName(Base):
    def _code(self, **over):
        o = self.env.make_owner("a", **over)
        r = start(o)
        return r, o

    def test_missing_instance_invalid_config_before_factory(self):  # F25
        o = self.env.make_owner("a", instance=...)
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_instance_differs_from_account(self):  # F25
        r, o = self._code(instance="b")
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_bad_names_rejected_before_factory(self):  # F25
        for bad in ("", "A", "a.b", "a/b", "x" * 33, "a b", "ё"):
            with self.subTest(name=bad):
                o = self.env.make_owner("a", instance=bad, account=bad)
                r = start(o)
                self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
                self.assertEqual(o.fake.factory_calls, [])

    def test_boundary_names_allowed(self):  # F25
        from test_telegram_session_owner_fakes import Env
        import tempfile
        for name in ("a", "x" * 32, "a_b-9"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as t:
                os.chmod(t, 0o700)
                env = Env(Path(t), accounts=(name,))
                r = start(env.make_owner(name))
                self.assertEqual(r["code"], "READY")


class Registry(Base):
    def test_registry_missing_invalid_config(self):  # F22/F25
        self.env.registry_path.unlink()
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_registry_without_own_entry(self):  # F22
        reg = json.loads(self.env.registry_path.read_text())
        reg["accounts"].pop("a")
        write_file(self.env.registry_path, json.dumps(reg))
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_registry_path_mismatch(self):  # F22
        reg = json.loads(self.env.registry_path.read_text())
        reg["accounts"]["a"]["session_path"] = str(self.base / "other.session")
        write_file(self.env.registry_path, json.dumps(reg))
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_registry_incomplete_entry(self):  # F22
        reg = json.loads(self.env.registry_path.read_text())
        del reg["accounts"]["b"]["owner_root"]
        write_file(self.env.registry_path, json.dumps(reg))
        o = self.env.make_owner("a")
        self.assertEqual(start(o)["exit_code"], 2)
        self.assertEqual(o.fake.factory_calls, [])

    def test_shared_owner_root_different_accounts_forbidden(self):  # F1
        reg = json.loads(self.env.registry_path.read_text())
        reg["accounts"]["b"]["owner_root"] = reg["accounts"]["a"]["owner_root"]
        write_file(self.env.registry_path, json.dumps(reg))
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual(r["exit_code"], 2)
        self.assertEqual(o.fake.factory_calls, [])

    def test_shared_root_two_owners_second_refused(self):  # F1
        first = self.env.make_owner("a")
        self.assertEqual(start(first)["code"], "READY")
        second = self.env.make_owner("a", store=FakeStore())
        r = start(second)
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        self.assertEqual(second.fake.factory_calls, [])


class Locks(Base):
    """F1 с реальными процессами/ФС. Базовый класс гоняет FakeStore (реальные flock),
    LocksRealStore - публичный адаптер telegram_session_owner.Store() (уточнение 13)."""

    def _store(self):
        return FakeStore()

    def _hold(self, path) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)],
                             stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def test_hardlinked_session_second_owner_refused_sigkill_releases(self):  # F1 реальные процессы/ФС
        sess = Path(self.env.accounts["a"]["session_path"])
        link = self.base / "alias.session"
        os.link(sess, link)
        holder = self._hold(link)  # держит lock inode через другой путь
        o = self.env.make_owner("a", store=self._store())
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        self.assertEqual(o.fake.factory_calls, [])
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        o2 = self.env.make_owner("a", store=self._store())
        self.assertEqual(start(o2)["code"], "READY")

    def test_session_without_extension_locks_dot_session_file(self):  # F1
        sdir = Path(self.env.accounts["a"]["session_path"]).parent
        self.env.accounts["a"]["session_path"] = str(sdir / "a")  # Telethon добавит .session
        self.env.write_registry()
        real = sdir / "a.session"
        fd = os.open(real, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self._hold(real)
        o = self.env.make_owner("a", store=self._store())
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_symlink_alias_realpath_locked(self):  # F1
        sess = Path(self.env.accounts["a"]["session_path"])
        link = self.base / "sym.session"
        link.symlink_to(sess)
        self._hold(sess)
        o = self.env.make_owner("a", store=self._store())
        self.assertEqual(start(o)["code"], "OWNER_ALREADY_RUNNING")

    def test_bindmount_same_devino_different_realpath_two_accounts(self):  # F1 Fake Store
        # у b другой session_path/root/account, но inode тот же (bind-mount/hardlink)
        sa = Path(self.env.accounts["a"]["session_path"])
        sb = Path(self.env.accounts["b"]["session_path"])
        first = self.env.make_owner("a")
        self.assertEqual(start(first)["code"], "READY")
        st = os.stat(sa)
        store = FakeStore()
        store.inspect_override[sb] = {"dev": st.st_dev, "ino": st.st_ino,
                                      "realpath": str(self.base / "elsewhere" / "b.session")}
        os.unlink(sb)
        os.link(sa, sb)  # реальный общий inode, чтобы flock был общим
        second = self.env.make_owner("b", store=store)
        r = start(second)
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        self.assertEqual(second.fake.factory_calls, [])

    def test_owner_lock_held_by_other_process_on_root(self):  # F1 общий root
        holder = self._hold(self.env.root("a") / "owner.lock")
        o = self.env.make_owner("a", store=self._store())
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        holder.kill()

    def test_swap_detected_unsafe_path(self):  # F1 подмена dev/ino между inspect до/после lock
        sess = Path(self.env.accounts["a"]["session_path"])
        store = FakeStore()
        real_inspect = store.inspect
        seen = {"n": 0}

        def swapping(path):
            info = real_inspect(path)
            if Path(path) == sess:
                seen["n"] += 1
                if seen["n"] >= 2:
                    info = dict(info, ino=info["ino"] + 1)
            return info

        store.inspect = swapping
        o = self.env.make_owner("a", store=store)
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("UNSAFE_PATH", 2))
        self.assertEqual(o.fake.factory_calls, [])


class LocksRealStore(Locks):
    """Те же процессные проверки через публичный реальный Store() (уточнение 13)."""

    def _store(self):
        return import_owner().Store()

    # проверки с подменой inspect относятся к Fake Store, повторять их тут не нужно
    test_bindmount_same_devino_different_realpath_two_accounts = None
    test_swap_detected_unsafe_path = None

    def test_real_store_second_owner_in_process_refused_then_free_after_close(self):  # F1
        o1 = self.env.make_owner("a", store=self._store())
        self.assertEqual(start(o1)["code"], "READY")
        o2 = self.env.make_owner("a", store=self._store())
        r = start(o2)
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        self.assertEqual(o2.fake.factory_calls, [])
        run(o1.close())
        self.assertEqual(start(self.env.make_owner("a", store=self._store()))["code"], "READY")

    def test_real_store_sigkilled_process_holding_store_lock_releases(self):  # F1
        code = ("import sys, time, pathlib; sys.path.insert(0, sys.argv[1])\n"
                "import telegram_session_owner as m\n"
                "s = m.Store(); lk = s.lock(pathlib.Path(sys.argv[2]))\n"
                "print('locked', flush=True); time.sleep(60)\n")
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        sess = self.env.accounts["a"]["session_path"]
        p = subprocess.Popen([sys.executable, "-c", code, str(scripts), sess],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertEqual(p.stdout.readline().strip(), "locked")
        r = start(self.env.make_owner("a", store=self._store()))
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        p.send_signal(signal.SIGKILL)
        p.wait()
        self.assertEqual(start(self.env.make_owner("a", store=self._store()))["code"], "READY")


class StoreAdapter(Base):
    """Публичный Store(): конструктор без аргументов, методы порта на реальной ФС (F1/F6, уточнение 13)."""

    def setUp(self):
        super().setUp()
        self.store = import_owner().Store()

    def test_constructor_no_args_and_port_methods(self):
        for n in ("lock", "inspect", "list", "read", "atomic_write", "publish", "snapshot", "remove"):
            self.assertTrue(callable(getattr(self.store, n)), n)

    def test_atomic_write_0700_parents_0600_file_replaces(self):
        p = self.base / "new" / "sub" / "f.json"
        self.store.atomic_write(p, b"one")
        self.store.atomic_write(p, b"two")
        self.assertEqual(p.read_bytes(), b"two")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(p.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual([x.name for x in p.parent.iterdir()], ["f.json"])  # без остатков tmp

    def test_publish_renames_without_overwrite(self):
        src, dst = self.base / "src", self.base / "dst"
        mkdirs(src)
        write_file(src / "x", "1")
        mkdirs(dst)
        write_file(dst / "y", "keep")
        with self.assertRaises(OSError):
            self.store.publish(src, dst)
        self.assertEqual((dst / "y").read_text(), "keep")
        self.assertTrue(src.exists())
        free = self.base / "free"
        self.store.publish(src, free)
        self.assertEqual((free / "x").read_text(), "1")
        self.assertFalse(src.exists())

    def test_inspect_fields_missing_and_kinds(self):
        f = self.base / "f"
        write_file(f, "abc")
        info = self.store.inspect(f)
        self.assertEqual((info["kind"], info["size"], info["mode"], info["uid"]),
                         ("file", 3, 0o600, os.getuid()))
        for k in ("realpath", "acl_safe", "dev", "ino", "mtime"):
            self.assertIn(k, info)
        link = self.base / "l"
        link.symlink_to(f)
        self.assertEqual(self.store.inspect(link)["kind"], "symlink")
        self.assertEqual(self.store.inspect(self.base)["kind"], "dir")
        with self.assertRaises(FileNotFoundError):
            self.store.inspect(self.base / "nope")

    def test_lock_busy_is_blockingioerror_shared_ok_no_truncate(self):
        p = self.base / "x.lock"
        write_file(p, b"data")
        a = self.store.lock(p)
        with self.assertRaises(BlockingIOError):
            import_owner().Store().lock(p)
        self.assertEqual(p.read_bytes(), b"data")
        a.__exit__(None, None, None)
        s1 = import_owner().Store().lock(p, shared=True)
        s2 = import_owner().Store().lock(p, shared=True)
        s1.__exit__(None, None, None)
        s2.__exit__(None, None, None)

    def test_snapshot_bounded_and_reports_devino_sha(self):
        import hashlib
        src = self.base / "src.bin"
        write_file(src, b"x" * 10)
        info = self.store.snapshot(src, self.base / "copy.bin", max_bytes=10)
        st = os.stat(src)
        self.assertEqual((info["size"], info["dev"], info["ino"]), (10, st.st_dev, st.st_ino))
        self.assertEqual(info["sha256"], hashlib.sha256(b"x" * 10).hexdigest())
        with self.assertRaises(OSError):
            self.store.snapshot(src, self.base / "copy2.bin", max_bytes=9)

    def test_remove_and_list_recursive(self):
        d = self.base / "d"
        mkdirs(d / "e")
        write_file(d / "e" / "f", "1")
        self.assertEqual({p.name for p in self.store.list(d)}, {"e", "f"})
        self.store.remove(d)
        self.assertFalse(d.exists())


class Safety(Base):
    def test_unsafe_modes_refused_before_factory(self):  # F22 0700/0600
        cases = {
            "root_0755": lambda: os.chmod(self.env.root("a"), 0o755),
            "session_0644": lambda: os.chmod(self.env.accounts["a"]["session_path"], 0o644),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                mutate()
                o = self.env.make_owner("a")
                r = start(o)
                self.assertEqual(r["exit_code"], 2)
                self.assertEqual(o.fake.factory_calls, [])
                os.chmod(self.env.root("a"), 0o700)
                os.chmod(self.env.accounts["a"]["session_path"], 0o600)

    def test_foreign_uid_and_acl_refused(self):  # F22
        for ov in ({"uid": os.getuid() + 1}, {"acl_safe": False}):
            with self.subTest(ov=ov):
                store = FakeStore()
                store.inspect_override[Path(self.env.accounts["a"]["session_path"])] = ov
                o = self.env.make_owner("a", store=store)
                r = start(o)
                self.assertEqual(r["exit_code"], 2)
                self.assertEqual(o.fake.factory_calls, [])


def hb_state(root: Path):
    return json.loads((root / "heartbeat.json").read_text(encoding="utf-8"))["state"]


def hb_writes(store) -> int:
    return len([e for e in store.log if e[0] == "atomic_write" and "heartbeat" in str(e[1])])


class Heartbeat(Base):
    """Спека "Готовность": первый ready в конце start; step каждые 5 с по monotonic и при смене
    состояния; degraded при сбое; close пишет stopping до disconnect (уточнение 7)."""

    def started(self, **kw):
        store = FakeStore()
        o = self.env.make_owner("a", store=store, **kw)
        self.assertEqual(start(o)["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        return o, store

    def test_first_ready_after_connect_and_catchup_before_ready_reply(self):  # F3
        seen = {}
        tg = FakeTelegram("a")
        tg.history_data = [{"id": 1, "type": "message", "date": "2026-11-19T10:00:01",
                            "date_unixtime": "1797674401", "text": "m"}]
        hb = self.env.root("a") / "heartbeat.json"
        tg.hooks["history"] = lambda: seen.setdefault("hb_at_catchup", hb.exists())
        o = self.env.make_owner("a", tg=tg, destinations=[self.env.destination("d1", 1001, "chat1")])
        self.assertEqual(start(o)["code"], "READY")
        self.addCleanup(lambda: run(o.close()))
        self.assertIs(seen["hb_at_catchup"], False)  # ready еще не объявлен
        self.assertEqual(hb_state(self.env.root("a")), "ready")

    def test_step_rewrites_heartbeat_every_5s_monotonic(self):  # F3
        o, store = self.started()
        n0 = hb_writes(store)
        clock = o.fake.clock
        clock.advance(4.9)
        run(o.step())
        self.assertEqual(hb_writes(store), n0)
        clock.advance(0.1)
        run(o.step())
        self.assertEqual(hb_writes(store), n0 + 1)
        clock.advance(5)
        run(o.step())
        self.assertEqual(hb_writes(store), n0 + 2)
        self.assertEqual(clock.async_sleeps, [])  # step не спит
        hb = self.env.root("a") / "heartbeat.json"
        self.assertEqual(oct(hb.stat().st_mode & 0o777), oct(0o600))

    def test_state_change_updates_immediately_and_recovers(self):  # F3/F21
        o, store = self.started()
        o.fake.tg.raises["events"] = import_owner().Disconnected()
        run(o.step())  # без ожидания 5 с
        self.assertEqual(hb_state(self.env.root("a")), "degraded")
        o.fake.clock.advance(15)  # срок backoff
        for _ in range(3):
            run(o.step())
        self.assertEqual(hb_state(self.env.root("a")), "ready")

    def test_overflow_degraded_then_ready_after_reconcile(self):  # F3/F21
        o, store = self.started()
        o.fake.tg.event_batches = [[{"kind": "overflow", "peer": None, "record": None, "ids": [],
                                      "revision": [NOW, 1]}]]
        run(o.step())
        self.assertIn(hb_state(self.env.root("a")), ("degraded", "ready"))
        for _ in range(3):
            run(o.step())
        self.assertEqual(hb_state(self.env.root("a")), "ready")

    def test_close_writes_stopping_before_disconnect_under_own_lock(self):  # F3
        o, store = self.started()
        seen = {}
        o.fake.tg.hooks["disconnect"] = lambda: seen.setdefault("state", hb_state(self.env.root("a")))
        run(o.close())
        self.assertEqual(seen["state"], "stopping")
        self.assertEqual(hb_state(self.env.root("a")), "stopping")
        self.assertEqual(start(self.env.make_owner("a", store=FakeStore()))["code"], "READY")  # lock снят

    def test_close_heartbeat_write_failure_still_releases_locks(self):  # F3
        o, store = self.started()
        store.fail_on[("atomic_write", "heartbeat")] = ("before", OSError("disk"))
        try:
            run(o.close())
        except OSError:
            pass
        self.assertIn("disconnect", o.fake.tg.names())
        self.assertEqual(start(self.env.make_owner("a", store=FakeStore()))["code"], "READY")

    def test_refused_second_owner_does_not_touch_heartbeat_of_first(self):  # F1/F3
        first, _ = self.started()
        hb = self.env.root("a") / "heartbeat.json"
        before = (hb.read_bytes(), hb.stat().st_mtime_ns)
        second = self.env.make_owner("a", store=FakeStore())
        self.assertEqual(start(second)["code"], "OWNER_ALREADY_RUNNING")
        run(second.close())
        self.assertEqual((hb.read_bytes(), hb.stat().st_mtime_ns), before)

    def test_failed_start_writes_no_heartbeat(self):  # F3
        o = self.env.make_owner("a", instance="b")
        self.assertEqual(start(o)["exit_code"], 2)
        run(o.close())
        self.assertFalse((self.env.root("a") / "heartbeat.json").exists())


class Independence(Base):
    def test_close_of_a_does_not_affect_b(self):  # F26
        oa, ob = self.env.make_owner("a"), self.env.make_owner("b")
        start(oa), start(ob)
        run(oa.close())
        self.assertTrue(ob.fake.tg.connected)
        self.assertNotIn("disconnect", ob.fake.tg.names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
