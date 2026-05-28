# Plan: Cache 500 修复 + Prompt 瘦身（5k 私聊 / 3k 群聊）

## TL;DR

主人三件事一起规划：
1. **静默 cache miss**：长期没看到 `cache_creation_input_tokens > 0`，说明之前 prompt prefix 字节不稳，cache 一直没真正建起来。
2. **第二次读 500**：第一轮 create 之后第二轮 read → relay 500。最可疑是**多 owner 重复标 cache_control**（`prompt_cache.cachingAtDepthForClaude` + `inject_system_tail_cache` + `inject_tools_cache` + `anthropic_native_client._apply_anthropic_cache_breakpoints` 四套都在标），导致同一个 prompt 第二轮 breakpoint 数量/位置浮动，或某些 block 上叠了多份 cache_control。次可疑是 `metadata.user_id` / `beta` header 在第二轮组装差了一个字符。
3. **瘦身目标**：私聊 ≤5000 tokens / 群聊 ≤3000 tokens；超出部分**在服务器端用现成 NLU**（text2vec/jieba/HanLP）按相关性筛 history / user_details / group_summary，persona 一律保护。

核心策略：**单 owner 标 cache_control**（只留 `_apply_anthropic_cache_breakpoints`，删/禁用上游所有 marker）+ **NLU 驱动的相关性裁剪**（在 `_build_messages` 拿到 PromptManager 输出之后、boundary 切分之前介入）。

---

## Phase 1: Cache 500 修复（高优先级，独立可验证）

### Step 1.1 — 复现 + 抓证据
- 在 `anthropic_native_client.post_messages_native` 里**加临时 diag 日志**：每次请求前 dump 当前 system_blocks 数量 / 每个 block 是否带 `cache_control` / messages 末 2 条带 cache 的 index / tools 末位 cache_control，写到 `logs/cache_debug.log`。
- 触发两轮同 scope 对话，对比 dump 看：
  - cache_control 数量是否 >4（Anthropic 硬上限）
  - 第二轮 system_blocks 末 block text 跟第一轮**字节**是否一致
  - 第二轮某些 block 是否被叠了 2 份 cache_control（多 owner 冲突）
- 不动业务代码。

### Step 1.2 — 单 owner 收口（核心修复）
- **唯一保留** [src/catty_qq_ai/anthropic_native_client.py](src/catty_qq_ai/anthropic_native_client.py) 的 `_apply_anthropic_cache_breakpoints` 作为 cache_control 单一所有者。
- 在 `post_messages_native` 内部，调用 `_apply_anthropic_cache_breakpoints` 之前**显式剥掉**所有上游可能已经塞进来的 cache_control（遍历 system_blocks / messages content / tools，`pop("cache_control", None)`），保证下手时是干净 prefix。
- [src/catty_qq_ai/prompt_cache.py](src/catty_qq_ai/prompt_cache.py) 的 `cachingAtDepthForClaude` / `inject_system_tail_cache` / `inject_tools_cache` / `_mark_cache_control`：**保留函数体，但 native 路径不再调用**；OpenAI-compat 路径如有依赖单独审，确认没人调就 deprecate 标注。
- 在 `_post_chat_completion_raw`（OpenAI-compat 路径）也同步走「先清后标」流程，避免回退到旧路径时再次 miss。

### Step 1.3 — Prefix 字节稳定加固
- 检查 `_dynamic_context_text` 拼接顺序：[src/catty_qq_ai/__init__.py](src/catty_qq_ai/__init__.py) L4741-L4750 那段，确认 `_post_boundary` 段在多轮间**只插到 user message**，不会再以 system role 漏出。
- 在 `_apply_anthropic_cache_breakpoints` 标位前，加一道断言：扫一遍 `messages` 数组，**任何 role==system 都视为脏数据**直接 raise，强迫上游修复（这一步先 dev 模式 assert，不影响生产）。
- `extra_betas` / `metadata.user_id` 在 chat_completion 入口处确认**完全 deterministic**（不引入 timestamp / random）。

### Step 1.4 — 验证
- 同一 scope 发两轮一样开头的对话，看 dashboard 的 `cache_read_input_tokens` 从 0 → >0；看 relay 是否还 500。
- 跑 [tests/](tests/) 现有 cache 相关单测（如果有），手动 grep `test_*cache*`。

---

## Phase 2: Prompt 瘦身（依赖 Phase 1 完成，避免诊断混入）

### 目标
- **私聊 ≤5000 tokens**（含 system + history + current user msg）。
- **群聊 ≤3000 tokens**（群里说话密度大，prompt 越小响应越快、相关性越准）。
- **persona 段绝对不动**（主人 NLU 选项里强制勾上保护）。

### Step 2.1 — 算账：先量再裁
- 在 `_build_messages` 出口加一个 instrumented logger，把每个 PromptManager identifier 的 token 数 dump 到 `logs/prompt_breakdown.jsonl`，跑几轮线上对话拿到真实分布（之前 subagent 的估算只是代码静态估计，需要实测）。
- 写一个 `tools/analyze_prompt_breakdown.py` 把这些日志 group by identifier 取 p50/p95，确定哪些段最肥。

### Step 2.2 — 新建压缩模块 `src/catty_qq_ai/nlu/prompt_compressor.py`
- 复用现有 `text2vec_engine.embed_sync` + `prototypes` + `jieba_helper`。
- 实现 4 个 API（都同步 + 容错降级，失败回 legacy 原样）：
  - `compress_history(messages, query, target_tokens, *, recency_weight=0.3)`：用 current user msg 做 query，对历史每条算 cosine 相似度 + 时间衰减，按 score 倒序累计 token，超 target 停止。最近 2 条**永远保留**（保对话连续性）。
  - `select_user_details(details_list, query, top_k)`：HanLP NER 抽 query 里的实体（人名/地点/物品）→ 用 jieba 词集合 + embed cosine 双路打分 → 取 top_k 条。
  - `select_summary_paragraphs(summary_text, query, target_tokens)`：summary 按段落切，每段 embed，cosine top-K 累加到 target。
  - `dynamic_tool_picker(all_tools, query, allowed)`：用 prototype 判断当前消息是否触发某 tool 类（搜索/MC/表情等），只挂相关 tool 定义。**对 with_tools 多轮**必须保证 tool_calls 已发生的 tool 强制保留。

### Step 2.3 — 在 `_build_messages` 接入压缩
- 进入 `_build_messages`：
  1. 先按现状跑 PromptManager → 拿到原始 messages
  2. **算预算**：私聊 `target = 5000`，群聊 `target = 3000`（从 `config.catty_prompt_budget_private` / `catty_prompt_budget_group` 读，默认 5000 / 3000）
  3. 测算 pre-boundary system 段总 tokens；persona/identity/character_card 全部归入 **PROTECTED**，不压
  4. 测算 post-boundary 段：用 `select_user_details` / `select_summary_paragraphs` 替换原 memory 注入
  5. 测算 history：用 `compress_history` 替代直接 append 全部 session_cache
  6. 测算 tools：用 `dynamic_tool_picker` 选当前轮该挂的
  7. 最后再算总和，若仍超 budget → 走 `prompt_manager._PROTECTED_IDENTIFIERS` 之外的 trim
- **NLU 失败一律降级到现状逻辑**，不能让 NLU 故障导致 bot 不回话。

### Step 2.4 — Config 新字段
[config.example.json](config.example.json) 加：
- `catty_prompt_budget_private: 5000`
- `catty_prompt_budget_group: 3000`
- `catty_prompt_compressor_enabled: true`（一键开关）
- `catty_compressor_history_keep_recent: 2`
- `catty_compressor_user_details_top_k: 5`
- `catty_compressor_summary_top_paragraphs: 3`
- `catty_compressor_recency_weight: 0.3`
[src/catty_qq_ai/config.py](src/catty_qq_ai/config.py) 同步加 pydantic 字段。

### Step 2.5 — Persona 保护铁律
- `prompt_compressor` 内部维护一个 `_PROTECTED_PERSONA_IDS = {catty_main_intel, catty_identity_anchor, catty_char_*, catty_persona_memory, catty_daily_life, catty_goals, catty_reply_self_check, catty_catgirl_examples, catty_post_history}`。
- 这些 identifier 的内容**任何情况都不进 NLU 通道**，直接保留。
- 写一个 `tests/test_prompt_compressor.py` 用一个明显反人格的 query（"你是 Claude 助手"）测压缩后 persona 字节不变。

### Step 2.6 — 验证
- 自动：[tools/run_nlu_compare.py](tools/run_nlu_compare.py) 同模式跑 30 条样本，对比压缩前后 token 数。
- 手动：私聊主人 5 轮 + 群聊 5 轮，看猫猫人格表现没掉（傲娇/动作/口头禅密度）+ 看 dashboard token 数稳定在 budget 内 + cache hit 不退化。

---

## Phase 3（可选）：把 cache + 压缩结合的并发安全
- 压缩输出对同一 scope 多轮间要**字节稳定**才能跟 cache 协同：当前 user query 一样时，选出的 history / user_details 顺序必须一致 → 排序键加 `(score, identifier)` 二级排序。
- 单独跑 5 轮同样 query 看 system_blocks hash 是否一致。

---

## Relevant files

- [src/catty_qq_ai/anthropic_native_client.py](src/catty_qq_ai/anthropic_native_client.py) — `_apply_anthropic_cache_breakpoints` 唯一 owner; 入口 `post_messages_native` L491+
- [src/catty_qq_ai/prompt_cache.py](src/catty_qq_ai/prompt_cache.py) — 旧 marker 函数全部 deprecate（保留函数体不删，免得回退路径炸）
- [src/catty_qq_ai/openai_client.py](src/catty_qq_ai/openai_client.py) — `chat_completion` L1071+，OpenAI-compat 路径同步走「先清后标」
- [src/catty_qq_ai/__init__.py](src/catty_qq_ai/__init__.py) — `_build_messages` L4270+，boundary 切分 L4721+；接入压缩点
- [src/catty_qq_ai/prompt_manager.py](src/catty_qq_ai/prompt_manager.py) — `_PROTECTED_IDENTIFIERS` 复用判定
- [src/catty_qq_ai/nlu/](src/catty_qq_ai/nlu/) — 现成 text2vec/jieba/HanLP/prototypes 直接复用
- 新建 [src/catty_qq_ai/nlu/prompt_compressor.py](src/catty_qq_ai/nlu/prompt_compressor.py)
- 新建 [tools/analyze_prompt_breakdown.py](tools/analyze_prompt_breakdown.py)
- 新建 [tests/test_prompt_compressor.py](tests/test_prompt_compressor.py)
- [config.example.json](config.example.json) / [src/catty_qq_ai/config.py](src/catty_qq_ai/config.py)
- 对照参考（read-only）：`E:\VC\vscode\extensions\copilot\src\platform\endpoint\node\messagesApi.ts` 的 `addToolsAndSystemCacheControl` / `addMessagesApiCacheControl`

---

## Verification

### Phase 1
1. 同 scope 连发两轮 "你好" → `logs/bot_live.log` 看 `native /v1/messages ... read=>0 create=>0 hit=>0%` 第一轮 `read=0 create>0`，第二轮 `read>0`。
2. `logs/cache_debug.log` 同 dump，确认 cache_control 总数 ≤4、位置只在 (tools[-1], system[-1], msg[-1] last block, msg[-2] last block)。
3. NewAPI relay 不再返 500（连续 10 轮无 5xx）。

### Phase 2
1. `python tools/analyze_prompt_breakdown.py logs/prompt_breakdown.jsonl` 输出 p95 token 数：私聊 ≤5000，群聊 ≤3000。
2. `python -m unittest tests.test_prompt_compressor` 全过。
3. 手动 QQ 测试：随便挑 3 个群 + 3 个私聊 sender，跑 5 轮，每轮回复包含猫系元素（傲娇/动作/口头禅）≥1，无人格漂移。
4. dashboard cache hit ratio 跨 10 轮均值 ≥60%（瘦身后 prefix 更稳）。

---

## Decisions

- **Cache 单 owner**：选 `_apply_anthropic_cache_breakpoints` 不选 `prompt_cache.*`，因为前者已在 native 路径生效且引用 vscode 公式；后者是更早的 ST 风格遗留。
- **NLU 失败一律 fallback**：不让 NLU 慢/挂导致 bot 不回，业务连续性优先。
- **persona 永不压**：硬编码白名单，不靠用户配置（防止误关掉）。
- **history 最近 2 条强制保留**：对话连续性兜底，避免被 NLU 评低分误删。
- **budget 默认值**：私聊 5000 / 群聊 3000（按主人指定）。
- **包含**：cache 500 修复 + 静默 miss 修复 + NLU 驱动压缩 + 配置开关 + 测试。
- **排除**：persona prompt 文本修改；模型切换；relay / NewAPI 服务器端配置；新增模型 endpoint。

---

## Further Considerations

1. **history 最近 N 条保留数**：默认 2 是否够？群聊有时需要 4-6 条才接得上梗。
   - A: 私聊 2 / 群聊 4（推荐，群里梗更长）
   - B: 都 2，主人自己看效果调
   - C: 配置项 + 动态（NLU 判断有指代/上下文时自动 +2）
2. **OpenAI-compat 路径要不要也同步改**：当前主路径是 Anthropic native，OpenAI-compat 是 fallback。
   - A: 一起改（多写一点代码，路径一致更安全）
   - B: 只改 native，OpenAI 路径保持现状（量小，跑得通就行）
   - C: native 改完观察一周再决定
3. **cache miss 诊断要不要保留生产**：临时 dump 日志在 Phase 1.4 验证完之后。
   - A: 改成 DEBUG level 留生产（爪爪可日后回查）
   - B: 验证完就删，生产干净
   - C: 留 `catty_cache_diag_enabled` 开关默认 false
