"""Read-only relational queries with table/function authorization and bounded output."""

import json
import math
import re
import sqlite3
import time

from .contracts import ExecutionError
from .resources import fingerprint

FUNCTIONS = frozenset(("sum", "total", "avg", "count", "min", "max", "abs", "round", "coalesce", "ifnull", "nullif",
                       "length", "lower", "upper", "substr", "substring", "trim", "ltrim", "rtrim", "replace",
                       "date", "datetime", "strftime", "julianday", "unixepoch", "row_number", "rank", "dense_rank",
                       "lag", "lead", "first_value", "last_value", "ntile"))


def input_schema():
    props = {"action": {"type": "string", "enum": ["schema", "query"]},
             "sql": {"type": "string", "maxLength": 12000},
             "parameters_json": {"type": "string", "maxLength": 12000}}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def output_schema():
    scalar = {"anyOf": [{"type": t} for t in ("string", "number", "null")]}
    props = {"format": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
             "rows": {"type": "array", "items": {"type": "array", "items": scalar}},
             "row_count": {"type": "integer"}, "query_hash": {"type": "string"},
             "truncated": {"type": "boolean"}, "numeric_semantics": {"type": "string"}}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def inspect_schema(connection, tables, deadline, max_rows):
    rows = []
    for table in tables:
        if time.monotonic() >= deadline:
            raise ExecutionError("TOOL_TIMEOUT", "数据库结构读取达到时间上限。")
        columns = connection.execute("PRAGMA table_info(" + quote_identifier(table) + ")").fetchall()
        if not columns:
            raise ExecutionError("TOOL_RESOURCE", "配置中的授权表不存在：" + table)
        foreign = connection.execute("PRAGMA foreign_key_list(" + quote_identifier(table) + ")").fetchall()
        for _, name, kind, required, _, primary in columns:
            refs = [{"table": row[2], "column": row[4], "constraint": row[0], "position": row[1]}
                    for row in foreign if row[3] == name and row[2] in tables]
            rows.append([table, name, kind, required, primary, json.dumps(refs, ensure_ascii=False)])
            if len(rows) > max_rows:
                raise ExecutionError("TOOL_SIZE", "数据库结构超过行数预算；请缩小授权表范围。")
    return {"format": "agentforge-sql-v1", "columns": ["table", "column", "type", "not_null", "primary_key_order", "foreign_keys_json"],
            "rows": rows, "row_count": len(rows), "query_hash": fingerprint(rows), "truncated": False,
            "numeric_semantics": "schema metadata; not a computed business answer"}


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def parameters(arguments):
    if not isinstance(arguments, dict) or set(arguments) - {"sql", "parameters_json"}:
        raise ExecutionError("INVALID_ARGUMENTS", "SQL 工具仅接受 sql 和 parameters_json。")
    sql = arguments.get("sql")
    if not isinstance(sql, str) or not re.match(r"\s*(SELECT|WITH)\b", sql, re.I) or len(sql) > 12000:
        raise ExecutionError("INVALID_ARGUMENTS", "需要一条长度不超过 12000 的 SELECT/WITH 查询。")
    try:
        values = json.loads(arguments.get("parameters_json", "{}"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise ExecutionError("INVALID_ARGUMENTS", "parameters_json 必须是合法 JSON 对象。")
    if (not isinstance(values, dict) or len(values) > 50
            or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k)
                   or v is not None and type(v) not in (str, int, float, bool) for k, v in values.items())
            or any(isinstance(v, str) and len(v) > 6000 or type(v) is int and not -(2**63) <= v < 2**63
                   or type(v) is float and not math.isfinite(v) for v in values.values())):
        raise ExecutionError("INVALID_ARGUMENTS", "SQL 绑定值必须是有界的具名标量。")
    return sql, values


def execute_read(connection, arguments, tables, deadline, max_rows=1000):
    sql, values = parameters(arguments)
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 12000)
    connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
    connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
    connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 20)
    connection.execute("PRAGMA query_only=ON")
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    allowed_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}
    def authorize(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if database == "main" and first in tables else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if (second or "").lower() in FUNCTIONS else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in allowed_actions else sqlite3.SQLITE_DENY
    connection.set_authorizer(authorize)
    try:
        if time.monotonic() >= deadline:
            raise ExecutionError("TOOL_TIMEOUT", "SQL 查询达到时间上限。")
        cursor = connection.execute(sql, values)
        columns = [col[0] for col in cursor.description]
        if len(columns) > 100 or len(set(columns)) != len(columns):
            raise ExecutionError("INVALID_ARGUMENTS", "查询列名必须唯一且不超过 100 列；请使用别名。")
        rows, size = [], 0
        for record in cursor:
            if time.monotonic() >= deadline:
                raise ExecutionError("TOOL_TIMEOUT", "SQL 查询达到时间上限。")
            if len(rows) >= max_rows:
                raise ExecutionError("TOOL_SIZE", "SQL 结果超过行数上限；请显式聚合或限制结果。")
            if any(isinstance(v, bytes) or isinstance(v, float) and not math.isfinite(v) for v in record):
                raise ExecutionError("TOOL_OUTPUT_CONTRACT", "SQL 结果不支持二进制或非有限数值。")
            size += len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if size > 1024 * 1024:
                raise ExecutionError("TOOL_SIZE", "SQL 结果超过 1 MiB。")
            rows.append(list(record))
        return {"format": "agentforge-sql-v1", "columns": columns, "rows": rows, "row_count": len(rows),
                "query_hash": fingerprint({"sql": sql, "parameters": values}), "truncated": False,
                "numeric_semantics": "sqlite; decimal aggregation may use floating point; use csv.metrics for exact business decimals"}
    except sqlite3.Error as exc:
        if time.monotonic() >= deadline:
            raise ExecutionError("TOOL_TIMEOUT", "SQL 查询达到时间上限。") from exc
        # SQL came from the current decision; syntax/authorization errors are repairable decisions.
        message = str(exc)[:400]
        raise ExecutionError("SQL_QUERY", message, origin="decision", retryable=True) from exc
    finally:
        connection.set_authorizer(None)
        connection.set_progress_handler(None, 0)


def csv_sql_catalog(resources):
    return [{"table": "dataset_" + str(index + 1), "dataset_id": dataset["id"], "name": dataset["name"],
             "columns": dataset["columns"], "types": {f["name"]: f.get("type", "text") for f in dataset.get("data_contract", {}).get("columns", [])},
             "storage": "TEXT canonical cells; cast deliberately. Decimal metrics use csv.metrics."}
            for index, dataset in enumerate(resources["datasets"])]


def execute_csv_sql(arguments, resources, deadline=None):
    from .codex_client import DeadlineExceeded
    parameters(arguments)  # Reject malformed requests before loading data.
    tables = csv_sql_catalog(resources)
    end = min(deadline, time.monotonic() + 10) if deadline is not None else time.monotonic() + 10
    connection = sqlite3.connect(":memory:")
    try:
        for table, dataset in zip(tables, resources["datasets"]):
            if len({c.lower() for c in dataset["columns"]}) != len(dataset["columns"]):
                raise ExecutionError("INVALID_ARGUMENTS", "SQLite 列名不区分大小写；请先规范化重复列名。")
            columns = ", ".join(quote_identifier(c) + " TEXT" for c in dataset["columns"])
            name = quote_identifier(table["table"])
            connection.execute("CREATE TABLE " + name + " (" + columns + ")")
            statement = "INSERT INTO " + name + " VALUES (" + ",".join("?" for _ in dataset["columns"]) + ")"
            for offset in range(0, len(dataset["rows"]), 128):
                if time.monotonic() >= end:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise DeadlineExceeded("CSV 关系查询达到运行时间上限。", "runtime")
                    raise ExecutionError("TOOL_TIMEOUT", "CSV 关系查询装载达到 10 秒上限。")
                connection.executemany(statement, [tuple(row[c] for c in dataset["columns"]) for row in dataset["rows"][offset:offset + 128]])
        connection.commit()
        result = execute_read(connection, arguments, {t["table"] for t in tables}, end)
        result["resources"] = [{"dataset_id": d["id"], "resource_hash": fingerprint(d)} for d in resources["datasets"]]
        return result
    finally:
        connection.close()


def summary(result):
    if not result["rows"]:
        return "没有满足条件的数据。"
    # Keep the complete result in data; bound the human-readable rendering.
    rows = result["rows"][:20]
    text = "\n".join("，".join(str(c) + "=" + ("空值" if v is None else str(v)) for c, v in zip(result["columns"], row)) for row in rows)
    if len(result["rows"]) > len(rows):
        text += "\n其余结果见 data，共 " + str(result["row_count"]) + " 行。"
    return text if len(text) <= 8000 else text[:7950] + "\n完整计算结果见 data。"
