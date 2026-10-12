"""Versioned CSV semantics shared by planning, host validation and execution.

The independent numeric oracle deliberately keeps its own implementation.
"""

import copy
import math


CONTRACT_VERSION = "csv-query-v1"
OPERATIONS = ("sum", "mean", "count", "min", "max")
QUERY_KEYS = ("dataset_id", "operation", "column", "group_by", "filters", "sort_by", "descending", "limit")
INPUT_KEYS = QUERY_KEYS[1:]
FILTER_OPERATORS = ("eq", "ne", "contains", "gt", "gte", "lt", "lte")
SORT_KEYS = ("group", "value", "count")
MAX_FILTERS = 20
MAX_RESULTS = 1000
FALLBACKS = {"column": None, "group_by": None, "filters": [], "sort_by": "group", "descending": False, "limit": None}


def contract_catalog():
    return {"version": CONTRACT_VERSION, "query_fields": list(QUERY_KEYS),
            "operations": list(OPERATIONS), "filter_operators": list(FILTER_OPERATORS),
            "max_filters": MAX_FILTERS, "sort_keys": list(SORT_KEYS),
            "limit": {"nullable": True, "minimum": 1, "maximum": MAX_RESULTS, "boolean_allowed": False},
            "count": {"semantics": "all filtered rows, including blank numeric cells", "column": None},
            "numeric_blanks": [None, ""], "empty_aggregation": {"sum": 0, "mean": None, "min": None, "max": None},
            "result_row_count": "all rows in each filtered group, not just nonblank numeric cells",
            "sorting": {"null_values": "first ascending, last descending", "ties": "stable group-string order"},
            "filter_comparison": {"eq_ne_contains": "string comparison", "ordered": "finite numeric comparison; blank does not match"},
            "parameter_precedence": ["human_reply", "explicit_user", "natural_goal", "default"],
            "explicit_empty_values": "[], false and null remain explicit user values",
            "errors": {"unknown_column": "UNKNOWN_COLUMN", "invalid_input": "INVALID_INPUT"}}


def contract_instructions():
    return ("Host CSV contract " + CONTRACT_VERSION + ": limit is null or an integer 1.."
            + str(MAX_RESULTS) + "; limit=0 and booleans are INVALID_INPUT. "
            "count counts all filtered rows, including blank numeric cells, and returns column=null. "
            "Numeric aggregates omit null/empty cells; empty sum=0, empty mean/min/max=null. "
            "Explicit user/human parameters take precedence over goal inference and defaults, including [], false and null.")


def canonical_parameters(parameters):
    result = copy.deepcopy(parameters)
    if result.get("operation") == "count":
        result["column"] = None
    return result


def validate_filters(filters, columns):
    from .contracts import ExecutionError
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise ExecutionError("INVALID_INPUT", "筛选条件必须是列表，最多 " + str(MAX_FILTERS) + " 项。")
    for item in filters:
        if not isinstance(item, dict) or set(item) != {"column", "operator", "value"}:
            raise ExecutionError("INVALID_INPUT", "筛选条件需要 column、operator 和 value。")
        if not isinstance(item["column"], str) or not isinstance(item["operator"], str):
            raise ExecutionError("INVALID_INPUT", "筛选列和运算符必须是文本。")
        if item["column"] not in columns:
            raise ExecutionError("UNKNOWN_COLUMN", "筛选列不存在。")
        if item["operator"] not in FILTER_OPERATORS:
            raise ExecutionError("INVALID_INPUT", "筛选运算符不受支持。")


def validate_parameters(parameters, resources, defaults=None, *, allow_dataset=False, argument_error="INVALID_INPUT"):
    from .contracts import ExecutionError
    allowed = QUERY_KEYS if allow_dataset else INPUT_KEYS
    if not isinstance(parameters, dict) or set(parameters) - set(allowed):
        raise ExecutionError(argument_error, "CSV 输入字段不受支持。")
    params = {**copy.deepcopy(FALLBACKS), **copy.deepcopy(defaults or {}), **copy.deepcopy(parameters)}
    dataset = next((d for d in resources["datasets"] if d["id"] == params.get("dataset_id")), None)
    if dataset is None:
        raise ExecutionError("UNKNOWN_RESOURCE", "数据集未授权或不存在。")
    if not isinstance(params.get("operation"), str) or params["operation"] not in OPERATIONS:
        raise ExecutionError("INVALID_INPUT", "聚合类型不支持。")
    for field in ("column", "group_by"):
        value = params[field]
        if value is not None and not isinstance(value, str):
            raise ExecutionError("INVALID_INPUT", "列名必须是文本或 null。")
        if value and value not in dataset["columns"] or field == "column" and params["operation"] != "count" and not value:
            raise ExecutionError("UNKNOWN_COLUMN", "所选分析列不存在。")
    validate_filters(params["filters"], dataset["columns"])
    limit = params["limit"]
    if (params["sort_by"] not in SORT_KEYS or type(params["descending"]) is not bool
            or limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_RESULTS)):
        raise ExecutionError("INVALID_INPUT", "排序或数量参数无效。")
    return canonical_parameters(params)


def query_schema(dataset=None, *, complete=True):
    """Use the same field schemas for partial tool calls and complete query plans."""
    column = {"type": "string"}
    dataset_id = {"type": "string"}
    if dataset is not None:
        column["enum"] = dataset["columns"]
        dataset_id["enum"] = [dataset["id"]]
    nullable_column = {"anyOf": [copy.deepcopy(column), {"type": "null"}]}
    scalar = {"anyOf": [{"type": kind} for kind in ("string", "number", "boolean", "null")]}
    filters = {"type": "object", "properties": {
        "column": copy.deepcopy(column),
        "operator": {"type": "string", "enum": list(FILTER_OPERATORS)}, "value": scalar},
        "required": ["column", "operator", "value"], "additionalProperties": False}
    properties = {"dataset_id": dataset_id,
                  "operation": {"type": "string", "enum": list(OPERATIONS)},
                  "column": copy.deepcopy(nullable_column), "group_by": copy.deepcopy(nullable_column),
                  "filters": {"type": "array", "items": filters, "maxItems": MAX_FILTERS},
                  "sort_by": {"type": "string", "enum": list(SORT_KEYS)}, "descending": {"type": "boolean"},
                  "limit": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}, {"type": "null"}]}}
    return {"type": "object", "properties": properties, "required": list(QUERY_KEYS) if complete else [], "additionalProperties": False}


def result_schema():
    nullable_text = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    row = {"type": "object", "properties": {
        "group": copy.deepcopy(nullable_text), "value": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "count": {"type": "integer", "minimum": 0}},
        "required": ["group", "value", "count"], "additionalProperties": False}
    properties = {"dataset_id": {"type": "string"}, "operation": {"type": "string", "enum": list(OPERATIONS)},
                  "column": copy.deepcopy(nullable_text), "group_by": copy.deepcopy(nullable_text),
                  "results": {"type": "array", "items": row, "maxItems": MAX_RESULTS}}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def validate_result(data, resources):
    """Validate native CSV results without coupling DAG exports to a model client."""
    from .contracts import ExecutionError
    def invalid(message):
        raise ExecutionError("OUTPUT_CONTRACT", message)
    if not isinstance(data, dict) or set(data) != set(result_schema()["properties"]):
        invalid("CSV 结果字段与宿主契约不一致。")
    if not isinstance(data["dataset_id"], str) or data["operation"] not in OPERATIONS:
        invalid("CSV 结果数据集或操作无效。")
    dataset = next((d for d in resources["datasets"] if d["id"] == data["dataset_id"]), None)
    if dataset is None:
        raise ExecutionError("PROVENANCE", "CSV 结果必须绑定授权数据集。")
    for field in ("column", "group_by"):
        value = data[field]
        if value is not None and (not isinstance(value, str) or value and value not in dataset["columns"]):
            invalid("CSV 结果列元数据无效。")
    if data["operation"] == "count" and data["column"] is not None:
        invalid("count 按筛选后的完整行计数，结果 column 必须为 null。")
    if data["operation"] != "count" and not data["column"]:
        invalid("数值聚合结果缺少数值列。")
    rows = data["results"]
    if not isinstance(rows, list) or len(rows) > MAX_RESULTS:
        invalid("CSV 结果数量超出契约。")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"group", "value", "count"}:
            invalid("CSV 结果行字段无效。")
        if row["group"] is not None and not isinstance(row["group"], str):
            invalid("CSV 分组值必须为文本或 null。")
        if type(row["count"]) is not int or row["count"] < 0:
            invalid("CSV 行数必须是非负整数。")
        value = row["value"]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            invalid("CSV 结果值必须是有限数值或 null。")
        if data["operation"] == "count" and (type(value) is not int or value != row["count"]):
            invalid("count 的结果值必须等于完整行数。")
    return data
