#!/usr/bin/env python3
"""`tools/build_ab_cases.py` 的纯函数测试（#66 锚点用例）。

    nv tests/test_build_ab_cases.py

锚点必须是**确定性**地从真实建议记录里挑出来的：同输入同输出、去重不撞
现有条目、每个 hand 最多 2 条、按版本新→旧 + 动作轮转、凑不够就 raise。这里不连库，
全部用合成行；真实 ab_set 的落盘结果由 replay-console/tests/test_case_seed.py
覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "replay-console"
for path in (str(ROOT), str(CONSOLE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from replay import case_seed, specs  # noqa: E402
from tools.build_ab_cases import (  # noqa: E402
    build_anchor_cases,
    merge_ab_set,
    plan_anchor_cases,
    select_anchors,
)

OLD = "lean-zh-2026-11-01.1"
NEW = "lean-zh-2026-11-16.3"


def hand(n: int) -> str:
    return f"20261116-{n:010x}"


def row(**overrides):
    """一条形状与 replay_live_advices dump 相同的建议行。"""
    base = {
        "id": 1,
        "street": "river",
        "action_index": 0,
        "decision_key": "river:pot:1",
        "option_kind": "call",
        "actual_action": "call",
        "prompt_version": OLD,
        "external_uid": hand(1),
    }
    base.update(overrides)
    return base


def _case(seed_id="E-01"):
    return {
        "id": seed_id,
        "priority": "P2",
        "hand": hand(99),
        "street": "flop",
        "action_index": 4,
        "category": "existing_case",
        "source": "manual",
        "status": "ready",
        "expect": "",
        "new_advice": "call",
        "note": "ab_set: 现有条目",
    }


def test_select_skips_existing_decisions():
    existing = [{"hand": hand(1), "street": "river", "action_index": 1}]
    rows = [
        row(id=9, external_uid=hand(1), action_index=1, prompt_version=NEW),
        row(id=10, external_uid=hand(1), action_index=2, prompt_version=NEW),
    ]
    got = select_anchors(rows, existing, per_street=1, surplus=0)
    assert [item["action_index"] for item in got] == [2], "撞 (hand,街,序号) 必须跳过"


def test_version_priority_newest_first():
    rows = [
        row(id=1, action_index=0, prompt_version=OLD),
        row(id=2, action_index=1, prompt_version=NEW),
    ]
    got = select_anchors(rows, [], per_street=1, surplus=0)
    assert [item["id"] for item in got] == [2], "新版本必须排前面"


def test_version_revision_breaks_same_day_tie():
    rows = [
        row(id=1, action_index=0, prompt_version="lean-zh-2026-11-01.1"),
        row(id=2, action_index=1, prompt_version="lean-zh-2026-11-01.2"),
    ]
    got = select_anchors(rows, [], per_street=1, surplus=0)
    assert [item["id"] for item in got] == [2]


def test_round_robin_balances_option_kinds():
    rows = [
        row(id=1, external_uid=hand(1), action_index=0,
            option_kind="fold", prompt_version=NEW),
        row(id=2, external_uid=hand(2), action_index=0,
            option_kind="fold", prompt_version=OLD),
        row(id=3, external_uid=hand(3), action_index=0,
            option_kind="fold", prompt_version=OLD),
        row(id=4, external_uid=hand(4), action_index=0,
            option_kind="bet", prompt_version=OLD),
        row(id=5, external_uid=hand(5), action_index=0,
            option_kind="call", prompt_version=OLD),
    ]
    got = select_anchors(rows, [], per_street=4, surplus=0)
    assert [item["option_kind"] for item in got] ==         ["bet", "call", "fold", "fold"], "必须按 bet/raise/call/check/fold 轮转"
    assert [item["id"] for item in got] == [4, 5, 1, 2],         "同一动作内仍按版本新→旧；某类耗尽后继续轮转"


def test_per_hand_capped_at_two():
    rows = [row(id=i, external_uid=hand(7), action_index=i) for i in range(3)]
    rows.append(row(id=9, external_uid=hand(8), action_index=9))
    got = select_anchors(rows, [], per_street=3, surplus=0)
    assert len(got) == 3
    assert sum(item["external_uid"] == hand(7) for item in got) == 2, \
        "每个 hand 最多取 2 条，第 3 条必须来自别的 hand"


def test_insufficient_raises_with_shortfall():
    try:
        select_anchors([row()], [], per_street=5, surplus=0)
    except ValueError as exc:
        message = str(exc)
        assert message.startswith("锚点不足："), message
        assert "river 需新增 5 条" in message and "缺 4" in message, message
    else:
        raise AssertionError("凑不够必须 raise，不许静默少给")


def test_min_total_raises():
    try:
        select_anchors([row()], [], per_street=1, surplus=0, min_total=60)
    except ValueError as exc:
        assert "锚点总数不足" in str(exc), str(exc)
    else:
        raise AssertionError("总数不够必须 raise")


def test_note_is_fixed_traceability_format():
    diverged = row(
        id=7, street="river", action_index=3, decision_key="river:pot:2",
        option_kind="bet", actual_action="check", prompt_version=NEW,
        external_uid=hand(3))
    case = build_anchor_cases([diverged])[0]
    assert case["note"] == (
        f"锚点：advice_row=7 hand={hand(3)} street=river action_index=3 "
        f"version={NEW} 建议=bet｜真实动作=check "
        f"decision=river:pot:2｜分歧"
    ), case["note"]

    unknown = build_anchor_cases([
        row(id=8, actual_action=None, decision_key="[]")])[0]
    assert unknown["note"].endswith("建议=call｜真实动作=未记录"), unknown["note"]
    assert not unknown["note"].endswith("分歧"), "真实动作缺失不能误标分歧"


def test_note_omits_empty_decision_key():
    for empty in ("[]", "null", "", None):
        note = build_anchor_cases([
            row(id=11, decision_key=empty, actual_action="call")])[0]["note"]
        assert " decision=" not in note, note
        assert note.endswith("建议=call｜真实动作=call"), note
        assert "street=river" in note and "action_index=0" in note, note

    note = build_anchor_cases([
        row(id=12, decision_key="river:pot:3")])[0]["note"]
    assert note.endswith("真实动作=call decision=river:pot:3"), note


def test_case_fields_match_ab_set_schema():
    case = build_anchor_cases([row(option_kind="all-in")])[0]
    assert set(case) == {
        "id", "priority", "hand", "street", "action_index", "category",
        "source", "status", "expect", "new_advice", "note",
    }, sorted(case)
    assert case["id"] == "C-river-01"
    assert case["category"] == "anchor_river_01"
    assert case["priority"] == "P1"
    assert case["source"] == "regression"
    assert case["status"] == "ready"
    assert case["expect"] == ""
    assert case["new_advice"] == "allin", "必须用 specs.ACTIONS 词，不写 all-in"


def test_merge_appends_and_preserves_rest():
    data = {
        "schema": "ab_set.v1",
        "updated": "2026-11-01",
        "cases": [_case()],
        "skipped": [{"id": "S-01"}],
    }
    new = build_anchor_cases([row(street="river")], data["cases"])
    merged = merge_ab_set(data, new, updated="2026-11-18")
    assert merged["cases"][:-1] == data["cases"], "现有条目一条都不许改/删"
    assert merged["cases"][-1] is new[0]
    assert merged["skipped"] == [{"id": "S-01"}], "skipped 不许动"
    assert merged["updated"] == "2026-11-18"
    assert data["cases"] == [_case()], "merge 不能原地改输入"


def test_output_passes_plan_seed_validation():
    existing = [_case()]
    rows = [row(id=i + 1, street=street, external_uid=hand(i + 10))
            for i, street in enumerate(("preflop", "flop", "turn", "river"))]
    new = plan_anchor_cases(rows, existing, per_street=1, surplus=0)
    assert len(new) == 3, "flop 已有 1 条、目标 1 条，不再新增"
    merged = merge_ab_set(
        {"schema": "ab_set.v1", "updated": "2026-11-01",
         "cases": existing, "skipped": []},
        new, updated="2026-11-18")
    plan = case_seed.plan_seed(merged)
    assert len(plan["specs"]) == 4
    assert len(plan["cases"]) == 4
    assert len(plan["skipped"]) == 0
    names = [spec["name"] for spec in plan["specs"]]
    assert len(names) == len(set(names)), "category 必须唯一"
    for spec in plan["specs"]:
        specs.validate_spec(spec["content"])


def test_selection_is_deterministic():
    rows = [row(id=i, external_uid=hand(i), action_index=i) for i in range(6)]
    first = select_anchors(rows, [], per_street=3, surplus=0)
    second = select_anchors(rows, [], per_street=3, surplus=0)
    assert first == second


def main() -> int:
    failed = []
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
            except Exception as exc:  # noqa: BLE001 - 汇总所有失败再退出
                failed.append(name)
                print(f"✗ {name}: {exc}")
    if failed:
        print(f"❌ {len(failed)} 条失败：{', '.join(failed)}")
        return 1
    print("✅ test_build_ab_cases 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
