"""炸弹池守则的生效判定（#114）。

炸弹池（屏幕中央出现「幸运暴击 / BOMB POT」横幅）翻前没有下注轮：每人先
扔同样金额进池，所以书里的翻前开注与翻后计划都不适用。守则只在「横幅确认
且本手翻前确实没有动作」时生效；横幅偶尔会串到下一手，翻前读到真实动作就
按有行动处理（与 `situation._conflicts` 里那条冲突提示同源）。

书计划、桌况偏移、守则段三处都调这一个谓词，避免各写一遍条件漂移。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # 只为类型标注，运行时不 import
    from ..situation import Situation

# 与 `hand_track.PREFLOP` 同值。这里不 import hand_track：它是手牌跟踪的
# 重模块，playbook 层只需要这一个街道串。
PREFLOP = "preflop"


def bomb_guard_applies(sit: "Situation") -> bool:
    """炸弹池守则是否生效：横幅确认，且翻前没有真实动作。"""
    try:
        snap = sit.snap
        if not snap.bomb_pot:
            return False
        return not snap.actions_of(PREFLOP)
    except Exception:                   # noqa: BLE001 - fail open
        # 判不出来就按普通局面走：宁可多给一张书卡，也不要吞掉事实。
        return False


__all__ = ["PREFLOP", "bomb_guard_applies"]
