"""规则台账的谓词快照——台账能引用的**唯一**事实来源。

设计见 `docs/规则台账-设计-2026-11-18.md` §5。三条铁律：

1. **只写知道的事实**：任何一项读不出来就不写这个键（fail open）。匹配器把
   缺键当「不知道」处理，绝不把缺失当 False——把「没读到筹码」当成「筹码是
   0」，会让所有短码规则的整体区间错位。
2. **不另开口径**：赔率、SPR、有效筹码、平跟数一律复用 `pot_math`。同一个
   事实在两处算法不同 = 静默错值，是这套台账最贵的坑。
3. **不抛异常**：这是 OCR 主链路的旁路，任何读不出的牌面/范围都退化成
   「没这条事实」，绝不让规则层把主循环搞挂。

谓词目录（29 项）见 `rules.PREDICATES`；本模块的产物必须落在里面，
`tests/test_rules_ledger.py` 会断言这一点。新增谓词要两边一起改。
"""

from __future__ import annotations

import math
from typing import Any

from .. import ranges as _ranges
from ..coach import parse_cards
from ..hand_track import PREFLOP, preflop_aggressive
from ..pot_math import (
    _effective_stack, _playable_depth, count_limpers, hero_first_to_act,
    pot_odds,
)
from ..preflop_ranges import hand_key, range_verdict


def facts_snapshot(sit: Any) -> dict[str, Any]:
    """把 `Situation` 摊平成「谓词 → 值」；读不出来的键直接不出现。"""
    snap = getattr(sit, "snap", None)
    if snap is None:
        return {}

    out: dict[str, Any] = {"street": snap.street}
    _put(out, "hero_position", getattr(snap, "hero_position", None))
    _put(out, "in_position", getattr(sit, "in_position", None))
    _put(out, "pot_type", getattr(sit, "pot_type", None))
    # `explosive_pot` 读不到底池时是 False，但那不是「不是炸弹池」而是「不知道」
    # ——底池缺席就别出面，否则 `eq:false` 的规则会命中一个没发生过的事实。
    if getattr(snap, "bomb_pot", False) or getattr(sit, "pot_per_player", None) is not None:
        _put(out, "explosive_pot", bool(getattr(sit, "explosive_pot", False)))

    # 在局人数含 hero：`opponents_in_hand` 不含 hero，规则里写区间（gte:3）
    # 的人数是「桌上还有几个人没弃牌」，所以要 +1。
    opponents = getattr(snap, "opponents", ()) or ()
    if opponents:
        alive = sum(1 for o in opponents if not getattr(o, "folded", False))
        if alive:
            _put(out, "players_in_hand", alive + 1)

    # 这两个谓词按设计 §5 只描述**翻前**：翻后的「前面还有几个人加注」是另一个
    # 概念（本街加注数），不能拿翻前序列冒充。
    if snap.street == PREFLOP:
        _put(out, "limpers_before", _int_or_none(_guard(count_limpers, snap)))
        _put(out, "raises_before", _raises_before(snap))

    pot = getattr(snap, "pot", None)
    if pot is not None and pot > 0:
        _put(out, "pot_bb", float(pot))
    _put(out, "to_call_bb", _num(getattr(snap, "to_call", None)))

    _put(out, "pot_odds", _num(_guard(pot_odds, snap)))

    if snap.street != PREFLOP:
        _put(out, "spr", _num(getattr(sit, "spr", None)))
        _put(out, "hero_is_first_to_act",
             _guard(hero_first_to_act, snap))
    _put(out, "effective_stack_bb", _num(_guard(_effective_stack, snap)))

    _put(out, "hero_hand_tier", getattr(sit, "hand_tier", None))
    _put(out, "hand_label", getattr(sit, "hand_label", None))
    # 牌力没读出来时 `has_draw`/`outs` 都是 0 而不是「没有听牌」，同样不出面。
    if getattr(sit, "hand_tier", None) is not None:
        _put(out, "has_draw", bool(getattr(sit, "has_draw", False)))
    _put(out, "outs", getattr(sit, "outs", None))
    _put(out, "equity_pct", _num(getattr(sit, "equity_pct", None)))
    _put(out, "board_texture", getattr(sit, "texture", None))
    _put(out, "board_paired", _board_paired(sit))

    _put(out, "ante_bb", _ante_bb(snap))
    _put(out, "previous_line", _previous_line(sit))
    _put_opponent(out, sit, snap)

    if snap.street == PREFLOP:
        _put(out, "hand_in_open_range", _in_open_range(sit, snap))
        _put(out, "hand_in_3bet_range", _in_3bet_range(snap))
    return out


# ─────────────────────────── 小工具 ───────────────────────────

def _put(out: dict, key: str, value: Any) -> None:
    """写一个事实；None / NaN 一律不写（缺事实就不出声）。"""
    if value is None:
        return
    if isinstance(value, float) and not math.isfinite(value):
        return
    out[key] = value


def _guard(fn, *args, **kwargs):
    """旁路调用：任何异常都退化成「没这条事实」。"""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - 见模块头铁律 3
        return None


def _num(value) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _int_or_none(value) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _raises_before(snap) -> int | None:
    """本街在 hero 之前**抬高水位**的次数（hero 自己此前的不算）。

    翻前的开池记作 `raise`、翻后第一次进攻记作 `bet`；这里数的是「我已经
    被加注几次」，所以 `bet` 不计数——「面对一个下注」和「面对一个加注」
    是两条不同规则的前提。
    """
    try:
        actions = snap.actions_of(snap.street)
    except Exception:  # noqa: BLE001
        return None
    count = 0
    for action in preflop_aggressive(actions):
        if getattr(action, "is_hero", False):
            continue
        if getattr(action, "action", None) == "bet":
            continue
        count += 1
    return count


def _board_paired(sit: Any) -> bool | None:
    """公对面：先信 `ground_truth`，没有就从牌面自己数（≥3 张才算）。"""
    info = getattr(getattr(sit, "ground_truth", None), "board_info", None)
    if info is not None and getattr(info, "paired", None) is not None:
        return bool(info.paired)
    board = tuple(getattr(getattr(sit, "snap", None), "board", ()) or ())
    if len(board) < 3:
        return None
    try:
        ranks = [rank for rank, _suit in parse_cards("".join(board))]
    except (ValueError, TypeError):
        return None
    return len(set(ranks)) < len(ranks)


def _ante_bb(snap) -> float | None:
    """前注（BB/人）；`Blinds.ante` 已经是 BB，这里不再换算。

    口径证据：`poker_ocr/parse.py:189-191` 把 `Blinds.sb/bb/ante` 的字段
    单位写成 BB，`parse_blinds` 在 `:219-221` 用 `ante_chips / bb_chips`
    把横幅里的筹码数折成 BB；`poker_ocr/prompt/system/__init__.py:52-53`
    也按「前注 {ante}BB」渲染。所以这里只做「>0 才出面」的门控，绝不乘除。
    """
    ante = getattr(getattr(snap, "blinds", None), "ante", None)
    number = _num(ante)
    return number if number and number > 0 else None


def _previous_line(sit: Any) -> str | None:
    """上一街 hero 的动作类型（`Situation.previous`）；同一手内才精确。"""
    option = getattr(getattr(sit, "previous", None), "option", None)
    kind = getattr(option, "kind", None)
    if isinstance(kind, str) and kind:
        return kind
    if isinstance(option, dict):
        kind = option.get("kind")
        if isinstance(kind, str) and kind:
            return kind
    return None


def _put_opponent(out: dict, sit: Any, snap) -> None:
    """对手画像类谓词。

    只有一个在局对手时，画像的 vpip/pfr/fold_to_cbet 直接可用；多个在局对手
    时类型只在全员一致时才算事实——把「有人凶」当成「所有人都凶」会让松系
    规则在多人池里瞎放大。
    """
    alive = {getattr(o, "seat_id", None) for o in (snap.opponents or ())
             if not getattr(o, "folded", False)}
    reads = [r for r in (getattr(sit, "opponent_reads", ()) or ())
             if getattr(r, "seat_id", None) in alive]
    if not reads:
        return
    archetypes = {getattr(r, "archetype", None) for r in reads}
    if len(archetypes) == 1:
        _put(out, "opponent_archetype", next(iter(archetypes)))
    if len(reads) != 1:
        return
    read = reads[0]
    _put(out, "opp_vpip", _num(getattr(read, "vpip", None)))
    _put(out, "opp_pfr", _num(getattr(read, "pfr", None)))
    _put(out, "opp_fold_to_cbet", _num(getattr(read, "fold_to_cbet_pct", None)))


def _in_open_range(sit: Any, snap) -> bool | None:
    """这手牌按 RFI 表该不该开池；SB/BB 没有开池频率，`in_range` 是 None。"""
    cards = tuple(getattr(snap, "hero_cards", ()) or ())
    position = getattr(snap, "hero_position", None)
    if len(cards) != 2 or not position:
        return None
    verdict = _guard(
        range_verdict, position, cards,
        table_dynamics=getattr(sit, "table_dynamics", None),
        limpers=_int_or_none(_guard(count_limpers, snap)) or 0,
        stack_bb=_num(_guard(_playable_depth, snap)),
    )
    in_range = getattr(verdict, "in_range", None)
    return None if in_range is None else bool(in_range)


def _in_3bet_range(snap) -> bool | None:
    """这手牌是否在 3-bet 范围里（`ranges.get("3BET")` 的 169 键投影）。"""
    cards = tuple(getattr(snap, "hero_cards", ()) or ())
    if len(cards) != 2:
        return None
    key = _guard(hand_key, cards)
    if not isinstance(key, str) or not key:
        return None
    # `ranges.get()` 认的是 `3BET_<后缀>` 前缀；`3BET` 单独不是合法 id。
    rng = _guard(_ranges.get, "3BET_default")
    if rng is None:
        return None
    try:
        keys = {_ranges.key_of_combo(combo) for combo in rng.combos()}
    except Exception:  # noqa: BLE001
        return None
    return key in keys
