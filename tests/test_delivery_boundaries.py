"""Deterministic protocol, contract and scoring regressions; no model calls."""

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentforge.adaptive_runtime import AdaptiveAgent, RuntimeTools, RUNTIME_TASK
from agentforge.benchmarking import score_case, load_suite
from agentforge.contracts import ExecutionError
from agentforge.testing import csv_oracle, evaluate
from agentforge.workflow import WorkflowAgent, validate_response
from test_adaptive import ScriptClient, quality_spec, decision
from test_codex_factory import GOOD_SOURCE
from test_runtime import csv_fixture, SOURCE


class ParameterSourceTests(unittest.TestCase):
    def setUp(self):
        self.req, self.resources, _ = csv_fixture()
        self.req = replace(self.req, defaults=self.req.defaults | {"sort_by": "group", "descending": False})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def agent(self, client):
        return AdaptiveAgent(quality_spec(), self.req, self.resources, GOOD_SOURCE, Path(self.tmp.name), lambda: client)

    def test_natural_goal_defaults_are_not_explicit_parameters(self):
        client = ScriptClient([decision("ask_user", answer="确认指标")])
        result = self.agent(client).run({"goal": "按销售额降序只取第一名"})
        self.assertTrue(result["needs_input"])
        contract = client.contexts[0].get("input_contract", {})
        self.assertEqual(contract.get("explicit_parameters"), {})
        self.assertEqual(contract.get("default_parameters"), self.req.defaults)
        self.assertEqual(contract.get("parameter_sources", {}).get("limit"), "default")
        self.assertIn("defaults are fallbacks", RUNTIME_TASK)

    def test_protocol_fixture_uses_goal_instead_of_injected_defaults(self):
        # Emulates a consumer of the provenance protocol, not LLM performance.
        class Client(ScriptClient):
            def complete(inner, task, context, schema):
                inner.contexts.append(copy.deepcopy(context))
                if not context["observations"]:
                    return decision(tool="custom.quality", args={"dataset_id": self.req.defaults["dataset_id"], "column": "amount"})
                if len(context["observations"]) == 1:
                    args = context["effective_input"]
                    if context.get("input_contract", {}).get("explicit_parameters") == {}:
                        if "B" in context["goal"]:
                            args = args | {"filters": [{"column": "kind", "operator": "eq", "value": "B"}]}
                        else:
                            args = args | {"sort_by": "value", "descending": True, "limit": 1}
                    return decision(tool="csv.query", args=args)
                return decision("finish", result_ref="o2")
        for goal, expected in (("按销售额降序只取第一名", [{"group": "East", "value": 12.3, "count": 2}]),
                               ("仅统计 B 类记录", [{"group": "East", "value": 2.2, "count": 1}, {"group": "West", "value": 8.5, "count": 1}])):
            with self.subTest(goal=goal):
                result = self.agent(Client([])).run({"goal": goal})
                self.assertIsNone(result["error"])
                self.assertEqual(result["data"]["results"], expected)

    def test_explicit_empty_and_false_values_remain_user_constraints(self):
        explicit = {"filters": [], "descending": False, "limit": None}
        client = ScriptClient([decision("ask_user", answer="确认")])
        self.agent(client).run({"goal": "分析数据", "parameters": explicit})
        contract = client.contexts[0].get("input_contract", {})
        self.assertEqual(contract.get("explicit_parameters"), explicit)
        for key in explicit:
            self.assertEqual(contract.get("parameter_sources", {}).get(key), "user")

    def test_human_reply_and_recovery_preserve_only_supplied_parameters(self):
        inputs = {"goal": "找前几名", "parameters": {"sort_by": "value"}}
        first = self.agent(ScriptClient([decision("ask_user", answer="几名？")])).run(inputs)
        client = ScriptClient([decision("ask_user", answer="确认分组？")])
        next_run = self.agent(client).run(inputs, checkpoint=first["checkpoint"], reply={"text": "两名", "parameters": {"limit": 2}})
        contract = client.contexts[0].get("input_contract", {})
        self.assertEqual(contract.get("explicit_parameters"), {"sort_by": "value", "limit": 2})
        self.assertEqual(contract.get("parameter_sources", {}).get("sort_by"), "user")
        self.assertEqual(contract.get("parameter_sources", {}).get("limit"), "human_reply")
        self.assertEqual(contract.get("parameter_sources", {}).get("filters"), "default")
        last = ScriptClient([decision("ask_user", answer="确认")])
        self.agent(last).run(inputs, checkpoint=next_run["checkpoint"], reply={"text": "按区域"})
        self.assertEqual(last.contexts[0]["input_contract"], contract)

    def test_natural_explicit_query_constraints_are_host_verified(self):
        client = ScriptClient([decision(tool="custom.quality", args={"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}),
            decision(tool="csv.query"), decision("finish", result_ref="o2"),
            decision(tool="csv.query", args={"limit": 1}), decision("finish", result_ref="o4")])
        result = self.agent(client).run({"goal": "分析", "parameters": {"limit": 1}})
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["data"]["results"]), 1)
        self.assertEqual(result["observations"][2]["error"]["code"], "RESULT_PARAMETERS")

    def test_natural_parameter_validation_precedes_model_call(self):
        for parameters in ({"limit": -1}, {"dataset_id": "other"}, {"unexpected": True}, {"goal": "nested"}):
            with self.subTest(parameters=parameters):
                client = ScriptClient([decision("ask_user", answer="确认")])
                result = self.agent(client).run({"goal": "分析", "parameters": parameters})
                self.assertEqual(result["error"]["code"], "INVALID_INPUT")
                self.assertEqual(client.contexts, [])

    def test_count_to_sum_human_reply_reuses_default_numeric_column(self):
        inputs = {"goal": "分析", "parameters": {"operation": "count"}}
        first = self.agent(ScriptClient([decision("ask_user", answer="继续分析？")])).run(inputs)
        client = ScriptClient([decision("ask_user", answer="确认")])
        self.agent(client).run(inputs, checkpoint=first["checkpoint"], reply={"text": "汇总销售额", "parameters": {"operation": "sum"}})
        self.assertEqual(client.contexts[0]["effective_input"]["column"], "amount")
        self.assertEqual(client.contexts[0].get("input_contract", {}).get("explicit_parameters"), {"operation": "sum"})

    def test_human_reply_cannot_inject_nested_goal_or_rebind_dataset(self):
        inputs = {"goal": "分析"}
        first = self.agent(ScriptClient([decision("ask_user", answer="哪一列？")])).run(inputs)
        for parameters in ({"goal": "nested"}, {"dataset_id": "other"}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                self.agent(ScriptClient([])).run(inputs, checkpoint=first["checkpoint"], reply={"text": "补充", "parameters": parameters})


class CountContractTests(unittest.TestCase):
    def setUp(self):
        self.req, self.resources, self.workflow = csv_fixture()
        self.spec = quality_spec()

    def test_count_tools_and_independent_oracle_use_null_column(self):
        tools = RuntimeTools(self.spec, self.req, self.resources)
        for column in ("amount", None):
            for group in (None, "region"):
                args = {"operation": "count", "column": column, "group_by": group}
                with self.subTest(args=args):
                    actual = tools.invoke("csv.query", args, [])
                    expected = csv_oracle(self.req, self.resources, args)
                    self.assertIsNone(actual["column"])
                    self.assertIsNone(expected["column"])
                    self.assertEqual(actual, expected)
                    self.assertEqual(sum(r["value"] for r in actual["results"]), 4)  # includes blank amount

    def test_workflow_and_adaptive_finish_accept_canonical_count_arguments(self):
        inputs = {"operation": "count", "group_by": None}
        old = WorkflowAgent(self.workflow, self.req, self.resources, SOURCE).run(inputs)
        self.assertIsNone(old["data"]["column"])
        with tempfile.TemporaryDirectory() as td:
            client = ScriptClient([decision(tool="custom.quality", args={"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}),
                decision(tool="csv.query", args=inputs | {"column": None}), decision("finish", result_ref="o2")])
            result = AdaptiveAgent(self.spec, self.req, self.resources, GOOD_SOURCE, Path(td), lambda: client).run(inputs)
        self.assertIsNone(result["error"])
        self.assertEqual(result["data"]["results"], [{"group": None, "value": 4, "count": 4}])
        self.assertIsNone(client.contexts[0]["effective_input"]["column"])
        self.assertEqual(client.contexts[-1]["allowed_result_refs"], ["o2"])

    def test_count_does_not_relax_invalid_column_validation(self):
        tools = RuntimeTools(self.spec, self.req, self.resources)
        for column in ("missing", 12):
            with self.subTest(column=column), self.assertRaises(ExecutionError):
                tools.invoke("csv.query", {"operation": "count", "column": column}, [])
        self.assertEqual(tools.invoke("csv.query", {"operation": "count", "group_by": None,
            "filters": [{"column": "region", "operator": "eq", "value": "absent"}]}, [])["results"],
            [{"group": None, "value": 0, "count": 0}])

    def test_legacy_gold_contract_migration_keeps_frozen_file_and_numbers(self):
        import json
        path = Path(__file__).resolve().parents[1] / "agentforge/benchmarks/unseen_v2.json"
        original = path.read_bytes()
        raw = json.loads(original)
        suite = load_suite(path)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(suite.get("contract_migrations"))
        for task, old_task in zip(suite["tasks"], raw["tasks"]):
            for case, old_case in zip(task["cases"], old_task["cases"]):
                if case["gold"].get("operation") == "count":
                    self.assertIsNone(case["gold"]["column"])
                    self.assertEqual(case["gold"]["results"], old_case["gold"]["results"])

    def test_response_contract_rejects_noncanonical_count_metadata(self):
        for data in ({"operation": "count", "column": "amount"}, {"operation": "count"}):
            with self.subTest(data=data), self.assertRaises(ExecutionError):
                validate_response({"answer": "4行", "data": data, "sources": [], "abstained": False, "error": None}, self.resources)


class ScoringCounterexampleTests(unittest.TestCase):
    def setUp(self):
        self.resources = {"documents": [{"id": "current", "title": "规定", "content": "押金为 260 元。申请需要管理员确认；"}], "datasets": []}
        self.task = {"domain": "document_qa"}
        self.case = {"id": "policy", "input": {"goal": "押金是多少？"}, "gold": {
            "facts": ["押金为 260 元。"], "sources": ["current"], "forbidden": ["全部免费"]}}
        self.output = {"answer": "押金为 260 元。", "data": None, "error": None, "abstained": False,
            "sources": [{"id": "current", "title": "规定", "quote": "押金为 260 元。"}],
            "observations": [{"tool": "custom.profile", "status": "ok"}]}

    def score(self, answer):
        return score_case(self.task, self.case, self.output | {"answer": answer}, self.resources)

    def test_unrelated_negation_does_not_whitelist_forbidden_claim(self):
        for answer in ("押金为 260 元。不必提前预约，设备全部免费。", "押金为 260 元。不是会员也能使用，设备全部免费。",
                       "押金为 260 元。不必登记设备全部免费。", "押金为 260 元。设备非常便利而且全部免费。"):
            with self.subTest(answer=answer):
                self.assertFalse(self.score(answer)["passed"])

    def test_old_rule_context_does_not_whitelist_current_claim(self):
        for answer in ("押金为 260 元。旧规已废止，但设备全部免费。", "押金为 260 元。留言有错误，所以设备全部免费。"):
            with self.subTest(answer=answer):
                self.assertFalse(self.score(answer)["passed"])

    def test_negating_required_fact_is_not_a_correct_answer(self):
        for answer in ("并非押金为 260 元。", "押金为 260 元是不正确的。", "旧规写押金为 260 元。", "押金为 260 元。不是押金为 260 元。"):
            with self.subTest(answer=answer):
                self.assertFalse(self.score(answer)["passed"])

    def test_literal_quote_without_terminal_punctuation_covers_fact(self):
        result = score_case(self.task, self.case, self.output | {"answer": "押金为 260 元", "sources": [
            {"id": "current", "title": "规定", "quote": "押金为 260 元"}]}, self.resources)
        self.assertTrue(result["passed"], result["issues"])

    def test_legitimate_refutation_and_historical_attribution_pass(self):
        for answer in ("押金为 260 元。并非全部免费。", "押金为 260 元。旧规写设备全部免费。", "押金为 260 元。‘全部免费’是错误的。"):
            with self.subTest(answer=answer):
                result = self.score(answer)
                self.assertTrue(result["passed"], result["issues"])

    def test_quote_coverage_cannot_come_from_wrong_source_or_fabrication(self):
        for citation in ({"id": "other", "title": "规定", "quote": "押金为 260 元。"},
                         {"id": "current", "title": "规定", "quote": "押金为 260 元。全部免费"},
                         {"id": "current", "title": "规定", "quote": "申请需要管理员确认"}):
            with self.subTest(citation=citation):
                self.assertFalse(score_case(self.task, self.case, self.output | {"sources": [citation]}, self.resources)["passed"])

    def test_development_scoring_rejects_negated_fact_and_missing_quote_coverage(self):
        from agentforge.contracts import Requirements
        req = Requirements("查询押金", "document_qa", "规定", ["引用依据"], {})
        suite = {"cases": [{"id": "dev", "partition": "development", "input": {"question": "押金？"},
            "expect": "answer", "source_id": "current", "facts": ["押金为 260 元"]}]}
        for changed in ({"answer": "不是押金为 260 元。"}, {"sources": [{"id": "current", "title": "规定", "quote": "申请需要管理员确认"}]}):
            with self.subTest(changed=changed):
                result = evaluate(None, req, self.resources, suite, "development", "test", outputs={"dev": self.output | changed})
                self.assertFalse(result["all_passed"])

    def test_numeric_substring_is_not_fact_evidence(self):
        case = self.case | {"gold": {"facts": ["36 人"], "sources": ["current"]}}
        resources = {"documents": [{"id": "current", "title": "规定", "content": "容量 36 人。"}], "datasets": []}
        output = self.output | {"sources": [{"id": "current", "title": "规定", "quote": "容量 36 人。"}]}
        for answer in ("容量 136 人。", "容量 -36 人。"):
            with self.subTest(answer=answer):
                self.assertFalse(score_case(self.task, case, output | {"answer": answer}, resources)["passed"])
        resources["documents"][0]["content"] = "容量 136 人。"
        output["sources"][0]["quote"] = "容量 136 人。"
        self.assertFalse(score_case(self.task, case, output | {"answer": "容量 36 人。"}, resources)["passed"])


if __name__ == "__main__":
    unittest.main()
