"""SDD-FIN-04: execute the real documented profile guard with local fixtures."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1] / 'skills/codex-audit/SKILL.md'
MARKER = 'SIMULATED_MODEL_LAUNCH_AFTER_PROFILE_GUARD'


class SddProfileTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which('bash'), 'bash is required for the documented adapter')
        self.temp = tempfile.TemporaryDirectory(prefix='sdd profiles ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles = self.root / 'codex fixture'
        (self.profiles / 'sessions').mkdir(parents=True)
        # Execute the actual fenced public adapter code, changing only its role selector.
        blocks = re.findall(r'```bash\s*\n(.*?)\n```', SKILL.read_text(), re.S)
        candidates = [block for block in blocks if re.search(r'^PROFILE=audit\b', block, re.M)
                      and 'OTHER_MODEL' in block]
        self.assertEqual(len(candidates), 1, 'Expected one executable role-profile guard')
        self.snippet = candidates[0]
        self.environment = {k: v for k, v in os.environ.items() if k not in ('BASH_ENV', 'ENV')}
        self.environment.update(HOME=str(self.root), CODEX_HOME=str(self.profiles))

    def run_profile(self, role, pair, primary=None):
        for path in self.profiles.glob('*.config.toml'):
            path.unlink()
        (self.profiles / (role + '.config.toml')).write_text(
            'model = "model-author"\n' if primary is None else primary)
        other = 'compliance' if role == 'implement' else 'implement'
        if pair != 'absent':
            content = {'empty': '# No usable model\nmodel = ""\n',
                       'match': "model = 'model-author' # same actual model\n",
                       'different': 'model = "model-independent"\n',
                       'malformed': "model = 'different-missing-closing-quote\n",
                       'nonstring': 'model = 42\n',
                       'whitespace': 'model = "   "\n',
                       'control': 'model = "different\\tmodel"\n'}[pair]
            (self.profiles / (other + '.config.toml')).write_text(content)
        program, count = re.subn(r'^PROFILE=audit\b', 'PROFILE=' + role, self.snippet, count=1, flags=re.M)
        self.assertEqual(count, 1)
        # No set -e: the profile guard itself must stop launch, not its incidental last status.
        program += '\nprintf "%s\\n" "' + MARKER + '"\n'
        return subprocess.run(['bash', '--noprofile', '--norc', '-c', program],
                              env=self.environment, cwd=self.root, capture_output=True, text=True, timeout=10)

    def test_implement_and_compliance_require_readable_distinct_pair_before_launch(self):
        for role in ('implement', 'compliance'):
            for pair in ('absent', 'empty', 'match', 'different'):
                with self.subTest(role=role, pair=pair):
                    result = self.run_profile(role, pair)
                    if pair == 'different':
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn(MARKER, result.stdout)
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertNotIn(MARKER, result.stdout + result.stderr)
                        self.assertTrue((result.stdout + result.stderr).strip())

    def test_broken_pair_profiles_cannot_authorize_independent_model_launch(self):
        for role in ('implement', 'compliance'):
            for pair in ('malformed', 'nonstring', 'whitespace', 'control'):
                with self.subTest(role=role, pair=pair):
                    result = self.run_profile(role, pair)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn(MARKER, result.stdout + result.stderr)
                    self.assertTrue((result.stdout + result.stderr).strip())

    def test_audit_requires_a_valid_nonempty_string_model_in_its_own_profile(self):
        for primary in ('model = ""\n', "model = 'missing-closing-quote\n",
                        'model = 42\n', 'model = "   "\n', 'model = "bad\\tmodel"\n'):
            with self.subTest(primary=primary):
                result = self.run_profile('audit', 'absent', primary=primary)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(MARKER, result.stdout + result.stderr)
                self.assertTrue((result.stdout + result.stderr).strip())

    def test_audit_does_not_require_an_independent_pair_profile(self):
        for pair in ('absent', 'empty', 'match', 'different'):
            with self.subTest(pair=pair):
                result = self.run_profile('audit', pair)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(MARKER, result.stdout)


if __name__ == '__main__':
    unittest.main()
