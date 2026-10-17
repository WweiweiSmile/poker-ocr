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
  · **盲注**解析失败时可以继承上一帧（`blinds.source = "carried"`）—— 盲注只在
    升级时变，而那条横幅经常被「All-in 全场禁言中…」盖住；继承比丢帧合理。
    但**底池不能继承**（每手/每街都在变）。
  · **弃牌**徽章画在头像上，实测 rec 会系统性读成形近字（弃牌→奔牌），
    所以 `fold` 是 best-effort 三态，允许 None（没读到 ≠ 没弃牌）。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .parse import (
    Amount, Blinds, NameTracker, is_fold_badge, parse_action, parse_amount,
    parse_blinds, sticky_name,
)
from .positions import assign_positions
from .profile import Profile
from .roi_reader import RoiFrame

# 状态里的动作枚举（与 call-back / api.py 的口径一致）
ACTIONS = ("check", "call", "bet", "raise", "fold")


@dataclass
class PlayerState:
    seat_id: str
    name: str | None = None
    position: str | None = None
    stack: float | None = None       # 后手筹码（BB）
    bet: float | None = None         # 本街已下注（BB）；None = 读到了但没读懂
    action: str | None = None        # check/call/bet/raise/fold（名字槽徽章）
    fold: bool | None = None         # 头像徽章 best-effort
    in_hand: bool = False
    all_in: bool = False
    status: str = "away"             # playing / folded / buying_in / away
    notes: list[str] = dc_field(default_factory=list)

    def to_debug(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "position": self.position,
            "name": self.name,
            "stack": self.stack,
            "bet": self.bet,
            "action": self.action,
            "fold": self.fold,
            "in_hand": self.in_hand,
            "all_in": self.all_in,
            "status": self.status,
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
    street_bet_total: float | None = None
    errors: list[str] = dc_field(default_factory=list)

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
            "players": [p.to_debug() for p in self.players],
            **({"errors": self.errors} if self.errors else {}),
        }


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def build_state(frame: RoiFrame, profile: Profile,
                prev_names: dict[str, str] | None = None,
                prev_blinds: Blinds | None = None,
                avatar_reads: dict[str, tuple[str, float]] | None = None,
                name_tracker: "NameTracker | None" = None,
                prev_all_in: set[str] | None = None) -> FrameState:
    """一帧 RoiFrame → FrameState（含解析错误清单）。

    `prev_names` 是**上一份被接受的状态**里的名字（名称粘性用）；
    `prev_blinds` 用于盲注横幅被盖住时继承；`avatar_reads` 是头像 ROI 的
    `{seat_id: (text, score)}`（弃牌检测，可选）。
    """
    prev_names = prev_names or {}
    avatar_reads = avatar_reads or {}
    prev_all_in = prev_all_in or set()

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

    pot_a = parse_amount(frame.text("pot"))
    pbs_a = parse_amount(frame.text("pot_before_street"))
    pot, pbs = pot_a.value, pbs_a.value
    street_total = (round(pot - pbs, 2) if pot is not None and pbs is not None
                    and pbs <= pot else None)

    dealer = frame.dealer.seat_id if (frame.dealer and frame.dealer.status == "found") else None

    # ── 每个座位 ──────────────────────────────────────────
    players: list[PlayerState] = []
    for seat_roi in frame.seats:
        sid = seat_roi.seat_id
        name_raw = frame.text("name", sid)
        chip_raw = frame.text("chip", sid)
        bet_raw = frame.text("bet", sid)

        # 动作徽章会盖住名字：先认徽章，再决定这块文本是不是名字
        action = parse_action(name_raw)
        fold = None
        if sid in avatar_reads:
            fold = is_fold_badge(*avatar_reads[sid])
        if action == "fold":
            fold = True

        # 名字粘性只在**这一座还有读数**时生效。整座空白（人走了 / 被面板盖住）
        # 必须能被看见：否则旧名字会把这个座位永远钉在「在场」，人走了也清不掉。
        has_evidence = bool(name_raw or chip_raw or bet_raw)
        if name_tracker is not None:
            name = (None if not has_evidence else
                    name_tracker.resolve(sid, name_raw or None, action))
        else:
            prev_name = prev_names.get(sid)
            name = (None if not has_evidence else
                    prev_name if action else sticky_name(prev_name, name_raw or None))

        chip: Amount = parse_amount(chip_raw)
        bet_a: Amount = parse_amount(bet_raw)
        # 空 bet 文本 = 这款 app 没渲染 pill = 本街 0；读到乱码才是「未知」
        bet = 0.0 if not bet_raw.strip() else (bet_a.value if bet_a.ok else None)
        stack = chip.value if chip.ok else None

        # 「正在带入」：头像上写着带入 + 筹码为 0 → 人坐在桌边但没被发牌。
        #
        # 光靠头像文字不够（实测 rec 时好时坏，人数会在 8/9 之间跳）——再加一条
        # 结构化判据：**筹码 0、本街没下注、也没有动作** 的人一定不在这一手
        # （全下的人 bet>0；跟注/下注过的人 pill 至少在结算前还在；hero 除外）。
        avatar_text = (avatar_reads.get(sid) or ("", 0.0))[0]
        buying_in = "带入" in avatar_text and stack == 0.0
        # 全下玩家在**下注结算后**会变成「筹码 0、pill 已收走」，看起来和离桌一样。
        # 用上一帧的全下集合区分：他全下过、筹码还是 0，就还是全下。
        was_all_in = sid in prev_all_in
        zero_idle = (stack == 0.0 and (bet or 0) == 0 and not action
                     and sid != frame.hero_seat_id and not was_all_in)
        sitting_out = zero_idle and not buying_in

        in_hand = ((name is not None or stack is not None or bet is not None)
                   and not buying_in and not sitting_out)
        all_in = bool(in_hand and stack == 0.0
                      and ((bet or 0) > 0 or was_all_in))
        status = ("buying_in" if buying_in else
                  "sitting_out" if sitting_out else
                  "away" if not in_hand else
                  "folded" if fold else "playing")

        notes: list[str] = []
        if chip_raw.strip() and not chip.ok:
            notes.append(f"chip: {chip.error}")
        if bet_raw.strip() and not bet_a.ok:
            notes.append(f"bet: {bet_a.error}")

        players.append(PlayerState(
            seat_id=sid, name=name, stack=stack, bet=bet, action=action,
            fold=fold, in_hand=in_hand, all_in=all_in, status=status, notes=notes,
        ))

    in_hand = [p for p in players if p.in_hand]

    # ── 位置（要等在场人数定下来） ─────────────────────────
    pos = assign_positions([p.seat_id for p in in_hand], dealer)
    if pos.ok:
        for p in in_hand:
            p.position = pos.positions.get(p.seat_id)

    # ── 质量闸：无法精准识别的帧要能被明确区分 ─────────────
    errors: list[str] = []
    if blinds is None:
        errors.append("blinds")
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

    for p in in_hand:
        if p.stack is None:
            errors.append(f"stack:{p.seat_id}")
        if p.bet is None:
            errors.append(f"bet:{p.seat_id}")
        if p.bet is not None and pot is not None and p.bet > pot + 1e-6:
            errors.append(f"bet_gt_pot:{p.seat_id}")

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
        street_bet_total=street_total,
        errors=errors,
    )


def signature(state: FrameState) -> tuple:
    """状态指纹：两份指纹相同 = 没有需要记录的变化。

    用户定的触发口径是「筹码 / 下注等没有变化就不记录」，所以指纹只收**牌局事实**：
    盲注、庄位、底池、在场人数、每座的 {位置, 名字, 后手, 下注, 在场, 全下}。

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
        tuple(
            (p.seat_id, p.position, p.name, _r(p.stack), _r(p.bet), p.in_hand, p.all_in)
            for p in state.players
        ),
    )


def names_of(state: FrameState) -> dict[str, str]:
    return {p.seat_id: p.name for p in state.players if p.name}
