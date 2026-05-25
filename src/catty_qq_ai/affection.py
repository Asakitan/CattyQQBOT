"""签到积分 + 好感度等级 系统。

数据模型（每个用户一条记录,key = QQ user_id 字符串）:
    {
        "points": int,                 # 当前可用积分
        "exp": int,                    # 累计好感度经验
        "last_checkin_date": "YYYY-MM-DD",
        "last_checkin_amount": int,
        "total_checkins": int,
        "daily_exp_date": "YYYY-MM-DD",
        "daily_exp_count": int,        # 当天已累积的好感度(用于 cap)
        "total_consumed": int,         # 历史消费积分总额(用于统计)
        "updated_at": iso-string,
    }

主人(catty_owner_qq)豁免:
- get_points 永远返回 OWNER_INFINITY_POINTS
- consume_points 永远成功且不真正扣
- get_level_and_exp 永远返回 (LEVEL_CAP, exp)
- 签到也允许,但实际不影响余额(只刷下 last_checkin 留档)
"""
from __future__ import annotations

import json
import random
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from nonebot import logger

from .config import Config


LEVEL_CAP = 10
EXP_PER_LEVEL = 100  # 每升一级需要的好感度经验; level = min(exp//100 + 1, 10)
DAILY_EXP_CAP = 100  # 每天单用户最多累积 100 好感度,防刷屏

CHECKIN_BASE_MIN = 200
CHECKIN_BASE_MAX = 300
CHECKIN_LEVEL_BONUS_STEP = 80      # Lv1 +80, Lv10 +800
CHECKIN_LEVEL_BONUS_CAP = 750      # 等级加成上限
CHECKIN_TOTAL_CAP = 1000           # 单次签到总分上限

IMAGE_COST_LOW = 20
IMAGE_COST_MEDIUM = 50
IMAGE_COST_HIGH = 100
IMAGE_COST_AUTO = 50

OWNER_INFINITY_POINTS = 9_999_999


def _today_local() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _level_from_exp(exp: int) -> int:
    if exp <= 0:
        return 1
    return min(int(exp // EXP_PER_LEVEL) + 1, LEVEL_CAP)


def _checkin_bonus_for_level(level: int) -> int:
    return min(max(level, 1) * CHECKIN_LEVEL_BONUS_STEP, CHECKIN_LEVEL_BONUS_CAP)


def predict_checkin_range(level: int) -> tuple[int, int]:
    """返回某等级签到能拿到的积分上下界(含等级加成,已 clamp 到 1000)。"""
    bonus = _checkin_bonus_for_level(level)
    lo = min(CHECKIN_BASE_MIN + bonus, CHECKIN_TOTAL_CAP)
    hi = min(CHECKIN_BASE_MAX + bonus, CHECKIN_TOTAL_CAP)
    return lo, hi


def image_cost_for_quality(quality: str) -> int:
    q = (quality or "").strip().lower()
    if q == "low":
        return IMAGE_COST_LOW
    if q == "medium":
        return IMAGE_COST_MEDIUM
    if q == "high":
        return IMAGE_COST_HIGH
    if q == "auto":
        return IMAGE_COST_AUTO
    return IMAGE_COST_LOW


class AffectionStore:
    """积分 + 好感度持久化层。

    存储独立于 MemoryStore: 数据落 `<memory_dir>/affection.json`,atomic write。
    高频改动(每条聊天 +1 exp)用 _dirty 标记,后台 task 5s flush 一次。
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        mem_path = Path(config.catty_memory_path).expanduser()
        if not mem_path.is_absolute():
            mem_path = mem_path.resolve()
        self._path = mem_path.parent / "affection.json"
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._load()

    # ── 加载/持久化 ────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"affection_store: load failed, starting empty: {exc}")
            return
        if not isinstance(raw, dict):
            return
        users = raw.get("users") if isinstance(raw.get("users"), dict) else raw
        for uid, record in users.items():
            if isinstance(record, dict):
                self._data[str(uid)] = record

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {"version": 1, "users": self._data}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    def flush_sync(self) -> bool:
        with self._lock:
            if not self._dirty:
                return False
            try:
                self._atomic_write()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"affection_store: flush failed: {exc}")
                return False
            self._dirty = False
            return True

    async def background_flush_loop(self) -> None:
        import asyncio
        while True:
            try:
                await asyncio.sleep(5.0)
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"affection_store: bg flush failed: {exc}")

    # ── 主人判定 ──────────────────────────────────────────────────
    def is_owner(self, user_id: str | int) -> bool:
        owner = str(getattr(self._config, "catty_owner_qq", "") or "").strip()
        if not owner or owner == "0":
            return False
        return str(user_id) == owner

    # ── 内部:取/建用户记录(不复制,直接 mutable 操作) ────────────────
    def _record(self, user_id: str) -> dict[str, Any]:
        rec = self._data.get(user_id)
        if rec is None:
            rec = {
                "points": 0,
                "exp": 0,
                "last_checkin_date": "",
                "last_checkin_amount": 0,
                "total_checkins": 0,
                "daily_exp_date": "",
                "daily_exp_count": 0,
                "total_consumed": 0,
                "updated_at": _now_iso(),
            }
            self._data[user_id] = rec
        return rec

    # ── 查询接口 ──────────────────────────────────────────────────
    def get_points(self, user_id: str | int) -> int:
        uid = str(user_id)
        if self.is_owner(uid):
            return OWNER_INFINITY_POINTS
        with self._lock:
            return int(self._record(uid).get("points") or 0)

    def get_level_and_exp(self, user_id: str | int) -> tuple[int, int]:
        uid = str(user_id)
        if self.is_owner(uid):
            return LEVEL_CAP, EXP_PER_LEVEL * LEVEL_CAP
        with self._lock:
            exp = int(self._record(uid).get("exp") or 0)
        return _level_from_exp(exp), exp

    def summary(self, user_id: str | int) -> dict[str, Any]:
        uid = str(user_id)
        level, exp = self.get_level_and_exp(uid)
        with self._lock:
            rec = dict(self._record(uid)) if not self.is_owner(uid) else {}
        next_lv_exp = (level * EXP_PER_LEVEL) if level < LEVEL_CAP else None
        lo, hi = predict_checkin_range(level)
        return {
            "user_id": uid,
            "is_owner": self.is_owner(uid),
            "points": self.get_points(uid),
            "level": level,
            "exp": exp,
            "exp_to_next_level": (next_lv_exp - exp) if next_lv_exp is not None else 0,
            "next_level_at_exp": next_lv_exp,
            "last_checkin_date": rec.get("last_checkin_date", ""),
            "last_checkin_amount": int(rec.get("last_checkin_amount") or 0),
            "total_checkins": int(rec.get("total_checkins") or 0),
            "total_consumed": int(rec.get("total_consumed") or 0),
            "checkin_range_today": (lo, hi),
        }

    # ── 操作接口 ──────────────────────────────────────────────────
    def daily_checkin(self, user_id: str | int) -> dict[str, Any]:
        """执行签到。返回 {success, already, gained, balance, level, ...}。"""
        uid = str(user_id)
        today = _today_local()
        with self._lock:
            rec = self._record(uid)
            last = str(rec.get("last_checkin_date") or "")
            if last == today:
                level = LEVEL_CAP if self.is_owner(uid) else _level_from_exp(int(rec.get("exp") or 0))
                return {
                    "success": False,
                    "already": True,
                    "balance": self.get_points(uid),
                    "level": level,
                    "last_amount": int(rec.get("last_checkin_amount") or 0),
                }
            level = LEVEL_CAP if self.is_owner(uid) else _level_from_exp(int(rec.get("exp") or 0))
            base = random.randint(CHECKIN_BASE_MIN, CHECKIN_BASE_MAX)
            bonus = _checkin_bonus_for_level(level)
            gained = min(base + bonus, CHECKIN_TOTAL_CAP)
            if self.is_owner(uid):
                # 主人签到只留档,不真正变更余额(本来就无限)
                rec["last_checkin_date"] = today
                rec["last_checkin_amount"] = gained
                rec["total_checkins"] = int(rec.get("total_checkins") or 0) + 1
                rec["updated_at"] = _now_iso()
                self._dirty = True
                return {
                    "success": True,
                    "already": False,
                    "is_owner": True,
                    "gained": gained,
                    "base": base,
                    "bonus": bonus,
                    "level": LEVEL_CAP,
                    "balance": OWNER_INFINITY_POINTS,
                }
            rec["points"] = int(rec.get("points") or 0) + gained
            rec["last_checkin_date"] = today
            rec["last_checkin_amount"] = gained
            rec["total_checkins"] = int(rec.get("total_checkins") or 0) + 1
            rec["updated_at"] = _now_iso()
            self._dirty = True
            return {
                "success": True,
                "already": False,
                "is_owner": False,
                "gained": gained,
                "base": base,
                "bonus": bonus,
                "level": level,
                "balance": int(rec["points"]),
            }

    def add_exp(self, user_id: str | int, amount: int = 1) -> dict[str, Any]:
        """累积好感度,带每日 cap。返回 {added, exp, level, level_up}。"""
        uid = str(user_id)
        if amount <= 0:
            level, exp = self.get_level_and_exp(uid)
            return {"added": 0, "exp": exp, "level": level, "level_up": False}
        if self.is_owner(uid):
            return {"added": 0, "exp": EXP_PER_LEVEL * LEVEL_CAP, "level": LEVEL_CAP, "level_up": False}
        today = _today_local()
        with self._lock:
            rec = self._record(uid)
            if str(rec.get("daily_exp_date") or "") != today:
                rec["daily_exp_date"] = today
                rec["daily_exp_count"] = 0
            daily = int(rec.get("daily_exp_count") or 0)
            room = max(DAILY_EXP_CAP - daily, 0)
            if room <= 0:
                exp = int(rec.get("exp") or 0)
                return {"added": 0, "exp": exp, "level": _level_from_exp(exp), "level_up": False, "capped": True}
            actual = min(amount, room)
            old_exp = int(rec.get("exp") or 0)
            new_exp = old_exp + actual
            old_level = _level_from_exp(old_exp)
            new_level = _level_from_exp(new_exp)
            rec["exp"] = new_exp
            rec["daily_exp_count"] = daily + actual
            rec["updated_at"] = _now_iso()
            self._dirty = True
            return {
                "added": actual,
                "exp": new_exp,
                "level": new_level,
                "level_up": new_level > old_level,
            }

    def consume_points(self, user_id: str | int, cost: int) -> dict[str, Any]:
        """扣积分。返回 {ok, balance_before, balance_after, cost, level, shortfall}。"""
        uid = str(user_id)
        if cost <= 0:
            return {"ok": True, "balance_before": self.get_points(uid), "balance_after": self.get_points(uid), "cost": 0}
        if self.is_owner(uid):
            return {
                "ok": True,
                "is_owner": True,
                "balance_before": OWNER_INFINITY_POINTS,
                "balance_after": OWNER_INFINITY_POINTS,
                "cost": cost,
            }
        with self._lock:
            rec = self._record(uid)
            balance = int(rec.get("points") or 0)
            if balance < cost:
                level = _level_from_exp(int(rec.get("exp") or 0))
                lo, hi = predict_checkin_range(level)
                return {
                    "ok": False,
                    "balance_before": balance,
                    "balance_after": balance,
                    "cost": cost,
                    "shortfall": cost - balance,
                    "level": level,
                    "checkin_range_today": (lo, hi),
                }
            new_balance = balance - cost
            rec["points"] = new_balance
            rec["total_consumed"] = int(rec.get("total_consumed") or 0) + cost
            rec["updated_at"] = _now_iso()
            self._dirty = True
            return {
                "ok": True,
                "balance_before": balance,
                "balance_after": new_balance,
                "cost": cost,
            }

    # ── persona prompt 注入辅助 ───────────────────────────────────
    def persona_hint(self, user_id: str | int) -> str:
        """生成一段简短的 system prompt,告诉笨猫当前对话用户的好感度等级。"""
        uid = str(user_id)
        if self.is_owner(uid):
            return (
                "【特殊关系】当前正在和你的主人对话——他是你最依赖、最亲近的人；"
                "保持平时的傲娇撒娇基调,可以更黏更不自觉地暴露真心。"
            )
        level, exp = self.get_level_and_exp(uid)
        # 4 档关系等级,描述给主 AI 看
        if level <= 2:
            tag = "陌生"
            tone = (
                "对方是新认识的群友/聊天对象,你和他还不熟。"
                "保持笨猫人格,但傲娇的『杂鱼』『笨蛋主人』这类极亲昵称呼不要乱给他——"
                "用『你』『喂』就好,适度可爱但有距离感。"
            )
        elif level <= 5:
            tag = "渐熟"
            tone = (
                "对方是已经聊过一阵的熟人,关系正在升温。"
                "可以更放松地撒娇、调侃,但还没到主人那种全心依赖的程度。"
                "适当称呼对方名字/QQ昵称,偶尔『笨蛋』可以,『主人』要慎用。"
            )
        elif level <= 7:
            tag = "亲近"
            tone = (
                "对方是聊得很熟的朋友,你已经很愿意跟他贴贴。"
                "可以撒娇、嘴硬、互怼互拷打,语气接近对待主人但少一些『主人』专属称呼。"
                "回复更短更黏更随意。"
            )
        else:
            tag = "超熟/挚友"
            tone = (
                "对方是超级熟、几乎和主人同级的挚友。"
                "全力撒娇黏人、嘴硬反差大、各种猫系动作和小尾巴词放开来,"
                "可以叫『笨蛋XX』之类的爱称,主动贴贴蹭蹭。"
            )
        return (
            f"【关系亲密度】当前对话用户好感度 Lv{level}/{LEVEL_CAP} (经验 {exp},档位:{tag})。\n"
            f"{tone}"
        )
