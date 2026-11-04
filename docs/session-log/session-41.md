# 会话 41 · 主动接话图片语境

## 目标与范围

检查群友未点名机机时，主动加入群聊对图片和问题的理解；保留正常看图、主动接话和静默能力。

## 已完成

- 通过远端 MCP 只读核对生产现场：2026-11-04 14:44:17 群友发图提问，14:45:55 与 14:46:02 有新消息，14:46:08 启动主动 `join` 生成，14:46:11 产生两次看图调用，14:46:23 发出截图里的回复。时间均为 UTC+8。
- 截图中的其他两条群友消息在生成前已到达，不是生成期间新消息导致的旧稿；日志没有完整请求或看图参数，无法断定模型实际选择的图片或具体推理。
- 主动提示补充近期话题、发言对象、图片用途与文字指代的判断，防止把表情或配图机械当成问题对象，也允许恰当时主动参与、追问或静默。明确要求分析图片时仍可看图。
- 主动快照把已有消息年龄以非负整数秒提供给模型；缺失或无效值不进入背景。明确需要沉默时允许输出 `NO_REPLY` 标记，消除原有控制标记禁令的冲突。

## 验证证据

- `E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p 'test_fdj_proactive*.py'`：119 项通过，覆盖主动调度、提示和看图接线。
- `E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p 'test_fdj_proactive_config_prompt.py'`：最终提示 22 项通过，覆盖图片指代、消息年龄和 `NO_REPLY` 例外。
- `E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p 'test_shared_persona_proactive.py'`：共享人格 19 项通过。
- 四份源码/测试文件的编辑器诊断无错误；未发真实模型请求，单次离线验证不能证明实际模型一定遵守提示。

## 部署状态与剩余事项

2026-11-04 14:58（UTC+8）经用户批准部署。`push_zip` 将两个源码和两份测试解压到 `D:/CattyQQAI`，4 个文件、0 跳过，服务端包 SHA-256 为 `b8e354176fa8bdf3dd4e3ce79015d38dae760ead485174b0221ef0335cf76431`；逐文件 SHA-256 双端一致：

- `fdj_proactive_prompt.py`：`F16298B1244E2C5AF5168EB25B4C9144DBFB019F2C10C9398B9518BF1A7F4221`
- `fdj_proactive_runtime.py`：`685E0AD47CD937FF0DEC0BA3B71C8764A9E825605162B796B799C9254B01A5E2`
- `test_fdj_proactive_config_prompt.py`：`126DA7BC51E17F56009F10662C08127FE4C3141A3C5FA56788992D0CAF3E0814`
- `test_fdj_proactive_integration.py`：`B4197D069BEFC147FE4D295B0F128D76750A4A566DC7925B9BA18BC62B892EE2`

部署前原件备份在 `D:/CattyQQAI/tmp/proactive-image-context-20261104/before/`。生产 Python 3.14.5 的 `py_compile`、主动链路 119 项及共享人格 19 项通过；14:58:34 插件加载并完成启动，14:58:55 OneBot 重连，心跳正常、加载后无 ERROR。生产配置、人格正文和运行数据未改，未发真实模型请求；实际 QQ 接话效果待观察。