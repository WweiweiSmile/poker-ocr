"""按**文字**定位操作区的动作按钮，而不是死磕 profile 里的固定槽位。

为什么需要这一层（2026-11-11 现场录屏实测，1472x1666，10fps，174s）：

客户端在同一张桌上会用**两套版面**，位置完全不同：

    版面 A（三按钮，最常见）
        弃牌 @ (566,1396)   主按钮 @ (736,1344)   右 pill @ (906,1397)
        主按钮文案在 加注 / 下注 / 确定 之间切换
        右 pill 文案在 让牌 / 跟注 / 自动让牌 之间切换

    版面 B（两按钮，只剩弃牌/免费）
        弃牌 @ (736,1344)   免费 @ (907,1388)

固定槽位是按版面 A 标的：

  · 版面 B 里点「弃牌」会落到 (563,1405)——那里是「偷偷看牌」促销位/毛毡，
    等于点空白（录屏里这种版面占了 11/174 秒的采样）；
  · 版面 B 里「免费」才是过牌按钮，而 A 布局里右 pill 的过牌叫「让牌」；
  · 左下角还有一个装饰性的「免费」@(194,1354)，**不是按钮**，必须按位置排除；
  · 桌心 y≈1280 处的「下注 / 加注 / 跟注」是**当前下注徽章**，也不是按钮，
    必须按 y 排除。

所以这里只认「底部操作行 + 已知文案 + 位置约束」，返回整帧像素坐标，
由调用方再用 `WindowMapper.point_from_image` 换成屏幕逻辑点。
"""

from __future__ import annotations

from typing import Mapping

import numpy as np

from .ocr import OcrEngine

# 动作 → 可能的文案。顺序即优先级。
ACTION_LABELS: dict[str, tuple[str, ...]] = {
    "fold": ("弃牌",),
    "check": ("让牌", "免费"),
    "call": ("跟注",),
    "raise": ("加注",),
    "bet": ("下注",),
    "confirm": ("确定",),
}

# 明确**不是**按钮、要忽略的文案（促销/自动让牌开关）
IGNORED_LABELS = ("自动让牌", "偷偷看牌", "发发看", "看牌", "免费看牌")

# 操作行在整帧里的纵向范围（归一化）。下面是桌面下注徽章所在行，
# 不加下界会把「下注 1BB」当成按钮点下去。
BAND_TOP = 0.775
BAND_BOTTOM = 0.900
# 「免费」只有出现在右侧 pill 位才是过牌按钮；左边那个是装饰。
FREE_MIN_X = 0.55
MIN_SCORE = 0.60


def find_action_buttons(img: np.ndarray, engine: OcrEngine | None = None, *,
                        band: tuple[float, float] = (BAND_TOP, BAND_BOTTOM),
                        min_score: float = MIN_SCORE
                        ) -> dict[str, tuple[float, float]]:
    """在操作行里按文案找按钮 → {动作: (cx, cy)}（整帧像素坐标）。

    找不到的动作不出现在返回里；调用方据此回退到 profile 槽位。
    同一动作命中多个时取**最靠右**的（右侧 pill 是主操作位，
    左下角那个装饰性「免费」靠 x 判定被排除）。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return {}
    engine = engine or OcrEngine()
    h, w = img.shape[:2]
    y0, y1 = int(h * band[0]), int(h * band[1])
    if y1 <= y0:
        return {}
    boxes = engine.read_boxes(img[y0:y1])
    found: dict[str, list[tuple[float, float]]] = {}
    for box in boxes:
        text = (box.text or "").strip()
        if not text or box.score < min_score:
            continue
        if any(word in text for word in IGNORED_LABELS):
            continue
        cx, cy = float(box.cx), float(box.cy) + y0
        for action, labels in ACTION_LABELS.items():
            if not any(word in text for word in labels):
                continue
            # 左下的装饰性「免费」不是过牌按钮
            if action == "check" and "免费" in text and cx < w * FREE_MIN_X:
                continue
            found.setdefault(action, []).append((cx, cy))
            break
    out: dict[str, tuple[float, float]] = {}
    for action, points in found.items():
        points.sort(key=lambda p: p[0])
        out[action] = points[-1]
    return out


def action_for_option(kind: str, *, to_call: float = 0.0,
                      is_confirm: bool = False) -> str:
    """把建议的动作 kind 映射成这里用的动作名。

    `check` / `call` 在客户端是同一个右 pill（文案不同），所以统一落到
    check 优先、再退 call 的顺序，由 `resolve_target` 决定实际点哪个。
    """
    if is_confirm:
        return "confirm"
    if kind == "fold":
        return "fold"
    if kind in ("call", "check"):
        return "check" if to_call <= 0 else "call"
    if kind == "raise":
        return "raise"
    if kind == "bet":
        return "bet"
    return kind


def resolve_target(buttons: Mapping[str, tuple[float, float]],
                   action: str) -> tuple[float, float] | None:
    """按动作名取坐标；check/call 互相兜底（同一颗右 pill）。"""
    if action in buttons:
        return buttons[action]
    if action == "check":
        return buttons.get("call")
    if action == "call":
        return buttons.get("check")
    return None
