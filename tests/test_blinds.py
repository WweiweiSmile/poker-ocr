#!/usr/bin/env python3
"""#25：盲注基线、注额差来源与全弃/炸弹池不补假动作。

    nv tests/test_blinds.py

覆盖：
  · 开手帧盲注 pill 未渲染时，随后到账的 SB/BB 强制投入不落成 call；
  · SB 补到 1.0 的真实跟注仍然记录，来源 bet_delta、金额取本街总额；
  · 炸弹池每人强制前注不落成 call/raise；
  · 全弃到 BB 的翻前，BB 不补 check；
  · 翻前水位下限保留（BB 的 1BB 仍是水位，不被当成 0）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import LiveHandTracker  # noqa: E402
from poker_ocr.parse import Blinds, parse_blinds  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def state(frame_id: str, *, bets: dict[str, float] | None = None,
          actions: dict[str, tuple[float, str]] | None = None,
          folded: tuple[str, ...] = (),
          bomb_pot: bool = False, ante: float | None = None,
          pot: float = 1.5) -> FrameState:
    """五个座位的一帧；bets 缺省全 0（模拟盲注 pill 还没渲染）。"""
    bets = bets or {}
    actions = actions or {}
    seats = [("hero", "me", "LJ"), ("sb", "SB", "SB"), ("bb", "BB", "BB"),
             ("btn", "BTN", "BTN"), ("utg", "UTG", "UTG")]
    players = []
    for sid, name, pos in seats:
        act = actions.get(sid)
        players.append(PlayerState(
            sid, name=name, position=pos, stack=100.0,
            bet=bets.get(sid, 0.0),
            action=act[1] if act else None,
            fold=True if sid in folded else False,
            in_hand=True,
            status="folded" if sid in folded else "playing"))
    return FrameState(
        frame_id=frame_id, table_seats=9, players_in_hand=len(players),
        players=players,
        blinds=(Blinds(sb=0.5, bb=1.0, ante=ante, raw="1/2")
                if ante is not None else parse_blinds("1/2")), dealer="btn",
        hero_seat_id="hero", hero_turn=False, bomb_pot=bomb_pot,
        pot=pot, pot_before_street=0.0,
        street_bet_total=max(pot - 0.0, 0.0))


def actions_of(hand: dict) -> list[dict]:
    return [a for s in hand.get("streets") or []
            for a in s.get("actions") or []]


def seat_actions(hand: dict, seat_id: str) -> list[dict]:
    return [a for a in actions_of(hand) if a.get("seat_id") == seat_id]


def test_blinds_rendered_late_are_not_actions() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1"), ["7c", "7s"], [])
    tracker.observe(state("f2", bets={"sb": 0.5, "bb": 1.0},
                         pot=1.5), ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    for seat in ("sb", "bb"):
        rows = seat_actions(hand, seat)
        check(not any(a["action"] in ("call", "bet", "raise") for a in rows),
              f"{seat} 的盲注不能被写成行动：{rows}")


def test_sb_complete_is_a_real_call() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1"), ["7c", "7s"], [])
    tracker.observe(state("f2", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    tracker.observe(state("f3", bets={"sb": 1.0, "bb": 1.0}), ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    calls = [a for a in seat_actions(hand, "sb") if a["action"] == "call"]
    check(len(calls) == 1, f"SB 补到 1.0 应记一条 call：{seat_actions(hand, 'sb')}")
    if calls:
        check(calls[0].get("source") == "bet_delta",
              f"注额差推出的动作应为 bet_delta：{calls[0]}")
        check(calls[0].get("amount") == 1.0,
              f"金额应为本街加到总额：{calls[0]}")


def test_bomb_ante_posts_are_not_actions() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bomb_pot=True, ante=5.0, pot=50.0),
                    ["7c", "7s"], [])
    tracker.observe(state("f2", bomb_pot=True, ante=5.0, pot=50.0,
                          bets={sid: 5.0 for sid in
                                ("hero", "sb", "bb", "btn", "utg")}),
                    ["7c", "7s"], [])
    tracker.observe(state("f3", bomb_pot=True, ante=5.0, pot=50.0,
                          bets={"hero": 5.0, "sb": 5.0, "bb": 5.0,
                                "btn": 10.0, "utg": 5.0},
                          actions={"btn": (10.0, "raise")}),
                    ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    rows = actions_of(hand)
    check(not any(a["action"] in ("call", "bet") and (a.get("amount") or 0) <= 5.0
                  for a in rows), f"炸弹前注不能落成 call/bet：{rows}")
    btn = seat_actions(hand, "btn")
    check([a["action"] for a in btn] == ["raise"],
          f"BTN 只应有真实加注：{btn}")


def test_all_fold_to_bb_has_no_check() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    folded = ("hero", "sb", "btn", "utg")
    # 盲注 pill 已渲染：BB 的 1.0 是基线。
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    tracker.observe(state("f2", bets={"sb": 0.5, "bb": 1.0}, folded=folded),
                    ["7c", "7s"], [])
    tracker.observe(state("f3", bets={"sb": 0.5, "bb": 1.0}, folded=folded),
                    ["7c", "7s"], [])
    tracker.finish_current()
    hand = tracker.take_finished()
    check(hand is not None, "应产出手牌")
    if hand is None:
        return
    rows = actions_of(hand)
    check(not any(a["seat_id"] == "bb" and a["action"] == "check" for a in rows),
          f"全弃到 BB 不应补 BB 过牌：{rows}")
    check(sum(1 for a in rows if a["action"] == "fold") >= 4,
          f"四个弃牌都应落账：{rows}")


def test_flop_bet_delta_keeps_source_and_total() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    # 翻前：UTG 跟 1BB，其余（含 BB）保持盲注基线。
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    tracker.observe(state("f2", bets={"utg": 1.0, "sb": 0.5, "bb": 1.0},
                          actions={"utg": (1.0, "call")}), ["7c", "7s"], [])
    tracker.observe(state("flop", bets={}, pot=8.5), ["7c", "7s"],
                    ["Ah", "Kd", "7c"])
    tracker.observe(state("flop2", bets={"btn": 6.0}, pot=8.5), ["7c", "7s"],
                    ["Ah", "Kd", "7c"])
    hand = tracker.take_current()
    check(hand is not None, "应产出手牌")
    if hand is None:
        return
    bets = [a for a in seat_actions(hand, "btn") if a["action"] in ("bet", "raise")]
    check(len(bets) == 1, f"BTN 翻牌应有一条下注：{seat_actions(hand, 'btn')}")
    if bets:
        check(bets[0].get("source") == "bet_delta",
              f"无徽章推出来的动作应为 bet_delta：{bets[0]}")
        check(bets[0].get("amount") == 6.0,
              f"金额应为本街加到总额：{bets[0]}")


def main() -> int:
    tests = [test_blinds_rendered_late_are_not_actions,
             test_sb_complete_is_a_real_call,
             test_bomb_ante_posts_are_not_actions,
             test_all_fold_to_bb_has_no_check,
             test_flop_bet_delta_keeps_source_and_total]
    print("── 盲注基线与注额差口径 ──────────────────────")
    for t in tests:
        before = len(FAILS)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            FAILS.append(f"{t.__name__} 抛异常：{exc!r}")
        name = t.__name__.replace("test_", "")
        print(("  ✓ " if len(FAILS) == before else "  ✗ ") + name)
    print()
    for f in FAILS:
        print("  ✗ " + f)
    print(f"\n{'FAIL' if FAILS else 'OK'} ({len(FAILS)} failed)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
