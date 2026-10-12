"""Bounded metric plans executed from frozen definitions, with exact decimal output."""

import copy
import json
import re
import time
from decimal import Decimal, localcontext, ROUND_HALF_EVEN

from .data_contract import decimal_value, fail, typed_filter
from .csv_contract import validate_filters, query_schema

FORMAT = "agentforge-metrics-v1"
AGGREGATES = ("sum", "mean", "min", "max", "count", "count_distinct")


def validate_metrics(definition):
    fields = {f["name"]: f for f in definition["columns"]}
    metrics = definition.get("metrics", [])
    if not isinstance(metrics, list) or len(metrics) > 30:
        fail("最多定义 30 个业务指标。")
    by_id = {}
    for metric in metrics:
        if not isinstance(metric, dict) or set(metric) - {"id", "description", "operation", "column", "filters", "unit", "scale", "null_policy", "formula"}:
            fail("指标定义包含未知字段。")
        mid = metric.get("id")
        if not isinstance(mid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", mid) or mid in by_id:
            fail("指标 ID 无效或重复。")
        if not isinstance(metric.get("description"), str) or not 1 <= len(metric["description"]) <= 2000:
            fail("每个指标必须说明业务含义。")
        if not isinstance(metric.get("unit", ""), str) or len(metric.get("unit", "")) > 100:
            fail("指标单位无效。")
        if type(metric.get("scale", 6)) is not int or not 0 <= metric.get("scale", 6) <= 18:
            fail("指标 scale 应为 0 至 18。")
        if metric.get("null_policy", "skip") not in ("skip", "error"):
            fail("指标空值策略必须是 skip 或 error。")
        if "formula" in metric:
            formula = metric["formula"]
            if set(metric) & {"operation", "column", "filters", "null_policy"}:
                fail("派生指标不能同时定义基础聚合。")
            if (not isinstance(formula, dict) or set(formula) != {"operation", "left", "right"}
                    or formula["operation"] not in ("divide", "subtract")
                    or any(not isinstance(formula[k], str) for k in ("left", "right"))):
                fail("派生指标仅支持 divide/subtract 和两个指标 ID；除零固定返回 null。")
        else:
            op, column = metric.get("operation"), metric.get("column")
            if op not in AGGREGATES or (op != "count" and (not isinstance(column, str) or column not in fields)):
                fail("指标的聚合操作或列无效。")
            if op == "count" and column is not None:
                fail("count 为计行数，column 必须省略或为 null。")
            if op in ("sum", "mean", "min", "max") and fields[column].get("type") not in ("integer", "decimal"):
                fail("数值指标必须绑定 integer/decimal 类型列。")
            validate_filters(metric.get("filters", []), fields)
            # Validate predicates even when the dataset is empty.
            for item in metric.get("filters", []):
                typed_filter({k: None for k in fields}, item, fields)
        by_id[mid] = metric
    active, done = set(), set()
    def visit(mid):
        if mid not in by_id or mid in active:
            fail("指标依赖不存在或构成循环。")
        if mid in done:
            return
        active.add(mid)
        if "formula" in by_id[mid]:
            for side in ("left", "right"):
                visit(by_id[mid]["formula"][side])
        active.remove(mid)
        done.add(mid)
    for mid in by_id:
        visit(mid)
    return by_id


def metric_schema(dataset):
    contract = dataset.get("data_contract", {})
    ids = [m["id"] for m in contract.get("metrics", [])]
    if not ids:
        return {"type": "null"}
    props = {
        "dataset_id": {"type": "string", "enum": [dataset["id"]]},
        "metric_ids": {"type": "array", "items": {"type": "string", "enum": ids}, "minItems": 1, "maxItems": 12},
        "dimensions": {"type": "array", "items": {"type": "string", "enum": contract.get("dimensions", []) or [""]}, "maxItems": min(3, len(contract.get("dimensions", [])))},
        "filters": query_schema(dataset)["properties"]["filters"],
        "sort_by": {"anyOf": [{"type": "string", "enum": ids}, {"type": "null"}]},
        "descending": {"type": "boolean"},
        "limit": query_schema(dataset)["properties"]["limit"]}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def validate_query(arguments, resources, default_dataset=None):
    from .contracts import ExecutionError
    allowed = {"dataset_id", "metric_ids", "dimensions", "filters", "sort_by", "descending", "limit"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise ExecutionError("INVALID_ARGUMENTS", "业务指标查询字段无效。")
    query = {"dataset_id": default_dataset, "dimensions": [], "filters": [], "sort_by": None, "descending": False, "limit": None, **copy.deepcopy(arguments)}
    dataset = next((d for d in resources["datasets"] if d["id"] == query["dataset_id"]), None)
    if dataset is None or not dataset.get("data_contract", {}).get("metrics"):
        raise ExecutionError("UNKNOWN_RESOURCE", "授权数据集没有冻结的业务指标契约。")
    contract = dataset["data_contract"]
    ids = {m["id"] for m in contract["metrics"]}
    requested = query.get("metric_ids")
    dimensions = query["dimensions"]
    if (not isinstance(requested, list) or not 1 <= len(requested) <= 12
            or any(not isinstance(v, str) or v not in ids for v in requested) or len(set(requested)) != len(requested)):
        raise ExecutionError("INVALID_ARGUMENTS", "请选择 1 至 12 个不同的已定义指标。")
    if (not isinstance(dimensions, list) or len(dimensions) > 3
            or any(not isinstance(v, str) or v not in contract.get("dimensions", []) for v in dimensions)
            or len(set(dimensions)) != len(dimensions)):
        raise ExecutionError("INVALID_ARGUMENTS", "只能按最多三个已声明维度分组。")
    if query["sort_by"] is not None and query["sort_by"] not in requested:
        raise ExecutionError("INVALID_ARGUMENTS", "排序指标必须包含在本次指标中。")
    if type(query["descending"]) is not bool or query["limit"] is not None and (type(query["limit"]) is not int or not 1 <= query["limit"] <= 1000):
        raise ExecutionError("INVALID_ARGUMENTS", "排序方向或 limit 无效。")
    validate_filters(query["filters"], dataset["columns"])
    fields = {f["name"]: f for f in contract["columns"]}
    for item in query["filters"]:
        try:
            typed_filter({k: None for k in fields}, item, fields)
        except ExecutionError as exc:
            raise ExecutionError("INVALID_ARGUMENTS", str(exc), origin="decision", retryable=True) from exc
    return query, dataset


def execute_metrics(arguments, requirements, resources, deadline=None):
    from .codex_client import DeadlineExceeded
    from .contracts import ExecutionError
    from .resources import fingerprint
    query, dataset = validate_query(arguments, resources, requirements.defaults.get("dataset_id"))
    contract = dataset["data_contract"]
    definitions = validate_metrics(contract)
    fields = {f["name"]: f for f in contract["columns"]}
    def check():
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExceeded("业务指标执行达到运行时间上限。", "runtime")
    groups = {} if query["dimensions"] else {(): []}
    for index, row in enumerate(dataset["rows"]):
        if index % 128 == 0:
            check()
        if all(typed_filter(row, item, fields) for item in query["filters"]):
            key = tuple(row[d] for d in query["dimensions"])
            groups.setdefault(key, []).append(row)
            if len(groups) > 1000:
                raise ExecutionError("RESULT_LIMIT", "指标分组超过 1000；请缩小筛选范围。")
    output, sort_values = [], {}
    with localcontext() as context:
        context.prec = 200
        for key in sorted(groups, key=lambda k: tuple((v is not None, v or "") for v in k)):
            check()
            rows, cache, evidence = groups[key], {}, {}
            def compute(mid):
                if mid in cache:
                    return cache[mid]
                metric = definitions[mid]
                if "formula" in metric:
                    formula = metric["formula"]
                    left, right = compute(formula["left"]), compute(formula["right"])
                    if left is None or right is None:
                        value = None
                    elif formula["operation"] == "subtract":
                        value = left - right
                    else:
                        value = left / right if right else None
                    evidence[mid] = {"formula": formula, "zero_division": "null"}
                else:
                    selected = []
                    for index, row in enumerate(rows):
                        if index % 128 == 0:
                            check()
                        if all(typed_filter(row, item, fields) for item in metric.get("filters", [])):
                            selected.append(row)
                    op, column = metric["operation"], metric.get("column")
                    cells = [row[column] for row in selected] if column else []
                    nonnull = [v for v in cells if v is not None]
                    if column and len(cells) != len(nonnull) and metric.get("null_policy", "skip") == "error":
                        fail("指标不允许空值：" + mid)
                    if op == "count":
                        value = Decimal(len(selected))
                    elif op == "count_distinct":
                        values = [decimal_value(v) for v in nonnull] if fields[column].get("type") in ("integer", "decimal") else nonnull
                        value = Decimal(len(set(values)))
                    else:
                        values = [decimal_value(v) for v in nonnull]
                        total = sum(values, Decimal(0))
                        if op == "sum":
                            value = total
                        elif not values:
                            value = None
                        elif op == "mean":
                            value = total / len(values)
                        else:
                            value = min(values) if op == "min" else max(values)
                    evidence[mid] = {"filtered_rows": len(selected), "non_null_values": len(nonnull) if column else len(selected),
                                     "mandatory_filters": metric.get("filters", []), "null_policy": metric.get("null_policy", "skip")}
                if value is not None and (not value.is_finite() or value and abs(value.adjusted()) > 100):
                    raise ExecutionError("RESULT_LIMIT", "指标计算超出十进制结果范围。")
                cache[mid] = value
                return value
            values = {}
            for mid in query["metric_ids"]:
                value = compute(mid)
                scale = definitions[mid].get("scale", 0 if definitions[mid].get("operation") in ("count", "count_distinct") else 6)
                values[mid] = None if value is None else format(value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_EVEN), "f")
            entry = {"dimensions": dict(zip(query["dimensions"], key)), "values": values, "evidence": evidence}
            output.append(entry)
            sort_values[id(entry)] = cache.get(query["sort_by"])
    if query["sort_by"] is not None:
        output.sort(key=lambda row: (sort_values[id(row)] is not None, sort_values[id(row)] or Decimal(0)), reverse=query["descending"])
    total_groups = len(output)
    if query["limit"] is not None:
        output = output[:query["limit"]]
    check()
    result = {"format": FORMAT, "dataset_id": dataset["id"], "resource_hash": fingerprint(dataset),
            "contract_hash": fingerprint(contract), "query": query, "metrics": [definitions[m] for m in query["metric_ids"]],
            "numeric_encoding": "decimal_string", "rounding": "ROUND_HALF_EVEN", "total_groups": total_groups,
            "rows": output, "row_count": len(output), "truncated": len(output) < total_groups}
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 1024 * 1024:
        raise ExecutionError("RESULT_LIMIT", "业务指标结果超过 1 MiB；请缩小筛选或分组范围。")
    return result


def summary(result):
    if not result["rows"]:
        return "没有满足条件的数据。"
    units = {m["id"]: m.get("unit", "") for m in result["metrics"]}
    text = "；".join((str(row["dimensions"]) + "：" if row["dimensions"] else "") +
                     "，".join(mid + "=" + (value if value is not None else "无值") + units[mid]
                               for mid, value in row["values"].items()) for row in result["rows"][:20])
    if len(result["rows"]) > 20:
        text += "\n其余结果见 data，共 " + str(result["row_count"]) + " 行。"
    return text if len(text) <= 8000 else text[:7950] + "\n完整计算结果见 data。"
