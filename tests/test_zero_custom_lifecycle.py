"""Production planner/factory/export pipeline with schema-valid model fixtures."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from agentforge.adaptive_planner import AdaptivePlanner
from agentforge.codex_client import validate_json
from agentforge.data_binding import OUTPUT_SOURCE
from agentforge.data_contract import VERSION
from agentforge.executions import create_execution, execute_record, continue_execution
from agentforge.factory import FactoryAgent
from agentforge.resources import read_json, fingerprint
from agentforge.testing import validate_suite
from architecture_fixtures import CSV, definition, fixture, plan
from test_adaptive import ScriptClient


def generated_cases():
    groups = {
        "development": [({}, None), ({"operation": "count", "group_by": None}, None),
                        ({"limit": 1}, None), ({"column": "missing_dev"}, "UNKNOWN_COLUMN")],
        "acceptance": [({"operation": "mean"}, None), ({"operation": "max", "group_by": None}, None),
                       ({"group_by": None, "filters": [{"column": "region", "operator": "eq", "value": "East"}]}, None),
                       ({"column": "missing_acceptance"}, "UNKNOWN_COLUMN")]}
    return {"cases": [{"id": part + str(i), "partition": part, "input_json": json.dumps(inputs),
                       "expect": "csv", "source_id": None, "facts": [], "purpose": "核对计算及输入边界", "criterion": "冻结口径",
                       "assertion": {"kind": "rejection" if code else "calculation", "error_code": code}}
                      for part, cases in groups.items() for i, (inputs, code) in enumerate(cases)]}


class BuildClient:
    model, timeout = None, 10
    last_metadata = {"model": "test_fixture"}

    def __init__(self):
        self.stages = []

    def complete(self, task, context, schema):
        properties = schema["properties"]
        if "supported" in properties:
            self.stages.append("requirements")
            defaults = copy.deepcopy(fixture()[0].defaults)
            defaults["dataset_id"] = context["resources"]["datasets"][0]["id"]
            defaults["filters_json"] = json.dumps(defaults.pop("filters"))
            value = {"supported": True, "task_type": "csv_analysis", "summary": "按区域汇总金额", "acceptance_criteria": ["计算准确", "检查非法输入", "保留查询口径"], "defaults": defaults}
        elif "custom_tools" in properties:
            self.stages.append("design")
            value = {"name": "零工具销售助手", "task_type": "csv_analysis", "answer_style": "facts",
                     "instructions": "按请求和冻结指标计算。", "planning_hints": ["读取口径", "选择查询", "核对结果"],
                     "tools": ["csv.query", "csv.sql"], "custom_tools": [], "max_steps": 6}
        elif "cases" in properties:
            self.stages.append("business_tests")
            value = generated_cases()
        elif "goal_cases" in properties:
            self.stages.append("goal_tests")
            value = {"tool_cases": [], "goal_cases": [
                {"id": "goal-" + part, "partition": part, "goal": goal, "parameters_json": "{}", "reference_query_json": json.dumps(query),
                 "source_id": None, "facts": [], "refusal": False, "purpose": "自然语言聚合", "criterion": "独立计算器核对"}
                for part, goal, query in [("development", "汇总所有区域", {}), ("acceptance", "金额最高的区域", {"limit": 1})]]}
        else:
            raise AssertionError("Unexpected generation stage; zero tools must skip code generation")
        validate_json(value, schema)
        return value


class GoalClient:
    last_metadata = {"model": "test_fixture"}

    def complete(self, task, context, schema):
        if "action" not in schema["properties"] or "query" not in schema["properties"]:
            raise AssertionError("Expected one typed query plan")
        query = copy.deepcopy(context["effective_input"])
        if context["goal"] == "金额最高的区域":
            query["limit"] = 1
        value = {"action": "execute", "query": query, "metric_query": None, "question": "", "reason": "fixture query"}
        validate_json(value, schema)
        return value


class ZeroCustomLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.build_client = BuildClient()
        cls.factory = FactoryAgent(cls.root, AdaptivePlanner(cls.build_client), runtime_client_factory=GoalClient)
        cls.state = cls.factory.drive(cls.factory.create("汇总销售并支持冻结指标", uploads=[{"name": "sales.csv", "content": CSV}],
            data_contract={"format": VERSION, "datasets": {"sales.csv": definition()}}, max_repairs=0))
        if cls.state["status"] != "completed":
            raise AssertionError(json.dumps({k: cls.state[k] for k in ("status", "phase", "last_error", "report", "acceptance_report")}, ensure_ascii=False, default=str))
        cls.export = Path(cls.state["export_path"])

    def standalone(self, script, *args, root=None, success=True):
        env = dict(os.environ, PYTHONPATH="", AGENTFORGE_CODEX_BIN=str(self.root / "no-codex.exe"))
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", script, *args], cwd=root or self.export,
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_zero_custom_generation_skips_code_request_and_keeps_real_acceptance(self):
        self.assertEqual(self.build_client.stages, ["requirements", "design", "business_tests", "goal_tests"])
        self.assertEqual(self.state["spec"]["custom_tools"], [])
        self.assertEqual((self.export / "logic.py").read_text(encoding="utf-8"), OUTPUT_SOURCE)
        self.assertEqual(list((self.export / "custom_tools").glob("*.py")), [])
        for report in (self.state["report"], self.state["acceptance_report"]):
            self.assertTrue(report["all_passed"])
            self.assertGreater(report["total"], 4)
            self.assertEqual(report["tool_coverage"], [])
            self.assertEqual(report["artifact_hash"], self.state["artifact_hash"])

    def test_local_verifier_reports_tool_checks_not_applicable(self):
        result = self.standalone("verify_agent.py")
        self.assertEqual(result["integrity"], "passed")
        self.assertEqual(result["mode"], "local_tools_only")
        for report in result["reports"]:
            self.assertEqual(report["total"], 0)
            self.assertEqual(report["custom_tool_validation"], "not_applicable")

    def test_archive_runs_without_repository_imports_or_codex(self):
        with tempfile.TemporaryDirectory() as folder:
            with zipfile.ZipFile(self.state["archive_path"]) as archive:
                archive.extractall(folder)
            target = Path(folder) / self.state["run_id"]
            self.standalone("verify_agent.py", root=target)
            result = self.standalone("run_agent.py", "--input-json", json.dumps({"metric_query": {"metric_ids": ["revenue"]}}), root=target)
            self.assertIsNone(result["error"])
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["data"]["rows"][0]["values"], {"revenue": "0.20"})

    def test_export_includes_all_runtime_dependencies_and_preserves_evidence_scope(self):
        manifest = read_json(self.export / "manifest.json")
        for name in ("data_contract", "metric_query", "sql_query", "data_binding", "resource_profiles", "csv_contract", "csv_execution", "csv_query_path"):
            self.assertIn("agentforge/" + name + ".py", manifest["files"])
        self.assertEqual(manifest["capability_evidence"], {"csv.metrics": "not_independently_evaluated", "csv.sql": "not_independently_evaluated"})
        self.assertEqual(read_json(self.export / "evaluation.json")["acceptance"]["artifact_hash"], self.state["artifact_hash"])

    def test_export_rejects_tampered_source(self):
        with tempfile.TemporaryDirectory() as folder:
            with zipfile.ZipFile(self.state["archive_path"]) as archive:
                archive.extractall(folder)
            target = Path(folder) / self.state["run_id"]
            with (target / "logic.py").open("a", encoding="utf-8") as stream:
                stream.write("\n# changed\n")
            result = self.standalone("verify_agent.py", root=target, success=False)
            self.assertIn("File integrity mismatch", result.stderr)

    def test_execution_record_freezes_binding_before_source_changes(self):
        req = fixture()[0]
        path = self.root / "record-binding.csv"
        path.write_text(CSV.replace("0.10", "2.10", 1), encoding="utf-8")
        before = fingerprint(self.factory.load(self.state["run_id"])["acceptance_report"])
        record = create_execution(self.factory, self.state["run_id"], {"metric_query": {"metric_ids": ["revenue"]}}, bindings={req.defaults["dataset_id"]: str(path)})
        path.write_text("changed-after-queue", encoding="utf-8")
        result = execute_record(self.factory, self.state["run_id"], record["execution_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result"]["data"]["rows"][0]["values"], {"revenue": "2.20"})
        self.assertEqual(result["result"]["data_binding"]["behavior_evaluation"], "not_evaluated_on_new_data")
        self.assertEqual(fingerprint(self.factory.load(self.state["run_id"])["acceptance_report"]), before)

    def test_binding_is_preserved_across_human_continuation(self):
        req = fixture()[0]
        path = self.root / "resume-binding.csv"
        path.write_text(CSV.replace("0.10", "3.10", 1), encoding="utf-8")
        record = create_execution(self.factory, self.state["run_id"], "净额是多少", bindings={req.defaults["dataset_id"]: str(path)})
        request = plan(req, "ask_user") | {"question": "请确认业务指标"}
        with patch.object(self.factory, "runtime_client_factory", lambda: ScriptClient([request])):
            waiting = execute_record(self.factory, self.state["run_id"], record["execution_id"])
        self.assertEqual(waiting["status"], "waiting_input", waiting)
        path.write_text("changed-after-question", encoding="utf-8")
        continue_execution(self.factory, self.state["run_id"], record["execution_id"], reply={"text": "已支付金额"})
        with patch.object(self.factory, "runtime_client_factory", lambda: ScriptClient([plan(req, "metrics")])):
            result = execute_record(self.factory, self.state["run_id"], record["execution_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result"]["data"]["rows"][0]["values"], {"revenue": "3.20"})
        self.assertEqual(result["data_binding"]["binding_hash"], record["data_binding"]["binding_hash"])

    def test_binding_uses_frozen_runtime_after_host_changes(self):
        req = fixture()[0]
        path = self.root / "frozen-binding.csv"
        path.write_text(CSV.replace("0.10", "5.10", 1), encoding="utf-8")
        with patch.object(self.factory, "runtime_hash", "new-host-runtime"), patch(
                "agentforge.adaptive_runtime.AdaptiveAgent.prepare_data_binding", side_effect=AssertionError("must use frozen runtime")):
            record = create_execution(self.factory, self.state["run_id"], {"metric_query": {"metric_ids": ["revenue"]}},
                                      bindings={req.defaults["dataset_id"]: str(path)})
            result = execute_record(self.factory, self.state["run_id"], record["execution_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result"]["data"]["rows"][0]["values"], {"revenue": "5.20"})

    def test_export_cli_accepts_compatible_binding_and_saves_snapshot(self):
        req = fixture()[0]
        path = self.root / "cli-binding.csv"
        path.write_text(CSV.replace("0.10", "4.10", 1), encoding="utf-8")
        result = self.standalone("run_agent.py", "--input-json", json.dumps({"metric_query": {"metric_ids": ["revenue"]}}),
                                 "--bindings-json", json.dumps({req.defaults["dataset_id"]: str(path)}), "--state-file", ".executions/phase1.json")
        self.assertEqual(result["data"]["rows"][0]["values"], {"revenue": "4.20"})
        self.assertEqual(result["model_calls"], 0)
        saved = read_json(self.export / ".executions/phase1.json")
        self.assertEqual(saved["data_binding"]["binding_hash"], result["data_binding"]["binding_hash"])


class GeneratedAssertionTests(unittest.TestCase):
    def setUp(self):
        self.req, self.resources, _ = fixture()
        self.suite = generated_cases()
        for case in self.suite["cases"]:
            case["input"] = json.loads(case.pop("input_json"))

    def test_rejection_cannot_be_mislabeled_as_calculation(self):
        self.suite["cases"][3]["assertion"] = {"kind": "calculation", "error_code": None}
        with self.assertRaisesRegex(ValueError, "声明计算成功"):
            validate_suite(self.suite, self.req, self.resources)

    def test_wrong_rejection_code_and_insufficient_calculations_fail(self):
        bad = copy.deepcopy(self.suite)
        bad["cases"][3]["assertion"]["error_code"] = "INVALID_INPUT"
        with self.assertRaisesRegex(ValueError, "实际错误"):
            validate_suite(bad, self.req, self.resources)
        for index in (1, 2):
            self.suite["cases"][index].update(input={"column": "missing" + str(index)}, assertion={"kind": "rejection", "error_code": "UNKNOWN_COLUMN"})
        with self.assertRaisesRegex(ValueError, "至少需要两个"):
            validate_suite(self.suite, self.req, self.resources)
