"""用户事实块的组装入口。

这个文件只负责**顺序与空行**，不写事实本身：每一段是 `user/` 下的一个模块，
返回 `list[str]`，空列表表示该场景不出现。事实块的分段顺序就是模型读到的
顺序——先硬事实（局面/几何/位置/价格），再行动线与牌力，然后是书计划与偏移，
最后是逐座位画像、现场观察、数据质量与可选动作。

#81 起这里不再组装旧策略段：攻击/剥削/池子先验/规则台账都在同一次原子切换里
移除（`poker_ocr/rules/ledger/*.json` 仍留在仓库，但运行时不读、不渲染）。

加新段：在 `user/` 新建模块 -> 在 `render_facts_zh()` 里按顺序 append。
不要在组装函数里写具体文案。
"""

from __future__ import annotations

from ..refs import collecting, section
from ..system import build_system
from .action_line import render_action_line_zh
from .bomb import render_bomb_guard_zh
from .book import render_book_plan_zh
from .explosive import render_explosive_pot_zh
from .geometry import render_geometry_zh, render_side_pot_zh
from .history import render_previous_advice_zh
from .notes import render_notes_zh
from .offsets import plan_offsets, render_offsets_zh
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

    # ── 上一街自己的建议：同一手内保持一致（不是新事实） ──
    history = render_previous_advice_zh(sit)
    if history:
        lines.append("")
        lines.extend(history)

    # ── 翻前事实 / 实时牌力 ──
    # 段包在 section() 里：收集器激活时行号才与段文本对得上（未激活是空操作）。
    with section("preflop") as sec:
        sec.extend(render_preflop_zh(sit))
    if sec.lines:
        lines.append("")
        lines.extend(sec.lines)

    with section("strength") as sec:
        sec.extend(render_hand_strength_zh(sit))
    if sec.lines:
        lines.append("")
        lines.extend(sec.lines)

    # ── 书中计划（Harrington 场景卡）：基线 ──
    # 解析失败时整段不出现（fail open），零匹配仍由渲染器写「书中无此局面」。
    # 炸弹池守则生效时书计划整段让位（返回 []），由下方守则段占这个位置。
    with section("book_plan") as sec:
        sec.extend(render_book_plan_zh(sit))
    if sec.lines:
        lines.append("")
        lines.extend(sec.lines)

    # ── 炸弹池守则（#114）：没有翻前下注轮的固定打法 ──
    # 只在横幅确认且翻前无动作时出现；普通局面为 []，不多出空行。
    with section("bomb_guard") as sec:
        sec.extend(render_bomb_guard_zh(sit))
    if sec.lines:
        lines.append("")
        lines.extend(sec.lines)

    # ── 偏移占位：书 -> 最终（#82 之前恒为空，整段不出现） ──
    with section("offsets") as sec:
        sec.extend(render_offsets_zh(plan_offsets(sit)))
    if sec.lines:
        lines.append("")
        lines.extend(sec.lines)

    # ── 逐座位画像：只留描述性数据，不给动作提示 ──
    profiles = render_profiles_zh(sit)
    if profiles:
        lines.append("")
        lines.extend(profiles)

    # ── 人工观察（.table_dy）：只覆盖它明确描述的范围 ──
    notes = render_notes_zh(sit)
    if notes:
        lines.append("")
        lines.extend(notes)

    # ── 数据质量 + 可选动作 ──
    with section("quality") as sec:
        sec.extend(render_data_quality_zh(sit))
    lines.append("")
    lines.extend(sec.lines)
    with section("options") as sec:
        sec.extend(render_options_zh(sit))
    lines.append("")
    lines.extend(sec.lines)
    return "\n".join(lines)


def build_messages(sit) -> tuple[str, str]:
    """返回中文 lean `(system, user facts)`。"""
    return build_system(sit), render_facts_zh(sit)


def build_messages_refs(sit) -> tuple[str, str, list[dict]]:
    """与 `build_messages` 同文本，另外返回行级 refs（spec #33 A 部分）。

    唯一测试缝：`(system, user)` 必须与 `build_messages(sit)` 逐字节相等；
    收集器是旁路（contextvar），未激活时 `fact()` 恒等返回。
    """
    with collecting() as collector:
        system = build_system(sit)
        user = render_facts_zh(sit)
    return system, user, collector.finish(system, user)


__all__ = ["build_messages", "build_messages_refs", "render_facts_zh"]
