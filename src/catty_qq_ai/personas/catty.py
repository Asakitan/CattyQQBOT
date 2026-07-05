"""catty(笨猫)Persona 实例 — 默认人格。

**关键约束**: 所有内容字段保持 None → prompt 管线走现有常量/builder 老路径,
catty 的 cache prefix 逐字节不变(Step 2 用 sim dry-run byte-diff 验收)。
这里绝不复制任何人格文本。
"""
from __future__ import annotations

from . import Persona

CATTY_PERSONA = Persona(
    name="catty",
    char_name="笨猫",
    # 全部内容字段 None = 用 catty_core_persona / character_card / persona_prompts 现有内容
    owner_concept=True,
    reply_gate_style=None,  # None → reply_gate prompt 用原文(含"用笨猫口吻")
)
