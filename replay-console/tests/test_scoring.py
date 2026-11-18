"""案例评分纯函数（#56）：权重、尺寸带、六个硬失败、稳定维与聚合。"""
from __future__ import annotations

import pytest

from replay import scoring


SPEC = {
    "allowed_actions": ["bet", "raise", "call"],
    "forbidden_actions": ["fold"],
    "forbidden_points": [{"key": "chase", "text": "追牌"}],
    "sizing": {"unit": "pot_fraction", "min": 0.5, "max": 1.0},
    "required_points": [],
}


def _advice(**over):
    base = {"option_id": "opt-1", "kind": "bet", "amount": 75.0,
            "reason": "这里应该下注拿价值", "source": "model"}
    base.update(over)
    return base


def test_weights_and_thresholds_are_exported_constants():
    assert scoring.W == {"action": 35, "size": 15, "reason": 30,
                         "fact": 15, "stable": 5}
    assert sum(scoring.W.values()) == 100
    assert scoring.PASS_SCORE == 60
    assert scoring.HF_CAP == 40
    assert scoring.FREQ_MIN_N == 15
    assert set(scoring.HARD_FAIL_CODES) == {
        "HF_ERROR", "HF_OPTION", "HF_FALLBACK", "HF_FORBIDDEN_ACTION",
        "HF_FORBIDDEN_POINT", "HF_HALLUCINATION",
    }


def test_pot_fraction_band_scales_with_distance():
    band = {"unit": "pot_fraction", "min": 0.5, "max": 1.0}
    assert scoring.size_score("bet", 75, band, pot=100) == 1.0
    assert scoring.size_score("bet", 30, band, pot=100) == pytest.approx(0.6)
    assert scoring.size_score("bet", 300, band, pot=100) == 0.0
    # 非下注类动作与 pot<=0 都不罚（fail open）
    assert scoring.size_score("call", 999, band, pot=100) == 1.0
    assert scoring.size_score("bet", 75, band, pot=0) == 1.0
    assert scoring.size_score("bet", 75, band, pot=None) == 1.0


def test_bb_band_uses_option_amount_directly():
    # option_amount 本身就是 BB（poker_ocr/options.py），不能再除一次大盲。
    band = {"unit": "bb", "min": 5, "max": 6}
    assert scoring.size_score("raise", 5.5, band) == 1.0
    assert scoring.size_score("raise", 30, band) == 0.0
    assert scoring.size_score("raise", 4.5, band) == pytest.approx(0.5)


def test_multiple_bands_take_the_minimum():
    both = [{"unit": "pot_fraction", "min": 0.5, "max": 1.0},
            {"unit": "bb", "min": 20, "max": 30}]
    single = [{"unit": "pot_fraction", "min": 0.5, "max": 1.0}]
    # 30BB 落在 bb 带内（1.0）；但占池 30/100=0.3 离 0.5 差 0.2 → 0.6。
    assert scoring.size_score("bet", 30, both, pot=100) == pytest.approx(0.6)
    assert (scoring.size_score("bet", 30, single, pot=100)
            == scoring.size_score("bet", 30, single[0], pot=100))


@pytest.mark.parametrize("code,advice,spec,extra", [
    ("HF_OPTION", {"option_id": "opt-9"}, {}, {}),
    ("HF_FALLBACK", {"source": "fallback"}, {}, {}),
    ("HF_FORBIDDEN_ACTION", {"kind": "fold"}, {}, {}),
    ("HF_FORBIDDEN_POINT", {"kind": "bet"}, {}, {"matched_points": ["chase"]}),
    ("HF_HALLUCINATION", {"kind": "bet"}, {}, {"hallucinated": True}),
    ("HF_ERROR", {"source": "error"}, {}, {}),
])
def test_each_hard_fail_caps_score(code, advice, spec, extra):
    payload = {**SPEC, **spec}
    result = scoring.score_case(
        spec=payload, advice=_advice(**advice), option_ids=["opt-1"], pot=100,
        **extra)
    assert result["hardFail"] == code
    assert result["score"] <= scoring.HF_CAP
    assert result["score"] <= 40


def test_no_hard_fail_keeps_full_score():
    result = scoring.score_case(spec=SPEC, advice=_advice(),
                                option_ids=["opt-1", "opt-2"], pot=100)
    assert result["hardFail"] is None
    assert result["score"] == 100.0


def test_stable_not_sampled_is_removed_from_denominator():
    unsampled = scoring.score_case(spec=SPEC, advice=_advice(),
                                   option_ids=["opt-1"], pot=100)
    assert unsampled["scores"]["stable"] is None
    assert unsampled["weight"] == 95.0
    assert unsampled["score"] == 100.0

    # 3 次重复里动作 2 次一致 → stable = 2/3
    sampled = scoring.score_case(
        spec=SPEC, advice=_advice(), option_ids=["opt-1"], pot=100,
        repeat_kinds=["bet", "raise", "bet"])
    assert sampled["scores"]["stable"] == pytest.approx(0.6667, abs=1e-4)
    expected = (35 + 15 + 30 + 15 + 5 * (2 / 3)) / 100 * 100
    assert sampled["score"] == pytest.approx(expected, abs=0.01)


def test_stable_score_needs_at_least_two_samples():
    assert scoring.stable_score(None) is None
    assert scoring.stable_score(["bet"]) is None
    assert scoring.stable_score(["bet", "bet"]) == 1.0
    assert scoring.stable_score(["bet", "call", "call"]) == pytest.approx(
        2 / 3)


def test_reason_falls_back_to_supports_action():
    # 点名要「下注」→ 支持；说「跟注」但动作是下注 → 不支持
    assert scoring.score_case(spec=SPEC, advice=_advice(reason="该下注"),
                               option_ids=["opt-1"], pot=100
                               )["scores"]["reason"] == 1.0
    assert scoring.score_case(spec=SPEC, advice=_advice(reason="跟注更好"),
                               option_ids=["opt-1"], pot=100
                               )["scores"]["reason"] == 0.0
    assert scoring.score_case(spec=SPEC, advice=_advice(reason=""),
                               option_ids=["opt-1"], pot=100
                               )["scores"]["reason"] == 0.0


def test_reason_uses_weighted_points_when_present():
    spec = {**SPEC, "required_points": [
        {"key": "range", "text": "范围", "weight": 2},
        {"key": "odds", "text": "赔率", "weight": 1},
    ]}
    result = scoring.score_case(spec=spec, advice=_advice(reason="范围"),
                                option_ids=["opt-1"], pot=100,
                                matched_points=["range"])
    assert result["scores"]["reason"] == pytest.approx(2 / 3, abs=1e-4)


def test_fact_score_counts_cited_and_wrong():
    assert scoring.fact_score(0, 0) == 1.0
    assert scoring.fact_score(2, 1) == 0.5
    assert scoring.fact_score(2, 5) == 0.0


def test_aggregate_reports_mean_p0_and_coverage():
    items = [
        {"priority": 0, "score": 80.0, "hardFail": None,
         "scores": {"action": 1.0}, "bucket": "a"},
        {"priority": 0, "score": 40.0, "hardFail": "HF_OPTION",
         "scores": {"action": 1.0}, "bucket": "a"},
        {"priority": 1, "score": 60.0, "hardFail": None,
         "scores": {"action": 0.0}, "bucket": "b"},
    ]
    result = scoring.aggregate(items, bucket_total=3)
    assert result["scored"] == 3
    assert result["score"] == scoring.mean_ci([80.0, 40.0, 60.0])
    assert result["score"]["mean"] == 60.0
    assert result["p0"] == {"total": 2, "passed": 1, "rate": 0.5,
                            "ci": scoring.wilson(1, 2)}
    assert result["hardFail"]["count"] == 1
    assert result["coverage"]["bucketTotal"] == 3
    assert result["coverage"]["reached"] == 0


def test_wilson_and_mean_ci_golden_numbers():
    assert scoring.wilson(8, 10) == [0.4902, 0.9433]
    assert scoring.wilson(0, 13) == [0.0, 0.2281]
    assert scoring.wilson(13, 13) == [0.7719, 1.0]
    assert scoring.wilson(0, 0) == [0.0, 0.0]
    assert scoring.mean_ci([40.0, 60.0, 80.0]) == {
        "n": 3, "mean": 60.0, "lo": 37.37, "hi": 82.63}


def test_coverage_needs_freq_min_n_samples():
    items = [{"bucket": "a", "score": 80.0}] * scoring.FREQ_MIN_N
    items += [{"bucket": "b", "score": 80.0}] * (scoring.FREQ_MIN_N - 1)
    result = scoring.coverage(items, bucket_total=2)
    assert result == {"bucketTotal": 2, "reached": 1, "rate": 0.5}


def test_freq_score_bands_and_sample_size():
    freq = {"action": "bet", "min": 0.4, "max": 0.6}
    assert scoring.freq_score(freq, ["bet"] * 14)["status"] == "insufficient"
    assert scoring.freq_score(freq, ["bet"] * 8
                              + ["fold"] * 12)["score"] == 1.0
    missed = scoring.freq_score(freq, ["bet"] * 15 + ["fold"] * 5)
    assert missed["status"] == "missed"
    # obs=0.75，超出上界 0.15，带宽 0.2 → 1 - 0.75
    assert missed["score"] == pytest.approx(0.25)
    assert scoring.freq_score(None, ["bet"] * 20)["status"] == "absent"


def test_p0_rate_counts_p0_cases_that_did_not_score():
    # 只按已出分的 P0 算分母会让通过率虚高：p0_total 覆盖没跑到的那些。
    items = [{"priority": 0, "score": 100.0, "scores": {"action": 1.0}},
             {"priority": 0, "score": None, "scores": {}},
             {"priority": 1, "score": 80.0, "scores": {"action": 1.0}}]
    result = scoring.aggregate(items, bucket_total=1, p0_total=3)
    assert result["p0"] == {"total": 3, "passed": 1, "rate": 0.3333,
                            "ci": scoring.wilson(1, 3)}


def test_bad_inputs_fail_open():
    result = scoring.score_case(
        spec={"sizing": "nope", "allowed_actions": None},
        advice={"option_id": "opt-1", "kind": "bet", "amount": "abc",
                "reason": None, "source": None},
        option_ids=["opt-1"], pot="x")
    assert result["scores"]["size"] == 0.0
    assert result["scores"]["action"] == 1.0
    assert result["scores"]["reason"] == 0.0
    assert result["hardFail"] is None
