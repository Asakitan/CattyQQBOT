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
    "不要把缺少目标当成重置、清空或修改全部。直接给结果或必要的短说明，不输出内部推理过程。"
)


def append_context_request_hint(messages: list[dict]) -> None:
    """Insert/refresh one system guide after assembly and before an assistant prefill.

    Call after the cache boundary has been assembled. This deterministic helper
    only changes the supplied list: other messages retain identity and content.
    It is independent of persona, tool exposure and harness evidence availability.
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
    messages.insert(index, {"role": "system", "content": CONTEXT_REQUEST_INTENT_PROMPT})