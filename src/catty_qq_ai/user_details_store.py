"""跨对话用户细节记忆 — 抓 keyword pattern 提取『对方喜欢 / 工作 / 宠物 / 近事』.

跟现有层的区别:
- memory.py: 完整对话语料 corpus, AI 摘要后用
- catty_rag: 向量召回相关历史片段
- user_vibe: per-user 调性画像 (techie/playful/...)
- user_details_store (本层): **结构化** key-value 细节 (favorite_foods / job /
  pet / recent_event / mentioned_topics), keyword pattern 自动提取, 持久化到 JSON.

为什么需要:
笨猫现在没有"记得对方爱吃 X / 养了 Y / 工作是 Z"的结构化能力. memory 是模糊摘要,
catty_rag 要 query 才召回. 这个 store 是『可枚举的对方画像细节』 — 注入 prompt
时『对方喜欢: 烤鱼/咖啡; 工作: 程序员; 宠物: 一只白猫 喵球』式直接展示, 让
笨猫主动 callback『主人之前不是说喜欢烤鱼嘛?』.

设计:
- per-user JSON 文件 (跟 user_vibe_store 同 backing)
- LRU 500 用户, 后台 30s flush
- 关键词 pattern 自动提取 (轻量, 不调 LLM)
- 每个细节带 confidence + last_mentioned_ts, 旧的自动淡出
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


_MAX_TOTAL_USERS = 500
_MAX_DETAILS_PER_FIELD = 5  # favorite_foods 最多记 5 个
_DETAIL_TTL_SECONDS = 30 * 24 * 3600  # 30 天过期


# ── Pattern 库 ─────────────────────────────────────────────────────────
# 每个 pattern: (regex, field_name)
# 抓到的 group(1) 是细节内容
_DETAIL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ── 食物偏好 ────────────────────────────────────────
    # 缩窄到 2-8 字, 排除时间副词 (每天/周末/晚上...) 防贪婪吃到 "红烧肉每天都吃"
    (re.compile(r"(?:我|人家|本人)?(?:超|很|特别|最)?(?:爱|喜欢|喜爱)吃([^,。!?\n每天周末晚上中午早上偶尔有时每次]{2,8})"),
     "favorite_foods"),
    (re.compile(r"(?:我|人家|本人)?(?:不|讨厌|超讨厌|最讨厌)吃([^,。!?\n]{2,8})"),
     "disliked_foods"),

    # ── 工作 ───────────────────────────────────────────
    # 加 (可选定语 15 字) 让 "我是做后端开发的程序员" 命中
    # 职业表跟 _JOB_TABLE 略不同 — 这里要的是 anchor pattern, 不需要全; 词表用作 rule layer 兜底
    (re.compile(r"(?:我|人家)(?:是|做|当)(?:个|名)?(?:[^,。!?\n]{0,15})?(程序员|工程师|设计师|学生|医生|老师|律师|司机|厨师|护士|经理|销售|运营|产品|测试|前端|后端|全栈|架构师?|主播|UP\s?主|博主|自媒体|外卖员|快递员|代驾|心理咨询师|瑜伽教练|私教|健身教练|插画师|原画师|动画师|声优|歌手|翻译|策划|项目经理|网红|带货主播|宝妈|全职妈妈|自由职业者?|代购|店主|实习生|博士|硕士|研究生|博士生|硕士生)"),
     "job"),
    # workplace pattern A: 主语可选 + 在/来到 + ORG (2-10 字) + 上班/工作 / 实习
    (re.compile(r"(?:^|[,。!?\n ])(?:在|来到|去)([^,。!?\n]{2,10})(?:上班|工作|实习|做事|干活)"),
     "workplace"),
    # workplace pattern B: 我/人家 + 在/的 + 学校/公司/单位 + (名称)
    (re.compile(r"(?:我|人家)(?:在|的)(?:学校|大学|公司|单位)(?:叫|是|名字叫)?([^,。!?\n]{2,20})"),
     "workplace"),

    # ── 宠物 ──────────────────────────────────────────
    # 主语扩到 "我老婆/我老公/我爸/我妈/对象/我们家/朋友" 等; 量词放松到 一/两/三/几
    # group(1) 非贪婪止于宠物名 (不需要后续边界 — noun 类别已限定)
    (re.compile(
        r"(?:我|人家|家里|我家|我们家|我老婆|我老公|我爸|我妈|对象|我朋友|TA|他|她|她家|他家)"
        r"(?:有|养了?|领养了?)"
        r"(?:[一二三四五六七八九十两几]?(?:只|条|头|窝|对))?"
        r"[^,。!?\n]*?(猫|狗|鱼|鸟|鹦鹉|龟|兔子|兔|仓鼠|柯基|金毛|哈士奇|泰迪|比熊|拉布拉多|柴犬|萨摩耶|边牧|博美|雪纳瑞|斗牛犬|英短|美短|布偶|橘猫|三花|蓝猫|缅因|波斯|龙猫|刺猬|豚鼠|蜥蜴|鹅|蜘蛛)"
    ), "pet"),

    # ── 兴趣爱好 ──────────────────────────────────────
    # hobby pattern A: 我/人家 + 喜欢 + 玩/打/看/追/学 + N (动词锚定)
    (re.compile(r"(?:我|人家)(?:超|很|特别|最)?(?:爱|喜欢)(?:玩|打|看|追|学)([^,。!?\n玩打看追学吃]{2,8})"),
     "hobby"),
    # hobby pattern B: 我/人家 + 喜欢 + N (无中间动词, 排除 吃 路径 — favorite_foods 处理)
    (re.compile(r"(?:我|人家)(?:超|很|特别|最)?(?:爱|喜欢|喜爱)(?!吃)([^,。!?\n吃玩打看追学的了过]{2,6})"),
     "hobby"),
    # recent_activity: 我/人家 + 在/最近在 + 玩/打/看 + N
    (re.compile(r"(?:我|人家)?(?:在|最近在|这阵子在|这段时间在)(?:玩|打|看|追|学)([^,。!?\n玩打看追学的了]{2,8})"),
     "recent_activity"),

    # ── 近事 ──────────────────────────────────────────
    # 时间锚扩到 "上周末/上周/这周末/前几天/最近", 主语可选
    # 拆 2 group: verb_phrase + noun, "的" 在两 group 之间不进任何 group
    # → _legacy_extract_details 拼最后 2 个 group 得 "买拉布拉多" 而非 "买的拉布拉多"
    (re.compile(
        r"(?:我|人家)?"
        r"(?:今天|昨天|前天|刚刚?|刚才|昨晚|今早|上午|下午|傍晚|晚上|上周末|上周|这周末|这周|最近|前几天|这阵子|周末)"
        r"((?:去|做|吃|买|看|玩|学|追)(?:了|过)?[一二三四五六七八九十两几]?(?:只|条|头|个|次|场|顿|盘|碗|份|杯)?)"
        r"(?:的)?"
        r"([^,。!?\n的]{2,10}?)"
        r"(?=[,。!?\n ]|撑|饱|够|完|好[吃看玩]|$)"
    ), "recent_event"),
    # 没显式时间锚但有 "新/刚" 近期信号
    (re.compile(
        r"(?:新|刚)((?:买|装|订|借|安装|做|学|拿|下载)(?:了|过)?)"
        r"(?:的)?"
        r"([^,。!?\n的]{2,10}?)"
        r"(?=[,。!?\n 还]|$)"
    ), "recent_event"),
]


def _legacy_extract_details(text: str) -> dict[str, list[str]]:
    """旧 regex pattern 抽细节. 保留作 fallback + NLU 路径求 union.

    recent_event 特例: 若 pattern 有 2+ group, 拼最后 2 个 (verb_phrase + noun)
    避免 "买的拉布拉多" 这种 "的" 进 snippet 的情况.
    """
    if not text:
        return {}
    out: dict[str, list[str]] = {}
    for pat, field in _DETAIL_PATTERNS:
        for m in pat.finditer(text):
            groups = [g for g in m.groups() if g]
            if not groups:
                continue
            if field == "recent_event" and len(groups) >= 2:
                detail = "".join(groups[-2:]).strip()
            else:
                detail = groups[-1].strip()
            if not detail or len(detail) < 2:
                continue
            if len(detail) > 30:
                detail = detail[:30]
            out.setdefault(field, []).append(detail)
    return out


# ── HanLP NER → field 映射 rule layer (commit 3) ───────────────────────
# 主人 2026-05-28 phase 5: 4 个 rule bag 大扩, 加现代职业 / popular 菜 / 新狗品种.

_PET_NOUNS = (
    # 通用
    "猫", "狗", "鱼", "鸟", "鹦鹉", "龟", "兔子", "兔", "仓鼠",
    # 狗品种
    "柯基", "金毛", "哈士奇", "泰迪", "比熊", "拉布拉多", "柴犬",
    "萨摩耶", "边牧", "博美", "雪纳瑞", "贵宾", "斗牛犬", "小鹿犬",
    # 猫品种
    "英短", "美短", "布偶", "橘猫", "三花", "蓝猫", "缅因", "波斯",
    # 其它
    "鹅", "蜥蜴", "蜘蛛", "刺猬", "豚鼠", "龙猫",
)
_PET_VERBS = ("养", "有", "撸", "领养", "买", "捡", "抱回", "新买")
_WORKPLACE_HINTS = (
    "公司", "上班", "工作", "单位", "学校", "大学",
    "office", "厂", "总部", "事务所", "工作室", "团队",
)
_FAVORITE_VERBS = (
    "喜欢", "爱", "最爱", "超爱", "真的爱", "超级喜欢", "好喜欢",
    "萌到爆", "戳到", "本命",
)
_RECENT_VERBS = (
    "去", "做", "买", "吃", "玩", "看", "追", "学",
    "刚", "才", "刚才", "刚刚", "新买", "新装", "新办",
    "下载", "安装", "订", "试", "听",
)
_FOOD_WORDS = (
    # 主食
    "饭", "菜", "面", "肉", "汤", "粥", "粉", "饺", "包子", "馒头",
    # 肉类
    "排骨", "鸡", "鸭", "鹅", "虾", "蟹", "牛", "猪", "羊", "豆腐", "鸡蛋",
    # 西式
    "披萨", "汉堡", "薯条", "蛋糕", "面包", "甜品",
    # 饮品
    "奶茶", "咖啡", "果汁", "啤酒", "可乐",
    # 中式特色 (主人 plan 强调火锅/螺蛳粉等现代高频)
    "火锅", "烤鱼", "烤串", "烧烤", "麻辣烫", "麻辣香锅",
    "螺蛳粉", "酸辣粉", "炒粉", "拉面", "乌冬", "凉皮",
    "寿司", "刺身", "天妇罗", "炸鸡", "关东煮",
    "麻婆豆腐", "回锅肉", "鱼香肉丝", "宫保鸡丁", "红烧肉", "糖醋里脊",
    "煲仔饭", "盖浇饭", "炒饭", "烤冷面", "煎饼", "肉夹馍",
    "海底捞", "黄焖鸡", "卤味", "凉菜",
)
_JOB_TABLE = (
    # 传统
    "程序员", "工程师", "设计师", "学生", "医生", "老师", "律师", "司机",
    "厨师", "护士", "经理", "销售", "运营", "产品", "测试", "前端", "后端",
    "全栈", "架构师", "运维", "实习生", "博士", "硕士", "研究员", "讲师",
    "教授", "会计", "出纳", "导演", "记者", "编辑",
    # 现代 / 新职业 (主人 plan 强调)
    "主播", "UP主", "UP 主", "博主", "自媒体", "电商", "客服",
    "网约车司机", "外卖员", "快递员", "代驾", "民宿主", "店主",
    "独立游戏开发者", "前端架构师", "DevOps", "SRE", "数据分析师",
    "心理咨询师", "瑜伽教练", "私教", "健身教练",
    "插画师", "原画师", "动画师", "声优", "歌手", "翻译",
    "策划", "项目经理", "scrum master", "QA", "DBA",
    "网红", "带货主播", "KOL", "博士生", "硕士生", "研究生",
)


def _map_entities_to_slots(
    text: str,
    entities: dict[str, list],
    existing: dict[str, list[str]],
) -> dict[str, list[str]]:
    """把 HanLP entities + POS 转成 user_details 槽位.

    existing 是 legacy regex 已抽到的细节, 用于 dedupe (避免双填).
    rule layer ~50 行, 不调外部.
    """
    out: dict[str, list[str]] = {}
    pos_pairs: list[tuple[str, str]] = entities.get("POS") or []
    tokens: list[str] = [w for w, _p in pos_pairs] if pos_pairs else []
    token_set = set(tokens)
    locs: list[str] = entities.get("LOC") or []
    orgs: list[str] = entities.get("ORG") or []
    dates: list[str] = entities.get("DATE") or []

    def _add(field: str, val: str) -> None:
        val = val.strip()
        if not val or len(val) < 2 or len(val) > 30:
            return
        cur = existing.get(field, [])
        out_list = out.setdefault(field, [])
        if val in cur or val in out_list:
            return
        # substring 包含也算重 (e.g. legacy 已存 '柯基', 别再加 '一只柯基')
        if any(val in c or c in val for c in (cur + out_list)):
            return
        out_list.append(val)

    # workplace: ORG/LOC + token 含 workplace hint
    if (orgs or locs) and any(h in tokens for h in _WORKPLACE_HINTS):
        for e in orgs + locs:
            _add("workplace", e)

    # job: 30 职业查表 ∩ tokens
    for w in tokens:
        if w in _JOB_TABLE:
            _add("job", w)

    # pet: token 含宠物名词 + 前 5 tokens 内有 养/有 (主人 2026-05-28: 扩到 5
    # 修 "我老婆养了一只柯基" 这种主语 + 宠物 + 量词隔开的 case)
    for i, w in enumerate(tokens):
        if any(p in w for p in _PET_NOUNS):
            window = tokens[max(0, i - 5): i]
            if any(v in window for v in _PET_VERBS):
                _add("pet", w)

    # favorite_foods / hobby: 喜欢/爱 后跟名词或动名词
    # HanLP 把"摄影"/"拍照"等动名词判 VV (动词), 也算 hobby. 但跳 NT (time noun) / AD.
    for i, (w, p) in enumerate(pos_pairs):
        if w in _FAVORITE_VERBS:
            for j in range(i + 1, min(i + 4, len(pos_pairs))):
                next_w, next_p = pos_pairs[j]
                # 跳过时间词 (NT) 和副词 (AD), 这些不是 hobby 主体
                if next_p in ("NT", "AD", "CD", "M", "AS", "DEC"):
                    continue
                if next_p.startswith("N") or next_p.startswith("V") or next_p.startswith("v"):
                    if any(f in next_w for f in _FOOD_WORDS):
                        _add("favorite_foods", next_w)
                    elif len(next_w) >= 2:
                        _add("hobby", next_w)
                    break

    # recent_event: verb (去/做/买/吃) + NP. 主人 2026-05-28 v2:
    # - DEC ("的") 跳过不拼 snippet → "新买的拉布拉多" snippet="买拉布拉多" 而非"买的拉布拉多"
    # - 时间锚扩 "上周末/这周末/上周/这周/前几天/晚上/昨晚/今早"
    has_recent_signal = bool(dates) or any(
        w in {
            "刚", "才", "刚刚", "刚才", "今天", "昨天", "前天", "最近", "新",
            "上周", "周末", "下周", "上周末", "这周末", "这周", "前几天",
            "昨晚", "今早", "上午", "下午", "傍晚", "晚上",
        }
        for w in tokens
    )
    if has_recent_signal:
        for i, (w, p) in enumerate(pos_pairs):
            if w in _RECENT_VERBS:
                # 拼 verb + 后续 NP. 跳过中间助词 "了"(AS) / 量词(M); 跳过 "的"(DEC) 但不拼.
                next_parts: list[str] = []
                for j in range(i + 1, min(i + 5, len(pos_pairs))):
                    next_w, np = pos_pairs[j]
                    if np == "DEC":
                        continue  # "的" 不拼进 snippet, 但允许后面继续找 NP
                    if np in ("AS", "M"):
                        next_parts.append(next_w)  # "了"/"一只" 拼进让 snippet 自然
                        continue
                    if np in ("NT", "AD", "CD"):
                        break  # 时间/副词/数字 = NP 边界
                    if np.startswith("N") or np.startswith("V"):
                        next_parts.append(next_w)
                        # 看再下一个是不是同类 NP (灌篮+高手), 是就拼
                        if j + 1 < len(pos_pairs):
                            next_w2, np2 = pos_pairs[j + 1]
                            if np2.startswith("N"):
                                next_parts.append(next_w2)
                        break
                if next_parts:
                    snippet = w + "".join(next_parts)
                    if len(snippet) >= 3:
                        _add("recent_event", snippet)
                break  # 一句一个 recent_event

    return out


def _extract_details(text: str) -> dict[str, list[str]]:
    """从单条 user msg 抓所有命中的细节. 返回 {field: [snippet, ...]}.

    主人 2026-05-28: 加 HanLP NER union 路径.
    - legacy regex 永远跑 (保 recall + 第一次启动时 hanlp 没加载也能用)
    - 配 catty_use_hanlp 开 → 再跑 HanLP NER + rule layer, 跟 legacy 结果求 union
    - HanLP 失败 / 短文本 / 关闭 → 仅 legacy
    """
    legacy = _legacy_extract_details(text)
    if not text:
        return legacy
    try:
        from nonebot import get_plugin_config
        from .config import Config
        cfg = get_plugin_config(Config)
    except Exception:
        return legacy
    if not bool(getattr(cfg, "catty_use_hanlp", False)):
        return legacy
    try:
        from .nlu import hanlp_engine
    except Exception:
        return legacy
    entities = hanlp_engine.extract_entities_sync(text)
    if not entities:
        return legacy
    nlu = _map_entities_to_slots(text, entities, legacy)
    if not nlu:
        return legacy
    # union: legacy slots + nlu slots
    merged: dict[str, list[str]] = {k: list(v) for k, v in legacy.items()}
    for field, items in nlu.items():
        merged.setdefault(field, []).extend(items)
    return merged


# ── Store ───────────────────────────────────────────────────────────────
class UserDetailsStore:
    """per-user 结构化细节, 文件持久化 + LRU.

    内部数据结构:
    {user_id: {
        field: deque[(detail_str, ts), ...],  # deque maxlen=_MAX_DETAILS_PER_FIELD
        ...
    }}
    """

    def __init__(self, memory_path: str | Path) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._path = p.parent / "user_details.json"
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, deque]] = {}
        self._dirty = False
        self._last_access: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        users = raw.get("users", {})
        if not isinstance(users, dict):
            return
        now = time.time()
        for uid, fields in users.items():
            if not isinstance(fields, dict):
                continue
            self._data[str(uid)] = {}
            for field, entries in fields.items():
                if not isinstance(entries, list):
                    continue
                dq = deque(maxlen=_MAX_DETAILS_PER_FIELD)
                for e in entries:
                    if isinstance(e, list) and len(e) >= 2:
                        dq.append((str(e[0]), float(e[1])))
                if dq:
                    self._data[str(uid)][str(field)] = dq
            self._last_access[str(uid)] = now

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "users": {
                uid: {
                    field: [[d, ts] for d, ts in dq]
                    for field, dq in fields.items()
                }
                for uid, fields in self._data.items()
            },
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
            except OSError:
                return False
            self._dirty = False
            return True

    async def background_flush_loop(self) -> None:
        import asyncio
        while True:
            try:
                await asyncio.sleep(30.0)
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                pass

    def _evict_lru(self) -> None:
        if len(self._data) <= _MAX_TOTAL_USERS:
            return
        ordered = sorted(self._last_access.items(), key=lambda kv: kv[1])
        for uid, _ in ordered[: len(self._data) - _MAX_TOTAL_USERS]:
            self._data.pop(uid, None)
            self._last_access.pop(uid, None)

    def record_message(self, user_id: str, text: str) -> None:
        """从消息抓细节 → 入库 (去重 + 时间戳更新)."""
        if not user_id or not text:
            return
        details = _extract_details(text)
        if not details:
            return
        now = time.time()
        with self._lock:
            fields = self._data.setdefault(user_id, {})
            for field, snippets in details.items():
                dq = fields.setdefault(
                    field, deque(maxlen=_MAX_DETAILS_PER_FIELD),
                )
                # 去重: 已存在的 snippet 只更新 ts, 不重复 push
                existing = {s for s, _t in dq}
                for s in snippets:
                    if s in existing:
                        # 更新 ts
                        for i, (snip, _t) in enumerate(dq):
                            if snip == s:
                                dq[i] = (snip, now)
                                break
                    else:
                        dq.append((s, now))
            self._last_access[user_id] = now
            self._evict_lru()
            self._dirty = True

    def get_details(
        self,
        user_id: str,
        *,
        max_age_seconds: float = _DETAIL_TTL_SECONDS,
    ) -> dict[str, list[str]]:
        """返回未过期的细节 {field: [snippets]}. 过期的过滤掉."""
        if not user_id:
            return {}
        now = time.time()
        cutoff = now - max_age_seconds
        with self._lock:
            fields = self._data.get(user_id)
            if not fields:
                return {}
            out: dict[str, list[str]] = {}
            for field, dq in fields.items():
                live = [s for s, ts in dq if ts >= cutoff]
                if live:
                    out[field] = live
            return out


# ── Prompt 注入 ─────────────────────────────────────────────────────────
_FIELD_DISPLAY: dict[str, str] = {
    "favorite_foods": "爱吃的",
    "disliked_foods": "讨厌的食物",
    "job": "工作",
    "workplace": "学校/公司",
    "pet": "养的宠物",
    "hobby": "爱好",
    "recent_activity": "最近在做",
    "recent_event": "近事",
}


def build_user_details_prompt(
    details: dict[str, list[str]],
    user_display: str = "对方",
) -> str:
    """构建 user details prompt 段. 空 details 返回 ""(skip register)."""
    if not details:
        return ""
    lines = [f"【已知{user_display}的细节】(从历史对话自动学的, 可以主动 callback『主人之前不是说 X 嘛?』式):"]
    for field, snippets in details.items():
        label = _FIELD_DISPLAY.get(field, field)
        lines.append(f"- {label}: {', '.join(snippets)}")
    lines.append("(不要复述这段给对方; 自然带进对话即可。最多回头提 1 次。)")
    return "\n".join(lines)


__all__ = [
    "UserDetailsStore",
    "build_user_details_prompt",
]
