"""全图标定 → runtime profile（M3.3）。

把 `full_table.observe_full()` 检测到的座位/文字框，转成 `Profile.load()` 能读的
归一化槽位 JSON。之后实时链路走固定 ROI，不再每帧跑全图 det+rec。

    result = observe_full(img, engine, full_cfg)
    profile = build_runtime_profile(result, img.shape[1::-1], source="browser")
    Path("config/runtime.json").write_text(json.dumps(profile, ...))

生成的 profile 是**按这次画面现场标定**的，不是硬编码布局。窗口尺寸/布局变了要
重新标定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .full_table import FullTableResult, SeatObservation, parse_chip_generic
from .ocr import OcrBox
from .parse import parse_blinds


DEFAULT_THRESHOLDS = {
    "chip_min_score": 0.7,
    "name_min_score": 0.7,
    "slot_margin": 0.18,
    "max_assign_dist": 0.5,
    "avatar_var_min": 18.0,
    "chip_pill_contrast_min": 25.0,
    "hero_turn_min_score": 0.5,
}

# 名字/筹码槽位在检测框外留白：全图框就是文字紧框，扩一点即可
NAME_PAD = 0.10
CHIP_PAD = 0.10
GLOBAL_PAD = 0.10


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _slot_from_box(box: OcrBox, size: tuple[int, int], pad: float,
                   min_w: float = 0.0, min_h: float = 0.0) -> list[float]:
    """检测框 → 槽位。筹码数值长度会变（3BB → 1,234BB），必须有最小宽度。"""
    w, h = size
    x1, y1 = box.x1 / w, box.y1 / h
    x2, y2 = box.x2 / w, box.y2 / h
    dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
    x1, y1, x2, y2 = x1 - dx, y1 - dy, x2 + dx, y2 + dy
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    if x2 - x1 < min_w:
        x1, x2 = cx - min_w / 2, cx + min_w / 2
    if y2 - y1 < min_h:
        y1, y2 = cy - min_h / 2, cy + min_h / 2
    x1, y1 = _clamp01(x1), _clamp01(y1)
    x2, y2 = _clamp01(x2), _clamp01(y2)
    if x2 - x1 < 0.002:
        x2 = min(1.0, x1 + 0.002)
    if y2 - y1 < 0.002:
        y2 = min(1.0, y1 + 0.002)
    return [round(x1, 6), round(y1, 6), round(x2, 6), round(y2, 6)]


def _fallback_slot(center: tuple[float, float], dx: float,
                   dy_from: float, dy_to: float) -> list[float]:
    x, y = center
    return [round(_clamp01(x - dx), 6), round(_clamp01(y + dy_from), 6),
            round(_clamp01(x + dx), 6), round(_clamp01(y + dy_to), 6)]


def _avatar_slot(seat: SeatObservation, size: tuple[int, int]) -> list[float]:
    """头像在名字和筹码之间；没有头像检测时用座位中心附近的一条带。"""
    x, y = seat.center
    w = 0.05
    if seat.name_box is not None:
        w = max(w, (seat.name_box.x2 - seat.name_box.x1) / size[0] * 0.6)
    if seat.stack_box is not None:
        w = max(w, (seat.stack_box.x2 - seat.stack_box.x1) / size[0] * 0.6)
    return [round(_clamp01(x - w), 6), round(_clamp01(y - 0.04), 6),
            round(_clamp01(x + w), 6), round(_clamp01(y + 0.04), 6)]


def _find_bet_slot(seat: SeatObservation, boxes: list[dict],
                   table_center: tuple[float, float],
                   size: tuple[int, int],
                   stack_rects: list[tuple[float, float, float, float]],
                   ) -> list[float] | None:
    """座位附近、又不是任何人的后手筹码的那个筹码框 = 本街下注 pill。"""
    w, h = size
    best = None
    for item in boxes:
        value, _ = parse_chip_generic(item.get("text", ""))
        if value is None:
            continue
        x1, y1, x2, y2 = item["box"]
        if any(all(abs(a - b) < 2.0
                   for a, b in zip((x1, y1, x2, y2), rect))
               for rect in stack_rects):
            continue
        cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
        d_seat = math.hypot(cx - seat.center[0], cy - seat.center[1])
        d_center = math.hypot(cx - table_center[0], cy - table_center[1])
        if d_seat > 0.16 or d_center < 0.14:
            continue
        if best is None or d_seat < best[0]:
            best = (d_seat, item)
    if best is None:
        return None
    item = best[1]
    box = OcrBox(item["text"], float(item.get("score", 1.0)),
                 *[float(v) for v in item["box"]], polygon=[])
    return _slot_from_box(box, size, CHIP_PAD)


def _find_global_slots(result: FullTableResult,
                       size: tuple[int, int]) -> dict[str, list[float]]:
    boxes = (result.debug or {}).get("boxes", [])
    w, h = size
    center = result.table_center
    out: dict[str, list[float]] = {}

    def center_dist(item) -> float:
        x1, y1, x2, y2 = item["box"]
        return math.hypot((x1 + x2) / 2 / w - center[0],
                          (y1 + y2) / 2 / h - center[1])

    # 底池/上一街底池：桌心附近的筹码框，按数值挑
    near = []
    for item in boxes:
        value, _ = parse_chip_generic(item.get("text", ""))
        if value is None:
            continue
        d = center_dist(item)
        if d <= 0.20:
            near.append((d, value, item))
    near.sort(key=lambda x: x[0])
    pot_item = None
    if result.pot is not None:
        for d, value, item in near:
            if abs(value - result.pot) <= 0.01:
                pot_item = item
                break
    if pot_item is None and near:
        pot_item = near[0][2]
    if pot_item is not None:
        box = OcrBox(pot_item["text"], float(pot_item.get("score", 1.0)),
                     *[float(v) for v in pot_item["box"]], polygon=[])
        out["pot"] = _slot_from_box(box, size, GLOBAL_PAD,
                                    min_w=0.09, min_h=0.02)
        pot_bottom = pot_item["box"][3]
        pbs = [x for x in near if x[2] is not pot_item
               and x[2]["box"][3] >= pot_bottom - 2]
        if pbs:
            item = pbs[0][2]
            box = OcrBox(item["text"], float(item.get("score", 1.0)),
                         *[float(v) for v in item["box"]], polygon=[])
            out["pot_before_street"] = _slot_from_box(
                box, size, GLOBAL_PAD, min_w=0.09, min_h=0.02)

    # 盲注横幅：按 parse_blinds 找，优先靠桌心横向位置
    blind_candidates = []
    for item in boxes:
        blinds = parse_blinds(item.get("text", ""))
        if blinds is None:
            continue
        x1, y1, x2, y2 = item["box"]
        cx = (x1 + x2) / 2 / w
        blind_candidates.append((abs(cx - 0.5), item))
    if blind_candidates:
        item = min(blind_candidates, key=lambda x: x[0])[1]
        box = OcrBox(item["text"], float(item.get("score", 1.0)),
                     *[float(v) for v in item["box"]], polygon=[])
        out["blinds"] = _slot_from_box(box, size, GLOBAL_PAD,
                                       min_w=0.10, min_h=0.018)
    return out


def build_runtime_profile(result: FullTableResult,
                          size: tuple[int, int],
                          source: str = "",
                          region_y: tuple[float, float] = (0.02, 0.995),
                          thresholds: dict | None = None,
                          include_bets: bool = False) -> dict:
    """FullTableResult → Profile.load 可读的 JSON dict。"""
    w, h = size
    if not result.seats:
        raise ValueError("全图结果里没有座位，无法生成 runtime profile")

    # result.seats 已经按 direction 排好环序，不能再按 angle 升序重排（ccw 会反）
    seats = list(result.seats)
    ring_order = [s.seat_id for s in seats]
    hero = next((s for s in seats if s.is_hero), None)
    if hero is None:
        hero = max(seats, key=lambda s: s.center[1])

    stack_rects = [
        (s.stack_box.x1, s.stack_box.y1, s.stack_box.x2, s.stack_box.y2)
        for s in seats if s.stack_box is not None
    ]
    seat_payload = []
    for ring_index, seat in enumerate(seats):
        if seat.name_box is not None:
            name_slot = _slot_from_box(seat.name_box, size, NAME_PAD,
                                       min_w=0.09, min_h=0.018)
        else:
            name_slot = _fallback_slot(seat.center, 0.06, -0.06, -0.015)
        if seat.stack_box is not None:
            chip_slot = _slot_from_box(seat.stack_box, size, CHIP_PAD,
                                       min_w=0.11, min_h=0.022)
        else:
            chip_slot = _fallback_slot(seat.center, 0.05, 0.015, 0.06)
        item = {
            "seat_id": seat.seat_id,
            "ring_index": ring_index,
            "name_slot": name_slot,
            "chip_slot": chip_slot,
            "avatar_slot": _avatar_slot(seat, size),
        }
        if include_bets:
            bet = _find_bet_slot(
                seat, (result.debug or {}).get("boxes", []),
                result.table_center, size, stack_rects)
            if bet is not None:
                item["bet_slot"] = bet
        seat_payload.append(item)

    frame_area = float(w * h)
    dealer = {
        "method": "blob",
        "enabled": True,
        "blob": {
            "v_min": 170,
            "s_max": 60,
            "area_min": round(frame_area * 0.00015, 1),
            "area_max": round(frame_area * 0.003, 1),
            "circularity_min": 0.75,
            "aspect_tol": 0.4,
            "max_seat_dist": 1.2,
        },
    }

    return {
        "_comment": "runtime profile，由全图识别现场生成；不要当人工标定配置直接改",
        "_provenance": {
            "source": source,
            "size": [w, h],
            "table_size": len(seats),
            "dealer_status": result.dealer_status,
            "hero": hero.seat_id,
        },
        "runtime": True,
        "reference_size": [w, h],
        "canvas": None,
        "table_center": [round(result.table_center[0], 6),
                         round(result.table_center[1], 6)],
        "hero_seat_id": hero.seat_id,
        "action_direction": result.direction,
        "direction_verified": result.dealer_status == "found",
        "ring_order": ring_order,
        "region_gate": {"y_min": region_y[0], "y_max": region_y[1]},
        "thresholds": {**DEFAULT_THRESHOLDS, **(thresholds or {})},
        "dealer": dealer,
        "global_slots": _find_global_slots(result, size),
        "seats": seat_payload,
    }
