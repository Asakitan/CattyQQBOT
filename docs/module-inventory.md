# 模块映射与状态

| 源码 | 职责 / 交付目标 | Status |
| --- | --- | --- |
| `src/catty_qq_ai/code_sandbox.py` | Python 工作区、产物快照、运行日志 → `docs/code-sandbox.md` | 已部署 |
| `src/catty_qq_ai/tools.py` / `file_formats.py` | 工具 schema、真实文件格式、图片与群历史引用 → `docs/tool-contracts.md` | 技能接线已部署 |
| `src/catty_qq_ai/openai_client.py` | 工具回执、前缀与会话预算、逐轮schema更新 → `docs/reply-recovery.md`、`docs/architecture.md` | 动态工具增量已部署 |
| `src/catty_qq_ai/__init__.py` / `reply_delivery.py` | 排队保留、回复重试、后台交付、确认后记账 → `docs/architecture.md` | 技能provider已部署 |
| `src/catty_qq_ai/prompt_manager.py` | 学术长度豁免与有效提示词注册 → `docs/prompt-testing.md` | 已部署 |
| `config.py` / `catty_config_loader.py` / 配置 JSON / 训练面板 | 删除失效系统提示字段；技能配置及env映射 → `docs/skill-runtime.md` | 7项技能默认配置已部署，生产config.json未改 |
| `tests/test_prompt_size_ablation.py` | 显式开启的在线评测，导入不执行 | 导入隔离已验收，未在线运行 |
| `scripts/run_offline_regressions.py` | 固定清单，每模块独立进程 | 55模块884项：874通过、9跳过、1既有浮点失败、0错误 |
| `src/catty_qq_ai/fadianji_harness.py` / `harness_prompt.py` | v2 证据编排与工具感知教程 → `docs/harness-evidence.md` | 已部署 |
| `src/catty_qq_ai/fdj_corpus_store.py` / `catty_config_loader.py` | 语料作用域及 FDJ 配置别名 → `docs/harness-config.md` | 已部署 |
| `scripts/fadianji_harness_cli.py` / `benchmark_harness.py` | 本地预览、显式生产预算、合成基准 → `docs/prompt-testing.md` | 已部署 |
| `persona_store.py` / `persona_override_store.py` | JSON 定义、指令映射、版本与原子发布 → `docs/persona-json.md` | 已部署 |
| `personas/__init__.py` / `persona_defaults.py` / `persona_runtime.py` | 动态注册、中性默认、逐轮快照与会话切换 → `docs/persona-runtime.md` | 已部署 |
| `persona_admin.py` / `ADMIN_COMMANDS.md` | 导入、生成、自定义指令与手册 → `docs/persona-generation.md` | 已部署 |
| `tools.py` / `request_intent.py` / `catty_sim_chat.py` | 上下文意图、可选文件能力、默认研究与技能提示，auto工具选择 → `docs/tool-contracts.md`、`docs/skill-guidance.md` | 研究增量已部署 |
| `tests/test_reply_queue_retention.py` / `test_request_intent.py` / `test_openai_client.py` / `test_reply_deadline.py` / `test_persona_runtime.py` | 排队准入、工具提示、预算与恢复合同 → `docs/session-log/session-04.md` | 已部署，双端一致 |
| `tools.py` / `__init__.py` / `features.py` / `config.py` / `catty_config_loader.py` / `config.example.json` | 移除联网搜索冷却、配置项与死辅助函数 → `docs/session-log/session-05.md` | 已部署，双端一致 |
| `src/catty_qq_ai/skill_store.py` | 全局JSON技能、候选检索、SHA校验、原子保存 → `docs/skill-storage.md` | 已部署，本地/远端28项通过 |
| `src/catty_qq_ai/skill_runtime.py` | 当轮资格、动态schema、公共来源账本、隔离提炼 → `docs/skill-runtime.md` | 已部署，本地/远端26项通过 |
| `tests/test_skill_*.py` / `test_request_intent.py` | 存储/运行时/工具/真实循环披露与研究提示 → `docs/session-log/session-06.md` | 技能75项及提示33项本地/远端通过，模型效果未专项验证 |

## 更新记录

| 日期 | 变更 | 验证 |
| --- | --- | --- |
| 2026-10-27 | push_zip部署默认研究与技能增量，生产配置/运行数据保持 | 32文件双端哈希一致；18文件py_compile通过；远端260项无失败/错误、9跳过；01:54:03重连 |
| 2026-10-27 | 默认研究提示、按需技能与公共学习；恢复错误回写；未部署 | 技能75项通过；55模块884项仅剩1项既有浮点失败；350文件语法与保持性通过 |
| 2026-10-27 | 移除 `catty_web_search` 与回切快路径的联网搜索冷却，删除配置项和死辅助函数；其它限流保持 | 800 项中 799 通过、1 项无关浮点断言失败；py_compile 退出0 |
| 2026-10-27 | push_zip 推送 11 文件（4 源码、4 根配置/文档、3 文档）；远端 `config.json` 未改 | 11 文件双端 SHA-256 一致；远端 py_compile 退出0；六组测试 OK；01:13:19 重启、01:13:27 OneBot 重连 |
| 2026-10-26 | 完成工具与提示词一致性修复；删除失效配置，保留人格与 NSFW 文案；未部署 | 465 项离线回归，326 文件语法通过 |
| 2026-10-26 | 完成 harness v2、来源诊断、作用域、教程与 CLI；保留生产预算和人格 | 622 项离线回归；合成优先级与成本基准通过 |
| 2026-10-26 | JSON 人格、聊天生成保存/切换、指令绑定、上下文自主决策；管理员手册与示例同步 | 782 项通过；343 文件语法与保持性检查通过 |
| 2026-10-26 | push_zip 部署到 `D:/CattyQQAI`；远端删除失效 `chat.system_prompt` 并重跑测试 | 2333 文件双端哈希一致；py_compile 退出0；12 模块 235 项通过；归档 `202971c` |
| 2026-10-26 | 修复定向排队丢弃、长前缀挤掉历史；按实际 schema 提示可选文件交付 | 800项，0失败/错误、9跳过；344文件语法与保持性检查通过 |
| 2026-10-26 | push_zip 部署 src/tests/docs/根文档；远端 py_compile 与六模块复跑 | 18 关键文件双端 SHA-256 一致；py_compile 退出0；20:21:10 重载、20:21:15 重连 |