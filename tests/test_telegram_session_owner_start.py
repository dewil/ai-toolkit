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
    Base, FakeClock, FakeStore, FakeTelegram, NOW, import_owner, run, write_heartbeat,
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
        p.write_bytes(b"SQLite format 3\x00data")
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
        self.env.registry_path.write_text(json.dumps(reg))
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_registry_path_mismatch(self):  # F22
        reg = json.loads(self.env.registry_path.read_text())
        reg["accounts"]["a"]["session_path"] = str(self.base / "other.session")
        self.env.registry_path.write_text(json.dumps(reg))
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("INVALID_CONFIG", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_registry_incomplete_entry(self):  # F22
        reg = json.loads(self.env.registry_path.read_text())
        del reg["accounts"]["b"]["owner_root"]
        self.env.registry_path.write_text(json.dumps(reg))
        o = self.env.make_owner("a")
        self.assertEqual(start(o)["exit_code"], 2)
        self.assertEqual(o.fake.factory_calls, [])

    def test_shared_owner_root_different_accounts_forbidden(self):  # F1
        reg = json.loads(self.env.registry_path.read_text())
        reg["accounts"]["b"]["owner_root"] = reg["accounts"]["a"]["owner_root"]
        self.env.registry_path.write_text(json.dumps(reg))
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
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        self.assertEqual(o.fake.factory_calls, [])
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        o2 = self.env.make_owner("a")
        self.assertEqual(start(o2)["code"], "READY")

    def test_session_without_extension_locks_dot_session_file(self):  # F1
        sdir = Path(self.env.accounts["a"]["session_path"]).parent
        self.env.accounts["a"]["session_path"] = str(sdir / "a")  # Telethon добавит .session
        self.env.write_registry()
        real = sdir / "a.session"
        fd = os.open(real, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self._hold(real)
        o = self.env.make_owner("a")
        r = start(o)
        self.assertEqual((r["code"], r["exit_code"]), ("OWNER_ALREADY_RUNNING", 2))
        self.assertEqual(o.fake.factory_calls, [])

    def test_symlink_alias_realpath_locked(self):  # F1
        sess = Path(self.env.accounts["a"]["session_path"])
        link = self.base / "sym.session"
        link.symlink_to(sess)
        self._hold(sess)
        o = self.env.make_owner("a")
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
        o = self.env.make_owner("a")
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


class Independence(Base):
    def test_close_of_a_does_not_affect_b(self):  # F26
        oa, ob = self.env.make_owner("a"), self.env.make_owner("b")
        start(oa), start(ob)
        run(oa.close())
        self.assertTrue(ob.fake.tg.connected)
        self.assertNotIn("disconnect", ob.fake.tg.names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
