# 任务中心与 DeepSeek 子代理

## 状态

2026-10-30 本地实现。任务、积分、子代理、MCP、知识库、扩展及管理接口已有离线测试；生产模型和真实 QQ 交付仍需上线后验证。本次没有部署或提交，原人格、声线、主模型和已有收费工具保持不变。

## 入口与默认额度

主模型可用 `catty_delegate` 创建和管理任务；`catty_schedule` 管理提醒与计划；`catty_knowledge` 导入和检索文档；`catty_mcp`、`catty_extension` 访问预先批准的工具；`catty_task_artifact` 读取和交付产物。

QQ 直接命令为 `/任务`、`/任务确认 TASK_ID`、`/任务取消 TASK_ID`、`/任务继续 TASK_ID`、`/计划`。管理页面 `/dashboard/tasks` 与原面板共用管理鉴权。

| 档位 | 每日免费次数 | 之后每次积分 | 累计 DS token | 时间上限 | 工具轮数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| light | 3 | 20 | 32000 | 180 秒 | 4 |
| standard | 1 | 80 | 128000 | 300 秒 | 8 |
| deep | 0 | 200 | 256000 | 600 秒 | 12 |

普通用户按 QQ 账号跨群和私聊累计，每日任务预算 600000 token、最多确认 20 次，同时运行 1 个任务。确认即占用免费次数和尝试次数，失败退款不返还尝试计数。主人免积分和个人额度，仍受单任务预算、时间与服务级并发限制。

每项任务最多 3 个子代理、同时执行 2 个；服务最多同时处理 4 个任务。每日边界按 `agent_tasks.timezone`，默认 `Asia/Shanghai`。

## 积分、取消与恢复

报价、确认、执行、交付分别记录。付费任务需要当前用户明确发送确认命令，模型不能代替用户确认；免费任务可直接排队。过期报价或因免费次数变化产生的新价格需要重新确认。

原 `AffectionStore` 用户记录保存冻结与幂等结算，不创建第二套余额。已冻结的积分不能被旧消费入口花掉。确认、终态和积分写入有持久检查点，跨存储中断按同一任务 ID 恢复，不重复扣款。

交付确认后结算，内部失败退款。`delivery_unknown` 留待人工核对，不自动重发。重启后的未完成运行标记 `interrupted`，明确继续后才执行；已发生的外部副作用不能据此推断可安全重做。

每次模型请求先预留输入估算和输出上限，再按真实 usage 结算；所有子代理、工具续轮和重试共用根预算。缺 usage、取消或连接中断保留未知用量预留。任务预算与旧聊天扣分分开，同一子任务不重复计价。

普通用户每天最多创建 5 个计划。提醒不调用生成模型；Agent 计划每轮独立检查权限和额度。需要付费的轮次暂停并保留报价，等待用户确认，不因创建过计划就自动无限扣款。

## 子代理和产物

### 完整目录与角色开关

主模型接收完整已注册工具定义；开关与权限不再通过删除 schema 表达。`tool_switches.py` 在本轮尾部增加短 `[Current tool switches]` 标记，注明角色、默认 on/off 和例外工具。目录保持稳定，切换只改变小标记，原人格、图片、群历史与工具回执不裁掉。

`catty_tool_switch` 接受 `enable`、`disable` 工具名数组，只对当前回复生效；开启后下一轮再调用。原权限层拒绝的工具不能被开启，模型不能修改自己的角色或权限。主模型默认开启获准工具；DS 的 research/document/analysis/artifact 角色默认开启各自常用工具，仍能看到其它目录项并在继承权限内按需开启。动态技能加载/保存继续沿用原资格检查。

### 主模型输入与缓存

`main_dispatch_enabled=true` 保留按用户、会话和人格隔离的来源档案。`main_input_limit=0` 是默认和当前本地配置，表示不额外裁剪完整主请求；模型本身的窗口与原预算仍有效。`main_input_target=22000` 不再是默认发送硬门槛。只有显式设置正数 limit 才启用兼容的短投影模式，不建议用它替代完整能力链路。

原会话历史不删改，DS 可通过 `catty_context` 按需读取来源；旧系统消息读回时只是非可信参考材料。档案默认 24 小时有效，并有数量上限。DS 单次输入默认上限 64K，仍受每项任务和用户每日累计预算约束；主模型只接子任务摘要、引用和产物，不重复接收完整长文。

成本以实际缓存读、缓存写、新输入、输出与 DS 用量共同判断，不把缓存命中视作免费，也不只比较请求总长度。目录和静态说明变化可能导致一次冷写；真实节省需上线后按相同任务对照，未宣称已达到固定费用或命中率。

research、document、analysis、artifact 四个角色统一使用 `agent_tasks.worker_model`，默认 `deepseek-v4.1-flash`，地址和凭据取已有 DeepSeek 辅助线路。网络前再次核对模型和端点，不借主模型兜底。

只传任务描述、显式材料和授权引用，不复制整段聊天或完整人格语料。角色决定默认开关，当前用户能力决定可开启的边界；知识库与产物只读，MCP 和扩展还要通过各自名单。身份、价格、模型、预算和目录不是模型可更改的参数。

完整结果写入任务目录，回主流程只给摘要、来源和产物引用。主模型可用小材料包生成交付语；失败保留已有结果，不重跑子任务。未确认的工具结果不视为成功产物。

## 文档知识库

支持 TXT、Markdown、HTML、PDF、DOCX、XLSX 和公共网页。QQ 文件只能来自真实文件段与平台文件接口，不能读取任意本机路径。上传硬上限 20 MiB，另有限制归档展开大小、比例、条目数及解析时间。

SQLite FTS5 提供持久检索，可复用本地 BGE 向量混合排序。语义后端未就绪会报告 `semantic_pending`，保留文本检索而不制造伪向量。文档按用户、会话、人格隔离，引用含文档 ID、版本、页码和块 ID。

PDF 页码为物理页，DOCX 只按显式分页，XLSX 以工作表为页；不包含扫描件 OCR。原件和提取文本保留为受控产物；删除同步撤销检索权限，磁盘清理失败可重试。

## MCP 与扩展

MCP 使用官方 Python SDK 1.x，支持 stdio 和 Streamable HTTP，服务器由管理员预配置，默认空名单。调用重新核对服务器、工具及身份，超时不自动重放。本地真实 stdio echo/add 和子进程退出已验证，外部业务服务器未实测。

`agent_tasks.mcp.servers` 条目支持 `id`、`transport`、`command`/`args` 或 `url`、`env`、`enabled`、`allow_guests`、`allowed_tools`、`timeout_seconds`、`max_result_bytes`。示例：

```json
{
  "id": "research",
  "transport": "streamable_http",
  "url": "https://HOST/mcp",
  "enabled": true,
  "allow_guests": false,
  "allowed_tools": ["search"],
  "timeout_seconds": 20,
  "max_result_bytes": 65536
}
```

扩展默认位于任务数据目录下，可配置 `agent_tasks.extensions.root`。每个扩展有 API 版本 1 的 manifest，管理员显式启用后加载；命名空间防止覆盖内置工具。支持生命周期、请求/工具/结果钩子，重载失败保留旧版，在途调用不会被强卸载。

Python 扩展是管理员信任的代码，不是隔离任意第三方代码的安全沙箱。默认启用名单为空，不下载或自动执行插件。

## 管理、追踪与配置

页面包含任务、计划、追踪、知识库、扩展、MCP、配置和诊断。写操作要求同源请求与当前 revision；配置仅修改 `agent_tasks`，用 SHA-256 检查并发，保留 10 个版本，回滚保留其它配置。

追踪关联入站、工具、模型用量和任务交付元数据，不记录正文或密钥。诊断 ZIP 包含环境、状态和有界脱敏追踪，不带知识库原文和原始配置凭据。配置在无在途任务时切换，避免中途破坏已确认工作。

配置样例见 `config.example.json`，源码主要是 `task_*`、`agent_delegate.py`、`mcp_runtime.py`、`knowledge_base.py`、`extension_runtime.py`。运行数据默认 `data/agent_tasks`，不进入 Git。

## 验证

所有新测试已列入固定离线清单。定向运行：

```powershell
E:/Py/python.exe -B -X utf8 scripts/run_offline_regressions.py --module test_task_store --module test_task_service --module test_task_host --module test_mcp_runtime
```

设计参考 AstrBot v4.29.3 的工具式委派、独立状态、归属校验、大结果外置和追踪机制；代码按 Catty 接口独立实现。多平台接入、视频理解和唱歌不属于本次六个核心模块交付范围。