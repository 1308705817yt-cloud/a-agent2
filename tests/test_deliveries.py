"""Delivery choice, actual computations, independent exports and model protocol boundaries."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentforge.codex_client import CodexClient, object_schema
from agentforge.delivery_policy import select_delivery
from agentforge.executions import create_execution, execute_record
from agentforge.factory import FactoryAgent
from agentforge.program_delivery import DataProgram, evaluate_cases, validate_recipe
from agentforge.resources import read_json, write_json
from architecture_fixtures import CSV, definition, fixture


def recipe(operation="csv.query"):
    args = {"dataset_id": "sales.csv", "operation": "sum", "column": "quantity", "group_by": None,
            "filters": [{"column": "region", "operator": "eq", "value": {"$param": "region"}}]}
    if operation == "csv.metrics":
        args = {"dataset_id": "sales.csv", "metric_ids": ["revenue"], "dimensions": [], "filters": args["filters"]}
    elif operation == "csv.sql":
        args = {"sql": "SELECT SUM(CAST(quantity AS INTEGER)) AS total FROM dataset_1 WHERE region = :region",
                "parameters": {"region": {"$param": "region"}}}
    return {"format": "agentforge-program-v1", "operation": operation, "arguments": args,
            "parameters": {"region": {"type": "string"}}, "defaults": {"region": "East"}}


def cases(expected=4):
    return [{"id": "east-units", "input": {}, "checks": [{"path": ["data", "results", 0, "value"], "equals": expected}]}]


class FakeClient:
    model = "fixture"
    timeout = 5
    deadline = None
    last_metadata = {}

    def __init__(self, answers=()):
        self.answers = iter(answers)
        self.contexts = []
        self.solves = 0

    def complete(self, task, context, schema):
        self.contexts.append(copy.deepcopy(context))
        return next(self.answers)

    def solve(self, task, context, schema, output_dir):
        self.solves += 1
        output_dir.mkdir(parents=True)
        (output_dir / "proof.txt").write_text("executed fixture", encoding="utf-8")
        return {"answer": "fixture answer", "evidence": ["proof.txt"], "limitations": []}


class DeliveryPolicyTests(unittest.TestCase):
    def test_fifteen_delivery_routes(self):
        routes = [({}, "direct"), ({"reusable": False}, "direct"), ({"dynamic": True}, "direct"),
                  ({"recipe": {}}, "direct"), ({"mode": "direct"}, "direct"),
                  ({"reusable": True}, "program"), ({"reusable": True, "recipe": {}}, "program"),
                  ({"mode": "program"}, "program"), ({"mode": "program", "reusable": True}, "program"),
                  ({"mode": "program", "recipe": {}}, "program"),
                  ({"mode": "agent"}, "agent"), ({"mode": "agent", "reusable": True}, "agent"),
                  ({"mode": "agent", "dynamic": True}, "agent"),
                  ({"reusable": True, "dynamic": True}, "agent"),
                  ({"mode": "agent", "reusable": True, "dynamic": True}, "agent")]
        for args, expected in routes:
            with self.subTest(args=args):
                self.assertEqual(select_delivery(**args)["kind"], expected)

    def test_contradictory_inputs_are_not_silently_discarded(self):
        for args in ({"mode": "bogus"}, {"reusable": "false"}, {"dynamic": 1},
                     {"mode": "agent", "recipe": {}}, {"mode": "program", "dynamic": True},
                     {"mode": "direct", "reusable": True}, {"dynamic": True, "recipe": {}}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                select_delivery(**args)


class ProgramTests(unittest.TestCase):
    def setUp(self):
        self.resources = fixture()[1]

    def test_query_and_named_parameter(self):
        program = DataProgram(recipe(), self.resources)
        self.assertEqual(program.run({})["data"]["results"][0]["value"], 4)
        result = program.run({"region": "West"})
        self.assertEqual(result["data"]["results"][0]["value"], 3)
        self.assertEqual(result["execution"]["model_calls"], 0)

    def test_metrics_keep_decimal_semantics(self):
        result = DataProgram(recipe("csv.metrics"), self.resources).run({})
        self.assertIsNone(result["error"])
        self.assertIn('"0.20"', json.dumps(result["data"]))

    def test_sql_is_parameterized(self):
        program = DataProgram(recipe("csv.sql"), self.resources)
        self.assertEqual(program.run({})["data"]["rows"], [[4]])
        self.assertEqual(program.run({"region": "East' OR 1=1 --"})["data"]["rows"], [[None]])

    def test_unknown_and_wrong_type_parameters_are_rejected(self):
        program = DataProgram(recipe(), self.resources)
        for params in ({"operation": "count"}, {"region": 1}, "East", {"region": float("nan")}):
            with self.subTest(params=params):
                self.assertEqual(program.run(params)["error"]["code"], "INVALID_INPUT")

    def test_mutating_sql_fails(self):
        value = recipe("csv.sql")
        value["arguments"]["sql"] = "DELETE FROM dataset_1 WHERE region=:region"
        self.assertIsNotNone(DataProgram(value, self.resources).run({})["error"])

    def test_recipe_rejects_ignored_or_unbound_parameters(self):
        variants = []
        value = recipe(); value["parameters"]["unused"] = {"type": "string"}; value["defaults"]["unused"] = "x"; variants.append(value)
        value = recipe(); value["defaults"] = {}; variants.append(value)
        value = recipe(); value["arguments"]["dataset_id"] = {"$param": "region"}; variants.append(value)
        value = recipe(); value["parameters"]["region"]["pattern"] = ".*"; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_recipe(value, self.resources)

    def test_independent_assertions_and_missing_evidence(self):
        program = DataProgram(recipe(), self.resources)
        self.assertFalse(evaluate_cases(program, [])["all_passed"])
        self.assertEqual(evaluate_cases(program, [])["scope"], "not_evaluated")
        self.assertTrue(evaluate_cases(program, cases())["all_passed"])
        self.assertFalse(evaluate_cases(program, cases(99))["all_passed"])
        value = cases(); value[0]["checks"][0]["path"] = ["data", "absent"]
        self.assertFalse(evaluate_cases(program, value)["all_passed"])


class DeliveryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = FakeClient()
        self.factory = FactoryAgent(self.root, planner=SimpleNamespace(client=self.client))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **kwargs):
        return self.factory.create("sum quantity for requested region", uploads=[{"name": "sales.csv", "content": CSV}],
                                   data_contract={"format": "agentforge-data-contract-v1", "datasets": {"sales.csv": definition()}},
                                   delivery_mode="auto", **kwargs)

    def completed_program(self, **kwargs):
        return self.factory.drive(self.create(reusable=True, recipe=recipe(), **kwargs))

    def test_program_needs_no_model_and_runs_with_record(self):
        state = self.completed_program(acceptance_cases=cases())
        self.assertEqual(state["status"], "completed")
        self.assertEqual(self.client.contexts, [])
        self.assertEqual(self.client.solves, 0)
        record = create_execution(self.factory, state["run_id"], {"region": "West"})
        result = execute_record(self.factory, state["run_id"], record["execution_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["data"]["results"][0]["value"], 3)

    def test_program_rejection_prevents_export(self):
        state = self.completed_program(acceptance_cases=cases(999))
        self.assertEqual(state["status"], "rejected")
        self.assertIsNone(state["archive_path"])
        self.assertFalse((self.root / "dist" / (state["run_id"] + ".zip")).exists())

    def test_no_business_cases_is_honest_in_manifest(self):
        state = self.completed_program()
        manifest = read_json(Path(state["export_path"]) / "manifest.json")
        self.assertEqual(manifest["business_validation"], "not_evaluated")

    def test_export_runs_without_codex_or_repository(self):
        state = self.completed_program(acceptance_cases=cases())
        target = self.root / "unpacked"
        with zipfile.ZipFile(state["archive_path"]) as archive:
            archive.extractall(target)
        env = dict(os.environ, AGENTFORGE_CODEX_BIN="does-not-exist", PYTHONPATH="")
        for args in (["--verify"], ["--input-json", '{"region":"West"}']):
            completed = subprocess.run([sys.executable, "-X", "utf8", "-B", "run_program.py", *args],
                                       cwd=target, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["data"]["results"][0]["value"], 3)

    def test_export_tampering_is_rejected(self):
        state = self.completed_program()
        bundle = self.factory.run_dir(state["run_id"]) / "delivery"
        with (bundle / "agentforge/program_delivery.py").open("a", encoding="utf-8") as f:
            f.write("\n# tampered\n")
        with self.assertRaisesRegex(ValueError, "完整性"):
            self.factory.run(state["run_id"], {})

    def test_queued_binding_is_snapshot_and_does_not_inherit_acceptance(self):
        state = self.completed_program(acceptance_cases=cases())
        new = self.root / "next.csv"
        new.write_text(CSV.replace("001,East,0.10,1,paid", "001,East,0.10,8,paid"), encoding="utf-8")
        rid = self.factory.resources(state)["datasets"][0]["id"]
        record = create_execution(self.factory, state["run_id"], {}, bindings={rid: str(new)})
        new.write_text("wrong,schema\nx,y\n", encoding="utf-8")
        result = execute_record(self.factory, state["run_id"], record["execution_id"])["result"]
        self.assertEqual(result["data"]["results"][0]["value"], 11)
        self.assertEqual(result["data_binding"]["behavior_evaluation"], "not_evaluated_on_new_data")

    def test_direct_uses_tool_session_and_is_not_reusable(self):
        state = self.factory.drive(self.create())
        self.assertEqual(state["status"], "completed")
        self.assertEqual(self.client.solves, 1)
        self.assertEqual(self.client.contexts, [])
        self.assertTrue((Path(state["export_path"]) / "evidence/proof.txt").is_file())
        with self.assertRaises(ValueError):
            create_execution(self.factory, state["run_id"], {})

    def test_direct_fixed_recipe_does_not_call_model(self):
        state = self.factory.drive(self.create(recipe=recipe()))
        self.assertEqual(state["delivery"]["kind"], "direct")
        self.assertEqual(self.client.solves, 0)
        self.assertFalse((Path(state["export_path"]) / "run_program.py").exists())

    def test_compiler_sees_no_held_out_assertions(self):
        self.client.answers = iter([{"status": "ready", "reason": "compile", "recipe_json": json.dumps(recipe())}])
        state = self.factory.drive(self.create(reusable=True, acceptance_cases=cases()))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(self.client.contexts), 1)
        self.assertNotIn("east-units", json.dumps(self.client.contexts))
        self.assertNotIn("acceptance_cases", self.client.contexts[0])

    def test_missing_business_definition_waits_and_can_resume(self):
        self.client.answers = iter([{"status": "needs_input", "reason": "which region?", "recipe_json": ""},
                                   {"status": "ready", "reason": "compile", "recipe_json": json.dumps(recipe())}])
        state = self.factory.drive(self.create(reusable=True))
        self.assertEqual(state["status"], "waiting_for_guidance")
        state = self.factory.resume(state["run_id"], "East")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(self.client.contexts[-1]["directions"][-1]["text"], "East")

    def test_unsupported_does_not_silently_build_agent(self):
        self.client.answers = iter([{"status": "unsupported", "reason": "requires semantic decisions", "recipe_json": ""}])
        state = self.factory.drive(self.create(reusable=True))
        self.assertEqual(state["status"], "unsupported")
        self.assertEqual(state["version"], 0)
        self.assertIsNone(state["spec"])

    def test_frozen_request_cannot_change_delivery_kind(self):
        state = self.create()
        state["delivery"]["kind"] = "program"
        self.factory.save(state)
        with self.assertRaisesRegex(ValueError, "冻结需求"):
            self.factory.load(state["run_id"])


class DirectCodexProtocolTests(unittest.TestCase):
    def test_tool_enabled_session_and_legacy_decisions_remain_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = CodexClient(root)
            captured = []
            def execute(arguments, **kwargs):
                captured.append((arguments, kwargs))
                work = Path(kwargs["cwd"])
                if (work / "outputs").exists():
                    (work / "outputs/proof.py").write_text("print(1)", encoding="utf-8")
                Path(arguments[arguments.index("--output-last-message") + 1]).write_text('{"ok":true}', encoding="utf-8")
                return subprocess.CompletedProcess(arguments, 0, '{"type":"turn.completed","usage":{}}\n', "")
            schema = object_schema({"ok": {"type": "boolean"}})
            with patch.object(client, "_execute", side_effect=execute):
                client.solve("compute", {"resources": fixture()[1]}, schema, root / "evidence")
                client.complete("plan", {}, schema)
            self.assertIn("workspace-write", captured[0][0])
            self.assertIn("read-only", captured[1][0])
            self.assertTrue((root / "evidence/proof.py").is_file())
            self.assertIn("execute local commands", captured[0][1]["input_text"])
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", captured[0][0])


if __name__ == "__main__":
    unittest.main()
