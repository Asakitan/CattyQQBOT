"""LaTeX 块识别 + matplotlib mathtext 渲染成 PNG，供 QQ 图片消息段使用。

设计要点：
- 入口 ``extract_segments(text)`` 把回复拆成 ``[Segment("text", ...), Segment("latex", ...), ...]`` 序列。
- ``render_latex_to_base64_uri(latex)`` 把 LaTeX 源码渲染成 ``base64://...`` URI，可直接喂给
  ``MessageSegment.image(file=...)``。
- matplotlib 是惰性 import（顶层包在 try-except 里）：如果服务器没装，``extract_segments``
  仍然工作但 ``render_latex_to_base64_uri`` 会抛 ``LatexRenderUnavailable``，调用方应
  fallback 回原文 plain text，让 bot 不会因为渲染失败而崩。
- matplotlib 自带 mathtext 支持常见 LaTeX 子集（分数 \\frac、根号 \\sqrt、积分 \\int、求和 \\sum、
  极限 \\lim、希腊字母、上下标、希望、对齐符号 \\to/\\leq/\\geq 等）；array、tikz、自定义宏不支持。
  渲染失败抛 ``LatexRenderError``，调用方同样 fallback 原文。
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass
from typing import Literal


SegmentKind = Literal["text", "latex"]


@dataclass(slots=True)
class Segment:
    kind: SegmentKind
    content: str  # text 模式是原文；latex 模式是源码（不含定界符 \[ \] $$ 等）


class LatexRenderError(RuntimeError):
    """matplotlib mathtext 渲染失败（语法不支持/解析异常）。"""


class LatexRenderUnavailable(RuntimeError):
    """matplotlib 未安装。调用方应 fallback 回原文。"""


# 常见 LaTeX 包裹形式（按优先级匹配，避免 $ 和 $$ 互相吞）：
#   \[ ... \]    display math
#   \( ... \)    inline math
#   $$ ... $$    display math (旧版/Markdown 风格)
#
# 故意不匹配单个 $...$ —— 普通文本里 $ 出现概率不低（钱币符号、网站 placeholder），
# 误匹配代价比漏匹配大。AI 想用 inline math 就用 \(...\)。
_LATEX_BLOCK_RE = re.compile(
    r"\\\[(?P<bracket>.+?)\\\]"
    r"|\\\((?P<paren>.+?)\\\)"
    r"|\$\$(?P<dollar>.+?)\$\$",
    re.DOTALL,
)


def _strip_boxed(source: str) -> str:
    """把 ``\\boxed{X}`` 递归替换成 ``[X]``（mathtext 不识别 \\boxed）。

    支持任意 brace 嵌套（``\\boxed{\\frac{1}{6}}`` → ``[\\frac{1}{6}]``）。
    """
    boxed_token = r"\boxed{"
    result: list[str] = []
    i = 0
    while i < len(source):
        if source.startswith(boxed_token, i):
            j = i + len(boxed_token)
            depth = 1
            while j < len(source):
                ch = source[j]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth == 0:
                inner = _strip_boxed(source[i + len(boxed_token) : j])
                result.append("[" + inner + "]")
                i = j + 1
                continue
        result.append(source[i])
        i += 1
    return "".join(result)


def _normalize_latex(source: str) -> str:
    """把 AI 常用但 matplotlib mathtext 不支持的 LaTeX 命令降级成兼容形式。

    mathtext 是 LaTeX 子集，许多常用命令(``\\boxed``, ``\\dfrac``, ``\\text``)不支持。
    与其让渲染失败 fallback 回 raw LaTeX，不如做最小语义保留的预处理。
    """
    out = _strip_boxed(source)
    out = re.sub(r"\\dfrac\b", r"\\frac", out)
    out = re.sub(r"\\tfrac\b", r"\\frac", out)
    # \text{xxx} → \mathrm{xxx}(mathtext 支持 \mathrm 但 \text 不支持)
    out = re.sub(r"\\text\{([^{}]*)\}", r"\\mathrm{\1}", out)
    return out


_MATPLOTLIB_READY: bool | None = None  # 三态: None=未尝试 import, True=可用, False=不可用


def _ensure_matplotlib() -> "tuple[object, object]":
    """惰性 import matplotlib。失败抛 LatexRenderUnavailable。"""
    global _MATPLOTLIB_READY
    if _MATPLOTLIB_READY is False:
        raise LatexRenderUnavailable("matplotlib not installed")
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        _MATPLOTLIB_READY = False
        raise LatexRenderUnavailable(str(exc)) from exc
    _MATPLOTLIB_READY = True
    return matplotlib, plt


def extract_segments(text: str) -> list[Segment]:
    """把 ``text`` 拆成 [text, latex, text, ...] 序列。

    没找到 LaTeX 块时返回 [Segment("text", text)]（或空 list，如果 text 全是空白）。
    LaTeX 块内的源码会被 strip()，外层定界符 (\\[ \\], \\( \\), $$) 不进 segment。
    """
    if not text:
        return []
    segments: list[Segment] = []
    last_end = 0
    for match in _LATEX_BLOCK_RE.finditer(text):
        start, end = match.span()
        if start > last_end:
            prefix = text[last_end:start]
            if prefix.strip() or prefix:
                # 保留空白以便上层正确拼接,但末尾会在发送链路里 strip
                segments.append(Segment("text", prefix))
        latex_src = (
            match.group("bracket") or match.group("paren") or match.group("dollar") or ""
        ).strip()
        if latex_src:
            segments.append(Segment("latex", latex_src))
        last_end = end
    if last_end < len(text):
        tail = text[last_end:]
        if tail:
            segments.append(Segment("text", tail))
    if not segments:
        # 没有命中任何 LaTeX 块
        return [Segment("text", text)] if text.strip() else []
    return segments


def has_latex_blocks(text: str) -> bool:
    """便捷判断：text 里至少有一个 LaTeX 块。"""
    return bool(_LATEX_BLOCK_RE.search(text or ""))


def render_latex_to_png(latex_source: str, *, fontsize: int = 20, dpi: int = 180) -> bytes:
    """把 LaTeX 源码渲染成 PNG bytes（透明背景白底）。

    Raises:
        LatexRenderUnavailable: matplotlib 没装
        LatexRenderError: 渲染失败（语法不支持、解析异常）
    """
    source = (latex_source or "").strip()
    if not source:
        raise LatexRenderError("empty latex source")

    _, plt = _ensure_matplotlib()

    source = _normalize_latex(source)

    # mathtext 要求内容包在 $...$ 里；如果源码已经是 $...$ 形式就不重复包
    if source.startswith("$") and source.endswith("$") and len(source) >= 2:
        expression = source
    else:
        expression = f"${source}$"

    fig, ax = plt.subplots(figsize=(0.01, 0.01))
    try:
        ax.text(
            0.0,
            0.0,
            expression,
            fontsize=fontsize,
            ha="left",
            va="bottom",
            color="black",
        )
        ax.axis("off")
        buf = io.BytesIO()
        try:
            fig.savefig(
                buf,
                format="png",
                bbox_inches="tight",
                pad_inches=0.15,
                dpi=dpi,
                transparent=False,
                facecolor="white",
            )
        except Exception as exc:  # noqa: BLE001
            raise LatexRenderError(f"mathtext render failed: {exc}") from exc
        return buf.getvalue()
    finally:
        plt.close(fig)


def render_latex_to_base64_uri(latex_source: str, *, fontsize: int = 20, dpi: int = 180) -> str:
    """渲染并打包成 OneBot v11 的 ``base64://`` URI。"""
    png = render_latex_to_png(latex_source, fontsize=fontsize, dpi=dpi)
    return "base64://" + base64.b64encode(png).decode("ascii")


# 占位符用 NUL 字符包裹 —— QQ 文本里基本不可能出现，且 _reply_chunks 按 \n 切时也不会
# 误伤这个短短的占位符 token。占位符体积小，文本长度估算和切段都安全。
_LATEX_PLACEHOLDER_PREFIX = "\x00LATEX_"
_LATEX_PLACEHOLDER_SUFFIX = "\x00"
_LATEX_PLACEHOLDER_RE = re.compile(r"\x00LATEX_(\d+)\x00")


def replace_latex_with_placeholders(text: str) -> tuple[str, list[str]]:
    """把 ``text`` 里的 LaTeX 块替换成占位符，返回 ``(new_text, [latex_sources])``。

    占位符形如 ``\\x00LATEX_N\\x00``，N 是 sources 数组下标。后续 ``chunk_to_segments``
    会把占位符还原成图片消息段（或渲染失败时 fallback 回原 LaTeX 源码包裹）。
    """
    sources: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        latex = (
            match.group("bracket")
            or match.group("paren")
            or match.group("dollar")
            or ""
        ).strip()
        if not latex:
            return match.group(0)
        idx = len(sources)
        sources.append(latex)
        return f"{_LATEX_PLACEHOLDER_PREFIX}{idx}{_LATEX_PLACEHOLDER_SUFFIX}"

    new_text = _LATEX_BLOCK_RE.sub(_sub, text or "")
    return new_text, sources


@dataclass(slots=True)
class MessagePart:
    kind: SegmentKind  # "text" / "latex" (latex 模式 content 是已经渲染好的 base64:// URI)
    content: str


def restore_latex_placeholders(text: str, sources: list[str]) -> str:
    """把占位符还原成 ``\\[源码\\]`` 文本（不渲染）。

    用于 history/memory：让记忆里保留可读的原 LaTeX 源码，而不是
    NUL 占位符或最终的图片 URI。
    """
    if not text:
        return text

    def _sub(match: re.Match[str]) -> str:
        try:
            idx = int(match.group(1))
        except (TypeError, ValueError):
            return ""
        if 0 <= idx < len(sources):
            return f"\\[{sources[idx]}\\]"
        return ""

    return _LATEX_PLACEHOLDER_RE.sub(_sub, text)


def chunk_to_segments(chunk_text: str, sources: list[str]) -> list[MessagePart]:
    """把含占位符的 ``chunk_text`` 还原成 ``[MessagePart("text", ...), MessagePart("latex", base64uri), ...]``。

    渲染失败时该 LaTeX 部分 fallback 回原 ``\\[源码\\]`` 文本（让群友至少看到原始而不是 NUL 占位符）。
    没有 matplotlib 时同 fallback。
    """
    parts: list[MessagePart] = []
    last_end = 0
    for match in _LATEX_PLACEHOLDER_RE.finditer(chunk_text):
        start, end = match.span()
        if start > last_end:
            prefix = chunk_text[last_end:start]
            if prefix:
                parts.append(MessagePart("text", prefix))
        try:
            idx = int(match.group(1))
        except (TypeError, ValueError):
            last_end = end
            continue
        latex_src = sources[idx] if 0 <= idx < len(sources) else ""
        if not latex_src:
            last_end = end
            continue
        try:
            uri = render_latex_to_base64_uri(latex_src)
            parts.append(MessagePart("latex", uri))
        except (LatexRenderUnavailable, LatexRenderError):
            # Fallback: 还原源码,让群友至少看到原 LaTeX 而不是 NUL 占位符
            parts.append(MessagePart("text", f"\\[{latex_src}\\]"))
        last_end = end
    if last_end < len(chunk_text):
        tail = chunk_text[last_end:]
        if tail:
            parts.append(MessagePart("text", tail))
    if not parts and chunk_text:
        parts.append(MessagePart("text", chunk_text))
    return parts
