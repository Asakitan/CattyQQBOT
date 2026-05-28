"""S4.1 Evolution emit/feedback 日志 (双向: 写 emit + 读采样).

记录 CPU 层 emit 的回复 + 用户后续反应, 供每日 DeepSeek 评审采样.
存储: append-only JSONL, 按日期分文件 (data/cpu_engine/evolution_logs/emit_YYYY-MM-DD.jsonl).

隐私:
- 私聊样本不进 evolution (config.catty_evolution_sample_only_group=True)
- 写盘时仍记录但 sampler 跳过, 保留私聊调试用

主人 2026-05-28 plan-cpu-alicebot-nlu-ai S4.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger


_EMIT_FILE_PREFIX = "emit_"
_FEEDBACK_FILE_PREFIX = "feedback_"
_DEFAULT_DIR = "src/catty_qq_ai/data/cpu_engine/evolution_logs"


@dataclass(slots=True)
class EmitRecord:
    ts: float
    scope: str
    user_id: str
    layer: str  # L1/L2/L3/L4/L0_beg
    route_name: str
    intent: str
    confidence: float
    reply: str  # 已渲染的最终输出
    user_text: str  # 用户原文 (脱敏后)
    matched_text: str = ""


@dataclass(slots=True)
class FeedbackRecord:
    ts: float
    scope: str
    user_id: str
    emit_ts: float  # 关联的 emit 时间戳
    kind: str  # follow_up_intent / affection_delta / recall / explicit_bad
    payload: dict[str, Any] = field(default_factory=dict)


def _today_filename(dir_path: Path, *, kind: str = "emit") -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    prefix = _FEEDBACK_FILE_PREFIX if kind == "feedback" else _EMIT_FILE_PREFIX
    return dir_path / f"{prefix}{today}.jsonl"


def _sanitize(text: str, max_len: int = 400) -> str:
    """脱敏 + 截断. QQ号占位 / URL 占位."""
    import re
    out = re.sub(r"\d{5,12}", "{uid}", text)
    out = re.sub(r"https?://\S+", "{url}", out)
    return out[:max_len]


def record_emit(
    *,
    log_dir: str | Path,
    scope: str,
    user_id: str,
    layer: str,
    route_name: str,
    intent: str,
    confidence: float,
    reply: str,
    user_text: str,
    matched_text: str = "",
) -> bool:
    """记录 CPU 层一次 emit. append-only, 失败仅 warn."""
    try:
        path = _today_filename(Path(log_dir), kind="emit")
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = EmitRecord(
            ts=time.time(),
            scope=scope,
            user_id=str(user_id),
            layer=layer,
            route_name=route_name,
            intent=intent,
            confidence=float(confidence),
            reply=_sanitize(reply, max_len=400),
            user_text=_sanitize(user_text, max_len=300),
            matched_text=_sanitize(matched_text, max_len=120),
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution.logger] record_emit failed: {exc}")
        return False


def record_feedback(
    *,
    log_dir: str | Path,
    scope: str,
    user_id: str,
    emit_ts: float,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """记录用户对某次 emit 的反应. kind: follow_up_intent / affection_delta / recall / explicit_bad."""
    try:
        path = _today_filename(Path(log_dir), kind="feedback")
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = FeedbackRecord(
            ts=time.time(),
            scope=scope,
            user_id=str(user_id),
            emit_ts=float(emit_ts),
            kind=kind,
            payload=payload or {},
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution.logger] record_feedback failed: {exc}")
        return False


def load_emits(
    log_dir: str | Path,
    *,
    since_ts: float,
    until_ts: float | None = None,
    skip_private: bool = True,
) -> list[EmitRecord]:
    """读最近窗口 emit. since_ts 起, until_ts 止 (None = now)."""
    log_dir = Path(log_dir)
    if not log_dir.exists():
        return []
    until = until_ts if until_ts is not None else time.time()
    records: list[EmitRecord] = []
    since_dt = datetime.fromtimestamp(since_ts)
    days_back = max(int((datetime.now() - since_dt).days) + 1, 1)
    for offset in range(days_back):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = log_dir / f"{_EMIT_FILE_PREFIX}{day}.jsonl"
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = float(data.get("ts", 0))
                    if ts < since_ts or ts > until:
                        continue
                    scope = str(data.get("scope", ""))
                    if skip_private and scope.startswith("private:"):
                        continue
                    records.append(EmitRecord(
                        ts=ts,
                        scope=scope,
                        user_id=str(data.get("user_id", "")),
                        layer=str(data.get("layer", "")),
                        route_name=str(data.get("route_name", "")),
                        intent=str(data.get("intent", "")),
                        confidence=float(data.get("confidence", 0.0)),
                        reply=str(data.get("reply", "")),
                        user_text=str(data.get("user_text", "")),
                        matched_text=str(data.get("matched_text", "")),
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution.logger] load_emits read {path} failed: {exc}")
    return records


def load_feedbacks(log_dir: str | Path, *, since_ts: float) -> list[FeedbackRecord]:
    log_dir = Path(log_dir)
    if not log_dir.exists():
        return []
    records: list[FeedbackRecord] = []
    since_dt = datetime.fromtimestamp(since_ts)
    days_back = max(int((datetime.now() - since_dt).days) + 1, 1)
    for offset in range(days_back):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = log_dir / f"{_FEEDBACK_FILE_PREFIX}{day}.jsonl"
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if float(data.get("ts", 0)) < since_ts:
                        continue
                    records.append(FeedbackRecord(
                        ts=float(data.get("ts", 0)),
                        scope=str(data.get("scope", "")),
                        user_id=str(data.get("user_id", "")),
                        emit_ts=float(data.get("emit_ts", 0)),
                        kind=str(data.get("kind", "")),
                        payload=dict(data.get("payload", {}) or {}),
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution.logger] load_feedbacks read {path} failed: {exc}")
    return records


def sample_emits_by_layer(
    emits: list[EmitRecord],
    *,
    samples_per_layer: int = 30,
) -> list[EmitRecord]:
    """按 layer 分桶, 每桶最多 samples_per_layer 条, 按 ts 倒序优先取最近."""
    by_layer: dict[str, list[EmitRecord]] = defaultdict(list)
    for r in sorted(emits, key=lambda x: x.ts, reverse=True):
        if len(by_layer[r.layer]) < samples_per_layer:
            by_layer[r.layer].append(r)
    out: list[EmitRecord] = []
    for layer in sorted(by_layer.keys()):
        out.extend(by_layer[layer])
    return out


def compute_negative_feedback_rate(
    emits: list[EmitRecord],
    feedbacks: list[FeedbackRecord],
) -> float:
    """近窗口负反馈率: count(explicit_bad / complaint / recall) / count(emits).

    返回 0.0-1.0.
    """
    if not emits:
        return 0.0
    negative_count = sum(
        1 for f in feedbacks
        if f.kind in {"explicit_bad", "recall", "complaint"}
    )
    return min(negative_count / max(len(emits), 1), 1.0)
