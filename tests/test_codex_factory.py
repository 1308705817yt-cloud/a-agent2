"""Factory lifecycle tests use fixtures only; production has no synthetic model backend."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentforge.codex_client import CodexError
from agentforge.contracts import AgentSpec, Requirements
from agentforge.factory import FactoryAgent
from agentforge.resources import read_json, write_json
from agentforge.testing import add_integrity_cases, validate_suite


GOOD_SOURCE = 'def run(context):\n    return context["result"]\n'
BAD_SOURCE = 'def run(context):\n    result = context["result"]\n    result["answer"] = "错误回答"\n    return result\n'
DOCUMENTS = [{"name": "policy.md", "content": "报销政策：出差餐补为每天 80 元。发票应在 30 天内提交。"}]


def doc_spec():
    def node(nid, tool, nxt=None, empty=None, error=None, arguments=None):
        return {"id": nid, "tool": tool, "arguments": arguments or {}, "next": nxt, "on_empty": empty, "on_error": error}
    nodes = [node("search", "documents.search", "answer", "refuse", "error", {"question": {"ref": "input.question"}, "min_score": 0.2, "top_k": 1}),
             node("answer", "documents.answer", "code"), node("refuse", "documents.refuse", "code"),
             node("error", "output.error", "code"), node("code", "code.transform")]
    return AgentSpec("政策助手", "document_qa", list(dict.fromkeys(n["tool"] for n in nodes)), "search", nodes)


def doc_suite(req, resources):
    doc = resources["documents"][0]
    cases = []
    for part in ("development", "acceptance"):
        for i, (question, expect, facts) in enumerate([
            ("出差餐补", "answer", ["每天 80 元"]), ("发票提交", "answer", ["30 天内提交"]),
            ("zxqv_planet_other", "refusal", []), ("", "error", [])]):
            cases.append({"id": part + str(i), "partition": part, "input": {"question": question},
                          "expect": expect, "source_id": doc["id"] if facts else None, "facts": facts,
                          "purpose": "核对政策与拒答", "criterion": "来源与事实"})
    suite = {"cases": cases}
    validate_suite(suite, req, resources)
    return add_integrity_cases(suite, req, resources)


class FixtureClient:
    model, timeout = None, 10
    last_metadata = {}


class FixturePlanner:
    def __init__(self, bad=False, failure=False, reject=False):
        self.client = FixtureClient()
        self.bad, self.failure, self.reject = bad, failure, reject
        self.repair_calls = []

    def requirements(self, request, resources):
        return Requirements(request, "document_qa", "带来源的政策问答", ["引用来源", "无证据拒答", "校验输入"], {})

    def workflow(self, req, resources):
        if self.failure:
            self.failure = False
            raise CodexError("fixture connection failure")
        return doc_spec()

    def tests(self, req, resources):
        suite = doc_suite(req, resources)
        if self.reject:
            for case in suite["cases"]:
                if case["id"] == "acceptance2":
                    case["input"] = {"question": "出差餐补"}
        return suite

    def code(self, req, spec, development, resources):
        assert all(c["partition"] == "development" for c in development)
        return {"source": BAD_SOURCE if self.bad else GOOD_SOURCE, "explanation": "初始生成"}

    def diagnose(self, req, spec, source, failures):
        assert all(c.get("partition", "development") == "development" for c in failures)
        return {"summary": "输出逻辑覆盖了事实", "failure_points": [{"location": "logic.py:3", "cause": "固定错误回答", "fix": "保留来源事实"}]}

    def repair(self, req, spec, source, failures, diagnosis, direction, development, resources):
        self.repair_calls.append((source, failures, direction))
        return spec, GOOD_SOURCE, "修复源码中的回答覆盖"


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.planner = FixturePlanner()
        self.factory = FactoryAgent(self.root, planner=self.planner)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, **kwargs):
        return self.factory.create("按上传政策回答，引用来源，无证据拒答", uploads=DOCUMENTS, **kwargs)

    def test_full_pipeline_and_standalone_export(self):
        state = self.factory.drive(self.create())
        self.assertEqual(state["status"], "completed")
        self.assertTrue(state["acceptance_report"]["all_passed"])
        self.assertTrue(Path(state["archive_path"]).is_file())
        result = subprocess.run([sys.executable, "-X", "utf8", "run_agent.py", "出差餐补"], cwd=state["export_path"],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("80 元", json.loads(result.stdout)["answer"])
        self.assertNotIn("codex_client.py", read_json(Path(state["export_path"]) / "manifest.json")["files"])
        verify = subprocess.run([sys.executable, "-X", "utf8", "verify_agent.py"], cwd=state["export_path"],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(all(r["all_passed"] for r in json.loads(verify.stdout)))

    def test_actual_code_revision_and_failure_locations(self):
        self.planner.bad = True
        state = self.factory.drive(self.create())
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["repairs"], 1)
        self.assertEqual(len(self.planner.repair_calls), 1)
        change = self.factory.detail(state["run_id"])["changes"][-1]
        self.assertIn('-    result["answer"]', change["code_diff"])
        self.assertNotEqual(state["versions"][0]["artifact_hash"], state["versions"][1]["artifact_hash"])

    def test_optional_human_direction_resumes_saved_failure(self):
        self.planner.bad = True
        state = self.factory.drive(self.create(review=True))
        self.assertEqual(state["status"], "waiting_for_guidance")
        final = self.factory.resume(state["run_id"], "保留原文事实")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(self.planner.repair_calls[0][2], "保留原文事实")

    def test_model_failure_retries_current_checkpoint(self):
        self.planner.failure = True
        state = self.factory.drive(self.create())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "workflow")
        final = self.factory.retry(state["run_id"])
        self.assertEqual(final["status"], "completed")

    def test_reserved_acceptance_failure_blocks_export_and_repair(self):
        self.planner.reject = True
        state = self.factory.drive(self.create())
        self.assertEqual(state["status"], "rejected")
        self.assertIsNone(state["archive_path"])
        self.assertEqual(self.planner.repair_calls, [])
        with self.assertRaises(ValueError):
            self.factory.retry(state["run_id"])

    def test_repair_budget_is_enforced(self):
        self.planner.bad = True
        state = self.factory.drive(self.create(max_repairs=0))
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["repairs"], 0)

    def test_frozen_resources_and_tests_cannot_be_changed(self):
        state = self.factory.drive(self.create())
        for name in ("resources", "test_suite", "requirements"):
            path = self.factory.run_dir(state["run_id"]) / (name + ".json")
            original = path.read_text(encoding="utf-8")
            write_json(path, {"tampered": True})
            with self.assertRaisesRegex(ValueError, "冻结"):
                self.factory.load(state["run_id"])
            path.write_text(original, encoding="utf-8")

    def test_mutated_code_and_stale_reports_are_rejected(self):
        state = self.factory.drive(self.create())
        path = self.factory.run_dir(state["run_id"]) / "versions" / "v1" / "logic.py"
        path.write_text(BAD_SOURCE, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "指纹"):
            self.factory.load(state["run_id"])

    def test_cross_process_lease_and_optimistic_writes(self):
        state = self.create()
        self.factory.store.acquire(state["run_id"], "owner-one")
        with self.assertRaisesRegex(ValueError, "运行"):
            self.factory.drive(state)
        with self.assertRaisesRegex(ValueError, "另一个进程"):
            self.factory.save(state)
        self.factory.store.release(state["run_id"], "owner-one")
        stale = self.factory.load(state["run_id"])
        self.factory.save(state)
        with self.assertRaisesRegex(ValueError, "更新"):
            self.factory.save(stale)

    def test_input_resources_and_id_validation(self):
        with self.assertRaisesRegex(ValueError, "资源|资料"):
            self.factory.create("创建一个文档 Agent")
        with self.assertRaises(ValueError):
            self.factory.load("../other")
        with self.assertRaises(ValueError):
            self.create(max_repairs=21)

    def test_recover_queued_and_preserve_orphan_version_evidence(self):
        state = self.create()
        orphan = self.factory.run_dir(state["run_id"]) / "versions" / "v1"
        orphan.mkdir(parents=True)
        (orphan / "logic.py").write_text("partial crash evidence", encoding="utf-8")
        final = self.factory.recover(state["run_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["version"], 2)
        self.assertEqual((orphan / "logic.py").read_text(encoding="utf-8"), "partial crash evidence")

    def test_runtime_fingerprint_and_export_file_integrity(self):
        state = self.factory.drive(self.create())
        self.assertEqual(state["runtime_hash"], self.factory.runtime_hash)
        path = Path(state["export_path"]) / "logic.py"
        path.write_text(BAD_SOURCE, encoding="utf-8")
        result = subprocess.run([sys.executable, "-X", "utf8", "verify_agent.py"], cwd=state["export_path"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integrity mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
