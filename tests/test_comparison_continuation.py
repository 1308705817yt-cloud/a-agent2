import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

from experiments.resume_csv_comparison import SupplementFactory
from experiments.summarize_csv_comparison import cumulative_full


class ContinuationEvidenceTests(unittest.TestCase):
    def test_new_full_calls_do_not_write_original_audit_directory(self):
        factory = SupplementFactory.__new__(SupplementFactory)
        factory.root = Path("original-factory")
        factory.supplement_folder = Path("separate-supplement")
        client = SimpleNamespace()
        factory.configure_audit(client, {"run_id": "123456abcdef", "version": 1, "phase": "completed"}, "runtime")
        self.assertEqual(client.audit_dir, Path("separate-supplement/full-calls"))
        self.assertEqual(client.audit_context["run_id"], "123456abcdef")
        self.assertNotEqual(client.audit_dir, factory.run_dir("123456abcdef") / "calls")

    def test_recovery_coverage_does_not_overwrite_first_attempt_or_hide_old_cost(self):
        case = lambda id, passed, actual, status: dict(id=id, category="structured", passed=passed, actual=actual, status=status)
        original = {"arms": {"full": {"summary": {"passed": 1, "total": 3, "runtime_calls": 4, "case_wall_sum_ms": 900},
            "cases": [case("a", True, {}, "completed"), case("b", False, {}, "completed"), case("c", False, None, "unexecuted")]}}}
        later = {"arms": {"full": {"summary": {"passed": 2, "total": 2, "runtime_calls": 6, "case_wall_sum_ms": 500},
            "cases": [case("b", True, {}, "completed"), case("c", True, {}, "completed")]}}}
        snapshot = copy.deepcopy(original)
        result = cumulative_full(original, later)
        self.assertEqual(original, snapshot)
        self.assertEqual((result["first_attempt"]["passed"], result["covered_passed"]), (1, 3))
        self.assertEqual((result["historical_successes_retained"], result["fresh_retries"]), (1, 1))
        self.assertEqual((result["runtime_calls_all_attempts"], result["runtime_wall_sum_all_attempts_ms"]), (10, 1400))

    def test_unexecuted_supplement_does_not_become_a_pass(self):
        row = {"id": "x", "category": "structured", "passed": False, "actual": None, "status": "unexecuted"}
        arm = {"cases": [row], "summary": {"passed": 0, "total": 1, "runtime_calls": 0, "case_wall_sum_ms": 0}}
        result = cumulative_full({"arms": {"full": arm}}, {"arms": {"full": arm}})
        self.assertEqual(result["covered_passed"], 0)
        self.assertEqual(result["total"], 1)

    def test_unknown_call_count_stays_unknown_after_combining_attempts(self):
        arm = {"cases": [], "summary": {"passed": 0, "total": 0, "runtime_calls": None, "case_wall_sum_ms": 5}}
        later = {"cases": [], "summary": {"passed": 0, "total": 0, "runtime_calls": 2, "case_wall_sum_ms": 6}}
        result = cumulative_full({"arms": {"full": arm}}, {"arms": {"full": later}})
        self.assertIsNone(result["runtime_calls_all_attempts"])
        self.assertEqual(result["runtime_wall_sum_all_attempts_ms"], 11)


if __name__ == "__main__":
    unittest.main()
