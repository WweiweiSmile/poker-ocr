"""一帧的完整桌面状态（M2.4）：把 ROI 原文 + 解析结果拼成可用的状态。

输出面向「抽帧流 → 状态机」：每帧给出所有玩家的 {名字, 位置, 后手筹码, 本街下注,
动作, 是否在池, 是否全下}，以及全局 {盲注, 底池, 本街下注总额, 庄位, 轮到我, 几人桌}。

**这一层不决定「记不记录」**，只决定「这帧能不能用」：
`errors` 非空就表示有字段无法精准识别（遮挡、动画、误读），调用方应当整帧丢掉
（用户的要求：遇到干扰的画面无法精准识别，直接放弃当前帧）。

几条刻意的口径：

  · **后手筹码**是筹码胶囊上的值（玩家下注后会减少），不是「带入」。
  · **本街下注**取每个座位的下注 pill；没有 pill = 0。注意它和
    `pot − pot_before_street` 是同一个量，后者用于交叉校验。
    **下注读到大于底池、或大于本街总额 = 不可能**（底池就是全场投入之和）：
    这一位降级成 `bet=None` / `bet_unknown=True` 并记进 `bet_warnings`，
    **不丢整帧** —— 单座一个坏字段不该把别人的筹码/底池/动作一起作废
    （见下面的质量闸）。降级时 `to_call` 依赖的最大下注不可信，改由调用方
    （`live.py`）跳过这一手的建议/点击，而不是让整帧消失。
  · **金额只做两侧清洗，不猜数字**：`5.5BB` 被读成 `55BB` 这种「小数点丢了」
    的量级错误**不修**（不猜小数点、不猜倍率，见 `parse.parse_amount`），
    交给上面那条几何闸降级成未知。清洗只裁掉贴在金额两侧的图标噪声
    （`1BB8` → `1`），不改动数字本身。
  · **盲注**解析失败时可以继承上一帧（`blinds.source = "carried"`）—— 盲注只在
    升级时变，而那条横幅经常被「All-in 全场禁言中…」盖住；继承比丢帧合理。
    但**底池不能继承**（每手/每街都在变）。
  · **弃牌**徽章画在头像上，实测 rec 会系统性读成形近字（弃牌→奔牌），
    所以 `fold` 是 best-effort 三态，允许 None（没读到 ≠ 没弃牌）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from .parse import (
    Amount, Blinds, NameTracker, is_all_in_badge, is_fold_badge,
    looks_like_badge, names_similar,
    parse_action, parse_amount, parse_blinds, parse_chip_loose, parse_equity,
    parse_percent, sticky_name, waiting_badge,
)
from .positions import assign_positions
from .profile import Profile
from .roi_reader import RoiFrame

# 状态里的动作枚举（与 call-back / api.py 的口径一致）
ACTIONS = ("check", "call", "bet", "raise", "fold")

# 底池横幅的「没有底池」文案。客户端的底池槽在开手前/收池动画里显示的是
# 「总底池」三个字（不是数字），这时的 pot 为 None 是**画面结构**，不是误读。
# 2026-11-11 实盘占比最高的一类丢弃（942/1508 帧）全是这个。
_POT_IDLE_RE = re.compile(r"总?底池")


@dataclass
class PlayerState:
    seat_id: str
    name: str | None = None
    position: str | None = None
    stack: float | None = None       # 后手筹码（BB）
    bet: float | None = None         # 本街已下注（BB）；None = 读到了但没读懂
    # `bet` 读到的是**不可能**的值（本街下注 > 底池），已按未知处理。
    # 和「没读懂」不同：这是明确知道这个读数不能用，不是没读到。
    # 见 build_state 的质量闸与 hand_track 的基线/水位处理。
    bet_unknown: bool = False
    action: str | None = None        # check/call/bet/raise/fold（名字槽徽章）
    fold: bool | None = None         # 头像徽章 best-effort
    in_hand: bool = False
    all_in: bool = False
    # 全下后筹码位置可能显示实时胜率（如 `30%`）。不是百分数时为 None。
    equity: float | None = None
    status: str = "away"             # playing / folded / buying_in / away
    # 本手中途离桌。离桌必然已经弃牌，所以本手剩余时间仍保留在 players 里：
    # 名字/位置沿用离桌前的，fold=True、status=folded，但不参与行动线。
    # 下一手重新识别这个座位（可能已经换人）。
    left: bool = False
    notes: list[str] = dc_field(default_factory=list)
    # ── 名字诊断（落库用，不影响判定）──
    # 本帧名字槽的**原始读数**和它的 rec 分数。用户反馈里最常见的追问就是
    # 「这个字到底是读错了还是没读到」，没有这两个字段就完全无法复盘
    # （2026-11-01 那次原始分数已经永久丢失）。分数是 rec 自评，别拿它当「对
    # 不对」的概率用 —— 见 name_lexicon 的模块注释。
    name_raw: str | None = None
    name_score: float = 0.0
    # tracker 给出的名字（可能被词表纠正过）。`name` 可能被 roster 兜底或
    # 离桌逻辑覆盖，这个字段始终保留 tracker 的当前结论，供 StateStream 比对
    # 后修复 roster。
    name_tracked: str | None = None
    # ── 弃牌诊断（落库用，不影响判定）──
    # `fold` 是「图标 + 文案」合并后的结论，事后看不出是哪条判据错了
    # （2026-11-13 #20：126 条亮牌落在 inferred fold 的座位上）。
    # 这里保留**原始读数**，由 hand_track 在补 fold 时快照进手牌记录。
    fold_icon: bool | None = None    # 头像底角手牌图标：True=有/False=没有/None=没读到
    fold_text: str | None = None     # 头像中间文案的原始读数（「弃牌」/「等待中」…）
    fold_text_score: float | None = None

    @property
    def seat_state(self) -> str:
        """三态玩家状态：``in_hand`` / ``waiting`` / ``folded``；``empty`` = 空座。

        业务语义的唯一口径（`status` 继续保留给 JSONL/旧消费者）：

          · **在局**是默认值：只要这一手有他（名单锁定的座位），没有明确的
            弃牌/等待证据就一直在局；
          · **等待入局**只由头像文案（等待审核 / 等待中 / 正在带入）触发；
          · **弃牌**只由「头像出现弃牌 + 手牌图标消失」两条同时满足触发；
          · **空座**不是玩家状态（虚线「+」占位 / 牌桌没坐满）。
        """
        if self.status == "away" and not self.in_hand:
            return "empty"
        if self.status in ("buying_in", "sitting_out"):
            return "waiting"
        if self.fold is True or self.status == "folded":
            return "folded"
        return "in_hand"

    def to_debug(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "position": self.position,
            "name": self.name,
            "stack": self.stack,
            "bet": self.bet,
            **({"bet_unknown": True} if self.bet_unknown else {}),
            "action": self.action,
            "fold": self.fold,
            "in_hand": self.in_hand,
            "all_in": self.all_in,
            **({"equity": self.equity} if self.equity is not None else {}),
            "status": self.status,
            # 只在真有名字相关读数时写：老记录的键集合保持原样
            **({"name_score": round(self.name_score, 4)}
               if self.name_score else {}),
            **({"name_raw": self.name_raw}
               if self.name_raw and self.name_raw != self.name else {}),
            **({"name_tracked": self.name_tracked}
               if self.name_tracked and self.name_tracked != self.name else {}),
            **({"left": True} if self.left else {}),
            **({"notes": self.notes} if self.notes else {}),
        }


@dataclass
class FrameState:
    frame_id: str
    table_seats: int
    players_in_hand: int
    players: list[PlayerState]
    blinds: Blinds | None = None
    blinds_source: str = "frame"     # frame / carried
    dealer: str | None = None
    hero_seat_id: str = ""
    hero_turn: bool | None = None
    pot: float | None = None
    pot_before_street: float | None = None
    # 底池横幅这一帧显示的是「总底池」标签（没有活的底池）= 手牌间隙帧。
    # 上层据此把它算成「手牌间隙」而不是「识别失败」，并可当换手信号用。
    pot_idle: bool = False
    street_bet_total: float | None = None
    # 屏幕中央的爆炸底池横幅（「幸运暴击 / BOMB POT」）。这是**本帧**是否
    # 看到横幅；跨帧保持由 StateStream / LiveHandTracker 负责。
    bomb_pot: bool = False
    errors: list[str] = dc_field(default_factory=list)
    # 名字相关的诊断（改口/抖动/词表存疑）。**不进 errors**：名字读不准不该
    # 让整帧被质量闸丢掉，那会把牌面和底池一起扔了。
    name_warnings: list[str] = dc_field(default_factory=list)
    # 本帧「本街下注读到不可能的值（> 底池）」的座位。**不进 errors**：那个
    # 座位这一帧的下注按未知处理（`bet=None` + `bet_unknown=True`），帧里
    # 其余字段（别人的筹码/底池/动作）仍然可用。调用方据此**不给 AI 建议**：
    # to_call/水位依赖最大下注，静默少算一位会点错金额。
    bet_warnings: list[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def player(self, seat_id: str) -> PlayerState:
        for p in self.players:
            if p.seat_id == seat_id:
                return p
        raise KeyError(seat_id)

    def to_debug(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "table_seats": self.table_seats,
            "players_in_hand": self.players_in_hand,
            "blinds": ({**self.blinds.to_debug(), "source": self.blinds_source}
                       if self.blinds else None),
            "dealer": self.dealer,
            "hero_seat_id": self.hero_seat_id,
            "hero_turn": self.hero_turn,
            "pot": self.pot,
            "pot_before_street": self.pot_before_street,
            "street_bet_total": self.street_bet_total,
            **({"bomb_pot": True} if self.bomb_pot else {}),
            "players": [p.to_debug() for p in self.players],
            **({"errors": self.errors} if self.errors else {}),
            **({"name_warnings": self.name_warnings}
               if self.name_warnings else {}),
            **({"bet_warnings": self.bet_warnings}
               if self.bet_warnings else {}),
        }


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


# 2026-11-13 起不再有「小数点量级修复」：曾经想按几何上限把 `55` 反推回
# `5.5`，但那是在猜数字——猜对了多 1 手，猜错了就是一个 10 倍的 to_call。
# 现在金额只做两侧噪声清洗（见 `_parse_amount_slot` 与 `parse.parse_amount`），
# 量级不可能时按未知降级，交给调用方跳过这一手建议。


def _parse_amount_slot(frame: RoiFrame, key: str,
                       seat_id: str) -> tuple[Amount, str | None]:
    """先按整块文本解析金额；失败再**逐框**试一次。

    下注 pill 两侧的筹码图标常被 rec 拆成单独一框，`text()` 会把它拼进金额
    （`1BB` + `8` → `1BB8`），整块解析失败就把整帧丢掉。逐框重试能把金额
    单独取出来；返回 `(amount, 命中的那一框原文)`，命中的框写进 notes 便于复盘。
    """
    raw = frame.text(key, seat_id)
    amount = parse_amount(raw)
    if amount.ok:
        return amount, None
    for text, _score in frame.parts(key, seat_id):
        if not text or text == raw:
            continue
        cand = parse_amount(text)
        # 必须带单位：单独一框的 `0`/`8` 是筹码图标，不是金额。
        if cand.ok and cand.unit:
            return Amount(raw, cand.value, cand.unit,
                          tail_noise=cand.tail_noise), text
    return amount, None


# ── 爆炸底池横幅 ──────────────────────────────────────────────
# WPK 的爆炸底池在屏幕中央弹「幸运暴击 / BOMB POT」：上面四个黄色大字，
# 下面一行英文。两块都是紧条 ROI，纯 rec 就能读干净；不用 det 是因为
# 整块中心区域里还有座位名、底池数字，det 会把它们一起框进来。
#
# `BOMB POT` 比中文稳：实测 11 帧里英文稳定命中 7 帧（分数 0.86–0.95），
# 中文 4 字有 1 帧读成「幸起暴击」。所以英文为主，中文做兜底（4 字里
# 至少命中 3 个不同字，才不会被动画里的「暴击」二字误触发）。
BOMB_EN_TEXT = "BOMBPOT"
BOMB_CN_TEXT = "幸运暴击"
BOMB_EN_MIN_SCORE = 0.6
BOMB_CN_MIN_SCORE = 0.7
BOMB_CN_MIN_CHARS = 3


def _bomb_compact(text: str) -> str:
    """只留字母/数字/汉字，去掉 OCR 爱插的空格和标点。"""
    return "".join(ch for ch in text.upper() if ch.isalnum())


def is_bomb_banner(cn_text: str, cn_score: float,
                   en_text: str, en_score: float) -> bool:
    """两块横幅文本里是否有一块能确认是爆炸底池。

    英文命中要求包含 `BOMBPOT`（去掉空格后）且分数达标；中文命中要求
    「幸运暴击」四个字里至少出现 3 个不同字。任一命中即可 —— 横幅动画
    里两块不一定同时读得清。
    """
    if (en_score >= BOMB_EN_MIN_SCORE
            and BOMB_EN_TEXT in _bomb_compact(en_text)):
        return True
    if cn_score < BOMB_CN_MIN_SCORE:
        return False
    hits = {ch for ch in _bomb_compact(cn_text) if ch in BOMB_CN_TEXT}
    return len(hits) >= BOMB_CN_MIN_CHARS


def _bomb_banner(frame: RoiFrame) -> bool:
    cn = frame.globals.get("bomb_cn")
    en = frame.globals.get("bomb_en")
    return is_bomb_banner(
        cn.text if cn is not None else "", cn.score if cn is not None else 0.0,
        en.text if en is not None else "", en.score if en is not None else 0.0)


def build_state(frame: RoiFrame, profile: Profile,
                prev_names: dict[str, str] | None = None,
                prev_blinds: Blinds | None = None,
                avatar_reads: dict[str, tuple[str, float]] | None = None,
                equity_reads: dict[str, tuple[str, float]] | None = None,
                name_tracker: "NameTracker | None" = None,
                prev_all_in: set[str] | None = None,
                lenient_chips: bool = False,
                card_icons: dict[str, bool | None] | None = None,
                shown_seats: set[str] | None = None,
                prev_fold: dict[str, bool | None] | None = None,
                ante_fallback: float | None = None,
                hand_roster: dict[str, tuple[str | None, str | None]] | None = None,
                left_seats: set[str] | None = None,
                prev_state: "FrameState | None" = None) -> FrameState:
    """一帧 RoiFrame → FrameState（含解析错误清单）。

    `prev_names` 是**上一份被接受的状态**里的名字（名称粘性用）；
    `prev_blinds` 用于盲注横幅被盖住时继承；`prev_state` 是完整的上一份
    被接受状态，只用于**一帧级的兜底**：底池没变时沿用上一帧的
    `pot_before_street` / 某个座位读不出的下注额（见质量闸前的说明）。
    `avatar_reads` 是头像 ROI 的
    `{seat_id: (text, score)}`（弃牌 / All-in 徽章，可选）；`equity_reads`
    是筹码胶囊下方胜率槽的 `{seat_id: (text, score)}`（全下摊牌时可选）；
    `card_icons` 是头像底角「POKER」手牌图标的三态检测结果
    （True=还在池里 / False=没图标 / None=判断不了，见 `card_icon.py`）。
    图标比「弃牌」二字的 OCR 可靠得多，能覆盖 rec 把弃牌读成别的字的帧。
    `shown_seats` 是这一帧亮牌的座位（摊牌时头像被牌顶掉，没有图标）。
    `prev_fold` 是上一份被接受状态里各座位的弃牌判定：亮牌的人弃没弃，
    只能沿用之前动作得出的结论（亮牌本身不代表没弃牌）。

    `hand_roster` / `left_seats` 是**一手牌内的座位身份锁**（实时链路用）：
    `hand_roster` = {seat_id: (名字, 位置)}，在这一手开始时由 StateStream 锁定；
    `left_seats` 是其中已经确认中途离桌的座位。锁定后：

      · 离桌座位仍然保留在 players 里，沿用锁定时名字/位置，`left=True`、
        `fold=True`、`status=folded`，本手剩余时间按弃牌处理；
      · 本手开始后才坐下的人（不在 roster 里）本手不参与、不算人数，
        名字也不在这手输出，等下一手重新识别；
      · 位置按**锁定的 roster** 而非当前在场人数排。否则一个非庄位玩家退出，
        他后面的人会整体往前挪一位（hero 的 UTG+2 会变成 LJ），行动顺序、
        画像和 AI 上下文全部错位；
      · 位置/名字被 ROI 缓存钉住时，名字槽的「文本非空」不再作为在场证据
        （空座会一直判在场并报 stack 错误）。老调用方不传这两个参数，行为不变。
    """
    return _build_state(BuildContext(
        frame=frame, profile=profile, prev_names=prev_names,
        prev_blinds=prev_blinds, avatar_reads=avatar_reads,
        equity_reads=equity_reads, name_tracker=name_tracker,
        prev_all_in=prev_all_in, lenient_chips=lenient_chips,
        card_icons=card_icons, shown_seats=shown_seats,
        prev_fold=prev_fold, ante_fallback=ante_fallback,
        hand_roster=hand_roster, left_seats=left_seats,
        prev_state=prev_state))


@dataclass(frozen=True)
class BuildContext:
    """`build_state` 的输入快照；避免编排函数继续加位置参数。"""
    frame: RoiFrame
    profile: Profile
    prev_names: dict[str, str] | None
    prev_blinds: Blinds | None
    avatar_reads: dict[str, tuple[str, float]] | None
    equity_reads: dict[str, tuple[str, float]] | None
    name_tracker: NameTracker | None
    prev_all_in: set[str] | None
    lenient_chips: bool
    card_icons: dict[str, bool | None] | None
    shown_seats: set[str] | None
    prev_fold: dict[str, bool | None] | None
    ante_fallback: float | None
    hand_roster: dict[str, tuple[str | None, str | None]] | None
    left_seats: set[str] | None
    prev_state: FrameState | None

def _global_facts(ctx: BuildContext):
    """解析盲注/底池/庄位等全局字段。"""
    frame = ctx.frame
    prev_blinds = ctx.prev_blinds
    ante_fallback = ctx.ante_fallback
    # ── 全局 ──────────────────────────────────────────────
    blinds = parse_blinds(frame.text("blinds"))
    blinds_source = "frame"
    if blinds is None and prev_blinds is not None:
        blinds, blinds_source = prev_blinds, "carried"
    elif blinds is not None and blinds.ante is None and prev_blinds is not None:
        # 括号里的前注被读丢（`1/2）`）→ 沿用上一帧的前注。前注是牌桌定义，
        # 只在升级时变；让它跟着 OCR 在 0 / 0.5 之间跳会制造假状态变化。
        blinds = Blinds(sb=blinds.sb, bb=blinds.bb, ante=prev_blinds.ante,
                        raw=blinds.raw)
    if blinds is not None and blinds.ante is None and ante_fallback:
        # 横幅把括号里的前注读空（`1/2(）`）时，用 profile 的默认前注兜底。
        # 前注直接影响开池尺寸（有前注要放大）和底池计算，不能当 0。
        blinds = Blinds(sb=blinds.sb, bb=blinds.bb,
                        ante=float(ante_fallback), raw=blinds.raw)

    pot_a = parse_amount(frame.text("pot"))
    pbs_a = parse_amount(frame.text("pot_before_street"))
    pot, pbs = pot_a.value, pbs_a.value
    # 底池横幅在「没有活的底池」时显示的是**标签**「总底池」，不是数字
    # （2026-11-11 实盘：同一块 ROI 正常帧读 `6.5BB`、收池帧读 `总底池`，
    # 置信 0.98/0.95）。这是手牌间隙帧，不是读数坏了 —— 单独标出来，
    # 让上层把它算成「手牌间隙」而不是「识别失败」，并顺手当换手信号。
    pot_idle = _POT_IDLE_RE.search(frame.text("pot") or "") is not None
    street_total = (round(pot - pbs, 2) if pot is not None and pbs is not None
                    and pbs <= pot else None)

    dealer = frame.dealer.seat_id if (frame.dealer and frame.dealer.status == "found") else None
    return blinds, blinds_source, pot, pbs, pot_idle, street_total, dealer


def _build_players(ctx: BuildContext):
    """逐座位重建 PlayerState（名字/筹码/下注/弃牌/All-in/名单锁）。"""
    frame = ctx.frame
    prev_names = ctx.prev_names or {}
    avatar_reads = ctx.avatar_reads or {}
    equity_reads = ctx.equity_reads or {}
    prev_all_in = ctx.prev_all_in or set()
    shown_seats = ctx.shown_seats
    hand_roster = ctx.hand_roster
    left_seats = ctx.left_seats
    prev_fold = ctx.prev_fold
    name_tracker = ctx.name_tracker
    lenient_chips = ctx.lenient_chips
    card_icons = ctx.card_icons
    prev_names = prev_names or {}
    avatar_reads = avatar_reads or {}
    equity_reads = equity_reads or {}
    prev_all_in = prev_all_in or set()
    shown = set(shown_seats or ())
    # 一手牌内的座位身份锁（见 docstring）。空 dict = 未启用，走旧逻辑。
    roster = dict(hand_roster or {})
    left = set(left_seats or ())
    # 整帧都判断不了（4x4 单测桩、整桌摊牌亮牌）时当作「没有这项信息」；
    # 只要有一座位给出了明确结果（有/没有图标），就按座位各自判断。
    icons = dict(card_icons or {})
    if not any(v is not None for v in icons.values()):
        icons = {}
    # ── 每个座位 ──────────────────────────────────────────
    players: list[PlayerState] = []
    name_warnings: list[str] = []
    for seat_roi in frame.seats:
        sid = seat_roi.seat_id
        name_raw = frame.text("name", sid)
        name_read = seat_roi.fields.get("name")
        name_score = float(getattr(name_read, "score", 0.0) or 0.0)
        chip_raw = frame.text("chip", sid)
        bet_raw = frame.text("bet", sid)

        # 动作徽章会盖住名字：先认徽章，再决定这块文本是不是名字。
        # 读残（`下注法` / `廣加注` / `跟注海` / 裸 `下`）也要算徽章，否则
        # 残片会被当成名字写进稳定器，玩家名变成 `跟` / `加` / `下`（#23）。
        action = parse_action(name_raw)
        name_is_badge = looks_like_badge(name_raw)
        icon = icons.get(sid)
        shown_here = sid in shown
        avatar_text, avatar_score = avatar_reads.get(sid, ("", 0.0))
        # 头像上的「在座但没入局」文案：正在带入 / 等待审核 / 等待中。
        # 真机依据 `人在桌未入局.mp4`：hero 右手边第一位头像显示「等待中」、
        # 筹码 200BB 照常渲染 —— 所以这组文案本身就是判据，不看筹码。
        # 它只说明「没被发牌」，所以下面还要过一道「在局证据优先」：已经弃牌/
        # 全下/亮牌/还挂着手牌图标的人，这一手显然有牌，不能被转场文案推翻。
        wait_kind = waiting_badge(avatar_text, avatar_score)
        # 注意：这里只是「文案是不是弃牌」的判定，和 PlayerState 里留档的
        # 原始 `fold_text`（头像文案本身）不是一回事，故另起名字。
        fold_badge = (is_fold_badge(avatar_text, avatar_score)
                      if (sid in avatar_reads and not shown_here) else None)
        # 弃牌 = 头像底角的红色手牌图标消失 **且** 头像中间出现「弃牌」。
        # 两个条件缺一不可：
        #   · 只看图标（没图标=弃牌）会在换手残留/遮挡/压缩时把在池玩家判成
        #     弃牌（4d6d 的大盲就是这样被丢掉的）；
        #   · 只看文字会被头像照片里的噪声误导。
        # 任一条件不满足 = 未知（None），交给筹码/下注/动作等结构化事实；
        # 不再用「没看到图标」这种负证据直接判弃。
        fold = None
        if icon is True:
            fold = False
        elif icon is False and fold_badge is True:
            fold = True
        elif sid == frame.hero_seat_id and fold_badge is True:
            # hero 头像从不挂手牌图标，没有条件 1，只能用「弃牌」二字。
            fold = True
        if shown_here:
            # 河底亮牌不代表没弃牌：早已弃牌的人也可能 show 牌。亮牌时头像
            # 被牌顶掉、图标也没有，只能沿用**前面动作**得出的弃牌判定
            # （上一帧被接受的状态）；没有历史就保持未知。
            fold = (prev_fold or {}).get(sid)

        all_in_badge = is_all_in_badge(avatar_text, avatar_score)
        if wait_kind is not None and (fold is True or shown_here
                                      or all_in_badge is True or icon is True):
            # 有明确在局证据 → 头像文案作废（它只表示「没发到牌」）
            wait_kind = None

        # 全下后筹码/下注位可能显示实时胜率（`30%`/`69%`）。百分数不是金额，
        # parse_amount 会拒掉；这里单独解析成 equity，并让 stack/bet 保持 None。
        chip_equity = parse_percent(chip_raw)
        bet_equity = parse_percent(bet_raw)
        eq_text, eq_score = equity_reads.get(sid, ("", 0.0))
        slot_equity = None
        if eq_score >= 0.3 and not fold:
            # 胜率槽必须有 `%`；裸数字很容易是牌背/筹码上的 1、8 之类。
            # 只有头像已经确认 All-in 时，才接受 OCR 吞掉 `%` 的裸数字。
            slot_equity = parse_equity(eq_text)
            if slot_equity is None and all_in_badge is True:
                slot_equity = parse_equity(eq_text, allow_bare=True)
        equity = (bet_equity if bet_equity is not None else
                  chip_equity if chip_equity is not None else slot_equity)

        chip: Amount = parse_amount(chip_raw)
        chip_loose = False
        if lenient_chips and not chip.ok and chip_equity is None:
            loose = parse_chip_loose(chip_raw)
            if loose is not None:
                chip = Amount(chip_raw, loose, "?", error=None)
                chip_loose = True
        bet_a, bet_part_raw = _parse_amount_slot(frame, "bet", sid)
        bet_loose = False
        if lenient_chips and bet_raw.strip() and not bet_a.ok and bet_equity is None:
            loose = parse_chip_loose(bet_raw)
            if loose is not None:
                bet_a = Amount(bet_raw, loose, "?", error=None)
                bet_loose = True
        # 空 bet 文本 = 这款 app 没渲染 pill = 本街 0。
        # **带中文的文本也是 0**：下注 pill 只有数字 + BB，读了汉字只可能是别的
        # UI 落进了这块 ROI（实测 WePoker 新加的「发发看 / 免费」促销按钮正好压在
        # hero 的 bet 槽上，读成「发发表」；动作徽章也是中文）。
        # 这种文本不是「没读懂的下注」：当成未知会把整帧丢掉 —— hero 一弃牌，
        # 下一手的牌面/建议就全被质量闸拦在外面，上一手也要等闸恢复才落库。
        # 只认汉字：`lOBB` / `BB` / `--` 这类没汉字的乱码仍然算读不出来
        # （可能是真下注被读坏），宁可丢一帧，也不能悄悄当成 0。
        # 百分数是胜率，不是下注额。
        bet_label = any("\u4e00" <= ch <= "\u9fff" for ch in bet_raw)
        bet = (None if bet_equity is not None else
               0.0 if (not bet_raw.strip() or bet_label) else
               (bet_a.value if bet_a.ok else None))
        stack = None if chip_equity is not None else (chip.value if chip.ok else None)

        # 名字粘性只在**这一座还有人的证据**时生效。不能拿「文本非空」当证据：
        # 人走后的空座位，筹码/下注槽也常被 rec 读成 `--`/`·` 之类乱码；
        # 若因此沿用上一手的名字，这个座位会被永远钉在「在场」，每帧报
        # `stack:<seat>`、整场丢帧（2026-10-29 实盘）。筹码、下注、动作、
        # 手牌图标、亮牌、All-in、胜率任一成立才算还有人；只剩名字槽本身
        # 有字时也算（人还在、筹码暂时被挡住）。
        #
        # 但**一手牌内的座位身份锁**打开时（roster），名字槽可能在开局就被
        # ROI 缓存钉住：人走了它还是返回旧名字，拿它当在场证据会让空座
        # 永远判在场。所以锁定的座位只看筹码/下注/图标等当前帧证据；
        # 名字由 roster 兜底沿用，不影响显示。
        name_text = ("" if name_is_badge
                     else (name_raw or "").strip())
        name_is_evidence = bool(name_text and action is None and sid not in roster)
        present = bool(
            name_is_evidence
            or name_is_badge
            or action is not None
            or stack is not None
            or (bet or 0) > 0
            or icon is True or shown_here
            or all_in_badge is True or equity is not None)
        tracked: str | None = None
        if name_tracker is not None:
            if present:
                tracked = name_tracker.resolve(sid, name_text or None, action)
                change = name_tracker.take_change(sid)
                if change is not None and change[0]:
                    # 只有「确实是两种拼法」时才记：换人（不像）和抖动（像）
                    # 分开写，复盘时能一眼看出是 OCR 抖还是座位换了人。
                    old, new = change
                    kind = ("名字抖动" if names_similar(old, new) else "名字换人")
                    name_warnings.append(f"{sid}: {kind} {old!r} → {new!r}")
            else:
                # 整座暂时没有「还有人」的证据：**不再立刻抹掉名字记忆**。
                # 旧代码这里调 forget()，下一次读数就成了「首读无条件采信」，
                # 于是换手空档里的一帧错读会被锁成整场的名字
                # （2026-11-01 21:22 实盘，锁了 10 分钟）。
                # absent() 只把当前名字下线并保留「上一次的名字」，座位重新
                # 出现时它会先被判成「候选」而不是直接覆盖。
                name_tracker.absent(sid)
                tracked = None
            name = tracked
        else:
            prev_name = prev_names.get(sid)
            name = (None if not present else
                    prev_name if action else sticky_name(prev_name,
                                                         name_text or None))

        # 「正在带入」：头像上写着带入 + 筹码为 0 → 人坐在桌边但没被发牌。
        #
        # 光靠头像文字不够（实测 rec 时好时坏，人数会在 8/9 之间跳）——再加一条
        # 结构化判据：**筹码 0、本街没下注、也没有动作** 的人一定不在这一手
        # （全下的人 bet>0；跟注/下注过的人 pill 至少在结算前还在；hero 除外）。
        #
        # 2026-11-02 起补上另外两种文案：等待审核 / 等待中。这三个词都表示
        # 「人坐在桌边、这一手没他份」，而且**筹码可能照常显示**（实测 200BB），
        # 所以文案命中就认，不再要求筹码 0。上面那道「在局证据优先」已经把
        # 明显有牌的人排除了，走到这里的人才是真的没被发牌。
        buying_in = (wait_kind == "buying_in"
                     or ("带入" in avatar_text and stack == 0.0))
        waiting = wait_kind == "waiting"
        # 一手牌内的名单锁：锁定的座位**默认在局**。这一帧名字/筹码/下注
        # 都读不到只说明被遮挡（质量闸会按 stack/bet 缺失丢帧），不能据此
        # 把人移出这一手 —— 移出会改人数/位置/行动顺序，还会被 vanish 逻辑
        # 误判成「离桌 → 弃牌」。空座/离桌只认头像「+」占位（正向证据，
        # state_stream 确认后写进 left_seats）。
        rostered = bool(roster and sid in roster)
        # 全下玩家在**下注结算后**会变成「筹码 0、pill 已收走」，看起来和离桌一样。
        # 用上一帧的全下集合区分：他全下过、筹码还是 0，就还是全下。
        was_all_in = sid in prev_all_in
        zero_idle = (stack == 0.0 and (bet or 0) == 0 and not action
                     and sid != frame.hero_seat_id and not was_all_in
                     and all_in_badge is not True and equity is None
                     and not shown_here and icon is not True)

        # ── 这个位置到底有没有人 ──────────────────────────────
        # 「没有手牌」= 明确没看到头像底角的 POKER 图标、也没亮牌。
        # （icon=None 是「判断不了」，不能当没有。）
        no_cards = (icon is False and not shown_here
                    and all_in_badge is not True and equity is None
                    and not was_all_in)
        # 1) 虚线「+」占位：名字/筹码/下注/动作全空，也没有手牌 → 这个位置没人。
        #    + 本来就不渲染文字，所以这些槽位天然为空；用图标再确认一次，
        #    避免 rec 的噪声把空座当成玩家。
        empty_slot = (no_cards and sid != frame.hero_seat_id
                      and name is None and stack is None
                      and (bet or 0) == 0 and action is None)
        # 2) 筹码 0 且没有手牌/亮牌/All-in → 人不在这一手。
        #    有名字的是「坐出/带入中」，连名字都没有就是空位。
        empty_zero = (no_cards and stack == 0.0 and (bet or 0) == 0
                      and sid != frame.hero_seat_id)
        if rostered:
            # 名单锁定的座位只有三种玩家状态：在局（默认）/ 等待入局 / 弃牌。
            # 「等待入局」只由头像文案（等待审核 / 等待中 / 正在带入）触发，
            # 不再用「筹码 0 + 没下注」这种结构性证据去猜。
            no_player = False
            sitting_out = waiting and not buying_in
        else:
            no_player = empty_slot or (empty_zero and name is None)
            # 在座未入局：筹码 0 的空转态（zero_idle / empty_zero）**或** 头像文案
            # 直接说明在等（等待审核 / 等待中）。后者筹码可以不为 0，所以单独一项。
            sitting_out = (zero_idle or waiting
                           or (empty_zero and not no_player)) and not buying_in

        # 空座位（牌桌没坐满时那个虚线「+」）三块槽位都是空文本：名字 None、
        # 筹码 None、bet 空文本被记成 0.0。**bet=0 不能当在场证据** —— 否则空座
        # 会以「有下注字段、但没有筹码」进入质量闸，报 stack:<空座>，整场 100%
        # 丢帧。只有 bet>0 才说明这座真的往池里放过钱；动作/弃牌徽章同理是
        # 在场证据（名字和筹码都被挡住时仍要能报出 stack 错误，而不是悄悄少人）。
        in_hand = (
            # 名单锁定的座位默认在局；只有头像文案（等待入局）能把他移出去。
            (rostered and not buying_in and not waiting)
            or ((name is not None or stack is not None or (bet or 0) > 0
                 or action is not None or fold is True
                 or icon is True or shown_here
                 or all_in_badge is True or equity is not None)
                and not buying_in and not sitting_out and not no_player)
        )
        # 头像黄色 All-in 徽章是最直接的证据；胜率显示也只在全下后出现。
        # 旧的结构化判据（筹码 0 + 有下注/曾经全下）保留给徽章读不到时兜底。
        all_in = bool(in_hand and (
            all_in_badge is True or equity is not None
            or (stack == 0.0 and ((bet or 0) > 0 or was_all_in))))
        if all_in:
            fold = False
        if no_player:
            fold = None
        status = ("buying_in" if buying_in else
                  "away" if no_player else
                  "sitting_out" if sitting_out else
                  "away" if not in_hand else
                  "folded" if fold else "playing")

        notes: list[str] = []
        if chip_loose:
            notes.append(f"chip: 宽松解析 {chip_raw!r} → {chip.value}")
        elif chip_raw.strip() and not chip.ok and not all_in:
            notes.append(f"chip: {chip.error}")
        if bet_label:
            # 不是金额的 UI 文本（促销按钮/动作徽章）落在 bet 槽：按 0 处理，
            # 但留一条 note 方便查「这块 ROI 又被谁占了」。
            notes.append(f"bet: 标签文本 {bet_raw!r} → 本街 0")
        elif bet_loose:
            notes.append(f"bet: 宽松解析 {bet_raw!r} → {bet_a.value}")
        elif bet_part_raw is not None:
            notes.append(f"bet: 多框，取金额框 {bet_part_raw!r}"
                         f"（整块 {bet_raw!r}）")
        elif bet_a.tail_noise:
            # 数字清洗：单位后面那串是筹码图标/误读字符，已经丢掉。
            notes.append(f"bet: 清洗 {bet_raw!r} → {bet_a.value:g}BB"
                         f"（丢掉 {bet_a.tail_noise!r}）")
        elif bet_raw.strip() and not bet_a.ok and not all_in:
            notes.append(f"bet: {bet_a.error}")
        if equity is not None:
            notes.append(f"equity: {equity:g}%")
        if wait_kind is not None:
            # 把**原始读数**写出来：这三个词 rec 常读错（等待中 → 练待中/#待中），
            # 复盘时要能一眼看出「是没读到，还是读成了别的」。
            label = "正在带入" if wait_kind == "buying_in" else "在座未入局"
            notes.append(f"头像 {avatar_text.strip()!r} → {label}，本手不参与")

        # ── 一手牌内的座位身份锁 ─────────────────────────────
        # 离桌的人肯定已经弃牌：本手剩余时间继续保留在 players 里，沿用锁定
        # 时的名字/位置，按 folded 处理，不再要求筹码/下注（人都不在了，
        # 这两块读不到是正常的，不能因此整帧丢弃）。
        left_here = sid in left
        if left_here:
            roster_name, roster_position = roster.get(sid, (None, None))
            name = roster_name or name
            in_hand = True
            fold = True
            all_in = False
            equity = None
            status = "folded"
            stack = None
            bet = 0.0
            action = None
            notes = [n for n in notes
                     if not n.startswith(("chip:", "bet:", "equity:"))]
            notes.append("本手中途离桌，按弃牌处理")
        elif roster and sid in roster:
            # 名字暂时被遮住时用锁定值兜底；读到新值（OCR 纠正）仍然优先。
            roster_name, _ = roster[sid]
            if name is None and roster_name:
                name = roster_name
        elif roster and sid != frame.hero_seat_id:
            # 本手开始后才坐下的人：这一手不参与、不算人数、不输出名字，
            # 等下一手再重新识别（roster 在换手时重建）。牌桌座位数不变，
            # 所以这里不动 table_seats，只把 in_hand/status 压掉。
            if in_hand or status == "folded":
                in_hand = False
                fold = None
                all_in = False
                equity = None
                status = "sitting_out"
                stack = None
                bet = 0.0
                action = None
                name = None
                notes = ["本手开始后入座，下一手再识别"]

        players.append(PlayerState(
            seat_id=sid, name=name, stack=stack, bet=bet, action=action,
            fold=fold, in_hand=in_hand, all_in=all_in, equity=equity,
            status=status, left=left_here, notes=notes,
            # 诊断字段：原始读数 / rec 分数 / tracker 的结论。名字被动作徽章
            # 盖住时 name_raw 是徽章文字，不写出来免得误导。
            name_raw=(name_text or None) if action is None else None,
            name_score=name_score if (action is None and name_text) else 0.0,
            # 离桌座位的 tracker 结论属于**下一手**的占用者，不能写进这一手
            name_tracked=None if left_here else tracked,
            # 弃牌判据的原始读数：`fold` 合并了图标与文案，这里原样留证。
            fold_icon=icon,
            fold_text=((avatar_text or "").strip() or None
                       if sid in avatar_reads else None),
            fold_text_score=(avatar_score
                             if (sid in avatar_reads
                                 and (avatar_text or "").strip()) else None),
        ))

    in_hand = [p for p in players if p.in_hand]
    return players, name_warnings


def _assign_positions(ctx: BuildContext, in_hand: list[PlayerState],
                      dealer: str | None):
    """按名单锁（没有锁时按在局玩家）排位置。"""
    frame = ctx.frame
    roster = dict(ctx.hand_roster or {})
    # ── 位置（要等在场人数定下来） ─────────────────────────
    # `seat_ring` 传整张桌的固定座位环（含空座）：中途有人退出、按钮还留在
    # 空座上时按死按钮排，而不是把整帧判成「庄位不在环上」丢掉。见 positions.py。
    #
    # 有座位身份锁时按**锁定的 roster** 排，而不是当前还在场的人：9 人桌
    # 中途退了一个，若按 8 人重新编号，空位后面的人会整体往前挪一位
    # （hero 的 UTG+2→LJ），行动顺序/AI 上下文全错。roster 里离桌的人仍占
    # 位置，只是按弃牌处理。
    position_seats = ([s.seat_id for s in frame.seats if s.seat_id in roster]
                      if roster else [p.seat_id for p in in_hand])
    pos = assign_positions(position_seats, dealer,
                           seat_ring=[s.seat_id for s in frame.seats])
    if pos.ok:
        for p in in_hand:
            p.position = pos.positions.get(p.seat_id)
    return pos


def _quality_gate(ctx: BuildContext, in_hand: list[PlayerState],
                  blinds, pot, pbs, street_total, dealer, pos):
    """一帧级兜底 + errors/bet_warnings；errors 顺序即丢帧口径。"""
    frame = ctx.frame
    prev_state = ctx.prev_state
    shown = set(ctx.shown_seats or ())
    # ── 质量闸：无法精准识别的帧要能被明确区分 ─────────────
    #
    # 先做两处**一帧级**的兜底（底池没变 = 这一帧没有新的事实，读数抖一下
    # 不该整帧丢）：
    #
    # ① 上一街底池横幅和底池横幅不是同一帧刷新：换街那一帧 pbs 已经跳到
    #    「上一街结算后的底池」，底池却还是旧值（pbs > pot / pbs == pot 而
    #    场上还有下注）。这时沿用上一帧的 pbs，下一帧两个横幅就对齐了。
    # ② 某个座位的下注 pill 在淡入/淡出那一两帧读成非金额（`lBB`、`BB`）。
    #    底池没变 → 下注额也不可能变，沿用上一帧读数。**hero 回合不兜底**：
    #    宁可丢一帧，也不能拿旧下注去算 to_call。
    if prev_state is not None and pot is not None \
            and prev_state.pot is not None \
            and abs(pot - prev_state.pot) < 1e-9:
        prev_pbs = prev_state.pot_before_street
        if (prev_pbs is not None and prev_state.pot > prev_pbs + 1e-9
                and (pbs is None or pbs > pot + 1e-6
                     or (pbs >= pot - 1e-6
                         and any((p.bet or 0) > 0 for p in in_hand)))):
            pbs = prev_pbs
            street_total = (round(pot - pbs, 2)
                            if pbs <= pot else None)
        if frame.hero_turn is not True:
            prev_bets = {p.seat_id: p.bet for p in prev_state.players
                         if p.in_hand}
            same_seats = prev_bets.keys() == {p.seat_id for p in in_hand}
            if same_seats:
                for p in in_hand:
                    if (p.bet is None and not p.all_in and not p.bet_unknown
                            and prev_bets.get(p.seat_id) is not None):
                        p.bet = prev_bets[p.seat_id]
                        p.notes.append(
                            f"bet: 沿用上一帧 {p.bet:g}BB（这一帧读不出，"
                            "底池没变）")

    errors: list[str] = []
    if blinds is None:
        errors.append("blinds")
    # 间隙帧的 pot 仍然是 None（不能当成 0 传下去）：这里照样报错，让状态
    # 保持「不可用」；上层用 `pot_idle` 把它归类成手牌间隙，而不是识别失败。
    if pot is None:
        errors.append("pot")
    if dealer is None:
        errors.append("dealer")
    if len(in_hand) < 2:
        errors.append(f"players:{len(in_hand)}")
    if dealer is not None and not pos.ok:
        errors.append(f"positions:{pos.error}")
    if pbs is not None and pot is not None and pbs > pot + 1e-6:
        errors.append("pbs_gt_pot")
    # 有人还有本街下注 → 本街投进底池的钱 > 0 → 上一街底池必须严格小于底池。
    # 相等只可能是「下注刚结算完、pill 已被收走」——那时不会有 bet>0 的座。
    if (pbs is not None and pot is not None
            and any((p.bet or 0) > 0 for p in in_hand)
            and pbs >= pot - 1e-6):
        errors.append("pbs_inconsistent")

    # 底池自己也得先站得住，才能拿它当「bet 读坏」的证据：翻前开局底池至少
    # 是前注总额，读到 0/极小值说明底池槽还没渲染或读坏了 —— 那时宁可照旧
    # 丢帧，也不能拿一个坏底池去判别人的下注。
    _ante = (blinds.ante or 0.0) if blinds is not None else 0.0
    pot_trusted = (pot is not None and pot > 1e-9
                   and (_ante <= 0 or pot >= _ante * max(len(in_hand), 2) - 1e-6))
    bet_warnings: list[str] = []

    for p in in_hand:
        # 离桌的人筹码/下注位已经不存在，读不到是正常的：不能因此整帧丢弃。
        if p.left:
            continue
        # **摊牌**的座位：头像被亮出的两张牌盖住、筹码胶囊显示 `0`（没有 BB
        # 单位 → None）、All-in 徽章也被牌盖住 —— 这时读不到筹码/下注是画面
        # 结构决定的，不是读数坏了。2026-11-11 实盘：两手全下摊牌各丢
        # 147 / 108 帧（19.9s / 14.5s），整个摊牌窗口没有状态、手牌结束判定
        # 也被拖慢。
        showdown = p.seat_id in shown
        # 全下玩家的筹码/下注位可能被 All-in 徽章或胜率百分比替代，允许读不到；
        # 其他玩家仍然要求这两个字段精准，否则整帧丢弃。
        if p.stack is None and not p.all_in and not showdown:
            errors.append(f"stack:{p.seat_id}")
        # **本街下注不可能大于底池**（底池 = 全场投入之和），读到就是串位：
        # bet 槽左侧的筹码图标会被 rec 读成前导数字（实测 6.5BB → 26.5BB，
        # logs/incidents/20261106_003352_339805_gate_bet），凑巧也读成过
        # 20.5 / 5.0 这类能过闸的值。这一个字段明确不可用，但**别人的筹码、
        # 底池、动作徽章仍然是有效的** —— 只把它降级成未知，不能整帧丢掉。
        # 2026-11-08 实盘：topL 一位读坏，连续丢 52 / 95 帧，下手牌
        # 8.8s / 22s 才开始识别（换手识别「慢」的真正原因）。
        dropped_bet = False
        if p.bet is not None and pot is not None and p.bet > pot + 1e-6:
            if pot_trusted:
                p.notes.append(f"bet {p.bet:g} > pot {pot:g}，按未知处理")
                p.bet = None
                p.bet_unknown = True
                bet_warnings.append(p.seat_id)
            else:
                # 底池自己就可疑（0/比前注总额还小）：这时分不清是 bet 还是
                # pot 读坏，照旧整帧丢弃。整帧都要丢，就别再改写原读数 ——
                # 排障时要能看到「当时到底读到了什么」。
                errors.append(f"bet_gt_pot:{p.seat_id}")
                dropped_bet = True
        # **单个座位的下注不可能超过本街总额**（本街总额 = 全场本街投入之和）。
        # 2026-11-12 实盘见过 bet 槽左侧的筹码图标被读成前导数字（6.5BB →
        # 26.5BB / 55BB），只要底池够大，上面那条 `bet > pot` 就漏得过去。
        # 口径与 `bet > pot` 一致：**降级成未知、记警告、不丢整帧**
        # （2026-11-13 用户裁定「降级，并给出错误日志」）。以前这里报错丢帧，
        # 一个座位读坏会把整桌的筹码/底池/动作一起作废。
        # 不再尝试 ÷10/÷100 把它「修」回 5.5BB —— 那是猜数字。
        if (not dropped_bet and p.bet is not None and street_total is not None
                and p.bet > street_total + 1e-6):
            p.notes.append(
                f"bet {p.bet:g} > 本街总额 {street_total:g}，按未知处理")
            p.bet = None
            p.bet_unknown = True
            bet_warnings.append(p.seat_id)
        if p.bet is None and not p.all_in and not p.bet_unknown and not showdown:
            errors.append(f"bet:{p.seat_id}")
    return errors, bet_warnings, street_total, pbs


def _build_state(ctx: BuildContext) -> FrameState:
    """编排：全局事实 → 座位 → 位置 → 质量闸 → FrameState。"""
    frame = ctx.frame
    (blinds, blinds_source, pot, pbs, pot_idle, street_total,
     dealer) = _global_facts(ctx)
    players, name_warnings = _build_players(ctx)
    in_hand = [p for p in players if p.in_hand]
    pos = _assign_positions(ctx, in_hand, dealer)
    if pos.ok:
        for p in in_hand:
            p.position = pos.positions.get(p.seat_id)
    errors, bet_warnings, street_total, pbs = _quality_gate(
        ctx, in_hand, blinds, pot, pbs, street_total, dealer, pos)
    return FrameState(
        frame_id=frame.frame_id,
        table_seats=len(players),
        players_in_hand=len(in_hand),
        players=players,
        blinds=blinds,
        blinds_source=blinds_source,
        dealer=dealer,
        hero_seat_id=frame.hero_seat_id,
        hero_turn=frame.hero_turn,
        pot=pot,
        pot_before_street=pbs,
        pot_idle=pot_idle,
        street_bet_total=street_total,
        bomb_pot=_bomb_banner(frame),
        errors=errors,
        name_warnings=name_warnings,
        bet_warnings=bet_warnings,
    )


def signature(state: FrameState) -> tuple:
    """状态指纹：两份指纹相同 = 没有需要记录的变化。

    用户定的触发口径是「筹码 / 下注等没有变化就不记录」，所以指纹只收**牌局事实**：
    盲注、庄位、底池、在场人数、爆炸底池横幅、每座的
    {位置, 名字, 后手, 下注, 在场, 全下, 是否中途离桌}。

    刻意**不含**（它们仍写进输出，只是不触发记录）：

      · `action` / `fold` —— 动作徽章会在动作结束后淡出、弃牌检测本身是三态
        best-effort，拿它们触发会制造假记录。
      · `hero_turn` —— 实测预设按钮会在一次回合内短暂消失又出现（真机 81s 有
        `2.5BB`、82.5s 空、84s `15BB`），拿它触发会多出两条只有回合字段变化的记录。
      · `pot_before_street` / `street_bet_total` —— 这个 pill 实测会偶发误读
        （把底池值读到 pbs 槽），而底池/下注本身已经覆盖了真实变化。
      · 盲注的 `ante` —— 括号被读丢时靠继承，`None→0.5` 不该算状态变化；
        sb/bb 变了才是真的升级。
      · `blinds_source` / `notes` —— 读数来源和备注不是状态。
    """
    b = (state.blinds.sb, state.blinds.bb) if state.blinds else None
    return (
        b,
        state.dealer,
        _r(state.pot),
        state.players_in_hand,
        state.bomb_pot,
        tuple(
            (p.seat_id, p.position, p.name, _r(p.stack), _r(p.bet), p.in_hand,
             p.all_in, p.left, _r(p.equity) if p.equity is not None else None)
            for p in state.players
        ),
    )


def action_signature(state: FrameState) -> tuple:
    """动作是否生效的指纹：比 `signature()` 多收 `hero_turn` / `action` / `fold`。

    记录用的 `signature()` 刻意排除这些字段（按钮闪断会制造假记录），但自动
    点击的重试恰好要判断「点完之后回合有没有结束 / 动作徽章有没有变」。所以
    单独一份：只要这一份变了，就认为点击已被客户端接受，不再重试。
    """
    return (
        state.hero_turn,
        state.dealer,
        _r(state.pot),
        _r(state.pot_before_street),
        state.players_in_hand,
        tuple(
            (p.seat_id, p.action, p.fold, p.in_hand,
             _r(p.stack), _r(p.bet), p.all_in, _r(p.equity))
            for p in state.players
        ),
    )


def names_of(state: FrameState) -> dict[str, str]:
    return {p.seat_id: p.name for p in state.players if p.name}
