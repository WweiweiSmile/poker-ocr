"""翻前开池范围表 —— 把提示词里那张 RFI 表变成可计算的代码。

为什么要做成代码：`_LEAN_SYSTEM` 里写着一张表，但模型是**凭记忆引用**它的，
实测会编出「AJo is in UTG+1's RFI range」这种和表直接矛盾的理由（AJo 到 LJ 才加进来）。
2026-10-23 那局 14 次翻前加注里 12 次是表外的手，合计 −123。所以范围归属不该交给
模型脑算：这里算好，作为**事实**注入提示词，模型只负责在事实之上做选择。

## 表从哪来

原样照抄 `advice.py` 的 `_LEAN_SYSTEM`「Preflop ranges」段，只补了一处：

    · 原表列了 UTG / UTG+1 / LJ / HJ / CO / BTN 六行，但 9 人桌有 **7 个非盲位**
      （`positions.py`: SB BB UTG UTG+1 UTG+2 LJ HJ CO BTN），**UTG+2 整行缺失**。
      模型遇到 UTG+2 只能自己插值。这里补一行，取 UTG+1 与 LJ 的中点
      （55, A9s），并在 `NOTES` 里标注是插值。

其余位置逐行累积（每行在上面的基础上增加），和原文「cumulative」的说法一致。

## 不覆盖的

- **SB / BB**：原文只给 SB 一个频率（~30%），BB 没有 RFI 表。这两个位置返回
  `in_range=None`，提示词里如实说明「不是清单，别当成范围检查」。
- **面对加注时的 3-bet 范围**：只做一档粗判（价值 QQ+/AK），不覆盖原文里的
  诈唬 3-bet（A5s-A2s、KQs、KJs 对晚位开池）。
- 这张表是「~100BB 带 ante」的默认线，不是 GTO。深度差异由 `depth_ranges`
  单独叠加（见 DEPTH_DEEP_BB / DEPTH_SHORT_BB）：深码 EP 收掉被支配的非同花
  高张、后位补同花连张；短码切掉最小的同花连张。调整只动边缘类别，幅度保守 ——
  真要按 300BB 重新标定，需要那一段的 solver 数据，不是靠改几行常量能替代的。
"""

from __future__ import annotations

from dataclasses import dataclass

from .table_dynamics import is_loose_table, is_passive_table

RANKS = "23456789TJQKA"

# 9 人桌的非盲位置，按翻前行动顺序（先说话 → 最后说话）
OPEN_POSITIONS = ("UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN")

# 每一行是**在这一行基础上新增**的手牌（原文写法：cumulative）。
#
# 2026-10-23 按用户口径「松弱跟注站、新手局」整体加宽了一档。加宽的原则是
# **按手牌类别、按位置**，不是无脑塞牌：
#
#   · 对子：跟注站会付钱给你中三条，所以小对子从 LJ 就开始收，BTN 收全部。
#   · 同花 A：坚果同花潜力，LJ 起全收（A2s-A6s）。
#   · 同花连张 / 隔张：成顺成花能收大池，CO 起加 54s/43s 这类。
#   · 非同花：**只加宽到「有张子的高张」为止**（AJo+/ATo+/KJo/KTo），
#     不加 A2o-A8o、76o 这类 —— 跟注站会用更好的 A 跟，那是反向隐含赔率。
#   · 位置越后越宽：EP 只加一档（翻后多路又没位置），LP 加得最狠
#     （有位置 + 对手付钱）。
#
# 开池频率（组合加权，1326 手为分母）：UTG 12.1% → BTN 43.0%。
CHART_ADDITIONS: dict[str, tuple[str, ...]] = {
    "UTG": ("55+", "A9s+", "KTs+", "QTs+", "JTs", "T9s", "98s", "AJo+", "KQo"),
    "UTG+1": ("44", "A8s", "K9s", "Q9s", "J9s", "87s", "ATo+"),
    # ⚠ 原文没有这一行，取 UTG+1 与 LJ 的中点（见模块 docstring）
    "UTG+2": ("33", "A7s", "K8s", "Q8s", "J8s", "T8s", "76s", "KJo"),
    "LJ": ("22", "A2s-A6s", "K7s", "Q7s", "J7s", "T7s", "97s", "86s", "65s",
           "KTo", "QJo"),
    "HJ": ("K6s", "Q6s", "J6s", "T6s", "96s", "85s", "75s", "64s", "A9o", "K9o"),
    "CO": ("K5s", "Q5s", "J5s", "T5s", "95s", "84s", "74s", "54s", "43s",
           "A8o", "K8o", "Q9o", "J9o", "T9o"),
    "BTN": ("K4s", "Q4s", "J4s", "T4s", "94s", "63s", "53s",
            "A7o", "K7o", "Q8o", "J8o", "T8o", "98o"),
}

NOTES = {
    "UTG+2": "原文 RFI 表没有 UTG+2 这一行，这里按 UTG+1 与 LJ 的中点插值",
}

# ── 筹码深度 ──────────────────────────────────────────────────────
# RFI 表按 ~100BB 标定。深度只动**边缘类别**，不动核心牌：
#   · 深码（>150BB）：EP 切掉被支配的非同花高张 —— 深码把「被支配」的代价
#     从一条街放大到三条街；后位补进同花连张/隔张 —— 隐含赔率随深度上涨。
#   · 短码（<40BB）：切掉最小的同花连张/隔张 —— 没有足够后手兑现听牌。
#
# 为什么用「类别」而不是逐个手牌调：这张表本身是松弱桌的加宽版，不是 GTO；
# 按类别调可以在没有 300BB solver 数据的前提下，保证方向正确、幅度保守。
DEPTH_DEEP_BB = 150.0
DEPTH_SHORT_BB = 40.0
EARLY_POSITIONS = ("UTG", "UTG+1", "UTG+2")

# 深码下 EP 不再开池：被支配的非同花高张。
DEEP_DROP = frozenset({"AJo", "ATo", "KJo", "KQo", "QJo", "KTo"})

# 深码下后位额外收：同花连张/隔张，靠隐含赔率，需要位置兑现。
DEEP_ADD: dict[str, tuple[str, ...]] = {
    "CO": ("64s", "53s"),
    "BTN": ("64s", "42s", "32s"),
}

# 短码下切掉：最小的同花连张/隔张。
SHORT_DROP = frozenset({"54s", "65s", "76s", "86s", "97s", "87s"})

# 原文「3-bet: value QQ+/AK; bluffs A5s-A2s, KQs, KJs vs late opens」
THREE_BET_VALUE = ("QQ+", "AK")
THREE_BET_BLUFF = ("A5s-A2s", "KQs", "KJs")


def _pairs(lo: str) -> set[str]:
    """'7' → 77,88,...,AA。"""
    return {r * 2 for r in RANKS[RANKS.index(lo):]}


def _suited(first: str, lo: str, hi: str | None = None) -> set[str]:
    """A 起头、第二张从 lo 到 hi（含）的同花。键是高牌在前：AJs / T9s。"""
    end = hi or RANKS[RANKS.index(first) - 1]
    out = set()
    for r in RANKS[RANKS.index(lo):RANKS.index(end) + 1]:
        if r == first:
            continue
        out.add(_key(first, r, "s"))
    return out


def _offsuit(first: str, lo: str, hi: str | None = None) -> set[str]:
    end = hi or RANKS[RANKS.index(first) - 1]
    out = set()
    for r in RANKS[RANKS.index(lo):RANKS.index(end) + 1]:
        if r == first:
            continue
        out.add(_key(first, r, "o"))
    return out


def _key(first: str, second: str, kind: str) -> str:
    """高牌在前的两手牌键：('A','J','s') → 'AJs'。"""
    a, b = (first, second) if RANKS.index(first) >= RANKS.index(second) \
        else (second, first)
    return f"{a}{b}{kind}"


def expand(token: str) -> set[str]:
    """把 '77+' / 'AJs+' / 'A2s-A7s' / 'KQs' 展开成具体的两手牌键。

    键的形状是 'AA' / 'AJs' / 'AQo'，高牌在前。非法写法返回空集（宁可漏判，不误判）。
    """
    token = token.strip()
    if not token:
        return set()

    # A2s-A7s：同一张 A，第二张从一个点数到另一个点数
    if "-" in token:
        left, right = token.split("-", 1)
        if (len(left) == 3 and len(right) == 3
                and left[0] == right[0] and left[2] == right[2]):
            first, lo, hi = left[0], left[1], right[1]
            fn = _suited if left[2] == "s" else _offsuit
            return fn(first, lo, hi)
        return set()

    # 77+：对子往上
    if token.endswith("+") and len(token) == 3 and token[0] == token[1]:
        return _pairs(token[0])

    # AJs+ / K9s+ / AQo+
    if token.endswith("+") and len(token) == 4:
        first, lo, kind = token[0], token[1], token[2]
        fn = _suited if kind == "s" else _offsuit
        return fn(first, lo)

    # 字面量：KQs / KQo / 55
    if len(token) == 2 and token[0] == token[1]:
        return {token}
    if len(token) == 3 and token[2] in "so":
        first, second = token[0], token[1]
        if RANKS.index(first) < RANKS.index(second):
            first, second = second, first
        return {"".join((first, second)) + token[2]}
    return set()


def _build() -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    acc: set[str] = set()
    for pos in OPEN_POSITIONS:
        for tok in CHART_ADDITIONS[pos]:
            acc |= expand(tok)
        out[pos] = frozenset(acc)
    return out


RFI: dict[str, frozenset[str]] = _build()


def _expand_many(tokens) -> set[str]:
    out: set[str] = set()
    for tok in tokens:
        out |= expand(tok)
    return out


def depth_ranges(stack_bb: float | None) -> dict[str, frozenset[str]]:
    """按筹码深度把整张 RFI 表调整一次。

    返回的字典和 `RFI` 同构，`range_verdict` 用它同时算 `in_range` 和
    `first_position` —— 两处必须用同一张表，否则会出现「在范围内但最早
    进入的位置是空」这种自相矛盾的事实。
    """
    if stack_bb is None or DEPTH_SHORT_BB <= stack_bb <= DEPTH_DEEP_BB:
        return RFI
    if stack_bb > DEPTH_DEEP_BB:
        out: dict[str, frozenset[str]] = {}
        for pos, base in RFI.items():
            adjusted = set(base)
            if pos in EARLY_POSITIONS:
                adjusted -= DEEP_DROP
            adjusted |= _expand_many(DEEP_ADD.get(pos, ()))
            out[pos] = frozenset(adjusted)
        return out
    return {pos: frozenset(base - SHORT_DROP) for pos, base in RFI.items()}


def depth_note(stack_bb: float | None) -> str:
    """给模型看的一句深度口径；没有调整时返回空串。"""
    if stack_bb is None:
        return ""
    if stack_bb > DEPTH_DEEP_BB:
        return (f"按深码 {stack_bb:.0f}BB 调整过：前位切掉被支配的非同花高张，"
                f"后位补进同花连张")
    if stack_bb < DEPTH_SHORT_BB:
        return f"按短码 {stack_bb:.0f}BB 调整过：切掉最小的同花连张"
    return ""


def chart_text(width: int = 86) -> str:
    """把 CHART_ADDITIONS 渲染成提示词里的那几行（按宽度折行）。

    提示词里的表**由这里生成**，不再手写 —— 手写两份一定会漂移，
    而模型拿到的表和代码判定不一致时，它会挑对自己有利的那份用。
    """
    clauses: list[str] = []
    for i, pos in enumerate(OPEN_POSITIONS):
        body = ", ".join(CHART_ADDITIONS[pos])
        clauses.append(f"{pos} {body};" if i == 0 else f"{pos} add {body};")
    lines: list[str] = []
    cur = ""
    for c in clauses:
        if cur and len(cur) + 1 + len(c) > width:
            lines.append(cur)
            cur = c
        else:
            cur = f"{cur} {c}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def open_frequency(position: str) -> float:
    """该位置开池范围占全部起手牌的百分比（组合加权，分母 1326）。"""
    hands = RFI.get(position.upper())
    if not hands:
        return 0.0
    total = 0
    for h in hands:
        total += 6 if len(h) == 2 else (4 if h[2] == "s" else 12)
    return total / 1326 * 100


def is_small_pair(key: str) -> bool:
    """'44' → True；'77' / 'AJo' → False。

    「小对子」按提示词里的例外定义：22–66。它们不在靠前位置的开池表里，
    但允许为 1BB 的 over-limp 抽三条（见中文 lean 的「翻前信息」事实段）。
    """
    return len(key) == 2 and key[0] == key[1] and key[0] in "23456"


def is_set_mine_pair(key: str) -> bool:
    """22-99：只按「买 set」玩的口袋对；TT+ 才算超对价值。

    2026-10-23 用户按当前松弱桌定的口径：平均 3-4 人底池，TT 以下的口袋对
    没中 set 时太容易被歪中的高张统治 —— 99 翻牌约 79% 至少见一张高张，
    77 约 92%。所以 77-99 不能再按「超对=价值牌」打光，和 22-66 一样按
    set-mine 处理（可以小加注拿主动，但翻后没中 set 就是中等牌）。
    """
    return len(key) == 2 and key[0] == key[1] and key[0] in "23456789"


def is_speculative(key: str) -> bool:
    """小同花 A / 同花连张、隔张 —— 赚隐含赔率的牌，不是价值牌。

    A4s 这类牌翻牌听花只有 10.9%、河牌成花 6.4%，顺子只有轮子；绝大多数
    翻牌只是 A 高或弱对。它在收紧后的隔离范围里（1-2 个 limper），但 raise
    的理由是「死钱 + 主动权、预期多人池」，不是「价值隔离」—— 松弱跟注站
    不会弃牌，隔离不了。它真正的价值是坚果同花听牌的隐含赔率，而这恰恰
    偏好便宜的多人池。
    """
    if len(key) != 3 or key[2] != "s":
        return False
    hi, lo = key[0], key[1]
    if hi == "A" and lo in "23456789":      # A2s-A9s；ATs+ 算价值
        return True
    if hi not in "AKQ" and RANKS.index(hi) - RANKS.index(lo) <= 2:
        return True                          # 同花连张/隔张：54s-JTs、64s-J9s
    return False


def hand_key(cards) -> str:
    """('Jh','Ks') → 'KJo'。"""
    a, b = cards[0], cards[1]
    ra, rb = a[0].upper(), b[0].upper()
    if ra == "1":                       # '10h' 这种写法
        ra = "T"
    if rb == "1":
        rb = "T"
    hi, lo = (ra, rb) if RANKS.index(ra) >= RANKS.index(rb) else (rb, ra)
    if ra == rb:
        return hi + lo
    return f"{hi}{lo}{'s' if a[-1] == b[-1] else 'o'}"


def sb_completable(cards) -> bool:
    """小盲花 0.5BB 补盲值不值 —— **看能不能翻出可以下注的东西，不看赔率**。

    为什么不能只看赔率：0.5BB 进 8BB 的池是 17:1，只需要 6% 权益，而任何两张牌
    的**原始**权益都有 30%（单挑口径）。但那是「能打到摊牌」的胜率 —— 杂色垃圾
    在多人池里没有位置、没有听牌，翻中一对还常常是第二好的对子，**可实现权益**
    远低于原始权益，反向隐含赔率还会把差额再吃掉。

    实操判据（能翻出可下注的牌面才算值）：
      · 同花 —— 有同花听
      · 对子 —— 有暗三条
      · 两张都是 T+ —— 有顶对强踢
      · 连张（gap ≤ 2 且最小 ≥ 5）—— 有顺子听
      · A 配 J 以上的踢脚 —— 顶对能站住
    """
    a, b = cards[0].upper(), cards[1].upper()
    ra = "T" if a.startswith("1") else a[0]
    rb = "T" if b.startswith("1") else b[0]
    ia, ib = RANKS.index(ra), RANKS.index(rb)
    gap = abs(ia - ib)
    if a[-1] == b[-1]:                      # 同花
        return True
    if ra == rb:                            # 对子
        return True
    if ia >= 8 and ib >= 8:                 # 两张 T+
        return True
    if gap <= 2 and min(ia, ib) >= 3:       # 连张，最小到 5
        return True
    if "A" in (ra, rb) and max(ia, ib) >= 9 and min(ia, ib) >= 9:
        return True                         # AJ+ （A 配 J 以上踢脚）
    return False


def has_low_straight_potential(cards) -> bool:
    """43o / 32o / 42o / 53o：有顺子结构，但只能成**最低端**的顺。

    不能说它们「没有顺子潜力」—— 43 能成 A-5、2-6、3-7，但都是底端顺，
    常被更高的顺和同花反超。`sb_completable` 因此把「最小 ≥ 5」作为门槛；
    这里单独把这一类挑出来，是为了让弃牌理由说对话，而不是把 43o 说成垃圾。
    """
    a, b = cards[0].upper(), cards[1].upper()
    ra = "T" if a.startswith("1") else a[0]
    rb = "T" if b.startswith("1") else b[0]
    try:
        ia, ib = RANKS.index(ra), RANKS.index(rb)
    except ValueError:
        return False
    return abs(ia - ib) <= 2 and min(ia, ib) < 3


@dataclass(frozen=True)
class Verdict:
    """这手牌在该位置的开池归属，以及「有 limper 时的隔离归属」。

    `in_range` 是 RFI（无人入池）口径；`isolation_ok` 是「前面有 N 个 limper」
    的口径 —— 用 N 个更早位置的 RFI 范围近似：人越多，能隔离的牌越强。

    在隔离范围里的牌还分两类：`is_speculative` 为真的投机牌（小同花 A、
    同花连张/隔张）只给「死钱 + 主动权、预期多人池」的理由，不给
    「value/isolation」—— 对松弱跟注站既隔离不了，也不是价值牌。
    `is_set_mine_pair` 为真的 22-99 只给 set-mine 口径：可以小加注拿主动，
    但没中 set 的超对是中等牌，不按价值牌打光。

    `loose` 与 `passive` 是两维：只有 `loose and passive` 才写
    "Loose-passive / raises are rare"；单纯「偏松」只写「跟注宽」，
    不能替用户断言「加注很少」。
    """

    hand: str
    position: str
    in_range: bool | None            # None = 该位置没有清单（SB/BB）
    first_position: str | None        # 最早包含它的位置
    note: str = ""
    loose: bool = False              # 桌风明确「松」（入池宽，不等于被动）
    passive: bool = False            # 桌风明确「被动/加注少」；loose+passive 才是松弱
    limpers: int = 0                 # 我行动前已经溜入的人数
    isolation_position: str | None = None   # 收紧后的参照位置
    isolation_ok: bool | None = None        # 这手牌在收紧后的范围里？
    depth_note: str = ""             # 按筹码深度调过范围时的口径

    def render(self) -> str:
        if self.in_range is None:
            return f"Preflop range: {self.hand} from {self.position} — {self.note}"
        if self.limpers > 0 and self.in_range and self.isolation_ok is not None:
            if self.isolation_ok:
                if is_set_mine_pair(self.hand):
                    text = (f"Preflop range: {self.hand} is IN the {self.position} RFI "
                            f"range, and still IN the tighter {self.isolation_position} "
                            f"range over {self.limpers} limper(s). A set-mine pair "
                            f"(22-99) here: over-limp for 1BB, or make a small raise "
                            f"for initiative — do not bloat the pot; an unimproved "
                            f"overpair is only a medium hand multiway.")
                elif is_speculative(self.hand):
                    # 2026-10-23 #990052：A4s CO、1 个 limper，模型把「IN open
                    # range」说成了 value/isolation。对跟注站它隔离不了，也不是
                    # 价值牌 —— 小同花 A / 同花连张赚的是坚果花/顺子的隐含赔率，
                    # 加注的正当理由是死钱 + 主动权，且预期被跟。
                    text = (f"Preflop range: {self.hand} is IN the {self.position} RFI "
                            f"range, and still IN the tighter {self.isolation_position} "
                            f"range over {self.limpers} limper(s) — but this is a "
                            f"SPECULATIVE hand, not a value hand. Raise for initiative "
                            f"and the dead money (antes), expecting calls and a "
                            f"multiway pot; over-limping for 1BB to keep the pot small "
                            f"is equally fine. Do not call it a value isolation.")
                else:
                    text = (f"Preflop range: {self.hand} is IN the {self.position} RFI "
                            f"range, and still IN the tighter {self.isolation_position} "
                            f"range over {self.limpers} limper(s). Raise for "
                            f"value/isolation — but expect calls, so size for value, "
                            f"not to fold them out.")
                if self.loose:
                    if is_speculative(self.hand):
                        if self.passive:
                            text += (" Loose-passive table: they call wide and rarely "
                                     "fold — a raise will not isolate them; it only "
                                     "builds a pot you will often miss. Size small or "
                                     "over-limp.")
                        else:
                            text += (" Loose table: calls are wide — expect calls; "
                                     "size small or over-limp.")
                    elif not is_set_mine_pair(self.hand):
                        if self.passive:
                            text += (" Loose-passive table: they call wide — isolate "
                                     "the hands that flop well multiway, not dominated "
                                     "offsuit aces.")
                        else:
                            text += (" Loose table: they call wide — raise the hands "
                                     "that flop well multiway, not dominated offsuit "
                                     "aces; expect calls, not folds.")
                return text
            # 2026-10-23 h3：A8o CO、3 个 limper，模型看到「IN CO open range」
            # 就开到 11BB；4 家跟注后 A8o 变成弱牌。RFI 表是「无人入池」的表，
            # 不能直接当隔离范围用 —— 有 limper 时要收紧。
            text = (f"Preflop range: {self.hand} is IN the {self.position} RFI range — but "
                    f"that is for an UNOPENED pot. Over {self.limpers} limper(s) the "
                    f"isolation range tightens to {self.isolation_position}, and {self.hand} "
                    f"is OUTSIDE it. Do not isolate big.")
            if is_small_pair(self.hand):
                text += (" A small pocket pair may still over-limp for 1BB to set-mine; "
                         "do not raise it just to isolate.")
            elif is_speculative(self.hand):
                # suited 的投机牌不能套「dominated offsuit」那套理由 —— A4s/A5s
                # 是坚果花潜力，出局时该给的是「便宜看翻牌」，不是「踢脚弱弃掉」。
                text += (" This is a speculative hand (nut-flush / straight implied "
                         "odds): over-limp for 1BB to see a cheap flop, or fold — "
                         "do not raise it just to isolate.")
            elif self.hand.endswith("o"):
                text += (" Dominated offsuit hands play badly multiway (weak kicker, "
                         "reverse implied odds). FOLD, or over-limp for 1BB only if you "
                         "can fold easily when you flop one pair.")
            else:
                text += (" FOLD as a raise; an over-limp for 1BB is a thin alternative "
                         "only on a passive table.")
            return text
        if self.in_range:
            text = (f"Preflop range: {self.hand} is IN the {self.position} open "
                    f"range (first position that opens it: {self.first_position}). "
                    f"Chart says RAISE.")
            if is_set_mine_pair(self.hand):
                # 2026-10-23 用户口径：TT 以下（22-99）都按买 set 玩。
                # 加注可以拿主动，但没中 set 的超对只是中等牌，不能打光。
                text += (" It is a set-mine pair (22-99): raise for initiative, but "
                         "treat an unimproved overpair as a medium hand — no set, "
                         "no big pot.")
                if self.loose:
                    if self.passive:
                        text += (" Loose-passive table: 3-4 way pots are the norm — "
                                 "expect calls and play it as a set-mine.")
                    else:
                        text += (" Loose table: expect calls and play it as a "
                                 "set-mine; do not assume a multiway pot.")
            elif self.loose:
                if is_speculative(self.hand):
                    if self.passive:
                        text += (" Loose-passive table: the chart is a floor here — a "
                                 "raise is fine for initiative and dead money, but this "
                                 "is a speculative hand: expect a multiway pot and do "
                                 "not overplay one pair.")
                    else:
                        text += (" Loose table: the chart is a floor here — a raise is "
                                 "fine for initiative and dead money, but this is a "
                                 "speculative hand: expect calls and do not overplay "
                                 "one pair.")
                else:
                    if self.passive:
                        text += (" Loose-passive table: the chart is a floor here — "
                                 "raise for value/isolation and expect calls.")
                    else:
                        text += (" Loose table: the chart is a floor here — raise for "
                                 "value and expect calls.")
            return text
        if self.first_position:
            text = (f"Preflop range: {self.hand} is OUTSIDE the {self.position} open "
                    f"range — it only enters at {self.first_position}. "
                    f"Chart says FOLD as an open from {self.position}.")
            if is_small_pair(self.hand):
                # 2026-10-23 h13：44 UTG 只看到「Chart says FOLD」就弃了，
                # 理由还写成「limping invites raises」，和松弱桌风正相反。
                # 例外不能只埋在 system prompt 的 SB 段里，必须跟结论一起出现。
                text += (" But a small pocket pair (22-66) may over-limp for 1BB to "
                         "set-mine — that is the chart's explicit exception.")
                if self.loose:
                    # 桌风优先：同样的牌在松弱桌和紧桌是两种默认。h13 的桌风
                    # 明确「很少主动加注」，limp 的加注税很低，弃牌反而更差。
                    if self.passive:
                        text += (" This table is loose-passive (raises are rare): "
                                 "over-limping for 1BB to set-mine is the better "
                                 "default than folding — the chart is too tight here.")
                    else:
                        text += (" This table is loose (calls are wide, but raises "
                                 "still happen): over-limping for 1BB to set-mine is "
                                 "still fine — the chart is too tight here.")
                else:
                    text += (" Check the implied-odds fact and the table dynamics "
                             "before folding.")
            elif self.loose:
                # 松桌不等于什么都能溜：只放宽有听牌/成牌潜力的类别。
                label = "loose-passive" if self.passive else "loose"
                text += (f" This table is {label}: the chart is a default, not a "
                         "ban — but widen only with playable hands (pairs, suited, "
                         "connected), not offsuit trash.")
            return text
        return (f"Preflop range: {self.hand} is OUTSIDE every open range in the chart. "
                f"Chart says FOLD.")


def range_verdict(position: str | None, cards,
                  table_dynamics: str | None = None,
                  limpers: int = 0,
                  stack_bb: float | None = None,
                  loose: bool | None = None,
                  passive: bool | None = None) -> Verdict | None:
    """位置 + 两张手牌 → 范围归属。

    `table_dynamics` 只影响「默认线」的渲染：桌风明确「松」时，开池表按**默认值**
    对待（放宽一档/小对子 over-limp），而不是绝对的 FOLD。它不改变 `in_range`
    本身 —— 事实和解释分开，复盘时仍然能看到「这手牌在标准表里本来不进」。
    只有 `loose and passive` 才写 "Loose-passive / raises are rare"。

    `limpers` > 0 时额外算「隔离归属」：RFI 表是无人入池的表，有 N 个 limper
    就用 N 个更早位置的范围来近似（人越多，能隔离的牌越强）。h3 的 A8o 就是
    只有 `in_range=True`、隔离归属却是 OUT —— 模型不能把前者当开池许可证。
    """
    if not position or not cards or len(cards) != 2:
        return None
    # 桌风优先用调用方给的**算出来的**结论（全场 VPIP / 3-bet）；没有统计时
    # 才退回 `.table_dy` 的关键词。`.table_dy` 现在只写现场观察，关键词经常
    # 匹配不到，所以这个先后顺序决定了松桌结论还灵不灵。
    if loose is None:
        loose = is_loose_table(table_dynamics)
    if passive is None:
        passive = is_passive_table(table_dynamics)
    limpers = max(int(limpers or 0), 0)
    pos = position.strip().upper().replace("+", "+")
    if pos not in RFI:
        if pos == "SB":
            key = hand_key(cards)
            if sb_completable(cards):
                note = (f"{key} can flop something you can bet (same suit / pair / "
                        f"connected / two broadways / AJ+). Completing for 0.5BB is fine; "
                        f"do not raise it.")
            elif has_low_straight_potential(cards):
                # 2026-10-23 h60：43o 的弃牌理由被写成「no straight potential」，
                # 但 43 是有顺子结构的，只是只能成底端顺。理由必须说对。
                note = (f"{key} has only low-end straight potential: 43/32 make the "
                        f"bottom of the straight (A-5, 2-6, 3-7), often beaten by "
                        f"higher straights and flushes. Offsuit and out of position, "
                        f"the 0.5BB price does not pay for the reverse implied odds. "
                        f"FOLD is the lower-variance default; completing is a thin "
                        f"exploit only on a very passive table.")
            else:
                note = (f"{key} is offsuit with no flush and only limited straight "
                        f"potential. Completing only buys second-best one-pair hands "
                        f"out of position — the 0.5BB price does not pay for that. FOLD.")
            return Verdict(key, pos, None, None, note=note, loose=loose,
                           passive=passive, limpers=limpers)
        if pos == "BB":
            return Verdict(hand_key(cards), pos, None, None,
                           note="BB has no open range (already posted) — this is a "
                                "check/defend spot, not an RFI decision",
                           loose=loose, passive=passive, limpers=limpers)
        return None

    key = hand_key(cards)
    # 深度调整后的表：`in_range` 与 `first_position` 必须来自同一张表，
    # 否则会出现「在范围内，但最早进入的位置是空」。
    ranges = depth_ranges(stack_bb)
    first = next((p for p in OPEN_POSITIONS if key in ranges[p]), None)
    isolation_position = None
    isolation_ok = None
    if limpers > 0:
        idx = OPEN_POSITIONS.index(pos)
        isolation_position = OPEN_POSITIONS[max(0, idx - limpers)]
        isolation_ok = key in ranges[isolation_position]
    return Verdict(hand=key, position=pos, in_range=key in ranges[pos],
                   first_position=first, note=NOTES.get(pos, ""), loose=loose,
                   passive=passive, limpers=limpers,
                   isolation_position=isolation_position,
                   isolation_ok=isolation_ok,
                   depth_note=depth_note(stack_bb))
