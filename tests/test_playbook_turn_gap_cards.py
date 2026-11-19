"""转牌缺口卡（#83）：三类缺口的匹配、选档与渲染。"""

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
)

ROOT = Path(__file__).resolve().parents[1]
MULTIWAY = ROOT / "poker_ocr/playbook/cards/harrington_cash_multiway.json"

# 本批新增的 7 张转牌卡；票 #83 要求的三类（听牌无人下注 / 打牌面 /
# 中对面对下注）都在其中，另外两张是强成牌面对下注/加注。
NEW_IDS = (
    "turn.mw.draw.bet",
    "turn.mw.board_play.check",
    "turn.mw.middle_pair.facing_bet",
    "turn.mw.top_pair_weak.check",
    "turn.mw.strong_made.facing_bet",
    "turn.mw.strong_made.facing_raise",
    "turn.mw.board_play.facing_bet",
)


def _cards():
    return load_cards(MULTIWAY)


def _card(card_id):
    for card in _cards():
        if card.id == card_id:
            return card
    raise AssertionError(f"missing card {card_id}")


@pytest.mark.parametrize("draw", ["gutshot", "oesd", "flush", "combo"])
def test_turn_draw_without_bet_is_bet(draw):
    got = resolve(_cards(), Spot(street="turn", players="4+", made_hand="draw",
                                 draw=draw, facing="none"))
    assert got.card is not None
    assert got.card.id == "turn.mw.draw.bet"


def test_turn_draw_without_bet_also_covers_three_handed():
    # replay 里还有两个 3 人池的听牌无人下注点；这张卡不按人数分流
    # （Ch6 的转牌建议本身没按人数切），否则它们仍落在「书中无此局面」。
    got = resolve(_cards(), Spot(street="turn", players="3", made_hand="draw",
                                 draw="flush", facing="none"))
    assert got.card is not None
    assert got.card.id == "turn.mw.draw.bet"


@pytest.mark.parametrize("spot, expected", [
    (Spot(street="turn", players="4+", made_hand="board_play", facing="none"),
     "turn.mw.board_play.check"),
    (Spot(street="turn", players="4+", made_hand="middle_or_bottom_pair",
          facing="bet"), "turn.mw.middle_pair.facing_bet"),
    (Spot(street="turn", players="4+", made_hand="top_pair_weak_kicker",
          draw="none", facing="none"), "turn.mw.top_pair_weak.check"),
    (Spot(street="turn", players="4+", made_hand="strong_made", facing="bet"),
     "turn.mw.strong_made.facing_bet"),
    (Spot(street="turn", players="4+", made_hand="strong_made", facing="raise"),
     "turn.mw.strong_made.facing_raise"),
    (Spot(street="turn", players="4+", made_hand="board_play", facing="bet"),
     "turn.mw.board_play.facing_bet"),
])
def test_turn_gap_spot_hits_its_card(spot, expected):
    got = resolve(_cards(), spot)
    assert got.card is not None
    assert got.card.id == expected


def _opt(kind, amount=None):
    return SimpleNamespace(kind=kind, amount=amount)


@pytest.mark.parametrize("card_id, options, expected_kind", [
    ("turn.mw.draw.bet", [_opt("check"), _opt("bet", 6.7)], "bet"),
    ("turn.mw.board_play.check", [_opt("check"), _opt("bet", 10.0)], "check"),
    ("turn.mw.middle_pair.facing_bet",
     [_opt("fold"), _opt("call", 10.0), _opt("raise", 20.0)], "fold"),
    ("turn.mw.top_pair_weak.check", [_opt("check"), _opt("bet", 10.0)], "check"),
    ("turn.mw.strong_made.facing_bet",
     [_opt("fold"), _opt("call", 10.0), _opt("raise", 20.0)], "raise"),
    ("turn.mw.strong_made.facing_raise",
     [_opt("fold"), _opt("call", 10.0), _opt("raise", 20.0), _opt("raise", 40.0)],
     "raise"),
    ("turn.mw.board_play.facing_bet",
     [_opt("fold"), _opt("call", 10.0), _opt("raise", 20.0)], "fold"),
])
def test_turn_card_lands_on_legal_option(card_id, options, expected_kind):
    match = choose_option(options, _card(card_id).plan, pot_bb=10.0, to_call_bb=5.0)
    assert match is not None
    assert match.option.kind == expected_kind


def test_new_turn_cards_carry_printed_and_pdf_pages():
    for card_id in NEW_IDS:
        card = _card(card_id)
        assert card.pages, card_id
        assert card.pdf_pages, card_id
        assert len(card.pages) == len(card.pdf_pages), card_id
        for printed, pdf in zip(card.pages, card.pdf_pages):
            assert pdf - printed == 6, card_id   # 第二卷 PDF/印刷页固定偏移


def _turn_sit(**overrides):
    base = dict(
        street="turn", pot_type="single-raised pot", opponents_in_hand=3,
        hand_tier="弱听", has_draw=True,
        ground_truth=SimpleNamespace(draws=SimpleNamespace(
            flush_draw=True, straight_draw=False, oesd=False, gutshot=False)),
        texture="公对面", spr=4.0, in_position=False,
        snap=SimpleNamespace(preflop_shapes=("single-raise",), to_call=0.0,
                             hero_position="BB"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_turn_draw_renders_plan_and_source_page():
    text = render_book_plan_text(_turn_sit())
    assert "转牌 / 4+ 人 / 听牌 / 无人下注" in text
    assert "计划: 下注 0.67 池（第 2 卷 p60，编译归纳）" in text
    assert "书中无此局面" not in text


def test_turn_middle_pair_facing_bet_renders_fold():
    text = render_book_plan_text(_turn_sit(
        hand_tier="中底对", has_draw=False,
        ground_truth=SimpleNamespace(draws=SimpleNamespace(
            flush_draw=False, straight_draw=False, oesd=False, gutshot=False)),
        snap=SimpleNamespace(preflop_shapes=("single-raise",), to_call=7.5,
                             hero_position="BB"),
    ))
    assert "转牌 / 4+ 人 / 中/底对 / 面对下注" in text
    assert "计划: 弃牌（第 2 卷 p45，编译归纳）" in text


def test_turn_board_play_without_bet_renders_check():
    text = render_book_plan_text(_turn_sit(
        hand_tier="打牌面", has_draw=False,
        ground_truth=SimpleNamespace(draws=SimpleNamespace(
            flush_draw=False, straight_draw=False, oesd=False, gutshot=False)),
    ))
    assert "转牌 / 4+ 人 / 打牌面 / 无人下注" in text
    assert "计划: 过牌（第 2 卷 p21，编译归纳）" in text


def _no_draw():
    return SimpleNamespace(draws=SimpleNamespace(
        flush_draw=False, straight_draw=False, oesd=False, gutshot=False))


def test_turn_top_pair_weak_renders_check():
    text = render_book_plan_text(_turn_sit(
        hand_tier="顶对弱踢", has_draw=False, ground_truth=_no_draw()))
    assert "转牌 / 4+ 人 / 顶对弱踢 / 无人下注" in text
    assert "计划: 过牌（第 2 卷 p46 / p47，编译归纳）" in text


def test_turn_strong_made_facing_bet_renders_raise():
    text = render_book_plan_text(_turn_sit(
        hand_tier="强成牌", has_draw=False, ground_truth=_no_draw(),
        snap=SimpleNamespace(preflop_shapes=("single-raise",), to_call=5.0,
                             hero_position="BB"),
    ))
    assert "转牌 / 4+ 人 / 强成牌 / 面对下注" in text
    assert "计划: 加注 1 池（第 2 卷 p12，编译归纳）" in text


def test_turn_strong_made_in_three_bet_pot_renders_raise():
    text = render_book_plan_text(_turn_sit(
        pot_type="3-bet+ pot", hand_tier="强成牌", has_draw=False,
        ground_truth=_no_draw(),
        snap=SimpleNamespace(preflop_shapes=("3-bet",), to_call=10.0,
                             hero_position="BB"),
    ))
    assert "转牌 / 4+ 人 / 强成牌 / 面对加注" in text
    assert "计划: 加注 1 池（第 2 卷 p35，编译归纳）" in text


def test_turn_board_play_facing_bet_renders_fold():
    text = render_book_plan_text(_turn_sit(
        hand_tier="打牌面", has_draw=False, ground_truth=_no_draw(),
        snap=SimpleNamespace(preflop_shapes=("single-raise",), to_call=7.5,
                             hero_position="BB"),
    ))
    assert "转牌 / 4+ 人 / 打牌面 / 面对下注" in text
    assert "计划: 弃牌（第 2 卷 p24，编译归纳）" in text


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
