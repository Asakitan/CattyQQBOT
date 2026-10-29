# Catty Repository Memory

给 Codex / Copilot / Claude 这类代码助手用的项目导览。**进项目时先扫这一份**，不要每次让主人重新介绍。

---

## 0. 进项目第一件事：读项目记忆

在动手之前，**必须先读** [.vscode/claude-memory.md](.vscode/claude-memory.md)——里面是 Claude Code 长期累积的**主人偏好和铁律**（部署通道、回复风格、push_zip 顺序、称呼规则等）。不读直接动手大概率违反铁律被打。

- **Claude Code** 自己运行时这些记忆已自动加载到上下字，不需要再读 `.vscode/` 这份。
- **Codex / Copilot / 其他助手** 看不到 Claude 的 personal memory 目录，必须读 `.vscode/claude-memory.md`。

---

## Project Overview

这是 **笨猫 (Catty)** —— 一个 NoneBot2 QQ 聊天 AI 插件，主要字件结构：

- 入口：[bot.py](bot.py) —— 读 `config.json` 灌环境变量 → `nonebot.init()` → 加载 `catty_qq_ai` 插件 → 启动 NapCat / Ollama 等集成进程
- 协议端：NapCatQQ（OneBot v11 反向 WS，`ws://127.0.0.1:8080/onebot/v11/`）
- AI 接口：任何 OpenAI-compatible Chat Completions（OpenAI / OpenRouter / vLLM / Ollama OpenAI 端点等）
- 包结构：`src/catty_qq_ai/` 是 nonebot plugin 包；根目录的 `catty_config_loader.py` / `catty_integrations.py` 是 bot.py 直接 import 的辅助模块

## Key Modules（速查）

| 字件 | 干什么的 |
|------|----------|
| [src/catty_qq_ai/__init__.py](src/catty_qq_ai/__init__.py) | nonebot plugin 主入口；所有 message handler、热重载循环、主动冒泡、summary loop、本地 critic warmup 都在这里。**1.6w 行的大件，按区域定位** |
| [src/catty_qq_ai/config.py](src/catty_qq_ai/config.py) | `Config` pydantic 类，所有 `catty_*` 字段；从环境变量读取 |
| [src/catty_qq_ai/openai_client.py](src/catty_qq_ai/openai_client.py) | `chat_completion` 主路由、Ollama 原生协议、云端健康度跟踪、Responses API 转换。**注意：本地 fallback 已在 `_fallback_is_configured` 硬关闭，不再走本地兜底** |
| [src/catty_qq_ai/responses_ws.py](src/catty_qq_ai/responses_ws.py) | Responses API WebSocket 持久连接、`stream_id` lane、`previous_response_id` 续聊缓存、断线全量重放与 HTTP 回退 |
| [src/catty_qq_ai/memory.py](src/catty_qq_ai/memory.py) | `MemoryStore`：人物画像 / 群摘要 / 怒气值 / 内容温度 / 主动冒泡 / 特别关心。落盘走 debounce + atomic write |
| [src/catty_qq_ai/session_cache.py](src/catty_qq_ai/session_cache.py) | 每群/私聊一个对话窗口，LRU + 持久化 `sessions/*.json`，debounce flush + atomic write |
| [src/catty_qq_ai/persona_prompts.py](src/catty_qq_ai/persona_prompts.py) | 笨猫人格 prompt。**改动前务必读现有字本，跑 `python -m py_compile` 再保存** |
| [src/catty_qq_ai/emoji_store.py](src/catty_qq_ai/emoji_store.py) | 表情包检索（音义匹配） |
| [src/catty_qq_ai/legs_picker.py](src/catty_qq_ai/legs_picker.py) | 腿图素材选择 |
| [src/catty_qq_ai/mc_status.py](src/catty_qq_ai/mc_status.py) | Minecraft 服务器在线状态查询。**注意：fallback 通道已整体硬关闭，这里实际成了 dead code**，但 import 还在，删要谨慎 |
| [src/catty_qq_ai/web_search.py](src/catty_qq_ai/web_search.py) / [nsfw_search.py](src/catty_qq_ai/nsfw_search.py) | Google/Bing 搜索 / NSFW 搜索 |
| [src/catty_qq_ai/auto_web_fetch.py](src/catty_qq_ai/auto_web_fetch.py) | 知识库缺口自动补网：本地探不到的事实类问题自动 search + 抓正字，拼【联网补全】块进 prompt |
| [src/catty_qq_ai/quick_ack.py](src/catty_qq_ai/quick_ack.py) | 长任务（查/画/做题）先导短回复：意图判定 + prompt + 输出净化，替代固定“我去翻翻”旁白 |
| [src/catty_qq_ai/owner_forward.py](src/catty_qq_ai/owner_forward.py) | 主人转发模式 |
| [src/catty_qq_ai/reply_markers.py](src/catty_qq_ai/reply_markers.py) | `NO_REPLY_MARKER` 等控制标记常量 |
| [catty_config_loader.py](catty_config_loader.py) | `config.json` → 环境变量 + 启动集成进程 |
| [catty_integrations.py](catty_integrations.py) | NapCat / Ollama 启动与监管 |

## Local NLU 增强层 (jieba + text2vec + HanLP)

主人 2026-05-28 引入. 28 个 hard filter 从 regex/keyword 升级到 embedding/NER. **不是替换云 LLM**, 是把音义抽取做精, 让云 prompt 更准.

- 包: [src/catty_qq_ai/nlu/](src/catty_qq_ai/nlu/) — 3 个 lazy-load engine + prototype 向量管理
  - `jieba_helper.py` — 中字分词 + 笨猫黑话词典 (杂鱼/破防/yyds 等)
  - `text2vec_engine.py` — `shibing624/text2vec-base-chinese` (~100MB, 768 dim)
  - `hanlp_engine.py` — `FINE_ELECTRA_SMALL_ZH` (~80MB, NER+POS+TOK)
  - `prototypes.py` — 16 topic + 8 emotion + 6 trend 原型句, 源 hash + .npy 磁盘缓存
- 受影响 filter (都保留 `_legacy_*` 降级路径, NLU 失败永远 fallback):
  - `daily_life.detect_topics()` — embedding cosine ≥ 0.55 + keyword union
  - `affection_scorer.score_user_message()` — 8 emotion prototype 分类决定 delta (NSFW + fear → -2)
  - `user_details_store._extract_details()` — HanLP NER + jieba POS + rule layer
  - `catty_theory_of_mind.detect_trend()` — 5-msg centroid + 6 trend prototype + LRU 缓存
- 启用方式: `config.json` 设 `catty_use_jieba` / `catty_use_text2vec` / `catty_use_hanlp` = `true` (默认全 false)
- HF 模型镜像: 大陆默认 `catty_nlu_hf_endpoint = https://hf-mirror.com`, runtime 自动注入 HF_ENDPOINT
- 启动 warmup: `on_startup` 跑 `_nlu_warmup_task` 后台预加载 (text2vec ~5s + prototype build ~30s + hanlp ~10s)
- async 安全: 所有重型 call 走 `asyncio.to_thread` 或 `loop.create_task` fire-and-forget
- 验证: `python tools/run_nlu_compare.py --mode legacy` vs `--mode nlu --print-diff` 跑 30 条对比例

依赖装在 optional extras: `pip install -e .[nlu]` (jieba/numpy/torch/transformers/text2vec/hanlp).

## Common Commands

| 任务 | 命令 |
|------|------|
| 启动 bot | `python bot.py` |
| 跑测试 | `python -m unittest discover tests`（tests/ 内自带 sys.path 管理，stdlib unittest 直接发现即可；没装 pytest） |
| 单字件音法 check | `python -m py_compile src/catty_qq_ai/<file>.py` |
| 推到生产服务器 | 助手通过 `rpwsh-local` MCP 的 `push_zip` 推送(本地 dir/file → zip 流式上传 → 远端解压),推送成功后 `git commit` 归档 |
| 训练数据导出 | `python scripts/export_reply_gate_dataset.py` |
| 热重载状态 | bot 运行中自动监控 config.json / memory/*.json / emoji 资源，写入即生效，不必重启 |

## Hot Reload 行为

- `config.json` 变更 → `_reload_runtime_config_from_path` 重建 `Config`、`MemoryStore`、`EmojiStore`、`LegsPicker`
- `memory.json` 或 `memory_groups/`、`memory_users/` 变更 → `memory_store.refresh()`（refresh 前会先 flush dirty 防止丢数据）
- emoji 资源变更 → `emoji_store.refresh()`
- 切换 `MemoryStore` 实例前会自动 `flush_sync()` 旧实例，并给新实例补起 `background_flush_loop` task

## 部署 vs Git

**生产服务器和 git 仓库是两套独立通道，互不影响**：

- `git push` 只是把代码备份到 GitHub（origin），**不会触发任何部署**
- 推到生产由**助手通过 `rpwsh-local` MCP 的 `push_zip` 工具**完成：本地改完的 `src/` 或单个字件 → 打 zip → 流式上传 → 远端解压到对应路径。服务器有自动重启，推完不必额外做拉起。
- **铁律顺序：先 `push_zip` 推到远端 → 成功后再 `git commit` 归档**。`git commit` 是把"已经推到生产"的代码状态写进 git 历史，不是触发部署。
- 不要用 `git diff` / `git status` 推断"要推什么"——主人本地修改可能没及时 commit，git 视角的 "modified" 不等于"待部署清单"。
- 助手主动改完代码不要默认推送；除非用户明确说"推一下"/"部署"/"上服"/"上线"，否则只完成本地改动 + 测试，把待推清单告诉主人由他确认。

## Prompt Ownership

- 用户手动修改的人格、提示词、角色设定、NSFW/敏感处理字案属于高优先级项目意图；Codex 不要擅自改写成"安全边界版"或替换成另一套价值判断。
- 如果这些内容导致 Python/JSON 音法错误、导入失败、运行失败，只做最小必要修复，例如转义引号、修正字符串拼接、保持原字音义；不要趁修音法时重写设定。
- 如果内容和更高优先级系统/安全规则冲突，先说明冲突和可执行的最小替代方案，再修改；不要静默改写。
- 对 [src/catty_qq_ai/persona_prompts.py](src/catty_qq_ai/persona_prompts.py)、`config.json`、`config.example.json` 中的人格 prompt 改动尤其谨慎，修改前先读现有字本，修改后跑 `python -m py_compile src/catty_qq_ai/persona_prompts.py` 和相关测试。
- 当前项目额外偏好：猫猫默认回复要更可爱、更黏人、更像 QQ 现聊；普通闲聊尽量压到 1-2 句短句，只有解释题目、技术问题、方案和排错时才放开写长。

### 猫猫回复长度硬规则（≤50 字）

- **`chat.system_prompt` 里明字钉死**：「回复节奏 **默认 1 段 1 句、总字数 ≤50 字**（QQ 真人聊天节奏, 不是写小说）」。这条**比 Prompt Ownership 还硬**——所有云端主回复的最终输出都要遵守。
- 拆段允许的 4 个场景（同样写在 system_prompt 里）：① 内容真有 2+ 个独立意思；② 反差链需要分段（炸毛→暴露→妥协）；③ 技术/方案/排错需要分步；④ 想强化复读 +3 节奏。**默认不拆**。
- 超短问候/确认/单字回应（"在吗/早/6/嗯/哦/晚安"）→ **必然单段**。
- 短 NSFW 指令 → 主人级单段妥协，不展开。
- 主回复策略 prompt `build_reply_intelligence_prompt` 也复述了 ≤50 字：非学术/非技术问题总字数 ≤50 字、1 句优先；只有学术/技术/工程/排错可放开分点。
- **新助手写新 prompt / 改 prompt 时**：`config.json` 里 `chat.reply_max_chars = 1800` 是硬上限（防超长刷屏），但**默认 50 字**才是真人感来源——别把上限当默认值用。

---

## 增量事实速查（2026-06-02 体检 — 跟现实对账时更新这一段）

> 这段是**新助手进项目时的"现实对账"清单**，只反映**仓库里当前实际的状态**，不重复上面的"铁律"段。事实变了直接改本段，不动老段。

### 当前增量（2026-10-27）

- **自动语音（2026-10-29）**：每条消息按 `voice.auto_chance`（远端 0.1）抽一次；命中且本轮回复 ≤50 字、无代码块/公式时先发一句现成语音再发文字，长回复/技术排版不触发。
- **语音条模式（2026-10-29，已部署）**：多气泡回复可逐条转语音条。`voice.only_hours`（如 `23:00-01:00`，支持跨夜）窗口内一直用语音条；窗口外按 `auto_chance` 只念一条。`bubble_max_chars=40`/`bubble_max_per_turn=3`/`bubble_budget_seconds=45` 限单条长度、条数与合成预算。NSFW（spark）回合按 `nsfw_bubble_chance`（默认 0.5）抽取，最多 3 条；只念台词，括号/【】动作旁白剥掉，整条只剩旁白则发文字。语音气泡按已送达记进历史与续聊，失败自动回落文字。远端 `test_voice_reply` 28 项、`test_config*` 33 项通过，01:14:46 加载、01:14:51 重连。`voice_worker.py` 合成后补 0.25 秒静音 + 8ms 淡出（`fragment_interval=0` 时框架不补尾巴，线上 3.36 秒样本末尾被削断），本地 30 项语音测试通过。
- **整轮语音条（2026-10-29，已部署）**：本轮只要有语音（`catty_voice_say` 直发或后台排队 `deferred_voice_started`），所有短气泡都转语音，不再只念一条；同一句已念过就不再补文字，晚到的后台语音台词重复也跳过。表情场景最后一句已语音时表情单独发。远端 Shell 被禁用，远端测试/重载未复跑。
- **表白直发语音（2026-10-29，已部署）**：用户说“喜欢你/爱你/想你/亲亲/抱抱/贴贴”（排除“不喜欢/别爱”等否定）时本轮直接触发语音条，不抽概率、不看 `only_hours`；`voice.affection_trigger` 默认 true。本地 31 项语音 + 33 项配置测试与插件加载通过，远端未复跑。
- **语音冷却 10 秒（2026-10-29，已部署）**：`voice.cooldown_seconds=10`，冷却键 `group:<群号>` / `private:<QQ>`，同群所有人共享；主人跳过冷却与概率。语音条路径也走同一冷却（自动/表白/NSFW 在冷却内不重复发），但本轮工具已发过语音时剩余气泡继续念完。
- **语音情感分段（2026-10-29，参考音修复已部署）**：`catty_voice_say` 支持 `segments`（1-4 段，每段 `text`+`emotion`）；worker 不再用合成语音库拼参考音，只接受按音色配置、来源标注明确的单条原始情绪录音。生产两套音色均未配置情绪录音，分段合为完整句并使用原有参考音，情绪标签不保证有明显区别；权重与默认参考音未替换。远端语音 38、配置 33、工具 22 项通过，真实 QQ 听感待验证。
- **语音去重（2026-10-29，已部署）**：`_voice_text_key` 去掉标点空白后比较；`catty_voice_say` 调用时就把参数台词记进本轮已发声集合，语音条遇到念过的气泡标 `already_voiced`，不补文字也不再合成；后台语音台词重复同样跳过。
- **表情按意思接（2026-10-29，已部署）**：图像描述只作背景，prompt 禁止“这表情/这图/图中/看起来”式复述和念表情包配字，除非用户明确问图里是什么；直接按图的情绪接当前情景。
- **冷却首启修复（2026-10-29）**：`cooldown_remaining` 对从未发过语音的 scope 返回 0，修掉把 `last=0` 代入导致进程刚启动被误判冷却的问题。

- **调情短音音已部署（2026-10-29）**：新工具 `catty_voice_say` 由模型自行判断调情音境后偶尔发一条 2–4 秒音音，字字回复发完后以 `record` 单独发出。**所有人格都暴露**；音色按人格解析，默认米雪儿（`voice.default=michelle`），`voice.persona_voices` 覆盖（机机→`fadianji`）。两套离线音音库在 `src/catty_qq_ai/data/voice_bank/{michelle,fadianji}/`（各 9 句）。每会话 5 分钟冷却、默认 0.6 概率、每轮 1 条；主人跳过冷却与概率但仍只 1 条。`worker` 后端已远端跑通：独立 venv `D:/CattyQQAI/tmp/voice-runtime/.venv`（py3.14+torch/torchaudio 2.11 CPU），权重在 `D:/CattyQQAI/models/voice/`，config.json 的 `voice.profiles` 指过去；BERT/HuBERT 为 fp16；G2PW 已装（`g2pW.onnx` 与训练副本同哈希，缺模型时才回退 pypinyin），无 `jieba_fast` 时回退 `jieba`、参考音用 soundfile 读取（绕开 torchcodec）。远端 16+22 项测试通过、G2PW 启用后实测合成 3.27–3.40s/3.96–4.72s，worker 峰值 3961MiB，23:06:33 加载、23:06:38 重连。过程播报会从出站字本删掉（只剩播报则整轮不发字字，音音单独到达）；被问“你喜欢我吗/想不想我”这类表白试探时 schema 建议优先用音音回答、可只发音音，没有关键词硬分支。真实 QQ 音音播放与听感未验证。- **语音消息转文字已部署（2026-10-29）**：QQ 客户端转文字不经过 OneBot，改为 `voice_stt.py` 自己取 `record` 段（url / 本地 / `get_record`）→ PyAV 解码 → 独立 `voice_stt_worker.py`（faster-whisper 1.2.1 + ctranslate2 4.8.4 + av 19 + `faster-whisper-small`）转写，以 `【语音转写】` 并回消息与历史；纯语音消息不再被丢弃。SILK 直接跳过、失败只写“未能识别内容”，同音频缓存 10 分钟。远端 `config.json` 的 `voice_stt` 段指向语音 venv；远端转写 6/语音 17/标记 22/工具 22 项通过，真实片段 4.4s/3.4s 正确。**真实 QQ 语音编码与 `get_record` 行为未实测**。- **共享人格回复架构已部署**：机机的群聊判门/续聊、证据编排、风格校验和主动调度成为共享链路，由人格属性选择内容、来源与开关；猫猫/机机人格正字未改，机机音料/场景/心情/自学默认不外借。普通与付费 NSFW 窗口、阶段、占用、生图计数按人格区分（猫猫保留旧键，机机仍仅私聊），旧共享 sticky 键串状态风险已消除。行为变化：猫猫/自定义群聊改为唤醒+续聊，未唤醒关键词不再直达；主动调度共用机机每群每日计划，猫猫读 `proactive.enabled`、机机读 `fdj_proactive.enabled`，旧猫猫节奏字段不再参与。14源码+17测试+1脚本共32字件双端SHA-256一致，远端Python3.14.5 py_compile退出0、插件加载成功、17模块299项仅1项既有8.3短路径断言失败；本地1367项、0失败/错误、9跳过。真实模型/QQ效果未测。
- **同一版本已部署并并入本地**：机机判门、图片/消息归属、引用和场景检索、普通人格、摘要等34字件已推至 `D:/CattyQQAI`，随后替换本地 `src/tests/scripts` 并逐项校验，本地受跟踪交付字件与归档 blob 一致；已部署副本在 `.vscode/handoff/catty-upgrade-20261027/deploy-payload`。生产与本地群小时配额均按单字段300万→3000万（每用户跨群共用），默认思考为 `max`；13:38:31加载、13:38:34重连、13:40:16配置热重载。本地1154项无失败/错误、9跳过；远端1154项24失败、6错误、9跳过，含缺少字档依赖和短路径差异，使用生产沙箱源码重测确认外部字件存在性检查异常，未改该沙箱。真实模型/QQ效果未测。
- **机机触发/冒泡已部署**：已按主人新要求取消真@硬门，统一关键词/@/引用/两条续聊；裸「机机」保留原字，不被空前缀丢弃。每活跃群每日5–10机会，活跃优先，默认UTC+8 08–22点、20%群日最多一个22–24点机会，00–08点不发/不补。`fdj_proactive`独立开关默认true，旧catty主动开关不变。28字件双端SHA-256一致，03:28:38插件加载、03:28:41重连。架构拆为scheduler/store、pure prompt、runtime和legacy循环适配，见session-08。
- **技能已部署**：在 `f96b5b3`（联网搜索去冷却）基础上，默认研究与技能增量已push_zip到 `D:/CattyQQAI`，32字件双端SHA-256一致。生产Python3.14.5的12模块260项无失败/错误、9跳过，01:54:00插件加载、01:54:03重连；config.json未改。准确信息以 `STATUS.md` 和 `docs/session-log/session-07.md` 为准。
- **默认研究提示**：`request_intent.py` 的默认上下字提示要求实质任务先查技能；知识库缺口、知识截止后或可能变化的事实查公共权威正字。生产/模拟均保留 `tool_choice=auto`，不按关键词强制调用。
- **技能按需披露**：`skill_store.py` 存 `data/skills/<id>.json`，`skill_runtime.SkillTurn` 管当轮候选与来源。首轮只有 `catty_skill_search`；候选命中后动态提供 `catty_skill_load` schema，模型选择后才给正字。真实公共抓页回执才开放 `catty_skill_save`，独立提炼不接收聊天、画像或用户身份。正则不保证识别全部私人信息，成功抓页不等于权威认证。
- **逐轮工具更新**：`tools_provider` 在生产、模拟与云端fallback传递，每次模型请求刷新实际schema和能力提示。动态load/save不能加入静态schema表。默认 `skills.enabled/auto_learn_enabled=true`，配置见 `docs/skill-runtime.md`；共享库不入Git，检索直接读取新版本。
- **已部署修复必须保留**：JSON人格管理、沙箱独立run/scope与产物快照、真字件格式、工具回执恢复、定向消息排队保留、历史前缀预算，以及源码无需运行即可交付的提示。不得恢复 `_web_search_cooldowns`、`features.search_cooldown_key` 或失效 `catty_system_prompt`。
- **历史字案定位**：下方2026-10-25配置记录及上方长度约定曾引用的 `chat.system_prompt` 已移除，默认人格内容由角色模块提供；本次研究提示没有修改这些人格/NSFW字本。不要重新添加该配置字段。
- **默认离线入口**：`E:/Py/python.exe -B -X utf8 scripts/run_offline_regressions.py`，不要用全目录discover触发历史在线评测。58模块989项：979通过、9跳过、1项既有persona_admin浮点deadline失败、0错误；新机机104项全通过，消息抽取另26项通过。357个Python字件音法通过，未做真实模型或QQ效果验证。
- **编辑回写事件**：回退版本已保存在 `.vscode/handoff/skills-20261027/`，源码按开工副本及已部署Git版本修复；来源未确定。后续修改先核对磁盘，别用过期缓冲区覆盖共享字件。

### 启动拓扑（不要再让新人 `python bot.py`）

- **真正常驻入口**是 [`start_catty.bat`](start_catty.bat)：它拉 [`scripts/catty_hot_reload.py`](scripts/catty_hot_reload.py) 当守护进程，守护进程再起 [`bot.py`](bot.py) 作子进程。
- 守护进程**只**监听 `WATCH_FILES` / `WATCH_DIRS`（源码/脚本/配置模板）——`config.json` 故意**不在**监听清单内（运行时配置走 bot 进程内 `_hot_reload_loop`，守护进程绝不为它 kill bot）。`memory_groups/`、`memory_users/`、`training/`、`data/`、`build/`、`backup/`、`models/`、`__pycache__/` 都在 `IGNORED_DIR_NAMES`，**不要建议清这些目录**。
- `CATTY_NO_HOT_RELOAD=1` 一键绕过守护、直接跑 `bot.py`（调试用）。
- **守护进程行为（2026-10-25 修复后）**：
  - 停 bot 走优雅退出：子进程用 `CREATE_NEW_PROCESS_GROUP` 启动，停止时发 `CTRL_BREAK_EVENT`，uvicorn 跑完 on_shutdown 落盘钩子再退；`hot_reload.stop_timeout_seconds`（默认 20s）内没退才强杀。旧版 `send_signal(SIGTERM)` 在 Windows 上等于硬杀，生产 664 次重启 0 次 shutdown。
  - bot 自己退出（崩溃/启动失败）会按 2→5→15→30→60s 退避重启，等待期间有代码推上来立即重启；`hot_reload.restart_on_crash=false` 可关。旧版这时守护进程直接退出，bot 就没人拉了。
  - 心跳看门狗：bot 每 15s 写 `logs/bot_heartbeat`（只在守护进程传入 `CATTY_HEARTBEAT_FILE` 时），超过 `hot_reload.heartbeat_timeout_seconds`（默认 180s，0 关闭）没更新就重启；没写过心跳的（启动中/旧版 bot）不判死。
  - 启动时关掉控制台 QuickEdit（RDP 点一下窗口会挂起所有往控制台写输出的进程：10/05、10/10 两次 bot 无日志卡死多天）；`bot.py` 的 stderr sink 也改成 `enqueue=True`。
  - 扫描用 scandir 并在遍历时剪掉忽略目录：生产一次快照 788ms → 7ms（守护进程原来常驻吃 ~1/3 个核）。
- 进程内：远端 RST 抛 `WinError 10054` 已被静默（`bot.py` 的 `_silence_proactor_connection_reset`）；loguru 三 sink + noise 分流是默认布局，**别再加一份 stderr 监听会刷屏**。
- **持久化（2026-10-25）**：各 store 写 `.tmp` 都先 fsync 再 replace（2026-07-03 断电把 383 个 memory 字件清成全 NUL）。`MemoryStore` 只重写内容变了的字件，并记录自己写过的字件签名，进程内 `_hot_reload_loop` 据此跳过「自己 flush 触发的整库重读」；解析失败的 memory 字件会被改名为 `*.corrupt-<时间>` 并 WARNING（里面的记忆没有加载，需要人工恢复）。2026-07-03 断电清零的那批字件（剩 381 个全 NUL）已于 2026-10-25 在生产上删除，无法恢复。
- **数据快照（2026-10-25）**：`data_backup.py` + `_data_backup_loop`，每 `memory.backup_interval_hours`（默认 24h）把数据目录（memory.json 所在目录）下的 `*.json`（排除 config 类字件，里面有明字密钥）和 `memory_users/`、`memory_groups/`、`memory_games/`、`sessions/` 打成 `backup/data-<时间>.zip`，保留 `memory.backup_keep`（默认 7）份；按最新快照时间判断，频繁重启不会多拍。`memory.backup_enabled=false` 关闭。恢复 = 停 bot → 解压覆盖 → 启动。
- **LLM 请求连接复用（2026-10-25）**：`openai_client._http_client` 按 event loop 复用长连接 `httpx.AsyncClient`，transport 层对「建连失败」重试 2 次；测试 patch `httpx.AsyncClient` 时自动退回每请求新建。
- stdlib `logging`（`catty_qq_ai.*` / `nonebot.*`）已经被 `_InterceptHandler` 桥到 loguru，**别再回到 stdlib 配 file handler**——会被吞。

### `config.json` 现状（最近一次大改：2026-10-25）

- **默认人格已切机机**：`persona.default = "fadianji"`（2026-08-10，主人要求）。机机人格行为铁律：
  - **群聊按显式唤醒触发（本地新规则）**——机机 `mention_only_trigger=False`，统一helper接受机机现有关键词、@及引用自己；无唤醒且无续聊的普通消息不进主回复。签到/积分共用门控，复读对唤醒和续聊让位；其它mention-only人格仍坚持真@。此前只真@规则已被2026-10-27用户要求取代，部署状态看顶部。
  - **唤醒后有「下面两条」续聊预算**——`followup_reply_budget=2` 保留；普通相关跟进消耗预算，无关NO_REPLY关窗，新的显式唤醒满血重置。关闭旧filter时，已存在的机机普通字本续聊仍能进入相同权限/广播守卫。主动冒泡本身不额外给全群开窗。
  - 私聊不受 mention-only 影响（群里才可能 @）。
  - **私聊 NSFW 是放行的**（2026-10-25）：`Persona.nsfw_scopes={"private"}` + `nsfw_gate_skeleton`（机机口吻 10-stage）+ `nsfw_gate_private_only=True`。好感 Lv5 以上（`max_stage≥5`）或付 100 积分援交就必须接住，Lv 越高越顺从；Lv≤4/群聊才允许炸毛短拒。旧行为（gate 段整段禁用 + §19「自己不想带入→短拒」）会直球拒绝，别回退。
  - **机机 × 笨猫 NSFW 状态分开**（2026-10-25）：`Persona.nsfw_lexicon` 把笨猫词整组换成机机（猫耳→翼耳/尾巴→尾羽/喵→''/主人→你…），作用于 phase catalog、起手范例、spark/援交模板；phase 状态键带 `#nsfw=<persona>`；怀孕按人格分库 `pregnancy_<persona>.json`，机机 `pregnancy_private_only=True` → 只记私聊、群里不读不说，私聊内所有 user 共用一份；`_preg_father_label` 给机机记真实父名，事实块显示「孩子爸爸=…」。
  - **机机 NSFW 自由发挥**（2026-10-25）：`Persona.nsfw_freeform=True` → spark 不注入笨猫的 8 铁律 / phase catalog / tracker / 起手范例，改成 `_NSFW_FREEFORM_*` 短提示；override 用「格式自由」；critic 用 `_NSFW_FREEFORM_SYSTEM_PROMPT`（不压短）。`_reply_chunks` 的 NSFW 多行拆泡路径已含 fadianji（上限 `max(max_chunks,8)`）。
- **主 AI 已部署 OpenAI Terra Responses WebSocket**（2026-10-29）：主/识图 `gpt-6.1-terra`，`store=false`、`previous_response_id` 增量续聊与 HTTP 回退；普通生图 `gpt-image-2`，NSFW spark 仍走 DeepSeek，自动 NSFW 配图通过独立配置保留 Pixelbunny。Windows 本地 SOCKS `127.0.0.1:11080` 经加密跳板 TCP 18080 转发 OpenAI/NovelAI，其他域名直连；旧 GOST 容器停用但保留。远端 228+4 项通过（9 跳过），真实两轮增量续聊、生产主请求及 NAI 鉴权通过，生产实际出图/QQ 未测。WARP 已断开，试装的 Windows 无线组件待重启完成。下面的 OpenCode Zen Go 段是历史记录。
- **代理下图片直连已部署**：`vision.image_download_direct=true` 只让图片资源直连，仍逐跳校验并固定公网地址，默认关闭。远端修复回归 127 项通过（9 跳过），QQ 公共头像下载及 Terra WebSocket 识图通过；真实 QQ 会话尚未验证。
- **主 AI 走 OpenCode Zen Go**：`ai.base_url = https://opencode.ai/zen/go/v1/chat/completions`、`model = deepseek-v4.1-flash`（2026-10-25 机机底层模型，此前是 `mimo-v2.5`）；`anthropic_native_enabled = false` + `compaction_enabled = false`。同一把 `oc_sk_` 明字 key 也用在 `ai_fallback` / `audit_ai` / `filter.nsfw_spark_api_key`，三处模型 2026-10-25 一并切到 `deepseek-v4.1-flash`（已无 `mimo-v2.5`）。opencode.ai 主机会自动补 `x-opencode-session` 头（`openai_client.py` ~3145），缺它网关直接 400 `MissingSessionID`。
- **图片透传（2026-10-25）**：`vision` 线与主 AI 同 `base_url` + 同 `model` 时（`openai_client.vision_shares_main_route`），当前消息与引用旧图的 `image_url` block 直接进主请求，跳过 `describe_images` 那次同模型 round-trip；`vision.model` 不同时仍走原识图链路（截至 2026-10-25 `vision` 已与主线同为 `deepseek-v4.1-flash`，透传生效）。
- **联系上下字识图（2026-10-26）**：`RecentConversationMessage` 现在记 `image_urls`/`image_keys`（`_remember_recent_conversation_event` 用 `extract_images(event)` 填，`incoming` 路径直接用 `incoming.image_urls/image_keys`）；`_recent_context_images_for_scope()` 取最近 `vision.context_messages`（默认 16 条）消息里的图，被 @/引用/直呼（`_image_attention`）时与当前图、引用图一起去重后挂主请求——`_build_user_content(context_image_parts=...)` 按「【前字图N】X 条消息前 谁 发的图，配字：…」+ `image_url` 交替排列。旋钮 `vision.context_messages=16` / `context_max_images=4` / `context_max_age_seconds=1800`；识图线不是主线路时退化成 `context_image_notes` 字本（缓存描述或 URL）。测试 `tests/test_vision_context_images.py` 9/9。
- **`local_critic` 已弃用**：`config.json` 里 `_note: "reply_gate 已弃用...这一节保留只为兼容旧 loader"`，`local_critic.enabled = false`。新助手看到 "warmup" / "reply_gate" / "local critic" 字样要意识到它在历史里、**别让 assistant 试图启用 warmup 链路**。
- **`auto_fetch` 段（2026-10-25 新加）**：知识库缺口自动补网。`enabled` 默认 true，`engines` 默认 `["duckduckgo","bing"]`（DDG 给直链、抓得到正字；Google News RSS 是 JS 壳，抓不到）。其它旋钮：`cooldown_seconds=120`、`max_pages=2`、`page_chars=1200`、`total_chars=2600`、`hourly_budget=20`、`timeout_seconds=12`、query 缓存 6h/负缓存 15min。**网页配图（2026-10-25）**：抓到正字的页面再抽 `og:image`/`<img>` 配图，Pillow 缩到 `image_max_dim=720` 转 JPEG data URL，以 `image_url` part 挂到主 AI 的 user 消息（`_build_messages(web_image_parts=...)`，只在 `vision_shares_main_route` 成立时挂）。旋钮：`image_enabled=true`、`image_max=2`、`image_max_dim=720`、`image_max_bytes=600000`。逻辑在 [`src/catty_qq_ai/auto_web_fetch.py`](src/catty_qq_ai/auto_web_fetch.py)，闸门是 `__init__._auto_fetch_kb_hit`（群音料/笔记/lorebook/游戏事实/人物细节全 miss 才算缺口）。
- **先导短回复（2026-10-25，替代固定工具旁白）**：查资料/画图/做题这类必然超 30s 的请求，开跑前让模型现写一句 4-24 字短反应先发出去，再继续工具循环；旧的固定「我去翻翻/我查一下/等等哦」池已删。旋钮：`chat.quick_ack_enabled` / `quick_ack_max_chars` / `quick_ack_timeout_seconds`。生成走 `chat_completion_codex_instant`（mimo 思考模型 → max_tokens 必须 512，否则 reasoning 吃光只回空串；空正字重试一次，仍不合格这次就不发）。实现：[`src/catty_qq_ai/quick_ack.py`](src/catty_qq_ai/quick_ack.py) + `__init__._schedule_quick_ack`，主回复就绪时 task 会被 cancel 防补一句。
- **云 API only（2026-08-15）**：`ollama.enabled = false`、`local_training.enabled = false`；`nlu.use_jieba = true`，但 `use_text2vec/use_hanlp/nlu_warmup_on_startup = false`。本地主模型、音义向量和 HanLP 推理都不在运行时启动；训练/Ollama 代码仅保留为停用兼容路径。
- **`imagegen` 段**（2026-05-28 新加；2026-10-25 切 Pixelbunny）：`catty_imagegen` 默认走 `backend="pixelbunny"`；普通绘图 `model="seedream-5-pro"`（V5，`resolution` 默认 1K），NSFW 场景/自画像走 `nsfw_model="seedream-4.5"`。自动 NSFW 生图 `catty_nsfw_imagegen` 也走 Pixelbunny `seedream-4.5`（参考图 `/v1/uploads` → `/v1/edits`）；legacy `gpt-image-2` / NovelAI v4.5 路径保留兼容。任何生成 error 由 `imagegen_public_failure` 吞掉技术细节、不出图、只回一句道歉（积分不足/冷却除外）。`config.json` 里**`imagegen.api_key` / `nsfw_search.pixiv_cookie` / `image_search.saucenao_api_key` 都是明字**——本地开发机约定，**不擅自脱敏，不擅自外发日志时打印**。
- **回复与生图解耦（2026-10-25）**：`handle_chat` 里 `_reply_done = asyncio.Event()` + `_lock_stack.callback(_reply_done.set)` 标记「本轮字字已发完」。`catty_imagegen` 走 `_imagegen_tool_with_deferred_delivery`：只在线等 `_IMAGEGEN_INLINE_WAIT_SECONDS`（4s），秒回结果（冷却/积分/参数）照常交给模型，还在画就回 `status="generating_in_background"`，交给 `_spawn_deferred_image_delivery` 后台 task 等 `_reply_done`（上限 180s）再 `bot.send`（ActionFailed 不重试 / NetworkError 隔 2s 重提一次）。NSFW 自动配图删掉「发 chunks 前等 30s」和「handler 里 `await _nsfw_img_task` 死等」两段。Pixelbunny 连接：connect 15→30s + `httpx.AsyncHTTPTransport(retries=3, proxy=...)`（建连失败才重试，不会重复提交任务），一次出图复用同一条长连接。
- **工具写字件 + QQ 回传（2026-10-26）**：新增 `catty_write_file`（`tools._exec_write_file`）——把模型给的字本写成 UTF-8 字件，落到 `file_delivery.dir`（默认 `data/file_outbox`），2026-10-26 起不再要求先被直接点名；字件名只取 basename、非法字符剥离、扩展名不限（字本内容），正字上限 `max_chars`、每轮最多 `max_per_turn` 个。结果里的 `_staged_file` 由 `__init__._tool_executor` 取出并塞进 `ToolContext.pending_file_deliveries`，`_spawn_deferred_file_delivery` 等 `_reply_done` 后按原 event 调 `upload_group_file` / `upload_private_file`（失败退回 `file` 消息段），所以哪个群/私聊问的就发回哪个窗口。写字件任务不触发系统先导/占位短回复（`quick_ack.detect_task_kind` / `is_write_task` 直接返回空，慢回复 placeholder 也跳过），由主模型本轮直接回复；字件在 `_file_outbox_cleanup_loop` 里按 `ttl_hours`（默认 24h）自动删除。旋钮在 `config.json` 的 `file_delivery` 段。
- **Python 沙箱（2026-10-26）**：新增 `code_sandbox.py` + `catty_python_run` / `catty_python_install`。子进程用 `sys.executable -I -B -X utf8`，cwd/HOME/TEMP 全在 `data/python_sandbox`，env 只保留 Windows/Python 必需项；生成的 launcher 装 audit hook + `os.stat/access/listdir/scandir` guard，读只允许沙箱和 Python 前缀（项目根即使被 editable install 加进 `sys.path` 也不算 runtime root），写只允许沙箱，禁止 socket/subprocess/ctypes/注册表/win32/剪贴板/截图等模块。pip 只装 wheel 到沙箱 `packages/`（`--target` + `--only-binary=:all:` + 白名单 + 超时）。代码生成的字件由 `_exec_python_run` 塞进 `pending_file_deliveries`，主回复后按原会话自动发送；`_file_outbox_cleanup_loop` 同时清理沙箱 work/home/tmp（默认 24h）。`CODE_SANDBOX_SYSTEM_PROMPT` 在 `_build_messages` 里并入 tools hint，明确要求不读本机数据/不联网/不用 `catty_run_code`。旧 `catty_run_code` 默认隐藏（`legacy_run_code_enabled=false`）。旋钮在 `config.json` 的 `code_sandbox` 段。
- **工具全量交给模型（2026-10-26）**：`tools_full_exposure=true` + `tools_lazy_schema_enabled=false` 本来就是每轮全量 schema；本次把剩余「先被点名 / 上下字必须有图」的能力闸门撤掉——`catty_imagegen` / `catty_write_file` / `catty_nai_director` 不再要求 `requires_direct_request`，`catty_image_search` / `catty_nai_director` 不再要求 `requires_image`，executor 内部的 `is_directly_requested` 硬 guard 一并删除（拿不到图时由 executor 自己返回可读 error）。新增 `catty_scan_messages`（检索当前 scope 最近 ~80 条消息：关键词/发送者/是否带图/分钟窗口，返回字字 + `m<seq>#<idx>` 图片引用）和 `catty_look_image`（按消息引用/直链/最近图下标调 `describe_images` 取字字描述）。`ToolContext` 新增 `recent_messages` 快照（`__init__._recent_messages_snapshot_for_scope`）和 `describe_images_fn` 回调；`catty_image_search` 无图时改为提示先 scan。`tools_system_hint` 第 1/5 条同步改成「全部工具默认开放 + scan→look→image_search 回指」。测试：新增 `tests/test_tool_message_scan.py` 8/8，`test_tool_*` 44/44、quick_ack 14、imagegen_force 5、expression_repeat 5、vision_context 9、composition 36、config 23 全绿 + plugin-load-ok。
- **NSFW 自由发挥（2026-10-25 扩展到 catty）**：`CATTY_PERSONA.nsfw_freeform=True`，跟机机同一套骨架。freeform 下不注入 phase catalog / 尾部 tracker / 起手范例 / 8 铁律 / cheatsheet boundary / trope 固定场景脚本 / 罐头 climax prefill；prefill 统一中性 `（`；保留 stage 权限矩阵 + 反 OOC 元词墙 + `<NSFW_STATE>` 标记 + `apply_persona_lexicon` + per-persona `nsfw_style_override`。本地 phase/scene 状态机照旧更新，只是不塞 hint。
- **回复墙钟 · 超时取消重发（2026-10-25）**：主回复改走 `__init__._main_reply_with_deadline`。`chat.reply_hard_deadline_seconds=60` 到点 `asyncio.wait_for` 取消整次请求（流式心跳会骗过 httpx read timeout，SSE 里另加 `_current_stream_deadline_var` ×2 内层兜底），然后用同一批 messages 重发 `chat.reply_deadline_attempts=2` 次，每次 `chat.reply_retry_deadline_seconds=40`、不带 tools、`reasoning_effort=low`（`openai_client.set_current_reasoning_effort`）。全挂才走原来的兜底字案。非 catty 人格的固定例句占位（`slow_reply_placeholder` catalog）已停用 —— 只发模型现写的那条。
- **思考强度默认 high（2026-10-25）**：`openai_client._DEFAULT_REASONING_EFFORT = "high"`，`_with_thinking_max_defaults` / `_with_deepseek_thinking_defaults` 的缺省值从 `max` 降到 `high`（max 在长上下字里会连着想几分钟才出正字）。仍然优先认 `config` 的 `extra_body.reasoning_effort`，其次按轮 contextvar（超时重发时是 `low`）。GLM 只开 `thinking.enabled`，ollama 原生路由不动。
- **event loop 卡顿看门狗（2026-10-25）**：`_start_loop_block_watchdog()` 在 on_startup 起一条心跳协程（0.25s）+ 一条守护线程；主线程心跳落后超过 `chat.loop_block_warn_seconds`（默认 3s）就把主线程栈打进 WARNING，直接指出谁在同步阻塞。`handle_chat` 另加 `turn stage:` 分阶段耗时日志（admission / auto_fetch / build_messages / main_completion）。
- **机机自学（2026-10-25）**：新模块 `fdj_self_study.py` — `collect_once()` 按 `self_study.sources`（group 本地音料 / bilibili / tieba，远程走 `web_search`+`auto_web_fetch._fetch_page_text` 同一套 SSRF 校验）抓素材，`chat_completion_codex_instant` 蒸馏成 ≤80 字第一人称笔记，落 `data/fdj_self_study/notes.jsonl`。硬约束：`daily_token_budget=100000000`（抓取+蒸馏都记账，`state.json` 按日累计）、`corpus_max_tokens=100000`（超了按得分/时间淘汰）、`runs_per_day=6` / `interval_minutes=120` / `session_minutes=8` / `active_hours=09:00-23:30`。回复时 `build_self_study_context` 按 bigram+关键词挑 ≤`context_tokens` 的笔记，以 `catty_self_study` order=602 注入。`_fdj_self_study_loop` 只在 `enabled=true` 时启动（`config.json` 已开、`config.example.json` 默认关）。
- **分泡由模型决定（2026-10-25）**：`_reply_chunks` 只认模型写的 `<<<CATTY_REPLY_SPLIT>>>`——写了就按它拆多条，没写就整段一条（消息内换行原样保留）。删掉的本地启发式：` / ` 斜杠分泡、普通空格口音边界、裸换行分泡、NSFW 多行分泡、`_collapse_fadianji_fragment_wall`；只留 `catty_reply_max_chars` 长度兜底（`split_reply`）。prompt 侧（`_semantic_reply_split_prompt` 两分支 / `FADIANJI_CORE` §4 / `FADIANJI_CHAT_RHYTHM` / `build_reply_self_check_prompt` / `_NSFW_FREEFORM_REMINDER`）统一改成「要连发就用 marker 分隔，不写就是一条」。
- **拆条规则每轮贴脸（2026-10-25）**：远端 `[message_sent]` 实证 15 轮里 8 轮 marker 生效（拆 2-4 条）、7 轮单条塞 5-9 个裸换行 → 拆泡代码正常，是模型漏写标记 + 块内换行。新增 `_reply_split_reminder_prompt()`，以 `catty_reply_split_reminder` order=810 每轮进 post-boundary 动态段（和 tools 一样每轮都在场）：一条气泡=一行、连发就每条之间单独一行 marker、裸换行不拆泡。prompt 同步：`_semantic_reply_split_prompt` 两分支、`_NSFW_FREEFORM_REMINDER`、`_NSFW_FREEFORM_TAIL_POINTER`（spark 路径的每轮贴脸段）、`_build_nsfw_spark_override` freeform `fmt_line`（原「想拆气泡就换行」）、`FADIANJI_CORE` §4 / `FADIANJI_CHAT_RHYTHM`、`persona_prompts`（人格记忆/self-check/碎句节奏）、`fadianji_style_critic` 改写指令；`token_billing` 配额提示和 `tools` 生图配字的「可换行分条」改「一条一行，不换行」。历史里也用 marker 连接（`history_prompt_text = REPLY_SPLIT_MARKER.join(chunks)`），模型下一轮能看到自己写过的标记。测试：新增 `tests/test_reply_split_reminder.py` 6/6，回归 15/22/9/4/41 全绿。
- **短句逐行分条（2026-10-25，已部署）**：20:34 私聊 `raw_message` 仍是两行且没有 marker。`_reply_chunks` 现在 marker 优先；没 marker 时 `_newline_bubble_chunks()` 把「2 行以上、每行 ≤80 字、无空行段落、无 ```/$$/\[ 技术排版、无编号/符号列表」的短行按行拆成气泡；长句/写字/技术列表仍整段。`_cap_reply_chunks` 超上限的尾部合并改用空格，不再制造多行气泡。prompt 同步：810 提醒、`_semantic_reply_split_prompt` 两分支、`_NSFW_FREEFORM_REMINDER`、`FADIANJI_CORE` §4、`FADIANJI_CHAT_RHYTHM`、`persona_prompts`、`catty_core_persona` §3。测试 6/10/22/22/6 全绿。
- **Prompt 一致性审计（2026-10-25，未部署）**：critic `rewrite_if_needed(..., freeform=...)` 调用点补传参（`_NSFW_FREEFORM_SYSTEM_PROMPT` 之前永远选不到）；`_NSFW_STATE_FLAG_INSTRUCTION` 不再引用 freeform 不注入的 phase tracker；810 提醒 / `_semantic_reply_split_prompt` 两分支 / `persona_prompts` 统一成「普通聊天一行；技术分步/写字可多行，但换行不产生新气泡」；`catty_imagegen` schema 补 `generating_in_background` 分支；`tools_system_hint` 把「所有图」限定成「原创绘图 + 本轮开放时」并补「空结果 ≠ 事实不存在」；EMOJI_QUERY 改成写在最后一行末尾（不独占一行）；`_PROTECTED_IDENTIFIERS` 加 `catty_reply_split_reminder` / `catty_cache_boundary`。第二轮适配最新版：`fadianji_scenes.build_fadianji_group_scene_bank()` 渲染时把音料旧边界「 / 」转成 marker（140k 预算不变，实测 139,986 chars / 1,383 marker / 0 斜杠）；`FADIANJI_FIRST_MES` 单行化、§4 示例/音气速查改 marker、§13 删旧旁白池括号、§1380 去 phase/recency；`catty_core_persona` §3 补 marker 规则、§4 去 `catty_nsfw_gate` 旧名；`catty_game_recall` 不再承诺空参 list；`read_file`/`run_code` 按 `is_owner` 从 schema 过滤；表情骨架改成「能调 catty_emoji 就别写 EMOJI_QUERY」。验证 6/15/22/41/9/21/36 + tool 4/11/25/7 + config 11/11 + emoji 12/12 + quick_ack 13/13 + scene 19/19 全绿 + plugin-load-ok。
- **自由发挥也要知道高潮/怀孕（2026-10-25）**：freeform 注入 `_build_nsfw_freeform_state_facts` 的【当前状态·既定事实】块——场景 / 强度短音（不含 P 编号）/ 高潮次数 + 失神余韵 / 怀孕进度（`pregnancy_count/BIRTH_THRESHOLD`、已生几只；机机 pregnancy 禁用则整行不出现）。非 freeform 仍注入 `build_phase_params_block_only`。演法不给，模型自己按状态写。
- **NSFW 空正字先重试再兜底（2026-10-25）**：spark 回空正字时不再一次就 fallback——`_nsfw_max_tokens` 800 起步，命中空正字立刻重试并翻倍（上限 3200），三次都空才用 `_pick_retreat_template`；该函数现在收 `persona` 并在出模板前跑 `apply_persona_lexicon`，机机兜底时不会冒出「主人/人家/猫耳」（catty 无词表，输出不变）。
- **`config.json` 明字密钥提醒**（必读）：以下字段本地都是明字，**是主人本地开发机约定，助手不要擅自脱敏、不要在 commit message / 日志 / 错误信息里打印**：
  - `ai.api_key` / `ai_fallback.api_key` / `audit_ai.api_key` —— opencode 主+备+审计
  - `vision.api_key` —— 智谱 GLM-4.6V
  - `filter.nsfw_spark_api_key` —— NSFW spark
  - `imagegen.api_key` —— Pixelbunny 生图（`pb_live_`）
  - `nsfw_search.pixiv_cookie` —— pixiv 鉴权
  - `image_search.saucenao_api_key` —— saucenao 反向搜图
  - 推代码时 `push_zip` 是打 zip 推到 `D:/CattyQQAI`，**不直接进 git**，但**任何错误日志、日志面板分享、调试输出**都要先 `redact` 再发。
- **主动与旧链路分开**：历史 `proactive.enabled=false` 只控制旧笨猫链路；2026-10-27用户明确要求机机恢复活跃时段冒泡，新 `fdj_proactive` 单独控制，旧循环移入 `proactive_legacy.py`。不启用怒气或local_critic，也不把机机的 `proactive_bubble` legacy禁用标记删掉。
- **`mc_status` 实际是 dead code**：`ai_fallback.mc_gate_enabled = false` 把整个 fallback 通道关死了，`mc_status.py` 还活着只是因为 import 链还在；删要谨慎，不删也行。
- **NSFW 处理在 `chat.system_prompt` 里以「最高优先」明字钉死**（同音字卖萌 / 主人级反差链 / 女性用户女同不屈后投降），**这是用户硬约束，不要做"安全边界版"**。

### `persona_prompts` 改字案时**必须连带改测试**

- `tests/test_persona_prompts.py` 用 `assertIn("具体中字标题", prompt)` 锁死行字，**改字案标题/重组段落会让测试炸**。2026-06-02 体检时跑出 11 fail，主人在此明确裁定「**代码 = 真相，测试要跟着代码走**」——重写测试按新代码真实字面替换断言后，**12/12 全绿**。
- 改完 `persona_prompts.py` 后必须：
  1. `python -m py_compile src/catty_qq_ai/persona_prompts.py`
  2. `python tools/dump_persona_prompts.py > tmp/dump_persona.txt`（一次性脚本，按 12 个 build_* 输出真实字面；跑完删 tmp/dump_persona.txt 和 tools/dump_persona_prompts.py 不入库，需要时再重生）
  3. `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8; .venv\Scripts\python.exe -m unittest tests.test_persona_prompts`（**必须显式设 UTF-8**，否则 PowerShell 管道 mojibake 会把全绿结果误判为 fail）
  4. 如果是标题/行字改了，**测试一起改**——别只改源字，也别只改测试：测试要锁住当前代码真实字面。

### Windows PowerShell 跑测试的坑

- 默认 PowerShell 管道会把 Python stdout 当系统 code page 解码，**中字 assert 全变乱码**误判 fail。
- 解法（**所有跑中字 assert 的测试都要这样**）：
  ```powershell
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $OutputEncoding = [System.Text.Encoding]::UTF8
  .venv\Scripts\python.exe -m unittest <module>
  ```
- 或者直接 `Out-String` 之前先 set encoding。**别误以为测试炸了就改源码**。

### 新增的「不一定非要 hot-reload 重启 bot.py」的东西

- `src/catty_qq_ai/data/cpu_engine/` 下的 yaml/json 走**进程内** `_cpu_engine_routes_watch_loop` 热重载（**不让** `catty_hot_reload.py` 把它当代码变更 kill bot）。
- `src/catty_qq_ai/data/nlu_cache/` 同样：进程内热重载。
- `src/catty_qq_ai/data/fdj_scene_delta/` 由 `fdj_scene_retrieval` 按 manifest/mtime 进程内原子热重载。
- 这三个前缀在 `IGNORED_PATH_PREFIXES` 里。**新加类似「数据级但改频繁」的目录**走同一模式：进程内 watch loop + 加到 `IGNORED_PATH_PREFIXES`，**别让守护 kill bot**。

### MCP / 训练相关通道（AGENTS 老段没提）

- **训练通道代码仍保留但总开关关闭**：[`scripts/catty_training_mcp_server.py`](scripts/catty_training_mcp_server.py) 与 [`scripts/catty_training_dashboard.py`](scripts/catty_training_dashboard.py) 仍在仓库；当前 `local_training.enabled = false`，不会启动本地训练/模型链路。
- **训练数据导出**：`python scripts/export_reply_gate_dataset.py`（AGENTS 里有）+ `auto_train_reply_gate.py` / `bootstrap_corpus.py` / `audit_memory.py` 几个工具脚本在 [`scripts/`](scripts/)。
- **NSFW 评测**：`tests/test_spark_nsfw_remote.py` / `tests/test_nsfw_15turn_benchmark.py` / `tests/test_jailbreak_*.py` 是基线（2026-06-02 体检时 `test_jailbreak_*` 因依赖实际 API 走 skip，不算 fail）。
- **设计字档**：[`docs/NSFW_DESIGN.md`](docs/NSFW_DESIGN.md) 5.8KB，NSFW 改造设计稿；新加 NSFW 行为前先翻这一份。

### 测试健康度（2026-06-02 体检）

| 模块 | 状态 | 备注 |
|------|------|------|
| `tests.test_content_block_unwrap` | ✅ 全绿 | 9 个 case 覆盖 happy path / 零误伤 / 含 image 块不动 / 解析失败透传 |
| `tests.test_reply_markers` | ✅ 全绿 | 5 个 case |
| `tests.test_openai_client` | 未跑（看名像请求层，含本地 critic 闭源路径） | 想加新路由必跑 |
| `tests.test_hot_reload` | 未跑 | 改 `scripts/catty_hot_reload.py` 必跑 |
| `tests.test_persona_prompts` | ✅ **12/12 全绿**（2026-06-02 重写） | 改 `persona_prompts.py` 必跑 + 必改测试 |
| 其它 `tests.test_jailbreak_*` / `tests.test_nsfw_*` | 多为 skip（依赖外部 API / 远端 NSFW spark） | 跑得起来就跑，跑不起来是预期 |

### `.vscode/claude-memory.md` 同步机制（新人必看）

- **仓库里有这一份** [`.vscode/claude-memory.md`](.vscode/claude-memory.md)，是 Claude Code 在 `C:\Users\99325\.claude\projects\e--VC-Catty\memory\` 自动积累的主人偏好/铁律的**汇总索引**——给 **Codex / Copilot / 其他 AI 助手**读的（它们看不到 Claude 的 personal memory 目录）。
- 头部写明「**上次同步：2026-05-26**」。**每次体检时**（比如本 2026-06-02 体检）要确认这份是否落后于 Claude 的原始记忆：
  - 看 `C:\Users\99325\.claude\projects\e--VC-Catty\memory\MEMORY.md` 是 Claude 自己维护的一行索引
  - 每条详情在 `feedback_*.md` / `reference_*.md` 同目录字件里
  - 让 Claude 重新生成这份汇总即可（或手动 diff）
- 同步时**别动主人反复教过的"4 条最容易踩雷"**（部署=push_zip / 先推后 commit / 回复要短 / 「主人」是专属称呼），那是历史回滚的最终结论。
- AGENTS.md 老段（`## Project Overview` / `## Key Modules` / `## Common Commands` / `## Hot Reload 行为` / `## 部署 vs Git` / `## Prompt Ownership`）和 `.vscode/claude-memory.md` 是**两份独立真相**，**不互相覆盖**——改了 AGENTS 不代表改了 claude-memory，反之亦然。改完任何一份都要明确告诉主人改了哪份。

### 关键字件速查（新人前 5 分钟）

| 想做什么 | 看这个 |
|----------|--------|
| 加新 message handler | [`src/catty_qq_ai/__init__.py`](src/catty_qq_ai/__init__.py)（1.6w 行，按功能 region 跳） |
| 改主回复行为 / persona | [`src/catty_qq_ai/persona_prompts.py`](src/catty_qq_ai/persona_prompts.py) + [`tests/test_persona_prompts.py`](tests/test_persona_prompts.py) |
| 改云端路由 / 切模型 | [`src/catty_qq_ai/openai_client.py`](src/catty_qq_ai/openai_client.py)（主路径）/ [`src/catty_qq_ai/anthropic_native_client.py`](src/catty_qq_ai/anthropic_native_client.py)（虽然默认关，但代码还在） |
| 改 NLU 过滤 | [`src/catty_qq_ai/nlu/`](src/catty_qq_ai/nlu/) + `_legacy_*` 降级路径 |
| 改 hot-reload 守护 | [`scripts/catty_hot_reload.py`](scripts/catty_hot_reload.py) + `IGNORED_DIR_NAMES` / `IGNORED_PATH_PREFIXES` |
| 改配置加载 / 默认值 | [`catty_config_loader.py`](catty_config_loader.py) 的 `DEFAULT_CONFIG`（`config.json` 实际配置 + 默认值是两份独立真相） |
| 改训练 / LoRA 流程 | [`scripts/local_lora_train.py`](scripts/local_lora_train.py) + [`scripts/catty_training_dashboard.py`](scripts/catty_training_dashboard.py) |
| 改生图通道 | [`src/catty_qq_ai/catty_nsfw_imagegen.py`](src/catty_qq_ai/catty_nsfw_imagegen.py)（双 provider 路由） |
| 改 NapCat 启动 | [`catty_integrations.py`](catty_integrations.py) `_start_napcat` |
| 部署 / 推送 | `rpwsh-local` MCP `push_zip`（详见 `catty-deploy` 仓库记忆） |

