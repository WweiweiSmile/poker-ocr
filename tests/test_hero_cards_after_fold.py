#!/usr/bin/env python3
"""弃牌后 hero 手牌不许再被改写（暗牌降质会把 6 读成 8）。

    nv tests/test_hero_cards_after_fold.py

2026-11-08 22:51 实盘 20261108-f1bdcddcf2：hero 是 6s4d，翻前弃牌后
1.4 秒整手牌被记成 8s4d。原因是弃牌后 WPK 会把 hero 手牌调暗（实测
V 从 246 掉到 ≈100），同一张 6 的字形上半缺口被糊死、读出第二个假孔，
NCC 翻成 8（本地用 0.8x 降采样可复现同一跳变）。

一手牌之内 hero 的底牌物理上不可能变，弃牌后又不再需要给建议 —— 所以
弃牌后的「新读数」只可能是降质读数。这里锁死：**弃牌后保留弃牌前的
读数，只把差异记进 cards_warnings 供复盘**；没有手牌时（中途接管）照常补。
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


def build_state(*, hero_fold: bool, hero_turn: bool, pot: float,
                frame_id: str) -> FrameState:
    """9 人桌，hero 在 CO；其他人都在局，底池按实盘推进。"""
    hero_status = "folded" if hero_fold else "playing"
    return FrameState(
        frame_id=frame_id, table_seats=9, players_in_hand=6,
        players=[
            PlayerState("hero", name="me", position="CO", stack=346.5,
                        bet=0.0, fold=True if hero_fold else None,
                        in_hand=True, status=hero_status),
            PlayerState("btn", name="BTN", position="BTN", stack=262.5,
                        bet=9.0, in_hand=True, status="playing"),
            PlayerState("sb", name="SB", position="SB", stack=245.0,
                        bet=9.0, in_hand=True, status="playing"),
            PlayerState("bb", name="BB", position="BB", stack=315.5,
                        bet=1.0, in_hand=True, status="playing"),
            PlayerState("utg", name="UTG", position="UTG", stack=128.0,
                        bet=0.0, in_hand=True, status="playing"),
            PlayerState("hj", name="HJ", position="HJ", stack=88.5,
                        bet=9.0, in_hand=True, status="playing"),
        ],
        blinds=parse_blinds("1/2"), dealer="btn",
        hero_seat_id="hero", hero_turn=hero_turn,
        pot=pot, pot_before_street=4.5, street_bet_total=pot - 4.5,
    )


def test_hero_cards_frozen_after_fold() -> None:
    """实盘口径：6s4d 弃牌后被读成 8s4d，落库必须还是 6s4d。"""
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(build_state(hero_fold=False, hero_turn=False, pot=6.0,
                                frame_id="deal"), ["6s", "4d"], [])
    tracker.observe(build_state(hero_fold=False, hero_turn=True, pot=24.0,
                                frame_id="turn"), ["6s", "4d"], [])
    # 弃牌后牌面变暗：这一帧起逐帧都读成 8s4d
    tracker.observe(build_state(hero_fold=True, hero_turn=False, pot=24.0,
                                frame_id="folded"), ["8s", "4d"], [])
    tracker.observe(build_state(hero_fold=True, hero_turn=False, pot=41.5,
                                frame_id="folded2"), ["8s", "4d"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    check(hand["hero_cards"] == ["6s", "4d"],
          f"弃牌后应保留 6s4d，实际 {hand['hero_cards']}")
    notes = [w for w in hand.get("cards_warnings") or [] if "弃牌后" in w]
    check(bool(notes), f"应在 cards_warnings 里留下差异记录：{hand.get('cards_warnings')}")


def test_hero_cards_still_corrected_while_in_hand() -> None:
    """没弃牌时保持原行为：OCR 纠正仍然生效（实测 8s 会被读成 6s）。"""
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(build_state(hero_fold=False, hero_turn=False, pot=6.0,
                                frame_id="deal"), ["8s", "4d"], [])
    tracker.observe(build_state(hero_fold=False, hero_turn=True, pot=24.0,
                                frame_id="turn"), ["8s", "4d"], [])
    tracker.observe(build_state(hero_fold=False, hero_turn=False, pot=24.0,
                                frame_id="fix"), ["6s", "4d"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    check(hand["hero_cards"] == ["6s", "4d"],
          f"未弃牌时应接受纠正，实际 {hand['hero_cards']}")


def test_hero_cards_fill_after_fold_when_empty() -> None:
    """中途接管：还没有手牌时，弃牌后的读数仍要补上（不能因为弃牌就永远空）。"""
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(build_state(hero_fold=True, hero_turn=False, pot=24.0,
                                frame_id="takeover"), ["8s", "4d"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    check(hand["hero_cards"] == ["8s", "4d"],
          f"空手牌时应补上读数，实际 {hand['hero_cards']}")


def main() -> int:
    for t in (test_hero_cards_frozen_after_fold,
              test_hero_cards_still_corrected_while_in_hand,
              test_hero_cards_fill_after_fold_when_empty):
        before = len(FAILS)
        t()
        print(f"  {'❌' if len(FAILS) > before else '✅'} {t.__name__}")
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ hero 手牌弃牌后冻结全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
