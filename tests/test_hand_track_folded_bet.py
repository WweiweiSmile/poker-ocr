#!/usr/bin/env python3
"""HUD 残留 bet pill 不能把已弃牌座位推成 3-bet。

    nv tests/test_hand_track_folded_bet.py

2026-11-06 实盘：SB 已经弃牌，bet 槽读到 20.5；底池 20.5、本街总下注
16.5，按座位下注反推成了「SB 3-bet」，整手牌被标成 3-bet 底池。
"""

from __future__ import annotations

import sys
from dataclasses import replace
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


def test_finish_current_adopts_the_ending_frame_folds() -> None:
    """收手那一帧的弃牌必须被采纳，否则这一手以「仍欠动作」收尾。

    2026-11-19 实盘：最后一条动作（面对下注弃牌）就发生在收池那一刻，而底池
    横幅已经翻成「总底池」→ 那一帧 `ok=False`，`observe` 直接丢掉；
    `finish_current()` 原来只用 `_last_frame`（弃牌之前那一帧），追踪仍当他
    在局 → 2f192f5c42 / 26672dd14a / fba0853748 / f82302bc49 四手都记成
    「未配平，仍欠动作」。
    """
    live = build_state(sb_fold=False)

    # 只传「最后一帧被接受的画面」：SB 的弃牌永远补不出来（旧行为）
    old = LiveHandTracker(confirm_frames=1)
    old.observe(live, ["7c", "7s"], [])
    old.finish_current()
    old_actions = [a for s in (old.take_finished() or {}).get("streets") or []
                   for a in s.get("actions") or []]
    check(not [a for a in old_actions
               if a.get("seat_id") == "sb" and a.get("action") == "fold"],
          f"这条用例要能复现旧缺口（只用 _last_frame 补不出 fold）：{old_actions}")

    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(live, ["7c", "7s"], [])
    end = replace(build_state(sb_fold=True), frame_id="pot-claimed")
    tracker.finish_current(end)
    hand = tracker.take_finished()
    check(hand is not None, "应产出已结束手牌")
    acts = [a for s in (hand or {}).get("streets") or []
            for a in s.get("actions") or []]
    check([a for a in acts
           if a.get("seat_id") == "sb" and a.get("action") == "fold"],
          f"收手帧上 SB 已弃牌，应补出 fold：{acts}")


def test_finish_current_ignores_vanished_seats() -> None:
    """收手帧里「读不到这个人」不等于弃牌：那是降级帧，不能照它补。"""
    tracker = LiveHandTracker(confirm_frames=1)
    live = build_state(sb_fold=False)
    tracker.observe(live, ["7c", "7s"], [])
    vanished = replace(live, frame_id="vanished", players_in_hand=4,
                       players=[p for p in live.players if p.seat_id != "sb"])
    tracker.finish_current(vanished)
    hand = tracker.take_finished()
    acts = [a for s in (hand or {}).get("streets") or []
            for a in s.get("actions") or []]
    check(not [a for a in acts
               if a.get("seat_id") == "sb" and a.get("action") == "fold"],
          f"没有弃牌证据时不能补 fold：{acts}")


def test_hand_end_inferred_fold_carries_evidence() -> None:
    """手末没有「下一街帧」时补出的 fold 也要带证据快照（#20 验收 1）。"""
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(build_state(sb_fold=False), ["7c", "7s"], [])
    # SB 整个人从在局集合里消失，但弃牌徽章没读到：只能靠手末差集补。
    gone = replace(build_state(sb_fold=False), frame_id="sb-gone",
                   players_in_hand=4,
                   players=[p for p in build_state(sb_fold=False).players
                            if p.seat_id != "sb"])
    tracker.observe(gone, ["7c", "7s"], [])
    tracker.finish_current()
    hand = tracker.take_finished()
    check(hand is not None, "应产出已结束手牌")
    fold = next((a for s in hand.get("streets") or []
                 for a in s.get("actions") or []
                 if a.get("seat_id") == "sb" and a.get("action") == "fold"),
                None)
    check(fold is not None, f"手末应补出 SB 的 fold：{hand.get('streets')}")
    if fold is None:
        return
    check(fold.get("inferred") is True, f"应为 inferred fold：{fold}")
    ev = fold.get("evidence")
    check(isinstance(ev, dict) and ev.get("frame_id") == "sb-gone",
          f"证据应取最后一帧：{ev}")


def main() -> int:
    tests = [test_folded_seat_bet_is_not_a_raise,
             test_impossible_bet_is_ignored_even_if_fold_unknown,
             test_finish_current_adopts_the_ending_frame_folds,
             test_finish_current_ignores_vanished_seats,
             test_hand_end_inferred_fold_carries_evidence]
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
