"""Codex planning, independent test generation, diagnosis and source revisions."""

import copy
import json
import re

from .codex_client import object_schema, text_schema
from .contracts import AgentSpec, Requirements
from .resources import catalog
from .tool_registry import registry_catalog
from .testing import add_integrity_cases, validate_suite


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


STR = text_schema(1000)
BOOL = {"type": "boolean"}
DEFAULTS = object_schema({"dataset_id": nullable(STR), "operation": nullable({"type": "string", "enum": ["sum", "mean", "count", "min", "max"]}),
                          "column": nullable(STR), "group_by": nullable(STR), "filters_json": text_schema(4000),
                          "sort_by": {"type": "string", "enum": ["group", "value", "count"]}, "descending": BOOL,
                          "limit": nullable({"type": "integer", "minimum": 1, "maximum": 1000})})
REQ_SCHEMA = object_schema({"supported": BOOL, "task_type": {"type": "string", "enum": ["document_qa", "csv_analysis"]},
                            "summary": STR, "acceptance_criteria": {"type": "array", "items": STR, "minItems": 3, "maxItems": 12},
                            "defaults": DEFAULTS})
NODE = object_schema({"id": STR, "tool": STR, "arguments_json": text_schema(4000),
                      "next": nullable(STR), "on_empty": nullable(STR), "on_error": nullable(STR)})
SPEC_SCHEMA = object_schema({"name": STR, "task_type": {"type": "string", "enum": ["document_qa", "csv_analysis"]},
                             "tools": {"type": "array", "items": STR, "minItems": 2, "maxItems": 12}, "entry": STR,
                             "nodes": {"type": "array", "items": NODE, "minItems": 2, "maxItems": 32},
                             "max_steps": {"type": "integer", "minimum": 2, "maximum": 64}})
CASE_SCHEMA = object_schema({"id": STR, "partition": {"type": "string", "enum": ["development", "acceptance"]},
                             "purpose": STR, "criterion": STR, "input_json": text_schema(4000),
                             "expect": {"type": "string", "enum": ["answer", "refusal", "error", "csv"]},
                             "source_id": nullable(STR), "facts": {"type": "array", "items": STR, "maxItems": 8}})
TEST_SCHEMA = object_schema({"cases": {"type": "array", "items": CASE_SCHEMA, "minItems": 8, "maxItems": 40}})
CODE_RULES = """Generate actual logic.py source with exactly def run(context). The context contains input and result;
result is a response dict {answer:string,data:object|null,sources:list,abstained:bool,error:object|null}.
Return a dict with exactly these five keys. Implement request-specific output formatting and relevant facts;
preserve authoritative sources, data and errors, and never hardcode test answers.
Do NOT duplicate input validation or constrain inputs to the request defaults: registered tools already validate.
CSV input overrides support all sum/mean/count/min/max, group_by=None, column=None for count, sort_by=group/value/count,
descending boolean, limit=None or any integer 1..1000, and filters. In Python 1 == True: do not mistake limit=1 for a bool.
CSV data has dataset_id,operation,column,group_by,results:[{group:string|null,value:number|null,count:integer}].
Format according to actual data values, not hardcoded dataset IDs, columns or default grouping. Empty results must work.
On error, immediately return the original response unchanged. On refusal, preserve abstained=true and empty sources.
For document success use the retrieved original text and source citations; do not append unsourced facts.
Document retrieval is lexical: shared generic words do not establish that a question's subject is covered.
When documents.search uses topic_aliases, rely on that source-derived routing; don't add a second narrower
keyword classifier in Python that breaks synonyms. Your code should format original evidence and citations.
Without topic routing you may use salient source topics to reject unsupported subjects. You may turn irrelevant
retrieved matches into refusal with an explicit answer, data=None,sources=[],abstained=True,error=None.
Preserve supported source facts and citations; do not hardcode answers or exact test questions.
Supported Python: assignments, subscript assignment, if/else, for, break/continue, return, constants,
list/dict/tuple, arithmetic (+ - * / // %), comparisons, boolean operations, slicing, conditional expressions.
Helpers ONLY len/str/int/float/bool/round/abs/min/max/sum/sorted/get/append/join/split/lower/upper.
get(dict,key,default=None), append(list,value), join(list_of_strings,separator), split(text,separator=None).
No attributes or methods, imports, comprehensions, f-strings, while, try, def helpers, keyword arguments, exec,
filesystem, network, subprocess. Build text using str, join and +. Runtime has 50000 steps and 120000 character data budget.
"""


def decode_spec(value):
    nodes = []
    for node in value["nodes"]:
        nodes.append({k: v for k, v in node.items() if k != "arguments_json"} | {"arguments": json.loads(node["arguments_json"])})
    return AgentSpec(**(value | {"nodes": nodes}))


class Planner:
    def __init__(self, client):
        self.client = client

    def requirements(self, request, resources):
        value = self.client.complete("Understand the request and choose one supported task. Do not silently drop requirements. "
            "Only document QA (extractive source-grounded answers with refusal) or CSV analysis (filters, grouping, sum/mean/count/min/max, "
            "sort and limit). No PDFs, semantic embeddings, online LLM in the exported runtime, ML, charts, cross-dataset joins or arbitrary Python. "
            "If any essential demand cannot be achieved, supported=false and explain in summary. "
            "CSV defaults must bind an exact provided dataset ID and requested operation/columns/filters/sort/limit; use [] for empty filters_json. "
            "Document defaults use null dataset/operation/column/group/limit, [] filters, sort_by=group descending=false. "
            "Acceptance criteria must describe the actual requested behavior in Chinese.",
            {"request": request, "resources": catalog(resources, True)}, REQ_SCHEMA)
        if not value["supported"]:
            raise ValueError("需求超出当前能力：" + value["summary"])
        defaults = value["defaults"]
        defaults["filters"] = json.loads(defaults.pop("filters_json"))
        if value["task_type"] == "document_qa":
            defaults = {}
        req = Requirements(request, value["task_type"], value["summary"], value["acceptance_criteria"], defaults)
        req.validate(resources)
        return req

    def workflow(self, requirements, resources):
        value = self.client.complete("Select the minimal required registered tools and construct a dynamic acyclic workflow for the request. "
            "Every node has id/tool/arguments_json/next/on_empty/on_error; null means absent edge. Arguments JSON may use "
            '{"ref":"input.column","default":"amount"}. Only input.<field> references allowed. CSV load dataset_id must be a literal exact frozen ID. '
            "CSV supports input operation/column/group_by/filters/sort_by/descending/limit overrides, using frozen defaults. Include filter,sort,limit "
            "so overrides are honored even if defaults are empty. Document search question must use input.question. "
            "Respect registry input/output types on each edge. on_empty is only allowed on matches/table/analysis output nodes, never response nodes. "
            "Document score is lexical query-token coverage, not semantic confidence: prefer a moderate threshold around 0.2. "
            "For document QA configure topic_aliases as {exact_document_id:[salient topic terms and reasonable synonyms]}. "
            "Use only resource-derived subjects and synonyms, not exact test questions. Provide around 25-35 useful aliases per source: "
            "include the canonical short subject, formal and colloquial synonyms, common abbreviations, and English equivalents. "
            "Also include salient concrete concepts from source content, using short noun terms, not only document-title paraphrases. "
            "Cover ways users refer indirectly to the topic through its objects or facts; avoid generic temporal/administrative words. "
            "Include common paraphrases such as annual leave versus yearly vacation if relevant. "
            "Do not require compound phrases when a precise short topic word suffices. Avoid generic terms like employee/policy/制度/申请 "
            "that apply to unrelated subjects. "
            "Routing with topic_aliases only returns documents with a matched theme, scoring 1.0; no theme match triggers refusal. "
            "Empty matches may branch to documents.refuse. Tool errors may branch to output.error. "
            "All success/empty/error branches that continue must finish a response followed by code.transform, which is required even for refusals. "
            "Use null on_error on code.transform. Selected tools must exactly equal the unique tools used in reachable nodes.",
            {"requirements": requirements.to_dict(), "resources": catalog(resources, True), "registry": registry_catalog()}, SPEC_SCHEMA)
        return decode_spec(value)

    def tests(self, requirements, resources):
        schema = copy.deepcopy(TEST_SCHEMA)
        schema["properties"]["cases"]["items"]["properties"]["expect"]["enum"] = (["csv"] if requirements.task_type == "csv_analysis" else ["answer", "refusal", "error"])
        task = ("Independently generate requirement-specific tests BEFORE seeing implementation. At least 4 different inputs "
            "in EACH of development and acceptance; include positive, negative, boundary scenarios linked to acceptance criteria. "
            "Document cases expect answer/refusal/error; positive source_id is exact provided doc ID and facts must be literal substrings of content. "
            "Refusal questions must concern absent information with distinct vocabulary; invalid input uses empty question. "
            "CSV cases expect csv, source_id=null,facts=[]; use varied valid overrides, filters, grouping, count and invalid columns/operation. "
            "Do NOT supply numeric expected answers: a separate Decimal oracle will compute them from the full dataset. "
            "input_json must encode an object. CSV input permitted keys operation,column,group_by,filters,sort_by,descending,limit; "
            "filters contain column/operator/value (eq,ne,gt,gte,lt,lte,contains). No question key for CSV. "
            "Case IDs use unique ASCII letters/digits/hyphens/underscores, at most 80 characters; do not use the integrity_ prefix. "
            "Development and reserved acceptance must differ; use document paraphrases for acceptance. Outputs should be Chinese.")
        self.test_attempts = []
        errors, previous = [], None
        for attempt in range(3):
            value = self.client.complete(task, {"requirements": requirements.to_dict(), "resources": catalog(resources, True),
                "previous_validation_errors": errors, "previous_rejected_tests": previous}, schema)
            raw = copy.deepcopy(value)
            try:
                used = set()
                for i, case in enumerate(value["cases"]):
                    original_id = case["id"]
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", original_id) or original_id.startswith("integrity_") or original_id in used:
                        case["generated_id"] = original_id
                        case["id"] = case["partition"] + "-generated-" + str(i + 1)
                        while case["id"] in used:
                            case["id"] = "g-" + case["id"]
                    used.add(case["id"])
                    case["input"] = json.loads(case.pop("input_json"))
                validate_suite(value, requirements, resources)
                return add_integrity_cases(value, requirements, resources)
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc)[:1200])
                previous = raw
                self.test_attempts.append({"attempt": attempt + 1, "error": errors[-1], "tests": raw})
        raise ValueError("业务测试生成连续校验失败：" + errors[-1])

    def code(self, requirements, spec, development, resources):
        value = self.client.complete(CODE_RULES, {"requirements": requirements.to_dict(), "workflow": spec.to_dict(),
            "development_cases": development, "resources": catalog(resources, True)}, object_schema({"source": text_schema(16000), "explanation": STR}))
        return value

    def diagnose(self, requirements, spec, source, failures):
        return self.client.complete("Analyze the observed build/runtime failures. Explain concrete failure points and a repair direction in Chinese. "
            "Use actual node IDs or logic.py line numbers from evidence; don't invent locations. Tests, resource snapshots and requirements are frozen.",
            {"requirements": requirements.to_dict(), "workflow": spec.to_dict(), "source": source, "failures": failures},
            object_schema({"summary": STR, "failure_points": {"type": "array", "items": object_schema({"location": STR, "cause": STR, "fix": STR}), "minItems": 1, "maxItems": 12}}))

    def repair(self, requirements, spec, source, failures, diagnosis, direction, development, resources):
        value = self.client.complete(CODE_RULES + " Repair actual source code and optionally change the workflow. Return full new source. "
            "The workflow uses the same schema and constraints as before: DAG, registered tools, input/output kind compatibility, "
            "all reachable success paths end with code.transform. on_empty ONLY on matches/table/analysis output nodes, never response nodes. "
            "code.transform has null next/on_empty/on_error. CSV dataset_id is the frozen literal ID; honor input overrides for every parameter. "
            "Document score is lexical query-token coverage, not semantic confidence. Use retrieval candidates' observed scores to set a threshold "
            "that admits supported questions while refusing out-of-vocabulary questions. No changes to tests, requirements or resources. "
            "Fix failures while preserving previously passing development cases. Avoid oscillating threshold changes: if supported and unsupported "
            "questions overlap in lexical scores, prefer source-derived topic_aliases rather than only raising/lowering the threshold. "
            "Retain useful source-topic synonyms when repairing; don't delete supported aliases to silence one failing case. "
            "Human direction is optional and cannot override the frozen contract. If workflow unchanged still return it in full.",
            {"requirements": requirements.to_dict(), "workflow": spec.to_dict(), "source": source,
             "failures": failures, "diagnosis": diagnosis, "human_direction": direction, "registry": registry_catalog(),
             "development_report": development, "resources": catalog(resources, True)},
            object_schema({"source": text_schema(16000), "workflow": SPEC_SCHEMA, "explanation": STR}))
        return decode_spec(value["workflow"]), value["source"], value["explanation"]
