"""条件弃牌率：把「历史弃牌率」和「本手收窄后的范围」合成一个数。

为什么需要它
────────────
`opponent_profile` 的 `fold_to_bet_flop/turn/river` 是**全范围平均**：它在
对手所有可能的手牌上做了加权，而这手牌里对手已经用动作把范围收窄了。
同一个「翻牌弃 45%」的对手，实测三条线路（Hero=AQ，牌面 K72-4）：

  · 只跟了翻前（182 组合，25% 强成牌）→ 条件弃牌率 ~45%
  · 翻牌 check-call（100 组合，59% 强成牌）→ ~23%
  · 翻牌 check-raise（23 组合，78% 强成牌）→ ~12%

拿 45% 去做三个决策，等于默认他三次都还是同一个人。所以这里改成：

    FE(size | line) = Σ_h  w(h | line) × f(h, size)

`w` 是 `hand_vs_ranges` 已经按动作收窄过的范围权重；`f` 是单手持牌面对某个
价格时的弃牌倾向，由**手牌类别**给出（`BASE_FOLD`），再按**价格**做弹性调整。

这不是精确解
────────────
真正的 `f` 由对手的均衡策略决定，只能用画像逼近。这里的价值在于「用范围给
形状」，而不是「用一个常数」。所以：

  · 只保证**相对排序**：范围越强 → 弃牌率越低；下注越大 → 弃牌率越高；
    多人池 → 全部弃牌的概率连乘。
  · 绝对水平的校准（拿翻前先验另算一遍，把整条曲线缩放到历史值）留给 v2。
  · `render()` 会把「本手估计」和「历史平均」并排放，让模型看到差距，
    而不是把两个数混成一个。

和 `advice.math_gate` 的分工
────────────────────────────
`math_gate` 回答「这个价格跟不跟得起」（权益 vs 底池赔率），回答不了
「我下注他会不会弃」。两个数一起才构成半诈唬判断：

    EV(下注) = FE × 现在能赢的底池 + (1−FE) × [权益 × 成池 − 投入]

反解出「盈亏平衡弃牌率」后，模型只需要比较「他会弃多少」和「他得弃多少」：
前者靠画像定性判断（模型擅长），后者由程序算（模型不擅长）。
"""

from __future__ import annotations

import math
import os
import random
import zlib
from functools import lru_cache
from dataclasses import dataclass

from .coach import (
    AIR, BOARD_PLAY, FLUSH, KICKER_STRONG_MIN, MID_BOTTOM, ONE_PAIR, OVERPAIR,
    STRAIGHT, STRAIGHT_FLUSH, STRONG_DRAW, STRONG_MADE, TPTK, TP_WEAK,
    TWO_PAIR, WEAK_DRAW, evaluate,
)

# ─────────────────────────── 先验 ───────────────────────────

# 手牌类别 → 面对「1/2 池下注」(price = 0.25) 时的弃牌概率。
# 排序比绝对值重要：强成牌几乎不弃，空气大部分会弃。
BASE_FOLD: dict[str, float] = {
    STRONG_MADE: 0.03,
    OVERPAIR: 0.10,
    TPTK: 0.15,
    TP_WEAK: 0.32,
    MID_BOTTOM: 0.52,
    STRONG_DRAW: 0.38,
    WEAK_DRAW: 0.58,
    BOARD_PLAY: 0.50,
    AIR: 0.72,
}

# 参考价格：1/2 池下注时他跟注的价格 = 0.5/(1+2×0.5) = 0.25
_PRICE_REF = 0.25
# 价格弹性：price 翻倍时弃牌倾向放大 2^GAMMA 倍
SIZE_GAMMA = 1.0
# 钳制：不把「必弃/必跟」当成确定事件
_FOLD_MIN, _FOLD_MAX = 0.01, 0.97

# 没有真实选项时的默认考察尺寸（底池比例）
DEFAULT_SIZES = (0.33, 0.5, 0.75, 1.0)

TIER_ZH = {
    STRONG_MADE: "强成牌", OVERPAIR: "超对", TPTK: "顶对强踢",
    TP_WEAK: "顶对弱踢", MID_BOTTOM: "中/底对", STRONG_DRAW: "强听",
    WEAK_DRAW: "弱听", BOARD_PLAY: "打牌面", AIR: "空气",
}


def enabled() -> bool:
    raw = (os.environ.get("FOLD_EQUITY_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


# ─────────────────────────── 手牌分类（轻量） ───────────────────────────


def _mask(ranks) -> int:
    m = 0
    for r in ranks:
        m |= 1 << r
    return m


@lru_cache(maxsize=8192)
def _has_straight(mask: int) -> bool:
    for hi in range(12, 3, -1):
        if ((mask >> (hi - 4)) & 0x1F) == 0x1F:
            return True
    return (mask & 0x100F) == 0x100F          # 轮子 A2345


@lru_cache(maxsize=8192)
def _straight_outs(mask: int) -> int:
    """下一张能成顺的**张数**（点数 × 4 花色）。按 rank 掩码缓存。"""
    hits = 0
    for r in range(13):
        bit = 1 << r
        if mask & bit:
            continue
        if _has_straight(mask | bit):
            hits += 1
    return hits * 4


@lru_cache(maxsize=256)
def _board_eval(board_key: tuple) -> tuple:
    """公共牌自己的牌型（按 board 缓存，避免每个组合都重算一遍）。"""
    return evaluate(list(board_key))


def _plays_board(hole, board, ev) -> bool:
    """和 `coach._plays_board` 同口径，但复用已算好的 `ev` 和缓存的公共牌牌型。"""
    cat, tb = ev
    bcat, btb = _board_eval(tuple(board))
    if cat != bcat:
        return False
    if cat in (STRAIGHT, FLUSH, STRAIGHT_FLUSH):
        return tuple(tb) <= tuple(btb)
    counts: dict[int, int] = {}
    for r, _ in list(hole) + list(board):
        counts[r] = counts.get(r, 0) + 1
    paired = {r for r, c in counts.items() if c >= 2}
    return not any(r in paired for r, _ in hole)


def tier_of(hole, board) -> str:
    """把「两张手牌 + 公共牌」读成 `coach` 的分档。

    不用 `coach.read_hand`：它内部调 `beat_rate`（枚举 C(47,2)），一个 200
    组合的范围就是 200 次全枚举，活链路上跑不起。这里只做成牌分类 + 花顺
    听判定，够用来估弃牌倾向。口径和 `read_hand` 对齐：**两高张不算听牌**。
    """
    cards = list(hole) + list(board)
    ev = evaluate(cards)
    cat = ev[0]
    hole_ranks = [r for r, _ in hole]
    board_ranks = sorted((r for r, _ in board), reverse=True)
    board_top = board_ranks[0] if board_ranks else -1
    pocket = hole_ranks[0] == hole_ranks[1]

    # 打牌面：最好的五张全靠公共牌。双公对面（6622）上人人都至少是两对，
    # 不先判这个会把整桌人都算成「强成牌」，弃牌率算出 8e-07 这种数。
    if len(board) >= 3 and cat >= ONE_PAIR and _plays_board(hole, board, ev):
        return BOARD_PLAY

    if cat >= TWO_PAIR:                       # 两对 / 三条 / 顺子 / 花 / 葫芦 …
        return STRONG_MADE

    if cat == ONE_PAIR:
        if pocket:
            return OVERPAIR if hole_ranks[0] > board_top else MID_BOTTOM
        matched = [r for r in hole_ranks if r in board_ranks]
        if matched:
            pair_rank = matched[0]
            kicker = (hole_ranks[0] if hole_ranks[1] == pair_rank
                      else hole_ranks[1])
            if pair_rank == board_top:
                return TPTK if kicker >= KICKER_STRONG_MIN else TP_WEAK
            return MID_BOTTOM
        return MID_BOTTOM                      # 公对（借牌面）

    # 高牌：只有真花顺听才算听牌
    if len(board) >= 5:                       # 河牌没有下一张
        return AIR
    suits: dict[str, int] = {}
    for _, s in cards:
        suits[s] = suits.get(s, 0) + 1
    ranks = _mask({r for r, _ in cards})
    straight = _straight_outs(ranks)
    flush_draw = max(suits.values()) == 4
    if flush_draw:
        # 同花 9 张 + 顺子张数，扣掉「既成花又成顺」的约 2 张
        outs = 9 + max(straight - 2, 0)
    else:
        outs = straight
    if outs >= 8:
        return STRONG_DRAW
    if outs >= 4:
        return WEAK_DRAW
    return AIR


# ─────────────────────────── 单点计算 ───────────────────────────


def price_of(size: float, pot_before: float) -> float:
    """他跟注需要赢的比例：size / (pot + 2×size)。"""
    if size <= 0 or pot_before <= 0:
        return 0.0
    return size / (pot_before + 2.0 * size)


def fold_probability(tier: str, price: float, scale: float = 1.0) -> float:
    """单手持牌面对某价格的弃牌概率（幂律缩放，1/2 池为参考点）。

    `scale` 是绝对水平校准系数：先验范围上的平均弃牌率应该等于这个对手的
    历史观测值，`scale` 就是两者的比值（见 `_calibration_scale`）。
    """
    base = BASE_FOLD.get(tier, BASE_FOLD[AIR])
    ratio = (max(price, 0.01) / _PRICE_REF) ** SIZE_GAMMA
    return min(_FOLD_MAX, max(_FOLD_MIN, base * ratio * scale))


def range_tier_map(rng, board, dead=()):
    """范围 → ({组合: 分档}, 总权重)。死牌已剔除，分类只做一次。"""
    dead_set = set(dead)
    board_set = set(board)
    tiers: dict = {}
    total = 0.0
    for combo, w in getattr(rng, "combo_weights", {}).items():
        if w <= 0:
            continue
        if combo[0] in dead_set or combo[1] in dead_set:
            continue
        if combo[0] in board_set or combo[1] in board_set:
            continue
        tiers[combo] = tier_of(list(combo), board)
        total += float(w)
    return tiers, total


def range_tier_mix(rng, board, dead=()):
    """范围 → 各分档的权重占比 + 组合数。分类只做一次，多个尺寸复用。"""
    tiers, total = range_tier_map(rng, board, dead)
    if total <= 0 or not tiers:
        return {}, 0
    weights: dict[str, float] = {}
    for combo, tier in tiers.items():
        w = float(rng.combo_weights[combo])
        weights[tier] = weights.get(tier, 0.0) + w
    return {t: w / total for t, w in weights.items()}, len(tiers)


def mix_fold_equity(mix: dict, price: float, scale: float = 1.0) -> float:
    """分档占比 → 该价格下的范围加权弃牌率。"""
    return sum(share * fold_probability(tier, price, scale)
               for tier, share in mix.items())


def range_fold_equity(rng, board, price: float, dead=(), scale: float = 1.0):
    """按范围权重加权的弃牌率 → (弃牌率, 组合数, 各档占比)。"""
    mix, combos = range_tier_mix(rng, board, dead)
    if not mix:
        return 0.0, 0, {}
    return mix_fold_equity(mix, price, scale), combos, mix


# ─────────────────────── 被跟注范围的权益（v2） ───────────────────────
#
# EV(下注) 里的权益必须是「他跟注之后」的权益。拿整个范围的权益代入会系统性
# 高估诈唬 EV：会跟注的那部分牌，正是范围里最强的那部分。
#
# 做法：从范围里按权重抽 (组合, 发牌)，把每手牌的结果记下来；算某个尺寸时，
# 按「这手牌会不会跟」重新加权同一个样本集（1 − f(tier, price)）。一次抽样
# 服务所有尺寸，不需要每个尺寸重跑一遍枚举。
#
# 抽样按范围权重进行，重加权时只乘跟注概率，不需要再乘范围权重（否则重复）。

_SUITS = "cdhs"
_DECK = tuple((r, s) for r in range(13) for s in _SUITS)


def sample_budget() -> int:
    raw = (os.environ.get("FOLD_EQUITY_SAMPLES") or "").strip()
    try:
        n = int(raw) if raw else 256
    except ValueError:
        n = 256
    return max(100, min(n, 2000))


def sample_outcomes(hero, board, rng, *, n: int | None = None, dead=(),
                    seed: int = 0, tier_map: dict | None = None):
    """抽 (对手组合, 发牌)，返回 [(分档, 得分)]。得分 1 赢 / 0.5 平 / 0 输。

    `tier_map` 传 `range_tier_map()` 的结果可以省掉一次重复分类。
    """
    hero = list(hero)
    board = list(board)
    dead_set = set(hero) | set(board) | set(dead)
    board_set = set(board)
    pool: list[tuple] = []
    total = 0.0
    for combo, w in getattr(rng, "combo_weights", {}).items():
        if w <= 0:
            continue
        if combo[0] in dead_set or combo[1] in dead_set:
            continue
        tier = tier_map.get(combo) if tier_map else None
        if tier is None:
            tier = tier_of(list(combo), board)
        pool.append((combo, float(w), tier))
        total += float(w)
    if not pool or total <= 0:
        return []

    rnd = random.Random(seed)
    budget = n or sample_budget()
    need = 5 - len(board)
    out: list[tuple[str, float]] = []
    for _ in range(budget):
        pick = rnd.random() * total
        acc = 0.0
        combo, tier = pool[-1][0], pool[-1][2]
        for c, w, t in pool:
            acc += w
            if pick <= acc:
                combo, tier = c, t
                break
        used = dead_set | set(combo)
        rest = [c for c in _DECK if c not in used]
        runout = rnd.sample(rest, need) if need > 0 else []
        hero7 = hero + board + runout
        vill7 = list(combo) + board + runout
        hv, vv = evaluate(hero7), evaluate(vill7)
        out.append((tier, 1.0 if hv > vv else 0.5 if hv == vv else 0.0))
    return out


def equity_when_called(samples, price: float, scale: float = 1.0) -> float:
    """按「会不会跟」重新加权后的权益。price 越大，留下的范围越强。"""
    num = den = 0.0
    for tier, score in samples:
        w = 1.0 - fold_probability(tier, price, scale)
        num += w * score
        den += w
    return num / den if den > 0 else 0.0


# ─────────────────────── 权益实现率 ───────────────────────
#
# `hr.equity` 是「发到河牌的全下权益」：它假设后面不再下注、也不需要弃牌。
# 真实对局里，我 call 了翻牌还要面对转牌的下注，打不动的牌会被打走 —— 拿到
# 手的权益不等于能兑现的权益。经验值：IP 的强成牌接近全额，OOP 的空气只有
# 一半多。不乘这个系数，跟注 EV 会系统性高估，边缘牌会被建议去跟。
#
# 这是先验，不是这个对手的实测曲线；口径和 BASE_FOLD 一样，只保证相对排序：
# 成牌 > 听牌 > 打牌面 > 空气，IP > OOP，人越多越低。

REALIZATION_BASE: dict[str, float] = {
    "value": 0.90,
    "draw": 0.82,
    "board_play": 0.75,
    "weak": 0.58,
}
REALIZATION_IP = 1.00
REALIZATION_OOP = 0.86
REALIZATION_UNKNOWN_POS = 0.93
REALIZATION_PER_EXTRA_OPPONENT = 0.95
_REAL_MIN, _REAL_MAX = 0.45, 1.0


def kind_of_tier(tier: str | None) -> str | None:
    """`coach` 的分档 → 实现率口径的四类。未知分档返回 None。"""
    if tier in (STRONG_MADE, OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM):
        return "value"
    if tier in (STRONG_DRAW, WEAK_DRAW):
        return "draw"
    if tier == BOARD_PLAY:
        return "board_play"
    if tier == AIR:
        return "weak"
    return None


# 还剩几条下注街，决定折扣能打多深：河牌 call 完直接摊牌，权益全额兑现；
# 转牌只剩一条街；翻牌还有两条。不区分街会把河牌的跟注 EV 系统性算低。
_STREET_DISCOUNT = {"flop": 1.0, "turn": 0.5, "river": 0.0}


def realization_factor(*, tier: str | None, in_position: bool | None,
                       opponents: int = 2, street: str | None = None) -> float:
    """我在这手牌上实际能兑现多少账面权益（0.45~1.0）。

    `street` 决定折扣深度：河牌恒为 1.0（没有下一街了），转牌是一半，
    翻牌是全额折扣。实现率未知时返回 1.0（宁可少修正也不凭空扣分）。
    """
    kind = kind_of_tier(tier)
    if kind is None:
        return 1.0
    base = REALIZATION_BASE[kind]
    if in_position is True:
        base *= REALIZATION_IP
    elif in_position is False:
        base *= REALIZATION_OOP
    else:
        base *= REALIZATION_UNKNOWN_POS
    extra = max(int(opponents) - 2, 0)
    base *= REALIZATION_PER_EXTRA_OPPONENT ** extra
    discount = _STREET_DISCOUNT.get(str(street or ""), 1.0)
    if discount < 1.0:                     # 只削减「折扣量」，不是削减权益
        base = 1.0 - (1.0 - base) * discount
    return min(_REAL_MAX, max(_REAL_MIN, base))


def breakeven_fold(pot_now: float, owed: float, size: float,
                   equity: float) -> float:
    """让下注回本所需的弃牌率。

    `pot_now` 现在能赢的底池（含他的下注）；`owed` 跟注额；`size` 我在
    跟注之外多投的部分。他弃我赢 `pot_now`，他跟则打
    `pot_now + owed + 2×size`，我投入 `owed + size`。
    """
    if pot_now <= 0 or size <= 0:
        return 1.0
    invested = owed + size
    ev_called = equity * (pot_now + owed + 2.0 * size) - invested
    if ev_called >= 0:                        # 被跟也赚：不需要弃牌率
        return 0.0
    return min(1.0, -ev_called / (pot_now - ev_called))


def ev_raise(pot_now: float, owed: float, size: float, equity: float,
             fold_equity: float) -> float:
    invested = owed + size
    ev_called = equity * (pot_now + owed + 2.0 * size) - invested
    return fold_equity * pot_now + (1.0 - fold_equity) * ev_called


def ev_call(pot_now: float, owed: float, equity: float,
            realization: float = 1.0) -> float:
    """跟注 EV：赢 `pot_now + owed`，投入 `owed`。

    `realization` 是这手牌实际能兑现的权益比例（见 `realization_factor`）。
    不乘它等于假设后面不再下注、也不会被打走，边缘牌会被高估成该跟。
    """
    return realization * equity * (pot_now + owed) - owed


# ─────────────────────────── 报告 ───────────────────────────


@dataclass(frozen=True)
class SizeLine:
    label: str
    size: float                  # 跟注之外多投的部分
    fold_equity: float
    breakeven: float
    ev: float
    equity_called: float = 0.0   # 他被跟注时我的权益（范围里最强的那些）

    @property
    def edge(self) -> float:
        return self.fold_equity - self.breakeven

    @property
    def verdict(self) -> str:
        if self.breakeven <= 0.001:
            return "value: +EV even if called"
        if self.ev <= 0:
            return "bluff is -EV"
        if self.edge >= 0.10:
            return "clearly +EV"
        return "marginal"


@dataclass(frozen=True)
class FoldEquityReport:
    street: str
    combos: int
    fold_equity: float                 # 全部对手都弃牌的概率
    observed: float | None             # 历史平均弃牌率
    strong_share: float
    shares: tuple[tuple[str, float], ...]
    lines: tuple[SizeLine, ...]
    opponents: int
    call_line: str | None = None
    range_equity: float = 0.0     # 对整个范围的权益（不扣跟注范围）
    realization: float = 1.0      # 跟注能兑现的权益比例
    scale: float = 1.0            # 弃牌率的绝对水平校准系数

    @property
    def narrowed(self) -> bool:
        return self.observed is not None and self.fold_equity < self.observed - 0.05

    def render(self) -> str:
        pct = lambda v: f"{v * 100:.0f}%"          # noqa: E731
        each = f"~{self.combos} combos each" if self.opponents > 1 else f"{self.combos} combos"
        head = (f"His line left {each} "
                f"({self.opponents} opponent{'s' if self.opponents != 1 else ''}); "
                f"strong made {pct(self.strong_share)}; "
                f"fold equity now {pct(self.fold_equity)}")
        if self.observed is not None:
            head += f" vs {pct(self.observed)} lifetime"
            if abs(self.scale - 1.0) > 0.05:
                head += f" (calibrated ×{self.scale:.2f})"
        out = ["## Fold equity (range-conditional)", head]
        if self.narrowed:
            out.append(
                "His line already removed the weak hands — his career fold rate "
                "overstates him here. Judge the bluff on THIS number.")
        elif self.observed is not None and self.fold_equity > self.observed + 0.10:
            out.append("His line leaves a wide/capped range — he folds more than "
                       "his career average here.")
        top = sorted(self.shares, key=lambda kv: -kv[1])[:3]
        if top:
            out.append("Range mix: " + ", ".join(
                f"{TIER_ZH.get(t, t)} {pct(s)}" for t, s in top))
        if self.lines:
            out.append("Bet sizes (fold equity vs break-even, EV in BB):")
            for ln in self.lines:
                need = ("needs no folds" if ln.breakeven <= 0.001
                        else f"needs {pct(ln.breakeven)} folds")
                line = (f"  {ln.label}: he folds {pct(ln.fold_equity)}, "
                        f"{need} → EV {ln.ev:+.2f}BB ({ln.verdict})")
                if self.range_equity and ln.equity_called + 0.02 < self.range_equity:
                    # 只有「被跟之后明显变差」才提醒：这就是 EV 别拿整范围
                    # 权益去算的原因，模型看得见才不会把诈唬估成价值。
                    line += f" [if called you have {pct(ln.equity_called)}]"
                out.append(line)
        if self.opponents > 1:
            out.append(
                "Multiway: the fold-equity number above is the chance ALL of them "
                "fold (product over the modelled opponents). Per-size EV is not "
                "shown — the semi-bluff EV math is heads-up only.")
        if self.call_line:
            out.append(self.call_line)
        return "\n".join(out)


def _size_lines(options, pot_now: float, owed: float) -> list[tuple[str, float]]:
    """优先用牌桌上真实可选的加注额，退回底池比例。size = 跟注之外多投的部分。"""
    out: list[tuple[str, float]] = []
    seen: set[int] = set()
    for opt in options or ():
        kind = getattr(opt, "kind", "")
        amount = getattr(opt, "amount", None)
        if kind not in ("bet", "raise", "allin") or amount is None:
            continue
        size = float(amount) - owed
        if size <= 0:
            continue
        key = int(round(size))
        if key in seen:
            continue
        seen.add(key)
        # 用牌桌自己的标签，模型才能和选项列表对上号
        label = getattr(opt, "label", None) or (
            "all-in" if kind == "allin"
            else f"{size / pot_now:.0%} pot" if pot_now > 0 else f"{size:g}")
        out.append((label, size))
    if out:
        return out[:4]
    # 有选项、但里面没有能下注的（面对全下、我已经套池）：这手打不了注，
    # 不能退回底池比例 —— 那会给一个根本不能做的动作算 EV。
    if options:
        return []
    if pot_now <= 0:
        return []
    return [(f"{f:.0%} pot", round(f * pot_now, 2)) for f in DEFAULT_SIZES]


def lookup(sit) -> FoldEquityReport | None:
    """从 `advice.Situation` 生成报告。缺数据/出错一律 None（fail open）。"""
    if not enabled():
        return None
    try:
        return _build(sit)
    except Exception:                          # noqa: BLE001
        return None


def _build(sit) -> FoldEquityReport | None:
    from .advice import call_amount, winnable_pot

    snap = sit.snap
    if str(getattr(snap, "street", "")) == "preflop":
        return None
    raw_board = list(snap.board)
    if len(raw_board) < 3:
        return None
    from .coach import parse_cards
    board = parse_cards("".join(raw_board[:5]))
    if len(board) != len(raw_board[:5]):
        return None

    hr = getattr(sit, "hand_ranges", None)
    edges = tuple(getattr(hr, "opponents", ()) or ())
    edge_ranges = [getattr(e, "rng", None) for e in edges]
    if not edges or any(r is None for r in edge_ranges):
        return None

    owed = float(call_amount(snap) or 0.0)
    winnable = float(winnable_pot(snap) or 0.0)
    if winnable <= 0:
        return None
    pot_now = winnable - owed                 # 他弃了我能赢的部分
    if pot_now <= 0:
        pot_now, owed = winnable, 0.0

    dead = parse_cards("".join(snap.hero_cards))
    if len(dead) != 2:
        return None
    equity = float(getattr(hr, "equity", 0.0) or 0.0)

    mixes: list[dict] = []
    tier_maps: list[dict] = []
    combos = 0
    shares_total: dict[str, float] = {}
    for rng in edge_ranges:
        tier_map, total_w = range_tier_map(rng, board, dead)
        if not tier_map or total_w <= 0:
            continue
        mix: dict[str, float] = {}
        for combo, tier in tier_map.items():
            w = float(rng.combo_weights[combo]) / total_w
            mix[tier] = mix.get(tier, 0.0) + w
        mixes.append(mix)
        tier_maps.append(tier_map)
        combos = max(combos, len(tier_map))      # 每个对手各剩多少，取最大的那个
        for tier, share in mix.items():
            shares_total[tier] = shares_total.get(tier, 0.0) + share
    if not mixes:
        return None
    # 多个对手时占比要按人数平均，否则加起来能到 189%（渲染出来就是错的）
    n_opp_mix = float(len(mixes))
    shares_total = {t: v / n_opp_mix for t, v in shares_total.items()}
    observed = _observed_fold(sit)
    scale = _calibration_scale(edge_ranges, board, dead, observed)
    per_opponent = [mix_fold_equity(m, _PRICE_REF, scale) for m in mixes]

    # 被跟注范围只在单挑（一个对手）时有意义：抽样是「我和这一家」的
    # 单挑权益，多人池里拿它去乘多人底池会得出 +44BB 这种不可能的 EV。
    heads_up = len(mixes) == 1
    samples: list[tuple[str, float]] = []
    if heads_up:
        seed = zlib.crc32(
            f"{getattr(hr, 'hero', '')}|{getattr(hr, 'board', '')}|"
            f"{','.join(getattr(e, 'range_id', '') for e in edges)}".encode())
        samples = sample_outcomes(dead, board, edge_ranges[0],
                                  n=sample_budget(), seed=seed,
                                  tier_map=tier_maps[0])

    lines: list[SizeLine] = []
    # 多人池：只给「全部弃牌」的概率，不给尺寸 EV —— 半诈唬的 EV 公式是单挑的，
    # 硬套会算出超过底池的收益，模型会照着打。
    size_lines = [] if not heads_up else _size_lines(
        getattr(sit, "options", ()), pot_now, owed)
    for label, size in size_lines:
        price = price_of(size, pot_now + owed)
        fe = math.prod(mix_fold_equity(m, price, scale) for m in mixes)
        # 他跟注的那部分牌比整个范围强，EV 必须用这个权益算。
        eq_called = (equity_when_called(samples, price, scale)
                     if samples else equity)
        lines.append(SizeLine(
            label=label, size=size, fold_equity=fe,
            breakeven=breakeven_fold(pot_now, owed, size, eq_called),
            ev=ev_raise(pot_now, owed, size, eq_called, fe),
            equity_called=eq_called))

    call_line = None
    real = realization_factor(
        tier=getattr(sit, "hand_tier", None),
        in_position=getattr(sit, "in_position", None),
        opponents=len(per_opponent) + 1,
        street=str(getattr(snap, "street", "")))
    if owed > 0:
        raw_bep = owed / (pot_now + owed) * 100
        call_ev = ev_call(pot_now, owed, equity, real)
        tail = f"you have {equity * 100:.0f}%"
        if real < 0.995:
            # 实现率是「拿得到手的权益」：口径和门槛分开写，模型才不会
            # 拿 7% 去和 24% 直接比完就以为只是「差一点」。
            tail += (f", realizing {real:.2f} of it → ~{equity * real * 100:.0f}%"
                     f" effective vs {raw_bep:.0f}% needed")
        call_line = f"Call {owed:g}BB: EV {call_ev:+.2f}BB ({tail})"

    return FoldEquityReport(
        street=str(getattr(snap, "street", "")),
        combos=combos,
        fold_equity=math.prod(per_opponent),
        observed=observed,
        scale=scale,
        strong_share=shares_total.get(STRONG_MADE, 0.0),
        shares=tuple(shares_total.items()),
        lines=tuple(lines),
        opponents=len(per_opponent),
        call_line=call_line,
        range_equity=equity,
        realization=real,
    )


def _prior_mix(rng, board, dead) -> dict:
    """取这个范围背后的翻前先验的分档占比（用来做绝对水平校准）。

    `prior_id` 为空有两种情况：check 模式下返回的就是翻前先验本身，或者
    根本没有先验（board-only）。两种都用范围自己当锚——我们只有这个。
    """
    pid = getattr(rng, "prior_id", None)
    if not pid:
        try:
            mix, _ = range_tier_mix(rng, board, dead)
            return mix
        except Exception:                      # noqa: BLE001
            return {}
    try:
        from . import ranges as ranges_mod
        prior = ranges_mod.get(pid)
        if prior is None:
            return {}
        mix, _ = range_tier_mix(prior, board, dead)
        return mix
    except Exception:                          # noqa: BLE001
        return {}


def _calibration_scale(edge_ranges, board, dead, observed: float | None) -> float:
    """把先验范围上的平均弃牌率缩放到历史观测值。

    `BASE_FOLD` 是先验表，绝对水平没有依据。可用的锚点只有一个：这个对手
    历史上「面对下注弃 X%」—— 那是**全范围**平均，正好对应翻前先验上的
    期望。于是 scale = 观测 / 先验上的模型均值，只缩放水平、不改形状。
    """
    if observed is None:
        return 1.0
    raws = []
    for rng in edge_ranges:
        mix = _prior_mix(rng, board, dead)
        if mix:
            raws.append(mix_fold_equity(mix, _PRICE_REF))
    if not raws:
        return 1.0
    raw = sum(raws) / len(raws)
    if raw <= 0.02:
        return 1.0
    return min(2.5, max(0.4, observed / raw))


def _observed_fold(sit) -> float | None:
    """历史弃牌率（0~1）：还在局里的对手取平均。

    不能用 `behind_opponents`（那是「我之后还要说话的人」）：我下注之后，
    之前已经过牌的人照样要再决定一次，他们同样是弃牌率的来源。多人池里
    取平均而不是取最大，是为了不做「挑一个最会弃的对手来证明该诈」。
    """
    attr = {"flop": "fold_to_bet_flop_pct", "turn": "fold_to_bet_turn_pct",
            "river": "fold_to_bet_river_pct"}.get(str(getattr(sit.snap, "street", "")))
    if attr is None:
        return None
    live = {getattr(o, "seat_id", None) for o in getattr(sit.snap, "opponents", ())
            if not getattr(o, "folded", False)}
    values: list[float] = []
    for read in getattr(sit, "opponent_reads", ()) or ():
        if getattr(read, "seat_id", None) not in live:
            continue
        value = getattr(read, attr, None)
        if value is None:
            continue
        value = float(value)
        values.append(value / 100.0 if value > 1.0 else value)
    if not values:
        return None
    return sum(values) / len(values)


__all__ = [
    "FoldEquityReport", "SizeLine", "tier_of", "fold_probability", "price_of",
    "range_fold_equity", "range_tier_mix", "range_tier_map", "mix_fold_equity",
    "sample_outcomes", "equity_when_called", "breakeven_fold",
    "realization_factor", "kind_of_tier", "REALIZATION_BASE", "ev_raise", "ev_call", "lookup",
    "enabled", "BASE_FOLD", "DEFAULT_SIZES", "TIER_ZH",
]
