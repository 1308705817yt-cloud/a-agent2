import copy
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from dataclasses import replace

from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.data_binding import OUTPUT_SOURCE
from agentforge.contracts import ExecutionError
from agentforge.sql_query import execute_csv_sql, execute_read, inspect_schema, input_schema, output_schema
from agentforge.trusted_tools import TrustedTools, load_definitions
from agentforge.resources import prepare_resources, write_json
from architecture_fixtures import fixture, plan
from test_adaptive import ScriptClient, decision


class SqlQueryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("CREATE TABLE orders(id INTEGER PRIMARY KEY, amount REAL);"
                             "INSERT INTO orders VALUES(1,2.5),(2,7.5);"
                             "CREATE TABLE items(order_id INTEGER REFERENCES orders(id), label TEXT);"
                             "INSERT INTO items VALUES(1,'A'),(2,'B');"
                             "CREATE TABLE private(secret TEXT); INSERT INTO private VALUES('hidden');")

    def query(self, sql, **kwargs):
        return execute_read(self.db, {"sql": sql, "parameters_json": json.dumps(kwargs)}, {"orders", "items"}, time.monotonic() + 5)

    def test_join_cte_window_and_bound_values(self):
        result = self.query("WITH selected AS (SELECT o.id, o.amount, i.label FROM orders o JOIN items i ON o.id=i.order_id WHERE o.amount>=:minimum) "
                            "SELECT label, sum(amount) OVER () AS total, row_number() OVER (ORDER BY id) AS position FROM selected ORDER BY id", minimum=2)
        self.assertEqual(result["rows"], [["A", 10.0, 1], ["B", 10.0, 2]])
        self.assertFalse(result["truncated"])
        self.assertEqual(self.query("SELECT label FROM items WHERE label=:value", value="A' OR 1=1 --")["rows"], [])

    def test_unauthorized_reads_writes_and_functions_are_denied(self):
        for sql in ("SELECT * FROM private", "SELECT * FROM sqlite_master", "SELECT load_extension('x')",
                    "SELECT randomblob(20)", "WITH x AS (SELECT 1) DELETE FROM orders RETURNING id",
                    "SELECT id FROM orders; DELETE FROM orders", "PRAGMA table_info(orders)"):
            with self.subTest(sql=sql), self.assertRaises(ExecutionError):
                self.query(sql)
        self.assertEqual(self.query("SELECT count(*) AS n FROM orders")["rows"], [[2]])

    def test_schema_contains_only_authorized_tables_and_foreign_keys(self):
        result = inspect_schema(self.db, ["orders", "items"], time.monotonic() + 5, 20)
        self.assertEqual({r[0] for r in result["rows"]}, {"orders", "items"})
        fk = next(r for r in result["rows"] if r[:2] == ["items", "order_id"])
        self.assertEqual(json.loads(fk[-1])[0]["table"], "orders")
        hidden = inspect_schema(self.db, ["items"], time.monotonic() + 5, 20)
        self.assertTrue(all(r[-1] == "[]" for r in hidden["rows"]))

    def test_column_free_count_cannot_read_shadow_tables(self):
        self.db.execute("CREATE TEMP TABLE orders(secret TEXT)")
        self.db.execute("INSERT INTO temp.orders VALUES('hidden')")
        with self.assertRaises(ExecutionError):
            self.query("SELECT count(*) FROM temp.orders")
        with self.assertRaises(ExecutionError):
            self.query("SELECT count(*) FROM private")

    def test_limits_duplicate_columns_and_non_json_cells(self):
        for sql, code in (("SELECT id AS x, amount AS x FROM orders", "INVALID_ARGUMENTS"),
                          ("SELECT x'00' AS binary_value", "TOOL_OUTPUT_CONTRACT"),
                          ("SELECT 1e999 AS value", "TOOL_OUTPUT_CONTRACT")):
            with self.subTest(sql=sql), self.assertRaises(ExecutionError) as caught:
                self.query(sql)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ExecutionError) as caught:
            execute_read(self.db, {"sql": "SELECT * FROM orders"}, {"orders"}, time.monotonic() + 5, max_rows=1)
        self.assertEqual(caught.exception.code, "TOOL_SIZE")

    def test_invalid_parameters_and_missing_bindings_are_diagnosable(self):
        for encoded in ("[]", "{\"x\": NaN}", "{\"x\": [1]}", json.dumps({"x": 2**63}), "not-json"):
            with self.subTest(encoded=encoded), self.assertRaises(ExecutionError) as caught:
                execute_read(self.db, {"sql": "SELECT :x AS value", "parameters_json": encoded}, set(), time.monotonic() + 5)
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENTS")
        with self.assertRaises(ExecutionError) as caught:
            self.query("SELECT :missing AS value")
        self.assertEqual(caught.exception.code, "SQL_QUERY")
        self.assertTrue(caught.exception.retryable)

    def test_expired_and_running_queries_respect_deadline(self):
        with self.assertRaises(ExecutionError) as caught:
            execute_read(self.db, {"sql": "SELECT 1"}, set(), time.monotonic() - 1)
        self.assertEqual(caught.exception.code, "TOOL_TIMEOUT")
        with self.assertRaises(ExecutionError) as caught:
            execute_read(self.db, {"sql": "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n"},
                         set(), time.monotonic() + 0.02)
        self.assertEqual(caught.exception.code, "TOOL_TIMEOUT")
        self.assertEqual(self.query("SELECT count(*) AS n FROM orders")["rows"], [[2]])

    def test_csv_join_keeps_text_ids_and_reports_resource_hashes(self):
        _, resources, _ = fixture()
        resources["datasets"].extend(prepare_resources(uploads=[{"name": "labels.csv", "content": "id,label\n001,first\n1,wrong\n003,third\n"}])["datasets"])
        result = execute_csv_sql({"sql": "SELECT a.id,b.label,a.amount FROM dataset_1 a JOIN dataset_2 b ON a.id=b.id ORDER BY a.id"}, resources)
        self.assertEqual(result["rows"], [["001", "first", "0.10"], ["003", "third", "10.00"]])
        self.assertEqual(len(result["resources"]), 2)
        self.assertIn("floating point", result["numeric_semantics"])

    def test_csv_case_collisions_and_invalid_identifiers_are_diagnosable(self):
        for content in ("id,ID\n1,2\n", "id,bad\x00name\n1,2\n"):
            resources = prepare_resources(uploads=[{"name": "bad.csv", "content": content}])
            with self.subTest(content=content), self.assertRaises(ExecutionError):
                execute_csv_sql({"sql": "SELECT * FROM dataset_1"}, resources)


class TrustedSqlQueryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "trusted_data").mkdir()
        with closing(sqlite3.connect(self.root / "trusted_data/sales.sqlite")) as db:
            db.executescript("CREATE TABLE sales(id TEXT, amount INTEGER); INSERT INTO sales VALUES('001',8);"
                             "CREATE TABLE private(secret TEXT); INSERT INTO private VALUES('hidden');")
        tool = {"id": "trusted.sales", "kind": "sqlite_query", "description": "授权销售查询",
                "config": {"database": "trusted_data/sales.sqlite", "tables": ["sales"], "timeout": 2, "max_rows": 20}}
        write_json(self.root / "trusted_tools.json", {"tools": [tool]})
        self.registry = TrustedTools(self.root, load_definitions(self.root))

    def invoke(self, sql="", action="query"):
        return self.registry.invoke("trusted.sales", {"action": action, "sql": sql, "parameters_json": "{}"})

    def test_config_gets_host_schemas_and_reads_schema_and_data(self):
        tool = self.registry.definitions["trusted.sales"]
        self.assertEqual(tool["input_schema"], input_schema())
        self.assertEqual(tool["output_schema"], output_schema())
        self.assertEqual({r[0] for r in self.invoke(action="schema")["rows"]}, {"sales"})
        self.assertEqual(self.invoke("SELECT sum(amount) AS total FROM sales")["rows"], [[8]])
        self.assertEqual(self.invoke("SELECT count(*) AS n FROM sales")["rows"], [[1]])
        self.assertEqual(self.registry.last_audit["access"], "read_only")

    def test_live_data_is_not_cached_and_private_tables_stay_blocked(self):
        self.assertEqual(self.invoke("SELECT amount FROM sales")["rows"], [[8]])
        with closing(sqlite3.connect(self.root / "trusted_data/sales.sqlite")) as db:
            db.execute("UPDATE sales SET amount=9")
            db.commit()
        self.assertEqual(self.invoke("SELECT amount FROM sales")["rows"], [[9]])
        with self.assertRaises(ExecutionError) as caught:
            self.invoke("SELECT * FROM private")
        self.assertEqual(caught.exception.origin, "decision")

    def test_schema_input_and_runtime_deadline(self):
        with self.assertRaises(ExecutionError):
            self.invoke("SELECT 1", action="schema")
        self.registry.deadline = time.monotonic() - 1
        with self.assertRaises(ExecutionError) as caught:
            self.invoke("SELECT 1 AS value")
        self.assertEqual(caught.exception.code, "TOOL_TIMEOUT")

    def test_schema_metadata_cannot_finish_a_business_question(self):
        req, resources, spec = fixture()
        tool = self.registry.definitions["trusted.sales"]
        spec = replace(spec, tools=spec.tools + [tool["id"]], trusted_tools=[tool],
                       execution_policy={k: v for k, v in spec.execution_policy.items() if k not in ("data_binding", "output_template")})
        client = ScriptClient([plan(req, "defer"),
            decision(tool="trusted.sales", args={"action": "schema", "sql": "", "parameters_json": "{}"}),
            decision("finish", result_ref="o2"),
            decision(tool="trusted.sales", args={"action": "query", "sql": "SELECT count(*) AS n FROM sales", "parameters_json": "{}"}),
            decision("finish", result_ref="o4")])
        result = AdaptiveAgent(spec, req, resources, OUTPUT_SOURCE, self.root, lambda: client).run("数据库有多少笔销售")
        self.assertIsNone(result["error"])
        self.assertEqual(result["observations"][2]["error"]["code"], "RESULT_REFERENCE")
        self.assertEqual(client.contexts[2]["allowed_result_refs"], [])
        self.assertEqual(result["data"]["rows"], [[1]])
