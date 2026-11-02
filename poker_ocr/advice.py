"""实时 AI 决策建议：把算好的中文事实交给模型，只回一个选项。

提示词已经按功能/场景拆到 `poker_ocr.prompt`（system / user / shared /
offline），本模块只保留「算事实 + 调模型 + 兜底」：

  · `pot_math`      牌桌几何/赔率/位置的纯计算（有效筹码、SPR、赔率门槛……）
  · `prompt.system` system：输出契约、事实优先级、决策顺序、指标口径、
                    筹码深度，以及按局面追加的条件场景段（前注局……）
  · `prompt.user`   user 事实块：当前局面、行动线、翻前、实时牌力、范围、
                    对手画像、桌况统计、人工观察、攻击/剥削、数据质量、选项
  · `prompt.offline` 旧英文提示词 + Value directive/Math gate，只服务离线审计

为什么输出是「选项编号」而不是自由文本：

  · 实时场景下人要的是一秒内能执行的动作，不是分析文章。
  · 选项把尺度约束死了（1/3、1/2、2/3、1、1.2 倍池、all-in），模型不可能
    编出不存在的尺度。
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

import hashlib
import http.client
import io
import json
import os
import re
import threading
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Iterable

from .coach import (
    DRY, HIGH_CARD, MID_BOTTOM, ONE_PAIR, OVERPAIR, STRAIGHT, STRONG_DRAW,
    STRONG_MADE, TPTK, TP_WEAK, TWO_PAIR,
    WEAK_DRAW, board_texture, opponent_strength, parse_cards, read_hand,
)
from .ground_truth import GroundTruth, compute_ground_truth
from .hand_track import FLOP, HandSnapshot, PREFLOP
from .opponent_profile import OpponentRead, PooledStats, pooled_stats_to_dict
from .positions import BB, SB
from .pot_math import (
    EXPLOSIVE_POT_BB_PER_PLAYER, EXPLOSIVE_POT_TOLERANCE,
    PREFLOP_MIN_RAISE_BB, _action_order, _big_blind_option_pending, _can_act,
    _effective_stack, _hero_total_stack, _no_opponent_anomaly,
    _opponent_total_stack, _playable_depth, _same_actor, _street_bet,
    _preflop_raise_count, _unknown_stack_opponents, behind_opponents,
    call_amount, count_limpers,
    hero_first_to_act, hero_in_position, is_explosive_pot, players_left_to_act,
    pot_per_player, pot_type, previous_street_pot, spr, uncalled_excess,
    winnable_pot,
)
from .preflop_ranges import range_verdict
from .table_dynamics import (is_aggressive_table, is_loose_table,
                             is_passive_table, value_heavy_aggression)

# ── 提示词：拆到 poker_ocr.prompt，这里 re-export 保持旧调用口径 ──
from .prompt import (  # noqa: F401
    PROMPT_LANGUAGE, PROMPT_VERSION, build_messages, build_system,
    render_facts_zh,
)
from .prompt.offline.audit import audit_advice_tags  # noqa: F401
from .prompt.offline.hand_facts import render_hand_facts  # noqa: F401
from .prompt.offline.strategy import (  # noqa: F401
    DOMINATED_BEAT_PCT, IMPLIED_ODDS_CAP_PCT, MathGate, _aggressive_context,
    _no_fold_equity, _pick_size, decision_read, math_gate, value_directive,
)
from .prompt.shared.labels import _TEXTURE_EN
from .prompt.user.action_line import _fmt_actions_zh  # noqa: F401
from .prompt.user.attack import render_attack_zh  # noqa: F401
from .prompt.user.explosive import render_explosive_zh  # noqa: F401
from .prompt.user.notes import _notes_ace_weakness  # noqa: F401
from .prompt.user.options import render_options_zh  # noqa: F401
from .prompt.user.preflop import render_preflop_zh  # noqa: F401
from .prompt.user.quality import _gap_zh, _summarize_gaps  # noqa: F401
from .prompt.user.ranges import render_ranges_zh  # noqa: F401
from .prompt.user.strength import render_hand_strength_zh  # noqa: F401
from .prompt.user.table_stats import (  # noqa: F401
    _table_stab_read_zh, render_table_stats_zh,
)

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
        n_raises = _preflop_raise_count(snap)
        if n_raises >= 1:
            bet_level = n_raises + 2
        else:
            bet_level = 4 if "three-bet" in (snap.preflop_shapes or ()) else 3
        prefix = f"{bet_level}-bet"
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
        return (f"{verdict.hand} 在 {verdict.position} 没有开池范围"
                "（盲注位按补盲/过牌/防守处理）")
    if verdict.in_range:
        text = f"{verdict.hand} 在 {verdict.position} 的开池范围内"
    else:
        text = f"{verdict.hand} 不在 {verdict.position} 的开池范围内"
    if verdict.limpers > 0 and verdict.isolation_ok is not None:
        iso = (f"收紧范围（≈{verdict.isolation_position}）仍包括它"
               if verdict.isolation_ok
               else f"收紧范围（≈{verdict.isolation_position}）已不包括它")
        text += (f"；前面有 {verdict.limpers} 个溜入者，{iso} → "
                 + ("可以加注隔离，不要平跟" if verdict.isolation_ok
                    else "默认弃牌，不要平跟"))
    elif verdict.in_range:
        text += " → 可以开池加注，不要平跟"
    else:
        text += " → 默认弃牌"
    if verdict.depth_note:
        text += f"（{verdict.depth_note}）"
    return text
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
    if (snap.street == PREFLOP and not snap.actions_of(PREFLOP)
            and not snap.bomb_pot):
        warnings.append(
            "翻前动作线缺失，翻前形态/PFR 未知（按未知处理）")
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

    # ── 复盘/手牌库：真正发给模型的 prompt 与原始响应 ──
    # prompt_version 只是版本号，下面的字段才是「模型当时看到了什么」。
    # 旧 JSONL 消费者会忽略这些键；空值不写入 to_dict()。
    prompt_system: str | None = None
    prompt_user: str | None = None
    prompt_sha256: str | None = None
    raw_response: str | None = None
    response_sha256: str | None = None
    reason_full: str | None = None
    facts: dict = field(default_factory=dict)
    model_params: dict = field(default_factory=dict)
    captured_at: str | None = None
    prompt_missing: bool = False
    prompt_missing_reason: str | None = None
    attempt: int = 1

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
            # 只有真的捕获到才写，保持旧记录形状稳定。
            **({"prompt_system": self.prompt_system}
               if self.prompt_system else {}),
            **({"prompt_user": self.prompt_user}
               if self.prompt_user else {}),
            **({"prompt_sha256": self.prompt_sha256}
               if self.prompt_sha256 else {}),
            **({"raw_response": self.raw_response}
               if self.raw_response else {}),
            **({"response_sha256": self.response_sha256}
               if self.response_sha256 else {}),
            **({"reason_full": self.reason_full}
               if self.reason_full else {}),
            **({"facts": self.facts} if self.facts else {}),
            **({"model_params": self.model_params}
               if self.model_params else {}),
            **({"captured_at": self.captured_at}
               if self.captured_at else {}),
            **({"prompt_missing": True} if self.prompt_missing else {}),
            **({"prompt_missing_reason": self.prompt_missing_reason}
               if self.prompt_missing_reason else {}),
            **({"attempt": self.attempt} if self.attempt and self.attempt != 1
               else {}),
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


def _sha256_text(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_digest(system: str, user: str) -> str:
    """与 replay 侧同一口径：system + 固定分隔符 + user。"""
    return hashlib.sha256(
        (system + "\n---\n" + user).encode("utf-8")).hexdigest()


def _facts_snapshot(sit: Situation) -> dict:
    """只服务复盘/审计的轻量事实快照。

    不 import replay.decisions，避免 live 反向依赖控制台；字段与
    `DecisionPoint` 的 facts 保持同名/同义，方便前端并排看。
    """
    snap = sit.snap
    facts = {
        "hand_tier": sit.hand_tier,
        "hand_label": sit.hand_label,
        "hand_label_en": sit.hand_label_en,
        "equity_pct": (round(sit.equity_pct, 1)
                       if sit.equity_pct is not None else None),
        "pot": snap.pot,
        "to_call": round(float(snap.to_call or 0.0), 2),
        "hero_stack": snap.hero_stack,
        "pot_type": sit.pot_type,
        "spr": sit.spr,
        "opponents_in_hand": sit.opponents_in_hand,
        "range_verdict": sit.range_verdict,
        "explosive_pot": bool(sit.explosive_pot),
        "bomb_pot": bool(snap.bomb_pot),
        "warnings": list(sit.warnings or ()),
        "options": [option.to_dict() for option in sit.options],
    }
    return {key: value for key, value in facts.items()
            if value not in (None, "", [], {})}


def _model_params(settings: AISettings | None) -> dict:
    """可公开的采样参数快照；永远不写 api_key。"""
    if settings is None:
        return {}
    params = {
        "model": settings.model,
        "base_url": settings.base_url,
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "max_tokens": settings.max_tokens,
        "thinking": settings.thinking,
        "reasoning_effort": settings.reasoning_effort,
        "reasoning": settings.reasoning,
    }
    return {key: value for key, value in params.items()
            if value is not None and value != ""}


def advise(sit: Situation, settings: AISettings | None,
           timeout: float = 20.0, complete=None,
           prompt_override: tuple[str, str] | None = None) -> Advice:
    """出一次建议。`complete` 可注入（单测用），默认走 complete_json。

    只有一套中文 lean：不注入 Math gate，不做弃牌重问，不加载技能库。
    模型调用发生的那一刻，把 system/user/raw/reason_full 一起带进 Advice；
    没有调用模型（未配置/异常帧）时显式标 prompt_missing。

    `prompt_override=(system, user)` 用于 A/B 的 baseline：直接重放线上/历史
    prompt 原文，跳过 `build_messages()`；选项校验、reason 截断、fallback
    仍走同一套逻辑。
    """
    started = time.monotonic()
    by_id = {o.id: o for o in sit.options}
    prompt_holder: dict[str, str | None] = {}
    raw_holder: dict[str, str | None] = {}

    def make(option: Option, reason: str, source: str,
             error: str | None = None, *, plan: str = "",
             confidence: float | None = None,
             alias_from: str | None = None,
             meta: dict | None = None,
             prompt_missing: bool = False,
             prompt_missing_reason: str | None = None,
             reason_full: str | None = None) -> Advice:
        snap = sit.snap
        meta = meta or {}
        usage = meta.get("usage") or {}
        raw = raw_holder.get("raw")
        system = prompt_holder.get("system")
        user = prompt_holder.get("user")
        return Advice(
            snap.hand_id, snap.street, option, reason, source,
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
            alias_from=alias_from,
            prompt_system=system,
            prompt_user=user,
            prompt_sha256=prompt_holder.get("sha"),
            raw_response=raw,
            response_sha256=_sha256_text(raw),
            reason_full=reason_full or reason,
            facts=_facts_snapshot(sit),
            model_params=_model_params(settings),
            captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            prompt_missing=prompt_missing,
            prompt_missing_reason=prompt_missing_reason,
        )

    if settings is None:
        return make(by_id[fallback_option(sit)],
                    "未配置 AI_SERVER_*，按最保守的默认动作",
                    "fallback", "未配置模型凭据",
                    prompt_missing=True,
                    prompt_missing_reason="no_settings")
    if sit.abnormal_no_opponents:
        # 0 对手异常帧不调模型：任何牌力/赔率/筹码数字都是伪事实。
        return make(by_id[fallback_option(sit)],
                    "状态帧异常（在局对手为 0），按最保守动作",
                    "fallback", "状态帧异常：在局对手为 0",
                    prompt_missing=True,
                    prompt_missing_reason="abnormal_no_opponents")

    system, user = (prompt_override if prompt_override
                    else build_messages(sit))
    prompt_holder.update({
        "system": system,
        "user": user,
        "sha": _prompt_digest(system, user),
    })
    caller = complete or complete_json

    def ask(prompt: str) -> dict:
        data = caller(settings, system, prompt, timeout=timeout)
        try:
            raw_holder["raw"] = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_holder["raw"] = str(data)
        return data

    try:
        data = ask(user)
        choice, alias_from = _canonical_choice(
            str(data.get("option", "")).strip(), by_id)
        if choice not in by_id:
            raise AIError(f"模型给了不在选项里的答案：{data.get('option')!r}")
        raw_reason = " ".join(str(data.get("reason", "")).split())
        reason = _clip_reason(raw_reason or "（模型未给理由）")
        plan = str(data.get("plan", "") or "").strip()[:160]
        return make(by_id[choice], reason, "model", plan=plan,
                    confidence=_confidence(data.get("confidence")),
                    alias_from=alias_from,
                    meta=data.get("_meta") if isinstance(data, dict) else None,
                    reason_full=raw_reason or None)
    except AIError as exc:
        # prompt 已经构建/发出去过，必须保留；raw 可能为空（连接失败）。
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
