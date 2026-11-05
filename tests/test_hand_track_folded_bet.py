#!/usr/bin/env python3
"""HUD 残留 bet pill 不能把已弃牌座位推成 3-bet。

    .venv/bin/python tests/test_hand_track_folded_bet.py

2026-11-06 实盘：SB 已经弃牌，bet 槽读到 20.5；底池 20.5、本街总下注
16.5，按座位下注反推成了「SB 3-bet」，整手牌被标成 3-bet 底池。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import LiveHandTracker  # noqa: E402
from poker_ocr.parse import parse_blinds  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def build_state(*, sb_fold: bool) -> FrameState:
    if not sb_fold:
        # 第一帧：SB 还没弃牌，本街总下注 1.5（SB 0.5 + BB 1 + UTG 1）。
        return FrameState(
            frame_id="start", table_seats=9, players_in_hand=5,
            players=[
                PlayerState("hero", name="me", position="LJ", stack=74.5,
                            bet=0.0, in_hand=True, status="playing"),
                PlayerState("sb", name="SB", position="SB", stack=218.0,
                            bet=0.5, in_hand=True, status="playing"),
                PlayerState("btn", name="BTN", position="BTN", stack=146.0,
                            bet=0.0, in_hand=True, status="playing"),
                PlayerState("bb", name="BB", position="BB", stack=474.0,
                            bet=1.0, in_hand=True, status="playing"),
                PlayerState("utg", name="UTG", position="UTG", stack=829.5,
                            bet=1.0, in_hand=True, status="playing"),
            ],
            blinds=parse_blinds("1/2"), dealer="btn",
            hero_seat_id="hero", hero_turn=True,
            pot=5.5, pot_before_street=4.0, street_bet_total=1.5,
        )
    return FrameState(
        frame_id="folded-bet", table_seats=9, players_in_hand=5,
        players=[
            PlayerState("hero", name="me", position="LJ", stack=74.5,
                        bet=7.0, in_hand=True, status="playing"),
            PlayerState("sb", name="SB", position="SB", stack=218.0,
                        bet=20.5, fold=True, in_hand=True, status="folded"),
            # 真实线路：Hero 加注 7，BTN 跟 7，BB/UTG 各 1；SB 只投了 0.5。
            PlayerState("btn", name="BTN", position="BTN", stack=146.0,
                        bet=7.0, in_hand=True, status="playing"),
            PlayerState("bb", name="BB", position="BB", stack=474.0,
                        bet=1.0, in_hand=True, status="playing"),
            PlayerState("utg", name="UTG", position="UTG", stack=829.5,
                        bet=1.0, in_hand=True, status="playing"),
        ],
        blinds=parse_blinds("1/2"), dealer="btn",
        hero_seat_id="hero", hero_turn=False,
        pot=20.5, pot_before_street=4.0, street_bet_total=16.5,
    )


def test_folded_seat_bet_is_not_a_raise() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(build_state(sb_fold=False), ["7c", "7s"], [])
    tracker.observe(build_state(sb_fold=True), ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    actions = [a for s in hand.get("streets") or []
               for a in s.get("actions") or []]
    sb_actions = [a for a in actions if a.get("seat_id") == "sb"]
    check(not any(a.get("action") in ("bet", "raise") for a in sb_actions),
          f"已弃牌 SB 不应被推成加注：{sb_actions}")
    check(any(a.get("action") == "fold" for a in sb_actions),
          f"SB 最终应记成弃牌：{sb_actions}")


def test_impossible_bet_is_ignored_even_if_fold_unknown() -> None:
    """fold 漏读时，bet 超过本街总下注仍不能拿来推 raise。"""
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(build_state(sb_fold=False), ["7c", "7s"], [])
    hand = tracker.take_current()
    actions = [a for s in hand.get("streets") or []
               for a in s.get("actions") or []]
    sb_actions = [a for a in actions if a.get("seat_id") == "sb"]
    check(not any(a.get("action") in ("bet", "raise") for a in sb_actions),
          f"20.5 > 本街总下注 16.5，不能推成加注：{sb_actions}")


def main() -> int:
    tests = [test_folded_seat_bet_is_not_a_raise,
             test_impossible_bet_is_ignored_even_if_fold_unknown]
    print("── 已弃牌座位的残留下注 ──────────────────────")
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
    print("\n✅ 已弃牌座位的残留下注全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
