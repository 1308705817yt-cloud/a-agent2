"""Explicit compatible snapshots; neither old artifacts nor their evaluations change."""

import copy
from pathlib import Path

from .data_contract import apply_dataset_contract
from .resources import fingerprint, prepare_resources

FORMAT = "agentforge-data-binding-v1"
OUTPUT_SOURCE = 'def run(context):\n    return context["result"]\n'


def eligible(spec, source):
    return (spec.task_type == "csv_analysis" and spec.execution_policy.get("data_binding") == "compatible-v1"
            and spec.execution_policy.get("output_template") == "observed-v1"
            and not spec.custom_tools and not spec.trusted_tools and source == OUTPUT_SOURCE)


def prepare_binding(spec, source, resources, paths):
    if not eligible(spec, source):
        raise ValueError("此产物未声明可复用的数据契约；需要使用宿主模板的新 CSV 构建。")
    if not isinstance(paths, dict) or not paths or any(not isinstance(k, str) or not isinstance(v, str) for k, v in paths.items()):
        raise ValueError("数据绑定需要 {原数据集ID:新CSV路径}。")
    original = {d["id"]: d for d in resources["datasets"]}
    if set(paths) - set(original):
        raise ValueError("只能替换产物已经授权的逻辑数据集。")
    rebound, records = copy.deepcopy(resources), []
    for rid, path in paths.items():
        old = original[rid]
        if not old.get("data_contract"):
            raise ValueError("跨数据复用需要原构建已声明字段类型和业务契约。")
        imported = prepare_resources(datasets=[path], data_contract={"format": "agentforge-data-contract-v1",
                                                                     "datasets": {Path(path).name: old["data_contract"]}})
        if imported["documents"] or len(imported["datasets"]) != 1:
            raise ValueError("每个绑定只能提供一份 CSV。")
        new = imported["datasets"][0]
        content_id = new["id"]
        # Stable logical ID keeps query plans reusable; content identity is audited separately.
        new["id"] = rid
        rebound["datasets"][next(i for i, d in enumerate(rebound["datasets"]) if d["id"] == rid)] = new
        records.append({"dataset_id": rid, "source_name": new["name"], "content_id": content_id,
                        "previous_hash": fingerprint(old), "resource_hash": fingerprint(new),
                        "contract_hash": fingerprint(old["data_contract"])})
    value = {"format": FORMAT, "base_resources_hash": fingerprint(resources), "resources": rebound,
             "bindings": records, "behavior_evaluation": "not_evaluated_on_new_data"}
    return {**value, "binding_hash": fingerprint(value)}


def bound_resources(spec, source, original, binding):
    if not eligible(spec, source):
        raise ValueError("产物不允许更换数据绑定。")
    keys = {"format", "base_resources_hash", "resources", "bindings", "behavior_evaluation", "binding_hash"}
    if (not isinstance(binding, dict) or set(binding) != keys or binding["format"] != FORMAT
            or binding["behavior_evaluation"] != "not_evaluated_on_new_data"):
        raise ValueError("数据绑定格式无效。")
    if binding["base_resources_hash"] != fingerprint(original) or binding["binding_hash"] != fingerprint({k: v for k, v in binding.items() if k != "binding_hash"}):
        raise ValueError("数据绑定与原始资源或内容指纹不一致。")
    result = binding["resources"]
    if not isinstance(result, dict) or set(result) != set(original) or result["documents"] != original["documents"]:
        raise ValueError("数据绑定不能改变文档资源或资源种类。")
    old = {d["id"]: d for d in original["datasets"]}
    if (not isinstance(result["datasets"], list)
            or any(not isinstance(d, dict) or not isinstance(d.get("id"), str) for d in result["datasets"])
            or [d["id"] for d in result["datasets"]] != list(old)):
        raise ValueError("数据绑定不能增加、删除或重排授权数据集。")
    records = binding["bindings"]
    record_keys = {"dataset_id", "source_name", "content_id", "previous_hash", "resource_hash", "contract_hash"}
    if (not isinstance(records, list) or not records
            or any(not isinstance(r, dict) or set(r) != record_keys
                   or any(not isinstance(v, str) or not v for v in r.values()) for r in records)
            or len({r["dataset_id"] for r in records}) != len(records)):
        raise ValueError("数据绑定审计无效或重复。")
    by_id = {r["dataset_id"]: r for r in records}
    if set(by_id) - set(old):
        raise ValueError("数据绑定审计含未授权数据集。")
    for dataset in result["datasets"]:
        previous, record = old[dataset["id"]], by_id.get(dataset["id"])
        if record is None:
            if dataset != previous:
                raise ValueError("数据改变却缺少绑定审计。")
            continue
        if (not previous.get("data_contract") or dataset.get("data_contract") != previous["data_contract"]
                or set(dataset) != set(previous) or dataset.get("columns") != previous["columns"]
                or record["source_name"] != dataset.get("name")
                or record["resource_hash"] != fingerprint(dataset)
                or record["previous_hash"] != fingerprint(previous)
                or record["contract_hash"] != fingerprint(previous["data_contract"])):
            raise ValueError("新数据必须满足冻结的完整字段与业务契约。")
        raw_columns = dataset["raw_columns"]
        if (not isinstance(raw_columns, list) or any(not isinstance(c, str) for c in raw_columns)
                or len(set(raw_columns)) != len(raw_columns) or set(raw_columns) != set(previous["raw_columns"])
                or not isinstance(dataset["raw_rows"], list)
                or any(not isinstance(row, dict) or set(row) != set(dataset["raw_columns"])
                       or any(not isinstance(v, str) for v in row.values()) for row in dataset["raw_rows"])):
            raise ValueError("绑定快照必须包含符合源列契约的原始 CSV 行。")
        raw = {"id": record["content_id"], "name": dataset["name"],
               "columns": dataset["raw_columns"], "rows": dataset["raw_rows"]}
        normalized = apply_dataset_contract(raw, previous["data_contract"])
        normalized["id"] = dataset["id"]
        if normalized != dataset:
            raise ValueError("绑定快照与原始行重新规范化后的结果不一致。")
    return copy.deepcopy(result)
