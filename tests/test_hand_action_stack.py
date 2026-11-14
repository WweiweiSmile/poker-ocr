#!/usr/bin/env python3
"""行动记录带「观测后手筹码」+ 离线重放 SPR（#35 / 规格 #33 B 段）。

    nv tests/test_hand_action_stack.py

口径（规格 #33 B 段，逐条对应）：

  1. 归档只存事实：每条行动记录带「行动时该座位后手筹码」（BB，与 amount 同
     单位）。观测到就写数字，**不补 99、不补 0、不猜**。
  2. 没读到就写 null：推断来源（bet_delta / ledger / avatar_fold 补记）、帧里
     没读到、盲注补记 —— 一律 null。
  3. 旧归档没有该字段 → 读回按 unknown（「无筹码」），绝不回落 99。
  4. 离线重放用观测值算「行动前有效筹码」，算不出 → spr=None + spr_missing。

新套件进全量清单由 lead 统一追加。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from poker_ocr.frame_state import FrameState, PlayerState  # noqa: E402
from poker_ocr.hand_track import LiveHandTracker  # noqa: E402
from poker_ocr.parse import parse_blinds  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def _state(frame_id: str, *, hero: tuple, sb: tuple, bb: tuple,
           pot: float, street_bet_total: float,
           pot_before_street: float = 4.0) -> FrameState:
    """一帧三人在局：hero=BTN，SB/BB 各一个座位。

    每个座位是 `(stack, bet, action)` 或 `(stack, bet, action, all_in)`。
    """

    def player(seat_id, name, position, spec):
        stack, bet, action = spec[:3]
        all_in = bool(spec[3]) if len(spec) > 3 else False
        return PlayerState(seat_id, name=name, position=position, stack=stack,
                           bet=bet, action=action, in_hand=True, all_in=all_in,
                           status="playing")

    return FrameState(
        frame_id=frame_id, table_seats=9, players_in_hand=3,
        players=[
            player("hero", "me", "BTN", hero),
            player("sb", "SB", "SB", sb),
            player("bb", "BB", "BB", bb),
        ],
        blinds=parse_blinds("1/2"), dealer="hero", hero_seat_id="hero",
        pot=pot, pot_before_street=pot_before_street,
        street_bet_total=street_bet_total,
    )


def _run(states: list[tuple[FrameState, list[str]]] | list[FrameState]) -> dict | None:
    """喂若干帧（可带牌面）→ 落库的 hand dict。"""
    tracker = LiveHandTracker(confirm_frames=1)
    for item in states:
        state, board = item if isinstance(item, tuple) else (item, [])
        tracker.observe(state, ["7c", "7s"], board)
    return tracker.take_current()


def _actions(hand: dict) -> list[dict]:
    return [a for street in hand.get("streets") or []
            for a in street.get("actions") or []]


def _by_seat(hand: dict, seat_id: str) -> list[dict]:
    return [a for a in _actions(hand) if a.get("seat_id") == seat_id]


# ─────────────────── 1. 观测到后手 → 落库有值（hero + 对手） ───────────────────


def test_observed_stack_is_recorded() -> None:
    hand = _run([
        _state("f1", hero=(100.0, 0.0, None), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=1.5, street_bet_total=1.0),
        _state("f2", hero=(97.0, 3.0, "raise"), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=4.5, street_bet_total=3.0),
        _state("f3", hero=(97.0, 3.0, None), sb=(197.0, 3.0, "call"),
               bb=(200.0, 1.0, None), pot=7.5, street_bet_total=4.0),
    ])
    check(hand is not None, "应产出当前手牌")
    if hand is None:
        return
    hero_actions = _by_seat(hand, "hero")
    sb_actions = _by_seat(hand, "sb")
    check(len(hero_actions) == 1, f"hero 应有一条行动，实际 {hero_actions}")
    check(len(sb_actions) == 1, f"SB 应有一条行动，实际 {sb_actions}")
    if hero_actions:
        check(hero_actions[0].get("stack") == 97.0,
              f"hero 加注时应落库观测后手 97.0，实际 {hero_actions[0].get('stack')!r}")
        check("stack" in hero_actions[0], "观测行动也必须写 stack 键")
    if sb_actions:
        check(sb_actions[0].get("stack") == 197.0,
              f"SB 跟注时应落库观测后手 197.0，实际 {sb_actions[0].get('stack')!r}")


# ─────────────────── 2. 读不到 / 推断 → null（不补 99、不补 0） ───────────────────


def test_unread_stack_is_null() -> None:
    hand = _run([
        _state("f1", hero=(100.0, 0.0, None), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=1.5, street_bet_total=1.0),
        # 徽章读到了，筹码这一帧没读到
        _state("f2", hero=(None, 3.0, "raise"), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=4.5, street_bet_total=3.0),
    ])
    hero_actions = _by_seat(hand or {}, "hero")
    check(len(hero_actions) == 1, f"hero 应有一条行动，实际 {hero_actions}")
    if hero_actions:
        check(hero_actions[0].get("stack") is None,
              f"帧里没读到筹码 → null，实际 {hero_actions[0].get('stack')!r}")
        check("stack" in hero_actions[0],
              "没读到也要写 stack 键（null），不能缺键")


def test_bare_zero_stack_is_not_a_fact() -> None:
    hand = _run([
        _state("f1", hero=(100.0, 0.0, None), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=1.5, street_bet_total=1.0),
        # 筹码图标被读成裸 0（已知误读），且没有全下 → 不能当 0 存
        _state("f2", hero=(97.0, 3.0, "raise"), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=4.5, street_bet_total=3.0),
        _state("f3", hero=(97.0, 3.0, None), sb=(0.0, 3.0, "call"),
               bb=(200.0, 1.0, None), pot=7.5, street_bet_total=4.0),
    ])
    sb_actions = _by_seat(hand or {}, "sb")
    check(len(sb_actions) == 1, f"SB 应有一条行动，实际 {sb_actions}")
    if sb_actions:
        check(sb_actions[0].get("stack") is None,
              f"裸 0 且没全下 → null，实际 {sb_actions[0].get('stack')!r}")


def test_all_in_zero_stack_is_kept() -> None:
    hand = _run([
        _state("f1", hero=(100.0, 0.0, None), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=1.5, street_bet_total=1.0),
        _state("f2", hero=(97.0, 3.0, "raise"), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=4.5, street_bet_total=3.0),
        # 全下：0 是真事实
        _state("f3", hero=(97.0, 3.0, None), sb=(0.0, 3.0, "call", True),
               bb=(200.0, 1.0, None), pot=7.5, street_bet_total=4.0),
    ])
    sb_actions = _by_seat(hand or {}, "sb")
    check(len(sb_actions) == 1, f"SB 应有一条行动，实际 {sb_actions}")
    if sb_actions:
        check(sb_actions[0].get("stack") == 0.0,
              f"全下后手 0 是真事实，实际 {sb_actions[0].get('stack')!r}")


def test_badge_amount_late_keeps_inferred_action_null() -> None:
    """bet_delta 的记录后来补上徽章金额，也不能顺带把观测筹码写进去。

    这是审查 P1：那支分支保留 `source=prev.source`，若同时写 stack_after，
    就会产出 "source":"bet_delta" + "stack":194.0 + inferred 的混血记录。
    """
    hand = _run([
        _state("f1", hero=(100.0, 0.0, None), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=1.5, street_bet_total=1.0),
        # 徽章没出来，从下注额反推成 bet 3（source=bet_delta）
        _state("f2", hero=(100.0, 0.0, None), sb=(197.0, 3.0, None),
               bb=(200.0, 1.0, None), pot=4.5, street_bet_total=3.0),
        # 徽章出现、金额抬到 6：走「同一动作金额后补」分支
        _state("f3", hero=(100.0, 0.0, None), sb=(194.0, 6.0, "raise"),
               bb=(200.0, 1.0, None), pot=7.5, street_bet_total=6.0),
    ])
    sb_actions = _by_seat(hand or {}, "sb")
    check(len(sb_actions) == 1, f"SB 应只有一条行动，实际 {sb_actions}")
    if sb_actions:
        action = sb_actions[0]
        check(action.get("source") == "bet_delta",
              f"来源仍是反推，实际 {action.get('source')!r}")
        check(action.get("stack") is None,
              f"反推记录补金额后仍不得带观测筹码，实际 {action.get('stack')!r}")


def test_inferred_action_has_no_stack() -> None:
    # 徽章没读到，从下注额反推成加注（source=bet_delta）：行动本身不是观测，
    # 后手一律 null —— 即使这一帧筹码读到了。
    hand = _run([
        _state("f1", hero=(100.0, 0.0, None), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=1.5, street_bet_total=1.0),
        _state("f2", hero=(100.0, 0.0, None), sb=(197.0, 3.0, None),
               bb=(200.0, 1.0, None), pot=4.5, street_bet_total=3.0),
    ])
    sb_actions = _by_seat(hand or {}, "sb")
    check(len(sb_actions) == 1, f"SB 应有一条反推行动，实际 {sb_actions}")
    if sb_actions:
        check(sb_actions[0].get("source") == "bet_delta",
              f"应标成 bet_delta，实际 {sb_actions[0].get('source')!r}")
        check(sb_actions[0].get("stack") is None,
              f"推断来源 → null，实际 {sb_actions[0].get('stack')!r}")



# ─────────────────── 3. 离线重放：记录 → ActionEvent（带 SPR） ───────────────────

from hand_action_events import action_stack, replay_hand  # noqa: E402

# 一手翻前加注/跟注、翻牌与转牌各一注一跟的两人池。筹码与底池都是整数，
# 期望 SPR 可以手算（见各用例）。
HAND: dict = {
    "type": "hand", "hand_id": "replay-1", "session": 1,
    "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.0},
    "board": ["Jc", "8d", "Kd", "Qc", "Ad"],
    "hero_cards": ["As", "Th"], "hero_position": "BTN", "table_size": 2,
    "hero_seat_id": "hero", "pot": 33.5,
    "streets": [
        {"street": "preflop", "pot_start": None, "actions": [
            {"seat_id": "hero", "name": "me", "position": "BTN",
             "action": "raise", "amount": 3.0, "all_in": False,
             "is_hero": True, "source": "badge", "stack": 97.0},
            {"seat_id": "upR", "name": "V", "position": "BB",
             "action": "call", "amount": 3.0, "all_in": False,
             "is_hero": False, "source": "badge", "stack": 97.0},
        ]},
        {"street": "flop", "pot_start": 7.5, "actions": [
            {"seat_id": "upR", "name": "V", "position": "BB",
             "action": "bet", "amount": 4.0, "all_in": False,
             "is_hero": False, "source": "badge", "stack": 93.0},
            {"seat_id": "hero", "name": "me", "position": "BTN",
             "action": "call", "amount": 4.0, "all_in": False,
             "is_hero": True, "source": "badge", "stack": 93.0},
        ]},
        {"street": "turn", "pot_start": 15.5, "actions": [
            {"seat_id": "upR", "name": "V", "position": "BB",
             "action": "bet", "amount": 9.0, "all_in": False,
             "is_hero": False, "source": "badge", "stack": 84.0},
            {"seat_id": "hero", "name": "me", "position": "BTN",
             "action": "call", "amount": 9.0, "all_in": False,
             "is_hero": True, "source": "badge", "stack": 84.0},
        ]},
    ],
}


def _without_stacks(hand: dict) -> dict:
    """旧归档形状：action 里没有 stack 键。"""
    out = json.loads(json.dumps(hand))
    for street in out["streets"]:
        for action in street["actions"]:
            action.pop("stack", None)
    return out


def _close(a, b, tol: float = 1e-6) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def test_replay_computes_spr_from_observations() -> None:
    steps = replay_hand(HAND)
    check([s.street for s in steps] == ["flop", "flop", "turn", "turn"],
          f"应逐行动重放翻后 4 个决策点，实际 {[s.street for s in steps]}")
    check(all(s.spr_missing is None for s in steps),
          f"数据齐全时不该标 spr_missing，实际 {[s.spr_missing for s in steps]}")
    expected = [
        # 翻牌 bet（upR）：行动前有效筹码 97（两家翻前跟注后的后手），底池 7.5
        ("flop", "upR", 97.0 / 7.5),
        # 翻牌 call（hero）：upR 下注后剩 93，底池 7.5+4
        ("flop", "hero", 93.0 / 11.5),
        # 转牌 bet（upR）：两家翻牌后都剩 93，底池 15.5
        ("turn", "upR", 93.0 / 15.5),
        # 转牌 call（hero）：upR 下注后剩 84，底池 15.5+9
        ("turn", "hero", 84.0 / 24.5),
    ]
    for step, (street, seat, spr) in zip(steps, expected):
        check(step.street == street and step.event.seat_id == seat,
              f"顺序应为 {street}/{seat}，实际 {step.street}/{step.event.seat_id}")
        check(_close(step.event.spr, spr),
              f"{street}/{seat} SPR 应为 {spr:.4f}，实际 {step.event.spr!r}")
        check(_close(step.event.pot, {("flop", "upR"): 7.5,
                                      ("flop", "hero"): 11.5,
                                      ("turn", "upR"): 15.5,
                                      ("turn", "hero"): 24.5}[(street, seat)]),
              f"{street}/{seat} 底池应为行动前值，实际 {step.event.pot!r}")


def test_replay_never_falls_back_to_99() -> None:
    steps = replay_hand(_without_stacks(HAND))
    check(len(steps) == 4, f"旧归档仍应重放出 4 个决策点，实际 {len(steps)}")
    for step in steps:
        check(step.event.spr is None,
              f"缺筹码时 spr 必须是 None，不能回落 99，实际 {step.event.spr!r}")
        check(step.spr_missing == "no_stack",
              f"缺筹码应标 spr_missing=no_stack，实际 {step.spr_missing!r}")


def test_check_first_action_uses_own_stack() -> None:
    """某座本手第一动是 check（amount=None）时，也要用本帧观测算 SPR（审查 P1）。"""
    hand = {
        "type": "hand", "hand_id": "replay-check", "session": 1,
        "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.0},
        "board": ["Jc", "8d", "Kd"], "hero_cards": ["As", "Th"],
        "hero_position": "BTN", "table_size": 2, "hero_seat_id": "hero",
        "hero_stack_bb": 100.0, "pot": 7.5,
        "streets": [{"street": "flop", "pot_start": 7.5, "actions": [
            {"seat_id": "upR", "name": "V", "position": "BB",
             "action": "check", "amount": None, "all_in": False,
             "is_hero": False, "source": "badge", "stack": 90.0},
        ]}],
    }
    steps = replay_hand(hand)
    check(len(steps) == 1, f"应重放出 1 个决策点，实际 {len(steps)}")
    if steps:
        step = steps[0]
        check(step.spr_missing is None,
              f"check 自带观测后手，不该标缺数据，实际 {step.spr_missing!r}")
        check(_close(step.event.spr, 90.0 / 7.5),
              f"SPR 应为 90/7.5=12.0，实际 {step.event.spr!r}")


def test_replay_marks_missing_pot() -> None:
    hand = json.loads(json.dumps(HAND))
    for street in hand["streets"]:
        street["pot_start"] = None
    steps = replay_hand(hand)
    check(len(steps) == 4, f"仍应重放出 4 个决策点，实际 {len(steps)}")
    for step in steps:
        check(step.event.spr is None,
              f"底池缺失时 spr 必须是 None，实际 {step.event.spr!r}")
        check(step.spr_missing in ("no_pot", "no_stack"),
              f"应标出缺数据原因，实际 {step.spr_missing!r}")
    check(any(s.spr_missing == "no_pot" for s in steps),
          "应能标出 no_pot")


def test_action_stack_reads_old_and_new_shapes() -> None:
    check(action_stack({"action": "bet", "stack": 93.0}) == 93.0,
          "新归档应读回观测后手")
    check(action_stack({"action": "bet", "stack": 0.0}) == 0.0,
          "全下后手 0 要读回 0，不能被当成缺失")
    check(action_stack({"action": "bet", "stack": None}) is None,
          "显式 null 应读回 None")
    check(action_stack({"action": "bet"}) is None,
          "旧归档没有 stack 键 → unknown")
    check(action_stack({"action": "bet", "stack": "88"}) is None,
          "非数字读数不能当筹码")


def test_old_archive_actions_display_no_stack() -> None:
    """旧形状（连 source 都没有）读回不崩，且明确是「无筹码」。"""
    hand = _without_stacks(HAND)
    for street in hand["streets"]:
        for action in street["actions"]:
            action.pop("source", None)
    steps = replay_hand(hand)
    check(len(steps) == 4, f"旧形状应照常重放，实际 {len(steps)}")
    for step in steps:
        check(step.event.spr is None and step.spr_missing == "no_stack",
              f"旧归档应显示无筹码，实际 {step.event.spr!r}/{step.spr_missing!r}")


# 主仓里的旧归档（未跟踪数据，不在 worktree 里；有就跑，没有就跳过）
OLD_ARCHIVE = Path("/Users/weiweigod/codes/poker-ocr/hands/1023.hands.json")


def test_real_old_archive_is_readable() -> None:
    """旧归档（109 手、action 里没有 stack 键）读得回、显示无筹码（#33 验收③）。"""
    if not OLD_ARCHIVE.exists():
        print(f"  · 跳过（{OLD_ARCHIVE} 不在本机）")
        return
    hands = json.loads(OLD_ARCHIVE.read_text("utf-8"))
    check(len(hands) == 109, f"旧归档应是 109 手，实际 {len(hands)}")
    steps = [step for hand in hands for step in replay_hand(hand)]
    check(bool(steps), "旧归档应能重放出决策点")
    for step in steps:
        check(step.event.spr is None,
              f"{step.street}/{step.event.seat_id} 旧归档没有筹码，不能算出 SPR")
        check(step.spr_missing in ("no_stack", "no_pot"),
              f"应标出缺数据原因，实际 {step.spr_missing!r}")
        check(step.event.spr != 99.0, "旧归档绝不能回落 99")
    check(all(action_stack(a) is None for hand in hands
              for street in hand.get("streets") or []
              for a in street.get("actions") or []),
          "旧归档的每条行动都应读成 unknown")


def test_tracker_output_replays_end_to_end() -> None:
    """live 落库的 hand dict 直接喂重放：观测值一路带到 SPR，与实时口径一致。"""
    flop = ["Jc", "8d", "Kd"]
    hand = _run([
        _state("f1", hero=(100.0, 0.0, None), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=1.5, street_bet_total=1.0),
        _state("f2", hero=(97.0, 3.0, "raise"), sb=(200.0, 0.5, None),
               bb=(200.0, 1.0, None), pot=4.5, street_bet_total=3.0),
        _state("f3", hero=(97.0, 3.0, None), sb=(197.0, 3.0, "call"),
               bb=(200.0, 1.0, None), pot=7.5, street_bet_total=4.0),
        (_state("f4", hero=(97.0, 0.0, None), sb=(197.0, 0.0, None),
                bb=(200.0, 0.0, None), pot=7.5, street_bet_total=0.0,
                pot_before_street=7.5), flop),
        (_state("f5", hero=(97.0, 0.0, None), sb=(193.0, 4.0, "bet"),
                bb=(200.0, 0.0, None), pot=11.5, street_bet_total=4.0,
                pot_before_street=7.5), flop),
        (_state("f6", hero=(93.0, 4.0, "call"), sb=(193.0, 4.0, None),
                bb=(200.0, 0.0, None), pot=15.5, street_bet_total=4.0,
                pot_before_street=7.5), flop),
    ])
    check(hand is not None, "应产出当前手牌")
    if not hand:
        return
    streets = [s["street"] for s in hand.get("streets") or []]
    check(streets == ["preflop", "flop"], f"应落库两街，实际 {streets}")
    steps = replay_hand(hand)
    check([(s.street, s.event.seat_id, s.event.action) for s in steps]
          == [("flop", "sb", "bet"), ("flop", "hero", "call")],
          f"应重放出 flop 两个决策点，实际 {[(s.street, s.event.seat_id) for s in steps]}")
    if len(steps) == 2:
        # sb 下注前：hero 后手 97、sb 197 → 有效 97；底池 7.5
        check(abs((steps[0].spr or 0.0) - 97.0 / 7.5) < 1e-9,
              f"flop/sb 的 SPR 应是 97/7.5，实际 {steps[0].spr}")
        # hero 跟注前：sb 已投入 4（观测 193 + 4 = 197）、hero 97；底池 11.5
        check(abs((steps[1].spr or 0.0) - 97.0 / 11.5) < 1e-9,
              f"flop/hero 的 SPR 应是 97/11.5，实际 {steps[1].spr}")
        check(all(step.spr_missing is None for step in steps),
              f"这手数据齐全，不该缺：{[s.spr_missing for s in steps]}")
        check(all(step.event.spr != 99.0 for step in steps),
              "不该出现 99 哨兵")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    if FAILS:
        for msg in FAILS:
            print(f"❌ {msg}")
        print(f"\n{len(FAILS)} 项失败")
        return 1
    print(f"✅ {len(tests)} 个用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
