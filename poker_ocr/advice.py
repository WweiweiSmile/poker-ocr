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
from dataclasses import dataclass, field

from . import skills
from .coach import (
    DRY, OVERPAIR, STRONG_DRAW, STRONG_MADE, TPTK, TP_WEAK, WEAK_DRAW,
    board_texture, opponent_strength, parse_cards, read_hand,
)
from .hand_track import ActionRecord, HandSnapshot, PREFLOP, RIVER, TURN
from .table_dynamics import value_heavy_aggression

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
# 大盲在溜入池里再加注时，尺度要比开池更大（没有位置劣势补偿，直接收池）
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


def _cap(target: float, hero_max: float | None, min_raise_to: float) -> tuple[float, str]:
    """把目标额修正到合法范围：低于最小加注抬上去，高于后手封顶为全下。"""
    note = ""
    if target < min_raise_to:
        target = min_raise_to
        note = "raised to the minimum raise"
    if hero_max is not None and target >= hero_max - 1e-9:
        target = hero_max
        note = "stack too short, effectively all-in"
    return _round_bb(target), note


def build_preflop_options(snap: HandSnapshot) -> list[Option]:
    """翻前选项：开池按「位置倍数 ×（有前注 ×2）+ 溜入者」，再加注按 3-4 倍。

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
    min_raise_to = max_bet + increment
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
            amount, note = _cap(max_bet * mult, hero_max, min_raise_to)
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
    elif hero_pos == "BB":
        options.append(Option("call", "Check", "check", None,
                              "Big blind sees the flop free"))
    else:
        options.append(Option("call", "limp in", "call", None,
                              "the first to enter should usually raise"))
    if to_call > 0:
        options.append(Option("fold", "Fold", "fold"))

    limpers = sum(1 for a in snap.all_actions()
                  if a.street == PREFLOP and a.action == "call" and not a.is_hero)
    base = _OPEN_BASES.get(hero_pos, (3.0, 3.25, 3.5))
    ante_x = 2.0 if (snap.blinds and snap.blinds.ante) else 1.0
    for b in base:
        target, note = _cap(b * ante_x + limpers, hero_max, min_raise_to)
        options.append(Option(f"open-{b:g}", f"Raise to {target:g}BB (open)",
                              "raise", target, note))
    return options


def _raise_geometry(snap: HandSnapshot) -> tuple[float, float, float, float | None, float]:
    """加注金额的公共几何：需跟额、底池、当前最高下注、后手封顶、最小加注到。

    `build_options()` 和 `preset_targets()` 共用，避免两处对「最小加注 / 全下
    封顶」的算法漂移。
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


def build_options(snap: HandSnapshot) -> list[Option]:
    """按现场底池与下注算出全部可执行选项。

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
        return build_preflop_options(snap)

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


def build_situation(snap: HandSnapshot,
                    table_dynamics: str | None = None) -> Situation:
    """把快照整理成一次决策的事实。

    `table_dynamics` 是根目录 `.table_dy` 的解析文本（见 table_dynamics.py）。
    由调用方显式传入，而不是在这里读文件：单测和离线重放不会因为仓库里恰好
    有一份动态文件而改变结果。
    """
    read = _read(snap)
    warnings: list[str] = list(snap.warnings)

    texture = None
    if read is not None:
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

    return Situation(
        snap=snap,
        options=build_options(snap),
        hand_label=read.label if read else None,
        hand_tier=read.tier if read else None,
        category=read.category if read else None,
        texture=texture,
        has_draw=bool(read and (read.outs >= 4)),
        plays_board=bool(read and read.plays_board),
        opponent_strength=opp,
        warnings=warnings,
        hand_label_en=read.label_en if read else None,
        texture_en=_TEXTURE_EN.get(texture, texture) if texture else None,
        opponent_strength_en=opp_en,
        equity_pct=read.equity_pct if read else None,
        table_dynamics=(table_dynamics or "").strip() or None,
    )


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
    """当前牌型本身是不是同花/葫芦/四条（这些不怕牌面成花或成对）。"""
    label = f"{sit.hand_label_en or ''} {sit.hand_label or ''}"
    return any(k in label for k in
               ("Flush", "Full house", "Quads", "同花", "葫芦", "四条"))


def _facing_raise(snap: HandSnapshot) -> bool:
    """本街是不是面对加注/全下（而不是一条普通下注）。

    单对子（超对/顶对）在**被加注**时和面对普通下注是两种局面：前者是
    「要不要把筹码打光」的问题，默认弃牌；后者才是抓诈/防守。用动作类型
    判断，不用下注比例 —— 990044 的全下只占池一半，但仍然是全下。
    """
    return any((not a.is_hero) and a.action in ("raise", "allin")
               for a in snap.actions_of(snap.street))


def _is_two_pair(sit: Situation) -> bool:
    label = f"{sit.hand_label_en or ''} {sit.hand_label or ''}".lower()
    return "two pair" in label or "两对" in label


def _passive_value_line(sit: Situation) -> bool:
    """桌风是否把对手本街的多街 aggression 读成价值线。

    只在转牌/河牌、面对下注时触发；翻牌的单条街下注不在此列——那时的价格
    通常还够用，过早弃掉 JJ 这类牌会变成另一种漏洞。
    """
    return (sit.street in (TURN, RIVER)
            and (sit.snap.to_call or 0.0) > 0
            and value_heavy_aggression(sit.table_dynamics))


def value_directive(sit: Situation) -> str | None:
    """把「强牌默认拿价值」写成一句贴着当前分档的硬指令。

    为什么放在 user 事实里、而不是只写在 system 技能里：JEV 是选择题，
    技能目录很长，模型容易只抓到最近的一条「对手弱可以过牌」；一条紧跟
    「My hand strength」的具体指令，比让它自己翻方法论可靠得多。
    """
    if sit.street == PREFLOP or sit.plays_board:
        return None
    tier = sit.hand_tier
    facing = (sit.snap.to_call or 0.0) > 0
    facing_raise = facing and _facing_raise(sit.snap)

    if tier == STRONG_MADE:
        # 顺子/三条/两对不是坚果：牌面三张同花时对手两张同花就是同花；牌面对子时
        # 对手可能有葫芦。这两种局面下不能按「强成牌 = 加注」处理。
        not_nuts = (not _is_flush_or_better(sit)
                    and (_flush_possible(sit.snap.board)
                         or _board_paired(sit.snap.board)))
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
            return ("VALUE CAUTION: strong made hand with no bet in front, but a flush or "
                    "full house is possible. Bet small for value (1/2 pot); do not build a "
                    "huge pot, and check behind on a very coordinated board.")
        size = "1/2–2/3 pot" if sit.texture == DRY else "2/3–full pot"
        return (f"VALUE DEFAULT: strong made hand with no bet in front — BET for value "
                f"({size}). Checking is only correct for a real slow-play: absolute nuts "
                f"on a dry board vs an aggressive opponent, or a bet that only better "
                f"hands call.")

    if tier in (OVERPAIR, TPTK):
        if facing_raise:
            return ("ONE PAIR vs AGGRESSION: overpair/top pair facing a raise or all-in — "
                    "FOLD unless you have a set, two pair, or a strong draw. One pair is "
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
        return ("VALUE DEFAULT: overpair/top pair with a strong kicker and no bet in "
                "front — BET 1/2–2/3 pot for value and protection. Checking gives free "
                "cards and misses value.")

    if tier == STRONG_DRAW:
        if facing:
            return ("DRAW ODDS: compare the estimated draw equity in the facts with "
                    "the break-even equity to call. With 8+ outs, folding is usually "
                    "wrong unless the price is bad.")
        return ("SEMI-BLUFF DEFAULT: 8+ out draw with no bet in front — betting is "
                "preferred (it can win by fold equity or by hitting).")

    if tier == TP_WEAK and facing:
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
    return None


# ─────────────────────────── 提示词 ───────────────────────────

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


def _is_pocket_pair(cards: tuple[str, ...] | list[str]) -> bool:
    """两张底牌同点数。翻前抽 set 的隐含赔率只对口袋对有意义。"""
    return (len(cards) == 2 and all(len(c) >= 2 for c in cards)
            and cards[0][0] == cards[1][0])


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
        lines.append(f"Effective stack (incl. hero): {round(effective, 2)}BB")
    if opps:
        lines.append("Opponents in hand: " + ", ".join(
            f"{o.position or o.seat_id}"
            f"{'(' + o.name + ')' if o.name else ''} "
            f"{o.stack if o.stack is not None else '?'}BB"
            for o in opps))
    if folded:
        lines.append("Folded: " + ", ".join(
            f"{o.position or o.seat_id}" for o in folded))

    lines.append("")
    to_call = round(snap.to_call, 2)
    # 后手不足以下注额时，实际只需跟到全下：客户端也只会给 Fold / All in。
    # 赔率和跟注比例必须按**实际能跟出的金额**算，否则会把门槛算高。
    call_amount = to_call
    if snap.hero_stack is not None and 0 < snap.hero_stack < to_call:
        call_amount = round(snap.hero_stack, 2)
    lines.append(f"Street: {_STREET_EN.get(snap.street, snap.street)}｜"
                 f"Board: {''.join(snap.board) or 'none'}｜"
                 f"Pot: {snap.pot if snap.pot is not None else 'unknown'}BB｜"
                 f"To call: {call_amount}BB")
    if call_amount < to_call:
        lines.append(f"The bet covers my stack: calling {call_amount}BB puts me "
                     f"all-in; raising is not possible.")
    if call_amount > 0 and snap.pot:
        # 赔率门槛：跟注额 ÷ 跟注后的底池。技能里的赔率表就是按这个口径
        bep = call_amount / (snap.pot + call_amount) * 100
        lines.append(f"Break-even equity to call: {bep:.0f}%; "
                     f"call is {call_amount / snap.pot:.0%} of the pot")

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
        # 抽 set 的隐含赔率：翻前不渲染成牌胜率，但小对子需要这组数字才能
        # 判断「跟 1BB 看翻牌」划不划算。阈值（15-20×）来自 preflop-limp.md
        if (to_call > 0 and effective is not None
                and _is_pocket_pair(snap.hero_cards)):
            lines.append(
                f"Implied odds (pocket pair): effective stack / call = "
                f"{effective / to_call:.0f}:1; flopping a set is 11.8% (1 in 8.5), "
                f"so 15:1 or better makes set-mining profitable (10:1 with antes).")
    else:
        tier_en = _TIER_EN.get(sit.hand_tier, sit.hand_tier or "unknown")
        lines.append(f"My hand strength: {sit.hand_label_en or 'unreadable'}"
                     f" (tier: {tier_en})")
        lines.append(f"Board texture: {sit.texture_en or 'unknown'}｜"
                     f"Draw: {'yes' if sit.has_draw else 'no'}｜"
                     f"Plays the board (best five all from board): "
                     f"{'yes' if sit.plays_board else 'no'}")
        if sit.opponent_strength_en:
            o = sit.opponent_strength_en
            lines.append(f"Opponent strength from actions: {o['label']} ({o['reason']})")
        if sit.equity_pct and sit.hand_tier in (STRONG_DRAW, WEAK_DRAW):
            # 四二法则只对**听牌**有意义。成牌（顺子/三条等）的 outs 数的是
            # 「还能不能改进」，不是胜率，直接乘会得到 92% 这种假精确值
            lines.append(f"Estimated draw equity (rule of 4/2): "
                         f"{sit.equity_pct:.0f}%")
        directive = value_directive(sit)
        if directive:
            lines.append(f"Value directive: {directive}")
    if snap.hero_invested is not None and snap.hero_stack:
        ratio = snap.hero_invested / max(snap.hero_stack + snap.hero_invested, 1e-6)
        lines.append(f"I have invested {round(snap.hero_invested, 2)}BB"
                     f" (about {ratio:.0%} of current stack)")

    if sit.warnings:
        # 缺牌/缺筹码这类缺口要让模型知道，它才会在理由里说明而不是硬猜
        lines.append("")
        lines.append("Data gaps: " + "; ".join(sit.warnings))

    if sit.table_dynamics:
        # 人工维护的整桌倾向。放在选项之前：模型对「最后看到的几条事实」
        # 更敏感，而且它不是本手牌的计算结果，不该混进 Computed facts。
        lines.append("")
        lines.append("## Table dynamics (player-provided, authoritative)")
        lines.append(sit.table_dynamics)

    lines.append("")
    lines.append("## Options (pick exactly one id)")
    for o in sit.options:
        amount = f", amount {o.amount}BB" if o.amount is not None else ""
        note = f" ({o.note})" if o.note else ""
        lines.append(f"- {o.id} — {o.label}{amount}{note}")
    return "\n".join(lines)


def build_messages(sit: Situation) -> tuple[str, str]:
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
        "0. «Table dynamics» (when present in the user facts) is the highest-priority "
        "exploitative instruction. When it says the opponent's raises or multi-street "
        "barrels are value-heavy, fold marginal made hands (one pair, two pair) even if "
        "a «Value directive» default says to call. Pot odds only tell you the price; "
        "they do not create bluffs in a value-heavy range.",
        "1. option must come strictly from the ids in the user message's «Options» section; "
        "no extras, no rewrites.",
        "2. All amounts are computed by the program. Do **not** compute the pot yourself, do not "
        "output any amount — output only the id.",
        "3. When facing a bet, actually compare pot odds: the equity needed to call comes from "
        "«Odds & Probability Quick Reference»; compare it against your hand strength / draw "
        "equity — fold if short, call or raise if enough.",
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
        "6. Strong draw (8+ outs) with no bet in front → betting (semi-bluff) is preferred. "
        "Facing a bet, compare the stated estimated equity with the break-even equity; do "
        "not fold a draw that has enough equity.",
        "7. The user facts include an explicit «Value directive» when one of these rules "
        "applies. It is a default for strong made hands; the «Table dynamics» section "
        "overrides it when it describes the opponent's aggression as value-heavy. In "
        "particular, a Value directive that says «call a normal-sized bet» does NOT bind "
        "you to call a turn/river barrel with one pair or two pair when the table notes "
        "say that line is value-heavy.",
        "8. One pair is not a stack-off hand: overpair/top pair facing a raise or all-in "
        "→ fold unless you have a set, two pair, or a strong draw. Do not call off a "
        "large bet with one pair.",
        "9. Before raising a strong hand, check whether the board allows a better hand: "
        "three or more of one suit → a flush is possible; a paired board → a full house "
        "or quads are possible. A straight/two pair/set is NOT the nuts then. On such a "
        "board, call for showdown instead of raising — a raise only gets called by better.",
        "",
        "## Table dynamics (user-provided, when present)",
        "The user facts may include a «Table dynamics» section written by the player. "
        "Treat it as authoritative for the current table and adjust exploitative choices:",
        "- loose-passive / calling station / novice: bluff less, value-bet thinner "
        "(more marginal hands) and size up, and never slow-play strong hands. Their "
        "raises and multi-street barrels are value-heavy: fold marginal made hands "
        "(including two pair on a paired board) instead of paying them off, unless you "
        "have a set/straight/flush or a strong draw with the right price;",
        "- tight / rarely bluffs / nit: fold marginal bluff-catchers, do not pay off, "
        "value-bet tighter (fewer marginal value hands), respect raises, and bluff more "
        "selectively;",
        "- loose-aggressive / maniac: trap with strong hands, call down lighter, and avoid "
        "bluffing wars;",
        "- tight-aggressive / solid: play straightforwardly, avoid fancy plays, and give "
        "credit to aggression.",
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
    system_parts.append(skills.render_catalog(all_, selected))
    system_parts.append(skills.render_block(selected))

    return "\n".join(system_parts), render_hand_facts(sit)


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
    thinking: str | None = None           # enabled | disabled
    reasoning_effort: str | None = None   # none | low | high | max

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

        temperature = _env_number(env, "AI_SERVER_TEMPERATURE", float, 0.0, 2.0)
        if temperature is None:
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
                   reasoning_effort=effort)


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
    # 鉴权失败不回显 body：供应商常在里面回显 Key 片段
    if exc.code in (401, 403):
        return AIError("模型鉴权失败，请检查 AI_SERVER_API_KEY")
    return AIError(f"模型服务返回 HTTP {exc.code}")


def complete_json(settings: AISettings, system: str, user: str,
                  timeout: float = 20.0, options: list[dict] | None = None,
                  request=None) -> dict:
    """调用模型并解析成 `{"option": ..., "reason": ...}`。失败抛 AIError。

    **模型名决定协议**：JEV 系列（`jev-*` / `vendor/jev-*`）走 OpenRouter 的
    decisions 接口（见 `complete_jev`），其余走 OpenAI 兼容的
    `/chat/completions`。`options` 是 JEV 需要的候选动作；chat 模式忽略它。

    chat 模式刻意不引 SDK、不走流式：一次只回几十个 token，urllib 足够，
    也避免给实时链路增加依赖。`response_format=json_object` 能显著降低
    「返回一段带 markdown 围栏的散文」的概率。
    """
    if is_jev_model(settings.model):
        return complete_jev(settings, system, user, timeout=timeout,
                            options=options, request=request)

    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + settings.api_key}

    def payload(json_mode: bool) -> dict:
        body = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        # 只发显式配置过的参数。deepseek-flash 思考模式会忽略 temperature，
        # 所以实时链路默认在 .env 里关掉思考（AI_SERVER_THINKING=disabled）。
        if settings.temperature is not None:
            body["temperature"] = settings.temperature
        if settings.top_p is not None:
            body["top_p"] = settings.top_p
        if settings.max_tokens is not None:
            body["max_tokens"] = settings.max_tokens
        if settings.thinking is not None:
            body["thinking"] = {"type": settings.thinking}
        if settings.reasoning_effort is not None:
            body["reasoning_effort"] = settings.reasoning_effort
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    url = settings.base_url + "/chat/completions"
    post = request or _post_json
    try:
        try:
            raw = post(url, payload(True), headers, timeout)
        except urllib.error.HTTPError as exc:
            # 有些兼容层不认 response_format（回 400）。它只是个增强，
            # 退一步重来，而不是把整次建议判失败
            if exc.code != 400:
                raise
            raw = post(url, payload(False), headers, timeout)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc

    try:
        body = json.loads(raw)
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIError("模型响应结构不对") from exc

    return parse_json_content(content)


# ─────────────────────────── JEV decisions ───────────────────────────

def jev_headers(settings: AISettings) -> dict:
    """JEV（OpenRouter）请求头。Referer/Title 是可选的排名信息。"""
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + settings.api_key}
    if settings.referer:
        headers["HTTP-Referer"] = settings.referer
    if settings.title:
        headers["X-OpenRouter-Title"] = settings.title
    return headers


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

    def to_dict(self) -> dict:
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
        }


def advise(sit: Situation, settings: AISettings | None,
           timeout: float = 20.0, complete=None) -> Advice:
    """出一次建议。`complete` 可注入（单测用），默认走 complete_json。"""
    started = time.monotonic()
    by_id = {o.id: o for o in sit.options}
    selected = skills.select_skills(_skill_context(sit))

    def make(option: Option, reason: str, source: str,
             error: str | None = None) -> Advice:
        snap = sit.snap
        return Advice(snap.hand_id, snap.street, option, reason, source,
                      settings.model if settings else None,
                      tuple(s.code for s in selected),
                      int((time.monotonic() - started) * 1000),
                      error, snap.pot, snap.to_call,
                      snap.hero_cards, snap.board,
                      decision_key=snap.decision_key,
                      preset_amounts=tuple(preset_amounts(snap).items()))

    if settings is None:
        return make(by_id[fallback_option(sit)],
                    "未配置 AI_SERVER_*，按最保守的默认动作",
                    "fallback", "未配置模型凭据")

    system, user = build_messages(sit)
    caller = complete or complete_json
    try:
        # options 只给 JEV 用：它需要把候选动作变成一道 choice 题。
        # chat/completions 模式会忽略这个参数
        data = caller(settings, system, user, timeout=timeout,
                      options=[o.to_dict() for o in sit.options])
        choice = str(data.get("option", "")).strip()
        if choice not in by_id:
            raise AIError(f"模型给了不在选项里的答案：{choice!r}")
        reason = str(data.get("reason", "")).strip() or "（模型未给理由）"
        return make(by_id[choice], reason, "model")
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
                 complete=None, log=None) -> None:
        import queue
        import threading

        self.settings = settings
        self.timeout = timeout
        self._complete = complete
        self._log = log or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._done: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        # 模型调用进行中。主循环据此暂停识别 —— 等待期间画面只会越来越旧
        self._busy = threading.Event()
        self._busy_since: float | None = None
        self._dropped = 0
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
                try:
                    advice = advise(sit, self.settings, self.timeout, self._complete)
                except Exception as exc:  # noqa: BLE001 — 后台线程不能死
                    self._log(f"[建议] 生成失败：{type(exc).__name__}: {exc}")
                    continue
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
