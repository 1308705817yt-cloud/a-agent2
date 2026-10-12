"""Small hand-calculated fixtures; no live model and no production fallback."""

import copy
from agentforge.adaptive_contracts import AdaptiveSpec
from agentforge.contracts import Requirements
from agentforge.data_contract import VERSION
from agentforge.resources import prepare_resources

CSV = ("id,region,amount,quantity,status,date\n"
       "001,East,0.10,1,paid,2026-01-01\n"
       "002,East,0.20,2,paid,2026-01-02\n"
       "003,West,10.00,0,refund,2026-01-03\n"
       "004,West,,3,paid,2026-01-04\n"
       "005,East,-0.10,1,paid,2026-01-05\n")


def definition():
    return {"columns": [
        {"name": "id", "type": "text", "nullable": False},
        {"name": "region", "type": "text"},
        {"name": "amount", "type": "decimal", "scale": 2, "unit": "元"},
        {"name": "quantity", "type": "integer"},
        {"name": "status", "type": "text"},
        {"name": "date", "type": "date"}],
        "primary_key": ["id"], "dimensions": ["region", "status"], "metrics": [
            {"id": "revenue", "description": "已支付金额含负值，空值跳过", "operation": "sum", "column": "amount", "scale": 2,
             "filters": [{"column": "status", "operator": "eq", "value": "paid"}]},
            {"id": "total", "description": "全部金额", "operation": "sum", "column": "amount", "scale": 2},
            {"id": "units", "description": "件数", "operation": "sum", "column": "quantity", "scale": 0},
            {"id": "orders", "description": "行数", "operation": "count"},
            {"id": "regions", "description": "去重区域", "operation": "count_distinct", "column": "region"},
            {"id": "average", "description": "金额均值", "operation": "mean", "column": "amount", "scale": 2},
            {"id": "minimum", "description": "最小金额", "operation": "min", "column": "amount", "scale": 2},
            {"id": "maximum", "description": "最大金额", "operation": "max", "column": "amount", "scale": 2},
            {"id": "per_unit", "description": "每件支付金额", "scale": 4,
             "formula": {"operation": "divide", "left": "revenue", "right": "units"}},
            {"id": "unpaid", "description": "非支付金额", "scale": 2,
             "formula": {"operation": "subtract", "left": "total", "right": "revenue"}}]}


def fixture(content=CSV, contract=None):
    contract = contract if contract is not None else definition()
    resources = prepare_resources(uploads=[{"name": "sales.csv", "content": content}],
                                  data_contract={"format": VERSION, "datasets": {"sales.csv": contract}})
    defaults = {"dataset_id": resources["datasets"][0]["id"], "operation": "sum", "column": "amount",
                "group_by": "region", "filters": [], "sort_by": "value", "descending": True, "limit": None}
    req = Requirements("按区域汇总金额", "csv_analysis", "金额分析", ["计算准确", "遵守口径"], defaults)
    spec = AdaptiveSpec("销售助手", "csv_analysis", "按冻结数据契约回答，复杂需求使用 SQL。", ["计算并核对"],
                        ["csv.query", "csv.metrics", "csv.sql", "csv.profile", "csv.preview"], [],
                        execution_policy={"csv_strategy": "auto", "resource_profile": True,
                                          "output_template": "observed-v1", "data_binding": "compatible-v1"},
                        answer_style="facts")
    return req, resources, spec


def plan(req, action="execute", overrides=None):
    value = {"action": action, "query": None, "metric_query": None, "question": "", "reason": "fixture plan"}
    if action == "execute":
        value["query"] = copy.deepcopy(req.defaults) | (overrides or {})
    elif action == "metrics":
        value["metric_query"] = {"dataset_id": req.defaults["dataset_id"], "metric_ids": ["revenue"],
                                 "dimensions": [], "filters": [], "sort_by": None, "descending": False,
                                 "limit": None} | (overrides or {})
    return value
