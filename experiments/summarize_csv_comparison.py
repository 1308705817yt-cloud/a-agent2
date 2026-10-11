"""Export portable evidence from a completed comparison; never invokes a model."""

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentforge.resources import fingerprint, read_json, write_json


def call_summary(directory):
    records = [read_json(p) for p in sorted(directory.glob("*.json"))]
    if len({r["call_id"] for r in records}) != len(records):
        raise ValueError("Duplicate call IDs")
    phases = Counter((r.get("context", {}).get("stage", "direct"),
                      r.get("context", {}).get("phase", "questions")) for r in records)
    usage = Counter()
    for record in records:
        usage.update({k: v for k, v in (record.get("usage") or {}).items()
                      if isinstance(v, (int, float))})
    return {"requests": len(records), "statuses": dict(Counter(r["status"] for r in records)),
            "by_stage": [{"stage": s, "phase": p, "requests": n}
                         for (s, p), n in sorted(phases.items())],
            "elapsed_known_requests": sum(r.get("elapsed_ms") is not None for r in records),
            "model_elapsed_sum_ms": round(sum(r.get("elapsed_ms") or 0 for r in records), 2),
            "usage_known_requests": sum(r.get("usage") is not None for r in records),
            "usage": dict(usage), "call_ids": [r["call_id"] for r in records],
            "failures": [{k: r.get(k) for k in ("call_id", "error_code", "error_category", "exit_code", "classification_basis", "scope", "elapsed_ms")} |
                         {"context": r.get("context")} for r in records if r["status"] == "failed"]}


def summarize(folder):
    folder = Path(folder).resolve()
    report = read_json(folder / "report.json")
    if report["status"] != "completed" or report.get("excluded_from_valid_comparison"):
        raise ValueError("Only a completed, valid comparison may be published")
    suite = read_json(folder / "suite.json")
    if report["suite_hash"] != fingerprint(suite):
        raise ValueError("Frozen suite fingerprint changed")
    identity = read_json(folder / "experiment-identity.json")
    for name, expected in identity["source_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Experiment source changed: " + name)
    full = report["arms"]["full"]
    run = folder / "factory-workspace" / "runs" / full["run_id"]
    rejected = read_json(run / "rejected_designs.json") if (run / "rejected_designs.json").exists() else []
    rejected_tests = read_json(run / "rejected_test_attempts.json") if (run / "rejected_test_attempts.json").exists() else []
    result = {"comparison_id": report["comparison_id"], "scenario": report["scenario"],
              "started_at": report["started_at"], "finished_at": report["finished_at"],
              "suite_hash": report["suite_hash"], "runtime_hash": report["runtime_hash"],
              "agentforge_version": report["agentforge_version"], "identity": identity,
              "configuration": report["configuration"], "limits": report["limits"],
              "protocol": report["protocol"], "limitations": report["limitations"],
              "wall_elapsed_ms": report["wall_elapsed_ms"],
              "raw_report": str((folder / "report.json").relative_to(ROOT)), "arms": {}}
    build = full.get("build") or {}
    artifact_path = run / "versions" / ("v" + str(build.get("version"))) / "agent.json"
    artifact = read_json(artifact_path) if artifact_path.exists() else {}
    contradiction = next((c for c in (build.get("development") or {}).get("cases", [])
                          if c["id"] == "dev-mean-positive-limit"), None)
    limit_instruction = next((line for line in artifact.get("instructions", "").splitlines()
                              if "limit=0 表示合法" in line), None)
    result["diagnostics"] = {
        "generated_limit_instruction": limit_instruction,
        "contradictory_test_description": ({k: contradiction.get(k) for k in (
            "id", "purpose", "criterion", "input", "expected", "passed")} if contradiction else None),
        "interpretation": ("Generated limit=0 instruction/criterion conflicts with host contract; expected INVALID_INPUT passes, not a successful computation. No source changes made using held-out evidence."
                           if limit_instruction and contradiction else None)}
    if (folder / "artifact-local-verification.json").exists():
        result["artifact_local_verification"] = read_json(folder / "artifact-local-verification.json")
    for name, arm in report["arms"].items():
        directory = folder / "direct-calls" if name == "direct" else run / "calls"
        calls = call_summary(directory)
        cases = [{k: c.get(k) for k in ("id", "category", "status", "passed", "issues", "elapsed_ms",
                                      "new_model_calls", "custom_tool_executed")} |
                 {"actual_data": (c.get("actual") or {}).get("data"),
                  "actual_error": (c.get("actual") or {}).get("error")} for c in arm["cases"]]
        numerical_times = [c["elapsed_ms"] for c in cases
                           if c["category"] != "invalid_input" and c["elapsed_ms"] is not None]
        result["arms"][name] = {"status": arm["status"], "delivered_all_cases": arm.get("delivered_all_cases"),
                               "blocked_by_model_service": arm.get("blocked_by_model_service"), "exception": arm.get("exception"),
                               "elapsed_ms": arm.get("elapsed_ms"), "summary": arm["summary"],
                               "computation_median_ms": round(statistics.median(numerical_times), 2) if numerical_times else None,
                               "calls": calls, "cases": cases}
        if name == "full":
            build = arm.get("build") or {}
            result["arms"][name]["build"] = {k: build.get(k) for k in (
                "status", "phase", "version", "elapsed_ms", "repairs", "design_repairs", "call_audit",
                "export_path", "archive_path", "last_error", "error_info")}
            for key in ("export_path", "archive_path"):
                if build.get(key):
                    result["arms"][name]["build"][key] = str(Path(build[key]).relative_to(ROOT))
            archive = Path(build["archive_path"]) if build.get("archive_path") else None
            result["arms"][name]["build"]["archive_sha256"] = (
                hashlib.sha256(archive.read_bytes()).hexdigest() if archive and archive.is_file() else None)
            for partition in ("development", "acceptance"):
                checks = build.get(partition) or {}
                result["arms"][name]["build"][partition] = {k: checks.get(k) for k in (
                    "passed", "total", "all_passed", "new_model_calls", "reused_cases", "tool_coverage")}
            result["arms"][name]["rejected_design_candidates"] = rejected
            result["arms"][name]["rejected_test_candidates"] = rejected_tests
    return result


def cumulative_full(original, supplement):
    """Keep first-attempt metrics separate from latest per-case coverage."""
    first = original["arms"]["full"]
    later = supplement["arms"]["full"]
    replacements = {c["id"]: c for c in later["cases"]}
    cases = []
    for case in first["cases"]:
        reused = bool(case.get("passed"))
        current = case if reused else replacements.get(case["id"], case)
        cases.append({"id": case["id"], "category": case["category"], "passed": bool(current.get("passed")),
                      "result_origin": "original_success" if reused else "supplement_execution",
                      "original_status": case["status"], "original_passed": bool(case.get("passed")),
                      "latest_status": current["status"]})
    return {"first_attempt": dict(first["summary"]),
            "covered_passed": sum(c["passed"] for c in cases), "total": len(cases),
            "historical_successes_retained": sum(c["result_origin"] == "original_success" for c in cases),
            "fresh_supplement_cases": len(later["cases"]),
            "fresh_retries": sum(c["id"] in replacements and c.get("actual") is not None and not c.get("passed") for c in first["cases"]),
            "runtime_calls_all_attempts": (None if any(a["summary"]["runtime_calls"] is None for a in (first, later))
                                           else first["summary"]["runtime_calls"] + later["summary"]["runtime_calls"]),
            "runtime_wall_sum_all_attempts_ms": round(first["summary"]["case_wall_sum_ms"] + later["summary"]["case_wall_sum_ms"], 2),
            "cases": cases}


def attach_supplement(result, folder):
    folder = Path(folder).resolve()
    path = folder / "report.json"
    supplement = read_json(path)
    checks = supplement.get("immutable_checks", {})
    if supplement["status"] != "completed" or checks.get("original_report") is not True or checks.get("archive") is not True:
        raise ValueError("Supplement incomplete or original evidence changed")
    original_folder = ROOT / ".agentforge/comparisons" / result["comparison_id"]
    original_path = original_folder / "report.json"
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != supplement["original_report_sha256"]:
        raise ValueError("Original report changed")
    if any(result[k] != supplement[k] for k in ("suite_hash", "runtime_hash", "configuration", "limits")):
        raise ValueError("Supplement conditions differ")
    result["supplement"] = {k: supplement[k] for k in (
        "continuation_id", "started_at", "finished_at", "wall_elapsed_ms", "protocol", "selected_cases",
        "historical_full_passes", "immutable_checks", "artifact_hash", "archive_sha256", "configuration", "limits")}
    result["supplement"]["raw_report"] = str(path.relative_to(ROOT))
    if (folder / "supplement-identity.json").exists():
        result["supplement"]["identity"] = read_json(folder / "supplement-identity.json")
    result["supplement"]["arms"] = {}
    for name, arm in supplement["arms"].items():
        times = [c["elapsed_ms"] for c in arm["cases"] if c["category"] != "invalid_input" and c.get("elapsed_ms") is not None]
        result["supplement"]["arms"][name] = {"status": arm["status"], "summary": arm["summary"],
            "computation_median_ms": round(statistics.median(times), 2) if times else None,
            "blocked_by_model_service": arm.get("blocked_by_model_service"), "exception": arm.get("exception"),
            "calls": call_summary(folder / (name + "-calls")),
            "cases": [{k: c.get(k) for k in ("id", "status", "passed", "elapsed_ms", "new_model_calls", "execution_conditions", "issues")} |
                      {"actual_data": (c.get("actual") or {}).get("data"), "actual_error": (c.get("actual") or {}).get("error")}
                      for c in arm["cases"]]}
    result["cumulative_full"] = cumulative_full(read_json(original_path), supplement)
    runtime_calls = result["cumulative_full"]["runtime_calls_all_attempts"]
    result["cumulative_full"]["model_calls_including_build"] = (None if runtime_calls is None else
        result["arms"]["full"]["build"]["call_audit"]["model_calls"] + runtime_calls)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison_id")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/csv-comparison-v1.json")
    parser.add_argument("--continuation", help="Attach a separately recorded completed supplement")
    args = parser.parse_args()
    folder = ROOT / ".agentforge/comparisons" / args.comparison_id
    result = summarize(folder)
    if args.continuation:
        result = attach_supplement(result, folder / "continuations" / args.continuation)
    write_json(args.output, result)
    output = {"output": str(args.output), "first_attempt": {
        name: {"summary": arm["summary"], "calls": arm["calls"]["requests"]}
        for name, arm in result["arms"].items()}}
    if result.get("supplement"):
        output["supplement"] = {name: arm["summary"] for name, arm in result["supplement"]["arms"].items()}
        output["cumulative_full"] = {k: result["cumulative_full"][k] for k in (
            "covered_passed", "total", "historical_successes_retained", "fresh_retries", "model_calls_including_build")}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
