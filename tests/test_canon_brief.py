#!/usr/bin/env python3
"""Тесты canon-brief.py - оформление upstream-кандидата в канон.
stdlib-only (unittest), файловая система - во временном каталоге.

Запуск: python3 tests/test_canon_brief.py

Ключевые инварианты: очередь пишется ПЕРВОЙ (при сбое теряется проектная
копия, а не находка); половина доставки не выглядит целой; тот же кандидат под
новой датой заменяет прежний файл, а не ложится вторым; check видит бриф,
написанный руками мимо скрипта.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(inbox: Path):
    """Модуль перезагружается на каждый тест: путь очереди читается из
    окружения на импорте, и общий модуль тащил бы очередь предыдущего теста."""
    os.environ["CANON_INBOX"] = str(inbox)
    spec = importlib.util.spec_from_file_location("canon_brief", SCRIPTS / "canon-brief.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        self.project = root / "2026-09 ALP"
        (self.project / "toolkit-log" / "upstream-pending").mkdir(parents=True)
        self.cb = load(self.inbox)
        self.addCleanup(self.tmp.cleanup)

    def deliver(self, name="find-x", body="# Находка\n", stamp="2026-09-01",
                slug=None, **kw):
        args = self.ns(cmd="deliver", name=name, source="-", date=stamp,
                       dry_run=False, project_root=str(self.project), slug=slug, **kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.patch_stdin(body):
                rc = self.cb.cmd_deliver(args)
        return rc, out.getvalue()

    def check(self, redeliver=False, restore_local=False):
        args = self.ns(cmd="check", redeliver=redeliver, restore_local=restore_local,
                       project_root=str(self.project), slug=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = self.cb.cmd_check(args)
        return rc, out.getvalue()

    @staticmethod
    def ns(**kw):
        import argparse
        return argparse.Namespace(**kw)

    @contextlib.contextmanager
    def patch_stdin(self, text):
        """У настоящего stdin есть .buffer - скрипт читает байты, чтобы
        не-UTF8 вход давал контролируемый отказ, а не traceback."""
        import sys, types
        data = text.encode("utf-8") if isinstance(text, str) else text
        orig = sys.stdin
        sys.stdin = types.SimpleNamespace(buffer=io.BytesIO(data),
                                          read=lambda: data.decode("utf-8"))
        try:
            yield
        finally:
            sys.stdin = orig

    def queue(self):
        return sorted(p.name for p in self.inbox.glob("*.md")) if self.inbox.is_dir() else []

    def pending(self):
        d = self.project / "toolkit-log" / "upstream-pending"
        return sorted(p.name for p in d.glob("*.md"))


class Delivery(Base):
    def test_both_copies_written(self):
        rc, _ = self.deliver()
        self.assertEqual(rc, 0)
        self.assertEqual(self.queue(), ["2026-09-01-2026-09-alp-find-x.md"])
        self.assertEqual(self.pending(), ["find-x.md"])

    def test_queue_written_first(self):
        """Порядок - не косметика: он определяет, что теряется при сбое.
        Проектная папка тут заменена файлом, поэтому запись в нее падает."""
        pend = self.project / "toolkit-log" / "upstream-pending"
        for p in pend.glob("*"):
            p.unlink()
        pend.rmdir()
        pend.write_text("занято", encoding="utf-8")
        rc, _ = self.deliver()
        self.assertEqual(rc, 3)
        self.assertEqual(self.queue(), ["2026-09-01-2026-09-alp-find-x.md"])

    def test_unwritable_queue_does_not_write_project_copy(self):
        """Иначе снаружи выглядело бы сделанным: проектный файл на месте,
        находка не доехала - ровно тот молчаливый провал, ради которого
        скрипт и написан."""
        self.inbox.write_text("не папка", encoding="utf-8")
        with self.assertRaises(SystemExit) as e:
            self.deliver()
        self.assertEqual(e.exception.code, 2)
        self.assertEqual(self.pending(), [])

    def test_same_candidate_new_date_replaces(self):
        """Дата в имени меняется, кандидат - нет: без замены разбор спросил бы
        про одну находку дважды."""
        self.deliver(stamp="2026-09-01")
        self.deliver(stamp="2026-09-05", body="# Находка v2\n")
        self.assertEqual(self.queue(), ["2026-09-05-2026-09-alp-find-x.md"])
        self.assertIn("v2", (self.inbox / "2026-09-05-2026-09-alp-find-x.md").read_text())

    def test_same_date_overwrites(self):
        self.deliver(stamp="2026-09-01")
        self.deliver(stamp="2026-09-01", body="# Свежее\n")
        self.assertEqual(len(self.queue()), 1)
        self.assertIn("Свежее", (self.inbox / self.queue()[0]).read_text())

    def test_other_candidates_not_touched(self):
        self.deliver(name="find-x")
        self.deliver(name="find-y")
        self.assertEqual(len(self.queue()), 2)

    def test_terminal_copy_is_not_deleted_but_warned(self):
        """Разобранный кандидат живет в applied/ и не удаляется. Повторное
        предложение той же находки при этом РАЗРЕШЕНО (человек может вернуться
        к отклоненному) - но громко: без предупреждения агент не узнает, что
        спорит с уже принятым решением. Автоматическая ветка ведет себя иначе,
        см. Backstop.test_terminal_counts_as_delivered."""
        (self.inbox / "applied").mkdir(parents=True)
        old = self.inbox / "applied" / "2026-08-01-2026-09-alp-find-x.md"
        old.write_text("прежний\n", encoding="utf-8")
        rc, out = self.deliver()
        self.assertEqual(rc, 0)
        self.assertTrue(old.exists())
        self.assertIn("уже разобран", out)
        self.assertEqual(self.queue(), ["2026-09-01-2026-09-alp-find-x.md"])

    def test_empty_brief_refused(self):
        with self.assertRaises(SystemExit) as e:
            self.deliver(body="   \n")
        self.assertEqual(e.exception.code, 1)
        self.assertEqual(self.queue(), [])

    def test_trailing_newline_added(self):
        self.deliver(body="# Без перевода строки")
        self.assertTrue((self.inbox / self.queue()[0]).read_text().endswith("\n"))

    def test_no_temp_file_left(self):
        self.deliver()
        self.assertEqual([p.name for p in self.inbox.glob("*.tmp")], [])

    def test_dry_run_writes_nothing(self):
        args = self.ns(cmd="deliver", name="find-x", source="-", date="2026-09-01",
                       dry_run=True, project_root=str(self.project), slug=None)
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()), self.patch_stdin("# Находка\n"):
            self.cb.cmd_deliver(args)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.pending(), [])


class Hardening(Base):
    """Регресс на находки состязательного ревью."""

    def test_foreign_project_brief_not_deleted(self):
        """Глоб по хвосту совпадал, когда наш слаг оказывался суффиксом
        чужого: проект alp удалял файлы проекта foo-alp как свою прежнюю
        версию."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        foreign = self.inbox / "2026-08-01-foo-2026-09-alp-find-x.md"
        foreign.write_text("чужой\n", encoding="utf-8")
        self.deliver()
        self.assertTrue(foreign.exists())

    def test_foreign_queue_entry_is_not_counted_as_delivered(self):
        """Обратная сторона того же: чужой файл давал ложный ноль сверки."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / "2026-08-01-foo-2026-09-alp-hand.md").write_text("чужой\n")
        (self.project / "toolkit-log" / "upstream-pending" / "hand.md").write_text("свой\n")
        rc, out = self.check()
        self.assertEqual(rc, 4)
        self.assertIn("hand.md", out)

    def test_suffix_slug_no_longer_collides(self):
        """Настоящий дефект: слаг alp подхватывал файлы проекта foo-alp."""
        self.assertTrue(self.cb.matches_candidate(
            "2026-09-01-alp-find-x.md", "alp", "find-x"))
        self.assertFalse(self.cb.matches_candidate(
            "2026-09-01-foo-alp-find-x.md", "alp", "find-x"))

    def test_known_residual_split_ambiguity(self):
        """Остаточная неоднозначность, закрепленная намеренно: имя
        `<дата>-alp-foo-bar.md` разбирается и как (alp, foo-bar), и как
        (alp-foo, bar). Убрать ее можно только сменой формата имени, что
        порвало бы существующую очередь. Практически она безвредна: слаг
        берется от проекта, а не угадывается, и внутри проекта он один."""
        for slug, name in (("alp", "foo-bar"), ("alp-foo", "bar")):
            self.assertTrue(self.cb.matches_candidate(
                "2026-09-01-alp-foo-bar.md", slug, name))

    def test_bad_date_refused(self):
        """Дата шла прямо в путь: '../..' и '/tmp/x' писали за пределы очереди."""
        for bad in ("../../escaped", "/tmp/escaped", "*", "2026-99-99", "2026-9-1"):
            with self.subTest(date=bad), self.assertRaises(SystemExit) as e:
                self.deliver(stamp=bad)
            self.assertEqual(e.exception.code, 1)
        self.assertEqual(self.queue(), [])

    def test_non_utf8_body_refused_controllably(self):
        with self.assertRaises(SystemExit) as e:
            self.deliver(body=b"\xff\xfe binary")
        self.assertEqual(e.exception.code, 1)
        self.assertEqual(self.queue(), [])

    def test_bom_only_body_is_empty(self):
        with self.assertRaises(SystemExit) as e:
            self.deliver(body="\ufeff\n")
        self.assertEqual(e.exception.code, 1)

    def test_temp_file_removed_on_failure(self):
        """Предсказуемое имя временного файла оставляло мусор рядом с очередью
        и ломало два параллельных прогона."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        target = self.inbox / "x.md"
        orig = self.cb.os.replace
        self.cb.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("бум"))
        try:
            with self.assertRaises(OSError):
                self.cb.write_atomic(target, "тело\n")
        finally:
            self.cb.os.replace = orig
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), [])

    def test_explicit_slug_is_remembered(self):
        """Иначе deliver --slug X и следующий check без флага работали с
        разными слагами: сверка объявляла свой же бриф недоставленным."""
        self.deliver(slug="custom")
        self.assertEqual(self.queue(), ["2026-09-01-custom-find-x.md"])
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertIn("все доставлены", out)

    def test_diverged_content_is_not_silent(self):
        """Имена совпадают, версии разные - прежде это считалось доставкой
        и молчало навсегда."""
        self.deliver(body="# v1\n")
        (self.project / "toolkit-log" / "upstream-pending" / "find-x.md").write_text(
            "# v2\n", encoding="utf-8")
        rc, out = self.check()
        self.assertEqual(rc, 4)
        self.assertIn("копии разошлись по содержимому", out)
        rc, _ = self.check(redeliver=True, restore_local=True)
        self.assertEqual(rc, 4)  # чинить автоматически нечем: решает человек

    def test_saved_slug_with_traversal_ignored(self):
        """`.canon-slug` лежит в проекте и правится чем угодно, а слаг идет
        прямо в путь: непроверенное значение уводило запись из очереди."""
        (self.project / "toolkit-log" / ".canon-slug").write_text(
            "x/../../../escaped\n", encoding="utf-8")
        self.deliver()
        self.assertEqual(self.queue(), ["2026-09-01-2026-09-alp-find-x.md"])

    def test_saved_slug_non_utf8_does_not_crash(self):
        (self.project / "toolkit-log" / ".canon-slug").write_bytes(b"\xff\xfe\n")
        rc, _ = self.deliver()
        self.assertEqual(rc, 0)

    def test_impossible_date_in_queue_is_not_a_delivery(self):
        """`2026-99-99-...` формат проходит, датой не является - принимать
        такой файл за доставку значит считать доставленным чужое."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / "2026-99-99-2026-09-alp-hand.md").write_text("x\n")
        (self.project / "toolkit-log" / "upstream-pending" / "hand.md").write_text("y\n")
        rc, out = self.check()
        self.assertEqual(rc, 4)
        self.assertIn("hand.md", out)

    def test_multiple_bom_body_is_empty(self):
        with self.assertRaises(SystemExit) as e:
            self.deliver(body="\ufeff \ufeff\n")
        self.assertEqual(e.exception.code, 1)

    def test_existing_file_mode_preserved(self):
        """mkstemp дает 0600 - перезапись не должна отбирать у файла права."""
        self.deliver()
        q = self.inbox / self.queue()[0]
        q.chmod(0o644)
        self.deliver(body="# новое\n")
        self.assertEqual((self.inbox / self.queue()[0]).stat().st_mode & 0o777, 0o644)

    def test_stale_temp_files_swept(self):
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / ".cb-abc.tmp").write_text("мусор")
        self.check()
        self.assertEqual([p.name for p in self.inbox.glob(".cb-*.tmp")], [])

    def test_control_chars_in_name_do_not_forge_lines(self):
        """Вывод check читается построчно наблюдением toolkit-репо."""
        self.assertEqual(self.cb.safe_line("real\n- forged.md"), "real?- forged.md")


class Naming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cb = load(Path(self.tmp.name) / "inbox")
        self.addCleanup(self.tmp.cleanup)

    def test_path_traversal_refused(self):
        """Имя брифа идет прямо в путь: разделитель или '..' вывели бы запись
        за пределы очереди."""
        for bad in ("../escape", "a/b", "/abs", "..", ".hidden"):
            with self.subTest(name=bad), self.assertRaises(SystemExit) as e:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.cb.check_name(bad)
            self.assertEqual(e.exception.code, 1)

    def test_md_suffix_stripped(self):
        self.assertEqual(self.cb.check_name("find-x.md"), "find-x")

    def test_slug_from_ascii_token(self):
        slug, translit = self.cb.slugify("2028-01 ALP")
        self.assertEqual(slug, "2028-01-alp")
        self.assertFalse(translit)

    def test_slug_transliterates_and_flags(self):
        """Слаг обязан быть стабильным между прогонами - иначе тот же кандидат
        приедет вторым файлом под новым именем."""
        slug, translit = self.cb.slugify("Проба Пера")
        self.assertTrue(translit)
        self.assertEqual(slug, self.cb.slugify("Проба Пера")[0])
        self.assertTrue(slug.isascii() and slug)

    def test_slug_collapses_separators(self):
        self.assertEqual(self.cb.slugify("2026-08  Get Course / Larisa")[0],
                         "2026-08-get-course-larisa")


class Backstop(Base):
    """check - бэкстоп на бриф, написанный руками мимо скрипта."""

    def hand_write(self, name="hand-made", body="# Руками\n", mtime=None):
        p = self.project / "toolkit-log" / "upstream-pending" / f"{name}.md"
        p.write_text(body, encoding="utf-8")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_no_pending_folder_is_not_a_failure(self):
        import shutil
        shutil.rmtree(self.project / "toolkit-log")
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertIn("ничего не выносил", out)

    def test_empty_pending(self):
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertIn("пуст", out)

    def test_stranded_brief_found(self):
        self.hand_write()
        rc, out = self.check()
        self.assertEqual(rc, 4)
        self.assertIn("hand-made.md", out)

    def test_delivered_brief_not_reported(self):
        self.deliver()
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertIn("все доставлены", out)

    def test_terminal_counts_as_delivered(self):
        """Разобранный кандидат доставлять заново не надо - иначе каждый
        прогон возвращал бы в очередь то, что уже влито или отклонено."""
        self.hand_write(name="find-x")
        (self.inbox / "rejected").mkdir(parents=True)
        (self.inbox / "rejected" / "2026-08-01-2026-09-alp-find-x.md").write_text("x\n")
        rc, _ = self.check()
        self.assertEqual(rc, 0)

    def test_redeliver_uses_file_date_not_today(self):
        """Застрявший месяц назад бриф не должен выглядеть свежим."""
        import datetime
        old = datetime.datetime(2026, 7, 15, 12, 0).timestamp()
        self.hand_write(mtime=old)
        rc, out = self.check(redeliver=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.queue(), ["2026-07-15-2026-09-alp-hand-made.md"])
        self.assertIn("доставлен", out)

    def test_missing_project_copy_is_restored(self):
        """Код 3 обещал починку через check --redeliver, а сверка шла только в
        одну сторону: обещание молчало одинаково при исправном и сломанном."""
        self.deliver()
        (self.project / "toolkit-log" / "upstream-pending" / "find-x.md").unlink()
        rc, out = self.check()
        self.assertEqual(rc, 4)
        self.assertIn("в очереди есть, а в проекте нет", out)
        # обратное направление чинится только явным флагом: имя брифа тут
        # выводится из имени файла очереди, и разбор неоднозначен
        rc, _ = self.check(redeliver=True)
        self.assertEqual(self.pending(), [])
        rc, _ = self.check(restore_local=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.pending(), ["find-x.md"])

    def test_project_terminal_brief_is_not_resurrected(self):
        """Бриф, переведенный проектом в upstream-applied/, отсутствует в
        pending законно - воскрешать его значит вернуть закрытую работу."""
        self.deliver()
        pend = self.project / "toolkit-log" / "upstream-pending" / "find-x.md"
        done = self.project / "toolkit-log" / "upstream-applied"
        done.mkdir(parents=True)
        pend.rename(done / "find-x.md")
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertEqual(self.pending(), [])

    def test_other_project_queue_entries_ignored(self):
        """Слаг соседа НАЧИНАЕТСЯ с нашего - именно этот случай проходил мимо
        проверки: 2026-09-alp против 2026-09-alp-foo."""
        (self.inbox).mkdir(parents=True, exist_ok=True)
        (self.inbox / "2026-09-01-2026-09-alp-foo-bar.md").write_text("чужой\n")
        rc, out = self.check(restore_local=True)
        # файл разбирается неоднозначно, поэтому он показывается, но
        # восстановление идет только по явному флагу и с оговоркой в выводе
        self.assertIn("имя брифа тут выводится", out)

    def test_redeliver_is_idempotent(self):
        self.hand_write()
        self.check(redeliver=True)
        before = self.queue()
        rc, _ = self.check(redeliver=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.queue(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
