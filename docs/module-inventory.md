# 模块映射与状态

| 源码 | 职责 / 交付目标 | Status |
| --- | --- | --- |
| `src/catty_qq_ai/code_sandbox.py` | Python 工作区、产物快照、运行日志 → `docs/code-sandbox.md` | 已部署 |
| `src/catty_qq_ai/tools.py` / `file_formats.py` | 工具 schema、真实文件格式、图片与群历史引用 → `docs/tool-contracts.md` | 已部署 |
| `src/catty_qq_ai/openai_client.py` | 工具循环与超时恢复回执 → `docs/reply-recovery.md` | 已部署 |
| `src/catty_qq_ai/__init__.py` / `reply_delivery.py` | 回复重试、后台交付、发送成功后记账 | 已部署 |
| `src/catty_qq_ai/prompt_manager.py` | 学术长度豁免与有效提示词注册 → `docs/prompt-testing.md` | 已部署 |
| `config.py` / `catty_config_loader.py` / 配置 JSON / 训练面板 | 删除失效的系统提示词字段及消费者 | 已部署 |
| `tests/test_prompt_size_ablation.py` | 显式开启的在线评测，导入不执行 | 导入隔离已验收，未在线运行 |
| `scripts/run_offline_regressions.py` | 固定清单，每模块独立进程 | 782 项，0 失败/错误，9 跳过 |
| `src/catty_qq_ai/fadianji_harness.py` / `harness_prompt.py` | v2 证据编排与工具感知教程 → `docs/harness-evidence.md` | 已部署 |
| `src/catty_qq_ai/fdj_corpus_store.py` / `catty_config_loader.py` | 语料作用域及 FDJ 配置别名 → `docs/harness-config.md` | 已部署 |
| `scripts/fadianji_harness_cli.py` / `benchmark_harness.py` | 本地预览、显式生产预算、合成基准 → `docs/prompt-testing.md` | 已部署 |
| `persona_store.py` / `persona_override_store.py` | JSON 定义、指令映射、版本与原子发布 → `docs/persona-json.md` | 已部署 |
| `personas/__init__.py` / `persona_defaults.py` / `persona_runtime.py` | 动态注册、中性默认、逐轮快照与会话切换 → `docs/persona-runtime.md` | 已部署 |
| `persona_admin.py` / `ADMIN_COMMANDS.md` | 导入、生成、自定义指令与手册 → `docs/persona-generation.md` | 已部署 |
| `tools.py` / `request_intent.py` / `catty_sim_chat.py` | 主人角色工具与上下文意图，auto 工具选择 → `docs/tool-contracts.md` | 已部署 |

## 更新记录

| 日期 | 变更 | 验证 |
| --- | --- | --- |
| 2026-10-26 | 完成工具与提示词一致性修复；删除失效配置，保留人格与 NSFW 文案；未部署 | 465 项离线回归，326 文件语法通过 |
| 2026-10-26 | 完成 harness v2、来源诊断、作用域、教程与 CLI；保留生产预算和人格 | 622 项离线回归；合成优先级与成本基准通过 |
| 2026-10-26 | JSON 人格、聊天生成保存/切换、指令绑定、上下文自主决策；管理员手册与示例同步 | 782 项通过；343 文件语法与保持性检查通过 |
| 2026-10-26 | push_zip 部署到 `D:/CattyQQAI`；远端删除失效 `chat.system_prompt` 并重跑测试 | 2333 文件双端哈希一致；py_compile 退出0；12 模块 235 项通过；归档 `202971c` |