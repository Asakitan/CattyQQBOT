# 会话 35 · @ 后发消息、群上下文与异常文字

## 目标与范围

- 修复截图中机器人只回应单独 @、漏读随后正文的问题；真实出站日志含异常文字“咋了 ප්‍ර了”。
- @ 后保留 10 秒读取窗口，无后续正文时仍联系群聊上下文；同群他人的消息不得归到当前发言者名下。
- 保持人物身份、原图能力、固定 12h 缓存、静态前缀和 WebSocket 续传修复；完成后直接部署。

## 已完成

- 定位机机动态群聊上下文仅取前一条，且不包含后发正文；新增回归复现后修复。
- `mention_followup.py` 保留 10 秒短时窗口，按机器人/群/用户/人格隔离；同人正文、图片和语音合并至原 @ 轮，已消费消息不重复回复，取消释放窗口。明确回复别人、@ 别人、斜杠命令和文件消息不合并。
- 配置/人格变化放弃旧轮；错误回复绑定该轮已选人格。群聊窗口保留多位发言者与同文不同人，严格 @ 人格窗口保持。
- 无来源异文字符不再通过短句免检，沿用现有审校线路重写；外语引用、翻译和代码保留，失败使用既有人格错误回复。
- 人格正文、静态骨架、缓存客户端、固定 12h、历史锚点和 WebSocket 实现未改；仅动态群聊上下文增加实际邻近消息。

## 验证证据

- 生产 2026-10-30 16:40:51 将裸 @ 转成通用开场请求，16:41:01 出站异常回复；未把截图差异误判成显示问题。
- 本地与生产各 281 项集中验收通过：`test_mention_followup`、`test_fadianji_style_critic`、`test_shared_persona_integration`、`test_fdj_proactive_integration`、`test_reply_message_composition`、`test_vision_context_images`、`test_prompt_audit_fixes`、`test_responses_ws`、`test_reply_deadline`、`test_persona_memory_isolation`。零失败/错误/跳过。
- 九份源码/测试逐文件 SHA-256 一致；生产 `openai_client.py`、`prompt_cache.py`、`responses_ws.py` 保持部署前字节，固定 12h 及续传实现未动。
- 没有重复全量回归；未发云模型或真实 QQ 测试消息。

## 部署状态与剩余事项

- 九份源码/测试已通过 `push_zip` 部署到 `D:/CattyQQAI`，九份解压、零跳过；七份相关文档随本轮同步。
- 16:54:44 插件加载成功，16:54:45 应用启动完成，16:54:47 OneBot 重连。
- 部署前文件备份在 `D:/CattyQQAI/tmp/mention-followup-20261030/before`，本地清单及交接在 `.vscode/handoff/mention-followup-20261030/`。
- 不改真实配置、人格正文和其他并行任务；未额外发起真实模型或 QQ 测试消息，使用效果待后续真实聊天观察。