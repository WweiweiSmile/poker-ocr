"""规则台账的谓词快照——台账能引用的**唯一**事实来源。

设计见 `docs/规则台账-设计-2026-11-18.md` §5。三条铁律：

1. **只写知道的事实**：任何一项读不出来就不写这个键（fail open）。匹配器把
   缺键当「不知道」处理，绝不把缺失当 False——把「没读到筹码」当成「筹码是
   0」，会让所有短码规则的整体区间错位。
2. **不另开口径**：赔率、SPR、有效筹码、平跟数一律复用 `pot_math`。同一个
   事实在两处算法不同 = 静默错值，是这套台账最贵的坑。
3. **不抛异常**：这是 OCR 主链路的旁路，任何读不出的牌面/范围都退化成
   「没这条事实」，绝不让规则层把主循环搞挂。

谓词目录（34 项，含翻后 5 项派生谓词）见 `rules.PREDICATES`；本模块的产物必须落在里面，
`tests/test_rules_ledger.py` 会断言这一点。新增谓词要两边一起改。
"""

from __future__ import annotations

import math
from typing import Any

from .. import ranges as _ranges
from ..coach import (
    AIR, BOARD_PLAY, MID_BOTTOM, OVERPAIR, STRONG_DRAW, STRONG_MADE, TPTK,
    TP_WEAK, WEAK_DRAW, parse_cards, read_hand, turn_scare,
)
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
    _put(out, "hand_label", _hand_label(sit, snap))
    # 牌力没读出来时 `has_draw`/`outs` 都是 0 而不是「没有听牌」，同样不出面。
    if getattr(sit, "hand_tier", None) is not None:
        _put(out, "has_draw", bool(getattr(sit, "has_draw", False)))
    _put(out, "outs", getattr(sit, "outs", None))
    _put(out, "equity_pct", _num(getattr(sit, "equity_pct", None)))
    _put(out, "board_texture", getattr(sit, "texture", None))
    _put(out, "board_paired", _board_paired(sit))

    # ── 翻后派生事实（票 70）──────────────────────────────────────────
    # 都是**折叠**出来的，不是新口径：hand_class 由 hero_hand_tier 折叠，
    # hand_tier_rose 用同一条 read_hand 读上一条街的牌面，turn_scare 直接用
    # coach 的判定。任何一步读不出来就不写这个键（fail open）。
    _put(out, "hand_class", _hand_class(getattr(sit, "hand_tier", None)))
    _put(out, "hero_is_nuts", _hero_is_nuts(sit))
    if snap.street in ("turn", "river"):
        _put(out, "hand_tier_rose", _hand_tier_rose(sit, snap))
    if snap.street == "turn":
        _put(out, "turn_scare", _turn_scare(snap))

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


# ── 翻后派生（票 70）─────────────────────────────────────────────

# 牌力档 → 尺寸档：书 p35 的 size_ladder 只有 strong/medium/weak 三行，
# 台账不能自己拼牌型，所以在这里折叠一次，三处共用同一份映射。
#   strong = 能拿价值、要被跟的牌（两对+、超对、顶对强踢）
#   medium = 只有一个对子、能摊牌但经不起大池（顶对弱踢、中/底对）
#   weak   = 没有摊牌价值或靠抽牌（听牌、空气、打牌面）
# 九个档位全部有归属；漏一个就是映射缺口 → 不出键，规则不命中（fail open）。
HAND_CLASS_BY_TIER: dict[str, str] = {
    STRONG_MADE: "strong", OVERPAIR: "strong", TPTK: "strong",
    TP_WEAK: "medium", MID_BOTTOM: "medium",
    STRONG_DRAW: "weak", WEAK_DRAW: "weak", AIR: "weak", BOARD_PLAY: "weak",
}

# 牌力档的高低序列（只用于判断「转/河比上一条街强了还是弱了」）。
# 成牌部分与 `poker_ocr/situation.py` 的 _MADE_TIERS 顺序一致（两对+ 最高、
# 打牌面是最低的成牌），听牌插在成牌下面、空气垫底——这是**档位**顺序，
# 不是权益排序，所以不拿它算胜率。
TIER_ORDER: tuple[str, ...] = (
    AIR, WEAK_DRAW, STRONG_DRAW, BOARD_PLAY, MID_BOTTOM,
    TP_WEAK, TPTK, OVERPAIR, STRONG_MADE,
)

# AF 分档阈值：AF 是「下注/加注次数 ÷ 跟注次数」，牌圈惯例 1 以下偏被动、
# 2 以上偏凶。这不是书上的数字（书没有 AF 口径），所以留在事实层当常量，
# 不进 rule_params（那张表只放能标书页的尺寸/频率）。
AF_HIGH = 2.0
AF_LOW = 1.0


def _hand_class(tier: Any) -> str | None:
    """牌力档 → 强/中/弱；不在映射表里就不出声。"""
    if not isinstance(tier, str):
        return None
    return HAND_CLASS_BY_TIER.get(tier)


def _hero_is_nuts(sit: Any) -> bool | None:
    """坚果事实：只信 ground_truth 的「没有对手持牌能打败我」。"""
    gt = getattr(sit, "ground_truth", None)
    if gt is None:
        return None
    flag = _guard(lambda: gt.hero_is_nuts)
    return None if flag is None else bool(flag)


def _tier_rank(tier: Any) -> int | None:
    if not isinstance(tier, str) or tier not in TIER_ORDER:
        return None
    return TIER_ORDER.index(tier)


def _hand_tier_rose(sit: Any, snap) -> bool | None:
    """本街牌力档是否**高于**上一条街（转/河才有意义）。

    用同一条 `coach.read_hand` 读「hero 底牌 + 上一条街的牌面」，与
    `sit.hand_tier` 同一口径；只要有一边读不出来就不出声（fail open）。
    严格大于才算「上升」：持平不算，掉了也不算。
    """
    board = tuple(getattr(snap, "board", ()) or ())
    hero = tuple(getattr(snap, "hero_cards", ()) or ())
    if len(board) < 4 or len(hero) != 2:
        return None
    current = _tier_rank(getattr(sit, "hand_tier", None))
    previous = _tier_rank(_guard(_read_tier, hero, board[:-1]))
    if current is None or previous is None:
        return None
    return current > previous


def _read_tier(hero, board) -> str | None:
    """`read_hand(底牌, 牌面).tier`；牌串读不出来时返回 None。"""
    hole = parse_cards("".join(hero))
    cards = parse_cards("".join(board))
    tier = read_hand(hole, cards).tier
    return tier if isinstance(tier, str) and tier else None


def _turn_scare(snap) -> str | None:
    """转牌有多吓人（coach.turn_scare 的口径，转牌才写）。"""
    board = tuple(getattr(snap, "board", ()) or ())
    if len(board) < 4:
        return None
    return _guard(lambda: turn_scare(parse_cards("".join(board))))


def _af_class(af: Any) -> str | None:
    """AF → 攻击度档；0/缺失 = 没算出来（opponent_profile 用 0.0 表示没样本）。"""
    number = _num(af)
    if number is None or number <= 0:
        return None
    if number >= AF_HIGH:
        return "high"
    if number <= AF_LOW:
        return "low"
    return "medium"


def _ante_bb(snap) -> float | None:
    """前注（BB/人）；`Blinds.ante` 已经是 BB，这里不再换算。

    口径证据：`poker_ocr/parse.py:189-191` 把 `Blinds.sb/bb/ante` 的字段
    单位写成 BB，`parse_blinds` 在 `:219-221` 用 `ante_chips / bb_chips`
    把横幅里的筹码数折成 BB；`poker_ocr/prompt/system/__init__.py:52-53`
    也按「前注 {ante}BB」渲染。所以这里只做门控，绝不乘除。

    `0` 是**明确读到「没有前注」**，要出面——规则要靠 `ante_bb<=0` 把无前注
    桌与有前注桌切开（pf.rfi.first_in / pf.sb.complete.no_ante）；`None` 才是
    「读不到」，那时不出面（fail open）。
    """
    ante = getattr(getattr(snap, "blinds", None), "ante", None)
    number = _num(ante)
    return number if number is not None and number >= 0 else None


def _hand_label(sit: Any, snap) -> str | None:
    """翻前用 169 键（`AA`/`AKs`/`42s`），翻后沿用牌型标签。

    翻前 `Situation._read` 直接返回 None，所以 `sit.hand_label` 翻前恒缺；
    规则要写「QQ+/AK」这类翻前口径只能从底牌现算——`hand_key` 与范围表
    同一份契约，绝不另造一套命名。
    """
    if snap.street == PREFLOP:
        cards = tuple(getattr(snap, "hero_cards", ()) or ())
        if len(cards) != 2:
            return None
        key = _guard(hand_key, cards)
        return key if isinstance(key, str) and key else None
    return getattr(sit, "hand_label", None)


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
    _put(out, "opp_aggression", _af_class(getattr(read, "af", None)))


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
