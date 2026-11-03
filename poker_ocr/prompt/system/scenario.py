"""条件场景段：只在该场景成立时追加，并按牌型只渲染对应口径。

设计原则（来自 `docs/提示词审阅-2026-11-02-系统性冲突.md` 与
`docs/提示词-听牌成牌双规则梳理.md`）：

  · **条件句优先于绝对句**：写「如果…则…；除非…」，不写「只用/绝不」。
  · **不重复事实**：人数、SPR、outs、赔率都在 user 事实块里；这里只给
    「拿到这些数字之后怎么权衡」。
  · **牌型与局面对齐**：牌型默认动作在 `hand_type.py`；本文件只写局面修正，
    而且每个场景按 `sit.hand_class` 只渲染听/成/空气中的一支，不再让听牌
    规则藏在成牌句里。

新增场景：在本文件加一个 `xxx_section(sit)`，返回 `str | None`，然后
注册到 `SCENARIO_SECTIONS`。不要在 `SYSTEM_SECTIONS`（恒定段）里加场景规则。
"""

from __future__ import annotations

from ...hand_track import PREFLOP, RIVER
from ...pot_math import _can_act, _hero_total_stack
from ..shared.format import _fmt_num

# 短码线：Hero 本手总筹码（后手 + 本街已下注）≤ 这个数时给口径。
SHORT_STACK_BB = 20.0

_SHORT_PREFLOP = ("- 翻前：本手总筹码 ≤20BB 且面对加注，或轮到我开池时，"
                  "优先把 all-in 或弃牌当作默认分支，"
                  "不要用「先开池、被加注再弃」的尺度把自己套进去；"
                  "BB 无人加注时按过牌/加注，不套用推/弃。")
_SHORT_POSTFLOP = "- 翻后不要套用翻前推/弃，按 SPR 和人数决定。"
_SHORT_MADE = ("- 成牌：SPR<3 时单挑/3 人池顶对可以套池，4 人以上仍然降级；"
               "SPR 约 3 时按人数/牌力，不自动套池。")
_SHORT_DRAW = ("- 听牌：本手总筹码越少隐含赔率越差——按即时赔率算，"
               "不要假设中牌后还能收钱。")
_SHORT_AIR = "- 空气：短码不要用空气跟注或诈唬；过牌或弃牌。"

_MW_MADE = """- 人数（含 Hero）：4 人以上顶对/超对降级；3 人池先按人数收紧，SPR<3 时顶对可以套池。
- 人数会放大「至少一家领先」的概率：两对+ 打光前先看公对面和更大的两对。
- 下注先问「更差的牌会不会跟」：人多时薄价值更难被跟，强价值偏向直接拿价值；诈唬需要更强的牌面或更少的对手。
- 跟注时赔率变好，但身后还有人能加注：跟注范围要比单挑更紧，别把「赔率够」当成「可以跟」。
- 位置权重更高：无位置默认过牌控池；两对+的强价值牌仍可按「更差的牌会不会跟」下注。"""

_MW_DRAW = """- 人多 → 弃牌率低：纯听默认过牌；强听要有「剥削程度 L2+、弃牌率 ≥50%」才下注诈唬（见牌型段）。
- 跟注用隐含赔率，但身后还有人能加注时隐含赔率要打折。
- 无位置默认过牌；不要用纯听牌在 3 人以上主动做大池。"""

_MW_AIR = """- 人多 → 诈唬需要更强的牌面或更少的对手；没有「剥削程度 L3、弃牌率 ≥50%」就过牌。
- 跟注只按价格/抓诈，不按「反超 outs」。"""

_HU_MADE = """- 单挑范围最宽：顶对/中对都可能值得跟注或价值下注，不套用多人池的紧标准。
- 有位置可以在同等赔率下更多实现权益（不是降低门槛）；无位置可以用下注或过牌-加注主动定义底池。
- 对手画像优先：对 LAG/maniac 减少诈唬、增加抓诈/跟注；薄价值仍看更差的牌会不会跟（他们跟得宽，薄价值反而更可下）；对 nit 减少诈唬、尊重其加注。"""

_HU_DRAW = """- 单挑弃牌率更高：强听有「剥削程度 L2+、弃牌率 ≥50%」时可按牌型段半诈唬；纯听仍然默认过牌。
- 成牌的「主动定义底池」不适用于纯听牌。"""

_HU_AIR = """- 单挑诈唬频率更高，但空气仍必须有「剥削程度 L3、弃牌率 ≥50%」的事实才开火；否则过牌/弃牌。"""

_THREE_BET = """- 对手范围极度收缩：按强范围（QQ+/AK、少量诈唬）评估；不要套用单加注池的宽范围。
- 本条优先于单挑的「范围最宽」：3-bet+ 池的对手范围比单挑窄得多。"""

_RIVER_MADE = """- 河牌只剩一条下注街：价值只问「更差的牌会不会跟」，诈唬只问「更好的牌会不会弃」；两者都要有事实（弃牌率/画像/线路）支撑，不能只凭「我有一对」。
- 已成对仍有摊牌价值，过牌不等于放弃；被加注时按赔率 + 对手范围判断，不默认弃。
- 非坚果的价值下注要控制尺度；对手线路越像价值，越不要把「我现在有牌」当跟注理由。"""

_RIVER_AIR = """- 河牌没有摊牌价值（或摊牌价值很薄）时：只剩诈唬（需「剥削程度 L3、弃牌率 ≥50%」）或弃牌。
- 河牌听牌等同空气，不要拿「反超 outs」当跟注权益。"""


def _by_class(sit, *, made: str, draw: str, air: str) -> str:
    """按当前牌型选一支；combo 走成牌口径（牌型段已给听牌修正）。"""
    cls = sit.hand_class
    if cls == "draw":
        return draw
    if cls in ("made", "combo"):
        return made
    return air


def short_stack_section(sit) -> str | None:
    """Hero 本手总筹码短：翻前推/弃 + 翻后按牌型给 SPR/隐含赔率口径。"""
    snap = sit.snap
    total = _hero_total_stack(snap)
    if total is None or total > SHORT_STACK_BB + 1e-9:
        return None
    if snap.hero_stack is None or snap.hero_stack <= 0:
        return None
    lines = [f"## 场景：短码（本手总筹码约 {_fmt_num(total)}BB）", _SHORT_PREFLOP]
    if snap.street != PREFLOP:
        lines.append(_SHORT_POSTFLOP)
        lines.append(_by_class(sit, made=_SHORT_MADE, draw=_SHORT_DRAW,
                               air=_SHORT_AIR))
    return "\n".join(lines)


def multiway_section(sit) -> str | None:
    """3 人池/多人池：按牌型给人数降级、薄价值、隐含赔率与位置口径。"""
    if sit.snap.street == PREFLOP:
        return None
    opponents = sit.opponents_in_hand
    if opponents is None or opponents < 2:
        return None
    if not any(_can_act(o) for o in sit.snap.opponents):
        return None
    players = opponents + 1
    title = "多人池" if players >= 4 else "3 人池"
    body = _by_class(sit, made=_MW_MADE, draw=_MW_DRAW, air=_MW_AIR)
    return f"## 场景：{title}（本手 {players} 人）\n{body}"


def heads_up_section(sit) -> str | None:
    """单挑：范围更宽、位置价值更大；按牌型给不同的开火/跟注口径。"""
    if sit.snap.street == PREFLOP:
        return None
    if sit.opponents_in_hand != 1:
        return None
    if not any(_can_act(o) for o in sit.snap.opponents):
        return None
    body = _by_class(sit, made=_HU_MADE, draw=_HU_DRAW, air=_HU_AIR)
    return "## 场景：单挑（2 人）\n" + body


def three_bet_section(sit) -> str | None:
    """3-bet+ 池：对手范围极度收缩，不套用单加注池的宽范围。"""
    if sit.snap.street == PREFLOP:
        return None
    if sit.pot_type != "3-bet+ pot":
        return None
    return "## 场景：3-bet+ 池\n" + _THREE_BET


def river_section(sit) -> str | None:
    """河牌：按牌型给摊牌价值/价值下注或「听牌已死=空气」。"""
    if sit.snap.street != RIVER:
        return None
    body = _by_class(sit, made=_RIVER_MADE, draw=_RIVER_AIR, air=_RIVER_AIR)
    return "## 场景：河牌\n" + body


# 注册表：顺序即输出顺序。前注局在 system/ante.py（它需要盲注数据）。
# 牌型段（听牌/成牌/空气）在 system/hand_type.py，先于这些场景段注入。
SCENARIO_SECTIONS = (
    short_stack_section,
    multiway_section,
    heads_up_section,
    three_bet_section,
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
