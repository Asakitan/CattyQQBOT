"""catty(笨猫)Persona 实例 — 默认人格。

**关键约束**: 所有内容字段保持 None → prompt 管线走现有常量/builder 老路径,
catty 的 cache prefix 逐字节不变(Step 2 用 sim dry-run byte-diff 验收)。
这里不复制任何挂在 Persona 的 prompt 文本；业务 fallback 目录独立定义且不挂到实例。
"""
from __future__ import annotations

from . import Persona, PersonaReplyCatalog

# 业务 fallback 目录独立于 CATTY_PERSONA；不挂到 Persona 上，保持旧 prompt/cache 路径。
CATTY_REPLY_CATALOG = PersonaReplyCatalog(
    slow_reply_placeholders=(
        "嗯…猫猫先想想喵～(尾巴轻轻晃)",
        "唔…让人家整理一下喵～(爪爪挠头)",
        "稍等下喵～猫猫脑袋在转(转圈圈)",
        "等等~~人家在翻记忆库喵 ฅฅ",
        "马上来嗷呜～(尾巴竖起来)",
        "哼~才不是不理你呢,人家想想啦喵",
        "笨猫还在想…别催别催嗷呜～(炸毛)",
        "喵呜～脑袋一时转不过来,等等人家",
        "唔嗯…让奴翻翻笔记喵～(爪爪翻页)",
        "等下喵～猫猫脑子有点转不动了哼",
        "稍等嗷呜~人家在认真想啦(歪头)",
        "诶?这个有点难,人家想下喵～",
        "笨猫思考中...请勿打扰(尾巴竖起警告)",
        "等等等等~猫猫还在码字哼(爪爪疾走)",
        "稍候喵,人家在整理思路 ฅฅ",
        "嗷呜～别急啦,奴马上回话",
        "哼,猫猫又不是机器人,让人家想想嘛",
        "等下下喵~笨猫脑袋热了在散热(冒烟)",
        "奴这就到~等一小会儿喵呜",
        "唔～脑袋装得太满,人家先理清一下喵",
        "再等下嗷呜～猫猫不是不理你啦",
        "喵?这题人家得想想…",
    ),
    slow_reply_owner_placeholders=(
        "奴这就给主人查~稍等下嗷呜 ฅฅ",
        "马上~奴这边在赶啦,主人坐稳 ฅฅ",
        "唔…让奴慢慢理给主人听喵～",
        "笨猫还在敲爪爪,主人稍等 ฅฅ",
        "稍等喵主人~人家正在认真想呢(爪爪)",
        "奴马上把答案端到主人面前嗷呜~",
    ),
    force_reply_instruction=(
        "刚才那条没回成功, 再回一次:按当前上下文直接给 user 一个自然回复。"
        "保持笨猫 QQ 口吻 (短句、可爱、有用), 信息不足就追问, 不要再沉默。"
    ),
    no_reply_image_fallback=(
        "在呢喵～图片人家收到了，刚刚差点装死不该的；{owner_address}想让笨猫看哪里呀？"
    ),
    no_reply_reply_fallback=(
        "在呢喵～{owner_address}回复到人家啦，笨猫这次不装死，{owner_address}要接着说什么？"
    ),
    no_reply_mention_fallback="在呢喵～{owner_address}喊笨猫啦，要人家做什么？",
    no_reply_default_fallback=(
        "在呢喵～人家接到了，刚刚差点没回不该的；{owner_address}这句奴会认真接。"
    ),
    api_timeout_reply="唔…刚刚等太久卡住啦，{owner_address}再说一次好不好？",
    api_transport_reply="呜，刚刚网线抽风啦，{owner_address}再发一遍嘛。",
    image_send_failure_reply="喵呜…图没发出去，{owner_address}过会儿再试嘛 (尾巴垂垂) ฅฅ",
    tool_result_follow_up_instruction="工具结果已经有啦，直接结合结果自然回复用户，不要解释工具调用过程。",
    turtle_soup_cooldown_reply=(
        "哼，这个群刚端过一碗海龟汤啦喵～"
        "还剩 {remaining} 才能开下一锅，先问问上一题也不是不行。"
    ),
    turtle_soup_rule_line="规则：只能问能用“是/否/无关”回答的问题，答案人家先藏起来喵。",
    api_key_missing_reply="还没有配置 API Key，先在 config.json 里填好 ai.api_key 再来找人家。",
    web_search_cooldown_reply=(
        "哼，{user_title}刚刚已经用过联网搜索啦喵～"
        "这次还在冷却中（还剩 {remaining}），"
        "等猫猫的搜索爪爪缓过来再找。"
    ),
    web_search_failure_instruction=(
        "本轮用户明确要求联网搜索「{query}」，但本地 Google/Bing 搜索插件调用失败。"
        "请用猫系人格如实说明这次联网查询失败，不要编造搜索结果、链接、日期或来源；"
        "可以基于已有知识给出有限建议，并提醒用户稍后重试。"
    ),
    web_search_disabled_instruction=(
        "本轮用户要求联网搜索，但当前配置关闭了 web_search.enabled。"
        "请用猫系人格说明联网搜索暂时不可用。"
    ),
    busy_fallback_reply=(
        "喵呜，MC 群友正在玩游戏中，猫猫这会儿不能用本地脑子顶上来——"
        "{owner_address}稍等一下再戳。"
    ),
)

CATTY_PERSONA = Persona(
    name="catty",
    char_name="笨猫",
    # 全部内容字段 None = 用 catty_core_persona / character_card / persona_prompts 现有内容
    owner_concept=True,
    reply_gate_style=None,  # None → reply_gate prompt 用原文(含"用笨猫口吻")
)
