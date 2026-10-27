# JSON 人格运行时

本地审查修复已随 2026-10-27 安全并入部署：人格覆盖文件 JSON/编码损坏时保留原文件并禁用写入，错误时间戳归零；失效覆盖会在概览中显示，显式选择当前有效人格时仍会写入新覆盖。命令路径的覆盖落盘在线程执行。安全修复在独立锁文件内重读、合并并原子发布，读取按需刷新；仅协调使用同一锁文件的进程。

猫猫人格与戳戳升级已部署：保留猫系口癖、短句和真实主人的专属称谓，让动作与接话按上下文选择，并将用户细节提示的称呼绑定真实 `is_owner` 和人格的主人概念。图片回指、反搜及画图短评不把普通用户叫主人；访客生图短评含「主人」时使用中性猫猫兜底。机机戳戳仍禁用。

## 注册与版本

- `personas.install_custom_personas(definitions, *, root)` 接收 `name → definition` 全量快照。先通过 `persona_store.validate_persona_definition` 校验整批定义、名字和别名，再完成所有类型转换，最后同步发布；失败不更改任一公开字典。
- `PERSONAS` / `PERSONA_ALIASES` 始终原地更新。内置 `catty` / `fadianji` 对象与源文件不替换、不写入。重装时移除旧自定义定义及别名；已持有的人格对象仍可供进行中的回复使用。
- 定义名、显示名、显式别名共同参与冲突检查；查找别名使用 `casefold()`。解析顺序为当前 scope 覆盖 → 群级覆盖 → 配置群人格 → 默认人格 → `catty`。
- 自定义 `definition_revision` 是规范化定义的排序键、紧凑 UTF-8 JSON 的 SHA-256，不依赖时间、文件 mtime 或人格根目录；不允许从 JSON 覆写。`persona_revision_identity(persona)` 返回内置名字或 `name@digest`。这是内容身份，不是原始文件字节的版本校验值；更新存储仍用 store 返回的 `sha256`。

## 默认值与类型

- `core_persona` 必须非空，不代填身份。默认无书库、开场、对话/风格/消歧示例；节奏、群聊判定、人格提醒、业务回执使用中性文本。默认唤醒词来自名字、显示名与别名。
- `CharacterBookEntry`、`PersonaImagegen`、`PersonaReplyCatalog` 由校验后的对象显式构造；字符串序列与成对字段变为 tuple，集合字段变为 frozenset。不会保留输入 JSON 的可变列表引用。
- 缺省或 null 的身份展示字段采用自定义默认值，不退回内置文本。显式空字符串/数组保留为空；`prompt_manager` 对核心、群聊、节奏、示例和 gate 使用 `is not None`，不把有意的空内容当成缺省。
- 默认表情目录为 `<root>/assets/<name>/emoji`，清单为该目录的 `manifest.json`，下载目录为 `downloaded`。`emoji_allow_downloads=None` 继续使用普通能力配置，不强制只读；JSON 可显式配置资源字段。
- 默认 `PersonaImagegen` 外观与参考图均为空，不指定性别。自动绘图收到空自定义外观时不再落到内置外观标签；中性配置对象也防止默认取用内置参考图。显式外观字段照常生效。既有场景 composer、伴随角色、生成策略及其文案没有改写，本切片不承诺这些旧链路已经实现任意性别/身体布局适配。

## 提示词与策略边界

- 自定义默认关闭的仅是内置身份强绑定内容：日常作息、小心思、猫系动作池、原世界观、固定剧本、图片反应、故事推进、心情等。handler 对应禁用 `mood/story_arc/cpu_engine/poke/proactive_bubble/keyword_replies/legs_picture`。显式定义可以替换禁用列表。
- 通用上下文理解、工具、记忆、签到积分、关系参数和既有权限策略保留；关系/日常/gate 静态骨架只替换内置身份词，并附上中性的当前身份优先说明。不会处理用户自己写的核心、示例或书库正文。
- 默认不禁用 `nsfw_spark`、`pregnancy`；场景集合、群私聊开关、主人概念沿用原默认，freeform 与当前内置默认一致。没有新增道德限制、默认拒绝或阶段权限政策。已有文案和阶段规则不重写。
- 默认不开 `semantic_harness_enabled`，不加载机机场景库；显式打开 harness 字段只声明能力，来源隔离仍由其运行时调用者负责。`style_examples` / `disambiguation_examples` 已接入真实注册器；内置字段和输出路径保持原样。

## 主入口接线合同

以下接线已由 `persona_runtime.py` 与 `__init__.py` 完成，资源和会话行为由入口统一协调。

1. 启动/重载时由存储取得 `definitions()` 全量快照，调用 `install_custom_personas(..., root=persona_root)`；发布应在同一运行时事件循环同步完成，本 API 不提供跨线程多字典事务读取保证。
2. 每轮捕获一次 Persona，对所有工具、图片、回执和消息组装复用该对象。不要在本轮中途重读可变 registry；对象冻结保证旧轮不跟着定义更新变形。
3. 会话历史基线使用 `persona_revision_identity`，不是单独 name。同名内容更新下轮触发隔离；聊天工具切换只写 scope override，不重入当前已持有的聊天锁。
4. 主入口自己的开场/提醒/后置策略保持显式空值语义；不再把空自定义内容当作内置 fallback。剩余主入口固定身份片段由主调度负责按当前人格适配。
5. 资源 store 刷新按当前人格的 emoji 路径和能力标志执行；本模块不创建目录、不部署、不自动切换用户会话。

## 验证

`tests/test_custom_personas.py` 使用隔离包名加载真实 registry、validator 和 PromptManager，使用临时目录与离线依赖替身。16项全部通过，覆盖整批失败不发布、别名/群级优先级、结构化字段、稳定摘要、内置对象/源码与渲染稳定性、空值语义、真实通用提示词无内置身份/世界观、默认策略不收窄及空外观不继承内置标签/参考图。

`tests/test_persona_runtime.py` 13项全部通过，覆盖保存/切换、可信拥有者、逐轮快照、重载部分失败、真实命令准入、空提醒、连字符状态名及覆盖表写失败。历史800项离线结果见 [会话04](session-log/session-04.md)；猫猫增量的本地与远端结果见 [状态](../STATUS.md)。