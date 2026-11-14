"""对手范围的事件模型：按动作时间顺序更新范围。

设计：`docs/对手范围-事件模型-设计.md`。旧路径（`board_range.build`）是
「一条街一个 mode、一次过滤」——过牌会把上一街收窄的信息整个丢回翻前先验，
河牌「本街未行动」时范围又变回 100%。这里换成按手牌时间连乘：

    w(h) ∝ w0(h) × Π_k [ L(a_k | class(h, B_k)) × M(ctx_k, group(h)) ]^α

三块内容：

  · **分类** `HandClass(made, draw, group)`：从 `board_range.Features` 派生，
    不写第二套分类器；牌面特征按 board 缓存，1326 个 combo 只算一次。
    成牌档按**牌面百分位**定（这手牌在该牌面上比多少 combo 大），不是按
    牌型名：KK66 在 `K9642` 上是次坚果，在 `6K94KK` 上只是抓诈唬。
  · **似然** `L`：这一类手牌在该动作上的频率。频率表按**合法动作集**归一化
    （面对下注：跟/加/弃；无人下注：过/下注），所以空气面对大注的继续
    概率只有 6%，纯花听接近 85%——范围收窄正是从这里来的。设计稿写的是
    「四动作归一化到 1」，那样一次更新最多只差几倍，连乘三个动作也压不掉
    700 个空气 combo（本手停在 27.5%，结论仍是 call）；带弃牌项的频率表是
    同一公式的更严格实现，见设计文档 §12.1。
  · **上下文乘数** `M`：10 个桶 × 5 个成牌档 + SPR + 画像。桶乘数只作用在
    **非过牌**动作上：桶描述「这个位置/线路上的持续动作意味着什么」，过牌
    的信息已经由 `L` 给出；把 donk 的 value 乘数套在过牌上方向会反。
    成牌档细分成 5 档（而不是设计稿的 3 组）是为了让价值组**向坚果集中**：
    多人池的 donk-jam 里，同花该升、两对/顶对该降，一个 value×1.3 做不到。

两条不变量：

  · 听牌不会被成牌强度门槛砍掉（旧路径 `MODE_MIN_WEIGHT` 的问题），只被
    听牌类动作倾向加权；权重再乘四二法则的街因子（翻牌 1.0 / 转牌 0.5 /
    河牌 0），转牌听牌不会按翻牌的权重留在范围里；
  · 范围在整手牌里是**持续更新的状态**，不是每街从翻前先验重算。

本模块只改「对手范围怎么来」，不改 `hand_vs_ranges` 的权益算法。
"""

from __future__ import annotations

import bisect
import math
import os
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import board_range, ranges, strategy_tables
from .coach import (
    FLUSH, FULL_HOUSE, HIGH_CARD, ONE_PAIR, QUADS, STRAIGHT, STRAIGHT_FLUSH,
    TRIPS, TWO_PAIR,
)

# ─────────────────────────── 数据结构 ───────────────────────────

# 十个上下文桶（设计 §4.1）。顺序固定：桶乘数表与测试都按它读。
BUCKETS: tuple[str, ...] = (
    "donk_multiway", "donk_hu", "lead_multiway", "lead_hu", "stab",
    "closing_check", "facing_bet_closing", "facing_bet_open",
    "facing_bet_and_call", "facing_raise",
)

GROUPS: tuple[str, ...] = ("value", "semi_bluff", "bluff")
MADE_KINDS: tuple[str, ...] = ("nut", "strong", "medium", "weak", "air")
DRAW_KINDS: tuple[str, ...] = ("combo", "flush", "oesd", "gutshot", "backdoor",
                               "none")

@dataclass(frozen=True)
class ActionEvent:
    """对手的一个动作，带**行动前**的上下文。

    上下文一律取行动前状态；动作本身（check/bet/call/raise）是预测目标，
    不进上下文（设计 §1）。
    """

    street: str                  # flop / turn / river（翻前由先验覆盖）
    board: str                   # 动作发生时的牌面（3/4/5 张）
    seat_id: str
    action: str                  # check / call / bet / raise
    all_in: bool = False
    amount: float | None = None
    facing_bet: bool = False     # 行动前本街是否已有 bet/raise
    raises_before: int = 0       # 行动前本街的 bet/raise 次数（3-bet 判定）
    n_callers: int = 0           # 行动前、本街这个下注之后的跟注数
    players_behind: int = 0      # 行动前还能行动、且本街还没行动过的对手数
    is_first_to_act: bool = True
    is_last_to_act: bool = True
    closes_action: bool = False  # 这次跟注是否封口
    prev_street_aggressor: str | None = None
    is_donk: bool = False        # 口径 A：首个行动、下注进上一街 aggressor
    spr: float = 99.0            # 行动前 有效筹码 / 底池
    pot: float = 0.0

    def label(self) -> str:
        act = self.action
        if self.all_in and act in ("bet", "raise"):
            act = "jam" if act == "bet" else "jam-raise"
        if self.is_donk:
            act = "donk-" + act
        return f"{self.street}:{act}"


@dataclass(frozen=True)
class HandClass:
    """一手牌在该牌面上的（成牌档、听牌档、三组归属）。"""

    made: str                    # nut / strong / medium / weak / air
    draw: str                    # combo / flush / oesd / gutshot / backdoor / none
    group: str                   # value / semi_bluff / bluff


@dataclass(frozen=True)
class Composition:
    """范围在**当前牌面**上的分区（按权重），给提示词渲染用。

    `value/semi/bluff` 是三组占比（乘数的作用对象）；`made_hand/draw/air`
    是「成牌 / 听牌 / 空气」占比（给人看的口径）。两者不是同一个切法：
    一对是成牌，但在三组里属于 semi_bluff。
    """

    value: float = 0.0
    semi: float = 0.0
    bluff: float = 0.0
    made_hand: float = 0.0
    draw: float = 0.0
    air: float = 0.0
    # 成牌类型的加权占比，例如 (("同花", 0.45), ("葫芦", 0.18))。
    types: tuple[tuple[str, float], ...] = ()
    # 「封顶」用的两个细项：两对+（含三条/顺子/同花/葫芦/四条）和顶对。
    # 它们都在 made_hand 里，单独拆出来是因为「对手连续过牌后还剩多少强牌」
    # 不能从「一对 48%」里看出来——顶对和中对/底对对这个决策是两回事。
    two_pair_plus: float = 0.0
    top_pair: float = 0.0

    def __bool__(self) -> bool:
        return self.made_hand + self.draw + self.air > 0


# ─────────────────────────── 似然表 ───────────────────────────
#
# 频率表是「描述性先验」：按合法动作集归一化（弃牌项只影响相对水平，
# 不会单独渲染）。数值不追求精确，追求三件事：
#   1. 方向单调：成牌越强越敢跟/加，空气越倾向弃；
#   2. 量级够用：空气在大注前的继续概率 ~8%，强听 ~85%，差 10 倍；
#   3. 可手写：5 档成牌 × 动作，改动有据可查。

# 数值本体在 `poker_ocr/tables/range_flow.json`（P0 抽出，每个数字带来源）。
# 这里只加载；改数值不要改这个文件，改 JSON 或加一个变体再用 profile 切换。
#
# 面对下注：call / raise / fold。
_FACING_PROBS: dict[str, tuple[float, ...]] = strategy_tables.rows(
    "range_flow", "facing_probs")

# 无人下注：check / bet。
_UNFACING_PROBS: dict[str, tuple[float, ...]] = strategy_tables.rows(
    "range_flow", "unfacing_probs")

# 听牌对四个动作的乘性调整（check / bet / call / raise / fold）。听牌不被
# 成牌门槛砍掉，靠的就是这里把「跟注 / 下注」抬起来，同时把「弃牌」压掉：
# 一个纯花听面对 2/3 池下注几乎不会弃，而同样被归进 air 的纯空气会弃。
_DRAW_PROBS: dict[str, tuple[float, ...]] = strategy_tables.rows(
    "range_flow", "draw_probs")

# 听牌权重的街因子：听牌的价值随「还能发几张牌」线性缩水，就是四二法则——
# 翻牌 4 张（outs×4）、转牌 2 张（outs×2）、河牌 0 张。绝对倍数 4/2 会在
# 每个动作的合法动作集归一化里约掉（call/raise/fold 同乘一个数，比例不变），
# 所以这里存相对翻牌的比值：4/4、2/4、0/4。它作用在**类别权重**上，不作用
# 在权益上；权益仍由 `hand_vs_ranges` 按剩余牌 runout 精确算。
_DRAW_STREET_FACTOR: dict[int, float] = {
    int(k): float(v) for k, v in
    strategy_tables.rows("range_flow", "draw_street_factor").items()}


def draw_street_factor(board_len: int) -> float:
    """听牌权重按剩余发牌张数缩放：翻牌 1.0、转牌 0.5、河牌 0。"""
    try:
        return _DRAW_STREET_FACTOR.get(int(board_len), 1.0)
    except (TypeError, ValueError):
        return 1.0


# 桶 × 成牌档（设计 §4.2 的细化）：设计稿给的是 3 组乘数，但「价值组」
# 内部还要**向坚果集中**——多人池的 donk-jam 里，坚果该升、两对/顶对该降，
# 用一个 value×1.3 同时作用在两者上做不到。所以把 3 组拆成 5 档，
# 组级语义仍由 `group_multiplier` 提供（value 取 strong 档）。
# 列：nut / strong / medium / weak / air。
_BUCKET_TIER: dict[str, tuple[float, ...]] = strategy_tables.rows(
    "range_flow", "bucket_tier")

# SPR 只调「主动进攻」的倾向：短码时中等成牌也愿意打光（设计 §4.3）。
_SPR_AGGRESSION: dict[str, dict[str, float]] = strategy_tables.rows(
    "range_flow", "spr_aggression")

# 画像只调两个旋钮（设计 §4.3）：诈唬系数、跟注宽度。
_ARCH_BLUFF: dict[str, float] = strategy_tables.rows("range_flow", "arch_bluff")
_ARCH_CALL: dict[str, float] = strategy_tables.rows("range_flow", "arch_call")

# 似然下限：不把「必弃」写成 0，否则一个动作就能把整类手牌清零。
_MIN_LIKELIHOOD = strategy_tables.value("range_flow", "min_likelihood")

# 支撑集下限：连乘后权重低于「最大权重 × 这个比例」的 combo 不进范围。
# 没有它，1326 个 combo 全非零，宽度永远是 100%，「范围 6%」这个事实就
# 没法给模型；有它，被压到极低的空气才会真正退出支撑集。取值只影响
# 渲染宽度与尾部质量（<1%），不影响权益的主排序。
_SUPPORT_FLOOR = strategy_tables.value("range_flow", "support_floor")
_SUPPORT_FLOOR_ENV = "RANGE_EVENT_SUPPORT_FLOOR"
# 温度 α：短线路（≤3 个动作）按精确更新，4 个以上才降温（见
# `effective_alpha`）。设计稿默认 0.7，实测 3 个动作下会压掉信息，见 §12.4。
_DEFAULT_ALPHA = strategy_tables.value("range_flow", "default_alpha")
_ALPHA_ENV = "RANGE_EVENT_ALPHA"


def spr_bucket(spr: float | None) -> str:
    """有效筹码 / 底池 → 五档。None/异常按深码处理。"""
    try:
        value = float(spr)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "4+"
    if value < 0.5:
        return "<0.5"
    if value < 1.0:
        return "0.5-1"
    if value < 2.0:
        return "1-2"
    if value < 4.0:
        return "2-4"
    return "4+"


def bucket_of(event: ActionEvent) -> str:
    """动作前的状态 → 十个桶之一（设计 §4.1）。

    判定顺序：面对下注 → 首个行动（donk 优先于 lead）→ 末位过牌 / stab。
    全下不是独立桶：`all_in` 只是同一动作的属性，倾向由 `spr_bucket` 调。
    """
    if event.facing_bet:
        if event.raises_before >= 2:
            return "facing_raise"
        if event.n_callers >= 1:
            return "facing_bet_and_call"
        if event.players_behind <= 0:
            return "facing_bet_closing"
        return "facing_bet_open"
    if event.is_first_to_act and event.players_behind >= 1:
        if event.is_donk:
            return "donk_hu" if event.players_behind == 1 else "donk_multiway"
        return "lead_hu" if event.players_behind == 1 else "lead_multiway"
    if event.players_behind <= 0:
        return "closing_check"
    return "stab"


# ─────────────────────────── 分类 ───────────────────────────

# 成牌档的百分位阈值：这手牌在该牌面上比多少 combo 大。
# 绝对牌型（「两对」）在干燥面和成花面上不是同一回事：KK66 在 K9642 上
# 是次坚果，在 K9696+成花面上只是抓诈唬。所以档位按**牌面百分位**定，
# 牌型名只用于渲染。
_TIER_PERCENTILE = tuple(
    (float(limit), str(tier))
    for limit, tier in strategy_tables.items("range_flow", "tier_percentile"))


def _tier_for_percentile(better: float) -> str:
    for limit, tier in _TIER_PERCENTILE:
        if better <= limit:
            return tier
    return "air"


def _class_of(feat: board_range.Features,
              better: float | None = None) -> HandClass:
    """`Features` + 牌面百分位 → `HandClass`。成牌档、听牌档、三组一次定完。

    `better` = 这张牌面上有多少比例的 combo 严格大于它。None 时退回按牌型
    的粗映射（单测/没有牌面统计的调用）。
    """
    cat = feat.category
    if better is not None:
        made = _tier_for_percentile(better)
    elif cat in (QUADS, STRAIGHT_FLUSH) or feat.nut_flush:
        made = "nut"
    elif cat in (FULL_HOUSE, FLUSH, STRAIGHT, TRIPS):
        made = "strong"
    elif cat == TWO_PAIR:
        made = "medium"
    elif cat == ONE_PAIR:
        # 超对/顶对是价值下限；中对/底对/公对只能算弱成牌。踢脚分不出来，
        # 也不用分：顶对弱踢本来就该在 medium 与 weak 之间，取 medium 更保守。
        made = "medium" if feat.pair_kind in ("overpair", "top") else "weak"
    else:
        made = "air"

    if feat.flush_draw and feat.straight_outs >= 2:
        draw = "combo"
    elif feat.flush_draw:
        draw = "flush"
    elif feat.straight_outs >= 2:
        draw = "oesd"
    elif feat.straight_outs == 1:
        draw = "gutshot"
    elif feat.backdoor_flush or feat.backdoor_straight:
        draw = "backdoor"
    else:
        draw = "none"

    # 三组按**实际牌型**归属，不按百分位档：底对可能因为牌面凶险落到 air 档
    # （似然该按弱牌算），但它仍是成牌，不能算进「诈唬」——否则画像的诈唬
    # 系数会作用到成牌上，"你只赢诈唬" 的警告也会把底对算成诈唬。
    if cat >= ONE_PAIR:
        group = "value" if made in ("nut", "strong", "medium") else "semi_bluff"
    elif draw in ("combo", "flush", "oesd"):
        group = "semi_bluff"
    else:
        group = "bluff"
    return HandClass(made=made, draw=draw, group=group)


_MADE_LABELS = {
    QUADS: "四条",
    STRAIGHT_FLUSH: "同花顺",
    FULL_HOUSE: "葫芦",
    FLUSH: "同花",
    STRAIGHT: "顺子",
    TRIPS: "三条",
    TWO_PAIR: "两对",
    ONE_PAIR: "一对",
    HIGH_CARD: "空气",
}


def _made_label(feat: board_range.Features) -> str:
    # 「打牌面」只给公顺/公花/公葫芦这种**整手都在牌面**的牌型。一对时
    # plays_board 也可能为 True（公对 + 我的踢脚），但踢脚仍参与比牌，
    # 渲染成「打牌面」会让模型误以为这手牌在分池。
    if feat.plays_board and feat.category >= STRAIGHT:
        return "打牌面"
    return _MADE_LABELS.get(feat.category, "空气")


# 牌面 → {combo: (HandClass, 成牌中文标签)}。和 board_range 的特征缓存一样
# 按 board 复用；装的是纯数据，调用方只读。
_CLASS_CACHE: dict[tuple[tuple[int, str], ...],
                   dict[ranges.Combo, tuple[HandClass, str, int]]] = {}
_CLASS_CACHE_MAX = 64


def _board_key(board) -> tuple[tuple[int, str], ...]:
    cards = board_range.as_cards(board)
    return tuple(sorted(cards))


def _class_map(board) -> dict[ranges.Combo, tuple[HandClass, str, int, str]]:
    key = _board_key(board)
    cached = _CLASS_CACHE.get(key)
    if cached is not None:
        return cached
    entries = board_range.features_for_board(key)
    # 牌面百分位：同一个牌型（比如「两对」）在干燥面和成花面上强度差很远。
    # 先按 evaluate 的 (牌型, 踢脚) 排序，再算每个 combo 有多少比例比它大。
    scores = sorted((feat.category, feat.tiebreak) for _c, feat, _s in entries)
    total = len(scores) or 1
    better_cache: dict[tuple, float] = {}
    table: dict[ranges.Combo, tuple[HandClass, str, int, str]] = {}
    for combo, feat, _score in entries:
        score = (feat.category, feat.tiebreak)
        better = better_cache.get(score)
        if better is None:
            above = total - bisect.bisect_right(scores, score)
            better = above / total
            better_cache[score] = better
        table[combo] = (_class_of(feat, better), _made_label(feat),
                        feat.category, feat.pair_kind)
    if len(_CLASS_CACHE) >= _CLASS_CACHE_MAX:
        _CLASS_CACHE.clear()
    _CLASS_CACHE[key] = table
    return table


def hand_class(combo, board) -> HandClass:
    """公开分类入口：combo + 当时的牌面 → HandClass。

    `combo` 接受 'AsKh' / ('As','Kh') / ((12,'s'),(11,'h'))；和公共牌撞牌
    的 combo 返回空气/ bluff，调用方应先按 `dead` 剔除。
    """
    cards = board_range.as_cards(combo)
    if len(cards) != 2:
        return HandClass("air", "none", "bluff")
    entry = _class_map(board).get(ranges.canon_combo(*cards))
    return entry[0] if entry else HandClass("air", "none", "bluff")


# ─────────────────────────── 似然 ───────────────────────────

# 组 → 代表档：只有用于「组级」查询与旧口径的单调性测试；实际似然按
# 5 个成牌档取乘数，这样价值组内部才能向坚果集中。
_GROUP_TIER = {"value": "strong", "semi_bluff": "weak", "bluff": "air"}


def tier_multiplier(bucket: str, made: str) -> float:
    """桶 × 成牌档乘数。未知桶按 1.0，不猜。"""
    row = _BUCKET_TIER.get(bucket)
    if row is None:
        return 1.0
    try:
        return row[MADE_KINDS.index(made)]
    except ValueError:
        return 1.0


def group_multiplier(bucket: str, group: str) -> float:
    """桶 × 三组乘数（设计 §4.2 的组级口径）。

    value 取 strong 档做代表：组级表只用于「方向」与对外解释，实际
    `likelihood` 用的是 `tier_multiplier`。
    """
    return tier_multiplier(bucket, _GROUP_TIER.get(group, "air"))


def _context_multiplier(action: str, cls: HandClass, bucket: str,
                        spr: str, archetype: str, *,
                        all_in: bool) -> float:
    """桶/SPR/画像的乘性修正。过牌不乘——见模块 docstring。"""
    if action == "check":
        return 1.0
    mult = tier_multiplier(bucket, cls.made)
    if action in ("bet", "raise"):
        mult *= _SPR_AGGRESSION.get(spr, {}).get(cls.made, 1.0)
        if cls.group == "bluff":
            mult *= _ARCH_BLUFF.get(archetype, 1.0)
        if all_in and spr in ("<0.5", "0.5-1"):
            mult *= 1.10
    elif action == "call":
        mult *= _ARCH_CALL.get(archetype, 1.0)
    return mult


def _facing_probability(action: str, cls: HandClass) -> float:
    call, raise_, fold = _FACING_PROBS[cls.made]
    _check, _bet, boost_call, boost_raise, boost_fold = _DRAW_PROBS[cls.draw]
    call *= boost_call
    raise_ *= boost_raise
    fold *= boost_fold
    total = call + raise_ + fold
    if total <= 0:
        return _MIN_LIKELIHOOD
    if action == "call":
        return max(call / total, _MIN_LIKELIHOOD)
    # 面对下注时记成 bet 的，本质是一次 raise（下注额高于当前水位）。
    return max(raise_ / total, _MIN_LIKELIHOOD)


def _unfacing_probability(action: str, cls: HandClass) -> float:
    check, bet = _UNFACING_PROBS[cls.made]
    boost_check, boost_bet, _bc, _br, _bf = _DRAW_PROBS[cls.draw]
    check *= boost_check
    bet *= boost_bet
    total = check + bet
    if total <= 0:
        return _MIN_LIKELIHOOD
    if action == "check":
        return max(check / total, _MIN_LIKELIHOOD)
    return max(bet / total, _MIN_LIKELIHOOD)


def likelihood(action: str, cls: HandClass, *, facing: bool, bucket: str,
               spr_bucket: str = "2-4", archetype: str = "unknown",
               all_in: bool = False) -> float:
    """`L(a | class, ctx)`：这一类手牌在该动作上的频率 × 上下文乘数。

    返回的是**未按动作归一化**的分数（乘数会改变三组的相对比例）；它只在
    `build_range` 里和其他 combo 比较，所以不需要是一个概率分布。
    """
    act = (action or "").strip().lower()
    if act in ("allin", "jam"):
        act = "raise"
    if act not in ("check", "call", "bet", "raise"):
        return 0.5
    if facing and act in ("call", "bet", "raise"):
        base = _facing_probability(act, cls)
    elif not facing and act in ("check", "bet", "raise"):
        base = _unfacing_probability(act, cls)
    else:
        # 口径不匹配（未面对下注却记成跟注 / 面对下注却记成过牌）：
        # 给中性值，不放大也不清零。
        return 0.5
    return base * _context_multiplier(act, cls, bucket, spr_bucket, archetype,
                                      all_in=all_in)


def event_factor(event: ActionEvent, cls: HandClass, *,
                 archetype: str = "unknown") -> float:
    """一个事件对一类手牌的乘数（未取 α 次幂）。

    听牌再乘一个街因子（四二法则）：翻牌 ×1.0（还剩 4 张发牌）、转牌 ×0.5
    （2 张）、河牌 ×0（没有下一张）。不这么做的话，同一手花听在转牌仍按
    翻牌的权重留在范围里，转牌听牌占比会虚高。后门听只在翻牌成立，不缩放。
    """
    factor = likelihood(
        event.action, cls,
        facing=event.facing_bet,
        bucket=bucket_of(event),
        spr_bucket=spr_bucket(event.spr),
        archetype=archetype,
        all_in=event.all_in,
    )
    if cls.draw in ("combo", "flush", "oesd", "gutshot"):
        # event.board 是拼起来的字符串（每张牌两个字符），换成张数再查表。
        factor *= draw_street_factor(len(event.board) // 2)
    return factor


# ─────────────────────────── 构建范围 ───────────────────────────

def _env_alpha() -> float:
    raw = os.environ.get(_ALPHA_ENV, "").strip()
    if not raw:
        return _DEFAULT_ALPHA
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_ALPHA
    return min(max(value, 0.05), 2.0)


def effective_alpha(alpha: float, n_events: int) -> float:
    """按动作个数给温度：3 个以内按给定值，4 个以上逐次降温。

    设计稿担心「连乘 4–5 个动作会过度自信」，但用 0.7 去压 3 个动作会把
    信息也一起压掉（本手第一次河牌决策停在 27.5%，高于 24.1% 的跟注门槛，
    结论仍然是 call）。这里改成按动作个数分段：短线路用精确更新，长线路
    才降温，下限 0.5，避免 5 个动作把范围压成两三个 combo。
    """
    if n_events <= 3:
        return alpha
    return max(0.5, alpha * (0.97 ** (n_events - 3)))


def _env_support_floor() -> float:
    raw = os.environ.get(_SUPPORT_FLOOR_ENV, "").strip()
    if not raw:
        return _SUPPORT_FLOOR
    try:
        value = float(raw)
    except ValueError:
        return _SUPPORT_FLOOR
    return value if value > 0 else _SUPPORT_FLOOR


def _normalize(weights: dict[ranges.Combo, float]) -> float:
    total = sum(w for w in weights.values() if w > 0)
    if total <= 0:
        return 0.0
    for combo, w in weights.items():
        weights[combo] = w / total
    return total


# 同一个决策点可能被算多次（模型重试、复盘重放）：范围对象复用后，
# `hand_vs_ranges` 的整块缓存（按 id(rng) 做键）才命中。
_BUILD_CACHE: dict[tuple, ranges.Range | None] = {}
_BUILD_CACHE_MAX = 64


# ─────────────────────────── trace 旁路 ───────────────────────────
#
# 采集侵入面的硬约束：**关掉 trace 时 build_range 逐位不变**。所以 trace 只读
# 中间量、不改乘除顺序；trace 路径也不读不写 `_BUILD_CACHE`（缓存键不含
# profile 与 detail 上限，复用会把上一次的记录形状带出来）。字段口径见
# docs/范围推导-trace-字段契约-v1.md。

TRACE_DETAIL_MAX = 400


def _cards_of(text: str) -> frozenset:
    """'Kd7c2s' → {('K','d'), ...}：牌面字符串 → (排名, 花色) 集合。"""
    cards = set()
    raw = text or ""
    for i in range(0, len(raw) - 1, 2):
        rank = ranges.RANK_CHARS.find(raw[i].upper())
        if rank >= 0:
            cards.add((rank, raw[i + 1].lower()))
    return frozenset(cards)


def _grid_array(weights: dict) -> list:
    """combo 权重 → 169 格占比（契约 §4 的平铺顺序）。"""
    grid = [0.0] * 169
    total = sum(w for w in weights.values() if w > 0)
    if total <= 0:
        return grid
    for combo, weight in weights.items():
        if weight <= 0:
            continue
        index = ranges.key_index(ranges.key_of_combo(combo))
        if index >= 0:
            grid[index] += weight / total
    return [round(value, 6) for value in grid]


def _cell_counts(weights: dict) -> list:
    """每个 169 键下的 combo 数（与 detail 同一取数口径）。"""
    counts = [0] * 169
    for combo, weight in weights.items():
        if weight <= 0:
            continue
        index = ranges.key_index(ranges.key_of_combo(combo))
        if index >= 0:
            counts[index] += 1
    return counts


def _likelihood_parts(action: str, cls: HandClass, *, facing: bool, bucket: str,
                      spr_bucket: str = "2-4", archetype: str = "unknown",
                      all_in: bool = False) -> tuple[float, float, float]:
    """(L, L0, M)：L0 是动作频率本身，M 是上下文乘数，L = L0 × M。

    只给 trace 拆归因用；数值与 `likelihood` 逐位一致（同一个乘积）。
    """
    act = (action or "").strip().lower()
    if act in ("allin", "jam"):
        act = "raise"
    if act not in ("check", "call", "bet", "raise"):
        return 0.5, 0.5, 1.0
    if facing and act in ("call", "bet", "raise"):
        base = _facing_probability(act, cls)
    elif not facing and act in ("check", "bet", "raise"):
        base = _unfacing_probability(act, cls)
    else:
        return 0.5, 0.5, 1.0
    mult = _context_multiplier(act, cls, bucket, spr_bucket, archetype,
                               all_in=all_in)
    return base * mult, base, mult


def _factor_parts(event: ActionEvent, cls: HandClass,
                  archetype: str) -> tuple[float, float, float, float]:
    """(L 生效值, L 夹取前, M 上下文乘数, D 街因子)，单位是「未取 α 次幂」。"""
    raw, _base, mult = _likelihood_parts(
        event.action, cls,
        facing=event.facing_bet,
        bucket=bucket_of(event),
        spr_bucket=spr_bucket(event.spr),
        archetype=archetype,
        all_in=event.all_in,
    )
    draw = 1.0
    if cls.draw in ("combo", "flush", "oesd", "gutshot"):
        draw = draw_street_factor(len(event.board) // 2)
    pre = raw * draw
    return max(pre, _MIN_LIKELIHOOD), pre, mult, draw


def _event_payload(event: ActionEvent) -> dict:
    """ActionEvent 全字段。spr 的 99 是「拿不到筹码」的哨兵，落盘写 null。"""
    try:
        spr = float(event.spr)
    except (TypeError, ValueError):
        spr = 99.0
    return {
        "actor": event.seat_id,
        "action": event.action,
        "amount": None if event.amount is None else round(float(event.amount), 3),
        "pot_before": round(float(event.pot or 0.0), 3),
        "spr_before": None if spr >= 99.0 else round(spr, 3),
        "facing": bool(event.facing_bet),
        "all_in": bool(event.all_in),
        "raises_before": int(event.raises_before),
        "n_callers": int(event.n_callers),
        "players_behind": int(event.players_behind),
        "is_first_to_act": bool(event.is_first_to_act),
        "is_last_to_act": bool(event.is_last_to_act),
        "closes_action": bool(event.closes_action),
        "prev_aggressor": event.prev_street_aggressor,
        "is_donk": bool(event.is_donk),
    }


def prior_payload(prior: ranges.Range) -> dict:
    """先验的 step-0 快照（契约 §3.3.1）；board_range 路径也要用。"""
    return _prior_payload(prior)


def final_summary(rng: ranges.Range, *, dropped: dict | None = None,
                  rho: float = 1.0) -> dict:
    """最终范围摘要（契约 §3.3 的 `final`）。没有剪枝时 rho=1。"""
    peak = max(rng.combo_weights.values()) if rng.combo_weights else 0.0
    return {
        "width_pct": round(rng.width_pct, 2),
        "weighted_width": round(rng.weighted_width, 2),
        "effective_width_pct": round(rng.effective_width_pct(), 2),
        "peak": round(peak, 6),
        "rho": round(rho, 4),
        "combo_count": rng.combo_count,
        "dropped": dropped or {"dead": 0, "zero": 0, "floor": 0},
        # v1.2：final 也要 169 热图（看板默认停在这一步）。取的是剪枝后
        # 支撑集内的归一化份额，所以和 width_pct 同一批 combo 算出来的。
        "grid": _grid_array(dict(rng.combo_weights)),
    }


def _prior_payload(prior: ranges.Range) -> dict:
    return {
        "id": prior.id,
        "version": prior.version,
        "source": prior.source,
        "width_pct": round(prior.width_pct, 2),
        "weighted_width": round(prior.weighted_width, 2),
        "combo_count": prior.combo_count,
        "grid": _grid_array(dict(prior.normalized())),
    }


def _resolve_alpha(alpha: float | None, n_events: int) -> tuple[float, float]:
    """(alpha_in, alpha_eff)：与 build_range 原来的取值顺序逐位一致。"""
    if alpha is None:
        alpha = _env_alpha()
    try:
        alpha = float(alpha)
    except (TypeError, ValueError):
        alpha = _DEFAULT_ALPHA
    alpha_in = min(max(alpha, 0.05), 2.0)
    return alpha_in, effective_alpha(alpha_in, n_events)


def _live_view(weights: dict, removed: set) -> tuple[dict, float]:
    """本步的存活支撑集（已删 dead 不收，本步刚死的仍收）与它的质量。"""
    live = {c: w for c, w in weights.items() if w > 0 and c not in removed}
    return live, sum(live.values())


def _step_payload(*, index: int, event: ActionEvent, alpha_eff: float,
                  weights: dict, before: dict, removed: set,
                  dead_now: frozenset, factor_parts: dict, classes: dict,
                  collapsed: int, last: bool, max_rows: int) -> tuple[dict, set]:
    """一步的留痕。返回 (payload, 本步之后要从视图里删掉的 combo 集合)。"""
    live, total = _live_view(weights, removed)
    prev_live, prev_total = _live_view(before, removed)
    shares = {c: w / total for c, w in live.items()} if total > 0 else {}
    prev_shares = ({c: w / prev_total for c, w in prev_live.items()}
                   if prev_total > 0 else {})
    dying = {c for c in live if c[0] in dead_now or c[1] in dead_now}
    width_before = len(prev_live) / ranges.TOTAL_COMBOS * 100
    width_after = len(live) / ranges.TOTAL_COMBOS * 100

    rows = []
    for combo in set(prev_shares) | set(shares):
        s0 = prev_shares.get(combo, 0.0)
        s1 = shares.get(combo, 0.0)
        dw = s1 - s0
        if combo in dying:
            cut = "dead"
        elif s1 <= 0 and s0 > 0:
            cut = "zero"
        else:
            cut = None
        entry = classes.get(combo)
        cls = entry[0] if entry else None
        parts = factor_parts.get(cls) if cls is not None else None
        w0 = before.get(combo, 0.0)
        w1 = weights.get(combo, 0.0)
        rows.append({
            "c": ranges.combo_text(combo),
            "dw": dw,
            "w0": w0,
            "w1": w1,
            "s0": s0,
            "s1": s1,
            "lr": None if (w0 <= 0 or w1 <= 0) else _safe_log(w1 / w0),
            "l": None if parts is None else round(parts[0], 6),
            "l_pre": None if parts is None else round(parts[1], 6),
            "m": None if parts is None else round(parts[2], 4),
            "d": None if parts is None else round(parts[3], 4),
            "group": cls.group if cls is not None else None,
            "made": cls.made if cls is not None else None,
            "draw": cls.draw if cls is not None else None,
            "cut": cut,
            "combo": combo,
        })

    rows.sort(key=lambda r: (-abs(r["dw"]), r["c"]))
    lost = [r for r in rows if r["dw"] < 0][:10]
    gained = [r for r in rows if r["dw"] > 0][:10]
    chosen = {id(r) for r in lost} | {id(r) for r in gained}
    rest = [r for r in rows if id(r) not in chosen]
    top = [_top_row(r) for r in lost + gained]
    if rest:
        top.append({"others": {"n": len(rest),
                               "sum_dw": round(sum(r["dw"] for r in rest), 6)}})

    detail = []
    truncated = False
    if max_rows > 0:
        support = sorted((r for r in rows if r["s1"] > 0),
                         key=lambda r: (-r["s1"], r["c"]))
        detail_n = len(support)
        truncated = detail_n > max_rows
        for row in support[:max_rows]:
            line = [row["c"], round(row["s1"], 6), round(row["dw"], 6),
                    row["l"], row["cut"]]
            if last:
                line.extend([row["group"], row["made"], row["draw"]])
            detail.append(line)
    else:
        detail_n = 0

    payload = {
        "i": index,
        "street": event.street,
        "event": _event_payload(event),
        "bucket": bucket_of(event),
        "spr_bucket": spr_bucket(event.spr),
        "alpha_k": round(alpha_eff, 4),
        "z_k": None if prev_total <= 0 else round(total / prev_total, 6),
        "mass_before": round(prev_total, 6),
        "mass_after": round(total, 6),
        "width_before": round(width_before, 2),
        "width_after": round(width_after, 2),
        "grid": _grid_array(live),
        "dropped": {"dead": len(dying), "zero": collapsed, "floor": 0},
        "top": top,
        "cell_n": _cell_counts(live),
        "detail_n": detail_n,
        "truncated": truncated,
        "detail": detail,
        "skipped": None,
    }
    return payload, dying


def _top_row(row: dict) -> dict:
    return {
        "c": row["c"],
        "dw": round(row["dw"], 6),
        "w0": round(row["w0"], 6),
        "w1": round(row["w1"], 6),
        "lr": None if row["lr"] is None else round(row["lr"], 4),
        "l": row["l"],
        "l_pre": row["l_pre"],
        "m": row["m"],
        "d": row["d"],
        "group": row["group"],
        "made": row["made"],
        "draw": row["draw"],
        "cut": row["cut"],
    }


def _safe_log(value: float) -> float | None:
    try:
        if value <= 0:
            return None
        return math.log(value)
    except (ValueError, OverflowError):
        return None


def _build_core(prior: ranges.Range | None, events: Sequence[ActionEvent], *,
                alpha_in: float, alpha_eff: float, archetype: str = "unknown",
                dead_set: frozenset = frozenset(),
                support_floor: float | None = None,
                trace: dict | None = None,
                ) -> tuple[ranges.Range | None, dict | None]:
    """`build_range` 的唯一实现。`trace` 非 None 时同时产出逐步留痕。"""
    if prior is None or not events:
        return None, None
    if support_floor is None:
        support_floor = _env_support_floor()

    weights: dict[ranges.Combo, float] = {
        combo: float(w) for combo, w in prior.normalized().items() if w > 0}
    if not weights:
        return None, None

    traced = trace is not None
    max_rows = int(trace.get("max_rows", TRACE_DETAIL_MAX)) if traced else 0
    max_ms = float(trace.get("max_ms", 6.0)) if traced else 0.0
    board_now = events[-1].board or ""
    hero_dead = frozenset(dead_set) - _cards_of(board_now)
    steps: list = []
    removed: set = set()
    omitted: list = []
    trace_ms = 0.0
    used: list[str] = []
    n_applied = 0

    for index, event in enumerate(events):
        classes = _class_map(event.board)
        if not classes:
            if traced:
                steps.append({
                    "i": index, "street": event.street,
                    "event": _event_payload(event),
                    "bucket": bucket_of(event),
                    "spr_bucket": spr_bucket(event.spr),
                    "alpha_k": round(alpha_eff, 4), "z_k": 1.0,
                    "mass_before": None, "mass_after": None,
                    "width_before": None, "width_after": None,
                    "grid": [], "dropped": {"dead": 0, "zero": 0, "floor": 0},
                    "top": [], "cell_n": [], "detail_n": 0,
                    "truncated": False, "detail": [],
                    "skipped": "no hand classes for this board",
                })
            continue
        factors: dict[HandClass, float] = {}
        for cls in {entry[0] for entry in classes.values()}:
            factors[cls] = max(event_factor(event, cls, archetype=archetype),
                               _MIN_LIKELIHOOD) ** alpha_eff
        parts: dict = {}
        if traced:
            for cls in {entry[0] for entry in classes.values()}:
                parts[cls] = _factor_parts(event, cls, archetype)
        before = dict(weights) if traced else {}
        dead_now = hero_dead | _cards_of(event.board)
        collapsed = 0
        for combo in weights:
            cls_entry = classes.get(combo)
            # 和这个牌面撞牌的 combo 不可能是他的手牌，直接归零。
            weights[combo] *= factors.get(cls_entry[0], 0.0) if cls_entry else 0.0
        if traced:
            collapsed = sum(1 for c, w in before.items()
                            if w > 0 and weights.get(c, 0.0) <= 0
                            and c not in removed
                            and c[0] not in dead_now and c[1] not in dead_now)
        if _normalize(weights) <= 0:
            return None, None
        used.append(event.label())
        n_applied += 1
        if traced:
            started = time.perf_counter()
            detail_budget = max_rows if trace_ms <= max_ms else 0
            if detail_budget <= 0:
                omitted.append(index)
            payload, dying = _step_payload(
                index=index, event=event, alpha_eff=alpha_eff, weights=weights,
                before=before, removed=removed, dead_now=dead_now,
                factor_parts=parts, classes=classes, collapsed=collapsed,
                last=(index == len(events) - 1), max_rows=detail_budget)
            steps.append(payload)
            trace_ms += (time.perf_counter() - started) * 1000.0
            removed |= dying

    dead_mass = 0.0
    dead_count = 0
    if dead_set:
        for combo in list(weights):
            if combo[0] in dead_set or combo[1] in dead_set:
                # 只数支撑集里的（w>0）：早先被似然归零的不算「这一步删掉的」。
                if weights[combo] > 0:
                    dead_mass += weights[combo]
                    dead_count += 1
                del weights[combo]
    if not weights:
        return None, None
    _normalize(weights)

    peak = max(weights.values())
    floor = peak * support_floor
    kept = 1.0
    floor_count = 0
    if floor > 0:
        kept = sum(w for w in weights.values() if w >= floor) or 1.0
        floor_count = sum(1 for w in weights.values() if w < floor)
        weights = {c: w for c, w in weights.items() if w >= floor}
    if not weights:
        return None, None

    rid = f"evt:{prior.id}"
    built = ranges.make_range(
        rid, weights,
        source=f"event-model × {prior.source or prior.id}",
        note=" → ".join(used[-4:]),
        prior_id=prior.id,
    )
    if not traced:
        return built, None

    trail = {
        "path": "event_model",
        "prior": _prior_payload(prior),
        "steps": steps,
        "params": {
            "alpha_in": round(alpha_in, 4),
            "alpha_eff": round(alpha_eff, 4),
            "n_events": len(events),
            "n_applied": n_applied,
            "support_floor": round(float(support_floor), 6),
            "min_likelihood": _MIN_LIKELIHOOD,
        },
        "detail_omitted": omitted,
        "trace_ms": round(trace_ms, 2),
    }
    if built is not None:
        trail["final"] = final_summary(
            built,
            dropped={"dead": dead_count, "zero": 0, "floor": floor_count},
            rho=kept)
    return built, trail


def build_range(prior: ranges.Range | None, events: Sequence[ActionEvent], *,
                alpha: float | None = None, archetype: str = "unknown",
                dead: Iterable = (), support_floor: float | None = None,
                ) -> ranges.Range | None:
    """按时间顺序更新先验，返回收窄后的范围。

    · 温度 `α`：默认 1.0；动作数超过 3 时由 `effective_alpha` 自动降温，
      避免连乘把范围压成两三个 combo；
    · `dead`：hero + 最终牌面的牌，先剔除再算宽度/分区，否则宽度永远虚高；
    · `support_floor`：相对支撑集下限，见 `_SUPPORT_FLOOR`。
    """
    if prior is None or not events:
        return None
    if support_floor is None:
        support_floor = _env_support_floor()
    alpha_in, alpha_eff = _resolve_alpha(alpha, len(events))
    dead_set = frozenset(dead)
    key = (prior.id, prior.version, tuple(events), round(alpha_eff, 4), archetype,
           dead_set, round(float(support_floor), 6))
    if key in _BUILD_CACHE:
        return _BUILD_CACHE[key]
    built, _trail = _build_core(
        prior, events, alpha_in=alpha_in, alpha_eff=alpha_eff,
        archetype=archetype, dead_set=dead_set, support_floor=support_floor)
    _store_cache(key, built)
    return built


def build_range_traced(prior: ranges.Range | None, events: Sequence[ActionEvent], *,
                       alpha: float | None = None, archetype: str = "unknown",
                       dead: Iterable = (), support_floor: float | None = None,
                       max_rows: int = TRACE_DETAIL_MAX,
                       max_ms: float = 6.0,
                       ) -> tuple[ranges.Range | None, dict | None]:
    """同 `build_range`，但不读不写 `_BUILD_CACHE`，并返回逐步留痕。

    逐位一致的边界：本函数返回的 `Range` 与 `build_range` 在同一输入上
    **完全相同**（同一个 `_build_core`，dead 仍在最后统一剔除）。每步删 dead
    只作用于留痕视图（grid/detail/width），不改连乘与归一化顺序。
    """
    if prior is None or not events:
        return None, None
    alpha_in, alpha_eff = _resolve_alpha(alpha, len(events))
    return _build_core(
        prior, events, alpha_in=alpha_in, alpha_eff=alpha_eff,
        archetype=archetype, dead_set=frozenset(dead),
        support_floor=support_floor,
        trace={"max_rows": max_rows, "max_ms": max_ms})


def _store_cache(key: tuple, built: ranges.Range | None) -> None:
    if len(_BUILD_CACHE) >= _BUILD_CACHE_MAX:
        _BUILD_CACHE.clear()
    _BUILD_CACHE[key] = built


def composition(rng: ranges.Range | None, board) -> Composition:
    """加权范围在**当前牌面**上的成牌/听牌/空气分区。"""
    if rng is None:
        return Composition()
    table = _class_map(board)
    total = 0.0
    value = semi = bluff = 0.0
    made_hand = draw_share = air = 0.0
    two_pair_plus = top_pair = 0.0
    types: dict[str, float] = {}
    for combo, weight in rng.combo_weights.items():
        entry = table.get(combo)
        if entry is None or weight <= 0:
            continue
        cls, label, category, pair_kind = entry
        total += weight
        if cls.group == "value":
            value += weight
        elif cls.group == "semi_bluff":
            semi += weight
        else:
            bluff += weight
        # 「成牌 / 听牌 / 空气」按**实际牌型**分，不按百分位档：一张高牌
        # 也可能落在 weak 档（档位是相对的），但它不是成牌。
        if category >= ONE_PAIR:
            made_hand += weight
            if category >= TWO_PAIR:
                two_pair_plus += weight
            elif pair_kind == "top":
                top_pair += weight
        elif cls.draw in ("combo", "flush", "oesd", "gutshot"):
            draw_share += weight
        else:
            air += weight
        types[label] = types.get(label, 0.0) + weight
    if total <= 0:
        return Composition()
    ranked = sorted(types.items(), key=lambda kv: -kv[1])
    shown = tuple((name, w / total) for name, w in ranked if w / total >= 0.01)[:4]
    return Composition(
        value=value / total, semi=semi / total, bluff=bluff / total,
        made_hand=made_hand / total, draw=draw_share / total, air=air / total,
        types=shown, two_pair_plus=two_pair_plus / total,
        top_pair=top_pair / total,
    )


def line_note(events: Sequence[ActionEvent]) -> str:
    """事件序列 → 一行英文线路（进 Range.note / JSONL，不进中文提示词）。"""
    return " → ".join(event.label() for event in events[-4:])


def enabled() -> bool:
    """事件模型开关。默认开；`RANGE_EVENT_MODEL=0` 回退旧路径。"""
    raw = os.environ.get("RANGE_EVENT_MODEL", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def reset_cache() -> None:
    _CLASS_CACHE.clear()
    _BUILD_CACHE.clear()
    board_range.reset_cache()


__all__ = [
    "ActionEvent", "BUCKETS", "Composition", "DRAW_KINDS", "GROUPS",
    "HandClass", "MADE_KINDS", "bucket_of", "build_range", "composition",
    "draw_street_factor", "effective_alpha", "enabled", "event_factor",
    "group_multiplier", "hand_class", "likelihood",
    "final_summary", "line_note", "prior_payload", "reset_cache",
    "spr_bucket", "TRACE_DETAIL_MAX",
]
