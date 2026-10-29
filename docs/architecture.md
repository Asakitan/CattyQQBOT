# Catty 工具与回复交付架构

## 语音消息转文字（已部署）

QQ 客户端的转文字结果不经过 OneBot；入口在 `_rule` 里用 `_extract_incoming_with_voice` 抽取消息，发现 `record` 段就调用 `voice_stt.transcribe_event`：`url` 下载 / 本地路径 / `get_record` 三选一取音频，嗅探格式后写入 `data/voice_inbox/`，再由独立 `voice_stt_worker.py` 用 faster-whisper（CT2 int8）转写，PyAV 自行解码到 16k 单声道。结果以 `【语音转写】` 并回 `incoming.text/history_content`，纯语音消息因此能进入准入和主模型；失败只给“未能识别内容”，SILK 直接跳过，同一音频缓存 10 分钟。

## 调情短语音（已部署）

`catty_voice_say` 与其它工具一样由模型自行决定何时调用，schema 明确限定“对方在调情/暧昧/撒娇时偶尔一条”，普通问答不适用。工具对所有人格暴露，音色按人格解析：`voice.default`（默认 `michelle`）兜底，`voice.persona_voices` 覆盖（默认把 `fadianji` 指到机机音色）。执行器再走 `voice_reply.reserve_slot`：每会话 10 秒冷却 + 概率门（默认 0.6）；主人（`catty_owner_qq`）跳过冷却与概率，但仍受“本轮 1 条”限制。

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

语音情感：`catty_voice_say` 的 `segments`（1-4 段，每段 `text`+`emotion`）由模型判断语气。此前部署版使用模型合成的语音库拼接参考音；现可用 `voice.profiles.<音色>.emotion_reference_dir` 指定原始录音，读取标注来源的 `bank.json`，只选择单条 3–10 秒、单声道 PCM16 且有对应文本的录音。声明来自录音不等于已验证听感或转写。

未配置或没有可用情绪录音时，worker 使用该音色配置的原始参考音，合并分段并一次生成完整句；此时不保证各情绪标签产生明显语气变化。请求的情绪至少有一条可用原始录音时才逐段合成，缺失的情绪回退该音色的默认参考音；`bank` 后端保持原有语音库行为。生产两套音色均未配置情绪录音；参考音目录变化后需重启语音 worker。详见 [语音候选](voice-candidates.md)。

去重：`_voice_text_key` 去掉标点和空白后比较；`catty_voice_say` 在调用时就把参数里的 `text`/`segments` 记进本轮已发声集合。语音条遇到已经念过的气泡只标记 `already_voiced`（不发文字、不重复合成），后台语音到达时同样按这个集合跳过重复台词；同一句用不同标点写出也算同一句。

图片与表情：vision 描述以“仅作背景，不要复述；除非用户明确问图里是什么”注入；`build_image_literacy_prompt` 明确禁止用“这表情/这图/图中/看起来”开头、禁止念表情包配字，要求把图的情绪当作对方当下的态度直接接话。

## 本地语音候选（尚未接入运行时）

`tmp/voice-training-20261028-01/` 独立 Python 3.11 环境和 GPT-SoVITS V2 副本负责双音色训练，权重、参考音与试听落在 `models/voice/{michelle,fadianji}/`。双音色远端 CPU worker 及参考音修复已部署；本轮试听候选仅在本地，真实 QQ 听感待验证。

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

### 日志驱动的源码修复（已部署并启用）

独立包 `src/catty_self_repair/` 不导入 NoneBot 插件，由既有守护进程监督。修复模型仅操作 `data/self_repair/` 中的源码候选；周账本、日志游标、固定校验、发布备份与启动健康检查各自独立。主机器人退出不影响修复任务，实际源码替换由守护在停止机器人后协调，未完成的发布事务先恢复再启动。配置与范围见 [自修复说明](self-repair.md)。

日志事件经过脱敏、合并、去重和冷却进入独立的 Chat Completions / Responses 工具循环。修复模型缺省时继承主 AI 的模型、端点、密钥和代理；显式修复模型不混用路由。UTC+8 自然周预算上限一亿 token，每次请求先预留、收到用量后结算。可信验证器对冻结基线和候选分别运行固定测试，按测试身份与结果比较；验证成功后 worker 写 `ready.json`，守护复核配置和全量源码哈希再停机应用。

`bot.py` 启动钩子记录当前 PID、插件加载状态和时间，监督器同时检查心跳并观察连续健康窗口。Windows 虚拟环境启动器可能产生真实解释器子进程，监督器通过系统进程快照核对它是被启动进程的直接子进程；不接受无关 PID。启动失败、进程退出、心跳失效或观察期源码漂移均回滚；与人工编辑冲突时保留事务和原件并停止启动。源码副本及 Python 审计钩子不构成操作系统安全沙箱。

已分阶段部署 24 个文件并核对 SHA-256；生产和本地已启用，配置模板默认仍关闭。新守护已加载，插件标记、父子 PID、心跳与 OneBot TCP 连接通过。生产真实日志触发两次处理，一次无改动、一次连接中断，均未发布；真实源码修复效果尚未验证。

### Responses WebSocket 主 / 识图线路（已部署）

主模型与识图可走 `wss://api.openai.com/v1/responses`：`responses_ws.py` 维护一个持久连接，发送 `response.create` 帧并按 `stream_id` 把事件路由到对应 lane。续聊用 `previous_response_id` + 只发新增 input items；`store=false` 时服务端只在连接本地内存缓存上一轮，连接换代后本地缓存里的 response id 失效，自动改为完整 input 重放。`data/responses_ws_cache.json` 保存每个 scope 的 response id、已覆盖 input/output item 指纹和 connection generation，TTL 过期或 generation 不匹配即弃用。连接断开、`previous_response_not_found` 或 50 分钟主动换连接都会清缓存并重放一次完整上下文；仍失败时按 `ai.websocket_fallback_http` 回退 HTTP Responses/chat。

未完成请求取消或超时会关闭连接，避免旧事件进入下一轮；连接标识跨实例唯一，失效续聊先释放 lane 再重放。请求侧把 `reasoning_effort` 转成 `reasoning.effort`（`max` 映射为 `high`），识图 URL 使用字符串。NSFW spark 仍走 DeepSeek，普通生图用 OpenAI；自动 NSFW 生图可用 `imagegen.nsfw_backend` 与 `imagegen.pixelbunny.base_url/api_key` 独立保留 Pixelbunny。

已部署的缓存修复将官方 OpenAI GPT-5.6 及以上请求的稳定系统前缀转换为 developer 输入，并在既有标记处设置显式断点；后续系统段保留原有位置，隐式断点继续启用。无标记及其他兼容端点保持原转换。Responses 用量透传读取、写入及期限明细，面板将写入与普通输入分开；缓存命中仍依赖前缀和工具一致、有效期限及服务端路由。部署后生产代码的 WebSocket 合成探针首轮写入 9625、续聊次轮读取 9625 token；真实 QQ 回合未测。

历史缓存增量已部署：`_mark_responses_history_cache` 在最近两条历史 user 的最后一个受支持输入内容块添加显式断点，排除当前 user；连同静态断点共三个显式写入位置，保留一个隐式写入位置。当前 user 的动态上下文不会原样保留到下一轮，仅在本轮末尾写入不足以缓存此前共享历史；两个滚动端点让相邻请求有重叠的历史写入边界。消息正文、角色和顺序不变，也不延长 TTL。`responses_ws.item_hash` 仅剥离协议输入块上的 `prompt_cache_breakpoint` 元数据，避免标记移动破坏本可续传的消息；正文、图片、工具参数与动态状态变化仍严格拒绝错误续聊。52 项定向回归通过，线上命中提升尚未验证。

生产机本地 SOCKS `127.0.0.1:11080` 由启动任务维持，加密隧道接跳板 TCP 18080；OpenAI 与 NovelAI 域名经隧道，其他流量直连。`vision.image_download_direct` 默认关闭，生产显式开启后只让图片资源绕过 API 代理，仍逐跳校验公网 IP、固定连接地址并禁止环境代理。真实 QQ 公共头像下载与 Terra WebSocket 识图、两轮增量续聊及 NAI 鉴权通过；生产实际出图、QQ 会话端到端与长期缓存命中未验证。单连接的 named lane 数量上限可能触发 HTTP 回退。

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

部署版默认用 `rag.local_bge_enabled=true` 为 RAG 单独选择 `rag.local_bge_model`（默认 `BAAI/bge-small-zh-v1.5`），不打开全局 text2vec/NER；模型 id 经 `CATTY_RAG_MODEL` 传给隔离 worker。宿主启动后台监督线程即返回，子进程只读取本地 Hugging Face 缓存，在 CPU 上生成 512 维向量；缓存缺失或模型加载失败不下载、不切到其他向量空间。关闭开关后保留旧 OpenAI/ONNX 选择，OpenAI embedding 模型名读 `rag.embedding_model`；需重启 bot 才应用此开关。

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
