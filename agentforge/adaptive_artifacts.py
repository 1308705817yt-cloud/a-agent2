"""Standalone entry points for generated Codex runtime agents."""

RUNNER = '''import argparse
import json
import sys
from pathlib import Path
from agentforge.adaptive_contracts import AdaptiveSpec
from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.codex_client import CodexClient
from agentforge.contracts import Requirements
from agentforge.resources import read_json, write_json, fingerprint

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description="Generated Codex agent: plan, act, observe, revise")
parser.add_argument("goal", nargs="?")
parser.add_argument("--input-json", help="JSON parameters or {goal, parameters}")
parser.add_argument("--model")
parser.add_argument("--timeout", type=int)
parser.add_argument("--max-seconds", type=int, help="Cumulative active runtime; 0 means unlimited")
parser.add_argument("--state-file", help="Save under .executions/ for explicit recovery")
parser.add_argument("--resume", action="store_true")
parser.add_argument("--reply", help="Human reply when waiting for input")
parser.add_argument("--parameters-json", help="Optional CSV parameter supplement")
args = parser.parse_args()
from verify_agent import verify_files
verify_files(root)
engine = read_json(root / "engine.json")
spec = AdaptiveSpec(**read_json(root / "agent.json"))
req = Requirements(**read_json(root / "requirements.json"))
agent = AdaptiveAgent(spec, req, read_json(root / "resources.json"), (root / "logic.py").read_text(encoding="utf-8"), root,
                      lambda: CodexClient(root, args.model or engine["model"], args.timeout or engine["timeout"]))
inputs = json.loads(args.input_json) if args.input_json else args.goal or {}
checkpoint, reply, state_path = None, None, None
if args.state_file:
    state_path = (root / args.state_file).resolve()
    if not state_path.is_relative_to(root / ".executions") or state_path.suffix != ".json":
        parser.error("--state-file must be a JSON file under .executions/")
if args.resume:
    if not state_path or args.goal or args.input_json:
        parser.error("--resume requires --state-file and uses its original input")
    saved = read_json(state_path)
    if fingerprint(saved["checkpoint"]) != saved["checkpoint_hash"]:
        parser.error("Checkpoint integrity mismatch")
    inputs, checkpoint = saved["input"], saved["checkpoint"]
elif state_path and state_path.exists():
    parser.error("State already exists; use --resume or another filename")
if args.reply:
    if not args.resume:
        parser.error("--reply requires --resume")
    reply = {"text": args.reply, "parameters": json.loads(args.parameters_json) if args.parameters_json else {}}
elif args.parameters_json:
    parser.error("--parameters-json requires --reply")
def save_checkpoint(value):
    if state_path:
        write_json(state_path, {"input": inputs, "checkpoint": value, "checkpoint_hash": fingerprint(value)})
output = agent.run(inputs, checkpoint=checkpoint, reply=reply, on_checkpoint=save_checkpoint, max_seconds=args.max_seconds)
output.pop("checkpoint", None)
print(json.dumps(output, ensure_ascii=False, indent=2))
raise SystemExit(1 if output.get("error") and not output.get("needs_input") else 0)
'''

VERIFIER = '''import argparse
import hashlib
import json
import sys
from pathlib import Path
from agentforge.adaptive_contracts import AdaptiveSpec
from agentforge.adaptive_runtime import AdaptiveAgent
from agentforge.adaptive_testing import tool_tests, evaluate_adaptive
from agentforge.codex_client import CodexClient
from agentforge.contracts import Requirements
from agentforge.resources import artifact_fingerprint, fingerprint, read_json

def verify_files(root):
    manifest = read_json(root / "manifest.json")
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("File integrity mismatch: " + name)
    for name, expected in manifest["frozen_hashes"].items():
        if fingerprint(read_json(root / (name + ".json"))) != expected:
            raise ValueError("Frozen contract mismatch: " + name)
    spec = read_json(root / "agent.json")
    source = (root / "logic.py").read_text(encoding="utf-8")
    if artifact_fingerprint(spec, source, manifest["runtime_hash"]) != manifest["artifact_hash"]:
        raise ValueError("Artifact fingerprint mismatch")
    for tool in spec["custom_tools"]:
        if (root / "custom_tools" / (tool["id"].split(".")[1] + ".py")).read_text(encoding="utf-8") != tool["source"]:
            raise ValueError("Custom source mismatch")
    return manifest

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Integrity and local tool checks; --live calls Codex for full re-evaluation")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = verify_files(root)
    spec = AdaptiveSpec(**read_json(root / "agent.json"))
    req, resources, suite = Requirements(**read_json(root / "requirements.json")), read_json(root / "resources.json"), read_json(root / "test_suite.json")
    reports = []
    if args.live:
        engine = read_json(root / "engine.json")
        agent = AdaptiveAgent(spec, req, resources, (root / "logic.py").read_text(encoding="utf-8"), root,
                              lambda: CodexClient(root, args.model or engine["model"], args.timeout or engine["timeout"]))
        reports = [evaluate_adaptive(agent, req, resources, suite, part, manifest["artifact_hash"]) for part in ("development", "acceptance")]
    else:
        for part in ("development", "acceptance"):
            cases = tool_tests(spec, req, resources, suite, part)
            reports.append({"partition": part, "passed": sum(c["passed"] for c in cases), "total": len(cases),
                            "all_passed": bool(cases) and all(c["passed"] for c in cases), "cases": cases})
    print(json.dumps({"integrity": "passed", "mode": "live_behavior" if args.live else "local_tools_only",
                     "reports": [{k: r[k] for k in ("partition", "passed", "total", "all_passed")} for r in reports]}, ensure_ascii=False, indent=2))
    return 0 if all(r["all_passed"] for r in reports) else 1

if __name__ == "__main__":
    raise SystemExit(main())
'''


def instructions(state):
    return "# " + state["spec"]["name"] + "\n\n" + state["requirements"]["summary"] + "\n\n" + '''需要 Python 3.11+、已安装并登录的 Codex CLI (`codex login`) 和可用额度。
构建和运行都调用真实 Codex；无本地模型替代模式。默认使用 CLI 默认模型，认证保留、用户配置隔离。

自然语言目标：`python -X utf8 run_agent.py "检查数据质量，然后分析各组总额"`

结构化 CSV 输入：`python -X utf8 run_agent.py --input-json '{}'`

更复杂目标：`python -X utf8 run_agent.py --input-json '{"goal":"你的目标","parameters":{}}'`

可选 `--model` / `--timeout`。结果包含 trace、observations、model_calls；预算耗尽、工具错误和缺少输入都有明确状态。
保存执行：`python -X utf8 run_agent.py "你的目标" --state-file .executions/session.json`
异常后恢复：`python -X utf8 run_agent.py --state-file .executions/session.json --resume`
人工接续：在上一条命令中加 `--reply "补充说明"`；CSV 可加 `--parameters-json`。
完成的工具观察不会重新执行；尚未提交的只读调用可能重读。未提交模型请求计入预算，恢复会请求新决策。
max_steps 是每段决策目标；启用 adaptive_steps 时，有新进展可自动延长，累计最多 3 倍，最多 5 次人工回复。
默认累计运行上限见 agent.json.execution_policy，可用 --max-seconds 调整；0 表示不限。人工等待不计时，不设 token 上限。
`custom_tools/*.py` 是按需求生成的业务源码，参数及输出契约见 agent.json；logic.py 仅整理最终答案。
源码在受约束 Python 解释器内执行，可处理参数、授权数据和已有观察；不支持任意导入、网络或操作系统访问。

`python -X utf8 verify_agent.py` 检查文件完整性和本地工具单测，不调用模型。
`python -X utf8 verify_agent.py --live` 使用真实 Codex 重跑开发及保留验收，会消耗额度；结果可能受模型变化影响。
构建时真实行为测试证据保存在 evaluation.json，不能把本地工具验证当作模型行为验收。
资源快照可能包含原文；运行时模型会收到授权资源内容及工具观察。
如选择可信工具，其固定 SQL/URL 与契约封存在 agent.json。SQLite 数据库不打包，部署时需在
trusted_data/ 指定路径准备数据库；HTTP 认证需设置配置引用的环境变量。密钥不写入导出包。
可信工具仅执行只读查询及 GET，返回值会送入模型；接入实时数据后的结果可能随数据变化。
'''
