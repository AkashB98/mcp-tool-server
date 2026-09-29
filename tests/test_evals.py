"""Eval harness tests: determinism + report contract."""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EVALS = os.path.dirname(HERE) + "/evals"


class TestEvals(unittest.TestCase):
    def test_golden_tasks_well_formed(self):
        tasks = json.load(open(os.path.join(EVALS, "golden_tasks.json")))
        self.assertEqual(len(tasks), 7)
        for t in tasks:
            self.assertIn("id", t)
            self.assertIn("task", t)
            self.assertIn("expect", t)
            self.assertIn("tools", t["expect"])

    def test_eval_run_is_deterministic(self):
        def run():
            p = subprocess.run(
                [sys.executable, os.path.join(EVALS, "run_evals.py")],
                capture_output=True, text=True, cwd=os.path.dirname(EVALS),
                timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr[-2000:])
            return open(os.path.join(EVALS, "eval_report.json")).read()
        first, second = run(), run()
        self.assertEqual(first, second)

    def test_report_contract(self):
        report = json.load(open(os.path.join(EVALS, "eval_report.json")))
        self.assertEqual(report["summary"]["total"], 7)
        self.assertEqual(report["summary"]["passed"], 7)
        for t in report["tasks"]:
            self.assertTrue(t["pass"], t["id"])
            self.assertIn("steps", t)
            self.assertIn("answer", t)
            for check in ("tools", "args", "answer_contains",
                          "answer_not_contains", "side_effect"):
                self.assertTrue(t["checks"][check], (t["id"], check))


if __name__ == "__main__":
    unittest.main()
