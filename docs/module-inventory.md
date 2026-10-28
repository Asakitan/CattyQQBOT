# 模块映射与状态

| 源码 | 职责 / 交付目标 | Status |
| --- | --- | --- |
| `voice_reply.py` / `voice_worker.py` / `tools.py` / `__init__.py` / `config.py` / `data/voice_bank/` | 调情短语音：人格→音色映射、双语音库、可选 GPT-SoVITS 子进程、冷却与主人例外 → `docs/voice-candidates.md`、`docs/architecture.md` | 已部署（含远端 worker 与两套模型）；远端 16+22 项通过，真实 QQ 播放未测 |
| `tmp/voice-training-20261028-01/` / `tmp/voice-quality-20261028-01/{phrase_ab,whole_refine,acoustic_ab}.py` | 双音色权重与等响度试听 → `models/voice/`、`docs/voice-candidates.md` | 用户首选文件02的4轮候选，selection.json已记录；端到端多句待验，未替换或部署 |
| `tools.py` / `__init__.py` / `catty_sim_chat.py` | 当前会话消息、水位、短等待与自然收尾 → `docs/tool-contracts.md`、`docs/architecture.md` | 已部署并合入；远端哈希一致 |
| `insight_memory.py` / `purpose_ledger.py` / `fdj_proactive_runtime.py` / `data_backup.py` | 会话/人格隔离的活跃记忆、完整档及目标证据回执 → `docs/persona-runtime.md`、`docs/fadianji-proactive.md` | 已部署并合入；远端哈希一致 |
| `persona_evolution_budget.py` / `persona_evolution_store.py` / `persona_evolution_runtime.py` / `__init__.py` | 当前人格语气扩展、写回校验及共享日预算 → `docs/persona-runtime.md`、`docs/architecture.md` | 已部署并合入；本地新增 49 项、远端新增 49 项通过；真实模型未测 |
| `personas/` / `persona_store.py` / `__init__.py` / `fadianji_harness.py` / `fdj_proactive_runtime.py` / `fadianji_style_critic.py` | 共享群聊判门、证据、风格及主动调度；人格能力和 NSFW 状态隔离 → `docs/persona-runtime.md`、`docs/architecture.md` | 已部署；本地 1367 项、远端 299 项通过（另 1 项既有短路径断言失败） |
| `src/catty_qq_ai/code_sandbox.py` | Python 工作区、产物快照、运行日志 → `docs/code-sandbox.md` | 已部署；Python 3.14 路径检查已上线 |
| `src/catty_qq_ai/tools.py` / `file_formats.py` | 工具 schema、真实文件格式、图片与群历史引用 → `docs/tool-contracts.md` | 技能接线已部署 |
| `src/catty_qq_ai/openai_client.py` | 工具回执、前缀、逐轮schema与图片下载 → `docs/tool-contracts.md` | 已部署；图片 URL 安全校验已上线 |
| `src/catty_qq_ai/__init__.py` / `reply_delivery.py` | 排队、回复恢复、宿主适配与确认后记账 → `docs/architecture.md` | 技能provider已部署；机机新适配已部署 |
| `message_utils.py` / `persona_prompts.py` / `prompt_manager.py` / `__init__.py` | 表情线索提取及图片、表情按语境自然接话 → `docs/persona-runtime.md`、`docs/architecture.md` | 已部署；本地 1401 项、9 跳过，远端 140 项通过 |
| `__init__.py` / `tests/test_legs_delivery.py` | 腿图文字及图片按发送回执记历史 → `docs/architecture.md` | 已部署并合入活动源码；远端 8 项、本地相关回归 96 项通过 |
| `dashboard.py` / `__init__.py` | 管理及开发 HTTP 接口访问 → `ADMIN_COMMANDS.md` | 已部署；回环限制与可选令牌鉴权 |
| `src/catty_qq_ai/prompt_manager.py` | 学术长度豁免与有效提示词注册 → `docs/prompt-testing.md` | 已部署 |
| `catty_rag.py` / `prompt_manager.py` / `__init__.py` | RAG 查询线程化、限时和在途上限，证据编排限时 → `docs/architecture.md` | 已部署；本地 1379 项、远端 221 项通过 |
| `rag_embedding_backend.py` / `rag_embedding_worker.py` / `catty_rag.py` / `config.py` / loader / `__init__.py` | 独立离线 BGE、生命周期、来源错误、回填计数与发送确认 → `docs/architecture.md` | 已部署、本地已合并 RAG 基线；1393 项、远端 47 项通过，模型就绪 |
| `config.py` / `catty_config_loader.py` / 配置 JSON / 训练面板 | 删除失效系统提示字段；技能配置及env映射 → `docs/skill-runtime.md` | 7项技能默认配置已部署；生产群额度单字段已更新 |
| `tests/test_prompt_size_ablation.py` | 显式开启的在线评测，导入不执行 | 导入隔离已验收，未在线运行 |
| `scripts/run_offline_regressions.py` | 固定清单，每模块独立进程 | 已部署；本地1214项、0失败/错误、9跳过 |
| `src/catty_qq_ai/fadianji_harness.py` / `harness_prompt.py` | v2 证据编排、引用检索与工具感知教程 → `docs/harness-evidence.md` | 已部署；成员作用域与身份校验已上线 |
| `reply_admission.py` / `__init__.py` | 同群历史与当前发言者驱动弱唤醒/续聊模型判门 | 已部署；16项纯判门和集成回归通过 |
| `fdj_scene_retrieval.py` | 昵称归一化精确索引与相关性过滤 | 已部署；语料配置与缓存键已上线 |
| `tools.py` / `__init__.py` | 图片发送者、历史来源与同模型原图透传 | 已部署；未验证真实模型识图 |
| `openai_client.py` / `config.py` / `token_billing.py` | 默认 max 思考与每用户跨群每小时额度 | 已部署；生产额度已更新为3000万 |
| `src/catty_qq_ai/fdj_corpus_store.py` / `catty_config_loader.py` | 语料作用域及 FDJ 配置别名 → `docs/harness-config.md` | 已部署 |
| `scripts/fadianji_harness_cli.py` / `benchmark_harness.py` | 本地预览、显式生产预算、合成基准 → `docs/prompt-testing.md` | 已部署 |
| `persona_store.py` / `persona_override_store.py` | JSON 定义、指令映射、版本与原子发布 → `docs/persona-json.md` | 已部署；覆盖表跨进程锁已上线 |
| `personas/__init__.py` / `persona_defaults.py` / `persona_runtime.py` | 动态注册、中性默认、逐轮快照与会话切换 → `docs/persona-runtime.md` | 已部署 |
| `catty_core_persona.py` / `catty_persona_reminder.py` / `persona_prompts.py` / `__init__.py` / `tools.py` | 猫猫人格、当前用户称呼及戳戳短回复 → `docs/persona-runtime.md`、`docs/architecture.md` | 已部署，双端一致；机机戳戳仍禁用 |
| `persona_admin.py` / `ADMIN_COMMANDS.md` | 导入、生成、自定义指令与手册 → `docs/persona-generation.md` | 已部署超时舍入修复 |
| `tools.py` / `request_intent.py` / `catty_sim_chat.py` | 上下文意图、可选文件能力、默认研究与技能提示，auto工具选择 → `docs/tool-contracts.md`、`docs/skill-guidance.md` | 研究增量已部署 |
| `tests/test_reply_queue_retention.py` / `test_request_intent.py` / `test_openai_client.py` / `test_reply_deadline.py` / `test_persona_runtime.py` | 排队准入、工具提示、预算与恢复合同 → `docs/session-log/session-04.md` | 已部署，双端一致 |
| `tools.py` / `__init__.py` / `features.py` / `config.py` / `catty_config_loader.py` / `config.example.json` | 移除联网搜索冷却、配置项与死辅助函数 → `docs/session-log/session-05.md` | 已部署，双端一致 |
| `src/catty_qq_ai/skill_store.py` | 全局JSON技能、候选检索、SHA校验、原子保存 → `docs/skill-storage.md` | 已部署，本地/远端28项通过 |
| `src/catty_qq_ai/skill_runtime.py` | 当轮资格、动态schema、公共来源账本、隔离提炼 → `docs/skill-runtime.md` | 已部署，本地/远端26项通过 |
| `tests/test_skill_*.py` / `test_request_intent.py` | 存储/运行时/工具/真实循环披露与研究提示 → `docs/session-log/session-06.md` | 技能75项及提示33项本地/远端通过，模型效果未专项验证 |
| `message_utils.py` / `personas/fadianji.py` / 入口触发helper | 机机关键词/@/引用/续聊，裸前缀保留，群聊prompt同步 → `docs/fadianji-proactive.md` | 已部署，双端一致 |
| `fdj_proactive.py` | 每群随机计划、活动许可、原子认领与重启恢复 → `docs/fadianji-proactive-scheduler.md` | 已部署，40项通过 |
| `fdj_proactive_runtime.py` / `proactive_legacy.py` | 宿主注入、忙碌/新鲜度/限时/回执；旧循环提取 → `docs/architecture.md` | 已部署，44项集成通过 |
| `fdj_proactive_prompt.py` / `config.py` / loader / example | 机机短聊提示与12项独立配置 → `docs/fadianji-proactive.md` | 已部署，20项通过 |
| `tests/test_fdj_proactive*.py` / `test_hot_reload.py` | 状态、配置、真实runtime往返、触发与锁生命周期 → `docs/session-log/session-08.md` | 新104项全通过；热重载23项通过 |
| `memory.py` / 入口摘要循环 | 摘要快照、并发新增与回执 → `docs/architecture.md` | 已部署；32项记忆测试通过 |
| `personas/fadianji.py` / `fadianji_scenes.py` | 普通聊天语境与真实样本口吻 → `docs/PERFORMANCE_AND_HARNESS.md` | 已部署；本地人格与语料测试通过 |
| `cpu_engine/semantic_route.py` / `corpus_txtai.py` | 模型与有序语料缓存身份 → `docs/PERFORMANCE_AND_HARNESS.md` | 已部署；29项缓存测试通过，CPU 默认停用 |
| `src/catty_qq_ai/__init__.py` / `tests/test_shared_persona_integration.py` | 怀孕预判 global 导出与临轮清零、分库 flush 托管、主路径场景/CPU phase 按人格 scope → `docs/session-log/session-10.md` | 已部署（基线+4 修复重放）；新增 4 例，远端 150 项通过 |

## 更新记录

| 日期 | 变更 | 验证 |
| --- | --- | --- |
| 2026-10-29 | 语音过程播报出站过滤（只剩播报则整轮无文字）；回执改中性措辞；表白试探由 schema 引导模型用语音回答 | 本地标记 22/语音 17/工具 22 通过；远端同三组通过（22/17/22），5 文件哈希一致，23:42:04 重启加载 |
| 2026-10-29 | 语音工具改为所有人格暴露、默认米雪儿、机机覆盖为 fadianji；补齐两套语音库、远端权重/配置/独立 venv 与 worker 实测；G2PW 分片合并上线 | 本地 16 项语音、22 项工具通过；远端 16+22 项通过；16 应用/模型 + 5 框架文件哈希一致；G2PW 启用后合成 3.27–3.40s/3.96–4.72s，峰值 3961MiB，23:06:33 加载、23:06:38 重连 |
| 2026-10-29 | 调情短语音：`catty_voice_say` + 9 句离线语音库 + 可选 worker 子进程；5 分钟冷却、主人免冷却、每轮 1 条 | 本地 15 项语音/22 项工具/26 项人格/33 项配置通过；双端 19 文件哈希一致、远端 py_compile 与 15+22 项通过；21:23:51/21:24:05 加载、21:24:13 重连 |
| 2026-10-29 | 澄清“第二个”为文件02的较早4轮版，记录首选候选，不是底模03 | selection.json绑定样本及检查点哈希；仅单句重放听评，端到端多句待验 |
| 2026-10-29 | 记录01前半段更合意/后半仍电音，淘汰其余调参版，固定语义比较8轮/4轮/底模声学权重 | 三组输入张量哈希一致、3.62秒、等RMS及零削波；捕获PCM与01完全一致，权重/框架/共享配置不变 |
| 2026-10-28 | 以用户偏好的整句02为基线，增加解码噪声、温度、种子及组合对照 | 五组等 RMS、零削波；两组解码配对输入哈希一致；旧基线52个PCM点相差1LSB，失败及有界续验记录保留 |
| 2026-10-28 | 双音色训练及发电机后半句电音对照，原设置、不分句、种子 7、温度 0.7 | 四组 CPU 生成、检查点/音频哈希与等 RMS 验证通过；原设置复现上一版，音质待听评 |
| 2026-10-28 | 借鉴 SurfCore：会话工具、分层记忆与目的证据闭环 | 21 文件已部署并合入；远端哈希一致、132 项通过，本地合并后 522 项通过 |
| 2026-10-28 | 当前角色语气进化及独立每日 token 预算，已部署并合入 | 13 文件与交付包哈希一致；本地 49 项及 297 项关联回归、远端 49 项及 229 项关联回归通过（关联回归各 9 跳过） |
| 2026-10-28 | 图片与表情自然接话；修复原图透传提示漏挂，保留原生表情线索 | 8 文件双端一致；本地 1401 项、9 跳过，远端 140 项通过 |
| 2026-10-28 | 补齐已部署的 RAG 后端、配置和测试及腿图发送记忆修复到活动源码 | 10 文件与已部署归档 SHA-256 一致；7 组 96 项通过；真实配置未改 |
| 2026-10-28 | 腿图发送记忆修复按最新生产基线重推，保留并发怀孕修复；活动源码未并入 | 双端两文件哈希一致；本地 132 项、远端 8 项通过；23:53 OneBot 重连；后续状态未复核 |
| 2026-10-28 | 共享怀孕/NSFW 状态缺陷修复：`_PREGNANCY_PREDICT` global 导出+临轮清零、分库 30s/shutdown flush、主路径场景与 CPU phase 按人格 scope；部署含一次误推旧分支的 6 分钟回退与基线重放修正 | 双端 6 文件哈希一致；远端 py_compile 退出 0、150 项通过、23:20:53 加载/23:21:03 重连+BGE ready |
| 2026-10-28 | QQ 发送错误影响排查；腿图失败后误记历史修复，隔离候选未部署 | 远端历史 59 条错误；新测试 8 项及 7 组共 114 项回归通过；真实 QQ 未测 |
| 2026-10-28 | push_zip 部署 RAG 本地 BGE 与 QQ 发送状态修复，本地源码未合并 | 9 文件双端哈希一致；远端 47 项通过，22:38:55 连接、22:38:56 模型就绪（远端时间） |
| 2026-10-28 | RAG 本地 BGE 子进程与 QQ 发送失败本地状态修复；隔离候选未部署 | 1393 项、0 失败/错误、9 跳过；远端隔离进程 8 项及真实缓存模型/复制旧索引验证通过 |
| 2026-10-28 | RAG 查询线程化与事件循环保护，含证据编排限时；已合并并部署 | 本地 1379 项、0 失败/错误、9 跳过；远端 11 模块 221 项通过；5 文件双端哈希一致 |
| 2026-10-27 | 共享人格回复机制及人格级开关，保留内置角色数据；已合并并部署 | 1367 项、0 失败/错误、9 跳过；381 文件语法通过；32 文件双端哈希一致；远端 299 项仅 1 项既有短路径断言失败 |
| 2026-10-27 | 猫猫人格、称呼与戳戳升级；保留机机戳戳禁用 | 本地27模块487项，0失败/错误、9跳过；16文件双端一致；远端210项通过；无真实模型/QQ评测 |
| 2026-10-27 | 图片地址、管理接口、Python 3.14 路径及人格覆盖并发安全并入并部署 | 本地1214项、0失败/错误、9跳过；Python 3.14.3 沙箱及并发45项、远端安全/存储/面板79项通过；33文件双端SHA-256一致 |
| 2026-10-27 | Harness、提示词与管理员命令隔离审查并入部署 | 1185项、0失败/错误、9跳过；随本次安全并入上线 |
| 2026-10-27 | push_zip 部署隔离候选34文件并并入本地，生产与本地群小时额度由300万调整为3000万 | 34文件双端哈希一致；本地1154项通过；13:38:31插件加载、13:38:34重连、13:40:16配置热重载；远端1154项24失败、6错误、9跳过 |
| 2026-10-27 | 候选增加群聊判门、图片/发言者归属、引用检索、场景精确召回、max与小时额度 | 64模块1154项、0失败/错误、9跳过；374文件语法通过；未部署或实测模型 |
| 2026-10-27 | 隔离候选：摘要并发、证据归属、机机普通人格、截止时间与缓存身份 | 63模块1092项、0失败/错误、9跳过；372文件语法通过；原源码未替换，未部署 |
| 2026-10-27 | push_zip 部署机机唤醒与活跃冒泡，生产配置与运行数据保持 | 28文件双端SHA-256一致；03:28:38加载、03:28:41重连；远端Shell不可用，未跑远端回归 |
| 2026-10-27 | 机机唤醒与活跃时段冒泡；同步prompt并拆分旧编排；未部署 | 58模块989项仅1既有浮点失败；新104项与消息抽取26项通过；357文件语法/保持性通过 |
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