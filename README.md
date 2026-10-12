# AgentForge

**用 Codex 将任务需求与数据资源生成可运行、可验收、可导出的 Agent。**

面向文档问答和 CSV 数据分析，提供需求解析、工具选择、代码生成、失败修复、冻结验收和独立交付。已有能力优先复用，允许零自定义工具；每个产物保留资源快照、源码、版本和执行证据。

**版本 v0.12.0** · **Python 3.11+** · **运行时仅依赖 Python 标准库**

[快速开始](#快速开始) · [当前能力](#当前能力) · [验证与实验](#验证与实验) · [使用指南](docs/usage.md) · [版本记录](docs/version-history.md)

## 快速开始

在仓库根目录执行。构建 Agent 需要已安装的 Codex CLI 和可用登录；`doctor` 检查安装及登录状态。

```powershell
codex login
python -X utf8 -m agentforge doctor
python -X utf8 -m agentforge serve
```

打开 [本地工作台](http://127.0.0.1:8787)，填写需求、上传资源并开始构建。工作台可以查看源码、测试、版本差异，试运行 Agent 并下载 ZIP。

也可通过 `python -m pip install -e .` 安装 `agentforge` 命令。工作区默认是当前目录，可用子命令前的 `--workspace <目录>` 指定；端口使用 `serve --port 8788` 调整。

### 创建第一个 CSV Agent

显式使用仓库中的合成销售数据与业务契约：

```powershell
python -X utf8 -m agentforge create `
  --request "按 region 汇总 amount，支持筛选、排序和已定义的业务指标" `
  --csv examples/sales.csv `
  --data-contract examples/data_contract.example.json
```

构建完成后，将下面的 `<任务ID>` 替换为输出的任务 ID：

```powershell
python -X utf8 -m agentforge run <任务ID> --input-json '{}'
python -X utf8 -m agentforge run <任务ID> "金额最高的区域是哪一个？"
```

文档问答、指标查询、参数说明、数据绑定及失败接续见 [使用指南](docs/usage.md) 和 [业务契约说明](docs/agent-quality-contracts.md)。

构建和需要模型决策的运行会向 Codex 发送需求、授权资源内容或工具结果，并消耗额度。资源快照保存在本机，随独立 Agent 包一起导出。

## 当前能力

| 能力 | 支持范围 |
| --- | --- |
| 文档问答 | TXT、Markdown、文档 JSON；搜索与分页读取，回答引用实际读到的原文 |
| CSV 基础分析 | 筛选、分组、sum / mean / count / min / max、排序和条数限制 |
| 数据契约与指标 | 字段映射、类型、空值、精度、日期时区、主键；精确十进制指标、去重、比值和多维分组 |
| 关系查询 | 授权 CSV 上的只读 SQL；通过显式配置的可信工具查询 SQLite 或读取 HTTP JSON |
| 按需生成工具 | 复用内置能力，允许 0–3 个自定义工具，源码在有预算的 Python 子集解释器中执行 |
| 验收与修复 | 冻结开发和保留验收场景；按失败证据修复源码或工具设计，保留验收失败阻止导出 |
| 恢复与复用 | 保存执行检查点、人工补充和版本指纹；资源档案缓存；兼容 CSV 可显式绑定新快照 |

### 三种执行路径

| 输入与任务 | 执行方式 | 运行时模型调用 |
| --- | --- | --- |
| 符合快速路径条件的结构化 CSV / 指标输入 | 宿主直接计算并核对结果 | 0 次 |
| 一次查询可完成的自然语言 CSV 目标 | 生成一个查询计划，再由宿主执行 | 正常完成时 1 次 |
| 复杂分析、SQL、实时工具或必需自定义计算 | 规划 → 工具执行 → 观察 → 校验与调整 | 按任务需要多次调用 |

计划冲突或执行失败时可转入决策循环；歧义会请求补充。实际路径、工具观察和调用次数写入执行记录，详见 [路由说明](docs/structural-query-routing.md)。

## 独立交付

验收通过后生成 `dist/<任务ID>/` 和对应 ZIP，包含 Agent 配置、资源快照、必要的工具源码、冻结运行时、测试报告和完整性清单。

在解压目录执行：

```powershell
python -X utf8 run_agent.py --input-json '{}'
python -X utf8 run_agent.py "你的问题"
python -X utf8 verify_agent.py
```

符合快速路径条件的结构化 CSV 查询可以在没有 Codex CLI 的环境中运行。自然语言规划和决策循环需要可用的 Codex CLI；`verify_agent.py` 只检查完整性和本地工具，`--live` 才调用模型重新验收。零自定义工具的本地工具检查标记为不适用。

## 验证与实验

| 验证记录 | 已记录结果 | 证据范围 |
| --- | --- | --- |
| [第 1 阶段架构验证](docs/phase1-architecture-validation.md)（2026-10-12） | **319/319 通过**，其中新增 76 项；零跳过 | 模拟模型响应，实际运行宿主、SQLite、工厂验收、绑定与独立导出 |
| [15 个路由样例](docs/phase1-architecture-validation.md#15-个路由样例) | 三种路径各 5 个，全部通过 | 检查执行路径、调用次数、工具和计算结果 |
| [历史真实 Codex 对照](docs/csv-comparison-v1.md) | 首轮直接方案 12/12、Agent 3/12；随后配对接续均 9/9 | 合成 CSV 任务；服务中断、未执行与计算结果分别记录 |

架构验证通过不等于真实模型准确率或交付率提高。历史对照中，配对九题的直接方案用了 9 次请求、4.78 分钟，Agent 用了 21 次请求、15.60 分钟，另有构建成本；尚无新架构的真实模型成本或隐藏任务对照结果。

运行完整本地验证，不消耗模型额度：

```powershell
python -X utf8 -B tests/run_phase1_validation.py
```

报告与日志写入 `.agentforge/validation/`。CI 配置覆盖 Windows / Linux、Python 3.11 / 3.12；前端检查需要 Node。已保存的 [机器可读结果](docs/verification/phase1-architecture-results.json) 包含环境、路由结果和源码指纹。

## 当前边界

基础 `csv.query` 使用浮点数，精确金额应使用已定义口径的 `csv.metrics`；SQL 执行成功不等于业务语义正确。数据库查询目前作为可信工具接入，尚无独立的 `database_qa` 构建与验收流程。PDF/OCR、任意 Python 或系统操作、多用户部署不在当前支持范围内。

旧产物继续使用自己的冻结运行时。新数据的契约兼容检查不会继承原数据上的业务验收结论。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [使用指南](docs/usage.md) | 启动、文档和 CSV 示例、参数、导出、配置与接续 |
| [业务契约与数据复用](docs/agent-quality-contracts.md) | 规范化、精确指标、SQL、绑定及证据边界 |
| [架构与执行边界](docs/architecture.md) | 工厂流程、执行层与验收约束 |
| [开发与故障恢复](docs/development.md) | 本地检查、状态、运行时升级及评测维护 |
| [可信工具接入](docs/tool-integrations.md) | SQLite / HTTP 工具的配置、权限和契约 |
| [版本与历史验证](docs/version-history.md) | v0.6–v0.12 更新、实验结果和原始证据入口 |
