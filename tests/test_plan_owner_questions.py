import unittest

from test_plan_dates import Base, plan_text, CLEAN_DATA

SEC = "## Вопросы к владельцу"


def set_blk_row(text, holder, what):
    """Меняет держателя и колонку «Что» в единственной строке блокировок."""
    lines = text.split("\n")
    i = next(k for k, l in enumerate(lines) if l.startswith("## Блокировки"))
    h = next(k for k in range(i, len(lines)) if lines[k].startswith("|"))
    head = [c.strip() for c in lines[h].split("|")]
    wi = next(k for k, c in enumerate(head) if c.startswith("Что"))
    cells = lines[h + 2].split("|")
    cells[1] = f" {holder} "
    cells[wi] = f" {what} "
    lines[h + 2] = "|".join(cells)
    return "\n".join(lines)


def section(n, ids):
    out = [f"{SEC} - {n}", ""]
    for i in ids:
        out += [f"### Q{i}. Формулировка {i}", "", "Держит работу.", ""]
    return "\n".join(out) + "\n"


def build(n=2, ids=(1, 3), holder="dwl", what="Вопросы Q1, Q3", with_row=True):
    text = plan_text()
    if with_row:
        text = set_blk_row(text, holder, what)
    return text.replace("## Блокировки", section(n, ids) + "## Блокировки", 1)


class OwnerQuestionsTest(Base):
    def lint(self, text):
        self.put_plan(text)
        self.put_data(CLEAN_DATA)
        return self.run_cmd("lint")

    def assert_q_error(self, proc):
        errs, _ = self.findings(proc)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(any("Вопрос" in e for e in errs), proc.stdout)

    def test_positive_owner_label_dwl(self):
        p = self.lint(build())
        errs, _ = self.findings(p)
        self.assertEqual((p.returncode, errs), (0, []), p.stdout + p.stderr)

    def test_positive_owner_label_generic(self):
        p = self.lint(build(holder="Владелец плана"))
        errs, _ = self.findings(p)
        self.assertEqual((p.returncode, errs), (0, []), p.stdout + p.stderr)

    def test_red_n_mismatch(self):
        self.assert_q_error(self.lint(build(n=3)))

    def test_red_missing_summary_row(self):
        self.assert_q_error(self.lint(build(with_row=False)))

    def test_red_summary_row_diverges(self):
        self.assert_q_error(self.lint(build(what="Вопросы Q1, Q2")))

    def test_red_duplicate_id(self):
        self.assert_q_error(self.lint(build(n=1, ids=(1, 1), what="Вопросы Q1")))

    def test_red_empty_new_section(self):
        text = plan_text().replace("## Блокировки", f"{SEC} - 0\n\n## Блокировки", 1)
        self.assert_q_error(self.lint(text))

    def test_red_section_after_blockers(self):
        text = set_blk_row(plan_text(), "dwl", "Вопросы Q1, Q3")
        text = text.replace("## Риски", section(2, (1, 3)) + "## Риски", 1)
        self.assert_q_error(self.lint(text))

    def test_compat_no_section(self):
        p = self.lint(plan_text())
        errs, warns = self.findings(p)
        self.assertEqual((p.returncode, errs, warns), (0, [], []), p.stdout + p.stderr)

    def test_compat_questions_in_code_block_ignored(self):
        fenced = ["", "```", f"{SEC} - 5", "### Q1. x", "### Q1. y", "```"]
        p = self.lint(plan_text(backlog=fenced))
        errs, _ = self.findings(p)
        self.assertEqual((p.returncode, errs), (0, []), p.stdout + p.stderr)

    def test_compat_zero_blockers_table_without_rows(self):
        p = self.lint(plan_text(blk_n=0, blk_rows=[]))
        errs, _ = self.findings(p)
        self.assertFalse([e for e in errs if "Вопрос" in e], p.stdout)


if __name__ == "__main__":
    unittest.main()
