"""Resource-scoped, versioned profiles for immutable authorized CSV snapshots.

Only this deterministic host computation is cached. Custom tools and live
trusted tools do not acquire cross-request caching by implication.
"""

import copy
import hashlib
import threading
import time
from pathlib import Path

from .codex_client import DeadlineExceeded
from .csv_contract import CONTRACT_VERSION
from .contracts import ExecutionError
from .resources import fingerprint, read_json, write_json
from .tool_registry import number


PROFILE_VERSION = "csv-resource-profile-v1"


class ResourceProfiles:
    def __init__(self, root):
        self.directory = Path(root).resolve() / ".agentforge" / "resource_profiles"
        self.memory = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        origin = Path(__file__).resolve().parent
        self.algorithm_hash = fingerprint({name: hashlib.sha256((origin / name).read_bytes()).hexdigest()
                                           for name in ("resource_profiles.py", "csv_contract.py", "tool_registry.py")})
        self.last_audit = None

    @property
    def last_audit(self):
        return getattr(self._local, "last_audit", None)

    @last_audit.setter
    def last_audit(self, value):
        self._local.last_audit = value

    def get(self, dataset, deadline=None, deadline_scope="runtime"):
        remaining = max(0, deadline - time.monotonic()) if deadline is not None else None
        acquired = self._lock.acquire(timeout=remaining) if remaining is not None else self._lock.acquire()
        if not acquired:
            raise DeadlineExceeded("等待资源质量检查达到运行时间上限。", deadline_scope)
        try:
            return self._get(dataset, deadline, deadline_scope)
        finally:
            self._lock.release()

    def _get(self, dataset, deadline, deadline_scope):
        started = time.perf_counter()
        binding = {"profile_version": PROFILE_VERSION, "contract_version": CONTRACT_VERSION,
                   "algorithm_hash": self.algorithm_hash,
                   "resource_hash": fingerprint(dataset)}
        key = fingerprint(binding)
        audit = {**binding, "cache_key": key, "cache_status": "miss"}
        self.last_audit = audit
        def check_deadline():
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded("资源质量检查达到运行时间上限。", deadline_scope)
        try:
            check_deadline()
            if key in self.memory:
                audit["cache_status"] = "memory_hit"
                return copy.deepcopy(self.memory[key])
            path = self.directory / (key + ".json")
            try:
                saved = read_json(path)
                result = saved["result"]
                if saved["binding"] == binding and saved["result_hash"] == fingerprint(result):
                    self.memory[key] = copy.deepcopy(result)
                    audit["cache_status"] = "disk_hit"
                    return copy.deepcopy(result)
                audit["cache_status"] = "invalidated"
            except FileNotFoundError:
                pass
            except (OSError, ValueError, KeyError, TypeError):
                audit["cache_status"] = "unreadable"
            columns = {c: {"blank_count": 0, "numeric_count": 0, "non_numeric_count": 0}
                       for c in dataset["columns"]}
            for index, row in enumerate(dataset["rows"]):
                if index % 128 == 0:
                    check_deadline()
                for column, counts in columns.items():
                    value = row[column]
                    if value is None or value == "":
                        counts["blank_count"] += 1
                    else:
                        try:
                            number(value)
                        except ExecutionError:
                            counts["non_numeric_count"] += 1
                        else:
                            counts["numeric_count"] += 1
            result = {"dataset_id": dataset["id"], "profile_version": PROFILE_VERSION,
                      "resource_hash": binding["resource_hash"], "row_count": len(dataset["rows"]), "columns": columns}
            check_deadline()
            self.memory[key] = copy.deepcopy(result)
            try:
                write_json(path, {"binding": binding, "result": result, "result_hash": fingerprint(result)})
            except OSError:
                audit["cache_write_failed"] = True
            return result
        finally:
            audit["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
