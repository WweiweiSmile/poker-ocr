#!/usr/bin/env python3
"""tools/compare_advice_styles.py 的纯逻辑测试：筛选手牌、重建决策点、对比口径。

不调模型、不读真实日志：用合成的手牌 + state 记录。

    .venv/bin/python tests/test_compare_advice_styles.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.advice import Option  # noqa: E402
from tools.compare_advice_styles import (  # noqa: E402
    build_decisions, build_results, classify,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def state(ts: str, stack: float, cards: list[str], pot: float = 6.0,
          board: list[str] | None = None, opp_bet: float = 0.0) -> dict:
    return {
        "type": "state", "ts": ts, "pot": pot, "hero_cards": cards,
        "board": board or [], "hero": {"stack": stack, "bet": 0.0},
        "opponents": [{"seat_id": "a", "position": "BB", "name": "A",
                       "stack": 100.0, "bet": opp_bet}],
        "players_in_hand": 9, "table_seats": 9,
        "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.5, "raw": "1/2(1)"},
    }


def hand(uid: str, ts: str, cards: list[str], actions: list[dict],
         pot_start: float = 6.0) -> dict:
    return {
        "type": "hand", "uid": uid, "hand_id": uid.split("-")[-1],
        "session": 1, "ts": ts, "hero_cards": cards,
        "hero_position": "BTN", "table_size": 9,
        "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.5, "raw": "1/2(1)"},
        "board": [], "pot": 15.0,
        "streets": [{"street": "preflop", "pot_start": pot_start,
                     "actions": actions}],
    }


def test_results_chain_and_missing_cards() -> None:
    hands = [
        hand("s01-h1", "10:00:10", ["As", "Ks"],
             [{"seat_id": "a", "action": "call", "amount": 1.0}]),
        hand("s01-h2", "10:00:20", ["7d", "2c"],
             [{"seat_id": "a", "action": "raise", "amount": 3.0},
              {"seat_id": "hero", "is_hero": True, "action": "call",
               "amount": 3.0}]),
        hand("s01-h3", "10:00:30", ["Ah", "Ad"],
             [{"seat_id": "a", "action": "call", "amount": 1.0}]),
    ]
    states = [
        state("10:00:05", 100.0, ["As", "Ks"]),
        state("10:00:09", 95.0, ["As", "Ks"]),
        state("10:00:15", 115.0, ["7d", "2c"], pot=9.0),
        state("10:00:29", 130.0, ["9c", "8c"]),   # 手牌对不上 = 边界帧
    ]
    out = build_results(hands, states)
    check(out["s01-h1"].net == -5.0, f"h1 应 -5：{out['s01-h1']}")
    check(out["s01-h2"].net == 20.0, f"h2 应 +20：{out['s01-h2']}")
    check(out["s01-h3"].net is None,
          f"手牌对不上时不能拿边界帧凑结果：{out['s01-h3']}")


def test_build_decisions_uses_state_pot_and_to_call() -> None:
    h = hand("s01-h2", "10:00:20", ["7d", "2c"],
             [{"seat_id": "a", "action": "raise", "amount": 3.0},
              {"seat_id": "hero", "is_hero": True, "action": "call",
               "amount": 3.0}])
    res = build_results([h], [
        state("10:00:15", 115.0, ["7d", "2c"], pot=9.0, opp_bet=3.0),
    ])["s01-h2"]
    decisions = build_decisions(h, res)
    check(len(decisions) == 1, f"应只有一个 hero 决策点：{len(decisions)}")
    d = decisions[0]
    check(d.street == "preflop", f"街不对：{d.street}")
    check(abs(d.pot - 9.0) < 1e-6, f"pot 应取 state 的 9：{d.pot}")
    check(abs(d.to_call - 3.0) < 1e-6, f"to_call 应 3：{d.to_call}")
    check(d.actual == "call" and d.actual_amount == 3.0,
          f"实际动作不对：{d.actual} {d.actual_amount}")
    check(d.snap.hero_stack == 115.0,
          f"hero_stack 应取 state：{d.snap.hero_stack}")


def test_classify_three_buckets() -> None:
    opt = Option("open-4", "Raise to 4BB", "raise", 4.0)
    check(classify(opt, "raise", 3.5, 9.0) == "一致",
          "尺度差小于 1/3 池应算一致")
    check(classify(opt, "raise", 9.0, 9.0) == "尺度差",
          "尺度差超过 1/3 池应算尺度差")
    check(classify(opt, "call", 3.0, 9.0) == "动作不同",
          "动作类别不同应算动作不同")
    check(classify(opt, None, None, 9.0) == "unknown",
          "没有实际动作应返回 unknown")


def main() -> int:
    tests = [
        test_results_chain_and_missing_cards,
        test_build_decisions_uses_state_pot_and_to_call,
        test_classify_three_buckets,
    ]
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{type(exc).__name__}: {exc}")
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 回放对比脚本全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
