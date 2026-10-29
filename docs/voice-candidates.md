# 本地语音候选

## 模型与范围

`models/voice/michelle/` 和 `models/voice/fadianji/` 各保存 GPT、SoVITS 权重、参考 WAV、参考文本、试听和清单。训练使用 GPT-SoVITS V2（`b4570b876f5c086819ba84e3215472733c6a7b70`），SoVITS 8 轮、GPT 15 轮。米雪儿 152 条、993.87 秒；发电机 109 条、299.7 秒，自动转写尚未人工校对。

模型和实验目录被 Git 忽略；独立 Python 3.11 环境位于 `tmp/voice-training-20261028-01/.venv/`。双音色权重和 CPU worker 此前已部署；本轮参考音修复已部署，A/B 试听和替代参考音候选只在本地。

## 运行时语音库（已部署）

`src/catty_qq_ai/data/voice_bank/michelle/` 与 `.../fadianji/` 各存 9 句离线语音和 `bank.json`：害羞 2、撩 2、温柔 2、开心 1、傲娇 2。米雪儿用其 8 轮 SoVITS 与 BWIKI 参考音，机机用主人选定的 4 轮 SoVITS 与 `candidates/reference-3d` 参考音，均为 32kHz 单声道，逐句做过自动转写记录（短句识别会把“这么”听成“这个”，不能当硬门）。

`catty_voice_say` 对所有人格暴露；默认音色 `michelle`，`voice.persona_voices` 可把某人格指到别的音色（默认 `{"fadianji": "fadianji"}`）。每会话 10 秒冷却、默认 0.6 概率，主人跳过冷却与概率，所有人每轮最多 1 条。

已部署：用户明确要求发语音或用语音回复时优先调用语音工具，跳过随机概率但保留群/私聊冷却和主人原有豁免；模型没有调用工具时，合格短句也可转语音条。否定、用户自己发语音和询问操作的常见说法不会触发。合成或发送失败时回落文字，后台待发不冒充已发声；慢合成时相同文字可先发、语音后到。识别规则不保证覆盖所有表达，真实 QQ 与在线模型效果尚未验证。

`voice.backend=worker` 时由独立 GPT-SoVITS 子进程念模型自己写的短句，按 `voice.profiles.<音色>` 取 gpt/sovits/参考音；启动握手、240 秒超时、4200MiB 内存门槛、空闲 180 秒卸载、关闭回收，未配置或失败自动退回语音库。切换音色会先收掉旧 worker 再判断内存，避免两个模型同时驻留。

## 语音消息转文字（已部署）

QQ 客户端的语音转文字结果不会通过 OneBot 下发；bot 自己取音频后转写。`message_utils.extract_voice_segments` 收集 `record` 段，`voice_stt.transcribe_event` 依次尝试：`url` 下载 → 本地 `file` 路径 → `get_record`（`out_format=wav`）。取到字节后按魔数嗅探格式，写入 `data/voice_inbox/`，再调用 `voice_stt_worker.py`（独立进程，faster-whisper 1.2.1 + ctranslate2 4.8.4 + PyAV 19，模型 `tmp/voice-training-20261028-01/faster-whisper-small`）。

解码由 worker 自己用 PyAV 重采样到 16k 单声道（faster-whisper 1.2 的 `av.open(metadata_errors=…)` 与 av 19 不兼容）。成功时把 `【语音转写】…` 并回文本与历史，纯语音消息也能进入主模型；失败只写“未能识别内容”。SILK 直接跳过不喂给 whisper；相同音频按 SHA-1 缓存 10 分钟。远端两段真实语音（4.4 秒、3.4 秒）返回正确文本；真实 QQ 语音的编码格式与 `get_record` 行为未实测。

## 远端合成环境（2026-10-29 部署）

- 独立 venv：`D:/CattyQQAI/tmp/voice-runtime/.venv`（Python 3.14.5，torch 2.11.0+cpu、torchaudio 2.11.0+cpu、transformers 4.57.6、librosa 1.0.0、numba 0.68.1、onnxruntime 1.31.1、peft 0.21.2、pytorch-lightning 2.6.6、matplotlib、fast-langdetect、split-lang、resampy、soundfile）。
- 预训练：`tmp/voice-runtime/pretrained/` 下 BERT（fp16，621MiB）与 HuBERT（fp16，180MiB），从 hf-mirror 下载后重存为 fp16，与训练所用本地副本精度一致；`fast_langdetect/lid.176.bin` 125MiB 随框架目录部署。G2PW 已装到 `GPT_SoVITS/text/G2PWModel/`：605.79MiB `g2pW.onnx` 由两段分片合并，SHA-256 `2eb3c71f…` 与本地训练副本一致，另补 `opencc-python-reimplemented`。两套训练权重与参考音在 `models/voice/{michelle,fadianji}/`。
- 框架适配（`tmp/.../gpt-sovits-source/...`，非仓库文件）：`is_g2pw` 恢复为环境变量开关（仅缺模型时自动关闭并回退 pypinyin）；`chinese.py`/`chinese2.py`/`tone_sandhi.py` 在无 `jieba_fast` 时回退纯 Python `jieba`；`TTS.py` 用 soundfile 读取参考音，绕开 torchaudio 2.11 的 torchcodec 依赖。本地副本保留同一补丁。
- 远端实测（G2PW 启用）：米雪儿 3.27–3.40 秒、机机 3.96–4.72 秒（各含一次模型加载）；worker 峰值工作集 3961 MiB，空闲卸载后可用内存 5.88 GiB。
- 真实 QQ 语音播放、真机听感与长句稳定性尚未验证。
- 过程播报会从出站文本里删掉（“语音正在生成/稍后发出来/马上发给你语音”等整句）；只剩播报或没写文字时整轮不发文字，语音单独到达。`catty_voice_say` 的回执也只说“已排队”，不含生成/稍后/后台字样，减少模型复读。语音在工具调用时就发出去，文字后跟或不跟；只有 worker 冷启动超过内联等待时，才在文字之后补发。
- 被问“你喜欢我吗/想不想我/爱不爱我”这类表白试探时，schema 建议优先用语音回答；现有表白识别也能直接触发语音条。
- 自动语音：每条消息额外抽一次 `voice.auto_chance`（默认 0.1），命中且本轮回复不超过 50 字、无代码块/公式时先发一句现成语音再发文字；长回复和技术排版不触发，模型已自己调过语音时不重复。
- 语音条模式：多气泡回复可逐条念成语音条。`voice.only_hours`（如 `23:00-01:00`，支持跨夜）窗口内一直用语音条，窗口外只念一条；单条上限 `bubble_max_chars=40`、每轮 `bubble_max_per_turn=3`、合成预算 `bubble_budget_seconds=45`。NSFW 回合按 `nsfw_bubble_chance`（默认 0.5）抽取，最多 3 条，只念台词、剥掉括号/【】动作旁白；语音气泡按已送达记进历史，发送失败改回文字。
- 语音情感：此前部署版用合成语音库拼接情绪参考音。现不再复用合成素材；无可用原始情绪录音时，`segments` 合为完整句并使用该音色原始参考音，语气随句子自然生成，但各情绪标签不保证可听到区别。分段只在 `worker` 后端生效，总字数仍受 `max_chars` 限制。

## 双音色参考音修复（已部署，试听待确认）

两套 `bank.json` 均注明片段来自本地合成，部分自动转写与原文不一致，复用它们可能放大伪影；听感下降是否完全由此造成仍需验证。生产代码默认使用各音色现有原始参考音，并保留 0.25 秒尾部静音和 8ms 淡出，不改变模型权重或合成参数。生产两套音色均未配置原始情绪录音；远端语音 38、配置 33、工具 22 项及语法检查通过，真实 QQ 听感未测。

`tmp/voice-quality-20261029-reference/` 包含两种音色的 `*-before.wav`（此前合成参考音）与 `*-after.wav`（原始参考音）。每组使用相同文本、权重和随机种子并对齐响度，4 个文件均为 32kHz 单声道、无削波；这些检查不等于听感验收。本地真实 worker 的两次探针均确认使用原始参考音，并将分段请求作为一次完整句生成。

离线 ASR 将米雪儿现用原始参考的试听末尾识别为“开箱”；另用 `prepared/michelle/wavs/0002.wav`、`0005.wav`、`0008.wav` 的原始录音生成三个 `michelle-original-*.wav`，均识别出完整目标句。三个候选的参考文本来自语料清单，尚需人工校对；ASR 不能判断音色或电音，也不据此自动替换现有配置。

如需恢复逐段情绪参考，在 `voice.profiles.<音色>.emotion_reference_dir` 指定专用目录，放入来源明确、文本与内容一致的原始录音及 `bank.json`：`source` 为 `original recordings`，`entries` 每项包含 `file`、`mood` 和 `text`。每条参考音须为单声道 PCM16 WAV、3–10 秒，worker 选用一条，不拼接多条；没有合格录音的情绪回退默认参考音。录音与转写仍需人工核验，目录变动后需重启 worker。

## 发电机参考音与局部电音

用户反馈“今天也要开心”处有电音。本轮按上一版 `fadianji/candidates/reference-3d/sample.wav` 排查；如果实际听的是根目录旧 `sample.wav`，应回到对应基线，不能把本轮对照结论直接套用。

参考候选使用公开视频 `BV1fVT161EE7` 的 713.07 秒附近片段，文本“只掉一个的话还可以，是的。”。该参考文本来自自动转写，权重未变。原始训练语料存在音量和录音条件差异、短片段及错词；这些是待改进项，并非已证实的局部电音根因。

`tmp/voice-quality-20261028-01/phrase_ab.py` 固定权重、参考音和目标句，在独立进程中分别生成以下样本。每次只改表中的一个输入，保留原始波形；试听统一线性 RMS 并限制峰值，未做降噪、滤波或压缩。

| 文件（`models/voice/fadianji/candidates/phrase-ab/`） | 唯一变化 | 时长 | 自动转写字符相似度 |
| --- | --- | --- | --- |
| `01-control.wav` | 无：`cut5`、seed 42、temperature 1 | 3.72 秒 | 0.923 |
| `02-whole-sentence.wav` | `text_split_method=cut0` | 3.62 秒 | 0.923 |
| `03-seed7.wav` | `seed=7` | 4.04 秒 | 1.000 |
| `04-temperature07.wav` | `temperature=0.7` | 4.68 秒 | 0.923 |

四组均为 32kHz 单声道，统一 RMS 约 0.108591，零削波；原设置的原始 WAV 哈希精确复现上一轮 `other-reference`。`cut5` 实际将“今天也要开心哦。”单独作为第二段；`cut0` 是整句，并保留上游预处理增加的开头句号。完整输入、实际分句、权重/参考/音频哈希及 ASR 结果在 `comparison.json`，原始生成日志位于 `tmp/voice-quality-20261028-01/phrase-ab/`。

ASR 只用于检查发音内容，等 RMS 只用于减轻音量对比较的影响；二者都不证明电音消失。用户确认 `02-whole-sentence.wav` 好一点，但仍需加强，整句版成为下一轮基线，原参考音、权重和试听保持。

## 整句版参数对照（调参版已淘汰）

`whole_refine.py` 沿用同一文本、权重、参考音和 `cut0`。V2 的 `vits_model.decode` 默认 `noise_scale=0.5`；实验只在当前实例的调用中改这个参数，不修改框架文件，不用后期降噪掩盖伪影。

| 文件（`models/voice/fadianji/candidates/whole-refine/`） | 相对整句基线变化 | 时长 |
| --- | --- | --- |
| `01-whole-control.wav` | 无，提供同响度对照 | 3.62 秒 |
| `02-whole-low-noise.wav` | 解码 `noise_scale=0.25` | 3.62 秒 |
| `03-whole-temperature08.wav` | 采样 `temperature=0.8` | 4.22 秒 |
| `04-whole-seed7.wav` | `seed=7` | 3.90 秒 |
| `05-whole-low-noise-temperature08.wav` | 低噪声 + 低温组合 | 4.22 秒 |

五组统一 RMS 约 0.103174、32kHz 单声道、零削波，ASR 字符相似度均 0.923。02 对 01、05 对 03 的语义 token、音素、解码前随机状态哈希一致，实际 `noise_scale` 不同且波形变化；这是参数生效证据，不是音质改善结论。

新控制与旧 02 输入、时长相同，115840 个采样点中 52 个相差一个 PCM16 量化单位；严格字节复现检查先退出 1，保存在临时目录 `whole-refine/validation-first-attempt.json`。续验复用全部波形，仅接受最大一个量化单位且差异点占比不超过 0.1% 的有界差异，`control_reproduces_previous_raw` 仍如实为 false。`feedback.json` 保存用户偏好与差异明细，`comparison.json` 保存参数、哈希和识别结果。

2026-10-29 用户听评：01前半段比旧整句02好，后半段仍有电音，其他版本全是电音。保留01作为诊断基线，低噪声、低温、换种子及组合方案淘汰。01与旧02几乎是相同波形，交付响度不同，不能把这次偏好当作模型本身改善的证据。

## 固定语义的声学检查点对照

`acoustic_ab.py` 从01捕获一次语义 token、音素、参考频谱与解码前 RNG，保存为临时目录的 `fixed-inputs.pt`。三次直接解码均重新读取该快照并核验张量哈希，保持 `noise_scale=0.5`、速度1和末尾0.3秒静音；只加载不同 SoVITS 检查点，不重新生成语义序列，也不重新训练。

| 文件（`models/voice/fadianji/candidates/acoustic-ab-20261029/`） | 声学权重 | 用途 |
| --- | --- | --- |
| `01-current-e8.wav` | 当前 `fadianji_e8_s440` | 同响度基线 |
| `02-earlier-e4.wav` | 早期 `fadianji_e4_s220` | 用户确认略好，当前首选候选 |
| `03-pretrained-diagnostic.wav` | 预训练 `s2G2333k` | 检查微调声学权重影响，可能改变音色，仅用于诊断 |

三组均3.62秒、32kHz单声道、统一RMS约0.099358、零削波。捕获波形与01的PCM逐采样一致，当前8轮直接重放通过有界PCM检查；三组输入张量哈希一致。所有权重文件和框架源码/共享推理配置哈希保持，新推理配置写入实验独立目录。完整参数与回执在 `comparison.json`，日志和原始波形在 `tmp/voice-quality-20261028-01/acoustic-ab-20261029/`。

用户反馈“第二个好一点”，经澄清明确指文件名 `02-earlier-e4.wav`，不是此前列表第二项的底模03。`selection.json` 锁定4轮权重及样本哈希，作为后续候选基线；本次只记录选择，没有重训、覆盖原8轮模型或启动新一轮生成。

该听评支持在本句固定输入下优先4轮，尚不能证明电音完全消失或训练过拟合。重放所用语义序列来自原01路径，后续需要验证4轮声学模型的端到端多句生成和音色一致性；不能把当前选择文件当成已经验收的运行时配置。
