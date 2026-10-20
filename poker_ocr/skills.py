"""教练技能库：按**当前行动街**挑技能，供实时 AI 决策使用。

正文来自 `poker_ocr/skills_md/*.md`，与 call-back 的
`services/skills/*.md` **逐字一致**（body 是单一事实源，两边同步）；路由元数据
（触发器 / 优先级）按 live 的口径在 Python 侧重写，而不是照搬 Go 的 ReviewHand 模型。

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

# 翻前局面形态，判据是「hero 行动前有几次加注」，与 call-back 一致
SHAPE_UNOPENED = "unopened"
SHAPE_SINGLE_RAISE = "single-raise"
SHAPE_THREE_BET = "three-bet"

# 底池类型
POT_HU = "hu"
POT_MULTI = "multi"

# 非驻留技能最多展开几篇。live 每条街基本只命中 1-3 篇，这里只是防止
# 将来触发器写宽了把提示词撑爆
MAX_EXPANDED = 6

SKILLS_DIR = Path(__file__).resolve().parent / "skills_md"


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
    ("core-stance", "底层立场与决策流程", "每一手牌",
     "先问能不能下注或加注，再问要不要过牌或弃牌；说不出理由的下注与过牌都是漏损",
     100, SkillTrigger(always=True), "core-stance.md"),
    ("odds-table", "赔率与概率速查", "任何要算赔率、outs 或成牌概率的地方",
     "1/3 池需 20% 胜率、1/2 池 25%、1 倍池 33%、2 倍池 40%；转牌面对 1/2 池以上的下注，"
     "9 张 outs 及以下的听牌跟注都是错的",
     95, SkillTrigger(always=True), "odds-table.md"),
    ("opponent-read", "对手读牌", "这手牌至少记录了一个对手的名字或位置",
     "五格形象各有对策；样本不足 3 次归「未知」按标准线打，不做剥削性偏离",
     88, SkillTrigger(min_villains=1), "opponent-read.md"),
    # ---- 河牌 ----
    ("river-value", "河牌价值下注", "河牌你持强牌，考虑收价值",
     "用钟形曲线定尺度，让价值下注看起来不像价值下注；过牌-加注使用率低于 1/10",
     82,
     SkillTrigger(streets=(RIVER,), tiers=("强成牌",), exclude_plays_board=True),
     "river-value.md"),
    ("river-medium", "河牌中等牌力", "河牌你持中等牌力（含被牌面削弱过的强牌）",
     "无位置持中等牌下注是最糟的错误之一，该过牌引诱诈唬；对手在无关牌面下注，倾向认定是诈唬",
     81,
     SkillTrigger(streets=(RIVER,),
                  tiers=("超对", "顶对强踢", "顶对弱踢", "中底对")),
     "river-medium.md"),
    # ---- 转牌 / 翻牌 ----
    ("postflop-turn", "转牌决策", "这手牌打到了转牌",
     "转牌不是耍花招的时候，有最好的牌就下注；吓人牌出现时把开火权让给对手、过牌-跟注控池",
     78, SkillTrigger(streets=(TURN,)), "postflop-turn.md"),
    ("postflop-made-hand", "成牌打法", "翻后你持两对或更强的成牌",
     "两对只有 17% 会再变大、顶底两对最脆弱；三条要让对手犯最大的错；"
     "顺子与同花按牌面结构定尺度",
     76,
     SkillTrigger(streets=POSTFLOP_STREETS, min_category=2,  # TWO_PAIR
                  exclude_plays_board=True),
     "postflop-made-hand.md"),
    ("postflop-draw", "听牌与半诈唬", "你在听花或听顺",
     "优质抽牌对单一对手可以当强牌打，主动下注有两条赢路；转牌跟注只看隐含赔率",
     74, SkillTrigger(streets=POSTFLOP_STREETS, require_draw=True),
     "postflop-draw.md"),
    ("postflop-cbet", "持续下注", "你是翻前最后的加注者，翻牌轮到你时还没有人下注",
     "按单挑情形取频率（位置好的对手跟注 65%、位置差的对手过牌 85%）；"
     "尺度用四因素法；对子面先下注",
     72, SkillTrigger(streets=(FLOP,), cbet_spot=True), "postflop-cbet.md"),
    ("postflop-multiway", "多人池铁律", "翻后还有三个以上的人",
     "人越多越不诈唬；认为牌最大就几乎肯定下注、几乎从不慢打",
     70,
     SkillTrigger(streets=POSTFLOP_STREETS, pot_types=(POT_MULTI,)),
     "postflop-multiway.md"),
    # ---- 翻前 ----
    ("preflop-3bet-pot", "翻前 3bet 底池", "翻前你行动时已经出现过再加注",
     "再加注尺度按位置定；翻前投入超过一半筹码即 100% 套池，此时诈唬毫无意义",
     66, SkillTrigger(streets=(PREFLOP,),
                      preflop_shapes=(SHAPE_THREE_BET,)),
     "preflop-3bet-pot.md"),
    ("preflop-open", "翻前开池", "翻前你行动时还没有人加注",
     "前位 2.5-3 倍、后位 3.5-4 倍，不按牌力调尺度；第一个入池总是加注，"
     "平跟只允许三种情形",
     62, SkillTrigger(streets=(PREFLOP,),
                      preflop_shapes=(SHAPE_UNOPENED,)),
     "preflop-open.md"),
    ("preflop-vs-open", "翻前面对开池", "翻前你行动时前面已有一次加注",
     "位置好倾向跟注设陷阱、盲注位倾向再加注；KK/QQ 几乎总是再加注",
     60, SkillTrigger(streets=(PREFLOP,),
                      preflop_shapes=(SHAPE_SINGLE_RAISE,)),
     "preflop-vs-open.md"),
    ("mindset", "心态与资金", "玩家自己的想法里出现情绪、上头、下风或资金相关的字眼",
     "Bad beat 是营业成本；只有「当时是领先下注」才值得复盘情绪；"
     "2000 手以内的波动不构成改策略的依据",
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
        "## 教练技能目录",
        "本次建议的依据来自 Phil Gordon《德州扑克小绿皮书》（仅现金局部分）。",
        "★ = 本次已展开全文，正文在下面；未展开的只有一句结论，"
        "不要凭一句话结论编细则。",
        "",
    ]
    for skill in all_:
        pad = " " * (width - len(skill.code))
        if skill.code in expanded:
            lines.append(f"★ {skill.code}{pad} — {skill.title}")
        else:
            lines.append(f"  {skill.code}{pad} — {skill.when}｜结论：{skill.summary}")
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
