"""像素风积分/签到卡片渲染。

设计:96x120 原始像素 → NEAREST 6x 放大成 576x720 输出。
GBC 风配色 + 5x7 自制像素字模 + 几何心形,完全不依赖外部 TTF 字体,
跨平台跑。

主入口:
- render_card(...) → PIL.Image    通用渲染
- render_card_to_file(...) → Path 写盘并返回路径,供 MessageSegment.image 用
"""
from __future__ import annotations

import hashlib
import random
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw


# ── 调色板 (GBC 复古风) ───────────────────────────────────────────────
BG = (255, 244, 218)        # 米黄背景
DARK = (61, 40, 23)         # 深棕(边框/标题/标签)
LIGHT = (240, 220, 180)     # 浅米黄(分隔)
HEART_FILL = (242, 92, 126)
HEART_EDGE = (180, 50, 90)
WHITE = (255, 255, 255)
ACCENT = (248, 182, 41)     # 星星黄
GREEN = (113, 194, 113)
BLUE = (100, 150, 220)
GRAY = (140, 120, 100)


# ── 5x7 像素字模 ─────────────────────────────────────────────────────
# 每行 5 字符 ("0"=透明,"1"=描点),共 7 行。空格代表透明。
GLYPHS: dict[str, list[str]] = {
    "0": ["01110","10001","10011","10101","11001","10001","01110"],
    "1": ["00100","01100","10100","00100","00100","00100","11111"],
    "2": ["01110","10001","00001","00010","00100","01000","11111"],
    "3": ["11110","00001","00001","01110","00001","00001","11110"],
    "4": ["00010","00110","01010","10010","11111","00010","00010"],
    "5": ["11111","10000","10000","11110","00001","00001","11110"],
    "6": ["01110","10000","10000","11110","10001","10001","01110"],
    "7": ["11111","00001","00010","00010","00100","00100","00100"],
    "8": ["01110","10001","10001","01110","10001","10001","01110"],
    "9": ["01110","10001","10001","01111","00001","00001","01110"],
    "A": ["01110","10001","10001","11111","10001","10001","10001"],
    "B": ["11110","10001","10001","11110","10001","10001","11110"],
    "C": ["01110","10001","10000","10000","10000","10001","01110"],
    "D": ["11110","10001","10001","10001","10001","10001","11110"],
    "E": ["11111","10000","10000","11110","10000","10000","11111"],
    "F": ["11111","10000","10000","11110","10000","10000","10000"],
    "G": ["01110","10001","10000","10111","10001","10001","01110"],
    "H": ["10001","10001","10001","11111","10001","10001","10001"],
    "I": ["11111","00100","00100","00100","00100","00100","11111"],
    "J": ["11111","00010","00010","00010","00010","10010","01100"],
    "K": ["10001","10010","10100","11000","10100","10010","10001"],
    "L": ["10000","10000","10000","10000","10000","10000","11111"],
    "M": ["10001","11011","10101","10101","10001","10001","10001"],
    "N": ["10001","11001","10101","10011","10001","10001","10001"],
    "O": ["01110","10001","10001","10001","10001","10001","01110"],
    "P": ["11110","10001","10001","11110","10000","10000","10000"],
    "Q": ["01110","10001","10001","10001","10101","10010","01101"],
    "R": ["11110","10001","10001","11110","10100","10010","10001"],
    "S": ["01111","10000","10000","01110","00001","00001","11110"],
    "T": ["11111","00100","00100","00100","00100","00100","00100"],
    "U": ["10001","10001","10001","10001","10001","10001","01110"],
    "V": ["10001","10001","10001","10001","10001","01010","00100"],
    "W": ["10001","10001","10001","10101","10101","11011","10001"],
    "X": ["10001","10001","01010","00100","01010","10001","10001"],
    "Y": ["10001","10001","01010","00100","00100","00100","00100"],
    "Z": ["11111","00001","00010","00100","01000","10000","11111"],
    " ": ["00000","00000","00000","00000","00000","00000","00000"],
    ".": ["00000","00000","00000","00000","00000","00000","00100"],
    ",": ["00000","00000","00000","00000","00000","00100","01000"],
    ":": ["00000","00100","00000","00000","00000","00100","00000"],
    "/": ["00001","00010","00010","00100","01000","01000","10000"],
    "-": ["00000","00000","00000","11111","00000","00000","00000"],
    "+": ["00000","00100","00100","11111","00100","00100","00000"],
    "*": ["00000","10101","01110","11111","01110","10101","00000"],
    "<": ["00001","00010","00100","01000","00100","00010","00001"],
    ">": ["10000","01000","00100","00010","00100","01000","10000"],
    "[": ["01110","01000","01000","01000","01000","01000","01110"],
    "]": ["01110","00010","00010","00010","00010","00010","01110"],
    "(": ["00010","00100","01000","01000","01000","00100","00010"],
    ")": ["01000","00100","00010","00010","00010","00100","01000"],
    "%": ["11001","11010","00100","01000","01011","10011","00000"],
    "#": ["01010","01010","11111","01010","11111","01010","01010"],
    "?": ["01110","10001","00001","00010","00100","00000","00100"],
    "!": ["00100","00100","00100","00100","00100","00000","00100"],
    "=": ["00000","00000","11111","00000","11111","00000","00000"],
    "♥": [  # ♥ 小爱心(5x7 装饰用,大爱心走几何方式)
        "01010","11111","11111","11111","01110","00100","00000"
    ],
    "★": [  # ★ 小星星
        "00100","00100","11111","01110","11111","10001","00000"
    ],
}

GLYPH_W = 5
GLYPH_H = 7


def _put_pixel(draw: ImageDraw.ImageDraw, x: int, y: int, color: tuple[int, int, int]) -> None:
    draw.point((x, y), fill=color)


def _draw_text(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, color: tuple[int, int, int],
               *, spacing: int = 1, scale: int = 1) -> int:
    """逐字符渲染到 (x,y)。返回最终 cursor x。scale=2 表示每像素绘成 2x2。"""
    cursor = x
    for ch in text.upper() if ch_should_upper(text) else text:
        glyph = GLYPHS.get(ch) or GLYPHS.get(ch.upper()) or GLYPHS.get(" ")
        if glyph is None:
            cursor += (GLYPH_W + spacing) * scale
            continue
        for row in range(GLYPH_H):
            line = glyph[row]
            for col in range(GLYPH_W):
                if line[col] == "1":
                    if scale == 1:
                        draw.point((cursor + col, y + row), fill=color)
                    else:
                        x0 = cursor + col * scale
                        y0 = y + row * scale
                        draw.rectangle((x0, y0, x0 + scale - 1, y0 + scale - 1), fill=color)
        cursor += (GLYPH_W + spacing) * scale
    return cursor


def ch_should_upper(_text: str) -> bool:
    # GLYPHS 既有大写也有小写映射? 不,只大写。统一转大写就行。
    return True


def _text_width(text: str, *, spacing: int = 1, scale: int = 1) -> int:
    if not text:
        return 0
    return ((GLYPH_W + spacing) * len(text) - spacing) * scale


def _draw_text_centered(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str,
                        color: tuple[int, int, int], *, spacing: int = 1, scale: int = 1) -> None:
    w = _text_width(text, spacing=spacing, scale=scale)
    _draw_text(draw, cx - w // 2, y, text, color, spacing=spacing, scale=scale)


def _draw_border(draw: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int,
                 color: tuple[int, int, int]) -> None:
    # 1px 矩形边框
    draw.rectangle((x0, y0, x1, y1), outline=color)


# 固定像素心形模板 (29 列 x 25 行) — 手工排版,完美对称,带 1px 描边
# 0 = 透明 / 1 = 填充粉红 / 2 = 描边深粉
_HEART_TEMPLATE: list[str] = [
    "00000111110000000111110000000",
    "00011111111100011111111100000",
    "00111111111111111111111110000",
    "01111111111111111111111111000",
    "11111111111111111111111111100",
    "11111111111111111111111111100",
    "11111111111111111111111111100",
    "11111111111111111111111111100",
    "11111111111111111111111111100",
    "11111111111111111111111111100",
    "01111111111111111111111111000",
    "01111111111111111111111111000",
    "00111111111111111111111110000",
    "00011111111111111111111100000",
    "00001111111111111111111000000",
    "00000111111111111111110000000",
    "00000011111111111111100000000",
    "00000001111111111111000000000",
    "00000000111111111110000000000",
    "00000000011111111100000000000",
    "00000000001111111000000000000",
    "00000000000111110000000000000",
    "00000000000011100000000000000",
    "00000000000001000000000000000",
    "00000000000000000000000000000",
]
HEART_TPL_W = len(_HEART_TEMPLATE[0])
HEART_TPL_H = len(_HEART_TEMPLATE)


def _draw_heart(draw: ImageDraw.ImageDraw, cx: int, top_y: int, *,
                radius: int = 12,
                fill: tuple[int, int, int] = HEART_FILL,
                edge: tuple[int, int, int] | None = HEART_EDGE) -> tuple[int, int, int, int]:
    """以 (cx, top_y) 为心形最高点的水平中线。
    用 29x25 固定模板渲染,完美对称。radius 参数兼容旧调用,不影响输出尺寸(由模板决定)。
    """
    x0 = cx - HEART_TPL_W // 2
    y0 = top_y
    for ry, row in enumerate(_HEART_TEMPLATE):
        for rx, c in enumerate(row):
            if c == "1":
                draw.point((x0 + rx, y0 + ry), fill=fill)
            elif c == "2" and edge is not None:
                draw.point((x0 + rx, y0 + ry), fill=edge)
    return (x0, y0, x0 + HEART_TPL_W - 1, y0 + HEART_TPL_H - 1)


def _exp_bar(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int,
             ratio: float, *, fill: tuple[int, int, int] = ACCENT,
             empty: tuple[int, int, int] = LIGHT,
             frame: tuple[int, int, int] = DARK) -> None:
    """像素风进度条:外框 + 填充。"""
    ratio = max(0.0, min(1.0, ratio))
    draw.rectangle((x, y, x + width - 1, y + height - 1), outline=frame, fill=empty)
    inner_w = width - 2
    filled = int(round(inner_w * ratio))
    if filled > 0:
        draw.rectangle((x + 1, y + 1, x + filled, y + height - 2), fill=fill)


# ── 像素猫爪印 ───────────────────────────────────────────────────────
# 9x8 基础模板:4 脚趾(对称等距,2 行)+ 圆形大肉垫(5 行)
# 参考主人提供的萌系猫爪 reference:矮胖 chubby,4 趾对称,大圆肉垫贴底
# scatter 时按 size_mult ∈ {1,2,3} 放大成 9x8 / 18x16 / 27x24,
# 每爪再 0-360° 随机旋转 → 视觉接近上方 29x25 心形,每爪都不一样
_PAW_TEMPLATE: list[str] = [
    ".X.X.X.X.",   # 4 脚趾上 (列 1,3,5,7,完美等距)
    ".X.X.X.X.",   # 4 脚趾下行(脚趾 2 高,饱满感)
    ".........",   # 间隔
    "..XXXXX..",   # 肉垫顶 (5 宽)
    ".XXXXXXX.",   # 肉垫往宽 (7)
    "XXXXXXXXX",   # 肉垫最宽 (9)
    ".XXXXXXX.",   # 肉垫收 (7)
    "..XXXXX..",   # 肉垫底圆 (5)
]
PAW_BASE_W = 9
PAW_BASE_H = 8


# ── 真实图片素材加载(主人给的萌系参考图)─────────────────────────────
# 主人需要把图保存到下面两个路径,代码就用真实图片;不存在则 fallback 到像素模板。
# paws_sheet.png : 9 个爪子的 grid(3x3),自动按等距 cell 切片 + 裁掉透明 padding
# pusheen.png    : 单张猫猫图,贴在卡片右下角
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
PAWS_SHEET_PATH = ASSETS_DIR / "paws_sheet.png"
PUSHEEN_PATH = ASSETS_DIR / "pusheen.png"

_PAW_CROPS_CACHE: list[Image.Image] | None = None
_PAW_CROPS_LOADED = False
_PUSHEEN_CACHE: Image.Image | None = None
_PUSHEEN_LOADED = False


def _load_paw_crops() -> list[Image.Image] | None:
    """加载 paws_sheet.png 并按 3x3 grid 切片成 9 个独立爪子 RGBA 子图。
    每个 cell 自动 getbbox() 去除透明 padding,保留爪子最紧凑的形状。
    """
    global _PAW_CROPS_CACHE, _PAW_CROPS_LOADED
    if _PAW_CROPS_LOADED:
        return _PAW_CROPS_CACHE
    _PAW_CROPS_LOADED = True
    if not PAWS_SHEET_PATH.exists():
        return None
    try:
        sheet = Image.open(PAWS_SHEET_PATH).convert("RGBA")
    except Exception:
        return None
    w, h = sheet.size
    cell_w, cell_h = w // 3, h // 3
    if cell_w <= 0 or cell_h <= 0:
        return None
    crops: list[Image.Image] = []
    for ry in range(3):
        for rx in range(3):
            cell = sheet.crop((rx * cell_w, ry * cell_h,
                               (rx + 1) * cell_w, (ry + 1) * cell_h))
            bbox = cell.getbbox()
            if bbox is None:
                continue
            paw = cell.crop(bbox)
            if paw.size[0] >= 3 and paw.size[1] >= 3:
                crops.append(paw)
    if not crops:
        return None
    _PAW_CROPS_CACHE = crops
    return crops


def _load_pusheen() -> Image.Image | None:
    """加载 pusheen.png(单张猫猫图)+ 裁掉透明 padding。"""
    global _PUSHEEN_CACHE, _PUSHEEN_LOADED
    if _PUSHEEN_LOADED:
        return _PUSHEEN_CACHE
    _PUSHEEN_LOADED = True
    if not PUSHEEN_PATH.exists():
        return None
    try:
        img = Image.open(PUSHEEN_PATH).convert("RGBA")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        _PUSHEEN_CACHE = img
    except Exception:
        return None
    return _PUSHEEN_CACHE


def _pick_random_paw_image(
    *,
    target_w: int,
    color_tint: tuple[int, int, int] | None = None,
    rng: random.Random | None = None,
) -> Image.Image:
    """优先用主人给的真实爪图随机挑一个 resize 到 target_w;
    asset 缺失就 fallback 到像素模板 _PAW_TEMPLATE。
    """
    r = rng or random
    crops = _load_paw_crops()
    if crops:
        paw = r.choice(crops)
        # 等比 resize
        scale = target_w / paw.width
        new_h = max(3, int(round(paw.height * scale)))
        new_w = max(3, int(round(paw.width * scale)))
        return paw.resize((new_w, new_h), resample=Image.Resampling.NEAREST)
    # Fallback:像素模板 + 根据 target_w 选 size_mult
    tint = color_tint or HEART_EDGE
    size_mult = max(1, target_w // PAW_BASE_W)
    return _render_paw_image(size_mult, tint)


def _render_paw_image(size_mult: int, color: tuple[int, int, int]) -> Image.Image:
    """生成单个爪印的 RGBA 子图,透明背景。
    每个 X 像素扩成 size_mult × size_mult 块,保持像素风。
    """
    w = PAW_BASE_W * size_mult
    h = PAW_BASE_H * size_mult
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for ry, row in enumerate(_PAW_TEMPLATE):
        for rx, c in enumerate(row):
            if c == "X":
                x0, y0 = rx * size_mult, ry * size_mult
                d.rectangle((x0, y0, x0 + size_mult - 1, y0 + size_mult - 1), fill=color)
    return img


def _bbox_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _paste_paw_text_aware(
    target: Image.Image,
    paw_img: Image.Image,
    x: int,
    y: int,
    *,
    bg_color: tuple[int, int, int] = BG,
    fade_alpha: int = 95,
) -> None:
    """粘贴爪印 — 印到背景色上完全不透明,印到字/边框等非背景像素上半透明混合。

    主人需求:『印到字体上的部分就半透明』。
    实现:逐像素检查 target 当前颜色,如果不是 BG(米黄背景) → 用 fade_alpha
    (default 95/255 ≈ 37%)与 target 混合,字体仍可读但爪印颜色还在,
    像"猫猫的爪印章按下去但字盖在上面"。
    """
    pw, ph = paw_img.size
    tw, th = target.size
    target_pixels = target.load()
    paw_pixels = paw_img.load()
    fade_ratio = fade_alpha / 255.0
    inv_ratio = 1.0 - fade_ratio
    for py in range(ph):
        for px in range(pw):
            sr, sg, sb, sa = paw_pixels[px, py]
            if sa == 0:
                continue
            tx, ty = x + px, y + py
            if not (0 <= tx < tw and 0 <= ty < th):
                continue
            t_pixel = target_pixels[tx, ty]
            tr, tg, tb = t_pixel[0], t_pixel[1], t_pixel[2]
            if (tr, tg, tb) == bg_color:
                target_pixels[tx, ty] = (sr, sg, sb)
            else:
                nr = int(sr * fade_ratio + tr * inv_ratio)
                ng = int(sg * fade_ratio + tg * inv_ratio)
                nb = int(sb * fade_ratio + tb * inv_ratio)
                target_pixels[tx, ty] = (nr, ng, nb)


def _scatter_paws(
    target: Image.Image,
    *,
    band_y_min: int,
    band_y_max: int,
    band_x_min: int,
    band_x_max: int,
    color: tuple[int, int, int] = HEART_EDGE,
    count_range: tuple[int, int] = (1, 3),
    target_widths: tuple[int, ...] = (8, 14, 22),
    forbidden_boxes: list[tuple[int, int, int, int]] | None = None,
    rng: random.Random | None = None,
) -> int:
    """在指定 band 内散布 1-3 个爪印,优先用主人给的真实图(随机挑一种),
    asset 缺失则 fallback 到像素模板。每个 size 不同 + 0-360° 随机旋转。

    - target_widths: 每爪从这里随机选一个目标宽度 → 大小各异
    - forbidden_boxes: 爪印不能盖到的 bbox(例:Pusheen 猫猫位置)
    - text-aware paste:背景不透、字/边框/已粘贴的猫上半透
    """
    r = rng or random
    if band_y_max < band_y_min or band_x_max < band_x_min:
        return 0
    forbidden = list(forbidden_boxes or [])
    n = r.randint(*count_range)
    placed_boxes: list[tuple[int, int, int, int]] = []
    used_widths: list[int] = []
    placed = 0
    for _ in range(n):
        # 优先选没用过的 width → 大小都不同
        remaining = [w for w in target_widths if w not in used_widths]
        target_w = r.choice(remaining or list(target_widths))
        angle = r.uniform(0.0, 360.0)
        paw = _pick_random_paw_image(target_w=target_w, color_tint=color, rng=r)
        rotated = paw.rotate(angle, resample=Image.Resampling.NEAREST, expand=True)
        rw, rh = rotated.size
        max_x = band_x_max - rw
        max_y = band_y_max - rh
        if max_x < band_x_min or max_y < band_y_min:
            # 太大放不下,降到更小的 width 再试
            for fallback_w in sorted(target_widths):
                if fallback_w >= target_w:
                    continue
                paw = _pick_random_paw_image(target_w=fallback_w, color_tint=color, rng=r)
                rotated = paw.rotate(angle, resample=Image.Resampling.NEAREST, expand=True)
                rw, rh = rotated.size
                if rw <= (band_x_max - band_x_min) and rh <= (band_y_max - band_y_min):
                    target_w = fallback_w
                    max_x = band_x_max - rw
                    max_y = band_y_max - rh
                    break
            else:
                continue
        # 反重叠:不能跟已放的爪印 或 forbidden_boxes(猫猫) 重叠
        for _try in range(20):
            x = r.randint(band_x_min, max_x)
            y = r.randint(band_y_min, max_y)
            box = (x, y, x + rw, y + rh)
            if any(_bbox_overlap(box, b) for b in placed_boxes):
                continue
            if any(_bbox_overlap(box, b) for b in forbidden):
                continue
            _paste_paw_text_aware(target, rotated, x, y)
            placed_boxes.append(box)
            used_widths.append(target_w)
            placed += 1
            break
    return placed


def _paste_pusheen_bottom_right(
    target: Image.Image,
    *,
    footer_top: int,
    canvas_w: int,
    target_w: int = 26,
    margin_x: int = 4,
    margin_y: int = 1,
) -> tuple[int, int, int, int] | None:
    """加载 Pusheen 图,贴右下角(刚好贴在 footer 上沿),返回 bbox 给 scatter avoid。
    asset 缺失返回 None,不放猫。
    """
    pusheen = _load_pusheen()
    if pusheen is None:
        return None
    if target_w < 6 or pusheen.width <= 0:
        return None
    aspect = pusheen.height / pusheen.width
    new_w = target_w
    new_h = max(6, int(round(new_w * aspect)))
    resized = pusheen.resize((new_w, new_h), resample=Image.Resampling.NEAREST)
    x = canvas_w - margin_x - new_w
    y = footer_top - margin_y - new_h
    if x < 0 or y < 0:
        return None
    target.paste(resized, (x, y), resized)
    return (x, y, x + new_w, y + new_h)


# ── 渲染主函数 ───────────────────────────────────────────────────────

CANVAS_W = 96
CANVAS_H = 120
SCALE = 6  # NEAREST 放大倍数,输出 576 x 720


def render_card(
    *,
    title: str,
    level: int,
    points: int,
    exp_current: int = 0,
    exp_next_level: int | None = None,
    is_owner: bool = False,
    checked_in_today: bool = False,
    last_amount: int = 0,
    today_gained: int | None = None,   # 本次签到得分(签到模式才传)
    mode: str = "summary",              # "summary" 或 "signin"
) -> Image.Image:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(img)

    # 外边框 + 内描线(双层框看起来更像素卡)
    _draw_border(draw, 0, 0, CANVAS_W - 1, CANVAS_H - 1, DARK)
    _draw_border(draw, 2, 2, CANVAS_W - 3, CANVAS_H - 3, DARK)

    # 标题条背景 + 文字
    draw.rectangle((3, 3, CANVAS_W - 4, 13), fill=DARK)
    title_text = title[:14]
    _draw_text_centered(draw, CANVAS_W // 2, 5, title_text, BG, scale=1)

    # 心形 + 等级数字(白色高对比,居中放心形视觉重心)
    heart_top_y = 16
    heart_bbox = _draw_heart(draw, CANVAS_W // 2, heart_top_y)
    if is_owner:
        lv_text = "MAX"
        lv_scale = 1
    elif level >= 10:
        lv_text = str(level)
        lv_scale = 1  # 双位数不放大,避免撑出心形
    else:
        lv_text = str(level)
        lv_scale = 2
    glyph_h_scaled = GLYPH_H * lv_scale
    # 模板视觉中心 y ≈ top_y + 9(两圆中心高)。数字居中放在这附近
    heart_visual_cy = heart_top_y + 9
    text_y = heart_visual_cy - glyph_h_scaled // 2
    _draw_text_centered(draw, CANVAS_W // 2, text_y, lv_text, WHITE, scale=lv_scale)

    # LEVEL 标签紧贴心形下沿
    heart_bottom_y = heart_bbox[3]
    lv_label_y = heart_bottom_y + 2
    _draw_text_centered(draw, CANVAS_W // 2, lv_label_y, "LEVEL", DARK, scale=1)

    # 分隔虚线
    sep_y = lv_label_y + GLYPH_H + 2
    for x in range(6, CANVAS_W - 6, 3):
        draw.point((x, sep_y), fill=GRAY)

    # POINTS 标签 + 大数字
    points_label_y = sep_y + 3
    _draw_text_centered(draw, CANVAS_W // 2, points_label_y, "POINTS", DARK, scale=1)

    points_text = "INF" if is_owner else str(points)
    big_y = points_label_y + GLYPH_H + 1
    _draw_text_centered(draw, CANVAS_W // 2, big_y, points_text,
                        ACCENT if not is_owner else GREEN, scale=2)

    # 底栏背景
    footer_top = CANVAS_H - 14
    draw.rectangle((3, footer_top, CANVAS_W - 4, CANVAS_H - 4), fill=DARK)

    # 1) 先放 Pusheen 到右下角(贴 footer 上沿)
    pusheen_box = _paste_pusheen_bottom_right(
        img, footer_top=footer_top, canvas_w=CANVAS_W, target_w=26,
    )

    # 2) 再散爪印:band 放宽到包含 POINTS 大数字 + 下方空白带
    # 1-3 个,每爪 target_width 不同(8/14/22 → 旋转后接近爱心 29x25),
    # 0-360° 旋转;优先用主人给的真实图(assets/paws_sheet.png 缺失则 fallback
    # 像素模板)。爪 bbox 避开 Pusheen,所以"不会盖到猫猫身上"。
    # text-aware paste 让盖到字上的部分半透,像猫猫盖印章。
    paw_band_top = points_label_y + GLYPH_H + 1
    paw_band_bot = footer_top - 1
    paw_band_x_min = 4
    paw_band_x_max = CANVAS_W - 4
    if paw_band_bot > paw_band_top and paw_band_x_max > paw_band_x_min:
        _scatter_paws(
            img,
            band_y_min=paw_band_top,
            band_y_max=paw_band_bot,
            band_x_min=paw_band_x_min,
            band_x_max=paw_band_x_max,
            color=HEART_EDGE,  # fallback 像素模板时用
            count_range=(1, 3),
            target_widths=(8, 14, 22),
            forbidden_boxes=[pusheen_box] if pusheen_box else None,
        )

    # ── 底栏内容 ─────────────────────────────────────────────
    if mode == "signin":
        if is_owner:
            line = "OWNER MAX"
        else:
            line = f"+{today_gained or 0} GOT IT"
        _draw_text_centered(draw, CANVAS_W // 2, footer_top + 3, line, BG, scale=1)
    else:
        if is_owner or exp_next_level is None or exp_next_level <= 0:
            tip = "OWNER MAX" if is_owner else "MAX LV"
            _draw_text_centered(draw, CANVAS_W // 2, footer_top + 3, tip, BG, scale=1)
        else:
            # 上一行 EXP 数字
            _draw_text_centered(
                draw, CANVAS_W // 2, footer_top + 1,
                f"EXP {exp_current}/{exp_next_level}", BG, scale=1
            )
            status_y = footer_top + 1 + GLYPH_H + 1
            if status_y + GLYPH_H <= CANVAS_H - 4:
                status_txt = "DAILY DONE" if checked_in_today else "GET DAILY!"
                color = GREEN if checked_in_today else ACCENT
                _draw_text_centered(draw, CANVAS_W // 2, status_y, status_txt, color, scale=1)

    # 角落像素星星点缀
    _draw_text(draw, 5, 4, "*", ACCENT, scale=1)
    _draw_text(draw, CANVAS_W - 10, 4, "*", ACCENT, scale=1)

    return img.resize((CANVAS_W * SCALE, CANVAS_H * SCALE), Image.Resampling.NEAREST)


def render_card_to_file(
    *,
    output_dir: Path,
    user_id: str,
    title: str,
    level: int,
    points: int,
    exp_current: int = 0,
    exp_next_level: int | None = None,
    is_owner: bool = False,
    checked_in_today: bool = False,
    last_amount: int = 0,
    today_gained: int | None = None,
    mode: str = "summary",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    img = render_card(
        title=title, level=level, points=points,
        exp_current=exp_current, exp_next_level=exp_next_level,
        is_owner=is_owner, checked_in_today=checked_in_today,
        last_amount=last_amount, today_gained=today_gained, mode=mode,
    )
    # 文件名:user_id + 时间戳哈希,避免缓存冲撞
    stamp = f"{user_id}_{mode}_{int(time.time())}"
    digest = hashlib.md5(stamp.encode("utf-8")).hexdigest()[:10]
    out = output_dir / f"affection_{user_id}_{mode}_{digest}.png"
    img.save(out, format="PNG", optimize=True)
    return out


def prune_cards(output_dir: Path, max_files: int = 200) -> None:
    """LRU 清理:只保留最近 max_files 张。"""
    if not output_dir.is_dir():
        return
    try:
        files = sorted(
            (p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return
    overflow = len(files) - max_files
    for stale in files[:overflow] if overflow > 0 else []:
        try:
            stale.unlink()
        except OSError:
            continue
