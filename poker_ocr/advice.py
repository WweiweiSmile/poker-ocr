"""实时 AI 决策建议：按当前行动街加载技能，把完整行动线交给模型，只回一个选项。

与 call-back 复盘的关系：**同一套方法论、同一种「目录 + 展开正文」的注入方式**
（见 skills.py），但输入是 `HandSnapshot`（实时链路逐帧累积出来的），不是
ReviewHand；输出也不是长篇点评，而是从固定选项里**选一个**。

为什么输出是「选项编号」而不是自由文本：

  · 实时场景下人要的是一秒内能执行的动作，不是分析文章。
  · 选项把尺度约束死了（1/3、1/2、2/3、1、1.2 倍池），模型不可能编出不存在的尺度。
  · 选项由本模块按现场底池算好金额，模型只做选择 —— 它不需要会算底池，
    也就不会算错。这一点和 coach.py「确定性规则不交给模型」是同一条原则。

调用协议：OpenAI 兼容的 /chat/completions（与 call-back 的 AIClient 同口径），
凭据来自环境变量 AI_SERVER_API_KEY / AI_SERVER_BASE_URL / AI_SERVER_MODEL。
**非流式**：一次建议的正文只有几十个 token，流式只会增加复杂度；超时用
`socket` 级的总时长控制（默认 20 秒，实时链路等不起更久）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import skills
from .coach import board_texture, opponent_strength, parse_cards, read_hand
from .hand_track import ActionRecord, HandSnapshot, PREFLOP

# ─────────────────────────── 选项 ───────────────────────────

# 行动线里 hero 的标识。与 coach.advise 的 hero_position 口径一致
HERO_ACTOR = "hero"

# 加注尺度：名字 → 底池倍数。「满池 1.2」是超池，用于需要施压/劝退的场合
RAISE_FRACTIONS: dict[str, float] = {
    "1/3": 1 / 3,
    "1/2": 0.5,
    "2/3": 2 / 3,
    "1": 1.0,
    "1.2": 1.2,
}

# 用户指定的选项顺序。AI 只能返回这里的 id
OPTION_ORDER = ("fold", "call", "raise-1/3", "raise-1/2", "raise-2/3",
                "raise-1", "raise-1.2")

OPTION_LABELS = {
    "fold": "弃牌",
    "call": "跟注",
    "raise-1/3": "加注到底池 1/3",
    "raise-1/2": "加注到底池 1/2",
    "raise-2/3": "加注到底池 2/3",
    "raise-1": "加注到底池 1 倍",
    "raise-1.2": "加注到底池 1.2 倍",
}


@dataclass(frozen=True)
class Option:
    """一个可执行选项。amount 是**加注后的本街总额**（BB），跟注为需跟额。"""

    id: str
    label: str
    kind: str                     # check | call | fold | raise
    amount: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        d = {"id": self.id, "label": self.label, "kind": self.kind}
        if self.amount is not None:
            d["amount_bb"] = round(self.amount, 2)
        if self.note:
            d["note"] = self.note
        return d


def _round_bb(v: float) -> float:
    return round(v, 2)


# 翻前开池的基础倍数（bb），按位置分档，来自 preflop-open.md：
# 前位 2.5-3.0 / 中位 3.0-3.5 / 后位 3.5-4.0 / 小盲 3.0。
# 大盲在溜入池里再加注时，尺度要比开池更大（没有位置劣势补偿，直接收池）
_OPEN_BASES: dict[str, tuple[float, ...]] = {
    "UTG": (2.5, 2.75, 3.0),
    "UTG+1": (2.5, 2.75, 3.0),
    "UTG+2": (2.5, 2.75, 3.0),
    "LJ": (3.0, 3.25, 3.5),
    "HJ": (3.0, 3.25, 3.5),
    "CO": (3.5, 3.75, 4.0),
    "BTN": (3.5, 3.75, 4.0),
    "SB": (3.0, 3.25, 3.5),
    "BB": (3.5, 4.0, 4.5),
}


def _cap(target: float, hero_max: float | None, min_raise_to: float) -> tuple[float, str]:
    """把目标额修正到合法范围：低于最小加注抬上去，高于后手封顶为全下。"""
    note = ""
    if target < min_raise_to:
        target = min_raise_to
        note = "已抬到最小加注额"
    if hero_max is not None and target >= hero_max - 1e-9:
        target = hero_max
        note = "后手不够，等同全下"
    return _round_bb(target), note


def build_preflop_options(snap: HandSnapshot) -> list[Option]:
    """翻前选项：开池按「位置倍数 ×（有前注 ×2）+ 溜入者」，再加注按 3-4 倍。

    翻前的底池只有盲注那么大，用「底池的 1/3」表达尺度没有意义 ——
    preflop-open.md 也明确要求「一律用大盲倍数说，不要说加注到几倍底池」。
    所以翻前换成 bb 口径的选项，翻后才用底池倍数。
    """
    to_call = max(snap.to_call or 0.0, 0.0)
    hero_bet = snap.hero_bet or 0.0
    stack = snap.hero_stack
    bb = snap.blinds.bb if snap.blinds else 1.0
    max_bet = hero_bet + to_call
    second = snap.second_bet
    increment = max(max_bet - second if second is not None else max_bet, bb)
    min_raise_to = max_bet + increment
    hero_max = (hero_bet + stack) if stack is not None else None

    # 判断「前面有没有人加注」用形态，不用 to_call：小盲面对大盲时 to_call=0.5，
    # 那是补齐盲注，不是面对加注
    facing_raise = any(s in ("single-raise", "three-bet")
                       for s in snap.preflop_shapes)

    options: list[Option] = []
    if facing_raise and to_call > 0:
        options.append(Option("call", "跟注", "call", _round_bb(to_call)))
        options.append(Option("fold", "弃牌", "fold"))
        # 面对加注：3bet/再加注到「上家加注的 3 倍 / 4 倍」，来自
        # preflop-3bet-pot.md「有位置 3-4 倍，盲注位一律 4 倍」
        for mult in (3, 4):
            amount, note = _cap(max_bet * mult, hero_max, min_raise_to)
            options.append(Option(
                f"raise-{mult}x", f"加注到上家加注的 {mult} 倍", "raise",
                amount, note))
        return options

    # 没人加注：平跟（limp）/ 补齐盲注 / 大盲过牌，然后按位置倍数开池
    hero_pos = snap.hero_position or ""
    if to_call > 0:
        options.append(Option("call", "跟注（补齐）", "call", _round_bb(to_call)))
    elif hero_pos == "BB":
        options.append(Option("call", "过牌", "check", None, "大盲免费看翻牌"))
    else:
        options.append(Option("call", "平跟进池（limp）", "call", None,
                              "第一个入池通常该加注"))
    if to_call > 0:
        options.append(Option("fold", "弃牌", "fold"))

    limpers = sum(1 for a in snap.all_actions()
                  if a.street == PREFLOP and a.action == "call" and not a.is_hero)
    base = _OPEN_BASES.get(hero_pos, (3.0, 3.25, 3.5))
    ante_x = 2.0 if (snap.blinds and snap.blinds.ante) else 1.0
    for b in base:
        target, note = _cap(b * ante_x + limpers, hero_max, min_raise_to)
        options.append(Option(f"open-{b:g}", f"加注到 {target:g}BB（开池）",
                              "raise", target, note))
    return options


def build_options(snap: HandSnapshot) -> list[Option]:
    """按现场底池与下注算出全部可执行选项。

    `to_call == 0` 是「轮到我、没人下注」：没有可跟的注，选项是过牌 + 各种下注。
    `to_call > 0` 是面对下注/加注：跟注 + 弃牌 + 各种加注。

    金额口径（与求解器/牌桌语言的通行口径一致）：

      · 面对下注：跟注后底池 = 底池 + 需跟额；「加注到 f 倍池」= 加注额再取它的 f，
        即 `加注后的本街总额 = 当前最高下注 + f × (底池 + 需跟额)`。
      · 没人下注：「下注 f 倍池」= `底池 × f`。

    金额全部由本模块算好，模型只选档位。两处**必须**的修正：
    低于最小加注额的档位要抬到合法值（否则给出一个牌桌上做不出来的动作），
    超过后手的要封顶并注明等同全下。
    """
    if snap.street == PREFLOP:
        return build_preflop_options(snap)

    to_call = max(snap.to_call or 0.0, 0.0)
    pot = snap.pot or 0.0
    hero_bet = snap.hero_bet or 0.0
    stack = snap.hero_stack
    bb = snap.blinds.bb if snap.blinds else 1.0

    options: list[Option] = []
    if to_call <= 0:
        options.append(Option("call", "过牌", "check",
                              note="没人下注，过牌"))
    else:
        options.append(Option("call", "跟注", "call", _round_bb(to_call)))
        options.append(Option("fold", "弃牌", "fold"))

    # 当前最高下注 = 我已投 + 需跟额
    max_bet = hero_bet + to_call
    # 最小加注额：上一次加注的增量；没有上一次（第一条下注）就是下注本身，
    # 但至少一个大盲（翻前盲注是强制下注，不能被当成 0.5BB 的增量）
    second = snap.second_bet
    increment = max(max_bet - second if second is not None else max_bet, bb)
    min_raise_to = max_bet + increment
    hero_max = (hero_bet + stack) if stack is not None else None

    for name in ("1/3", "1/2", "2/3", "1", "1.2"):
        if to_call > 0:
            target = max_bet + RAISE_FRACTIONS[name] * (pot + to_call)
        else:
            target = RAISE_FRACTIONS[name] * pot
        amount, note = _cap(target, hero_max, min_raise_to)
        options.append(Option(f"raise-{name}", OPTION_LABELS[f"raise-{name}"],
                              "raise", amount, note))

    by_id = {o.id: o for o in options}
    return [by_id[i] for i in OPTION_ORDER if i in by_id]


# ─────────────────────────── 牌况 ───────────────────────────

@dataclass(frozen=True)
class Situation:
    """一次决策的结构化事实。提示词由它渲染，规则兜底也用它。"""

    snap: HandSnapshot
    options: list[Option]
    hand_label: str | None
    hand_tier: str | None
    category: int | None
    texture: str | None
    has_draw: bool
    plays_board: bool
    opponent_strength: dict | None
    warnings: list[str] = field(default_factory=list)

    @property
    def street(self) -> str:
        return self.snap.street


def _read(snap: HandSnapshot):
    """翻后读牌：返回 coach.Read 或 None（翻前 / 牌不全 / 读不出）。"""
    if snap.street == PREFLOP:
        return None
    if not snap.hero_cards or len(snap.board) < 3:
        return None
    try:
        return read_hand(parse_cards("".join(snap.hero_cards)),
                         parse_cards("".join(snap.board[:5])))
    except ValueError:
        return None


def build_situation(snap: HandSnapshot) -> Situation:
    read = _read(snap)
    warnings: list[str] = list(snap.warnings)

    texture = None
    if read is not None:
        try:
            texture, _tags = board_texture(parse_cards("".join(snap.board[:5])))
        except ValueError:
            texture = None

    opp = None
    if snap.street != PREFLOP:
        opp = opponent_strength(_coach_actions(snap), snap.street, HERO_ACTOR,
                                snap.pot or 0.0).to_dict()

    return Situation(
        snap=snap,
        options=build_options(snap),
        hand_label=read.label if read else None,
        hand_tier=read.tier if read else None,
        category=read.category if read else None,
        texture=texture,
        has_draw=bool(read and (read.outs >= 4)),
        plays_board=bool(read and read.plays_board),
        opponent_strength=opp,
        warnings=warnings,
    )


def _coach_actions(snap: HandSnapshot) -> list[dict]:
    """把快照的行动线转成 coach / call-back 的 action 字典。

    amount 口径与 coach.advise 一致：bet/raise 是「加到总额」，跟注不带金额。
    """
    out: list[dict] = []
    for rec in snap.all_actions():
        out.append({
            "actor": HERO_ACTOR if rec.is_hero else rec.seat_id,
            "action": rec.action,
            "amount": rec.amount,
            "street": rec.street,
        })
    return out


# ─────────────────────────── 提示词 ───────────────────────────

_STREET_ZH = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}


def _fmt_actions(actions: list[ActionRecord], hero_name: str = "Hero") -> str:
    if not actions:
        return "（无）"
    parts = []
    for a in actions:
        who = hero_name if a.is_hero else (a.position or a.name or a.seat_id)
        extra = "（全下）" if a.all_in else ""
        parts.append(f"{who} {a.label()}{extra}")
    return " → ".join(parts)


def _position_zh(pos: str | None) -> str:
    return pos or "未知"


def render_hand_facts(sit: Situation) -> str:
    """把当前局面写成紧凑的手牌历史式文本 —— 模型对这套写法先验最强。"""
    snap = sit.snap
    blinds = snap.blinds
    lines: list[str] = []

    table = f"{snap.table_size}人桌" if snap.table_size else "桌型未知"
    lines.append(f"桌型: {table}")
    if blinds:
        ante = f"（前注 {blinds.ante}）" if blinds.ante else ""
        lines.append(f"盲注: {blinds.sb}/{blinds.bb}{ante}")
    else:
        lines.append("盲注: 未知")
    opps = [o for o in snap.opponents if not o.folded]
    folded = [o for o in snap.opponents if o.folded]
    stacks = [o.stack for o in opps if o.stack is not None]
    effective = min([*stacks, snap.hero_stack]) if snap.hero_stack is not None \
        and stacks else snap.hero_stack

    lines.append(f"Hero 位置: {_position_zh(snap.hero_position)}｜"
                 f"手牌: {''.join(snap.hero_cards) or '未知'}｜"
                 f"后手: {snap.hero_stack if snap.hero_stack is not None else '未知'}BB")
    if effective is not None:
        lines.append(f"有效筹码（含 hero 后手）: {round(effective, 2)}BB")
    if opps:
        lines.append("在场对手: " + "、".join(
            f"{o.position or o.seat_id}"
            f"{'(' + o.name + ')' if o.name else ''} "
            f"{o.stack if o.stack is not None else '?'}BB"
            for o in opps))
    if folded:
        lines.append("已弃牌: " + "、".join(
            f"{o.position or o.seat_id}" for o in folded))

    lines.append("")
    to_call = round(snap.to_call, 2)
    lines.append(f"当前街: {_STREET_ZH.get(snap.street, snap.street)}｜"
                 f"公共牌: {''.join(snap.board) or '无'}｜"
                 f"底池: {snap.pot if snap.pot is not None else '未知'}BB｜"
                 f"本街需再跟: {to_call}BB")
    if to_call > 0 and snap.pot:
        # 赔率门槛：跟注额 ÷ 跟注后的底池。技能里的赔率表就是按这个口径
        bep = to_call / (snap.pot + to_call) * 100
        lines.append(f"跟注需要胜率（BEP）: {bep:.0f}%；"
                     f"需跟额占底池 {to_call / snap.pot:.0%}")

    lines.append("")
    lines.append("## 行动线")
    for rec in snap.streets:
        pot_start = f"（街前底池 {rec.pot_start}BB）" if rec.pot_start is not None else ""
        lines.append(f"{_STREET_ZH.get(rec.street, rec.street)}{pot_start}: "
                     f"{_fmt_actions(list(rec.actions))}")

    lines.append("")
    lines.append("## 程序算出的事实（直接采信，不要重算）")
    if sit.street == PREFLOP:
        shape = {"unopened": "前面没人加注", "single-raise": "前面已有一次加注",
                 "three-bet": "前面已经出现过再加注"}
        shapes = "、".join(shape.get(s, s) for s in snap.preflop_shapes) or "未知"
        lines.append(f"翻前局面: {shapes}｜我是翻前加注者: "
                     f"{'是' if snap.hero_is_pfr else '否'}")
    else:
        lines.append(f"我的牌力: {sit.hand_label or '读不出'}"
                     f"（分档: {sit.hand_tier or '未知'}）")
        lines.append(f"牌面质地: {sit.texture or '未知'}｜"
                     f"听牌: {'有' if sit.has_draw else '无'}｜"
                     f"打牌面（最好五张全来自公共牌）: {'是' if sit.plays_board else '否'}")
        if sit.opponent_strength:
            o = sit.opponent_strength
            lines.append(f"从对手动作读出的牌力: {o['label']}（{o['reason']}）")
    if snap.hero_invested is not None and snap.hero_stack:
        ratio = snap.hero_invested / max(snap.hero_stack + snap.hero_invested, 1e-6)
        lines.append(f"我已投入 {round(snap.hero_invested, 2)}BB"
                     f"（约占当前筹码 {ratio:.0%}）")

    if sit.warnings:
        # 缺牌/缺筹码这类缺口要让模型知道，它才会在理由里说明而不是硬猜
        lines.append("")
        lines.append("数据缺口: " + "；".join(sit.warnings))

    lines.append("")
    lines.append("## 可选选项（只能选一个 id）")
    for o in sit.options:
        amount = f"，金额 {o.amount}BB" if o.amount is not None else ""
        note = f"（{o.note}）" if o.note else ""
        lines.append(f"- {o.id} — {o.label}{amount}{note}")
    return "\n".join(lines)


def build_messages(sit: Situation) -> tuple[str, str]:
    """返回 (system, user)。system 由「角色 + 输出契约 + 技能层」拼成。"""
    all_ = skills.all_skills()
    selected = skills.select_skills(_skill_context(sit), all_)

    system_parts = [
        "你是一位德州扑克现金局教练，正在**实时**辅助一位玩家做决定。"
        "玩家打完一手牌后会复盘，但此刻他只有几秒钟，需要的是一个能直接执行的动作。",
        "",
        "## 输出格式",
        "只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释性文字：",
        '{"option": "选项 id", "reason": "一句话理由，30 字以内，必须引用技能里的规则"}',
        "",
        "## 硬性约束",
        "1. option 必须严格取自用户消息里「可选选项」的 id，不许多写、不许改写。",
        "2. 金额一律由程序算好，你**不要**自己算底池、不要输出金额，只输出 id。",
        "3. 面对下注时要真的按赔率比较：跟注需要的胜率来自「赔率与概率速查」，"
        "拿它和你的牌力/听牌成牌率比，不够就弃，够就跟或加。",
        "4. 翻前没有成牌率可比：按位置、前面加注次数、对手人数，用「翻前」那几篇的尺度表。",
        "5. 说不出下注/加注理由时选过牌；说不出过牌理由时选下注"
        "（「说不出理由的过牌同样是漏损」）。",
        "6. 对手形象一律按「未知」处理 —— 实时链路没有历史样本，不做剥削性偏离。",
        "7. 不要臆测对手的具体手牌，只描述范围倾向。",
        "8. 理由用中文，牌面记号保持 AsKh 格式。",
        "",
    ]
    system_parts.append(skills.render_catalog(all_, selected))
    system_parts.append(skills.render_block(selected))

    return "\n".join(system_parts), render_hand_facts(sit)


def _skill_context(sit: Situation) -> skills.SkillContext:
    snap = sit.snap
    known = sum(1 for o in snap.opponents if o.name or o.position)
    return skills.SkillContext(
        street=snap.street,
        board="".join(snap.board),
        hero_cards="".join(snap.hero_cards),
        hero_position=snap.hero_position,
        table_size=snap.table_size,
        opponents=snap.opponents_in_hand,
        preflop_shapes=snap.preflop_shapes,
        hero_is_pfr=snap.hero_is_pfr,
        cbet_spot=snap.cbet_spot,
        villains_known=known,
        category=sit.category,
        tier=sit.hand_tier,
        has_draw=sit.has_draw,
        plays_board=sit.plays_board,
    )


# ─────────────────────────── 模型调用 ───────────────────────────

@dataclass(frozen=True)
class AISettings:
    api_key: str
    base_url: str
    model: str
    source: str = "env"              # env | dotenv，只用于启动日志

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None,
                 source: str = "env") -> "AISettings | None":
        """从环境变量读凭据。缺任何一项都返回 None（= 没配置，调用方应跳过）。

        与 call-back 的 AI_SERVER_* 同口径：同一台机器上两边共用一套配置。
        `.env` 由 poker_ocr.env 先加载进环境变量，这里不关心值从哪来。
        """
        env = os.environ if env is None else env
        key = (env.get("AI_SERVER_API_KEY") or "").strip()
        base = (env.get("AI_SERVER_BASE_URL") or "").strip()
        model = (env.get("AI_SERVER_MODEL") or "").strip()
        if not (key and base and model):
            return None
        return cls(api_key=key, base_url=base.rstrip("/"), model=model,
                   source=source)


class AIError(RuntimeError):
    """模型调用失败。消息面向用户，不含供应商原文（可能回显 Key 片段）。"""


def complete_json(settings: AISettings, system: str, user: str,
                  timeout: float = 20.0) -> dict:
    """调用 OpenAI 兼容接口并解析 JSON。失败抛 AIError。

    刻意不引 SDK、不走流式：一次只回几十个 token，urllib 足够，
    也避免给实时链路增加依赖。`response_format=json_object` 能显著降低
    「返回一段带 markdown 围栏的散文」的概率。
    """
    def request(json_mode: bool) -> bytes:
        payload = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            settings.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + settings.api_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    try:
        try:
            raw = request(json_mode=True)
        except urllib.error.HTTPError as exc:
            # 有些兼容层不认 response_format（回 400）。它只是个增强，
            # 退一步重来，而不是把整次建议判失败
            if exc.code != 400:
                raise
            raw = request(json_mode=False)
    except urllib.error.HTTPError as exc:
        # 鉴权失败不回显 body：供应商常在里面回显 Key 片段
        if exc.code in (401, 403):
            raise AIError("模型鉴权失败，请检查 AI_SERVER_API_KEY") from exc
        raise AIError(f"模型服务返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIError(f"调用模型失败：{exc}") from exc

    try:
        body = json.loads(raw)
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIError("模型响应结构不对") from exc

    return parse_json_content(content)


def parse_json_content(content: str) -> dict:
    """解析模型正文。允许被 ```json 围栏包住（有些供应商不认 response_format）。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        data = json.loads(text)
    except ValueError:
        # 最后兜一道：供应商偶尔在 JSON 前后加一句废话。只取第一个 { 到最后一个 }
        lo, hi = text.find("{"), text.rfind("}")
        if lo < 0 or hi <= lo:
            raise AIError("模型返回的不是合法 JSON")
        try:
            data = json.loads(text[lo:hi + 1])
        except ValueError as exc:
            raise AIError("模型返回的不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise AIError("模型返回的 JSON 不是对象")
    return data


# ─────────────────────────── 兜底 ───────────────────────────

def fallback_option(sit: Situation) -> str:
    """模型不可用时的确定性兜底。

    刻意**只做最保守的选择**，不试图复刻技能库的完整规则（那是 coach.py 的事）：
    面对下注且读不出牌力 → 弃牌；否则 → 过牌。宁可给一个保守答案，
    也不给一个看着像样却是错的建议。
    """
    if sit.snap.to_call > 0:
        return "fold"
    return "call"           # to_call == 0 时 call 这个 id 就是「过牌」


# ─────────────────────────── 结果 ───────────────────────────

@dataclass(frozen=True)
class Advice:
    hand_id: str
    street: str
    option: Option
    reason: str
    source: str                      # model | fallback
    model: str | None = None
    skill_codes: tuple[str, ...] = ()
    duration_ms: int = 0
    error: str | None = None
    pot: float | None = None
    to_call: float = 0.0
    hero_cards: tuple[str, ...] = ()
    board: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "hand_id": self.hand_id,
            "street": self.street,
            "hero_cards": list(self.hero_cards),
            "board": list(self.board),
            "pot": self.pot,
            "to_call": round(self.to_call, 2),
            "option": self.option.to_dict(),
            "reason": self.reason,
            "source": self.source,
            "model": self.model,
            "skill_codes": list(self.skill_codes),
            "duration_ms": self.duration_ms,
            **({"error": self.error} if self.error else {}),
        }


def advise(sit: Situation, settings: AISettings | None,
           timeout: float = 20.0, complete=None) -> Advice:
    """出一次建议。`complete` 可注入（单测用），默认走 complete_json。"""
    import time

    started = time.monotonic()
    by_id = {o.id: o for o in sit.options}
    selected = skills.select_skills(_skill_context(sit))

    def make(option: Option, reason: str, source: str,
             error: str | None = None) -> Advice:
        snap = sit.snap
        return Advice(snap.hand_id, snap.street, option, reason, source,
                      settings.model if settings else None,
                      tuple(s.code for s in selected),
                      int((time.monotonic() - started) * 1000),
                      error, snap.pot, snap.to_call,
                      snap.hero_cards, snap.board)

    if settings is None:
        return make(by_id[fallback_option(sit)],
                    "未配置 AI_SERVER_*，按最保守的默认动作",
                    "fallback", "未配置模型凭据")

    system, user = build_messages(sit)
    caller = complete or complete_json
    try:
        data = caller(settings, system, user, timeout=timeout)
        choice = str(data.get("option", "")).strip()
        if choice not in by_id:
            raise AIError(f"模型给了不在选项里的答案：{choice!r}")
        reason = str(data.get("reason", "")).strip() or "（模型未给理由）"
        return make(by_id[choice], reason, "model")
    except AIError as exc:
        return make(by_id[fallback_option(sit)],
                    "模型不可用，按最保守的默认动作",
                    "fallback", str(exc))


# ─────────────────────────── 后台执行 ───────────────────────────

class AdviceRunner:
    """把模型调用放到后台线程，**绝不阻塞 OCR 主循环**。

    实时链路一帧约 0.5s，模型一次要几秒；同步调用会让采样停顿、画面延迟越积越大。
    这里只保留「最新一个待办」：新的决策点覆盖旧的，旧结果仍然返回并落库
    （它可能已经过时，但复盘时有用）。
    """

    def __init__(self, settings: AISettings | None, *, timeout: float = 20.0,
                 complete=None, log=None) -> None:
        import queue
        import threading

        self.settings = settings
        self.timeout = timeout
        self._complete = complete
        self._log = log or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._done: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._dropped = 0
        self._thread = threading.Thread(target=self._loop, name="advice",
                                        daemon=True)
        self._thread.start()

    def submit(self, sit: Situation) -> bool:
        """投递一个决策点。队列里已有待办时**用新的覆盖旧的**（最新局面更重要）。"""
        import queue

        try:
            self._queue.put_nowait(sit)
            return True
        except queue.Full:
            # 丢掉还没开始的旧决策点，换成最新的。取走与放回之间 worker
            # 可能刚好取走队列，所以放回也要允许失败
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(sit)
                self._dropped += 1
                return True
            except queue.Full:
                return False

    def poll(self) -> list[Advice]:
        """主循环里调用：取走已完成的结果。"""
        import queue

        out: list[Advice] = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out

    def _loop(self) -> None:
        import queue

        while not self._stop.is_set():
            try:
                sit = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if sit is None:
                return
            try:
                advice = advise(sit, self.settings, self.timeout, self._complete)
            except Exception as exc:      # noqa: BLE001 — 后台线程不能死
                self._log(f"[建议] 生成失败：{type(exc).__name__}: {exc}")
                continue
            self._done.put(advice)

    def close(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except Exception:                 # noqa: BLE001 — 队列满就直接退出
            pass
        self._thread.join(timeout=2.0)

    @property
    def dropped(self) -> int:
        return self._dropped
