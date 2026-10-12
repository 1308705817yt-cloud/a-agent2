"""Answer bundles and independently runnable programs with frozen source and evidence."""

import hashlib
import importlib
import shutil
import sys
import types
import zipfile
from pathlib import Path

from .resources import fingerprint, read_json, write_json

RUNTIME_FILES = ("program_delivery.py", "resources.py", "contracts.py", "csv_contract.py", "csv_execution.py",
                 "tool_registry.py", "program.py", "metric_query.py", "data_contract.py", "data_binding.py",
                 "sql_query.py", "codex_client.py")

RUNNER = '''"""Run a frozen data program using Python 3.11+; no Codex installation needed."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Integrity check failed: " + name)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", default="{}")
    parser.add_argument("--bindings-json")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = verify(root)
    if args.verify:
        print(json.dumps({"integrity": "passed", "business_validation": manifest["business_validation"]}))
        return 0
    from agentforge.program_delivery import DataProgram
    from agentforge.resources import read_json
    program = DataProgram(read_json(root / "recipe.json"), read_json(root / "resources.json"))
    if args.bindings_json:
        program = program.with_data_binding(program.prepare_data_binding(json.loads(args.bindings_json)))
    result = program.run(json.loads(args.input_json))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if result["error"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))
'''


def verify_bundle(target, expected_hash=None):
    target = Path(target).resolve()
    manifest = read_json(target / "manifest.json")
    if expected_hash is not None and fingerprint(manifest) != expected_hash:
        raise ValueError("交付清单与已冻结版本不一致。")
    for name, expected in manifest["files"].items():
        file = (target / name).resolve()
        if not file.is_relative_to(target) or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError("交付文件完整性检查失败：" + name)
    return manifest


def export_delivery(factory, state):
    base = factory.run_dir(state["run_id"])
    target = base / "delivery"
    target.mkdir(exist_ok=True)
    kind = state["delivery"]["kind"]
    for name in ("resources", "delivery_request", "delivery_result", "delivery_validation"):
        write_json(target / (name + ".json"), read_json(base / (name + ".json")))
    if kind == "program":
        write_json(target / "recipe.json", read_json(base / "delivery_recipe.json"))
        runtime = target / "agentforge"
        runtime.mkdir(exist_ok=True)
        (runtime / "__init__.py").write_text("", encoding="utf-8")
        for name in RUNTIME_FILES:
            (runtime / name).write_bytes(factory.delivery_runtime_files[name])
        (target / "run_program.py").write_text(RUNNER, encoding="utf-8")
        instructions = "Run: python run_program.py --input-json '{}'\nVerify: python run_program.py --verify\n"
    else:
        instructions = "This is an answer and evidence bundle. It is not a reusable agent or an accepted program.\n"
        evidence = state.get("direct_output_dir")
        if evidence:
            shutil.copytree(base / evidence, target / "evidence", dirs_exist_ok=True)
    validation = read_json(base / "delivery_validation.json")
    (target / "README.txt").write_text(instructions + "Business validation: " + validation["scope"] +
                                      "\nNew data does not inherit previous business acceptance.\n", encoding="utf-8")
    manifest = {"format": "agentforge-delivery-v1", "kind": kind,
                "business_validation": validation["scope"], "resources_hash": state["frozen_hashes"]["resources"],
                "files": {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(target.rglob("*")) if p.is_file() and p.name != "manifest.json"}}
    write_json(target / "manifest.json", manifest)
    artifact_hash = fingerprint(manifest)
    verify_bundle(target, artifact_hash)
    exported = factory.root / "dist" / state["run_id"]
    shutil.copytree(target, exported, dirs_exist_ok=True)
    verify_bundle(exported, artifact_hash)
    archive = exported.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name in [*manifest["files"], "manifest.json"]:
            z.write(exported / name, name)
    state.update(version=1, artifact_hash=artifact_hash, export_path=str(exported), archive_path=str(archive))


def frozen_program(factory, state):
    target = factory.run_dir(state["run_id"]) / "delivery"
    verify_bundle(target, state["artifact_hash"])
    namespace = "_agentforge_program_" + state["artifact_hash"]
    if namespace not in sys.modules:
        package = types.ModuleType(namespace)
        package.__path__ = [str(target / "agentforge")]
        sys.modules[namespace] = package
    module = importlib.import_module(namespace + ".program_delivery")
    return module.DataProgram(read_json(target / "recipe.json"), read_json(target / "resources.json"))
