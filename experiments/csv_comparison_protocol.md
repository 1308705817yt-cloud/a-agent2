# 仓库净出库账本：直接 Codex 与 AgentForge 对照协议

采用一个新合成账本：24 行、4 个仓库，包含正常出库、负数调整、空值和待处理记录。固定 12 个问题，其中 10 个计算问题、2 个非法输入问题。人工指定的答案已用独立 Decimal 计算器核对；自然语言问题的参考查询只用于离线核对，不发送给模型。

冻结测试集见 [csv_comparison_v1.json](csv_comparison_v1.json)，指纹为 `30b5183a6f77bd228fe1733fc46934a0a04d794d6bb97ad7fd2b3c93edcd52b1`。

## 两种方案

直接方案：每个问题使用一次新的 Codex 结构化请求。提供完整原始账本、共同业务需求、默认规则和当前输入；不生成 Agent，不调用框架计算工具，不进行源码修复，不提供评分反馈。这个基线代表直接结构化模型回答，不代表 Codex CLI 使用 Python、文件工具或其他工作流的最佳表现。

完整流程：一次全新 AgentForge 构建，执行需求理解、工具与工作流设计、独立生成测试、生成源码、开发检查、自动修复、保留验收和导出。只有完成并导出的 Agent 才进入独立问题评测。构建失败后问题标为未执行，并保留在交付分母中。

两边使用相同 Codex CLI 与 requested model 配置，CLI 默认模型的真实后端身份没有被暴露，记为 resolved_model=null。用户配置均隔离。每次请求最长 600 秒，每题累计最多 600 秒；完整构建最多 3600 秒、3 次自动修复。无 token 上限、无外部人工答案修正或重试。

两组同时启动；每组独立问题顺序执行，项目内部开发/验收沿用默认并发机制。网络波动和共享服务负载可能影响时延，因此本次耗时是观察值，不是严格性能因果结论。

## 共同评分与记录

- 两组使用相同的完整数值、结果排序、筛选、行数和响应契约断言。
- `count` 包含空数值行且 column=null；合法 CSV 回答 sources=[]。
- 共同的条数限制契约沿用宿主：`limit=null` 或 `1..1000` 的整数；`limit=0` 计非法输入。
- 宿主负例与合法计算分别统计，不能互相补足。
- 执行生成的自定义工具是完整流程的诊断项，不是直接方案获得计算分的条件。
- 每个模型 complete 请求保存审计 ID、状态、耗时、配置和可用 usage；调用量不等同于隐藏的模型推理轮次。
- 构建耗时/调用单列；每题耗时、执行次数、修复次数及全流程总量同时保留。
- 独立答案、参考查询和未来问题不进入构建、修复或当前模型请求。独立问题的评分失败不会再触发源码修复。

独立问题通过 `FactoryAgent.run` 执行指纹绑定的冻结版本运行时。导出目录与 ZIP 另行验证完整性及本地工具测试；这项离线验证不计为新增真实行为题。

## 校准记录

正式对照 ID：`45f1144aa042`；全新构建 ID：`39f9bde5ddfd`。原始记录保存在 `.agentforge/comparisons/45f1144aa042/`。

之前的 `208bf72a3730` 因实验脚本的 sources JSON Schema 缺少 additionalProperties=false 而无效，已单独标为 `invalid_harness_calibration` 并保留审计。子任务调用可能在进程停止时未提交，远端最终用量和耗时未知；它不参与有效方案的正确率和时延比较，也不计作模型算错。修正后的完整 Schema 先通过了真实协议连通检查，业务需求、输入和答案没有修改。

## 复跑

在项目根目录执行：

```powershell
python -X utf8 -m unittest discover -s tests -p test_comparison.py -v
python -X utf8 experiments/run_csv_comparison.py --timeout 600 --build-seconds 3600 --case-seconds 600 --max-repairs 3
python -X utf8 experiments/summarize_csv_comparison.py <上一步输出的对照ID>
```

每次运行会创建新的对照目录和全新 Agent 构建，不覆盖既有证据。脚本没有本地演示模式。

## 因额度中断后的配对接续

原始对照已经结束：直接方案 12/12；完整流程构建成功，独立题前三题通过，第四题达到累计 600 秒上限，第五题收到额度/速率限制，其余七题未执行。第四题的审计依据是调用方 runtime deadline，不能仅凭随后额度错误断定其底层超时原因。

用户要求继续后，为未通过或未执行的九题安排单独接续 `5ac10a8196e6`。两种方案都在新时间窗口重新回答这九题；完整流程继续使用原冻结产物，不重建、不修改源码，也不接收独立评分反馈。原来的两个失败调用没有已提交观察，采用每题新的 600 秒执行额度，模式记为 fresh_retry；其他七题记为 fresh_execution。三个旧成功结果不在新窗口重新验证。

新审计与报告保存在原对照目录的 `continuations/<ID>/`；原始报告、原始调用和交付 ZIP 保留。分别报告首次交付、接续结果和累计覆盖，不能把两次执行后的 12/12 写成首次全部通过。基础测试和工具/目标测试生成各一次，合计两次 tests 调用；不将其误计为测试生成重试。

```powershell
python -X utf8 -m unittest discover -s tests -p test_comparison_continuation.py -v
python -X utf8 experiments/resume_csv_comparison.py 45f1144aa042
python -X utf8 experiments/summarize_csv_comparison.py 45f1144aa042 --continuation 5ac10a8196e6
```

上面的 resume 命令会产生新的接续 ID，不覆盖已有接续结果；需要使用新输出的 ID 导出该次报告。

这是一个场景、一个有效构建尝试的描述性对照，不能据此推断广泛交付率或任意生产任务能力。数值及契约评分也不能证明自然语言回答中每一句话都正确。
