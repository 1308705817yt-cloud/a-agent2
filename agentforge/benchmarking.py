"""External holdout tasks. Gold answers never enter factory generation or repair."""

import copy
import math
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .resources import read_json, write_json, fingerprint, prepare_resources
from .contracts import ExecutionError, error_response, canonical_csv_parameters
from .codex_client import CodexError
from .testing import document_answer_issues


SCORING_CONTRACT = "v0.12-row-count-clause-evidence"


def load_suite(suite_path=None):
    suite = read_json(Path(suite_path) if suite_path else Path(__file__).parent / "benchmarks" / "unseen_v1.json")
    if not re.fullmatch(r"unseen-v\d+(?:\.\d+)?", str(suite.get("version", ""))) or not suite.get("tasks"):
        raise ValueError("独立评测集无效。")
    # Migrate representation in memory; frozen files and numeric gold never change.
    if "source_suite_hash" not in suite:
        suite["source_suite_hash"] = fingerprint(suite)
    suite.setdefault("contract_migrations", [])
    for task in suite["tasks"]:
        for case in task["cases"]:
            if task["domain"] == "csv_analysis" and case["gold"].get("operation") == "count" and case["gold"].get("column") is not None:
                suite["contract_migrations"].append({"task_id": task["id"], "case_id": case["id"],
                    "field": "gold.column", "before": case["gold"]["column"], "after": None, "reason": "count counts filtered rows"})
                case["gold"] = canonical_csv_parameters(case["gold"])
    suite["scoring_contract"] = SCORING_CONTRACT
    return suite


def benchmark_catalog(suite_path=None):
    suite = load_suite(suite_path)
    return [{"id": t["id"], "domain": t["domain"], "cases": len(t["cases"]), "suite_version": suite["version"]} for t in suite["tasks"]]


def same_value(actual, expected):
    if type(expected) in (float, int):
        return type(actual) in (float, int) and math.isfinite(actual) and math.isclose(actual, expected, abs_tol=1e-8, rel_tol=1e-10)
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(same_value(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(same_value(a, b) for a, b in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def score_case(task, case, output, resources, asked=False):
    issues = []
    gold = case["gold"]
    if output.get("error"):
        issues.append("运行错误：" + str(output["error"].get("code")))
    if "reply" in case and not asked:
        issues.append("含糊目标未先询问用户。")
    if task["domain"] == "csv_analysis":
        actual = output.get("data")
        expected = {"dataset_id": resources["datasets"][0]["id"], **canonical_csv_parameters(gold)}
        if output.get("abstained") or not same_value(actual, expected):
            issues.append("结果不符合人工核对的账本答案（检查筛选、空值、退款和完整行数）。")
    elif gold.get("refusal"):
        if not output.get("abstained") or output.get("sources"):
            issues.append("制度未载明的罚款应拒答且不附答案引用。")
    else:
        issues.extend(document_answer_issues(output, resources, gold["sources"], gold["facts"], gold.get("forbidden", [])))
    if not gold.get("refusal") and not any(o.get("status") == "ok" and str(o.get("tool", "")).startswith("custom.") for o in output.get("observations", [])):
        issues.append("未执行生成的业务工具。")
    return {"id": case["id"], "input": case["input"], "gold": gold, "asked_user": asked,
            "passed": not issues, "issues": issues, "actual": output}


def evaluate_task(factory, state, task, evaluate_generated=False):
    if state["status"] != "completed" and not (evaluate_generated and state.get("version") and state["status"] in ("stopped", "rejected", "failed")):
        return [{"id": c["id"], "passed": False, "issues": ["构建未交付：" + state["status"]], "actual": None} for c in task["cases"]]
    artifact = state["artifact_hash"]
    resources = factory.resources(state)
    if resources != prepare_resources(uploads=task["files"]) or state["request"] != task["request"]:
        raise ValueError("复用的 Agent 需求或资源与陌生任务不一致。")
    def execute(case):
        # Only this case's input is sent at runtime. No gold, future cases, or scoring rules.
        try:
            result = factory.run(state["run_id"], copy.deepcopy(case["input"]), expected_artifact=artifact,
                                 audit_context={"stage": "benchmark", "task_id": task["id"], "case_id": case["id"]})
        except (ValueError, OSError, ExecutionError, CodexError) as exc:
            result = error_response("BENCHMARK_EXECUTION", str(exc)[:1000])
        asked = result.get("needs_input", False)
        if "reply" in case and asked and result.get("checkpoint"):
            result = factory.run(state["run_id"], copy.deepcopy(case["input"]), expected_artifact=artifact,
                                 checkpoint=result["checkpoint"], reply=copy.deepcopy(case["reply"]),
                                 audit_context={"stage": "benchmark", "task_id": task["id"], "case_id": case["id"]})
        result.pop("checkpoint", None)
        return score_case(task, case, result, resources, asked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        cases = list(executor.map(execute, task["cases"]))
    if factory.load(state["run_id"])["artifact_hash"] != artifact:
        raise ValueError("评测期间 Agent 版本发生变化。")
    return cases


def run_benchmark(factory, task_ids=None, run_id=None, max_repairs=1, evaluate_generated=False, suite_path=None,
                  task_workers=1, max_seconds=None, run_max_seconds=None, cohort="public_regression"):
    import threading
    from .factory import FactoryAgent
    suite = load_suite(suite_path)
    if cohort not in ("public_regression", "historical_regression", "novel"):
        raise ValueError("评测分组无效。")
    selected = task_ids or [t["id"] for t in suite["tasks"]]
    if len(set(selected)) != len(selected) or set(selected) - {t["id"] for t in suite["tasks"]} or (run_id and len(selected) != 1):
        raise ValueError("任务 ID 无效、重复，或复用构建时未指定唯一任务。")
    if type(task_workers) is not int or not 1 <= task_workers <= 4:
        raise ValueError("任务并行数必须在 1 至 4 之间。")
    eid = uuid.uuid4().hex[:12]
    path = factory.root / "evaluations" / eid / "report.json"
    measured = run_id is None and not evaluate_generated
    report = {"evaluation_id": eid, "suite_version": suite["version"], "suite_hash": fingerprint(suite),
              "source_suite_hash": suite["source_suite_hash"], "scoring_contract": SCORING_CONTRACT,
              "contract_migrations": copy.deepcopy(suite["contract_migrations"]),
              "cohort": cohort,
              "authorship": suite["authorship"], "started_at": time.time(), "status": "running",
              "tasks": [{"task_id": tid, "status": "planned"} for tid in selected], "evaluate_generated": evaluate_generated,
              "fresh_builds": measured, "task_workers": task_workers,
              "metric_definition": {"first_delivery": "一次全新自动构建（允许既定自动修复，无人工重试或复用）交付并通过独立任务的全部问题；所有选定任务进入分母。",
                                    "first_candidate": "上述条件且代码版本为 v1、修复次数为 0。",
                                    "calls": "本地审计记录的 complete 请求次数，包含失败及未提交请求；不以阶段事件条数代替。",
                                    "time": "wall_elapsed_ms 是真实墙钟耗时；active_elapsed_ms 是构建与独立运行累计活跃耗时，包含并行用例的耗时之和。"},
              "limitations": "公开且固定的陌生任务回归集；只验证指定事实、引用及数值，不能证明任意业务能力。结果禁止反馈到本轮生成或修复。"}
    write_json(path, report)
    write_json(path.parent / "suite.json", suite)
    lock = threading.Lock()
    def persist(entry):
        with lock:
            report["tasks"][selected.index(entry["task_id"])] = copy.deepcopy(entry)
            write_json(path, report)
    def metrics(runner, rid):
        value = runner.call_metrics(rid)
        return value if isinstance(value, dict) else None
    def audit_breakdown(runner, rid):
        from collections import Counter
        records = [read_json(p) for p in (runner.run_dir(rid) / "calls").glob("*.json")]
        counts = Counter((r["context"].get("stage", "unknown"), r["context"].get("phase", "unknown"),
                          r["context"].get("partition", "none")) for r in records)
        return [{"stage": stage, "phase": phase, "partition": part, "calls": count}
                for (stage, phase, part), count in sorted(counts.items())]
    def execute(tid):
        task = next(t for t in suite["tasks"] if t["id"] == tid)
        # Each concurrent build owns a planner, deadline and lease; no shared factory state.
        runner = factory if task_workers == 1 else FactoryAgent(factory.root, model=factory.planner.client.model,
                                                               timeout=factory.planner.client.timeout)
        entry = {"task_id": tid, "status": "building", "run_id": None}
        started = time.perf_counter()
        state = None
        try:
            if run_id:
                state = runner.load(run_id)
            else:
                options = {"uploads": copy.deepcopy(task["files"]), "max_repairs": max_repairs}
                if max_seconds is not None:
                    options["max_seconds"] = max_seconds
                if run_max_seconds is not None:
                    options["run_max_seconds"] = run_max_seconds
                state = runner.create(task["request"], **options)
                entry["run_id"] = state["run_id"]
                persist(entry)
                state = runner.drive(state)
            entry.update(run_id=state["run_id"], version=state["version"], artifact_hash=state["artifact_hash"],
                         build_status=state["status"], eligible_for_delivery=state["status"] == "completed", status="evaluating")
            build_calls = metrics(runner, state["run_id"])
            persist(entry)
            cases = evaluate_task(runner, state, task, evaluate_generated)
        except Exception as exc:
            # Preserve the complete denominator and keep unaffected tasks running.
            entry["exception"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            if entry["run_id"]:
                state = runner.load(entry["run_id"])
            entry.update(build_status=(state or {}).get("status", "failed"), eligible_for_delivery=False)
            build_calls = metrics(runner, entry["run_id"]) if entry["run_id"] else None
            cases = [{"id": c["id"], "passed": False, "issues": ["构建或评测中断：" + type(exc).__name__], "actual": None} for c in task["cases"]]
        state = state or {}
        total_calls = metrics(runner, entry["run_id"]) if entry["run_id"] else None
        active_build = state.get("performance", {}).get("elapsed_ms")
        runtime_time = round(sum((c.get("actual") or {}).get("elapsed_ms", 0) for c in cases), 2)
        entry.update(status="completed", cases=cases, passed=sum(c["passed"] for c in cases), total=len(cases))
        entry["performance"] = {"build_elapsed_ms": active_build, "runtime_elapsed_ms": runtime_time,
                                "active_elapsed_ms": active_build + runtime_time if active_build is not None else None,
                                "wall_elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                                "repairs": state.get("repairs", 0), "design_repairs": state.get("design_repairs", 0),
                                "runtime_model_calls": sum((c.get("actual") or {}).get("model_calls", 0) for c in cases),
                                "build_call_audit": build_calls, "total_call_audit": total_calls}
        if entry["run_id"]:
            entry["performance"]["calls_by_phase"] = audit_breakdown(runner, entry["run_id"])
            entry["human_interventions"] = {"operator_directions": len(state.get("human_directions", [])),
                                             "heldout_requested_replies": sum(c.get("asked_user", False) for c in cases),
                                             "heldout_predeclared_replies": sum("reply" in c for c in task["cases"])}
            progress = [read_json(p) for p in (runner.run_dir(entry["run_id"]) / "versions").glob("v*/*_case_progress.json")]
            attempts = [a for record in progress for a in record.get("attempts", [])]
            entry["recovery"] = {"evaluation_attempts": len(attempts), "reused_cases": sum(len(a.get("reused", [])) for a in attempts),
                "resumed_cases": sum(len(a.get("resumed", [])) for a in attempts),
                "historical_recorded_calls_reused": sum(e.get("recorded_model_calls", 0) for a in attempts for e in a.get("reused_details", [])),
                "avoided_decision_reexecution": sum(e.get("committed_decisions", 0) for a in attempts for e in a.get("reused_details", []))
                    + sum(e.get("reused_committed_decisions", 0) for a in attempts for e in a.get("executed_details", [])),
                "measurement": "No natural interruption means zero observed recovery use. Avoided decisions estimate re-execution savings; no counterfactual full rerun was measured."}
        entry["first_delivery_passed"] = measured and entry["eligible_for_delivery"] and entry["passed"] == entry["total"] and entry["total"] > 0
        entry["first_candidate_passed"] = entry["first_delivery_passed"] and state.get("repairs", 0) == 0 and state.get("version") == 1
        persist(entry)
        if entry["run_id"]:
            write_json(runner.run_dir(entry["run_id"]) / "benchmarks" / (eid + ".json"),
                       {"evaluation_id": eid, "suite_version": suite["version"], "suite_hash": report["suite_hash"], **entry})
        return entry
    with ThreadPoolExecutor(max_workers=task_workers) as executor:
        list(executor.map(execute, selected))
    report.update(status="completed", finished_at=time.time(), report_path=str(path),
                  passed=sum(t["passed"] for t in report["tasks"]), total=sum(t["total"] for t in report["tasks"]))
    report["delivered_tasks"] = sum(t["eligible_for_delivery"] for t in report["tasks"])
    report["all_passed"] = report["passed"] == report["total"] and report["total"] > 0 and report["delivered_tasks"] == len(report["tasks"])
    totals = [t["performance"]["total_call_audit"] for t in report["tasks"]]
    report["metrics"] = {"task_count": len(selected), "first_delivery_successes": sum(t["first_delivery_passed"] for t in report["tasks"]) if measured else None,
                         "first_delivery_rate": sum(t["first_delivery_passed"] for t in report["tasks"]) / len(selected) if measured else None,
                         "first_candidate_successes": sum(t["first_candidate_passed"] for t in report["tasks"]) if measured else None,
                         "repairs": sum(t["performance"]["repairs"] for t in report["tasks"]),
                         "design_repairs": sum(t["performance"]["design_repairs"] for t in report["tasks"]),
                         "wall_elapsed_ms": round((report["finished_at"] - report["started_at"]) * 1000, 2),
                         "model_calls": sum(m["model_calls"] for m in totals) if all(m is not None for m in totals) else None}
    write_json(path, report)
    return report


def rescore_benchmark(factory, evaluation_id):
    from .storage import valid_id
    original = read_json(factory.root / "evaluations" / valid_id(evaluation_id) / "report.json")
    if original["status"] != "completed":
        raise ValueError("只能重新判分已完成的评测。")
    snapshot = factory.root / "evaluations" / evaluation_id / "suite.json"
    suite = load_suite(snapshot) if snapshot.exists() else load_suite()
    eid = uuid.uuid4().hex[:12]
    path = factory.root / "evaluations" / eid / "report.json"
    report = {"evaluation_id": eid, "suite_version": suite["version"], "suite_hash": fingerprint(suite),
              "source_suite_hash": suite["source_suite_hash"], "scoring_contract": SCORING_CONTRACT,
              "contract_migrations": copy.deepcopy(suite["contract_migrations"]), "original_suite_hash": original["suite_hash"],
              "authorship": suite["authorship"], "status": "completed", "started_at": time.time(),
              "rescored_from": evaluation_id, "mode": "replay_scoring", "model_calls": 0, "tasks": [], "fresh_builds": False,
              "metrics": {"first_delivery_rate": None, "first_delivery_successes": None, "first_candidate_successes": None, "model_calls": 0},
              "limitations": "复用旧的真实回答修正判分；不是新盲测，不改变交付状态。"}
    for old in original["tasks"]:
        task = next((t for t in suite["tasks"] if t["id"] == old["task_id"]), None)
        if task is None or len(old["cases"]) != len(task["cases"]):
            raise ValueError("评测任务或问题集合已经变化，不能只重新判分。")
        state = factory.load(old["run_id"])
        resources = factory.resources(state)
        if state["artifact_hash"] != old["artifact_hash"] or resources != prepare_resources(uploads=task["files"]) or state["request"] != task["request"]:
            raise ValueError("版本、需求或资源发生变化，不能复用回答判分。")
        entry = {k: copy.deepcopy(v) for k, v in old.items() if k not in ("cases", "passed", "total")}
        entry["eligible_for_delivery"] = old.get("eligible_for_delivery", old["build_status"] == "completed")
        for key in ("first_delivery_passed", "first_candidate_passed"):
            if key in entry:
                entry["original_" + key] = entry.pop(key)
        entry["cases"] = []
        for saved, case in zip(old["cases"], task["cases"]):
            if saved["id"] != case["id"] or (saved.get("input") is not None and saved["input"] != case["input"]) or (saved.get("gold") is not None and saved["gold"] != case["gold"]):
                raise ValueError("问题或标准答案发生变化，不能以判分规则修订的名义复用。")
            entry["cases"].append(score_case(task, case, copy.deepcopy(saved["actual"]), resources, saved.get("asked_user", False)) if saved.get("actual") is not None else copy.deepcopy(saved))
        entry.update(passed=sum(c["passed"] for c in entry["cases"]), total=len(entry["cases"]))
        report["tasks"].append(entry)
        write_json(factory.run_dir(old["run_id"]) / "benchmarks" / (eid + ".json"), {"evaluation_id": eid, "suite_version": suite["version"], "rescored_from": evaluation_id, **entry})
    report.update(finished_at=time.time(), report_path=str(path), passed=sum(t["passed"] for t in report["tasks"]),
                  total=sum(t["total"] for t in report["tasks"]), delivered_tasks=sum(t["eligible_for_delivery"] for t in report["tasks"]))
    report["all_passed"] = report["passed"] == report["total"] and report["total"] > 0 and report["delivered_tasks"] == len(report["tasks"])
    write_json(path, report)
    return report
