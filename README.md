# Catty QQ AI

一个基于 NoneBot2 + OneBot v11 的 QQ 聊天 AI 插件，调用 OpenAI-compatible `/v1/chat/completions` 接口。

## 架构

- QQ 协议端：NapCatQQ，负责登录 QQ 号并提供 OneBot v11 事件/接口。
- Bot 框架：NoneBot2，负责接收 QQ 消息和加载插件。
- AI 接口：任何兼容 OpenAI Chat Completions 的服务，例如 OpenAI、OpenRouter、本地 vLLM、Ollama OpenAI 兼容端点等。

> 提醒：QQ 第三方协议端可能有账号风控或平台条款风险。建议先用小号测试，不要做刷屏、骚扰、群发广告等行为。

## 安装

建议使用 Python 3.10+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .
```

配置文件已经放在项目根目录：

```powershell
notepad config.json
```

至少填写 `ai.api_key`、`ai.base_url`、`ai.model`：

```json
{
  "ai": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "你的 API Key",
    "model": "gpt-4o-mini"
  }
}
```

如果你接的是本地或第三方 OpenAI-compatible 服务，只要把 `ai.base_url` 改成它的 `/v1` 根地址即可，例如：

```json
{
  "ai": {
    "base_url": "http://127.0.0.1:8000/v1",
    "api_key": "local-api-key-or-empty-if-your-server-allows",
    "model": "qwen2.5-7b-instruct"
  }
}
```

## 启动 NoneBot

```powershell
python bot.py
```

默认监听：

```text
127.0.0.1:8080
```

## 接入 QQ 号

1. 项目已经带了 NapCat Windows 一键包，位置是 `tools/napcat-onekey`。
2. 首次使用可以先运行 `tools/napcat-onekey/NapCatInstaller.exe` 完成初始化。
3. 登录你准备作为机器人的 QQ 号。
4. 在 NapCatQQ 的 OneBot v11 网络配置里启用反向 WebSocket。
5. 将反向 WebSocket 地址填成：

```text
ws://127.0.0.1:8080/onebot/v11/
```

NoneBot OneBot 适配器文档也推荐反向 WebSocket，并要求 NoneBot 使用 `ReverseDriver`。本项目 `config.json` 已经配置了 FastAPI ASGI 驱动。

`config.json` 里的 `qq` 区域可以记录 QQ 相关连接信息：

```json
{
  "qq": {
    "account": "你的机器人 QQ 号",
    "onebot_reverse_ws_url": "ws://127.0.0.1:8080/onebot/v11/",
    "napcat_webui_url": "http://127.0.0.1:6099",
    "napcat_access_token": "",
    "auto_start_napcat": true,
    "napcat_workdir": "tools/napcat-onekey/bootmain",
    "napcat_executable": "NapCatWinBootMain.exe",
    "napcat_args": [],
    "napcat_new_console": true,
    "skip_if_napcat_running": true
  }
}
```

QQ 的扫码/密码登录仍然由 NapCatQQ 完成；这个程序只连接 OneBot，不建议把 QQ 密码写进配置文件。

如果 `napcat_args` 留空，并且 `qq.account` 填了 QQ 号，程序会把这个 QQ 号作为启动参数传给 NapCat。想自己手动开 NapCat，就把 `auto_start_napcat` 改成 `false`。

更新 NapCat 一键包：

```powershell
.\scripts\update_napcat_onekey.ps1
```

## 怎么聊天

私聊默认直接回复：

```text
你好，介绍一下你自己
```

群聊默认需要艾特机器人、使用触发前缀，或者消息里带有指向词：

```text
@机器人 帮我写一段 Python
@机器人
ai 帮我总结这段话
猫猫 今天天气适合写代码吗
你看看这张图
```

艾特、回复机器人消息、拍了拍机器人或触发开头前缀时会直接回复；如果句子中出现“猫猫/笨猫/你”等配置里的名字或指向词，程序会把这类软触发消息交给 AI，由 AI 根据整句主语、称呼对象和上下文判断是不是在呼唤机器人。明显像“猫猫你看看”“问猫猫这个怎么弄”的直接喊名会使用更高的 `chat.direct_address_reply_probability`，普通软触发使用 `chat.soft_directed_reply_probability`；当群聊/私聊已经积累足够语料、摘要或画像时，还会按 `memory.reply_boost_*` 给回复倾向加一点权重。普通群聊会按群攒到 `filter.group_batch_messages` 条，或距离本群上一批普通群消息达到 `filter.group_batch_seconds` 秒后，把这段最近未 filter 的普通消息作为压缩窗口交给 AI，从中查找疑似指向 BOT/AI/猫猫的话题再决定是否自然回复；如果不该回复，AI 会输出内部不回复标记且不会发到群里。

如果群友在短时间内连续发送同一个 QQ 表情、普通图片、图片类表情或文字消息，默认第 2 次时机器人会直接复读这条消息，不调用 AI 接口；机器人自己的消息不会计入连发判断。AI 的普通文本回复会由 `filter` 小模型判断是否追加本轮专用的轻量分段提示，再由主模型按语义决定是否拆成两条消息发送；原始 `system_prompt` 不会被改写。

清空当前会话上下文：

```text
ai reset
ai 清空上下文
```

查看当前群/私聊已存储的记忆和人物信息：

```text
ai 查看记忆
ai 查看人物信息
```

清空当前群/私聊的待压缩缓存，同时保留已经压缩好的长期摘要和人物画像：

```text
ai 清空缓存
ai 清空记忆缓存
```

联网搜索：群友明确要求“联网搜索/上网查/搜一下/查一下”等时，机器人会抓取一轮网页搜索结果再交给主 AI 回答。普通用户每 10 分钟 1 次；在 `memory.user_titles` 或 `memory.group_user_titles` 里配置了称呼的用户不受冷却限制。冷却未结束时，机器人会用猫系人格拒绝，不会消耗 AI 回复。

海龟汤：群里有人要求“海龟汤”时，机器人会直接开一题；每个群 5 分钟 1 次，冷却未结束时会用猫系人格提醒。私聊不按群冷却。

星痕共鸣：程序内置了《星痕共鸣》/Blue Protocol: Star Resonance 的轻量本地语料记忆；本轮输入包含星痕共鸣相关词时，会把这份本地记忆提供给主 AI。若用户要最新赛季、强度榜或活动，仍建议触发联网搜索。

## 群友记忆和称呼

程序会自动把见过的 QQ 用户、群成员昵称和最后出现时间记录下来。`memory.json` 会放在 `config.json` 同目录作为索引壳子；每个群会单独写到 `memory_groups/group_<群号>.json`，每个私聊人物会单独写到 `memory_users/user_<QQ>.json`，重启后仍然能记住群友和私聊人物。

`config.json` 里可以配置称呼：

```json
{
  "memory": {
    "enabled": true,
    "path": "memory.json",
    "group_storage_dir": "memory_groups",
    "user_storage_dir": "memory_users",
    "max_known_members": 20,
    "special_group_ids": [168538447],
    "special_care_user_ids": [993255714],
    "group_special_care_user_ids": {
      "168538447": [993255714]
    },
    "special_care_cooldown_seconds": 90,
    "special_care_response_window_minutes": 30,
    "summary_interval_minutes": 30,
    "max_corpus_messages": 800,
    "private_summary_messages": 500,
    "member_mention_threshold": 20,
    "special_group_active_window_enabled": false,
    "special_group_active_minutes_per_hour": 10,
    "user_titles": {
      "993255714": "主任"
    },
    "group_titles": {
      "168538447": "社员"
    },
    "group_user_titles": {
      "168538447": {
        "993255714": "主任"
      }
    }
  }
}
```

优先级是：`group_user_titles` 指定群内专属称呼 > `user_titles` 全局称呼 > `group_titles` 群默认称呼 > `群友`。

所有允许的群都会记录待压缩语料，并按 `summary_interval_minutes` 定时压缩成长期摘要；摘要会在后续对话里提供给模型，用来形成群印象和群友画像。生成长期摘要后，当前待压缩语料会自动清空。私聊也会按人物单独记录语料并定时压缩，用来记住用户偏好、称呼和重要事实。主回复线程还会额外加入一条很短的“笨猫人格记忆”，把“笨猫/猫猫/米雪儿/你都是当前助手本人”、回复节奏和称呼规则固定住；如果模型有客服腔、报告腔、第三人称说笨猫等脱离人格倾向，会被要求先在心里重读主 prompt 和笨猫人格记忆，再重新组织回复。主线程还会追加“主回复智能策略”，要求模型先判断任务类型、主语指代、上下文、记忆和是否需要澄清，再给最终正文，减少关键词式傻回复。

`special_group_ids` 是旧特别关心群配置：当前不再用它限制总结范围，也不再用短活跃窗口控制普通群插话。普通群聊的长期主动观察由 `filter.group_batch_messages` 和 `filter.group_batch_seconds` 控制；到达批次后 AI 会自行判断是否值得插话，也可以选择不回复。

`special_care_user_ids` 和 `group_special_care_user_ids` 是特别关心用户配置：命中用户在群里发言时，不等普通批量 filter，直接把本轮消息交给主 AI 判断是否自然跟上去回复；若 AI 判断不适合，可以只输出不回复标记。机器人会记录上次跟特别关心贴上去后有没有被接话，没被理时会把一点“败犬感/酸酸失落”的状态交给主 AI 表现，但不会硬编码固定句子。

`proactive` 会让机器人每天在加入的群里主动冒泡：每个群每天最多 5 次，实际次数会根据该群互动分和当天群友发言量浮动。主动冒泡会参考群摘要、群友画像、近期聊天和上次有没有人接话，内容会从卡拉彼丘、自己的现实世界生活感、或适合当前群的话题里挑一个方向。如果冒泡后没人回应，机器人会记录一点失落感并降低该群互动分。

默认会把本地表情库候选交给主 AI；普通轻松聊天会优先让 AI 自己挑一张表情包，若 AI 没输出表情标记，程序也会按 `emoji.reply_probability` 尝试自动补一张命中的本地表情。`emojis/manifest.json` 是可发送表情白名单，只有 manifest 里登记且实际图片文件也存在于 `emoji.dir` 下的图片才会参与匹配，例如 `emojis/事后喵.jpg`；如果 AI 明确点名了一个本地没有的表情，程序会先尝试收养 `emoji.download_dir` 里文件名匹配的已下载图片，本轮消息带图片时也会尝试下载当前图片并登记为下载表情，仍然没有可用图片就跳过。默认表情不需要 `interest` 字段，`interest` 只用于下载表情。

图片消息：私聊发图会触发回复；群里如果带艾特、前缀或指向词，也会先把图片下载地址交给 `vision` 图片识别模型，识别成文字、兴趣程度、表情含义和情绪标签后再交给主聊天模型。主 AI 可以决定是否追加本地表情包；默认表情放在 `emojis/`，高兴趣图片会按配置保存到 `emojis/downloaded/` 并记录到 `emojis/manifest.json`。若 `vision` 不单独配置，会复用 `ai` 主模型配置；若遇到 GIF 或动态 WebP，程序会自动截取第一帧转成 PNG 再识别。若图片识别失败，会退回为文字方式把图片地址交给主模型。

本地 reply gate：`local_critic` 现在默认关闭，回复取舍主要交给主 AI 结合唤起上下文判断。需要时仍可接入 OpenAI-compatible 的本地小模型，例如 Ollama 的 `qwen2.5:1.5b`；开启后默认 `mode=reply_gate_only`，只负责入口粗筛，主 AI 仍会结合上下文决定是否输出正文或 `NO_REPLY`。@、回复、前缀、私聊、明显喊猫猫这些硬触发会直接进入主 AI 判断，不再等待本地模型。非训练实时 gate 会通过 `think=false`、`/no_think`、独立 `reply_gate_max_tokens`、独立 `reply_gate_request_timeout` 和短 payload 控制在 1-5 秒内；超时或返回坏 JSON 时只走硬判断 fallback。默认不再把历史训练样本塞进实时 gate prompt；开启采样时才会把放行/拒绝样本写回 `local_critic_samples.jsonl`。若 `warmup_enabled` 打开，启动后会用 Ollama 原生 `/api/generate` 空 prompt 预加载模型，并按 `warmup_interval_seconds` 刷新 `warmup_keep_alive`，减少长时间无人说话后的冷启动。

本地训练：`local_training` 会把 reply gate 样本导出成 `training/reply_gate_dataset.jsonl`，也会把主模型真实收到的上下文和最终回复收集到 `training/assistant_reply_samples.jsonl`，再导出成 `training/assistant_reply_dataset.jsonl`。聊天正文走 `ai`，普通审核/判断和训练成果审批走 `audit_ai`；`filter.model` 留空时会继承 `audit_ai`。Ollama 本体不能边运行边在线增参；样本达到对应 `min_samples` 且新增样本达到对应 `min_new_samples` 后，启动时会自动判断是否适合训练。`auto_fill_training_commands` 打开时，空的 `train_command` / `busy_train_command` / `assistant_train_command` / `assistant_busy_train_command` 会自动落到项目内安全 wrapper：`scripts/local_lora_train.py`。wrapper 只会执行你配置的 `backend_command` / `assistant_backend_command` / `busy_backend_command` / `assistant_busy_backend_command`，后端为空时只写状态并跳过，不会让主 AI 生成或执行任意 shell 命令。训练后如果输出目录出现 LoRA adapter、`Modelfile` 或 `.gguf`，wrapper 会记录成果并调用 `audit_ai` 做成果审核；审核模型只输出 `allow_apply/allow_merge` 和 `next_suggestions` JSON，不直接执行命令。配置了 `apply_trained_adapter_command` 且审核同意时会把小成果接入微调，样本达到 `merge_min_samples`、处于闲时且审核同意时才会执行 `merge_trained_model_command` 合并大成果。默认这些应用/合并命令为空，所以不会热替换正在工作的审核模型。`watch_interval_seconds` 大于 0 时会在后台循环检查。默认会根据服务器本地系统时间、凌晨闲时窗口和样本文件最近更新时间判断闲时；忙时训练会用低优先级并受 `busy_training_max_seconds` 和 `busy_training_max_steps` 限制，避免影响 reply 审核和主程序运行。

训练进度窗口：打开 `local_training.progress_window_enabled` 后，启动时会额外弹出 `scripts/catty_training_dashboard.py` 的 Tk 小窗，轮询数据集样本数、最近训练状态、GLM-5.1 成果审批、`next_suggestions` 和 `training/local_training.log`。窗口里还有 `Ollama test` 页，可以用主 AI 的 `chat.system_prompt` 向本地 `local_critic.model` 提问，查看输出耗时和内容，再把 1-5 分评分、备注写入 `model_test_scores_path`。测试请求会模拟主回复线程，带上笨猫人格记忆、自检提示、风格例句、入口唤起提示和简化记忆上下文，避免小模型把笨猫当第三个人，也方便观察它在接近真实主线程时的表现。默认以 `/no_think` 测试；勾选 `Thinking` 会发送 `think=true`，方便对比思考模式耗时和质量。窗口不会执行训练命令，也不会展示 API key。

训练 MCP server：项目内包含 `scripts/catty_training_mcp_server.py`，可作为 MCP stdio server 暴露 `training_status` 和 `training_config_summary` 两个工具，方便外部 MCP 客户端查看训练样本、最新成果状态和 hook 配置。它不会返回 API key。

项目内 Ollama：`ollama.enabled` 打开后，服务器启动 Catty 时会自动把 Ollama 便携包部署到 `tools/ollama`，把模型放到 `models/ollama`，并用当前配置文件所在文件夹作为工作目录启动。`install_dir` 和 `models_dir` 必须留在项目文件夹内，避免把程序或模型装到项目外。

也可以手动执行同样的项目内部署：

```powershell
.\scripts\start_ollama_local.ps1
```

脚本会下载项目内便携 Ollama 并拉取 `qwen2.5:1.5b`；临时不想拉模型时加 `-SkipPull`。

服务器 Python 环境：运行 `install_python_env.bat` 会自动寻找或安装 Python 3.11、创建 `.venv`、安装依赖，然后生成只用于启动的 `start_catty.bat`。安装脚本成功后会自动删除自己；以后服务器直接运行 `start_catty.bat`。

## 打包 exe

安装打包依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[build]"
```

打包：

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean CattyQQAI.spec
```

产物在：

```text
dist/CattyQQAI.exe
```

把 `config.json` 和 `tools/napcat-onekey` 放在 exe 同目录即可。程序启动时会优先读取当前目录或 exe 同目录的 `config.json`；如果找不到，会自动生成一个默认配置。

## 常用配置

| JSON 路径 | 默认值 | 说明 |
| --- | --- | --- |
| `server.host` | `127.0.0.1` | NoneBot 监听地址 |
| `server.port` | `8080` | NoneBot 监听端口 |
| `qq.account` | 空 | 记录机器人 QQ 号 |
| `qq.onebot_reverse_ws_url` | `ws://127.0.0.1:8080/onebot/v11/` | NapCatQQ 反向 WebSocket 地址 |
| `qq.napcat_access_token` | 空 | NapCat OneBot 鉴权 token，需和 NapCat 网络配置一致 |
| `qq.auto_start_napcat` | `true` | 启动本程序时是否自动拉起 NapCat |
| `qq.napcat_workdir` | `tools/napcat-onekey/bootmain` | NapCat 启动目录 |
| `qq.napcat_executable` | `NapCatWinBootMain.exe` | NapCat 启动程序 |
| `ai.base_url` | `https://api.openai.com/v1` | OpenAI-compatible API 根地址 |
| `ai.api_key` | 空 | API Key |
| `ai.model` | `gpt-4o-mini` | 模型名 |
| `ai.extra_headers` | `{}` | 额外 HTTP Header |
| `ai.extra_body` | `{}` | 额外请求体字段 |
| `audit_ai.base_url` | 空 | 审核/判断/训练成果审批模型地址；空则最终兜底 `ai.base_url` |
| `audit_ai.api_key` | 空 | 审核模型 API Key；空则最终兜底 `ai.api_key` |
| `audit_ai.model` | 空 | 审核模型名；空则最终兜底 `ai.model` |
| `audit_ai.max_tokens` | `320` | 审核 JSON 输出最大 token |
| `vision.base_url` | 空 | 图片识别模型 OpenAI-compatible 地址；空则复用 `ai.base_url` |
| `vision.api_key` | 空 | 图片识别模型 API Key；空则复用 `ai.api_key` |
| `vision.model` | 空 | 图片识别模型名；空则复用 `ai.model` |
| `vision.max_tokens` | `800` | 图片识别结果最大 token |
| `filter.enabled` | `true` | 是否启用普通群消息批量主动判断、语义分段判断和怒气判断 |
| `filter.base_url` | 空 | 过滤模型 OpenAI-compatible 地址；空则复用 `ai.base_url` |
| `filter.api_key` | 空 | 过滤模型 API Key；空则复用 `ai.api_key` |
| `filter.model` | 空 | 过滤模型名；空则复用 `ai.model` |
| `filter.max_tokens` | `64` | 过滤判断最大 token，建议使用便宜快速模型 |
| `filter.group_batch_messages` | `200` | 每个群累计多少条普通群消息后触发一次主动回复判断 |
| `filter.group_batch_seconds` | `1200` | 每个群普通群消息最多等待多少秒触发一次主动回复判断；在下一条群消息到达时检查 |
| `filter.anger_enabled` | `true` | 是否启用用户无用复读怒气值判断 |
| `filter.anger_warn_threshold` | `60` | 怒气达到该值后把不耐烦状态反馈给主 AI |
| `filter.anger_mute_threshold` | `100` | 怒气达到该值后进入少搭理冷却；filter 只提供状态，是否回复和怎么表达交给主 AI |
| `filter.anger_cooldown_seconds` | `3600` | 怒气爆表后的冷却秒数 |
| `ollama.enabled` | `false` | 是否在 Catty 启动时自动部署并启动项目内 Ollama |
| `ollama.auto_install` | `true` | `tools/ollama` 缺少 Ollama 时是否自动下载便携包 |
| `ollama.auto_pull_model` | `true` | 模型不存在时是否自动拉取到 `models/ollama` |
| `ollama.model` | `qwen2.5:1.5b` | 自动拉取和校正默认使用的本地模型 |
| `ollama.install_dir` | `tools/ollama` | Ollama 程序安装目录；必须在项目文件夹内 |
| `ollama.models_dir` | `models/ollama` | Ollama 模型目录；必须在项目文件夹内 |
| `ollama.download_url` | 空 | 自定义 Ollama 便携包下载地址；空则按系统使用默认下载地址 |
| `ollama.stop_existing` | `true` | 启动项目内 Ollama 前是否先停止已有 Ollama 进程 |
| `local_critic.enabled` | `false` | 是否启用本地模型 reply gate |
| `local_critic.mode` | `reply_gate_only` | 本地模型模式；默认只做 reply gate，设为 `reply_gate_and_critic` 才启用后置回复校正 |
| `local_critic.base_url` | `http://127.0.0.1:11434/v1` | 本地校正模型 OpenAI-compatible 地址 |
| `local_critic.api_key` | `ollama` | 本地校正模型 API Key；Ollama 兼容端点可用任意非空值 |
| `local_critic.model` | `qwen2.5:1.5b` | 本地校正模型名 |
| `local_critic.max_tokens` | `16` | 后置校正模式的 JSON 输出最大 token；`reply_gate_only` 下实时 gate 使用 `reply_gate_max_tokens` |
| `local_critic.request_timeout` | `4` | 本地模型默认请求超时；`reply_gate_only` 下实时 gate 使用 `reply_gate_request_timeout` |
| `local_critic.extra_body` | `{"think": false}` | 传给本地模型端点的额外请求体；默认只关闭 thinking，不再给实时 gate 塞额外 Ollama 参数 |
| `local_critic.rewrite_when_score_below` | `0` | 仅 `reply_gate_and_critic` 使用；评分低于该值时请求主模型重写 |
| `local_critic.reply_gate_enabled` | `true` | 是否由本地模型决定本轮是否交给主 AI 回复 |
| `local_critic.reply_gate_min_confidence` | `55` | 本地 reply gate 放行所需最低置信度 |
| `local_critic.reply_gate_examples` | `0` | 每次判定时读取最近多少条 reply gate 样本作为参考；实时低延迟建议保持 0 |
| `local_critic.reply_gate_max_tokens` | `16` | reply gate JSON 输出最大 token |
| `local_critic.reply_gate_request_timeout` | `4` | reply gate 单次请求超时，超时后走硬判断 fallback |
| `local_critic.reply_gate_user_message_chars` | `120` | reply gate 最多读取多少字符的群聊展示消息 |
| `local_critic.reply_gate_plain_text_chars` | `60` | reply gate 最多读取多少字符的纯文本 |
| `local_critic.reply_gate_context_chars` | `80` | reply gate 最多读取多少字符的批量/特别关心上下文 |
| `local_critic.warmup_enabled` | `true` | 是否用 Ollama 原生空 prompt 后台预加载/保温本地校正模型；仅在 `local_critic.enabled` 打开时运行 |
| `local_critic.warmup_keep_alive` | `30m` | 预热请求要求 Ollama 将模型保留在内存中的时长 |
| `local_critic.warmup_interval_seconds` | `300` | 后台保温间隔；应短于 `warmup_keep_alive` |
| `local_critic.warmup_request_timeout` | `60` | 单次预热/保温请求超时 |
| `local_critic.force_direct_reply` | `true` | @、回复、前缀、私聊、明显喊名时即使 gate 异常也强制放行 |
| `local_critic.collect_training_samples` | `true` | 是否保存草稿、评分和最终回复样本 |
| `local_critic.training_samples_path` | `local_critic_samples.jsonl` | 校正样本 JSONL 文件路径 |
| `local_training.enabled` | `false` | 是否启用本地训练数据导出/训练钩子 |
| `local_training.auto_train_on_startup` | `false` | 启动时是否自动检查样本并运行训练命令 |
| `local_training.dataset_path` | `training/reply_gate_dataset.jsonl` | 导出的 reply gate 训练集路径 |
| `local_training.output_dir` | `training/reply_gate_lora` | 训练输出目录 |
| `local_training.min_samples` | `200` | 至少累计多少样本才允许训练 |
| `local_training.min_new_samples` | `50` | 距离上次训练至少新增多少样本才再次训练 |
| `local_training.train_command` | 安全 wrapper | reply gate 闲时训练命令；支持 `{dataset}`、`{output_dir}`、`{config}`、`{python}`、`{scripts_dir}` 占位符 |
| `local_training.collect_assistant_samples` | `true` | 是否收集主模型上下文和最终回复，供本地模型模仿学习 |
| `local_training.assistant_samples_path` | `training/assistant_reply_samples.jsonl` | 主模型影子语料原始 JSONL |
| `local_training.assistant_dataset_path` | `training/assistant_reply_dataset.jsonl` | 导出的主模型回复训练集 |
| `local_training.assistant_output_dir` | `training/assistant_reply_lora` | 主模型回复 LoRA 输出目录 |
| `local_training.assistant_train_command` | 安全 wrapper | 主模型回复闲时训练命令；支持 `{dataset}`、`{output_dir}`、`{config}`、`{python}`、`{scripts_dir}` 占位符 |
| `local_training.auto_fill_training_commands` | `true` | 训练命令为空时是否自动使用项目内安全 wrapper |
| `local_training.backend_command` | 空 | wrapper 执行的 reply gate 闲时后端命令；支持 `{dataset}`、`{output_dir}`、`{config}`、`{task}`、`{mode}`、`{max_steps}` |
| `local_training.assistant_backend_command` | 空 | wrapper 执行的主模型回复闲时后端命令 |
| `local_training.busy_backend_command` | 空 | wrapper 执行的 reply gate 忙时小训练后端命令 |
| `local_training.assistant_busy_backend_command` | 空 | wrapper 执行的主模型回复忙时小训练后端命令 |
| `local_training.artifact_audit_enabled` | `true` | 是否用 `audit_ai` 审核训练成果 |
| `local_training.artifact_audit_route` | `audit_ai` | 成果审核路由；默认走 `audit_ai`，最终兜底 `ai` |
| `local_training.artifact_audit_base_url` | 空 | 成果审核模型地址；空则复用 `audit_ai.base_url` |
| `local_training.artifact_audit_api_key` | 空 | 成果审核 API Key；空则复用 `audit_ai.api_key` |
| `local_training.artifact_audit_model` | 空 | 成果审核模型；空则复用 `audit_ai.model` |
| `local_training.artifact_audit_temperature` | `0.5` | 训练成果审批温度；给 GLM-5.1 留一点建议弹性 |
| `local_training.artifact_audit_max_tokens` | `640` | 成果审批 JSON 最大 token，包含下一步建议 |
| `local_training.artifact_audit_can_approve_apply` | `true` | 审核模型是否有权批准小成果接入 |
| `local_training.artifact_audit_can_approve_merge` | `true` | 审核模型是否有权批准大成果合并 |
| `local_training.artifact_audit_next_suggestions_enabled` | `true` | 是否要求并记录 GLM-5.1 审批时给出的下一步建议 |
| `local_training.artifact_audit_suggestions_path` | `training/glm_audit_suggestions.jsonl` | 审批建议 JSONL 记录路径 |
| `local_training.apply_trained_adapter_enabled` | `true` | 训练产出 adapter 后是否允许执行接入微调 hook |
| `local_training.apply_trained_adapter_command` | 空 | reply gate 小成果接入命令；支持 `{artifact}` 等 wrapper 占位符 |
| `local_training.assistant_apply_trained_adapter_command` | 空 | 主模型回复小成果接入命令 |
| `local_training.merge_trained_model_enabled` | `true` | 大成果达到阈值后是否允许执行模型合并 hook |
| `local_training.merge_trained_model_command` | 空 | reply gate 大成果合并命令；建议只在闲时创建新模型标签 |
| `local_training.assistant_merge_trained_model_command` | 空 | 主模型回复大成果合并命令 |
| `local_training.merge_min_samples` | `1000` | reply gate 至少多少训练样本才允许大成果合并 |
| `local_training.assistant_merge_min_samples` | `1000` | 主模型回复至少多少训练样本才允许大成果合并 |
| `local_training.busy_training_max_steps` | `20` | wrapper 传给忙时后端的建议最大步数 |
| `local_training.idle_training_max_steps` | `200` | wrapper 传给闲时后端的建议最大步数 |
| `local_training.busy_training_enabled` | `true` | 忙时是否允许小幅度训练；需要单独配置 busy 命令 |
| `local_training.busy_train_command` | 安全 wrapper | 忙时 reply gate 小训练命令；默认走受控 wrapper |
| `local_training.assistant_busy_train_command` | 安全 wrapper | 忙时主模型回复小训练命令；默认走受控 wrapper |
| `local_training.busy_training_max_seconds` | `600` | 忙时单次训练最大秒数；超时会放弃本轮 |
| `local_training.idle_training_max_seconds` | `0` | 闲时单次训练最大秒数；0 表示不限制 |
| `local_training.idle_training_enabled` | `true` | 是否启用自动闲时判断；关闭后只按样本条件运行训练命令 |
| `local_training.idle_start_hour` | `2` | 本地系统时间闲时开始小时 |
| `local_training.idle_end_hour` | `6` | 本地系统时间闲时结束小时 |
| `local_training.idle_min_quiet_minutes` | `45` | 样本文件多久没更新才认为聊天足够安静 |
| `local_training.allow_quiet_idle` | `true` | 非凌晨窗口但长时间安静时是否也允许完整训练 |
| `local_training.active_check_interval_seconds` | `900` | 判断为忙时的下次检查间隔 |
| `local_training.idle_check_interval_seconds` | `3600` | 判断为闲时的下次检查间隔 |
| `local_training.mcp_server_enabled` | `true` | 是否随配置提供训练 MCP server 脚本 |
| `local_training.mcp_server_script` | `scripts/catty_training_mcp_server.py` | 训练 MCP server 脚本路径 |
| `local_training.progress_window_enabled` | `false` | 是否启动训练进度/GLM 审批小窗 |
| `local_training.progress_window_script` | `scripts/catty_training_dashboard.py` | 训练进度窗口脚本 |
| `local_training.progress_window_poll_seconds` | `5` | 训练进度窗口刷新间隔 |
| `local_training.progress_log_path` | `training/local_training.log` | 自动训练 watcher 的日志路径 |
| `local_training.model_test_max_tokens` | `480` | 训练窗口里手动询问本地模型时的最大输出 token |
| `local_training.model_test_request_timeout` | `60` | 训练窗口里手动询问本地模型时的请求超时 |
| `local_training.model_test_thinking_max_tokens` | `96` | 训练窗口勾选 `Thinking` 时的最大输出 token；小模型或老 CPU 上建议保持较低 |
| `local_training.model_test_thinking_timeout` | `20` | 训练窗口勾选 `Thinking` 时的单独超时；避免远端思维链卡住导致小窗长时间等待 |
| `local_training.model_test_scores_path` | `training/model_eval_scores.jsonl` | 训练窗口保存人工评分和输出耗时的 JSONL 路径 |
| `local_training.assistant_min_samples` | `200` | 主模型回复样本至少累计多少条才允许训练 |
| `local_training.assistant_min_new_samples` | `50` | 主模型回复距离上次训练至少新增多少条才再次训练 |
| `local_training.watch_interval_seconds` | `0` | 大于 0 时后台循环检查训练条件；0 表示启动时只检查一次 |
| `web_search.enabled` | `true` | 是否允许显式联网搜索 |
| `web_search.cooldown_seconds` | `600` | 普通用户联网搜索冷却；有 `user_titles`/`group_user_titles` 的用户不受限制 |
| `web_search.max_results` | `5` | 每次搜索交给 AI 的结果数量上限 |
| `web_search.request_timeout` | `10` | 联网搜索请求超时秒数 |
| `turtle_soup.cooldown_seconds` | `300` | 每个群触发海龟汤的冷却秒数 |
| `chat.trigger_prefixes` | `["ai","AI","猫猫"]` | 群聊文字触发前缀 |
| `chat.group_require_mention_or_prefix` | `true` | 群聊是否必须艾特或前缀 |
| `chat.private_require_prefix` | `false` | 私聊是否必须前缀 |
| `chat.history_turns` | `16` | 每个会话保留的上下文轮数 |
| `chat` 唤起上下文 | 自动 | 入口被唤起后，会把当前唤起消息附近最多上 3 条/下 3 条近期聊天交给主 AI 判断；主 AI 可输出 `NO_REPLY` 安静不回 |
| `chat.reply_max_chars` | `1800` | 单条回复超过该长度时最多切成两条发送 |
| `chat.reply_human_split_enabled` | `true` | 是否允许本地概率判断本轮是否追加语义分段提示 |
| `chat.reply_human_split_probability` | `0.35` | 分段提示的本地触发概率，不再额外调用 filter API |
| `chat.reply_human_split_min_chars` | `48` | 提示 AI 至少达到该字符数才考虑语义分段 |
| `chat.reply_human_split_delay_seconds` | `0.8` | 分段发送之间的等待秒数 |
| `chat.reply_self_check_enabled` | `true` | 回复前追加隐藏自检提示，让主 AI 先理解意图；若有脱离人格倾向则先重读主 prompt 和笨猫人格记忆；遇到敏感/超限请求时用猫娘语气短拒绝或转安全替代 |
| `chat.reply_style_examples_enabled` | `true` | 回复前追加猫娘风格例句提示，让主 AI 学习可爱但有用的 QQ 口语节奏 |
| `chat.directed_keywords` | `["你","猫猫","猫娘","看看"]` | 群聊软触发词；命中后交给 AI 判断主语、呼唤对象和是否需要回应 |
| `chat.soft_directed_reply_probability` | `0.65` | 普通软触发交给 AI 后的回复倾向提示；数值越高越容易接话 |
| `chat.direct_address_reply_probability` | `0.9` | 像“猫猫你看看/问猫猫”这类明显喊名时的回复倾向提示 |
| `chat.image_response_enabled` | `true` | 是否响应图片消息 |
| `chat.image_vision_enabled` | `true` | 是否启用图片识别；启用后先走 `vision`，再把识别结果交给主模型 |
| `chat.expression_repeat_enabled` | `true` | 是否启用群聊表情/文字连发复读 |
| `chat.expression_repeat_threshold` | `2` | 连续多少条相同消息后直接复读 |
| `chat.expression_repeat_window_seconds` | `20` | 连发计数允许的最大间隔秒数 |
| `chat.expression_repeat_include_images` | `true` | 是否把图片类表情也纳入复读检测 |
| `chat.expression_repeat_include_text` | `true` | 是否把文字消息也纳入复读检测 |
| `emoji.enabled` | `true` | 是否启用本地表情库和高兴趣图片入库 |
| `emoji.dir` | `emojis` | 默认表情库目录，直接放在该目录下的图片优先级高于下载表情 |
| `emoji.download_dir` | `emojis/downloaded` | 高兴趣图片自动保存目录 |
| `emoji.manifest_path` | `emojis/manifest.json` | 表情含义、标签和来源记录文件 |
| `emoji.interest_threshold` | `60` | 识图兴趣达到该值后才把表情候选交给主 AI |
| `emoji.save_interest_threshold` | `85` | 识图兴趣达到该值且 vision 标记可复用时保存到下载表情库 |
| `emoji.max_candidates` | `8` | 每次提供给主 AI 选择的表情候选数量 |
| `emoji.reply_enabled` | `true` | 是否允许 AI 回复后额外发送一张本地表情包 |
| `emoji.reply_probability` | `0.85` | AI 没主动选择表情时，程序自动补发本地表情包的概率 |
| `memory.enabled` | `true` | 是否启用群友记忆 |
| `memory.path` | `memory.json` | 记忆索引文件路径 |
| `memory.group_storage_dir` | `memory_groups` | 按群号拆分保存群记忆 JSON 的目录 |
| `memory.user_storage_dir` | `memory_users` | 按 QQ 号拆分保存私聊人物 JSON 的目录 |
| `memory.special_group_ids` | `[]` | 特别关心群号列表 |
| `memory.special_care_user_ids` | `[]` | 全局特别关心 QQ 用户；这些用户群聊发言会立即交给主 AI 判断是否跟上 |
| `memory.group_special_care_user_ids` | `{}` | 按群号配置特别关心 QQ 用户列表 |
| `memory.special_care_cooldown_seconds` | `90` | 同一特别关心用户普通发言触发主 AI 判断的最小间隔；明确 @/前缀不受影响 |
| `memory.special_care_response_window_minutes` | `30` | 笨猫跟特别关心回复后等待对方接话的窗口；超时会记录一点没被理状态 |
| `memory.summary_interval_minutes` | `30` | 群聊语料压缩间隔 |
| `memory.max_corpus_messages` | `800` | 每个群最多保留的待总结语料条数 |
| `memory.reply_boost_enabled` | `true` | 语料、摘要或画像足够时是否提高软触发回复倾向 |
| `memory.reply_boost_min_corpus_messages` | `80` | 待压缩语料达到多少条后启用回复倾向加成 |
| `memory.reply_boost_probability_bonus` | `0.15` | 记忆加成给软触发回复倾向增加的数值 |
| `memory.reply_boost_max_probability` | `0.95` | 记忆加成后的回复倾向上限 |
| `memory.private_summary_messages` | `500` | 私聊累计多少条后做一次长期总结 |
| `memory.member_mention_threshold` | `20` | 群友被 @ 提到多少次后做画像总结 |
| `memory.special_group_active_minutes_per_hour` | `10` | 旧热点窗口配置；当前普通群消息按 `filter.group_batch_messages` / `filter.group_batch_seconds` 批量判断 |
| `memory.user_titles` | `{}` | 按 QQ 号配置全局称呼 |
| `memory.group_titles` | `{}` | 按群号配置群默认称呼 |
| `memory.group_user_titles` | `{}` | 按群号和 QQ 号配置专属称呼 |
| `proactive.enabled` | `true` | 是否启用每天按群主动冒泡 |
| `proactive.max_daily_per_group` | `5` | 每个群每天最多主动冒泡次数 |
| `proactive.check_interval_seconds` | `300` | 主动冒泡调度器检查间隔 |
| `proactive.min_interval_minutes` | `120` | 同一群两次主动冒泡的最小间隔 |
| `proactive.response_window_minutes` | `30` | 主动冒泡后等待群友回应的窗口；超时无人回复会降低互动分 |
| `proactive.recent_messages` | `40` | 主动冒泡时参考的近期群聊条数 |
| `access.allowed_user_ids` | `[]` | 只允许这些 QQ 用户 |
| `access.allowed_group_ids` | `[]` | 只允许这些 QQ 群 |

`ai.extra_headers` 示例：

```json
{
  "ai": {
    "extra_headers": {
      "HTTP-Referer": "https://example.com",
      "X-Title": "Catty QQ AI"
    }
  }
}
```

`ai.extra_body` 示例：

```json
{
  "ai": {
    "extra_body": {
      "top_p": 0.9
    }
  }
}
```

## 资料来源

- NoneBot2 GitHub：<https://github.com/nonebot/nonebot2>
- NoneBot OneBot 适配器：<https://github.com/nonebot/adapter-onebot>
- OneBot 连接配置文档：<https://onebot.adapters.nonebot.dev/docs/guide/setup/>
- NapCatQQ OneBot 网络基础：<https://www.napcat.wiki/onebot/network>
- OpenAI Chat Completions API：<https://platform.openai.com/docs/api-reference/chat/create-chat-completion>
