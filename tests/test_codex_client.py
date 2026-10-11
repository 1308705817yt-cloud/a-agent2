"""验证 Codex CLI 参数、结构化响应、超时与错误处理，不消耗模型额度。"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentforge.codex_client import CodexClient, CodexError, object_schema, validate_json


class CodexClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.client = CodexClient(self.root, model="test-model", timeout=5)
        self.schema = object_schema({"connected": {"type": "boolean", "enum": [True]}})

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.temporary.cleanup()

    def test_prompt_is_stdin_and_cli_is_read_only_with_schema(self):
        captured = {}

        def execute(arguments, **kwargs):
            captured.update(arguments=arguments, **kwargs)
            path = Path(arguments[arguments.index("--output-last-message") + 1])
            path.write_text('{"connected":true}', encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 0, '{"type":"turn.completed","usage":{"input_tokens":10}}\n', '')

        with patch.object(self.client, "_execute", side_effect=execute):
            output = self.client.complete("检查", {"text": "$(never_execute) & echo fake"}, self.schema)
        self.assertTrue(output["connected"])
        self.assertIn("--ignore-user-config", captured["arguments"])
        self.assertIn("read-only", captured["arguments"])
        self.assertIn("--output-schema", captured["arguments"])
        self.assertEqual(captured["arguments"][-1], "-")
        self.assertNotIn("$(never_execute)", " ".join(captured["arguments"]))
        self.assertIn("$(never_execute)", captured["input_text"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", captured["arguments"])
        self.assertEqual(self.client.last_metadata["usage"]["input_tokens"], 10)

    def test_failure_cannot_use_partial_response_file(self):
        def execute(arguments, **kwargs):
            Path(arguments[arguments.index("--output-last-message") + 1]).write_text('{"connected":true}', encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 1, '', 'connection failed')

        with patch.object(self.client, "_execute", side_effect=execute), self.assertRaisesRegex(CodexError, "网络"):
            self.client.complete("检查", {}, self.schema)

    def test_missing_and_invalid_final_json_are_rejected(self):
        for content in (None, "not json", '{"connected":false}', '{"connected":true,"extra":"bad"}'):
            with self.subTest(content=content):
                def execute(arguments, **kwargs):
                    if content is not None:
                        Path(arguments[arguments.index("--output-last-message") + 1]).write_text(content, encoding="utf-8")
                    return subprocess.CompletedProcess(arguments, 0, '', '')
                with patch.object(self.client, "_execute", side_effect=execute), self.assertRaises(CodexError):
                    self.client.complete("检查", {}, self.schema)

    def test_timeout_is_reported_without_alternative_result(self):
        with patch("agentforge.codex_client.resolve_codex_binary", return_value="codex.exe"), \
             patch("agentforge.codex_client.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 5)), \
             self.assertRaisesRegex(CodexError, "超时"):
            self.client.complete("检查", {}, self.schema)

    def test_unsupported_model_and_login_errors_are_specific(self):
        for error, expected in (("model is not supported", "不支持"), ("401 Unauthorized", "认证")):
            with self.subTest(error=error), patch.object(self.client, "_execute", return_value=subprocess.CompletedProcess([], 1, '', error)), \
                 self.assertRaisesRegex(CodexError, expected):
                self.client.complete("检查", {}, self.schema)

    def test_schema_rejects_wrong_types_and_unknown_fields(self):
        for value in ({"connected": 1}, {"connected": True, "extra": 1}, {}):
            with self.assertRaises(CodexError):
                validate_json(value, self.schema)

    def test_login_status_failure_only_means_unauthenticated_with_explicit_evidence(self):
        for stderr, category in (("Not logged in", "authentication"), ("Error checking login status: permission denied", "unknown")):
            results = [subprocess.CompletedProcess([],0,"codex-cli test",""), subprocess.CompletedProcess([],1,"",stderr)]
            with self.subTest(category=category), patch.object(self.client,"_execute",side_effect=results), self.assertRaises(CodexError) as raised:
                self.client.check_installation()
            self.assertEqual(raised.exception.failure["category"],category)
            self.assertEqual(raised.exception.failure["exit_code"],1)


if __name__ == "__main__":
    unittest.main()
