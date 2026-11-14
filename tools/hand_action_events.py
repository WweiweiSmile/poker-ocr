#!/usr/bin/env python3
"""hands 归档 → 逐决策的 ActionEvent（带行动前有效筹码与 SPR）。

    nv tools/hand_action_events.py hands/1023.hands.json
    nv tools/hand_action_events.py hands/1023.hands.json --hand 20261111-e8194f46f8
    nv tools/hand_action_events.py hands/1023.hands.json --missing-only

只读：不写文件、不联网、不调模型。

## 为什么要有它

实时链路的 SPR 来自「当前帧的筹码」（`field_ranges._stack_at_event`：当前后手
+ 本手总投入 − 行动前已投入）。hands 归档没有「当前帧」，只有每条行动上观测到
的后手（#35 新增的 `stack` 键）。离线重放只能用这些观测值自己算，算不出来就写
`None` —— **绝不回落到 99**（#29 定下的「不写假值」）。

## 口径

行动前有效筹码（决策点 E、在局座位 S）：

    该座在 E 之前**最近一次观测到的后手** − 那次观测之后又投进去的钱

筹码只在行动时移动，同一座位两次行动之间后手不变，所以这条就是它行动前的
真实后手。观测值本身是行动**之后**的读数（徽章帧上筹码已经扣掉，见
`logs/live.mac.jsonl` 的 2026-11-11T20:27:38.099 帧：raise 9 → 97→88），
所以对**正在行动的那个人**，用「本动观测 + 本动投入的增量」回推：

    本动观测 + (amount − 该座本街之前已投入)

「观测」有两种，都是归档里的既有事实：

  · 每条行动上的 `stack`（#35 新增，徽章帧读到的后手，是行动**之后**的读数）；
  · 开手筹码（`start_stacks[].stack` / `hero_stack_bb`，手牌记录里「含前注/盲注
    之前的值」）—— 减去该座已交的前注/盲注，就是开手那一刻的后手。没行动过的
    座位靠它才有值；旧归档没有这两个键时只能算 unknown。

在局座位 = 该街起始在局（`street_active`；旧归档没有这个键时退化成「这手出现
过的所有人」）减去在 E 之前已经弃牌的。任一在局座位没有可用观测、或底池起始
值缺失 → `spr=None`，并在 `ReplayStep.spr_missing` 上写原因：

  · `no_stack` —— 至少一个在局座位没有观测后手（推断补记、帧没读到、旧归档）
  · `no_pot`   —— 该街起始底池缺失或非正

底池：`pot_start`（该街起始底池）+ 本街已发生的投入增量（amount 是「本街加到
总额」，增量 = amount − 该座本街之前已投入）。起始值没读到时用前面各街累加出来
的底池兜底（与 `field_ranges.build_events` 同口径）。

翻前不产生事件：先验已经覆盖「翻前用哪些牌」，重复计入等于算两次（与实时链路
一致）。牌面不足该街张数时不产生事件 —— 没有牌面就没有这条事实。

## 边界

本模块只负责 SPR 这条线：ActionEvent 的 `street/board/seat_id/action/all_in/
amount/pot/spr` 是事实，其余上下文标志位（facing_bet、raises_before、
players_behind、is_donk…）**不重建**，保持 dataclass 缺省。需要完整决策上下文
时请走 `field_ranges.build_events(HandSnapshot)` —— 那需要一帧真实快照，归档里
没有。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr import range_flow  # noqa: E402

# 缺数据原因（写进 ReplayStep.spr_missing 与报告，别用 99 冒充）
SPR_NO_STACK = "no_stack"
SPR_NO_POT = "no_pot"

POSTFLOP = ("flop", "turn", "river")
_BOARD_LEN = {"flop": 3, "turn": 4, "river": 5}


@dataclass(frozen=True)
class ReplayStep:
    """一个决策点：ActionEvent + 它的有效筹码怎么来的（或缺什么）。"""

    street: str
    index: int                     # 该街 actions 里的下标
    event: range_flow.ActionEvent
    effective: float | None        # 行动前有效筹码（BB）
    spr_missing: str | None        # None = 算出来了；否则见 SPR_* 常量

    @property
    def spr(self) -> float | None:
        return self.event.spr

    def label(self) -> str:
        """报告用的一格：算不出来就明说缺什么，不写 99。"""
        if self.spr is None:
            return f"无筹码({self.spr_missing})"
        return f"{self.spr:.2f}"


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def action_stack(action: dict) -> float | None:
    """读回一条行动的观测后手（BB）。

    旧归档没有 `stack` 键 → None（unknown），**绝不回落 99**；显式 null → None；
    非数字（字符串、布尔）不当筹码。`0.0` 是合法读数（全下后手为 0）。
    """
    return _number(action.get("stack"))


def _seat(action: dict, hero_seat: str | None) -> str | None:
    seat = action.get("seat_id")
    if seat:
        return str(seat)
    return hero_seat if action.get("is_hero") else None


def _hero_seat(hand: dict) -> str | None:
    seat = hand.get("hero_seat_id")
    if seat:
        return str(seat)
    for street in hand.get("streets") or []:
        for action in street.get("actions") or []:
            if action.get("is_hero") and action.get("seat_id"):
                return str(action["seat_id"])
    return None


def _all_seats(hand: dict, hero_seat: str | None) -> list[str]:
    """这手出现过的所有座位（在局判定的兜底集合）。"""
    seats: list[str] = []

    def add(seat) -> None:
        if seat and str(seat) not in seats:
            seats.append(str(seat))

    add(hero_seat)
    for street in hand.get("streets") or []:
        for action in street.get("actions") or []:
            add(action.get("seat_id"))
    for street_seats in (hand.get("street_active") or {}).values():
        for seat in street_seats or []:
            add(seat)
    for key in ("seat_meta", "shows", "all_ins"):
        for row in hand.get(key) or []:
            if isinstance(row, dict):
                add(row.get("seat_id"))
    return seats


def _live_seats(street: str, street_active: dict, all_seats: list[str],
                folded: set[str]) -> set[str]:
    """这条街还在局的人：街起始在局 − 之前已弃牌。"""
    start = street_active.get(street)
    seats = {str(s) for s in start} if start else set(all_seats)
    return seats - folded


def _positions(hand: dict) -> dict[str, str]:
    """座位 → 位置（`seat_meta` 优先，再用动作里带的位置兜底）。"""
    out: dict[str, str] = {}
    for row in hand.get("seat_meta") or []:
        if isinstance(row, dict) and row.get("seat_id") and row.get("position"):
            out[str(row["seat_id"])] = str(row["position"])
    for street in hand.get("streets") or []:
        for action in street.get("actions") or []:
            seat = action.get("seat_id")
            if seat and action.get("position") and str(seat) not in out:
                out[str(seat)] = str(action["position"])
    return out


def _posted(hand: dict, positions: dict[str, str]) -> dict[str, float]:
    """开手就投进去的钱：前注（每人）+ 小盲/大盲。"""
    blinds = hand.get("blinds") or {}
    ante = _number(blinds.get("ante")) or 0.0
    sb = _number(blinds.get("sb")) or 0.0
    bb = _number(blinds.get("bb")) or 0.0
    out: dict[str, float] = {}
    for seat, position in positions.items():
        posted = ante
        if position == "SB":
            posted += sb
        elif position == "BB":
            posted += bb
        out[seat] = posted
    return out


def _opening(hand: dict) -> dict[str, float]:
    """开手筹码（含前注/盲注之前的值）：`start_stacks` + `hero_stack_bb`。"""
    out: dict[str, float] = {}
    for row in hand.get("start_stacks") or []:
        if not isinstance(row, dict) or not row.get("seat_id"):
            continue
        stack = _number(row.get("stack"))
        if stack is not None:
            out[str(row["seat_id"])] = stack
    hero_seat = _hero_seat(hand)
    hero_stack = _number(hand.get("hero_stack_bb"))
    if hero_seat and hero_stack is not None and hero_seat not in out:
        out[hero_seat] = hero_stack
    return out


def _behind(seat: str, observed: dict[str, tuple[float, float]],
            invested: dict[str, float], action: dict,
            amount: float | None, before: float) -> float | None:
    """该座在决策点 E 之前的后手。

    优先用「E 之前最近一次观测」：观测值是那次行动**之后**的读数，所以要把
    那之后又投进去的钱加回来（`observed` 里连观测时的累计投入一起存着）。
    该座本手还没有过观测时（它这是第一动），用「本动观测 + 本动增量」回推。
    """
    seen = observed.get(seat)
    if seen is not None:
        stack, invested_at = seen
        return stack - (invested.get(seat, 0.0) - invested_at)
    # 只有「正在行动的人」能靠本动观测回推：它的 action 传进来，别人传 None。
    if action is None:
        return None
    own = action_stack(action)
    if own is None:
        return None
    if amount is None:
        # check 不带金额：这一动没投钱，观测到的后手就是行动前的后手。
        return own
    return own + (amount - before)


def replay_hand(hand: dict, *, streets: Iterable[str] = POSTFLOP) -> list[ReplayStep]:
    """把一手归档重放成逐决策的 ActionEvent（带 SPR）。

    纯函数：只读传入的 dict，不读文件、不写文件、不改入参。
    """
    wanted = tuple(streets)
    board = [c for c in (hand.get("board") or []) if c]
    hero_seat = _hero_seat(hand)
    street_active = hand.get("street_active") or {}
    all_seats = _all_seats(hand, hero_seat)

    steps: list[ReplayStep] = []
    folded: set[str] = set()
    observed: dict[str, tuple[float, float]] = {}  # 座位 → (观测后手, 观测时累计投入)
    invested: dict[str, float] = {}        # 座位 → 本手累计投入
    # 开手筹码也是观测：减掉前注/盲注就是开手那一刻的后手，先播种进去。
    posted = _posted(hand, _positions(hand))
    for seat, opening in _opening(hand).items():
        behind_at_open = opening - posted.get(seat, 0.0)
        if behind_at_open > 0:
            observed[seat] = (behind_at_open, 0.0)
    running_pot: float | None = None       # 前面各街累加出来的底池（兜底）
    street_commit: dict[str, float] = {}   # 本街每座已投入（本街加到总额）

    for rec in hand.get("streets") or []:
        street = rec.get("street")
        if street in wanted:
            street_commit = {}
        pot = _number(rec.get("pot_start"))
        if pot is None:
            pot = running_pot
        for index, action in enumerate(rec.get("actions") or []):
            seat = _seat(action, hero_seat)
            kind = action.get("action")
            amount = _number(action.get("amount"))
            if kind == "fold":
                if seat:
                    folded.add(seat)
                continue
            if seat is None:
                continue
            before = street_commit.get(seat, 0.0)
            need = _BOARD_LEN.get(street or "", 0)
            board_text = "".join(board[:need])
            if street in wanted and len(board[:need]) == need:
                live = _live_seats(street, street_active, all_seats, folded)
                live.add(seat)
                behind = {s: _behind(s, observed, invested,
                                     action if s == seat else None,
                                     amount, before)
                          for s in live}
                missing = [s for s, value in behind.items() if value is None]
                effective = (min(v for v in behind.values() if v is not None)
                             if not missing else None)
                if effective is None:
                    spr, reason = None, SPR_NO_STACK
                elif pot is None or pot <= 0:
                    spr, reason = None, SPR_NO_POT
                else:
                    spr, reason = effective / pot, None
                steps.append(ReplayStep(
                    street=street, index=index,
                    event=range_flow.ActionEvent(
                        street=street, board=board_text, seat_id=seat,
                        action=kind, all_in=bool(action.get("all_in")),
                        amount=amount,
                        # 缺数据时就是 None，绝不写 99
                        spr=spr, pot=pot if pot is not None else 0.0),
                    effective=effective, spr_missing=reason))
            # 更新本街投入、观测与底池（都在事件之后）
            inc = 0.0 if amount is None else amount - before
            if amount is not None:
                street_commit[seat] = amount
                if pot is not None:
                    pot += inc
            invested[seat] = invested.get(seat, 0.0) + inc
            obs = action_stack(action)
            if obs is not None:
                observed[seat] = (obs, invested[seat])
        if street in wanted:
            running_pot = pot if pot is not None else running_pot
        elif pot is not None:
            running_pot = pot

    return steps


def spr_summary(hand: dict) -> dict:
    """报告用的小结：几个决策点、几个算得出 SPR、缺数据各多少。"""
    steps = replay_hand(hand)
    missing: dict[str, int] = {}
    for step in steps:
        if step.spr_missing:
            missing[step.spr_missing] = missing.get(step.spr_missing, 0) + 1
    return {
        "hand_id": hand.get("hand_id"),
        "decisions": len(steps),
        "with_spr": sum(1 for s in steps if s.spr is not None),
        "spr_missing": missing,
    }


def _load_hands(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    return [row for row in data if isinstance(row, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 hands 归档重放成逐决策的 SPR（缺数据写 null，不写 99）")
    parser.add_argument("path", type=Path, help="hands.json（JSON 数组）")
    parser.add_argument("--hand", action="append", default=[],
                        help="只看这些 hand_id（可重复）")
    parser.add_argument("--limit", type=int, default=0, help="最多看几手")
    parser.add_argument("--missing-only", action="store_true",
                        help="只打印缺数据的决策点")
    args = parser.parse_args(argv)

    hands = _load_hands(args.path)
    if args.hand:
        wanted = set(args.hand)
        hands = [h for h in hands if h.get("hand_id") in wanted]
    if args.limit:
        hands = hands[:args.limit]

    totals = {"decisions": 0, "with_spr": 0, "missing": 0}
    for hand in hands:
        steps = replay_hand(hand)
        summary = spr_summary(hand)
        totals["decisions"] += summary["decisions"]
        totals["with_spr"] += summary["with_spr"]
        totals["missing"] += summary["decisions"] - summary["with_spr"]
        print(f"\n== {hand.get('hand_id')}  "
              f"{summary['decisions']} 个决策点，"
              f"{summary['with_spr']} 个有 SPR")
        for step in steps:
            if args.missing_only and step.spr is None:
                continue
            print(f"  {step.street:7s} #{step.index} {step.event.seat_id:6s} "
                  f"{step.event.action:5s} "
                  f"底池 {step.event.pot:6.2f} "
                  f"有效 {('-' if step.effective is None else f'{step.effective:.2f}'):>7s} "
                  f"SPR {step.label()}")
    print(f"\n合计：{totals['decisions']} 个决策点，"
          f"{totals['with_spr']} 个有 SPR，{totals['missing']} 个缺数据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
