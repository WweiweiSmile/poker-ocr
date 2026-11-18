"""版本对照纯逻辑（#60）：配对、翻转方向、跨版本告警。

这些用例只喂「已归一化的条目/运行快照」，不碰数据库：口径一旦写错，
服务层再怎么修都会算错。
"""
from __future__ import annotations

from replay import compare


def _item(case_id, canonical_id, decision_key, *, score=50.0, hard_fail=None,
          kind="raise", source="model", spec_id=11, spec_name="flop cbet"):
    return {
        "caseId": case_id,
        "canonicalId": canonical_id,
        "decisionKey": decision_key,
        "specId": spec_id,
        "specName": spec_name,
        "specRevision": 1,
        "score": score,
        "scores": {"action": 1.0},
        "hardFail": hard_fail,
        "source": source,
        "comparable": source != "error",
        "overridden": False,
        "street": "flop",
        "actionIndex": 2,
        "priority": 1,
        "p0": False,
        "bucket": {"street": ["flop"], "position_bucket": ["IP"]},
        "bucketLabel": "翻牌·IP",
        "hand": {"heroCards": "AhAd", "board": "Kd7c2s",
                 "heroPosition": "BTN"},
        "advice": {"optionId": kind, "kind": kind, "amount": 6.0,
                   "reason": "有位置优势"},
    }


def _side(score, *, hard_fail=None, source="model"):
    return {"score": score, "hardFail": hard_fail,
            "comparable": source != "error"}


# ── 配对 ──

def test_pairs_by_decision_identity_not_case_id():
    """案例集升 revision 会整集复制、case_id 全变；身份是决策点本身。"""
    left = [_item(101, 7, "flop:2"), _item(102, 7, "turn:1")]
    right = [_item(201, 7, "flop:2"), _item(202, 7, "turn:1")]
    pairs, counts = compare.pair(left, right)
    assert [pair[0]["decisionKey"] for pair in pairs] == ["flop:2", "turn:1"]
    assert [pair[1]["caseId"] for pair in pairs] == [201, 202]
    assert counts == {"left": 2, "right": 2, "common": 2,
                      "excludedLeft": 0, "excludedRight": 0}


def test_cases_outside_intersection_are_counted_not_compared():
    left = [_item(101, 7, "flop:2"), _item(102, 7, "turn:1"),
            _item(103, 8, "flop:1")]
    right = [_item(201, 7, "flop:2"), _item(202, 8, "flop:1"),
             _item(203, 9, "river:1")]
    pairs, counts = compare.pair(left, right)
    assert [(pair[0]["canonicalId"], pair[0]["decisionKey"])
            for pair in pairs] == [(7, "flop:2"), (8, "flop:1")]
    assert counts["common"] == 2
    assert counts["excludedLeft"] == 1
    assert counts["excludedRight"] == 1


def test_item_without_canonical_falls_back_to_case_id():
    left = [_item(101, None, None)]
    right = [_item(101, None, None), _item(202, None, None)]
    pairs, counts = compare.pair(left, right)
    assert counts["common"] == 1 and counts["excludedRight"] == 1


# ── 翻转方向 ──

def test_score_rise_is_improved():
    assert compare.flips(_side(50.0), _side(58.0)) == "improved"


def test_score_drop_is_worse():
    assert compare.flips(_side(58.0), _side(50.0)) == "worse"


def test_change_within_epsilon_is_unchanged():
    assert compare.flips(_side(50.0), _side(50.004)) == "unchanged"
    assert compare.flips(_side(50.0), _side(49.996)) == "unchanged"


def test_hard_fail_appearing_is_worse_even_if_score_rises():
    """硬失败封顶 40，但 38→40 的「涨分」其实是多了条 HF_OPTION。"""
    assert compare.flips(_side(38.0),
                         _side(40.0, hard_fail="HF_OPTION")) == "worse"


def test_hard_fail_clearing_is_improved_even_if_score_barely_moves():
    assert compare.flips(_side(40.0, hard_fail="HF_OPTION"),
                         _side(42.0)) == "improved"


def test_hard_fail_code_change_compares_scores():
    assert compare.flips(_side(40.0, hard_fail="HF_OPTION"),
                         _side(10.0, hard_fail="HF_HALLUCINATION")) == "worse"


def test_error_side_is_incomparable_not_unchanged():
    assert compare.flips(_side(50.0, source="error"),
                         _side(50.0)) == "incomparable"


def test_missing_score_is_incomparable():
    assert compare.flips(_side(None), _side(50.0)) == "incomparable"


# ── 行与汇总 ──

def test_row_carries_both_sides_and_delta():
    row = compare.compare_row(_item(101, 7, "flop:2", score=60.0),
                              _item(201, 7, "flop:2", score=45.0))
    assert row["direction"] == "worse"
    assert row["delta"] == -15.0
    assert row["left"]["caseId"] == 101
    assert row["right"]["caseId"] == 201
    assert row["left"]["advice"]["kind"] == "raise"
    assert row["bucketLabel"] == "翻牌·IP"
    assert row["canonicalId"] == 7 and row["decisionKey"] == "flop:2"


def test_incomparable_row_has_no_delta():
    row = compare.compare_row(_item(101, 7, "flop:2", source="error"),
                              _item(201, 7, "flop:2", score=45.0))
    assert row["direction"] == "incomparable"
    assert row["delta"] is None


def test_summary_counts_directions_and_means_common_only():
    rows = [
        compare.compare_row(_item(1, 7, "flop:2", score=80.0),
                            _item(11, 7, "flop:2", score=60.0)),
        compare.compare_row(_item(2, 7, "flop:2", score=40.0),
                            _item(12, 7, "flop:2", score=40.0)),
        compare.compare_row(_item(3, 7, "flop:2", source="error"),
                            _item(13, 7, "flop:2", score=70.0)),
    ]
    out = compare.summarize(rows)
    assert out["n"] == 3 and out["scored"] == 2
    assert out["directions"] == {"worse": 1, "improved": 0,
                                 "unchanged": 1, "incomparable": 1}
    # 不可比那行两侧都不进均分：80/40 与 60/40
    assert out["left"]["mean"] == 60.0
    assert out["right"]["mean"] == 50.0
    assert out["delta"] == -10.0


# ── 跨版本告警 ──

def _run(run_id, *, set_id=4, revision=1, specs=None, judge="rules-v1",
         reconstruct="canonical-v1", status="succeeded"):
    return {
        "id": run_id,
        "status": status,
        "caseSet": {"id": set_id, "name": "seed", "revision": revision},
        "version": {"id": run_id, "name": "v", "model": "m"},
        "judgeVersion": judge,
        "reconstructVersion": reconstruct,
        "config": {"specs": specs or []},
    }


def test_version_diff_flags_spec_and_case_set_changes():
    left = _run(1, revision=1,
                specs=[{"id": 11, "name": "flop cbet", "revision": 1}])
    right = _run(2, revision=2,
                 specs=[{"id": 11, "name": "flop cbet", "revision": 3},
                        {"id": 12, "name": "river thin", "revision": 1}])
    diff = compare.version_diff(left, right)
    assert diff["caseSet"]["same"] is False
    assert diff["specs"]["same"] is False
    assert diff["specs"]["changed"] == [
        {"id": 11, "name": "flop cbet", "left": 1, "right": 3}]
    assert [spec["id"] for spec in diff["specs"]["onlyRight"]] == [12]
    assert diff["crossVersion"] is True
    codes = [warning["code"] for warning in compare.warnings(diff, left, right)]
    assert codes == ["CASE_SET_CHANGED", "SPEC_CHANGED"]


def test_version_diff_ignores_test_version_change():
    """被测版本变了正是对照的目的，不算口径变化。"""
    left = _run(1, specs=[{"id": 11, "name": "s", "revision": 1}])
    right = _run(2, specs=[{"id": 11, "name": "s", "revision": 1}])
    diff = compare.version_diff(left, right)
    assert diff["version"]["same"] is False
    assert diff["crossVersion"] is False
    assert compare.warnings(diff, left, right) == []


def test_judge_and_reconstruct_changes_are_flagged():
    left = _run(1)
    right = _run(2, judge="llm-j1:deepseek-v4-pro", reconstruct="canonical-v2")
    codes = [warning["code"]
             for warning in compare.warnings(compare.version_diff(left, right),
                                             left, right)]
    assert codes == ["JUDGE_CHANGED", "RECONSTRUCT_CHANGED"]


def test_unfinished_run_is_flagged():
    left = _run(1)
    right = _run(2, status="partial")
    warnings = compare.warnings(compare.version_diff(left, right), left, right)
    assert [warning["code"] for warning in warnings] == ["RUN_NOT_FINISHED"]
    assert "2" in warnings[0]["message"]


def test_compare_row_without_bucket_falls_back_to_left_labels():
    left = _item(101, 7, "flop:2")
    left["bucket"], left["bucketLabel"] = None, ""
    right = _item(201, 7, "flop:2")
    right["bucket"] = {"street": ["turn"]}
    row = compare.compare_row(left, right)
    assert row["bucket"] is None
    assert row["direction"] == "unchanged"
