"""从实时快照解析每个在局对手的「翻前先验 + 当前街动作模式」。

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

当前街动作决定牌面范围的 mode：
  · bet/raise/allin → 同名 mode；
  · call / check → call / check；
  · 还没动作 → check（没有新信息）；
  · 有下注额但没有动作记录 → unknown + warning。
"""

from __future__ import annotations

from dataclasses import dataclass

from .hand_track import PREFLOP, ActionRecord, HandSnapshot
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
    return None, "unknown", "no preflop action"


def _mode(snap: HandSnapshot, opp, warnings: list[str]) -> str:
    """当前街动作 → 牌面范围 mode。"""
    if snap.street == PREFLOP:
        return "check"
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
    # 有下注额但没有动作徽章：不能当成过牌，牌面范围会放宽。
    if opp.bet is not None and float(opp.bet) > 1e-9:
        warnings.append(f"seat {opp.seat_id}: bet size without an action badge; "
                        "using unknown mode (wider board range)")
        return "unknown"
    # 还没行动 / 过牌但徽章没读到：没有新信息，保留翻前范围。
    return "check"


def resolve(snap: HandSnapshot) -> Field:
    """把快照解析成每个在局对手的 (先验, mode)。失败也返回 Field + warnings。"""
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
        if prior_id is None and not unreliable_raise:
            warnings.append(f"seat {opp.seat_id}: range unknown ({role})")
        specs.append(OpponentRangeSpec(
            seat_id=opp.seat_id,
            position=opp.position,
            prior_id=prior_id,
            mode=mode,
            role=role,
            note=note,
        ))

    # 短码的有效筹码会把范围整体拉宽/拉窄；表仍可用，但要标注近似。
    stacks = [o.stack for o in active if o.stack is not None]
    if snap.hero_stack is not None and stacks:
        if min([*stacks, snap.hero_stack]) < 40:
            warnings.append("short stack (<40BB): preflop ranges are approximate")

    return Field(tuple(specs), tuple(dict.fromkeys(warnings)), None)


__all__ = ["Field", "OpponentRangeSpec", "bucket", "resolve"]
