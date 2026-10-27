# 会话 10 · 共享怀孕/NSFW 状态缺陷修复

## 目标与范围

- 检查共享人格架构（2026-10-27 部署）下共享怀孕状态与 NSFW 状态隔离是否有错误，有则修复。
- 审查范围：`_pregnancy_store_for` 分库、`_pregnancy_active_for`、`_preg_father_label`、`_phase_scope_for` 全部 call site、`_PREGNANCY_PREDICT` 预判链路、`_managed_store_flush_loop`/shutdown flush 覆盖、spark/main 两条路径的 phase/pregnancy 读写、CPU 引擎 S3 phase 读取。

## 已完成

- `__init__.py` 修复四处：
  1. `_build_messages` 内 `_PREGNANCY_PREDICT = {...}` 缺 `global` 声明——赋值只落局部，handle_chat 读到的模块级 global 恒为 `{}`，生产预选小猫名字从未生效（落盘变随机名）。补 `global _PREGNANCY_PREDICT` 并在每次构建前清零，主路径轮不再吃到上一轮 spark（可能另一个人格）的 stale 预判。
  2. `_PREGNANCY_STORES` 分库（`pregnancy_<persona>.json`）之前无任何 flush 覆盖（managed loop 只收全局 `pregnancy_store`，shutdown 钩子同样只 flush 全局），脏数据只在内存，重启/热重载丢整段进度。已在 `_managed_store_flush_loop` 加 30s 托管落盘、`_flush_pregnancy_store_on_shutdown` 加分库 flush。
  3. 主路径场景 hint（`_gps_main2` / `_build_phase_params_main2`）用裸 `_arc_scope` 读 phase——非 catty 人格会读到笨猫命名空间的 phase/场景（机机因 `nsfw_freeform` 跳过未受影响，非 freeform 自定义人格受影响）；改用 `_phase_scope`。
  4. `_cpu_engine_rule` S3 强互动判定的 `_ce_get_phase_state(scope, user_id)` 同样用裸 scope；改用 `_phase_scope_for(scope, _persona_for_scope(scope))` 归一。
- 审查确认无问题的部分：spark 路径 phase/sticky/grant/占位/生图计数全部走 `_phase_scope`；`_sticky_key`/`_nsfw_scope_key` 含人格后缀；mood_overlay 写入带 `#nsfw=` 后缀；owner_forward 付费窗口按人格校验；`_build_nsfw_freeform_state_facts` 怀孕行三态/父亲归属正确；catty 在两处 scope 修复下行为字节不变。
- `tests/test_shared_persona_integration.py` 新增 `SharedPregnancyStateTests` 4 例（global 导出/清零、主路径场景 scope、CPU phase scope、flush 钩子覆盖 `_PREGNANCY_STORES`）。

## 验证证据

- `E:\Py\python.exe -m py_compile src/catty_qq_ai/__init__.py` 通过；`nonebot.init(); nonebot.load_plugin('catty_qq_ai')` → plugin-load-ok。
- 测试集群（UTF-8 控制台）：`test_shared_persona_integration`（含新 4 例）、`test_persona_runtime`、`test_fadianji_nsfw_split`、`test_nsfw_freeform_state`、`test_personas`、`test_fadianji_nsfw_split_bubbles` 共 74 项 OK；`test_owner_forward`、`test_fdj_proactive_integration`、`test_persona_memory_isolation` 共 76 项 OK。合计 150 项、0 失败。
- 功能探针：`_pregnancy_store_for(fadianji)` 返回 `PregnancyStore` 并入 `_PREGNANCY_STORES` 缓存；catty 恒等全局 `pregnancy_store`；`pregnancy_probe.json` 15 次高潮受孕 → `flush_sync()` → 新实例重载，`is_pregnant` 与 `father_addr='小明'` 保留。

## 部署状态与剩余事项

- 已 push_zip 部署 6 个文件并双端 SHA-256 一致：`src/catty_qq_ai/__init__.py`（最终 `94e07e3e…`）、`tests/test_shared_persona_integration.py`、`STATUS.md`、`docs/session-log/session-10.md`、`docs/session-log/README.md`、`docs/module-inventory.md`。
- **部署事故与修正**：首次推送误用本地活动 `__init__.py`（RAG 会话未并入本地的旧分支，qq-120 会话 state.json 曾标注 ACTIVE_CONCURRENT_CHANGE），把生产已部署的 RAG/发送修复临时回退约 6 分钟（`start_embedding_worker` 启动、`_stop_rag_embedding_on_shutdown`、复读仅在发送成功后记历史、腿图中性失败文案）；期间两次重启 BGE worker 仍就绪。随后以 `.vscode/handoff/rag-async-20261028/local-bge-deploy-payload` 的已部署基线为底重放 4 处修复（diff 31+/5- 仅含修复），重推恢复。远端最终验证：哈希一致、py_compile 退出 0、基线特性与 4 处修复全部在场、23:20:53 插件加载、23:21:03 OneBot 重连 + BGE worker ready、无 ERROR；远端 `test_shared_persona_integration` 18/18、另 8 模块 132 项通过。本地活动源码已同步为该合并版。
- 教训：**推送前必须把本地活动文件与最近一次部署基线做哈希/diff 对账**（`.vscode/handoff/*/state.json` 的 deployed_baseline_sha256），本地旧分支不可直接覆盖生产。
- 未做真实模型/QQ 验证。生产上机机怀孕进度部署后首刷会写入 `pregnancy_fadianji.json`。
