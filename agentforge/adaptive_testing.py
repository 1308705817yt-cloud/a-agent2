"""Frozen custom-tool unit oracles and live Codex agent behavior checks."""

from concurrent.futures import ThreadPoolExecutor

from .contracts import ExecutionError
from .custom_tools import CustomTool, validate_tool_arguments
from .resources import fingerprint
from .testing import csv_oracle, equivalent, evaluate, OracleError, document_answer_issues
from .adaptive_contracts import parameter_schema
from .codex_client import validate_json, SchemaValidationError, json_type
from .evaluation_progress import EvaluationProgress


class SuiteValidationError(ValueError):
    def __init__(self, case, details, message):
        self.details = {"case_id": case["id"], **details}
        super().__init__(message + " 用例=" + case["id"] + "，字段=" + details.get("field_path", "input")
                         + "，期望=" + details.get("expected_type", "valid expectation")
                         + "，实际=" + details.get("actual_type", "invalid expectation")
                         + "，拒绝层=" + details.get("rejection_layer", "test_definition") + "。")


def validate_tool_case(case, definition):
    expected_error = case.get("expected_error")
    has_result = "expected" in case
    has_error = expected_error is not None
    if has_error == has_result:
        raise SuiteValidationError(case, {"field_path": "expected_error", "expected_type": "exactly one of expected / expected_error",
                                   "actual_type": "both" if has_result else "neither", "rejection_layer": "test_definition"},
                                   "合法计算测试与宿主拒绝测试必须使用互斥断言。")
    rejection = None
    try:
        validate_tool_arguments(definition, case["input"])
    except ExecutionError as exc:
        rejection = exc
    if has_error:
        if expected_error != {"code": "INVALID_ARGUMENTS", "origin": "decision", "layer": "host"}:
            raise SuiteValidationError(case, {"field_path": "expected_error", "expected_type": "INVALID_ARGUMENTS / decision / host",
                                       "actual_type": json_type(expected_error), "rejection_layer": "host"}, "宿主负例断言无效。")
        if rejection is None:
            raise SuiteValidationError(case, {"field_path": "input", "expected_type": "schema-invalid input",
                                       "actual_type": "schema-valid input", "rejection_layer": "host"},
                                       "宿主负例使用了合法参数；工具业务错误应断言正常输出。")
        return
    if rejection:
        raise SuiteValidationError(case, rejection.details,
                                   "该参数在源码执行前被宿主拒绝。保留负例，使用 expected_error，并将 expected_json 设为 null。") from rejection
    try:
        validate_json(case["expected"], definition["output_schema"], "expected")
    except SchemaValidationError as exc:
        raise SuiteValidationError(case, {**exc.details, "rejection_layer": "tool_output"},
                                   "工具结果断言与输出契约矛盾。") from exc


def validate_extra_suite(suite, spec, req, resources):
    ids = {c["id"] for c in suite["cases"]}
    tools = {t["id"]: t for t in spec.custom_tools}
    docs = {d["id"]: d for d in resources["documents"]}
    for c in [*suite["tool_cases"], *suite["goal_cases"]]:
        if c["id"] in ids or c["partition"] not in ("development", "acceptance"):
            raise ValueError("新增验收 ID 重复或分区无效。")
        ids.add(c["id"])
        if (not isinstance(c["input"], dict) and not c.get("expected_error")) or not c["purpose"] or not c["criterion"]:
            raise ValueError("工具与目标测试必须有输入及需求依据。")
    for c in suite["tool_cases"]:
        if c["tool"] not in tools:
            raise ValueError("工具测试引用了未生成的工具。")
        validate_tool_case(c, tools[c["tool"]])
    validate_tool_coverage(suite, spec.custom_tools)
    for partition in ("development", "acceptance"):
        if {c["tool"] for c in suite["tool_cases"] if c["partition"] == partition} != set(tools):
            raise ValueError("每个自定义工具在两个分区都必须有独立测试。")
        if not any(c["partition"] == partition for c in suite["goal_cases"]):
            raise ValueError("每个分区需要运行时自然语言目标场景。")
    for c in suite["goal_cases"]:
        if req.task_type == "document_qa" and not c["refusal"]:
            doc = docs.get(c["source_id"])
            if not doc or not c["facts"] or any(f not in doc["content"] for f in c["facts"]):
                raise ValueError("目标测试的事实无法在原文核对。")
        if req.task_type == "csv_analysis":
            try:
                csv_oracle(req, resources, c["reference_query"])
            except OracleError:
                raise ValueError("CSV 自然语言场景必须有可核对的合法参考查询。")


def tool_case_role(case, definition):
    if case.get("expected_error"):
        return "host_rejection"
    inputs = fingerprint(case["input"])
    from .adaptive_contracts import parse_json
    return "business_error" if any(e["layer"] == "tool_result" and fingerprint(parse_json(e["input_json"])) == inputs
                                   for e in definition.get("error_cases", [])) else "computation"


def validate_tool_coverage(suite, definitions, partitions=("development", "acceptance")):
    """Check independent result assertions separately from both kinds of negative coverage."""
    from .adaptive_contracts import parse_json
    coverage = []
    for definition in definitions:
        for partition in partitions:
            cases = [c for c in suite["tool_cases"] if c["tool"] == definition["id"] and c["partition"] == partition]
            groups = {role: [c for c in cases if tool_case_role(c, definition) == role]
                      for role in ("computation", "host_rejection", "business_error")}
            distinct = len({fingerprint(c["input"]) for c in groups["computation"]})
            required = 2 if partition == "development" else 1
            reason = None
            if distinct < required:
                reason = f"至少 {required} 个不同的合法计算结果样例（宿主拒绝和业务错误不抵消计算覆盖）"
            elif partition == "acceptance" and not any(fingerprint(c["input"]) not in {
                    fingerprint(d["input"]) for d in suite["tool_cases"] if d["tool"] == definition["id"]
                    and d["partition"] == "development" and tool_case_role(d, definition) == "computation"}
                    for c in groups["computation"]):
                reason = "至少一个区别于开发输入的独立合法计算样例"
            elif not groups["host_rejection"]:
                reason = "至少一个宿主拒绝样例"
            for example in definition.get("error_cases", []):
                role = "host_rejection" if example["layer"] == "host" else "business_error"
                if not any(fingerprint(c["input"]) == fingerprint(parse_json(example["input_json"])) for c in groups[role]):
                    reason = "设计声明的 " + role + " 样例 " + example["id"]
                    break
            if reason:
                error = ValueError(definition["id"] + " 在 " + partition + " 分区缺少 " + reason + "。")
                error.details = {"tool_id": definition["id"], "partition": partition, "coverage_required": reason,
                                 "observed_cases": {role: [c["id"] for c in values] for role, values in groups.items()}}
                raise error
            coverage.append({"tool": definition["id"], "partition": partition,
                             **{role: [c["id"] for c in values] for role, values in groups.items()}})
    return coverage


def tool_tests(spec, req, resources, suite, partition, progress=None):
    definitions = {t["id"]: t for t in spec.custom_tools}
    for case in suite["tool_cases"]:
        if case["partition"] == partition:
            validate_tool_case(case, definitions[case["tool"]])
    validate_tool_coverage(suite, spec.custom_tools, (partition,))
    results = []
    for case in suite["tool_cases"]:
        if case["partition"] != partition:
            continue
        if progress is not None:
            cached = progress.cached(case)
            if cached is not None:
                results.append(cached)
                continue
            progress.begin(case)
        result = execute_tool_case(definitions[case["tool"]], case, req, resources)
        results.append(result)
        if progress is not None:
            progress.commit(case, result, unit=True)
    return results


def execute_tool_case(definition, case, req, resources):
    """One assertion; aggregate acceptance must additionally pass validate_tool_coverage."""
    validate_tool_case(case, definition)
    issues, actual, location, failure = [], None, "", None
    try:
        if case.get("expected_error"):
            validate_tool_arguments(definition, case["input"])
            issues.append("预期的宿主参数拒绝没有发生。")
        else:
            actual = CustomTool(definition, resources).invoke(case["input"], [], req)
        if not case.get("expected_error") and not equivalent(actual, case["expected"]):
            issues.append("生成的工具计算与独立冻结样例不一致。")
    except (ExecutionError, ValueError) as exc:
        expected = case.get("expected_error")
        matched = (isinstance(exc, ExecutionError) and expected and exc.code == expected["code"]
                   and exc.origin == expected["origin"] and (exc.details or {}).get("rejection_layer") == expected["layer"])
        if not matched:
            issues.append(str(exc))
        location = getattr(exc, "location", "")
        failure = exc.info() if isinstance(exc, ExecutionError) else None
    role = tool_case_role(case, definition)
    return {**case, "passed": not issues, "issues": issues, "actual": actual, "location": location,
            "failure": failure, "kind": "host_contract" if role == "host_rejection" else "business_error" if role == "business_error" else "custom_tool",
            "source_executed": not bool(case.get("expected_error"))}


def evaluate_adaptive(agent, req, resources, suite, partition, artifact_hash, workers=3, max_seconds=None, progress_path=None):
    validate_extra_suite(suite, agent.spec, req, resources)
    client = agent.client_factory()
    model = client.configuration() if hasattr(client, "configuration") else {
        "backend": type(client).__qualname__, "requested_model": getattr(client, "model", None),
        "request_timeout_seconds": getattr(client, "timeout", None)}
    conditions = {"max_seconds": agent.spec.execution_policy.get("max_seconds", 0) if max_seconds is None else max_seconds,
                  "requested_max_seconds": max_seconds, "model_configuration": model}
    if type(conditions["max_seconds"]) not in (int, float) or not 0 <= conditions["max_seconds"] <= 86400:
        raise ValueError("运行总耗时应为 0 至 86400 秒；0 表示不限。")
    selected = [c for key in ("cases", "goal_cases", "tool_cases") for c in suite[key] if c["partition"] == partition]
    binding = {"artifact_hash": artifact_hash, "suite_hash": fingerprint(suite), "partition": partition,
               "requirements_hash": fingerprint(req.to_dict()), "resources_hash": fingerprint(resources),
               "agent_hash": fingerprint({"spec": agent.spec.to_dict(), "source": agent.source})}
    progress = EvaluationProgress(progress_path, binding, selected, conditions)
    units = tool_tests(agent.spec, req, resources, suite, partition, progress)
    new_calls = 0
    if any(not c["passed"] for c in units):
        results = units
        behavior_skipped = True
    else:
        cases = [c for c in [*suite["cases"], *suite["goal_cases"]] if c["partition"] == partition]
        def execute(case):
            cached = progress.cached(case)
            if cached is not None:
                return case["id"], cached, 0
            checkpoint = progress.begin(case)
            options = {"checkpoint": checkpoint, "on_checkpoint": lambda value: progress.checkpoint(case, value)} if getattr(agent, "supports_resume", False) else {}
            output = agent.run(case["input"], max_seconds=max_seconds, **options,
                               audit_context={"partition": partition, "case_id": case["id"], "evaluation_attempt": progress.attempt_id})
            progress.commit(case, output)
            return case["id"], output, output.get("new_model_calls", output.get("model_calls", 0))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            executed = list(pool.map(execute, cases))
        outputs = {cid: output for cid, output, calls in executed}
        new_calls = sum(calls for cid, output, calls in executed)
        base = evaluate(agent, req, resources, suite, partition, artifact_hash, outputs=outputs)
        results = base["cases"] + units
        for case in suite["goal_cases"]:
            if case["partition"] != partition:
                continue
            output = outputs[case["id"]]
            issues, expected = [], None
            successful = [o for o in output.get("observations", []) if o["status"] == "ok"]
            if len(successful) < 2:
                issues.append("目标场景缺少多个工具的实际执行证据。")
            if req.task_type == "csv_analysis":
                expected = csv_oracle(req, resources, case["reference_query"])
                if output.get("error") or output.get("abstained") or not equivalent(output.get("data"), expected):
                    issues.append("目标场景的主查询结果与独立计算不一致。")
            elif case["refusal"]:
                if output.get("error") or not output.get("abstained") or output.get("sources"):
                    issues.append("缺少证据的目标应拒答。")
            else:
                expected = {"source_id": case["source_id"], "facts": case["facts"]}
                issues.extend(document_answer_issues(output, resources, [case["source_id"]], case["facts"]))
            results.append({**case, "passed": not issues, "issues": issues, "expected": expected, "actual": output, "kind": "agent_goal"})
        behavior_skipped = False
    passed = sum(r["passed"] for r in results)
    for result in results:
        result["evaluation"] = progress.evidence(result["id"])
    reused = progress.attempt["reused_details"]
    executed_details = progress.attempt["executed_details"]
    return {"partition": partition, "artifact_hash": artifact_hash, "suite_hash": fingerprint(suite),
            "passed": passed, "total": len(results), "all_passed": bool(results) and passed == len(results) and not behavior_skipped,
            "behavior_skipped": behavior_skipped, "cases": results,
            "evaluation_attempt": progress.attempt_id, "reused_cases": progress.attempt["reused"],
            "executed_cases": progress.attempt["executed"], "resumed_cases": progress.attempt["resumed"],
            "new_model_calls": new_calls,
            "execution_conditions": conditions, "condition_change": progress.condition_change,
            "tool_coverage": validate_tool_coverage(suite, agent.spec.custom_tools, (partition,)),
            "historical_reuse": reused, "current_executions": executed_details,
            "comparison_eligibility": "fresh" if not reused and not progress.attempt["resumed"] else "reused_or_resumed",
            "result_groups": {mode: {"total": sum(c["evaluation"]["mode"] == mode for c in results),
                                     "passed": sum(c["passed"] and c["evaluation"]["mode"] == mode for c in results)}
                              for mode in ("fresh_execution", "historical_reuse", "resumed")},
            "reused_recorded_model_calls": sum(e["recorded_model_calls"] for e in reused),
            "avoided_decision_reexecution": sum(e["committed_decisions"] for e in reused) + sum(e["reused_committed_decisions"] for e in executed_details),
            "reuse_metric_definition": "Historical billed calls and reused committed decisions are recorded separately. Avoided decision re-execution is a lower-bound estimate, not a measured counterfactual.",
            "model_calls": sum(c.get("actual", {}).get("model_calls", 0) if isinstance(c.get("actual"), dict)
                               and c.get("kind") not in ("custom_tool", "host_contract", "business_error") else 0 for c in results)}
