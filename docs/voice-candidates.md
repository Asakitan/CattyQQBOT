# 本地语音候选

## 模型与范围

`models/voice/michelle/` 和 `models/voice/fadianji/` 各保存 GPT、SoVITS 权重、参考 WAV、参考文本、试听和清单。训练使用 GPT-SoVITS V2（`b4570b876f5c086819ba84e3215472733c6a7b70`），SoVITS 8 轮、GPT 15 轮。米雪儿 152 条、993.87 秒；发电机 109 条、299.7 秒，自动转写尚未人工校对。

模型和实验目录被 Git 忽略；独立 Python 3.11 环境位于 `tmp/voice-training-20261028-01/.venv/`。训练权重未部署到服务器；运行时使用的是下面的离线语音库，远端 CPU 合成未启用。

## 运行时语音库（已部署）

`src/catty_qq_ai/data/voice_bank/michelle/` 与 `.../fadianji/` 各存 9 句离线语音和 `bank.json`：害羞 2、撩 2、温柔 2、开心 1、傲娇 2。米雪儿用其 8 轮 SoVITS 与 BWIKI 参考音，机机用主人选定的 4 轮 SoVITS 与 `candidates/reference-3d` 参考音，均为 32kHz 单声道，逐句做过自动转写记录（短句识别会把“这么”听成“这个”，不能当硬门）。

`catty_voice_say` 对所有人格暴露；默认音色 `michelle`，`voice.persona_voices` 可把某人格指到别的音色（默认 `{"fadianji": "fadianji"}`）。每会话 5 分钟冷却、默认 0.6 概率，主人跳过冷却与概率，所有人每轮最多 1 条。

`voice.backend=worker` 时由独立 GPT-SoVITS 子进程念模型自己写的短句，按 `voice.profiles.<音色>` 取 gpt/sovits/参考音；启动握手、240 秒超时、4200MiB 内存门槛、空闲 180 秒卸载、关闭回收，未配置或失败自动退回语音库。切换音色会先收掉旧 worker 再判断内存，避免两个模型同时驻留。

## 远端合成环境（2026-10-29 部署）

- 独立 venv：`D:/CattyQQAI/tmp/voice-runtime/.venv`（Python 3.14.5，torch 2.11.0+cpu、torchaudio 2.11.0+cpu、transformers 4.57.6、librosa 1.0.0、numba 0.68.1、onnxruntime 1.31.1、peft 0.21.2、pytorch-lightning 2.6.6、matplotlib、fast-langdetect、split-lang、resampy、soundfile）。
- 预训练：`tmp/voice-runtime/pretrained/` 下 BERT（fp16，621MiB）与 HuBERT（fp16，180MiB），从 hf-mirror 下载后重存为 fp16，与训练所用本地副本精度一致；`fast_langdetect/lid.176.bin` 125MiB 随框架目录部署。G2PW 已装到 `GPT_SoVITS/text/G2PWModel/`：605.79MiB `g2pW.onnx` 由两段分片合并，SHA-256 `2eb3c71f…` 与本地训练副本一致，另补 `opencc-python-reimplemented`。两套训练权重与参考音在 `models/voice/{michelle,fadianji}/`。
- 框架适配（`tmp/.../gpt-sovits-source/...`，非仓库文件）：`is_g2pw` 恢复为环境变量开关（仅缺模型时自动关闭并回退 pypinyin）；`chinese.py`/`chinese2.py`/`tone_sandhi.py` 在无 `jieba_fast` 时回退纯 Python `jieba`；`TTS.py` 用 soundfile 读取参考音，绕开 torchaudio 2.11 的 torchcodec 依赖。本地副本保留同一补丁。
- 远端实测（G2PW 启用）：米雪儿 3.27–3.40 秒、机机 3.96–4.72 秒（各含一次模型加载）；worker 峰值工作集 3961 MiB，空闲卸载后可用内存 5.88 GiB。
- 真实 QQ 语音播放、真机听感与长句稳定性尚未验证。
- 过程播报会从出站文本里删掉（“语音正在生成/稍后发出来/马上发给你语音”等整句）；只剩播报或没写文字时整轮不发文字，语音单独到达。`catty_voice_say` 的回执也只说“已排队”，不含生成/稍后/后台字样，减少模型复读。语音在工具调用时就发出去，文字后跟或不跟；只有 worker 冷启动超过内联等待时，才在文字之后补发。
- 被问“你喜欢我吗/想不想我/爱不爱我”这类表白试探时，schema 建议模型优先用语音回答、可只发语音；没有关键词硬分支，是否发声由模型自己判断。

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