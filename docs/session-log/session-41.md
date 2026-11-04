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

## 追加：18:06 主动冒泡仍描述图片（已部署）

- 现场：18:06 前群友聊 AI 补全速度并配睡狗图，机机主动冒泡输出「我去 图里就一只狗躺那睡着了，AI 都把代码补全完了它还在睡」。
- 本轮 MCP PowerShell 被禁用，未读取生产日志；以截图为准，不推断看图调用或服务端提示。
- 主动提示改为只有群友明确要求识别、描述或分析图片时才看图；默认把图片当情绪、递梗或分享，不描述或概括画面，不写「图里/图中/图上/这图/这张图/看起来」，不念表情包配字。
- 运行时在发送前拦截：最近 300 秒没有明确问图，而回复含画面位置描述时跳过 `unrequested_image_description`；明确问图仍放行，`地图上` 等普通用法用负向后行断言避开。
- 验证：`test_fdj_proactive*.py` 123 项、`test_shared_persona_proactive.py` 19 项通过；四个源码/测试文件无编辑器诊断。
- 状态：2026-11-04 15:14（UTC+8）经用户批准部署，`push_zip` 解压四个文件、0 跳过，包 SHA-256 为 `74555998713b3754c34c64f6673e218bfc28bb51b8ca6a780a85675cf213c523`；双端逐文件 SHA-256 一致：
  - `fdj_proactive_prompt.py`：`421EC20880758740291C7C205076CB03C836AE1AAC2E5EA860151B38EE0CE654`
  - `fdj_proactive_runtime.py`：`4B9798AD8A7F91350BEA78CE77111BFC2B9CE5FE713759DC6427F3E55BF82467`
  - `test_fdj_proactive_config_prompt.py`：`4FC42F3FAEF61BE6F5118EEFA1C2C59332D2D108C72962B78E383E73FF61A51E`
  - `test_fdj_proactive_integration.py`：`8FC89FB30BC1692BC9B3E1F328B8F7F7C30042BE3CAB96B4A1F4A833CCA842AC`
- 生产 15:14:18 优雅重启，15:14:44 插件加载、15:14:45 启动完成、15:14:48 OneBot 重连，心跳正常、重启后无异常；`bot_startup_ready.json` 显示 pid 4768、`plugin_loaded=true`。生产 PowerShell 被禁用，远端 `unittest` 未运行；回滚副本在 `E:/VC/Catty/.vscode/handoff/deploy-proactive-no-image-desc-20261104/before/`（14:58 版）。