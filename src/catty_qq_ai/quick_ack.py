"""长任务先导短回复 (主人 2026-10-25).

查资料 / 画图 / 做题这类必然超过 30 秒的请求, 不再套固定开场 (「我去翻翻」),
改成开跑工具循环之前先让模型现写一句 4-20 字的即时反应, 再由主流程继续深想。

模块只做纯逻辑 (意图判定 / prompt 拼装 / 输出净化), 无网络与 nonebot 依赖。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from .reply_markers import NO_REPLY_MARKER, strip_tone_parenthetical


KIND_DRAW = "draw"
KIND_SOLVE = "solve"
KIND_LOOKUP = "lookup"

KIND_LABELS: dict[str, str] = {
    KIND_DRAW: "画图/生图",
    KIND_SOLVE: "做题/解题",
    KIND_LOOKUP: "查资料",
}

_DRAW_HINTS = (
    "画一张", "画张", "画个", "画一", "画幅", "帮我画", "给我画", "生图", "出图",
    "立绘", "来张", "来一张", "壁纸", "表情包", "做个头像", "插画", "出个图",
)
_SOLVE_HINTS = (
    "做题", "解题", "作业", "题目", "这道题", "这题", "求解", "证明", "计算", "算出",
    "方程", "函数", "导数", "积分", "概率", "几何", "代数", "数学题", "物理题",
    "化学题", "算法题", "编程题", "报错", "调试", "写代码", "改代码", "写个脚本",
)
_LOOKUP_HINTS = (
    "查一下", "查查", "帮我查", "搜一下", "搜搜", "搜下", "百度", "谷歌", "谷歌一下",
    "资料", "攻略", "教程", "最新", "新闻", "热搜", "版本", "价格", "多少钱",
    "什么时候", "上线", "发售", "测评", "参数", "配置",
)
_WRITE_HINTS = (
    "写成文件", "写个文件", "写一份文件", "生成文件", "导出", "保存成", "存成",
    "写成文档", "整理成文档", "做成文档", "整理成文件", "保存为",
    "写个表格", "生成表格", "整理成表格", "写个报告", "写份报告", "生成报告",
    "文件发我", "发我文件", "导出成", "txt", "md文件", "csv", "markdown",
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# 固定套话 / 后台过程词: 命中就丢掉重写 (宁可这次不发, 也不回到模板味)
_FIXED_PHRASES: tuple[str, ...] = (
    "我去翻翻", "我查一下", "我搜一下", "我去查", "我去搜", "等等哦", "稍等",
    "等一下", "等下", "马上回来", "马上就好",
)
_PROCESS_WORDS: tuple[str, ...] = (
    "联网", "搜索", "知识库", "工具", "人工智能", "ai", "http",
)


def is_write_task(text: str, allowed_tool_names: Iterable[str] = ()) -> bool:
    """写文件任务不发系统先导/占位短回复, 由主模型本轮直接回复。"""
    allowed = {str(name) for name in allowed_tool_names}
    if "catty_write_file" not in allowed:
        return False
    lowered = str(text or "").casefold()
    return any(hint in lowered for hint in _WRITE_HINTS)


def detect_task_kind(
    text: str,
    allowed_tool_names: Iterable[str] = (),
    *,
    directed: bool = True,
    force_imagegen: bool = False,
) -> tuple[str, str]:
    """判断这句是不是长任务。返回 ``(kind, 中文任务名)``; 空串 = 不发先导回复。"""
    if not directed:
        return "", ""
    clean = str(text or "").strip()
    if len(clean) < 2:
        return "", ""
    allowed = {str(name) for name in allowed_tool_names}
    lowered = clean.casefold()
    # 写文件任务不发系统先导短回复, 由主模型在本轮直接回复, 回完再发文件。
    if is_write_task(clean, allowed):
        return "", ""
    imagegen_ready = force_imagegen or "catty_imagegen" in allowed
    if imagegen_ready and any(hint in lowered for hint in _DRAW_HINTS):
        return KIND_DRAW, KIND_LABELS[KIND_DRAW]
    if any(hint in lowered for hint in _SOLVE_HINTS):
        return KIND_SOLVE, KIND_LABELS[KIND_SOLVE]
    if _URL_RE.search(clean) and "catty_fetch_url" in allowed:
        return KIND_LOOKUP, KIND_LABELS[KIND_LOOKUP]
    if "catty_web_search" in allowed and any(hint in lowered for hint in _LOOKUP_HINTS):
        return KIND_LOOKUP, KIND_LABELS[KIND_LOOKUP]
    return "", ""


def build_prompt(
    *,
    char_name: str,
    style: str,
    kind_label: str,
    user_text: str,
    max_chars: int = 24,
) -> list[dict[str, str]]:
    """给 codex_instant 的短先导回复 prompt。"""
    style_line = f" {style}" if style else ""
    system = (
        f"你在扮演{char_name}。{style_line}\n"
        f"用户刚提了一个要花时间处理的任务 ({kind_label})。先用一句很短的即时反应接住,"
        "然后你会继续认真处理, 稍后再给正式结果。\n"
        "硬性要求:\n"
        f"1) 只输出一句中文正文, 4-{max(max_chars, 8)} 字, 不换行, 不带引号/前缀/解释;\n"
        "2) 每次现写, 禁止用『我去翻翻/我查一下/等等哦/稍等』这类固定套话;\n"
        "3) 不承诺结果、不剧透答案、不提工具/联网/搜索/AI;\n"
        "4) 不用括号动作或语气注解。\n"
    ).strip()
    user = f"用户原话: {str(user_text or '').strip()[:80]}\n任务: {kind_label}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_line(line: str, *, max_chars: int = 24) -> str:
    """净化模型输出: 单行 / 限长 / 无固定套话 / 无后台过程词; 不合格返回空串。"""
    text = str(line or "").replace(NO_REPLY_MARKER, "").strip()
    if not text:
        return ""
    if "\n" in text:
        text = text.splitlines()[0].strip()
    text = text.strip(" \t\"'“”‘’`")
    text = strip_tone_parenthetical(text).strip()
    if len(text) < 2 or len(text) > max(max_chars, 8):
        return ""
    lowered = text.casefold()
    if any(phrase in lowered for phrase in _FIXED_PHRASES):
        return ""
    if any(word in lowered for word in _PROCESS_WORDS):
        return ""
    return text


def pick_line(candidates: Sequence[str], *, max_chars: int = 24) -> str:
    """多个候选里取第一条合格的 (模型偶尔一次给多行)。"""
    for candidate in candidates:
        cleaned = validate_line(candidate, max_chars=max_chars)
        if cleaned:
            return cleaned
    return ""
