#!/usr/bin/env python3
"""行动线回放审计：来源拆分、弃牌推断、盲注误记、顺序未知。

    nv tests/test_action_line_audit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import action_line_audit as audit  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def hand() -> dict:
    return {
        "hand_id": "h1",
        "blinds": {"sb": 0.5, "bb": 1.0, "ante": None, "raw": "1/2"},
        "streets": [
            {"street": "preflop", "actions": [
                {"seat_id": "sb", "position": "SB", "action": "call",
                 "amount": 0.5, "source": "ledger", "inferred": True},
                {"seat_id": "bb", "position": "BB", "action": "call",
                 "amount": 1.0, "source": "badge"},
                {"seat_id": "utg", "position": "UTG", "action": "raise",
                 "amount": 2.0, "source": "badge"},
            ]},
            {"street": "flop", "actions": [
                {"seat_id": "bb", "position": "BB", "action": "fold",
                 "amount": None, "source": "ledger", "inferred": True,
                 "order_unknown": True},
                {"seat_id": "utg", "position": "UTG", "action": "check",
                 "amount": None, "source": "badge"},
            ]},
        ],
    }


def test_summarize_splits_by_source() -> None:
    s = audit.summarize([hand()])
    check(s["actions"] == 5, f"动作数应为 5：{s}")
    check(s["inferred"] == 2, f"推断数应为 2：{s}")
    check(s["sources"].get("badge") == 3, f"badge 应为 3：{s['sources']}")
    check(s["sources"].get("ledger") == 2, f"ledger 应为 2：{s['sources']}")
    check(s["inferred_by_source"].get("ledger") == 2,
          f"ledger 推断应为 2：{s['inferred_by_source']}")
    check(s["folds"] == 1 and s["fold_inferred"] == 0,
          f"街末不在局弃牌不算漏抓：{s}")
    check(s["fold_exit"] == 1, f"街末不在局弃牌应为 1：{s}")
    check(s["inferred_effective"] == 1,
          f"剔除离局弃牌后推断应为 1：{s}")
    check(s["order_unknown"] == 1, f"顺序未知应为 1：{s}")


def test_blind_posts_are_flagged() -> None:
    rows = audit.blind_misrecords([hand()])
    check(len(rows) == 2, f"应抓到 2 条盲注误记：{rows}")
    kinds = {(r["position"], r["action"], r["amount"]) for r in rows}
    check(kinds == {("SB", "call", 0.5), ("BB", "call", 1.0)},
          f"误记应是 SB 0.5 / BB 1.0：{kinds}")


def test_real_completion_is_not_flagged() -> None:
    h = hand()
    h["streets"][0]["actions"] = [
        {"seat_id": "sb", "position": "SB", "action": "call",
         "amount": 1.0, "source": "badge"},
        {"seat_id": "bb", "position": "BB", "action": "call",
         "amount": 9.0, "source": "badge"},
    ]
    check(not audit.blind_misrecords([h]),
          f"真实补注/跟注不应算盲注误记：{audit.blind_misrecords([h])}")


def test_hand_rows_count_per_hand() -> None:
    rows = audit.hand_rows([hand()])
    check(len(rows) == 1 and rows[0]["actions"] == 5, f"逐手行不对：{rows}")
    check(rows[0]["blind"] == 2 and rows[0]["order_unknown"] == 1,
          f"逐手统计不对：{rows}")


def main() -> int:
    tests = [test_summarize_splits_by_source,
             test_blind_posts_are_flagged,
             test_real_completion_is_not_flagged,
             test_hand_rows_count_per_hand]
    print("── 行动线回放审计 ──────────────────────────")
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{exc!r}")
        print(("  ✓ " if len(FAILS) == before else "  ✗ ")
              + t.__name__.replace("test_", ""))
    print()
    for f in FAILS:
        print("  ✗ " + f)
    print(f"\n{'FAIL' if FAILS else 'OK'} ({len(FAILS)} failed)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
