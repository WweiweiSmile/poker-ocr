from __future__ import annotations

import json

from replay.decisions import (
    APPROX, analyze_hand, hand_fingerprint, hand_to_input,
    missing_action_advices,
)

from tests.test_decisions import hand  # noqa: E402  （同一份夹具）


def advice(street: str, action_index: int, pot: float, to_call: float, **kw):
    return {
        "street": street, "action_index": action_index,
        "pot": pot, "to_call": to_call, "actual_action": None, **kw,
    }


def test_street_end_advice_backfills_missing_action():
    """河牌英雄动作缺录：街末的 advice 补出一个 backfilled 决策点。"""
    data = hand(advices=[advice("river", 12, 16.0, 0.0)])
    analysis = analyze_hand(data)

    assert analysis.decision_count == 5
    last = analysis.decisions[-1]
    assert last.street == "river"
    assert last.action_index == 12
    assert last.seq == 4
    assert last.backfilled is True
    assert last.actual is None
    assert last.actual_amount is None
    assert last.pot == 16.0
    assert last.snap.to_call == 0.0
    assert any("英雄动作缺录" in w for w in last.warnings)
    assert last.snap.warnings and any("英雄动作缺录" in w for w in last.snap.warnings)
    assert last.quality == APPROX
    assert analysis.quality == APPROX


def test_backfill_keeps_existing_decision_seqs():
    """已有决策点的 seq 是 advice / 案例引用的身份，回填不能改它们。"""
    base = analyze_hand(hand())
    filled = analyze_hand(hand(advices=[advice("river", 12, 16.0, 0.0)]))

    assert len(filled.decisions) == len(base.decisions) + 1
    for before, after in zip(base.decisions, filled.decisions):
        assert (before.street, before.seq, before.action_index) == (
            after.street, after.seq, after.action_index)
        assert after.backfilled is False


def test_backfill_drops_advice_with_mismatched_pot():
    """底池对不上行动线 = 误触发的建议，不能当决策点（298 翻前 7.5）。"""
    analysis = analyze_hand(hand(advices=[advice("river", 12, 99.0, 0.0)]))
    assert analysis.decision_count == 4
    assert not any(d.backfilled for d in analysis.decisions)


def test_bad_advice_values_fail_open():
    """advice 是外部 JSON：坏类型不能抛异常，也不能变成决策点。"""
    analysis = analyze_hand(hand(advices=[
        {"street": "river", "action_index": "x", "pot": 16.0, "to_call": 0.0},
        {"street": "river", "action_index": 12, "pot": "abc", "to_call": 0.0},
        {"street": "river", "action_index": 13, "pot": {"x": 1}, "to_call": 0.0},
        {"street": "river", "action_index": 14, "pot": 16.0, "to_call": [0]},
        {"street": ["river"], "action_index": 15, "pot": 16.0, "to_call": 0.0},
        "not-a-dict",
        None,
    ]))
    assert analysis.decision_count == 4
    assert not any(d.backfilled for d in analysis.decisions)


def test_mismatched_advice_is_reported_not_swallowed():
    """丢弃的缺口 advice 要留 warning，不能静默吞掉。"""
    analysis = analyze_hand(hand(advices=[advice("river", 12, 99.0, 0.0)]))
    assert any("已丢弃" in w and "river ai=12" in w for w in analysis.warnings)


def test_backfill_drops_points_before_last_hero_action():
    """最后一个英雄动作之前的 advice 已有真实动作可挂，补进来会改 seq。"""
    data = hand(advices=[advice("preflop", 2, 4.0, 2.5)])
    analysis = analyze_hand(data)
    assert analysis.decision_count == 4
    assert not any(d.backfilled for d in analysis.decisions)


def test_backfill_dedupes_same_street_and_index():
    data = hand(advices=[advice("river", 12, 16.0, 0.0),
                         advice("river", 12, 16.0, 0.0)])
    analysis = analyze_hand(data)
    assert sum(1 for d in analysis.decisions if d.backfilled) == 1


def test_mid_street_backfill_before_villain_action():
    """缺口在街中（后面还有对手动作）：边界处插入，不影响前面的决策点。"""
    data = hand(
        streets=[
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
                {"actor": "hero", "action": "check"},
                {"actor": "CO", "action": "bet", "amountBb": 8.0},
            ]},
        ],
        advices=[advice("river", 11, 16.0, 0.0)],
    )
    analysis = analyze_hand(data)

    assert analysis.decision_count == 5
    backfilled = analysis.decisions[-1]
    assert backfilled.backfilled is True
    assert backfilled.action_index == 11
    assert backfilled.pot == 16.0
    assert analysis.decisions[3].actual == "check"   # 河牌自己的过牌还在


def test_backfill_can_make_hand_replayable():
    """438：整手没有英雄动作，唯一决策点来自 advice。"""
    data = hand(
        heroPosition="BTN",
        streets=[
            {"street": "preflop", "actions": [
                {"actor": "CO", "action": "raise", "amountBb": 2.5},
                {"actor": "BB", "action": "call"},
            ]},
        ],
        advices=[advice("preflop", 2, 5.5, 2.5)],
    )
    analysis = analyze_hand(data)

    assert analysis.decision_count == 1
    only = analysis.decisions[0]
    assert only.backfilled is True
    assert only.actual is None
    assert only.snap.to_call == 2.5
    assert analysis.quality == APPROX


def test_missing_action_advices_skips_points_with_actual_action():
    data = hand(advices=[dict(advice("river", 12, 16.0, 0.0), actual_action="call")])
    assert missing_action_advices(data) == []


def test_advices_read_from_payload_json_row():
    """导入路径的 DB 行把 advice 快照放在 payload_json 里。"""
    row = {
        "id": 7, "table_size": 6, "hero_position": "BTN", "hero_cards": "KhQh",
        "hero_stack_bb": 100.0, "small_blind_bb": 0.5, "big_blind_bb": 1.0,
        "board": "Ks7d2c3h9s",
        "streets": json.dumps(hand()["streets"]),
        "villains": json.dumps(hand()["villains"]),
        "payload_json": json.dumps({"advices": [advice("river", 12, 16.0, 0.0)]}),
    }
    h = hand_to_input(row)
    assert h["advices"] and h["advices"][0]["action_index"] == 12
    # 身份（指纹）不因 advice 快照而变
    assert hand_fingerprint(h) == hand_fingerprint(hand_to_input({**row, "payload_json": "{}"}))


def test_fractional_action_index_is_not_truncated():
    """advice 的 action_index 是下标：12.9 不能悄悄变成 12（会挂错决策点）。"""
    analysis = analyze_hand(hand(advices=[advice("river", 12.9, 16.0, 0.0)]))
    assert analysis.decision_count == 4
    assert not any(d.backfilled for d in analysis.decisions)


def test_refresh_stale_continues_after_a_bad_hand():
    """一手坏数据不能让整批重算停摆（fail open，与回填主循环同一条）。"""
    from replay.backfill import refresh_stale_enrichment

    class DB:
        def query(self, sql, params=()):
            assert "INVALID" in params[0]
            assert "决定" not in params[1]       # 参数就是两个 SQL 模式
            assert params[1] == '%"decisionId":null%'
            return [{"id": 1}, {"id": 2}, {"id": 3}]

    class Service:
        def __init__(self):
            self.seen = []

        def refresh_enrichment(self, hand_id):
            if hand_id == 2:
                raise RuntimeError("boom")
            self.seen.append(hand_id)
            return hand_id != 3                  # 3 = 写库失败但不抛

    service = Service()
    out = refresh_stale_enrichment(DB(), service)
    assert service.seen == [1, 3]
    assert out["refreshed"] == 1             # 3 没写成功，不能算已重算
    assert [item["handId"] for item in out["failed"]] == [2, 3]

    dry = refresh_stale_enrichment(DB(), Service(), dry_run=True)
    assert dry["pending"] == [1, 2, 3]
    assert dry["refreshed"] == 0


def test_nan_advice_values_are_treated_as_missing():
    """字符串 "NaN" 能转成 float("nan")，比对时恒「不超容差」——必须当缺。"""
    data = hand(advices=[advice("river", 12, "NaN", "NaN")])
    analysis = analyze_hand(data)
    assert analysis.decision_count == 4
    assert not any(d.backfilled for d in analysis.decisions)


def test_refresh_enrichment_reads_advices_from_payload():
    """get_row 不取 payload_json，重算时必须自己把 advices 补进 row。

    少了这一步，回填出来的决策点会在重算时消失，flags 又变回「没有 hero 决策点」。
    """
    from replay.service import ReplayService

    service = object.__new__(ReplayService)
    snapshot = [advice("river", 12, 16.0, 0.0)]

    class HandRepo:
        def get_row(self, hand_id):
            return {"id": hand_id, "streets": "[]"}

    class ImportRepo:
        def get_payload(self, hand_id):
            return {"advices": snapshot}

    class DecisionRepo:
        def for_hand(self, hand_id, fingerprint=None):
            return {
                ("preflop", 1): {"id": 11, "action_index": 3},
                ("river", 2): {"id": 12, "action_index": 12},
                ("river", 3): {"id": None, "action_index": 13},
            }

    seen = {}

    class Analysis:
        fingerprint = "fp"

    def fake_analyze(row):
        seen["advices"] = row.get("advices")
        return Analysis()

    def fake_apply(hand_id, row, analysis, decision_ids=None):
        seen["applied"] = (hand_id, analysis, decision_ids)

    service.hand_repo = HandRepo()
    service.import_repo = ImportRepo()
    service.decision_repo = DecisionRepo()
    service.analyze = fake_analyze
    service._apply_enrichment = fake_apply

    service.refresh_enrichment(7)
    assert seen["advices"] == snapshot
    hand_id, analysis, decision_ids = seen["applied"]
    assert hand_id == 7
    assert analysis is seen["applied"][1]
    # 帧里的 decisionId 只能从这份映射来；重算不传会把时间线洗成 null。
    assert decision_ids == {("preflop", 3): 11, ("river", 12): 12}
