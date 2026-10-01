"""Independent integrity regressions for FR-AIB-03/06/07."""
import json
import re
import subprocess
import sys
import unittest

from test_ai_bootstrap import AiBootstrapContract, SCRIPT, snapshot


class AiBootstrapIntegrity(unittest.TestCase):
    setUp = AiBootstrapContract.setUp
    put = AiBootstrapContract.put
    policy = AiBootstrapContract.policy
    command = AiBootstrapContract.command

    def test_removed_ignore_protection_is_not_healthy(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.command('apply')
        private = ['.AI/project.md', '.AI/memory/MEMORY.md',
                   '.AI/canon/canon.state.json', '.ai-bootstrap/lock',
                   '.claude/settings.local.json']
        for path in private:
            result = subprocess.run(['git', '-C', str(self.root), 'check-ignore', path],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, path)
        (self.root / '.gitignore').write_text('# user rules\n')
        before = snapshot(self.root)
        self.command('check', expected=1)
        self.assertEqual(snapshot(self.root), before)

    def test_each_missing_privacy_boundary_is_detected(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.command('apply')
        ignore = self.root / '.gitignore'
        original = ignore.read_text()
        for rule in ('/.AI/memory/', '/.AI/project.md', '/.AI/canon/',
                     '/.ai-bootstrap/', '/.claude/settings.local.json'):
            with self.subTest(rule=rule):
                ignore.write_text(original.replace(rule + '\n', ''))
                self.command('check', expected=1)
        ignore.write_text(original)
        self.command('check')

    def test_required_local_infrastructure_must_exist(self):
        self.command('apply')
        for relative in ('.AI/project.md', '.AI/memory/MEMORY.md',
                         '.AI/canon/canon.intent.yaml', 'docs/dev/backlog', 'docs/dev/done'):
            with self.subTest(path=relative):
                path = self.root / relative
                saved = self.base / 'saved'
                path.rename(saved)
                try:
                    result = subprocess.run([sys.executable, str(SCRIPT),
                                             'check', '--root', str(self.root)], capture_output=True)
                    self.assertNotEqual(result.returncode, 0, relative)
                finally:
                    saved.rename(path)
        self.command('check')

    def test_editable_private_content_stays_local_and_survives_repeat(self):
        self.command('apply')
        state_path = self.root / '.AI/canon/canon.state.json'
        before_state = json.loads(state_path.read_text())
        for rel in ('.AI/project.md', '.AI/memory/MEMORY.md'):
            (self.root / rel).write_text('PRIVATE LOCAL EDIT ' + rel + '\n')
        self.command('check')
        before = snapshot(self.root)
        self.assertEqual(json.loads(self.command('apply').stdout)['status'], 'up-to-date')
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(json.loads(state_path.read_text())['source_files'], before_state['source_files'])
        for name in ('AGENTS.md', 'CLAUDE.md'):
            self.assertNotIn('PRIVATE LOCAL EDIT', (self.root / name).read_text())

    def test_existing_casefold_instruction_name_preserved(self):
        (self.root / 'agents.md').write_text('User instructions\n')
        for verb in ('plan', 'apply'):
            with self.subTest(verb=verb):
                before = snapshot(self.root)
                self.command(verb, expected=2)
                self.assertEqual(snapshot(self.root), before)

    def test_parent_case_collision_rejected_before_any_write(self):
        manifest = self.bundle / 'manifest.yaml'
        manifest.write_text(manifest.read_text().replace(
            'universal:\n', 'universal:\n  - rules/Foo/a.md\n  - rules/foo/b.md\n'))
        self.put(self.bundle / 'rules/Foo/a.md', 'a\n')
        self.put(self.bundle / 'rules/foo/b.md', 'b\n')
        for verb in ('plan', 'apply'):
            with self.subTest(verb=verb):
                before = snapshot(self.root)
                self.command(verb, expected=2)
                self.assertEqual(snapshot(self.root), before)
                self.assertFalse((self.root / '.ai-bootstrap').exists())

    def test_real_bundle_start_links_resolve(self):
        repository = SCRIPT.parent.parent
        self.bundle = repository
        self.command('apply')
        start = self.root / '.AI/START.md'
        links = re.findall(r'\[[^\]]+\]\(([^)]+)\)', start.read_text())
        self.assertTrue(links, 'START must catalogue installed canon')
        for target in links:
            if '://' not in target and not target.startswith('#'):
                with self.subTest(target=target):
                    self.assertTrue((start.parent / target).exists(), target)
        installed = json.loads((self.root / '.AI/canon/context-policy.json').read_text())
        template = json.loads((repository / 'templates/ai/context-policy.json').read_text())
        self.assertEqual(installed, template)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(AiBootstrapIntegrity)


if __name__ == '__main__':
    unittest.main()
