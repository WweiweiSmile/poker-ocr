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
- 这张表是「~100BB 带 ante」的默认线，不是 GTO；有效筹码短到 20BB 以下时会偏宽。
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class Verdict:
    """这手牌在该位置的开池归属。"""

    hand: str
    position: str
    in_range: bool | None            # None = 该位置没有清单（SB/BB）
    first_position: str | None        # 最早包含它的位置
    note: str = ""

    def render(self) -> str:
        if self.in_range is None:
            return f"Preflop range: {self.hand} from {self.position} — {self.note}"
        if self.in_range:
            return (f"Preflop range: {self.hand} is IN the {self.position} open "
                    f"range (first position that opens it: {self.first_position}). "
                    f"Chart says RAISE.")
        if self.first_position:
            return (f"Preflop range: {self.hand} is OUTSIDE the {self.position} open "
                    f"range — it only enters at {self.first_position}. "
                    f"Chart says FOLD from {self.position}.")
        return (f"Preflop range: {self.hand} is OUTSIDE every open range in the chart. "
                f"Chart says FOLD.")


def range_verdict(position: str | None, cards) -> Verdict | None:
    """位置 + 两张手牌 → 范围归属。牌不全或位置未知时返回 None。"""
    if not position or not cards or len(cards) != 2:
        return None
    pos = position.strip().upper().replace("+", "+")
    if pos not in RFI:
        if pos == "SB":
            key = hand_key(cards)
            if sb_completable(cards):
                note = (f"{key} can flop something you can bet (same suit / pair / "
                        f"connected / two broadways / AJ+). Completing for 0.5BB is fine; "
                        f"do not raise it.")
            else:
                note = (f"{key} is offsuit with no flush and no straight potential. "
                        f"Completing only buys second-best one-pair hands out of "
                        f"position — the 0.5BB price does not pay for that. FOLD.")
            return Verdict(key, pos, None, None, note=note)
        if pos == "BB":
            return Verdict(hand_key(cards), pos, None, None,
                           note="BB has no open range (already posted) — this is a "
                                "check/defend spot, not an RFI decision")
        return None

    key = hand_key(cards)
    first = next((p for p in OPEN_POSITIONS if key in RFI[p]), None)
    return Verdict(hand=key, position=pos, in_range=key in RFI[pos],
                   first_position=first, note=NOTES.get(pos, ""))
