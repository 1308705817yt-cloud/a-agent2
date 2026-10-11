import http.client
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentforge.factory import FactoryAgent
from test_codex_factory import FixturePlanner, DOCUMENTS


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        factory = FactoryAgent(cls.root, planner=FixturePlanner())
        cls.state = factory.drive(factory.create("文档问答，引用原文", uploads=DOCUMENTS))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "agentforge", "--workspace", str(cls.root),
                                        "serve", "--port", str(cls.port)], cwd=Path(__file__).resolve().parents[1],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/api/jobs")
                response = conn.getresponse()
                response.read()
                conn.close()
                if response.status == 200:
                    return
            except OSError:
                time.sleep(0.05)
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.temp.cleanup()
        raise RuntimeError("Dashboard did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.temp.cleanup()

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(data) if data is not None else None
        options = {"Content-Type": "application/json"} if payload else {}
        options.update(headers or {})
        conn.request(method, path, payload, options)
        response = conn.getresponse()
        content = response.read()
        result = response.status, dict(response.getheaders()), content
        conn.close()
        return result

    def test_static_assets_and_csp(self):
        for path in ("/", "/app.js", "/style.css"):
            status, headers, content = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(content)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_jobs_detail_and_tool_registry(self):
        _, _, content = self.request("GET", "/api/jobs")
        self.assertEqual(json.loads(content)["jobs"][0]["status"], "completed")
        _, _, content = self.request("GET", "/api/jobs/" + self.state["run_id"])
        self.assertIn("def run", json.loads(content)["source"])
        _, _, content = self.request("GET", "/api/tools")
        self.assertIn("csv.query", [t["id"] for t in json.loads(content)["tools"]])

    def test_pagination_metadata_and_invalid_query(self):
        status, _, content = self.request("GET", "/api/jobs?limit=1")
        page = json.loads(content)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(page['total'], 1)
        self.assertIn('status_counts', page)
        self.assertIn('next_cursor', page)
        for query in ('limit=0', 'limit=201', 'cursor=bad', 'limit=1&limit=2', 'other=1'):
            self.assertEqual(self.request('GET', '/api/jobs?' + query)[0], 400)

    def test_try_run_and_download(self):
        status, _, content = self.request("POST", "/api/jobs/" + self.state["run_id"] + "/run", {"input": {"question": "出差餐补"}})
        self.assertEqual(status, 200)
        self.assertIn("80 元", json.loads(content)["answer"])
        status, headers, content = self.request("GET", "/api/jobs/" + self.state["run_id"] + "/download")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertTrue(content.startswith(b"PK"))

    def test_cross_site_host_and_simple_form_requests_rejected(self):
        for headers in ({"Origin": "https://example.com"}, {"Host": "evil.example"}, {"Content-Type": "text/plain"}):
            status, _, _ = self.request("POST", "/api/jobs", {"request": "blocked"}, headers)
            self.assertEqual(status, 400)

    def test_invalid_resources_and_paths(self):
        status, _, _ = self.request("POST", "/api/jobs", {"request": "create", "files": []})
        self.assertEqual(status, 400)
        status, _, _ = self.request("GET", "/api/jobs/../../private")
        self.assertEqual(status, 400)


class AdaptiveDashboardTests(DashboardTests):
    @classmethod
    def setUpClass(cls):
        from test_adaptive import AdaptiveFixturePlanner, DocClient
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        factory = FactoryAgent(cls.root, AdaptiveFixturePlanner(), runtime_client_factory=DocClient)
        cls.state = factory.drive(factory.create("文档问答并核对政策", uploads=DOCUMENTS))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        source = Path(__file__).resolve().parents[1]
        script = "import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd() / 'tests')); "
        script += "from test_adaptive import HumanDocClient; from agentforge.factory import FactoryAgent; import agentforge.server as server; "
        script += "from agentforge.codex_client import CodexClient; CodexClient.complete = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('Unexpected real Codex call in test')); "
        script += "server.FactoryAgent = lambda root, **kwargs: FactoryAgent(root, runtime_client_factory=HumanDocClient, **kwargs); "
        script += "server.serve(Path(sys.argv[1]), port=int(sys.argv[2]))"
        cls.process = subprocess.Popen([sys.executable, "-c", script, str(cls.root), str(cls.port)], cwd=source,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/api/jobs")
                response = conn.getresponse()
                response.read()
                conn.close()
                if response.status == 200:
                    return
            except OSError:
                time.sleep(0.05)
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.temp.cleanup()
        raise RuntimeError("Adaptive dashboard did not start")

    def test_try_run_and_download(self):
        path = "/api/jobs/" + self.state["run_id"]
        status, _, content = self.request("POST", path + "/run", {"input": {"question": "出差餐补"}})
        self.assertEqual(status, 202)
        record = json.loads(content)
        execution_id = record["execution_id"]
        for _ in range(300):
            status, _, content = self.request("GET", path + "/executions/" + execution_id)
            record = json.loads(content)
            if record["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(record["status"], "completed", record)
        self.assertEqual(record["result"]["model_calls"], 3)
        self.assertIn("80 元", record["result"]["answer"])
        self.assertTrue(any(e["type"] == "tool" for e in record["trace"]))
        _, _, content = self.request("GET", path)
        self.assertEqual(json.loads(content)["executions"][0]["execution_id"], execution_id)
        status, _, content = self.request("GET", path + "/download")
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(b"PK"))

    def test_human_continuation_api(self):
        path = "/api/jobs/" + self.state["run_id"]
        status, _, content = self.request("POST", path + "/run", {"input": {"question": "需要接续"}})
        self.assertEqual(status, 202)
        eid = json.loads(content)["execution_id"]
        def poll():
            for _ in range(200):
                _, _, content = self.request("GET", path + "/executions/" + eid)
                record = json.loads(content)
                if record["status"] not in ("running", "queued"):
                    return record
                time.sleep(0.05)
            self.fail("execution did not finish: " + str(record))
        first = poll()
        self.assertEqual(first["status"], "waiting_input", first)
        status, _, _ = self.request("POST", path + "/executions/" + eid + "/continue", {})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", path + "/executions/" + eid + "/continue", {"reply": {"text": "出差餐补"}})
        self.assertEqual(status, 202)
        final = poll()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"]["model_calls"], 4)


if __name__ == "__main__":
    unittest.main()
