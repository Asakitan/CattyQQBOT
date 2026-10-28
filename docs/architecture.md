# Catty 工具与回复交付架构

## 语音消息转文字（已部署）

QQ 客户端的转文字结果不经过 OneBot；入口在 `_rule` 里用 `_extract_incoming_with_voice` 抽取消息，发现 `record` 段就调用 `voice_stt.transcribe_event`：`url` 下载 / 本地路径 / `get_record` 三选一取音频，嗅探格式后写入 `data/voice_inbox/`，再由独立 `voice_stt_worker.py` 用 faster-whisper（CT2 int8）转写，PyAV 自行解码到 16k 单声道。结果以 `【语音转写】` 并回 `incoming.text/history_content`，纯语音消息因此能进入准入和主模型；失败只给“未能识别内容”，SILK 直接跳过，同一音频缓存 10 分钟。

## 调情短语音（已部署）

`catty_voice_say` 与其它工具一样由模型自行决定何时调用，schema 明确限定“对方在调情/暧昧/撒娇时偶尔一条”，普通问答不适用。工具对所有人格暴露，音色按人格解析：`voice.default`（默认 `michelle`）兜底，`voice.persona_voices` 覆盖（默认把 `fadianji` 指到机机音色）。执行器再走 `voice_reply.reserve_slot`：每会话 5 分钟冷却 + 概率门（默认 0.6）；主人（`catty_owner_qq`）跳过冷却与概率，但仍受“本轮 1 条”限制。

`bank` 后端读取包内 `data/voice_bank/<音色>/bank.json`，按 mood 选片段并避免连续重复，秒级返回。`worker` 后端用独立 GPT-SoVITS 子进程合成模型自己写的一句短话：启动回执握手、240 秒超时、4200MiB 可用内存门槛、空闲 180 秒卸载、关闭时回收；按 `voice.profiles.<音色>` 取 gpt/sovits/参考音，切换音色时先收掉旧 worker 再判断内存；未配置或失败时退回语音库。

远端 worker 使用独立 venv（Python 3.14 + torch/torchaudio 2.11 CPU），预训练 BERT/HuBERT 以 fp16 存放；框架在缺 G2PW 时回退 pypinyin、在缺 `jieba_fast` 时回退 `jieba`，参考音用 soundfile 读取以绕开 torchcodec。合成结果裁剪后补 0.25 秒静音并做 8ms 淡出再归一化写盘：`fragment_interval=0` 时框架不追加尾部静音，线上 3.36 秒样本的最后 0.2ms 即结束，播放会像被削掉尾音。`catty_voice_say` 选好的语音在工具调用时就以 `record` 发出去，文字跟在后面或不写；只有 worker 合成超过内联等待时，才由 `_spawn_deferred_voice_delivery` 在文字之后补发（慢路径例外）。ActionFailed 不重试以免重复播报，NetworkError 隔 2 秒重试一次；发送失败只写日志。产物在 `data/voice_outbox`，随既有清理循环按 TTL 删除。

本轮排了语音时，出站文本先经 `reply_markers.strip_voice_generation_narration`：命中“语音正在生成/稍后发出来/马上发给你语音”等整句就删除；删完为空或模型本来没写文字，就跳过整轮文字发送（`matcher.finish()`），语音照常单独到达。工具回执与 schema 都不再出现“生成/稍后/后台”，避免模型照抄。被问“你喜欢我吗/想不想我/爱不爱我”时，schema 建议优先用语音回答、可只发语音，但没有关键词硬分支，是否发声由模型决定。

自动语音按 `voice.auto_chance`（默认 0.1）逐条消息抽取：命中且本轮回复 ≤50 字、无代码块/公式时，在文字前先发一句现成语音；模型已调用工具时不重复。

## 语音条模式（已部署）

拆成多个气泡的回复可以逐条念成语音。`voice_reply.only_window_active` 解析 `voice.only_hours`（支持 `23:00-01:00` 跨夜）：窗口内所有可念气泡都转语音；窗口外只有 `auto_chance` 命中时念一条。每轮发送前先按 `bubble_max_chars`（默认 40）过滤，`spoken_voice_text` 会剥掉括号/【】里的动作旁白，只念台词；整条只剩旁白就保持文字。合成条数受 `bubble_max_per_turn`（默认 3）与 `bubble_budget_seconds`（默认 45 秒）限制，超出预算的剩余气泡照常发文字。

NSFW（spark）回合走同一段逻辑，额外按 `voice.nsfw_bubble_chance`（默认 0.5）抽取，命中后最多念 `bubble_max_per_turn` 条。语音气泡用 `record` 发出，成功后把台词按“已送达”写入本轮交付回执，从而进入历史、续聊和自回复队列；发送失败则回落为文字，尾气泡失败也不会吞掉最后一句。

本轮只要有语音（`catty_voice_say` 直发或后台排队 `deferred_voice_started`），所有短气泡都会转成语音，不再只念一条；同一句已用语音说过时对应气泡不再补文字，晚到的后台语音若台词与已念气泡重复也会跳过。表情包场景最后一句已语音时，表情单独发送，不重复最后一句文字。

用户这句话在表达爱意/撒娇（`voice_reply.affection_voice_eligible`：喜欢你、爱你、想你、亲亲、抱抱、贴贴，排除“不喜欢/别爱”等否定）时，本轮直接触发语音条，不抽 `auto_chance`、不看 `only_hours`；开关 `voice.affection_trigger` 默认开启。

发声冷却按 scope 共享：`group:<群号>` 的 10 秒冷却由同群所有人共用，私聊按 `private:<QQ>` 各自计算；`voice.cooldown_seconds` 默认 10 秒，主人跳过冷却与概率。语音条触发同样受该冷却约束，但本轮 `catty_voice_say` 已经发过语音时不再额外等待，剩余短气泡继续念完。

## 本地语音候选（尚未接入运行时）

`tmp/voice-training-20261028-01/` 独立 Python 3.11 环境和 GPT-SoVITS V2 副本负责双音色训练，权重、参考音与试听落在 `models/voice/{michelle,fadianji}/`。本地 CPU 生成成功；远端 CPU 推理和 QQ 语音发送未实现验证，不由 Bot 自动加载这些权重。

`tmp/voice-quality-20261028-01/phrase_ab.py` 为发电机生成独立进程的单变量对照，读取同一组哈希锁定的模型及参考音；原始波形与日志保存在临时目录，统一 RMS 的试听和完整参数清单位于 `models/voice/fadianji/candidates/phrase-ab/`。只生成候选，不替换正式参考音或权重。详见 [语音候选](voice-candidates.md)。

`whole_refine.py` 复用生成与响度验证，以用户偏好的整句版为基线。在实验进程当前模型实例上包装 `decode`，记录语义 token、音素及 RNG 哈希并显式设置 `noise_scale`，不修改框架源码。新试听独立写入 `candidates/whole-refine/`，按实际结果保存用户反馈和有界 PCM 差异，不覆盖前轮文件。

用户淘汰调参候选后，`acoustic_ab.py` 捕获01的真实解码输入，将语义、音素、参考频谱和RNG存为本地张量快照，再用当前8轮/较早4轮/预训练声学权重逐项重放。每次重载快照并核验哈希，推理配置独立落在实验目录；产物位于 `candidates/acoustic-ab-20261029/`，用于归因而不是运行时音色替换。

同目录 `selection.json` 记录用户首选文件02的4轮检查点及样本哈希，只是实验候选指针，不被 Bot 自动读取。原8轮权重仍在原位置；4轮端到端多句推理尚未验收。

## 会话、记忆与目的补强（已推送）

观察器在收到消息时先更新当前会话滚动缓冲并唤醒等待者，再在线程记录符合显著条件的记忆和明确目的；不等待磁盘写入，进程突然退出可能丢失尚未提交的记录。消息读取和水位只绑定当前 scope 与人格，最多等待 3 秒。收尾工具只提交提议；本轮全部文本发送调用正常返回、没有更新的真人消息或待交付工作且当前文本不是问句/请求时，才关闭该用户续聊窗口。主回复沿用现有发送状态，不校验消息 ID；直接唤醒和私聊原路径不变。

`insight_memory.py` 用 SQLite 同事务保存完整记录和活跃标记；重要度与时间决定活跃排序及淘汰，完整档不随衰减删除。入口对当前 scope/人格查询活跃卡，`catty_recall` 可读取同范围归档；显式删除及退群清理同时删除完整记录并保留无正文墓碑，已有备份不会随之删除。存储新捕获的显著记忆，不等于全量聊天备份；两份账本及 SQLite 日志不纳入 Git。

`purpose_ledger.py` 按 scope、人格和主体记录明确计划/承诺及证据。共享主动调度读取本群候选；只有输出文本与目的内容匹配且 OneBot 回执含有效消息 ID 时才登记投递。目标主体引用该条消息的明确反馈才推进状态。文本关联是字词启发式，不保证语义判断；资料是未可信输入。两份 SQLite 文件由既有备份流程使用在线备份复制。21 文件已部署并合入，远端哈希一致、132 项通过；本地合并后 522 项通过。

## 共享人格架构（已合并并部署）

机机的群聊判门、证据编排、风格校验与主动调度接入通用人格流程；逐轮捕获人格对象，按其内容、开关及来源组装。群聊唤醒、续聊、复读让位和主动发送沿用同一组人格属性；真实发送开始后的人格切换不能撤销发送，成功回执归原人格。

近期自身消息按人格及自定义定义版本隔离，未知归属的自身回显等待发送回执，群友及其他机器人消息保留。猫猫旧 NSFW 键不变，其他人格按名称区分普通/付费窗口、阶段、占用及生图计数；权限仍由各人格的 `nsfw_scopes` 等字段决定。

共享主动调度复用 `fdj_proactive` 的每群每日计划和节奏，猫猫开关读 `proactive.enabled`，机机开关读 `fdj_proactive.enabled`；旧猫猫的节奏字段不再映射。机机场景及语料须由人格显式选择，其心情和自学也各有独立开关。离线回归 1367 项、9 跳过；远端 17 模块 299 项仅一个既有 8.3 短路径断言失败。未测真实模型或 QQ。

## 运行入口

`start_catty.bat` → `scripts/catty_hot_reload.py` → `bot.py` → NoneBot 插件 `src/catty_qq_ai/__init__.py`。RAG 本地模型由插件独立监督子进程，部署通道不变。

## 提示词与工具

当前配置由 `catty_config_loader.py` 映射至 `Config`；已删除失效的 `chat.system_prompt`。角色文件及既有专用提示段提供人格内容；`PromptManager` 组装静态段和动态上下文。静态分泡节奏与每轮格式提示共用启用条件，发送端分泡解析保持原行为。

工具 schema、capability 与 executor 在 `tools.py` 对齐。图片候选统一当前/引用/最近图；群个人发言从当前 scope 的滚动快照按真实发送者检索。

图片与表情接话：当前图片、引用图、上下文图、网页配图及识图文本进入共享接话提示；QQ 原生表情、表情包摘要和 Unicode 表情也可触发该提示。表情线索写入当前用户内容及近期对话，未知含义不猜；现有群聊准入和表情复读路径不变。该增量已部署并通过本地及远端回归，未测试真实模型回复。

### 回复前证据编排

`_build_cognitive_turn_context_sync` 在线程中调用证据包构建器，读取 `text` 与结构化来源诊断；失败只记录来源和异常类型。当前昵称来自真实事件的群名片/昵称，语料开关与长度上限显式传入。生产预算仍是 `scene_k=12 / book_k=0 / max_chars=12000`。

认知证据和时间线放在缓存边界后的 system 消息；自适应上下文随本轮 user 内容。重建消息列表时保留这两类内容。生产工具清单确定后追加对应 Agent 教程，无工具线路不推荐具体工具，末尾 assistant prefill 的顺序不变。

### RAG 事件循环保护（已部署）

提示构建只把 `catty_rag_recall` 放入线程，等待最多 5 秒，保持其顺序、停用开关与预算；证据编排最多等待 8 秒。两个入口共享最多 4 个在途 RAG 查询，容量不足报告来源错误；支持 `with_options` 的 embedding 客户端采用 5 秒网络超时并关闭 SDK 重试。超时取消等待但不终止线程，槽位在线程实际结束后释放；网络超时不代表总墙钟上限。2026-10-27 21:43（远端时间）推送后双端哈希一致，远端 Python 3.14.5 语法通过并重新加载插件。

### 本地 BGE 子进程（已部署并合入本地）

部署版默认用 `rag.local_bge_enabled=true` 为 RAG 单独选择 `BAAI/bge-small-zh-v1.5`，不打开全局 text2vec/NER。宿主启动后台监督线程即返回，子进程只读取本地 Hugging Face 缓存，在 CPU 上生成 512 维向量；缓存缺失或模型加载失败不下载、不切到其他向量空间。关闭开关后保留旧 OpenAI/ONNX 选择，需重启 bot 才应用此开关。

模型加载期限为 30 秒、单次推理期限为 5 秒，失败时终止对应子进程并退避重启；未就绪、忙碌和错误均不生成伪向量。关闭时回收子进程，Windows 加载期间通过父进程句柄监测；为避免已复现的 NumPy DLL 初始化死锁，stdin 读取线程在模型导入后启动。子进程环境不继承 API 密钥，模型加载禁止联网；打包为 frozen 程序时不递归启动自身，本地 RAG 报不可用。

查询后端失败以来源错误报告，不把失败冒充空命中；仅明确的 `where`/filter 类型错误才回退为无服务端过滤，并保留客户端人格过滤。回填只计实际成功写入，相同 ID 且文档及元数据未变则跳过；缺失时间戳的记录仍可能产生新 ID。QQ 复读在发送确认后才记历史，失败原因未明时不盲目重发。

### JSON 人格与上下文决策

`persona_store.py` 校验并原子保存 `data/personas/definitions/<name>.json`，`switch_commands.json` 保存完整指令映射；`personas.install_custom_personas` 将整批有效定义物化为冻结对象，保留内置对象。`persona_runtime.py` 统一工具/命令的数据操作，更新必须携带此前读取的文件 SHA。坏文件重载保留上次有效定义，部分失败如实回执。

`persona_admin.py` 解析直接管理命令；生成命令通过无工具主模型请求得到 JSON，最多一次修复，共享60秒默认截止时间。普通聊天不增加规划模型：主回复根据上下文直接填写 `catty_persona` 的定义参数。保存不自动切换，工具权限与 scope 来自真实事件。

`handle_chat` 准入后用 ContextVar 固定本轮 Persona。工具切换只保存覆盖选择，不重入已持有的聊天锁；下轮按 `name@definition_revision` 清理旧口吻历史。直接切换命令保留原来的转场锁顺序。人格目录配置变更重建注册表；手工改 JSON 后显式重载。

### 当前人格语气进化

后台调度从单个会话的近期消息筛选当前人格示例，按人格基底和现有语气请求模型生成可替换的表达扩展。`persona_evolution_store.py` 以基底摘要和文件 SHA 校验后原子发布；同人格会话共享扩展，原角色正文不改。运行配置或人格变化会使旧任务在生成前或写入前失效，写盘取消时等待实际线程结束再释放转场锁。

`persona_evolution_budget.py` 为所有人格维护独立 SQLite 日账本，按配置模型上下文与输出上限预留，凭服务商 usage 结算；未确认的请求持续占用预留。该机制依赖服务商遵守限额并准确返回 usage，最低配置额度不会自动上调。样本发送模型接口，语义提示不能保证私人信息不会进入共享扩展。详细配置与验证见 [人格运行时](persona-runtime.md)。

`request_intent.py` 在缓存边界后追加唯一的上下文意图说明；生产与模拟入口均使用 `tool_choice=auto`，不再按末句关键词强制绘图。模型结合前文、引用、补充、纠正和成功回执决定执行或回复，指代明确就沿用参数。

本轮实际开放 `catty_write_file` 或 `catty_python_run` 时，同一意图段补充可选文件交付能力；只介绍存在的工具。生成正文/源码并不要求执行 Python，短示例仍可直接回复。无工具重试刷新这一段并撤下文件能力，不改内置人格或工具权限。

### 按需技能与公共知识学习

`request_intent.py` 默认提示实质任务先查技能，未知或可能过时的事实查官方/一手正文；主回复仍自主选择工具。首轮仅有 `catty_skill_search` 入口，没有技能库目录、候选或正文。搜索命中后 `SkillTurn` 记录候选ID与SHA，下一次模型请求才增加带候选枚举的 `catty_skill_load`；模型选择后返回完整分块正文。

`execute_tool_call` 观察真实 `catty_fetch_url` 结果，合格的公共正文产生当轮不透明回执并开放 `catty_skill_save`。保存只收回执ID；独立生成请求不带聊天、画像或用户身份，在空ContextVar环境中提炼可共享知识。`skill_store.py` 用文件锁、SHA版本校验及fsync/replace写入 `data/skills/<id>.json`，所有会话共享检索。目录不入Git，保存不会重启bot；下次检索直接读磁盘新版本。

生产和模拟入口把静态工具与 `skill_tool_schemas(ctx)` 合并为 `tools_provider`；`chat_completion_with_tools` 每次模型请求前重新获取schema、允许名称并刷新自有意图提示。云端fallback保留provider及 `ToolReplyState`，deadline文字降级则不带工具/provider。动态schema同样纳入既有模型窗口预算，不预注入技能正文以占用上下文。仅有抓取回执不等于来源权威，正则筛查不保证识别所有隐私；来源判断、模型选择与线上质量仍需实测。

## 排队与历史预算

聊天锁仍串行处理同一会话。`reply_queue_max_wait_seconds` 只淘汰已过期的非定向群聊插话；已准入的私聊及 `directly_requested/mentioned/replied_to_self` 请求不会因前轮耗时而被丢弃。机机改为关键词/@/引用或已有续聊准入；其它mention-only人格仍坚持真@，广播/权限/自介守卫与两条续聊预算保持。

请求准备把历史之前的消息及工具 schema 算作前缀，加到会话软目标后再受模型窗口减输出预留的限制。历史按剩余额度保留完整轮次；若最新一轮也装不下，则该请求不带历史，不沿用持久化裁剪器的超预算末轮保留策略。本轮用户及工具后缀原样保留，磁盘历史不因请求裁剪被修改。新增 `prefix_input_tokens` 与 `effective_target_context_tokens` 诊断字段；它们是估算，不是供应商计费token。

### 已部署：判门、引用和图片

机机群聊在现有唤醒门之后，对弱唤醒或续聊调用 `reply_admission.decide_reply`；模型只判定是否接话，超时、异常或格式无效时不回复。明确指向机器人与既有特殊上下文保留原路径，普通未唤醒消息仍不入模型。线上耗时与判断质量未验证。

入口把引用文本传给 harness 检索，并拒绝明确标注为其它群的引用；缺少群元数据的引用仍依赖事件上下文。群历史、压缩摘要与图片标注发言者，旧记录无法确定时标为未知。相同 URL 的不同发送消息保留各自来源，图片工具的精确引用返回单一消息来源，URL 返回所有匹配来源。

主视觉线路相同时，当前、引用和相关上下文图片分别标注后作为多模态输入传给主模型。可能的后发配图只等待同群同一发送者，未明确回指旧图时不因此附带旧图；读取失败明确标注，不凭 URL 假装看见。默认推理为 `max`，生产群聊小时额度为每用户跨群3000万。

## 执行与恢复

### 猫猫戳戳

真实戳戳事件沿用原有目标、权限、概率和每用户冷却门。猫猫人格下将该用户的关系及当前会话中最多6条近期相关消息传给单次模型回复；12秒超时、失败、空结果或无密钥时使用旧池兜底。结果限制为一条短文本，普通用户的「主人」称呼被过滤；发送成功后才写入该用户的近期对话记录。机机的戳戳禁用保持不变。该路径已部署，真实模型和QQ未验证。

### 机机主动群聊（本地增量）

按既有薄入口、纯构造、独立运行时和明确回执分层，不在 `__init__.py` 再嵌大循环：

- 入口用 `_group_persona_trigger_allowed` 统一主回复、签到积分和复读让位；`_extract_trigger_incoming` 只为机机保留裸唤醒词及已有窗口的普通文本。消息抽取扩展默认关闭。主回复在原 `AsyncExitStack` 登记/释放群忙碌计数，不改变锁顺序或并发容量。
- `fdj_proactive.py` 负责每群/UTC+8日计划、活动许可、原子claim与sent/failed/unknown。每个root由一个bot进程拥有，多实例由进程内锁协调；I/O经runtime线程执行。
- `fdj_proactive_prompt.py` 使用当前人格核心、本群会话段及已渲染的节奏模板，复用统一上下文指导，再限定本次只是主动闲聊。当前群JSON背景最多4000字符，无工具schema，不执行历史任务。
- `fdj_proactive_runtime.FdjProactiveRuntime` 通过显式 `BubbleHost` 注入宿主，不导入插件入口；协调5分钟确认群缓存、活动/人格快照、生成/发送各自限时与回执。每tick最多尝试一群，活跃优先、同层随机；正常新增真人消息允许插嘴，新增bot回复或上下文整批过期则让位。
- 实际发送前再查作用域、开关、权限、人格、群忙碌、活动许可、slot及日期。先durable claim，再单次安全文本发送；复用 `ReplyDeliveryState`，有效ACK才写recent ledger，不伪造用户turn或给全群开续聊。发送已调用后的异常/取消/超时/畸形ACK均unknown，不重试。
- `proactive_legacy.py` 接管旧猫娘循环编排，入口薄wrapper提供原候选、额度、发送和错误回调；原prompt/MemoryStore语义不变，新开关不启用旧链路，不保留重复旧loop。

配置与时段见 [机机说明](fadianji-proactive.md)，认领与恢复见 [调度合同](fadianji-proactive-scheduler.md)。该增量已部署到 `D:/CattyQQAI`，28文件双端一致，推送后插件重载并重连。

`_main_reply_with_deadline` 为一轮回复创建 `ToolReplyState`。工具完成时立即写入恢复快照，完整保留 assistant/tool 对应关系；主模型收尾超时后使用回执纯文本重试，不重做副作用。未取得结果的工具明确为未知状态。

生图只在线等待短窗口，慢结果交给后台投递；在线等待被取消时仍为受保护的生成任务登记投递所有者。

## 文件产物

- `code_sandbox.py`：会话+用户身份 → 成功工作副本 → 独立 run → 成功产物快照 `exports/<run_id>`。失败产物只用于诊断；packages 共享，普通运行不能改写。
- `file_formats.py`：文本 → 真 DOCX/XLSX/PDF 或明确的 UTF-8 原文 → fsync/原子暂存。格式依赖在 pyproject 中声明。
- `tools.py`：成功暂存文件进入 `pending_file_deliveries`；公共入口在文字结束后按原会话上传。`staged` 不是上传成功。

## 发送确认

`reply_delivery.py` 为主回复记录 pending / delivered / unconfirmed。发送确认后才更新好感与续聊；处理器退出时仅把已确认气泡提交至会话历史。后台图片/文件交付仍使用独立任务，不是跨重启持久化队列。

已部署并合入本地的腿图发送路径在文字发送返回后记录已确认片段，图片发送返回后才记录图片；取消或部分失败仍保留此前已确认文字，冷却和现有重试行为不变。QQ NT `result=120` 的具体原因未知。

## 验证

`scripts/run_offline_regressions.py` 仅运行已审查清单，逐模块独立进程。原工作区开工基线58模块989项：979通过、9跳过、1项 persona_admin 浮点失败；机机主动冒泡增量已部署，见会话08。

本轮部署的摘要路径由构造快照绑定输入和存储实例，提交时只消费已选语料；发言者来源随群召回保留。同名历史画像只用于口吻。本地扩充清单64模块1154项，0失败/错误、9跳过；34文件已推送并校验，并已并入本地工作区，`src/tests/scripts` 356个 Python 文件语法通过。真实模型与QQ效果未专项评测。
