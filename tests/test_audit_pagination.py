import copy
import base64
import json
import queue
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from agentforge.codex_client import CodexClient, CodexError, DeadlineExceeded, ModelTimeout, object_schema
from agentforge.storage import Store
from agentforge.resources import fingerprint, read_json, write_json
from agentforge.executions import create_execution, execute_record, continue_execution, execution_path
from agentforge.trusted_tools import TrustedTools
from agentforge.contracts import ExecutionError
from agentforge.server import recover_build_queue
from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.factory import FactoryAgent
from test_adaptive import ScriptClient, quality_spec, decision, AdaptiveFixturePlanner, DocClient
from test_trusted_tools import sqlite_tool
from test_runtime import csv_fixture
from test_codex_factory import GOOD_SOURCE, DOCUMENTS


class CallAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_request_timeout_is_not_reclassified_when_clock_crosses_task_deadline(self):
        client = CodexClient(self.root, timeout=5)
        client.deadline = 10
        with patch('agentforge.codex_client.time.monotonic', side_effect=[0, 11]), \
             patch('agentforge.codex_client.resolve_codex_binary', return_value='codex'), \
             patch('agentforge.codex_client.subprocess.run', side_effect=subprocess.TimeoutExpired('codex', 5)), \
             self.assertRaises(ModelTimeout):
            client._execute(['exec'])

    def test_clamped_deadline_is_classified_by_its_original_scope(self):
        client = CodexClient(self.root, timeout=5)
        client.deadline, client.deadline_scope = 2, 'build'
        with patch('agentforge.codex_client.time.monotonic', return_value=0), \
             patch('agentforge.codex_client.resolve_codex_binary', return_value='codex'), \
             patch('agentforge.codex_client.subprocess.run', side_effect=subprocess.TimeoutExpired('codex', 2)) as run:
            with self.assertRaises(DeadlineExceeded) as caught:
                client._execute(['exec'])
            self.assertEqual(run.call_args.kwargs['timeout'], 2)
            self.assertEqual(caught.exception.scope, 'build')

    def test_parallel_client_metadata_and_disk_audit_remain_bound_to_each_call(self):
        client = CodexClient(self.root)
        client.audit_dir = self.root / 'calls'
        barrier = threading.Barrier(2)
        finished = threading.Barrier(2)
        schema = object_schema({'number': {'type': 'integer'}})
        def execute(args, **kwargs):
            number = json.loads(kwargs['input_text'].split('\n', 1)[1])['context']['number']
            Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps({'number': number}))
            barrier.wait(timeout=5)
            return subprocess.CompletedProcess(args, 0, json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': number}}), '')
        def call(number):
            client.audit_context = {'case_id': str(number)}
            client.deadline = number + 100
            result = client.complete('test', {'number': number}, schema)
            finished.wait(timeout=5)
            return result, copy.deepcopy(client.last_metadata), client.deadline
        with patch.object(client, '_execute', side_effect=execute), ThreadPoolExecutor(2) as pool:
            outputs = list(pool.map(call, [7, 13]))
        self.assertEqual(len(list(client.audit_dir.glob('*.json'))), 2)
        for result, metadata, deadline in outputs:
            number = result['number']
            self.assertEqual(metadata['usage']['input_tokens'], number)
            self.assertEqual(metadata['context']['case_id'], str(number))
            self.assertEqual(deadline, number + 100)
            self.assertEqual(read_json(client.audit_dir / (metadata['call_id'] + '.json')), metadata)

    def test_failed_and_inflight_calls_are_retained_without_prompt_data(self):
        client = CodexClient(self.root)
        client.audit_dir = self.root / 'calls'
        with patch.object(client, '_execute', side_effect=ModelTimeout('timeout')), self.assertRaises(ModelTimeout):
            client.complete('private task', {'secret': 'do-not-store'}, object_schema({}))
        record = read_json(next(client.audit_dir.glob('*.json')))
        self.assertEqual(record['error_code'], 'MODEL_TIMEOUT')
        self.assertEqual(record['status'], 'failed')
        self.assertNotIn('do-not-store', json.dumps(record))

    def test_runtime_request_timeout_is_retryable_and_not_task_timeout(self):
        req, resources, _ = csv_fixture()
        client = ScriptClient([decision(tool='csv.preview'), ModelTimeout('request limit')])
        agent = AdaptiveAgent(quality_spec(), req, resources, GOOD_SOURCE, self.root, lambda: client)
        output = agent.run({})
        self.assertEqual(output['error']['code'], 'MODEL_TIMEOUT')
        self.assertEqual(output['checkpoint']['phase'], 'retryable')
        self.assertEqual(len(output['checkpoint']['observations']), 1)
        self.assertEqual(output['model_calls'], 2)

    def test_runtime_timeout_does_not_enter_design_repair(self):
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        state = factory.drive(factory.create('政策问答', uploads=DOCUMENTS))
        report = {'cases': [{'actual': {'error': {'code': 'TASK_TIMEOUT', 'scope': 'runtime'}}}]}
        with patch('agentforge.factory.evaluate_adaptive', return_value=report), self.assertRaises(DeadlineExceeded) as caught:
            factory.evaluate(state, 'development')
        self.assertEqual(caught.exception.scope, 'runtime')
        self.assertTrue((factory.run_dir(state['run_id']) / 'versions/v1/development_time_limit.json').exists())

    def test_already_decided_finish_can_resume_at_call_limit_without_new_request(self):
        factory = FactoryAgent(self.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        state = factory.drive(factory.create('政策问答', uploads=DOCUMENTS))
        factory.runtime_client_factory = lambda: ScriptClient([decision(tool='resources.list'), DeadlineExceeded('limit')])
        execution = create_execution(factory, state['run_id'], '制度未提供的赔偿金额？')
        record = execute_record(factory, state['run_id'], execution['execution_id'])
        saved = record['checkpoint']
        # A legal checkpoint after the last permitted decision, before host verification.
        saved['calls'] = state['spec']['max_steps'] * 3
        saved['pending'] = decision('finish', answer='制度未提供赔偿金额。', abstained=True)
        record['checkpoint_hash'] = fingerprint(saved)
        write_json(execution_path(factory, state['run_id'], execution['execution_id']), record)
        continue_execution(factory, state['run_id'], execution['execution_id'], max_seconds=0)
        client = ScriptClient([])
        factory.runtime_client_factory = lambda: client
        result = execute_record(factory, state['run_id'], execution['execution_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result']['new_model_calls'], 0)
        self.assertEqual(client.contexts, [])

    def test_parallel_trusted_reads_do_not_exchange_result_hashes(self):
        registry = TrustedTools(self.root, [sqlite_tool()])
        barrier, finished = threading.Barrier(2), threading.Barrier(2)
        def read(tool, args):
            barrier.wait(timeout=5)
            return {'rows': [{'sku': args['branch'], 'stock': len(args['branch'])}], 'row_count': 1}
        def invoke(branch):
            result = registry.invoke('trusted.stock', {'branch': branch})
            finished.wait(timeout=5)
            return result, copy.deepcopy(registry.last_audit)
        with patch.object(registry, 'sqlite', side_effect=read), ThreadPoolExecutor(2) as pool:
            values = list(pool.map(invoke, ['East', 'Western']))
        self.assertEqual(len({a['call_id'] for _, a in values}), 2)
        for result, audit in values:
            self.assertEqual(audit['result_hash'], fingerprint(result))
            self.assertEqual(audit['status'], 'completed')

    def test_invalid_arguments_and_tool_timeout_have_complete_audit(self):
        registry = TrustedTools(self.root, [sqlite_tool()])
        with self.assertRaises(ExecutionError):
            registry.invoke('trusted.stock', {'branch': 7})
        self.assertEqual(registry.last_audit['error_code'], 'INVALID_ARGUMENTS')
        self.assertIn('elapsed_ms', registry.last_audit)
        with patch.object(registry, 'sqlite', side_effect=TimeoutError()), self.assertRaises(ExecutionError) as caught:
            registry.invoke('trusted.stock', {'branch': 'East'})
        self.assertEqual(caught.exception.code, 'TOOL_TIMEOUT')
        self.assertEqual(registry.last_audit['error_code'], 'TOOL_TIMEOUT')


class PaginationTests(unittest.TestCase):
    def test_scan_recovers_jobs_beyond_200_even_when_each_page_is_updated(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            for n in range(451):
                store.save({'run_id': f'{n:012x}', 'created_at': n, 'status': 'queued' if n < 7 else 'completed'})
            seen = []
            for state in store.iter_all(limit=37):
                seen.append(state['run_id'])
                state['touched'] = True
                store.save(state)
            self.assertEqual(len(seen), 451)
            self.assertEqual(len(set(seen)), 451)
            fake, pending = Mock(), queue.Queue()
            fake.store = store
            self.assertEqual(recover_build_queue(fake, pending), 7)
            self.assertEqual({pending.get_nowait() for _ in range(7)}, {f'{n:012x}' for n in range(7)})
            self.assertEqual(store.page(10)['total'], 451)

    def test_same_creation_time_is_stable_and_new_jobs_do_not_shift_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            for n in range(5):
                store.save({'run_id': f'{n:012x}', 'created_at': 100, 'status': 'completed'})
            first = store.page(2)
            store.save({'run_id': 'ffffffffffff', 'created_at': 200, 'status': 'queued'})
            second = store.page(2, first['next_cursor'])
            third = store.page(2, second['next_cursor'])
            self.assertEqual([s['run_id'] for s in first['jobs'] + second['jobs'] + third['jobs']], [f'{n:012x}' for n in reversed(range(5))])
            self.assertIsNone(third['next_cursor'])
            oversized = base64.urlsafe_b64encode(json.dumps([2 ** 128, 'abcdef123456']).encode()).decode()
            for bad in ('', 'not-base64', 'WzEsImJhZCJd', oversized):
                with self.assertRaises(ValueError):
                    store.page(10, bad)

    def test_legacy_index_migration_retains_all_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / '.agentforge').mkdir()
            with closing(sqlite3.connect(root / '.agentforge/agentforge.db')) as conn, conn:
                conn.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,updated REAL NOT NULL,payload TEXT NOT NULL)')
                state = {'run_id': 'abcdef123456', 'created_at': 5, 'status': 'failed'}
                conn.execute('INSERT INTO jobs VALUES(?,?,?,?)', (state['run_id'], 1, 10, json.dumps(state)))
            store = Store(root)
            self.assertEqual(store.list(), [state])
            self.assertEqual(store.page()['status_counts'], {'failed': 1})


if __name__ == '__main__':
    unittest.main()
