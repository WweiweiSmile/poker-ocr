#!/usr/bin/env python3
"""#24：头像弃牌按首次观测进入行动线。

    nv tests/test_avatar_fold.py

覆盖：
  · 连续 2 帧弃牌 → 只落一条 fold，source=avatar_fold，证据取第一帧；
  · 位置按首次观测插入，落在它前后的两个动作之间，不是街末；
  · 单帧弃牌不提交（中途恢复 = 没有弃牌）；
  · 街末/手末不再为已提交的 avatar_fold 追加重复 fold；
  · 换街那一帧才凑够 2 帧的，也按第一帧的位置确认；
  · 已弃牌座位的残留 bet 仍不推成 bet/raise。
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


def state(frame_id: str, *, folded: tuple[str, ...] = (),
          actions: dict[str, tuple[float, str]] | None = None,
          bets: dict[str, float] | None = None,
          missing: tuple[str, ...] = ()) -> FrameState:
    """五个座位的一帧；`folded` 里的座位头像显示弃牌。"""
    actions = actions or {}
    bets = bets or {}
    base = [
        ("hero", "me", "LJ", 0.0),
        ("sb", "SB", "SB", 0.5),
        ("bb", "BB", "BB", 1.0),
        ("btn", "BTN", "BTN", 0.0),
        ("utg", "UTG", "UTG", 0.0),
    ]
    players = []
    for sid, name, pos, bet in base:
        if sid in missing:
            continue
        act = actions.get(sid)
        players.append(PlayerState(
            sid, name=name, position=pos, stack=100.0 - bet,
            bet=bets.get(sid, bet),
            action=act[1] if act else None,
            fold=True if sid in folded else False,
            in_hand=True,
            status="folded" if sid in folded else "playing"))
    return FrameState(
        frame_id=frame_id, table_seats=9, players_in_hand=len(players),
        players=players, blinds=parse_blinds("1/2"), dealer="btn",
        hero_seat_id="hero", hero_turn=False,
        pot=1.5, pot_before_street=0.0, street_bet_total=1.5)


def actions_of(hand: dict) -> list[dict]:
    return [a for s in hand.get("streets") or []
            for a in s.get("actions") or []]


def street_of(hand: dict, seat_id: str, action: str) -> str | None:
    for s in hand.get("streets") or []:
        if any(a.get("seat_id") == seat_id and a.get("action") == action
               for a in s.get("actions") or []):
            return s.get("street")
    return None


def test_two_frames_place_fold_between_actions() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", actions={"utg": (1.0, "call")}),
                    ["7c", "7s"], [])
    tracker.observe(state("f2", folded=("btn",)), ["7c", "7s"], [])
    tracker.observe(state("f3", folded=("btn",),
                          actions={"bb": (2.0, "raise")}, bets={"bb": 2.0}),
                    ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    rows = actions_of(hand)
    folds = [a for a in rows if a["seat_id"] == "btn"
             and a["action"] == "fold"]
    check(len(folds) == 1, f"两帧弃牌只应落一条 fold：{rows}")
    if not folds:
        return
    fold = folds[0]
    check(fold.get("source") == "avatar_fold",
          f"头像弃牌应为 avatar_fold：{fold}")
    check("inferred" not in fold, f"avatar_fold 不是推断动作：{fold}")
    check((fold.get("evidence") or {}).get("frame_id") == "f2",
          f"证据应取第一次看到的帧 f2：{fold}")
    order = [(a["seat_id"], a["action"]) for a in rows
             if (a["seat_id"], a["action"]) in {
                 ("utg", "call"), ("btn", "fold"), ("bb", "raise")}]
    check(order == [("utg", "call"), ("btn", "fold"), ("bb", "raise")],
          f"弃牌应落在第一次观测的位置：{order}")


def test_single_frame_is_not_committed() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", actions={"utg": (1.0, "call")}),
                    ["7c", "7s"], [])
    tracker.observe(state("f2", folded=("btn",)), ["7c", "7s"], [])
    # 图标/文案恢复：那一帧不是弃牌，候选作废。
    tracker.observe(state("f3"), ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    folds = [a for a in actions_of(hand)
             if a["seat_id"] == "btn" and a["action"] == "fold"]
    check(not folds, f"单帧弃牌不应提交：{folds}")


def test_street_end_does_not_duplicate_committed_fold() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", actions={"utg": (1.0, "call")}),
                    ["7c", "7s"], [])
    tracker.observe(state("f2", folded=("btn",)), ["7c", "7s"], [])
    tracker.observe(state("f3", folded=("btn",)), ["7c", "7s"], [])
    # 换街：BTN 已经从在局集合里消失，街末不能再补一条 ledger fold。
    tracker.observe(state("flop", missing=("btn",)),
                    ["7c", "7s"], ["Ah", "Kd", "7c"])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    folds = [a for a in actions_of(hand)
             if a["seat_id"] == "btn" and a["action"] == "fold"]
    check(len(folds) == 1, f"街末不应重复补 fold：{folds}")
    if folds:
        check(folds[0].get("source") == "avatar_fold",
              f"保留头像来源，不应被 ledger 覆盖：{folds[0]}")


def test_confirm_on_street_change_uses_first_frame() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", actions={"utg": (1.0, "call")}),
                    ["7c", "7s"], [])
    # 上一街最后一帧第一次看到弃牌，这一帧换街、头像仍是弃牌 → 第 2 帧。
    tracker.observe(state("f2", folded=("btn",)), ["7c", "7s"], [])
    tracker.observe(state("flop", folded=("btn",)),
                    ["7c", "7s"], ["Ah", "Kd", "7c"])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    folds = [a for a in actions_of(hand)
             if a["seat_id"] == "btn" and a["action"] == "fold"]
    check(len(folds) == 1, f"换街帧应补上确认：{folds}")
    if not folds:
        return
    check(street_of(hand, "btn", "fold") == "preflop",
          f"应按第一帧记在翻前：{street_of(hand, 'btn', 'fold')}")
    check(folds[0].get("source") == "avatar_fold", f"来源不对：{folds[0]}")
    check((folds[0].get("evidence") or {}).get("frame_id") == "f2",
          f"证据应取第一帧：{folds[0]}")


def test_residual_bet_after_fold_is_not_a_raise() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1", actions={"utg": (1.0, "call")}),
                    ["7c", "7s"], [])
    tracker.observe(state("f2", folded=("btn",)), ["7c", "7s"], [])
    tracker.observe(state("f3", folded=("btn",), bets={"btn": 20.0}),
                    ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    btn = [a for a in actions_of(hand) if a["seat_id"] == "btn"]
    check(not any(a["action"] in ("bet", "raise") for a in btn),
          f"已弃牌座位的残留 bet 不能推成加注：{btn}")


def main() -> int:
    tests = [test_two_frames_place_fold_between_actions,
             test_single_frame_is_not_committed,
             test_street_end_does_not_duplicate_committed_fold,
             test_confirm_on_street_change_uses_first_frame,
             test_residual_bet_after_fold_is_not_a_raise]
    print("── 头像弃牌进行动线 ──────────────────────────")
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
    print("\n✅ 头像弃牌进行动线全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
