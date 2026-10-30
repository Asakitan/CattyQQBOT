# 会话 23 · 动态 TTL 覆盖所有聊天会话

> 历史记录：此动态策略已由[会话33](session-33.md)的首轮固定12h策略取代。

## 目标与范围

- 按用户修正，将动态 TTL 从仅群聊扩展到全部有 session key 的主聊天请求，包括私聊、群内独立用户会话和自定义 session。
- 继续使用滚动 30 分钟至少 3 个逻辑回合的阈值；辅助任务、空 session 和不支持的模型或接口不参与。不改工具、人格或真实配置；用户明确要求后部署。

## 已完成

- 移除计数器和请求准入中的 `group:` 限定，以完整 scope 隔离状态；保留原 helper 名称和接口以兼容已部署调用方。
- 扩展判定测试和 WebSocket 换档测试，覆盖私聊与自定义 session 的升档、冷却、重试去重及续传。

## 验证证据

- 首轮 8 项 TTL 判定与准入测试通过。
- `E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p "test_responses*.py"`：66 项通过，包含群聊、私聊、群内独立用户及自定义 session 的升降档与 WebSocket 续传。
- `E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_openai_client.py`：65 项中 9 跳过，零失败/错误；合计 122 通过、9 跳过。
- 两份源码及测试编辑器诊断无错误。未运行无关表情工具测试，未调用真实模型。

## 部署状态与剩余事项

- `src/catty_qq_ai/prompt_cache.py`、`src/catty_qq_ai/openai_client.py` 与 `tests/test_responses_ws.py` 已经 `push_zip` 解压至 `D:/CattyQQAI`，3 文件解压、0 跳过；未推送配置。
- 按用户要求未额外做冒烟、逐文件哈希或真实模型请求；生产自动加载、缓存命中率及账单效果尚未复核。后续可观察真实请求的 policy 日志与缓存写入期限。