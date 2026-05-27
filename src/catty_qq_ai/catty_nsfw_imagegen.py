"""NSFW 自动生图: 笨猫第一次被插入(phase>=4)后,每 3 turn 用 NovelAI 画一张图。

主人 2026-05-28 需求:
- 触发条件: nsfw_phase.current_phase 进入 >=4 (P4 主动迎合 = 已经插入) 之后开始计数
- 间隔: 每 3 turn 画一张
- 参考图: Miao/miaomiao.png (character) + Miao/miaomiaonude.png (nude pose)
  - 都走 Precise Reference 模式 (extracted=0.9) — Precise Reference 和 Vibe Transfer 不能在同一请求共存
  - char 抓笨猫五官/发型/猫耳,nude 抓裸体姿态,两张同样高 extracted 让 NAI 把两者细节都贴上
- 扣费: 每张 10 积分 (主人豁免, 非主人余额不够则跳过本次)
- 路径: 复用 catty_imagegen_nai 通道, 因为 references 在 v4.5 不支持会自动 fallback v3, 这里直接走 v3 不绕弯

counter 落盘 affection.json 同目录 nsfw_imagegen_counter.json, 跨重启保留。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
from nonebot import logger
from PIL import Image as PILImage


# ── 配置默认值 (可被 config 覆盖) ──
DRAW_INTERVAL = 3              # 每 N turn 画一张
COST_PER_IMAGE = 10            # 扣多少积分
PHASE_THRESHOLD = 4            # phase >= 此值才开始计数(P4 主动迎合 = 已经插入)
DEFAULT_PROMPT = (
    "2 figures, 1girl, 1boy, hetero, sex, vaginal, penetration, "
    "white hair, cat ears, cat tail, long hair, twintails, blue eyes, "
    "school uniform, sailor collar, blue ribbon, blue pleated skirt, "
    "white shirt unbuttoned, breasts out, white thighhighs, skirt lifted, "
    "faceless male, masculine hands, missionary, blush, sweat, "
    "open mouth, bed, anime style"
)
DEFAULT_NEGATIVE = (
    "lowres, worst quality, bad anatomy, bad hands, watermark, signature, "
    "jpeg artifacts, multiple girls, multiple boys, futanari, "
    "solo, 1girl alone, single character, single person, only one person, no male, "
    "censored, mosaic censoring, bar censor"
)

# phase 描述给 prompt composer 用 — 让 AI 知道当前情景节奏
_PHASE_BRIEF = {
    4: "笨猫在主动迎合, 已经被插入还在节奏中(主动抬腰/夹紧/喊再深一点)",
    5: "笨猫临界点, 蜜穴一阵阵痉挛, 思维断片, 即将高潮(腿失控抖/视线散/嘴张开)",
    6: "笨猫高潮峰值, 全身痉挛 + 失神(瞳孔散开/腰弓起/尖叫喵呜)",
    7: "高潮后 overstim 过载, 浑身瘫软还在痉挛(泪眼/嘴角口水/全身敏感)",
    8: "余韵 — 全身瘫软, 喘息逐渐平复, 抱着主人撒娇",
}


async def _compose_nai_prompt(config, phase: int, current_reply: str) -> str:
    """让小 AI 根据当前 phase + 最新 reply 上下文写 NAI danbooru tags prompt。

    失败/超时 fallback 到 DEFAULT_PROMPT。返回干净 prompt 字符串(英文 tags)。
    """
    try:
        from .openai_client import chat_completion_instant
    except ImportError:
        return ""

    phase_brief = _PHASE_BRIEF.get(int(phase), "笨猫被插入中")
    system_prompt = (
        "你是 NovelAI 提示词生成器。根据下面情景写一段**英文 danbooru tag 风格**的 NSFW 生图 prompt"
        "(逗号分隔, 80-180 个 token)。\n"
        "\n"
        "**必带 tags(顺序无所谓但都要有)**:\n"
        "  1) 双人锁: `2 figures, 1girl, 1boy, hetero`\n"
        "  2) 笨猫角色锁: `white hair, cat ears, cat tail, long hair, twintails, blue eyes`\n"
        "  3) 笨猫衣物(半解状态): `school uniform, sailor collar, blue ribbon, blue pleated skirt, "
        "white shirt unbuttoned, breasts out, thigh-high stockings (white), skirt lifted`\n"
        "  4) 主人(faceless / 视角侧): `male, masculine hands, faceless male` (主人脸不必出现, 只要能感觉到在场)\n"
        "  5) **明确性行为**: `sex, vaginal, penetration, pussy juice` (必须画出插入, 不能只是暧昧)\n"
        "\n"
        "**情景 tags(根据 phase 选)**:\n"
        "  - P4 主动迎合: `missionary 或 cowgirl 或 doggystyle`, `clenched teeth`, `flushed`, `sweat`, `saliva trail`, `hands holding waist`\n"
        "  - P5 临界: `rolling eyes`, `open mouth`, `tears`, `drool`, `trembling legs`, `ahegao incoming`, `clenched sheets`\n"
        "  - P6 高潮: `orgasm`, `ahegao`, `fucked silly`, `tongue out`, `rolling eyes`, `squirting`, `arched back`, `creampie`, `tears flowing`\n"
        "  - P7 overstim: `aftershock`, `trembling`, `overflow`, `cum drip`, `mind break`, `half lidded eyes`\n"
        "  - P8 余韵: `afterglow`, `lying down`, `cuddling`, `cum on body`, `sleepy smile`, `soft lighting`, `bed sheet covering`\n"
        "\n"
        "**输出**: 只输出英文 tags(逗号分隔), 不要解释 / 不要 negative / 不要中文 / 不要 markdown。"
        "格式: tag1, tag2, tag3, ..."
    )
    user_prompt = (
        f"当前 NSFW phase: P{phase} — {phase_brief}\n"
        f"刚生成的笨猫剧情(参考姿态/动作/情绪):\n{(current_reply or '').strip()[:600]}\n\n"
        "请输出 NAI danbooru tags:"
    )

    try:
        raw = await asyncio.wait_for(
            chat_completion_instant(
                config,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                fallback_max_tokens=300,
            ),
            timeout=20.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"nsfw_imagegen compose prompt failed: {exc}")
        return ""

    if not raw:
        return ""
    cleaned = raw.strip()
    # 剥掉 markdown / 引号 / 常见 prefix
    for prefix in ("Prompt:", "prompt:", "Tags:", "tags:", "PROMPT:", "TAGS:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    if cleaned.startswith("```"):
        # 剥 fenced code block
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:]).strip()
    cleaned = cleaned.strip("`'\"").strip()
    # 多行合并(NAI tags 一行就够)
    cleaned = ", ".join(part.strip() for part in cleaned.splitlines() if part.strip())
    # 强 fallback: AI 漏关键 tag 时手动补 (双人 / 角色 / 衣物 / 性行为 4 类必须有)
    lowered = cleaned.lower()
    extra: list[str] = []
    if "1girl" not in lowered:
        extra.append("1girl")
    if "1boy" not in lowered and "faceless male" not in lowered and "male" not in lowered:
        extra.append("1boy, hetero, faceless male")
    if "cat ears" not in lowered and "cat_ears" not in lowered:
        extra.append("cat ears, cat tail")
    if "white hair" not in lowered:
        extra.append("white hair")
    if "school uniform" not in lowered and "sailor" not in lowered:
        extra.append("school uniform, sailor collar, blue ribbon, blue pleated skirt")
    if "thighhigh" not in lowered and "thigh-high" not in lowered and "stocking" not in lowered:
        extra.append("white thighhighs")
    # 明确性行为(P4-P7 都要画出来; P8 余韵单独 fallback 到 afterglow 不强补 penetration)
    if int(phase) <= 7 and not any(k in lowered for k in ("sex", "penetration", "vaginal", "fucked")):
        extra.append("sex, vaginal, penetration")
    if extra:
        cleaned = cleaned + ", " + ", ".join(extra)
    return cleaned[:800]  # 上限 800 字符防上游

# v3 是目前 NAI 后端唯一接受 reference_image_multiple 的模型, 本模块强制走 v3
NAI_MODEL = "nai-diffusion-3"
NAI_ENDPOINT = "https://image.novelai.net/ai/generate-image"

# ── counter 落盘 ──
_COUNTER: dict[str, dict[str, int]] = {}  # key → {turn_count, draws_made}
_COUNTER_FILE: Path | None = None
_COUNTER_DIRTY = False
_COUNTER_LOCK = threading.RLock()


def init_counter_path(memory_path: str | Path) -> None:
    """启动时调用,跟 affection.json 放同一目录,nsfw_imagegen_counter.json。"""
    global _COUNTER_FILE
    mem = Path(memory_path).expanduser()
    if not mem.is_absolute():
        mem = mem.resolve()
    _COUNTER_FILE = mem.parent / "nsfw_imagegen_counter.json"
    _load_counter()


def _load_counter() -> None:
    global _COUNTER
    if _COUNTER_FILE is None or not _COUNTER_FILE.is_file():
        return
    try:
        raw = json.loads(_COUNTER_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _COUNTER = {
                str(k): {"turn_count": int(v.get("turn_count", 0)), "draws_made": int(v.get("draws_made", 0))}
                for k, v in raw.items() if isinstance(v, dict)
            }
            logger.info(f"nsfw_imagegen counter loaded: {len(_COUNTER)} entries")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"nsfw_imagegen counter load failed: {exc}")


def _save_counter() -> None:
    global _COUNTER_DIRTY
    if _COUNTER_FILE is None or not _COUNTER_DIRTY:
        return
    try:
        with _COUNTER_LOCK:
            data = dict(_COUNTER)
        tmp = _COUNTER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_COUNTER_FILE)
        _COUNTER_DIRTY = False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"nsfw_imagegen counter save failed: {exc}")


def _key(scope_key: str, user_id: str) -> str:
    return f"{scope_key}::{user_id}"


def reset_counter(scope_key: str, user_id: str) -> None:
    """NSFW arc 结束(reset_phase / closing)时调用, 清掉这个 scope+user 的计数。"""
    global _COUNTER_DIRTY
    k = _key(scope_key, user_id)
    with _COUNTER_LOCK:
        if k in _COUNTER:
            _COUNTER.pop(k, None)
            _COUNTER_DIRTY = True


def _bump_turn(scope_key: str, user_id: str) -> tuple[int, int]:
    """phase >= PHASE_THRESHOLD 时调一次, 返回 (turn_count_after, draws_made)。"""
    global _COUNTER_DIRTY
    k = _key(scope_key, user_id)
    with _COUNTER_LOCK:
        rec = _COUNTER.setdefault(k, {"turn_count": 0, "draws_made": 0})
        rec["turn_count"] = int(rec.get("turn_count", 0)) + 1
        _COUNTER_DIRTY = True
        return int(rec["turn_count"]), int(rec.get("draws_made", 0))


def _mark_drawn(scope_key: str, user_id: str) -> None:
    """成功画完一张后 +1 draws_made。"""
    global _COUNTER_DIRTY
    k = _key(scope_key, user_id)
    with _COUNTER_LOCK:
        rec = _COUNTER.setdefault(k, {"turn_count": 0, "draws_made": 0})
        rec["draws_made"] = int(rec.get("draws_made", 0)) + 1
        _COUNTER_DIRTY = True


def get_counter_snapshot(scope_key: str, user_id: str) -> tuple[int, int]:
    k = _key(scope_key, user_id)
    with _COUNTER_LOCK:
        rec = _COUNTER.get(k) or {}
        return int(rec.get("turn_count", 0)), int(rec.get("draws_made", 0))


def flush_counter() -> bool:
    """flush dirty counter 到磁盘 (后台 task 周期调)。"""
    if _COUNTER_DIRTY:
        _save_counter()
        return True
    return False


# ── 参考图: 启动时一次性加载 + cache,避免每次 disk I/O ──
_REF_CACHE: dict[str, str] = {}


def _load_reference_base64(path: Path) -> str:
    """读图 → resize 448x448 letterbox → PNG base64 (NAI v4 reference_image_multiple 格式)。"""
    key = str(path.resolve())
    if key in _REF_CACHE:
        return _REF_CACHE[key]
    src = PILImage.open(path).convert("RGBA")
    canvas = PILImage.new("RGBA", (448, 448), (0, 0, 0, 0))
    src.thumbnail((448, 448), PILImage.LANCZOS)
    off_x = (448 - src.width) // 2
    off_y = (448 - src.height) // 2
    canvas.paste(src, (off_x, off_y), src)
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    _REF_CACHE[key] = b64
    return b64


def _find_reference_dir(config) -> Path | None:
    """Miao/ 目录在 catty cwd 下。"""
    candidates = [
        Path.cwd() / "Miao",
        Path(getattr(config, "catty_imagegen_cache_dir", "pictures/imagegen_cache")).parent / "Miao",
    ]
    for p in candidates:
        if p.is_dir() and (p / "miaomiao.png").is_file():
            return p
    return None


# ── 主入口: 判断 + 异步生图 ──
async def maybe_generate_image(
    *,
    config,
    scope_key: str,
    user_id: str,
    phase_state,
    affection_store,
    current_reply: str = "",
) -> Any:
    """如果当前 turn 触发画图条件, 走 NAI + 双参考图生成, 返回 MessageSegment(发送由调用者负责)。
    不触发 / 余额不足 / 报错 都返回 None, 调用者不需特殊处理。

    扣费在画图成功后才发生(主人豁免)。
    """
    # 0) 前置: phase < 阈值不计数也不画
    if not phase_state or getattr(phase_state, "current_phase", 0) < PHASE_THRESHOLD:
        return None

    # 1) NAI 通道未启用 / token 缺 → 静默跳过
    if not getattr(config, "catty_imagegen_nai_enabled", False):
        return None
    token = str(getattr(config, "catty_imagegen_nai_token", "") or "").strip()
    if not token:
        return None

    # 2) 累加 turn 计数, 决定本轮是否触发
    turn_count, draws_made = _bump_turn(scope_key, user_id)
    if turn_count % DRAW_INTERVAL != 0:
        return None

    # 3) 余额校验 (主人豁免)
    cost = COST_PER_IMAGE
    if affection_store is not None and cost > 0:
        is_owner = bool(affection_store.is_owner(user_id))
        if not is_owner:
            balance = int(affection_store.get_points(user_id) or 0)
            if balance < cost:
                logger.info(
                    f"nsfw_imagegen skip (balance {balance} < {cost}, "
                    f"user={user_id}, turn_count={turn_count})"
                )
                return None

    # 4) 找参考图
    ref_dir = _find_reference_dir(config)
    if ref_dir is None:
        logger.warning("nsfw_imagegen: Miao/ 目录或 miaomiao.png 缺失, 跳过本次")
        return None
    char_ref = ref_dir / "miaomiao.png"
    style_ref = ref_dir / "miaomiaonude.png"
    if not style_ref.is_file():
        # nude 缺就用 char 复用
        style_ref = char_ref
    try:
        char_b64 = _load_reference_base64(char_ref)
        style_b64 = _load_reference_base64(style_ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"nsfw_imagegen: 参考图加载失败 {exc}")
        return None

    # 5) 让 AI 根据当前 phase + 刚生成的 reply 写 NAI danbooru tags prompt (失败 fallback default)
    composed = await _compose_nai_prompt(config, int(phase_state.current_phase), current_reply)
    prompt = composed or str(
        getattr(config, "catty_nsfw_imagegen_prompt", "") or DEFAULT_PROMPT
    ).strip() or DEFAULT_PROMPT
    negative = str(
        getattr(config, "catty_nsfw_imagegen_negative", "") or DEFAULT_NEGATIVE
    ).strip() or DEFAULT_NEGATIVE
    width, height = 832, 1216  # portrait 立绘
    steps = 28

    payload = {
        "input": prompt,
        "model": NAI_MODEL,
        "action": "generate",
        "parameters": {
            "width": width,
            "height": height,
            "scale": 5.0,
            "sampler": "k_euler_ancestral",
            "steps": steps,
            "n_samples": 1,
            "seed": int(time.time()) & 0xFFFFFFFF,
            "negative_prompt": negative,
            "ucPreset": 0,
            "qualityToggle": True,
            "sm": False,
            "sm_dyn": False,
            "noise_schedule": "karras",
            # 两张都走 Precise Reference (高 extracted=0.9 — Precise vs Vibe 不能混)
            # strength 降到 0.4/0.3: extracted=0.9 抓角色细节(脸/猫耳/发色), strength 低让
            # composition 由 prompt 主导(1boy/sex/penetration 才有空间画出来, 不被单人立绘 reference 压死)
            "reference_image_multiple": [char_b64, style_b64],
            "reference_information_extracted_multiple": [0.9, 0.9],
            "reference_strength_multiple": [0.4, 0.3],
        },
    }

    # 6) HTTP
    proxy_str = str(getattr(config, "catty_http_proxy", "") or "").strip()
    timeout = float(getattr(config, "catty_imagegen_nai_timeout_seconds", 180.0) or 180.0)
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=15.0),
        "follow_redirects": True,
        "http2": False,
        "limits": httpx.Limits(max_keepalive_connections=0, max_connections=10),
    }
    if proxy_str:
        client_kwargs["proxy"] = proxy_str

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(
                NAI_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                },
                json=payload,
            )
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        elapsed = time.monotonic() - started
        logger.warning(
            f"nsfw_imagegen transport error after {elapsed:.1f}s: "
            f"{exc.__class__.__name__}: {exc} (user={user_id}, turn={turn_count})"
        )
        return None

    elapsed = time.monotonic() - started
    if response.status_code != 200:
        detail = response.text[:300]
        logger.warning(
            f"nsfw_imagegen status={response.status_code} elapsed={elapsed:.1f}s "
            f"body={detail} (user={user_id}, turn={turn_count})"
        )
        return None

    # 7) 解 zip 拿图
    try:
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = zf.namelist()
        if not names:
            logger.warning("nsfw_imagegen: zip 是空的")
            return None
        image_bytes = zf.read(names[0])
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        logger.warning(f"nsfw_imagegen: zip 解包失败 {exc}")
        return None
    if not image_bytes:
        return None

    # 8) 落盘
    cache_dir_raw = str(
        getattr(config, "catty_imagegen_cache_dir", "pictures/imagegen_cache") or "pictures/imagegen_cache"
    )
    cache_dir = Path(cache_dir_raw).expanduser()
    if not cache_dir.is_absolute():
        cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    fname = f"nsfw_auto_{int(time.time()*1000)}.png"
    file_path = cache_dir / fname
    try:
        file_path.write_bytes(image_bytes)
    except OSError as exc:
        logger.warning(f"nsfw_imagegen: 写图失败 {exc}")
        return None

    # 9) 构造 segment
    try:
        from nonebot.adapters.onebot.v11 import MessageSegment
    except ImportError:
        return None
    segment = MessageSegment.image(file=file_path.resolve().as_uri())

    # 10) 扣积分(主人豁免) + 记录
    consume_log = ""
    if affection_store is not None and cost > 0:
        try:
            res = affection_store.consume_points(user_id, cost)
            consume_log = (
                f"cost={cost} balance_after={res.get('balance_after')} "
                f"is_owner={res.get('is_owner')}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"nsfw_imagegen consume_points failed: {exc}")

    _mark_drawn(scope_key, user_id)
    logger.info(
        f"nsfw_imagegen ★ drew (user={user_id} scope={scope_key} "
        f"phase=P{phase_state.current_phase} turn_count={turn_count} "
        f"draws_made={draws_made + 1} elapsed={elapsed:.1f}s bytes={len(image_bytes)} "
        f"file={file_path.name} {consume_log} "
        f"ai_composed={'yes' if composed else 'no'} prompt={prompt[:200]!r})"
    )
    return segment
