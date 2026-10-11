"""Design repair preserves business acceptance; flexible execution stays bounded."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.adaptive_planner import AdaptivePlanner
from agentforge.codex_client import CodexClient, DeadlineExceeded, object_schema
from agentforge.contracts import ExecutionError
from agentforge.factory import FactoryAgent
from agentforge.resources import read_json, fingerprint, write_json
from agentforge.executions import create_execution, execute_record, continue_execution
from test_adaptive import AdaptiveFixturePlanner, DocClient, ScriptClient, decision, quality_spec, QUALITY
from test_codex_factory import DOCUMENTS, GOOD_SOURCE, doc_suite
from test_runtime import csv_fixture


class DesignPlanner(AdaptiveFixturePlanner):
    def code(self, req, spec, development, resources, tool_development):
        assert all(c["partition"] == "development" for c in [*development, *tool_development])
        tool = spec.custom_tools[0]
        if "characters" in tool["output_schema"]["properties"]:
            source = ('def run(context):\n    text = get(context["input"], "text", None)\n'
                      '    if text is None:\n        text = document(context["input"]["doc_id"])["content"]\n'
                      '    return {"statements": len(split(text, "。")) - 1, "characters": len(text)}\n')
        else:
            source = 'def run(context):\n    return {"statements": 99}\n'
        return {"spec": replace(spec, custom_tools=[tool | {"source": source}]),
                "source": GOOD_SOURCE, "explanation": "Implement current interface"}

    def diagnose(self, *args):
        return {"summary": "工具接口需要提供结构化文档信息", "repair_scope": "design",
                "failure_points": [{"location": "agent.json", "cause": "接口不完整", "fix": "重设文档结构工具"}]}

    def redesign(self, req, spec, failures, diagnosis, direction, resources):
        tool = spec.custom_tools[0] | {"description": "Count statements and characters in a document",
                "parameters": [{"name": "doc_id", "type": "string", "required": True, "description": "Document ID"},
                               {"name": "text", "type": "string", "required": False, "description": "Optional literal test text"}],
                "output_schema": object_schema({"statements": {"type": "integer"}, "characters": {"type": "integer"}}), "source": ""}
        return replace(spec, custom_tools=[tool])

    def extra_tests(self, req, resources, definitions, errors, previous):
        rid = resources["documents"][0]["id"]
        expected = {"statements": 2, "characters": len(resources["documents"][0]["content"])}
        return {"tool_cases": [{"id": p + "-new-interface", "partition": p, "tool": definitions[0]["id"],
                                "input_json": json.dumps({"doc_id": rid} | ({"text": "甲。乙。"} if p == "acceptance" else {})),
                                "expected_json": json.dumps({"statements": 2, "characters": 4} if p == "acceptance" else expected),
                                "purpose": "Document structure", "criterion": "Count literal source"}
                               for p in ("development", "acceptance")] + [
                            {"id": "development-empty-text", "partition": "development", "tool": definitions[0]["id"],
                             "input_json": json.dumps({"doc_id": rid, "text": ""}),
                             "expected_json": json.dumps({"statements": 0, "characters": 0}),
                             "purpose": "Empty text", "criterion": "Zero structure counts"}] + [
                            {"id": p + "-host", "partition": p, "tool": definitions[0]["id"], "input_json": '{"doc_id": 1}',
                             "expected_json": None, "expected_error": {"code": "INVALID_ARGUMENTS", "origin": "decision", "layer": "host"},
                             "purpose": "Host type", "criterion": "Reject"} for p in ("development", "acceptance")],
                "goal_cases": [{"id": p + "-discarded-goal", "partition": p, "goal": "解释费用规定", "parameters_json": "{}",
                                "reference_query_json": "{}", "source_id": rid, "facts": ["每天 80 元"], "refusal": False,
                                "purpose": "Policy", "criterion": "Evidence"} for p in ("development", "acceptance")]}

    decode_tests = staticmethod(AdaptivePlanner.decode_tests)
    redesign_tests = AdaptivePlanner.redesign_tests


class DesignDocClient(DocClient):
    def complete(self, task, context, schema):
        if len(context["observations"]) == 1:
            tool = next(t for t in context["tools"] if t["id"].startswith("custom."))
            return decision(tool=tool["id"], args={tool["parameters"][0]["name"]: context["resources"]["documents"][0]["id"]})
        return super().complete(task, context, schema)


class DesignRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.factory = FactoryAgent(self.root, DesignPlanner(), runtime_client_factory=DesignDocClient)

    def tearDown(self):
        self.temp.cleanup()

    def test_invalid_schema_feedback_identifies_tool_and_json_position(self):
        from agentforge.adaptive_planner import decode_design
        with self.assertRaisesRegex(ValueError, "custom.profile.output_schema_json 在字符"):
            decode_design({"custom_tools": [{"id": "custom.profile", "output_schema_json": '{"type":"object","properties":{'}]})

    def test_changed_interfaces_deliver_without_editing_business_tests_or_history(self):
        state = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["design_repairs"], 1)
        self.assertEqual(state["version"], 2)
        base = self.factory.run_dir(state["run_id"])
        original = read_json(base / "test_suite.json")
        revised = self.factory.suite(state)
        for key in ("cases", "goal_cases"):
            self.assertEqual(revised[key], original[key])
        self.assertNotEqual(revised["tool_cases"], original["tool_cases"])
        self.assertEqual(fingerprint(original), state["frozen_hashes"]["test_suite"])
        initial = read_json(base / "versions/v1/development.json")
        self.assertFalse(initial["all_passed"])
        self.assertTrue(all(not c["passed"] for c in initial["cases"] if c["kind"] == "custom_tool"))
        self.assertFalse(read_json(base / "versions/v1/change.json")["design_repair"])
        self.assertTrue(read_json(base / "versions/v2/change.json")["design_repair"])
        result = subprocess.run([sys.executable, "-X", "utf8", "verify_agent.py"], cwd=state["export_path"],
                                capture_output=True, text=True, encoding="utf8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_json(Path(state["export_path"]) / "business_suite.json"), original)

    def test_redesign_cannot_silently_drop_an_acceptance_scenario(self):
        original = self.factory.planner.redesign_tests
        def weaken(*args):
            suite = original(*args)
            suite["cases"] = [c for c in suite["cases"] if c["partition"] == "development"]
            return suite
        self.factory.planner.redesign_tests = weaken
        state = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "redesign_tests")
        self.assertEqual(state["version"], 1)
        self.assertIn("不能修改业务测试", state["last_error"])
        self.assertIsNone(state["export_path"])

    def test_failed_redesign_stage_resumes_without_rewriting_previous_version(self):
        original = self.factory.planner.redesign_tests
        self.factory.planner.redesign_tests = lambda *args: (_ for _ in ()).throw(DeadlineExceeded("time limit"))
        state = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS))
        self.assertEqual(state["status"], "time_limit")
        self.assertEqual(state["phase"], "redesign_tests")
        old = (self.factory.run_dir(state["run_id"]) / "versions/v1/agent.json").read_bytes()
        self.factory.planner.redesign_tests = original
        state = self.factory.retry(state["run_id"], max_seconds=0)
        self.assertEqual(state["status"], "completed")
        self.assertEqual((self.factory.run_dir(state["run_id"]) / "versions/v1/agent.json").read_bytes(), old)

    def test_active_business_suite_and_pending_contract_tampering_is_rejected(self):
        state = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS))
        suite_path = self.factory.run_dir(state["run_id"]) / state["active_design"]["path"] / "test_suite.json"
        suite = read_json(suite_path)
        suite["cases"].pop()
        write_json(suite_path, suite)
        with self.assertRaisesRegex(ValueError, "冻结"):
            self.factory.load(state["run_id"])

    def test_no_progress_escalates_code_repair_to_design(self):
        self.factory.planner.diagnose = lambda *args: {"summary": "实现问题", "repair_scope": "code", "failure_points": []}
        def repair(req, spec, source, *args):
            return spec, source, "Retry same implementation"
        self.factory.planner.repair = repair
        state = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["repairs"], 2)
        self.assertEqual(state["design_repairs"], 1)

    def test_stopped_build_can_extend_repairs_and_choose_design_without_recreating_task(self):
        state = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS, max_repairs=0))
        self.assertEqual(state["status"], "stopped")
        original_hashes = dict(state["frozen_hashes"])
        with self.assertRaisesRegex(ValueError, "max-repairs"):
            self.factory.retry(state["run_id"])
        finished = self.factory.retry(state["run_id"], max_repairs=2, repair_scope="design", direction="简化工具，保留业务验收")
        self.assertEqual(finished["run_id"], state["run_id"])
        self.assertEqual(finished["status"], "completed")
        for key, value in original_hashes.items():
            self.assertEqual(finished["frozen_hashes"][key], value)
        self.assertNotIn("repair_scope_override", finished)

    def test_tool_only_failure_does_not_compare_against_full_behavior_ratio(self):
        state = self.factory.create("政策问答", uploads=DOCUMENTS)
        state["versions"] = [{"version": 1, "passed": 11, "total": 13}, {"version": 2, "passed": 3, "total": 4}]
        base = self.factory.run_dir(state["run_id"]) / "versions"
        write_json(base / "v1/development.json", {"suite_hash": "same", "passed": 11, "total": 13,
                   "behavior_skipped": False, "cases": [{"id": "tool"}, {"id": "business"}]})
        write_json(base / "v2/development.json", {"suite_hash": "same", "passed": 3, "total": 4,
                   "behavior_skipped": True, "cases": [{"id": "tool"}]})
        self.assertFalse(self.factory.repair_stalled(state))

    def test_new_interface_tests_do_not_trigger_stagnation_from_incomparable_scores(self):
        state = self.factory.create("政策问答", uploads=DOCUMENTS)
        state["versions"] = [{"version": 1, "passed": 1, "total": 2}, {"version": 2, "passed": 0, "total": 2}]
        base = self.factory.run_dir(state["run_id"]) / "versions"
        for version in (1, 2):
            write_json(base / ("v" + str(version)) / "development.json", {"suite_hash": "interface-" + str(version),
                       "passed": 2 - version, "total": 2, "cases": [{"id": "one"}, {"id": "two"}]})
        self.assertFalse(self.factory.repair_stalled(state))

    def test_exhausted_time_keeps_current_stage_and_can_be_extended(self):
        state = self.factory.create("政策问答", uploads=DOCUMENTS, max_seconds=1)
        state["performance"]["elapsed_ms"] = 1500
        self.factory.save(state)
        stopped = self.factory.drive(state)
        self.assertEqual(stopped["status"], "time_limit")
        self.assertEqual(stopped["phase"], "requirements")
        resumed = self.factory.retry(stopped["run_id"], max_seconds=0)
        self.assertEqual(resumed["status"], "completed")


class RuntimePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.req, self.resources, _ = csv_fixture()
        self.spec = quality_spec()
        self.args = {"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, decisions, spec=None):
        self.client = ScriptClient(decisions)
        return AdaptiveAgent(spec or self.spec, self.req, self.resources, GOOD_SOURCE, self.root, lambda: self.client)

    def test_progress_earns_more_steps_and_completion_has_explicit_requirements(self):
        spec = replace(self.spec, max_steps=3, execution_policy={"adaptive_steps": True})
        agent = self.agent([decision(tool="csv.preview"), decision(tool="custom.quality", args=self.args),
                            decision(tool="csv.query"), decision("finish", result_ref="o3")], spec)
        output = agent.run({})
        self.assertIsNone(output["error"])
        self.assertEqual(output["model_calls"], 4)
        self.assertTrue(any(e["type"] == "budget_extension" for e in output["trace"]))
        self.assertEqual(self.client.contexts[-1]["completion_requirements"]["missing_required_tools"], [])

    def test_repeating_without_progress_does_not_extend_calls(self):
        spec = replace(self.spec, max_steps=3, execution_policy={"adaptive_steps": True})
        result = self.agent([decision(tool="csv.preview") for _ in range(3)], spec).run({})
        self.assertEqual(result["error"]["code"], "AGENT_BUDGET")
        self.assertEqual(result["model_calls"], 3)

    def test_builtin_observations_are_reused_without_rereading_data(self):
        agent = self.agent([decision(tool="csv.preview"), decision(tool="csv.preview"),
                            decision(tool="custom.quality", args=self.args), decision(tool="csv.query"),
                            decision("finish", result_ref="o4")])
        with patch.object(agent.tools, "invoke", wraps=agent.tools.invoke) as invoke:
            result = agent.run({})
        self.assertIsNone(result["error"])
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(result["observations"][1]["cached_from"], "o1")

    def test_representation_normalization_preserves_actual_computation(self):
        result = self.agent([decision(tool="custom.quality", args=self.args), decision(tool="csv.query"),
                            decision("finish", result_ref="csv.query", sources=[{"id": "csv", "quote": "bad citation"}])]).run({})
        self.assertIsNone(result["error"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["data"], result["observations"][1]["result"])
        self.assertTrue(any(e["type"] == "normalization" for e in result["trace"]))

    def test_ambiguous_reference_is_not_guessed(self):
        agent = self.agent([])
        observations = [{"id": "o1", "tool": "csv.query", "status": "ok", "arguments": {}},
                        {"id": "o2", "tool": "csv.query", "status": "ok", "arguments": {"limit": 1}}]
        normalized, changes = agent.normalize_finish(decision("finish", result_ref="csv.query"), observations, {}, True)
        self.assertEqual(normalized["result_ref"], "csv.query")
        self.assertEqual(changes, [])

    def test_runtime_time_limit_keeps_evidence_and_resume_does_not_replay(self):
        agent = self.agent([decision(tool="custom.quality", args=self.args), DeadlineExceeded("limit")])
        first = agent.run({})
        self.assertEqual(first["error"]["code"], "TASK_TIMEOUT")
        self.assertEqual(first["checkpoint"]["phase"], "time_limit")
        result = self.agent([decision(tool="csv.query"), decision("finish", result_ref="o2")]).run({}, checkpoint=first["checkpoint"], max_seconds=0)
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["observations"]), 2)

    def test_crash_checkpoint_records_active_time_before_model_request(self):
        snapshots = []
        def crash(value):
            snapshots.append(value)
            raise RuntimeError("crash")
        with patch("agentforge.adaptive_runtime.time.perf_counter", side_effect=[0, 2]), self.assertRaises(RuntimeError):
            self.agent([]).run({}, on_checkpoint=crash)
        self.assertEqual(snapshots[-1]["elapsed_ms"], 2000)
        output = self.agent([]).run({}, checkpoint=snapshots[-1], max_seconds=1)
        self.assertEqual(output["error"]["code"], "TASK_TIMEOUT")
        self.assertEqual(output["new_model_calls"], 0)

    def test_source_contract_retry_uses_exact_ids_and_development_only(self):
        bad = {"source": GOOD_SOURCE, "custom_sources": [{"id": "custom.wrong", "source": QUALITY}], "explanation": "wrong id"}
        good = {"source": GOOD_SOURCE, "custom_sources": [{"id": "custom.quality", "source": QUALITY}], "explanation": "correct id"}
        client = ScriptClient([bad, good])
        planner = AdaptivePlanner(client)
        result = planner.code(self.req, self.spec, [], self.resources, [])
        self.assertEqual(result["spec"].custom_tools[0]["id"], "custom.quality")
        self.assertEqual(len(planner.code_attempts), 1)
        self.assertIn("custom.wrong", client.contexts[-1]["previous_generation_errors"][0])
        self.assertEqual(client.contexts[-1]["expected_custom_ids"], ["custom.quality"])
        self.assertNotIn("acceptance", client.contexts[-1])

    def test_generated_test_labels_normalize_without_changing_oracles(self):
        from agentforge.planner import Planner
        from agentforge.contracts import Requirements
        from agentforge.resources import prepare_resources
        resources = prepare_resources(uploads=DOCUMENTS)
        req = Requirements("政策问答", "document_qa", "政策", ["引用"], {})
        original = doc_suite(req, resources)
        raw = copy.deepcopy(original)
        for case in raw["cases"]:
            case["id"] = "重复中文标识"
            case["input_json"] = json.dumps(case.pop("input"))
        client = ScriptClient([raw])
        suite = Planner(client).tests(req, resources)
        cases = suite["cases"][:len(original["cases"])]
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for before, after in zip(original["cases"], cases):
            self.assertEqual(after["generated_id"], "重复中文标识")
            for key in ("input", "facts", "source_id", "partition", "expect", "criterion"):
                self.assertEqual(after[key], before[key])

    def test_new_tool_generation_rejects_duplicate_development_inputs(self):
        definitions = [{"id": "custom.profile"}]
        suite = {"tool_cases": [{"id": "dev0", "tool": "custom.profile", "partition": "development", "input": {"rows": []}},
                                {"id": "dev1", "tool": "custom.profile", "partition": "development", "input": {"rows": []}},
                                {"id": "accept0", "tool": "custom.profile", "partition": "acceptance", "input": {"rows": [1]}}]}
        suite["tool_cases"].extend({"id": p + "host", "tool": "custom.profile", "partition": p, "input": {"rows": "bad"},
                                    "expected_error": {"code": "INVALID_ARGUMENTS"}} for p in ("development", "acceptance"))
        with self.assertRaisesRegex(ValueError, "合法计算"):
            AdaptivePlanner.validate_generated_tool_coverage(suite, definitions)
        suite["tool_cases"][1]["input"] = {"rows": [1, 2]}
        AdaptivePlanner.validate_generated_tool_coverage(suite, definitions)

    def test_coverage_checks_each_new_tool_separately(self):
        suite = {"tool_cases": [{"id": "dev" + str(v), "tool": "custom.first", "partition": "development", "input": {"value": v}}
                                for v in (1, 2)] + [{"id": "accept", "tool": "custom.first", "partition": "acceptance", "input": {"value": 3}}]}
        suite["tool_cases"].extend({"id": p + "host", "tool": "custom.first", "partition": p, "input": {},
                                    "expected_error": {"code": "INVALID_ARGUMENTS"}} for p in ("development", "acceptance"))
        with self.assertRaisesRegex(ValueError, "custom.second"):
            AdaptivePlanner.validate_generated_tool_coverage(suite, [{"id": "custom.first"}, {"id": "custom.second"}])

    def test_bad_nested_data_is_diagnosable_instead_of_crashing_the_host(self):
        from agentforge.custom_tools import CustomTool
        spec = quality_spec()
        tool = spec.custom_tools[0] | {"source": 'def run(context):\n    return get("not-an-object", "value", 0)\n'}
        with self.assertRaises(ExecutionError) as raised:
            CustomTool(tool, self.resources).invoke({"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}, [], self.req)
        self.assertEqual(raised.exception.code, "CODE_RUNTIME")
        self.assertEqual(raised.exception.location, "custom_tools/quality.py:2")

    def test_source_generation_corrects_invalid_syntax_before_saving_a_version(self):
        invalid = {"source": GOOD_SOURCE, "custom_sources": [{"id": "custom.quality", "source":
                   'def run(context):\n    while True:\n        return {}\n'}], "explanation": "Bad syntax"}
        valid = {"source": GOOD_SOURCE, "custom_sources": [{"id": "custom.quality", "source": QUALITY}], "explanation": "Valid code"}
        client = ScriptClient([invalid, valid])
        planner = AdaptivePlanner(client)
        result = planner.code(self.req, quality_spec(), [], self.resources)
        self.assertEqual(result["spec"].custom_tools[0]["source"], QUALITY)
        self.assertIn("custom_tools/quality.py:2", client.contexts[1]["previous_generation_errors"][0])
        self.assertIn("While", client.contexts[1]["previous_generation_errors"][0])
        self.assertNotIn("spec", planner.code_attempts[0]["generation"])
        json.dumps(planner.code_attempts)

    def test_pure_type_helpers_handle_mixed_json_values(self):
        from agentforge.program import Program
        source = ('def run(context):\n    valid = []\n'
                  '    for item in context["input"]:\n'
                  '        if is_object(item):\n'
                  '            text = get(item, "text", None)\n'
                  '            if is_string(text):\n'
                  '                append(valid, text)\n'
                  '    return {"texts": valid, "array": is_array(context["input"])}\n')
        values = [{"text": "证据"}, {"text": 7}, "not-an-object", None, [], {"text": ""}]
        self.assertEqual(Program(source).run({"input": values}), {"texts": ["证据", ""], "array": True})

    def test_deadline_expired_does_not_start_another_model_request(self):
        agent = self.agent([decision("ask_user", answer="分析哪一列？")])
        first = agent.run({"goal": "分析"})
        first["checkpoint"]["elapsed_ms"] = 2000
        agent = self.agent([])
        result = agent.run({"goal": "分析"}, checkpoint=first["checkpoint"], reply={"text": "金额"}, max_seconds=1)
        self.assertEqual(result["error"]["code"], "TASK_TIMEOUT")
        self.assertEqual(result["new_model_calls"], 0)
        self.assertEqual(self.client.contexts, [])

    def test_client_deadline_clamps_subprocess_timeout(self):
        client = CodexClient(self.root, timeout=180)
        import time
        client.deadline = time.monotonic() + 4
        with patch("agentforge.codex_client.resolve_codex_binary", return_value="codex"), patch("agentforge.codex_client.subprocess.run") as run:
            client._execute(["exec"])
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 4)

    def test_persisted_time_limit_accepts_larger_budget_and_preserves_observations(self):
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        state = factory.drive(factory.create("政策问答", uploads=DOCUMENTS))
        rid = factory.resources(state)["documents"][0]["id"]
        factory.runtime_client_factory = lambda: ScriptClient([
            decision(tool="documents.read", args={"ids": [rid]}), DeadlineExceeded("limit")])
        execution = create_execution(factory, state["run_id"], "政策？")
        stopped = execute_record(factory, state["run_id"], execution["execution_id"])
        self.assertEqual(stopped["status"], "time_limit")
        continue_execution(factory, state["run_id"], execution["execution_id"], max_seconds=0)
        factory.runtime_client_factory = DocClient
        completed = execute_record(factory, state["run_id"], execution["execution_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["model_calls"], 4)
        self.assertEqual(len(completed["result"]["observations"]), 2)
        self.assertEqual(completed["max_seconds"], 0)

    def test_lease_heartbeat_extends_only_current_owner(self):
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner())
        state = factory.create("政策问答", uploads=DOCUMENTS)
        factory.store.acquire(state["run_id"], "owner-a")
        factory.store.renew(state["run_id"], "owner-a")
        self.assertTrue(factory.store.active(state["run_id"]))
        with self.assertRaisesRegex(ValueError, "租约"):
            factory.store.renew(state["run_id"], "owner-b")
        factory.store.release(state["run_id"], "owner-a")

    def test_v2_csv_gold_matches_complete_independent_decimal_oracle(self):
        from agentforge.benchmarking import load_suite
        from agentforge.resources import prepare_resources
        from agentforge.contracts import Requirements
        from agentforge.testing import csv_oracle
        suite = load_suite(Path(__file__).resolve().parents[1] / "agentforge/benchmarks/unseen_v2.json")
        task = suite["tasks"][1]
        resources = prepare_resources(uploads=task["files"])
        defaults = {"dataset_id": resources["datasets"][0]["id"], "operation": "sum", "column": "net_units", "group_by": "warehouse",
                    "filters": [], "sort_by": "value", "descending": True, "limit": None}
        req = Requirements(task["request"], "csv_analysis", "Shipments", ["Full data"], defaults)
        queries = [{"limit": 2}, task["cases"][1]["input"],
                   {"filters": [{"column": "status", "operator": "eq", "value": "returned"}], "sort_by": "group", "descending": False},
                   task["cases"][3]["input"], task["cases"][4]["input"], {}, task["cases"][6]["input"]]
        for case, query in zip(task["cases"], queries):
            self.assertEqual(csv_oracle(req, resources, query), {"dataset_id": defaults["dataset_id"], **case["gold"]}, case["id"])


if __name__ == "__main__":
    unittest.main()
