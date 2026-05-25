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


# ── 高分辨率 paw / Pusheen(原图分辨率 + alpha threshold 削彩边) ─────────
# 关键:LANCZOS 缩小和 NEAREST 旋转都会产生半透明 fringe 像素,paste 后看起来"花"。
# 修复:每步 resize/rotate 后用 _alpha_threshold 把 alpha 二值化(全透 or 全不透),
# 彻底消除彩色 fringe。这样 360° 任意角度旋转都干净。
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
PAWS_SHEET_PATH = ASSETS_DIR / "paws_sheet.png"
PUSHEEN_PATH = ASSETS_DIR / "pusheen.png"

_PAW_CROPS_CACHE: list[Image.Image] | None = None
_PUSHEEN_CACHE: Image.Image | None = None


def _bbox_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _alpha_threshold(img: Image.Image, threshold: int = 128) -> Image.Image:
    """硬 alpha:像素 alpha < threshold → 完全透明,≥ threshold → 完全不透。
    消除 LANCZOS / NEAREST rotate 产生的半透明 fringe(那些半透像素 paste 后
    跟卡片背景混色,看起来就是"花"的彩噪点)。
    """
    if img.mode != "RGBA":
        return img
    img = img.copy()
    pixels = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = pixels[x, y]
            pixels[x, y] = (r, g, b, 255 if a >= threshold else 0)
    return img


def _load_paw_crops() -> list[Image.Image]:
    """从 paws_sheet.png 用连通组件算法切出独立爪子(不依赖 grid 等距假设)。

    旧版 3x3 等距切 + getbbox 假设各爪居中且 cell 间无渗入,主人参考图实际
    grid 不严格等距 / 个别 cell 边界处描边渗入隔壁,导致切错。
    新版用 scipy.ndimage.label 在 alpha mask 上找所有连通区,按面积排序取前
    9 个,每个紧贴非透明像素 bbox 裁出,与 grid 位置无关。
    """
    global _PAW_CROPS_CACHE
    if _PAW_CROPS_CACHE is not None:
        return _PAW_CROPS_CACHE
    if not PAWS_SHEET_PATH.exists():
        _PAW_CROPS_CACHE = []
        return []
    try:
        sheet = Image.open(PAWS_SHEET_PATH).convert("RGBA")
    except Exception:
        _PAW_CROPS_CACHE = []
        return []
    sheet = _alpha_threshold(sheet, 128)
    try:
        import numpy as np
        from scipy import ndimage
    except ImportError:
        # scipy 不可用 → 回退到老的 grid 切片
        _PAW_CROPS_CACHE = _load_paw_crops_grid_fallback(sheet)
        return _PAW_CROPS_CACHE

    alpha = np.array(sheet.split()[-1])
    mask = alpha > 128
    # 关键:先 dilation 5 像素让脚趾跟主肉垫连成一片(参考图里它们隔 2-4 像素),
    # 在膨胀后的 mask 上 label,这样每个『爪整体(肉垫+所有脚趾)』算一个 component。
    # bbox 还是从原 mask 取,只是组件归属用 dilated label。
    dilated = ndimage.binary_dilation(mask, iterations=5)
    labeled, n_comp = ndimage.label(dilated)
    # 每个膨胀组件 → 它包含的原 mask 像素的 bbox
    candidates: list[tuple[int, tuple[int, int, int, int]]] = []
    for i in range(1, n_comp + 1):
        region_in_orig = (labeled == i) & mask
        ys, xs = np.where(region_in_orig)
        if len(xs) < 50:  # 真噪点过滤
            continue
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        # 原 mask 像素数作面积权重
        candidates.append((int(region_in_orig.sum()), (x0, y0, x1, y1)))
    candidates.sort(key=lambda kv: -kv[0])
    crops: list[Image.Image] = []
    for area, box in candidates[:9]:
        paw = sheet.crop(box)
        if paw.width >= 3 and paw.height >= 3:
            crops.append(paw)
    _PAW_CROPS_CACHE = crops
    return crops


def _load_paw_crops_grid_fallback(sheet: Image.Image) -> list[Image.Image]:
    """scipy 不可用时的 fallback:3x3 grid 等距切。"""
    w, h = sheet.size
    cell_w, cell_h = w // 3, h // 3
    pad_x, pad_y = max(1, cell_w // 8), max(1, cell_h // 8)
    crops: list[Image.Image] = []
    for ry in range(3):
        for rx in range(3):
            cell = sheet.crop((
                rx * cell_w + pad_x, ry * cell_h + pad_y,
                (rx + 1) * cell_w - pad_x, (ry + 1) * cell_h - pad_y,
            ))
            bbox = cell.getbbox()
            if bbox is None:
                continue
            paw = cell.crop(bbox)
            if paw.width >= 3 and paw.height >= 3:
                crops.append(paw)
    return crops


def _load_pusheen() -> Image.Image | None:
    """加载 Pusheen 原图,floodfill 抠掉网格背景,alpha threshold 清边。"""
    global _PUSHEEN_CACHE
    if _PUSHEEN_CACHE is not None:
        return _PUSHEEN_CACHE
    if not PUSHEEN_PATH.exists():
        return None
    try:
        img = Image.open(PUSHEEN_PATH).convert("RGBA")
        for corner in ((0, 0), (img.width - 1, 0),
                       (0, img.height - 1), (img.width - 1, img.height - 1)):
            try:
                ImageDraw.floodfill(img, corner, (0, 0, 0, 0), thresh=40)
            except Exception:
                pass
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        _PUSHEEN_CACHE = _alpha_threshold(img, 128)
    except Exception:
        return None
    return _PUSHEEN_CACHE


def _resize_rgba_sharp_alpha(
    img: Image.Image, new_w: int, new_h: int,
) -> Image.Image:
    """RGBA 全通道 NEAREST 缩放 — 保持像素艺术风格。

    原图本身是像素艺术(肉垫色块 + 外圈描边),LANCZOS 会把这种"块状色"插值成
    渐变,违背风格。NEAREST 直接采样最近像素,放大缩小都保持每个像素的纯色,
    符合 catty 卡片字 / 心形的同一像素风。
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    return img.resize((new_w, new_h), Image.Resampling.NEAREST)


def _prepare_paw_rotated(
    paw: Image.Image, target_w: int, angle: float,
) -> Image.Image:
    """爪图 resize → rotate → alpha threshold:零 fringe + 高分辨率细节。

    - 先 alpha threshold(crops 加载时已 threshold,但保险)
    - resize 用 _resize_rgba_sharp_alpha(RGB LANCZOS + alpha NEAREST)避免棕框
    - rotate 用 NEAREST(整数像素旋转,无色彩 anti-aliasing)
    - rotate 后再 alpha threshold 一次清最后边缘
    """
    paw = _alpha_threshold(paw, 128)
    if paw.width != target_w:
        scale = target_w / paw.width
        new_h = max(8, int(round(paw.height * scale)))
        paw = _resize_rgba_sharp_alpha(paw, target_w, new_h)
        paw = _alpha_threshold(paw, 128)
    rotated = paw.rotate(angle, resample=Image.Resampling.NEAREST, expand=True)
    return _alpha_threshold(rotated, 128)


def _paste_text_aware(
    target: Image.Image, src: Image.Image, x: int, y: int,
    *, bg_color: tuple[int, int, int] = BG, fade_on_text: int = 110,
) -> None:
    """逐像素 paste:src alpha=0 跳过;target 是 BG → 完全 paste;
    target 是字/边框/已贴元素 → fade_on_text/255 比例混合(主人要求"印到字上半透明")。
    """
    pw, ph = src.size
    tw, th = target.size
    sp = src.load()
    tp = target.load()
    fade = fade_on_text / 255.0
    inv = 1.0 - fade
    for py in range(ph):
        for px in range(pw):
            sr, sg, sb, sa = sp[px, py]
            if sa < 128:
                continue
            tx, ty = x + px, y + py
            if not (0 <= tx < tw and 0 <= ty < th):
                continue
            t = tp[tx, ty]
            tr, tg, tb = t[0], t[1], t[2]
            if (tr, tg, tb) == bg_color:
                tp[tx, ty] = (sr, sg, sb)
            else:
                tp[tx, ty] = (
                    int(sr * fade + tr * inv),
                    int(sg * fade + tg * inv),
                    int(sb * fade + tb * inv),
                )


def _scatter_paws(
    target: Image.Image,
    *,
    band_y_min: int,
    band_y_max: int,
    band_x_min: int,
    band_x_max: int,
    count_range: tuple[int, int] = (1, 3),
    target_widths: tuple[int, ...] = (60, 100, 140),
    forbidden_boxes: list[tuple[int, int, int, int]] | None = None,
    rng: random.Random | None = None,
) -> int:
    """大画布上散 1-3 个高分辨率原图爪:
    - target_widths: 大画布像素(默认 60/100/140,接近原图分辨率)
    - 360° 完全随机旋转(NEAREST + alpha threshold,零 fringe)
    - 9 种 paw 随机挑,size 优先不重复
    - 反 bbox 重叠 + 避开 forbidden_boxes (Pusheen)
    - text-aware paste:盖到字上半透明
    """
    r = rng or random
    crops = _load_paw_crops()
    if not crops or band_y_max < band_y_min or band_x_max < band_x_min:
        return 0
    forbidden = list(forbidden_boxes or [])
    n = r.randint(*count_range)
    placed_boxes: list[tuple[int, int, int, int]] = []
    used_widths: list[int] = []
    placed = 0
    for _ in range(n):
        remaining = [w for w in target_widths if w not in used_widths]
        target_w = r.choice(remaining or list(target_widths))
        angle = r.uniform(0.0, 360.0)
        paw = r.choice(crops)
        rotated = _prepare_paw_rotated(paw, target_w, angle)
        rw, rh = rotated.size
        max_x = band_x_max - rw
        max_y = band_y_max - rh
        if max_x < band_x_min or max_y < band_y_min:
            # 太大,降到更小 target_w 再试
            for fw in sorted(target_widths):
                if fw >= target_w:
                    continue
                rotated = _prepare_paw_rotated(paw, fw, angle)
                rw, rh = rotated.size
                if rw <= band_x_max - band_x_min and rh <= band_y_max - band_y_min:
                    target_w = fw
                    max_x = band_x_max - rw
                    max_y = band_y_max - rh
                    break
            else:
                continue
        # 反重叠 retry
        for _try in range(20):
            x = r.randint(band_x_min, max_x)
            y = r.randint(band_y_min, max_y)
            box = (x, y, x + rw, y + rh)
            if any(_bbox_overlap(box, b) for b in placed_boxes):
                continue
            if any(_bbox_overlap(box, b) for b in forbidden):
                continue
            _paste_text_aware(target, rotated, x, y)
            placed_boxes.append(box)
            used_widths.append(target_w)
            placed += 1
            break
    return placed


def _paste_pusheen_bottom_right(
    target: Image.Image,
    *,
    footer_top_sm: int,
    canvas_w_sm: int,
    target_w_large: int = 150,
    margin_x_sm: int = 4,
    margin_y_sm: int = 1,
    scale: int = 6,
) -> tuple[int, int, int, int] | None:
    """大画布上贴 Pusheen 右下角(footer 上沿)。坐标参数是小画布坐标(×scale)。
    target_w_large 直接是大画布像素;LANCZOS 缩小 + alpha threshold 削彩边。
    """
    pusheen = _load_pusheen()
    if pusheen is None or pusheen.width <= 0:
        return None
    aspect = pusheen.height / pusheen.width
    new_w = target_w_large
    new_h = max(8, int(round(new_w * aspect)))
    if new_w != pusheen.width:
        resized = _resize_rgba_sharp_alpha(pusheen, new_w, new_h)
        resized = _alpha_threshold(resized, 128)
    else:
        resized = pusheen
    x = canvas_w_sm * scale - margin_x_sm * scale - new_w
    y = footer_top_sm * scale - margin_y_sm * scale - new_h
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

    # paw + Pusheen 等大画布渲染完之后再画底栏文字(在大画布上)
    # 这里只占位 — 实际 paste 在最后 NEAREST 放大后做
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

    # NEAREST 6x 放大小画布 → 大画布上 paste 接近原图分辨率的 paw + Pusheen
    # (避免在小画布上 paste 导致分辨率丢失)
    large = img.resize((CANVAS_W * SCALE, CANVAS_H * SCALE),
                       Image.Resampling.NEAREST)
    pusheen_box = _paste_pusheen_bottom_right(
        large, footer_top_sm=footer_top, canvas_w_sm=CANVAS_W,
        target_w_large=150, scale=SCALE,
    )
    paw_band_top = (lv_label_y + GLYPH_H + 1) * SCALE
    paw_band_bot = (footer_top - 1) * SCALE
    paw_band_x_min = 4 * SCALE
    paw_band_x_max = (CANVAS_W - 4) * SCALE
    if paw_band_bot > paw_band_top and paw_band_x_max > paw_band_x_min:
        _scatter_paws(
            large,
            band_y_min=paw_band_top, band_y_max=paw_band_bot,
            band_x_min=paw_band_x_min, band_x_max=paw_band_x_max,
            count_range=(1, 3),
            target_widths=(60, 100, 140),  # 大画布像素,接近原图分辨率
            forbidden_boxes=[pusheen_box] if pusheen_box else None,
        )
    return large


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
