"""实时手牌跟踪：逐帧累积「这一手发生了什么」，并在轮到我时给出决策点。

`StateStream` 记录的是**桌面状态**（底池、筹码、每座的下注），不是**行动线**：
动作徽章会被刻意排除在变化指纹之外（见 frame_state.signature 的说明），
所以 `action` 只散落在每一帧里，没人把它们串成一手牌。这个模块补的就是这一层。

几条刻意的口径：

  · **吃每一帧被接受的状态，不只吃被记录的**。签名不含 `action`/`hero_turn`，
    对手过牌、轮到我，都不会产生新记录 —— 只吃 record 会漏掉一半行动线。
  · **行动按「徽章 + 本街下注额」落库**，同座同街同动作的重复帧只记一次。
    去重看的是**这个座自己上一次记录的动作**，不是「它是不是整条序列的最后一条」：
    动作徽章会在名字槽停留好几秒，期间别人可能已经行动过。类型相同且下注额没变
    → 丢弃；下注额变大且中间有人加注过 → 是跟注之后又被加注、再跟的一次，
    必须记两条。
  · **金额一律用「本街加到总额」**（与 call-back / coach 同口径）。
    跟注/过牌的金额是下注 pill 的原值；读不到就不写金额，不猜。
  · **徽章漏读时从下注额反推**：下注额升到当前水位以上就是加注（新街第一次
    是下注），追平就是跟注。盲注是基线，不会被记成「跟注」。
  · **街末用底池对账补漏**：下一街的 `pot_before_street` 就是这一街结算后的底池，
    减去这一街起始底池即「这一街所有人投进去的总和」。它比行动线记录的总额多时，
    先按已知水位补跟注；对不上再用剩余人数反解最终水位，补「一次加注 + 跟注」。
    加注金额由底池精确反解，但谁是加注者只能按行动顺序取最早能行动的人；
    两种情况都对不上就留空不猜。
  · **换手判据**：庄位变了 / 公共牌连续 2 帧掉回 0 / hero 手牌变成另一组。
    hero 换牌不再单独触发换手：翻后（或翻前已经有人入池）换牌按 OCR 纠正原地
    更新，只有底池还在盲注/前注水位时才当新手；人数骤变也只在翻前 + 初始底池
    时才认，避免资料弹窗把一手牌劈成两半。1/2 张牌是发牌动画，5→4 更像读错
    一张，都不算换手。
  · **换手空档就是新手**：空档手（还没有 hero 手牌）读到第一组牌时直接补上，
    不另开一手；空档期间读到的起始筹码也能继续用。
  · **轮到我用「连续帧确认 + 决策点指纹」去重**，不用单帧上升沿。真机上预设按钮
    会在一次回合内消失又出现（见 frame_state 的注释），上升沿会重复触发；
    而多人池里 hero 过牌后对手下注、又轮回 hero 时，按钮可能全程都在、
    根本没有上升沿。指纹里含行动条数与最高下注，正好同时解决这两种情况。
  · **换手空档也会开一手**：上一手收牌后、下一手发牌前有一段没有 hero 手牌的帧，
    这里会先把空壳记下来。落库层默认按「没有 hero 手牌」跳过，避免每手牌后面
    多一条空记录；旁观/观察对手时用 `--record-without-hero` 保留
    （见 live.LiveProcessor._write_hand）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .frame_state import FrameState, PlayerState
from .parse import Blinds

PREFLOP = "preflop"
FLOP = "flop"
TURN = "turn"
RIVER = "river"

# 公共牌张数 → 街。0 张是翻前，1/2 张是发牌动画，5 张之后是河牌
STREET_BY_BOARD = {0: PREFLOP, 3: FLOP, 4: TURN, 5: RIVER}

# 街的先后顺序。只允许「往后」推进时对账；单帧 5→3 的误读不能倒着补
STREET_SEQ = {PREFLOP: 0, FLOP: 1, TURN: 2, RIVER: 3}

# 徽章原文（parse.ACTION_WORDS 已经翻成英文）→ 我们的行动类型，一一对应
ACTION_TYPES = ("check", "call", "bet", "raise", "fold")

AGGRESSIVE = ("bet", "raise")

# 换手时「在局人数骤变」的阈值。注意这不是桌型座位数（那个用 state.table_seats）
IN_HAND_JUMP = 2

# 全下要连续几帧确认才写进 all_ins。摊牌亮牌/牌背会让 OCR 单帧误判成
# All-in；单帧就记会把「只秀一张 K」的玩家写成全下。
ALL_IN_CONFIRM_FRAMES = 2

# `finish_current()` 之后等新手信号的上限帧数。超过就强制开新手：
# 牌背/弹窗可能让 hero 手牌和庄位连续读不到，不能无限等下去。
AWAIT_NEW_HAND_MAX_FRAMES = 20

# 写进 hand 记录时只保留这些关键词命中的牌面异常；动画期间的
# 「新读数待确认」是正常过程，写进手牌记忆只会变成噪声。
_CARD_WARNING_KEYS = ("回退", "牌背", "遮挡", "不完整", "合法张数",
                      "不可靠", "置信度", "没读出", "投票")

# 翻前/翻后共用的行动顺序（各人数下的位置表都是它的子序列）。
# 街末补漏时用它给同一批跟注排序，避免按物理座位顺序把 CO 排到 BTN 后面。
ACTION_ORDER = ("SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN")

# 街末对账的金额容差：底池/下注都是 0.01BB 精度的文本
RECONCILE_TOLERANCE = 0.02

# 爆炸底池强制前注：每人 5BB。横幅没抓到时，用「ante≥5 且翻前无动作」
# 兜底识别，不能把这条强制前注当成一次翻前行动机会。
BOMB_ANTE_BB = 5.0


def street_of_board(board_len: int) -> str | None:
    """公共牌张数 → 街。1/2 张是动画中间态，返回 None（不推进街）。"""
    return STREET_BY_BOARD.get(board_len)


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


@dataclass(frozen=True)
class ActionRecord:
    """一条行动。amount 是**本街加到总额**（BB），读不到为 None。"""

    street: str
    seat_id: str
    name: str | None
    position: str | None
    action: str
    amount: float | None = None
    all_in: bool = False
    is_hero: bool = False
    # 视频流漏抓、由街末底池对账补出来的记录。落库时单独标出来，复盘时可区分
    inferred: bool = False

    def label(self) -> str:
        if self.action == "check":
            return "过牌"
        if self.action == "fold":
            return "弃牌"
        if self.action == "call":
            return "跟注" + (f" {self.amount}BB" if self.amount else "")
        if self.action == "bet":
            return "下注" + (f" {self.amount}BB" if self.amount else "")
        if self.action == "raise":
            return "加注到" + (f" {self.amount}BB" if self.amount else " ?")
        return self.action


@dataclass(frozen=True)
class StreetRecord:
    street: str
    pot_start: float | None
    actions: tuple[ActionRecord, ...]


@dataclass(frozen=True)
class OpponentSnapshot:
    seat_id: str
    name: str | None
    position: str | None
    stack: float | None
    bet: float | None
    folded: bool


@dataclass(frozen=True)
class HandSnapshot:
    """某一瞬间这手牌的只读快照。AI 在后台线程里读它，主循环继续改写实时手牌。"""

    hand_id: str
    hand_index: int
    street: str
    board: tuple[str, ...]
    hero_cards: tuple[str, ...]
    hero_position: str | None
    hero_stack: float | None
    hero_bet: float | None
    hero_invested: float | None
    to_call: float
    second_bet: float | None
    pot: float | None
    pot_before_street: float | None
    # 牌桌座位数（9 人桌 = 9），不是当前还在局的人数。
    # 后者用 opponents_in_hand（已弃牌的人仍留在 opponents 里）。
    table_size: int
    blinds: Blinds | None
    opponents: tuple[OpponentSnapshot, ...]
    streets: tuple[StreetRecord, ...]
    preflop_shapes: tuple[str, ...]
    hero_is_pfr: bool
    cbet_spot: bool
    decision_key: tuple
    warnings: tuple[str, ...] = ()
    # 屏幕中央出现过「幸运暴击 / BOMB POT」横幅 = 这手是爆炸底池。
    # 与按底池/人数推算的 explosive_pot 互相独立，见 advice.is_explosive_pot。
    bomb_pot: bool = False
    # 这手实际参与的人数（含本街已弃牌，不含空座/离桌）。
    # 炸弹池按每人固定 5BB 收前注，推算人均时要用它，不能拿 seats 当分母。
    players_dealt: int = 0

    @property
    def opponents_in_hand(self) -> int:
        return sum(1 for o in self.opponents if not o.folded)

    def all_actions(self) -> list[ActionRecord]:
        return [a for s in self.streets for a in s.actions]

    def actions_of(self, street: str) -> list[ActionRecord]:
        for s in self.streets:
            if s.street == street:
                return list(s.actions)
        return []


@dataclass
class _LiveHand:
    """可变的手牌工作副本。对外只通过 snapshot() 交出去。"""

    hand_id: str
    hand_index: int
    hero_cards: tuple[str, ...] = ()
    hero_position: str | None = None
    table_size: int = 0
    dealer: str | None = None
    blinds: Blinds | None = None
    # hero 这一手的**起始筹码**（翻前下注/前注之前）。不是「第一帧读到的后手」：
    # 开局前几帧还没渲染筹码时，用「当前后手 + 已投入」反推，否则整手的套池
    # 比例都会少算掉翻前已经投进去的那部分。
    start_stack: float | None = None
    # 各座起始筹码：seat_id → {seat_id, name, position, stack}（**不含 hero**，
    # hero 在 start_stack / hero_stack_bb）。翻前逐帧补齐，落库时写进 hand 记录，
    # 复跑控制台重建有效筹码时要用（缺了就只能按 unknown）。
    start_stacks: dict[str, dict] = field(default_factory=dict)
    board: tuple[str, ...] = ()
    street: str = PREFLOP
    # 这一手从哪条街开始观察。正常从翻前开始；中途接管（第一条街是
    # flop/turn/river）只输出观察到的街，不凭空补一个没见过的翻前。
    start_street: str = PREFLOP
    pot: float | None = None
    pot_before_street: float | None = None
    # 街 → 该街起始底池
    pot_start: dict[str, float | None] = field(default_factory=dict)
    actions: list[ActionRecord] = field(default_factory=list)
    # (street, seat_id) → 该座在这条街最后一次行动在 actions 里的下标
    last_index: dict[tuple[str, str], int] = field(default_factory=dict)
    # 街 → 当前最高下注（「本街加到总额」）。用来在徽章漏读时，从下注额变化
    # 反推 bet/raise/call
    level: dict[str, float] = field(default_factory=dict)
    # (street, seat_id) → 该座在这条街的「已知下注额」。盲注不是行动，所以基线
    # 必须按座位记，不能拿 0 当基线
    baseline: dict[tuple[str, str], float] = field(default_factory=dict)
    # 摊牌/主动秀牌时读到的对手手牌：seat_id → {seat_id, name, position, cards}
    shows: dict[str, dict] = field(default_factory=dict)
    # 全下汇总：seat_id → {seat_id, name, position, amount, equity}
    all_ins: dict[str, dict] = field(default_factory=dict)
    # 这一手遇到过的牌面异常（稳定闸判断牌面不可靠时）。落库用于复盘：
    # 哪些建议/行动是在牌面异常期间记录的。
    cards_warnings: list[str] = field(default_factory=list)
    cards_unreliable: bool = False
    # 本手看到过爆炸底池横幅。开局出现一次，之后一直保留到换手。
    bomb_pot: bool = False
    # hero 的座位 id（只用于落库时给 ProfileStore 锚定「谁不是对手」）
    hero_seat_id: str | None = None
    # 街 → 该街开始时仍在局（已发牌、未弃牌、未离桌）的座位。
    # VPIP/PFR 的分母必须按「机会」算：这些人在这条街都有行动机会，
    # 不能用「观察到的动作数」当分母（见 docs/对手类型识别-设计.md 4.2）。
    street_start_active: dict[str, set[str]] = field(default_factory=dict)
    # 当前仍在局的座位（每帧更新）。街末和手末用它和 start_active 的差集
    # 补出视频流漏掉的弃牌。
    active: set[str] = field(default_factory=set)
    # 本街曾经在局过的座位（含中途弃牌的人）。补过牌时只看「街末还在」
    # 的人，所以这个集合只作审计用途。
    street_seen: dict[str, set[str]] = field(default_factory=dict)
    # 已经做过补全的街，避免 finish_current/take_finished 重复补
    finalized_streets: set[str] = field(default_factory=set)

    def street_actions(self, street: str) -> list[ActionRecord]:
        return [a for a in self.actions if a.street == street]

    def snapshot(self) -> tuple[StreetRecord, ...]:
        """从 start_street 到当前街，**每一街都输出**（空街也留一条）。

        旧写法只输出「有动作或就是当前街」的街：发牌动画/遮挡导致翻牌或转牌
        一帧都没接住时，记录会从 preflop 直接跳到 river，看起来像缺了一整条街。
        现在空街也留 `pot_start`（可能为 None）和空 actions，复盘时能明确看出
        「这一街确实打了，只是没抓到动作」，而不是凭空消失。
        """
        out: list[StreetRecord] = []
        start = STREET_SEQ.get(self.start_street, 0)
        end = STREET_SEQ.get(self.street, 0)
        for street in (PREFLOP, FLOP, TURN, RIVER):
            i = STREET_SEQ[street]
            if i < start or i > end:
                continue
            acts = tuple(a for a in self.actions if a.street == street)
            out.append(StreetRecord(street, self.pot_start.get(street), acts))
        return tuple(out)


class LiveHandTracker:
    """逐帧喂状态，累积成一手牌；轮到我时返回一个决策点快照。

    用法（单线程，LiveProcessor 主循环里）：

        point = tracker.observe(state, hero_codes, board_codes)
        if point is not None:
            advisor.submit(point)
    """

    def __init__(self, *, confirm_frames: int = 2) -> None:
        # 轮到我需要连续几帧确认。真机上预设按钮会在回合内闪断，单帧不够
        self.confirm_frames = max(1, int(confirm_frames))
        self.hand: _LiveHand | None = None
        self.hand_seq = 0
        self.turn_frames = 0
        # 已经建议过的决策点。同一个回合里按钮闪断会重复给出同一个 key
        self.advised: set[tuple] = set()
        # 上一帧的 hero 手牌 / 在局人数，用来判换手（后者不是桌型座位数）
        self._prev_hero_cards: tuple[str, ...] = ()
        self._prev_in_hand = 0
        # 公共牌掉回翻前/翻牌的确认计数。单帧读空可能只是遮挡，连续两帧才算换手
        self._drop_frames = 0
        # 上一手结束时的工作副本。调用方 start_hand 时取走，落成一条 hand 记录
        self.last_finished: _LiveHand | None = None
        # 最近一次有效状态里各座的名字/位置。亮牌帧经常因为筹码被顶掉而整帧
        # 丢弃，但亮牌本身不依赖状态；用这份缓存给 show 记录补名字/位置。
        self._seat_meta: dict[str, tuple[str | None, str | None]] = {}
        # seat_id → 首次看到这组亮牌时的手牌 id。亮牌会在屏幕上停留好几秒，
        # 跨过换手；没有这份映射会把上一手的亮牌也写进下一手。
        self._active_shows: dict[str, str] = {}
        # `finish_current()` 主动结束当前手后，等真正的新手信号再开新手。
        # 否则摊牌后的同一帧会被当成新手牌开头，把一手牌劈成两半。
        self._await_new_hand = False
        self._await_frames = 0
        # 等待期间保留一份结束手牌的副本：`take_finished()` 会把 last_finished
        # 清空，不能再用它判断「下一帧是不是同一手」。
        self._await_hand: _LiveHand | None = None
        # seat_id → 连续读到 all_in 的帧数。够 ALL_IN_CONFIRM_FRAMES 才写记录。
        self._all_in_streak: dict[str, int] = {}

    # ─────────────────────────── 主流程 ───────────────────────────

    def observe(self, state: FrameState,
                hero_cards: list[str] | tuple[str, ...],
                board: list[str] | tuple[str, ...], *,
                cards_reliable: bool = True,
                cards_warnings: tuple[str, ...] = ()
                ) -> HandSnapshot | None:
        """喂一帧被接受的状态；返回决策点快照或 None。

        调用方只应喂 `state.ok` 的帧 —— 带 errors 的帧里任何字段都可能是猜的。
        对手亮牌走独立的 `record_shows()`：亮牌帧常常因为筹码/底池被顶掉而
        `not ok`，不能等这里。

        `cards_reliable=False` 时**不给决策点**（也不把决策点标成已建议）：
        牌面张数/点数不稳定时，AI 基于错牌面给的结论比没有结论更糟。等牌面
        恢复可靠后，同一个决策点还会再尝试一次。
        """
        if not state.ok:
            return None

        for p in state.players:
            if p.seat_id != state.hero_seat_id:
                self._seat_meta[p.seat_id] = (p.name, p.position)

        cards = tuple(hero_cards)
        board = tuple(board)
        board_len = len(board)
        street = street_of_board(board_len)

        if self._await_new_hand:
            self._await_frames += 1
            if (not self._looks_like_new_hand(state, cards, board_len)
                    and self._await_frames < AWAIT_NEW_HAND_MAX_FRAMES):
                return None
            self._await_new_hand = False
            self._await_hand = None
            self._await_frames = 0

        if self._is_new_hand(state, cards, board_len):
            if self.hand is not None:
                # 正常换手（不是 finish_current 主动收手）也要补全最后一街：
                # 弃牌徽章漏读时，最后几帧的 active 差集就是全部证据。
                self._finalize_street(self.hand, self.hand.street, None,
                                      at_hand_end=True)
                self.last_finished = copy.deepcopy(self.hand)
            self._start_hand(state, cards, board_len, board)

        hand = self.hand
        assert hand is not None
        hand.hero_seat_id = state.hero_seat_id or hand.hero_seat_id

        # 这一帧仍在局的人。弃牌徽章漏读时，街末就是靠它和街初快照的差集
        # 补出弃牌；不只看 action=="fold"（实测大量弃牌徽章根本没读到）。
        current_active = self._active_seats(state)

        # 手牌/位置在遮挡帧里可能读空，只更新有值的
        if cards:
            hand.hero_cards = cards
        # 这一手遇到过的牌面异常。只留真正值得复盘的原因（回退/牌背/遮挡/
        # 张数不合法），跳过动画期间每帧的「新读数待确认」，并限 20 条。
        for warning in cards_warnings:
            if not any(k in warning for k in _CARD_WARNING_KEYS):
                continue
            if warning not in hand.cards_warnings and len(hand.cards_warnings) < 20:
                hand.cards_warnings.append(warning)
        hero = self._hero(state)
        if hero is not None and hero.position:
            hand.hero_position = hero.position
        # 桌型 = 座位数。不能再拿 players_in_hand：弃牌会让 9 人桌越打越小，
        # 提示词里就会写成「2-handed」，误导模型。
        hand.table_size = state.table_seats
        if state.bomb_pot:
            hand.bomb_pot = True
        hand.dealer = state.dealer or hand.dealer
        hand.blinds = state.blinds or hand.blinds
        hand.pot = state.pot
        hand.pot_before_street = state.pot_before_street

        # 街推进：公共牌只增不减；1/2 张是动画，不推进。
        # 4 张的读数不能覆盖已经读到的 5 张 —— 那是读错一张，不是新街
        if street is not None and street != hand.street and board_len >= 3:
            # 旧街已经结算：先用它的最终底池补上中间漏抓的跟注，再换街
            self._reconcile_street(hand, street, state)
            # 补弃牌/过牌必须在换街之前：start_active 是旧街开始时的在局集合，
            # current_active 是这一帧（新街）的在局集合，差集就是旧街上
            # 已经弃牌、但徽章一帧都没抓到的人。
            self._finalize_street(hand, hand.street, current_active, state=state)
            hand.street = street
            hand.pot_start.setdefault(street, state.pot_before_street)
            hand.street_start_active.setdefault(street, set(current_active))
            hand.street_seen.setdefault(street, set()).update(current_active)
            # **不能在这里按桌上的下注额重置基线**：那一刻的下注往往就是本街
            # 第一个动作（比如翻牌刚下的注），重置会把「他下注了」抹平。
            # 基线保持「上一街的下注额」，于是 0 → 2 仍然能被反推成下注。
            if board_len >= len(hand.board):
                hand.board = board
        elif board_len >= 3 and board_len >= len(hand.board):
            hand.board = board

        self._record_actions(state, hand)
        # 记录完本帧动作后再更新在局集合：本帧的弃牌徽章已经落成 fold，
        # 街末补弃牌时不会重复补。
        hand.active = set(current_active)
        hand.street_start_active.setdefault(hand.street, set(current_active))
        hand.street_seen.setdefault(hand.street, set()).update(current_active)
        # 起始筹码在翻前逐帧补齐：开场那几帧筹码可能还没渲染（或读成 0），
        # 等读到后手时用「后手 + 已投入」反推，保证整手的套池比例不漏算。
        # 放在街推进/行动记录之后：换街那一帧要按**新街**回看上一街的投入，
        # 否则会把翻前已经投进去的筹码漏掉。
        self._fill_start_stacks(state, hand)
        self._prev_hero_cards = cards or self._prev_hero_cards
        self._prev_in_hand = state.players_in_hand

        return self._maybe_decision(state, hand, cards_reliable=cards_reliable)

    def record_shows(self, shows: dict[str, list[str]] | None) -> None:
        """把本帧读到的亮牌挂到当前（或刚结束的）这手牌上。

        亮牌帧常常因为筹码/底池被牌顶掉而 `state.ok=False`，所以这个入口
        不要求有效状态。名字/位置用最近一次有效状态的缓存；同一手牌里重复
        读到同一座位时以后读到的为准。
        """
        shows = shows or {}
        # 本帧不再出现的亮牌：结束这次「亮牌事件」，下一手同样的牌可以重新记。
        for seat_id in list(self._active_shows):
            if seat_id not in shows:
                del self._active_shows[seat_id]
        if not shows:
            return
        target = self._target_hand()
        if target is None:
            return
        for seat_id, codes in shows.items():
            codes = tuple(codes)
            # 摊牌 2 张；河牌主动只秀 1 张。0 张/3 张以上都是误读。
            if len(codes) not in (1, 2):
                continue
            # 同一组亮牌从上一手一直停留到这一手时，不要再写进新手牌。
            first_hand = self._active_shows.get(seat_id)
            if first_hand is not None and first_hand != target.hand_id:
                continue
            self._active_shows[seat_id] = target.hand_id
            name, position = self._seat_meta.get(seat_id, (None, None))
            target.shows[seat_id] = {
                "seat_id": seat_id,
                "name": name,
                "position": position,
                "cards": list(codes),
            }

    def record_all_ins(self, state: FrameState | None) -> None:
        """把本帧的全下座位、金额和实时胜率挂到当前（或刚结束的）手牌上。

        亮牌/摊牌帧经常 `state.ok=False`，所以这个入口也不要求状态有效。
        金额优先用本街下注 pill（全下时它显示的是全下总额），读不到再退回
        筹码胶囊；胜率只有界面显示百分比时才有。
        """
        if state is None:
            return
        target = self._target_hand()
        if target is None:
            return
        current = {p.seat_id for p in state.players if p.in_hand and p.all_in}
        for seat_id in list(self._all_in_streak):
            if seat_id not in current:
                del self._all_in_streak[seat_id]
        for p in state.players:
            if not (p.in_hand and p.all_in):
                continue
            streak = self._all_in_streak.get(p.seat_id, 0) + 1
            self._all_in_streak[p.seat_id] = streak
            if streak < ALL_IN_CONFIRM_FRAMES:
                continue                 # 单帧误判（亮牌/牌背/遮挡）不落库
            prev = target.all_ins.get(p.seat_id, {})
            amount = (p.bet if (p.bet or 0) > 0 else
                      p.stack if (p.stack or 0) > 0 else None)
            name, position = p.name, p.position
            meta_name, meta_position = self._seat_meta.get(p.seat_id,
                                                          (None, None))
            name = name or meta_name
            position = position or meta_position
            if name is None or position is None or amount is None:
                # 全下帧常常 state.ok=False，name/position/amount 可能是空的。
                # 从这手已有的行动线补；比只依赖单帧更稳。
                for a in reversed(target.actions):
                    if a.seat_id != p.seat_id:
                        continue
                    if name is None and a.name:
                        name = a.name
                    if position is None and a.position:
                        position = a.position
                    if amount is None and a.all_in and (a.amount or 0) > 0:
                        amount = a.amount
            amount = amount if amount is not None else prev.get("amount")
            equity = p.equity if p.equity is not None else prev.get("equity")
            target.all_ins[p.seat_id] = {
                "seat_id": p.seat_id,
                "name": name,
                "position": position,
                "amount": amount,
                **({"equity": equity} if equity is not None else {}),
            }

    def _target_hand(self) -> _LiveHand | None:
        """亮牌/全下信息该挂到哪一手。

        当前手牌还是换手后的空壳（没有 hero 手牌/公共牌）时，信息更可能
        属于刚结束的上一手。
        """
        target = self.hand
        if (target is None or (not target.hero_cards and not target.board)) \
                and self.last_finished is not None:
            target = self.last_finished
        return target

    def finish_current(self) -> None:
        """主动结束当前手牌（All-in 徽章消失），等真正的新手信号再开新手。"""
        if self.hand is None:
            return
        # 手末再补一次：河牌打完/全下摊牌时没有「下一街」可以触发街末补全。
        self._finalize_street(self.hand, self.hand.street, None,
                              at_hand_end=True)
        hand = copy.deepcopy(self.hand)
        self.last_finished = hand
        self._await_hand = hand
        self.hand = None
        self._await_new_hand = True
        self._await_frames = 0
        self._all_in_streak.clear()

    def _looks_like_new_hand(self, state: FrameState,
                             cards: tuple[str, ...], board_len: int) -> bool:
        """`finish_current()` 之后判断当前帧是否真的是下一手。

        不直接用 `_is_new_hand()`：那个函数在 `self.hand is None` 时会立刻
        返回 True，会把摊牌后的同一帧当成新手牌开头。
        """
        hand = self._await_hand or self.last_finished
        if hand is None:
            return True
        if hand.dealer and state.dealer and hand.dealer != state.dealer:
            return True
        if (len(cards) == 2 and len(hand.hero_cards) == 2
                and cards != hand.hero_cards):
            return True
        if board_len == 0 and len(hand.board) > 0:
            return True
        if (self._prev_in_hand
                and abs(state.players_in_hand - self._prev_in_hand) >= IN_HAND_JUMP):
            return True
        return False

    def reset(self) -> None:
        """断流重连：桌面状态全部作废，但已给出的建议不重发。"""
        self.hand = None
        self.turn_frames = 0
        self._prev_hero_cards = ()
        self._prev_in_hand = 0
        self._drop_frames = 0
        self._seat_meta.clear()
        self._active_shows.clear()
        self._await_new_hand = False
        self._await_frames = 0
        self._await_hand = None
        self._all_in_streak.clear()

    # ─────────────────────────── 换手 ───────────────────────────

    def _is_new_hand(self, state: FrameState, cards: tuple[str, ...],
                     board_len: int) -> bool:
        hand = self.hand
        if hand is None:
            return True
        if hand.dealer and state.dealer and hand.dealer != state.dealer:
            return True
        # 公共牌读空（0 张）连续两帧才是换手。**不把 4→3 当换手**：
        # 转牌某一张被动画/遮挡读丢时，牌面会从 4 掉到 3；实测这会把同一手
        # 劈成两半，AI 建议留在前半段、all-in/亮牌落在后半段。
        # 新手如果从翻牌开始（没有 0 张帧），靠庄位/hero 手牌/在局人数跳变识别。
        # 确认期间手牌状态故意不推进，上一帧的 board_len 会提前变成 0，
        # 所以拿「这手牌见过的最长公共牌」比。
        seen = len(hand.board)
        if board_len == 0 and seen > 0:
            self._drop_frames += 1
            if self._drop_frames >= 2:
                self._drop_frames = 0
                return True
            # hero 手牌换了一组 = 新一手已经发牌，不用等第二帧
            if len(cards) == 2 and len(self._prev_hero_cards) == 2 \
                    and cards != self._prev_hero_cards:
                self._drop_frames = 0
                return True
            # 确认之前不改写手牌（牌面/街仍沿用上一帧）
            return False
        self._drop_frames = 0

        card_changed = (len(cards) == 2 and len(self._prev_hero_cards) == 2
                        and cards != self._prev_hero_cards)
        if card_changed:
            # 换手空档：当前手还没有 hero 手牌，这一组牌就是它自己的，
            # 直接补上，不另开一手（否则每手都会多一条被跳过的空档记录，
            # 而且空档里更早读到的起始筹码会被丢掉）。
            if not hand.hero_cards:
                return False
            # 已经打到翻后：同一手的牌面不会中途换两张，换牌只可能是 OCR
            # 纠正（实测 8s 会持续被读成 6s 好几秒，按换手会把一手劈成两半）。
            # 例外：底池比上一帧还小 = 真正的新一手已经开始了（中途接管）。
            if board_len >= 3:
                if (hand.pot is not None and state.pot is not None
                        and state.pot < hand.pot - 0.01):
                    return True
                return False
            # 翻前：只有底池还停在盲注/前注水位（没人入池）才像新手开局；
            # 已经有人加注后再换牌，按 OCR 纠正处理。
            return self._pot_is_initial(state, hand)
        # 在局人数一次变化 ≥2：旧口径直接判换手，资料弹窗让两个人短暂
        # in_hand=false 就会把一手牌劈成两半。现在只在「翻前 + 底池还是盲注
        # 水位」时才认；真正换桌/换手通常还有庄位变化兜底。
        if (self._prev_in_hand
                and abs(state.players_in_hand - self._prev_in_hand) >= IN_HAND_JUMP
                and board_len == 0
                and self._pot_is_initial(state, hand)):
            return True
        return False

    @staticmethod
    def _pot_is_initial(state: FrameState, hand: _LiveHand) -> bool:
        """底池是否还停在「盲注 + 前注」的初始水位（没人主动入池）。

        换手用的辅助判据：新手第一帧的底池是 sb+bb+ante×人数。资料弹窗/
        遮挡也会让人数跳变，但那时通常已经有人加注，底池明显更大。
        没有底池/盲注信息时返回 True —— 宁可多开一手，也不要吞掉真换手。
        """
        blinds = state.blinds or hand.blinds
        if blinds is None or state.pot is None:
            return True
        ante = blinds.ante or 0.0
        seats = state.table_seats or state.players_in_hand or 2
        initial = blinds.sb + blinds.bb + ante * max(seats, 2)
        return state.pot <= initial + 0.01

    def _start_hand(self, state: FrameState, cards: tuple[str, ...],
                    board_len: int, board: tuple[str, ...]) -> None:
        self.hand_seq += 1
        self._all_in_streak.clear()
        street = street_of_board(board_len) or PREFLOP
        hero = self._hero(state)
        hand = _LiveHand(
            hand_id=f"h{self.hand_seq}",
            hand_index=self.hand_seq,
            hero_cards=cards,
            hero_position=hero.position if hero else None,
            table_size=state.table_seats,
            dealer=state.dealer,
            blinds=state.blinds,
            # 起始筹码不在这里从单帧后手直接取（开场帧可能还没渲染筹码），
            # 交给 `_fill_start_stacks` 用「后手 + 已投入」逐帧补齐。
            start_stack=None,
            board=board if board_len >= 3 else tuple(),
            street=street,
            start_street=street,
            pot=state.pot,
            pot_before_street=state.pot_before_street,
            bomb_pot=state.bomb_pot,
            hero_seat_id=state.hero_seat_id or None,
            active=self._active_seats(state),
            street_start_active={street: self._active_seats(state)},
            street_seen={street: self._active_seats(state)},
        )
        hand.pot_start[street] = state.pot_before_street
        # 基线：接管链路时桌上可能已经有下注/加注，先把它当当前水位
        hand.level[street] = max((p.bet or 0.0) for p in state.players)
        for p in state.players:
            hand.baseline[(street, p.seat_id)] = p.bet or 0.0
        self.hand = hand
        self.turn_frames = 0

    # ─────────────────────── 起始筹码 ───────────────────────

    def _fill_start_stacks(self, state: FrameState, hand: _LiveHand) -> None:
        """把这一手各座的**起始筹码**补全（每帧调用，只补缺、不改写）。

        为什么不能只在开局那一帧取：发牌动画的前几帧筹码胶囊可能还没渲染，
        OCR 快的时候恰恰最容易抓到这些帧；只取一次会让 hero 的起始筹码空着，
        整手都算不出套池比例。这里允许晚几帧补，并把「这期间已经投进去的
        筹码」加回去，得到真正的起始值。

        `stack <= 0` 一律不当起始筹码：翻前还没下注就 0，只可能是误读
        （实测筹码图标会被读成裸 `0`）或者人已经不在这一手；真正的全下
        在起始筹码已经抓到之后才发生，不需要在这里补。
        """
        hero = self._hero(state)
        # hero 只在「这一手确实在局」时才取：换手空档里 hero 可能还没被发牌，
        # 那几帧的后手属于上一手，不能当新手的起始筹码。
        if hero is not None and hero.in_hand and hand.start_stack is None:
            hand.start_stack = self._opening_stack(state, hand, hero)
        for p in state.players:
            entry = hand.start_stacks.get(p.seat_id)
            if entry is not None:
                # 起始筹码抓到的那一帧名字/位置可能还空着（发牌动画），补上
                if entry.get("name") is None and p.name:
                    entry["name"] = p.name
                if entry.get("position") is None and p.position:
                    entry["position"] = p.position
                continue
            if not p.in_hand or p.seat_id == state.hero_seat_id:
                continue
            value = self._opening_stack(state, hand, p)
            if value is None:
                continue
            hand.start_stacks[p.seat_id] = {
                "seat_id": p.seat_id,
                "name": p.name,
                "position": p.position,
                "stack": value,
            }

    def _opening_stack(self, state: FrameState, hand: _LiveHand,
                       p: PlayerState) -> float | None:
        """当前后手 + 这一手已经投入的筹码 = 起始筹码（BB）。"""
        if p.stack is None or p.stack <= 0:
            return None
        return round(p.stack + self._posted_so_far(state, hand, p), 2)

    def _posted_so_far(self, state: FrameState, hand: _LiveHand,
                       p: PlayerState) -> float:
        """这个座位这一手到目前为止已经投进底池的筹码（BB，含前注）。

        · 本街用下注 pill（`p.bet`）：它天然包含盲注和本街已加到的总额；
        · 已结束的街用**该座最后一次行动金额**（口径是「本街加到总额」）；
          翻前还要和盲注取大 —— 大盲过牌时行动金额是 None，但那 1BB 确实
          已经投进去了。
        · 前注按人收，每手一次，单独加。
        """
        ante = hand.blinds.ante if hand.blinds and hand.blinds.ante else 0.0
        total = ante + (p.bet or 0.0)
        for street in (PREFLOP, FLOP, TURN, RIVER):
            if street == hand.street:
                break
            last: float | None = None
            for a in hand.actions:
                if (a.street == street and a.seat_id == p.seat_id
                        and a.amount is not None):
                    last = a.amount
            if street == PREFLOP and hand.blinds is not None:
                blind = {"SB": hand.blinds.sb,
                         "BB": hand.blinds.bb}.get(p.position or "", 0.0)
                last = max(last or 0.0, blind)
            total += last or 0.0
        return total

    # ─────────────────────────── 行动 ───────────────────────────

    def _record_actions(self, state: FrameState, hand: _LiveHand) -> None:
        """把这一帧里每座的徽章翻成行动记录，并去掉重复帧。

        动作徽章会在名字槽停留好几秒，期间别人可能已经行动过。所以去重不能只看
        「它是不是整条序列的最后一条」（那会把夹在别人行动之后复现的旧徽章
        当成新动作），要看**这个座自己**上一次被记录的动作：

          · 动作类型不同 → 一定是一次新行动（过牌后面对下注跟注）。
          · 类型相同、本街下注额没变 → 同一个徽章的重复帧，丢掉。
          · 类型相同、下注额变大 → 要么是补读，要么是他真的又行动了一次
            （跟注后被加注、再跟一次）。判据是「中间有没有别人把下注额抬高」。
        """
        hero = self._hero(state)
        hero_seat = hero.seat_id if hero else state.hero_seat_id
        level = hand.level.get(hand.street, 0.0)

        # 按本街下注额升序处理：同一帧里读到的多个行动，下注额小的多半先发生
        # （跟注额 = 当前水位，加注额更高）。额相同则先处理带激进徽章的
        def order(p):
            aggressive = 0 if p.action in AGGRESSIVE else 1
            return (p.bet or 0.0, aggressive)

        for p in sorted(state.players, key=order):
            # 不在这一手的人不可能有行动（空座/带入中/暂离）；弃牌是例外 ——
            # 弃牌的人 `in_hand` 仍为 True（status=folded），但徽章可能先消失
            if not p.in_hand and p.action != "fold":
                continue
            if p.action is not None and p.action not in ACTION_TYPES:
                continue

            key = (hand.street, p.seat_id)
            last = hand.last_index.get(key)
            prev = hand.actions[last] if last is not None else None
            bet = p.bet or 0.0
            prev_amount = (prev.amount or 0.0) if prev is not None else 0.0

            action = p.action
            if action is None:
                # 徽章漏读时从下注额变化反推。没变化 = 没有行动
                known = hand.baseline.get(key, 0.0)
                if bet <= known + 1e-9:
                    continue
                if bet > level + 1e-9:
                    # 翻前的第一个激进动作也记 raise（盲注不是「下注」）
                    action = "bet" if (hand.street != PREFLOP and level <= 1e-9) \
                        else "raise"
                else:
                    action = "call"

            if prev is not None and prev.action == action:
                if bet <= prev_amount + 1e-9:
                    continue                 # 同一个徽章的重复帧
                raised_since = any(
                    a.seat_id != p.seat_id and a.action in AGGRESSIVE
                    and (a.amount or 0.0) > prev_amount + 1e-9
                    for a in hand.actions[last + 1:])
                if not raised_since:
                    # 只是下注 pill 后更新了（徽章先出现、金额后到）
                    hand.actions[last] = ActionRecord(
                        street=prev.street, seat_id=prev.seat_id, name=prev.name,
                        position=prev.position, action=prev.action,
                        amount=p.bet, all_in=prev.all_in or p.all_in,
                        is_hero=prev.is_hero)
                    hand.baseline[key] = bet
                    continue

            record = ActionRecord(
                street=hand.street,
                seat_id=p.seat_id,
                name=p.name,
                position=p.position,
                action=action,
                amount=p.bet,
                all_in=p.all_in,
                is_hero=p.seat_id == hero_seat,
            )
            hand.actions.append(record)
            hand.last_index[key] = len(hand.actions) - 1
            hand.baseline[key] = bet
            if action in AGGRESSIVE and bet > level:
                level = bet

        hand.level[hand.street] = level

    # ─────────────────────────── 街末对账 ───────────────────────────

    def _reconcile_street(self, hand: _LiveHand, new_street: str,
                          state: FrameState) -> None:
        """换街时用底池账单补齐上一街漏抓的动作。

        下一街的 `pot_before_street` = 上一街结算后的底池，所以

            上一街实际投入 = pbs(下一街) − pot_start(上一街)

        行动线里能算出来的投入是每座「盲注基线 / 已记录动作金额」之和
        （金额都是「本街加到总额」，同一座取最大值）。两者相减就是没抓到的钱。

        先试最保守的**只补跟注**：差额正好等于「还在池里的人补齐到当前水位」。
        再试**单次加注**：把弃牌者已记录的投入扣掉后，用剩余人数反解最终水位。
        典型场景都是最后位置预设了动作，视频流没抓到那一帧就换街（下注 pill
        已被收走）。加注者身份用行动顺序取「最后一个已记录动作之后最早能行动的
        人」—— 金额是底池反解出来的精确值，但谁加注无法从底池唯一确定，所以
        落库和提示词里都标 `inferred`。
        """
        street = hand.street
        # 只接受往后推进。公共牌 5→3 的单帧误读会走到这里，不能倒着补
        if STREET_SEQ.get(new_street, -1) <= STREET_SEQ.get(street, -1):
            return
        start = hand.pot_start.get(street)
        end = state.pot_before_street
        if start is None or end is None:
            return
        actual = round(end - start, 2)
        level = hand.level.get(street, 0.0)
        if actual <= 1e-9 or level <= 1e-9:
            return

        # 已经记进账的投入：盲注基线 + 每条动作的「本街加到总额」
        known: dict[str, float] = {}
        for (s, seat), amount in hand.baseline.items():
            if s == street:
                known[seat] = max(known.get(seat, 0.0), amount or 0.0)
        for a in hand.street_actions(street):
            known[a.seat_id] = max(known.get(a.seat_id, 0.0), a.amount or 0.0)
        missing = round(actual - sum(known.values()), 2)
        if missing <= 1e-9:
            return

        # 还在池里的人，理论上街末都应该补齐到最终水位。弃牌/离桌的人不用补；
        # 全下的人可能只跟到更少的钱，不当作漏抓（否则金额会猜错）
        remaining = [p for p in state.players
                     if p.in_hand and p.fold is not True
                     and p.status != "folded" and not p.all_in]
        if not remaining:
            return

        # ① 只补跟注：所有人补齐到已经记录到的当前水位
        owed = sorted(
            ((p, round(level - known.get(p.seat_id, 0.0), 2))
             for p in remaining),
            key=lambda item: self._action_rank(item[0]))
        owed = [(p, d) for p, d in owed if d > 1e-9]
        if owed and abs(sum(d for _, d in owed) - missing) <= RECONCILE_TOLERANCE:
            self._append_inferred(hand, street, state,
                                  [(p, "call", level) for p, _ in owed])
            return

        # ② 单次加注：底池反解最终水位，再按行动顺序补加注 + 跟注
        self._infer_single_raise(hand, street, state, actual, known,
                                 remaining, level)

    def _infer_single_raise(self, hand: _LiveHand, street: str,
                            state: FrameState, actual: float,
                            known: dict[str, float],
                            remaining: list[PlayerState], level: float) -> None:
        """反解「一次加注后所有人跟注」的漏抓线路。

        底池只能给出**金额**，给不出「谁加注」：SB bet 22 → hero raise 66 →
        BTN call → SB call，和 SB bet 22 → hero call → BTN raise 66 → SB call →
        hero call，底池与每人最终投入完全一样。这里取最早能行动的人当加注者，
        让补出来的序列自洽；身份是推断，金额是精确值。
        """
        if level <= 1e-9:
            return
        acts = hand.street_actions(street)
        aggressive = [i for i, a in enumerate(acts) if a.action in AGGRESSIVE]
        if not aggressive:
            return
        # level 是记录到的最高水位；锚点取最后一个达到这个水位的激进动作
        anchor_i = aggressive[-1]
        for i in reversed(aggressive):
            if (acts[i].amount or 0.0) >= level - RECONCILE_TOLERANCE:
                anchor_i = i
                break

        # 锚点（最后一次已记录的下注/加注）之后，只允许有仍在旧水位上的动作。
        # 如果已经有「投入高于旧水位」的记录，加注发生在它之前，补出来的记录
        # 没法插到正确位置 —— 不猜。
        lower = acts[anchor_i]
        for a in acts[anchor_i + 1:]:
            if (a.amount or 0.0) > level + RECONCILE_TOLERANCE:
                return
            lower = a
        lower_rank = self._rank_or_none(lower.position)
        if lower_rank is None:
            return

        # 非剩余座位（已弃牌/全下/离桌）的投入已记录在 known 里，先从账单扣掉；
        # 剩下的人每人最终都投到 final
        ids = {p.seat_id for p in remaining}
        other_known = sum(v for seat, v in known.items() if seat not in ids)
        n = len(remaining)
        final = round((actual - other_known) / n, 2)
        if final <= level + RECONCILE_TOLERANCE:
            return
        # 投入都是整 chip（1/2 盲注下 0.5BB 一格）。反解出 73.33 这种数，
        # 多半是有弃牌者的投入没记到，不能当成一个合法加注
        if abs(final - round(final * 2) / 2) > RECONCILE_TOLERANCE:
            return
        if abs(n * final + other_known - actual) > RECONCILE_TOLERANCE:
            return
        if any(known.get(p.seat_id, 0.0) > final + RECONCILE_TOLERANCE
               for p in remaining):
            return

        # 加注者 = 最后一个已记录动作之后、还差钱的人里最早能行动的那个
        candidates = [p for p in remaining
                      if known.get(p.seat_id, 0.0) < final - RECONCILE_TOLERANCE]

        def after_key(p: PlayerState) -> int:
            distance = self._after_rank(lower_rank, p)
            return len(ACTION_ORDER) + 1 if distance is None else distance

        candidates.sort(key=after_key)
        if not candidates:
            return
        raiser = candidates[0]
        if self._after_rank(lower_rank, raiser) is None:
            return

        ring = self._ring_from(raiser, remaining)
        if not ring or ring[0] is not raiser:
            return
        items = [(raiser, "raise", final)]
        items += [(p, "call", final) for p in ring[1:]
                  if known.get(p.seat_id, 0.0) < final - RECONCILE_TOLERANCE]
        self._append_inferred(hand, street, state, items)

    def _append_inferred(self, hand: _LiveHand, street: str,
                         state: FrameState,
                         items: list[tuple[PlayerState, str, float]]) -> None:
        """把补出来的动作按发生顺序追加到这一街，并标 inferred。"""
        for p, action, amount in items:
            hand.actions.append(ActionRecord(
                street=street, seat_id=p.seat_id, name=p.name,
                position=p.position, action=action, amount=amount,
                all_in=p.all_in, is_hero=p.seat_id == state.hero_seat_id,
                inferred=True))
            hand.last_index[(street, p.seat_id)] = len(hand.actions) - 1
            hand.baseline[(street, p.seat_id)] = amount

    # ─────────────────────── 弃牌 / 过牌补全 ───────────────────────

    @staticmethod
    def _active_seats(state: FrameState) -> set[str]:
        """这一帧仍在局（已发牌、未弃牌、未离桌）的座位。

        弃牌后 `in_hand` 仍然是 True（status=folded），所以不能只看 in_hand。
        空座/带入中/暂离的人 `status` 不是 playing，会被排除。
        """
        return {p.seat_id for p in state.players
                if p.in_hand and p.fold is not True
                and p.status not in ("folded", "buying_in", "away",
                                     "sitting_out")}

    def _seat_info(self, hand: _LiveHand, seat_id: str,
                   state: FrameState | None = None) -> tuple[str | None,
                                                              str | None]:
        """补记录时给座位找名字/位置：当前帧 > 座位缓存 > 起始筹码 > 行动线。"""
        if state is not None:
            for p in state.players:
                if p.seat_id == seat_id:
                    if p.name or p.position:
                        return p.name, p.position
        name, position = self._seat_meta.get(seat_id, (None, None))
        if name or position:
            return name, position
        entry = hand.start_stacks.get(seat_id)
        if entry:
            return entry.get("name"), entry.get("position")
        for a in reversed(hand.actions):
            if a.seat_id == seat_id and (a.name or a.position):
                return a.name, a.position
        shown = hand.shows.get(seat_id)
        if shown:
            return shown.get("name"), shown.get("position")
        return None, None

    def _finalize_street(self, hand: _LiveHand, street: str,
                         current_active: set[str] | None,
                         *, state: FrameState | None = None,
                         at_hand_end: bool = False) -> None:
        """街末/手末补全：漏读的弃牌 + 已经轮过且没动作的过牌。

        视频流里弃牌徽章经常整条街都抓不到；但「街开始时在局、街末已经不在局」
        是可靠的结构证据。过牌更保守：只有这一街**没有任何 bet/raise** 时才补，
        因为一旦有人下注，没记录到动作的人更可能是跟注（金额对账会先处理），
        把它写成 check 会污染行动线。
        """
        if street in hand.finalized_streets:
            return
        start = set(hand.street_start_active.get(street) or ())
        if not start:
            return
        acts = hand.street_actions(street)
        # 爆炸底池的「翻前」只是每人强制交 5BB 前注，没有行动。这里不能补
        # check/fold：那些 check 会变成一次假的「翻前机会」，把 VPIP 分母
        # 拉大、把被动入池算成弃牌。有真实 call/raise 时才按正常街处理。
        bomb_ante = bool(hand.blinds and
                         (hand.blinds.ante or 0.0) >= BOMB_ANTE_BB)
        if ((hand.bomb_pot or bomb_ante) and street == PREFLOP
                and not any(not a.inferred for a in acts)):
            hand.finalized_streets.add(street)
            return
        current = set(hand.active if current_active is None else current_active)
        recorded_folds = {a.seat_id for a in acts if a.action == "fold"}

        # ① 弃牌：街初在局、街末不在局、又没有 fold 记录的座位。
        for seat_id in sorted(start - current - recorded_folds):
            name, position = self._seat_info(hand, seat_id, state)
            hand.actions.append(ActionRecord(
                street=street, seat_id=seat_id, name=name, position=position,
                action="fold", is_hero=seat_id == hand.hero_seat_id,
                inferred=True))
            hand.last_index[(street, seat_id)] = len(hand.actions) - 1

        acts = hand.street_actions(street)
        acted = {a.seat_id for a in acts}
        all_in = set(hand.all_ins) | {a.seat_id for a in acts if a.all_in}
        aggressive = any(a.action in AGGRESSIVE for a in acts)

        # ② 过牌：街末仍在局、这一街完全没动作、没有全下，且没有人下注。
        # 手末的翻前不补：所有人弃牌到 BB 时，BB 是直接收池，不是「过牌」。
        if not aggressive and not (at_hand_end and street == PREFLOP):
            for seat_id in sorted((start & current) - acted - all_in):
                name, position = self._seat_info(hand, seat_id, state)
                hand.actions.append(ActionRecord(
                    street=street, seat_id=seat_id, name=name,
                    position=position, action="check",
                    is_hero=seat_id == hand.hero_seat_id, inferred=True))
                hand.last_index[(street, seat_id)] = len(hand.actions) - 1
        hand.finalized_streets.add(street)

    @staticmethod
    def _action_rank(p: PlayerState) -> int:
        """按行动顺序排序；位置读不到时保持 state.players 顺序。"""
        try:
            return ACTION_ORDER.index(p.position)
        except (TypeError, ValueError):
            return len(ACTION_ORDER)

    @staticmethod
    def _rank_or_none(position: str | None) -> int | None:
        try:
            return ACTION_ORDER.index(position)
        except (TypeError, ValueError):
            return None

    def _after_rank(self, from_rank: int, p: PlayerState) -> int | None:
        """从 from_rank 之后绕到 p 还要经过几个位置；未知位置返回 None。"""
        rank = self._rank_or_none(p.position)
        if rank is None:
            return None
        distance = (rank - from_rank) % len(ACTION_ORDER)
        return distance or len(ACTION_ORDER)

    def _ring_from(self, first: PlayerState,
                   players: list[PlayerState]) -> list[PlayerState] | None:
        """按行动顺序从 first 开始绕一圈（first 排第一）。"""
        first_rank = self._rank_or_none(first.position)
        if first_rank is None:
            return None

        def key(p: PlayerState) -> int:
            rank = self._rank_or_none(p.position)
            return (len(ACTION_ORDER) + 1 if rank is None
                    else (rank - first_rank) % len(ACTION_ORDER))

        return sorted(players, key=key)

    # ─────────────────────────── 决策点 ───────────────────────────

    def _maybe_decision(self, state: FrameState,
                        hand: _LiveHand, *,
                        cards_reliable: bool = True) -> HandSnapshot | None:
        """轮到我时给一个决策点。去重靠 decision_key，不靠上升沿。

        为什么不是「只在 hero_turn 由假变真的那一帧」：多人池里 hero 过牌后
        对手下注、又轮回 hero，预设按钮可能全程都在、中间没有假帧 —— 上升沿会
        把第二次决策点整个吞掉。而 decision_key 里含行动条数与最高下注，
        同一个回合内的按钮闪断反而是重复的 key，正好被它挡掉。
        """
        if state.hero_turn is not True:
            self.turn_frames = 0
            return None

        self.turn_frames += 1
        if self.turn_frames < self.confirm_frames:
            return None
        # 不知道自己的手牌就不该给动作建议；按钮还在，等读到牌的那一帧再给
        if not hand.hero_cards:
            return None
        if hand.street != PREFLOP and len(hand.board) < 3:
            return None
        # 牌面还没稳定/出现 ?/低置信度/牌背：不给建议，也不占用 advised 名额。
        # 这样牌面恢复后同一个决策点还能补投一次。只有真正拦下一次「本来
        # 会给出的建议」时才标 cards_unreliable，避免把发牌动画的正常等待
        # 也写进每一手牌。
        if not cards_reliable:
            hand.cards_unreliable = True
            return None

        snap = self._snapshot(state, hand)
        if snap.decision_key in self.advised:
            return None
        self.advised.add(snap.decision_key)
        return snap

    def _snapshot(self, state: FrameState, hand: _LiveHand) -> HandSnapshot:
        hero = self._hero(state)
        hero_seat = hero.seat_id if hero else state.hero_seat_id
        hero_bet = (hero.bet or 0.0) if hero else 0.0

        bets = sorted(
            (p.bet for p in state.players if p.bet is not None and p.in_hand),
            reverse=True)
        max_bet = bets[0] if bets else 0.0
        second = bets[1] if len(bets) > 1 else None
        to_call = max(max_bet - hero_bet, 0.0)

        opponents = tuple(
            OpponentSnapshot(seat_id=p.seat_id, name=p.name, position=p.position,
                             stack=p.stack, bet=p.bet,
                             folded=(p.fold is True or p.status == "folded"))
            for p in state.players
            if p.seat_id != hero_seat and (p.in_hand or p.status == "folded")
        )

        # 炸弹池前注是每个参与者在翻前交的；本街弃牌的人也算。
        # 空座（status=away / buying_in）不算，所以不能用 table_seats。
        players_dealt = sum(
            1 for p in state.players if p.in_hand or p.status == "folded")

        invested = None
        if hand.start_stack is not None and hero is not None and hero.stack is not None:
            invested = max(hand.start_stack - hero.stack, 0.0)

        shapes = self._preflop_shapes(hand, max_bet, hero_seat)
        pfr = self._hero_is_pfr(hand, hero_seat)
        # 持续下注点：我是翻前加注者、翻牌轮到我、且没有人下注
        cbet = (hand.street == FLOP and pfr and to_call == 0
                and (hero_bet or 0) == 0)

        warnings: list[str] = []
        if hand.start_stack is None:
            warnings.append("hero opening stack unread, pot-commitment ratio unknown")
        if not hand.hero_cards:
            warnings.append("hero hole cards unread")
        if hand.street != PREFLOP and len(hand.board) < 3:
            warnings.append("board incomplete")

        # 同一条街、同样的行动序列与下注额 = 同一个决策点
        key = (hand.hand_id, hand.street, len(hand.actions),
               _r(state.pot), _r(max_bet), tuple(hand.hero_cards))

        return HandSnapshot(
            hand_id=hand.hand_id,
            hand_index=hand.hand_index,
            street=hand.street,
            board=hand.board,
            hero_cards=hand.hero_cards,
            hero_position=hand.hero_position,
            hero_stack=hero.stack if hero else None,
            hero_bet=hero_bet,
            hero_invested=invested,
            to_call=to_call,
            second_bet=second,
            pot=state.pot,
            pot_before_street=state.pot_before_street,
            table_size=state.table_seats,
            blinds=state.blinds or hand.blinds,
            opponents=opponents,
            streets=hand.snapshot(),
            preflop_shapes=shapes,
            hero_is_pfr=pfr,
            cbet_spot=cbet,
            decision_key=key,
            warnings=tuple(warnings),
            bomb_pot=hand.bomb_pot,
            players_dealt=players_dealt,
        )

    def _preflop_shapes(self, hand: _LiveHand, max_bet: float,
                        hero_seat: str) -> tuple[str, ...]:
        """到我这一动为止，翻前已经出现过几次加注。

        快照是在**轮到我**时取的，所以已记录的行动全都发生在我这一动之前 ——
        不需要在 hero 自己的行动处截断。hero 开池后被 3bet 时，两次加注都在
        记录里，形态会正确落到 three-bet。

        没人加注时还要区分「没人入池」和「已经有人溜入」：前者才是
        preflop-open 的「第一个入池必须加注」；溜入池里平跟（over-limp）是
        正常线路，尤其是小对子抽 set。

        一次翻前行动都没记到（中途接管链路）时用盲注水位兜底：有人投到 1BB
        以上说明已经有人加注。宁可把一个 3bet 局面判成 single-raise，
        也不要因为「没记录」把有人加注的局面判成无人加注。
        """
        raises = sum(
            1 for a in hand.actions
            if a.street == PREFLOP
            and (a.action in ("raise", "bet") or (a.action == "call" and a.all_in))
        )
        if raises == 0 and not hand.street_actions(PREFLOP):
            bb = hand.blinds.bb if hand.blinds else 1.0
            if max_bet > bb + 1e-9:
                raises = 1

        if raises == 0:
            limped = any(a.street == PREFLOP and a.action == "call"
                         and not a.is_hero for a in hand.actions)
            return ("limped",) if limped else ("unopened",)
        if raises == 1:
            return ("single-raise",)
        return ("three-bet",)

    def _hero_is_pfr(self, hand: _LiveHand, hero_seat: str) -> bool:
        last: str | None = None
        for a in hand.actions:
            if a.street == PREFLOP and (a.action in ("raise", "bet")
                                        or (a.action == "call" and a.all_in)):
                last = a.seat_id
        return last is not None and last == hero_seat

    # ─────────────────────────── 小工具 ───────────────────────────

    @staticmethod
    def _hero(state: FrameState) -> PlayerState | None:
        for p in state.players:
            if p.seat_id == state.hero_seat_id:
                return p
        return None

    def take_current(self) -> dict | None:
        """取走当前这手牌（会话结束时用：最后一手没等到换手，也要落库）。"""
        hand = self.hand
        self.hand = None
        return self._hand_dict(hand) if hand is not None else None

    def take_finished(self) -> dict | None:
        """取走上一条已结束手牌的完整行动线（换手时才有）。"""
        hand = self.last_finished
        self.last_finished = None
        return self._hand_dict(hand) if hand is not None else None

    def _hand_dict(self, hand: _LiveHand) -> dict:
        out = {
            "hand_id": hand.hand_id,
            "hero_cards": list(hand.hero_cards),
            "hero_position": hand.hero_position,
            "table_size": hand.table_size,
            "blinds": hand.blinds.to_debug() if hand.blinds else None,
            "board": list(hand.board),
            "pot": hand.pot,
            "streets": [
                {
                    "street": rec.street,
                    "pot_start": rec.pot_start,
                    "actions": [
                        {"seat_id": a.seat_id, "name": a.name,
                         "position": a.position, "action": a.action,
                         "amount": a.amount, "all_in": a.all_in,
                         "is_hero": a.is_hero,
                         # 只在补出来时写这个键：正常手牌的落库内容保持原样
                         **({"inferred": True} if a.inferred else {})}
                        for a in rec.actions
                    ],
                }
                for rec in hand.snapshot()
            ],
        }
        if hand.hero_seat_id:
            out["hero_seat_id"] = hand.hero_seat_id
        # 每街开始时的在局座位。对手画像的「机会」分母从这里来；旧记录没有
        # 这个键，ProfileStore 会退回 start_stacks，再不行就跳过该手的 VPIP。
        if hand.street_start_active:
            out["street_active"] = {
                street: sorted(seats)
                for street, seats in hand.street_start_active.items()
                if seats
            }
            first = hand.street_start_active.get(hand.start_street)
            if first:
                out["players_dealt"] = len(first)
        # 座位元数据（名字/位置/起始筹码），只给确实在局的人。名字是画像键的
        # 来源；没有名字的座位由 ProfileStore 用 temp key 兜底。
        seat_meta: dict[str, dict] = {}
        for entry in hand.start_stacks.values():
            seat_meta[entry["seat_id"]] = {
                "seat_id": entry["seat_id"],
                "name": entry.get("name"),
                "position": entry.get("position"),
                "stack": entry.get("stack"),
            }
        for seat_id in {s for seats in hand.street_start_active.values()
                        for s in seats}:
            item = seat_meta.setdefault(seat_id, {"seat_id": seat_id})
            if item.get("name") is None or item.get("position") is None:
                name, position = self._seat_info(hand, seat_id)
                if item.get("name") is None:
                    item["name"] = name
                if item.get("position") is None:
                    item["position"] = position
        if seat_meta:
            out["seat_meta"] = [seat_meta[key] for key in sorted(seat_meta)]
        # 起始筹码（含前注/盲注之前的值）。复跑控制台靠它重建 hero 剩余筹码和
        # 有效筹码；没有它整手只能按 unknown 跑，套池/隐含赔率约束全部失效。
        if hand.start_stack is not None:
            out["hero_stack_bb"] = round(hand.start_stack, 2)
        if hand.start_stacks:
            out["start_stacks"] = [hand.start_stacks[seat_id]
                                   for seat_id in hand.start_stacks]
        # 摊牌/主动秀牌读到的对手手牌。没有亮牌时连键都不出现。
        if hand.shows:
            out["shows"] = [hand.shows[seat_id]
                            for seat_id in sorted(hand.shows)]
        # 全下座位、金额与实时胜率。没有全下时连键都不出现。
        if hand.all_ins:
            out["all_ins"] = [hand.all_ins[seat_id]
                              for seat_id in sorted(hand.all_ins)]
        # 牌面异常只在发生过时出现；cards_unreliable=True 说明这手至少有一帧
        # 因牌面不可靠跳过了 AI 建议。
        if hand.cards_warnings:
            out["cards_warnings"] = list(hand.cards_warnings)
        if hand.cards_unreliable:
            out["cards_unreliable"] = True
        # 只在是爆炸底池时写键，普通手牌的落库内容保持原样
        if hand.bomb_pot:
            out["bomb_pot"] = True
        return out

    def copy_hand(self) -> _LiveHand | None:
        """测试/调试用：拿一份独立的工作副本。"""
        return copy.deepcopy(self.hand)
