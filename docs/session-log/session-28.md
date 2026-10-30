# 会话 28 · 缓存重写排查与主动冒泡分线

## 目标与范围

- 按生产日志定位缓存低命中来源并修复可复现的本地原因。
- 主动冒泡生成改走 DeepSeek 辅助线路；保留调度、人格、群权限与发送逻辑。
- 用户明确要求后部署代码和测试；不额外调用生产模型，不改真实配置。

## 已完成

- 生产 2026-10-30 日志存在正常热请求约 97% 命中与穿插的大额缓存重写；固定工具组合也有零命中，不能全部解释为工具变更。
- 主动冒泡此前绑定主 `chat_completion`，本地改为独立 `proactive` 请求类型与 DeepSeek 辅助完成入口。
- 对齐截图 UTC 日期的采样区间从 UTC+8 08:00 到 12:27:24：44 次 Terra 请求、总输入 5652593、读取 4401552、写入 1106560 token，命中 77.87%，12 次零命中。与截图时间窗口不完全相同，不作同一份统计或固定输入 A/B。
- 明确缺陷：原 12h 仅标静态段，长历史仍 30m；deadline 恢复撤下 tools 并降低请求级思考参数。日志 10:15:08 的无工具同群请求写入 169863 token，邻近有工具请求约 97% 命中；不把所有零命中都归于这两点。
- `HistoryCacheAnchors` 固定长历史 user 端点，不随新消息滑动；新历史/隐式端点保持短缓存。内容或设置不匹配时失效，元数据在现有 WebSocket 缓存旁原子保存，最多 1024 条，无正文。
- WebSocket 续传指纹纳入实际缓存设置与长断点位置；同名工具 schema 改变也完整重放，`tool_choice=none` 不视为设置改变。新增 `responses cache layout` 指纹日志。
- `ToolReplyState` 保存最后请求及 schemas；官方 Responses deadline 重试保留工具定义和思考配置，禁用调用，已成功回执不重做。其它端点保持原恢复行为。
- `aux_route(deepseek_only=True)` 仅选择 audit/fallback 的完整 DeepSeek 配置；两个冒泡入口均绑定独立完成器，缺配置不调用主模型，原调度与确认路径不改。

## 验证证据

- 最终五组定向回归：`test_responses*.py` 72 项、`test_openai_client.py` 69 项（9 跳过）、`test_reply_deadline.py` 10 项、`test_fdj_proactive_integration.py` 52 项、`test_shared_persona_proactive.py` 17 项；合计 211 通过、9 跳过，零失败/错误。
- 覆盖固定断点增长/冷却/重启/内容变化、设置变化重放、无执行器收尾、动态 schemas 恢复、无 DeepSeek 时拒绝主线、实际冒泡宿主接线。仅替身或本地传输，未发生产模型请求。
- 三份源码和四份测试编辑器诊断通过；未跑无关语音、搜图或全目录测试。

## 部署状态与剩余事项

- 源码 `src/catty_qq_ai/{openai_client,prompt_cache,__init__}.py` 和四份测试已 push_zip 至 `D:/CattyQQAI`，7 文件解压、0 跳过；未推送真实配置。
- 生产命中率、费用变化及真实主动消息尚未验证；可观察后续真实请求的 `responses cache layout`、缓存写入期限和独立 proactive 路由，无需额外收费探针。