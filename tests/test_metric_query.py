import copy
import time
import unittest

from agentforge.codex_client import DeadlineExceeded, validate_json
from agentforge.contracts import ExecutionError
from agentforge.metric_query import execute_metrics, metric_schema, validate_query
from architecture_fixtures import CSV, definition, fixture


class MetricQueryTests(unittest.TestCase):
    def setUp(self):
        self.req, self.resources, _ = fixture()

    def query(self, ids, **kwargs):
        return execute_metrics({"metric_ids": ids, **kwargs}, self.req, self.resources)

    def test_all_aggregates_and_formulas_have_independent_expected_values(self):
        ids = [m["id"] for m in definition()["metrics"]]
        result = self.query(ids)
        self.assertEqual(result["rows"][0]["values"], {"revenue": "0.20", "total": "10.20", "units": "7", "orders": "5",
                         "regions": "2", "average": "2.55", "minimum": "-0.10", "maximum": "10.00",
                         "per_unit": "0.0286", "unpaid": "10.00"})
        self.assertEqual(result["numeric_encoding"], "decimal_string")
        self.assertEqual(result["rows"][0]["evidence"]["revenue"]["filtered_rows"], 4)
        self.assertEqual(result["rows"][0]["evidence"]["revenue"]["non_null_values"], 3)
        validate_json(result["query"], metric_schema(self.resources["datasets"][0]))

    def test_fixed_business_filter_cannot_be_overridden(self):
        result = self.query(["revenue", "orders"], filters=[{"column": "status", "operator": "eq", "value": "refund"}])
        self.assertEqual(result["rows"][0]["values"], {"revenue": "0.00", "orders": "1"})

    def test_grouping_sort_and_limit(self):
        result = self.query(["total"], dimensions=["region", "status"], sort_by="total", descending=True, limit=1)
        self.assertEqual(result["total_groups"], 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["rows"][0]["dimensions"], {"region": "West", "status": "refund"})
        self.assertEqual(result["rows"][0]["values"], {"total": "10.00"})

    def test_empty_sets_and_zero_denominators(self):
        result = self.query(["per_unit", "average", "total", "orders"],
                            filters=[{"column": "status", "operator": "eq", "value": "refund"}])
        self.assertIsNone(result["rows"][0]["values"]["per_unit"])
        empty = [{"column": "id", "operator": "eq", "value": "absent"}]
        self.assertEqual(self.query(["average", "total", "orders"], filters=empty)["rows"][0]["values"],
                         {"average": None, "total": "0.00", "orders": "0"})
        self.assertEqual(self.query(["total"], filters=empty, dimensions=["region"])["rows"], [])

    def test_round_half_even_and_formula_uses_unrounded_dependencies(self):
        contract = definition()
        contract["metrics"] = [
            {"id": "average", "description": "均值", "operation": "mean", "column": "amount", "scale": 2},
            {"id": "units", "description": "单位", "operation": "sum", "column": "quantity", "scale": 0},
            {"id": "ratio", "description": "比率", "formula": {"operation": "divide", "left": "average", "right": "units"}, "scale": 4}]
        self.req, self.resources, _ = fixture("id,region,amount,quantity,status,date\n1,E,0.12,1,paid,2026-01-01\n2,E,0.13,1,paid,2026-01-02\n", contract)
        self.assertEqual(self.query(["average", "ratio"])["rows"][0]["values"], {"average": "0.12", "ratio": "0.0625"})

    def test_null_policy_error(self):
        contract = definition()
        contract["metrics"][0]["null_policy"] = "error"
        self.req, self.resources, _ = fixture(contract=contract)
        with self.assertRaises(ExecutionError) as caught:
            self.query(["revenue"])
        self.assertEqual(caught.exception.code, "DATA_CONTRACT")

    def test_invalid_definitions_and_cyclic_dependencies(self):
        for mutate in (lambda c: c["metrics"][0].update(column="region"),
                       lambda c: c["metrics"][0].update(operation="median"),
                       lambda c: c["metrics"][-1]["formula"].update(left="unpaid"),
                       lambda c: c["metrics"][-1]["formula"].update(right="unknown"),
                       lambda c: c["metrics"][0].update(filters=[{"column": "amount", "operator": "contains", "value": "1"}])):
            contract = definition()
            mutate(contract)
            with self.subTest(contract=contract), self.assertRaises(ExecutionError):
                fixture(contract=contract)

    def test_invalid_queries_are_correctable_decisions(self):
        for query in ({"metric_ids": []}, {"metric_ids": ["total", "total"]}, {"metric_ids": ["unknown"]},
                      {"metric_ids": ["total"], "dimensions": ["id"]}, {"metric_ids": ["total"], "sort_by": "units"},
                      {"metric_ids": ["total"], "limit": True}, {"metric_ids": ["total"], "invented": 1},
                      {"metric_ids": ["total"], "filters": [{"column": "date", "operator": "eq", "value": "bad"}]}):
            with self.subTest(query=query), self.assertRaises(ExecutionError) as caught:
                validate_query(query, self.resources, self.req.defaults["dataset_id"])
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENTS")
            self.assertTrue(caught.exception.retryable)

    def test_large_exact_decimal_and_deadline(self):
        self.req, self.resources, _ = fixture(CSV.replace("0.10", "9007199254740993.10", 1))
        self.assertEqual(self.query(["revenue"])["rows"][0]["values"]["revenue"], "9007199254740993.20")
        with self.assertRaises(DeadlineExceeded):
            execute_metrics({"metric_ids": ["total"]}, self.req, self.resources, deadline=time.monotonic() - 1)

    def test_group_count_is_bounded(self):
        contract = definition()
        contract["dimensions"].append("id")
        content = "id,region,amount,quantity,status,date\n" + "".join(f"{i},E,1,1,paid,2026-01-01\n" for i in range(1001))
        self.req, self.resources, _ = fixture(content, contract)
        with self.assertRaises(ExecutionError) as caught:
            self.query(["total"], dimensions=["id"])
        self.assertEqual(caught.exception.code, "RESULT_LIMIT")
