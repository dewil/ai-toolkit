"""Простой владельца: чтения status не растут с числом терминальных пакетов (RED).

Границы: Store-порт (read/list/inspect/remove) и FakeClock. Внутренних имен
реализации тест не использует. Инвариант "sending после рестарта -> unknown без
повторной отправки" уже покрыт
test_telegram_session_owner_send.py::test_status_sending_found_after_restart_becomes_unknown_no_resend.
"""
import hashlib
import json
import os
import unittest
import uuid

from test_telegram_session_owner_fakes import (
    NOW, Base, FakeStore, import_owner, read_status, run, send_request,
    write_file, write_package,
)

TERMINAL = (
    ("sent", "SENT", 0),
    ("scheduled", "SCHEDULED", 0),
    ("refreshed", "REFRESHED", 0),
    ("rejected", "INVALID_REQUEST", 2),
    ("unknown", "SEND_UNCERTAIN", 4),
)
FAR = 7 * 86400


class CountingStore(FakeStore):
    """Считает обращения Store к status/ с привязкой к номеру шага."""

    def __init__(self):
        super().__init__()
        self.step_no = 0
        self.status_reads = 0
        self.status_touch_steps = set()

    def _note(self, path, read=False):
        if "/status/" in f"{path}/" or str(path).endswith("/status"):
            self.status_touch_steps.add(self.step_no)
            if read:
                self.status_reads += 1

    def read(self, path):
        self._note(path, read=True)
        return super().read(path)

    def list(self, path):
        self._note(path)
        return super().list(path)

    def inspect(self, path):
        self._note(path)
        return super().inspect(path)

    def remove(self, path):
        self._note(path)
        return super().remove(path)


class IdleIO(Base):
    def make(self, terminal=0, retain=FAR):
        store = CountingStore()
        owner = self.env.make_owner("a", store=store)
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        root = self.env.root("a")
        ids = []
        for i in range(terminal):
            state, code, exit_code = TERMINAL[i % len(TERMINAL)]
            req = send_request("a")
            if state == "refreshed":
                req["operation"] = "refresh"
            write_package(root, req)
            rid = req["request_id"]
            ids.append(rid)
            FakeStore.atomic_write(store, root / "status" / f"{rid}.json", json.dumps({
                "schema_version": 1, "request_id": rid, "operation": req["operation"],
                "state": state, "code": code, "exit_code": exit_code, "retry_after": None,
                "result": {},
                "request_hash": hashlib.sha256(json.dumps(
                    req, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                "accepted_at": NOW, "updated_at": NOW,
                "retain_until": None if state == "unknown" else NOW + retain}).encode())
        return owner, store, ids

    def steps(self, owner, store, count, tick=0.1):
        for _ in range(count):
            store.step_no += 1
            owner.fake.clock.advance(tick)
            run(owner.step())

    def idle_reads(self, n):
        owner, store, _ = self.make(terminal=n)
        self.steps(owner, store, 1)  # прогрев: разовое знакомство с пакетами допустимо
        store.status_reads = 0
        self.steps(owner, store, 20)
        return store.status_reads

    def test_idle_status_reads_do_not_grow_with_terminal_packages(self):
        small = self.idle_reads(5)
        # второй независимый каталог установки для N=100
        from tempfile import TemporaryDirectory
        from test_telegram_session_owner_fakes import Env
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.chmod(tmp.name, 0o700)
        self.env = Env(tmp.name)
        large = self.idle_reads(100)
        self.assertLessEqual(large, 10, f"N=100: {large} чтений status за 20 шагов (N=5: {small})")
        self.assertLessEqual(large, small + 2,
                             f"чтения растут с N: N=5 -> {small}, N=100 -> {large}")

    def test_new_package_after_idle_steps_is_sent_on_next_step(self):
        owner, store, _ = self.make(terminal=90)  # квота спула 100 пакетов
        self.steps(owner, store, 20)
        req = send_request("a", text="fresh", now=owner.fake.clock.utc())
        req["options"]["no_pace_check"] = True
        write_package(self.env.root("a"), req)
        self.steps(owner, store, 1)
        self.assertEqual(len(owner.fake.tg.sends()), 1)
        self.assertEqual(read_status(self.env.root("a"), req["request_id"])["state"], "sent")

    def test_housekeeping_is_not_run_on_every_step(self):
        owner, store, _ = self.make(terminal=5)
        self.steps(owner, store, 1)
        store.status_touch_steps.clear()
        self.steps(owner, store, 300)  # 30 с модельного времени
        touched = len(store.status_touch_steps)
        self.assertLessEqual(touched, 6, f"status/ затронут на {touched} из 300 шагов")

    def test_expired_status_is_still_removed_within_30s(self):
        owner, store, ids = self.make(terminal=5, retain=10)
        root = self.env.root("a")
        self.steps(owner, store, 50)  # +5 с: срок еще не вышел
        for rid in ids[:4]:  # unknown (5-й) не удаляется по сроку
            self.assertIsNotNone(read_status(root, rid))
        self.steps(owner, store, 400)  # до +45 с: срок +10 с и 30 с на уборку
        for rid in ids[:4]:
            self.assertIsNone(read_status(root, rid), "просроченный status не удален за ~30 с")
            self.assertFalse((root / "outbox" / rid).exists())
        self.assertIsNotNone(read_status(root, ids[4]))

    def test_old_tmp_is_still_removed_within_30s(self):
        owner, store, _ = self.make(terminal=0)
        root = self.env.root("a")
        old = write_package(root, send_request("a"), where="tmp")
        lock = old / ".writer.lock"
        write_file(lock, b"")
        for p in (lock, old / "request.json", old):
            os.utime(p, (NOW - 7200, NOW - 7200))
        self.steps(owner, store, 400)  # 40 с
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
