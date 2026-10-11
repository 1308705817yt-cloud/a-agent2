"""Offline regressions for host negatives, CLI evidence and case-level recovery."""

import copy
import json
import subprocess
import tempfile
import threading
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agentforge.adaptive_planner import AdaptivePlanner
from agentforge.adaptive_testing import validate_extra_suite, tool_tests, execute_tool_case, SuiteValidationError
from agentforge.codex_client import CodexClient, CodexError, ModelTimeout, classify_cli_failure, object_schema
from agentforge.contracts import ExecutionError
from agentforge.custom_tools import validate_tool_arguments
from agentforge.evaluation_progress import EvaluationProgress
from agentforge.factory import FactoryAgent
from agentforge.resources import read_json, write_json
from test_adaptive import quality_spec, ScriptClient, AdaptiveFixturePlanner, DocClient
import test_adaptive as fixtures
from test_codex_factory import DOCUMENTS
from test_runtime import csv_fixture


HOST_ERROR = {"code": "INVALID_ARGUMENTS", "origin": "decision", "layer": "host"}


class HostContractTests(unittest.TestCase):
    def setUp(self):
        self.req, self.resources, _ = csv_fixture()
        self.spec = quality_spec()
        self.args = {"dataset_id": self.req.defaults["dataset_id"], "column": "amount"}

    def case(self, cid, inputs, expected=None, partition="development", negative=False):
        value = {"id": cid, "partition": partition, "tool": "custom.quality", "input": inputs,
                 "purpose": "Exercise declared boundary", "criterion": "Independent assertion"}
        value["expected_error" if negative else "expected"] = copy.deepcopy(HOST_ERROR if negative else expected)
        return value

    def suite(self):
        cases, goals = [], []
        for partition in ("development", "acceptance"):
            cases.extend([
                self.case(partition + "-empty", self.args | {"rows": []}, {"rows": 0, "missing": 0, "total": 0}, partition),
                self.case(partition + "-normal", self.args | {"rows": [{"amount": "3"}, {"amount": ""}]},
                          {"rows": 2, "missing": 1, "total": 3}, partition),
                self.case(partition + "-host", self.args | {"rows": "not-an-array"}, partition=partition, negative=True)])
            goals.append({"id": partition + "-goal", "partition": partition, "input": {"goal": "汇总", "parameters": {}},
                          "reference_query": {}, "source_id": None, "facts": [], "refusal": False,
                          "purpose": "Natural query", "criterion": "Independent aggregate"})
        cases[4].update(input=self.args | {"rows": [{"amount": "5"}]}, expected={"rows": 1, "missing": 0, "total": 5})
        return {"cases": [], "tool_cases": cases, "goal_cases": goals}

    def error_case(self, layer="host"):
        return {"id": "rows-type", "layer": layer, "input_json": json.dumps(self.args | {"rows": "not-an-array"}),
                "code": "INVALID_ARGUMENTS" if layer == "host" else "INVALID_ROWS", "description": "Non-array rows"}

    def raw(self, suite):
        value = {"tool_cases": [], "goal_cases": []}
        for case in suite["tool_cases"]:
            c = copy.deepcopy(case)
            c["input_json"] = json.dumps(c.pop("input"))
            c["expected_json"] = json.dumps(c.pop("expected")) if "expected" in c else None
            c.setdefault("expected_error", None)
            value["tool_cases"].append(c)
        for case in suite["goal_cases"]:
            c = copy.deepcopy(case)
            inputs = c.pop("input")
            c.update(goal=inputs["goal"], parameters_json=json.dumps(inputs["parameters"]),
                     reference_query_json=json.dumps(c.pop("reference_query")))
            value["goal_cases"].append(c)
        return value

    def test_legal_computation_and_host_negatives_pass_without_weakening_rows(self):
        suite = self.suite()
        validate_extra_suite(suite, self.spec, self.req, self.resources)
        results = tool_tests(self.spec, self.req, self.resources, suite, "development")
        self.assertTrue(all(c["passed"] for c in results))
        negative = results[-1]
        self.assertEqual(negative["kind"], "host_contract")
        self.assertEqual(negative["failure"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(negative["failure"]["details"]["field_path"], "input.rows")
        self.assertEqual(next(p["type"] for p in self.spec.custom_tools[0]["parameters"] if p["name"] == "rows"), "array")

    def test_host_negative_does_not_construct_or_execute_source(self):
        suite = {"tool_cases": [self.case("host", self.args | {"rows": "bad"}, negative=True)]}
        with patch("agentforge.adaptive_testing.CustomTool", side_effect=AssertionError("source must not execute")):
            self.assertTrue(execute_tool_case(self.spec.custom_tools[0], suite["tool_cases"][0], self.req, self.resources)["passed"])

    def test_parcel_contradiction_feedback_has_case_path_types_and_layer(self):
        suite = self.suite()
        case = suite["tool_cases"][2]
        case.pop("expected_error")
        case["expected"] = {"rows": 0, "missing": 0, "total": 0}
        with self.assertRaises(SuiteValidationError) as raised:
            validate_extra_suite(suite, self.spec, self.req, self.resources)
        self.assertEqual({k: raised.exception.details[k] for k in
                         ("case_id", "field_path", "expected_type", "actual_type", "rejection_layer")},
                         {"case_id": "development-host", "field_path": "input.rows", "expected_type": "array",
                          "actual_type": "string", "rejection_layer": "host"})

    def test_missing_unknown_and_top_level_types_use_same_live_guard(self):
        cases = [({}, "input.dataset_id", "string", "missing"),
                 (self.args | {"unknown": True}, "input.unknown", "absent", "boolean"),
                 ([], "input", "object", "array")]
        for inputs, field, expected, actual in cases:
            with self.subTest(field=field), self.assertRaises(ExecutionError) as raised:
                validate_tool_arguments(self.spec.custom_tools[0], inputs)
            self.assertEqual(raised.exception.details["field_path"], field)
            self.assertEqual(raised.exception.details["expected_type"], expected)
            self.assertEqual(raised.exception.details["actual_type"], actual)
            self.assertEqual(raised.exception.origin, "decision")

    def test_error_assertions_are_exclusive_and_must_actually_reject(self):
        for change in ("both", "legal", "wrong-origin", "empty-error"):
            suite = self.suite()
            case = suite["tool_cases"][2]
            if change == "both":
                case["expected"] = {}
            elif change == "legal":
                case["input"] = self.args
            elif change == "empty-error":
                case.update(input=self.args, expected={"rows": 4, "missing": 1, "total": 20.8}, expected_error={})
            else:
                case["expected_error"]["origin"] = "source"
            with self.subTest(change=change), self.assertRaises(SuiteValidationError):
                validate_extra_suite(suite, self.spec, self.req, self.resources)

    def test_design_rejects_unreachable_business_error_before_source_generation(self):
        tool = self.spec.custom_tools[0] | {"error_cases": [self.error_case("tool_result")]}
        spec = replace(self.spec, custom_tools=[tool])
        with self.assertRaises(ExecutionError) as raised:
            spec.validate(self.req)
        self.assertEqual(raised.exception.code, "DESIGN_ERROR_BOUNDARY")
        self.assertEqual(raised.exception.origin, "design")
        self.assertEqual(raised.exception.details["case_id"], "rows-type")
        self.assertEqual(raised.exception.details["rejection_layer"], "host")
        valid = replace(self.spec, custom_tools=[tool | {"error_cases": [self.error_case()]}])
        valid.validate(self.req)

    def test_negative_coverage_does_not_replace_distinct_legal_fixtures(self):
        definition = self.spec.custom_tools[0] | {"error_cases": [self.error_case()]}
        suite = self.suite()
        AdaptivePlanner.validate_generated_tool_coverage(suite, [definition])
        for remove in ("development-normal", "acceptance-host"):
            bad = copy.deepcopy(suite)
            bad["tool_cases"] = [c for c in bad["tool_cases"] if c["id"] != remove]
            with self.subTest(remove=remove), self.assertRaises(ValueError):
                AdaptivePlanner.validate_generated_tool_coverage(bad, [definition])

    def test_design_retry_receives_error_ownership_details(self):
        good = fixtures.PlannerFeedbackTests().design()
        bad = copy.deepcopy(good)
        bad["custom_tools"][0]["error_cases"][0].update(layer="tool_result", code="INVALID_ROWS")
        client = ScriptClient([bad, good])
        planner = AdaptivePlanner(client)
        spec = planner.workflow(self.req, self.resources)
        self.assertEqual(spec.custom_tools[0]["error_cases"][0]["layer"], "host")
        feedback = client.contexts[1]["previous_validation_errors"][0]
        self.assertEqual(feedback["case_id"], "rows-type")
        self.assertEqual(feedback["field_path"], "input.rows")
        self.assertEqual(feedback["expected_type"], "array")
        self.assertEqual(feedback["rejection_layer"], "host")

    def test_legal_business_error_is_a_tool_output_assertion(self):
        tool = self.spec.custom_tools[0] | {"output_schema": object_schema({"ok": {"type": "boolean"}, "code": {"type": "string"}}),
            "source": 'def run(context):\n    return {"ok": False, "code": "INVALID_ROWS"}\n',
            "error_cases": [self.error_case(), {"id": "bad-cell", "layer": "tool_result",
                "input_json": json.dumps(self.args | {"rows": [None]}), "code": "INVALID_ROWS", "description": "Invalid row contents"}]}
        spec = replace(self.spec, custom_tools=[tool])
        spec.validate(self.req, built=True)
        case = self.case("bad-cell", self.args | {"rows": [None]}, {"ok": False, "code": "INVALID_ROWS"})
        report = execute_tool_case(tool, case, self.req, self.resources)
        self.assertTrue(report["passed"])
        self.assertEqual(report["kind"], "business_error")
        self.assertIsNone(report["failure"])

    def test_model_gets_structured_feedback_and_keeps_negative_case(self):
        good = self.raw(self.suite())
        bad = copy.deepcopy(good)
        bad["tool_cases"][2].update(expected_json=json.dumps({"rows": 0, "missing": 0, "total": 0}), expected_error=None)
        client = ScriptClient([bad, good])
        planner = AdaptivePlanner(client)
        with patch("agentforge.planner.Planner.tests", return_value={"cases": []}):
            suite = planner.tests(self.req, self.resources, self.spec)
        feedback = client.contexts[1]["previous_validation_errors"][0]
        self.assertEqual(feedback["case_id"], "development-host")
        self.assertEqual(feedback["field_path"], "input.rows")
        self.assertEqual(len([c for c in suite["tool_cases"] if c.get("expected_error")]), 2)
        self.assertEqual(planner.test_attempts[0]["feedback"], feedback)


class CliEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = CodexClient(self.root, timeout=5)
        self.schema = object_schema({"connected": {"type": "boolean"}})

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, events, stderr="", code=1):
        return subprocess.CompletedProcess([], code, "\n".join(json.dumps(e) for e in events), stderr)

    def complete(self, result):
        def execute(arguments, **kwargs):
            Path(arguments[arguments.index("--output-last-message") + 1]).write_text('{"connected":true}', encoding="utf-8")
            return result
        with patch.object(self.client, "_execute", side_effect=execute):
            return self.client.complete("Check", {}, self.schema)

    def test_business_numbers_do_not_become_authentication_or_rate_errors(self):
        for count in (401, 403, 429):
            result = self.result([{"type": "item.completed", "item": {"type": "agent_message", "text": f"result count: {count}"}},
                                  {"type": "turn.failed", "error": {"message": "stream disconnected"}}])
            with self.subTest(count=count), self.assertRaises(CodexError) as raised:
                self.complete(result)
            self.assertEqual(raised.exception.failure["category"], "network")
            self.assertNotIn("登录", str(raised.exception))

    def test_unrelated_output_and_unrecognized_error_remain_unknown(self):
        for events, stderr in (([{"type": "item.completed", "item": {"type": "agent_message", "text": "HTTP 401"}}], "result count: 429"),
                               ([{"type": "turn.failed", "error": {"message": "Unrecognized failure"}}], "401 Unauthorized"),
                               ([None, 401, [], "401 Unauthorized"], "")):
            with self.subTest(events=events):
                self.assertEqual(classify_cli_failure(self.result(events, stderr))["category"], "unknown")

    def test_structured_codes_and_statuses_have_exact_redacted_basis(self):
        for error, category, field in (({"status_code": 401}, "authentication", "error.status_code"),
                                       ({"http_status": "403"}, "authorization", "error.http_status"),
                                       ({"status": 429}, "rate_limit", "error.status"),
                                       ({"code": "model_not_found"}, "model_unavailable", "error.code"),
                                       ({"error": {"code": "request_timeout"}}, "remote_timeout", "error.error.code")):
            with self.subTest(error=error):
                info = classify_cli_failure(self.result([{"type": "turn.failed", "error": error}], code=7))
                self.assertEqual(info["category"], category)
                self.assertEqual(info["exit_code"], 7)
                self.assertEqual(info["basis"]["field_path"], field)

    def test_zero_exit_failed_turn_or_error_event_never_uses_partial_response(self):
        for event in ({"type": "turn.failed", "error": {"message": "connection failed"}},
                      {"type": "error", "message": "connection failed"},
                      {"type": "item.completed", "item": {"type": "error", "message": "connection failed"}}):
            with self.subTest(event=event), self.assertRaises(CodexError):
                self.complete(self.result([event], code=0))
        self.assertTrue(self.complete(self.result([{"type": "error", "message": "connection failed"},
                                                  {"type": "turn.completed"}], code=0))["connected"])

    def test_error_event_on_stderr_is_supported(self):
        result = self.result([], json.dumps({"type": "turn.failed", "error": {"code": "network_error"}}), code=0)
        with self.assertRaises(CodexError) as raised:
            self.complete(result)
        self.assertEqual(raised.exception.failure["category"], "network")

    def test_failed_audit_does_not_store_raw_error_business_output_or_secrets(self):
        self.client.audit_dir = self.root / "audit"
        result = self.result([{"type": "item.completed", "item": {"type": "agent_message", "text": "private-business-401"}},
                              {"type": "turn.failed", "error": {"status_code": 401, "message": "Bearer test-secret"}}], code=3)
        with self.assertRaises(CodexError):
            self.complete(result)
        record = read_json(next(self.client.audit_dir.glob("*.json")))
        self.assertEqual(record["error_category"], "authentication")
        self.assertEqual(record["exit_code"], 3)
        self.assertEqual(record["classification_basis"]["signal"], "http_status_401")
        self.assertNotIn("test-secret", json.dumps(record))
        self.assertNotIn("private-business", json.dumps(record))

    def test_remote_timeout_and_process_timeout_keep_distinct_evidence(self):
        with self.assertRaises(ModelTimeout) as raised:
            self.complete(self.result([{"type": "turn.failed", "error": {"code": "request_timeout"}}]))
        self.assertEqual(raised.exception.failure["category"], "remote_timeout")
        with patch("agentforge.codex_client.resolve_codex_binary", return_value="codex.exe"), \
             patch("agentforge.codex_client.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 5)), \
             self.assertRaises(ModelTimeout) as raised:
            self.client.complete("Check", {}, self.schema)
        self.assertEqual(raised.exception.failure["category"], "process_timeout")
        self.assertEqual(raised.exception.failure["basis"]["effective_timeout_seconds"], 5)


class ProgressIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "progress.json"
        self.binding = {"artifact_hash": "artifact1", "suite_hash": "suite1", "partition": "acceptance"}
        self.cases = [{"id": "case" + str(i), "input": {"n": i}, "expected": i} for i in range(12)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_commits_survive_reload_with_every_case_and_unique_attempt(self):
        progress = EvaluationProgress(self.path, self.binding, self.cases)
        def execute(case):
            progress.begin(case)
            progress.commit(case, {"error": None, "data": case["expected"]})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(execute, self.cases))
        resumed = EvaluationProgress(self.path, self.binding, self.cases)
        self.assertNotEqual(progress.attempt_id, resumed.attempt_id)
        for case in self.cases:
            self.assertEqual(resumed.cached(case)["data"], case["expected"])
        self.assertEqual(len(resumed.attempt["reused"]), 12)
        self.assertEqual(len(read_json(self.path)["entries"]), 12)

    def test_behavior_failure_is_completed_but_model_and_dependency_failure_retry(self):
        progress = EvaluationProgress(self.path, self.binding, self.cases)
        outputs = [{"error": None, "data": "incorrect answer"}, {"error": {"code": "MODEL_ERROR"}},
                   {"error": {"code": "MODEL_TIMEOUT"}}, {"error": {"code": "TASK_TIMEOUT"}},
                   {"error": {"code": "SERVICE_CONTRACT"}, "failure": {"origin": "external"}}]
        for case, output in zip(self.cases, outputs):
            progress.begin(case)
            progress.commit(case, output)
        resumed = EvaluationProgress(self.path, self.binding, self.cases)
        self.assertIsNotNone(resumed.cached(self.cases[0]))
        self.assertTrue(all(resumed.cached(c) is None for c in self.cases[1:5]))

    def test_inflight_case_keeps_last_committed_checkpoint_after_crash(self):
        progress = EvaluationProgress(self.path, self.binding, self.cases)
        case = self.cases[0]
        progress.begin(case)
        checkpoint = {"phase": "decided", "pending": {"action": "call_tool"}, "observations": [{"id": "o1"}]}
        progress.checkpoint(case, checkpoint)
        progress.checkpoint(case, {"phase": "completed"})
        resumed = EvaluationProgress(self.path, self.binding, self.cases)
        self.assertIsNone(resumed.cached(case))
        self.assertEqual(resumed.begin(case), checkpoint)
        self.assertEqual(resumed.attempt["resumed"], [case["id"]])

    def test_binding_case_or_output_tamper_is_rejected(self):
        for change in ("artifact", "suite", "partition", "case", "output", "checkpoint"):
            progress = EvaluationProgress(None, self.binding, self.cases)
            case = self.cases[0]
            progress.begin(case)
            progress.checkpoint(case, {"phase": "ready", "calls": 1})
            progress.commit(case, {"error": {"code": "MODEL_ERROR"}})
            stored = copy.deepcopy(progress.state)
            cases, binding = copy.deepcopy(self.cases), dict(self.binding)
            if change in ("artifact", "suite", "partition"):
                binding[change + "_hash" if change != "partition" else "partition"] = "different"
            elif change == "case":
                cases[0]["expected"] = 999
            else:
                stored["entries"][case["id"]][change]["tampered"] = True
            write_json(self.path, stored)
            with self.subTest(change=change), self.assertRaises(ValueError):
                EvaluationProgress(self.path, binding, cases)


class AcceptanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_factory_retry_reuses_finished_cases_and_resumes_only_interrupted_case(self):
        counts, decisions = Counter(), []
        lock = threading.Lock()
        failed = set()
        class FlakyClient(DocClient):
            def complete(inner, task, context, schema):
                key = (inner.audit_context["partition"], inner.audit_context["case_id"])
                with lock:
                    counts[key] += 1
                    if key == ("acceptance", "acceptance0") and len(context["observations"]) == 1 and key not in failed:
                        failed.add(key)
                        raise CodexError("simulated network interruption", failure={"category": "network", "exit_code": 1,
                                             "basis": {"source": "stdout.json_event", "signal": "network_error"}})
                output = super().complete(task, context, schema)
                with lock:
                    decisions.append((key, output["tool"], len(context["observations"])))
                return output
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=FlakyClient)
        first = factory.drive(factory.create("按政策回答", uploads=DOCUMENTS))
        self.assertEqual((first["status"], first["phase"]), ("failed", "acceptance"))
        self.assertEqual(first["repairs"], 0)
        before = counts.copy()
        path = factory.run_dir(first["run_id"]) / "versions/v1/acceptance_case_progress.json"
        self.assertEqual(read_json(path)["entries"]["acceptance0"]["status"], "interrupted")
        detail = factory.detail(first["run_id"])
        acceptance = next(r for r in detail["evaluation_reports"] if r["partition"] == "acceptance")
        self.assertFalse(acceptance["all_passed"])
        recovery = next(r for r in detail["evaluation_recovery"] if r["partition"] == "acceptance")
        self.assertEqual(recovery["interrupted"], ["acceptance0"])
        second = factory.retry(first["run_id"])
        self.assertEqual(second["status"], "completed")
        report = read_json(path.parent / "acceptance.json")
        self.assertEqual(report["executed_cases"], ["acceptance0"])
        self.assertEqual(report["resumed_cases"], ["acceptance0"])
        self.assertEqual(report["new_model_calls"], 2)
        self.assertEqual(len(report["reused_cases"]), report["total"] - 1)
        self.assertTrue(all(counts[k] == n for k, n in before.items() if k != ("acceptance", "acceptance0")))
        reads = [d for d in decisions if d[0] == ("acceptance", "acceptance0") and d[1] == "documents.read"]
        self.assertEqual(len(reads), 1)
        self.assertEqual((second["version"], second["repairs"]), (1, 0))
        # Running the same frozen acceptance again consumes no additional model calls.
        cached = factory.evaluate(second, "acceptance")
        self.assertTrue(cached["all_passed"])
        self.assertEqual(cached["new_model_calls"], 0)
        self.assertEqual(cached["executed_cases"], [])

    def test_recover_after_unhandled_crash_keeps_parallel_commits_and_tool_observations(self):
        counts, crashed = Counter(), set()
        lock = threading.Lock()
        class CrashingClient(DocClient):
            def complete(inner, task, context, schema):
                key = (inner.audit_context["partition"], inner.audit_context["case_id"])
                with lock:
                    counts[key] += 1
                    if key == ("acceptance", "acceptance1") and len(context["observations"]) == 1 and not crashed:
                        crashed.add(key)
                        raise RuntimeError("simulate abrupt worker crash")
                return super().complete(task, context, schema)
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=CrashingClient)
        state = factory.create("按政策回答", uploads=DOCUMENTS)
        with self.assertRaisesRegex(RuntimeError, "abrupt worker crash"):
            factory.drive(state)
        before = counts.copy()
        path = factory.run_dir(state["run_id"]) / "versions/v1/acceptance_case_progress.json"
        saved = read_json(path)
        self.assertEqual(saved["entries"]["acceptance1"]["status"], "inflight")
        self.assertEqual(saved["entries"]["acceptance1"]["checkpoint"]["phase"], "model_inflight")
        completed = {cid for cid, e in saved["entries"].items() if e["status"] == "completed"}
        result = factory.recover(state["run_id"])
        self.assertEqual(result["status"], "completed")
        report = read_json(path.parent / "acceptance.json")
        self.assertEqual(report["resumed_cases"], ["acceptance1"])
        self.assertTrue(completed.issubset(set(report["reused_cases"])))
        self.assertTrue(all(counts[k] == n for k, n in before.items() if k[0] != "acceptance" or k[1] in completed))
        case = next(c for c in report["cases"] if c["id"] == "acceptance1")
        self.assertEqual(len([o for o in case["actual"]["observations"] if o["tool"] == "documents.read"]), 1)

    def test_mixed_model_failures_do_not_become_blanket_timeout(self):
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        state = factory.drive(factory.create("按政策回答", uploads=DOCUMENTS))
        report = {"cases": [{"actual": {"error": {"code": code}}, "passed": False}
                            for code in ("MODEL_ERROR", "MODEL_TIMEOUT")]}
        with patch("agentforge.factory.evaluate_adaptive", return_value=report), self.assertRaises(CodexError) as raised:
            factory.evaluate(state, "acceptance")
        self.assertNotIsInstance(raised.exception, ModelTimeout)
        self.assertEqual(raised.exception.failure["category"], "mixed_model_failures")
        self.assertEqual(raised.exception.failure["basis"]["error_codes"], ["MODEL_ERROR", "MODEL_TIMEOUT"])


if __name__ == "__main__":
    unittest.main()
