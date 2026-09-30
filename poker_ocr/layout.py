"""把 OCR 文本框按几何位置分配给 9 个座位的槽位，并排出环序。

分两层过滤，都是几何/类型约束，**不做内容黑名单**（那种词表在 app 改版后必然失效）：
  1. 区域门：挡掉状态栏和播放器 UI
  2. 槽位门：文本框中心必须靠近某个槽位
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field

from .fields import looks_like_chip, looks_like_name
from .ocr import OcrBox
from .profile import Profile, Seat, Size


@dataclass
class SlotAssignment:
    seat_id: str
    chip_boxes: list[OcrBox] = dc_field(default_factory=list)
    name_boxes: list[OcrBox] = dc_field(default_factory=list)

    def chip_text(self) -> str | None:
        if not self.chip_boxes:
            return None
        return self.chip_boxes[0].text

    def name_text(self) -> str | None:
        """多个名字框横向拼接。用 "" 不用 " " —— 否则「奥利奥H」被切成
        「奥利奥」+「H」两框时会拼成「奥利奥 H」。"""
        if not self.name_boxes:
            return None
        ordered = sorted(self.name_boxes, key=lambda b: b.x1)
        return "".join(b.text for b in ordered)


@dataclass
class Assignment:
    by_seat: dict[str, SlotAssignment]
    noise: list[OcrBox]
    gated_out: list[OcrBox]
    candidates: list[tuple[float, str, str, OcrBox]] = dc_field(default_factory=list)


# 名字槽最多接受 2 个框（名字被切成两段的情况），筹码槽只认 1 个
_NAME_CAPACITY = 2
_CHIP_CAPACITY = 1


def _cost(box: OcrBox, slot_px: tuple[int, int, int, int]) -> float:
    """归一化中心距：以槽位自身尺寸为单位，这样不同大小的槽位可比。"""
    sx1, sy1, sx2, sy2 = slot_px
    scx, scy = (sx1 + sx2) / 2, (sy1 + sy2) / 2
    sw = max(1.0, sx2 - sx1)
    sh = max(1.0, sy2 - sy1)
    return math.hypot((box.cx - scx) / sw, (box.cy - scy) / sh)


def _iou(a: OcrBox, b: OcrBox) -> float:
    ix1, iy1 = max(a.x1, b.x1), max(a.y1, b.y1)
    ix2, iy2 = min(a.x2, b.x2), min(a.y2, b.y2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def dedupe_boxes(boxes: list[OcrBox], iou_threshold: float = 0.5) -> list[OcrBox]:
    """去掉重复检测。

    检测模型偶尔会对同一段文字吐两个几乎重合的框（实测 table1 里
    「菜菜子31」「谈轩66」各出现两次）。不理它的话，名字槽能装 2 个框，
    同一段文字会被拼两遍变成「菜菜子31菜菜子31」。
    """
    kept: list[OcrBox] = []
    for b in boxes:
        if any(k.text == b.text and _iou(k, b) > iou_threshold for k in kept):
            continue
        kept.append(b)
    return kept


def assign_boxes(boxes: list[OcrBox], profile: Profile, size: Size) -> Assignment:
    by_seat = {s.seat_id: SlotAssignment(s.seat_id) for s in profile.seats}
    noise: list[OcrBox] = []
    gated: list[OcrBox] = []

    # ── 第 0 层：去重 ──
    boxes = dedupe_boxes(boxes)

    # ── 第 1 层：区域门 ──
    in_region: list[OcrBox] = []
    for b in boxes:
        if profile.in_region_gate(b.normalized_center(size), size):
            in_region.append(b)
        else:
            gated.append(b)

    # ── 第 2 层：类型门 + 候选对 ──
    pairs: list[tuple[float, str, str, OcrBox]] = []
    for b in in_region:
        is_chip = looks_like_chip(b.text)
        is_name = looks_like_name(b.text)
        if not is_chip and not is_name:
            noise.append(b)
            continue
        for seat in profile.seats:
            if is_chip:
                pairs.append((_cost(b, profile.slot_to_px(seat.chip_slot, size)),
                              seat.seat_id, "chip", b))
            if is_name:
                pairs.append((_cost(b, profile.slot_to_px(seat.name_slot, size)),
                              seat.seat_id, "name", b))

    # ── 贪心分配：全局按代价升序，就近落座 ──
    pairs.sort(key=lambda p: p[0])
    claimed: set[int] = set()
    counts: dict[tuple[str, str], int] = {}
    limit = profile.thresholds.get("max_assign_dist", 1.6)

    for cost, seat_id, kind, box in pairs:
        if id(box) in claimed or cost > limit:
            continue
        capacity = _NAME_CAPACITY if kind == "name" else _CHIP_CAPACITY
        used = counts.get((seat_id, kind), 0)
        if used >= capacity:
            continue
        slot = by_seat[seat_id]
        (slot.name_boxes if kind == "name" else slot.chip_boxes).append(box)
        counts[(seat_id, kind)] = used + 1
        claimed.add(id(box))

    # 没被任何槽位认领的（过了区域门但离槽位太远 / 槽位已满）
    unclaimed = [b for b in in_region if id(b) not in claimed]

    return Assignment(by_seat=by_seat, noise=noise + unclaimed,
                      gated_out=gated, candidates=pairs)


def seat_angle(profile: Profile, seat: Seat) -> float:
    """座位相对桌心的方位角（度）。图像坐标系 y 向下，所以角度增大 = 屏幕上顺时针。"""
    nx = (seat.name_slot[0] + seat.name_slot[2]) / 2
    ny = (seat.name_slot[1] + seat.name_slot[3]) / 2
    dx = nx - profile.table_center[0]
    dy = ny - profile.table_center[1]
    return math.degrees(math.atan2(dy, dx)) % 360
