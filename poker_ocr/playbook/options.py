"""把书里的计划落到当前 `options` 的合法 id。

书和桌面的尺度不是一套：书说 0.6 池，桌面只给 1/3、1/2、2/3、1、1.2。
这里只做「最近合法尺寸」映射，并把偏差记下来；不静默改书的动作。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


# 动作 -> 能承载它的 option kind。raise 可以落在 raise 或全下档；bet 在
# 「面对下注/加注」的选项里同样以 raise 档出现。只有这一份映射：选档、
# 覆盖统计、「options 缺动作档」的判定都查它。
KINDS_FOR_ACTION: dict[str, tuple[str, ...]] = {
    "check": ("check",),
    "call": ("call",),
    "fold": ("fold",),
    "all_in": ("allin",),
    "bet": ("bet", "raise", "allin"),
    "raise": ("raise", "allin"),
}


def kinds_for_action(action: str) -> tuple[str, ...]:
    """动作能落到哪些 option kind；认不出的动作返回空。"""
    return KINDS_FOR_ACTION.get(str(action), ())


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
    kinds = kinds_for_action(getattr(plan, "action", ""))
    if not kinds:
        return None
    candidates = [o for o in options if o.kind in kinds]
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
