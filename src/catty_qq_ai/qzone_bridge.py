from __future__ import annotations

import html
import math
import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx


class QzoneBridgeError(RuntimeError):
    pass


def _action_url(base_url: str, action: str) -> str:
    return f"{base_url.rstrip('/')}/{action.lstrip('/')}"


async def call_qzone_action(
    base_url: str,
    action: str,
    params: Mapping[str, Any],
    *,
    access_token: str = "",
    timeout_seconds: float = 30.0,
) -> Any:
    headers: dict[str, str] = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                _action_url(base_url, action), json=dict(params), headers=headers
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise QzoneBridgeError(f"QZone action {action!r} HTTP request failed: {exc}") from exc
    try:
        wrapper = response.json()
    except (TypeError, ValueError) as exc:
        raise QzoneBridgeError(f"QZone action {action!r} returned invalid JSON") from exc
    if not isinstance(wrapper, Mapping):
        raise QzoneBridgeError(f"QZone action {action!r} returned a non-object JSON value")
    if wrapper.get("status") != "ok":
        raise QzoneBridgeError(f"QZone action {action!r} returned status {wrapper.get('status')!r}")
    try:
        retcode = int(wrapper.get("retcode"))
    except (TypeError, ValueError) as exc:
        raise QzoneBridgeError(f"QZone action {action!r} returned an invalid retcode") from exc
    if retcode != 0:
        raise QzoneBridgeError(f"QZone action {action!r} returned retcode {retcode}")
    return wrapper.get("data")


def extract_friend_feed_items(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        data = payload
    msglist = data.get("msglist")
    if not isinstance(msglist, (list, tuple)):
        return []
    return [item for item in msglist if isinstance(item, Mapping)]


def _first_value(raw: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name not in raw:
            continue
        value = raw[name]
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value
    return None


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _parse_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        return datetime.fromisoformat(iso_text).timestamp()
    except ValueError:
        return None


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<[^>]*>", "", text)
    return " ".join(html.unescape(text).split())


def _conlist_text(value: Any) -> str:
    if not isinstance(value, (list, tuple)):
        return ""
    parts: list[str] = []
    for item in value:
        piece = _first_value(item, ("con", "content")) if isinstance(item, Mapping) else item
        if piece is not None:
            parts.append(str(piece))
    return "".join(parts)


def normalize_friend_feed(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    feed_id_value = _first_value(raw, ("tid", "cellid"))
    author_uin_value = _first_value(raw, ("uin", "opuin", "frienduin"))
    author_name_value = _first_value(raw, ("nickname", "name"))
    conlist_text = _conlist_text(raw.get("conlist"))
    text_value = conlist_text or _first_value(raw, ("content", "con", "cellcontent"))
    feed_id = "" if feed_id_value is None else str(feed_id_value).strip()
    author_uin = "" if author_uin_value is None else str(author_uin_value).strip()
    text = _clean_text(text_value)
    if not feed_id or not author_uin or not text:
        return None
    feed_time = _parse_timestamp(
        _first_value(raw, ("feed_time", "created_time", "createdTime", "ctime", "pubtime"))
    )
    if feed_time is None or not math.isfinite(feed_time) or feed_time <= 0:
        return None
    return {
        "feed_id": feed_id,
        "author_uin": author_uin,
        "author_name": "" if author_name_value is None else str(author_name_value).strip(),
        "text": text,
        "like_count": max(_as_int(_first_value(raw, ("like_count", "likenum", "likeNum", "likecount", "likeCount"))), 0),
        "liked_by": [],
        "feed_time": feed_time,
        "recorded_at": time.time(),
        "source": "qzone",
        "_is_liked": _as_bool(_first_value(raw, ("isLiked", "is_liked", "liked"))),
        "_appid": _as_int(raw.get("appid")),
        "_typeid": _as_int(raw.get("typeid")),
        "_unikey": "" if raw.get("likeUnikey") is None else str(raw.get("likeUnikey")).strip(),
        "_curkey": "" if raw.get("likeCurkey") is None else str(raw.get("likeCurkey")).strip(),
    }


def build_like_params(feed: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "user_id": feed.get("author_uin", ""),
        "tid": feed.get("feed_id", ""),
        "abstime": int(_parse_timestamp(feed.get("feed_time")) or 0),
    }
    for feed_key, request_key in (("_appid", "appid"), ("_typeid", "typeid"), ("_unikey", "unikey"), ("_curkey", "curkey")):
        value = feed.get(feed_key)
        if feed_key in {"_appid", "_typeid"}:
            parsed = _as_int(value)
            if parsed:
                params[request_key] = parsed
        else:
            text = "" if value is None else str(value).strip()
            if text:
                params[request_key] = text
    return params