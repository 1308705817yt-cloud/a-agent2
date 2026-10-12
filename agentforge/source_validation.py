"""Local output-hook preflight and runtime enforcement; no model or held-out oracles."""

import copy
import json

from .contracts import ExecutionError, error_response
from .program import Program
from .workflow import validate_response
from .csv_contract import contract_catalog


def response_contract():
    return {"csv_contract": contract_catalog(), "hook_context": {"input": "effective user parameters", "result":
                {"answer": "nonempty string", "data": "observed JSON or null", "sources": "[{id,title,quote}]",
                 "abstained": "boolean", "error": "null or {code,message}"}},
            "exact_output_fields": ["answer", "data", "sources", "abstained", "error"],
            "mutable_fields": ["answer"], "preserved_fields": ["data", "sources", "abstained", "error"],
            "decision_only_fields": ["action", "reason", "plan", "tool", "arguments_json", "result_ref"],
            "rules": "result_ref selects an observation in a runtime decision. The host resolves it to data before the hook. "
                     "result_ref and final_response are NEVER hook fields. Return all five original fields; only answer may change. "
                     "No new finish flags or context bindings exist. Keep error and refusal fields unchanged."}


def apply_output_hook(source, payload, inputs, resources):
    # Input-envelope limits belong to the runtime; executed code/output defects belong to source.
    validate_response(payload, resources)
    program = Program(source)
    try:
        transformed = program.run({"input": inputs, "result": copy.deepcopy(payload)})
        validate_response(transformed, resources)
        changed = [k for k in ("data", "sources", "abstained", "error")
                   if json.dumps(transformed[k], sort_keys=True, ensure_ascii=False, allow_nan=False)
                   != json.dumps(payload[k], sort_keys=True, ensure_ascii=False, allow_nan=False)]
        if changed:
            raise ExecutionError("OUTPUT_CONTRACT", "输出代码只能修改 answer；被修改的字段：" + ", ".join(changed))
        return transformed
    except ExecutionError as exc:
        origin = "runtime" if exc.origin == "runtime" else "source"
        raise ExecutionError(exc.code, str(exc), exc.location or "logic.py:" + str(getattr(program, "line", 1)),
                             origin=origin, retryable=False,
                             recovery=exc.recovery if origin == "runtime" else None) from exc


def preflight_output_hook(source, requirements, resources):
    """Probe host shapes, not business answers. Every source revision must pass."""
    Program(source)
    payloads = []
    citations = []
    if requirements.task_type == "document_qa":
        citations = [{"id": d["id"], "title": d["title"], "quote": d["content"][:100]}
                     for d in resources.get("documents", [])[:1] if d["content"]]
    for label, data in (("null", None), ("object", {"results": [{"group": "probe", "value": 2.5, "count": 1}],
                                               "nested": {"empty": [], "flag": False}}),
                        ("array", [{"value": 0}, {"value": None}]), ("scalar", 0)):
        payloads.append(("success-" + label, {"answer": "契约校验", "data": data, "sources": copy.deepcopy(citations), "abstained": False, "error": None}))
    for document in resources.get("documents", [])[:2]:
        if document["content"]:
            payloads.append(("document-" + document["id"], {"answer": "契约校验", "data": None,
                "sources": [{"id": document["id"], "title": document["title"], "quote": document["content"][:100]}],
                "abstained": False, "error": None}))
    payloads.extend([("refusal", {"answer": "没有足够证据", "data": None, "sources": [], "abstained": True, "error": None}),
                     ("error", error_response("PREFLIGHT_ERROR", "工具未完成"))])
    passed = []
    inputs = [("", dict(requirements.defaults))]
    if requirements.task_type == "document_qa":
        inputs = [("", {**requirements.defaults, "question": "契约校验"}),
                  ("-natural-goal", dict(requirements.defaults))]
    for suffix, effective_input in inputs:
        for label, payload in payloads:
            case_id = label + suffix
            try:
                apply_output_hook(source, payload, effective_input, resources)
            except ExecutionError as exc:
                raise ExecutionError(exc.code, "冻结前输出契约检查 " + case_id + " 失败：" + str(exc), exc.location,
                                     origin=exc.origin, retryable=False, recovery=exc.recovery) from exc
            passed.append(case_id)
    return {"stage": "before_snapshot", "all_passed": True, "model_calls": 0, "cases": passed,
            "contract": response_contract()}
