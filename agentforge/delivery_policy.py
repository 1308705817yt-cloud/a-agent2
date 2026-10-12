"""Choose a deliverable from explicit use, without a model call or keyword guessing."""

MODES = ("auto", "direct", "program", "agent")


def select_delivery(mode="auto", reusable=False, dynamic=False, recipe=None):
    if mode not in MODES or type(reusable) is not bool or type(dynamic) is not bool:
        raise ValueError("交付方式应为 auto/direct/program/agent；复用和动态决策选项必须是布尔值。")
    if recipe is not None and not isinstance(recipe, dict):
        raise ValueError("程序配方必须是 JSON 对象。")
    if dynamic and recipe is not None:
        raise ValueError("固定程序配方不能同时要求每次动态选择工具。")
    if mode == "direct" and reusable:
        raise ValueError("直接回答不提供可复用程序；请选择 auto/program/agent。")
    if mode == "program" and dynamic:
        raise ValueError("程序执行不调用模型；动态决策需求请选择 auto/agent。")
    if mode == "agent" and recipe is not None:
        raise ValueError("固定配方属于 program 交付，不能在 Agent 构建中忽略它。")
    if mode != "auto":
        kind, reason = mode, "用户明确指定交付方式。"
    elif not reusable:
        kind, reason = "direct", "一次性任务直接求解，无需先构建可复用 Agent。"
    elif dynamic:
        kind, reason = "agent", "重复任务需要在运行时理解新意图并动态选择工具。"
    else:
        kind, reason = "program", "重复任务优先冻结参数和计算规则，运行时不调用模型。"
    return {"kind": kind, "requested_mode": mode, "reusable": reusable, "dynamic": dynamic,
            "reason": reason, "policy_version": "delivery-v1"}
