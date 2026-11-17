"""ab_set.json → seed 案例集的纯映射（#55）。

只测纯逻辑：解析 23 条、ready 与 expect=n-a 的口径、spec 内容能过
`replay.specs.validate_spec`、重复规划结果稳定。落库与幂等由
test_case_sets.py 的服务层测试负责。
"""
from __future__ import annotations

from pathlib import Path

from replay import case_seed, specs

AB_SET = Path(__file__).resolve().parents[2] / "tools" / "ab_set.json"


def _plan():
    return case_seed.plan_seed(case_seed.load_ab_set(AB_SET))


def test_plan_seed_counts_follow_body_rule():
    plan = _plan()
    # tools/ab_set.json 现有 22 条（票面写 23，按文件实测为准）：全部建 spec，
    # 只有 ready 且 expect 不是 n-a 的才建 case。
    assert len(plan["specs"]) == 22
    assert len(plan["cases"]) == 13
    assert len(plan["cases"]) + len(plan["skipped"]) == 22
    ids = {case["seedId"] for case in plan["cases"]}
    assert {"P0-01", "P0-06", "P1-05", "P2-01", "P2-03"} <= ids


def test_expect_na_and_blocked_entries_only_keep_spec():
    plan = _plan()
    skipped = {item["seedId"]: item["reason"] for item in plan["skipped"]}
    # AC5 与正文冲突的那条：P2-02 status=ready 但 expect=n-a，按正文只留 spec。
    assert "P2-02" in skipped
    assert "expect=n-a" in skipped["P2-02"]
    # 非 ready 的条目一律只留 spec，并写明状态。
    assert "P1-06" in skipped and "blocked_cap" in skipped["P1-06"]
    assert "P3-01" in skipped and "needs_reconstruction" in skipped["P3-01"]


def test_specs_are_valid_and_carry_bucket_and_action():
    plan = _plan()
    by_name = {spec["name"]: spec for spec in plan["specs"]}
    assert len(by_name) == 22, "category 必须唯一，否则会静默合并两条 spec"

    river = by_name["value_vs_multiway_donk_jam"]
    assert river["content"]["bucket"] == {"street": ["river"]}
    assert river["content"]["allowed_actions"] == ["fold"]
    assert river["status"] == "active"

    for spec in plan["specs"]:
        # 未知字段/非法词表必须在这里就炸，而不是落库时才 422。
        specs.validate_spec(spec["content"])
        assert spec["note"].startswith("ab_set:")


def test_case_priority_and_spec_pin():
    plan = _plan()
    cases = {case["seedId"]: case for case in plan["cases"]}
    assert cases["P0-01"]["priority"] == 0
    assert cases["P1-05"]["priority"] == 1
    assert cases["P2-01"]["priority"] == 2
    assert cases["P0-01"]["specName"] == "value_vs_multiway_donk_jam"
    assert cases["P0-01"]["handUid"] == "20261101-d32f55ecc1"
    assert cases["P0-01"]["actionIndex"] == 16
    assert cases["P0-01"]["street"] == "river"


def test_plan_is_deterministic():
    assert _plan() == _plan()


def test_normalize_advice_words():
    assert case_seed.normalize_advice("all-in") == "allin"
    assert case_seed.normalize_advice("raise-1/2") == "raise"
    assert case_seed.normalize_advice("raise-1/3") == "raise"
    assert case_seed.normalize_advice("call") == "call"
    assert case_seed.normalize_advice("") is None
    assert case_seed.normalize_advice("n/a") is None
