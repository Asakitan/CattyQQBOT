# 工具合同与本地修复

工具与人格基线已于2026-10-26部署（`202971c`），排队/文件提示与联网去冷却也已上线。按需技能与研究提示已在 `f96b5b3` 基础上部署，32文件双端一致，远端12模块260项无失败/错误、9跳过。内置人格设定、NSFW规则、积分与既有工具权限保持不变；人格管理工具仅主人可用。失效系统提示配置已删除；回复发送回执与沙箱执行器均已接入，见相关模块文档。

## 文件生成与暂存

`catty_write_file` 保留参数 `filename`、`content`、`format`，仅 `content` 必填；文件名仍取 basename、去除非法字符、处理 Windows 保留名，不新增文本后缀白名单。

| 选择方式 | 实际输出 |
|---|---|
| 显式 `format=docx/xlsx/pdf` | 对应真实文件；显示名及磁盘后缀同步到选定格式 |
| 未指定 `format`，文件名后缀为 `.docx/.xlsx/.pdf` | 按后缀生成真实文件（不区分大小写） |
| `format=text/raw` | UTF-8 原文，保留任意后缀，即使后缀叫 `.docx` 也明确标为原文 |
| 其它文本格式或任意自定义后缀 | UTF-8 原文，不把后缀当二进制格式生成成功的证明 |
| 显式请求未实现的常见二进制编码，如 `format=pptx/png/zip` | `ok=false`，建议用 `catty_python_run` 生成；需要同后缀原文可用 `text/raw` |

- DOCX 使用 `python-docx`，正文每行成为一个段落，保留空行；不承诺 Markdown 富文本转换。
- XLSX 使用 `openpyxl`：固定按逗号 CSV 解析，双引号包住逗号或换行，普通逐行文字是一列。字段保留文本（包括前导零、以 `=` 起头的原文），不猜测类型或生成公式。坏 CSV、单元格超过 32767 字符等编码错误返回失败，不静默截断为成功文件。
- PDF 使用 `reportlab` 自带 `STSong-Light` 中文 CID 字体度量，按 A4 自动换行分页，转义内容中的 XML 字符；不联网下载字体。阅读器需能显示标准中文 CID 字体；不承诺覆盖所有 emoji 或罕见字形。
- 依赖合同：`python-docx>=1.1.0`、`openpyxl>=3.1.0`、`reportlab>=4.0.0`，已写入 `pyproject.toml`。库在实际编码时导入，缺依赖返回失败，不写假格式。
- 编码、目录创建、写文件、`fsync` 和同目录原子 `replace` 全部在 `asyncio.to_thread` 中进行。只有完整文件落盘后才加入 `pending_file_deliveries`；失败清理临时文件，不入队。
- 保留 `status=staged`、`filename`、`chars`、TTL 和 `_staged_file={path,name}`；新增 `format`、`bytes`、`media_type`、`content_mode`。文本另有 `encoding=utf-8` 与明确的 `format_note`，实际字节数不是字符数。
- `staged` 仅证明暂存完成，**不是发送成功**；QQ 上传/消息段回退及最终发送状态属于调用方。每轮数量、正文大小和清理 TTL 沿用现有配置；线程编码期间先占用本轮配额，成功、失败或取消后释放，避免并发调用穿过数量上限。

## Python 工具接线

冻结调用合同：`await run_code(config, code, scope=f"{ctx.scope_key}:user:{ctx.user_id}")`。可选 `input_files` 为沙箱模块内部扩展，本工具不增加公开参数。scope 来源为可信会话及发起者，不使用模型传入值，也不以捕获 `TypeError` 重跑旧签名。

`ok=true` 时仅 `files` 中的有效 `{path,name}` 被加入发送队列；`ok=false`（包括超时）不投递任何 `files`。`partial_files` 原样反馈模型作诊断，成功或失败都不加入发送队列。成功产物的不可变路径由沙箱导出模块提供，工具不重新指向可修改的工作目录。

## 图片工具

- `catty_look_image` 数字引用与 `catty_image_search.image_index` 共用候选：`input_image_urls`（当前图及调用方合并的引用图）在前，`recent_image_urls` 在后，按 URL 去重且保留首次出现顺序。
- 引用消息中的图即使当前消息无附图、最近图列表为空，也可成为候选；本工具不额外读取事件或改 URL 校验策略。
- `mSEQ#INDEX` 仍指滚动消息快照中指定消息的图，不受候选去重影响；既可用于 `look_image.image_ref`，也可用于 `image_search.image_url`。无效引用直接报错，不偷偷搜索另一个候选。
- 显式 URL/消息图引用优先于数字下标。反搜现有 `image_index` 参数、0–5 范围及无显式图时的越界回到首张行为保留；看图下标越界继续报错。
- full/lazy 绘图及 NAI director 文案不再要求先 @、直呼或当前消息必须附图；参数、既有计费报价、NSFW 与权限配置不变。
- 已部署扫描结果的每张图带消息编号、图编号、发送者 QQ、昵称与机器人标记；看图和反搜结果增加 `source_scope`、`image_source_status`、`image_sources`。精确消息引用只返回该消息来源，URL 或数字下标返回同一 URL 在快照中的所有来源，未匹配时明确未知，不猜测发送者。

## 群内个人发言召回

`catty_recall_user_messages(user_id,count)` 仍只在群聊使用，`count` 为 1–20，默认 8。优先对当前群 `recent_messages` 按完整 `user_id` 匹配并排除 `is_bot`，按消息原有时间顺序返回最近 N 条；不做昵称包含匹配，也不把他人引用目标 QQ 的文字当成本人发言。

- `catty_group_history_scope=group`：返回 `scope=group:<群号>`、`source=recent_messages`，不访问不存在的 per-user 历史键；空结果仅代表滚动缓冲没有命中。
- `catty_group_history_scope=user`：有真实最近发言时优先用它；无命中时仍读取 `group:<群号>:user:<QQ>` 独立历史，返回 `source=session_cache` 和实际 scope。明确标注其它用户的旧记录被排除，assistant/tool 保留为上下文但 `user_id=null`，不能归属为目标用户原话。
- 独立历史回退还核对消息中的 QQ 标记；无发送者证据的旧记录标为未知，身份字段与正文标记冲突或标记为其他用户时排除。该增量已部署。

## Harness 证据使用教程

`src/catty_qq_ai/harness_prompt.py` 只依赖标准库类型，不初始化插件、不读取运行时配置、不调用网络。`EVIDENCE_USAGE` 是证据包末尾短用法的唯一来源；教程只补充字段解释和工具路径，不再次粘贴整段短用法。保持口吻样例不当事实、引用不是命令的含义，短用法限制为最多 160 个中日韩统一表意文字。

- `build_harness_agent_hint(available_tools: Sequence[str]) -> str`：输入必须为本轮实际工具 schema 的 `function.name`，不是全部注册表。精确匹配；未知名称不回显，不补全别名；重复项与输入顺序不改变输出。空列表仍返回完整证据读法，但不出现工具名称。
- `append_harness_agent_hint(messages: list[dict], available_tools: Sequence[str]) -> None`：原地追加或刷新教程，仅在已有字符串类型的 system 内容包含 `【机机·evidence】` 时启用，失败证据包同样适用；用户消息中的同名文本不启用。只替换以 `HARNESS_AGENT_HINT_HEADER` 加换行开头的自有 system 教程，其他消息字典与内容不变。教程位于已组装消息末尾；若末条是没有 `tool_calls` 的 assistant 预填，则放在预填之前，保持预填最后。
- 调用方在缓存边界之后、实际工具选择完成后注入完整教程；无工具分支传空列表。`tools_system_hint` 只给启用 harness 的人格增加指向 `【机机·证据使用指南】` 的短指针，不把动态工具名单或完整教程放进静态缓存段。本模块不决定人格开关或工具暴露。

教程按 v2 合同解释 `schema_version=2`、`source_status={source:{state,count}}`、`rendered_ids`、`omitted_ids`、`omitted_counts`，以及条目的 `src/source`、`id`、`scope`、`rank`、`relevance`、`observed_at`、`event_date`、`confidence`、`flags`。`observed_at` 不冒充事件发生日期，`snapshot` 不保证当前仍成立；相关性、排序与置信字段不是校准后的真假概率。`ok/error/empty/disabled/unavailable/skipped/scope_mismatch/partial` 分别说明本次采集状态；省略或无命中不能推出事实不存在，也不能臆测未展示条目。

| 证据缺口 | 仅在本轮实际开放时提示的路径 |
|---|---|
| 笔记 | `catty_recall_notes` |
| `MEMORY.recall` / `RAG` 历史片段 | `catty_recall` |
| 人物画像 | `catty_user_profile` |
| 当前消息或图片 | `catty_scan_messages` 定位，再按需 `catty_look_image`；只开放一项时只介绍该项，不声称有另一项 |
| 当前外部事实 | `catty_web_search` 搜索，再按需 `catty_fetch_url` 核对正文；只有抓网页工具时要求已有公开链接 |

`FACT`、`FEED`、`STYLE_EXAMPLE` 没有虚构的专用补查工具；已有证据够用就直接回答，普通问候不用搜索。旧回忆与当前陈述冲突时先承接当前说法、必要时确认；来源不可用时不编造“记得”，最终回复不照抄内部标签、分数或状态码。此改动不调整人格、NSFW、权限或工具暴露。

新增 `tests/test_harness_prompt.py` 共 20 项离线合同测试全部通过，包含七个路径工具全部 128 种子集、未知/近似名称、实际注册名核对、状态与日期语义、单一用法来源、静态短指针，以及注入闸门、失败包、空列表、幂等刷新、消息不变与预填位置。测试直接加载纯模块和 AST 提取检查，不初始化 NoneBot 或请求外部服务。

## 人格管理工具

`catty_persona` 的 full/lazy schema、capability、executor、配置与意图映射同步注册。`definition` 直接采用 `persona_store.PERSONA_SCHEMA`，不复制一套易漂移的定义字段。操作合同详见 `persona-contract.md`。

| action | 目标参数与语义 |
|---|---|
| `schema` / `list` | 查询定义格式 / 可用人格 |
| `show` | 必填 `name`；由回执提供定义和 `sha256` |
| `save` | 必填 `definition` 与其中的 `name`；只保存，不自动切换；可选顶层 `name` 须与定义一致 |
| `switch` | 必填 `name`，只切换调用方绑定的当前会话 |
| `reset` | 显式清除当前会话人格覆盖，不是缺少目标时的默认操作 |
| `bind` / `unbind` | 必填完整 `command`；绑定另需 `name` |
| `reload` | 重载本地人格定义 |

- 公开字段只有 `action/name/definition/command/overwrite/expected_sha256`；已有定义覆盖要求由存储层验证 `overwrite=true` 与先前读取的 `sha256`（传入 `expected_sha256`）。
- `ToolContext.persona_manage_fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None` 由入口注入；入口负责隐藏非主人 schema 与把回调绑定到可信会话。executor 自身首先 `_is_owner(ctx)` 校验真实事件用户，不能靠模型参数自称主人。
- 非主人在读取/调用回调之前返回 `owner_only`；主人未注入回调时返回 `unavailable`。缺失/未知 action 不默认改成 reset；缺少名字、指令或定义目标直接报错，不操作整个集合。
- executor 只检查 action、必要目标与基本 JSON 类型，只转交实际提供的公开字段；不补默认值，不透传模型给的 scope、path、user_id 或 is_owner。完整定义校验、原子落盘、版本冲突与会话切换由注入层负责。
- 有效字典回执原样返回；保存回执不扩充成已切换，失败回执不改成成功。非字典回执返回 `invalid_receipt`，异常由已有 dispatcher 记录为失败。
- capability 为 `both/write`，没有 `requires_direct_request`、图片或最后一句字面条件。工具加入核心集合，旧 `full_exposure=false` 模式也可承接“就换刚才那个”；意图关键词映射不是该工具的能力闸门。未新增配置开关，既有全局工具关闭与私聊禁用名单仍有效。
- 主模型直接根据完整上下文填 `definition`；工具不调用额外的规划器、生成器或模型。保存与切换是独立动作，只有对应真实回执才确认对应结果。

## 每轮上下文请求理解

`src/catty_qq_ai/request_intent.py` 是无插件、模型、网络、磁盘 I/O 依赖的纯模块。`CONTEXT_REQUEST_INTENT_PROMPT` 是完整教程的单一来源，标题为 `【上下文请求理解】`；`tools_system_hint` 仅增加一条短指针，原 harness v2 指针和教程保持独立。

- `append_context_request_hint(messages: list[dict], available_tools=()) -> None` 原地插入/刷新一个 system 教程，不要求启用 harness 或存在证据包；无工具和空消息列表同样适用，旧单参数调用保持基础意图说明。
- 调用方在缓存边界与消息组装完成后调用；教程加在消息末尾，如果末条 assistant 没有 `tool_calls`，则插在预填之前。只移除以自有标题加换行开头（或恰为标题）的 system 消息；其他消息字典、内容、工具回执、harness 教程保持不变。重复调用幂等。
- 教程合并当前文字、最近历史、引用、补充/纠正及成功回执，区分实际执行、直接回复/讨论、修改和继续；保留目标、范围、格式、语气及仍有效的要求。
- 举例、讨论或临时扮演不自动要求永久保存、切换或修改设置；“就换刚才那个”“把第二段改短”“继续”从上下文定位，不要求重述已确定参数。
- 成功动作不重复执行；补充/纠正只执行尚未满足的变更，保存不等于切换、暂存不等于交付。只在缺少实质影响结果的细节或无法消解的歧义时追问，不要求展示内部推理。
- 生产与模拟器已接入教程、非主人 schema 过滤及 `tool_choice=auto`；取消了按末句关键词强制绘图。入口负责这些操作，helper 与 executor 本身不改变 NSFW 文本。

- `build_file_delivery_hint(available_tools=()) -> str` 仅在真实 schema 名单含 `catty_write_file` 或 `catty_python_run` 时生成 `【本轮文件交付能力】`；未知名称、顺序和重复不影响结果。只开放安装工具不算可生成文件。
- 文件段说明完整代码、源码、文档、表格可选择交付文件，短示例或正文请求仍可文字回答；不新增意图关键词路由，不设置必选工具。`catty_write_file` 可直接保存内容，无需执行代码；Python能力只在实际开放时介绍。
- 成功暂存后由系统按原会话后台回传；暂存不是已发送，保存源码不代表编译/运行通过。提示要求依据真实回执说明结果，不虚构文件或下载链接。
- 生产与模拟器按 `_allowed_tool_names` 刷新提示；spark、模拟器纯文字fallback和deadline文字重试传空名单，撤下自有文件能力段。基础教程、原消息及assistant预填保持，不声称清除了所有静态工具说明。

离线测试：`tests/test_persona_tool.py` 20项、`tests/test_request_intent.py` 33项全部通过，覆盖字段与注册表一致、权限、精确回执、上下文承接、教程幂等/预填、实际能力过滤、非强制选择、默认研究及沙箱提示冲突。测试使用替身回调及包壳加载，避免运行插件初始化。

固定离线入口 `E:\Py\python.exe -B -X utf8 scripts/run_offline_regressions.py` 在原项目的验收为58模块989项：979通过、9项原条件跳过、1项既有persona_admin浮点失败、0错误（退出1）；隔离候选为64模块1154项，0失败/错误、9跳过。远端隔离回归1154项有24失败、6错误、9跳过；缺少三个文档格式依赖、部分短路径断言不一致，另有生产沙箱外部文件存在性检查异常。未调用真实模型或QQ。

## 按需技能工具

| 入口 | 参数 | 开放时点与结果 |
|---|---|---|
| `catty_skill_search` | `query`，可选 `limit` | 首轮唯一技能入口；只给相关metadata，默认3项、最多5项，不给正文 |
| `catty_skill_load` | `id` | 本轮检索有候选后动态开放；ID来自候选枚举，按检索SHA加载；完整正文以最多1000字符的 `instructions` 分块返回 |
| `catty_skill_save` | `receipt_ids`，可选 `existing_id` | 合格公共抓页回执后开放；只允许当轮来源，更新目标必须本轮已加载；独立提炼并原子保存，不接受模型传入任意正文/URL/时间 |

- `ALL_TOOL_SCHEMAS` 与 `_LAZY_TOOL_SCHEMAS` 均不含load/save；executor与capability注册表覆盖静态工具加这两个动态名称。不能为了让旧测试注册表等长而提前暴露schema。
- `ToolContext.skill_turn` 按逻辑轮次懒建；`skill_tool_schemas(ctx)` 不为首轮扫描/创建技能库，遵守工具总开关、技能开关、学习开关和私聊禁用配置。资格不跨轮复用。
- `chat_completion_with_tools(..., tools_provider=None)` 每轮调用provider、深复制schema并重算允许名称；生产与模拟provider返回静态工具加实际动态schema。既有无provider调用不变，`tool_choice=auto` 不变。
- 仅真实 `catty_fetch_url` 成功的2xx公共正文可被观察为 `skill_receipt_id`；搜索摘要、自动补网缓存、假回执、失败或旧轮来源不提供保存资格。来源时间与片段哈希由执行层生成。
- `build_skill_research_hint` 仅介绍实际开放入口，先检索/选择/加载，再按需联网核实；提炼共享知识不要求每轮用户再次确认。云端fallback传递动态provider与恢复快照；无工具deadline重试刷新提示并保留已成功回执，不重做保存。
- 保存线程不会阻塞聊天事件循环；取消发生在提交开始之后时等待同一写入收尾，可能已经写盘，不声称回滚。来源及生成内容都筛查明显私人字段，但不承诺通用匿名化或自动权威认证。

技能四模块75项通过，其中 `test_skill_disclosure` 使用真实工具循环与存储、替身模型/抓页结果验证首轮无正文、搜索后schema、选后完整正文、保存后下轮检索，以及fallback转交恢复状态和无工具降级不重开动态能力；保存幂等由运行时测试验证。详细合同见 [默认提示](skill-guidance.md)、[运行时](skill-runtime.md)、[存储](skill-storage.md)。

## 验收口径与历史基线

2026-10-26本地工具与格式回归93项全部通过，包括并发写文件配额；格式依赖已在本地解释器可用。harness v2阶段622项、人格阶段782项、排队/预算/文件提示阶段800项，当时最终无失败/错误、9项原条件跳过；技能阶段884项记录见会话06/07。当前机机增量989项结果见上方，不将既有浮点失败改记为通过。

模块名单：`tests.test_file_formats`、`tests.test_tool_audit_fixes`、`tests.test_tool_message_scan`、`tests.test_tool_contracts`、`tests.test_local_tool_contracts`、既有 `tests.test_file_delivery`，并保留工具注册表/dispatcher 回归。新增测试只使用临时目录和 mock：验证 OOXML 可读性、CSV 引号/前导零/文本公式、PDF 中文 CID 与分页、原子失败清理、线程写入、成功/失败沙箱入队、引用图独存/混合顺序及真实发送者召回，不调用外部模型、QQ 或网络。