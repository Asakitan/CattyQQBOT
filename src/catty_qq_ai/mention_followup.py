from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import time
from typing import Any, Awaitable, Callable

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, MessageSegment

from .message_utils import contains_at_all, event_plain_text, mentions_other_user, reply_message_ids


@dataclass(slots=True)
class PendingMention:
    key: tuple[str, str, str, str]
    message_id: str
    deadline: float
    events: list[MessageEvent] = field(default_factory=list)


class MentionFollowups:
    def __init__(
        self, *, window_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.window_seconds = window_seconds
        self.clock = clock
        self.sleep = sleep
        self.recent: OrderedDict[tuple[str, str, str, str], deque] = OrderedDict()
        self.pending: dict[tuple[str, str, str, str], PendingMention] = {}
        self.consumed: OrderedDict[tuple, float] = OrderedDict()

    @staticmethod
    def key(event: MessageEvent, persona: str) -> tuple[str, str, str, str]:
        return str(event.self_id), str(event.group_id), str(event.user_id), persona

    @staticmethod
    def message_id(event: MessageEvent) -> str:
        return str(getattr(event, "message_id", "") or id(event))

    def _prune(self) -> None:
        now = self.clock()
        for key, expiry in list(self.consumed.items()):
            if expiry <= now:
                self.consumed.pop(key, None)
        for key, rows in list(self.recent.items()):
            while rows and rows[0][0] < now - self.window_seconds:
                rows.popleft()
            if not rows and key not in self.pending:
                self.recent.pop(key, None)
        while len(self.recent) > 256:
            self.recent.popitem(last=False)
        while len(self.consumed) > 4096:
            self.consumed.popitem(last=False)

    def _capture(self, pending: PendingMention, event: MessageEvent) -> None:
        message_id = self.message_id(event)
        if message_id == pending.message_id or any(self.message_id(item) == message_id for item in pending.events):
            return
        if len(pending.events) >= 80:
            return
        pending.events.append(event)
        self.consumed[(*pending.key, message_id)] = self.clock() + 60.0

    def observe(self, event: MessageEvent, persona: str) -> None:
        if not isinstance(event, GroupMessageEvent) or str(event.user_id) == str(event.self_id):
            return
        if contains_at_all(event) or mentions_other_user(str(event.self_id), event) or reply_message_ids(event):
            return
        if any(segment.type == "file" for segment in event.get_message()):
            return
        if event_plain_text(event).lstrip().startswith("/"):
            return
        self._prune()
        key = self.key(event, persona)
        rows = self.recent.setdefault(key, deque(maxlen=80))
        self.recent.move_to_end(key)
        message_id = self.message_id(event)
        if any(self.message_id(item) == message_id for _, item in rows):
            return
        snapshot = event.model_copy(deep=True)
        now = self.clock()
        rows.append((now, snapshot))
        pending = self.pending.get(key)
        if pending is not None and now <= pending.deadline:
            self._capture(pending, snapshot)

    def is_consumed(self, event: MessageEvent, persona: str) -> bool:
        if not isinstance(event, GroupMessageEvent):
            return False
        self._prune()
        return (*self.key(event, persona), self.message_id(event)) in self.consumed

    def begin(self, event: MessageEvent, persona: str) -> PendingMention | None:
        self._prune()
        key = self.key(event, persona)
        if key in self.pending or len(self.pending) >= 256:
            return None
        message_id = self.message_id(event)
        rows = list(self.recent.get(key, ()))
        anchor = next((index for index, (_, item) in enumerate(rows) if self.message_id(item) == message_id), None)
        started_at = rows[anchor][0] if anchor is not None else self.clock()
        pending = PendingMention(key, message_id, started_at + self.window_seconds)
        self.pending[key] = pending
        if anchor is not None:
            for received_at, item in rows[anchor + 1:]:
                if received_at <= pending.deadline:
                    self._capture(pending, item)
        return pending

    async def collect(self, pending: PendingMention) -> list[MessageEvent]:
        try:
            await self.sleep(max(pending.deadline - self.clock(), 0.0))
            return list(pending.events)
        finally:
            if self.pending.get(pending.key) is pending:
                self.pending.pop(pending.key, None)


def merge_mention_events(event: MessageEvent, following: list[MessageEvent]) -> MessageEvent:
    message = Message(event.get_message())
    for item in following:
        message += MessageSegment.text("\n") + item.get_message()
    return event.model_copy(update={
        "message": message, "original_message": Message(message), "raw_message": str(message),
    })