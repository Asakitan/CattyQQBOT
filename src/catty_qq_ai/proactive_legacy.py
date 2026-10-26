"""Legacy catty proactive orchestration, isolated from the FDJ scheduler."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence


@dataclass(frozen=True)
class LegacyBubbleHost:
    config: Callable[[], Any]
    has_api_key: Callable[[], bool]
    bots: Callable[[], Sequence[Any]]
    candidate_group_ids: Callable[[Any], Awaitable[list[str]]]
    memory: Callable[[], Any]
    persona: Callable[[str], Any]
    send: Callable[[Any, str], Awaitable[bool]]
    on_error: Callable[[str, Exception], None]


async def legacy_proactive_tick(host: LegacyBubbleHost, *, sleep: Callable = asyncio.sleep) -> None:
    cfg = host.config()
    if not cfg.catty_proactive_enabled or not host.has_api_key():
        return
    for bot in host.bots():
        groups = await host.candidate_group_ids(bot)
        cfg = host.config()
        due = host.memory().due_proactive_group_ids(
            groups,
            max_daily=max(cfg.catty_proactive_max_daily_per_group, 0),
            min_interval_minutes=max(cfg.catty_proactive_min_interval_minutes, 1.0),
            active_window_minutes=max(cfg.catty_proactive_active_window_minutes, 0.0),
            active_min_messages=max(cfg.catty_proactive_active_min_messages, 0),
        )
        due = [gid for gid in due if not host.persona(f"group:{gid}").feature_disabled("proactive_bubble")]
        for group_id in due:
            try:
                await host.send(bot, group_id)
            except Exception as exc:
                host.on_error(group_id, exc)
            await sleep(2)


async def run_legacy_proactive_loop(host: LegacyBubbleHost) -> None:
    while True:
        await asyncio.sleep(max(host.config().catty_proactive_check_interval_seconds, 60.0))
        await legacy_proactive_tick(host)