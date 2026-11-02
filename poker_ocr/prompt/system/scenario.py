"""条件场景段：只在该场景成立时追加，避免恒定 system 变成长篇通用建议。

设计原则（来自 `docs/提示词审阅-2026-11-02-系统性冲突.md`）：

  · **条件句优先于绝对句**：写「如果…则…；除非…」，不写「只用/绝不」。
  · **不重复事实**：人数、SPR、outs、赔率都在 user 事实块里；这里只给
    「拿到这些数字之后怎么权衡」，并且明确让事实优先。
  · **一段一个场景**：改多人池口径不会碰到河牌，改河牌不会碰到听牌。

新增场景：在本文件加一个 `xxx_section(sit)`，返回 `str | None`，然后
注册到 `SCENARIO_SECTIONS`。不要在 `SYSTEM_SECTIONS`（恒定段）里加场景规则。
"""

from __future__ import annotations

from ...coach import MID_BOTTOM, OVERPAIR, STRONG_DRAW, TPTK, TP_WEAK, WEAK_DRAW
from ...hand_track import PREFLOP, RIVER
from ...pot_math import _can_act, _hero_total_stack
from ..shared.format import _fmt_num

# 短码线：Hero 本手总筹码（后手 + 本街已下注）≤ 这个数时给口径。
SHORT_STACK_BB = 20.0

SHORT_STACK_SECTION = """## 场景：短码（本手总筹码约 {stack}BB）
- 短码的核心是「翻前推/弃、翻后按 SPR 打」：翻前不要用「先开池、被加注再弃」的尺度把自己套进去；本手总筹码 ≤20BB 时，翻前优先把 all-in 或弃牌当作默认分支。翻后不要套用翻前推/弃，按 SPR 和人数决定。
- 翻后 SPR <3 时顶对可以套池，但先看人数：4 人以上仍然降级；SPR 约 3 时按人数/牌力，不自动套池。
- 本手总筹码越少，隐含赔率越差：听牌按即时赔率算，不要假设中牌后还能收钱；若听牌段写隐含赔率，以本段为准。"""

MULTIWAY_SECTION = """## 场景：{title}（本手 {players} 人）
- 人数（含 Hero）：4 人以上顶对/超对降级；3 人池先按人数收紧，SPR<3 时顶对可以套池。
- 人数会放大「至少一家领先」的概率：同样的牌在 3 人池能收价值，4 人以上要先看公对面和更大的两对，再决定两对要不要打光。
- 下注先问「更差的牌会不会跟」：人多时薄价值更难被跟，偏向直接拿价值；诈唬需要更少的对手或更强的阻断。
- 跟注时赔率变好，但身后还有人能加注：跟注范围要比单挑更紧，别把「赔率够」当成「可以跟」。
- 位置权重更高：无位置默认过牌控池；但两对+的强价值牌仍可按「更差的牌会不会跟」下注。有位置且收尾时可以提高下注频率，但仍以事实里的弃牌率/画像为准。"""

HEADS_UP_SECTION = """## 场景：单挑
- 单挑的范围最宽、诈唬频率最高：不要套用多人池的紧标准，顶对/中对都可能值得跟注或价值下注。
- 位置的价值变大：有位置可以多跟注/过牌，让对手继续开火；无位置时用下注或过牌-加注主动定义底池。
- 对手画像优先：对 LAG/maniac 减少薄价值、增加跟注；对 nit 减少诈唬、尊重其加注。
- 听牌按「场景：听牌」处理：无位置纯听牌默认过牌、强听才考虑半诈唬；本段「主动定义底池」只适用于成牌/中等牌，不覆盖听牌段。"""

DRAW_SECTION = """## 场景：听牌
- 先分清「成牌率」和「对范围胜率」：四二法则是到河牌的中牌概率，不是摊牌胜率；后面还会下注时，用赔率 + 隐含赔率 + 对手线路，不要直接和单街门槛比。
- 没有弃牌率时，纯听牌不主动下注（默认过牌或跟小注）；强听（≥8 outs、坚果听、组合听）才考虑半诈唬，多人池或无位置默认过牌。
- 面对全下时按剩余牌发完算；被跟注后不要再用「没中」当理由继续投入。
- 短码（本手总筹码 ≤20BB）时隐含赔率按 0 算；本段不覆盖短码段。"""

RIVER_SECTION = """## 场景：河牌
- 河牌没有后续牌：纯听牌等同空气；已经成对的牌仍有摊牌价值，过牌不等于放弃。
- 价值下注只问「更差的牌会不会跟」；诈唬只问「更好的牌会不会弃」。两者都要有事实（弃牌率/画像/线路）支撑，不能只凭「我有一对」。
- 被加注时按赔率 + 对手范围判断（没有范围段时按价格/线路/保守先验），不默认弃；非坚果默认不打三条街；但对手线路越像价值，越不要把「我现在有牌」当跟注理由。
- 深码河牌不要用 overbet/all-in 代替正常价值尺度，除非事实显示对手是跟注站。
- 短码的「翻前推/弃」只在翻前；河牌按本段赔率/范围判断，不默认弃。"""


def short_stack_section(sit) -> str | None:
    """Hero 本手总筹码短：翻前推/弃与翻后套池口径。"""
    snap = sit.snap
    total = _hero_total_stack(snap)
    if total is None or total > SHORT_STACK_BB + 1e-9:
        return None
    if snap.hero_stack is None or snap.hero_stack <= 0:
        return None
    return SHORT_STACK_SECTION.format(stack=_fmt_num(total))


def multiway_section(sit) -> str | None:
    """3 人池/多人池：降级、薄价值、跟注范围、位置权重。"""
    opponents = sit.opponents_in_hand
    if opponents is None or opponents < 2:
        return None
    if not any(_can_act(o) for o in sit.snap.opponents):
        return None
    players = opponents + 1
    return MULTIWAY_SECTION.format(
        players=players, title="多人池" if players >= 4 else "3 人池")


def heads_up_section(sit) -> str | None:
    """单挑：范围最宽、位置价值最大。"""
    if sit.opponents_in_hand != 1:
        return None
    if not any(_can_act(o) for o in sit.snap.opponents):
        return None
    return HEADS_UP_SECTION


def draw_section(sit) -> str | None:
    """Hero 是听牌（纯听或成牌+强听）：成牌率 vs 胜率、半诈唬边界。"""
    snap = sit.snap
    if snap.street in (PREFLOP, RIVER):
        return None
    tier = sit.hand_tier
    is_draw = tier in (STRONG_DRAW, WEAK_DRAW)
    combo_draw = (not is_draw and sit.outs is not None and sit.outs >= 8
                  and tier in (OVERPAIR, TPTK, TP_WEAK, MID_BOTTOM))
    if not (is_draw or combo_draw):
        return None
    return DRAW_SECTION


def river_section(sit) -> str | None:
    """河牌：摊牌价值、价值/诈唬的判据、被加注不默认弃。"""
    if sit.snap.street != RIVER:
        return None
    return RIVER_SECTION


# 注册表：顺序即输出顺序。前注局在 system/ante.py（它需要盲注数据）。
SCENARIO_SECTIONS = (
    short_stack_section,
    multiway_section,
    heads_up_section,
    draw_section,
    river_section,
)


def scenario_sections(sit) -> list[str]:
    """返回当前局面成立的所有场景段（保持注册顺序）。"""
    out: list[str] = []
    for render in SCENARIO_SECTIONS:
        text = render(sit)
        if text:
            out.append(text)
    return out
