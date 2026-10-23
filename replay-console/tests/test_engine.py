from __future__ import annotations

from replay.engine import classify, decision_to_public, summarize_version


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
