"""Independent additive synthetic regressions; original frozen tests unchanged."""
import hashlib
import json
import unittest
import test_task_cost as fixture


class TaskCostFailures(unittest.TestCase):
    setUp = fixture.TaskCost.setUp
    cli = fixture.TaskCost.cli
    ledger = fixture.TaskCost.ledger

    def test_cache_known_component_cannot_exceed_inclusive_input(self):
        for read, write in ((11, None), (None, 11)):
            with self.subTest(read=read, write=write):
                self.ledger([fixture.record(tokens=dict(input=10, output=0,
                    cache_read=read, cache_write=write, reasoning=None))], expected=2)

    def test_known_money_survives_unknown_usage(self):
        money = dict(amount='0.25', currency='USD', kind='actual',
                     source='synthetic receipt', as_of='2026-10-07')
        data = self.ledger([fixture.record(status='unknown', tokens=None,
            reason='usage unavailable', money=money)], expected=3)
        self.assertEqual(data['money_totals'], [dict(currency='USD', kind='actual', amount='0.25')],
                         'TARGET: preserve known money independently of token coverage')
        self.assertEqual(data['money_unknown_records'], 0)

    def snapshot(self, provider, values):
        path = self.root / 'source.jsonl'
        path.write_text(''.join(json.dumps(v) + '\n' for v in values))
        result = self.cli('--snapshot', '--provider', provider, '--file', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        cp = json.loads(result.stdout)
        checkpoint = self.root / 'checkpoint.json'
        checkpoint.write_text(json.dumps(cp))
        return path, checkpoint, cp

    def delta(self, provider, path, checkpoint, values):
        with path.open('a') as handle:
            handle.write(''.join(json.dumps(v) + '\n' for v in values))
        result = self.cli('--snapshot', '--provider', provider, '--file', path, '--since', checkpoint)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_claude_checkpoint_unreported_reasoning_is_unknown(self):
        _, _, cp = self.snapshot('claude', [fixture.claude()])
        self.assertIsNone(cp['groups']['model-a']['reasoning'], 'TARGET: missing reasoning is not zero')

    def test_codex_disappearing_optional_is_unknown(self):
        path, checkpoint, _ = self.snapshot('codex', fixture.codex_start() +
            [fixture.usage(100, 20, cached_input_tokens=80)])
        data = self.delta('codex', path, checkpoint, [fixture.usage(120, 24)])
        self.assertIsNone(data['records'][0]['tokens']['cache_read'],
                          'TARGET: missing optional end counter is not measured zero')

    def test_codex_missing_optional_across_model_boundary_is_unknown(self):
        path, checkpoint, _ = self.snapshot('codex', fixture.codex_start() +
            [fixture.usage(100, 20)])
        data = self.delta('codex', path, checkpoint, [
            dict(type='turn_context', payload=dict(model='model-b')),
            fixture.usage(110, 22), fixture.usage(120, 24, cached_input_tokens=90)])
        record = next(r for r in data['records'] if r['model'] == 'model-b')
        self.assertEqual(record['tokens']['input'], 20)
        self.assertIsNone(record['tokens']['cache_read'],
                          'TARGET: counter spanning unknown model boundary cannot be attributed precisely')

    def test_native_record_hash_binds_prefix_through_end(self):
        path, checkpoint, _ = self.snapshot('codex', fixture.codex_start() + [fixture.usage()])
        data = self.delta('codex', path, checkpoint, [fixture.usage(120, 24)])
        record = data['records'][0]
        self.assertEqual(record['source']['prefix_sha256'],
            hashlib.sha256(path.read_bytes()[:record['source']['end']]).hexdigest(),
            'TARGET: native source hash covers end, not begin')

    def test_native_without_billing_evidence_does_not_invent_access(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                initial = fixture.codex_start() + [fixture.usage()] if provider == 'codex' else [fixture.claude()]
                appended = [fixture.usage(120, 24)] if provider == 'codex' else [fixture.claude('message-b')]
                path, checkpoint, _ = self.snapshot(provider, initial)
                data = self.delta(provider, path, checkpoint, appended)
                self.assertEqual(data['records'][0]['access'], 'unknown',
                                 'TARGET: parser identity does not establish billing access')

    def test_markdown_header_matches_numeric_columns(self):
        path = self.root / 'ledger.json'
        path.write_text(json.dumps(dict(schema_version=1, task_id='synthetic', records=[fixture.record()])))
        result = self.cli('--task-ledger', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith('|')]
        headings = [v.strip() for v in lines[0].strip('|').split('|')]
        values = [v.strip() for v in lines[2].strip('|').split('|')]
        mapped = dict(zip(headings, values))
        self.assertEqual({k: mapped[k] for k in ('input', 'output', 'cache_read', 'cache_write', 'reasoning')},
            dict(input='100', output='20', cache_read='40', cache_write='10', reasoning='5'),
            'TARGET: table values follow their named columns')


if __name__ == '__main__':
    unittest.main()
