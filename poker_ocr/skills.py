"""教练技能库：按**当前行动街**挑技能，供实时 AI 决策使用。

正文来自 `poker_ocr/skills_md_en/*.md` —— 大部分是 `skills_md/*.md` 的**英文翻译**
（`skills_md/` 保留中文，逐字同步 call-back 的 `services/skills/*.md`；JEV 等模型
读中文较差，所以给模型的提示词用英文副本）。live 专属的新增篇（如
`preflop-limp.md`）没有中文对应的副本。路由元数据（触发器 / 优先级）按 live
的口径在 Python 侧重写，而不是照搬 Go 的 ReviewHand 模型。

与 call-back 的差异（刻意的）：

  · **只按「当前这一条街」路由**。call-back 是复盘整手牌，会把每条记录到的街的
    技能都展开；实时链路只有当前决策点，翻前的技能在河牌没有意义。
  · **牌力维度用 coach.read_hand 的分档**（强成牌/超对/顶对…），因为 live 现场
    拿到的就是 `coach.Read`，不再另写一套 evaluator。
  · **mindset 在 live 永不命中** —— 它的触发词来自用户手写的「我的想法」，
    实时链路没有这个输入。保留在目录里只为和 call-back 的方法论保持一致。
  · 展开篇数上限沿用 call-back 的口径（常驻不占额度），但 live 每条街能命中的
    专项技能本来就少，上限只是防御性兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ─────────────────────────── 街 / 局面常量 ───────────────────────────

PREFLOP = "preflop"
FLOP = "flop"
TURN = "turn"
RIVER = "river"
POSTFLOP_STREETS = (FLOP, TURN, RIVER)

# 翻前局面形态，判据是「hero 行动前有几次加注」，与 call-back 一致。
# 没人加注时再分两种：没人入池（unopened）和已经有人溜入（limped）——
# 「第一个入池必须加注」只适用于前者，溜入池里平跟是正常线路。
SHAPE_UNOPENED = "unopened"
SHAPE_LIMPED = "limped"
SHAPE_SINGLE_RAISE = "single-raise"
SHAPE_THREE_BET = "three-bet"

# 底池类型
POT_HU = "hu"
POT_MULTI = "multi"

# 非驻留技能最多展开几篇。live 每条街基本只命中 1-3 篇，这里只是防止
# 将来触发器写宽了把提示词撑爆
MAX_EXPANDED = 6

SKILLS_DIR = Path(__file__).resolve().parent / "skills_md_en"


@dataclass(frozen=True)
class SkillTrigger:
    """一篇技能的路由条件。各非零维度取交集，全部通过才算命中。

    全零（且 `always=False`）= 永不命中。刻意没写「未设置」标记：
    和 call-back 一样，没写适用范围的技能不该默认命中。
    """

    always: bool = False
    streets: tuple[str, ...] = ()
    positions: tuple[str, ...] = ()
    pot_types: tuple[str, ...] = ()
    table_sizes: tuple[int, ...] = ()
    preflop_shapes: tuple[str, ...] = ()
    min_villains: int = 0
    cbet_spot: bool = False
    # 牌力：coach.evaluate 的牌型下标下限（含）
    min_category: int = -1
    # 只在这些 coach 分档里（精确匹配）
    tiers: tuple[str, ...] = ()
    require_draw: bool = False
    # 「打牌面」不是我的牌，人人都有 —— 成牌类技能必须排除
    exclude_plays_board: bool = False
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Skill:
    code: str
    title: str
    when: str
    summary: str
    priority: int
    trigger: SkillTrigger
    body: str


@dataclass(frozen=True)
class SkillContext:
    """一次实时决策点上看技能所需的事实。

    这里只放**能可靠拿到**的东西：位置、人数、底池类型、翻前形态、牌力分档。
    对手形象一律「未知」—— live 没有跨手样本，任何剥削性偏离都不该发生。
    """

    street: str
    board: str = ""                       # 紧凑牌码，如 Ks7d2c
    hero_cards: str = ""                  # 如 AhQd
    hero_position: str | None = None
    table_size: int = 0
    opponents: int = 0                    # 当前还在场的对手数
    preflop_shapes: tuple[str, ...] = ()
    hero_is_pfr: bool = False
    cbet_spot: bool = False
    villains_known: int = 0
    category: int | None = None           # coach 牌型下标；翻前/读不出时为 None
    tier: str | None = None               # coach 分档键
    has_draw: bool = False
    plays_board: bool = False
    text: str = ""                        # 用户的自由文本；live 恒为空

    @property
    def pot_type(self) -> str:
        return POT_MULTI if self.opponents >= 2 else POT_HU


# ─────────────────────────── 注册表 ───────────────────────────

# 正文文件与 trigger 的对应关系。Priority 与 call-back 保持一致：展开顺序、
# 抢名额时的先后都看它。
_DEFS: tuple[tuple[str, str, str, str, int, SkillTrigger, str], ...] = (
    ("core-stance", "Core Stance & Decision Flow", "Every hand",
     "Ask first whether you can bet or raise, then whether to check or fold; "
     "a bet or check you cannot justify is a leak",
     100, SkillTrigger(always=True), "core-stance.md"),
    ("odds-table", "Odds & Probability Quick Reference",
     "Whenever you need pot odds, outs, or draw-equity numbers",
     "1/3 pot needs 20% equity, 1/2 pot 25%, full pot 33%, 2× pot 40%; on the turn, "
     "calling with 9 outs or fewer facing a 1/2-pot-or-bigger bet is a mistake",
     95, SkillTrigger(always=True), "odds-table.md"),
    ("opponent-read", "Opponent Reading",
     "At least one opponent's name or position is recorded this hand",
     "Each of the five player types has a counter-strategy; below 3 samples classify as "
     "'unknown' and play the standard line — no exploitative deviation",
     88, SkillTrigger(min_villains=1), "opponent-read.md"),
    # ---- river ----
    ("river-value", "River Value Betting", "On the river with a strong hand, considering value",
     "Size with a bell curve so value bets don't look like value bets; "
     "check-raise on the river less than 1 in 10 times",
     82,
     SkillTrigger(streets=(RIVER,), tiers=("强成牌",), exclude_plays_board=True),
     "river-value.md"),
    ("river-medium", "River Medium-Strength Hands",
     "On the river with medium strength (including strong hands weakened by the board)",
     "Betting a medium hand out of position on the river is one of the worst errors — "
     "check to induce; if the opponent bets a blank board, lean toward reading it as a bluff",
     81,
     SkillTrigger(streets=(RIVER,),
                  tiers=("超对", "顶对强踢", "顶对弱踢", "中底对")),
     "river-medium.md"),
    # ---- turn / flop ----
    ("postflop-turn", "Turn Decisions", "The hand reached the turn",
     "The turn is no place for fancy play — bet when you have the best hand; "
     "when a scare card hits, yield the betting lead and check-call to control the pot",
     78, SkillTrigger(streets=(TURN,)), "postflop-turn.md"),
    ("postflop-made-hand", "Playing Made Hands", "Postflop with two pair or better",
     "Two pair only improves 17% of the time and top-and-bottom two pair is the most fragile; "
     "with a set, make the opponent make the biggest mistake; size straights and flushes "
     "by board structure",
     76,
     SkillTrigger(streets=POSTFLOP_STREETS, min_category=2,  # TWO_PAIR
                  exclude_plays_board=True),
     "postflop-made-hand.md"),
    ("postflop-draw", "Draws & Semi-Bluffs", "You are on a flush or straight draw",
     "A premium draw can be played as a strong hand heads-up — betting gives you two ways "
     "to win; on the turn, call only on implied odds",
     74, SkillTrigger(streets=POSTFLOP_STREETS, require_draw=True),
     "postflop-draw.md"),
    ("postflop-cbet", "Continuation Betting",
     "You were the last preflop raiser and it's checked to you on the flop",
     "Take frequency by heads-up scenario (65% vs a call in position, 85% when "
     "out-of-position opponents check); use the four-factor sizing method; "
     "bet first on paired boards",
     72, SkillTrigger(streets=(FLOP,), cbet_spot=True), "postflop-cbet.md"),
    ("postflop-multiway", "Multiway Pot Rules", "Three or more players remain postflop",
     "The more players, the less you bluff; if you believe you have the best hand, "
     "almost always bet and almost never slow-play",
     70,
     SkillTrigger(streets=POSTFLOP_STREETS, pot_types=(POT_MULTI,)),
     "postflop-multiway.md"),
    # ---- preflop ----
    ("preflop-3bet-pot", "Preflop 3-Bet Pots",
     "There has already been a re-raise before your preflop action",
     "Re-raise size depends on position; committing more than half your stack preflop "
     "makes you 100% pot-committed, so bluffing is pointless",
     66, SkillTrigger(streets=(PREFLOP,),
                      preflop_shapes=(SHAPE_THREE_BET,)),
     "preflop-3bet-pot.md"),
    ("preflop-open", "Preflop Open-Raising", "No one has raised before your preflop action",
     "2.5-3× early, 3.5-4× late, never size by hand strength; always raise as the first to "
     "enter — limping is allowed only in three situations",
     62, SkillTrigger(streets=(PREFLOP,),
                      preflop_shapes=(SHAPE_UNOPENED,)),
     "preflop-open.md"),
    ("preflop-limp", "Preflop Limped Pots & Set Mining",
     "Players have limped and no one has raised before your preflop action",
     "Over-limp pocket pairs when the call is small and the effective stack is at least "
     "15× the call; a set flops 11.8% and the dead money pays for the misses",
     61, SkillTrigger(streets=(PREFLOP,),
                      preflop_shapes=(SHAPE_LIMPED,)),
     "preflop-limp.md"),
    ("preflop-vs-open", "Preflop vs an Open", "There has been one raise before your preflop action",
     "In position lean toward calling to trap; in the blinds lean toward re-raising; "
     "almost always re-raise KK/QQ",
     60, SkillTrigger(streets=(PREFLOP,),
                      preflop_shapes=(SHAPE_SINGLE_RAISE,)),
     "preflop-vs-open.md"),
    ("mindset", "Mindset & Bankroll",
     "The player's own notes mention tilt, emotions, downswing, or bankroll",
     "A bad beat is a cost of doing business; only review emotions when you were ahead "
     "when the money went in; variance over fewer than 2000 hands is no basis for "
     "changing strategy",
     86,
     SkillTrigger(keywords=("上头", "情绪", "下风", "tilt", "追损", "破产", "心态")),
     "mindset.md"),
)


class SkillLibraryError(RuntimeError):
    """技能正文加载失败。和 call-back 一样，这里不降级：静默变成空技能库会让
    所有建议失去依据，而且没有任何报错。"""


def load_skills(directory: Path = SKILLS_DIR) -> list[Skill]:
    """读取全部技能正文。文件缺失 / 为空一律抛错。"""
    if not directory.is_dir():
        raise SkillLibraryError(f"技能目录不存在：{directory}")

    out: list[Skill] = []
    for code, title, when, summary, priority, trigger, filename in _DEFS:
        path = directory / filename
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillLibraryError(f"技能 {code} 的正文读取失败（{path}）：{exc}") from exc
        if not body.strip():
            raise SkillLibraryError(f"技能 {code} 的正文是空的（{path}）")
        out.append(Skill(code=code, title=title, when=when, summary=summary,
                         priority=priority, trigger=trigger, body=body))
    return out


# 进程内缓存：正文不会在运行中变，没必要每手牌重读磁盘
_LIBRARY: list[Skill] | None = None


def all_skills() -> list[Skill]:
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = load_skills()
    return _LIBRARY


# ─────────────────────────── 路由 ───────────────────────────

def _anchor_streets(trigger: SkillTrigger) -> tuple[str, ...]:
    """这篇技能锚定在哪条街。

    与 call-back 同一口径：翻前形态 → 翻前；显式 Streets；持续下注 → 翻牌。
    都没有（对手读牌、心态）则不属于任何街，不参与「按街加载」。
    """
    if trigger.preflop_shapes:
        return (PREFLOP,)
    if trigger.streets:
        return trigger.streets
    if trigger.cbet_spot:
        return (FLOP,)
    return ()


def match_skill(trigger: SkillTrigger, ctx: SkillContext) -> tuple[int, bool]:
    """返回 (命中维度数, 是否命中)。维度越多说明触发器填得越具体。"""
    if trigger.always:
        return 0, True

    hits = 0

    if trigger.streets:
        if ctx.street not in trigger.streets:
            return 0, False
        hits += 1
    if trigger.positions:
        if ctx.hero_position not in trigger.positions:
            return 0, False
        hits += 1
    if trigger.pot_types:
        if ctx.pot_type not in trigger.pot_types:
            return 0, False
        hits += 1
    if trigger.table_sizes:
        if ctx.table_size not in trigger.table_sizes:
            return 0, False
        hits += 1
    if trigger.preflop_shapes:
        if not any(s in ctx.preflop_shapes for s in trigger.preflop_shapes):
            return 0, False
        hits += 1
    if trigger.min_villains:
        if ctx.villains_known < trigger.min_villains:
            return 0, False
        hits += 1
    if trigger.cbet_spot:
        if not ctx.cbet_spot:
            return 0, False
        hits += 1
    if trigger.min_category >= 0:
        if ctx.category is None or ctx.category < trigger.min_category:
            return 0, False
        hits += 1
    if trigger.tiers:
        if ctx.tier not in trigger.tiers:
            return 0, False
        hits += 1
    if trigger.require_draw:
        if not ctx.has_draw:
            return 0, False
        hits += 1
    if trigger.exclude_plays_board and ctx.plays_board:
        return 0, False
    if trigger.keywords:
        haystack = ctx.text
        if not any(kw and kw in haystack for kw in trigger.keywords):
            return 0, False
        hits += 1

    return hits, hits > 0


def select_skills(context: SkillContext, all_: list[Skill] | None = None) -> list[Skill]:
    """挑出这次决策要展开的技能：常驻 + 当前街命中的专项。

    刻意不做 call-back 那套「每街保底」：live 只有当前一条街，保底就是
    「锚定当前街的技能只要命中就展开」，没必要再抢名额。
    """
    skills = all_skills() if all_ is None else all_
    matched: list[tuple[int, Skill]] = []
    resident: list[Skill] = []

    for skill in skills:
        if skill.trigger.always:
            resident.append(skill)
            continue
        anchors = _anchor_streets(skill.trigger)
        if anchors and context.street not in anchors:
            continue
        hits, ok = match_skill(skill.trigger, context)
        if ok:
            matched.append((hits, skill))

    # 排序必须完全确定：同一决策点两次请求的提示词要一模一样，否则
    # input_snapshot / 复现都无从谈起。最后一级用 code 兜底
    matched.sort(key=lambda item: (-item[1].priority, -item[0], item[1].code))
    chosen = resident + [s for _, s in matched[:MAX_EXPANDED]]
    chosen.sort(key=lambda s: (-s.priority, s.code))
    return chosen


# ─────────────────────────── 渲染 ───────────────────────────

def render_catalog(all_: list[Skill], selected: list[Skill]) -> str:
    """第一层：技能目录，永远注入。★ = 本次已展开全文。"""
    expanded = {s.code for s in selected}
    width = max((len(s.code) for s in all_), default=0)

    lines = [
        "## Coach Skill Catalog",
        "The advice this session is based on Phil Gordon's \"Little Green Book\" "
        "(cash-game sections only).",
        "★ = full text expanded below; unexpanded entries show only a one-line conclusion — "
        "do not invent details from a one-line summary.",
        "",
    ]
    for skill in all_:
        pad = " " * (width - len(skill.code))
        if skill.code in expanded:
            lines.append(f"★ {skill.code}{pad} — {skill.title}")
        else:
            lines.append(f"  {skill.code}{pad} — {skill.when} | conclusion: {skill.summary}")
    return "\n".join(lines) + "\n"


def render_block(selected: list[Skill]) -> str:
    """第二层：选中技能的正文。正文原样来自 .md，中间不插任何东西。"""
    sections: list[str] = []
    for skill in selected:
        section = f"## {skill.title}\n{skill.body}"
        if not section.endswith("\n"):
            section += "\n"
        sections.append(section)
    return "\n".join(sections)


def body_chars(selected: list[Skill]) -> int:
    return sum(len(s.body) for s in selected)
