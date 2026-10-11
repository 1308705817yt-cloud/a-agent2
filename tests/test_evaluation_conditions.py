"""Acceptance cannot pass on negative-only suites or stale execution conditions."""
import copy
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from agentforge.adaptive_testing import validate_extra_suite, tool_tests, validate_tool_coverage
from agentforge.evaluation_progress import EvaluationProgress
from agentforge.factory import FactoryAgent
from agentforge.resources import read_json, write_json, fingerprint
import test_contract_recovery as fixtures
from test_adaptive import AdaptiveFixturePlanner, DocClient
from test_codex_factory import DOCUMENTS


class AcceptanceCoverageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HostContractTests()
        self.fixture.setUp()
        self.spec, self.req, self.resources = self.fixture.spec, self.fixture.req, self.fixture.resources

    def test_negative_only_acceptance_is_rejected_by_validator_and_tool_evaluator(self):
        suite = self.fixture.suite()
        suite["tool_cases"] = [c for c in suite["tool_cases"] if c["partition"] != "acceptance" or c.get("expected_error")]
        with self.assertRaisesRegex(ValueError, "合法计算"):
            validate_extra_suite(suite, self.spec, self.req, self.resources)
        with patch("agentforge.adaptive_testing.CustomTool", side_effect=AssertionError("no source")), \
             self.assertRaisesRegex(ValueError, "合法计算"):
            tool_tests(self.spec, self.req, self.resources, suite, "acceptance")

    def test_declared_business_errors_need_result_assertions_in_both_partitions(self):
        error = {"id": "bad-cell", "layer": "tool_result", "input_json": '{"dataset_id":"fixture","column":"amount","rows":[null]}',
                 "code": "BAD_CELL", "description": "Invalid cell contents"}
        tool = self.spec.custom_tools[0] | {"error_cases": [self.fixture.error_case(), error]}
        suite = self.fixture.suite()
        for partition in ("development", "acceptance"):
            with self.assertRaisesRegex(ValueError, "bad-cell"):
                validate_tool_coverage(suite, [tool], (partition,))
            suite["tool_cases"].append(self.fixture.case(partition + "business", {"dataset_id":"fixture","column":"amount","rows":[None]},
                                                        {"rows":1,"missing":1,"total":0}, partition))
        report = validate_tool_coverage(suite, [tool])
        self.assertTrue(all(len(r["business_error"]) == 1 for r in report))
        # Business errors have schema-valid arguments, but cannot replace computation assertions.
        suite["tool_cases"] = [c for c in suite["tool_cases"] if c["partition"] != "acceptance" or c["id"].endswith("business") or c.get("expected_error")]
        with self.assertRaisesRegex(ValueError, "合法计算"):
            validate_tool_coverage(suite, [tool])

    def test_acceptance_computation_must_include_an_independent_input(self):
        suite = self.fixture.suite()
        development = [c for c in suite["tool_cases"] if c["partition"] == "development" and not c.get("expected_error")]
        for target, source in zip((c for c in suite["tool_cases"] if c["partition"] == "acceptance" and not c.get("expected_error")), development):
            target.update(input=copy.deepcopy(source["input"]), expected=copy.deepcopy(source["expected"]))
        with self.assertRaisesRegex(ValueError, "独立合法计算"):
            validate_extra_suite(suite, self.spec, self.req, self.resources)


class ExecutionConditionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "progress.json"
        self.binding, self.cases = {"artifact_hash":"fixture"}, [{"id":"finished"},{"id":"interrupted"}]
        self.conditions = {"max_seconds": 100, "model_configuration":{"backend":"fixture","requested_model":"model-a","request_timeout_seconds":30}}

    def tearDown(self):
        self.tmp.cleanup()

    def populate(self):
        progress = EvaluationProgress(self.path, self.binding, self.cases, self.conditions)
        progress.begin(self.cases[0])
        progress.commit(self.cases[0], {"error":None,"model_calls":3,"trace":[{"type":"decision"}]*3})
        progress.begin(self.cases[1])
        progress.checkpoint(self.cases[1], {"phase":"retryable","trace":[{"type":"decision"}],"observations":[{"id":"o1"}]})
        progress.commit(self.cases[1], {"error":{"code":"MODEL_ERROR"}})

    def test_relaxed_limit_reuses_finished_results_and_retains_checkpoint_with_old_conditions(self):
        self.populate()
        current = self.conditions | {"max_seconds":200}
        progress = EvaluationProgress(self.path, self.binding, self.cases, current)
        self.assertIsNotNone(progress.cached(self.cases[0]))
        self.assertEqual(progress.begin(self.cases[1])["observations"], [{"id":"o1"}])
        self.assertEqual(progress.evidence("finished")["conditions"]["max_seconds"], 100)
        self.assertEqual(progress.evidence("finished")["requested_conditions"]["max_seconds"], 200)
        self.assertEqual(progress.evidence("finished")["rule"], "relaxed_runtime_limit")
        self.assertEqual(progress.evidence("interrupted")["checkpoint_conditions"]["max_seconds"], 100)

    def test_tightened_limit_or_model_change_restarts_without_old_observations(self):
        for changed in (self.conditions | {"max_seconds":0.001},
                        self.conditions | {"model_configuration":self.conditions["model_configuration"] | {"requested_model":"model-b"}},
                        self.conditions | {"model_configuration":self.conditions["model_configuration"] | {"request_timeout_seconds":1}}):
            self.populate()
            progress = EvaluationProgress(self.path, self.binding, self.cases, changed)
            self.assertIsNone(progress.cached(self.cases[0]))
            self.assertIsNone(progress.begin(self.cases[1]))
            self.assertEqual(len(progress.state["entries"]["finished"]["history"]), 1)
            self.path.unlink()

    def test_unlimited_to_finite_is_tightening_and_finite_to_unlimited_is_relaxing(self):
        self.conditions["max_seconds"] = 0
        self.populate()
        finite = EvaluationProgress(self.path, self.binding, self.cases, self.conditions | {"max_seconds":100})
        self.assertIsNone(finite.cached(self.cases[0]))
        finite.begin(self.cases[0])
        finite.commit(self.cases[0], {"error":None})
        unlimited = EvaluationProgress(self.path, self.binding, self.cases, self.conditions)
        self.assertIsNotNone(unlimited.cached(self.cases[0]))

    def test_legacy_unknown_conditions_are_preserved_as_history_and_not_reused(self):
        self.populate()
        stored = read_json(self.path)
        stored["format"] = 1
        for entry in stored["entries"].values():
            entry.pop("conditions")
            entry.pop("conditions_hash")
        for attempt in stored["attempts"]:
            attempt.pop("conditions")
            attempt.pop("conditions_hash")
        write_json(self.path, stored)
        progress = EvaluationProgress(self.path, self.binding, self.cases, self.conditions)
        self.assertIsNone(progress.cached(self.cases[0]))
        self.assertIsNone(progress.begin(self.cases[1]))
        self.assertEqual(progress.state["entries"]["finished"]["history"][0]["invalidated_by"], "unknown_previous_conditions")

    def test_condition_tamper_is_rejected(self):
        self.populate()
        stored = read_json(self.path)
        stored["entries"]["finished"]["conditions"]["max_seconds"] = 999
        write_json(self.path, stored)
        with self.assertRaisesRegex(ValueError, "条件指纹"):
            EvaluationProgress(self.path, self.binding, self.cases, self.conditions)

    def test_completed_acceptance_is_not_a_pass_under_new_tiny_limit(self):
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        state = factory.drive(factory.create("政策问答", uploads=DOCUMENTS, run_max_seconds=100))
        same = factory.evaluate(state, "acceptance")
        self.assertTrue(same["all_passed"])
        self.assertEqual(same["new_model_calls"], 0)
        state["performance"]["run_max_seconds"] = 0.000000001
        from agentforge.adaptive_testing import evaluate_adaptive
        report = evaluate_adaptive(factory.agent(state), factory.agent(state).req, factory.resources(state), factory.suite(state),
            "acceptance", state["artifact_hash"], max_seconds=0.000000001,
            progress_path=factory.run_dir(state["run_id"]) / "versions/v1/acceptance_case_progress.json")
        self.assertFalse(report["all_passed"])
        self.assertEqual(report["reused_cases"], [])
        self.assertEqual(report["resumed_cases"], [])
        self.assertEqual(report["condition_change"]["rule"], "tightened_runtime_limit")
        runtime_cases = [c for c in report["cases"] if c.get("kind") not in ("custom_tool","host_contract","business_error")]
        self.assertTrue(any(c["actual"]["error"]["code"] == "TASK_TIMEOUT" for c in runtime_cases))
        self.assertTrue(all(c["evaluation"]["mode"] == "fresh_execution" for c in runtime_cases))


if __name__ == "__main__":
    unittest.main()
