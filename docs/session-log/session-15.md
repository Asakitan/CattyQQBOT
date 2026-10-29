# 会话 15 · Terra Responses WebSocket 主/识图线路

## 目标与范围

- 主模型与识图切到 `gpt-6.1-terra`，走 OpenAI Responses API + WebSocket。
- WebSocket 需要支持 `previous_response_id` 断点续聊、连接内缓存、断线重放和 HTTP 回退。
- NSFW spark 继续走 DeepSeek；OpenAI 生图工具改用同一把新 key 并可调用。
- 不修改远端配置，不部署；不打印或提交 key。

## 已完成

- `config.py` / `catty_config_loader.py` 新增 `catty_openai_websocket_*` 字段与 `ai` 段 env 映射：enabled/url/store/previous_response/stream_id/include/connect timeout/idle timeout/max age/cache ttl/cache path/fallback。
- `responses_ws.py`：`ResponsesWebSocketTransport` 维护持久连接，发送 `response.create`，按 `stream_id` 路由事件，单 lane 串行；`ResponseChainCache` 保存 scope→response id、已覆盖 input/output item 指纹、generation、store、时间戳，原子写 `data/responses_ws_cache.json`。
- `openai_client.py`：
  - `_get_ws_transport` / `_get_ws_chain_cache` 单例，按 URL/key/model/header/proxy 变化重建；
  - `_stream_responses_ws_attempt` 把 chat payload 转 Responses body，计算 input item 前缀，命中缓存时只发新增 input + `previous_response_id`；记录 completed response id；连接断开或 `previous_response_not_found` 清缓存并全量重放一次；
  - 主重试循环在 WS 失败时按配置回退 HTTP；`_payload_to_responses_body` 将 `reasoning_effort` 转 `reasoning.effort`（`max`→`high`），丢弃 `thinking`；`input_image.image_url` 改为字符串 URL；
  - 缓存前缀包含上一轮 assistant message/function_call 输出，避免下一轮把 assistant 回复重发。
- `config.json`：`ai.base_url=https://api.openai.com/v1/responses`、`model=gpt-6.1-terra`、WS 开关、`store=false`、`previous_response=true`、`include=["reasoning.encrypted_content"]`、`prompt_cache_key` 开、`fallback_http=true`；`vision` 同线路同模型；`imagegen.backend=openai`、`base_url=https://api.openai.com/v1`、`model=gpt-image-2`、`force_http_scheme=false`、同一把新 key；`filter.nsfw_spark_*` 保持 DeepSeek 未改。
- `config.example.json` 补 `ai.websocket_*` 默认键；`pyproject.toml` 加 `websockets>=16.0`；`scripts/run_offline_regressions.py` 加 `test_responses_ws`。
- 新增 `tests/test_responses_ws.py`：本地 fake WebSocket 服务验证帧形状、`stream_id`/default lane 路由、`previous_response_id` 透传、断开异常、error 帧、缓存 round-trip/TTL、URL 推导；`tests/test_responses_api.py` 更新识图 `image_url` 断言。

## 验证证据

- `py_compile`：`responses_ws.py`、`openai_client.py`、`config.py`、`catty_config_loader.py` 通过。
- 定向：`test_responses_ws` 5、`test_responses_api` 21、`test_openai_client` 64（9 skip）、`test_input_context` 14、`test_config` 17、`test_config_loader` 17、`test_shared_persona_nsfw_imagegen` 8、`test_imagegen_force_tool` 5 全通过。
- 离线清单：1467 项，0 failures、42 errors、9 skipped；42 errors 仍只来自既有 `test_fdj_proactive_integration` / `test_shared_persona_integration`（`_extract_incoming_with_voice` 未注入）。
- 真实 OpenAI 探测（未打印 key）：
  - `wss://api.openai.com/v1/responses` + `gpt-6.1-terra` 文本非流式/流式通过；
  - 同连接 `previous_response_id` 只发新 input，第二轮仍能答出第一轮码词；
  - 两轮工具调用（`get_number(7)`）通过，第二轮输入仅 1 个 function_call_output；
  - 红图识图返回 red；
  - `GET /v1/models` 200，`gpt-image-2` 在列；`POST /v1/images/generations` 200，687917 bytes。
- 未验证：QQ 端到端、真实群聊并发/长上下文、50 分钟换连接与网络抖动、远端 Python 环境是否安装 websockets。

## 部署状态与剩余事项

- 本会话结束时未部署；后续已由 [会话 16](session-16.md) 部署并合并远端配置。
- 远端生产虚拟环境已有 `websockets 16.0`，依赖检查通过。
- `store=false` 的续聊只在当前连接内有效；进程重启或连接换代后首轮会全量重放，这是协议预期。
- 生产若并发 scope 超过 32 个 named lane，服务端会返回 `websocket_stream_limit_reached`，当前按失败回退 HTTP；后续可做连接池或 lane 复用。
