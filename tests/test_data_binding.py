import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.data_binding import OUTPUT_SOURCE, bound_resources, prepare_binding
from agentforge.resources import fingerprint
from architecture_fixtures import CSV, fixture
from test_adaptive import ScriptClient


class DataBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.req, self.resources, self.spec = fixture()
        self.rid = self.req.defaults["dataset_id"]
        self.path = self.root / "new.csv"
        self.path.write_text(CSV.replace("0.10", "2.10", 1), encoding="utf-8")
        self.binding = prepare_binding(self.spec, OUTPUT_SOURCE, self.resources, {self.rid: str(self.path)})

    def check(self, binding):
        return bound_resources(self.spec, OUTPUT_SOURCE, self.resources, binding)

    def test_compatible_snapshot_keeps_logical_id_and_original_immutable(self):
        original = copy.deepcopy(self.resources)
        result = self.check(self.binding)
        self.assertEqual(result["datasets"][0]["id"], self.rid)
        self.assertEqual(result["datasets"][0]["rows"][0]["amount"], "2.10")
        self.assertNotEqual(self.binding["bindings"][0]["content_id"], self.rid)
        self.assertEqual(self.binding["behavior_evaluation"], "not_evaluated_on_new_data")
        result["datasets"][0]["rows"][0]["amount"] = "999"
        self.assertEqual(self.resources, original)
        self.assertEqual(self.check(self.binding)["datasets"][0]["rows"][0]["amount"], "2.10")

    def test_rebound_agent_uses_saved_snapshot_when_source_file_changes(self):
        client = ScriptClient([])
        agent = AdaptiveAgent(self.spec, self.req, self.resources, OUTPUT_SOURCE, self.root, lambda: client)
        rebound = agent.with_data_binding(self.binding)
        self.path.write_text("invalid new content", encoding="utf-8")
        result = rebound.run({"metric_query": {"metric_ids": ["revenue"]}})
        self.assertIsNone(result["error"])
        self.assertEqual(result["data"]["rows"][0]["values"]["revenue"], "2.20")
        self.assertEqual(result["model_calls"], 0)
        original = agent.run({"metric_query": {"metric_ids": ["revenue"]}})
        self.assertEqual(original["data"]["rows"][0]["values"]["revenue"], "0.20")

    def test_ineligible_artifacts_and_unknown_datasets_are_rejected(self):
        for spec, source, paths in [
            (replace(self.spec, execution_policy={}), OUTPUT_SOURCE, {self.rid: str(self.path)}),
            (self.spec, OUTPUT_SOURCE + "\n", {self.rid: str(self.path)}),
            (self.spec, OUTPUT_SOURCE, {"unknown": str(self.path)}), (self.spec, OUTPUT_SOURCE, {})]:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                prepare_binding(spec, source, self.resources, paths)

    def test_incompatible_columns_precision_and_keys_fail_at_import(self):
        for content in (CSV.replace("amount", "money", 1), CSV.replace("0.10", "0.111", 1), CSV.replace("002,", "001,")):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ValueError):
                prepare_binding(self.spec, OUTPUT_SOURCE, self.resources, {self.rid: str(self.path)})

    def test_source_column_reordering_preserves_canonical_contract(self):
        # Source order is not part of the logical schema: fields bind by name.
        reordered = "\n".join(",".join(reversed(line.split(","))) for line in CSV.strip().splitlines()) + "\n"
        self.path.write_text(reordered, encoding="utf-8")
        binding = prepare_binding(self.spec, OUTPUT_SOURCE, self.resources, {self.rid: str(self.path)})
        result = self.check(binding)["datasets"][0]
        self.assertEqual(result["columns"], self.resources["datasets"][0]["columns"])
        self.assertEqual(result["rows"], self.resources["datasets"][0]["rows"])
        self.assertEqual(result["raw_columns"], list(reversed(result["columns"])))

    def test_hashes_and_re_normalization_detect_tampering(self):
        for recompute in (False, True):
            bad = copy.deepcopy(self.binding)
            bad["resources"]["datasets"][0]["rows"][0]["amount"] = "999.00"
            if recompute:
                bad["bindings"][0]["resource_hash"] = fingerprint(bad["resources"]["datasets"][0])
                bad["binding_hash"] = fingerprint({k: v for k, v in bad.items() if k != "binding_hash"})
            with self.subTest(recompute=recompute), self.assertRaises(ValueError):
                self.check(bad)

    def test_rehashed_malformed_audit_is_rejected_consistently(self):
        for mutate in (lambda b: b.update(behavior_evaluation="passed"),
                       lambda b: b["bindings"][0].update(source_name="different.csv"),
                       lambda b: b.update(bindings=[{}]),
                       lambda b: b["resources"].update(datasets=[None]),
                       lambda b: b["resources"].update(documents=[{"id": "injected"}])):
            bad = copy.deepcopy(self.binding)
            mutate(bad)
            bad["binding_hash"] = fingerprint({k: v for k, v in bad.items() if k != "binding_hash"})
            with self.subTest(binding=bad), self.assertRaises(ValueError):
                self.check(bad)
