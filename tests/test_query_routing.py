"""15 routing cases execute real tools; scripted plans isolate host routing from model quality."""

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.data_binding import OUTPUT_SOURCE
from agentforge.sql_query import input_schema, output_schema
from architecture_fixtures import fixture, plan
from test_adaptive import ScriptClient, decision, quality_spec


class QueryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.req, self.resources, self.spec = fixture()

    def run_case(self, inputs, decisions, route, calls, tools, spec=None):
        self.client = ScriptClient(decisions)
        agent = AdaptiveAgent(spec or self.spec, self.req, self.resources, OUTPUT_SOURCE, self.root, lambda: self.client)
        result = agent.run(inputs)
        self.assertIsNone(result["error"], result)
        self.assertEqual(result["model_calls"], calls)
        self.assertEqual(len(self.client.contexts), calls)
        self.assertEqual([e["strategy"] for e in result["trace"] if e.get("type") == "routing"][-1], route)
        self.assertEqual([o["tool"] for o in result["observations"] if o["status"] == "ok"], ["csv.profile", *tools])
        self.assertIn("查询口径", result["answer"]) if (spec or self.spec).execution_policy.get("output_template") else None
        self.assertEqual(next(self.client.decisions, None), None, "Script contains unconsumed decisions")
        self.route_evidence = {"case": self.id(), "input": inputs, "expected_route": route,
                               "actual_routes": [e["strategy"] for e in result["trace"] if e.get("type") == "routing"],
                               "model_calls": result["model_calls"], "successful_tools": [o["tool"] for o in result["observations"] if o["status"] == "ok"],
                               "data": result["data"]}
        return result

    def optional_spec(self, required=False):
        custom = copy.deepcopy(quality_spec().custom_tools[0]) | {"required": required}
        return replace(self.spec, custom_tools=[custom], tools=self.spec.tools + [custom["id"]],
                       execution_policy={k: v for k, v in self.spec.execution_policy.items() if k != "data_binding"})

    def test_r01_structured_default_aggregation(self):
        r = self.run_case({}, [], "direct_query", 0, ["csv.query"])
        self.assertEqual(r["data"]["results"][0]["group"], "West")
        self.assertEqual(r["data"]["results"][0]["value"], 10)

    def test_r02_structured_count_includes_blank_amount(self):
        r = self.run_case({"operation": "count", "group_by": None}, [], "direct_query", 0, ["csv.query"])
        self.assertEqual(r["data"]["results"][0]["value"], 5)
        self.assertIsNone(r["data"]["column"])

    def test_r03_structured_explicit_negative_filter(self):
        r = self.run_case({"group_by": None, "filters": [{"column": "amount", "operator": "lt", "value": 0}]},
                          [], "direct_query", 0, ["csv.query"])
        self.assertEqual(r["data"]["results"][0]["value"], -0.1)

    def test_r04_structured_business_metric(self):
        r = self.run_case({"metric_query": {"metric_ids": ["revenue"]}}, [], "direct_query", 0, ["csv.metrics"])
        self.assertEqual(r["data"]["rows"][0]["values"], {"revenue": "0.20"})

    def test_r05_optional_custom_tool_does_not_disable_direct_metrics(self):
        r = self.run_case({"metric_query": {"metric_ids": ["per_unit"]}}, [], "direct_query", 0, ["csv.metrics"], self.optional_spec())
        self.assertEqual(r["data"]["rows"][0]["values"], {"per_unit": "0.0286"})

    def test_r06_natural_language_top_region(self):
        r = self.run_case("金额最高的区域，只要第一名", [plan(self.req, overrides={"limit": 1})], "single_query", 1, ["csv.query"])
        self.assertEqual(len(r["data"]["results"]), 1)
        self.assertEqual(r["data"]["results"][0]["group"], "West")

    def test_r07_natural_language_row_count(self):
        r = self.run_case("共有多少行，包括金额为空的记录", [plan(self.req, overrides={"operation": "count", "column": None, "group_by": None})],
                          "single_query", 1, ["csv.query"])
        self.assertEqual(r["data"]["results"][0]["value"], 5)

    def test_r08_natural_language_distinct_metric(self):
        r = self.run_case("数据涉及多少个不同区域", [plan(self.req, "metrics", {"metric_ids": ["regions"]})], "single_query", 1, ["csv.metrics"])
        self.assertEqual(r["data"]["rows"][0]["values"], {"regions": "2"})

    def test_r09_natural_language_preserves_explicit_empty_false_and_null(self):
        explicit = {"filters": [], "descending": False, "limit": None, "group_by": None}
        r = self.run_case({"goal": "按指定参数汇总所有金额", "parameters": explicit}, [plan(self.req, overrides=explicit)],
                          "single_query", 1, ["csv.query"])
        self.assertAlmostEqual(r["data"]["results"][0]["value"], 10.2)
        self.assertEqual(self.client.contexts[0]["input_contract"]["explicit_parameters"], explicit)

    def test_r10_optional_tool_keeps_single_plan_for_typed_date_metric(self):
        filters = [{"column": "date", "operator": "gte", "value": "2026-01-02"}]
        r = self.run_case("1月2日起的已支付金额", [plan(self.req, "metrics", {"filters": filters})],
                          "single_query", 1, ["csv.metrics"], self.optional_spec())
        self.assertEqual(r["data"]["rows"][0]["values"], {"revenue": "0.10"})
        self.assertIn("custom.quality", [t["id"] for t in self.client.contexts[0]["available_tools"]])

    def test_r11_relational_join_and_window_defer_to_loop(self):
        sql = ("SELECT a.id, b.region, row_number() OVER (ORDER BY a.id) AS position "
               "FROM dataset_1 a JOIN dataset_1 b ON a.id=b.id ORDER BY a.id LIMIT 2")
        r = self.run_case("自连接核对订单并给出窗口序号", [plan(self.req, "defer"), decision(tool="csv.sql", args={"sql": sql}),
                          decision("finish", result_ref="o2")], "adaptive_loop", 3, ["csv.sql"])
        self.assertEqual(r["data"]["rows"], [["001", "East", 1], ["002", "East", 2]])

    def test_r12_live_database_schema_and_query_defer_to_loop(self):
        (self.root / "trusted_data").mkdir()
        with closing(sqlite3.connect(self.root / "trusted_data/sales.sqlite")) as db:
            db.executescript("CREATE TABLE sales(amount INTEGER); INSERT INTO sales VALUES(8),(4);")
        tool = {"id": "trusted.sales", "kind": "sqlite_query", "description": "实时数据库", "input_schema": input_schema(), "output_schema": output_schema(),
                "config": {"database": "trusted_data/sales.sqlite", "tables": ["sales"], "timeout": 2, "max_rows": 20}}
        spec = replace(self.spec, tools=self.spec.tools + [tool["id"]], trusted_tools=[tool],
                       execution_policy={k: v for k, v in self.spec.execution_policy.items() if k not in ("data_binding", "output_template")})
        r = self.run_case("实时数据库的销售总额是多少", [plan(self.req, "defer"),
                          decision(tool="trusted.sales", args={"action": "schema", "sql": "", "parameters_json": "{}"}),
                          decision(tool="trusted.sales", args={"action": "query", "sql": "SELECT sum(amount) AS total FROM sales", "parameters_json": "{}"}),
                          decision("finish", result_ref="o3", answer="总额 12")], "adaptive_loop", 4, ["trusted.sales", "trusted.sales"], spec)
        self.assertEqual(r["data"]["rows"], [[12]])

    def test_r13_interpretation_uses_loop_and_labels_unverified_narrative(self):
        spec = replace(self.spec, answer_style="analysis")
        r = self.run_case("解释区域差异，说明证据的局限", [plan(self.req, "defer"), decision(tool="csv.query"),
                          decision("finish", result_ref="o2", answer="区域不同，单凭汇总无法确认原因。")], "adaptive_loop", 3, ["csv.query"], spec)
        self.assertIn("无法确认原因", r["answer"])
        self.assertIn("计算结果", r["answer"])
        self.assertEqual(r["answer_evidence"], "observed_data_with_unverified_narrative")

    def test_r14_required_custom_tool_bypasses_planner(self):
        args = {"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}
        r = self.run_case("先执行必需质量计算，再汇总区域金额", [decision(tool="custom.quality", args=args),
                          decision(tool="csv.query"), decision("finish", result_ref="o3")],
                          "adaptive_loop", 3, ["custom.quality", "csv.query"], self.optional_spec(required=True))
        self.assertEqual(r["observations"][1]["result"]["missing"], 1)
        self.assertFalse(any(e.get("type") == "query_plan" for e in r["trace"]))

    def test_r15_plan_conflict_falls_back_and_preserves_user_limit(self):
        r = self.run_case({"goal": "第一名", "parameters": {"limit": 1}},
                          [plan(self.req, overrides={"limit": 2}), decision(tool="csv.query", args={"limit": 1}),
                           decision("finish", result_ref="o3")], "adaptive_loop", 3, ["csv.query"])
        self.assertEqual(r["observations"][1]["error"]["code"], "RESULT_PARAMETERS")
        self.assertEqual(len(r["data"]["results"]), 1)


class QueryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.req, self.resources, self.spec = fixture()

    def agent(self, decisions):
        self.client = ScriptClient(decisions)
        return AdaptiveAgent(self.spec, self.req, self.resources, OUTPUT_SOURCE, self.root, lambda: self.client)

    def test_ambiguous_metric_asks_user_and_resumes_with_one_new_plan(self):
        request = plan(self.req, "ask_user") | {"question": "净额指哪个指标？"}
        first = self.agent([request]).run("给我净额")
        self.assertEqual(first["error"]["code"], "NEEDS_INPUT")
        self.assertEqual(first["model_calls"], 1)
        result = self.agent([plan(self.req, "metrics")]).run("给我净额", checkpoint=first["checkpoint"], reply={"text": "已支付金额 revenue"})
        self.assertIsNone(result["error"])
        self.assertEqual(result["new_model_calls"], 1)
        self.assertEqual(result["data"]["rows"][0]["values"], {"revenue": "0.20"})
        self.assertEqual(sum(o["tool"] == "csv.profile" for o in result["observations"]), 1)

    def test_committed_plan_resumes_without_another_model_call(self):
        saved = []
        def crash(checkpoint):
            if checkpoint["phase"] == "query_planned":
                saved.append(checkpoint)
                raise RuntimeError("simulated process loss")
        with self.assertRaisesRegex(RuntimeError, "process loss"):
            self.agent([plan(self.req)]).run("汇总", on_checkpoint=crash)
        result = self.agent([]).run("汇总", checkpoint=saved[0])
        self.assertIsNone(result["error"])
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["new_model_calls"], 0)

    def test_sql_error_replans_and_preview_cannot_replace_calculation(self):
        result = self.agent([plan(self.req, "defer"), decision(tool="csv.sql", args={"sql": "SELECT * FROM missing"}),
                             decision(tool="csv.preview"), decision("finish", result_ref="o3"),
                             decision(tool="csv.sql", args={"sql": "SELECT count(*) AS n FROM dataset_1"}),
                             decision("finish", result_ref="o5")]).run("用 SQL 统计行数")
        self.assertIsNone(result["error"])
        self.assertEqual(result["observations"][1]["error"]["code"], "SQL_QUERY")
        self.assertEqual(result["observations"][3]["error"]["code"], "RESULT_REFERENCE")
        self.assertEqual(result["data"]["rows"], [[5]])
