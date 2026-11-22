#!/usr/bin/env python3
"""翻前书卡（票 #87、#91）的验收：主桶、面对 3-bet 层、来源页与 options 落位。

    nv -m pytest tests/test_playbook_cards_preflop.py -q

覆盖：
  1. 新卡文件能加载、validate_cards 全过，且每张卡解析回自己；
  2. RFI / 面对单次加注 / 溜入池三个主桶都有卡命中，且同一手牌类在不同
     位置/花色/位置优势下拿到的计划符合书（前位同花 KQ 平跟、非同花弃；
     前位 88-66 平跟；TT 攻击溜入者加注；后位弱 A 加注等）；
  3. 每张卡都能通过 choose_option 落到本桌合法 options（失败 0）；
  4. 0.5BB 前注：卡里只写书的 3BB 基线，最终尺寸由偏移层夹到本桌合法档，
     动作不被偏移改写；
  5. 面对 3-bet 层（#91）：14 个手牌类各一张卡，AA-KK 再加注、QQ 跟注、
     其余弃；4-bet/5-bet 仍然显式 unknown，不伪造书页。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.playbook import (  # noqa: E402
    Spot, apply_offsets, choose_option, load_cards, resolve, validate_cards,
)
from poker_ocr.playbook.options import kinds_for_action  # noqa: E402
from poker_ocr.playbook.prompt import render_book_plan_zh  # noqa: E402

CARDS = ROOT / "poker_ocr/playbook/cards/harrington_cash_preflop.json"


def _options(*kinds_with_amounts):
    return tuple(
        SimpleNamespace(kind=kind, amount=amount, id=f"opt-{kind}-{i}")
        for i, (kind, amount) in enumerate(kinds_with_amounts)
    )


def _table_options():
    """一张「什么动作都有」的桌：用来验证卡 -> options 的映射不依赖具体金额。"""
    return _options(("check", None), ("fold", None), ("call", 1.0),
                    ("raise", 3.0), ("raise", 6.0), ("allin", 100.0))


def _raises(count):
    """交替的加注序列：1 = 对手开池，2 = 开池 + 本方 3-bet，3 = 再加注……"""
    return tuple(("hero" if index % 2 else "villain", "raise")
                 for index in range(count))


def _sit(*, street="preflop", node="unopened", facing="bet", cards=("Kh", "Kd"),
         position="BTN", in_position=False, pot_type="unopened pot",
         to_call=1.0, pot=6.0, ante=None, options=None, raises=None):
    shape = {"raised": "single-raise", "three_bet": "three-bet"}.get(node, node)
    if raises is None:
        raises = {"raised": _raises(1), "three_bet": _raises(2)}.get(node, ())
    actions = tuple(SimpleNamespace(action=action, is_hero=(who == "hero"))
                    for who, action in raises)
    if to_call == 0.0 and facing == "bet":
        to_call = 0.0
    snap = SimpleNamespace(
        preflop_shapes=((shape,) if shape else ()),
        hero_cards=cards,
        hero_position=position,
        to_call=to_call,
        pot=pot,
        blinds=SimpleNamespace(small=0.5, big=1.0, ante=ante),
        actions_of=lambda street: actions if street == "preflop" else (),
    )
    return SimpleNamespace(
        street=street, pot_type=pot_type, opponents_in_hand=8,
        hand_tier=None, has_draw=False, ground_truth=None, texture=None,
        spr=None, in_position=in_position, snap=snap,
        options=options if options is not None else _table_options(),
    )


def _spot(**kwargs):
    return Spot.from_situation(_sit(**kwargs))


def test_preflop_deck_loads_and_validates():
    cards = load_cards(CARDS)
    assert len(cards) == 87
    assert validate_cards(cards) == []
    for card in cards:
        got = resolve(cards, card.spot)
        assert got.card is not None, card.id
        assert got.card.id == card.id, card.id


def test_all_cards_validate_together():
    from poker_ocr.playbook.prompt import _all_cards
    assert validate_cards(_all_cards()) == []


def test_every_card_has_print_and_pdf_pages():
    for card in load_cards(CARDS):
        assert card.pages, card.id
        assert len(card.pdf_pages) == len(card.pages), card.id
        assert all(pdf > page for page, pdf in zip(card.pages, card.pdf_pages)), card.id


def test_rfi_cards_hit_positions_and_suits():
    cases = [
        (dict(cards=("Ah", "Ad"), position="UTG"), "preflop.rfi.big_pair_aa_kk"),
        (dict(cards=("Kh", "Kd"), position="HJ"), "preflop.rfi.big_pair_aa_kk"),
        (dict(cards=("Qh", "Qd"), position="HJ"), "preflop.rfi.big_pair_qq"),
        (dict(cards=("Jh", "Jd"), position="UTG"), "preflop.rfi.medium_pair.early"),
        (dict(cards=("Jh", "Jd"), position="HJ"), "preflop.rfi.medium_pair.middle"),
        (dict(cards=("9h", "9d"), position="CO"), "preflop.rfi.medium_pair.late"),
        (dict(cards=("8h", "8d"), position="UTG"), "preflop.rfi.low_pair.early"),
        (dict(cards=("8h", "8d"), position="BTN"), "preflop.rfi.low_pair.late"),
        (dict(cards=("Ah", "Kh"), position="CO"), "preflop.rfi.ak"),
        (dict(cards=("Ah", "Qh"), position="CO"), "preflop.rfi.aq"),
        (dict(cards=("Ah", "Jh"), position="UTG"), "preflop.rfi.ace_broadway.early.suited"),
        (dict(cards=("Ah", "Jd"), position="UTG"), "preflop.rfi.ace_broadway.early.offsuit"),
        (dict(cards=("Ah", "Jh"), position="HJ"), "preflop.rfi.ace_broadway.middle"),
        (dict(cards=("Ah", "9h"), position="UTG"), "preflop.rfi.weak_ace.early"),
        (dict(cards=("Ah", "9h"), position="HJ"), "preflop.rfi.weak_ace.middle.suited"),
        (dict(cards=("Ah", "9d"), position="HJ"), "preflop.rfi.weak_ace.middle.offsuit"),
        (dict(cards=("Ah", "9h"), position="BTN"), "preflop.rfi.weak_ace.late"),
        (dict(cards=("Kh", "Qh"), position="UTG"), "preflop.rfi.trouble.early.suited"),
        (dict(cards=("Kh", "Qd"), position="UTG"), "preflop.rfi.trouble.early.offsuit"),
        (dict(cards=("Kh", "Qh"), position="HJ"), "preflop.rfi.trouble.middle"),
        (dict(cards=("Kh", "Qh"), position="BTN"), "preflop.rfi.trouble.late"),
        (dict(cards=("Kh", "Th"), position="UTG"), "preflop.rfi.weak_trouble.early"),
        (dict(cards=("Kh", "Th"), position="HJ"), "preflop.rfi.weak_trouble.middle"),
        (dict(cards=("Kh", "Th"), position="BTN"), "preflop.rfi.weak_trouble.late"),
        (dict(cards=("5h", "5d"), position="UTG"), "preflop.rfi.small_pair.early"),
        (dict(cards=("5h", "5d"), position="BTN"), "preflop.rfi.small_pair.late"),
        (dict(cards=("Jh", "Th"), position="UTG"),
         "preflop.rfi.suited_connector.early"),
        (dict(cards=("Jh", "Th"), position="HJ"),
         "preflop.rfi.suited_connector.middle"),
        (dict(cards=("Jh", "Th"), position="CO"),
         "preflop.rfi.suited_connector.late"),
        (dict(cards=("9h", "7h"), position="CO"), "preflop.rfi.speculative.late"),
        (dict(cards=("9h", "7d"), position="CO"), "preflop.rfi.speculative.late"),
        (dict(cards=("7h", "2d"), position="CO"), "preflop.rfi.other"),
    ]
    cards = load_cards(CARDS)
    for kwargs, expected in cases:
        got = resolve(cards, _spot(**kwargs))
        assert got.card is not None, kwargs
        assert got.card.id == expected, (kwargs, got.card.id)


def test_prompt_renders_primary_and_mix_frequencies():
    """#107：70/30 的混合卡必须同时印出主行动与副分支的频率。"""
    text = "\n".join(render_book_plan_zh(_sit(cards=("Jh", "Jd"), position="UTG")))
    assert "计划: 加注 3BB（频率 70%，" in text
    assert "混合: 跟注 1BB（频率 30%）" in text


def test_medium_and_low_pair_follow_book_mix():
    """书 p110：前位 JJ-99 70% 加注，88-66 30% 加注。"""
    cards = load_cards(CARDS)
    medium = resolve(cards, _spot(cards=("Jh", "Jd"), position="UTG")).card
    low = resolve(cards, _spot(cards=("8h", "8d"), position="UTG")).card
    assert medium.plan.action == "raise" and medium.plan.freq == 0.7
    assert [(m.action, m.freq) for m in medium.mix] == [("call", 0.3)]
    assert low.plan.action == "call" and low.plan.freq == 0.7
    assert [(m.action, m.freq) for m in low.mix] == [("raise", 0.3)]


def test_suited_trouble_plays_early_while_offsuit_folds():
    """书 p120：前位只玩 KQs，KQo 弃；KT/QT 中位直接弃。"""
    cards = load_cards(CARDS)
    suited = resolve(cards, _spot(cards=("Kh", "Qh"), position="UTG")).card
    offsuit = resolve(cards, _spot(cards=("Kh", "Qd"), position="UTG")).card
    weak = resolve(cards, _spot(cards=("Kh", "Th"), position="HJ")).card
    assert suited.plan.action == "call"
    assert offsuit.plan.action == "fold"
    assert weak.plan.action == "fold"


def test_small_pair_limps_early_and_raises_late():
    cards = load_cards(CARDS)
    early = resolve(cards, _spot(cards=("5h", "5d"), position="UTG")).card
    late = resolve(cards, _spot(cards=("5h", "5d"), position="BTN")).card
    assert early.plan.action == "call" and early.plan.size.value == 1.0
    assert late.plan.action == "raise" and late.plan.size.value == 3.0


def test_facing_one_raise_uses_position():
    cards = load_cards(CARDS)
    kwargs = dict(node="raised", pot_type="single-raised pot", to_call=8.0, pot=14.0)

    ip = resolve(cards, _spot(cards=("Jh", "Jd"), in_position=True, **kwargs)).card
    oop = resolve(cards, _spot(cards=("Jh", "Jd"), in_position=False, **kwargs)).card
    unknown = resolve(cards, _spot(cards=("Jh", "Jd"), in_position=None, **kwargs)).card
    assert ip.id == "preflop.vs_raise.medium_pair.ip" and ip.plan.action == "call"
    assert oop.id == "preflop.vs_raise.medium_pair.oop" and oop.plan.action == "fold"
    assert unknown.id == "preflop.vs_raise.medium_pair.unknown"
    assert unknown.plan.action == "fold" and unknown.priority < ip.priority

    big = resolve(cards, _spot(cards=("Ah", "Ad"), **kwargs)).card
    assert big.id == "preflop.vs_raise.big_pair_aa_kk"
    assert big.plan.action == "raise" and big.plan.size.value == 9.0
    # QQ 在单加注池里同样再加注；分岔要等面对 3-bet 才出现（p109）。
    queens = resolve(cards, _spot(cards=("Qh", "Qd"), **kwargs)).card
    assert queens.id == "preflop.vs_raise.big_pair_qq"
    assert queens.plan.action == "raise" and queens.plan.size.value == 9.0


def test_limped_pot_cards():
    cards = load_cards(CARDS)
    kwargs = dict(node="limped", pot_type="limped pot", pot=9.0)
    big = resolve(cards, _spot(cards=("Ah", "Ad"), **kwargs)).card
    tens = resolve(cards, _spot(cards=("Th", "Td"), **kwargs)).card
    low = resolve(cards, _spot(cards=("8h", "8d"), **kwargs)).card
    weak_ace = resolve(cards, _spot(cards=("Ah", "9h"), **kwargs)).card
    junk = resolve(cards, _spot(cards=("7h", "2d"), **kwargs)).card
    pair = resolve(cards, _spot(cards=("5h", "5d"), **kwargs)).card
    assert big.id == "preflop.limped.big_pair_aa_kk" and big.plan.action == "raise"
    queens = resolve(cards, _spot(cards=("Qh", "Qd"), **kwargs)).card
    assert queens.id == "preflop.limped.big_pair_qq" and queens.plan.action == "raise"
    # 书 p129：攻击溜入者时 AA-TT 加注，TT 不能平跟。
    assert tens.id == "preflop.limped.medium_pair" and tens.plan.action == "raise"
    assert low.id == "preflop.limped.low_pair" and low.plan.action == "call"
    assert weak_ace.id == "preflop.limped.weak_ace" and weak_ace.plan.action == "fold"
    assert junk.id == "preflop.limped.other" and junk.plan.action == "fold"
    assert pair.id == "preflop.limped.small_pair" and pair.plan.action == "call"


def test_big_blind_free_check_does_not_steal_other_spots():
    cards = load_cards(CARDS)
    bb_check = resolve(cards, _spot(node="unopened", facing="none", pot_type="unopened pot",
                                    position="BB", to_call=0.0,
                                    cards=("7h", "2d"))).card
    assert bb_check.id == "preflop.bb.check.unopened"
    assert bb_check.plan.action == "check"
    # 非大盲的「不用跟注」不应命中 check 卡（条款写死 hero_position=BB）。
    other = resolve(cards, _spot(node="limped", facing="none",
                                 pot_type="limped pot", position="BTN",
                                 to_call=0.0, cards=("Ad", "6d")))
    assert other.card is None or other.card.id != "preflop.bb.check.limped"


def test_every_card_maps_onto_legal_options():
    failures = []
    for card in load_cards(CARDS):
        match = choose_option(_table_options(), card.plan, pot_bb=6.0, to_call_bb=1.0)
        if match is None or match.option.kind not in kinds_for_action(card.plan.action):
            failures.append((card.id, card.plan.action,
                             None if match is None else match.option.kind))
    assert failures == []


def test_ante_offset_only_changes_size():
    cards = load_cards(CARDS)
    card = next(c for c in cards if c.id == "preflop.rfi.big_pair_aa_kk")
    sit = _sit(cards=("Ah", "Ad"), position="UTG", ante=0.5,
               options=_options(("fold", None), ("call", 1.0),
                                ("raise", 5.5), ("raise", 7.5)))
    deltas = apply_offsets(card, sit)
    assert [d.target for d in deltas] == ["size_bb"], deltas
    assert deltas[0].baseline == "3BB" and deltas[0].final == "5.5BB"
    assert card.plan.action == "raise"      # 卡本身不被改写
    assert resolve(cards, Spot.from_situation(sit)).card is not None


def test_no_ante_no_offset():
    cards = load_cards(CARDS)
    card = next(c for c in cards if c.id == "preflop.rfi.big_pair_aa_kk")
    sit = _sit(cards=("Ah", "Ad"), position="UTG", ante=None)
    assert apply_offsets(card, sit) == ()


# 面对 3-bet 的 14 类各一手样本（词表见 hand_class.py）。
_THREE_BET_HANDS = {
    "big_pair_aa_kk": ("Ah", "Ad"),
    "big_pair_qq": ("Qh", "Qd"),
    "medium_pair": ("Jh", "Jd"),
    "low_pair": ("8h", "8d"),
    "small_pair": ("5h", "5d"),
    "ak": ("Ah", "Kh"),
    "aq": ("Ah", "Qh"),
    "ace_broadway": ("Ah", "Jh"),
    "weak_ace": ("Ah", "9h"),
    "trouble": ("Kh", "Qh"),
    "weak_trouble": ("Kh", "Th"),
    "suited_connector": ("Jh", "Th"),
    "speculative": ("9h", "7h"),
    "other": ("7h", "2d"),
}


def _three_bet_spot(cards, *, raises=2):
    return _spot(node="three_bet", pot_type="3-bet+ pot", facing="raise",
                 to_call=23.0, pot=37.0, cards=cards, raises=_raises(raises))


def test_three_bet_layer_has_one_card_per_hand_class():
    """14 个手牌类各一张面对 3-bet 卡；书只讲到这一层（#91）。"""
    cards = load_cards(CARDS)
    three = [card for card in cards if card.spot.facing_level == "three_bet"]
    assert len(three) == len(_THREE_BET_HANDS)
    assert {card.spot.hand_class for card in three} == set(_THREE_BET_HANDS)
    for card in three:
        got = resolve(cards, _three_bet_spot(_THREE_BET_HANDS[card.spot.hand_class]))
        assert got.card is not None and got.card.id == card.id, card.id


def test_three_bet_actions_follow_book():
    """书 p109：AA-KK 再加注、QQ 最多跟注；p111-122：其余弃。"""
    cards = load_cards(CARDS)
    aa = resolve(cards, _three_bet_spot(("Ah", "Ad"))).card
    assert aa.id == "preflop.vs_3bet.big_pair_aa_kk"
    assert aa.plan.action == "raise" and aa.plan.size is None
    queens = resolve(cards, _three_bet_spot(("Qh", "Qd"))).card
    assert queens.id == "preflop.vs_3bet.big_pair_qq"
    assert queens.plan.action == "call"
    for hand in (("Jh", "Jd"), ("8h", "8d"), ("Ah", "Kh"),
                 ("Kh", "Qh"), ("7h", "2d")):
        got = resolve(cards, _three_bet_spot(hand)).card
        assert got.plan.action == "fold", (hand, got.id)


def test_four_and_five_bet_stay_unknown():
    """书里没有 4-bet/5-bet 的决策：认得出来，但必须显式无卡。"""
    cards = load_cards(CARDS)
    for raises in (3, 4):
        got = resolve(cards, _three_bet_spot(("Ah", "Ad"), raises=raises))
        assert got.card is None, raises


def test_prompt_renders_facing_level():
    sit = _sit(node="three_bet", pot_type="3-bet+ pot", facing="raise",
               to_call=23.0, pot=37.0, cards=("Qh", "Qd"), position="BTN")
    text = "\n".join(render_book_plan_zh(sit))
    assert "面对 3-bet" in text and "本方 3-bet" in text
    assert "大口袋对（QQ）" in text
    assert "计划: 跟注" in text and "p109" in text


def test_validate_rejects_facing_level_outside_preflop():
    import dataclasses
    from poker_ocr.playbook.prompt import _all_cards
    flop = next(c for c in _all_cards() if c.street == "flop")
    bad = dataclasses.replace(
        flop, spot=dataclasses.replace(flop.spot, facing_level="three_bet"))
    errors = validate_cards([bad])
    assert any("facing_level" in e for e in errors), errors


def test_validate_rejects_unknown_facing_level():
    import dataclasses
    from poker_ocr.playbook.prompt import _all_cards
    card = next(c for c in _all_cards() if c.id == "preflop.vs_3bet.ak")
    bad = dataclasses.replace(
        card, spot=dataclasses.replace(card.spot, facing_level="four_bet_x"))
    errors = validate_cards([bad])
    assert any("facing_level" in e for e in errors), errors


def test_raised_pot_without_bet_remains_unknown():
    """面对加注但已无需跟注（facing=none）不是书里讲过的局面。"""
    cards = load_cards(CARDS)
    got = resolve(cards, _spot(node="raised", pot_type="single-raised pot",
                               facing="none", to_call=0.0, pot=14.0,
                               cards=("Jh", "Jd"), position="BTN"))
    assert got.card is None


def test_prompt_renders_preflop_hand_class_and_source():
    sit = _sit(cards=("5h", "5d"), position="UTG")
    lines = render_book_plan_zh(sit)
    text = "\n".join(lines)
    assert "翻前" in text and "前位" in text and "小口袋对" in text
    assert "非同花" in text
    assert "计划: 跟注 1BB" in text
    assert "p113" in text
    assert "书中无此局面" not in text


def test_prompt_renders_suited_status():
    suited = "\n".join(render_book_plan_zh(_sit(cards=("Kh", "Qh"), position="UTG")))
    offsuit = "\n".join(render_book_plan_zh(_sit(cards=("Kh", "Qd"), position="UTG")))
    assert "麻烦牌型 / 同花" in suited
    assert "麻烦牌型 / 非同花" in offsuit


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

def test_validate_rejects_suited_flag_outside_preflop():
    """hand_suited 只对翻前有意义；翻后卡带上它是编译错误。"""
    import dataclasses
    from poker_ocr.playbook.prompt import _all_cards
    flop = next(c for c in _all_cards() if c.street == "flop")
    bad = dataclasses.replace(
        flop, spot=dataclasses.replace(flop.spot, hand_suited=True))
    errors = validate_cards([bad])
    assert any("hand_suited" in e for e in errors), errors


def test_validate_rejects_non_bool_suited_flag():
    import dataclasses
    from poker_ocr.playbook.prompt import _all_cards
    card = next(c for c in _all_cards() if c.id == "preflop.rfi.ak")
    bad = dataclasses.replace(
        card, spot=dataclasses.replace(card.spot, hand_suited="yes"))
    errors = validate_cards([bad])
    assert any("hand_suited" in e for e in errors), errors
