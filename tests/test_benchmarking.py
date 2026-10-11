import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agentforge.benchmarking import load_suite, score_case, evaluate_task, run_benchmark, rescore_benchmark
from agentforge.resources import prepare_resources, read_json
from agentforge.contracts import Requirements
from agentforge.testing import csv_oracle


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.suite = load_suite()

    def test_manual_gold_matches_original_sources_and_independent_decimal_check(self):
        docs, csv = self.suite["tasks"]
        pack = prepare_resources(uploads=docs["files"])
        contents = {d["id"]: d["content"] for d in pack["documents"]}
        for case in docs["cases"]:
            for fact in case["gold"].get("facts", []):
                self.assertTrue(any(fact in contents[sid] for sid in case["gold"]["sources"]))
        resources = prepare_resources(uploads=csv["files"])
        defaults = {"dataset_id": resources["datasets"][0]["id"], "operation": "sum", "column": "net", "group_by": "branch", "filters": [], "sort_by": "group", "descending": False, "limit": None}
        req = Requirements(csv["request"], "csv_analysis", "账本", ["完整数据"], defaults)
        queries = [{"sort_by": "value", "descending": True, "limit": 2}, csv["cases"][1]["input"],
                   {"filters": [{"column": "status", "operator": "eq", "value": "returned"}]},
                   csv["cases"][3]["input"], csv["cases"][4]["input"], {}]
        for case, query in zip(csv["cases"], queries):
            self.assertEqual(csv_oracle(req, resources, query), {"dataset_id": defaults["dataset_id"], **case["gold"]})

    def test_wrong_numbers_and_correct_but_irrelevant_quotes_fail(self):
        docs, csv = self.suite["tasks"]
        resources = prepare_resources(uploads=docs["files"])
        output = {"answer": "最长借用 4 个自然日", "sources": [{"id": "loan-current", "quote": "此规则替代 2024 旧版。"}], "error": None, "abstained": False,
                  "observations": [{"tool": "custom.profile", "status": "ok"}]}
        result = score_case(docs, docs["cases"][0], output, resources)
        self.assertFalse(result["passed"])
        self.assertTrue(any("引用片段" in i for i in result["issues"]))
        resources = prepare_resources(uploads=csv["files"])
        output["data"] = {"dataset_id": resources["datasets"][0]["id"], **copy.deepcopy(csv["cases"][0]["gold"])}
        output["data"]["results"][0]["value"] = 999
        self.assertFalse(score_case(csv, csv["cases"][0], output, resources)["passed"])

    def test_gold_and_future_questions_never_enter_generation_or_runtime(self):
        csv = self.suite["tasks"][1]
        resources = prepare_resources(uploads=csv["files"])
        state = {"status": "completed", "run_id": "abc123def456", "version": 1, "artifact_hash": "frozen", "request": csv["request"]}
        fake = Mock()
        fake.resources.return_value = resources
        fake.load.return_value = state
        def run(run_id, inputs, **kwargs):
            case = next(c for c in csv["cases"] if c["input"] == inputs)
            if "reply" in case and not kwargs.get("reply"):
                return {"needs_input": True, "checkpoint": {"waiting": True}}
            return {"data": {"dataset_id": resources["datasets"][0]["id"], **case["gold"]}, "error": None, "abstained": False,
                    "observations": [{"tool": "custom.quality", "status": "ok"}]}
        fake.run.side_effect = run
        with tempfile.TemporaryDirectory() as td:
            fake.root = Path(td)
            fake.run_dir.return_value = Path(td) / "runs" / state["run_id"]
            fake.create.return_value = state
            fake.drive.return_value = state
            report = run_benchmark(fake, [csv["id"]])
            self.assertEqual(report["passed"], 6)
            self.assertEqual(fake.create.call_args.args, (csv["request"],))
            self.assertEqual(set(fake.create.call_args.kwargs), {"uploads", "max_repairs"})
            self.assertFalse(fake.planner.called)
            self.assertEqual(read_json(Path(report["report_path"]))["suite_hash"], report["suite_hash"])
            original_text = Path(report["report_path"]).read_text(encoding="utf8")
            fake.load.return_value = {**state, "status": "stopped"}
            # A previously qualified report preserves its recorded qualification on scoring replay.
            before = fake.run.call_count
            replay = rescore_benchmark(fake, report["evaluation_id"])
            self.assertEqual(fake.run.call_count, before)
            self.assertEqual(replay["model_calls"], 0)
            self.assertIsNone(replay['metrics']['first_delivery_rate'])
            self.assertEqual(replay["rescored_from"], report["evaluation_id"])
            self.assertEqual(Path(report["report_path"]).read_text(encoding="utf8"), original_text)
        for call in fake.run.call_args_list:
            self.assertNotIn("gold", call.kwargs)
            self.assertNotIn("cases", call.kwargs)

    def test_build_failure_counts_all_hidden_cases_and_never_triggers_gold_repair(self):
        csv = self.suite["tasks"][1]
        fake = Mock()
        results = evaluate_task(fake, {"status": "rejected"}, csv)
        self.assertEqual(len(results), 6)
        self.assertFalse(any(c["passed"] for c in results))
        fake.run.assert_not_called()

    def test_refutation_is_not_an_affirmative_untrusted_claim(self):
        task = self.suite["tasks"][0]
        case = next(c for c in task["cases"] if c["id"] == "untrusted-instruction")
        resources = prepare_resources(uploads=task["files"])
        output = {"answer": "并非全部免费。现行便携投影仪押金为 260 元。", "sources": [{"id": "loan-current", "quote": "便携投影仪押金为 260 元。"}],
                  "error": None, "abstained": False, "observations": [{"tool": "custom.profile", "status": "ok"}]}
        self.assertTrue(score_case(task, case, output, resources)["passed"])
        output["answer"] = "便携投影仪押金为 260 元。设备全部免费。"
        self.assertFalse(score_case(task, case, output, resources)["passed"])

    def test_candidate_behavior_does_not_change_delivery_qualification(self):
        task = self.suite["tasks"][0]
        resources = prepare_resources(uploads=task["files"])
        state = {"status": "stopped", "run_id": "abc123def456", "version": 1, "artifact_hash": "frozen", "request": task["request"]}
        fake = Mock()
        fake.resources.return_value = resources
        fake.load.return_value = state
        fake.run.return_value = {"error": {"code": "AGENT_BUDGET"}}
        with tempfile.TemporaryDirectory() as td:
            fake.root = Path(td)
            fake.run_dir.return_value = Path(td) / "runs" / state["run_id"]
            report = run_benchmark(fake, [task["id"]], state["run_id"], evaluate_generated=True)
            self.assertEqual(fake.run.call_count, 6)
            self.assertEqual(report["delivered_tasks"], 0)
            self.assertFalse(report["all_passed"])
            self.assertIsNone(report['metrics']['first_delivery_rate'])

    def test_new_suite_manual_oracles_cover_all_24_questions(self):
        suite = load_suite(Path(__file__).resolve().parents[1] / 'agentforge/benchmarks/unseen_v3.json')
        self.assertEqual(sum(len(t['cases']) for t in suite['tasks']), 24)
        for task in suite['tasks']:
            resources = prepare_resources(uploads=task['files'])
            if task['domain'] == 'document_qa':
                docs = {d['id']: d['content'] for d in resources['documents']}
                for case in task['cases']:
                    for fact in case['gold'].get('facts', []):
                        self.assertTrue(any(fact in docs[sid] for sid in case['gold']['sources']), case['id'])
                continue
            column, group = ('net_units', 'supplier') if task['id'] == 'supplier_net_units' else ('kwh', 'zone')
            defaults = {'dataset_id': resources['datasets'][0]['id'], 'operation': 'sum', 'column': column,
                        'group_by': group, 'filters': [], 'sort_by': 'value', 'descending': True, 'limit': None}
            req = Requirements(task['request'], 'csv_analysis', task['id'], ['full data'], defaults)
            for i, case in enumerate(task['cases']):
                query = case['input']
                if 'goal' in query:
                    if task['id'] == 'supplier_net_units':
                        query = [{'limit': 2}, {}, {'filters': [{'column': 'status', 'operator': 'eq', 'value': 'returned'}], 'sort_by': 'group', 'descending': False}, {}, {}, {}][i]
                    else:
                        query = {'operation': 'mean', 'sort_by': 'group', 'descending': False} if i == 5 else {}
                self.assertEqual(csv_oracle(req, resources, query), {'dataset_id': defaults['dataset_id'], **case['gold']}, task['id'] + '/' + case['id'])

    def test_failed_fresh_build_stays_in_first_delivery_denominator_and_counts_calls(self):
        task = self.suite['tasks'][0]
        state = {'status': 'failed', 'run_id': 'abc123def456', 'version': 0, 'artifact_hash': None,
                 'request': task['request'], 'performance': {'elapsed_ms': 1200}, 'repairs': 0}
        fake = Mock()
        fake.create.return_value = state
        fake.drive.return_value = state
        fake.call_metrics.return_value = {'model_calls': 3, 'completed_calls': 2, 'failed_calls': 1, 'inflight_calls': 0, 'model_elapsed_ms': 1000}
        with tempfile.TemporaryDirectory() as td:
            fake.root = Path(td)
            fake.run_dir.return_value = Path(td) / 'runs' / state['run_id']
            report = run_benchmark(fake, [task['id']])
            self.assertEqual(report['metrics']['task_count'], 1)
            self.assertEqual(report['metrics']['first_delivery_rate'], 0)
            self.assertEqual(report['metrics']['model_calls'], 3)
            self.assertEqual(report['total'], 6)
            self.assertEqual(report['passed'], 0)
            fake.run.assert_not_called()

    def test_build_exception_is_recorded_without_dropping_remaining_tasks(self):
        state = {'status': 'failed', 'run_id': 'abc123def456', 'version': 0, 'artifact_hash': None}
        fake = Mock()
        fake.create.return_value = state
        fake.drive.side_effect = RuntimeError('host interrupted')
        fake.load.return_value = state
        fake.call_metrics.return_value = {'model_calls': 1}
        with tempfile.TemporaryDirectory() as td:
            fake.root = Path(td)
            fake.run_dir.return_value = Path(td) / 'runs' / state['run_id']
            report = run_benchmark(fake)
            self.assertEqual(report['metrics']['task_count'], 2)
            self.assertEqual(report['metrics']['first_delivery_rate'], 0)
            self.assertEqual(report['total'], 12)
            self.assertEqual(fake.drive.call_count, 2)
            self.assertTrue(all(t['exception']['type'] == 'RuntimeError' for t in report['tasks']))


if __name__ == "__main__":
    unittest.main()
