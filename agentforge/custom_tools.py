"""Generated tools have real source and data access; no untrusted Python exec."""

import copy

from .adaptive_contracts import parameter_schema
from .codex_client import CodexError, validate_json
from .contracts import ExecutionError
from .program import Program, TOOL_BUDGET, TOOL_MAX_TEXT, TOOL_MAX_ITEMS


def validate_tool_arguments(definition, arguments):
    """The same host boundary used by live calls and negative contract tests."""
    try:
        validate_json(arguments, parameter_schema(definition), "input")
    except CodexError as exc:
        details = {**getattr(exc, "details", {}), "rejection_layer": "host", "tool": definition["id"]}
        raise ExecutionError("INVALID_ARGUMENTS", str(exc), details.get("field_path", definition["id"]),
                             origin="decision", retryable=True, details=details) from exc


class CustomTool:
    def __init__(self, definition, resources):
        self.definition, self.resources = definition, resources
        # Validate source even before its first invocation.
        try:
            Program(definition["source"], extra_helpers={"dataset": None, "document": None, "observation": None})
        except ExecutionError as exc:
            filename = definition["id"].split(".")[1] + ".py"
            raise ExecutionError(exc.code, str(exc), exc.location.replace("logic.py", "custom_tools/" + filename),
                                 origin="source", retryable=False) from exc

    def invoke(self, arguments, observations, requirements):
        validate_tool_arguments(self.definition, arguments)
        def dataset(rid):
            found = next((d for d in self.resources["datasets"] if d["id"] == rid), None)
            if not found:
                raise ExecutionError("UNKNOWN_RESOURCE", "自定义工具不能读取未授权数据集。")
            return copy.deepcopy(found)
        def document(rid):
            found = next((d for d in self.resources["documents"] if d["id"] == rid), None)
            if not found:
                raise ExecutionError("UNKNOWN_RESOURCE", "自定义工具不能读取未授权文档。")
            return copy.deepcopy(found)
        def observation(rid):
            found = next((o for o in observations if o["id"] == rid), None)
            if not found:
                raise ExecutionError("UNKNOWN_OBSERVATION", "工具引用了不存在的观察结果。")
            return copy.deepcopy(found)
        helpers = {"dataset": dataset, "document": document, "observation": observation}
        program = Program(self.definition["source"], budget=TOOL_BUDGET, extra_helpers=helpers,
                          max_text=TOOL_MAX_TEXT, max_items=TOOL_MAX_ITEMS)
        try:
            result = program.run({"input": copy.deepcopy(arguments), "defaults": requirements.defaults})
            validate_json(result, self.definition["output_schema"], "tool.result")
        except CodexError as exc:
            filename = self.definition["id"].split(".")[1] + ".py"
            raise ExecutionError("TOOL_OUTPUT_CONTRACT", str(exc), "custom_tools/" + filename + ":" + str(program.line),
                                 origin="source", retryable=False) from exc
        except ExecutionError as exc:
            filename = self.definition["id"].split(".")[1] + ".py"
            raise ExecutionError(exc.code, str(exc), exc.location.replace("logic.py", "custom_tools/" + filename),
                                 origin=exc.origin, retryable=exc.retryable, recovery=exc.recovery) from exc
        return result
