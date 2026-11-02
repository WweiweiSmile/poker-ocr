"""从实时快照解析每个在局对手的「翻前先验 + 动作事件序列」。

这是范围与快照之间的接线层：`hand_vs_ranges` 只负责算，不负责猜角色。
职责分开的好处是——角色解析错了，事实块会明确写 `range unknown`，而不是
拿一个不相干的 BB 范围去冒充所有人。

角色判定只用能确认的证据：
  · 翻前第一个加注者 → opener（`RFI_<pos>`）；
  · 最后一个加注者 → 3-bettor（`3BET_vs_<opener bucket>`）；
  · 跟注加注的 BB → BB 防守；其他跟注 → 冷跟；
  · 溜入池 → `LIMP_<bucket>`，BB 用 `BB_CHECK`（任意两张）；
  · 炸弹池 → `BOMB`（任意两张，标注 board-only）。
与 `preflop_shapes` 矛盾时不猜：该座位 `prior_id=None`，写一条 warning。

事件序列（`docs/对手范围-事件模型-设计.md`）：
  · 翻前不做事件，由 `prior_id` 覆盖；
  · 翻后每个动作一条 `ActionEvent`，上下文取**行动前**状态：身后还有几人、
    面对什么下注、本街这个下注之后已有几次跟注、SPR；
  · 动作顺序以 `snap.streets` 的记录为准；对账补出来的 `inferred` 动作照用，
    但整条链上只要有一条是补的，就在数据质量里标注。

当前街动作仍保留 `mode`（兼容旧的 `board_range.build` 路径）：bet/raise/allin
→ 同名 mode；call / check → call / check；还没动作 → unacted。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import range_flow
from .hand_track import FLOP, PREFLOP, RIVER, TURN, ActionRecord, HandSnapshot
from .positions import BB, POSITIONS_BY_TABLE_SIZE, SB


@dataclass(frozen=True)
class OpponentRangeSpec:
    """一个在局对手的范围口径；`prior_id=None` 表示没有把握，不要猜。"""

    seat_id: str
    position: str | None
    prior_id: str | None
    mode: str
    role: str = ""
    note: str = ""
    # 翻后动作序列（时间顺序）。空 = 这手还没有可用的翻后动作，退回旧口径。
    events: tuple[range_flow.ActionEvent, ...] = field(default=())


@dataclass(frozen=True)
class Field:
    specs: tuple[OpponentRangeSpec, ...]
    warnings: tuple[str, ...] = ()
    pot_type: str | None = None

    @property
    def ranges(self) -> tuple[OpponentRangeSpec, ...]:
        """别名：旧文档把 specs 叫 ranges。"""
        return self.specs

    def __bool__(self) -> bool:
        return bool(self.specs)


_EP = ("UTG", "UTG+1", "UTG+2")
_MP = ("LJ", "HJ")
_LP = ("CO", "BTN")


def bucket(position: str | None) -> str:
    """位置 → 范围桶（EP/MP/LP/SB/BB）。未知位置返回 LP（仅用于兜底 ID）。"""
    pos = (position or "").strip().upper()
    if pos in _EP:
        return "EP"
    if pos in _MP:
        return "MP"
    if pos in _LP:
        return "LP"
    if pos in (SB, BB):
        return pos
    return "LP"


def _position_order(table_size: int | None) -> list[str] | None:
    return POSITIONS_BY_TABLE_SIZE.get(table_size or 0)


def _index(position: str | None, table_size: int | None) -> int | None:
    order = _position_order(table_size)
    if not order or not position or position not in order:
        return None
    return order.index(position)


def _in_position(caller: str | None, opener: str | None,
                 table_size: int | None) -> bool | None:
    """caller 是否在 opener 后面行动（翻后更有位置）。位置缺失 → None。"""
    i, j = _index(caller, table_size), _index(opener, table_size)
    if i is None or j is None:
        return None
    return i > j


def _raises(actions: list[ActionRecord]) -> list[ActionRecord]:
    """翻前的加注动作；全下跟注也算一次加注（与 hand_track 同口径）。"""
    return [a for a in actions
            if a.action in ("raise", "bet")
            or (a.action == "call" and a.all_in)]


def _action_position(action: ActionRecord, snap: HandSnapshot) -> str | None:
    """动作记录里的位置；hero 的动作可能只有快照位置。"""
    if action.position:
        return action.position
    if action.is_hero:
        return snap.hero_position
    return None


def _opener_position(snap: HandSnapshot, raises: list[ActionRecord]) -> str | None:
    if raises:
        return _action_position(raises[0], snap)
    if snap.hero_is_pfr:
        return snap.hero_position
    return None


def _last_aggressor(raises: list[ActionRecord]) -> ActionRecord | None:
    return raises[-1] if raises else None


def _role_prior(snap: HandSnapshot, opp, raises: list[ActionRecord],
                opener_pos: str | None, last: ActionRecord | None,
                warnings: list[str]) -> tuple[str | None, str, str]:
    """返回 (prior_id, role, note)。"""
    pos = opp.position
    seat = opp.seat_id

    if snap.bomb_pot:
        return "BOMB", "bomb", "bomb pot: any two cards"

    if len(raises) >= 3:
        warnings.append("4-bet+ pot: no range table for this spot (range unknown)")
        return None, "unknown", "4-bet+"

    if len(raises) >= 2:
        if last is not None and seat == last.seat_id:
            if opener_pos is None:
                warnings.append(f"seat {seat}: 3-bet opener position unread "
                                "(range unknown)")
                return None, "three_bettor", "3-bet opener position unread"
            return (f"3BET_vs_{bucket(opener_pos)}", "three_bettor",
                    "3-bet range")
        last_pos = _action_position(last, snap) if last is not None else None
        if last_pos is None:
            warnings.append(f"seat {seat}: 3-bettor position unread "
                            "(range unknown)")
            return None, "three_bet_caller", "3-bettor position unread"
        return (f"3BET_CALL_vs_{bucket(last_pos)}",
                "three_bet_caller", "called a 3-bet")

    if len(raises) == 1:
        if seat == raises[0].seat_id:
            if pos:
                return f"RFI_{pos}", "pfr", "preflop raiser"
            warnings.append(f"seat {seat}: opener position unread (range unknown)")
            return None, "pfr", "opener position unread"
        if pos == BB:
            if opener_pos is None:
                warnings.append(f"seat {seat}: BB defense needs opener position")
                return None, "bb_def", "opener position unread"
            return (f"BB_DEF_vs_{opener_pos}", "bb_def",
                    "big blind defense")
        if pos:
            if opener_pos is None:
                warnings.append(f"seat {seat}: opener position unread "
                                "(range unknown)")
                return None, "cold_call", "opener position unread"
            ip = _in_position(pos, opener_pos, snap.table_size)
            if ip is None:
                warnings.append(f"seat {seat}: position relative to opener "
                                "unknown; using OOP cold-call range")
                ip = False
            return (f"COLD_CALL_{'IP' if ip else 'OOP'}_vs_{bucket(opener_pos)}",
                    "cold_call", "cold-called the open")
        warnings.append(f"seat {seat}: position unread (range unknown)")
        return None, "cold_call", "position unread"

    # 没有加注
    if snap.hero_is_pfr:
        # 形状说 single-raise，但只靠 hero_is_pfr 知道 opener；仍可用 BB/冷跟。
        if snap.hero_position is None:
            warnings.append(f"seat {seat}: hero position unread; "
                            "cannot infer the opener (range unknown)")
            return None, "unknown", "hero position unread"
        if pos == BB:
            return (f"BB_DEF_vs_{snap.hero_position}", "bb_def",
                    "inferred from hero_is_pfr")
        if pos:
            ip = _in_position(pos, snap.hero_position, snap.table_size)
            return (f"COLD_CALL_{'IP' if ip else 'OOP'}_vs_"
                    f"{bucket(snap.hero_position)}", "cold_call",
                    "inferred from hero_is_pfr")
        return None, "unknown", "position unread"

    shapes = set(snap.preflop_shapes or ())
    if "limped" in shapes:
        if pos == BB:
            return "BB_CHECK", "bb_check", "limped pot, BB checked"
        if pos:
            return f"LIMP_{bucket(pos)}", "limper", "limped pot"
        warnings.append(f"seat {seat}: position unread (range unknown)")
        return None, "limper", "position unread"

    if "single-raise" in shapes or "three-bet" in shapes:
        warnings.append("preflop shape says a raise but no raise was recorded "
                        f"(seat {seat}: range unknown)")
        return None, "unknown", "raise unread"
    if not shapes:
        warnings.append(f"seat {seat}: no preflop action; "
                        "preflop shape/PFR unknown")
        return None, "unknown", "no preflop action"
    return None, "unknown", "no preflop action"


# ─────────────────────────── 事件序列 ───────────────────────────

_BOARD_LEN = {FLOP: 3, TURN: 4, RIVER: 5}
# hero 在快照里没有 seat_id（`opponents` 不含 hero）。动作记录里通常能拿到，
# 拿不到时用这个占位符参与座位环，只影响「身后还有几人」的计数。
_HERO_KEY = "__hero__"


def _action_seat(action: ActionRecord, hero: str) -> str:
    return hero if action.is_hero else action.seat_id


def _seats(snap: HandSnapshot) -> tuple[dict[str, str | None], str]:
    """seat_id → 位置（含 hero 的占位/真实 seat_id）。"""
    seats: dict[str, str | None] = {o.seat_id: o.position for o in snap.opponents}
    hero = _HERO_KEY
    for rec in snap.streets:
        found = next((a.seat_id for a in rec.actions if a.is_hero), None)
        if found:
            hero = found if found not in seats else _HERO_KEY
            break
    seats.setdefault(hero, snap.hero_position)
    return seats, hero


def _seat_ring(snap: HandSnapshot, seats: dict[str, str | None],
               hero: str) -> list[str]:
    """翻后行动顺序的座位环。位置齐全时按位置表，否则用动作记录兜底。

    位置表（`POSITIONS_BY_TABLE_SIZE`）本身就是翻前行动顺序，翻后从 SB 起
    沿同一方向走，所以位置齐全时它就是正确的行动环。OCR 漏位置/位置重复
    时才退回「动作出现的顺序」——那只保证大致方向，不保证环是对的。
    """
    order = POSITIONS_BY_TABLE_SIZE.get(snap.table_size or 0)
    positions = [p for p in seats.values() if p]
    if (order and len(positions) == len(seats)
            and len(set(positions)) == len(seats)
            and all(p in order for p in positions)):
        return sorted(seats, key=lambda sid: order.index(seats[sid] or ""))
    seen: list[str] = []
    for rec in snap.streets:
        for a in rec.actions:
            seat = _action_seat(a, hero)
            if seat not in seen:
                seen.append(seat)
    for sid in seats:
        if sid not in seen:
            seen.append(sid)
    return seen


def _invested_totals(snap: HandSnapshot, hero: str) -> dict[str, float]:
    """每个座位这手一共投进底池多少（用「本街加到总额」逐街累加）。

    快照只给**当前**后手，事件发生在过去。后手 = 当前后手 + 总投入 − 行动前
    已投入，用它才能算行动时的 SPR。前注/盲注不在动作记录里，会整体少算
    几 BB；SPR 只做方向性修正，这点误差可以接受。
    """
    totals: dict[str, float] = {}
    for rec in snap.streets:
        per_street: dict[str, float] = {}
        for a in rec.actions:
            if a.action == "fold" or a.amount is None:
                continue
            seat = _action_seat(a, hero)
            per_street[seat] = max(per_street.get(seat, 0.0), float(a.amount))
        for seat, amount in per_street.items():
            totals[seat] = totals.get(seat, 0.0) + amount
    return totals


def _players_behind(ring: list[str], seat: str, folded: set[str],
                    all_in: set[str], acted: set[str]) -> int:
    """身后还能行动、且本街还没行动过的对手数（设计 §1 的补充约定）。"""
    try:
        index = ring.index(seat)
    except ValueError:
        return 0
    behind = 0
    for step in range(1, len(ring)):
        other = ring[(index + step) % len(ring)]
        if other == seat:
            continue
        if other in folded or other in all_in or other in acted:
            continue
        behind += 1
    return behind


def build_events(snap: HandSnapshot) -> tuple[dict[str, tuple], tuple[str, ...]]:
    """按时间顺序给每个座位生成翻后动作事件。

    返回 `{seat_id: (ActionEvent, ...)}` 与 warnings。翻前不产生事件：
    先验已经覆盖了「他翻前用哪些牌进池」，重复计入等于算两次。
    """
    seats, hero = _seats(snap)
    ring = _seat_ring(snap, seats, hero)
    warnings: list[str] = []

    current_stack: dict[str, float | None] = {
        o.seat_id: o.stack for o in snap.opponents}
    current_stack[hero] = snap.hero_stack
    invested_total = _invested_totals(snap, hero)
    invested_before_street: dict[str, float] = {}
    events: dict[str, list[range_flow.ActionEvent]] = {sid: [] for sid in ring}
    # 弃牌状态以动作记录为准（它带时间顺序）。但「这手根本没参与」的座位
    # （空座、带入中、翻前就弃且没抓到动作）在记录里一条动作都没有，只能
    # 用快照的 folded 兜底；否则他会一直算进「身后还有几人」。
    recorded = {_action_seat(a, hero)
                for rec in snap.streets for a in rec.actions}
    folded: set[str] = {o.seat_id for o in snap.opponents
                        if o.folded and o.seat_id not in recorded}
    all_in: set[str] = set()
    prev_aggressor: str | None = None
    running_pot = 0.0
    inferred_used = 0

    for rec in snap.streets:
        street = rec.street
        emit = street in _BOARD_LEN
        board_len = _BOARD_LEN.get(street, 0)
        board_cards = snap.board[:board_len]
        if emit and len(board_cards) < board_len:
            warnings.append(f"{street}: board incomplete "
                            f"({len(board_cards)}/{board_len} cards); "
                            "action events skipped for this street")
            emit = False
        board_text = "".join(board_cards)

        committed: dict[str, float] = {}
        acted: set[str] = set()
        level = 0.0
        raises = 0
        callers = 0
        street_aggressor: str | None = None
        if rec.pot_start is not None:
            pot = float(rec.pot_start)
        else:
            pot = running_pot
            if emit:
                warnings.append(f"{street}: pot at street start unread; "
                                "SPR is approximate")

        for action in rec.actions:
            seat = _action_seat(action, hero)
            if action.action == "fold":
                folded.add(seat)
                acted.add(seat)
                continue
            if seat not in events:              # 不在座位环上（离桌/漏座位）
                events.setdefault(seat, [])

            amount = float(action.amount) if action.amount is not None else None
            before = committed.get(seat, 0.0)
            facing = level > 0 and before < level - 1e-9
            behind = _players_behind(ring, seat, folded, all_in, acted)
            first = not acted
            pot_before = pot
            if emit:
                stack_actor = _stack_at_event(current_stack, invested_total,
                                              invested_before_street, seat,
                                              before)
                stack_hero = _stack_at_event(current_stack, invested_total,
                                             invested_before_street, hero,
                                             committed.get(hero, 0.0))
                effective = min((s for s in (stack_actor, stack_hero)
                                 if s is not None), default=None)
                spr = (effective / pot_before
                       if effective is not None and pot_before > 1e-9 else 99.0)
                events[seat].append(range_flow.ActionEvent(
                    street=street,
                    board=board_text,
                    seat_id=seat,
                    action=action.action,
                    all_in=bool(action.all_in),
                    amount=amount,
                    facing_bet=facing,
                    raises_before=raises,
                    n_callers=callers,
                    players_behind=behind,
                    is_first_to_act=first,
                    is_last_to_act=behind <= 0,
                    closes_action=(action.action == "call" and behind <= 0),
                    prev_street_aggressor=prev_aggressor,
                    is_donk=(not facing and first
                             and action.action in ("bet", "raise")
                             and prev_aggressor is not None
                             # 自己是上一街 aggressor 时是续注（lead），
                             # 不是 donk——donk 是「下注进别人的 aggressor」。
                             and prev_aggressor != seat
                             and prev_aggressor not in folded
                             and prev_aggressor not in acted),
                    spr=spr,
                    pot=pot_before,
                ))
                if action.inferred:
                    inferred_used += 1

            # ── 状态推进（用行动**前**的 before/level 算，再更新） ──
            delta = max((amount if amount is not None else level) - before, 0.0)
            pot = pot_before + delta
            if amount is not None:
                committed[seat] = max(before, amount)
            if action.action in ("bet", "raise") or (
                    action.action == "call" and action.all_in):
                level = max(level, committed.get(seat, level))
                raises += 1
                callers = 0
                street_aggressor = seat
            elif action.action == "call":
                callers += 1
            acted.add(seat)
            if action.all_in:
                all_in.add(seat)

        # 下一街的 donk 只能对着**本街真下过注**的人；本街全过牌就没有。
        prev_aggressor = street_aggressor
        for seat, amount in committed.items():
            invested_before_street[seat] = (
                invested_before_street.get(seat, 0.0) + amount)
        running_pot = pot

    if inferred_used:
        warnings.append(f"range events include {inferred_used} inferred "
                        "action(s) from pot reconciliation; order/context "
                        "are approximate")
    return ({sid: tuple(items) for sid, items in events.items()},
            tuple(dict.fromkeys(warnings)))


def _stack_at_event(current_stack: dict[str, float | None],
                    invested_total: dict[str, float],
                    invested_before_street: dict[str, float],
                    seat: str, street_committed: float) -> float | None:
    now = current_stack.get(seat)
    if now is None:
        return None
    invested_before = (invested_before_street.get(seat, 0.0) + street_committed)
    return now + invested_total.get(seat, 0.0) - invested_before


def _mode(snap: HandSnapshot, opp, warnings: list[str]) -> str:
    """当前街动作 → 牌面范围 mode。"""
    if snap.street == PREFLOP:
        return "unacted"
    acts = [a for a in snap.actions_of(snap.street)
            if a.seat_id == opp.seat_id]
    if acts:
        last = acts[-1]
        if last.action == "fold":
            return "fold"
        if last.all_in:
            return "allin"
        action = last.action
        if action in ("bet", "raise", "call", "check", "allin"):
            return action
    # 有下注额但没有动作徽章：分不清是 bet 还是 call。
    # 退回 "call"（两者里更宽的那个），**不是** "unknown"：unknown 会让
    # board_range.build 把 prior 置空、退化成「按牌面筛的随机牌」，实测同一
    # 局面范围从 11.1% 膨胀到 50.1%、我方权益从 5.9% 虚高到 15.8%。
    # 丢掉翻前先验没有任何依据 —— 他进池跟到这一街，就不可能拿着任意两张。
    if opp.bet is not None and float(opp.bet) > 1e-9:
        warnings.append(f"seat {opp.seat_id}: bet size without an action badge; "
                        "using call range (wider of bet/call, keeps preflop prior)")
        return "call"
    # 还没行动 / 过牌但徽章没读到：没有新信息，保留翻前范围。
    # 不能叫 "check"：本街完全没动作时写「过牌范围」等于凭空造事实。
    return "unacted"


def resolve(snap: HandSnapshot) -> Field:
    """把快照解析成每个在局对手的 (先验, mode, 事件序列)。失败也返回 warnings。"""
    warnings: list[str] = []
    active = [o for o in snap.opponents if not o.folded]
    if not active:
        return Field((), ("no opponents in hand",))
    if len(active) > 4:
        warnings.append(f"{len(active)} opponents in hand: only the first 4 are "
                        "modeled (render/perf cap)")
        active = active[:4]

    pre = snap.actions_of(PREFLOP)
    raises = _raises(pre)
    opener_pos = _opener_position(snap, raises)
    last = _last_aggressor(raises)
    if any(a.inferred for a in raises):
        warnings.append("preflop raise was inferred from the pot ledger; "
                        "roles are approximate")

    # 与 preflop_shapes 交叉校验：形状是 hand_track 累积的，动作线可能漏读。
    shapes = set(snap.preflop_shapes or ())
    unreliable_raise = False
    if "three-bet" in shapes and len(raises) < 2:
        warnings.append("preflop shape says three-bet but fewer than two raises "
                        "were recorded; ranges may be approximations")
        unreliable_raise = True
    if "single-raise" in shapes and not raises and not snap.hero_is_pfr:
        warnings.append("preflop shape says single-raise but no raiser was "
                        "recorded; ranges unavailable")
        unreliable_raise = True
    if "limped" in shapes and raises:
        warnings.append("preflop shape says limped but a raise was recorded")
        unreliable_raise = True
    if "unopened" in shapes and raises:
        warnings.append("preflop shape says unopened but a raise was recorded")
        unreliable_raise = True
    if "limped" in shapes and snap.hero_is_pfr:
        warnings.append("hero_is_pfr contradicts a limped pot; ranges unavailable")
        unreliable_raise = True
    if "unopened" in shapes and snap.hero_is_pfr:
        warnings.append("hero_is_pfr contradicts an unopened pot; ranges unavailable")
        unreliable_raise = True

    specs: list[OpponentRangeSpec] = []
    # 事件序列是「这手牌怎么走到现在的」唯一完整记录，按座位生成一次。
    # 关闭事件模型时不必算，省掉一层遍历。
    event_map: dict[str, tuple] = {}
    if range_flow.enabled():
        event_map, event_warnings = build_events(snap)
        warnings.extend(event_warnings)
    for opp in active:
        if unreliable_raise:
            # 形状与动作线矛盾时不猜角色：宁可 range unknown，也不拿 BB 范围
            # 去冒充一个 3-bet 池里的冷跟者。
            prior_id, role, note = None, "unknown", "preflop actions contradictory"
            warnings.append(f"seat {opp.seat_id}: range unknown "
                            "(preflop actions contradictory)")
        else:
            prior_id, role, note = _role_prior(snap, opp, raises, opener_pos,
                                               last, warnings)
        mode = _mode(snap, opp, warnings)
        if mode == "fold":
            continue
        seat_events = event_map.get(opp.seat_id, ())
        if mode == "unacted" and snap.street != PREFLOP and not seat_events:
            warnings.append(
                f"seat {opp.seat_id}: no action this street; "
                "range kept at preflop prior")
        if prior_id is None and not unreliable_raise:
            warnings.append(f"seat {opp.seat_id}: range unknown ({role})")
        specs.append(OpponentRangeSpec(
            seat_id=opp.seat_id,
            position=opp.position,
            prior_id=prior_id,
            mode=mode,
            role=role,
            note=note,
            events=seat_events,
        ))

    # 短码的有效筹码会把范围整体拉宽/拉窄；表仍可用，但要标注近似。
    stacks = [o.stack for o in active if o.stack is not None]
    if snap.hero_stack is not None and stacks:
        if min([*stacks, snap.hero_stack]) < 40:
            warnings.append("short stack (<40BB): preflop ranges are approximate")

    return Field(tuple(specs), tuple(dict.fromkeys(warnings)), None)


__all__ = ["Field", "OpponentRangeSpec", "bucket", "build_events", "resolve"]
