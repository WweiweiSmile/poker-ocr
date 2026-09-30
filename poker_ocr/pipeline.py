"""单帧 → FrameResult：串起 OCR、槽位分配、字段解析、占用判定、庄位。

输出刻意做成**可合并**的：每个座位带稳定的 seat_id 和逐字段 score。
调用方拿一叠单帧结果自己按 seat_id 合并即可（名字粘性、跨帧投票之类），
不需要这个模块知道「上一帧」是什么。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path

import cv2
import numpy as np

from .dealer import DealerDetector, DealerResult
from .fields import NameResult, read_name, parse_chip
from .layout import Assignment, assign_boxes
from .ocr import OcrEngine
from .positions import PositionAssignment, assign_positions
from .profile import Profile, Size

OCCUPIED = "occupied"
EMPTY = "empty"
UNCERTAIN = "uncertain"

# 名字裁剪在检测框外留白的比例。
#
# 这个值不影响**文本**（名字文本由全图检测决定，见 fields.read_name），
# 只影响阶梯读得好不好，进而影响分数和「要不要复核」的标记。
# 实测（table.jpg + table1.jpg 共 18 个名字）留白对正确率的影响是非单调的：
# 0.05 和 0.25 全对，0.08 / 0.20 / 0.35 各错一个 —— 说明这个维度上差异多半是
# 识别器的噪声，不必精调。取 0.15 是中间值，留足笔画边缘又不会把小号后缀缩没。
NAME_CROP_PAD = 0.15


@dataclass
class SeatState:
    ring_index: int
    seat_id: str
    name: str | None = None
    name_score: float = 0.0
    name_review: bool = False
    name_variants: list[dict] = dc_field(default_factory=list)
    stack: int | float | None = None
    unit: str | None = None
    stack_score: float = 0.0
    stack_corrected: bool = False
    status: str = UNCERTAIN
    is_dealer: bool | None = None
    position: str | None = None          # SB / BB / UTG / ... / BTN，由庄位推出
    evidence: dict = dc_field(default_factory=dict)

    def to_debug(self) -> dict:
        return {
            "ring_index": self.ring_index,
            "seat_id": self.seat_id,
            "position": self.position,
            "name": self.name,
            "name_score": round(self.name_score, 4),
            "name_review": self.name_review,
            "stack": self.stack,
            "unit": self.unit,
            "stack_score": round(self.stack_score, 4),
            "status": self.status,
            "is_dealer": self.is_dealer,
            **({"name_variants": self.name_variants} if self.name_variants else {}),
            **({"stack_corrected": True} if self.stack_corrected else {}),
            **({"evidence": self.evidence} if self.evidence else {}),
        }


@dataclass
class FrameResult:
    frame_id: str
    size: Size
    direction: str
    direction_verified: bool
    dealer: DealerResult
    table_size: int
    seats: list[SeatState]
    valid: bool
    violations: list[str]
    debug: dict = dc_field(default_factory=dict)

    def to_dict(self, include_debug: bool = True) -> dict:
        payload = {
            "frame_id": self.frame_id,
            "size": list(self.size),
            "direction": self.direction,
            "direction_verified": self.direction_verified,
            "table_size": self.table_size,
            "dealer": self.dealer.to_debug(),
            "valid": self.valid,
            "violations": self.violations,
            "seats_ring_from_hero": [s.to_debug() for s in self.seats],
        }
        if include_debug:
            payload["debug"] = self.debug
        return payload


# ─────────────────────────── 占用判定 ───────────────────────────

def _name_crop_from_boxes(name_boxes, size: Size,
                          pad: float = NAME_CROP_PAD) -> tuple[int, int, int, int]:
    """按**检测框**裁名字。

    检测框就是这段文字的实际范围，按它裁既不会切掉长名字的开头，也不会留多余空白。

    留白要克制：**别和加宽的静态槽位取并集**。并集之后裁剪区宽了近一倍，四周全是毛毡，
    识别器会把小号的后缀丢掉 —— 实测「悍将v1」变成「悍将」（v1 只有 3 个字符宽，
    缩小占比后就被忽略了）。同理庄位的「D」留白一多会被读成「·」。
    """
    x1 = min(b.x1 for b in name_boxes)
    y1 = min(b.y1 for b in name_boxes)
    x2 = max(b.x2 for b in name_boxes)
    y2 = max(b.y2 for b in name_boxes)
    dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
    w, h = size
    return (max(0, int(x1 - dx)), max(0, int(y1 - dy)),
            min(w, int(x2 + dx)), min(h, int(y2 + dy)))


def _region_std(gray: np.ndarray, rect: tuple[int, int, int, int]) -> float:
    x1, y1, x2, y2 = rect
    patch = gray[y1:y2, x1:x2]
    if patch.size == 0:
        return 0.0
    return float(patch.std())


def judge_occupancy(name: NameResult, chip_ok: bool, avatar_std: float,
                    chip_std: float, thresholds: dict) -> tuple[str, dict]:
    """三态判定，多信号投票 + 保守偏置。

    信号 A/B（强）：筹码能解析、名字能读出 —— 命中就是有人
    信号 C/D（中）：头像区、筹码区的灰度方差 —— 照片/胶囊底片远高于纯毛毡
                    （实测有人时筹码区 std 65-77、头像区 32-72，纯毛毡 4.7）

    为什么要三态：「空座位」和「有人但筹码是 0」（全下/刚输光）是两回事，
    都输出 0 的话下游分不清。不确定的宁可报 uncertain 也不猜。
    """
    evidence = {
        "chip_read": chip_ok,
        "name_read": name.ok,
        "avatar_std": round(avatar_std, 2),
        "chip_std": round(chip_std, 2),
    }
    if chip_ok or name.ok:
        return OCCUPIED, evidence

    visual = (avatar_std >= thresholds.get("avatar_var_min", 18.0)
              or chip_std >= thresholds.get("chip_pill_contrast_min", 25.0))
    evidence["visual_hit"] = visual
    if visual:
        # 看得出有东西但读不出字 —— 可能是头像占位图、也可能是全下状态
        return UNCERTAIN, evidence
    return EMPTY, evidence


# ─────────────────────────── 主流程 ───────────────────────────

def extract(img: np.ndarray, profile: Profile, engine: OcrEngine,
            dealer_detector: DealerDetector, frame_id: str,
            use_ladder: bool = True) -> FrameResult:
    size: Size = (img.shape[1], img.shape[0])
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    th = profile.thresholds

    boxes = engine.read_boxes(img)
    assignment: Assignment = assign_boxes(boxes, profile, size)

    dealer = dealer_detector.detect(img, profile, size)

    seats: list[SeatState] = []
    for ring_index, seat in enumerate(profile.seats_in_ring_order):
        slot = assignment.by_seat[seat.seat_id]

        # 筹码
        chip_text = slot.chip_text()
        chip = parse_chip(chip_text, slot.chip_boxes[0].score) if chip_text else None

        # 名字：全图检测的原文当 L0，阶梯在检测框裁剪上补强
        name_res = NameResult(None, 0.0, ok=False, error="没有可用的名字区域")
        seed_text, seed_score, crop = None, 0.0, None
        if slot.name_boxes:
            crop = _name_crop_from_boxes(slot.name_boxes, size)
            seed_text = slot.name_text()
            seed_score = min(b.score for b in slot.name_boxes)
        elif chip is not None and chip.ok:
            # 筹码读出来了说明这个座位有人，但检测没框到名字。
            # 这时才动用（已加宽的）静态槽位自己识别一遍 —— 加宽的意义就在这里。
            # 只在「确定有人」时才补，否则空座位会被识别出幻觉名字。
            crop = profile.slot_to_px(seat.name_slot, size, pad=0.06)

        if crop is not None:
            x1, y1, x2, y2 = crop
            roi = img[y1:y2, x1:x2]
            if roi.size:
                name_res = read_name(engine, roi, th.get("name_min_score", 0.7),
                                     seed_text=seed_text, seed_score=seed_score)
                if not use_ladder and name_res.variants:
                    # --no-ladder：只认 L0，用于排查阶梯有没有帮倒忙
                    first = name_res.variants[0]
                    name_res.text, name_res.score = first.text, first.score
                    name_res.variants = [first]

        status, evidence = judge_occupancy(
            name_res,
            chip_ok=bool(chip and chip.ok),
            avatar_std=_region_std(gray, profile.slot_to_px(seat.avatar_slot, size)),
            chip_std=_region_std(gray, profile.slot_to_px(seat.chip_slot, size)),
            thresholds=th,
        )

        state = SeatState(
            ring_index=ring_index,
            seat_id=seat.seat_id,
            name=name_res.text if name_res.ok else None,
            name_score=name_res.score,
            name_review=name_res.review,
            name_variants=[v.to_debug() for v in name_res.variants],
            status=status,
            evidence=evidence,
        )

        if status == EMPTY:
            state.stack = 0                       # 空座位按需求返回 0
        elif chip is not None and chip.ok:
            state.stack = chip.value
            state.unit = chip.unit
            state.stack_score = chip.score
            state.stack_corrected = chip.corrected
        else:
            # 读到了但单位不对/洗不出数字 —— 值留空、保留错误原因供复核
            state.stack = None
            if chip is not None:
                state.stack_score = chip.score
                if chip.error:
                    evidence = {**evidence, "chip_error": chip.error}
                    state.evidence = evidence
            if status == OCCUPIED:
                state.status = UNCERTAIN       # 有人但筹码没读懂

        state.is_dealer = (dealer.seat_id == seat.seat_id) if dealer.status == "found" else None
        seats.append(state)

    # 位置要等占用判定跑完才知道人数 —— 空座位不发牌，不占位置
    occupied_ring = [s.seat_id for s in seats if s.status != EMPTY]
    pos = assign_positions(occupied_ring, dealer.seat_id if dealer.status == "found" else None)
    if pos.ok:
        for s in seats:
            s.position = pos.positions.get(s.seat_id)

    violations = _check_invariants(seats, dealer, pos)

    return FrameResult(
        frame_id=frame_id,
        size=size,
        direction=profile.action_direction,
        direction_verified=profile.direction_verified,
        dealer=dealer,
        table_size=pos.table_size,
        seats=seats,
        valid=not violations,
        violations=violations,
        debug={
            "noise_boxes": [b.to_debug() for b in assignment.noise],
            "gated_out": [b.to_debug() for b in assignment.gated_out],
            "detected_boxes": len(boxes),
            **({"position_error": pos.error} if pos.error else {}),
        },
    )


def _check_invariants(seats: list[SeatState], dealer: DealerResult,
                      pos: PositionAssignment) -> list[str]:
    """牌局层面必须成立的事。违反就报出来，不静默。"""
    v: list[str] = []
    if len(seats) != 9:
        v.append(f"座位数应为 9，实际 {len(seats)}")
    if sorted(s.ring_index for s in seats) != list(range(9)):
        v.append("ring_index 不齐")

    # 没有庄位时位置自然也定不出来，但那是同一个根因，不重复报一遍
    if not pos.ok and dealer.status == "found":
        v.append(f"位置没能定出来：{pos.error}")
    else:
        named = [s for s in seats if s.position]
        if len(named) != pos.table_size:
            v.append(f"拿到位置的座位数 {len(named)} 与桌号 {pos.table_size} 不符")
        dup = {p for p in pos.positions.values() if
               list(pos.positions.values()).count(p) > 1}
        if dup:
            v.append(f"位置重复：{sorted(dup)}")
        if dealer.status == "found" and pos.table_size > 2:
            # 单挑时庄家兼任 SB，位置名不是 BTN，所以只在 3 人以上查
            d = next((s for s in seats if s.is_dealer), None)
            if d and d.position != "BTN":
                v.append(f"庄家 {d.seat_id} 的位置应是 BTN，实际 {d.position}")

    dealer_seats = [s.seat_id for s in seats if s.is_dealer]
    if dealer.status == "found" and len(dealer_seats) != 1:
        v.append(f"庄位应有且仅有 1 个，实际 {len(dealer_seats)}")
    if dealer.status == "ambiguous":
        v.append("庄位检测有歧义，未定")
    if dealer.status == "not_found":
        v.append("庄位未找到（当前帧可能没有渲染庄位，或用 --dealer-seat 指定）")

    for s in seats:
        if s.status == OCCUPIED and s.stack is None:
            v.append(f"{s.seat_id}: 判定有人但筹码读不出来")
    return v


# ─────────────────────────── 调试图 ───────────────────────────

def render_slots(img: np.ndarray, profile: Profile, only: bool = True) -> np.ndarray:
    """把槽位画在原图上（原生分辨率，不 resize）。"""
    size: Size = (img.shape[1], img.shape[0])
    vis = img.copy()
    for seat in profile.seats:
        for slot, color in ((seat.name_slot, (0, 255, 0)),
                            (seat.chip_slot, (0, 255, 255)),
                            (seat.avatar_slot, (255, 0, 255))):
            x1, y1, x2, y2 = profile.slot_to_px(slot, size, pad=0.06)
            cv2.rectangle(vis, (x1, y1), (x2, y2), color, 3)
        nx1, ny1, _, _ = profile.slot_to_px(seat.name_slot, size)
        cv2.putText(vis, f"{seat.ring_index}:{seat.seat_id}", (nx1, max(30, ny1 - 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 3)
    cx, cy = profile.point_to_px(profile.table_center, size)
    cv2.drawMarker(vis, (int(cx), int(cy)), (255, 0, 0), cv2.MARKER_CROSS, 60, 4)
    return vis


def render_contact_sheet(img: np.ndarray, profile: Profile, result: FrameResult,
                         out_dir: Path) -> None:
    """9 个名字裁剪拼一张接触表 —— 人眼扫一遍约 10 秒，比读 JSON 划算。"""
    size: Size = (img.shape[1], img.shape[0])
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = []
    for state in result.seats:
        seat = profile.seat(state.seat_id)
        x1, y1, x2, y2 = profile.slot_to_px(seat.name_slot, size, pad=0.10)
        tile = cv2.copyMakeBorder(img[y1:y2, x1:x2], 34, 6, 6, 6,
                                  cv2.BORDER_CONSTANT, value=(32, 32, 32))
        label = f"{state.ring_index} {state.name or '-'} {state.name_score:.2f}"
        cv2.putText(tile, label, (6, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 0) if not state.name_review else (0, 165, 255), 2)
        cv2.imwrite(str(out_dir / f"name_{state.ring_index}_{state.seat_id}.png"), tile)
        tiles.append(cv2.resize(tile, (420, 60)))

    if tiles:
        cols = 1
        rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
        max_w = max(r.shape[1] for r in rows)
        rows = [cv2.copyMakeBorder(r, 0, 0, 0, max_w - r.shape[1],
                                   cv2.BORDER_CONSTANT, value=(32, 32, 32)) for r in rows]
        cv2.imwrite(str(out_dir / "_contact_sheet.png"), np.vstack(rows))
