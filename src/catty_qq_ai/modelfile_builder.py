"""Compose Catty's persona into an Ollama Modelfile.

The Ollama Modelfile lets us bake the entire system prompt into a derived
model (e.g. ``catty-7b``). Calls then send only ``user``/``assistant``
messages without any system role, so:

* Per-request prompt drops from ~5000 tokens to a few hundred.
* Ollama can reuse the cached KV state of the SYSTEM block across requests
  (no re-prefill until the model is unloaded).

This module ONLY writes the Modelfile content. ``catty_integrations`` runs
``ollama create`` and tracks signature for diff-based rebuild.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .persona_prompts import (
    CHARACTER_SYNC_PROMPT,
    IDENTITY_ANCHOR_PROMPT,
    NSFW_RULE_SYNC_PROMPT,
)


# 紧凑核心规则:身份/口吻/长度/NO_REPLY/傲娇骨架五件事压成 5 条
_COMPACT_CORE_RULES = (
    "精简核心规则(本地兜底模型用):\n"
    "1. 你就是米雪儿/笨猫/猫猫,用户叫'你/猫猫/笨猫/米雪儿'就是叫你本人,第一人称回应。\n"
    "2. 回复默认 1-3 句 QQ 口语短句,自然带'喵/嗷呜/爪爪/(动作)';用户问技术问题才放长。\n"
    "3. 傲娇骨架:先嘴硬'哼/才不/笨蛋'再不自觉露真心。\n"
    "4. 不是叫你/机器人自介/群里别人之间对话 → 只输出 <<<CATTY_NO_REPLY>>> 标记不要废话。\n"
    "5. 短回复保留核心信息(路径/命令/结论),不展开多余解释。"
)


def build_modelfile_content(
    base_model: str,
    system_prompt: str,
    *,
    num_ctx: int = 4096,
    temperature: float = 0.7,
    num_thread: int | None = None,
) -> str:
    """Compose a Modelfile string. SYSTEM block carries the full persona."""
    parts: list[str] = []
    user_prompt = (system_prompt or "").strip()
    if user_prompt:
        parts.append(user_prompt)
    parts.append(IDENTITY_ANCHOR_PROMPT)
    parts.append(CHARACTER_SYNC_PROMPT)
    parts.append(NSFW_RULE_SYNC_PROMPT)
    parts.append(_COMPACT_CORE_RULES)
    combined = "\n\n".join(parts).strip()
    # Modelfile uses triple-quoted strings for SYSTEM; escape any nested triple quotes
    escaped = combined.replace('"""', '\\"\\"\\"')

    lines = [
        f"FROM {base_model}",
        f"PARAMETER temperature {temperature}",
        f"PARAMETER num_ctx {int(num_ctx)}",
    ]
    if num_thread is not None and num_thread > 0:
        lines.append(f"PARAMETER num_thread {int(num_thread)}")
    lines.append('SYSTEM """')
    lines.append(escaped)
    lines.append('"""')
    lines.append("")
    return "\n".join(lines)


def content_signature(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_modelfile_if_changed(path: Path, content: str) -> bool:
    """Write content only if different. Returns True if file changed."""
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = None
    if existing == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return True


def signature_file_for(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def read_recorded_signature(path: Path) -> str:
    sig_path = signature_file_for(path)
    try:
        return sig_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_recorded_signature(path: Path, sig: str) -> None:
    sig_path = signature_file_for(path)
    sig_path.parent.mkdir(parents=True, exist_ok=True)
    sig_path.write_text(sig, encoding="utf-8")
