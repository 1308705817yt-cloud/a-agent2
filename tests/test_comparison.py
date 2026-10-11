import copy
import unittest

from agentforge.resources import prepare_resources, read_json
from agentforge.codex_client import validate_json
from experiments.run_csv_comparison import ROOT, verify_gold, direct_context, response_schema, score, summarize


class ComparisonProtocolTests(unittest.TestCase):
    def setUp(self):
        self.suite = read_json(ROOT / "experiments/csv_comparison_v1.json")
        self.resources = prepare_resources(uploads=self.suite["files"])
        self.case = self.suite["cases"][0]
        self.output = {"answer": "已按账本汇总", "sources": [], "abstained": False, "error": None,
                       "data": {"dataset_id": self.resources["datasets"][0]["id"], **copy.deepcopy(self.case["gold"])}}

    def test_frozen_gold_matches_independent_decimal_oracle(self):
        verify_gold(self.suite, self.resources)
        self.assertEqual(len(self.suite["cases"]), 12)

    def test_model_context_excludes_gold_reference_queries_and_other_questions(self):
        context = direct_context(self.suite, self.case, self.resources)
        self.assertEqual(set(context), {"request", "input", "defaults", "resources"})
        self.assertNotIn("cases", context)
        self.assertEqual(context["input"], self.case["input"])
        context["resources"]["datasets"][0]["rows"].clear()
        self.assertEqual(len(self.resources["datasets"][0]["rows"]), 24)

    def test_all_schema_objects_have_strict_properties_including_empty_sources(self):
        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(set(node.get("required", [])), set(node.get("properties", {})))
                for child in node.values():
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)
        check(response_schema())

    def test_both_arms_use_business_assertions_without_custom_tool_prerequisite(self):
        self.assertTrue(score(self.case, self.output, self.resources)["passed"])
        validate_json(self.output, response_schema())
        self.output["data"]["results"][0]["value"] = 999
        self.assertFalse(score(self.case, self.output, self.resources)["passed"])

    def test_count_value_correct_but_noncanonical_column_fails(self):
        case = self.suite["cases"][9]
        output = self.output | {"data": {"dataset_id": self.resources["datasets"][0]["id"], **case["gold"], "column": "net_units"}}
        self.assertFalse(score(case, output, self.resources)["passed"])

    def test_invalid_input_does_not_pass_with_success_output_or_other_error(self):
        case = self.suite["cases"][-1]
        self.assertFalse(score(case, self.output, self.resources)["passed"])
        output = self.output | {"data": None, "abstained": True, "error": {"code": "MODEL_ERROR", "message": "unavailable"}}
        self.assertFalse(score(case, output, self.resources)["passed"])
        output["error"]["code"] = "INVALID_INPUT"
        self.assertTrue(score(case, output, self.resources)["passed"])

    def test_unexecuted_cases_stay_in_denominator_and_negative_coverage_is_separate(self):
        cases = [{"category": "structured", "actual": None, "passed": False},
                 {"category": "invalid_input", "actual": self.output, "passed": True, "elapsed_ms": 5, "new_model_calls": 0}]
        value = summarize({"cases": cases})
        self.assertEqual((value["passed"], value["total"], value["executed"]), (1, 2, 1))
        self.assertEqual(value["computation_passed"], 0)
        self.assertEqual(value["invalid_input_passed"], 1)

    def test_missing_call_count_is_unknown_instead_of_zero(self):
        value = summarize({"cases": [{"category": "structured", "actual": self.output, "passed": False,
                                     "elapsed_ms": 5, "new_model_calls": None}]})
        self.assertIsNone(value["runtime_calls"])


if __name__ == "__main__":
    unittest.main()
