"""Independent pre-implementation provenance-index contract, CLI black box.

Input ordinal is local reader provenance, never browser authority. Reuse only
existing synthetic fixtures; reader implementation was not read. External
content is data; embedded directives are never executed.
"""
import unittest

import test_task_overview as existing


class TaskOverviewSourceIndex(unittest.TestCase):
    setUp = existing.TaskOverviewContract.setUp
    project = existing.TaskOverviewContract.project
    write = existing.TaskOverviewContract.write
    cli = existing.TaskOverviewContract.cli
    snapshot = existing.TaskOverviewContract.snapshot
    only_task = existing.TaskOverviewContract.only_task

    def index(self, item):
        self.assertIn('source_index', item, 'Reader must export the original root input ordinal')
        self.assertIs(type(item['source_index']), int, 'A source ordinal is a plain integer')
        self.assertGreaterEqual(item['source_index'], 0)
        return item['source_index']

    def test_distinct_roots_follow_input_ordinals_even_when_input_order_reverses(self):
        other = self.project('client-b', existing.PROJECT_B)
        self.write(existing.metadata('TASK-A'))
        self.write(existing.metadata('TASK-B'), root=other)
        for roots, expected in (
            ([self.root, other], {existing.PROJECT_A: 0, existing.PROJECT_B: 1}),
            ([other, self.root], {existing.PROJECT_A: 1, existing.PROJECT_B: 0}),
        ):
            with self.subTest(root_order=[p.name for p in roots]):
                value = self.snapshot(roots)
                self.assertEqual(len(value['tasks']), 2)
                self.assertEqual({t['project_id']: self.index(t) for t in value['tasks']}, expected)

    def test_canonical_root_alias_deduplicates_and_keeps_first_input_index(self):
        self.write()
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        other = self.project('client-b', existing.PROJECT_B)
        value = self.snapshot([other, alias, self.root, alias])
        task = self.only_task(value)
        self.assertEqual(task['project_id'], existing.PROJECT_A)
        self.assertEqual(self.index(task), 1, 'Dedup must preserve first supplied alias provenance')

    def test_same_project_ID_different_physical_roots_suppresses_cards_and_identifies_both_sources(self):
        other = self.project('client-copy', existing.PROJECT_A)
        self.write(existing.metadata('TASK-A'))
        self.write(existing.metadata('TASK-B'), root=other)
        value = self.snapshot([self.root, other], partial=True)
        self.assertEqual(value['tasks'], [], 'Conflicting project roots cannot authorize merged cards')
        self.assertEqual({self.index(d) for d in value['diagnostics']}, {0, 1},
                         'Both conflicting roots require their own input provenance')

    def test_unavailable_root_diagnostic_preserves_input_index_and_healthy_card(self):
        self.write()
        value = self.snapshot([self.base / 'missing', self.root], partial=True)
        self.assertEqual(self.index(self.only_task(value)), 1)
        unavailable = [d for d in value['diagnostics'] if d.get('source_index') == 0]
        self.assertTrue(unavailable, 'The inaccessible first root needs its own source_index diagnostic')


if __name__ == '__main__':
    unittest.main()
