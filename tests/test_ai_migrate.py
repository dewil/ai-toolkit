"""Independent CLI acceptance tests for legacy-to-.AI migration (FR-AIM)."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_ai_bootstrap as bootstrap

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ai-migrate.py'


def blob_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class AiMigrationContract(unittest.TestCase):
    put = bootstrap.AiBootstrapContract.put
    policy = bootstrap.AiBootstrapContract.policy

    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'ai-migrate implementation is required')
        bootstrap.AiBootstrapContract.setUp(self)
        self.legacy_rule = b'# Local coding rule\r\nPRESERVED_LOCAL_RULE\r\n'
        self.legacy_memory = b'# Client memory\r\nPRIVATE_MEMORY_RECORD\r\n'
        self.legacy_context = (b'# Client instructions\nPRIVATE_PROJECT_RECORD\n'
                               b'@.claude/rules/coding.md\n@.claude/agents/reviewer.md\n'
                               b'Tasks: docs/dev/backlog/; finished: docs/dev/done/.\n')
        for source, data in self.sources.items():
            target = source if source.startswith('scripts/') else '.claude/' + source
            self.put(self.root / target, data)
        self.put(self.root / '.claude/rules/coding.md', self.legacy_rule)
        self.put(self.root / '.claude/memory/MEMORY.md', self.legacy_memory)
        self.put(self.root / '.claude/memory/nested/fact.md', b'nested memory\x00\xff')
        self.put(self.root / '.claude/rules/local-only.md', b'# Local-only rule\n')
        self.put(self.root / '.claude/skills/private/SKILL.md', b'# Private skill\n')
        self.put(self.root / '.claude/agents/private.md', b'# Private role\n')
        self.put(self.root / '.claude/commands/private.md', b'# Private command\n')
        self.put(self.root / '.claude/settings.local.json', '{"custom":true}\n')
        self.put(self.root / '.claude/local-notes.txt', 'local CLI data\n')
        self.put(self.root / 'CLAUDE.md', self.legacy_context)
        self.put(self.root / 'AGENTS.md', b'Prior agent instructions\n')
        self.put(self.root / 'README.md', 'User README\n')
        self.put(self.root / 'docs/dev/backlog/task.md', 'Task ID: TASK-17\n')
        self.put(self.root / 'docs/dev/done/old.md', 'Completed TASK-16\n')
        self.put(self.root / 'docs/dev/design.md', 'Existing design\n')
        self.put(self.root / 'docs/unrelated.md', 'Unrelated document\n')
        self.put(self.root / 'subproject/.git/HEAD', 'ref: refs/heads/main\n')
        self.put(self.root / 'subproject/code.txt', 'Nested project bytes\n')
        self.put(self.root / '.gitignore', '# user rule\nuser-cache/\n')
        self.write_registry()
        self.original = bootstrap.snapshot(self.root)

    def write_registry(self):
        registry = 'canon_base: https://raw.githubusercontent.com/example/toolkit/main\n'
        registry += 'project_type: coding\nfiles:\n'
        registry += ''.join('  - ' + path + '\n' for path in self.sources)
        registry += 'file_hashes:\n'
        registry += ''.join('  ' + path + ': ' + blob_sha(data) + '\n'
                            for path, data in self.sources.items())
        self.put(self.root / '.claude/canon.yaml', registry)

    def command(self, verb, extra=(), expected=0):
        args = [sys.executable, str(SCRIPT), verb, '--root', str(self.root)]
        if verb in ('plan', 'apply'):
            args += ['--bundle', str(self.bundle), '--project-id', bootstrap.PROJECT_ID,
                     '--types', 'coding,wiki', '--adapters', 'claude,codex,kimi']
        result = subprocess.run(args + list(extra), capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def reject_unchanged(self, extra=()):
        before = bootstrap.snapshot(self.root)
        self.command('apply', extra, expected=2)
        self.assertEqual(bootstrap.snapshot(self.root), before)

    def test_01_plan_is_read_only_and_reports_json(self):
        before = bootstrap.snapshot(self.base)
        self.assertIsInstance(json.loads(self.command('plan').stdout), dict)
        self.assertEqual(bootstrap.snapshot(self.base), before)
        self.root = self.base / 'absent'
        result = subprocess.run([sys.executable, str(SCRIPT), 'plan', '--root', str(self.root),
                                 '--bundle', str(self.bundle), '--project-id', bootstrap.PROJECT_ID,
                                 '--types', 'coding,wiki'], capture_output=True, text=True)
        self.assertIn(result.returncode, (0, 2), result.stdout + result.stderr)
        self.assertFalse(self.root.exists())

    def test_01_apply_preserves_memory_local_content_and_aliases(self):
        self.command('apply')
        self.assertEqual((self.root / '.AI/memory/MEMORY.md').read_bytes(), self.legacy_memory)
        self.assertEqual((self.root / '.AI/memory/nested/fact.md').read_bytes(), b'nested memory\x00\xff')
        self.assertEqual((self.root / '.AI/rules/coding.md').read_bytes(), self.legacy_rule)
        for source, destination in [('rules/local-only.md', 'rules/local-only.md'),
                                    ('skills/private/SKILL.md', 'skills/private/SKILL.md'),
                                    ('agents/private.md', 'roles/private.md'),
                                    ('commands/private.md', 'commands/private.md')]:
            self.assertEqual((self.root / '.AI' / destination).read_bytes(),
                             self.original['.claude/' + source][1])
        for alias, target in [('rules', 'rules'), ('skills', 'skills'), ('agents', 'roles'),
                              ('commands', 'commands'), ('memory', 'memory')]:
            self.assertTrue((self.root / '.claude' / alias).is_symlink())
            self.assertEqual((self.root / '.claude' / alias).resolve(), (self.root / '.AI' / target).resolve())
        self.assertEqual((self.root / '.agents/skills').resolve(), (self.root / '.AI/skills').resolve())
        self.command('check')

    def test_02_backup_exact_and_unrelated_documents_unchanged(self):
        self.command('apply')
        backup = self.root / '.ai-bootstrap/legacy'
        self.assertTrue(backup.is_dir())
        archived = [p.read_bytes() for p in backup.rglob('*') if p.is_file() and not p.is_symlink()]
        for path, descriptor in self.original.items():
            if descriptor[0] == 'file' and (path.startswith('.claude/') or path.startswith('docs/dev/')
                                             or path in ('CLAUDE.md', 'AGENTS.md')):
                self.assertIn(descriptor[1], archived, 'Missing backup bytes for ' + path)
        for path in ['README.md', 'docs/unrelated.md', 'subproject/.git/HEAD', 'subproject/code.txt',
                     '.claude/settings.local.json', '.claude/local-notes.txt']:
            self.assertEqual((self.root / path).read_bytes(), self.original[path][1])
        self.assertFalse((self.root / '.claude/canon.yaml').exists())

    def test_02_project_routes_rewritten_and_private_text_not_in_entries(self):
        self.command('apply')
        context = (self.root / '.AI/project.md').read_text()
        for expected in ['PRIVATE_PROJECT_RECORD', '.AI/rules/coding.md', '.AI/roles/reviewer.md',
                         'docs/backlog/', 'docs/done/']:
            self.assertIn(expected, context)
        self.assertNotIn('.claude/rules/', context)
        self.assertNotIn('docs/dev/', context)
        for name in ['AGENTS.md', 'CLAUDE.md']:
            entry = (self.root / name).read_text()
            self.assertNotIn('PRIVATE_PROJECT_RECORD', entry)
            self.assertNotIn('PRIVATE_MEMORY_RECORD', entry)

    def test_02_clean_known_source_updates_but_local_change_keeps_upstream_base(self):
        source = 'rules/wiki.md'
        upstream = b'# New upstream wiki rule\n'
        self.put(self.bundle / source, upstream)
        self.command('apply')
        self.assertEqual((self.root / '.AI' / source).read_bytes(), upstream)
        self.assertEqual((self.root / '.AI/rules/coding.md').read_bytes(), self.legacy_rule)
        state = json.loads((self.root / '.AI/canon/canon.state.json').read_text())
        self.assertEqual(state['source_files']['rules/coding.md']['blob_sha'], blob_sha(self.sources['rules/coding.md']))
        self.assertNotIn('rules/local-only.md', state['source_files'])
        self.assertIn('local-only.md', json.dumps(state))

    def test_03_invalid_configuration_preserves_entire_legacy_tree(self):
        for extra in [('--types', 'nonexistent'), ('--adapters', 'deepseek'), ('--project-id', 'bad')]:
            with self.subTest(extra=extra):
                self.reject_unchanged(extra)

    def test_03_missing_upstream_source_or_unknown_registry_refused(self):
        missing = self.bundle / bootstrap.MANDATORY[0]
        missing.unlink()
        self.reject_unchanged()
        self.put(missing, self.sources[bootstrap.MANDATORY[0]])
        self.put(self.root / '.claude/canon.yaml', '{"schema_version":999,"files":[]}')
        self.reject_unchanged()

    def test_03_symlink_and_case_collision_refused_before_archive(self):
        outside = self.base / 'outside.txt'
        outside.write_text('Outside content')
        link = self.root / '.claude/rules/escape.md'
        link.symlink_to(outside)
        self.reject_unchanged()
        self.assertEqual(outside.read_text(), 'Outside content')
        link.unlink()
        self.put(self.root / '.claude/rules/LOCAL-ONLY.md', 'Case collision')
        self.reject_unchanged()

    def test_04_repeat_is_noop_and_configuration_change_refused(self):
        self.command('apply')
        before = bootstrap.snapshot(self.root)
        self.command('apply')
        self.assertEqual(bootstrap.snapshot(self.root), before)
        self.reject_unchanged(['--types', 'wiki'])
        self.reject_unchanged(['--project-id', '5c81d47f-17af-4799-a84f-f01df470d80f'])

    def test_04_bootstrap_build_accepts_migrated_state_and_local_rule_edit(self):
        self.command('apply')
        source = self.root / '.AI' / bootstrap.MANDATORY[0]
        source.write_text(source.read_text() + '\nAFTER_MIGRATION_RULE_EDIT\n')
        result = subprocess.run([sys.executable, str(bootstrap.SCRIPT), 'build', '--root', str(self.root)],
                                 capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for entry in ['AGENTS.md', 'CLAUDE.md']:
            self.assertIn('AFTER_MIGRATION_RULE_EDIT', (self.root / entry).read_text())
        self.command('check')

    def test_06_docs_flatten_preserves_tasks_and_refuses_destination_collision(self):
        self.put(self.root / 'docs/design.md', 'Conflicting destination')
        self.reject_unchanged()
        (self.root / 'docs/design.md').unlink()
        self.command('apply')
        for path in ['backlog/task.md', 'done/old.md', 'design.md']:
            self.assertEqual((self.root / 'docs' / path).read_bytes(), self.original['docs/dev/' + path][1])
        self.assertFalse((self.root / 'docs/dev').exists())
        self.assertIn('# user rule\nuser-cache/\n', (self.root / '.gitignore').read_text())


if __name__ == '__main__':
    unittest.main()
