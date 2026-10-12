"""Frozen, parameterized computations. No model or generated Python is executed."""

import copy
import json
import math
import re
import time
from types import SimpleNamespace

from .codex_client import CodexError, DeadlineExceeded, validate_json
from .contracts import ExecutionError
from .csv_execution import execute_query
from .data_binding import OUTPUT_SOURCE, bound_resources, prepare_binding
from .metric_query import execute_metrics
from .resources import fingerprint
from .sql_query import execute_csv_sql

FORMAT = "agentforge-program-v1"
OPERATIONS = ("csv.query", "csv.metrics", "csv.sql")


def validate_recipe(value, resources):
    if not isinstance(value, dict) or set(value) != {"format", "operation", "arguments", "parameters", "defaults"}:
        raise ValueError("程序配方需要 format、operation、arguments、parameters、defaults。")
    recipe = copy.deepcopy(value)
    if recipe["format"] != FORMAT or recipe["operation"] not in OPERATIONS:
        raise ValueError("程序只支持 csv.query、csv.metrics、csv.sql。")
    if any(not isinstance(recipe[k], dict) for k in ("arguments", "parameters", "defaults")):
        raise ValueError("程序参数和默认值必须是对象。")
    if not resources.get("datasets"):
        raise ValueError("确定性数据程序需要 CSV 数据。文档语义问答可选择 direct 或 agent。")
    if len(recipe["parameters"]) > 20:
        raise ValueError("程序最多声明 20 个标量参数。")
    for name, schema in recipe["parameters"].items():
        if (not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name)
                or not isinstance(schema, dict) or schema.get("type") not in ("string", "integer", "number", "boolean", "null")
                or set(schema) - {"type", "enum", "minimum", "maximum", "minLength", "maxLength"}):
            raise ValueError("程序输入仅支持显式类型的标量参数。")
        for key in ("minimum", "maximum", "minLength", "maxLength"):
            if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
                raise ValueError("参数约束必须是有限数值。")
        if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
            raise ValueError("参数枚举必须是非空列表。")
    if set(recipe["defaults"]) != set(recipe["parameters"]):
        raise ValueError("每个参数需要默认值，以便交付前检查；不能包含未声明参数。")
    used = set()
    def check(node):
        if isinstance(node, dict):
            if "$param" in node:
                if set(node) != {"$param"} or not isinstance(node["$param"], str) or node["$param"] not in recipe["parameters"]:
                    raise ValueError("参数引用必须是 {\"$param\":\"已声明参数名\"}。")
                used.add(node["$param"])
            else:
                for child in node.values():
                    check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)
    check(recipe["arguments"])
    if used != set(recipe["parameters"]):
        raise ValueError("声明的参数必须实际用于计算，不能忽略用户输入。")
    if recipe["operation"] == "csv.sql":
        args = recipe["arguments"]
        if set(args) != {"sql", "parameters"} or not isinstance(args["sql"], str) or not isinstance(args["parameters"], dict):
            raise ValueError("SQL 配方需要固定 sql 文本和 parameters 对象；只允许具名绑定值。")
    else:
        # Names are accepted at creation; the stored recipe always uses stable logical IDs.
        dataset = recipe["arguments"].get("dataset_id")
        if dataset is None and len(resources["datasets"]) == 1:
            recipe["arguments"]["dataset_id"] = resources["datasets"][0]["id"]
        elif isinstance(dataset, str):
            matches = [d for d in resources["datasets"] if dataset in (d["id"], d["name"])]
            if len(matches) != 1:
                raise ValueError("配方数据集名称或 ID 必须唯一且已授权。")
            recipe["arguments"]["dataset_id"] = matches[0]["id"]
        else:
            raise ValueError("多份数据需要固定 dataset_id；不能在运行时扩大数据授权。")
    resolve_arguments(recipe, {})
    return recipe


def resolve_arguments(recipe, inputs):
    if not isinstance(inputs, dict) or set(inputs) - set(recipe["parameters"]):
        raise ValueError("运行程序仅接受已声明的参数对象；不能替换计算规则。")
    values = {**recipe["defaults"], **inputs}
    validate_json(values, {"type": "object", "properties": recipe["parameters"],
                           "required": list(recipe["parameters"]), "additionalProperties": False})
    # Reject NaN even for direct Python callers.
    fingerprint(values)
    def expand(node):
        if isinstance(node, dict):
            return values[node["$param"]] if "$param" in node else {k: expand(v) for k, v in node.items()}
        if isinstance(node, list):
            return [expand(v) for v in node]
        return node
    return expand(recipe["arguments"])


def binding_spec():
    return SimpleNamespace(task_type="csv_analysis", custom_tools=[], trusted_tools=[],
                           execution_policy={"data_binding": "compatible-v1", "output_template": "observed-v1"})


class DataProgram:
    supports_data_binding = True

    def __init__(self, recipe, resources, binding=None):
        self.recipe = validate_recipe(recipe, resources)
        self.original = copy.deepcopy(resources)
        self.binding = binding
        self.resources = bound_resources(binding_spec(), OUTPUT_SOURCE, resources, binding) if binding else self.original

    def prepare_data_binding(self, paths):
        return prepare_binding(binding_spec(), OUTPUT_SOURCE, self.original, paths)

    def with_data_binding(self, binding):
        return DataProgram(self.recipe, self.original, binding)

    def run(self, inputs=None, max_seconds=None):
        started = time.monotonic()
        if max_seconds is not None and (type(max_seconds) not in (int, float) or not 0 <= max_seconds <= 86400):
            raise ValueError("程序运行上限应为 0 至 86400 秒。")
        deadline = started + max_seconds if max_seconds else None
        try:
            args = resolve_arguments(self.recipe, {} if inputs is None else inputs)
            req = SimpleNamespace(defaults={})
            if self.recipe["operation"] == "csv.query":
                data = execute_query(args, req, self.resources)
            elif self.recipe["operation"] == "csv.metrics":
                data = execute_metrics(args, req, self.resources, deadline)
            else:
                data = execute_csv_sql({"sql": args["sql"],
                                        "parameters_json": json.dumps(args["parameters"], allow_nan=False)}, self.resources, deadline)
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded("程序执行达到运行时间上限。", "runtime")
            output = {"answer": "已按冻结计算规则执行，完整结果见 data。", "data": data,
                      "sources": [], "abstained": False, "error": None}
        except (ValueError, CodexError) as exc:
            output = {"answer": str(exc), "data": None, "sources": [], "abstained": True,
                      "error": exc.info() if isinstance(exc, ExecutionError) else
                      {"code": "TASK_TIMEOUT" if isinstance(exc, DeadlineExceeded) else "INVALID_INPUT", "message": str(exc)}}
        output["execution"] = {"path": "program", "model_calls": 0, "recipe_hash": fingerprint(self.recipe),
                               "resources_hash": fingerprint(self.resources),
                               "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
        output["trace"] = [{"type": "tool", "tool": self.recipe["operation"],
                            "status": "failed" if output["error"] else "completed", "result": output["data"]}]
        if self.binding:
            output["data_binding"] = {"binding_hash": self.binding["binding_hash"], "bindings": self.binding["bindings"],
                                      "behavior_evaluation": "not_evaluated_on_new_data"}
        return output


def validate_cases(cases):
    if not isinstance(cases, list) or len(cases) > 100:
        raise ValueError("验收用例应为最多 100 项的列表。")
    ids = set()
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {"id", "input", "checks"}
                or not isinstance(case["id"], str) or not case["id"] or case["id"] in ids
                or not isinstance(case["input"], dict) or not isinstance(case["checks"], list) or not case["checks"]):
            raise ValueError("验收用例需要唯一 id、input 和非空 checks。")
        ids.add(case["id"])
        for check in case["checks"]:
            if (not isinstance(check, dict) or set(check) != {"path", "equals"} or not isinstance(check["path"], list)
                    or not check["path"] or any(type(p) not in (str, int) for p in check["path"])
                    or check["path"][0] not in ("data", "error", "abstained")):
                raise ValueError("断言需要从 data/error/abstained 开始的 path 列表和 equals 值。")
    fingerprint(cases)
    return copy.deepcopy(cases)


def same_json(left, right):
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


def evaluate_cases(program, cases):
    results = []
    for case in validate_cases(cases):
        actual = program.run(case["input"])
        issues = []
        for check in case["checks"]:
            value = actual
            try:
                for key in check["path"]:
                    value = value[key]
                if not same_json(value, check["equals"]):
                    issues.append("值不匹配：" + str(check["path"]))
            except (KeyError, TypeError, IndexError):
                issues.append("路径不存在：" + str(check["path"]))
        # An unexpected error cannot pass merely because a weak assertion matches.
        expects_error = any(c["path"][0] == "error" and c["equals"] is not None for c in case["checks"])
        if actual["error"] and not expects_error:
            issues.append("程序返回了未预期的错误。")
        results.append({**case, "actual": actual, "passed": not issues, "issues": issues})
    return {"scope": "user_supplied_assertions" if cases else "not_evaluated",
            "passed": sum(c["passed"] for c in results), "total": len(results),
            "all_passed": bool(results) and all(c["passed"] for c in results), "cases": results}
