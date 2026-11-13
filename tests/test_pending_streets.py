#!/usr/bin/env python3
"""#26：街末待行动集合与唯一解补全。

    nv tests/test_pending_streets.py

覆盖：
  · 全街无人下注、所有人都已配平 → 按位置顺序补 check（英雄在正确顺序）；
  · 手末最后一街底池账单正好等于各座欠额 → 唯一解补 call 到水位；
  · 底池账单对不上（还有加注没抓到）→ 不补 call；
  · 投入未知的街不补 check，只把欠动作的座位写进 data_warnings；
  · 街末补出的弃牌按位置顺序，不按 seat_id 字典序。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import ActionRecord, LiveHandTracker  # noqa: E402
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


def street_actions(hand: dict, street: str) -> list[dict]:
    for s in hand.get("streets") or []:
        if s.get("street") == street:
            return list(s.get("actions") or [])
    return []


def all_actions(hand: dict) -> list[dict]:
    return [a for s in hand.get("streets") or []
            for a in s.get("actions") or []]


def limped_preflop(tracker: LiveHandTracker) -> None:
    """五个人都溜入到 1.0，翻前配平。"""
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    tracker.observe(state(
        "f2", pot=5.0, pbs=0.0,
        bets={sid: 1.0 for sid in ("hero", "sb", "bb", "btn", "utg")},
        actions={"hero": (1.0, "call"), "sb": (1.0, "call"),
                 "btn": (1.0, "call"), "utg": (1.0, "call"),
                 "bb": (1.0, "check")},
    ), ["7c", "7s"], [])


def test_checks_follow_position_order() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    limped_preflop(tracker)
    tracker.observe(state("f3", board_len=3, pot=5.0, pbs=5.0),
                    ["7c", "7s"], BOARD[:3])
    tracker.observe(state("f4", board_len=3, pot=5.0, pbs=5.0),
                    ["7c", "7s"], BOARD[:3])
    # 转牌出现 → 结算翻牌：五人全过牌。
    tracker.observe(state("f5", board_len=4, pot=5.0, pbs=5.0),
                    ["7c", "7s"], BOARD[:4])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    flop = [a["seat_id"] for a in street_actions(hand, "flop")
            if a["action"] == "check"]
    check(flop == ["sb", "bb", "utg", "hero", "btn"],
          f"过牌应按位置顺序：{flop}")
    check(all(a.get("source") == "ledger"
              for a in street_actions(hand, "flop")),
          f"街末过牌应为 ledger：{street_actions(hand, 'flop')}")


def test_hand_end_unique_calls_fill_to_level() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    # UTG 下注 3；其余四人的下注 pill 没渲染，但底池 15 里已经含了他们的跟注。
    tracker.observe(state("f2", pot=15.0, pbs=0.0,
                          bets={"utg": 3.0}, actions={"utg": (3.0, "bet")}),
                    ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    calls = [a for a in street_actions(hand, "preflop")
             if a["action"] == "call"]
    check({a["seat_id"] for a in calls} == {"sb", "bb", "btn", "hero"},
          f"四人应补跟注：{calls}")
    check(all(a.get("amount") == 3.0 for a in calls),
          f"跟注金额应补到水位 3BB：{calls}")
    check(all(a.get("source") == "ledger" for a in calls),
          f"补出的跟注应为 ledger：{calls}")
    hero = [a for a in calls if a["seat_id"] == "hero"]
    check(len(hero) == 1, f"英雄位也应补到同一水位：{calls}")
    order = [a["seat_id"] for a in calls]
    check(order == sorted(order, key=lambda s: ("SB", "BB", "LJ", "BTN")
                          .index({"sb": "SB", "bb": "BB",
                                  "hero": "LJ", "btn": "BTN"}[s])),
          f"补注顺序应按位置：{order}")


def test_calls_not_filled_when_account_does_not_confirm() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    # 底池只有下注那 3BB：欠额（10.5）与账单（4.5−4.5=0）对不上，不能补。
    tracker.observe(state("f2", pot=4.5, pbs=0.0,
                          bets={"utg": 3.0}, actions={"utg": (3.0, "bet")}),
                    ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    calls = [a for a in all_actions(hand) if a["action"] == "call"]
    check(not calls, f"账单对不上时不能补跟注：{calls}")
    warnings = hand.get("data_warnings") or []
    check(any("未配平" in w for w in warnings),
          f"欠动作座位应写进诊断：{warnings}")


def test_unknown_street_does_not_fill_checks() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    # 开手帧 BB 的下注读坏：本街水位不可信。
    tracker.observe(state("f1", bets={"sb": 0.5}, unknown=("bb",)),
                    ["7c", "7s"], [])
    tracker.observe(state("f2", pot=1.5, pbs=0.0, bets={"sb": 0.5},
                          unknown=("bb",)), ["7c", "7s"], [])
    tracker.observe(state("f3", board_len=3, pot=1.5, pbs=1.5,
                          bets={"sb": 0.0}, unknown=("bb",)),
                    ["7c", "7s"], BOARD[:3])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    checks = [a for a in street_actions(hand, "preflop")
              if a["action"] == "check"]
    check(not checks, f"水位不可信的翻前不能补过牌：{checks}")
    warnings = hand.get("data_warnings") or []
    check(any("未配平" in w for w in warnings),
          f"欠动作座位应写进诊断：{warnings}")


def test_street_end_folds_follow_position_order() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}), ["7c", "7s"], [])
    # UTG、BTN 的座位还在，但状态已是 folded：没有徽章，靠街末差集补。
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
    folds = [a["seat_id"] for a in street_actions(hand, "preflop")
             if a["action"] == "fold"]
    check(folds == ["utg", "btn"],
          f"街末弃牌应按位置顺序（UTG 在 BTN 前）：{folds}")


def test_folded_on_earlier_street_is_not_checked_later() -> None:
    """前一街已弃牌的座位，手末帧没更新状态时也不能在后面的街补过牌。"""
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}),
                    ["7c", "7s"], [])
    hand = tracker.hand
    assert hand is not None
    hand.actions.append(ActionRecord.make(
        source="ledger", street="preflop", seat_id="sb", name="SB",
        position="SB", action="fold"))
    hand.street_start_active["turn"] = {"sb", "bb", "utg", "hero", "btn"}
    hand.active = {"sb", "bb", "utg", "hero", "btn"}
    hand.level["turn"] = 0.0
    hand.finalized_streets.discard("turn")
    pending = tracker._pending_seats(hand, "turn", None, None)
    sb = next(p for p in pending if p.seat_id == "sb")
    check(sb.folded and not sb.in_hand,
          f"翻前已弃的 SB 在转牌不能算在局：folded={sb.folded} in_hand={sb.in_hand}")


def test_existing_call_amount_is_backfilled_not_duplicated() -> None:
    """徽章先到、金额没读到的跟注：换街对账补金额，不再追加一条。"""
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", bets={"sb": 0.5, "bb": 1.0}),
                    ["7c", "7s"], [])
    hand = tracker.hand
    assert hand is not None
    hand.actions.append(ActionRecord.make(
        source="badge", street="preflop", seat_id="utg", name="UTG",
        position="UTG", action="call", amount=None))
    hand.last_index[("preflop", "utg")] = len(hand.actions) - 1
    before = len(hand.actions)
    check(tracker._fill_amount_instead(hand, "preflop", "utg", 2.0),
          "已有 call 应走补金额路径")
    check(len(hand.actions) == before, "补金额不应追加新动作")
    check(hand.actions[-1].amount == 2.0,
          f"金额应补到 2.0：{hand.actions[-1].amount}")


def main() -> int:
    tests = [test_checks_follow_position_order,
             test_hand_end_unique_calls_fill_to_level,
             test_calls_not_filled_when_account_does_not_confirm,
             test_unknown_street_does_not_fill_checks,
             test_street_end_folds_follow_position_order,
             test_folded_on_earlier_street_is_not_checked_later,
             test_existing_call_amount_is_backfilled_not_duplicated]
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
    print("\n✅ 待行动集合与街末补全全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
