"""Harrington 场景卡的 schema、静态校验与解析。

设计口径（docs/harrington-决策层-设计-2026-11-19.md）：

  · 场景卡按「局面 -> 计划」组织，不是互不相干的 if/then 规则堆；
  · 同一局面只允许一张卡胜出；同优先级有交集是编译期错误；
  · 零匹配返回 unknown，不编造书页；
  · 书只描述动作/尺寸/后续街计划，不直接改 prompt。

这里不 import `poker_ocr.situation`：`Spot.from_situation` 用鸭子类型，
只读字段，避免事实层与书中层循环依赖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

# 翻前专用维度（见 hand_class.py）：翻前没有 board，made_hand 恒为 unknown，
# 不补这几维就没法把「AA 加注 / 72o 弃牌」写成同一批卡。词表只定义一次，
# 这里直接复用；hand_class 不 import 本模块，所以没有循环依赖。
from .hand_class import (
    POSITION_BUCKETS, PREFLOP_HAND_CLASSES, position_bucket, preflop_hand_class,
    preflop_suited,
)

STREETS = ("preflop", "flop", "turn", "river")
PREFLOP_NODES = ("unopened", "limped", "raised", "three_bet", "four_bet", "unknown")
POT_TYPES = ("unopened", "limped", "single_raised", "three_bet_plus", "bomb", "unknown")
PLAYERS = ("2", "3", "4+", "unknown")
IN_POSITION = (True, False)
MADE_HANDS = (
    "strong_made", "overpair", "top_pair_top_kicker", "top_pair_weak_kicker",
    "middle_or_bottom_pair", "board_play", "draw", "air", "unknown",
)
DRAWS = ("none", "flush", "oesd", "gutshot", "combo", "other", "unknown")
BOARDS = ("dry", "flush", "straight", "flush_straight", "paired", "unknown")
SPRS = ("lt3", "3to6", "gt6", "unknown")
FACINGS = ("none", "bet", "raise", "unknown")
# 面对加注的层级（翻前专用）：书里只有到 3-bet 的决策，4/5-bet 显式 unknown。
FACING_LEVELS = ("three_bet", "four_bet", "five_bet", "unknown")
# 英雄在翻前加注序列里的角色；cold = 一次都没加注过（含冷跟/盲注）。
HERO_ROLES = ("cold", "open", "three_bet", "unknown")
COMMITMENTS = ("committed", "uncommitted", "unknown")
OPPONENTS = ("unknown", "loose_passive", "tight_passive", "aggressive")

ACTIONS = ("check", "bet", "call", "raise", "fold", "all_in")
BOOK_CONFIDENCE = ("explicit", "interpreted")
OFFSET_POLICIES = ("size_only", "none", "action_allowed")
SIZE_KINDS = ("pct_pot", "bb", "table")


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bucket_players(opponents_in_hand: Any) -> str:
    try:
        n = int(opponents_in_hand)
    except (TypeError, ValueError):
        return "unknown"
    if n <= 0:
        return "unknown"
    if n == 1:
        return "2"
    if n == 2:
        return "3"
    return "4+"


def _bucket_spr(spr: Any) -> str:
    try:
        value = float(spr)
    except (TypeError, ValueError):
        return "unknown"
    if value < 3:
        return "lt3"
    if value <= 6:
        return "3to6"
    return "gt6"


def _norm_alias(value: Any, aliases: dict[str, str],
                allowed: tuple[str, ...]) -> str:
    """事实文案 -> 卡片词表；认不出的值一律 unknown（不猜）。"""
    text = _norm(value)
    if text is None:
        return "unknown"
    return aliases.get(text, text if text in allowed else "unknown")


def _norm_pot_type(value: Any) -> str:
    return _norm_alias(value, {
        "unopened pot": "unopened",
        "limped pot": "limped",
        "single-raised pot": "single_raised",
        "3-bet+ pot": "three_bet_plus",
        "bomb pot": "bomb",
    }, POT_TYPES)


def _norm_board(value: Any) -> str:
    return _norm_alias(value, {
        "干燥": "dry",
        "花面": "flush",
        "顺面": "straight",
        "花顺面": "flush_straight",
        "公对面": "paired",
    }, BOARDS)


def _norm_made_hand(value: Any) -> str:
    return _norm_alias(value, {
        "强成牌": "strong_made",
        "超对": "overpair",
        "顶对强踢": "top_pair_top_kicker",
        "顶对弱踢": "top_pair_weak_kicker",
        "中底对": "middle_or_bottom_pair",
        "打牌面": "board_play",
        "强听": "draw",
        "弱听": "draw",
        "空气": "air",
    }, MADE_HANDS)


def _norm_draw(sit: Any) -> str:
    gt = getattr(sit, "ground_truth", None)
    draws = getattr(gt, "draws", None) if gt is not None else None
    if draws is not None:
        if getattr(draws, "flush_draw", False) and getattr(draws, "straight_draw", False):
            return "combo"
        if getattr(draws, "flush_draw", False):
            return "flush"
        if getattr(draws, "oesd", False):
            return "oesd"
        if getattr(draws, "gutshot", False):
            return "gutshot"
    return "other" if getattr(sit, "has_draw", False) else "none"


# 翻前的加注动作；与 preflop_shapes 的口径一致（bet/raise 一定算加注）。
_AGGRESSIVE_ACTIONS = ("bet", "raise", "all_in")

# 加注次数 -> 面对层级：开池(1)+3-bet(2) 就是「面对 3-bet」，再往上同理。
_FACING_LEVEL_BY_RAISES = {2: "three_bet", 3: "four_bet", 4: "five_bet"}

# 英雄在序列里的下标 -> 角色：第 1 个加注是开池，第 2 个是 3-bet。
_HERO_ROLE_BY_INDEX = {0: "open", 1: "three_bet"}


def _preflop_raises(snap: Any) -> tuple[Any, ...]:
    """翻前加注序列（按时间顺序）；快照没给动作线时返回空元组。

    只读事实、不猜：拿不到 actions_of 或动作线读坏，就退化成空序列，
    由调用方把层级/角色写成 unknown（fail open）。
    """
    getter = getattr(snap, "actions_of", None)
    if not callable(getter):
        return ()
    try:
        actions = tuple(getter("preflop") or ())
    except (KeyError, TypeError):
        return ()
    return tuple(action for action in actions
                 if getattr(action, "action", "") in _AGGRESSIVE_ACTIONS)


def _facing_level(raises: tuple[Any, ...]) -> str:
    return _FACING_LEVEL_BY_RAISES.get(len(raises), "unknown")


def _hero_role(raises: tuple[Any, ...]) -> str:
    """英雄加注过就是加注者；一次都没加注 = cold；拿不到动作线 = unknown。"""
    if not raises:
        return "unknown"
    for index, action in enumerate(raises):
        if getattr(action, "is_hero", False):
            return _HERO_ROLE_BY_INDEX.get(index, "unknown")
    return "cold"


def _norm_preflop_node(snap: Any) -> str:
    shapes = tuple(getattr(snap, "preflop_shapes", ()) or ())
    if "three-bet" in shapes:
        return "three_bet"
    if "single-raise" in shapes:
        return "raised"
    if "limped" in shapes:
        return "limped"
    if "unopened" in shapes:
        return "unopened"
    return "unknown"


@dataclass(frozen=True)
class Spot:
    """一个局面分类。字段为 None = 不限制；匹配时只看非 None 字段。"""

    street: str
    preflop_node: str | None = None
    pot_type: str | None = None
    players: str | None = None
    hero_position: str | None = None
    in_position: bool | None = None
    made_hand: str | None = None
    draw: str | None = None
    board: str | None = None
    spr: str | None = None
    facing: str | None = None
    commitment: str | None = None
    opponent: str | None = None
    # 翻前手牌类（见 hand_class.py）；只有翻前有值，翻后一律 None。
    hand_class: str | None = None
    # 位置分区（前/中/后/盲注）：翻前翻后都能算，翻前卡用它落书里的位置差异。
    position_bucket: str | None = None
    # 是否同花：书里按花色分流（前位只玩 KQs/AJs），翻前才有值。
    hand_suited: bool | None = None
    # 面对加注的层级（翻前专用）：2/3/4 次加注 = 面对 3-bet/4-bet/5-bet。
    # 书只写到 3-bet，4/5-bet 认得出来但故意不落卡（#91）。
    facing_level: str | None = None
    # 英雄在翻前加注序列里的角色；书在 3-bet 层不分冷跟/开池，但统计要看。
    hero_role: str | None = None

    def matches(self, other: "Spot") -> bool:
        """本卡是否覆盖 `other`；None 字段不限制。"""
        for key in self.__dataclass_fields__:
            want = getattr(self, key)
            if want is None:
                continue
            if getattr(other, key) != want:
                return False
        return True

    def specificity(self) -> int:
        return sum(1 for key in self.__dataclass_fields__
                   if getattr(self, key) is not None)

    def overlaps(self, other: "Spot") -> bool:
        """两张卡的 spot 是否有交集（逐字段相等或任一方 None）。"""
        for key in self.__dataclass_fields__:
            a, b = getattr(self, key), getattr(other, key)
            if a is not None and b is not None and a != b:
                return False
        return True

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__
                if getattr(self, k) is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Spot":
        allowed = set(cls.__dataclass_fields__)
        extra = set(data) - allowed
        if extra:
            raise ValueError(f"spot 未知字段: {sorted(extra)}")
        # in_position / hand_suited 是布尔值，不能过 _norm（True -> "True"）。
        booleans = ("in_position", "hand_suited")
        return cls(**{k: v if k in booleans else _norm(v)
                      for k, v in data.items()})

    @classmethod
    def from_situation(cls, sit: Any) -> "Spot":
        """从 Situation 派生；只读事实，计算不出就是 unknown。"""
        snap = getattr(sit, "snap", None)
        pot_type = _norm_pot_type(getattr(sit, "pot_type", None))
        street = _norm(getattr(sit, "street", None))
        if street == "preflop":
            preflop_node = _norm_preflop_node(snap)
        else:
            preflop_node = {
                "single_raised": "raised",
                "three_bet_plus": "three_bet",
                "limped": "limped",
                "unopened": "unopened",
            }.get(pot_type, "unknown")
        facing = "unknown"
        to_call = getattr(snap, "to_call", None)
        if to_call is not None:
            facing = "none" if float(to_call) <= 0 else (
                "raise" if preflop_node in ("three_bet", "four_bet") else "bet")
        spr = getattr(sit, "spr", None)
        spr_bucket = _bucket_spr(spr)
        commitment = ("unknown" if spr_bucket == "unknown"
                      else "committed" if spr_bucket == "lt3" else "uncommitted")
        hero_cards = getattr(snap, "hero_cards", None)
        hand_class = (preflop_hand_class(hero_cards)
                      if street == "preflop" else None)
        hand_suited = (preflop_suited(hero_cards)
                       if street == "preflop" else None)
        raises = _preflop_raises(snap) if street == "preflop" else ()
        return cls(
            street=street or "unknown",
            preflop_node=preflop_node,
            pot_type=pot_type,
            players=_bucket_players(getattr(sit, "opponents_in_hand", None)),
            hero_position=_norm(getattr(snap, "hero_position", None)),
            in_position=getattr(sit, "in_position", None),
            made_hand=_norm_made_hand(getattr(sit, "hand_tier", None)),
            draw=_norm_draw(sit),
            board=_norm_board(getattr(sit, "texture", None)),
            spr=spr_bucket,
            facing=facing,
            commitment=commitment,
            opponent="unknown",
            hand_class=hand_class,
            position_bucket=position_bucket(
                getattr(snap, "hero_position", None)),
            hand_suited=hand_suited,
            facing_level=_facing_level(raises) if street == "preflop" else None,
            hero_role=_hero_role(raises) if street == "preflop" else None,
        )


@dataclass(frozen=True)
class Size:
    kind: str
    value: float
    table: str | None = None

    def to_dict(self) -> dict:
        out = {"kind": self.kind, "value": self.value}
        if self.table:
            out["table"] = self.table
        return out


@dataclass(frozen=True)
class Plan:
    action: str
    size: Size | None = None
    future: dict[str, str] = field(default_factory=dict)
    freq: float = 1.0

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"action": self.action}
        if self.size is not None:
            out["size"] = self.size.to_dict()
        if self.future:
            out["future"] = dict(self.future)
        if self.freq != 1.0:
            out["freq"] = self.freq
        return out


def _plan_from_dict(data: Mapping[str, Any]) -> Plan:
    size_data = data.get("size")
    size = (Size(kind=str(size_data["kind"]), value=float(size_data["value"]),
                 table=size_data.get("table"))
            if isinstance(size_data, Mapping) else None)
    return Plan(
        action=str(data.get("action") or ""),
        size=size,
        future={str(k): str(v) for k, v in (data.get("future") or {}).items()},
        freq=float(data.get("freq") or 1.0),
    )


@dataclass(frozen=True)
class Card:
    id: str
    book: str
    volume: int
    chapter: str
    pages: tuple[int, ...]
    street: str
    spot: Spot
    plan: Plan
    why: str
    confidence: str = "interpreted"
    priority: int = 100
    offset_policy: str = "size_only"
    pdf_pages: tuple[int, ...] = ()
    # 书里的混合频率：plan 是主行动，mix 是其余分支；freq 合计应为 1。
    mix: tuple[Plan, ...] = ()

    def to_dict(self) -> dict:
        out = {
            "id": self.id,
            "book": self.book,
            "volume": self.volume,
            "chapter": self.chapter,
            "pages": list(self.pages),
            "street": self.street,
            "spot": self.spot.to_dict(),
            "plan": self.plan.to_dict(),
            "why": self.why,
            "confidence": self.confidence,
            "priority": self.priority,
            "offset_policy": self.offset_policy,
        }
        if self.pdf_pages:
            out["pdf_pages"] = list(self.pdf_pages)
        if self.mix:
            out["mix"] = [p.to_dict() for p in self.mix]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Card":
        return cls(
            id=str(data["id"]),
            book=str(data.get("book") or ""),
            volume=int(data.get("volume") or 0),
            chapter=str(data.get("chapter") or ""),
            pages=tuple(int(p) for p in data.get("pages") or ()),
            street=str(data["street"]),
            spot=Spot.from_dict(data.get("spot") or {}),
            plan=_plan_from_dict(data.get("plan") or {}),
            why=str(data.get("why") or ""),
            confidence=str(data.get("confidence") or "interpreted"),
            priority=int(data.get("priority") or 100),
            offset_policy=str(data.get("offset_policy") or "size_only"),
            pdf_pages=tuple(int(p) for p in data.get("pdf_pages") or ()),
            mix=tuple(_plan_from_dict(p) for p in (data.get("mix") or ())),
        )


@dataclass(frozen=True)
class Resolution:
    card: Card | None
    matched_fields: int = 0

    @property
    def unknown(self) -> bool:
        return self.card is None


def validate_cards(cards: Iterable[Card]) -> list[str]:
    """编译期静态校验；返回错误列表，空列表 = 通过。"""
    errors: list[str] = []
    seen: dict[str, Card] = {}
    for card in cards:
        if not card.id:
            errors.append("card.id 为空")
            continue
        if card.id in seen:
            errors.append(f"id 重复: {card.id}")
        seen[card.id] = card
        if card.street not in STREETS:
            errors.append(f"{card.id}: street 非法 {card.street!r}")
        if card.spot.street != card.street:
            errors.append(f"{card.id}: spot.street 与 street 不一致")
        if card.confidence not in BOOK_CONFIDENCE:
            errors.append(f"{card.id}: confidence 非法 {card.confidence!r}")
        if card.offset_policy not in OFFSET_POLICIES:
            errors.append(f"{card.id}: offset_policy 非法 {card.offset_policy!r}")
        if not card.pages:
            errors.append(f"{card.id}: pages 为空")
        if not card.why.strip():
            errors.append(f"{card.id}: why 为空")
        if card.priority < 0:
            errors.append(f"{card.id}: priority 必须 >=0")
        if card.spot.hand_class is not None:
            if card.spot.street != "preflop":
                errors.append(
                    f"{card.id}: hand_class 只对翻前有意义（street="
                    f"{card.spot.street!r}）")
            if card.spot.hand_class not in PREFLOP_HAND_CLASSES:
                errors.append(
                    f"{card.id}: hand_class 非法 {card.spot.hand_class!r}")
        if card.spot.hand_suited is not None:
            if not isinstance(card.spot.hand_suited, bool):
                errors.append(
                    f"{card.id}: hand_suited 必须是布尔值，"
                    f"当前 {card.spot.hand_suited!r}")
            if card.spot.street != "preflop":
                errors.append(
                    f"{card.id}: hand_suited 只对翻前有意义（street="
                    f"{card.spot.street!r}）")
        for name, allowed in (("facing_level", FACING_LEVELS),
                             ("hero_role", HERO_ROLES)):
            value = getattr(card.spot, name)
            if value is None:
                continue
            if card.spot.street != "preflop":
                errors.append(
                    f"{card.id}: {name} 只对翻前有意义（street="
                    f"{card.spot.street!r}）")
            if value not in allowed:
                errors.append(f"{card.id}: {name} 非法 {value!r}")
        if (card.spot.position_bucket is not None
                and card.spot.position_bucket not in POSITION_BUCKETS):
            errors.append(
                f"{card.id}: position_bucket 非法 {card.spot.position_bucket!r}")
        plans = (card.plan,) + tuple(card.mix)
        for plan in plans:
            if plan.action not in ACTIONS:
                errors.append(f"{card.id}: action 非法 {plan.action!r}")
            if not (0 < plan.freq <= 1):
                errors.append(f"{card.id}: freq 必须在 (0,1]")
            if plan.size is not None:
                size = plan.size
                if size.kind not in SIZE_KINDS:
                    errors.append(f"{card.id}: size.kind 非法 {size.kind!r}")
                if size.kind == "pct_pot" and not (0 < size.value <= 2):
                    errors.append(f"{card.id}: pct_pot 必须在 (0,2]")
                if size.kind == "bb" and size.value <= 0:
                    errors.append(f"{card.id}: bb 必须 >0")
        total = sum(p.freq for p in plans)
        if total > 1.001:
            errors.append(f"{card.id}: freq 合计 {total:.3f} 超过 1")

    ordered = list(seen.values())
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if a.street != b.street:
                continue
            if a.priority != b.priority:
                continue
            if a.spot.overlaps(b.spot):
                errors.append(
                    f"同优先级且 spot 有交集: {a.id} / {b.id}（priority={a.priority}）")
    return errors


def load_cards(path: Path | str) -> list[Card]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("cards") if isinstance(data, Mapping) else data
    if not isinstance(rows, list):
        raise ValueError(f"{path}: 期望 cards 数组")
    return [Card.from_dict(row) for row in rows]


def resolve(cards: Iterable[Card], spot: Spot) -> Resolution:
    """选最具体、优先级最高的一张；同分不同卡 -> 编译期错误。"""
    matched = [card for card in cards if card.spot.matches(spot)]
    if not matched:
        return Resolution(card=None)
    matched.sort(key=lambda c: (c.priority, c.spot.specificity()), reverse=True)
    best = matched[0]
    tied = [c for c in matched
            if c.priority == best.priority
            and c.spot.specificity() == best.spot.specificity()]
    if len(tied) > 1:
        ids = ", ".join(c.id for c in tied)
        raise ValueError(f"同一局面多张同分卡: {ids}")
    return Resolution(card=best, matched_fields=best.spot.specificity())
