"""复盘归因用的硬规则审计（**legacy offline only**）。

`audit_advice_tags()` 不看 Value directive，只看可证伪的事实：牌力解析、
坚果校验、牌面结构、赔率。replay / compare_review_hands 用它回答
「这次下注是否与事实矛盾」，不进入 live 提示词。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...coach import (
    MID_BOTTOM, ONE_PAIR, OVERPAIR, STRONG_DRAW, STRONG_MADE, TPTK, TP_WEAK,
    WEAK_DRAW,
)
from ...hand_track import PREFLOP
from ...pot_math import call_amount, winnable_pot
from ...preflop_ranges import range_verdict, sb_completable
from .strategy import IMPLIED_ODDS_CAP_PCT, _is_set_mine_pair

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation


# ─────────────────── 离线审计（legacy offline only） ───────────────────

# 与 replay / compare_review_hands 共用的硬规则审计。新 prompt 不依赖这些标签，
# 它们只用于复盘时回答「这次下注是否与事实矛盾」。口径只看可证伪的事实：
# 牌力解析、坚果校验、牌面结构、赔率，不再读 Value directive。
_ONE_PAIR_TIERS = (OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM)


def audit_advice_tags(sit: Situation, kind: str | None,
                      amount: float | None) -> set[str]:
    """返回建议的违规标签集合（空集 = 没抓到硬错）。"""
    tags: set[str] = set()
    snap = sit.snap
    street = snap.street
    pos = (snap.hero_position or "").upper()
    cards = snap.hero_cards
    to_call = float(snap.to_call or 0.0)
    pot = float(snap.pot or 0.0)
    agg = kind in ("bet", "raise", "allin")
    tier = sit.hand_tier
    gt = sit.ground_truth
    hs = sit.hand_strength
    if hs is None and sit.hand_ranges is not None:
        # live 不再预计算 hand_strength；离线审计需要 kind/size 时这里懒计算。
        try:
            from ... import hand_strength
            if hand_strength.enabled():
                hs = hand_strength.compute_hand_strength(sit)
        except Exception:  # noqa: BLE001 — 审计不能拖垮主流程
            hs = None
    hs_kind = getattr(hs, "kind", None)
    hs_size = getattr(hs, "size", None)
    multiway = (sit.opponents_in_hand or 0) >= 3

    # 翻后：事实说明这不是价值牌（空气/打牌面/多路听牌）却主动下注。
    if agg and (hs_kind in ("weak", "board_play")
                or (hs_kind == "draw" and multiway)):
        tags.add("非价值下注")
    if agg and sit.plays_board:
        if street != "flop" or (amount is not None and pot and amount > 0.5 * pot):
            tags.add("打牌面下注")
    # 非坚果/一对主动加注：只有坚果或牌面明显安全时才不算错。
    if kind == "raise" and tier == STRONG_MADE and gt is not None:
        if not gt.hero_is_nuts and (gt.board_info.paired
                                    or gt.board_info.flush_made_possible):
            tags.add("非坚果加注")
    if kind == "raise" and tier in _ONE_PAIR_TIERS:
        tags.add("一对加注")
    if agg and multiway and tier in (STRONG_DRAW, WEAK_DRAW):
        tags.add("多路听牌下注")

    # 翻前范围。
    if street == PREFLOP and len(cards) == 2:
        verdict = range_verdict(pos, cards)
        if verdict is not None:
            if pos not in ("SB", "BB") and verdict.in_range is False:
                if agg and to_call <= 1.0:
                    tags.add("翻前表外加注")
                if kind == "call" and to_call <= 1.0 and not _is_set_mine_pair(cards):
                    tags.add("翻前表外溜入")
            if (pos not in ("SB", "BB") and verdict.in_range is True
                    and kind == "fold" and to_call <= 1.0):
                tags.add("范围内却弃牌")
        if (pos == "SB" and kind == "call" and to_call <= 1.5
                and not sb_completable(cards)):
            tags.add("SB补盲垃圾牌")
        if (pos not in ("SB", "BB") and kind == "call" and to_call <= 1.0
                and tuple(snap.preflop_shapes) == ("unopened",)):
            tags.add("开池平跟")

    # 听牌在有即时赔率时弃牌。
    if (kind == "fold" and to_call > 0 and tier in (STRONG_DRAW, WEAK_DRAW)
            and sit.equity_pct is not None and pot > 0):
        owed = call_amount(snap)
        winnable = winnable_pot(snap)
        bep = owed / winnable * 100 if owed > 0 and winnable > 0 else 0.0
        if sit.equity_pct > bep + IMPLIED_ODDS_CAP_PCT:
            tags.add("有赔率却弃牌")

    # 强价值牌下注过小。
    if (street != PREFLOP and agg and amount is not None and pot > 0
            and hs_kind == "value" and hs_size in ("big", "very_big")
            and amount < 0.4 * pot):
        tags.add("价值下注过小")
    return tags
