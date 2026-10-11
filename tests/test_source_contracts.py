"""Regression coverage for preflight, failure ownership and executable tool designs."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentforge.adaptive_planner import AdaptivePlanner
from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.contracts import ExecutionError
from agentforge.factory import FactoryAgent
from agentforge.program import ALLOWED, HELPERS, interpreter_capabilities
from agentforge.resources import read_json
from agentforge.source_validation import apply_output_hook, preflight_output_hook
from test_adaptive import ScriptClient, decision, quality_spec, QUALITY
import test_adaptive
from test_codex_factory import GOOD_SOURCE
from test_runtime import csv_fixture


# The actual shape of the v0.7 laboratory output-hook defect: a runtime decision
# field was incorrectly returned as a hook field and data/error were dropped.
WRONG_LAYER = '''def run(context):
    result = context["result"]
    return {"answer": result["answer"], "sources": result["sources"],
            "abstained": result["abstained"], "result_ref": None}
'''


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.req, self.resources, _ = csv_fixture()
        self.spec = quality_spec()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.args = {"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}

    def tearDown(self):
        self.temp.cleanup()

    def generation(self, source=GOOD_SOURCE, tool_source=QUALITY):
        return {"source": source, "custom_sources": [{"id": "custom.quality", "source": tool_source}],
                "explanation": "Implement the contract", "instructions": self.spec.instructions}

    def agent(self, decisions, source=GOOD_SOURCE, tool_source=QUALITY):
        self.client = ScriptClient(decisions)
        spec = replace(self.spec, custom_tools=[self.spec.custom_tools[0] | {"source": tool_source}])
        return AdaptiveAgent(spec, self.req, self.resources, source, self.root, lambda: self.client)

    def test_wrong_layer_is_rejected_before_any_model_call(self):
        with self.assertRaises(ExecutionError) as raised:
            self.agent([], WRONG_LAYER)
        error = raised.exception.info()
        self.assertEqual(error["code"], "OUTPUT_CONTRACT")
        self.assertEqual(error["recovery"], "repair_source")
        self.assertIn("data", error["message"])
        self.assertIn("error", error["message"])
        self.assertIn("result_ref", error["message"])
        self.assertTrue(error["location"].startswith("logic.py:"))
        self.assertEqual(self.client.contexts, [])

    def test_generation_retries_with_precise_preflight_evidence(self):
        client = ScriptClient([self.generation(WRONG_LAYER), self.generation()])
        planner = AdaptivePlanner(client)
        result = planner.code(self.req, self.spec, [], self.resources)
        self.assertEqual(result["source"], GOOD_SOURCE)
        self.assertEqual(planner.code_attempts[0]["failure"]["origin"], "source")
        self.assertIn("result_ref", client.contexts[1]["previous_generation_errors"][0])
        self.assertEqual(client.contexts[1]["response_contract"]["mutable_fields"], ["answer"])
        self.assertEqual(planner.last_preflight["model_calls"], 0)
        json.dumps(planner.code_attempts)

    def test_repair_validates_ast_and_output_before_returning(self):
        invalid = self.generation(tool_source='def run(context):\n    return [x for x in []]\n')
        client = ScriptClient([invalid, self.generation(WRONG_LAYER), self.generation()])
        planner = AdaptivePlanner(client)
        revised, source, _ = planner.repair(self.req, self.spec, GOOD_SOURCE, [], {}, "", {}, self.resources)
        self.assertEqual(source, GOOD_SOURCE)
        self.assertEqual(revised.custom_tools[0]["source"], QUALITY)
        self.assertEqual(len(planner.repair_attempts), 2)
        self.assertIn("custom_tools/quality.py:2", client.contexts[1]["previous_generation_errors"][0])
        self.assertIn("result_ref", client.contexts[2]["previous_generation_errors"][1])
        json.dumps(planner.repair_attempts)

    def test_exhausted_generation_is_source_failure(self):
        planner = AdaptivePlanner(ScriptClient([self.generation(WRONG_LAYER) for _ in range(3)]))
        with self.assertRaises(ExecutionError) as raised:
            planner.code(self.req, self.spec, [], self.resources)
        self.assertEqual(raised.exception.code, "SOURCE_PREFLIGHT")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(planner.code_attempts), 3)

    def test_factory_defense_does_not_create_invalid_version(self):
        factory = FactoryAgent(self.root)
        state = factory.create("数据分析", uploads=[{"name": "fixture.csv", "content": "amount\n1\n"}])
        state["requirements"] = self.req.to_dict()
        with self.assertRaises(ExecutionError):
            factory.snapshot(state, self.spec, WRONG_LAYER, "Bad source")
        self.assertEqual(state["version"], 0)
        self.assertFalse((factory.run_dir(state["run_id"]) / "versions").exists())

    def test_snapshot_records_zero_call_contract_preflight(self):
        from agentforge.resources import write_json
        factory = FactoryAgent(self.root)
        state = factory.create("数据分析", uploads=[{"name": "fixture.csv", "content": "amount\n1\n"}])
        state["requirements"] = self.req.to_dict()
        write_json(factory.run_dir(state["run_id"]) / "resources.json", self.resources)
        state["frozen_hashes"]["test_suite"] = "test-fixture"
        write_json(factory.run_dir(state["run_id"]) / "test_suite.json", {"cases": []})
        factory.snapshot(state, self.spec, GOOD_SOURCE, "Valid source")
        report = read_json(factory.run_dir(state["run_id"]) / "versions/v1/source_preflight.json")
        self.assertTrue(report["all_passed"])
        self.assertIn("refusal", report["cases"])
        self.assertIn("error", report["cases"])
        self.assertEqual(report["model_calls"], 0)

    def test_input_specific_hook_defect_stops_at_first_finish(self):
        source = '''def run(context):
    result = context["result"]
    if get(context["input"], "descending", False) == False:
        result["data"] = "corrupted"
    return result
'''
        agent = self.agent([decision(tool="custom.quality", args=self.args), decision(tool="csv.query", args={"descending": False}),
                            decision("finish", result_ref="o2")], source)
        result = agent.run({"goal": "trigger-defect", "parameters": {"descending": False}})
        self.assertEqual(result["error"]["code"], "OUTPUT_CONTRACT")
        self.assertEqual(result["failure"]["origin"], "source")
        self.assertFalse(result["failure"]["retryable"])
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(len(self.client.contexts), 3)
        self.assertNotIn("checkpoint", result)
        self.assertEqual(result["observations"][-1]["error"]["recovery"], "repair_source")

    def test_custom_code_exception_is_fatal_not_argument_retry(self):
        agent = self.agent([decision(tool="custom.quality", args=self.args)],
                           tool_source='def run(context):\n    return 1 / 0\n')
        output = agent.run({})
        self.assertEqual(output["error"]["code"], "CODE_RUNTIME")
        self.assertEqual(output["failure"]["location"], "custom_tools/quality.py:2")
        self.assertEqual(output["model_calls"], 1)

    def test_custom_output_schema_defect_is_fatal(self):
        agent = self.agent([decision(tool="custom.quality", args=self.args)], tool_source='def run(context):\n    return {}\n')
        output = agent.run({})
        self.assertEqual(output["error"]["code"], "TOOL_OUTPUT_CONTRACT")
        self.assertEqual(output["failure"]["origin"], "source")
        self.assertEqual(output["model_calls"], 1)

    def test_invalid_arguments_and_finish_references_remain_correctable(self):
        agent = self.agent([decision(tool="custom.quality", args={}), decision(tool="custom.quality", args=self.args),
                            decision("finish"), decision(tool="csv.query"), decision("finish", result_ref="o4")])
        output = agent.run({})
        self.assertIsNone(output["error"])
        errors = [o["error"] for o in output["observations"] if o["status"] == "error"]
        self.assertEqual([e["code"] for e in errors], ["INVALID_ARGUMENTS", "RESULT_REFERENCE"])
        self.assertTrue(all(e["retryable"] and e["origin"] == "decision" for e in errors))

    def test_immutable_data_preserves_json_types(self):
        source = 'def run(context):\n    result = context["result"]\n    result["data"] = False\n    return result\n'
        payload = {"answer": "计算", "data": 0, "sources": [], "abstained": False, "error": None}
        with self.assertRaises(ExecutionError) as raised:
            apply_output_hook(source, payload, {}, self.resources)
        self.assertEqual(raised.exception.code, "OUTPUT_CONTRACT")
        self.assertEqual(payload["data"], 0)

    def test_unsupported_design_helpers_and_syntax_retry_before_freezing(self):
        good = test_adaptive.PlannerFeedbackTests().design()
        bad_helper = copy.deepcopy(good)
        bad_helper["custom_tools"][0]["execution_plan"]["helpers"].append("re.search")
        bad_syntax = copy.deepcopy(good)
        bad_syntax["custom_tools"][0]["execution_plan"]["syntax"].append("ListComp")
        client = ScriptClient([bad_helper, bad_syntax, good])
        planner = AdaptivePlanner(client)
        spec = planner.workflow(self.req, self.resources)
        self.assertEqual(spec.custom_tools[0]["source"], "")
        self.assertEqual(len(planner.design_attempts), 2)
        self.assertIn("re.search", client.contexts[1]["previous_validation_errors"][0])
        self.assertIn("ListComp", client.contexts[2]["previous_validation_errors"][1])
        self.assertEqual(set(client.contexts[0]["interpreter_capabilities"]["helpers"]), HELPERS)
        self.assertEqual(set(interpreter_capabilities()["ast_nodes"]), {n.__name__ for n in ALLOWED})

    def test_legacy_design_without_plan_remains_loadable(self):
        self.spec.validate(self.req, built=True)
        self.assertNotIn("execution_plan", self.spec.custom_tools[0])

    def test_zero_parameter_design_is_revised_before_test_freezing(self):
        good = test_adaptive.PlannerFeedbackTests().design()
        bad = copy.deepcopy(good)
        bad["custom_tools"][0]["parameters"] = []
        client = ScriptClient([bad, good])
        planner = AdaptivePlanner(client)
        planner.workflow(self.req, self.resources)
        self.assertIn("两个不同的合法开发输入", planner.design_attempts[0]["error"])
        self.assertEqual(len(client.contexts), 2)

    def test_helper_signature_mismatch_is_caught_before_execution(self):
        from agentforge.program import Program
        with self.assertRaises(ExecutionError) as raised:
            Program('def run(context):\n    return get(context)\n')
        self.assertEqual(raised.exception.code, "CODE_INVALID")
        self.assertEqual(raised.exception.location, "logic.py:2")
        self.assertEqual(interpreter_capabilities()["helper_arity"]["get"], {"minimum": 2, "maximum": 3})

    def test_preflight_does_not_invent_goal_in_effective_input(self):
        source = 'def run(context):\n    goal = context["input"]["goal"]\n    return context["result"]\n'
        with self.assertRaises(ExecutionError) as raised:
            preflight_output_hook(source, self.req, self.resources)
        self.assertEqual(raised.exception.code, "CODE_RUNTIME")
        self.assertIn("goal", str(raised.exception))

    def test_document_goal_preflight_has_no_structured_question_field(self):
        from agentforge.contracts import Requirements
        from agentforge.resources import prepare_resources
        from test_codex_factory import DOCUMENTS
        req = Requirements("问答", "document_qa", "问答", ["引用"], {})
        resources = prepare_resources(uploads=DOCUMENTS)
        source = 'def run(context):\n    question = context["input"]["question"]\n    return context["result"]\n'
        with self.assertRaises(ExecutionError) as raised:
            preflight_output_hook(source, req, resources)
        self.assertIn("natural-goal", str(raised.exception))
        self.assertEqual(raised.exception.code, "CODE_RUNTIME")

    def test_diagnosis_uses_real_host_contract(self):
        client = ScriptClient([{"summary": "source", "repair_scope": "code", "failure_points": []}])
        AdaptivePlanner(client).diagnose(self.req, self.spec, WRONG_LAYER, [{"failure": {
            "origin": "source", "retryable": False, "code": "OUTPUT_CONTRACT"}}])
        self.assertEqual(client.contexts[0]["response_contract"]["exact_output_fields"],
                         ["answer", "data", "sources", "abstained", "error"])
        self.assertIn("result_ref", client.contexts[0]["response_contract"]["decision_only_fields"])


if __name__ == "__main__":
    unittest.main()
