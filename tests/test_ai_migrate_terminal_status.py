"""Receipt regressions for the post-design JSON-status contract clarification."""
import hashlib
import json
import unittest

import test_ai_migrate as migration
import test_ai_migrate_terminal as terminal


class TerminalStatusContract(unittest.TestCase):
    setUp = migration.AiMigrationContract.setUp
    put = migration.AiMigrationContract.put
    policy = migration.AiMigrationContract.policy
    write_registry = migration.AiMigrationContract.write_registry
    command = migration.AiMigrationContract.command
    invoke = terminal.MigrationTerminalContract.invoke
    prepare = terminal.MigrationTerminalContract.prepare
    run_request = terminal.MigrationTerminalContract.run_request

    def fake_engine(self, handoff, responses):
        engine = handoff / 'tools/ai-migrate.py'
        engine.write_text('import sys\nresponses = ' + repr(responses) +
                          '\nprint(responses[sys.argv[1]])\n')
        request = handoff / 'request.json'
        data = json.loads(request.read_text())
        data['tools_sha256']['ai-migrate.py'] = hashlib.sha256(engine.read_bytes()).hexdigest()
        request.write_text(json.dumps(data))

    def assert_failed(self, handoff, operation, stage):
        self.run_request(handoff, operation, expected=2)
        result = json.loads((handoff / 'result.json').read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['stage'], stage)
        self.assertIsNone(result['check'])
        self.assertNotEqual(result['exit_code'], 0)

    def test_exit_zero_with_malformed_or_unexpected_plan_is_failure(self):
        for response in ['not JSON', '{"status":"ok"}', '[]']:
            with self.subTest(response=response):
                handoff, _ = self.prepare(handoff=self.base / ('plan-' + str(len(response))))
                self.fake_engine(handoff, {'plan': response})
                self.assert_failed(handoff, 'plan', 'plan')

    def test_apply_and_recovery_require_real_ok_check_status(self):
        for operation, status in [('apply', 'migrated'), ('recover', 'no-transaction')]:
            with self.subTest(operation=operation):
                handoff, _ = self.prepare(operation)
                self.fake_engine(handoff, {'plan': '{"status":"planned","steps":[]}',
                                          operation: json.dumps({'status': status}),
                                          'check': '{"status":"up-to-date"}'})
                self.assert_failed(handoff, operation, 'check')

    def test_checked_client_must_match_request_uuid(self):
        self.command('apply')
        handoff, _ = self.prepare('check')
        request = handoff / 'request.json'
        data = json.loads(request.read_text())
        data['project_id'] = 'a1111111-1111-4111-8111-111111111111'
        request.write_text(json.dumps(data))
        before = terminal.complete_snapshot(self.root)
        self.assert_failed(handoff, 'check', 'check')
        self.assertEqual(terminal.complete_snapshot(self.root), before)


if __name__ == '__main__':
    unittest.main()
