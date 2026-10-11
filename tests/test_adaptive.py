"""Model fixtures are test-only; these checks execute real generated source."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agentforge.adaptive_contracts import AdaptiveSpec
from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.codex_client import CodexError, object_schema
from agentforge.contracts import Requirements, ExecutionError
from agentforge.custom_tools import CustomTool
from agentforge.executions import create_execution, execute_record, execution_path
from agentforge.factory import FactoryAgent
from agentforge.adaptive_planner import AdaptivePlanner
from agentforge.resources import prepare_resources, read_json, fingerprint
from test_codex_factory import FixturePlanner, DOCUMENTS, GOOD_SOURCE, doc_suite
from test_runtime import csv_fixture


QUALITY = '''def run(context):
    rows = get(context["input"], "rows", None)
    if rows is None:
        rows = dataset(context["input"]["dataset_id"])["rows"]
    column = context["input"]["column"]
    missing = 0
    total = 0.0
    for row in rows:
        value = get(row, column, "")
        if value == "" or value is None:
            missing = missing + 1
        else:
            total = total + float(value)
    return {"rows": len(rows), "missing": missing, "total": round(total, 2)}
'''


def parameter(name, kind, required=True):
    return {"name": name, "type": kind, "required": required, "description": name}


def quality_spec():
    tool = {"id": "custom.quality", "description": "Count missing cells and compute the numeric total",
            "parameters": [parameter("dataset_id", "string"), parameter("column", "string"), parameter("rows", "array", False)],
            "output_schema": object_schema({"rows": {"type": "integer"}, "missing": {"type": "integer"}, "total": {"type": "number"}}),
            "required": True, "source": QUALITY}
    return AdaptiveSpec("质量分析", "csv_analysis", "Inspect quality and query aggregates; preserve user overrides.",
                        ["Inspect", "Compute", "Verify"], ["csv.preview", "csv.query", "custom.quality"], [tool], 8)


def decision(action="call_tool", tool=None, args=None, result_ref=None, answer="已完成", sources=None, abstained=False):
    return {"action": action, "tool": tool, "arguments_json": json.dumps(args or {}), "result_ref": result_ref,
            "answer": answer, "sources": sources or [], "abstained": abstained, "reason": "Use observed evidence", "plan": ["Inspect evidence", "Compute and verify"]}


class ScriptClient:
    last_metadata = {"model": "test_fixture"}
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.contexts = []
        self.schemas = []
    def complete(self, task, context, schema):
        self.contexts.append(copy.deepcopy(context))
        self.schemas.append(copy.deepcopy(schema))
        value = next(self.decisions)
        if isinstance(value, Exception):
            raise value
        return value


class AdaptiveRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.req, self.resources, _ = csv_fixture()
        self.spec = quality_spec()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.args = {"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}
    def tearDown(self):
        self.tmp.cleanup()
    def agent(self, decisions, source=GOOD_SOURCE, spec=None):
        self.client = ScriptClient(decisions)
        return AdaptiveAgent(spec or self.spec, self.req, self.resources, source, self.root, lambda: self.client)

    def test_generated_tool_computes_from_resources_and_new_arguments(self):
        tool = CustomTool(self.spec.custom_tools[0], self.resources)
        self.assertEqual(tool.invoke(self.args, [], self.req), {"rows": 4, "missing": 1, "total": 20.8})
        self.assertEqual(tool.invoke(self.args | {"rows": [{"amount": "3"}, {"amount": ""}, {"amount": "7"}]}, [], self.req),
                         {"rows": 3, "missing": 1, "total": 10.0})

    def test_loop_observes_tool_failure_and_replans(self):
        bad = self.args | {"dataset_id": "not-authorized"}
        events = []
        agent = self.agent([decision(tool="custom.quality", args=bad), decision(tool="custom.quality", args=self.args),
                            decision(tool="csv.query", args={}), decision("finish", result_ref="o3")])
        result = agent.run({}, lambda trace: events.append(trace))
        self.assertIsNone(result["error"])
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(self.client.contexts[1]["observations"][0]["error"]["code"], "UNKNOWN_RESOURCE")
        self.assertEqual(self.client.contexts[2]["observations"][1]["result"]["missing"], 1)
        self.assertEqual(result["data"]["results"][0]["group"], "East")
        self.assertGreater(len(events), 4)

    def test_finish_without_custom_tool_is_observed_and_corrected(self):
        agent = self.agent([decision("finish"), decision(tool="custom.quality", args=self.args),
                            decision(tool="csv.query"), decision("finish", result_ref="o3")])
        result = agent.run({})
        self.assertIsNone(result["error"])
        self.assertEqual(result["observations"][0]["error"]["code"], "MISSING_REQUIRED_TOOL")

    def test_query_cannot_ignore_user_override(self):
        agent = self.agent([decision(tool="custom.quality", args=self.args), decision(tool="csv.query"),
                            decision("finish", result_ref="o2"), decision(tool="csv.query", args={"limit": 1}),
                            decision("finish", result_ref="o4")])
        result = agent.run({"limit": 1})
        self.assertIsNone(result["error"])
        self.assertEqual(result["observations"][2]["error"]["code"], "RESULT_PARAMETERS")
        self.assertEqual(len(result["data"]["results"]), 1)

    def test_output_code_cannot_fabricate_numbers(self):
        source = 'def run(context):\n    value = context["result"]\n    value["data"]["results"][0]["value"] = 999\n    return value\n'
        with self.assertRaises(ExecutionError) as raised:
            self.agent([], source)
        self.assertEqual(raised.exception.origin, "source")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(self.client.contexts, [])

    def test_backend_error_has_no_fallback(self):
        result = self.agent([CodexError("connection unavailable")]).run({})
        self.assertEqual(result["error"]["code"], "MODEL_ERROR")
        self.assertEqual(result["observations"], [])

    def test_model_schema_only_offers_real_matching_query_references_and_no_csv_citations(self):
        from agentforge.codex_client import validate_json
        result = self.agent([decision(tool="custom.quality", args=self.args), decision(tool="csv.query"), decision("finish", result_ref="o2")]).run({})
        self.assertIsNone(result["error"])
        self.assertEqual(self.client.contexts[-1]["allowed_result_refs"], ["o2"])
        validate_json(decision("finish", result_ref="o2"), self.client.schemas[-1])
        with self.assertRaises(CodexError):
            validate_json(decision("finish", result_ref="csv.query"), self.client.schemas[-1])
        with self.assertRaises(CodexError):
            validate_json(decision("finish", result_ref="o2", sources=[{"id": "fake", "quote": "fake"}]), self.client.schemas[-1])

    def test_invalid_human_parameters_do_not_commit_or_consume_waiting_checkpoint(self):
        first = self.agent([decision("ask_user", answer="什么指标？")]).run({"goal": "分析"})
        saved = copy.deepcopy(first["checkpoint"])
        updates = []
        with self.assertRaises(ValueError):
            self.agent([]).run({"goal": "分析"}, checkpoint=saved, reply={"text": "金额", "parameters": {"limit": -1}}, on_checkpoint=updates.append)
        self.assertEqual(updates, [])
        self.assertEqual(saved, first["checkpoint"])

    def test_budget_and_repeat_limits(self):
        result = self.agent([decision(tool="csv.preview") for _ in range(8)]).run({})
        self.assertEqual(result["error"]["code"], "AGENT_BUDGET")
        self.assertEqual(result["observations"][2]["error"]["code"], "TOOL_REPEAT")

    def test_ask_user_and_bad_input(self):
        result = self.agent([decision("ask_user", answer="需要分析哪一列？")]).run({"goal": "分析"})
        self.assertTrue(result["needs_input"])
        self.assertEqual(result["error"]["code"], "NEEDS_INPUT")
        result = self.agent([]).run({"limit": -1})
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")
        self.assertEqual(result["model_calls"], 0)

    def test_custom_contracts_and_code_boundaries(self):
        tool = CustomTool(self.spec.custom_tools[0], self.resources)
        with self.assertRaises(ExecutionError):
            tool.invoke(self.args | {"unexpected": 1}, [], self.req)
        invalid = self.spec.custom_tools[0] | {"source": 'def run(context):\n    return open("secret")\n'}
        with self.assertRaises(ExecutionError):
            CustomTool(invalid, self.resources)
        wrong = self.spec.custom_tools[0] | {"source": 'def run(context):\n    return {"fake": 1}\n'}
        with self.assertRaises(ExecutionError) as caught:
            CustomTool(wrong, self.resources).invoke(self.args, [], self.req)
        self.assertEqual(caught.exception.code, "TOOL_OUTPUT_CONTRACT")

    def test_custom_tool_can_compute_from_a_prior_observation(self):
        definition = {"id": "custom.share", "description": "Compute a share from an observed total", "required": True,
                      "parameters": [parameter("ref", "string"), parameter("part", "number")],
                      "output_schema": object_schema({"share": {"type": "number"}}),
                      "source": 'def run(context):\n    total = observation(context["input"]["ref"])["result"]["total"]\n    return {"share": round(context["input"]["part"] / total, 4)}\n'}
        observations = [{"id": "o1", "tool": "custom.quality", "status": "ok", "arguments": {}, "result": {"total": 20}}]
        tool = CustomTool(definition, self.resources)
        self.assertEqual(tool.invoke({"ref": "o1", "part": 5}, observations, self.req), {"share": 0.25})
        with self.assertRaises(ExecutionError) as caught:
            tool.invoke({"ref": "o404", "part": 5}, observations, self.req)
        self.assertEqual(caught.exception.code, "UNKNOWN_OBSERVATION")

    def test_doc_citation_requires_an_actually_read_fragment(self):
        resources = prepare_resources(uploads=DOCUMENTS)
        req = Requirements("政策问答", "document_qa", "政策", ["来源"], {})
        spec = adaptive_doc_spec(resources)
        rid = resources["documents"][0]["id"]
        decisions = [decision(tool="documents.read", args={"ids": [rid], "limit": 5}),
                     decision(tool="custom.policy_profile", args={"document_id": rid})]
        decisions += [decision("finish", answer=DOCUMENTS[0]["content"], sources=[{"id": rid, "quote": DOCUMENTS[0]["content"]}]) for _ in range(4)]
        result = AdaptiveAgent(spec, req, resources, GOOD_SOURCE, self.root, lambda: ScriptClient(decisions)).run("发票时间？")
        self.assertEqual(result["error"]["code"], "AGENT_BUDGET")
        self.assertTrue(result["observations"][0]["result"][0]["has_more"])
        self.assertEqual(result["observations"][2]["error"]["code"], "PROVENANCE")


def adaptive_doc_spec(resources):
    tool = {"id": "custom.policy_profile", "description": "Count policy statements in an authorized document or optional literal text fixture",
            "parameters": [parameter("document_id", "string"), parameter("text", "string", False)], "required": True,
            "output_schema": object_schema({"statements": {"type": "integer"}}),
            "source": 'def run(context):\n    text = get(context["input"], "text", None)\n    if text is None:\n        text = document(context["input"]["document_id"])["content"]\n    return {"statements": len(split(text, "。")) - 1}\n'}
    return AdaptiveSpec("政策助手", "document_qa", "Read policies, compute profile, cite exact source facts.",
                        ["Read", "Profile", "Answer"], ["resources.list", "documents.read", "custom.policy_profile"], [tool], 6)


class DocClient:
    last_metadata = {"model": "test_fixture"}
    def complete(self, task, context, schema):
        doc = context["resources"]["documents"][0]
        obs = context["observations"]
        if not obs:
            return decision(tool="documents.read", args={"ids": [doc["id"]]})
        if len(obs) == 1:
            return decision(tool="custom.policy_profile", args={"document_id": doc["id"]})
        goal = context["goal"]
        if "zxqv" in goal:
            return decision("finish", abstained=True, answer="资料没有此信息。")
        return decision("finish", answer=DOCUMENTS[0]["content"], sources=[{"id": doc["id"], "quote": DOCUMENTS[0]["content"]}])


class HumanDocClient(DocClient):
    def complete(self, task, context, schema):
        if context["goal"] == "需要接续" and not context.get("human_replies"):
            return decision("ask_user", answer="请说明政策主题。")
        return super().complete(task, context, schema)


class AdaptiveFixturePlanner(FixturePlanner):
    adaptive = True
    def workflow(self, req, resources):
        return replace(adaptive_doc_spec(resources), custom_tools=[t | {"source": ""} for t in adaptive_doc_spec(resources).custom_tools])
    def tests(self, req, resources, spec):
        suite = doc_suite(req, resources)
        rid = resources["documents"][0]["id"]
        suite["tool_cases"] = [{"id": p + "tool", "partition": p, "tool": "custom.policy_profile", "input": {"document_id": rid},
                                "expected": {"statements": 2}, "purpose": "Statement profile", "criterion": "Literal count"} for p in ("development", "acceptance")]
        suite["tool_cases"][1].update(input={"document_id": rid, "text": "甲。乙。丙。"}, expected={"statements": 3})
        suite["tool_cases"].append({"id": "development-empty-text", "partition": "development", "tool": "custom.policy_profile",
            "input": {"document_id": rid, "text": ""}, "expected": {"statements": 0}, "purpose": "Empty fixture", "criterion": "Zero count"})
        suite["tool_cases"].extend({"id": p + "-host", "partition": p, "tool": "custom.policy_profile", "input": {"document_id": 1},
            "expected_error": {"code": "INVALID_ARGUMENTS", "origin": "decision", "layer": "host"},
            "purpose": "Host rejection", "criterion": "No tool code"} for p in ("development", "acceptance"))
        suite["goal_cases"] = [{"id": p + "goal", "partition": p, "input": {"goal": "解释出差费用规定", "parameters": {}},
                                "reference_query": {}, "source_id": rid, "facts": ["每天 80 元"], "refusal": False,
                                "purpose": "Semantic policy goal", "criterion": "Cite facts and profile"} for p in ("development", "acceptance")]
        return suite
    def code(self, req, spec, development, resources, tool_development):
        assert all(c["partition"] == "development" for c in [*development, *tool_development])
        return {"spec": adaptive_doc_spec(resources), "source": GOOD_SOURCE, "explanation": "Generated real tool source"}


class AdaptiveFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
    def tearDown(self):
        self.tmp.cleanup()
    def build(self):
        return self.factory.drive(self.factory.create("按政策回答并核对政策条目", uploads=DOCUMENTS))

    def test_full_build_sources_and_standalone_local_verification(self):
        state = self.build()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["schema_version"], 4)
        self.assertGreater(state["report"]["model_calls"], 0)
        export = Path(state["export_path"])
        self.assertTrue((export / "custom_tools" / "policy_profile.py").is_file())
        self.assertTrue((export / "agentforge" / "codex_client.py").is_file())
        result = subprocess.run([sys.executable, "-X", "utf8", "verify_agent.py"], cwd=export, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["mode"], "local_tools_only")
        (export / "custom_tools" / "policy_profile.py").write_text("tamper", encoding="utf-8")
        result = subprocess.run([sys.executable, "verify_agent.py"], cwd=export, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integrity mismatch", result.stderr)

    def test_generated_source_tamper_and_version_diff(self):
        state = self.build()
        changes = self.factory.detail(state["run_id"])["changes"]
        self.assertIn("document(", changes[0]["tool_diffs"]["custom.policy_profile"])
        source = self.factory.run_dir(state["run_id"]) / "versions/v1/custom_tools/policy_profile.py"
        source.write_text("tamper", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "源码"):
            self.factory.load(state["run_id"])

    def test_exported_entrypoint_saves_and_resumes_human_input(self):
        state = self.build()
        export = Path(state["export_path"])
        # Replace only the model boundary in the standalone process; all package/CLI/state code is real.
        script = '''import runpy,sys,json
from agentforge.codex_client import CodexClient
def complete(self,task,context,schema):
    self.last_metadata={"model":"test_fixture"}
    docs=context["resources"]["documents"]
    obs=context["observations"]
    action="call_tool";tool="documents.read";args={"ids":[docs[0]["id"]]};answer="完成";sources=[]
    if not context["human_replies"]:
        action="ask_user";tool=None;args={};answer="需要哪项政策？"
    elif len(obs)==1:
        tool="custom.policy_profile";args={"document_id":docs[0]["id"]}
    elif len(obs)>=2:
        action="finish";tool=None;args={};answer=obs[0]["result"][0]["content"];sources=[{"id":docs[0]["id"],"quote":answer}]
    return {"action":action,"tool":tool,"arguments_json":json.dumps(args),"answer":answer,"sources":sources,"abstained":False,"result_ref":None,"reason":"fixture","plan":["inspect"]}
CodexClient.complete=complete
sys.argv=["run_agent.py"]+sys.argv[1:]
runpy.run_path("run_agent.py",run_name="__main__")
'''
        def run(*args):
            return subprocess.run([sys.executable, "-X", "utf8", "-c", script, *args], cwd=export, capture_output=True, text=True, encoding="utf8", timeout=10)
        first = run("政策？", "--state-file", ".executions/session.json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)["needs_input"])
        final = run("--state-file", ".executions/session.json", "--resume", "--reply", "餐补")
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertEqual(json.loads(final.stdout)["model_calls"], 4)
        saved = read_json(export / ".executions/session.json")
        self.assertEqual(saved["checkpoint"]["phase"], "completed")
        self.assertEqual(saved["checkpoint_hash"], fingerprint(saved["checkpoint"]))
        invalid = run("--state-file", "agent.json", "政策？")
        self.assertEqual(invalid.returncode, 2)

    def test_persistent_child_execution(self):
        state = self.build()
        record = create_execution(self.factory, state["run_id"], {"question": "发票提交时间"})
        final = execute_record(self.factory, state["run_id"], record["execution_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["artifact_hash"], state["artifact_hash"])
        saved = read_json(execution_path(self.factory, state["run_id"], record["execution_id"]))
        self.assertEqual(saved["result"]["model_calls"], 3)
        self.assertTrue(saved["trace"])
        with self.assertRaises(ValueError):
            execute_record(self.factory, state["run_id"], record["execution_id"])

    def test_model_failure_stops_test_phase_without_code_repair(self):
        self.factory.runtime_client_factory = lambda: ScriptClient([CodexError("network")])
        state = self.build()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "evaluate")
        self.assertEqual(state["repairs"], 0)
        self.assertTrue((self.factory.run_dir(state["run_id"]) / "versions/v1/development_interrupted.json").is_file())

    def test_readonly_history_survives_runtime_upgrade(self):
        state = self.build()
        self.factory.runtime_hash = "new-runtime"
        self.assertEqual(self.factory.detail(state["run_id"])["status"], "completed")
        result = self.factory.run(state["run_id"], {"question": "餐补"})
        self.assertIsNone(result["error"])
        self.assertEqual(result["model_calls"], 3)
        export = Path(state["export_path"]) / "agentforge" / "adaptive_runtime.py"
        export.write_text("tampered runtime", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "完整性"):
            self.factory.run(state["run_id"], {"question": "餐补"})

    def test_scope_refusal_does_not_force_unrelated_business_tool(self):
        state = self.build()
        self.factory.runtime_client_factory = lambda: ScriptClient([decision(tool="resources.list"),
            decision("finish", abstained=True, answer="资料未载明，无法回答未知主题。")])
        result = self.factory.run(state["run_id"], "未知主题")
        self.assertIsNone(result["error"])
        self.assertTrue(result["abstained"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["model_calls"], 2)

    def test_runtime_upgrade_preserves_frozen_tests_and_old_version(self):
        self.factory.runtime_files.pop("adaptive_artifacts.py")
        self.factory.runtime_manifest.pop("adaptive_artifacts.py")
        self.factory.runtime_hash = fingerprint(self.factory.runtime_manifest)
        self.factory.planner.code = self.broken_code
        old = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS, review=True))
        self.assertEqual(old["status"], "waiting_for_guidance")
        current = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        final = current.upgrade_runtime(old["run_id"])
        self.assertEqual(final["version"], 2)
        self.assertEqual(final["runtime_hash"], current.runtime_hash)
        self.assertEqual(final["frozen_hashes"], old["frozen_hashes"])
        self.assertEqual(read_json(current.run_dir(old["run_id"]) / "versions/v1/runtime_manifest.json"), self.factory.runtime_manifest)
        self.assertEqual(final["status"], "waiting_for_guidance")

    def test_unfinished_agent_resumes_with_frozen_runtime_after_host_upgrade(self):
        from agentforge.executions import continue_execution
        self.factory.planner.code = self.broken_code
        state = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS, review=True))
        self.assertEqual(state["status"], "waiting_for_guidance")
        self.factory.runtime_client_factory = lambda: ScriptClient([decision("ask_user", answer="哪个主题？")])
        record = create_execution(self.factory, state["run_id"], "政策？")
        first = execute_record(self.factory, state["run_id"], record["execution_id"])
        self.assertEqual(first["status"], "waiting_input")
        current = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        current.runtime_hash = "upgraded-host-runtime"
        continue_execution(current, state["run_id"], record["execution_id"], {"text": "餐补"})
        final = execute_record(current, state["run_id"], record["execution_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["artifact_hash"], first["artifact_hash"])
        runtime = self.factory.run_dir(state["run_id"]) / "versions/v1/runtime/adaptive_runtime.py"
        runtime.write_text("tampered", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "运行时"):
            current.run(state["run_id"], "餐补")

    def broken_code(self, req, spec, development, resources, tool_development):
        built = adaptive_doc_spec(resources)
        broken = [t | {"source": 'def run(context):\n    return {"statements": 999}\n'} for t in built.custom_tools]
        return {"spec": replace(built, custom_tools=broken), "source": GOOD_SOURCE, "explanation": "Fault injection"}

    def test_tool_source_failure_repairs_code_before_live_behavior(self):
        self.factory.planner.code = self.broken_code
        self.factory.planner.repair = lambda req, spec, source, failures, diagnosis, direction, development, resources: (
            adaptive_doc_spec(resources), GOOD_SOURCE, "Correct dynamic document computation")
        state = self.build()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["repairs"], 1)
        first = read_json(self.factory.run_dir(state["run_id"]) / "versions/v1/development.json")
        self.assertTrue(first["behavior_skipped"])
        self.assertEqual(first["model_calls"], 0)
        diff = self.factory.detail(state["run_id"])["changes"][-1]["tool_diffs"]["custom.policy_profile"]
        self.assertIn('-    return {"statements": 999}', diff)
        self.assertIn('+        text = document', diff)

    def test_repair_cannot_change_frozen_tool_definition(self):
        self.factory.planner.code = self.broken_code
        def mutate(req, spec, source, failures, diagnosis, direction, development, resources):
            tools = [t | {"description": "Changed contract"} for t in adaptive_doc_spec(resources).custom_tools]
            return replace(spec, custom_tools=tools), GOOD_SOURCE, "Invalid repair"
        self.factory.planner.repair = mutate
        state = self.build()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "repair")
        self.assertIn("冻结", state["last_error"])
        self.assertEqual(state["version"], 1)


class PlannerFeedbackTests(unittest.TestCase):
    def design(self):
        spec = quality_spec().to_dict()
        spec.pop("runtime")
        spec.pop("trusted_tools")
        spec["custom_tools"] = [{k: v for k, v in t.items() if k not in ("source", "output_schema")} |
                                {"output_schema_json": json.dumps(t["output_schema"]), "error_cases": [{
                                    "id": "rows-type", "layer": "host", "input_json": json.dumps({
                                        "dataset_id": "fixture", "column": "amount", "rows": "not-an-array"}),
                                    "code": "INVALID_ARGUMENTS", "description": "Host rejects non-array rows"}], "execution_plan": {
                                    "algorithm": "Loop over rows, count missing cells and sum numeric values",
                                    "helpers": ["get", "dataset", "len", "float", "round"],
                                    "syntax": ["Assign", "If", "For", "Return", "Dict"]}} for t in spec["custom_tools"]]
        return spec

    def test_design_selects_only_operator_configured_trusted_tools(self):
        from test_trusted_tools import sqlite_tool
        req, resources, _ = csv_fixture()
        bad, good = self.design(), self.design()
        bad["tools"].append("trusted.invented")
        good["tools"].append("trusted.stock")
        client = ScriptClient([bad, good])
        planner = AdaptivePlanner(client)
        planner.trusted_tools = [sqlite_tool()]
        result = planner.workflow(req, resources)
        self.assertEqual(result.trusted_tools, [sqlite_tool()])
        self.assertNotIn("config", client.contexts[0]["trusted_tools"][0])
        self.assertTrue(planner.design_attempts)
        original = FactoryAgent.tool_contracts(result.to_dict())
        changed = copy.deepcopy(original)
        changed["trusted_tools"][0]["config"]["query"] = "SELECT secret FROM private"
        self.assertFalse(FactoryAgent.same_tool_contracts(original, changed))

    def test_malformed_schema_is_recorded_before_feedback(self):
        req, resources, _ = csv_fixture()
        good, bad = self.design(), self.design()
        bad["custom_tools"][0]["output_schema_json"] += "}"
        client = ScriptClient([bad, good])
        planner = AdaptivePlanner(client)
        spec = planner.workflow(req, resources)
        self.assertEqual(spec.custom_tools[0]["source"], "")
        self.assertEqual(planner.design_attempts[0]["design"], bad)
        self.assertTrue(client.contexts[1]["previous_validation_errors"])

    def test_invalid_reference_dataset_is_not_silently_changed(self):
        req, resources, _ = csv_fixture()
        spec = quality_spec()
        rid = req.defaults["dataset_id"]
        value = {"tool_cases": [], "goal_cases": []}
        for p in ("development", "acceptance"):
            value["tool_cases"].append({"id": p + "-host", "partition": p, "tool": "custom.quality",
                "input_json": json.dumps({"rows": "bad"}), "expected_json": None,
                "expected_error": {"code": "INVALID_ARGUMENTS", "origin": "decision", "layer": "host"}, "purpose": "Host", "criterion": "Reject"})
            value["tool_cases"].append({"id": p + "tool", "partition": p, "tool": "custom.quality",
                "input_json": json.dumps({"dataset_id": rid, "column": "amount"} | ({"rows": [{"amount": "5"}]} if p == "acceptance" else {})),
                "expected_json": json.dumps({"rows": 1, "missing": 0, "total": 5} if p == "acceptance" else {"rows": 4, "missing": 1, "total": 20.8}), "purpose": "Quality", "criterion": "Numeric profile"})
            value["goal_cases"].append({"id": p + "goal", "partition": p, "goal": "检查质量并按区域汇总", "parameters_json": "{}",
                "reference_query_json": json.dumps({"dataset_id": rid}), "source_id": None, "facts": [], "refusal": False,
                "purpose": "Natural goal", "criterion": "Independent result"})
        value["tool_cases"].append({"id": "development-empty", "partition": "development", "tool": "custom.quality",
            "input_json": json.dumps({"dataset_id": rid, "column": "amount", "rows": []}),
            "expected_json": json.dumps({"rows": 0, "missing": 0, "total": 0}), "purpose": "Empty data", "criterion": "Zero profile"})
        bad = copy.deepcopy(value)
        bad["goal_cases"][0]["reference_query_json"] = json.dumps({"dataset_id": "unauthorized"})
        client = ScriptClient([bad, value])
        planner = AdaptivePlanner(client)
        with patch("agentforge.planner.Planner.tests", return_value={"cases": []}):
            result = planner.tests(req, resources, spec)
        self.assertTrue(planner.test_attempts)
        self.assertEqual(result["goal_cases"][0]["reference_query"], {})
        self.assertIn("unauthorized", client.contexts[1]["previous_rejected_tests"]["goal_cases"][0]["reference_query_json"])


if __name__ == "__main__":
    unittest.main()
