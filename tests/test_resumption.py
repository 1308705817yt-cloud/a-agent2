import copy
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.codex_client import CodexError
from agentforge.executions import create_execution, execute_record, execution_path, continue_execution, recover_interrupted, locked
from agentforge.factory import FactoryAgent
from agentforge.resources import read_json, write_json
from test_adaptive import AdaptiveFixturePlanner, DocClient, ScriptClient, adaptive_doc_spec, decision
from test_codex_factory import DOCUMENTS, GOOD_SOURCE
from agentforge.contracts import Requirements
from agentforge.resources import prepare_resources


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.resources = prepare_resources(uploads=DOCUMENTS)
        self.req = Requirements("政策问答", "document_qa", "政策", ["来源"], {})
        self.spec = adaptive_doc_spec(self.resources)
        self.rid = self.resources["documents"][0]["id"]
        self.finish = decision("finish", answer=DOCUMENTS[0]["content"], sources=[{"id": self.rid, "quote": DOCUMENTS[0]["content"]}])

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, decisions):
        self.client = ScriptClient(decisions)
        return AdaptiveAgent(self.spec, self.req, self.resources, GOOD_SOURCE, self.root, lambda: self.client)

    def tool_decisions(self):
        return [decision(tool="documents.read", args={"ids": [self.rid]}), decision(tool="custom.policy_profile", args={"document_id": self.rid})]

    def test_human_reply_preserves_evidence_and_previous_plan(self):
        first = self.agent([*self.tool_decisions(), decision("ask_user", answer="你问的是哪项政策？")]).run("政策？")
        result = self.agent([self.finish]).run("政策？", checkpoint=first["checkpoint"], reply={"text": "出差餐补"})
        self.assertIsNone(result["error"])
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(result["new_model_calls"], 1)
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(self.client.contexts[0]["human_replies"], [{"text": "出差餐补"}])
        self.assertEqual(self.client.contexts[0]["previous_plan"], ["Inspect evidence", "Compute and verify"])

    def test_backend_retry_does_not_replay_completed_reads(self):
        first = self.agent([*self.tool_decisions(), CodexError("network")]).run("餐补？")
        result = self.agent([self.finish]).run("餐补？", checkpoint=first["checkpoint"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(len(result["observations"]), 2)

    def test_crash_after_decision_recovers_pending_tool_without_new_decision(self):
        snapshots = []
        def crash(state):
            snapshots.append(state)
            if state["phase"] == "decided":
                raise RuntimeError("simulated process crash")
        with self.assertRaisesRegex(RuntimeError, "process crash"):
            self.agent(self.tool_decisions()).run("餐补？", on_checkpoint=crash)
        saved = snapshots[-1]
        result = self.agent([self.tool_decisions()[1], self.finish]).run("餐补？", checkpoint=saved)
        self.assertIsNone(result["error"])
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(len([e for e in result["trace"] if e["type"] == "decision"]), 3)

    def test_uncommitted_model_request_is_counted_on_recovery(self):
        snapshots = []
        def crash(state):
            snapshots.append(state)
            if state["phase"] == "model_inflight":
                raise RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.agent([]).run("餐补？", on_checkpoint=crash)
        result = self.agent([*self.tool_decisions(), self.finish]).run("餐补？", checkpoint=snapshots[-1])
        self.assertEqual(result["model_calls"], 4)
        self.assertTrue(any(e["type"] == "recovery" for e in result["trace"]))

    def test_binding_changes_and_missing_reply_are_rejected(self):
        first = self.agent([decision("ask_user", answer="哪项政策？")]).run("政策？")
        for inputs, reply in (("不同问题", {"text": "餐补"}), ("政策？", None)):
            with self.assertRaises(ValueError):
                self.agent([]).run(inputs, checkpoint=first["checkpoint"], reply=reply)

    def test_cumulative_budget_cannot_be_reset_by_resume(self):
        first = self.agent([decision(tool="resources.list") for _ in range(6)]).run("未知？")
        second = self.agent([decision(tool="resources.list") for _ in range(6)]).run("未知？", checkpoint=first["checkpoint"])
        third = self.agent([decision(tool="resources.list") for _ in range(6)]).run("未知？", checkpoint=second["checkpoint"])
        fourth = self.agent([]).run("未知？", checkpoint=third["checkpoint"])
        self.assertEqual(fourth["model_calls"], 18)
        self.assertEqual(fourth["new_model_calls"], 0)


class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        self.state = self.factory.drive(self.factory.create("政策问答", uploads=DOCUMENTS))

    def tearDown(self):
        self.temp.cleanup()

    def waiting(self):
        self.factory.runtime_client_factory = lambda: ScriptClient([decision("ask_user", answer="请说明政策主题。")])
        record = create_execution(self.factory, self.state["run_id"], "政策？")
        return execute_record(self.factory, self.state["run_id"], record["execution_id"])

    def test_persistent_reply_survives_restart(self):
        first = self.waiting()
        self.assertEqual(first["status"], "waiting_input")
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        queued = continue_execution(factory, self.state["run_id"], first["execution_id"], {"text": "出差餐补"})
        self.assertEqual(queued["status"], "queued")
        final = execute_record(factory, self.state["run_id"], first["execution_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"]["model_calls"], 4)
        self.assertEqual(len(final["attempts"]), 1)

    def test_restart_marks_checkpoint_recoverable_without_automatic_model_calls(self):
        first = self.waiting()
        path = execution_path(self.factory, self.state["run_id"], first["execution_id"])
        first.update(status="running")
        write_json(path, first)
        recover_interrupted(self.factory)
        self.assertEqual(read_json(path)["status"], "interrupted")
        self.assertEqual(read_json(path)["checkpoint"]["calls"], 1)
        queued = continue_execution(self.factory, self.state["run_id"], first["execution_id"], {"text": "餐补"})
        self.assertEqual(queued["status"], "queued")

    def test_checkpoint_tamper_and_duplicate_claim_are_rejected(self):
        first = self.waiting()
        path = execution_path(self.factory, self.state["run_id"], first["execution_id"])
        with locked(path):
            with self.assertRaises(ValueError):
                continue_execution(self.factory, self.state["run_id"], first["execution_id"], {"text": "餐补"})
        first["checkpoint"]["calls"] = 0
        write_json(path, first)
        with self.assertRaisesRegex(ValueError, "指纹"):
            continue_execution(self.factory, self.state["run_id"], first["execution_id"], {"text": "餐补"})

    def test_reply_queued_before_crash_is_preserved_on_explicit_recovery(self):
        first = self.waiting()
        continue_execution(self.factory, self.state["run_id"], first["execution_id"], {"text": "餐补"})
        recover_interrupted(self.factory)
        queued = continue_execution(self.factory, self.state["run_id"], first["execution_id"])
        self.assertEqual(queued["reply"], {"text": "餐补"})
        self.factory.runtime_client_factory = DocClient
        final = execute_record(self.factory, self.state["run_id"], first["execution_id"])
        self.assertEqual(final["status"], "completed")

    def test_another_process_cannot_claim_the_same_execution(self):
        first = self.waiting()
        path = execution_path(self.factory, self.state["run_id"], first["execution_id"])
        script = "import sys; from pathlib import Path; from agentforge.executions import locked; "
        script += "claim=locked(Path(sys.argv[1])); claim.__enter__()"
        with locked(path):
            process = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(path)],
                                     cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf8", timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("另一个进程", process.stderr)
        self.assertEqual(read_json(path)["status"], "waiting_input")


if __name__ == "__main__":
    unittest.main()
