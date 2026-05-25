"""SillyTavern 风 PromptManager - 带 identifier / order / enabled 的 system prompt 注册器。

这一刀只接「新建立的 ST 风模块」(daily_life / world_info / story_arc / author_note /
character_card 等),不动现有 persona_prompts.py 那 30+ 散装 append 链(后续按需迁移)。

设计目标:
- 每条 prompt 段有唯一 identifier + 排序 order,运行时按 order 升序合并
- 通过 config.catty_prompt_order 可以覆盖默认 order(JSON 配,不改代码)
- 通过 config.catty_prompts_disabled 可以单独关任意段
- 段内容用 lazy callable 提供(只有被 enable 才计算,省钱)
- 输出 list[{"role": "system", "content": ...}] 直接 extend 进 messages

参考 ST default Default.json 的 prompt_order:
    main → worldInfoBefore → personaDescription → charDescription → charPersonality
    → scenario → enhanceDefinitions → nsfw → worldInfoAfter → dialogueExamples
    → chatHistory → jailbreak

我们这版的命名(尽量贴近 ST 但落地到 catty 现状):
    catty_persona_core   — 现有 persona_prompts 集合(占位,order=10,本期不真接)
    catty_daily_life     — daily_life.build_daily_life_prompt (order=200)
    catty_world_info     — world_info.build_world_info_block (order=300)
    catty_story_arc      — story_arc.build_story_arc_prompt (order=350)
    catty_author_note_*  — author_note 走 inject_author_note 不走 PromptManager
                           (因为它要插入到 chat history 里,不是 system 段)

JSON 配置示例(config.json 顶层):
    {
      "prompt_order": ["catty_persona_core", "catty_daily_life", "catty_world_info",
                       "catty_story_arc"],
      "prompts_disabled": ["catty_daily_life"]   // 关掉日常生活感
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PromptEntry:
    identifier: str
    order: int = 100
    role: str = "system"
    enabled: bool = True
    content_fn: Callable[[], str] | None = None  # lazy 计算,启用时才跑

    def materialize(self) -> dict[str, str] | None:
        """生成 {"role": ..., "content": ...} 或 None(空内容/失败)。"""
        if not self.enabled or self.content_fn is None:
            return None
        try:
            content = self.content_fn()
        except Exception:  # noqa: BLE001
            return None
        if not content or not str(content).strip():
            return None
        return {"role": self.role, "content": str(content)}


class PromptManager:
    """register-then-build 模式。每次 build 调用都新建一份 entries 列表。

    不持久化、不全局单例 — __init__.py handle_chat 每轮 new 一个 manager,
    register 完之后 build_messages() 拿到结果。
    """

    def __init__(self) -> None:
        self._entries: list[PromptEntry] = []

    def register(
        self,
        identifier: str,
        content_fn: Callable[[], str],
        *,
        order: int = 100,
        role: str = "system",
        enabled: bool = True,
    ) -> None:
        self._entries.append(PromptEntry(
            identifier=identifier, order=order, role=role,
            enabled=enabled, content_fn=content_fn,
        ))

    def apply_config(
        self,
        *,
        order_override: list[str] | None = None,
        disabled: list[str] | None = None,
    ) -> None:
        """根据 config.json 调 order 和 enabled。

        order_override 是 identifier 列表,出现在列表里的按列表位置(乘 100)排序,
        没出现的保持原 order。disabled 里的 identifier 直接 enabled=False。
        """
        disabled_set = {s.strip() for s in (disabled or []) if str(s).strip()}
        if disabled_set:
            for e in self._entries:
                if e.identifier in disabled_set:
                    e.enabled = False
        if order_override:
            # 列表位置 i → order = (i+1)*100 (用 100 步长留空间手动微调)
            position_map = {ident.strip(): (i + 1) * 100 for i, ident in enumerate(order_override) if str(ident).strip()}
            for e in self._entries:
                if e.identifier in position_map:
                    e.order = position_map[e.identifier]

    def build_messages(self) -> list[dict[str, str]]:
        """按 order 升序输出非空 entry 的 system messages。"""
        sorted_entries = sorted(self._entries, key=lambda e: (e.order, e.identifier))
        out: list[dict[str, str]] = []
        for entry in sorted_entries:
            mat = entry.materialize()
            if mat is not None:
                out.append(mat)
        return out

    def __len__(self) -> int:
        return sum(1 for e in self._entries if e.enabled)


__all__ = [
    "PromptEntry",
    "PromptManager",
]
