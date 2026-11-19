"""河牌书卡（#84）：价值、诈唬、抓诈唬的匹配、选档与渲染。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poker_ocr.playbook import (  # noqa: E402
    Spot,
    choose_option,
    load_cards,
    render_book_plan_text,
    resolve,
    validate_cards,
)

ROOT = Path(__file__).resolve().parents[1]
RIVER = ROOT / "poker_ocr/playbook/cards/harrington_cash_river.json"

# 本批新增的 9 张河牌卡：价值 1、诈唬 2、抓诈唬 2、摊牌过牌 3、弃牌 1。
NEW_IDS = (
    "river.value.strong_made.bet",
    "river.bluff.air.bet",
    "river.bluff.air.facing_bet",
    "river.bluff_catch.middle_pair.facing_bet",
    "river.bluff_catch.top_pair_weak.facing_bet",
    "river.fold.board_play.facing_bet",
    "river.check.middle_pair",
    "river.check.board_play",
    "river.check.top_pair_weak",
)


def _cards():
    return load_cards(RIVER)


def _all_cards():
    cards = []
    for path in sorted((ROOT / "poker_ocr/playbook/cards").glob("*.json")):
        cards.extend(load_cards(path))
    return cards


def _card(card_id):
    for card in _cards():
        if card.id == card_id:
            return card
    raise AssertionError(f"missing card {card_id}")


def test_river_cards_validate():
    cards = _cards()
    assert len(cards) == 9
    assert validate_cards(cards) == []


def test_each_river_card_resolves_to_itself():
    cards = _cards()
    for card in cards:
        got = resolve(cards, card.spot)
        assert got.card is not None, card.id
        assert got.card.id == card.id, card.id


@pytest.mark.parametrize("spot, expected", [
    (Spot(street="river", players="4+", made_hand="strong_made", facing="none"),
     "river.value.strong_made.bet"),
    (Spot(street="river", players="4+", made_hand="air", facing="none"),
     "river.bluff.air.bet"),
    (Spot(street="river", players="4+", made_hand="air", facing="bet"),
     "river.bluff.air.facing_bet"),
    (Spot(street="river", players="4+", made_hand="middle_or_bottom_pair",
          facing="bet"), "river.bluff_catch.middle_pair.facing_bet"),
    (Spot(street="river", players="4+", made_hand="top_pair_weak_kicker",
          facing="bet"), "river.bluff_catch.top_pair_weak.facing_bet"),
    (Spot(street="river", players="4+", made_hand="board_play", facing="bet"),
     "river.fold.board_play.facing_bet"),
    (Spot(street="river", players="4+", made_hand="middle_or_bottom_pair",
          facing="none"), "river.check.middle_pair"),
    (Spot(street="river", players="4+", made_hand="board_play", facing="none"),
     "river.check.board_play"),
    (Spot(street="river", players="4+", made_hand="top_pair_weak_kicker",
          facing="none"), "river.check.top_pair_weak"),
])
def test_river_bucket_hits_its_card(spot, expected):
    got = resolve(_cards(), spot)
    assert got.card is not None
    assert got.card.id == expected


def test_river_cards_also_cover_three_handed():
    # Ch7 的河牌建议本身没按人数分流，所以河牌卡不写 players；
    # replay 里 3 人池与单挑池的河牌点也一并命中。
    for players in ("2", "3"):
        got = resolve(_cards(), Spot(street="river", players=players,
                                     made_hand="air", facing="none"))
        assert got.card is not None
        assert got.card.id == "river.bluff.air.bet"


def test_river_cards_do_not_leak_to_turn():
    got = resolve(_all_cards(), Spot(street="turn", players="4+",
                                     made_hand="air", draw="none",
                                     facing="none"))
    assert got.card is not None
    assert got.card.id == "turn.mw.air.check"


def _opt(kind, amount=None):
    return SimpleNamespace(kind=kind, amount=amount)


@pytest.mark.parametrize("card_id, options, expected_kind", [
    ("river.value.strong_made.bet",
     [_opt("check"), _opt("bet", 6.7)], "bet"),
    ("river.bluff.air.bet", [_opt("check"), _opt("bet", 6.7)], "bet"),
    ("river.bluff.air.facing_bet",
     [_opt("fold"), _opt("call", 5.0)], "fold"),
    ("river.bluff_catch.middle_pair.facing_bet",
     [_opt("fold"), _opt("call", 5.0), _opt("raise", 20.0)], "call"),
    ("river.bluff_catch.top_pair_weak.facing_bet",
     [_opt("fold"), _opt("call", 5.0)], "call"),
    ("river.fold.board_play.facing_bet",
     [_opt("fold"), _opt("call", 5.0)], "fold"),
    ("river.check.middle_pair", [_opt("check"), _opt("bet", 6.7)], "check"),
    ("river.check.board_play", [_opt("check"), _opt("bet", 6.7)], "check"),
    ("river.check.top_pair_weak", [_opt("check"), _opt("bet", 6.7)], "check"),
])
def test_river_card_lands_on_legal_option(card_id, options, expected_kind):
    match = choose_option(options, _card(card_id).plan, pot_bb=10.0, to_call_bb=5.0)
    assert match is not None
    assert match.option.kind == expected_kind


def test_value_and_bluff_sizes_are_explicit_in_the_card():
    # 票面要求尺寸走卡里的显式值，不在代码里写魔法数字。
    for card_id in ("river.value.strong_made.bet", "river.bluff.air.bet"):
        size = _card(card_id).plan.size
        assert size is not None, card_id
        assert size.kind == "pct_pot"
        assert size.value == 0.67


def test_new_river_cards_carry_printed_and_pdf_pages():
    for card_id in NEW_IDS:
        card = _card(card_id)
        assert card.pages, card_id
        assert card.pdf_pages, card_id
        assert len(card.pages) == len(card.pdf_pages), card_id
        for printed, pdf in zip(card.pages, card.pdf_pages):
            assert pdf - printed == 6, card_id   # 第二卷 PDF/印刷页固定偏移


def _no_draw():
    return SimpleNamespace(draws=SimpleNamespace(
        flush_draw=False, straight_draw=False, oesd=False, gutshot=False))


def _river_sit(**overrides):
    base = dict(
        street="river", pot_type="single-raised pot", opponents_in_hand=3,
        hand_tier="强成牌", has_draw=False, ground_truth=_no_draw(),
        texture="干燥", spr=4.0, in_position=False,
        snap=SimpleNamespace(preflop_shapes=("single-raise",), to_call=0.0,
                             hero_position="BB"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_river_value_bet_renders_plan_and_pages():
    text = render_book_plan_text(_river_sit(hand_tier="强成牌"))
    assert "河牌 / 4+ 人 / 强成牌 / 无人下注" in text
    assert "计划: 下注 0.67 池（第 2 卷 p71 / p82，书中明确）" in text
    assert "书中无此局面" not in text


def test_river_bluff_renders_bet_with_check_mix():
    # 书里标准是过牌；诈唬只在特定条件下，卡里用 50% 混合表达，不落成无条件下注。
    text = render_book_plan_text(_river_sit(hand_tier="空气"))
    assert "河牌 / 4+ 人 / 空气 / 无人下注" in text
    assert "计划: 下注 0.67 池（第 2 卷 p78 / p82，编译归纳）" in text
    assert "混合: 过牌，频率 50%" in text


def test_river_bluff_catch_renders_call():
    text = render_book_plan_text(_river_sit(
        hand_tier="中底对", has_draw=False,
        snap=SimpleNamespace(preflop_shapes=("single-raise",), to_call=7.5,
                             hero_position="BB"),
    ))
    assert "河牌 / 4+ 人 / 中/底对 / 面对下注" in text
    assert "计划: 跟注（第 2 卷 p75 / p85，编译归纳）" in text


def test_strong_made_facing_bet_is_deliberately_unmatched():
    # two pair（书里中等，跟）与 set+（书里强，加注）在 Spot 里都归 strong_made，
    # 一张卡无法同时忠实；宁可留空，也不写死会错一半的基线。
    text = render_book_plan_text(_river_sit(
        hand_tier="强成牌",
        snap=SimpleNamespace(preflop_shapes=("single-raise",), to_call=5.0,
                             hero_position="BB"),
    ))
    assert "书中无此局面" in text


def test_river_board_play_without_bet_renders_check():
    text = render_book_plan_text(_river_sit(hand_tier="打牌面"))
    assert "河牌 / 4+ 人 / 打牌面 / 无人下注" in text
    assert "计划: 过牌（第 2 卷 p74，编译归纳）" in text


def test_river_air_facing_bet_renders_fold():
    text = render_book_plan_text(_river_sit(
        hand_tier="空气",
        snap=SimpleNamespace(preflop_shapes=("single-raise",), to_call=7.5,
                             hero_position="BB"),
    ))
    assert "河牌 / 4+ 人 / 空气 / 面对下注" in text
    assert "计划: 弃牌（第 2 卷 p78，书中明确）" in text


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
