"""Владелец: пакет, опубликованный в тот же тик mtime outbox/, не застревает (RED).

Спека правки простоя, п.2: новый пакет в outbox/ берется в отправку на ближайшем
шаге, в том числе когда mtime каталога outbox/ не сдвинулся (ext4, ядро 6.8:
mtime меняется с шагом около тика). Эмуляция: mtime возвращается через os.utime(ns=).
Границы: FakeStore/FakeClock/FakeTelegram, внутренних имен реализации нет.
"""
import os
import unittest

from test_telegram_session_owner_fakes import (
    Base, read_status, run, send_request, write_package,
)

TICK = 0.1
MAX_STEPS = 30  # 3 с модельного времени


def fast_request(text, now):
    req = send_request("a", text=text, now=now)
    req["options"]["no_pace_check"] = True
    return req


class OutboxSameTick(Base):
    def start(self):
        owner = self.env.make_owner("a")
        self.assertEqual(run(owner.start())["code"], "READY")
        self.addCleanup(lambda: run(owner.close()))
        return owner

    def step(self, owner, n=1):
        for _ in range(n):
            owner.fake.clock.advance(TICK)
            run(owner.step())

    def steps_until_status(self, owner, root, rid):
        for i in range(1, MAX_STEPS + 1):
            self.step(owner)
            st = read_status(root, rid)
            if st is not None and st["state"] != "queued":
                return i
        return None

    def publish_same_tick(self, root, req, mtime_ns):
        write_package(root, req)
        os.utime(root / "outbox", ns=(mtime_ns, mtime_ns))
        self.assertEqual(os.stat(root / "outbox").st_mtime_ns, mtime_ns)

    def test_package_published_with_unchanged_outbox_mtime_is_sent(self):
        owner = self.start()
        root = self.env.root("a")
        a = fast_request("A", owner.fake.clock.utc())
        write_package(root, a)
        self.step(owner, 3)
        self.assertEqual(read_status(root, a["request_id"])["state"], "sent")
        before = os.stat(root / "outbox").st_mtime_ns

        b = fast_request("B", owner.fake.clock.utc())
        self.publish_same_tick(root, b, before)
        n = self.steps_until_status(owner, root, b["request_id"])

        self.assertIsNotNone(
            n, f"пакет B без терминального статуса за {MAX_STEPS} шагов "
               f"({MAX_STEPS * TICK} с) при неизменном mtime outbox/")
        self.assertEqual(read_status(root, b["request_id"])["state"], "sent")
        self.assertEqual(len(owner.fake.tg.sends()), 2,
                         f"отправок {len(owner.fake.tg.sends())}, ожидалось 2 (A и B)")

    def test_package_published_after_external_removal_in_same_tick_is_sent(self):
        # уборка (внешняя, через Store) удалила терминальный A, и в тот же тик
        # опубликован B: mtime каталога совпадает с видимым владельцу ранее.
        owner = self.start()
        root = self.env.root("a")
        a = fast_request("A", owner.fake.clock.utc())
        write_package(root, a)
        self.step(owner, 3)
        self.assertEqual(read_status(root, a["request_id"])["state"], "sent")
        before = os.stat(root / "outbox").st_mtime_ns

        owner.fake.store.remove(root / "outbox" / a["request_id"])
        b = fast_request("B", owner.fake.clock.utc())
        self.publish_same_tick(root, b, before)
        n = self.steps_until_status(owner, root, b["request_id"])

        self.assertIsNotNone(
            n, f"пакет B без терминального статуса за {MAX_STEPS} шагов "
               "после удаления A и публикации в тот же тик")
        self.assertEqual(len(owner.fake.tg.sends()), 2,
                         f"отправок {len(owner.fake.tg.sends())}, ожидалось 2")

    def test_control_changed_mtime_is_sent(self):
        # контроль фикстуры: без подмены mtime B берется (должен быть зеленым)
        owner = self.start()
        root = self.env.root("a")
        a = fast_request("A", owner.fake.clock.utc())
        write_package(root, a)
        self.step(owner, 3)
        b = fast_request("B", owner.fake.clock.utc())
        write_package(root, b)
        os.utime(root / "outbox", ns=(os.stat(root / "outbox").st_mtime_ns + 5_000_000_000,) * 2)
        self.assertIsNotNone(self.steps_until_status(owner, root, b["request_id"]))
        self.assertEqual(len(owner.fake.tg.sends()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
