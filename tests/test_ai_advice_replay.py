#!/usr/bin/env python3
"""tools/ai_advice_dev_hands.py 的重建逻辑测试（不连库、不调模型）。

    .venv/bin/python tests/test_ai_advice_replay.py

只测 walk_snapshots / classify：把一手真实形状的复盘手牌（dev 库 id=990010）
喂进去，核对每个决策点的底池、需跟额、行动线截断、对手快照等关键字段。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import ai_advice_dev_hands as tool  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


# dev 库 id=990010：BTN 持 AsTh，4 人池（UTG/UTG+1/UTG+2），天顺翻牌后一路打到河牌。
HAND = {
    "id": 990010,
    "heroPosition": "BTN",
    "heroCards": "AsTh",
    "heroStackBb": 352,
    "sb": 0.5, "bb": 1.0, "ante": 0.5, "bombPot": 0.0,
    "board": "Jc8dKdQcAd",
    "villainCount": 3,
    "villains": [
        {"name": "比卡奇 v", "stackBb": 453, "position": "UTG+2"},
        {"name": "平常心", "stackBb": 130, "position": "UTG"},
        {"name": "脏我就梭哈", "stackBb": 273, "position": "UTG+1"},
    ],
    "streets": [
        {"street": "preflop", "actions": [
            {"actor": "UTG", "action": "call"},
            {"actor": "UTG+1", "action": "call"},
            {"actor": "UTG+2", "action": "call"},
            {"actor": "hero", "action": "raise", "amountBb": 7.5},
            {"actor": "UTG", "action": "call"},
            {"actor": "UTG+1", "action": "call"},
            {"actor": "UTG+2", "action": "call"}]},
        {"street": "flop", "actions": [
            {"actor": "UTG", "action": "check"},
            {"actor": "UTG+1", "action": "check"},
            {"actor": "UTG+2", "action": "check"},
            {"actor": "hero", "action": "check"}]},
        {"street": "turn", "actions": [
            {"actor": "UTG", "action": "check"},
            {"actor": "UTG+1", "action": "check"},
            {"actor": "UTG+2", "action": "bet", "amountBb": 36},
            {"actor": "hero", "action": "raise", "amountBb": 90},
            {"actor": "UTG", "action": "fold"},
            {"actor": "UTG+1", "action": "fold"},
            {"actor": "UTG+2", "action": "call"}]},
        {"street": "river", "actions": [
            {"actor": "UTG+2", "action": "bet", "amountBb": 144.5},
            {"actor": "hero", "action": "fold"}]},
    ],
}


def test_walk_finds_three_postflop_decisions() -> None:
    ds = tool.walk_snapshots(tool.normalize(HAND))
    check([d["snap"].street for d in ds] == ["flop", "turn", "river"],
          f"应依次是 flop/turn/river 三个决策点，实际 {[d['snap'].street for d in ds]}")


def test_flop_decision() -> None:
    d = tool.walk_snapshots(tool.normalize(HAND))[0]
    s = d["snap"]
    check(s.street == "flop" and len(s.board) == 3, f"翻牌公共牌应 3 张：{s.board}")
    check(s.pot == 33.5, f"翻牌底池应 33.5（7 人次跟注翻前），实际 {s.pot}")
    check(s.to_call == 0 and s.hero_bet == 0, f"翻牌轮到 hero 时应没人下注：{s.to_call}/{s.hero_bet}")
    check(s.hero_invested == 7.5, f"翻牌 hero 只投入了翻前加注 7.5：{s.hero_invested}")
    check(d["actual"] == "check", f"翻牌实际动作应是 check：{d['actual']}")


def test_turn_decision_snapshot_truncates_at_hero() -> None:
    ds = tool.walk_snapshots(tool.normalize(HAND))
    d = ds[1]
    s = d["snap"]
    check(s.street == "turn" and len(s.board) == 4, f"转牌公共牌应 4 张：{s.board}")
    check(s.pot == 69.5, f"转牌底池应 33.5+36=69.5，实际 {s.pot}")
    check(s.to_call == 36.0, f"转牌需跟 36：{s.to_call}")
    # 快照的行动线必须停在 hero 加注之前：转牌街只有 3 条（两个过牌 + 一个下注）
    turn = [r for r in s.streets if r.street == "turn"][0]
    check(len(turn.actions) == 3, f"转牌街应只记到 hero 之前 3 条：{[a.action for a in turn.actions]}")
    check(d["actual"] == "raise" and d["actual_amt"] == 90,
          f"转牌实际动作应是 raise 到 90：{d['actual']} {d['actual_amt']}")


def test_river_decision_carries_full_history() -> None:
    d = tool.walk_snapshots(tool.normalize(HAND))[2]
    s = d["snap"]
    check(s.street == "river" and len(s.board) == 5, f"河牌公共牌应 5 张：{s.board}")
    check(s.pot == 358.0, f"河牌底池应 213.5+144.5=358.0，实际 {s.pot}")
    check(s.to_call == 144.5, f"河牌需跟 144.5：{s.to_call}")
    check(s.hero_invested == 97.5, f"河牌 hero 已投入 7.5+90=97.5：{s.hero_invested}")
    # 河牌快照应包含完整转牌街（hero 加注到 90 已在历史里）
    turn = [r for r in s.streets if r.street == "turn"][0]
    check(len(turn.actions) == 7, f"河牌快照里转牌街应 7 条：{[a.action for a in turn.actions]}")
    check(d["actual"] == "fold", f"河牌实际动作应是 fold：{d['actual']}")


def test_snapshot_opponents_and_blinds() -> None:
    s = tool.walk_snapshots(tool.normalize(HAND))[1]["snap"]
    check(len(s.opponents) == 3, f"应 3 个对手：{len(s.opponents)}")
    by_pos = {o.position: o for o in s.opponents}
    check(by_pos["UTG"].stack == 130, f"UTG 筹码 130：{by_pos['UTG'].stack}")
    check(by_pos["UTG+2"].bet == 36, f"UTG+2 本街已下注 36：{by_pos['UTG+2'].bet}")
    check(s.blinds is not None and s.blinds.bb == 1.0 and s.blinds.ante == 0.5,
          f"盲注 0.5/1(0.5)：{s.blinds}")
    check(s.hero_is_pfr and s.preflop_shapes == ("single-raise",),
          f"hero 是翻前加注者、single-raise：{s.hero_is_pfr}/{s.preflop_shapes}")


def test_multi_action_same_street_gives_two_decisions() -> None:
    """hero 同一街行动两次（下注后被加注、再跟注），应出两个决策点。"""
    h = {
        "id": 1, "heroPosition": "BTN", "heroCards": "AsTh",
        "heroStackBb": 100, "sb": 0, "bb": 0, "ante": 0, "bombPot": 0,
        "board": "2c7d9s", "villainCount": 1, "villains": [],
        "streets": [
            {"street": "flop", "actions": [
                {"actor": "hero", "action": "bet", "amountBb": 10},
                {"actor": "UTG", "action": "raise", "amountBb": 30},
                {"actor": "hero", "action": "call"}]},
        ],
    }
    ds = tool.walk_snapshots(h)
    check(len(ds) == 2, f"同街两次行动应 2 个决策点：{len(ds)}")
    check(ds[0]["actual"] == "bet" and ds[0]["snap"].to_call == 0,
          f"第一次：bet、to_call 0：{ds[0]['actual']}/{ds[0]['snap'].to_call}")
    check(ds[1]["actual"] == "call" and ds[1]["snap"].to_call == 20,
          f"第二次：call、需跟 30-10=20：{ds[1]['actual']}/{ds[1]['snap'].to_call}")


def test_classify_roundtrip() -> None:
    # 跟注 vs 跟注 → 一致；模型弃牌 vs 实际下注 → 动作不同
    check(tool.classify("call", "call", None, None, 10) == "一致", "call/call 应一致")
    check(tool.classify("fold", "bet", None, None, 10) == "动作不同", "fold/bet 应动作不同")
    # 加注到 90 vs 实际加注到 90，底池 60 → 一致；尺度差超 1/4 池才判尺度差
    check(tool.classify("raise", "raise", 90, 90, 60) == "一致", "同尺度加注应一致")
    check(tool.classify("raise", "raise", 30, 90, 60) == "尺度差", "尺度差 60 应超 1/4 池")
    # 实际 allin 与模型 raise 都算激进，看尺度
    check(tool.classify("raise", "allin", 100, 100, 40) == "一致", "raise/allin 同额应一致")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILS:
        print(f"\n{len(FAILS)} 个失败：")
        for f in FAILS:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("全部通过")
