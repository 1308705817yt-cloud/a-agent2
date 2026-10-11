"""工具注册表及确定性的资源操作，生成工作流只能引用已注册能力。"""

import math
import re
from dataclasses import dataclass

from .contracts import ExecutionError, OPERATIONS, error_response


ABSTENTION = "资料中没有足够证据回答这个问题。"


@dataclass(frozen=True)
class Tool:
    id: str
    description: str
    task_types: tuple
    input_kind: str
    output_kind: str
    parameters: tuple


TOOLS = {tool.id: tool for tool in [
    Tool("documents.search", "英文词/中文相邻双字词项检索；score=交集词项/问题词项。min_score范围(0,1]，top_k范围1至10。可配置topic_aliases为{文档ID:[主题词及同义词]}；配置后只召回主题词命中的文档，命中得分1，泛化到主题同义表达并拒绝无关主题。", ("document_qa",), "any", "matches", ("question", "min_score", "top_k", "topic_aliases")),
    Tool("documents.answer", "按检索原文回答并附带可核对的来源", ("document_qa",), "matches", "response", ()),
    Tool("documents.refuse", "无证据时明确拒答", ("document_qa",), "any", "response", ()),
    Tool("csv.load", "载入授权数据集，检查数据输入字段", ("csv_analysis",), "any", "table", ("dataset_id",)),
    Tool("csv.filter", "按照输入或默认条件筛选数据行", ("csv_analysis",), "table", "table", ("filters",)),
    Tool("csv.aggregate", "按列及可选分组计算 sum/mean/count/min/max", ("csv_analysis",), "table", "analysis", ("operation", "column", "group_by")),
    Tool("csv.sort", "按分组或聚合值排序", ("csv_analysis",), "analysis", "analysis", ("sort_by", "descending")),
    Tool("csv.limit", "限制结果数量，用于排名或摘要", ("csv_analysis",), "analysis", "analysis", ("limit",)),
    Tool("csv.format", "把分析结果包装成统一响应", ("csv_analysis",), "analysis", "response", ()),
    Tool("output.error", "把工具错误转换成结构化响应", ("document_qa", "csv_analysis"), "any", "response", ()),
    Tool("code.transform", "执行生成的受约束 Python 输出逻辑", ("document_qa", "csv_analysis"), "response", "response", ())
]}


def registry_catalog():
    return [{"id": t.id, "description": t.description, "task_types": list(t.task_types),
             "input_kind": t.input_kind, "output_kind": t.output_kind, "parameters": list(t.parameters)} for t in TOOLS.values()]


def tokenize(text):
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return tokens


def number(value):
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ExecutionError("NON_NUMERIC_COLUMN", "所选列包含非数值内容。")
    if not math.isfinite(result):
        raise ExecutionError("NON_NUMERIC_COLUMN", "所选列包含非有限数值。")
    return result


def validate_filters(filters, columns):
    if not isinstance(filters, list) or len(filters) > 20:
        raise ExecutionError("INVALID_INPUT", "筛选条件必须是列表，最多 20 项。")
    for item in filters:
        if not isinstance(item, dict) or set(item) != {"column", "operator", "value"}:
            raise ExecutionError("INVALID_INPUT", "筛选条件需要 column、operator 和 value。")
        if not isinstance(item["column"], str) or not isinstance(item["operator"], str):
            raise ExecutionError("INVALID_INPUT", "筛选列和运算符必须是文本。")
        if item["column"] not in columns:
            raise ExecutionError("UNKNOWN_COLUMN", "筛选列不存在。")
        if item["operator"] not in ("eq", "ne", "gt", "gte", "lt", "lte", "contains"):
            raise ExecutionError("INVALID_INPUT", "筛选运算符不受支持。")


def matches_filter(row, item):
    actual, expected, op = row[item["column"]], item["value"], item["operator"]
    if op == "contains":
        return str(expected) in str(actual)
    if op in ("eq", "ne"):
        equal = str(actual) == str(expected)
        return equal if op == "eq" else not equal
    left, right = number(actual), number(expected)
    if left is None or right is None:
        return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


def validate_aliases(aliases, resources):
    if aliases is None:
        return
    ids = {d["id"] for d in resources["documents"]}
    if not isinstance(aliases, dict) or not aliases or set(aliases) - ids:
        raise ExecutionError("INVALID_ARGUMENTS", "主题映射必须指向已有文档。")
    for terms in aliases.values():
        if (not isinstance(terms, list) or not 1 <= len(terms) <= 40
                or any(not isinstance(t, str) or not 2 <= len(t.strip()) <= 100 for t in terms)):
            raise ExecutionError("INVALID_ARGUMENTS", "每个主题需要 1 至 40 个长度为 2 至 100 的词语。")


def execute_tool(tool_id, params, context, resources):
    previous = context.get("result")
    if tool_id == "documents.search":
        question = params.get("question", context["input"].get("question"))
        if not isinstance(question, str) or not question.strip():
            raise ExecutionError("INVALID_INPUT", "请提供非空问题。")
        score = params.get("min_score", 0.15)
        top_k = params.get("top_k", 1)
        if type(score) not in (float, int) or not 0 < score <= 1:
            raise ExecutionError("INVALID_ARGUMENTS", "min_score 必须为大于 0 且小于等于 1 的数值。")
        if type(top_k) is not int or not 1 <= top_k <= 10:
            raise ExecutionError("INVALID_ARGUMENTS", "top_k 必须为 1 至 10 的整数。")
        tokens = tokenize(question)
        aliases = params.get("topic_aliases")
        validate_aliases(aliases, resources)
        found = []
        context["retrieval"] = {"min_score": score, "query_tokens": sorted(tokens), "candidates": []}
        for doc in resources["documents"]:
            coverage = len(tokens & tokenize(doc["title"] + " " + doc["content"])) / max(len(tokens), 1)
            if aliases is not None:
                hits = [t for t in aliases.get(doc["id"], []) if t.lower() in question.lower()]
                coverage = 1.0 if hits else 0.0
            else:
                hits = []
            context["retrieval"]["candidates"].append({"id": doc["id"], "score": round(coverage, 4), "topic_hits": hits})
            if coverage >= score:
                found.append({**doc, "score": coverage})
        return sorted(found, key=lambda item: (-item["score"], item["id"]))[:top_k]
    if tool_id == "documents.answer":
        if not previous:
            return {"answer": ABSTENTION, "data": None, "sources": [], "abstained": True, "error": None}
        sources = [{"id": doc["id"], "title": doc["title"], "quote": doc["content"]} for doc in previous]
        return {"answer": "\n\n".join(doc["content"] + f"\n[来源：{doc['id']} · {doc['title']}]" for doc in previous),
                "data": None, "sources": sources, "abstained": False, "error": None}
    if tool_id == "documents.refuse":
        return {"answer": ABSTENTION, "data": None, "sources": [], "abstained": True, "error": None}
    if tool_id == "csv.load":
        dataset = next((d for d in resources["datasets"] if d["id"] == params.get("dataset_id")), None)
        if not dataset:
            raise ExecutionError("UNKNOWN_RESOURCE", "数据集未授权或不存在。")
        return {"columns": dataset["columns"], "rows": dataset["rows"], "dataset_id": dataset["id"]}
    if tool_id == "csv.filter":
        filters = params.get("filters", [])
        validate_filters(filters, previous["columns"])
        return {**previous, "rows": [row for row in previous["rows"] if all(matches_filter(row, f) for f in filters)]}
    if tool_id == "csv.aggregate":
        operation = params.get("operation", "sum")
        column, group_by = params.get("column"), params.get("group_by")
        if operation not in OPERATIONS:
            raise ExecutionError("INVALID_INPUT", "聚合操作不受支持。")
        if operation != "count" and not column:
            raise ExecutionError("UNKNOWN_COLUMN", "该聚合需要数值列。")
        if any(field and field not in previous["columns"] for field in (column, group_by)):
            raise ExecutionError("UNKNOWN_COLUMN", "分析列或分组列不存在。")
        groups = {} if group_by else {None: []}
        for row in previous["rows"]:
            key = row[group_by] if group_by else None
            groups.setdefault(key, []).append(row)
        if len(groups) > 1000:
            raise ExecutionError("RESULT_LIMIT", "分组数量超过 1000。")
        results = []
        for group, rows in groups.items():
            values = [number(row[column]) for row in rows] if column and operation != "count" else []
            values = [value for value in values if value is not None]
            if operation == "count":
                value = len(rows)
            elif operation == "sum":
                value = math.fsum(values)
            elif operation == "mean":
                value = math.fsum(values) / len(values) if values else None
            else:
                value = (min(values) if operation == "min" else max(values)) if values else None
            results.append({"group": group, "value": value, "count": len(rows)})
        return {"dataset_id": previous["dataset_id"], "operation": operation, "column": column,
                "group_by": group_by, "results": sorted(results, key=lambda row: str(row["group"]))}
    if tool_id == "csv.sort":
        by, descending = params.get("sort_by", "group"), params.get("descending", False)
        if by not in ("group", "value", "count") or type(descending) is not bool:
            raise ExecutionError("INVALID_INPUT", "排序字段或方向无效。")
        def key(row):
            value = row[by]
            return (value is not None, str(value) if by == "group" else value or 0)
        return {**previous, "results": sorted(previous["results"], key=key, reverse=descending)}
    if tool_id == "csv.limit":
        limit = params.get("limit")
        if limit is None:
            return previous
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ExecutionError("INVALID_INPUT", "结果数量应在 1 至 1000 之间。")
        return {**previous, "results": previous["results"][:limit]}
    if tool_id == "csv.format":
        return {"answer": json_summary(previous), "data": previous, "sources": [], "abstained": False, "error": None}
    if tool_id == "output.error":
        error = context.get("last_error", {"code": "EXECUTION_ERROR", "message": "工具执行失败。"})
        return error_response(error["code"], error["message"])
    raise ExecutionError("UNKNOWN_TOOL", "工具未注册。")


def json_summary(analysis):
    rows = analysis["results"]
    if not rows:
        return "没有满足条件的数据。"
    return "；".join((str(row["group"]) + "：" if row["group"] is not None else "") +
                    str(row["value"]) + f"（{row['count']} 行）" for row in rows)
