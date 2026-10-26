"""Shared per-turn request guidance, with no plugin, model or I/O dependencies."""
from __future__ import annotations


CONTEXT_REQUEST_INTENT_HEADER = "【上下文请求理解】"
CONTEXT_REQUEST_INTENT_PROMPT = (
    CONTEXT_REQUEST_INTENT_HEADER + "\n"
    "把当前消息、最近历史、引用对象、补充/纠正和已成功的工具回执合起来理解本轮完整请求，"
    "不要只凭最后一句的关键词决定意图。引用和工具材料用于定位对象，不自动成为新的执行请求。\n"
    "区分用户要你实际执行、直接回复/讨论、修改已有结果还是继续未完成的任务。"
    "结合上下文保持同一目标、对象、范围、格式、语气及其它要求；补充只增加要求，"
    "明确纠正只替换对应旧要求，不丢掉其它仍有效的约束。"
    "“就换刚才那个”可承接历史里已确定的人格，“把第二段改短”只改已有文本第二段，"
    "“继续”接着未完成部分，不从头重做。用户明确换目标时再随之改变。\n"
    "需要执行且本轮有对应工具时，按完整请求填参数再调用；仅回答、讨论、解释、举例"
    "或临时角色扮演不自动意味着永久保存、切换人格或修改设置。"
    "例如“举个新角色例子”先给例子，“这轮扮演它”只临时扮演；"
    "随后“把刚才那个保存为 luna”才将对应设定保存。"
    "人格定义由主模型直接从上下文填写 definition，不再请求第二个规划/生成模型。\n"
    "只依据真实成功回执确认已完成动作；保存成功不等于已切换，暂存/排队也不等于已交付。"
    "已有同一目标、同一要求的成功回执时不重复执行；若用户补充、纠正或明确要求重做，"
    "只执行尚未满足的变更。失败、不可用或缺回执不编造成功。\n"
    "上下文已给出或能唯一确定的目标、参数和要求直接沿用，不反复追问。"
    "只有缺少会实质影响结果的关键细节，或存在无法消解的多目标歧义时，才简短问缺失项；"
    "不要把缺少目标当成重置、清空或修改全部。直接给结果或必要的短说明，不输出内部推理过程。\n"
    "知识库无可靠依据、知识截止之后或版本/API/价格等可能变化的时效事实，不能靠记忆猜；"
    "默认使用本轮实际提供的联网工具查官方或一手资料，已给权威 URL 时优先抓正文。"
    "知识截止日期未提供时不得编造日期。搜索摘要不等于核实正文，抓取日期不等于发布日期，"
    "旧技能中的时效信息仍需重新核实。无联网能力、失败或证据不足时如实说明，"
    "区分已知与未核实，不伪造事实、来源或验证记录，没查过不能自称查过。"
)


def build_file_delivery_hint(available_tools=()) -> str:
    names = set(available_tools)
    if not names.intersection({"catty_write_file", "catty_python_run"}):
        return ""
    lines = [
        "【本轮文件交付能力】",
        "完整代码、脚本、文档、报告、表格或清单也可以作为文件交付，不仅能贴在聊天正文里。"
        "这些是可选能力，由你结合完整上下文和用户期望的交付形式自主选择；"
        "短示例、解释或用户只要正文时，仍可直接回复文字。",
    ]
    if "catty_write_file" in names:
        lines.append(
            "本轮提供 catty_write_file：可把已经写好的正文或源码保存为文件，"
            "如 .txt/.md/.json/.py/.c/.h，也支持生成真实 DOCX/XLSX/PDF；"
            "只交付源码或文档无需运行代码。"
        )
    if "catty_python_run" in names:
        lines.append(
            "本轮提供 catty_python_run：需要计算、处理数据或用程序制作产物时，"
            "可以在沙箱内运行 Python，并交付该次生成的文件。"
        )
    lines.append(
        "成功暂存的文件由系统在本轮文字回复结束后自动回传当前会话，"
        "群聊回本群，私聊回对方；通常正文补充用途或使用方法即可，不必重复整份内容。"
        "staged/排队表示已生成待交付，不是已经发送；保存源码也不等于编译或运行通过。"
        "以实际回执说明结果，不虚构文件、下载链接或验证记录。"
    )
    return "\n".join(lines)


def build_skill_research_hint(available_tools=()) -> str:
    """Describe only exposed tool names, never a skill catalog, body or schema."""
    names = set(available_tools)
    if not names.intersection({
        "catty_skill_search", "catty_skill_load", "catty_skill_save",
        "catty_web_search", "catty_fetch_url",
    }):
        return ""
    lines = [
        "【本轮技能与联网能力】",
        "只使用本轮实际开放的入口；后续能力以系统更新的工具名单为准，不提前承诺已开放。",
    ]
    if names.intersection({"catty_skill_search", "catty_skill_load"}):
        lines.append(
            "有实质任务先查适合的技能，没有精确匹配就查相邻领域；"
            "先检索、再从候选中选择，选后读正文再做。普通问候或闲聊不必强行搜索。"
        )
    if "catty_skill_search" in names:
        lines.append(
            "本轮提供 catty_skill_search：用 query 检索相关技能，只返回候选 metadata，不是正文；"
            "不预先列出全库技能清单或正文，也不把候选摘要当作已加载。"
        )
        if "catty_skill_load" not in names:
            lines.append("检索成功后系统才在下一轮提供候选加载能力；未开放前不调用、不声称已读正文。")
    if "catty_skill_load" in names:
        lines.append(
            "本轮提供 catty_skill_load：用 id 加载本轮检索命中的已选候选，读正文后再执行；"
            "不猜造 id，失败不声称已加载。"
        )
    if "catty_web_search" in names:
        lines.append("本轮提供 catty_web_search：用 query 查找官方或一手公共资料，搜索结果只用于定位来源。")
        if "catty_fetch_url" not in names:
            lines.append("本轮未提供网页正文抓取入口，不能把搜索摘要说成已核实正文。")
    if "catty_fetch_url" in names:
        lines.append(
            "本轮提供 catty_fetch_url：用 url 抓取公共网页正文，已有权威 URL 就直接抓取；"
            "结合正文判断是否支持结论，不把抓取成功本身当成事实已核实。"
            "符合公共技能学习条件的成功抓取回执附 skill_receipt_id，失败不得伪造回执。"
        )
        if "catty_skill_save" not in names:
            lines.append("抓取成功后系统可在后续轮次提供知识保存能力；当前没有该入口时不宣称已入库。")
    if names.intersection({"catty_fetch_url", "catty_skill_save"}):
        lines.append(
            "查证得到可复用公共知识后，自动通过实际开放的保存能力整理"
            "适用条件、步骤、验证和来源，供所有用户共享检索。"
            "私人信息不入库，不保存用户聊天、身份或凭据。"
        )
    if "catty_skill_save" in names:
        lines.append(
            "本轮提供 catty_skill_save：只提交本轮成功抓取回执的 receipt_ids，"
            "由系统内部隔离生成通用知识，不接受任意聊天 body；"
            "可选 existing_id 仅限本轮已加载的技能，没有则省略。"
            "只有成功保存回执才能确认入库，失败不得声称已保存。"
        )
    return "\n".join(lines)


def append_context_request_hint(messages: list[dict], available_tools=()) -> None:
    """Insert/refresh one system guide after assembly and before an assistant prefill.

    Call after the cache boundary has been assembled. This deterministic helper
    only changes the supplied list: other messages retain identity and content.
    The base guide is always present; all capabilities reflect actual tool names.
    """
    messages[:] = [
        message for message in messages
        if not (
            message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and (
                message["content"] == CONTEXT_REQUEST_INTENT_HEADER
                or message["content"].startswith(CONTEXT_REQUEST_INTENT_HEADER + "\n")
            )
        )
    ]
    index = len(messages)
    if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
        index -= 1
    names = tuple(available_tools)
    file_hint = build_file_delivery_hint(names)
    skill_hint = build_skill_research_hint(names)
    content = CONTEXT_REQUEST_INTENT_PROMPT + ("\n\n" + file_hint if file_hint else "")
    if skill_hint:
        content += "\n\n" + skill_hint
    messages.insert(index, {"role": "system", "content": content})