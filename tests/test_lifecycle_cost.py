"""Fair cost aggregation and independent scoring; no live model or Docker dependency."""

import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import subprocess

from agentforge.lifecycle_cost import summarize_lifecycle
from agentforge.resources import read_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reuse_comparison", ROOT / "experiments/run_reuse_comparison.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


class LifecycleCostTests(unittest.TestCase):
    def test_build_maintenance_and_failed_cases_remain_in_total(self):
        arm = {"builds": [{"phase": "initial", "elapsed_ms": 100, "model_calls": 2},
                          {"phase": "maintenance", "elapsed_ms": 50, "model_calls": 1}],
               "cases": [{"actual": {}, "passed": True, "elapsed_ms": 10, "model_calls": 0},
                         {"actual": None, "passed": False, "elapsed_ms": 30, "model_calls": 1}]}
        summary = summarize_lifecycle(arm)
        self.assertEqual(summary["total"], {"elapsed_ms": 190, "model_calls": 4})
        self.assertEqual(summary["amortized_per_requested_case"]["elapsed_ms"], 95)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["not_executed"], 1)
        self.assertIsNone(summary["human_minutes"])
        self.assertIsNone(summary["monetary_cost"])

    def test_empty_suite_does_not_claim_zero_amortized_cost(self):
        self.assertIsNone(summarize_lifecycle({})["amortized_per_requested_case"])

    def test_frozen_data_gold_and_schema_migration(self):
        suite = read_json(ROOT / "experiments/reuse_comparison_v1.json")
        comparison.verify_gold(suite)
        self.assertEqual(len(suite["epochs"]), 2)
        suite["epochs"][0]["cases"][0]["gold"]["value"] += 1
        with self.assertRaisesRegex(ValueError, "gold mismatch"):
            comparison.verify_gold(suite)

    def test_score_does_not_accept_boolean_or_partial_result(self):
        gold = {"value": 1, "row_count": 3}
        self.assertTrue(comparison.matches({"value": 1.0, "row_count": 3}, gold))
        for actual in (None, {"value": True, "row_count": 3}, {"value": 1, "row_count": 2},
                       {"value": 1, "row_count": 3, "extra": "ignored"}):
            self.assertFalse(comparison.matches(actual, gold))

    def test_container_is_offline_readonly_and_cleaned_on_timeout(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[1] == "image":
                return subprocess.CompletedProcess(command, 0, "sha256:fixture\n", "")
            if command[1] == "run":
                raise subprocess.TimeoutExpired(command, 1)
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(comparison.subprocess, "run", side_effect=run):
            runner = comparison.DockerScriptRunner("local-image")
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.run(Path("solution.py"), {}, 1)
        command = calls[1]
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(command[command.index("--pull") + 1], "never")
        self.assertIn("--read-only", command)
        self.assertEqual(calls[-1][:3], ["docker", "rm", "-f"])
        self.assertEqual(calls[-1][-1], command[command.index("--name") + 1])


if __name__ == "__main__":
    unittest.main()
