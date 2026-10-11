"""Frozen requirement-derived cases and an independent Decimal CSV oracle."""

import json
import math
import re
from decimal import Decimal, InvalidOperation

from .contracts import ExecutionError, OPERATIONS
from .resources import fingerprint
from .workflow import validate_response


def validate_suite(suite, requirements, resources):
    cases = suite.get("cases")
    if not isinstance(cases, list) or not 8 <= len(cases) <= 40:
        raise ValueError("Codex 应生成 8 至 40 条验收场景。")
    ids = set()
    docs = {d["id"]: d for d in resources["documents"]}
    for case in cases:
        if (not isinstance(case.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", case["id"])
                or case["id"].startswith("integrity_") or case["id"] in ids or case["partition"] not in ("development", "acceptance")):
            raise ValueError("测试 ID 重复或测试分区无效。")
        ids.add(case["id"])
        if not isinstance(case["input"], dict) or not case["purpose"] or not case["criterion"]:
            raise ValueError("每个测试必须包含输入、目的和需求依据。")
        if requirements.task_type == "document_qa":
            if case["expect"] not in ("answer", "refusal", "error"):
                raise ValueError("文档测试的期望类型无效。")
            if case["expect"] == "answer":
                doc = docs.get(case["source_id"])
                if not doc or not case["facts"] or any(not fact or fact not in doc["content"] for fact in case["facts"]):
                    raise ValueError("文档测试的事实必须可在指定来源中核对。")
        elif case["expect"] != "csv":
            raise ValueError("CSV 测试的正确答案由独立计算器生成。")
    for partition in ("development", "acceptance"):
        group = [c for c in cases if c["partition"] == partition]
        if len(group) < 4 or len({json.dumps(c["input"], sort_keys=True) for c in group}) < 4:
            raise ValueError("每个分区至少需要 4 个不同输入。")
        if requirements.task_type == "document_qa" and not {"answer", "refusal"} <= {c["expect"] for c in group}:
            raise ValueError("文档测试每个分区都要覆盖可回答与拒答。")
    return suite


def add_integrity_cases(suite, requirements, resources):
    """Resource-derived controls supplement, rather than replace, generated tests."""
    result = {"cases": list(suite["cases"])}
    for part in ("development", "acceptance"):
        controls = [({}, "error"), ({"question": "zxqv_987654_unavailable"}, "refusal")] if requirements.task_type == "document_qa" else [
            ({}, "csv"), ({"operation": "unsupported"}, "csv"),
            ({"column": "__missing_column__", "operation": "sum"}, "csv"),
            ({"filters": [{"column": next(d for d in resources["datasets"] if d["id"] == requirements.defaults["dataset_id"])["columns"][0], "operator": "eq", "value": "zxqv_987654_unavailable"}]}, "csv"),
            ({"limit": 1}, "csv"), ({"operation": "count", "column": None, "group_by": None, "sort_by": "count"}, "csv")]
        for i, (inputs, expected) in enumerate(controls):
            result["cases"].append({"id": f"integrity_{part}_{i}", "partition": part,
                                    "purpose": "系统契约与边界检查", "criterion": "输入校验与确定性结果",
                                    "input": inputs, "expect": expected, "source_id": None, "facts": []})
    return result


class OracleError(Exception):
    def __init__(self, code):
        self.code = code


def decimal_value(value):
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not math.isfinite(float(number)):
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError):
        raise OracleError("NON_NUMERIC_COLUMN")


def csv_oracle(requirements, resources, inputs):
    try:
        return _csv_oracle(requirements, resources, inputs)
    except (TypeError, KeyError, IndexError, OverflowError):
        raise OracleError("INVALID_INPUT")


def _csv_oracle(requirements, resources, inputs):
    """Does not import production CSV tools or aggregation functions."""
    allowed = {"operation", "column", "group_by", "filters", "sort_by", "descending", "limit"}
    if not isinstance(inputs, dict) or set(inputs) - allowed:
        raise OracleError("INVALID_INPUT")
    params = {**requirements.defaults, **inputs}
    if (not isinstance(params.get("operation"), str)
            or any(v is not None and not isinstance(v, str) for v in (params.get("column"), params.get("group_by")))
            or not isinstance(params.get("sort_by", "group"), str)):
        raise OracleError("INVALID_INPUT")
    dataset = next(d for d in resources["datasets"] if d["id"] == requirements.defaults["dataset_id"])
    op, column, group = params["operation"], params.get("column"), params.get("group_by")
    filters = params.get("filters", [])
    if not isinstance(filters, list) or len(filters) > 20:
        raise OracleError("INVALID_INPUT")
    for f in filters:
        if not isinstance(f, dict) or set(f) != {"column", "operator", "value"}:
            raise OracleError("INVALID_INPUT")
        if not isinstance(f["column"], str) or not isinstance(f["operator"], str):
            raise OracleError("INVALID_INPUT")
        if f["column"] not in dataset["columns"]:
            raise OracleError("UNKNOWN_COLUMN")
        if f["operator"] not in ("eq", "ne", "gt", "gte", "lt", "lte", "contains"):
            raise OracleError("INVALID_INPUT")
    def keep(row):
        for f in filters:
            left, right, sign = row[f["column"]], f["value"], f["operator"]
            if sign in ("eq", "ne", "contains"):
                passes = str(left) == str(right) if sign != "contains" else str(right) in str(left)
                if sign == "ne":
                    passes = not passes
            else:
                a, b = decimal_value(left), decimal_value(right)
                passes = False if a is None or b is None else {
                    "gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[sign]
            if not passes:
                return False
        return True
    rows = [row for row in dataset["rows"] if keep(row)]
    if op not in OPERATIONS:
        raise OracleError("INVALID_INPUT")
    if op != "count" and not column or any(c and c not in dataset["columns"] for c in (column, group)):
        raise OracleError("UNKNOWN_COLUMN")
    buckets = {} if group else {None: []}
    for row in rows:
        buckets.setdefault(row[group] if group else None, []).append(row)
    if len(buckets) > 1000:
        raise OracleError("RESULT_LIMIT")
    results = []
    for key in sorted(buckets, key=str):
        records = buckets[key]
        values = [decimal_value(row[column]) for row in records] if column and op != "count" else []
        values = [v for v in values if v is not None]
        if op == "count":
            value = len(records)
        elif op == "sum":
            value = float(sum(values, Decimal(0)))
        elif op == "mean":
            value = float(sum(values, Decimal(0)) / len(values)) if values else None
        else:
            value = float((min if op == "min" else max)(values)) if values else None
        results.append({"group": key, "value": value, "count": len(records)})
    by, descending, limit = params.get("sort_by", "group"), params.get("descending", False), params.get("limit")
    if by not in ("group", "value", "count") or type(descending) is not bool:
        raise OracleError("INVALID_INPUT")
    results.sort(key=lambda r: (r[by] is not None, str(r[by]) if by == "group" else r[by] or 0), reverse=descending)
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise OracleError("INVALID_INPUT")
        results = results[:limit]
    return {"dataset_id": dataset["id"], "operation": op, "column": column, "group_by": group, "results": results}


def equivalent(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(equivalent(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(equivalent(a, b) for a, b in zip(actual, expected))
    if type(expected) in (int, float):
        return type(actual) in (int, float) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-9)
    return actual == expected


def evaluate(agent, requirements, resources, suite, partition, artifact_hash, outputs=None):
    results = []
    for case in suite["cases"]:
        if case["partition"] != partition:
            continue
        output = outputs[case["id"]] if outputs is not None else agent.run(case["input"])
        issues, expected = [], None
        try:
            validate_response({k: output[k] for k in ("answer", "data", "sources", "abstained", "error")}, resources)
        except (ExecutionError, KeyError) as exc:
            issues.append("响应契约：" + str(exc))
        if requirements.task_type == "csv_analysis":
            try:
                expected = csv_oracle(requirements, resources, case["input"])
                if output.get("error") or output.get("abstained") or not equivalent(output.get("data"), expected):
                    issues.append("CSV 结果与独立 Decimal 计算不一致。")
            except OracleError as exc:
                expected = {"error_code": exc.code}
                if (output.get("error") or {}).get("code") != exc.code:
                    issues.append("输入错误没有返回正确错误码：" + exc.code)
        elif case["expect"] == "answer":
            expected = {"source_id": case["source_id"], "facts": case["facts"]}
            if output.get("error") or output.get("abstained"):
                issues.append("有证据的问题被拒答。")
            if not any(s.get("id") == case["source_id"] for s in output.get("sources", [])):
                issues.append("缺少预期来源引用。")
            if any(fact not in output.get("answer", "") for fact in case["facts"]):
                issues.append("回答缺少需求要求的事实。")
        elif case["expect"] == "refusal":
            if not output.get("abstained") or output.get("error") or output.get("sources"):
                issues.append("证据不足时应拒答并且不编造来源。")
        elif not output.get("error"):
            issues.append("非法输入必须返回结构化错误。")
        results.append({**case, "passed": not issues, "issues": issues, "expected": expected, "actual": output})
    passed = sum(r["passed"] for r in results)
    return {"partition": partition, "artifact_hash": artifact_hash, "suite_hash": fingerprint(suite),
            "passed": passed, "total": len(results), "all_passed": bool(results) and passed == len(results), "cases": results}
