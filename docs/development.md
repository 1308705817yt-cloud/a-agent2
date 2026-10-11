# 开发与故障恢复

## 常用检查

```powershell
python -m unittest discover -s tests -v
python -m compileall -q agentforge
node --check agentforge/web/app.js
```

本项目运行不依赖 Node；Node 仅可用于前端语法检查。单元测试采用接口故障注入，不会调用真实模型。真实 Codex 联调请显式提供允许发送的资源。

## 任务状态

| 状态 | 含义与操作 |
| --- | --- |
| queued / running | 已排队或执行中；控制台持续显示阶段 |
| waiting_for_guidance | 开发失败且启用了人工方向；resume 可选择输入方向 |
| time_limit | 累计构建时间达到上限；retry --max-seconds 增加上限或设为 0 |
| failed | 模型响应、认证、网络或系统调用中断；排除原因后 retry |
| stopped | 已用完修复次数；可 retry --max-repairs 增加轮次，并选择 --repair-scope code/design，原业务验收不变 |
| rejected | 开发通过、保留验收失败，不导出 |
| unsupported | 关键需求超过当前注册能力 |
| completed | 当前源码和工作流通过两层检查，已导出独立版本 |

认证检查：codex login status；重新登录：codex login。指定模型不受账号支持时，移除 --model 使用 CLI 默认模型。调用超时可提高 --timeout，但最大为 600 秒。

## 增加能力

业务纯数据工具由 adaptive_planner.py 生成，adaptive_contracts.py 校验契约，custom_tools.py 注入授权数据辅助函数。新增受信内置能力需在 adaptive_runtime.py 注册并实现，增加独立测试；只改提示词不能授予文件或网络权限。新任务类型需要扩展需求契约、运行时输入、测试生成、结果校验和导出。

SQLite/HTTP JSON 只读工具可通过工作区 trusted_tools.json 接入，无需让模型编写连接器；详见 [工具接入说明](tool-integrations.md)。选中的配置随 Agent 冻结，不自动授予新的接口权限。

先检查 trace 中的 verification/tool 错误；工具代码错误会标注 custom_tools/<名称>.py:<行号>。工具单测失败会跳过行为测试。MODEL_ERROR 中断保存到版本的 *_interrupted.json，排除登录、网络或额度问题后 retry。

## 工作区

资源、需求、工具定义和测试在 runs/<id>/ 中冻结；版本目录包含 agent.json、logic.py、custom_tools/*.py、change.json、development.json 和可选 acceptance.json/diagnosis.json。子 Agent 执行保存在 runs/<id>/executions/<execution_id>.json，可在控制台运行记录中查看。SQLite 在 .agentforge/agentforge.db；忽略目录不会被打进项目源码包。

不建议手动编辑已冻结的任务来“修复”验收。修改需求或数据应创建新任务，保留既有构建记录。源码包不包含用户 runs/dist 数据；单独导出的 Agent ZIP 则包含该任务的资源快照。

## 子 Agent 接续

| 执行状态 | 操作 |
| --- | --- |
| waiting_input | 工作台填写补充，或 continue-execution --reply "说明" |
| retryable | 排除模型故障后 continue-execution，不传 reply |
| interrupted | 进程重启后显式继续；若检查点在 waiting_input，仍需 reply |
| time_limit | continue-execution --max-seconds 增加累计运行上限或设为 0，保留已有观察 |
| budget | 可显式继续，直到累计 max_steps × 3 上限 |
| completed / failed | 已完成或不可恢复；重新运行会创建新执行 ID |

```powershell
python -m agentforge continue-execution <任务ID> <执行ID> --reply "分析第二天的净额" --parameters-json '{"filters":[{"column":"day","operator":"eq","value":"2"}]}'
python -m agentforge continue-execution <任务ID> <执行ID>
```

检查点包含绑定、phase、待执行决策、观察、trace、回复、重复调用计数和累计预算。执行记录的 checkpoint_hash 用于发现意外改动；OS 锁阻止跨进程重复执行。Windows 并发读状态时可能短暂阻止 rename，原子保存使用独立临时文件和有限重试。

导出包可用 run_agent.py --state-file .executions/session.json 保存状态，--resume 恢复，--reply 人工接续。执行完成后不能再次 resume 同一个状态文件。

## 独立评测维护

```powershell
python -m agentforge benchmark list
python -m agentforge benchmark run --task equipment_policies --timeout 360
```

任务和手写标准答案在 agentforge/benchmarks/unseen_v1.json。固定用例只在导出版本后评测，报告在 evaluations/ 下并绑定版本、产物和整套评测集指纹；构建未交付时所有外部场景记录为未通过，不假装已经执行。系统不把外部失败回送自动修复。追加新场景需更新 suite 版本和 gold 校验测试，保留既有报告。

公开固定集不是永久盲测；后续根据失败改进产品时，另加未见任务衡量泛化。判分检查指定事实、完整数值和引用覆盖，不提供任意答案的语义证明。CI 不跑真实 benchmark，避免隐式模型额度支出。

`--evaluate-generated --run-id <ID> --task <匹配任务>` 可对 terminal 状态的未交付候选做额外诊断；记录实际逐题能力，但 eligible_for_delivery 仍为 false，all_passed 不会变为 true，也不会改写构建或导出资格。常规基线与诊断报告分别保存。

v1.1 修正了把否定、引用留言和讨论旧制度当成违规断言的关键词误判。`benchmark rescore --evaluation-id <评测ID>` 仅复用原答重新判分，必须保持问题、gold、资源和 Agent 指纹一致；生成新报告并保留旧报告，model_calls=0。复判结果标记 replay_scoring，不作为新的盲测。

## 设计修复调试

查看 diagnosis.repair_scope 和 versions/*/change.json.design_repair。修复连续没有提升通过率时，会从代码修复升级到设计修复；仍受 max_repairs 总轮次限制。designs/dN 保存新接口与适用测试，旧源码/测试/失败报告继续保留。无法通过修改 frozen_hashes 或删除验收场景来导出；原业务 cases/goal_cases 会逐项核对。

新增检查在 tests/test_performance.py，覆盖接口变更后的交付、设计阶段超时恢复、禁止弱化业务验收、进展扩展与停滞、结果复用、引用表示纠正、时间接续和租约续期。
