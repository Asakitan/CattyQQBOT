"""多 provider 缓存命中统计 (主人 2026-07-06 openai-claude-95 计划 §二).

按 "provider|model" 分桶 rolling — 修掉旧版全局单 deque 混模型污染 (deepseek-pro
one-shot 曾把 flash 的 rolling 平均拉低)。统一三 provider 的 HIT_TARGET 行格式。

本模块只做纯计算: 不打日志 (返回格式化字符串/状态, 由调用方用各自 logger 打, 保持
日志路由不变), 不 import 任何 catty 模块 (避免循环依赖)。

HIT_TARGET 行 (字段追加式兼容 — 老 grep 的 model=/this=/rolling/hit_tok=/miss_tok=/
scope= 键位全部保留原位, 新字段插在 miss_tok 与 scope 之间):
  HIT_TARGET model=<m> this=X.X% rollingN=Y.Y% target=95-98% status=OK|LOW(-Npp)
  hit_tok=A miss_tok=B create_tok=C prompt_tok=P provider=<p> msgs=M hist=H warm=0|1 scope=<s>

字段语义 (按 provider):
  deepseek: hit=prompt_cache_hit_tokens  miss=prompt_cache_miss_tokens  create=0
  openai:   hit=prompt_tokens_details.cached_tokens  miss=prompt_tokens-cached  create=0
  claude:   hit=cache_read_input_tokens  miss=input_tokens(未缓存新输入)
            create=cache_creation_input_tokens
命中率 = hit / (hit + miss + create); prompt_tok = 三者之和 (总输入)。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

_TARGET_MIN = 0.95
_ROLLING_MAXLEN = 20
# 桶 key = f"{provider}|{model}"; 元素 = (hit_tok, total_tok)
_ROLLING: dict[str, deque[tuple[int, int]]] = {}
_LOW_STREAK: dict[str, int] = {}
_MAX_BUCKETS = 64  # 保险丝: 模型名理论上不会爆炸, 满了整体清空重来


@dataclass
class RollingStats:
    this_rate: float
    roll_rate: float
    roll_n: int
    status: str
    should_warn: bool


def record_hit(
    provider: str,
    model: str,
    hit_tok: int,
    miss_tok: int,
    create_tok: int = 0,
) -> RollingStats:
    """记一次请求命中并返回该桶的滑动统计.

    should_warn 语义与旧 deepseek 版一致: 桶内样本 >=5 且 rolling<90% 连续 3 次
    才 True, 触发后计数重置防刷屏。
    """
    key = f"{provider}|{model}"
    bucket = _ROLLING.get(key)
    if bucket is None:
        if len(_ROLLING) >= _MAX_BUCKETS:
            _ROLLING.clear()
            _LOW_STREAK.clear()
        bucket = deque(maxlen=_ROLLING_MAXLEN)
        _ROLLING[key] = bucket
    hit_tok = max(int(hit_tok), 0)
    total = hit_tok + max(int(miss_tok), 0) + max(int(create_tok), 0)
    this_rate = (hit_tok / total) if total > 0 else 0.0
    bucket.append((hit_tok, total))
    roll_hits = sum(h for h, _ in bucket)
    roll_total = sum(t for _, t in bucket)
    roll_rate = (roll_hits / roll_total) if roll_total > 0 else 0.0
    if roll_rate >= _TARGET_MIN:
        status = "OK"
    else:
        status = f"LOW(-{int((_TARGET_MIN - roll_rate) * 100)}pp)"
    should_warn = False
    if len(bucket) >= 5 and roll_rate < 0.9:
        streak = _LOW_STREAK.get(key, 0) + 1
        if streak >= 3:
            should_warn = True
            streak = 0
        _LOW_STREAK[key] = streak
    else:
        _LOW_STREAK[key] = 0
    return RollingStats(
        this_rate=this_rate,
        roll_rate=roll_rate,
        roll_n=len(bucket),
        status=status,
        should_warn=should_warn,
    )


def compute_warm_fields(messages: list | None) -> tuple[int, int, int]:
    """返回 (msgs, hist, warm).

    hist = messages 里 assistant 条数 (≈ 已完成对话轮数); warm = 1 当 hist>=2 —
    warm 会话口径 (主人拍板 95% KPI 的统计口径)。阈值不进 config: 原始字段都在
    HIT_TARGET 行里, A/B 脚本侧可自行调口径, 免重部署。
    """
    if not messages:
        return 0, 0, 0
    try:
        msgs = len(messages)
        hist = sum(
            1
            for m in messages
            if isinstance(m, dict) and m.get("role") == "assistant"
        )
    except Exception:  # noqa: BLE001
        return 0, 0, 0
    return msgs, hist, 1 if hist >= 2 else 0


def format_hit_target_line(
    *,
    provider: str,
    model: str,
    stats: RollingStats,
    hit_tok: int,
    miss_tok: int,
    create_tok: int,
    msgs: int,
    hist: int,
    warm: int,
    scope: str,
) -> str:
    prompt_tok = int(hit_tok) + int(miss_tok) + int(create_tok)
    return (
        f"HIT_TARGET model={(model or '')[:20]} this={stats.this_rate:.1%} "
        f"rolling{stats.roll_n}={stats.roll_rate:.1%} "
        f"target=95-98% status={stats.status} "
        f"hit_tok={hit_tok} miss_tok={miss_tok} create_tok={create_tok} "
        f"prompt_tok={prompt_tok} provider={provider} "
        f"msgs={msgs} hist={hist} warm={warm} scope={scope}"
    )
