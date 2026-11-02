"""牌桌几何 / 赔率 / 位置纯计算：只吃 HandSnapshot，不渲染任何文字。

这一层是「事实层」的原子：`advice.build_situation()` 用它填 `Situation`，
`prompt/` 的各段用它渲染中文事实，离线审计用它复算。函数只读快照、不读写
全局状态，便于单测；不 import 提示词模块，避免 prompt ↔ advice 循环依赖。

从 advice.py 拆出（2026-11-03 提示词模块化）。函数名保持不变，`advice`
仍 re-export 全部名称，旧调用方无需改动。
"""

from __future__ import annotations

from .hand_track import FLOP, PREFLOP, ActionRecord, HandSnapshot
from .positions import POSITIONS_BY_TABLE_SIZE, BB, SB


# ─────────────────────── 牌桌规则常量 ───────────────────────

# 翻前桌上规则：任何加注/开池/再加注的「加注到」总额不得低于 5.5BB。
# 牌桌客户端会拒绝更小的尺度，给出 2.5BB 的开池建议等于给一个点不出来的动作。
# 位置尺度（2.5-4BB）低于这个下限时由 `_cap()` 抬到 5.5BB；真正的全下不受此限
# （后手不足 5.5BB 时只能全下，这是合法动作）。改牌桌规则时只改这个常量。
PREFLOP_MIN_RAISE_BB = 5.5


# ─────────────────────── 翻前人数 ───────────────────────

def count_limpers(snap: HandSnapshot) -> int:
    """我首次行动前、且第一次加注前已经平跟进池的人数。

    行动线的金额是「本街加到总额」：只有 ≤1BB 的 call 才是溜入；6.5BB 的
    call 是「跟加注」。光按金额筛不够：4-bet 之后 SB 补齐 0.5BB 也是 ≤1BB，
    但它不是溜入。所以再加两道门：只看 Hero 首次行动之前、第一次加注之前。

    优先看行动线；但 limper 平跟 1BB 往往不生成动作徽章，行动线会漏。
    再按「同帧各家下注额 == 1BB 且不是盲注位」补一次，取两者较大值。
    """
    bb = snap.blinds.bb if snap.blinds else 1.0
    acts = list(snap.actions_of(PREFLOP))
    hero_idx = next((i for i, a in enumerate(acts) if a.is_hero), len(acts))
    first_raise = next(
        (i for i, a in enumerate(acts) if a.action in ("raise", "bet")),
        None)
    cutoff = hero_idx if first_raise is None else min(hero_idx, first_raise)
    from_actions = sum(
        1 for a in acts[:cutoff]
        if a.action == "call" and not a.is_hero
        and (a.amount is None or float(a.amount) <= bb + 1e-9))
    from_bets = sum(
        1 for o in snap.opponents
        if o.position not in ("SB", "BB") and not o.folded
        and o.bet is not None and abs(float(o.bet) - bb) < 0.01)
    return max(from_actions, from_bets)


def _preflop_raise_count(snap: HandSnapshot) -> int:
    """翻前已经发生的加注次数（含「跟注全下」这种实际再加注）。"""
    return sum(
        1 for a in snap.actions_of(PREFLOP)
        if a.action in ("raise", "bet")
        or (a.action == "call" and a.all_in))



# ─────────────────────── 位置 / SPR / 赔率门槛 ───────────────────────

def call_amount(snap: HandSnapshot) -> float:
    """跟注实际要付的筹码：后手不足时只能跟到全下。

    赔率门槛、离线审计、事实渲染必须用同一个口径，否则「后手 5BB 面对
    20BB 下注」会被算成 20/25 = 80% 这种牌桌上不存在的门槛。
    """
    to_call = round(snap.to_call, 2)
    if snap.hero_stack is not None and 0 < snap.hero_stack < to_call:
        return round(snap.hero_stack, 2)
    return to_call


def _street_bet_from_actions(snap: HandSnapshot, opponent) -> float | None:
    """从本街行动线兜底取某座的本街下注总额（下注 pill 读不到时用）。

    行动线的 amount 是「本街加到总额」，同一座多次行动取最大值即可；
    全下记录尤其容易只有徽章没有金额，所以这个兜底不能省。
    """
    best: float | None = None
    for action in snap.actions_of(snap.street):
        if action.amount is None or action.action not in (
                "bet", "raise", "allin", "call"):
            continue
        if action.seat_id not in (opponent.seat_id, opponent.position) \
                and action.position != opponent.position:
            continue
        amount = float(action.amount)
        best = amount if best is None else max(best, amount)
    return best


def uncalled_excess(snap: HandSnapshot) -> float:
    """当前底池里对手下注超出「我能跟到的总额」的部分。

    全下完全 cover 我时，超出我后手的筹码无人能跟，会原样退回，根本不在
    底池里。牌桌显示的 pot 往往已经把它算进去了（990036：显示 569BB，
    实际可争夺 433.5BB）。只统计**没弃牌**的对手：弃牌玩家投进底池的钱
    是死钱，哪怕超过我的后手也仍然可以被我赢走。
    """
    owed = call_amount(snap)
    hero_total = (snap.hero_bet or 0.0) + owed
    excess = 0.0
    for opponent in snap.opponents:
        if opponent.folded:
            continue
        bet = opponent.bet
        if bet is None:
            bet = _street_bet_from_actions(snap, opponent)
        if bet is not None:
            excess += max(float(bet) - hero_total, 0.0)
    return round(excess, 2)


def winnable_pot(snap: HandSnapshot) -> float:
    """跟注后我实际能争夺的底池（包含我的跟注）。

    赔率的分母只能用这个数：`pot` 是牌桌显示的总池，对手超额全下被退回的
    那部分也在里面。跟注额已经由 `call_amount` 封顶，底池不扣同一部分，
    被 cover 时门槛就会被系统性低估（990036 的 23% vs 真实 39%）。
    """
    pot = snap.pot or 0.0
    if pot <= 0:
        # 底池读不到时不能拿「跟注额」冒充底池，否则门槛会变成 100%。
        return 0.0
    owed = call_amount(snap)
    return round(max(pot - uncalled_excess(snap) + owed, 0.0), 2)


def _action_order(snap: HandSnapshot,
                  street: str | None = None) -> list[str] | None:
    """本街的行动顺序。

    `positions.POSITIONS_BY_TABLE_SIZE` 是「从 SB 起、按行动方向」排的，
    即**翻后**顺序；翻前要把 SB/BB 挪到末尾（UTG 先说话）。
    """
    order = POSITIONS_BY_TABLE_SIZE.get(snap.table_size or 0)
    if not order:
        return None
    if snap.table_size == 2:
        # 单挑：按钮就是 SB —— 翻前 SB 先说话，翻后 BB 先说话。
        return [SB, BB] if (street or snap.street) == PREFLOP else [BB, SB]
    if (street or snap.street) == PREFLOP:
        return order[2:] + order[:2]
    return list(order)


def _can_act(opponent) -> bool:
    """还能主动行动：没弃牌，且后手不为 0（stack=0 就是全下）。"""
    return not opponent.folded and (opponent.stack is None or opponent.stack > 0)


def _positions_known(snap: HandSnapshot, order: list[str]) -> bool:
    """在局且还能行动的对手位置都在表里，才敢算「身后还有谁」。

    有一个对手位置没读出来时，算出来的「我是最后一个」可能是假的 ——
    宁可返回 None 让提示词省略这一行，也不要给模型一个错的「没人了」。
    """
    return all(o.position in order for o in snap.opponents if _can_act(o))


def _after_hero(snap: HandSnapshot, street: str | None = None) -> list[str]:
    """还在手、还能行动、且在本街行动顺序上排在我后面的位置。"""
    order = _action_order(snap, street)
    hero = snap.hero_position
    if order is None or hero not in order:
        return []
    idx = order.index(hero)
    live = {o.position for o in snap.opponents if _can_act(o)}
    return [p for p in order[idx + 1:] if p in live]


def _street_bet(snap: HandSnapshot, opponent) -> float:
    """对手本街已下注总额；下注槽读不到时从行动线兜底，仍未知按 0。"""
    bet = opponent.bet
    if bet is None:
        bet = _street_bet_from_actions(snap, opponent)
    return float(bet or 0.0)


def _same_actor(action: ActionRecord, opponent) -> bool:
    """行动线记录是否属于某个对手（seat_id 或位置任一匹配）。"""
    if action.is_hero:
        return False
    if action.seat_id and opponent.seat_id and action.seat_id == opponent.seat_id:
        return True
    return bool(action.position and opponent.position
                and action.position == opponent.position)


def _big_blind_option_pending(snap: HandSnapshot):
    """翻前 BB 还没主动行动时，即使已投满 1BB 也仍有加注选项。

    `behind_opponents()` 的有下注分支只数「还没跟到最高注」的人。BB 已经
    投了 1BB，Hero 平跟/补盲到 1BB 后 BB 的已投注正好等于最高注，会被过滤
    掉；但 BB 在这一轮仍有 check/raise 的选项，Hero 并没有收尾。
    """
    if snap.street != PREFLOP or (snap.to_call or 0.0) <= 0:
        return None
    if snap.hero_position == BB:
        return None
    bb_opp = next((o for o in snap.opponents
                   if o.position == BB and _can_act(o)), None)
    if bb_opp is None:
        return None
    pre = snap.actions_of(PREFLOP)
    # BB 已经主动行动过（过牌/跟注/加注），或者已经有人加注：前者没有
    # option 了，后者 BB 的 1BB 低于最高注，正常逻辑会把它算进来。
    if any(_same_actor(a, bb_opp) for a in pre):
        return None
    if any(a.action in ("raise", "bet")
           or (a.action == "call" and a.all_in) for a in pre):
        return None
    max_bet = (snap.hero_bet or 0.0) + float(snap.to_call or 0.0)
    if _street_bet(snap, bb_opp) >= max_bet - 1e-9:
        return bb_opp
    return None


def behind_opponents(snap: HandSnapshot) -> list:
    """本街 Hero 之后还要行动的对手。

    有下注时不能按「位置在我后面」数：先行动的人（SB/BB/溜入者）可能还没
    跟到最高注，他们在这一轮会绕回来再说话；而已经下到最高注的人（最后
    加注者）在我跟注后不会再行动。所以有下注时只看「还没跟到最高注」。
    没人下注时才是纯位置顺序：排在我后面的都要过牌/下注。
    """
    order = _action_order(snap)
    hero = snap.hero_position
    if order is None or hero not in order:
        return []
    idx = order.index(hero)
    live = [o for o in snap.opponents if _can_act(o) and o.position in order]
    if snap.to_call and snap.to_call > 0:
        max_bet = (snap.hero_bet or 0.0) + float(snap.to_call)
        behind = []
        for o in live:
            bet = _street_bet(snap, o)
            if bet >= max_bet - 1e-9:
                continue
            # 轮到我时，排在我前面的人如果是 0 注，常见有两种：已经过牌
            # （下注/加注会重新打开行动，他仍要响应）或漏读弃牌/下注额。
            # 只有明确记录过 check 的人才算「还会再说话」；完全没有动作
            # 记录的 0 仍按漏读处理，避免凭一个坏帧说「身后还有人」。
            if bet <= 1e-9 and order.index(o.position) <= idx:
                checked = any(
                    a.action == "check" and _same_actor(a, o)
                    for a in snap.actions_of(snap.street))
                if not checked:
                    continue
            behind.append(o)
        bb_option = _big_blind_option_pending(snap)
        if bb_option is not None and bb_option not in behind:
            behind.append(bb_option)
        behind.sort(key=lambda o: order.index(o.position))
        return behind
    return [o for o in live if order.index(o.position) > idx]


def players_left_to_act(snap: HandSnapshot) -> int | None:
    """我之后还有几个在局对手要说话（按当前下注状态）。

    未知位置返回 None（不要瞎猜成 0）。

    「身后还有人」直接决定能不能诈唬、要不要用中等牌控池：0 人时按当前
    下注状态没有其他人要先响应，但**不等于下注后没人能加注**。任何下注/
    加注都会重新打开行动，已经过牌/跟注/下注的人仍可再行动；具体还要看
    `behind_opponents()` 和对手画像里的 check-raise 频率。
    """
    order = _action_order(snap)
    if (order is None or snap.hero_position not in order
            or not _positions_known(snap, order)):
        return None
    return len(behind_opponents(snap))


def hero_in_position(snap: HandSnapshot) -> bool | None:
    """翻后按**座位顺序**我是否在最后。

    注意：这不等于「本轮当前最后行动」。前面已经过牌的人，在 Hero 下注后
    仍会再行动（可以 check-raise）；真正还有多少人要响应看
    `players_left_to_act()` / `behind_opponents()`。
    """
    order = _action_order(snap, street=FLOP)
    if (order is None or snap.hero_position not in order
            or not _positions_known(snap, order)):
        return None
    return not _after_hero(snap, street=FLOP)


def hero_first_to_act(snap: HandSnapshot) -> bool | None:
    """翻后我是不是**全场第一个**行动。

    `hero_in_position=False` 只表示「我不是最后一个」，中间位置也满足；
    旧文案把它写成「翻后先行动」，CO 在 BTN 之前、UTG 之后时会说错。
    """
    order = _action_order(snap, street=FLOP)
    hero = snap.hero_position
    if (order is None or hero not in order
            or not _positions_known(snap, order)):
        return None
    idx = order.index(hero)
    return all(order.index(o.position) > idx
               for o in snap.opponents
               if _can_act(o) and o.position in order)


def pot_type(snap: HandSnapshot) -> str | None:
    """底池类型：翻前动作线决定翻后的范围宽度与 SPR 语义。

    炸弹池没有翻前动作，`preflop_shapes` 会是 `unopened`；但「无人入池」
    与屏幕上的 BOMB POT 是两回事，模型会因此把强制前注池当普通未开池池。
    横幅/5BB 强制前注已经记在 `bomb_pot` 上，优先用它。
    """
    if snap.bomb_pot:
        return "bomb pot"
    shapes = set(snap.preflop_shapes)
    if "three-bet" in shapes:
        return "3-bet+ pot"
    if "single-raise" in shapes:
        return "single-raised pot"
    if "limped" in shapes:
        return "limped pot"
    if "unopened" in shapes:
        return "unopened pot"
    return None


def spr(snap: HandSnapshot) -> float | None:
    """SPR = 有效筹码 ÷ 跟注前的可争夺底池（翻后才有意义）。

    分母与赔率同口径：先扣掉对手无人能跟的超额下注（`winnable_pot - call`），
    但不把「我的跟注」算进去 —— 与牌桌语言的 SPR 一致（94 / 10.5 = 9.0）。

    分子用「总筹码」而不是后手：对手下注 27.5BB、后手 24BB 时，这一手
    实际能打到的是 51.5BB（51.5 / 45 = 1.1），不是 24 / 45 = 0.5。
    任一在局对手筹码未读出时返回 None：忽略它会把短码局算成深码局。
    没有在局对手时也返回 None：单方面算出来的 SPR 是伪事实。
    """
    if snap.street == PREFLOP or not snap.pot or snap.hero_stack is None:
        return None
    if _unknown_stack_opponents(snap):
        return None
    pot_before_call = winnable_pot(snap) - call_amount(snap)
    if pot_before_call <= 0:
        pot_before_call = snap.pot
    hero = _hero_total_stack(snap)
    if hero is None:
        return None
    totals = [_opponent_total_stack(snap, o) for o in snap.opponents
              if not o.folded and o.stack is not None]
    totals = [t for t in totals if t is not None]
    if not totals:
        return None
    return round(min([*totals, hero]) / pot_before_call, 1)

# ─────────────────────── 爆炸底池 ───────────────────────

# 爆炸底池的前注是固定金额：每个参与者在翻前正好交 5BB（8 人局 40BB、
# 9 人局 45BB）。所以判据是「上一街底池 ÷ 实际参与人数 ≈ 5BB」——刚好
# 整除，而不是 ≥5BB：普通大池的人均可以超过 5BB，但只有炸弹池的总额
# 严格等于 5BB × 人数。命中后全桌都进池，翻后会有大量歪中的两对/三条，
# 顶对只是中等牌，松弱玩家主动打出来的牌也不会小。
EXPLOSIVE_POT_BB_PER_PLAYER = 5.0
# 底池按 0.01BB 显示、人数是整数，理论上能整除；留一分钱误差吸收浮点。
EXPLOSIVE_POT_TOLERANCE = 0.01


def previous_street_pot(snap: HandSnapshot) -> float | None:
    """上一街结束时的底池（BB）。翻前或数据缺失 → None。

    主口径是 `pot_before_street`（界面上的 pbs pill，正是「本街开始前的底池」）。
    这个 pill 实测偶尔会把底池值读到 pbs 槽：当本街已有下注、pbs 却等于底池时，
    用 `pot − 本街所有下注` 反推（客户端底池含本街下注）。反推需要每座下注额
    都读到；有任一未知就继续用 pbs，最后退回本街的 `pot_start`。
    """
    if snap.street == PREFLOP:
        return None
    bets = [snap.hero_bet, *(o.bet for o in snap.opponents)]
    bets_known = all(b is not None for b in bets)
    street_bets = sum(bets) if bets_known else 0.0

    pbs = snap.pot_before_street
    if pbs is not None:
        misread = (snap.pot is not None and bets_known and street_bets > 0
                   and pbs >= snap.pot - 1e-9)
        if misread:
            return max(snap.pot - street_bets, 0.0)
        return pbs
    if snap.pot is not None and bets_known:
        return max(snap.pot - street_bets, 0.0)
    for rec in snap.streets:
        if rec.street == snap.street:
            return rec.pot_start
    return None


def pot_per_player(snap: HandSnapshot) -> float | None:
    """上一街底池 ÷ 实际参与人数（BB/人）。翻前 / 人数未知 / 底池未知 → None。

    参与人数含本街已弃牌的人（炸弹池前注是翻前交的），不含空座/离桌。
    `table_size` 是座位数，9 人桌固定为 9；桌上有空座时用它除会低估人均
    （8 人交前注的 40BB 池，40 ÷ 9 = 4.44，而不是 40 ÷ 8 = 5）。
    `players_dealt` 缺失时退回座位数。
    """
    pot = previous_street_pot(snap)
    count = snap.players_dealt or snap.table_size
    if pot is None or count <= 0:
        return None
    return pot / count


def is_explosive_pot(snap: HandSnapshot) -> bool:
    """这手是不是爆炸底池。

    两个来源，任一成立即可：

      · **屏幕横幅**（`snap.bomb_pot`）：开局中央弹出「幸运暴击 / BOMB POT」，
        这是客户端自己宣布的爆炸底池，最权威。人数或底池读数有偏差时，
        横幅仍然能确认。
      · **底池推算**：翻后「上一街底池 ÷ 实际参与人数」刚好等于 5BB/人。
        炸弹池前注固定 5BB/人，所以总额能被人数整除（40BB ÷ 8 人 = 5BB）；
        普通大池人均可能更高，但不会刚好整除。用于横幅没抓到的手牌
        （中途接管、横幅动画被遮挡/丢弃）。
    """
    if snap.bomb_pot:
        return True
    ratio = pot_per_player(snap)
    if ratio is None:
        return False
    return abs(ratio - EXPLOSIVE_POT_BB_PER_PLAYER) <= EXPLOSIVE_POT_TOLERANCE


# ─────────────────────── 筹码总量 / 有效筹码 / 可玩深度 ───────────────────────

def _no_opponent_anomaly(snap: HandSnapshot) -> bool:
    """翻后状态帧把对手全标弃牌、底池却还有钱：事实自相矛盾。

    这种帧不能按「无对手」或「单挑」打：有效筹码、SPR、落后比例都没有意义，
    提示词和选项都必须硬闸，而不是只加一句「建议不可信」。
    """
    return (snap.street != PREFLOP
            and sum(1 for o in snap.opponents if not o.folded) == 0
            and (snap.pot or 0) > 0)


def _unknown_stack_opponents(snap: HandSnapshot) -> list:
    """在局但筹码没读出来的对手。有效筹码/SPR 必须把它们当成未知。"""
    return [o for o in snap.opponents if not o.folded and o.stack is None]


def _hero_total_stack(snap: HandSnapshot) -> float | None:
    """Hero 本街开始时的总筹码 = 后手 + 本街已下注。

    OCR 的 `stack` 是筹码槽（后手），`bet` 是本街下注槽。两手牌之间的
    「有效筹码」是**这一手还能投入的总量**，已经推进底池的钱也算数。
    """
    if snap.hero_stack is None:
        return None
    return float(snap.hero_stack) + float(snap.hero_bet or 0.0)


def _opponent_total_stack(snap: HandSnapshot, opponent) -> float | None:
    """对手本街开始时的总筹码 = 后手 + 本街已下注。

    只取后手会漏掉他已经推出去的钱：对手下注 27.5BB、后手 24BB 时，
    他的实际筹码是 51.5BB；按 24BB 算会把 SPR 从 1.1 压到 0.5，
    然后给出「有效筹码仅 24BB，全下」这种错建议（2026-10-30 a334）。
    """
    if opponent.stack is None:
        return None
    return float(opponent.stack) + _street_bet(snap, opponent)


def _effective_stack(snap: HandSnapshot) -> float | None:
    """有效筹码 = min(Hero 总筹码, 所有未弃牌对手的总筹码)。

    「总筹码」= 后手 + 本街已下注（见 `_opponent_total_stack`）：对手推进
    底池的那部分仍然是这一手能赢到的钱，不能只算后手。

    有对手筹码未读出时返回 None，而不是忽略它。忽略未知对手会把
    「300BB 对 100BB」算成 300BB，让深码建议出现在短码局。
    没有在局对手时也返回 None：不能把自己的后手当成「有效筹码」。
    """
    hero = _hero_total_stack(snap)
    if hero is None or _unknown_stack_opponents(snap):
        return None
    totals = [_opponent_total_stack(snap, o) for o in snap.opponents
              if not o.folded and o.stack is not None]
    totals = [t for t in totals if t is not None]
    return min([hero, *totals]) if totals else None


def _playable_depth(snap: HandSnapshot) -> float | None:
    """开池范围该按多深来算。

    不是 min(我, 所有对手)：桌上只要有**一个**短码，min 就把整桌判成浅码，
    而决定这手牌能打多深的是能跟注的最深那个对手。所以取
    min(我的后手, 最深的未弃牌对手)。
    """
    if snap.hero_stack is None:
        return None
    others = [o.stack for o in snap.opponents
              if not o.folded and o.stack is not None]
    if not others:
        return snap.hero_stack
    return min(snap.hero_stack, max(others))
