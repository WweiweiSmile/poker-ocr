from __future__ import annotations

import copy
import json

from replay.decisions import analyze_hand
from replay.importer import (
    SOURCE_LIVE, SOURCE_REVIEW, normalize_review_hand, parse_hand_text,
)


def review_hand(**overrides):
    data = {
        "id": 990001,
        "title": "BTN KQs",
        "heroCards": "KhQh",
        "board": "Ks7d2c3h9s",
        "heroPosition": "BTN",
        "tableSize": 6,
        "heroStackBb": 100.0,
        "smallBlindBb": 0.5,
        "bigBlindBb": 1.0,
        "anteBb": 0.0,
        "bombPotBb": 0.0,
        "villainCount": 2,
        "villains": [
            {"position": "CO", "stackBb": 100.0},
            {"position": "BB", "stackBb": 80.0},
        ],
        "streets": [
            {"street": "preflop", "actions": [
                {"actor": "CO", "action": "raise", "amountBb": 2.5},
                {"actor": "hero", "action": "call", "amountBb": 2.5},
                {"actor": "BB", "action": "call", "amountBb": 2.5},
            ]},
            {"street": "flop", "actions": [
                {"actor": "BB", "action": "check"},
                {"actor": "CO", "action": "bet", "amountBb": 4.0},
                {"actor": "hero", "action": "call", "amountBb": 4.0},
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
        "created_at": "2026-10-23T03:06:53+08:00",
    }
    data.update(overrides)
    return data


def live_hand(**overrides):
    data = {
        "type": "hand",
        "ts": "2026-10-23T03:08:07+08:00",
        "hand_id": "h4",
        "uid": "s01-h4",
        "hero_cards": ["Ah", "Js"],
        "hero_position": "UTG+1",
        "table_size": 9,
        "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.5, "raw": "1/2（1）"},
        "board": ["7c", "7h", "10d", "8c", "6h"],
        "pot": 66.5,
        "streets": [
            {"street": "preflop", "pot_start": 4.0, "actions": [
                {"seat_id": "hero", "name": "hero", "position": "UTG+1",
                 "action": "raise", "amount": 7.5, "all_in": False,
                 "is_hero": True},
                {"seat_id": "topR", "name": "陈7程7", "position": "BTN",
                 "action": "call", "amount": 7.5, "all_in": False,
                 "is_hero": False},
            ]},
            {"street": "flop", "pot_start": 20.5, "actions": [
                {"seat_id": "hero", "name": "hero", "position": "UTG+1",
                 "action": "bet", "amount": 6.5, "all_in": False,
                 "is_hero": True},
                {"seat_id": "topR", "name": "陈7程7", "position": "BTN",
                 "action": "call", "amount": 6.5, "all_in": False,
                 "is_hero": False, "inferred": True},
            ]},
        ],
    }
    data.update(overrides)
    return data


def test_review_json_array_is_normalized_and_replayable():
    response = parse_hand_text(json.dumps([review_hand()]))
    assert response.source == SOURCE_REVIEW
    assert response.container == "json"
    assert len(response.records) == 1
    row = response.records[0].row
    assert row["hero_cards"] == "KhQh"
    assert row["board"] == "Ks7d2c3h9s"
    assert row["external_id"] == "990001"
    assert row["streets"][0]["actions"][0] == {
        "actor": "CO", "action": "raise", "amountBb": 2.5,
    }
    analysis = analyze_hand(row)
    assert analysis.quality == "exact"
    assert analysis.decision_count == 4


def test_review_snake_case_jsonl_and_double_encoded_streets():
    hand = {
        "id": 990002,
        "hero_cards": "AhKd",
        "hero_position": "BB",
        "table_size": 6,
        "hero_stack_bb": 50,
        "small_blind_bb": 0.5,
        "big_blind_bb": 1.0,
        "ante_bb": 0,
        "streets": json.dumps([{
            "street": "preflop",
            "potStartBb": 1.5,
            "actions": [
                {"actor": "CO", "action": "raise", "amountBb": 3},
                {"actor": "hero", "action": "call", "amountBb": 3},
            ],
        }]),
    }
    response = parse_hand_text(json.dumps(hand) + "\n")
    assert response.source == SOURCE_REVIEW
    assert len(response.records) == 1
    row = response.records[0].row
    assert row["hero_cards"] == "AhKd"
    assert row["streets"][0]["actions"][1]["action"] == "call"
    assert analyze_hand(row).decision_count == 1


def test_live_hands_json_is_detected_and_normalized():
    response = parse_hand_text(json.dumps([live_hand()]))
    assert response.source == SOURCE_LIVE
    assert len(response.records) == 1
    row = response.records[0].row
    assert row["external_id"] == "s01-h4"
    assert row["hero_cards"] == "AhJs"
    assert row["board"] == "7c7hTd8c6h"
    assert row["small_blind_bb"] == 0.5
    assert row["streets"][0]["potStartBb"] == 4.0
    assert {v["position"] for v in row["villains"]} == {"BTN"}
    analysis = analyze_hand(row)
    assert analysis.quality == "approx"
    assert analysis.decision_count == 2


def test_live_hand_start_stacks_feed_replay_constraints():
    """实时手牌记录带了起始筹码时，重建出的决策点要能做筹码约束。"""
    hand = live_hand(
        hero_stack_bb=100.0,
        start_stacks=[
            {"seat_id": "hero", "name": "hero", "position": "UTG+1",
             "stack": 100.0},
            {"seat_id": "topR", "name": "陈7程7", "position": "BTN",
             "stack": 80.0},
            # 行动线里没有这个位置：不能凭空把他加进「在局对手」，
            # 否则一个没被标记弃牌的短码会把有效筹码压小。
            {"seat_id": "lowL", "name": "老王", "position": "CO",
             "stack": 120.0},
        ],
    )
    row = parse_hand_text(json.dumps([hand])).records[0].row
    assert row["hero_stack_bb"] == 100.0
    stacks = {v["position"]: v["stackBb"] for v in row["villains"]}
    assert stacks["BTN"] == 80.0
    assert "CO" not in stacks
    # hero 自己不能被当成对手重复算一次
    assert "UTG+1" not in stacks

    analysis = analyze_hand(row)
    assert not any("缺 hero 起始筹码" in w for w in analysis.warnings)
    first = analysis.decisions[0].snap
    assert first.hero_stack is not None
    assert first.hero_invested is not None


def test_raw_live_log_skips_state_partial_and_mid_hand():
    session_start = {"type": "session_start", "started_at": "2026-10-23T03:00:00+08:00"}
    state = {"type": "state", "hero": {"stack": 100}}
    partial = live_hand(hand_id="h2", uid=None, partial=True)
    mid = live_hand(
        hand_id="h3", uid=None,
        streets=[{"street": "flop", "pot_start": 9.0, "actions": [
            {"seat_id": "hero", "position": "BB", "action": "bet",
             "amount": 3, "is_hero": True},
            {"seat_id": "topR", "position": "BTN", "action": "call",
             "amount": 3, "is_hero": False},
        ]}],
    )
    text = "\n".join(json.dumps(item) for item in
                     [session_start, state, live_hand(uid=None), partial, mid])
    response = parse_hand_text(text)
    assert response.source == SOURCE_LIVE
    assert response.container == "jsonl"
    assert response.session_count == 1
    assert response.skipped_non_hand == 1
    assert response.skipped_partial == 1
    assert response.skipped_mid_hand == 1
    assert len(response.records) == 1
    row = response.records[0].row
    assert row["external_id"] == "s01-h4"

    included = parse_hand_text(text, include_partial=True, include_mid_hand=True)
    assert len(included.records) == 3


def test_invalid_hand_is_parsed_but_analysis_is_invalid():
    hand = review_hand(heroCards="")
    response = parse_hand_text(json.dumps(hand))
    assert len(response.records) == 1
    analysis = analyze_hand(response.records[0].row)
    assert analysis.quality == "invalid"
    assert "底牌" in (analysis.invalid_reason or "")


def test_forced_source_and_bad_json_error():
    text = json.dumps([review_hand()])
    response = parse_hand_text(text, source=SOURCE_REVIEW)
    assert response.source == SOURCE_REVIEW

    try:
        parse_hand_text("")
    except Exception as exc:  # ApiError
        assert getattr(exc, "code", None) == "IMPORT_EMPTY"
    else:  # pragma: no cover
        raise AssertionError("空内容应该报错")


def test_ten_rank_is_normalized_to_t():
    response = parse_hand_text(json.dumps([live_hand(
        hero_cards=["10h", "As"],
        board=["10c", "7d", "2s", "10d", "Kh"],
    )]))
    row = response.records[0].row
    assert row["hero_cards"] == "ThAs"
    assert row["board"] == "Tc7d2sTdKh"
    assert len(row["board"]) == 10


def test_normalize_review_hand_does_not_mutate_input():
    hand = review_hand()
    snapshot = copy.deepcopy(hand)
    normalize_review_hand(hand)
    assert hand == snapshot
