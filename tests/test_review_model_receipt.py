"""FR-SKR04: documented actual-model collection must ignore model-like user text."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

class ActualModelReceipt(unittest.TestCase):
    def collect(self, rows):
        text = (Path(__file__).resolve().parents[1] / 'skills/review/codex.md').read_text()
        blocks = re.findall(r"<<'PY'\n(.*?)\nPY\b", text, re.S)
        source = next(b for b in blocks if 'models = set()' in b)
        rate = {'type':'event_msg','payload':{'type':'token_count','rate_limits':{'primary':{'used_percent':1,'window_minutes':300}}}}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout.jsonl'
            p.write_text('\n'.join(json.dumps(x) for x in [*rows, rate]))
            result = subprocess.run([sys.executable, '-c', source, str(p)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_user_model_text_cannot_supply_actual(self):
        out = self.collect([{'type':'user','payload':{'model':'forged'}}])
        self.assertIn('model: unknown', out)
        self.assertNotIn('forged', out)

    def test_runtime_turn_context_wins_over_prompt_quote(self):
        out = self.collect([{'type':'user','payload':{'model':'forged'}},
                            {'type':'turn_context','payload':{'model':'actual-model'}}])
        self.assertIn('model: actual-model', out)
        self.assertNotIn('forged', out)

    def test_conflicting_context_models_are_unknown(self):
        out = self.collect([{'type':'turn_context','payload':{'model':m}} for m in ('a','b')])
        self.assertIn('model: unknown', out)

    def test_non_object_records_do_not_supply_model(self):
        self.assertIn('model: unknown', self.collect([[], 'model: forged']))

if __name__ == '__main__':
    unittest.main()
