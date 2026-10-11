"""Run identical deterministic tests against old and current source in isolated processes."""

import argparse
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


WORKER = r'''
import hashlib, io, json, sys, unittest
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root), str(root / 'tests')]
import agentforge
assert Path(agentforge.__file__).resolve().is_relative_to(root / 'agentforge'), agentforge.__file__
class Results(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = {}
    def startTest(self, test):
        super().startTest(test)
        self.cases[test.id()] = {'id': test.id(), 'status': 'passed', 'problems': []}
    def record(self, test, kind, error, subtest=None):
        row = self.cases[test.id()]
        row['status'] = 'failed'
        row['problems'].append({'kind': kind, 'subtest': str(subtest) if subtest else None,
                                'exception': error[0].__name__, 'message': str(error[1])})
    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.record(test, 'failure', error)
    def addError(self, test, error):
        super().addError(test, error)
        self.record(test, 'error', error)
    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error:
            self.record(test, 'failure' if issubclass(error[0], test.failureException) else 'error', error, subtest)
suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_delivery_boundaries.py')
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Results).run(suite)
cases = list(result.cases.values())
manifest = {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / 'agentforge').rglob('*.py')) if '__pycache__' not in p.parts}
print(json.dumps({'package_root_verified': True, 'tests': result.testsRun,
    'passed': sum(c['status'] == 'passed' for c in cases), 'failed': sum(c['status'] == 'failed' for c in cases),
    'failure_assertions': len(result.failures), 'errors': len(result.errors), 'cases': cases,
    'source_manifest': manifest, 'log': stream.getvalue()}, ensure_ascii=False))
raise SystemExit(0 if result.wasSuccessful() else 1)
'''


def run(root):
    completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", WORKER, str(root)],
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
    if completed.returncode not in (0, 1):
        raise RuntimeError(completed.stderr)
    result = json.loads(completed.stdout)
    result["exit_code"] = completed.returncode
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    before = parser.add_mutually_exclusive_group(required=True)
    before.add_argument("--before-ref", help="Git revision of the original source")
    before.add_argument("--before-dir", type=Path, help="Saved original source snapshot")
    parser.add_argument("--output", type=Path, default=Path("docs/verification-v0.12.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agentforge-regression-") as td:
        baseline = Path(td)
        if args.before_dir:
            original = args.before_dir.resolve()
            for directory in ("agentforge", "tests"):
                shutil.copytree(original / directory, baseline / directory, ignore=shutil.ignore_patterns("__pycache__"))
            reference = str(args.before_dir)
        else:
            reference = subprocess.check_output(["git", "rev-parse", "--verify", args.before_ref + "^{commit}"],
                                                cwd=root, text=True).strip()
            archive = subprocess.check_output(["git", "archive", "--format=zip", reference, "agentforge", "tests"], cwd=root)
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                for member in bundle.infolist():
                    target = baseline / member.filename
                    if not target.resolve().is_relative_to(baseline):
                        raise ValueError("Archive path escapes baseline")
                    if not member.is_dir():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(bundle.read(member))
        # Only the new regression module is overlaid; all implementation and fixtures stay original.
        shutil.copyfile(root / "tests/test_delivery_boundaries.py", baseline / "tests/test_delivery_boundaries.py")
        old, new = run(baseline), run(root)
    import hashlib
    report = {"kind": "deterministic_before_after", "generated_at": datetime.now().astimezone().isoformat(),
              "before_reference": reference, "model_calls": 0, "real_delivery_evaluation": False,
              "test_sha256": hashlib.sha256((root / "tests/test_delivery_boundaries.py").read_bytes()).hexdigest(),
              "before": old, "after": new,
              "limitations": "Scripted protocol consumers verify host behavior, not real model interpretation or delivery gains. Clause scoring is bounded, not a semantic proof."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for label, value in (("before", old), ("after", new)):
        print(f"{label}: {value['passed']}/{value['tests']} passed; {value['failed']} failed methods; {value['failure_assertions']} failure assertions; {value['errors']} errors")
    print(args.output)
    if old["failed"] == 0 or old["errors"] or new["failed"] or old["tests"] != new["tests"]:
        raise SystemExit("Before/after proof failed")


if __name__ == "__main__":
    main()
