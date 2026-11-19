"""Harrington 场景卡 schema 的最小回归：匹配、解析、编译期冲突。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from types import SimpleNamespace

import pytest

from poker_ocr.playbook import (
    Card,
    Plan,
    Size,
    Spot,
    load_cards,
    resolve,
    validate_cards,
)


def _card(cid="c1", *, street="flop", priority=100, **spot):
    base = dict(street=street, made_hand="top_pair_top_kicker", board="dry",
                players="2", in_position=True, facing="bet")
    base.update(spot)
    return Card(
        id=cid, book="harrington_cash", volume=1, chapter="ch4",
        pages=(180,), street=street, spot=Spot(**base),
        plan=Plan(action="call", size=None),
        why="测试卡", priority=priority,
    )


def test_spot_matches_ignores_none_fields():
    card = Spot(street="flop", made_hand="top_pair_top_kicker")
    actual = Spot(street="flop", made_hand="top_pair_top_kicker", board="flush",
                  players="3")
    assert card.matches(actual)
    assert not Spot(street="turn", made_hand="top_pair_top_kicker").matches(actual)


def test_specificity_counts_fields():
    assert Spot(street="flop").specificity() == 1
    assert Spot(street="flop", made_hand="air", facing="bet").specificity() == 3


def test_resolve_prefers_specific_then_priority():
    broad = _card("broad", priority=100)
    narrow = _card("narrow", priority=100, draw="flush")
    spot = Spot(street="flop", made_hand="top_pair_top_kicker", board="dry",
                players="2", in_position=True, facing="bet", draw="flush")
    assert resolve([broad, narrow], spot).card.id == "narrow"
    assert resolve([broad], Spot(street="river")).card is None


def test_resolve_prefers_higher_priority_number():
    low = _card("low", priority=10)
    high = _card("high", priority=100)
    spot = Spot(street="flop", made_hand="top_pair_top_kicker", board="dry",
                players="2", in_position=True, facing="bet")
    assert resolve([low, high], spot).card.id == "high"


def test_same_priority_overlap_is_compile_error():
    a = _card("a")
    b = _card("b")
    errors = validate_cards([a, b])
    assert any("同优先级且 spot 有交集" in e for e in errors)


def test_same_priority_disjoint_is_ok():
    a = _card("a", board="dry")
    b = _card("b", board="flush")
    assert validate_cards([a, b]) == []


def test_bad_card_fields_are_reported():
    card = _card("bad", priority=100)
    broken = Card(
        id="bad", book="", volume=0, chapter="", pages=(), street="flop",
        spot=Spot(street="flop"), plan=Plan(action="shove"),
        why="", confidence="guess", priority=-1, offset_policy="free",
    )
    errors = validate_cards([card, broken])
    joined = "\n".join(errors)
    assert "id 重复" in joined
    assert "action 非法" in joined
    assert "pages 为空" in joined
    assert "confidence 非法" in joined
    assert "offset_policy 非法" in joined


def test_from_situation_maps_existing_facts():
    draws = SimpleNamespace(flush_draw=True, straight_draw=False, oesd=False,
                            gutshot=False)
    sit = SimpleNamespace(
        street="flop",
        snap=SimpleNamespace(to_call=2.0, hero_position="BTN",
                             preflop_shapes=("single-raise",)),
        pot_type="single-raised pot",
        opponents_in_hand=2,
        in_position=True,
        hand_tier="顶对强踢",
        texture="花面",
        spr=4.2,
        has_draw=True,
        ground_truth=SimpleNamespace(draws=draws),
    )
    spot = Spot.from_situation(sit)
    assert spot.street == "flop"
    assert spot.pot_type == "single_raised"
    assert spot.players == "3"
    assert spot.made_hand == "top_pair_top_kicker"
    assert spot.draw == "flush"
    assert spot.board == "flush"
    assert spot.spr == "3to6"
    assert spot.facing == "bet"
    assert spot.commitment == "uncommitted"
    assert spot.preflop_node == "raised"


def test_load_cards_roundtrip(tmp_path):
    path = tmp_path / "cards.json"
    path.write_text(
        '{"cards": [{"id": "c1", "book": "harrington_cash", "volume": 1,'
        ' "chapter": "ch4", "pages": [180], "street": "flop",'
        ' "spot": {"street": "flop", "made_hand": "top_pair_top_kicker"},'
        ' "plan": {"action": "bet", "size": {"kind": "pct_pot", "value": 0.5}},'
        ' "why": "测试", "confidence": "explicit", "priority": 10,'
        ' "offset_policy": "size_only"}]}',
        encoding="utf-8",
    )
    cards = load_cards(path)
    assert len(cards) == 1
    assert cards[0].plan.size == Size(kind="pct_pot", value=0.5)
    assert validate_cards(cards) == []
    assert cards[0].to_dict()["spot"]["made_hand"] == "top_pair_top_kicker"


def test_mixed_frequency_roundtrip(tmp_path):
    path = tmp_path / "cards.json"
    path.write_text(
        '{"cards": [{"id": "mix", "book": "harrington_cash", "volume": 1,'
        ' "chapter": "ch4", "pages": [190], "street": "flop",'
        ' "spot": {"street": "flop", "made_hand": "top_pair_top_kicker"},'
        ' "plan": {"action": "check", "freq": 0.7},'
        ' "mix": [{"action": "bet", "freq": 0.3,'
        ' "size": {"kind": "pct_pot", "value": 0.5}}],'
        ' "why": "测试混合频率", "confidence": "explicit", "priority": 10,'
        ' "offset_policy": "size_only"}]}',
        encoding="utf-8",
    )
    cards = load_cards(path)
    card = cards[0]
    assert card.plan.freq == 0.7
    assert card.mix[0].action == "bet"
    assert card.mix[0].freq == 0.3
    assert validate_cards(cards) == []
    assert card.to_dict()["mix"][0]["size"]["value"] == 0.5


def test_frequency_sum_over_one_is_error():
    card = Card(
        id="over", book="harrington_cash", volume=1, chapter="ch4",
        pages=(190,), street="flop",
        spot=Spot(street="flop"), plan=Plan(action="check", freq=0.8),
        mix=(Plan(action="bet", freq=0.4),), why="测试",
    )
    errors = validate_cards([card])
    assert any("freq 合计" in e for e in errors)

if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
