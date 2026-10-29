# 会话 19 · Responses 缓存修复

## 目标与范围

- 排查主机器人 Responses 缓存长期显示为零，修复并部署请求及缓存统计；不修改生产配置或聊天数据。

## 排查与修复

- 生产日志的 Terra 大上下文在 19:21、19:22、19:25 连续返回零命中，输入分别为 140801、141327、143302 token；19:26 和 19:28 较短请求命中 98%，因此不是所有请求都为零。
- 原 Responses 转换将 system 消息提到单一 `instructions`；本地修复对官方 OpenAI GPT-5.6 及以上的既有稳定标记建立显式断点，并保留隐式缓存及标记后的消息顺序。标记缺失或其它兼容端点继续原有转换；前缀或工具变动、缓存过期与服务端路由仍会造成未命中。
- 原响应转换丢弃 `input_tokens_details.cache_write_tokens`，导致日志与面板的缓存写入显示为零。修复后保留读、写和 TTL 明细，普通输入扣除读写；Terra/Sol 的面板估算包含 30 分钟、12 小时写入倍率和自定义覆盖。

## 验证证据

- 本地 `test_responses_api` 28 项、`test_responses_ws` 10 项、`test_dashboard_state` 8 项、`test_cache_cohorts` 20 项、`test_openai_client` 64 项（9 跳过）、`test_prompt_cache_deepseek` 6 项，其余均通过；源码及三组测试文件 `py_compile` 通过。
- 使用生产账户和现有代理发送合成内容，独立 HTTP 两轮均为 200：首轮写入 14014、次轮读取 14002 token；独立 WebSocket 两轮均 completed，首轮写入 10812、带 `previous_response_id` 的次轮读取 10812 token。仅验证请求格式与服务端缓存，未发送真实聊天内容。
- 部署后用生产代码和隔离合成内容走 WebSocket：首轮输入 9628、写入 9625 token；次轮输入 9647、读取 9625、写入 19 token，确认使用 `previous_response_id`。真实 QQ 回合未验证。
- 远端相同六组测试共 136 项、9 跳过，其余通过；部署前重新运行本地三组受影响测试共 46 项通过。

## 部署状态

- 两份源码与三份测试经 `push_zip` 部署至 `D:/CattyQQAI`，逐项 SHA-256 与本地交付包一致；原有四份文件备份在 `tmp/responses-cache-backup-20261029-a7f2/`。
- 自动重载后机器人心跳正常、OneBot 8080 连接已建立；生产配置和聊天数据未修改。

## 后续：66% 历史缓存命中排查

- 生产 19:59–20:05 的单次请求反复读取 30800 token、写入约 1.4–1.6 万 token，命中 65–69%；31 个工具及前缀保持一致、无历史裁剪。请求间隔仅几十秒，不是 30 分钟过期，也不是累计面板口径。
- 只读比较生产会话与实际 Responses 续聊指纹：276 条落盘历史中，前 274 条与上轮发送序列连续一致，分歧从最新 user 开始。现有全量隐式写入包含本轮动态上下文，下一轮不会保留这段，历史没有独立写入端点可复用。
- 本地补最近两个历史 user 内容块的滚动显式断点，保留静态断点与隐式缓存；不裁内容、不改角色、不改变 TTL。WebSocket 内容指纹只忽略协议输入块上的缓存断点，正文、图片、角色和工具参数仍严格比较。
- 本地一次性运行 `test_responses_api` 33 项、`test_responses_ws` 13 项、`test_prompt_cache_deepseek` 6 项，共 52 项通过；四个代码/测试文件语法和编辑器诊断通过。新增测试覆盖 137 轮历史与动态尾部的共享写入端点、图片不变、当前 user 不标记、工具回执保持、旧端点及四轮 WebSocket 续传。
- 历史缓存增量的两份源码及两份测试经 `push_zip` 部署至 `D:/CattyQQAI`，四文件均解压成功并归档于 `5db9ea9`；基础修复为 `d64a0cf`。没有额外生产模型请求或重复交付哈希；后续真实请求观察见下。
- 参考官方 [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) 与 [Responses create](https://developers.openai.com/api/reference/resources/responses/methods/create)：内容块的显式断点不会关闭隐式缓存；不在不支持该字段的 assistant `output_text` 上添加断点。

## 后续：固定指南与工具续轮优化（已部署）

- 历史断点部署后，20:31:17 私聊读取 32896、写入 4807、输入 37706，单次命中 87.2%；工具续轮 86.9%。20:35:34 另一群聊读取 138359、写入 6607、输入 144969，命中 95.4%。scope 与原 66% 样本不同，不能当成严格 A/B；这些数字来自既有生产日志，没有追加模型请求。
- 固定请求理解、文件交付、技能联网、证据指南在包含相应工具的合成组合下分别为 604/264/336/474 个 o200k token，共 1678。原转换把它们塞进每轮当前 user；工具续轮又因结尾为 tool 而不折叠，导致同一消息的 wire 字节不同。
- 本次优化限定官方 GPT-5.6+ Responses：保留原 user 与动态 developer 项，只把与原生成器及当前工具名单完全相同的两份指南移到稳定断点前。未知文案、近似标题、用户内容、真实状态与检索证据不移动；旧兼容端点和 DeepSeek 不变。
- 工具循环只在上述线路保留未被净化的合法参数 JSON 原字节；重复键、敏感字段、非有限值、超长数据仍净化或摘要。服务端输出指纹不改写，真实内容差异仍拒绝续传。
- 定向回归：`test_responses_api` 37、`test_responses_ws` 18、`test_prompt_cache_deepseek` 6、`test_openai_client` 64、`test_input_context` 14、`test_request_intent` 33、`test_harness_prompt` 20、`test_tool_history_recovery` 9，共201项、0失败/错误、9跳过；三文件语法与编辑器诊断通过。模块各自独立进程，无生产模型调用。
- 新增回归使用真实工具循环和假传输，覆盖当前user/动态证据原样保留、两份固定指南仅出现一次、刷新位置不影响前缀、合法JSON原字节、旧协议、相似文案/用户文本不前移、重复键与敏感内容净化。工具名单不变且参数未净化时续轮只发回执和短提示；能力或参数投影变化时全量重放。
- 用户明确要求部署后，`src/catty_qq_ai/openai_client.py`、`tests/test_responses_api.py`、`tests/test_responses_ws.py` 经push_zip推送到 `D:/CattyQQAI`，3文件解压、0跳过；另同步STATUS、模块清单、架构、本文/索引、工具合同和技能提示合同。不含并行语音、实际配置和聊天数据。
- 部署沿用上述本地验收，不重复远端测试、冒烟、模型请求或交付哈希。当前没有新版线上98–99%证据，后续观察同一会话正常请求的真实读写比例。