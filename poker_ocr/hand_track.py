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
  · **换手判据**：庄位变了 / 公共牌掉回 0 或 3 张 / hero 手牌变成另一组 / 人数骤变。
    1/2 张牌是发牌动画，5→4 更像读错一张，都不算换手。
  · **轮到我用「连续帧确认 + 决策点指纹」去重**，不用单帧上升沿。真机上预设按钮
    会在一次回合内消失又出现（见 frame_state 的注释），上升沿会重复触发；
    而多人池里 hero 过牌后对手下注、又轮回 hero 时，按钮可能全程都在、
    根本没有上升沿。指纹里含行动条数与最高下注，正好同时解决这两种情况。
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

# 徽章原文（parse.ACTION_WORDS 已经翻成英文）→ 我们的行动类型，一一对应
ACTION_TYPES = ("check", "call", "bet", "raise", "fold")

AGGRESSIVE = ("bet", "raise")

# 换手时「人数骤变」的阈值：一桌人同时换掉一大半，只可能是换了手/换了桌
TABLE_SIZE_JUMP = 2


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
    table_size: int
    blinds: Blinds | None
    opponents: tuple[OpponentSnapshot, ...]
    streets: tuple[StreetRecord, ...]
    preflop_shapes: tuple[str, ...]
    hero_is_pfr: bool
    cbet_spot: bool
    decision_key: tuple
    warnings: tuple[str, ...] = ()

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
    start_stack: float | None = None
    board: tuple[str, ...] = ()
    street: str = PREFLOP
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

    def street_actions(self, street: str) -> list[ActionRecord]:
        return [a for a in self.actions if a.street == street]

    def snapshot(self) -> tuple[StreetRecord, ...]:
        out: list[StreetRecord] = []
        for street in (PREFLOP, FLOP, TURN, RIVER):
            acts = tuple(a for a in self.actions if a.street == street)
            if acts or street == self.street:
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
        # 上一帧的 hero 手牌/人数，用来判换手
        self._prev_hero_cards: tuple[str, ...] = ()
        self._prev_table_size = 0
        # 公共牌掉回翻前/翻牌的确认计数。单帧读空可能只是遮挡，连续两帧才算换手
        self._drop_frames = 0
        # 上一手结束时的工作副本。调用方 start_hand 时取走，落成一条 hand 记录
        self.last_finished: _LiveHand | None = None

    # ─────────────────────────── 主流程 ───────────────────────────

    def observe(self, state: FrameState,
                hero_cards: list[str] | tuple[str, ...],
                board: list[str] | tuple[str, ...]) -> HandSnapshot | None:
        """喂一帧被接受的状态；返回决策点快照或 None。

        调用方只应喂 `state.ok` 的帧 —— 带 errors 的帧里任何字段都可能是猜的。
        """
        if not state.ok:
            return None

        cards = tuple(hero_cards)
        board = tuple(board)
        board_len = len(board)
        street = street_of_board(board_len)

        if self._is_new_hand(state, cards, board_len):
            if self.hand is not None:
                self.last_finished = copy.deepcopy(self.hand)
            self._start_hand(state, cards, board_len, board)

        hand = self.hand
        assert hand is not None

        # 手牌/位置在遮挡帧里可能读空，只更新有值的
        if cards:
            hand.hero_cards = cards
        hero = self._hero(state)
        if hero is not None and hero.position:
            hand.hero_position = hero.position
        if hand.start_stack is None and hero is not None and hero.stack is not None:
            hand.start_stack = hero.stack
        hand.table_size = state.players_in_hand
        hand.dealer = state.dealer or hand.dealer
        hand.blinds = state.blinds or hand.blinds
        hand.pot = state.pot
        hand.pot_before_street = state.pot_before_street

        # 街推进：公共牌只增不减；1/2 张是动画，不推进。
        # 4 张的读数不能覆盖已经读到的 5 张 —— 那是读错一张，不是新街
        if street is not None and street != hand.street and board_len >= 3:
            hand.street = street
            hand.pot_start.setdefault(street, state.pot_before_street)
            # **不能在这里按桌上的下注额重置基线**：那一刻的下注往往就是本街
            # 第一个动作（比如翻牌刚下的注），重置会把「他下注了」抹平。
            # 基线保持「上一街的下注额」，于是 0 → 2 仍然能被反推成下注。
            if board_len >= len(hand.board):
                hand.board = board
        elif board_len >= 3 and board_len >= len(hand.board):
            hand.board = board

        self._record_actions(state, hand)
        self._prev_hero_cards = cards or self._prev_hero_cards
        self._prev_table_size = state.players_in_hand

        return self._maybe_decision(state, hand)

    def reset(self) -> None:
        """断流重连：桌面状态全部作废，但已给出的建议不重发。"""
        self.hand = None
        self.turn_frames = 0
        self._prev_hero_cards = ()
        self._prev_table_size = 0
        self._drop_frames = 0

    # ─────────────────────────── 换手 ───────────────────────────

    def _is_new_hand(self, state: FrameState, cards: tuple[str, ...],
                     board_len: int) -> bool:
        hand = self.hand
        if hand is None:
            return True
        if hand.dealer and state.dealer and hand.dealer != state.dealer:
            return True
        # 公共牌张数掉回翻前/翻牌，是换手信号。只认 0 和 3：
        # 5→4、3→2 这类更像是读错了一张，不该把一手牌劈成两半。
        # 还要连续两帧确认 —— 单帧读空（遮挡/动画）不能当换手
        # 拿「这手牌见过的最长公共牌」比，而不是上一帧 —— 确认期间手牌状态
        # 故意不推进，上一帧的 board_len 会提前变成 0，第二帧就丢了信号
        seen = len(hand.board)
        if (board_len == 0 and seen > 0) or (board_len == 3 and seen >= 4):
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
        # hero 手牌换成另一组（两张都非空才算，读空是遮挡）
        if len(cards) == 2 and len(self._prev_hero_cards) == 2 \
                and cards != self._prev_hero_cards:
            return True
        if (self._prev_table_size
                and abs(state.players_in_hand - self._prev_table_size) >= TABLE_SIZE_JUMP):
            return True
        return False

    def _start_hand(self, state: FrameState, cards: tuple[str, ...],
                    board_len: int, board: tuple[str, ...]) -> None:
        self.hand_seq += 1
        street = street_of_board(board_len) or PREFLOP
        hero = self._hero(state)
        hand = _LiveHand(
            hand_id=f"h{self.hand_seq}",
            hand_index=self.hand_seq,
            hero_cards=cards,
            hero_position=hero.position if hero else None,
            table_size=state.players_in_hand,
            dealer=state.dealer,
            blinds=state.blinds,
            start_stack=hero.stack if hero else None,
            board=board if board_len >= 3 else tuple(),
            street=street,
            pot=state.pot,
            pot_before_street=state.pot_before_street,
        )
        hand.pot_start[street] = state.pot_before_street
        # 基线：接管链路时桌上可能已经有下注/加注，先把它当当前水位
        hand.level[street] = max((p.bet or 0.0) for p in state.players)
        for p in state.players:
            hand.baseline[(street, p.seat_id)] = p.bet or 0.0
        self.hand = hand
        self.turn_frames = 0

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

    # ─────────────────────────── 决策点 ───────────────────────────

    def _maybe_decision(self, state: FrameState,
                        hand: _LiveHand) -> HandSnapshot | None:
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
            warnings.append("没读到 hero 的开局筹码，套池比例算不出")
        if not hand.hero_cards:
            warnings.append("没读到 hero 手牌")
        if hand.street != PREFLOP and len(hand.board) < 3:
            warnings.append("公共牌不完整")

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
            table_size=state.players_in_hand,
            blinds=state.blinds or hand.blinds,
            opponents=opponents,
            streets=hand.snapshot(),
            preflop_shapes=shapes,
            hero_is_pfr=pfr,
            cbet_spot=cbet,
            decision_key=key,
            warnings=tuple(warnings),
        )

    def _preflop_shapes(self, hand: _LiveHand, max_bet: float,
                        hero_seat: str) -> tuple[str, ...]:
        """到我这一动为止，翻前已经出现过几次加注。

        快照是在**轮到我**时取的，所以已记录的行动全都发生在我这一动之前 ——
        不需要在 hero 自己的行动处截断。hero 开池后被 3bet 时，两次加注都在
        记录里，形态会正确落到 three-bet。

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
            return ("unopened",)
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

    @staticmethod
    def _hand_dict(hand: _LiveHand) -> dict:
        return {
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
                         "is_hero": a.is_hero}
                        for a in rec.actions
                    ],
                }
                for rec in hand.snapshot()
            ],
        }

    def copy_hand(self) -> _LiveHand | None:
        """测试/调试用：拿一份独立的工作副本。"""
        return copy.deepcopy(self.hand)
