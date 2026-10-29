#!/usr/bin/env python3
"""手牌分组统计：炸弹池判定、净值对齐、按组汇总。

    .venv/bin/python tests/test_hand_group_stats.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from hand_group_stats import (  # noqa: E402
    build_report,
    estimate_nets,
    is_bomb_pot,
    summarize,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def hand(**over) -> dict:
    base = {
        "hand_id": "h1",
        "hero_cards": ["As", "Kd"],
        "hero_position": "BTN",
        "board": ["2c", "7h", "Js"],
        "pot": 20.0,
        "streets": [{"street": "preflop", "pot_start": 4.5, "actions": []}],
    }
    base.update(over)
    return base


def test_bomb_flag_wins() -> None:
    check(is_bomb_pot(hand(bomb_pot=True)), "带 bomb_pot 标记的手没判成炸弹池")


def test_bomb_by_structure() -> None:
    structural = hand(streets=[{"street": "preflop", "pot_start": 45.0, "actions": []}])
    check(is_bomb_pot(structural), "9 人 × 5BB、翻前无动作应判为炸弹池")


def test_regular_big_pot_not_bomb() -> None:
    # 普通大池：翻前有行动，即便底池超过 30BB 也不是炸弹池
    big = hand(streets=[{"street": "preflop", "pot_start": 30.0,
                         "actions": [{"action": "raise", "is_hero": False}]}])
    check(not is_bomb_pot(big), "翻前有行动的普通大池被误判成炸弹池")


def test_bomb_flag_leak_is_not_bomb() -> None:
    """横幅串到下一手：标记为真、但有翻前行动 → 按普通牌局处理。"""
    leaked = hand(bomb_pot=True, streets=[{
        "street": "preflop", "pot_start": 4.5,
        "actions": [{"action": "raise", "is_hero": False, "amount": 11.0}]}])
    check(not is_bomb_pot(leaked),
          "有翻前行动的炸弹池标记是串标记，应判为普通牌局")


def test_regular_small_pot() -> None:
    check(not is_bomb_pot(hand()), "普通手牌被误判成炸弹池")


def test_net_skips_cross_session() -> None:
    hands = [hand(hero_stack_bb=100.0, _session=0),
             hand(hero_stack_bb=150.0, _session=1)]
    nets, _ = estimate_nets(hands)
    check(not nets, "跨 session 的筹码跳变被当成输赢（重买会算成大胜）")


def test_net_same_session() -> None:
    hands = [hand(hero_stack_bb=100.0, _session=0),
             hand(hero_stack_bb=97.5, _session=0)]
    nets, _ = estimate_nets(hands)
    check(nets.get(0) == -2.5, f"同 session 净值算错：{nets.get(0)}")


def test_net_drops_impossible() -> None:
    # 起始 10BB，下一手 100BB：不可能在底池 20BB 里赢到 90BB，应丢弃
    hands = [hand(hero_stack_bb=10.0, pot=20.0, _session=0),
             hand(hero_stack_bb=100.0, _session=0)]
    nets, dropped = estimate_nets(hands)
    check(not nets and dropped == 1, "不可能的筹码跳变没被丢弃")


def test_vpip_ignores_check() -> None:
    checked = hand(streets=[
        {"street": "preflop", "pot_start": 4.5,
         "actions": [{"action": "check", "is_hero": True}]}])
    called = hand(streets=[
        {"street": "preflop", "pot_start": 4.5,
         "actions": [{"action": "call", "is_hero": True}]}])
    check(summarize([checked], {}, [0])["vpip"] == 0, "过牌被算进了 VPIP")
    check(summarize([called], {}, [0])["vpip"] == 1, "跟注没算进 VPIP")


def test_report_splits_groups() -> None:
    bomb = hand(bomb_pot=True, hero_stack_bb=100.0, _session=0)
    regular = hand(hero_stack_bb=95.0, _session=0)
    tail = hand(hero_stack_bb=90.0, _session=0)
    report = build_report([bomb, regular, tail])
    check(report["bomb"]["hands"] == 1, f"炸弹池手数错：{report['bomb']['hands']}")
    check(report["regular"]["hands"] == 2, f"普通池手数错：{report['regular']['hands']}")
    check(report["usable_hands"] == 3, "可统计手数错")


def main() -> int:
    tests = [
        test_bomb_flag_wins,
        test_bomb_by_structure,
        test_regular_big_pot_not_bomb,
        test_bomb_flag_leak_is_not_bomb,
        test_regular_small_pot,
        test_net_skips_cross_session,
        test_net_same_session,
        test_net_drops_impossible,
        test_vpip_ignores_check,
        test_report_splits_groups,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print("\n❌ " + "\n❌ ".join(FAILS))
        return 1
    print("\n✅ 手牌分组统计全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
