# 会话 20 · 模型路由收口（主/识图 terra，其余 deepseek）

## 目标与范围

- 按用户要求核对机机（及所有人格共用）的模型选择：主回复与识图走 `gpt-6.1-terra`，判门/总结/NSFW/后台杂活统一走 `deepseek-v4.1-flash`。
- 只改模型路由与相关文档，不改人格正文、prompt 文案、聊天数据，也未部署。

## 现状核对

- 主回复 `ai` 与识图 `vision` 已是 `https://api.openai.com/v1/responses` + `gpt-6.1-terra`；`vision_shares_main_route` 为真，当前图、引用图和上下文图直接进主请求。
- `ai_fallback`、`audit_ai`、`filter.nsfw_spark_*`、`evolution.judge_model` 与 `catty_codex_instant_model`（空 → 回落到 spark）都指向 opencode zen go 的 `deepseek-v4.1-flash`。判门 `_model_should_reply_to_group`、群过滤器 `_filter_completion`、`token_billing.ai_gate_reply`、Qzone 判赞都走 filter/audit 线。
- 后台总结 `chat_completion_summary` 走 `ai_fallback`，会话 AI 压缩复用同一路径；NSFW 主生成经 `chat_completion_codex_instant(model_override=nsfw_spark_model)` 走 deepseek。
- 仍走主模型的非聊天任务：人格语气进化 `persona_evolution_runtime`、人格定义生成 `persona_admin`、技能提炼 `skill_runtime._default_completion`，以及 `self_repair` 缺省继承主 AI。

## 改动

- `openai_client.py` 新增 `AuxRoute` / `aux_route()` / `chat_completion_aux()`：次要 AI 线路按 `audit_ai → ai_fallback → 主 ai` 逐级取三件套，超时、温度、max_tokens、extra 跟随该线路；不挂工具。
- `persona_admin.py`、`skill_runtime.py` 改走 `chat_completion_aux`（`persona_definition` / `skill_learning` 路由标签），蒸馏抑制与 finally 恢复行为不变。
- `persona_evolution_runtime.evolve_once` 改走 `aux_route()`；预算预留从“整条上下文窗口”改为 `min(线路上下文, 32K) + 4K`，避免每天白吃千万 token 的预留。
- `config.py` / `catty_config_loader.py` / `config.example.json` 新增 `audit_ai.context_tokens`（默认 `128000`，env `CATTY_AUDIT_AI_CONTEXT_TOKENS`），只给人格进化的预算上限使用。
- `config.json`：`self_repair` 由占位 `HOST/TOKEN/TARGET` 改为显式 deepseek 三件套，继承主 AI 的状态关闭。

## 验证证据

- `config.json`、`config.example.json` 解析通过；`openai_client.py`、`persona_evolution_runtime.py`、`persona_admin.py`、`skill_runtime.py`、`config.py`、`catty_config_loader.py` 的 `py_compile` 通过。
- 用真实 `config.json` 经 loader 注入环境后构造 `Config`：主模型 `gpt-6.1-terra`、识图同线路且 `vision_shares_main_route=True`；`ai_fallback` / `audit_ai` / `nsfw_spark` / `codex_instant` / `aux_route` 均为 `deepseek-v4.1-flash`（aux 上下文 128000）；`catty_self_repair.config.load_config` 得到 `configured=True`、`deepseek-v4.1-flash`。
- 定向回归 `test_config`、`test_config_loader`、`test_persona_admin`、`test_persona_evolution_budget/runtime/store/http/plugin`、`test_skill_runtime/store/disclosure/tools`、`test_self_repair_config/agent` 共 243 项通过；`test_openai_client`、`test_no_local_model_runtime_config`、其余 `test_self_repair_*` 共 212 项通过（9 跳过）。
- 测试替身按新入口更新：`test_persona_evolution_runtime` 注入 `aux_route`，`test_skill_runtime` / `test_persona_admin` 注入 `chat_completion_aux`，断言同步为新的 kwargs。

## 部署状态

- 19 文件（5 源码 + `catty_config_loader.py` + `config.example.json` + 3 测试 + 7 文档 + `AGENTS.md`/`STATUS.md`）经 `push_zip` 推至 `D:/CattyQQAI`，0 跳过，逐个 SHA-256 与本地一致；未推送生产 `config.json`。
- 远端 Python 3.14.5 上六个源码/模板 `py_compile` 通过；`test_config`、`test_config_loader`、`test_persona_admin`、`test_persona_evolution_runtime`、`test_skill_runtime` 共 104 项通过。
- 守护自动重启：00:24:42 初始化、00:24:47 插件加载、00:24:51 OneBot 重连。真实模型与 QQ 效果未专项验证；归档提交见仓库。
