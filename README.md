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

艾特、回复机器人消息、触发前缀或命中 `directed_keywords` 时会直接回复；普通群聊会按群攒到 `filter.group_batch_messages` 条，或距离本群上一批普通群消息达到 `filter.group_batch_seconds` 秒后，带主动插话提示交给 AI 判断是否自然回复；如果不该回复，AI 会输出内部不回复标记且不会发到群里。

如果群友在短时间内连续发送同一个 QQ 表情，默认第 3 次时机器人会直接复读这条表情消息，不调用 AI 接口。AI 的普通文本回复会由 `filter` 小模型判断是否追加本轮专用的轻量分段提示，再由主模型按语义决定是否拆成两条消息发送；原始 `system_prompt` 不会被改写。

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

所有允许的群都会记录待压缩语料，并按 `summary_interval_minutes` 定时压缩成长期摘要；摘要会在后续对话里提供给模型，用来形成群印象和群友画像。生成长期摘要后，当前待压缩语料会自动清空。私聊也会按人物单独记录语料并定时压缩，用来记住用户偏好、称呼和重要事实。

`special_group_ids` 是旧特别关心群配置：当前不再用它限制总结范围，也不再用短活跃窗口控制普通群插话。普通群聊的长期主动观察由 `filter.group_batch_messages` 和 `filter.group_batch_seconds` 控制；到达批次后 AI 会自行判断是否值得插话，也可以选择不回复。

`proactive` 会让机器人每天在加入的群里主动冒泡：每个群每天最多 5 次，实际次数会根据该群互动分和当天群友发言量浮动。主动冒泡会参考群摘要、群友画像、近期聊天和上次有没有人接话，内容会从卡拉彼丘、自己的现实世界生活感、或适合当前群的话题里挑一个方向。如果冒泡后没人回应，机器人会记录一点失落感并降低该群互动分。

当前默认 prompt 已经严格清理 emoji 使用：AI 默认不输出 emoji、颜文字或 `:heart:` 这类表情代码，除非用户明确要求。

图片消息：私聊发图会触发回复；群里如果带艾特、前缀或指向词，也会先把图片下载地址交给 `vision` 图片识别模型，识别成文字后再交给主聊天模型。若 `vision` 不单独配置，会复用 `ai` 主模型配置；若遇到 GIF 或动态 WebP，程序会自动截取第一帧转成 PNG 再识别。若图片识别失败，会退回为文字方式把图片地址交给主模型。

## 打包 exe

安装打包依赖：

```powershell
pip install -e ".[build]"
```

打包：

```powershell
pyinstaller --noconfirm --clean --onefile --name CattyQQAI --paths src --add-data "src/catty_qq_ai;catty_qq_ai" --hidden-import catty_qq_ai --hidden-import catty_integrations --hidden-import nonebot.drivers.fastapi --hidden-import nonebot.adapters.onebot.v11 bot.py
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
| `vision.base_url` | 空 | 图片识别模型 OpenAI-compatible 地址；空则复用 `ai.base_url` |
| `vision.api_key` | 空 | 图片识别模型 API Key；空则复用 `ai.api_key` |
| `vision.model` | 空 | 图片识别模型名；空则复用 `ai.model` |
| `vision.max_tokens` | `800` | 图片识别结果最大 token |
| `filter.enabled` | `true` | 是否启用普通群消息批量主动判断、语义分段判断和怒气判断 |
| `filter.base_url` | 空 | 过滤模型 OpenAI-compatible 地址；空则复用 `ai.base_url` |
| `filter.api_key` | 空 | 过滤模型 API Key；空则复用 `ai.api_key` |
| `filter.model` | 空 | 过滤模型名；空则复用 `ai.model` |
| `filter.max_tokens` | `64` | 过滤判断最大 token，建议使用便宜快速模型 |
| `filter.group_batch_messages` | `50` | 每个群累计多少条普通群消息后触发一次主动回复判断 |
| `filter.group_batch_seconds` | `120` | 每个群普通群消息最多等待多少秒触发一次主动回复判断；在下一条群消息到达时检查 |
| `filter.anger_enabled` | `true` | 是否启用用户无用复读怒气值判断 |
| `filter.anger_warn_threshold` | `60` | 怒气达到该值后把不耐烦状态反馈给主 AI |
| `filter.anger_mute_threshold` | `100` | 怒气达到该值后暂时不回复该用户 |
| `filter.anger_cooldown_seconds` | `3600` | 怒气爆表后的冷却秒数 |
| `chat.trigger_prefixes` | `["ai","AI","猫猫"]` | 群聊文字触发前缀 |
| `chat.group_require_mention_or_prefix` | `true` | 群聊是否必须艾特或前缀 |
| `chat.private_require_prefix` | `false` | 私聊是否必须前缀 |
| `chat.history_turns` | `16` | 每个会话保留的上下文轮数 |
| `chat.reply_max_chars` | `1800` | 单条回复超过该长度时强制切分 |
| `chat.reply_human_split_enabled` | `true` | 是否允许 filter API 判断本轮是否追加语义分段提示 |
| `chat.reply_human_split_probability` | `0.35` | 兼容旧配置；大于 0 时启用 filter API 语义分段判断 |
| `chat.reply_human_split_min_chars` | `48` | 提示 AI 至少达到该字符数才考虑语义分段 |
| `chat.reply_human_split_delay_seconds` | `0.8` | 分段发送之间的等待秒数 |
| `chat.directed_keywords` | `["你","猫猫","猫娘","看看"]` | 群聊指向词，命中后可触发回复 |
| `chat.image_response_enabled` | `true` | 是否响应图片消息 |
| `chat.image_vision_enabled` | `true` | 是否启用图片识别；启用后先走 `vision`，再把识别结果交给主模型 |
| `chat.expression_repeat_enabled` | `true` | 是否启用群聊表情连发复读 |
| `chat.expression_repeat_threshold` | `3` | 连续多少条相同表情后直接复读 |
| `chat.expression_repeat_window_seconds` | `20` | 连发计数允许的最大间隔秒数 |
| `chat.expression_repeat_include_images` | `true` | 是否把图片类表情也纳入复读检测 |
| `memory.enabled` | `true` | 是否启用群友记忆 |
| `memory.path` | `memory.json` | 记忆索引文件路径 |
| `memory.group_storage_dir` | `memory_groups` | 按群号拆分保存群记忆 JSON 的目录 |
| `memory.user_storage_dir` | `memory_users` | 按 QQ 号拆分保存私聊人物 JSON 的目录 |
| `memory.special_group_ids` | `[]` | 特别关心群号列表 |
| `memory.summary_interval_minutes` | `30` | 群聊语料压缩间隔 |
| `memory.max_corpus_messages` | `800` | 每个群最多保留的待总结语料条数 |
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
