"""S6 (主人 2026-05-29): 自动抓 DeepSeek 问答蒸馏到 L3 corpus.

主人 2026-05-29: 蒸馏埋点收口在 openai_client 的【面向用户回复入口】(chat_completion /
chat_completion_with_tools / chat_completion_instant / chat_completion_codex_instant),
一处覆盖主 AI / fallback DeepSeek / catnify 透传回 L5 / NSFW spark / 占位话 / 签到 caption.
**不采** filter/分类/情绪/怒气判断 (输出 bool/JSON) 和后台 summary / local_critic — 那些
入口不埋, 避免把判断结果当问答污染语料. CPU 自产的 L1/L2/L3 直答也不蒸 (杜绝自循环).

回调 ``maybe_distill(scope, user_text, assistant_text, intent, source, sanitize_terms)``:
- 过滤无效对 (含 CQ 媒体 / URL / 过长过短) — 复用 bootstrap_corpus.py 规则
- 去重 (normalized hash + LRU 窗口)
- 隐私: 私聊 (含 NSFW) 也采 = 全采, 写入前 ``_sanitize`` 脱敏 (@标签 / QQ号 / 调用方传的称呼)
- 追加 ``qa_corpus_live.jsonl`` (与 bootstrap 静态 corpus 分文件)
- 累积 ``rebuild_threshold`` 条触发 ``router.reload_l3_corpus()`` 异步重 build index, 不阻塞主链路

跟 plan S4 evolution 的差异:
- S4 = 每日 03:00 批量, DeepSeek 评审 + git commit + 回滚栅栏
- S6 = 实时蒸馏, 仅过滤+写入, 不评审 (粗放但快)

主人后续可跑 S4 evolution 给 live 语料做质量审核 (S6 是 input, S4 是 filter)
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from loguru import logger


_RE_CQ_MEDIA = re.compile(r"\[CQ:(image|record|video|file|share|music|forward)")
_RE_CQ_ANY = re.compile(r"\[CQ:[a-z_]+(?:,[^\]]*)?\]")
_RE_URL = re.compile(r"(http[s]?://|www\.)", re.IGNORECASE)

# S6 (主人 2026-05-29 v3): 动态脱敏 — 硬编码词典覆盖不了, 调用方按 event 上下文传 terms.
# CQ:at,qq=xxx 标签 → {at}
_RE_CQ_AT = re.compile(r"\[CQ:at,qq=\d+(?:,[^\]]*)?\]")
# 纯 QQ 号 (5-12 位数字, 前后有空白/中文/标点) → {uid}
_RE_QQ_NUMBER = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
# 模板占位符 {xxx} 保留


def _sanitize_text(text: str, *, terms: list[str] | None = None) -> str:
    """脱敏: 去 @ 标签 / QQ 号 + 调用方提供的 terms (用户称呼/真名/群名 等).

    调用方应按 event 上下文准备 terms:
    - event.sender.nickname / card (用户 QQ 昵称/群名片)
    - bot 给该用户用过的称呼 (主人/小哥哥/{nickname} 等)
    - group_name (群名)
    - 用户在 user_text 里告诉 bot 的真名 (难, 留给后续 NER)

    替换策略: terms 长的优先 (避免子串误替换), 替换为空 (保留上下文语义).
    例:
    - terms=["主人","小李"] / "主人你好 小李去做饭啦" → "你好 去做饭啦"
    """
    if not text:
        return text
    out = text
    # 1. @标签 → {at}
    out = _RE_CQ_AT.sub("{at}", out)
    # 2. 调用方提供的 terms - 按长度倒序避免子串误替换 (例 "笨蛋主人" 在 "主人" 之前)
    if terms:
        seen: set[str] = set()
        ordered = sorted(
            (t.strip() for t in terms if t and t.strip()),
            key=lambda s: -len(s),
        )
        for t in ordered:
            if t in seen:
                continue
            seen.add(t)
            if t and t in out:
                out = out.replace(t, "")
    # 3. 纯 QQ 号 → {uid}
    out = _RE_QQ_NUMBER.sub("{uid}", out)
    # 4. 收紧空白
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _normalize_for_dedup(text: str) -> str:
    """跟 bootstrap_corpus.normalize_for_dedup 一致, 去 CQ + 小写. 脱敏后再 norm 让 dedup 更准."""
    return _RE_CQ_ANY.sub("", text or "").strip().lower()


def _is_clean(text: str, *, min_len: int, max_len: int) -> bool:
    if not text:
        return False
    if not (min_len <= len(text) <= max_len):
        return False
    if _RE_CQ_MEDIA.search(text):
        return False
    if _RE_URL.search(text):
        return False
    return True


def _scope_is_private(scope: str) -> bool:
    """scope 格式: 'group:xxx' / 'private:xxx'."""
    return scope.lower().startswith("private:")


class CorpusDistiller:
    """状态化: 维护 dedup 窗口 + 写入累计计数 + rebuild 触发."""

    def __init__(
        self,
        *,
        live_corpus_path: Path,
        rebuild_callback: Any,  # Callable[[], Awaitable[bool]] | None — router.l3.reload async wrapper
        config: Any,
    ) -> None:
        self._live_path = live_corpus_path
        self._rebuild_cb = rebuild_callback
        self._config = config
        self._dedup_window = int(
            getattr(config, "catty_cpu_engine_l3_distill_dedup_window", 0) or 1000
        )
        # LRU dedup: 最近 N 条 normalized user_text hash
        self._dedup: deque[str] = deque(maxlen=self._dedup_window)
        self._dedup_set: set[str] = set()
        self._pending_count: int = 0
        self._total_appended: int = 0
        self._last_rebuild_ts: float = 0.0
        self._rebuild_lock = asyncio.Lock()

    def update_config(self, config: Any) -> None:
        """S6 (主人 2026-05-29 hotreload): 热重载时刷新 config 引用, 让 distill 参数
        (enabled / skip_private / rebuild_threshold / 长度门槛) 不重启 bot 即生效.

        这些参数都在 _enabled / _skip_private / _rebuild_threshold / _min_*_len 里
        每次 getattr(self._config) 动态读 — 换掉引用就热生效. 只有 dedup_window 是
        构造期定的 deque maxlen, 变了才重建窗口 (保留最近 N 条).
        """
        self._config = config
        new_window = int(
            getattr(config, "catty_cpu_engine_l3_distill_dedup_window", 0) or 1000
        )
        if new_window != self._dedup_window:
            self._dedup_window = new_window
            kept = list(self._dedup)[-new_window:]
            self._dedup = deque(kept, maxlen=new_window)
            self._dedup_set = set(self._dedup)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "pending_count": self._pending_count,
            "total_appended": self._total_appended,
            "dedup_size": len(self._dedup_set),
            "last_rebuild_ts": self._last_rebuild_ts,
            "live_path": str(self._live_path),
        }

    def _enabled(self) -> bool:
        return bool(getattr(self._config, "catty_cpu_engine_l3_distill_enabled", False))

    def _skip_private(self) -> bool:
        # 主人 2026-05-29 v2: 私聊也采 (默认 False), 但走 _sanitize 脱敏后写入
        return bool(
            getattr(self._config, "catty_cpu_engine_l3_distill_skip_private", False)
        )

    def _rebuild_threshold(self) -> int:
        return int(
            getattr(self._config, "catty_cpu_engine_l3_distill_rebuild_threshold", 0)
            or 50
        )

    def _min_user_len(self) -> int:
        return int(
            getattr(self._config, "catty_cpu_engine_l3_distill_min_user_len", 0) or 4
        )

    def _max_user_len(self) -> int:
        return int(
            getattr(self._config, "catty_cpu_engine_l3_distill_max_user_len", 0) or 200
        )

    def _min_assistant_len(self) -> int:
        return int(
            getattr(self._config, "catty_cpu_engine_l3_distill_min_assistant_len", 0)
            or 8
        )

    def _max_assistant_len(self) -> int:
        return int(
            getattr(self._config, "catty_cpu_engine_l3_distill_max_assistant_len", 0)
            or 500
        )

    def _accept(self, scope: str, user_text: str, assistant_text: str) -> tuple[bool, str]:
        """返回 (是否接受, 拒绝原因供 metric)."""
        if not self._enabled():
            return False, "disabled"
        if self._skip_private() and _scope_is_private(scope):
            return False, "private_skipped"
        if not _is_clean(
            user_text,
            min_len=self._min_user_len(),
            max_len=self._max_user_len(),
        ):
            return False, "user_unclean"
        if not _is_clean(
            assistant_text,
            min_len=self._min_assistant_len(),
            max_len=self._max_assistant_len(),
        ):
            return False, "assistant_unclean"
        key = _normalize_for_dedup(user_text)
        if not key:
            return False, "empty_norm"
        if key in self._dedup_set:
            return False, "dedup_hit"
        return True, ""

    async def maybe_distill(
        self,
        *,
        scope: str,
        user_text: str,
        assistant_text: str,
        intent: str = "default",
        source: str = "deepseek",
        sanitize_terms: list[str] | None = None,
    ) -> bool:
        """主入口: 主链路 emit 后 fire-and-forget 调用. 失败永不抛.

        ``sanitize_terms`` 主人 2026-05-29 v3 决策:
        - 调用方按 event 上下文准备**要去除的词**: 真名/nickname/card/QQ号/群名/对该
          用户用过的称呼 (主人/小哥哥/{nickname} 等)
        - **绝不包含 bot 自称** (米雪儿/笨猫/猫猫/雪儿) — 这些是 character voice 保留
        - terms 为 None / 空 时仅做 @标签 + QQ号脱敏

        返回 True 表示已 append (可能仍未 rebuild); False = 过滤掉或异常.
        """
        try:
            # 主人 2026-05-29: 先脱敏再过滤+写入 - 写入文件的永远是脱敏后版本
            user_san = _sanitize_text(user_text, terms=sanitize_terms)
            assistant_san = _sanitize_text(assistant_text, terms=sanitize_terms)
            ok, reason = self._accept(scope, user_san, assistant_san)
            if not ok:
                logger.debug(f"[cpu_engine.distill] skip reason={reason} scope={scope}")
                return False

            entry = {
                "q": user_san,
                "a": assistant_san,
                "intent": intent or "default",
                "tags": [],
                "weight": 1.0,
                # scope 也脱敏: 群号 / QQ 号 → 占位
                "source": f"{source}:live:{_sanitize_text(scope)}:{int(time.time())}",
            }
            self._live_path.parent.mkdir(parents=True, exist_ok=True)
            with self._live_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # 更新 dedup (用脱敏后版本 - "主人在不?" 和 "杂鱼在不?" 视为同一条)
            key = _normalize_for_dedup(user_san)
            self._dedup.append(key)
            self._dedup_set.add(key)
            # 控制 set 大小 (deque maxlen 自动 pop 但 set 不会, 简化全量同步)
            if len(self._dedup_set) > self._dedup_window * 2:
                self._dedup_set = set(self._dedup)
            self._pending_count += 1
            self._total_appended += 1

            logger.info(
                f"[cpu_engine.distill] appended scope={scope} intent={intent} "
                f"q_len={len(user_text)} a_len={len(assistant_text)} "
                f"pending={self._pending_count}/{self._rebuild_threshold()}"
            )

            if self._pending_count >= self._rebuild_threshold():
                # 触发异步重 build, 不 await — 主链路立即返回
                asyncio.create_task(self._trigger_rebuild())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cpu_engine.distill] maybe_distill raised: {exc}")
            return False

    async def _trigger_rebuild(self) -> None:
        async with self._rebuild_lock:
            if self._pending_count < self._rebuild_threshold():
                # 多个 task 并发触发时, 第一个跑完后续直接退
                return
            if self._rebuild_cb is None:
                logger.warning("[cpu_engine.distill] rebuild_cb is None, skip")
                self._pending_count = 0
                return
            t0 = time.monotonic()
            try:
                # rebuild_cb 可能是 sync 也可能是 async — 兼容
                result = self._rebuild_cb()
                if asyncio.iscoroutine(result):
                    result = await result
                elapsed = (time.monotonic() - t0) * 1000.0
                if result:
                    self._pending_count = 0
                    self._last_rebuild_ts = time.time()
                    logger.info(
                        f"[cpu_engine.distill] L3 reload OK latency_ms={elapsed:.0f}"
                    )
                else:
                    logger.warning(
                        f"[cpu_engine.distill] L3 reload returned False "
                        f"latency_ms={elapsed:.0f} pending={self._pending_count}"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[cpu_engine.distill] L3 reload raised: {exc}")


_GLOBAL_DISTILLER: CorpusDistiller | None = None


def get_distiller(
    config: Any,
    *,
    router_reload_async: Any,
) -> CorpusDistiller:
    """惰性单例. router_reload_async 是 async fn 用来调 router.txtai_corpus.reload()."""
    global _GLOBAL_DISTILLER
    if _GLOBAL_DISTILLER is None:
        live_path = Path(
            str(
                getattr(
                    config,
                    "catty_cpu_engine_l3_distill_live_corpus_path",
                    "",
                )
                or "src/catty_qq_ai/data/cpu_engine/corpus/qa_corpus_live.jsonl"
            )
        )
        _GLOBAL_DISTILLER = CorpusDistiller(
            live_corpus_path=live_path,
            rebuild_callback=router_reload_async,
            config=config,
        )
        logger.info(
            f"[cpu_engine.distill] init live_path={live_path} "
            f"enabled={_GLOBAL_DISTILLER._enabled()} "
            f"threshold={_GLOBAL_DISTILLER._rebuild_threshold()}"
        )
    else:
        # S6 hotreload: 单例已存在 → 刷新 config 引用, 让 config.json 改的 distill 参数热生效.
        _GLOBAL_DISTILLER.update_config(config)
    return _GLOBAL_DISTILLER


def reset_distiller_for_test() -> None:
    global _GLOBAL_DISTILLER
    _GLOBAL_DISTILLER = None
