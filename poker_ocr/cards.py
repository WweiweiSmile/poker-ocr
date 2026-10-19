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

版式：优先用 profile 的 global_slots（board / hero_cards）。配置里没有时退回
WPK 移动端的默认归一化坐标 —— 默认坐标是 0-1 相对值，换分辨率不用改。
"""

from __future__ import annotations

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
    """一帧的公共牌 + hero 手牌。"""

    size: Size
    board: CardSlotRead
    hero_cards: CardSlotRead
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def board_codes(self) -> list[str]:
        return self.board.codes

    @property
    def hero_codes(self) -> list[str]:
        return self.hero_cards.codes

    def to_dict(self, include_details: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "size": list(self.size),
            "board": self.board_codes,
            "hero_cards": self.hero_codes,
        }
        if include_details:
            d["board_read"] = self.board.to_dict()
            d["hero_cards_read"] = self.hero_cards.to_dict()
        if self.warnings:
            d["warnings"] = self.warnings
        return d


# ─────────────────────────── 牌面切分 ───────────────────────────

def _detect_card_rects(roi: np.ndarray) -> list[tuple[int, int, int, int]]:
    """在 ROI 里找出牌面矩形，按从左到右排序。

    牌面是白/灰低饱和，毛毡是绿色高饱和 —— 这个差异很稳。只取外轮廓
    （RETR_EXTERNAL），再用「面积 + 宽高比 + 高度」三道几何门过滤，
    不做颜色以外的内容判断。
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
        if not (_CARD_ASPECT[0] < w / max(1, h) < _CARD_ASPECT[1]):
            continue
        if h < roi_h * _CARD_HEIGHT_FRAC:
            continue
        rects.append((x, y, w, h))

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


def _classify_rank(glyph: np.ndarray | None) -> tuple[str, float, float, dict[str, float]]:
    """返回 (rank, score, margin, 全部分数)。"""
    if glyph is None:
        return "", 0.0, 0.0, {}
    templates = _load_templates()
    scores = {rank: _ncc(glyph > 0, tpl) for rank, tpl in templates.items()}
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    rank, score = ordered[0]
    margin = score - ordered[1][1]
    return rank, float(score), float(margin), {k: round(v, 4) for k, v in scores.items()}


# ─────────────────────────── 单张牌 / 单块区域 ───────────────────────────

def _read_card(roi: np.ndarray, rect: tuple[int, int, int, int],
               offset: tuple[int, int], slot_name: str) -> tuple[Card, list[str]]:
    card_img = _canonical_card(roi, rect)
    warnings: list[str] = []

    suit, suit_conf, evidence = _classify_suit(card_img)
    if evidence.get("ink_pixels", 0) < _MIN_SUIT_INK:
        warnings.append(f"{slot_name}: 花色墨迹过少，可能是牌面被遮挡")
    if evidence.get("shape_agrees") is False:
        warnings.append(
            f"{slot_name}: 颜色判 {suit} 与形状判 {evidence.get('shape_suit')} 不一致，"
            "已按颜色输出")

    glyph = _rank_glyph(card_img, suit)
    rank, rank_score, margin, scores = _classify_rank(glyph)
    evidence["rank_scores"] = scores
    evidence["rank_margin"] = round(margin, 4)
    if not rank:
        rank = "?"
        warnings.append(f"{slot_name}: 点数没读出来")
    elif rank_score < _MIN_RANK_SCORE or margin < _MIN_RANK_MARGIN:
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
                     max_cards: int = _MAX_CARDS) -> CardSlotRead:
    """识别图像中一个**像素矩形**里的牌。rect 是原图坐标 (x1,y1,x2,y2)。"""
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

    rects = _detect_card_rects(roi)[:max_cards]
    for r in rects:
        card, warnings = _read_card(roi, r, (x1, y1), name)
        read.cards.append(card)
        read.warnings.extend(warnings)

    # 一张都没检出不是异常：翻前本来就没有公共牌，hero 也可能还没发牌。
    # 「张数不合理」由 recognize_cards 统一判（1/2 张公共牌、hero 不是 0/2 张）。
    return read


# ─────────────────────────── 对外的入口 ───────────────────────────

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

    return CardsResult(size=size, board=reads["board"],
                       hero_cards=reads["hero_cards"], warnings=warnings)


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
    "recognize_cards",
    "recognize_cards_of_file",
    "recognize_region",
]
