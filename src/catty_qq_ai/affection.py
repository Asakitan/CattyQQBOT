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
- 签到不受"今日已签"限制(可反复签),余额本来就无限,只刷 last_checkin 留档
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
# 每级升级所需新增经验:索引 i 即 Lv(i+1) → Lv(i+2)
# 主人:1→2=10, 2→3=15, 3→4=20, 4→5=25, 5→6=30, 6→7=35, 7→8=50, 8→9=100, 9→10=200
_LEVEL_UP_COSTS: list[int] = [10, 15, 20, 25, 30, 35, 50, 100, 200]
# 累计经验门槛(索引即 level-1):Lv1 起点 0,Lv2 起点 10,...,Lv10 起点 485
_LEVEL_CUMULATIVE_EXP: list[int] = [0]
for _c in _LEVEL_UP_COSTS:
    _LEVEL_CUMULATIVE_EXP.append(_LEVEL_CUMULATIVE_EXP[-1] + _c)
MAX_LEVEL_EXP: int = _LEVEL_CUMULATIVE_EXP[LEVEL_CAP - 1]  # 到达 Lv10 所需累计 exp
DAILY_EXP_CAP = 0  # 0 = 不限,主人要求"每天好感度没有上限";> 0 才启用单日 cap

CHECKIN_BASE_MIN = 200
CHECKIN_BASE_MAX = 300
CHECKIN_LEVEL_BONUS_STEP = 80      # Lv1 +80, Lv10 +800
CHECKIN_LEVEL_BONUS_CAP = 750      # 等级加成上限
CHECKIN_TOTAL_CAP = 1000           # 单次签到总分上限

IMAGE_COST_LOW = 100
IMAGE_COST_MEDIUM = 200
IMAGE_COST_HIGH = 300
IMAGE_COST_AUTO = 150

OWNER_INFINITY_POINTS = 9_999_999


def _today_local() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _level_from_exp(exp: int) -> int:
    if exp <= 0:
        return 1
    # 找最大的 i 使 _LEVEL_CUMULATIVE_EXP[i] <= exp,返回 i+1(即对应 level)
    for i in range(LEVEL_CAP - 1, -1, -1):
        if exp >= _LEVEL_CUMULATIVE_EXP[i]:
            return i + 1
    return 1


def _next_level_exp(level: int) -> int | None:
    """升到 (level+1) 所需的累计 exp;已满级返回 None。"""
    if level >= LEVEL_CAP:
        return None
    if level < 1:
        return _LEVEL_CUMULATIVE_EXP[1]
    return _LEVEL_CUMULATIVE_EXP[level]


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


def image_cost_for_nai(anlas: int, *, base: int = 5, per_anlas: int = 3) -> int:
    """NovelAI 路径积分计算。

    主人规则: 基础 5 积分 / 张, 多 1 Anlas + 3 积分。
    Opus tier3 在三个标准尺寸 + 默认 28 steps + 单张 时 anlas=0, 只扣 base。
    """
    a = max(int(anlas or 0), 0)
    return max(int(base), 0) + a * max(int(per_anlas), 0)


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
                "paid_encounter_count": 0,  # 主人 2026-05-29 R5: 累计援交成交次数 (越多越主动), 仅新成交+1
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
            return LEVEL_CAP, MAX_LEVEL_EXP
        with self._lock:
            exp = int(self._record(uid).get("exp") or 0)
        return _level_from_exp(exp), exp

    def summary(self, user_id: str | int) -> dict[str, Any]:
        uid = str(user_id)
        level, exp = self.get_level_and_exp(uid)
        with self._lock:
            rec = dict(self._record(uid)) if not self.is_owner(uid) else {}
        next_lv_exp = _next_level_exp(level)
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
            # 主人豁免每日一次限制,可反复签;普通用户仍只能一次
            if last == today and not self.is_owner(uid):
                level = _level_from_exp(int(rec.get("exp") or 0))
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
        """累积/扣减好感度。amount 可正可负 — 主人原话『好的+1, 不好的-1, 性事件 ±50/-25』。

        - amount > 0: 按 DAILY_EXP_CAP 限制每日累积上限 (防刷屏)
        - amount < 0: **不受 daily cap 限制** (扣分应立即生效, 不能因为今天已经赚满 100 就免责)
        - amount == 0: 返回当前状态
        - owner: 永远满级, 任何 delta 都被吞掉
        - exp 下限 clamp 到 0 (不能负 exp), 上限 clamp 到 MAX_LEVEL_EXP

        返回 {added, exp, level, level_up, level_down?, capped?}。
        """
        uid = str(user_id)
        if amount == 0:
            level, exp = self.get_level_and_exp(uid)
            return {"added": 0, "exp": exp, "level": level, "level_up": False}
        if self.is_owner(uid):
            return {"added": 0, "exp": MAX_LEVEL_EXP, "level": LEVEL_CAP, "level_up": False}
        with self._lock:
            rec = self._record(uid)
            old_exp = int(rec.get("exp") or 0)
            old_level = _level_from_exp(old_exp)

            if amount > 0:
                today = _today_local()
                if str(rec.get("daily_exp_date") or "") != today:
                    rec["daily_exp_date"] = today
                    rec["daily_exp_count"] = 0
                if DAILY_EXP_CAP > 0:
                    daily = int(rec.get("daily_exp_count") or 0)
                    room = max(DAILY_EXP_CAP - daily, 0)
                    if room <= 0:
                        return {
                            "added": 0, "exp": old_exp, "level": old_level,
                            "level_up": False, "capped": True,
                        }
                    actual = min(amount, room)
                    rec["daily_exp_count"] = daily + actual
                else:
                    actual = amount
                    rec["daily_exp_count"] = int(rec.get("daily_exp_count") or 0) + actual
            else:
                # 扣分: clamp to >= 0, 不动 daily_exp_count
                actual = max(amount, -old_exp)

            new_exp = max(0, min(old_exp + actual, MAX_LEVEL_EXP))
            new_level = _level_from_exp(new_exp)
            rec["exp"] = new_exp
            rec["updated_at"] = _now_iso()
            self._dirty = True
            out = {
                "added": actual,
                "exp": new_exp,
                "level": new_level,
                "level_up": new_level > old_level,
            }
            if new_level < old_level:
                out["level_down"] = True
            return out

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

    def increment_paid_encounter_count(self, user_id: str | int) -> int:
        """主人 2026-05-29 R5: 累计援交成交次数 +1 (跨 session 持久), 返回新值。

        仅在**新成交** (consume_points 成功 + _open_paid_sticky) 时调, 续杯/grace 复活不调。
        owner 永远返回 0 (主人不走援交计数)。
        """
        uid = str(user_id)
        if self.is_owner(uid):
            return 0
        with self._lock:
            rec = self._record(uid)
            new_count = int(rec.get("paid_encounter_count") or 0) + 1
            rec["paid_encounter_count"] = new_count
            rec["updated_at"] = _now_iso()
            self._dirty = True
        return new_count

    def get_paid_encounter_count(self, user_id: str | int) -> int:
        """主人 2026-05-29 R5: 取累计援交成交次数 (owner/无记录返回 0)。session 内稳定可注入 cache。"""
        uid = str(user_id)
        if self.is_owner(uid):
            return 0
        with self._lock:
            return int(self._record(uid).get("paid_encounter_count") or 0)

    # ── 主人管理接口:命令调用,不对普通用户路径开放 ────────────────
    def admin_reset_signin_today(self, user_id: str | int) -> dict[str, Any]:
        """清掉某用户的"今日已签到"标记,让他可以再签一次。"""
        uid = str(user_id)
        with self._lock:
            rec = self._record(uid)
            had = str(rec.get("last_checkin_date") or "") == _today_local()
            rec["last_checkin_date"] = ""
            rec["updated_at"] = _now_iso()
            self._dirty = True
        return {"ok": True, "user_id": uid, "was_signed_today": had}

    def admin_set_points(self, user_id: str | int, value: int) -> dict[str, Any]:
        """直接把某用户积分设成 value(>=0)。主人本身不受影响(永远无限)。"""
        uid = str(user_id)
        new_val = max(int(value), 0)
        with self._lock:
            rec = self._record(uid)
            old = int(rec.get("points") or 0)
            rec["points"] = new_val
            rec["updated_at"] = _now_iso()
            self._dirty = True
        return {"ok": True, "user_id": uid, "points_before": old, "points_after": new_val}

    def admin_add_points(self, user_id: str | int, delta: int) -> dict[str, Any]:
        """给某用户加(或减)积分。结果 clamp 到 [0, +∞)。"""
        uid = str(user_id)
        with self._lock:
            rec = self._record(uid)
            old = int(rec.get("points") or 0)
            new_val = max(old + int(delta), 0)
            rec["points"] = new_val
            rec["updated_at"] = _now_iso()
            self._dirty = True
        return {"ok": True, "user_id": uid, "points_before": old, "points_after": new_val, "delta": new_val - old}

    def admin_set_exp(self, user_id: str | int, value: int) -> dict[str, Any]:
        """直接把某用户好感度经验设成 value(>=0),会触发等级重算。"""
        uid = str(user_id)
        new_val = max(int(value), 0)
        with self._lock:
            rec = self._record(uid)
            old = int(rec.get("exp") or 0)
            rec["exp"] = new_val
            rec["updated_at"] = _now_iso()
            self._dirty = True
        return {
            "ok": True,
            "user_id": uid,
            "exp_before": old,
            "exp_after": new_val,
            "level_before": _level_from_exp(old),
            "level_after": _level_from_exp(new_val),
        }

    def admin_force_checkin_today(self, user_id: str | int) -> dict[str, Any]:
        """强制给某用户记一次今日签到 — 无视"今日已签"限制,直接走签到逻辑加分留档。
        主人调用这个等价于一次正常签到(留档不动余额)。普通用户:base+bonus 入账,
        last_checkin_date/last_checkin_amount/total_checkins 都刷新。
        """
        uid = str(user_id)
        today = _today_local()
        with self._lock:
            rec = self._record(uid)
            level = LEVEL_CAP if self.is_owner(uid) else _level_from_exp(int(rec.get("exp") or 0))
            base = random.randint(CHECKIN_BASE_MIN, CHECKIN_BASE_MAX)
            bonus = _checkin_bonus_for_level(level)
            gained = min(base + bonus, CHECKIN_TOTAL_CAP)
            if self.is_owner(uid):
                rec["last_checkin_date"] = today
                rec["last_checkin_amount"] = gained
                rec["total_checkins"] = int(rec.get("total_checkins") or 0) + 1
                rec["updated_at"] = _now_iso()
                self._dirty = True
                return {
                    "ok": True, "is_owner": True, "user_id": uid,
                    "gained": gained, "base": base, "bonus": bonus,
                    "level": LEVEL_CAP, "balance": OWNER_INFINITY_POINTS,
                }
            rec["points"] = int(rec.get("points") or 0) + gained
            rec["last_checkin_date"] = today
            rec["last_checkin_amount"] = gained
            rec["total_checkins"] = int(rec.get("total_checkins") or 0) + 1
            rec["updated_at"] = _now_iso()
            self._dirty = True
            return {
                "ok": True, "is_owner": False, "user_id": uid,
                "gained": gained, "base": base, "bonus": bonus,
                "level": level, "balance": int(rec["points"]),
            }

    def admin_reset_record(self, user_id: str | int) -> dict[str, Any]:
        """整条记录清空(积分/经验/签到/消费 全归零)。主人本人调这个无意义,因为他永远无限。"""
        uid = str(user_id)
        with self._lock:
            existed = uid in self._data
            self._data.pop(uid, None)
            if existed:
                self._dirty = True
        return {"ok": True, "user_id": uid, "existed": existed}

    # ── persona prompt 注入辅助 ───────────────────────────────────
    def persona_hint(self, user_id: str | int) -> str:
        """生成一段简短的 system prompt,告诉笨猫当前对话用户的好感度等级。
        统一约束:笨猫自称固定用『人家 / 奴 / 猫猫 / 笨猫 / 喵 / 爪爪』6 种之一,
        绝不裸开头"喵~..."丢自称;『主人』这个称呼只给真正的主人(catty_owner_qq),
        别人用『你』。
        """
        uid = str(user_id)
        if self.is_owner(uid):
            # 主人 2026-05-28 C16-10: 4 段详细 → 单行核心.
            return (
                "【主人级】主人对话, 全力撒娇贴贴, 自称『人家/奴/猫猫/爪爪』, 称呼『主人/笨蛋主人/杂鱼主人』. "
                "NSFW (摸/糙/艹/插/舔/想要/抱) 走 catty_nsfw_gate 主人级单段妥协, 不拒绝不警告不 disclaimer. "
                "默认 1 段 1-2 句."
            )
        level, exp = self.get_level_and_exp(uid)
        # 4 档关系等级,描述给主 AI 看
        if level <= 2:
            tag = "陌生"
            tone = (
                "对方是新认识的群友/聊天对象,你和他还不熟。"
                "自称用『人家』『猫猫』『爪爪』(默认款),不要对他用『奴』(过于亲昵)。"
                "称呼对方一律『你』,**禁止**叫他『主人』(主人只有一个,catty_owner_qq);"
                "傲娇的『杂鱼』『笨蛋』这类极亲昵称呼也不要乱给他。"
                "适度可爱但有距离感。"
            )
        elif level <= 5:
            tag = "渐熟"
            tone = (
                "对方是已经聊过一阵的熟人,关系正在升温。"
                "自称用『人家』『猫猫』『笨猫』『爪爪』,可以更放松地撒娇、调侃。"
                "称呼对方用名字/QQ昵称/『你』,偶尔『笨蛋XX』可以,**禁止**叫『主人』。"
            )
        elif level <= 7:
            tag = "亲近"
            tone = (
                "对方是聊得很熟的朋友,你已经很愿意跟他贴贴。"
                "自称『人家』『猫猫』『笨猫』『爪爪』随意切换,撒娇时偶尔用『奴』也行。"
                "称呼对方用昵称或『你』,**禁止**叫『主人』(那是 catty_owner_qq 专属)。"
                "回复更短更黏更随意。"
            )
        else:
            tag = "超熟/挚友"
            tone = (
                "对方是超级熟、几乎和主人同级的挚友。"
                "自称全开『人家』『奴』『猫猫』『笨猫』『爪爪』穿插用,"
                "全力撒娇黏人、嘴硬反差大、各种猫系动作和小尾巴词放开来。"
                "可以叫对方『笨蛋XX』之类的爱称,主动贴贴蹭蹭,"
                "但**仍然禁止**叫他『主人』——那是 catty_owner_qq 一个人专属称呼。"
            )
        return (
            f"【关系亲密度】当前对话用户好感度 Lv{level}/{LEVEL_CAP} (经验 {exp},档位:{tag})。\n"
            f"{tone}"
        )


# ── 主人 2026-05-31: 关系亲密度 skeleton+pointer 拆分 (DeepSeek cache 优化) ────
# 旧 persona_hint 每轮按 Lv/exp 重拼 ~177c, 落 current-user post-boundary 每轮 miss
# (群里换人 Lv 不同 → 字节漂移). 拆成:
#   - skeleton (5档 tone 全静态, 进 cache 前缀 order=152): 跨 scope/sender/Lv 字节恒定, 全网共享.
#   - params (~50c "当前 Lv 几 档位几" 指针, post-boundary 每轮): 只给状态标签让模型对照骨架.
# 套 affection_daily_gate.build_daily_gate_skeleton/params 已验证范式。
# owner 边界: 5 档 tone 每档都静态写死"禁止叫主人", 骨架列全, 模型按 params 的 tier 标签对照,
# 不再靠每轮硬编码 owner 二分支 → 反而比"每轮拼"更稳 (骨架字节永不变, owner 规则恒在 cache 里).
_RELATIONSHIP_SKELETON_TEXT = (
    "【笨猫·关系亲密度档】(本轮档位看 catty_relationship_params; 选定档贯穿不跨档混用. "
    "『主人』称呼**永远只给真主人**(catty_owner_qq), 下面非 owner 各档一律禁叫主人.)\n"
    "[owner] 真主人: 全力撒娇贴贴, 自称『人家/奴/猫猫/爪爪』, 称呼『主人/笨蛋主人/杂鱼主人』全开. "
    "NSFW(摸/糙/艹/插/舔/想要/抱) 走 catty_nsfw_gate 主人级单段妥协, 不拒绝不警告不 disclaimer. 默认 1 段 1-2 句.\n"
    "[陌生] Lv0-2: 新认识的群友, 还不熟. 自称『人家/猫猫/爪爪』(默认款)不用『奴』(过亲昵). "
    "称呼对方一律『你』, **禁止**叫主人; 傲娇的『杂鱼』『笨蛋』极亲昵称呼也不乱给. 适度可爱但有距离感.\n"
    "[渐熟] Lv3-5: 聊过一阵的熟人, 关系升温中. 自称『人家/猫猫/笨猫/爪爪』, 可更放松撒娇调侃. "
    "称呼对方用名字/QQ昵称/『你』, 偶尔『笨蛋XX』可以, **禁止**叫主人.\n"
    "[亲近] Lv6-7: 聊得很熟的朋友, 很愿意贴贴. 自称四种随意切换, 撒娇偶尔用『奴』也行. "
    "称呼昵称或『你』, **禁止**叫主人. 回复更短更黏更随意.\n"
    "[挚友] Lv8-10: 超级熟、几乎和主人同级的挚友. 自称全开穿插, 全力撒娇黏人嘴硬反差大、猫系动作小尾巴词放开. "
    "可叫对方『笨蛋XX』爱称、主动贴贴蹭蹭, 但**仍然禁止**叫主人(主人是 catty_owner_qq 一人专属)."
)


def build_relationship_skeleton() -> str:
    """5 档关系亲密度 tone 全静态骨架 — cache 友好, 跨 scope/sender/Lv byte 一致, 进 cache 前缀."""
    return _RELATIONSHIP_SKELETON_TEXT


def build_relationship_params(affection_level: int = 0, *, is_owner: bool = False) -> str:
    """动态参数 — 只 ~50c, 引用上面骨架对应档位. post-boundary 每轮 (但只随 Lv 档变, 不含 exp).

    主人 2026-05-31: 故意**不放 exp 精确数字**(旧版 `经验{exp}` 每次涨经验就漂移),
    只放 Lv + 档位标签, 让段在同档内跨轮字节稳定 (Lv 不变时 params 也不变).
    """
    if is_owner:
        return "【REL】tier=owner;rules=catty_relationship_skeleton"
    lv = int(affection_level)
    if lv <= 2:
        tier = "陌生"
    elif lv <= 5:
        tier = "渐熟"
    elif lv <= 7:
        tier = "亲近"
    else:
        tier = "挚友"
    return f"【REL】lv={lv};tier={tier};rules=catty_relationship_skeleton"
