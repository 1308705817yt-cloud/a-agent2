"""Generate runtime agent policies and executable custom tools, then repair them."""

from .adaptive_contracts import AdaptiveSpec, BUILTIN_IDS, PARAM_TYPES, parse_json
from .codex_client import CodexError, object_schema, text_schema
from .planner import Planner, REQ_SCHEMA, STR, BOOL
from .contracts import Requirements, ExecutionError
from .resources import catalog
from .adaptive_runtime import builtin_catalog
from .program import interpreter_capabilities
from .source_validation import response_contract, preflight_output_hook
from .csv_contract import CONTRACT_VERSION, contract_catalog, contract_instructions
from .data_binding import OUTPUT_SOURCE


PARAM = object_schema({"name": text_schema(40), "type": {"type": "string", "enum": list(PARAM_TYPES)},
                       "required": BOOL, "description": STR})
ERROR_CASE = object_schema({"id": text_schema(80), "layer": {"type": "string", "enum": ["host", "tool_result"]},
                           "input_json": text_schema(6000), "code": text_schema(80), "description": STR})
TOOL_DEF = object_schema({"id": text_schema(50), "description": STR,
                          "parameters": {"type": "array", "items": PARAM, "maxItems": 12},
                          "error_cases": {"type": "array", "items": ERROR_CASE, "minItems": 1, "maxItems": 12},
                          "execution_plan": object_schema({"algorithm": text_schema(3000),
                              "helpers": {"type": "array", "items": text_schema(80), "maxItems": 30},
                              "syntax": {"type": "array", "items": text_schema(80), "maxItems": 50}}),
                          "output_schema_json": text_schema(6000), "required": BOOL})
DESIGN_SCHEMA = object_schema({"name": text_schema(100), "task_type": {"type": "string", "enum": ["document_qa", "csv_analysis"]},
    "answer_style": {"type": "string", "enum": ["facts", "analysis"]},
    "instructions": text_schema(8000), "planning_hints": {"type": "array", "items": STR, "minItems": 3, "maxItems": 8},
    "tools": {"type": "array", "items": text_schema(50), "minItems": 1, "maxItems": 8},
    "custom_tools": {"type": "array", "items": TOOL_DEF, "minItems": 0, "maxItems": 3},
    "max_steps": {"type": "integer", "minimum": 4, "maximum": 12}})
CUSTOM_SOURCE = object_schema({"id": text_schema(50), "source": text_schema(16000)})
CODE_SCHEMA = object_schema({"source": text_schema(16000), "custom_sources": {"type": "array", "items": CUSTOM_SOURCE, "minItems": 0, "maxItems": 3}, "explanation": STR})
TOOL_CASE = object_schema({"id": text_schema(80), "partition": {"type": "string", "enum": ["development", "acceptance"]},
                           "tool": text_schema(50), "input_json": text_schema(6000),
                           "expected_json": {"anyOf": [text_schema(6000), {"type": "null"}]},
                           "expected_error": {"anyOf": [object_schema({"code": {"type": "string", "enum": ["INVALID_ARGUMENTS"]},
                                "origin": {"type": "string", "enum": ["decision"]}, "layer": {"type": "string", "enum": ["host"]}}), {"type": "null"}]},
                           "purpose": STR, "criterion": STR})
GOAL_CASE = object_schema({"id": text_schema(80), "partition": {"type": "string", "enum": ["development", "acceptance"]},
                           "goal": STR, "parameters_json": text_schema(4000), "reference_query_json": text_schema(4000),
                           "source_id": {"anyOf": [text_schema(100), {"type": "null"}]},
                           "facts": {"type": "array", "items": STR, "maxItems": 8}, "refusal": BOOL,
                           "purpose": STR, "criterion": STR})
EXTRA_TESTS = object_schema({"tool_cases": {"type": "array", "items": TOOL_CASE, "minItems": 0, "maxItems": 96},
                             "goal_cases": {"type": "array", "items": GOAL_CASE, "minItems": 2, "maxItems": 4}})
TOOL_CODE_RULES = """Generate actual Python source for every declared custom tool with exactly def run(context).
Implement its described business computation, NOT a stub, hardcoded test answer, or response passthrough.
context['input'] is the tool arguments; context['defaults'] is frozen request defaults.
Use dataset(id) to obtain authorized {id,name,columns,rows}; rows are dicts with string cells.
Use document(id) for {id,title,content}. Use observation(id) for a prior {id,tool,arguments,status,result}.
These resource helpers are host data access, not filesystem. No arbitrary imports, file, network, process or exec.
Supported syntax: assignments/subscript assignments, if/else, for, break/continue, return, dict/list, slices,
arithmetic, comparisons, booleans and conditional expressions. Helpers len/str/int/float/bool/round/abs/min/max/sum/sorted/get/append/join/split/lower/upper,
plus dataset/document/observation and is_object/is_array/is_string for JSON type checks.
get(dict,key,default=None); append(list,value); join(list_of_strings,separator).
Validate untyped nested arrays/objects with these type helpers before calling get or accessing their fields.
No attributes/methods, comprehensions, f-strings, while, try, nested def or keyword arguments.
Return the exact declared output schema. Handle empty numeric cells appropriately; read resources/arguments dynamically.
The host validates declared parameter types, required/unknown fields and data bounds BEFORE running tool code.
Host-invalid cases expect INVALID_ARGUMENTS at layer=host; no generated branch can receive such inputs.
Business failures inside legal nested values use the declared output schema (tool_result), not expected_error.
Tools run under a 200000-step / 600000-character data budget. Do not change a tool's declared contract to fit code.
Also generate logic.py with def run(context), where context['result'] is the final response; only format answer,
preserving data/sources/abstained/error. This output hook uses only the base pure helpers and a 50000-step budget.
The EXACT hook response has five keys: answer, data, sources, abstained, error. result_ref is ONLY a runtime
decision field: the host has already resolved it to data. No final_response flag or extra context binding exists.
Start with result = context['result']; change result['answer'] if needed and return result. Preserve error/refusal too.
For document QA, make validated source quotes and document IDs visible in answer when they are not already shown:
copy them from context['result']['sources'], never invent quotes or hardcode test facts. Keep the answer first.
"""


def decode_design(value):
    tools = []
    for tool in value["custom_tools"]:
        import json
        encoded = tool["output_schema_json"]
        try:
            schema = json.loads(encoded, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限数值")))
        except json.JSONDecodeError as exc:
            excerpt = encoded[max(0, exc.pos - 90):exc.pos + 90]
            raise ValueError(tool["id"] + ".output_schema_json 在字符 " + str(exc.pos) + " 处无效：" + exc.msg
                             + "；附近内容：" + repr(excerpt) + "。检查 properties 对象是否在 required 之前闭合。") from exc
        except (ValueError, TypeError) as exc:
            raise ValueError(tool["id"] + ".output_schema_json 无效：" + str(exc)) from exc
        tools.append({k: v for k, v in tool.items() if k != "output_schema_json"} |
                     {"output_schema": schema, "source": ""})
    return AdaptiveSpec(**(value | {"custom_tools": tools}))


def apply_sources(spec, value):
    sources = {v["id"]: v["source"] for v in value["custom_sources"]}
    if len(sources) != len(value["custom_sources"]) or set(sources) != {t["id"] for t in spec.custom_tools}:
        raise ValueError("生成源码必须与工具定义一一对应；需要 " + repr([t["id"] for t in spec.custom_tools])
                         + "，实际返回 " + repr([v["id"] for v in value["custom_sources"]]))
    from dataclasses import replace
    return replace(spec, custom_tools=[t | {"source": sources[t["id"]]} for t in spec.custom_tools])


class AdaptivePlanner(Planner):
    adaptive = True

    def requirements(self, request, resources):
        value = self.client.complete("Understand the agent-building request. Support runtime Codex document QA with semantic understanding, "
            "or CSV data-analysis agents that plan queries and execute generated pure data-processing tools. "
            "Custom tools can calculate business metrics, profiles, data quality, comparisons and transformations from supplied resources. "
            "Built-in csv.sql supports bounded read-only joins, window functions and multi-column aggregates across supplied CSVs. "
            "Frozen data_contract metrics use exact decimal calculation, distinct counts and declared ratios/differences via csv.metrics. "
            "Preserve the requested scope; do not invent extra mandatory domain calculations. For document QA, a compact metadata/evidence "
            "tool is meaningful custom work; a bespoke policy-rule parser/calculator is needed only when explicitly requested. "
            "Operator-configured trusted tools may additionally read databases or HTTP JSON services; no arbitrary OS/network actions, ML training, PDFs or unprovided external data. Do not silently drop requirements. "
            "If essential behavior cannot be achieved set supported=false. CSV defaults bind one provided dataset and sum/mean/count/min/max, "
            "columns, optional grouping/filters/sort/limit. Use [] in filters_json. Document defaults use null dataset/operation/column/group/limit, "
            "[] filters, sort_by=group, descending=false. Include source/numeric evidence and requested business behavior in criteria. "
            "Do not add mandatory custom computation or a data-quality step when the requested behavior is already covered by builtins.",
            {"request": request, "resources": catalog(resources, True), "trusted_tools": self.trusted_catalog(),
             "csv_contract": contract_catalog(),
             "interpreter_capabilities": interpreter_capabilities()}, REQ_SCHEMA)
        if not value["supported"]:
            raise ValueError("需求超出当前能力：" + value["summary"])
        defaults = value["defaults"]
        defaults["filters"] = parse_json(defaults.pop("filters_json"))
        if value["task_type"] == "document_qa":
            defaults = {}
        req = Requirements(request, value["task_type"], value["summary"], value["acceptance_criteria"], defaults)
        req.validate(resources)
        return req

    def workflow(self, requirements, resources):
        self.design_attempts = []
        errors = []
        for attempt in range(3):
            try:
                return self.design(requirements, resources, errors)
            except (ValueError, TypeError, KeyError, ExecutionError) as exc:
                feedback = getattr(exc, "details", None)
                errors.append(feedback or str(exc)[:1000])
                self.design_attempts.append({"attempt": attempt + 1, "error": str(exc)[:1000], "feedback": feedback, "design": self.last_design})
        raise ExecutionError("DESIGN_INVALID", "工具设计校验连续失败：" + str(errors[-1]), origin="design", retryable=False)

    def design(self, requirements, resources, errors):
        value = self.client.complete("Design an agent using declared capabilities. Reuse builtins first; generate 0-3 custom tools "
            "only for requested computations not covered by existing capabilities. Ordinary CSV queries need zero custom tools. "
            "Do not generate source yet. Tool IDs custom.<snake_case>.",
            {"requirements": requirements.to_dict(), "resources": catalog(resources, True), "builtins": builtin_catalog(), "trusted_tools": self.trusted_catalog(),
             "csv_contract": contract_catalog(),
             "design_repair_evidence": getattr(self, "redesign_evidence", None),
             "interpreter_capabilities": interpreter_capabilities(), "response_contract": response_contract(),
             "previous_validation_errors": errors, "previous_rejected_design": getattr(self, "last_design", None) if errors else None,
             "rules": "Generate instructions for requested behavior. Use custom_tools=[] when builtins suffice. "
                "Choose answer_style=facts for numeric lookups/rankings; analysis only when the user requires interpretation. "
                "Instructions describe reusable business rules, not hardcoded findings or assumptions from this snapshot. "
                "Set required=true only for a custom computation essential to the user's requirements. "
                "Common CSV queries may execute directly from structured inputs or one typed natural-language plan; complex goals retain the loop. "
                "A deterministic resource-level csv.profile is available and reused automatically for CSV snapshots; never generate a redundant quality tool. "
                "For EVERY custom tool declare execution_plan.algorithm and list the required helpers and AST node names in "
                "execution_plan.helpers/syntax. These must exist in interpreter_capabilities. Algorithms must use bounded loops "
                "within its execution limits. Do not require imports, regex libraries, datetime, key-based sorting or object methods. "
                "Implement equivalent simple computation using allowed operations or choose a different tool boundary; "
                "semantic policy interpretation belongs to runtime Codex. Do not weaken requested business behavior. "
                "Every custom tool needs at least two distinct legal inputs for independent normal/boundary tests. "
                "Declare error_cases with id/layer/input_json/code/description: at least one schema-invalid example at "
                "layer=host, code=INVALID_ARGUMENTS. Types, required and unknown parameters, and host size bounds are "
                "checked BEFORE code executes. NEVER require generated code to return a business error for a parameter "
                "type forbidden by its schema. Business error examples use layer=tool_result and schema-valid inputs, "
                "returning the declared output schema. These concrete examples are checked before source generation. "
                "Do not design a zero-parameter tool bound to one fixed resource. Declare optional text/rows fixtures with "
                "well-defined empty-input behavior, or another meaningful varying parameter. Runtime defaults still read authorized resources. "
                "Document agent uses documents.read/search to let runtime Codex reason over source; prefer a compact, deterministic "
                "evidence index (IDs, literal excerpts, deduplication or text structure counts). Let Codex interpret policy meaning. "
                "Generate a domain-specific rule parser only when the request explicitly requires it. CSV agent must include csv.query. "
                "Shares or period comparison may need custom business logic; ordinary grouping/filtering/aggregation needs none. Resource IDs are exact provided IDs. "
                "Custom output_schema_json uses type/properties/required/additionalProperties=false/items/anyOf; arrays need items. "
                "Every schema node MUST declare type (object/array/string/number/integer/boolean/null) or anyOf. "
                "output_schema_json must be syntactically valid JSON: close properties before adding required/additionalProperties. "
                "Do not use type arrays, untyped {}, $ref, oneOf or definitions. Example output: "
                '{"type":"object","properties":{"missing":{"type":"integer"}},"required":["missing"],"additionalProperties":false}. '
                "Custom parameters are declared flat named JSON values with name/type/required/description. Prefer one focused tool "
                "CSV custom computations should accept optional rows array for independent small-fixture tests; when omitted use dataset(id). "
                "to keep calls efficient. Runtime structured CSV inputs preserve overrides; natural goals may require multiple tools. "
                "Final CSV primary result_ref is an OBSERVATION ID from csv.query, never a tool/resource ID; CSV sources must be []. "
                "csv.query count always counts all filtered rows, including blank cells, and returns column=null; a valid input column is ignored. "
                "Runtime input_contract separates explicit user/human parameters from defaults. For natural goals defaults are fallbacks, "
                "not user constraints; infer missing filters/sort/limit/aggregation from the goal while preserving explicit values. "
                "Extra custom results are in observations. Document answers quote facts. "
                "Factory runs frozen tests; runtime should solve the user goal, not invent and replay a test suite on every run. "
                "Do not embed preview-derived totals or policy facts as fixed answers in instructions; derive them from live tool observations. "
                "Keep outputs compact and numerical when possible; avoid prose summaries in tool schemas and redundant reimplementations of csv.query. "
                "The tool description is its semantic contract: specify the algorithm for EVERY output field, exact label strings, "
                "array order and deduplication keys, empty/missing-input behavior, and conditions for optional outputs. "
                "Do not declare inferred conflicts or semantic categories unless their deterministic predicates and output values are fully defined. "
                "Pure tool outputs should be deterministic: numbers, flags, IDs and literal excerpts. Let the runtime model write explanations "
                "instead of introducing free-form claim/explanation fields whose wording is then compared by unit tests. "
                "Global quality profiling is already provided by csv.profile; raw aggregation is provided by csv.query. "
                "For resources with data_contract, field definitions and metrics are authoritative operator-provided rules. "
                "csv.metrics provides exact decimal aggregations, distinct counts, up to three dimensions and defined metric formulas. "
                "csv.sql provides bounded joins, multiple aggregates and windows with a declared table catalog; select it only if required. "
                "Do not rewrite these calculations as custom tools or invent missing business definitions. Ask for ambiguity at runtime. "
                "Generate only missing business capability. For actual custom tools use optional rows for independent fixtures. "
                "Use canonical raw input keys operation,column,group_by,filters,sort_by,descending,limit; dataset_id is the bound resource ID. "
                "Structured business input uses {metric_query:{metric_ids,dimensions,filters,sort_by,descending,limit}}. "
                "If a configured trusted tool helps the goal, select its exact ID in tools. Never generate or modify its implementation or configuration. "
                "Custom tools can process its observed result with observation(id). Use 6-8 model steps as a loop budget for complex goals; "
                "common queries do not need this many model calls. Host-managed fast paths and profiles can execute declared builtins."}, DESIGN_SCHEMA)
        self.last_design = value
        for tool in value["custom_tools"]:
            if "execution_plan" not in tool:
                raise ExecutionError("DESIGN_CAPABILITY", "新工具设计必须声明 execution_plan。", tool["id"],
                                     origin="design", retryable=False)
            if not tool["parameters"]:
                raise ExecutionError("DESIGN_TESTABILITY", "工具没有参数，无法构造两个不同的合法开发输入；"
                                     "请声明可变参数或有明确空值规则的可选 text/rows 测试输入。", tool["id"],
                                     origin="design", retryable=False)
            if "error_cases" not in tool:
                raise ExecutionError("DESIGN_ERROR_BOUNDARY", "新工具设计必须声明宿主与工具结果的错误归属样例。", tool["id"], origin="design")
        spec = decode_design(value)
        from dataclasses import replace
        spec = replace(spec, trusted_tools=[t for t in getattr(self, "trusted_tools", []) if t["id"] in spec.tools],
                       execution_policy={"adaptive_steps": True, "max_seconds": 900})
        if requirements.task_type == "csv_analysis":
            dataset = next(d for d in resources["datasets"] if d["id"] == requirements.defaults["dataset_id"])
            metric_tools = ["csv.metrics"] if dataset.get("data_contract", {}).get("metrics") else []
            if "csv.metrics" in spec.tools and not metric_tools:
                raise ExecutionError("DESIGN_CAPABILITY", "csv.metrics 需要用户提供已定义指标的数据契约。", origin="design", retryable=False)
            spec = replace(spec, tools=list(dict.fromkeys([*spec.tools, "csv.profile"])),
                           instructions=spec.instructions + "\n\n" + contract_instructions(),
                           execution_policy={**spec.execution_policy, "csv_strategy": "auto", "resource_profile": True,
                                             "csv_contract_version": CONTRACT_VERSION})
            spec = replace(spec, tools=list(dict.fromkeys([*spec.tools, *metric_tools])))
            if not spec.custom_tools and not spec.trusted_tools:
                spec = replace(spec, execution_policy={**spec.execution_policy, "output_template": "observed-v1",
                                                       "data_binding": "compatible-v1"})
        spec.validate(requirements)
        return spec

    def trusted_catalog(self):
        from .trusted_tools import tool_catalog
        return tool_catalog(getattr(self, "trusted_tools", []))

    def tests(self, requirements, resources, spec=None):
        suite = super().tests(requirements, resources)
        definitions = [{k: v for k, v in t.items() if k != "source"} for t in spec.custom_tools]
        self.test_attempts = list(getattr(self, "test_attempts", []))
        errors, previous = [], None
        for attempt in range(3):
            value = self.extra_tests(requirements, resources, definitions, errors, previous)
            previous = value
            try:
                candidate = self.decode_tests(value, suite, spec, requirements, resources)
                self.validate_generated_tool_coverage(candidate, definitions)
                return candidate
            except (ValueError, TypeError, KeyError, ExecutionError, CodexError) as exc:
                feedback = getattr(exc, "details", None)
                errors.append(feedback or str(exc)[:1000])
                self.test_attempts.append({"attempt": attempt + 1, "error": str(exc)[:1000], "feedback": feedback, "tests": value})
        raise ValueError("新增测试校验连续失败：" + str(errors[-1]))

    def extra_tests(self, requirements, resources, definitions, errors, previous):
        value = self.client.complete("Independently generate custom-tool examples and runtime goal scenarios BEFORE seeing source. "
            "Cover every declared custom tool in both development and acceptance, with concrete correct expected_json computed from provided "
            "resources and arguments using ONLY its frozen description and schemas. Do not invent unspecified output semantics. "
            "For EACH tool generate at least TWO development cases with distinct inputs: a representative normal fixture and "
            "a boundary/alternative fixture. EACH acceptance partition also needs at least ONE legal computation-result fixture. "
            "Host rejection and declared business-error cases DO NOT count toward either computation minimum. "
            "Development together must exercise every declared computation/output branch, not just "
            "one convenient topic. Acceptance changes input values while testing the same explicit contract. Cover empty/missing values "
            "when allowed by parameters; use literal excerpts instead of paraphrased expectations. Derive array ordering from the contract. "
            "Keep negative tests. For illegal parameter types, missing required/unknown fields or host bounds, "
            "set expected_json=null and expected_error={code:INVALID_ARGUMENTS,origin:decision,layer:host}. "
            "For legal inputs, expected_error=null and expected_json is the declared tool output, including business error results. "
            "These expectations are mutually exclusive. Preserve EVERY design error_cases example in BOTH partitions: "
            "host uses expected_error; tool_result uses legal input plus expected_json asserting its declared business-error result. "
            "Every tool/partition needs host rejection coverage independently of computation coverage. "
            "negative tests do not replace the TWO distinct legal development fixtures. Never loosen types or delete negatives to pass. "
            "For large datasets use parameter-supplied small fixtures if the contract permits; don't pretend a preview "
            "is the full dataset. No code or test edits. Generate one natural-language goal per partition. "
            "For CSV goal include explicit parameters_json and reference_query_json representing its expected primary csv.query; numeric "
            "reference_query_json permits ONLY operation,column,group_by,filters,sort_by,descending,limit. "
            "Do NOT include dataset_id (the oracle binds the frozen dataset), dataset, sort or other keys. "
            "answers will be calculated by the independent Decimal oracle. Use custom tools only when declared and relevant. "
            "If tools is empty, tool_cases MUST be []; goal scenarios still cover the requested behavior. "
            "For document goal use a different paraphrase with facts literally present in exact source_id, or refusal=true for absent facts. "
            "Document reference_query_json={}, parameters_json={}. Both partitions should differ in goal wording. IDs unique across all cases.",
            {"requirements": requirements.to_dict(), "tools": definitions, "resources": catalog(resources, True),
             "interpreter_capabilities": interpreter_capabilities(),
             "csv_contract": contract_catalog(),
             "previous_validation_errors": errors, "previous_rejected_tests": previous}, EXTRA_TESTS)
        return value

    @staticmethod
    def validate_generated_tool_coverage(suite, definitions):
        from .adaptive_testing import validate_tool_coverage
        validate_tool_coverage(suite, definitions)

    @staticmethod
    def decode_tests(value, suite, spec, requirements, resources):
        import copy
        value, suite = copy.deepcopy(value), copy.deepcopy(suite)
        for case in value["tool_cases"]:
            case["input"] = parse_json(case.pop("input_json"))
            expected = case.pop("expected_json", None)
            if expected is not None:
                case["expected"] = parse_json(expected)
            if case.get("expected_error") is None:
                case.pop("expected_error", None)
        for case in value["goal_cases"]:
            case["input"] = {"goal": case.pop("goal"), "parameters": parse_json(case.pop("parameters_json"))}
            case["reference_query"] = parse_json(case.pop("reference_query_json"))
            query = case["reference_query"]
            if requirements.task_type == "csv_analysis" and "dataset_id" in query:
                if query.pop("dataset_id") != requirements.defaults["dataset_id"]:
                    raise ValueError("参考查询不能改变绑定的数据集。")
        suite.update(value)
        from .adaptive_testing import validate_extra_suite
        validate_extra_suite(suite, spec, requirements, resources)
        return suite

    def code(self, requirements, spec, development, resources, tool_development=None):
        self.code_attempts = []
        if spec.execution_policy.get("output_template") == "observed-v1":
            self.validate_sources(requirements, spec, OUTPUT_SOURCE, resources)
            return {"spec": spec, "source": OUTPUT_SOURCE, "custom_sources": [],
                    "explanation": "复用 observed-v1 宿主输出模板；数值和口径来自实际工具观察，无需生成重复源码。"}
        errors, previous = [], None
        for attempt in range(3):
            value = self.client.complete(TOOL_CODE_RULES + " Return custom_sources with exactly the declared custom tool IDs, "
                "one complete source per ID; use [] when there are zero custom tools. Never put builtin tools or logic.py in custom_sources.",
                {"requirements": requirements.to_dict(), "agent": spec.to_dict(),
                 "csv_contract": contract_catalog(),
                 "expected_custom_ids": [t["id"] for t in spec.custom_tools],
                 "interpreter_capabilities": interpreter_capabilities(), "response_contract": response_contract(),
                 "development_cases": development, "tool_development_cases": tool_development or [], "resources": catalog(resources, True),
                 "previous_generation_errors": errors, "previous_rejected_generation": previous}, CODE_SCHEMA)
            try:
                generated_spec = apply_sources(spec, value)
                self.validate_sources(requirements, generated_spec, value["source"], resources)
                value["spec"] = generated_spec
                return value
            except (ValueError, TypeError, KeyError, ExecutionError) as exc:
                errors.append((getattr(exc, "location", "") + ": " + str(exc))[:1200])
                previous = value
                self.code_attempts.append({"attempt": attempt + 1, "error": errors[-1], "generation": value,
                                           "failure": exc.info() if isinstance(exc, ExecutionError) else {"origin": "source", "retryable": False}})
        raise ExecutionError("SOURCE_PREFLIGHT", "源码生成契约连续失败：" + errors[-1],
                             origin="source", retryable=False)

    def validate_sources(self, requirements, spec, source, resources):
        from .custom_tools import CustomTool
        spec.validate(requirements, built=True)
        if spec.execution_policy.get("output_template") == "observed-v1" and source != OUTPUT_SOURCE:
            raise ExecutionError("OUTPUT_CONTRACT", "宿主输出模板不能被生成或修复源码替换。", "logic.py", origin="source", retryable=False)
        for tool in spec.custom_tools:
            CustomTool(tool, resources)
        self.last_preflight = preflight_output_hook(source, requirements, resources)
        return self.last_preflight

    def diagnose(self, requirements, spec, source, failures):
        return self.client.complete("Analyze observed child-agent failures using actual decisions, tool results and generated source. "
            "Identify whether the problem is agent instructions, a tool implementation, its output schema or output code. "
            "Failure.origin=source and retryable=false mean the host has identified a generated source defect; repeated runtime "
            "decisions cannot fix it. Preserve the concrete error code, missing/extra fields and source location in your analysis. "
            "Failure.origin=external/configuration identifies trusted service/data/binding faults; never repair generated code "
            "to bypass these faults or alter operator bindings. Failure.origin=runtime with CODE_LIMIT before line 1 means "
            "the input envelope exceeds the host budget, not a proven source defect; choose design for compact results or explicit budget review. "
            "Use response_contract and interpreter_capabilities as authoritative. result_ref is decision-only; the hook has data/error. "
            "No final_response flag or undocumented context fields exist; never suggest inventing a host interface. "
            "Use real custom_tools/<name>.py line locations when given, or agent.json.instructions for decision-policy failures. "
            "Never invent DAG node locations. Explain concrete causes and repair directions in Chinese. "
            "Choose repair_scope=design when tool boundaries, parameter/output schemas, required tools or planning structure cause failures; "
            "choose code for implementation or instruction defects. Business acceptance criteria cannot change. "
            "Do not request design changes merely to evade a failing test.",
            {"requirements": requirements.to_dict(), "agent": spec.to_dict(), "source": source, "failures": failures,
             "csv_contract": contract_catalog(),
             "response_contract": response_contract(), "interpreter_capabilities": interpreter_capabilities()},
            object_schema({"summary": STR, "repair_scope": {"type": "string", "enum": ["code", "design"]},
                          "failure_points": {"type": "array", "items": object_schema({"location": STR, "cause": STR, "fix": STR}),
                                                              "minItems": 1, "maxItems": 12}}))

    def redesign(self, requirements, spec, failures, diagnosis, direction, resources):
        self.redesign_evidence = {"previous_design": {**spec.to_dict(), "custom_tools": [
            {k: v for k, v in t.items() if k != "source"} for t in spec.custom_tools]},
            "development_failures": failures, "diagnosis": diagnosis, "human_direction": direction,
            "rules": "Improve the architecture using development evidence. Original business tests remain immutable. "
                     "You may change tool IDs, schemas, mandatory flags and planning. Do not rename tools merely to evade failures. "
                     "When prose fields cause brittle unit failures, replace them with literal excerpts, numbers, flags and IDs; "
                     "runtime answers handle explanation. Simplify the tool contract instead of copying the previous design with more instructions. "
                     "Prefer a smaller focused design; no acceptance examples or independent benchmark answers are available."}
        try:
            return self.workflow(requirements, resources)
        finally:
            self.redesign_evidence = None

    def redesign_tests(self, requirements, resources, spec, frozen_suite, previous_spec):
        """Regenerate only interface-specific tests; preserve every business scenario."""
        import copy
        from .adaptive_testing import validate_extra_suite
        definitions = [{k: v for k, v in t.items() if k != "source"} for t in spec.custom_tools]
        old = {t["id"]: {k: v for k, v in t.items() if k != "source"} for t in previous_spec.custom_tools}
        unchanged = {t["id"] for t in definitions if old.get(t["id"]) == t}
        errors, previous = [], None
        for _ in range(3):
            value = self.extra_tests(requirements, resources, definitions, errors, previous)
            previous = value
            try:
                candidate = self.decode_tests(value, {"cases": frozen_suite["cases"]}, spec, requirements, resources)
                suite = copy.deepcopy(frozen_suite)
                suite["tool_cases"] = [copy.deepcopy(c) for c in frozen_suite["tool_cases"] if c["tool"] in unchanged]
                used = {c["id"] for k in ("cases", "goal_cases", "tool_cases") for c in suite.get(k, [])}
                for case in candidate["tool_cases"]:
                    if case["tool"] in unchanged:
                        continue
                    case["id"] = "redesign-" + case["id"]
                    while case["id"] in used:
                        case["id"] = "r-" + case["id"]
                    used.add(case["id"])
                    suite["tool_cases"].append(case)
                validate_extra_suite(suite, spec, requirements, resources)
                AdaptivePlanner.validate_generated_tool_coverage(suite, [t for t in definitions if t["id"] not in unchanged])
                return suite
            except (ValueError, TypeError, KeyError, ExecutionError, CodexError) as exc:
                errors.append(getattr(exc, "details", None) or str(exc)[:1000])
        raise ValueError("重新设计后的工具测试连续校验失败：" + str(errors[-1]))

    def repair(self, requirements, spec, source, failures, diagnosis, direction, development, resources):
        self.repair_attempts = []
        errors, previous = [], None
        for attempt in range(3):
            value = self.client.complete(TOOL_CODE_RULES + " Repair the observed failures in source and agent instructions. "
            "Keep tool IDs, descriptions, parameter/output schemas and required flags frozen; tests, requirements and resources cannot change. "
            "Runtime finish decisions must encode refusal as abstained=true, sources=[], result_ref=null; explain absence in answer. "
            "Fix refusal/selection behavior in agent instructions, never change these flags in logic.py. "
            "For CSV finish, sources MUST be []; raw query results reference csv.query, defined metric results reference csv.metrics. count counts filtered rows including blank cells and returns column=null. "
            "If execution_policy.output_template=observed-v1, preserve the exact input source; repair decision instructions only. "
            "Do not copy a diagnosis suggestion that conflicts with this host response contract. "
            "Use all development evidence to preserve previous passes. Return complete code for all tools, the output hook and updated instructions. "
            "custom_sources contains exactly the declared custom IDs, never builtin tools or logic.py.",
            {"requirements": requirements.to_dict(), "agent": spec.to_dict(), "source": source, "failures": failures,
             "expected_custom_ids": [t["id"] for t in spec.custom_tools],
             "interpreter_capabilities": interpreter_capabilities(), "response_contract": response_contract(),
             "previous_generation_errors": errors, "previous_rejected_generation": previous,
             "diagnosis": diagnosis, "human_direction": direction, "development_report": development, "resources": catalog(resources, True)},
            object_schema({**CODE_SCHEMA["properties"], "instructions": text_schema(8000)}))
            try:
                from dataclasses import replace
                instructions = value["instructions"]
                if requirements.task_type == "csv_analysis":
                    instructions += "\n\n" + contract_instructions()
                revised = replace(apply_sources(spec, value), instructions=instructions)
                if spec.execution_policy.get("output_template") == "observed-v1":
                    value["source"] = OUTPUT_SOURCE
                self.validate_sources(requirements, revised, value["source"], resources)
                return revised, value["source"], value["explanation"]
            except (ValueError, TypeError, KeyError, ExecutionError) as exc:
                errors.append((getattr(exc, "location", "") + ": " + str(exc))[:1200])
                previous = value
                self.repair_attempts.append({"attempt": attempt + 1, "error": errors[-1], "generation": value,
                    "failure": exc.info() if isinstance(exc, ExecutionError) else {"origin": "source", "retryable": False}})
        raise ExecutionError("SOURCE_PREFLIGHT", "修复源码契约连续失败：" + errors[-1], origin="source", retryable=False)
