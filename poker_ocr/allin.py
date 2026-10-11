"""翻前「全下或弃牌」的均衡范围表：按位置 + 已在场全下的人，查该不该推。

**这张表的适用面很窄，用错比不用更糟**，先把边界写死：

- **只对 4 人桌、每人 8BB、盲注 0.5/1、无前注的「全下或弃牌」局有效。**
  翻前只有全下 8BB 或弃牌两个选择、没有翻后。换人数、换筹码深度、
  允许小额加注的局，表里的数都不适用 —— 所以查不到节点时**返回 None，
  绝不猜**（宁可不给建议，也不给一个看着像样却是错的建议）。
- **数值是纯 chip-EV 的纳什均衡，不含 ICM。** 打有奖金的锦标赛要明显收紧。

表本身在 `config/allin_ranges_4max_8bb.json`，由
`call-front/tools/open-size-ev/allin-nash.ts`（虚拟对局求解器）产出，不是手抄的。

## 两个容易踩的地方

1. **p 是均衡概率，不是二值范围。** 表里大量手牌是混合的（如 0.47），
   意思是均衡里这手牌按这个频率推。judge 用 0.5 当阈值取「更常做的那一边」是
   合理默认，但要清楚**恰好卡在 0.5 附近的手牌本来就是不确定的**——
   这类手牌推/弃的 EV 差极小，别把它当成一个明确结论去教人。

2. **`reliability == "low"` 的节点数值偏宽。** 那是「已经有 ≥2 家全下之后再跟」
   的节点，稀有、采样噪声大，整体偏宽约 1-3%（BB 对三家更甚）。
   默认允许使用，但调用方可以在意精度时把 `allow_low_reliability=False`
   传进来，让这些节点拒绝作答。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_RANGES = PROJECT_ROOT / "config" / "allin_ranges_4max_8bb.json"

# 均衡表里的位置名与行动顺序。必须与 positions.POSITIONS_BY_TABLE_SIZE[4] 一致。
POSITIONS_4 = ("SB", "BB", "UTG", "BTN")
# 翻前行动顺序（用于把 already_allin 规范化成稳定的键序）
ACTION_ORDER_4 = ("UTG", "BTN", "SB", "BB")

RANK_ORDER = "AKQJT98765432"            # A 在前：用来定「较大牌」
VALID_SUITS = "shdc"

ALLIN = "allin"
FOLD = "fold"


# ─────────────────────────── 手牌类 ───────────────────────────

def _two_cards(cards) -> tuple[str, str]:
    """把 'AsKh' / '10s10h' / ['As','Kh'] 归一成两张牌的字符串。

    字符串形式按「点数+花色」逐张扫描而不是切两半：识别器可能会给出
    `10s10h` 这种 6 字符写法（T 被写成 10），按固定长度切会切错。
    """
    if isinstance(cards, str):
        s = cards.strip().replace(" ", "")
        out: list[str] = []
        i = 0
        while i < len(s):
            if s[i:i + 2] == "10":            # '10s' -> 点数 10、花色 s
                out.append(s[i:i + 3])
                i += 3
            else:
                out.append(s[i:i + 2])
                i += 2
        if len(out) != 2:
            raise ValueError(f"底牌字符串必须正好两张牌（如 'AsKh'），实际 {cards!r}")
        return out[0], out[1]
    seq = list(cards)
    if len(seq) != 2:
        raise ValueError(f"底牌必须是两张，实际 {cards!r}")
    return str(seq[0]).strip(), str(seq[1]).strip()


def _rank_suit(card: str) -> tuple[str, str]:
    c = card.strip().upper()
    if c.startswith("10"):                       # '10s' / '10H'
        rank, suit = "T", c[2:]
    else:
        rank, suit = c[:1], c[1:]
    if rank not in RANK_ORDER:
        raise ValueError(f"认不出点数：{card!r}")
    if suit.lower() not in VALID_SUITS or len(suit) != 1:
        raise ValueError(f"认不出花色：{card!r}")
    return rank, suit.lower()


def hand_class(cards) -> str:
    """把具体底牌归约成 169 个手牌类之一：'AA' / 'AKs' / 'AKo'。

    同花与否只看花色是否相同；对子不带后缀。'AsKh' → 'AKo'，
    'KdAd' → 'AKs'（顺序无关），'10s10h' → 'TT'。
    """
    a, b = _two_cards(cards)
    ra, sa = _rank_suit(a)
    rb, sb = _rank_suit(b)
    if ra == rb:
        return ra + rb
    hi, lo = (ra, rb) if RANK_ORDER.index(ra) < RANK_ORDER.index(rb) else (rb, ra)
    return f"{hi}{lo}{'s' if sa == sb else 'o'}"


# ─────────────────────────── 表 ───────────────────────────

@dataclass(frozen=True)
class Node:
    """一个决策点：某个位置，在「已在场全下的人」给定时的均衡策略。"""

    key: str
    hero: str
    already_allin: tuple[str, ...]
    kind: str                    # 'open'（没人全下，轮到我开池） | 'call'（有人全下了）
    label: str
    allin_pct: float             # 该节点全下/跟注的组合加权占比
    reliability: str             # 'high' | 'low'
    p: dict[str, float]

    def prob(self, hand: str) -> float:
        """某手牌类在该节点全下/跟注的概率（0-1）。手牌类不存在时抛错。"""
        try:
            return self.p[hand]
        except KeyError:
            raise KeyError(f"{self.key} 没有手牌类 {hand!r}") from None


@dataclass(frozen=True)
class RangeTable:
    meta: dict
    nodes: dict[str, Node]

    @property
    def table_size(self) -> int:
        return self.meta["table_size"]

    @property
    def stack_bb(self) -> float:
        return self.meta["stack_bb"]

    def node(self, position: str, already_allin=()) -> Node | None:
        """查节点。位置组合非法、或不是本表覆盖的局，返回 None（不猜）。"""
        try:
            allin = _canonical_allin(already_allin)
        except ValueError:
            return None
        if position not in POSITIONS_4:
            return None
        if position in allin:
            return None                       # 自己不可能已经在场全下
        key = f"{position}:open" if not allin else f"{position}:call[{','.join(allin)}]"
        return self.nodes.get(key)

    def prob(self, position: str, cards, already_allin=()) -> float | None:
        node = self.node(position, already_allin)
        return None if node is None else node.prob(hand_class(cards))


def _canonical_allin(already_allin) -> tuple[str, ...]:
    """把「已在场全下的人」规范化成按行动顺序排的元组（键的稳定形式）。"""
    if already_allin is None:
        return ()
    seq = [already_allin] if isinstance(already_allin, str) else list(already_allin)
    for pos in seq:
        if pos not in ACTION_ORDER_4:
            raise ValueError(f"非法位置：{pos!r}")
    return tuple(sorted(set(seq), key=ACTION_ORDER_4.index))


def load(path: Path | None = None) -> RangeTable:
    """读范围表。默认读 config/allin_ranges_4max_8bb.json。"""
    raw = json.loads(Path(path or DEFAULT_RANGES).read_text(encoding="utf-8"))
    nodes = {k: Node(key=k, p=v["p"], already_allin=tuple(v["already_allin"]),
                     hero=v["hero"], kind=v["kind"], label=v["label"],
                     allin_pct=v["allin_pct"], reliability=v["reliability"])
             for k, v in raw["nodes"].items()}
    return RangeTable(meta=raw["meta"], nodes=nodes)


_TABLE: RangeTable | None = None


def table() -> RangeTable:
    """进程内缓存的默认表（每次请求都读盘没必要）。"""
    global _TABLE
    if _TABLE is None:
        _TABLE = load()
    return _TABLE


# ─────────────────────────── 判定 ───────────────────────────

@dataclass(frozen=True)
class Decision:
    """给一手具体牌的建议。"""

    ok: bool
    action: str | None                   # 'allin' | 'fold'；ok=False 时为 None
    prob: float | None                   # 均衡里全下/跟注的概率
    hand: str | None                     # 手牌类
    node: Node | None
    reason: str | None = None            # 给不出建议的原因

    @property
    def mixed(self) -> bool:
        """是否落在混合区（推/弃 EV 差极小，结论不该被当成硬规则）。"""
        return self.prob is not None and 0.35 < self.prob < 0.65


def decide(position: str, cards, already_allin=(),
           threshold: float = 0.5,
           allow_low_reliability: bool = True) -> Decision:
    """判断该不该全下。

    position      我的位置（UTG/BTN/SB/BB）
    cards         我的底牌，'AsKh' / ['As','Kh'] 均可
    already_allin 已经全下的人的位置列表（不含我）
    threshold     概率达到多少算「推」；默认 0.5 = 取均衡里更常做的那一边
    allow_low_reliability  False 时，reliability=low 的多路节点拒绝作答

    查不到节点（非 4 人桌 8BB 全下局、或位置组合非法）时 ok=False，
    绝不退化成「随便给个建议」。
    """
    tbl = table()
    node = tbl.node(position, already_allin)
    if node is None:
        return Decision(False, None, None, None, None,
                        reason=f"本表只覆盖 4 人桌 8BB 全下局，"
                               f"查不到 {position} / 已全下={list(already_allin)} 的节点")
    if node.reliability == "low" and not allow_low_reliability:
        return Decision(False, None, None, None, node,
                        reason=f"{node.key} 是多路跟注节点，数值偏宽，已拒绝作答")

    hand = hand_class(cards)
    p = node.prob(hand)
    return Decision(True, ALLIN if p >= threshold else FOLD, p, hand, node)
