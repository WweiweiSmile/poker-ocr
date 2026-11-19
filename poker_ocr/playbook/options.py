"""把书里的计划落到当前 `options` 的合法 id。

书和桌面的尺度不是一套：书说 0.6 池，桌面只给 1/3、1/2、2/3、1、1.2。
这里只做「最近合法尺寸」映射，并把偏差记下来；不静默改书的动作。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class OptionMatch:
    option: Any
    target_bb: float | None
    deviation_bb: float | None
    deviation_pct: float | None

    @property
    def exact(self) -> bool:
        return self.deviation_pct is not None and self.deviation_pct <= 1e-9


def _size_target(plan: Any, pot_bb: float, to_call_bb: float) -> float | None:
    size = getattr(plan, "size", None)
    if size is None:
        return None
    if size.kind == "bb":
        return float(size.value)
    if size.kind == "pct_pot":
        # 下注：底池 × 比例；加注：先跟注，再加「跟注后的底池」× 比例。
        return to_call_bb + (pot_bb + to_call_bb) * float(size.value)
    return None


def choose_option(options: Iterable[Any], plan: Any, *,
                  pot_bb: float, to_call_bb: float) -> OptionMatch | None:
    """按动作与尺寸选最近的可执行 option；找不到返回 None。"""
    action = getattr(plan, "action", "")
    opts = list(options)
    if action == "check":
        candidates = [o for o in opts if o.kind == "check"]
    elif action == "call":
        candidates = [o for o in opts if o.kind == "call"]
    elif action == "fold":
        candidates = [o for o in opts if o.kind == "fold"]
    elif action == "all_in":
        candidates = [o for o in opts if o.kind == "allin"]
    elif action == "bet":
        candidates = [o for o in opts if o.kind in ("bet", "raise", "allin")]
    elif action == "raise":
        candidates = [o for o in opts if o.kind in ("raise", "allin")]
    else:
        return None
    if not candidates:
        return None

    target = _size_target(plan, pot_bb, to_call_bb)
    sized = [o for o in candidates if getattr(o, "amount", None) is not None]
    if target is None or not sized:
        return OptionMatch(candidates[0], target, None, None)

    best = min(sized, key=lambda o: abs(float(o.amount) - target))
    deviation = abs(float(best.amount) - target)
    pct = deviation / target if target else None
    return OptionMatch(best, target, deviation, pct)
