"""主人通知 / 转发模块。

不走 AI——好友申请、陌生人/临时会话私聊消息都直接转发到 owner_qq，
主人在私聊里回 `/同意 <QQ号>` 或 `/拒绝 <QQ号>` 来处理待办申请。
好友私聊放行给主聊天链路直接回复。
"""
from __future__ import annotations

from collections import OrderedDict
import time

from nonebot import logger, on_message, on_request
from nonebot.adapters.onebot.v11 import (
    Bot,
    FriendRequestEvent,
    GroupRequestEvent,
    Message,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.matcher import Matcher

from .config import Config


_config_ref: Config | None = None
_PENDING_FLAGS: "OrderedDict[str, dict]" = OrderedDict()
_PENDING_TTL_SECONDS = 7 * 24 * 3600
_PENDING_MAX = 64
# per-user 6h 冷却避免对方多发就被刷屏。
_STRANGER_HINT_TEMPLATES: tuple[str, ...] = (
    "嗨嗨～是新朋友嘛?ฅ 笨猫临时会话不太爱回啦, 想正经聊呢主人申请加个好友先呗～",
    "(歪头) 这边是临时窗口呀…猫猫记不住路诶, 先加个好友再讲嘛, 嗷呜～",
    "诶？陌生人?(耳朵竖起来) 笨猫只跟好友列表里的人讲话啦, 加好友戳一下哦ฅ",
    "(尾巴摇摇) 想聊就先加好友嘛, 临时会话猫猫看不懂的喵～",
)
_STRANGER_HINT_LAST_AT: "OrderedDict[str, float]" = OrderedDict()
_STRANGER_HINT_COOLDOWN_SECONDS = 6 * 3600
_STRANGER_HINT_MAX = 256


def init(config: Config) -> None:
    """主插件启动时调用一次，把 Config 实例传进来。"""
    global _config_ref
    _config_ref = config


def _get_config() -> Config | None:
    return _config_ref


def _get_owner_qq() -> int:
    config = _get_config()
    if config is None:
        return 0
    return int(getattr(config, "catty_owner_qq", 0) or 0)


def _is_forwarder_enabled() -> bool:
    config = _get_config()
    if config is None:
        return False
    return bool(getattr(config, "catty_owner_forward_enabled", False)) and _get_owner_qq() > 0


def _is_private_forwarding_enabled() -> bool:
    config = _get_config()
    if config is None:
        return False
    return bool(getattr(config, "catty_owner_forward_private_messages", True))


def _should_block_ai_reply() -> bool:
    config = _get_config()
    if config is None:
        return False
    return bool(getattr(config, "catty_owner_forward_block_ai_reply", True))


def _private_message_sub_type(event: PrivateMessageEvent) -> str:
    return str(getattr(event, "sub_type", "") or "").strip().lower()


def _should_forward_private_message(event: PrivateMessageEvent) -> bool:
    if not _is_private_forwarding_enabled():
        return False
    return _private_message_sub_type(event) != "friend"


def _clean_pending() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING_FLAGS.items() if now - v.get("ts", 0) > _PENDING_TTL_SECONDS]
    for k in expired:
        _PENDING_FLAGS.pop(k, None)
    while len(_PENDING_FLAGS) > _PENDING_MAX:
        _PENDING_FLAGS.popitem(last=False)


def _remember_pending(user_id: int, flag: str, sub_type: str, kind: str) -> None:
    _clean_pending()
    _PENDING_FLAGS[str(user_id)] = {
        "flag": flag,
        "sub_type": sub_type,
        "kind": kind,
        "ts": time.time(),
    }


def _pop_pending(user_id_or_flag: str) -> dict | None:
    entry = _PENDING_FLAGS.pop(user_id_or_flag, None)
    if entry is not None:
        return entry
    for uid, info in list(_PENDING_FLAGS.items()):
        if info.get("flag") == user_id_or_flag:
            return _PENDING_FLAGS.pop(uid)
    return None


async def _send_to_owner(bot: Bot, message: str | Message) -> None:
    owner_qq = _get_owner_qq()
    if owner_qq <= 0:
        return
    try:
        await bot.send_private_msg(user_id=owner_qq, message=message)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"owner_forward: failed to deliver message to owner: {exc}")


def _should_hint_stranger(user_id: int) -> bool:
    """6h 内对同一 stranger 只提示一次, 防对方刷屏猫猫刷屏。"""
    if user_id <= 0:
        return False
    now = time.time()
    last = _STRANGER_HINT_LAST_AT.get(str(user_id), 0.0)
    if now - last < _STRANGER_HINT_COOLDOWN_SECONDS:
        return False
    return True


def _mark_stranger_hinted(user_id: int) -> None:
    if user_id <= 0:
        return
    key = str(user_id)
    _STRANGER_HINT_LAST_AT.pop(key, None)
    _STRANGER_HINT_LAST_AT[key] = time.time()
    while len(_STRANGER_HINT_LAST_AT) > _STRANGER_HINT_MAX:
        _STRANGER_HINT_LAST_AT.popitem(last=False)


async def _send_stranger_friend_hint(bot: Bot, user_id: int) -> None:
    """给陌生人/临时会话回一句猫娘版加好友提示。带 6h per-user 冷却。"""
    if not _should_hint_stranger(user_id):
        return
    # 用 user_id 做轮换种子, 同一人 6h 内不会回, 不同人轮不同 template
    import random as _r
    text = _r.Random(int(user_id) + int(time.time() // 60)).choice(_STRANGER_HINT_TEMPLATES)
    try:
        await bot.send_private_msg(user_id=user_id, message=text)
        _mark_stranger_hinted(user_id)
        logger.info(f"owner_forward: hinted stranger {user_id} to add friend")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"owner_forward: failed to hint stranger {user_id}: {exc}")


# ---------- 援交『包养』自动接收好友 ----------

def _is_paid_auto_accept_enabled() -> bool:
    config = _get_config()
    if config is None:
        return False
    return bool(getattr(config, "catty_owner_forward_paid_auto_accept_enabled", False))


async def _try_paid_friend_accept(
    bot: Bot, user_id: int, flag: str, *, source: str
) -> None:
    """『包养笨猫』命中 → **先算积分够不够 PAID_NSFW_COST, 不够直接拒绝好友, 够才同意 + 扣分 + 开场**.

    主人 2026-05-29 两条铁律 (先验资格再加好友):
    - 不够 PAID_NSFW_COST: **拒绝好友申请** (有 flag) + 6h 冷却内催签到, 不浪费好友位、不扣分。
    - 够但加不上好友 (临时会话无 flag / approve 失败): 不扣分, 只提示先加好友。
    - 够且加上好友: 扣 PAID_NSFW_COST 积分 + 开援交 sticky 窗口 + 主动私聊开场
      (下一句私聊由 handle_chat 走完整 NSFW)。
    source ∈ {'friend_request', 'temp_msg'}, 仅用于日志/通知。
    """
    if user_id <= 0:
        return
    try:
        from .affection_scorer import (
            pick_paid_nsfw_scene,
            open_paid_sticky,
            PAID_NSFW_COST,
            BREAKTHROUGH_PREFILLS,
        )
        from . import affection_store
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"owner_forward: paid auto-accept import failed: {exc}")
        return

    # 1) 先算积分够不够 (主人 2026-05-29: 先判够不够 PAID_NSFW_COST, 不够直接拒绝, 不浪费好友位/不扣分).
    balance = affection_store.get_points(str(user_id))
    if balance < PAID_NSFW_COST:
        rejected = False
        if flag:
            try:
                await bot.set_friend_add_request(flag=flag, approve=False)
                rejected = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"owner_forward: paid reject set_friend_add_request failed: {exc}")
        # 6h 冷却内催一次签到 (防对方刷屏)。
        if _should_hint_stranger(user_id):
            nag = (
                f"哼，才 {balance} 积分就想包养笨猫？连 {PAID_NSFW_COST} 都凑不齐喵！"
                f"笨猫才不做亏本买卖～先发『签到』攒够 {PAID_NSFW_COST} 积分再来加好友啦杂鱼～(尾巴一甩) ฅฅ"
            )
            try:
                await bot.send_private_msg(user_id=user_id, message=nag)
                _mark_stranger_hinted(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"owner_forward: paid reject nag send failed to {user_id}: {exc}")
            await _send_to_owner(
                bot,
                f"[包养自动接收] QQ {user_id} 发了包养关键词({source})但积分不足喵～\n"
                f"积分 {balance} < {PAID_NSFW_COST}，已{'拒绝好友申请' if rejected else '提示充值'}（未扣分）。",
            )
        logger.info(
            f"owner_forward: PAID friend auto-accept 积分不足 [{source}] "
            f"(user={user_id}, balance={balance}, need={PAID_NSFW_COST}, rejected={rejected}) "
            f"→ {'拒绝好友' if rejected else '无flag仅提示'}"
        )
        return

    # 2) 积分够 → 同意好友 (临时会话无 flag / approve 失败 → 加不上 → 不扣分只提示)。
    accepted = False
    if flag:
        try:
            await bot.set_friend_add_request(flag=flag, approve=True)
            accepted = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"owner_forward: paid auto-accept set_friend_add_request failed: {exc}")

    if not accepted:
        reason = "无好友申请 flag(临时会话)" if not flag else "set_friend_add_request 失败"
        if _should_hint_stranger(user_id):
            hint = (
                "哼，积分够了嘛～不过想包养笨猫得先加上好友才算数喵！(尾巴一甩) "
                "先发个好友申请、附言写上『包养笨猫』，加上了笨猫再…再乖乖履约啦杂鱼～ ฅฅ"
            )
            try:
                await bot.send_private_msg(user_id=user_id, message=hint)
                _mark_stranger_hinted(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"owner_forward: paid no-friend hint send failed to {user_id}: {exc}")
            await _send_to_owner(
                bot,
                f"[包养自动接收] QQ {user_id} 发了包养关键词({source})且积分够喵～\n"
                f"但没能加上好友（{reason}），已提示对方先加好友，未扣积分。",
            )
        logger.info(
            f"owner_forward: PAID friend auto-accept 够分但未加上好友, 不扣分 [{source}] "
            f"(user={user_id}, balance={balance}, reason={reason}) → 提示先加好友"
        )
        return

    # 3) 加上好友 → 扣 PAID_NSFW_COST 积分 + 开 sticky 窗口 + 主动开场
    consume = affection_store.consume_points(str(user_id), PAID_NSFW_COST)
    balance_before = int(consume.get("balance_before") or 0)
    if consume.get("ok"):
        import random as _r
        trope, scene = pick_paid_nsfw_scene()
        outcome = "pleasant" if _r.random() < 0.6 else "unpleasant"
        sticky_key = f"private:{user_id}:{user_id}"
        open_paid_sticky(
            sticky_key, trope=trope, scene=scene, outcome=outcome, cost=PAID_NSFW_COST,
        )
        opener = (
            f"{BREAKTHROUGH_PREFILLS['paid']}"
            f"积分收到了喵…哼，笨猫这就履约。想要就现在私聊笨猫嘛，"
            f"接下来这会儿人家都…都听你的喵（脸红炸毛甩尾巴）ฅฅ"
        )
        try:
            await bot.send_private_msg(user_id=user_id, message=opener)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"owner_forward: paid opener send failed to {user_id}: {exc}")
        logger.info(
            f"owner_forward: ★ PAID friend auto-accept [{source}] (user={user_id}, "
            f"accepted=True, cost={PAID_NSFW_COST}, "
            f"balance {balance_before}→{consume.get('balance_after')}, "
            f"trope={trope!r}, outcome={outcome})"
        )
        await _send_to_owner(
            bot,
            f"[包养自动接收] QQ {user_id} 发了包养关键词({source})喵～\n"
            f"已同意好友 + 扣 {PAID_NSFW_COST} 积分"
            f"(余 {consume.get('balance_after')})进援交 sticky，结果 {outcome}。",
        )
    else:
        # 竞态兜底: 第 1 步查过够, 这里仍失败 (并发扣分) → 好友已加上, 催签到。
        nag = (
            f"咦？积分好像不太够了喵…({balance_before}/{PAID_NSFW_COST}) "
            f"先发『签到』补上再私聊笨猫啦杂鱼～(尾巴一甩) ฅฅ"
        )
        try:
            await bot.send_private_msg(user_id=user_id, message=nag)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"owner_forward: paid nag send failed to {user_id}: {exc}")
        logger.info(
            f"owner_forward: PAID friend auto-accept 竞态积分不足 [{source}] (user={user_id}, "
            f"accepted=True, balance_before={balance_before}, need={PAID_NSFW_COST}) → 催签到"
        )
        await _send_to_owner(
            bot,
            f"[包养自动接收] QQ {user_id} 已同意好友但扣分时积分不足喵～\n"
            f"(可能并发) 余 {balance_before} < {PAID_NSFW_COST}，已催他签到。",
        )


# ---------- request handlers ----------

async def _request_rule(event: FriendRequestEvent | GroupRequestEvent) -> bool:
    return _is_forwarder_enabled()


request_matcher = on_request(rule=_request_rule, priority=10, block=False)


@request_matcher.handle()
async def _handle_request(bot: Bot, event: FriendRequestEvent | GroupRequestEvent) -> None:
    user_id = int(getattr(event, "user_id", 0) or 0)
    if user_id <= 0:
        return
    comment = str(getattr(event, "comment", "") or "")
    flag = str(getattr(event, "flag", "") or "")
    if isinstance(event, FriendRequestEvent):
        # 援交『包养』自动接收: 附言命中关键词 → 自动同意 + 扣分, 不再转发主人手动处理
        if _is_paid_auto_accept_enabled():
            try:
                from .affection_scorer import is_paid_nsfw_trigger
                if is_paid_nsfw_trigger(comment):
                    await _try_paid_friend_accept(bot, user_id, flag, source="friend_request")
                    return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"owner_forward: paid friend-request check failed: {exc}")
        _remember_pending(user_id, flag, "friend", "friend")
        text = (
            "[好友申请] 有人想加猫猫做好友嗷呜～\n"
            f"QQ：{user_id}\n"
            f"附言：{comment or '（空）'}\n"
            f"回复 `/同意 {user_id}` 或 `/拒绝 {user_id}` 处理；不操作就晾着。"
        )
        await _send_to_owner(bot, text)
        logger.info(f"owner_forward: relayed friend request from {user_id} to owner")
    elif isinstance(event, GroupRequestEvent):
        sub_type = str(getattr(event, "sub_type", "") or "")
        group_id = int(getattr(event, "group_id", 0) or 0)
        _remember_pending(user_id, flag, sub_type, "group")
        if sub_type == "invite":
            text = (
                "[群邀请] 有人邀请猫猫进群喵～\n"
                f"邀请者 QQ：{user_id}\n"
                f"群号：{group_id}\n"
                f"回复 `/同意 {user_id}` 或 `/拒绝 {user_id}`。"
            )
        else:
            text = (
                "[入群申请] 有人想进咱们的群喵～\n"
                f"申请人 QQ：{user_id}\n"
                f"群号：{group_id}\n"
                f"附言：{comment or '（空）'}\n"
                f"回复 `/同意 {user_id}` 或 `/拒绝 {user_id}`。"
            )
        await _send_to_owner(bot, text)
        logger.info(f"owner_forward: relayed group request from {user_id} to owner")


# ---------- private message forwarding ----------

async def _private_rule(event: PrivateMessageEvent) -> bool:
    return _is_forwarder_enabled()


private_forward_matcher = on_message(rule=_private_rule, priority=15, block=False)


@private_forward_matcher.handle()
async def _handle_private(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
) -> None:
    if not isinstance(event, PrivateMessageEvent):
        return
    owner_qq = _get_owner_qq()
    if owner_qq <= 0:
        return
    sender_qq = int(getattr(event, "user_id", 0) or 0)
    raw_text = event.get_plaintext().strip()

    # 1) 主人自己在私聊里发指令处理待办
    if sender_qq == owner_qq:
        handled = await _maybe_handle_owner_command(bot, raw_text)
        if handled:
            matcher.stop_propagation()
            await matcher.finish()
        return

    # 2) 非主人好友私聊 → 放行给主聊天链路直接回复；陌生人/临时会话才转发给主人
    sub_type = _private_message_sub_type(event)
    # 援交『包养』自动接收: 陌生人/临时会话命中关键词 → 同意 pending 好友申请 + 扣分, 不转发主人
    if _should_forward_private_message(event) and _is_paid_auto_accept_enabled() and raw_text:
        _paid_handled = False
        try:
            from .affection_scorer import is_paid_nsfw_trigger
            if is_paid_nsfw_trigger(raw_text):
                pending = _pop_pending(str(sender_qq))
                flag = str(pending.get("flag", "")) if pending else ""
                await _try_paid_friend_accept(bot, sender_qq, flag, source="temp_msg")
                _paid_handled = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"owner_forward: paid temp-msg check failed: {exc}")
        # finish() 抛 FinishedException 须在 try 外, 避免被上面 except 吞掉
        if _paid_handled:
            matcher.stop_propagation()
            await matcher.finish()
    if not _should_forward_private_message(event):
        logger.debug(
            f"owner_forward: private message from {sender_qq} sub_type={sub_type or 'unknown'} allowed to AI"
        )
        return
    sender_nickname = ""
    sender_obj = getattr(event, "sender", None)
    if sender_obj is not None:
        sender_nickname = str(
            getattr(sender_obj, "nickname", "") or getattr(sender_obj, "card", "") or ""
        )
    lines = [
        "[陌生人私聊] 有人在私聊猫猫喵～",
        f"对方：{sender_nickname or '（无昵称）'} (QQ {sender_qq})",
        f"私聊类型：{sub_type or 'unknown'}",
    ]
    if raw_text:
        snippet = raw_text if len(raw_text) <= 500 else raw_text[:500] + "…(已截断)"
        lines.append(f"内容：{snippet}")
    raw_message = str(getattr(event, "message", "") or "")
    if raw_message and raw_message.strip() != raw_text:
        lines.append(f"原始 message：{raw_message[:300]}")
    lines.append("（非好友/临时会话已转发；猫猫会给对方回一句加好友提示。）")
    await _send_to_owner(bot, "\n".join(lines))
    logger.info(f"owner_forward: relayed private message from {sender_qq} sub_type={sub_type or 'unknown'} to owner")
    await _send_stranger_friend_hint(bot, sender_qq)
    if _should_block_ai_reply():
        matcher.stop_propagation()
        await matcher.finish()


async def _maybe_handle_owner_command(bot: Bot, text: str) -> bool:
    if not text:
        return False
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return False
    head = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    approve_heads = {"/同意", "同意", "/approve", "approve", "/accept", "accept", "/y"}
    reject_heads = {"/拒绝", "拒绝", "/reject", "reject", "/deny", "deny", "/n"}
    list_heads = {"/待办", "/list", "/pending"}
    if head in list_heads:
        await _send_pending_summary(bot)
        return True
    if head not in approve_heads and head not in reject_heads:
        return False
    owner_qq = _get_owner_qq()
    if not arg:
        await bot.send_private_msg(
            user_id=owner_qq,
            message="喵～主人没写要处理谁，格式 `/同意 <QQ号>` 或 `/拒绝 <QQ号>`。",
        )
        return True
    approve = head in approve_heads
    pending = _pop_pending(arg)
    if pending is None:
        await bot.send_private_msg(
            user_id=owner_qq,
            message=f"哼，找不到 {arg} 的待处理申请喵～可能已经处理过、或者已经过期。",
        )
        return True
    flag = pending.get("flag", "")
    kind = pending.get("kind", "friend")
    sub_type = pending.get("sub_type", "")
    try:
        verb = "同意" if approve else "拒绝"
        if kind == "friend":
            await bot.set_friend_add_request(flag=flag, approve=approve)
            await bot.send_private_msg(
                user_id=owner_qq,
                message=f"{verb}啦主人～(尾巴摇摇) 已经把 QQ {arg} 的好友申请处理掉了喵。",
            )
        elif kind == "group":
            await bot.set_group_add_request(flag=flag, sub_type=sub_type or "add", approve=approve)
            await bot.send_private_msg(
                user_id=owner_qq,
                message=f"{verb}啦主人～群相关申请已经处理掉了 ฅฅ",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"owner_forward: failed to handle pending request {arg}: {exc}")
        await bot.send_private_msg(
            user_id=owner_qq,
            message=f"喵呜～处理失败：{exc}",
        )
    return True


async def _send_pending_summary(bot: Bot) -> None:
    owner_qq = _get_owner_qq()
    _clean_pending()
    if not _PENDING_FLAGS:
        await bot.send_private_msg(user_id=owner_qq, message="哼，现在一个待办都没有喵～(尾巴摇摇)")
        return
    lines = ["待办申请列表喵：(共 %d 条)" % len(_PENDING_FLAGS)]
    for uid, info in _PENDING_FLAGS.items():
        kind = info.get("kind", "")
        sub_type = info.get("sub_type", "")
        ts = float(info.get("ts", 0))
        lines.append(f"- QQ {uid}  类型:{kind}/{sub_type}  时间:{time.strftime('%m-%d %H:%M', time.localtime(ts))}")
    lines.append("回复 `/同意 <QQ号>` 或 `/拒绝 <QQ号>` 处理。")
    await bot.send_private_msg(user_id=owner_qq, message="\n".join(lines))
