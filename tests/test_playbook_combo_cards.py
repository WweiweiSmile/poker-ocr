"""#108 成牌+听牌交叉：去 draw 键、95 覆盖卡、两张转牌精确卡。"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poker_ocr.playbook import Spot, load_cards, resolve, validate_cards

ROOT = Path(__file__).resolve().parents[1]
MULTIWAY = ROOT / "poker_ocr/playbook/cards/harrington_cash_multiway.json"

# 成牌卡不再写 draw:"none"，带听牌时也能命中（#108）。
STRIPPED_IDS = (
    "flop.mw.middle_pair.check",
    "flop.mw.top_pair_weak.facing_bet",
    "turn.mw.strong_made.bet",
    "turn.mw.overpair.bet",
    "turn.mw.top_pair_top.bet",
    "turn.mw.top_pair_top.facing_bet",
    "turn.mw.top_pair_top.facing_raise",
    "turn.mw.top_pair_weak.check",
)


def _card(cards, card_id):
    return next(card for card in cards if card.id == card_id)


def test_made_hand_cards_accept_draws():
    cards = load_cards(MULTIWAY)
    for card_id in STRIPPED_IDS:
        card = _card(cards, card_id)
        assert card.spot.draw is None, card_id
        for draw in ("none", "flush"):
            got = resolve(cards, replace(card.spot, draw=draw))
            assert got.card is not None, (card_id, draw)
            assert got.card.id == card_id, (card_id, draw, got.card.id)


def test_facing_bet_override_layers_by_draw():
    cards = load_cards(MULTIWAY)
    base = Spot(
        street="flop",
        players="3+",
        made_hand="middle_or_bottom_pair",
        facing="bet",
    )
    cases = (
        ("none", "flop.mw.middle_pair.facing_bet", "fold"),
        ("flush", "flop.mw.middle_pair.facing_bet.with_draw", "call"),
        ("oesd", "flop.mw.middle_pair.facing_bet.with_draw", "call"),
        ("gutshot", "flop.mw.middle_pair.facing_bet.with_draw", "call"),
    )
    for draw, expected_id, expected_action in cases:
        got = resolve(cards, replace(base, draw=draw))
        assert got.card is not None, draw
        assert got.card.id == expected_id, (draw, got.card.id)
        assert got.card.plan.action == expected_action, (draw, got.card.plan.action)


def test_exact_turn_gap_cards():
    cards = load_cards(MULTIWAY)

    got = resolve(
        cards,
        Spot(
            street="turn",
            players="3+",
            made_hand="middle_or_bottom_pair",
            draw="gutshot",
            facing="raise",
        ),
    )
    assert got.card is not None
    assert got.card.id == "turn.mw.middle_pair.facing_raise"
    assert got.card.plan.action == "fold"

    got = resolve(
        cards,
        Spot(
            street="turn",
            players="3+",
            made_hand="top_pair_weak_kicker",
            draw="gutshot",
            facing="bet",
        ),
    )
    assert got.card is not None
    assert got.card.id == "turn.mw.top_pair.facing_bet.gutshot"
    assert got.card.plan.action == "fold"


def test_exact_cards_do_not_steal_flush_or_oesd():
    """精确卡只写 gutshot；flush/OESD 留给未来更精确的卡，不拿卡顺卡硬套。"""
    cards = load_cards(MULTIWAY)
    for draw in ("flush", "oesd"):
        got = resolve(
            cards,
            Spot(
                street="turn",
                players="3+",
                made_hand="top_pair_weak_kicker",
                draw=draw,
                facing="bet",
            ),
        )
        assert got.card is None, (draw, got.card.id if got.card else None)


def test_combo_cards_validate():
    cards = load_cards(MULTIWAY)
    assert len(cards) == 49
    assert validate_cards(cards) == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
