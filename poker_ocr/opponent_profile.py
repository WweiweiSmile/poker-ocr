"""对手类型识别（实时画像）。

设计见 `docs/对手类型识别-设计.md`。这里只做三件事：

  1. 从一手牌的落库记录（`hand` dict）解析出每个对手的**机会/行动**观察；
  2. 把观察聚合成可解释的统计（VPIP/PFR/AF/摊牌…），用确定性规则分类；
  3. 读写本地 `data/opponents/profiles.json`，供实时建议按**本手在局对手**取用。

几条刻意的口径：

  · **按机会计数**：VPIP 的分母是「有翻前行动机会的手数」，不是观察到的
    动作数。没有 `street_active` / `start_stacks` 的旧手牌无法确定谁被发牌，
    直接跳过该手的 VPIP，不猜。
  · **证据不足不乱下结论**：`n < 8` 一律 unknown；8–14 或置信度不够只给
    tentative，实时建议不会因它改硬指令。
  · **人工 pin 永远优先**：pin 的类型直接覆盖自动结果，但自动统计仍继续更新。
  · **纯本地、纯确定性**：不调用模型、不依赖远程 DB；写盘用临时文件 +
    `os.replace` 原子替换。
  · **画像可重建**：手牌记录是唯一事实来源；`tools/rebuild_opponent_profiles.py`
    可以从头重算。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

# 分类器版本：规则/阈值改动时递增，画像文件里会记录，重建时能看出差异。
CLASSIFIER_VERSION = "rules-2026-10-23"

# 合法类型。与设计文档 6.1 一致。
ARCHETYPES = ("unknown", "station", "lag", "maniac", "nit", "tag")
ARCHETYPE_NAMES = {
    "unknown": "未知",
    "station": "松弱/跟注站",
    "lag": "松凶",
    "maniac": "松浪",
    "nit": "紧弱",
    "tag": "紧凶",
}
AGGRESSIVE_ARCHETYPES = ("lag", "maniac")
PASSIVE_ARCHETYPES = ("station", "nit")

# 门控阈值（设计文档 6.3）
MIN_SAMPLE = 8
READ_SAMPLE = 15
READ_CONFIDENCE = 0.65
STABLE_SAMPLE = 30
STABLE_CONFIDENCE = 0.75

# 数据质量：inferred 占比超过该值的手牌降权
INFERRED_DOWNWEIGHT = 0.3
INFERRED_WEIGHT = 0.5

# 爆炸底池强制前注：每人固定 5BB。它属于「被动入池」（人人在池里），
# 不是 VPIP。识别口径与 advice.EXPLOSIVE_POT_BB_PER_PLAYER 一致。
BOMB_ANTE_BB = 5.0

# 列表上限，避免画像文件无限膨胀
MAX_OPEN_SIZES = 200
MAX_BET_SIZES = 500
MAX_SEEN_HANDS = 5000
MAX_ALIASES = 2000

DEFAULT_PROFILE_PATH = Path("data/opponents/profiles.json")
OPPONENT_DB_VAR = "OPPONENT_DB"

_PREFLOP = "preflop"
_POSTFLOP = ("flop", "turn", "river")
_STREET_ORDER = ("preflop", "flop", "turn", "river")


# ─────────────────────────── 小工具 ───────────────────────────

def normalize_name(name: str | None) -> str | None:
    """NFKC、去首尾空格、连续空白合并。名字为空返回 None。"""
    if name is None:
        return None
    text = unicodedata.normalize("NFKC", str(name)).strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def pool_key_from_blinds(blinds, table_size: int | None = None) -> str:
    """`0.5/1-9max` 形式的牌桌池键。盲注读不到时用 unknown。"""
    sb = bb = ante = None
    if blinds is not None:
        if isinstance(blinds, Mapping):
            sb = blinds.get("sb")
            bb = blinds.get("bb")
            ante = blinds.get("ante")
        else:
            sb = getattr(blinds, "sb", None)
            bb = getattr(blinds, "bb", None)
            ante = getattr(blinds, "ante", None)
    seats = int(table_size) if table_size else 9
    if sb is None or bb is None:
        return f"unknown-{seats}max"
    # MVP 同一桌前注通常固定，先不把 ante 拼进池键（设计文档 4.3）。
    _ = ante
    return f"{float(sb):g}/{float(bb):g}-{seats}max"


def hand_uid(hand: Mapping) -> str:
    """跨会话唯一键。优先用导出工具写的 `uid`，否则 session + hand_id。"""
    uid = hand.get("uid")
    if uid:
        return str(uid)
    session = hand.get("session")
    hid = hand.get("hand_id")
    if session is not None and hid:
        return f"s{int(session):02d}-{hid}"
    return str(hid or "")


def _iso_now(now: Callable[[], datetime] | None = None) -> str:
    moment = (now or (lambda: datetime.now().astimezone()))()
    return moment.isoformat(timespec="seconds")


def _num(value: float | int | None):
    """JSON 友好：整数值写成 int，避免 50.0 这种噪声。"""
    if value is None:
        return None
    if isinstance(value, float) and abs(value - round(value)) < 1e-9:
        # 避免 Python 的银行家舍入把 0.5 舍成 0（降权后的样本数会看起来像没有）
        return int(value + 0.5) if value >= 0 else int(value - 0.5)
    return round(value, 4) if isinstance(value, float) else value


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _log_gauss(x: float | None, mu: float, sigma: float) -> float:
    """对数空间的高斯项。缺失特征返回 0（= 中性，不奖励也不惩罚）。

    分类用对数空间求和而不是连乘：连乘在 5 个类型之间很容易一起下溢到
    1e-30，softmax 退化成均匀分布，置信度永远是 0.2，门控永远打不开。
    """
    if x is None:
        return 0.0
    return -0.5 * ((x - mu) / sigma) ** 2


def _softmax(scores: Mapping[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    peak = max(scores.values())
    exps = {k: math.exp(v - peak) for k, v in scores.items()}
    total = sum(exps.values()) or 1.0
    return {k: v / total for k, v in exps.items()}


# ─────────────────────────── 观察 ───────────────────────────

@dataclass(frozen=True)
class HandObservation:
    """一个对手在一手牌里的观察。字段与设计文档 4.1 对齐。"""

    opponent_key: str
    hand_uid: str
    session_id: str
    pool_key: str
    preflop_opp: bool = False
    vpip: bool = False
    pfr: bool = False
    threebet_opp: bool = False
    threebet: bool = False
    saw_flop: bool = False
    postflop_opp: bool = False
    bet_raise: int = 0
    call: int = 0
    fold: int = 0
    check: int = 0
    facing_bet: int = 0
    open_size: float | None = None
    bet_sizes: tuple[float, ...] = ()
    showdown_category: int | None = None
    aggressive_showdown: bool = False
    inferred_ratio: float = 0.0
    # 爆炸底池：人人交固定前注，没有翻前行动。`passive_entry=True` 表示这一手
    # 只有强制前注、没有主动入池，不计入 VPIP 分母（VPIP 只统计主动入池）。
    bomb_pot: bool = False
    passive_entry: bool = False
    display_name: str | None = None
    seat_id: str | None = None
    position: str | None = None
    weight: float = 1.0


def _seat_meta(hand: Mapping) -> dict[str, dict]:
    """seat_id → {name, position, stack}。多个来源按可信度合并。"""
    meta: dict[str, dict] = {}

    def put(seat_id: str | None, name=None, position=None, stack=None):
        if not seat_id:
            return
        item = meta.setdefault(seat_id, {"seat_id": seat_id, "name": None,
                                         "position": None, "stack": None})
        if item.get("name") is None and name:
            item["name"] = name
        if item.get("position") is None and position:
            item["position"] = position
        if item.get("stack") is None and stack is not None:
            item["stack"] = stack

    for entry in hand.get("seat_meta") or []:
        if isinstance(entry, Mapping):
            put(entry.get("seat_id"), entry.get("name"),
                entry.get("position"), entry.get("stack"))
    for entry in hand.get("start_stacks") or []:
        if isinstance(entry, Mapping):
            put(entry.get("seat_id"), entry.get("name"),
                entry.get("position"), entry.get("stack"))
    for entry in hand.get("shows") or []:
        if isinstance(entry, Mapping):
            put(entry.get("seat_id"), entry.get("name"), entry.get("position"))
    for street in hand.get("streets") or []:
        for action in street.get("actions") or []:
            put(action.get("seat_id"), action.get("name"),
                action.get("position"))
    return meta


def _hero_seat(hand: Mapping, meta: Mapping[str, dict]) -> str | None:
    seat = hand.get("hero_seat_id")
    if seat:
        return str(seat)
    for street in hand.get("streets") or []:
        for action in street.get("actions") or []:
            if action.get("is_hero") and action.get("seat_id"):
                return str(action["seat_id"])
    return None


def _street_active(hand: Mapping) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    raw = hand.get("street_active")
    if isinstance(raw, Mapping):
        for street, seats in raw.items():
            if isinstance(seats, (list, tuple, set)):
                out[str(street)] = {str(s) for s in seats if s}
            elif isinstance(seats, Mapping):
                out[str(street)] = {str(s) for s in seats if s}
    return out


def _actions_by_street(hand: Mapping) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for street in hand.get("streets") or []:
        name = str(street.get("street") or "")
        for action in street.get("actions") or []:
            if isinstance(action, Mapping):
                out.setdefault(name, []).append({**action, "_street": name})
    return out


def _dealt_seats(hand: Mapping, active: Mapping[str, set[str]],
                 meta: Mapping[str, dict], hero: str | None
                 ) -> tuple[set[str] | None, set[str] | None]:
    """返回 (翻前在局集合, 翻牌在局集合)；None = 无法确定。

    优先用 `street_active`（新版落库）。旧记录只有 `start_stacks` 时，用
    「start_stacks + hero」近似翻前在局者；连这个都没有就返回 None，
    该手不计入 VPIP/PFR（宁可不统计，也不把弃牌多的人算成入池多的人）。
    """
    preflop = active.get(_PREFLOP)
    flop = active.get("flop")
    if preflop:
        return set(preflop), (set(flop) if flop is not None else None)
    stacks = [e for e in (hand.get("start_stacks") or [])
              if isinstance(e, Mapping) and e.get("seat_id")]
    if stacks:
        seats = {str(e["seat_id"]) for e in stacks}
        if hero:
            seats.add(str(hero))
        # 旧记录只能确认「这些座位被发过牌」，翻牌在局者用 flop 街动作近似
        return seats, (set(flop) if flop is not None else None)
    return None, (set(flop) if flop is not None else None)


def _bet_size_ratios(hand: Mapping, seat_id: str) -> tuple[float, ...]:
    """按街用 `pot_start` 重算下注尺度；没有 pot_start 的街跳过。"""
    sizes: list[float] = []
    for street in hand.get("streets") or []:
        name = str(street.get("street") or "")
        if name not in _POSTFLOP:
            continue
        pot_start = street.get("pot_start")
        if not isinstance(pot_start, (int, float)):
            continue
        contrib: dict[str, float] = {}
        for action in street.get("actions") or []:
            if not isinstance(action, Mapping):
                continue
            kind = action.get("action")
            seat = str(action.get("seat_id") or "")
            amount = action.get("amount")
            amount = float(amount) if isinstance(amount, (int, float)) else None
            if kind in ("bet", "raise") and seat == seat_id and amount:
                pot_before = float(pot_start) + sum(contrib.values())
                if pot_before > 0:
                    ratio = amount / pot_before
                    if 0 < ratio <= 10:
                        sizes.append(round(ratio, 3))
            if amount is not None:
                contrib[seat] = max(contrib.get(seat, 0.0), amount)
    return tuple(sizes)


def _showdown(hand: Mapping, seat_id: str,
              board: Sequence[str]) -> tuple[int | None, bool]:
    """摊牌牌型 + 摊牌前是否主动下注/加注。只有完整两张亮牌才算摊牌。"""
    cards = None
    for show in hand.get("shows") or []:
        if isinstance(show, Mapping) and str(show.get("seat_id")) == seat_id:
            cards = [str(c) for c in (show.get("cards") or [])]
            break
    if not cards or len(cards) != 2 or len(board) < 3:
        return None, False
    try:
        from .coach import evaluate, parse_cards
        category, _ = evaluate(parse_cards("".join(cards + list(board[:5]))))
    except (ValueError, TypeError):
        return None, False
    aggressive = False
    for street in _POSTFLOP:
        for action in (hand.get("streets") or []):
            if str(action.get("street")) != street:
                continue
            for act in action.get("actions") or []:
                if (str(act.get("seat_id")) == seat_id
                        and act.get("action") in ("bet", "raise")):
                    aggressive = True
                    break
            if aggressive:
                break
        if aggressive:
            break
    return category, aggressive


def _bomb_pot_entry(hand: Mapping,
                    preflop_actions: Sequence[Mapping]) -> tuple[bool, bool]:
    """返回 (是否爆炸底池, 是否只有被动入池)。

    爆炸底池有两种来源：手牌记录里的 `bomb_pot` 横幅，或 `ante>=5` 且翻前
    没有主动行动。强制前注不是 VPIP；只有真正出现**非推断的** call/bet/raise
    时，才把这一手当成有翻前行动机会。
    """
    flagged = bool(hand.get("bomb_pot"))
    blinds = hand.get("blinds") or {}
    ante = blinds.get("ante") if isinstance(blinds, Mapping) else None
    try:
        big_ante = ante is not None and float(ante) >= BOMB_ANTE_BB
    except (TypeError, ValueError):
        big_ante = False
    # 只有「非 inferred 的动作」才算真实翻前行动。旧记录里被补出来的
    # inferred check 会伪装成一条翻前街，不能据此把强制前注算成 VPIP 机会。
    real_preflop = any(not a.get("inferred") for a in preflop_actions)
    # 没有横幅时，只有「大 ante + 翻前确实没动作」才按爆炸底池处理；
    # 普通 5BB 前注局里真实发生的 call/check/raise 不能被误标成被动入池。
    bomb = flagged or (big_ante and not real_preflop)
    if not bomb:
        return False, False
    return True, not real_preflop


def observations_from_hand(hand: Mapping, *,
                            pool_key: str | None = None) -> list[HandObservation]:
    """把一条 hand 记录解析成每个对手的 `HandObservation`。

    hero 自己不算对手；名字缺失的座位用 `temp:<session>:<seat_id>` 临时键。
    """
    if not isinstance(hand, Mapping):
        return []
    meta = _seat_meta(hand)
    active = _street_active(hand)
    hero = _hero_seat(hand, meta)
    preflop, flop = _dealt_seats(hand, active, meta, hero)
    actions = _actions_by_street(hand)
    preflop_actions = actions.get(_PREFLOP) or []
    bomb_pot, passive_entry = _bomb_pot_entry(hand, preflop_actions)
    board = [str(c) for c in (hand.get("board") or [])]
    session = str(hand.get("session") if hand.get("session") is not None else "")
    uid = hand_uid(hand)
    hand_pool = pool_key or pool_key_from_blinds(
        hand.get("blinds"), hand.get("table_size"))

    # 对手候选：翻前在局者优先；没有翻前数据时用翻牌在局者
    seats = set(preflop or ()) | set(flop or ())
    if hero:
        seats.discard(hero)
    if not seats:
        return []

    out: list[HandObservation] = []
    for seat_id in sorted(seats):
        info = meta.get(seat_id) or {}
        name = normalize_name(info.get("name"))
        key = name or f"temp:{session}:{seat_id}"
        seat_actions = [a for street in _STREET_ORDER
                        for a in actions.get(street) or []
                        if str(a.get("seat_id")) == seat_id]
        post_acts = [a for a in seat_actions
                     if str(a.get("_street") or "") in _POSTFLOP]
        pre_acts = [a for a in preflop_actions
                    if str(a.get("seat_id")) == seat_id]
        if passive_entry:
            # 爆炸底池的「翻前」没有行动。旧记录可能被补过 inferred check，
            # 这些既不是 VPIP，也不该拉低数据质量；按翻后动作算 inferred。
            pre_acts = []
            seat_actions = [a for a in seat_actions
                            if str(a.get("_street") or "") != _PREFLOP]
        def _aggr(action) -> bool:
            return action.get("action") in ("bet", "raise")

        vpip = any(a.get("action") in ("call", "bet", "raise")
                   for a in pre_acts)
        pfr = any(_aggr(a) or (a.get("action") == "call" and a.get("all_in"))
                  for a in pre_acts)

        # 3bet 机会：这个座位首次行动之前已经有别人加注；没有动作时，
        # 只要翻前出现过加注就按「面对过加注」算机会（样本只用于降置信度）。
        first_idx = None
        for i, action in enumerate(preflop_actions):
            if str(action.get("seat_id")) == seat_id:
                first_idx = i
                break
        prior = (preflop_actions[:first_idx] if first_idx is not None
                 else preflop_actions)
        threebet_opp = any(_aggr(a) and str(a.get("seat_id")) != seat_id
                           for a in prior)
        threebet = False
        seen_raise = False
        for action in preflop_actions:
            if _aggr(action):
                if (str(action.get("seat_id")) == seat_id and seen_raise):
                    threebet = True
                if str(action.get("seat_id")) != seat_id:
                    seen_raise = True

        open_size = None
        for action in preflop_actions:
            if str(action.get("seat_id")) == seat_id and _aggr(action):
                amount = action.get("amount")
                if isinstance(amount, (int, float)) and amount > 0:
                    open_size = float(amount)
                break

        # 翻后计数
        bet_raise = sum(1 for a in post_acts if _aggr(a))
        call = sum(1 for a in post_acts if a.get("action") == "call")
        fold = sum(1 for a in post_acts if a.get("action") == "fold")
        check = sum(1 for a in post_acts if a.get("action") == "check")

        # 面对下注次数：按街顺序扫，激进动作把 level 抬高；之后该座每次
        # call/fold/raise 都算一次面对下注。
        facing = 0
        for street in _POSTFLOP:
            level = 0.0
            for action in actions.get(street) or []:
                kind = action.get("action")
                amount = action.get("amount")
                amount = float(amount) if isinstance(amount, (int, float)) else 0.0
                if kind in ("bet", "raise"):
                    if (str(action.get("seat_id")) == seat_id
                            and level > 0):
                        facing += 1
                    level = max(level, amount)
                elif kind in ("call", "fold"):
                    if (str(action.get("seat_id")) == seat_id
                            and level > 0):
                        facing += 1

        inferred = sum(1 for a in seat_actions if a.get("inferred"))
        inferred_ratio = inferred / len(seat_actions) if seat_actions else 0.0
        weight = (INFERRED_WEIGHT if inferred_ratio > INFERRED_DOWNWEIGHT
                  else 1.0)

        category, aggressive_showdown = _showdown(hand, seat_id, board)
        bet_sizes = _bet_size_ratios(hand, seat_id)

        out.append(HandObservation(
            opponent_key=key,
            hand_uid=uid,
            session_id=session,
            pool_key=hand_pool,
            preflop_opp=bool(preflop is not None and seat_id in preflop
                             and not passive_entry),
            vpip=vpip and not passive_entry,
            pfr=pfr,
            threebet_opp=threebet_opp,
            threebet=threebet,
            saw_flop=bool(flop is not None and seat_id in flop),
            postflop_opp=bool(flop is not None and seat_id in flop),
            bet_raise=bet_raise,
            call=call,
            fold=fold,
            check=check,
            facing_bet=facing,
            open_size=open_size,
            bet_sizes=bet_sizes,
            showdown_category=category,
            aggressive_showdown=aggressive_showdown,
            inferred_ratio=round(inferred_ratio, 3),
            bomb_pot=bomb_pot,
            passive_entry=passive_entry,
            display_name=name,
            seat_id=seat_id,
            position=info.get("position"),
            weight=weight,
        ))
    return out


# ─────────────────────────── 统计 ───────────────────────────

@dataclass
class OpponentStats:
    """一个对手的聚合统计。数值允许是小数（数据质量降权后的加权计数）。"""

    key: str = ""
    display_name: str | None = None
    pool_key: str = ""
    hands: float = 0.0
    # 未降权的真实样本数：n 用它，避免 0.5 权重把一手牌四舍五入成 0。
    sample_hands: float = 0.0
    sample_preflop_opps: float = 0.0
    preflop_opps: float = 0.0
    vpip: float = 0.0
    pfr: float = 0.0
    threebet_opps: float = 0.0
    threebet: float = 0.0
    postflop_opps: float = 0.0
    bets_raises: float = 0.0
    calls: float = 0.0
    folds: float = 0.0
    checks: float = 0.0
    facing_bets: float = 0.0
    open_sizes: list[float] = field(default_factory=list)
    bet_sizes: list[float] = field(default_factory=list)
    showdowns: float = 0.0
    aggressive_showdowns: float = 0.0
    weak_aggressive_showdowns: float = 0.0
    bomb_pot_hands: float = 0.0
    passive_entry_hands: float = 0.0
    inferred_weighted: float = 0.0
    weight_total: float = 0.0
    manual_pin: str | None = None
    pinned_at: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    updated_at: str | None = None

    # ── 派生特征 ──
    @property
    def vpip_pct(self) -> float:
        return (self.vpip / self.preflop_opps * 100
                if self.preflop_opps > 0 else 0.0)

    @property
    def pfr_pct(self) -> float:
        return (self.pfr / self.preflop_opps * 100
                if self.preflop_opps > 0 else 0.0)

    @property
    def threebet_pct(self) -> float | None:
        return (self.threebet / self.threebet_opps * 100
                if self.threebet_opps > 0 else None)

    @property
    def postflop_aggr_pct(self) -> float | None:
        return (self.bets_raises / self.postflop_opps * 100
                if self.postflop_opps > 0 else None)

    @property
    def af(self) -> float | None:
        if self.postflop_opps <= 0 and self.calls <= 0 and self.bets_raises <= 0:
            return None
        return (self.bets_raises / max(self.calls, 1.0))

    @property
    def overbet_pct(self) -> float | None:
        if not self.bet_sizes:
            return None
        return sum(1 for x in self.bet_sizes if x > 1.2) / len(self.bet_sizes) * 100

    @property
    def open_median(self) -> float | None:
        return _median(self.open_sizes)

    @property
    def bet_median(self) -> float | None:
        return _median(self.bet_sizes)

    @property
    def weak_aggressive_pct(self) -> float | None:
        if self.aggressive_showdowns <= 0:
            return None
        return (self.weak_aggressive_showdowns / self.aggressive_showdowns * 100)

    @property
    def inferred_ratio(self) -> float:
        return (self.inferred_weighted / self.weight_total
                if self.weight_total > 0 else 0.0)

    @property
    def n(self) -> int:
        sample = self.sample_preflop_opps or self.preflop_opps
        return int(sample + 0.5) if sample > 0 else 0

    def add(self, obs: HandObservation) -> None:
        w = float(obs.weight or 1.0)
        # hands 是真实手数（不降权）；特征计数才乘数据质量权重。
        self.hands += 1.0
        self.sample_hands += 1.0
        if obs.preflop_opp:
            self.sample_preflop_opps += 1.0
        self.weight_total += w
        self.inferred_weighted += w * float(obs.inferred_ratio or 0.0)
        self.preflop_opps += w if obs.preflop_opp else 0.0
        self.vpip += w if obs.vpip else 0.0
        self.pfr += w if obs.pfr else 0.0
        self.threebet_opps += w if obs.threebet_opp else 0.0
        self.threebet += w if obs.threebet else 0.0
        self.postflop_opps += w if obs.postflop_opp else 0.0
        self.bets_raises += w * obs.bet_raise
        self.calls += w * obs.call
        self.folds += w * obs.fold
        self.checks += w * obs.check
        self.facing_bets += w * obs.facing_bet
        if obs.open_size is not None and obs.open_size > 0:
            self.open_sizes.append(round(float(obs.open_size), 3))
            if len(self.open_sizes) > MAX_OPEN_SIZES:
                del self.open_sizes[:len(self.open_sizes) - MAX_OPEN_SIZES]
        for size in obs.bet_sizes:
            self.bet_sizes.append(float(size))
        if len(self.bet_sizes) > MAX_BET_SIZES:
            del self.bet_sizes[:len(self.bet_sizes) - MAX_BET_SIZES]
        if obs.bomb_pot:
            self.bomb_pot_hands += 1.0
        if obs.passive_entry:
            self.passive_entry_hands += 1.0
        if obs.showdown_category is not None:
            self.showdowns += w
            if obs.aggressive_showdown:
                self.aggressive_showdowns += w
                if obs.showdown_category <= 1:      # 高牌/一对
                    self.weak_aggressive_showdowns += w
        if obs.display_name and not self.display_name:
            self.display_name = obs.display_name

    def to_dict(self) -> dict:
        read = classify(self)
        return {
            "display_name": self.display_name or self.key,
            "pool_key": self.pool_key,
            "hands": _num(self.hands),
            "sample_hands": _num(self.sample_hands),
            "sample_preflop_opps": _num(self.sample_preflop_opps),
            "preflop_opps": _num(self.preflop_opps),
            "vpip": _num(self.vpip),
            "pfr": _num(self.pfr),
            "threebet_opps": _num(self.threebet_opps),
            "threebet": _num(self.threebet),
            "postflop_opps": _num(self.postflop_opps),
            "bets_raises": _num(self.bets_raises),
            "calls": _num(self.calls),
            "folds": _num(self.folds),
            "checks": _num(self.checks),
            "facing_bets": _num(self.facing_bets),
            "open_sizes": [_num(x) for x in self.open_sizes],
            "bet_sizes": [_num(x) for x in self.bet_sizes],
            "showdowns": _num(self.showdowns),
            "aggressive_showdowns": _num(self.aggressive_showdowns),
            "weak_aggressive_showdowns": _num(self.weak_aggressive_showdowns),
            "bomb_pot_hands": _num(self.bomb_pot_hands),
            "passive_entry_hands": _num(self.passive_entry_hands),
            "inferred_ratio": round(self.inferred_ratio, 4),
            "archetype": read.archetype,
            "confidence": round(read.confidence, 4),
            "manual_pin": self.manual_pin,
            "pinned_at": self.pinned_at,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "updated_at": self.updated_at,
            "classifier_version": CLASSIFIER_VERSION,
        }

    @classmethod
    def from_dict(cls, key: str, raw: Mapping) -> "OpponentStats":
        def f(name: str) -> float:
            value = raw.get(name)
            return float(value) if isinstance(value, (int, float)) else 0.0

        return cls(
            key=key,
            display_name=raw.get("display_name") or key,
            pool_key=str(raw.get("pool_key") or ""),
            hands=f("hands"),
            sample_hands=f("sample_hands") or f("hands"),
            sample_preflop_opps=(f("sample_preflop_opps")
                                 or f("preflop_opps")),
            preflop_opps=f("preflop_opps"),
            vpip=f("vpip"),
            pfr=f("pfr"),
            threebet_opps=f("threebet_opps"),
            threebet=f("threebet"),
            postflop_opps=f("postflop_opps"),
            bets_raises=f("bets_raises"),
            calls=f("calls"),
            folds=f("folds"),
            checks=f("checks"),
            facing_bets=f("facing_bets"),
            open_sizes=[float(x) for x in raw.get("open_sizes") or []
                        if isinstance(x, (int, float))],
            bet_sizes=[float(x) for x in raw.get("bet_sizes") or []
                       if isinstance(x, (int, float))],
            showdowns=f("showdowns"),
            aggressive_showdowns=f("aggressive_showdowns"),
            weak_aggressive_showdowns=f("weak_aggressive_showdowns"),
            bomb_pot_hands=f("bomb_pot_hands"),
            passive_entry_hands=f("passive_entry_hands"),
            inferred_weighted=(f("inferred_ratio") * f("hands")),
            weight_total=f("hands"),
            manual_pin=(raw.get("manual_pin")
                        if raw.get("manual_pin") in ARCHETYPES else None),
            pinned_at=raw.get("pinned_at"),
            first_seen=raw.get("first_seen"),
            last_seen=raw.get("last_seen"),
            updated_at=raw.get("updated_at"),
        )


# ─────────────────────────── 分类 ───────────────────────────

@dataclass(frozen=True)
class OpponentRead:
    """一次决策要用的对手画像快照（不可变，可安全跨线程）。"""

    seat_id: str | None
    key: str
    display_name: str | None
    archetype: str
    confidence: float
    n: int
    vpip: float
    pfr: float
    af: float
    source: str
    note: str
    gate: str = "unknown"

    @property
    def aggressive(self) -> bool:
        return self.archetype in AGGRESSIVE_ARCHETYPES

    @property
    def passive(self) -> bool:
        return self.archetype in PASSIVE_ARCHETYPES

    def label(self) -> str:
        return ARCHETYPE_NAMES.get(self.archetype, self.archetype)


def _gate(n: int, confidence: float) -> str:
    if n < MIN_SAMPLE:
        return "unknown"
    if n >= STABLE_SAMPLE and confidence >= STABLE_CONFIDENCE:
        return "stable"
    if n >= READ_SAMPLE and confidence >= READ_CONFIDENCE:
        return "read"
    return "tentative"


def _unknown_read(stats: OpponentStats | None, *, seat_id: str | None = None,
                  key: str = "", source: str = "unknown",
                  note: str = "样本不足（n<8）") -> OpponentRead:
    n = stats.n if stats is not None else 0
    return OpponentRead(
        seat_id=seat_id,
        key=key or (stats.key if stats is not None else ""),
        display_name=(stats.display_name if stats is not None else None) or key,
        archetype="unknown",
        confidence=0.0,
        n=n,
        vpip=round(stats.vpip_pct, 1) if stats is not None else 0.0,
        pfr=round(stats.pfr_pct, 1) if stats is not None else 0.0,
        af=round(stats.af, 2) if stats is not None and stats.af is not None else 0.0,
        source=source,
        note=note,
        gate="unknown",
    )


def classify(stats: OpponentStats) -> OpponentRead:
    """规则评分 + softmax。样本不足返回 unknown，缺失特征保持中性。"""
    n = stats.n
    if n < MIN_SAMPLE:
        return _unknown_read(stats)

    vpip = stats.vpip_pct
    pfr = stats.pfr_pct
    # 每个特征有自己的最小样本；不足时传 None（gauss 返回中性 1.0）。
    af = stats.af if stats.postflop_opps >= 8 else None
    open_med = stats.open_median if len(stats.open_sizes) >= 5 else None
    overbet = stats.overbet_pct if len(stats.bet_sizes) >= 10 else None
    weak_aggr = (stats.weak_aggressive_pct
                 if stats.showdowns >= 5 and stats.aggressive_showdowns >= 5
                 else None)
    threebet = stats.threebet_pct if stats.threebet_opps >= 5 else None
    post_aggr = stats.postflop_aggr_pct if stats.postflop_opps >= 8 else None

    scores = {
        "station": (_log_gauss(vpip, 45, 12) + _log_gauss(pfr, 8, 6)
                    + _log_gauss(af, 0.7, 0.5)),
        "lag": (_log_gauss(vpip, 35, 10) + _log_gauss(pfr, 26, 8)
                + _log_gauss(af, 2.2, 0.9)),
        "maniac": (_log_gauss(pfr, 45, 12) + _log_gauss(open_med, 10, 3)
                   + _log_gauss(overbet, 20, 12)),
        "nit": (_log_gauss(vpip, 14, 6) + _log_gauss(pfr, 9, 5)
                + _log_gauss(af, 0.8, 0.5)),
        "tag": (_log_gauss(vpip, 24, 6) + _log_gauss(pfr, 20, 6)
                + _log_gauss(af, 2.4, 0.8)),
    }
    # 弱牌激进摊牌高 → 更松浪；摊牌范围强 → 更紧凶。
    if weak_aggr is not None:
        if weak_aggr >= 50:
            scores["maniac"] += math.log(1.6)
            scores["lag"] += math.log(1.15)
        elif weak_aggr <= 20:
            scores["tag"] += math.log(1.25)
    if threebet is not None:
        scores["maniac"] += math.log(1.0 + min(threebet, 60) / 100.0)
        scores["lag"] += math.log(1.0 + min(threebet, 40) / 200.0)
    if post_aggr is not None:
        # AF 已经包含翻后激进；这里只做轻微加权，避免把高 AF 的 TAG 推成 LAG。
        scores["lag"] += math.log(1.0 + min(post_aggr, 100) / 500.0)
        scores["maniac"] += math.log(1.0 + min(post_aggr, 100) / 600.0)
    if not stats.bet_sizes and not stats.open_sizes:
        # 完全看不到尺度时，maniac 的「大 open/超池」证据为零，不要靠先验赢。
        scores["maniac"] += math.log(0.5)
    if not stats.showdowns:
        scores["tag"] += math.log(0.8)

    probs = _softmax(scores)
    archetype = max(probs, key=probs.get)
    # 设计文档写的是 n/30，但「n≥15 且 conf≥0.65 才算 read」在 n/30 下
    # 最多只能到 0.5，门控永远打不开。这里按门控的本意改成 n/15：
    # 15 手以后样本因子不再压低置信度，8 手时约为 0.53。
    sample_factor = min(1.0, n / 15.0)
    confidence = max(probs.values()) * sample_factor
    gate = _gate(n, confidence)
    note_parts = [f"VPIP {vpip:.0f}%", f"PFR {pfr:.0f}%"]
    if af is not None:
        note_parts.append(f"AF {af:.1f}")
    note_parts.append(f"n={n}")
    if gate == "tentative":
        note_parts.append("tentative")
    return OpponentRead(
        seat_id=None,
        key=stats.key,
        display_name=stats.display_name or stats.key,
        archetype=archetype,
        confidence=round(confidence, 3),
        n=n,
        vpip=round(vpip, 1),
        pfr=round(pfr, 1),
        af=round(af, 2) if af is not None else 0.0,
        source="auto",
        note=", ".join(note_parts),
        gate=gate,
    )


# ─────────────────────────── 存储 ───────────────────────────

class ProfileStore:
    """本地画像仓库：加载、更新、按座位读、原子落盘。

    一个实例单进程写。`path` 指向 JSON 文件；`enabled=False` 时所有方法都是
    空操作，方便 `--no-opponent-profile` 回退到 `.table_dy`。
    """

    def __init__(self, path: str | Path = DEFAULT_PROFILE_PATH,
                 pool_key: str | None = None, *, enabled: bool = True,
                 now: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] | None = None) -> None:
        self.path = Path(path)
        self._pool_key = pool_key or ""
        self.enabled = bool(enabled)
        self._now = now or (lambda: datetime.now().astimezone())
        self._monotonic = monotonic or time.monotonic
        self.players: dict[str, OpponentStats] = {}
        self.aliases: dict[str, str] = {}
        self.seat_keys: dict[str, str] = {}
        self.seen_hands: list[str] = []
        self._seen: set[str] = set()
        self.load_error: str | None = None
        self.last_save_error: str | None = None
        self.classifier_version = CLASSIFIER_VERSION
        self._dirty = False
        self._updates = 0
        self._last_save = 0.0
        self._saved_at: str | None = None

    # ── 属性 ──
    @property
    def pool_key(self) -> str:
        return self._pool_key

    def set_pool_key(self, pool_key: str | None) -> None:
        """首次读到盲注后确定牌桌池。已有画像时不会被改掉。"""
        if not pool_key or self._pool_key == pool_key:
            return
        if not self._pool_key:
            self._pool_key = pool_key
            self._dirty = True

    # ── 读写 ──
    def load(self) -> None:
        if not self.enabled:
            return
        self.load_error = None
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.load_error = f"读取 {self.path} 失败：{exc}"
            return
        except UnicodeDecodeError as exc:
            self.load_error = f"{self.path} 不是 UTF-8 文本：{exc}"
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.load_error = f"{self.path} 不是合法 JSON：{exc}"
            return
        if not isinstance(data, Mapping):
            self.load_error = f"{self.path} 顶层不是对象"
            return
        self.classifier_version = str(
            data.get("classifier_version") or CLASSIFIER_VERSION)
        if not self._pool_key:
            self._pool_key = str(data.get("pool_key") or "")
        for key, entry in (data.get("players") or {}).items():
            if not isinstance(entry, Mapping):
                continue
            stats = OpponentStats.from_dict(str(key), entry)
            self.players[str(key)] = stats
        self.aliases = {str(k): str(v) for k, v in
                        (data.get("aliases") or {}).items()}
        self.seat_keys = {str(k): str(v) for k, v in
                          (data.get("seat_keys") or {}).items()}
        seen = [str(x) for x in (data.get("seen_hands") or [])]
        self.seen_hands = seen[-MAX_SEEN_HANDS:]
        self._seen = set(self.seen_hands)

    def update_hand(self, hand: Mapping) -> list[HandObservation]:
        """解析一手牌并更新画像。同一 hand_uid 重复调用不会重复计数。"""
        if not self.enabled:
            return []
        # 有盲注就按这一手自己的盲注推导池键；没有盲注（旧记录）才用
        # store 的默认池。这样同一个文件里 1/2 和 5/10 不会混在一起。
        observations = observations_from_hand(
            hand, pool_key=(None if hand.get("blinds")
                            else (self._pool_key or None)))
        if not observations:
            return []
        uid = observations[0].hand_uid
        if uid and uid in self._seen:
            return []
        self._learn_seat_aliases(observations)
        for obs in observations:
            key = obs.opponent_key
            pool = obs.pool_key or self._pool_key
            if not self._pool_key and pool:
                self._pool_key = pool
            base = self.players.get(key)
            if (base is not None and base.pool_key and pool
                    and base.pool_key != pool):
                # 同名不同池：按 pool 分开，避免 1/2 和 5/10 混在一起。
                key = f"{key}@{pool}"
            stats = self.players.get(key)
            if stats is None:
                stats = OpponentStats(key=key, display_name=obs.display_name,
                                      pool_key=pool)
                self.players[key] = stats
            stats.add(obs)
            if not stats.pool_key:
                stats.pool_key = pool
            if not stats.first_seen:
                stats.first_seen = str(hand.get("ts") or _iso_now(self._now))
            stats.last_seen = str(hand.get("ts") or _iso_now(self._now))
            stats.updated_at = stats.last_seen
            if obs.display_name:
                stats.display_name = obs.display_name
        if uid:
            self.seen_hands.append(uid)
            self._seen.add(uid)
            if len(self.seen_hands) > MAX_SEEN_HANDS:
                self.seen_hands = self.seen_hands[-MAX_SEEN_HANDS:]
                self._seen = set(self.seen_hands)
        self._dirty = True
        self._updates += 1
        return observations

    def _learn_seat_aliases(self, observations: Sequence[HandObservation]) -> None:
        """名字 OCR 恢复后，把临时键的统计合并到名字键，并写 aliases。"""
        for obs in observations:
            if not obs.session_id or not obs.seat_id:
                continue
            seat_key = f"{obs.session_id}|{obs.seat_id}"
            previous = self.seat_keys.get(seat_key)
            if previous is None:
                self.seat_keys[seat_key] = obs.opponent_key
                continue
            if previous == obs.opponent_key:
                continue
            if not obs.opponent_key.startswith("temp:") \
                    and previous.startswith("temp:"):
                old = self.players.pop(previous, None)
                target = self.players.get(obs.opponent_key)
                if old is not None:
                    if target is None:
                        old.key = obs.opponent_key
                        self.players[obs.opponent_key] = old
                    else:
                        self._merge_stats(target, old)
                self.aliases[previous] = obs.opponent_key
                if len(self.aliases) > MAX_ALIASES:
                    for stale in list(self.aliases)[:len(self.aliases) - MAX_ALIASES]:
                        self.aliases.pop(stale, None)
                self._dirty = True
            self.seat_keys[seat_key] = obs.opponent_key

    @staticmethod
    def _merge_stats(target: OpponentStats, old: OpponentStats) -> None:
        target.hands += old.hands
        target.sample_hands += old.sample_hands
        target.sample_preflop_opps += old.sample_preflop_opps
        target.preflop_opps += old.preflop_opps
        target.vpip += old.vpip
        target.pfr += old.pfr
        target.threebet_opps += old.threebet_opps
        target.threebet += old.threebet
        target.postflop_opps += old.postflop_opps
        target.bets_raises += old.bets_raises
        target.calls += old.calls
        target.folds += old.folds
        target.checks += old.checks
        target.facing_bets += old.facing_bets
        target.open_sizes.extend(old.open_sizes)
        target.bet_sizes.extend(old.bet_sizes)
        target.showdowns += old.showdowns
        target.aggressive_showdowns += old.aggressive_showdowns
        target.weak_aggressive_showdowns += old.weak_aggressive_showdowns
        target.bomb_pot_hands += old.bomb_pot_hands
        target.passive_entry_hands += old.passive_entry_hands
        target.inferred_weighted += old.inferred_weighted
        target.weight_total += old.weight_total
        if old.first_seen and (not target.first_seen
                               or old.first_seen < target.first_seen):
            target.first_seen = old.first_seen
        if old.last_seen and (not target.last_seen
                              or old.last_seen > target.last_seen):
            target.last_seen = old.last_seen
        if not target.manual_pin and old.manual_pin:
            target.manual_pin = old.manual_pin
            target.pinned_at = old.pinned_at

    # ── pin ──
    def _resolve_key(self, name: str | None) -> str | None:
        normalized = normalize_name(name)
        if not normalized:
            return None
        key = normalized
        if key in self.players:
            return key
        pool = self._pool_key
        if pool and f"{key}@{pool}" in self.players:
            return f"{key}@{pool}"
        alias = self.aliases.get(key)
        if alias and alias in self.players:
            return alias
        return None

    def pin(self, key: str, archetype: str | None) -> None:
        """人工 pin。`archetype=None` 清除；类型非法抛 ValueError。"""
        if not self.enabled:
            return
        if archetype is not None and archetype not in ARCHETYPES[1:]:
            raise ValueError(f"未知类型：{archetype!r}（可选："
                             f"{', '.join(ARCHETYPES[1:])}；清除用 --unpin）")
        normalized = normalize_name(key)
        if not normalized:
            raise ValueError("pin 的名字不能为空")
        stats = self.players.get(normalized)
        if stats is None:
            stats = OpponentStats(key=normalized, display_name=normalized,
                                  pool_key=self._pool_key)
            self.players[normalized] = stats
        stats.manual_pin = archetype
        stats.pinned_at = _iso_now(self._now) if archetype else None
        stats.updated_at = _iso_now(self._now)
        self._dirty = True

    # ── 读取 ──
    def _read_for_stats(self, stats: OpponentStats | None,
                        seat_id: str | None) -> OpponentRead:
        if stats is None:
            return _unknown_read(None, seat_id=seat_id,
                                 note="未匹配到画像")
        auto = classify(stats)
        if stats.manual_pin:
            return OpponentRead(
                seat_id=seat_id, key=stats.key,
                display_name=stats.display_name or stats.key,
                archetype=stats.manual_pin, confidence=1.0, n=auto.n,
                vpip=auto.vpip, pfr=auto.pfr, af=auto.af,
                source="pin",
                note=f"manual pin（自动 n={auto.n}，{auto.archetype}）",
                gate="stable",
            )
        if auto.archetype != "unknown" and auto.gate in ("read", "stable"):
            return OpponentRead(
                seat_id=seat_id, key=auto.key, display_name=auto.display_name,
                archetype=auto.archetype, confidence=auto.confidence,
                n=auto.n, vpip=auto.vpip, pfr=auto.pfr, af=auto.af,
                source="auto", note=auto.note, gate=auto.gate)
        return OpponentRead(
            seat_id=seat_id, key=auto.key, display_name=auto.display_name,
            archetype=auto.archetype, confidence=auto.confidence,
            n=auto.n, vpip=auto.vpip, pfr=auto.pfr, af=auto.af,
            source="auto" if auto.archetype != "unknown" else "unknown",
            note=auto.note, gate=auto.gate)

    def read_for(self, opponents: Iterable) -> dict[str, OpponentRead]:
        """按 `seat_id → OpponentRead` 返回本手在局对手的画像。

        输入是 `OpponentSnapshot` 列表（也接受带 seat_id/name 的 mapping）。
        匹配用名字，但返回值一定以 seat_id 为锚 —— 同名/OCR 错名时，seat_id
        才是这一手的唯一身份。
        """
        out: dict[str, OpponentRead] = {}
        if not self.enabled:
            return out
        for opp in opponents or ():
            if isinstance(opp, Mapping):
                seat_id = opp.get("seat_id")
                name = opp.get("name")
            else:
                seat_id = getattr(opp, "seat_id", None)
                name = getattr(opp, "name", None)
            seat = str(seat_id) if seat_id is not None else None
            key = self._resolve_key(name)
            stats = self.players.get(key) if key else None
            out[seat or str(name or "")] = self._read_for_stats(stats, seat)
        return out

    def read_one(self, name: str | None, *,
                 seat_id: str | None = None) -> OpponentRead:
        key = self._resolve_key(name)
        return self._read_for_stats(self.players.get(key) if key else None,
                                    seat_id)

    def archetype_for(self, name: str | None) -> str | None:
        return self.read_one(name).archetype

    def report(self, *, limit: int | None = None) -> list[dict]:
        """离线报告：按样本数降序返回每个玩家的统计与分类。"""
        rows = []
        for stats in sorted(self.players.values(),
                            key=lambda s: (-s.preflop_opps, s.key)):
            read = self._read_for_stats(stats, None)
            rows.append({
                "key": stats.key,
                "name": stats.display_name or stats.key,
                "n": stats.n,
                "hands": int(round(stats.hands)),
                "vpip": round(stats.vpip_pct, 1),
                "pfr": round(stats.pfr_pct, 1),
                "af": round(stats.af, 2) if stats.af is not None else None,
                "threebet": (round(stats.threebet_pct, 1)
                             if stats.threebet_pct is not None else None),
                "postflop_aggr": (round(stats.postflop_aggr_pct, 1)
                                  if stats.postflop_aggr_pct is not None else None),
                "showdowns": int(round(stats.showdowns)),
                "bomb_pot_hands": int(round(stats.bomb_pot_hands)),
                "passive_entry_hands": int(round(stats.passive_entry_hands)),
                "weak_aggressive": (round(stats.weak_aggressive_pct, 1)
                                    if stats.weak_aggressive_pct is not None
                                    else None),
                "archetype": read.archetype,
                "confidence": round(read.confidence, 3),
                "source": read.source,
                "gate": read.gate,
            })
        return rows[:limit] if limit else rows

    # ── 落盘 ──
    def save(self, force: bool = False) -> bool:
        """原子写。默认按「每 10 手 / 5 秒」节流；force=True 立即写。"""
        if not self.enabled or not self._dirty:
            return True
        now_mono = self._monotonic()
        if not force and self._updates < 10 and now_mono - self._last_save < 5.0:
            return True
        self._dirty = False
        self._updates = 0
        self._last_save = now_mono
        data = {
            "version": 1,
            "pool_key": self._pool_key,
            "classifier_version": CLASSIFIER_VERSION,
            "updated_at": _iso_now(self._now),
            "players": {key: stats.to_dict()
                        for key, stats in self.players.items()},
            "aliases": dict(self.aliases),
            "seat_keys": dict(self.seat_keys),
            "seen_hands": list(self.seen_hands[-MAX_SEEN_HANDS:]),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            self.last_save_error = f"写入 {self.path} 失败：{exc}"
            self._dirty = True
            return False
        self.last_save_error = None
        self._saved_at = data["updated_at"]
        return True

    def clear(self, *, pool_key: str | None = None) -> int:
        """清空画像。指定 pool 时只清该池；返回删除的玩家数。"""
        target = pool_key or self._pool_key
        if not target:
            removed = len(self.players)
            self.players.clear()
        else:
            keys = [k for k, s in self.players.items()
                    if not s.pool_key or s.pool_key == target]
            removed = len(keys)
            for key in keys:
                self.players.pop(key, None)
        # 清空的同时丢掉去重/别名状态：否则重建同一批手牌时，
        # seen_hands 会把它们全部当成「已经处理过」而静默跳过。
        self.aliases.clear()
        self.seat_keys.clear()
        self.seen_hands = []
        self._seen = set()
        self._dirty = True
        return removed


# ─────────────────────────── 便捷入口 ───────────────────────────

def load_profile_store(path: str | Path | None = None,
                       pool_key: str | None = None, *,
                       enabled: bool = True) -> ProfileStore:
    """按 `OPPONENT_DB` / 默认路径构造并加载。"""
    env_path = (os.environ.get(OPPONENT_DB_VAR) or "").strip()
    chosen = path or env_path or DEFAULT_PROFILE_PATH
    store = ProfileStore(chosen, pool_key, enabled=enabled)
    store.load()
    return store


def render_reads(reads: Iterable[OpponentRead],
                 positions: Mapping[str, str],
                 *,
                 header: str = ("Opponent reads (per-seat, from local hand history — "
                                "use these before any table-wide label):")) -> list[str]:
    """按座位渲染画像行（英文）；中文 lean 用 `render_reads_zh`。

    `positions` 只含本手还在局的座位（seat_id → 位置）：不在其中（已弃牌）或
    archetype 为 unknown 的画像不输出。tentative 只标注、不当硬结论；排序按
    位置，保证同一决策点的提示词稳定。
    """
    rows: list[tuple[str, OpponentRead]] = []
    for read in reads:
        if read.archetype == "unknown":
            continue
        seat = read.seat_id
        if not seat or seat not in positions:
            continue
        rows.append((positions[seat], read))
    if not rows:
        return []
    rows.sort(key=lambda item: item[0])
    lines = [header] if header else []
    for position, read in rows:
        name = read.display_name or read.key
        gate = " [tentative]" if read.gate == "tentative" else ""
        source = ""
        if read.source == "pin":
            source = ", manual pin"
        elif read.source == "prior":
            source = ", .table_dy prior"
        af = f", AF {read.af:g}" if read.af else ""
        lines.append(
            f"- {position} {name}: {read.archetype.upper()} "
            f"(n={read.n}, VPIP {read.vpip:g}%, PFR {read.pfr:g}%{af}, "
            f"conf {read.confidence:.2f}{source}){gate}")
    return lines


def render_reads_zh(reads: Iterable[OpponentRead],
                    positions: Mapping[str, str],
                    *,
                    header: str = "## 对手画像（逐座位，优先于桌级桌风）") -> list[str]:
    """按座位渲染中文画像行，供中文 lean 事实块使用。

    `positions` 只含本手还在局的座位（seat_id → 位置）：不在其中（已弃牌）或
    archetype 为 unknown 的画像不输出。tentative 只标注、不当硬结论。
    """
    rows: list[tuple[str, OpponentRead]] = []
    for read in reads:
        if read.archetype == "unknown":
            continue
        seat = read.seat_id
        if not seat or seat not in positions:
            continue
        rows.append((positions[seat], read))
    if not rows:
        return []
    rows.sort(key=lambda item: item[0])
    lines = [header] if header else []
    for position, read in rows:
        name = read.display_name or read.key
        gate = " [tentative]" if read.gate == "tentative" else ""
        source = ""
        if read.source == "pin":
            source = "，人工 pin"
        elif read.source == "prior":
            source = "，.table_dy 先验"
        af = f"，AF {read.af:g}" if read.af else ""
        lines.append(
            f"- {position} {name}：{read.archetype.upper()}"
            f"（n={read.n}，VPIP {read.vpip:g}%，PFR {read.pfr:g}%{af}，"
            f"置信 {read.confidence:.2f}{source}）{gate}")
    return lines


def format_read_line(read: OpponentRead, position: str | None = None) -> str:
    """终端/日志用的一行画像。"""
    where = f"{position} " if position else ""
    name = read.display_name or read.key
    if read.archetype == "unknown":
        return f"{where}{name} unknown（n={read.n}，样本不足）"
    tag = " tentative" if read.gate == "tentative" else ""
    return (f"{where}{name} {read.archetype.upper()} {read.confidence:.2f} "
            f"(n={read.n}) VPIP {read.vpip:g} PFR {read.pfr:g} "
            f"AF {read.af:g}{tag}")
