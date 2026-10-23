#!/usr/bin/env python3
"""Ground Truth 事实层回归：坚果校验、两张同花 ≠ 花面、最好五张、提示词注入。

    .venv/bin/python tests/test_ground_truth.py
    .venv/bin/python -m pytest tests/test_ground_truth.py -q

对应 2026-10-23 复盘的两次退化：
  · 990010 转牌 `Jc8dKdQc`：只有两张方块/梅花，模型却读成「花面」，把坚果顺
    降级成 call；
  · 990009 河牌 `Jc8dKdQcAd`：三张方块，顺子确实不是坚果，模型该降级时没降。

这里把「牌面能不能成花」「有没有人打得过我」都做成断言，跑不过就不许上线。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import (  # noqa: E402
    build_messages, build_situation, render_hand_facts, value_directive,
)
from poker_ocr.coach import CATEGORY_NAMES, parse_cards  # noqa: E402
from poker_ocr.ground_truth import card_text, compute_ground_truth  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402

BLINDS = Blinds(sb=0.5, bb=1.0, ante=0.5, raw="1/2(1)")


def snap(*, board, hero_cards, street="flop", n_opp=3, to_call=0.0,
         pot=20.0, table_size=9) -> HandSnapshot:
    opps = tuple(OpponentSnapshot(f"o{i}", None, None, 100.0, 0.0, False)
                 for i in range(n_opp))
    return HandSnapshot(
        hand_id="h1", hand_index=1, street=street, board=tuple(board),
        hero_cards=tuple(hero_cards), hero_position="BB", hero_stack=100.0,
        hero_bet=0.0, hero_invested=2.0, to_call=to_call, second_bet=None,
        pot=pot, pot_before_street=pot, table_size=table_size, blinds=BLINDS,
        opponents=opps,
        streets=(StreetRecord("preflop", 0.0, ()),
                 StreetRecord(street, pot, ())),
        preflop_shapes=(), hero_is_pfr=False, cbet_spot=False,
        decision_key=("h1", street, 0, pot, to_call, tuple(hero_cards)),
        players_dealt=n_opp + 1,
    )


# ─────────────────────────── 坚果校验 ───────────────────────────

def test_990010_turn_nut_straight_on_two_tone_board() -> None:
    """Jc8dKdQc + AsTh：坚果顺，牌面只有两张方块/梅花，没有成花。"""
    gt = compute_ground_truth("AsTh", "Jc8dKdQc", opponents=3)
    assert gt is not None
    assert gt.made.name == "Straight"
    assert "A-high" in gt.made.detail
    assert gt.hero_is_nuts, "A-K-Q-J-T 在 Jc8dKdQc 上是坚果，没有任何对手持牌能赢"
    assert gt.rank.beaten_by == ()
    assert gt.rank.tie_pct > 0, "AT 分池，tie 比例应 > 0"
    assert gt.board_info.flush_made_possible is False
    assert gt.board_info.flush_draw_possible is True

    text = gt.render()
    assert "no made flush possible" in text
    assert "NUTS" in text
    assert "Beaten by: nothing" in text
    # 绝不能把两张同花说成「花面」。
    assert "FLUSH: possible" not in text


def test_990009_river_straight_loses_to_flush() -> None:
    """Jc8dKdQcAd + AsTh：三张方块，顺子不是坚果，beaten_by 必须含同花。"""
    gt = compute_ground_truth("AsTh", "Jc8dKdQcAd")
    assert gt is not None
    assert gt.made.name == "Straight"
    assert not gt.hero_is_nuts
    names = [b.name for b in gt.rank.beaten_by]
    assert "Flush" in names, f"顺子应被同花打败，实际 {names}"
    assert gt.board_info.flush_made_possible is True
    assert "NOT THE NUTS" in gt.render()
    assert "A flush is ALREADY possible" in gt.render()


def test_proposal_case_flush_beats_straight() -> None:
    """用户给的断言用例：AhKhQhJh2c + Ts9s 必须是顺子，且 beaten_by 含同花。"""
    gt = compute_ground_truth("Ts9s", "AhKhQhJh2c")
    assert gt is not None
    assert gt.made.name == "Straight"
    names = [b.name for b in gt.rank.beaten_by]
    assert "Flush" in names, f"beaten_by 应含 Flush，实际 {names}"
    assert "Straight flush" in names, "四张红心 + Th 还有同花顺"
    assert not gt.hero_is_nuts


def test_wheel_uses_five_as_high_card() -> None:
    """A2345 是 5-high 顺子；A 的双重角色不能算成 A-high。"""
    gt = compute_ground_truth("5c4d", "Ah2s3d9c")
    assert gt is not None
    assert gt.made.name == "Straight"
    assert "5-high" in gt.made.detail
    assert gt.hero_is_nuts


def test_evaluator_ordering_rules() -> None:
    """顺子比顶端、葫芦先比三条、同花不分花色 —— 事实层直接复用 coach.evaluate。"""
    from poker_ocr.coach import evaluate
    wheel = evaluate(parse_cards("Ah2s3d4c5h"))
    six_high = evaluate(parse_cards("2s3d4c5h6s"))
    assert six_high > wheel, "6-high 顺子必须大于 5-high 轮子"

    flush = evaluate(parse_cards("AhKhQhJh9h"))
    boat = evaluate(parse_cards("AhAdAcKhKd"))
    assert boat > flush, "葫芦必须大于同花"

    trips = evaluate(parse_cards("AhAdAcKhQd"))
    straight = evaluate(parse_cards("9h8d7c6s5h"))
    assert straight > trips, "顺子必须大于三条"


def test_paired_board_allows_full_house() -> None:
    """公对面：同花也不是坚果，葫芦/四条都在 beaten_by 里。"""
    gt = compute_ground_truth("Ah3h", "7h7cKh2h", opponents=3)
    assert gt is not None
    assert gt.made.name == "Flush"
    names = [b.name for b in gt.rank.beaten_by]
    assert "Full house" in names or "Quads" in names
    assert not gt.hero_is_nuts
    assert "paired" in gt.render().lower()


# ─────────────────────────── 最好五张 / 底牌贡献 ───────────────────────────

def test_best_five_and_hero_cards_used() -> None:
    gt = compute_ground_truth("AsTh", "Jc8dKdQc")
    assert gt is not None
    five = {card_text(c) for c in gt.made.best_five}
    assert five == {"As", "Th", "Jc", "Qc", "Kd"}
    used = {card_text(c) for c in gt.made.hero_cards_used}
    assert used == {"As", "Th"}

    # 22 在 88T：两张 2 都要用上，不是「打牌面」。
    gt2 = compute_ground_truth("2d2c", "8d8hTh", opponents=7)
    assert gt2 is not None
    assert {card_text(c) for c in gt2.made.hero_cards_used} == {"2d", "2c"}


def test_board_play_uses_no_hole_cards() -> None:
    """公共牌自己就是顺子：最好五张全来自公共牌。"""
    gt = compute_ground_truth("2d3d", "9c8d7s6h5c")
    assert gt is not None
    assert gt.made.name == "Straight"
    assert gt.made.hero_cards_used == ()
    assert "playing the board" in gt.render()


# ─────────────────────────── 听牌口径 ───────────────────────────

def test_flush_draw_is_nine_outs_and_nut() -> None:
    gt = compute_ground_truth("AhKh", "Qh9h2c")
    assert gt is not None
    d = gt.draws
    assert d.flush_draw and d.nut_flush_draw
    assert d.flush_outs == 9
    assert d.total_outs == 9
    assert d.equity_pct == 36.0          # 翻牌 ×4


def test_oesd_and_gutshot() -> None:
    oesd = compute_ground_truth("9s8s", "7c6d2h")
    assert oesd is not None and oesd.draws.oesd and oesd.draws.straight_outs == 8
    gut = compute_ground_truth("QdJd", "9c8h2s")
    assert gut is not None and gut.draws.gutshot and gut.draws.straight_outs == 4


def test_river_has_no_draws() -> None:
    gt = compute_ground_truth("AsTh", "Jc8dKdQcAd")
    assert gt is not None
    assert not gt.draws.flush_draw and not gt.draws.straight_draw
    assert gt.draws.total_outs == 0


def test_two_pair_does_not_get_phantom_straight_outs() -> None:
    """22 在 88T 上补 2 是葫芦，不是顺子补牌。"""
    gt = compute_ground_truth("2d2c", "8d8hTh", opponents=7)
    assert gt is not None
    assert gt.draws.total_outs == 0


# ─────────────────────────── 输入校验 ───────────────────────────

def test_no_ground_truth_preflop_or_short_board() -> None:
    assert compute_ground_truth("AsTh", "") is None
    assert compute_ground_truth("AsTh", "Jc8d") is None
    assert compute_ground_truth("As", "Jc8dKdQc") is None
    assert compute_ground_truth("AsAs", "Jc8dKdQc") is None
    assert compute_ground_truth("AsTh", "As8dKdQc") is None   # 重叠牌


# ─────────────────────────── 提示词注入 ───────────────────────────

def test_prompt_contains_ground_truth_and_forced_rules() -> None:
    sit = build_situation(snap(board=("Jc", "8d", "Kd", "Qc"),
                               hero_cards=("As", "Th"), street="turn",
                               n_opp=3, pot=72.0, to_call=36.0))
    assert sit.ground_truth is not None

    facts = render_hand_facts(sit)
    assert "GROUND TRUTH" in facts
    assert "no made flush possible" in facts
    assert "NUT STATUS: NUTS" in facts
    assert "My hand strength" in facts

    directive = value_directive(sit) or ""
    assert "ABSOLUTE NUTS" in directive
    assert "RAISE" in directive

    system, user = build_messages(sit)
    assert "GROUND TRUTH (highest priority" in system
    assert "No made flush possible" in system
    assert "GROUND TRUTH" in user


def test_river_prompt_says_flush_beats_straight() -> None:
    sit = build_situation(snap(board=("Jc", "8d", "Kd", "Qc", "Ad"),
                               hero_cards=("As", "Th"), street="river",
                               n_opp=1, pot=358.0, to_call=144.5))
    facts = render_hand_facts(sit)
    assert "NOT THE NUTS" in facts
    assert "Flush" in facts
    directive = value_directive(sit) or ""
    assert "VALUE CAUTION" in directive
    assert "Do NOT raise" in directive


def test_draws_are_not_judged_by_made_hand_rank() -> None:
    """坚果同花听牌此刻只是 A 高；排名低不代表该弃牌。"""
    sit = build_situation(snap(board=("Qh", "9h", "2c"),
                               hero_cards=("Ah", "Kh"), n_opp=2, pot=34.5))
    facts = render_hand_facts(sit)
    assert "does NOT apply to a draw" in facts
    assert sit.ground_truth is not None
    assert sit.ground_truth.draws.nut_flush_draw


def test_rank_text_labels_equity_per_opponent_and_multiway() -> None:
    """equity 是**单对手**口径；多人要单独的 at-least-one 数字。"""
    gt = compute_ground_truth("Js3s", "Jd4d5s", opponents=5)
    assert gt is not None
    text = gt.render()
    assert "PER OPPONENT" in text
    assert "vs ONE opponent" in text
    assert "Multiway: with 5 opponents" in text
    assert "chance at least one is ahead" in text
    expected = f"{gt.rank.beat_at_least_pct * 100:.1f}%"
    assert expected in text, f"缺少多人 at-least-one 数字 {expected}"


def test_draw_focus_hides_made_hand_nut_status() -> None:
    """纯听牌不能渲染会压过 SEMI-BLUFF 的 NUT STATUS / Beaten by。"""
    gt = compute_ground_truth("AsKd", "Ts8s9s")
    assert gt is not None and gt.draws.nut_flush_draw
    text = gt.render(draw_focus=True)
    assert "NUT STATUS" not in text
    assert "Beaten by:" not in text
    assert "DRAW STATUS (action basis" in text
    assert "semi-bluff is the baseline" in text
    assert "NO FOLD EQUITY" in text
    # 默认渲染仍保留成手牌口径，供非听牌场景使用。
    assert "NUT STATUS" in gt.render()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
