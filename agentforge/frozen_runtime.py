"""Load only the verified runtime of a completed export under its own namespace."""

import hashlib
import importlib
import sys
import types

from .resources import read_json, fingerprint


def frozen_agent(factory, state):
    target = factory.root / "dist" / state["run_id"]
    version = factory.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"]))
    original = read_json(version / "runtime_manifest.json")
    if fingerprint(original) != state["runtime_hash"]:
        raise ValueError("历史运行时指纹不一致。")
    if state["status"] == "completed" and (target / "manifest.json").is_file():
        manifest = read_json(target / "manifest.json")
        if manifest["artifact_hash"] != state["artifact_hash"] or manifest.get("runtime_hash") != state.get("runtime_hash"):
            raise ValueError("历史导出包与当前版本不一致。")
        for name, expected in manifest["files"].items():
            path = (target / name).resolve()
            if not path.is_relative_to(target.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("历史导出包文件完整性检查失败：" + name)
        runtime_dir = target / "agentforge"
    else:
        runtime_dir = version / "runtime"
        if not runtime_dir.is_dir():
            raise ValueError("运行时已升级；历史任务没有运行时源码快照，需要 upgrade-runtime 后重新验收。")
    for name, expected in original.items():
        path = runtime_dir / name
        # Packaging helpers may be recorded but are not execution imports.
        if name == "adaptive_artifacts.py" and runtime_dir == target / "agentforge":
            continue
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("历史执行源码与冻结运行时不一致。")
        if (state["schema_version"] == 4 or name in ("contracts.py", "resources.py", "program.py", "workflow.py", "tool_registry.py", "testing.py")) and not path.is_file():
            raise ValueError("历史导出包缺少运行时模块。")
    namespace = "_agentforge_frozen_" + state["run_id"] + "_" + state["runtime_hash"][:12]
    if namespace not in sys.modules:
        package = types.ModuleType(namespace)
        package.__path__ = [str(runtime_dir)]
        sys.modules[namespace] = package
    module = lambda name: importlib.import_module(namespace + "." + name)
    req = module("contracts").Requirements(**state["requirements"])
    if state["schema_version"] == 4:
        spec = module("adaptive_contracts").AdaptiveSpec(**state["spec"])
        engine = state["engine"]
        client_factory = factory.runtime_client_factory or (lambda: module("codex_client").CodexClient(factory.root, engine["model"], engine["timeout"]))
        return module("adaptive_runtime").AdaptiveAgent(spec, req, factory.resources(state), factory.source(state), factory.root, client_factory)
    spec = module("contracts").AgentSpec.from_dict(state["spec"])
    return module("workflow").WorkflowAgent(spec, req, factory.resources(state), factory.source(state))
