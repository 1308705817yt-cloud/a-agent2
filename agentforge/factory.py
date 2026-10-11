"""Persistent Codex agent factory: frozen contracts, generated workflows and code repair."""

import difflib
import time
import uuid
import threading
from pathlib import Path

from .artifacts import export_artifact
from .codex_client import CodexClient, CodexError, DeadlineExceeded, ModelTimeout
from .contracts import AgentSpec, DependencyError, ExecutionError, Requirements
from .adaptive_planner import AdaptivePlanner
from .adaptive_contracts import AdaptiveSpec
from .adaptive_runtime import AdaptiveAgent
from .adaptive_testing import evaluate_adaptive
from .resources import artifact_fingerprint, fingerprint, prepare_resources, read_json, write_json
from .storage import Store, valid_id
from .testing import evaluate
from .workflow import WorkflowAgent


class FactoryAgent:
    def __init__(self, root: Path, planner=None, model=None, timeout=180, runtime_client_factory=None):
        self.root = Path(root).resolve()
        self.store = Store(self.root)
        self.planner = planner or AdaptivePlanner(CodexClient(self.root, model, timeout))
        self.runtime_client_factory = runtime_client_factory
        origin = Path(__file__).parent
        self.runtime_files = {name: (origin / name).read_bytes() for name in (
            "contracts.py", "resources.py", "tool_registry.py", "program.py", "workflow.py", "testing.py",
            "codex_client.py", "adaptive_contracts.py", "custom_tools.py", "adaptive_runtime.py", "adaptive_testing.py", "adaptive_artifacts.py", "trusted_tools.py", "source_validation.py", "evaluation_progress.py")}
        import hashlib
        self.runtime_manifest = {name: hashlib.sha256(data).hexdigest() for name, data in self.runtime_files.items()}
        self.runtime_hash = fingerprint(self.runtime_manifest)

    def run_dir(self, run_id):
        return self.root / "runs" / valid_id(run_id)

    def save(self, state, owner=None):
        self.store.save(state, owner)
        write_json(self.run_dir(state["run_id"]) / "state.json", state)

    def create(self, request, documents=None, datasets=None, uploads=None, review=False, max_repairs=3,
               max_seconds=3600, run_max_seconds=900, adaptive_steps=True):
        if not isinstance(request, str) or not request.strip() or len(request) > 6000:
            raise ValueError("需求必须是 1 至 6000 个字符。")
        if type(max_repairs) is not int or not 0 <= max_repairs <= 20:
            raise ValueError("最大修复次数必须在 0 至 20 之间。")
        for seconds in (max_seconds, run_max_seconds):
            if type(seconds) not in (int, float) or not 0 <= seconds <= 86400:
                raise ValueError("总耗时应为 0 至 86400 秒；0 表示不限。")
        if type(adaptive_steps) is not bool:
            raise ValueError("adaptive_steps 必须是布尔值。")
        resources = prepare_resources(documents, datasets, uploads)
        client = self.planner.client
        state = {"run_id": uuid.uuid4().hex[:12], "schema_version": 4 if getattr(self.planner, "adaptive", False) else 3, "status": "queued",
                 "created_at": time.time(), "phase": "requirements", "request": request,
                 "engine": {"type": "codex-cli", "model": client.model, "timeout": client.timeout},
                 "review": bool(review), "max_repairs": max_repairs, "repairs": 0, "version": 0,
                 "requirements": None, "spec": None, "report": None, "acceptance_report": None,
                 "diagnosis": None, "human_directions": [], "reviewed_versions": [], "events": [],
                 "versions": [], "last_error": None, "stop_reason": None, "export_path": None,
                 "archive_path": None, "artifact_hash": None,
                 "performance": {"max_seconds": max_seconds, "run_max_seconds": run_max_seconds,
                                 "adaptive_steps": adaptive_steps, "elapsed_ms": 0},
                 "design_repairs": 0, "active_design": None, "pending_design": None,
                 "frozen_hashes": {"resources": fingerprint(resources)}}
        write_json(self.run_dir(state["run_id"]) / "resources.json", resources)
        if state["schema_version"] == 4:
            from .trusted_tools import load_definitions
            bindings = {"tools": load_definitions(self.root)}
            write_json(self.run_dir(state["run_id"]) / "trusted_tools.json", bindings)
            state["frozen_hashes"]["trusted_tools"] = fingerprint(bindings)
        self.save(state)
        return state

    def load(self, run_id):
        state = self.store.load(run_id)
        if state.get("schema_version") not in (3, 4):
            raise ValueError("任务版本不支持。")
        self.verify(state)
        return state

    def verify(self, state):
        base = self.run_dir(state["run_id"])
        for key, expected in state["frozen_hashes"].items():
            if fingerprint(read_json(base / (key + ".json"))) != expected:
                raise ValueError("冻结的 " + key + " 已被修改，拒绝继续。")
        if state["requirements"] is not None and fingerprint(state["requirements"]) != state["frozen_hashes"]["requirements"]:
            raise ValueError("存档需求与冻结契约不一致。")
        if state["version"]:
            version = base / "versions" / ("v" + str(state["version"]))
            source = (version / "logic.py").read_text(encoding="utf-8")
            spec = read_json(version / "agent.json")
            artifact_hash = artifact_fingerprint(spec, source, state.get("runtime_hash"))
            if state.get("runtime_hash"):
                manifest = read_json(version / "runtime_manifest.json")
                if fingerprint(manifest) != state["runtime_hash"]:
                    raise ValueError("版本运行时指纹不一致。")
            if state.get("schema_version") == 4:
                for tool in spec["custom_tools"]:
                    file = version / "custom_tools" / (tool["id"].split(".")[1] + ".py")
                    if file.read_text(encoding="utf-8") != tool["source"]:
                        raise ValueError("自定义工具源码与版本指纹不一致。")
                design = state.get("active_design")
                contract_path = base / (design["path"] if design else "") / "tool_contracts.json"
                if not self.same_tool_contracts(self.tool_contracts(spec), read_json(contract_path)):
                    raise ValueError("自定义工具定义与冻结契约不一致。")
            if spec != state["spec"] or artifact_hash != state["artifact_hash"]:
                raise ValueError("当前代码或工作流与版本指纹不一致。")
            for key in ("report", "acceptance_report"):
                report = state[key]
                if report and (report["artifact_hash"] != artifact_hash or report["suite_hash"] != self.suite_hash(state)):
                    raise ValueError("测试结果与当前版本不一致。")
        if state.get("active_design"):
            design = state["active_design"]
            if design["suite_hash"] != state["frozen_hashes"].get(design["path"] + "/test_suite"):
                raise ValueError("设计测试指纹不一致。")
            original = read_json(base / "test_suite.json")
            current = self.suite(state)
            if any(original.get(k, []) != current.get(k, []) for k in ("cases", "goal_cases")):
                raise ValueError("设计修复不能修改原业务开发及保留验收场景。")
        if state.get("pending_design"):
            pending = state["pending_design"]
            stored = read_json(base / pending["path"] / "tool_contracts.json")
            if not self.same_tool_contracts(self.tool_contracts(pending["spec"]), stored):
                raise ValueError("待实现的设计与冻结接口不一致。")
            if pending.get("suite_hash") and pending["suite_hash"] != state["frozen_hashes"].get(pending["path"] + "/test_suite"):
                raise ValueError("待实现的设计测试指纹不一致。")
        return state

    def source(self, state):
        return (self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / "logic.py").read_text(encoding="utf-8")

    @staticmethod
    def spec(value):
        return AdaptiveSpec(**value) if value.get("runtime") == "codex_loop" else AgentSpec.from_dict(value)

    @staticmethod
    def tool_contracts(spec):
        value = {"tools": spec["tools"], "custom_tools": [{k: v for k, v in t.items() if k != "source"} for t in spec["custom_tools"]]}
        if "trusted_tools" in spec:
            value["trusted_tools"] = spec["trusted_tools"]
        return value

    @staticmethod
    def same_tool_contracts(left, right):
        # v0.4 had no trusted_tools field; an empty capability list is equivalent.
        return {**left, "trusted_tools": left.get("trusted_tools", [])} == {**right, "trusted_tools": right.get("trusted_tools", [])}

    def agent(self, state):
        req, resources, spec = Requirements(**state["requirements"]), self.resources(state), self.spec(state["spec"])
        if state["schema_version"] == 4:
            engine = state["engine"]
            original_factory = self.runtime_client_factory or (lambda: CodexClient(self.root, engine["model"], engine["timeout"]))
            def client_factory():
                client = original_factory()
                self.configure_audit(client, state, "runtime")
                if getattr(self, "deadline", None) is not None:
                    client.deadline = self.deadline
                    client.deadline_scope = "build"
                return client
            return AdaptiveAgent(spec, req, resources, self.source(state), self.root, client_factory)
        return WorkflowAgent(spec, req, resources, self.source(state))

    def configure_audit(self, client, state, stage):
        client.audit_dir = self.run_dir(state["run_id"]) / "calls"
        client.audit_context = {"run_id": state["run_id"], "version": state["version"],
                                "stage": stage, "phase": state["phase"]}

    def call_metrics(self, run_id):
        records = [read_json(p) for p in (self.run_dir(run_id) / "calls").glob("*.json")]
        state = self.store.load(run_id)
        available = state.get("runtime_hash") in (None, self.runtime_hash)
        return {"available": available, "model_calls": len(records), "completed_calls": sum(r["status"] == "completed" for r in records),
                "failed_calls": sum(r["status"] == "failed" for r in records),
                "inflight_calls": sum(r["status"] == "inflight" for r in records),
                "model_elapsed_ms": round(sum(r.get("elapsed_ms", 0) for r in records), 2)}

    def evaluate(self, state, partition):
        agent = self.agent(state)
        evaluator = evaluate_adaptive if state["schema_version"] == 4 else evaluate
        options = {"max_seconds": state.get("performance", {}).get("run_max_seconds")} if state["schema_version"] == 4 else {}
        if state["schema_version"] == 4:
            options["progress_path"] = self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / (partition + "_case_progress.json")
        report = evaluator(agent, Requirements(**state["requirements"]), self.resources(state), self.suite(state), partition, state["artifact_hash"], **options)
        if getattr(self, "deadline", None) is not None and time.monotonic() >= self.deadline:
            write_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / (partition + "_time_limit.json"), report)
            raise DeadlineExceeded("构建总耗时已达到上限；当前测试阶段及已执行结果已保留。", "build")
        errors = [c["actual"]["error"] for c in report["cases"] if isinstance(c.get("actual"), dict) and isinstance(c["actual"].get("error"), dict)]
        if any(e["code"] == "TASK_TIMEOUT" for e in errors):
            write_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / (partition + "_time_limit.json"), report)
            raise DeadlineExceeded("子 Agent 达到运行时间上限；保留测试阶段，调整 --run-max-seconds 后重试。", "runtime")
        if any(e["code"] in ("MODEL_ERROR", "MODEL_TIMEOUT") for e in errors):
            write_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / (partition + "_interrupted.json"), report)
            codes = {e["code"] for e in errors if e["code"] in ("MODEL_ERROR", "MODEL_TIMEOUT")}
            error = ModelTimeout if codes == {"MODEL_TIMEOUT"} else CodexError
            raise error("子 Agent 的 Codex 调用中断；逐用例记录已保留，重试只执行中断或未完成用例。请按各用例分类依据恢复。",
                        failure={"category": "mixed_model_failures" if len(codes) > 1 else "model_batch_interrupted", "exit_code": None,
                                 "basis": {"source": "evaluation_cases", "error_codes": sorted(codes)}})
        dependencies = [c["actual"]["failure"] for c in report["cases"]
                        if not c["passed"] and isinstance(c.get("actual"), dict) and c["actual"].get("error")
                        and isinstance(c["actual"].get("failure"), dict)
                        and c["actual"]["failure"].get("origin") in ("external", "configuration")]
        if dependencies:
            write_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) /
                       (partition + "_dependency_blocked.json"), report)
            failure = dependencies[0]
            raise DependencyError(failure["code"], "可信依赖阻止验收；检查服务或运维配置后重试当前阶段。冻结的配置变更需新构建。",
                                  failure.get("location", ""), origin=failure["origin"], retryable=failure.get("retryable", False),
                                  recovery=failure.get("recovery"))
        return report

    def snapshot(self, state, spec, source, explanation, design=None):
        preflight = None
        if state["schema_version"] == 4:
            spec.validate(Requirements(**state["requirements"]), built=True)
            # Guard every path, including repair, redesign and runtime upgrades.
            from .source_validation import preflight_output_hook
            from .custom_tools import CustomTool
            resources = self.resources(state)
            for tool in spec.custom_tools:
                CustomTool(tool, resources)
            preflight = preflight_output_hook(source, Requirements(**state["requirements"]), resources)
        previous = self.source(state) if state["version"] else ""
        previous_spec = state["spec"]
        previous_version = state["version"]
        versions_dir = self.run_dir(state["run_id"]) / "versions"
        existing = [int(p.name[1:]) for p in versions_dir.glob("v*") if p.is_dir() and p.name[1:].isdigit()]
        version = max([previous_version, *existing]) + 1
        path = self.run_dir(state["run_id"]) / "versions" / ("v" + str(version))
        if path.exists():
            # A crash after file writes but before DB commit may leave an orphan version.
            if read_json(path / "agent.json") != spec.to_dict() or (path / "logic.py").read_text(encoding="utf-8") != source:
                raise ValueError("版本目录已存在且内容不同；为保留证据拒绝覆盖。")
        else:
            path.mkdir(parents=True)
            write_json(path / "agent.json", spec.to_dict())
            (path / "logic.py").write_text(source, encoding="utf-8")
            for tool in spec.to_dict().get("custom_tools", []):
                (path / "custom_tools").mkdir(exist_ok=True)
                (path / "custom_tools" / (tool["id"].split(".")[1] + ".py")).write_text(tool["source"], encoding="utf-8")
        write_json(path / "runtime_manifest.json", self.runtime_manifest)
        if preflight is not None:
            write_json(path / "source_preflight.json", preflight)
        runtime_dir = path / "runtime"
        runtime_dir.mkdir(exist_ok=True)
        for name, data in self.runtime_files.items():
            saved = runtime_dir / name
            if saved.exists() and saved.read_bytes() != data:
                raise ValueError("版本运行时快照已存在且内容不同，拒绝覆盖。")
            if not saved.exists():
                saved.write_bytes(data)
        state.update(version=version, spec=spec.to_dict(), runtime_hash=self.runtime_hash,
                     artifact_hash=artifact_fingerprint(spec.to_dict(), source, self.runtime_hash),
                     report=None, acceptance_report=None, diagnosis=None)
        if design is not None:
            state["active_design"] = design
        code_diff = "".join(difflib.unified_diff(previous.splitlines(True), source.splitlines(True),
                            fromfile="v" + str(previous_version) + "/logic.py", tofile="v" + str(version) + "/logic.py"))
        change = {"version": version, "explanation": explanation, "code_diff": code_diff,
                  "workflow_before": previous_spec, "workflow_after": spec.to_dict(), "artifact_hash": state["artifact_hash"]}
        change["runtime_after"] = self.runtime_hash
        change["design_repair"] = design is not None
        change["suite_hash"] = self.suite_hash(state)
        write_json(path / "test_suite.json", self.suite(state))
        if state["schema_version"] == 4:
            write_json(path / "tool_contracts.json", self.tool_contracts(spec.to_dict()))
        if previous_version:
            change["runtime_before"] = fingerprint(read_json(versions_dir / ("v" + str(previous_version)) / "runtime_manifest.json"))
        old_tools = {t["id"]: t["source"] for t in (previous_spec or {}).get("custom_tools", [])}
        change["tool_diffs"] = {t["id"]: "".join(difflib.unified_diff(old_tools.get(t["id"], "").splitlines(True), t["source"].splitlines(True),
                                  fromfile="v" + str(previous_version) + "/" + t["id"], tofile="v" + str(version) + "/" + t["id"]))
                                for t in spec.to_dict().get("custom_tools", [])}
        write_json(path / "change.json", change)
        state["versions"].append({"version": version, "artifact_hash": state["artifact_hash"], "explanation": explanation})

    def resources(self, state):
        return read_json(self.run_dir(state["run_id"]) / "resources.json")

    def suite(self, state):
        design = state.get("active_design")
        return read_json(self.run_dir(state["run_id"]) / (design["path"] if design else "") / "test_suite.json")

    @staticmethod
    def suite_hash(state):
        return state["active_design"]["suite_hash"] if state.get("active_design") else state["frozen_hashes"]["test_suite"]

    def failures(self, state):
        return [c for c in state["report"]["cases"] if not c["passed"]]

    def repair_stalled(self, state):
        """Compare the same checks, not a tool-only run with a full behavior run."""
        history = [v for v in state["versions"] if "passed" in v]
        if len(history) < 2:
            return False
        reports = [read_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(v["version"])) / "development.json")
                   for v in history[-2:]]
        before, after = reports
        if not before.get("suite_hash") or before["suite_hash"] != after.get("suite_hash"):
            return False
        ids_before = {c["id"] for c in before["cases"]}
        ids_after = {c["id"] for c in after["cases"]}
        if not ids_before or ids_before != ids_after or bool(before.get("behavior_skipped")) != bool(after.get("behavior_skipped")):
            return False
        return after["passed"] / max(1, after["total"]) <= before["passed"] / max(1, before["total"])

    def event(self, state, phase, message, error=False):
        metadata = dict(self.planner.client.last_metadata)
        if metadata.get("call_id") and metadata.get("context", {}).get("phase") != phase:
            metadata = {}
        state["events"].append({"time": time.time(), "phase": phase, "message": message, "error": error,
                                "codex": metadata})

    def drive(self, state):
        run_id, owner = state["run_id"], uuid.uuid4().hex
        self.store.acquire(run_id, owner)
        heartbeat_stop, heartbeat_errors = threading.Event(), []
        def heartbeat():
            while not heartbeat_stop.wait(30):
                try:
                    self.store.renew(run_id, owner)
                except (ValueError, OSError) as exc:
                    heartbeat_errors.append(str(exc))
                    return
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True, name="agentforge-build-lease")
        heartbeat_thread.start()
        try:
            state = self.load(run_id)
            if state.get("runtime_hash") and state["runtime_hash"] != self.runtime_hash:
                raise ValueError("运行时已升级；历史任务可查看或使用原导出包，请创建新构建。")
            if state["status"] not in ("queued", "running"):
                raise ValueError("该任务当前不能直接运行。")
            state["status"] = "running"
            performance = state.get("performance", {})
            max_seconds = performance.get("max_seconds", 0)
            remaining = max_seconds - performance.get("elapsed_ms", 0) / 1000 if max_seconds else None
            self.deadline = time.monotonic() + max(0, remaining) if remaining is not None else None
            self.planner.client.deadline = self.deadline
            self.planner.client.deadline_scope = "build"
            self.save(state, owner)
            while state["status"] == "running":
                self.verify(state)
                phase = state["phase"]
                print("[" + run_id + "] " + phase + "…", flush=True)
                started = time.perf_counter()
                try:
                    self.configure_audit(self.planner.client, state, "factory")
                    if heartbeat_errors:
                        raise ValueError(heartbeat_errors[-1])
                    if self.deadline is not None and time.monotonic() >= self.deadline:
                        raise DeadlineExceeded("构建已达到总耗时上限；可增加 --max-seconds 后重试当前阶段。", "build")
                    message = self.step(state)
                    self.event(state, phase, message)
                except DeadlineExceeded as exc:
                    state.update(status="time_limit", last_error=str(exc), error_info={"code": "TASK_TIMEOUT", "scope": exc.scope}, stop_reason="已保留当前阶段，等待调整时间后继续。")
                    self.event(state, phase, str(exc), True)
                except (CodexError, OSError, ValueError, ExecutionError) as exc:
                    state.update(status="failed", last_error=str(exc)[:1500])
                    state["error_info"] = exc.info() if isinstance(exc, ExecutionError) else {"code": "MODEL_TIMEOUT" if isinstance(exc, ModelTimeout) else "MODEL_ERROR" if isinstance(exc, CodexError) else "BUILD_ERROR"}
                    if isinstance(exc, CodexError):
                        state["error_info"]["model_failure"] = exc.failure
                    for name in ("design_attempts", "test_attempts", "code_attempts", "repair_attempts"):
                        if getattr(self.planner, name, None):
                            write_json(self.run_dir(run_id) / ("rejected_" + name + ".json"), getattr(self.planner, name))
                    self.event(state, phase, state["last_error"], True)
                if "performance" in state:
                    state["performance"]["elapsed_ms"] += round((time.perf_counter() - started) * 1000, 2)
                    state["performance"]["calls"] = self.call_metrics(run_id)
                self.save(state, owner)
            return state
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
            self.deadline = None
            self.planner.client.deadline = None
            self.store.release(run_id, owner)

    def step(self, state):
        phase = state["phase"]
        resources = self.resources(state)
        req = Requirements(**state["requirements"]) if state["requirements"] else None
        if phase == "requirements":
            if state["schema_version"] == 4:
                self.planner.trusted_tools = read_json(self.run_dir(state["run_id"]) / "trusted_tools.json")["tools"] if "trusted_tools" in state["frozen_hashes"] else []
            try:
                req = self.planner.requirements(state["request"], resources)
            except ValueError as exc:
                if str(exc).startswith("需求超出当前能力"):
                    state.update(status="unsupported", stop_reason=str(exc))
                    return str(exc)
                raise
            state["requirements"] = req.to_dict()
            write_json(self.run_dir(state["run_id"]) / "requirements.json", req.to_dict())
            state["frozen_hashes"]["requirements"] = fingerprint(req.to_dict())
            state["phase"] = "workflow"
            return req.summary
        if phase == "workflow":
            if state["schema_version"] == 4:
                self.planner.trusted_tools = read_json(self.run_dir(state["run_id"]) / "trusted_tools.json")["tools"] if "trusted_tools" in state["frozen_hashes"] else []
            state["spec"] = self.planner.workflow(req, resources).to_dict()
            if state["schema_version"] == 4:
                policy = state.get("performance", {})
                state["spec"]["execution_policy"] = {"adaptive_steps": policy.get("adaptive_steps", True),
                                                     "max_seconds": policy.get("run_max_seconds", 900)}
            if getattr(self.planner, "design_attempts", None):
                write_json(self.run_dir(state["run_id"]) / "rejected_designs.json", self.planner.design_attempts)
            if state["schema_version"] == 4:
                contracts = self.tool_contracts(state["spec"])
                write_json(self.run_dir(state["run_id"]) / "tool_contracts.json", contracts)
                state["frozen_hashes"]["tool_contracts"] = fingerprint(contracts)
            state["phase"] = "tests"
            return "已设计 Agent 指令、工具契约与决策预算。"
        if phase == "tests":
            suite = self.planner.tests(req, resources, self.spec(state["spec"])) if state["schema_version"] == 4 else self.planner.tests(req, resources)
            if getattr(self.planner, "test_attempts", None):
                write_json(self.run_dir(state["run_id"]) / "rejected_test_attempts.json", self.planner.test_attempts)
            write_json(self.run_dir(state["run_id"]) / "test_suite.json", suite)
            state["frozen_hashes"]["test_suite"] = fingerprint(suite)
            state["phase"] = "code"
            return "已冻结 " + str(sum(len(suite.get(k, [])) for k in ("cases", "tool_cases", "goal_cases"))) + " 条需求、工具及目标场景测试。"
        if phase == "code":
            suite = self.suite(state)
            development = [c for k in ("cases", "goal_cases") for c in suite.get(k, []) if c["partition"] == "development"]
            spec = self.spec(state["spec"])
            tool_dev = [c for c in suite.get("tool_cases", []) if c["partition"] == "development"]
            result = self.planner.code(req, spec, development, resources, tool_dev) if state["schema_version"] == 4 else self.planner.code(req, spec, development, resources)
            if getattr(self.planner, "code_attempts", None):
                write_json(self.run_dir(state["run_id"]) / "rejected_code_attempts.json", self.planner.code_attempts)
            self.snapshot(state, result.get("spec", spec), result["source"], result["explanation"])
            state["phase"] = "evaluate"
            return "已生成输出代码和自定义工具源码 v" + str(state["version"])
        if phase == "evaluate":
            self.check_runtime()
            try:
                report = self.evaluate(state, "development")
            except DependencyError:
                raise
            except (ExecutionError, ValueError, SyntaxError) as exc:
                report = {"partition": "development", "artifact_hash": state["artifact_hash"],
                          "suite_hash": self.suite_hash(state), "passed": 0, "total": 1, "all_passed": False,
                           "cases": [{"id": "build", "passed": False, "issues": [str(exc)], "location": getattr(exc, "location", ""),
                                      "failure": exc.info() if isinstance(exc, ExecutionError) else None,
                                     "error_code": getattr(exc, "code", "BUILD_INVALID")}]}
            state["report"] = report
            write_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / "development.json", report)
            state["versions"][-1].update(passed=report["passed"], total=report["total"])
            state["phase"] = "acceptance" if report["all_passed"] else "diagnosis"
            return str(report["passed"]) + "/" + str(report["total"]) + " 开发检查通过。"
        if phase == "diagnosis":
            state["diagnosis"] = self.planner.diagnose(req, self.spec(state["spec"]), self.source(state), self.failures(state))
            write_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / "diagnosis.json", state["diagnosis"])
            if state["repairs"] >= state["max_repairs"]:
                state.update(status="stopped", stop_reason="已达到最大修复次数。")
            elif state["review"] and state["version"] not in state["reviewed_versions"]:
                state.update(status="waiting_for_guidance", phase="repair")
            else:
                state["phase"] = "repair"
            return state["diagnosis"]["summary"]
        if phase == "repair":
            diagnosis = state["diagnosis"] or {}
            stalled = self.repair_stalled(state)
            if (state["schema_version"] == 4 and hasattr(self.planner, "redesign")
                    and (state.get("repair_scope_override") == "design" or
                         state.get("repair_scope_override") != "code" and (diagnosis.get("repair_scope") == "design" or stalled))):
                state["phase"] = "redesign"
                return "失败涉及工具设计或修复没有进展，进入设计级修复；业务验收保持冻结。"
            spec, source, explanation = self.planner.repair(req, self.spec(state["spec"]), self.source(state),
                self.failures(state), state["diagnosis"], state["human_directions"][-1]["text"] if state["human_directions"] else "",
                state["report"], resources)
            if getattr(self.planner, "repair_attempts", None):
                write_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / "rejected_repair_attempts.json", self.planner.repair_attempts)
            if state["schema_version"] == 4 and not self.same_tool_contracts(self.tool_contracts(spec.to_dict()), self.tool_contracts(state["spec"])):
                raise ValueError("修复不能改变冻结的工具定义。")
            self.snapshot(state, spec, source, explanation)
            state.pop("repair_scope_override", None)
            state["repairs"] += 1
            state["phase"] = "evaluate"
            return explanation
        if phase == "redesign":
            self.planner.trusted_tools = read_json(self.run_dir(state["run_id"]) / "trusted_tools.json")["tools"] if "trusted_tools" in state["frozen_hashes"] else []
            spec = self.planner.redesign(req, self.spec(state["spec"]), self.failures(state), state["diagnosis"],
                state["human_directions"][-1]["text"] if state["human_directions"] else "", resources)
            from dataclasses import replace
            spec = replace(spec, execution_policy=self.spec(state["spec"]).execution_policy)
            revision = state.get("design_repairs", 0) + 1
            existing = [int(p.name[1:]) for p in (self.run_dir(state["run_id"]) / "designs").glob("d*") if p.name[1:].isdigit()]
            path = "designs/d" + str(max([revision - 1, *existing]) + 1)
            state["pending_design"] = {"path": path, "spec": spec.to_dict(), "reason": state["diagnosis"]["summary"]}
            write_json(self.run_dir(state["run_id"]) / path / "tool_contracts.json", self.tool_contracts(spec.to_dict()))
            state["frozen_hashes"][path + "/tool_contracts"] = fingerprint(self.tool_contracts(spec.to_dict()))
            state["phase"] = "redesign_tests"
            return "已生成新的工具设计；旧版本及原业务验收保留。"
        if phase == "redesign_tests":
            pending = state["pending_design"]
            suite = self.planner.redesign_tests(req, resources, self.spec(pending["spec"]), self.suite(state), self.spec(state["spec"]))
            if any(suite.get(k, []) != self.suite(state).get(k, []) for k in ("cases", "goal_cases")):
                raise ValueError("重新设计不能修改业务测试。")
            write_json(self.run_dir(state["run_id"]) / pending["path"] / "test_suite.json", suite)
            pending["suite_hash"] = fingerprint(suite)
            state["frozen_hashes"][pending["path"] + "/test_suite"] = pending["suite_hash"]
            state["phase"] = "redesign_code"
            return "已独立生成新接口的工具测试，原业务开发与保留验收场景未变。"
        if phase == "redesign_code":
            pending = state["pending_design"]
            suite = read_json(self.run_dir(state["run_id"]) / pending["path"] / "test_suite.json")
            development = [c for k in ("cases", "goal_cases") for c in suite.get(k, []) if c["partition"] == "development"]
            result = self.planner.code(req, self.spec(pending["spec"]), development, resources,
                                      [c for c in suite["tool_cases"] if c["partition"] == "development"])
            if getattr(self.planner, "code_attempts", None):
                write_json(self.run_dir(state["run_id"]) / pending["path"] / "rejected_code_attempts.json", self.planner.code_attempts)
            if not self.same_tool_contracts(self.tool_contracts(result["spec"].to_dict()), self.tool_contracts(pending["spec"])):
                raise ValueError("代码生成不能修改新的工具契约。")
            self.snapshot(state, result["spec"], result["source"], result["explanation"],
                          {"path": pending["path"], "suite_hash": pending["suite_hash"]})
            state["design_repairs"] = state.get("design_repairs", 0) + 1
            state["pending_design"] = None
            state.pop("repair_scope_override", None)
            state["repairs"] += 1
            state["phase"] = "evaluate"
            return "设计级修复形成 v" + str(state["version"]) + "，重新运行全部开发与保留验收。"
        if phase == "acceptance":
            self.check_runtime()
            report = self.evaluate(state, "acceptance")
            state["acceptance_report"] = report
            write_json(self.run_dir(state["run_id"]) / "versions" / ("v" + str(state["version"])) / "acceptance.json", report)
            if report["all_passed"]:
                state["phase"] = "export"
            else:
                state.update(status="rejected", stop_reason="保留验收未通过，已阻止导出；本轮不使用保留用例继续修复。")
            return str(report["passed"]) + "/" + str(report["total"]) + " 保留验收通过。"
        if phase == "export":
            self.check_runtime()
            self.verify(state)
            if not state["report"]["all_passed"] or not state["acceptance_report"]["all_passed"]:
                raise ValueError("缺少通过的开发与保留验收证据。")
            state["export_path"], state["archive_path"] = export_artifact(self.root, state, resources, self.suite(state), self.source(state), self.runtime_files)
            state.update(status="completed", phase="completed")
            return "已导出可独立运行的版本和 ZIP。"
        raise ValueError("未知构建阶段。")

    def check_runtime(self):
        origin = Path(__file__).parent
        if any((origin / name).read_bytes() != data for name, data in self.runtime_files.items()):
            raise ValueError("构建期间运行时源码发生变化，拒绝使用不一致的测试或导出。")

    def continue_run(self, run_id, status, direction="", max_seconds=None, max_repairs=None, repair_scope=None, run_max_seconds=None):
        if max_seconds is not None and (type(max_seconds) not in (int, float) or not 0 <= max_seconds <= 86400):
            raise ValueError("总耗时应为 0 至 86400 秒。")
        if run_max_seconds is not None and (type(run_max_seconds) not in (int, float) or not 0 <= run_max_seconds <= 86400):
            raise ValueError("子 Agent 总耗时应为 0 至 86400 秒。")
        if max_repairs is not None and (type(max_repairs) is not int or not 0 <= max_repairs <= 20):
            raise ValueError("最大修复次数应为 0 至 20。")
        if repair_scope not in (None, "auto", "code", "design"):
            raise ValueError("修复范围应为 auto/code/design。")
        owner = uuid.uuid4().hex
        self.store.acquire(run_id, owner)
        try:
            state = self.load(run_id)
            if state["status"] not in status:
                raise ValueError("当前状态不能执行此操作。")
            if len(direction) > 2000:
                raise ValueError("修改方向最多 2000 字符。")
            if state["status"] == "stopped" and (max_repairs is None or max_repairs <= state["repairs"]):
                raise ValueError("继续已停止构建需设置大于已用轮次的 --max-repairs。")
            if state["status"] == "waiting_for_guidance":
                state["human_directions"].append({"version": state["version"], "text": direction or "按照失败证据修复"})
                state["reviewed_versions"].append(state["version"])
            elif direction:
                state["human_directions"].append({"version": state["version"], "text": direction})
            if max_seconds is not None:
                state.setdefault("performance", {})["max_seconds"] = max_seconds
            if run_max_seconds is not None:
                state.setdefault("performance", {})["run_max_seconds"] = run_max_seconds
            if max_repairs is not None:
                state["max_repairs"] = max_repairs
            if state["status"] == "stopped":
                state["phase"] = "repair"
            if repair_scope in ("code", "design"):
                if not state["version"] or state["schema_version"] != 4 or state["phase"] not in ("repair", "diagnosis", "redesign"):
                    raise ValueError("只能在已生成自适应 Agent 的修复阶段选择范围。")
                state["phase"] = "redesign" if repair_scope == "design" else "repair"
                state["repair_scope_override"] = repair_scope
            state.update(status="queued", last_error=None, stop_reason=None, error_info=None)
            self.save(state, owner)
        finally:
            self.store.release(run_id, owner)
        return state

    def resume(self, run_id, direction=""):
        return self.drive(self.continue_run(run_id, {"waiting_for_guidance"}, direction))

    def retry(self, run_id, max_seconds=None, max_repairs=None, direction="", repair_scope=None, run_max_seconds=None):
        return self.drive(self.continue_run(run_id, {"failed", "time_limit", "stopped"}, direction,
                          max_seconds=max_seconds, max_repairs=max_repairs, repair_scope=repair_scope, run_max_seconds=run_max_seconds))

    def recover(self, run_id):
        state = self.load(run_id)
        if state["status"] not in ("queued", "running"):
            raise ValueError("recover 仅用于排队或遗留运行任务。")
        if self.store.active(run_id):
            raise ValueError("运行租约仍有效；请等待正在执行的构建或租约到期。")
        return self.drive(state)

    def upgrade_runtime(self, run_id):
        owner = uuid.uuid4().hex
        self.store.acquire(run_id, owner)
        try:
            state = self.load(run_id)
            if not state["version"] or state["status"] == "completed":
                raise ValueError("仅升级已生成代码且未完成验收的任务；完成的版本使用其冻结运行时。")
            if state.get("runtime_hash") == self.runtime_hash:
                raise ValueError("任务已经使用当前运行时。")
            self.check_runtime()
            self.snapshot(state, self.spec(state["spec"]), self.source(state), "升级受信运行时；保留原需求、工具定义及测试，重新执行全部验收。")
            state.update(status="queued", phase="evaluate", last_error=None, stop_reason=None)
            self.event(state, "runtime_upgrade", "运行时升级形成独立版本；原版本和失败证据保留。")
            self.save(state, owner)
        finally:
            self.store.release(run_id, owner)
        return self.drive(state)

    def run(self, run_id, inputs, on_event=None, expected_artifact=None, checkpoint=None, reply=None, on_checkpoint=None, max_seconds=None, audit_context=None):
        state = self.load(run_id)
        if expected_artifact and state["artifact_hash"] != expected_artifact:
            raise ValueError("Agent 版本变化，请重新运行。")
        if not state["version"]:
            raise ValueError("该任务尚未生成代码。")
        if state.get("runtime_hash") and state["runtime_hash"] != self.runtime_hash:
            from .frozen_runtime import frozen_agent
            agent = frozen_agent(self, state)
        else:
            agent = self.agent(state)
        if getattr(agent, "supports_resume", False):
            options = {"max_seconds": max_seconds} if max_seconds is not None and getattr(agent, "supports_time_limit", False) else {}
            if max_seconds is not None and not options:
                raise ValueError("历史运行时不支持总耗时设置，请升级未完成任务或使用新构建。")
            if audit_context is not None and getattr(agent, "supports_audit", False):
                options["audit_context"] = audit_context
            return agent.run(inputs, on_event if on_checkpoint is None else None, checkpoint=checkpoint, reply=reply, on_checkpoint=on_checkpoint, **options)
        if checkpoint is not None or reply is not None:
            raise ValueError("历史运行时不支持决策恢复；请创建使用新运行时的 Agent。")
        return agent.run(inputs, on_event) if state["schema_version"] == 4 else agent.run(inputs)

    def detail(self, run_id):
        state = self.load(run_id)
        state = {**state, "source": self.source(state) if state["version"] else "",
                 "active": self.store.active(run_id)}
        state["changes"] = [read_json(self.run_dir(run_id) / "versions" / ("v" + str(v["version"])) / "change.json") for v in state["versions"]]
        if "test_suite" in state["frozen_hashes"]:
            state["test_suite"] = self.suite(state)
        records = [read_json(p) for p in (self.run_dir(run_id) / "executions").glob("*.json")]
        state["executions"] = [{k: r[k] for k in ("execution_id", "version", "status", "created_at", "error")}
                               for r in sorted(records, key=lambda r: r["created_at"], reverse=True)[:20]]
        state["benchmarks"] = [read_json(p) for p in (self.run_dir(run_id) / "benchmarks").glob("*.json")]
        state["evaluation_reports"] = []
        state["evaluation_recovery"] = []
        if state["version"] and state["schema_version"] == 4:
            version = self.run_dir(run_id) / "versions" / ("v" + str(state["version"]))
            for partition in ("development", "acceptance"):
                files = [version / (partition + suffix + ".json") for suffix in
                         ("", "_interrupted", "_time_limit", "_dependency_blocked")]
                files = sorted((p for p in files if p.is_file()), key=lambda p: p.stat().st_mtime_ns, reverse=True)
                if files:
                    report = read_json(files[0])
                    if report.get("artifact_hash") == state["artifact_hash"]:
                        state["evaluation_reports"].append(report)
                path = version / (partition + "_case_progress.json")
                if path.is_file():
                    progress = read_json(path)
                    if progress.get("binding", {}).get("artifact_hash") == state["artifact_hash"]:
                        entries = progress["entries"]
                        state["evaluation_recovery"].append({"partition": partition,
                            "completed": [cid for cid, e in entries.items() if e["status"] == "completed"],
                            "interrupted": [cid for cid, e in entries.items() if e["status"] == "interrupted"],
                            "inflight": [cid for cid, e in entries.items() if e["status"] == "inflight"],
                            "last_attempt": progress["attempts"][-1] if progress["attempts"] else None})
        state["call_metrics"] = self.call_metrics(run_id)
        return state
