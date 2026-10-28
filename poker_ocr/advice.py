"""实时 AI 决策建议：按当前行动街加载技能，把完整行动线交给模型，只回一个选项。

与 call-back 复盘的关系：**同一套方法论、同一种「目录 + 展开正文」的注入方式**
（见 skills.py），但输入是 `HandSnapshot`（实时链路逐帧累积出来的），不是
ReviewHand；输出也不是长篇点评，而是从固定选项里**选一个**。

为什么输出是「选项编号」而不是自由文本：

  · 实时场景下人要的是一秒内能执行的动作，不是分析文章。
  · 选项把尺度约束死了（1/3、1/2、2/3、1、1.2 倍池），模型不可能编出不存在的尺度。
  · 选项由本模块按现场底池算好金额，模型只做选择 —— 它不需要会算底池，
    也就不会算错。这一点和 coach.py「确定性规则不交给模型」是同一条原则。

调用协议：OpenAI 兼容的 /chat/completions（与 call-back 的 AIClient 同口径），
凭据来自环境变量 AI_SERVER_API_KEY / AI_SERVER_BASE_URL / AI_SERVER_MODEL。
**非流式**：一次建议的正文只有几十个 token，流式只会增加复杂度；超时用
`socket` 级的总时长控制（默认 20 秒，实时链路等不起更久）。**连接复用**：
同一 host 的 HTTP/1.1 长连接会被缓存，省掉每次建议都要付的 TLS 握手
（国内到 OpenRouter 实测 0.4–1.4s，常常比模型本身还慢）。
"""

from __future__ import annotations

import http.client
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Iterable

from . import skills
from .coach import (
    DRY, MID_BOTTOM, OVERPAIR, STRONG_DRAW, STRONG_MADE, TPTK, TP_WEAK, TWO_PAIR,
    WEAK_DRAW, board_texture, opponent_strength, parse_cards, read_hand,
)
from .ground_truth import GroundTruth, compute_ground_truth
from .hand_track import ActionRecord, FLOP, HandSnapshot, PREFLOP, RIVER, TURN
from .opponent_profile import (AGGRESSIVE_ARCHETYPES, PASSIVE_ARCHETYPES,
                               OpponentRead)
from .positions import BB, POSITIONS_BY_TABLE_SIZE, SB
from .preflop_ranges import chart_text, range_verdict
from .table_dynamics import (is_aggressive_table, is_loose_table,
                             value_heavy_aggression)

# ─────────────────────────── 选项 ───────────────────────────

# 行动线里 hero 的标识。与 coach.advise 的 hero_position 口径一致
HERO_ACTOR = "hero"

# 加注尺度：名字 → 底池倍数。「满池 1.2」是超池，用于需要施压/劝退的场合
RAISE_FRACTIONS: dict[str, float] = {
    "1/3": 1 / 3,
    "1/2": 0.5,
    "2/3": 2 / 3,
    "1": 1.0,
    "1.2": 1.2,
}

# 用户指定的选项顺序。AI 只能返回这里的 id
OPTION_ORDER = ("fold", "call", "raise-1/3", "raise-1/2", "raise-2/3",
                "raise-1", "raise-1.2")

OPTION_LABELS = {
    "fold": "Fold",
    "call": "Call",
    "raise-1/3": "Raise to 1/3 pot",
    "raise-1/2": "Raise to 1/2 pot",
    "raise-2/3": "Raise to 2/3 pot",
    "raise-1": "Raise to full pot",
    "raise-1.2": "Raise to 1.2× pot",
}

# 没有人下注时这些档位是**下注**，不是加注。id 沿用 raise-*（对外契约不变），
# 但标签必须说 Bet —— 给模型看「Raise to 1/2 pot」会让它以为需要先有一个下注，
# 反而更容易选 Check。kind 也随之改成 bet，复盘对比时口径才对得上。
BET_LABELS = {
    "raise-1/3": "Bet 1/3 pot",
    "raise-1/2": "Bet 1/2 pot",
    "raise-2/3": "Bet 2/3 pot",
    "raise-1": "Bet full pot",
    "raise-1.2": "Bet 1.2× pot",
}


@dataclass(frozen=True)
class Option:
    """一个可执行选项。amount 是**加注后的本街总额**（BB），跟注为需跟额。"""

    id: str
    label: str
    kind: str                     # check | call | fold | raise
    amount: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        d = {"id": self.id, "label": self.label, "kind": self.kind}
        if self.amount is not None:
            d["amount_bb"] = round(self.amount, 2)
        if self.note:
            d["note"] = self.note
        return d


def _round_bb(v: float) -> float:
    return round(v, 2)


# 翻前开池的基础倍数（bb），按位置分档，来自 preflop-open.md：
# 前位 2.5-3.0 / 中位 3.0-3.5 / 后位 3.5-4.0 / 小盲 3.0。
# 大盲在溜入池里再加注时，尺度要比开池更大（没有位置劣势补偿，直接收池）。
# 这些只是**下限修正前**的原始尺度：低于本桌 6BB 最低加注（PREFLOP_MIN_RAISE_BB）
# 的档位会由 `_cap()` 统一抬到 6BB。
_OPEN_BASES: dict[str, tuple[float, ...]] = {
    "UTG": (2.5, 2.75, 3.0),
    "UTG+1": (2.5, 2.75, 3.0),
    "UTG+2": (2.5, 2.75, 3.0),
    "LJ": (3.0, 3.25, 3.5),
    "HJ": (3.0, 3.25, 3.5),
    "CO": (3.5, 3.75, 4.0),
    "BTN": (3.5, 3.75, 4.0),
    "SB": (3.0, 3.25, 3.5),
    "BB": (3.5, 4.0, 4.5),
}

# 松桌的开池加成（BB）：跟注站/松弱桌开大一点收价值；紧桌不加。
# 叠加在「位置倍数 × 前注加成 + 溜入者加成」之后，再由 _cap 做全下/最小加注修正。
LOOSE_OPEN_BONUS = 0.5
# 溜入者的加成递减：前两个每个 +1BB，第 3 个起每个 +0.5BB。
# 3-4 个 limper 时如果还按 +1BB 线性叠加，BTN 会开到 12.5BB（100BB 的 12%+），
# 只会把能跟注的范围压成两极化；递减后 3-4 个 limper 的 BTN 约 10-11.5BB。
LIMP_BONUS_FIRST = 1.0
LIMP_BONUS_EXTRA = 0.5

# 翻前桌上规则：任何加注/开池/再加注的「加注到」总额不得低于 6BB。
# 牌桌客户端会拒绝更小的尺度，给出 2.5BB 的开池建议等于给一个点不出来的动作。
# 位置尺度（2.5-4BB）低于这个下限时由 `_cap()` 抬到 6BB；真正的全下不受此限
# （后手不足 6BB 时只能全下，这是合法动作）。改牌桌规则时只改这个常量。
PREFLOP_MIN_RAISE_BB = 6.0

# 成手牌「**单个对手**能赢我」的比例超过这个值，就不再按价值牌处理。
#
# 为什么用单人比例、不用「全桌至少一家领先」：后者会被人数放大——同一手顶对，
# 单挑时 15%、9 人池里就成了 76%，全被误判成「不能收价值」。人数应该影响**下注
# 值不值**，不该影响**我的牌什么水平**。
#
# 22% 的取法来自实测（2026-10-23 那局）：
#   8822@88T 25.9% / 6644@QJ266 40.9% → 拦下（这两手合计 −54.5）
#   花面的顺子 4.3% / 公对面的同花 1.8% / 干燥面真两对 0.5% / 坚果顺 0.9%
#   → 不动，仍然收价值
# 设到 30% 会漏掉 h75（25.9%）；设到 15% 会误伤多路池里的顶对。
DOMINATED_BEAT_PCT = 22.0


def _cap(target: float, hero_max: float | None, min_raise_to: float,
         min_note: str = "raised to the minimum raise") -> tuple[float, str]:
    """把目标额修正到合法范围：低于最小加注抬上去，高于后手封顶为全下。

    `min_note` 让翻前的牌桌下限（6BB）能和扑克本身的最小加注区分开：
    模型看到「raised to the table minimum 6BB」才知道不是程序算错了。
    """
    note = ""
    if target < min_raise_to:
        target = min_raise_to
        note = min_note
    if hero_max is not None and target >= hero_max - 1e-9:
        target = hero_max
        note = "stack too short, effectively all-in"
    return _round_bb(target), note


def count_limpers(snap: HandSnapshot) -> int:
    """我行动前已经平跟进池的人数（不含盲注位）。

    优先看行动线；但 limper 平跟 1BB 往往不生成动作徽章，行动线会漏。
    再按「同帧各家下注额 == 1BB 且不是盲注位」补一次，取两者较大值。
    """
    bb = snap.blinds.bb if snap.blinds else 1.0
    from_actions = sum(
        1 for a in snap.all_actions()
        if a.street == PREFLOP and a.action == "call" and not a.is_hero)
    from_bets = sum(
        1 for o in snap.opponents
        if o.position not in ("SB", "BB") and not o.folded
        and o.bet is not None and abs(float(o.bet) - bb) < 0.01)
    return max(from_actions, from_bets)


def build_preflop_options(snap: HandSnapshot,
                          open_bonus: float = 0.0) -> list[Option]:
    """翻前选项：开池按「位置倍数 ×（有前注 ×2）+ 溜入者 + 松桌加成」，
    再加注按 3-4 倍。

    翻前的底池只有盲注那么大，用「底池的 1/3」表达尺度没有意义 ——
    preflop-open.md 也明确要求「一律用大盲倍数说，不要说加注到几倍底池」。
    所以翻前换成 bb 口径的选项，翻后才用底池倍数。
    """
    to_call = max(snap.to_call or 0.0, 0.0)
    hero_bet = snap.hero_bet or 0.0
    stack = snap.hero_stack
    bb = snap.blinds.bb if snap.blinds else 1.0
    max_bet = hero_bet + to_call
    second = snap.second_bet
    increment = max(max_bet - second if second is not None else max_bet, bb)
    legal_min_raise_to = max_bet + increment
    # 本桌的 6BB 下限与扑克的最小加注取较严的那个：上家已经加注到 10BB 时，
    # 合法的再加注到 20BB 比 6BB 更严，不能被 6BB 覆盖掉。
    table_min_raise_to = bb * PREFLOP_MIN_RAISE_BB
    min_raise_to = max(legal_min_raise_to, table_min_raise_to)
    min_note = ("raised to the minimum raise"
                if legal_min_raise_to >= table_min_raise_to
                else f"raised to the table minimum {table_min_raise_to:g}BB")
    hero_max = (hero_bet + stack) if stack is not None else None

    # 下注/加注已经超过后手：跟注就是全下，牌桌上不存在「再加注」。
    # 此时只能弃牌或 all-in —— 与客户端只显示 Fold / All in 一致。
    call_all_in = (stack is not None and stack > 0 and to_call >= stack)
    if call_all_in:
        options = [
            Option("call", "Call all-in", "call", _round_bb(stack),
                   "the bet covers my stack; calling puts me all-in"),
            Option("fold", "Fold", "fold"),
        ]
        by_id = {o.id: o for o in options}
        return [by_id[i] for i in OPTION_ORDER if i in by_id]

    # 判断「前面有没有人加注」用形态，不用 to_call：小盲面对大盲时 to_call=0.5，
    # 那是补齐盲注，不是面对加注
    facing_raise = any(s in ("single-raise", "three-bet")
                       for s in snap.preflop_shapes)

    options: list[Option] = []
    if facing_raise and to_call > 0:
        options.append(Option("call", "Call", "call", _round_bb(to_call)))
        options.append(Option("fold", "Fold", "fold"))
        # 面对加注：3bet/再加注到「上家加注的 3 倍 / 4 倍」，来自
        # preflop-3bet-pot.md「有位置 3-4 倍，盲注位一律 4 倍」
        for mult in (3, 4):
            amount, note = _cap(max_bet * mult, hero_max, min_raise_to, min_note)
            options.append(Option(
                f"raise-{mult}x", f"Raise to {mult}× the last raise", "raise",
                amount, note))
        return options

    # 没人加注：平跟（limp）/ 补齐盲注 / 大盲过牌，然后按位置倍数开池。
    # 平跟标签必须区分「小盲补齐」和「非盲位的 over-limp」：写成 complete
    # 会让模型以为这是小盲的补盲动作，看不到溜入池里抽 set 的线路
    hero_pos = snap.hero_position or ""
    if to_call > 0:
        label = ("Complete the blind" if hero_pos == "SB"
                 else f"Call {to_call:g}BB (over-limp)")
        options.append(Option("call", label, "call", _round_bb(to_call)))
    elif hero_pos == "SB":
        # SB 已经补齐盲注（to_call=0）时，这是 check/raise 点，不是 limp。
        # h60 的第二条 preflop 建议把这里标成 "limp in"，模型只能选
        # 「最便宜的退出」；标签错了，动作空间也就错了。
        if (snap.hero_bet or 0.0) >= bb:
            options.append(Option("call", "Check", "check", None,
                                  "SB has matched the blind; check or raise"))
        else:
            need = max(bb - (snap.hero_bet or 0.0), 0.0)
            options.append(Option("call", "Complete the blind", "call",
                                  _round_bb(need) if need else None,
                                  "SB still needs to match the big blind"))
    elif hero_pos == "BB":
        options.append(Option("call", "Check", "check", None,
                              "Big blind sees the flop free"))
    else:
        options.append(Option("call", "limp in", "call", None,
                              "the first to enter should usually raise"))
    if to_call > 0:
        options.append(Option("fold", "Fold", "fold"))

    limpers = count_limpers(snap)
    limper_bonus = (min(limpers, 2) * LIMP_BONUS_FIRST
                    + max(limpers - 2, 0) * LIMP_BONUS_EXTRA)
    base = _OPEN_BASES.get(hero_pos, (3.0, 3.25, 3.5))
    # 前注按「大盲的倍数」加，不能拿它去乘开池尺度：0.5BB 的前注会把 UTG 的
    # 2.5BB 开池直接翻成 5BB（h39 的 5.5BB 就是这么来的），既偏离标准
    # 2.5-3.5BB，也把能跟注的范围压成两极化。前注只是死钱，开池加 0.5-1BB
    # 足够覆盖。
    ante_bb = 0.0
    if snap.blinds and snap.blinds.ante and snap.blinds.bb:
        ante_bb = float(snap.blinds.ante) / float(snap.blinds.bb)
    for b in base:
        target = b + ante_bb + limper_bonus + max(open_bonus, 0.0)
        target, note = _cap(target, hero_max, min_raise_to, min_note)
        options.append(Option(f"open-{b:g}", f"Raise to {target:g}BB (open)",
                              "raise", target, note))
    return options


def _raise_geometry(snap: HandSnapshot) -> tuple[float, float, float, float | None, float]:
    """加注金额的公共几何：需跟额、底池、当前最高下注、后手封顶、最小加注到。

    `build_options()` 和 `preset_targets()` 共用，避免两处对「最小加注 / 全下
    封顶」的算法漂移。翻前还要叠加本桌的 6BB 下限（`PREFLOP_MIN_RAISE_BB`）：
    点击器按这个几何选档位，档位目标低于 6BB 就等于点出一个牌桌拒绝的动作。
    """
    to_call = max(snap.to_call or 0.0, 0.0)
    pot = snap.pot or 0.0
    hero_bet = snap.hero_bet or 0.0
    stack = snap.hero_stack
    bb = snap.blinds.bb if snap.blinds else 1.0
    max_bet = hero_bet + to_call
    second = snap.second_bet
    increment = max(max_bet - second if second is not None else max_bet, bb)
    min_raise_to = max_bet + increment
    if snap.street == PREFLOP:
        min_raise_to = max(min_raise_to, bb * PREFLOP_MIN_RAISE_BB)
    hero_max = (hero_bet + stack) if stack is not None else None
    return to_call, pot, max_bet, hero_max, min_raise_to


def preset_targets(snap: HandSnapshot) -> dict[str, tuple[float, str]]:
    """五个快捷档位（底池 1/3 … 1.2）在当前局面的目标额。

    与 `build_options()` 的翻后档位使用同一套公式和 `_cap()`：面对下注时
    `最高下注 + f × (底池 + 需跟额)`，没人下注时 `f × 底池`。翻前也一样算，
    虽然翻前建议用的是 bb 口径，但客户端按钮仍按底池档位显示，点击器要拿它
    做「最近档位」映射。返回 `{option_id: (amount_bb, note)}`。
    """
    to_call, pot, max_bet, hero_max, min_raise_to = _raise_geometry(snap)
    out: dict[str, tuple[float, str]] = {}
    for name in ("1/3", "1/2", "2/3", "1", "1.2"):
        if to_call > 0:
            target = max_bet + RAISE_FRACTIONS[name] * (pot + to_call)
        else:
            target = RAISE_FRACTIONS[name] * pot
        out[f"raise-{name}"] = _cap(target, hero_max, min_raise_to)
    return out


def preset_amounts(snap: HandSnapshot) -> dict[str, float]:
    """`preset_targets()` 的金额部分，给点击器做最近档位选择。"""
    return {oid: amount for oid, (amount, _note) in preset_targets(snap).items()}


def build_options(snap: HandSnapshot,
                  open_bonus: float = 0.0,
                  block_draw_bets: bool = False) -> list[Option]:
    """按现场底池与下注算出全部可执行选项。

    `open_bonus` 只加在翻前开池上（松桌 +1BB），翻后按底池倍数的选项不动。

    `block_draw_bets=True` 用于「只有听牌 + 3 家以上」：这种局面没有弃牌率，
    主动下注/加注是白送（h31：4 家池里 8 张顺子听、没人下注，模型挑了下注 1/2）。
    提示词管不住它把下注重新解释成「为权益而打」，所以干脆把这几档从动作空间里
    拿掉：模型只选档位，不给它选错的机会。

    `to_call == 0` 是「轮到我、没人下注」：没有可跟的注，选项是过牌 + 各种下注。
    `to_call > 0` 是面对下注/加注：跟注 + 弃牌 + 各种加注。

    金额口径（与求解器/牌桌语言的通行口径一致）：

      · 面对下注：跟注后底池 = 底池 + 需跟额；「加注到 f 倍池」= 加注额再取它的 f，
        即 `加注后的本街总额 = 当前最高下注 + f × (底池 + 需跟额)`。
      · 没人下注：「下注 f 倍池」= `底池 × f`。

    金额全部由本模块算好，模型只选档位。两处**必须**的修正：
    低于最小加注额的档位要抬到合法值（否则给出一个牌桌上做不出来的动作），
    超过后手的要封顶并注明等同全下。
    """
    if snap.street == PREFLOP:
        return build_preflop_options(snap, open_bonus=open_bonus)

    to_call = max(snap.to_call or 0.0, 0.0)
    stack = snap.hero_stack

    options: list[Option] = []
    if to_call <= 0:
        options.append(Option("call", "Check", "check",
                              note="no bet yet, check"))
    else:
        # 下注超过后手：跟注即全下，且不能再加注。客户端此时只给
        # Fold / All in 两个按钮，选项必须与牌桌一致。
        if stack is not None and stack > 0 and to_call >= stack:
            options.append(Option("call", "Call all-in", "call", _round_bb(stack),
                                  "the bet covers my stack; calling puts me all-in"))
            options.append(Option("fold", "Fold", "fold"))
            by_id = {o.id: o for o in options}
            return [by_id[i] for i in OPTION_ORDER if i in by_id]
        options.append(Option("call", "Call", "call", _round_bb(to_call)))
        options.append(Option("fold", "Fold", "fold"))

    if block_draw_bets:
        # 只剩过牌/跟注（必要时还有弃牌）：多路听牌没有弃牌率，下注/加注不是选项。
        by_id = {o.id: o for o in options}
        return [by_id[i] for i in OPTION_ORDER if i in by_id]
    presets = preset_targets(snap)
    for name in ("1/3", "1/2", "2/3", "1", "1.2"):
        option_id = f"raise-{name}"
        if to_call > 0:
            label, kind = OPTION_LABELS[option_id], "raise"
        else:
            label, kind = BET_LABELS[option_id], "bet"
        amount, note = presets[option_id]
        options.append(Option(option_id, label, kind, amount, note))

    by_id = {o.id: o for o in options}
    return [by_id[i] for i in OPTION_ORDER if i in by_id]


# ─────────────────────── 位置 / SPR / 赔率闸门 ───────────────────────

def call_amount(snap: HandSnapshot) -> float:
    """跟注实际要付的筹码：后手不足时只能跟到全下。

    赔率门槛、Math gate、事实渲染必须用同一个口径，否则「后手 5BB 面对
    20BB 下注」会被算成 20/25 = 80% 这种牌桌上不存在的门槛。
    """
    to_call = round(snap.to_call, 2)
    if snap.hero_stack is not None and 0 < snap.hero_stack < to_call:
        return round(snap.hero_stack, 2)
    return to_call


def _street_bet_from_actions(snap: HandSnapshot, opponent) -> float | None:
    """从本街行动线兜底取某座的本街下注总额（下注 pill 读不到时用）。

    行动线的 amount 是「本街加到总额」，同一座多次行动取最大值即可；
    全下记录尤其容易只有徽章没有金额，所以这个兜底不能省。
    """
    best: float | None = None
    for action in snap.actions_of(snap.street):
        if action.amount is None or action.action not in (
                "bet", "raise", "allin", "call"):
            continue
        if action.seat_id not in (opponent.seat_id, opponent.position) \
                and action.position != opponent.position:
            continue
        amount = float(action.amount)
        best = amount if best is None else max(best, amount)
    return best


def uncalled_excess(snap: HandSnapshot) -> float:
    """当前底池里对手下注超出「我能跟到的总额」的部分。

    全下完全 cover 我时，超出我后手的筹码无人能跟，会原样退回，根本不在
    底池里。牌桌显示的 pot 往往已经把它算进去了（990036：显示 569BB，
    实际可争夺 433.5BB）。只统计**没弃牌**的对手：弃牌玩家投进底池的钱
    是死钱，哪怕超过我的后手也仍然可以被我赢走。
    """
    owed = call_amount(snap)
    hero_total = (snap.hero_bet or 0.0) + owed
    excess = 0.0
    for opponent in snap.opponents:
        if opponent.folded:
            continue
        bet = opponent.bet
        if bet is None:
            bet = _street_bet_from_actions(snap, opponent)
        if bet is not None:
            excess += max(float(bet) - hero_total, 0.0)
    return round(excess, 2)


def winnable_pot(snap: HandSnapshot) -> float:
    """跟注后我实际能争夺的底池（包含我的跟注）。

    赔率的分母只能用这个数：`pot` 是牌桌显示的总池，对手超额全下被退回的
    那部分也在里面。跟注额已经由 `call_amount` 封顶，底池不扣同一部分，
    被 cover 时门槛就会被系统性低估（990036 的 23% vs 真实 39%）。
    """
    pot = snap.pot or 0.0
    if pot <= 0:
        # 底池读不到时不能拿「跟注额」冒充底池，否则门槛会变成 100%。
        return 0.0
    owed = call_amount(snap)
    return round(max(pot - uncalled_excess(snap) + owed, 0.0), 2)


def _action_order(snap: HandSnapshot,
                  street: str | None = None) -> list[str] | None:
    """本街的行动顺序。

    `positions.POSITIONS_BY_TABLE_SIZE` 是「从 SB 起、按行动方向」排的，
    即**翻后**顺序；翻前要把 SB/BB 挪到末尾（UTG 先说话）。
    """
    order = POSITIONS_BY_TABLE_SIZE.get(snap.table_size or 0)
    if not order:
        return None
    if snap.table_size == 2:
        # 单挑：按钮就是 SB —— 翻前 SB 先说话，翻后 BB 先说话。
        return [SB, BB] if (street or snap.street) == PREFLOP else [BB, SB]
    if (street or snap.street) == PREFLOP:
        return order[2:] + order[:2]
    return list(order)


def _can_act(opponent) -> bool:
    """还能主动行动：没弃牌，且后手不为 0（stack=0 就是全下）。"""
    return not opponent.folded and (opponent.stack is None or opponent.stack > 0)


def _positions_known(snap: HandSnapshot, order: list[str]) -> bool:
    """在局且还能行动的对手位置都在表里，才敢算「身后还有谁」。

    有一个对手位置没读出来时，算出来的「我是最后一个」可能是假的 ——
    宁可返回 None 让提示词省略这一行，也不要给模型一个错的「没人了」。
    """
    return all(o.position in order for o in snap.opponents if _can_act(o))


def _after_hero(snap: HandSnapshot, street: str | None = None) -> list[str]:
    """还在手、还能行动、且在本街行动顺序上排在我后面的位置。"""
    order = _action_order(snap, street)
    hero = snap.hero_position
    if order is None or hero not in order:
        return []
    idx = order.index(hero)
    live = {o.position for o in snap.opponents if _can_act(o)}
    return [p for p in order[idx + 1:] if p in live]


def players_left_to_act(snap: HandSnapshot) -> int | None:
    """我之后还有几个在局对手要说话。未知位置返回 None（不要瞎猜成 0）。

    「身后还有人」直接决定能不能诈唬、要不要用中等牌控池：0 人时我是
    最后一个行动，下注可以免费看摊牌；≥1 人时下注会被加注惩罚。
    """
    order = _action_order(snap)
    if (order is None or snap.hero_position not in order
            or not _positions_known(snap, order)):
        return None
    return len(_after_hero(snap))


def hero_in_position(snap: HandSnapshot) -> bool | None:
    """翻后我是否最后一个行动（按翻后顺序算，翻前也能给出「翻后有没有位置」）。"""
    order = _action_order(snap, street=FLOP)
    if (order is None or snap.hero_position not in order
            or not _positions_known(snap, order)):
        return None
    return not _after_hero(snap, street=FLOP)


def pot_type(snap: HandSnapshot) -> str | None:
    """底池类型：翻前动作线决定翻后的范围宽度与 SPR 语义。"""
    shapes = set(snap.preflop_shapes)
    if "three-bet" in shapes:
        return "3-bet+ pot"
    if "single-raise" in shapes:
        return "single-raised pot"
    if "limped" in shapes:
        return "limped pot"
    if "unopened" in shapes:
        return "unopened pot"
    return None


def spr(snap: HandSnapshot) -> float | None:
    """SPR = 有效筹码 ÷ 当前底池（翻后才有意义）。"""
    if snap.street == PREFLOP or not snap.pot or snap.hero_stack is None:
        return None
    stacks = [o.stack for o in snap.opponents
              if not o.folded and o.stack is not None]
    if not stacks:
        return round(snap.hero_stack / snap.pot, 1)
    effective = min([*stacks, snap.hero_stack])
    return round(effective / snap.pot, 1)


# 听牌跟注的隐含赔率上限：提示词、Math gate、单测三处必须是同一个数。
# 2026-10-24 用户口径：隐含赔率按 0 算，只用即时赔率。
IMPLIED_ODDS_CAP_PCT = 0.0
# 权益与门槛差在这个范围内算「边界」，不给硬结论。
GATE_MARGIN_PCT = 2.0


def implied_odds_rule() -> str:
    """提示词里的听牌赔率口径；与 IMPLIED_ODDS_CAP_PCT 保持同一个数。"""
    if IMPLIED_ODDS_CAP_PCT <= 0:
        return ("Ignore implied odds: for draws, call only when equity >= the "
                "break-even equity; otherwise FOLD — do not pay to chase.")
    points = "point" if IMPLIED_ODDS_CAP_PCT == 1 else "points"
    return (f"For draws, count implied odds as AT MOST "
            f"{IMPLIED_ODDS_CAP_PCT:g} percentage {points}: call only when "
            f"(equity + {IMPLIED_ODDS_CAP_PCT:g}%) >= the break-even equity; "
            "otherwise FOLD — do not pay to chase.")


@dataclass(frozen=True)
class MathGate:
    """把「赔率 vs 权益」算成一句结论，而不是让模型自己比两个数。

    h104 的教训：模型自己写出「36% vs 29%」然后选了弃牌。规则写在提示词里
    它也会违反，所以这里由程序给出 call/fold 的判定，`advise()` 再用它做
    一次重问与违规标记。
    """

    verdict: str                  # call | fold | borderline
    equity_pct: float             # 四二法则的听牌权益
    break_even_pct: float
    text: str


def math_gate(sit: "Situation") -> MathGate | None:
    """只对**听牌**给硬结论：成牌的胜率是对随机牌的枚举，不是对跟注范围。"""
    if sit.street == PREFLOP:
        return None
    if sit.hand_tier not in (STRONG_DRAW, WEAK_DRAW):
        return None
    if sit.equity_pct is None:
        return None
    owed = call_amount(sit.snap)
    pot = winnable_pot(sit.snap)
    if owed <= 0 or pot <= 0:
        return None
    bep = owed / pot * 100
    effective = sit.equity_pct + IMPLIED_ODDS_CAP_PCT
    diff = effective - bep
    if diff >= GATE_MARGIN_PCT:
        verdict = "call"
        tail = ("→ CALL is +EV on direct odds. A fold needs a concrete reason "
                "(outs dead or dominated), not \"the price is thin\".")
    elif diff <= -GATE_MARGIN_PCT:
        verdict = "fold"
        tail = "→ FOLD: do not pay to chase."
    else:
        verdict = "borderline"
        tail = ("→ BORDERLINE: the price alone does not decide it; call only "
                "with a concrete read.")
    if IMPLIED_ODDS_CAP_PCT > 0:
        effective_text = (f"draw equity {sit.equity_pct:.0f}% + "
                          f"{IMPLIED_ODDS_CAP_PCT:g}% implied cap = "
                          f"{effective:.0f}%")
    else:
        effective_text = f"draw equity {sit.equity_pct:.0f}% (implied odds 0%)"
    text = (f"Math gate: {effective_text} vs "
            f"break-even {bep:.0f}% {tail}")
    return MathGate(verdict, round(sit.equity_pct, 1), round(bep, 1), text)


# ─────────────────────────── 牌况 ───────────────────────────

@dataclass(frozen=True)
class Situation:
    """一次决策的结构化事实。提示词由它渲染，规则兜底也用它。"""

    snap: HandSnapshot
    options: list[Option]
    hand_label: str | None
    hand_tier: str | None
    category: int | None
    texture: str | None
    has_draw: bool
    plays_board: bool
    opponent_strength: dict | None
    warnings: list[str] = field(default_factory=list)
    # 英文渲染：coach 的中文标签是 /coach/advice 的口径，给模型的提示词用英文
    hand_label_en: str | None = None
    texture_en: str | None = None
    opponent_strength_en: dict | None = None
    # 四二法则估的成牌率（0-100）。给提示词做「赔率 vs 胜率」比较用
    equity_pct: float | None = None
    # 人工维护的牌桌动态（根目录 .table_dy）。空/None = 按「未知对手」标准线。
    table_dynamics: str | None = None
    # 爆炸底池：上一街底池 ÷ 实际参与人数（BB/人）及其是否刚好 5BB。
    # 见 previous_street_pot() / is_explosive_pot()。
    pot_per_player: float | None = None
    explosive_pot: bool = False
    # 成手牌的**实际排名**：枚举对手两手牌，算多大概率比我这手大（0-100）。
    # beat_pct 是单个对手，beat_at_least_pct 是当前还在手的人数下至少一家领先。
    # 见 coach.beat_rate ——「两对」是个名字，这两个数才是强度。
    beat_pct: float | None = None
    beat_at_least_pct: float | None = None
    opponents_in_hand: int | None = None
    # 翻前：这手牌在该位置的开池范围归属（见 preflop_ranges.range_verdict）。
    # 由程序算、作为事实注入，模型只负责在事实之上做选择。
    range_verdict: str | None = None
    # 牌力事实块：最好的五张、精确坚果校验、听牌、牌面结构。
    # 由 ground_truth.compute_ground_truth 生成；None = 翻前/牌不全。
    ground_truth: GroundTruth | None = None
    # 桌风是否明确含「松浪/松凶」。混合桌也可以为 True：只要桌上有一个
    # LAG，中等成牌就不能默认做薄价值。见 table_dynamics.is_aggressive_table。
    aggressive_table: bool = False
    # 当前改良 outs（coach.Read.outs）。用于区分「成牌+弱听」和「成牌+强听」：
    # 4 张卡顺不该豁免「被统治」判定，8+ 张 combo draw 才该按半诈唬处理。
    outs: int | None = None
    # 本手在局对手的画像（按 seat_id 对回具体的人）。空 = 没有画像，
    # 行为退回 `.table_dy` / aggressive_table 的旧口径。
    opponent_reads: tuple[OpponentRead, ...] = ()
    # 翻后决策的几何事实。由程序算、直接渲染进事实块：SPR 决定「顶对能不能
    # 打光」，位置/身后人数决定「能不能诈唬」，让模型自己推会推出两套口径。
    pot_type: str | None = None
    spr: float | None = None
    in_position: bool | None = None
    players_left_to_act: int | None = None
    # 上一街自己的决定（同手）。空 = 本手第一个决策点，或跨手重放。
    previous: "Advice | None" = None
    # GTO 近似表查询结果（单挑/单加注池/翻牌或转牌才有）。None = 不覆盖或查不到。
    gto: "object | None" = None
    # 牌面范围权益（Monte Carlo，多人池也能用）。None = 没表/翻前/位置不明。
    board_eq: "object | None" = None
    # 手牌 vs 对手范围（实时牌面范围 + 精确 behind + MC equity）。
    # None = 翻前/牌面不足/没有可确认的对手范围。
    hand_ranges: "object | None" = None

    @property
    def street(self) -> str:
        return self.snap.street


def _read(snap: HandSnapshot):
    """翻后读牌：返回 coach.Read 或 None（翻前 / 牌不全 / 读不出）。"""
    if snap.street == PREFLOP:
        return None
    if not snap.hero_cards or len(snap.board) < 3:
        return None
    try:
        return read_hand(parse_cards("".join(snap.hero_cards)),
                         parse_cards("".join(snap.board[:5])))
    except ValueError:
        return None


def _precise_texture_en(gt: GroundTruth | None, texture: str | None) -> str | None:
    """给模型/复盘看的牌面标签。两张同花不能再叫 "Flush + straight"。

    规则引擎的 `board_texture` 仍按历史口径（2 张同花 = 花面）用于下注尺度；
    这里只改「事实怎么说」，避免 990010 那类「只有花听被读成已成花」。
    """
    if gt is None:
        return _TEXTURE_EN.get(texture, texture) if texture else None
    b = gt.board_info
    parts: list[str] = []
    if b.flush_made_possible:
        parts.append("Flush possible")
    elif b.flush_draw_possible:
        parts.append("Two-tone (flush draw only — no made flush)")
    else:
        parts.append("Rainbow")
    if b.paired:
        parts.append("paired")
    if b.straight_possible:
        parts.append("straight possible")
    return " + ".join(parts)


def build_situation(snap: HandSnapshot,
                    table_dynamics: str | None = None,
                    opponent_reads: Iterable[OpponentRead] | None = None,
                    previous: "Advice | None" = None
                    ) -> Situation:
    """把快照整理成一次决策的事实。

    `table_dynamics` 是根目录 `.table_dy` 的解析文本（见 table_dynamics.py）。
    由调用方显式传入，而不是在这里读文件：单测和离线重放不会因为仓库里恰好
    有一份动态文件而改变结果。

    `opponent_reads` 是 ProfileStore.read_for() 的结果（按本手 seat_id）。
    没有画像时 `aggressive_table` 仍是 fallback，行为不会退化。
    """
    reads = tuple(opponent_reads or ())
    read = _read(snap)
    warnings: list[str] = list(snap.warnings)
    n_opp = sum(1 for o in snap.opponents if not o.folded)

    # 事实块只算一次：牌面结构、最好五张、精确排名、坚果校验全从这里出。
    # 实时建议和复盘共用同一个 GroundTruth，避免「一个说听牌没成、一个说成对」。
    gt = None
    if read is not None:
        try:
            gt = compute_ground_truth(list(snap.hero_cards),
                                      list(snap.board[:5]), n_opp)
        except (ValueError, TypeError):
            gt = None

    texture = None
    if gt is not None:
        texture = gt.board_info.texture
    elif read is not None:
        try:
            texture, _tags = board_texture(parse_cards("".join(snap.board[:5])))
        except ValueError:
            texture = None

    opp = None
    opp_en = None
    if snap.street != PREFLOP:
        opp_read = opponent_strength(_coach_actions(snap), snap.street,
                                     HERO_ACTOR, snap.pot or 0.0)
        opp = opp_read.to_dict()
        opp_en = {"value": opp_read.value, "label": opp_read.label_en,
                  "reason": opp_read.reason_en}

    per_player = pot_per_player(snap)
    # 纯听牌 + 3 家以上：没有弃牌率，主动下注/加注不该出现在选项里（h31）。
    # 「纯听」= 读牌分档在听牌档；已经成牌（一对以上）不在此列——
    # 那种情况是价值下注，哪怕多路也允许。
    draw_only = bool(read and read.tier in (STRONG_DRAW, WEAK_DRAW))
    block_draw_bets = (snap.street != PREFLOP and n_opp >= 3 and draw_only)
    beat_pct = beat_at_least = None
    if gt is not None:
        beat_pct = round(gt.rank.beat_pct * 100, 1)
        beat_at_least = round(gt.rank.beat_at_least_pct * 100, 1)
    elif read is not None:
        # 兜底：gt 计算失败时仍复用 read_hand 已经枚举过的单人比例
        p = read.beat_pct
        beat_pct = round(p * 100, 1)
        beat_at_least = round((1.0 - (1.0 - p) ** max(n_opp, 1)) * 100, 1)
    sit = Situation(
        snap=snap,
        options=build_options(snap,
                              open_bonus=(LOOSE_OPEN_BONUS
                                          if is_loose_table(table_dynamics)
                                          else 0.0),
                              block_draw_bets=block_draw_bets),
        hand_label=read.label if read else None,
        hand_tier=read.tier if read else None,
        category=read.category if read else None,
        texture=texture,
        has_draw=bool(read and (read.outs >= 4)),
        plays_board=bool(read and read.plays_board),
        opponent_strength=opp,
        warnings=warnings,
        hand_label_en=read.label_en if read else None,
        texture_en=_precise_texture_en(gt, texture),
        opponent_strength_en=opp_en,
        equity_pct=read.equity_pct if read else None,
        table_dynamics=(table_dynamics or "").strip() or None,
        pot_per_player=per_player,
        explosive_pot=is_explosive_pot(snap),
        beat_pct=beat_pct,
        beat_at_least_pct=beat_at_least,
        opponents_in_hand=n_opp,
        range_verdict=(
            (lambda v: v.render() if v else None)(
                range_verdict(snap.hero_position, snap.hero_cards,
                              table_dynamics=table_dynamics,
                              limpers=count_limpers(snap)))
            if snap.street == PREFLOP else None),
        ground_truth=gt,
        aggressive_table=is_aggressive_table(table_dynamics),
        outs=read.outs if read else None,
        opponent_reads=reads,
        pot_type=pot_type(snap),
        spr=spr(snap),
        in_position=hero_in_position(snap),
        players_left_to_act=players_left_to_act(snap),
        previous=previous,
    )
    # GTO 表是可选事实层：查不到/没装/多人池都静默跳过，绝不影响主链路。
    # Situation 是 frozen 的，用 replace 带出新实例。
    try:
        from . import gto_lookup
        sit = replace(sit, gto=gto_lookup.lookup(sit))
    except Exception:  # noqa: BLE001
        pass
    # 手牌 vs 对手范围：答案在 ground truth 之后、board structure 之前。
    # 任何一步缺数据都 fail open，绝不影响主链路。
    try:
        from . import hand_vs_ranges
        hand_ranges = hand_vs_ranges.lookup(sit)
        if hand_ranges is not None:
            # 对手范围缺失/短码这类缺口要进 Data gaps，模型才知道哪些座位
            # 是 range unknown，而不是把随机牌排名当成范围。
            merged = list(dict.fromkeys([*sit.warnings,
                                         *hand_ranges.warnings]))
            sit = replace(sit, hand_ranges=hand_ranges, warnings=merged)
        elif hand_vs_ranges.enabled() and snap.street != PREFLOP \
                and len(snap.board) >= 3:
            # 全部座位都算不出范围时，也要把“range unknown”写进 Data gaps。
            from . import field_ranges
            gaps = field_ranges.resolve(snap).warnings
            if gaps:
                sit = replace(sit, warnings=list(dict.fromkeys(
                    [*sit.warnings, *gaps])))
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import board_equity
        sit = replace(sit, board_eq=board_equity.lookup(sit))
    except Exception:  # noqa: BLE001
        pass
    return sit


def _coach_actions(snap: HandSnapshot) -> list[dict]:
    """把快照的行动线转成 coach / call-back 的 action 字典。

    amount 口径与 coach.advise 一致：bet/raise 是「加到总额」，跟注不带金额。
    """
    out: list[dict] = []
    for rec in snap.all_actions():
        out.append({
            "actor": HERO_ACTOR if rec.is_hero else rec.seat_id,
            "action": rec.action,
            "amount": rec.amount,
            "street": rec.street,
        })
    return out


# ─────────────────────────── 价值取向 ───────────────────────────

# 这些分档的默认动作是「下注/加注拿价值」，不是过牌/跟注。
_VALUE_TIERS = (STRONG_MADE, OVERPAIR, TPTK)


def _flush_possible(board: tuple[str, ...] | list[str]) -> bool:
    """牌面是否已经三张同花（任何对手拿两张同花就是同花）。"""
    return len(board) >= 3 and max(Counter(c[1] for c in board).values()) >= 3


def _board_paired(board: tuple[str, ...] | list[str]) -> bool:
    return any(v >= 2 for v in Counter(c[0] for c in board).values())


def _is_flush_or_better(sit: Situation) -> bool:
    """当前牌型本身是不是同花/葫芦/四条（花面上不怕被同花打败）。"""
    label = f"{sit.hand_label_en or ''} {sit.hand_label or ''}"
    return any(k in label for k in
               ("Flush", "Full house", "Quads", "同花", "葫芦", "四条"))


def _is_boat_or_better(sit: Situation) -> bool:
    """葫芦/四条 —— **公对面**上唯一能豁免的牌型。

    公对面不能用 `_is_flush_or_better` 豁免：同花打得过三条，却打不过葫芦，
    而跟注站的跟注范围里就有葫芦（h100 的 44、h75 的 22 都是被这一条吃掉的）。
    """
    label = f"{sit.hand_label_en or ''} {sit.hand_label or ''}"
    return any(k in label for k in
               ("Full house", "Quads", "Straight flush", "葫芦", "四条", "同花顺"))


def _facing_raise(snap: HandSnapshot) -> bool:
    """本街是不是面对加注/全下（而不是一条普通下注）。

    单对子（超对/顶对）在**被加注**时和面对普通下注是两种局面：前者是
    「要不要把筹码打光」的问题，默认弃牌；后者才是抓诈/防守。用动作类型
    判断，不用下注比例 —— 990044 的全下只占池一半，但仍然是全下。
    """
    return any((not a.is_hero) and a.action in ("raise", "allin")
               for a in snap.actions_of(snap.street))


# 对手画像改硬指令的门槛：pin 直接信；自动画像要达到 read/stable 门控。
_READ_CONFIDENCE = 0.65


def last_aggressor_seat(snap: HandSnapshot) -> str | None:
    """本街最后一个 bet/raise 的座位。跟注决策看这个人，不看整桌平均。"""
    for action in reversed(snap.actions_of(snap.street)):
        if action.action in ("bet", "raise"):
            return action.seat_id
    return None


def _reliable_reads(sit: Situation) -> list[OpponentRead]:
    """本手在局、且可信到能改硬指令的画像。

    · 已弃牌的对手不参与；
    · unknown 不参与；
    · tentative（样本 8–14 或置信度不够）只展示，不改硬指令；
    · 人工 pin 置信度恒为 1，直接参与。
    """
    folded = {o.seat_id for o in sit.snap.opponents if o.folded}
    out: list[OpponentRead] = []
    for read in sit.opponent_reads:
        if not read.seat_id or read.seat_id in folded:
            continue
        if read.archetype == "unknown":
            continue
        if read.source == "pin":
            out.append(read)
        elif read.gate in ("read", "stable") and read.confidence >= _READ_CONFIDENCE:
            out.append(read)
    return out


def decision_read(sit: Situation) -> OpponentRead | None:
    """这一决策真正该看的那个对手。

    · 面对下注：看本街最后一个下注/加注的人；
    · 无人下注：只要本手有可信的 LAG/maniac，就看他们 —— 薄价值下注会被
      他们加注；全是 station/nit 时才按价值分支处理。
    """
    reads = _reliable_reads(sit)
    if not reads:
        return None
    if (sit.snap.to_call or 0.0) > 0:
        seat = last_aggressor_seat(sit.snap)
        for read in reads:
            if read.seat_id == seat:
                return read
        # 下注的人没有画像：不要拿别的对手的标签替它做决定，留给桌风 fallback。
        return None
    # 无人下注时，价值下注的风险来自「还在后面、可能加注的人」。只要本手
    # 有可信的 LAG/maniac，就不能按薄价值下注（h8 的 BTN 就是这种人）。
    # 全是 station/nit 时才走价值分支。
    aggressive = [r for r in reads if r.archetype in AGGRESSIVE_ARCHETYPES]
    if aggressive:
        return max(aggressive, key=lambda r: (r.confidence, r.n))
    return max(reads, key=lambda r: (r.confidence, r.n))


def _aggressive_context(sit: Situation) -> bool:
    """本决策是否按「松浪/松凶在局」处理。

    画像优先；没有可用画像时才用整桌关键词。逐座位画像是按人统计的，
    比「整桌有一个 LAG」这种桌级判断准。
    """
    read = decision_read(sit)
    if read is not None:
        return read.archetype in AGGRESSIVE_ARCHETYPES
    return sit.aggressive_table


def _passive_value_line(sit: Situation) -> bool:
    """本街的多街 aggression 是否该读成价值线。

    画像优先：本街下注者若是可信的 station/nit，他的大注/连续下注是价值；
    若是 LAG/maniac，则不是。没有可用画像时才退回桌级关键词。
    只在转牌/河牌、面对下注时触发；翻牌的单条街下注不在此列——那时的价格
    通常还够用，过早弃掉 JJ 这类牌会变成另一种漏洞。
    """
    if not (sit.street in (TURN, RIVER)
            and (sit.snap.to_call or 0.0) > 0):
        return False
    read = decision_read(sit)
    if read is not None:
        return read.archetype in PASSIVE_ARCHETYPES
    return value_heavy_aggression(sit.table_dynamics)


def _is_two_pair(sit: Situation) -> bool:
    label = f"{sit.hand_label_en or ''} {sit.hand_label or ''}".lower()
    return "two pair" in label or "两对" in label


def _heads_up_flop_overpair(sit: Situation) -> bool:
    """单挑翻牌、超对、面对一枪加注且赔率不差 → 至少跟一枪。

    2026-10-23 用户口径：KK 在 8d10c4c 单挑翻牌被 BB check-raise 一枪，
    「一对 vs 加注 → 弃」把超对也一起弃了，太紧弱。超对不是边际一对：
    对手的加注范围里仍有顶对/听牌，22.5 跟 68 只需要 25% 胜率。只有
    多人池、转/河、或加注大到赔率不够（跟注额 > 半个底池）时才弃。
    """
    if sit.hand_tier != OVERPAIR or sit.street != FLOP:
        return False
    if (sit.opponents_in_hand or 0) != 1:
        return False
    pot = winnable_pot(sit.snap)
    owed = call_amount(sit.snap)
    pot_before_call = max(pot - owed, 0.0)
    return pot > 0 and 0 < owed <= 0.5 * pot_before_call


# ─────────────────────────── 爆炸底池 ───────────────────────────

# 爆炸底池的前注是固定金额：每个参与者在翻前正好交 5BB（8 人局 40BB、
# 9 人局 45BB）。所以判据是「上一街底池 ÷ 实际参与人数 ≈ 5BB」——刚好
# 整除，而不是 ≥5BB：普通大池的人均可以超过 5BB，但只有炸弹池的总额
# 严格等于 5BB × 人数。命中后全桌都进池，翻后会有大量歪中的两对/三条，
# 顶对只是中等牌，松弱玩家主动打出来的牌也不会小。
EXPLOSIVE_POT_BB_PER_PLAYER = 5.0
# 底池按 0.01BB 显示、人数是整数，理论上能整除；留一分钱误差吸收浮点。
EXPLOSIVE_POT_TOLERANCE = 0.01


def previous_street_pot(snap: HandSnapshot) -> float | None:
    """上一街结束时的底池（BB）。翻前或数据缺失 → None。

    主口径是 `pot_before_street`（界面上的 pbs pill，正是「本街开始前的底池」）。
    这个 pill 实测偶尔会把底池值读到 pbs 槽：当本街已有下注、pbs 却等于底池时，
    用 `pot − 本街所有下注` 反推（客户端底池含本街下注）。反推需要每座下注额
    都读到；有任一未知就继续用 pbs，最后退回本街的 `pot_start`。
    """
    if snap.street == PREFLOP:
        return None
    bets = [snap.hero_bet, *(o.bet for o in snap.opponents)]
    bets_known = all(b is not None for b in bets)
    street_bets = sum(bets) if bets_known else 0.0

    pbs = snap.pot_before_street
    if pbs is not None:
        misread = (snap.pot is not None and bets_known and street_bets > 0
                   and pbs >= snap.pot - 1e-9)
        if misread:
            return max(snap.pot - street_bets, 0.0)
        return pbs
    if snap.pot is not None and bets_known:
        return max(snap.pot - street_bets, 0.0)
    for rec in snap.streets:
        if rec.street == snap.street:
            return rec.pot_start
    return None


def pot_per_player(snap: HandSnapshot) -> float | None:
    """上一街底池 ÷ 实际参与人数（BB/人）。翻前 / 人数未知 / 底池未知 → None。

    参与人数含本街已弃牌的人（炸弹池前注是翻前交的），不含空座/离桌。
    `table_size` 是座位数，9 人桌固定为 9；桌上有空座时用它除会低估人均
    （8 人交前注的 40BB 池，40 ÷ 9 = 4.44，而不是 40 ÷ 8 = 5）。
    `players_dealt` 缺失时退回座位数。
    """
    pot = previous_street_pot(snap)
    count = snap.players_dealt or snap.table_size
    if pot is None or count <= 0:
        return None
    return pot / count


def is_explosive_pot(snap: HandSnapshot) -> bool:
    """这手是不是爆炸底池。

    两个来源，任一成立即可：

      · **屏幕横幅**（`snap.bomb_pot`）：开局中央弹出「幸运暴击 / BOMB POT」，
        这是客户端自己宣布的爆炸底池，最权威。人数或底池读数有偏差时，
        横幅仍然能确认。
      · **底池推算**：翻后「上一街底池 ÷ 实际参与人数」刚好等于 5BB/人。
        炸弹池前注固定 5BB/人，所以总额能被人数整除（40BB ÷ 8 人 = 5BB）；
        普通大池人均可能更高，但不会刚好整除。用于横幅没抓到的手牌
        （中途接管、横幅动画被遮挡/丢弃）。
    """
    if snap.bomb_pot:
        return True
    ratio = pot_per_player(snap)
    if ratio is None:
        return False
    return abs(ratio - EXPLOSIVE_POT_BB_PER_PLAYER) <= EXPLOSIVE_POT_TOLERANCE


def _explosive_reason(sit: Situation) -> str:
    """爆炸底池的事实来源，用于给模型解释「为什么」而不是只给标签。

    横幅触发时直接说是炸弹池的前注；只有底池推算命中时才说人均 5BB，
    避免模型把横幅和精确数字混为一谈。
    """
    if sit.snap.bomb_pot:
        return "this is a bomb pot: everyone posted a forced ante"
    return "the pot entering this street is exactly 5BB per player (the bomb ante)"


def _pick_size(sit: Situation, want: float) -> str | None:
    """在本次可选的档位里，挑最接近 `want`（底池倍数）的那个 option id。

    为什么要点名：`value_directive` 原来只写「BET 2/3–full pot」，模型照样挑 1/3
    （复盘里 12 次翻后下注有 9 次是 1/3，赢的池最大只有 60.5）。直接把 id 写进事实里，
    比让它自己把「2/3」映射到某个按钮可靠。短码时档位会被 `_cap` 封顶，所以只在
    **实际存在**的选项里挑。
    """
    targets = {"raise-1/3": 1 / 3, "raise-1/2": 0.5, "raise-2/3": 2 / 3,
               "raise-1": 1.0, "raise-1.2": 1.2}
    ids = [o.id for o in sit.options
           if o.id in targets and o.kind in ("bet", "raise")]
    if not ids:
        return None
    return min(ids, key=lambda i: abs(targets[i] - want))


def _no_fold_equity(sit: Situation) -> bool:
    """3 家以上、手里又只有听牌：主动下注/加注没有弃牌率。

    跟注站不会因为一张听牌面弃掉任何对子或听牌；多路池里「用听牌下注」唯一的
    效果是把底池做大、把跟注站留在手里。h31 就是这么输的（4 家、8 张顺子听、
    下注 1/2 池），模型还把它解释成「为权益而打」，所以不能只靠提示词。
    """
    return (sit.street != PREFLOP
            and (sit.opponents_in_hand or 0) >= 3
            and sit.hand_tier in (STRONG_DRAW, WEAK_DRAW))


def value_directive(sit: Situation) -> str | None:
    """把「强牌默认拿价值」写成一句贴着当前分档的硬指令。

    为什么放在 user 事实里、而不是只写在 system 技能里：JEV 是选择题，
    技能目录很长，模型容易只抓到最近的一条「对手弱可以过牌」；一条紧跟
    「My hand strength」的具体指令，比让它自己翻方法论可靠得多。
    """
    if sit.street == PREFLOP:
        return None
    if sit.plays_board:
        # 打牌面 = 最好的五张全靠公共牌，人人都有。它不是「一对」，没有摊牌价值。
        # 只在板面自己只是「一对/两对」时才提醒：板面自己成了顺子/同花时，
        # 「谁都比你大」是错的（那是分池的牌）。
        if sit.category is not None and sit.category <= TWO_PAIR:
            return ("BOARD PLAY: your best five are all community cards — you have no "
                    "hand of your own, and any pair beats you. Betting is a pure bluff "
                    "with no showdown value: at most ONE small bet on the flop. On the "
                    "turn or river with no improvement, check and give up; never call a "
                    "raise and never fire a second barrel.")
        return None
    tier = sit.hand_tier
    facing = (sit.snap.to_call or 0.0) > 0
    facing_raise = facing and _facing_raise(sit.snap)
    aggressive = _aggressive_context(sit)
    read = decision_read(sit)
    read_tag = (f" [{read.display_name or read.key} = {read.archetype.upper()}, "
                f"conf {read.confidence:.2f}]" if read is not None else "")

    # **牌型排名优先于牌型名字。** 下面这条必须在所有按分档写的指令之前：
    # 一个「两对」如果枚举下来四分之一的随机牌都比它大，它就不是价值牌，
    # 任何「bet small for value」的措辞都会把模型推向送钱。
    # 用**单人**比例做判据（不是全桌）：全桌比例会被人数放大，见 DOMINATED_BEAT_PCT。
    # 只对**成牌**生效：听牌的枚举结果反映的是「当前还没成牌」，没有意义。
    # 成牌+4-7 张弱听不豁免（h8 的 QJ 在 AAQK 上多 4 张 T，不能因此变成价值牌）；
    # 只有 8+ 张强听（combo draw）才按半诈唬处理，交给下面的听牌分支。
    # 松浪桌不走这条、交给下面的 aggressive 分支：动作同样是 check，但会补
    # 「让对手先开枪 / 抓诈」的后续计划。
    if (not facing
            and not aggressive
            and sit.hand_tier in (STRONG_MADE, OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM)
            and not (sit.has_draw and (sit.outs or 0) >= 8)
            and (sit.beat_pct or 0.0) >= DOMINATED_BEAT_PCT):
        n = sit.opponents_in_hand or 1
        return (f"NOT A VALUE HAND BY RANK: nominal {sit.hand_label_en or 'made hand'}, "
                f"but an exact count over every opponent holding has it beaten by "
                f"{sit.beat_pct:g}% of random hands per opponent "
                f"(with {n} opponent(s) in, {sit.beat_at_least_pct:g}% chance at least "
                f"one is ahead). The hand's NAME is not its RANK. "
                f"Do NOT bet or raise for value — anything that calls has you beaten. "
                f"Check and give up; call one small bet as a bluff-catcher only when the "
                f"price is very good. If you bet, it is a bluff with no fold equity.")

    # 2026-10-23 用户口径：TT 以下（22-99）都按买 set 玩。没中 set 时，
    # 超对/中对只是中等牌 —— 3-4 人池里 99 有 ~79%、77 有 ~92% 的概率见高张，
    # 不能拿「超对」的名头去价值下注/打光。TT+ 不受这条约束。
    if _is_set_mine_pair(sit.snap.hero_cards) and tier in (OVERPAIR, MID_BOTTOM):
        n = sit.opponents_in_hand or 1
        way = f"{n + 1}-way"
        if facing_raise:
            return (f"SET-MINE PAIR ({way}) vs AGGRESSION: 22-99 unimproved is only "
                    "one pair — FOLD to the raise/all-in. Do not stack off; no set, "
                    "no big pot.")
        if facing:
            if _passive_value_line(sit):
                return ("TABLE DYNAMICS OVERRIDE: 22-99 unimproved is a medium hand and "
                        "the opponent's raises/barrels are value-heavy — FOLD to the "
                        "turn/river barrel. Do not pay off with one pair.")
            return (f"SET-MINE PAIR ({way}): 22-99 unimproved is a medium hand / "
                    "bluff-catcher — call one small bet (up to ~1/2 pot), fold to a "
                    "large bet or multi-street barrel. Do not stack off.")
        pick = _pick_size(sit, 0.4)
        hint = f" Use option {pick}." if pick else ""
        return (f"SET-MINE PAIR ({way}): 22-99 unimproved is a MEDIUM hand, not a value "
                "stack-off — overcards dominate it. Bet small (1/3–1/2 pot) for "
                "protection or check; do not build a big pot." + hint)

    # 松浪/松凶桌：中等成牌默认不是薄价值牌。对手的范围更宽、更爱加注，
    # OOP 主动下注只会让更差的牌弃掉、更好的牌跟/加。混合桌也走这条：
    # 只要桌上有一个 LAG，就不该把全桌默认成跟注站。
    if aggressive:
        medium = (sit.category == TWO_PAIR
                  or tier in (TPTK, TP_WEAK, MID_BOTTOM))
        if medium and not facing and not (
                sit.ground_truth is not None and sit.ground_truth.hero_is_nuts):
            return ("AGGRESSIVE TABLE — DO NOT THIN-VALUE: a maniac/LAG is in the "
                    "hand. This medium made hand (two pair / one pair) is a CHECK, "
                    "not a small value bet: a bet folds out the hands you beat and "
                    "gets called or raised by better. Check, let them barrel, and "
                    "call one bet as a bluff-catcher. Do not bet-fold." + read_tag)
        if medium and facing and not facing_raise:
            return ("AGGRESSIVE TABLE — BLUFF-CATCH: a maniac/LAG's single bet is "
                    "not automatically value. CALL one bet with this medium made "
                    "hand; do not fold to a single barrel. Fold only to a raise, "
                    "an overbet, or a multi-street barrel." + read_tag)

    if tier == STRONG_MADE:
        # 精确坚果：枚举结果说「没有任何对手持牌能打败我」。这时模型最常见的
        # 退化是「怕一个牌面上不存在的更大的牌」而只跟（990010 坚果顺 → call）。
        # 用一句明确的 ABSOLUTE NUTS 指令压住它，而不是让它自己组合推理。
        if sit.ground_truth is not None and sit.ground_truth.hero_is_nuts:
            size = "1/2–2/3 pot" if sit.texture == DRY else "2/3–full pot"
            pick = _pick_size(sit, 0.5 if sit.texture == DRY else 0.7)
            hint = f" Use option {pick}." if pick else ""
            if facing:
                return ("VALUE DEFAULT — ABSOLUTE NUTS: the evaluator enumerated every "
                        f"opponent holding and NONE beats you. RAISE for value (2/3–full "
                        f"pot).{hint} Do NOT call, do NOT check, and do NOT describe a "
                        "flush/full house/any higher hand as possible — the facts list "
                        "no hand that beats you.")
            return (f"VALUE DEFAULT — ABSOLUTE NUTS: no opponent holding beats you. BET "
                    f"for value ({size}).{hint} Do NOT check out of fear of a better hand; "
                    "the facts list nothing that beats you. Slow-play only if you can "
                    "name a concrete reason from the action line, not from the board.")

        # 顺子/同花/两对不是「有名字就强」：牌力要看它在**这个牌面**上的排名。
        #   · 公对面：连**同花**都可能被葫芦打败（h100 的 44 就是这一类），
        #     只有葫芦/四条以上才豁免；
        #   · 纯花面（未成对）：顺子/三条不是坚果，同花以上才豁免。
        # 为什么同花也不再豁免：这桌是跟注站，能跟到底的正是同花和葫芦。
        if _board_paired(sit.snap.board):
            not_nuts = not _is_boat_or_better(sit)
        elif _flush_possible(sit.snap.board):
            not_nuts = not _is_flush_or_better(sit)
        else:
            not_nuts = False
        if facing and not_nuts:
            if _passive_value_line(sit) and _is_two_pair(sit):
                return ("TABLE DYNAMICS OVERRIDE: the player's table notes say this "
                        "opponent's raises and multi-street barrels are value-heavy. "
                        "Two pair is a bluff-catcher here, not a value hand. Do NOT raise. "
                        "FOLD to the turn/river barrel; call only with a set/straight/"
                        "flush or a strong draw at the right price. Pot odds alone do not "
                        "justify paying off a value-heavy range.")
            return ("VALUE CAUTION: strong made hand, but the board allows a better hand "
                    "(three to a flush or a paired board), so your straight/set/two pair "
                    "is NOT the nuts. Do NOT raise. Call a normal-sized bet (up to about "
                    "2/3 pot) when the price is good; fold only to a large bet (pot-sized "
                    "or more) or to a raise/all-in.")
        if facing:
            return ("VALUE DEFAULT: strong made hand facing a bet — RAISE for value. "
                    "Call only if the board is four-to-a-straight/flush or the opponent's "
                    "line says you are beaten.")
        if not_nuts:
            return ("VALUE CAUTION: this made hand is NOT the nuts on this board, and on a "
                    "calling-station table the hands that continue are exactly the ones "
                    "that beat it. Do NOT raise and do NOT bet big. Check, or bet small "
                    "(1/3–1/2 pot) for a cheap price, and FOLD to a raise.")
        size = "1/2–2/3 pot" if sit.texture == DRY else "2/3–full pot"
        pick = _pick_size(sit, 0.5 if sit.texture == DRY else 0.7)
        hint = f" Use option {pick}." if pick else ""
        return (f"VALUE DEFAULT: strong made hand with no bet in front — BET for value "
                f"({size}).{hint} Checking is only correct for a real slow-play: absolute "
                f"nuts on a dry board vs an aggressive opponent, or a bet that only "
                f"better hands call.")

    if tier in (OVERPAIR, TPTK):
        if sit.explosive_pot:
            if facing_raise:
                return (f"EXPLOSIVE POT: {_explosive_reason(sit)}, so everyone is in "
                        "and a raise is rarely a bluff or a small sizing. Top "
                        "pair/overpair is only a MEDIUM hand here — FOLD. Do not stack "
                        "off with one pair.")
            if facing:
                return (f"EXPLOSIVE POT: {_explosive_reason(sit)}, so the field is "
                        "wide and full of random two pair/trips. Top pair/overpair is "
                        "only a MEDIUM hand — do NOT raise. Call one small bet only "
                        "(up to about 1/2 pot); fold to a larger bet or to any "
                        "turn/river barrel.")
            return (f"EXPLOSIVE POT: {_explosive_reason(sit)}, so everyone is in and "
                    "random two pair/trips are common. Top pair/overpair is only a "
                    "MEDIUM hand — bet small for value/protection (1/3–1/2 pot) and "
                    "pot-control; do not build a huge pot. Fold to a raise or to "
                    "multi-street aggression.")
        if facing_raise:
            if _heads_up_flop_overpair(sit):
                pot = winnable_pot(sit.snap)
                owed = call_amount(sit.snap)
                bep = owed / pot * 100 if pot > 0 else 0.0
                return (f"ONE PAIR vs AGGRESSION — CALL: heads-up on the flop, an "
                        f"overpair is not the marginal one pair the table note refers "
                        f"to. The raise range still contains top pair and draws, and "
                        f"you need only {bep:.0f}% equity to call. Do NOT fold to a "
                        "single raise — call and re-evaluate the turn. Re-raise only "
                        "if you can name a draw-heavy range.")
            return ("ONE PAIR vs AGGRESSION: top pair, or an overpair in a multiway "
                    "pot / on the turn or river, facing a raise or all-in — FOLD "
                    "unless you have a set, two pair, or a strong draw. One pair is "
                    "not a stack-off hand; do not pay off a raise.")
        if facing:
            if _passive_value_line(sit):
                return ("TABLE DYNAMICS OVERRIDE: the player's table notes say this "
                        "opponent's raises and multi-street barrels are value-heavy. "
                        "One pair is a bluff-catcher, not a call-down hand. FOLD to the "
                        "turn/river barrel; do not pay off a value-heavy range.")
            return ("VALUE/DEFEND DEFAULT: overpair or top pair with a strong kicker "
                    "facing a single bet — calling or raising is usually correct. Fold "
                    "only if the board is four-to-a-straight/flush or several opponents "
                    "have shown strength.")
        pick = _pick_size(sit, 0.6)
        hint = f" Use option {pick}." if pick else ""
        return ("VALUE DEFAULT: overpair/top pair with a strong kicker and no bet in "
                "front — BET 1/2–2/3 pot for value and protection." + hint + " Checking "
                "gives free cards and misses value.")

    if tier == STRONG_DRAW:
        if _no_fold_equity(sit):
            return ("NO FOLD EQUITY: 3+ opponents are in the pot, so betting a draw "
                    "wins nothing — nobody folds a draw or top pair here, and the "
                    "draw has no made hand to charge them with. CHECK. Call only if "
                    "the price is right; never bet or raise (those options are "
                    "withheld in this spot).")
        if facing:
            return ("DRAW ODDS: compare the estimated draw equity in the facts with "
                    "the break-even equity to call. With 8+ outs, folding is usually "
                    "wrong unless the price is bad.")
        return ("SEMI-BLUFF DEFAULT: 8+ out draw with no bet in front — betting is "
                "preferred (it can win by fold equity or by hitting).")

    if tier == TP_WEAK and facing:
        if sit.explosive_pot:
            if facing_raise:
                return ("EXPLOSIVE POT: top pair with a weak kicker is only a "
                        "bluff-catcher in this big multiway pot — FOLD to the raise. Do "
                        "not pay off.")
            return ("EXPLOSIVE POT: top pair with a weak kicker is only a medium/"
                    "bluff-catcher hand here. Call only a very small bet (up to about "
                    "1/3 pot); fold to a larger bet or any turn/river barrel.")
        if facing_raise:
            return ("ONE PAIR vs AGGRESSION: top pair with a weak kicker facing a raise "
                    "or all-in — FOLD. The raise says your top pair is beaten; do not "
                    "stack off.")
        if _passive_value_line(sit):
            return ("TABLE DYNAMICS OVERRIDE: the player's table notes say this "
                    "opponent's raises and multi-street barrels are value-heavy. Top "
                    "pair with a weak kicker is a bluff-catcher; FOLD to the turn/river "
                    "barrel.")
        if (sit.snap.to_call or 0) <= 0.6 * max(sit.snap.pot or 0.0, 1e-9):
            return ("SHOWDOWN DEFAULT: top pair facing a small single bet — calling is "
                    "usually correct. Fold to a large bet or multiway aggression.")
        return ("SHOWDOWN: top pair facing a large single bet — folding is acceptable; "
                "call only if the opponent's line is bluff-heavy.")
    if tier == WEAK_DRAW and _no_fold_equity(sit):
        return ("NO FOLD EQUITY: 3+ opponents are in the pot and this is only a weak "
                "draw — check/fold. Call only a small bet with the right price; never "
                "bet or raise (those options are withheld in this spot).")
    return None


# ─────────────────────────── 提示词 ───────────────────────────

# 事实块的最高优先规则。两套提示词都注入同一段：牌力事实只能引用，不能重算。
_GROUND_TRUTH_RULES = """\
## GROUND TRUTH (highest priority)
The facts include a «GROUND TRUTH» block from a deterministic evaluator: exact hand,
best five, nut status, every hand class that beats you, board flush/straight possibility,
draws. It is the only source for these; never contradict or recompute it.
- "No made flush possible (at most 2 of any suit)" → no flush exists yet; do not call
  the board a flush board.
- "NUTS" (made hand) → no opponent holding beats you; facing a bet, RAISE. Never
  invent a higher hand.
- "Beaten by" is exact: only those classes beat you.
- A non-nut straight or flush is still strong, but call — do not raise as if unbeatable.
- The rank's "equity" is vs ONE opponent. For multiway decisions use the
  "Multiway: with N opponents, X% chance at least one is ahead" line, not that equity.
- If a «Math gate» line is present, it is the binding price verdict: the program
  already compared draw equity (+2% cap) with the break-even. Do not re-judge the
  same two numbers, and do not call a positive-gate fold "thin".
- If this is a DRAW (Strong draw / Weak draw), the made-hand rank/NUT/"Beaten by" lines
  describe the CURRENT unmade hand only — they must NOT veto the draw directive.
  Heads-up with no bet in front, a semi-bluff is the baseline; against a station that
  never folds you may check, but the reason must be NO FOLD EQUITY — never "I am not
  the nuts". With 3+ opponents, check. Judge a draw by its outs and pot odds.
"""

_STREET_EN = {"preflop": "Preflop", "flop": "Flop", "turn": "Turn", "river": "River"}

# coach 的中文分档/质地/牌力 → 英文，只用于给模型的提示词渲染。
_TIER_EN = {
    "强成牌": "Strong made hand",
    "超对": "Overpair",
    "顶对强踢": "Top pair, strong kicker",
    "顶对弱踢": "Top pair, weak kicker",
    "中底对": "Middle/bottom pair",
    "强听": "Strong draw",
    "弱听": "Weak draw",
    "空气": "Air",
    "打牌面": "Board play",
}
_TEXTURE_EN = {
    "干燥": "Dry", "花面": "Flush-y", "顺面": "Straight-y",
    "花顺面": "Flush + straight", "公对面": "Paired",
}


def _action_en(a: ActionRecord) -> str:
    if a.action == "check":
        return "checks"
    if a.action == "fold":
        return "folds"
    if a.action == "call":
        return "calls" + (f" {a.amount}BB" if a.amount else "")
    if a.action == "bet":
        return "bets" + (f" {a.amount}BB" if a.amount else "")
    if a.action == "raise":
        return "raises to" + (f" {a.amount}BB" if a.amount else " ?")
    return a.action


def _fmt_actions(actions: list[ActionRecord], hero_name: str = "Hero") -> str:
    if not actions:
        return "(none)"
    parts = []
    for a in actions:
        who = hero_name if a.is_hero else (a.position or a.name or a.seat_id)
        # inferred = 视频流漏抓、按街末底池对账补出来的动作。模型要能看到这个
        # 标记，才知道这条行动是程序补的、金额是「本街加到总额」而不是差值
        tags = [name for name, on in (("all-in", a.all_in),
                                      ("inferred", a.inferred)) if on]
        extra = f" ({', '.join(tags)})" if tags else ""
        parts.append(f"{who} {_action_en(a)}{extra}")
    return " → ".join(parts)


def _position_en(pos: str | None) -> str:
    return pos or "unknown"


def _is_set_mine_pair(cards: tuple[str, ...] | list[str]) -> bool:
    """22-99：只按「买 set」看的口袋对；TT+ 才算超对价值。

    3-4 人池里高张统治的概率太高（99 翻牌 ~79% 见高张，77 ~92%），
    没中 set 的超对/中对不能按价值牌打光。见 `preflop_ranges.is_set_mine_pair`。
    """
    if len(cards) != 2 or not all(len(c) >= 2 for c in cards):
        return False
    ra, rb = cards[0][0].upper(), cards[1][0].upper()
    if ra == "1":                       # '10h' 这种写法
        ra = "T"
    if rb == "1":
        rb = "T"
    return ra == rb and ra in "23456789"


def render_opponent_reads(sit: Situation) -> list[str]:
    """把本手在局对手的画像写进事实块。unknown 不输出，tentative 标注。"""
    active = {o.seat_id: o for o in sit.snap.opponents if not o.folded}
    rows: list[tuple[str, OpponentRead]] = []
    for read in sit.opponent_reads:
        if read.archetype == "unknown" or read.seat_id not in active:
            continue
        position = active[read.seat_id].position or read.seat_id
        rows.append((position, read))
    if not rows:
        return []
    rows.sort(key=lambda item: item[0])
    lines = ["Opponent reads (per-seat, from local hand history — use these before "
             "any table-wide label):"]
    for position, read in rows:
        name = read.display_name or read.key
        gate = " [tentative]" if read.gate == "tentative" else ""
        source = ""
        if read.source == "pin":
            source = ", manual pin"
        elif read.source == "prior":
            source = ", .table_dy prior"
        af = f", AF {read.af:g}" if read.af else ""
        lines.append(
            f"- {position} {name}: {read.archetype.upper()} "
            f"(n={read.n}, VPIP {read.vpip:g}%, PFR {read.pfr:g}%{af}, "
            f"conf {read.confidence:.2f}{source}){gate}")
    return lines


def render_hand_facts(sit: Situation) -> str:
    """把当前局面写成紧凑的手牌历史式文本 —— 模型对这套写法先验最强。"""
    snap = sit.snap
    blinds = snap.blinds
    lines: list[str] = []

    table = f"{snap.table_size}-handed" if snap.table_size else "table size unknown"
    lines.append(f"Table: {table}")
    if blinds:
        ante = f" (ante {blinds.ante})" if blinds.ante else ""
        lines.append(f"Blinds: {blinds.sb}/{blinds.bb}{ante}")
    else:
        lines.append("Blinds: unknown")
    opps = [o for o in snap.opponents if not o.folded]
    folded = [o for o in snap.opponents if o.folded]
    stacks = [o.stack for o in opps if o.stack is not None]
    effective = min([*stacks, snap.hero_stack]) if snap.hero_stack is not None \
        and stacks else snap.hero_stack

    lines.append(f"Hero position: {_position_en(snap.hero_position)}｜"
                 f"Hole cards: {''.join(snap.hero_cards) or 'unknown'}｜"
                 f"Stack: {snap.hero_stack if snap.hero_stack is not None else 'unknown'}BB")
    if effective is not None:
        hero_stack = (f"{snap.hero_stack}BB" if snap.hero_stack is not None
                      else "unknown")
        lines.append(f"Effective stack: {round(effective, 2)}BB "
                     f"(my stack behind: {hero_stack})")
    if opps:
        lines.append("Opponents in hand: " + ", ".join(
            f"{o.position or o.seat_id}"
            f"{'(' + o.name + ')' if o.name else ''} "
            f"{o.stack if o.stack is not None else '?'}BB"
            for o in opps))
    if folded:
        lines.append("Folded: " + ", ".join(
            f"{o.position or o.seat_id}" for o in folded))
    # 短码会把 min() 算出的 Effective stack 拉到 2.5BB 这种值。牌桌上真正的
    # 问题是「跟他只能赢/输这么多、其余是 side pot」，必须单独说一句，
    # 否则模型会拿全池算赔率（990051 的教训）。
    short = [o for o in opps if o.stack is not None
             and snap.hero_stack is not None and o.stack < snap.hero_stack * 0.95]
    if short:
        lines.append("Short stacks: " + ", ".join(
            f"{o.position or o.seat_id} {o.stack:g}BB" for o in short) +
            " — side pots are not modeled; only that many BB are at risk "
            "against them.")

    lines.append("")
    to_call = round(snap.to_call, 2)
    # 后手不足以下注额时，实际只需跟到全下：客户端也只会给 Fold / All in。
    # 赔率和跟注比例必须按**实际能跟出的金额**算，否则会把门槛算高。
    owed = call_amount(snap)
    winnable = winnable_pot(snap)
    excess = uncalled_excess(snap)
    lines.append(f"Street: {_STREET_EN.get(snap.street, snap.street)}｜"
                 f"Board: {''.join(snap.board) or 'none'}｜"
                 f"Pot: {snap.pot if snap.pot is not None else 'unknown'}BB｜"
                 f"To call: {owed}BB")
    spot = [sit.pot_type] if sit.pot_type else []
    if sit.in_position is not None:
        spot.append("Hero in position" if sit.in_position
                    else "Hero out of position")
    if sit.players_left_to_act is not None:
        n = sit.players_left_to_act
        spot.append(f"{n} player{'s' if n != 1 else ''} left to act")
    if sit.spr is not None:
        # 短码全下时 SPR 可能只有 0.03：写成 0 会看起来像「没有筹码」。
        spr_text = "<0.1" if 0 < sit.spr < 0.1 else f"{sit.spr:g}"
        spot.append(f"SPR {spr_text}")
    if spot:
        lines.append("Spot: " + "｜".join(spot))
    if owed < to_call:
        lines.append(f"The bet covers my stack: calling {owed}BB puts me "
                     f"all-in; raising is not possible.")
    if excess > 0 and winnable > 0:
        # 牌桌底池包含对手无人能跟的筹码，直接拿它算赔率会低估门槛。
        matched = round((snap.hero_bet or 0.0) + owed, 2)
        lines.append(
            f"Uncalled bet: {excess:g}BB of the table pot is above my "
            f"{matched:g}BB matched total and is returned — the winnable pot "
            f"after my call is {winnable:g}BB. Use {winnable:g}BB for pot odds, "
            f"not the table pot.")
    if owed > 0 and winnable > 0:
        # 赔率门槛：跟注额 ÷ 跟注后可争夺的底池。技能里的赔率表就是按这个口径
        bep = owed / winnable * 100
        before_call = max(winnable - owed, 1e-9)
        lines.append(f"Break-even equity to call: {bep:.0f}%; "
                     f"call is {owed / before_call:.0%} of the pot before my call")

    lines.append("")
    lines.append("## Action line")
    if any(a.inferred for rec in snap.streets for a in rec.actions):
        lines.append('Actions tagged "inferred" were reconstructed from the pot '
                     'ledger after the video missed that frame; treat them as real '
                     'actions (amounts are "total invested on the street"). If an '
                     'inferred raise is present, its size is exact, but the raiser '
                     'is the earliest actor consistent with the pot and action order.')
    for rec in snap.streets:
        pot_start = f" (pot before street {rec.pot_start}BB)" \
            if rec.pot_start is not None else ""
        lines.append(f"{_STREET_EN.get(rec.street, rec.street)}{pot_start}: "
                     f"{_fmt_actions(list(rec.actions))}")

    lines.append("")
    lines.append("## Computed facts (trust these, don't recompute)")
    if sit.street == PREFLOP:
        shape = {"unopened": "no one has entered the pot",
                 "limped": "limpers are in, no raise",
                 "single-raise": "one raise already",
                 "three-bet": "a re-raise already"}
        shapes = ", ".join(shape.get(s, s) for s in snap.preflop_shapes) or "unknown"
        lines.append(f"Preflop shape: {shapes}｜Hero is preflop raiser: "
                     f"{'yes' if snap.hero_is_pfr else 'no'}")
        # 范围归属由程序算，不靠模型回忆那张表。实测模型会编出
        # 「AJo is in UTG+1's RFI range」这种和表直接矛盾的理由。
        if sit.range_verdict:
            lines.append(sit.range_verdict)
        limpers = count_limpers(snap)
        if limpers:
            # 行动线可能漏掉平跟（没有动作徽章）；用同帧下注额补出来，
            # 让模型知道前面到底有几个人溜入，而不是只看底池猜。
            lines.append(f"Limpers before me: {limpers}")
        # 本桌规则：翻前任何加注不得低于 6BB。选项生成已经按这个下限抬过；
        # 这里再写一句，防止模型在 reason 里建议「加到 2.5-3BB」这种点不出来的尺度。
        if any(o.kind in ("raise", "bet") for o in sit.options):
            lines.append(
                f"Preflop minimum raise: {PREFLOP_MIN_RAISE_BB:g}BB (table rule); "
                "any raise option below it has already been raised to that minimum.")
        # 抽 set 的隐含赔率：翻前不渲染成牌胜率，但小对子需要这组数字才能
        # 判断「跟 1BB 看翻牌」划不划算。阈值（15-20×）来自 preflop-limp.md。
        # 只对 22-99 有意义：TT+ 是超对价值，根本不按抽 set 打，给这组数字
        # 只会让模型把 JJ/TT 也当成「够赔率就跟」。
        if (to_call > 0 and effective is not None
                and _is_set_mine_pair(snap.hero_cards)):
            if "three-bet" in (snap.preflop_shapes or ()):
                # 面对 3-bet 时不能再拿 10:1/15:1 当跟注理由：3-bet 范围窄到
                # 大对子 + AK，中 set 要么撞更大 set，要么他直接弃、拿不到支付。
                lines.append(
                    "Implied odds (pocket pair): effective stack / call = "
                    f"{effective / to_call:.0f}:1, but the 15:1 rule does NOT apply "
                    "vs a 3-bet: his range is QQ+/AK, so a flopped set is either "
                    "set-under-set or gets no pay-off when he whiffs. Do not set-mine "
                    "a 3-bet with this pair.")
            else:
                lines.append(
                    f"Implied odds (pocket pair): effective stack / call = "
                    f"{effective / to_call:.0f}:1; flopping a set is 11.8% (1 in 8.5), "
                    f"so 15:1 or better makes set-mining profitable (10:1 with antes).")
        lines.extend(render_opponent_reads(sit))
    else:
        tier_en = _TIER_EN.get(sit.hand_tier, sit.hand_tier or "unknown")
        lines.append(f"My hand strength: {sit.hand_label_en or 'unreadable'}"
                     f" (tier: {tier_en})")
        if sit.ground_truth is not None:
            # 成手牌、最好五张、精确排名、坚果校验、听牌、牌面结构。
            # 这一段是模型唯一允许引用的牌力事实；不要再在别处重复。
            # 纯听牌走 draw_focus：不渲染 NUT STATUS/Beaten by，避免模型
            # 用「当前未成牌不是坚果」否决 SEMI-BLUFF DEFAULT。
            is_draw = sit.hand_tier in (STRONG_DRAW, WEAK_DRAW)
            lines.append(sit.ground_truth.render(draw_focus=is_draw))
            if is_draw:
                # 听牌时排名只反映「还没成牌」（坚果同花听牌此刻可能只是 A 高）。
                lines.append(
                    "Note: the rank above is for the CURRENT unmade hand. This does "
                    "NOT apply to a draw — judge a draw by its outs and the pot odds, "
                    "not by this number.")
        else:
            lines.append(f"Board texture: {sit.texture_en or 'unknown'}｜"
                         f"Draw: {'yes' if sit.has_draw else 'no'}｜"
                         f"Plays the board (best five all from board): "
                         f"{'yes' if sit.plays_board else 'no'}")
            # 「两对」「顺子」只是名字，不是强度。把实际排名算出来给模型看 ——
            # 8822 在 88T 上 87.7% 已被领先，和「花面的顺子」19.9% 差一个量级，
            # 靠 capped_by_board 这个布尔值分不开。见 coach.beat_rate。
            if sit.beat_pct is not None and sit.beat_at_least_pct is not None:
                n = sit.opponents_in_hand or 0
                if sit.hand_tier in (STRONG_DRAW, WEAK_DRAW):
                    # 听牌时这个数只反映「还没成牌」（坚果同花听牌此刻只是 A 高，
                    # 会被算成 40%+ 已落后）。不提示清楚会把它推去弃牌。
                    lines.append(
                        f"Made-hand rank vs RANDOM hands (lower bound) of the CURRENT "
                        f"(unmade) hand: {sit.beat_at_least_pct:g}% of the time "
                        f"someone already has you beaten right now. This does NOT "
                        f"apply to a draw — judge a draw by its outs and the pot odds, "
                        f"not by this number.")
                else:
                    lines.append(
                        f"Made-hand rank vs RANDOM hands (lower bound; exact count "
                        f"over all {n + 1}-way holdings): beaten by {sit.beat_pct:g}% "
                        f"of random hands per opponent; with {n} opponent(s) in the "
                        f"hand, {sit.beat_at_least_pct:g}% chance at least one is "
                        f"ahead. Random hands are a LOWER bound — anyone who called "
                        f"or raised has a stronger range than random.")
        if "three-bet" in (snap.preflop_shapes or ()):
            lines.append(
                "3-bet pot: the opponent's range is far narrower than random "
                "(premium pairs, big aces, bluffs). The rank above is an UPPER "
                "bound on your equity, NOT your equity against his actual range "
                "— discount it heavily when he bets again.")
        if sit.street in ("turn", "river") and owed > 0:
            lines.append(
                "Facing another barrel: his range has narrowed to value + draws; "
                "the random-hand rank overstates your equity in this spot.")
        if snap.bomb_pot:
            lines.append("Bomb pot banner: YES (幸运暴击 / BOMB POT shown on screen)")
        prev_pot = previous_street_pot(snap)
        if sit.pot_per_player is not None and prev_pot is not None:
            dealer_count = snap.players_dealt or snap.table_size
            why = ("bomb-pot banner on screen" if snap.bomb_pot else
                   f"threshold exactly {EXPLOSIVE_POT_BB_PER_PLAYER:g}BB/player")
            lines.append(
                f"Pot per player entering this street: "
                f"{sit.pot_per_player:.2f}BB ({prev_pot:g}BB ÷ {dealer_count})｜"
                f"Explosive pot: {'YES' if sit.explosive_pot else 'no'} ({why})")
        elif snap.bomb_pot:
            lines.append("Explosive pot: YES (bomb-pot banner on screen)")
        aggressive = _aggressive_context(sit)
        if sit.opponent_strength_en:
            o = sit.opponent_strength_en
            suffix = (" — on this LAG/maniac table, aggression is not automatically "
                      "value." if aggressive else "")
            lines.append(f"Opponent strength from actions: {o['label']} ({o['reason']})"
                         f"{suffix}")
        lines.extend(render_opponent_reads(sit))
        if aggressive:
            lines.append(
                "Table read: value-light aggression (LAG/maniac present) — their bets "
                "and raises are NOT automatically value. Do not thin-value a medium "
                "made hand; check-call and let them barrel.")
        if sit.equity_pct and sit.hand_tier in (STRONG_DRAW, WEAK_DRAW):
            # 四二法则只对**听牌**有意义。成牌（顺子/三条等）的 outs 数的是
            # 「还能不能改进」，不是胜率，直接乘会得到 92% 这种假精确值
            lines.append(f"Estimated draw equity (rule of 4/2): "
                         f"{sit.equity_pct:.0f}%")
            gate = math_gate(sit)
            if gate is not None:
                # 把「权益 vs 门槛」直接算成结论。h104 里模型自己写出
                # 「36% vs 29%」还是弃牌 —— 规则不够，得给它一句判定。
                lines.append(gate.text)
            if (sit.opponents_in_hand or 0) >= 2:
                lines.append(
                    "Multiway draw: the rule-of-4/2 number is the chance to "
                    "COMPLETE, not to win against the field — treat it as an "
                    "upper bound.")
        directive = value_directive(sit)
        if directive:
            lines.append(f"Value directive: {directive}")
        if sit.hand_ranges is not None:
            lines.append("")
            lines.append(sit.hand_ranges.render())
        if sit.board_eq is not None:
            lines.append("")
            lines.append(sit.board_eq.render())
        if sit.gto is not None:
            lines.append("")
            lines.append(sit.gto.render())
    if snap.hero_invested is not None and snap.hero_stack:
        ratio = snap.hero_invested / max(snap.hero_stack + snap.hero_invested, 1e-6)
        lines.append(f"I have invested {round(snap.hero_invested, 2)}BB"
                     f" (about {ratio:.0%} of current stack)")

    if sit.warnings:
        # 缺牌/缺筹码这类缺口要让模型知道，它才会在理由里说明而不是硬猜
        lines.append("")
        lines.append("Data gaps: " + "; ".join(sit.warnings))

    if sit.previous is not None and sit.previous.street != sit.street:
        # 实时链路每街都是独立调用：不回灌上一街自己的决定，模型会在转牌
        # 把翻牌的「抓诈计划」改成弃牌，前后自相矛盾。
        prev = sit.previous
        plan = f" Plan: {prev.plan}." if getattr(prev, "plan", "") else ""
        lines.append("")
        lines.append("## My previous decision (same hand)")
        lines.append(f"{_STREET_EN.get(prev.street, prev.street)}: I chose "
                     f"{prev.option.label} — \"{prev.reason[:200]}\".{plan}")
        lines.append("Keep that plan unless a computed fact contradicts it; do not "
                     "reverse a line you chose last street without a reason.")

    if sit.table_dynamics:
        # 人工维护的**桌级**倾向（不含玩家名字/单人风格）。放在选项之前：
        # 模型对「最后看到的几条事实」更敏感，而且它不是本手牌的计算结果。
        # 逐座位画像/人工 pin 永远优先于这段桌级描述。
        lines.append("")
        lines.append("## Table context (table-level; per-seat profiles outrank it)")
        lines.append(sit.table_dynamics)

    lines.append("")
    lines.append("## Options (pick exactly one id)")
    for o in sit.options:
        amount = f", amount {o.amount}BB" if o.amount is not None else ""
        note = f" ({o.note})" if o.note else ""
        lines.append(f"- {o.id} — {o.label}{amount}{note}")
    return "\n".join(lines)


PROMPT_STYLE_LEAN = "lean"
PROMPT_STYLE_SKILLS = "skills"
PROMPT_STYLES = (PROMPT_STYLE_LEAN, PROMPT_STYLE_SKILLS)
# 默认精简版：契约 + 计算事实 + 桌面动态 + 一份紧凑的现代范围框架。
# 旧版（15 篇 2005 年《Little Green Book》技能，system 约 20k 字符）保留给
# A/B 对比：`--advice-style skills`。lean 版不再展开技能库。
DEFAULT_PROMPT_STYLE = PROMPT_STYLE_LEAN


def _build_messages_skills(sit: Situation) -> tuple[str, str]:
    """返回 (system, user)。system 由「角色 + 输出契约 + 技能层」拼成。"""
    all_ = skills.all_skills()
    selected = skills.select_skills(_skill_context(sit), all_)

    system_parts = [
        "You are a Texas Hold'em cash-game coach assisting a player **in real time**. "
        "The player will review the hand later, but right now they have only a few seconds "
        "and need a directly executable action.",
        "",
        "## Output format",
        "Output a single JSON object, no markdown code fence, no explanatory text:",
        '{"option": "<option id>", "reason": "<one-sentence reason in English, under 30 words, '
        "must cite a rule from the skills or the table dynamics>\"}",
        "",
        "## Hard constraints",
        "0. «Table dynamics» (when present in the user facts) and «Explosive pot: YES» "
        "are the highest-priority exploitative instructions. When the table notes say "
        "the opponent's raises or multi-street barrels are value-heavy, or the pot is "
        "explosive, fold marginal made hands (one pair, two pair) even if a «Value "
        "directive» default says to call. Pot odds only tell you the price; they do "
        "not create bluffs in a value-heavy range.",
        "1. option must come strictly from the ids in the user message's «Options» section; "
        "no extras, no rewrites.",
        "2. All amounts are computed by the program. Do **not** compute the pot yourself, do not "
        "output any amount — output only the id.",
        "3. When facing a bet, actually compare pot odds: the equity needed to call comes from "
        "«Odds & Probability Quick Reference»; compare it against your hand strength / draw "
        f"equity — fold if short, call or raise if enough. {implied_odds_rule()}",
        "4. Preflop there is no made-hand equity to compare: use the sizing tables in the "
        "«Preflop» skills, by position, number of prior raises, and number of opponents.",
        "5. Default order of thought: with a strong made hand, first ask «bet or raise for "
        "value»; only then ask «check or call». Checking/folding must justify itself "
        "(«an unjustifiable check is also a leak»).",
        "6. If the user facts include a «Table dynamics» section, treat it as authoritative "
        "exploitative context for the current table. Otherwise, treat the opponent as "
        "«unknown» — the live pipeline has no history, so no exploitative deviation.",
        "7. Do not guess the opponent's exact hand, only describe range tendencies.",
        "8. Reason in English; keep card notation in AsKh format.",
        "",
        "## Value-first rules (read before choosing)",
        "A strong made hand is any hand whose computed tier is «Strong made hand» "
        "(two pair, set/trips, straight, flush, full house), «Overpair», or "
        "«Top pair, strong kicker».",
        "1. Strong made hand + no bet in front → BET for value by default. "
        "«Opponent checked» / «no opponent action yet» is a reason to bet, not a reason to "
        "check: charge draws and get paid by worse hands.",
        "2. Strong made hand + facing a bet → RAISE for value by default. Call only if the "
        "board is four-to-a-straight/flush, a paired board makes a full house likely, or "
        "the opponent's line clearly says you are beaten.",
        "3. Slow-play exceptions — checking a strong hand is correct only when one of these "
        "is true: (a) you hold the absolute nuts (nut straight/flush/full house) on a dry, "
        "uncoordinated board and expect the opponent to bluff; (b) a bet would fold out "
        "every worse hand and only get called by better; (c) the board is so coordinated "
        "that your made hand is likely beaten.",
        "4. Never slow-play two pair, a set, or top pair. Slow-play only the nut flush or a "
        "full house on a dry board against an aggressive opponent.",
        "5. Size for value: 1/2–2/3 pot on a dry board, 2/3–full pot on a wet/draw-heavy "
        "board. Do not default to the smallest size.",
        "6. Strong draw (8+ outs) with no bet in front → betting (semi-bluff) is preferred — "
        "**heads-up only**: with 3+ opponents in the pot, check instead, because a draw has "
        "no fold equity against a calling field. Facing a bet, use the pot-odds "
        "gate from rule 3: call only if it clears the break-even; otherwise fold.",
        "7. The user facts include an explicit «Value directive» when one of these rules "
        "applies. It is a default for strong made hands; the «Table dynamics» section "
        "overrides it when it describes the opponent's aggression as value-heavy. In "
        "particular, a Value directive that says «call a normal-sized bet» does NOT bind "
        "you to call a turn/river barrel with one pair or two pair when the table notes "
        "say that line is value-heavy.",
        "8. One pair is not a stack-off hand, but do not auto-fold it: heads-up on the "
        "flop, an overpair (TT+) facing a single raise is a CALL — the raise range still "
        "contains top pair and draws. Fold top pair / marginal one pair, and fold an "
        "overpair to multiway aggression, a turn/river barrel, or a raise that gives bad "
        "odds.",
        "9. Before raising a strong hand, check whether the board allows a better hand: "
        "three or more of one suit → a flush is possible; a paired board → a full house "
        "or quads are possible. A straight/two pair/set is NOT the nuts then. On such a "
        "board, call for showdown instead of raising — a raise only gets called by better.",
        "",
        "## Explosive pot (multiway / big preflop pot)",
        "«Explosive pot: YES» in the user facts has two triggers: (a) the client "
        "displayed the «幸运暴击 / BOMB POT» banner at the start of the hand — a "
        "forced-ante bomb pot where the whole table sees the flop; or (b) the pot "
        "entering this street is exactly 5BB per player (previous-street pot ÷ "
        "players dealt = 5BB — the fixed bomb ante; ordinary big pots do not divide "
        "out exactly). In both cases everyone is in the pot, so the field is "
        "wide and full of «weird» two "
        "pair / trips from hands that would normally fold. Loose-passive players do not "
        "put chips in this pot with nothing, and their bet/raise sizes here will not be "
        "small. Be especially careful:",
        "- One pair (overpair or top pair) is only a MEDIUM hand in an explosive pot. Do "
        "not raise it, do not stack off with it, and do not call down multiple streets. "
        "Call at most one small bet; fold to a raise, a large bet, or a turn/river "
        "barrel.",
        "- Do not assume top pair is good just because the board looks dry — in a "
        "multiway limped/raised pot, random two pair and trips are exactly what the "
        "field holds.",
        "- Value-bet strong made hands (two pair, set, straight, flush), but pot-control "
        "medium hands. Do not bloat the pot with one pair.",
        "- Explosive pot overrides the default «VALUE DEFAULT: bet/call with top pair» "
        "directives: top pair is a medium hand / bluff-catcher here, so do not raise or "
        "stack off with it even though the table notes say to value-bet thin. A small "
        "value bet when checked to is still fine.",
        "",
        "## Table dynamics (user-provided, when present)",
        "The user facts may include a «Table dynamics» section written by the player. "
        "Treat it as authoritative for the current table and adjust exploitative choices. "
        "**The table may be mixed:** apply the note per opponent/seat, match the opponent "
        "in this hand, and never average it into one «loose-passive» profile:",
        "- loose-passive / calling station / novice: bluff less, value-bet thinner "
        "(more marginal hands) and size up, and never slow-play strong hands. Their "
        "raises and multi-street barrels are value-heavy: fold marginal made hands "
        "(including two pair on a paired board) instead of paying them off, unless you "
        "have a set/straight/flush or a strong draw with the right price. A heads-up "
        "flop overpair is not marginal — call one raise;",
        "- tight / rarely bluffs / nit: fold marginal bluff-catchers, do not pay off, "
        "value-bet tighter (fewer marginal value hands), respect raises, and bluff more "
        "selectively;",
        "- loose-aggressive / maniac (LAG, loose-maniac/loose-aggressive): do NOT "
        "assume their bet/raise is value. Trap with strong hands, call down lighter, "
        "avoid thin value and bluffing wars; do not bet-fold a medium hand. Let them "
        "barrel into your bluff-catchers;",
        "- passive preflop + aggressive postflop: preflop calls are wide, but postflop "
        "bets are not automatically value — treat it as a bluff-catching spot;",
        "- tight-aggressive / solid: play straightforwardly, avoid fancy plays, and give "
        "credit to aggression.",
        "- in an explosive pot (bomb pot or exactly 5BB per player, everyone in), "
        "these tendencies "
        "amplify: a loose-passive player's bet or raise is value and will not be small, "
        "top pair is only medium, and bluffing a field this wide is burning money;",
        "Example: on 55 2 T 8, hero has JJ (two pair) and a loose-passive opponent bets "
        "flop, turn and river. Their line is value-heavy (5x trips, boats); fold the "
        "turn/river even though the small sizes give a low break-even equity.",
        "It overrides the default «unknown opponent» assumption and may justify deviating "
        "from a default call/fold/check line when the hand is marginal (bluff-catcher or "
        "medium strength). It never overrides computed hand strength or pot odds: with a "
        "strong made hand still bet/raise for value; with a strong draw still compare "
        "equity. When the dynamics change the line, say which tendency drove it.",
        "",
    ]
    system_parts.append(_GROUND_TRUTH_RULES)
    system_parts.append(skills.render_catalog(all_, selected))
    system_parts.append(skills.render_block(selected))

    return "\n".join(system_parts), render_hand_facts(sit)


# ── lean 提示词：契约 + 事实 + 桌面动态 + 现代范围框架 ──────────────
# 刻意不加载 skills_md_en：那套是 2005 年《Little Green Book》的绝对化打法，
# 没有范围表，system 约 20k 字符。这里只保留模型做「范围决策」真正需要的：
# 输出契约、怎么用事实/赔率、按位置的 RFI/3-bet/冷跟范围、翻后纹理与尺度、
# 爆炸底池一行、以及桌面动态的调频方式。
_LEAN_SYSTEM = """\
You are a Texas Hold'em cash-game advisor working in real time. Hero has a few seconds and needs one directly executable action.

## Priority (highest first)
1. Legal actions and computed math (hand rank, equity, pot odds, SPR, behind-now) — never violate.
2. A reliable per-seat read — «Opponent reads» or a manual pin — outranks every table-wide statement.
3. «Hand vs ranges» — use it for what he has: "equity vs field" decides, "behind now" is the made hand (a draw can be behind and still have equity); the random rank is only a lower bound.
4. Table context, «Board structure» and «GTO baseline» — context, not the answer.
5. The chart and generic heuristics — defaults; never override 1–4.

## GTO baseline (only when the facts contain that block)
It is the solved default for this exact board class: range mix, your hand's size mix, EV per size. It outranks the generic size rules below, but computed math, a reliable read, table dynamics, multiway/explosive-pot rules and the block's caveats override it. If you pick a non-best-EV size, name the exploitative reason. Never present an «[approx]» (interpolated) size as solved.

## Who you are playing
The pool is **mixed** — do not project one label onto the whole table. Classify each opponent from «Opponent reads» (per-seat stats or a manual pin); «tentative» is a hint. With no read, treat the opponent as unknown, not as a station. «Table context» is table-level only — it never names individual players.
- station: calls wide, raises little; their bet/raise is value → thin value, size up, fold marginal hands to barrels.
- LAG/maniac (loose-maniac/loose-aggressive): opens/3-bets large, barrels wide, overbets; their bet is NOT automatically value → do not thin-value OOP; check-call, let them barrel, do not bet-fold; bluff-catch more.
- LAG/maniac, but betting SMALL on multiple streets: a LAG's bluff tool is the big bet (only big sizing gets folds); repeated small sizing is a merged VALUE line (top pair+, protection, price-setting). His range has no air → your bluff-raise has negative fold equity: do not raise it, do not pay off three streets with one pair, fold marginal hands on the turn instead of bluff-catching the river. Save the bluff-catch for his big bet / overbet.
- passive preflop + aggressive postflop: postflop bets are not automatically value → bluff-catching spot.

## The question that decides every bet
Ask two questions, in order:
1. **Which worse hands actually CALL me?** (value)
2. **Which better hands RAISE me, and who is left to act?** (risk)
- Two or more worse callers and no better raiser → bet 2/3 to full pot (the «Value directive» may name another size; it wins).
- "Only better hands call, worse hands fold" → check.
- Against a station, Q1 dominates. Against a maniac/LAG, Q2 dominates: a "value" bet that folds worse and gets raised by better is a check.
- Raising a non-nut hand narrows their range to its top: value-raise a station, but do not bluff-raise a maniac.

## Output
Return exactly one JSON object, no markdown fence and no other text:
{"option": "<id from Options>", "reason": "<one sentence, under 25 words>", "plan": "<optional next-street intent, under 10 words>", "confidence": <optional 0.0-1.0>}
- option must be exactly one id from the user's «Options» section. Never invent ids or amounts; all amounts are computed by the program.
- reason must cite the range, computed fact, or table tendency that decided it.
- plan (optional) is what you will do next street if the action goes as expected.
- confidence (optional) is your certainty in the option; it is logged, never used to click.

## Ground rules
1. Computed facts are authoritative for MATH: positions, stack, pot, to_call, break-even equity, SPR, hand tier, draw, texture, explosive pot. Do not recompute amounts.
2. Preflop: choose by position and range. Do not limp as the first player in, except an SB complete or a small pocket pair (22-66) over-limping for 1BB to set-mine. This table enforces a <<PREFLOP_MIN_RAISE>> minimum preflop raise (all-in excepted); every raise option is already at or above it — never argue for a smaller size.
3. Postflop: judge the calling range and board texture first; then value/check/size.
4. Call only when your equity beats the stated break-even. <<IMPLIED_ODDS_RULE>> Unquantified "implied odds" is never a reason to call.
5. If calling covers Hero's stack, the decision is all-in or fold.
6. A «Value directive» in the facts is a coarse default; when a «Math gate» line is present, it is the binding price verdict. Overrule either only with a concrete read from the facts.
7. Keep your own previous plan (facts: «My previous decision») unless a computed fact contradicts it. Do not reverse a line you chose last street without a reason.

## Preflop ranges (9-handed, ~100BB, ante) — DEFAULTS for an unknown opponent
RFI by position (cumulative: each row adds to the previous; raise to the offered size, fold the rest). «Table dynamics» outranks this table: loose → widen by roughly one seat and let small pairs over-limp for 1BB; tight → do not widen.
<<RFI_CHART>>
SB: open ~30%, raise-or-fold vs opens (not vs limps). In a limped pot, completing for 0.5BB is cheap, but **raw pot odds never justify it — you need realized equity**. Complete only hands that can flop something you can bet: suited, connected with gap 2 or less, a pocket pair, or an ace with a real kicker. FOLD offsuit trash (92o, T6o, Q6o).
The facts may carry a «Preflop range» line computed from this chart. It is the chart DEFAULT: when it says OUT, do not open-raise — but table dynamics can widen or narrow what is playable. One exception — a small pocket pair (22-66) may over-limp for 1BB to set-mine even when the chart says OUT; a non-pair hand never qualifies. This chart is RFI (unopened pot): over limpers the isolation range is tighter, never treat "IN the open range" as an isolation license. Speculative hands over limpers (A2s-A9s, suited connectors/gappers): raise for initiative and dead money, not value isolation.
3-bet: value QQ+/AK; bluffs A5s-A2s, KQs, KJs vs late opens; 3x IP, 4x OOP.
Cold-call IP (vs a single open): 22-99, 54s-JTs, KTs, QJs; no offsuit broadways vs early opens.
BB vs late open: defend wide; 3-bet linear 55+, A9s+, KTs+, ATo+, KQo.
**Facing a 3-bet: continue ONLY with QQ+/AK** — 4-bet for value, or flat in position to keep his bluffs in. Fold the rest: small pairs never set-mine (his range is QQ+/AK, so a flopped set is set-under-set or paid off by nothing); suited connectors/offsuit broadways never realize equity.
At 6-8 handed, widen each early-position range by roughly one seat. Below ~25BB the chart is void: jam or fold, do not open-raise small.

## Postflop (defaults; table dynamics and opponent type override)
**Opponent type changes the line.** The absolutes below are for a station. Against a maniac/LAG: do not thin-value OOP; check-call medium hands, let them barrel, do not bet-fold; bluff-catch more and give less credit to a single BIG bet — but a repeated small bet from him is his merged value line, so fold one pair to it rather than calling three streets.
- C-bet dry A/K-high or paired boards at 1/3 pot only with equity; air is a donation.
- Size by what worse hands call, and prefer the size named in the «Value directive»; 1/3 is never a value size vs a station, and against a LAG a small blocker bet is worse than a check.
- Bluff only with a real draw. Heads-up, a strong draw with no bet in front is a BET. 3+ opponents: never bet/raise a draw — no fold equity; check and call only at the right price. (The program withholds those options multiway.)
- One pair: one bet, then pot-control. At SPR ≤ 2 with top pair top kicker or better, stack off; at SPR ≥ 6 one pair is not a stack-off hand.
- 22-99 unimproved are MEDIUM set-mines — bet small/check, never stack off, especially 3+ way.
- Facing a raise or a multi-street barrel from a station: FOLD marginal one pair and non-nut made hands; a heads-up flop overpair calls one raise.
- MDF assumes a bluffer. vs a station, fold more than MDF; vs a LAG, do not over-fold to a single barrel — call more, fold to the raise/overbet or multi-street barrel.
- Multiway (3+): bluffing is off the table; raise less, call more; top pair is medium.

## Turn
- One card to come: draw equity = outs × 2, not × 4. Do not pay a turn bet with a weak draw (4-7 outs); a strong draw needs the price.
- Second barrel only if the turn improves your range or you had a real draw; give up with air.
- Medium made hands: check behind or call one bet, do not build the pot.
- A turn raise from a passive player is value: fold one pair and two pair.

## River
- No more cards: a draw is air. Check/fold it; never call "for the draw".
- Value bet only if a worse hand calls; one pair checks behind for showdown.
- A 2/3+ river bet from a passive player is under-bluffed: fold one pair and two pair without a read; call with a set, straight or flush.
- Do not bluff a station or a multiway field on the river: no fold equity.
- Bluff-catch only with a good price AND a credible line (a missed draw that would have bet earlier).

## 3-bet / 4-bet pots
- SPR is low (often < 3): top pair top kicker and overpairs are stack-off hands; do not fold them to a single raise.
- 4-bet value QQ+/AK, bluff A5s-A2s. Do not cold-call a 3-bet with offsuit broadways.
- C-bet smaller and more often; ranges are narrow, so "who calls" is sharper.

## Explosive pot
«Explosive pot: YES» (bomb banner, or 5BB per player) means wide ranges and many two pair/trips. Top pair/overpair is a MEDIUM hand — pot-control it.

## Table context (user-provided, table-level, when present)
Table-wide context outranks the chart and generic heuristics; those never override table dynamics or computed math. A reliable per-seat profile or a manual pin outranks it. **The table may be mixed:** apply the note to the table's overall tendency; never average individuals into "loose-passive". It overrides the default «unknown opponent» assumption, never computed hand strength or pot odds.
- station: value-bet thin/big; respect raises.
- LAG/maniac: never thin-value or bet-fold a medium hand — check-call and let them barrel. Exception: repeated small bets from them are a merged value line, so fold one pair instead of calling it down, and never bluff-raise it.
- passive preflop + aggressive postflop: postflop bets are bluff-catches, not value.
- nit/tight: steal more, fold marginal. TAG: straightforward.
- If raises are rare (loose-passive), a small pair may over-limp for 1BB even OOP; "do not over-limp too much" is a frequency cap, not a ban at 1BB.
"""

# 范围表由 preflop_ranges 生成，不再手写两份：手写一定漂移，而模型拿到
# 和代码判定不一致的表时，会挑对自己有利的那份用。
_LEAN_SYSTEM = _LEAN_SYSTEM.replace("<<RFI_CHART>>", chart_text())
_LEAN_SYSTEM = _LEAN_SYSTEM.replace(
    "<<PREFLOP_MIN_RAISE>>", f"{PREFLOP_MIN_RAISE_BB:g}BB")
_LEAN_SYSTEM = _LEAN_SYSTEM.replace("<<IMPLIED_ODDS_RULE>>",
                                    implied_odds_rule())


def _build_messages_lean(sit: Situation) -> tuple[str, str]:
    return _LEAN_SYSTEM + "\n" + _GROUND_TRUTH_RULES, render_hand_facts(sit)


def build_messages(sit: Situation,
                   style: str = DEFAULT_PROMPT_STYLE) -> tuple[str, str]:
    """按风格拼 (system, user)。

    lean（默认）= 契约 + 事实 + 现代范围框架；skills = 旧的技能库全量注入。
    未知风格直接报错，避免拼写错误静默退回某个版本。
    """
    if style == PROMPT_STYLE_SKILLS:
        return _build_messages_skills(sit)
    if style == PROMPT_STYLE_LEAN:
        return _build_messages_lean(sit)
    raise ValueError(f"未知提示词风格：{style!r}（可选：{', '.join(PROMPT_STYLES)}）")


def _skill_context(sit: Situation) -> skills.SkillContext:
    snap = sit.snap
    known = sum(1 for o in snap.opponents if o.name or o.position)
    return skills.SkillContext(
        street=snap.street,
        board="".join(snap.board),
        hero_cards="".join(snap.hero_cards),
        hero_position=snap.hero_position,
        table_size=snap.table_size,
        opponents=snap.opponents_in_hand,
        preflop_shapes=snap.preflop_shapes,
        hero_is_pfr=snap.hero_is_pfr,
        cbet_spot=snap.cbet_spot,
        villains_known=known,
        category=sit.category,
        tier=sit.hand_tier,
        has_draw=sit.has_draw,
        plays_board=sit.plays_board,
    )


# ─────────────────────────── 模型调用 ───────────────────────────

def _env_text(env: dict[str, str], key: str) -> str | None:
    """可选配置：空值等同于没配，避免 `.env` 里的 `KEY=` 覆盖掉默认。"""
    value = (env.get(key) or "").strip()
    return value or None


def _env_number(env: dict[str, str], key: str, cast, low: float,
                high: float) -> float | int | None:
    raw = _env_text(env, key)
    if raw is None:
        return None
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{key} 不是合法数字：{raw!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{key} 应在 {low}–{high} 之间，实际 {raw!r}")
    return value


@dataclass(frozen=True)
class AISettings:
    api_key: str
    base_url: str
    model: str
    source: str = "env"              # env | dotenv，只用于启动日志
    # OpenRouter 的可选排名头（JEV 走 OpenRouter 时用）
    referer: str = ""
    title: str = ""
    # 采样/思考参数。None = 不传，用服务端默认。
    # 注意：deepseek-flash 默认开启思考（effort=high），思考模式下 temperature
    # 不生效，见 AI_SERVER_THINKING 的说明。
    temperature: float | None = 0.2
    top_p: float | None = None
    max_tokens: int | None = None
    thinking: str | None = None           # enabled | disabled（DeepSeek）
    reasoning_effort: str | None = None   # none | low | high | max
    # OpenRouter 推理开关：请求体里是 {"reasoning": {"enabled": bool}}。
    # 与 DeepSeek 的 thinking 是两套字段，各自按供应商填写。None = 不传。
    reasoning: bool | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None,
                 source: str = "env") -> "AISettings | None":
        """从环境变量读凭据与采样参数。缺凭据返回 None（= 没配置）。

        与 call-back 的 AI_SERVER_* 同口径：同一台机器上两边共用一套配置。
        `.env` 由 poker_ocr.env 先加载进环境变量，这里不关心值从哪来。

        采样参数非法时抛 ValueError（而不是静默退回默认）：
        「配了却没生效」是这类配置最难查的故障。
        """
        env = os.environ if env is None else env
        key = (env.get("AI_SERVER_API_KEY") or "").strip()
        base = (env.get("AI_SERVER_BASE_URL") or "").strip()
        model = (env.get("AI_SERVER_MODEL") or "").strip()
        if not (key and base and model):
            return None

        thinking = _env_text(env, "AI_SERVER_THINKING")
        if thinking is not None:
            thinking = thinking.lower()
            if thinking not in ("enabled", "disabled"):
                raise ValueError("AI_SERVER_THINKING 只能是 enabled 或 disabled")
        effort = _env_text(env, "AI_SERVER_REASONING_EFFORT")
        if effort is not None:
            effort = effort.lower()
            if effort not in ("none", "low", "high", "max"):
                raise ValueError(
                    "AI_SERVER_REASONING_EFFORT 只能是 none / low / high / max")

        reasoning_text = _env_text(env, "AI_SERVER_REASONING")
        if reasoning_text is not None:
            reasoning_text = reasoning_text.lower()
            if reasoning_text not in ("enabled", "disabled"):
                raise ValueError("AI_SERVER_REASONING 只能是 enabled 或 disabled")
        reasoning = (None if reasoning_text is None
                     else reasoning_text == "enabled")

        temperature = _env_number(env, "AI_SERVER_TEMPERATURE", float, 0.0, 2.0)
        # 推理模型（OpenRouter 的 GPT/o 系列）只接受默认 temperature，
        # 显式发 0.2 会被服务端拒绝。没配 temperature 时只在非推理模式
        # 保留原来的 0.2 默认；开了 reasoning 就不发。
        if temperature is None and not reasoning:
            temperature = 0.2

        return cls(api_key=key, base_url=base.rstrip("/"), model=model,
                   source=source,
                   referer=(env.get("AI_SERVER_REFERER") or "").strip(),
                   title=(env.get("AI_SERVER_TITLE") or "").strip(),
                   temperature=temperature,
                   top_p=_env_number(env, "AI_SERVER_TOP_P", float, 0.0, 1.0),
                   max_tokens=_env_number(env, "AI_SERVER_MAX_TOKENS", int,
                                          1, 393216),
                   thinking=thinking,
                   reasoning_effort=effort,
                   reasoning=reasoning)


class AIError(RuntimeError):
    """模型调用失败。消息面向用户，不含供应商原文（可能回显 Key 片段）。"""


def is_jev_model(model: str) -> bool:
    """模型名是不是 JEV 系列。

    覆盖两种写法：`jev-1.14`（模型名本身）和 `typesafe/jev-1.14`（OpenRouter
    的 vendor/model 形式，也是请求样例里的写法）。只看路径分段，所以
    `my-jev-model` 不会被误判。

    这类模型不走 `/chat/completions`：它是 OpenRouter 的 decisions 接口，
    输入是 state + 带类型的 questions，输出是 answers。
    """
    name = (model or "").strip().lower()
    if not name:
        return False
    if name.startswith("jev"):
        return True
    return any(part.startswith("jev") for part in name.split("/") if part)


# 一次建议的模型本身只要几百毫秒，但国内到 OpenRouter 的 TLS 握手实测
# 0.4–1.4s —— urllib 每次 urlopen 都新建连接，等于每给一次建议就多付一次
# 握手钱。这里按 host 缓存一条 HTTP/1.1 长连接。只在没配代理时用：
# http.client 不会读 HTTPS_PROXY，配了代理就退回 urllib（保持原有行为）。
_conn_lock = threading.Lock()
_conns: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "ALL_PROXY", "all_proxy")


def _proxy_configured() -> bool:
    return any(os.environ.get(k) for k in _PROXY_ENV)


def _post_keepalive(url: str, data: bytes, headers: dict,
                    timeout: float) -> bytes:
    parts = urllib.parse.urlsplit(url)
    https = parts.scheme == "https"
    key = (parts.scheme, parts.hostname or "",
           parts.port or (443 if https else 80))
    conn_cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    with _conn_lock:
        conn = _conns.get(key)
        if conn is None:
            conn = conn_cls(key[1], key[2], timeout=timeout)
            _conns[key] = conn
        elif conn.sock is not None:
            conn.sock.settimeout(timeout)
        try:
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        except BaseException:
            # 连接坏了不能留在缓存里，下一次还得付握手钱
            conn.close()
            _conns.pop(key, None)
            raise
        if resp.will_close:
            conn.close()
            _conns.pop(key, None)
    if resp.status >= 400:
        # 与 urllib 的口径一致：HTTP 错误带着 code 原样抛出，调用方决定
        # 401/403 脱敏、400 退回非 JSON 模式重试
        raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                     resp.headers, io.BytesIO(body))
    return body


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> bytes:
    """POST 一段 JSON 并返回原始响应体。

    `HTTPError` 原样抛出，由调用方决定怎么脱敏（401/403 不回显 body）；
    网络层错误统一转 AIError。
    """
    data = json.dumps(payload).encode("utf-8")
    if not _proxy_configured():
        try:
            return _post_keepalive(url, data, headers, timeout)
        except (http.client.HTTPException, ConnectionError):
            # 长连接被服务端按空闲超时关掉是最常见的情况：丢掉它，走 urllib
            # 新建连接重试一次。模型请求是幂等的选择题，重试不会产生副作用。
            pass
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIError(f"调用模型失败：{exc}") from exc


def _http_error(exc: urllib.error.HTTPError) -> AIError:
    # 不回显 body：供应商常在里面回显 Key 片段
    if exc.code == 401:
        return AIError("模型鉴权失败，请检查 AI_SERVER_API_KEY")
    if exc.code == 403:
        # 403 不一定是 Key 错：OpenRouter 对 OpenAI 模型常因账号/地区/
        # 供应商条款拒绝，Key 本身仍然有效。两者分开报才查得动。
        return AIError("模型服务拒绝访问（HTTP 403）：可能是账号、地区或供应商"
                       "条款限制，不一定是 Key 写错。OpenRouter 可在 Activity "
                       "页查看被拒原因")
    return AIError(f"模型服务返回 HTTP {exc.code}")


def chat_headers(settings: AISettings) -> dict:
    """OpenAI 兼容 /chat/completions 的请求头。

    `HTTP-Referer` / `X-OpenRouter-Title` 只在配置了 `AI_SERVER_REFERER` /
    `AI_SERVER_TITLE` 时发送（OpenRouter 用它做可选排名，其他供应商会忽略）。
    """
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + settings.api_key}
    if settings.referer:
        headers["HTTP-Referer"] = settings.referer
    if settings.title:
        headers["X-OpenRouter-Title"] = settings.title
    return headers


def chat_payload(settings: AISettings, messages: list[dict],
                 json_mode: bool = False) -> dict:
    """组装 /chat/completions 请求体。只发显式配置过的参数。

    `thinking` 是 DeepSeek 的字段；`reasoning` 是 OpenRouter 的字段
    （`{"enabled": true}`）。两者互不替代，按 `.env` 里实际配置的发。
    `reasoning_effort` 在 OpenRouter 下嵌进 reasoning.effort，其余供应商
    仍发顶层字段（DeepSeek 的口径）。
    """
    body = {"model": settings.model, "messages": messages, "stream": False}
    if settings.temperature is not None:
        body["temperature"] = settings.temperature
    if settings.top_p is not None:
        body["top_p"] = settings.top_p
    if settings.max_tokens is not None:
        body["max_tokens"] = settings.max_tokens
    if settings.thinking is not None:
        body["thinking"] = {"type": settings.thinking}
    if settings.reasoning is not None:
        reasoning: dict = {"enabled": settings.reasoning}
        if settings.reasoning_effort is not None:
            reasoning["effort"] = settings.reasoning_effort
        body["reasoning"] = reasoning
    elif settings.reasoning_effort is not None:
        body["reasoning_effort"] = settings.reasoning_effort
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def chat_message(settings: AISettings, messages: list[dict],
                 timeout: float = 20.0, request=None,
                 json_mode: bool = False) -> dict:
    """POST `{BASE_URL}/chat/completions`，返回 assistant message 原对象。

    返回的 message 可能带 `reasoning` / `reasoning_details`。**多轮续推**时把
    上一条 assistant message 原样放回 `messages` 即可（`reasoning_details`
    不要裁剪或改写，OpenRouter 需要它来续接推理）::

        first = chat_message(settings, messages)
        messages += [{"role": "assistant",
                      "content": first.get("content"),
                      "reasoning_details": first.get("reasoning_details")},
                     {"role": "user", "content": "Are you sure?"}]
        second = chat_message(settings, messages)

    `json_mode` 打开时先带 `response_format=json_object` 请求；兼容层不认它
    会回 400，此时自动退回不带该字段重试一次（保留原有行为）。
    """
    url = settings.base_url + "/chat/completions"
    post = request or _post_json
    headers = chat_headers(settings)
    try:
        try:
            raw = post(url, chat_payload(settings, messages, json_mode),
                       headers, timeout)
        except urllib.error.HTTPError as exc:
            # 有些兼容层不认 response_format（回 400）。它只是个增强，
            # 退一步重来，而不是把整次建议判失败
            if exc.code != 400 or not json_mode:
                raise
            raw = post(url, chat_payload(settings, messages, False),
                       headers, timeout)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc

    try:
        body = json.loads(raw)
        message = body["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIError("模型响应结构不对") from exc
    if not isinstance(message, dict):
        raise AIError("模型响应结构不对")
    return message


def complete_json(settings: AISettings, system: str, user: str,
                  timeout: float = 20.0, options: list[dict] | None = None,
                  request=None, history: list[dict] | None = None) -> dict:
    """调用模型并解析成 `{"option": ..., "reason": ...}`。失败抛 AIError。

    **模型名决定协议**：JEV 系列（`jev-*` / `vendor/jev-*`）走 OpenRouter 的
    decisions 接口（见 `complete_jev`），其余走 OpenAI 兼容的
    `/chat/completions`。`options` 是 JEV 需要的候选动作；chat 模式忽略它。

    `history` 是多轮对话里位于 system 之后、本轮 user 之前的消息。要续接
    推理模型的思考，把上一次的 assistant message（含 `reasoning_details`）
    原样放进去；返回值也会带上本次的 `reasoning_details`。

    chat 模式刻意不引 SDK、不走流式：一次只回几十个 token，urllib 足够，
    也避免给实时链路增加依赖。`response_format=json_object` 能显著降低
    「返回一段带 markdown 围栏的散文」的概率。
    """
    if is_jev_model(settings.model):
        return complete_jev(settings, system, user, timeout=timeout,
                            options=options, request=request)

    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})

    message = chat_message(settings, messages, timeout=timeout,
                           request=request, json_mode=True)
    content = message.get("content")
    if not isinstance(content, str):
        # 推理模型偶尔只回 reasoning、content 为空；这不能当成合法选项
        raise AIError("模型响应结构不对（content 为空）")

    data = parse_json_content(content)
    # 保留原样的 reasoning_details：调用方续推时直接回传，不做任何改写
    if message.get("reasoning_details") is not None:
        data["reasoning_details"] = message["reasoning_details"]
    return data


# ─────────────────────────── JEV decisions ───────────────────────────

def jev_headers(settings: AISettings) -> dict:
    """JEV（OpenRouter decisions）请求头，与 chat 模式共用同一套鉴权/排名头。"""
    return chat_headers(settings)


def jev_payload(settings: AISettings, state: list, questions: dict) -> dict:
    """JEV 请求体：state 是文本/图片项的列表，questions 是带类型的提问。

    与请求样例一一对应：
        {"model": ..., "state": [...], "questions": {...}}
    """
    return {"model": settings.model, "state": list(state),
            "questions": dict(questions)}


def jev_decisions(settings: AISettings, state: list, questions: dict,
                  timeout: float = 20.0, request=None) -> dict:
    """按 JEV decisions 协议提问，返回 `answers`。

    `state` 里每项可以是字符串，也可以是样例里的
    `{"type": "image_url", "image_url": {"url": "data:...;base64,..."}}`。
    `questions` 的结构见请求样例（noul / choice / score）。

    `request` 可注入，签名 `(url, payload, headers, timeout) -> bytes`；
    默认走 urllib。返回的是原始 answers 字典，类型判断交给调用方。
    """
    payload = jev_payload(settings, state, questions)
    post = request or _post_json
    try:
        raw = post(settings.base_url, payload, jev_headers(settings), timeout)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc

    try:
        body = json.loads(raw)
        answers = body["answers"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AIError("模型响应结构不对（缺少 answers）") from exc
    if not isinstance(answers, dict):
        raise AIError("模型响应里的 answers 不是对象")
    return answers


def _option_criterion(option: dict) -> str:
    parts = [str(option.get("label") or option.get("id") or "")]
    if option.get("amount_bb") is not None:
        parts.append(f"{option['amount_bb']}BB")
    if option.get("note"):
        parts.append(str(option["note"]))
    return ", ".join(p for p in parts if p)


def complete_jev(settings: AISettings, system: str, user: str,
                 timeout: float = 20.0, options: list[dict] | None = None,
                 request=None) -> dict:
    """JEV 版本的一次选择：把候选动作变成一道 choice 题。

    返回结构与 `complete_json` 对齐（`option` / `reason`），这样 `advise()`
    不需要知道模型走的是哪套协议。JEV 没有自由文本，`reason` 用选择 +
    概率拼出来；概率同时放进 `confidence`，只用于日志。
    """
    if not options:
        raise AIError("JEV 调用缺少候选选项")

    criteria = {str(o["id"]): _option_criterion(o) for o in options}
    answers = jev_decisions(
        settings,
        state=[system, user],
        questions={
            "option": {
                "type": "choice",
                "instructions": "It is Hero's turn to act. Based on the skills and the hand "
                                "facts, choose the single most appropriate action from the "
                                "candidates. Value-first: if the facts say Hero has a strong "
                                "made hand, prefer the value bet/raise line unless a "
                                "slow-play exception applies; with a strong draw, prefer "
                                "betting/calling over folding when equity is enough. Do not "
                                "stack off one pair against a raise or all-in.",
                "criteria": criteria,
            },
        },
        timeout=timeout,
        request=request,
    )

    answer = answers.get("option")
    if not isinstance(answer, dict):
        raise AIError("模型没有返回 option 答案")
    choice = str(answer.get("choice") or "").strip()
    if choice not in criteria:
        raise AIError(f"模型给了不在选项里的答案：{choice!r}")

    reason = f"JEV chose {criteria[choice]}"
    confidence = None
    probs = answer.get("probabilities")
    if isinstance(probs, dict) and isinstance(probs.get(choice), (int, float)):
        confidence = float(probs[choice])
        reason += f" (probability {confidence:.0%})"
    return {"option": choice, "reason": reason, "confidence": confidence}


def parse_json_content(content: str) -> dict:
    """解析模型正文。允许被 ```json 围栏包住（有些供应商不认 response_format）。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        data = json.loads(text)
    except ValueError:
        # 最后兜一道：供应商偶尔在 JSON 前后加一句废话。只取第一个 { 到最后一个 }
        lo, hi = text.find("{"), text.rfind("}")
        if lo < 0 or hi <= lo:
            raise AIError("模型返回的不是合法 JSON")
        try:
            data = json.loads(text[lo:hi + 1])
        except ValueError as exc:
            raise AIError("模型返回的不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise AIError("模型返回的 JSON 不是对象")
    return data


# ─────────────────────────── 兜底 ───────────────────────────

def fallback_option(sit: Situation) -> str:
    """模型不可用时的确定性兜底。

    刻意**只做最保守的选择**，不试图复刻技能库的完整规则（那是 coach.py 的事）：
    面对下注且读不出牌力 → 弃牌；否则 → 过牌。宁可给一个保守答案，
    也不给一个看着像样却是错的建议。
    """
    if sit.snap.to_call > 0:
        return "fold"
    return "call"           # to_call == 0 时 call 这个 id 就是「过牌」


# ─────────────────────────── 结果 ───────────────────────────

def _confidence(value) -> float | None:
    """模型自评置信度：只接受 0–1；别把 "72%" 这种字符串当数用。"""
    if isinstance(value, str) and value.strip().endswith("%"):
        value = value.strip()[:-1]
        try:
            value = float(value) / 100
        except ValueError:
            return None
    try:
        got = float(value)
    except (TypeError, ValueError):
        return None
    return round(got, 3) if 0.0 <= got <= 1.0 else None


@dataclass(frozen=True)
class Advice:
    hand_id: str
    street: str
    option: Option
    reason: str
    source: str                      # model | fallback
    model: str | None = None
    skill_codes: tuple[str, ...] = ()
    duration_ms: int = 0
    error: str | None = None
    pot: float | None = None
    to_call: float = 0.0
    hero_cards: tuple[str, ...] = ()
    board: tuple[str, ...] = ()
    # 决策点身份与五个快捷档位的当前目标额。点击器用前者判过期、用后者做
    # 「翻前建议 → 最近底池档位」映射；不写进 JSONL，保持输出形状稳定。
    decision_key: tuple = ()
    preset_amounts: tuple[tuple[str, float], ...] = ()
    # 出这条建议时用到的对手画像快照（seat/type/conf/n/source）。空 = 没有画像。
    opponent_reads: tuple[OpponentRead, ...] = ()
    # 模型给的下一街意图与自评置信度。plan 会回灌到下一街的事实块里，
    # 避免「翻牌说要抓诈、转牌又弃牌」这种跨街自相矛盾。
    plan: str = ""
    confidence: float | None = None
    # Math gate 的判定（call/fold/borderline）与「弃牌但闸门说 +EV」标记。
    # 只用于复盘与日志，不改变点击行为。
    gate: str | None = None
    gate_violation: bool = False

    def to_dict(self) -> dict:
        reads = [
            {"seat_id": r.seat_id, "key": r.key,
             "display_name": r.display_name, "archetype": r.archetype,
             "confidence": r.confidence, "n": r.n, "source": r.source,
             "gate": r.gate}
            for r in self.opponent_reads
        ]
        return {
            "hand_id": self.hand_id,
            "street": self.street,
            "hero_cards": list(self.hero_cards),
            "board": list(self.board),
            "pot": self.pot,
            "to_call": round(self.to_call, 2),
            "option": self.option.to_dict(),
            "reason": self.reason,
            "source": self.source,
            "model": self.model,
            "skill_codes": list(self.skill_codes),
            "duration_ms": self.duration_ms,
            **({"error": self.error} if self.error else {}),
            # 复盘时能回答「当时用了哪个对手的什么类型/置信度/样本数」。
            **({"opponent_reads": reads} if reads else {}),
            **({"plan": self.plan} if self.plan else {}),
            **({"confidence": round(self.confidence, 3)}
               if self.confidence is not None else {}),
            **({"math_gate": self.gate} if self.gate else {}),
            **({"gate_violation": True} if self.gate_violation else {}),
        }


def advise(sit: Situation, settings: AISettings | None,
           timeout: float = 20.0, complete=None,
           style: str = DEFAULT_PROMPT_STYLE) -> Advice:
    """出一次建议。`complete` 可注入（单测用），默认走 complete_json。"""
    started = time.monotonic()
    by_id = {o.id: o for o in sit.options}
    gate = math_gate(sit)
    # lean 不加载技能库，skill_codes 留空（它只是复盘时的元数据）。
    selected = (skills.select_skills(_skill_context(sit))
                if style == PROMPT_STYLE_SKILLS else [])

    def make(option: Option, reason: str, source: str,
             error: str | None = None, *, plan: str = "",
             confidence: float | None = None,
             gate_violation: bool = False) -> Advice:
        snap = sit.snap
        return Advice(snap.hand_id, snap.street, option, reason, source,
                      settings.model if settings else None,
                      tuple(s.code for s in selected),
                      int((time.monotonic() - started) * 1000),
                      error, snap.pot, snap.to_call,
                      snap.hero_cards, snap.board,
                      decision_key=snap.decision_key,
                      preset_amounts=tuple(preset_amounts(snap).items()),
                      opponent_reads=tuple(sit.opponent_reads),
                      plan=plan, confidence=confidence,
                      gate=(gate.verdict if gate else None),
                      gate_violation=gate_violation)

    if settings is None:
        return make(by_id[fallback_option(sit)],
                    "未配置 AI_SERVER_*，按最保守的默认动作",
                    "fallback", "未配置模型凭据")

    system, user = build_messages(sit, style)
    caller = complete or complete_json

    def ask(prompt: str) -> dict:
        # options 只给 JEV 用：它需要把候选动作变成一道 choice 题。
        # chat/completions 模式会忽略这个参数
        return caller(settings, system, prompt, timeout=timeout,
                      options=[o.to_dict() for o in sit.options])

    try:
        data = ask(user)
        choice = str(data.get("option", "")).strip()
        if choice not in by_id:
            raise AIError(f"模型给了不在选项里的答案：{choice!r}")
        # Math gate 已经把「跟注是不是 +EV」算过了。模型仍然弃牌时给一次
        # 重问机会；只重问一次，且第一次调用已经吃掉大半超时预算就放弃，
        # 实时链路等不起第二次。
        violation = bool(gate and gate.verdict == "call" and choice == "fold"
                         and "call" in by_id)
        if violation and (time.monotonic() - started) < timeout * 0.6:
            hint = ("\n\n## Correction\nYour answer folded, but the computed "
                    f"«Math gate» says: {gate.text} Reconsider. Fold only if you "
                    "can name a concrete read that makes the gate wrong.\n")
            try:
                retry = ask(user + hint)
                retry_choice = str(retry.get("option", "")).strip()
                if retry_choice in by_id:
                    data, choice = retry, retry_choice
            except AIError:
                pass                     # 重问失败就保留第一份答案
        reason = str(data.get("reason", "")).strip() or "（模型未给理由）"
        plan = str(data.get("plan", "") or "").strip()[:160]
        return make(by_id[choice], reason, "model", plan=plan,
                    confidence=_confidence(data.get("confidence")),
                    gate_violation=violation and choice == "fold")
    except AIError as exc:
        return make(by_id[fallback_option(sit)],
                    "模型不可用，按最保守的默认动作",
                    "fallback", str(exc))


# ─────────────────────────── 后台执行 ───────────────────────────

class AdviceRunner:
    """把模型调用放到后台线程，**绝不阻塞 OCR 主循环**。

    实时链路一帧约 0.5s，模型一次要几秒；同步调用会让采样停顿、画面延迟越积越大。
    这里只保留「最新一个待办」：新的决策点覆盖旧的，旧结果仍然返回并落库
    （它可能已经过时，但复盘时有用）。
    """

    def __init__(self, settings: AISettings | None, *, timeout: float = 20.0,
                 complete=None, log=None,
                 style: str = DEFAULT_PROMPT_STYLE) -> None:
        import queue
        import threading

        self.settings = settings
        self.timeout = timeout
        self._complete = complete
        self.style = style
        self._log = log or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._done: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        # 模型调用进行中。主循环据此暂停识别 —— 等待期间画面只会越来越旧
        self._busy = threading.Event()
        self._busy_since: float | None = None
        self._dropped = 0
        # 上一街自己的决定（只留最近一手）。下一街投递时回灌进事实块，
        # 否则每街都是独立调用，模型会把自己的陷阱线推翻。
        self._last: dict[str, Advice] = {}
        self._thread = threading.Thread(target=self._loop, name="advice",
                                        daemon=True)
        self._thread.start()

    def submit(self, sit: Situation) -> bool:
        """投递一个决策点。队列里已有待办时**用新的覆盖旧的**（最新局面更重要）。"""
        import queue

        try:
            self._queue.put_nowait(sit)
            return True
        except queue.Full:
            # 丢掉还没开始的旧决策点，换成最新的。取走与放回之间 worker
            # 可能刚好取走队列，所以放回也要允许失败
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(sit)
                self._dropped += 1
                return True
            except queue.Full:
                return False

    def poll(self) -> list[Advice]:
        """主循环里调用：取走已完成的结果。"""
        import queue

        out: list[Advice] = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out

    def _loop(self) -> None:
        import queue

        while not self._stop.is_set():
            try:
                sit = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if sit is None:
                return
            self._busy_since = time.monotonic()
            self._busy.set()
            try:
                prev = self._last.get(sit.snap.hand_id)
                # 只回灌模型自己的决定：fallback 的理由是中文兜底文案，
                # 喂回去既没用又会把中文带进英文提示词。
                if (prev is not None and prev.source == "model"
                        and prev.street != sit.snap.street):
                    # 同一手、不同街：把上一街的动作和 plan 带进这一街的事实块。
                    sit = replace(sit, previous=prev)
                try:
                    advice = advise(sit, self.settings, self.timeout,
                                    self._complete, style=self.style)
                except Exception as exc:  # noqa: BLE001 — 后台线程不能死
                    self._log(f"[建议] 生成失败：{type(exc).__name__}: {exc}")
                    continue
                self._last = {advice.hand_id: advice}
                self._done.put(advice)
            finally:
                self._busy_since = None
                self._busy.clear()

    def close(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except Exception:                 # noqa: BLE001 — 队列满就直接退出
            pass
        self._thread.join(timeout=2.0)

    @property
    def busy(self) -> bool:
        """模型调用进行中（从开始调用到结果放进 `_done`）。"""
        return self._busy.is_set()

    @property
    def busy_seconds(self) -> float:
        """这一次模型调用已经跑了多少秒；不在调用中时为 0。

        主循环用它打「生成中…」心跳：等模型时 OCR 是暂停的，没有这行日志，
        控制台会先静默几秒、再把建议和恢复后的识别日志一次性吐出来。
        """
        since = self._busy_since
        return max(time.monotonic() - since, 0.0) if since is not None else 0.0

    @property
    def dropped(self) -> int:
        return self._dropped
