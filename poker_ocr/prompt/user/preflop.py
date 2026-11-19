"""翻前事实：形态、最后加注者、溜入人数、本桌最低加注。

翻前的「该用什么范围开池/继续」属于书页（#87 补翻前书卡）；#81 之后提示词
不再给范围结论、limp 例外或隔离尺寸建议——这些模型都能从「可选动作」和
书页里读到，重复一遍就是第二个策略来源。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...hand_track import PREFLOP
from ...pot_math import PREFLOP_MIN_RAISE_BB, _preflop_raise_count, count_limpers
from ..shared.labels import _PREFLOP_SHAPE_ZH

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...hand_track import HandSnapshot
from ..refs import fact


def _pfr_label_zh(snap: HandSnapshot) -> str:
    """最后翻前加注者的三态；`hero_is_pfr` 本身只是 bool，读不出「未知」。"""
    if snap.hero_is_pfr:
        return "是（Hero 是最后一个翻前加注者）"
    pre = snap.actions_of(PREFLOP)
    if not pre:
        return "未知（翻前动作线缺失）"
    if any(a.is_hero and a.action in ("raise", "bet") for a in pre):
        return "否（Hero 加注后被再加注）"
    return "否"


def _preflop_shape_labels(snap: HandSnapshot) -> list[str]:
    """把 ``three-bet`` 这个内部上限展开成实际级别（4-bet/5-bet）。"""
    out: list[str] = []
    for shape in (snap.preflop_shapes or ()):
        if shape != "three-bet":
            out.append(_PREFLOP_SHAPE_ZH.get(shape, shape))
            continue
        raises = _preflop_raise_count(snap)
        if raises >= 4:
            out.append(f"{raises + 1}-bet")
        elif raises == 3:
            out.append("4-bet")
        elif raises == 2:
            out.append("3-bet")
        else:
            out.append("3-bet+")
    return out


def render_preflop_zh(sit: Situation) -> list[str]:
    """「翻前信息」；只在翻前渲染，只写读得到的事实。"""
    if sit.street != PREFLOP:
        return []
    snap = sit.snap
    lines = ["## 翻前信息"]
    shapes = _preflop_shape_labels(snap)
    lines.append("翻前形态：" + ("、".join(shapes) if shapes else "未知"))
    lines.append("最后翻前加注者（PFR）：" + _pfr_label_zh(snap))
    limpers = count_limpers(snap)
    if limpers:
        lines.append(f"前面溜入人数：{limpers}")
    raises = sorted({o.amount for o in sit.options
                     if o.kind in ("raise", "bet") and o.amount is not None})
    if raises:
        lines.append(fact(
            f"本桌翻前最低加注：{PREFLOP_MIN_RAISE_BB:g}BB（更小的会被客户端"
            "拒绝；实际金额以「可选动作」为准）", kind="derived"))
    return lines
