#!/usr/bin/env python3
"""#125：离桌动作按本街水位分流 —— 有注弃牌、无注过牌。

    nv tests/test_left_actions.py

覆盖：
  · 翻牌有人下注，离桌座位补「弃牌」（source=left、推断、顺序未知）；
  · 翻牌无人下注，离桌座位补「过牌」，同样标记推断；
  · 同一街两个离桌座位按位置顺序补，不按座位号字典序；
  · hero 离桌、已全下、本街已有动作的座位都不补；
  · 暂离（left_uncertain）一条动作都不补（负证据）；
  · 误判离桌恢复时只撤销离桌补出的动作，真实弃牌不动；
  · 街末已结算的街不重复补；
  · 换手帧结算最后一街：状态层已清掉 left，仍按水位分流、不退回 ledger。
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
HERO_CARDS = ["Ah", "Qd"]
SEATS = (
    ("hero", "me", "LJ"),
    ("sb", "SB", "SB"),
    ("bb", "BB", "BB"),
    ("btn", "BTN", "BTN"),
    ("utg", "UTG", "UTG"),
)
LEFT_SEAT = dict(fold=True, status="folded", stack=None, bet=0.0, left=True)
ABSENT_SEAT = dict(fold=None, status="uncertain", stack=None, bet=None,
                   left_uncertain=True)


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def seat(seat_id: str, name: str, position: str, **overrides) -> PlayerState:
    base = dict(seat_id=seat_id, name=name, position=position, stack=100.0,
                bet=0.0, action=None, fold=False, all_in=False,
                in_hand=True, status="playing")
    base.update(overrides)
    return PlayerState(**base)


def frame(frame_id: str, *, board_len: int = 0, pot: float = 1.5,
          pbs: float = 0.0, **overrides) -> FrameState:
    """五个座位的一帧；overrides 按座位 id 覆盖读数。"""
    players = [seat(sid, name, pos, **overrides.get(sid, {}))
               for sid, name, pos in SEATS]
    return FrameState(
        frame_id=frame_id, table_seats=9, players_in_hand=len(players),
        players=players, blinds=parse_blinds("1/2"), dealer="btn",
        hero_seat_id="hero", hero_turn=False, pot=pot,
        pot_before_street=pbs, street_bet_total=max(pot - pbs, 0.0))


def preflop(frame_id: str) -> FrameState:
    return frame(frame_id)


def flop(frame_id: str, *, pot: float = 1.5, **overrides) -> FrameState:
    return frame(frame_id, board_len=3, pot=pot, pbs=1.5, **overrides)


def turn(frame_id: str, *, pot: float = 1.5, **overrides) -> FrameState:
    return frame(frame_id, board_len=4, pot=pot, pbs=1.5, **overrides)


def start() -> LiveHandTracker:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(preflop("f1"), HERO_CARDS, [])
    tracker.observe(flop("f2"), HERO_CARDS, BOARD[:3])
    return tracker


def left_actions(tracker: LiveHandTracker, street: str) -> list:
    return [a for a in tracker.hand.street_actions(street) if a.left]


def by_seat(tracker: LiveHandTracker, street: str, seat_id: str) -> list:
    return [a for a in tracker.hand.street_actions(street)
            if a.seat_id == seat_id]


def test_left_seat_folds_when_street_has_a_bet() -> None:
    tracker = start()
    # 翻牌 UTG 下注 2，同一街 BTN 离桌：轮到他时前面有人下注 → 弃牌。
    tracker.observe(flop("f3", pot=3.5,
                         utg={"bet": 2.0, "action": "bet"},
                         btn=LEFT_SEAT), HERO_CARDS, BOARD[:3])
    check(not left_actions(tracker, "flop"),
          "街中间不补动作：离桌动作等街末水位确定")
    tracker.observe(turn("f4", pot=3.5, btn=LEFT_SEAT), HERO_CARDS, BOARD[:4])
    acts = left_actions(tracker, "flop")
    check(len(acts) == 1, f"离桌应补一条动作：{acts}")
    if not acts:
        return
    act = acts[0]
    check(act.seat_id == "btn" and act.action == "fold",
          f"有注场景应补弃牌：{act}")
    check(act.source == "left", f"证据源应记离桌：{act.source}")
    check(act.inferred and act.order_unknown,
          f"离桌补出的动作是推断且顺序未知：{act}")
    check(act.amount is None, f"离桌弃牌不编金额：{act.amount}")
    check(not [a for a in tracker.hand.street_actions("preflop")
               if a.seat_id == "btn" and a.action == "fold"],
          "离桌发生在翻牌，不应在翻前补弃牌")


def test_left_seat_checks_when_street_has_no_bet() -> None:
    tracker = start()
    # 翻牌全员过牌（水位 0），BTN 同街离桌：轮到他时没人下注 → 过牌。
    tracker.observe(flop("f3", btn=LEFT_SEAT), HERO_CARDS, BOARD[:3])
    tracker.observe(turn("f4", btn=LEFT_SEAT), HERO_CARDS, BOARD[:4])
    acts = left_actions(tracker, "flop")
    check(len(acts) == 1, f"离桌应补一条动作：{acts}")
    if not acts:
        return
    act = acts[0]
    check(act.seat_id == "btn" and act.action == "check",
          f"无注场景应补过牌：{act.action}")
    check(act.source == "left", f"证据源应记离桌：{act.source}")
    check(act.inferred and act.order_unknown,
          f"离桌补出的过牌也是推断且顺序未知：{act}")
    check(act.amount is None, f"过牌不带金额：{act.amount}")
    check(len(by_seat(tracker, "flop", "btn")) == 1,
          "同一街离桌只补一条动作")


def test_two_left_seats_follow_position_order() -> None:
    tracker = start()
    tracker.observe(flop("f3", sb=LEFT_SEAT, btn=LEFT_SEAT),
                    HERO_CARDS, BOARD[:3])
    tracker.observe(turn("f4", sb=LEFT_SEAT, btn=LEFT_SEAT),
                    HERO_CARDS, BOARD[:4])
    acts = left_actions(tracker, "flop")
    check([a.seat_id for a in acts] == ["sb", "btn"],
          f"应按位置顺序补（SB 在 BTN 前），不按座位号：{[a.seat_id for a in acts]}")
    check(all(a.order_unknown for a in acts),
          "补出的动作都标顺序未知，不伪造顺序")


def test_no_action_for_hero_all_in_or_acted_seat() -> None:
    # hero 离桌：他的手牌与动作另有来源，不替他补。
    tracker = start()
    tracker.observe(flop("f3", hero={"fold": True, "status": "folded",
                                     "stack": None, "left": True,
                                     "bet": 0.0}), HERO_CARDS, BOARD[:3])
    tracker.observe(turn("f4", hero={"fold": True, "status": "folded",
                                     "stack": None, "left": True,
                                     "bet": 0.0}), HERO_CARDS, BOARD[:4])
    check(not left_actions(tracker, "flop"), "hero 离桌不补动作")

    # 已全下再离桌：不能既 all_in 又补弃牌。
    tracker = start()
    tracker.observe(flop("f3", pot=101.5,
                         btn={"bet": 100.0, "action": "bet", "all_in": True,
                              "stack": 0.0}), HERO_CARDS, BOARD[:3])
    tracker.observe(turn("f4", pot=101.5, btn=LEFT_SEAT),
                    HERO_CARDS, BOARD[:4])
    check(not left_actions(tracker, "flop"), "全下后离桌不补动作")

    # 本街已经有动作（过牌）再离桌：不重复补。
    tracker = start()
    tracker.observe(flop("f3", btn={"action": "check"}), HERO_CARDS, BOARD[:3])
    tracker.observe(turn("f4", btn=LEFT_SEAT), HERO_CARDS, BOARD[:4])
    acts = by_seat(tracker, "flop", "btn")
    check(len(acts) == 1 and acts[0].action == "check",
          f"已有动作的人不再补一条：{[(a.action, a.source) for a in acts]}")


def test_absent_seat_gets_no_action() -> None:
    tracker = start()
    tracker.observe(flop("f3", btn=ABSENT_SEAT), HERO_CARDS, BOARD[:3])
    tracker.observe(turn("f4", btn=ABSENT_SEAT), HERO_CARDS, BOARD[:4])
    check(not by_seat(tracker, "flop", "btn"),
          f"暂离一条动作都不补：{by_seat(tracker, 'flop', 'btn')}")


def test_reviving_left_seat_revokes_only_left_actions() -> None:
    tracker = start()
    # 同一街：UTG 真实弃牌（徽章），BTN 离桌（无注 → 过牌）。
    tracker.observe(flop("f3", utg={"fold": True, "status": "folded",
                                    "bet": 0.0, "action": "fold"},
                         btn=LEFT_SEAT), HERO_CARDS, BOARD[:3])
    tracker.observe(turn("f4", utg={"fold": True, "status": "folded",
                                    "bet": 0.0},
                         btn=LEFT_SEAT), HERO_CARDS, BOARD[:4])
    acts = left_actions(tracker, "flop")
    check(len(acts) == 1 and acts[0].action == "check",
          f"离桌过牌应先补出来：{[(a.seat_id, a.action) for a in acts]}")

    # 误判恢复：BTN 手牌图标回来 → 只撤销离桌补出的那条。
    tracker.observe(turn("f5", utg={"fold": True, "status": "folded",
                                    "bet": 0.0},
                         btn={"bet": 0.0}), HERO_CARDS, BOARD[:4])
    check(not left_actions(tracker, "flop"),
          f"误判离桌恢复应撤销离桌动作：{left_actions(tracker, 'flop')}")
    utg_acts = by_seat(tracker, "flop", "utg")
    check(any(a.action == "fold" and not a.left for a in utg_acts),
          f"真实弃牌不受影响：{[(a.action, a.source) for a in utg_acts]}")


def test_finalized_street_is_not_backfilled_twice() -> None:
    tracker = start()
    tracker.observe(flop("f3", btn=LEFT_SEAT), HERO_CARDS, BOARD[:3])
    tracker.observe(turn("f4", btn=LEFT_SEAT), HERO_CARDS, BOARD[:4])
    tracker.observe(turn("f5", btn=LEFT_SEAT), HERO_CARDS, BOARD[:4])
    acts = left_actions(tracker, "flop")
    check(len(acts) == 1,
          f"已结算的街不重复补：{[(a.seat_id, a.action) for a in acts]}")


def finished_rows(tracker: LiveHandTracker) -> list[tuple[str, dict]]:
    """换手后取回已结束的那一手：(street, action) 行。"""
    hand = tracker.take_finished()
    if hand is None:
        return []
    return [(s.get("street"), a) for s in hand.get("streets") or []
            for a in s.get("actions") or []]


def seat_gone(frame_id: str) -> FrameState:
    """换手前的清空帧：状态层已经不再把离桌/暂离的人算在本手里。"""
    return frame(frame_id, pot=1.5,
                 # 人不在这一手了：不在局、也不是 left（left 已被状态层清空）。
                 **{"btn": {"in_hand": False, "status": "away", "fold": False,
                            "stack": 100.0}})


def test_hand_end_finalize_keeps_left_rule_for_last_street() -> None:
    """换手帧结算最后一街时，状态层已经清掉 left —— 仍要按水位分流补弃牌。"""
    tracker = start()
    tracker.observe(flop("f3", pot=3.5, utg={"bet": 2.0, "action": "bet"},
                         btn=LEFT_SEAT), HERO_CARDS, BOARD[:3])
    # 换手：hand_left 已被状态层清空，公共牌连续两帧读空才确认换手。
    tracker.observe(seat_gone("f4"), HERO_CARDS, [])
    tracker.observe(seat_gone("f5"), HERO_CARDS, [])
    rows = finished_rows(tracker)
    btn = [a for street, a in rows if a.get("seat_id") == "btn"]
    check(any(a.get("action") == "fold" and a.get("source") == "left"
              for a in btn),
          f"换手帧上仍应按水位补弃牌（source=left）：{btn}")
    check(not any(a.get("source") == "ledger" for a in btn),
          f"不能退回「离桌 = 弃牌」的 ledger 口径：{btn}")


def test_hand_end_finalize_keeps_check_rule_for_last_street() -> None:
    """无注时同理：手末结算也必须补过牌，不能补成弃牌。"""
    tracker = start()
    tracker.observe(flop("f3", btn=LEFT_SEAT), HERO_CARDS, BOARD[:3])
    tracker.observe(seat_gone("f4"), HERO_CARDS, [])
    tracker.observe(seat_gone("f5"), HERO_CARDS, [])
    rows = finished_rows(tracker)
    btn = [a for street, a in rows if a.get("seat_id") == "btn"]
    check(any(a.get("action") == "check" and a.get("source") == "left"
              for a in btn),
          f"无注时手末应补过牌：{btn}")
    check(not any(a.get("action") == "fold" for a in btn),
          f"无注时不应补弃牌：{btn}")


def test_hand_end_finalize_keeps_absent_silent() -> None:
    """暂离同理：换手帧结算最后一街时仍然一条动作都不补。"""
    tracker = start()
    tracker.observe(flop("f3", btn=ABSENT_SEAT), HERO_CARDS, BOARD[:3])
    tracker.observe(seat_gone("f4"), HERO_CARDS, [])
    tracker.observe(seat_gone("f5"), HERO_CARDS, [])
    rows = finished_rows(tracker)
    btn = [a for street, a in rows if a.get("seat_id") == "btn"]
    check(not btn, f"暂离者的最后一街也不补动作：{btn}")


def main() -> int:
    tests = [test_left_seat_folds_when_street_has_a_bet,
             test_left_seat_checks_when_street_has_no_bet,
             test_two_left_seats_follow_position_order,
             test_no_action_for_hero_all_in_or_acted_seat,
             test_absent_seat_gets_no_action,
             test_reviving_left_seat_revokes_only_left_actions,
             test_finalized_street_is_not_backfilled_twice,
             test_hand_end_finalize_keeps_left_rule_for_last_street,
             test_hand_end_finalize_keeps_check_rule_for_last_street,
             test_hand_end_finalize_keeps_absent_silent]
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
    print("\n✅ 离桌动作分流全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
