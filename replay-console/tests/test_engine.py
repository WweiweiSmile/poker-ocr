from __future__ import annotations

from poker_ocr.advice import build_situation
from poker_ocr.hand_track import HandSnapshot, OpponentSnapshot, StreetRecord
from poker_ocr.parse import Blinds

from replay.engine import (
    audit, classify, decision_to_public, reads_from_context,
    summarize_version, table_stats_from_context,
)

BLINDS = Blinds(sb=0.5, bb=1.0, ante=None, raw="1/2")


def snapshot(**overrides):
    """默认是 990036 D3：AdQd 在 9d6d5s 面对 cover 我的全下。"""
    base = dict(
        hand_id="h1", hand_index=1, street="flop",
        board=("9d", "6d", "5s"), hero_cards=("Ad", "Qd"),
        hero_position="LJ", hero_stack=168.0, hero_bet=19.5,
        hero_invested=27.0, to_call=471.5, second_bet=None, pot=569.0,
        pot_before_street=97.5, table_size=8, blinds=BLINDS,
        opponents=(
            OpponentSnapshot("v", "八六上青山", "UTG+1", 498.0, 491.0, False),
            OpponentSnapshot("bb", "jackhshdn", "BB", 168.0, 19.5, False),
        ),
        streets=(StreetRecord("preflop", 0.0, ()),
                 StreetRecord("flop", 97.5, ())),
        preflop_shapes=("single-raise",), hero_is_pfr=True, cbet_spot=False,
        decision_key=("h1", "flop", 2, 569.0, 471.5, ("Ad", "Qd")),
        players_dealt=8,
    )
    base.update(overrides)
    return HandSnapshot(**base)


def test_audit_covered_all_in_fold_uses_winnable_pot():
    """990036 D3：超额 303.5 退回后门槛是 39%，36% 的听牌弃牌不该报违规。

    旧口径拿牌桌底池 569 当分母，门槛只有 23%，会把正确弃牌标成
    「有赔率却弃牌」；赔率分母必须和事实层一样用可争夺底池 433.5。
    """
    sit = build_situation(snapshot())
    assert sit.hand_tier == "强听" and sit.equity_pct == 36
    assert audit(sit, "fold", None) == set()


def test_audit_still_flags_fold_when_price_is_actually_bad():
    """同样的听牌，赔率真的好（门槛 25%）时仍要报违规。"""
    sit = build_situation(snapshot(
        hero_stack=100.0, hero_bet=0.0, hero_invested=3.0,
        to_call=10.0, pot=30.0, pot_before_street=30.0,
        opponents=(OpponentSnapshot("a", "v", "BB", 100.0, 10.0, False),),
        decision_key=("h1", "flop", 0, 30.0, 10.0, ("Ad", "Qd")),
    ))
    assert sit.hand_tier == "强听" and sit.equity_pct == 36
    assert audit(sit, "fold", None) == {"有赔率却弃牌"}


def result(*, option_id, kind, amount=None, source="model", repeat=0, reason="r"):
    return {
        "id": repeat + 1,
        "version_id": 3,
        "repeat_index": repeat,
        "option_id": option_id,
        "option_kind": kind,
        "option_amount": amount,
        "option_label": option_id,
        "reason": reason,
        "source": source,
        "model": "deepseek-flash",
        "duration_ms": 100,
        "violation_tags": "[]",
        "facts_json": '{"handTier": "顶对"}',
        "error_msg": None,
    }


def test_classify_preflop_tolerance_and_actions():
    assert classify("raise", 11.0, "raise", 10.5, 8.0, "preflop") == "一致"
    assert classify("raise", 11.0, "raise", 7.5, 8.0, "preflop") == "尺度差"
    assert classify("call", None, "raise", 7.5, 8.0, "preflop") == "动作不同"


def test_summary_uses_option_kind_for_open_ids():
    summary = summarize_version(
        [result(option_id="open-4", kind="raise", amount=11.0)],
        actual_action="raise", actual_amount=7.5, pot=8.0, street="preflop")
    assert summary["optionKind"] == "raise"
    assert summary["bucket"] == "尺度差"
    assert summary["label"] == "raise 11"


def test_summary_distribution_and_vote():
    results = [
        result(option_id="call", kind="call", repeat=0),
        result(option_id="raise-2/3", kind="raise", amount=10.0, repeat=1),
        result(option_id="raise-2/3", kind="raise", amount=10.0, repeat=2),
    ]
    summary = summarize_version(results, actual_action="call", actual_amount=None,
                                pot=15.0, street="flop")
    assert summary["optionId"] == "raise-2/3"
    assert summary["distribution"] == {"call": 1, "raise-2/3": 2}
    assert summary["allSame"] is False
    assert summary["bucket"] == "动作不同"


def test_fallback_is_excluded_from_consistency_denominator():
    summary = summarize_version(
        [result(option_id="fold", kind="fold", source="fallback"),
         result(option_id="fold", kind="fold", repeat=1, source="fallback")],
        actual_action="call", actual_amount=None, pot=10.0, street="flop")
    assert summary["n"] == 0
    assert summary["total"] == 2
    assert summary["source"] == "fallback"
    assert summary["bucket"] == "error"
    assert summary["allSame"] is False


def test_decision_to_public_splits_cards_and_keeps_actual():
    row = {
        "decision_id": 9, "hand_id": 1001, "street": "flop", "seq": 1,
        "pot": 15.5, "to_call": 4.0, "hero_stack": 92.0,
        "hero_position": "BTN", "hero_cards": "KhQh", "board": "Ks7d2c",
        "players": 6, "opponents": 2, "quality": "exact", "warnings": "[]",
        "actual_action": "call", "actual_amount": 4.0,
    }
    block = decision_to_public(row, [{"id": 3, "name": "v1", "model": "m"}], [])
    assert block["decision"]["heroCards"] == ["Kh", "Qh"]
    assert block["decision"]["board"] == ["Ks", "7d", "2c"]
    assert block["actual"]["label"] == "call 4"
    assert block["versions"][0]["summary"]["source"] == "none"


def test_reads_from_context_roundtrip():
    context = {
        "opponent_reads": [{
            "seat_id": "btn", "key": "芒果与西米", "display_name": "芒果与西米",
            "archetype": "lag", "confidence": 0.82, "n": 96,
            "vpip": 55.0, "pfr": 25.0, "af": 3.0,
            "source": "auto", "gate": "stable",
        }],
    }
    reads = reads_from_context(context)
    assert len(reads) == 1
    assert reads[0].seat_id == "btn"
    assert reads[0].archetype == "lag"
    assert reads[0].n == 96


def test_table_stats_from_context_roundtrip():
    context = {
        "table_stats": {
            "scope": "current_table", "hands": 312, "players": 4,
            "total_players": 6, "approx_hands": True,
            "vpip": 38.0, "pfr": 12.0, "threebet": 4.0,
            "opportunities": {"threebet": 80.0},
        },
    }
    stats = table_stats_from_context(context)
    assert stats is not None
    assert stats.scope == "current_table"
    assert stats.total_players == 6
    assert stats.players == 4
    assert stats.hands == 312
    assert stats.opportunities["threebet"] == 80.0
