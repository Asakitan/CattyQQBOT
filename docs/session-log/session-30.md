# 会话 30 · 单独 @ 承接上一条图片

## 目标与范围

- 用户反馈先发成绩截图、随后单独 @，机器人回复没看清图片；检查 Catty 图片组装与 Responses WebSocket 链路。
- 只修本地请求路由和测试，不改变模型、配置、人格、图片下载、缓存协议或生产部署。

## 已完成

- `handle_chat` 原本只按文字指代收集前文图。裸 @ 已被接收并触发回复，但自动生成的文字不满足条件，图片没有加入请求。
- `_is_bare_image_wakeup` 识别群聊单独 @、无附加文字、当前图、表情或语音；有引用图时维持原引用路径。
- `_recent_context_images_for_scope` 的可选 `preceding_user_id` 仅选请求前同一发送者最后一条消息，不跳过中间文字寻找旧图。保留同群、窗口、张数、有效期和来源标签。
- 添加四项上下文回归及一项真实回环 WebSocket 测试，检查 PNG 原字节、图片进入增量帧、后续文字增量及断线完整重放。

## 验证证据

```powershell
E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_vision_context_images.py
E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_responses_ws.py
```

- 图片上下文 23 项、Responses WebSocket 36 项，共 59 项通过；三个源码/测试文件编辑器诊断为空。
- WebSocket 使用回环地址随机端口、合成 PNG 和占位令牌，运行真实传输与转换代码，未调用云模型。
- 未取得该 QQ 请求的实际出站帧；这些测试不代表真实成绩图已被模型读对。

## 部署状态与剩余事项

- 以生产原文件为基线应用本次三处改动，未携带同工作区任务中心会话的未完成代码；生产入口不含 `task_host` 或 `traced_tool_call`。
- 部署按既有 `push_zip` 通道完成：`src/catty_qq_ai/__init__.py`、`tests/test_vision_context_images.py`、`tests/test_responses_ws.py`、本文件均 0 跳过，双端 SHA-256 逐项一致（入口 `bc50f411…`、两份测试 `a3117024…`/`0a6c1309…`、本文 `a41ba054…`）。
- 归档提交 `eedd6fe`（仅入口、两份测试与本文件；入口文件与生产内容逐字节一致，仅 CRLF/LF 差异），已推送到 `origin/master`。共享状态与架构文档因并行会话仍在编辑，留在工作区未单独提交。
- 远端 Shell 当前被禁用，未在生产执行 `py_compile` 或测试；生产加载与真实 QQ 的“先发图、单独 @”结果待观察。