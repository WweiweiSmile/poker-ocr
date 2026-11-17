"""期望库（spec）纯逻辑：§4.1 校验与 revision diff。

这里只测公开接口：
  · validate_spec(payload) → 规范化后的 §4.1 形状（非法输入 fail fast）
  · diff_specs(old, new)   → 人能看懂的变更清单
  · bucket_label(bucket)   → 列表分组用的桶标签
"""

from __future__ import annotations

import pytest

from replay.errors import ApiError
from replay.specs import bucket_label, diff_specs, meta, validate_spec


def full_payload(**overrides):
    payload = {
        "schema": "spot_spec.v1",
        "bucket": {
            "street": ["flop"],
            "position_bucket": ["EP"],
            "hand_tier": ["TP_WEAK", "TPTK", "TP_WEAK"],
            "board_texture": ["干燥"],
            "pot_type": ["single"],
            "facing": [0],
            "opponent_profile": ["松弱"],
        },
        "allowed_actions": ["bet", "check"],
        "forbidden_actions": ["fold"],
        "sizing": {"unit": "pot_fraction", "min": 0.33, "max": 0.75},
        "freq": {"action": "check", "min": 0.5, "max": 0.8},
        "required_points": [
            {"key": "protect_check_range", "text": "过牌是为了保护弱牌范围",
             "weight": 2},
            {"key": "value_vs_loose", "text": "对手偏松，价值下注可更宽"},
        ],
        "forbidden_points": [
            {"key": "assume_two_pair", "text": "断言对手有两对/三条"},
        ],
        "rationale": "前位用一部分顶对过牌。",
    }
    payload.update(overrides)
    return payload


def code_of(exc: pytest.ExceptionInfo) -> str:
    assert isinstance(exc.value, ApiError)
    return exc.value.code


# ─────────────────────────── 校验 ───────────────────────────

def test_valid_spec_is_normalized():
    spec = validate_spec(full_payload())
    assert spec["schema"] == "spot_spec.v1"
    # 空维度被省略；数组去重；维度顺序固定
    assert spec["bucket"] == {
        "street": ["flop"],
        "position_bucket": ["EP"],
        "hand_tier": ["TP_WEAK", "TPTK"],
        "board_texture": ["干燥"],
        "pot_type": ["single"],
        "facing": [0],
        "opponent_profile": ["松弱"],
    }
    # 动作按固定顺序，不按用户输入顺序
    assert spec["allowed_actions"] == ["check", "bet"]
    assert spec["forbidden_actions"] == ["fold"]
    assert spec["sizing"] == {"unit": "pot_fraction", "min": 0.33, "max": 0.75}
    assert spec["freq"] == {"action": "check", "min": 0.5, "max": 0.8}
    assert spec["required_points"][1]["weight"] == 1
    assert spec["forbidden_points"][0] == {
        "key": "assume_two_pair", "text": "断言对手有两对/三条"}


def test_empty_bucket_means_global_wildcard():
    spec = validate_spec(full_payload(bucket={}))
    assert spec["bucket"] == {}
    assert bucket_label(spec["bucket"]) == "全局"


def test_unknown_bucket_dimension_fails_fast():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(bucket={"position": ["EP"]}))
    assert code_of(exc) == "INVALID_SPEC_DIMENSION"
    assert "position" in exc.value.message
    assert "position_bucket" in exc.value.message


def test_invalid_bucket_value_fails_fast():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(bucket={"hand_tier": ["顶对"]}))
    assert code_of(exc) == "INVALID_SPEC_VALUE"
    assert "TPTK" in exc.value.message


def test_bucket_dimension_must_be_array():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(bucket={"street": "flop"}))
    assert code_of(exc) == "INVALID_SPEC_BUCKET"


def test_facing_accepts_only_zero_or_one():
    assert validate_spec(full_payload(bucket={"facing": [1]}))["bucket"] == {
        "facing": [1]}
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(bucket={"facing": [2]}))
    assert code_of(exc) == "INVALID_SPEC_VALUE"


def test_invalid_sizing_unit_fails_fast():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(
            sizing={"unit": "percent_pot", "min": 0.3, "max": 0.7}))
    assert code_of(exc) == "INVALID_SPEC_SIZING"
    assert "pot_fraction" in exc.value.message


def test_sizing_accepts_multiple_bands_and_bb_unit():
    spec = validate_spec(full_payload(sizing=[
        {"unit": "pot_fraction", "min": 0.33, "max": 0.75},
        {"unit": "bb", "min": 2, "max": 4},
    ]))
    assert spec["sizing"] == [
        {"unit": "pot_fraction", "min": 0.33, "max": 0.75},
        {"unit": "bb", "min": 2, "max": 4},
    ]


def test_sizing_accepts_a_zero_lower_bound():
    spec = validate_spec(full_payload(
        sizing={"unit": "pot_fraction", "min": 0, "max": 0.75}))
    assert spec["sizing"] == {"unit": "pot_fraction", "min": 0.0, "max": 0.75}


def test_actions_may_both_be_empty():
    spec = validate_spec(full_payload(allowed_actions=[], forbidden_actions=[]))
    assert spec["allowed_actions"] == []
    assert spec["forbidden_actions"] == []


def test_sizing_rejects_inverted_band():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(
            sizing={"unit": "bb", "min": 5, "max": 2}))
    assert code_of(exc) == "INVALID_SPEC_SIZING"


def test_min_n_is_not_allowed_in_spec():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(
            freq={"action": "check", "min": 0.5, "max": 0.8, "min_n": 15}))
    assert code_of(exc) == "INVALID_SPEC_FREQ"
    assert "FREQ_MIN_N" in exc.value.message


def test_overlapping_allowed_and_forbidden_actions_fail_fast():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(
            allowed_actions=["check", "bet"], forbidden_actions=["bet"]))
    assert code_of(exc) == "INVALID_SPEC_ACTION"
    assert "bet" in exc.value.message


def test_unknown_action_fails_fast():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(allowed_actions=["check", "shove"]))
    assert code_of(exc) == "INVALID_SPEC_ACTION"
    assert "shove" in exc.value.message


def test_duplicate_point_key_fails_fast():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(required_points=[
            {"key": "same", "text": "a"},
            {"key": "same", "text": "b"},
        ]))
    assert code_of(exc) == "INVALID_SPEC_POINT"
    assert "same" in exc.value.message


def test_point_text_is_required():
    with pytest.raises(ApiError) as exc:
        validate_spec(full_payload(forbidden_points=[{"key": "x", "text": " "}]))
    assert code_of(exc) == "INVALID_SPEC_POINT"


# ─────────────────────────── diff ───────────────────────────

def test_diff_detects_bucket_action_sizing_and_point_changes():
    old = validate_spec(full_payload())
    new = validate_spec(full_payload(
        bucket={"hand_tier": ["TP_WEAK"]},
        allowed_actions=["check", "bet", "call"],
        forbidden_actions=["fold"],
        sizing={"unit": "bb", "min": 2, "max": 4},
        required_points=[
            {"key": "protect_check_range", "text": "过牌保护弱牌范围", "weight": 2},
        ],
        forbidden_points=[],
        freq=None,
    ))
    changes = diff_specs(old, new)
    by_section = {}
    for change in changes:
        by_section.setdefault(change["section"], []).append(change)

    # 桶：hand_tier 从两个值收窄成一个
    tier = next(c for c in by_section["bucket"] if c["field"] == "hand_tier")
    assert tier["kind"] == "changed"
    assert tier["before"] == ["TP_WEAK", "TPTK"]
    assert tier["after"] == ["TP_WEAK"]
    # 其他维度被清空 → removed
    street = next(c for c in by_section["bucket"] if c["field"] == "street")
    assert street["kind"] == "removed"

    # 动作：多了一个 call
    added = [c for c in by_section["actions"] if c["kind"] == "added"]
    assert [(c["field"], c["value"]) for c in added] == [
        ("allowed_actions", "call")]

    # 尺寸带：pot_fraction 被换成 bb
    sizing = by_section["sizing"]
    assert {c["kind"] for c in sizing} == {"added", "removed"}
    assert any(c["after"] and c["after"]["unit"] == "bb" for c in sizing)

    # 要点：文案改了 → changed；删掉一个 → removed
    points = by_section["points"]
    changed = next(c for c in points if c["kind"] == "changed")
    assert changed["before"]["text"] == "过牌是为了保护弱牌范围"
    assert changed["after"]["text"] == "过牌保护弱牌范围"
    removed = [c for c in points if c["kind"] == "removed"]
    assert {c["key"] for c in removed} == {
        "value_vs_loose", "assume_two_pair"}

    # 频率带被移除
    assert by_section["freq"][0]["kind"] == "removed"


def test_diff_of_identical_spec_is_empty():
    spec = validate_spec(full_payload())
    assert diff_specs(spec, spec) == []


def test_bucket_label_is_readable():
    bucket = {"street": ["flop"], "hand_tier": ["TP_WEAK", "TPTK"],
              "opponent_profile": ["松弱"]}
    assert bucket_label(bucket) == "翻牌 · 顶对弱踢 / 顶对强踢 · 松弱"


def test_meta_exposes_vocabulary_for_the_form():
    vocab = meta()
    keys = [dim["key"] for dim in vocab["dimensions"]]
    assert keys == ["street", "position_bucket", "hand_tier",
                    "board_texture", "pot_type", "facing",
                    "opponent_profile"]
    tiers = {v["value"]: v["label"] for dim in vocab["dimensions"]
             if dim["key"] == "hand_tier" for v in dim["values"]}
    assert tiers["TPTK"] == "顶对强踢"
    assert {a["value"] for a in vocab["actions"]} == {
        "fold", "check", "call", "bet", "raise", "allin"}
    assert {u["value"] for u in vocab["sizingUnits"]} == {
        "pot_fraction", "bb"}
