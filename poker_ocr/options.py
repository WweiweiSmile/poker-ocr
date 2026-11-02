"""选项空间：把当前局面能做的动作算成固定 id + 中文标签 + 实际金额。

模型只从 `build_options()` 给出的 id 里选，所以「尺度」不由模型生成：
翻后按底池倍数（1/3、1/2、2/3、1、1.2 倍池）算好金额，翻前按位置/前注/
溜入者算开池与 3-bet/4-bet 金额，all-in 只有在几何合理时才挂进来。

这一层只读 `HandSnapshot` 和牌桌规则，不调模型、不渲染提示词；提示词里的
「## 可选动作」由 `prompt/user/options.py` 渲染，点击器用 `preset_targets()`。
从 advice.py 拆出（2026-11-03 第二次拆分），`advice` 仍 re-export 全部名字。
"""

from __future__ import annotations

from dataclasses import dataclass

from .hand_track import PREFLOP, HandSnapshot
from .pot_math import (
    PREFLOP_MIN_RAISE_BB, _effective_stack, _hero_total_stack,
    _preflop_raise_count, call_amount, winnable_pot,
)


# ─────────────────────────── 选项 ───────────────────────────

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
        total_stack = _hero_total_stack(snap)
        short_stack = (total_stack is not None
                       and total_stack <= PREFLOP_ALLIN_MAX_STACK_BB)
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
        note = (f"本手总筹码 {hero_max:g}BB；有效筹码只有 {effective:g}BB，"
                f"多出的 {hero_max - effective:g}BB 不会被跟（退回）")
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
