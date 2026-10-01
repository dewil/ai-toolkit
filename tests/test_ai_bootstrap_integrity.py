"""Independent integrity regressions for FR-AIB-03/06/07."""
import base64
import hashlib
import json
import os
import stat
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

    def test_corrupt_git_index_cannot_bypass_tracked_private_file_refusal(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        private = self.root / '.AI/project.md'
        self.put(private, 'Private project already in Git index\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '-f', '.AI/project.md'], check=True)
        tracked = subprocess.run(['git', '-C', str(self.root), 'ls-files', '--', '.AI/project.md'],
                                 capture_output=True, text=True, check=True)
        self.assertEqual(tracked.stdout.strip(), '.AI/project.md')
        private.unlink()
        private.parent.rmdir()
        (self.root / '.git/index').write_bytes(b'corrupt index must fail closed\n')
        before = snapshot(self.root)
        result = subprocess.run([sys.executable, str(SCRIPT), 'apply', '--root', str(self.root),
                                 '--bundle', str(self.bundle), '--project-id',
                                 '22aaab19-84df-42b0-9f1a-51aa4fbb3425', '--types', 'coding,wiki'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(result.stderr.strip(), 'Git failure must have a diagnostic')
        self.assertEqual(snapshot(self.root), before)

    def test_corrupt_git_index_makes_installed_project_check_fail_without_writes(self):
        self.command('apply')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.git/index').write_bytes(b'corrupt installed project index\n')
        before = snapshot(self.root)
        result = subprocess.run([sys.executable, str(SCRIPT), 'check', '--root', str(self.root)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stderr.strip(), 'Git failure must have a diagnostic')
        self.assertEqual(snapshot(self.root), before)

    def test_invalid_git_metadata_is_not_treated_as_a_non_git_project(self):
        original_root = self.root
        for corruption in ('invalid-gitdir', 'invalid-config'):
            with self.subTest(corruption=corruption):
                self.root = self.base / corruption
                self.root.mkdir()
                if corruption == 'invalid-gitdir':
                    self.put(self.root / '.git', 'gitdir: ' + str(self.base / 'missing-git-dir') + '\n')
                else:
                    subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
                    (self.root / '.git/config').write_text('[unterminated section\n')
                before = snapshot(self.root)
                result = self.command('apply', expected=2)
                self.assertTrue(result.stderr.strip(), 'Git failure must have a diagnostic')
                self.assertEqual(snapshot(self.root), before)
        self.root = original_root

    def test_corrupt_git_head_cannot_bypass_root_or_parent_tracked_private_file(self):
        for location in ('root', 'parent'):
            with self.subTest(repository=location):
                repository = self.base / ('corrupt-head-' + location)
                repository.mkdir()
                self.root = repository if location == 'root' else repository / 'nested-client'
                self.root.mkdir(exist_ok=True)
                subprocess.run(['git', 'init', '-q', str(repository)], check=True)
                private = self.root / '.AI/project.md'
                self.put(private, 'Tracked private project with corrupt repository HEAD\n')
                tracked_path = private.relative_to(repository).as_posix()
                subprocess.run(['git', '-C', str(repository), 'add', '-f', tracked_path], check=True)
                tracked = subprocess.run(['git', '-C', str(repository), 'ls-files', '--', tracked_path],
                                         capture_output=True, text=True, check=True)
                self.assertEqual(tracked.stdout.strip(), tracked_path)
                private.unlink()
                private.parent.rmdir()
                (repository / '.git/HEAD').write_bytes(b'malformed HEAD metadata\n')
                before = snapshot(repository)
                result = self.command('apply', expected=2)
                self.assertTrue(result.stderr.strip(), 'Malformed Git HEAD needs a diagnostic')
                self.assertEqual(snapshot(repository), before)

    def test_real_parent_git_metadata_is_checked_through_intermediate_symlink(self):
        repository = self.base / 'physical-repository'
        repository.mkdir()
        physical_container = repository / 'container'
        physical_container.mkdir()
        physical_root = physical_container / 'nested-client'
        physical_root.mkdir()
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        private = physical_root / '.AI/project.md'
        self.put(private, 'Tracked private project behind an intermediate symlink\n')
        subprocess.run(['git', '-C', str(repository), 'add', '-f', 'container/nested-client/.AI/project.md'], check=True)
        private.unlink()
        private.parent.rmdir()
        (repository / '.git/HEAD').write_bytes(b'malformed HEAD metadata\n')
        alias = self.base / 'repository-alias'
        alias.symlink_to(physical_container, target_is_directory=True)
        self.root = alias / 'nested-client'
        self.assertFalse(self.root.is_symlink(), 'Only the intermediate parent is a symlink')
        before = snapshot(repository)
        result = self.command('apply', expected=2)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(snapshot(repository), before)

    def test_missing_git_binary_refuses_known_repository_but_allows_non_git_root(self):
        empty_bin = self.base / 'empty-path'
        empty_bin.mkdir()
        environment = dict(os.environ, PATH=str(empty_bin))
        for known_repository in (True, False):
            with self.subTest(known_repository=known_repository):
                self.root = self.base / ('known-repository' if known_repository else 'plain-directory')
                self.root.mkdir()
                if known_repository:
                    subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
                    private = self.root / '.AI/project.md'
                    self.put(private, 'Tracked private project requiring Git verification\n')
                    subprocess.run(['git', '-C', str(self.root), 'add', '-f', '.AI/project.md'], check=True)
                    private.unlink()
                    private.parent.rmdir()
                before = snapshot(self.root)
                result = subprocess.run([sys.executable, str(SCRIPT), 'apply', '--root', str(self.root),
                                         '--bundle', str(self.bundle), '--project-id',
                                         '22aaab19-84df-42b0-9f1a-51aa4fbb3425', '--types', 'coding,wiki'],
                                        capture_output=True, text=True, env=environment)
                if known_repository:
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertTrue(result.stderr.strip())
                    self.assertEqual(snapshot(self.root), before)
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue((self.root / '.AI/project.md').is_file())

    def test_missing_git_with_explicit_external_git_environment_refuses_install(self):
        empty_bin = self.base / 'empty-environment-path'
        empty_bin.mkdir()
        external = self.base / 'external-git-context'
        external.mkdir()
        for variable in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
            with self.subTest(variable=variable):
                self.root = self.base / ('plain-' + variable.lower())
                self.root.mkdir()
                environment = dict(os.environ, PATH=str(empty_bin))
                for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
                    environment.pop(name, None)
                environment[variable] = str(external / variable.lower())
                before = snapshot(self.base)
                result = subprocess.run([sys.executable, str(SCRIPT), 'apply', '--root', str(self.root),
                                         '--bundle', str(self.bundle), '--project-id',
                                         '22aaab19-84df-42b0-9f1a-51aa4fbb3425', '--types', 'coding,wiki'],
                                        capture_output=True, text=True, env=environment)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(snapshot(self.base), before)

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
                         '.AI/canon/canon.intent.yaml', 'docs/backlog', 'docs/done'):
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

    def test_malformed_recovery_metadata_refused_before_any_write(self):
        self.command('apply')
        state_path = self.root / '.AI/canon/canon.state.json'
        original_state = state_path.read_bytes()
        project_path = self.root / '.AI/project.json'
        original_project = project_path.read_bytes()
        output_path = self.root / 'AGENTS.md'
        original_output = output_path.read_bytes()
        journal_path = self.root / '.ai-bootstrap/journal.json'

        def action(path, data):
            existing = self.root / path
            return {'path': path,
                    'before': {'kind': 'file', 'sha256': hashlib.sha256(existing.read_bytes()).hexdigest(),
                               'mode': stat.S_IMODE(existing.stat().st_mode)},
                    'after': {'kind': 'file', 'sha256': hashlib.sha256(data).hexdigest(), 'mode': 0o644},
                    'data': base64.b64encode(data).decode()}

        for corruption in ('future-context', 'future-source-hash', 'current-project-version'):
            with self.subTest(corruption=corruption):
                state_path.write_bytes(original_state)
                project_path.write_bytes(original_project)
                output_path.write_bytes(original_output)
                future = json.loads(original_state)
                replacement = b'planned replacement must not be written\n'
                future['context']['outputs']['AGENTS.md'] = hashlib.sha256(replacement).hexdigest()
                if corruption == 'future-context':
                    future['context'] = {}
                elif corruption == 'future-source-hash':
                    next(iter(future['source_files'].values()))['blob_sha'] = 'not-a-blob'
                else:
                    project = json.loads(original_project)
                    project['schema_version'] = 999
                    project_path.write_text(json.dumps(project))
                journal = {'schema_version': 2, 'layout_version': 1, 'actions': [
                    action('AGENTS.md', replacement),
                    action('.AI/canon/canon.state.json', json.dumps(future).encode())]}
                journal_path.write_text(json.dumps(journal))
                before = snapshot(self.root)
                self.command('recover', expected=2)
                self.assertEqual(snapshot(self.root), before,
                                 'Malformed metadata must be rejected before output/state replacements')

    def test_unknown_context_policy_version_rejected_before_install(self):
        policy = self.bundle / 'templates/ai/context-policy.json'
        data = json.loads(policy.read_text())
        data['schema_version'] = 999
        policy.write_text(json.dumps(data))
        for command in ('plan', 'apply'):
            with self.subTest(command=command):
                before = snapshot(self.root)
                self.command(command, expected=2)
                self.assertEqual(snapshot(self.root), before)

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
