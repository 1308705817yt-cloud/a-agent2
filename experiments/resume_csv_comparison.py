"""Run a paired supplement for unfinished cases without altering original evidence."""

import argparse
import copy
import hashlib
import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.run_csv_comparison import (CodexClient, CodexError, DeadlineExceeded, ExecutionError,
    FactoryAgent, ModelTimeout, DIRECT_TASK, audit_metrics, direct_context, error_response,
    fingerprint, read_json, response_schema, score, summarize, verify_gold, write_json)


class SupplementFactory(FactoryAgent):
    def configure_audit(self, client, state, stage):
        super().configure_audit(client, state, stage)
        client.audit_dir = self.supplement_folder / "full-calls"


def run_supplement(comparison_id):
    original_folder = ROOT / ".agentforge/comparisons" / comparison_id
    original_path = original_folder / "report.json"
    original_bytes = original_path.read_bytes()
    original = json.loads(original_bytes)
    full = original["arms"]["full"]
    if original.get("excluded_from_valid_comparison") or (full.get("build") or {}).get("status") != "completed":
        raise ValueError("A valid completed build is required")
    suite, resources = read_json(original_folder / "suite.json"), read_json(original_folder / "resources.json")
    if fingerprint(suite) != original["suite_hash"]:
        raise ValueError("Frozen suite changed")
    verify_gold(suite, resources)
    identity = read_json(original_folder / "experiment-identity.json")
    for name, expected in identity["source_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen comparison source changed: " + name)
    selected_ids = {c["id"] for c in full["cases"] if not c.get("passed")}
    cases = [c for c in suite["cases"] if c["id"] in selected_ids]
    if not cases:
        raise ValueError("No unfinished cases")
    sid = uuid.uuid4().hex[:12]
    folder = original_folder / "continuations" / sid
    limits = original["limits"]
    factory = SupplementFactory(original_folder / "factory-workspace",
                                model=original["configuration"]["requested_model"] if original["configuration"]["requested_model"] != "cli_default" else None,
                                timeout=original["configuration"]["request_timeout_seconds"])
    factory.supplement_folder = folder
    state = factory.load(full["run_id"])
    if factory.runtime_hash != original["runtime_hash"]:
        raise ValueError("Runtime changed; use a separate experiment")
    configuration = factory.planner.client.configuration()
    if configuration != original["configuration"]:
        raise ValueError("Model configuration changed; use a separate experiment")
    archive = Path(full["build"]["archive_path"])
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    direct = CodexClient(folder, factory.planner.client.model, factory.planner.client.timeout)
    direct.audit_dir = folder / "direct-calls"
    skeleton = [{"id": c["id"], "category": c["category"], "status": "planned", "actual": None} for c in cases]
    report = {"continuation_id": sid, "comparison_id": comparison_id, "status": "running",
        "started_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "original_report_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "suite_hash": original["suite_hash"], "runtime_hash": factory.runtime_hash,
        "artifact_hash": state["artifact_hash"], "archive_sha256": archive_hash,
        "run_id": full["run_id"], "configuration": configuration, "limits": limits,
        "selected_cases": [c["id"] for c in cases],
        "historical_full_passes": [c["id"] for c in full["cases"] if c.get("passed")],
        "protocol": {"selection": "Only original full-arm failures/unexecuted cases; fixed before supplement calls.",
            "execution": "Fresh per-case executions in BOTH arms; no rebuild, source edits, human direction or scoring feedback.",
            "recovery": "Old failed model requests had no committed observations; retry with a fresh 600-second case budget, preserve old elapsed/calls/failures.",
            "scheduling": "Arms overlap; questions sequential within each arm.",
            "reporting": "Original attempt remains immutable. Supplement is a later service window, not a first-attempt 12/12 success."},
        "arms": {name: {"status": "queued", "cases": copy.deepcopy(skeleton)} for name in ("direct", "full")},
        "report_path": str(folder / "report.json")}
    lock, started = threading.Lock(), time.perf_counter()
    write_json(folder / "suite.json", suite)
    write_json(folder / "resources.json", resources)
    def save(name=None, arm=None):
        with lock:
            if name is not None:
                report["arms"][name] = copy.deepcopy(arm)
            report["wall_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            write_json(folder / "report.json", report)
    save()
    print("continuation=" + sid + " report=" + report["report_path"], flush=True)
    def evaluate(name):
        arm, tick_arm = copy.deepcopy(report["arms"][name]), time.perf_counter()
        arm["status"] = "evaluating"
        for i, case in enumerate(cases):
            row, tick = arm["cases"][i], time.perf_counter()
            row["status"] = "running"
            save(name, arm)
            context = {"comparison_id": comparison_id, "continuation_id": sid, "arm": name, "case_id": case["id"]}
            try:
                if name == "direct":
                    direct.audit_context = context
                    direct.deadline, direct.deadline_scope = time.monotonic() + limits["case_seconds"], "runtime"
                    output = direct.complete(DIRECT_TASK, direct_context(suite, case, resources), response_schema())
                    calls = 1
                else:
                    output = factory.run(full["run_id"], copy.deepcopy(case["input"]),
                        expected_artifact=state["artifact_hash"], max_seconds=limits["case_seconds"],
                        audit_context=context | {"stage": "comparison_continuation"})
                    calls = output.get("new_model_calls", output.get("model_calls", 0))
            except (CodexError, ExecutionError) as exc:
                code = "TASK_TIMEOUT" if isinstance(exc, DeadlineExceeded) else "MODEL_TIMEOUT" if isinstance(exc, ModelTimeout) else exc.code if isinstance(exc, ExecutionError) else "MODEL_ERROR"
                output, calls = error_response(code, str(exc)), 1 if name == "direct" else None
                output["failure"] = getattr(exc, "failure", None)
            row.update(status="completed", actual=output, elapsed_ms=round((time.perf_counter() - tick) * 1000, 2),
                       new_model_calls=calls, **score(case, output, resources))
            row["execution_conditions"] = {"max_seconds": limits["case_seconds"], "configuration": configuration,
                "mode": "fresh_retry" if name == "full" and next(c for c in full["cases"] if c["id"] == case["id"]).get("actual") else "fresh_execution"}
            row["custom_tool_executed"] = any(o.get("status") == "ok" and o.get("tool", "").startswith("custom.") for o in output.get("observations", []))
            save(name, arm)
            print(name + "/" + case["id"] + " " + ("PASS" if row["passed"] else "FAIL"), flush=True)
            failure = output.get("model_failure") or output.get("failure") or {}
            if failure.get("category") in ("authentication", "authorization", "rate_limit", "model_unavailable"):
                arm["blocked_by_model_service"] = failure
                for remaining, other in zip(arm["cases"][i + 1:], cases[i + 1:]):
                    remaining.update(status="unexecuted", **score(other, None, resources))
                break
        arm.update(status="completed", summary=summarize(arm), elapsed_ms=round((time.perf_counter() - tick_arm) * 1000, 2),
                   call_audit=audit_metrics([folder / (name + "-calls")]))
        save(name, arm)
    def guarded(name):
        try:
            evaluate(name)
        except Exception as exc:
            arm = copy.deepcopy(report["arms"][name])
            arm["exception"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            for row, case in zip(arm["cases"], cases):
                if row["status"] != "completed":
                    row.update(status="unexecuted", **score(case, None, resources))
            arm.update(status="interrupted", summary=summarize(arm))
            save(name, arm)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(guarded, ("direct", "full")))
    report["immutable_checks"] = {"original_report": original_path.read_bytes() == original_bytes,
                                  "archive": hashlib.sha256(archive.read_bytes()).hexdigest() == archive_hash}
    report.update(status="completed", finished_at=datetime.now(timezone(timedelta(hours=8))).isoformat())
    save()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison_id")
    result = run_supplement(parser.parse_args().comparison_id)
    print(json.dumps({"continuation_id": result["continuation_id"], "arms": {
        name: arm["summary"] for name, arm in result["arms"].items()}}, ensure_ascii=False))
