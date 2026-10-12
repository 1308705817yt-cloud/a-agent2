"""Operator-defined read-only capabilities; SQL generation requires explicit sqlite_query authorization."""

import copy
import json
import os
import re
import sqlite3
import time
import threading
import uuid
import socket
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .codex_client import CodexError, validate_json
from .contracts import ExecutionError
from .resources import fingerprint, read_json


MAX_RESPONSE = 1024 * 1024


def database_path(root, relative):
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("数据库路径必须使用 trusted_data/ 下的相对路径。")
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if not relative.startswith("trusted_data/") or not path.is_relative_to(base / "trusted_data") or ".." in Path(relative).parts:
        raise ValueError("数据库必须在工作区 trusted_data/ 内。")
    return path


def validate_definitions(definitions):
    from .adaptive_contracts import validate_schema_definition
    if not isinstance(definitions, list) or len(definitions) > 12:
        raise ValueError("最多配置 12 个可信工具。")
    ids = set()
    for tool in definitions:
        if not isinstance(tool, dict) or set(tool) != {"id", "description", "kind", "input_schema", "output_schema", "config"}:
            raise ValueError("可信工具定义字段无效。")
        tid = tool["id"]
        if not isinstance(tid, str) or not re.fullmatch(r"trusted\.[a-z][a-z0-9_]{0,39}", tid) or tid in ids:
            raise ValueError("可信工具 ID 无效或重复。")
        ids.add(tid)
        if not isinstance(tool["description"], str) or not tool["description"].strip():
            raise ValueError("可信工具需要用途说明。")
        for name in ("input_schema", "output_schema"):
            validate_schema_definition(tool[name])
        schema = tool["input_schema"]
        if schema.get("type") != "object" or any(s.get("type") not in ("string", "integer", "number", "boolean") for s in schema["properties"].values()):
            raise ValueError("可信工具参数必须是具名标量；生成 SQL 仅允许显式配置的 sqlite_query 工具。")
        config = tool["config"]
        if not isinstance(config, dict) or type(config.get("timeout")) is not int or not 1 <= config["timeout"] <= 10:
            raise ValueError("可信工具超时必须为 1 至 10 秒。")
        if tool["kind"] in ("sqlite_read", "sqlite_query"):
            expected = {"database", "query", "tables", "max_rows", "timeout"} if tool["kind"] == "sqlite_read" else {"database", "tables", "max_rows", "timeout"}
            if set(config) - {"schema_notes"} != expected or "schema_notes" in config and (not isinstance(config["schema_notes"], str) or len(config["schema_notes"]) > 6000):
                raise ValueError("SQLite 配置字段无效。")
            database_path(Path.cwd(), config["database"])
            if tool["kind"] == "sqlite_read" and (not isinstance(config["query"], str) or not re.match(r"\s*(SELECT|WITH)\b", config["query"], re.I) or len(config["query"]) > 6000):
                raise ValueError("SQLite 只接受运维配置的 SELECT/WITH 查询。")
            if tool["kind"] == "sqlite_query":
                from .sql_query import input_schema, output_schema
                if tool["input_schema"] != input_schema() or tool["output_schema"] != output_schema():
                    raise ValueError("sqlite_query 使用宿主定义的结构查询契约。")
            if not isinstance(config["tables"], list) or not config["tables"] or any(not isinstance(t, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t) for t in config["tables"]):
                raise ValueError("必须配置允许读取的表名。")
            if type(config["max_rows"]) is not int or not 1 <= config["max_rows"] <= 1000:
                raise ValueError("SQLite 返回上限必须为 1 至 1000 行。")
        elif tool["kind"] == "http_json":
            if set(config) != {"url", "header_env", "timeout"} or not isinstance(config["url"], str):
                raise ValueError("HTTP 配置字段无效。")
            url = urlsplit(config["url"])
            if not url.hostname or url.username or url.password or url.query or url.fragment or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1"))):
                raise ValueError("HTTP 需要固定 HTTPS 地址；本机服务可使用 HTTP；禁止内嵌密钥、查询串和片段。")
            headers = config["header_env"]
            if not isinstance(headers, dict) or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", k) or k.lower() in ("host", "cookie", "content-length") or not isinstance(v, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", v) for k, v in headers.items()):
                raise ValueError("HTTP 密钥只能引用环境变量；不能配置 Host/Cookie 等头。")
        else:
            raise ValueError("可信工具仅支持 sqlite_read、sqlite_query 和 http_json。")


def load_definitions(root):
    path = Path(root) / "trusted_tools.json"
    value = read_json(path) if path.exists() else {"tools": []}
    if not isinstance(value, dict) or set(value) != {"tools"}:
        raise ValueError("trusted_tools.json 必须包含 tools 列表。")
    if isinstance(value["tools"], list):
        from .sql_query import input_schema, output_schema
        for tool in value["tools"]:
            if isinstance(tool, dict) and tool.get("kind") == "sqlite_query":
                tool.setdefault("input_schema", input_schema())
                tool.setdefault("output_schema", output_schema())
    validate_definitions(value["tools"])
    for tool in value["tools"]:
        if tool["kind"] in ("sqlite_read", "sqlite_query") and not database_path(root, tool["config"]["database"]).is_file():
            raise ValueError("可信数据库文件不存在。")
    return copy.deepcopy(value["tools"])


def tool_catalog(definitions):
    return [{**{k: copy.deepcopy(t[k]) for k in ("id", "description", "kind", "input_schema", "output_schema")},
             **({"schema_notes": t["config"].get("schema_notes", ""), "tables": t["config"]["tables"]}
                if t["kind"] == "sqlite_query" else {})} for t in definitions]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ExecutionError("TOOL_REDIRECT", "可信 HTTP 工具禁止跳转到其他地址。")


class TrustedTools:
    def __init__(self, root, definitions):
        validate_definitions(definitions)
        self.root = Path(root).resolve()
        self.definitions = {t["id"]: copy.deepcopy(t) for t in definitions}
        self._local = threading.local()
        self.last_audit = None

    @property
    def deadline(self):
        return getattr(self._local, "deadline", None)

    @deadline.setter
    def deadline(self, value):
        self._local.deadline = value

    @property
    def last_audit(self):
        return getattr(self._local, "audit", None)

    @last_audit.setter
    def last_audit(self, value):
        self._local.audit = value

    @staticmethod
    def attribute_error(tool, exc):
        """Attribute failures at the trusted boundary, independently of shared error codes."""
        if exc.code in ("INVALID_ARGUMENTS", "UNKNOWN_TOOL", "SQL_QUERY") or tool["kind"] in ("sqlite_read", "sqlite_query") and exc.code == "TOOL_SIZE":
            return ExecutionError(exc.code, str(exc), tool["id"], origin="decision", retryable=True)
        configuration = exc.code in ("TOOL_AUTH", "TOOL_RESOURCE") or tool["kind"] in ("sqlite_read", "sqlite_query") and exc.code != "TOOL_TIMEOUT"
        origin = "configuration" if configuration else "external"
        field = "output_schema" if exc.code == "TOOL_OUTPUT_CONTRACT" else "config"
        return ExecutionError(exc.code, str(exc), "trusted_tools.json.tools." + tool["id"] + "." + field,
                              origin=origin, retryable=exc.code == "TOOL_TIMEOUT")

    def invoke(self, tid, arguments):
        self.last_audit = None
        tool = self.definitions.get(tid)
        if tool is None:
            raise ExecutionError("UNKNOWN_TOOL", "可信工具未授权。")
        audit = {"call_id": uuid.uuid4().hex, "tool": tid, "binding_hash": fingerprint(tool),
                 "arguments_hash": fingerprint(arguments), "kind": tool["kind"], "access": "read_only", "status": "running"}
        self.last_audit = audit
        started = time.perf_counter()
        def failed(exc):
            audit.update(status="failed", error_code=exc.code, error=exc.info())
            return exc
        try:
            try:
                validate_json(arguments, tool["input_schema"])
            except CodexError as exc:
                raise ExecutionError("INVALID_ARGUMENTS", "可信工具参数未通过契约校验。") from exc
            result = (self.sqlite(tool, arguments) if tool["kind"] == "sqlite_read" else
                      self.sqlite_query(tool, arguments) if tool["kind"] == "sqlite_query" else self.http(tool, arguments))
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > MAX_RESPONSE:
                raise ExecutionError("TOOL_SIZE", "可信工具返回数据超过 1 MiB。")
            validate_json(result, tool["output_schema"])
            audit.update(result_hash=fingerprint(result), status="completed")
            return result
        except CodexError as exc:
            error = self.attribute_error(tool, ExecutionError("TOOL_OUTPUT_CONTRACT", "可信工具返回值未通过契约校验。"))
            raise failed(error) from exc
        except ExecutionError as exc:
            raise failed(self.attribute_error(tool, exc)) from exc
        except HTTPError as exc:
            audit["http_status"] = exc.code
            code = "TOOL_AUTH" if exc.code in (401, 403) else "TRUSTED_TOOL_ERROR"
            origin = "configuration" if code == "TOOL_AUTH" else "external"
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            exc.close()
            raise failed(ExecutionError(code, "可信 HTTP 服务拒绝请求（HTTP " + str(exc.code) + "）。",
                         "trusted_tools.json.tools." + tid + ".config", origin=origin, retryable=retryable)) from exc
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            timed_out = isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout))
            code = "TOOL_TIMEOUT" if timed_out else "TRUSTED_TOOL_ERROR"
            # Network/driver messages can contain addresses, credentials or private SQL values.
            origin = "external" if timed_out or tool["kind"] == "http_json" else "configuration"
            retryable = timed_out or tool["kind"] == "http_json" and isinstance(exc, (URLError, OSError))
            raise failed(ExecutionError(code, "可信工具读取失败（" + type(exc).__name__ + "）；检查服务、权限或配置。",
                         "trusted_tools.json.tools." + tid + ".config", origin=origin, retryable=retryable)) from exc
        finally:
            audit["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)

    def sqlite(self, tool, arguments):
        config = tool["config"]
        path = database_path(self.root, config["database"])
        if not path.is_file():
            raise ExecutionError("TOOL_RESOURCE", "可信数据库不存在。")
        deadline = time.monotonic() + config["timeout"]
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=max(0, deadline - time.monotonic()))
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
            def authorize(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_READ:
                    return sqlite3.SQLITE_OK if database == "main" and arg1 in config["tables"] else sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ("load_extension", "readfile", "writefile"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
            connection.set_authorizer(authorize)
            cursor = connection.execute(config["query"], arguments)
            columns = [c[0] for c in cursor.description]
            rows = cursor.fetchmany(config["max_rows"] + 1)
            if len(rows) > config["max_rows"]:
                raise ExecutionError("TOOL_SIZE", "查询超过行数上限；请缩小查询条件。")
            if len(set(columns)) != len(columns):
                raise ExecutionError("TOOL_OUTPUT_CONTRACT", "查询列名重复；请配置唯一别名。")
            return {"rows": [dict(zip(columns, row)) for row in rows], "row_count": len(rows)}
        except sqlite3.OperationalError as exc:
            if time.monotonic() >= deadline and "interrupt" in str(exc).lower():
                raise ExecutionError("TOOL_TIMEOUT", "可信 SQLite 查询超过时间上限。") from exc
            raise
        finally:
            connection.close()

    def sqlite_query(self, tool, arguments):
        from .sql_query import execute_read, inspect_schema
        config = tool["config"]
        path = database_path(self.root, config["database"])
        if not path.is_file():
            raise ExecutionError("TOOL_RESOURCE", "可信数据库不存在。")
        end = time.monotonic() + config["timeout"]
        if self.deadline is not None:
            end = min(end, self.deadline)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=max(0, end - time.monotonic()))
        try:
            connection.execute("PRAGMA query_only=ON")
            if arguments["action"] == "schema":
                if arguments["sql"] or arguments["parameters_json"] != "{}":
                    raise ExecutionError("INVALID_ARGUMENTS", "读取结构时 sql 必须为空且 parameters_json 必须为 {}。")
                return inspect_schema(connection, config["tables"], end, config["max_rows"])
            return execute_read(connection, {k: arguments[k] for k in ("sql", "parameters_json")}, set(config["tables"]), end, config["max_rows"])
        finally:
            connection.close()

    def http(self, tool, arguments):
        config = tool["config"]
        headers = {"Accept": "application/json"}
        for name, env_name in config["header_env"].items():
            value = os.environ.get(env_name)
            if not value:
                raise ExecutionError("TOOL_AUTH", "可信 HTTP 工具所需认证环境变量未设置。")
            headers[name] = value
        query = urlencode(arguments)
        request = Request(config["url"] + ("?" + query if query else ""), headers=headers, method="GET")
        # Explicit bindings do not inherit proxy redirects or ambient cookies.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        timeout = min(config["timeout"], max(0.001, self.deadline - time.monotonic())) if self.deadline is not None else config["timeout"]
        with opener.open(request, timeout=timeout) as response:
            if response.headers.get_content_type() != "application/json":
                raise ExecutionError("TOOL_OUTPUT_CONTRACT", "HTTP 服务必须返回 application/json。")
            content = response.read(MAX_RESPONSE + 1)
            if len(content) > MAX_RESPONSE:
                raise ExecutionError("TOOL_SIZE", "HTTP 返回数据超过 1 MiB。")
        try:
            return json.loads(content.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限数值")))
        except (ValueError, UnicodeError) as exc:
            raise ExecutionError("TOOL_OUTPUT_CONTRACT", "HTTP 服务未返回有效的 UTF-8 JSON。") from exc
