"""工作流及产物契约；JSON 数据与可执行能力保持分离。"""

from dataclasses import asdict, dataclass


TASK_TYPES = ("document_qa", "csv_analysis")
OPERATIONS = ("sum", "mean", "count", "min", "max")


@dataclass(frozen=True)
class Requirements:
    request: str
    task_type: str
    summary: str
    acceptance_criteria: list[str]
    defaults: dict

    def validate(self, resources):
        if self.task_type not in TASK_TYPES or not self.summary.strip() or not self.acceptance_criteria:
            raise ValueError("任务类型、需求摘要或验收条件无效。")
        if self.task_type == "document_qa" and not resources["documents"]:
            raise ValueError("文档问答需要至少一份文档。")
        if self.task_type == "csv_analysis":
            datasets = {d["id"]: d for d in resources["datasets"]}
            dataset = datasets.get(self.defaults.get("dataset_id"))
            if not dataset:
                raise ValueError("CSV 需求必须绑定已提供的数据集。")
            if self.defaults.get("operation") not in OPERATIONS:
                raise ValueError("CSV 操作必须是 sum、mean、count、min 或 max。")
            for field in ("column", "group_by"):
                if self.defaults.get(field) and self.defaults[field] not in dataset["columns"]:
                    raise ValueError(f"需求中的 {field} 不存在于数据集。")
            if self.defaults["operation"] != "count" and not self.defaults.get("column"):
                raise ValueError("数值聚合需要指定列。")
            from .tool_registry import validate_filters
            validate_filters(self.defaults.get("filters", []), dataset["columns"])
            if self.defaults.get("sort_by", "group") not in ("group", "value", "count") or type(self.defaults.get("descending", False)) is not bool:
                raise ValueError("默认排序参数无效。")
            limit = self.defaults.get("limit")
            if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
                raise ValueError("默认结果数量无效。")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class AgentSpec:
    name: str
    task_type: str
    tools: list[str]
    entry: str
    nodes: list[dict]
    max_steps: int = 32

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**value)


class ExecutionError(ValueError):
    def __init__(self, code, message, location="", *, origin=None, retryable=None, recovery=None, details=None):
        super().__init__(message)
        self.code, self.location = code, location
        self.origin = origin or ("source" if code.startswith("CODE_") or code == "TOOL_OUTPUT_CONTRACT"
                                 else "input" if code == "INVALID_INPUT" else "decision")
        self.retryable = (self.origin == "decision") if retryable is None else retryable
        self.details = details
        self.recovery = recovery or ("repair_source" if self.origin == "source" else
                                     "redesign_tool" if self.origin == "design" else
                                     "check_tool_configuration" if self.origin == "configuration" else
                                     "retry_tool" if self.origin == "external" and self.retryable else
                                     "check_service_or_configuration" if self.origin == "external" else
                                     "reduce_result_or_adjust_limit" if self.origin == "runtime" else
                                     "revise_decision" if self.retryable else "correct_input")

    def info(self):
        return {"code": self.code, "message": str(self), "location": self.location,
                "origin": self.origin, "retryable": self.retryable,
                "recovery": self.recovery, **({"details": self.details} if self.details is not None else {})}


class DependencyError(ExecutionError):
    """A failed dependency needs operator recovery, not generated-code repair."""


def error_response(code, message):
    return {"answer": message, "data": None, "sources": [], "abstained": True,
            "error": {"code": code, "message": message}}
