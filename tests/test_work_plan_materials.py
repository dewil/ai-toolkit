import json
from pathlib import Path
import unittest

from test_plan_dates import Base, CLEAN_DATA, plan_text


ROOT = Path(__file__).resolve().parents[1]


class WorkPlanMaterials(Base):
    def test_published_template_matches_installed_skill_materials(self):
        assets = ROOT / 'skills/work-plan/templates'
        manifest = (ROOT / 'manifest.yaml').read_text().splitlines()
        skill = (assets.parent / 'SKILL.md').read_text()
        for asset in ('work-plan.md', 'work-plan-sroki.example.json'):
            self.assertIn('  - skills/work-plan/templates/' + asset, manifest)
            self.assertIn('](templates/' + asset + ')', skill)
            self.assertEqual((assets / asset).read_bytes(), (ROOT / 'templates' / asset).read_bytes())
        self.put_plan((assets / 'work-plan.md').read_text())
        self.put_data(json.loads((assets / 'work-plan-sroki.example.json').read_text()))
        result = self.run_cmd('all')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_last_answer_removes_the_summary_but_keeps_other_waits(self):
        row = '| dwl | Вопросы Q3 | этап | 23.11 | 24.11 | ссылка | - |'
        self.put_data(CLEAN_DATA)
        self.put_plan(plan_text(blk_n=1, blk_rows=[row]))
        result = self.run_cmd('lint')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('без раздела', result.stdout)
        self.put_plan(plan_text(blk_n=0, blk_rows=[]))
        self.assertEqual(self.run_cmd('lint').returncode, 0)


if __name__ == '__main__':
    unittest.main()
