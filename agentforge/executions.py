"""Persist live child-agent traces independently of the build lease."""

import time
import uuid
import copy
import os
from contextlib import contextmanager

from .resources import read_json, write_json, fingerprint
from .storage import valid_id


def execution_path(factory, run_id, execution_id):
    return factory.run_dir(run_id) / "executions" / (valid_id(execution_id) + ".json")


@contextmanager
def locked(path):
    """An OS lock survives neither a process crash nor a stale Python thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("此执行正在被另一个进程处理。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def create_execution(factory, run_id, inputs, max_seconds=None, bindings=None):
    state = factory.load(run_id)
    if not state["version"]:
        raise ValueError("尚未生成 Agent。")
    if max_seconds is not None and (type(max_seconds) not in (int, float) or not 0 <= max_seconds <= 86400):
        raise ValueError("运行总耗时应为 0 至 86400 秒。")
    record = {"execution_id": uuid.uuid4().hex[:12], "run_id": run_id, "version": state["version"],
              "artifact_hash": state["artifact_hash"], "status": "queued", "input": inputs,
              "created_at": time.time(), "updated_at": time.time(), "trace": [], "result": None, "error": None,
              "checkpoint": None, "checkpoint_hash": None, "reply": None, "attempts": [], "max_seconds": max_seconds}
    if bindings is not None:
        agent = factory.execution_agent(state)
        if not getattr(agent, "supports_data_binding", False):
            raise ValueError("历史产物不支持兼容数据绑定。")
        # Resolve local paths once. Recovery reuses the saved immutable snapshot.
        record["data_binding"] = agent.prepare_data_binding(bindings)
    write_json(execution_path(factory, run_id, record["execution_id"]), record)
    return record


def execute_record(factory, run_id, execution_id):
    path = execution_path(factory, run_id, execution_id)
    with locked(path):
        return _execute_record(factory, run_id, execution_id, path)


def _execute_record(factory, run_id, execution_id, path):
    record = read_json(path)
    if record["status"] != "queued":
        raise ValueError("执行记录不能重复运行。")
    state = factory.load(run_id)
    record.update(status="running", updated_at=time.time())
    write_json(path, record)
    def update(trace):
        record.update(trace=trace, updated_at=time.time())
        write_json(path, record)
    def checkpoint(value):
        record.update(checkpoint=value, checkpoint_hash=fingerprint(value), trace=value["trace"], updated_at=time.time())
        write_json(path, record)
    try:
        if record["artifact_hash"] != state["artifact_hash"]:
            raise ValueError("排队期间 Agent 版本变化，请重新运行。")
        if record.get("checkpoint") and fingerprint(record["checkpoint"]) != record.get("checkpoint_hash"):
            raise ValueError("执行恢复状态指纹不一致。")
        options = {"max_seconds": record["max_seconds"]} if record.get("max_seconds") is not None else {}
        if record.get("data_binding") is not None:
            options["data_binding"] = record["data_binding"]
        result = factory.run(run_id, record["input"], update, record["artifact_hash"],
                             checkpoint=record.get("checkpoint"), reply=record.get("reply"), on_checkpoint=checkpoint, **options)
        result.pop("checkpoint", None)
        code = (result.get("error") or {}).get("code")
        status = "waiting_input" if code == "NEEDS_INPUT" and record.get("checkpoint") else "retryable" if code in ("MODEL_ERROR", "MODEL_TIMEOUT") and record.get("checkpoint") else "time_limit" if code == "TASK_TIMEOUT" and record.get("checkpoint") else "budget" if code == "AGENT_BUDGET" and record.get("checkpoint") else "failed" if result.get("error") else "completed"
        record.update(status=status, result=result, reply=None,
                      trace=result.get("trace", []), error=result.get("error"))
    except Exception as exc:
        record.update(status="failed", error={"code": "EXECUTION_FAILED", "message": str(exc)[:1000]})
    record["updated_at"] = time.time()
    write_json(path, record)
    return record


def continue_execution(factory, run_id, execution_id, reply=None, max_seconds=None):
    path = execution_path(factory, run_id, execution_id)
    with locked(path):
        record = read_json(path)
        state = factory.load(run_id)
        if state["artifact_hash"] != record["artifact_hash"] or state["version"] != record["version"]:
            raise ValueError("Agent 版本变化，不能继续历史执行。")
        if record["status"] not in ("waiting_input", "retryable", "interrupted", "budget", "time_limit") or not record.get("checkpoint"):
            raise ValueError("此执行没有可恢复的决策状态；历史运行时需重新运行。")
        saved = record["checkpoint"]
        if fingerprint(saved) != record.get("checkpoint_hash"):
            raise ValueError("执行恢复状态指纹不一致。")
        if saved["calls"] >= state["spec"]["max_steps"] * 3 and saved.get("pending") is None:
            raise ValueError("已达到累计决策上限，不能继续增加调用。")
        if saved["phase"] == "waiting_input":
            if reply is None and record.get("reply") is not None:
                reply = copy.deepcopy(record["reply"])
            if not isinstance(reply, dict) or set(reply) - {"text", "parameters"} or not isinstance(reply.get("text"), str) or not reply["text"].strip() or len(reply["text"]) > 6000 or not isinstance(reply.get("parameters", {}), dict) or len(saved["replies"]) >= 5:
                raise ValueError("请提供非空 text 和可选 parameters，最多接续 5 次。")
            if reply.get("parameters"):
                if state["requirements"]["task_type"] != "csv_analysis":
                    raise ValueError("文档问答只接受文字补充。")
                agent = factory.execution_agent(state)
                if record.get("data_binding") is not None:
                    agent = agent.with_data_binding(record["data_binding"])
                _, params, _ = agent.prepare(record["input"])
                for old in saved["replies"]:
                    params.update(old.get("parameters", {}))
                agent.prepare({k: v for k, v in params.items() if k != "dataset_id"} | reply["parameters"])
        elif reply is not None:
            raise ValueError("此执行不在等待人工回复。")
        if max_seconds is not None:
            if type(max_seconds) not in (int, float) or not 0 <= max_seconds <= 86400:
                raise ValueError("运行总耗时应为 0 至 86400 秒。")
            record["max_seconds"] = max_seconds
        record.setdefault("attempts", []).append({"time": time.time(), "status": record["status"],
                                                  "error": record["error"], "checkpoint_hash": record["checkpoint_hash"]})
        record.update(status="queued", reply=copy.deepcopy(reply), error=None, updated_at=time.time())
        write_json(path, record)
        return record


def recover_interrupted(factory):
    """Expose explicit recovery; never silently repeat billable model requests."""
    for path in (factory.root / "runs").glob("*/executions/*.json"):
        try:
            with locked(path):
                record = read_json(path)
                if record["status"] in ("queued", "running"):
                    record.update(status="interrupted" if record.get("checkpoint") else "failed",
                                  error={"code": "INTERRUPTED", "message": "进程退出中断了执行；可从最后提交状态继续。" if record.get("checkpoint") else "执行尚未保存恢复状态，请重新运行。"}, updated_at=time.time())
                    write_json(path, record)
        except ValueError:
            continue
