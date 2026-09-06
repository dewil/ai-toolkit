"""Сверка правок и удалений в telegram-snapshot (merge_edits).

Инкремент по id не видит отредактированных и удаленных сообщений (HR,
18.08.2026: три прогона "новых нет" при пяти правках). Здесь проверяется
чистая функция сверки зеркала с перечитанным окном - без сети и telethon.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _stub_telethon() -> None:
    try:
        import telethon  # noqa: F401
        return
    except ImportError:
        pass

    def make(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr: type(attr, (), {})  # type: ignore[method-assign]
        return mod

    telethon = make("telethon")
    tl = make("telethon.tl")
    tl_types = make("telethon.tl.types")
    telethon.tl = tl
    tl.types = tl_types
    sys.modules.update({"telethon": telethon, "telethon.tl": tl, "telethon.tl.types": tl_types})


def load_snapshot():
    _stub_telethon()
    spec = importlib.util.spec_from_file_location("tg_snapshot_edits", SCRIPTS / "telegram-snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SNAP = load_snapshot()


def msg(mid: int, text: str, edited: int | None = None, **extra) -> dict:
    rec = {
        "id": mid, "type": "message", "date": f"2026-08-18T10:{mid:02d}:00",
        "date_unixtime": str(1_700_000_000 + mid), "from": "A", "from_id": "user1",
        "text": text, "text_entities": [{"type": "plain", "text": text}] if text else [],
    }
    if edited:
        rec["edited"] = f"2026-08-18T11:{mid:02d}:00"
        rec["edited_unixtime"] = str(edited)
    rec.update(extra)
    return rec


class MergeEdits(unittest.TestCase):
    def test_edit_updates_text_and_keeps_history(self):
        mirror = [msg(1, "привет"), msg(2, "старый текст")]
        fresh = {1: msg(1, "привет"), 2: msg(2, "новый текст", edited=1_700_000_500)}
        edits, deletions = SNAP.merge_edits(mirror, fresh, {1, 2})
        self.assertEqual((edits, deletions), (1, 0))
        m = mirror[1]
        self.assertEqual(m["text"], "новый текст")
        self.assertEqual(m["edited_unixtime"], "1700000500")
        self.assertEqual(m["edit_history"], [{
            "edited": "2026-08-18T10:02:00",  # без прежнего edited - берется дата сообщения
            "text": "старый текст",
            "text_entities": [{"type": "plain", "text": "старый текст"}],
        }])
        self.assertNotIn("edit_history", mirror[0])

    def test_idempotent_second_pass(self):
        mirror = [msg(2, "старый")]
        fresh = {2: msg(2, "новый", edited=1_700_000_500)}
        SNAP.merge_edits(mirror, fresh, {2})
        snapshot = copy.deepcopy(mirror)
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {2}), (0, 0))
        self.assertEqual(mirror, snapshot)

    def test_older_edit_does_not_roll_back(self):
        mirror = [msg(2, "самый новый", edited=1_700_000_900)]
        fresh = {2: msg(2, "постарее", edited=1_700_000_500)}
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {2}), (0, 0))
        self.assertEqual(mirror[0]["text"], "самый новый")

    def test_second_edit_appends_history(self):
        mirror = [msg(2, "v1")]
        SNAP.merge_edits(mirror, {2: msg(2, "v2", edited=100)}, {2})
        SNAP.merge_edits(mirror, {2: msg(2, "v3", edited=200)}, {2})
        self.assertEqual([h["text"] for h in mirror[0]["edit_history"]], ["v1", "v2"])
        self.assertEqual(mirror[0]["edit_history"][1]["edited"], "2026-08-18T11:02:00")
        self.assertEqual(mirror[0]["text"], "v3")

    def test_edit_date_without_text_change_updates_stamp_only(self):
        mirror = [msg(2, "тот же")]
        edits, _ = SNAP.merge_edits(mirror, {2: msg(2, "тот же", edited=100)}, {2})
        self.assertEqual(edits, 1)
        self.assertEqual(mirror[0]["edited_unixtime"], "100")
        self.assertNotIn("edit_history", mirror[0])

    def test_deleted_is_flagged_not_removed(self):
        mirror = [msg(1, "a"), msg(2, "b"), msg(3, "c")]
        fresh = {1: msg(1, "a"), 3: msg(3, "c")}
        edits, deletions = SNAP.merge_edits(mirror, fresh, {1, 3})
        self.assertEqual((edits, deletions), (0, 1))
        self.assertEqual([m["id"] for m in mirror], [1, 2, 3])
        self.assertTrue(mirror[1]["deleted"])
        self.assertEqual(mirror[1]["text"], "b")
        # повтор не считает то же удаление второй раз
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {1, 3}), (0, 0))

    def test_below_window_is_untouched(self):
        mirror = [msg(1, "древнее"), msg(5, "в окне")]
        fresh = {5: msg(5, "в окне")}
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {5, 6, 7}), (0, 0))
        self.assertNotIn("deleted", mirror[0])

    def test_service_ids_count_as_present(self):
        # service-сообщение (id 2) в зеркале не хранится, но в окне присутствует -
        # это не удаление
        mirror = [msg(1, "a"), msg(3, "c")]
        fresh = {1: msg(1, "a"), 3: msg(3, "c")}
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {1, 2, 3}), (0, 0))

    def test_empty_window_is_noop(self):
        mirror = [msg(1, "a")]
        self.assertEqual(SNAP.merge_edits(mirror, {}, set()), (0, 0))
        self.assertNotIn("deleted", mirror[0])

    def test_new_messages_in_window_are_ignored(self):
        # новые (id > last_id) еще не в зеркале - их сверка не касается
        mirror = [msg(1, "a")]
        fresh = {1: msg(1, "a"), 9: msg(9, "новое")}
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {1, 9}), (0, 0))
        self.assertEqual(len(mirror), 1)


if __name__ == "__main__":
    unittest.main()


def load_deltas():
    _stub_telethon()
    spec = importlib.util.spec_from_file_location("tg_deltas_edits", SCRIPTS / "telegram-deltas.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MergeEditsRound2(unittest.TestCase):
    """Регресс на находки состязательного ревью 06.09.2026."""

    def test_media_replacement_updates_metadata_and_history(self):
        mirror = [msg(2, "старая подпись", file_name="old.pdf", file_size=10, mime_type="application/pdf")]
        fresh = {2: msg(2, "новая подпись", edited=100, file_name="new.pdf", file_size=20, mime_type="application/pdf")}
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {2}), (1, 0))
        m = mirror[0]
        self.assertEqual((m["file_name"], m["file_size"]), ("new.pdf", 20))
        self.assertEqual(m["edit_history"][0]["file_name"], "old.pdf")
        self.assertEqual(m["edit_history"][0]["file_size"], 10)
        self.assertNotIn("mime_type", m["edit_history"][0])  # не менялся - в историю не идет

    def test_same_second_edit_with_different_text_is_taken(self):
        mirror = [msg(2, "v1", edited=100)]
        self.assertEqual(SNAP.merge_edits(mirror, {2: msg(2, "v2", edited=100)}, {2}), (1, 0))
        self.assertEqual(mirror[0]["text"], "v2")
        self.assertEqual(mirror[0]["edit_history"][0]["text"], "v1")
        # та же секунда, тот же текст - ничего
        self.assertEqual(SNAP.merge_edits(mirror, {2: msg(2, "v2", edited=100)}, {2}), (0, 0))

    def test_short_chat_deletion_below_window_min(self):
        mirror = [msg(1, "a"), msg(2, "b")]
        fresh = {2: msg(2, "b")}
        # окно не полное (complete): история кончилась, id 1 в ней нет - удалено
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {2}, complete=True), (0, 1))
        self.assertTrue(mirror[0]["deleted"])
        # без complete нижняя граница - min(seen), про id 1 ничего не известно
        mirror2 = [msg(1, "a"), msg(2, "b")]
        self.assertEqual(SNAP.merge_edits(mirror2, fresh, {2}), (0, 0))

    def test_empty_window_stays_noop_even_if_complete(self):
        mirror = [msg(1, "a")]
        self.assertEqual(SNAP.merge_edits(mirror, {}, set(), complete=True), (0, 0))
        self.assertNotIn("deleted", mirror[0])

    def test_new_message_edited_between_requests_when_merged_jointly(self):
        # process_chat сверяет data["messages"] + new_msgs: новое сообщение,
        # правленое между двумя запросами, попадает в зеркало уже правленым
        old = [msg(1, "a")]
        new = [msg(2, "v1")]
        fresh = {1: msg(1, "a"), 2: msg(2, "v2", edited=100)}
        self.assertEqual(SNAP.merge_edits(old + new, fresh, {1, 2}), (1, 0))
        self.assertEqual(new[0]["text"], "v2")

    def test_deltas_skip_deleted(self):
        deltas = load_deltas()
        prev = [msg(1, "a")]
        cur = [msg(1, "a"), msg(2, "b", deleted=True), msg(3, "c")]
        self.assertEqual([m["id"] for m in deltas.filter_new(cur, prev, 24)], [3])
        # ветка без baseline (по времени) - тоже без удаленных
        self.assertEqual([m["id"] for m in deltas.filter_new(cur, [], 10**6)], [1, 3])


class Utf16Entities(unittest.TestCase):
    """Смещения entities Telegram - в единицах UTF-16 (находка ревью 06.09.2026)."""

    def ent(self, cls_name: str, offset: int, length: int):
        cls = getattr(SNAP, cls_name)
        try:
            return cls(offset=offset, length=length)   # настоящий telethon
        except TypeError:
            e = cls()                                  # заглушка без telethon
            e.offset, e.length = offset, length
            return e

    def test_emoji_before_bold(self):
        ents = [self.ent("MessageEntityBold", 2, 1)]  # 😀 = 2 единицы, жирная A
        self.assertEqual(SNAP.entities_to_text_entities("😀AB", ents), [
            {"type": "plain", "text": "😀"}, {"type": "bold", "text": "A"}, {"type": "plain", "text": "B"},
        ])

    def test_bmp_text_unchanged(self):
        ents = [self.ent("MessageEntityBold", 0, 6)]
        self.assertEqual(SNAP.entities_to_text_entities("привет мир", ents), [
            {"type": "bold", "text": "привет"}, {"type": "plain", "text": " мир"},
        ])

    def test_entity_spanning_emoji(self):
        ents = [self.ent("MessageEntityBold", 0, 3)]  # "😀A" = 3 единицы
        self.assertEqual(SNAP.entities_to_text_entities("😀AB", ents), [
            {"type": "bold", "text": "😀A"}, {"type": "plain", "text": "B"},
        ])

    def test_never_edited_content_drift_is_not_an_edit(self):
        # оба без edit_date, содержимое разное (дрейф конвертера против Desktop-экспорта) - не правка
        mirror = [msg(2, "😀AB", text_entities=[{"type": "plain", "text": "😀"}, {"type": "bold", "text": "A"}, {"type": "plain", "text": "B"}])]
        fresh = {2: msg(2, "😀AB", text_entities=[{"type": "plain", "text": "😀A"}, {"type": "bold", "text": "B"}])}
        self.assertEqual(SNAP.merge_edits(mirror, fresh, {2}), (0, 0))
        self.assertEqual(mirror[0]["text_entities"][1], {"type": "bold", "text": "A"})
