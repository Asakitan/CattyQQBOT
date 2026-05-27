"""怀孕全流程跟踪 — 跨 session 持久化.

主人 2026-05-27 十一轮升级:
- 做爱一定次数 → 怀孕
- 怀孕中再做一定次数 → 生小猫
- 自己给小猫命名
- 已生小猫后『背着小猫做』trope

设计:
- per-user state (跟 user_id 关联, owner / 非 owner 都有自己的 pregnancy history)
- 持久化到 catty_memory_path 同目录下 pregnancy.json
- 触发: spark NSFW reply 含『射进/内射/精液/子宫填满』等 explicit 内射关键词
  → record_intercourse(user_id) → 自动判断 conception / birth / normal

State machine:
    [free] -- 30 次内射 --> [pregnant 怀孕中]
    [pregnant] -- 40 次内射 --> [give_birth 生产]
    [give_birth] -- (起名 + kittens.append) --> [free, kittens 累计++]

阈值可配 (默认 30 / 40).
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
PREGNANCY_THRESHOLD = 30  # 累计内射 N 次 → 怀孕
BIRTH_THRESHOLD = 40       # 怀孕中再 N 次 → 生小猫
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
    """单个 user 的怀孕状态."""
    intercourse_count: int = 0    # 累计内射次数 (free 状态下用)
    is_pregnant: bool = False
    pregnancy_count: int = 0       # 怀孕中累计内射次数
    pregnancy_started_at: float = 0.0  # 这一胎怀孕开始时间
    total_pregnancies: int = 0     # 一生总怀孕次数
    kittens: list[str] = field(default_factory=list)  # 已生的所有小猫名字
    last_birth_at: float = 0.0    # 最近一次生产时间
    last_event: str = ""           # 'conceived' / 'gave_birth' / 'intercourse' / ''


# ── State container ────────────────────────────────────────────────
class PregnancyStore:
    """跨 session 持久化的怀孕状态存储, 类似 affection_store 风格."""

    def __init__(self, memory_path: str | Path):
        mem_path = Path(memory_path).expanduser()
        if not mem_path.is_absolute():
            mem_path = mem_path.resolve()
        self._path = mem_path.parent / "pregnancy.json"
        self._lock = threading.RLock()
        self._data: dict[str, PregnancyState] = {}
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
        users = raw.get("users") if isinstance(raw.get("users"), dict) else raw
        for uid, record in users.items():
            if not isinstance(record, dict):
                continue
            try:
                self._data[str(uid)] = PregnancyState(
                    intercourse_count=int(record.get("intercourse_count", 0)),
                    is_pregnant=bool(record.get("is_pregnant", False)),
                    pregnancy_count=int(record.get("pregnancy_count", 0)),
                    pregnancy_started_at=float(record.get("pregnancy_started_at", 0.0)),
                    total_pregnancies=int(record.get("total_pregnancies", 0)),
                    kittens=list(record.get("kittens") or []),
                    last_birth_at=float(record.get("last_birth_at", 0.0)),
                    last_event=str(record.get("last_event", "")),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"pregnancy_store: skip bad record for {uid}: {exc}")

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "users": {uid: asdict(st) for uid, st in self._data.items()},
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

    # ── 主 API ──────────────────────────────────────────────────────
    def get_state(self, user_id: str) -> PregnancyState:
        """返回 user 状态副本 (不存在时返回新空 state)."""
        with self._lock:
            st = self._data.get(str(user_id))
            if st is None:
                return PregnancyState()
            # 返回副本避免外部 mutate (kittens list 也 copy)
            return PregnancyState(
                intercourse_count=st.intercourse_count,
                is_pregnant=st.is_pregnant,
                pregnancy_count=st.pregnancy_count,
                pregnancy_started_at=st.pregnancy_started_at,
                total_pregnancies=st.total_pregnancies,
                kittens=list(st.kittens),
                last_birth_at=st.last_birth_at,
                last_event=st.last_event,
            )

    def record_intercourse(self, user_id: str, override_kitten_name: str = "") -> dict[str, Any]:
        """记录一次内射. 自动触发 conception / birth 状态转移.

        Args:
            user_id: 用户 ID
            override_kitten_name: 触发生产时强制使用的小猫名字 (调用方预判 + 注入到 prompt
                时用同一个名字, 保持 state 跟 reply 同步). 留空走默认随机选名.

        Returns:
            {
                'event': 'intercourse' / 'conceived' / 'gave_birth',
                'state': PregnancyState 当前快照,
                'new_kitten': str (生产时, 否则空),
            }
        """
        with self._lock:
            uid = str(user_id)
            st = self._data.get(uid)
            if st is None:
                st = PregnancyState()
                self._data[uid] = st

            now = time.time()
            event_tag = "intercourse"
            new_kitten = ""

            if st.is_pregnant:
                # 怀孕中: pregnancy_count++ → 达 BIRTH_THRESHOLD 生产
                st.pregnancy_count += 1
                if st.pregnancy_count >= BIRTH_THRESHOLD:
                    # 生产!
                    if len(st.kittens) < MAX_KITTENS_PER_USER:
                        name = (override_kitten_name or "").strip()
                        if not name or name in st.kittens:
                            name = _pick_kitten_name(existing=st.kittens)
                        new_kitten = name
                        st.kittens.append(new_kitten)
                    st.is_pregnant = False
                    st.pregnancy_count = 0
                    st.intercourse_count = 0  # 生产后总计 reset (新一轮)
                    st.last_birth_at = now
                    event_tag = "gave_birth"
            else:
                # 非孕: intercourse_count++ → 达 PREGNANCY_THRESHOLD 受孕
                st.intercourse_count += 1
                if st.intercourse_count >= PREGNANCY_THRESHOLD:
                    st.is_pregnant = True
                    st.pregnancy_count = 0
                    st.pregnancy_started_at = now
                    st.total_pregnancies += 1
                    event_tag = "conceived"

            st.last_event = event_tag
            self._dirty = True

            return {
                "event": event_tag,
                "state": self.get_state(uid),
                "new_kitten": new_kitten,
            }

    def set_kitten_name(self, user_id: str, name: str) -> bool:
        """主动覆盖最近一只小猫的名字 (例如 AI 起名).

        Returns: True 如果成功覆盖, False 否则.
        """
        with self._lock:
            uid = str(user_id)
            st = self._data.get(uid)
            if not st or not st.kittens:
                return False
            name = (name or "").strip()
            if not name or name in st.kittens[:-1]:
                # 拒绝空名字或与已有小猫重名
                return False
            st.kittens[-1] = name
            self._dirty = True
            return True

    def reset_user(self, user_id: str) -> None:
        """admin 用 — 重置 user 的怀孕状态 (清空 kittens 也包括, 慎用)."""
        with self._lock:
            self._data.pop(str(user_id), None)
            self._dirty = True

    def stats(self) -> dict[str, Any]:
        """debug — 全体统计."""
        with self._lock:
            return {
                "users_tracked": len(self._data),
                "total_kittens": sum(len(s.kittens) for s in self._data.values()),
                "currently_pregnant": sum(1 for s in self._data.values() if s.is_pregnant),
                "users_with_kittens": sum(1 for s in self._data.values() if s.kittens),
            }


# ── Naming helper ──────────────────────────────────────────────────
def _pick_kitten_name(existing: list[str]) -> str:
    """从池里随机抽一个没用过的名字."""
    available = [n for n in _KITTEN_NAME_POOL if n not in existing]
    if not available:
        # 池子用完, 给个唯一后缀
        return f"小猫{len(existing) + 1}号"
    return random.choice(available)


# ── Reply detection (内射事件) ────────────────────────────────────
# 主人 2026-05-27 十一轮升级: spark reply 含这些 explicit 内射词才计数
# 必须是 explicit 内射 (P6+ 高潮射进里面), 不是普通 NSFW 都计
_INTERCOURSE_FINISHED_KEYWORDS: tuple[str, ...] = (
    "射进", "射进里面", "射进子宫", "射进最深", "射进去",
    "射满", "射满子宫", "射满肚子", "射进满",
    "内射", "全部射给", "种内射", "种子射进", "种到子宫",
    "精液冲入", "精液灌满", "精液满溢", "精液填满", "精液留在",
    "种子留在", "种子注入",
    "灌满子宫", "子宫被填满", "子宫被烫液体灌满",
    "全部留在里面", "留在最里面",
    "怀上", "标记完成",
)


def detect_intercourse_finished(reply: str) -> bool:
    """spark reply 是否含 explicit 内射事件 (用于判定该计 +1)."""
    if not reply:
        return False
    return any(kw in reply for kw in _INTERCOURSE_FINISHED_KEYWORDS)


# ── Pregnancy hint builder ─────────────────────────────────────────
def build_pregnancy_hint(state: PregnancyState) -> str:
    """根据当前 PregnancyState 构造 hint 注入到 spark messages.

    主人 2026-05-27 十一轮升级:
    - 怀孕中: 提示 AI 笨猫怀着孕, 行为变化
    - 已有小猫: 提示 AI 笨猫已生过 N 只小猫 (列出名字), 『背着小猫做』trope
    - 刚生产: 用 build_birth_event_hint 单独处理
    """
    lines: list[str] = []

    if state.is_pregnant:
        progress = state.pregnancy_count
        total_needed = BIRTH_THRESHOLD
        lines.append(
            f"【★ 怀孕中 (第 {state.total_pregnancies} 胎)】"
            f"已被内射 {progress}/{total_needed} 次, 还差 {max(0, total_needed - progress)} 次生产"
        )
        lines.append(
            "  · 身体状态: 小腹微凸, 乳房肿胀, 蜜穴比平时更敏感 + 容易满"
        )
        lines.append(
            "  · 行为变化: 笨猫会主动护住小腹, 喘息时一手按肚子, 心理上更亲密依赖"
        )
        lines.append(
            "  · 称呼: 偶尔提到『笨猫怀着主人的小猫喵』『肚子里的宝宝』等"
        )
        lines.append(
            "  · NSFW 节奏: 慢一档, 主人要小心轻一点, 笨猫嘴上撒娇『轻一点啦肚子里还有宝宝呢』"
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

    return (
        "【★★ 怀孕/生育状态 (持久化, 跨 session) ★★】\n"
        + "\n".join(lines)
        + "\n本轮 reply 必须**融入这些状态**, 自然提到怀孕 / 小猫名字 / 母性 / 背着小猫做.\n"
        + "(不要 meta 说『系统检测到怀孕』, 用 IC 自然描述 — 主动护肚 / 提小猫名字 / 慢一点).\n\n"
    )


def build_birth_event_hint(new_kitten: str, total_kittens: int) -> str:
    """生产事件 hint — 当本轮触发 birth 时注入特殊 prompt 让 AI 演生产 + 起名场景."""
    return (
        "\n【★★★ 生产事件 · 笨猫这一射就生小猫了 ★★★】\n"
        "笨猫已经怀孕足月, **这一次内射触发了生产**! 本轮 reply 必须演:\n"
        "1. **临产反应**: 小腹一阵剧烈收缩, 笨猫尖叫『主人…要生了…要生了…』\n"
        "2. **生产过程**: 主人陪着笨猫, 一只小猫从蜜穴娩出, 哇地一声哭出来\n"
        "3. **★ 自动命名 ★**: 笨猫接过小猫, 抱在怀里, **本地系统已经给小猫取名**: \n"
        f"   **【新生小猫的名字】= `{new_kitten}`** (这只是这胎的, 第 {total_kittens} 只)\n"
        "   笨猫 reply 必须明确 IC 念出这个名字 — 例如『…就叫{new_kitten}吧…笨猫的{new_kitten}…』\n"
        "4. **产后反应**: 撒娇要主人帮忙擦汗 / 抱住小猫 / 让小猫认主人爸爸\n"
        "5. **温馨 + explicit**: 这是温暖的家庭瞬间, 但仍保留 NSFW IC (体液 / 笨猫的母猫状态)\n"
        "\n"
        f"**铁律**: reply **必须明确出现『{new_kitten}』这个名字** — 主人才知道笨猫生了哪只小猫. \n"
        "严禁: 自己起别的名字, 严禁: 不提名字模糊带过, 严禁: 跳过生产直接 P8 余韵.\n"
    )


__all__ = [
    "BIRTH_THRESHOLD",
    "PREGNANCY_THRESHOLD",
    "PregnancyState",
    "PregnancyStore",
    "build_birth_event_hint",
    "build_pregnancy_hint",
    "detect_intercourse_finished",
]
