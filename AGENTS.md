# Catty Repository Memory

给 Codex / Copilot / Claude 这类代码助手用的项目导览。**进项目时先扫这一份**，不要每次让主人重新介绍。

---

## Project Overview

这是 **笨猫 (Catty)** —— 一个 NoneBot2 QQ 聊天 AI 插件，主要文件结构：

- 入口：[bot.py](bot.py) —— 读 `config.json` 灌环境变量 → `nonebot.init()` → 加载 `catty_qq_ai` 插件 → 启动 NapCat / Ollama 等集成进程
- 协议端：NapCatQQ（OneBot v11 反向 WS，`ws://127.0.0.1:8080/onebot/v11/`）
- AI 接口：任何 OpenAI-compatible Chat Completions（OpenAI / OpenRouter / vLLM / Ollama OpenAI 端点等）
- 包结构：`src/catty_qq_ai/` 是 nonebot plugin 包；根目录的 `catty_config_loader.py` / `catty_integrations.py` 是 bot.py 直接 import 的辅助模块

## Key Modules（速查）

| 文件 | 干什么的 |
|------|----------|
| [src/catty_qq_ai/__init__.py](src/catty_qq_ai/__init__.py) | nonebot plugin 主入口；所有 message handler、热重载循环、主动冒泡、summary loop、本地 critic warmup 都在这里。**1.6w 行的大件，按区域定位** |
| [src/catty_qq_ai/config.py](src/catty_qq_ai/config.py) | `Config` pydantic 类，所有 `catty_*` 字段；从环境变量读取 |
| [src/catty_qq_ai/openai_client.py](src/catty_qq_ai/openai_client.py) | `chat_completion` 主路由、Ollama 原生协议、云端健康度跟踪。**注意：本地 fallback 已在 `_fallback_is_configured` 硬关闭，不再走本地兜底** |
| [src/catty_qq_ai/memory.py](src/catty_qq_ai/memory.py) | `MemoryStore`：人物画像 / 群摘要 / 怒气值 / 内容温度 / 主动冒泡 / 特别关心。落盘走 debounce + atomic write |
| [src/catty_qq_ai/session_cache.py](src/catty_qq_ai/session_cache.py) | 每群/私聊一个对话窗口，LRU + 持久化 `sessions/*.json`，debounce flush + atomic write |
| [src/catty_qq_ai/persona_prompts.py](src/catty_qq_ai/persona_prompts.py) | 笨猫人格 prompt。**改动前务必读现有文本，跑 `python -m py_compile` 再保存** |
| [src/catty_qq_ai/emoji_store.py](src/catty_qq_ai/emoji_store.py) | 表情包检索（语义匹配） |
| [src/catty_qq_ai/legs_picker.py](src/catty_qq_ai/legs_picker.py) | 腿图素材选择 |
| [src/catty_qq_ai/mc_status.py](src/catty_qq_ai/mc_status.py) | Minecraft 服务器在线状态查询（被 fallback gate 用） |
| [src/catty_qq_ai/web_search.py](src/catty_qq_ai/web_search.py) / [nsfw_search.py](src/catty_qq_ai/nsfw_search.py) | Google/Bing 搜索 / NSFW 搜索 |
| [src/catty_qq_ai/owner_forward.py](src/catty_qq_ai/owner_forward.py) | 主人转发模式 |
| [src/catty_qq_ai/reply_markers.py](src/catty_qq_ai/reply_markers.py) | `NO_REPLY_MARKER` 等控制标记常量 |
| [catty_config_loader.py](catty_config_loader.py) | `config.json` → 环境变量 + 启动集成进程 |
| [catty_integrations.py](catty_integrations.py) | NapCat / Ollama 启动与监管 |
| ~~scripts/_local_push_pack.py~~ | **已废弃并删除**——之前的命令行部署脚本，硬编码 token + 维护 FILES 列表很糙；现在统一走 Studio 的推送接口（主人手动操作） |

## Common Commands

| 任务 | 命令 |
|------|------|
| 启动 bot | `python bot.py` |
| 跑测试 | `python -c "import sys; sys.path.insert(0, 'src'); import unittest; unittest.main(module=None, argv=['', 'discover', 'tests'])"`（项目没 pytest，用 stdlib unittest） |
| 单文件语法 check | `python -m py_compile src/catty_qq_ai/<file>.py` |
| 推到生产服务器 | 通过 Studio 的推送接口操作 ，推送完成后进行Commit |
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
- 推到生产由**主人通过 Studio 的推送接口手动完成**，AI 助手不直接执行部署
- 不要用 `git diff` / `git status` 推断"要推什么"——主人本地修改可能没及时 commit，git 视角的 "modified" 不等于"待部署清单"
- **推送完主人会让助手 `git commit` 一次**，把当时推到服务器的代码状态归档到 git，方便日后对照

> 历史：项目曾经有一个 `scripts/_local_push_pack.py` 命令行脚本走 HTTP zip 上传，但因为 token 硬编码 + FILES 清单维护麻烦已废弃删除。

## Prompt Ownership

- 用户手动修改的人格、提示词、角色设定、NSFW/敏感处理文案属于高优先级项目意图；Codex 不要擅自改写成"安全边界版"或替换成另一套价值判断。
- 如果这些内容导致 Python/JSON 语法错误、导入失败、运行失败，只做最小必要修复，例如转义引号、修正字符串拼接、保持原文语义；不要趁修语法时重写设定。
- 如果内容和更高优先级系统/安全规则冲突，先说明冲突和可执行的最小替代方案，再修改；不要静默改写。
- 对 [src/catty_qq_ai/persona_prompts.py](src/catty_qq_ai/persona_prompts.py)、`config.json`、`config.example.json` 中的人格 prompt 改动尤其谨慎，修改前先读现有文本，修改后跑 `python -m py_compile src/catty_qq_ai/persona_prompts.py` 和相关测试。
- 当前项目额外偏好：猫猫默认回复要更可爱、更黏人、更像 QQ 现聊；普通闲聊尽量压到 1-2 句短句，只有解释题目、技术问题、方案和排错时才放开写长。
