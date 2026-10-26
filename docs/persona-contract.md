# JSON 人格与上下文决策实现合同

## 用户需求

内置人格保留；本地 JSON 可导入，聊天描述由主模型生成定义并保存；手动/聊天切换；可绑定自定义切换指令；同步管理员手册。普通聊天根据前文、引用、补充和纠正理解整体要求，自主选择工具执行或直接回复。

## 数据和存储

- 默认目录为 `data/personas`，由 `persona.dir` / `catty_persona_dir` 配置；定义在 `definitions/<name>.json`，指令映射在 `switch_commands.json`。
- 定义是平铺 JSON：`schema_version: 1`、`name`、`char_name`、`core_persona` 必填；`aliases` 可选；其余采用现有 `Persona` 的可序列化字段名。结构化字段显式转换，JSON 不执行代码。
- 默认内置人格和既有功能政策不被覆写；自定义人格的缺省展示文字使用中性模板，不混入笨猫/机机身份。既有 NSFW 策略不新增收敛或默认禁用。
- 定义保存成功立即落盘并可选用，但不会自动切换当前会话；没有强制草稿/确认步骤。
- 已有定义更新需要 `overwrite=true` 与先前读取的 `expected_sha256`；写失败/校验失败/并发版本冲突不发布半成品。
- 单文件原子发布：同目录唯一临时文件、flush、fsync、replace，成功后才更新内存。重载坏文件保留该文件上次可用定义，其它有效文件照常加载。
- `PersonaDefinitionStore` 提供 `reload()`、`definitions()`、`get(name)`、`list()`、`save(definition, overwrite=False, expected_sha256=None)`、`bind(command,name,overwrite=False)`、`unbind(command)`、`switch_commands()`；回执为字典，定义读取含 `definition` 与 `sha256`。
- `validate_persona_definition(value)` 返回规范化字典，失败抛 `PersonaValidationError`；`PERSONA_SCHEMA` 为同一套 JSON 字段契约。

## 运行时

- registry 公开 `install_custom_personas(definitions, *, root)`；传入 name→定义字典，原地更新公开 registry/aliases，保持内置对象。校验整个候选后再发布。
- 自定义 Persona 带 `definition_revision`（内置默认 None）；`persona_revision_identity(persona)` 返回内置 name 或自定义 name@revision，供历史隔离使用。
- 本轮捕获的人格保持稳定；聊天工具切换只持久化 scope override，不重新获取当前已持有的聊天锁，不在当前回复中清空历史；下轮通过现有隔离入口重建口吻上下文。
- `/人格` 的直接命令继续走现有转场锁顺序，支持内置/自定义角色；自定义切换指令按完整消息匹配，权限沿用主人校验。

## 命令

- `/人格`、`/人格 列表`：当前状态/可用人格。
- `/人格 <名字>`、`/人格 切换 <名字>`：切换；`默认/清除/default/reset` 清除覆盖。
- `/人格 查看 <名字>`：定义与版本信息。
- `/人格 导入 | <JSON>`；`/人格 导入 覆盖 | <JSON>`：创建/按当前版本更新。
- `/人格 生成 <id> | <设定>`；`/人格 生成 <id> 覆盖 | <设定>`：仅命令路径发起有总超时的无工具主模型生成，至多一次JSON修复，验证后保存，不切换。
- `/人格 指令 <名字> | <完整指令>`：绑定；`/人格 解绑 | <完整指令>`：解绑；`/人格 重载`：重新加载本地定义。
- 解析器 `parse_persona_command(text, switch_commands=None)` 返回带 `action/name/body/command/overwrite` 的 `PersonaCommand` 或 None。
- 生成器 `generate_persona_definition(config, *, name, description, existing=None, context_text='', completion_fn=None)` 只返回已校验定义，不负责持久化。

## 聊天工具与上下文

主人专用 `catty_persona`，action 为 `schema/list/show/save/switch/reset/bind/unbind/reload`；参数为 `name/definition/command/overwrite/expected_sha256`，操作前在执行器校验真实事件主人身份。`ToolContext.persona_manage_fn` 接收参数字典并异步返回回执，由生产入口注入。模型直接从完整上下文填写 definition，无须第二个规划模型；生成专用命令才调用无工具生成器。

不要求人格名或执行动词逐字出现在最后一句；“就换刚才那个”可以承接上下文。已有成功回执不重复执行；保存与切换分别回执。仅讨论、举例或临时扮演不等于要求永久保存。只有缺少会影响结果的关键参数才追问，不用关键词强制绘图工具选择。

## 验收

覆盖 JSON 类型/别名/指令冲突、原子写失败、重启重载、版本冲突、坏文件保留、无内置身份串入、主人/非主人、会话隔离、工具切换锁安全、上下文教程与 auto 工具选择。仅使用离线模型替身与临时目录；没有真实模型质量或 QQ 实投验收。