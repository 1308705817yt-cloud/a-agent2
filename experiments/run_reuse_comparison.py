"""Real, tool-enabled Codex / reusable script / data program / Agent lifetime comparison.

Scripts execute in a network-disabled Docker container, never as a host Python subprocess.
The image must already exist locally (--pull never). No real model calls in unit tests.
"""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentforge.codex_client import CodexClient, object_schema
from agentforge.factory import FactoryAgent
from agentforge.lifecycle_cost import summarize_lifecycle
from agentforge.resources import fingerprint, prepare_resources, read_json, write_json

SCHEMA = object_schema({"value": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                        "row_count": {"type": "integer", "minimum": 0}})


def model_calls(path):
    return len(list(Path(path).glob("*.json")))


def snapshot(epoch, content):
    return prepare_resources(uploads=[{"name": "data.csv", "content": content}], data_contract=epoch["data_contract"])


def verify_gold(suite):
    """Independent Decimal oracle; never given to any builder or execution backend."""
    if suite.get("format") != "agentforge-reuse-comparison-v1" or not suite.get("epochs"):
        raise ValueError("Unsupported reuse comparison suite")
    ids = set()
    for epoch in suite["epochs"]:
        if epoch["id"] in ids or not epoch["cases"]:
            raise ValueError("Epoch IDs must be unique with nonempty held-out cases")
        ids.add(epoch["id"])
        for case in epoch["cases"]:
            resources = snapshot(epoch, case["csv"])
            rows = [r for r in resources["datasets"][0]["rows"] if r["region"] == case["region"]]
            value = sum((Decimal(r[epoch["column"]]) for r in rows if r[epoch["column"]] not in (None, "")), Decimal(0))
            if value != Decimal(str(case["gold"]["value"])) or len(rows) != case["gold"]["row_count"]:
                raise ValueError("Frozen gold mismatch: " + case["id"])


def matches(actual, gold):
    return (isinstance(actual, dict) and set(actual) == {"value", "row_count"}
            and type(actual["value"]) in (int, float) and type(actual["row_count"]) is int
            and Decimal(str(actual["value"])) == Decimal(str(gold["value"])) and actual["row_count"] == gold["row_count"])


def normalize_result(result):
    if result.get("error"):
        raise ValueError(result["error"]["message"])
    data = result["data"]
    if (data.get("operation") != "sum" or data.get("group_by") is not None or len(data["results"]) != 1):
        raise ValueError("Expected one ungrouped sum with row count")
    return {"value": data["results"][0]["value"], "row_count": data["results"][0]["count"]}


class DockerScriptRunner:
    def __init__(self, image):
        # Fail before spending model calls if the local isolated executor is unavailable.
        check = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                               capture_output=True, text=True, timeout=20)
        if check.returncode:
            raise ValueError("Reuse script baseline requires an existing local Docker Python image: " + image)
        self.image = check.stdout.strip()

    def run(self, script, payload, timeout):
        name = "agentforge-eval-" + uuid.uuid4().hex[:16]
        command = ["docker", "run", "--name", name, "--rm", "--pull", "never", "--network", "none", "--read-only",
                   "--memory", "256m", "--cpus", "1", "--pids-limit", "64", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "-i",
                   "--mount", "type=bind,source=" + str(script.resolve()) + ",target=/work/solution.py,readonly",
                   self.image, "python", "-I", "-B", "/work/solution.py"]
        try:
            result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                                    text=True, encoding="utf-8", timeout=timeout)
            if result.returncode or len(result.stdout) > 100000:
                raise ValueError("Script execution failed: " + result.stderr[:1000])
            return json.loads(result.stdout, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite number")))
        finally:
            # Covers timeouts too; remove only this invocation's uniquely named container.
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def run_comparison(root, suite, runner, model=None, timeout=180, client_factory=CodexClient, factory_type=FactoryAgent):
    verify_gold(suite)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    report = {"format": "agentforge-reuse-results-v1", "suite_hash": fingerprint(suite),
              "scope": "Synthetic paired lifetime comparison; no general quality or pricing claim.",
              "script_executor": {"type": "docker", "image_id": runner.image},
              "arms": {a: {"builds": [], "cases": [], "human_minutes": None} for a in ("direct_codex", "codex_script", "program", "agent")}}
    write_json(root / "suite.json", suite)
    previous_script = None
    for index, epoch in enumerate(suite["epochs"]):
        initial_resources = snapshot(epoch, epoch["training_csv"])
        for arm, records in report["arms"].items():
            work = root / arm / epoch["id"]
            calls = work / "calls"
            client = client_factory(work, model=model, timeout=timeout)
            client.audit_dir = calls
            client.audit_context = {"arm": arm, "epoch": epoch["id"], "stage": "build"}
            state, factory, script, build_error = None, None, None, None
            before, started = model_calls(calls), time.perf_counter()
            if arm != "direct_codex":
                try:
                    if arm == "codex_script":
                        context = {"request": epoch["request"], "resources": initial_resources,
                                   "previous_source": previous_script,
                                   "protocol": "stdin JSON {resources: normalized resource snapshot, input: {region: string}}; stdout JSON {value: number, row_count: integer}"}
                        client.solve("Create outputs/solution.py: an ordinary reusable Python standard-library program. "
                                     "Implement task.json request and protocol. It must read each invocation's stdin data, "
                                     "not hard-code training rows or answers. No model, framework or network dependency. "
                                     "Run local checks on training data, fix errors, and return a short build summary. "
                                     "If previous_source is present, adapt it to the revised contract.", context,
                                     object_schema({"summary": {"type": "string"}}), work / "script")
                        script = work / "script/solution.py"
                        previous_script = script.read_text(encoding="utf-8")
                    else:
                        factory = factory_type(work, model=model, timeout=timeout)
                        state = factory.drive(factory.create(epoch["request"],
                            uploads=[{"name": "data.csv", "content": epoch["training_csv"]}],
                            data_contract=epoch["data_contract"], delivery_mode=arm, reusable=True,
                            max_seconds=1800, run_max_seconds=timeout))
                        if state["status"] != "completed":
                            raise ValueError("Build not delivered: " + state["status"])
                except Exception as exc:
                    build_error = str(exc)[:1000]
                records["builds"].append({"epoch": epoch["id"], "phase": "initial" if index == 0 else "maintenance",
                    "strategy": "new_build" if arm != "codex_script" else "generate_or_adapt_script",
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "model_calls": factory.call_metrics(state["run_id"])["model_calls"] if factory and state else model_calls(calls) - before,
                    "error": build_error, "script_hash": fingerprint(previous_script) if script and script.is_file() else None,
                    "run_id": state["run_id"] if state else None})
            for case in epoch["cases"]:
                actual, error, raw = None, build_error, None
                audit = factory.run_dir(state["run_id"]) / "calls" if factory and state else calls
                before, started = model_calls(audit), time.perf_counter()
                if not build_error:
                    try:
                        resources = snapshot(epoch, case["csv"])
                        if arm == "direct_codex":
                            client.audit_context = {"arm": arm, "epoch": epoch["id"], "case": case["id"], "stage": "execution"}
                            actual = client.solve(epoch["request"] + " Return value and row_count for input.region.",
                                {"request": epoch["request"], "resources": resources, "input": {"region": case["region"]}},
                                SCHEMA, work / "evidence" / case["id"])
                        elif arm == "codex_script":
                            actual = runner.run(script, {"resources": resources, "input": {"region": case["region"]}}, timeout)
                        else:
                            path = work / (case["id"] + ".csv")
                            path.write_text(case["csv"], encoding="utf-8")
                            executor = factory.execution_agent(state)
                            rid = initial_resources["datasets"][0]["id"]
                            binding = executor.prepare_data_binding({rid: str(path)})
                            inputs = {"region": case["region"]} if arm == "program" else {
                                "operation": "sum", "column": epoch["column"], "group_by": None,
                                "filters": [{"column": "region", "operator": "eq", "value": case["region"]}]}
                            raw = factory.run(state["run_id"], inputs, data_binding=binding, max_seconds=timeout)
                            actual = normalize_result(raw)
                    except Exception as exc:
                        error = str(exc)[:1000]
                records["cases"].append({"id": case["id"], "epoch": epoch["id"], "actual": actual, "raw": raw,
                    "error": error, "passed": error is None and matches(actual, case["gold"]),
                    "elapsed_ms": (time.perf_counter() - started) * 1000, "model_calls": model_calls(audit) - before})
                records["summary"] = summarize_lifecycle(records)
                write_json(root / "results.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "experiments/reuse_comparison_v1.json")
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--image", default="python:3.11-slim")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    suite = read_json(args.suite)
    verify_gold(suite)
    runner = DockerScriptRunner(args.image)
    target = args.workspace / ".agentforge/reuse-comparisons" / uuid.uuid4().hex[:12]
    report = run_comparison(target, suite, runner, args.model, args.timeout)
    print(json.dumps({a: r["summary"] for a, r in report["arms"].items()}, ensure_ascii=False, indent=2))
    print("Report:", target / "results.json")
    return 0 if all(r["summary"]["passed"] == r["summary"]["total_cases"] for r in report["arms"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
