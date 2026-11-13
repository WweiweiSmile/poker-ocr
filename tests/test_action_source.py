#!/usr/bin/env python3
"""#22：每条行动都带证据源，`inferred` 仍是派生的兼容别名。

    nv tests/test_action_source.py

覆盖：
  · 名称区徽章 → source=badge，JSON 里不写 inferred（旧格式不变）；
  · 徽章漏读、从注额差反推 → source=bet_delta，inferred=True；
  · 街末对账补出的弃牌 → source=ledger，inferred=True；
  · 同一徽章的重复帧回填金额时，source/order_unknown 不丢；
  · 旧构造器只传 inferred 时按 inferred 反推 source，非法 source 直接报错。
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


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def state(frame_id: str, *, btn_bet: float = 0.0,
          btn_action: str | None = None, unknown: bool = False,
          missing: tuple[str, ...] = (), pot: float = 1.5) -> FrameState:
    players = [
        PlayerState("hero", name="me", position="LJ", stack=100.0,
                    bet=0.0, in_hand=True, status="playing"),
        PlayerState("sb", name="SB", position="SB", stack=100.0,
                    bet=0.5, in_hand=True, status="playing"),
        PlayerState("bb", name="BB", position="BB", stack=100.0,
                    bet=1.0, in_hand=True, status="playing"),
        PlayerState("btn", name="BTN", position="BTN", stack=100.0,
                    bet=btn_bet, bet_unknown=unknown, action=btn_action,
                    in_hand=True, status="playing"),
        PlayerState("utg", name="UTG", position="UTG", stack=100.0,
                    bet=0.0, in_hand=True, status="playing"),
    ]
    players = [p for p in players if p.seat_id not in missing]
    return FrameState(
        frame_id=frame_id, table_seats=9, players_in_hand=len(players),
        players=players, blinds=parse_blinds("1/2"), dealer="btn",
        hero_seat_id="hero", hero_turn=False,
        pot=pot, pot_before_street=0.0, street_bet_total=pot)


def actions_of(hand: dict) -> list[dict]:
    return [a for s in hand.get("streets") or []
            for a in s.get("actions") or []]


def test_badge_source() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1"), ["7c", "7s"], [])
    tracker.observe(state("f2", btn_bet=1.0, btn_action="call", pot=2.5),
                    ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    row = next((a for a in actions_of(hand)
                if a["seat_id"] == "btn" and a["action"] == "call"), None)
    check(row is not None, f"BTN 的 call 应落库：{actions_of(hand)}")
    if row is None:
        return
    check(row.get("source") == "badge", f"徽章动作应为 badge：{row}")
    # 正常手牌的旧格式不变：inferred 只在补出来时出现
    check("inferred" not in row, f"badge 动作不应写 inferred：{row}")
    check(row.get("order_unknown") is False, f"顺序应确定：{row}")


def test_bet_delta_source() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1"), ["7c", "7s"], [])
    tracker.observe(state("f2", btn_bet=1.0, pot=2.5), ["7c", "7s"], [])
    hand = tracker.take_current()
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    row = next((a for a in actions_of(hand)
                if a["seat_id"] == "btn" and a["action"] == "call"), None)
    check(row is not None, f"注额差应反推出 call：{actions_of(hand)}")
    if row is None:
        return
    check(row.get("source") == "bet_delta", f"反推动作应为 bet_delta：{row}")
    check(row.get("inferred") is True, f"bet_delta 必须 inferred：{row}")


def test_ledger_fold_source() -> None:
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1"), ["7c", "7s"], [])
    tracker.observe(state("f2", missing=("btn",)), ["7c", "7s"], [])
    tracker.finish_current()
    hand = tracker.take_finished()
    check(hand is not None, "应产出已结束手牌")
    if hand is None:
        return
    rows = actions_of(hand)
    check(all(a.get("source") for a in rows),
          f"每条行动都应有 source：{rows}")
    fold = next((a for a in rows if a["seat_id"] == "btn"
                 and a["action"] == "fold"), None)
    check(fold is not None, f"街末应补出 BTN 的 fold：{rows}")
    if fold is None:
        return
    check(fold.get("source") == "ledger", f"街末补全应为 ledger：{fold}")
    check(fold.get("inferred") is True, f"ledger 必须 inferred：{fold}")


def test_backfill_keeps_source() -> None:
    """徽章先出现、金额后到：同一条记录回填 amount，source 不能丢。"""
    tracker = LiveHandTracker(confirm_frames=1)
    tracker.observe(state("f1"), ["7c", "7s"], [])
    tracker.observe(state("f2", btn_bet=1.0, btn_action="call", unknown=True),
                    ["7c", "7s"], [])
    tracker.observe(state("f3", btn_bet=1.0, btn_action="call", pot=2.5),
                    ["7c", "7s"], [])
    hand = tracker.take_current()
    if hand is None:
        check(False, "应产出当前手牌")
        return
    rows = [a for a in actions_of(hand)
            if a["seat_id"] == "btn" and a["action"] == "call"]
    check(len(rows) == 1, f"同一徽章只应有一条：{rows}")
    if not rows:
        return
    check(rows[0].get("amount") == 1.0, f"金额应回填：{rows[0]}")
    check(rows[0].get("source") == "badge", f"回填后 source 应保留：{rows[0]}")


def test_legacy_constructors() -> None:
    plain = ActionRecord("preflop", "a", "A", "BB", "call", 1.0)
    check(plain.source == "badge", f"旧构造器默认为 badge：{plain.source}")
    check(plain.inferred is False, "badge 不是 inferred")
    legacy = ActionRecord("turn", "a", "A", "BB", "fold", inferred=True)
    check(legacy.source == "ledger", f"只传 inferred 应反推 ledger：{legacy.source}")
    check(legacy.inferred is True, "ledger 必须是 inferred")
    try:
        ActionRecord("flop", "a", "A", "BB", "check", source="guess")
    except ValueError:
        pass
    else:
        check(False, "非法 source 应抛 ValueError")


def main() -> int:
    tests = [test_badge_source, test_bet_delta_source, test_ledger_fold_source,
             test_backfill_keeps_source, test_legacy_constructors]
    print("── 行动证据源 ────────────────────────────────")
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
    print("\n✅ 行动证据源全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
