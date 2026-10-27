"""Independent FDJ bubbles: host adapters in, one durable delivery attempt out.

No plugin import, user-turn fabrication, tool execution or legacy proactive quota.
All scheduler I/O is drained on cancellation; an invoked send is never retried.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import inspect
import logging
import random
import time
from typing import Any, Awaitable, Callable, Sequence

from .fdj_proactive import BubblePolicy, BubbleScheduler
from .fdj_proactive_prompt import build_fdj_proactive_messages
from .persona_runtime import bind_turn_persona, reset_turn_persona
from .reply_delivery import ReplyDeliveryState

_LOG = logging.getLogger(__name__)
_LOCAL_TIME = timezone(timedelta(hours=8))


async def _drain(task: asyncio.Task) -> Any:
    """Keep ownership until a worker/commit completes, including repeated cancels."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def store_call(fn: Callable, *args: Any, cancel_result: Callable | None = None, **kwargs: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await _drain(task)
        if cancel_result is not None:
            await _drain(asyncio.create_task(asyncio.to_thread(cancel_result, result)))
        raise


@dataclass(frozen=True)
class BubbleHost:
    config: Callable[[], Any]
    persona: Callable[[str], Any]
    bots: Callable[[], Sequence[Any]]
    recent: Callable[[str], Sequence[Any]]
    busy: Callable[[str], bool]
    has_api_key: Callable[[], bool]
    generate: Callable[[Any, list[dict]], Awaitable[str]]
    send_text: Callable[[Any, str, str], Awaitable[Any]]
    remember: Callable[[Any, str, str, str], None]
    clean_text: Callable[..., str]


class FdjProactiveRuntime:
    def __init__(
        self, host: BubbleHost, *, wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        scheduler_factory: Callable = BubbleScheduler, rng: Any = None,
    ) -> None:
        self.host = host
        self.wall = wall
        self.monotonic = monotonic
        self.scheduler_factory = scheduler_factory
        self.rng = rng if rng is not None else random.SystemRandom()
        self._scheduler: Any = None
        self._scheduler_key: Any = None
        self._groups: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._tick_lock = asyncio.Lock()

    def _settings(self, cfg: Any) -> tuple[str, BubblePolicy]:
        return str(getattr(cfg, "catty_fdj_proactive_dir", "data/fdj_proactive")), BubblePolicy(
            daily_min=int(getattr(cfg, "catty_fdj_proactive_daily_min", 5)),
            daily_max=int(getattr(cfg, "catty_fdj_proactive_daily_max", 10)),
            min_gap_seconds=float(getattr(cfg, "catty_fdj_proactive_min_interval_minutes", 60)) * 60,
            active_window_seconds=float(getattr(cfg, "catty_fdj_proactive_active_window_minutes", 15)) * 60,
            active_min_messages=int(getattr(cfg, "catty_fdj_proactive_active_min_messages", 5)),
            warm_window_seconds=float(getattr(cfg, "catty_fdj_proactive_warm_window_minutes", 360)) * 60,
            night_chance=float(getattr(cfg, "catty_fdj_proactive_night_chance", .2)),
            failure_backoff_seconds=float(getattr(cfg, "catty_fdj_proactive_failure_backoff_minutes", 60)) * 60,
            slot_grace_seconds=1800,
        )

    @staticmethod
    def _config_source(persona: Any) -> str:
        return getattr(persona, "proactive_config_source", (
            "fdj" if getattr(persona, "name", "") == "fadianji" else "legacy"
        ))

    @classmethod
    def _persona_stamp(cls, persona: Any) -> tuple:
        return (
            id(persona), getattr(persona, "name", None),
            getattr(persona, "definition_revision", None),
            getattr(persona, "proactive_enabled", True), cls._config_source(persona),
        )

    def _stamp(self, cfg: Any, persona: Any) -> tuple:
        source = self._config_source(persona)
        switch = "catty_fdj_proactive_enabled" if source == "fdj" else "catty_proactive_enabled"
        return (
            id(cfg), self._settings(cfg),
            float(getattr(cfg, "catty_fdj_proactive_timeout_seconds", 30)),
            tuple(sorted(str(g) for g in (getattr(cfg, "catty_allowed_group_ids", ()) or ()))),
            bool(getattr(cfg, "catty_enable_group", True)),
            source, bool(getattr(cfg, switch, True)),
        )

    async def _get_scheduler(self, cfg: Any) -> Any:
        key = self._settings(cfg)
        if self._scheduler is None or key != self._scheduler_key:
            scheduler = await store_call(self.scheduler_factory, *key)
            self._scheduler, self._scheduler_key = scheduler, key
        return self._scheduler

    def _enabled(self, cfg: Any = None) -> bool:
        if cfg is None:
            cfg = self.host.config()
        return self.host.has_api_key() and (
            bool(getattr(cfg, "catty_fdj_proactive_enabled", True))
            or bool(getattr(cfg, "catty_proactive_enabled", True))
        )

    def _eligible(
        self, group_id: str, *, persona: Any = None, stamp: Any = None,
        persona_stamp: Any = None, check_busy: bool = True,
    ) -> bool:
        cfg = self.host.config()
        if not self._enabled(cfg) or not bool(getattr(cfg, "catty_enable_group", True)):
            return False
        allowed = {str(g) for g in (getattr(cfg, "catty_allowed_group_ids", ()) or ())}
        if allowed and group_id not in allowed:
            return False
        scope = f"group:{group_id}"
        current = self.host.persona(scope)
        if current is None or not getattr(current, "name", "") or (check_busy and self.host.busy(scope)):
            return False
        source = self._config_source(current)
        if source not in {"legacy", "fdj"} or not bool(getattr(current, "proactive_enabled", True)):
            return False
        switch = "catty_fdj_proactive_enabled" if source == "fdj" else "catty_proactive_enabled"
        if not bool(getattr(cfg, switch, True)):
            return False
        if persona is not None and current is not persona:
            return False
        if persona_stamp is not None and self._persona_stamp(current) != persona_stamp:
            return False
        return stamp is None or self._stamp(cfg, current) == stamp

    async def _group_ids(self, bot: Any, *, refresh: bool = False) -> tuple[str, ...]:
        key = str(bot.self_id)
        now = self.monotonic()
        cached = self._groups.get(key)
        if not refresh and cached is not None and 0 <= now - cached[0] < 300:
            return cached[1]
        try:
            rows = await asyncio.wait_for(bot.get_group_list(), timeout=30)
            if not isinstance(rows, list):
                raise ValueError("invalid group list")
            groups = tuple(sorted({
                str(row["group_id"]) for row in rows
                if isinstance(row, dict) and str(row.get("group_id", "")).isdigit()
            }))
        except Exception as exc:
            # Fail closed, including when the old confirmed cache has expired.
            # Never consult MemoryStore or delete memories on this path.
            _LOG.warning("FDJ group list unavailable: %s", type(exc).__name__)
            groups = ()
        self._groups[key] = (self.monotonic(), groups)
        return groups

    def _membership_confirmed(self, bot: Any, group_id: str) -> bool:
        cached = self._groups.get(str(bot.self_id))
        return bool(cached is not None and 0 <= self.monotonic() - cached[0] < 300 and group_id in cached[1])

    def _snapshot(self, bot: Any, group_id: str) -> tuple[list[dict], list[float], tuple]:
        cfg = self.host.config()
        warm = max(float(getattr(cfg, "catty_fdj_proactive_warm_window_minutes", 360)) * 60, 0)
        now, mono = self.wall(), self.monotonic()
        rows: list[dict] = []
        humans: list[float] = []
        context: list[tuple] = []
        for item in tuple(self.host.recent(f"group:{group_id}")):
            age = mono - float(item.created_at)
            if age < 0 or age > warm:
                continue
            is_bot = bool(item.is_bot) or str(item.user_id) == str(bot.self_id)
            text = str(item.text or "").strip()
            if not text:
                continue
            rows.append({"speaker": str(item.display_name), "text": text, "is_bot": is_bot})
            if not is_bot:
                humans.append(now - age)
            context.append((is_bot, getattr(item, "seq", None), item.created_at, str(item.user_id), text))
        return rows, humans, tuple(context)

    @staticmethod
    def _context_fresh(before: tuple, after: tuple) -> bool:
        old_humans = {item for item in before if not item[0]}
        new_humans = {item for item in after if not item[0]}
        old_bots = {item for item in before if item[0]}
        new_bots = {item for item in after if item[0]}
        # Normal ongoing human chat is precisely where a bubble belongs. Yield
        # when a bot has answered, or the whole prompt context has aged/rolled out.
        return bool(old_humans & new_humans) and not (new_bots - old_bots)

    def _window_open(self, candidate: Any, window: tuple[float, float]) -> bool:
        now = self.wall()
        return window[0] <= now < window[1] and datetime.fromtimestamp(now, _LOCAL_TIME).date().isoformat() == candidate.day

    @staticmethod
    def _same_slot(left: Any, right: Any) -> bool:
        return right is not None and all(
            getattr(left, name) == getattr(right, name) for name in ("group_id", "day", "slot_id")
        )

    async def send_candidate(
        self, bot: Any, scheduler: Any, candidate: Any, *, persona: Any = None,
        stamp: Any = None, persona_stamp: Any = None, context: Any = None,
    ) -> bool:
        group_id = str(candidate.group_id)
        scope = f"group:{group_id}"
        cfg = self.host.config()
        if persona is None:
            persona = self.host.persona(scope)
        if stamp is None:
            stamp = self._stamp(cfg, persona)
        if persona_stamp is None:
            persona_stamp = self._persona_stamp(persona)

        async def skip(reason: str) -> bool:
            await store_call(scheduler.skip, candidate, self.wall(), reason=reason)
            return False

        if scheduler is not self._scheduler or self._settings(cfg) != self._scheduler_key:
            return await skip("settings_changed")
        if self.host.busy(scope):
            return False  # No LLM work yet: let this slot wait for a quieter tick.
        if not self._eligible(group_id, persona=persona, stamp=stamp, persona_stamp=persona_stamp):
            return await skip("unavailable")
        if group_id not in await self._group_ids(bot, refresh=True):
            return await skip("membership_changed")
        rows, humans, latest = self._snapshot(bot, group_id)
        if not humans or (context is not None and not self._context_fresh(context, latest)):
            return await skip("cold")
        due = await store_call(scheduler.due, group_id, self.wall(), human_times=humans)
        if not self._same_slot(candidate, due):
            return await skip("expired")
        status = await store_call(scheduler.status, group_id, self.wall())
        slot = next((item for item in status.get("slots", ()) if item.get("slot_id") == candidate.slot_id), None)
        if not status.get("ok") or slot is None:
            return await skip("unconfirmed_slot")
        window = (float(slot["at"]), float(slot["expires_at"]))
        if not self._window_open(candidate, window):
            return await skip("expired")
        if self.host.busy(scope):
            return False
        _, current_humans, current_context = self._snapshot(bot, group_id)
        if (
            not current_humans or not self._context_fresh(latest, current_context)
            or not self._membership_confirmed(bot, group_id)
            or not self._window_open(candidate, window)
            or not self._eligible(group_id, persona=persona, stamp=stamp, persona_stamp=persona_stamp)
        ):
            return await skip("changed_before_generation")
        rows, _, latest = self._snapshot(bot, group_id)
        generation_started = self.monotonic()
        timeout = max(float(getattr(cfg, "catty_fdj_proactive_timeout_seconds", 30)), .01)
        token = bind_turn_persona(scope, persona)
        try:
            messages = build_fdj_proactive_messages(
                persona, scope=scope, mode=candidate.mode, recent_messages=rows,
                now=datetime.fromtimestamp(self.wall(), _LOCAL_TIME),
            )
            reply = await asyncio.wait_for(
                self.host.generate(cfg, messages),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            await skip("generation_cancelled")
            raise
        except Exception as exc:
            _LOG.info("FDJ generation skipped: %s", type(exc).__name__)
            return await skip("generation_failed")
        finally:
            reset_turn_persona(token)
        try:
            try:
                inspect.signature(self.host.clean_text).bind(reply, persona)
            except (TypeError, ValueError):
                text = self.host.clean_text(reply)
            else:
                text = self.host.clean_text(reply, persona)
        except Exception:
            return await skip("invalid_reply")
        if not text:
            return await skip("no_reply")
        # Persona lookup is deliberately outside the bound generation context.
        if not self._eligible(group_id, persona=persona, stamp=stamp, persona_stamp=persona_stamp):
            return await skip("changed_after_generation")
        if group_id not in await self._group_ids(bot, refresh=True):
            return await skip("membership_changed")
        _, humans, current_latest = self._snapshot(bot, group_id)
        if not humans or not self._context_fresh(latest, current_latest) or self.monotonic() - generation_started > timeout:
            return await skip("stale_context")
        due = await store_call(scheduler.due, group_id, self.wall(), human_times=humans)
        if not self._same_slot(candidate, due):
            return await skip("expired")
        if not self._eligible(group_id, persona=persona, stamp=stamp, persona_stamp=persona_stamp):
            return await skip("changed_before_claim")
        attempt = await store_call(
            lambda: scheduler.claim(candidate, self.wall()),
            cancel_result=lambda result: scheduler.finish(group_id, result, "failed", self.wall()) if result else None,
        )
        if not attempt:
            return await skip("claim_rejected")
        outcome = "failed"
        try:
            delivery = ReplyDeliveryState()

            async def checked_send(message: str) -> Any:
                nonlocal outcome
                _, current_humans, current_context = self._snapshot(bot, group_id)
                if (
                    not current_humans or not self._context_fresh(latest, current_context)
                    or self.monotonic() - generation_started > timeout
                    or not self._window_open(candidate, window)
                    or not self._eligible(group_id, persona=persona, stamp=stamp, persona_stamp=persona_stamp)
                    or not self._membership_confirmed(bot, group_id)
                ):
                    raise RuntimeError("pre_send_changed")
                outcome = "unknown"
                ack = await self.host.send_text(bot, group_id, message)
                # Missing/malformed ACKs are unconfirmed, not delivered receipts.
                message_id = ack.get("message_id") if isinstance(ack, dict) else None
                if isinstance(message_id, bool) or not isinstance(message_id, (str, int)) or not str(message_id).strip() or message_id == 0:
                    raise ValueError("missing send acknowledgement")
                return ack

            if group_id not in await self._group_ids(bot, refresh=True):
                raise RuntimeError("membership_changed")
            ack = await asyncio.wait_for(delivery.send(checked_send, text, history_text=text), timeout=timeout)
            outcome = "sent"
            token = bind_turn_persona(scope, persona)
            try:
                self.host.remember(bot, group_id, text, str(ack["message_id"]))
            finally:
                reset_turn_persona(token)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.info("FDJ delivery %s: %s", outcome, type(exc).__name__)
            return False
        finally:
            await store_call(scheduler.finish, group_id, attempt, outcome, self.wall())

    async def tick(self) -> bool:
        if self._tick_lock.locked() or not self._enabled():
            return False
        async with self._tick_lock:
            cfg = self.host.config()
            scheduler = await self._get_scheduler(cfg)
            options: list[tuple[Any, str, list[float], bool, Any, tuple, tuple, tuple]] = []
            now = self.wall()
            active_seconds = float(getattr(cfg, "catty_fdj_proactive_active_window_minutes", 15)) * 60
            active_min = int(getattr(cfg, "catty_fdj_proactive_active_min_messages", 5))
            for bot in self.host.bots():
                for group_id in await self._group_ids(bot):
                    current_cfg = self.host.config()
                    persona = self.host.persona(f"group:{group_id}")
                    persona_stamp = self._persona_stamp(persona)
                    stamp = self._stamp(current_cfg, persona)
                    if not self._eligible(
                        group_id, persona=persona, stamp=stamp, persona_stamp=persona_stamp,
                        check_busy=False,
                    ):
                        continue
                    # Busy groups keep their ungenerated opportunity until a
                    # later quiet tick or the scheduler's normal slot expiry.
                    _, humans, context = self._snapshot(bot, group_id)
                    if not humans:
                        continue
                    active = sum(now - stamp <= active_seconds for stamp in humans) >= active_min
                    options.append((bot, group_id, humans, active, persona, stamp, persona_stamp, context))
            self.rng.shuffle(options)
            options.sort(key=lambda row: not row[3])  # active first; random/fair within each tier
            for bot, group_id, humans, _, persona, stamp, persona_stamp, context in options:
                candidate = await store_call(scheduler.due, group_id, self.wall(), human_times=humans)
                if candidate is not None:
                    return await self.send_candidate(
                        bot, scheduler, candidate, persona=persona, stamp=stamp,
                        persona_stamp=persona_stamp, context=context,
                    )
            return False

    async def run(self) -> None:
        while True:
            cfg = self.host.config()
            await asyncio.sleep(max(float(getattr(cfg, "catty_fdj_proactive_check_interval_seconds", 60)), 1))
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.warning("FDJ bubble tick failed: %s", type(exc).__name__)