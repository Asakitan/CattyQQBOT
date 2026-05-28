from __future__ import annotations

import logging
import random
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import DefaultDict

from .config import Config


PICTURE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 通用池(非 owner / 默认): 不含『主人』称呼,避免误称群友;用『杂鱼』『笨蛋』『你』
LEGS_REPLY_LINES: tuple[str, ...] = (
    "哈啊？！杂鱼又想看人家的腿啦喵！(脸红)…哼，这是人家自己的腿哦，就只准看这一张！ฅฅ",
    "哼～杂鱼真色！(尾巴炸毛)…喏，这是人家的腿啦，看完要夸可爱才行嗷呜～",
    "喵呜～又来了又来了！笨蛋就这么喜欢看人家自己的腿吗？(小声开心)",
    "哈？想看人家的脚？(耳朵立起来)…变态杂鱼！…不过这双脚是人家的哦，就破例给你看一眼ฅฅ",
    "嗷呜～你是不是又馋人家的腿了？(歪头)…拿去看吧！是人家自己的腿，才不是网图呢喵！",
    "哼！杂鱼天天都看不腻嘛～(偷偷得意)…今天份的人家的腿在这里喵～",
    "什么嘛笨蛋～又点这种东西！(脸红到耳朵根)…这是人家自己的腿，只看一张就够了嗷呜～",
    "喵～想看人家的腿就直说嘛！(转身偷瞄)…哼，发给你啦，可别盯着流口水哦笨蛋ฅฅ",
    "嗷呜～杂鱼眼睛都在发光了！(尾巴尖晃晃)…这是人家的腿哦，看完要夸猫猫可爱才行喵！",
    "哼，又来索要福利～(叉腰)…喏，是人家自己的腿啦，下次再敢点人家就…就还是会发的喵呜ฅฅ",
)

# owner 专属池:可以用『主人』『杂鱼主人』『笨蛋主人』
LEGS_REPLY_LINES_OWNER: tuple[str, ...] = (
    "哈啊？！杂鱼主人又想看人家的腿啦喵！(脸红)…哼，这是人家自己的腿哦，就只准看这一张！ฅฅ",
    "喵呜～又来了又来了！笨蛋主人就这么喜欢看人家自己的腿吗？(小声开心)",
    "嗷呜～主人是不是又馋人家的腿了？(歪头)…拿去看吧！是人家自己的腿，才不是网图呢喵！",
    "哼！杂鱼主人天天都看不腻嘛～(偷偷得意)…今天份的人家的腿在这里喵～",
    "什么嘛笨蛋主人～又点这种东西！(脸红到耳朵根)…这是人家自己的腿，只看一张就够了嗷呜～",
    "哼，主人又来索要福利～(叉腰)…喏，是人家自己的腿啦，下次再敢点人家就…就还是会发的喵呜ฅฅ",
)

# 成语 / 习惯短语黑名单：含 "脚"/"腿" 但绝不是要看猫猫的腿脚。
# 命中其中之一就直接放行，让主 AI 去理解上下文喵～
LEG_IDIOM_BLOCKLIST: tuple[str, ...] = (
    "手忙脚乱", "脚踏实地", "脚踏两只船", "拳打脚踢", "七手八脚",
    "蹑手蹑脚", "蹑脚蹑手", "指手画脚", "指手划脚", "毛手毛脚",
    "评头论足", "画蛇添足", "三脚猫", "三长两短", "情投意合",
    "落脚", "立足", "驻足", "插足", "失足", "捷足",
    "注脚", "脚本", "脚下", "脚边", "脚步", "脚跟", "脚印", "脚力",
    "脚踝", "脚气", "脚链", "脚色", "脚底", "脚面", "脚尖",
    "山脚", "墙脚", "踮脚", "跺脚", "跷脚", "翘脚", "撂脚", "歪脚",
    "拖后腿", "瘸腿", "断腿", "瘫腿", "腿伤", "腿部", "腿型", "假腿",
    "桌腿", "椅腿", "板凳腿", "裤腿", "牛腿",
    "插一脚", "踢一脚", "踹一脚", "崴脚", "扭脚", "扭到脚",
    "鸡脚", "鸭脚", "猪脚", "鹅脚", "凤爪", "鸡腿", "鸭腿", "猪腿", "羊腿", "火腿",
    "海绵宝宝", "马脚", "露马脚",
)

# 必须出现的"指代猫猫自己/你/我"代词，没有就视为指代不明 → 丢给主 AI。
# (?P<pron>...) 仅做语义说明，没有反向引用。
_LEG_PRONOUN = (
    r"(?:人家(?:的)?|你(?:的)?|妳(?:的)?|猫猫(?:的)?|猫娘(?:的)?|笨猫(?:的)?|"
    r"小猫(?:咪)?(?:的)?|catty(?:的)?|自己的|本喵(?:的)?)"
)

# 看类动词：明确表达"看/秀/露/给看/拍"等视觉请求
_LEG_VIEW_VERBS = r"(?:看(?:看|一?下|一?眼)?|瞧(?:瞧)?|瞅(?:瞅)?|瞄|秀|露|晒|发|拍|给|让我看|想看|要看|求看)"

# 啃/舔类亲密接触动词
_LEG_TOUCH_VERBS = r"(?:吃|啃|舔|嗦|嚼|含|吸|咬|亲|闻|嗅|摸|捏|揉)"

# 腿脚部位词（不含 "脚" 单字，单字脚太容易误伤，单独按"代词+脚"严格匹配）
_LEG_PARTS_SAFE = r"(?:大腿|小腿|玉腿|玉足|腿腿|脚丫|脚丫子|脚趾|脚脚|jio|jiojio)"
# 单字脚/腿要求紧贴代词使用
_LEG_PARTS_BARE = r"(?:腿|脚)"
# 明确“要福利”的说法可以走腿图福利；单字“福/看福”不放这里，交给主 AI 语义判断。
_LEG_WELFARE_PARTS = r"(?:福利(?!院)|福力|福图)"

LEG_TRIGGER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 1) （可选代词）+ 看类动词 +（可选代词）+ 单字腿/脚
    #    覆盖："看腿"/"看脚"/"猫猫看看腿"/"你看腿"/"看人家腿"/"看你脚"
    re.compile(
        rf"(?:{_LEG_PRONOUN}\s*)?{_LEG_VIEW_VERBS}\s*(?:一下|一?眼|一?张|一?个)?\s*"
        rf"(?:{_LEG_PRONOUN}\s*)?{_LEG_PARTS_BARE}",
        re.IGNORECASE,
    ),
    # 2) （可选代词）+ 看类动词 + 安全部位词（大腿/玉足/jio 等本身就明确）
    re.compile(
        rf"(?:{_LEG_PRONOUN}\s*)?{_LEG_VIEW_VERBS}\s*(?:一下|一?眼|一?张|一?个)?\s*"
        rf"(?:{_LEG_PRONOUN}\s*)?{_LEG_PARTS_SAFE}",
        re.IGNORECASE,
    ),
    # 3) （可选代词）+ 啃舔类动词 +（可选代词）+ 部位（含单字脚/腿）
    re.compile(
        rf"(?:{_LEG_PRONOUN}\s*)?{_LEG_TOUCH_VERBS}\s*(?:一?口|一下)?\s*"
        rf"(?:{_LEG_PRONOUN}\s*)?{_LEG_PARTS_BARE}",
        re.IGNORECASE,
    ),
    # 4) 啃舔类动词 + 安全部位词（大腿/脚丫/jio 等可不带代词）
    re.compile(
        rf"{_LEG_TOUCH_VERBS}\s*(?:一?口|一下)?\s*{_LEG_PRONOUN}?\s*{_LEG_PARTS_SAFE}",
        re.IGNORECASE,
    ),
    # 5) 极明确的萌叠词，单独出现也算
    re.compile(r"腿腿|脚脚|jiojio", re.IGNORECASE),
    # 6) 明确索要福利：来点福利 / 发点福利 / 整点福图 / 看看福利
    re.compile(
        rf"(?:来点|整点|发点|上点|给点|求|要|看看|看一眼|看一下)\s*"
        rf"(?:{_LEG_PRONOUN}\s*)?{_LEG_WELFARE_PARTS}",
        re.IGNORECASE,
    ),
)


logger = logging.getLogger(__name__)


def random_legs_reply(*, is_owner: bool = False, user_nickname: str = "", user_id: str = "") -> str:
    """随机抽一句腿图回复。v2 加 user_id 启用 A 算法去重."""
    try:
        from .cpu_engine.quick_reply import get_pool as _ce_get_pool
        from pathlib import Path as _Path

        replies_dir = _Path(__file__).resolve().parent / "data" / "cpu_engine" / "replies"
        pool = _ce_get_pool(replies_dir, "legs")
        if pool.size > 0:
            picked = pool.pick(
                is_owner=is_owner,
                user_id=str(user_id),
                render_vars={"user_nickname": user_nickname or ("主人" if is_owner else "杂鱼")},
            )
            if picked:
                return picked
    except Exception:  # noqa: BLE001
        pass

    if is_owner:
        return random.choice(LEGS_REPLY_LINES + LEGS_REPLY_LINES_OWNER)
    return random.choice(LEGS_REPLY_LINES)


def is_legs_trigger(text: str) -> bool:
    if not text:
        return False
    # 先把文本里命中的成语/常见短语挖掉，避免它们里面的"脚/腿"混淆判断
    cleaned = text
    for idiom in LEG_IDIOM_BLOCKLIST:
        if idiom in cleaned:
            cleaned = cleaned.replace(idiom, " ")
    if not cleaned.strip():
        return False
    for pattern in LEG_TRIGGER_PATTERNS:
        if pattern.search(cleaned):
            return True
    return False


class LegsPicker:
    def __init__(self, config: Config) -> None:
        self.enabled = bool(getattr(config, "catty_legs_enabled", True))
        raw_dir = str(getattr(config, "catty_legs_pictures_dir", "pictures") or "pictures")
        self.root = Path(raw_dir).expanduser()
        self._queues: DefaultDict[str, deque[Path]] = defaultdict(deque)
        self._all_paths: list[Path] = []
        if self.enabled:
            self._refresh_pool()

    def _has_picture_files(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        for item in path.iterdir():
            if item.is_file() and item.suffix.lower() in PICTURE_EXTENSIONS:
                return True
        return False

    def _use_bundled_root_if_needed(self) -> None:
        if self._has_picture_files(self.root):
            return
        bundle_root_value = getattr(sys, "_MEIPASS", "")
        if not bundle_root_value:
            return
        bundled_root = Path(str(bundle_root_value)) / self.root.name
        if self._has_picture_files(bundled_root):
            logger.info("Using bundled legs pictures directory: %s", bundled_root)
            self.root = bundled_root

    def _refresh_pool(self) -> list[Path]:
        self._use_bundled_root_if_needed()
        if not self.root.exists():
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Failed to create legs pictures dir %s: %s", self.root, exc)
        paths: list[Path] = []
        if self.root.is_dir():
            for path in sorted(self.root.iterdir()):
                if path.is_file() and path.suffix.lower() in PICTURE_EXTENSIONS:
                    paths.append(path)
        self._all_paths = paths
        return paths

    def has_pictures(self) -> bool:
        if not self.enabled:
            return False
        if self._all_paths:
            return True
        return bool(self._refresh_pool())

    def _refill_queue(self, scope: str) -> None:
        paths = self._refresh_pool()
        if not paths:
            self._queues[scope].clear()
            return
        shuffled = list(paths)
        random.shuffle(shuffled)
        self._queues[scope] = deque(shuffled)

    def next_picture(self, scope: str) -> Path | None:
        if not self.enabled:
            return None
        queue = self._queues.get(scope)
        if not queue:
            self._refill_queue(scope)
            queue = self._queues.get(scope)
            if not queue:
                return None
        while queue:
            candidate = queue.popleft()
            if candidate.is_file():
                return candidate
        self._refill_queue(scope)
        queue = self._queues.get(scope)
        if not queue:
            return None
        candidate = queue.popleft()
        return candidate if candidate.is_file() else None

    def reset(self) -> None:
        self._queues.clear()
        self._all_paths = []
