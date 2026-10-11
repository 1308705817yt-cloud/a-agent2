"""Validate and execute generated DAGs with explicit tool capabilities."""

import copy
import re
import time

from .contracts import AgentSpec, ExecutionError, error_response
from .program import Program
from .tool_registry import TOOLS, execute_tool, validate_aliases


def resolve(value, inputs):
    if isinstance(value, dict) and "ref" in value:
        if set(value) - {"ref", "default"} or not isinstance(value["ref"], str) or not value["ref"].startswith("input."):
            raise ValueError("引用只能指向 input 字段。")
        key = value["ref"][6:]
        if not re.fullmatch(r"[a-z_]+", key):
            raise ValueError("输入引用无效。")
        return copy.deepcopy(inputs.get(key, value.get("default")))
    if isinstance(value, dict):
        return {k: resolve(v, inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, inputs) for v in value]
    return value


def validate_spec(spec, requirements, resources):
    if spec.task_type != requirements.task_type or not spec.name or len(spec.name) > 100:
        raise ValueError("agent 名称或任务类型不符合需求。")
    if type(spec.max_steps) is not int or not 1 <= spec.max_steps <= 64 or not 2 <= len(spec.nodes) <= 32:
        raise ValueError("工作流节点或步数预算无效。")
    if len(spec.tools) != len(set(spec.tools)) or any(t not in TOOLS for t in spec.tools):
        raise ValueError("选择了未注册或重复的工具。")
    nodes = {}
    input_fields = {"question"} if spec.task_type == "document_qa" else {
        "operation", "column", "group_by", "filters", "sort_by", "descending", "limit"}
    def check_refs(value):
        if isinstance(value, dict):
            if "ref" in value and value["ref"][6:] not in input_fields:
                raise ValueError("引用字段不在该任务的输入契约中。")
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)
    for node in spec.nodes:
        if set(node) != {"id", "tool", "arguments", "next", "on_empty", "on_error"}:
            raise ValueError("节点字段必须是 id/tool/arguments/next/on_empty/on_error。")
        nid, tool = node["id"], node["tool"]
        if not isinstance(nid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", nid) or nid in nodes:
            raise ValueError("节点 ID 无效或重复。")
        if tool not in spec.tools or spec.task_type not in TOOLS[tool].task_types:
            raise ValueError("节点工具不在该任务的能力范围。")
        if not isinstance(node["arguments"], dict) or set(node["arguments"]) - set(TOOLS[tool].parameters):
            raise ValueError("工具参数不在注册表中。")
        resolve(node["arguments"], {})
        check_refs(node["arguments"])
        if tool == "documents.search" and "topic_aliases" in node["arguments"]:
            validate_aliases(node["arguments"]["topic_aliases"], resources)
        if tool == "csv.load" and node["arguments"].get("dataset_id") != requirements.defaults["dataset_id"]:
            raise ValueError("CSV 工作流必须使用需求绑定的数据集。")
        nodes[nid] = node
    if spec.entry not in nodes or set(spec.tools) != {n["tool"] for n in spec.nodes} or "code.transform" not in spec.tools:
        raise ValueError("入口、工具选择或代码节点不完整。")
    visited, active, checked = set(), set(), set()
    def walk(nid, kind, has_code, depth):
        if depth > spec.max_steps:
            raise ValueError("工作流路径超过步数预算。")
        if nid in active:
            raise ValueError("工作流存在循环。")
        signature = (nid, kind, has_code, depth)
        if signature in checked:
            return
        checked.add(signature)
        node = nodes[nid]
        tool = TOOLS[node["tool"]]
        if tool.input_kind not in ("any", kind):
            raise ValueError(f"{nid} 的输入类型需要 {tool.input_kind}，实际为 {kind}。")
        if node["on_empty"] and tool.output_kind not in ("matches", "table", "analysis"):
            raise ValueError("on_empty 只能用于检索或数据节点。")
        active.add(nid)
        visited.add(nid)
        has_code = has_code or tool.id == "code.transform"
        if not node["next"] and (tool.output_kind != "response" or not has_code):
            raise ValueError("每条成功路径必须返回响应并经过 code.transform。")
        for edge in ("next", "on_empty", "on_error"):
            target = node[edge]
            if target:
                if target not in nodes:
                    raise ValueError("节点指向不存在的目标。")
                walk(target, "any" if edge == "on_error" else tool.output_kind, has_code, depth + 1)
        active.remove(nid)
    walk(spec.entry, "any", False, 1)
    if visited != set(nodes):
        raise ValueError("工作流包含无法到达的节点。")
    return spec


def validate_response(output, resources):
    if not isinstance(output, dict) or set(output) != {"answer", "data", "sources", "abstained", "error"}:
        required = {"answer", "data", "sources", "abstained", "error"}
        actual = set(output) if isinstance(output, dict) else set()
        raise ExecutionError("OUTPUT_CONTRACT", "响应必须包含且仅包含 answer/data/sources/abstained/error；缺少 "
                             + repr(sorted(required - actual)) + "，多出 " + repr(sorted(actual - required, key=str)) + "。")
    if not isinstance(output["answer"], str) or not output["answer"].strip() or type(output["abstained"]) is not bool:
        raise ExecutionError("OUTPUT_CONTRACT", "回答或拒答标记无效。")
    if not isinstance(output["sources"], list):
        raise ExecutionError("OUTPUT_CONTRACT", "来源必须是列表。")
    docs = {d["id"]: d for d in resources["documents"]}
    for source in output["sources"]:
        if (not isinstance(source, dict) or set(source) != {"id", "title", "quote"}
                or any(not isinstance(source[k], str) for k in ("id", "title", "quote"))):
            raise ExecutionError("PROVENANCE", "来源格式无效。")
        doc = docs.get(source["id"])
        if not doc or source["title"] != doc["title"] or not source["quote"] or source["quote"] not in doc["content"]:
            raise ExecutionError("PROVENANCE", "来源无法在原文中核对。")
    error = output["error"]
    if error is not None and (not isinstance(error, dict) or set(error) != {"code", "message"}
                              or any(not isinstance(v, str) for v in error.values()) or not output["abstained"]):
        raise ExecutionError("OUTPUT_CONTRACT", "错误响应格式无效。")
    return output


class WorkflowAgent:
    def __init__(self, spec, requirements, resources, source):
        self.spec = validate_spec(spec, requirements, resources)
        self.requirements, self.resources = requirements, resources
        self.program = Program(source)

    def run(self, inputs):
        started = time.perf_counter()
        trace = []
        if isinstance(inputs, str) and self.spec.task_type == "document_qa":
            inputs = {"question": inputs}
        if not isinstance(inputs, dict):
            return {**error_response("INVALID_INPUT", "输入必须是 JSON 对象。"), "trace": trace}
        allowed = {"question"} if self.spec.task_type == "document_qa" else {
            "operation", "column", "group_by", "filters", "sort_by", "descending", "limit"}
        if set(inputs) - allowed:
            return {**error_response("INVALID_INPUT", "输入包含不支持的字段。"), "trace": trace}
        if self.spec.task_type == "csv_analysis":
            values = {**self.requirements.defaults, **inputs}
            if (not isinstance(values.get("operation"), str)
                    or any(v is not None and not isinstance(v, str) for v in (values.get("column"), values.get("group_by")))
                    or not isinstance(values.get("sort_by", "group"), str)):
                return {**error_response("INVALID_INPUT", "操作、列名和排序字段必须是文本。"), "trace": trace}
        context = {"input": {**copy.deepcopy(self.requirements.defaults), **copy.deepcopy(inputs)}, "result": None}
        nodes = {n["id"]: n for n in self.spec.nodes}
        nid = self.spec.entry
        try:
            for _ in range(self.spec.max_steps):
                if not nid:
                    break
                node = nodes[nid]
                entry = {"node": nid, "tool": node["tool"], "status": "ok"}
                trace.append(entry)
                try:
                    params = resolve(node["arguments"], context["input"])
                    entry["arguments"] = params
                    if node["tool"] == "code.transform":
                        previous = copy.deepcopy(context["result"])
                        context["result"] = self.program.run(context)
                        validate_response(context["result"], self.resources)
                        if previous["error"] is not None and context["result"]["error"] != previous["error"]:
                            raise ExecutionError("OUTPUT_CONTRACT", "生成代码不能隐藏工具错误。")
                    else:
                        context["result"] = execute_tool(node["tool"], params, context, self.resources)
                        if node["tool"] == "documents.search":
                            entry["retrieval"] = context.get("retrieval")
                    result = context["result"]
                    empty = not result or isinstance(result, dict) and (
                        "rows" in result and not result["rows"] or "results" in result and not result["results"])
                    nid = node["on_empty"] if empty and node["on_empty"] else node["next"]
                except ExecutionError as exc:
                    entry.update(status="error", code=exc.code, message=str(exc), location=exc.location)
                    context["last_error"] = {"code": exc.code, "message": str(exc)}
                    if not node["on_error"]:
                        raise
                    nid = node["on_error"]
            else:
                raise ExecutionError("WORKFLOW_LIMIT", "工作流超过执行步数。")
            output = validate_response(context["result"], self.resources)
            if self.spec.task_type == "document_qa":
                if not output["abstained"] and not output["error"] and not output["sources"]:
                    raise ExecutionError("PROVENANCE", "文档回答必须包含可核对的来源。")
                if output["abstained"] and output["sources"]:
                    raise ExecutionError("PROVENANCE", "拒答不能附带被当作答案证据的来源。")
        except ExecutionError as exc:
            output = error_response(exc.code, str(exc))
        except (TypeError, ValueError, KeyError, IndexError, OverflowError) as exc:
            output = error_response("INVALID_INPUT", str(exc)[:300])
            if trace:
                trace[-1].update(status="error", code="INVALID_INPUT", message=str(exc)[:300])
        return {**output, "trace": trace, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
