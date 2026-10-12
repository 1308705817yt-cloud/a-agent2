"""Executable, operator-owned data types and business metric definitions."""

import copy
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "agentforge-data-contract-v1"
TYPES = ("text", "integer", "decimal", "date", "datetime", "boolean")


def fail(message, **details):
    from .contracts import ExecutionError
    raise ExecutionError("DATA_CONTRACT", message, origin="input", retryable=False, details=details or None)


def decimal_value(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        fail("数值必须是有限的十进制数。")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        fail("数值格式无效。")
    if not result.is_finite() or len(result.as_tuple().digits) > 38 or abs(result.as_tuple().exponent) > 38 or result.adjusted() >= 38:
        fail("十进制数最多 38 位，指数绝对值最多 38。")
    return result


def normalize_cell(value, field):
    """JSON-safe canonical cells. Exact numbers remain decimal strings."""
    if value is None or value in field.get("missing_values", [""]):
        if not field.get("nullable", True):
            fail("必填字段包含空值。")
        return None
    kind = field.get("type", "text")
    if not isinstance(value, (str, int, float, bool)):
        fail("单元格必须是标量。")
    if kind == "text":
        return str(value)
    if kind in ("integer", "decimal"):
        number = decimal_value(value)
        if kind == "integer" and number != number.to_integral_value():
            fail("整数列包含小数。")
        scale = 0 if kind == "integer" else field.get("scale")
        if scale is not None:
            with localcontext() as ctx:
                ctx.prec = 100
                rounded = number.quantize(Decimal(1).scaleb(-scale))
            if rounded != number:
                fail("输入精度超过字段 scale；接入层不会自动舍入。")
            if len(rounded.as_tuple().digits) > 38:
                fail("按字段 scale 表示的十进制数超过 38 位。")
            number = rounded
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                number = number.normalize()
        return format(number.copy_abs() if number.is_zero() else number, "f")
    if kind == "boolean":
        text = str(value).lower()
        if text not in ("true", "false", "1", "0"):
            fail("布尔列仅接受 true/false/1/0。")
        return "true" if text in ("true", "1") else "false"
    try:
        if kind == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
                fail("日期必须显式采用 YYYY-MM-DD。")
            return date.fromisoformat(str(value)).isoformat()
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            zone = field.get("timezone")
            if not zone:
                fail("无偏移量的时间必须声明字段 timezone。")
            tz = timezone.utc if zone == "UTC" else ZoneInfo(zone)
            first, second = parsed.replace(tzinfo=tz, fold=0), parsed.replace(tzinfo=tz, fold=1)
            if first.utcoffset() != second.utcoffset() or first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != parsed:
                fail("夏令时歧义或不存在的当地时间必须提供明确偏移量。")
            parsed = first
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, ZoneInfoNotFoundError) as exc:
        # ExecutionError is a ValueError too; preserve its actionable contract message.
        from .contracts import ExecutionError
        if isinstance(exc, ExecutionError):
            raise
        fail("日期、时间或时区无效；IANA 时区需要系统时区数据。")


def validate_definition(definition, source_columns):
    if not isinstance(definition, dict) or set(definition) - {"columns", "primary_key", "dimensions", "metrics", "description", "csv"}:
        fail("数据集契约字段无效。")
    options = definition.get("csv", {})
    if (not isinstance(options, dict) or set(options) - {"encoding", "delimiter"}
            or options.get("encoding", "utf-8-sig") not in ("utf-8", "utf-8-sig", "gb18030", "utf-16", "latin-1")
            or not isinstance(options.get("delimiter", ","), str) or len(options.get("delimiter", ",")) != 1
            or options.get("delimiter", ",") in ("\r", "\n", '"', "\x00")):
        fail("CSV 接入配置需要支持的 encoding 和单字符 delimiter。")
    fields = definition.get("columns")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
        fail("契约需要 1 至 100 个显式字段定义。")
    names, sources, aliases = set(), set(), set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {"name", "source", "type", "nullable", "missing_values", "scale", "timezone", "unit", "description", "aliases"}:
            fail("字段定义包含未知配置。")
        name, source = field.get("name"), field.get("source", field.get("name"))
        if not isinstance(name, str) or not name or len(name) > 100 or name in names or source not in source_columns or source in sources:
            fail("字段名或源列无效、重复或不存在。")
        names.add(name)
        sources.add(source)
        if field.get("type", "text") not in TYPES or type(field.get("nullable", True)) is not bool:
            fail("字段类型或 nullable 无效。")
        if "scale" in field and (field.get("type") != "decimal" or type(field["scale"]) is not int or not 0 <= field["scale"] <= 18):
            fail("仅 decimal 字段可声明 0 至 18 的 scale。")
        if "timezone" in field and (field.get("type") != "datetime" or not isinstance(field["timezone"], str)):
            fail("仅 datetime 字段可声明 timezone。")
        missing = field.get("missing_values", [""])
        if not isinstance(missing, list) or len(missing) > 20 or any(not isinstance(v, str) for v in missing):
            fail("missing_values 必须是最多 20 个文本标记。")
        labels = field.get("aliases", [])
        if (not isinstance(labels, list) or len(labels) > 20
                or any(not isinstance(v, str) or not 1 <= len(v) <= 100 or v in aliases for v in labels)
                or len(set(labels)) != len(labels)):
            fail("字段别名必须非空且不能跨列重复。")
        aliases.update(labels)
        for key in ("unit", "description"):
            if key in field and (not isinstance(field[key], str) or len(field[key]) > 2000):
                fail("字段说明必须是有界文本。")
    if sources != set(source_columns):
        fail("契约必须覆盖所有源列；不得静默丢弃未声明字段。")
    for field in fields:
        if any(alias in names and alias != field["name"] for alias in field.get("aliases", [])):
            fail("字段别名不能与其他规范列名冲突。")
    for key in ("primary_key", "dimensions"):
        values = definition.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or v not in names for v in values) or len(values) != len(set(values)):
            fail(key + " 必须引用不同的规范字段。")
    if len(definition.get("dimensions", [])) > 12:
        fail("最多声明 12 个可分组维度。")
    from .metric_query import validate_metrics
    validate_metrics(definition)
    return copy.deepcopy(definition)


def apply_dataset_contract(dataset, definition):
    from .resources import fingerprint
    definition = validate_definition(definition, dataset["columns"])
    rows, changed, seen = [], 0, set()
    for index, raw in enumerate(dataset["rows"], 2):
        row = {}
        for field in definition["columns"]:
            name, source = field["name"], field.get("source", field["name"])
            try:
                row[name] = normalize_cell(raw[source], field)
            except ValueError as exc:
                fail(str(exc), row=index, column=name, source_column=source)
            changed += row[name] != raw[source]
        keys = definition.get("primary_key", [])
        if keys:
            key = tuple(row[k] for k in keys)
            if None in key or key in seen:
                fail("主键为空或重复。", row=index, columns=keys)
            seen.add(key)
        rows.append(row)
    return {**copy.deepcopy(dataset), "columns": [f["name"] for f in definition["columns"]], "rows": rows,
            "raw_columns": list(dataset["columns"]), "raw_rows": copy.deepcopy(dataset["rows"]),
            "data_contract": definition, "data_contract_version": VERSION,
            "normalization": {"contract_hash": fingerprint(definition), "source_hash": fingerprint(dataset),
                              "normalized_hash": fingerprint(rows), "changed_cells": changed,
                              "row_count": len(rows), "dropped_rows": 0, "invalid_policy": "reject"}}


def apply_contract(pack, contract):
    if not isinstance(contract, dict) or set(contract) != {"format", "datasets"} or contract["format"] != VERSION or not isinstance(contract["datasets"], dict):
        fail("数据契约必须包含 format=agentforge-data-contract-v1 和 datasets 映射。")
    result = copy.deepcopy(pack)
    matched = set()
    for index, dataset in enumerate(pack["datasets"]):
        keys = [key for key in contract["datasets"] if key in (dataset["id"], dataset["name"])]
        if len(keys) > 1 or keys and keys[0] in matched:
            fail("数据集契约匹配不唯一。")
        if keys:
            matched.add(keys[0])
            result["datasets"][index] = apply_dataset_contract(dataset, contract["datasets"][keys[0]])
    if matched != set(contract["datasets"]):
        fail("数据契约引用了未提供的数据集。")
    return result


def typed_filter(row, item, fields):
    """Predicate semantics for normalized data; never coerce identifiers to numbers."""
    field = fields[item["column"]]
    left, op = row[item["column"]], item["operator"]
    right = normalize_cell(item["value"], {**field, "nullable": True})
    if op == "contains" and field.get("type", "text") != "text":
        fail("contains 仅适用于文本字段。")
    if op in ("gt", "gte", "lt", "lte") and field.get("type") not in ("integer", "decimal", "date", "datetime"):
        fail("有序比较仅适用于数值、日期和时间字段。")
    if op in ("eq", "ne"):
        equal = left == right
        if left is not None and right is not None and field.get("type") in ("integer", "decimal"):
            equal = decimal_value(left) == decimal_value(right)
        return equal if op == "eq" else not equal
    if left is None or right is None:
        return False
    if op == "contains":
        return right in left
    if field.get("type") in ("integer", "decimal"):
        left, right = decimal_value(left), decimal_value(right)
    elif field.get("type") not in ("date", "datetime"):
        fail("有序比较仅适用于数值、日期和时间字段。")
    elif field.get("type") == "datetime":
        left, right = datetime.fromisoformat(left), datetime.fromisoformat(right)
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]

