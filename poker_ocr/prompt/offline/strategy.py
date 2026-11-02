"""离线审计用的旧策略层（**不进入 live 提示词**）。

`value_directive` / `math_gate` / `_is_set_mine_pair` 是 2026-10 之前的
「规则层」：由程序直接给价值取向和赔率裁决，再把结论塞进英文事实块。
2026-10-29 起 live 链路只给事实、不给规则，这一层只服务
`render_hand_facts()`（复跑历史记录）与 `audit_advice_tags()`（复盘归因），
所以从 advice.py 拆到 prompt/offline/，名字保持不变，`advice` 仍 re-export。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...coach import (
    DRY, MID_BOTTOM, OVERPAIR, STRONG_DRAW, STRONG_MADE, TPTK, TP_WEAK,
    TWO_PAIR, WEAK_DRAW,
)
from ...hand_track import FLOP, PREFLOP, RIVER, TURN, HandSnapshot
from ...opponent_profile import (
    AGGRESSIVE_ARCHETYPES, PASSIVE_ARCHETYPES, OpponentRead,
)
from ...pot_math import call_amount, winnable_pot
from ...table_dynamics import value_heavy_aggression

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...advice import Situation


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


# ─────────────────────── 价值取向（legacy offline） ───────────────────────

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


# ─────────────────────── 赔率闸门（legacy offline） ───────────────────────

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


# ─────────────────────── 爆炸底池口径 / 尺度选择（legacy offline） ───────────────────────

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


# ─────────────────────── 价值指令（legacy offline） ───────────────────────

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


# ─────────────────────── 口袋对性质 ───────────────────────

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

