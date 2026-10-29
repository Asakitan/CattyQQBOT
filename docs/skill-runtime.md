# 技能运行时：当轮资格与公共来源学习

## 范围与状态

`src/catty_qq_ai/skill_runtime.py` 管理每轮技能资格和公共来源学习，存储委托 `skill_store.SkillStore`。已接入工具注册、生产/模拟主请求循环及配置；人格与自动联网缓存不作为学习输入。

已部署到 `D:/CattyQQAI`，运行时26项、存储28项、工具15项、真实循环披露6项在本地和远端Python3.14.5均通过；未调用真实模型专项验证提炼质量。部署记录见 [会话07](session-log/session-07.md)。

## 公开接口与接入次序

- `SkillTurn(config: Any, *, completion_fn=None)`：每个逻辑聊天轮次创建一个实例；没有 `scope`、`user`、`messages` 参数。构造及 `schemas()` 不打开、不扫描技能库。不要跨轮复用实例。
- `await search(args: dict) -> dict`：参数 `query`（非空、最多 500 字符），可选 `limit`。默认最多3个候选，可配置到5个，且受回执长度上限限制；仅保留当轮实际返回的 `id → sha256` 资格，最多24个累计候选。候选不含正文，即便底层意外返回正文也会投影丢弃。
- `await load(args: dict) -> dict`：仅接受 `id`，必须为当轮候选，并按检索时 SHA 重新读存储。成功回执为 `{ok, skill: {id, title, description, keywords, body_chars, updated_at, sources, instructions}, sha256}`，不返回 `body`。只有成功交付回执才记录更新资格。
- `observe_tool_result(name: str, result: dict) -> dict`：同步；由可信执行器在实际工具完成后调用，返回新字典，不修改原结果。成功公共抓页结果增加不透明的 `skill_receipt_id`；输入中伪造的同名字段总是移除。
- `await save(args: dict) -> dict`：仅接受 `receipt_ids`（1～3 个不重复的当轮回执）及可选 `existing_id`。不接受主模型提交的正文、URL、时间、来源或 `shareable`。返回有界 `{ok, status, id, sha256}`，失败为 `{ok: false, error}`。
- `schemas() -> list[dict]`：OpenAI-compatible 动态 function schema，仅含当前开放的 `catty_skill_load` / `catty_skill_save`，永不包含 `catty_skill_search`。

调用方首轮仅注册技能类入口 `catty_skill_search`；其它非技能工具的既有策略不由本模块变更。搜索有候选后再将动态 load schema 送给主模型，由模型选择是否加载。只有实际成功的公共 `catty_fetch_url` 正文才开放 save。没有匹配知识时仅提示“搜索→抓公共页”，不捏造技能、不自行联网、不强制工具调用，`tool_choice` 保持 `auto`。

动态工具名称一旦开放，在本轮保持；后续空搜索、保存完成不会删除名称，避免 native 历史工具名失配。候选、SHA、来源及更新资格每次仍校验。`id` / `receipt_ids` 的枚举值来自当前实例；schema 每次返回独立结构，调用方修改它不能改资格账本。配置关闭优先于名称保持规则。

## 正文及回执预算

存储正文上限为配置值与 6000 的较小值；load 不静默截断正文。正文拆成 `instructions: list[str]`，每块最多 **1000 字符**，满足合同的 ≤1500 限制，同时避开既有 serializer 的 1024 字符 blob 启发式与 2000 字符字符串上限。以 `data:` / `base64://` 开头的块再拆小段，避免被 serializer 当作二进制替换，拼接仍精确还原正文。

回执中标题、描述、关键词、时间、URL、来源数量均有界。load 序列化长度超过 11500 字符（例如大量转义控制字符）会明确失败，不给更新资格；不会让既有 12000 字符 history 预算静默丢失正文。

## 来源账本

仅认可名称精确为 `catty_fetch_url`、`ok is True`、整数 `status_code` 在 200～299、非空 `text`、公共形式 HTTP(S) URL 的实际返回结果。不认搜索摘要、`auto_fetch`、失败/重定向/空正文、旧轮回执或模型编造来源。

每轮最多保留 3 个来源；相同 URL 与保留正文 SHA 的重复观察复用回执。每份保留正文去首尾空白后最多 6000 字符，超过 65536 字符的异常输入不入账。回执使用随机不透明 ID，`fetched_at` 由服务端观察时生成带 UTC 时区的时间，`text_sha256` 是**实际保留的正文片段**的 UTF-8 SHA-256，不冒充整页哈希。网页 title 为空时以该公共 URL 的 hostname 代替标题。

送入存储的 sources 严格投影为 `{url, title, fetched_at, content_sha256}`；`content_sha256` 是账本 `text_sha256` 的合同映射。来源文本不持久化，时间/URL/hash 绝不取自生成器输出。不自动标记 `official`：抓取成功只说明取到正文，不代表权威、真实性或网页指令可信。

URL 检查不重新联网：拒绝非 HTTP(S)、凭据 userinfo、本地/私网 IP、回环/本地名称、数字 IP 简写、畸形 host/port、明显凭据 URL。它仅作语法/地址分类复核；域名解析、重定向及 DNS 重绑定防护仍由原抓页执行器负责。不要把模型提供的字典直接送进 observer——此接口的信任根是实际工具执行结果，并不是加密远程证明。

## 隔离生成与隐私边界

提炼请求走 `openai_client.chat_completion_aux`（`audit_ai` → `ai_fallback` → 主 ai 的次要 AI 线路），只做纯文本请求、不挂工具，不占用主回复与识图模型。

内部生成仅使用固定中文系统提示和账本中的公共 `title/url/text`。不接收、不拼接聊天记录、用户画像、私聊原文、检索 query、用户 ID 或旧技能正文。生成任务在全新的 `contextvars.Context()` 中运行，防止主回复 scope、session、persona、蒸馏上下文等隐式传递。

`completion_fn(config, messages)` 可注入 async 函数或返回字符串的函数。默认实现懒加载 `openai_client.chat_completion`，临时 `set_distill_suppressed(True)`，在 `finally` 恢复。传入配置是深复制副本，`catty_max_tokens` 使用技能学习预算；剥除 extra_body 中的消息、用户、metadata、工具及覆盖 token 预算字段，不改原配置。

生成器必须输出单个合法 JSON：`{id,title,description,keywords,body,shareable:true}` 或 `{shareable:false}`。拒绝无效 JSON、重复字段、非布尔 true、无效/过长字段、明显凭据及 PII；生成器自带 sources/时间/权威声明不作为元数据使用。`shareable` 仅运行时审核，不作为 store 输入字段。网页资料只是数据，提示词明确禁止将网页指令升级为系统规则。

公共资料也可能包含私人信息，来源与生成内容均有保守拒绝信号（邮箱、部分电话/身份证形式、显式 QQ/住址、明显密钥与凭据等）。**正则与模型判断不能保证识别全部隐私**；本模块保证输入通道隔离和有条件拒绝，不声称完成通用匿名化，也不保证阻止所有网页提示注入或知识投毒。真正的全局知识适用性仍需复核来源和时效。

## 保存、更新、并发与取消

- 所有技能使用同一 `catty_skills_dir`，不按群/私聊/用户分库；只有批准分享的通用内容可写入。
- 更新要求 `existing_id` 本轮已成功 load；生成器无法自行指定覆盖目标。提交使用 load 时 SHA，生成期间其它写入造成的冲突由 store 拒绝，不覆盖新记录。
- 同一实例以排序后的 receipt 组合、更新目标、加载 SHA 作为缓存键；async 锁合并并发/顺序重复保存，只生成和提交一次，失败回执也缓存。跨实例/跨进程冲突与原子文件一致性仍由 store 负责。
- 生成阶段使用固定学习超时，默认20秒；非有限或非正值回退默认。生成超时、异常或取消不启动提交，取消向生成任务传播。
- 写前有取消检查点；文件锁、fsync和原子提交放在线程中，不阻塞聊天事件循环。若取消发生在提交已启动后，等待同一写入结束再传播取消，不重做或遗留无主写入；此时文件可能已保存，不能把取消说成回滚。总墙钟可能包含提交收尾时间。

## 默认配置

| 字段 | 默认值 |
|---|---|
| `catty_skills_enabled` | `True`；false 关闭所有技能接口/schema |
| `catty_skills_auto_learn_enabled` | `True`；false 仅关闭观察/学习/save，保留 search/load |
| `catty_skills_dir` | `data/skills` |
| `catty_skills_search_limit` | `3`，运行时最多5且受回执预算限制 |
| `catty_skills_max_body_chars` | `6000`，运行时最多 6000 |
| `catty_skills_learning_timeout_seconds` | `20.0` |
| `catty_skills_learning_max_tokens` | `2400` |

## 验证

在仓库根执行：

`E:/Py/python.exe -B -X utf8 scripts/run_offline_regressions.py --module test_skill_runtime --module test_skill_store`

也可单独执行：`E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_skill_runtime.py -v`。

专项测试覆盖无扫描构造、空搜索、检索不泄露正文、候选 enum、load SHA、动态名称保持、观察结果非原地修改、失败/假来源/跨轮拒绝、正文/URL/时间参数注入、公共字段与 ContextVar 隔离、无 title 资料、显式 PII/凭据拒绝、分享 false/无效 JSON、默认蒸馏恢复、超时取消无写、并发重复幂等、更新 SHA 冲突、下轮全局复用，以及通过**真实现有 history serializer**保留 6000 字正文。另有真实 `SkillStore` 临时目录往返，不能用双桩全绿代替合同兼容验证。