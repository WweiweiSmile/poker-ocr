from __future__ import annotations

import copy

from replay.decisions import (
    APPROX, EXACT, INVALID, analyze_hand, hand_fingerprint, snapshot_from_json,
    snapshot_to_json,
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
