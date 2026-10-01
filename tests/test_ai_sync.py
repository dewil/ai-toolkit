"""Blind FR-AIS01..09 acceptance tests from the accepted public CLI contract.

Fixtures are synthetic; bootstrap is executed as a black box. No sync helpers
or private client files are imported. Model used to author these tests: unknown.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_ai_bootstrap as bootstrap

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ai-sync.py'


def tree(root):
    """Bytes, link targets and permission bits, including empty directories."""
    return {key: (value, (root / key).lstat().st_mode & 0o7777)
            for key, value in bootstrap.snapshot(root).items()}


class SyncFixture(unittest.TestCase):
    put = bootstrap.AiBootstrapContract.put
    policy = bootstrap.AiBootstrapContract.policy

    def setUp(self):
        bootstrap.AiBootstrapContract.setUp(self)
        bootstrap.AiBootstrapContract.command(self, 'apply')
        self.assertTrue(SCRIPT.is_file(), 'FR-AIS: scripts/ai-sync.py implementation required')
        self.state_path = self.root / '.AI/canon/canon.state.json'
        self.intent_path = self.root / '.AI/canon/canon.intent.yaml'
        self.initial_state = self.read_json(self.state_path)

    def read_json(self, path):
        return json.loads(path.read_text())

    def edit_json(self, path, **changes):
        value = self.read_json(path)
        value.update(changes)
        path.write_text(json.dumps(value))

    def destination(self, canonical):
        return self.root / (canonical if canonical.startswith('scripts/') else
                            '.AI/' + canonical.replace('agents/', 'roles/', 1))

    def upstream(self, path, body):
        self.put(self.bundle / path, body)

    def add_upstream(self, path, body=b'# New upstream\n'):
        self.upstream(path, body)
        manifest = self.bundle / 'manifest.yaml'
        manifest.write_text(manifest.read_text().replace('universal:\n', 'universal:\n  - ' + path + '\n', 1))

    def remove_upstream(self, path):
        manifest = self.bundle / 'manifest.yaml'
        manifest.write_text(manifest.read_text().replace('  - ' + path + '\n', ''))
        (self.bundle / path).unlink()

    def cli(self, verb, extra=(), reject=False):
        args = [sys.executable, str(SCRIPT), verb, '--root', str(self.root)]
        if verb in ('plan', 'apply'):
            args += ['--bundle', str(self.bundle)]
        result = subprocess.run(args + list(extra), capture_output=True, text=True, timeout=20)
        if reject:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def plan(self):
        before = tree(self.root)
        result = self.cli('plan')
        self.assertEqual(tree(self.root), before, 'FR-AIS04: plan must not create lock/journal')
        value = json.loads(result.stdout)
        self.assertIn(value['status'], ('planned', 'up-to-date'))
        self.assertIs(type(value['applicable']), bool)
        self.assertRegex(value['plan_sha256'], r'^[0-9a-f]{64}$')
        self.assertIsInstance(value['changes'], list)
        self.assertIsInstance(value['conflicts'], list)
        for row in value['changes']:
            for key in ('canonical', 'path', 'class'):
                self.assertIsInstance(row[key], str)
        for row in value['conflicts']:
            for key in ('canonical', 'path', 'reason'):
                self.assertIsInstance(row[key], str)
        for marker in ('PRIVATE_PROJECT_SENTINEL', 'PRIVATE_MEMORY_SENTINEL'):
            self.assertNotIn(marker, result.stdout)
        def strings(item):
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                for child in item.values():
                    yield from strings(child)
            elif isinstance(item, list):
                for child in item:
                    yield from strings(child)
        output_strings = list(strings(value))
        for source in self.bundle.rglob('*'):
            if source.is_file() and source.name != 'manifest.yaml':
                body = source.read_bytes()
                for encoded in [body.decode('utf-8'), base64.b64encode(body).decode('ascii')]:
                    self.assertFalse(any(encoded in text for text in output_strings),
                                     'FR-AIS08: plan leaked source payload')
        return value

    def apply(self, plan=None):
        plan = plan or self.plan()
        self.assertTrue(plan['applicable'], plan)
        return self.cli('apply', ['--expect-plan', plan['plan_sha256']])

    def reject_apply(self, digest):
        before = tree(self.root)
        self.cli('apply', ['--expect-plan', digest], reject=True)
        self.assertEqual(tree(self.root), before)
        self.assertFalse((self.root / '.ai-bootstrap/sync.json').exists())

    def classes(self, plan):
        return {row['canonical']: row['class'] for row in plan['changes']}

    def baseline(self, path):
        return self.read_json(self.state_path)['source_files'][path]['sha256']

    def assert_base(self, path, body):
        self.assertEqual(self.baseline(path), hashlib.sha256(body).hexdigest())


class AiSyncAcceptance(SyncFixture):
    def test_01_three_way_clean_new_converged_unchanged_and_removed(self):
        self.upstream('rules/coding.md', b'# Coding\nUPSTREAM_UPDATE\n')
        self.upstream('rules/wiki.md', b'# Wiki\nCONVERGED\n')
        self.destination('rules/wiki.md').write_bytes(b'# Wiki\nCONVERGED\n')
        self.add_upstream('agents/new-role.md', b'# New role\n')
        self.remove_upstream('commands/review.md')
        plan = self.plan()
        self.assertEqual(self.plan(), plan)
        classes = self.classes(plan)
        for path, expected in [('rules/coding.md', 'update'), ('rules/wiki.md', 'converged'),
                               ('agents/new-role.md', 'new'), ('commands/review.md', 'removed-upstream')]:
            self.assertEqual(classes[path], expected)
        self.assertEqual(classes[bootstrap.MANDATORY[0]], 'unchanged')
        before_identity = (self.root / '.AI/project.json').read_bytes()
        self.apply(plan)
        for path in ['rules/coding.md', 'rules/wiki.md', 'agents/new-role.md']:
            self.assertEqual(self.destination(path).read_bytes(), (self.bundle / path).read_bytes())
            self.assert_base(path, (self.bundle / path).read_bytes())
        self.assertEqual(self.destination('commands/review.md').read_bytes(), self.sources['commands/review.md'])
        self.assert_base('commands/review.md', self.sources['commands/review.md'])
        self.assertEqual((self.root / '.AI/project.json').read_bytes(), before_identity)
        self.assertNotEqual(self.read_json(self.state_path)['source'], self.initial_state['source'])
        self.assertFalse((self.root / 'canon.yaml').exists())
        self.cli('check')
        before = tree(self.root)
        self.apply()
        self.assertEqual(tree(self.root), before)

    def test_02_true_upstream_base_survives_local_edit_without_build(self):
        path = bootstrap.MANDATORY[0]
        body = self.sources[path] + b'LOCAL_FACT_NOT_UPSTREAM\n'
        self.destination(path).write_bytes(body)
        plan = self.plan()
        self.assertEqual(self.classes(plan)[path], 'local-edit')
        self.apply(plan)
        self.assertEqual(self.destination(path).read_bytes(), body)
        self.assert_base(path, self.sources[path])
        for entry in ['AGENTS.md', 'CLAUDE.md']:
            self.assertIn(b'LOCAL_FACT_NOT_UPSTREAM', (self.root / entry).read_bytes())
        self.cli('check')

    def test_02_upstream_and_local_conflict_blocks_all_other_updates(self):
        self.destination('rules/wiki.md').write_bytes(b'LOCAL_WIKI\n')
        self.upstream('rules/wiki.md', b'UPSTREAM_WIKI\n')
        self.upstream('rules/coding.md', b'UNRELATED_UPDATE\n')
        plan = self.plan()
        self.assertFalse(plan['applicable'])
        self.assertIn('rules/wiki.md', [row['canonical'] for row in plan['conflicts']])
        self.reject_apply(plan['plan_sha256'])

    def test_02_missing_tracked_file_blocks_even_optional_source(self):
        self.destination('commands/review.md').unlink()
        self.upstream('rules/coding.md', b'UPDATE_ELSEWHERE\n')
        before = tree(self.root)
        result = subprocess.run([sys.executable, str(SCRIPT), 'plan', '--root', str(self.root),
                                 '--bundle', str(self.bundle)], capture_output=True, text=True)
        if result.returncode == 0:
            plan = json.loads(result.stdout)
            self.assertFalse(plan['applicable'])
            self.reject_apply(plan['plan_sha256'])
        else:
            self.reject_apply('0' * 64)
        self.assertEqual(tree(self.root), before)

    def test_02_unknown_new_destination_is_ownership_conflict(self):
        self.add_upstream('rules/new.md')
        self.put(self.destination('rules/new.md'), b'UNKNOWN_USER_FILE\n')
        plan = self.plan()
        self.assertFalse(plan['applicable'])
        self.assertIn('rules/new.md', [row['canonical'] for row in plan['conflicts']])
        self.reject_apply(plan['plan_sha256'])

    def test_03_explicit_exclusions_preserve_bytes_and_real_bases(self):
        private = 'skills/private/SKILL.md'
        self.put(self.destination(private), b'PRIVATE_SKILL_BODY\n')
        self.destination('rules/wiki.md').write_bytes(b'OWNER_OVERRIDE\n')
        self.upstream('rules/wiki.md', b'UPSTREAM_WIKI\n')
        self.upstream('rules/coding.md', b'UPSTREAM_CODING\n')
        self.add_upstream('agents/skipped.md')
        self.edit_json(self.intent_path, local_only=[private], overrides=['rules/wiki.md'],
                       skip_sync=['rules/coding.md', 'agents/skipped.md'], extension={'keep': True})
        plan = self.plan()
        self.assertNotIn('PRIVATE_SKILL_BODY', json.dumps(plan))
        self.apply(plan)
        self.assertEqual(self.destination(private).read_bytes(), b'PRIVATE_SKILL_BODY\n')
        self.assertEqual(self.destination('rules/wiki.md').read_bytes(), b'OWNER_OVERRIDE\n')
        self.assertEqual(self.destination('rules/coding.md').read_bytes(), self.sources['rules/coding.md'])
        self.assertFalse(self.destination('agents/skipped.md').exists())
        state = self.read_json(self.state_path)
        self.assertIn(private, state['local_files'])
        self.assertNotIn(private, state['source_files'])
        self.assertNotIn('agents/skipped.md', state['source_files'])
        for path in ['rules/coding.md', 'rules/wiki.md']:
            self.assert_base(path, self.sources[path])
        self.assertEqual(self.read_json(self.intent_path)['extension'], {'keep': True})
        for entry in ['AGENTS.md', 'CLAUDE.md']:
            self.assertNotIn(b'PRIVATE_SKILL_BODY', (self.root / entry).read_bytes())
        self.assertIn('private', (self.root / '.AI/START.md').read_text())
        self.cli('check')

    def test_03_clean_override_stays_permanent_and_registered_local_cannot_be_forgotten(self):
        private = 'skills/private/SKILL.md'
        self.put(self.destination(private), b'PRIVATE_BODY\n')
        self.edit_json(self.intent_path, local_only=[private], overrides=['rules/wiki.md'])
        self.upstream('rules/wiki.md', b'UPSTREAM_CHANGED\n')
        self.apply()
        self.assertEqual(self.destination('rules/wiki.md').read_bytes(), self.sources['rules/wiki.md'])
        self.assert_base('rules/wiki.md', self.sources['rules/wiki.md'])
        self.edit_json(self.intent_path, local_only=[])
        before = tree(self.root)
        self.cli('plan', reject=True)
        self.assertEqual(tree(self.root), before)

    def test_03_check_validates_current_intent(self):
        self.edit_json(self.intent_path, overrides=['rules/not-registered.md'])
        before = tree(self.root)
        self.cli('check', reject=True)
        self.assertEqual(tree(self.root), before)

    def test_03_local_only_collision_requires_owner_resolution(self):
        path = 'agents/private.md'
        self.put(self.destination(path), b'LOCAL_ROLE\n')
        self.edit_json(self.intent_path, local_only=[path])
        self.add_upstream(path)
        plan = self.plan()
        self.assertFalse(plan['applicable'])
        self.reject_apply(plan['plan_sha256'])

    def test_03_invalid_intent_exclusions_reject_without_writes(self):
        original = self.intent_path.read_bytes()
        cases = [dict(local_only=['rules/wiki.md'], skip_sync=['rules/wiki.md']),
                 dict(skip_sync=['rules/wiki.md', 'rules/wiki.md']), dict(local_only='../escape'),
                 dict(local_only=['../escape']), dict(overrides=['rules/unknown.md']),
                 dict(skip_sync=['rules/*.md']), dict(schema_version=999)]
        for change in cases:
            with self.subTest(change=change):
                self.intent_path.write_bytes(original)
                self.edit_json(self.intent_path, **change)
                before = tree(self.root)
                self.cli('plan', reject=True)
                self.assertEqual(tree(self.root), before)

    def test_04_digest_required_and_wrong_digest_refused(self):
        self.upstream('rules/coding.md', b'UPDATE\n')
        before = tree(self.root)
        self.cli('apply', reject=True)
        self.assertEqual(tree(self.root), before)
        self.reject_apply('0' * 64)

    def test_04_digest_binds_retained_input_source_config_intent_state_policy(self):
        self.upstream('rules/coding.md', b'UPDATE\n')
        self.remove_upstream('commands/review.md')
        targets = [self.destination('commands/review.md'), self.bundle / 'rules/wiki.md',
                   self.root / '.AI/project.json', self.intent_path, self.state_path,
                   self.root / '.AI/canon/context-policy.json', self.root / '.AI/START.md',
                   self.root / 'AGENTS.md']
        for target in targets:
            with self.subTest(input=target.name):
                plan = self.plan()
                original = target.read_bytes()
                target.write_bytes(original + b'\n')
                self.reject_apply(plan['plan_sha256'])
                target.write_bytes(original)

    def test_04_legitimate_memory_docs_and_project_writes_do_not_stale_digest(self):
        self.upstream('rules/coding.md', b'UPDATE\n')
        plan = self.plan()
        for path in ['.AI/memory/new-fact.md', 'docs/backlog/current-task.md', '.AI/project.md']:
            self.put(self.root / path, b'PRIVATE_LIVE_WRITE\n')
        self.apply(plan)
        for path in ['.AI/memory/new-fact.md', 'docs/backlog/current-task.md', '.AI/project.md']:
            self.assertEqual((self.root / path).read_bytes(), b'PRIVATE_LIVE_WRITE\n')

    def test_07_malformed_metadata_and_native_alias_refused(self):
        original = self.state_path.read_bytes()
        for change in [dict(schema_version=999), dict(layout_version=999), dict(source_files=[]),
                       dict(project_id='bad-id')]:
            with self.subTest(change=change):
                self.state_path.write_bytes(original)
                self.edit_json(self.state_path, **change)
                before = tree(self.root)
                self.cli('plan', reject=True)
                self.assertEqual(tree(self.root), before)
        self.state_path.write_bytes(original)
        link = self.root / '.claude/rules'
        link.unlink()
        outside = self.base / 'outside'
        outside.mkdir()
        link.symlink_to(outside)
        before = tree(self.base)
        self.cli('plan', reject=True)
        self.assertEqual(tree(self.base), before)

    def test_07_unsafe_manifest_collision_and_intermediate_symlink(self):
        original = (self.bundle / 'manifest.yaml').read_bytes()
        for paths in [('../outside.md',), ('/tmp/outside.md',), ('rules/../outside.md',),
                      ('rules/Case.md', 'rules/case.md'), ('rules/caf\u00e9.md', 'rules/cafe\u0301.md')]:
            with self.subTest(paths=paths):
                (self.bundle / 'manifest.yaml').write_bytes(original)
                for path in paths:
                    if path.startswith('rules/') and '..' not in path:
                        self.put(self.bundle / path, b'COLLISION\n')
                manifest = self.bundle / 'manifest.yaml'
                manifest.write_text(manifest.read_text().replace('universal:\n', 'universal:\n' + ''.join('  - ' + p + '\n' for p in paths)))
                before = tree(self.root)
                self.cli('plan', reject=True)
                self.assertEqual(tree(self.root), before)
        (self.bundle / 'manifest.yaml').write_bytes(original)
        self.add_upstream('skills/escaped/SKILL.md')
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / '.AI/skills/escaped').symlink_to(outside)
        before = tree(self.base)
        self.cli('plan', reject=True)
        self.assertEqual(tree(self.base), before)

    def test_07_invalid_source_mode_and_tracked_private_file_refused(self):
        source = self.bundle / 'scripts/helper.py'
        source.chmod(0o4755)
        before = tree(self.root)
        self.cli('plan', reject=True)
        self.assertEqual(tree(self.root), before)
        source.chmod(0o755)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '-f', '.AI/project.md'], check=True)
        before = tree(self.root)
        self.cli('plan', reject=True)
        self.assertEqual(tree(self.root), before)

    def test_08_private_destinations_archive_metadata_and_policy_preserved(self):
        sentinels = ['.AI/project.md', '.AI/memory/MEMORY.md', 'docs/design.md', '.claude/settings.json',
                     '.ai-bootstrap/legacy/old.txt']
        for path in sentinels:
            self.put(self.root / path, b'PRIVATE_PRESERVE\r\n')
        self.edit_json(self.state_path, machine_extension={'installation': 'synthetic'},
                       archive_extension={'receipt': 'synthetic'})
        policy = self.root / '.AI/canon/context-policy.json'
        policy_bytes = policy.read_bytes()
        self.upstream(bootstrap.MANDATORY[0], self.sources[bootstrap.MANDATORY[0]] + b'NEW_REQUIRED_FACT\n')
        self.upstream('scripts/helper.py', b'#!/usr/bin/env python3\nprint("updated")\n')
        self.apply()
        for path in sentinels:
            self.assertEqual((self.root / path).read_bytes(), b'PRIVATE_PRESERVE\r\n')
        self.assertEqual(policy.read_bytes(), policy_bytes)
        state = self.read_json(self.state_path)
        self.assertEqual(state['machine_extension'], {'installation': 'synthetic'})
        self.assertEqual(state['archive_extension'], {'receipt': 'synthetic'})
        self.assertEqual((self.root / 'scripts/helper.py').stat().st_mode & 0o777, 0o755)
        agents = (self.root / 'AGENTS.md').read_bytes()
        self.assertEqual(agents, (self.root / 'CLAUDE.md').read_bytes())
        self.assertIn(b'NEW_REQUIRED_FACT', agents)
        self.assertNotIn(b'PRIVATE_PRESERVE', agents)
        self.assertLessEqual(len(agents), json.loads(policy_bytes)['max_bytes'])
        self.cli('check')

    def test_08_completed_migration_receipt_and_exact_archive_survive_sync(self):
        import test_ai_migrate as migration
        fixture = migration.AiMigrationContract()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.command('apply')
        self.root, self.bundle, self.sources = fixture.root, fixture.bundle, fixture.sources
        self.state_path = self.root / '.AI/canon/canon.state.json'
        self.intent_path = self.root / '.AI/canon/canon.intent.yaml'
        migrated_state = self.read_json(self.state_path)
        self.assertIn('migration', migrated_state, 'black-box migration must create historical receipt')
        receipt = migrated_state['migration']
        archive = self.root / '.ai-bootstrap/legacy'
        archived = tree(archive)
        self.assertFalse((self.root / '.ai-bootstrap/migration.json').exists())
        self.upstream('rules/wiki.md', b'# Wiki\nUPDATE_MIGRATED_CLIENT\n')
        self.apply()
        self.assertEqual(self.read_json(self.state_path)['migration'], receipt)
        self.assertEqual(tree(archive), archived)
        self.assertEqual(self.destination('rules/coding.md').read_bytes(), fixture.legacy_rule)
        self.assertEqual((self.root / '.AI/memory/MEMORY.md').read_bytes(), fixture.legacy_memory)
        self.cli('check')

    def test_09_start_catalog_retains_removed_and_rejects_manual_entries(self):
        self.remove_upstream('commands/review.md')
        self.apply()
        self.assertIn('review.md', (self.root / '.AI/START.md').read_text())
        for path in ['.AI/START.md', 'AGENTS.md', 'CLAUDE.md']:
            with self.subTest(path=path):
                target = self.root / path
                original = target.read_bytes()
                target.write_bytes(original + b'OWNER_MANUAL_EDIT\n')
                before = tree(self.root)
                self.cli('plan', reject=True)
                self.assertEqual(tree(self.root), before)
                target.write_bytes(original)

    def test_09_managed_start_receipt_tracks_generation_not_private_bootstrap_receipts(self):
        private_receipt = self.initial_state['bootstrap_files']['.AI/project.md']
        self.upstream('rules/coding.md', b'NEW_GENERATION\n')
        self.apply()
        state = self.read_json(self.state_path)
        self.assertEqual(state['sync']['schema_version'], 1)
        self.assertEqual(state['sync']['managed_start_sha256'],
                         hashlib.sha256((self.root / '.AI/START.md').read_bytes()).hexdigest())
        self.assertEqual(state['bootstrap_files']['.AI/project.md'], private_receipt)

    def test_09_budget_failure_is_pretransaction(self):
        self.edit_json(self.root / '.AI/canon/context-policy.json', max_bytes=1)
        before = tree(self.root)
        self.cli('plan', reject=True)
        self.assertEqual(tree(self.root), before)


if __name__ == '__main__':
    unittest.main()
