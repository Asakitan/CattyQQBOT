# Catty Repository Memory

## Prompt Ownership

- 用户手动修改的人格、提示词、角色设定、NSFW/敏感处理文案属于高优先级项目意图；Codex 不要擅自改写成“安全边界版”或替换成另一套价值判断。
- 如果这些内容导致 Python/JSON 语法错误、导入失败、运行失败，只做最小必要修复，例如转义引号、修正字符串拼接、保持原文语义；不要趁修语法时重写设定。
- 如果内容和更高优先级系统/安全规则冲突，先说明冲突和可执行的最小替代方案，再修改；不要静默改写。
- 对 `src/catty_qq_ai/persona_prompts.py`、`config.json`、`config.example.json` 中的人格 prompt 改动尤其谨慎，修改前先读现有文本，修改后跑 `python -m py_compile src/catty_qq_ai/persona_prompts.py` 和相关测试。
- 当前项目额外偏好：猫猫默认回复要更可爱、更黏人、更像 QQ 现聊；普通闲聊尽量压到 1-2 句短句，只有解释题目、技术问题、方案和排错时才放开写长。
