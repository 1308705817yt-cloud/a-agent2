# 业务契约、执行能力与数据复用

本轮按用户要求仅修改代码和文档：没有新增或运行测试，没有调用模型，没有重新构建或评测 Agent。以下描述实现和使用接口，不能据此声称正确率、交付率或速度已经提高。历史实验和冻结产物保持原状。

## 工厂产物与运行路径

生成器优先复用 csv.query、csv.profile、csv.metrics、csv.sql 和已授权可信工具，仅为缺失的业务计算生成代码。无自定义工具和可信工具的 CSV 产物使用 observed-v1 宿主输出模板，省去重复生成 logic.py 的模型请求；仍保留原有构建验收阶段。

简单查询不再因为声明了可选扩展工具而失去快速路径。存在 required=true 的自定义步骤仍使用循环，不能跳过必需计算。自然语言规划会看到完整能力目录：常规聚合或已有指标可一次规划；SQL、实时工具、解释需求和其他复杂工作返回循环。结构化 CSV/指标输入可由宿主直接执行。上述调用量是路径设计，不是性能测量。

answer_style=facts 使用实际计算结果生成答案；analysis 保留模型解释并附计算结果。运行记录的 answer_evidence 区分宿主渲染与尚未逐句验证的模型解释。预览、资源目录和质量档案不能被当作最终计算结果。

## 接入数据契约

CLI create 增加 --data-contract；HTTP 创建接口接受 data_contract 对象。工作台也可把包含 format=agentforge-data-contract-v1 的 JSON 与 CSV 一起上传，该 JSON 不会变成问答文档。配置示例见 [data_contract.example.json](../examples/data_contract.example.json)，字段对应已有 sales.csv。

契约以原文件名或资源 ID 匹配数据集，支持：

- 完整字段映射：name 是规范字段，source 是原列名，省略 source 时两者相同。所有源列必须覆盖，不能静默丢列。
- text/integer/decimal/date/datetime/boolean 类型；字段别名、单位、空值标记、nullable 和十进制 scale。标识符使用 text，保留前导零。
- 日期采用 YYYY-MM-DD；时间转为 UTC。无偏移量的时间必须声明 timezone，夏令时歧义和不存在的当地时间必须由输入提供明确偏移量。非 UTC 的 IANA 时区依赖系统时区数据。
- csv.encoding 支持 utf-8、utf-8-sig、gb18030、utf-16、latin-1；csv.delimiter 显式指定单字符分隔符。本地非 UTF-8 文件应通过 CLI 的 --data-contract 指定按文件名绑定的编码；网页上传仍要求浏览器正确解码后的文本。
- primary_key 用于非空和唯一性检查；dimensions 声明可用于业务指标分组的维度。

接入失败会指出行号和列名，不把非法数值改成零，不丢弃非法记录。十进制输入超过声明 scale 时拒绝，不能接入时自动舍入。快照保存 raw_rows/raw_columns、规范行、契约指纹、原始数据指纹、转换单元格数和行数。未附契约的旧 CSV 接口继续使用原语义。

以下为使用说明，本文编写期间未执行：

```powershell
python -m agentforge create --request "分析地区销售金额、数量和平均每件收入" --csv examples/sales.csv --data-contract examples/data_contract.example.json
```

这条 create 命令在用户将来执行时会启动正常的真实构建及验收流程；本轮未执行它。

## 精确业务指标

csv.metrics 从冻结契约选择指标，调用者不能重新定义公式。基础操作支持 sum/mean/min/max/count/count_distinct；count 计完整行，count_distinct 跳过空值，文本 ID 不做数值合并。指标固定 filters 与本次 filters 取 AND，调用者不能移除固定条件。

派生指标支持 divide 和 subtract，操作数为已定义指标 ID；循环依赖或不存在的引用在接入时拒绝。派生计算使用未舍入中间值；除零和缺失操作数返回 null。默认数值 scale=6、计数 scale=0，可显式覆盖；最终采用 ROUND_HALF_EVEN。Decimal 计算结果编码为字符串，避免经过 JSON 浮点数损失精度。

一次计划可选择最多 12 个指标、三个声明的分组维度。结果带实际查询、指标定义、数据/契约指纹、计算行数、空值口径和依赖证据；最多 1000 个分组、1 MiB 结果。默认指标分组为空，与原始 CSV 默认 group_by 分开，避免隐式改变指标口径。

```powershell
python -m agentforge run <run_id> --input-json '{"metric_query":{"metric_ids":["revenue","revenue_per_unit"],"dimensions":["region"],"sort_by":"revenue","descending":true}}'
```

普通 csv.query 为兼容旧契约仍返回 JSON number，仍采用原来的字符串相等比较与浮点聚合。精确金额和有类型的筛选使用 csv.metrics；不能把两种接口的数字编码与筛选语义混为一谈。字段别名和业务说明提供给规划器，宿主查询只接受规范列名。自然语言能否正确映射指标仍待独立验证。

## CSV 与数据库关系查询

csv.sql 使用标准库 SQLite 在当前授权 CSV 快照上执行一条 SELECT/WITH。表名为按资源顺序确定的 dataset_1、dataset_2 等；工具目录列出表与数据集映射。支持 SQL 的连接、多列聚合、去重和窗口运算，限制为 10 秒、1000 行、100 列和 1 MiB 输出。表以规范文本单元格装载，必要时需显式转换。

执行器同时使用只读模式、表白名单、SQL 操作授权和函数白名单；值使用 parameters_json 中的具名标量绑定。禁止读写文件、扩展加载和未授权表。超出结果预算直接报错，不把静默截断当作完整结果。查询成功不能证明连接基数、重复累计或指标口径正确，SQLite 的数值转换和聚合也不等于精确 Decimal。

原 sqlite_read 继续使用固定 SQL。新增 sqlite_query 仅在运维显式配置时开放动态查询，不能由生成器把原工具自动升级。见 [配置示例](../examples/trusted_sql_query.example.json)：input_schema/output_schema 省略时由宿主填入固定接口，模型只选择 schema/query 动作并提交 SQL 和绑定值。schema 返回授权表的字段、主键位置和授权范围内的外键；schema_notes 补充业务口径。数据库文件仍须位于工作区 trusted_data/，实时结果不跨请求缓存。

数据库能力目前作为已有 Agent 的可信工具接入。工厂任务类型仍是 document_qa/csv_analysis，尚无单独的 database_qa 构建与独立语义验收流程；不能把新增查询执行器描述为完整数据库问答系统。生成工具不会获得数据库写权限。

## 复用新的数据快照

只有声明 compatible-v1、使用宿主模板、无自定义或实时工具且原数据已附契约的 CSV 产物可更换数据快照。新文件必须满足原字段、类型、主键和业务契约。逻辑数据集 ID 保持稳定，内容 ID 和资源指纹单独保存。

```powershell
python -m agentforge run <run_id> --bindings-json '{"<原数据集ID>":"C:/data/sales-next.csv"}' --input-json '{"metric_query":{"metric_ids":["revenue"],"dimensions":["region"]}}'
```

创建执行记录时读取新文件并保存规范快照；执行恢复使用已保存快照，不重新读取可能变化的路径。原资源、产物、测试和验收报告不变。数据绑定记录包含前后资源指纹、契约指纹和 binding_hash，并明确 behavior_evaluation=not_evaluated_on_new_data。新数据通过兼容性检查，不等于业务行为已重新验收。

独立导出包 run_agent.py 也支持 --bindings-json；配合 --state-file 保存绑定及检查点，--resume 时不能换数据。旧冻结运行时不自动获得这些接口。

## 生成与证据边界

未来生成的新 CSV 验收场景必须明确声明 assertion.kind=calculation/rejection 和 error_code。冻结前使用已有独立 Decimal oracle 检查实际结果类别，声明计算成功却被拒绝的输入不能转成通过的负例；每分区至少两个真实计算场景。展示的目的与判据从结构化断言派生，原生成文字留在 generation_notes 供审阅，不能作为另一个冲突契约。这里修改了产品的场景生成和冻结校验逻辑，本轮没有生成或执行任何场景。

原 CSV 验收只覆盖其既有查询契约。csv.metrics/csv.sql 的独立验证状态在新导出 manifest 的 capability_evidence 中标为 not_independently_evaluated，不继承旧 CSV 的通过率。通用自然语言解释和开放 SQL 语义仍未验证。

后续评测应分别比较一次结构化回答、允许计算工具的直接 Codex、旧冻结 Agent、新构建 Agent；控制业务知识、权限及预算，按业务/schema/数据集划分开发和隐藏任务。分别记录首次交付、已执行正确率、未执行/服务/语义/计算错误、构建成本、请求与 token、延迟和人工介入。取消强制工具、快速路径、缓存、业务契约和 SQL 能力应分别比较；本轮没有创建或执行这套评测。
