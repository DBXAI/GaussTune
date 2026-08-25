from pathlib import Path
import unittest

from huawei7.stage_spec import read_stage_spec


class StageSpecTest(unittest.TestCase):
    def test_checked_in_stages_are_exact_ppt_contract(self):
        path = Path(__file__).resolve().parents[1] / "config" / "ppt_five_stages.json"
        stages = read_stage_spec(path)
        self.assertEqual(stages[3].ap_queries, (2, 9, 13, 18, 21))
        self.assertEqual(stages[4].tp_terminals, 144)
        self.assertEqual(stages[4].tp_baseline_terminals, 128)
        self.assertEqual(stages[4].tp_surge_terminals, 16)
        self.assertEqual(stages[3].tp_surge_terminals, 0)


if __name__ == "__main__":
    unittest.main()
