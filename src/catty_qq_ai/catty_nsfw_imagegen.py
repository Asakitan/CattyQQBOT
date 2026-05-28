"""NSFW 自动生图: 笨猫第一次被插入(phase>=4)后,每 3 turn 用 NovelAI 画一张图。

主人 2026-05-28 需求:
- **触发: 每次 user 发一句 NSFW 消息 = 一个 turn, 每 3 个 turn 画一张**
  (不看 phase, phase 只用来给 composer 决定画哪个情景。这个函数挂在 NSFW spark
   路径回复成功后, 走到这里就意味着 user 发了 NSFW 触发了 spark, 算一个 turn)
- 模型: NovelAI v4.5-full
- **Precise Reference (director_reference_*)**: Miao/miaomiao.png + miaomiaonude.png 两张
  作为 character&style reference (fidelity=1.0 strength=1.0), 把笨猫角色细节 + 画风锁死
- **characterPrompts 双角色锁**: 笨猫 (左) + 主人 (右,黑色剪影 + 无脸)
- 图片格式: 1024x1536 letterbox PNG base64 (NAI SDK 标准)
- 扣费: 每张 10 积分 (主人豁免, 非主人余额不够则跳过本次)
- prompt: 由 AI(spark/instant) 根据当前 phase + 刚生成的 reply 写 base caption 描述情景

counter 落盘 affection.json 同目录 nsfw_imagegen_counter.json, 跨重启保留。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import tempfile
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from nonebot import logger


async def _curl_post_json(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    proxy: str = "",
    timeout: float = 120.0,
) -> tuple[int, bytes, str]:
    """走 subprocess curl.exe POST JSON, 返回 (status_code, body_bytes, error_msg)。

    主人 2026-05-28: Python httpx/socksio/httpx-socks/curl_cffi 等所有 async SOCKS5
    实现连这个 gost proxy 都有 30-50% 偶发 ConnectError reset (Win Error 10054),
    但 curl.exe 100% 稳定. 改 subprocess curl.exe 绕过 Python SOCKS 实现 bug.

    body 写临时文件读出避免 stdout 二进制 zip 污染 status marker.
    """
    curl_exe = shutil.which("curl") or "curl.exe"
    body_file = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    body_file.close()
    body_path = body_file.name
    try:
        args = [
            curl_exe, "-sS", "-X", "POST",
            "--max-time", str(int(timeout)),
            "-d", "@-",  # payload from stdin
            "-o", body_path,  # body 写文件
            "-w", "%{http_code}",  # stdout 只剩 status code
        ]
        if proxy:
            args.extend(["-x", proxy])
        for k, v in headers.items():
            args.extend(["-H", f"{k}: {v}"])
        args.append(url)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return 0, b"", f"curl not found: {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload).encode("utf-8")),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return 0, b"", f"curl timeout after {timeout}s"
        status_str = stdout.strip().decode("ascii", errors="replace")
        try:
            status = int(status_str or "0")
        except ValueError:
            status = 0
        body = b""
        try:
            with open(body_path, "rb") as f:
                body = f.read()
        except OSError:
            pass
        if proc.returncode != 0 and status == 0:
            return 0, body, f"curl exit={proc.returncode} stderr={stderr.decode('utf-8', 'replace')[:300]}"
        return status, body, ""
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


# ── 配置默认值 (可被 config 覆盖) ──
DRAW_INTERVAL = 3              # 每 N 个 user turn 画一张
COST_PER_IMAGE = 10            # 扣多少积分
# 主人 2026-05-28: 不按 phase 卡入口, 走到这函数本身就意味着是 NSFW spark 路径处理过的 user 消息
# (chat handler 在 _was_refusal=False 后才调这函数), 所以一调就 +1 turn, 每 3 turn 画。

# 角色锁 (硬编码, 不让 AI 决定; 实际细节由 Precise Reference 图片提供)
CHARACTER_GIRL_TAGS = (
    "1girl, blonde hair, ahoge, twintails, blue eyes, "
    "white cat ears, white cat tail, blue ribbon hair tie, "
    "school uniform, sailor collar, blue ribbon, "
    "white shirt unbuttoned, breasts out, white thighhighs, skirt lifted, "
    "blush, sweat, parted lips"
)
CHARACTER_BOY_TAGS = (
    "1boy, faceless male, completely black silhouette, dark shadow body, "
    "transparent dark figure, no face, no skin detail, pure silhouette"
)
CHARACTER_BOY_UC = "male face, light skin, visible eyes, normal man body, realistic male"

# AI composer 失败时的兜底 base caption (默认中性场景, AI 失败就回 P4 missionary)
# {{nsfw}}, {{explicit}} 必带 — NAI v4.5 默认 SFW 滤镜, 不带就画 SFW
DEFAULT_BASE_CAPTION = (
    "{{1boy with 1girl}}, {{hetero couple together}}, {{2 figures, hetero}}, "
    "{{nsfw}}, {{explicit}}, kissing, hugging, on bed, indoor, "
    "anime style, dramatic lighting, romantic atmosphere"
)
DEFAULT_NEGATIVE = (
    "lowres, worst quality, bad anatomy, bad hands, watermark, signature, "
    "jpeg artifacts, multiple girls, multiple boys, futanari, "
    # 主人 2026-05-30: 用 {{}} 给 solo 拒绝 tag 提权 — Precise Reference (solo 笨猫) 把
    # 单人构图也锁过来了, characterPrompts 里的 1boy 黑影渲不出来 → 主人在画面里消失.
    "{{{solo}}}, {{{1girl alone}}}, {{{single character}}}, {{{single person}}}, "
    "{{{only one person}}}, {{{no male}}}, {{empty space}}, {{missing partner}}, "
    "male skin color visible, realistic male body, three figures, 3 figures, "
    "censored, mosaic censoring, bar censor, "
    "sfw, safe, family friendly, clothed sex, fully clothed"
)

# v4.5 是默认模型 (支持 characterPrompts 双角色 + director_reference Precise Reference)
NAI_MODEL = "nai-diffusion-4-5-full"
NAI_ENDPOINT = "https://image.novelai.net/ai/generate-image"

# Precise Reference 标准尺寸 (NAI SDK crop_and_resize 默认), letterbox 黑边居中
_DIRECTOR_REF_TARGET = (1024, 1536)


def _load_director_reference(path: Path) -> str:
    """读图 → 等比缩到 1024x1536 letterbox 黑边居中 → PNG base64 (NAI Precise Reference 标准)。"""
    from PIL import Image as PILImage
    src = PILImage.open(path).convert("RGB")
    tgt_w, tgt_h = _DIRECTOR_REF_TARGET
    src_w, src_h = src.size
    src_ratio = src_w / src_h
    tgt_ratio = tgt_w / tgt_h
    if src_ratio > tgt_ratio:
        new_w = tgt_w
        new_h = int(tgt_w / src_ratio)
    else:
        new_h = tgt_h
        new_w = int(tgt_h * src_ratio)
    resized = src.resize((new_w, new_h), PILImage.LANCZOS)
    canvas = PILImage.new("RGB", (tgt_w, tgt_h), (0, 0, 0))
    canvas.paste(resized, ((tgt_w - new_w) // 2, (tgt_h - new_h) // 2))
    buf = BytesIO()
    canvas.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# 启动时一次性 cache reference base64 (Path.resolve() str → b64), 避免每次 disk + PIL
_REF_CACHE: dict[str, str] = {}


def _get_reference_b64(path: Path) -> str:
    key = str(path.resolve())
    if key in _REF_CACHE:
        return _REF_CACHE[key]
    b64 = _load_director_reference(path)
    _REF_CACHE[key] = b64
    return b64


def _find_miao_dir(config) -> Path | None:
    """找 Miao/ 目录(本地 cwd 优先, 不存在则尝试 config 路径)。"""
    candidates = [
        Path.cwd() / "Miao",
        Path(getattr(config, "catty_imagegen_cache_dir", "pictures/imagegen_cache") or ".").parent / "Miao",
    ]
    for p in candidates:
        if p.is_dir() and (p / "miaomiao.png").is_file():
            return p
    return None

# phase 描述给 prompt composer 用 (P1-P8 全覆盖, 触发不看 phase 但情景看)
_PHASE_BRIEF = {
    1: "暧昧/试探阶段, 脸红心跳, 偷瞄主人, 还没动手只是气氛上来(crush blush look)",
    2: "升温/前戏, 接吻 / 抚摸 / 主人手摸到敏感处, 衣服还在(kiss touching foreplay)",
    3: "前戏深化, 解衣服 / 摸胸 / 摸大腿, 主人手伸进衣服, 喘息开始(undressing groping)",
    4: "主动迎合, 已经被插入还在节奏中(主动抬腰/夹紧/喊再深一点)",
    5: "临界点, 蜜穴一阵阵痉挛, 思维断片, 即将高潮(腿失控抖/视线散/嘴张开)",
    6: "高潮峰值, 全身痉挛 + 失神(瞳孔散开/腰弓起/尖叫喵呜)",
    7: "高潮后 overstim 过载, 浑身瘫软还在痉挛(泪眼/嘴角口水/全身敏感)",
    8: "余韵 — 全身瘫软, 喘息逐渐平复, 抱着主人撒娇",
}


# ── counter 落盘 ──
_COUNTER: dict[str, dict[str, int]] = {}
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
    """NSFW arc 结束时清掉这个 scope+user 的计数。"""
    global _COUNTER_DIRTY
    k = _key(scope_key, user_id)
    with _COUNTER_LOCK:
        if k in _COUNTER:
            _COUNTER.pop(k, None)
            _COUNTER_DIRTY = True


def _bump_turn(scope_key: str, user_id: str) -> tuple[int, int]:
    global _COUNTER_DIRTY
    k = _key(scope_key, user_id)
    with _COUNTER_LOCK:
        rec = _COUNTER.setdefault(k, {"turn_count": 0, "draws_made": 0})
        rec["turn_count"] = int(rec.get("turn_count", 0)) + 1
        _COUNTER_DIRTY = True
        return int(rec["turn_count"]), int(rec.get("draws_made", 0))


def _mark_drawn(scope_key: str, user_id: str) -> None:
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
    if _COUNTER_DIRTY:
        _save_counter()
        return True
    return False


# ── AI 写情景 caption (不用写角色锁, 那是 characterPrompts 的事) ──
def _format_scene_block(
    phase_state,
    pregnancy_state,
    affection_info: dict | None,
    user_text: str,
    recent_messages: list | None,
    current_reply: str,
) -> str:
    """把所有场景信息序列化成给 deepseek 看的人类可读 block。

    主人 2026-05-30: 旧版只传 phase int + 笨猫 reply, deepseek 看不到主人原话
    (例如『扯掉内裤』『按到桌上』这种具体场景指令) 和角色状态 (怀孕/失神/穿着/
    敏感部位锁), 画出来的 caption 经常和当下剧情对不上 (主人桌上 → AI 默写床上).
    把场景上下文打包丢给 deepseek, 让它的 caption 真正对应当前 turn 的情境.
    """
    lines: list[str] = []

    # === 1. 阶段 & arc ===
    if phase_state is not None:
        ph = int(getattr(phase_state, "current_phase", 1) or 1)
        brief = _PHASE_BRIEF.get(ph, "")
        lines.append(f"[NSFW phase] P{ph} ({brief})")
        if int(getattr(phase_state, "turn_count", 0) or 0) > 0:
            lines.append(f"[phase turn] 当前 phase 已持续 {phase_state.turn_count} 轮")
        if int(getattr(phase_state, "arc_count", 1) or 1) > 1:
            lines.append(
                f"[arc] 第 {phase_state.arc_count} 轮 arc"
                "(身体有上一轮记忆, 更易入境, 可有疲劳感)"
            )
        if int(getattr(phase_state, "climax_count", 0) or 0) > 0:
            lines.append(f"[climax_count] 本 arc 已高潮 {phase_state.climax_count} 次")
        if bool(getattr(phase_state, "dazed", False)):
            tdz = int(getattr(phase_state, "turns_dazed", 0) or 0)
            lines.append(f"[dazed] 失神状态 (持续 {tdz} 轮) — 表情应有恍惚/瞳孔散开/失焦")

        # === 2. 场景锚点 (location/outfit/time_of_day) ===
        loc = str(getattr(phase_state, "location", "") or "").strip()
        amb = str(getattr(phase_state, "location_ambient", "") or "").strip()
        if loc or amb:
            lines.append(f"[location] {loc or '(未指定)'}" + (f" — {amb}" if amb else ""))
        outfit = str(getattr(phase_state, "outfit", "") or "").strip()
        if outfit:
            lines.append(f"[outfit] {outfit}")
        tod = str(getattr(phase_state, "time_of_day", "") or "").strip()
        if tod:
            lines.append(f"[time_of_day] {tod}")
        mood = str(getattr(phase_state, "mood", "") or "").strip()
        if mood:
            lines.append(f"[mood] {mood}")
        body_focus = str(getattr(phase_state, "body_focus", "") or "").strip()
        if body_focus:
            lines.append(f"[body_focus] {body_focus} — 重点刻画此部位")
        facet = str(getattr(phase_state, "personality_facet", "") or "").strip()
        if facet:
            lines.append(f"[personality_facet] {facet}")

    # === 3. 怀孕状态 ===
    if pregnancy_state is not None:
        is_preg = bool(getattr(pregnancy_state, "is_pregnant", False))
        if is_preg:
            pc = int(getattr(pregnancy_state, "pregnancy_count", 0) or 0)
            lines.append(
                f"[pregnancy] 笨猫已怀孕 (孕中 +{pc}) — 画面可加微凸小腹"
                "/手扶肚子/孕期柔光"
            )
        elif int(getattr(pregnancy_state, "total_pregnancies", 0) or 0) > 0:
            lines.append(
                f"[pregnancy] 经产体 (总怀孕 {pregnancy_state.total_pregnancies} 次)"
                " — 身体更熟"
            )

    # === 4. 好感/积分 ===
    if affection_info:
        lvl = affection_info.get("level")
        is_own = affection_info.get("is_owner")
        if lvl is not None or is_own is not None:
            tags = []
            if is_own:
                tags.append("真实主人")
            if lvl is not None:
                tags.append(f"Lv={lvl}")
            if tags:
                lines.append(f"[relation] {' / '.join(tags)}")

    # === 5. 用户原话 (本轮场景指令) — 最关键 ===
    ut = (user_text or "").strip()
    if ut:
        lines.append(f"[主人本轮原话] {ut[:300]}")

    # === 6. 最近对话 (最多 3 轮 user/assistant) ===
    if recent_messages:
        tail: list[str] = []
        try:
            for m in recent_messages[-12:]:  # 最多扫尾部 12 条
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = m.get("content", "")
                if isinstance(content, list):
                    # Anthropic content blocks → 拼 text
                    text = " ".join(
                        str(b.get("text", "") or "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                else:
                    text = str(content or "").strip()
                if not text:
                    continue
                # 砍掉 catty internal hint 包裹避免污染场景理解
                if "<<<CATTY_INTERNAL_INSTRUCTION" in text:
                    text = text.split("<<<CATTY_INTERNAL_INSTRUCTION")[0].strip()
                if not text:
                    continue
                tail.append(f"{role}: {text[:200]}")
        except Exception:  # noqa: BLE001
            tail = []
        # 只留最后 6 条 (~ 3 轮 user/assistant)
        tail = tail[-6:]
        if tail:
            lines.append("[最近对话]\n" + "\n".join(tail))

    # === 7. 笨猫刚生成的 reply (姿态/动作/情绪参考) ===
    cr = (current_reply or "").strip()
    if cr:
        lines.append(f"[笨猫本轮 reply 片段] {cr[:400]}")

    return "\n".join(lines)


async def _compose_base_caption(
    config,
    phase_state,
    current_reply: str,
    *,
    user_text: str = "",
    recent_messages: list | None = None,
    pregnancy_state=None,
    affection_info: dict | None = None,
) -> str:
    """让 deepseek 根据完整场景信息写 base caption(情景/构图/动作)。

    **不要 AI 写角色锁** (1girl/1boy/白发/黑影等), 那些由 characterPrompts 锁定。
    AI 只描述: 体位/动作/表情/分泌物/场景/光线。

    主人 2026-05-30: 升级 — 不再只传 phase int, 而是把
        (phase_state 全字段 + pregnancy_state + affection + 用户原话 +
         最近 3 轮对话 + 笨猫 reply)
    全部序列化丢给 deepseek, 让它的 caption 真正贴当下场景, 不再瞎默写床上.
    """
    # 主人 2026-05-29: 修复 — 旧实现走 chat_completion_codex_instant, 但 codex_instant
    # 内部 model 优先级是 codex_instant_model > nsfw_spark_model > filter_model, 而 config
    # 没配 codex_instant_model, nsfw_spark_model = claude-sonnet-4-6 → 实际 caption 依然
    # 跑主 sonnet (每 3 turn 烧 sonnet token + 经 NewAPI relay 不省钱). 改成直接调
    # ai_fallback (DeepSeek 官方端点) 跟 catty_imagegen 的 _deepseek_imagegen_plan 对齐.
    _fb_base = str(getattr(config, "catty_ai_fallback_base_url", "") or "").strip()
    _fb_key = str(getattr(config, "catty_ai_fallback_api_key", "") or "").strip()
    _fb_model = str(getattr(config, "catty_ai_fallback_model", "") or "").strip()
    if not (_fb_base and _fb_key and _fb_model):
        logger.info("nsfw_imagegen compose: ai_fallback (DeepSeek) 未配齐, 跳过 caption 生成")
        return ""

    phase_int = int(getattr(phase_state, "current_phase", 1) or 1) if phase_state else 1

    scene_block = _format_scene_block(
        phase_state=phase_state,
        pregnancy_state=pregnancy_state,
        affection_info=affection_info,
        user_text=user_text,
        recent_messages=recent_messages,
        current_reply=current_reply,
    )

    system_prompt = (
        "你是 NovelAI v4.5 提示词生成器。根据**完整场景信息**输出"
        "**英文 danbooru tag 风格** base caption(逗号分隔, 60-150 token)。\n"
        "\n"
        "**关键: 必须 honor 当前场景** — 主人原话 / 当前 location / 当前 outfit / "
        "body_focus / pregnancy / dazed 状态等都要反映到 tags 里, 不要瞎默写床上 missionary。\n"
        "\n"
        "**只写情景 tags** — 角色锁(1girl/1boy/白发/黑影)已由 characterPrompts 锁定, 不要重复。\n"
        "**NAI v4.5 提权语法**: `{{tag}}` 加权 1.1x。把**关键体位/性行为**用 {{}} 提权。\n"
        "\n"
        "**必带类型(必须都覆盖)**:\n"
        "  0) **NSFW 锁(NAI v4.5 默认 SFW 滤镜, 不带这两个就只画 SFW)**: `{{nsfw}}, {{explicit}}`\n"
        "  1) **场景锁**: `{{2 figures, hetero}}` (因为只有这样 NAI 才同时画两人)\n"
        "  2) **性行为强度按 phase 选(P1-P3 不要写 penetration!)**:\n"
        "     - P1 暧昧: `{{blush}}, looking at viewer, close-up`, NO sex tags\n"
        "     - P2 前戏: `{{kiss}}, hugging, touching`, NO penetration\n"
        "     - P3 解衣: `{{undressing}}, {{groping breast}}, half undressed`, NO penetration yet\n"
        "     - P4-P7 性行为: `{{vaginal penetration}}, {{sex}}` (必带)\n"
        "     - P8 余韵: 不带 penetration, 带 afterglow/cum on body\n"
        "  3) **体位/动作**: missionary / cowgirl / doggystyle / spooning / sex from behind / "
        "on top / standing / against wall / on desk / 等 — **按场景 location 选, 不要默认床**\n"
        "  4) **环境**: 按 [location] 写 — bed/desk/bathroom/classroom/kitchen/floor/sofa/shower 等; "
        "光线按 [time_of_day] (morning sunlight / midnight dim / etc)\n"
        "  5) **服装**: 按 [outfit] 起手, P2-P3 加 partial undressing, P4+ 加 disheveled/undone\n"
        "  6) **情绪/表情/分泌物**(根据 phase + dazed):\n"
        "     - P1: shy blush, heart eyes\n"
        "     - P2: blush, parted lips, eyes closed\n"
        "     - P3: blush, sweat, parted lips, breast visible\n"
        "     - P4 主动: blush, sweat, parted lips, saliva, hands gripping\n"
        "     - P5 临界: tears, drool, open mouth, rolling eyes, trembling, ahegao incoming\n"
        "     - P6 高潮: {{orgasm}}, {{ahegao}}, {{fucked silly}}, tongue out, squirting, "
        "arched back, {{creampie}}\n"
        "     - P7 overstim: aftershock, trembling, overflow, cum drip, mind break\n"
        "     - P8 余韵: {{afterglow}}, {{cuddling}}, lying down, cum on body, sleepy smile, "
        "soft lighting\n"
        "     - **dazed 时**: {{empty eyes}}, {{unfocused eyes}}, drooling, tongue out\n"
        "  7) **怀孕**: pregnancy_state 命中怀孕时加 `pregnant, slight belly bulge, hand on belly`\n"
        "  8) **body_focus**: 命中时强调对应部位 (cat ears / tail base / inner thigh / neck 等)\n"
        "\n"
        "**输出**: 只输出英文 tags(逗号分隔, 含 {{}} 提权), 不要解释 / 不要 negative / "
        "不要中文 / 不要 markdown / 不要写 1girl/1boy/角色外观。\n"
        "格式: {{2 figures, hetero}}, {{vaginal penetration}}, {{sex}}, doggystyle, on desk, "
        "classroom, school uniform disheveled, blush, sweat, ..."
    )
    user_prompt = (
        "=== 当前场景信息(请严格按此写 caption, 不要瞎想) ===\n"
        f"{scene_block}\n\n"
        "请基于以上场景输出 base caption tags:"
    )

    # 直接调 ai_fallback (DeepSeek 官方 endpoint), 跟 tools._deepseek_imagegen_plan 一脉同源.
    # 走 _post_chat_completion_raw 让 deepseek 直接出 caption tags, 不经过任何 sonnet/relay 中转.
    raw = ""
    try:
        from .openai_client import _post_chat_completion_raw
        timeout = float(
            getattr(config, "catty_ai_fallback_request_timeout", 60.0)
            or getattr(config, "catty_request_timeout", 60.0)
            or 60.0
        )
        proxy = str(getattr(config, "catty_http_proxy", "") or "")
        response = await _post_chat_completion_raw(
            base_url=_fb_base,
            api_key=_fb_key,
            model=_fb_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=timeout,
            proxy=proxy,
            temperature=0.6,
            max_tokens=400,
            extra_headers={},
            extra_body={},
        )
        raw = str(response["choices"][0]["message"]["content"] or "")
        logger.info(
            f"nsfw_imagegen compose: deepseek ({_fb_model}) caption_len={len(raw)} "
            f"phase=P{phase_int} scene_block_len={len(scene_block)}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"nsfw_imagegen compose caption failed: {exc.__class__.__name__}: {exc}")
        return ""

    if not raw:
        return ""
    cleaned = raw.strip()
    for prefix in ("Prompt:", "prompt:", "Tags:", "tags:", "Caption:", "caption:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:]).strip()
    cleaned = cleaned.strip("`'\"").strip()
    cleaned = ", ".join(part.strip() for part in cleaned.splitlines() if part.strip())

    # 强补关键 tag (P1-P3 还没插入, 不强补 penetration; P4-P7 才强补)
    # nsfw / explicit 无论 phase 都必带 (NAI v4.5 不带就走 SFW 滤镜)
    # 主人 2026-05-30 BUG FIX『画面里主人没了』:
    #   reference 锁松开 (1.0 → 0.85/0.7) 后还是要给双人构图加一道语义保险, 让 NAI
    #   明确知道画面里**必须有 1boy 和 1girl 一起**, 不是只画女主.
    lowered = cleaned.lower()
    extra: list[str] = []
    # 双人锁放最前 (NAI v4.5 越靠前权重越大)
    if "1boy with 1girl" not in lowered and "hetero couple" not in lowered:
        extra.append("{{1boy with 1girl}}, {{hetero couple together}}")
    if "2 figures" not in lowered and "hetero" not in lowered:
        extra.append("{{2 figures, hetero}}")
    if "nsfw" not in lowered:
        extra.append("{{nsfw}}")
    if "explicit" not in lowered:
        extra.append("{{explicit}}")
    if 4 <= phase_int <= 7 and not any(k in lowered for k in ("sex", "penetration", "vaginal", "fucked")):
        extra.append("{{vaginal penetration}}, {{sex}}")
    if extra:
        cleaned = ", ".join(extra) + ", " + cleaned
    return cleaned[:700]


# ── 主入口 ──
async def maybe_generate_image(
    *,
    config,
    scope_key: str,
    user_id: str,
    phase_state,
    affection_store,
    current_reply: str = "",
    user_text: str = "",
    recent_messages: list | None = None,
    pregnancy_state=None,
) -> Any:
    """每 3 个 user turn 触发, 走 V4.5 + characterPrompts 双角色锁 + director_reference 生图。

    返回 MessageSegment 或 None。不触发/余额不足/报错都返回 None。
    走到这函数本身就算一个 user turn (调用方 chat handler 在 NSFW spark 成功后才调)。
    phase 只用来让 AI composer 写对应情景的 prompt, 不参与触发判断。

    主人 2026-05-30: 升级 — 调用方需要把 (user_text, recent_messages, pregnancy_state)
    一起传过来, _compose_base_caption 会把场景信息全套丢给 deepseek 让它写对应当下
    场景的 caption (不再瞎默写床上 missionary).
    """
    if not getattr(config, "catty_imagegen_nai_enabled", False):
        return None
    token = str(getattr(config, "catty_imagegen_nai_token", "") or "").strip()
    if not token:
        return None

    turn_count, draws_made = _bump_turn(scope_key, user_id)
    if turn_count % DRAW_INTERVAL != 0:
        return None

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

    # phase 只用来给 composer 决定情景 (P1-P8); None / 取不到默认 P1 (无 penetration 那档)
    phase_int = int(getattr(phase_state, "current_phase", 1) or 1) if phase_state else 1

    # 准备 affection_info (level / is_owner) 给 caption composer
    affection_info: dict | None = None
    if affection_store is not None:
        try:
            _lvl_exp = (
                affection_store.get_level_and_exp(user_id)
                if hasattr(affection_store, "get_level_and_exp") else None
            )
            affection_info = {
                "is_owner": bool(affection_store.is_owner(user_id)),
                "level": int(_lvl_exp[0]) if _lvl_exp else None,
            }
        except Exception:  # noqa: BLE001
            affection_info = None

    # AI 写情景 base caption — 把完整场景信息丢给 deepseek
    composed = await _compose_base_caption(
        config,
        phase_state,
        current_reply,
        user_text=user_text,
        recent_messages=recent_messages,
        pregnancy_state=pregnancy_state,
        affection_info=affection_info,
    )
    base_caption = composed or DEFAULT_BASE_CAPTION
    negative = str(
        getattr(config, "catty_nsfw_imagegen_negative", "") or DEFAULT_NEGATIVE
    ).strip() or DEFAULT_NEGATIVE

    width, height = 832, 1216
    steps = 28

    # Precise Reference: 加载 Miao/ 两张参考图(启动后 cache, 失败则降级到无 reference)
    director_ref_b64_list: list[str] = []
    miao_dir = _find_miao_dir(config)
    if miao_dir is not None:
        for ref_name in ("miaomiao.png", "miaomiaonude.png"):
            ref_path = miao_dir / ref_name
            if not ref_path.is_file():
                continue
            try:
                director_ref_b64_list.append(_get_reference_b64(ref_path))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"nsfw_imagegen: ref {ref_name} load failed: {exc}")

    # characterPrompts 双角色锁: 笨猫 left, 黑影主人 right
    char_girl = CHARACTER_GIRL_TAGS
    char_boy = CHARACTER_BOY_TAGS
    char_boy_uc = CHARACTER_BOY_UC

    payload = {
        "input": base_caption,
        "model": NAI_MODEL,
        "action": "generate",
        "use_new_shared_trial": True,
        "parameters": {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": 5.0,
            "sampler": "k_euler_ancestral",
            "steps": steps,
            "n_samples": 1,
            "seed": int(time.time()) & 0xFFFFFFFF,
            "negative_prompt": negative,
            "ucPreset": 1,
            "qualityToggle": True,
            "sm": False,
            "sm_dyn": False,
            "autoSmea": False,
            "noise_schedule": "karras",
            "dynamic_thresholding": False,
            "cfg_rescale": 0.0,
            "legacy": False,
            "legacy_uc": False,
            "legacy_v3_extend": False,
            "deliberate_euler_ancestral_bug": False,
            "prefer_brownian": True,
            "strength": 0.85,
            "add_original_image": False,
            "controlnet_strength": 1.0,
            "normalize_reference_strength_multiple": False,
            "use_coords": True,
            "v4_prompt": {
                "caption": {
                    "base_caption": base_caption,
                    "char_captions": [
                        {"char_caption": char_girl, "centers": [{"x": 0.3, "y": 0.5}]},
                        {"char_caption": char_boy, "centers": [{"x": 0.7, "y": 0.5}]},
                    ],
                },
                "use_coords": True,
                "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {
                    "base_caption": negative,
                    "char_captions": [
                        {"char_caption": "", "centers": [{"x": 0.3, "y": 0.5}]},
                        {"char_caption": char_boy_uc, "centers": [{"x": 0.7, "y": 0.5}]},
                    ],
                },
                "legacy_uc": False,
            },
            "characterPrompts": [
                {"prompt": char_girl, "uc": "", "center": {"x": 0.3, "y": 0.5}, "enabled": True},
                {"prompt": char_boy, "uc": char_boy_uc, "center": {"x": 0.7, "y": 0.5}, "enabled": True},
            ],
        },
    }

    # Precise Reference (director_reference_*) — 有 reference 才加
    if director_ref_b64_list:
        # 主人 2026-05-30 BUG FIX『画面里主人没了』:
        #   - 旧设 strength=1.0 + information_extracted=1.0 + description="character&style"
        #     把 reference 图(solo 笨猫)的**单人构图**也复制了 → characterPrompts 里的
        #     1boy 黑影主人渲不出来 → 主人在画面里消失.
        #
        # 紧急回滚 (22:11 log 实锤): NAI v4.5 API **硬性要求**
        # director_reference_information_extracted 必须是 EXACTLY 1.0, 改 0.7 直接 400.
        # 改成只调能调的两个维度:
        #   - description "character&style" → "character" (只锁人物, 不锁 style)
        #   - strength_values 1.0 → 0.85 (人物特征还锁, 留余地让 characterPrompts 加第二人)
        #   - secondary_strength_values 0.0 → 0.0 (保持不传 style/构图)
        #   - information_extracted 保持 1.0 (NAI API 硬要求)
        # 剩下的"主人没了"问题靠 caption {{1boy with 1girl}} + 提权负面 solo 解决.
        n = len(director_ref_b64_list)
        payload["parameters"]["director_reference_images"] = director_ref_b64_list
        payload["parameters"]["director_reference_descriptions"] = [
            {
                "caption": {"base_caption": "character", "char_captions": []},
                "legacy_uc": False,
            }
            for _ in range(n)
        ]
        payload["parameters"]["director_reference_strength_values"] = [0.85] * n
        payload["parameters"]["director_reference_secondary_strength_values"] = [0.0] * n
        payload["parameters"]["director_reference_information_extracted"] = [1.0] * n

    # NAI 专用 proxy 优先 (远端国内服务器到 image.novelai.net 真 IP 被墙时填),
    # fallback 全局 proxy, 再 fallback 直连。
    proxy_str = (
        str(getattr(config, "catty_imagegen_nai_http_proxy", "") or "").strip()
        or str(getattr(config, "catty_http_proxy", "") or "").strip()
    )
    timeout = float(getattr(config, "catty_imagegen_nai_timeout_seconds", 180.0) or 180.0)

    # 主人 2026-05-28: Python async SOCKS5 实现都有 30-50% 偶发 ConnectError reset,
    # subprocess curl.exe 100% 稳. 走 curl.exe.
    started = time.monotonic()
    status, body, err = await _curl_post_json(
        url=NAI_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        },
        payload=payload,
        proxy=proxy_str,
        timeout=timeout,
    )
    elapsed = time.monotonic() - started
    if err or status == 0:
        logger.warning(
            f"nsfw_imagegen curl error after {elapsed:.1f}s: {err} (user={user_id}, turn={turn_count})"
        )
        return None
    if status != 200:
        logger.warning(
            f"nsfw_imagegen status={status} elapsed={elapsed:.1f}s "
            f"body={body[:300]!r} (user={user_id}, turn={turn_count})"
        )
        return None

    try:
        zf = zipfile.ZipFile(BytesIO(body))
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

    try:
        from nonebot.adapters.onebot.v11 import MessageSegment
    except ImportError:
        return None
    segment = MessageSegment.image(file=file_path.resolve().as_uri())

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
        f"phase=P{phase_int} turn_count={turn_count} "
        f"draws_made={draws_made + 1} elapsed={elapsed:.1f}s bytes={len(image_bytes)} "
        f"file={file_path.name} {consume_log} "
        f"ai_composed={'yes' if composed else 'no'} caption={base_caption[:200]!r})"
    )
    return segment
