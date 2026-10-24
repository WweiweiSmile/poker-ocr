from __future__ import annotations

import copy

from replay.decisions import (
    APPROX, EXACT, INVALID, analyze_hand, call_amount, hand_fingerprint,
    snapshot_from_json, snapshot_to_json,
)


def hand(**overrides):
    data = {
        "id": 1001,
        "title": "BTN KQs",
        "tableSize": 6,
        "heroPosition": "BTN",
        "heroCards": "KhQh",
        "heroStackBb": 100.0,
        "smallBlindBb": 0.5,
        "bigBlindBb": 1.0,
        "anteBb": 0.0,
        "bombPotBb": 0.0,
        "board": "Ks7d2c3h9s",
        "villainCount": 2,
        "villains": [
            {"position": "CO", "stackBb": 100.0},
            {"position": "BB", "stackBb": 80.0},
        ],
        "streets": [
            {"street": "preflop", "actions": [
                {"actor": "CO", "action": "raise", "amountBb": 2.5},
                {"actor": "hero", "action": "call"},
                {"actor": "BB", "action": "call"},
            ]},
            {"street": "flop", "actions": [
                {"actor": "BB", "action": "check"},
                {"actor": "CO", "action": "bet", "amountBb": 4.0},
                {"actor": "hero", "action": "call"},
            ]},
            {"street": "turn", "actions": [
                {"actor": "BB", "action": "check"},
                {"actor": "CO", "action": "check"},
                {"actor": "hero", "action": "check"},
            ]},
            {"street": "river", "actions": [
                {"actor": "BB", "action": "check"},
                {"actor": "CO", "action": "check"},
                {"actor": "hero", "action": "check"},
            ]},
        ],
        "result": "win",
        "resultAmount": 12.5,
    }
    data.update(overrides)
    return data


def test_preflop_start_without_pot_start_is_exact():
    analysis = analyze_hand(hand())
    assert analysis.quality == EXACT
    assert analysis.decision_count == 4
    assert [d.street for d in analysis.decisions] == ["preflop", "flop", "turn", "river"]
    assert analysis.decisions[0].actual == "call"
    assert analysis.decisions[1].snap.pot == 12.0


def test_bb_zero_is_invalid():
    analysis = analyze_hand(hand(bigBlindBb=0.0))
    assert analysis.quality == INVALID
    assert "bb" in (analysis.invalid_reason or "")


def test_start_postflop_without_pot_start_is_approx():
    data = hand(streets=hand()["streets"][1:])
    analysis = analyze_hand(data)
    assert analysis.quality == APPROX
    assert any("从 flop 开始" in w for w in analysis.warnings)


def test_old_aggregate_actor_is_approx():
    streets = copy.deepcopy(hand()["streets"])
    streets[0]["actions"][0]["actor"] = "villain"
    analysis = analyze_hand(hand(streets=streets))
    assert analysis.quality == APPROX
    assert any("聚合口径" in w for w in analysis.warnings)


def test_missing_hero_action_before_later_decision_is_approx():
    streets = copy.deepcopy(hand()["streets"])
    # 翻牌只剩对手行动，但转牌/河牌仍有 hero 决策 → 行动线缺口
    streets[1]["actions"] = [
        {"actor": "BB", "action": "check"},
        {"actor": "CO", "action": "bet", "amountBb": 4.0},
    ]
    analysis = analyze_hand(hand(streets=streets))
    assert analysis.quality == APPROX
    assert any("flop 缺 hero 动作" in w for w in analysis.warnings)


def test_fingerprint_ignores_title_result_and_note():
    base = hand()
    changed = hand(title="别的标题", result="lose", resultAmount=-99.0)
    assert hand_fingerprint(base) == hand_fingerprint(changed)
    changed_board = hand(board="Ks7d2c3h8s")
    assert hand_fingerprint(base) != hand_fingerprint(changed_board)


def test_snapshot_json_roundtrip():
    analysis = analyze_hand(hand())
    snap = analysis.decisions[1].snap
    restored = snapshot_from_json(snapshot_to_json(snap))
    assert restored == snap
    assert restored.hero_cards == ("Kh", "Qh")
    assert restored.opponents == snap.opponents


def _bomb_streets(pot_start: float = 30.0) -> list[dict]:
    return [
        {"street": "preflop", "actions": []},
        {"street": "flop", "potStartBb": pot_start, "actions": [
            {"actor": "BB", "action": "check"},
            {"actor": "hero", "action": "check"},
        ]},
    ]


def test_bomb_pot_without_preflop_actions():
    """炸弹池：翻前没有任何动作，翻牌直接发；前注 5BB/人。"""
    analysis = analyze_hand(hand(anteBb=5.0, streets=_bomb_streets()))
    assert analysis.bomb is True
    assert analysis.decisions[0].snap.bomb_pot is True
    assert analysis.decisions[0].snap.pot == 30.0


def test_bomb_pot_detected_from_pot_start_without_flag():
    """没有标记、前注也没读到 5：翻牌底池正好 5BB × 人数也能认出来。"""
    analysis = analyze_hand(hand(anteBb=0.5, streets=_bomb_streets()))
    assert analysis.bomb is True


def test_bomb_pot_flag_wins_even_if_actions_exist():
    analysis = analyze_hand(hand(bombPotBb=1.0))
    assert analysis.bomb is True


def test_normal_hand_is_not_bomb_pot():
    analysis = analyze_hand(hand())
    assert analysis.bomb is False
    assert analysis.decisions[0].snap.bomb_pot is False


def test_invalid_hand_still_marks_bomb_pot():
    """不能重跑的手牌也要显示爆炸底池标记：翻前无动作本身就是信息。"""
    analysis = analyze_hand(hand(heroCards="", bombPotBb=1.0))
    assert analysis.quality == INVALID
    assert analysis.bomb is True


def test_call_amount_caps_to_hero_stack():
    """对手 all-in cover 我时，「需跟」是后手，不是对手的本街最高下注。"""
    assert call_amount(471.5, 168.0) == 168.0
    assert call_amount(4.0, 100.0) == 4.0
    assert call_amount(4.0, None) == 4.0


def test_preflop_pot_start_adds_missing_blinds():
    """实时日志的翻前 pbs 只记前注：6 人 × 0.5 = 3.0，要补上 1.5 盲注。

    h39 的原记录是 4.5（9 人 × 0.5），同一帧的实时建议用的却是真实底池 6.0；
    不补盲注，翻前所有决策点的底池、赔率和客户端档位都会偏小。
    """
    data = hand(anteBb=0.5, heroPosition="UTG", streets=[
        {"street": "preflop", "potStartBb": 3.0, "actions": [
            {"actor": "hero", "action": "raise", "amountBb": 2.5},
            {"actor": "CO", "action": "call"},
        ]},
    ])
    analysis = analyze_hand(data)
    snap = analysis.decisions[0].snap
    assert snap.pot == 4.5, snap.pot
    assert snap.pot_before_street == 4.5, snap.pot_before_street


def test_preflop_pot_start_with_blinds_is_not_double_counted():
    """已经含盲注的 potStart 不能再加一次。"""
    data = hand(anteBb=0.5, heroPosition="UTG", streets=[
        {"street": "preflop", "potStartBb": 4.5, "actions": [
            {"actor": "hero", "action": "raise", "amountBb": 2.5},
            {"actor": "CO", "action": "call"},
        ]},
    ])
    assert analyze_hand(data).decisions[0].snap.pot == 4.5
