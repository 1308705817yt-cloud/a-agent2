import copy
import unittest

from agentforge.contracts import AgentSpec, ExecutionError, Requirements
from agentforge.program import Program
from agentforge.resources import prepare_resources
from agentforge.testing import csv_oracle, equivalent, evaluate, OracleError
from agentforge.workflow import WorkflowAgent, validate_spec
from test_codex_factory import doc_spec, GOOD_SOURCE, DOCUMENTS


CSV = "region,amount,kind\nEast,10.10,A\nWest,8.50,B\nEast,2.20,B\nWest,,A\n"
SOURCE = 'def run(context):\n    result = context["result"]\n    if result["error"] is None and result["data"] is not None:\n        parts = []\n        for row in result["data"]["results"]:\n            append(parts, str(row["group"]) + ": " + str(round(row["value"], 2)) if row["value"] is not None else "empty")\n        result["answer"] = join(parts, "; ") if len(parts) > 0 else "没有满足条件的数据。"\n    return result\n'


def csv_fixture():
    resources = prepare_resources(uploads=[{"name": "sales.csv", "content": CSV}])
    defaults = {"dataset_id": resources["datasets"][0]["id"], "operation": "sum", "column": "amount", "group_by": "region",
                "filters": [], "sort_by": "value", "descending": True, "limit": None}
    req = Requirements("按区域汇总销售额", "csv_analysis", "区域汇总", ["数值正确", "排序", "错误处理"], defaults)
    nodes = []
    chain = [("load", "csv.load", {"dataset_id": defaults["dataset_id"]}),
             ("filter", "csv.filter", {"filters": {"ref": "input.filters"}}),
             ("aggregate", "csv.aggregate", {k: {"ref": "input." + k} for k in ("operation", "column", "group_by")}),
             ("sort", "csv.sort", {k: {"ref": "input." + k} for k in ("sort_by", "descending")}),
             ("limit", "csv.limit", {"limit": {"ref": "input.limit"}}),
             ("format", "csv.format", {}), ("code", "code.transform", {})]
    for i, (nid, tool, arguments) in enumerate(chain):
        nodes.append({"id": nid, "tool": tool, "arguments": arguments, "next": chain[i + 1][0] if i + 1 < len(chain) else None,
                      "on_empty": None, "on_error": "error" if nid != "code" else None})
    nodes.append({"id": "error", "tool": "output.error", "arguments": {}, "next": "code", "on_empty": None, "on_error": None})
    spec = AgentSpec("销售分析", "csv_analysis", [n["tool"] for n in nodes], "load", nodes)
    return req, resources, spec


class ProgramTests(unittest.TestCase):
    def test_generated_function_supports_loop_and_formatting(self):
        code = 'def run(context):\n    items = []\n    for n in context["values"]:\n        if n > 1:\n            append(items, str(n * 2))\n    return join(items, ",")\n'
        self.assertEqual(Program(code).run({"values": [1, 2, 3]}), "4,6")

    def test_import_reflection_methods_and_unbounded_syntax_rejected(self):
        for code in ('import os\n', 'def run(context):\n    return context.__class__\n',
                     'def run(context):\n    return open("secret")\n', 'def run(context):\n    while True:\n        pass\n',
                     'def run(context):\n    return [x for x in context]\n', 'def run(context):\n    return str.__call__(context)\n'):
            with self.subTest(code=code), self.assertRaises(ExecutionError):
                Program(code)

    def test_budget_and_large_sequence_are_enforced(self):
        with self.assertRaisesRegex(ExecutionError, "步数"):
            Program('def run(context):\n    for n in context["values"]:\n        pass\n    return 1\n', budget=10).run({"values": list(range(100))})
        with self.assertRaisesRegex(ExecutionError, "预算"):
            Program('def run(context):\n    return "x" * 999999999\n').run({})

    def test_runtime_errors_report_code_line(self):
        with self.assertRaises(ExecutionError) as caught:
            Program('def run(context):\n    return 1 / 0\n').run({})
        self.assertEqual(caught.exception.location, "logic.py:2")

    def test_source_cannot_mutate_input_context(self):
        context = {"result": {"answer": "hello"}}
        Program('def run(context):\n    context["result"]["answer"] = "changed"\n    return context["result"]\n').run(context)
        self.assertEqual(context["result"]["answer"], "hello")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.req, self.resources, self.spec = csv_fixture()
        self.agent = WorkflowAgent(self.spec, self.req, self.resources, SOURCE)

    def test_csv_grouped_sum_sorted_and_limited(self):
        result = self.agent.run({"limit": 1})
        self.assertIsNone(result["error"])
        self.assertEqual(result["data"]["results"][0]["group"], "East")
        self.assertAlmostEqual(result["data"]["results"][0]["value"], 12.3)
        self.assertEqual(len(result["trace"]), 7)

    def test_all_aggregates_match_independent_oracle(self):
        for operation in ("sum", "mean", "count", "min", "max"):
            for group_by in (None, "region"):
                inputs = {"operation": operation, "group_by": group_by}
                with self.subTest(inputs=inputs):
                    self.assertTrue(equivalent(self.agent.run(inputs)["data"], csv_oracle(self.req, self.resources, inputs)))

    def test_filters_and_empty_results_match_oracle(self):
        for operator, value in (("eq", "East"), ("ne", "East"), ("contains", "est"), ("eq", "no such region")):
            inputs = {"filters": [{"column": "region", "operator": operator, "value": value}]}
            self.assertTrue(equivalent(self.agent.run(inputs)["data"], csv_oracle(self.req, self.resources, inputs)))
        for operator in ("gt", "gte", "lt", "lte"):
            inputs = {"filters": [{"column": "amount", "operator": operator, "value": 8.5}]}
            self.assertTrue(equivalent(self.agent.run(inputs)["data"], csv_oracle(self.req, self.resources, inputs)))

    def test_invalid_inputs_and_non_numeric_column(self):
        for inputs, code in (({"column": "missing"}, "UNKNOWN_COLUMN"), ({"column": "kind"}, "NON_NUMERIC_COLUMN"),
                             ({"operation": "execute"}, "INVALID_INPUT"), ({"limit": -1}, "INVALID_INPUT"),
                             ({"question": "anything"}, "INVALID_INPUT"), ({"filters": "bad"}, "INVALID_INPUT")):
            with self.subTest(inputs=inputs):
                self.assertEqual(self.agent.run(inputs)["error"]["code"], code)

    def test_cycles_unreachable_nodes_and_wrong_capabilities(self):
        for mutation in ("cycle", "unreachable", "tool", "resource", "type", "skip_code"):
            spec = copy.deepcopy(self.spec)
            if mutation == "cycle":
                spec.nodes[0]["next"] = "load"
            elif mutation == "unreachable":
                spec.nodes[0]["next"] = "aggregate"
            elif mutation == "tool":
                spec.nodes[0]["tool"] = "shell.execute"
            elif mutation == "resource":
                spec.nodes[0]["arguments"] = {"dataset_id": {"ref": "input.dataset_id"}}
            elif mutation == "type":
                spec.nodes[0]["next"] = "format"
            else:
                spec.nodes[5]["next"] = None
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_spec(spec, self.req, self.resources)

    def test_output_code_cannot_hide_tool_error(self):
        source = 'def run(context):\n    result = context["result"]\n    result["error"] = None\n    return result\n'
        agent = WorkflowAgent(self.spec, self.req, self.resources, source)
        self.assertEqual(agent.run({"column": "missing"})["error"]["code"], "OUTPUT_CONTRACT")

    def test_oracle_detects_wrong_generated_numeric_result(self):
        source = 'def run(context):\n    result = context["result"]\n    if result["data"] is not None:\n        result["data"]["results"][0]["value"] = 999\n    return result\n'
        agent = WorkflowAgent(self.spec, self.req, self.resources, source)
        suite = {"cases": [{"id": "sum", "partition": "development", "input": {}, "expect": "csv"}]}
        self.assertFalse(evaluate(agent, self.req, self.resources, suite, "development", "hash")["all_passed"])


class ResourceTests(unittest.TestCase):
    def test_no_automatic_sample_resources(self):
        with self.assertRaises(ValueError):
            prepare_resources()

    def test_invalid_upload_csv_headers_and_rows(self):
        for uploads in ([{"name": "a.csv", "content": 3}], [{"name": "a.exe", "content": "text"}],
                        [{"name": "a.csv", "content": "x,x\n1,2\n"}], [{"name": "a.csv", "content": "x,y\n1\n"}]):
            with self.subTest(uploads=uploads), self.assertRaises(ValueError):
                prepare_resources(uploads=uploads)

    def test_utf8_document_json(self):
        resources = prepare_resources(uploads=[{"name": "docs.json", "content": '[{"id":"p1","title":"政策","content":"提交时限为 30 天。"}]'}])
        self.assertEqual(resources["documents"][0]["id"], "p1")


class TopicRoutingTests(unittest.TestCase):
    def setUp(self):
        self.resources = prepare_resources(uploads=DOCUMENTS)
        self.req = Requirements("原文报销问答", "document_qa", "报销资料问答", ["引用", "拒答", "同义表达"], {})
        self.spec = doc_spec()
        self.spec.nodes[0]["arguments"]["topic_aliases"] = {self.resources["documents"][0]["id"]: ["报销", "餐补", "发票", "票据", "出差补贴"]}
        self.agent = WorkflowAgent(self.spec, self.req, self.resources, GOOD_SOURCE)

    def test_source_topics_admit_paraphrases_and_refuse_missing_subjects(self):
        answer = self.agent.run("出差补贴是多少？")
        self.assertFalse(answer["abstained"])
        self.assertIn("80 元", answer["answer"])
        refusal = self.agent.run("员工病假有多少天？")
        self.assertTrue(refusal["abstained"])
        self.assertFalse(refusal["sources"])

    def test_unknown_sources_and_generic_input_refs_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec.nodes[0]["arguments"]["topic_aliases"] = {"not_authorized": ["报销"]}
        with self.assertRaises(ExecutionError):
            validate_spec(spec, self.req, self.resources)
        spec = copy.deepcopy(self.spec)
        spec.nodes[0]["arguments"]["min_score"] = {"ref": "input.secret", "default": 0.2}
        with self.assertRaises(ValueError):
            validate_spec(spec, self.req, self.resources)
