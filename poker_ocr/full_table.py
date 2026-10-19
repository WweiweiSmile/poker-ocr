"""全图 OCR → 座位聚类 → D 庄位 → 位置（M3.2）。

和 M2 的固定 ROI 路线不同：这里**不假设分辨率和布局一一对应**。

    1. 全图 det+rec，拿到所有文本框
    2. 文本分两类：名字 / 筹码（支持 1,234、$1,234、BB、DB/RB/B8 这类误读）
    3. 每个名字向下找最近的筹码，组成一个座位；没有名字的筹码如果落在
       座位环上，也单独成为一个座位（OCR 漏名字时仍然能定位）
    4. 以所有座位的中心估计桌心，按方位角排出顺时针环序
    5. 找白底「D」圆片，归到最近的座位；用 positions.assign_positions 推位置
    6. 最下方的座位视为 hero（浏览器/手机牌桌通常如此）

这一层只输出「这一帧看到了什么」，不做跨帧防抖。防抖/记录由调用方做。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field, fields as dc_fields
from pathlib import Path

import numpy as np

from .dealer import BLOB_DEFAULTS, BlobDealerDetector
from .fields import looks_like_name
from .layout import dedupe_boxes
from .ocr import OcrBox, OcrEngine
from .parse import Blinds, parse_blinds, parse_chip_loose
from .positions import assign_positions, positions_for_table_size


DEFAULT_UI_WORDS = frozenset({
    # 中文动作/界面
    "弃牌", "跟注", "加注", "下注", "过牌", "让牌", "免费", "限注",
    "底池", "总底池", "盲注", "庄家", "庄位", "行动", "时间", "座位",
    "空位", "坐下", "带入", "保险", "随机入座", "低水保险", "全下",
    "让或弃", "拍好运", "等待中", "准备", "暂离",
    # 英文动作/界面
    "fold", "call", "raise", "check", "bet", "all-in", "allin", "all in",
    "pot", "total pot", "dealer", "button", "sit", "sit here", "wait",
    "timer", "blinds", "ante", "show", "muck", "win", "auto", "auto-fold",
    "buy-in", "buyin", "leave", "rebuy", "disconnect", "away", "post",
    "straddle", "call amount", "min raise", "max bet",
    # 牌面/街名/牌型标签只做**精确词**过滤；不要做子串，否则「A河牌救我」会被误杀
    "手牌", "翻牌", "转牌", "河牌",
    "高牌", "一对", "两对", "三条", "顺子", "同花", "葫芦", "四条",
    "同花顺", "皇家同花顺", "牌型", "平局",
    # 位置/庄位标签，不是名字
    "d", "sb", "bb", "btn", "utg", "utg+1", "utg+2", "lj", "hj", "co",
    # 统计面板
    "桌面玩家", "玩家", "胜率", "性率", "平均底池", "全部带入",
    "剩余时间", "带入", "带人", "桌面", "统计",
})

# 出现在名字框附近、但不是名字的界面词（子串匹配，宁可多挡）
_UI_SUBSTRINGS = (
    "代入", "入座", "保险", "限制", "网址", "www", "http", "胜率", "性率",
    "入池", "带入", "带人", "桌面", "玩家", "平均底池", "全部带入",
    "剩余时间", "统计", "action", "time bank",
)


@dataclass
class FullTableConfig:
    """全图路线的可调参数。默认值先在 WPK 竖屏上验过，浏览器可另存一份。"""

    region_y: tuple[float, float] = (0.05, 0.995)
    region_x: tuple[float, float] = (0.0, 1.0)     # 排除左侧统计面板/浏览器侧栏
    table_center: tuple[float, float] | None = None
    # 名字 → 筹码的配对窗口（归一化坐标）。pair_dy_min 用来跳过与名字同高的
    # 下注预设按钮；浏览器如果筹码就画在名字旁边，可以设成 0。
    pair_dy_min: float = 0.01
    pair_dy_max: float = 0.24
    pair_dx_max: float = 0.18
    # 未配对的筹码/名字吸附到已有座位的距离（归一化欧氏距离）
    attach_dist: float = 0.14
    # 离桌心这么近的文本框一律当底池/下注区，不做座位（归一化坐标距离）
    center_radius: float = 0.18
    # 无名筹码成座：必须落在座位环附近，且不能太小（排除下注 pill）
    ring_radius_tol: float = 0.25
    min_orphan_value: float = 5.0
    min_name_len: int = 2
    min_name_score: float = 0.75          # 低分名字更可能是牌面/按钮碎片
    max_seats: int = 9                    # 德州桌最多 9 人；超出的当 UI 噪声裁掉
    direction: str = "cw"                 # cw | ccw
    dealer_ref_size: tuple[int, int] = (1080, 2400)
    dealer_blob: dict = field(default_factory=dict)
    ui_words: frozenset[str] = DEFAULT_UI_WORDS
    min_seats: int = 2


@dataclass
class SeatObservation:
    seat_id: str
    center: tuple[float, float]       # 归一化
    angle: float
    name: str | None = None
    name_score: float = 0.0
    stack: float | None = None
    stack_score: float = 0.0
    position: str | None = None
    is_dealer: bool = False
    is_hero: bool = False
    fold: bool = False
    name_box: OcrBox | None = None
    stack_box: OcrBox | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "seat_id": self.seat_id,
            "position": self.position,
            "name": self.name,
            "stack": self.stack,
            "is_dealer": self.is_dealer,
            "is_hero": self.is_hero,
            "fold": self.fold,
            "angle": round(self.angle, 1),
            "center": [round(self.center[0], 4), round(self.center[1], 4)],
        }
        if self.name_score:
            d["name_score"] = round(self.name_score, 4)
        if self.stack_score:
            d["stack_score"] = round(self.stack_score, 4)
        return d


@dataclass
class FullTableResult:
    size: tuple[int, int]
    table_center: tuple[float, float]
    seats: list[SeatObservation]
    dealer_seat_id: str | None
    dealer_status: str
    dealer_center: tuple[float, float] | None
    pot: float | None
    blinds: Blinds | None
    direction: str
    warnings: list[str]
    debug: dict = field(default_factory=dict)

    @property
    def table_size(self) -> int:
        return len(self.seats)

    def to_dict(self, include_debug: bool = True) -> dict:
        d = {
            "size": list(self.size),
            "table_size": self.table_size,
            "table_center": [round(self.table_center[0], 4),
                             round(self.table_center[1], 4)],
            "dealer": {"seat_id": self.dealer_seat_id, "status": self.dealer_status,
                       **({"center": [round(self.dealer_center[0], 4),
                                      round(self.dealer_center[1], 4)]}
                          if self.dealer_center else {})},
            "direction": self.direction,
            "pot": self.pot,
            "blinds": self.blinds.to_debug() if self.blinds else None,
            "seats": [s.to_dict() for s in self.seats],
            "warnings": self.warnings,
        }
        if include_debug:
            d["debug"] = self.debug
        return d


# ─────────────────────────── 文本分类 ───────────────────────────

def parse_chip_generic(text: str, score: float = 1.0) -> tuple[float | None, float]:
    """兼容旧名字的薄包装；实际规则在 parse.parse_chip_loose。"""
    return parse_chip_loose(text), score


def _is_name(text: str, ui_words: frozenset[str], min_len: int = 2) -> bool:
    t = (text or "").strip()
    if not t or len(t) < min_len:
        return False
    if t.lower() in ui_words or t in ui_words:
        return False
    low = t.lower()
    if any(s in low for s in _UI_SUBSTRINGS):
        return False
    return looks_like_name(t)


def _box_norm(b: OcrBox, size: tuple[int, int]) -> tuple[float, float]:
    w, h = size
    return b.cx / w, b.cy / h


def _box_dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ─────────────────────────── 座位聚类 ───────────────────────────

@dataclass
class _Chip:
    box: OcrBox
    value: float
    center: tuple[float, float]          # 像素


@dataclass
class _Name:
    box: OcrBox
    center: tuple[float, float]          # 像素


def _table_center(chips: list[_Chip], names: list[_Name],
                  cfg: FullTableConfig, size: tuple[int, int],
                  in_region: list[OcrBox]) -> tuple[float, float]:
    if cfg.table_center is not None:
        return cfg.table_center
    # 先用较大的筹码（排除下注 pill / 底池）估桌心
    values = sorted(c.value for c in chips)
    if values:
        median = values[len(values) // 2]
        cut = max(10.0, median * 0.10)
        strong = [c for c in chips if c.value >= cut]
        pts = [c.center for c in strong] or [c.center for c in chips]
    elif names:
        pts = [n.center for n in names]
    else:
        w, h = size
        return (w / 2, h / 2)
    w, h = size
    return (sum(p[0] for p in pts) / len(pts) / w,
            sum(p[1] for p in pts) / len(pts) / h)


def _pair_seats(names: list[_Name], chips: list[_Chip],
                center: tuple[float, float], cfg: FullTableConfig,
                size: tuple[int, int]) -> list[SeatObservation]:
    w, h = size
    cx, cy = center[0] * w, center[1] * h
    used_chips: set[int] = set()
    used_names: set[int] = set()
    seats: list[SeatObservation] = []

    # 名字质量：长名字优先，分数高的优先；1 个字的名字最后
    order = sorted(range(len(names)),
                   key=lambda i: (len(names[i].box.text.strip()) >= 2,
                                  names[i].box.score,
                                  len(names[i].box.text.strip())),
                   reverse=True)

    def far_from_center(chip: _Chip) -> bool:
        d = _box_dist((chip.center[0] / w, chip.center[1] / h), center)
        return d > cfg.center_radius

    for ni in order:
        nm = names[ni]
        nx, ny = _box_norm(nm.box, size)
        best = None
        for ci, ch in enumerate(chips):
            if ci in used_chips or not far_from_center(ch):
                continue
            chx, chy = _box_norm(ch.box, size)
            dx, dy = chx - nx, chy - ny
            if abs(dx) > cfg.pair_dx_max:
                continue
            # 优先名字下方；下方没有才看上方。pair_dy_min 跳过与名字同高的
            # 下注预设按钮（例如 WPK 底部的 0.5/1/2 BB 快捷钮）。
            below = cfg.pair_dy_min <= dy <= cfg.pair_dy_max
            above = -cfg.pair_dy_max <= dy <= -cfg.pair_dy_min
            if not (below or above):
                continue
            # 下方优先；同一个座位下方通常同时有「下注 pill」和「后手筹码」，
            # 后手筹码一般更大，所以按值降序选，避免把 0.5BB 的下注键当筹码。
            score = (0 if below else 1, -ch.value, abs(dy))
            if best is None or score < best[0]:
                best = (score, ci)
        if best is None:
            continue
        ci = best[1]
        used_chips.add(ci)
        used_names.add(ni)
        ch = chips[ci]
        mid = ((nm.center[0] + ch.center[0]) / 2 / w,
               (nm.center[1] + ch.center[1]) / 2 / h)
        seats.append(SeatObservation(
            seat_id="", center=mid, angle=0.0,
            name=nm.box.text.strip(), name_score=nm.box.score,
            stack=ch.value, stack_score=ch.box.score,
            name_box=nm.box, stack_box=ch.box))

    # 没有名字的筹码：吸附到已有座位，或单独成座（OCR 漏名字）
    for ci, ch in enumerate(chips):
        if ci in used_chips or not far_from_center(ch):
            continue
        chx, chy = _box_norm(ch.box, size)
        # 吸附：已经有座位的筹码槽空着才附
        best = None
        for si, seat in enumerate(seats):
            d = _box_dist((chx, chy), seat.center)
            if d <= cfg.attach_dist and (best is None or d < best[0]):
                best = (d, si)
        if best is not None:
            si = best[1]
            if seats[si].stack is None:
                seats[si].stack = ch.value
                seats[si].stack_score = ch.box.score
                seats[si].stack_box = ch.box
                seats[si].center = ((seats[si].center[0] + chx) / 2,
                                    (seats[si].center[1] + chy) / 2)
            used_chips.add(ci)
            continue
        if ch.value < cfg.min_orphan_value:
            continue
        seats.append(SeatObservation(
            seat_id="", center=(chx, chy), angle=0.0,
            stack=ch.value, stack_score=ch.box.score, stack_box=ch.box))
        used_chips.add(ci)

    # 没有筹码的名字：吸附或单独成座（筹码读不出）
    for ni, nm in enumerate(names):
        if ni in used_names:
            continue
        nx, ny = _box_norm(nm.box, size)
        attached = False
        for seat in seats:
            if _box_dist((nx, ny), seat.center) <= cfg.attach_dist:
                if seat.name is None:
                    seat.name = nm.box.text.strip()
                    seat.name_score = nm.box.score
                    seat.name_box = nm.box
                    seat.center = ((seat.center[0] + nx) / 2,
                                   (seat.center[1] + ny) / 2)
                attached = True
                break
        if not attached:
            seats.append(SeatObservation(
                seat_id="", center=(nx, ny), angle=0.0,
                name=nm.box.text.strip(), name_score=nm.box.score,
                name_box=nm.box))

    # 无名筹码成座时用「环半径」再挡一次：明显在环内的下注 pill 不要
    paired_radii = [_box_dist(s.center, center)
                    for s in seats if s.name is not None and s.stack is not None]
    if paired_radii:
        mean_r = sum(paired_radii) / len(paired_radii)
        kept = []
        for s in seats:
            if s.name is None and s.stack is not None and s.stack < cfg.min_orphan_value:
                continue
            r = _box_dist(s.center, center)
            lo = mean_r * (1 - cfg.ring_radius_tol)
            hi = mean_r * (1 + cfg.ring_radius_tol)
            if s.name is None and s.stack is not None and r < lo:
                continue
            # 只有名字没有筹码的座位必须是「环上的人」；否则是状态栏/按钮文字
            if s.name is not None and s.stack is None and not (lo <= r <= hi):
                continue
            kept.append(s)
        seats = kept

    return seats


def _seat_quality(s: SeatObservation) -> tuple:
    """全图路线噪声多时用来裁到 max_seats：名字+筹码都有 > 只有名字 > 只有筹码。"""
    return (int(s.name is not None and s.stack is not None),
            int(s.name is not None), int(s.stack is not None),
            s.stack if s.stack is not None else -1.0,
            s.name_score)


def _sort_ring(seats: list[SeatObservation], center: tuple[float, float],
               direction: str) -> list[SeatObservation]:
    def angle(s: SeatObservation) -> float:
        return math.degrees(math.atan2(s.center[1] - center[1],
                                       s.center[0] - center[0])) % 360
    for s in seats:
        s.angle = angle(s)
    ordered = sorted(seats, key=lambda s: s.angle, reverse=(direction == "ccw"))

    # 稳定 seat_id：按方位角 10° 分桶；同一桶撞了就加后缀
    seen: dict[int, int] = {}
    for s in ordered:
        bucket = int(round(s.angle / 10.0)) * 10 % 360
        n = seen.get(bucket, 0)
        seen[bucket] = n + 1
        s.seat_id = f"s{bucket:03d}" + (f"_{n}" if n else "")
    return ordered


_DEALER_GLYPHS = frozenset({"D", "0", "O", "Ø", "Ð"})


def _nearest_seat(seats: list[SeatObservation],
                  center_norm: tuple[float, float]):
    nearest, nd = None, float("inf")
    for s in seats:
        d = _box_dist(center_norm, s.center)
        if d < nd:
            nearest, nd = s, d
    return nearest, nd


def _pick_dealer(candidates: list[tuple]) -> tuple | None:
    """candidates: (nd, -confidence, seat, cx, cy)。两个不同座位距离几乎一样就算歧义。"""
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda x: (x[0], x[1]))
    if len(ordered) > 1 and abs(ordered[0][0] - ordered[1][0]) < 0.01 \
            and ordered[0][2] is not ordered[1][2]:
        return None
    return ordered[0] if ordered[0][0] <= 0.22 else None


def _detect_dealer(img: np.ndarray, engine: OcrEngine,
                   cfg: FullTableConfig, size: tuple[int, int],
                   seats: list[SeatObservation],
                   d_boxes: list[OcrBox]) -> tuple[str | None, str,
                                                   tuple[float, float] | None]:
    class _Ref:
        reference_size = cfg.dealer_ref_size

        def in_region_gate(self, pt, _size):
            return (cfg.region_x[0] <= pt[0] <= cfg.region_x[1]
                    and cfg.region_y[0] <= pt[1] <= cfg.region_y[1])

    w, h = size
    if not seats:
        return None, "not_found", None

    base = {**BLOB_DEFAULTS, **cfg.dealer_blob}
    relaxed = dict(base)
    relaxed.update({
        "v_min": max(120, base["v_min"] - 30),
        "s_max": base["s_max"] + 25,
        "circularity_min": max(0.55, base["circularity_min"] - 0.12),
        "area_min": max(20.0, base["area_min"] * 0.5),
        "area_max": base["area_max"] * 1.5,
        "aspect_tol": base["aspect_tol"] + 0.15,
    })

    verified: list[tuple] = []
    usable: list[tuple] = []
    seen: set[tuple[float, float]] = set()
    ref = _Ref()
    for attempt in (base, relaxed):
        det = BlobDealerDetector(attempt, engine=engine)
        for center, circularity, bbox in det._find_blobs(img, ref, size):
            key = (round(center[0], 1), round(center[1], 1))
            if key in seen:
                continue
            seen.add(key)
            # 全图路线允许 0/O 这种压缩后的形近读法；ROI 路线仍只认 D
            reads_d, _text, ocr_score = det._reads_d(
                img, bbox, accepted=_DEALER_GLYPHS)
            cxn, cyn = center[0] / w, center[1] / h
            seat, nd = _nearest_seat(seats, (cxn, cyn))
            if seat is None:
                continue
            conf = circularity * (max(ocr_score, 0.01) if reads_d else 0.5)
            item = (nd, -conf, seat, cxn, cyn)
            (verified if reads_d else usable).append(item)
        if verified:
            break

    # 1) 白/浅色圆片 + 认出 D（允许 0/O 形近）
    pick = _pick_dealer(verified)
    if pick is not None:
        return pick[2].seat_id, "found", (pick[3], pick[4])

    # 2) 全图 OCR 的「D」文本框
    text_items = []
    for b in d_boxes:
        cxn, cyn = b.cx / w, b.cy / h
        seat, nd = _nearest_seat(seats, (cxn, cyn))
        if seat is not None:
            text_items.append((nd, -0.5, seat, cxn, cyn))
    pick = _pick_dealer(text_items)
    if pick is not None:
        return pick[2].seat_id, "found", (pick[3], pick[4])

    # 3) 唯一/同座的浅色圆片：字没读出来但几何上只有一个候选
    if usable:
        pick = _pick_dealer(usable)
        if pick is not None:
            return pick[2].seat_id, "found", (pick[3], pick[4])
        if len({u[2].seat_id for u in usable}) == 1:
            u = sorted(usable, key=lambda x: x[0])[0]
            if u[0] <= 0.22:
                return u[2].seat_id, "found", (u[3], u[4])
        return None, "ambiguous", None
    return None, "not_found", None


def _pot_value(chips: list[_Chip], center: tuple[float, float],
               size: tuple[int, int], cfg: FullTableConfig) -> float | None:
    w, h = size
    cx, cy = center[0] * w, center[1] * h
    candidates = []
    for ch in chips:
        r = _box_dist((ch.center[0] / w, ch.center[1] / h), center)
        if r > cfg.center_radius:
            continue
        # 底池通常在桌心上方；下注 pill 在座位边
        candidates.append((ch.value, r, ch.center[1]))
    if not candidates:
        return None
    above = [c for c in candidates if c[2] <= cy]
    pool = above or candidates
    return max(pool, key=lambda c: (c[0], -c[1]))[0]


# ─────────────────────────── 主入口 ───────────────────────────

def observe_full(img: np.ndarray, engine: OcrEngine,
                 cfg: FullTableConfig | None = None) -> FullTableResult:
    cfg = cfg or FullTableConfig()
    h, w = img.shape[:2]
    size = (w, h)
    warnings: list[str] = []

    boxes = dedupe_boxes(engine.read_boxes(img))
    in_region = [b for b in boxes
                 if cfg.region_x[0] <= b.cx / w <= cfg.region_x[1]
                 and cfg.region_y[0] <= b.cy / h <= cfg.region_y[1]]
    d_boxes = [b for b in in_region if b.text.strip().upper() == "D"]

    chips: list[_Chip] = []
    names: list[_Name] = []
    for b in in_region:
        value, score = parse_chip_generic(b.text, b.score)
        if value is not None:
            chips.append(_Chip(box=b, value=value, center=(b.cx, b.cy)))
        elif (_is_name(b.text, cfg.ui_words, cfg.min_name_len)
              and b.score >= cfg.min_name_score):
            names.append(_Name(box=b, center=(b.cx, b.cy)))

    if not chips and not names:
        warnings.append("没有可用的名字/筹码文本框")
    center = _table_center(chips, names, cfg, size, in_region)
    seats = _pair_seats(names, chips, center, cfg, size)
    if len(seats) > cfg.max_seats:
        seats = sorted(seats, key=_seat_quality, reverse=True)[:cfg.max_seats]
    seats = _sort_ring(seats, center, cfg.direction)

    # 弃牌徽章：只标记，不删座位。弃牌玩家仍然占一个座位、仍然参与位置环
    fold_words = {"弃牌", "奔牌", "已弃牌", "fold", "folded"}
    for b in in_region:
        if b.text.strip().lower() not in fold_words:
            continue
        bx, by = b.cx / w, b.cy / h
        nearest, nd = None, float("inf")
        for s in seats:
            d = _box_dist((bx, by), s.center)
            if d < nd:
                nearest, nd = s, d
        if nearest is not None and nd <= 0.14:
            nearest.fold = True

    dealer_id, dealer_status, dealer_center = _detect_dealer(
        img, engine, cfg, size, seats, d_boxes)
    if dealer_id is None:
        warnings.append("没有找到可确认的 D 庄位，位置无法推导")
        for s in seats:
            s.is_dealer = False
    else:
        for s in seats:
            s.is_dealer = (s.seat_id == dealer_id)

    pos = assign_positions([s.seat_id for s in seats], dealer_id)
    if pos.ok:
        for s in seats:
            s.position = pos.positions.get(s.seat_id)
    elif dealer_id is not None:
        warnings.append(pos.error or "位置推导失败")

    # hero：最下方且最靠横向中线的座位
    hero = None
    if seats:
        max_y = max(s.center[1] for s in seats)
        bottom = [s for s in seats if s.center[1] >= max_y - 0.04]
        hero = min(bottom, key=lambda s: abs(s.center[0] - 0.5))
        hero.is_hero = True

    pot = _pot_value(chips, center, size, cfg)
    blinds = None
    for b in in_region:
        blinds = parse_blinds(b.text)
        if blinds is not None:
            break

    if len(seats) < cfg.min_seats:
        warnings.append(f"只聚出 {len(seats)} 个座位，可能不是牌桌画面")

    return FullTableResult(
        size=size,
        table_center=center,
        seats=seats,
        dealer_seat_id=dealer_id,
        dealer_status=dealer_status,
        dealer_center=dealer_center,
        pot=pot,
        blinds=blinds,
        direction=cfg.direction,
        warnings=warnings,
        debug={
            "boxes": [b.to_debug() for b in boxes],
            "chips": [{"text": c.box.text, "value": c.value,
                       "score": round(c.box.score, 4)} for c in chips],
            "names": [{"text": n.box.text, "score": round(n.box.score, 4)}
                      for n in names],
        },
    )


# ─────────────────────────── 配置与记录 ───────────────────────────

def load_full_config(path: Path | None) -> FullTableConfig:
    """从 JSON 加载配置；字段名与 FullTableConfig 一致。"""
    if path is None:
        return FullTableConfig()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    valid = {f.name for f in dc_fields(FullTableConfig)}
    unknown = set(raw) - valid
    if unknown:
        raise ValueError(f"全图配置里有未知字段：{sorted(unknown)}")
    if raw.get("table_center") is not None:
        raw["table_center"] = tuple(raw["table_center"])
    if "region_y" in raw:
        raw["region_y"] = tuple(raw["region_y"])
    if "region_x" in raw:
        raw["region_x"] = tuple(raw["region_x"])
    if "dealer_ref_size" in raw:
        raw["dealer_ref_size"] = tuple(raw["dealer_ref_size"])
    if "ui_words" in raw:
        raw["ui_words"] = frozenset(raw["ui_words"])
    return FullTableConfig(**raw)


@dataclass(frozen=True)
class FullOutcome:
    kind: str                 # record | no_change | discard
    result: FullTableResult
    record: dict | None


class FullStateRecorder:
    """全图结果的防抖/记录器。语义和 ROI 路线一致：首帧基线、稳定帧才记录。"""

    def __init__(self, stable_frames: int = 2) -> None:
        self.stable_frames = max(1, int(stable_frames))
        self.sampled = 0
        self.accepted = 0
        self.records = 0
        self.discarded: Counter = Counter()
        self.last_sig: tuple | None = None
        self.cand_sig: tuple | None = None
        self.cand_n = 0

    def push(self, result: FullTableResult, t: float,
             frame_id: str, *, extra_signature: tuple | None = None) -> FullOutcome:
        """`extra_signature` 语义同 ROI 路线：把牌面变化并进指纹。"""
        self.sampled += 1
        if result.table_size < 2:
            self.discarded["seats"] += 1
            return FullOutcome("discard", result, None)
        if result.dealer_status not in ("found", "carried") \
                or result.dealer_seat_id is None:
            self.discarded[f"dealer:{result.dealer_status}"] += 1
            return FullOutcome("discard", result, None)

        sig = self.signature(result)
        if extra_signature is not None:
            sig = (sig, extra_signature)
        self.accepted += 1
        record = None
        if self.last_sig is None:
            record = self.payload(result, t, frame_id, self.records + 1)
        elif sig == self.cand_sig:
            self.cand_n += 1
            if self.cand_n >= self.stable_frames and sig != self.last_sig:
                record = self.payload(result, t, frame_id, self.records + 1)
        else:
            self.cand_sig, self.cand_n = sig, 1

        if record is not None:
            self.last_sig = sig
            self.records += 1
            return FullOutcome("record", result, record)
        return FullOutcome("no_change", result, None)

    @staticmethod
    def signature(result: FullTableResult) -> tuple:
        blinds = (result.blinds.sb, result.blinds.bb) if result.blinds else None
        seats = tuple(
            (s.seat_id, s.position, s.name,
             round(s.stack, 2) if s.stack is not None else None,
             s.is_dealer, s.is_hero, s.fold)
            for s in sorted(result.seats, key=lambda x: x.seat_id)
        )
        return (result.table_size, result.dealer_seat_id,
                round(result.pot, 2) if result.pot is not None else None,
                blinds, seats)

    @staticmethod
    def payload(result: FullTableResult, t: float,
                frame_id: str, index: int) -> dict:
        d = {
            "index": index,
            "t": round(t, 2),
            "frame": frame_id,
            "table_size": result.table_size,
            "dealer": result.dealer_seat_id,
            "dealer_status": result.dealer_status,
            "pot": result.pot,
            "blinds": result.blinds.to_debug() if result.blinds else None,
            "seats": [s.to_dict() for s in result.seats],
        }
        # 翻前行动顺序：positions 表本身就是按行动顺序排的（SB 先说话）
        by_pos = {s.position: s.seat_id for s in result.seats if s.position}
        d["action_order"] = [
            {"position": p, "seat_id": by_pos.get(p)}
            for p in positions_for_table_size(result.table_size)
        ]
        if result.warnings:
            d["warnings"] = result.warnings
        return d

    def reset(self) -> None:
        """断流重连后清跨帧状态；累计计数保留。"""
        self.last_sig = None
        self.cand_sig = None
        self.cand_n = 0

    def summary(self) -> dict:
        return {
            "sampled_frames": self.sampled,
            "accepted_frames": self.accepted,
            "discarded_frames": self.sampled - self.accepted,
            "discarded_reasons": dict(self.discarded),
            "record_count": self.records,
        }


class DealerTracker:
    """跨帧庄位跟踪：D 暂时没读到时沿用上一帧，最多 max_carry 帧。

    庄位一手只变一次，而 D 圆片会被动画/遮挡/压缩间歇性读不到。只要连续几帧
    找不到 D 就丢掉整帧，识别率会很低。这里做有限次数的沿用；每帧仍会重新
    推位置，输出里 `dealer_status="carried"`，消费端可以决定要不要信。
    """

    def __init__(self, max_carry: int = 4) -> None:
        self.max_carry = max(0, int(max_carry))
        self.last_seat_id: str | None = None
        self.last_name: str | None = None
        self.last_center: tuple[float, float] | None = None
        self.miss = 0

    def reset(self) -> None:
        self.last_seat_id = None
        self.last_name = None
        self.last_center = None
        self.miss = 0

    def apply(self, result: FullTableResult) -> FullTableResult:
        if result.dealer_status == "found" and result.dealer_seat_id:
            seat = next((s for s in result.seats
                         if s.seat_id == result.dealer_seat_id), None)
            if seat is not None:
                self.last_seat_id = seat.seat_id
                self.last_name = seat.name
                self.last_center = seat.center
                self.miss = 0
            return result

        if (self.max_carry <= 0 or self.miss >= self.max_carry
                or (self.last_seat_id is None and self.last_name is None)):
            return result
        if not result.seats:
            return result

        seat = None
        if self.last_seat_id:
            seat = next((s for s in result.seats
                         if s.seat_id == self.last_seat_id), None)
        if seat is None and self.last_name:
            matches = [s for s in result.seats if s.name == self.last_name]
            if len(matches) == 1:
                seat = matches[0]
        if seat is None and self.last_center:
            nearest = min(result.seats,
                          key=lambda s: _box_dist(s.center, self.last_center),
                          default=None)
            if nearest is not None \
                    and _box_dist(nearest.center, self.last_center) <= 0.12:
                seat = nearest
        if seat is None:
            self.miss += 1
            return result

        for s in result.seats:
            s.is_dealer = (s.seat_id == seat.seat_id)
        result.dealer_seat_id = seat.seat_id
        result.dealer_status = "carried"
        result.dealer_center = seat.center
        result.warnings.append("D 暂未读到，庄位沿用上一帧")
        pos = assign_positions([s.seat_id for s in result.seats], seat.seat_id)
        if pos.ok:
            for s in result.seats:
                s.position = pos.positions.get(s.seat_id)
        self.miss += 1
        return result
