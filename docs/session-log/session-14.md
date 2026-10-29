# 会话 14 · 日志驱动的 Harness 自修复与升级

> 本会话记录的是最初独立模型实现；缺省主 AI 路由与后续部署见 [会话 17](session-17.md)。

## 目标与范围

- 新增独立可配置的修复 LLM，由 `logs/` 中的 WARNING / ERROR 触发源码修复；1E 按每周 100,000,000 token 实现。
- 修复范围是 Catty 本仓库的运行与工具代码；候选先验证，再由守护进程协调应用和回滚。
- 保留现有工作区改动、人格内容与凭据；本次仅本地实现和验证，不部署、不提交。

## 已完成

- 独立包 `src/catty_self_repair/`：配置、原子存储、周预算、日志监测、源码工作区、可信验证器、模型循环、worker 和父级监督器；不导入 NoneBot 插件。
- `config.json` / `config.example.json` / loader 增加 `self_repair`，实际本地配置用 `HOST` / `TOKEN` / `TARGET` 占位、`enabled=false`；不借用主聊天密钥。
- UTC+8 周一自然周，硬上限一亿 token，逐请求预留与 usage 结算；未知费用跨周保留，降额持久化，账本损坏不重置。
- 增量读取主日志及新增守护日志，归并 traceback、脱敏、按故障指纹去重及冷却；WARNING / ERROR / CRITICAL 都可触发。
- 模型只通过六种受限工具查看、搜索、精确替换或新增允许的 Python 源码。人格正文、实际配置、用户数据、修复控制器及冻结测试不开放修改。
- 两份源码快照和固定测试，逐 case/subtest 比较结果；全量哈希绑定验证与发布。Windows Job / POSIX 进程组回收验证进程，清理环境、限制普通文件写入和网络；这不是操作系统安全沙箱。
- `scripts/catty_hot_reload.py` 在停 bot 后应用事务，`bot.py` 报告插件启动标记；监督器按 PID、标记和持续心跳确认，失败回滚，发布中断先恢复。人工文件冲突保留原件并阻止启动。
- `scripts/catty_self_repair.py` 提供 `--status`、`--once`、`--request`；手动与自动处理共用锁和周预算。`scripts/run_offline_regressions.py` 纳入八个新模块；`data/self_repair/` 已忽略。

## 验证证据

解释器 `E:/Py/python.exe`，Python 3.11.0；PowerShell 使用 UTF-8。证据目录为 `.vscode/handoff/self-repair-20261029/`。

| 测试模块 | 通过项数 |
| --- | ---: |
| `test_self_repair_config` | 8 |
| `test_self_repair_budget` | 18 |
| `test_self_repair_logs` | 23 |
| `test_self_repair_agent` | 29 |
| `test_self_repair_worker` | 24 |
| `test_self_repair_workspace` | 46 |
| `test_self_repair_supervisor` | 28 |
| `test_self_repair_acceptance` | 4 |
| **合计** | **180** |

- 进程验收启动本机 HTTP 模型夹具、worker、守护与临时 bot，实测日志→读源码→修改→固定测试→停机应用→新进程健康确认；另一个候选启动后退出 23，实测恢复旧源码并重新启动。还覆盖停 worker 期间关闭配置和活动事务丢失，防止过期发布或虚报回滚。
- 开工完整离线基线：1462 项、0 failures、45 errors、9 skipped（`baseline-regressions.log`）。
- 最终完整离线回归：1653 项、0 failures、42 errors、9 skipped，进程退出 1（`final-offline-regressions-v2.log`）。42 个错误均在既有 `test_fdj_proactive_integration` / `test_shared_persona_integration`，原因是 `_extract_incoming_with_voice` 未注入；不能称全量全绿。期间其它会话更新了已有测试及输入上下文，测试数量及那三项错误的改善不归于本功能。
- 真实仓库默认验证：冻结 419 文件，基线与候选分别执行 8 模块、各 193 项，完整性及结果比对 `ok=true`，退出 0；原样源码没有应用到 live。真实配置没有复制，缺文件基线错误保留在报告中。回执 `default-workspace-validation.json`，完整 run 为 `data/self_repair/runs/offline-preflight-4b9be3b6e563441dbb20926a1af65ca0/`。
- 第一次真实快照验证暴露 Windows `FileNotFoundError` 的 `repr` 双反斜杠导致错误身份不同。已统一原始/repr/JSON 路径（含中文目录），同时保留不同文件名和十六进制错误码，新增 5 项正反例。初次失败 run `offline-preflight-ce26184b998443da8ef09b6772ffbf04` 的 `validation.json` 保留，不覆盖失败证据。
- 开工后首轮预检确认 38 个既有配置段哈希不变；最终配置段对账、源码/文档哈希及验收源文件漂移核对由 `audit-delivery.py` 写入 `delivery-manifest.json`，不保存实际配置内容。

## 交付与启用

- 新包、CLI、八个测试模块及 `docs/self-repair*.md`；接线修改为 `bot.py`、loader、配置模板、守护、固定回归清单与忽略规则。
- `STATUS.md`、模块清单、架构、会话索引、本记录和仓库当前状态同步；未修改 `AGENTS.md` 或 `.vscode/claude-memory.md`。
- 启用需用户填写 `self_repair.base_url/api_key/model` 并打开 `enabled`。首次加载新版守护需要重新启动 `start_catty.bat` 的守护进程；之后配置热读。仅重启 bot 子进程不足以加载旧守护尚未拥有的新逻辑。

## 部署状态与剩余事项

- 本地实现与验证完成；缺少实际独立修复 LLM 配置，因此保持关闭。未调用真实修复模型、未验证真实 QQ、未修改远端。
- 开工 Git 归档为 `252e636b5cbf14cf31d3d3b1d5f1480c157bc098`。未部署、未提交；按项目规则，仅用户明确要求部署后才 push_zip，成功再归档。
- 工作区还包含其它未部署变更，后续部署须按交付清单核对共享文件，不能用 Git modified 列表替代部署范围；真实 `config.json` 单独按段合并，不整份覆盖远端。