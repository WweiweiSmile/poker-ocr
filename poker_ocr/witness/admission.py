"""摊牌证据准入与族 / 局面派生（#12 / #19 / #38）。

只做**投影**：手牌 JSON → 证据行或拒绝原因。不写盘、不读盘、不调用 LLM，也不
改 poker_ocr/opponent_profile.py 里旧的摊牌统计（_showdown / showdown_reads）。

口径（docs/摊牌-witness-数据契约-v1.md §3–§5）：
- 证据侧 fail closed：判不了就 rejected 或不生成行，绝不猜。
- 主链路 fail open：本模块的公开函数不向上抛异常，调用方按「没有这条证据」处理。
- 只投影最后街是 turn/river 的手；只投影非 hero 座位；同手同座最多一行。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..coach import (
    HIGH_CARD,
    ONE_PAIR,
    RANK_CHARS,
    TRIPS,
    TWO_PAIR,
    evaluate,
    parse_cards,
)

# ---------------------------------------------------------------- 版本与枚举

WITNESS_SCHEMA = "witness-2026-11-14a"

STATUS_ADMITTED = "admitted"
STATUS_TAGGED = "tagged"
STATUS_REJECTED = "rejected"

REASON_FOLD_INFERRED = "fold_inferred"
REASON_NOT_IN_STREET_ACTIVE = "not_in_street_active"
REASON_BOARD_SHORT = "board_short"
REASON_HAND_MISMATCH = "hand_mismatch"

SHOW_SHOWDOWN = "showdown"
SHOW_VOLUNTARY = "voluntary"
SHOW_SINGLE = "single"

WINNER_DERIVED = "derived"
WINNER_UNKNOWN = "unknown"
WINNER_SINGLE_SHOW = "single_show"

STREET_TURN = "turn"
STREET_RIVER = "river"

ACTION_FACING_BET = "facing_bet"
ACTION_CHECK = "check"
ACTION_BET = "bet"
ACTION_UNKNOWN = "unknown"

POSITION_IP = "ip"
POSITION_OOP = "oop"

# 12 族：判定层（#12），渲染时按 6 组去重。
FAMILIES: tuple[str, ...] = (
    "weak_ace_top_pair",
    "strong_ace_top_pair",
    "non_ace_top_pair",
    "overpair",
    "middle_pair",
    "small_pair",
    "air_high_card",
    "suited_connector",
    "busted_draw",
    "two_pair_plus",
    "set",
    "playing_board",
)

FAMILY_ZH: dict[str, str] = {
    "weak_ace_top_pair": "弱A顶对",
    "strong_ace_top_pair": "A顶对强踢",
    "non_ace_top_pair": "非A顶对",
    "overpair": "超对",
    "middle_pair": "中等对子",
    "small_pair": "小对子",
    "air_high_card": "高张无对",
    "suited_connector": "同花连张",
    "busted_draw": "听牌破产",
    "two_pair_plus": "两对+",
    "set": "暗三",
    "playing_board": "打牌面",
}

# 同组的两族不在一张桌上重复出现（#12「渲染层 6 组」）。
FAMILY_RENDER_GROUP: dict[str, str] = {
    "weak_ace_top_pair": "weak_ace_top_pair",
    "strong_ace_top_pair": "strong_top_pair",
    "non_ace_top_pair": "strong_top_pair",
    "overpair": "pocket_core",
    "middle_pair": "pocket_core",
    "small_pair": "small_and_board",
    "playing_board": "small_and_board",
    "air_high_card": "draws_and_air",
    "suited_connector": "draws_and_air",
    "busted_draw": "draws_and_air",
    "two_pair_plus": "two_pair_plus",
    "set": "two_pair_plus",
}

_STREET_ORDER = ("preflop", "flop", "turn", "river")

# 9-max 翻后行动顺序（#14）；6-max 的位置名同样落在这张表里。
_POSITION_INDEX: dict[str, int] = {
    "SB": 0,
    "BB": 1,
    "UTG": 2,
    "UTG+1": 3,
    "UTG+2": 4,
    "LJ": 5,
    "HJ": 6,
    "CO": 7,
    "BTN": 8,
}

_ACTION_MAP = {
    "bet": ACTION_BET,
    "raise": ACTION_FACING_BET,
    "call": ACTION_FACING_BET,
    "check": ACTION_CHECK,
}

_ACE = RANK_CHARS.index("A")        # 12
_TEN = RANK_CHARS.index("T")        # 8
_SEVEN = RANK_CHARS.index("7")      # 5

_DATED_HAND_RE = re.compile(r"(\d{8}-[0-9a-f]{6,})$")
_HAND_ID_RE = re.compile(r"^\d{8}-[0-9a-f]{6,}$")

_ROW_FIELDS = (
    "player",
    "hand",
    "seat",
    "ts",
    "street",
    "action",
    "position",
    "cards",
    "board",
    "family",
    "show_kind",
    "status",
    "winner",
    "source",
    "table",
)


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class WinnerEvidence:
    """证据行里的 winner 三态（§3.2）：只进文字，不进频率。"""

    status: str
    seat: str | None = None
    family: str | None = None


@dataclass(frozen=True)
class Admission:
    status: str
    reason: str | None
    show_kind: str | None
    cards: tuple[str, ...]
    winner: WinnerEvidence
    family: str | None

    @property
    def accepted(self) -> bool:
        return self.status in (STATUS_ADMITTED, STATUS_TAGGED)


# ---------------------------------------------------------------- 小工具


def _cards_of(show: Mapping[str, Any]) -> tuple[str, ...]:
    raw = show.get("cards") or ()
    return tuple(str(c) for c in raw if c)


def _rank(card: str) -> int:
    return parse_cards(card)[0][0]


def _suit(card: str) -> str:
    return parse_cards(card)[0][1]


def _pocket(hole: Sequence[str]) -> bool:
    return len(hole) == 2 and _rank(hole[0]) == _rank(hole[1])


def _category(cards: Sequence[str], board: Sequence[str]) -> tuple[int, tuple[int, ...]] | None:
    try:
        category, kickers = evaluate(parse_cards("".join(list(cards) + list(board[:5]))))
    except (TypeError, ValueError, IndexError):
        return None
    return category, tuple(kickers or ())


def _busted_draw(hole: Sequence[str], board: Sequence[str]) -> bool:
    """#12 的启发式：花听（手牌同花、牌面该花色恰 2 张）或顺听（5 连窗口缺 1）。"""
    hole_ranks = [_rank(c) for c in hole]
    board_ranks = [_rank(c) for c in board]
    hole_suits = [_suit(c) for c in hole]
    board_suits = [_suit(c) for c in board]
    if hole_suits[0] == hole_suits[1] and len(board) >= 3:
        if sum(1 for s in board_suits if s == hole_suits[0]) == 2:
            return True
    all_ranks = set(hole_ranks) | set(board_ranks)
    for low in range(0, 9):
        window = set(range(low, low + 5))
        if window & set(hole_ranks) and len(window & all_ranks) >= 4:
            return True
    return False


def classify_family(cards: Sequence[str], board: Sequence[str]) -> str | None:
    """按 #12 §2.3 自上而下判定 12 族；判不了返回 None（证据侧 fail closed）。"""
    hole = [str(c) for c in cards]
    board = [str(c) for c in board][:5]
    if len(hole) != 2 or len(board) < 3:
        return None
    try:
        hole_ranks = [_rank(c) for c in hole]
        board_ranks = [_rank(c) for c in board]
    except (TypeError, ValueError, IndexError):
        return None
    got = _category(hole, board)
    if got is None:
        return None
    category = got[0]

    if category >= TWO_PAIR:
        if category == TRIPS and _pocket(hole):
            return "set"
        return "two_pair_plus"

    if category == ONE_PAIR:
        counts = Counter(hole_ranks + board_ranks)
        pair_rank = max(r for r, n in counts.items() if n >= 2)
        board_top = max(board_ranks)
        if _pocket(hole):
            if pair_rank > board_top:
                return "overpair"
            return "middle_pair" if pair_rank >= _SEVEN else "small_pair"
        if not any(r == pair_rank for r in hole_ranks):
            return "playing_board"
        if pair_rank == board_top:
            if _ACE in hole_ranks:
                other = hole_ranks[1] if hole_ranks[0] == _ACE else hole_ranks[0]
                return "strong_ace_top_pair" if other >= _TEN else "weak_ace_top_pair"
            return "non_ace_top_pair"
        board_second = sorted(set(board_ranks), reverse=True)
        if len(board_second) >= 2 and pair_rank == board_second[1]:
            return "middle_pair"
        return "small_pair"

    if category == HIGH_CARD:
        if _busted_draw(hole, board):
            return "busted_draw"
        try:
            same_suit = _suit(hole[0]) == _suit(hole[1])
        except (TypeError, ValueError, IndexError):
            same_suit = False
        if same_suit and abs(hole_ranks[0] - hole_ranks[1]) <= 2:
            return "suited_connector"
        return "air_high_card"

    return None


# ---------------------------------------------------------------- 手牌读取


def _streets(hand: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = hand.get("streets") or []
    return [st for st in raw if isinstance(st, Mapping)]


def _actions_in_street(hand: Mapping[str, Any], street: str) -> list[Mapping[str, Any]]:
    for st in _streets(hand):
        if str(st.get("street")) == street:
            return [a for a in (st.get("actions") or []) if isinstance(a, Mapping)]
    return []


def last_street(hand: Mapping[str, Any]) -> str | None:
    present = {str(st.get("street")) for st in _streets(hand)}
    for name in reversed(_STREET_ORDER):
        if name in present:
            return name
    return None


def _street_active(hand: Mapping[str, Any]) -> dict[str, set[str]]:
    raw = hand.get("street_active") or {}
    out: dict[str, set[str]] = {}
    if not isinstance(raw, Mapping):
        return out
    for street, seats in raw.items():
        if isinstance(seats, (list, tuple, set)):
            out[str(street)] = {str(s) for s in seats if s}
    return out


def _last_action_by_seat(hand: Mapping[str, Any]) -> dict[str, str]:
    last: dict[str, str] = {}
    for st in _streets(hand):
        for action in st.get("actions") or []:
            if not isinstance(action, Mapping):
                continue
            seat = action.get("seat_id")
            name = action.get("action")
            if seat and name:
                last[str(seat)] = str(name)
    return last


def hero_seat(hand: Mapping[str, Any]) -> str | None:
    seat = hand.get("hero_seat_id") or hand.get("hero_seat")
    if seat:
        return str(seat)
    for st in _streets(hand):
        for action in st.get("actions") or []:
            if isinstance(action, Mapping) and action.get("is_hero") and action.get("seat_id"):
                return str(action["seat_id"])
    return None


def _hero_cards(hand: Mapping[str, Any]) -> tuple[str, ...]:
    raw = hand.get("hero_cards") or ()
    return tuple(str(c) for c in raw if c)


def _shows(hand: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for show in hand.get("shows") or []:
        if not isinstance(show, Mapping):
            continue
        seat = show.get("seat_id")
        if seat:
            out[str(seat)] = show
    return out


def _seat_position(hand: Mapping[str, Any], seat: str) -> str | None:
    for meta in hand.get("seat_meta") or []:
        if not isinstance(meta, Mapping):
            continue
        if str(meta.get("seat_id")) == seat and meta.get("position"):
            return str(meta["position"])
    show = _shows(hand).get(seat)
    if show and show.get("position"):
        return str(show["position"])
    return None


def position_of(hand: Mapping[str, Any], seat: str) -> str | None:
    """相对 hero 的翻后行动顺序：IP / OOP；任一位置缺失或不在 9-max 表里 → None。"""
    hero = hero_seat(hand)
    if hero is None or hero == seat:
        return None
    hero_pos = _seat_position(hand, hero)
    seat_pos = _seat_position(hand, seat)
    if hero_pos not in _POSITION_INDEX or seat_pos not in _POSITION_INDEX:
        return None
    return POSITION_IP if _POSITION_INDEX[seat_pos] > _POSITION_INDEX[hero_pos] else POSITION_OOP


def action_of(hand: Mapping[str, Any], seat: str, street: str) -> str | None:
    """最后街该座位的最后动作 → §3.2 四值；fold 返回 None（该座位不出行）。"""
    last: str | None = None
    for action in _actions_in_street(hand, street):
        if str(action.get("seat_id")) != seat:
            continue
        name = action.get("action")
        if name:
            last = str(name)
    if last is None:
        return ACTION_UNKNOWN
    return _ACTION_MAP.get(last)


def hand_ending(hand: Mapping[str, Any]) -> str:
    board = hand.get("board") or ()
    if len(board) < 5:
        return "board<5"
    river = _actions_in_street(hand, "river")
    if not river:
        return "无河牌动作"
    names = [str(a.get("action")) for a in river]
    aggressive = any(n in ("bet", "raise") for n in names)
    called = any(n == "call" for n in names)
    if aggressive and called:
        return "下注-跟注"
    if aggressive:
        return "下注-无人跟"
    if all(n == "check" for n in names):
        return "过牌到底"
    return "混合"


def show_kind(hand: Mapping[str, Any], card_count: int) -> str:
    if card_count == 1:
        return SHOW_SINGLE
    if hand_ending(hand) == "下注-无人跟":
        return SHOW_VOLUNTARY
    return SHOW_SHOWDOWN


def canonical_hand_key(hand: Mapping[str, Any]) -> str:
    """§3.4 规范 hand 键；空字符串表示判不了（该行不出）。"""
    uid = str(hand.get("uid") or "").strip()
    hand_id = str(hand.get("hand_id") or "").strip()
    matched = _DATED_HAND_RE.search(uid)
    if matched:
        return matched.group(1)
    if _HAND_ID_RE.match(hand_id):
        return hand_id
    if uid:
        return uid
    if hand_id:
        session = hand.get("session")
        if isinstance(session, int):
            return "s{:02d}-{}".format(session, hand_id)
        if isinstance(session, str) and session.isdigit():
            return "s{:02d}-{}".format(int(session), hand_id)
        return hand_id
    return ""


# ---------------------------------------------------------------- 准入


def _fold_inferred(hand: Mapping[str, Any], seat: str) -> bool:
    """最后动作是 fold/弃牌，或最后街不再活跃：任一成立即拒（#19 任一否决即拒）。"""
    board = hand.get("board") or ()
    street = last_street(hand)
    if street is None:
        return True
    if len(board) >= 5:
        last = _last_action_by_seat(hand).get(seat)
        if last in ("fold", "弃牌"):
            return True
    active = _street_active(hand)
    if street in active and seat not in active[street]:
        return True
    return False


def winner_evidence(
    hand: Mapping[str, Any],
    seat: str,
    cards: Sequence[str],
    board: Sequence[str],
    kind: str,
) -> WinnerEvidence:
    """只能在全桌底牌都能从 shows / hero_cards 复原时比较；否则 unknown。"""
    if kind == SHOW_SINGLE:
        return WinnerEvidence(WINNER_SINGLE_SHOW)
    if kind != SHOW_SHOWDOWN:
        return WinnerEvidence(WINNER_UNKNOWN)
    if len(board) != 5:
        return WinnerEvidence(WINNER_UNKNOWN)
    street = last_street(hand)
    if street is None:
        return WinnerEvidence(WINNER_UNKNOWN)
    active = set(_street_active(hand).get(street) or ())
    if not active:
        return WinnerEvidence(WINNER_UNKNOWN)
    shows = _shows(hand)
    known: dict[str, tuple[str, ...]] = {}
    for other in active:
        if other == seat:
            known[other] = tuple(cards)
            continue
        show = shows.get(other)
        if show is not None:
            other_cards = _cards_of(show)
            if len(other_cards) == 2:
                known[other] = other_cards
    hero = hero_seat(hand)
    if hero and hero in active and hero not in known:
        hero_cards = _hero_cards(hand)
        if len(hero_cards) == 2:
            known[hero] = hero_cards
    if len(known) != len(active):
        return WinnerEvidence(WINNER_UNKNOWN)
    scored: dict[str, tuple[int, tuple[int, ...]]] = {}
    for other, other_cards in known.items():
        got = _category(other_cards, board)
        if got is None:
            return WinnerEvidence(WINNER_UNKNOWN)
        scored[other] = got
    best = max(scored.values())
    winners = [s for s, v in scored.items() if v == best]
    if len(winners) != 1:
        return WinnerEvidence(WINNER_UNKNOWN)
    winner = winners[0]
    return WinnerEvidence(
        WINNER_DERIVED,
        seat=winner,
        family=classify_family(known[winner], board),
    )


def admit(hand: Mapping[str, Any], seat_id: str) -> Admission | None:
    """单座位准入（§4.1）。返回 None 表示「不投影」：只有翻前/翻牌手，或不是亮牌座位。"""
    try:
        seat = str(seat_id)
        show = _shows(hand).get(seat)
        if show is None:
            return None
        board = tuple(str(c) for c in (hand.get("board") or ()) if c)
        street = last_street(hand)
        if street not in (STREET_TURN, STREET_RIVER):
            return None
        cards = _cards_of(show)
        if len(cards) not in (1, 2):
            return Admission(
                STATUS_REJECTED, REASON_HAND_MISMATCH, None, cards,
                WinnerEvidence(WINNER_UNKNOWN), None,
            )
        if len(board) < 3:
            return Admission(
                STATUS_REJECTED, REASON_BOARD_SHORT, None, cards,
                WinnerEvidence(WINNER_UNKNOWN), None,
            )
        if _fold_inferred(hand, seat):
            reason = REASON_FOLD_INFERRED
            active = _street_active(hand)
            if street in active and seat not in active[street]:
                reason = REASON_NOT_IN_STREET_ACTIVE
            return Admission(
                STATUS_REJECTED, reason, None, cards,
                WinnerEvidence(WINNER_UNKNOWN), None,
            )
        kind = show_kind(hand, len(cards))
        family = classify_family(cards, board) if len(cards) == 2 else None
        if len(cards) == 2 and family is None:
            return Admission(
                STATUS_REJECTED, REASON_HAND_MISMATCH, None, cards,
                WinnerEvidence(WINNER_UNKNOWN), None,
            )
        status = STATUS_TAGGED if kind in (SHOW_VOLUNTARY, SHOW_SINGLE) else STATUS_ADMITTED
        return Admission(
            status,
            None,
            kind,
            cards,
            winner_evidence(hand, seat, cards, board, kind),
            family,
        )
    except Exception:  # noqa: BLE001 - 证据侧 fail closed
        return Admission(
            STATUS_REJECTED, REASON_HAND_MISMATCH, None, (), WinnerEvidence(WINNER_UNKNOWN), None,
        )


# ---------------------------------------------------------------- 行投影


def _normalize_player(raw: Any) -> str | None:
    from ..opponent_profile import normalize_name

    return normalize_name(raw)


def to_row(
    hand: Mapping[str, Any],
    seat_id: str,
    admission: Admission | None = None,
    *,
    source: str = "outbox",
) -> dict[str, Any] | None:
    """§3.2 行；返回 None 表示这一座位不该出现在 rows（hero、缺位置、缺时间等）。"""
    try:
        seat = str(seat_id)
        hero = hero_seat(hand)
        if hero is not None and seat == hero:
            return None
        adm = admission if admission is not None else admit(hand, seat)
        # §4.2：admitted 与 tagged（主动秀 / 单张）都入 rows；rejected 不入。
        # 渲染层只用 status == admitted（§10.1），tagged 留在库里等 v2。
        if adm is None or not adm.accepted:
            return None
        street = last_street(hand)
        if street not in (STREET_TURN, STREET_RIVER):
            return None
        action = action_of(hand, seat, street)
        if action is None:
            return None
        position = position_of(hand, seat)
        if position is None:
            return None
        show = _shows(hand).get(seat)
        if show is None:
            return None
        player = _normalize_player(show.get("name"))
        if not player:
            return None
        hand_key = canonical_hand_key(hand)
        if not hand_key:
            return None
        ts = str(hand.get("ts") or "")
        if not ts:
            return None
        row = {
            "player": player,
            "hand": hand_key,
            "seat": seat,
            "ts": ts,
            "street": street,
            "action": action,
            "position": position,
            "cards": list(adm.cards),
            "board": [str(c) for c in (hand.get("board") or ()) if c],
            "family": adm.family,
            "show_kind": adm.show_kind,
            "status": adm.status,
            "winner": {
                "status": adm.winner.status,
                "seat": adm.winner.seat,
                "family": adm.winner.family,
            },
            "source": source,
            "table": str(hand.get("table_signature") or ""),
        }
        return {name: row[name] for name in _ROW_FIELDS}
    except Exception:  # noqa: BLE001 - 证据侧 fail closed
        return None


def iter_admissions(hand: Mapping[str, Any]) -> Iterator[tuple[str, Admission]]:
    for seat in _shows(hand):
        got = admit(hand, seat)
        if got is not None:
            yield seat, got


def iter_rows(hand: Mapping[str, Any], source: str = "outbox") -> Iterator[dict[str, Any]]:
    for seat in _shows(hand):
        row = to_row(hand, seat, source=source)
        if row is not None:
            yield row
