"""Durable per-case evaluation commits, bound to frozen artifact and inputs."""

import copy
import threading
import uuid
import time
from pathlib import Path

from .resources import fingerprint, read_json, write_json


def interrupted(output):
    if not isinstance(output, dict):
        return False
    code = (output.get("error") or {}).get("code")
    return code in ("MODEL_ERROR", "MODEL_TIMEOUT", "TASK_TIMEOUT") or bool(output.get("error") and
           (output.get("failure") or {}).get("origin") in ("external", "configuration"))


def reuse_rule(previous, current):
    if previous is None:
        return "unknown_previous_conditions"
    if previous.get("model_configuration") != current.get("model_configuration"):
        return "model_configuration_changed"
    old, new = previous["max_seconds"], current["max_seconds"]
    if old == new:
        return "same_conditions"
    if new == 0 or old != 0 and new >= old:
        return "relaxed_runtime_limit"
    return "tightened_runtime_limit"


class EvaluationProgress:
    def __init__(self, path, binding, cases, conditions=None):
        self.path = Path(path) if path is not None else None
        self.lock = threading.Lock()
        self.hashes = {c["id"]: fingerprint(c) for c in cases}
        self.binding = binding
        self.conditions = copy.deepcopy(conditions or {"max_seconds": 0, "model_configuration": {"backend": "unspecified"}})
        self.state = {"format": 2, "binding": binding, "entries": {}, "attempts": []}
        if self.path is not None and self.path.exists():
            stored = read_json(self.path)
            if stored.get("format") not in (1, 2) or stored.get("binding") != binding:
                raise ValueError("用例恢复记录与产物、运行时、测试集或分区不一致；拒绝复用。")
            for cid, entry in stored["entries"].items():
                if cid not in self.hashes or entry.get("case_hash") != self.hashes[cid]:
                    raise ValueError("用例恢复记录与冻结输入或断言不一致。")
                for field in ("output", "checkpoint"):
                    if field in entry and fingerprint(entry[field]) != entry.get(field + "_hash"):
                        raise ValueError("用例恢复记录 " + cid + " 的 " + field + " 指纹不一致。")
                if "conditions" in entry and fingerprint(entry["conditions"]) != entry.get("conditions_hash"):
                    raise ValueError("用例执行条件指纹不一致。")
                if entry.get("status") not in ("inflight", "completed", "interrupted"):
                    raise ValueError("用例恢复状态无效。")
                if entry.get("status") == "completed" and interrupted(entry.get("output")):
                    raise ValueError("调用中断的用例不能标为已完成。")
                if entry.get("status") == "completed" and "output" not in entry:
                    raise ValueError("已完成用例缺少提交结果。")
            self.state = stored
            for attempt in stored["attempts"]:
                if "conditions_hash" in attempt and fingerprint(attempt.get("conditions")) != attempt["conditions_hash"]:
                    raise ValueError("评测批次条件指纹不一致。")
        previous = self.state["attempts"][-1].get("conditions") if self.state["attempts"] else None
        self.condition_change = {"previous": previous, "current": self.conditions, "rule": reuse_rule(previous, self.conditions)}
        for cid, entry in list(self.state["entries"].items()):
            rule = reuse_rule(entry.get("conditions"), self.conditions)
            if rule not in ("same_conditions", "relaxed_runtime_limit"):
                history = entry.get("history", []) + [{k: copy.deepcopy(v) for k, v in entry.items() if k != "history"} | {"invalidated_by": rule}]
                self.state["entries"][cid] = {"case_hash": self.hashes[cid], "status": "inflight", "history": history}
        self.state["format"] = 2
        self.attempt_id = uuid.uuid4().hex
        self.state["attempts"].append({"id": self.attempt_id, "started_at": time.time(), "conditions": self.conditions,
                                      "conditions_hash": fingerprint(self.conditions),
                                      "condition_change": self.condition_change, "executed": [], "reused": [], "resumed": [],
                                      "reused_details": [], "executed_details": []})
        self.attempt = self.state["attempts"][-1]
        self.flush()

    def flush(self):
        if self.path is not None:
            write_json(self.path, self.state)

    def entry(self, case):
        return self.state["entries"].setdefault(case["id"], {"case_hash": self.hashes[case["id"]], "status": "inflight",
                                                           "conditions": self.conditions, "conditions_hash": fingerprint(self.conditions)})

    def cached(self, case):
        with self.lock:
            entry = self.entry(case)
            if entry["status"] != "completed":
                return None
            self.attempt["reused"].append(case["id"])
            self.attempt["reused_details"].append({"case_id": case["id"], "mode": "historical_reuse", "conditions": entry["conditions"],
                "requested_conditions": self.conditions, "rule": reuse_rule(entry["conditions"], self.conditions),
                "original_attempt": entry.get("completed_attempt_id"),
                "recorded_model_calls": entry["output"].get("model_calls", 0),
                "committed_decisions": sum(e.get("type") == "decision" for e in entry["output"].get("trace", []))})
            self.flush()
            return copy.deepcopy(entry["output"])

    def begin(self, case):
        with self.lock:
            entry = self.entry(case)
            checkpoint = copy.deepcopy(entry.get("checkpoint"))
            previous_conditions = entry.get("conditions")
            entry["status"] = "inflight"
            entry["attempt_id"] = self.attempt_id
            self.attempt["executed"].append(case["id"])
            if checkpoint is not None:
                self.attempt["resumed"].append(case["id"])
            entry.update(conditions=self.conditions, conditions_hash=fingerprint(self.conditions))
            self.attempt["executed_details"].append({"case_id": case["id"], "mode": "resumed" if checkpoint is not None else "fresh_execution",
                "conditions": self.conditions, "checkpoint_conditions": previous_conditions if checkpoint is not None else None,
                "reused_committed_decisions": sum(e.get("type") == "decision" for e in (checkpoint or {}).get("trace", [])),
                "elapsed_before_ms": (checkpoint or {}).get("elapsed_ms", 0)})
            self.flush()
            return checkpoint

    def evidence(self, case_id):
        return copy.deepcopy(next(e for e in [*self.attempt["reused_details"], *self.attempt["executed_details"]] if e["case_id"] == case_id))

    def checkpoint(self, case, checkpoint):
        # Retain the last resumable point before a fatal dependency read, not a completed state.
        if checkpoint.get("phase") in ("completed", "waiting_input", "budget"):
            return
        with self.lock:
            entry = self.entry(case)
            entry["checkpoint"] = copy.deepcopy(checkpoint)
            entry["checkpoint_hash"] = fingerprint(checkpoint)
            self.flush()

    def commit(self, case, output, *, unit=False):
        with self.lock:
            entry = self.entry(case)
            entry["status"] = "interrupted" if not unit and interrupted(output) else "completed"
            entry["output"] = copy.deepcopy(output)
            entry["output_hash"] = fingerprint(output)
            entry["completed_attempt_id"] = self.attempt_id
            if entry["status"] == "completed":
                entry.pop("checkpoint", None)
                entry.pop("checkpoint_hash", None)
            self.flush()
