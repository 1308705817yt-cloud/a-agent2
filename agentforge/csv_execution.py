"""Deterministic CSV query execution, independent of model scheduling."""

from .csv_contract import validate_parameters
from .tool_registry import execute_tool


QUERY_STEPS = (("csv.load", ("dataset_id",)), ("csv.filter", ("filters",)),
               ("csv.aggregate", ("operation", "column", "group_by")),
               ("csv.sort", ("sort_by", "descending")), ("csv.limit", ("limit",)))


def execute_query(arguments, requirements, resources):
    params = validate_parameters(arguments, resources, requirements.defaults,
                                 allow_dataset=True, argument_error="INVALID_ARGUMENTS")
    context = {"input": params, "result": None}
    for name, fields in QUERY_STEPS:
        context["result"] = execute_tool(name, {k: params[k] for k in fields}, context, resources)
    return context["result"]
