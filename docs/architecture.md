# Catty 工具与回复交付架构

## 运行入口

`start_catty.bat` → `scripts/catty_hot_reload.py` → `bot.py` → NoneBot 插件 `src/catty_qq_ai/__init__.py`。本次未改变启动拓扑、模型路由或部署通道。

## 提示词与工具

当前配置由 `catty_config_loader.py` 映射至 `Config`；已删除失效的 `chat.system_prompt`。角色文件及既有专用提示段提供人格内容；`PromptManager` 组装静态段和动态上下文。静态分泡节奏与每轮格式提示共用启用条件，发送端分泡解析保持原行为。

工具 schema、capability 与 executor 在 `tools.py` 对齐。图片候选统一当前/引用/最近图；群个人发言从当前 scope 的滚动快照按真实发送者检索。

### 回复前证据编排

`_build_cognitive_turn_context_sync` 在线程中调用证据包构建器，读取 `text` 与结构化来源诊断；失败只记录来源和异常类型。当前昵称来自真实事件的群名片/昵称，语料开关与长度上限显式传入。生产预算仍是 `scene_k=12 / book_k=0 / max_chars=12000`。

认知证据和时间线放在缓存边界后的 system 消息；自适应上下文随本轮 user 内容。重建消息列表时保留这两类内容。生产工具清单确定后追加对应 Agent 教程，无工具线路不推荐具体工具，末尾 assistant prefill 的顺序不变。

### JSON 人格与上下文决策

`persona_store.py` 校验并原子保存 `data/personas/definitions/<name>.json`，`switch_commands.json` 保存完整指令映射；`personas.install_custom_personas` 将整批有效定义物化为冻结对象，保留内置对象。`persona_runtime.py` 统一工具/命令的数据操作，更新必须携带此前读取的文件 SHA。坏文件重载保留上次有效定义，部分失败如实回执。

`persona_admin.py` 解析直接管理命令；生成命令通过无工具主模型请求得到 JSON，最多一次修复，共享60秒默认截止时间。普通聊天不增加规划模型：主回复根据上下文直接填写 `catty_persona` 的定义参数。保存不自动切换，工具权限与 scope 来自真实事件。

`handle_chat` 准入后用 ContextVar 固定本轮 Persona。工具切换只保存覆盖选择，不重入已持有的聊天锁；下轮按 `name@definition_revision` 清理旧口吻历史。直接切换命令保留原来的转场锁顺序。人格目录配置变更重建注册表；手工改 JSON 后显式重载。

`request_intent.py` 在缓存边界后追加唯一的上下文意图说明；生产与模拟入口均使用 `tool_choice=auto`，不再按末句关键词强制绘图。模型结合前文、引用、补充、纠正和成功回执决定执行或回复，指代明确就沿用参数。

本轮实际开放 `catty_write_file` 或 `catty_python_run` 时，同一意图段补充可选文件交付能力；只介绍存在的工具。生成正文/源码并不要求执行 Python，短示例仍可直接回复。无工具重试刷新这一段并撤下文件能力，不改内置人格或工具权限。

### 按需技能与公共知识学习

`request_intent.py` 默认提示实质任务先查技能，未知或可能过时的事实查官方/一手正文；主回复仍自主选择工具。首轮仅有 `catty_skill_search` 入口，没有技能库目录、候选或正文。搜索命中后 `SkillTurn` 记录候选ID与SHA，下一次模型请求才增加带候选枚举的 `catty_skill_load`；模型选择后返回完整分块正文。

`execute_tool_call` 观察真实 `catty_fetch_url` 结果，合格的公共正文产生当轮不透明回执并开放 `catty_skill_save`。保存只收回执ID；独立生成请求不带聊天、画像或用户身份，在空ContextVar环境中提炼可共享知识。`skill_store.py` 用文件锁、SHA版本校验及fsync/replace写入 `data/skills/<id>.json`，所有会话共享检索。目录不入Git，保存不会重启bot；下次检索直接读磁盘新版本。

生产和模拟入口把静态工具与 `skill_tool_schemas(ctx)` 合并为 `tools_provider`；`chat_completion_with_tools` 每次模型请求前重新获取schema、允许名称并刷新自有意图提示。云端fallback保留provider及 `ToolReplyState`，deadline文字降级则不带工具/provider。动态schema同样纳入既有模型窗口预算，不预注入技能正文以占用上下文。仅有抓取回执不等于来源权威，正则筛查不保证识别所有隐私；来源判断、模型选择与线上质量仍需实测。

## 排队与历史预算

聊天锁仍串行处理同一会话。`reply_queue_max_wait_seconds` 只淘汰已过期的非定向群聊插话；已准入的私聊及 `directly_requested/mentioned/replied_to_self` 请求不会因前轮耗时而被丢弃。原准入、mention-only 与续聊开窗规则不变。

请求准备把历史之前的消息及工具 schema 算作前缀，加到会话软目标后再受模型窗口减输出预留的限制。历史按剩余额度保留完整轮次；若最新一轮也装不下，则该请求不带历史，不沿用持久化裁剪器的超预算末轮保留策略。本轮用户及工具后缀原样保留，磁盘历史不因请求裁剪被修改。新增 `prefix_input_tokens` 与 `effective_target_context_tokens` 诊断字段；它们是估算，不是供应商计费token。

## 执行与恢复

`_main_reply_with_deadline` 为一轮回复创建 `ToolReplyState`。工具完成时立即写入恢复快照，完整保留 assistant/tool 对应关系；主模型收尾超时后使用回执纯文本重试，不重做副作用。未取得结果的工具明确为未知状态。

生图只在线等待短窗口，慢结果交给后台投递；在线等待被取消时仍为受保护的生成任务登记投递所有者。

## 文件产物

- `code_sandbox.py`：会话+用户身份 → 成功工作副本 → 独立 run → 成功产物快照 `exports/<run_id>`。失败产物只用于诊断；packages 共享，普通运行不能改写。
- `file_formats.py`：文本 → 真 DOCX/XLSX/PDF 或明确的 UTF-8 原文 → fsync/原子暂存。格式依赖在 pyproject 中声明。
- `tools.py`：成功暂存文件进入 `pending_file_deliveries`；公共入口在文字结束后按原会话上传。`staged` 不是上传成功。

## 发送确认

`reply_delivery.py` 为主回复记录 pending / delivered / unconfirmed。发送确认后才更新好感与续聊；处理器退出时仅把已确认气泡提交至会话历史。后台图片/文件交付仍使用独立任务，不是跨重启持久化队列。

## 验证

`scripts/run_offline_regressions.py` 仅运行已审查清单，逐模块独立进程。在线评测需要显式入口，不参与默认回归。2026-10-27本地验收为55模块884项：874通过、9跳过、1项既有persona_admin浮点失败、0错误，退出1；技能四模块75项通过，350个Python文件语法通过。技能增量已部署：32文件双端哈希一致，远端18文件py_compile与12模块260项回归通过（9跳过），插件重载和重连已确认；真实模型与QQ效果未专项评测，见会话07。