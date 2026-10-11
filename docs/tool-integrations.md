# 可信工具接入

v0.5 支持运维人员配置的 SQLite 只读查询和 HTTP JSON GET。配置文件为工作区根目录的 `trusted_tools.json`，格式参考 [配置示例](../examples/trusted_tools.example.json)。示例地址必须换成自己的服务；未配置时可信工具列表为空，不自动加载演示数据。

每个工具声明唯一 `trusted.<名称>`、用途、输入和输出 JSON Schema、种类及固定配置。配置由本机人员维护，网页上传、模型或生成的 Python 代码不能定义新的数据库路径、SQL、URL 或认证头。

## SQLite

数据库放在工作区 `trusted_data/` 内。例如使用已有的 `trusted_data/inventory.sqlite`，表 `inventory` 具有 `branch`、`sku` 和整数 `available` 三列。保存为 `trusted_tools.json` 时保持 `{"tools":[...]}` 包装，只保留已准备好数据库的 inventory 工具。查询使用 `:branch` 绑定参数，Agent 只能传入门店名。

执行器使用 SQLite 的 `mode=ro`、`query_only` 和 authorizer 三层限制。只能读取配置列出的 main 表，不能写入、ATTACH、调用扩展或读写文件函数；有查询进度超时、行数上限和 1 MiB 返回上限。不存在的数据库会明确报错，不会创建空库。机制依据 [Python sqlite3 官方文档](https://docs.python.org/3/library/sqlite3.html)。

## HTTP JSON

只支持固定 HTTPS GET 地址；明确配置的 localhost/127.0.0.1/::1 服务可用 HTTP。模型参数经过契约校验和 query 编码，不能改变目标地址、方法或认证头。禁止跳转和代理继承；服务需返回 `application/json`，输出还要通过契约校验。

认证头只引用环境变量名称，例如 `Authorization: AGENTFORGE_SERVICE_AUTH`。环境变量值应为服务要求的完整认证字符串。密钥不进入模型目录、Agent 定义、trace 或导出包；异常文本也不会直接回传驱动的原始报错。配置的 HTTP timeout 是网络 I/O 超时，不能当作严格总执行时间预算。机制依据 [urllib.request 官方文档](https://docs.python.org/3/library/urllib.request.html)。

固定地址的服务是部署者信任的能力边界；应由服务自身做业务授权。GET 是否改变业务状态也需要由部署者保证。本版本不支持写入操作、任意 URL、MCP 动态发现或模型生成连接器。

## 构建与部署

先用 `python -m agentforge tools` 检查目录；`python -m agentforge check-tool trusted.inventory --input-json '{"branch":"East"}'` 会实际查询配置的数据库并校验契约，不调用模型。

配置完成后创建新构建，并在需求里说明希望怎样使用可信工具。例如“分析门店销售，调用 trusted.inventory 查询该门店库存，用生成工具比较观察结果”。需求和设计阶段只收到工具说明与契约。选中的固定配置封存进 Agent，参与工具契约和产物指纹；修改全局配置不会改变已有 Agent。

运行时由 Codex 选择工具；可信工具返回完整观察，可由生成工具的 `observation(id)` 再加工。trace 记录工具参数、成功或失败、配置指纹、数据指纹与耗时。可信服务返回的数据会发送到 Codex。

导出包包含连接器实现和冻结配置。数据库与认证信息不自动打包；部署时在包的 `trusted_data/` 路径放置正确数据库，设置相应环境变量。实时服务与数据库的数据会变化，构建通过不代表未来所有数据都通过验收。
