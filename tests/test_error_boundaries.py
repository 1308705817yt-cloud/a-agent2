"""Regressions for legal large tool results and failures outside generated code."""

import copy
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.codex_client import object_schema
from agentforge.contracts import ExecutionError, error_response
from agentforge.custom_tools import CustomTool
from agentforge.factory import FactoryAgent
from agentforge.program import MAX_TEXT, Program
from agentforge.resources import fingerprint, read_json
from agentforge.source_validation import apply_output_hook
from agentforge.trusted_tools import TrustedTools
from test_adaptive import AdaptiveFixturePlanner, DocClient, ScriptClient, decision, quality_spec
from test_codex_factory import DOCUMENTS, GOOD_SOURCE
from test_runtime import csv_fixture
import test_trusted_tools


def payload(data):
    return {"answer": "结果", "data": data, "sources": [], "abstained": False, "error": None}


class LargeResultBoundaries(unittest.TestCase):
    def test_reported_120001_character_result_is_structured_code_limit(self):
        with self.assertRaises(ExecutionError) as caught:
            apply_output_hook(GOOD_SOURCE, payload("x" * 120001), {}, {"documents": []})
        error = caught.exception.info()
        self.assertEqual(error["code"], "CODE_LIMIT")
        self.assertEqual(error["location"], "logic.py:1")
        self.assertEqual(error["origin"], "runtime")
        self.assertFalse(error["retryable"])
        self.assertEqual(error["recovery"], "reduce_result_or_adjust_limit")

    def test_exact_json_budget_and_one_character_over(self):
        source = 'def run(context):\n    return context["result"]\n'
        envelope = {"result": ""}
        overhead = len(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
        envelope["result"] = "x" * (MAX_TEXT - overhead)
        self.assertEqual(len(json.dumps(envelope)), MAX_TEXT)
        program = Program(source)
        self.assertEqual(program.run(envelope), envelope["result"])
        with self.assertRaises(ExecutionError) as caught:
            program.run({"result": envelope["result"] + "x"})
        self.assertEqual(caught.exception.code, "CODE_LIMIT")
        self.assertEqual(caught.exception.location, "logic.py:1")

    def test_same_instance_resets_line_and_recovers_after_oversize_input(self):
        program = Program('def run(context):\n    value = context["result"]\n    return value\n')
        self.assertEqual(program.run({"result": "small"}), "small")
        self.assertEqual(program.line, 3)
        with self.assertRaises(ExecutionError) as caught:
            program.run({"result": "x" * 120001})
        self.assertEqual(caught.exception.location, "logic.py:1")
        self.assertEqual(program.env, {})
        self.assertEqual(program.steps, 0)
        self.assertEqual(program.run({"result": "next"}), "next")

    def test_growth_by_executed_source_is_still_source_failure(self):
        program = Program('def run(context):\n    return "x" * 120001\n')
        with self.assertRaises(ExecutionError) as caught:
            program.run({})
        self.assertEqual(caught.exception.code, "CODE_LIMIT")
        self.assertEqual(caught.exception.location, "logic.py:2")
        self.assertEqual(caught.exception.origin, "source")
        self.assertEqual(caught.exception.recovery, "repair_source")

    def test_legal_custom_tool_result_larger_than_hook_budget_has_terminal_failure(self):
        req, resources, _ = csv_fixture()
        spec = quality_spec()
        tool = spec.custom_tools[0] | {"output_schema": object_schema({"blob": {"type": "array", "items": {"type": "string", "maxLength": 10000}}}),
                                      "source": 'def run(context):\n    return {"blob": ["x" * 10000] * 13}\n'}
        args = {"dataset_id": req.defaults["dataset_id"], "column": "amount"}
        observed = CustomTool(tool, resources).invoke(args, [], req)
        self.assertGreater(len(json.dumps(observed)), MAX_TEXT)
        client = ScriptClient([decision(tool=tool["id"], args=args), decision("finish", result_ref="o1")])
        with tempfile.TemporaryDirectory() as root:
            result = AdaptiveAgent(replace(spec, custom_tools=[tool]), req, resources, GOOD_SOURCE,
                                   Path(root), lambda: client).run({"goal": "返回该自定义工具的结果"})
        self.assertEqual(result["error"]["code"], "CODE_LIMIT")
        self.assertEqual(result["failure"]["origin"], "runtime")
        self.assertEqual(result["failure"]["location"], "logic.py:1")
        self.assertFalse(result["failure"]["retryable"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["observations"][0]["status"], "ok")
        self.assertEqual(result["observations"][0]["result"], observed)


class TrustedFailureBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                mode = self.path.split("?")[0].lstrip("/")
                status = int(mode) if mode.isdigit() else 200
                body = b'{"value":"bad"}' if mode == "wrong-type" else b'{"value":12}'
                if mode == "invalid-json":
                    body = b'{'
                if mode == "invalid-utf8":
                    body = b'\xff'
                self.send_response(status)
                self.send_header("Content-Type", "text/plain" if mode == "wrong-mime" else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.req, self.resources, _ = csv_fixture()

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, mode="wrong-type"):
        return {"id": "trusted.metric", "kind": "http_json", "description": "Read fixed external metric",
                "input_schema": object_schema({"symbol": {"type": "string"}}),
                "output_schema": object_schema({"value": {"type": "number"}}),
                "config": {"url": f"http://127.0.0.1:{self.server.server_port}/{mode}", "header_env": {}, "timeout": 2}}

    def agent(self, client, tool=None):
        spec = quality_spec()
        tool = tool or self.tool()
        return AdaptiveAgent(replace(spec, tools=spec.tools + [tool["id"]], trusted_tools=[tool]),
                             self.req, self.resources, GOOD_SOURCE, self.root, lambda: client)

    def test_service_contract_error_has_external_origin_in_exception_and_audit(self):
        for mode in ("wrong-type", "wrong-mime", "invalid-json", "invalid-utf8"):
            with self.subTest(mode=mode):
                registry = TrustedTools(self.root, [self.tool(mode)])
                with self.assertRaises(ExecutionError) as caught:
                    registry.invoke("trusted.metric", {"symbol": "test"})
                info = caught.exception.info()
                self.assertEqual(info["code"], "TOOL_OUTPUT_CONTRACT")
                self.assertEqual(info["origin"], "external")
                self.assertFalse(info["retryable"])
                self.assertEqual(info["recovery"], "check_service_or_configuration")
                self.assertEqual(info["location"], "trusted_tools.json.tools.trusted.metric.output_schema")
                self.assertEqual(registry.last_audit["error"], info)
                self.assertEqual(registry.last_audit["status"], "failed")
                self.assertGreaterEqual(registry.last_audit["elapsed_ms"], 0)

    def test_http_status_distinguishes_configuration_transient_and_permanent(self):
        for status, origin, retryable in ((401, "configuration", False), (403, "configuration", False),
                                           (404, "external", False), (429, "external", True), (503, "external", True)):
            with self.subTest(status=status):
                registry = TrustedTools(self.root, [self.tool(str(status))])
                with self.assertRaises(ExecutionError) as caught:
                    registry.invoke("trusted.metric", {"symbol": "test"})
                self.assertEqual(caught.exception.origin, origin)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertEqual(registry.last_audit["http_status"], status)
                self.assertEqual(registry.last_audit["error"], caught.exception.info())

    def test_missing_auth_is_configuration_and_bad_arguments_are_decision(self):
        tool = self.tool()
        tool["config"]["header_env"] = {"Authorization": "AGENTFORGE_TEST_MISSING_SECRET"}
        registry = TrustedTools(self.root, [tool])
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ExecutionError) as caught:
                registry.invoke(tool["id"], {"symbol": "test"})
        self.assertEqual(caught.exception.code, "TOOL_AUTH")
        self.assertEqual(caught.exception.origin, "configuration")
        self.assertEqual(caught.exception.recovery, "check_tool_configuration")
        with self.assertRaises(ExecutionError) as caught:
            registry.invoke(tool["id"], {"symbol": 123})
        self.assertEqual(caught.exception.origin, "decision")
        self.assertTrue(caught.exception.retryable)

    def test_runtime_stops_external_contract_error_without_source_repair(self):
        client = ScriptClient([decision(tool="trusted.metric", args={"symbol": "test"})])
        result = self.agent(client).run({"goal": "读取指标"})
        self.assertEqual(result["error"]["code"], "TOOL_OUTPUT_CONTRACT")
        self.assertEqual(result["failure"]["origin"], "external")
        self.assertFalse(result["failure"]["retryable"])
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["observations"][0]["error"], result["failure"])
        self.assertEqual(result["observations"][0]["audit"]["error"], result["failure"])

    def test_network_timeout_and_connection_failures_are_retryable_external(self):
        for exception, code in ((URLError(TimeoutError("private endpoint")), "TOOL_TIMEOUT"),
                                (URLError(OSError("private endpoint")), "TRUSTED_TOOL_ERROR")):
            with self.subTest(code=code):
                registry = TrustedTools(self.root, [self.tool()])
                with patch.object(registry, "http", side_effect=exception):
                    with self.assertRaises(ExecutionError) as caught:
                        registry.invoke("trusted.metric", {"symbol": "test"})
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.origin, "external")
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(caught.exception.recovery, "retry_tool")
                self.assertNotIn("private endpoint", str(caught.exception))

    def test_transient_error_can_retry_and_finish_preserves_failed_origin(self):
        args = {"symbol": "test"}
        quality_args = {"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}
        client = ScriptClient([decision(tool="custom.quality", args=quality_args),
                               decision(tool="trusted.metric", args=args), decision("finish", result_ref="o2")])
        result = self.agent(client, self.tool("503")).run({"goal": "读取指标"})
        self.assertEqual(result["failure"]["origin"], "external")
        self.assertTrue(result["failure"]["retryable"])
        self.assertEqual(result["failure"], result["observations"][1]["error"])
        client = ScriptClient([decision(tool="trusted.metric", args=args), decision(tool="trusted.metric", args=args),
                               decision(tool="custom.quality", args={"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}),
                               decision("finish", result_ref="o2")])
        agent = self.agent(client, self.tool("ok"))
        with patch.object(agent.tools.trusted, "http", side_effect=[URLError(TimeoutError()), {"value": 12}]):
            result = agent.run({"goal": "读取指标"})
        self.assertIsNone(result["error"])
        self.assertEqual(result["data"], {"value": 12})
        self.assertEqual(result["model_calls"], 4)

    def test_generated_custom_contract_defect_remains_source(self):
        tool = quality_spec().custom_tools[0] | {"source": 'def run(context):\n    return {"rows": "bad"}\n'}
        with self.assertRaises(ExecutionError) as caught:
            CustomTool(tool, self.resources).invoke({"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}, [], self.req)
        self.assertEqual(caught.exception.code, "TOOL_OUTPUT_CONTRACT")
        self.assertEqual(caught.exception.origin, "source")
        self.assertEqual(caught.exception.recovery, "repair_source")

    def test_factory_dependency_failure_keeps_version_and_phase_then_resumes(self):
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        failure = ExecutionError("TOOL_OUTPUT_CONTRACT", "服务返回错误类型", "trusted_tools.json.tools.trusted.metric.output_schema",
                                 origin="external", retryable=False).info()
        actual = error_response(failure["code"], failure["message"]) | {"failure": failure}
        report = {"cases": [{"id": "service-case", "passed": False, "actual": actual}], "passed": 0, "total": 1}
        with patch("agentforge.factory.evaluate_adaptive", return_value=copy.deepcopy(report)):
            state = factory.drive(factory.create("回答政策问题", uploads=DOCUMENTS))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "evaluate")
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["repairs"], 0)
        self.assertEqual(state["error_info"]["origin"], "external")
        path = factory.run_dir(state["run_id"]) / "versions/v1/development_dependency_blocked.json"
        self.assertEqual(read_json(path), report)
        spec_hash = fingerprint(state["spec"])
        frozen = copy.deepcopy(state["frozen_hashes"])
        source = factory.source(state)
        resumed = factory.retry(state["run_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["version"], 1)
        self.assertEqual(resumed["repairs"], 0)
        self.assertEqual(fingerprint(resumed["spec"]), spec_hash)
        self.assertEqual(resumed["frozen_hashes"], frozen)
        self.assertEqual(factory.source(resumed), source)


class SQLiteFailureBoundaries(unittest.TestCase):
    """Reuse only the real read-only database fixture setup."""
    setUp = test_trusted_tools.TrustedToolsTests.setUp
    tearDown = test_trusted_tools.TrustedToolsTests.tearDown
    def test_duplicate_columns_and_missing_database_are_configuration_failures(self):
        tool = copy.deepcopy(self.tool)
        tool["config"]["query"] = "SELECT sku AS same, stock AS same FROM stock WHERE branch = :branch"
        for definition, code in ((tool, "TOOL_OUTPUT_CONTRACT"),
                                 (self.tool | {"config": self.tool["config"] | {"database": "trusted_data/missing.sqlite"}}, "TOOL_RESOURCE")):
            with self.subTest(code=code):
                registry = TrustedTools(self.root, [definition])
                with self.assertRaises(ExecutionError) as caught:
                    registry.invoke(definition["id"], {"branch": "East"})
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.origin, "configuration")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.recovery, "check_tool_configuration")


if __name__ == "__main__":
    unittest.main()
