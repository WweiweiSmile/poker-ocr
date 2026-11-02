"""ReplayTimeline / flags：帧顺序、决策插入、推算标记与深挖位图。"""

from __future__ import annotations

from poker_ocr.advice import build_situation
from replay.decisions import analyze_hand
from replay.flags import FLAG_BITS, compute_flags, parse_flags, mask_for
from replay.importer import normalize_live_hand
from replay.timeline import build_timeline


def live_rec():
    return {
        "schema": "hand.v2", "uid": "20261101-bfdd2c4dd3",
        "hand_id": "20261101-bfdd2c4dd3",
        "hero_cards": ["Ks", "Td"], "hero_position": "LJ",
        "table_size": 9, "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.5},
        "board": ["3d", "Ac", "2c", "Kh", "9s"],
        "table_roster": [
            {"seat_id": "hero", "name": "me", "position": "LJ",
             "status": "playing", "in_hand": True},
            {"seat_id": "lowL", "name": "A", "position": "UTG",
             "status": "playing", "in_hand": True, "stack": 230.0},
            {"seat_id": "midL", "name": "B", "position": "UTG+1",
             "status": "playing", "in_hand": True, "stack": 209.0},
            {"seat_id": "upR", "name": "C", "position": "CO",
             "status": "playing", "in_hand": True, "stack": 237.0},
        ],
        "start_stacks": [{"seat_id": "hero", "stack": 182.0}],
        "streets": [
            {"street": "preflop", "pot_start": 4.5, "actions": [
                {"seat_id": "lowL", "position": "UTG", "action": "fold"},
                {"seat_id": "midL", "position": "UTG+1", "action": "call",
                 "amount": 1.0},
                {"seat_id": "upR", "position": "CO", "action": "raise",
                 "amount": 3.5},
                {"seat_id": "hero", "position": "LJ", "action": "call",
                 "amount": 3.5, "is_hero": True},
            ]},
            {"street": "flop", "pot_start": 12.5, "actions": [
                {"seat_id": "upR", "position": "CO", "action": "bet",
                 "amount": 6.0},
                {"seat_id": "hero", "position": "LJ", "action": "call",
                 "amount": 6.0, "is_hero": True},
            ]},
        ],
        "result": {"hero_net_bb": -12.5, "source": "stack_delta"},
    }


def test_timeline_orders_decisions_by_global_action_index():
    row = normalize_live_hand(live_rec())
    analysis = analyze_hand({**row, "id": 0})
    # 决策点 action_index 是整手牌的动作总数，不是本街序号。
    assert [(d.street, d.action_index) for d in analysis.decisions] == [
        ("preflop", 3), ("flop", 5),
    ]
    timeline = build_timeline(row, analysis)
    frames = timeline["frames"]
    assert [f["type"] for f in frames[:5]] == [
        "hand_start", "street_start", "action", "action", "action",
    ]
    assert frames[5]["type"] == "decision"
    assert frames[5]["street"] == "preflop"
    assert frames[5]["snapshot"]["toCall"] == 3.5
    # flop 的决策点插在 upR bet 之后、hero call 之前
    flop_decision = next(f for f in frames
                         if f["type"] == "decision" and f["street"] == "flop")
    assert flop_decision["snapshot"]["pot"] == 18.5
    assert frames[-1]["type"] == "hand_end"
    assert frames[-1]["result"]["heroNetBb"] == -12.5


def test_timeline_marks_missing_amount_as_derived():
    rec = live_rec()
    rec["streets"][1]["actions"][0]["amount"] = None
    row = normalize_live_hand(rec)
    analysis = analyze_hand({**row, "id": 0})
    timeline = build_timeline(row, analysis)
    bet = next(f for f in timeline["frames"]
               if f["type"] == "action" and f["street"] == "flop"
               and f["actor"]["seatId"] == "upR")
    assert bet["derived"] is True


def test_flags_ai_diff_and_prompt_missing():
    row = normalize_live_hand(live_rec())
    analysis = analyze_hand({**row, "id": 0})
    advices = [
        {"street": "preflop", "action_index": 3,
         "option": {"id": "fold", "kind": "fold"},
         "source": "model", "confidence": 0.3,
         "prompt_missing": True, "context": {}},
        {"street": "flop", "action_index": 5,
         "option": {"id": "call", "kind": "call", "amount_bb": 6},
         "source": "model", "prompt_sha256": "abc",
         "context": {"opponent_reads": []}},
    ]
    mask, details, score = compute_flags(row, analysis, advices)
    assert mask & FLAG_BITS["AI_DIFF"]
    assert mask & FLAG_BITS["PROMPT_MISSING"]
    assert mask & FLAG_BITS["LOW_CONF"]
    assert score > 0
    assert "AI_DIFF" in details
    assert parse_flags("AI_DIFF,PROMPT_MISSING") == [
        "AI_DIFF", "PROMPT_MISSING",
    ]
    assert mask_for(["AI_DIFF"]) == FLAG_BITS["AI_DIFF"]
