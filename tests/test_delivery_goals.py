# Blind G8 tests for the delivery-goals skill. All state lives in synthetic TMP dirs;
# nothing here simulates a live client, a live Work/_shared or a live setup.
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

import test_ai_bootstrap as bootstrap
import test_ai_sync as sync

SOURCE = pathlib.Path(__file__).resolve().parents[1]
SKILL_PATH = 'skills/delivery-goals/SKILL.md'
SKILL = SOURCE / SKILL_PATH
BOOKKEEPING = ('.AI/canon', '.ai-bootstrap')
START_KEY = '.AI/START.md'
WRITERS = ('new', 'update', 'restore', 'retire')


def cut_comment(text):
    # Drop an inline YAML comment: a # preceded by whitespace.
    for index, char in enumerate(text):
        if char == '#' and index and text[index - 1].isspace():
            return text[:index].rstrip()
    return text


def unquote(text):
    quotes = chr(39) + chr(34)
    if text[:1] and text[0] in quotes:
        end = text.find(text[0], 1)
        if end > 0:
            return text[1:end]
    return cut_comment(text).strip().strip(quotes)


def universal_entries(manifest_text):
    entries = []
    in_universal = False
    for line in manifest_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('-'):
            if in_universal:
                item = unquote(stripped[1:].strip())
                if item:
                    entries.append(item)
        elif not line[0].isspace():
            in_universal = cut_comment(stripped) == 'universal:'
    return entries


def skill_bytes(case):
    # A missing skill must be an AssertionError, never FileNotFoundError or ImportError.
    case.assertTrue(SKILL.is_file(), 'missing Source skill: ' + SKILL_PATH)
    body = SKILL.read_bytes()
    case.assertTrue(body.strip(), 'Source skill is empty')
    return body


def class_map(case, plan):
    # Plan.changes classifies ALL paths (unchanged/local-only/override included); classes(plan) is the public fixture port.
    found = case.classes(plan)
    case.assertIsInstance(found, dict, 'classes(plan) must map path to class')
    return {path: str(kind).lower() for path, kind in found.items()}


def assert_no_writes(case, plan):
    # up-to-date does not mean changes == []; it means no writer class and no conflicts.
    case.assertEqual(plan['status'], 'up-to-date')
    case.assertEqual(plan['conflicts'], [])
    writers = {path: kind for path, kind in class_map(case, plan).items() if kind in WRITERS}
    case.assertEqual(writers, {}, 'unexpected writer classes in an up-to-date plan')


def assert_skill_class(case, plan, expected):
    kinds = class_map(case, plan)
    case.assertIn(SKILL_PATH, kinds)
    case.assertEqual(kinds[SKILL_PATH], expected, SKILL_PATH)


class DeliveryGoalsBootstrap(unittest.TestCase):
    put = bootstrap.AiBootstrapContract.put
    policy = bootstrap.AiBootstrapContract.policy
    command = bootstrap.AiBootstrapContract.command
    classes = sync.SyncFixture.classes

    def setUp(self):
        bootstrap.AiBootstrapContract.setUp(self)
        # The fixture prepared a synthetic bundle; switch to the real Source afterwards.
        self.bundle = SOURCE

    def test_real_manifest_installs_exact_skill_bytes_into_universal_client(self):
        self.command('apply', extra=['--types', 'wiki'])
        # Positive controls: ordinary bootstrap of the real Source works before the goals path is checked.
        for relative in bootstrap.MANDATORY:
            source = SOURCE / relative
            self.assertTrue(source.is_file(), 'positive control: missing Source ' + relative)
            self.assertEqual((self.root / '.AI' / relative).read_bytes(), source.read_bytes(), relative)
        for relative in ('START.md', 'project.md', 'memory/MEMORY.md'):
            self.assertTrue((self.root / '.AI' / relative).is_file(), relative)
        state = json.loads((self.root / '.AI/canon/canon.state.json').read_text())
        self.assertIn('source_files', state)
        self.assertIn(bootstrap.MANDATORY[0], state['source_files'])
        # Target assertions.
        body = skill_bytes(self)
        manifest_file = SOURCE / 'manifest.yaml'
        self.assertTrue(manifest_file.is_file(), 'missing Source manifest.yaml')
        manifest = manifest_file.read_text(encoding='utf-8')
        self.assertEqual(universal_entries(manifest).count(SKILL_PATH), 1)
        installed = self.root / '.AI' / SKILL_PATH
        self.assertTrue(installed.is_file(), 'skill was not installed for a wiki-only client')
        self.assertEqual(installed.read_bytes(), body)
        self.assertIn(SKILL_PATH, state['source_files'])
        self.assertEqual(state['source_files'][SKILL_PATH]['sha256'], hashlib.sha256(body).hexdigest())
        # The first sync adopts bootstrap metadata; no-op applies to a subsequent update.
        first_plan = subprocess.run([sys.executable, str(sync.SCRIPT), 'plan', '--root', str(self.root),
                                     '--bundle', str(SOURCE)], capture_output=True, text=True, timeout=60)
        self.assertEqual(first_plan.returncode, 0, first_plan.stdout + first_plan.stderr)
        first = json.loads(first_plan.stdout)
        self.assertTrue(first['applicable'], first)
        adopted = subprocess.run([sys.executable, str(sync.SCRIPT), 'apply', '--root', str(self.root),
                                  '--bundle', str(SOURCE), '--expect-plan', first['plan_sha256']],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(adopted.returncode, 0, adopted.stdout + adopted.stderr)
        before = sync.tree(self.root)
        result = subprocess.run([sys.executable, str(sync.SCRIPT), 'plan', '--root', str(self.root),
                                 '--bundle', str(SOURCE)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sync.tree(self.root), before)
        plan = json.loads(result.stdout)
        assert_no_writes(self, plan)

    def test_new_client_start_catalog_and_skill_links_reach_installed_skill(self):
        self.command('apply', extra=['--types', 'wiki'])
        start = self.root / '.AI/START.md'
        skills = self.root / '.AI/skills'
        agents = self.root / '.agents/skills'
        # Positive controls for the ordinary bootstrap, START and link machinery.
        for control in (start, self.root / '.AI/project.md', self.root / '.AI/memory/MEMORY.md'):
            self.assertTrue(control.is_file(), str(control))
        self.assertTrue(start.read_text(encoding='utf-8').strip())
        self.assertTrue(skills.is_dir())
        self.assertTrue(agents.is_symlink())
        self.assertEqual(os.readlink(agents), '../.AI/skills')
        self.assertEqual(agents.resolve(), skills.resolve())
        # Target assertions for the goals skill.
        body = skill_bytes(self)
        self.assertIn(SKILL_PATH, start.read_text(encoding='utf-8'))
        self.assertEqual((skills / 'delivery-goals/SKILL.md').read_bytes(), body)
        target = skills.resolve()
        links = [key for key, value in bootstrap.snapshot(self.root).items()
                 if value[0] == 'link' and (self.root / key).resolve() == target]
        self.assertIn('.agents/skills', links)
        for key in links:
            self.assertEqual((self.root / key / 'delivery-goals/SKILL.md').read_bytes(), body, key)


class DeliveryGoalsSync(sync.SyncFixture):
    def setUp(self):
        super().setUp()
        # Adopt bootstrap metadata before testing an already-synchronised client.
        self.apply()

    def test_existing_client_receives_skill_via_sync_and_keeps_local_state(self):
        project = self.root / '.AI/project.md'
        memory = self.root / '.AI/memory/MEMORY.md'
        note = self.root / '.AI/memory/local-note.md'
        project.write_bytes(project.read_bytes() + b'LOCAL_IDENTITY_EDIT')
        memory.write_bytes(memory.read_bytes() + b'LOCAL_MEMORY_EDIT')
        self.put(note, 'LOCAL_NOTE')
        local = {path: path.read_bytes() for path in (project, memory, note)}
        intent = self.intent_path.read_bytes()
        # Positive controls: the old-style client syncs cleanly and has no goals skill yet.
        assert_no_writes(self, self.plan())
        self.assertTrue(self.destination('rules/secrets-handling.md').is_file())
        self.assertFalse(self.destination(SKILL_PATH).exists())
        start = self.destination('START.md')
        self.assertTrue(start.is_file())
        self.assertNotIn(SKILL_PATH, start.read_text(encoding='utf-8'))
        # Target: the real skill bytes arrive through the universal manifest list.
        body = skill_bytes(self)
        self.add_upstream(SKILL_PATH, body)
        before = sync.tree(self.root)
        plan = self.plan()
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['conflicts'], [])
        assert_skill_class(self, plan, 'new')
        self.apply(plan)
        destination = self.destination(SKILL_PATH)
        self.assertEqual(destination.read_bytes(), body)
        self.assert_base(SKILL_PATH, body)
        # START is generated from the effective manifest/sources, so it is the one allowed change.
        self.assertIn(SKILL_PATH, start.read_text(encoding='utf-8'))
        after = sync.tree(self.root)
        for key, value in before.items():
            if key == START_KEY or key.startswith(BOOKKEEPING):
                continue
            self.assertEqual(after.get(key), value, key)
        for key in after:
            if key not in before and not key.startswith(BOOKKEEPING):
                self.assertTrue(key.endswith('delivery-goals') or key.endswith('delivery-goals/SKILL.md'), key)
        for path, content in local.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual(self.intent_path.read_bytes(), intent)
        assert_no_writes(self, self.plan())
        self.cli('check')

    def test_optional_goals_alias_follows_synced_skill_without_touching_source(self):
        # The alias lives only in this test's own TMP; it is not a live Work/_shared.
        shared = self.base / 'Work/_shared'
        shared.mkdir(parents=True)
        rule = self.destination('rules/secrets-handling.md')
        control = shared / 'CONTROL.md'
        control.symlink_to(rule)
        # Positive controls: alias mechanics and a clean plan work before the skill is involved.
        self.assertTrue(control.is_symlink())
        self.assertEqual(control.read_bytes(), rule.read_bytes())
        assert_no_writes(self, self.plan())
        # Target: install the skill, point the alias at it, then update it through sync.
        original = skill_bytes(self)
        self.add_upstream(SKILL_PATH, original)
        self.apply()
        destination = self.destination(SKILL_PATH)
        self.assertEqual(destination.read_bytes(), original)
        alias = shared / 'GOALS.md'
        alias.symlink_to(destination)
        link_target = os.readlink(alias)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.read_bytes(), original)
        updated = original + b'<!-- synthetic update for the alias test -->'
        self.upstream(SKILL_PATH, updated)
        plan = self.plan()
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['conflicts'], [])
        assert_skill_class(self, plan, 'update')
        self.apply(plan)
        self.assertTrue(alias.is_symlink(), 'sync replaced the alias with a regular file')
        self.assertEqual(os.readlink(alias), link_target)
        self.assertEqual(alias.resolve(), destination.resolve())
        self.assertEqual(alias.read_bytes(), updated)
        self.assertEqual(destination.read_bytes(), updated)
        self.assert_base(SKILL_PATH, updated)
        self.assertEqual(SKILL.read_bytes(), original, 'original Source skill was modified')
        self.assertNotIn(SOURCE, destination.resolve().parents)


if __name__ == '__main__':
    unittest.main()
