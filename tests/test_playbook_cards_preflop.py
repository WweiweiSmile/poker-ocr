#!/usr/bin/env python3
"""翻前书卡（票 #87）的验收：三个主桶、每张卡的来源页与 options 落位。

    nv -m pytest tests/test_playbook_cards_preflop.py -q

覆盖：
  1. 新卡文件能加载、validate_cards 全过，且每张卡解析回自己；
  2. RFI / 面对单次加注 / 溜入池三个主桶都有卡命中，且同一手牌类在不同
     位置/花色/位置优势下拿到的计划符合书（前位同花 KQ 平跟、非同花弃；
     前位 88-66 平跟；TT 攻击溜入者加注；后位弱 A 加注等）；
  3. 每张卡都能通过 choose_option 落到本桌合法 options（失败 0）；
  4. 0.5BB 前注：卡里只写书的 3BB 基线，最终尺寸由偏移层夹到本桌合法档，
     动作不被偏移改写；
  5. 未做的桶（3-bet/4-bet、翻前已经无人下注的非大盲位）显式 unknown，
     不伪造书页。
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


def _sit(*, street="preflop", node="unopened", facing="bet", cards=("Kh", "Kd"),
         position="BTN", in_position=False, pot_type="unopened pot",
         to_call=1.0, pot=6.0, ante=None, options=None):
    shape = {"raised": "single-raise", "three_bet": "three-bet"}.get(node, node)
    if to_call == 0.0 and facing == "bet":
        to_call = 0.0
    snap = SimpleNamespace(
        preflop_shapes=((shape,) if shape else ()),
        hero_cards=cards,
        hero_position=position,
        to_call=to_call,
        pot=pot,
        blinds=SimpleNamespace(small=0.5, big=1.0, ante=ante),
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
    assert len(cards) == 70
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
        (dict(cards=("Ah", "Ad"), position="UTG"), "preflop.rfi.big_pair"),
        (dict(cards=("Kh", "Kd"), position="HJ"), "preflop.rfi.big_pair"),
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
    assert big.id == "preflop.vs_raise.big_pair"
    assert big.plan.action == "raise" and big.plan.size.value == 9.0


def test_limped_pot_cards():
    cards = load_cards(CARDS)
    kwargs = dict(node="limped", pot_type="limped pot", pot=9.0)
    big = resolve(cards, _spot(cards=("Ah", "Ad"), **kwargs)).card
    tens = resolve(cards, _spot(cards=("Th", "Td"), **kwargs)).card
    low = resolve(cards, _spot(cards=("8h", "8d"), **kwargs)).card
    weak_ace = resolve(cards, _spot(cards=("Ah", "9h"), **kwargs)).card
    junk = resolve(cards, _spot(cards=("7h", "2d"), **kwargs)).card
    pair = resolve(cards, _spot(cards=("5h", "5d"), **kwargs)).card
    assert big.id == "preflop.limped.big_pair" and big.plan.action == "raise"
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
    card = next(c for c in cards if c.id == "preflop.rfi.big_pair")
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
    card = next(c for c in cards if c.id == "preflop.rfi.big_pair")
    sit = _sit(cards=("Ah", "Ad"), position="UTG", ante=None)
    assert apply_offsets(card, sit) == ()


def test_three_bet_remains_unknown():
    cards = load_cards(CARDS)
    got = resolve(cards, _spot(node="three_bet", pot_type="3-bet+ pot",
                               facing="raise", to_call=23.0, pot=37.0,
                               cards=("Ah", "Ad")))
    assert got.card is None


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
    assert "p111" in text
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
