"""实时 AI 决策建议：把算好的中文事实交给模型，只回一个选项。

现在只保留一套中文 `lean` 提示词：system 给决策顺序与输出契约，user 给状态、
几何/价格/牌力/牌面/对手事实、全场统计/人工观察、行动线与**带实际金额的中文选项**。
不再加载技能库、范围表、价值指令、Math gate 或 GTO；模型只看事实自己做选择。

为什么输出是「选项编号」而不是自由文本：

  · 实时场景下人要的是一秒内能执行的动作，不是分析文章。
  · 选项把尺度约束死了（1/3、1/2、2/3、1、1.2 倍池、all-in），模型不可能
    编出不存在的尺度。
  · 选项由本模块按现场底池算好金额，模型只做选择 —— 它不需要会算底池，
    也就不会算错。这一点和 coach.py「确定性规则不交给模型」是同一条原则。

`value_directive` / `math_gate` / `render_hand_facts` 等旧规则层保留在本文件，
**只供 tools/replay 离线审计**，`build_messages` 与 `advise` 不再调用它们。

调用协议：OpenAI 兼容的 /chat/completions（与 call-back 的 AIClient 同口径），
凭据来自环境变量 AI_SERVER_API_KEY / AI_SERVER_BASE_URL / AI_SERVER_MODEL。
**非流式**：一次建议的正文只有几十个 token，流式只会增加复杂度；超时用
`socket` 级的总时长控制（默认 20 秒，实时链路等不起更久）。**连接复用**：
同一 host 的 HTTP/1.1 长连接会被缓存，省掉每次建议都要付的 TLS 握手
（国内到 OpenRouter 实测 0.4–1.4s，常常比模型本身还慢）。
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Iterable

from .coach import (
    DRY, HIGH_CARD, MID_BOTTOM, ONE_PAIR, OVERPAIR, STRAIGHT, STRONG_DRAW,
    STRONG_MADE, TPTK, TP_WEAK, TWO_PAIR,
    WEAK_DRAW, board_texture, opponent_strength, parse_cards, read_hand,
)
from .ground_truth import GroundTruth, card_text, compute_ground_truth
from .hand_track import ActionRecord, FLOP, HandSnapshot, PREFLOP, RIVER, TURN
from .opponent_profile import (AGGRESSIVE_ARCHETYPES, PASSIVE_ARCHETYPES,
                               OpponentRead, PooledStats, pooled_stats_to_dict,
                               render_reads, render_reads_zh,
                               table_exploit_zh)
from .positions import BB, POSITIONS_BY_TABLE_SIZE, SB
from .preflop_ranges import range_verdict, sb_completable
from .table_dynamics import (is_aggressive_table, is_loose_table,
                             is_passive_table, value_heavy_aggression)

# ─────────────────────────── 选项 ───────────────────────────

# 行动线里 hero 的标识。与 coach.advise 的 hero_position 口径一致
HERO_ACTOR = "hero"

# 当前唯一的中文 lean 提示词版本。写入 session_start / advice JSONL，
# 复盘归因用；改动 system 或事实块时 bump。
PROMPT_VERSION = "lean-zh-2026-11-01.4"
PROMPT_LANGUAGE = "zh"

# 加注尺度：名字 → 底池倍数。「满池 1.2」是超池，用于需要施压/劝退的场合
RAISE_FRACTIONS: dict[str, float] = {
    "1/3": 1 / 3,
    "1/2": 0.5,
    "2/3": 2 / 3,
    "1": 1.0,
    "1.2": 1.2,
}

# 用户指定的选项顺序。AI 只能返回这里的 id；all-in 永远是最后一个。
OPTION_ORDER = ("fold", "call", "raise-1/3", "raise-1/2", "raise-2/3",
                "raise-1", "raise-1.2", "all-in")

# 尺度后缀（中文标签用）。option id 保持不变，避免点击器/日志契约漂移。
_SIZE_ZH = {
    "1/3": "1/3 池",
    "1/2": "1/2 池",
    "2/3": "2/3 池",
    "1": "满池",
    "1.2": "1.2 池",
}


@dataclass(frozen=True)
class Option:
    """一个可执行选项。amount 是**加注后的本街总额**（BB），跟注为需跟额。"""

    id: str
    label: str
    kind: str                     # check | call | fold | raise | bet | allin
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


# all-in 只有在几何上说得通时才挂进选项：深码顶对局面挂一个 10 倍池的
# 「全下」只会污染动作空间，模型还得自己判断它是不是正常尺度。阈值按
# 跟注后底池和 SPR 双重判断；短码推弃（≤20BB）和已经套池（SPR≤3）不受限。
ALL_IN_MAX_POT_MULTIPLE = 2.5
PREFLOP_ALLIN_MAX_STACK_BB = 20.0


def _all_in_option(snap: HandSnapshot) -> Option | None:
    """生成 all-in 选项；几何上不合理时返回 None。

    · 翻前：只给短码推弃（≤20BB）或面对 3-bet 且金额没有离谱到超池的局面；
      100BB 普通开池、40BB 普通开池都不再挂 all-in。
    · 翻后：金额超过「跟注后底池」2.5 倍、且 SPR > 3、又不是在被大注
      逼着套池时，不追加。
    `to_call >= hero_stack` 时走「跟全下/弃牌」分支，不重复生成。
    """
    stack = snap.hero_stack
    if stack is None or stack <= 0:
        return None
    to_call = max(snap.to_call or 0.0, 0.0)
    if to_call >= stack:
        return None
    hero_max = _round_bb((snap.hero_bet or 0.0) + stack)
    if hero_max <= 0:
        return None

    if snap.street == PREFLOP:
        short_stack = stack <= PREFLOP_ALLIN_MAX_STACK_BB
        facing_threebet = "three-bet" in (snap.preflop_shapes or ())
        if not (short_stack or facing_threebet):
            return None
    else:
        presets = preset_targets(snap)
        if presets and hero_max <= max(a for a, _ in presets.values()) + 1e-9:
            return None

    effective = _effective_stack(snap)
    # 只能被 effective stack 跟到；把「全下」标成真正能进底池的金额，
    # 避免 240BB 后手对 91.5BB 对手写出「全下 240BB」这种噪声。
    # `effective` 已经含对手本街已下注（= 他能匹配的本街总额），不能再
    # 加一次 Hero 自己的下注，否则 5 + 51.5 = 56.5 会超过对手能跟的 51.5。
    all_in_amount = hero_max
    if effective is not None and effective < hero_max - 1e-9:
        all_in_amount = _round_bb(effective)

    pot_after = winnable_pot(snap)
    if pot_after > 0 and stack > PREFLOP_ALLIN_MAX_STACK_BB:
        pot_before = pot_after - call_amount(snap)
        spr = (effective / pot_before
               if effective is not None and pot_before > 0 else None)
        committing = to_call >= 0.5 * stack
        too_big = all_in_amount > ALL_IN_MAX_POT_MULTIPLE * pot_after
        if too_big and not committing and (spr is None or spr > 3.0):
            return None

    note = ""
    if effective is not None and effective < hero_max - 1e-9:
        note = (f"实际后手 {hero_max:g}BB；有效筹码只有 {effective:g}BB，"
                f"多出的 {hero_max - effective:g}BB 不会被跟")
    return Option("all-in", f"全下 {all_in_amount:g}BB", "allin",
                  all_in_amount, note)


# 翻前开池的基础倍数（bb），按位置分档，来自 preflop-open.md：
# 前位 2.5-3.0 / 中位 3.0-3.5 / 后位 3.5-4.0 / 小盲 3.0。
# 大盲在溜入池里再加注时，尺度要比开池更大（没有位置劣势补偿，直接收池）。
# 这些是**无前注、底池 1.5BB** 时的原始尺度。实际开池还要乘上底池缩放
# (底池/1.5)**OPEN_POT_EXPONENT（见 build_preflop_options），再被本桌
# 5.5BB 最低开池（PREFLOP_MIN_RAISE_BB）抬一次下限。本桌 9 人 0.5BB 前注
# → 底池 6BB → 缩放 2.0 倍 → 后位 7-8BB。
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

# 开池的底池缩放指数：开池 = 位置基础 × (底池 / 1.5BB) ** 指数。
#
# 0.5（开方）时底池 4 倍 → 开池 2 倍（后位 8BB）；0.6 → 后位 9.2BB。
# 1.0（线性）会在底池 6BB 时把后位开到 16BB，可跟注范围直接两极化。
# 指数越大，前注/溜入者推高尺度的力度越强。2026-11-01 从 0.6 下调到 0.5：
# 本桌 6BB 底池下后位大档 9BB → 8BB。
OPEN_POT_EXPONENT = 0.5

# 松桌的开池加成（BB）：跟注站/松弱桌开大一点收价值；紧桌不加。
# 叠加在「位置倍数 × 底池缩放」之后，再由 _cap 做全下/最小加注修正。
#
# 溜入者不再单独加成：他们本来就把底池做大了，开池尺度按底池缩放时已经
# 计过一次；再叠一层就是重复收费（底池涨一次、尺度再涨一次）。
LOOSE_OPEN_BONUS = 0.5

# 翻前桌上规则：任何加注/开池/再加注的「加注到」总额不得低于 5.5BB。
# 牌桌客户端会拒绝更小的尺度，给出 2.5BB 的开池建议等于给一个点不出来的动作。
# 位置尺度（2.5-4BB）低于这个下限时由 `_cap()` 抬到 5.5BB；真正的全下不受此限
# （后手不足 5.5BB 时只能全下，这是合法动作）。改牌桌规则时只改这个常量。
PREFLOP_MIN_RAISE_BB = 5.5

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
         min_note: str = "已抬到最小加注") -> tuple[float, str]:
    """把目标额修正到合法范围：低于最小加注抬上去，高于后手封顶为全下。

    `min_note` 让翻前的牌桌下限（5.5BB）能和扑克本身的最小加注区分开：
    模型看到「已抬到本桌最低加注 5.5BB」才知道不是程序算错了。
    """
    note = ""
    if target < min_raise_to:
        target = min_raise_to
        note = min_note
    if hero_max is not None and target >= hero_max - 1e-9:
        target = hero_max
        note = "后手不足，等同全下"
    return _round_bb(target), note


def count_limpers(snap: HandSnapshot) -> int:
    """我首次行动前、且第一次加注前已经平跟进池的人数。

    行动线的金额是「本街加到总额」：只有 ≤1BB 的 call 才是溜入；6.5BB 的
    call 是「跟加注」。光按金额筛不够：4-bet 之后 SB 补齐 0.5BB 也是 ≤1BB，
    但它不是溜入。所以再加两道门：只看 Hero 首次行动之前、第一次加注之前。

    优先看行动线；但 limper 平跟 1BB 往往不生成动作徽章，行动线会漏。
    再按「同帧各家下注额 == 1BB 且不是盲注位」补一次，取两者较大值。
    """
    bb = snap.blinds.bb if snap.blinds else 1.0
    acts = list(snap.actions_of(PREFLOP))
    hero_idx = next((i for i, a in enumerate(acts) if a.is_hero), len(acts))
    first_raise = next(
        (i for i, a in enumerate(acts) if a.action in ("raise", "bet")),
        None)
    cutoff = hero_idx if first_raise is None else min(hero_idx, first_raise)
    from_actions = sum(
        1 for a in acts[:cutoff]
        if a.action == "call" and not a.is_hero
        and (a.amount is None or float(a.amount) <= bb + 1e-9))
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
    # 本桌的 5.5BB 下限与扑克的最小加注取较严的那个：上家已经加注到 10BB 时，
    # 合法的再加注到 20BB 比 5.5BB 更严，不能被 5.5BB 覆盖掉。
    table_min_raise_to = bb * PREFLOP_MIN_RAISE_BB
    min_raise_to = max(legal_min_raise_to, table_min_raise_to)
    min_note = ("已抬到最小加注"
                if legal_min_raise_to >= table_min_raise_to
                else f"已抬到本桌最低加注 {table_min_raise_to:g}BB")
    hero_max = (hero_bet + stack) if stack is not None else None

    # 下注/加注已经超过后手：跟注就是全下，牌桌上不存在「再加注」。
    # 此时只能弃牌或 all-in —— 与客户端只显示 Fold / All in 一致。
    call_all_in = (stack is not None and stack > 0 and to_call >= stack)
    if call_all_in:
        options = [
            Option("call", f"跟全下 {_round_bb(stack):g}BB", "call",
                   _round_bb(stack), "对手下注已覆盖我的后手，跟注即全下"),
            Option("fold", "弃牌", "fold"),
        ]
        by_id = {o.id: o for o in options}
        return [by_id[i] for i in OPTION_ORDER if i in by_id]

    # 判断「前面有没有人加注」用形态，不用 to_call：小盲面对大盲时 to_call=0.5，
    # 那是补齐盲注，不是面对加注
    facing_raise = any(s in ("single-raise", "three-bet")
                       for s in snap.preflop_shapes)

    options: list[Option] = []
    if facing_raise and to_call > 0:
        options.append(Option("call", f"跟注 {_round_bb(to_call):g}BB", "call",
                              _round_bb(to_call)))
        options.append(Option("fold", "弃牌", "fold"))
        # 面对加注：3bet/再加注到「上家加注的 3 倍 / 4 倍」，来自
        # preflop-3bet-pot.md「有位置 3-4 倍，盲注位一律 4 倍」。
        #
        # 标签只写动作的性质（3-bet / 4-bet），倍数放括注：原来直接把倍数字
        # 写成「4-bet」，模型会把「对单加注的 4 倍」误读成「对 3-bet 的再加注」，
        # 进而套用错误的 4-bet 范围。
        prefix = "4-bet" if "three-bet" in (snap.preflop_shapes or ()) else "3-bet"
        for mult in (3, 4):
            amount, note = _cap(max_bet * mult, hero_max, min_raise_to, min_note)
            options.append(Option(
                f"raise-{mult}x", f"{prefix} 到 {amount:g}BB（{mult} 倍）", "raise",
                amount, note))
        extra = _all_in_option(snap)
        if extra is not None:
            options.append(extra)
        return options

    # 没人加注：平跟（limp）/ 补齐盲注 / 大盲过牌，然后按位置倍数开池。
    # 平跟标签必须区分「小盲补齐」和「非盲位的 over-limp」：写成 complete
    # 会让模型以为这是小盲的补盲动作，看不到溜入池里抽 set 的线路
    hero_pos = snap.hero_position or ""
    if to_call > 0:
        label = ("补齐盲注" if hero_pos == "SB"
                 else f"平跟 {to_call:g}BB（溜入）")
        options.append(Option("call", label, "call", _round_bb(to_call)))
    elif hero_pos == "SB":
        # SB 已经补齐盲注（to_call=0）时，这是 check/raise 点，不是 limp。
        # h60 的第二条 preflop 建议把这里标成 "limp in"，模型只能选
        # 「最便宜的退出」；标签错了，动作空间也就错了。
        if (snap.hero_bet or 0.0) >= bb:
            options.append(Option("call", "过牌", "check", None,
                                  "小盲已补齐盲注，可以过牌或加注"))
        else:
            need = max(bb - (snap.hero_bet or 0.0), 0.0)
            options.append(Option("call", "补齐盲注", "call",
                                  _round_bb(need) if need else None,
                                  "小盲还需要补齐大盲"))
    elif hero_pos == "BB":
        options.append(Option("call", "过牌", "check", None,
                              "大盲可以免费看翻牌"))
    else:
        options.append(Option("call", "平跟进池", "call", None,
                              "第一个入池通常应该加注"))
    if to_call > 0:
        options.append(Option("fold", "弃牌", "fold"))

    base = _OPEN_BASES.get(hero_pos, (3.0, 3.25, 3.5))
    # 开池尺度按**底池**缩放，不按盲注。
    #
    # 2.5-4BB 这套位置尺度成立的前提是「无前注、底池 1.5BB」。本桌 9 人
    # 各交 0.5BB 前注，底池一到翻前就是 6BB —— 开 5.5BB 时大盲只需补 4.5
    # 进 16，门槛 28%，比无前注局的 31% 还便宜。开池要对已经躺在底池里的
    # 死钱收费，所以尺度必须跟着底池走。
    #
    # 用**开方**而不是线性：底池 1.5→6 是 4 倍，开池跟着乘 2（前位 5BB、
    # 后位 8BB）；线性放大到 10-16BB 会把能跟注的范围压成两极化 —— 那正是
    # h39 当初要避免的问题。溜入者也通过底池自动计入，不再单独加码，避免
    # 「底池涨一次、尺度再涨一次」的重复计算。
    pot_now = max(float(snap.pot or 0.0), 1.5 * bb)
    scale = (pot_now / (1.5 * bb)) ** OPEN_POT_EXPONENT
    rows: list[tuple[float, str, float]] = []
    for b in base:
        target = b * scale + max(open_bonus, 0.0)
        target, note = _cap(target, hero_max, min_raise_to, min_note)
        rows.append((target, note, b))

    # 5.5BB 下限会把 2.5/2.75/3.0 三个基础尺度一起抬成同一个金额。
    # 三个 id、同一个金额不是选择，是噪声：改成下限、下限+1、下限+2 三个
    # 真正不同的尺度（仍然走 _cap，后手不足时会封顶/去重）。
    if len({target for target, _note, _b in rows}) <= 2:
        floor = max(min_raise_to, table_min_raise_to)
        rows = []
        for b, step in zip(base, (0.0, 1.0, 2.0)):
            target, note = _cap(floor + step, hero_max, min_raise_to, min_note)
            # floor 本身就在牌桌下限上：_cap 看到 target == min_raise_to 不会
            # 再补 note，但这一行是**按牌桌下限构造**的，不说明来源模型会以为
            # 是程序把尺度算成了 5.5。补一句，和第一行的口径一致。
            if not note and abs(target - table_min_raise_to) < 1e-9:
                note = f"已抬到本桌最低加注 {table_min_raise_to:g}BB"
            rows.append((target, note, b))

    seen_targets: set[float] = set()
    for target, note, base_size in rows:
        if target in seen_targets:
            continue
        seen_targets.add(target)
        table_min = "（本桌最低加注）" if target >= table_min_raise_to \
            and target == table_min_raise_to else ""
        options.append(Option(f"open-{base_size:g}",
                              f"开池到 {target:g}BB{table_min}",
                              "raise", target, note))
    extra = _all_in_option(snap)
    if extra is not None:
        options.append(extra)
    return options


def _raise_geometry(snap: HandSnapshot) -> tuple[float, float, float, float | None, float]:
    """加注金额的公共几何：需跟额、底池、当前最高下注、后手封顶、最小加注到。

    `build_options()` 和 `preset_targets()` 共用，避免两处对「最小加注 / 全下
    封顶」的算法漂移。翻前还要叠加本桌的 5.5BB 下限（`PREFLOP_MIN_RAISE_BB`）：
    点击器按这个几何选档位，档位目标低于 5.5BB 就等于点出一个牌桌拒绝的动作。
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


def _unsized_options(snap: HandSnapshot) -> list[Option]:
    """底池/后手未读全时的保守选项：只给过牌/跟注/弃牌。

    没有底池就算不出任何合法尺度；这时硬塞 1BB 的「下注」等于伪造事实。
    """
    to_call = max(snap.to_call or 0.0, 0.0)
    stack = snap.hero_stack
    options: list[Option] = []
    if to_call > 0:
        amount = call_amount(snap)
        label = (f"跟全下 {amount:g}BB"
                 if stack is not None and stack <= amount + 1e-9
                 else f"跟注 {amount:g}BB")
        options.append(Option("call", label, "call", amount,
                              "底池/后手未读全，只保留跟注/弃牌"))
        options.append(Option("fold", "弃牌", "fold"))
    else:
        options.append(Option("call", "过牌", "check", None,
                              "底池/后手未读全，暂不下注"))
    by_id = {o.id: o for o in options}
    return [by_id[i] for i in OPTION_ORDER if i in by_id]


def _abnormal_options(snap: HandSnapshot) -> list[Option]:
    """翻后 0 对手异常帧的保守选项：只给过牌/跟注/弃牌。

    没有在局对手时，任何「下注/加注尺度」都是按假想对手算的伪事实；
    和 `_unsized_options()` 的区别是 note 明确写「状态帧异常」。
    """
    to_call = max(snap.to_call or 0.0, 0.0)
    stack = snap.hero_stack
    options: list[Option] = []
    if to_call > 0:
        amount = call_amount(snap)
        label = (f"跟全下 {amount:g}BB"
                 if stack is not None and stack <= amount + 1e-9
                 else f"跟注 {amount:g}BB")
        options.append(Option("call", label, "call", amount,
                              "状态帧异常，只保留跟注/弃牌"))
        options.append(Option("fold", "弃牌", "fold"))
    else:
        options.append(Option("call", "过牌", "check", None,
                              "状态帧异常，只保留过牌"))
    by_id = {o.id: o for o in options}
    return [by_id[i] for i in OPTION_ORDER if i in by_id]


def build_options(snap: HandSnapshot,
                  open_bonus: float = 0.0) -> list[Option]:
    """按现场底池与下注算出全部可执行选项。

    `open_bonus` 只加在翻前开池上（松桌 +1BB），翻后按底池倍数的选项不动。

    `to_call == 0` 是「轮到我、没人下注」：没有可跟的注，选项是过牌 + 各种下注。
    `to_call > 0` 是面对下注/加注：跟注 + 弃牌 + 各种加注。

    金额口径（与求解器/牌桌语言的通行口径一致）：

      · 面对下注：跟注后底池 = 底池 + 需跟额；「加注到 f 倍池」= 加注额再取它的 f，
        即 `加注后的本街总额 = 当前最高下注 + f × (底池 + 需跟额)`。
      · 没人下注：「下注 f 倍池」= `底池 × f`。

    金额全部由本模块算好，模型只选档位。两处**必须**的修正：
    低于最小加注额的档位要抬到合法值（否则给出一个牌桌上做不出来的动作），
    超过后手的要封顶并注明等同全下。深码/短码场景在末尾追加 `all-in`。
    底池或后手缺失时只给过牌/跟注/弃牌：算不出尺度就不要伪造尺度。
    """
    if snap.pot is None or snap.pot <= 0 or snap.hero_stack is None:
        return _unsized_options(snap)
    if snap.street == PREFLOP:
        return build_preflop_options(snap, open_bonus=open_bonus)

    to_call = max(snap.to_call or 0.0, 0.0)
    stack = snap.hero_stack

    options: list[Option] = []
    if to_call <= 0:
        options.append(Option("call", "过牌", "check",
                              note="还没人下注，可以过牌"))
    else:
        # 下注超过后手：跟注即全下，且不能再加注。客户端此时只给
        # Fold / All in 两个按钮，选项必须与牌桌一致。
        if stack is not None and stack > 0 and to_call >= stack:
            options.append(Option("call", f"跟全下 {_round_bb(stack):g}BB",
                                  "call", _round_bb(stack),
                                  "对手下注已覆盖我的后手，跟注即全下"))
            options.append(Option("fold", "弃牌", "fold"))
            by_id = {o.id: o for o in options}
            return [by_id[i] for i in OPTION_ORDER if i in by_id]
        options.append(Option("call", f"跟注 {_round_bb(to_call):g}BB", "call",
                              _round_bb(to_call)))
        options.append(Option("fold", "弃牌", "fold"))

    presets = preset_targets(snap)
    for name in ("1/3", "1/2", "2/3", "1", "1.2"):
        option_id = f"raise-{name}"
        amount, note = presets[option_id]
        if to_call > 0:
            label, kind = (f"加注到 {amount:g}BB（{_SIZE_ZH[name]}加注）",
                           "raise")
        else:
            label, kind = f"下注 {amount:g}BB（{_SIZE_ZH[name]}）", "bet"
        options.append(Option(option_id, label, kind, amount, note))

    extra = _all_in_option(snap)
    if extra is not None:
        options.append(extra)

    by_id = {o.id: o for o in options}
    return [by_id[i] for i in OPTION_ORDER if i in by_id]


# ─────────────────────── 位置 / SPR / 赔率闸门 ───────────────────────

def call_amount(snap: HandSnapshot) -> float:
    """跟注实际要付的筹码：后手不足时只能跟到全下。

    赔率门槛、离线审计、事实渲染必须用同一个口径，否则「后手 5BB 面对
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


def _street_bet(snap: HandSnapshot, opponent) -> float:
    """对手本街已下注总额；下注槽读不到时从行动线兜底，仍未知按 0。"""
    bet = opponent.bet
    if bet is None:
        bet = _street_bet_from_actions(snap, opponent)
    return float(bet or 0.0)


def _same_actor(action: ActionRecord, opponent) -> bool:
    """行动线记录是否属于某个对手（seat_id 或位置任一匹配）。"""
    if action.is_hero:
        return False
    if action.seat_id and opponent.seat_id and action.seat_id == opponent.seat_id:
        return True
    return bool(action.position and opponent.position
                and action.position == opponent.position)


def _big_blind_option_pending(snap: HandSnapshot):
    """翻前 BB 还没主动行动时，即使已投满 1BB 也仍有加注选项。

    `behind_opponents()` 的有下注分支只数「还没跟到最高注」的人。BB 已经
    投了 1BB，Hero 平跟/补盲到 1BB 后 BB 的已投注正好等于最高注，会被过滤
    掉；但 BB 在这一轮仍有 check/raise 的选项，Hero 并没有收尾。
    """
    if snap.street != PREFLOP or (snap.to_call or 0.0) <= 0:
        return None
    if snap.hero_position == BB:
        return None
    bb_opp = next((o for o in snap.opponents
                   if o.position == BB and _can_act(o)), None)
    if bb_opp is None:
        return None
    pre = snap.actions_of(PREFLOP)
    # BB 已经主动行动过（过牌/跟注/加注），或者已经有人加注：前者没有
    # option 了，后者 BB 的 1BB 低于最高注，正常逻辑会把它算进来。
    if any(_same_actor(a, bb_opp) for a in pre):
        return None
    if any(a.action in ("raise", "bet")
           or (a.action == "call" and a.all_in) for a in pre):
        return None
    max_bet = (snap.hero_bet or 0.0) + float(snap.to_call or 0.0)
    if _street_bet(snap, bb_opp) >= max_bet - 1e-9:
        return bb_opp
    return None


def behind_opponents(snap: HandSnapshot) -> list:
    """本街 Hero 之后还要行动的对手。

    有下注时不能按「位置在我后面」数：先行动的人（SB/BB/溜入者）可能还没
    跟到最高注，他们在这一轮会绕回来再说话；而已经下到最高注的人（最后
    加注者）在我跟注后不会再行动。所以有下注时只看「还没跟到最高注」。
    没人下注时才是纯位置顺序：排在我后面的都要过牌/下注。
    """
    order = _action_order(snap)
    hero = snap.hero_position
    if order is None or hero not in order:
        return []
    idx = order.index(hero)
    live = [o for o in snap.opponents if _can_act(o) and o.position in order]
    if snap.to_call and snap.to_call > 0:
        max_bet = (snap.hero_bet or 0.0) + float(snap.to_call)
        behind = []
        for o in live:
            bet = _street_bet(snap, o)
            if bet >= max_bet - 1e-9:
                continue
            # 轮到我时，排在我前面的人如果是 0 注，常见有两种：已经过牌
            # （下注/加注会重新打开行动，他仍要响应）或漏读弃牌/下注额。
            # 只有明确记录过 check 的人才算「还会再说话」；完全没有动作
            # 记录的 0 仍按漏读处理，避免凭一个坏帧说「身后还有人」。
            if bet <= 1e-9 and order.index(o.position) <= idx:
                checked = any(
                    a.action == "check" and _same_actor(a, o)
                    for a in snap.actions_of(snap.street))
                if not checked:
                    continue
            behind.append(o)
        bb_option = _big_blind_option_pending(snap)
        if bb_option is not None and bb_option not in behind:
            behind.append(bb_option)
        behind.sort(key=lambda o: order.index(o.position))
        return behind
    return [o for o in live if order.index(o.position) > idx]


def players_left_to_act(snap: HandSnapshot) -> int | None:
    """我之后还有几个在局对手要说话（按当前下注状态）。

    未知位置返回 None（不要瞎猜成 0）。

    「身后还有人」直接决定能不能诈唬、要不要用中等牌控池：0 人时按当前
    下注状态没有其他人要先响应，但**不等于下注后没人能加注**。任何下注/
    加注都会重新打开行动，已经过牌/跟注/下注的人仍可再行动；具体还要看
    `behind_opponents()` 和对手画像里的 check-raise 频率。
    """
    order = _action_order(snap)
    if (order is None or snap.hero_position not in order
            or not _positions_known(snap, order)):
        return None
    return len(behind_opponents(snap))


def hero_in_position(snap: HandSnapshot) -> bool | None:
    """翻后按**座位顺序**我是否在最后。

    注意：这不等于「本轮当前最后行动」。前面已经过牌的人，在 Hero 下注后
    仍会再行动（可以 check-raise）；真正还有多少人要响应看
    `players_left_to_act()` / `behind_opponents()`。
    """
    order = _action_order(snap, street=FLOP)
    if (order is None or snap.hero_position not in order
            or not _positions_known(snap, order)):
        return None
    return not _after_hero(snap, street=FLOP)


def hero_first_to_act(snap: HandSnapshot) -> bool | None:
    """翻后我是不是**全场第一个**行动。

    `hero_in_position=False` 只表示「我不是最后一个」，中间位置也满足；
    旧文案把它写成「翻后先行动」，CO 在 BTN 之前、UTG 之后时会说错。
    """
    order = _action_order(snap, street=FLOP)
    hero = snap.hero_position
    if (order is None or hero not in order
            or not _positions_known(snap, order)):
        return None
    idx = order.index(hero)
    return all(order.index(o.position) > idx
               for o in snap.opponents
               if _can_act(o) and o.position in order)


def pot_type(snap: HandSnapshot) -> str | None:
    """底池类型：翻前动作线决定翻后的范围宽度与 SPR 语义。

    炸弹池没有翻前动作，`preflop_shapes` 会是 `unopened`；但「无人入池」
    与屏幕上的 BOMB POT 是两回事，模型会因此把强制前注池当普通未开池池。
    横幅/5BB 强制前注已经记在 `bomb_pot` 上，优先用它。
    """
    if snap.bomb_pot:
        return "bomb pot"
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
    """SPR = 有效筹码 ÷ 跟注前的可争夺底池（翻后才有意义）。

    分母与赔率同口径：先扣掉对手无人能跟的超额下注（`winnable_pot - call`），
    但不把「我的跟注」算进去 —— 与牌桌语言的 SPR 一致（94 / 10.5 = 9.0）。

    分子用「总筹码」而不是后手：对手下注 27.5BB、后手 24BB 时，这一手
    实际能打到的是 51.5BB（51.5 / 45 = 1.1），不是 24 / 45 = 0.5。
    任一在局对手筹码未读出时返回 None：忽略它会把短码局算成深码局。
    没有在局对手时也返回 None：单方面算出来的 SPR 是伪事实。
    """
    if snap.street == PREFLOP or not snap.pot or snap.hero_stack is None:
        return None
    if _unknown_stack_opponents(snap):
        return None
    pot_before_call = winnable_pot(snap) - call_amount(snap)
    if pot_before_call <= 0:
        pot_before_call = snap.pot
    hero = _hero_total_stack(snap)
    if hero is None:
        return None
    totals = [_opponent_total_stack(snap, o) for o in snap.opponents
              if not o.folded and o.stack is not None]
    totals = [t for t in totals if t is not None]
    if not totals:
        return None
    return round(min([*totals, hero]) / pot_before_call, 1)


# 听牌跟注的隐含赔率上限（离线审计口径）：Math gate 与单测共用同一个数。
# 2026-10-24 用户口径：隐含赔率按 0 算，只用即时赔率。
IMPLIED_ODDS_CAP_PCT = 0.0
# 权益与门槛差在这个范围内算「边界」，不给硬结论。
GATE_MARGIN_PCT = 2.0


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
    # 四二法则估的成牌/反超率（0-100）：花顺听 + 纯两高张的 6 张反超 outs。
    # 给提示词做「赔率 vs 胜率」比较用，但它是**成牌率**，不是对范围的河牌胜率。
    equity_pct: float | None = None
    # 人工维护的**现场观察**（根目录 .table_dy）。只写统计算不出来的东西：
    # 「3 号位上头了」「这桌有人合伙」。桌子的松紧/3-bet 频率由 table_stats 给数字。
    table_dynamics: str | None = None
    # 当前桌/全场先验统计。`scope` 决定提示词标题和口径。
    table_stats: "PooledStats | None" = None
    # 本手第几个 hero 决策点（0 起）。写进 advice 后可与 replay 的
    # DecisionPoint.action_index 对齐，解决「同一街多个决策点」无法配对。
    action_index: int = 0
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
    # 当前改良 outs（coach.Read.outs），含无花顺听时的两高张反超 outs。
    # 用于区分「成牌+弱听」和「成牌+强听」：4 张卡顺不该豁免「被统治」判定，
    # 8+ 张 combo draw 才该按半诈唬处理。两高张不是听牌（见 hand_tier）。
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
    # 牌力解析结论：把 hand_ranges 的 behind_any / equity 翻译成
    # 「大/中/小 + 程度 + 价值/听牌」。**只服务离线审计**：live 的
    # build_situation(include_strength=False) 不计算它，prompt 也不再渲染。
    # None = 未计算或没有 hand_ranges。
    hand_strength: "object | None" = None
    # 条件弃牌率：用本手收窄后的范围按权重算「他会不会弃」，并给出各尺寸的
    # 盈亏平衡弃牌率与 EV。见 fold_equity 模块。None = 翻前/无范围/已关闭。
    fold_eq: "object | None" = None
    # 翻后状态帧显示在局对手为 0、底池却有钱：事实自相矛盾。
    # 这种帧必须硬闸：不渲染筹码几何/牌力落后，只给过牌/弃牌。
    abnormal_no_opponents: bool = False

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
                    table_stats: "PooledStats | None" = None,
                    previous: "Advice | None" = None,
                    include_advanced: bool = False,
                    include_strength: bool = True
                    ) -> Situation:
    """把快照整理成一次决策的事实。

    `include_advanced=True` 才计算 GTO 近似表与 board equity（离线审计/实验
    用）。`include_strength=False` 时跳过 hand_strength：实时链路不再需要
    第 4 套牌力口径，离线审计在需要时自己懒计算。
    `table_dynamics` 是根目录 `.table_dy` 的解析文本（见 table_dynamics.py）。
    由调用方显式传入，而不是在这里读文件：单测和离线重放不会因为仓库里恰好
    有一份动态文件而改变结果。

    `opponent_reads` 是 ProfileStore.read_for() 的结果（按本手 seat_id）。
    没有画像时 `aggressive_table` 仍是 fallback，行为不会退化。
    """
    reads = tuple(opponent_reads or ())
    read = _read(snap)
    warnings: list[str] = list(snap.warnings)
    unknown_stacks = _unknown_stack_opponents(snap)
    if unknown_stacks:
        warnings.append(
            f"{len(unknown_stacks)} 名对手筹码未读出，有效筹码/SPR 未计算")
    if snap.pot is None or snap.pot <= 0 or snap.hero_stack is None:
        warnings.append("底池/后手未读全，已隐藏下注选项，只保留过牌/跟注/弃牌")
    n_opp = sum(1 for o in snap.opponents if not o.folded)
    anomaly = _no_opponent_anomaly(snap)
    # 翻后只剩 Hero、底池却很大：状态帧把对手全标成弃牌（0c2e 的
    # 22:07:59 帧）。这种事实会自相矛盾（「本手在局 1 人」却有人全下），
    # 必须让模型看到「翻后建议不可信」，而不是按单挑打。
    if anomaly:
        warnings.append("翻后状态帧显示在局对手为 0（数据异常，翻后建议不可信）")

    # 事实块只算一次：牌面结构、最好五张、精确排名、坚果校验全从这里出。
    # 实时建议和复盘共用同一个 GroundTruth，避免「一个说听牌没成、一个说成对」。
    gt = None
    if read is not None and not anomaly:
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
    beat_pct = beat_at_least = None
    if gt is not None:
        beat_pct = round(gt.rank.beat_pct * 100, 1)
        beat_at_least = round(gt.rank.beat_at_least_pct * 100, 1)
    elif read is not None and not anomaly:
        # 兜底：gt 计算失败时仍复用 read_hand 已经枚举过的单人比例
        p = read.beat_pct
        beat_pct = round(p * 100, 1)
        beat_at_least = round((1.0 - (1.0 - p) ** max(n_opp, 1)) * 100, 1)
    # 桌子的松紧优先用**算出来的**全场统计；没有统计（第一晚、新桌）才退回
    # `.table_dy` 的关键词。`.table_dy` 现在只写现场观察，关键词多数时候匹配
    # 不到，所以这里的先后顺序很关键。
    loose_flag = (table_stats.loose
                  if table_stats is not None and table_stats.loose is not None
                  else is_loose_table(table_dynamics))
    passive_flag = (table_stats.passive
                    if table_stats is not None
                    and table_stats.passive is not None
                    else is_passive_table(table_dynamics))
    sit = Situation(
        snap=snap,
        options=(_abnormal_options(snap) if anomaly
                 else build_options(snap,
                                    open_bonus=(LOOSE_OPEN_BONUS
                                                if loose_flag else 0.0))),
        hand_label=read.label if read else None,
        hand_tier=read.tier if read else None,
        category=read.category if read else None,
        texture=texture,
        # 两高张有反超 outs，但不是听牌：只有真正的花顺听才算 has_draw。
        has_draw=bool(read and read.tier in (STRONG_DRAW, WEAK_DRAW)
                      and read.outs >= 4),
        plays_board=bool(read and read.plays_board),
        opponent_strength=opp,
        warnings=warnings,
        hand_label_en=read.label_en if read else None,
        texture_en=_precise_texture_en(gt, texture),
        opponent_strength_en=opp_en,
        equity_pct=read.equity_pct if read else None,
        table_dynamics=(table_dynamics or "").strip() or None,
        table_stats=table_stats,
        action_index=(int(snap.decision_key[2])
                      if len(snap.decision_key) > 2 else 0),
        pot_per_player=per_player,
        explosive_pot=is_explosive_pot(snap),
        beat_pct=beat_pct,
        beat_at_least_pct=beat_at_least,
        opponents_in_hand=n_opp,
        # 翻前范围结论只在无人入池/溜入时渲染；面对加注不给 RFI 结论，
        # 否则模型会把「开池范围」当成「跟注/3-bet 范围」。
        range_verdict=(
            _range_conclusion_zh(snap, table_dynamics, loose_flag,
                                 passive_flag)
            if snap.street == PREFLOP and snap.preflop_shapes
            and set(snap.preflop_shapes) <= {"unopened", "limped"}
            else None),
        ground_truth=gt,
        aggressive_table=is_aggressive_table(table_dynamics),
        outs=read.outs if read else None,
        opponent_reads=reads,
        pot_type=pot_type(snap),
        spr=spr(snap),
        in_position=hero_in_position(snap),
        players_left_to_act=players_left_to_act(snap),
        previous=previous,
        abnormal_no_opponents=anomaly,
    )
    # GTO 表/board equity 是旧实验事实层，不再进入中文 lean 提示词。
    # 只有离线工具显式要 include_advanced=True 时才计算，且失败静默跳过。
    if include_advanced:
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
    if include_advanced:
        try:
            from . import board_equity
            sit = replace(sit, board_eq=board_equity.lookup(sit))
        except Exception:  # noqa: BLE001
            pass
    # 牌力解析：在 hand_ranges 之上再加一层纯判定，输出一行 Hand-strength read。
    # 依赖 hand_ranges；缺失时静默跳过，绝不猜。任何异常 fail open。
    # live 传 include_strength=False：这个结论只服务离线审计，不进 prompt。
    if include_strength:
        try:
            from . import hand_strength
            if hand_strength.enabled():
                sit = replace(sit,
                              hand_strength=hand_strength.compute_hand_strength(sit))
        except Exception:  # noqa: BLE001
            pass
    # 条件弃牌率：历史弃牌率是「全范围平均」，这手牌里对手已经用动作收窄了
    # 范围。这里用同一个收窄范围算加权弃牌率 + 各尺寸的盈亏平衡点。
    # 依赖 hand_ranges；缺失或出错时静默跳过（fail open）。
    try:
        from . import fold_equity
        report = fold_equity.lookup(sit)
        if report is not None:
            sit = replace(sit, fold_eq=report)
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
    report = getattr(sit, "fold_eq", None)
    if report is not None:
        # 有本手范围算出来的数就用数：多人池里「全部弃牌」是连乘，
        # 3 家各弃 40% 只剩 6%，2 家各弃 60% 还有 36%，不能用人数一刀切。
        return report.fold_equity < 0.15
    return (sit.street != PREFLOP
            and (sit.opponents_in_hand or 0) >= 3
            and sit.hand_tier in (STRONG_DRAW, WEAK_DRAW))


def value_directive(sit: Situation) -> str | None:
    """把「强牌默认拿价值」写成一句贴着当前分档的硬指令。

    **legacy offline only**：`build_messages` / `advise` 不再调用；只给
    tools/compare_review_hands 与 replay 审计历史决策用。

    为什么放在 user 事实里、而不是只写在 system 技能里：模型只做选择题，
    技能目录很长时容易只抓最近的一条「对手弱可以过牌」；一条紧跟
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
    """把本手在局对手的画像写进事实块。unknown 不输出，tentative 标注。

    渲染逻辑与 `opponent_profile.render_reads` 共用，避免两处格式漂移；
    这里只负责把快照里的座位映射成「在局 + 位置」。**legacy offline only**：
    中文 lean 用的是 `render_reads_zh`。
    """
    active = {o.seat_id: o for o in sit.snap.opponents if not o.folded}
    positions = {seat: (o.position or seat) for seat, o in active.items()}
    return render_reads(sit.opponent_reads, positions)


def render_hand_facts(sit: Situation) -> str:
    """英文旧事实块。**legacy offline only**：只给 tools/replay 审计历史用。

    实时链路已经改用 `render_facts_zh`；这里保留是为了复跑旧记录时仍能看到
    当时的英文事实 + Value directive / Math gate。
    """
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
        spr_text = "<0.1" if 0 < sit.spr < 0.1 else f"{sit.spr:.1f}"
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
        # 本桌规则：翻前任何加注不得低于 5.5BB。选项生成已经按这个下限抬过；
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
        if sit.fold_eq is not None:
            # 紧跟在「他的范围是什么」之后：范围决定他现在还剩什么牌，
            # 弃牌率决定这些牌里有多少会被打走，两件事必须连着读。
            lines.append("")
            lines.append(sit.fold_eq.render())
        if sit.hand_strength is not None:
            # 牌力解析是「主答案」：behind/equity 三个正交数合成的一行结论。
            # 位置刻意放在 Hand vs ranges 之后、Board structure 之前。
            lines.append(f"Hand-strength read: {sit.hand_strength.summary_en()}")
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


# ─────────────────── 离线审计（legacy offline only） ───────────────────

# 与 replay / compare_review_hands 共用的硬规则审计。新 prompt 不依赖这些标签，
# 它们只用于复盘时回答「这次下注是否与事实矛盾」。口径只看可证伪的事实：
# 牌力解析、坚果校验、牌面结构、赔率，不再读 Value directive。
_ONE_PAIR_TIERS = (OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM)


def audit_advice_tags(sit: Situation, kind: str | None,
                      amount: float | None) -> set[str]:
    """返回建议的违规标签集合（空集 = 没抓到硬错）。"""
    tags: set[str] = set()
    snap = sit.snap
    street = snap.street
    pos = (snap.hero_position or "").upper()
    cards = snap.hero_cards
    to_call = float(snap.to_call or 0.0)
    pot = float(snap.pot or 0.0)
    agg = kind in ("bet", "raise", "allin")
    tier = sit.hand_tier
    gt = sit.ground_truth
    hs = sit.hand_strength
    if hs is None and sit.hand_ranges is not None:
        # live 不再预计算 hand_strength；离线审计需要 kind/size 时这里懒计算。
        try:
            from . import hand_strength
            if hand_strength.enabled():
                hs = hand_strength.compute_hand_strength(sit)
        except Exception:  # noqa: BLE001 — 审计不能拖垮主流程
            hs = None
    hs_kind = getattr(hs, "kind", None)
    hs_size = getattr(hs, "size", None)
    multiway = (sit.opponents_in_hand or 0) >= 3

    # 翻后：事实说明这不是价值牌（空气/打牌面/多路听牌）却主动下注。
    if agg and (hs_kind in ("weak", "board_play")
                or (hs_kind == "draw" and multiway)):
        tags.add("非价值下注")
    if agg and sit.plays_board:
        if street != "flop" or (amount is not None and pot and amount > 0.5 * pot):
            tags.add("打牌面下注")
    # 非坚果/一对主动加注：只有坚果或牌面明显安全时才不算错。
    if kind == "raise" and tier == STRONG_MADE and gt is not None:
        if not gt.hero_is_nuts and (gt.board_info.paired
                                    or gt.board_info.flush_made_possible):
            tags.add("非坚果加注")
    if kind == "raise" and tier in _ONE_PAIR_TIERS:
        tags.add("一对加注")
    if agg and multiway and tier in (STRONG_DRAW, WEAK_DRAW):
        tags.add("多路听牌下注")

    # 翻前范围。
    if street == PREFLOP and len(cards) == 2:
        verdict = range_verdict(pos, cards)
        if verdict is not None:
            if pos not in ("SB", "BB") and verdict.in_range is False:
                if agg and to_call <= 1.0:
                    tags.add("翻前表外加注")
                if kind == "call" and to_call <= 1.0 and not _is_set_mine_pair(cards):
                    tags.add("翻前表外溜入")
            if (pos not in ("SB", "BB") and verdict.in_range is True
                    and kind == "fold" and to_call <= 1.0):
                tags.add("范围内却弃牌")
        if (pos == "SB" and kind == "call" and to_call <= 1.5
                and not sb_completable(cards)):
            tags.add("SB补盲垃圾牌")
        if (pos not in ("SB", "BB") and kind == "call" and to_call <= 1.0
                and tuple(snap.preflop_shapes) == ("unopened",)):
            tags.add("开池平跟")

    # 听牌在有即时赔率时弃牌。
    if (kind == "fold" and to_call > 0 and tier in (STRONG_DRAW, WEAK_DRAW)
            and sit.equity_pct is not None and pot > 0):
        owed = call_amount(snap)
        winnable = winnable_pot(snap)
        bep = owed / winnable * 100 if owed > 0 and winnable > 0 else 0.0
        if sit.equity_pct > bep + IMPLIED_ODDS_CAP_PCT:
            tags.add("有赔率却弃牌")

    # 强价值牌下注过小。
    if (street != PREFLOP and agg and amount is not None and pot > 0
            and hs_kind == "value" and hs_size in ("big", "very_big")
            and amount < 0.4 * pot):
        tags.add("价值下注过小")
    return tags


# ─────────────────────── 中文 lean 提示词 ───────────────────────

# 唯一保留的 system core。只给输出契约、事实优先级、决策顺序和指标口径；
# 前注局按局面条件追加，避免无前注桌也读一段无关规则。
SYSTEM_CORE_ZH = """你是一名德州扑克现金局实时教练。Hero 只有几秒钟，需要你从「可选动作」中选一个能直接执行的动作。

## 输出
只输出一个 JSON 对象（不要 Markdown/解释）：
{"option": "<id>", "reason": "<中文一句话，≤40 字>"}
需要置信度时（可选）：
{"option": "<id>", "reason": "...", "confidence": 0.7}
- option 必须严格来自「可选动作」，不得编造 id 或金额。
- id 映射（以「可选动作」为准）：
  翻前无人入池用 open-*/call（平跟、补盲、BB 过牌）；翻前面对加注用 raise-3x/raise-4x（3x/4x=上家加注的 3/4 倍；3-bet 池标签写 4-bet，id 不变）。
  翻后无人下注 call=过牌、raise-1/3…1.2=下注；翻后面对下注 call=跟注、raise-1/3…1.2=加注；全下 all-in。
- 不要自造 check/bet-*。
- reason 引用输入事实（牌力/赔率/位置/画像/SPR/底池类型/牌面）；不要引用未提供的规则、图表或概率。
- reason 不得与事实矛盾：无位置不能说「有位置」，没人行动过不能写「对手过牌」。
- 关键事实缺失时选最保守的合法动作并说明缺什么；保守=能过牌就过牌、面对下注缺牌力/赔率/筹码则弃、不主动下注/加注/诈唬。事实齐全时正常决策。

## 事实优先级
1. 硬事实：牌/公共牌/底池/需跟/在局人数/后手/位置/行动线/有效筹码/底池类型。
2. 程序计算：SPR、赔率门槛、牌型/坚果/听牌、范围权益、当前落后比例、听牌价格。
3. 逐座位对手画像；4. 当前牌桌统计/全场先验/人工观察；5. 通用经验。
冲突时高级覆盖低级、规则提示不能压硬事实；程序裁决（攻击/炸弹/听牌价格）优先于画像/桌况，人工观察是启发式。

## 决策顺序
- 翻前：看位置、前面行动（未开池/溜入/单加注/3-bet）、有效筹码和范围；不要因为“便宜”用垃圾牌入池。4-bet 只用 KK/AA，AK 只跟。
- 翻后：先问哪些更差的牌会跟、哪些更好的牌会加，再比权益与赔率门槛，决定价值/诈唬/过牌/弃牌。
- 听牌：按 outs、价格、弃牌率、公对面/成花风险判断；不要因为“当前未成牌不是坚果”就否决半诈唬。
- 多人池（含 Hero）：4 人以上顶对/超对降级，3 人池 SPR<3 可套池。无弃牌率别用听牌硬下注。
- 牌力：强牌=两对+/超对/顶对（踢脚 T+）；中等=顶对弱踢/中对/底对；空气=无成牌无听牌；4+ 人顶对按中等牌。
- 位置=翻后座位顺序：身后还有在局对手=无位置，没有=有位置；翻前行动顺序另看「翻前行动」，BB 可能还没说话。
- 下注/加注会重新打开行动：已过牌/跟注/下注的对手仍可再行动；「有位置」不等于「没人能加注」。
- 河牌：纯听牌等同空气，已成对的牌仍有摊牌价值；跟注看赔率/线路，价值下注看更差的牌会不会跟。非坚果不打三条街，被加注默认弃。
- 尺度只能从「可选动作」里选，金额由程序算好；深码不要用 all-in 代替正常尺度，后手 ≤20BB 的翻前推/弃区全下是标准动作。
- 炸弹池（强制前注、低 SPR）：顶对及以上总共只投一条街的钱（下注/跟注），之后过牌/弃牌、被下注即弃；低于顶对默认过牌/弃牌、不主动下注，只有「有位置+画像可读(疯子/高弃牌率)+赔率≥3:1」才跟一注。

## 指标口径
- 补牌成牌率（四二法则）= 听牌到河牌成牌概率，不是胜率；翻牌两街，只有全下/后面不再下注才能直接比单街门槛。
- 对范围权益 = 对建模范围（未建模对手按随机牌计入）的河牌胜率，假设后面不再下注；非全下时按后续下注折价再比门槛。
- 当前成牌落后 = 按当前五张至少一家领先，不是输牌概率/权益。
- 跟注看权益与门槛；下注看更差的牌会不会跟；不要用“当前落后”单独否决听牌，对成牌它也不是输牌概率。

## 筹码深度
- 有效筹码 = min(我,对手) 的「后手+本街已下注」；翻后套池/SPR 用它，不用自己的后手。
- 可玩深度 = min(我的后手, 最深未弃牌对手的后手)；翻前开池/单挑范围用它，不含本街已下注。
- SPR 高（>6）一对不做大底池；SPR 低（<3）单挑/3 人池顶对可套池，4 人以上降级；炸弹池按事实块几何。
- 20–40BB 仍是正常加注，只是翻后基本一枪定生死。
"""

# 只有桌况带前注时追加。前注局的核心差异是「底池更大、开池要收费」，
# 不需要把 9 人/0.5BB 这类具体牌桌写死进 system。
SYSTEM_ANTE_ZH = """## 前注局（前注 {ante}BB）
- 前注是死钱：底池更大、入池赔率更好；放宽主要在后位/BB，前位仍按位置范围，别因为“便宜”用垃圾牌入池。
- 开池尺度按底池放大；选项金额已算好，别用固定 3BB 质疑。
- 有前注不等于低 SPR：算出来 SPR<3 才是低 SPR；深码前注局仍按深码打。
"""


def build_system(sit: "Situation") -> str:
    """按局面拼 system：core 恒定，前注局只在有前注时追加。"""
    parts = [SYSTEM_CORE_ZH.rstrip()]
    blinds = sit.snap.blinds
    if blinds is not None and blinds.ante:
        parts.append(SYSTEM_ANTE_ZH.format(
            ante=_fmt_num(blinds.ante)).rstrip())
    return "\n\n".join(parts)

_STREET_ZH = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
_POT_TYPE_ZH = {
    "3-bet+ pot": "3-bet+ 池",
    "single-raised pot": "单加注池",
    "limped pot": "溜入池",
    "unopened pot": "无人入池",
    "bomb pot": "炸弹池（强制前注）",
}
_PREFLOP_SHAPE_ZH = {
    "unopened": "无人入池",
    "limped": "溜入",
    "single-raise": "单加注",
    "three-bet": "3-bet",
}
_SUIT_ZH = {"s": "黑桃", "h": "红桃", "d": "方块", "c": "梅花"}
_RANK_ZH = {"T": "10", "J": "J", "Q": "Q", "K": "K", "A": "A"}
_CATEGORY_EN_ZH = {
    "High card": "高牌", "One pair": "一对", "Two pair": "两对",
    "Trips": "三条", "Straight": "顺子", "Flush": "同花",
    "Full house": "葫芦", "Quads": "四条", "Straight flush": "同花顺",
}


def _fmt_num(value: float | None) -> str:
    """BB 数字：整数不带小数点，其余保留两位。"""
    if value is None:
        return "未知"
    value = float(value)
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value))}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _no_opponent_anomaly(snap: HandSnapshot) -> bool:
    """翻后状态帧把对手全标弃牌、底池却还有钱：事实自相矛盾。

    这种帧不能按「无对手」或「单挑」打：有效筹码、SPR、落后比例都没有意义，
    提示词和选项都必须硬闸，而不是只加一句「建议不可信」。
    """
    return (snap.street != PREFLOP
            and sum(1 for o in snap.opponents if not o.folded) == 0
            and (snap.pot or 0) > 0)


def _unknown_stack_opponents(snap: HandSnapshot) -> list:
    """在局但筹码没读出来的对手。有效筹码/SPR 必须把它们当成未知。"""
    return [o for o in snap.opponents if not o.folded and o.stack is None]


def _hero_total_stack(snap: HandSnapshot) -> float | None:
    """Hero 本街开始时的总筹码 = 后手 + 本街已下注。

    OCR 的 `stack` 是筹码槽（后手），`bet` 是本街下注槽。两手牌之间的
    「有效筹码」是**这一手还能投入的总量**，已经推进底池的钱也算数。
    """
    if snap.hero_stack is None:
        return None
    return float(snap.hero_stack) + float(snap.hero_bet or 0.0)


def _opponent_total_stack(snap: HandSnapshot, opponent) -> float | None:
    """对手本街开始时的总筹码 = 后手 + 本街已下注。

    只取后手会漏掉他已经推出去的钱：对手下注 27.5BB、后手 24BB 时，
    他的实际筹码是 51.5BB；按 24BB 算会把 SPR 从 1.1 压到 0.5，
    然后给出「有效筹码仅 24BB，全下」这种错建议（2026-10-30 a334）。
    """
    if opponent.stack is None:
        return None
    return float(opponent.stack) + _street_bet(snap, opponent)


def _effective_stack(snap: HandSnapshot) -> float | None:
    """有效筹码 = min(Hero 总筹码, 所有未弃牌对手的总筹码)。

    「总筹码」= 后手 + 本街已下注（见 `_opponent_total_stack`）：对手推进
    底池的那部分仍然是这一手能赢到的钱，不能只算后手。

    有对手筹码未读出时返回 None，而不是忽略它。忽略未知对手会把
    「300BB 对 100BB」算成 300BB，让深码建议出现在短码局。
    没有在局对手时也返回 None：不能把自己的后手当成「有效筹码」。
    """
    hero = _hero_total_stack(snap)
    if hero is None or _unknown_stack_opponents(snap):
        return None
    totals = [_opponent_total_stack(snap, o) for o in snap.opponents
              if not o.folded and o.stack is not None]
    totals = [t for t in totals if t is not None]
    return min([hero, *totals]) if totals else None


def _playable_depth(snap: HandSnapshot) -> float | None:
    """开池范围该按多深来算。

    不是 min(我, 所有对手)：桌上只要有**一个**短码，min 就把整桌判成浅码，
    而决定这手牌能打多深的是能跟注的最深那个对手。所以取
    min(我的后手, 最深的未弃牌对手)。
    """
    if snap.hero_stack is None:
        return None
    others = [o.stack for o in snap.opponents
              if not o.folded and o.stack is not None]
    if not others:
        return snap.hero_stack
    return min(snap.hero_stack, max(others))


def _range_conclusion_zh(snap: HandSnapshot,
                         table_dynamics: str | None,
                         loose: bool | None = None,
                         passive: bool | None = None) -> str | None:
    """翻前开池归属的一句话中文结论（只在无人入池/溜入时使用）。

    `preflop_ranges.Verdict.render()` 是英文长句；模型只需要「在/不在 +
    有溜入者时收紧后的归属」，这里只取结构化字段重新拼。
    """
    verdict = range_verdict(snap.hero_position, snap.hero_cards,
                            table_dynamics=table_dynamics,
                            limpers=count_limpers(snap),
                            stack_bb=_playable_depth(snap),
                            loose=loose, passive=passive)
    if verdict is None:
        return None
    if verdict.in_range is None:
        return f"{verdict.hand} 在 {verdict.position} 没有开池范围（盲注位不是开池点）"
    if verdict.in_range:
        text = (f"{verdict.hand} 在 {verdict.position} 的开池范围内"
                "（参考，不覆盖价格/对手/筹码）")
    else:
        text = (f"{verdict.hand} 不在 {verdict.position} 的开池范围内"
                "（参考，不覆盖价格/对手/筹码）")
    if verdict.limpers > 0 and verdict.isolation_ok is not None:
        iso = ("仍在收紧后的范围内" if verdict.isolation_ok
               else "已不在收紧后的范围内")
        text += (f"；前面有 {verdict.limpers} 个溜入者，"
                 f"按 {verdict.isolation_position} 的更紧范围看{iso}")
    if verdict.depth_note:
        text += f"（{verdict.depth_note}）"
    return text


def _action_zh(action: ActionRecord) -> str:
    if action.action == "check":
        return "过牌"
    if action.action == "fold":
        return "弃牌"
    if action.action == "call":
        return "跟注" + (f" {_fmt_num(action.amount)}BB"
                         if action.amount is not None else "")
    if action.action == "bet":
        return "下注" + (f" {_fmt_num(action.amount)}BB"
                         if action.amount is not None else "")
    if action.action == "raise":
        return "加注到" + (f" {_fmt_num(action.amount)}BB"
                           if action.amount is not None else " ?")
    if action.action == "allin":
        return "全下" + (f" {_fmt_num(action.amount)}BB"
                         if action.amount is not None else "")
    return action.action


def _fmt_actions_zh(actions: list[ActionRecord]) -> str:
    if not actions:
        return "（无行动）"
    parts = []
    for action in actions:
        who = ("Hero" if action.is_hero
               else (action.position or action.name or action.seat_id))
        tags = [name for name, on in (("全下", action.all_in),
                                      ("推断", action.inferred)) if on]
        extra = f"（{'、'.join(tags)}）" if tags else ""
        parts.append(f"{who} {_action_zh(action)}{extra}")
    return " → ".join(parts)


def _board_structure_zh(gt: GroundTruth) -> str:
    b = gt.board_info
    if b.flush_made_possible:
        flush = "成花面（同花已可能）"
    elif b.flush_draw_possible:
        flush = "花听面（未成花）"
    else:
        flush = "彩虹面（无同花可能）"
    straight = "顺子可能" if b.straight_possible else "顺子不可能"
    paired = "公对面：是" if b.paired else "公对面：否"
    return "｜".join((flush, straight, paired))


def _street_change_zh(gt: GroundTruth) -> str:
    """这一街新发的牌改变了什么（成花 / 公对面成三条 / 成对）。"""
    board = gt.board
    if len(board) < 4:
        return "无（翻牌没有上一街可比）"
    last, prev = board[-1], board[:-1]
    changes: list[str] = []
    suit = last[1]
    now = sum(1 for card in board if card[1] == suit)
    before = sum(1 for card in prev if card[1] == suit)
    if now >= 3 and before < 3:
        changes.append(f"{card_text(last)} 让牌面成花"
                       f"（{now} 张{_SUIT_ZH.get(suit, suit)}）")
    rank = last[0]
    same = sum(1 for card in board if card[0] == rank)
    before_same = sum(1 for card in prev if card[0] == rank)
    if same >= 4:
        changes.append(f"{card_text(last)} 让牌面出现四条")
    elif same == 3 and before_same == 2:
        changes.append(f"{card_text(last)} 让牌面出现三条")
    elif same == 2 and before_same == 1:
        changes.append(f"{card_text(last)} 让牌面成对")
    return "；".join(changes) + "。" if changes else "无"


def _draws_zh(gt: GroundTruth) -> str:
    d = gt.draws
    parts: list[str] = []
    if d.flush_draw:
        parts.append(("坚果同花听牌" if d.nut_flush_draw else "同花听牌")
                     + f"（{d.flush_suit}，{d.flush_outs} outs）")
    if d.straight_draw:
        kind = "两头顺/双卡顺听牌" if d.oesd else "卡顺听牌"
        parts.append(f"{kind}（{d.straight_outs} outs）")
    return "；".join(parts) if parts else "无"


def render_gt_warnings_zh(gt: GroundTruth | None,
                          draw_focus: bool = False) -> list[str]:
    """把 ground_truth 的英文警告映射成中文牌面提示。

    只保留**当前牌面特有**的风险（成花、公对、三条、顺子可能、花听）。
    「你不是坚果 / 这是听牌 / 河牌没有后续牌」这类通用句已经在
    「坚果」「牌力」「街道」里说过，再渲染只会重复并放大恐惧。

    `draw_focus` 保留参数兼容旧调用；听牌的通用警告现在一律不渲染。
    """
    if gt is None:
        return []
    low_signal = (
        "You are the ABSOLUTE NUTS", "You are the NUTS",
        "You are NOT the nuts", "This is a DRAW, not a made hand",
        "River: no more cards",
    )
    out: list[str] = []
    unknown = 0
    for warning in gt.warnings:
        if warning.startswith(low_signal):
            continue
        if warning.startswith("A flush is ALREADY possible"):
            text = "牌面已成花：同花打败当前成牌，不能当坚果"
        elif warning.startswith("The board is paired"):
            text = "牌面成对：葫芦或四条可能"
        elif warning.startswith("One of your two pairs is the BOARD pair"):
            # 公对只是两对里的一对；另一对如果是自己的口袋对/手牌对，
            # 仍是真两对，能拿价值。旧文案一律写「抓诈牌」，f2f 的
            # 66 在 99T 转牌就被劝成过牌。
            hero_ranks = {r for r, _ in (gt.hero if gt is not None else ())}
            counts: dict[int, int] = {}
            for rank, _suit in (gt.made.best_five if gt is not None else ()):
                counts[rank] = counts.get(rank, 0) + 1
            pair_ranks = {r for r, n in counts.items() if n >= 2}
            # 两对里只要有一对用到了 Hero 的底牌（口袋对或手牌对），
            # 就不是纯「打牌面」；只有两对都来自公面才是抓诈牌。
            own_pair = any(r in hero_ranks for r in pair_ranks)
            text = ("两对里有一对来自公对面；另一对用到了你的底牌，仍是真两对，"
                    "可以下注拿价值，但别当坚果（更大口袋对/顶对能压过）"
                    if own_pair else
                    "两对都来自公面/你只共享公对，这是抓诈牌")
        elif warning.startswith("There are trips on the board"):
            text = "牌面有三条：四条可能"
        elif warning.startswith("An opponent can complete a straight"):
            text = "对手可以用两张牌补成顺子"
        elif warning.startswith("At most 2 of any suit are on the river board"):
            text = "河牌最多两张同花：不存在同花"
        elif warning.startswith("Only a flush DRAW is possible"):
            text = "牌面最多两张同花：只有花听，没有成花"
        elif warning.startswith("You are the nuts NOW"):
            text = "现在是坚果，但后续第三张同花可以成花"
        elif warning.startswith("Your flush draw is on a paired board"):
            text = "花听在公对面上：葫芦可能已经存在，中花未必是坚果"
        else:
            unknown += 1
            continue
        out.append(text)
    if out:
        # 去重但保留顺序：同一条牌面风险只提醒一次。
        out = list(dict.fromkeys(out))
    if unknown:
        out.append(f"另有 {unknown} 条牌面提示（技术细节见日志）")
    return out


def _gap_zh(text: str) -> str:
    """把数据缺口翻成一句可读中文；未知英文告警不再原样塞进 prompt。"""
    text = (text or "").strip()
    if not text:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text
    if "range unknown" in text or "range not found" in text:
        return "某座位范围未知"
    if "ranges unavailable" in text or "contradict" in text:
        return "翻前动作与形态矛盾，范围不可用"
    if "only the first" in text and "modeled" in text:
        return "只建模前 4 名对手；其余范围未知"
    if "bet size without an action badge" in text:
        return "有下注额但没抓到动作徽章，范围按更宽处理"
    if "no action this street" in text:
        return "本街行动线缺失，范围按未行动处理（不是已过牌）"
    if "no range table" in text or "4-bet+ pot" in text:
        return "4-bet+ 池没有范围表，范围未知"
    if "inferred action" in text:
        return "部分动作是按底池对账补出的，范围的事件顺序为近似"
    if "board incomplete" in text:
        return "牌面缺张，该街动作未计入范围"
    if "pot at street start unread" in text:
        return "街前底池未读全，范围 SPR 为近似值"
    # 未知技术告警对模型没有可执行信息（它看不到日志），不再占一行。
    return ""


def _summarize_gaps(gaps: list[str], *, has_range_equity: bool = False) -> list[str]:
    """把逐座位/技术告警压成 1–4 条中文，避免 5 行同义的 range unknown。"""
    ordered = [g for g in dict.fromkeys(gaps) if g]
    range_unknown = sum(1 for g in ordered if g == "某座位范围未知")
    rest = [g for g in ordered if g != "某座位范围未知"]
    out: list[str] = []
    if range_unknown:
        tail = ("按随机牌计入权益" if has_range_equity
                else "未计入范围权益（没有可用范围）")
        out.append(f"{range_unknown} 名对手范围未知，{tail}")
    # 硬事实缺口（底池/后手/筹码未读全、0 对手异常）优先于普通技术告警，
    # 不能被 cap 挤掉。
    hard = [g for g in rest
            if "未读全" in g or "筹码未读出" in g or "状态帧" in g]
    unacted = [g for g in rest if "本街行动线缺失" in g]
    out.extend(hard)
    out.extend(unacted)
    out.extend(g for g in rest if g not in hard and g not in unacted)
    return out[:4]

# 这些 note 只是解释动作本身，标签已经说清楚；写两遍只会占 token。
_GENERIC_OPTION_NOTES = {
    "还没人下注，可以过牌",
    "剩余后手全部投入",
    "第一个入池通常应该加注",
    "对手下注已覆盖我的后手，跟注即全下",
}


def _option_note(option: Option) -> str:
    """只在 note 提供约束信息时附加；重复/废话 note 丢掉。"""
    note = (option.note or "").strip()
    if not note or note in option.label:
        return ""
    if note in _GENERIC_OPTION_NOTES:
        return ""
    # 标签已经有「（本桌最低加注）」，再补一句「已抬到本桌最低加注」是重复。
    if "（" in option.label and "最低加注" in note:
        return ""
    return f"；{note}" if "）" in option.label else f"（{note}）"


def render_options_zh(sit: Situation) -> list[str]:
    """「可选动作」：`- id｜中文标签`，只保留有约束意义的 note。

    后手不足时五个档位会封顶成同一个金额；prompt 里只显示一个，避免
    「五个 id、同一个动作」的伪选择。`sit.options` 仍保留全部 id 供点击器
    和 preset 映射使用。
    """
    lines = ["## 可选动作"]
    seen: set[tuple] = set()
    for option in sit.options:
        if option.amount is not None:
            key = (option.kind, option.amount)
            if key in seen:
                continue
            seen.add(key)
        lines.append(f"- {option.id}｜{option.label}{_option_note(option)}")
    return lines


_RANGE_MODE_ZH = {
    "call": "跟注范围",
    "bet": "下注范围",
    "check": "过牌范围",
    "raise": "加注范围",
    "unacted": "未行动范围",
}

# 牌型/分组名（hand_vs_ranges 用英文做内部键）→ 中文。同一张表同时服务
# “赢你的”和“你赢的”，避免两处各写一份翻译。
_RANGE_GROUP_ZH = {
    "quads": "四条",
    "straight flush": "同花顺",
    "full house": "葫芦",
    "flush": "同花",
    "straight": "顺子",
    "trips": "三条",
    "sets": "三条",
    "two pair": "两对",
    "overpair": "超对",
    "top pair": "顶对",
    "second pair": "中对",
    "middle pair": "中对",
    "bottom pair": "底对",
    "underpair": "低对",
    "board pair": "公对",
    "one pair": "一对",
    "high card / air": "空气",
    # hand_vs_ranges 的 ahead_groups 直接用 CATEGORY_NAMES_EN
    "high card": "空气",
}

_STREET_ZH_SHORT = {"flop": "翻牌", "turn": "转牌", "river": "河牌"}


def _event_action_zh(event) -> str:
    action = getattr(event, "action", "")
    if action == "check":
        return "过牌"
    if action == "call":
        return "跟注"
    if action == "bet":
        return "全下" if getattr(event, "all_in", False) else "下注"
    if action == "raise":
        return "加注全下" if getattr(event, "all_in", False) else "加注"
    return action


def _range_line_zh(events) -> str:
    """动作链 → 一行线路（设计 §6：线路是模型判断范围强弱最需要的事实）。

    只取最近三条动作，控制在一屏内；最后一条带上行动前上下文（donk 进几人 /
    面对下注+跟注 / 封口），这是判断「这个动作有多强」的关键。
    """
    events = tuple(events or ())
    if not events:
        return ""
    tokens: list[str] = []
    for event in events[-3:]:
        text = _event_action_zh(event)
        if getattr(event, "is_donk", False):
            text = "donk" + text
        tokens.append(f"{_STREET_ZH_SHORT.get(event.street, event.street)}{text}")
    last = events[-1]
    if getattr(last, "is_donk", False):
        behind = int(getattr(last, "players_behind", 0) or 0)
        tokens[-1] += f"进{behind}人"
    elif getattr(last, "facing_bet", False):
        callers = int(getattr(last, "n_callers", 0) or 0)
        behind = int(getattr(last, "players_behind", 0) or 0)
        if callers:
            tokens[-1] += f"（面对下注+{callers}跟注）"
        elif behind <= 0:
            tokens[-1] += "（封口）"
        else:
            tokens[-1] += f"（面对下注，身后{behind}人）"
    return "→".join(tokens)


def _range_group_zh(name: str | None) -> str:
    key = (name or "").strip().lower()
    return _RANGE_GROUP_ZH.get(key, name or "未知")


def _composition_zh(edge) -> str:
    """范围的成牌/听牌/空气分区。没有事件模型的数据时返回空串。"""
    comp = getattr(edge, "composition", None)
    if comp is None:
        return ""
    parts = [f"成牌 {comp.made_hand:.0%}"]
    # 分项只列成牌类型：空气已经单独写在后面，混进括号会变成
    # 「成牌 100%（一对 63%、空气 37%）」这种自相矛盾的句子。
    types = "、".join(f"{name} {share:.0%}" for name, share in comp.types
                      if name != "空气")
    if types:
        parts[0] += f"（{types}）"
    if comp.draw >= 0.05:
        parts.append(f"听牌 {comp.draw:.0%}")
    parts.append(f"空气 {comp.air:.0%}")
    return "；".join(parts)


def _range_short(edge) -> str:
    """最近两条动作的短标签：`翻牌过牌→转牌过牌`。

    第一行汇总用；逐家明细只写位置，避免同一个长标签在一屏里出现两遍。
    只写最后一条会把「连续过牌」压成「转牌过牌」——模型看不出这是第二次
    过牌，也就不会把顶对/两对+ 从范围里拿走。
    """
    events = tuple(getattr(edge, "events", ()) or ())
    if events:
        return _range_line_zh(events[-2:])
    return _RANGE_MODE_ZH.get(edge.mode or "", edge.mode or "")


def _trailing_checks(edge) -> int:
    """整条翻后线路都是过牌时，返回过牌次数；有任何下注/加注/跟注返回 0。

    只认「纯过牌」线路：过牌后再跟注/加注的范围已经由那个动作重新收窄，
    不能再用「连续过牌封顶」解释。
    """
    events = tuple(getattr(edge, "events", ()) or ())
    if not events:
        return 0
    if any(getattr(event, "action", "") != "check" for event in events):
        return 0
    return len(events)


def _range_cap_zh(edge, actual_opponents: int, behind_any: float,
                  equity: float) -> str:
    """对手连续过牌后的「范围封顶」事实：强牌剩多少、单家/全场各是多少。

    这一段直接回答用户 2026-11-01 的漏：只看「权益 21%/32%」会把连续
    过牌的中等成牌看扁。把「两对+ / 顶对」的残量、「单家你领先多少」和
    「N 家至少一家领先你」并排写出来，模型才不会把单家领先当成全场领先。
    """
    checks = _trailing_checks(edge)
    comp = getattr(edge, "composition", None)
    if checks <= 0 or comp is None:
        return ""
    lead = max(0.0, 1.0 - edge.behind - edge.tie)
    label = "连续过牌封顶" if checks >= 2 else "过牌后范围收窄"
    text = (f"- {label}：两对+ {comp.two_pair_plus:.0%}、"
            f"顶对 {comp.top_pair:.0%}；")
    if actual_opponents <= 1:
        text += f"你领先 {lead:.0%}"
    else:
        text += (f"单家范围你领先 {lead:.0%}，但 {actual_opponents} 家"
                 f"至少一家领先你 {behind_any:.0%}")
    if actual_opponents > 0 and equity > 0:
        fair = 1.0 / (actual_opponents + 1)
        text += (f"；权益 {equity:.0%} 是公平份额 {fair:.1%} 的 "
                 f"{equity / fair:.1f} 倍")
    return text


def _range_label(edge) -> str:
    """`BTN [check]` → `BTN 过牌范围`；内部 range id / combo 数不进 prompt。"""
    position = edge.position or edge.seat_id
    return f"{position} {_range_short(edge)}".strip()


def render_ranges_zh(sit: Situation) -> list[str]:
    """`Hand vs ranges` 的中文事实：线路 + 宽度 + 分区 + 谁赢我。

    设计 §6 的要求：把**线路**写出来（前置位 donk 进 2 人），把**分区**写出来
    （成牌/听牌/空气），再按牌型列「你领先的 / 赢你的」——避免模型把
    「领先 76%」误读成「76% 会推」。range id、combo 数、mode 英文串是内部
    实现，留在 JSONL，不进 prompt。
    """
    hr = sit.hand_ranges
    if hr is None:
        return []
    modeled = len(hr.opponents)
    random_fill = getattr(hr, "random_opponents", 0)
    actual = (sit.opponents_in_hand
              if sit.opponents_in_hand is not None
              else sit.snap.opponents_in_hand)
    shown = hr.opponents
    more = 0
    if len(shown) > 3:
        shown = tuple(sorted(shown, key=lambda e: -e.behind)[:3])
        more = len(hr.opponents) - 3

    def width(edge) -> str:
        eff = getattr(edge, "eff_width_pct", 0.0) or edge.width_pct
        return f"≈{eff:.1f}%"

    ranges = "｜".join(f"{_range_label(o)} {width(o)}" for o in shown)
    if more:
        ranges += f"｜另有 {more} 家只计入权益"
    lines = [f"范围：{ranges}" if ranges else "范围：全部按随机牌计入"]

    # 「本街还没动作」只在**整手都没有翻后动作**时才提；事件模型下范围是
    # 上一街收窄后的状态，不是翻前先验。
    stale = [o for o in hr.opponents
             if getattr(o, "mode", None) == "unacted"
             and not getattr(o, "events", ())]
    if stale:
        lines.append("- 本街还没有动作：范围沿用翻前先验，不是已过牌")

    for edge in shown:
        composition = _composition_zh(edge)
        detail = composition + "；" if composition else ""
        position = edge.position or edge.seat_id
        lead = max(0.0, 1.0 - edge.behind - edge.tie)
        lines.append(f"- {position}：{detail}当前成牌领先我 "
                     f"{edge.behind:.1%}（平局 {edge.tie:.1%}；"
                     f"你领先 {lead:.1%}）")

    # 连续过牌是**强范围退场**的证据：把「两对+/顶对还剩多少」写成一行，
    # 并同时给出「单家领先」和「N 家至少一家领先」，避免模型把单家领先
    # 当成全场领先。只取一个代表（各座位同一线路时数值相同）。
    capped = [e for e in shown if _trailing_checks(e) > 0
              and getattr(e, "composition", None) is not None]
    if capped:
        rep = max(capped, key=_trailing_checks)
        lines.append(_range_cap_zh(rep, actual, hr.behind_any, hr.equity))

    # 按牌型列「你赢的 / 赢你的」，只在事件模型给出分区时才写：旧路径的
    # ahead_groups 只有类别名，混进去会让模型把「类别」当成「范围比例」。
    hit = [g for edge in shown
           for g in getattr(edge, "beaten_by", ()) if g.share >= 0.03]
    beat = [g for edge in shown
            for g in getattr(edge, "ahead_groups", ()) if g.share >= 0.03]
    if hit:
        names: dict[str, float] = {}
        for group in hit:
            zh = _range_group_zh(group.name)
            # 「空气压过我」只是踢脚比较（A 高 vs K 高），不是能打败我的牌型；
            # 写进去会让模型把“空气”当成威胁。
            if zh == "空气":
                continue
            names[zh] = max(names.get(zh, 0.0), group.share)
        ranked = sorted(names.items(), key=lambda kv: -kv[1])[:4]
        if ranked:
            lines.append("- 赢你的：" + "、".join(name for name, _ in ranked))
    if beat and any(getattr(e, "composition", None) for e in shown):
        names = dict.fromkeys(_range_group_zh(g.name) for g in beat)
        lines.append("- 你赢的：" + "、".join(list(names)[:4]))

    # 只在「面对全下」时给警告：这是最容易被当前成牌强度带偏的节点。
    jammers = [e for e in shown
               if getattr(e, "jam", False) and getattr(e, "composition", None)]
    facing_jam = bool(sit.snap.to_call and sit.snap.to_call > 0 and jammers)
    if facing_jam:
        if sit.snap.street == "river":
            # 只对**全下的人**说「你只赢诈唬」：还没行动的人范围宽是正常的，
            # 拿他的空气占比来算会让警告时有时无。
            # 用「空气」分区而不是三组里的 bluff：三组按牌型归属，底对不算诈唬。
            air = max(e.composition.air for e in jammers)
            behind = max(e.behind for e in jammers)
            if behind >= 0.80:
                if air < 0.02:
                    lines.append(
                        f"- 范围警告：河牌面对全下，这个范围里 {behind:.0%} 压过你，"
                        "几乎没有诈唬；不要拿「我现在有牌」当跟注理由，"
                        "按权益和价格判断")
                else:
                    lines.append(
                        f"- 范围警告：河牌面对全下，{behind:.0%} 压过你，"
                        f"你只能赢那 {air:.0%} 的诈唬；不要拿「我现在有牌」"
                        "当跟注理由，按权益和价格判断")
        else:
            semi = max(e.composition.draw for e in jammers)
            if semi >= 0.10:
                lines.append(
                    f"- 范围警告：全下范围里听牌约 {semi:.0%}，"
                    "权益要按剩余牌发完算，不能只看「他现在还没成牌」")

    if random_fill:
        lines.append(f"- 范围建模 {modeled} 家；其余 {random_fill} 家"
                     "按随机牌计入权益，不展开逐家范围")
    elif modeled < actual:
        lines.append(f"- 建模 {modeled} 家，实际 {actual} 家；"
                     "未建模对手未计入范围权益")
    return lines


def render_hand_strength_zh(sit: Situation) -> list[str]:
    """「实时牌力」中文事实块；翻前返回空列表。

    顺序按决策相关性排：我有什么 → 坚果/听牌/成牌率 → 对范围强弱 →
    牌面会怎么变 → 牌面警告。听牌永远按「听牌」渲染，不降级成 VERY SMALL。
    """
    snap = sit.snap
    if snap.street == PREFLOP:
        return []
    if sit.abnormal_no_opponents:
        # 0 对手时 gt/落后比例是按假想对手算的；整块不渲染，交给数据质量段。
        return []
    lines = ["## 实时牌力"]
    tier = sit.hand_tier
    is_draw = tier in (STRONG_DRAW, WEAK_DRAW)
    # 「有牌又有听」：对子类成牌 + ≥8 outs 的强听。它不是纯听牌，
    # 多人池里不能按「没有弃牌率就过牌」处理。
    combo_draw = (not is_draw and sit.outs is not None and sit.outs >= 8
                  and tier in (OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM))

    if is_draw:
        kind = "强听牌" if tier == STRONG_DRAW else "弱听牌"
        outs = sit.outs or (sit.ground_truth.draws.total_outs
                            if sit.ground_truth is not None else None)
        lines.append(f"牌力：{kind}" + (f"（{outs} outs）" if outs else ""))
    elif tier == "打牌面":
        # 「打牌面」在本项目指「最好的五张全靠公共牌」；公对/公三条时高张
        # 踢脚还可能参与比牌，但公顺/公花/公葫芦/公四条是整手牌都在牌面，
        # 踢脚不再参与。0c2e 的 AK 在 442 上仍有 A/K 踢脚，不能当空气；
        # 但公顺面说「踢脚参与比牌」同样是错的。
        made_gt = sit.ground_truth
        if made_gt is not None and made_gt.made.category >= STRAIGHT:
            lines.append("牌力：打牌面（公共牌自己成顺/花/葫芦/四条，人人都有；"
                         "只能用更高的同类型牌或听牌反超，踢脚不参与比牌）")
        else:
            lines.append("牌力：打牌面（只共享公对/公三条，没有自己的成牌；"
                         "高张/踢脚仍参与比牌，不等于空气）")
    elif tier == "空气":
        if sit.outs:
            lines.append("牌力：空气（无成牌、无听牌；有高张反超 outs）")
        else:
            lines.append("牌力：空气（无成牌、无听牌）")
    elif sit.hand_label:
        # 只给牌型标签；「大 / 中 / 小」是 hand_strength 的第 4 套口径，
        # 已经由 hand_tier / ground_truth / hand_ranges 的数字覆盖，不再重复。
        lines.append(f"牌力：{sit.hand_label}")

    gt = sit.ground_truth
    if gt is not None:
        made = gt.made.name_cn
        if gt.made.category == HIGH_CARD:
            rank = card_text(gt.made.best_five[0])[0] if gt.made.best_five else ""
            made = f"{_RANK_ZH.get(rank, rank)} 高" if rank else made
        if is_draw:
            made += "（未成牌）"
        elif sit.plays_board:
            made += "（打牌面）"
        lines.append(f"当前牌型：{made}")
        best = " ".join(card_text(c) for c in gt.made.best_five)
        lines.append(f"最好五张：{best}")
        if gt.hero_is_nuts:
            nuts = "是（没有任何对手持牌能打败你）"
            if gt.rank.tie_pct > 0:
                nuts += f"；平局 {gt.rank.tie_pct:.1%} 分池"
        elif is_draw:
            nuts = ("否（指当前成牌；听牌不代表最终输；"
                    "更大牌型存在，但不代表对手一定持有）")
        else:
            nuts = ("否（指当前成牌；更大牌型存在，"
                    "但不代表对手一定持有）")
        lines.append(f"坚果：{nuts}")
        if not gt.hero_is_nuts:
            stronger = []
            for beaten in gt.rank.beaten_by:
                if beaten.category == ONE_PAIR:
                    name = ("更大的对子（一对）"
                            if gt.made.category == ONE_PAIR else "一对")
                else:
                    name = _CATEGORY_EN_ZH.get(beaten.name, beaten.name)
                stronger.append(name)
            if stronger:
                lines.append("能打败你的牌型（理论上）："
                             + "、".join(stronger))
        lines.append(f"听牌：{_draws_zh(gt)}")
        if gt.draws.overcard_outs:
            kind = ("两高张" if gt.draws.overcard_outs >= 6 else "单高张")
            lines.append(f"反超 outs：{kind} {gt.draws.overcard_outs} 张")
        if combo_draw:
            combo_tail = ("只有范围权益高于公平份额时，才提高下注频率"
                          if sit.hand_ranges is not None
                          else "没有范围权益时不要假设自己领先")
            lines.append(
                "有牌又有听：成牌 + 强听（combo draw，不是纯听牌）；"
                "可作为保护/半诈唬下注，不是默认；多人池或没有弃牌率时优先过牌。"
                + combo_tail)
        if gt.draws.total_outs:
            # 纯两高张不是「补牌成牌」，是反超当前顶对的权益；分开写，
            # 免得模型把 24% 当成「已经能跟任何下注」的胜率。
            overcard_only = (gt.draws.overcard_outs
                             and not gt.draws.flush_draw
                             and not gt.draws.straight_draw)
            label = "反超成牌率" if overcard_only else "补牌成牌率"
            basis = ("按高张 outs 估，不是对范围的河牌胜率"
                     if overcard_only else "仅全下/无后续下注时可直接比门槛")
            lines.append(f"{label}（四二法则，到河牌；{basis}）："
                         f"{gt.draws.equity_pct:.0f}%")

    hr = sit.hand_ranges
    if hr is None and gt is not None:
        behind = (f"当前成牌落后至少一家："
                  f"{gt.rank.beat_at_least_pct:.1%}（按随机牌下界")
        if is_draw:
            behind += "；当前未成牌，不代表最终胜率"
        else:
            behind += ("；这是当前五张被至少一家压过的概率，"
                       "不是输牌概率/权益")
        lines.append(behind + "）")
    if hr is not None:
        modeled_players = len(hr.opponents) + 1
        random_fill = getattr(hr, "random_opponents", 0)
        total_players = modeled_players + random_fill
        opponents = (sit.opponents_in_hand
                     if sit.opponents_in_hand is not None
                     else snap.opponents_in_hand)
        actual_players = opponents + 1
        fair_share = 1.0 / actual_players if actual_players > 0 else 0.0
        equity_note = f"按河牌、未计后续下注，{total_players} 人池"
        unacted = [o for o in hr.opponents
                   if getattr(o, "mode", None) == "unacted"
                   and not getattr(o, "events", ())]
        if unacted:
            equity_note += "；对手本街未行动，按沿用翻前范围估算"
        elif any(getattr(o, "mode", None) == "unacted" for o in hr.opponents):
            # 事件模型：本街没动作 != 范围回到翻前——上一街收窄的信息还在。
            equity_note += "；对手本街未行动，沿用上一街收窄后的范围"
        if random_fill:
            equity_note += (f"；范围建模 {len(hr.opponents)} 家 + "
                            f"随机牌 {random_fill} 家")
        elif actual_players > modeled_players:
            equity_note += f"；按建模 {len(hr.opponents)} 名对手"
        if total_players != actual_players:
            equity_note += f"；实际 {actual_players} 人"
        # 权益已经包含随机牌时，拿它对实际人数公平份额做倍数才有意义；
        # 两边覆盖人数不一致时宁可不给倍数，避免再次出现「41% × 8 人池」
        # 这种分子/分母不是同一批人的算法。
        if (combo_draw and fair_share > 0
                and total_players == actual_players):
            equity_note += (f"；公平份额 {fair_share:.1%}，"
                            f"权益是它的 {hr.equity / fair_share:.1f} 倍")
        lines.append(f"对范围权益：{hr.equity:.0%}（{equity_note}）")
        behind = f"当前成牌落后至少一家：{hr.behind_any:.1%}"
        extra = []
        if random_fill:
            extra.append(f"含随机牌 {random_fill} 家")
        if is_draw:
            extra.append("当前未成牌，不代表最终胜率")
        else:
            extra.append("当前五张被至少一家压过的概率，"
                         "不是输牌概率/权益")
        lines.append(behind + "（" + "；".join(extra) + "）")
        if actual_players == 2:
            pool_label = "单挑"
        elif actual_players == 3:
            pool_label = "3 人池"
        else:
            pool_label = "多人池"
        multiway = (f"{pool_label}：实际 {actual_players} 人 / "
                    f"范围建模 {len(hr.opponents)} 名对手")
        if random_fill:
            multiway += f" / 随机牌 {random_fill} 家"
        multiway += f" / 公平份额 {fair_share:.1%}"
        lines.append(multiway)
        lines.extend(render_ranges_zh(sit))
    elif gt is None and sit.equity_pct is not None and is_draw:
        lines.append("补牌成牌率（四二法则，到河牌；"
                     "仅全下/无后续下注时可直接比门槛）："
                     f"{sit.equity_pct:.0f}%")

    # 听牌的即时价格：SPR ≤ 2 时跟注/全下基本就是最后一手，四二法则可以
    # 直接和门槛比。模型看到「32% vs 33.3%」比看到两个孤立的数字更不容易
    # 把「筹码不多，推了」当成理由（a334 的全下就是这么来的）。
    if is_draw and snap.to_call and snap.to_call > 0 and (sit.spr or 99) <= 2.0:
        owed = call_amount(snap)
        winnable = winnable_pot(snap)
        eq = sit.equity_pct
        if eq is None and gt is not None and gt.draws.total_outs:
            eq = gt.draws.equity_pct
        if eq is not None and owed > 0 and winnable > 0:
            bep = owed / winnable * 100
            # 四二法则是「到河牌」的两街成牌率，只有跟注即全下、或所有
            # 对手都已全下（后面不再下注）时，才能和单街门槛直接比。
            call_is_all_in = (snap.hero_stack is not None
                              and owed >= snap.hero_stack - 1e-9)
            no_more_betting = not any(_can_act(o) for o in snap.opponents)
            direct = call_is_all_in or no_more_betting
            base = (f"听牌价格：跟注 {_fmt_num(owed)}BB / 可争夺底池 "
                    f"{_fmt_num(winnable)}BB = {bep:.0f}% 门槛；"
                    f"四二法则 {eq:.0f}%")
            if direct:
                verdict = "够" if eq >= bep else "直接赔率不够"
                lines.append(base + f" → {verdict}（跟注即全下/后面不再下注）")
            else:
                lines.append(
                    base + "；参考值，不可直接与单街门槛比较"
                    "（未全下，后面还可能下注）")

    if gt is not None:
        lines.append(f"牌面结构：{_board_structure_zh(gt)}")
        lines.append(f"转河变化：{_street_change_zh(gt)}")
        warnings = [w.rstrip("。；")
                    for w in render_gt_warnings_zh(gt, draw_focus=is_draw)]
        lines.append("牌面警告：" + ("；".join(warnings) + "。" if warnings else "无"))

    # 只有标题、没有任何事实时整段省略。
    return lines if len(lines) > 1 else []


# 面对加注时还能继续的强牌：不管多人池多紧，这些牌都不该被范围结论劝退。
_STRONG_VS_RAISE_TOKENS = ("99+", "AQs+", "AKs", "AKo", "KQs", "AQo")
# 面对 4-bet 只剩价值 4-bet/跟注范围。
_FOUR_BET_CONTINUE_TOKENS = ("QQ+", "AKs", "AKo")


def _range_width_pct(keys: set) -> float:
    """169 键集合的组合宽度（分母 1326）。仅用于事实行里的「范围有多宽」。"""
    total = 0
    for key in keys:
        total += 6 if len(key) == 2 else (4 if key[2] == "s" else 12)
    return total / 1326 * 100


def _in_keys_range(key: str, rng) -> bool | None:
    """key 是否在 combo 级范围里；范围缺失返回 None（不猜）。"""
    if rng is None:
        return None
    from .ranges import combos_of_key
    combos = combos_of_key(key)
    return bool(combos) and any(c in rng.combo_weights for c in combos)


def _hand_shape_zh(key: str) -> str:
    """手牌结构的一句话，用来解释「为什么这手牌跟不动」。"""
    if len(key) == 2:
        return f"（口袋对 {key}）"
    high, low, kind = key[0], key[1], key[2]
    if kind == "s":
        return f"（{key} 同花）"
    from .preflop_ranges import RANKS
    broadway = (RANKS.index(high) >= RANKS.index("T")
                and RANKS.index(low) >= RANKS.index("T"))
    if broadway:
        return f"（{key} 非同花双高张、踢脚 {low}）"
    if high == "A":
        return f"（{key} A 高、踢脚 {low} 的非同花）"
    return f"（{key} 非同花、踢脚 {low}）"


def _facing_raise_zh(sit: Situation) -> list[str]:
    """翻前面对加注：这手牌在不在跟注/继续范围里，以及为什么。

    旧提示词只在「无人入池/溜入」时给范围结论，面对加注完全不给 ——
    模型只能抓「赔率门槛 21.6%」这类价格事实，于是拿 K8o/QTo 的便宜赔率
    当理由跟注。价格不等于范围：被支配的非同花高张翻中一对常常是第二好，
    直接赔率兑现不了。这里把范围归属、被支配结构、多人池/没收尾的降级
    一起算成事实，模型只负责选动作。
    """
    snap = sit.snap
    if snap.street != PREFLOP or len(snap.hero_cards) != 2:
        return []
    shapes = set(snap.preflop_shapes or ())
    if not shapes & {"single-raise", "three-bet"}:
        return []
    hero_pos = (snap.hero_position or "").strip()
    if not hero_pos:
        return []
    acts = list(snap.actions_of(PREFLOP))
    raises = [a for a in acts
              if a.action in ("raise", "bet")
              or (a.action == "call" and a.all_in)]
    if not raises:
        return []          # 形状说有加注、动作线却没记到：不猜

    from . import ranges as range_mod
    from .field_ranges import bucket
    from .preflop_ranges import hand_key

    bb = snap.blinds.bb if snap.blinds else 1.0
    key = hand_key(snap.hero_cards)
    last = raises[-1]
    raise_pos = last.position or (hero_pos if last.is_hero else None)
    if not raise_pos:
        return []       # 加注者位置读不出来：范围先验无锚点，不猜
    raise_amt = last.amount
    n_raises = len(raises)

    # 加注前的溜入者、加注后的跟注者。只认动作线，不靠下注额猜。
    first_idx = next(i for i, a in enumerate(acts) if a is raises[0])
    last_idx = next(i for i, a in enumerate(acts) if a is last)
    before, after = acts[:first_idx], acts[last_idx + 1:]
    hero_limped = any(a.is_hero and a.action == "call"
                      and float(a.amount or 0) <= bb + 1e-9 for a in before)
    other_limpers = sum(
        1 for a in before
        if not a.is_hero and a.action == "call"
        and float(a.amount or 0) <= bb + 1e-9)
    limpers = other_limpers + (1 if hero_limped else 0)
    callers = sum(1 for a in after if a.action == "call")
    behind = behind_opponents(snap)
    tight_reasons: list[str] = []
    if limpers:
        tight_reasons.append(
            f"{limpers} 人溜入（含你）" if hero_limped
            else f"{limpers} 个溜入者")
    if callers:
        tight_reasons.append(f"{callers} 人跟注这次加注")
    if behind:
        tight_reasons.append(f"{len(behind)} 人还没响应加注")
    multiway = bool(callers or limpers or behind)

    # ── 选「继续/跟注」的参照范围 ──
    strong = range_mod.expand_tokens(_STRONG_VS_RAISE_TOKENS)
    tightened = False
    if n_raises >= 3:
        kind = "4-bet+"
        label = "面对 4-bet 的继续范围（QQ+/AK）"
        continue_keys = range_mod.expand_tokens(_FOUR_BET_CONTINUE_TOKENS)
        in_range = key in continue_keys
        width = _range_width_pct(continue_keys)
    elif n_raises == 2:
        kind = "3-bet"
        label = "面对 3-bet 的继续范围（TT+、AQs+、AK）"
        rng = range_mod.get("3BET_CALL_VS_MP")
        in_range = _in_keys_range(key, rng)
        width = rng.width_pct if rng is not None else None
    else:
        kind = "单加注"
        order = POSITIONS_BY_TABLE_SIZE.get(snap.table_size or 0)
        hero_ip = None
        if order and raise_pos in order and hero_pos in order:
            hero_ip = order.index(hero_pos) > order.index(raise_pos)
        if hero_pos == BB:
            base_label = "BB 单加注宽防守范围"
        elif hero_pos == SB:
            base_label = "SB 无位置跟注范围"
        else:
            base_label = (f"{hero_pos} 有位置冷跟范围" if hero_ip
                          else f"{hero_pos} 无位置冷跟范围")
        base = range_mod.get(
            f"BB_DEF_VS_{bucket(raise_pos)}" if hero_pos == BB
            else f"COLD_CALL_{'IP' if hero_ip else 'OOP'}_vs_{bucket(raise_pos)}")
        base_in = _in_keys_range(key, base)
        width = base.width_pct if base is not None else None
        label = base_label
        if key in strong:
            in_range = True
        elif hero_pos == BB and multiway:
            # BB 的「宽防守」只在收尾的单挑池里成立；有溜入者/跟注者、
            # 还有人没响应时必须收紧到无位置冷跟范围。
            tight = range_mod.get(f"COLD_CALL_OOP_vs_{bucket(raise_pos)}")
            in_range = _in_keys_range(key, tight)
            tightened = True
            label = "多人池紧防守范围"
            width = tight.width_pct if tight is not None else None
        else:
            in_range = base_in

    # ── 事实行 ──
    verb = ("3-bet 到" if n_raises == 2
            else "再加注到" if n_raises >= 3 else "加注到")
    amount_text = f" {_fmt_num(raise_amt)}BB" if raise_amt else ""
    spot = f"{kind}，{raise_pos or '对手'} {verb}{amount_text}"
    detail = []
    if other_limpers:
        detail.append(f"{other_limpers} 个溜入者之后")
    if hero_limped:
        detail.append("你已溜入 1BB")
    if detail:
        spot += "（" + "；".join(detail) + "）"
    if behind:
        if len(behind) > 3:
            spot += f"；身后还有 {len(behind)} 人没响应加注"
        else:
            names = "、".join(o.position or o.seat_id for o in behind)
            spot += f"；身后的 {names} 还没响应加注"
    lines = [f"面对加注：{spot}"]

    if in_range is None:
        lines.append("对加注的牌力：范围表缺失（对手位置/先验不可用），"
                     "只能按牌力和价格自行判断")
        return lines
    shape = _hand_shape_zh(key)
    width_text = f"（{width:.0f}%）" if width else ""
    if in_range is False and tightened:
        lines.append(f"对加注的牌力：{key} 在单加注收尾的宽防守里；当前有 "
                     f"{'、'.join(tight_reasons)}，按{label}{width_text}不在内"
                     f"{shape} → 默认弃牌")
    elif in_range is False:
        lines.append(f"对加注的牌力：{key} 不在 {label}{width_text}内"
                     f"{shape} → 默认弃牌")
    elif tightened:
        lines.append(f"对加注的牌力：{key} 在 {label}{width_text}内{shape}"
                     f" → 可以跟注（多人池按边缘牌处理，别把顶对当坚果）")
    elif n_raises >= 2:
        # 2026-11-01：面对 3-bet 的 4-bet 只用 KK/AA；AK/QQ 只跟不加。
        # 之前这里对范围内所有牌都写「可以跟注或 4-bet」，AK 打光就是这条线。
        if key in ("AA", "KK"):
            lines.append(f"对加注的牌力：{key} 在 {label}{width_text}内"
                         " → 价值牌，可以 4-bet 或跟注")
        else:
            lines.append(f"对加注的牌力：{key} 在 {label}{width_text}内"
                         " → 可以跟注，不 4-bet（4-bet 只用 KK/AA）")
    elif key in range_mod.expand_tokens(("QQ+", "AKs", "AKo")):
        lines.append(f"对加注的牌力：{key} 在 {label}{width_text}内"
                     " → 可以 3-bet 或跟注")
    else:
        tail = " → 可以跟注或 3-bet" if key in strong else " → 可以跟注"
        lines.append(f"对加注的牌力：{key} 在 {label}{width_text}内{tail}")

    # 价格提醒：只在「不该跟」时出现，免得诱导模型用赔率给弱牌找理由。
    if in_range is False or tightened:
        owed = call_amount(snap)
        winnable = winnable_pot(snap)
        if owed > 0 and winnable > 0:
            lines.append(f"价格：{owed / winnable * 100:.0f}% 赔率只是直接价格，"
                         f"被支配牌实现不了这么多权益，不能单独当跟注理由")
    # 只有**实测**的松凶画像才能软化范围表结论；`.table_dy` 的关键词
    # （aggressive_table）只是现场形容词，不覆盖程序算出来的范围归属。
    aggressive_reads = [
        r for r in sit.opponent_reads
        if r.archetype in AGGRESSIVE_ARCHETYPES
        and r.gate in ("read", "stable")
    ]
    if aggressive_reads:
        names = [r.display_name or r.key for r in aggressive_reads]
        who = "、".join(names[:3])
        if len(names) > 3:
            # 只点 3 个名字会被读成「就这 3 家松凶」，表里其余松凶的座位
            # 反而被这句话藏掉了。
            who += f" 等 {len(names)} 家"
        lines.append(
            f"对手画像：{who}是松凶（实测，n≥{min(r.n for r in aggressive_reads)}）"
            "；范围表是基线，「默认弃牌」不是绝对，按对手实际加注宽度调整。")
    return lines


def _pfr_label_zh(snap: HandSnapshot) -> str:
    """最后翻前加注者的三态；`hero_is_pfr` 本身只是 bool，读不出「未知」。"""
    if snap.hero_is_pfr:
        return "是（Hero 是最后一个翻前加注者）"
    pre = snap.actions_of(PREFLOP)
    if not pre:
        return "未知（翻前动作线缺失）"
    if any(a.is_hero and a.action in ("raise", "bet") for a in pre):
        return "否（Hero 加注后被再加注）"
    return "否"


def render_preflop_zh(sit: Situation) -> list[str]:
    """「翻前信息」；只在翻前渲染。面对加注给继续范围，不给 RFI 结论。"""
    if sit.street != PREFLOP:
        return []
    snap = sit.snap
    lines = ["## 翻前信息"]
    shapes = [ _PREFLOP_SHAPE_ZH.get(s, s) for s in (snap.preflop_shapes or ())]
    lines.append("翻前形态：" + ("、".join(shapes) if shapes else "未知"))
    lines.append("最后翻前加注者（PFR）：" + _pfr_label_zh(snap))
    if sit.range_verdict:
        lines.append(f"翻前范围结论：{sit.range_verdict}")
    lines.extend(_facing_raise_zh(sit))
    limpers = count_limpers(snap)
    if limpers:
        lines.append(f"前面溜入人数：{limpers}")
    raises = sorted({o.amount for o in sit.options
                     if o.kind in ("raise", "bet") and o.amount is not None})
    if raises:
        if len(raises) > 1 or raises[0] > PREFLOP_MIN_RAISE_BB + 1e-9:
            lines.append(f"本桌翻前最低加注：{PREFLOP_MIN_RAISE_BB:g}BB —— 这是牌桌"
                         "下限（更小的会被客户端拒绝）；开池尺度按底池放大，"
                         "以上面的选项金额为准")
        else:
            lines.append(f"本桌翻前最低加注：{PREFLOP_MIN_RAISE_BB:g}BB；"
                         "当前可选开池金额都受这个下限约束")
    return lines


_TABLE_SMALL_OPPS = 20.0


def _table_stat_zh(label: str, value: float,
                   opportunities: float | None = None) -> str:
    """单个桌况指标；机会数 <20 时明说，避免 5 次机会的 82% 被当成硬事实。"""
    text = f"{label} {value:.0f}%"
    if opportunities is not None and opportunities < _TABLE_SMALL_OPPS:
        text += f"（仅 {_fmt_num(opportunities)} 次机会）"
    return text


def _table_stab_read_zh(stats: "PooledStats") -> str:
    """桌况的 stab 读法：把「前面没人下注时下注 X%」翻成可执行动作。

    用户 2026-11-01 口径：只给提示，不做硬闸。高 stab 的核心含义是
    「你过牌不是免费看牌」——强牌可过牌诱导，中等牌别主动下注把空气
    打走；但 stab 只描述「下注」，不描述「加注」，加注范围仍按价值密集
    处理。低 stab 才是「过牌基本能免费看下一张」。
    """
    stab = stats.stab
    opps = (stats.opportunities or {}).get("stab", 0.0)
    if stab is None or opps < _TABLE_SMALL_OPPS:
        return ""
    if stab >= 40:
        return (f"桌况读法：前面没人下注时这桌爱开火（{stab:.0f}%）。"
                "你过牌不是免费看牌——强牌可以过牌诱导 check-raise，"
                "中等牌别主动下注把他们的空气打走；但他们加注范围仍偏强，"
                "别用中对跟加注。")
    if stab >= 30:
        return (f"桌况读法：前面没人下注时这桌会开火（{stab:.0f}%）。"
                "过牌别当免费看牌；强牌可过牌诱导，中等牌下注前先想清楚"
                "被加注怎么办。")
    if stab <= 15:
        return (f"桌况读法：前面没人下注时这桌很少开火（{stab:.0f}%）。"
                "你过牌大概率能免费看下一张；没摊牌价值时可以在下一街"
                "主动偷池。")
    return ""


def render_table_stats_zh(stats: "PooledStats | None",
                          opponents_in_hand: int | None = None,
                          street: str | None = None,
                          suppress_cbet: bool = False) -> list[str]:
    """桌况事实：`current_table` 是本手在局对手，`population` 是冷启动先验。

    替代 `.table_dy` 里「整体偏松」那类形容词。样本太小时不渲染 ——
    十手牌算出来的「本桌 VPIP」比没有更误导。

    `opponents_in_hand` 用主事实块的在局人数，而不是 `stats.total_players`：
    后者来自聚合时的活动对手列表，偶尔会和状态帧/行动线对不上；两个数字
    同时出现时模型会不知道该信谁（d3a9 出现过「本手在局 8 人」+「当前牌桌
    数据（本手在局 3 人）」）。桌况只声明「有画像的对手数」，覆盖率按主事实
    人数算。
    """
    if stats is None or stats.hands < 20:
        return []
    # 0 对手是状态帧异常（翻后只剩 Hero）。这时任何「本桌 N 名对手有画像」
    # 都会和主事实「本手在局 1 人」打架，直接不渲染桌况。
    if opponents_in_hand is not None and opponents_in_hand <= 0:
        return []
    opportunities = stats.opportunities or {}
    parts = [
        _table_stat_zh(label, value, opportunities.get(key))
        for label, value, key in (
            ("VPIP", stats.vpip, "preflop"),
            ("PFR", stats.pfr, "preflop"),
            ("3-bet", stats.threebet, "threebet"),
            ("溜入", stats.limp, "limp"),
            ("对 c-bet 弃", stats.fold_to_cbet, "fcbet"),
            ("前面没人下注时下注", stats.stab, "stab"),
        )
        if value is not None
    ]
    if not parts:
        return []
    total = 0
    if stats.scope == "current_table":
        # 以主事实块的在局人数为准；没有传时退回聚合口径。
        if opponents_in_hand is not None:
            total = max(int(opponents_in_hand), 0) + 1
        else:
            total = max(int(stats.total_players or 0), stats.players + 1)
        sample = (f"历史画像，单人最多约 {stats.hands} 手"
                  if stats.approx_hands else f"历史画像，单人最多 {stats.hands} 手")
        opponents_total = max(total - 1, 0)
        # 聚合时可能有已弃牌/已离桌的人；不能在「本手 2 人」时写 4 名对手
        # 有画像。以主事实人数封顶，缺口只算真实在局的人。
        known = min(int(stats.players), opponents_total)
        lines = [f"## 当前牌桌数据（{known} 名在局对手有画像；桌均；{sample}）"]
        unknown = max(opponents_total - known, 0)
        if unknown:
            lines.append(f"另外 {unknown} 名在局对手没有画像，"
                         "按未知对手处理，不伪造个人数据")
    else:
        lines = ["## 全场数据（全场先验，非本桌）"]
    if stats.scope == "current_table":
        lines.append("｜".join(parts))
    else:
        lines.append(f"{stats.players} 人 / {stats.hands} 手："
                     + "｜".join(parts))
    street_stats = [
        _table_stat_zh(label, value, opportunities.get(key))
        for label, value, key in (
            ("翻牌", stats.fold_to_bet_flop, "flop"),
            ("转牌", stats.fold_to_bet_turn, "turn"),
            ("河牌", stats.fold_to_bet_river, "river"),
        )
        if value is not None
    ]
    if street_stats:
        lines.append("面对下注弃：" + "｜".join(street_stats))
    stab_read = _table_stab_read_zh(stats)
    if stab_read:
        lines.append(stab_read)
    tags: list[str] = []
    if stats.loose is True:
        tags.append("松")
    elif stats.loose is False:
        tags.append("紧")
    # 「3-bet 少」不是「整体被动」：后者需要翻后 call/raise 数据，
    # 只看 3-bet 会把带 MANIAC 的桌子写成被动桌。
    if stats.passive is True:
        tags.append("3-bet 少")
    if tags:
        lines.append("整体：" + "、".join(tags))
    total_opponents = max(total - 1, 0)
    all_known = total_opponents > 0 and stats.players >= total_opponents
    # 至少一半在局对手有画像才给桌级结论；9 人桌只有 2 人有数据时，
    # 「当前桌可剥削」会盖过另外 6 个未知对手。
    enough_coverage = (stats.players >= 2
                       and stats.players * 2 >= total_opponents)
    allow_exploit = (stats.scope != "current_table"
                     or all_known or enough_coverage)
    if allow_exploit:
        exploit = table_exploit_zh(stats, street=street,
                                   suppress_cbet=suppress_cbet)
        if exploit:
            label = ("当前桌可剥削" if stats.scope == "current_table"
                     else "全场可剥削")
            lines.append(label + "：" + exploit)
    return lines


def render_explosive_zh(sit: Situation) -> list[str]:
    """炸弹池的结构与打法事实。

    用户 2026-11-01 口径（全量 497 手复盘后修订）：炸弹池顶对及以上**只打一条街**
    ——下注一街后过牌收手，被加注/过牌-加注即弃；低于顶对默认过牌/弃牌、不主动
    下注，只有「有位置 + 画像可读 + 赔率≥3:1」才跟一注。依据：41 手炸弹池里唯一
    的大坑是明三条打满三条街（-140.5），一条街内结束的几手都只 -23。
    """
    if sit.street == PREFLOP:
        return ["炸弹池翻前：每人强制投入 5BB 前注后直接发翻牌，翻前没有可做的决策；"
                "翻牌后按正常翻后规则打。"]
    spr = sit.spr
    if spr is None:
        return ["炸弹池结构：SPR 未知，按有效筹码和底池几何，按正常翻后规则打。"]

    players = (sit.opponents_in_hand + 1
               if sit.opponents_in_hand is not None
               else (sit.snap.players_dealt or sit.snap.table_size or 0))
    players_text = f"{players} 人" if players else "多人"
    lines = [
        f"炸弹池结构：SPR {spr:.1f}，{players_text}池；底池大不等于必须打光，"
        "按正常翻后规则（牌力、赔率、位置、对手线路）打。",
    ]
    if spr > 3.0:
        lines.append(
            "炸弹池深码：一对不做大底池；顶对强踢/超对可以正常下注/跟注，"
            "但面对加注或多街连开要降级。")
    else:
        lines.append(
            "炸弹池浅码：后手少、底池大，但顶对/超对仍按赔率和对范围权益决定；"
            "无加注、干燥面、价格合适时可以继续，但不主动造大底池；"
            "顶对弱踢/中对/底对/弱听牌不要为了套池主动造大底池。")
    lines.append(
        "炸弹池打法：顶对及以上总共只投一条街的钱（下注/跟注），之后过牌/弃牌、"
        "被下注即弃；低于顶对默认过牌/弃牌、不主动下注，只有「有位置 + 画像可读"
        "（疯子/高弃牌率）+ 赔率≥3:1」才跟一注；不要用「炸弹池」当打光理由。")

    # 已经投过一条街的，直接把「本街不再投钱」写成事实：模型对
    # 「只打一条街」会理解成「我还能打这一条街」，K3s 河牌就是这么跟出去的。
    prior = []
    for name, zh in (("flop", "翻牌"), ("turn", "转牌")):
        if name == sit.street:
            break
        for act in sit.snap.actions_of(name) or []:
            if (getattr(act, "is_hero", False) or act.seat_id == "hero") \
                    and act.action in ("bet", "raise", "call") \
                    and (act.amount or 0) > 0:
                prior.append(zh)
                break
    if prior:
        lines.append(
            f"炸弹池节奏（硬规则）：你已在{'、'.join(prior)}投过这一条街，"
            "本街及后续只能过牌/弃牌，**不能跟注、不能加注**；"
            "对手能跟到下注的正是比你大的牌，「赔率合适」不构成跟注理由。")

    hr = sit.hand_ranges
    if hr is not None:
        actual = (sit.opponents_in_hand + 1
                  if sit.opponents_in_hand is not None else 1)
        fair = 1.0 / max(actual, 1)
        random_fill = getattr(hr, "random_opponents", 0)
        detail = f"实际 {actual} 人；范围建模 {len(hr.opponents)} 家"
        if random_fill:
            detail += f" + 随机牌 {random_fill} 家"
        lines.append(
            f"炸弹池权益：对范围权益 {hr.equity:.0%}（按河牌、未计后续下注，{detail}；"
            f"公平份额 {fair:.1%}）。这只是当前权益，不自动等于价值下注。")
    return lines


_ACE_MARKERS = ("见a", "有a", "a高", "a面", "a的", "a时", "公共牌有a")
_ACE_NEGATIONS = (
    "不弃", "不怕", "不惧", "不弱", "不会弃", "不会怕", "不易弃",
    "不一定会弃", "未必会弃", "未必弃", "不会轻易弃", "抵抗强",
    "抵抗好", "扛得住", "不怂",
)
_ACE_WEAKNESS = ("抵抗", "弃", "怕", "弱", "fold")


def _filter_table_dynamics_zh(text: str | None,
                              snap: HandSnapshot) -> str | None:
    """按街/牌面过滤现场观察：A 高面的观察只在翻后且牌面有 A 时注入。

    `.table_dy` 是自由文本，程序不可能完全理解语义；这里只做一条最影响
    决策的过滤：翻前没有公共牌、非 A 牌面也不该看到「A 高面多打」的指令。
    其余观察原样保留。
    """
    if not text:
        return None
    board_has_ace = any(str(card)[:1].upper() == "A" for card in snap.board)
    keep: list[str] = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        compact = line.replace(" ", "").lower()
        ace_specific = any(k in compact for k in ("a高", "见a", "有a", "a面",
                                                  "a时", "公共牌有a"))
        if ace_specific and (snap.street == PREFLOP or not board_has_ace):
            continue
        keep.append(line)
    return "\n".join(keep) or None


def _notes_ace_weakness(text: str | None) -> bool:
    """人工观察里是否写了「见 A 抵抗力下降」这类池级剥削。

    这是用户 2026-10-31 的现场口径，不是程序统计算出来的：写进 `.table_dy`
    后，这里只负责在 A 高面把它翻译成一条**条件事实**，没写就不编。

    按分句匹配否定：同一段里写「他从不弃牌」不会关掉另一句「A 高面容易弃」；
    「见A不弃」「A高不怕」「A面抵抗强」仍然读成「不弱」。
    """
    if not text:
        return False
    for clause in re.split(r"[\n。；;，,]", str(text)):
        compact = clause.replace(" ", "").lower()
        if not compact:
            continue
        if any(k in compact for k in _ACE_NEGATIONS):
            continue
        if not any(k in compact for k in _ACE_MARKERS):
            continue
        if any(k in compact for k in _ACE_WEAKNESS):
            return True
    return False


def _attack_trap_reads(sit: Situation) -> tuple[list[str], list[str]]:
    """活对手里「跟 c-bet 多」/「过牌-加注频繁」的人（先验估计不算硬结论）。

    剥削不能只看「对手范围宽」：如果对手 xc（过牌-跟注）/xr（过牌-加注）
    频率高，翻牌小注诈唬没有弃牌率，还会被加注惩罚 —— 这时正确动作是
    跟着过牌，而不是高频开火。
    """
    live = {o.seat_id: o for o in sit.snap.opponents if not o.folded}
    callers: list[str] = []
    raisers: list[str] = []
    for read in sit.opponent_reads:
        seat = read.seat_id
        if seat not in live or read.archetype == "unknown":
            continue
        position = live[seat].position or seat
        call = read.call_to_cbet_pct
        fold = read.fold_to_cbet_pct
        if ("call_to_cbet" not in read.prior_dominated
                and call is not None and call >= 55):
            callers.append(f"{position} 跟 c-bet {call:.0f}%")
        elif ("fold_to_cbet" not in read.prior_dominated
                and fold is not None and fold <= 30):
            callers.append(f"{position} 对 c-bet 只弃 {fold:.0f}%")
        xr = read.check_raise_pct
        if ("check_raise" not in read.prior_dominated
                and xr is not None and xr >= 20):
            raisers.append(f"{position} 过牌-加注 {xr:.0f}%")
    return callers, raisers


_ATTACK_FOLD_SUPPORT_PCT = 50.0
_ATTACK_FOLD_WEAK_PCT = 35.0


def _checked_before_hero(snap: HandSnapshot) -> list:
    """本街在 Hero 之前已经过牌、且还能行动的对手。

    `behind_opponents()` 只数位置在 Hero 之后的人；已经过牌的人不在里面，
    但 Hero 下注后他们会再行动一次，可以 check-raise。攻击块必须看到这批人。
    """
    order = _action_order(snap)
    hero = snap.hero_position
    if order is None or hero not in order:
        return []
    idx = order.index(hero)
    checks = [a for a in snap.actions_of(snap.street) if a.action == "check"]
    out = []
    for opponent in snap.opponents:
        if (opponent.folded or not _can_act(opponent)
                or opponent.position not in order):
            continue
        if order.index(opponent.position) >= idx:
            continue
        if any(_same_actor(a, opponent) for a in checks):
            out.append(opponent)
    return out


def _checkraise_note(checked: list) -> str:
    """把「前面过牌的人仍能加注」写成一句硬事实。"""
    if checked:
        names = "、".join(o.position or o.seat_id for o in checked[:4])
        if len(checked) > 4:
            names += "…"
        return (f"前面 {len(checked)} 人已过牌（{names}），"
                "他们仍可过牌-加注；这不是无风险下注。")
    return ("前面可能还有人已过牌（行动线未记录全），"
            "仍要防过牌-加注；这不是无风险下注。")


def _xr_data_known(sit: Situation) -> bool:
    """是否至少有一名活对手的过牌-加注频率是实测值。

    只判断「有没有数据」；xr 高不高由 `_attack_trap_reads()` 判断，
    后者命中时整个攻击块会走「跟着过牌」分支。
    """
    live = {o.seat_id for o in sit.snap.opponents if not o.folded}
    return any(
        r.seat_id in live and r.archetype != "unknown"
        and r.check_raise_pct is not None
        and "check_raise" not in r.prior_dominated
        for r in sit.opponent_reads)


def _attack_fold(stats: "PooledStats | None", street: str):
    """当前街「面对下注弃牌率」的值和机会数。"""
    value = getattr(stats, f"fold_to_bet_{street}", None) if stats else None
    opps = (stats.opportunities or {}).get(street, 0.0) if stats else 0.0
    return value, opps


def _attack_fold_verdict(value: float | None, opps: float) -> str:
    """只看弃牌率这一项的结论；最终开火/不开火由攻击块统一裁决。"""
    if value is None:
        return "unknown"
    if opps < _TABLE_SMALL_OPPS:
        return "small"
    if value >= _ATTACK_FOLD_SUPPORT_PCT:
        return "support"
    if value <= _ATTACK_FOLD_WEAK_PCT:
        return "weak"
    return "neutral"


def _board_wet(sit: Situation) -> bool:
    gt = sit.ground_truth
    return bool(gt is not None and (
        gt.board_info.flush_draw_possible
        or gt.board_info.flush_made_possible
        or gt.board_info.straight_possible))


def _attack_evidence(sit: Situation, stats: "PooledStats | None", street: str,
                     scope_label: str, fold_verdict: str = "unknown") -> list[str]:
    """攻击块的「依据」：只陈述单项事实，不再给第二套结论。"""
    lines: list[str] = []
    value, opps = _attack_fold(stats, street)
    if value is not None:
        cn = {"flop": "翻牌", "turn": "转牌", "river": "河牌"}.get(
            street, street)
        line = f"{scope_label}：对手面对{cn}下注弃 {value:.0f}%"
        if opps < _TABLE_SMALL_OPPS:
            line += f"（仅 {_fmt_num(opps)} 次机会）"
            verdict = "机会数不足，不能单独作为开火理由"
        elif value >= _ATTACK_FOLD_SUPPORT_PCT:
            verdict = "弃牌率支持开火"
        elif value <= _ATTACK_FOLD_WEAK_PCT:
            verdict = "弃牌率不足，不支持开火"
        else:
            verdict = "弃牌率一般，不能单独作为开火理由"
        # 「只描述这一项」避免和上面的总裁决读成两句相反建议。
        lines.append(line + f" → {verdict}（只描述这一项，结论见上）")

    if _board_wet(sit):
        lines.append(
            "牌面有花/顺可能：对手范围更容易连上，降低开火频率；"
            "优先用有后门/阻断的牌，纯空气别硬开。")

    if any(str(card)[:1].upper() == "A" for card in sit.snap.board):
        if _notes_ace_weakness(sit.table_dynamics):
            if fold_verdict == "support":
                lines.append(
                    "A 高面：人工观察说本池对手见 A 抵抗力下降（容易弃牌）→ "
                    "可以借 A 施压（本街弃牌率也支持）；仍受 xr/跟注约束。")
            else:
                # 观察只作背景，不改变「弃牌率不足/没有 xr 数据」的结论。
                lines.append(
                    "A 高面：人工观察说本池对手见 A 容易弃牌，但本街弃牌率"
                    "不足/没有 xr 数据，不能只凭 A 高开火；按上面的结论。")
        else:
            # 没有现场观察时只给事实，不断言「A 高面一定更有效」。
            lines.append(
                "A 高面：A 是牌面最大张；对手会不会弃牌要看上面的弃牌率"
                "和画像，不能只凭 A 高默认开火。")
    return lines


def render_attack_zh(sit: Situation) -> list[str]:
    """宽范围/死钱局的剥削判断（程序判断「范围封顶 + Hero 收尾」）。

    触发条件很窄：**翻前溜入池 + 本街没人下注 + Hero 最后一个行动**。
    这里只输出**一个结论**（不开火 / 仅强牌下注 / 可小注 / 可高频小注），
    依据里的弃牌率、xr、牌面、A 高面都只描述单项，不另下结论。

    用户 2026-10-31 口径：这个池子的人看到公共牌有 A 时抵抗力明显下降，
    A 高面要多打。人工观察写在 `.table_dy`，这里只在 A 高面引用它；
    但对手 xc/xr 高时，A 高面也不硬开。
    """
    snap = sit.snap
    street = snap.street
    if street == PREFLOP or sit.pot_type != "limped pot":
        return []
    if (snap.to_call or 0.0) > 0 or sit.players_left_to_act != 0:
        return []
    opponents = sit.opponents_in_hand or 0
    if opponents < 2:
        return []

    limpers = count_limpers(snap)
    limp_text = f"翻前 {limpers} 人溜入" if limpers else "翻前多人溜入"
    checked = _checked_before_hero(snap)
    # 只有「所有还能行动的对手都记录了过牌」才能说「全部过牌到 Hero」。
    # 全下的人不需要 check；OCR 漏抓 check 时 players_left_to_act 仍可能是 0。
    expected_checks = sum(1 for o in snap.opponents if _can_act(o))
    if expected_checks == 0:
        # 所有人都全下：没有「过牌到 Hero」这回事，也没有可攻击的弃牌率。
        return []
    all_checked = len(checked) >= expected_checks
    if all_checked:
        dead_money = (
            f"{limp_text}、没人加注；本街全部过牌到 Hero"
            "（你收尾）→ 对手范围宽，底池是死钱。")
    else:
        dead_money = (
            f"{limp_text}、没人加注；本街没有下注，但行动线未记录全，"
            "不能确认全部过牌（按可能 check-raise 处理）→ "
            "对手范围可能仍宽，底池是死钱，但不能当无风险开火点。")
    callers, raisers = _attack_trap_reads(sit)
    stats = sit.table_stats
    scope = (getattr(stats, "scope", "population")
             if stats is not None else "population")
    scope_label = "本桌" if scope == "current_table" else "全场先验"
    cbet_opps = (stats.opportunities or {}).get("fcbet", 0.0) if stats else 0.0
    table_sticky = bool(
        stats is not None and scope == "current_table"
        and stats.fold_to_cbet is not None
        and cbet_opps >= _TABLE_SMALL_OPPS and stats.fold_to_cbet <= 35)
    xr_known = _xr_data_known(sit)
    fold_value, fold_opps = _attack_fold(stats, street)
    fold_verdict = _attack_fold_verdict(fold_value, fold_opps)
    wet = _board_wet(sit)

    if raisers or callers or table_sticky:
        lines = ["## 剥削判断（宽范围/死钱）", dead_money]
        lines.append(_checkraise_note(checked))
        lines.append("但他们的过牌不一定是放弃：")
        if raisers:
            lines.append(
                "、".join(raisers) + " —— 过牌-加注频繁，这是「过牌控池」点，"
                "不是开火点；用中等牌/空气下注会被 xr 惩罚。")
        if callers:
            lines.append(
                "、".join(callers) + " —— 跟 c-bet 多，小注诈唬没有弃牌率。")
        if table_sticky:
            lines.append(
                f"{scope_label}对 c-bet 只弃 {stats.fold_to_cbet:.0f}%"
                f"（{_fmt_num(cbet_opps)} 次机会）—— 诈唬没有弃牌率。")
        lines.append(
            "结论：跟着过牌，看免费牌；只有强牌/强听可以下注拿价值，"
            "别用空气/中等牌硬开。")
        if (any(str(card)[:1].upper() == "A" for card in snap.board)
                and _notes_ace_weakness(sit.table_dynamics)):
            lines.append("A 高面也一样：对手 xc/xr 高时，怕 A 不等于会弃牌。")
        return lines

    lines = ["## 攻击机会（宽范围/死钱）", dead_money]
    lines.append(_checkraise_note(checked))
    if not xr_known:
        lines.append(
            "没有对手过牌-加注数据，不能把这里当无风险开火点。")

    if fold_verdict == "support" and xr_known and not wet:
        conclusion = "可高频小注攻击（1/3–1/2 池），别只拿成牌下注"
    elif fold_verdict == "support" and xr_known:
        conclusion = ("可小注，但牌面有花/顺可能，降低频率；"
                      "别只拿成牌下注")
    elif fold_verdict == "support":
        conclusion = ("可小注，但不默认高频（没有 xr 数据）；"
                      "只用强牌/强听，中等牌/空气优先过牌")
    elif fold_verdict == "weak":
        conclusion = "不开火：只用强牌/强听下注，中等牌/空气过牌"
    elif fold_verdict == "small":
        conclusion = "仅强牌/强听下注（弃牌率样本不足，不能当开火理由）"
    else:
        conclusion = "仅强牌/强听下注（没有该街弃牌率，不能默认开火）"
    lines.append("结论：" + conclusion)
    lines.append("依据：")
    lines.extend(_attack_evidence(sit, stats, street, scope_label,
                                  fold_verdict))
    if fold_verdict == "support" and xr_known and not wet:
        lines.append(
            "范围封顶 + 弃牌率达标 + 没有高 xr/高跟注证据；"
            "被跟注后按下一街权益继续，被加注要放弃。")
    return lines


def _behind_detail_zh(snap: HandSnapshot,
                      prefix: str = "跟注/过牌后待行动") -> str:
    """「跟注/过牌后待行动」一行：人数 + 位置名 + 谁还没跟到当前注。

    只写人数（旧口径）会让模型忽略「溜入者还没响应加注」这种关键约束；
    1c9 的 K8o 就是把它当成「收尾的便宜跟注」。
    """
    behind = behind_opponents(snap)
    order = _action_order(snap)
    if order is None or snap.hero_position not in order:
        return ""
    behind.sort(key=lambda o: order.index(o.position))
    max_bet = (snap.hero_bet or 0.0) + float(snap.to_call or 0.0)
    pending = [o for o in behind
               if 1e-9 < _street_bet(snap, o) < max_bet - 1e-9]
    pending_ids = {o.seat_id for o in pending}
    bb_option = _big_blind_option_pending(snap)
    parts = []
    if pending:
        pending_names = "、".join(o.position or o.seat_id for o in pending)
        parts.append(f"{pending_names} 还没跟到 {_fmt_num(max_bet)}BB")
    rest = [o.position or o.seat_id for o in behind
            if o.seat_id not in pending_ids
            and (bb_option is None or o.seat_id != bb_option.seat_id)]
    if rest:
        shown = "、".join(rest[:4]) + ("…" if len(rest) > 4 else "")
        parts.append(f"{shown} 还没行动")
    if bb_option is not None:
        parts.append("BB 还没行动，你跟注后 BB 仍有加注选项")
    text = f"{prefix}：{len(behind)} 人"
    if parts:
        text += "（" + "；".join(parts) + "）"
    return text


def _table_stab_hint_short(stats: "PooledStats | None") -> str:
    """当前局面里的一行 stab 提示：放在行动线之前，别让模型只看到权益。

    和桌况块的完整读法不重复细节：这里只说「这桌爱开火，过牌不是免费
    看牌；强牌诱导、中等牌控池」，让模型在决策前就看到这条桌风。
    """
    if stats is None:
        return ""
    stab = getattr(stats, "stab", None)
    opps = (getattr(stats, "opportunities", {}) or {}).get("stab", 0.0)
    if stab is None or opps < _TABLE_SMALL_OPPS or stab < 40:
        return ""
    return (f"桌况提示：这桌前面没人下注时爱开火（{stab:.0f}%）；"
            "你过牌不是免费看牌，强牌可过牌诱导，中等牌过牌控池。")


def render_facts_zh(sit: Situation) -> str:
    """中文 lean 的 user 事实块（§4.1）。所有金额/概率来自程序计算。"""
    snap = sit.snap
    anomaly = sit.abnormal_no_opponents
    lines: list[str] = ["## 当前局面"]

    table = f"{snap.table_size} 人桌" if snap.table_size else "人数未知"
    if snap.blinds:
        blinds = f"{_fmt_num(snap.blinds.sb)}/{_fmt_num(snap.blinds.bb)}"
        if snap.blinds.ante:
            blinds += f"（前注 {_fmt_num(snap.blinds.ante)}）"
    else:
        blinds = "未知"
    lines.append(f"桌况：{table}｜盲注 {blinds}")
    in_hand = snap.opponents_in_hand
    folded = sum(1 for o in snap.opponents if o.folded)
    in_hand_line = (f"本手在局：{in_hand + 1} 人"
                    f"（Hero + {in_hand} 名对手）")
    if folded:
        in_hand_line += f"；已弃牌 {folded} 人"
    lines.append(in_hand_line)
    hero_cards = " ".join(snap.hero_cards) or "未知"
    stack_text = (f"{_fmt_num(snap.hero_stack)}BB"
                  if snap.hero_stack is not None else "未知")
    lines.append(f"Hero：{snap.hero_position or '位置未知'}｜底牌 {hero_cards}｜"
                 f"后手 {stack_text}")
    board = " ".join(snap.board) or "无"
    pot_text = (f"{_fmt_num(snap.pot)}BB"
                if snap.pot is not None and snap.pot > 0 else "未知")
    lines.append(f"街道：{_STREET_ZH.get(snap.street, snap.street)}｜"
                 f"公共牌 {board}｜底池 {pot_text}｜"
                 f"需跟 {_fmt_num(call_amount(snap))}BB")

    geometry: list[str] = []
    effective = _effective_stack(snap)
    playable = _playable_depth(snap)
    if effective is not None:
        geometry.append(f"有效筹码：{_fmt_num(effective)}BB")
        # 多人池里一个短码会把 min 压到很短，但开池/单挑范围该按能跟注的
        # 最深对手算；两个数都写出来，模型才不会把整桌当短码局。
        if (playable is not None and effective < playable - 0.5
                and (sit.opponents_in_hand or 0) >= 2):
            geometry.append(f"可玩深度：{_fmt_num(playable)}BB"
                            "（对最深对手的后手；翻前开池/单挑范围按此，"
                            "不含本街已下注；翻后套池看有效筹码）")
    if sit.spr is not None:
        spr_text = "<0.1" if 0 < sit.spr < 0.1 else f"{sit.spr:.1f}"
        geometry.append(f"SPR {spr_text}")
    if sit.pot_type:
        geometry.append("底池类型：" + _POT_TYPE_ZH.get(sit.pot_type, sit.pot_type))
    if anomaly:
        lines.append("有效筹码/SPR：未知（状态帧异常，在局对手为 0）")
    if geometry:
        lines.append("｜".join(geometry))

    position: list[str] = []
    if snap.street != PREFLOP and (sit.opponents_in_hand or 0) == 0:
        # 状态帧把对手全标成弃牌时，任何「有位置/先行动」都是假的。
        position.append("位置：无在局对手（状态帧异常，翻后建议不可信）")
    elif sit.in_position is not None:
        # 位置永远是翻后座位顺序。翻前另有行动顺序（BB 可能还没说话），
        # 所以翻前的这行必须标成「翻后位置」，不能只写「位置」。
        label = "翻后位置" if snap.street == PREFLOP else "位置"
        if sit.in_position:
            position.append(f"{label}：Hero 有位置（按座位顺序最后行动）")
        elif hero_first_to_act(snap):
            position.append(f"{label}：Hero 无位置（翻后先行动）")
        else:
            position.append(f"{label}：Hero 无位置（翻后身后还有在局对手）")
    if sit.players_left_to_act is not None and not anomaly:
        if snap.street == PREFLOP:
            position.insert(0, _behind_detail_zh(snap, prefix="翻前行动")
                            or f"翻前行动：{sit.players_left_to_act} 人")
        else:
            position.append(_behind_detail_zh(snap)
                            or f"跟注/过牌后待行动：{sit.players_left_to_act} 人")
        position.append("下注/加注会重新打开行动，已过牌/跟注/下注的对手仍可再行动"
                        "（位置优势≠没人能加注）")
    elif snap.street == PREFLOP:
        position.insert(0, "翻前行动：未知（有对手位置未读出）")
    if position:
        lines.append("｜".join(position))

    owed = call_amount(snap)
    winnable = winnable_pot(snap)
    if owed > 0 and winnable > 0:
        lines.append(f"赔率门槛：{_fmt_num(owed)}BB / 可争夺底池 "
                     f"{_fmt_num(winnable)}BB = {owed / winnable * 100:.1f}%")
    if snap.pot is not None and snap.pot > 0:
        excess = uncalled_excess(snap)
        pot_line = f"可争夺底池：{_fmt_num(winnable)}BB" + (
            "（含我的跟注）" if owed > 0 else "")
        if excess > 0:
            pot_line += (f"；牌桌底池 {_fmt_num(snap.pot)}BB 含对手超额 "
                         f"{_fmt_num(excess)}BB，已扣除")
        lines.append(pot_line)

    hero_total = _hero_total_stack(snap)
    short: list[tuple] = []
    for o in snap.opponents:
        if o.folded:
            continue
        total = _opponent_total_stack(snap, o)
        if (total is not None and hero_total is not None
                and total < hero_total * 0.95):
            short.append((o, total))
    if short:
        detail = "、".join(
            f"{o.position or o.seat_id} {_fmt_num(total)}BB"
            for o, total in short)
        lines.append(f"短码/side pot：{detail}（side pot 未建模；"
                     "能争夺的是他们本手还能投入的总额 = 后手 + 本街已下注）")
    elif anomaly:
        lines.append("短码/side pot：未知（状态帧异常，在局对手为 0）")
    elif hero_total is None:
        # 自己的后手没读出来时，不能写「无」：短码关系根本没算过。
        lines.append("短码/side pot：未知（Hero 后手未读出）")
    elif _unknown_stack_opponents(snap):
        lines.append("短码/side pot：未知（有对手筹码未读出）")
    else:
        lines.append("短码/side pot：无")
    if snap.hero_invested is not None:
        lines.append(f"本手已投入：{_fmt_num(snap.hero_invested)}BB")

    if sit.explosive_pot or snap.bomb_pot:
        explosive = "是"
        if snap.bomb_pot:
            explosive += "（屏幕出现「幸运暴击 / BOMB POT」横幅）"
        elif sit.explosive_pot and sit.pot_per_player is not None:
            prev_pot = previous_street_pot(snap)
            count = snap.players_dealt or snap.table_size
            explosive += (f"（上一街底池 {_fmt_num(prev_pot)}BB ÷ {count} 人 = "
                          f"{sit.pot_per_player:.2f}BB/人，炸弹前注固定 5BB/人）")
        lines.append(f"爆炸底池：{explosive}")
    if sit.explosive_pot:
        lines.extend(render_explosive_zh(sit))

    lines.append("")
    lines.append("## 行动线")
    lines.append("（行动线来自 OCR/视频解码，可能有归因或顺序误差；"
                 "与「当前局面」硬事实冲突时，以硬事实为准。）")
    if any(a.inferred for rec in snap.streets for a in rec.actions):
        lines.append("标注「推断」的动作是视频漏抓后按底池对账补出来的；"
                     "金额是「本街加到总额」。")
    for rec in snap.streets:
        pot_start = (f"（街前底池 {_fmt_num(rec.pot_start)}BB）"
                     if rec.pot_start is not None else "")
        lines.append(f"{_STREET_ZH.get(rec.street, rec.street)}{pot_start}："
                     f"{_fmt_actions_zh(list(rec.actions))}")

    preflop = render_preflop_zh(sit)
    if preflop:
        lines.append("")
        lines.extend(preflop)

    strength = render_hand_strength_zh(sit)
    if strength:
        lines.append("")
        lines.extend(strength)

    # 本手还没弃牌的对手全部进提示词：多人池里模型的 sizing/弃牌决策依赖
    # 「身后还剩谁、谁是什么类型」，截到 3 家会让 9 人桌的其余座位变成盲区。
    # 渲染顺序由 `render_reads_zh` 按位置排，不按画像在元组里的先后。
    active = {o.seat_id: (o.position or o.seat_id)
              for o in snap.opponents if not o.folded}
    in_hand_reads = [r for r in sit.opponent_reads
                     if r.archetype != "unknown" and r.seat_id in active]
    reads = render_reads_zh(
        in_hand_reads, active,
        header="## 对手画像（逐座位，优先于全场统计/人工观察）")
    if reads:
        lines.append("")
        lines.extend(reads)

    # 攻击块会给出本街弃牌率的最终裁决；有攻击块、或某个座位已经有
    # 「别开火」的反向证据时，桌级 c-bet 建议不再单独出现，避免同一份
    # prompt 里两个「能不能开火」的来源。
    attack = render_attack_zh(sit)
    trap_callers, trap_raisers = _attack_trap_reads(sit)
    suppress_cbet = bool(attack) or bool(trap_callers or trap_raisers)
    table_stats = render_table_stats_zh(
        sit.table_stats, opponents_in_hand=sit.opponents_in_hand,
        street=snap.street, suppress_cbet=suppress_cbet)
    if table_stats:
        lines.append("")
        lines.extend(table_stats)

    dynamics = _filter_table_dynamics_zh(sit.table_dynamics, snap)
    if dynamics:
        lines.append("")
        lines.append("## 人工观察（现场记录；统计算不出来的东西）")
        lines.append(dynamics)

    if attack:
        lines.append("")
        lines.extend(attack)

    gaps = [_gap_zh(w) for w in sit.warnings]
    if sit.hand_ranges is not None:
        actual = (sit.opponents_in_hand
                  if sit.opponents_in_hand is not None
                  else snap.opponents_in_hand)
        modeled = len(sit.hand_ranges.opponents)
        random_fill = getattr(sit.hand_ranges, "random_opponents", 0)
        if (actual > modeled + random_fill
                and not any("只建模前" in g for g in gaps)):
            gaps.append(f"只建模前 {modeled} 名对手；另有 "
                        f"{actual - modeled - random_fill} 名未计入范围权益")
    lines.append("")
    lines.append("## 数据质量（硬事实缺失会影响可用动作；"
                 "范围缺口会降低范围权益可信度）")
    summary = _summarize_gaps(
        gaps, has_range_equity=sit.hand_ranges is not None)
    if summary:
        lines.extend(f"- {gap}" for gap in summary)
    else:
        lines.append("无")

    lines.append("")
    stab_hint = _table_stab_hint_short(sit.table_stats)
    if stab_hint:
        lines.append("## 决策提示")
        lines.append(stab_hint)
        lines.append("")
    lines.extend(render_options_zh(sit))
    return "\n".join(lines)


def build_messages(sit: Situation) -> tuple[str, str]:
    """返回中文 lean `(system, user facts)`；system 按前注与否拼装。"""
    return build_system(sit), render_facts_zh(sit)


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
    # OpenRouter 的可选排名头（走 OpenRouter 时用）
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
        except TimeoutError as exc:
            # 超时不能悄悄落到 urllib 再等一轮：调用方需要 AIError 才能出兜底建议。
            raise AIError(f"调用模型超时：{exc}") from exc
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
                 json_mode: bool = False, return_meta: bool = False):
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
        choice = body["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIError("模型响应结构不对") from exc
    if not isinstance(message, dict):
        raise AIError("模型响应结构不对")
    if not return_meta:
        return message
    return message, {
        "usage": body.get("usage") if isinstance(body, dict) else None,
        "finish_reason": choice.get("finish_reason"),
    }


def complete_json(settings: AISettings, system: str, user: str,
                  timeout: float = 20.0,
                  request=None, history: list[dict] | None = None) -> dict:
    """调用 OpenAI 兼容的 `/chat/completions` 并解析成 `{option, reason}`。

    失败抛 AIError。这里刻意不引 SDK、不走流式：一次只回几十个 token，
    urllib 足够，也避免给实时链路增加依赖。

    `history` 是多轮对话里位于 system 之后、本轮 user 之前的消息。要续接
    推理模型的思考，把上一次的 assistant message（含 `reasoning_details`）
    原样放进去；返回值也会带上本次的 `reasoning_details`。

    `response_format=json_object` 能显著降低「返回一段带 markdown 围栏的
    散文」的概率；供应商不认这个字段时会自动退回普通模式重试一次。
    """
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})

    message, meta = chat_message(settings, messages, timeout=timeout,
                                 request=request, json_mode=True,
                                 return_meta=True)
    content = message.get("content")
    if not isinstance(content, str):
        # 推理模型偶尔只回 reasoning、content 为空；这不能当成合法选项
        raise AIError("模型响应结构不对（content 为空）")

    data = parse_json_content(content)
    # 保留原样的 reasoning_details：调用方续推时直接回传，不做任何改写
    if message.get("reasoning_details") is not None:
        data["reasoning_details"] = message["reasoning_details"]
    # 观测字段：prompt/ completion token 与是否被 max_tokens 截断。
    data["_meta"] = meta
    return data


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
    # 旧字段保留一版，恒为空：避免旧 JSONL 消费者读不到键而报错。
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
    # 本手第几个 hero 决策点。与 replay 的 DecisionPoint.action_index 对齐。
    action_index: int = 0
    # 出这条建议时的桌况快照：table_stats/table_dynamics/roster 版本等。
    # 手牌库/A-B 用它在重跑时复现线上上下文；空 = 旧记录。
    context: dict = field(default_factory=dict)
    # 模型给的下一街意图与自评置信度。新 prompt 不再要求 plan，通常为空。
    plan: str = ""
    confidence: float | None = None
    # 生成这条建议时的中文 lean 提示词版本，复盘归因用。
    prompt_version: str = PROMPT_VERSION
    # 模型返回的结束原因与 token 用量；只有 chat/completions 路径有。
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # 模型用了别名（check/bet-1/3/allin）时记下原词，便于发现 id 歧义。
    alias_from: str | None = None

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
            "prompt_version": self.prompt_version,
            "prompt_lang": PROMPT_LANGUAGE,
            "skill_codes": list(self.skill_codes),
            "duration_ms": self.duration_ms,
            **({"error": self.error} if self.error else {}),
            # 复盘时能回答「当时用了哪个对手的什么类型/置信度/样本数」。
            **({"opponent_reads": reads} if reads else {}),
            **({"action_index": self.action_index}
               if self.action_index else {}),
            **({"context": self.context} if self.context else {}),
            **({"plan": self.plan} if self.plan else {}),
            **({"confidence": round(self.confidence, 3)}
               if self.confidence is not None else {}),
            **({"finish_reason": self.finish_reason}
               if self.finish_reason else {}),
            **({"prompt_tokens": self.prompt_tokens}
               if self.prompt_tokens is not None else {}),
            **({"completion_tokens": self.completion_tokens}
               if self.completion_tokens is not None else {}),
            **({"alias_from": self.alias_from}
               if self.alias_from else {}),
        }


# 模型偶尔会按语义造 id：过牌写成 check、下注写成 bet-1/3、全下写成 allin。
# 这些都是同一个动作，不该因为 id 拼写不同就退化成弃牌/过牌；记下别名，
# 用日志反过来找 prompt 里没写清楚的地方。
_OPTION_ALIASES = {
    "check": "call",
    "allin": "all-in",
    "all_in": "all-in",
    "bet-1/3": "raise-1/3",
    "bet-1/2": "raise-1/2",
    "bet-2/3": "raise-2/3",
    "bet-1": "raise-1",
    "bet-1.2": "raise-1.2",
}


def _canonical_choice(choice: str, by_id: dict) -> tuple[str, str | None]:
    """把常见别名映射回合法 id；返回 (canonical, alias_from)。"""
    raw = (choice or "").strip()
    if raw in by_id:
        return raw, None
    key = raw.lower().replace(" ", "")
    target = _OPTION_ALIASES.get(key)
    if target is None and key.startswith("bet-"):
        target = "raise-" + key[4:]
    if target not in by_id:
        return raw, None
    if key == "check" and by_id[target].kind != "check":
        return raw, None
    if key.startswith("bet") and by_id[target].kind not in ("bet", "raise"):
        return raw, None
    return target, raw


def _clip_reason(text: str, limit: int = 40) -> str:
    """reason 截到 limit 字：优先在标点处断，避免 JSON 被长理由撑爆。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in "。；，、":
        idx = cut.rfind(mark)
        if idx >= limit // 2:
            return cut[:idx + 1]
    return cut[:max(1, limit - 1)] + "…"


def _advice_context(sit: Situation) -> dict:
    """线上建议的上下文快照：重跑/A-B 必须看到和线上同一份桌况。"""
    stats = sit.table_stats
    dynamics = (sit.table_dynamics or "").strip()
    reads = [
        {"seat_id": read.seat_id, "key": read.key,
         "display_name": read.display_name, "archetype": read.archetype,
         "confidence": read.confidence, "n": read.n,
         "source": read.source, "gate": read.gate}
        for read in sit.opponent_reads
    ]
    context = {
        "table_dynamics": dynamics or None,
        "table_dynamics_sha": (hashlib.sha256(dynamics.encode("utf-8")).hexdigest()
                               if dynamics else None),
        "table_stats": pooled_stats_to_dict(stats),
        "table_stats_scope": stats.scope if stats is not None else None,
        "opponent_reads": reads or None,
    }
    return {key: value for key, value in context.items()
            if value not in (None, "", {})}


def advise(sit: Situation, settings: AISettings | None,
           timeout: float = 20.0, complete=None) -> Advice:
    """出一次建议。`complete` 可注入（单测用），默认走 complete_json。

    只有一套中文 lean：不注入 Math gate，不做弃牌重问，不加载技能库。
    """
    started = time.monotonic()
    by_id = {o.id: o for o in sit.options}

    def make(option: Option, reason: str, source: str,
             error: str | None = None, *, plan: str = "",
             confidence: float | None = None,
             alias_from: str | None = None,
             meta: dict | None = None) -> Advice:
        snap = sit.snap
        meta = meta or {}
        usage = meta.get("usage") or {}
        return Advice(snap.hand_id, snap.street, option, reason, source,
                      settings.model if settings else None,
                      (),
                      int((time.monotonic() - started) * 1000),
                      error, snap.pot, snap.to_call,
                      snap.hero_cards, snap.board,
                      decision_key=snap.decision_key,
                      preset_amounts=tuple(preset_amounts(snap).items()),
                      opponent_reads=tuple(sit.opponent_reads),
                      action_index=sit.action_index,
                      context=_advice_context(sit),
                      plan=plan, confidence=confidence,
                      prompt_version=PROMPT_VERSION,
                      finish_reason=meta.get("finish_reason"),
                      prompt_tokens=usage.get("prompt_tokens"),
                      completion_tokens=usage.get("completion_tokens"),
                      alias_from=alias_from)

    if settings is None:
        return make(by_id[fallback_option(sit)],
                    "未配置 AI_SERVER_*，按最保守的默认动作",
                    "fallback", "未配置模型凭据")
    if sit.abnormal_no_opponents:
        # 0 对手异常帧不调模型：任何牌力/赔率/筹码数字都是伪事实。
        return make(by_id[fallback_option(sit)],
                    "状态帧异常（在局对手为 0），按最保守动作",
                    "fallback", "状态帧异常：在局对手为 0")

    system, user = build_messages(sit)
    caller = complete or complete_json

    def ask(prompt: str) -> dict:
        return caller(settings, system, prompt, timeout=timeout)

    try:
        data = ask(user)
        choice, alias_from = _canonical_choice(
            str(data.get("option", "")).strip(), by_id)
        if choice not in by_id:
            raise AIError(f"模型给了不在选项里的答案：{data.get('option')!r}")
        reason = _clip_reason(
            str(data.get("reason", "")).strip() or "（模型未给理由）")
        plan = str(data.get("plan", "") or "").strip()[:160]
        return make(by_id[choice], reason, "model", plan=plan,
                    confidence=_confidence(data.get("confidence")),
                    alias_from=alias_from,
                    meta=data.get("_meta") if isinstance(data, dict) else None)
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
                    advice = advise(sit, self.settings, self.timeout,
                                    self._complete)
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
