"""翻前手牌分类：书层卡片的确定性派生维度。

翻前没有公共牌，`situation._read` 直接返回 None，所以 `Spot.made_hand` 在翻前
永远是 unknown。书里的翻前计划不是按「牌力未知」一刀切，而是按牌型分的：
大口袋对、中口袋对、小口袋对、AK、AQ、其他 A、麻烦牌型、同花连张、投机牌。
少了这一维，任何翻前卡都只能对任何两张牌说同一句话 —— 要么全加注（把 72o
也加注），要么全弃（把 AA 也弃）。

这里只回答「书把它归到哪一类」，不判断强弱、不做范围表：范围表是
`poker_ocr.preflop_ranges` 的事（按位置/筹码深度给「加注/跟注/弃牌」的判决），
强弱是翻后 `made_hand` 的事。两套分类词面相近但口径不同，**不要**把这里的类
当成范围表的桶：书里「小口袋对」是 55-22，范围表的 `is_small_pair` 是 22-66
的抽三条对象；书里「投机牌」只指非同花连张与同花隔张，范围表把同花连张与
Axs 也算投机牌。同一手牌在任何位置、任何筹码深度下都是同一个类，位置由
`position_bucket` 单独给。

分类边界跟书走（Harrington 第 1 卷第三章），不按「强弱直觉」切：

- 中口袋对再分两段，因为书 p110 给了两套频率：JJ-99 前位 70% 加注、
  88-66 前位 30% 加注；p129 攻击溜入者时只把 AA-TT 算作加注牌。
- 麻烦牌型再分两段，因为书 p120 把 KQ/KJ/QJ 与 KT/QT 分开讲：后者中位
  直接弃。
- 大口袋对再分两段（#91）：面对 3-bet 时书 p109 让 AA/KK 继续再加注，QQ 只
  到「最多跟注」；同一句「大口袋对」会把 QQ 也说成 4-bet。
"""

from __future__ import annotations

from ..positions import BB, BTN, CO, HJ, LJ, SB, UTG, UTG1, UTG2
from ..preflop_ranges import RANKS, hand_key

# 书里逐个讨论过的牌型（Harrington 第 1 卷第三章）。这里只是词表 + 成员校验，
# 顺序不承载优先级：分类由 `preflop_hand_class` 里的规则决定。
PREFLOP_HAND_CLASSES = (
    "big_pair_aa_kk",    # AA KK（书：大口袋对里可以 4-bet 的两手）
    "big_pair_qq",       # QQ（书：面对 3-bet 最多跟注）
    "medium_pair",       # JJ TT 99（书：中口袋对里较大的一段，前位 70% 加注）
    "low_pair",          # 88 77 66（书：中口袋对里较小的一段，前位 30% 加注）
    "small_pair",        # 55 44 33 22（书：小口袋对，抽三条）
    "ak",
    "aq",
    "ace_broadway",      # AJ / AT：书里单列（前位只玩 AJs）
    "weak_ace",          # A9-A2：书里 Ax 的更弱一段
    "trouble",           # KQ KJ QJ（书：麻烦牌型，前位只玩同花）
    "weak_trouble",      # KT QT（书：中位直接弃的更弱一段）
    "suited_connector",  # JT-54 同花连张（书：同花连牌）
    "speculative",       # 非同花连张 / 同花隔张（书：其他可玩牌型）
    "other",             # 书里没列的牌
)

# 牌桌位置 -> 书里的行动分区。书按「前位 / 中位 / 后位」讲，盲注单列。
POSITION_BUCKETS = ("early", "middle", "late", "blind")

# 座位名一律取自 poker_ocr.positions，不在这里重抄一遍：那边新增座位时
# tests/test_playbook_hand_class.py 会先红，而不是静默落到 None。
_POSITION_BUCKET = {
    UTG: "early",
    UTG1: "early",
    UTG2: "early",
    LJ: "middle",
    HJ: "middle",
    CO: "late",
    BTN: "late",
    SB: "blind",
    BB: "blind",
}

_PAIRS_BIG = frozenset({"AA", "KK"})
_PAIRS_BIG_QQ = frozenset({"QQ"})
_PAIRS_MEDIUM = frozenset({"JJ", "TT", "99"})
_PAIRS_LOW = frozenset({"88", "77", "66"})
_PAIRS_SMALL = frozenset({"55", "44", "33", "22"})
_TROUBLE = frozenset({"KQ", "KJ", "QJ"})
_WEAK_TROUBLE = frozenset({"KT", "QT"})

_J_INDEX = RANKS.index("J")
_4_INDEX = RANKS.index("4")


def _split(key: str) -> tuple[str, str, str]:
    """'AKo' -> ('A', 'K', 'o')；'55' -> ('5', '5', '')。"""
    hi, lo = key[0], key[1]
    return hi, lo, key[2:] if len(key) > 2 else ""


def preflop_hand_class(cards) -> str | None:
    """('Ah','Ks') / ['Ah','Ks'] -> 'ak'；算不出返回 None。

    只认两张牌；hero_cards 缺失、长度不对、点数认不出一律 None —— fail open，
    让卡片匹配退化成「书中无此局面」，而不是猜一个类。
    """
    if not cards or len(cards) != 2:
        return None
    try:
        key = hand_key(cards)
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    if len(key) < 2 or key[0] not in RANKS or key[1] not in RANKS:
        return None

    hi, lo, suited = _split(key)
    if hi == lo:
        if key in _PAIRS_BIG:
            return "big_pair_aa_kk"
        if key in _PAIRS_BIG_QQ:
            return "big_pair_qq"
        if key in _PAIRS_MEDIUM:
            return "medium_pair"
        if key in _PAIRS_LOW:
            return "low_pair"
        return "small_pair"
    if (hi, lo) == ("A", "K"):
        return "ak"
    if (hi, lo) == ("A", "Q"):
        return "aq"
    if hi == "A":
        return "ace_broadway" if lo in ("J", "T") else "weak_ace"
    if hi + lo in _TROUBLE:
        return "trouble"
    if hi + lo in _WEAK_TROUBLE:
        return "weak_trouble"

    high, low = RANKS.index(hi), RANKS.index(lo)
    gap = high - low
    if (suited == "s" and gap == 1
            and high <= _J_INDEX and low >= _4_INDEX):
        # 书里的同花连牌：JT-54；43s/32s 归投机牌（书只用「更小的同花连牌」带过）。
        return "suited_connector"
    if high <= _J_INDEX and gap <= 3:
        # 非同花连张（T9o）、同花隔张（97s/96s）：书里「其他可玩牌型」。
        return "speculative"
    return "other"


def preflop_suited(cards) -> bool | None:
    """两张同花 -> True；两张非同花（含对子）-> False；算不出 -> None。

    书里反复按花色分流（前位只玩 KQs/AJs、KQo 弃），而 `hand_class` 只到
    「麻烦牌型」这一层，所以同花与否必须作为独立维度带出来，不能塞进类名。

    走 `hand_key` 而不是自己读第一张牌：它认识 '10h' 这种记法、也做了排序，
    自己读 text[0] 会把 ATs 判成「花色未知」，让同花卡静默不命中。
    """
    if not cards or len(cards) != 2:
        return None
    suits = []
    for card in cards:
        text = str(card or "").strip().upper()
        if len(text) < 2:
            return None
        suit = text[-1].lower()
        rank = text[:-1]
        if rank == "10":
            rank = "T"
        if rank not in RANKS or suit not in ("s", "h", "d", "c"):
            return None     # 认不出的牌不猜花色
        suits.append(suit)
    return suits[0] == suits[1]


def position_bucket(position) -> str | None:
    """'HJ' -> 'middle'；认不出的位置返回 None（不猜、不默认后位）。"""
    if not position:
        return None
    return _POSITION_BUCKET.get(str(position).strip().upper())


__all__ = [
    "POSITION_BUCKETS",
    "PREFLOP_HAND_CLASSES",
    "position_bucket",
    "preflop_hand_class",
    "preflop_suited",
]
