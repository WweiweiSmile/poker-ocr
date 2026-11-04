"""用户事实块的组装入口。

这个文件只负责**顺序与空行**，不写事实本身：每一段是 `user/` 下的一个模块，
返回 `list[str]`，空列表表示该场景不出现。事实块的分段顺序就是模型读到的
顺序——先硬事实（局面/几何/位置/价格），再行动线与牌力，然后画像/现场观察，最后是
建议块、数据质量与可选动作。

加新段：在 `user/` 新建模块 -> 在 `render_facts_zh()` 里按顺序 append。
不要在组装函数里写具体文案。
"""

from __future__ import annotations

from ..system import build_system
from .action_line import render_action_line_zh
from .attack import attack_signals, render_attack_zh
from .explosive import render_explosive_pot_zh
from .geometry import render_geometry_zh, render_side_pot_zh
from .notes import render_notes_zh
from .options import render_options_zh
from .position import render_position_zh
from .preflop import render_preflop_zh
from .price import render_price_zh
from .profiles import render_profiles_zh
from .quality import render_data_quality_zh
from .situation import render_current_state_zh
from .strength import render_hand_strength_zh


def render_facts_zh(sit) -> str:
    """中文 lean 的 user 事实块：所有金额/概率来自程序计算。"""
    # ── 当前局面（硬事实）：局面/几何/位置/价格/短码/爆炸底池 ──
    lines: list[str] = []
    lines.extend(render_current_state_zh(sit))
    lines.extend(render_geometry_zh(sit))
    lines.extend(render_position_zh(sit))
    lines.extend(render_price_zh(sit))
    lines.extend(render_side_pot_zh(sit))
    lines.extend(render_explosive_pot_zh(sit))

    # ── 行动线提前：模型判断范围最需要的是「谁做了什么」 ──
    lines.append("")
    lines.extend(render_action_line_zh(sit))

    # ── 翻前信息 / 实时牌力 ──
    preflop = render_preflop_zh(sit)
    if preflop:
        lines.append("")
        lines.extend(preflop)

    strength = render_hand_strength_zh(sit)
    if strength:
        lines.append("")
        lines.extend(strength)

    # 攻击块会给出本街弃牌率的单一裁决；有攻击块、或某个座位已经有
    # 「别开火」的反向证据时，逐座位的「可剥削」不再给第二个动作方向，
    # 避免同一份 prompt 里两个「能不能开火」的来源。
    attack = render_attack_zh(sit)
    trap_callers, trap_raisers = attack_signals(sit)
    suppress_action_hints = bool(attack) or bool(trap_callers or trap_raisers)

    # ── 逐座位画像：优先于全场统计；攻击块在场时只留描述性数据 ──
    profiles = render_profiles_zh(
        sit, suppress_action_hints=suppress_action_hints)
    if profiles:
        lines.append("")
        lines.extend(profiles)

    # ── 人工观察（.table_dy）：只覆盖它明确描述的范围 ──
    notes = render_notes_zh(sit)
    if notes:
        lines.append("")
        lines.extend(notes)

    # ── 攻击/剥削结论：带「程序建议」标记，不是事实；与画像冲突时按画像 ──
    if attack:
        lines.append("")
        lines.extend(attack)

    # ── 数据质量 + 可选动作 ──
    lines.append("")
    lines.extend(render_data_quality_zh(sit))
    lines.append("")
    lines.extend(render_options_zh(sit))
    return "\n".join(lines)


def build_messages(sit) -> tuple[str, str]:
    """返回中文 lean `(system, user facts)`；system 按局面场景拼装。"""
    return build_system(sit), render_facts_zh(sit)


__all__ = ["build_messages", "render_facts_zh"]
