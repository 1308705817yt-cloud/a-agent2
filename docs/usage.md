# 使用指南

[返回首页](../README.md) · [业务契约与数据绑定](agent-quality-contracts.md) · [故障恢复](development.md)

以下命令在仓库根目录执行。`<任务ID>`、`<执行ID>` 是需要替换的占位符。

## 启动

需要 Python 3.11+、已安装并登录的 Codex CLI。运行时只依赖 Python 标准库。

```powershell
codex login
python -X utf8 -m agentforge doctor
python -X utf8 -m agentforge serve
```

打开 **http://127.0.0.1:8787**，填写需求、上传资源并开始构建。控制台展示真实任务状态、工具工作流、源码、测试证据和版本差异，并提供试运行与 ZIP 下载。

`doctor` 检查安装与登录；需要真实请求探测时显式加 `--probe`，该选项会调用模型。

也可以安装命令行入口：

```powershell
python -m pip install -e .
agentforge serve
```

工作区默认为当前目录；全局参数 `--workspace <目录>` 应放在子命令前。端口可通过 `serve --port 8788` 调整。

构建时需求、文档内容预览、CSV 表头和前 12 行、开发失败证据会发送到当前 Codex 服务。运行时模型还会收到工具结果和读取的文档片段。完整资源快照保存在本机并随独立包导出。示例文件为合成数据；系统不会自动使用示例资源。

## 命令行工作流

### 文档问答

```powershell
python -X utf8 -m agentforge create --request "基于上传的虚构测试制度回答问题，摘取原文并引用来源，没有证据时拒答" --documents examples/synthetic_documents.json --review
```

支持 TXT、Markdown 和文档 JSON。JSON 格式：

```json
[
  {"id": "policy-1", "title": "政策", "content": "可引用的原文。"}
]
```

也接受 `{"documents": [...]}`。子 Agent 可以先搜索或分页读取原文，再由 Codex 理解问题、调用生成的文档工具并组织回答。引用必须来自实际读取的片段且可在原文核对。支持语义改写；PDF/OCR 和向量检索尚未实现。来源校验不能保证所有问题的语义回答正确。

### CSV 分析

```powershell
python -X utf8 -m agentforge create --request "检查 amount 空值及总额，再按 region 汇总 amount，按金额降序；优先复用内置能力" --csv examples/sales.csv
```

基础查询支持单个绑定数据集的筛选、分组、聚合、排序与数量限制。质量档案由 `csv.profile` 提供，业务契约指标与关系查询分别使用 `csv.metrics`、`csv.sql`；仅为已有能力无法完成的计算生成自定义工具。运行可以使用自然语言目标或 JSON 参数：

```powershell
python -X utf8 -m agentforge run <任务ID> --input-json '{"operation":"sum","column":"amount","group_by":"region","sort_by":"value","descending":true,"limit":2}'
python -X utf8 -m agentforge run <任务ID> "检查金额缺失情况，再比较各地区总额"
```

| 参数 | 可用值 |
| --- | --- |
| operation | sum / mean / count / min / max |
| column | 数值列；count 的结果固定为 null（兼容有效输入列名） |
| group_by | 列名或 null，null 表示不分组 |
| filters | `[{"column":"region","operator":"eq","value":"East"}]` |
| 筛选运算符 | eq / ne / gt / gte / lt / lte / contains |
| sort_by | group / value / count |
| descending | true / false |
| limit | null 或 1 至 1000 的整数 |

空数值单元格不参与基础数值聚合；count 计筛选后的行数。文本比较区分大小写。每组查询结果包含 group、value、count。查询使用浮点聚合，独立验收使用 Decimal 后以容差核对。最终 data 引用主机实际执行的观察，输出代码不能篡改数值。自定义工具样例由模型在看到源码前单独生成，能发现代码偏差，但不是所有业务计算的正确性证明。

### 业务指标与兼容数据绑定

创建时使用 `--data-contract examples/data_contract.example.json` 附加字段和业务指标契约。该示例与 `examples/sales.csv` 配套，声明了 `revenue`、`units`、`product_count` 和 `revenue_per_unit`。运行指标查询：

```powershell
python -X utf8 -m agentforge run <任务ID> --input-json '{"metric_query":{"metric_ids":["revenue"],"dimensions":["region"]}}'
```

只有声明 `compatible-v1`、使用宿主模板、无自定义或实时工具且原数据已附契约的 CSV 产物支持新快照绑定。`--bindings-json` 使用原逻辑数据集 ID 和新 CSV 路径；执行记录会保存新快照，恢复不会重新读取已变化的文件。字段兼容不等于新数据已通过业务验收。完整配置和命令见 [业务契约与数据复用](agent-quality-contracts.md)。

子 Agent 的 max_steps 是每段决策目标（3–16 步）。新构建默认启用自适应扩展：出现新的成功工具结果时可增加决策步数；重复相同失败不会无限延长，累计仍最多 3 倍。重复相同工具参数超过两次会收到错误；已完成的内置查询和读取会复用观察，自定义及实时可信工具保持正常执行。决策预算耗尽、模型调用失败和工具错误都有明确记录。复杂目标可以用 `{"goal":"目标","parameters":{}}` 输入；可通过配置接入可信只读工具，新增任务类型仍需扩展实现。

### 设计修复与时间控制

诊断为接口/工具划分问题时，工厂进入 `redesign → redesign_tests → redesign_code`；连续代码修复在同一测试集、同一批实际检查中没有进展时，也会重新设计。工具失败后跳过的业务测试，不会与此前完整测试的通过率直接比较；接口变更后的新测试也不与旧接口分数比较。原业务目标、资源和所有业务测试保持不变，保留验收失败仍阻止交付。本轮不会把独立评测答案或保留验收失败传给修复。

默认构建累计上限为 60 分钟，子 Agent 默认为 15 分钟；包含模型及工具时间，人工等待不计入。上限可调或设为 0；不设置 token/费用配额。单次 Codex 超时仍由 `--timeout` 控制。

```powershell
python -m agentforge create --request "你的需求" --csv data.csv --max-seconds 7200 --run-max-seconds 1800
python -m agentforge retry <任务ID> --max-seconds 0
python -m agentforge retry <停止任务ID> --max-repairs 5 --repair-scope design --direction "简化工具接口，保留业务验收"
python -m agentforge continue-execution <任务ID> <执行ID> --max-seconds 1800
python -m agentforge upgrade-runtime <未交付任务ID> --timeout 600
```

达到修复轮次上限后可在原任务上增加轮次（最多 20）并继续，也可指定 code/design 范围；仍需通过原业务验收。`--fixed-steps` 可关闭自动决策扩展。控制台创建表单和超时接续操作也提供时间设置。

受信运行器修复后，未交付任务可用 `upgrade-runtime` 生成新的运行时版本并重新执行全部验收，保留原版本和冻结测试。自定义工具可用 `is_object`、`is_array`、`is_string` 校验嵌套 JSON 数据，再取字段；辅助函数的类型错误会记录具体工具行号，不会直接击穿构建进程。

### 查看、恢复与运行

```powershell
python -m agentforge list
python -m agentforge show <任务ID> --json
python -m agentforge diff <任务ID> --version 2
python -m agentforge resume <任务ID> --direction "保留来源原文，调整输出格式"
python -m agentforge retry <任务ID>
python -m agentforge recover <任务ID>
python -m agentforge upgrade-runtime <未完成任务ID>
python -X utf8 -m agentforge run <任务ID> "每年有多少天年假？"
python -m agentforge tools
```

`resume` 用于等待人工方向的任务；方向可省略。`retry` 用于模型/系统调用失败，从保存的阶段和逐用例进度恢复；已完成验收用例复用，中断用例继续检查点，修复预算不重置。`recover` 处理排队或崩溃后遗留运行任务。`--max-repairs` 为 0 至 20，默认 3。

运行时升级后，未完成任务可用 `upgrade-runtime` 生成新版本、保留原契约并重跑全部验收。已完成任务使用其导出包中校验过的原运行时，在控制台也能继续运行。新版本还保存运行时源码快照，使未交付候选的已有执行也能沿用原运行时接续；旧任务缺少快照时需要 upgrade-runtime。正常文档回答必须执行必需工具；核对资源目录后对域外问题明确拒答的路径可免执行业务工具。

## 陌生任务评测与运行接续

内置 `unseen-v1.1` 包含 2 个陌生业务任务、12 条固定场景。制度任务覆盖现行/废止规则、跨文档回答、拒答和不可信留言；账本任务覆盖完整行、负数退款、全空列、空筛选和人工补充。资料是显式虚构数据。标准答案和判分规则不进入工厂；仅构建完成后对冻结版本运行。评测失败不会触发使用标准答案的自动修复。

```powershell
python -m agentforge benchmark list
python -X utf8 -m agentforge benchmark run --timeout 360
python -X utf8 -m agentforge benchmark run --task returns_ledger --run-id <相同任务的构建ID>
python -X utf8 -m agentforge continue-execution <任务ID> <执行ID> --reply "以净额为指标"
```

真实评测会构建 Agent 并消耗 Codex 额度。报告保存在 `evaluations/<评测ID>/report.json`，工作台测试页也显示结果。也可用 `--evaluate-generated --run-id <ID> --task <匹配任务>` 单独诊断冻结的未交付候选；该结果不会改变交付状态。它是公开的固定回归集，适合度量当前两类能力；若人工根据失败修改产品，后续需要新增未见用例，不能继续把同一套场景当成未污染的盲测。

子 Agent 的 `waiting_input` 状态接受文字回复，CSV 可附 parameters；工作台运行页可直接提交。模型故障和进程中断可以从最后保存的决策状态显式恢复，已完成工具观察保留。尚未提交的只读调用可能重读；未提交模型请求计入预算，新请求仍可能消耗额度。每段以 max_steps 为目标，有进展可自动延长；累计最多 3 倍，人工回复最多 5 次。历史 v0.4 导出运行时保持原行为，新增接续能力需要新构建。

可信工具由根目录 `trusted_tools.json` 配置，在工具页可查看。详见 [可信工具接入](tool-integrations.md)。SQLite 数据库和服务密钥不自动进入导出包。

## 独立产物

`dist/<任务ID>/` 与 `dist/<任务ID>.zip` 包含：

- agent.json、logic.py、custom_tools/*.py、tool_contracts.json、requirements.json、resources.json、engine.json；
- 冻结的 test_suite.json、开发及保留验收报告；
- run_agent.py 与最小运行时；
- manifest.json：产物版本、契约及运行时指纹、文件 SHA-256；
- verify_agent.py：校验文件与契约、工具单测；`--live` 重新执行模型行为验收。

解压后需要 Python 3.11+，Python 运行时仅依赖标准库。符合快速路径条件的结构化 CSV 或指标查询不调用模型，可以在没有 Codex CLI 的环境中运行；自然语言规划和决策循环需要已登录的 Codex CLI 并消耗额度：

```powershell
python -X utf8 run_agent.py "你的问题"
python -X utf8 run_agent.py --input-json '{}'
python -X utf8 verify_agent.py
python -X utf8 verify_agent.py --live
```

`verify_agent.py` 只检查文件完整性及本地自定义工具；没有自定义工具时相应检查标为不适用。`--live` 才重新执行模型行为验收。路径选择和调用次数见 [路由说明](structural-query-routing.md)，原始产物始终使用自己的冻结运行时。

## Codex 配置

复用现有登录认证，通过 `codex exec` 的结构化输出调用模型。默认不固定模型名称，使用账号可用的 CLI 默认模型；可用 `--model` 或 `AGENTFORGE_CODEX_MODEL` 指定。单次请求默认超时 180 秒，可用 `--timeout` 调整到 600 秒以内。

项目使用 `--ignore-user-config` 隔离可能影响结构化构建的全局运行配置，同时保留登录；使用只读沙箱和关闭交互审批的调用方式。不会修改全局 Codex 配置。原生二进制可通过 `AGENTFORGE_CODEX_BIN` 指定。
