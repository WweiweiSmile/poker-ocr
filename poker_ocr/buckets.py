"""七个评测桶维度的单一推导：决策点 → 桶（稳定键）。

设计依据：`docs/手牌库-AB案例-一体化设计-2026-11-17.md` §3.3、§4.1、§7.1。

这个模块只做**纯逻辑**：不读 DB、不发请求、不依赖运行态。回填（013）与
评分（场景期望匹配、案例评分、评分卡）必须共用 `bucket_of()` —— 口径散
在各处时，同一手牌在「回填的桶」和「页面上看到的桶」会给出不同答案，而
A/B 结论正是按桶聚合出来的。

词表（与 `replay-console/replay/specs.py` 同源，那里直接 re-export 这里）：

  · `street` / `position_bucket` / `pot_type` / `hand_tier` 存稳定英文键；
  · `board_texture` 与 `opponent_profile` 沿用它已有的中文标签 —— 牌面质地
    本来就叫「干燥/花面」，画像读牌本来就叫「紧弱/松凶」，翻成英文键只是
    多一层要同步的映射，没有收益。
  · `hand_tier` 必须存英文键：`coach.py` 的中文文案被
    `replay/versioning.py` 记录过多次调整（"顶对强踢 T→J"），存中文会让老
    spec 在某次改文案后集体失配。

维度缺失（没有底牌、牌面不足 3 张、没有位置……）一律返回 `None`，
调用方按「通配」处理 —— 与 §4.1「空数组即通配」同义，绝不抛异常。
"""

from __future__ import annotations

import collections
from typing import Any, Iterable, Mapping

from .coach import HIGH_CARD, ONE_PAIR, RANK_CHARS, board_texture as _coach_texture
from .coach import evaluate, parse_cards, read_hand

# 桶维度的固定顺序：存储、diff、列表分组都按它来，避免 JSON 键序影响展示。
BUCKET_ORDER = (
    "street", "position_bucket", "hand_tier", "board_texture",
    "pot_type", "facing", "opponent_profile",
)

STREETS = {
    "preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌",
}
POSITION_BUCKETS = {
    "EP": "前位", "MP": "中位", "LP": "后位", "盲注": "盲注", "?": "未知",
}
# 稳定键（英文）→ 展示文案。键与设计 §3.3 一致；文案对齐 coach 的牌力分档常量。
HAND_TIERS = {
    "STRONG_MADE": "强成牌",
    "OVERPAIR": "超对",
    "TPTK": "顶对强踢",
    "TP_WEAK": "顶对弱踢",
    "MID_BOTTOM": "中底对",
    "STRONG_DRAW": "强听",
    "WEAK_DRAW": "弱听",
    "AIR": "空气",
    "BOARD_PLAY": "打牌面",
}
# 牌面质地本身就是中文标签（与 `poker_ocr/coach.py` 的主标签同口径）。
BOARD_TEXTURES = {
    "干燥": "干燥", "花面": "花面", "顺面": "顺面",
    "花顺面": "花顺面", "公对面": "公对面",
}
POT_TYPES = {
    "limped": "溜入池", "single": "单次加注池",
    "3bet+": "3bet+ 池", "bomb": "炸弹池", "unknown": "未知",
}
FACING = {0: "未面对下注", 1: "面对下注"}
OPPONENT_PROFILES = {
    "紧弱": "紧弱", "松弱": "松弱", "松凶": "松凶",
    "紧凶": "紧凶", "未知": "未知",
}

BUCKET_VOCAB = {
    "street": ("街", STREETS),
    "position_bucket": ("位置桶", POSITION_BUCKETS),
    "hand_tier": ("手牌档", HAND_TIERS),
    "board_texture": ("牌面质地", BOARD_TEXTURES),
    "pot_type": ("池型", POT_TYPES),
    "facing": ("面对下注", FACING),
    "opponent_profile": ("对手画像", OPPONENT_PROFILES),
}

# ── 位置 → 位置桶（9 人桌）。未列出的位置是「未知」，不是猜一个桶。──
POSITION_BUCKET = {
    "UTG": "EP", "UTG+1": "EP", "UTG+2": "EP",
    "LJ": "MP", "HJ": "MP",
    "CO": "LP", "BTN": "LP",
    "SB": "盲注", "BB": "盲注",
}
UNKNOWN_POSITION = "?"

# ── 池型：只看翻前动作。无翻前动作 ⇒ 炸弹池（与旧评分卡逐字一致）。──
POT_RAISES = ("raise", "bet")
UNKNOWN_POT = "unknown"

# ── 对手画像：archetype 键 → 桶值。词表只有 5 个值（§3.3），所以「松浪」
#   （maniac）归到「松凶」：它是松凶的极端，不是另一类。──
UNKNOWN_PROFILE = "未知"
PROFILE_BY_ARCHETYPE = {
    "nit": "紧弱",          # 紧弱
    "station": "松弱",      # 松弱/跟注站
    "lag": "松凶",          # 松凶
    "maniac": "松凶",       # 松浪 → 松凶的极端
    "tag": "紧凶",          # 紧凶
    "unknown": UNKNOWN_PROFILE,
}

# 主动动作：把钱推进底池、逼后面的人跟或弃。与 coach.AGGRESSIVE 同义。
AGGRESSIVE = ("bet", "raise", "allin")


# ─────────────────────────── 词表展示 ───────────────────────────

def label(dimension: str, value: Any) -> str:
    """把一个桶取值翻成中文；不认识的取值原样返回（不吞数据）。"""
    vocab = BUCKET_VOCAB.get(dimension, ("", {}))[1]
    return str(vocab.get(value, value))


def bucket_label(bucket: Mapping | None) -> str:
    """列表分组用：把桶里各维度的取值翻成中文，按固定顺序拼一行。"""
    bucket = bucket or {}
    parts: list[str] = []
    for dimension in BUCKET_ORDER:
        values = bucket.get(dimension) or []
        if not values:
            continue
        parts.append(" / ".join(label(dimension, v) for v in values))
    return " · ".join(parts) or "全局"


# ─────────────────────── 七个维度各自的推导 ───────────────────────

def street_of(street: Any) -> str | None:
    return street if street in STREETS else None


def position_bucket_of(position: Any) -> str:
    """位置 → EP/MP/LP/盲注/?。大小写与空白不敏感。"""
    return POSITION_BUCKET.get(str(position or "").strip().upper(),
                               UNKNOWN_POSITION)


def facing_of(to_call: Any) -> int:
    """是否面对下注：0/1（`to_call > 0`）。"""
    try:
        return 1 if float(to_call or 0.0) > 0 else 0
    except (TypeError, ValueError):
        return 0


def pot_type_of(actions: Iterable) -> str:
    """翻前动作 → limped / single / 3bet+ / bomb / unknown。

    口径与 `tools/advice_scorecard.py` 的历史实现逐字一致（含两条容易被
    「顺手修正」的细节）：**allin 不计入加注次数**，**没有任何翻前动作就是
    炸弹池**（炸弹池本来就没有翻前下注）。改这两条会改变既有统计的池型
    分布，要单独评审，不要顺手改。
    """
    kinds = []
    for action in actions or ():
        kind = (action.get("action") if isinstance(action, Mapping)
                else getattr(action, "action", action))
        if kind:
            kinds.append(kind)
    if not kinds:
        return "bomb"
    raises = sum(1 for kind in kinds if kind in POT_RAISES)
    if raises >= 2:
        return "3bet+"
    if raises == 1:
        return "single"
    return "limped" if "call" in kinds else UNKNOWN_POT


def hand_tier_of(hole_cards: Iterable, board: Iterable) -> str | None:
    """底牌 + 牌面 → coach 的牌力分档键（9 档）。牌面不足 3 张时是 None。"""
    hole, board_cards = cards_of(hole_cards), cards_of(board)
    if len(hole) != 2 or len(board_cards) < 3:
        return None
    return read_hand(hole, board_cards).tier


def texture_of(board: Iterable) -> str | None:
    """牌面 → 干燥/花面/顺面/花顺面/公对面。牌面不足 3 张时是 None。"""
    board_cards = cards_of(board)
    if len(board_cards) < 3:
        return None
    return _coach_texture(board_cards)[0]


def opponent_profile_of(reads: Mapping | None, *, position: Any = None,
                        name: Any = None, seat: Any = None) -> str:
    """本街最后加注者的画像；没有读牌或认不出类型时是「未知」。

    `reads` 是按座位（位置 / 座位 id / 名字）索引的读牌，值可以是
    `OpponentRead`，也可以直接是 archetype 字符串。
    """
    if not reads:
        return UNKNOWN_PROFILE
    raw = None
    for key in (seat, position, name):
        if key is not None and key in reads:
            raw = reads[key]
            break
    if raw is None:
        return UNKNOWN_PROFILE
    archetype = getattr(raw, "archetype", raw)
    return PROFILE_BY_ARCHETYPE.get(str(archetype or "").strip().lower(),
                                    UNKNOWN_PROFILE)


def cards_of(items: Any) -> list[tuple[int, str]]:
    """`['Ah', 'Kd']` / `'AhKd'` / `[(12, 'h'), (11, 'd')]` → 统一元组。

    认不出的输入退化成空列表（fail open：宁可少一条事实，不阻塞主链路）。
    """
    if not items:
        return []
    if isinstance(items, str):
        items = [items]
    out: list[tuple[int, str]] = []
    for item in items:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            try:
                out.append((int(item[0]), str(item[1]).lower()))
            except (TypeError, ValueError):
                return []
        elif isinstance(item, str):
            try:
                out.extend(parse_cards(item))
            except ValueError:
                return []
        else:
            return []
    return out


# ───────────────────── 决策点 → 七个桶（单一入口） ─────────────────────

# 快照字段在两种形状间切换：`snapshot_to_dict()` 的 camelCase（落库/回填）
# 与 `HandSnapshot` 的 snake_case 属性（运行态/评分）。两种都吃。
_FIELDS = {
    "street": ("street",),
    "board": ("board",),
    "hero_cards": ("heroCards", "hero_cards"),
    "hero_position": ("heroPosition", "hero_position"),
    "to_call": ("toCall", "to_call"),
    "bomb_pot": ("bombPot", "bomb_pot"),
    "streets": ("streets",),
    "actions": ("actions",),
    "action": ("action",),
    "position": ("position",),
    "name": ("name",),
    "seat": ("seatId", "seat_id"),
    "is_hero": ("isHero", "is_hero"),
}


def _get(obj: Any, field: str, default: Any = None) -> Any:
    for key in _FIELDS[field]:
        if isinstance(obj, Mapping):
            if key in obj and obj[key] is not None:
                return obj[key]
        elif hasattr(obj, key):
            value = getattr(obj, key)
            if value is not None:
                return value
    return default


def _view(decision: Any) -> dict:
    """把 DecisionPoint / HandSnapshot / snapshot_json 归一成同一份视图。"""
    snap = getattr(decision, "snap", decision)   # DecisionPoint → HandSnapshot
    return {
        "street": _get(snap, "street"),
        "board": _get(snap, "board", ()) or (),
        "hero_cards": _get(snap, "hero_cards", ()) or (),
        "hero_position": _get(snap, "hero_position"),
        "to_call": _get(snap, "to_call", 0.0) or 0.0,
        "bomb_pot": bool(_get(snap, "bomb_pot", False)),
        "streets": _get(snap, "streets", ()) or (),
    }


def _actions(streets: Iterable, street: Any) -> list[dict]:
    """取某条街的动作（按时间顺序），只留桶推导要用的四个字段。"""
    out: list[dict] = []
    for record in streets or ():
        if _get(record, "street") != street:
            continue
        for action in _get(record, "actions", ()) or ():
            out.append({
                "action": _get(action, "action"),
                "position": _get(action, "position"),
                "name": _get(action, "name"),
                "seat": _get(action, "seat"),
                "is_hero": bool(_get(action, "is_hero", False)),
            })
    return out


def last_aggressor(streets: Iterable, street: Any) -> dict | None:
    """本街最后一个**对手**的主动动作（hero 自己的下注不算「面对谁」）。"""
    aggressive = [a for a in _actions(streets, street)
                  if a["action"] in AGGRESSIVE and not a["is_hero"]]
    return aggressive[-1] if aggressive else None


def bucket_of(decision: Any, *, opponent_reads: Mapping | None = None) -> dict:
    """决策点 → 七个桶维度（稳定键）。缺的维度是 None（通配）。

    `decision` 可以是 `DecisionPoint`、`HandSnapshot`，或
    `snapshot_to_dict()` 的 camelCase 字典；`opponent_reads` 是座位 → 读牌
    的映射（回填时由画像库提供，评分时由 service 提供）。纯函数：
    同样的输入永远给出同样的七个值。
    """
    view = _view(decision)
    street = street_of(view["street"])
    aggressor = last_aggressor(view["streets"], street)
    return {
        "street": street,
        "position_bucket": position_bucket_of(view["hero_position"]),
        "hand_tier": hand_tier_of(view["hero_cards"], view["board"]),
        "board_texture": texture_of(view["board"]),
        "pot_type": ("bomb" if view["bomb_pot"]
                     else pot_type_of(_actions(view["streets"], "preflop"))),
        "facing": facing_of(view["to_call"]),
        "opponent_profile": opponent_profile_of(
            opponent_reads,
            position=(aggressor or {}).get("position"),
            name=(aggressor or {}).get("name"),
            seat=(aggressor or {}).get("seat"),
        ),
    }


# ─────────────── 评分卡的 5 档「牌力列」（历史口径，勿混用） ───────────────

def coarse_strength(hole_cards: Iterable, board: Iterable) -> str:
    """strong / top_pair / mid / draw / air —— **评分卡脚本的分组列**。

    这不是 `hand_tier`：评分卡按这 5 档聚合了多年历史数据，表里的「牌力/
    位置」列必须保持逐字不变（票 50 的验收：输出不变）。两处口径的差别是
    真实的，不要合并 —— 例如「打牌面 + 听牌」在 coach 里是 `WEAK_DRAW`，
    在这里是 `mid`/`air`。需要七维桶时用 `hand_tier_of()`。
    """
    hole, board_cards = cards_of(hole_cards), cards_of(board)
    if len(hole) < 2 or len(board_cards) < 3:
        return "air"
    cat, _ = evaluate(hole + board_cards)
    if len(board_cards) >= 5:
        board_cat, _ = evaluate(board_cards)
        if board_cat >= cat and board_cat >= HIGH_CARD + 1:
            return "air"
    if cat >= HIGH_CARD + 2:
        return "strong"
    if cat == ONE_PAIR:
        top = sorted({r for r, _ in board_cards}, reverse=True)[0]
        if any(r == top for r, _ in hole):
            return "top_pair"
        return "mid"
    # 听牌：4 张同花，或补一张能成顺（猫 4 = 顺子）
    suits = collections.Counter(suit for _, suit in hole + board_cards)
    if suits.most_common(1)[0][1] >= 4:
        return "draw"
    used = {suit for _, suit in hole + board_cards}
    spare_suit = next((s for s in "shdc" if s not in used), "s")
    have = {rank for rank, _ in hole + board_cards}
    for rank in range(len(RANK_CHARS)):
        if rank in have:
            continue
        straight, _ = evaluate(hole + board_cards + [(rank, spare_suit)])
        if straight == HIGH_CARD + 4:
            return "draw"
    return "air"
