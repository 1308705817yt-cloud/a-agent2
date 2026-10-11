"""Build reproducible standalone artifacts from the exact tested version."""

import hashlib
import zipfile
from pathlib import Path

from .resources import write_json


RUNNER = '''import argparse
import json
from pathlib import Path
from agentforge.contracts import AgentSpec, Requirements
from agentforge.resources import read_json
from agentforge.workflow import WorkflowAgent

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description="Generated AgentForge agent")
parser.add_argument("question", nargs="?")
parser.add_argument("--input-json", help="CSV parameters or document input as JSON")
args = parser.parse_args()
requirements = Requirements(**read_json(root / "requirements.json"))
spec = AgentSpec.from_dict(read_json(root / "agent.json"))
agent = WorkflowAgent(spec, requirements, read_json(root / "resources.json"), (root / "logic.py").read_text(encoding="utf-8"))
inputs = json.loads(args.input_json) if args.input_json else (args.question or {})
print(json.dumps(agent.run(inputs), ensure_ascii=False, indent=2))
'''


VERIFIER = '''import hashlib
import json
from pathlib import Path
from agentforge.contracts import AgentSpec, Requirements
from agentforge.resources import artifact_fingerprint, fingerprint, read_json
from agentforge.testing import evaluate
from agentforge.workflow import WorkflowAgent

root = Path(__file__).resolve().parent
manifest = read_json(root / "manifest.json")
for name, expected in manifest["files"].items():
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit("File integrity mismatch: " + name)
for name, expected in manifest["frozen_hashes"].items():
    if fingerprint(read_json(root / (name + ".json"))) != expected:
        raise SystemExit("Frozen contract mismatch: " + name)
spec = AgentSpec.from_dict(read_json(root / "agent.json"))
source = (root / "logic.py").read_text(encoding="utf-8")
if artifact_fingerprint(spec.to_dict(), source, manifest.get("runtime_hash")) != manifest["artifact_hash"]:
    raise SystemExit("Artifact fingerprint mismatch")
req = Requirements(**read_json(root / "requirements.json"))
resources = read_json(root / "resources.json")
suite = read_json(root / "test_suite.json")
agent = WorkflowAgent(spec, req, resources, source)
reports = [evaluate(agent, req, resources, suite, part, manifest["artifact_hash"]) for part in ("development", "acceptance")]
print(json.dumps([{k:r[k] for k in ("partition","passed","total","all_passed")} for r in reports], ensure_ascii=False, indent=2))
raise SystemExit(0 if all(r["all_passed"] for r in reports) else 1)
'''


def export_artifact(root, state, resources, suite, source, runtime_files=None):
    adaptive = state.get("schema_version") == 4
    target = root / "dist" / state["run_id"]
    target.mkdir(parents=True, exist_ok=True)
    package = target / "agentforge"
    package.mkdir(exist_ok=True)
    package.joinpath("__init__.py").write_text('', encoding="utf-8")
    origin = Path(__file__).resolve().parent
    names = ("contracts.py", "resources.py", "tool_registry.py", "program.py", "workflow.py", "testing.py")
    if adaptive:
        names += ("codex_client.py", "adaptive_contracts.py", "custom_tools.py", "adaptive_runtime.py", "adaptive_testing.py", "trusted_tools.py", "source_validation.py", "evaluation_progress.py")
    for name in names:
        (package / name).write_bytes(runtime_files[name] if runtime_files else (origin / name).read_bytes())
    for name, value in (("agent.json", state["spec"]), ("requirements.json", state["requirements"]),
                        ("resources.json", resources), ("test_suite.json", suite),
                        ("evaluation.json", {"development": state["report"], "acceptance": state["acceptance_report"]})):
        write_json(target / name, value)
    (target / "logic.py").write_text(source, encoding="utf-8")
    if adaptive:
        from .adaptive_artifacts import RUNNER as runner, VERIFIER as verifier, instructions
        write_json(target / "engine.json", state["engine"])
        from .source_validation import preflight_output_hook
        from .contracts import Requirements
        write_json(target / "source_preflight.json", preflight_output_hook(source, Requirements(**state["requirements"]), resources))
        from .factory import FactoryAgent
        write_json(target / "tool_contracts.json", FactoryAgent.tool_contracts(state["spec"]))
        if state.get("active_design"):
            from .resources import read_json
            write_json(target / "business_suite.json", read_json(root / "runs" / state["run_id"] / "test_suite.json"))
            write_json(target / "design_history.json", {"design_repairs": state.get("design_repairs", 0),
                       "changes": state["versions"], "business_scenarios_preserved": True})
        if "trusted_tools" in state["frozen_hashes"]:
            from .resources import read_json
            write_json(target / "trusted_tools.json", read_json(root / "runs" / state["run_id"] / "trusted_tools.json"))
        (target / "custom_tools").mkdir(exist_ok=True)
        for tool in state["spec"]["custom_tools"]:
            (target / "custom_tools" / (tool["id"].split(".")[1] + ".py")).write_text(tool["source"], encoding="utf-8")
    else:
        runner, verifier = RUNNER, VERIFIER
    (target / "run_agent.py").write_text(runner, encoding="utf-8")
    (target / "verify_agent.py").write_text(verifier, encoding="utf-8")
    (target / "README.md").write_text(
        "# " + state["spec"]["name"] + "\n\n" + state["requirements"]["summary"] + "\n\n"
        "需要 Python 3.11+，仅使用标准库，运行无需 Codex。\n\n"
        "文档问答：`python -X utf8 run_agent.py \"你的问题\"`\n\n"
        "CSV：`python -X utf8 run_agent.py --input-json '{}'`，可覆盖 operation/column/group_by/filters/sort_by/descending/limit。\n\n"
        "验证完整性及重新验收：`python -X utf8 verify_agent.py`。\n\n"
        "logic.py 由受约束 Python 解释器执行；不是任意 Python 插件。文档使用主题词路由/词项检索和原文摘取。"
        "测试通过表示此资源快照与测试范围内验收通过。资源可能包含用户原文。\n", encoding="utf-8")
    if adaptive:
        (target / "README.md").write_text(instructions(state), encoding="utf-8")
    files = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(target.rglob("*")) if p.is_file() and p.name != "manifest.json" and "__pycache__" not in p.parts}
    frozen = {k: v for k, v in state["frozen_hashes"].items() if "/" not in k}
    if state.get("active_design"):
        from .resources import fingerprint
        frozen.update(test_suite=fingerprint(suite), tool_contracts=fingerprint(FactoryAgent.tool_contracts(state["spec"])),
                      business_suite=state["frozen_hashes"]["test_suite"])
    write_json(target / "manifest.json", {"format_version": 4 if adaptive else 3, "run_id": state["run_id"], "version": state["version"],
               "artifact_hash": state["artifact_hash"], "runtime_hash": state.get("runtime_hash"),
               "frozen_hashes": frozen, "files": files})
    archive = root / "dist" / (state["run_id"] + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in [*files, "manifest.json"]:
            bundle.write(target / name, arcname=state["run_id"] + "/" + name)
    return str(target), str(archive)
