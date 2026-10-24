'''Blind FR-SKR-05/06 tests for ai-sync --retire-source (public CLI only).

Contract: plan prints JSON {status, applicable, plan_sha256, changes, conflicts}; apply needs
--expect-plan <plan_sha256>. Synthetic fixtures, no implementation imports. Authored by Sonnet 5.5;
authoring is not execution: these tests have NOT been run. Positive retire controls precede every
refusal check, so an unimplemented flag fails them (RED) instead of passing as a fake refusal.
Negative cases run the CLI via subprocess and reject argparse unknown-flag errors as a reason.
'''
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_ai_bootstrap as bootstrap
from test_ai_sync import SyncFixture, tree

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ai-sync.py'
SAMPLE = 'skills/sample/SKILL.md'
SECOND = 'skills/second/SKILL.md'
SECOND_BODY = b'---\nname: second\ndescription: Synthetic second skill.\n---\n# Second\n'
MANDATORY = 'rules/agent-portability.md'
FLAG = '--retire-source'
EXPECT = '--expect-plan'
WRONG_DIGEST = '0' * 64


def args(*paths):
    out = []
    for path in paths:
        out += [FLAG, path]
    return out


def strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


class RetireSourceContract(SyncFixture):
    def setUp(self):
        super().setUp()
        self.sample_dest = self.destination(SAMPLE)
        self.assertTrue(self.sample_dest.is_file(), 'fixture must track the sample skill')
        self.sample_bytes = self.sample_dest.read_bytes()
        self.retired_root = self.root / '.ai-bootstrap/retired'

    # helpers -----------------------------------------------------------
    def build(self):
        bootstrap.AiBootstrapContract.command(self, 'build')

    def raw(self, verb, extra=()):
        cmd = [sys.executable, str(SCRIPT), verb, '--root', str(self.root)]
        if verb in ('plan', 'apply'):
            cmd += ['--bundle', str(self.bundle)]
        return subprocess.run(cmd + list(extra), capture_output=True, text=True, timeout=20)

    def parse(self, result):
        try:
            return json.loads(result.stdout)
        except ValueError:
            return None

    def sync(self, extra=()):
        data = self.parse(self.cli('plan', extra))
        self.assertIsInstance(data, dict)
        self.cli('apply', list(extra) + [EXPECT, data['plan_sha256']])

    def add_second(self):
        self.add_upstream(SECOND, SECOND_BODY)
        self.sync()
        self.second_dest = self.destination(SECOND)
        self.assertTrue(self.second_dest.is_file())
        self.second_bytes = self.second_dest.read_bytes()

    def drop(self, *paths):
        for path in paths:
            self.remove_upstream(path)

    def retire_plan(self, paths):
        data = self.parse(self.cli('plan', args(*paths)))
        self.assertIsInstance(data, dict)
        self.assertIs(data.get('applicable'), True)
        self.assertEqual(64, len(data['plan_sha256']))
        return data

    def retire(self, *paths):
        data = self.retire_plan(paths)
        return self.cli('apply', args(*paths) + [EXPECT, data['plan_sha256']])

    def backup_path(self, canonical, content):
        return self.retired_root / hashlib.sha256(content).hexdigest() / canonical

    def assert_apply_rejected(self, extra):
        before = tree(self.root)
        result = self.raw('apply', extra)
        text = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, text)
        self.assertNotIn('unrecognized arguments', text)
        self.assertTrue(text.strip(), 'a diagnosed reason is required')
        self.assertEqual(before, tree(self.root), 'rejected apply must not write')

    def assert_refused(self, paths):
        before = tree(self.root)
        result = self.raw('plan', args(*paths))
        text = result.stdout + result.stderr
        self.assertNotIn('unrecognized arguments', text)
        data = self.parse(result)
        if isinstance(data, dict) and data.get('applicable') is False:
            reason = data.get('conflicts') or result.stderr.strip()
        elif result.returncode == 2:
            reason = result.stderr.strip() or result.stdout.strip()
        else:
            self.fail('plan neither rc2 nor applicable=false: ' + text)
        self.assertTrue(reason, 'a diagnosed reason is required')
        self.assertEqual(before, tree(self.root), 'plan must not write')
        digest = data.get('plan_sha256') if isinstance(data, dict) else None
        self.assert_apply_rejected(args(*paths) + [EXPECT, digest or WRONG_DIGEST])
        self.assertFalse(self.retired_root.exists(), 'no backup on refusal')

    # plan ----------------------------------------------------------------
    def test_plan_with_retire_is_read_only(self):
        self.drop(SAMPLE)
        before = tree(self.root)
        data = self.retire_plan([SAMPLE])
        self.assertEqual(before, tree(self.root))
        for key in ('status', 'changes', 'conflicts'):
            self.assertIn(key, data)
        self.assertTrue(self.sample_dest.is_file())

    def test_plan_digest_covers_sorted_retire_set(self):
        self.add_second()
        self.drop(SAMPLE, SECOND)
        plain = self.parse(self.cli('plan'))['plan_sha256']
        first = self.retire_plan([SAMPLE])['plan_sha256']
        both = self.retire_plan([SAMPLE, SECOND])['plan_sha256']
        swapped = self.retire_plan([SECOND, SAMPLE])['plan_sha256']
        self.assertNotEqual(plain, first)
        self.assertNotEqual(first, both)
        self.assertEqual(both, swapped, 'retire list is sorted before hashing')
        self.assertEqual(first, self.retire_plan([SAMPLE])['plan_sha256'])

    # ordinary sync (regression guard, expected GREEN before the feature) -------
    def test_plain_sync_retains_removed_upstream_file(self):
        self.drop(SAMPLE)
        self.sync()
        self.assertEqual(self.sample_bytes, self.sample_dest.read_bytes())
        self.assertFalse(self.retired_root.exists())
        settled = tree(self.root)
        self.sync()
        self.assertEqual(settled, tree(self.root))

    # explicit retire ---------------------------------------------------------
    def test_retire_removes_clean_file_and_backs_up_by_content_sha256(self):
        self.drop(SAMPLE)
        self.retire(SAMPLE)
        self.assertFalse(self.sample_dest.exists())
        backup = self.backup_path(SAMPLE, self.sample_bytes)
        self.assertTrue(backup.is_file())
        self.assertEqual(self.sample_bytes, backup.read_bytes())
        self.assertEqual(1, len(list(self.retired_root.iterdir())))
        self.cli('check')

    def test_retire_updates_records_and_keeps_protected_files(self):
        self.drop(SAMPLE)
        initial = {k: v for k, v in self.initial_state.items() if k != 'sync'}
        self.assertTrue(any(SAMPLE in s for s in strings(initial)), 'precondition: state tracks the file')
        intent = self.intent_path.read_bytes()
        before = tree(self.root)
        self.retire(SAMPLE)
        state = self.read_json(self.state_path)
        self.assertIn('sync', state)
        others = {k: v for k, v in state.items() if k != 'sync'}
        self.assertFalse(any(SAMPLE in s for s in strings(others)), 'source_files/generated membership')
        self.assertTrue(any(SAMPLE in s for s in strings(state['sync']['retired_sources'])))
        starts = [p for p in self.root.rglob('START*')
                  if p.is_file() and '.ai-bootstrap' not in p.relative_to(self.root).parts]
        self.assertTrue(starts, 'a START file is expected in the fixture')
        for start in starts:
            self.assertNotIn(SAMPLE.encode(), start.read_bytes())
        self.assertEqual(intent, self.intent_path.read_bytes())
        after = tree(self.root)
        for key, value in before.items():
            if key.startswith('.ai-bootstrap') or SAMPLE in key:
                continue
            if any(part in key for part in ('memory', 'identity', 'settings')):
                self.assertEqual(value, after.get(key), key)

    def test_repeatable_flag_removes_only_exact_files(self):
        self.add_second()
        notes = {}
        for dest in (self.sample_dest, self.second_dest):
            note = dest.parent / 'notes.md'
            note.write_bytes(b'local note ' + dest.parent.name.encode() + b'\n')
            notes[note] = note.read_bytes()
        self.drop(SAMPLE, SECOND)
        self.retire(SAMPLE, SECOND)
        for dest in (self.sample_dest, self.second_dest):
            self.assertFalse(dest.exists())
            self.assertTrue(dest.parent.is_dir())
        for note, content in notes.items():
            self.assertEqual(content, note.read_bytes())
        self.assertEqual(self.sample_bytes, self.backup_path(SAMPLE, self.sample_bytes).read_bytes())
        self.assertEqual(self.second_bytes, self.backup_path(SECOND, self.second_bytes).read_bytes())
        self.cli('check')

    def test_repeat_of_completed_retire_is_noop(self):
        self.drop(SAMPLE)
        self.retire(SAMPLE)
        settled = tree(self.root)
        backup = self.backup_path(SAMPLE, self.sample_bytes)
        self.retire(SAMPLE)
        self.assertEqual(settled, tree(self.root))
        self.assertEqual(self.sample_bytes, backup.read_bytes())
        self.assertFalse(self.sample_dest.exists())
        self.cli('check')

    # refusals, no writes -------------------------------------------------------
    def test_unknown_and_duplicate_sources_refused(self):
        self.drop(SAMPLE)
        self.retire_plan([SAMPLE])
        self.assert_refused([SAMPLE, 'skills/never-tracked/SKILL.md'])
        self.assert_refused(['skills/never-tracked/SKILL.md'])
        self.assert_refused([SAMPLE, SAMPLE])

    def test_still_shipped_file_refused(self):
        self.add_second()
        self.drop(SAMPLE)
        self.retire_plan([SAMPLE])
        self.assert_refused([SAMPLE, SECOND])
        self.assert_refused([SECOND])

    def test_unsafe_paths_and_directories_refused(self):
        self.drop(SAMPLE)
        self.retire_plan([SAMPLE])
        for bad in ('../outside.md', '/etc/passwd', 'skills/../../outside.md', 'skills/sample',
                    'skills/sample/', '.AI/skills/sample/SKILL.md'):
            with self.subTest(path=bad):
                self.assert_refused([SAMPLE, bad])

    def test_symlinked_tracked_file_refused(self):
        self.add_second()
        self.drop(SAMPLE, SECOND)
        self.retire_plan([SAMPLE])
        target = self.root / 'outside-target.txt'
        target.write_bytes(b'must survive\n')
        self.second_dest.unlink()
        self.second_dest.symlink_to(target)
        self.assert_refused([SAMPLE, SECOND])
        self.assert_refused([SECOND])
        self.assertEqual(b'must survive\n', target.read_bytes())

    def test_local_edit_is_not_retired_or_lost(self):
        self.add_second()
        edited = self.second_bytes + b'local addition\n'
        self.second_dest.write_bytes(edited)
        self.build()
        self.drop(SAMPLE, SECOND)
        self.retire_plan([SAMPLE])
        self.assert_refused([SAMPLE, SECOND])
        self.assert_refused([SECOND])
        self.assertEqual(edited, self.second_dest.read_bytes())

    def test_override_and_skip_sync_marked_files_refused(self):
        self.add_second()
        self.drop(SAMPLE, SECOND)
        self.edit_json(self.intent_path, overrides=[SECOND])
        self.build()
        self.retire_plan([SAMPLE])
        self.assert_refused([SAMPLE, SECOND])
        self.assert_refused([SECOND])
        self.edit_json(self.intent_path, overrides=[], skip_sync=[SECOND])
        self.build()
        self.retire_plan([SAMPLE])
        self.assert_refused([SAMPLE, SECOND])
        self.assert_refused([SECOND])
        self.assertEqual(self.second_bytes, self.second_dest.read_bytes())

    def test_mandatory_rule_outside_skills_refused(self):
        mandatory = self.destination(MANDATORY)
        self.assertTrue(mandatory.is_file(), 'fixture must track the mandatory rule')
        self.drop(SAMPLE)
        self.retire_plan([SAMPLE])
        self.assert_refused([SAMPLE, MANDATORY])
        self.assert_refused([MANDATORY])
        if ('  - ' + MANDATORY + '\n') in (self.bundle / 'manifest.yaml').read_text():
            self.drop(MANDATORY)
            self.assert_refused([SAMPLE, MANDATORY])
            self.assert_refused([MANDATORY])
        self.assertTrue(mandatory.is_file())

    # digest and stale plan ---------------------------------------------------------
    def test_apply_requires_matching_digest(self):
        self.drop(SAMPLE)
        plain = self.parse(self.cli('plan'))['plan_sha256']
        good = self.retire_plan([SAMPLE])['plan_sha256']
        self.assert_apply_rejected(args(SAMPLE))
        self.assert_apply_rejected(args(SAMPLE) + [EXPECT, WRONG_DIGEST])
        self.assert_apply_rejected(args(SAMPLE) + [EXPECT, plain])
        self.assertTrue(self.sample_dest.is_file())
        self.cli('apply', args(SAMPLE) + [EXPECT, good])
        self.assertFalse(self.sample_dest.exists())

    def test_apply_after_local_drift_since_plan_refused(self):
        self.drop(SAMPLE)
        digest = self.retire_plan([SAMPLE])['plan_sha256']
        self.sample_dest.write_bytes(self.sample_bytes + b'drift after plan\n')
        self.assert_apply_rejected(args(SAMPLE) + [EXPECT, digest])
        self.assertFalse(self.retired_root.exists())
        self.assertEqual(self.sample_bytes + b'drift after plan\n', self.sample_dest.read_bytes())
        self.assert_refused([SAMPLE])

    def test_apply_after_upstream_restored_since_plan_refused(self):
        manifest = self.bundle / 'manifest.yaml'
        saved_manifest = manifest.read_text()
        saved_body = (self.bundle / SAMPLE).read_bytes()
        self.drop(SAMPLE)
        digest = self.retire_plan([SAMPLE])['plan_sha256']
        manifest.write_text(saved_manifest)
        (self.bundle / SAMPLE).write_bytes(saved_body)
        self.assert_apply_rejected(args(SAMPLE) + [EXPECT, digest])
        self.assertEqual(self.sample_bytes, self.sample_dest.read_bytes())
        self.assertFalse(self.retired_root.exists())


if __name__ == '__main__':
    unittest.main()
