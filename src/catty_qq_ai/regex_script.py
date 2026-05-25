"""SillyTavern 风 Regex Script — 输出侧最后一道净化网。

参考 ST extensions/regex/index.js:
- 每条 script 有 findRegex/replaceString/placement/disabled 等字段
- placement = USER_INPUT(1) / AI_OUTPUT(2) / SLASH_COMMAND(3) / WORLD_INFO(5) / REASONING(6)
- 在 prompt 注入和 message 渲染前后多个 stage hook

笨猫只做最高价值的 **ai_output** 净化,跟 _sanitize_residual_markers 叠在一起,
作为 LLM 漏检时的最后一道防线。覆盖 4 类场景:

1. 破设定话术 — LLM 偶尔会冒"作为 AI / 我只是个语言模型"漏出来,直接抹掉
2. 客服拒绝套话 — "抱歉,我无法 / 对不起,我不能" 单独成句也抹掉
3. 重复尾巴词 — 连续两个相同 "喵~" "嗷呜~" 等折叠成一个 (避免 LLM 复读机)
4. **称呼防御网 (non-owner only)** — 真主人之外的用户被叫"主人/笨蛋主人/杂鱼主人"
   时自动改回"你/笨蛋你/杂鱼",兜底 `feedback_owner_address_exclusive` 这条铁律

规则定义在本模块顶部,改库就动这里;以后真要支持外部 JSON 配置再加 loader。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RegexScript:
    """单条 regex 替换规则。"""
    identifier: str
    pattern: re.Pattern[str]
    replacement: str
    placement: str = "ai_output"          # ai_output | user_input | (扩展)
    description: str = ""
    only_when_non_owner: bool = False     # True = 仅对非主人 user 启用 (称呼防御网)
    enabled: bool = True


# ── 默认规则集 ───────────────────────────────────────────────────────
# 编译一次,后面 apply 时直接用。所有 pattern 默认 re.IGNORECASE | re.UNICODE。
_FLAGS = re.IGNORECASE | re.UNICODE


def _compile(pat: str, *extra_flags: int) -> re.Pattern[str]:
    flags = _FLAGS
    for f in extra_flags:
        flags |= f
    return re.compile(pat, flags)


_DEFAULT_SCRIPTS: tuple[RegexScript, ...] = (
    # ── 1. 破设定话术 ──────────────────────────────────────────────
    # 整句删除:句尾找标点 (。!?！？\n) 或字符串末尾
    RegexScript(
        identifier="strip_as_an_ai_zh",
        pattern=_compile(
            r"(?:^|(?<=[。!?！？\n]))\s*"
            r"(?:作为(?:一个|一只)?(?:AI|人工智能|大型?语言模型|语言模型|机器人|聊天机器人|助手)"
            r"|我(?:只是|不过是|其实是)(?:一个|一只)?(?:AI|人工智能|大型?语言模型|语言模型|机器人|程序|助手|虚拟角色)"
            r"|我没有(?:真实)?身体|我没有(?:真实的)?情感|我不是(?:真实的|真的)(?:人|猫|猫娘)"
            r")[^。!?！？\n]*[。!?！？\n]?"
        ),
        replacement="",
        description="抹掉 LLM 漏出来的『作为 AI / 我只是语言模型 / 我不是真的猫娘』整句",
    ),
    RegexScript(
        identifier="strip_as_an_ai_en",
        pattern=_compile(
            r"(?:^|(?<=[.!?\n]))\s*"
            r"(?:as an? (?:AI|artificial intelligence|large language model|language model|assistant|chatbot)"
            r"|i'?m (?:just|only|merely) an? (?:AI|language model|assistant|program|virtual)"
            r"|i (?:don'?t|do not) have (?:a )?(?:real )?(?:body|emotions|feelings)"
            r")[^.!?\n]*[.!?\n]?"
        ),
        replacement="",
        description="同上的英文版",
    ),

    # ── 2. 客服拒绝套话 ────────────────────────────────────────────
    RegexScript(
        identifier="strip_apology_refusal",
        pattern=_compile(
            r"(?:^|(?<=[。!?！？\n]))\s*"
            r"(?:很?抱歉(?:[,，]?我?(?:无法|不能|不可以))"
            r"|对不起(?:[,，]?我?(?:无法|不能|不可以))"
            r"|很?遗憾(?:[,，]?我?(?:无法|不能|不可以))"
            r")[^。!?！？\n]*[。!?！？\n]?"
        ),
        replacement="",
        description="抹掉客服腔的『抱歉我无法 / 对不起我不能』整句",
    ),

    # ── 3. 折叠重复尾巴词 ──────────────────────────────────────────
    # 同一句里出现两个紧邻的相同猫系词(允许中间有 1-3 个非字母字符)
    # 例: "喵~ 喵~" / "嗷呜～嗷呜～" → 留一个
    RegexScript(
        identifier="dedupe_cat_tail_meow",
        pattern=_compile(r"(喵[~～]?)[\s,，.。]{0,3}\1"),
        replacement=r"\1",
        description="折叠『喵~ 喵~』→『喵~』,防 LLM 尾巴词复读",
    ),
    RegexScript(
        identifier="dedupe_cat_tail_aow",
        pattern=_compile(r"(嗷呜[~～]?)[\s,，.。]{0,3}\1"),
        replacement=r"\1",
        description="折叠『嗷呜~ 嗷呜~』→『嗷呜~』",
    ),
    RegexScript(
        identifier="dedupe_cat_tail_meowwu",
        pattern=_compile(r"(喵呜[~～]?)[\s,，.。]{0,3}\1"),
        replacement=r"\1",
        description="折叠『喵呜~ 喵呜~』→『喵呜~』",
    ),

    # ── 4. 称呼防御网 (非主人 only) ────────────────────────────────
    # `feedback_owner_address_exclusive` 这条铁律的最后一道防线 — 即使所有 prompt 段
    # 都漏了,这里 regex 也会把『主人/笨蛋主人/杂鱼主人』替换掉,避免穿帮。
    # 注意词序:更长的特殊变体先匹配,否则会被『主人 → 你』提前吃掉。
    RegexScript(
        identifier="non_owner_strip_baka_zhuren",
        pattern=_compile(r"(?<!真)笨蛋主人"),
        replacement="笨蛋你",
        only_when_non_owner=True,
        description="非主人不能叫『笨蛋主人』,替换『笨蛋你』",
    ),
    RegexScript(
        identifier="non_owner_strip_zayu_zhuren",
        pattern=_compile(r"杂鱼主人"),
        replacement="杂鱼",
        only_when_non_owner=True,
        description="非主人不能叫『杂鱼主人』,替换『杂鱼』",
    ),
    RegexScript(
        identifier="non_owner_strip_zhuren",
        # negative lookbehind: 排除"笨蛋主人/杂鱼主人/真主人" 这种,只抓裸『主人』
        pattern=_compile(r"(?<![笨杂蛋鱼真])主人"),
        replacement="你",
        only_when_non_owner=True,
        description="非主人不能叫『主人』,替换『你』(裸词,前一规则没吃到的兜底)",
    ),
)


# ── apply 接口 ───────────────────────────────────────────────────────
def apply_output_scripts(
    text: str,
    *,
    is_owner: bool = False,
    scripts: tuple[RegexScript, ...] | None = None,
) -> str:
    """对 LLM output 跑所有匹配 placement=ai_output 的 enabled scripts。

    is_owner=True 时跳过 only_when_non_owner 的称呼防御规则
    (真主人本来就该叫主人)。
    """
    if not text:
        return text
    out = text
    for s in (scripts or _DEFAULT_SCRIPTS):
        if not s.enabled or s.placement != "ai_output":
            continue
        if s.only_when_non_owner and is_owner:
            continue
        out = s.pattern.sub(s.replacement, out)
    return out


def default_scripts() -> tuple[RegexScript, ...]:
    """返回默认规则集,主要给单元测试 / debug 用。"""
    return _DEFAULT_SCRIPTS


__all__ = [
    "RegexScript",
    "apply_output_scripts",
    "default_scripts",
]
