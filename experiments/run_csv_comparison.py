"""Paired real Codex answers vs a freshly built AgentForge agent. No offline mode."""

import argparse
import copy
import json
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentforge import __version__
from agentforge.benchmarking import same_value
from agentforge.codex_client import CodexClient, CodexError, DeadlineExceeded, ModelTimeout, object_schema
from agentforge.contracts import ExecutionError, Requirements, error_response
from agentforge.factory import FactoryAgent
from agentforge.resources import fingerprint, prepare_resources, read_json, write_json
from agentforge.testing import csv_oracle, OracleError
from agentforge.workflow import validate_response


DIRECT_TASK = """直接回答用户的 CSV 分析问题。使用全部已给出的账本记录，独立完成计算并给出最终响应。
你不需要生成 Agent、工具代码或测试，不调用 AgentForge 的工作流、查询工具或验证反馈。
遵守 request 的默认分析规则和 input。自然语言可以覆盖没有显式指定的默认参数，
显式 []、false、null 都是用户选择，优先于目标文字。count 计筛选后的完整行，返回 column=null。
结果 data 包含 dataset_id/operation/column/group_by/results，每个结果含 group/value/count。
count 是本组完整行数，空值不参与数值聚合；无数据的 mean/min/max 为 null。
合法结果 sources=[]、abstained=false、error=null；非法列名返回 UNKNOWN_COLUMN，
非法参数（如 limit 非 1..1000 的整数或 null）返回 INVALID_INPUT，data=null、abstained=true。
仅输出符合 JSON Schema 的最终响应，不能读取未来问题或参考答案。
"""


def response_schema():
    nullable_text = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    row = object_schema({"group": nullable_text, "value": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                         "count": {"type": "integer", "minimum": 0}})
    data = object_schema({"dataset_id": {"type": "string"}, "operation": {"type": "string"},
                          "column": nullable_text, "group_by": nullable_text,
                          "results": {"type": "array", "items": row}})
    return object_schema({"answer": {"type": "string", "minLength": 1},
        "data": {"anyOf": [data, {"type": "null"}]}, "sources": {"type": "array", "items": object_schema({}), "maxItems": 0},
        "abstained": {"type": "boolean"}, "error": {"anyOf": [object_schema({"code": {"type": "string"}, "message": {"type": "string"}}), {"type": "null"}]}})


def direct_context(suite, case, resources):
    # Deliberate allowlist: no gold, oracle query or future cases enter the model.
    return {"request": suite["request"], "input": copy.deepcopy(case["input"]),
            "defaults": copy.deepcopy(suite["defaults"]), "resources": copy.deepcopy(resources)}


def verify_gold(suite, resources):
    req = Requirements(suite["request"], "csv_analysis", suite["scenario"], ["Complete data"],
                       suite["defaults"] | {"dataset_id": resources["datasets"][0]["id"]})
    for case in suite["cases"]:
        try:
            value = csv_oracle(req, resources, case["oracle_query"])
        except OracleError as exc:
            if case.get("expected_error") != exc.code:
                raise ValueError("Frozen error gold mismatch: " + case["id"]) from exc
        else:
            expected = {"dataset_id": resources["datasets"][0]["id"], **case["gold"]}
            if not same_value(value, expected):
                raise ValueError("Frozen numeric gold mismatch: " + case["id"])


def score(case, output, resources):
    issues = []
    if output is None:
        return {"passed": False, "issues": ["未执行；保留在交付分母内。"]}
    try:
        validate_response({k: output[k] for k in ("answer", "data", "sources", "abstained", "error")}, resources)
    except (ExecutionError, KeyError, TypeError) as exc:
        issues.append("响应契约：" + str(exc))
    if output.get("sources"):
        issues.append("CSV sources 必须为空。")
    if "expected_error" in case:
        if ((output.get("error") or {}).get("code") != case["expected_error"]
                or output.get("data") is not None or output.get("abstained") is not True):
            issues.append("没有按共同契约拒绝非法输入。")
    else:
        expected = {"dataset_id": resources["datasets"][0]["id"], **case["gold"]}
        if output.get("error") or output.get("abstained") or not same_value(output.get("data"), expected):
            issues.append("数值、行数、筛选、排序或结果元数据与冻结答案不一致。")
    return {"passed": not issues, "issues": issues}


def audit_metrics(paths):
    records = [read_json(p) for directory in paths for p in Path(directory).glob("*.json")]
    return {"model_calls": len(records), "statuses": dict(Counter(r["status"] for r in records)),
            "error_categories": dict(Counter(r.get("error_category", "unknown") for r in records if r["status"] == "failed")),
            "model_elapsed_ms": round(sum(r.get("elapsed_ms", 0) for r in records), 2),
            "unique_call_ids": len({r["call_id"] for r in records}) == len(records)}


def summarize(arm):
    cases = arm["cases"]
    completed = [c for c in cases if c.get("actual") is not None]
    computations = [c for c in cases if c["category"] != "invalid_input"]
    errors = [c for c in cases if c["category"] == "invalid_input"]
    times = [c["elapsed_ms"] for c in completed]
    return {"passed": sum(c.get("passed", False) for c in cases), "total": len(cases),
            "executed": len(completed), "computation_passed": sum(c.get("passed", False) for c in computations),
            "computation_total": len(computations), "invalid_input_passed": sum(c.get("passed", False) for c in errors),
            "invalid_input_total": len(errors), "median_case_ms": round(statistics.median(times), 2) if times else None,
            "case_wall_sum_ms": round(sum(times), 2),
            "runtime_calls": None if any(c.get("new_model_calls", 0) is None for c in completed) else sum(c.get("new_model_calls", 0) for c in cases)}


def run_comparison(root, suite_path, model=None, timeout=600, build_seconds=3600, case_seconds=600, max_repairs=3):
    suite = read_json(suite_path)
    resources = prepare_resources(uploads=suite["files"])
    verify_gold(suite, resources)
    eid = uuid.uuid4().hex[:12]
    folder = Path(root).resolve() / ".agentforge" / "comparisons" / eid
    workspace = folder / "factory-workspace"
    factory = FactoryAgent(workspace, model=model, timeout=timeout)
    direct = CodexClient(folder, model=model, timeout=timeout)
    direct.audit_dir = folder / "direct-calls"
    assert direct.configuration() == factory.planner.client.configuration()
    skeleton = [{"id": c["id"], "category": c["category"], "status": "planned", "actual": None} for c in suite["cases"]]
    report = {"comparison_id": eid, "scenario": suite["scenario"], "status": "running",
        "started_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "suite_hash": fingerprint(suite),
        "agentforge_version": __version__, "runtime_hash": factory.runtime_hash,
        "configuration": direct.configuration(), "limits": {"build_seconds": build_seconds, "case_seconds": case_seconds, "max_repairs": max_repairs},
        "protocol": {"direct": "One fresh Codex complete request per question; no tools, generated agent, repair or feedback.",
                     "full": "One fresh AgentForge build with generated tools, independent development/acceptance, automatic repairs and export; frozen agent then answers the same questions.",
                     "arm_scheduling": "Both arms overlap; cases within each arm execute sequentially. Internal factory test concurrency follows the project defaults.",
                     "scoring": "Both arms use identical frozen numeric and response assertions; custom-tool execution is diagnostic only, not a baseline scoring prerequisite.",
                     "startup": "Full build time/calls reported separately; no cold-start cost is hidden. No gold or future questions in generation/repair.",
                     "fresh_attempts": 1, "external_retries": 0},
        "limitations": ["One synthetic ledger and one fresh build, not a general success-rate estimate or a causal performance proof.",
                        "Same CLI/default model configuration; CLI does not expose resolved backend model identity.",
                        "Concurrent arms and network variation can affect latency; repeated independent runs are needed.",
                        "This baseline is a direct structured model response without computational tools, not all possible Codex CLI workflows.",
                        "Numeric data/contract assertions do not prove every sentence of the natural-language answer correct."],
        "arms": {"direct": {"status": "queued", "cases": copy.deepcopy(skeleton)},
                 "full": {"status": "queued", "cases": copy.deepcopy(skeleton), "build": None}}, "report_path": str(folder / "report.json")}
    lock = threading.Lock()
    started = time.perf_counter()
    write_json(folder / "suite.json", suite)
    write_json(folder / "resources.json", resources)
    def save(name=None, arm=None):
        with lock:
            if name is not None:
                report["arms"][name] = copy.deepcopy(arm)
            report["wall_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            write_json(folder / "report.json", report)
    save()
    print("comparison=" + eid + " report=" + report["report_path"], flush=True)
    def evaluate_arm(name):
        arm = copy.deepcopy(report["arms"][name])
        arm_started = time.perf_counter()
        arm["status"] = "building" if name == "full" else "evaluating"
        save(name, arm)
        state = None
        if name == "full":
            build_started = time.perf_counter()
            state = factory.create(suite["request"], uploads=copy.deepcopy(suite["files"]), review=False,
                                   max_repairs=max_repairs, max_seconds=build_seconds, run_max_seconds=case_seconds)
            arm["run_id"] = state["run_id"]
            save(name, arm)
            state = factory.drive(state)
            arm["build"] = {"status": state["status"], "phase": state["phase"], "version": state["version"],
                "elapsed_ms": round((time.perf_counter() - build_started) * 1000, 2),
                "repairs": state["repairs"], "design_repairs": state["design_repairs"],
                "development": state.get("report"), "acceptance": state.get("acceptance_report"),
                "export_path": state.get("export_path"), "archive_path": state.get("archive_path"),
                "last_error": state.get("last_error"), "error_info": state.get("error_info"),
                "call_audit": factory.call_metrics(state["run_id"])}
            save(name, arm)
        arm["status"] = "evaluating"
        for i, case in enumerate(suite["cases"]):
            row = arm["cases"][i]
            if name == "full" and state["status"] != "completed":
                row.update(status="unexecuted", **score(case, None, resources))
                save(name, arm)
                continue
            tick = time.perf_counter()
            row["status"] = "running"
            save(name, arm)
            try:
                if name == "direct":
                    direct.audit_context = {"comparison_id": eid, "arm": name, "case_id": case["id"]}
                    direct.deadline = time.monotonic() + case_seconds if case_seconds else None
                    direct.deadline_scope = "runtime"
                    output = direct.complete(DIRECT_TASK, direct_context(suite, case, resources), response_schema())
                    new_calls = 1
                else:
                    output = factory.run(state["run_id"], copy.deepcopy(case["input"]), expected_artifact=state["artifact_hash"],
                        max_seconds=case_seconds, audit_context={"comparison_id": eid, "stage": "comparison", "arm": name, "case_id": case["id"]})
                    new_calls = output.get("new_model_calls", output.get("model_calls", 0))
            except (CodexError, ExecutionError) as exc:
                code = "TASK_TIMEOUT" if isinstance(exc, DeadlineExceeded) else "MODEL_TIMEOUT" if isinstance(exc, ModelTimeout) else exc.code if isinstance(exc, ExecutionError) else "MODEL_ERROR"
                output = error_response(code, str(exc))
                output["failure"] = getattr(exc, "failure", None)
                new_calls = 1 if name == "direct" else None
            row.update(status="completed", actual=output, elapsed_ms=round((time.perf_counter() - tick) * 1000, 2),
                       new_model_calls=new_calls, **score(case, output, resources))
            row["custom_tool_executed"] = any(o.get("status") == "ok" and o.get("tool", "").startswith("custom.") for o in output.get("observations", []))
            save(name, arm)
            print(name + "/" + case["id"] + " " + ("PASS" if row["passed"] else "FAIL"), flush=True)
            failure = output.get("model_failure") or output.get("failure") or {}
            if failure.get("category") in ("authentication", "authorization", "rate_limit", "model_unavailable"):
                arm["blocked_by_model_service"] = failure
                for remaining, other in zip(arm["cases"][i + 1:], suite["cases"][i + 1:]):
                    remaining.update(status="unexecuted", **score(other, None, resources))
                break
        arm["summary"] = summarize(arm)
        arm["elapsed_ms"] = round((time.perf_counter() - arm_started) * 1000, 2)
        arm["call_audit"] = audit_metrics([direct.audit_dir if name == "direct" else factory.run_dir(state["run_id"]) / "calls"])
        arm["delivered_all_cases"] = arm["summary"]["passed"] == len(suite["cases"]) and (name == "direct" or state["status"] == "completed")
        arm["status"] = "completed"
        save(name, arm)
    def guarded(name):
        try:
            evaluate_arm(name)
        except Exception as exc:
            arm = copy.deepcopy(report["arms"][name])
            arm["exception"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            for row, case in zip(arm["cases"], suite["cases"]):
                if row["status"] != "completed":
                    row.update(status="unexecuted", **score(case, None, resources))
            arm["summary"] = summarize(arm)
            arm["status"] = "interrupted"
            save(name, arm)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(guarded, ("direct", "full")))
    report["status"] = "completed"
    report["finished_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "experiments/csv_comparison_v1.json")
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--build-seconds", type=int, default=3600)
    parser.add_argument("--case-seconds", type=int, default=600)
    parser.add_argument("--max-repairs", type=int, default=3)
    args = parser.parse_args()
    report = run_comparison(args.workspace, args.suite, args.model, args.timeout, args.build_seconds, args.case_seconds, args.max_repairs)
    print(json.dumps({"comparison_id": report["comparison_id"], "arms": {n: a["summary"] for n, a in report["arms"].items()},
                      "report_path": report["report_path"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
