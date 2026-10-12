"""Delivery lifecycle before any optional Agent factory work."""

import json
import uuid

from .codex_client import object_schema, text_schema, validate_json
from .csv_contract import contract_catalog
from .delivery_artifacts import export_delivery
from .program_delivery import DataProgram, evaluate_cases, validate_recipe
from .resources import catalog, fingerprint, read_json, write_json
from .sql_query import csv_sql_catalog

DIRECT_SCHEMA = object_schema({"answer": text_schema(30000), "evidence": {"type": "array", "items": text_schema(2000)},
                               "limitations": {"type": "array", "items": text_schema(2000)}})
PLAN_SCHEMA = object_schema({"status": {"type": "string", "enum": ["ready", "needs_input", "unsupported"]},
                             "reason": text_schema(3000), "recipe_json": {"type": "string", "maxLength": 40000}})


def freeze(factory, state, name, value):
    write_json(factory.run_dir(state["run_id"]) / (name + ".json"), value)
    state["frozen_hashes"][name] = fingerprint(value)


def step_delivery(factory, state):
    base = factory.run_dir(state["run_id"])
    request = read_json(base / "delivery_request.json")
    resources = factory.resources(state)
    kind = state["delivery"]["kind"]
    if state["phase"] == "deliver_plan":
        recipe = request["recipe"]
        if recipe is None and kind == "program":
            context = {"request": state["request"], "resources": catalog(resources, include_content=True),
                       "csv_contract": contract_catalog(), "sql_tables": csv_sql_catalog(resources),
                       "directions": state["human_directions"],
                       "recipe_format": {"format": "agentforge-program-v1", "operation": "csv.query | csv.metrics | csv.sql",
                                         "arguments": {}, "parameters": {}, "defaults": {}}}
            planned = factory.planner.client.complete(
                "Compile the repeated task into one deterministic computation. Do not generate an agent or Python source. "
                "Return recipe_json with exactly the shown recipe fields. csv.query arguments follow csv_contract; "
                "csv.metrics can ONLY use metrics already declared in data_contract. Its arguments are dataset_id, "
                "metric_ids, dimensions, filters, sort_by, descending, limit. csv.sql arguments are fixed sql and "
                "parameters (object of named scalar bindings). Use SQL bound parameters, never string interpolation. "
                "For variable values use {\"$param\":\"name\"}, declare each scalar JSON schema in parameters "
                "and provide all defaults. Do not parameterize dataset_id or SQL syntax. Empty parameters/defaults "
                "are valid for fixed computations. Monetary semantics must be explicit; ask for missing business "
                "definitions with needs_input, never invent them. Unsupported tasks return unsupported. "
                "Do not request or infer held-out acceptance answers.", context, PLAN_SCHEMA)
            validate_json(planned, PLAN_SCHEMA)
            if planned["status"] != "ready":
                state.update(status="waiting_for_guidance" if planned["status"] == "needs_input" else "unsupported",
                             stop_reason=planned["reason"])
                return planned["reason"]
            try:
                recipe = json.loads(planned["recipe_json"])
            except ValueError as exc:
                raise ValueError("模型返回的程序配方不是有效 JSON。") from exc
        if recipe is not None:
            recipe = validate_recipe(recipe, resources)
            freeze(factory, state, "delivery_recipe", recipe)
        state["phase"] = "deliver_check"
        return "已冻结计算规则。" if recipe is not None else "直接使用 Codex 求解，不构建 Agent。"
    if state["phase"] == "deliver_check":
        if "delivery_recipe" in state["frozen_hashes"]:
            program = DataProgram(read_json(base / "delivery_recipe.json"), resources)
            result = program.run({}, max_seconds=state["performance"]["run_max_seconds"])
            if result["error"]:
                raise ValueError("程序执行检查失败：" + result["error"]["message"])
            validation = evaluate_cases(program, request["acceptance_cases"])
            if validation["total"] and not validation["all_passed"]:
                freeze(factory, state, "delivery_result", result)
                freeze(factory, state, "delivery_validation", validation)
                state.update(status="rejected", stop_reason="用户提供的业务验收未通过，已保留证据并阻止导出。")
                return state["stop_reason"]
        else:
            relative = "direct_outputs/" + uuid.uuid4().hex[:12]
            result = factory.planner.client.solve(state["request"],
                {"request": state["request"], "resources": resources, "directions": state["human_directions"]},
                DIRECT_SCHEMA, base / relative)
            validate_json(result, DIRECT_SCHEMA)
            result = {**result, "execution": {"path": "direct_codex", "model_sessions": 1,
                                             "tool_actions": "see evidence/codex-events.jsonl"}}
            state["direct_output_dir"] = relative
            validation = {"scope": "not_evaluated", "all_passed": False, "total": 0, "passed": 0, "cases": []}
        validation["execution_check"] = "passed"
        freeze(factory, state, "delivery_result", result)
        freeze(factory, state, "delivery_validation", validation)
        state["phase"] = "deliver_export"
        return "结果与证据已保存；业务验收范围：" + validation["scope"]
    if state["phase"] == "deliver_export":
        export_delivery(factory, state)
        state.update(status="completed", phase="completed")
        return "已交付答案与证据。" if kind == "direct" else "已交付不依赖 Codex 的确定性程序。"
    raise ValueError("不支持的交付阶段。")
