"""点击标注：把「可点击区域」和「OCR 命中点」画出来。

为什么需要它：识别对不对，光看日志看不出来 —— 文字读到了不等于坐标落对，
坐标落对了也不等于**映射到屏幕**后还在按钮上。这里把两件事分开画：

  · 槽位框（蓝）  profile 算出来的**可点区域**，也就是点击器真正会点的范围；
  · OCR 命中（红）按文字现找的按钮中心点（`action_buttons.find_action_buttons`）；
  · 字幕         窗口几何、视频帧几何、按比例预计的动作行偏移。

两种用法：
  · `render_overlay`        透明 RGBA 图 → 屏幕浮层（`poker_ocr.overlay`）；
  · `compose_on_screenshot` 叠到真实截图/单帧图上 → 存档、离线核对。

所有 `Mark` 坐标都是**屏幕逻辑点（左上原点）**，和 `clicker.Rect` 同一口径。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Mapping

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .bet_slider import WindowMapper, WindowRect
from .clicker import BUTTON_LABELS, ClickerConfig, Rect

# 框里放不下长名字（35pt 宽的档位钮），用短标签
SHORT_LABELS = {
    "fold": "弃牌",
    "call": "跟注",
    "check": "过牌",
    "raise-1/3": "1/3",
    "raise-1/2": "1/2",
    "raise-2/3": "2/3",
    "raise-1": "1",
    "raise-1.2": "1.2",
    "allin": "ALLIN",
}

# 颜色按 BGR 习惯写，PIL 里转 RGB 用
COLORS = {
    "slot": (0, 145, 255),        # 蓝：可点区域
    "ocr": (52, 199, 89),         # 绿：OCR 命中点
    "warn": (48, 48, 255),        # 红：出界
    "click": (10, 214, 255),      # 黄：实际点击点
}

FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
)

# OCR 动作 → 点击器槽位（check/call 是同一颗右 pill）
SLOT_FOR_ACTION = {"fold": "fold", "check": "call", "call": "call"}


@dataclass(frozen=True)
class Mark:
    """一个标注：屏幕逻辑点矩形（点标记 w=h=0）+ 文案。"""

    x: float
    y: float
    w: float = 0.0
    h: float = 0.0
    label: str = ""
    kind: str = "slot"            # slot | ocr | click | warn
    note: str = ""
    shape: str = "rect"           # rect | disc | circle（和 clicker.Rect 一致）


# ─────────────────────────── 标从哪来 ───────────────────────────

def marks_from_buttons(config: ClickerConfig,
                       labels: Mapping[str, str] | None = None) -> list[Mark]:
    """点击器配置里的每个按钮 → 一个蓝色框。"""
    labels = labels or BUTTON_LABELS
    return [Mark(r.x, r.y, r.w, r.h,
                 label=SHORT_LABELS.get(name) or labels.get(name, name),
                 kind="slot", shape=r.shape)
            for name, r in config.buttons.items()]


def targets_to_marks(found: Mapping[str, tuple[float, float]],
                     frame_size: tuple[int, int],
                     mapper: WindowMapper) -> list[Mark]:
    """OCR 在**帧像素**里找到的点 → 屏幕逻辑点的标记。

    和 `live._locate_click_targets` 用同一套换算（帧归一化 → `WindowMapper`），
    所以这里画出来的位置就是点击器真正会落下去的位置。
    """
    w, h = frame_size
    if w <= 0 or h <= 0:
        return []
    out = []
    for action, (cx, cy) in found.items():
        sx, sy = mapper.point_from_image(float(cx) / w, float(cy) / h)
        out.append(Mark(sx, sy, label=action, kind="ocr"))
    return out


def annotate_alignment(marks: list[Mark],
                       buttons: Mapping[str, Rect]) -> list[Mark]:
    """给 OCR 命中点补备注：是否落在对应动作的可点区域内。

    OCR 点在框里 = 文字中心被可点区域盖住，点击落点不会跑出按钮；
    出界则改成红色并写上偏了多少 pt —— 这是最值得盯的一种错。
    """
    out: list[Mark] = []
    for m in marks:
        slot_name = SLOT_FOR_ACTION.get(m.label)
        slot = buttons.get(slot_name) if slot_name else None
        if m.kind != "ocr" or slot is None:
            out.append(m)
            continue
        dx = max(slot.x - m.x, 0.0, m.x - slot.x2)
        dy = max(slot.y - m.y, 0.0, m.y - slot.y2)
        if dx <= 0 and dy <= 0:
            out.append(m)
        else:
            out.append(replace(m, kind="warn",
                               note=f"出界 {math.hypot(dx, dy):.0f}pt"))
    return out


def predicted_offset_pt(window: WindowRect, frame_size: tuple[int, int]) -> float:
    """帧顶对齐时，帧比窗口矮/高多少 pt（正 = 帧更矮 = 点击偏低）。

    这就是 2026-11-11 实盘「动作行略微偏下」的那个量：窗口 845pt、帧
    1472x1666（=736x833pt）→ +12pt，动作行 ny≈0.85 处约偏低 10pt。
    """
    fw, fh = frame_size
    if fw <= 0 or fh <= 0 or window.w <= 0:
        return 0.0
    return window.h - window.w * fh / fw


# ─────────────────────────── 画图 ───────────────────────────

@lru_cache(maxsize=8)
def _font(size: int):
    size = max(9, int(size))
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _rgba(kind: str, alpha: int = 255) -> tuple[int, int, int, int]:
    b, g, r = COLORS.get(kind, COLORS["slot"])
    return (r, g, b, alpha)


def _pill(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
          font, *, fill=(0, 0, 0, 165), fg=(255, 255, 255, 255),
          pad: float = 3.0) -> float:
    """带底色的文字标签，返回宽度。"""
    if not text:
        return 0.0
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = float(xy[0]), float(xy[1])
    draw.rounded_rectangle([x, y, x + tw + pad * 2, y + th + pad * 2],
                           radius=3, fill=fill)
    draw.text((x + pad - bbox[0], y + pad - bbox[1]), text, font=font, fill=fg)
    return tw + pad * 2


def _to_px(m: Mark, window: WindowRect, scale: float
           ) -> tuple[float, float, float, float]:
    return ((m.x - window.x) * scale, (m.y - window.y) * scale,
            m.w * scale, m.h * scale)


def _draw_slot(draw, m: Mark, window: WindowRect, scale: float, font) -> None:
    """按按钮真实形状画：圆钮画圆（半径 = 短边/2，和点击器的 disc 一致），
    其余画圆角矩形。"""
    x, y, w, h = _to_px(m, window, scale)
    width = max(2, round(2 * scale))
    if m.shape in ("disc", "circle"):
        cx, cy = x + w / 2, y + h / 2
        r = min(w, h) / 2
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     fill=_rgba("slot", 42),
                     outline=_rgba("slot", 235), width=width)
    else:
        draw.rounded_rectangle([x, y, x + w, y + h],
                               radius=max(4, round(6 * scale)),
                               fill=_rgba("slot", 42),
                               outline=_rgba("slot", 235), width=width)
    if not m.label:
        return
    tw = draw.textlength(m.label, font=font)
    _pill(draw, (x + (w - tw) / 2, y + h + 2 * scale), m.label, font)


def _draw_point(draw, m: Mark, window: WindowRect, scale: float, font) -> None:
    x, y, _w, _h = _to_px(m, window, scale)
    color = _rgba("warn" if m.kind == "warn" else m.kind)
    width = max(2, round(2 * scale))
    r = max(6.0, 9.0 * scale)
    for dx, dy in ((-1.7, 0), (1.7, 0), (0, -1.7), (0, 1.7)):
        draw.line([(x + dx * r, y + dy * r), (x, y)], fill=color, width=width)
    draw.ellipse([x - r, y - r, x + r, y + r], outline=color, width=width)
    draw.ellipse([x - 1.5 * scale, y - 1.5 * scale,
                  x + 1.5 * scale, y + 1.5 * scale], fill=color)
    text = f"OCR {m.label}" + (f" {m.note}" if m.note else "")
    _pill(draw, (x + r + 4 * scale, y - r), text, font,
          fg=color if m.kind == "warn" else (255, 255, 255, 255))


def _draw_caption(draw, text: str, font, scale: float) -> None:
    if not text:
        return
    lines = text.split("\n")
    pad = round(6 * scale)
    widths = [draw.textlength(line, font=font) for line in lines]
    line_h = draw.textbbox((0, 0), "Ag", font=font)[3] + round(2 * scale)
    box_w = max(widths) + pad * 2
    box_h = line_h * len(lines) + pad * 2
    draw.rounded_rectangle([0, 0, box_w, box_h], radius=6,
                           fill=(0, 0, 0, 185))
    for i, line in enumerate(lines):
        draw.text((pad, pad + i * line_h), line, font=font,
                  fill=(255, 255, 255, 245))


def render_overlay(window: WindowRect, scale: float, marks: list[Mark], *,
                   caption: str = "") -> np.ndarray:
    """透明底 RGBA 浮层图（尺寸 = 窗口逻辑点 × scale）。"""
    size = (max(1, round(window.w * scale)), max(1, round(window.h * scale)))
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(round(12 * scale))
    for m in marks:
        if m.w > 0 and m.h > 0:
            _draw_slot(draw, m, window, scale, font)
        else:
            _draw_point(draw, m, window, scale, font)
    _draw_caption(draw, caption, _font(round(13 * scale)), scale)
    return np.array(img)


def compose_on_screenshot(shot: np.ndarray, window: WindowRect, scale: float,
                          marks: list[Mark], *, caption: str = "") -> np.ndarray:
    """把标注叠到真实截图（BGR）或单帧图上，返回 BGR。"""
    if shot is None or getattr(shot, "size", 0) == 0:
        raise ValueError("截图是空的")
    h, w = shot.shape[:2]
    layer = render_overlay(window, scale, marks, caption=caption)
    if layer.shape[1] != w or layer.shape[0] != h:
        layer = np.array(Image.fromarray(layer, "RGBA").resize((w, h)))
    base = Image.fromarray(cv2.cvtColor(shot, cv2.COLOR_BGR2RGBA), "RGBA")
    out = Image.alpha_composite(base, Image.fromarray(layer, "RGBA"))
    return cv2.cvtColor(np.array(out), cv2.COLOR_RGBA2BGR)

