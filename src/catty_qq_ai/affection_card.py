"""像素风积分/签到卡片渲染。

设计:96x120 原始像素 → NEAREST 6x 放大成 576x720 输出。
GBC 风配色 + 5x7 自制像素字模 + 几何心形,完全不依赖外部 TTF 字体,
跨平台跑。

GIF 动画(主人需求):24 帧 / 12 fps / 2s loop,首尾帧周期对齐:
- 心形跳动(等级越高跳越快/越大)
- Pusheen 呼吸(等级越高呼吸越快/越开心)
- 3 个爪子上下浮动(相位错开,漂浮感)
- 头顶冒♥/★粒子,飘高变大渐淡消失(等级越高 spawn 越频繁)

主入口:
- render_card(...) → PIL.Image    单帧静态渲染(GIF 首帧,fallback 用)
- render_card_to_file(...) → Path 写盘 GIF,供 MessageSegment.image 用
"""
from __future__ import annotations

import hashlib
import math
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


def _fmt_compact(n: int) -> str:
    """像素卡数字紧凑显示:1234 → 1.2K,12345 → 12K,1234567 → 1.2M。

    - < 1000:原样
    - 1000~999999:K 单位(< 10 保留 1 位小数且去末尾 .0,>= 10 取整)
    - 1000000+:M 单位(规则同上)
    """
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v, unit = n / 1000.0, "K"
    else:
        v, unit = n / 1_000_000.0, "M"
    if v < 10:
        s = f"{v:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return f"{s}{unit}"
    return f"{int(v)}{unit}"


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

# cache 是 module 级,bot 进程启动时填一次;升级 scipy/numpy 后需重启 bot
# 让本 module 重新 import,这两个 cache 才会被新算法重建
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
    # 关键:先 dilation 让脚趾跟主肉垫连成一片(参考图小爪脚趾隔得远)。
    # iter 10 够把所有 paw 的脚趾跟肉垫粘成单一连通区,bbox 还从原 mask 取,
    # 只是组件归属用 dilated label。
    dilated = ndimage.binary_dilation(mask, iterations=10)
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
    count: int = 3,
    target_widths: tuple[int, ...] = (60, 90, 130),
    forbidden_boxes: list[tuple[int, int, int, int]] | None = None,
    rng: random.Random | None = None,
    angle_range: tuple[float, float] = (-45.0, 15.0),
    scatter_zone_x_ratio: float = 0.62,
    scatter_zone_y_ratio: float = 0.55,
) -> int:
    """『左下角散开 3 爪』布局 — 主人最新需求:
    - count 默认 3,size 优先不重复(3 档)
    - 区域限定:band 左下 ~62% 宽 × ~55% 高(右上让位给 Pusheen + INF 数字)
    - 角度随机 [-45°, +15°]:PIL 负角=顺时针(向右倾) / 正角=逆时针(向左倾)
      → 总体朝右上方,但每爪角度自由偏差,看着自然不机械
    - bbox 互不重叠(主人要求"脚印不要重叠")
    - 避开 Pusheen forbidden_box
    - text-aware paste:盖到字/边框上的像素半透
    """
    r = rng or random
    crops = _load_paw_crops()
    if not crops or band_y_max < band_y_min or band_x_max < band_x_min:
        return 0
    forbidden = list(forbidden_boxes or [])
    # 限定左下角散布区
    bw = band_x_max - band_x_min
    bh = band_y_max - band_y_min
    scatter_x_max = band_x_min + int(bw * scatter_zone_x_ratio)
    scatter_y_min = band_y_min + int(bh * (1.0 - scatter_zone_y_ratio))
    scatter_x_min = band_x_min
    scatter_y_max = band_y_max
    placed_boxes: list[tuple[int, int, int, int]] = []
    used_widths: list[int] = []
    placed = 0
    for _ in range(count):
        # size 优先选未用过的 → 大小各异
        remaining = [w for w in target_widths if w not in used_widths]
        tw = r.choice(remaining or list(target_widths))
        # 角度随机
        angle = r.uniform(*angle_range)
        # 试不同 paw + 位置组合,直到放下不重叠
        placed_here = False
        for outer in range(8):
            paw = r.choice(crops)
            rotated = _prepare_paw_rotated(paw, tw, angle)
            rw, rh = rotated.size
            max_x = scatter_x_max - rw
            max_y = scatter_y_max - rh
            # 太大就降到下一档 size 再试
            if max_x < scatter_x_min or max_y < scatter_y_min:
                for fw in sorted(target_widths):
                    if fw >= tw:
                        continue
                    rotated = _prepare_paw_rotated(paw, fw, angle)
                    rw, rh = rotated.size
                    if (scatter_x_max - rw >= scatter_x_min
                            and scatter_y_max - rh >= scatter_y_min):
                        tw = fw
                        max_x = scatter_x_max - rw
                        max_y = scatter_y_max - rh
                        break
                else:
                    continue
            for _try in range(20):
                x = r.randint(scatter_x_min, max_x)
                y = r.randint(scatter_y_min, max_y)
                box = (x, y, x + rw, y + rh)
                if any(_bbox_overlap(box, b) for b in placed_boxes):
                    continue
                if any(_bbox_overlap(box, b) for b in forbidden):
                    continue
                _paste_text_aware(target, rotated, x, y)
                placed_boxes.append(box)
                used_widths.append(tw)
                placed += 1
                placed_here = True
                break
            if placed_here:
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


# ── GIF 动画参数 ─────────────────────────────────────────────────────
# 主人选 24 帧 / 12 fps / 克制型;周期 = 1.92s,首尾帧能自然衔接 loop。
GIF_TOTAL_FRAMES = 24
GIF_DURATION_MS = 80  # 12 fps


def _heart_pulse_scale(frame_idx: int, level: int) -> float:
    """心跳 scale。等级越高 period 越短(跳得越快)+ amplitude 越大。

    克制档:Lv1 周期 14 帧(0.86Hz) amp 0.04 → 1.00~1.04
            Lv10 周期 6 帧(2.0Hz)   amp 0.12 → 1.00~1.12
    """
    lv = max(1, min(level, 10))
    period = 14 - (lv - 1) * 8 / 9.0
    amp = 0.04 + (lv - 1) * 0.009
    phase = 2.0 * math.pi * frame_idx / period
    # (1+sin)/2 保证 scale ≥ 1.0(心只放大不缩小)
    return 1.0 + amp * (1.0 + math.sin(phase)) / 2.0


def _pusheen_breathe_scale(frame_idx: int, level: int) -> tuple[float, float]:
    """Pusheen 呼吸 (w_scale, h_scale)。吸气高+略瘦,呼气低+略扁(体积守恒)。

    克制档:Lv1 周期 24 帧 amp 0.05;Lv10 周期 12 帧 amp 0.10。
    """
    lv = max(1, min(level, 10))
    period = 24 - (lv - 1) * 12 / 9.0
    amp = 0.05 + (lv - 1) * 0.005
    phase = 2.0 * math.pi * frame_idx / period
    h = 1.0 + amp * math.sin(phase)
    w = 1.0 - amp * math.sin(phase) * 0.4
    return w, h


def _paw_float_offset_sm(frame_idx: int, paw_index: int) -> int:
    """爪子上下浮动 y_offset(小画布像素,±2 → 大画布 ±12 px)。
    3 个爪子相位错开(120° / 0.33),漂浮感不机械。
    """
    phase = 2.0 * math.pi * (frame_idx / GIF_TOTAL_FRAMES + paw_index * 0.33)
    return int(round(2.0 * math.sin(phase)))


def _particle_spawn_period(level: int) -> int:
    """粒子 spawn 周期(帧)。Lv1=18,Lv10=6(克制档)。"""
    lv = max(1, min(level, 10))
    return max(6, 18 - (lv - 1) * 12 // 9)


PARTICLE_LIFETIME = 18  # 帧 (1.5s @ 12fps)
PARTICLE_VY_LARGE = 5   # 每帧上升 px(大画布)


def _particles_for_frame(
    frame_idx: int, level: int, spawn_anchor_lg: tuple[int, int],
    layout_seed: int,
) -> list[tuple[str, tuple[int, int, int], int, int, int, float]]:
    """确定性算出当前帧所有 active 粒子,(char, color, x_lg, y_lg, draw_scale, alpha)。

    粒子在每 _particle_spawn_period(level) 帧生成,生命 PARTICLE_LIFETIME 帧。
    deterministic:用 (layout_seed + birth_frame) 当 RNG seed,GIF 重生成参数一致。
    """
    period = _particle_spawn_period(level)
    out: list[tuple[str, tuple[int, int, int], int, int, int, float]] = []
    for age in range(PARTICLE_LIFETIME):
        birth_frame = frame_idx - age
        # 让粒子在所有 birth_frame % period == 0 时刻生成(允许 birth_frame<0,GIF loop 头)
        if birth_frame % period != 0:
            continue
        prng = random.Random(layout_seed * 977 + birth_frame * 31 + 13)
        x_off = prng.randint(-22, 38)  # 头部上方偏右
        choice = prng.random()
        if choice < 0.55:
            char, color = "♥", HEART_FILL
        elif choice < 0.85:
            char, color = "★", ACCENT
        else:
            char, color = "♥", (255, 196, 92)  # 暖黄心
        draw_scale = 1 + min(2, age // 6)  # 1→2→3 渐大
        if age < 12:
            alpha = 1.0
        else:
            alpha = max(0.0, 1.0 - (age - 12) / 6.0)
        x = spawn_anchor_lg[0] + x_off
        y = spawn_anchor_lg[1] - PARTICLE_VY_LARGE * age
        out.append((char, color, x, y, draw_scale, alpha))
    return out


def _draw_particle_at(
    target: Image.Image, char: str, color: tuple[int, int, int],
    x: int, y: int, scale: int, alpha: float,
) -> None:
    """像素粒子绘制 — 用 GLYPHS 5x7 字模,alpha 用 BG blend 模拟。"""
    glyph = GLYPHS.get(char) or GLYPHS.get(char.upper())
    if glyph is None or alpha <= 0.01:
        return
    r, g, b = color
    bgr, bgg, bgb = BG
    a = max(0.0, min(1.0, alpha))
    fill = (
        int(r * a + bgr * (1 - a)),
        int(g * a + bgg * (1 - a)),
        int(b * a + bgb * (1 - a)),
    )
    draw = ImageDraw.Draw(target)
    tw, th = target.size
    for ry in range(GLYPH_H):
        line = glyph[ry]
        for rx in range(GLYPH_W):
            if line[rx] != "1":
                continue
            px = x + rx * scale
            py = y + ry * scale
            if 0 <= px < tw and 0 <= py < th:
                draw.rectangle((px, py, px + scale - 1, py + scale - 1), fill=fill)


# ── 心形 sprite(可任意 scale 缩放) ──────────────────────────────────

def _make_heart_sprite_sm(level: int, is_owner: bool) -> Image.Image:
    """生成包含心形 + LEVEL 数字的 RGBA sprite(模板原尺寸 29x25)。
    后续 NEAREST 缩放到任意 scale,心 + 数字同步缩放。
    """
    sprite = Image.new("RGBA", (HEART_TPL_W, HEART_TPL_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sprite)
    for ry, row in enumerate(_HEART_TEMPLATE):
        for rx, c in enumerate(row):
            if c == "1":
                draw.point((rx, ry), fill=HEART_FILL + (255,))
            elif c == "2":
                draw.point((rx, ry), fill=HEART_EDGE + (255,))
    if is_owner:
        lv_text, lv_scale = "MAX", 1
    elif level >= 10:
        lv_text, lv_scale = str(level), 1
    else:
        lv_text, lv_scale = str(level), 2
    text_w = _text_width(lv_text, spacing=1, scale=lv_scale)
    text_h = GLYPH_H * lv_scale
    cy = 9  # 心形视觉中心(跟原 _draw_heart heart_visual_cy 一致)
    tx = HEART_TPL_W // 2 - text_w // 2
    ty = cy - text_h // 2
    _draw_text(draw, tx, ty, lv_text, WHITE + (255,), scale=lv_scale)
    return sprite


def _paste_sprite_scaled(
    target: Image.Image, sprite: Image.Image,
    center_lg: tuple[int, int], base_scale: int,
    scale_x: float = 1.0, scale_y: float = 1.0,
) -> tuple[int, int, int, int]:
    """sprite NEAREST 缩放到 (W*base_scale*sx, H*base_scale*sy),paste 到大画布中心。
    返回 paste bbox。
    """
    sw = max(1, int(round(sprite.width * base_scale * scale_x)))
    sh = max(1, int(round(sprite.height * base_scale * scale_y)))
    scaled = sprite.resize((sw, sh), Image.Resampling.NEAREST)
    scaled = _alpha_threshold(scaled, 128)
    x = center_lg[0] - sw // 2
    y = center_lg[1] - sh // 2
    target.paste(scaled, (x, y), scaled)
    return (x, y, x + sw, y + sh)


# ── 爪子布局(跨帧复用) ────────────────────────────────────────────

def _decide_paw_layout(
    *,
    band_y_min: int, band_y_max: int,
    band_x_min: int, band_x_max: int,
    count: int = 3,
    target_widths: tuple[int, ...] = (60, 90, 130),
    forbidden_boxes: list[tuple[int, int, int, int]] | None = None,
    rng: random.Random | None = None,
    angle_range: tuple[float, float] = (-45.0, 15.0),
    scatter_zone_x_ratio: float = 0.62,
    scatter_zone_y_ratio: float = 0.55,
) -> list[tuple[Image.Image, int, int]]:
    """决定 3 个爪子的位置 + 旋转 + 大小,返回 (rotated_img, base_x, base_y) 列表。
    布局只跑一次,所有帧共用 — 帧间只加 y_offset 浮动。
    """
    r = rng or random
    crops = _load_paw_crops()
    if not crops or band_y_max < band_y_min or band_x_max < band_x_min:
        return []
    forbidden = list(forbidden_boxes or [])
    bw = band_x_max - band_x_min
    bh = band_y_max - band_y_min
    scatter_x_max = band_x_min + int(bw * scatter_zone_x_ratio)
    scatter_y_min = band_y_min + int(bh * (1.0 - scatter_zone_y_ratio))
    scatter_x_min = band_x_min
    scatter_y_max = band_y_max
    placed_boxes: list[tuple[int, int, int, int]] = []
    used_widths: list[int] = []
    layout: list[tuple[Image.Image, int, int]] = []
    for _ in range(count):
        remaining = [w for w in target_widths if w not in used_widths]
        tw = r.choice(remaining or list(target_widths))
        angle = r.uniform(*angle_range)
        placed_here = False
        for _outer in range(8):
            paw = r.choice(crops)
            rotated = _prepare_paw_rotated(paw, tw, angle)
            rw, rh = rotated.size
            max_x = scatter_x_max - rw
            max_y = scatter_y_max - rh
            if max_x < scatter_x_min or max_y < scatter_y_min:
                for fw in sorted(target_widths):
                    if fw >= tw:
                        continue
                    rotated = _prepare_paw_rotated(paw, fw, angle)
                    rw, rh = rotated.size
                    if (scatter_x_max - rw >= scatter_x_min
                            and scatter_y_max - rh >= scatter_y_min):
                        tw = fw
                        max_x = scatter_x_max - rw
                        max_y = scatter_y_max - rh
                        break
                else:
                    continue
            for _try in range(20):
                x = r.randint(scatter_x_min, max_x)
                y = r.randint(scatter_y_min, max_y)
                box = (x, y, x + rw, y + rh)
                if any(_bbox_overlap(box, b) for b in placed_boxes):
                    continue
                if any(_bbox_overlap(box, b) for b in forbidden):
                    continue
                layout.append((rotated, x, y))
                placed_boxes.append(box)
                used_widths.append(tw)
                placed_here = True
                break
            if placed_here:
                break
    return layout


# ── 渲染主函数 ───────────────────────────────────────────────────────

CANVAS_W = 96
CANVAS_H = 120
SCALE = 6  # NEAREST 放大倍数,输出 576 x 720


def _render_static_base_small(
    *,
    title: str,
    points: int,
    exp_current: int,
    exp_next_level: int | None,
    is_owner: bool,
    checked_in_today: bool,
    today_gained: int | None,
    mode: str,
) -> tuple[Image.Image, dict]:
    """渲染 GIF 各帧共享的静态层(小画布):边框/标题/LEVEL 标签/分隔/POINTS/底栏/星星。
    心形、Pusheen、爪子、粒子是动态层,每帧单独画。
    返回 (base_img_sm, layout_meta)。layout_meta 给主渲染器算位置。
    """
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(img)

    # 双层边框
    _draw_border(draw, 0, 0, CANVAS_W - 1, CANVAS_H - 1, DARK)
    _draw_border(draw, 2, 2, CANVAS_W - 3, CANVAS_H - 3, DARK)

    # 标题条
    draw.rectangle((3, 3, CANVAS_W - 4, 13), fill=DARK)
    title_text = title[:14]
    _draw_text_centered(draw, CANVAS_W // 2, 5, title_text, BG, scale=1)

    # 心形位置预留(不画心 — 心在大画布动态层画)
    heart_top_y = 16
    heart_bottom_y = heart_top_y + HEART_TPL_H - 1
    lv_label_y = heart_bottom_y + 2
    _draw_text_centered(draw, CANVAS_W // 2, lv_label_y, "LEVEL", DARK, scale=1)

    # 分隔虚线
    sep_y = lv_label_y + GLYPH_H + 2
    for x in range(6, CANVAS_W - 6, 3):
        draw.point((x, sep_y), fill=GRAY)

    # POINTS
    points_label_y = sep_y + 3
    _draw_text_centered(draw, CANVAS_W // 2, points_label_y, "POINTS", DARK, scale=1)
    points_text = "INF" if is_owner else _fmt_compact(points)
    big_y = points_label_y + GLYPH_H + 1
    _draw_text_centered(draw, CANVAS_W // 2, big_y, points_text,
                        ACCENT if not is_owner else GREEN, scale=2)

    # 底栏
    footer_top = CANVAS_H - 14
    draw.rectangle((3, footer_top, CANVAS_W - 4, CANVAS_H - 4), fill=DARK)
    if mode == "signin":
        if is_owner:
            line = "OWNER MAX"
        else:
            line = f"+{_fmt_compact(today_gained or 0)} GOT IT"
        _draw_text_centered(draw, CANVAS_W // 2, footer_top + 3, line, BG, scale=1)
    else:
        if is_owner or exp_next_level is None or exp_next_level <= 0:
            tip = "OWNER MAX" if is_owner else "MAX LV"
            _draw_text_centered(draw, CANVAS_W // 2, footer_top + 3, tip, BG, scale=1)
        else:
            _draw_text_centered(
                draw, CANVAS_W // 2, footer_top + 1,
                f"EXP {_fmt_compact(exp_current)}/{_fmt_compact(exp_next_level)}", BG, scale=1
            )
            status_y = footer_top + 1 + GLYPH_H + 1
            if status_y + GLYPH_H <= CANVAS_H - 4:
                status_txt = "DAILY DONE" if checked_in_today else "GET DAILY!"
                color = GREEN if checked_in_today else ACCENT
                _draw_text_centered(draw, CANVAS_W // 2, status_y, status_txt, color, scale=1)

    # 角落星星
    _draw_text(draw, 5, 4, "*", ACCENT, scale=1)
    _draw_text(draw, CANVAS_W - 10, 4, "*", ACCENT, scale=1)

    layout = {
        "heart_top_y_sm": heart_top_y,
        "heart_center_y_sm": heart_top_y + 9,  # 心形视觉中心
        "lv_label_y_sm": lv_label_y,
        "footer_top_sm": footer_top,
    }
    return img, layout


def render_card_frames(
    *,
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
    layout_seed: int | None = None,
) -> list[Image.Image]:
    """渲染 GIF 的 GIF_TOTAL_FRAMES 帧。layout_seed 控制爪子布局/粒子初值的随机。

    流程:
    1) 静态 base 小画布 → NEAREST 6x → 大画布 base
    2) 心形 sprite + Pusheen RGBA 预生成(每帧 NEAREST 缩放重 paste)
    3) 爪子布局一次决定(跨帧共享,帧间只 y_offset)
    4) 每帧 base.copy() + 画心 + Pusheen + 爪 + 粒子
    """
    seed = layout_seed if layout_seed is not None else random.randint(0, 1 << 30)
    rng = random.Random(seed)

    base_sm, lay = _render_static_base_small(
        title=title, points=points, exp_current=exp_current,
        exp_next_level=exp_next_level, is_owner=is_owner,
        checked_in_today=checked_in_today, today_gained=today_gained,
        mode=mode,
    )
    base_lg = base_sm.resize((CANVAS_W * SCALE, CANVAS_H * SCALE),
                             Image.Resampling.NEAREST)

    # 心形位置(大画布中心 x、心视觉中心 y)
    heart_cx_lg = (CANVAS_W // 2) * SCALE
    heart_cy_lg = lay["heart_center_y_sm"] * SCALE
    heart_sprite = _make_heart_sprite_sm(level, is_owner)

    # Pusheen 基础尺寸(大画布 px) + 锚定位置(bottom-right)
    pusheen = _load_pusheen()
    pusheen_data: dict | None = None
    if pusheen is not None and pusheen.width > 0:
        target_w_lg = 150
        aspect = pusheen.height / pusheen.width
        target_h_lg = max(8, int(round(target_w_lg * aspect)))
        pusheen_base = _resize_rgba_sharp_alpha(pusheen, target_w_lg, target_h_lg)
        pusheen_base = _alpha_threshold(pusheen_base, 128)
        margin_x_sm, margin_y_sm = 4, 1
        anchor_x = CANVAS_W * SCALE - margin_x_sm * SCALE - target_w_lg
        anchor_bottom_y = lay["footer_top_sm"] * SCALE - margin_y_sm * SCALE
        anchor_y = anchor_bottom_y - target_h_lg
        if anchor_x >= 0 and anchor_y >= 0:
            pusheen_data = {
                "img": pusheen_base,
                "w": target_w_lg, "h": target_h_lg,
                "anchor_x": anchor_x, "anchor_y": anchor_y,
                "anchor_bottom_y": anchor_bottom_y,
            }

    pusheen_box = None
    if pusheen_data:
        pusheen_box = (
            pusheen_data["anchor_x"], pusheen_data["anchor_y"],
            pusheen_data["anchor_x"] + pusheen_data["w"],
            pusheen_data["anchor_y"] + pusheen_data["h"],
        )

    # 爪子布局(跨帧)
    paw_band_top = (lay["lv_label_y_sm"] + GLYPH_H + 1) * SCALE
    paw_band_bot = (lay["footer_top_sm"] - 1) * SCALE
    paw_band_x_min = 4 * SCALE
    paw_band_x_max = (CANVAS_W - 4) * SCALE
    paw_layout: list[tuple[Image.Image, int, int]] = []
    if paw_band_bot > paw_band_top and paw_band_x_max > paw_band_x_min:
        paw_layout = _decide_paw_layout(
            band_y_min=paw_band_top, band_y_max=paw_band_bot,
            band_x_min=paw_band_x_min, band_x_max=paw_band_x_max,
            count=3, target_widths=(60, 90, 130),
            forbidden_boxes=[pusheen_box] if pusheen_box else None,
            rng=rng, angle_range=(-45.0, 15.0),
        )

    # 粒子锚点:Pusheen 头顶左偏(头在左上角)
    if pusheen_data:
        particle_anchor = (
            pusheen_data["anchor_x"] + pusheen_data["w"] // 3,
            pusheen_data["anchor_y"] - 2,  # 略高于 Pusheen 顶部
        )
    else:
        particle_anchor = None

    frames: list[Image.Image] = []
    for fi in range(GIF_TOTAL_FRAMES):
        frame = base_lg.copy()

        # 1) 心跳(scale 同步 x/y)
        s = _heart_pulse_scale(fi, level)
        _paste_sprite_scaled(frame, heart_sprite,
                             (heart_cx_lg, heart_cy_lg), SCALE,
                             scale_x=s, scale_y=s)

        # 2) Pusheen 呼吸(锚定底部中线,看起来吸地呼吸)
        if pusheen_data:
            wx, hy = _pusheen_breathe_scale(fi, level)
            new_w = max(8, int(round(pusheen_data["w"] * wx)))
            new_h = max(8, int(round(pusheen_data["h"] * hy)))
            puff = pusheen_data["img"].resize((new_w, new_h),
                                              Image.Resampling.NEAREST)
            puff = _alpha_threshold(puff, 128)
            cx = pusheen_data["anchor_x"] + pusheen_data["w"] // 2
            paste_x = cx - new_w // 2
            paste_y = pusheen_data["anchor_bottom_y"] - new_h
            frame.paste(puff, (paste_x, paste_y), puff)

        # 3) 爪子浮动
        for idx, (paw_img, base_x, base_y) in enumerate(paw_layout):
            y_off = _paw_float_offset_sm(fi, idx) * SCALE
            _paste_text_aware(frame, paw_img, base_x, base_y + y_off)

        # 4) 头顶粒子
        if particle_anchor:
            for char, color, x, y, sc, alpha in _particles_for_frame(
                fi, level, particle_anchor, layout_seed=seed,
            ):
                _draw_particle_at(frame, char, color, x, y, sc, alpha)

        frames.append(frame)

    return frames


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
    today_gained: int | None = None,
    mode: str = "summary",
) -> Image.Image:
    """单帧渲染(取 GIF 首帧)— 兜底/兼容用,正式发卡走 GIF。"""
    frames = render_card_frames(
        title=title, level=level, points=points,
        exp_current=exp_current, exp_next_level=exp_next_level,
        is_owner=is_owner, checked_in_today=checked_in_today,
        last_amount=last_amount, today_gained=today_gained, mode=mode,
    )
    return frames[0] if frames else Image.new("RGB", (CANVAS_W * SCALE, CANVAS_H * SCALE), BG)


def _frames_to_gif_palette(frames: list[Image.Image]) -> list[Image.Image]:
    """所有帧量化到第一帧的 palette,避免 GIF 跨帧调色板闪烁。"""
    if not frames:
        return frames
    master = frames[0].convert(
        "P", palette=Image.Palette.ADAPTIVE, colors=128, dither=Image.Dither.NONE,
    )
    out = [master]
    for f in frames[1:]:
        out.append(f.quantize(palette=master, dither=Image.Dither.NONE))
    return out


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
    """渲染并写盘 GIF。返回 .gif 路径,QQ 客户端原生支持。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    frames = render_card_frames(
        title=title, level=level, points=points,
        exp_current=exp_current, exp_next_level=exp_next_level,
        is_owner=is_owner, checked_in_today=checked_in_today,
        last_amount=last_amount, today_gained=today_gained, mode=mode,
    )
    stamp = f"{user_id}_{mode}_{int(time.time())}"
    digest = hashlib.md5(stamp.encode("utf-8")).hexdigest()[:10]
    out = output_dir / f"affection_{user_id}_{mode}_{digest}.gif"
    palette_frames = _frames_to_gif_palette(frames)
    palette_frames[0].save(
        out, format="GIF", save_all=True,
        append_images=palette_frames[1:],
        duration=GIF_DURATION_MS, loop=0, optimize=True, disposal=2,
    )
    return out


def prune_cards(output_dir: Path, max_files: int = 200) -> None:
    """LRU 清理:只保留最近 max_files 张。"""
    if not output_dir.is_dir():
        return
    try:
        files = sorted(
            (p for p in output_dir.iterdir()
             if p.is_file() and p.suffix.lower() in (".gif", ".png")),
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
