"""Run full unittest discovery and save actual route evidence plus source fingerprints.

Usage: python -X utf8 -B tests/run_phase1_validation.py
No live model calls. The suite uses temporary resources and local HTTP servers.
"""

import argparse
import collections
import contextlib
import datetime
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import sys
import time
import unittest


NEW_MODULES = {"test_data_contract", "test_metric_query", "test_sql_query", "test_data_binding",
               "test_resource_profiles", "test_query_routing", "test_zero_custom_lifecycle"}


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.modules = collections.Counter()
        self.routes = []

    def startTest(self, test):
        super().startTest(test)
        self.modules[test.__class__.__module__] += 1

    def addSuccess(self, test):
        super().addSuccess(test)
        if hasattr(test, "route_evidence"):
            self.routes.append(test.route_evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=".agentforge/validation/phase1-architecture-results.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    log = root / ".agentforge/validation/phase1-full.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # Freeze the manifest before discovery/import so the report identifies the
    # implementation that started this run, rather than later edits.
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for directory in ("agentforge", "tests") for p in sorted((root / directory).glob("*.py"))}
    started = time.perf_counter()
    with log.open("w", encoding="utf-8") as stream, contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=EvidenceResult).run(suite)
    report = {
        "phase": "phase_1_architecture_validation", "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "environment": {"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version},
        "command": "python -X utf8 -B tests/run_phase1_validation.py --report " + args.report,
        "scope": "Scripted model responses; real host modules, SQLite, local HTTP, frontend, factory and exported subprocesses. No live Codex calls or model quality claims.",
        "tests": result.testsRun, "old_tests": sum(n for m, n in result.modules.items() if m not in NEW_MODULES),
        "new_tests": sum(n for m, n in result.modules.items() if m in NEW_MODULES),
        "elapsed_seconds": round(time.perf_counter() - started, 3), "successful": result.wasSuccessful(),
        "failures": [{"test": str(t), "traceback": error} for t, error in result.failures],
        "errors": [{"test": str(t), "traceback": error} for t, error in result.errors],
        "skipped": [{"test": str(t), "reason": reason} for t, reason in result.skipped],
        "modules": dict(sorted(result.modules.items())), "routes": result.routes, "source_hashes": hashes}
    output = root / args.report
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("tests", "old_tests", "new_tests", "elapsed_seconds", "successful", "failures", "errors", "skipped")}, ensure_ascii=False))
    print("Report:", output)
    print("Log:", log)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
