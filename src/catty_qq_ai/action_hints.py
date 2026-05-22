"""本地解析层联动闭环 —— 交叉 intent/entity/pulse 给 AI 一个具体『下一步建议』。

设计目标:
- 不替 AI 做决策,只在判定足够明确的 case 给一个具体行动建议。
- **依赖已有解析层结果**(避免重复扫描):caller 把 intent_tags + entities + pulse_phase 传进来。
- **保守发出 hint**:宁可不建议,别给错建议。AI 拿到 hint 也可以忽略。

例子:
| 触发                                                | 建议                                  |
|----------------------------------------------------|--------------------------------------|
| 未来 time entity + intent=command_to_cat            | 考虑 catty_remember 登记这个约定      |
| qq_id entity + 当前发言不是该 QQ                    | 考虑 catty_user_profile 查这个 QQ    |
| url entity + intent=info_request/question           | URL 内容你看不到,可 catty_web_search  |
| phase=cold + intent=greeting                        | 群里冷,主动展开个话题                |
| money entity + intent=command_to_cat                | 涉及金额,认真复述别口误数字          |

不做的事:
- 决定 AI 必须调用哪个 tool。
- 替 AI 解析意图(那是 intent_classifier 的事)。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .entity_extractor import Entity, extract_entities
from .intent_classifier import classify_intent
from .topic_classifier import classify_topic


def _has_future_time(entities: Iterable[Entity], *, reference: datetime | None = None) -> Entity | None:
    """从 entities 找一个未来的 time(有 iso 且 > reference)。返回第一条,无则 None。"""
    ref = reference if reference is not None else datetime.now()
    for e in entities:
        if e.kind != "time" or not e.iso:
            continue
        try:
            # iso 可能是 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM
            if "T" in e.iso:
                dt = datetime.fromisoformat(e.iso)
            else:
                dt = datetime.fromisoformat(e.iso + "T00:00")
        except ValueError:
            continue
        if dt > ref:
            return e
    return None


def _has_kind(entities: Iterable[Entity], kind: str) -> Entity | None:
    for e in entities:
        if e.kind == kind:
            return e
    return None


def build_action_hints(
    text: str,
    *,
    has_image: bool = False,
    pulse_phase: str = "normal",
    sender_qq: str = "",
    reference: datetime | None = None,
) -> str:
    """计算并返回一段 system prompt 文本,无建议返回空。

    内部会调 classify_intent + extract_entities + classify_topic;
    为简洁省去 caller 重复传参。
    """
    if not text:
        return ""
    intent_tags = classify_intent(text, has_image=has_image)
    entities = extract_entities(text, reference=reference)
    topic_tags = classify_topic(text)
    intents = set(intent_tags)
    topics = set(topic_tags)

    hints: list[str] = []

    # 1) 未来时间 + 命令/问句 → 约定登记建议
    future = _has_future_time(entities, reference=reference)
    if future and {"command_to_cat", "question"} & intents:
        # gaming 话题下时间约定 = 约黑,提示更具体
        if "gaming" in topics:
            hints.append(
                f"消息含未来时间『{future.raw}』({future.iso}) + 游戏话题,这看着像约开黑,"
                "强烈建议 catty_remember(scope=user, tags=['约黑']) 登记下次找得到"
            )
        else:
            hints.append(
                f"消息含未来时间『{future.raw}』({future.iso}),如果是约定/计划/活动,"
                "可以调 catty_remember(scope=user, ttl_days 到事件结束) 登记下次找得到"
            )

    # 2) qq_id entity + 不是当前发言者 → 查画像建议
    qq_ent = _has_kind(entities, "qq_id")
    if qq_ent and qq_ent.raw != str(sender_qq or ""):
        hints.append(
            f"消息提到 QQ 号 {qq_ent.raw}(非当前发言者),如果你不认识可以"
            "调 catty_user_profile(user_id) 查一下画像"
        )

    # 3) URL + 信息请求 → web_search 建议
    if _has_kind(entities, "url") and {"info_request", "question"} & intents:
        hints.append(
            "群友贴了 URL 又在问问题,你看不到链接内容,可以 catty_web_search 用"
            "原话关键词查一下;能基于常识答就别浪费一次工具"
        )

    # 4) 金额 + 命令/问句 → 提醒精确
    if _has_kind(entities, "money") and {"command_to_cat", "question"} & intents:
        hints.append(
            "消息涉及金额数字,如果你要复述或答应什么,**数字必须原样准确**,不要口误"
        )

    # 5) 冷场 + 招呼 → 主动开话题
    if pulse_phase == "cold" and "greeting" in intents:
        hints.append(
            "群里冷场且对方就是来打招呼,顺手开个轻话题(『今天天气真好』『有没有吃饭』之类),"
            "别只回一个『嗨』"
        )

    # 6) 刷屏 + 撩猫 → 短反应不陪刷
    if pulse_phase == "burst" and "tease_cat" in intents:
        hints.append(
            "对方在刷屏 + 撩你,反应要短(1 句以内)+ 嘴硬,别陪着展开"
        )

    # ── topic 联动规则(新) ────────────────────────────────────────

    # 7) finance + question → 实时行情可能要查
    if "finance" in topics and {"question", "info_request"} & intents:
        hints.append(
            "金融/股票/币圈话题 + 在问问题,**不要瞎猜实时行情/价格**;"
            "用 catty_web_search 查实时数据,或承认猫猫不懂金融"
        )

    # 8) politics → hard 提醒不表态
    if "politics" in topics:
        hints.append(
            "话题涉及政治/时政,**严格不要发表立场/支持/反对**,软转移到别的话题"
            "(『主人这个话题猫猫不懂啦,聊点别的喵』)"
        )

    # 9) tech + question → 鼓励详细回答
    if "tech" in topics and {"question", "info_request"} & intents:
        hints.append(
            "技术/编程问题,**可以放开写长一些**,代码思路 + 短示例 + 关键概念;"
            "拿不准的版本/语法可调 catty_web_search 查最新"
        )

    # 10) self_care + complaint → 强化"软安慰别讲道理"
    if "self_care" in topics and "complaint" in intents:
        hints.append(
            "对方情绪低落 + 在抱怨,**只软软陪着,别讲鸡汤别教育**;"
            "『主人摸摸~』『陪猫猫贴贴喵』比『要积极向上』强一万倍"
        )

    # 11) relationship + tease_cat → 撩反应要更强
    if "relationship" in topics and "tease_cat" in intents:
        hints.append(
            "话题涉及感情 + 在撩你,反应要按害羞反应链来 — 脸红→嘴硬→偷偷暴露,"
            "不要直接温柔接住"
        )

    if not hints:
        return ""
    return "本地解析层联动建议(供参考,可忽略): " + "; ".join(hints) + "。"
