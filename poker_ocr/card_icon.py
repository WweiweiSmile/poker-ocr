"""头像底角的红色「POKER」手牌图标：还在池里的玩家才有。

WPK 在**没有弃牌**的玩家头像底角挂一张红色小牌（两张牌背并排，写着
`POKER`）：它在，人就在这一手里；弃牌后图标消失，头像被整块「弃牌」
遮罩盖住。这条证据比「弃牌」两个字的 OCR 稳得多 —— 实测头像区的 rec 会把
弃牌读成「奔牌」「水」「风」「O」之类，分数还忽高忽低。

摊牌时头像会被两张亮出的手牌顶掉，同样没有图标 —— 但**亮牌不代表没弃牌**
（河底 show 牌的人可能早就弃了），所以这里返回 None，交给调用方沿用
之前动作得出的弃牌判定；不能拿「有 show」当「还在池里」。

    返回三态：

    True  —— 看到图标：这一帧还在池里（没弃牌）
    False —— 没看到图标，且头像槽看起来仍是头像
    None  —— 判断不了（摊牌亮牌 / 空槽 / 图太小 / 头像槽被别的东西盖住）

调用方（`build_state`）只把 True/False 当证据，None 不改变任何判断。

几何（2026-10-22 样张标定，参考尺寸 982x2182）：图标约 47x50px，占头像
高度 ~35%，红色填充率 ≥0.55；位置固定在头像**左下或右下角**，中心分别在
槽宽的 ~11% / ~89%、槽高的 ~80%。所以只在这两个底角找，头像中间/上方的
红色（衣服、道具）不会误判。所有裁块先按头像槽高度归一化到 131px 再判断，
阈值不随流分辨率变化；槽高 <56px 时直接返回 None（分辨率太低，宁可不知道，
也不能把整桌人误判成弃牌）。
"""

from __future__ import annotations

import cv2
import numpy as np

from .profile import Profile

# 图标在头像里的相对尺寸：高 25%~58%（实测 35%），宽高比 0.6~1.7（实测 0.9）
ICON_H_RANGE = (0.25, 0.58)
ICON_ASPECT_RANGE = (0.6, 1.7)
ICON_MIN_FILL = 0.55        # 红色像素 / bbox 面积；照片里的红块填充率明显更低
ICON_MIN_AREA = 250         # 像素（归一化后头像槽高 131px）；再小就是噪点
ICON_PAD = 0.12             # 头像槽向外扩一点，图标有一半压在槽边上
# 底角：中心相对槽宽的 x 落在 [0, 0.35] 或 [0.65, 1.0]，y 落在 [0.55, 1.05]
ICON_CORNER_X = 0.35
ICON_CORNER_Y = (0.55, 1.05)
# 白边校验：红色 bbox 外 3px 的一圈里，白色像素占比至少这么多
ICON_WHITE_RING_MIN = 0.35
# 头像槽统一缩放到这个高度再判断：阈值都是参考分辨率（982x2182）下的像素，
# 低分辨率流（OBS 只给 540 宽）不先放大就只剩十几个像素，会整桌漏检。
ICON_CANON_H = 131
# 头像槽小于这个高度就别判了：图标只有十几像素时宁可返回「未知」，
# 也不能把整桌人误判成弃牌（对应流分辨率低于参考尺寸 ~43%，实测 0.4x 仍可，
# 0.37x 开始漏检）。
ICON_MIN_SLOT_H = 56
# 头像槽白到这种程度 = 摊牌亮出的两张牌，不是头像（实测 0.72；最白的头像 0.54）
SHOWN_CARDS_WHITE = 0.62


def _has_icon(roi: np.ndarray, slot_w: float, slot_h: float,
              off_x: float, off_y: float) -> bool:
    """在头像槽（含外扩）里找一张红色小牌。

    `off_x` / `off_y` 是 roi 左上角相对头像槽左上角的像素偏移（外扩为负）。
    """
    if roi.size == 0 or roi.shape[0] < 8 or roi.shape[1] < 8:
        return False
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = (((h <= 10) | (h >= 170)) & (s >= 90) & (v >= 70))
    red = red.astype(np.uint8) * 255
    mask = cv2.morphologyEx(red, cv2.MORPH_CLOSE,
                            np.ones((5, 5), np.uint8), iterations=2)
    white = (v >= 150) & (s <= 90)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < ICON_MIN_AREA:
            continue
        if not (ICON_H_RANGE[0] * slot_h <= h <= ICON_H_RANGE[1] * slot_h):
            continue
        if not (ICON_ASPECT_RANGE[0] <= w / h <= ICON_ASPECT_RANGE[1]):
            continue
        if area / float(w * h) < ICON_MIN_FILL:
            continue
        cx, cy = centroids[i]
        xr = (cx + off_x) / slot_w
        yr = (cy + off_y) / slot_h
        if not (ICON_CORNER_Y[0] <= yr <= ICON_CORNER_Y[1]):
            continue
        if not (xr <= ICON_CORNER_X or xr >= 1.0 - ICON_CORNER_X):
            continue
        # 图标有白边；照片里的红块通常和周围颜色连在一起，没有一圈白
        ring = np.zeros_like(mask)
        y0, y1 = max(0, y - 3), min(mask.shape[0], y + h + 3)
        x0, x1 = max(0, x - 3), min(mask.shape[1], x + w + 3)
        ring[y0:y1, x0:x1] = 255
        ring[y:y + h, x:x + w] = 0
        near = white[ring > 0]
        if near.size and float(near.mean()) < ICON_WHITE_RING_MIN:
            continue
        return True
    return False


def read_card_icons(img: np.ndarray,
                    profile: Profile) -> dict[str, bool | None]:
    """逐座判断头像底角有没有「POKER」手牌图标（hero 跳过，返回 None）。

    只做 OpenCV 颜色/几何判断，不跑 OCR：9 个小裁块，耗时 <5ms。
    """
    h, w = img.shape[:2]
    out: dict[str, bool | None] = {}
    for seat in profile.seats:
        sid = seat.seat_id
        if sid == profile.hero_seat_id:
            out[sid] = None          # hero 自己头像从不挂这张图标
            continue
        x1, y1, x2, y2 = profile.slot_to_px(seat.avatar_slot, (w, h))
        slot_w, slot_h = float(x2 - x1), float(y2 - y1)
        if slot_h < ICON_MIN_SLOT_H:        # 图太小（单测的 4x4 桩）→ 不判
            out[sid] = None
            continue
        ix1, iy1 = max(0, x1), max(0, y1)
        ix2, iy2 = min(w, x2), min(h, y2)
        slot = img[iy1:iy2, ix1:ix2]
        hsv = cv2.cvtColor(slot, cv2.COLOR_BGR2HSV)
        white_ratio = float(((hsv[:, :, 2] >= 170)
                             & (hsv[:, :, 1] <= 70)).mean())
        if white_ratio >= SHOWN_CARDS_WHITE:
            out[sid] = None          # 摊牌亮牌：头像被明牌顶掉，没有图标不代表弃牌
            continue
        pad_x, pad_y = int(slot_w * ICON_PAD), int(slot_h * ICON_PAD)
        X1, Y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
        X2, Y2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
        roi = img[Y1:Y2, X1:X2]
        # 统一到参考尺寸：阈值（5px 闭运算、白边 3px、最小面积）才有意义
        scale = ICON_CANON_H / slot_h
        interp = (cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
        roi = cv2.resize(roi, None, fx=scale, fy=scale, interpolation=interp)
        out[sid] = _has_icon(roi, slot_w * scale, slot_h * scale,
                             (X1 - x1) * scale, (Y1 - y1) * scale)
    return out
