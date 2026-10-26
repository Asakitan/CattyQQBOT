# 人格命令解析与生成边界

`src/catty_qq_ai/persona_admin.py` 只提供纯命令解析和无持久化生成；QQ 权限、当前 scope、存储、原子发布及切换锁由调用方负责。用户命令见 `ADMIN_COMMANDS.md`，共享数据契约见 `persona-contract.md`。

## 解析器

`parse_persona_command(text, switch_commands=None)` 返回不可变的 `PersonaCommand(action, name, body, command, overwrite)`，无关文本返回 `None`，已识别但参数不合法的命令抛 `PersonaCommandParseError(ValueError)`。

| 输入 | action | 参数 |
|---|---|---|
| `/人格`、`/人格 overview` | `overview` | 无 |
| `/人格 列表`、`/人格 list` | `list` | 无 |
| `/人格 <名字>`、`/人格 切换/use/switch <名字>` | `switch` | `name` 保留输入别名，由运行时解析 |
| `/人格 默认/清除/default/reset` | `reset` | 无 |
| `/人格 查看/show <名字>` | `show` | `name` |
| `/人格 导入/import [覆盖/overwrite] \| <JSON>` | `import` | `body` 保留多行 JSON，`overwrite` |
| `/人格 生成/generate <id> [覆盖/overwrite] \| <设定>` | `generate` | `name`、`body`、`overwrite` |
| `/人格 指令/bind <名字> \| <完整指令>` | `bind` | `name`、`command` |
| `/人格 解绑/unbind \| <完整指令>` | `unbind` | `command` |
| `/人格 重载/reload` | `reload` | 无 |
| 完整自定义切换指令 | `switch` | `name` 取映射目标；`command` 为规范化完整消息 |

正文只按第一个半角 `|` 分隔，后续竖线和换行不拆参数。自定义消息使用 `persona_store.normalize_switch_command` 统一规范化（单行、空白归一、英文大小写归一），不匹配子串；内置 `/人格` 语法优先。未知多参数子命令报用法；单个裸名字与合法的自定义 id 无法在纯解析阶段区分，交给运行时人格库校验，不能将未知名字默认为笨猫。

原有 `/人格`、内置别名及清除覆盖语法保留。解析器不改 NoneBot 的 state；旧 `catty_persona_cmd_arg` 由运行时适配。自然聊天“就换刚才那个”不会在此被关键词匹配，而由看到完整上下文的主聊天工具处理。

## 生成器

`generate_persona_definition(config, *, name, description, existing=None, context_text='', completion_fn=None)` 返回 `validate_persona_definition` 规范化后的定义字典，无文件写入、保存、切换、后台任务或草稿状态。

- 注入接口为异步 `completion_fn(config, messages) -> str`；默认延迟导入公开的 `openai_client.chat_completion`，不使用 codex/spark 辅助路线，不传工具。默认请求用现有 `set_distill_suppressed` 临时关闭回复蒸馏，并在 `finally` 恢复，避免定义 JSON 被当成真实聊天回复写入训练/记忆。
- 复制配置后设置 `catty_max_tokens = catty_persona_generation_max_tokens`（默认 4096）；不改变调用方配置、端点或模型。
- `catty_persona_generation_timeout_seconds` 默认 60 秒，必须为正有限数；整个生成最多两次模型调用，由同一个绝对截止时间与单个 `asyncio.wait_for` 约束。只有输出 JSON/校验失败可修复一次；传输错误与超时不重试。
- 提示提供真实 `PERSONA_SCHEMA`，把 `description`、`quoted_context`、`existing_definition` 作为 JSON 引用资料。保留身份、语气、关系和行为意图，不额外改写政策或添加默认禁用设定；前文可解析代词，不要求目标名出现在最后一句。
- 用现有 `lenient_json_loads` 接受普通或带围栏 JSON；返回值必须是单个对象并通过真实校验器。请求 id 须为规范小写形式，并与生成 `name` 完全相等；不靠本地替换 id 把生成错误伪装成功。
- `PersonaGenerationError(ValueError)` 表示输入、请求、解析或验证错误；`PersonaGenerationTimeoutError` 同时属于该错误和 `TimeoutError`。外部 `CancelledError` 原样向上传播，并取消正在等待的请求，不开始修复。
- 调用方生成前读取现有定义及 SHA，生成后携 `overwrite` 和 `expected_sha256` 原子保存；保存成功不等于切换。默认持久化位置仍是 `data/personas`。

## 离线验证

`tests/test_persona_admin.py` 使用真实 schema/validator 与异步模型替身，覆盖旧命令、多行 JSON、完整指令映射、未知子命令、上下文引用、配置副本、生成校验、围栏/宽容解析、一次修复、id 不匹配、共享时限、外部取消和公开主模型入口。测试不初始化 NoneBot、不访问模型或 QQ；不代表在线生成质量或生产切换验收。

该模块31项全部通过；已纳入当前782项固定离线清单，完整结果见 [会话03](session-log/session-03.md)。