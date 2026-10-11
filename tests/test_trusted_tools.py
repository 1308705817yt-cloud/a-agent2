import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentforge.trusted_tools import TrustedTools, validate_definitions, load_definitions
from agentforge.contracts import ExecutionError
from agentforge.codex_client import object_schema
from agentforge.adaptive_runtime import AdaptiveAgent
from dataclasses import replace
from contextlib import closing
from test_adaptive import quality_spec, ScriptClient, decision
from test_codex_factory import GOOD_SOURCE
from test_runtime import csv_fixture


def sqlite_tool():
    return {"id": "trusted.stock", "kind": "sqlite_read", "description": "Read authorized stock by branch",
            "input_schema": object_schema({"branch": {"type": "string", "maxLength": 80}}),
            "output_schema": object_schema({"rows": {"type": "array", "items": object_schema({"sku": {"type": "string"}, "stock": {"type": "integer"}})}, "row_count": {"type": "integer"}}),
            "config": {"database": "trusted_data/stock.sqlite", "query": "SELECT sku, stock FROM stock WHERE branch = :branch ORDER BY sku", "tables": ["stock"], "max_rows": 5, "timeout": 2}}


class TrustedToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "trusted_data").mkdir()
        with closing(sqlite3.connect(self.root / "trusted_data/stock.sqlite")) as db:
            db.execute("CREATE TABLE stock(branch TEXT, sku TEXT, stock INTEGER)")
            db.executemany("INSERT INTO stock VALUES(?,?,?)", [("East", "A", 4), ("East", "B", 7), ("West", "A", 9)])
            db.execute("CREATE TABLE private(secret TEXT)")
            db.execute("INSERT INTO private VALUES('private-value')")
            db.commit()
        self.tool = sqlite_tool()

    def tearDown(self):
        self.temp.cleanup()

    def test_real_sqlite_read_and_injection_is_a_bound_value(self):
        registry = TrustedTools(self.root, [self.tool])
        self.assertEqual(registry.invoke("trusted.stock", {"branch": "East"}), {"rows": [{"sku": "A", "stock": 4}, {"sku": "B", "stock": 7}], "row_count": 2})
        self.assertEqual(registry.last_audit["access"], "read_only")
        self.assertEqual(registry.invoke("trusted.stock", {"branch": "East' OR 1=1 --"})["row_count"], 0)
        with self.assertRaises(ExecutionError):
            registry.invoke("trusted.stock", {"branch": "East", "query": "DROP TABLE stock"})

    def test_authorizer_blocks_other_tables_and_writes_even_in_with_clause(self):
        for query in ("SELECT secret FROM private", "WITH a AS (SELECT 1) DELETE FROM stock RETURNING sku, stock", "SELECT load_extension('x')"):
            tool = copy.deepcopy(self.tool)
            tool["config"]["query"] = query
            with self.assertRaises(ExecutionError):
                TrustedTools(self.root, [tool]).invoke(tool["id"], {"branch": "East"})
        with closing(sqlite3.connect(self.root / "trusted_data/stock.sqlite")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM stock").fetchone()[0], 3)

    def test_path_escape_unknown_tools_output_schema_and_row_limit(self):
        for path in ("../stock.sqlite", "trusted_data/../../outside.sqlite", "C:/private.sqlite"):
            tool = copy.deepcopy(self.tool)
            tool["config"]["database"] = path
            with self.assertRaises(ValueError):
                TrustedTools(self.root, [tool])
        registry = TrustedTools(self.root, [self.tool])
        with self.assertRaises(ExecutionError):
            registry.invoke("trusted.unknown", {})
        tool = copy.deepcopy(self.tool)
        tool["config"]["max_rows"] = 1
        with self.assertRaises(ExecutionError) as caught:
            TrustedTools(self.root, [tool]).invoke(tool["id"], {"branch": "East"})
        self.assertEqual(caught.exception.code, "TOOL_SIZE")

    def test_runtime_selects_trusted_tool_and_audits_actual_result(self):
        req, resources, _ = csv_fixture()
        spec = quality_spec()
        spec = replace(spec, tools=spec.tools + [self.tool["id"]], trusted_tools=[self.tool])
        client = ScriptClient([decision(tool="trusted.stock", args={"branch": "East"}),
                              decision(tool="custom.quality", args={"dataset_id": req.defaults["dataset_id"], "column": "amount"}),
                              decision(tool="csv.query"), decision("finish", result_ref="o3")])
        result = AdaptiveAgent(spec, req, resources, GOOD_SOURCE, self.root, lambda: client).run({})
        self.assertIsNone(result["error"])
        self.assertEqual(result["observations"][0]["result"]["row_count"], 2)
        self.assertIn("result_hash", result["observations"][0]["audit"])
        self.assertNotIn("config", client.contexts[0]["tools"][-1])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/private")
                    self.end_headers()
                    return
                content = json.dumps({"value": 12, "request": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def definition(self, path="/rate"):
        return {"id": "trusted.rate", "kind": "http_json", "description": "Read rate from fixed service",
                "input_schema": object_schema({"symbol": {"type": "string"}}),
                "output_schema": object_schema({"value": {"type": "number"}, "request": {"type": "string"}}),
                "config": {"url": "http://127.0.0.1:" + str(self.server.server_port) + path, "header_env": {}, "timeout": 2}}

    def test_real_http_get_encodes_arguments_without_changing_host(self):
        result = TrustedTools(".", [self.definition()]).invoke("trusted.rate", {"symbol": "x&url=https://evil.example"})
        self.assertEqual(result["value"], 12)
        self.assertIn("%26url%3D", result["request"])

    def test_redirect_and_inline_credentials_are_rejected(self):
        with self.assertRaises(ExecutionError) as caught:
            TrustedTools(".", [self.definition("/redirect")]).invoke("trusted.rate", {"symbol": "x"})
        self.assertEqual(caught.exception.code, "TOOL_REDIRECT")
        tool = self.definition()
        tool["config"]["url"] = "https://user:password@example.com/rate"
        with self.assertRaises(ValueError):
            validate_definitions([tool])

    def test_missing_auth_and_service_errors_do_not_leak_credentials(self):
        tool = self.definition()
        tool["config"]["header_env"] = {"Authorization": "TEST_TOOL_TOKEN"}
        with patch.dict("os.environ", {"TEST_TOOL_TOKEN": ""}):
            with self.assertRaises(ExecutionError) as caught:
                TrustedTools(".", [tool]).invoke(tool["id"], {"symbol": "x"})
        self.assertEqual(caught.exception.code, "TOOL_AUTH")
        with patch.dict("os.environ", {"TEST_TOOL_TOKEN": "secret-token"}), patch("agentforge.trusted_tools.build_opener", side_effect=OSError("secret-token")):
            with self.assertRaises(ExecutionError) as caught:
                TrustedTools(".", [tool]).invoke(tool["id"], {"symbol": "x"})
        self.assertNotIn("secret-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
