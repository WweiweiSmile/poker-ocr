"""公共牌 / hero 手牌识别（图形化牌面）。

背景：WPK 移动端的牌面是**图形化**的，OCR 只能读出数字，花色和 J/Q/K/A 读不出
（见 config/table_wpk_1080x2400.json 的 _sample_notes.hero_cards）。所以读牌必须
单独做，不能指望 OCR。

两件事分别用两种特征来判，互为校验：

  1. **花色看颜色 + 形状。**
     这套 UI 的四色是固定的：♠ 黑、♥ 红、♣ 绿、♦ 蓝。
     颜色在 HSV 下分离得很干净；形状再独立判一次：
       ♦ 菱形   —— 实心度高、上下对称
       ♥ 红桃   —— 上宽下尖，质心偏上
       ♣ 梅花   —— 三叶，实心度低
       ♠ 黑桃   —— 上尖下宽，质心偏下
     两者一致才给高置信度；不一致时以颜色为准并记 warning —— 颜色是用户明确
     给出的判据，形状只用来兜住「颜色阈值跨设备不稳」的情况。

  2. **点数看字形模板。**
     点数字形与花色无关（K♠ 和 K♥ 的 K 是同一个字形），所以每个点数只需要
     一张模板。模板按紧外接框裁出、按高度归一化后做 NCC 匹配，天然跨花色、
     跨亮/暗状态（hero 弃牌后牌面会变灰）。模板图集由
     tools/build_card_templates.py 从标注样本生成。

  3. **摊牌亮牌。**
     对手主动秀牌/摊牌时，两张牌画在该座的 action_slot 区域；两张牌常常
     紧贴成一个白色轮廓，先沿中间的缝切开，再走同一套花色 + 点数识别。

版式：优先用 profile 的 global_slots（board / hero_cards）。配置里没有时退回
WPK 移动端的默认归一化坐标 —— 默认坐标是 0-1 相对值，换分辨率不用改。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field as dc_field
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .profile import Profile, Size, Slot

# ─────────────────────────── 常量 ───────────────────────────

RANKS: tuple[str, ...] = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")

SUIT_NAMES = {
    "s": "spades",
    "h": "hearts",
    "c": "clubs",
    "d": "diamonds",
}

# WPK 移动端的默认牌区（归一化 x1,y1,x2,y2）。
# 与 config/table_wpk_1080x2400.json 的 global_slots 一致；profile 里有时以 profile 为准。
# 之所以内置一份：api.py 默认加载的是 table_default.json（浏览器版式，没有牌区），
# 不内置的话 /ocr/table 对这批 1080x2400 截图永远读不到牌。
DEFAULT_CARD_SLOTS: dict[str, Slot] = {
    "board": (0.21, 0.448, 0.79, 0.528),
    "hero_cards": (0.38, 0.862, 0.62, 0.94),
}

# 牌面底：白/灰、低饱和。V>=90 能兜住「hero 弃牌后变灰」的牌面（实测 V≈103），
# 同时把绿色毛毡（S 很高）挡在外面。
_CARD_MIN_VALUE = 90
_CARD_MAX_SAT = 60

# 单张牌的几何约束。实测 1080x2400 下牌面 114x168，宽高比 0.68。
_CARD_MIN_AREA_FRAC = 0.04     # 占 ROI 面积的下限；D 庄位圆片远小于它
_CARD_HEIGHT_FRAC = 0.60       # 牌高至少占 ROI 高度的 60%
_CARD_ASPECT = (0.50, 0.95)
# 摊牌时两张亮出的手牌常常紧贴在一起，白色牌面会被闭运算连成一个轮廓，
# 宽高比约等于单张的 2 倍（实测 AQ 样张 199x144 ≈ 1.38）。这个区间用来
# 识别「两张牌粘在一起」，再沿中间的缝切开。
_CARD_PAIR_ASPECT = (1.05, 1.90)
_MAX_CARDS = 5

# 花色符号区（占牌面的比例）：下半部中间，避开左上角的点数。
_SUIT_Y = (0.40, 0.92)
_SUIT_X = (0.10, 0.90)

# 颜色掩码（OpenCV HSV：H 0-179）。四色分离实测计数差在 10 倍以上。
_BLACK = "black"
_RED = "red"
_GREEN = "green"
_BLUE = "blue"


def _color_masks(hsv: np.ndarray) -> dict[str, np.ndarray]:
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return {
        _BLACK: (v <= 85) & (s <= 90),
        _RED: ((h <= 10) | (h >= 170)) & (s >= 110) & (v >= 60),
        _GREEN: (h >= 35) & (h <= 89) & (s >= 90) & (v >= 40),
        _BLUE: (h >= 90) & (h <= 135) & (s >= 110) & (v >= 60),
    }


_COLOR_TO_SUIT = {_BLACK: "s", _RED: "h", _GREEN: "c", _BLUE: "d"}

# 点数模板图集
_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_TEMPLATE_ATLAS = _ASSET_DIR / "card_rank_templates.png"
_GLYPH_W, _GLYPH_H = 52, 44
_GLYPH_INNER_H = 36

# 置信度门槛。实测 56 张牌 best NCC 最低 0.925、best-second 最低 0.055，
# 门槛留足余量，只用来拦真正异常的帧，不误伤正常牌。
_MIN_RANK_SCORE = 0.70
_MIN_RANK_MARGIN = 0.03
_MIN_SUIT_INK = 200

# 稳定闸用的「这张牌本身可不可信」门槛。标注样本里最低 confidence/rank_score
# 都是 0.795，0.65 留足余量；真正的牌背实测 confidence≈0.50、rank_score≈0.64。
_MIN_STABLE_CONFIDENCE = 0.65

# 牌背分类：POKER 牌背的白色边框仍会被检出成牌面矩形，但内部是大面积高饱和
# 图案（实测 S 中位数≈150、高饱和像素占 82%–85%，V 中位数≈170）；真牌是白底，
# 只有花色墨迹高饱和（占比≈20%，S 中位数≈5，V 中位数≈246）。
_CARD_BACK_SAT_FRAC = 0.55
_CARD_BACK_SAT_MEDIAN = 80.0


# ─────────────────────────── 结果结构 ───────────────────────────

@dataclass(frozen=True)
class Card:
    """一张识别出的牌。rect 是**原图**像素坐标。"""

    rank: str
    suit: str
    confidence: float
    rank_score: float
    rect: tuple[int, int, int, int]
    evidence: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def code(self) -> str:
        """紧凑表示：Ad / 10s / Kh。"""
        return f"{self.rank}{self.suit}"

    def to_dict(self, include_evidence: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "code": self.code,
            "rank": self.rank,
            "suit": self.suit,
            "suit_name": SUIT_NAMES[self.suit],
            "confidence": round(self.confidence, 4),
            "rank_score": round(self.rank_score, 4),
            "rect": list(self.rect),
        }
        if include_evidence:
            d["evidence"] = self.evidence
        return d


@dataclass
class CardSlotRead:
    """一块牌区（公共牌 / hero 手牌）的读数。"""

    name: str
    slot: Slot
    rect: tuple[int, int, int, int]
    cards: list[Card] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [c.code for c in self.cards]

    def to_dict(self, include_details: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "slot": [round(float(x), 4) for x in self.slot],
            "rect": list(self.rect),
            "cards": self.codes,
        }
        if include_details:
            d["details"] = [c.to_dict(include_evidence=True) for c in self.cards]
        if self.warnings:
            d["warnings"] = self.warnings
        return d


@dataclass
class CardsResult:
    """一帧的公共牌 + hero 手牌 + 摊牌/主动秀出的对手手牌。"""

    size: Size
    board: CardSlotRead
    hero_cards: CardSlotRead
    # seat_id → 该座亮出的两张牌。只有识别到正好 2 张、点数都读出来才会进来。
    shows: dict[str, CardSlotRead] = dc_field(default_factory=dict)
    warnings: list[str] = dc_field(default_factory=list)
    # 稳定结果是否可靠：张数合法、没有 ?/低置信度/牌背/遮挡。AI 建议吃这个标志。
    reliable: bool = True

    @property
    def board_codes(self) -> list[str]:
        return self.board.codes

    @property
    def hero_codes(self) -> list[str]:
        return self.hero_cards.codes

    @property
    def show_codes(self) -> dict[str, list[str]]:
        return {seat_id: read.codes for seat_id, read in self.shows.items()}

    def to_dict(self, include_details: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "size": list(self.size),
            "board": self.board_codes,
            "hero_cards": self.hero_codes,
            "reliable": self.reliable,
        }
        if include_details:
            d["board_read"] = self.board.to_dict()
            d["hero_cards_read"] = self.hero_cards.to_dict()
        if self.shows:
            d["shows"] = {
                seat_id: read.to_dict(include_details=include_details)
                for seat_id, read in self.shows.items()
            }
        if self.warnings:
            d["warnings"] = self.warnings
        return d


# ─────────────────────────── 牌面切分 ───────────────────────────

def _detect_card_rects(roi: np.ndarray, *,
                       split_pairs: bool = False) -> list[tuple[int, int, int, int]]:
    """在 ROI 里找出牌面矩形，按从左到右排序。

    牌面是白/灰低饱和，毛毡是绿色高饱和 —— 这个差异很稳。只取外轮廓
    （RETR_EXTERNAL），再用「面积 + 宽高比 + 高度」三道几何门过滤，
    不做颜色以外的内容判断。

    `split_pairs=True` 时，把「两张牌粘成一个宽轮廓」的情况沿中间的缝切开。
    摊牌亮牌区（座位 action_slot）里两张牌往往只隔几个像素，5x5 闭运算会把
    它们连成一块；公共牌/hero 牌的间距更大，保持默认 False 不受影响。
    """
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = ((hsv[..., 2] >= _CARD_MIN_VALUE) & (hsv[..., 1] <= _CARD_MAX_SAT))
    mask = mask.astype(np.uint8) * 255
    # 闭运算填掉牌面里的字/花色造成的小洞；5x5 不会把相邻牌之间的缝（约 8px）连上
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    roi_h, roi_w = roi.shape[:2]
    min_area = _CARD_MIN_AREA_FRAC * roi_w * roi_h

    rects: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < min_area:
            continue
        if h < roi_h * _CARD_HEIGHT_FRAC:
            continue
        aspect = w / max(1, h)
        if _CARD_ASPECT[0] < aspect < _CARD_ASPECT[1]:
            rects.append((x, y, w, h))
            continue
        if not (split_pairs and _CARD_PAIR_ASPECT[0] <= aspect <= _CARD_PAIR_ASPECT[1]):
            continue

        # 在轮廓中间 ±15% 宽度范围内找白色像素最少的一列，就是两张牌之间的缝；
        # 找不到明显低谷时退回几何中点。
        sub = (mask[y:y + h, x:x + w] > 0)
        cols = sub.sum(axis=0)
        mid = w // 2
        lo = max(1, mid - int(w * 0.15))
        hi = min(w - 1, mid + int(w * 0.15))
        split = mid
        if hi > lo:
            k = lo + int(np.argmin(cols[lo:hi]))
            if cols[k] < 0.5 * max(1, cols[mid]):
                split = k
        for rx, rw in ((x, split), (x + split, w - split)):
            if rw <= 0:
                continue
            half_aspect = rw / max(1, h)
            if _CARD_ASPECT[0] < half_aspect < _CARD_ASPECT[1]:
                rects.append((rx, y, rw, h))

    rects.sort(key=lambda r: r[0])
    return rects[:_MAX_CARDS]


def _canonical_card(roi: np.ndarray, rect: tuple[int, int, int, int]) -> np.ndarray:
    """裁出单张牌并缩放到固定尺寸，让后续特征与牌的实际像素大小无关。"""
    x, y, w, h = rect
    return cv2.resize(roi[y:y + h, x:x + w], (115, 168), interpolation=cv2.INTER_AREA)


# ─────────────────────────── 花色：颜色 ───────────────────────────

def _suit_color(card: np.ndarray) -> tuple[str, float, dict[str, Any]]:
    """按颜色判花色，返回 (suit, confidence, evidence)。"""
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    masks = _color_masks(hsv)
    h, w = card.shape[:2]
    ys = slice(int(h * _SUIT_Y[0]), int(h * _SUIT_Y[1]))
    xs = slice(int(w * _SUIT_X[0]), int(w * _SUIT_X[1]))

    counts = {name: int(m[ys, xs].sum()) for name, m in masks.items()}
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    best_name, best = ordered[0]
    second = ordered[1][1]
    total = sum(counts.values())

    # 比例看「赢得多不多」，间距看「赢得稳不稳」；两者都高才可信
    ratio = best / max(1, total)
    margin = (best - second) / max(1, best)
    confidence = 0.6 * ratio + 0.4 * margin
    if best < _MIN_SUIT_INK:
        confidence *= 0.3

    return _COLOR_TO_SUIT[best_name], float(confidence), {
        "color_counts": counts,
        "color_suit": _COLOR_TO_SUIT[best_name],
        "ink_pixels": best,
    }


# ─────────────────────────── 花色：形状 ───────────────────────────

def _suit_shape(card: np.ndarray) -> tuple[str | None, dict[str, Any]]:
    """独立于颜色，按花色符号的轮廓形状判一次。

    只看下半部的符号区，并取所有颜色墨迹的并集 —— 这样形状与颜色是两条独立的
    证据，可以互相校验。符号是连通的单块，取最大轮廓即可。
    """
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    h, w = card.shape[:2]
    region = np.zeros((h, w), dtype=bool)
    region[int(h * _SUIT_Y[0]):int(h * _SUIT_Y[1]),
           int(w * _SUIT_X[0]):int(w * _SUIT_X[1])] = True

    union = np.zeros((h, w), dtype=bool)
    for m in _color_masks(hsv).values():
        union |= m
    mask = ((union & region).astype(np.uint8)) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, {}
    c = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(c))
    if area < 200:
        return None, {"shape_area": area}

    x, y, bw, bh = cv2.boundingRect(c)
    solidity = area / max(1.0, float(cv2.contourArea(cv2.convexHull(c))))
    approx = cv2.approxPolyDP(c, 0.04 * cv2.arcLength(c, True), True)
    moments = cv2.moments(c)
    cy = moments["m01"] / moments["m00"] if moments["m00"] else y + bh / 2
    # 质心相对外接框中心的纵向偏移：>0 偏下（黑桃：上尖下宽），<0 偏上（红桃：上宽下尖）
    rel = (cy - (y + bh / 2)) / max(1.0, float(bh))

    evidence = {
        "shape_area": round(area, 1),
        "shape_solidity": round(solidity, 3),
        "shape_vertices": int(len(approx)),
        "shape_centroid_rel": round(rel, 3),
    }

    # 四条规则按「最不可能误判」的顺序排：
    # 菱形实心度高且上下对称；红桃质心明显偏上；梅花实心度低；剩下的是黑桃
    if solidity >= 0.94 and abs(rel) < 0.05:
        return "d", evidence
    if rel <= -0.05:
        return "h", evidence
    if solidity < 0.86:
        return "c", evidence
    return "s", evidence


def _classify_suit(card: np.ndarray) -> tuple[str, float, dict[str, Any]]:
    color_suit, color_conf, evidence = _suit_color(card)
    shape_suit, shape_ev = _suit_shape(card)
    evidence.update(shape_ev)
    evidence["shape_suit"] = shape_suit

    if shape_suit is None:
        # 形状没抽到符号：颜色仍然可用，但要标出来，别让下游以为是双证据
        evidence["shape_agrees"] = None
        return color_suit, color_conf * 0.8, evidence

    evidence["shape_agrees"] = shape_suit == color_suit
    if shape_suit != color_suit:
        # 颜色是用户给定的硬判据，形状只做校验。不一致时降置信度，让调用方能复核
        return color_suit, color_conf * 0.5, evidence
    return color_suit, color_conf, evidence


# ─────────────────────────── 点数：字形模板 ───────────────────────────

def _ink_mask(card: np.ndarray, suit: str) -> np.ndarray:
    """点数字形的墨迹掩码。颜色与花色一致；黑桃的字是黑的。"""
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    masks = _color_masks(hsv)
    # 点数提取比花色计数放宽一点：字形细，抗锯齿像素容易掉在阈值外
    for name, m in masks.items():
        if _COLOR_TO_SUIT[name] == suit:
            mask = m
            break
    else:
        return np.zeros(card.shape[:2], np.uint8)

    m = mask.astype(np.uint8) * 255
    h, w = m.shape
    # 去掉牌面边缘的描边/阴影，避免被当成字形的一部分
    m[:int(h * 0.03), :] = 0
    m[:, :int(w * 0.03)] = 0
    m[int(h * 0.94):, :] = 0
    m[:, int(w * 0.94):] = 0
    return m


def _rank_glyph(card: np.ndarray, suit: str) -> np.ndarray | None:
    """裁出左上角的点数字形，归一化成固定画布。

    关键：花色符号的顶点在 y≈0.49H，点数形的底在 y≈0.43H。用 0.45H 切开，
    就能只拿点数、不把下面的花色符号并进来 —— 并进来会让模板匹配彻底失效。
    "10" 是两个连通块，按外接框合并即可。
    """
    m = _ink_mask(card, suit)
    h, w = m.shape
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)

    boxes: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 40:
            continue
        if y > h * 0.45:          # 花色符号及其以下
            continue
        if x > w * 0.75:          # 牌面右侧
            continue
        boxes.append((int(x), int(y), int(bw), int(bh)))
    if not boxes:
        return None

    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    glyph = m[y0:y1, x0:x1]

    nh = _GLYPH_INNER_H
    nw = max(8, min(_GLYPH_W - 4, int(round(glyph.shape[1] * nh / max(1, glyph.shape[0])))))
    glyph = cv2.resize(glyph, (nw, nh), interpolation=cv2.INTER_AREA)

    canvas = np.zeros((_GLYPH_H, _GLYPH_W), np.uint8)
    xo = (_GLYPH_W - nw) // 2
    canvas[4:4 + nh, xo:xo + nw] = glyph
    return canvas


@lru_cache(maxsize=1)
def _load_templates() -> dict[str, np.ndarray]:
    """加载点数模板图集（13 列，按 RANKS 顺序）。"""
    atlas = cv2.imread(str(_TEMPLATE_ATLAS), cv2.IMREAD_GRAYSCALE)
    if atlas is None:
        raise RuntimeError(
            f"缺少点数模板图集 {_TEMPLATE_ATLAS}；"
            "运行 `.venv/bin/python tools/build_card_templates.py` 生成")
    if atlas.shape[0] != _GLYPH_H or atlas.shape[1] < _GLYPH_W * len(RANKS):
        raise RuntimeError(
            f"点数模板图集尺寸不对：{atlas.shape}，"
            f"期望 ({_GLYPH_H}, {_GLYPH_W * len(RANKS)})")
    return {
        rank: atlas[:, i * _GLYPH_W:(i + 1) * _GLYPH_W] > 127
        for i, rank in enumerate(RANKS)
    }


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """归一化互相关。两者同尺寸、同画布，直接算即可。"""
    aa = a.astype(np.float32) - float(a.mean())
    bb = b.astype(np.float32) - float(b.mean())
    denom = float(np.sqrt((aa * aa).sum() * (bb * bb).sum()))
    return float((aa * bb).sum() / denom) if denom else 0.0


def _glyph_holes(glyph: np.ndarray | None) -> int | None:
    """点数字形里闭合孔的数量。

    3 和 8 的字形轮廓几乎一样，NCC 在低分辨率/抗锯齿下只差 1–2 个百分点；
    但拓扑差异是硬的：**3 没有闭合孔，8 有两个**。这个特征只用来给
    「3 vs 8」这一对做兜底，不参与其他点数的判断。
    """
    if glyph is None:
        return None
    contours, hierarchy = cv2.findContours(
        (glyph > 0).astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return 0
    # RETR_CCOMP 的层级里 parent != -1 的轮廓是孔（外层背景的 parent 为 -1）
    return sum(1 for h in hierarchy[0] if h[3] != -1)


def _classify_rank(
        glyph: np.ndarray | None
) -> tuple[str, float, float, dict[str, float], dict[str, Any]]:
    """返回 (rank, score, margin, 全部分数, 额外证据)。"""
    if glyph is None:
        return "", 0.0, 0.0, {}, {"glyph_holes": None}
    templates = _load_templates()
    scores = {rank: _ncc(glyph > 0, tpl) for rank, tpl in templates.items()}
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    rank, score = ordered[0]
    second_rank, second_score = ordered[1]
    holes = _glyph_holes(glyph)

    # 闭合孔拓扑兜底：3/8 与 6/8 的模板分数经常只差 0.01。
    #   3 → 0 孔，8 → 2 孔；6 → 1 孔。
    # 只在模板赢家是这几张时使用，避免影响其他点数。
    topology_rank: str | None = None
    override = False
    if rank == "8" and holes is not None:
        if holes == 0:
            topology_rank, override = "3", True
        elif holes == 1 and second_rank == "6":
            topology_rank, override = "6", True
        elif holes >= 2:
            topology_rank = "8"
    elif rank == "3" and holes is not None:
        if holes >= 2:
            topology_rank, override = "8", True
        elif holes == 0:
            topology_rank = "3"
    elif rank == "6" and holes is not None and second_rank == "8":
        if holes >= 2:
            topology_rank, override = "8", True
        elif holes == 1:
            topology_rank = "6"

    if override and topology_rank is not None:
        winner_score = score
        rank = topology_rank
        # 模板分不出胜负时，用两者较高的分数当作「字形匹配度」；
        # margin 记为 0，表示这一判由拓扑决定，而不是 NCC。
        score = max(winner_score, scores.get(topology_rank, winner_score))
        second_score = score
        margin = 0.0
    else:
        margin = score - second_score
    extra = {
        "glyph_holes": holes,
        "topology_rank": topology_rank,
        "topology_resolved": topology_rank is not None,
    }
    return (rank, float(score), float(margin),
            {k: round(v, 4) for k, v in scores.items()}, extra)


# ─────────────────────────── 单张牌 / 单块区域 ───────────────────────────

def _card_back_evidence(card: np.ndarray) -> dict[str, Any]:
    """判这张矩形是牌面还是牌背/大面积遮挡。

    牌背（POKER 背面）有白边，所以 `_detect_card_rects` 仍会把它当牌面检出；
    只看 NCC 会读出一个 0.6 分的假点数。这里用颜色分布做显式分类：真牌是白底
    + 少量花色墨迹，牌背是整块高饱和图案。返回的字段同时写进 evidence，
    下游（亮牌过滤 / StableCards 质量闸）不用再猜。
    """
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    s, v = hsv[..., 1], hsv[..., 2]
    sat_frac = float((s >= _CARD_BACK_SAT_MEDIAN).mean())
    sat_med = float(np.median(s))
    v_med = float(np.median(v))
    return {
        "card_back": bool(sat_frac >= _CARD_BACK_SAT_FRAC
                          and sat_med >= _CARD_BACK_SAT_MEDIAN),
        "sat_frac": round(sat_frac, 3),
        "sat_median": round(sat_med, 1),
        "v_median": round(v_med, 1),
    }


def _read_card(roi: np.ndarray, rect: tuple[int, int, int, int],
               offset: tuple[int, int], slot_name: str) -> tuple[Card, list[str]]:
    card_img = _canonical_card(roi, rect)
    warnings: list[str] = []

    suit, suit_conf, evidence = _classify_suit(card_img)
    evidence.update(_card_back_evidence(card_img))
    if evidence.get("ink_pixels", 0) < _MIN_SUIT_INK:
        warnings.append(f"{slot_name}: 花色墨迹过少，可能是牌面被遮挡")
    if evidence.get("shape_agrees") is False:
        warnings.append(
            f"{slot_name}: 颜色判 {suit} 与形状判 {evidence.get('shape_suit')} 不一致，"
            "已按颜色输出")

    glyph = _rank_glyph(card_img, suit)
    rank, rank_score, margin, scores, rank_evidence = _classify_rank(glyph)
    evidence["rank_scores"] = scores
    evidence["rank_margin"] = round(margin, 4)
    evidence.update(rank_evidence)
    # 显式遮挡：花色墨迹几乎没读到，或连点数字形都没抽出来。半张牌/被徽章、
    # 胜率、牌背压住时会落到这里，不再只靠低 rank_score 间接判断。
    evidence["occluded"] = bool(
        evidence.get("ink_pixels", 0) < _MIN_SUIT_INK or not rank)
    if evidence["card_back"]:
        warnings.append(f"{slot_name}: 检测到牌背（高饱和图案），不是有效牌面")
    elif evidence["occluded"]:
        warnings.append(f"{slot_name}: 检测到遮挡/半张牌，点数或花色不完整")
    if not rank:
        rank = "?"
        warnings.append(f"{slot_name}: 点数没读出来")
    elif rank_score < _MIN_RANK_SCORE or (
            margin < _MIN_RANK_MARGIN
            and not rank_evidence.get("topology_resolved")):
        warnings.append(
            f"{slot_name}: 点数 {rank} 置信度偏低（score={rank_score:.2f}, margin={margin:.2f}）")

    # 综合置信度：花色与点数里较差的那个，避免「花色很稳、点数瞎猜」被平均掉
    confidence = min(suit_conf, rank_score) if rank != "?" else suit_conf
    x, y, w, h = rect
    ox, oy = offset
    card = Card(
        rank=rank,
        suit=suit,
        confidence=float(confidence),
        rank_score=rank_score,
        rect=(x + ox, y + oy, w, h),
        evidence=evidence,
    )
    return card, warnings


def recognize_region(img: np.ndarray, rect: tuple[int, int, int, int], *,
                     name: str = "cards", slot: Slot = (0.0, 0.0, 1.0, 1.0),
                     max_cards: int = _MAX_CARDS,
                     split_pairs: bool = False) -> CardSlotRead:
    """识别图像中一个**像素矩形**里的牌。rect 是原图坐标 (x1,y1,x2,y2)。

    `split_pairs=True` 用于摊牌亮牌区：两张牌紧贴时会粘成一个轮廓，需要切开。
    """
    h, w = img.shape[:2]
    x1, y1, x2, y2 = rect
    x1, y1 = max(0, min(x1, w)), max(0, min(y1, h))
    x2, y2 = max(x1, min(x2, w)), max(y1, min(y2, h))
    rect = (x1, y1, x2, y2)

    read = CardSlotRead(name=name, slot=slot, rect=rect)
    roi = img[y1:y2, x1:x2]
    if roi.size == 0:
        read.warnings.append(f"{name}: 牌区为空")
        return read

    rects = _detect_card_rects(roi, split_pairs=split_pairs)[:max_cards]
    for r in rects:
        card, warnings = _read_card(roi, r, (x1, y1), name)
        read.cards.append(card)
        read.warnings.extend(warnings)

    # 一张都没检出不是异常：翻前本来就没有公共牌，hero 也可能还没发牌。
    # 「张数不合理」由 recognize_cards 统一判（1/2 张公共牌、hero 不是 0/2 张）。
    return read


# ─────────────────────────── 对外的入口 ───────────────────────────

# 亮牌识别的置信度门槛。比公共牌略严：这里没有「翻前本来就没有牌」的合法空态，
# 任何误检都会直接写进对手手牌记录。
_MIN_SHOW_CONFIDENCE = 0.55


def recognize_shown_cards(img: np.ndarray, profile: Profile | None, *,
                          seats: list[str] | None = None) -> dict[str, CardSlotRead]:
    """识别每个对手座位亮出的两张手牌。

    摊牌/主动秀牌时，WPK 会把牌画在该座的 action_slot 区域（实测
    `images/card/show_card.jpg` 的 midL：A♣Q♥；`images/card/showOne` 的
    lowR：只亮一张 K♦，旁边是 POKER 牌背）。这里逐座裁 action_slot，
    按「紧贴的牌」模式识别：

      · 2 张：正常摊牌；
      · 1 张：河牌主动只秀一张（另一张是牌背/低置信度，忽略）；
      · 0 张或两张都是牌背：不返回。

    返回 {seat_id: CardSlotRead}。hero 自己的牌由 hero_cards 负责，跳过。
    """
    if profile is None:
        return {}

    h, w = img.shape[:2]
    wanted = set(seats) if seats is not None else None
    out: dict[str, CardSlotRead] = {}
    for seat in profile.seats:
        if seat.seat_id == profile.hero_seat_id:
            continue
        if wanted is not None and seat.seat_id not in wanted:
            continue
        if seat.action_slot is None:
            continue
        px = profile.slot_to_px(seat.action_slot, (w, h))
        read = recognize_region(
            img, px, name=f"show:{seat.seat_id}", slot=seat.action_slot,
            max_cards=2, split_pairs=True)
        # 只秀一张时，另一张是带 POKER 字样的牌背；它会检出低分数的假点数
        # （样张 lowR：K♦ + 牌背被读成 8h，rank_score 0.64）。逐张过滤，
        # 保留 1 张高置信度的真牌，而不是因为第二张是牌背把整座丢掉。
        kept = [c for c in read.cards
                if c.rank != "?" and c.confidence >= _MIN_SHOW_CONFIDENCE
                and c.rank_score >= _MIN_RANK_SCORE
                and not c.evidence.get("card_back")
                and not c.evidence.get("occluded")]
        if not (1 <= len(kept) <= 2):
            continue
        if len(kept) == 2 and kept[0].code == kept[1].code:
            continue
        if len(kept) < len(read.cards):
            read.warnings.append(
                f"{seat.seat_id}: 忽略 {len(read.cards) - len(kept)} 张"
                "低置信度/牌背")
        read.cards = kept
        out[seat.seat_id] = read
    return out


# ─────────────────────────── 跨帧稳定闸 ───────────────────────────

def _reading_issues(read: CardSlotRead) -> list[str]:
    """一张牌区读数自身的问题（不看张数）。用于稳定闸拒绝替换 last-good。"""
    issues: list[str] = []
    for c in read.cards:
        if c.evidence.get("card_back"):
            issues.append(f"{c.code} 是牌背")
        elif c.evidence.get("occluded"):
            issues.append(f"{c.code} 被遮挡/不完整")
        elif c.rank == "?":
            issues.append("有点数没读出")
        elif c.rank_score < _MIN_RANK_SCORE:
            issues.append(f"{c.code} 点数分低 {c.rank_score:.2f}")
        elif c.confidence < _MIN_STABLE_CONFIDENCE:
            issues.append(f"{c.code} 置信度低 {c.confidence:.2f}")
    return issues


@dataclass
class _StableSlot:
    """一块牌区（board / hero_cards）的跨帧状态。"""

    read: CardSlotRead | None = None       # last-good
    pending: tuple[str, ...] | None = None
    pending_read: CardSlotRead | None = None
    pending_n: int = 0
    established: bool = False
    # 最近几帧的合法读数。连续两帧不一致时用它们做 top-1 多数投票，
    # 兜住 3/8、6/8 这类 NCC 只差 0.01、逐帧在两个字之间跳的情况。
    recent: list[CardSlotRead] = dc_field(default_factory=list)


class StableCards:
    """跨帧牌面状态机：只让稳定、合法的读数替换 last-good。

    背景（实测事故）：发牌/翻牌动画中间帧会让公共牌 4 张短暂读成 3 张，手牌
    被劈成 h1/h2；只秀一张时牌背会被当成第二张牌。逐帧直接采用 OCR 结果太脆。

    规则：

      · **公共牌只前进**：合法张数是 0/3/4/5；4→3、5→4 这类回退一律不采用，
        只记异常并继续用 last-good。0 是新一手信号，也要连续 2 帧确认。
      · **hero 只接受 0/2 张**：1 张只走主动亮牌路径（`recognize_shown_cards`），
        不写进 hero_cards。
      · **新读数连续 `confirm_frames` 帧一致才替换 last-good**；期间继续输出
        last-good，并把原因写进 `warnings`。
      · **牌背 / 遮挡 / ? / 低置信度不参与替换**：这些读数即使连续两帧一致
        也不可信（典型的 POKER 牌背），只记异常。
      · 公共牌确认归零（新一手）时清空 hero last-good，避免拿上一手的手牌
        给这一手出建议；hero 从 2 张换成另一组 2 张时也清空公共牌，
        兜住「从翻牌开始接管」的新手。

    用法：

        stable = StableCards()
        cards = stable.update(recognize_cards(img, profile))
        cards.board_codes / cards.hero_codes / cards.reliable / cards.warnings

    `update()` 原样返回非 `CardsResult` 的鸭子类型结果（单测桩），方便注入。
    """

    BOARD_COUNTS = (0, 3, 4, 5)
    HERO_COUNTS = (0, 2)
    # 多帧投票：最近 3 帧里同一位置至少有 2 帧读出同一个点数/花色才采用。
    VOTE_FRAMES = 3
    VOTE_MIN_AGREE = 2

    def __init__(self, confirm_frames: int = 2,
                 vote_frames: int = VOTE_FRAMES) -> None:
        self.confirm_frames = max(1, int(confirm_frames))
        # 0 = 关闭投票，只认连续 confirm_frames 帧一致
        self.vote_frames = max(0, int(vote_frames))
        self.last_anomalies: list[str] = []
        self.reliable = True
        self._reset_tracks()

    def _reset_tracks(self) -> None:
        self._board = _StableSlot()
        self._hero = _StableSlot()

    def reset(self) -> None:
        """断流重连 / 换桌：跨帧 last-good 全部作废。"""
        self._reset_tracks()
        self.last_anomalies = []
        self.reliable = True

    def update(self, result: CardsResult | Any) -> CardsResult | Any:
        if not isinstance(result, CardsResult):
            return result

        anomalies: list[str] = []
        warnings: list[str] = list(result.warnings)

        board, board_reset, _ = self._update_slot(
            result.board, self._board, "board", self.BOARD_COUNTS,
            anomalies, warnings, board=True)
        if board_reset:
            # 新一手：hero 的上一手牌不能继续沿用。
            self._hero = _StableSlot()

        hero, _, hero_changed = self._update_slot(
            result.hero_cards, self._hero, "hero_cards", self.HERO_COUNTS,
            anomalies, warnings, board=False,
            # 公共牌刚归零的这一帧，hero 槽里可能还停留着摊牌亮出的旧牌，
            # 不能让它开始累计确认。
            skip_accept=board_reset)
        if hero_changed:
            # hero 换了一组两张牌 = 新一手，哪怕没看到公共牌归零。公共牌
            # last-good 属于上一手，先清空，等新手牌面重新读两帧。
            self._board = _StableSlot()
            board = CardSlotRead(name=board.name, slot=board.slot,
                                 rect=board.rect)
            # 这是正常换手，不是事故帧；只写 warning，不触发事故落盘。
            warnings.append("hero_cards: 换了一组手牌，公共牌等待重新确认")

        reliable = (self._slot_reliable(self._board, self.BOARD_COUNTS)
                    and self._slot_reliable(self._hero, self.HERO_COUNTS))
        self.last_anomalies = anomalies
        self.reliable = reliable
        warnings = _dedup(warnings)
        return CardsResult(size=result.size, board=board, hero_cards=hero,
                           shows=result.shows, warnings=warnings,
                           reliable=reliable)

    def _update_slot(self, raw: CardSlotRead, track: _StableSlot, name: str,
                     valid_counts: tuple[int, ...], anomalies: list[str],
                     warnings: list[str], *, board: bool,
                     skip_accept: bool = False
                     ) -> tuple[CardSlotRead, bool, bool]:
        """返回 (对外读数, 是否确认归零, 是否确认换了一组 2 张 hero)。"""
        codes = tuple(raw.codes)
        n = len(codes)

        if n not in valid_counts:
            msg = (f"{name}: 本帧 {n} 张，合法张数是 "
                   f"{'/'.join(str(v) for v in valid_counts)}")
            anomalies.append(msg)
            warnings.append(f"{msg}；沿用上一帧")
            return self._current(track, raw), False, False

        issues = _reading_issues(raw)
        if issues:
            msg = f"{name}: 读数不可靠（{'；'.join(issues)}）"
            anomalies.append(msg)
            warnings.append(f"{msg}；沿用上一帧")
            return self._current(track, raw), False, False

        # 公共牌回退：4→3、5→4 一律不接受，哪怕连续多帧。0 是新一手信号，
        # 走下面的 2 帧确认；否则 4 张短暂读成 3 张会把一手牌劈成两半。
        if board and track.established and track.read is not None:
            last_n = len(track.read.cards)
            if n < last_n and n != 0:
                msg = f"{name}: 张数 {last_n}→{n} 回退，不采用"
                anomalies.append(msg)
                warnings.append(f"{msg}；沿用上一帧")
                return track.read, False, False

        if track.read is not None and codes == tuple(track.read.codes):
            track.pending = None
            track.pending_read = None
            track.pending_n = 0
            track.recent.clear()
            return track.read, False, False

        # 首次就是空读数：没有牌不是误读，直接建立基线，不白等两帧。
        # 这不是「公共牌归零」的换手事件，不能触发 hero 清空。
        if not track.established and n == 0:
            track.read = raw
            track.established = True
            return raw, False, False

        if skip_accept:
            warnings.append(f"{name}: 新手牌开始，等牌面稳定后再确认")
            return self._current(track, raw), False, False

        # 合法且与 last-good 不同的读数进入投票窗口（最多保留最近 3 帧）。
        if self.vote_frames:
            track.recent.append(raw)
            if len(track.recent) > self.vote_frames:
                track.recent = track.recent[-self.vote_frames:]

        if track.pending == codes:
            track.pending_n += 1
            track.pending_read = raw
        else:
            track.pending = codes
            track.pending_read = raw
            track.pending_n = 1

        if track.pending_n >= self.confirm_frames:
            accepted = track.pending_read or raw
            previous = track.read
            changed = (not board and previous is not None
                       and len(previous.cards) == 2
                       and len(accepted.cards) == 2
                       and tuple(previous.codes) != tuple(accepted.codes))
            track.read = accepted
            track.established = True
            track.pending = None
            track.pending_read = None
            track.pending_n = 0
            track.recent.clear()
            return accepted, board and n == 0, changed

        # 连续两帧不一致时，用最近 3 帧的 top-1 读数投票。3/8、6/8 的
        # NCC 只差 0.01 时会逐帧跳字；同一位置 3 帧里 2 帧一致就采用，
        # 比无限等「连续一致」更稳，也不会采用从没读到过的组合。
        if self.vote_frames and len(track.recent) >= self.vote_frames:
            voted = self._vote(track.recent, n)
            if voted is not None:
                previous = track.read
                changed = (not board and previous is not None
                           and len(previous.cards) == 2
                           and len(voted.cards) == 2
                           and tuple(previous.codes) != tuple(voted.codes))
                track.read = voted
                track.established = True
                track.pending = None
                track.pending_read = None
                track.pending_n = 0
                track.recent.clear()
                warnings.append(
                    f"{name}: {self.vote_frames} 帧投票采用 "
                    f"{'/'.join(voted.codes)}")
                return voted, board and n == 0, changed

        warnings.append(
            f"{name}: 新读数待确认（{track.pending_n}/{self.confirm_frames} 帧）")
        return self._current(track, raw), False, False

    def _vote(self, recent: list[CardSlotRead],
              n_cards: int) -> CardSlotRead | None:
        """最近几帧按位置投票；每个位置的点数和花色都要有 2 帧一致。"""
        if n_cards == 0 or not recent:
            return None
        base = recent[-1]
        voted_cards: list[Card] = []
        for i in range(n_cards):
            per = [r.cards[i] for r in recent if i < len(r.cards)]
            if len(per) != len(recent):
                return None
            rank, rank_votes = Counter(c.rank for c in per).most_common(1)[0]
            suit, suit_votes = Counter(c.suit for c in per).most_common(1)[0]
            if (rank_votes < self.VOTE_MIN_AGREE
                    or suit_votes < self.VOTE_MIN_AGREE):
                return None
            # 优先用同时命中点数和花色的那一张（保留它的 rect/evidence）
            chosen = next((c for c in reversed(per)
                           if c.rank == rank and c.suit == suit), None)
            if chosen is None:
                src = per[-1]
                chosen = Card(
                    rank=rank, suit=suit,
                    confidence=min(c.confidence for c in per),
                    rank_score=min(c.rank_score for c in per),
                    rect=src.rect,
                    evidence={**src.evidence,
                              "vote": {"frames": len(per),
                                       "rank_votes": rank_votes,
                                       "suit_votes": suit_votes}})
            voted_cards.append(chosen)
        return CardSlotRead(name=base.name, slot=base.slot, rect=base.rect,
                            cards=voted_cards, warnings=list(base.warnings))

    @staticmethod
    def _current(track: _StableSlot, raw: CardSlotRead) -> CardSlotRead:
        if track.read is not None:
            return track.read
        # 还没有 last-good：输出空牌区，但保留坐标/槽位，方便排查。
        return CardSlotRead(name=raw.name, slot=raw.slot, rect=raw.rect)

    @staticmethod
    def _slot_reliable(track: _StableSlot,
                       valid_counts: tuple[int, ...]) -> bool:
        if not track.established or track.read is None:
            return False                    # 还没稳定读过，不能给 AI 用
        if len(track.read.cards) not in valid_counts:
            return False
        return not _reading_issues(track.read)


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def recognize_cards(img: np.ndarray, profile: Profile | None = None, *,
                    slots: dict[str, Slot] | None = None) -> CardsResult:
    """识别公共牌与 hero 手牌。

    槽位选择优先级：显式 slots > profile.global_slots > DEFAULT_CARD_SLOTS。
    返回结构见 CardsResult；board / hero_cards 都是**从左到右**的顺序。
    """
    h, w = img.shape[:2]
    size: Size = (w, h)

    resolved: dict[str, Slot] = dict(DEFAULT_CARD_SLOTS)
    source = "default"
    if profile is not None and profile.global_slots:
        for key in ("board", "hero_cards"):
            slot = profile.global_slots.get(key)
            if slot is not None:
                resolved[key] = slot
                source = "profile"
    if slots:
        resolved.update(slots)
        source = "explicit"

    warnings: list[str] = []
    reads: dict[str, CardSlotRead] = {}
    for key in ("board", "hero_cards"):
        slot = resolved[key]
        if profile is not None and source != "explicit":
            # profile.slot_to_px 会处理 canvas/letterbox，和座位坐标同一套换算
            px = profile.slot_to_px(slot, size)
        else:
            px = (int(slot[0] * w), int(slot[1] * h),
                  int(slot[2] * w), int(slot[3] * h))
        reads[key] = recognize_region(img, px, name=key, slot=slot)

    if source == "default":
        warnings.append(
            "profile 没有 board / hero_cards 槽位，已使用 WPK 移动端默认牌区")
    if len(reads["hero_cards"].cards) not in (0, 2):
        warnings.append(
            f"hero 手牌识别出 {len(reads['hero_cards'].cards)} 张（正常是 2 张）")
    if len(reads["board"].cards) in (1, 2):
        warnings.append(
            f"公共牌只识别出 {len(reads['board'].cards)} 张（正常是 0/3/4/5 张）")

    # 单帧入口也给出可靠标志；实时链路还会再过一遍 StableCards，
    # 那里用 last-good 覆盖瞬时误读。这里只描述「这一帧本身」。
    reliable = (len(reads["board"].cards) in StableCards.BOARD_COUNTS
                and len(reads["hero_cards"].cards) in StableCards.HERO_COUNTS
                and not _reading_issues(reads["board"])
                and not _reading_issues(reads["hero_cards"]))
    shows = recognize_shown_cards(img, profile)
    return CardsResult(size=size, board=reads["board"],
                       hero_cards=reads["hero_cards"], shows=shows,
                       warnings=warnings, reliable=reliable)


def recognize_cards_of_file(path: str | Path,
                            profile: Profile | None = None) -> CardsResult:
    """便捷入口：读一张图并识别。供 CLI / 排查用。"""
    img = cv2.imread(str(path))
    if img is None:
        raise FileNotFoundError(f"读不了这张图：{path}")
    return recognize_cards(img, profile)


__all__ = [
    "Card",
    "CardSlotRead",
    "CardsResult",
    "DEFAULT_CARD_SLOTS",
    "RANKS",
    "SUIT_NAMES",
    "StableCards",
    "recognize_cards",
    "recognize_cards_of_file",
    "recognize_region",
    "recognize_shown_cards",
]
