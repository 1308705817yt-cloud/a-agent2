"""工作流及产物契约；JSON 数据与可执行能力保持分离。"""

from dataclasses import asdict, dataclass

from .csv_contract import OPERATIONS, canonical_parameters, validate_parameters


TASK_TYPES = ("document_qa", "csv_analysis")


def canonical_csv_parameters(parameters):
    """Compatibility entry point for the shared executable CSV contract."""
    return canonical_parameters(parameters)


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
            validate_parameters(self.defaults, resources, allow_dataset=True)

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
