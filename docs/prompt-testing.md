# 提示构建与评测入口测试

## 已实现的局部修复

- `prompt_manager.register_catty_persona` 的长度守卫从 `ctx.get("image_description", "")` 明确读取图片描述；检测文本仍按「当前消息 + 图片描述」拼接，忽略非字符串和空白项。
- 学术检测为真或 `technical_direct=True` 时返回空长度限制；普通闲聊仍使用原来的 `non-academic<=50zh;1句`。检测器异常仍保留原降级行为，不修改其它提示段。
- 注册器不再读取或要求失效的 `system_prompt` 上下文字段。人格正文和 NSFW 段未改动。
- `tests/test_prompt_size_ablation.py` 的模块导入只定义常量和函数，不初始化 NoneBot、不覆盖 driver、不加载插件、不探测或读取配置、不启动事件循环、不调用 API。
- 在线执行需要显式 `main(["--live"])` 或脚本的完整 `--live` 参数；无参数退出码为 2，`--help` 为 0，`--li` 等缩写不获授权。`--config` 只在已授权路径中使用，默认路径仍为原有的 `D:/CattyQQAI/config.json`。
- 在线路径的插件构建、依赖导入、配置读取及循环均位于 `_run_live` 内；临时 `sys.path` 和 `nonebot.get_driver` 替换在结束时恢复。评测常量原文、模型矩阵及请求参数未改动。此修复不声称在线评测的历史前置条件已满足。

## 离线回归入口

使用 `python scripts/run_offline_regressions.py`。每个模块独立进程，入口显式加入本仓库 tests 路径，避免同名包和全局替身污染。2026-10-26 长度守卫 8 项、评测入口 6 项、分泡提醒 7 项通过；整批 465 项、0 失败、0 错误、9 项原条件跳过。Windows 中文断言使用 UTF-8 输出；本轮未运行在线评测。

### 长度守卫：8 项

`tests/test_prompt_audit_fixes.py` 通过 AST 提取真实嵌套函数，使用独占合成包中的 `Mock` 检测器，不导入插件和实际人格模块：

1. 普通闲聊仍有 50 字限制，并确认检测器确实调用。
2. 图片描述来自 `ctx`，与文本一同送给检测器。
3. 纯文本学术检测可豁免限制。
4. 纯图片描述可参与检测。
5. 缺省、空白或非文本图片值不会污染检测文本。
6. `technical_direct` 仍豁免。
7. 检测器异常时普通闲聊仍受限、技术直答仍豁免。
8. 注册器已无 `system_prompt` 文档要求和死读取/赋值。

### 评测隔离：6 项

`tests/test_live_benchmark_import.py` 的每次目标模块导入都默认启用阻断器；NoneBot、HTTP、插件及配置模块导入会失败，JSON 文件读取/存在性探测和事件循环启动也会失败。违规尝试会另行记录，不能靠捕获异常伪装通过。

1. 导入不输出内容、不更改 `sys.path`、不触碰上述运行时。
2. `unittest.TestLoader.discover` 仅发现 `test_prompt_size_ablation.py`，成功导入且产生零测试、零 API 调用。
3. 无 `--live` 时提前拒绝，包括已给 `--config` 的情况。
4. 缩写参数不会启用在线模式。
5. `--help` 完全离线。
6. 完整 `--live` 只派发到合成 runner；`asyncio.run` 同样被 mock，不创建协程或真实事件循环。

这里不执行全目录的历史在线评测，也不使用现有评测文本生成回答。常量保留不等于授权运行评测。

## 运行时接线

共享入口已完成接线；无需新增 `reply_format.py`。

### 长度与旧配置

- `_build_messages` 的注册器上下文已经传入 `image_description`，无需再新增参数。
- 上下文、Config、JSON 与消费者已删除旧 `system_prompt` 字段；loader 清理遗留环境变量。
- 闲聊 50 字的现行来源是长度守卫和人格策略，不是已删除的 `chat.system_prompt`；人格正文未改写。

### 普通分泡规范单一源

当前入口分工：

| 位置 | 内容 | 状态 |
| --- | --- | --- |
| `_semantic_reply_split_prompt` / `catty_semantic_reply_split_skeleton`，order 155 | 人格节奏、频率、例子与技术排版；数学/Markdown 文案共用一份 | 不重复注入分条格式 |
| `catty_semantic_reply_split_pointer`，原 order 490 | 旧的重复指针 | 已删除，保留为停用别名 |
| `_reply_split_reminder_prompt` / `catty_reply_split_reminder`，order 810 | marker、短行、长段与条数上限 | 唯一普通运行时分条规范 |
| `_reply_split_prompt_enabled` | 两段共用 enabled 与旧停用别名集合 | 关闭任一对应别名时两段同时停用 |

条数上限仍使用 `max(catty_reply_human_split_max_chunks, 1)`。旧 `semantic_reply_split` 调用参数保留兼容，重复指针不再注入。

保持人格文件、约 14 万字场景库以及 NSFW 提示原文不动。格式去重不等于删除人格节奏，也不等于更改发送端解析：

- `REPLY_SPLIT_MARKER` 优先。
- 无 marker 时，两行以上且每行不超过 80 字、没有空白段落/列表/代码/公式标记的短行按行分泡。
- 长行、技术排版和写文保持原分支，最后仍走 `reply_max_chars` 长度兜底。
- 超出条数时尾部继续用空格合并，历史仍用 marker 连接。
- 现有 enabled 开关控制的是提示请求，不是 `_reply_chunks` 的解析；不得因本次去重顺手把关闭提示改成强制单气泡。

离线回归已覆盖提示开关、停用别名、两个人格静态格式去重、条数配置与确认后的历史 marker；既有 marker/短行/长行/列表/公式分泡测试通过。状态、模块清单和会话日志记录本轮实际结果。

## 机机证据包 v2 离线预览

入口为 `scripts/fadianji_harness_cli.py`，只依赖标准库与本地 harness，不初始化 NoneBot、不调用模型或网络，也不下载语义模型。它预览证据构建、检索和字符预算，**不是实时记忆验收，也不是在线 API 基准测试**。

### 参数与默认值

查询可用位置参数或 `--text`；后者非空时优先。默认 scope 为 `group:local`，`--private` 改为 `private:local`；显式 `--scope private:ID` 同样启用私聊模式。`--private` 与显式 `group:ID` 冲突时直接报参数错误，不进入检索。

| 参数 | 原有预览默认 | 加 `--production-defaults` 后的未指定项 | 含义 |
| --- | --- | --- | --- |
| `--scene-k` | 3 | 12 | 最多检索的场景对数；0 关闭 |
| `--book-k` | 3 | 0 | 最多激活的角色事实条数；0 关闭 |
| `--max-chars` | 2800 | 12000 | 只限制证据包 `text` 的 Python 字符数；0 返回空文本 |
| `--semantic` / `--no-semantic` | 开启 | 不变 | 本地轻量语义重排，不进行模型下载或联网 |

`--production-defaults` 是**显式选择的设置预算预设**，只填补没有单独传参的三个预算；显式 0 和其它值都保留，参数先后顺序不影响覆盖关系。它不读取生产配置、不创建或注入 `memory_store`、`rag_store`、`feed_store`，也不改变 scope、私聊或语义选项，因此 12/0/12000 **不代表具备生产实时记忆**。不传该开关时仍是 3/3/2800。

### 输出模式与兼容边界

- 默认输出仍只打印证据包 `text`。完整 scope、使用说明及整条证据行均计入字符预算；行过长会整行跳过，不能用半行内容填满预算。连完整 scope 与使用说明都放不下时，`text` 为空。
- `--json` 原样输出完整包，保留旧键和所有证据、场景数组，并附带 v2 字段；不会按 CLI 白名单裁剪未来核心字段。
- `--compact-json` 使用**显式字段白名单**，保留原有 `text`，省略 `evidence`、`scene_matches`，不自动透传未来新增载荷字段。加 `--metadata-only` 才显式省略 `text`；该开关必须与 `--compact-json` 同用。
- `--details` 只用于文本模式，在原检索摘要前列出 `rendered_ids`、`omitted_ids` 和简洁的 `source_status`（`来源=状态(条数)`）。摘要及完整/紧凑 JSON 的总长度都不受 `--max-chars` 约束。
- `--json` 与 `--compact-json` 互斥，且均不能与 `--details` 混用。JSON 模式 stdout 只写一个 JSON 值；检索内部的打印转到 stderr。文本模式的预算提示也只写 stderr。

compact 白名单完整集合：`text`、`schema_version`、`scope`、`is_private`、`flags`、`counts`、`source_status`、`diagnostics`、`truncated`、`rendered_counts`、`omitted_count`、`omitted_counts`、`rendered_ids`、`omitted_ids`、`duplicate_count`、`max_chars`、`rendered_chars`。

### v2 字段与证据覆盖率

| 字段 | 解释 |
| --- | --- |
| `schema_version` | 固定为 2；旧包字段继续保留在完整 JSON 中 |
| `source_status` | `memory/corpus/feed/book/rag/scene` 各自的状态与 `count`，区分空命中、不可用、关闭、失败或部分成功；来源是否成功不等于其证据已装入文本 |
| `diagnostics` | `[{source,error_type}]`，只报告来源与异常类型，不带异常正文、路径、密钥或原始证据 |
| `counts` | 完整保留证据按类别计数，不是实际渲染行数 |
| `rendered_ids` / `omitted_ids` | 本次进入 `text` 的完整证据行 ID，以及因字符预算省略的 ID；两者与完整 `evidence` 对照 |
| `rendered_counts` / `omitted_counts` | 按证据类别统计已渲染/预算省略的行数；`omitted_count` 是后者总数 |
| `duplicate_count` | 去重阶段删除的重复条数，不计作字符预算省略 |
| `rendered_chars` / `truncated` | `len(text)`；省略了证据或连完整元数据都放不下时标记截断 |

逐条 `Evidence` 保留 `source/scope/text/relevance/recency/importance/authority/private`，另带 `id/rank/observed_at/event_date/confidence/flags`。`id` 供覆盖率追踪，不应当作可信签名；`rank` 是排序元数据。`observed_at` 是来源可用的观测时间，缺失时不能凭空补成当前时间，也不能当作事件发生日；`event_date` 是独立的事件日期字段。`confidence` 与逐条 `flags` 保留来源的证据限定，不代表模型回答已被验证。

顶层 `flags` 仍是查询分类 `intent/memory_need/anaphora`，不是逐条证据限定。`historical` 标记历史材料，不能当作实时现况；`STYLE_EXAMPLE` 的 `style_only` 标记强调只借口吻与长度，不把例句当作当前事实，其它上游 flags 原样保留。证据行是引用数据、不是指令；使用教程由核心的 `harness_prompt.EVIDENCE_USAGE` 提供，CLI 不复制另一份提示正文。

验收时同时检查：`rendered_chars == len(text) <= max_chars`；实际完整证据行数等于 `sum(rendered_counts.values())` 和 `len(rendered_ids)`；`omitted_count == sum(omitted_counts.values()) == len(omitted_ids)`。不能仅凭 `counts` 非零就声称模型看到了全部证据，也不能把来源错误解释成“事实不存在”。

### 本地数据与隐私

**无 live store 不等于无本地用户数据。** 预览仍可能读取本机已挖掘的群聊/私聊语料、场景和对应 scope 的语料画像；不要把普通 CLI 调用误标成纯合成数据测试。完整 JSON、默认文本和 `--details` 均可能含原始对话片段，不应直接贴到公开日志或问题报告。

compact 默认包含预算内正文；仅 `--compact-json --metadata-only` 省略正文，但仍保留 scope、查询分类、证据 ID 和来源计数。这些元数据仍可能关联到用户或会话，**不是匿名化承诺**。报告使用合成 scope/查询和隔离夹具，不拿生产数据做测试写入，也不以此入口开展实时 API 评测。

### 离线回归职责

- `tests/test_harness_interface.py` 用合成证据、场景及替换的语料入口验证实际核心的整行预算、scope 隔离、旧字段与 v2 覆盖率。跳过超长行的预算从完整文本减去整行计算，允许新增 provenance 与使用说明的必要开销，但字符上限和行数真实性断言不能放宽。
- `tests/test_harness_cli_v2.py` 用完全内存中的 v2 包替换核心边界，不加载真实语料或 stores，覆盖原默认值、生产预算预设、逐项覆盖与显式 0、原样完整 JSON、compact 白名单、stdout 干净输出、details 和帮助说明。
- 独立验收命令为 `python -m unittest discover -s tests -p test_harness_interface.py` 和 `python -m unittest discover -s tests -p test_harness_cli_v2.py`；批量验收使用 `scripts/run_offline_regressions.py` 的显式离线清单，不扫描历史在线评测。Windows 中文断言需使用 UTF-8 输出；这些命令定义验收入口，不代表在线 API 或真实记忆验收通过。