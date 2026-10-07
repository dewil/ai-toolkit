"""Blind task accounting CLI contract; synthetic local receipts, no network."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/session-cost.py'
PRIVATE = 'SYNTHETIC_PRIVATE_CONTENT_DO_NOT_REPORT'


def record(ident='call-a', **changes):
    value = dict(id=ident, role='author', vendor='vendor-a', model='model-a',
                 platform='cli', access='api', status='measured', source='synthetic:receipt',
                 tokens=dict(input=100, output=20, cache_read=40, cache_write=10, reasoning=5))
    value.update(changes)
    return value


def native(begin, end, session='synthetic-session', provider='codex'):
    return dict(session_id=session, provider=provider, begin=begin, end=end,
                source_file='/synthetic/source.jsonl', prefix_sha256='a' * 64)


def codex_start(model='model-a'):
    return [dict(type='session_meta', payload=dict(id='synthetic-codex')),
            dict(type='turn_context', payload=dict(model=model)),
            dict(type='event_msg', payload=dict(type='user_message', message=PRIVATE))]


def usage(input=100, output=20, **optional):
    return dict(type='event_msg', payload=dict(type='token_count', info=dict(
        total_token_usage=dict(input_tokens=input, output_tokens=output, **optional),
        last_token_usage=dict(input_tokens=999999, output_tokens=999999))))


def claude(ident='message-a', model='model-a', session='synthetic-claude', **usage_values):
    tokens = dict(input_tokens=10, output_tokens=3, cache_read_input_tokens=20,
                  cache_creation_input_tokens=5)
    tokens.update(usage_values)
    return dict(type='assistant', sessionId=session, message=dict(
        id=ident, model=model, usage=tokens, content=[dict(type='text', text=PRIVATE)]))


class TaskCost(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task-cost-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              cwd=self.root, capture_output=True, text=True, timeout=15)

    def ledger(self, records, expected=0, **changes):
        value = dict(schema_version=1, task_id='SYNTHETIC-TASK', records=records)
        value.update(changes)
        path = self.root / 'ledger.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        result = self.cli('--task-ledger', path, '--json')
        self.assertEqual(result.returncode, expected, result.stderr)
        if expected == 2:
            self.assertTrue(result.stderr.strip())
            self.assertFalse(result.stdout.strip(), 'invalid input must not produce successful summary')
            return None
        data = json.loads(result.stdout)
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['task_id'], 'SYNTHETIC-TASK')
        return data

    def assert_known(self, data, total, unknown=0):
        self.assertEqual(data['measured_tokens'], total, 'TARGET: count input/output only, never cache/reasoning twice')
        self.assertEqual(data['unknown_records'], unknown, 'TARGET: unknown usage must remain visible')
        self.assertEqual(data['coverage'], 'partial' if unknown else 'complete')
        self.assertEqual(sum(row['total'] for row in data['rows']), total)

    def test_multi_vendor_retry_unknown_and_local_zero(self):
        a = record()
        retry = record('retry', status='unknown', tokens=None, reason='unsupported usage',
                       vendor='vendor-b', model='unknown', role='reviewer', access='subscription')
        b = record('review', vendor='vendor-b', model='model-b', role='reviewer',
                   tokens=dict(input=30, output=7, cache_read=None, cache_write=None, reasoning=None))
        local = record('test', vendor='local', model='none', role='checks', access='local',
                       tokens=dict(input=0, output=0, cache_read=0, cache_write=0, reasoning=0))
        data = self.ledger([a, retry, b, local], expected=3)
        self.assert_known(data, 157, 1)
        self.assertEqual(sum(row['records'] for row in data['rows']), 4)
        row = next(row for row in data['rows'] if row['model'] == 'model-b')
        self.assertIsNone(row['tokens']['cache_read'])
        self.assertIsNone(row['tokens']['reasoning'])
        self.assertEqual(row['total'], 37)

    def test_cache_breakdown_unknown_without_unknown_total(self):
        b = record('second', tokens=dict(input=50, output=10, cache_read=None, cache_write=0, reasoning=None))
        data = self.ledger([record(), b])
        self.assert_known(data, 180)
        self.assertEqual(len(data['rows']), 1)
        self.assertIsNone(data['rows'][0]['tokens']['cache_read'])
        self.assertIsNone(data['rows'][0]['tokens']['reasoning'])
        self.assertEqual(data['rows'][0]['tokens']['cache_write'], 10)

    def test_identical_id_once_and_conflict_error(self):
        self.assert_known(self.ledger([record(), record()]), 120)
        self.assert_known(self.ledger([record('initial'), record('retry')]), 240)
        wrong = record()
        wrong['tokens']['output'] = 21
        self.ledger([record(), wrong], expected=2)
        self.ledger([record(), record(role='other')], expected=2)

    def test_native_overlap_and_parallel_sessions(self):
        a = record('a', source=native(0, 100))
        self.ledger([a, record('b', source=native(50, 150), role='review')], expected=2)
        self.assert_known(self.ledger([a, record('b', source=native(100, 150))]), 240)
        self.assert_known(self.ledger([a, record('b', source=native(0, 100, session='another'))]), 240)
        self.assert_known(self.ledger([a, record('b', source=native(0, 100), model='another')]), 240)

    def test_invalid_ledger_values(self):
        variants = []
        for field, value in [('input', True), ('output', -1), ('cache_read', 101),
                             ('cache_write', -1), ('reasoning', 21), ('input', 1.5)]:
            r = record(); r['tokens'][field] = value; variants.append(r)
        r = record(); del r['tokens']['reasoning']; variants.append(r)
        r = record(); r['tokens'].update(cache_read=90, cache_write=20); variants.append(r)
        variants += [record(role=''), record(access='invalid'), record(status='unknown', tokens=None),
                     record(source=native(100, 100)), record(source=native(True, 100))]
        for r in variants:
            with self.subTest(record=r):
                self.ledger([r], expected=2)
        self.ledger([], expected=2, schema_version=2)
        self.ledger([], expected=2, task_id='')

    def test_money_decimal_separate_currency_kind_and_unknown(self):
        def money(amount, currency='USD', kind='actual'):
            return dict(amount=amount, currency=currency, kind=kind, source='synthetic:method', as_of='2026-10-07')
        rows = [record('a', money=money('0.1')), record('b', money=money('0.2')),
                record('c', money=money('2', kind='estimate'), access='subscription'),
                record('d', money=money('3', currency='EUR')), record('e')]
        data = self.ledger(rows)
        totals = {(r['currency'], r['kind']): r['amount'] for r in data['money_totals']}
        from decimal import Decimal
        self.assertEqual(Decimal(totals['USD', 'actual']), Decimal('0.3'))
        self.assertEqual(Decimal(totals['USD', 'estimate']), Decimal('2'))
        self.assertEqual(Decimal(totals['EUR', 'actual']), Decimal('3'))
        self.assertEqual(len(totals), 3)
        self.assertEqual(data['money_unknown_records'], 1)
        for change in [dict(amount='NaN'), dict(amount='Infinity'), dict(amount='-1'),
                       dict(amount=0.1), dict(currency='usd'), dict(currency='РУБ'),
                       dict(as_of='2026-02-30'), dict(source=''), dict(kind='invalid')]:
            m = money('1'); m.update(change)
            self.ledger([record(money=m)], expected=2)
        self.ledger([record(money=money('1'), access='subscription')], expected=2)

    def test_markdown_escaping_partial_and_cli_exclusion(self):
        data = self.ledger([record(role='author|review\nsecond')])
        result = self.cli('--task-ledger', self.root / 'ledger.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('\\|', result.stdout)
        self.assertNotIn('review\nsecond', result.stdout)
        self.assertNotIn(PRIVATE, result.stdout)
        for flags in [('--file', self.root / 'source'), ('--session', 'another'),
                      ('--snapshot', '--provider', 'codex', '--file', self.root / 'source')]:
            self.assertEqual(self.cli('--task-ledger', self.root / 'ledger.json', *flags).returncode, 2)
        self.assertEqual(self.cli('--task-ledger', self.root / 'missing.json', '--json').returncode, 2)

    def jsonl(self, lines, name='source.jsonl'):
        path = self.root / name
        path.write_bytes(b''.join((json.dumps(line) + '\n').encode() for line in lines))
        return path

    def checkpoint(self, provider, path):
        result = self.cli('--snapshot', '--provider', provider, '--file', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['provider'], provider)
        self.assertEqual(data['offset'], path.stat().st_size)
        self.assertEqual(data['prefix_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        for key in ['session_id', 'source_file', 'groups']:
            self.assertIn(key, data)
        saved = self.root / 'checkpoint.json'; saved.write_text(result.stdout)
        return saved, data

    def delta(self, provider, path, checkpoint, expected=0):
        result = self.cli('--snapshot', '--provider', provider, '--file', path, '--since', checkpoint)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        if expected == 2:
            self.assertFalse(result.stdout.strip())
            return None
        data = json.loads(result.stdout)
        self.assertEqual(data['schema_version'], 1)
        return data['records']

    def append(self, path, lines):
        with path.open('ab') as stream:
            stream.write(b''.join((json.dumps(line) + '\n').encode() for line in lines))

    def test_codex_cumulative_duplicate_model_switch_and_boundary(self):
        initial = usage(100, 20, cached_input_tokens=40, cache_write_input_tokens=10, reasoning_output_tokens=5)
        path = self.jsonl(codex_start() + [initial])
        checkpoint, start = self.checkpoint('codex', path)
        next_usage = usage(160, 40, cached_input_tokens=70, cache_write_input_tokens=15, reasoning_output_tokens=10)
        self.append(path, [initial, next_usage, next_usage,
                           dict(type='turn_context', payload=dict(model='model-b')),
                           usage(200, 55, cached_input_tokens=80, cache_write_input_tokens=20, reasoning_output_tokens=12)])
        records = self.delta('codex', path, checkpoint)
        by_model = {r['model']: r for r in records}
        self.assertEqual(set(by_model), {'model-a', 'model-b'})
        self.assertEqual(by_model['model-a']['tokens'], dict(input=60, output=20, cache_read=30, cache_write=5, reasoning=5))
        self.assertEqual(by_model['model-b']['tokens'], dict(input=40, output=15, cache_read=10, cache_write=5, reasoning=2))
        for r in records:
            self.assertEqual(r['source']['begin'], start['offset'])
            self.assertEqual(r['source']['end'], path.stat().st_size)
        self.assertEqual(self.delta('codex', path, checkpoint), records, 'same snapshot interval must have stable record IDs')
        self.assert_known(self.ledger(records), 135)

    def test_codex_optional_unknown_and_no_new_usage(self):
        path = self.jsonl(codex_start() + [usage(100, 20)])
        checkpoint, _ = self.checkpoint('codex', path)
        self.append(path, [dict(type='event_msg', payload=dict(type='user_message', message=PRIVATE))])
        self.assertEqual(self.delta('codex', path, checkpoint), [])
        self.append(path, [usage(105, 23)])
        records = self.delta('codex', path, checkpoint)
        self.assertEqual(records[0]['tokens'], dict(input=5, output=3, cache_read=None, cache_write=None, reasoning=None))

    def test_codex_drift_reset_and_invalid_usage(self):
        path = self.jsonl(codex_start() + [usage()]); checkpoint, _ = self.checkpoint('codex', path)
        self.append(path, [usage(99, 20)])
        self.delta('codex', path, checkpoint, expected=2)
        path.write_bytes(path.read_bytes().replace(b'synthetic-codex', b'changed-codex'))
        self.delta('codex', path, checkpoint, expected=2)
        for lines in [[usage()], codex_start(model=None) + [usage()],
                      codex_start() + [usage(input=True)],
                      codex_start() + [usage(cached_input_tokens=101)],
                      codex_start() + [usage(reasoning_output_tokens=21)]]:
            with self.subTest(lines=lines):
                path = self.jsonl(lines)
                self.assertEqual(self.cli('--snapshot', '--provider', 'codex', '--file', path).returncode, 2)

    def test_strict_snapshot_bytes_and_checkpoint_identity(self):
        path = self.jsonl(codex_start() + [usage()]); saved, data = self.checkpoint('codex', path)
        for raw in [b'\xff\n', b'{malformed}\n', b'{"type":"event_msg"}']:
            bad = self.root / 'bad.jsonl'; bad.write_bytes(raw)
            self.assertEqual(self.cli('--snapshot', '--provider', 'codex', '--file', bad).returncode, 2)
        for change in [dict(provider='claude'), dict(session_id='other'), dict(offset=True),
                       dict(offset=path.stat().st_size + 100), dict(prefix_sha256='bad'),
                       dict(source_file=str(self.root / 'other'))]:
            wrong = dict(data); wrong.update(change); saved.write_text(json.dumps(wrong))
            self.delta('codex', path, saved, expected=2)

    def test_claude_last_nonempty_dedup_caches_and_no_children(self):
        first = claude(); path = self.jsonl([first]); saved, _ = self.checkpoint('claude', path)
        empty = copy.deepcopy(first); empty['message']['usage'] = {}
        self.append(path, [first, empty, claude(input_tokens=15, output_tokens=8),
                           claude('message-b', 'model-b', input_tokens=2, output_tokens=4,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0)])
        child = self.root / 'source' / 'subagents'; child.mkdir(parents=True)
        (child / 'child.jsonl').write_text(json.dumps(claude(input_tokens=900000)) + '\n')
        records = self.delta('claude', path, saved)
        by_model = {r['model']: r['tokens'] for r in records}
        self.assertEqual(by_model['model-a'], dict(input=5, output=5, cache_read=0, cache_write=0, reasoning=None))
        self.assertEqual(by_model['model-b'], dict(input=2, output=4, cache_read=0, cache_write=0, reasoning=None))
        self.assert_known(self.ledger(records), 16)

    def test_claude_snapshot_invalid_identity_model_usage_and_rollback(self):
        missing_id = claude(); del missing_id['message']['id']
        missing_usage = claude(); missing_usage['message']['usage']['input_tokens'] = True
        for lines in [[missing_id], [claude(session=None)], [claude(model=None)],
                      [claude(), claude(session='other')], [claude(), claude(model='different')],
                      [missing_usage]]:
            path = self.jsonl(lines)
            self.assertEqual(self.cli('--snapshot', '--provider', 'claude', '--file', path).returncode, 2)
        path = self.jsonl([claude()]); saved, _ = self.checkpoint('claude', path)
        self.append(path, [claude(input_tokens=1, output_tokens=1)])
        self.delta('claude', path, saved, expected=2)

    def test_report_oracle_positive_and_known_wrong(self):
        correct = dict(measured_tokens=120, unknown_records=0, coverage='complete', rows=[dict(total=120)])
        self.assert_known(correct, 120)
        wrong = copy.deepcopy(correct); wrong['measured_tokens'] = 175
        with self.assertRaisesRegex(AssertionError, 'TARGET: count input/output only'):
            self.assert_known(wrong, 120)


if __name__ == '__main__':
    unittest.main()
