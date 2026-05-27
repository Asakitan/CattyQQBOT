"""HanLP 中文 NER/POS 包装 — lazy load + 轻量 pipeline.

主人 2026-05-28 plan: 给 user_details_store 提供命名实体 + 词性识别,
替换原 7 个 regex pattern, 抓更多自然语言变体里的事实.

模型: FINE_ELECTRA_SMALL_ZH (~80MB, NER+POS+TOK 一站式) — 在 4 核 CPU ~50-200ms.
特性:
- HF 走 hf-mirror.com 镜像
- lazy load (第一次 extract 时加载, 阻塞 5-15s)
- 失败 graceful fallback: extract_entities() 返回 None
- 短消息 (<8 char) 跳过 NER (无信息量, 省 CPU)

输出格式 (extract_entities):
{
  "PER": [...],   # 人名
  "LOC": [...],   # 地点
  "ORG": [...],   # 组织
  "DATE": [...],  # 时间
  "TOK": [...],   # 全部 token (POS-tagged 时是 [(word, pos), ...])
  "POS": [(word, pos), ...]  # 词性标注
}
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

logger = logging.getLogger("catty.nlu.hanlp")


_PIPELINE: Any = None
_LOAD_FAILED: bool = False
_LOAD_LOCK = threading.Lock()
_MIN_TEXT_LEN = 8  # 短于这个跳过 NER (省 CPU)
_MAX_TEXT_LEN = 200  # 长文截断 (NER 模型对长文吃 CPU 严重)


def _config() -> Any:
    try:
        from nonebot import get_driver
        return get_driver().config
    except Exception:
        return None


def _is_enabled() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "catty_use_hanlp", False)) if cfg else False


def _get_pipeline_name() -> str:
    cfg = _config()
    name = getattr(cfg, "catty_hanlp_pipeline", "") if cfg else ""
    return name or "FINE_ELECTRA_SMALL_ZH"


def _setup_hf_endpoint() -> None:
    cfg = _config()
    endpoint = (
        (getattr(cfg, "catty_nlu_hf_endpoint", "") if cfg else "") or ""
    ).strip()
    if endpoint and "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = endpoint


def _load_blocking() -> Any | None:
    """同步加载 HanLP pipeline. 第一次会下模型 ~80MB."""
    global _PIPELINE, _LOAD_FAILED
    if _LOAD_FAILED:
        return None
    if _PIPELINE is not None:
        return _PIPELINE
    with _LOAD_LOCK:
        if _LOAD_FAILED:
            return None
        if _PIPELINE is not None:
            return _PIPELINE
        try:
            _setup_hf_endpoint()
            import hanlp  # type: ignore
            pipeline_name = _get_pipeline_name()
            logger.info("loading hanlp pipeline: %s (first call ~ 5-15s)", pipeline_name)
            # hanlp.load 接受预定义常量字符串
            pipeline_const = getattr(hanlp.pretrained.mtl, pipeline_name, None)
            if pipeline_const is None:
                # fallback to small chinese multi-task
                pipeline_const = hanlp.pretrained.mtl.CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_SMALL_ZH
            _PIPELINE = hanlp.load(pipeline_const)
            logger.info("hanlp ready")
            return _PIPELINE
        except Exception as exc:
            logger.warning("hanlp load failed, falling back to None: %s", exc)
            _LOAD_FAILED = True
            return None


def is_hanlp_available() -> bool:
    return _PIPELINE is not None and not _LOAD_FAILED


def extract_entities_sync(text: str | None) -> dict[str, list] | None:
    """同步 NER + POS. 返回 {PER, LOC, ORG, DATE, POS}. 失败/空/短 → None.

    POS 是 [(word, tag), ...]; PER/LOC/ORG/DATE 是 ner span 的 surface text 列表.
    """
    if not _is_enabled():
        return None
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if len(text) < _MIN_TEXT_LEN:
        return None
    text = text[:_MAX_TEXT_LEN]
    pipeline = _load_blocking()
    if pipeline is None:
        return None
    try:
        # HanLP mtl pipeline 接受 list 或 str, 返回 dict (含 tok/pos/ner 等 key)
        result = pipeline(text, tasks=("tok", "pos", "ner"))
        out: dict[str, list] = {"PER": [], "LOC": [], "ORG": [], "DATE": [], "POS": []}
        # tok: list[str], pos: list[str], ner: list[(start, end, type, surface)]
        toks = result.get("tok/fine") or result.get("tok") or []
        poses = result.get("pos/ctb") or result.get("pos") or []
        if isinstance(toks, list) and toks and isinstance(toks[0], list):
            toks = toks[0]
        if isinstance(poses, list) and poses and isinstance(poses[0], list):
            poses = poses[0]
        out["POS"] = list(zip(toks, poses)) if (toks and poses) else []
        ner = result.get("ner/msra") or result.get("ner") or []
        if isinstance(ner, list) and ner and isinstance(ner[0], list):
            ner = ner[0]
        for span in ner or []:
            try:
                if len(span) >= 3:
                    ent_type = str(span[1]).upper() if isinstance(span[1], str) else ""
                    surface = str(span[0])
                    # HanLP 实体 type: PERSON/PER, LOCATION/LOC, ORGANIZATION/ORG, DATE
                    if ent_type.startswith("PER"):
                        out["PER"].append(surface)
                    elif ent_type.startswith("LOC"):
                        out["LOC"].append(surface)
                    elif ent_type.startswith("ORG"):
                        out["ORG"].append(surface)
                    elif ent_type.startswith("DATE") or ent_type.startswith("TIM"):
                        out["DATE"].append(surface)
            except Exception:
                continue
        return out
    except Exception as exc:
        logger.debug("hanlp extract failed on text len=%d: %s", len(text), exc)
        return None


async def extract_entities(text: str | None) -> dict[str, list] | None:
    """异步包装. 走 asyncio.to_thread 不阻塞 event loop."""
    if not _is_enabled() or not text:
        return None
    return await asyncio.to_thread(extract_entities_sync, text)


async def warmup() -> bool:
    """启动时调一次, 后台预加载."""
    if not _is_enabled():
        return False
    try:
        pipeline = await asyncio.to_thread(_load_blocking)
        return pipeline is not None
    except Exception as exc:
        logger.warning("hanlp warmup failed: %s", exc)
        return False


__all__ = [
    "extract_entities",
    "extract_entities_sync",
    "is_hanlp_available",
    "warmup",
]
