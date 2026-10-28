"""实时牌面范围：按公共牌给每个 combo 打「牌面连接度」，再乘翻前先验。

核心语义（设计文档 §6.1）：

    P(hand | board, action) ∝ P(action | hand, board) × P(hand | preflop)

牌面**不会**改变对手发到什么牌，改变的是他「愿不愿意继续」。所以这里构建的
是**动作条件范围**，不是「他的全部范围」：

  · 过牌 → 不按牌面筛，直接用翻前先验（空气也可能是过牌）；
  · 跟注 → 低门槛（对子/卡顺/后门都算）；
  · 下注 / 加注 / 全下 → 门槛逐级抬高。

分类必须按 combo + 花色：`AsKs4d` 上 `7s8s` 是花听（不是「78 无关」），
`7h8h` 才是只有后门顺；`QhJh` 有卡顺，不是空气。

性能：牌面特征按 board 缓存，冷启动约 10–15ms，之后各 mode/prior 只做
加权过滤。顺子补牌用 8,192 的位掩码查表，不逐张 evaluate。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from . import ranges
from .coach import (
    FLUSH, FULL_HOUSE, HIGH_CARD, ONE_PAIR, STRAIGHT, STRAIGHT_FLUSH, TRIPS,
    TWO_PAIR, _plays_board, _parse_card, evaluate, parse_cards,
)

# ─────────────────────────── 牌面特征 ───────────────────────────

# 13 位点数掩码（2..A 对应 bit 0..12）→ 顺子顶张；-1 = 无顺子。
# 轮子 A2345 的顶张是 5（bit 3），与 coach._straight_high 同口径。
STRAIGHT_HIGH: tuple[int, ...] = tuple(
    (lambda mask: next(
        (hi for hi in range(12, 3, -1)
         if all(mask & (1 << (hi - i)) for i in range(5))),
        (3 if (mask & (1 << 12)) and all(mask & (1 << r) for r in (0, 1, 2, 3))
         else -1),
    ))(mask)
    for mask in range(1 << 13)
)


def _mask(cards: Iterable[tuple[int, str]]) -> int:
    out = 0
    for rank, _suit in cards:
        out |= 1 << rank
    return out


# 所有 5 连顺窗口（点数集合）。轮子 A2345 单独处理：A 当 1，窗口顶张是 5。
_STRAIGHT_WINDOWS: tuple[frozenset[int], ...] = (
    *[frozenset(range(lo, lo + 5)) for lo in range(0, 9)],  # 2-6 ... T-A
    frozenset({12, 0, 1, 2, 3}),                             # A2345
)


def _backdoor_straight(mask: int, hole_ranks: Iterable[int]) -> bool:
    """后门顺：需要 turn+river 两张才成顺。

    关键口径（验收用例 7h2c = 空气）：**两张底牌的 rank 都要参与**这条
    潜在顺子。7h2c 在 AsKs4d 上只有 2 能进 A2345，7 永远用不上，所以不算
    后门顺；7h8h 的 7、8 都在 4-5-6-7-8 里，才算。
    """
    need = set(hole_ranks)
    for window in _STRAIGHT_WINDOWS:
        if not need <= window:
            continue
        missing = sum(1 for r in window if not mask & (1 << r))
        if 1 <= missing <= 2:
            return True
    return False


def straight_high(mask: int) -> int:
    return STRAIGHT_HIGH[mask & 0x1FFF]


@dataclass(frozen=True)
class Features:
    """一个 combo 在某个牌面上的成牌/听牌特征。"""

    category: int             # evaluate(hole+board) 的牌型下标
    pair_kind: str            # overpair/top/second/middle/bottom/under/board/none
    flush_draw: bool
    backdoor_flush: bool
    straight_outs: int        # 下一张能成顺的**点数**个数（不是牌张数）
    backdoor_straight: bool
    overcards: bool
    plays_board: bool


def _as_cards(value) -> list[tuple[int, str]]:
    """接受 'AsKs4d'、['As','Ks','4d'] 或 [(12,'s'), ...]。"""
    if isinstance(value, str):
        return parse_cards(value)
    out: list[tuple[int, str]] = []
    for item in value:
        if isinstance(item, str):
            out.append(_parse_card(item))
        else:
            rank, suit = item
            out.append((int(rank), str(suit).lower()))
    return out


def _pair_kind(combo: tuple[tuple[int, str], ...],
               board: list[tuple[int, str]], cat: int) -> str:
    hole_ranks = [r for r, _ in combo]
    board_ranks = [r for r, _ in board]
    board_sorted = sorted(set(board_ranks), reverse=True)
    pocket = hole_ranks[0] == hole_ranks[1]

    if cat == TRIPS:
        if pocket and hole_ranks[0] in board_ranks:
            return "set"
        return "trips"
    if cat == TWO_PAIR:
        return "two_pair"
    if cat != ONE_PAIR:
        return "none"

    if pocket:
        pair_rank = hole_ranks[0]
        if pair_rank > board_sorted[0]:
            return "overpair"
        return "underpair"

    matched = [r for r in hole_ranks if r in board_ranks]
    if matched:
        pair_rank = matched[0]
        if pair_rank == board_sorted[0]:
            return "top"
        if len(board_sorted) >= 2 and pair_rank == board_sorted[1]:
            return "second"
        if len(board_sorted) >= 3 and pair_rank == board_sorted[-1]:
            return "bottom"
        return "middle"

    # 公对牌面且底牌没碰到：人人都有这一对，权重必须低。
    if len(set(board_ranks)) < len(board_ranks):
        return "board"
    return "none"


@lru_cache(maxsize=8192)
def _classify(combo: tuple[tuple[int, str], ...],
              board: tuple[tuple[int, str], ...]) -> Features:
    board_list = list(board)
    all_cards = list(combo) + board_list
    cat, _tb = evaluate(all_cards)
    board_len = len(board_list)
    hole_ranks = [r for r, _ in combo]
    board_ranks = [r for r, _ in board_list]
    board_max = max(board_ranks)
    mask = _mask(all_cards)

    suit_counts: dict[str, int] = {}
    for _r, suit in all_cards:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    flush_suit = next((s for s, n in suit_counts.items() if n == 4), None)
    hole_suits = {s for _r, s in combo}
    flush_draw = bool(flush_suit and flush_suit in hole_suits and cat < FLUSH
                      and board_len < 5)
    backdoor_flush = bool(
        board_len == 3 and cat < FLUSH
        and any(n == 3 and s in hole_suits for s, n in suit_counts.items())
    )

    straight_outs = 0
    backdoor_straight = False
    if board_len < 5 and cat < STRAIGHT:
        for rank in range(13):
            if mask & (1 << rank):
                continue
            if straight_high(mask | (1 << rank)) >= 0:
                straight_outs += 1
        if board_len == 3 and straight_outs == 0:
            backdoor_straight = _backdoor_straight(mask, hole_ranks)

    overcards = (board_len == 3 and cat == HIGH_CARD
                 and all(r > board_max for r in hole_ranks))
    # coach._plays_board 的「打牌面」是为成对/成牌设计的；高牌时它会把
    # 「两张底牌只当踢脚」也判成 True。这里只对成手牌沿用它的口径。
    plays_board = (cat != HIGH_CARD
                   and _plays_board(list(combo), board_list))

    return Features(
        category=cat,
        pair_kind=_pair_kind(combo, board_list, cat),
        flush_draw=flush_draw,
        backdoor_flush=backdoor_flush,
        straight_outs=straight_outs,
        backdoor_straight=backdoor_straight,
        overcards=overcards,
        plays_board=plays_board,
    )


def classify(combo, board) -> Features:
    """公开的分类入口；接受 combo tuple + board 字符串/卡牌列表。"""
    cards = tuple(ranges.canon_combo(*combo))
    board_cards = tuple(_as_cards(board))
    return _classify(cards, board_cards)


# 连接度分数（设计文档 §6.3）。分数是「关系强度」，不是概率。
_BASE_SCORE = {
    FULL_HOUSE: 1.70,
    FLUSH: 1.60,
    STRAIGHT: 1.50,
    TRIPS: 1.40,
    TWO_PAIR: 1.25,
}
_PAIR_SCORE = {
    "overpair": 0.90,
    "top": 0.80,
    "second": 0.75,
    "middle": 0.70,
    "bottom": 0.60,
    "underpair": 0.45,
    # 公对牌面、人人都有的一对：给一个低于 call 阈值的权重，避免空气化。
    "board": 0.25,
    "none": 0.0,
}
# 验收用例要求 5s6s / 7s8s（花听 + 后门顺）是 0.90；花听单独已经到
# bet 门槛，后门顺不再叠加（设计里「后门只在没有下一张听牌时加」）。
_FLUSH_DRAW_SCORE = 0.90
_OESD_SCORE = 0.70
_GUTSHOT_SCORE = 0.35
_BACKDOOR_FLUSH_SCORE = 0.15
_BACKDOOR_STRAIGHT_SCORE = 0.10
_OVERCARD_SCORE = 0.10
_MAX_SCORE = 1.8


def score(features: Features) -> float:
    """把一个 combo 的牌面关系转成 0–1.8 的连接度。"""
    cat = features.category
    base = _BASE_SCORE.get(cat, 0.0)
    if cat == ONE_PAIR:
        base = _PAIR_SCORE.get(features.pair_kind, 0.0)
    if features.plays_board:
        base = min(base, 0.25)
    total = base

    # 听牌：花听和顺听取较高者，不直接相加（并集口径，避免重复计数）。
    if features.flush_draw:
        total += _FLUSH_DRAW_SCORE
    elif features.straight_outs >= 2:
        total += _OESD_SCORE
    elif features.straight_outs == 1:
        total += _GUTSHOT_SCORE

    # 后门分只在「没有下一张听牌」时加；成牌（两对+）不需要后门加成。
    no_next_draw = not features.flush_draw and features.straight_outs == 0
    if no_next_draw and cat < TWO_PAIR:
        if features.backdoor_flush:
            total += _BACKDOOR_FLUSH_SCORE
        if features.backdoor_straight:
            total += _BACKDOOR_STRAIGHT_SCORE

    if features.overcards:
        total += _OVERCARD_SCORE
    return min(total, _MAX_SCORE)


# ─────────────────────────── 构建范围 ───────────────────────────

MODE_MIN_WEIGHT: dict[str, float] = {
    "check": 0.0,       # 不按牌面筛
    "call": 0.30,
    "bet": 0.75,
    "raise": 0.85,
    "allin": 0.90,
    "unknown": 0.35,
}


@lru_cache(maxsize=64)
def _features_for_board(board: tuple[tuple[int, str], ...]
                        ) -> tuple[tuple[tuple[tuple[int, str], ...], Features, float], ...]:
    """一个牌面上所有可用 combo 的特征 + 分数。按 board 缓存。"""
    board_set = set(board)
    out = []
    for combo in ranges.ALL_COMBOS:
        if combo[0] in board_set or combo[1] in board_set:
            continue
        feat = _classify(combo, board)
        out.append((combo, feat, score(feat)))
    return tuple(out)


_BUILD_CACHE: dict[tuple, ranges.Range | None] = {}
_BUILD_CACHE_MAX = 128


def _env_min_weight() -> float | None:
    raw = os.environ.get("BOARD_RANGE_MIN_WEIGHT", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _board_key(board: tuple[tuple[int, str], ...]) -> tuple[tuple[int, str], ...]:
    return tuple(sorted(board, key=lambda c: (c[0], c[1])))


def build(board, mode: str = "call", prior: ranges.Range | str | None = None,
          min_weight: float | None = None, *,
          preflop_range: ranges.Range | str | None = None) -> ranges.Range | None:
    """构建动作条件范围。

    `board` 3–5 张；`mode` 取 check/call/bet/raise/allin/unknown；
    `prior` 是翻前先验（Range 或 ID），None 时降级为均匀牌面范围（board-only）。
    `preflop_range` 是旧文档的别名。
    """
    if prior is None:
        prior = preflop_range
    if isinstance(prior, str):
        prior = ranges.get(prior)

    try:
        board_cards = _as_cards(board)
    except (ValueError, TypeError):
        return None
    if not (3 <= len(board_cards) <= 5) or len(set(board_cards)) != len(board_cards):
        return None

    mode = (mode or "call").strip().lower()
    if mode not in MODE_MIN_WEIGHT:
        mode = "call"
    # 动作读不到时不知道他是下注还是跟注，只能用纯牌面范围（board-only），
    # 不能拿翻前先验假装知道他收窄到哪一档。
    if mode == "unknown":
        prior = None
    # 过牌不按牌面筛：没有新信息，保留翻前范围里的空气。
    if mode == "check":
        if prior is not None:
            return prior
        return ranges.uniform_range("board:check:uniform",
                                    note="board-only (any two cards)")

    if min_weight is None:
        min_weight = _env_min_weight()
    if min_weight is None:
        min_weight = MODE_MIN_WEIGHT[mode]

    board_cards = tuple(board_cards)
    bkey = _board_key(board_cards)
    pkey = ((prior.id, prior.version, id(prior))
            if prior is not None else ("uniform", 0))
    ckey = (bkey, mode, pkey, round(float(min_weight), 4))
    if ckey in _BUILD_CACHE:
        return _BUILD_CACHE[ckey]

    prior_weights = prior.normalized() if prior is not None else None
    weights: dict[tuple[tuple[int, str], tuple[int, str]], float] = {}
    for combo, _feat, feat_score in _features_for_board(bkey):
        if feat_score < min_weight:
            continue
        if prior_weights is None:
            w = feat_score
        else:
            w = feat_score * prior_weights.get(combo, 0.0)
        if w > 0:
            weights[combo] = w

    base_id = prior.id if prior is not None else "uniform"
    source = f"board:{mode}:{prior.source if prior is not None else 'uniform'}"
    note = (f"board-connected ∩ {base_id}, min {min_weight:g}"
            if prior is not None else
            f"board-only, min {min_weight:g}")
    built = ranges.make_range(f"board:{mode}:{base_id}", weights,
                              source=source, note=note)
    if len(_BUILD_CACHE) >= _BUILD_CACHE_MAX:
        _BUILD_CACHE.clear()
    _BUILD_CACHE[ckey] = built
    return built


def reset_cache() -> None:
    _features_for_board.cache_clear()
    _BUILD_CACHE.clear()


__all__ = [
    "Features", "MODE_MIN_WEIGHT", "STRAIGHT_HIGH", "build", "classify",
    "reset_cache", "score", "straight_high",
]
