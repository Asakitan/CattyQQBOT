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
# 5x6 经典像素 paw print:3 个脚趾(上面 2 行)+ 圆形肉垫(下面 4 行)
# 视觉上比 7x5 4 脚趾的更紧凑,在 96x120 画布里不抢戏
_PAW_TEMPLATE: list[str] = [
    "X.X.X",   # 3 脚趾上行
    "X.X.X",   # 3 脚趾下行(脚趾 2px 高,看着像小肉球)
    ".....",   # 间隔
    ".XXX.",   # 肉垫顶
    "XXXXX",   # 肉垫中(最宽)
    ".XXX.",   # 肉垫底
]
PAW_W = 5
PAW_H = 6


def _draw_paw(draw: ImageDraw.ImageDraw, x0: int, y0: int,
              color: tuple[int, int, int]) -> None:
    """以 (x0, y0) 为左上角画一个 5x6 猫爪印。"""
    for ry, row in enumerate(_PAW_TEMPLATE):
        for rx, c in enumerate(row):
            if c == "X":
                draw.point((x0 + rx, y0 + ry), fill=color)


def _scatter_paws(
    draw: ImageDraw.ImageDraw,
    *,
    band_y_min: int,
    band_y_max: int,
    band_x_min: int,
    band_x_max: int,
    color: tuple[int, int, int] = HEART_EDGE,
    count_range: tuple[int, int] = (2, 3),
    min_gap: int = 3,
    rng: random.Random | None = None,
) -> int:
    """在指定矩形 band 内随机散布 1-3 个爪印,横向不重叠(min_gap 像素间距)。

    返回实际画上去的爪印数量(可能 < count 上限,如果空间不足)。
    band_y_max/band_x_max 是允许 paw 左上角的最大坐标(已 留出 PAW_W/PAW_H 空间)。
    """
    r = rng or random
    if band_y_max < band_y_min or band_x_max < band_x_min:
        return 0
    n = r.randint(*count_range)
    used_xs: list[int] = []
    placed = 0
    for _ in range(n):
        # 最多试 8 次避免和已放的横向重叠;失败就放弃这个
        for _try in range(8):
            x = r.randint(band_x_min, band_x_max)
            if all(abs(x - ux) >= PAW_W + min_gap for ux in used_xs):
                y = r.randint(band_y_min, band_y_max)
                _draw_paw(draw, x, y, color)
                used_xs.append(x)
                placed += 1
                break
    return placed


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

    # 爪印散布(POINTS 大数字下方、footer 上方的空白带)
    # 每次随机 2-3 个,位置每次不同,体现"猫猫来盖章"的活泼感
    big_y_bottom = big_y + GLYPH_H * 2  # 大数字底部 (scale=2)
    paw_band_top = big_y_bottom + 2
    paw_band_bot = footer_top - 1 - PAW_H
    paw_band_x_min = 5
    paw_band_x_max = CANVAS_W - 5 - PAW_W
    if paw_band_bot >= paw_band_top and paw_band_x_max > paw_band_x_min:
        _scatter_paws(
            draw,
            band_y_min=paw_band_top,
            band_y_max=paw_band_bot,
            band_x_min=paw_band_x_min,
            band_x_max=paw_band_x_max,
            color=HEART_EDGE,  # 深粉,在米黄背景上显眼且和心形呼应
            count_range=(2, 3),
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
