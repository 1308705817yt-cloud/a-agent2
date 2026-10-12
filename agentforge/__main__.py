"""Command-line interface for AgentForge."""

import argparse
import json
import sys
from pathlib import Path

from .codex_client import CodexError
from .contracts import ExecutionError
from .factory import FactoryAgent
from .tool_registry import registry_catalog


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def summary(state):
    print("任务：" + state["run_id"] + " · " + state["status"] + " · v" + str(state["version"]))
    if state.get("delivery"):
        print("交付：" + state["delivery"]["kind"] + " · " + state["delivery"]["reason"])
    for key in ("report", "acceptance_report"):
        report = state[key]
        if report:
            print(("开发检查" if key == "report" else "保留验收") + "：" + str(report["passed"]) + "/" + str(report["total"]))
    if state["diagnosis"]:
        print_json(state["diagnosis"])
    if state["last_error"] or state["stop_reason"]:
        print(state["last_error"] or state["stop_reason"])
    if state["status"] == "waiting_for_guidance":
        print("继续：python -m agentforge resume " + state["run_id"] + ' --direction "修改方向"')
    if state["status"] in ("failed", "time_limit", "stopped"):
        print("重试：python -m agentforge retry " + state["run_id"])
        if state["status"] == "time_limit":
            print("可加 --max-seconds 0 取消总时间上限，或指定更大的累计秒数。")
        if state["status"] == "stopped":
            print("可加 --max-repairs 增加总修复轮次，--repair-scope design 选择重新设计。")
    if state["archive_path"]:
        print("产物：" + state["archive_path"])


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AgentForge — 按任务交付答案、确定性程序或 Agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="任务及产物工作区，默认当前目录")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", aliases=["solve"], help="solve 默认自动选择交付；create 保留显式 Agent 构建")
    create.add_argument("--request", required=True)
    create.add_argument("--mode", choices=("auto", "direct", "program", "agent"), help="solve 默认 auto；create 默认 agent")
    create.add_argument("--reusable", action="store_true", help="需要长期复用；auto 优先交付确定性程序")
    create.add_argument("--dynamic", action="store_true", help="重复使用时仍需理解新意图并动态选择工具")
    create.add_argument("--recipe", type=Path, help="确定性程序配方 JSON；提供后不调用模型规划")
    create.add_argument("--acceptance-cases", type=Path, help="独立的程序业务断言 JSON；不发送给模型，失败阻止导出")
    create.add_argument("--documents", action="append", default=[], help="TXT/MD/文档 JSON，可重复指定")
    create.add_argument("--csv", action="append", default=[], help="CSV 数据文件，可重复指定")
    create.add_argument("--data-contract", type=Path, help="字段类型、业务指标及兼容数据绑定契约 JSON")
    create.add_argument("--review", action="store_true", help="开发测试失败后等待人工修改方向")
    create.add_argument("--max-repairs", type=int, default=3)
    create.add_argument("--max-seconds", type=int, default=3600, help="构建累计耗时上限，0 表示不限；不限制 token")
    create.add_argument("--run-max-seconds", type=int, default=900, help="生成的 Agent 默认累计运行上限，0 表示不限")
    create.add_argument("--fixed-steps", action="store_true", help="关闭基于进展的自动决策扩展")
    resume = sub.add_parser("resume", help="恢复等待人工方向的任务")
    resume.add_argument("run_id")
    resume.add_argument("--direction", default="")
    retry = sub.add_parser("retry", help="重试模型或系统调用失败的任务")
    retry.add_argument("run_id")
    retry.add_argument("--max-seconds", type=int, help="调整构建累计耗时上限；0 表示不限")
    retry.add_argument("--run-max-seconds", type=int, help="调整开发/验收时子 Agent 的累计运行上限")
    retry.add_argument("--max-repairs", type=int, help="增加累计修复轮次，最多 20；原业务验收不变")
    retry.add_argument("--direction", default="", help="补充人工修改方向")
    retry.add_argument("--repair-scope", choices=("auto", "code", "design"), help="可选指定修复范围")
    recover = sub.add_parser("recover", help="运行排队或租约已过期的遗留任务")
    recover.add_argument("run_id")
    upgrade = sub.add_parser("upgrade-runtime", help="为未完成任务生成新运行时版本并重新验收")
    upgrade.add_argument("run_id")
    doctor = sub.add_parser("doctor", help="检查 Codex CLI 与登录")
    doctor.add_argument("--probe", action="store_true")
    serve_parser = sub.add_parser("serve", help="启动本机构建工作台")
    serve_parser.add_argument("--port", type=int, default=8787)
    for command in (create, resume, retry, recover, upgrade, doctor, serve_parser):
        command.add_argument("--model", help="账号可用模型；默认使用 CLI 默认模型")
        command.add_argument("--timeout", type=int, default=None, help="单次模型请求超时，默认 180 秒")
    show = sub.add_parser("show", help="查看完整任务状态与失败证据")
    show.add_argument("run_id")
    show.add_argument("--json", action="store_true")
    sub.add_parser("list", help="列出工作区任务")
    sub.add_parser("tools", help="显示工具注册表")
    check_tool = sub.add_parser("check-tool", help="调用已配置的可信只读工具，验证连接及契约")
    check_tool.add_argument("tool_id")
    check_tool.add_argument("--input-json", required=True)
    diff = sub.add_parser("diff", help="查看源码与工作流版本差异")
    diff.add_argument("run_id")
    diff.add_argument("--version", type=int)
    run = sub.add_parser("run", help="运行已交付的程序或 Agent")
    run.add_argument("run_id")
    run.add_argument("question", nargs="?")
    run.add_argument("--input-json", help="CSV 参数或文档输入 JSON")
    run.add_argument("--bindings-json", help="显式替换数据快照：{原数据集ID:新CSV路径}；仅兼容模板产物")
    run.add_argument("--model", help="覆盖子 Agent 使用的模型")
    run.add_argument("--timeout", type=int, default=None)
    run.add_argument("--max-seconds", type=int, help="覆盖累计运行耗时，0 表示不限")
    continuation = sub.add_parser("continue-execution", help="恢复子 Agent；等待输入时提交人工回复")
    continuation.add_argument("run_id")
    continuation.add_argument("execution_id")
    continuation.add_argument("--reply", help="人工文字回复")
    continuation.add_argument("--parameters-json", help="可选 CSV 参数补充")
    continuation.add_argument("--model")
    continuation.add_argument("--timeout", type=int, default=None)
    continuation.add_argument("--max-seconds", type=int, help="调整累计运行耗时，0 表示不限")
    benchmark = sub.add_parser("benchmark", help="独立陌生任务评测；run 调用真实 Codex")
    benchmark.add_argument("action", choices=("list", "run", "rescore"))
    benchmark.add_argument("--task", action="append", help="可重复指定任务 ID，默认全部")
    benchmark.add_argument("--suite", type=Path, help="独立评测集 JSON，默认 unseen_v1")
    benchmark.add_argument("--cohort", choices=("public_regression", "historical_regression", "novel"), default="public_regression")
    benchmark.add_argument("--run-id", help="复用相同需求和资源的已完成构建，仅限单个 task")
    benchmark.add_argument("--max-repairs", type=int, default=1)
    benchmark.add_argument("--task-workers", type=int, default=1, help="并行构建独立任务，1 至 4；各自持有独立后端和时限")
    benchmark.add_argument("--max-seconds", type=int, help="每个全新构建的累计时间上限")
    benchmark.add_argument("--run-max-seconds", type=int, help="每个子 Agent 的累计运行上限")
    benchmark.add_argument("--evaluate-generated", action="store_true", help="单独诊断冻结的未交付候选；通过不改变交付资格")
    benchmark.add_argument("--evaluation-id", help="rescore 复用已保存的回答，仅更新判分，保留原报告")
    benchmark.add_argument("--model")
    benchmark.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        model, timeout = getattr(args, "model", None), getattr(args, "timeout", None)
        if args.command in ("resume", "retry", "recover", "upgrade-runtime"):
            saved = FactoryAgent(args.workspace).load(args.run_id)
            model = model or saved["engine"]["model"]
            timeout = timeout or saved["engine"]["timeout"]
        factory = FactoryAgent(args.workspace, model=model, timeout=timeout or 180)
        if args.command in ("create", "solve"):
            from .resources import read_json
            state = factory.drive(factory.create(args.request, documents=args.documents, datasets=args.csv,
                                                  review=args.review, max_repairs=args.max_repairs, max_seconds=args.max_seconds,
                                                  run_max_seconds=args.run_max_seconds, adaptive_steps=not args.fixed_steps,
                                                  data_contract=read_json(args.data_contract) if args.data_contract else None,
                                                  delivery_mode=args.mode or ("auto" if args.command == "solve" else "agent"),
                                                  reusable=args.reusable, dynamic=args.dynamic,
                                                  recipe=read_json(args.recipe) if args.recipe else None,
                                                  acceptance_cases=read_json(args.acceptance_cases) if args.acceptance_cases else None))
            summary(state)
            if state["schema_version"] == 5 and "delivery_result" in state["frozen_hashes"]:
                print_json({"result": read_json(factory.run_dir(state["run_id"]) / "delivery_result.json"),
                            "validation": read_json(factory.run_dir(state["run_id"]) / "delivery_validation.json")})
            if state["status"] not in ("completed", "waiting_for_guidance"):
                parser.exit(1)
        elif args.command in ("resume", "retry", "recover", "upgrade-runtime"):
            state = (factory.resume(args.run_id, args.direction) if args.command == "resume" else
                     factory.retry(args.run_id, args.max_seconds, args.max_repairs, args.direction, args.repair_scope, args.run_max_seconds) if args.command == "retry" else factory.upgrade_runtime(args.run_id) if args.command == "upgrade-runtime" else factory.recover(args.run_id))
            summary(state)
            if state["status"] not in ("completed", "waiting_for_guidance"):
                parser.exit(1)
        elif args.command == "doctor":
            print_json(factory.planner.client.check_installation())
            if args.probe:
                print_json(factory.planner.client.probe())
        elif args.command == "serve":
            from .server import serve
            serve(args.workspace, port=args.port, model=model, timeout=timeout or 180)
        elif args.command == "show":
            state = factory.detail(args.run_id)
            print_json(state) if args.json else summary(state)
        elif args.command == "list":
            for state in factory.store.list():
                print(state["run_id"], state["status"], "v" + str(state["version"]), state["request"][:80])
        elif args.command == "tools":
            from .adaptive_runtime import builtin_catalog
            from .trusted_tools import load_definitions, tool_catalog
            print_json(builtin_catalog() + tool_catalog(load_definitions(factory.root)))
        elif args.command == "check-tool":
            from .trusted_tools import load_definitions, TrustedTools
            registry = TrustedTools(factory.root, load_definitions(factory.root))
            result = registry.invoke(args.tool_id, json.loads(args.input_json))
            print_json({"tool": args.tool_id, "result": result, "audit": registry.last_audit})
        elif args.command == "diff":
            state = factory.load(args.run_id)
            if state["schema_version"] == 5:
                if args.version not in (None, 1) or not state["version"]:
                    raise ValueError("交付版本不存在。")
                print_json({"delivery": state["delivery"], "artifact_hash": state["artifact_hash"],
                            "recipe": factory.detail(args.run_id).get("delivery_recipe")})
                return
            version = args.version or state["version"]
            if version not in {v["version"] for v in state["versions"]}:
                raise ValueError("版本不存在。")
            from .resources import read_json
            print_json(read_json(factory.run_dir(args.run_id) / "versions" / ("v" + str(version)) / "change.json"))
        elif args.command == "benchmark":
            from .benchmarking import benchmark_catalog, run_benchmark, rescore_benchmark
            if args.action == "list":
                print_json(benchmark_catalog(args.suite))
            else:
                if args.action == "rescore" and not args.evaluation_id:
                    raise ValueError("rescore 需要 --evaluation-id。")
                result = rescore_benchmark(factory, args.evaluation_id) if args.action == "rescore" else run_benchmark(factory, args.task, args.run_id, args.max_repairs, args.evaluate_generated, args.suite,
                                                                                                                  args.task_workers, args.max_seconds, args.run_max_seconds, args.cohort)
                print_json({k: result[k] for k in ("evaluation_id", "passed", "total", "all_passed", "delivered_tasks", "report_path")})
                if not result["all_passed"]:
                    parser.exit(1)
        elif args.command == "continue-execution":
            from .executions import continue_execution, execute_record
            if model or timeout:
                from .codex_client import CodexClient
                engine = factory.load(args.run_id)["engine"]
                factory.runtime_client_factory = lambda: CodexClient(factory.root, model or engine["model"], timeout or engine["timeout"])
            if args.parameters_json and not args.reply:
                raise ValueError("参数补充需要同时提供 --reply。")
            reply = {"text": args.reply, "parameters": json.loads(args.parameters_json) if args.parameters_json else {}} if args.reply else None
            record = continue_execution(factory, args.run_id, args.execution_id, reply, args.max_seconds)
            record = execute_record(factory, args.run_id, record["execution_id"])
            print_json(record)
            if record["status"] not in ("completed", "waiting_input"):
                parser.exit(1)
        elif args.command == "run":
            inputs = json.loads(args.input_json) if args.input_json else args.question or {}
            from .executions import create_execution, execute_record
            if model or timeout:
                from .codex_client import CodexClient
                engine = factory.load(args.run_id)["engine"]
                factory.runtime_client_factory = lambda: CodexClient(factory.root, model or engine["model"], timeout or engine["timeout"])
            record = create_execution(factory, args.run_id, inputs, args.max_seconds,
                                      bindings=json.loads(args.bindings_json) if args.bindings_json else None)
            record = execute_record(factory, args.run_id, record["execution_id"])
            print_json(record)
            if record["status"] not in ("completed", "waiting_input"):
                parser.exit(1)
    except (CodexError, ValueError, OSError, ExecutionError) as exc:
        parser.exit(2, "错误：" + str(exc) + "\n")


if __name__ == "__main__":
    main()
