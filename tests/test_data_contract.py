import copy
import tempfile
import unittest
from pathlib import Path

from agentforge.contracts import ExecutionError
from agentforge.data_contract import VERSION, apply_contract, normalize_cell, typed_filter
from agentforge.resources import prepare_resources, fingerprint
from architecture_fixtures import CSV, definition, fixture


class DataContractTests(unittest.TestCase):
    def test_normalization_preserves_raw_data_and_hashes(self):
        _, pack, _ = fixture()
        data = pack["datasets"][0]
        self.assertEqual(data["rows"][0]["id"], "001")
        self.assertIsNone(data["rows"][3]["amount"])
        self.assertEqual(data["raw_rows"][3]["amount"], "")
        self.assertEqual(data["normalization"]["normalized_hash"], fingerprint(data["rows"]))
        self.assertEqual(data["normalization"]["contract_hash"], fingerprint(definition()))
        self.assertEqual(data["normalization"]["dropped_rows"], 0)
        self.assertEqual(data["normalization"]["changed_cells"], 1)

    def test_canonical_types_and_explicit_missing_markers(self):
        for value, field, expected in [
            ("001", {"type": "text"}, "001"), ("1.00", {"type": "integer"}, "1"),
            ("-0.00", {"type": "decimal", "scale": 2}, "0.00"),
            ("1.2300", {"type": "decimal"}, "1.23"),
            ("NA", {"type": "decimal", "missing_values": ["NA"]}, None),
            ("TRUE", {"type": "boolean"}, "true"), ("0", {"type": "boolean"}, "false"),
            ("2024-02-29", {"type": "date"}, "2024-02-29"),
            ("2026-01-01T08:00:00+08:00", {"type": "datetime"}, "2026-01-01T00:00:00+00:00"),
            ("2026-01-01T00:00:00", {"type": "datetime", "timezone": "UTC"}, "2026-01-01T00:00:00+00:00")]:
            with self.subTest(value=value, field=field):
                self.assertEqual(normalize_cell(value, field), expected)

    def test_invalid_values_fail_without_rounding_or_coercion(self):
        for value, field in [("1.001", {"type": "decimal", "scale": 2}), ("1.5", {"type": "integer"}),
                             (True, {"type": "decimal"}), ("NaN", {"type": "decimal"}),
                             ("1e100", {"type": "decimal"}), ("", {"nullable": False}),
                             ("yes", {"type": "boolean"}), ("2025-02-29", {"type": "date"}),
                             ("20260101", {"type": "date"}), ("2026-01-01T12:00", {"type": "datetime"})]:
            with self.subTest(value=value), self.assertRaises(ExecutionError) as caught:
                normalize_cell(value, field)
            self.assertEqual(caught.exception.code, "DATA_CONTRACT")

    def test_invalid_row_identifies_source_without_dropping_it(self):
        with self.assertRaises(ExecutionError) as caught:
            fixture(CSV.replace("0.10", "not-a-number", 1))
        self.assertEqual(caught.exception.details["row"], 2)
        self.assertEqual(caught.exception.details["source_column"], "amount")

    def test_primary_key_is_checked_after_canonicalization(self):
        for content in (CSV.replace("002,", "001,"), CSV.replace("001,", ",", 1)):
            with self.subTest(content=content), self.assertRaises(ExecutionError):
                fixture(content)
        contract = definition()
        contract["columns"][0]["type"] = "integer"
        with self.assertRaises(ExecutionError):
            fixture(CSV.replace("002,", "1,"), contract)

    def test_renaming_aliases_and_all_source_columns_are_enforced(self):
        contract = definition()
        contract["columns"][0].update(name="order_id", source="id", aliases=["订单编号"])
        contract["primary_key"] = ["order_id"]
        _, resources, _ = fixture(contract=contract)
        self.assertEqual(resources["datasets"][0]["rows"][0]["order_id"], "001")
        for mutate in (lambda c: c["columns"].pop(),
                       lambda c: c["columns"][0].update(aliases=["amount"]),
                       lambda c: c["columns"][0].update(scale=2),
                       lambda c: c.update(csv={"delimiter": "||"})):
            bad = definition()
            mutate(bad)
            with self.subTest(contract=bad), self.assertRaises(ValueError):
                fixture(contract=bad)

    def test_matching_is_unambiguous_and_does_not_mutate_input(self):
        raw = prepare_resources(uploads=[{"name": "sales.csv", "content": CSV}])
        before = copy.deepcopy(raw)
        rid = raw["datasets"][0]["id"]
        result = apply_contract(raw, {"format": VERSION, "datasets": {rid: definition()}})
        self.assertIn("data_contract", result["datasets"][0])
        self.assertEqual(raw, before)
        for mapping in ({"missing.csv": definition()}, {rid: definition(), "sales.csv": definition()}):
            with self.assertRaises(ExecutionError):
                apply_contract(raw, {"format": VERSION, "datasets": mapping})

    def test_configured_encoding_and_delimiter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.csv"
            path.write_bytes(CSV.replace(",", ";").replace("East", "东部").encode("gb18030"))
            contract = definition() | {"csv": {"encoding": "gb18030", "delimiter": ";"}}
            resources = prepare_resources(datasets=[str(path)], data_contract={"format": VERSION, "datasets": {path.name: contract}})
            self.assertEqual(resources["datasets"][0]["rows"][0]["region"], "东部")

    def test_filters_preserve_identifiers_decimal_and_null_semantics(self):
        _, pack, _ = fixture()
        data = pack["datasets"][0]
        fields = {f["name"]: f for f in data["data_contract"]["columns"]}
        for column, op, value, expected in [("id", "eq", "1", False), ("id", "eq", "001", True),
                                          ("amount", "eq", "0.100", True), ("amount", "gt", "0.09", True),
                                          ("date", "lt", "2026-01-02", True)]:
            self.assertEqual(typed_filter(data["rows"][0], {"column": column, "operator": op, "value": value}, fields), expected)
        self.assertTrue(typed_filter(data["rows"][3], {"column": "amount", "operator": "eq", "value": None}, fields))
        self.assertFalse(typed_filter(data["rows"][3], {"column": "amount", "operator": "gt", "value": "0"}, fields))
        with self.assertRaises(ExecutionError):
            typed_filter(data["rows"][0], {"column": "amount", "operator": "contains", "value": "0"}, fields)
