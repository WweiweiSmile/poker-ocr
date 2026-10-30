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
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .positions import POSITIONS_BY_TABLE_SIZE

# 分类器版本：规则/阈值改动时递增，画像文件里会记录，重建时能看出差异。
CLASSIFIER_VERSION = "rules-2026-10-30e"

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


# ── 人群先验（β 收缩）──────────────────────────────────────────
# 单个对手一个月也就几十手，任何「2 次机会弃了 2 次」的原始比例都是噪声。
# 所以低频样本不直接显示原始比例，而是向**全场均值**收缩：
#
#     shrunk = (hit + k * p0) / (opps + k)
#
# k 是「先验相当于几次机会」。k 越大越保守，越不容易被 2/2 带跑。
# 下面的 k 按事件稀有度设：常见事件给 8-10，稀有事件给 4-6（否则永远
# 收缩到先验上，等于没采）。
PRIOR_STRENGTH: dict[str, float] = {
    "open": 8.0,
    "cbet": 8.0,
    "barrel": 6.0,
    "stab": 8.0,
    "check_raise": 5.0,
    "limp": 8.0,
    "fold_to_cbet": 8.0,
    "fold_to_barrel": 5.0,
    "delay_cbet": 5.0,
    "fold_to_3bet": 4.0,
    "threebet": 6.0,
    "call_to_cbet": 8.0,
    "fourbet": 4.0,
    "fold_to_bet_flop": 8.0,
    "fold_to_bet_turn": 6.0,
    "barrel_call": 5.0,
    "fold_to_bet_river": 6.0,
}

# 全场样本不足时的兜底先验（百分比）。数值来自常见现金局的经验值，
# 只用来在「还没有人群数据」时把低频样本拉回地面。
DEFAULT_PRIORS: dict[str, float] = {
    "open": 25.0,
    "cbet": 65.0,
    "barrel": 55.0,
    "stab": 40.0,
    "check_raise": 12.0,
    "limp": 20.0,
    "fold_to_cbet": 45.0,
    "fold_to_barrel": 45.0,
    "delay_cbet": 30.0,
    "fold_to_3bet": 55.0,
    "threebet": 6.0,
    "call_to_cbet": 45.0,
    "fourbet": 5.0,
    "fold_to_bet_flop": 45.0,
    "fold_to_bet_turn": 45.0,
    "barrel_call": 50.0,
    "fold_to_bet_river": 60.0,
}

# 指标 → (命中字段, 机会字段)。人群先验和收缩都靠这张表。
PRIOR_FIELDS: dict[str, tuple[str, str]] = {
    "open": ("opened", "open_opp"),
    "cbet": ("cbets", "cbet_opp"),
    "barrel": ("barrels", "barrel_opp"),
    "stab": ("stabs", "stab_opp"),
    "check_raise": ("check_raises", "check_raise_opp"),
    "limp": ("limped", "limp_opp"),
    "fold_to_cbet": ("ftc_fold", "ftc_opp"),
    "fold_to_barrel": ("ftb_fold", "ftb_opp"),
    "delay_cbet": ("dcb", "dcb_opp"),
    "fold_to_3bet": ("f3b_fold", "f3b_opp"),
    "threebet": ("threebet", "threebet_opps"),
    "call_to_cbet": ("ftc_call", "ftc_opp"),
    "fourbet": ("f3b_4bet", "f3b_opp"),
}


def _field(raw, name: str) -> float:
    """读一个计数字段。`raw` 可能是画像 JSON 的 dict，也可能是 OpponentStats
    对象 —— 两种都要支持，否则人群先验永远退回默认值（实测就是这么静默
    失效的）。"""
    value = (raw.get(name) if isinstance(raw, Mapping)
             else getattr(raw, name, None))
    return float(value) if isinstance(value, (int, float)) else 0.0


def pooled_priors(players: Mapping) -> dict[str, float]:
    """从整份画像算人群先验（按机会数加权的合并比例）。

    只在该指标的全场机会数 >= 20 时采用；否则退回 `DEFAULT_PRIORS` ——
    十来次机会算出来的「全场均值」比经验值更不靠谱。
    """
    priors = dict(DEFAULT_PRIORS)
    for name, (hit_field, opp_field) in PRIOR_FIELDS.items():
        hit = sum(_field(raw, hit_field) for raw in players.values())
        opps = sum(_field(raw, opp_field) for raw in players.values())
        if opps >= 20:
            priors[name] = hit / opps * 100
    return priors


def shrink_pct(hit: float, opps: float, prior_pct: float,
               strength: float) -> float:
    """β 收缩后的比例（百分比）。"""
    if opps <= 0:
        return prior_pct
    return (hit + strength * prior_pct / 100.0) / (opps + strength) * 100


def _pct(hit: float, opps: float, *, min_opps: float = 3.0) -> float | None:
    """命中率；机会数不足返回 None —— 3 次机会算出 0% 或 100% 没有信息量。"""
    if opps < min_opps:
        return None
    return hit / opps * 100


def _dict_field(raw, name: str, street: str) -> float:
    """读「按街存 dict」的计数字段：street → 次数。"""
    mapping = (raw.get(name) if isinstance(raw, Mapping)
               else getattr(raw, name, None))
    if not isinstance(mapping, Mapping):
        return 0.0
    value = mapping.get(street)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _bucket_map(raw) -> dict[str, float]:
    """JSON 里的位置分桶计数：只留数值，别让脏数据把整份画像读崩。"""
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): float(v) for k, v in raw.items()
            if isinstance(v, (int, float))}


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
    # 位置开池：翻前轮到我时前面没人主动入池 → 这是开池机会。
    open_opp: bool = False
    opened: bool = False
    # 翻牌 c-bet：我是翻前最后加注者，且翻牌第一个下注是我。
    cbet_opp: bool = False
    cbet: bool = False
    # 转牌续打：翻牌 c-bet 过之后，转牌还下注。
    barrel_opp: bool = False
    barrel: bool = False
    # 被过牌后下注（stab）：这条街前面所有人过牌，轮到我。
    stab_opp: bool = False
    stab: bool = False
    # 过牌-加注：我先过牌，后面有人下注，我加注。
    check_raise_opp: bool = False
    check_raise: bool = False
    # 溜入：前面无人加注时主动跟注（BB 免费过牌不算机会）。
    limp_opp: bool = False
    limped: bool = False
    # 面对 c-bet：翻牌面对「翻前最后加注者」的第一枪，拆成弃/跟/加。
    ftc_opp: bool = False
    ftc_fold: bool = False
    ftc_call: bool = False
    ftc_raise: bool = False
    # 面对转牌续打：翻牌跟了 c-bet 之后，转牌再面对同一个人的下注。
    ftb_opp: bool = False
    ftb_fold: bool = False
    ftb_call: bool = False
    # 分街面对下注：这条街上**前面有人下注**时自己的反应。比「只统计
    # 同一个人的续打」样本多得多 —— 后者要「翻牌跟注 + 转牌同一个人再开枪」，
    # 实测 193 手是 0 次。
    face_opp: dict = field(default_factory=dict)
    face_fold: dict = field(default_factory=dict)
    face_call: dict = field(default_factory=dict)
    face_raise: dict = field(default_factory=dict)
    # 延迟 c-bet：翻前加注者翻牌过牌、转牌补一枪。
    dcb_opp: bool = False
    dcb: bool = False
    # 面对 3-bet：我开池之后有人再加注，我弃/跟/4-bet。
    f3b_opp: bool = False
    f3b_fold: bool = False
    f3b_call: bool = False
    f3b_4bet: bool = False
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


_POSITION_BUCKETS = {
    "EP": {"UTG", "UTG+1", "UTG+2"},
    "MP": {"LJ", "HJ"},
    "LP": {"CO", "BTN"},
}


def position_bucket(position: str | None) -> str:
    """位置粗分档：EP / MP / LP / 盲注。画像按档统计，不按具体位置 ——
    9 人桌每个具体位置一个 session 只有几手，分到单点全是噪声。"""
    if not position:
        return "?"
    pos = position.strip().upper()
    for bucket, names in _POSITION_BUCKETS.items():
        if pos in names:
            return bucket
    return "盲注" if pos in ("SB", "BB") else "?"


def _position_rank(table_size: int | None) -> dict[str, int]:
    """位置 → 行动顺序（SB 先说话，BTN 最后）。"""
    order = (POSITIONS_BY_TABLE_SIZE.get(int(table_size or 9))
             or POSITIONS_BY_TABLE_SIZE[9])
    return {pos: i for i, pos in enumerate(order)}


def _ordered_street_actions(actions: list[dict],
                            rank: Mapping[str, int]) -> list[dict]:
    """把一条街的行动按**真实下注顺序**重排。

    推断出来的 check/fold 是街末统一追加的，追加顺序不是行动顺序（实测
    一手翻牌记成 BB、UTG、SB… 的乱序）。依赖顺序的线路统计（stab、
    过牌-加注）必须先还原顺序，否则「前面有没有人过牌/下注」全是错的。

    还原办法：按「每个座在这条街的第几次行动」分轮，轮内按位置序排。
    check-raise 也能覆盖：A 的 check 在轮 1、B 的 bet 在轮 1、A 的 raise
    在轮 2 —— 排序后 B 的 bet 会正确落在 A 的 raise 之前。
    """
    seen: dict[str, int] = {}
    keyed = []
    for index, action in enumerate(actions):
        seat = str(action.get("seat_id") or "")
        position = str(action.get("position") or "")
        round_index = seen.get(seat, 0)
        seen[seat] = round_index + 1
        keyed.append(((round_index, rank.get(position, len(rank)), index),
                      action))
    keyed.sort(key=lambda item: item[0])
    return [action for _, action in keyed]


_STREET_BOARD_LEN = {"flop": 3, "turn": 4, "river": 5}


def _reached_street(hand: Mapping, name: str) -> bool:
    """这一手有没有走到某条街 —— 以**公共牌长度**为准。

    记录里可能缺某条街的 `streets` 条目（没动作、或被截断），但牌面不会
    骗人：发了 5 张牌就是走到了河牌。只看 `streets` 会把「翻牌就结束」
    误判成「河牌还在打」。
    """
    need = _STREET_BOARD_LEN.get(name)
    if need is None:
        return False
    if len(hand.get("board") or []) >= need:
        return True
    return _has_street(hand, name)


def _has_street(hand: Mapping, name: str) -> bool:
    """这一手有没有进入过某条街。

    不能看「这条街有没有动作」：街存在但没人下注时动作列表是空的，
    而「有没有转牌」正是判断「翻牌是否已经结束」的依据。
    """
    return any(str(street.get("street") or "") == name
               for street in hand.get("streets") or [])


def _preflop_aggressor(actions: list[dict]) -> str | None:
    """翻前最后一个加注者；没有加注返回 None。"""
    last = None
    for action in actions:
        if action.get("action") in ("bet", "raise"):
            last = str(action.get("seat_id") or "") or None
    return last


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
    # 推断出来的 check/fold 是街末追加的，顺序不是行动顺序；线路统计必须用
    # 还原过顺序的版本（见 _ordered_street_actions 的说明）。
    rank = _position_rank(hand.get("table_size"))
    ordered = {street: _ordered_street_actions(actions.get(street) or [], rank)
               for street in _STREET_ORDER}
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

        # 分街面对下注：按**还原过顺序**的行动线扫，这条街前面有人下注
        # 才轮到「面对下注」。
        #
        # 原来用原始顺序 + 金额水位判断，有两个问题：推断出来的过牌/弃牌
        # 是乱序追加的，水位经常还没抬起来就遇到本座的 fold；金额缺失时
        # 水位永远是 0。结果是分母偏小、出现过 folds > facing_bets 的
        # 不可能比值（实测 10/9）。
        face_opp: dict = {}
        face_fold: dict = {}
        face_call: dict = {}
        face_raise: dict = {}
        board_len = len(board)
        last_street = ("river" if board_len >= 5 else
                       "turn" if board_len == 4 else
                       "flop" if board_len == 3 else
                       next((st for st in reversed(_STREET_ORDER)
                             if _has_street(hand, st)), None))
        for street in _POSTFLOP:
            seq = ordered.get(street) or []
            bet_seen = False
            responded = False
            for action in seq:
                kind = action.get("action")
                mine = str(action.get("seat_id")) == seat_id
                if mine and bet_seen and kind in ("call", "fold", "raise"):
                    face_opp[street] = 1
                    responded = True
                    if kind == "fold":
                        face_fold[street] = 1
                    elif kind == "call":
                        face_call[street] = 1
                    else:
                        face_raise[street] = 1
                    break
                if kind in ("bet", "raise"):
                    bet_seen = True
            # 补推：这条街就是最后一街、前面有人下注、本座没有任何反应，
            # 且牌局正常结束（不是被截断）—— 那只能是弃牌。和 c-bet 的补推
            # 同一条判据，只是推广到每条街。
            if (not responded and bet_seen and street == last_street
                    and not hand.get("partial")
                    and seat_id in (active.get(street) or ())):
                face_opp[street] = 1
                face_fold[street] = 1
        facing = sum(face_opp.values())

        inferred = sum(1 for a in seat_actions if a.get("inferred"))
        inferred_ratio = inferred / len(seat_actions) if seat_actions else 0.0
        weight = (INFERRED_WEIGHT if inferred_ratio > INFERRED_DOWNWEIGHT
                  else 1.0)

        category, aggressive_showdown = _showdown(hand, seat_id, board)
        bet_sizes = _bet_size_ratios(hand, seat_id)

        # ── 位置开池：轮到我时前面没人主动入池（跟注/加注都算入池）──
        prior_pre = (preflop_actions[:first_idx]
                     if first_idx is not None else preflop_actions)
        open_opp = (not passive_entry and bool(preflop)
                    and seat_id in preflop
                    and not any(a.get("action") in ("call", "bet", "raise")
                                for a in prior_pre))
        opened = open_opp and any(_aggr(a) for a in pre_acts)

        # ── c-bet：我是翻前最后加注者，翻牌第一个下注是我 ──
        aggressor = _preflop_aggressor(preflop_actions)
        cbet_opp = cbet = False
        barrel_opp = barrel = False
        if aggressor == seat_id and flop and seat_id in flop:
            flop_acts = ordered.get("flop") or []
            first_bet = next((a for a in flop_acts
                              if a.get("action") in ("bet", "raise")), None)
            cbet_opp = True
            cbet = bool(first_bet
                        and str(first_bet.get("seat_id")) == seat_id)
            if cbet:
                # 续打只在「翻牌下注后真的看到转牌」时才有机会
                turn_acts = ordered.get("turn") or []
                if turn_acts:
                    barrel_opp = True
                    barrel = any(
                        a.get("action") in ("bet", "raise")
                        and str(a.get("seat_id")) == seat_id
                        for a in turn_acts)

        # ── 溜入：前面无人加注时主动跟注。BB 的免费过牌不算机会 ──
        first_pre = pre_acts[0] if pre_acts else None
        limp_opp = (not passive_entry
                    and str(info.get("position") or "").upper() != "BB"
                    and first_pre is not None
                    and not any(_aggr(a) for a in prior_pre))
        limped = bool(limp_opp and first_pre.get("action") == "call")

        # ── 面对 3-bet：自己开的池，被后面的人再加注 ──
        f3b_opp = f3b_fold = f3b_call = f3b_4bet = False
        pre_seq = ordered.get(_PREFLOP) or []
        first_own = next((i for i, a in enumerate(pre_seq)
                          if str(a.get("seat_id")) == seat_id), None)
        if first_own is not None:
            first_action = pre_seq[first_own]
            opened_it = (
                first_action.get("action") in ("bet", "raise")
                and not any(a.get("action") in ("bet", "raise")
                            for a in pre_seq[:first_own]))
            if opened_it:
                later = pre_seq[first_own + 1:]
                re_raise = next(
                    (a for a in later
                     if a.get("action") in ("bet", "raise")
                     and str(a.get("seat_id")) != seat_id), None)
                if re_raise is not None:
                    response = next(
                        (a for a in later[later.index(re_raise) + 1:]
                         if str(a.get("seat_id")) == seat_id), None)
                    if response is not None:
                        f3b_opp = True
                        kind = response.get("action")
                        f3b_fold = kind == "fold"
                        f3b_call = kind == "call"
                        f3b_4bet = kind in ("bet", "raise")
                    elif (not _reached_street(hand, "flop")
                            and not hand.get("partial")):
                        # 被 3-bet 之后没有翻牌、也没有我的下一个动作 ——
                        # 手牌在翻前就结束了，只能是我弃牌。和 c-bet 那条
                        # 一样：弃牌徽章经常在收池动画里被吞掉。
                        f3b_opp = True
                        f3b_fold = True

        # ── 面对 c-bet / 转牌续打 / 延迟 c-bet ──
        flop_seq = ordered.get("flop") or []
        turn_seq = ordered.get("turn") or []
        first_aggr = next((a for a in flop_seq
                           if a.get("action") in ("bet", "raise")), None)
        is_cbet = bool(first_aggr is not None
                       and str(first_aggr.get("seat_id")) == aggressor)
        ftc_opp = ftc_fold = ftc_call = ftc_raise = False
        ftb_opp = ftb_fold = ftb_call = False
        if is_cbet and aggressor != seat_id:
            # 注意别叫 `facing`：下面的 facing_bet 计数用的是同一个变量名，
            # 在这里覆写会让所有机会计数变成一个 list（实测直接崩）。
            facing_actions = flop_seq[flop_seq.index(first_aggr) + 1:]
            mine = next((a for a in facing_actions
                         if str(a.get("seat_id")) == seat_id), None)
            if mine is not None:
                ftc_opp = True
                kind = mine.get("action")
                ftc_fold = kind == "fold"
                ftc_call = kind == "call"
                ftc_raise = kind in ("bet", "raise")
            elif (not _reached_street(hand, "turn") and not hand.get("partial")
                    and seat_id in (flop or ())):
                # 这手在翻牌就结束了，而且没有转牌 —— 有人跟注就一定会发
                # 转牌，所以这个座位只能是弃牌。追踪器没拍到那几帧（发牌
                # 动画把弃牌徽章盖掉了），实测四成 c-bet 属于这种。
                #
                # `partial=True` 是被截断的手牌，不按这个推。
                ftc_opp = True
                ftc_fold = True
                # 只有跟注的人才会在转牌再面对同一个人的续打
                if ftc_call and turn_seq:
                    turn_bet = next((a for a in turn_seq
                                     if a.get("action") in ("bet", "raise")), None)
                    if (turn_bet is not None
                            and str(turn_bet.get("seat_id")) == aggressor):
                        mine_turn = next(
                            (a for a in turn_seq[turn_seq.index(turn_bet) + 1:]
                             if str(a.get("seat_id")) == seat_id), None)
                        if mine_turn is not None:
                            ftb_opp = True
                            kind_turn = mine_turn.get("action")
                            ftb_fold = kind_turn == "fold"
                            ftb_call = kind_turn == "call"

        # ── 延迟 c-bet：翻前加注者翻牌过牌、转牌下注 ──
        dcb_opp = dcb = False
        if aggressor == seat_id and flop_seq:
            mine_flop = next((a for a in flop_seq
                              if str(a.get("seat_id")) == seat_id), None)
            mine_turn = next((a for a in turn_seq
                              if str(a.get("seat_id")) == seat_id), None)
            if (mine_flop is not None and mine_flop.get("action") == "check"
                    and mine_turn is not None):
                dcb_opp = True
                dcb = mine_turn.get("action") in ("bet", "raise")

        # ── 被过牌后下注 / 过牌-加注：按街扫描自己的第一次动作 ──
        stab_opp = stab = False
        check_raise_opp = check_raise = False
        for street in _POSTFLOP:
            seq = ordered.get(street) or []
            mine = next((i for i, a in enumerate(seq)
                         if str(a.get("seat_id")) == seat_id), None)
            if mine is None:
                continue
            before = seq[:mine]
            action = seq[mine].get("action")
            if (not stab_opp
                    and not any(a.get("action") in ("bet", "raise")
                                for a in before)
                    and any(a.get("action") == "check" for a in before)):
                stab_opp = True
                stab = action in ("bet", "raise")
            if not check_raise_opp and action == "check":
                after = seq[mine + 1:]
                if any(a.get("action") in ("bet", "raise") for a in after):
                    check_raise_opp = True
                    check_raise = any(
                        a.get("action") in ("bet", "raise")
                        and str(a.get("seat_id")) == seat_id
                        for a in after)
            if stab_opp and check_raise_opp:
                break

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
            open_opp=open_opp,
            opened=opened,
            cbet_opp=cbet_opp,
            cbet=cbet,
            barrel_opp=barrel_opp,
            barrel=barrel,
            stab_opp=stab_opp,
            stab=stab,
            check_raise_opp=check_raise_opp,
            check_raise=check_raise,
            limp_opp=limp_opp,
            limped=limped,
            ftc_opp=ftc_opp,
            ftc_fold=ftc_fold,
            ftc_call=ftc_call,
            ftc_raise=ftc_raise,
            ftb_opp=ftb_opp,
            ftb_fold=ftb_fold,
            ftb_call=ftb_call,
            face_opp=face_opp,
            face_fold=face_fold,
            face_call=face_call,
            face_raise=face_raise,
            dcb_opp=dcb_opp,
            dcb=dcb,
            f3b_opp=f3b_opp,
            f3b_fold=f3b_fold,
            f3b_call=f3b_call,
            f3b_4bet=f3b_4bet,
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
    # ── 位置开池与翻后线路（用户口径：要能看出「在哪个位置爱加注」和
    #    「x / xc / xr」这些线路，否则只能靠 VPIP/PFR 猜）──
    open_opp: float = 0.0
    opened: float = 0.0
    cbet_opp: float = 0.0
    cbets: float = 0.0
    barrel_opp: float = 0.0
    barrels: float = 0.0
    stab_opp: float = 0.0
    stabs: float = 0.0
    check_raise_opp: float = 0.0
    check_raises: float = 0.0
    open_by_bucket: dict[str, float] = field(default_factory=dict)
    open_opp_by_bucket: dict[str, float] = field(default_factory=dict)
    # 溜入 / 防守端 / 延迟 c-bet
    limp_opp: float = 0.0
    limped: float = 0.0
    ftc_opp: float = 0.0
    ftc_fold: float = 0.0
    ftc_call: float = 0.0
    ftc_raise: float = 0.0
    ftb_opp: float = 0.0
    ftb_fold: float = 0.0
    ftb_call: float = 0.0
    # 分街面对下注：street → 机会/弃/跟/加
    face_opp: dict[str, float] = field(default_factory=dict)
    face_fold: dict[str, float] = field(default_factory=dict)
    face_call: dict[str, float] = field(default_factory=dict)
    face_raise: dict[str, float] = field(default_factory=dict)
    dcb_opp: float = 0.0
    dcb: float = 0.0
    f3b_opp: float = 0.0
    f3b_fold: float = 0.0
    f3b_call: float = 0.0
    f3b_4bet: float = 0.0
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
    def cbet_pct(self) -> float | None:
        return _pct(self.cbets, self.cbet_opp)

    @property
    def barrel_pct(self) -> float | None:
        return _pct(self.barrels, self.barrel_opp)

    @property
    def stab_pct(self) -> float | None:
        """被过牌后下注频率：这条街前面全过牌时他下注的比例。"""
        return _pct(self.stabs, self.stab_opp)

    @property
    def check_raise_pct(self) -> float | None:
        return _pct(self.check_raises, self.check_raise_opp)

    @property
    def limp_pct(self) -> float | None:
        """前面没人加注时主动跟注的比例。BB 的免费过牌不计入机会。"""
        return _pct(self.limped, self.limp_opp)

    @property
    def fold_to_cbet_pct(self) -> float | None:
        return _pct(self.ftc_fold, self.ftc_opp)

    @property
    def call_to_cbet_pct(self) -> float | None:
        return _pct(self.ftc_call, self.ftc_opp)

    @property
    def raise_to_cbet_pct(self) -> float | None:
        return _pct(self.ftc_raise, self.ftc_opp)

    @property
    def fold_to_barrel_pct(self) -> float | None:
        return _pct(self.ftb_fold, self.ftb_opp)

    @property
    def barrel_call_pct(self) -> float | None:
        """翻牌跟了 c-bet 之后，转牌面对同一个人再跟注的比例。"""
        return _pct(self.ftb_call, self.ftb_opp)

    def fold_to_bet_pct(self, street: str) -> float | None:
        """某条街上「前面有人下注」时弃牌的比例。"""
        return _pct(self.face_fold.get(street, 0.0),
                    self.face_opp.get(street, 0.0))

    def call_to_bet_pct(self, street: str) -> float | None:
        return _pct(self.face_call.get(street, 0.0),
                    self.face_opp.get(street, 0.0))

    def face_opps(self, street: str) -> float:
        return self.face_opp.get(street, 0.0)

    @property
    def delay_cbet_pct(self) -> float | None:
        return _pct(self.dcb, self.dcb_opp)

    @property
    def fold_to_3bet_pct(self) -> float | None:
        return _pct(self.f3b_fold, self.f3b_opp)

    @property
    def fourbet_pct(self) -> float | None:
        return _pct(self.f3b_4bet, self.f3b_opp)

    def open_pct(self, bucket: str) -> float | None:
        """某个位置档的开池频率。样本不足返回 None。"""
        return _pct(self.open_by_bucket.get(bucket, 0.0),
                    self.open_opp_by_bucket.get(bucket, 0.0))

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
        self.open_opp += w if obs.open_opp else 0.0
        self.opened += w if obs.opened else 0.0
        self.cbet_opp += w if obs.cbet_opp else 0.0
        self.cbets += w if obs.cbet else 0.0
        self.barrel_opp += w if obs.barrel_opp else 0.0
        self.barrels += w if obs.barrel else 0.0
        self.stab_opp += w if obs.stab_opp else 0.0
        self.stabs += w if obs.stab else 0.0
        self.check_raise_opp += w if obs.check_raise_opp else 0.0
        self.check_raises += w if obs.check_raise else 0.0
        self.limp_opp += w if obs.limp_opp else 0.0
        self.limped += w if obs.limped else 0.0
        self.ftc_opp += w if obs.ftc_opp else 0.0
        self.ftc_fold += w if obs.ftc_fold else 0.0
        self.ftc_call += w if obs.ftc_call else 0.0
        self.ftc_raise += w if obs.ftc_raise else 0.0
        self.ftb_opp += w if obs.ftb_opp else 0.0
        self.ftb_fold += w if obs.ftb_fold else 0.0
        self.ftb_call += w if obs.ftb_call else 0.0
        for target, source in ((self.face_opp, obs.face_opp),
                               (self.face_fold, obs.face_fold),
                               (self.face_call, obs.face_call),
                               (self.face_raise, obs.face_raise)):
            for street, hit in (source or {}).items():
                target[street] = target.get(street, 0.0) + w * float(hit)
        self.dcb_opp += w if obs.dcb_opp else 0.0
        self.dcb += w if obs.dcb else 0.0
        self.f3b_opp += w if obs.f3b_opp else 0.0
        self.f3b_fold += w if obs.f3b_fold else 0.0
        self.f3b_call += w if obs.f3b_call else 0.0
        self.f3b_4bet += w if obs.f3b_4bet else 0.0
        if obs.open_opp:
            bucket = position_bucket(obs.position)
            self.open_opp_by_bucket[bucket] = (
                self.open_opp_by_bucket.get(bucket, 0.0) + w)
            if obs.opened:
                self.open_by_bucket[bucket] = (
                    self.open_by_bucket.get(bucket, 0.0) + w)
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
            "open_opp": _num(self.open_opp),
            "opened": _num(self.opened),
            "cbet_opp": _num(self.cbet_opp),
            "cbets": _num(self.cbets),
            "barrel_opp": _num(self.barrel_opp),
            "barrels": _num(self.barrels),
            "stab_opp": _num(self.stab_opp),
            "stabs": _num(self.stabs),
            "check_raise_opp": _num(self.check_raise_opp),
            "check_raises": _num(self.check_raises),
            "open_by_bucket": {k: _num(v) for k, v in self.open_by_bucket.items()},
            "open_opp_by_bucket": {k: _num(v)
                                   for k, v in self.open_opp_by_bucket.items()},
            "limp_opp": _num(self.limp_opp),
            "limped": _num(self.limped),
            "ftc_opp": _num(self.ftc_opp),
            "ftc_fold": _num(self.ftc_fold),
            "ftc_call": _num(self.ftc_call),
            "ftc_raise": _num(self.ftc_raise),
            "ftb_opp": _num(self.ftb_opp),
            "ftb_fold": _num(self.ftb_fold),
            "ftb_call": _num(self.ftb_call),
            "face_opp": {k: _num(v) for k, v in self.face_opp.items()},
            "face_fold": {k: _num(v) for k, v in self.face_fold.items()},
            "face_call": {k: _num(v) for k, v in self.face_call.items()},
            "face_raise": {k: _num(v) for k, v in self.face_raise.items()},
            "dcb_opp": _num(self.dcb_opp),
            "dcb": _num(self.dcb),
            "f3b_opp": _num(self.f3b_opp),
            "f3b_fold": _num(self.f3b_fold),
            "f3b_call": _num(self.f3b_call),
            "f3b_4bet": _num(self.f3b_4bet),
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
            open_opp=f("open_opp"),
            opened=f("opened"),
            cbet_opp=f("cbet_opp"),
            cbets=f("cbets"),
            barrel_opp=f("barrel_opp"),
            barrels=f("barrels"),
            stab_opp=f("stab_opp"),
            stabs=f("stabs"),
            check_raise_opp=f("check_raise_opp"),
            check_raises=f("check_raises"),
            open_by_bucket=_bucket_map(raw.get("open_by_bucket")),
            open_opp_by_bucket=_bucket_map(raw.get("open_opp_by_bucket")),
            limp_opp=f("limp_opp"),
            limped=f("limped"),
            ftc_opp=f("ftc_opp"),
            ftc_fold=f("ftc_fold"),
            ftc_call=f("ftc_call"),
            ftc_raise=f("ftc_raise"),
            ftb_opp=f("ftb_opp"),
            ftb_fold=f("ftb_fold"),
            ftb_call=f("ftb_call"),
            face_opp=_bucket_map(raw.get("face_opp")),
            face_fold=_bucket_map(raw.get("face_fold")),
            face_call=_bucket_map(raw.get("face_call")),
            face_raise=_bucket_map(raw.get("face_raise")),
            dcb_opp=f("dcb_opp"),
            dcb=f("dcb"),
            f3b_opp=f("f3b_opp"),
            f3b_fold=f("f3b_fold"),
            f3b_call=f("f3b_call"),
            f3b_4bet=f("f3b_4bet"),
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
    # ── 打法倾向：位置开池与翻后线路。None = 样本不够，不猜。──
    open_ep_pct: float | None = None
    open_lp_pct: float | None = None
    cbet_pct: float | None = None
    barrel_pct: float | None = None
    stab_pct: float | None = None
    check_raise_pct: float | None = None
    threebet_pct: float | None = None
    limp_pct: float | None = None
    fold_to_cbet_pct: float | None = None
    call_to_cbet_pct: float | None = None
    fold_to_barrel_pct: float | None = None
    delay_cbet_pct: float | None = None
    fold_to_3bet_pct: float | None = None
    fourbet_pct: float | None = None
    fold_to_bet_flop_pct: float | None = None
    fold_to_bet_turn_pct: float | None = None
    barrel_call_pct: float | None = None
    fold_to_bet_river_pct: float | None = None
    # 哪些指标是被人群先验收缩过的（不是实测值）。渲染时加 ≈。
    prior_dominated: tuple[str, ...] = ()

    @property
    def aggressive(self) -> bool:
        return self.archetype in AGGRESSIVE_ARCHETYPES

    @property
    def passive(self) -> bool:
        return self.archetype in PASSIVE_ARCHETYPES

    def label(self) -> str:
        return ARCHETYPE_NAMES.get(self.archetype, self.archetype)


@dataclass(frozen=True)
class PooledStats:
    """全场合并统计：整份画像按机会数加权合并。

    这是「这张桌子是什么局」的**事实**，用来替代 `.table_dy` 里那些
    「整体偏松 / 多数人不诈唬」的形容词 —— 同一件事，数字比形容词可靠，
    而且不会在换桌之后过期。
    """

    hands: int
    players: int
    vpip: float | None = None
    pfr: float | None = None
    threebet: float | None = None
    limp: float | None = None
    fold_to_cbet: float | None = None
    stab: float | None = None
    cbet: float | None = None
    fold_to_bet_flop: float | None = None
    fold_to_bet_turn: float | None = None
    fold_to_bet_river: float | None = None
    # 各指标的**机会数**。比率必须有机会数托底才能拿去下结论 ——
    # 2 次机会算出来的 100% 不是「全场都是这样」。
    opportunities: dict[str, float] = field(default_factory=dict)
    # 当前桌视图：本手在局人数（含没有画像的人）。`players` 是其中有画像的
    # 人数；两者一起才能说清「6 人里 4 人有数据」。
    total_players: int = 0
    scope: str = "population"          # population / current_table
    # 当前桌的 hands 只能按玩家 hands 取 max 近似（同一手多人重复），
    # 精确去重需要 observation 表。提示词里要标出来，别把近似值当精确值。
    approx_hands: bool = False

    @property
    def loose(self) -> bool | None:
        """入池宽不宽。和 `table_dynamics.is_loose_table` 的语义一致：
        只说「入池宽」，不说被动。"""
        return None if self.vpip is None else self.vpip >= 32.0

    @property
    def passive(self) -> bool | None:
        """加注少不少。只看 3-bet 频率：这一桌 3-bet 少就是被动。"""
        return None if self.threebet is None else self.threebet <= 8.0


# 全场级结论的最低机会数。比逐座位宽松（全场是合并样本），但也不能没有。
TABLE_MIN_OPPS: dict[str, float] = {
    "river": 12.0,
    "fcbet": 20.0,
    "limp": 20.0,
    "threebet": 20.0,
}


def pooled_stats_to_dict(stats: "PooledStats | None") -> dict | None:
    """把 TableStats/PooledStats 序列化成可落库/可重跑的紧凑快照。"""
    if stats is None:
        return None
    return {
        "scope": stats.scope,
        "hands": stats.hands,
        "players": stats.players,
        "total_players": stats.total_players,
        "approx_hands": stats.approx_hands,
        "vpip": stats.vpip,
        "pfr": stats.pfr,
        "threebet": stats.threebet,
        "limp": stats.limp,
        "fold_to_cbet": stats.fold_to_cbet,
        "stab": stats.stab,
        "cbet": stats.cbet,
        "fold_to_bet_flop": stats.fold_to_bet_flop,
        "fold_to_bet_turn": stats.fold_to_bet_turn,
        "fold_to_bet_river": stats.fold_to_bet_river,
        "opportunities": dict(stats.opportunities),
    }


def table_exploit_zh(stats: PooledStats, *, limit: int = 2) -> str:
    """**全场级**的剥削方向：这桌整体该怎么打。

    与逐座位的 `exploit_hint_zh` 是两层：那个回答「这个人怎么打」，这个回答
    「这桌怎么打」。全场是合并样本，比任何单人都稳，所以可以直接下结论；
    但仍然要求机会数达标 —— 2 次机会算出来的 100% 不是「全场都这样」。
    """
    def enough(key: str) -> bool:
        return stats.opportunities.get(key, 0.0) >= TABLE_MIN_OPPS[key]

    hints: list[str] = []
    if (stats.fold_to_bet_river is not None and enough("river")
            and stats.fold_to_bet_river >= 80):
        hints.append("河牌大注基本没人跟 —— 没摊牌价值时可以诈唬收池；"
                     "自己有强牌反而要收小注，指望被跟")
    if (stats.fold_to_cbet is not None and enough("fcbet")
            and stats.fold_to_cbet >= 70):
        hints.append("对 c-bet 弃得多 —— 翻牌可以高频开火，别只拿强牌下注")
    if (stats.limp is not None and enough("limp") and stats.limp >= 40):
        hints.append("全场爱溜入 —— 有位置时多隔离加注")
    if (stats.threebet is not None and enough("threebet")
            and stats.threebet <= 8):
        hints.append("3-bet 很少 —— 开池很少被反击，前位也可以放宽开池")
    return "；".join(hints[:limit])


def _gate(n: int, confidence: float) -> str:
    if n < MIN_SAMPLE:
        return "unknown"
    if n >= STABLE_SAMPLE and confidence >= STABLE_CONFIDENCE:
        return "stable"
    if n >= READ_SAMPLE and confidence >= READ_CONFIDENCE:
        return "read"
    return "tentative"


# 每个倾向指标的最低机会数。低于这个数不给百分比 —— 3 次机会算出的
# 0%/100% 会把模型带向过度剥削。
TENDENCY_MIN_OPPS = {
    "open": 8.0,
    "cbet": 6.0,
    "barrel": 5.0,
    "stab": 6.0,
    "check_raise": 6.0,
    "limp": 8.0,
    "fold_to_cbet": 8.0,
    "fold_to_barrel": 5.0,
    "delay_cbet": 5.0,
    "fold_to_3bet": 5.0,
    "threebet": 6.0,
    "call_to_cbet": 8.0,
    "fourbet": 5.0,
    "fold_to_bet_flop": 8.0,
    "fold_to_bet_turn": 6.0,
    "barrel_call": 5.0,
    "fold_to_bet_river": 8.0,
}


def _tendency(hit: float, opps: float, kind: str,
              priors: Mapping[str, float]) -> tuple[float | None, bool]:
    """(百分比, 是否被先验主导)。

    机会数够了给实测比例；不够但至少有 1 次机会时给收缩后的估计，并标记
    「这条被先验主导」—— 渲染时加 ≈，让模型知道它不是实测值。完全没有
    机会就返回 None（不渲染）。
    """
    if opps <= 0:
        return None, False
    if opps >= TENDENCY_MIN_OPPS[kind]:
        return round(hit / opps * 100, 0), False
    if opps < 1.0:
        return None, False
    prior = priors.get(kind, DEFAULT_PRIORS.get(kind, 50.0))
    strength = PRIOR_STRENGTH.get(kind, 8.0)
    return round(shrink_pct(hit, opps, prior, strength), 0), True


def _tendencies(stats: OpponentStats,
                priors: Mapping[str, float] | None = None
                ) -> tuple[dict, tuple[str, ...]]:
    """位置开池与翻后线路。低频样本向人群先验收缩，并记下哪些被收缩过。"""
    priors = priors or DEFAULT_PRIORS
    fields: dict = {}
    low: list[str] = []

    def put(name: str, hit: float, opps: float, kind: str | None = None) -> None:
        pct, used_prior = _tendency(hit, opps, kind or name, priors)
        if pct is None:
            return
        fields[f"{name}_pct"] = pct
        if used_prior:
            low.append(name)

    put("open_ep", stats.open_by_bucket.get("EP", 0.0),
        stats.open_opp_by_bucket.get("EP", 0.0), "open")
    put("open_lp", stats.open_by_bucket.get("LP", 0.0),
        stats.open_opp_by_bucket.get("LP", 0.0), "open")
    put("threebet", stats.threebet, stats.threebet_opps)
    put("cbet", stats.cbets, stats.cbet_opp)
    put("barrel", stats.barrels, stats.barrel_opp)
    put("stab", stats.stabs, stats.stab_opp)
    put("check_raise", stats.check_raises, stats.check_raise_opp)
    put("limp", stats.limped, stats.limp_opp)
    put("fold_to_cbet", stats.ftc_fold, stats.ftc_opp)
    put("call_to_cbet", stats.ftc_call, stats.ftc_opp)
    put("fold_to_barrel", stats.ftb_fold, stats.ftb_opp)
    put("delay_cbet", stats.dcb, stats.dcb_opp)
    put("fold_to_3bet", stats.f3b_fold, stats.f3b_opp)
    put("fourbet", stats.f3b_4bet, stats.f3b_opp)
    put("fold_to_bet_flop", stats.face_fold.get("flop", 0.0),
        stats.face_opps("flop"), "fold_to_bet_flop")
    put("fold_to_bet_turn", stats.face_fold.get("turn", 0.0),
        stats.face_opps("turn"), "fold_to_bet_turn")
    put("barrel_call", stats.ftb_call, stats.ftb_opp, "barrel_call")
    put("fold_to_bet_river", stats.face_fold.get("river", 0.0),
        stats.face_opps("river"), "fold_to_bet_river")
    return fields, tuple(low)


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


def classify(stats: OpponentStats,
             priors: Mapping[str, float] | None = None) -> OpponentRead:
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
    fields, prior_dominated = _tendencies(stats, priors)
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
        **fields,
        prior_dominated=prior_dominated,
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
        """记录牌桌池，仅用于日志/元数据；**不参与玩家身份**。"""
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
        for raw_key, entry in (data.get("players") or {}).items():
            if not isinstance(entry, Mapping):
                continue
            key = str(raw_key)
            stats = OpponentStats.from_dict(key, entry)
            # 旧文件里可能有「名字@池」的分区键。现在身份全局合并：把 @ 后面的
            # 池后缀去掉，统计并入裸名字。pool_key 只保留为元数据。
            if "@" in key:
                base = key.split("@", 1)[0]
                existing = self.players.get(base)
                if existing is None:
                    stats.key = base
                    self.players[base] = stats
                else:
                    self._merge_stats(existing, stats)
                self._dirty = True
            else:
                self.players[key] = stats
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
        # 池键仍然从这一手的盲注推导，但它只是观察/手牌的元数据，不再参与
        # 玩家身份。同名玩家跨池合并到同一个 key（见 docs/桌子动态重构...）。
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
            # 全局身份：不再按 pool 分键。pool 只记录最近一次见到的桌型，
            # 供日志/元数据使用。
            key = obs.opponent_key
            pool = obs.pool_key or self._pool_key
            if not self._pool_key and pool:
                self._pool_key = pool
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
        if not target.display_name and old.display_name:
            target.display_name = old.display_name
        if not target.pool_key and old.pool_key:
            target.pool_key = old.pool_key
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
        target.open_opp += old.open_opp
        target.opened += old.opened
        target.cbet_opp += old.cbet_opp
        target.cbets += old.cbets
        target.barrel_opp += old.barrel_opp
        target.barrels += old.barrels
        target.stab_opp += old.stab_opp
        target.stabs += old.stabs
        target.check_raise_opp += old.check_raise_opp
        target.check_raises += old.check_raises
        target.limp_opp += old.limp_opp
        target.limped += old.limped
        target.ftc_opp += old.ftc_opp
        target.ftc_fold += old.ftc_fold
        target.ftc_call += old.ftc_call
        target.ftc_raise += old.ftc_raise
        target.ftb_opp += old.ftb_opp
        target.ftb_fold += old.ftb_fold
        target.ftb_call += old.ftb_call
        for target_map, source_map in ((target.face_opp, old.face_opp),
                                       (target.face_fold, old.face_fold),
                                       (target.face_call, old.face_call),
                                       (target.face_raise, old.face_raise)):
            for street, value in source_map.items():
                target_map[street] = target_map.get(street, 0.0) + value
        target.dcb_opp += old.dcb_opp
        target.dcb += old.dcb
        target.f3b_opp += old.f3b_opp
        target.f3b_fold += old.f3b_fold
        target.f3b_call += old.f3b_call
        target.f3b_4bet += old.f3b_4bet
        for bucket, value in old.open_by_bucket.items():
            target.open_by_bucket[bucket] = (
                target.open_by_bucket.get(bucket, 0.0) + value)
        for bucket, value in old.open_opp_by_bucket.items():
            target.open_opp_by_bucket[bucket] = (
                target.open_opp_by_bucket.get(bucket, 0.0) + value)
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
    def pooled(self) -> PooledStats:
        """全场合并统计（按机会加权），只用于冷启动先验。

        不是「当前桌」统计 —— 当前桌用 `pooled_for_stats()`。这个口径保留
        给 `.table_dy` 兜底和 `population_prior`，避免新桌第一手完全没数据。
        """
        return self._pooled(
            list(self.players.values()),
            hands=len(self.seen_hands),
            scope="population",
        )

    def pooled_for_stats(self, stats: Iterable[OpponentStats], *,
                         total_players: int = 0) -> PooledStats:
        """当前手在局对手的聚合统计。

        `total_players` 是本手在局人数（含没有画像的人）；`players` 是其中
        真正有画像的人数。`hands` 用玩家 hands 的最大值近似 —— 同一手牌会
        让多个玩家各记一次，求和会重复计数。精确去重等 observation 表。
        """
        players = [raw for raw in stats if raw is not None]
        hands = max(
            (int(round(float(raw.hands or 0.0))) for raw in players),
            default=0,
        )
        return self._pooled(
            players,
            hands=hands,
            scope="current_table",
            total_players=max(int(total_players or 0), len(players)),
            approx_hands=True,
        )

    def _pooled(self, players: list[OpponentStats], *, hands: int,
                scope: str, total_players: int = 0,
                approx_hands: bool = False) -> PooledStats:
        """按机会加权合并一组玩家。调用方决定 hands/scope 的口径。"""

        def agg(hit_field: str, opp_field: str) -> float | None:
            hit = sum(_field(raw, hit_field) for raw in players)
            opps = sum(_field(raw, opp_field) for raw in players)
            return hit / opps * 100 if opps > 0 else None

        def street_fold(street: str) -> float | None:
            hit = sum(_dict_field(raw, "face_fold", street) for raw in players)
            opps = sum(_dict_field(raw, "face_opp", street) for raw in players)
            return hit / opps * 100 if opps > 0 else None

        return PooledStats(
            hands=int(hands),
            players=len(players),
            vpip=agg("vpip", "preflop_opps"),
            pfr=agg("pfr", "preflop_opps"),
            threebet=agg("threebet", "threebet_opps"),
            limp=agg("limped", "limp_opp"),
            fold_to_cbet=agg("ftc_fold", "ftc_opp"),
            stab=agg("stabs", "stab_opp"),
            cbet=agg("cbets", "cbet_opp"),
            fold_to_bet_flop=street_fold("flop"),
            fold_to_bet_turn=street_fold("turn"),
            fold_to_bet_river=street_fold("river"),
            opportunities={
                "flop": sum(_dict_field(raw, "face_opp", "flop")
                            for raw in players),
                "turn": sum(_dict_field(raw, "face_opp", "turn")
                            for raw in players),
                "river": sum(_dict_field(raw, "face_opp", "river")
                             for raw in players),
                "cbet": sum(_field(raw, "cbet_opp") for raw in players),
                "fcbet": sum(_field(raw, "ftc_opp") for raw in players),
                "limp": sum(_field(raw, "limp_opp") for raw in players),
                "threebet": sum(_field(raw, "threebet_opps") for raw in players),
            },
            total_players=max(int(total_players or 0), len(players)),
            scope=scope,
            approx_hands=approx_hands,
        )

    def _priors(self) -> dict[str, float]:
        """整份画像算出的全场先验；每次现算，避免「更新了画像但先验没刷新」。"""
        return pooled_priors(self.players)

    def _read_for_stats(self, stats: OpponentStats | None,
                        seat_id: str | None) -> OpponentRead:
        if stats is None:
            return _unknown_read(None, seat_id=seat_id,
                                 note="未匹配到画像")
        auto = classify(stats, self._priors())
        # 用 replace 而不是重列字段：以后再加倾向指标时，这里不会静默丢字段。
        if stats.manual_pin:
            return replace(
                auto, seat_id=seat_id,
                display_name=stats.display_name or stats.key,
                archetype=stats.manual_pin, confidence=1.0,
                source="pin",
                note=f"manual pin（自动 n={auto.n}，{auto.archetype}）",
                gate="stable")
        if auto.archetype != "unknown" and auto.gate in ("read", "stable"):
            return replace(auto, seat_id=seat_id, source="auto")
        return replace(
            auto, seat_id=seat_id,
            source="auto" if auto.archetype != "unknown" else "unknown")

    def stats_for_opponents(self, opponents: Iterable) -> list[OpponentStats]:
        """把本手在局对手解析成 `OpponentStats`，同名只返回一次。

        这是「当前桌统计」的入口：只取 `point.opponents`（本手发牌/在局的人），
        不再读整个 `self.players`。名字匹配走 `_resolve_key`，因此 alias /
        OCR 纠正后的键也能对上。
        """
        out: list[OpponentStats] = []
        if not self.enabled:
            return out
        seen: set[str] = set()
        for opp in opponents or ():
            if isinstance(opp, Mapping):
                name = opp.get("name")
            else:
                name = getattr(opp, "name", None)
            key = self._resolve_key(name)
            if not key:
                continue
            stats = self.players.get(key)
            if stats is None or stats.key in seen:
                continue
            seen.add(stats.key)
            out.append(stats)
        return out

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
        gate = "（暂定）" if read.gate == "tentative" else ""
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
                    header: str = "## 对手画像（逐座位，优先于全场统计/人工观察）") -> list[str]:
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
        gate = "（暂定）" if read.gate == "tentative" else ""
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
        profile = render_tendencies_zh(read)
        if profile:
            lines.append(f"  打法：{profile}")
        exploit = exploit_hint_zh(read)
        if exploit:
            lines.append(f"  可剥削：{exploit}")
    return lines


def _mark(read: OpponentRead, names: tuple[str, ...], text: str) -> str:
    """被先验收缩过的指标加 ≈：让模型知道这是估计值，不是实测。"""
    return f"≈{text}" if any(n in read.prior_dominated for n in names) else text


def render_tendencies_zh(read: OpponentRead) -> str:
    """位置开池 + 翻后线路的一行摘要；没有足够样本时返回空串。

    `≈` 表示这一项样本不够、已向全场均值收缩 —— 是估计值不是实测值。
    """
    parts: list[str] = []
    opens = []
    if read.open_ep_pct is not None:
        opens.append(f"EP {read.open_ep_pct:.0f}%")
    if read.open_lp_pct is not None:
        opens.append(f"LP {read.open_lp_pct:.0f}%")
    if opens:
        parts.append(_mark(read, ("open_ep", "open_lp"),
                           "开池 " + " / ".join(opens)))
    if read.threebet_pct is not None:
        parts.append(_mark(read, ("threebet",), f"3-bet {read.threebet_pct:.0f}%"))
    if read.cbet_pct is not None:
        parts.append(_mark(read, ("cbet",), f"翻牌 c-bet {read.cbet_pct:.0f}%"))
    if read.barrel_pct is not None:
        parts.append(_mark(read, ("barrel",), f"转牌续打 {read.barrel_pct:.0f}%"))
    if read.stab_pct is not None:
        parts.append(_mark(read, ("stab",),
                           f"被过牌后下注 {read.stab_pct:.0f}%"))
    if read.check_raise_pct is not None:
        parts.append(_mark(read, ("check_raise",),
                           f"过牌-加注 {read.check_raise_pct:.0f}%"))
    if read.limp_pct is not None:
        parts.append(_mark(read, ("limp",), f"溜入 {read.limp_pct:.0f}%"))
    if read.fold_to_cbet_pct is not None:
        seg = f"对 c-bet 弃 {read.fold_to_cbet_pct:.0f}%"
        if read.call_to_cbet_pct is not None:
            seg += f" / 跟 {read.call_to_cbet_pct:.0f}%"
        parts.append(_mark(read, ("fold_to_cbet", "call_to_cbet"), seg))
    if read.fold_to_barrel_pct is not None:
        parts.append(_mark(read, ("fold_to_barrel",),
                           f"转牌再弃 {read.fold_to_barrel_pct:.0f}%"))
    if read.delay_cbet_pct is not None:
        parts.append(_mark(read, ("delay_cbet",),
                           f"延迟 c-bet {read.delay_cbet_pct:.0f}%"))
    if (read.fold_to_bet_flop_pct is not None
            or read.fold_to_bet_turn_pct is not None):
        # 逐街单独标记：河牌样本通常比翻/转少，不能因为河牌是估计值就
        # 把整段都标成 ≈。
        seg = []
        for street, name, value in (
                ("翻牌", "fold_to_bet_flop", read.fold_to_bet_flop_pct),
                ("转牌", "fold_to_bet_turn", read.fold_to_bet_turn_pct),
                ("河牌", "fold_to_bet_river", read.fold_to_bet_river_pct)):
            if value is not None:
                seg.append(_mark(read, (name,), f"{street} {value:.0f}%"))
        parts.append("面对下注弃 " + " / ".join(seg))
    if read.barrel_call_pct is not None:
        parts.append(_mark(read, ("barrel_call",),
                           f"翻牌跟注→转牌再跟 {read.barrel_call_pct:.0f}%"))
    if read.fold_to_3bet_pct is not None:
        seg = f"被 3-bet 弃 {read.fold_to_3bet_pct:.0f}%"
        if read.fourbet_pct is not None:
            seg += f" / 4-bet {read.fourbet_pct:.0f}%"
        parts.append(_mark(read, ("fold_to_3bet", "fourbet"), seg))
    return "｜".join(parts)


def _confident(read: OpponentRead, *names: str) -> bool:
    """这条结论能不能拿去剥削：必须是实测值，不能是先验收缩出来的估计。"""
    return all(name not in read.prior_dominated for name in names)


def exploit_hint_zh(read: OpponentRead, *, limit: int = 2) -> str:
    """把倾向翻译成**可执行的剥削方向**，最多两条。

    只采信**实测**指标：被先验收缩过的（低频样本）不产生指令 —— 那只是
    估计，用来理解对手可以，用来下注不行。中间值两边都不站，也不说。
    """
    hints: list[str] = []

    if _confident(read, "stab") and read.stab_pct is not None:
        if read.stab_pct <= 25:
            hints.append("被过牌后很少下注 —— 他过牌多半没牌，过牌到你时可以更主动偷池")
        elif read.stab_pct >= 60:
            hints.append("被过牌后爱下注 —— 过牌给他要准备 check-raise，别用中等牌白跟")

    if (_confident(read, "check_raise")
            and read.check_raise_pct is not None
            and read.check_raise_pct >= 20):
        hints.append("过牌-加注频繁 —— 别对他的过牌随手开火")

    if _confident(read, "cbet") and read.cbet_pct is not None:
        if read.cbet_pct >= 75:
            hints.append("c-bet 频率高 —— 可以多 float，转牌他再开枪再决定")
        elif read.cbet_pct <= 40:
            hints.append("c-bet 频率低 —— 翻牌他过牌基本是没中，可以在转牌接管控池")

    if _confident(read, "fold_to_cbet") and read.fold_to_cbet_pct is not None:
        if read.fold_to_cbet_pct >= 60:
            hints.append("对 c-bet 弃得多 —— 干燥面可以高频开火，别只拿强牌下注")
        elif read.fold_to_cbet_pct <= 30:
            hints.append("对 c-bet 跟得多 —— 别用空气开火，价值牌打大")

    if (_confident(read, "barrel_call")
            and read.barrel_call_pct is not None
            and read.barrel_call_pct >= 60):
        hints.append("翻牌跟注后转牌还会跟 —— 第二枪别用空气开，等真牌再打")

    if (_confident(read, "fold_to_bet_turn")
            and read.fold_to_bet_turn_pct is not None):
        if read.fold_to_bet_turn_pct >= 65:
            hints.append("转牌弃得多 —— 翻牌跟注之后可以连开第二枪")
        elif read.fold_to_bet_turn_pct <= 40:
            hints.append("转牌很少弃 —— 第二枪要拿真牌，别拿空气硬开")

    if (_confident(read, "fold_to_bet_river")
            and read.fold_to_bet_river_pct is not None):
        if read.fold_to_bet_river_pct >= 80:
            hints.append("河牌面对下注弃得多 —— 河牌下注很少被跟，"
                         "没摊牌价值的牌可以在河牌开一枪")
        elif read.fold_to_bet_river_pct <= 40:
            hints.append("河牌跟得多 —— 河牌诈唬无效，只用价值下注")

    if (_confident(read, "limp") and read.limp_pct is not None
            and read.limp_pct >= 25):
        hints.append("爱溜入 —— 他的溜入范围偏弱，有位置时可以直接隔离加注")

    if (_confident(read, "delay_cbet") and read.delay_cbet_pct is not None
            and read.delay_cbet_pct >= 40):
        hints.append("翻牌过牌不代表放弃 —— 他会在转牌补一枪，别用中等牌白跟")

    if (_confident(read, "fold_to_3bet")
            and read.fold_to_3bet_pct is not None
            and read.fold_to_3bet_pct >= 60):
        # 这才是挤压的真实判据：他开池后被 3-bet 会弃。
        hints.append("被 3-bet 就弃 —— 他开池后可以直接挤压，"
                     "身后都弃牌时成功率高")
    elif (_confident(read, "open_lp") and read.open_lp_pct is not None
            and read.open_lp_pct >= 40
            and (read.threebet_pct is None or read.threebet_pct <= 8)):
        # 兜底：没采到「被 3-bet 的弃牌率」时，用开池宽度做代理。
        hints.append("后位开池宽、3-bet 少 —— 他开池的范围偏弱，"
                     "身后没人入池时可以有一定频率 3-bet 挤压"
                     "（还没采到被 3-bet 的弃牌率）")

    return "；".join(hints[:limit])


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
