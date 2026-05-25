# Catty QQ AI

> 一只接进 QQ 的「笨猫」——基于 NoneBot2 + OneBot v11 的 QQ 聊天 AI 插件，
> 后端走任何 OpenAI 兼容的 `/v1/chat/completions` 服务。
>
> **v1.0.0 整版发布** ฅฅ 群聊主动插话、长期记忆 + 群友画像、识图、本地表情、
> 联网搜索、特别关心、主动冒泡、本地小模型 reply gate、闲时 LoRA 自训、
> NapCat 一键打包、热重载守护——一锅炖好了喵～

---

## 目录

- [架构](#架构)
- [快速开始](#快速开始)
- [接入 QQ（NapCat）](#接入-qqnapcat)
- [对话与触发](#对话与触发)
- [记忆 / 群友画像 / 称呼](#记忆--群友画像--称呼)
- [识图 / 本地表情 / 联网](#识图--本地表情--联网)
- [特别关心 & 主动冒泡](#特别关心--主动冒泡)
- [Prompt & 会话缓存](#prompt--会话缓存)
- [本地 reply gate & 训练（可选）](#本地-reply-gate--训练可选)
- [热重载与一键启动](#热重载与一键启动)
- [打包 exe](#打包-exe)
- [主人专属命令](#主人专属命令)
- [常用配置](#常用配置)
- [资料来源](#资料来源)

---

## 架构

| 组件 | 作用 |
| --- | --- |
| **NapCatQQ** | QQ 协议端，登录机器人小号，提供 OneBot v11 事件 / API |
| **NoneBot2** | Bot 框架，反向 WebSocket 接 NapCat，加载 `catty_qq_ai` 插件 |
| **catty_qq_ai** | 本项目主体：人格、记忆、识图、表情、训练、命令、热重载等全部在这里 |
| **OpenAI-compatible API** | 任何兼容 `/v1/chat/completions` 的服务（OpenAI / OpenRouter / vLLM / Ollama OpenAI 端等） |

> 提醒：QQ 第三方协议端有账号风控和平台条款风险，先用小号测试，别拿来刷屏 / 群发广告。

---

## 快速开始

需要 **Python 3.10+**。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .
```

复制示例配置并填好 API：

```powershell
copy config.example.json config.json
```

然后用任意编辑器打开 `config.json` 填好。

至少填好 `ai.base_url` / `ai.api_key` / `ai.model`：

```json
{
  "ai": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "<API Key>",
    "model": "gpt-4o-mini"
  }
}
```

接本地 / 第三方 OpenAI 兼容服务时把 `base_url` 改成它的 `/v1` 根地址即可，例如：

```json
{
  "ai": {
    "base_url": "http://127.0.0.1:8000/v1",
    "api_key": "local-or-empty",
    "model": "qwen2.5-7b-instruct"
  }
}
```

启动 NoneBot：

```powershell
python bot.py
```

默认监听 `127.0.0.1:8080`。

> 服务器一键部署：跑 `install_python_env.bat` 自动找 / 装 Python 3.11、建 `.venv`、装依赖，
> 之后双击 `start_catty.bat` 就能起。安装脚本成功后会自删；以后只用 `start_catty.bat`。

---

## 接入 QQ（NapCat）

项目自带 NapCat Windows 一键包：`tools/napcat-onekey`。

1. 首次运行 `tools/napcat-onekey/NapCatInstaller.exe` 初始化。
2. 登录准备当机器人的 QQ 号。
3. 在 NapCat 的 OneBot v11 网络配置里启用 **反向 WebSocket**：
   ```text
   ws://127.0.0.1:8080/onebot/v11/
   ```
4. 在 `config.json` 的 `qq` 区域填好信息：

```json
{
  "qq": {
    "account": "机器人 QQ 号",
    "onebot_reverse_ws_url": "ws://127.0.0.1:8080/onebot/v11/",
    "napcat_access_token": "",
    "auto_start_napcat": true,
    "napcat_workdir": "tools/napcat-onekey/bootmain",
    "napcat_executable": "NapCatWinBootMain.exe",
    "skip_if_napcat_running": true
  }
}
```

`auto_start_napcat=true` 时，Catty 启动会自动拉起 NapCat；`napcat_args` 留空且填了 `qq.account`，
会把 QQ 号作为启动参数自动传给 NapCat。手动开 NapCat 把 `auto_start_napcat` 改成 `false` 即可。

更新 NapCat 一键包：

```powershell
.\scripts\update_napcat_onekey.ps1
```

---

## 对话与触发

**私聊**：默认直接回复。

**群聊**：以下情况会让笨猫开口——

- `@机器人`、回复机器人消息、拍了拍机器人
- 以 `chat.trigger_prefixes`（默认 `ai` / `AI` / `猫猫`）开头
- 句子里出现 `chat.directed_keywords`（默认 `你` / `猫猫` / `猫娘` / `看看` 等）
  → 交给主 AI 判断主语 / 呼唤对象 / 是否需要回应
- **批量主动观察**：积满 `filter.group_batch_messages` 条或等到 `filter.group_batch_seconds` 秒后，
  把最近一批普通消息丢给 AI 自由判断要不要插话；不插话时会输出内部 `NO_REPLY` 标记，不刷屏

软触发回复倾向按场景给概率：

| 场景 | 概率提示 |
| --- | --- |
| 明显喊名（"猫猫你看看"） | `chat.direct_address_reply_probability`（默认 0.9） |
| 普通软触发 | `chat.soft_directed_reply_probability`（默认 0.65） |
| 当群语料 / 画像足够（`memory.reply_boost_*`） | 自动加成，上限 `reply_boost_max_probability` |

**复读模式**：群友短时间内连发同一条 QQ 表情 / 图 / 文字，到第 `expression_repeat_threshold` 条时
笨猫直接复读，不调 AI；机器人自己的消息不计入。

**人工分段**：AI 长回复会按本地概率追加分段提示，由模型按语义自己决定拆几段 / 发几条；
分段间等待 `chat.reply_human_split_delay_seconds`，最多 `reply_human_split_max_chunks` 条。

清空当前会话（**仅 Bot owner 可发**，群里其他人发同样的话不响应）：

```text
ai reset
ai 清空上下文
```

---

## 记忆 / 群友画像 / 称呼

笨猫会自动记下见过的群友昵称、最后出现时间，并按 `memory.summary_interval_minutes`
定时把待压缩语料压成长期摘要 + 群友画像。

存储拆分（重启不丢）：

```
memory.json                  ← 索引
memory_groups/group_<群号>.json
memory_users/user_<QQ>.json
```

每条待压缩语料带 `content_temperature`——新内容温度高、随时间自然降温；低温的旧梗 / 脏话 /
攻击性称呼只作背景，摘要和主动冒泡都被提醒不要重新主动续聊。

主回复线程额外注入一段「笨猫人格记忆」固定身份和口吻，避免模型客服腔 / 报告腔 / 第三人称谈笨猫。

**称呼配置**（优先级：群内专属 > 全局 > 群默认 > 「群友」）：

```json
{
  "memory": {
    "user_titles":        { "<用户QQ>": "群主" },
    "group_titles":       { "<群号>": "社员" },
    "group_user_titles":  { "<群号>": { "<用户QQ>": "群主" } }
  }
}
```

**游戏主题群**（注入本地游戏背景，无需联网）：

```json
{
  "game_context": {
    "star_resonance_group_ids": [],
    "strinova_group_ids":       []
  }
}
```

目前内置《星痕共鸣》/ Blue Protocol: Star Resonance 和《卡拉彼丘》/ Strinova 的轻量本地语料，
需要赛季 / 强度榜 / 兑换码这类最新信息时仍然建议触发联网搜索。

查看当前 scope 的记忆（**仅 Bot owner 可发**，避免内部状态被群友刷出来）：

```text
ai 查看记忆
ai 查看人物信息
```

清空当前 scope 的待压缩缓存（**仅 Bot owner 可发**，保留长期摘要 / 画像）：

```text
ai 清空缓存
ai 清空记忆缓存
```

---

## 识图 / 本地表情 / 联网

**识图（vision）**：私聊发图直接触发；群里带 @、前缀或指向词时先走 `vision` 模型，
识别成文字 / 兴趣度 / 表情含义后再交给主模型。GIF / 动态 WebP 会自动取首帧转 PNG。
`vision` 未单独配置时复用 `ai` 主模型。

**本地表情库**：默认把本地表情候选交给 AI 自己挑；AI 没选时按 `emoji.reply_probability` 自动补一张。

```
emojis/                ← 默认表情
emojis/downloaded/     ← 高兴趣图片自动入库
emojis/manifest.json   ← 含义 + 标签 + 来源记录
```

未登记的图会自动补入 manifest；AI 点名了本地没有的表情时，会先尝试收养下载目录里的匹配图，
再通过图片搜索下载到 `emoji.download_dir`，并由 `vision` 生成 `meaning/tags` 后写回 manifest。

**联网搜索**：群友明确说"联网搜索 / 上网查 / 搜一下 / 查一下"才触发；优先用主 AI 自带联网能力，
主 AI 不支持就如实告知，不编造。普通用户 10 分钟 1 次；配了专属称呼的用户不受冷却限制。

**海龟汤**：群里有人喊"海龟汤"会直接开题；每群 5 分钟 1 次，冷却内用猫系语气提醒。

---

## 特别关心 & 主动冒泡

**特别关心**（`memory.special_care_user_ids` / `group_special_care_user_ids`）：
命中用户群聊发言时不等批量 filter，直接交主 AI 判断是否自然跟上去；
若 AI 判断不合适会输出不回复标记。笨猫接话后会跟踪窗口内对方有没有理她，
没被理时把一点「败犬感 / 酸酸失落」交给主 AI 自由表现（**不硬编码句子**）。

**主动冒泡**（`proactive`）：每天每群最多 `max_daily_per_group`（默认 5）次，
实际次数随群互动分和当天发言量浮动。冒泡会参考群摘要 / 群友画像 / 近期聊天 / 上次接话情况，
方向从游戏话题、现实生活感、或当前群适合的话题里挑。
冒泡没人理时会自动降低该群互动分。

---

## Prompt & 会话缓存

**会话缓存**：每个群 / 私聊一份独立窗口，key 形如 `group:<群号>` / `private:<QQ>`。

- 持久化到 `sessions/` 目录，重启自动恢复
- 内存 LRU，超过 `session_cache.max_sessions` 时连同盘上文件一起淘汰
- dirty 标记 + 后台节流 `session_cache.save_debounce_seconds`，关停 flush 一次

**Prompt 优化**：system prompt 按"稳定性"排——人格 / 流水线 / 自检放最前面，
按事件变化的（图片、强制回复、软触发、消息上下文）放后面，配合 OpenAI 系
prompt cache 前缀命中可让输入 token 走 0.1× 价。

历史攒到 12 条（≈6 轮）后自动**跳过教学型例句**（节省约 30~40% system token），
冷会话依旧挂全套例句保证起手风格。

Bot owner（`qq.owner_qq` 配置的 QQ 号）在**私聊里**才能列会话（群里发同样的话不响应）：

```text
ai 会话列表
ai 列会话
/sessions
```

---

## 本地 reply gate & 训练（可选）

**`local_critic`** 默认关闭，回复取舍主要交给主 AI。需要时可接 OpenAI 兼容的本地小模型
（例如 Ollama 的 `qwen2.5:1.5b`），默认 `mode=reply_gate_only`，只做入口粗筛。

- @、回复、前缀、私聊、明显喊名属于硬触发，直接进主 AI，不等本地模型
- 实时 gate 通过 `think=false` / 独立短 token / 独立短 timeout 控制在 1~5 秒
- 超时 / 坏 JSON 自动退回硬判断 fallback
- `warmup_enabled=true` 时启动通过 Ollama 原生 `/api/generate` 空 prompt 预热，
  按 `warmup_interval_seconds` 刷新 `warmup_keep_alive`，减少冷启动

**`local_training`**：可把 reply gate 样本和主模型上下文 / 回复同时收集成数据集。
- 走 `audit_ai`（审核 / 判断 / 训练成果审批）做成果验收，审核模型只输出
  `allow_apply / allow_merge / next_suggestions` JSON，不直接执行命令
- `auto_fill_training_commands=true` 时，空 train 命令会落到项目内安全 wrapper
  `scripts/local_lora_train.py`，wrapper 只跑配置里指定的 `backend_command`，不让主 AI 跑任意 shell
- 闲时判定：默认按本地时间 + 安静时间窗，或开 `mc_idle_check_enabled` 走
  **MC Server List Ping**——MC 有人在线绝不训，无人持续 `mc_idle_min_minutes` 才允许启训
- MC 不可达按"不训"处理，保护游戏不被误抢资源

**项目内 Ollama**：`ollama.enabled=true` 时启动会自动部署便携 Ollama 到 `tools/ollama`，
模型放到 `models/ollama`（**两者必须在项目内**，避免把模型装出项目）。
手动同样部署：

```powershell
.\scripts\start_ollama_local.ps1
```

加 `-SkipPull` 跳过下载默认模型。

**训练 MCP server**：`scripts/catty_training_mcp_server.py` 可作为 stdio MCP server，
提供 `training_status` 和 `training_config_summary` 两个工具供外部 MCP 客户端查询，
不会返回 API key。

**训练进度小窗**：`local_training.progress_window_enabled=true` 时启动
`scripts/catty_training_dashboard.py` 的 Tk 小窗，可视化样本数 / 最近训练状态 / 审批结果 /
`next_suggestions` / 训练日志，还能用主 AI 的 `chat.system_prompt` 测本地小模型，
打分写入 `model_test_scores_path`。窗口不执行训练命令，也不展示 API key。

---

## 热重载与一键启动

`start_catty.bat` 默认先起 `scripts/catty_hot_reload.py` 守护，再由守护进程拉起 `bot.py`。

| 文件 | 行为 |
| --- | --- |
| `config.json` / `emojis/manifest.json` / `emojis/` / `memory.json` / `memory_groups/` / `memory_users/` | 运行中自动重读，**不重启** |
| `src/` / `scripts/` / `bot.py` / `catty_config_loader.py` / `catty_integrations.py` / `pyproject.toml` / `CattyQQAI.spec` / `README.md` | 触发子进程**自动重启** |

临时不想用守护：设环境变量 `CATTY_NO_HOT_RELOAD=1` 再跑 `start_catty.bat`。

热重载切 `MemoryStore` 实例前会自动 `flush_sync()` 旧实例，给新实例补起 `background_flush_loop`，
不会丢正在写盘的脏数据。

---

## 打包 exe

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[build]"
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean CattyQQAI.spec
```

产物在 `dist/CattyQQAI.exe`。把 `config.json` 和 `tools/napcat-onekey` 放在 exe 同目录即可。
启动时优先读取当前目录 / exe 同目录的 `config.json`；找不到会自动生成默认配置。

---

## 主人专属命令

只有 `config.json` 的 `qq.owner_qq`（= `catty_owner_qq`）匹配的 QQ 能用。
完整列表见 [ADMIN_COMMANDS.md](ADMIN_COMMANDS.md)，常用速查：

| 命令 | 作用 |
| --- | --- |
| `/aff_show <qq>` | 查那个用户的积分 / 等级 / 签到 / 消费记录 |
| `/aff_set_points <qq> <n>` | 设积分 |
| `/aff_add_points <qq> <n>` | 加 / 减积分（可负，下限 0） |
| `/aff_reset <qq>` | 清掉整本积分账户 |
| `/vibe_show [<qq>]` | 查用户 vibe 画像（不带 QQ 查自己） |
| `/vibe_reset <qq>` | 清掉用户画像 |
| `收藏` / `存表情` / `/saveemoji` | 把当前 / 引用 / 最近群图入表情库（可带 tag） |

Owner 本身（即 `qq.owner_qq` 配置的 QQ）积分恒为 `OWNER_INFINITY_POINTS`、等级恒为 `LEVEL_CAP`，免每日签到次数限制。

---

## 常用配置

完整配置请看 [config.example.json](config.example.json)；下表只挑最常调的：

| 路径 | 默认值 | 说明 |
| --- | --- | --- |
| `server.host` / `server.port` | `127.0.0.1` / `8080` | NoneBot 监听 |
| `qq.onebot_reverse_ws_url` | `ws://127.0.0.1:8080/onebot/v11/` | NapCat 反向 WS |
| `qq.auto_start_napcat` | `true` | 启动时自动拉起 NapCat |
| `ai.base_url` / `ai.api_key` / `ai.model` | OpenAI / 空 / `gpt-4o-mini` | 主聊天模型 |
| `audit_ai.*` | 空 → 兜底 `ai.*` | 审核 / 判断 / 训练成果审批模型 |
| `vision.*` | 空 → 复用 `ai.*` | 图片识别模型 |
| `filter.group_batch_messages` / `group_batch_seconds` | `200` / `1200` | 普通群批量主动判断阈值 |
| `filter.anger_warn_threshold` / `anger_mute_threshold` | `60` / `100` | 怒气状态阈值（由主 AI 表现） |
| `chat.trigger_prefixes` | `["ai","AI","猫猫"]` | 群文字触发前缀 |
| `chat.directed_keywords` | `["你","猫猫","猫娘","看看",…]` | 群软触发词 |
| `chat.soft_directed_reply_probability` | `0.65` | 普通软触发的回复倾向 |
| `chat.direct_address_reply_probability` | `0.9` | 明显喊名的回复倾向 |
| `chat.history_turns` | `16` | 单会话保留上下文轮数 |
| `chat.reply_max_chars` | `1800` | 单条上限，超过自动切分 |
| `chat.reply_human_split_*` | — | 语义分段：开关 / 概率 / 最小长度 / 最大段数 / 延迟 |
| `session_cache.persistence_enabled` / `dir` / `max_sessions` | `true` / `sessions` / `200` | 会话持久化 + LRU |
| `memory.summary_interval_minutes` | `30` | 群语料压缩周期 |
| `memory.reply_boost_*` | — | 语料 / 摘要 / 画像够时的回复倾向加成 |
| `memory.user_titles` / `group_titles` / `group_user_titles` | `{}` | 称呼三层 |
| `memory.special_care_user_ids` / `group_special_care_user_ids` | `[]` / `{}` | 特别关心 |
| `game_context.star_resonance_group_ids` / `strinova_group_ids` | `[]` / `[]` | 游戏主题群 |
| `emoji.reply_probability` | `0.85` | AI 未选表情时自动补图概率 |
| `emoji.interest_threshold` / `save_interest_threshold` | `60` / `85` | 识图入库阈值 |
| `web_search.cooldown_seconds` / `engines` | `600` / `["google","bing"]` | 联网搜索冷却 / 引擎 |
| `proactive.max_daily_per_group` / `min_interval_minutes` | `5` / `120` | 主动冒泡上限 / 最小间隔 |
| `ollama.enabled` / `install_dir` / `models_dir` | `false` / `tools/ollama` / `models/ollama` | 项目内 Ollama |
| `local_critic.enabled` / `mode` | `false` / `reply_gate_only` | 本地小模型 reply gate |
| `local_training.enabled` / `mc_idle_check_enabled` | `false` / `false` | 本地训练 / MC 闲时判定 |
| `hot_reload.enabled` / `restart_on_code_change` | `true` / `true` | 热重载 / 代码改动自动重启 |


```json
{
  "ai_local_backup": {
    "_note": "复制覆盖到 ai 节点即可切到本地 Qwen2.5-7B",
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key": "ollama",
    "model": "qwen2.5:7b",
    "max_tokens": 4096,
    "extra_body": {"think": false}
  }
}
```

### `ai.extra_headers` / `extra_body` 示例

```json
{
  "ai": {
    "extra_headers": {
      "HTTP-Referer": "https://example.com",
      "X-Title": "Catty QQ AI"
    },
    "extra_body": { "top_p": 0.9 }
  }
}
```

---

## 资料来源

- NoneBot2：<https://github.com/nonebot/nonebot2>
- NoneBot OneBot 适配器：<https://github.com/nonebot/adapter-onebot>
- OneBot 连接配置文档：<https://onebot.adapters.nonebot.dev/docs/guide/setup/>
- NapCatQQ OneBot 网络基础：<https://www.napcat.wiki/onebot/network>
- OpenAI Chat Completions API：<https://platform.openai.com/docs/api-reference/chat/create-chat-completion>

---

> 1.0.0 整版的笨猫长好啦~ 部署完先用小号测一轮，别一上来就把她拉去主力群里炸毛喵 ฅฅ
