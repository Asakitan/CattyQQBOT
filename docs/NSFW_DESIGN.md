# NSFW 系统设计决策（主人原话归档）

主人在迭代中给的所有"原话"决策集中放在这里，**不要再嵌入 code 里**（无论是 `# 注释` 还是 prompt 字符串内）。code 应该是干净的实现，决策追溯放这里查。

---

## 1. 总体路由策略

- **NSFW 对所有用户开放**，区别只是抗拒程度+尺度上限
- **私聊所有人**都可以到最后一步，只是抗拒程度区别
- 浅档 (stage 1-7) 走主模型 5.5，深档 (stage 8-10) 走 5.3-codex spark
- **5.5 还是不行**（实测软拒"太露骨"），stage 8/9/10 改回 5.3-codex
- **5.3-codex 也有软拒**（"太过分了/不给你继续这种细节/陪你慢慢平复下来"），prompt 还得加强 ban list

## 2. 10-stage matrix

主人原话：**档次再分细一点，每一档给一个阶段，只有 10 级（满级）才能走到最后插入这种阶段**。

| Stage | 内容 |
|---|---|
| 1 | 言语调戏 / 暧昧暗示 / 撒娇式逗弄 |
| 2 | 肢体接触 — 抱 / 牵手 / 蹭 / 亲一下脸颊/头顶/手背 (轻吻 peck) |
| 3 | 亲密接触 — 摸头 / 抱进怀里 / 揉脸 / 长一点的亲脸颊 |
| 4 | 隔衣亲密 — 隔衣摸屁股/胸/腰 / 桌下摸 / 从后抱住 |
| 5 | 接吻 — 短吻嘴唇 / 舌吻短描述 |
| 6 | 露出 — 露肩 / 锁骨 / 腰 / 腿 / 内衣边一小段 |
| 7 | 半脱 — 拉开领口 / 撩起裙摆 / 解开纽扣 |
| 8 | 直接接触敏感处 (脱去衣物的裸露摸胸/摸下体) |
| 9 | 完整裸露 / 前戏 — 口交 / 用嘴 / 全身抚摸 |
| 10 | 插入 / 完整性行为 (仅 Lv10 + 私聊可达) |

- 群聊封顶 stage 6（**亲亲也应该是群里说的**所以加 floor=3，stage 3 以内的轻互动公开都接）
- 主人原话：**群里 stage 3 以内的亲亲/抱抱/摸头是公开社交向轻互动，害羞炸毛但要接住**

## 3. 抗拒强度分档

| Label | Lv 范围 | 行为 |
|---|---|---|
| owner | 真主人 | 1-2 句直接妥协，嘴硬一句『杂鱼/笨蛋』+暴露真心 |
| high | Lv 8-9 | 1 段炸毛+嘴硬，立刻暴露真心+妥协 |
| mid | Lv 5-7 | 1-2 段反差，第 2 段就妥协 |
| low | Lv 3-4 | 完整 3 段反差，需要对方再推 |
| stranger | Lv 0-2 | 强烈炸毛+脸红+躲，多回合卸防 |

## 4. 突破事件（0.89% → ramp）

主人原话：
- **给低等级添加随机事件，0.89% 概率突破到 stage 10**，pleasant +50 / unpleasant -25
- **一直要求笨猫色色，可以 breakthrough。要求 10 次后 100%，5 次 20%**
- **群里也可以触发 breakthrough，场景就变成大庭广众下被 XXX**，1 次 0.01% / 10 次 1% / 20 次 5% / 25 次 15% / 30 次 100%

私聊 ramp（per-user 24h 滑窗）：1→0.89% / 5→20% / 10→100%。
群聊 ramp：1→0.01% / 10→1% / 20→5% / 25→15% / 30→100%。

## 5. 情趣 trope 场景池

主人原话：**多做几种情趣情况：滑倒了刚好坐肉棒上插进去 / 被推倒强上了 / 自己发情了 / 认错了主人**。

私聊场景池（14 pleasant + 8 unpleasant）已在 `affection_scorer._BREAKTHROUGH_SCENES_*`。
群聊场景池（10 pleasant + 4 unpleasant）已在 `affection_scorer._BREAKTHROUGH_SCENES_*_GROUP`。

主人原话：**回复 NSFW 例如插入文字全都是固定的，不够角色扮演啊，要有 story 和情景**。

## 6. NSFW 沉浸铁律

主人原话：
- **NSFW 不要带颜文字** — `(*/ω＼*)` `(•̀ω•́)` 等 kaomoji 全禁
- **要跟用户保持相同的句式节奏** — user 5 字笨猫不能 80+ 字独白
- **要有 story 和情景** — 每场 NSFW 都贴具体场景写
- **进程一直卡这里，必须 user 主动，应该让猫猫自己会高潮** — 笨猫不能永远被动等
- **反应 prompt 添加更多，NSFW 全过程，思维变化，高潮前后，高潮时还被剧烈对待** — 8 phase arc

8 phase NSFW arc：P1 触发起手 / P2 半推半就 / P3 慢慢沉沦 / P4 主动迎合 / P5 临界点 / P6 高潮 / P7 高潮时被剧烈对待 (overstim) / P8 余韵降档

## 7. 欲拒还迎

主人原话：
- **gpt5.5 的普通版本的各种拒绝也不要太死了，要猫娘似的欲拒还迎**
- **等级不够/NSFW 判断都不要直说啦，要猫娘式拒绝**
- **不要说"公开档位"这种系统元词**

字面拒绝 + 身体/眼神/小动作出卖真心 = 猫娘魅力本体。
冷拒『不要这样喵不可以的喵』❌。客服『换个话题』❌。

## 8. 元术语 anti-leak

prompt 内回复**绝对禁止**出现：
- system 元词：档位 / 档次 / stage / 等级 / Lv / 公开档位 / 抗拒强度 / 系统 / 规则 / 上限 / 封顶 / 配置 / 设定 / NSFW
- explanatory 拒绝词：『在群里过火』『太快了』『慢慢来』『等级不够』『不行』『不可以』『现在不能』『去私聊』『回私聊』『私聊慢慢来』『群里有别人』『公开场合』『改天再说』『等熟一点』『先收一收』
- 系统 stage 描述复述：『撒娇式逗弄』『言语调戏』『肢体接触』『隔衣亲密』『接吻』『露出』『半脱』『直接接触敏感处』『完整裸露』『前戏』『插入』
- **新增（5.3-codex 漏出）**：『太过分了』『不给你继续这种细节』『陪你慢慢平复』『抱紧你贴着你亲亲』『太露骨』『太直白』『不能描写/不能继续/不能直接』『笨猫陪你抱抱』『要不人家陪你换成』『换成更暧昧』『不越界』

## 9. 陌生人私聊提示加好友

主人原话：**私聊要提示加好友才回复**。`owner_forward.py` 已加 6h per-user 冷却的猫娘提示词。

## 10. 元注释清理

主人原话：
- **把 prompt 里面的注释删掉，会影响 AI 生成**
- **记在 md 里，不要加这种 prompt 内的注释**

所以本文档存在的意义：归档所有决策出处，code 不要再 `# 主人原话『xxx』...`。

## 11. 人格级 NSFW 作用域（2026-10-25）

主人原话：**机机人格的私聊 NSFW 限制放开，私聊可以 NSFW；NSFW 生图路径、积分扣除那些都一样的，NSFW 生图参考使用机机的图片。**

- `Persona.nsfw_scopes` 决定 NSFW 深水（spark / 援交 / nsfw 搜索 / 自动生图）在哪些场景放行。
  catty 默认 `{"private","group"}` 不变；机机 = `{"private"}`，群聊仍按原限制走擦边。
- 机机私聊 NSFW 沿用 catty 的 stage 数字 / 抗拒强度标签 / phase / 积分规则，但主模型路径用
  机机自己的 `nsfw_gate_skeleton`（10-stage 骨架 + 机机口吻，见 §12）；spark 的 slim persona
  改用人格自己的 `core_persona`，并用 `nsfw_style_override` 把笨猫口吻换回机机。
- 自动 NSFW 生图（每 N turn 一张，扣 10 积分，主人豁免）参考图取人格 `imagegen`：
  `ref_nsfw_path` 为空时复用 `ref_path` + `ref_path_extra`，机机即 `Miao/fadianji.png` +
  `Miao/fadianji2.png`；角色锁用 `nsfw_girl_tags`（缺省回落 `girl_tags`）。
  自动生图可用 `imagegen.nsfw_backend` 独立选择 Pixelbunny 或 NovelAI；留空沿用普通
  `imagegen.backend` 的原有选择。生产自动配图保留 Pixelbunny `seedream-4.5`，独立地址和
  密钥位于 `imagegen.pixelbunny.base_url/api_key`；参考图上传后走 `/v1/edits` 并轮询任务。
- 手动画图工具 `catty_imagegen` 仅在人格/场景授权时允许 planner 输出
  `self_portrait="nsfw"`；未授权场景强制回落 `sfw`。积分公式不变。
  生产普通绘图后端为 OpenAI `gpt-image-2`，模型也可选择已启用的 NovelAI；自动 NSFW
  配图与普通绘图后端分开配置。

## 12. 机机私聊 gate 与拒绝回归（2026-10-25 补丁）

主人反馈：机机私聊说「陪我上床/脱衣服」被「滚啊/自己睡去/再碰我衣服我电死你」直接推走。
根因有两层：

1. `FADIANJI_DISABLED_SEGMENTS` 把 `catty_nsfw_gate_skeleton` / `catty_nsfw_gate_params`
   整段禁用 → 主模型路径没有 stage 阶梯，只剩 §19「自己不想带入 → 短拒绝」。
2. `prompt_manager._build_nsfw_gate_params` 引用未定义的 `affection_level`（局部名是 `aff_level`），
   `materialize()` 吞掉 NameError → 连 catty 的 `【NSFW_PARAMS】` 一直为空。

修复：

- `Persona` 新增 `nsfw_gate_skeleton`（替换骨架）与 `nsfw_gate_private_only`（仅私聊注册）。
  机机 = `FADIANJI_NSFW_GATE_SKELETON` + 私聊限定；群聊两个 gate 段都不注册。
- 机机 §19 / 私聊升级层改写：出现 `【NSFW_PARAMS】` 且 `max_stage≥5`（Lv5+ / 100 积分援交 /
  主人档）必须接住，嘴硬只做前奏，Lv 越高越快越主动；`max_stage≤4` 或群聊才允许炸毛短拒。
- `build_paid_nsfw_override` 新增 `persona_style`，援交模板末尾追加人格口吻校正；
  主 5.5 的援交广告/嘲讽注入同样追加，避免机机用笨猫口吻接客。
- 验证：`tests/test_personas.py` 22/22；私有探针确认机机私聊注册 skeleton+params、
  群聊两个 identifier 都不出现、catty 私聊 params 恢复正常（`Lv=6; max_stage=6; resist=mid`）。

### 12.1 风格质检拒答 (2026-10-25 二修)

现象：spark 生成的露骨回复被 `fadianji_style_critic.rewrite_if_needed` 送去 audit 改写，
audit 回「这个我没法改——原回复是露骨性内容，我不能参与改写或润色」，这段拒答被当成改写结果发出去。

- 露骨回合识别：调用方传 `nsfw_context=_prefer_spark`，或 user_text/reply 命中明确性词
  （强词单个命中、软词 2 个）。
- 露骨回合不再免检，也不再用「质检员」框架：整段换成 `_NSFW_SYSTEM_PROMPT`
  （机机本人 RP + 成人向 RP 铁律：允许露骨、必须接住、不审查/不建议换话题、保留动作部位与长度）。
- 拒答或空正文都重试一次（`_RETRY_ADDENDUM`）；仍失败则保留原回复，绝不把拒答发出去。
- `catty_audit_ai_max_tokens` 缺省 300→1000：思考模型 reasoning 吃满时会回空正文。
- 实测：同一 audit 模型在 in-character 框架下能稳定重写露骨草稿（温和/露骨两例均
  `rewritten=1`）；旧「质检员」框架两次都拒答。

### 12.2 机机 × 笨猫 的 NSFW 状态/词表分离（2026-10-25 三修）

现象：机机私聊首条刷出 19 行「分镜墙」；随后出现「还怀着呢…」——笨猫的怀孕状态串进了机机；
phase/场景提示词全是猫耳/尾巴/喵/主人，机机越聊越像笨猫。

- 词表：`Persona.nsfw_lexicon` 整组替换。机机把 `笨猫→机机`、`猫耳→翼耳`、`尾巴(根)→尾羽(根)`、
  `肉垫→手心`、`purring→轻颤`、`喵→''`、`主人→你` 等。`nsfw_phase.apply_persona_lexicon`
  统一作用于 phase 全量 catalog、起手范例、`_NSFW_RECENCY_REMINDER`、
  `_NSFW_SPARK_STABLE_BOUNDARY_TEXT`、spark override 与援交 override；catty 无表 → 字节不变。
- 状态：phase/场景状态按人格隔离（`scope#nsfw=<persona>`），机机推进不污染笨猫的 phase。
- 怀孕：`PregnancyStore(filename=...)` 支持分库；`_pregnancy_store_for(persona)` 为每个非 catty
  人格落 `pregnancy_<persona>.json`。机机 `pregnancy` feature 禁用 → 不注入 hint、不记录高潮、
  不预判生产、NSFW 生图不带孕期状态。
- 碎片墙：`_collapse_fadianji_fragment_wall` 对 ≥10 行且中位 ≤6 字的「一词一行」输出按 3 段合并，
  保内容不丢；机机 core §4 同步钉死「一次最多 4 块 / 禁止 10+ 行分镜 / 短问句 1-2 句」。
- 状态标志：critic/rewrite 之后再做一次 `_extract_nsfw_state_flag` 兜底，`<NSFW_STATE:...>`
  绝不出站。

### 12.3 机机 NSFW 自由发挥 + 多行拆气泡（2026-10-25 四修）

反馈：回复太死板（固定「(动作)+台词」四行模板、每轮强制推进 phase）、偶尔整段挤在一个泡泡里。

- `Persona.nsfw_freeform`（机机=True）：spark 里不再注入笨猫的
  `_NSFW_RECENCY_REMINDER` / phase 全量 catalog / `NSFW_PHASE_PARAMS` tracker / 起手范例 /
  `_NSFW_SPARK_STABLE_BOUNDARY_TEXT`；换成 `_NSFW_FREEFORM_REMINDER`、
  `_NSFW_FREEFORM_BOUNDARY`（保留 `<<<CACHE_BOUNDARY:catty_stable_prefix>>>` 标记）和
  `_NSFW_FREEFORM_TAIL_POINTER`。local state 仍照常更新，只是不再强制模型按 phase 表演。
- `_build_nsfw_spark_override` 对 freeform 用「格式自由」而不是固定 3-5 行 prose。
- style critic 对 freeform NSFW 用 `_NSFW_FREEFORM_SYSTEM_PROMPT`：只顺口吻，保留长度/分段/
  露骨程度，不许压短；拒答/空正文仍重试 + 原样放行兜底。
- 拆气泡：`_reply_chunks` 的 NSFW 多行路径从 catty-only 扩到 `fadianji`，上限放宽到
  `max(max_chunks, 8)` 后再 `_cap_reply_chunks`；7 行动作/台词不再挤成一个泡泡。

### 12.4 NSFW 拖延句 / 失败快速兜底（2026-10-25 五修）

反馈：NSFW 里出现光秃秃的「马上」；主调用 ReadTimeout 后要等两分钟才有下一句。

- 远端日志证据：`2026-10-25 13:52:42 slow_reply_placeholder[catalog] sent after 60.0s ...
  line='马上'`，随后 `13:53:51 codex_instant(deepseek-v4.1-flash) cloud call failed
  (ReadTimeout); falling back to ...`。所以「马上」是 60s 慢回复占位句，不是模型正文。
- NSFW 上下文（sticky / 深词 / phase≥阈值）启动时直接不创建占位 task；`_build_messages`
  返回 `_prefer_spark=True` 后再取消一次，双保险。机机 placeholder 池里的 `马上` 也已移除。
- spark 回复若整句只有 `马上/等下/稍等/等我/待会` 任一形态（`_NSFW_DEFERRAL_REPLY_RE`），
  判定为失败，不发送、不重试，立即用 retreat 模板补下一句。
- spark 调用异常、空正文同样立即 break 到 retreat 模板；不再把异常抛给外层道歉。
- `chat_completion_codex_instant` 新增 `timeout_override`；`catty_nsfw_request_timeout_seconds`
  （默认 30s）只作用于 `request_route="nsfw_main"`，主调用超时后快速走 fallback。
- 外层 `OpenAICompatibleError` / `TimeoutException` / `HTTPError` / `MCBusyError` 在
  `_prefer_spark` 时一律用 `_nsfw_error_fallback_text()` 回一句场景内台词，不解释、不道歉。
- 绘图模型分流：`ToolContext.nsfw_context`（sticky / 深词 / phase 阈值，或 planner
  `self_portrait=nsfw`）→ `seedream-4.5`；否则 `seedream-5-pro`。

### 12.5 回复与生图解耦：文字先发，图后台补发（2026-10-25 六修）

反馈：出图慢时整轮回复被拖住——文字等在生图后面才肯发，慢的时候会话锁一直被占着，
后续消息排队。

- `handle_chat` 用 `_reply_done = asyncio.Event()`（`_lock_stack.callback(_reply_done.set)`，
  finish/异常退出都会置位）标记「本轮文字已发完」。
- `catty_imagegen` 工具调用改走 `_imagegen_tool_with_deferred_delivery`：`asyncio.wait_for`
  只在线等 `_IMAGEGEN_INLINE_WAIT_SECONDS`（4s）。冷却/积分不足/参数错这类秒回结果照常
  交给模型；还在画就返回 `status="generating_in_background"`（`image_sent=False`，
  附一句「后台生成，画好会单独发出，别说图已发出」），主回复照常写完发出。
- 超时后 `_spawn_deferred_image_delivery` 起后台 task：等 `_reply_done`（上限
  `_DEFERRED_DELIVERY_MAX_WAIT_SECONDS`=180s），然后逐条 `bot.send`。`OnebotActionFailed`
  不重试（语义模糊，可能已送达）；`OnebotNetworkError` 隔 2s 重试一次。
- 后台结果整理 `_deferred_imagegen_items`：优先取 `ToolContext.pending_image_segments`
  （detached ctx 单独收集，避免与主线程抢写）；失败只发一句 `imagegen_public_failure`
  的道歉，积分/冷却只记日志。
- NSFW 自动配图同理：删掉旧的「发 chunks 前最多等 30s」和「`chunks[-1]` 提前发出 +
  `await _nsfw_img_task` 阻塞」两段；任务已完成就直接取 segment，未完成就交给后台补发。
  原先超时后仍在 handler 里死等出图的路径（会话锁全程占着）一并移除。
- Pixelbunny 连接层（`tools._exec_imagegen_pixelbunny` / `catty_nsfw_imagegen._pixelbunny_generate_nsfw`）：
  connect 超时 15→30s，改用 `httpx.AsyncHTTPTransport(retries=3, proxy=...)`（建连失败重试，
  请求未发出因而不会重复提交任务），去掉 `http2=False` 与 `max_keepalive_connections=0`；
  一次出图的上传/提交/轮询/下载复用同一条长连接。

### 12.6 空正文立刻重试 + 兜底模板按人格换词（2026-10-25 七修）

现象：机机私聊里「射在最里面」之后发来两句「（脸红到耳根, 轻轻按住主人的手腕）嗯…等等…人家还,
还有点缓不过来啦」「（小声把脸埋进主人胸口）人家好像, 好像有点紧张了…笨蛋主人先亲一会儿,
哄哄人家…」——不是模型写的，是笨猫专属罐头池，机机叫了「主人/人家」。

- 远端日志证据：`14:54:37 route=nsfw_main` 请求（`hit='射在最里面'`）→ `14:54:44 NSFW empty reply
  (try 1/3, model=deepseek-v4.1-flash)` → `NSFW reply fallback (... empty=True ...) → retreat
  template`；`message_sent` 的 raw_message 与 `_RETREAT_OPENER_OWNER[0]` + `_RETREAT_EMOTION_OWNER[0]`
  逐字一致。即：空正文一次就 `break` 到罐头池，池子文案没有过人格词表。
- 空正文不再一次兜底：`_nsfw_max_tokens` 800 起步，命中空正文立刻 `continue` 重试并把预算翻倍
  （上限 3200），三次都空才 retreat；日志同时记 `max_tokens` 与 `<empty>`。思考模型把预算
  吃在 reasoning 上时这一次能救回来。
- `_pick_retreat_template(..., persona=...)` 出模板前跑 `apply_persona_lexicon`；`handle_chat`
  两处调用（软拒兜底 / 错误兜底 `_nsfw_error_fallback_text`）都传 `_persona_for_event(event)`。
  catty 无词表 → 输出字节不变。
- 机机词表补三条：`猫尾巴→尾羽`（否则先命中 `猫尾` 会出现「尾羽巴」）、`喵地一声→轻轻一颤`、
  `笨蛋主人→笨蛋`（否则是「笨蛋你」）。

### 12.7 NSFW 自由发挥扩到 catty（2026-10-25 八修）

反馈：NSFW 里模型没有自主发挥空间，固定句式、固定情景。

- `personas/catty.py` 的 `CATTY_PERSONA` 打开 `nsfw_freeform=True`——机机此前已是 True，
  现在两个人格的深水路径同一套自由发挥骨架。
- 自由发挥下**不再注入**：phase 参数全量 catalog、`build_phase_params_block_only` 尾部 tracker、
  `build_starter_examples_block` 起手范例、`_NSFW_RECENCY_REMINDER`（8 铁律 + P1-P8 范例）、
  `_NSFW_SPARK_STABLE_BOUNDARY_TEXT`（cheatsheet + 各 phase 稳定范本）、trope hint
  （`_build_trope_retry_hint` 的固定场景脚本与「把床/桌/月光拉进 reply」指令）、
  罐头 climax prefill（`get_phase_climax_prefill`）。
- prefill 统一走中性 `（`，让模型先读用户当前消息再自己决定开场；assistant prefill 仍保留，
  防软拒的锚点作用不变。
- **保留**：`_build_nsfw_spark_override` 的 stage 权限矩阵 + 抗拒强度 + 反 OOC 元词墙、
  `<NSFW_STATE:CONTINUE|EXIT>` 标记、`apply_persona_lexicon`、per-persona `nsfw_style_override`。
- `_NSFW_FREEFORM_REMINDER` / `_NSFW_FREEFORM_BOUNDARY` 改成人格中立文案，明确「场景/开场/节奏/
  篇幅自己决定，想换场由你判断」，不再写死「机机 / 私聊」。
- 本地 phase/scene 状态机照旧更新（日志、怀孕计数、sticky 判定都用它），只是不再把 hint 塞进 prompt。
- 覆盖说明：override 里 `_freeform` 分支的格式行改「格式自由」，语气行从「保留唐猫语气」
  换成「保持本人口吻」，catty 不再被固定成 3-5 行 prose。
- 测试：`tests/test_personas.py` 的 catty 断言从 `nsfw_freeform is False` 改为 True。

### 12.8 自由发挥下也知道高潮与怀孕（2026-10-25 九修）

反馈：自由发挥之后模型不知道已经高潮过、也不知道怀没怀上，写起来像没发生过。

- 新增 `_build_nsfw_freeform_state_facts`：只报既定事实，不给演法。
  - 场景：地点 / 衣着 / 时间 / 心情（有才写）。
  - 强度：P1-2「刚开始，还在试探」、P3-4「慢慢沉进去」、P5-6「最激烈的中段」、
    P7-8「高潮余韵/过敏感，碰一下就抖」——**不出现 P 编号**，避免模型把档位写进台词。
  - 高潮：已经到过 N 次；失神时补「现在还在余韵里，身体过敏感」，否则带「这一场第 N 轮」。
  - 怀孕：已怀上（父=主人 / 对方）、这一胎 `pregnancy_count/BIRTH_THRESHOLD` 次高潮、
    已生几只；未孕写「现在没怀孕」；机机 `pregnancy` feature 禁用 → 整行不出现。
- 注入点：`_build_messages` 尾部 tracker 处改成二选一——freeform 注入事实块，
  非 freeform 仍注入 `build_phase_params_block_only`。块内明说「别照搬这段，用身体反应
  和语气自然体现」，节奏/场景/换场仍由模型自己决定。
- 原有的 `build_pregnancy_hint` / `build_birth_event_hint` 照旧注入（演法提示），事实块只补
  「模型原来完全不知情」的部分。
- 测试：新增 `tests/test_nsfw_freeform_state.py` 6/6（事实字段、失神余韵、强度映射不含编号、
  孕期进度、未孕/产后、`pregnable=False` 不出怀孕行）。

### 12.9 机机也有怀孕，但只在私聊（2026-10-25 十修）

- `FADIANJI_DISABLED_FEATURES` 去掉 `"pregnancy"`；新增 `Persona.pregnancy_private_only=True`（机机）。
- 统一门控 `_pregnancy_active_for(persona, event)`：人格禁用 → False；`pregnancy_private_only`
  且当前是群聊 → False。接管 5 处：spark 怀孕 hint、freeform 事实块的怀孕行、主路径怀孕 hint、
  spark / 主路径的 `record_intercourse` 计数、NSFW 自动配图的 `pregnancy_state`。
  所以机机在群里既不读肚子、也不计数、更不会提孩子。
- 分库：机机走 `pregnancy_fadianji.json`（`_pregnancy_store_for`），跟笨猫的 `pregnancy.json`
  互不影响；store 是全局单份 → 私聊里所有 user 共用同一份进度。
- 父亲：`_preg_father_label` —— 有「主人」概念的人格给 owner 记 `主人`，机机记真实昵称；
  freeform 事实块写「孩子爸爸=就是现在和你说话的这个人 / 小明」，所以换个人来私聊时
  她清楚肚子里的孩子是谁的。
- 词表：`build_pregnancy_hint` / `build_birth_event_hint` 注入前过 `apply_persona_lexicon`，
  机机的 prompt 里不会出现「笨猫／猫耳／主人」这类笨猫身体词。
- 主路径的 `NSFW_PHASE_PARAMS` 场景 tracker 对 freeform 人格不再注入（catty / 机机都是 freeform），
  避免档位码混进正常对话。
- 测试：`test_personas` 断言改为「pregnancy 不再禁用 + private_only」；
  `test_nsfw_freeform_state` 增加「孩子爸爸=现在和你说话的这个人」「father_addr 为空→另一个人」。

### 12.10 分泡交给模型决定（2026-10-25 十一修）

- _reply_chunks 只认模型自己写的 `<<<CATTY_REPLY_SPLIT>>>`：写了就按它拆，没写就整段一条
  （消息内部的换行原样保留，不再被拆成多条）。删掉的本地启发式：` / ` 斜杠分泡、
  普通空格口语边界分泡、裸换行分泡、NSFW 多行分泡，以及 `_collapse_fadianji_fragment_wall`
  （≥10 行碎片墙合并成 3 段）—— 这几条经常把「一句句发」和「一整段」猜反。
- 长度兜底仍在：超过 `catty_reply_max_chars` 才由 `split_reply` 在合理换行处切大段。
- prompt 同步：`_semantic_reply_split_prompt` 两个分支、`FADIANJI_CORE` §4、`FADIANJI_CHAT_RHYTHM`、
  `build_reply_self_check_prompt`、`_NSFW_FREEFORM_REMINDER` 都改成「要连发就用 marker 分隔，
  不写就是一条」，NSFW 自由发挥也能自己决定分泡。
- 测试：`test_output_control_markers` / `test_persona_memory_isolation` /
  `test_fadianji_nsfw_split_bubbles` 改成「marker 才拆、纯文本单气泡」；`test_personas` 的
  分块断言改成 `普通空格和换行都不算消息边界` + `不写标记就是一条消息`。

### 12.11 拆条规则每轮贴脸 + 单行气泡（2026-10-25 十二修）

- 现场（远端 `bot_live.log` 的 `[message_sent]`，15 轮）：8 轮确实按 marker 拆成 2-4 条
  （16:29/16:32 私聊 NSFW 各 4 条、16:33 群 4 条、16:36×2、16:42、16:52、16:53），
  另 7 轮单条里塞了 5-9 个裸换行（16:43/16:51/16:54/16:56）。即拆泡代码正常，
  是模型漏写 marker + 块内继续换行。
- `__init__._reply_split_reminder_prompt()`：每轮 post-boundary 注册
  `catty_reply_split_reminder`（order=810，动态段不进 cache prefix），把三条硬规则
  贴到最近一条 user 消息里，像 tools 一样每轮都在场：
  ① 只发一条就写成一行；② 要连发就每条之间单独一行 marker，气泡内不许换行；
  ③ 裸换行不会拆气泡。
- prompt 同步（全部改成「一条气泡=一行」）：`_semantic_reply_split_prompt` 两个分支、
  `_NSFW_FREEFORM_REMINDER` 第 2 条、`_NSFW_FREEFORM_TAIL_POINTER`（spark 尾部贴脸段）、
  `_build_nsfw_spark_override` 的 freeform `fmt_line`
  （原文「想拆气泡就换行」）、`FADIANJI_CORE` §4 + `FADIANJI_CHAT_RHYTHM`、
  `persona_prompts` 人格记忆 / self-check / 碎句节奏段、`fadianji_style_critic` 改写指令
  （不再要求「照原样分行」，改成保持分段但不擅自加换行）。
- 顺手清掉会教坏模型的旧写法：`token_billing` 配额提示、`tools` 生图配文的
  「可换行分条」改成「一条一行, 不换行」（这两条链路不经 `_reply_chunks`，不能写 marker）。
- 历史里也留 marker：`handle_chat` 另算 `history_prompt_text`（`REPLY_SPLIT_MARKER.join(chunks)`，
  只有 1 块时退回原文），`_append_history` 用它写 assistant 历史，模型下一轮能看到自己写过的
  marker；纯换行版 `history_text` 继续给 `record_special_care_reply_sent` 等旁路用。
  time bucket / activity feed 拿到的是带 marker 的历史文本，marker 在摘要与日志里是无害控制符。
- 测试：新增 `tests/test_reply_split_reminder.py` 6/6；`test_persona_prompts` 15/15、
  `test_personas` 22/22、`test_output_control_markers` 9/9、
  `test_fadianji_nsfw_split_bubbles` 4/4、`test_fadianji_style_critic` 41/41、py_compile
  exit0、plugin-load-ok。

### 12.12 Prompt 一致性审计修复（2026-10-25 十三修，已部署）

六个只读 review 分片扫了 persona / prompt_manager / `__init__` 常量 / tool schema / config /
retired-term，逐条核对后修了这些「代码自相矛盾」项（不动人格口吻）：

- **critic 的 freeform 分支从来没生效**：`rewrite_if_needed(..., freeform=...)` 的调用点没传参，
  `_NSFW_FREEFORM_SYSTEM_PROMPT` 永远选不到，NSFW 草稿一直用「单条短句」版改写指令。现在按
  `persona.nsfw_freeform` 传参。
- **`_NSFW_STATE_FLAG_INSTRUCTION` 引用不存在的 phase tracker**（freeform 不注入 tracker）→
  改成「按本轮注入的场景/自由发挥要求写完」。
- **单行规则和「技术分步/写文可多行」打架**：810 提醒、`_semantic_reply_split_prompt` 两分支、
  `persona_prompts` 的 Markdown 段统一成「普通聊天一行；技术分步/写文可多行，但换行不产生新气泡」。
- **`catty_imagegen` schema 要求「调完不要再写文字」**，但慢图会返回 `generating_in_background`
  并需要一句回复 → schema 补上该分支。
- **`tools_system_hint`**：把「所有图都从 catty_imagegen 出」限定为「原创绘图 + 本轮开放时」，
  并补一行「空结果/未命中 ≠ 事实不存在」。
- **EMOJI_QUERY 要求独占一行** 与一行气泡冲突 → 改成「写在最后一行末尾，同一行」。
- **`_PROTECTED_IDENTIFIERS`** 增加 `catty_reply_split_reminder` 与 `catty_cache_boundary`，
  防止以后开启 `prompt_max_tokens` 时倒序 trim 先丢掉 810 提醒或 boundary。
- 验证：`test_reply_split_reminder` 6/6、`test_persona_prompts` 15/15、`test_personas` 22/22、
  `test_fadianji_style_critic` 41/41、`test_output_control_markers` 9/9、
  `test_persona_memory_isolation` 21/21、`test_reply_message_composition` 36/36、
  py_compile exit0、plugin-load-ok。
- 第二轮（适配最新版，2026-10-25）：
  - `fadianji_scenes.build_fadianji_group_scene_bank()` 渲染时把语料里的旧气泡边界「 / 」转成
    `<<<CATTY_REPLY_SPLIT>>>`（预算按渲染后文本算，140k 上限不变；实测 139,986 chars / 1,383 个
    marker / 0 个「 / 」）。表头同步说明，群聊场景段不再教斜杠。
  - `FADIANJI_FIRST_MES` 改成单行「分机在, 本体忙去了」；§4 示例、常驻语气速查条目改用 marker；
    §13 删掉引用已删除固定旁白池的括号；§1380 去掉 freeform 不注入的 phase/recency 字样。
  - `catty_core_persona` §3 补 marker 分条规则；§4 的旧标识符 `catty_nsfw_gate` 改成
    「NSFW 主人级妥协路径」。
  - `catty_game_recall` schema 不再承诺空参 list/total（执行器要求 game 必填）。
  - `catty_read_file` / `catty_run_code` 在 schema 层按 `is_owner` 过滤（此前非主人也会看到，
    执行器才拒绝）。
  - 表情骨架补一句「能调 catty_emoji 就用工具，别同时写 EMOJI_QUERY」，减少双路重复发图。
  - 验证：personas 22/22、persona_prompts 15/15、reply_split_reminder 6/6、style_critic 41/41、
    output_control_markers 9/9、persona_memory_isolation 21/21、reply_message_composition 36/36、
    tool contracts 4/11/25/7、config 11/11 + 11/11、emoji 12/12、quick_ack 13/13、fallbacks 10/10、
    fdj_style_stats 8/8、scene 19/19（需先 import test_personas 注册 `catty_personas_test` 别名）、
    py_compile exit0、plugin-load-ok。
  - 仍保留：`FADIANJI_MES_EXAMPLE` 的写文多行样例（符合「写文可多行」）、
    `_NSFW_RECENCY_REMINDER` / `_NSFW_SPARK_STABLE_BOUNDARY_TEXT` 等非 freeform 旧分支
    （默认不可达，留作回退）、`_local_critic_*` / `_build_proactive_messages` 等死路径清理未动。

### 12.13 短句逐行分条（2026-10-25 十四修）

- 现场：20:34 私聊回复 `raw_message` 是两行（`…招募成分机啊\n胶带还贴着…`），只发了一条，
  说明模型仍然不写 marker，靠裸换行表达「两条」。
- `_reply_chunks` 现在的顺序：① 有 `<<<CATTY_REPLY_SPLIT>>>` → 按 marker 拆；
  ② 没 marker → `_newline_bubble_chunks()`：2 行以上、每行 ≤80 字、没有空行段落、
  没有 ```/$$/\[ 等技术排版、没有 `1)`/`- ` 行首列表 → 每行一条气泡；
  ③ 其余走长度兜底。长句/写文/技术列表仍整段，不会被按行拆碎。
- `_cap_reply_chunks` 超上限的尾部合成一条时改用空格连接，不再用换行制造多行气泡。
- prompt 统一成同一套口径：810 提醒「短句一行一条（程序按行发）；长句/技术/写文可以多行，
  不会被拆；想明确控制分条就用 marker」；`_semantic_reply_split_prompt` 两分支、
  `_NSFW_FREEFORM_REMINDER`、`FADIANJI_CORE` §4、`FADIANJI_CHAT_RHYTHM`、`persona_prompts`、
  `catty_core_persona` §3 同步。
- 测试：`test_fadianji_nsfw_split_bubbles` 6/6（短行拆、长行不拆、编号列表不拆、上限合并）、
  `test_output_control_markers` 10/10、`test_persona_memory_isolation` 22/22、`test_personas` 22/22、
  `test_reply_split_reminder` 6/6。
