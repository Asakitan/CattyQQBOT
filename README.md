# Catty QQ AI

> 主人好呀，我是住在 QQ 里的猫猫小女仆喵 ฅฅ
> 平时陪大家聊天、记住熟人和群里的趣事，也会看图、发语音、查资料、画画和做文件。
> 想换个伙伴一起玩也可以：除了笨猫和机机，主人还能给我添加自己喜欢的人格喵～

Catty 基于 NoneBot2 + OneBot v11，接入支持 Chat Completions、Responses 或兼容接口的模型服务。各项能力都可以按需要配置，陪聊和干活一起来喵。

---

## 目录

- [架构](#架构)
- [快速开始](#快速开始)
- [接入 QQ（NapCat）](#接入-qqnapcat)
- [人格与对话](#人格与对话)
- [记忆与会话](#记忆与会话)
- [图片、表情与语音](#图片表情与语音)
- [工具、联网与技能](#工具联网与技能)
- [主动冒泡](#主动冒泡)
- [模型线路与缓存](#模型线路与缓存)
- [可选能力](#可选能力)
- [运行与热重载](#运行与热重载)
- [离线回归测试](#离线回归测试)
- [打包 exe](#打包-exe)
- [主人专属命令](#主人专属命令)
- [常用配置](#常用配置)
- [项目文档](#项目文档)

---

## 架构

| 组件 | 作用 |
| --- | --- |
| **NapCatQQ** | 帮猫猫登录 QQ，用 OneBot v11 收发消息 |
| **NoneBot2** | 接收 NapCat 的消息，加载 `catty_qq_ai` 插件 |
| **catty_qq_ai** | 管理人格、记忆、图片、语音、工具和会话 |
| **模型服务** | 负责聊天和其他 AI 任务；主回复、识图与后台任务可以分开配置 |

> 初次接入可以先用 QQ 小号试一试，确认消息收发正常再邀请猫猫进群喵。

---

## 快速开始

先准备 **Python 3.10+**，在项目目录里创建环境、安装依赖喵：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .
```

接着复制一份示例配置，已有配置会保留：

```powershell
if (-not (Test-Path .\config.json)) { Copy-Item .\config.example.json .\config.json }
```

打开 `config.json`，填好 `ai.base_url`、`ai.api_key` 和 `ai.model`。让猫猫自动启动 NapCat 的话，也填上 `qq.account`；主人专属命令使用 `owner_forward.owner_qq` 识别主人。下面是模型配置的写法：

```json
{
  "ai": {
    "base_url": "https://HOST/v1",
    "api_key": "TOKEN",
    "model": "TARGET"
  }
}
```

把 `HOST`、`TOKEN`、`TARGET` 换成自己的服务地址、密钥和模型名；使用 Responses 时可以填写 `/v1/responses` 地址喵。

准备好以后，启动守护进程：

```powershell
.\.venv\Scripts\python.exe .\scripts\catty_hot_reload.py
```

猫猫默认在 `127.0.0.1:8080` 等 NapCat 连过来。守护进程会启动 `bot.py`，照看热重载、崩溃重启和心跳；调试时也可以直接运行 `python bot.py`。

> Windows 下也可以运行 `install_python_env.bat`，让它创建环境、安装依赖并生成 `start_catty.bat`，以后双击启动就好喵。安装脚本会覆盖同名启动文件，成功后会删除自身。

---

## 接入 QQ（NapCat）

先给猫猫准备一套 NapCat。可以使用自己安装的版本，也可以运行下面的脚本下载 Windows 一键包：

```powershell
.\scripts\update_napcat_onekey.ps1
```

脚本会重新创建 `tools/napcat-onekey`，已有安装请先备份喵。

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

`auto_start_napcat=true` 时，猫猫启动时会帮忙拉起 NapCat；填了 `qq.account` 且 `napcat_args` 留空时，会自动带上 QQ 号。想自己启动 NapCat，就把 `auto_start_napcat` 改成 `false`。

---

## 人格与对话

现在家里住着 `catty`（笨猫）和 `fadianji`（机机），示例配置默认叫机机出来聊天。每个会话可以选自己喜欢的人格，也可以给群设置专属人格喵。

- **私聊**：直接发消息就能聊。**群聊**：叫名字、@ 或引用机器人，就能把对应人格叫出来；接上话以后，相关的后续消息也能继续聊。
- 想找机机时，叫“机机”“小机”或“发电机”都可以。遇到意思不太明确的呼唤和续聊，猫猫会结合上下文判断要不要接话。
- 群友一起复读时，猫猫也会跟着玩；聊天短句可以自然分成几条气泡，技术解释和长文则保留排版。
- 主人可以导入 JSON 人格，或描述设定让模型帮忙创建。保存后再用 `/人格 <名字>` 切换；`/人格` 可以查看当前选择。详细操作见 [人格定义](docs/persona-json.md) 和 [管理命令](ADMIN_COMMANDS.md)。

---

## 记忆与会话

猫猫会整理群摘要、群友画像和熟悉的称呼，聊天时再把相关记忆找出来。人物资料可以跨人格共享，场景状态和各自的近期对话则按会话、人格分别保存喵。

- 会话记录默认放在 `sessions/`；长期记忆分布在 `memory.json`、`memory_groups/`、`memory_users/` 等目录，重启后也能接着用。
- 值得记住的事件、计划和约定会进入显著记忆或目的账本，给之后的聊天和主动冒泡提供线索。
- 称呼、特别关心、摘要周期和数据备份可以在 `memory` 段调整；主人也能用 [管理命令](ADMIN_COMMANDS.md) 查看和整理资料。

`config.json` 和运行数据里可能有密钥或私人聊天，留在自己的机器上就好喵。

---

## 图片、表情与语音

- **看图**：当前图片、引用的图片和近期聊天里的图片都可以结合上下文来看；`vision.context_*` 控制回看的范围。同一识图和主回复线路可以把图片直接交给主模型喵。
- **表情与画画**：猫猫会根据聊天气氛挑表情，也能按配置搜索图片、生成或加工图片。搜图时会保留来源并检查候选内容，选好后再安排发送。
- **语音回复**：`voice` 可以使用现成语音库 `bank`，也可以接 TTS `worker` 念出新台词；不同人格能使用不同音色。主人说想听语音，或者碰上自动语音、语音气泡时，猫猫就有机会开口啦。
- **听语音**：`voice_stt` 用 faster-whisper 和 PyAV 转写可解码的语音，再把文字带进对话；原始 SILK 等暂时无法解码的音频会跳过。

TTS worker 和语音转写需要另外准备运行环境与模型；音色、参考音和路径都在 `voice` / `voice_stt` 段配置，详见 [示例配置](config.example.json) 和 [语音说明](docs/voice-candidates.md)。

---

## 工具、联网与技能

除了聊天，猫猫还会按当前开放的工具查网页、翻最近的消息、找图片、画图和做文件。图片、文件或语音做好后，有时会在文字回复之后送到当前会话喵。

- **联网**：`web_search` 负责搜索，`auto_fetch` 可以在需要时补充网页正文和配图；自动补全有独立的缓存和预算设置。
- **技能**：遇到任务时，猫猫可以按需搜索、学习和使用技能，公共知识会保存在 `data/skills/`，以后也能接着用。
- **文件与代码**：`catty_write_file` 保存文本文件，`catty_python_run` 在独立工作目录运行 Python，并把成功生成的文件交回来。Python 工作区使用用户态隔离，详细边界见 [Python 沙箱](docs/code-sandbox.md)。

工具会根据配置、人格和权限开放；想看看具体用法，可以翻 [工具合同](docs/tool-contracts.md) 和 [技能运行时](docs/skill-runtime.md)。

---

## 主动冒泡

群里热闹的时候，猫猫和机机也会挑合适的时机冒个泡，聊聊大家最近在说什么。每天的机会按群安排，具体何时开口会参考活跃时段和近期消息喵。

机机的开关是 `fdj_proactive.enabled`，笨猫的开关是 `proactive.enabled`；共享调度的时间和节奏在 `fdj_proactive` 里调整。详细规则见 [主动调度](docs/fadianji-proactive.md)。

---

## 模型线路与缓存

想把不同工作交给不同模型的话，可以在配置里分工喵：`ai` 负责主回复，`vision` 负责识图，`filter` / `audit_ai` 负责判断和审核，`ai_fallback` 可以处理后台总结与会话压缩。人格生成、语气进化和技能提炼会依次选择可用的 `audit_ai`、`ai_fallback` 或主 `ai`。

Responses 支持 WebSocket 连接和 `previous_response_id` 增量续聊，也可以按配置回退 HTTP。符合条件的官方 Responses 主请求还会根据会话活跃度选择缓存期限；设置项在 `ai.websocket_*`，细节见 [架构说明](docs/architecture.md)。

---

## 可选能力

- **本地语言处理**：`nlu` 可以接 jieba、text2vec 和 HanLP。示例默认关闭；需要时用 `pip install -e ".[nlu]"` 安装依赖，再准备对应模型。
- **本地模型与训练**：项目保留 Ollama 和训练相关配置，示例里的 `ollama.enabled`、`local_training.enabled` 默认关闭，需要时再按环境启用。
- **源码自修复**：打开 `self_repair.enabled` 后，可以根据日志生成源码候选并跑固定验证；`auto_apply` 控制是否交给守护进程应用，健康检查失败时会回滚。示例默认关闭，候选验证会在本机执行 Python，开启前可以先看看 [自修复说明](docs/self-repair.md)。

---

## 运行与热重载

日常运行时，`scripts/catty_hot_reload.py` 会先照看好环境，再拉起 `bot.py`。安装脚本生成的 `start_catty.bat` 也是走这条线路喵。

| 文件 | 行为 |
| --- | --- |
| `config.json` / `emojis/manifest.json` / `emojis/` / `memory.json` / `memory_groups/` / `memory_users/` | 运行中自动重读，继续聊天 |
| `src/` / `scripts/` / `bot.py` / `catty_config_loader.py` / `catty_integrations.py` / `pyproject.toml` / `CattyQQAI.spec` / `README.md` | 触发子进程**自动重启** |

使用 `start_catty.bat` 时，设置 `CATTY_NO_HOT_RELOAD=1` 可以直接运行 `bot.py`，方便调试。重载记忆前猫猫会先保存待写内容；临时写入失败时留待下一轮重试。

---

## 离线回归测试

改完东西想检查一下的话，安装项目依赖后就可以跑离线清单喵：

```powershell
python -B -X utf8 scripts/run_offline_regressions.py
```

清单按模块启动独立进程；只想检查几个模块时，可以重复使用 `--module`：

```powershell
python -B -X utf8 scripts/run_offline_regressions.py --module test_config --module test_config_loader
```

测试记录在 [当前状态](STATUS.md) 和 [会话记录](docs/session-log/README.md)；模型、语音与 QQ 的实际收发可以接好服务后再试喵。

---

## 打包 exe

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[build]"
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean CattyQQAI.spec
```

打包完成后，可以在 `dist/CattyQQAI.exe` 找到猫猫。运行前记得准备模型配置、OneBot 连接，以及需要使用的 NapCat 和语音环境。这个 exe 从 `bot.py` 启动；日常开发的守护进程则使用上面的脚本。

---

## 主人专属命令

先在 `config.json` 的 `owner_forward.owner_qq` 填上主人的 QQ，猫猫就知道这些管理命令该听谁的啦。完整列表见 [ADMIN_COMMANDS.md](ADMIN_COMMANDS.md)，常用的先摆在这里喵：

| 命令 | 作用 |
| --- | --- |
| `/人格`、`/人格 <名字>` | 查看当前人格 / 切换当前会话 |
| `/人格 导入 \| <JSON>` | 校验保存人格，不自动切换 |
| `/人格 生成 <id> \| <设定>` | 次要 AI 线路生成人格并保存 |
| `/人格 指令 <名字> \| <指令>` | 绑定完整消息作为切换指令 |
| `/aff_show <qq>` | 查那个用户的积分 / 等级 / 签到 / 消费记录 |
| `/aff_set_points <qq> <n>` | 设积分 |
| `/aff_add_points <qq> <n>` | 加 / 减积分（可负，下限 0） |
| `/aff_reset <qq>` | 清掉整本积分账户 |
| `/vibe_show [<qq>]` | 查用户 vibe 画像（不带 QQ 查自己） |
| `/vibe_reset <qq>` | 清掉用户画像 |
| `收藏` / `存表情` / `/saveemoji` | 把当前 / 引用 / 最近群图入表情库（可带 tag） |

---

## 常用配置

常用的设置都放在下面啦，完整字段和默认值可以翻 [config.example.json](config.example.json)，实际运行时读取自己的 `config.json`。密钥留在本机配置里就好喵。

| 配置段 | 用途 |
| --- | --- |
| `server` / `qq` / `owner_forward.owner_qq` | 监听地址、NapCat 连接与主人身份 |
| `ai` / `vision` / `filter` / `audit_ai` / `ai_fallback` | 主回复、识图、判门、审核和后台模型线路 |
| `persona` / `chat` / `memory` | 人格、聊天节奏、会话与长期记忆；会话缓存使用 `chat.session_cache_*` |
| `web_search` / `auto_fetch` / `skills` | 搜索、自动补网与共享技能 |
| `imagegen` / `emoji` / `voice` / `voice_stt` | 生图、表情、语音合成与转写 |
| `file_delivery` / `code_sandbox` | 文件交付与受限 Python 工作区 |
| `fdj_proactive` / `proactive.enabled` | 共享主动调度与人格开关 |
| `self_repair` / `hot_reload` | 可选源码自修复与守护进程 |
| `nlu` / `ollama` / `local_training` | 可选本地语言处理、模型和训练 |

想调整角色设定，可以从人格模块或自定义人格定义入手喵。

---

## 项目文档

- 看进度和测试：[当前状态](STATUS.md)、[会话记录](docs/session-log/README.md)。
- 看猫猫怎么工作：[架构说明](docs/architecture.md)、[模块清单](docs/module-inventory.md)。
- 管理和创作角色：[管理命令](ADMIN_COMMANDS.md)、[人格定义](docs/persona-json.md)、[人格运行时](docs/persona-runtime.md)。
- 使用工具：[工具合同](docs/tool-contracts.md)、[Python 沙箱](docs/code-sandbox.md)、[技能运行时](docs/skill-runtime.md)、[自修复](docs/self-repair.md)。

想读代码的话，可以从 [bot.py](bot.py)、[配置加载器](catty_config_loader.py)、[聊天插件](src/catty_qq_ai/__init__.py)、[模型客户端](src/catty_qq_ai/openai_client.py) 和 [守护进程](scripts/catty_hot_reload.py) 开始喵。

> 仓库推送负责同步 Git；运行中的服务器请使用自己的部署流程。主人准备好以后，猫猫就在 QQ 等你啦喵～
