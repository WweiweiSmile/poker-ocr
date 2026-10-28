"""实时牌力解析：把 `hand_vs_ranges` 的数字翻译成一行「大 / 中 / 小 + 程度 + 价值 / 听牌」。

它是**纯判定层**，不重算任何数字。`hand_vs_ranges` 已经给出 `behind_any`（现在
成手牌至少一家领先的比例）和 `equity`（打到河牌的权益），这里只回答：

  · `size`：当前成手牌大小（very_big / big / medium / small / very_small）；
  · `equity_tier`：权益相对平均分池线（1/N）的档次（strong / above_average /
    neutral / weak）；
  · `kind`：价值牌 / 听牌 / 弱牌（空气）/ 打牌面；
  · `summary_en`：一行英文结论，直接注入提示词。

设计文档：docs/实时牌力解析-接入AI建议-设计.md
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .coach import (
    AIR, BOARD_PLAY, OVERPAIR, STRONG_DRAW, STRONG_MADE, TPTK, WEAK_DRAW,
)

# ─────────────────────────── 枚举值 ───────────────────────────

SIZE_VERY_BIG = "very_big"
SIZE_BIG = "big"
SIZE_MEDIUM = "medium"
SIZE_SMALL = "small"
SIZE_VERY_SMALL = "very_small"

KIND_VALUE = "value"
KIND_DRAW = "draw"
KIND_WEAK = "weak"
KIND_BOARD_PLAY = "board_play"

TIER_STRONG = "strong"
TIER_ABOVE = "above_average"
TIER_NEUTRAL = "neutral"
TIER_WEAK = "weak"

# 默认阈值（设计文档 §4.1 / §4.2）。可由 HAND_STRENGTH_THRESHOLDS 的 JSON 覆盖，
# 用于回放校准，不碰判定代码。
_BEHIND_DEMOTE = 0.35          # behind_any >= 此值，big/very_big 成牌降一档
_RATIO_STRONG = 2.0            # equity / (1/N) >= 2.0 → strong
_RATIO_ABOVE = 1.3             # >= 1.3 → above_average
_RATIO_NEUTRAL = 0.85          # >= 0.85 → neutral；更低 → weak


@dataclass(frozen=True)
class HandStrengthRead:
    """一手的牌力判定结论。字段语义见设计文档 §5。"""

    size: str                 # very_big | big | medium | small | very_small
    equity_tier: str          # strong | above_average | neutral | weak
    kind: str                 # value | draw | weak | board_play
    made_name: str            # "Two pair" / "Straight" / "Top pair K kicker" ...
    nuts: bool                # 是否坚果
    behind_any: float         # 0..1，多人池「至少一家当前领先」比例
    equity: float             # 0..1
    avg: float                # 1 / (对手数 + 1)
    n: int                    # 对手数

    def summary_en(self) -> str:
        """给模型的单行事实。"""
        head = ("DRAW" if self.kind == KIND_DRAW
                else self.size.upper().replace("_", " "))
        return (f"{head} {self.made_name}; behind now {self.behind_any:.0%}, "
                f"equity {self.equity:.0%} vs {self.n + 1}-way avg "
                f"{self.avg:.0%} ({self.equity_tier}) — {self._tail()}.")

    def _tail(self) -> str:
        if self.kind == KIND_DRAW:
            return "a draw, judge by outs and price, not by behind-now"
        if self.kind == KIND_WEAK:
            return "no made hand, no draw"
        if self.kind == KIND_BOARD_PLAY:
            return "playing the board (everyone shares the best five)"
        if self.nuts:
            return "nuts / near-nuts — value hand"
        if self.size == SIZE_MEDIUM:
            if self.behind_any >= 0.35:
                return "ahead pair, multiway behind; do not overplay"
            return "medium made hand, thin value"
        return "value hand"


def enabled() -> bool:
    """与 HAND_VS_RANGES_ENABLED 解耦的开关。缺数据自动不输出，绝不猜。"""
    return os.environ.get("HAND_STRENGTH_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _thresholds(env=None) -> tuple[float, float, float, float]:
    """返回 (behind_demote, ratio_strong, ratio_above, ratio_neutral)。

    HAND_STRENGTH_THRESHOLDS 是 JSON，覆盖默认阈值；非法配置静默回退默认。
    """
    env = os.environ if env is None else env
    raw = (env.get("HAND_STRENGTH_THRESHOLDS") or "").strip()
    out = [_BEHIND_DEMOTE, _RATIO_STRONG, _RATIO_ABOVE, _RATIO_NEUTRAL]
    if not raw:
        return tuple(out)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return tuple(out)
    if not isinstance(data, dict):
        return tuple(out)
    keys = ("behind_demote", "ratio_strong", "ratio_above", "ratio_neutral")
    for i, key in enumerate(keys):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[i] = float(value)
    return tuple(out)


def compute_hand_strength(sit) -> HandStrengthRead | None:
    """输入 `advice.Situation`（hand_ranges 已算好），输出牌力判定。

    依赖 `hand_ranges`；None（翻前/范围缺失）或任何异常都返回 None —— fail open，
    绝不影响主建议链路。
    """
    hr = getattr(sit, "hand_ranges", None)
    if hr is None or not getattr(hr, "opponents", None):
        return None
    try:
        return _compute(sit, hr)
    except Exception:  # noqa: BLE001
        return None


def _compute(sit, hr) -> HandStrengthRead:
    n = len(hr.opponents)
    avg = 1.0 / (n + 1)
    ratio = hr.equity / avg if avg > 0 else 0.0
    behind_demote, ratio_strong, ratio_above, ratio_neutral = _thresholds()

    tier = getattr(sit, "hand_tier", None)
    made_name = getattr(sit, "hand_label_en", "") or ""
    gt = getattr(sit, "ground_truth", None)
    if not made_name and gt is not None:
        made_name = getattr(getattr(gt, "made", None), "name", "") or ""
    nuts = bool(gt is not None and getattr(gt, "hero_is_nuts", False))

    # 先判 kind（听牌 / 打牌面 / 成牌），再判 size。听牌永远 DRAW，不因 equity 高
    # 而标成 BIG made hand。
    if tier in (STRONG_DRAW, WEAK_DRAW):
        kind, size = KIND_DRAW, SIZE_VERY_SMALL
    elif tier == BOARD_PLAY:
        kind, size = KIND_BOARD_PLAY, SIZE_VERY_SMALL
    elif tier == AIR:
        kind, size = KIND_WEAK, SIZE_VERY_SMALL
    elif tier == STRONG_MADE:
        kind, size = KIND_VALUE, (SIZE_VERY_BIG if nuts else SIZE_BIG)
    elif tier == OVERPAIR:
        kind, size = KIND_VALUE, (SIZE_VERY_BIG if nuts else SIZE_BIG)
    elif tier == TPTK:
        kind, size = KIND_VALUE, SIZE_BIG
    else:                                  # TP_WEAK / MID_BOTTOM / 未知成牌
        kind, size = KIND_VALUE, SIZE_MEDIUM

    # 现在位置用 behind_any 修正：成牌但多路已落后 → 降一档。
    if (kind == KIND_VALUE and hr.behind_any >= behind_demote
            and size in (SIZE_BIG, SIZE_VERY_BIG)):
        size = SIZE_MEDIUM

    if ratio >= ratio_strong:
        eq_tier = TIER_STRONG
    elif ratio >= ratio_above:
        eq_tier = TIER_ABOVE
    elif ratio >= ratio_neutral:
        eq_tier = TIER_NEUTRAL
    else:
        eq_tier = TIER_WEAK

    return HandStrengthRead(
        size=size, equity_tier=eq_tier, kind=kind, made_name=made_name,
        nuts=nuts, behind_any=hr.behind_any, equity=hr.equity, avg=avg, n=n,
    )


__all__ = [
    "HandStrengthRead", "compute_hand_strength", "enabled",
    "KIND_VALUE", "KIND_DRAW", "KIND_WEAK", "KIND_BOARD_PLAY",
    "SIZE_VERY_BIG", "SIZE_BIG", "SIZE_MEDIUM", "SIZE_SMALL", "SIZE_VERY_SMALL",
    "TIER_STRONG", "TIER_ABOVE", "TIER_NEUTRAL", "TIER_WEAK",
]
