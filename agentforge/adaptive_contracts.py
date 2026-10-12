"""Contracts for runtime model decisions and generated data-processing tools."""

import json
import re
from dataclasses import asdict, dataclass, field

from .codex_client import object_schema, text_schema, validate_json, SchemaValidationError
from .contracts import ExecutionError


BUILTIN_IDS = ("resources.list", "documents.read", "documents.search", "csv.preview", "csv.query", "csv.profile", "csv.metrics", "csv.sql")
PARAM_TYPES = ("string", "integer", "number", "boolean", "object", "array")


@dataclass(frozen=True)
class AdaptiveSpec:
    name: str
    task_type: str
    instructions: str
    planning_hints: list[str]
    tools: list[str]
    custom_tools: list[dict]
    max_steps: int = 8
    runtime: str = "codex_loop"
    trusted_tools: list[dict] = field(default_factory=list)
    execution_policy: dict = field(default_factory=dict)
    answer_style: str = "analysis"

    def to_dict(self):
        return asdict(self)

    def validate(self, requirements, built=False):
        if self.runtime != "codex_loop" or self.task_type != requirements.task_type or not self.name or not self.instructions:
            raise ValueError("自适应 Agent 配置与需求不一致。")
        if self.answer_style not in ("facts", "analysis"):
            raise ValueError("answer_style 必须是 facts 或 analysis。")
        if type(self.max_steps) is not int or not 3 <= self.max_steps <= 16:
            raise ValueError("Codex 决策预算应为 3 至 16 步。")
        policy = self.execution_policy
        if not isinstance(policy, dict) or set(policy) - {"adaptive_steps", "max_seconds", "csv_strategy", "resource_profile", "csv_contract_version", "data_binding", "output_template"}:
            raise ValueError("运行策略字段无效。")
        if "adaptive_steps" in policy and type(policy["adaptive_steps"]) is not bool:
            raise ValueError("adaptive_steps 必须是布尔值。")
        if policy.get("data_binding") not in (None, "compatible-v1") or policy.get("output_template") not in (None, "observed-v1"):
            raise ValueError("数据绑定或输出模板版本无效。")
        if policy.get("data_binding") and (self.task_type != "csv_analysis" or self.custom_tools or self.trusted_tools
                                           or policy.get("output_template") != "observed-v1"):
            raise ValueError("兼容数据绑定仅用于具有宿主输出模板、无自定义和实时工具的 CSV Agent。")
        if policy.get("csv_strategy", "loop") not in ("auto", "loop"):
            raise ValueError("csv_strategy 必须是 auto 或 loop。")
        if "resource_profile" in policy and type(policy["resource_profile"]) is not bool:
            raise ValueError("resource_profile 必须是布尔值。")
        if policy.get("resource_profile", False) and (self.task_type != "csv_analysis" or "csv.profile" not in self.tools):
            raise ValueError("资源级质量检查需要 CSV 任务及 csv.profile 能力。")
        from .csv_contract import CONTRACT_VERSION
        if "csv_contract_version" in policy and policy["csv_contract_version"] != CONTRACT_VERSION:
            raise ValueError("CSV 契约版本与冻结运行时不一致。")
        seconds = policy.get("max_seconds", 0)
        if type(seconds) not in (int, float) or not 0 <= seconds <= 86400:
            raise ValueError("运行总耗时应为 0 至 86400 秒；0 表示不设总上限。")
        if not 0 <= len(self.custom_tools) <= 3 or len(self.tools) != len(set(self.tools)):
            raise ValueError("允许 0 至 3 个自定义工具，能力列表不得重复。")
        ids = set()
        for tool in self.custom_tools:
            if set(tool) - {"execution_plan", "error_cases"} != {"id", "description", "parameters", "output_schema", "required", "source"}:
                raise ValueError("自定义工具定义字段无效。")
            if "execution_plan" in tool:
                from .program import validate_execution_plan
                validate_execution_plan(tool["execution_plan"], "agent.json.custom_tools." + str(tool.get("id")))
            tid = tool["id"]
            if not isinstance(tid, str) or not re.fullmatch(r"custom\.[a-z][a-z0-9_]{0,39}", tid) or tid in ids:
                raise ValueError("自定义工具名称无效或重复。")
            ids.add(tid)
            if not isinstance(tool["description"], str) or not tool["description"].strip() or type(tool["required"]) is not bool:
                raise ValueError("自定义工具必须说明用途与是否为必需步骤。")
            params = tool["parameters"]
            names = set()
            if not isinstance(params, list) or len(params) > 12:
                raise ValueError("自定义工具最多允许 12 个参数。")
            for p in params:
                if (set(p) != {"name", "type", "required", "description"} or p["type"] not in PARAM_TYPES
                        or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", p["name"]) or p["name"] in names
                        or type(p["required"]) is not bool):
                    raise ValueError("自定义工具参数定义无效。")
                names.add(p["name"])
            validate_schema_definition(tool["output_schema"])
            if "error_cases" in tool:
                validate_error_cases(tool)
            if built and (not isinstance(tool["source"], str) or not tool["source"].strip()):
                raise ValueError("自定义工具缺少源码。")
        from .trusted_tools import validate_definitions
        validate_definitions(self.trusted_tools)
        trusted_ids = {t["id"] for t in self.trusted_tools}
        if any(t not in {*BUILTIN_IDS, *ids, *trusted_ids} for t in self.tools) or not ids <= set(self.tools) or not trusted_ids <= set(self.tools):
            raise ValueError("Agent 选择了未授权工具或遗漏生成的工具。")
        if self.task_type == "csv_analysis" and "csv.query" not in self.tools:
            raise ValueError("CSV Agent 必须具备 csv.query 作为主结果计算能力。")
        return self


def validate_schema_definition(schema, depth=0):
    import math
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum", "anyOf",
               "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum"}
    if not isinstance(schema, dict) or depth > 6 or set(schema) - allowed:
        raise ValueError("工具输出 schema 不在支持范围内。")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 100):
        raise ValueError("工具 enum 必须是有界值列表。")
    if "anyOf" in schema:
        if not isinstance(schema["anyOf"], list) or not 1 <= len(schema["anyOf"]) <= 4:
            raise ValueError("工具 anyOf 无效。")
        for child in schema["anyOf"]:
            validate_schema_definition(child, depth + 1)
    elif schema.get("type") not in (*PARAM_TYPES, "null"):
        raise ValueError("工具输出 schema 必须声明 JSON 类型。")
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        if not isinstance(props, dict) or len(props) > 30 or schema.get("additionalProperties") is not False:
            raise ValueError("输出对象必须声明 properties 并关闭 additionalProperties。")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(k, str) for k in required) or set(required) - set(props):
            raise ValueError("工具输出必需字段无效。")
        for child in props.values():
            validate_schema_definition(child, depth + 1)
    if schema.get("type") == "array":
        validate_schema_definition(schema.get("items", {}), depth + 1)
    # Reject malformed schema constraints before they reach validation code.
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or not 0 <= schema[key] <= 20000):
            raise ValueError("工具 schema 大小约束无效。")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError("工具 schema 数值约束无效。")
    for lower, upper in (("minItems", "maxItems"), ("minLength", "maxLength"), ("minimum", "maximum")):
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise ValueError("工具 schema 上下界不一致。")


def parameter_schema(tool):
    properties = {p["name"]: {"type": p["type"]} for p in tool["parameters"]}
    return {"type": "object", "properties": properties,
            "required": [p["name"] for p in tool["parameters"] if p["required"]], "additionalProperties": False}


def validate_error_cases(tool):
    """Design examples must respect the executable boundary, before source exists."""
    cases = tool["error_cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise ExecutionError("DESIGN_ERROR_BOUNDARY", "工具必须声明有界的错误归属样例。", tool["id"], origin="design")
    ids = set()
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {"id", "layer", "input_json", "code", "description"}
                or not isinstance(case["id"], str) or not case["id"] or case["id"] in ids
                or case["layer"] not in ("host", "tool_result") or not isinstance(case["description"], str)
                or not case["description"].strip() or not isinstance(case["code"], str) or not case["code"]):
            raise ExecutionError("DESIGN_ERROR_BOUNDARY", "错误归属样例字段或 ID 无效。", tool["id"], origin="design")
        ids.add(case["id"])
        try:
            inputs = parse_json(case["input_json"])
        except ExecutionError as exc:
            raise ExecutionError("DESIGN_ERROR_BOUNDARY", "错误样例也必须提供合法 JSON。", case["id"], origin="design") from exc
        mismatch = None
        try:
            validate_json(inputs, parameter_schema(tool), "input")
        except SchemaValidationError as exc:
            mismatch = exc
        if case["layer"] == "host" and mismatch is not None and case["code"] == "INVALID_ARGUMENTS":
            continue
        if case["layer"] == "tool_result" and mismatch is None:
            continue
        details = {"case_id": case["id"], "rejection_layer": "host" if mismatch else "tool_result",
                   **(mismatch.details if mismatch else {"field_path": "input", "expected_type": "schema-invalid input", "actual_type": "schema-valid input"})}
        raise ExecutionError("DESIGN_ERROR_BOUNDARY", "错误样例 " + case["id"] + " 的归属与参数契约矛盾；"
                             "类型、必需字段、未知字段和宿主大小限制由宿主返回 INVALID_ARGUMENTS；"
                             "工具业务错误必须使用合法参数并返回 output_schema 内的结果。",
                             details["field_path"], origin="design", details=details)
    if not any(c["layer"] == "host" for c in cases):
        raise ExecutionError("DESIGN_ERROR_BOUNDARY", "工具设计必须包含至少一个宿主拒绝样例。", tool["id"], origin="design")


def decision_schema(tools, task_type=None, observation_ids=None):
    nullable_text = {"anyOf": [text_schema(200), {"type": "null"}]}
    if observation_ids is not None:
        nullable_text = {"anyOf": [{"type": "string", "enum": observation_ids}, {"type": "null"}]} if observation_ids else {"type": "null"}
    return object_schema({
        "action": {"type": "string", "enum": ["call_tool", "finish", "ask_user"]},
        "reason": text_schema(1200),
        "plan": {"type": "array", "items": text_schema(300), "minItems": 1, "maxItems": 8},
        "tool": {"anyOf": [{"type": "string", "enum": tools}, {"type": "null"}]},
        "arguments_json": {"type": "string", "maxLength": 12000},
        "answer": {"type": "string", "maxLength": 12000},
        "result_ref": nullable_text,
        "sources": {"type": "array", "items": object_schema({"id": text_schema(100), "quote": text_schema(6000)}), "maxItems": 0 if task_type == "csv_analysis" else 10},
        "abstained": {"type": "boolean"}
    })


def parse_json(text):
    try:
        return json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限数值")))
    except (ValueError, TypeError) as exc:
        raise ExecutionError("INVALID_ARGUMENTS", "参数不是有效 JSON。") from exc
