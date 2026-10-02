"""Independent FR-MTE02..04 black-box tests written before the wrapper."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import unittest

import test_ai_bootstrap as bootstrap
import test_ai_migrate as migration
import test_ai_migrate_recovery as recovery

SCRIPT = migration.SCRIPT.with_name('ai-migrate-terminal.py')
TOOLS = ('ai-bootstrap.py', 'ai-migrate.py', 'ai-migrate-terminal.py')


def complete_snapshot(root):
    """Include permissions and lock paths too: prepare must make zero client writes."""
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        name = str(path.relative_to(root))
        stat = path.lstat()
        result[name] = (stat.st_mode, os.readlink(path) if path.is_symlink() else
                        (path.read_bytes() if path.is_file() else None))
    return result


class MigrationTerminalContract(unittest.TestCase):
    setUp = migration.AiMigrationContract.setUp
    put = migration.AiMigrationContract.put
    policy = migration.AiMigrationContract.policy
    write_registry = migration.AiMigrationContract.write_registry
    command = migration.AiMigrationContract.command

    def invoke(self, args, expected=0, script=SCRIPT, env=None):
        self.assertTrue(script.is_file(), 'FR-MTE02 requires the terminal handoff CLI')
        result = subprocess.run([sys.executable, str(script), *map(str, args)],
                                capture_output=True, text=True, timeout=30, env=env)
        if expected == 0:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('PRIVATE_MEMORY_RECORD', result.stdout + result.stderr)
        self.assertNotIn('PRIVATE_PROJECT_RECORD', result.stdout + result.stderr)
        return result

    def prepare(self, operation='plan', handoff=None, extra=(), expected=0):
        handoff = handoff or self.base / ('handoff ' + operation)
        before = complete_snapshot(self.root)
        result = self.invoke(['prepare', '--root', self.root, '--bundle', self.bundle,
                              '--project-id', bootstrap.PROJECT_ID, '--types', 'coding,wiki',
                              '--adapters', 'claude,codex,kimi', '--handoff-dir', handoff,
                              '--operation', operation, *extra], expected)
        self.assertEqual(complete_snapshot(self.root), before, 'prepare wrote client metadata/content/mode')
        return handoff, result

    def run_request(self, handoff, operation, expected=0, env=None):
        return self.invoke([operation, '--request', handoff / 'request.json'], expected,
                           script=handoff / 'tools/ai-migrate-terminal.py', env=env)

    def assert_receipt(self, handoff, operation):
        receipt = json.loads((handoff / 'result.json').read_text())
        self.assertEqual(receipt['root'], str(self.root.resolve()))
        self.assertEqual(receipt['project_id'], bootstrap.PROJECT_ID)
        self.assertEqual(receipt['operation'], operation)
        self.assertEqual(receipt['request_sha256'], hashlib.sha256((handoff / 'request.json').read_bytes()).hexdigest())
        self.assertIn('status', receipt)
        self.assertIn('check', receipt)
        self.assertEqual(receipt['exit_code'], 0)
        if operation == 'plan':
            self.assertEqual(receipt['status'], 'planned')
            self.assertIsNone(receipt['check'])
        else:
            self.assertEqual(receipt['check'], 'ok')
            self.assertIn(receipt['status'], {'apply': ['migrated', 'up-to-date'],
                                             'check': ['ok'], 'recover': ['recovered']}[operation])
        return receipt

    def test_prepare_readonly_three_matching_tools_and_one_quoted_command(self):
        moved = self.base / "client ' quoted space $ literal"
        self.root.rename(moved)
        self.root = moved
        handoff, result = self.prepare(handoff=self.base / "handoff ' quoted space")
        request = handoff / 'request.json'
        self.assertTrue(request.is_file())
        self.assertTrue((handoff / 'run.sh').is_file())
        saved_request = request.read_text()
        data = json.loads(saved_request)
        self.assertEqual(set(data), {'schema_version', 'root', 'project_id', 'operation',
                                     'project_type', 'adapters', 'source', 'tools_sha256'})
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['root'], str(self.root.resolve()))
        self.assertEqual(data['project_id'], bootstrap.PROJECT_ID)
        self.assertEqual(data['operation'], 'plan')
        self.assertEqual(data['project_type'], ['coding', 'wiki'])
        self.assertEqual(data['adapters'], ['claude', 'codex', 'kimi'])
        self.assertEqual(data['source'], {'kind': 'bundle', 'path': str(self.bundle.resolve())})
        self.assertEqual(set(data['tools_sha256']), set(TOOLS))
        for name in TOOLS:
            retained = handoff / 'tools' / name
            self.assertEqual(retained.read_bytes(), (SCRIPT.parent / name).read_bytes())
            self.assertIn(hashlib.sha256(retained.read_bytes()).hexdigest(), saved_request)
        self.assertRegex(result.stdout.lower(), r'terminal|терминал')
        self.assertEqual(len(re.findall(r'\bbash\s', result.stdout)), 1)
        command = re.search(r'\bbash\s+[^\n]+', result.stdout).group(0)
        self.assertEqual(shlex.split(command), ['bash', str(handoff / 'run.sh')])
        self.assertNotIn('"schema', result.stdout)
        self.assertNotIn('base64', result.stdout.lower())
        before = complete_snapshot(self.root)
        run = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(complete_snapshot(self.root), before, 'plan-only handoff applied migration')
        self.assertNotRegex(run.stdout.lstrip(), r'^\{')
        self.assert_receipt(handoff, 'plan')

    def test_prepare_does_not_inventory_invalid_legacy(self):
        self.put(self.root / '.claude/canon.yaml', 'INVALID INVENTORY')
        self.prepare()

    def test_prepare_pinned_http_is_readonly_and_does_not_fetch_source(self):
        before = complete_snapshot(self.root)
        handoff = self.base / 'http-handoff'
        self.invoke(['prepare', '--root', self.root, '--source-base',
                     'https://raw.githubusercontent.com/example/toolkit/' + 'a' * 40,
                     '--project-id', bootstrap.PROJECT_ID, '--types', 'coding,wiki',
                     '--adapters', 'claude,codex,kimi', '--handoff-dir', handoff,
                     '--operation', 'plan'], env=self.network_denied_environment())
        self.assertEqual(complete_snapshot(self.root), before)
        source = json.loads((handoff / 'request.json').read_text())['source']
        self.assertEqual(source, {'kind': 'http', 'base':
                                 'https://raw.githubusercontent.com/example/toolkit/' + 'a' * 40})

    def network_denied_environment(self):
        guard = self.base / 'network-denied'
        self.put(guard / 'sitecustomize.py',
                 'import socket\n'
                 'def deny(*args, **kwargs):\n'
                 '    raise RuntimeError("FR-MTE04 attempted network access")\n'
                 'socket.create_connection = deny\n'
                 'socket.socket.connect = deny\n'
                 'socket.socket.connect_ex = deny\n')
        env = os.environ.copy()
        env['PYTHONPATH'] = str(guard)
        return env

    def test_inside_symlink_inside_and_nonempty_handoff_refused_without_overwrite(self):
        self.prepare(handoff=self.root / 'handoff', expected=2)
        alias = self.base / 'alias-to-client'
        alias.symlink_to(self.root, target_is_directory=True)
        self.prepare(handoff=alias / 'handoff', expected=2)
        occupied = self.base / 'occupied'
        self.put(occupied / 'user.txt', 'KEEP ORIGINAL')
        before = complete_snapshot(occupied)
        self.prepare(handoff=occupied, expected=2)
        self.assertEqual(complete_snapshot(occupied), before)
        handoff, _ = self.prepare()
        self.put(handoff / 'run.sh', 'USER EDIT\n')
        before = complete_snapshot(handoff)
        self.prepare(handoff=handoff, expected=2)
        self.assertEqual(complete_snapshot(handoff), before)

    def test_invalid_uuid_config_operation_and_unpinned_source_refused(self):
        for extra in [('--project-id', 'invalid'), ('--types', 'unknown'),
                      ('--adapters', 'deepseek'), ('--operation', 'unknown')]:
            with self.subTest(extra=extra):
                self.prepare(extra=extra, expected=2)
        self.invoke(['prepare', '--root', self.root, '--source-base',
                     'https://raw.githubusercontent.com/example/toolkit/main',
                     '--project-id', bootstrap.PROJECT_ID, '--types', 'coding,wiki',
                     '--adapters', 'claude,codex,kimi', '--handoff-dir', self.base / 'unpinned',
                     '--operation', 'apply'], expected=2)

    def test_apply_runs_existing_preserving_engine_and_verified_check(self):
        handoff, _ = self.prepare('apply')
        run = subprocess.run(['bash', str(handoff / 'run.sh')], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.command('check')
        self.assert_receipt(handoff, 'apply')
        self.assertEqual((self.root / '.AI/memory/MEMORY.md').read_bytes(), self.legacy_memory)
        self.assertEqual((self.root / '.AI/rules/coding.md').read_bytes(), self.legacy_rule)
        self.assertEqual((self.root / '.AI/skills/private/SKILL.md').read_bytes(), b'# Private skill\n')
        self.assertEqual((self.root / '.claude/settings.local.json').read_bytes(), b'{"custom":true}\n')
        self.assertEqual((self.root / 'docs/backlog/task.md').read_bytes(), b'Task ID: TASK-17\n')
        self.assertEqual((self.root / '.agents/skills').resolve(), (self.root / '.AI/skills').resolve())
        self.assertEqual((self.root / '.ai-bootstrap/legacy/CLAUDE.md').read_bytes(), self.legacy_context)
        state = json.loads((self.root / '.AI/canon/canon.state.json').read_text())
        self.assertEqual(json.loads((self.root / '.AI/project.json').read_text())['project_id'], bootstrap.PROJECT_ID)
        self.assertEqual(state['schema_version'], 2)
        before = bootstrap.snapshot(self.root)
        self.run_request(handoff, 'apply')
        self.assertEqual(bootstrap.snapshot(self.root), before)

    def test_tool_sha_tamper_fails_before_engine_and_preserves_handoff(self):
        handoff, _ = self.prepare('apply')
        for name in TOOLS:
            with self.subTest(tool=name):
                path = handoff / 'tools' / name
                original = path.read_bytes()
                path.write_bytes(original + b'\n# tampered\n')
                before = complete_snapshot(self.root)
                self.run_request(handoff, 'apply', expected=2)
                self.assertEqual(complete_snapshot(self.root), before)
                if (handoff / 'result.json').exists():
                    self.assertEqual(json.loads((handoff / 'result.json').read_text())['status'], 'failed')
                self.assertTrue(path.exists())
                path.write_bytes(original)

    def test_unknown_field_arbitrary_command_and_operation_mismatch_failclosed(self):
        handoff, _ = self.prepare('plan')
        request = handoff / 'request.json'
        original = request.read_bytes()
        for key, value in [('unknown', True), ('executable', '/bin/false'), ('args', ['--unsafe'])]:
            with self.subTest(field=key):
                data = json.loads(original)
                data[key] = value
                request.write_text(json.dumps(data))
                before = complete_snapshot(self.root)
                self.run_request(handoff, 'plan', expected=2)
                self.assertEqual(complete_snapshot(self.root), before)
        request.write_bytes(original)
        before = complete_snapshot(self.root)
        self.run_request(handoff, 'apply', expected=2)
        self.assertEqual(complete_snapshot(self.root), before)

    def test_request_schema_types_identity_source_and_receipts_rejected_before_writes(self):
        handoff, _ = self.prepare('apply')
        request = handoff / 'request.json'
        original = request.read_bytes()
        mutations = [('schema_version', 999), ('schema_version', True), ('root', 'relative'),
                     ('root', []), ('project_id', 'invalid'), ('project_type', 'coding'),
                     ('project_type', []), ('project_type', ['coding', 'coding']),
                     ('project_type', ['unknown']), ('adapters', []), ('adapters', ['deepseek']),
                     ('source', {'kind': 'http', 'base': 'https://example.invalid/main'}),
                     ('source', {'kind': 'bundle', 'path': 'relative'}),
                     ('source', {'kind': 'bundle', 'path': str(self.bundle), 'executable': '/bin/true'}),
                     ('tools_sha256', {}), ('tools_sha256', {name: '0' * 64 for name in TOOLS})]
        for key, value in mutations:
            with self.subTest(field=key, value=value):
                data = json.loads(original)
                data[key] = value
                request.write_text(json.dumps(data))
                before = complete_snapshot(self.root)
                self.run_request(handoff, 'apply', expected=2)
                self.assertEqual(complete_snapshot(self.root), before)
        request.write_bytes(original)

    def test_underlying_failure_retains_artifacts_and_does_not_recover_automatically(self):
        self.put(self.root / '.agents/user.txt', 'KEEP')
        handoff, _ = self.prepare('apply')
        before = complete_snapshot(self.root)
        self.run_request(handoff, 'apply', expected=2)
        self.assertEqual(complete_snapshot(self.root), before)
        self.assertTrue((handoff / 'request.json').is_file())
        self.assertTrue((handoff / 'tools/ai-migrate.py').is_file())
        if (handoff / 'result.json').exists():
            data = json.loads((handoff / 'result.json').read_text())
            self.assertNotIn(data.get('status'), ['success', 'ok', 'healthy'])

    def test_interrupted_matching_recovery_and_check_are_offline_using_retained_tools(self):
        handoff, _ = self.prepare('recover')
        check, _ = self.prepare('check')
        # Reuse an existing, previously independent engine fault fixture; no code inspection.
        args = [sys.executable, '-c', recovery.WRAPPER, str(handoff / 'tools/ai-migrate.py'),
                str(self.root), str(self.bundle), bootstrap.PROJECT_ID, 'AGENTS.md', 'fail']
        fault = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(fault.returncode, 0, fault.stdout + fault.stderr)
        self.assertIn('FAULT_POINT', fault.stdout)
        self.assertTrue((self.root / '.ai-bootstrap/migration.json').exists())
        shutil.rmtree(self.bundle)
        offline = self.network_denied_environment()
        self.run_request(handoff, 'recover', env=offline)
        self.assert_receipt(handoff, 'recover')
        self.command('check')
        self.assertFalse((self.root / '.ai-bootstrap/migration.json').exists())
        self.assertEqual((self.root / '.AI/memory/MEMORY.md').read_bytes(), self.legacy_memory)
        before = bootstrap.snapshot(self.root)
        self.run_request(handoff, 'recover', env=offline)
        self.assertEqual(bootstrap.snapshot(self.root), before)
        self.run_request(check, 'check', env=offline)
        self.assert_receipt(check, 'check')


if __name__ == '__main__':
    unittest.main()
