# 会话 32 · QQ 附件双向顺序与格式修复

## 目标与范围

修复群聊附件出现 `Cache_...: unsupported_format` 和 `attachment.bin: unsupported_format` 的反馈。用户要求无指向文件不浪费 token、不全量下载缓存，并明确两种顺序都支持：先发文件后说看、先说看后发文件。最后要求直接部署并推送。范围为附件接收、短期元数据/等待状态、格式识别、群聊准入和对应测试，不改模型、人格或生产配置。

## 已完成

- 知识库原先只按文件名后缀选解析器；接收端只取 `name`，空上传通知还能生成假 `attachment.bin`。现在兼容 `name/file_name/filename`、忽略空条目，并按文件 ID/URL 合并同事件重复条目。
- 无后缀与 `.bin` 文档按 PDF 文件头、Office 包结构及可解码文本识别原支持格式；保留原字节、路径和大小约束、归档检查。真正不支持的二进制仍拒绝；失败显示中文说明。
- 普通上传只观察元数据；群内无指向文件消息在模型判门前退出，续聊不提升。下载前再次校验明确 @、回复机器人、已判定的直接请求或私聊上传，不获取无关文件 URL、不下载、不解析、不入库、不回复错误。
- `observe_files` 按机器人、用户、会话、人格隔离，仅内存保留最多 256 组、每组 4 份元数据、10 分钟有效。紧随上传的明确“看一下”可承接前条文件；隔了其它话题需明确引用文件。问候、否定读取、过期和其它身份不触发下载。
- 本轮仅向主模型补充文件名、标识与可用状态，不含全文或下载 URL；使用现有文档工具解析和读取，未读取前不声称看过。原群上传不提前入库。
- 先说看后上传使用仅内存的 2 分钟、最多 256 组单次等待，按同一机器人、用户、会话和人格匹配；取消或本用户转移话题后失效。昵称直接请求复用原提取器。消息和上传通知都恢复原阅读要求，继续原主回复流程；未来文件要求不误读旧附件，广播不创建等待。
- 旧知识库、群文件与其它缓存未删除；不建立群文件全量缓存。明确入库的原件与提取文本仍由既有删除入口管理。

## 验证证据

初始回归复现 5 个失败与 2 个错误；格式和接收修复后 47 项通过。用户补充准入要求后，最终使用固定离线运行器一次检查四个相关模块：

```powershell
E:/Py/python.exe -B -X utf8 scripts/run_offline_regressions.py --module test_task_host --module test_knowledge_base --module test_shared_persona_integration --module test_fdj_proactive_integration
```

准入初版共 125 项通过。先发后读的最终验收：`test_task_host` 39、`test_knowledge_base` 22、`test_shared_persona_integration` 21、`test_fdj_proactive_integration` 52，共 134 项，0 失败、0 错误、0 跳过。覆盖真实注册入口先观察再引用、上传通知后读取、元数据上限、提示不含正文或 URL、跨身份/过期/间隔话题/否定读取，以及原先的零下载/入库/回复和模型判门负例。明确 @、私聊、真实 DOCX/XLSX 字节及未知二进制拒绝仍通过。

双向顺序最终本地和生产均为 142 项：宿主 46、知识库 22、共享人格 22、主动集成 52，0 失败/错误/跳过。新增先请求后上传、一次消费、过期/取消、昵称请求、上传通知恢复、真实 OneBot 事件进入原群聊规则与未来文件不取旧附件。生产源码 `py_compile` 通过；远端固定运行器尚不认识 `test_task_host`，实际四组使用 `python -m unittest discover -s tests -p <module>.py` 独立运行，未修改或部署运行器。

## 部署状态与剩余事项

已通过 `push_zip` 部署上述三份源码和三份测试到 `D:/CattyQQAI`，六文件双端 SHA-256 一致、0 跳过，生产配置哈希未变。15:59:31 插件加载、15:59:32 服务启动、15:59:52 OneBot 重连，心跳正常、重载后无新错误。未调用云模型测试，截图未提供原附件字节，真实 QQ 文件交互尚未复验。

本地冻结清单在 `.vscode/handoff/attachment-bidirectional-20261030/manifest.json`，生产备份在 `D:/CattyQQAI/tmp/attachment-bidirectional-20261030/backup`；部署 ZIP SHA-256 为 `6f1d22b89873a3b784b3a187e9f6bb28dc7595cb1b53cdb964ab9660bb9cc8bc`。Git 归档和连接最终确认见 `.vscode/handoff/attachment-gate-20261030.md`。