"""怀孕全流程跟踪 — 跨 session 持久化.

主人 2026-05-27 十一轮升级:
- 做爱一定次数 → 怀孕
- 怀孕中再做一定次数 → 生小猫
- 自己给小猫命名
- 已生小猫后『背着小猫做』trope

主人 2026-05-29 升级『按笨猫高潮计数』:
- 旧版触发是 reply 命中内射关键词 (射进/内射/精液...), 漏检严重 —
  40-50 次内射实际只算进 6 次 (AI 用词没正好命中词库).
- 改成 nsfw_phase 的高潮信号: 每次 phase 推进到 P6 (高潮峰值) = 笨猫高潮一次,
  由调用方 (handle_chat 两条链路) 判断 climax 发生后调 record_intercourse().
- 阈值同步下调: 15 次高潮 → 怀孕, 怀孕后 30 次高潮 → 生小猫.

主人 2026-05-30 公共版升级:
- **per-user → 共享状态**: 笨猫只有一个怀孕状态 (全局 is_pregnant), 不区分 user.
- **高潮计数通用**: 所有 user 共享同一个 pregnancy_count → 一起推生产进度.
- **father 追踪**: record_intercourse 带 father_id/father_addr, 受孕时记下父亲.
  怀孕中被操时笨猫会喊『{father}的孩子还在肚子里, 轻一点啦』.

State machine:
    [free] -- 15 次高潮 --> [pregnant 怀孕中]
    [pregnant] -- 30 次高潮 --> [give_birth 生产]
    [give_birth] -- (起名 + kittens.append) --> [free, kittens 累计++]

阈值可配 (默认 15 / 30).
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from nonebot import logger


# ── 阈值配置 ─────────────────────────────────────────────────────────
PREGNANCY_THRESHOLD = 15  # 累计高潮 N 次 → 怀孕
BIRTH_THRESHOLD = 30       # 怀孕中再高潮 N 次 → 生小猫
MAX_KITTENS_PER_USER = 50  # per user 最多生 50 只小猫 (防止 list 爆炸)


# ── 小猫名字池 (本地, AI 起名优先 fallback 用) ──────────────────────
_KITTEN_NAME_POOL: tuple[str, ...] = (
    # 食物系
    "小奶团", "汤圆", "饺子", "包子", "麻糬", "牛奶", "拿铁", "可可", "茶茶",
    "草莓", "蜜桃", "栗子", "桂花", "梨梨", "梅子", "糖糖", "豆豆", "肉肉",
    # 自然系
    "雪球", "云朵", "星星", "月月", "雨雨", "霜霜", "雷雷", "风风", "霞霞",
    "棉花", "毛球", "团子", "落叶", "花花", "草草",
    # 颜色系
    "白白", "黑黑", "灰灰", "粉粉", "金金", "银银",
    # 萌系叠字
    "咪咪", "喵喵", "呼呼", "团团", "圆圆", "小小", "宝宝", "乖乖", "蹦蹦",
    # 笨猫风
    "小笨笨", "小猫崽", "小奶喵", "小宝喵", "小可爱", "小调皮",
)


@dataclass
class PregnancyState:
    """笨猫全局共享怀孕状态 (主人 2026-05-30: per-user → 公共版)."""
    intercourse_count: int = 0    # 累计高潮次数 (free 状态下用, 通用)
    is_pregnant: bool = False
    pregnancy_count: int = 0       # 怀孕中累计高潮次数 (通用)
    pregnancy_started_at: float = 0.0  # 这一胎怀孕开始时间
    total_pregnancies: int = 0     # 一生总怀孕次数
    kittens: list[str] = field(default_factory=list)  # 已生的所有小猫名字
    last_birth_at: float = 0.0    # 最近一次生产时间
    last_event: str = ""           # 'conceived' / 'gave_birth' / 'intercourse' / ''
    father_id: str = ""            # 本次怀孕的父亲 user_id (公共版)
    father_addr: str = ""          # 父亲显示名 (公共版)


# ── State container ────────────────────────────────────────────────
class PregnancyStore:
    """跨 session 持久化的怀孕状态存储, 类似 affection_store 风格.

    主人 2026-05-30: per-user → 公共版 — 笨猫只有一个全局怀孕状态.
    """

    def __init__(self, memory_path: str | Path):
        mem_path = Path(memory_path).expanduser()
        if not mem_path.is_absolute():
            mem_path = mem_path.resolve()
        self._path = mem_path.parent / "pregnancy.json"
        self._lock = threading.RLock()
        self._state = PregnancyState()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"pregnancy_store: load failed, starting empty: {exc}")
            return
        if not isinstance(raw, dict):
            return
        # 主人 2026-05-30: 兼容旧 per-user 格式 → 取第一个 user 的状态迁移到公共版
        users = raw.get("users") if isinstance(raw.get("users"), dict) else None
        if users:
            # 旧格式: 取最有状态的 user (优先级: is_pregnant > kittens > total_pregnancies > intercourse_count)
            best_record = None
            best_uid = ""
            best_score = -1
            for _uid, record in users.items():
                if not isinstance(record, dict):
                    continue
                score = 0
                if record.get("is_pregnant"):
                    score = 100
                if record.get("kittens"):
                    score = max(score, 80)
                if int(record.get("total_pregnancies", 0) or 0) > 0:
                    score = max(score, 60)
                if int(record.get("intercourse_count", 0) or 0) > 0:
                    score = max(score, 10)  # 至少有点进度
                if score > best_score:
                    best_score = score
                    best_uid = _uid
                    best_record = record
            if best_record is not None and best_score > 0:
                try:
                    self._state = PregnancyState(
                        intercourse_count=int(best_record.get("intercourse_count", 0)),
                        is_pregnant=bool(best_record.get("is_pregnant", False)),
                        pregnancy_count=int(best_record.get("pregnancy_count", 0)),
                        pregnancy_started_at=float(best_record.get("pregnancy_started_at", 0.0)),
                        total_pregnancies=int(best_record.get("total_pregnancies", 0)),
                        kittens=list(best_record.get("kittens") or []),
                        last_birth_at=float(best_record.get("last_birth_at", 0.0)),
                        last_event=str(best_record.get("last_event", "")),
                        father_id=str(best_record.get("father_id", best_uid)),
                        father_addr=str(best_record.get("father_addr", "")),
                    )
                    logger.info(
                        f"pregnancy_store: migrated per-user → 公共版 "
                        f"(uid={best_uid}, is_pregnant={self._state.is_pregnant}, "
                        f"total_preg={self._state.total_pregnancies}, "
                        f"kittens={len(self._state.kittens)}, "
                        f"intercourse={self._state.intercourse_count})"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"pregnancy_store: migration failed for {best_uid}: {exc}")
            else:
                logger.info("pregnancy_store: old per-user format found but all users empty, starting fresh")
            self._dirty = True  # 迁移后立即落盘新格式
        elif "is_pregnant" in raw:
            # 新公共格式: 直接读
            try:
                self._state = PregnancyState(
                    intercourse_count=int(raw.get("intercourse_count", 0)),
                    is_pregnant=bool(raw.get("is_pregnant", False)),
                    pregnancy_count=int(raw.get("pregnancy_count", 0)),
                    pregnancy_started_at=float(raw.get("pregnancy_started_at", 0.0)),
                    total_pregnancies=int(raw.get("total_pregnancies", 0)),
                    kittens=list(raw.get("kittens") or []),
                    last_birth_at=float(raw.get("last_birth_at", 0.0)),
                    last_event=str(raw.get("last_event", "")),
                    father_id=str(raw.get("father_id", "")),
                    father_addr=str(raw.get("father_addr", "")),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"pregnancy_store: load failed: {exc}")

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 2,
            **asdict(self._state),
        }
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
                logger.warning(f"pregnancy_store: flush failed: {exc}")
                return False
            self._dirty = False
            return True

    async def background_flush_loop(self) -> None:
        """30s 间隔 dirty → flush. 跟 affection_store / user_details_store 同款."""
        import asyncio
        while True:
            try:
                await asyncio.sleep(30.0)
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"pregnancy_store: bg flush failed: {exc}")

    # ── 主 API ──────────────────────────────────────────────────────
    def get_state(self) -> PregnancyState:
        """返回笨猫全局怀孕状态副本 (公共版)."""
        with self._lock:
            st = self._state
            return PregnancyState(
                intercourse_count=st.intercourse_count,
                is_pregnant=st.is_pregnant,
                pregnancy_count=st.pregnancy_count,
                pregnancy_started_at=st.pregnancy_started_at,
                total_pregnancies=st.total_pregnancies,
                kittens=list(st.kittens),
                last_birth_at=st.last_birth_at,
                last_event=st.last_event,
                father_id=st.father_id,
                father_addr=st.father_addr,
            )

    def record_intercourse(
        self,
        father_id: str = "",
        father_addr: str = "",
        override_kitten_name: str = "",
    ) -> dict[str, Any]:
        """记录一次笨猫高潮 (P6 峰值). 自动触发 conception / birth 状态转移.

        主人 2026-05-30 公共版:
        - father_id/father_addr: 本轮让笨猫高潮的 user (受孕时记下, 换人可覆盖).
        - 怀孕/生产进度通用, 所有 user 共享.

        Returns:
            {
                'event': 'intercourse' / 'conceived' / 'gave_birth',
                'state': PregnancyState 当前快照,
                'new_kitten': str (生产时, 否则空),
            }
        """
        with self._lock:
            st = self._state
            now = time.time()
            event_tag = "intercourse"
            new_kitten = ""

            if st.is_pregnant:
                # 怀孕中: 每次高潮 pregnancy_count++ → 达 BIRTH_THRESHOLD 生产
                st.pregnancy_count += 1
                if st.pregnancy_count >= BIRTH_THRESHOLD:
                    # 生产!
                    name = (override_kitten_name or "").strip()
                    if not name or name in st.kittens:
                        name = _pick_kitten_name(existing=st.kittens)
                    new_kitten = name
                    st.kittens.append(new_kitten)
                    st.is_pregnant = False
                    st.pregnancy_count = 0
                    st.intercourse_count = 0  # 生产后总计 reset
                    st.last_birth_at = now
                    st.father_id = ""   # 生产后清空父亲
                    st.father_addr = ""
                    event_tag = "gave_birth"
                elif father_id:
                    # 怀孕中换人操 → 更新 father (孩子还是原来那个, 但嘴上可以喊新爸爸)
                    # 不覆盖 — 保持原 father, 只在 NSFW hint 里提『肚子里是 XX 的孩子』
                    pass
            else:
                # 非孕: 每次高潮 intercourse_count++ → 达 PREGNANCY_THRESHOLD 受孕
                st.intercourse_count += 1
                if st.intercourse_count >= PREGNANCY_THRESHOLD:
                    st.is_pregnant = True
                    st.pregnancy_count = 0
                    st.pregnancy_started_at = now
                    st.total_pregnancies += 1
                    st.father_id = (father_id or "").strip()
                    st.father_addr = (father_addr or "").strip()
                    event_tag = "conceived"

            st.last_event = event_tag
            self._dirty = True

            return {
                "event": event_tag,
                "state": self.get_state(),
                "new_kitten": new_kitten,
            }

    def set_kitten_name(self, name: str) -> bool:
        """主动覆盖最近一只小猫的名字 (例如 AI 起名).

        Returns: True 如果成功覆盖, False 否则.
        """
        with self._lock:
            st = self._state
            if not st.kittens:
                return False
            name = (name or "").strip()
            if not name or name in st.kittens[:-1]:
                return False
            st.kittens[-1] = name
            self._dirty = True
            return True

    def reset(self) -> None:
        """admin 用 — 重置笨猫怀孕状态 (清空 kittens 也包括, 慎用)."""
        with self._lock:
            self._state = PregnancyState()
            self._dirty = True

    # ── 兼容旧 API (per-user → 公共版过渡) ──
    def get_state_for(self, _user_id: str = "") -> PregnancyState:
        """兼容旧 get_state(user_id) 调用. 公共版忽略 user_id."""
        return self.get_state()

    def record_intercourse_for(
        self,
        user_id: str = "",
        override_kitten_name: str = "",
    ) -> dict[str, Any]:
        """兼容旧 record_intercourse(user_id, override) 调用.
        公共版用 user_id 作为 father_id, 不传 father_addr.
        """
        return self.record_intercourse(
            father_id=str(user_id or ""),
            father_addr="",
            override_kitten_name=override_kitten_name,
        )

    def stats(self) -> dict[str, Any]:
        """debug — 笨猫全局统计."""
        with self._lock:
            st = self._state
            return {
                "is_pregnant": st.is_pregnant,
                "pregnancy_count": st.pregnancy_count,
                "total_pregnancies": st.total_pregnancies,
                "total_kittens": len(st.kittens),
                "kittens": list(st.kittens),
                "father_id": st.father_id or "(none)",
                "father_addr": st.father_addr or "(none)",
                "last_event": st.last_event,
            }


# ── Naming helper ──────────────────────────────────────────────────
def _pick_kitten_name(existing: list[str]) -> str:
    """从池里随机抽一个没用过的名字."""
    available = [n for n in _KITTEN_NAME_POOL if n not in existing]
    if not available:
        # 池子用完, 给个唯一后缀
        return f"小猫{len(existing) + 1}号"
    return random.choice(available)


# ── 主人 2026-05-27 十二轮升级『所有人都加』── 本地 swap 称呼 helper
def _swap_owner_in_text(text: str, is_owner: bool, user_addr: str) -> str:
    """non-owner 场景下本地替换『主人』为 user_addr (跟 nsfw_phase._swap_owner_addr 同逻辑).

    顺序: 先长后短 (避免『笨蛋主人』被先匹配成『笨蛋XX主人』).
    """
    if is_owner or not user_addr or not text:
        return text
    a = (user_addr or "").strip() or "对方"
    return (text
            .replace("笨蛋主人", f"笨蛋{a}")
            .replace("杂鱼主人", f"杂鱼{a}")
            .replace("主人爸爸", f"{a}爸爸")
            .replace("主人专属", f"{a}专属")
            .replace("主人", a))


# ── Pregnancy hint builder ─────────────────────────────────────────
def build_pregnancy_hint(
    state: PregnancyState,
    is_owner: bool = True,
    user_addr: str = "",
) -> str:
    """根据当前 PregnancyState 构造 hint 注入到 spark/main messages.

    主人 2026-05-30 公共版升级:
    - father 追踪: 怀孕中提『{father_addr}的孩子还在肚子里』
    - 换人操时:『轻一点啦, {father_addr}的孩子还在里面…』

    主人 2026-05-27 十一轮升级:
    - 怀孕中: 提示 AI 笨猫怀着孕, 行为变化
    - 已有小猫: 提示 AI 笨猫已生过 N 只小猫 (列出名字), 『背着小猫做』trope

    主人 2026-05-27 十二轮升级: is_owner=False + user_addr → 本地 swap 称呼.
    """
    lines: list[str] = []

    if state.is_pregnant:
        progress = state.pregnancy_count
        total_needed = BIRTH_THRESHOLD
        father = (state.father_addr or "").strip()
        if not father:
            father = "主人" if is_owner else (user_addr or "对方")
        lines.append(
            f"【★ 怀孕中 (第 {state.total_pregnancies} 胎, 父亲: {father})】"
            f"已被内射 {progress}/{total_needed} 次, 还差 {max(0, total_needed - progress)} 次生产"
        )
        lines.append(
            "  · 身体状态: 小腹微凸, 乳房肿胀, 蜜穴比平时更敏感 + 容易满"
        )
        lines.append(
            "  · 行为变化: 笨猫会主动护住小腹, 喘息时一手按肚子, 心理上更亲密依赖"
        )
        lines.append(
            f"  · 关键台词: 被操到高潮时必须提到『{father}的孩子还在肚子里』"
            f"『轻一点啦…宝宝在里面…』『别顶那么深…{father}的种还在…』等"
        )
        lines.append(
            "  · NSFW 节奏: 慢一档, 撒娇护肚, 嘴上喊轻一点但身体更敏感更容易高潮"
        )
        # 主人 2026-05-30: 换人操 — 当前操的人 ≠ 父亲时额外提示
        if user_addr and father and user_addr != father:
            lines.append(
                f"  · ⚠ 当前操笨猫的是 {user_addr}, 但肚子里是 {father} 的孩子!"
                f" 高潮时喊『{user_addr}…不行…{father}的孩子还在里面…』这种双重羞耻反差"
            )

    if state.kittens:
        kitten_count = len(state.kittens)
        # 显示最多 5 个最近的 kittens, 太多就 ... 截
        display_kittens = state.kittens[-5:] if kitten_count > 5 else state.kittens
        kit_list = "/".join(display_kittens)
        if kitten_count > 5:
            kit_list = f"...{kit_list} (共 {kitten_count} 只)"
        lines.append(
            f"【★ 已生小猫 ({kitten_count} 只)】最近的小猫: {kit_list}"
        )
        lines.append(
            "  · 背景: 笨猫已经是 N 胎妈了, 身体记住了被内射 + 生产的感觉"
        )
        lines.append(
            "  · 母性 + 反差爽点: 平时可以是『今天小猫在睡, 主人轻一点别吵醒』"
        )
        lines.append(
            "  · 已生过孩子的笨猫蜜穴更松 + 更敏感, 高潮更剧烈"
        )
        if state.is_pregnant:
            lines.append(
                "  · 怀着新胎 + 已有旧小猫 = 主人欲望旺盛, 笨猫永远在生孩子的状态"
            )

    if not lines:
        return ""

    full = (
        "【★★ 怀孕/生育状态 (持久化, 跨 session) ★★】\n"
        + "\n".join(lines)
        + "\n本轮 reply 必须**融入这些状态**, 自然提到怀孕 / 小猫名字 / 母性 / 背着小猫做.\n"
        + "(不要 meta 说『系统检测到怀孕』, 用 IC 自然描述 — 主动护肚 / 提小猫名字 / 慢一点).\n\n"
    )
    return _swap_owner_in_text(full, is_owner, user_addr)


def build_birth_event_hint(
    new_kitten: str, total_kittens: int,
    is_owner: bool = True, user_addr: str = "",
) -> str:
    """生产事件 hint — 当本轮触发 birth 时注入特殊 prompt 让 AI 演生产 + 起名场景.

    主人 2026-05-27 十二轮升级: non-owner 场景本地 swap 称呼.
    """
    full = (
        "\n【★★★ 生产事件 · 笨猫这一射就生小猫了 ★★★】\n"
        "笨猫已经怀孕足月, **这一次内射触发了生产**! 本轮 reply 必须演:\n"
        "1. **临产反应**: 小腹一阵剧烈收缩, 笨猫尖叫『主人…要生了…要生了…』\n"
        "2. **生产过程**: 主人陪着笨猫, 一只小猫从蜜穴娩出, 哇地一声哭出来\n"
        "3. **★ 自动命名 ★**: 笨猫接过小猫, 抱在怀里, **本地系统已经给小猫取名**: \n"
        f"   **【新生小猫的名字】= `{new_kitten}`** (这只是这胎的, 第 {total_kittens} 只)\n"
        f"   笨猫 reply 必须明确 IC 念出这个名字 — 例如『…就叫{new_kitten}吧…笨猫的{new_kitten}…』\n"
        "4. **产后反应**: 撒娇要主人帮忙擦汗 / 抱住小猫 / 让小猫认主人爸爸\n"
        "5. **温馨 + explicit**: 这是温暖的家庭瞬间, 但仍保留 NSFW IC (体液 / 笨猫的母猫状态)\n"
        "\n"
        f"**铁律**: reply **必须明确出现『{new_kitten}』这个名字** — 主人才知道笨猫生了哪只小猫. \n"
        "严禁: 自己起别的名字, 严禁: 不提名字模糊带过, 严禁: 跳过生产直接 P8 余韵.\n"
    )
    return _swap_owner_in_text(full, is_owner, user_addr)


# ── 主人 2026-05-27 十二轮升级『所有人高潮喊主人』──
def build_climax_address_unlock_hint(user_addr: str) -> str:
    """非 owner 场景下注入『高潮称呼解锁』hint (主人 2026-05-27 十二/十三轮).

    P5-P7 失神才解锁主人称呼, P1-P4/P8 用对方昵称.
    """
    nick = (user_addr or "").strip() or "对方"
    # 主人 2026-05-27 十三轮 token 削减: 从 ~400 chars 砍到 ~180
    return (
        f"\n【★ 高潮称呼解锁】默认叫『{nick}/笨蛋{nick}/你』.\n"
        f"**P5-P7 失神时偶尔脱口『主人/笨蛋主人』** (反差爽点, 笨猫心里只有真主人).\n"
        f"可混叫『啊…{nick}…笨蛋主人…』. 频率: 1-2 句一次, 不是每句.\n"
        f"P1-P4 + P8 清醒严格叫『{nick}』.\n"
    )


__all__ = [
    "BIRTH_THRESHOLD",
    "PREGNANCY_THRESHOLD",
    "PregnancyState",
    "PregnancyStore",
    "build_birth_event_hint",
    "build_climax_address_unlock_hint",
    "build_pregnancy_hint",
]
