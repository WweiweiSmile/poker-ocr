"""翻前范围：169 键 → 1,326 个 combo 的加权范围。

这是「实时牌面范围 + 手牌强度」的数据层。设计见
`docs/实时牌面范围与手牌强度-设计文档.md`：

  · `Range` 统一存 **combo 级权重**（不是 169 键频率），因为牌面范围必须
    按花色区分：`AsKs4d` 上 `7s8s` 是花听，`7h8h` 只是后门顺；
  · 权重归一化后表示「范围内抽到该 combo 的概率」；
  · 翻前先验分三档，按可靠性降级：具名表（RFI / BB 防守）→ 规则先验
    （冷跟 / 溜入 / 3-bet）→ 均匀。均匀范围必须标注 board-only。

为什么不直接在 `preflop_ranges.py` 上改：那个模块回答的是「这手牌该不该
开池」，是规范性的；这里回答「对手可能有什么」，是描述性的。两者共用同一
张 RFI 表，但不要把「应该」和「实际」混成一个 API。
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .preflop_ranges import RFI, expand

RANK_CHARS = "23456789TJQKA"
SUITS = "cdhs"
DECK: tuple[tuple[int, str], ...] = tuple(
    (r, s) for r in range(len(RANK_CHARS)) for s in SUITS
)

Card = tuple[int, str]
Combo = tuple[Card, Card]

# 全部 1,326 个 combo，顺序固定（点数升序、花色 c/d/h/s）。确定性对缓存和
# 采样种子都很重要，不能依赖 set 的迭代顺序。
ALL_COMBOS: tuple[Combo, ...] = tuple(itertools.combinations(DECK, 2))
TOTAL_COMBOS = len(ALL_COMBOS)
RANGE_VERSION = 1


def canon_combo(a: Card, b: Card) -> Combo:
    """把两张牌排成固定顺序，避免 (As,Kh) 与 (Kh,As) 被当成两个 combo。"""
    return (a, b) if (a[0], a[1]) <= (b[0], b[1]) else (b, a)


def combo_text(combo: Combo) -> str:
    """显示用：高牌在前。'AsKh' / 'TdTc'。"""
    a, b = sorted(combo, key=lambda c: (c[0], c[1]), reverse=True)
    return f"{RANK_CHARS[a[0]]}{a[1]}{RANK_CHARS[b[0]]}{b[1]}"


def key_of_combo(combo: Combo) -> str:
    """combo → 169 键：对子 'AA'、同花 'AKs'、非同花 'AKo'。"""
    a, b = combo
    hi, lo = (a, b) if a[0] >= b[0] else (b, a)
    if hi[0] == lo[0]:
        return f"{RANK_CHARS[hi[0]]}{RANK_CHARS[lo[0]]}"
    kind = "s" if hi[1] == lo[1] else "o"
    return f"{RANK_CHARS[hi[0]]}{RANK_CHARS[lo[0]]}{kind}"


@lru_cache(maxsize=256)
def combos_of_key(key: str) -> tuple[Combo, ...]:
    """169 键 → 具体 combo。非法键返回空元组（宁可漏，不误判）。"""
    if not key:
        return ()
    if len(key) == 2 and key[0] == key[1]:
        if key[0] not in RANK_CHARS:
            return ()
        r = RANK_CHARS.index(key[0])
        return tuple(canon_combo((r, s1), (r, s2))
                     for s1, s2 in itertools.combinations(SUITS, 2))
    if len(key) == 3 and key[2] in "so":
        hi, lo = key[0], key[1]
        if hi not in RANK_CHARS or lo not in RANK_CHARS or hi == lo:
            return ()
        rh, rl = RANK_CHARS.index(hi), RANK_CHARS.index(lo)
        if key[2] == "s":
            return tuple(canon_combo((rh, s), (rl, s)) for s in SUITS)
        return tuple(canon_combo((rh, s1), (rl, s2))
                     for s1 in SUITS for s2 in SUITS if s1 != s2)
    return ()


def expand_tokens(tokens: Iterable[str]) -> set[str]:
    """把 '22+' / 'A2s+' / 'KQs' 这类写法展成 169 键集合。"""
    keys: set[str] = set()
    for token in tokens:
        keys |= expand(token)
    return keys


@dataclass(frozen=True)
class Range:
    """一个已归一化的 combo 级范围。

    `combo_weights` 的和为 1；`width_pct` 是**非零 combo 数 / 1326**（视觉口径），
    `weighted_width` 是原始权重和 / 1326（混合频率口径）。两者不同时以
    `width_pct` 为主，渲染时写清来源。
    """

    id: str
    version: int
    combo_weights: Mapping[Combo, float]
    width_pct: float = 0.0
    source: str = ""
    note: str = ""
    weighted_width: float = 0.0
    combo_count: int = 0

    def __post_init__(self) -> None:
        positive = {c: w for c, w in self.combo_weights.items() if w > 0}
        if self.combo_count <= 0:
            object.__setattr__(self, "combo_count", len(positive))
        if self.width_pct <= 0 and positive:
            object.__setattr__(self, "width_pct",
                               len(positive) / TOTAL_COMBOS * 100)
        if self.weighted_width <= 0 and positive:
            object.__setattr__(self, "weighted_width",
                               sum(positive.values()) / TOTAL_COMBOS * 100)

    def combos(self) -> tuple[Combo, ...]:
        """权重 > 0 的 combo，顺序固定。"""
        return tuple(c for c, w in self.combo_weights.items() if w > 0)

    def normalized(self) -> Mapping[Combo, float]:
        total = sum(w for w in self.combo_weights.values() if w > 0)
        if total <= 0:
            return MappingProxyType({})
        return MappingProxyType({c: w / total for c, w in self.combo_weights.items()
                                 if w > 0})

    def render_label(self) -> str:
        return f"{self.id} {self.width_pct:.1f}%"


def make_range(id: str, combo_weights: Mapping[Combo, float], *,
               source: str = "", note: str = "",
               version: int = RANGE_VERSION) -> Range | None:
    """过滤负数/零权重并归一化。空范围返回 None（不要返回空范围让权益变 0）。"""
    raw = {canon_combo(*c): float(w) for c, w in combo_weights.items()
           if w is not None and float(w) > 0}
    if not raw:
        return None
    total = sum(raw.values())
    if total <= 0:
        return None
    weights = {c: w / total for c, w in raw.items()}
    return Range(
        id=id,
        version=version,
        combo_weights=MappingProxyType(weights),
        width_pct=len(weights) / TOTAL_COMBOS * 100,
        source=source,
        note=note,
        weighted_width=total / TOTAL_COMBOS * 100,
        combo_count=len(weights),
    )


def _from_keys(id: str, keys: Iterable[str], *, source: str,
               note: str = "") -> Range | None:
    weights: dict[Combo, float] = {}
    # 169 键来自 set，迭代顺序随进程 hash 变化；排序后范围/采样才确定。
    for key in sorted(keys):
        for combo in combos_of_key(key):
            weights[combo] = 1.0
    return make_range(id, weights, source=source, note=note)


def _from_tokens(id: str, tokens: Iterable[str], *, source: str,
                 note: str = "") -> Range | None:
    return _from_keys(id, expand_tokens(tokens), source=source, note=note)


def uniform_range(id: str = "UNIFORM", *, note: str = "") -> Range:
    """全部 1,326 个 combo。只在没有翻前先验时用，且必须标注 board-only。"""
    built = make_range(id, {c: 1.0 for c in ALL_COMBOS},
                       source="uniform", note=note or "board-only (any two cards)")
    assert built is not None
    return built


# ─────────────────────────── 规则先验 ───────────────────────────
#
# 没有具名表时用的「描述性池子先验」。宽度按设计文档的目标区间校准：
#   · 冷跟 IP ~18–19%，OOP ~16%；
#   · 溜入 EP ~28%、MP ~31%、LP ~41%（现场松弱池）；
#   · 3-bet ~5.6%，面对 3-bet 继续 ~3.8%。
# 这些不是 GTO 频率，只是「他实际会拿什么」的粗模型。所有 token 都用
# preflop_ranges.expand 展开；测试会检查没有写错的空 token。

_RULE_TOKENS: dict[str, tuple[str, ...]] = {
    "COLD_CALL_IP": (
        "22", "33", "44", "55", "66", "77", "88", "99",
        "A2s-AJs", "KTs+", "QTs+", "JTs", "T9s", "98s", "87s", "76s",
        "65s", "54s", "KQo",
    ),
    "COLD_CALL_OOP": (
        "22", "33", "44", "55", "66", "77", "88", "99",
        "A2s-ATs", "KTs+", "QTs+", "JTs", "T9s", "98s", "87s", "76s",
        "KQo",
    ),
    "LIMP_EP": (
        "22+", "A2s+", "K9s+", "Q9s+", "J9s+", "T8s+", "97s+", "86s+",
        "75s+", "65s", "54s", "A2o+", "KTo+", "QJo", "JTo", "A9o+",
    ),
    "LIMP_MP": (
        "22+", "A2s+", "K6s+", "Q6s+", "J7s+", "T7s+", "96s+", "86s+",
        "75s+", "65s", "54s", "A2o+", "K9o+", "Q9o+", "J9o+", "T9o",
    ),
    "LIMP_LP": (
        "22+", "A2s+", "K2s+", "Q6s+", "J7s+", "T7s+", "96s+", "86s+",
        "75s+", "65s", "54s", "A2o+", "K8o+", "Q9o+", "J9o+", "T9o", "98o",
    ),
    "THREE_BET": ("QQ+", "AKs", "AKo", "A5s+", "KQs", "KJs"),
    "THREE_BET_CALL": ("TT+", "AQs+", "AKs", "AKo"),
    # SB 开池在 RFI 表里没有单独一行；按提示词 ~30% 的粗范围兜底。
    "OPEN_SB": (
        "22+", "A2s+", "KTs+", "QTs+", "JTs", "T9s", "98s", "87s", "76s",
        "AJo+", "KQo",
    ),
}

# 具名 BB 防守表的兜底：和 gto/specs/spots.json 的 oop_range 同一张静态表。
_BB_DEF_FALLBACK = (
    "22+", "A2s+", "K2s+", "Q4s+", "J6s+", "T6s+", "96s+", "85s+", "75s+",
    "64s+", "54s", "A2o+", "K8o+", "Q9o+", "J9o+", "T9o", "98o", "87o",
)

_BB_DEF_CACHE: tuple[str, ...] | None = None


def _bb_defense_tokens() -> tuple[str, ...]:
    """BB 防守 token。优先读现有 GTO spot 数据，缺失时用同一张静态兜底。"""
    global _BB_DEF_CACHE
    if _BB_DEF_CACHE is not None:
        return _BB_DEF_CACHE
    path = Path(__file__).resolve().parents[1] / "gto" / "specs" / "spots.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        spots = data.get("spots") or []
        for spot in spots:
            text = str(spot.get("oop_range") or "").strip()
            if text:
                tokens = tuple(t.strip() for t in text.split(",") if t.strip())
                if tokens and all(expand(t) for t in tokens):
                    _BB_DEF_CACHE = tokens
                    return tokens
    except (OSError, ValueError, TypeError):
        pass
    _BB_DEF_CACHE = _BB_DEF_FALLBACK
    return _BB_DEF_CACHE


def _bucket(text: str | None) -> str:
    raw = (text or "").strip().upper()
    if raw in ("UTG", "UTG+1", "UTG+2", "EP"):
        return "EP"
    if raw in ("LJ", "HJ", "MP"):
        return "MP"
    if raw in ("CO", "BTN", "LP"):
        return "LP"
    if raw in ("SB", "BB"):
        return raw
    return "LP"


def _range_from_rule(id: str, base: str) -> Range | None:
    tokens = _RULE_TOKENS.get(base)
    if not tokens:
        return None
    return _from_tokens(id, tokens, source=f"rules:{base.lower()}",
                        note="rule prior (descriptive pool baseline)")


@lru_cache(maxsize=256)
def get(range_id: str | None) -> Range | None:
    """按 ID 取范围。未知/空 ID 返回 None（fail open，不拿别的范围冒充）。"""
    if not range_id:
        return None
    rid = str(range_id).strip()
    upper = rid.upper()

    if upper in ("UNIFORM", "BOARD_ONLY", "BOMB", "BB_CHECK"):
        # 炸弹池/BB 过牌没有可用的翻前收紧：任意两张，必须标注 board-only。
        return uniform_range(rid, note="board-only (any two cards)")

    if upper.startswith("RFI_"):
        pos = rid[4:].upper()
        keys = RFI.get(pos)
        if keys:
            return _from_keys(rid, keys, source=f"named:RFI_{pos}",
                              note="preflop RFI chart")
        if pos == "SB":
            return _range_from_rule(rid, "OPEN_SB")
        return None

    if upper.startswith("BB_DEF_VS_"):
        bucket = _bucket(upper.split("_VS_", 1)[1])
        return _from_tokens(rid, _bb_defense_tokens(),
                            source=f"named:BB_DEF_vs_{bucket}",
                            note="static BB defense (same table for all openers)")

    if upper.startswith("COLD_CALL_"):
        # COLD_CALL_<IP|OOP>_vs_<bucket>
        head = upper.split("_VS_", 1)[0]
        ip_oop = head.replace("COLD_CALL_", "", 1).strip("_")
        base = "COLD_CALL_IP" if ip_oop == "IP" else "COLD_CALL_OOP"
        return _range_from_rule(rid, base)

    if upper.startswith("LIMP_"):
        bucket = _bucket(rid.split("_", 1)[1])
        base = "LIMP_EP" if bucket == "EP" else (
            "LIMP_MP" if bucket == "MP" else "LIMP_LP")
        return _range_from_rule(rid, base)

    if upper.startswith("3BET_CALL_"):
        return _range_from_rule(rid, "THREE_BET_CALL")

    if upper.startswith("3BET_"):
        return _range_from_rule(rid, "THREE_BET")

    return None


def reset_cache() -> None:
    """测试/重放用：清掉 token 展开和范围缓存。"""
    global _BB_DEF_CACHE
    get.cache_clear()
    combos_of_key.cache_clear()
    _BB_DEF_CACHE = None


__all__ = [
    "ALL_COMBOS", "Card", "Combo", "DECK", "RANGE_VERSION", "Range",
    "TOTAL_COMBOS", "canon_combo", "combo_text", "combos_of_key",
    "expand_tokens", "get", "key_of_combo", "make_range", "reset_cache",
    "uniform_range",
]
