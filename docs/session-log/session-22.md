# 会话 22 · 活跃群动态缓存 TTL

> 历史记录：此动态策略已由[会话33](session-33.md)的首轮固定12h策略取代。

## 目标与范围

- 按已发起群主请求的逻辑回合选择稳定前缀 TTL：最近 30 分钟至少 3 轮（含当前轮）用 `12h`，其余用 `30m`；历史断点保持 `30m`。活跃度不是群内全部消息数，也不等于已成功送达的回复数。
- 修改缓存策略、定向测试和相关文档，并在用户明确要求后部署；不额外调用真实模型，不改人格和工具权限。

## 已完成

- `prompt_cache.GroupCacheTTL` 按 scope 与逻辑回合去重，群间隔离，状态按群数与每群回合数限制容量。
- 阈值为 `WINDOW_SECONDS=1800`、`MIN_TURNS=3`；容量为 `MAX_GROUPS=1024`、`MAX_TURNS=128`，群数超限淘汰最久未访问的群。状态仅在内存中，重启重新统计。
- `openai_client._select_responses_cache_ttl` 限官方 GPT-5.6+ Responses 的 `main/chat` 群请求；私聊、辅助任务、兼容网关及旧模型保持原行为。TTL 写入逻辑回合上下文与 `responses_cache_ttl` 诊断，同回合重试不换档。
- 稳定 developer 断点写入选定 TTL，历史断点和隐式缓存保持默认 `30m`。不修改提示词、工具名单、输入顺序或费用统计口径。
- WebSocket 首个稳定项的续传指纹区分长 TTL；升档/降档时重放完整输入以传递新断点，同档位继续增量。通用 `item_hash` 的滚动断点忽略规则不变。
- 日志新增 `responses cache policy scope=... ttl=...`。已有 `30m` 缓存升档需重新按 `12h` 写入；降档不会撤销已写长缓存。动态策略不保证省钱或固定命中率。

## 验证证据

- `E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p "test_responses*.py"`：65 项通过，覆盖 9 项新增的活跃/冷却、重复回合、容量、路由隔离、混合断点和 WebSocket 升降档测试。
- `E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_openai_client.py`：65 项，9 跳过，零失败/错误。合计 130 项，121 通过、9 跳过。
- 两份源码及测试的编辑器诊断无错误。全部传输使用本地服务器或替身；未调用生产模型。
- 当前环境不能按 `tests.test_responses_ws` 包名导入测试，实际验证使用上述 discover 入口。

## 部署状态与剩余事项

- `src/catty_qq_ai/prompt_cache.py`、`src/catty_qq_ai/openai_client.py` 与 `tests/test_responses_ws.py` 经 `push_zip` 解压至 `D:/CattyQQAI`，3 文件解压、0 跳过；未推送配置。
- 遵用户要求未额外做冒烟、逐文件哈希或真实模型请求；生产自动加载、缓存命中率与账单效果尚未复核。后续可观察真实群请求的 policy 日志与实际缓存写入期限，无需额外生成模型请求。