#!/usr/bin/env python3
"""#27：补全顺序、同帧同额歧义、换街首帧旧徽章。

    nv tests/test_order_unknown.py

覆盖：
  · 同一帧两个同额跟注：不猜先后，两条都标 order_unknown；
  · 街末补出的弃牌：标 order_unknown（只看到人没了，看不到何时弃的）；
  · 换街首帧还挂着的旧徽章不记成新动作；等徽章变化或下注额变化才认；
  · 同一徽章重复帧不重复、金额晚到回填仍落在同一条记录上。
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

BOARD = ["2c", "7d", "9s", "Jh", "Kd"]


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def state(frame_id: str, *, board_len: int = 0, pot: float = 1.5,
          pbs: float = 0.0, bets: dict[str, float] | None = None,
          actions: dict[str, tuple[float, str]] | None = None,
          folded_status: tuple[str, ...] = (),
          unknown: tuple[str, ...] = ()) -> FrameState:
    """五个座位的一帧；`folded_status` 只改状态，不亮弃牌徽章。"""
    bets = bets or {}
    actions = actions or {}
    seats = [("hero", "me", "LJ"), ("sb", "SB", "SB"), ("bb", "BB", "BB"),
             ("btn", "BTN", "BTN"), ("utg", "UTG", "UTG")]
    players = []
    for sid, name, pos in seats:
        act = actions.get(sid)
        players.append(PlayerState(
            sid, name=name, position=pos, stack=100.0,
            bet=None if sid in unknown else bets.get(sid, 0.0),
            bet_unknown=sid in unknown,
            action=act[1] if act else None,
            fold=False,
            in_hand=True,
            status="folded" if sid in folded_status else "playing"))
    return FrameState(
        frame_id=frame_id, table_seats=9, players_in_hand=len(players),
        players=players, blinds=parse_blinds("1/2"), dealer="btn",
        hero_seat_id="hero", hero_turn=False,
        pot=pot, pot_before_street=pbs,
        street_bet_total=max(pot - pbs, 0.0))


def actions_of(hand: dict, street: str) -> list[dict]:
    for s in hand.get("streets") or []:
        if s.get("street") == street:
            return list(s.get("actions") or [])
    return []


def test_same_frame_equal_calls_marked_order_unknown() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    # 同一帧里 UTG 加注、SB/BB 都跟到 2：两个跟注同额，谁先谁后没有证据。
    tracker.observe(state(
        "f2", pot=6.0, pbs=0.0,
        bets={"utg": 2.0, "sb": 2.0, "bb": 2.0},
        actions={"utg": (2.0, "raise"), "sb": (2.0, "call"),
                 "bb": (2.0, "call")},
    ), ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    calls = {a["seat_id"]: a for a in actions_of(hand, "preflop")
             if a["action"] == "call"}
    check(set(calls) == {"sb", "bb"}, f"应有两个跟注：{calls}")
    check(all(a.get("order_unknown") is True for a in calls.values()),
          f"同帧同额跟注应标 order_unknown：{calls}")
    raises = [a for a in actions_of(hand, "preflop") if a["action"] == "raise"]
    check(all(a.get("order_unknown") is not True for a in raises),
          f"加注的先后不受同额歧义影响：{raises}")


def test_street_end_fold_marked_order_unknown() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    tracker.observe(state("f2", bets={"sb": 0.5, "bb": 1.0},
                          folded_status=("utg", "btn")),
                    ["7c", "7s"], [])
    tracker.observe(state("f3", board_len=3, pot=1.5, pbs=1.5,
                          bets={"sb": 0.5, "bb": 1.0},
                          folded_status=("utg", "btn")),
                    ["7c", "7s"], BOARD[:3])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    folds = [a for a in actions_of(hand, "preflop") if a["action"] == "fold"]
    check([a["seat_id"] for a in folds] == ["utg", "btn"],
          f"街末弃牌应按位置顺序：{folds}")
    check(all(a.get("order_unknown") is True for a in folds),
          f"街末补出的弃牌应标 order_unknown：{folds}")
    check(all(a.get("source") == "ledger" for a in folds),
          f"街末补出的弃牌应为 ledger：{folds}")


def test_stale_badge_on_new_street_is_ignored() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    tracker.observe(state("f2", pot=4.0, pbs=0.0, bets={"utg": 2.0},
                          actions={"utg": (2.0, "bet")}),
                    ["7c", "7s"], [])
    # 翻牌第一帧：名字槽里还挂着上一街的「下注」，下注额已是 0。
    tracker.observe(state("f3", board_len=3, pot=4.0, pbs=4.0,
                          actions={"utg": (0.0, "bet")}),
                    ["7c", "7s"], BOARD[:3])
    tracker.observe(state("f4", board_len=3, pot=4.0, pbs=4.0,
                          actions={"utg": (0.0, "bet")}),
                    ["7c", "7s"], BOARD[:3])
    hand = tracker.hand
    check(hand is not None, "应产出当前手牌")
    stale = [a for a in (hand.actions if hand else [])
             if a.street == "flop" and a.seat_id == "utg"
             and a.action in ("bet", "raise")]
    check(not stale, f"换街首帧旧徽章不能记成翻牌动作：{stale}")
    # 真正的下注出现（注额变化）才认。
    tracker.observe(state("f5", board_len=3, pot=7.0, pbs=4.0,
                          bets={"utg": 3.0}, actions={"utg": (3.0, "bet")}),
                    ["7c", "7s"], BOARD[:3])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    bets = [(a["seat_id"], a["action"], a.get("amount"))
            for a in actions_of(hand, "flop") if a["action"] in ("bet", "raise")]
    check(bets == [("utg", "bet", 3.0)],
          f"旧徽章应等注额变化后才记成下注：{bets}")


def test_stale_badge_replaced_by_new_badge() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    tracker.observe(state("f2", pot=4.0, pbs=0.0, bets={"utg": 2.0},
                          actions={"utg": (2.0, "bet")}),
                    ["7c", "7s"], [])
    # 换街首帧：还挂着上一街的「下注」，这一帧不认。
    tracker.observe(state("f3", board_len=3, pot=4.0, pbs=4.0,
                          actions={"utg": (0.0, "bet")}),
                    ["7c", "7s"], BOARD[:3])
    # 徽章换成「让牌」：不是旧徽章，立即认成翻牌过牌。
    tracker.observe(state("f4", board_len=3, pot=4.0, pbs=4.0,
                          actions={"utg": (0.0, "check")}),
                    ["7c", "7s"], BOARD[:3])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    utg = [a for a in actions_of(hand, "flop") if a["seat_id"] == "utg"]
    check([a["action"] for a in utg] == ["check"],
          f"徽章变化后应记成过牌：{utg}")
    check(utg and utg[0].get("source") == "badge",
          f"这条过牌应来自徽章而不是街末补全：{utg}")


def test_duplicate_badge_and_late_amount_backfill() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    # 徽章先到、下注额读坏：金额留空。
    tracker.observe(state("f2", pot=1.5, pbs=0.0, actions={"utg": (0.0, "call")},
                          unknown=("utg",)),
                    ["7c", "7s"], [])
    # 下一帧徽章没变、金额补到 2：回填同一条记录，不能多出一条。
    tracker.observe(state("f3", pot=6.0, pbs=0.0, bets={"utg": 2.0},
                          actions={"utg": (2.0, "call")}),
                    ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    calls = [a for a in actions_of(hand, "preflop")
             if a["seat_id"] == "utg" and a["action"] == "call"]
    check(len(calls) == 1, f"同一徽章的重复帧只能有一条：{calls}")
    check(calls and calls[0].get("amount") == 2.0,
          f"晚到的金额应回填到同一条记录：{calls}")


def main() -> int:
    tests = [test_same_frame_equal_calls_marked_order_unknown,
             test_street_end_fold_marked_order_unknown,
             test_stale_badge_on_new_street_is_ignored,
             test_stale_badge_replaced_by_new_badge,
             test_duplicate_badge_and_late_amount_backfill]
    for test in tests:
        before = len(FAILS)
        try:
            test()
        except Exception as exc:  # noqa: BLE001 —— 失败要带测试名
            FAILS.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
        mark = "✅" if len(FAILS) == before else "❌"
        print(f"  {mark} {test.__name__}")
    if FAILS:
        print("\n".join(f"  ✗ {f}" for f in FAILS))
        return 1
    print("\n✅ 补全顺序与顺序未知全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
