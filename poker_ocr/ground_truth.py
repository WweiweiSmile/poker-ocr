"""牌力事实层（Ground Truth）—— 模型只能引用，不能自己断言。

这个模块**不重新实现评估器**。牌型大小、踢脚、牌面质地、随机牌排名全部复用
`coach.evaluate` / `coach.board_ceiling` / `coach.rank_hands`。它只做三件事：

  1. 把「最好的五张、用了哪两张底牌、听牌、牌面精确结构、坚果与否」整理成
     一个不可变的 `GroundTruth`；
  2. 用**精确枚举**回答「有没有比我大的牌」：`beaten_by` 列出所有能打败我的
     牌型，`hero_is_nuts` 只有在没有任何对手持牌能打败我时才为真；
  3. 渲染成一段固定格式的提示词事实块（`render()`），供实时建议和复盘共用。

为什么需要它（对应 2026-10-23 复盘的两个退化）：

  · `Jc8dKdQc` 被模型读成「花面」：牌面只有 **两张** 方块/梅花，根本没有成花。
    这里 `BoardInfo` 把「已成花可能（≥3 张同花）」和「只是花听（=2 张同花）」
    分开写死，提示词会明确说 "no made flush possible"。
  · `AsTh` 在 `Jc8dKdQc` 是**坚果顺**，模型却说「有更大的牌」。这里枚举 1035
    种对手持牌，`beaten_by=()`、`hero_is_nuts=True` 会直接写进事实块。

口径（与 coach 一致）：
  - 顺子只比顶端牌；A 可作最大或最小（A2345 = 5-high）；
  - 同花不比花色；
  - 葫芦先比三条、再比对子；
  - 「坚果」指**没有任何对手持牌能打败我**，允许分池；分池比例单独给。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .coach import (
    CATEGORY_NAMES, CATEGORY_NAMES_EN, FLUSH, FULL_HOUSE, HIGH_CARD, ONE_PAIR,
    QUADS, RANK_CHARS, STRAIGHT, STRAIGHT_FLUSH, TRIPS, TWO_PAIR, VALID_SUITS,
    _has_run, board_ceiling, board_texture, evaluate, parse_cards, rank_char,
    rank_hands,
)

Card = tuple[int, str]


# ─────────────────────────── 基础转换 ───────────────────────────

def _coerce(cards: str | list[str] | tuple[str, ...] | list[Card]) -> list[Card]:
    """接受 'AsTh' / ['As','Th'] / [(12,'s'), ...]，统一成 [(点数, 花色)]。

    重复牌会被去掉（OCR 偶发把同一张牌读两次）。重复不是合法牌局，但这里
    不抛异常：事实层要让主链路继续跑，重复只影响自己的这两张，去重后仍可评估。
    """
    raw: list[Card] = []
    if isinstance(cards, str):
        raw = parse_cards(cards)
    else:
        for c in cards:
            if isinstance(c, tuple):
                raw.append(c)
            else:
                raw.extend(parse_cards(c))
    seen: set[Card] = set()
    out: list[Card] = []
    for c in raw:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def card_text(card: Card) -> str:
    return rank_char(card[0]) + card[1]


def cards_text(cards: tuple[Card, ...] | list[Card]) -> str:
    return " ".join(card_text(c) for c in cards)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# ─────────────────────────── 数据结构 ───────────────────────────

@dataclass(frozen=True)
class MadeHand:
    category: int
    name: str                       # 英文牌型名，给提示词
    name_cn: str
    detail: str                     # "A-high straight" / "Kings full of Sevens"
    best_five: tuple[Card, ...]
    hero_cards_used: tuple[Card, ...]

    def to_dict(self) -> dict:
        return {"category": self.name, "category_cn": self.name_cn,
                "detail": self.detail,
                "best_five": [card_text(c) for c in self.best_five],
                "hero_cards_used": [card_text(c) for c in self.hero_cards_used]}


@dataclass(frozen=True)
class DrawInfo:
    flush_draw: bool
    nut_flush_draw: bool
    flush_suit: str | None
    flush_outs: int
    straight_draw: bool
    oesd: bool
    gutshot: bool
    straight_outs: int
    total_outs: int
    equity_pct: float               # 四二法则，0-100
    note: str

    def to_dict(self) -> dict:
        return {"flush_draw": self.flush_draw,
                "nut_flush_draw": self.nut_flush_draw,
                "flush_suit": self.flush_suit,
                "flush_outs": self.flush_outs,
                "straight_draw": self.straight_draw,
                "oesd": self.oesd, "gutshot": self.gutshot,
                "straight_outs": self.straight_outs,
                "total_outs": self.total_outs,
                "equity_pct": round(self.equity_pct, 1),
                "note": self.note}


@dataclass(frozen=True)
class BeatenByInfo:
    category: int
    name: str
    combos: int
    example: tuple[Card, ...]

    def to_dict(self) -> dict:
        return {"category": self.name, "combos": self.combos,
                "example": [card_text(c) for c in self.example]}


@dataclass(frozen=True)
class RankInfo:
    opponents: int
    total: int
    win_pct: float
    tie_pct: float
    beat_pct: float
    beat_at_least_pct: float
    equity_pct: float
    hero_is_nuts: bool
    beaten_by: tuple[BeatenByInfo, ...]
    best_opponent_category: int | None
    best_opponent_name: str | None
    best_opponent_cards: tuple[Card, ...] | None

    def to_dict(self) -> dict:
        return {"opponents": self.opponents, "total_holdings": self.total,
                "win_pct": round(self.win_pct * 100, 1),
                "tie_pct": round(self.tie_pct * 100, 1),
                "beat_pct": round(self.beat_pct * 100, 1),
                "beat_at_least_pct": round(self.beat_at_least_pct * 100, 1),
                "equity_pct": round(self.equity_pct * 100, 1),
                "hero_is_nuts": self.hero_is_nuts,
                "beaten_by": [b.to_dict() for b in self.beaten_by],
                "best_opponent": self.best_opponent_name,
                "best_opponent_cards": ([card_text(c)
                                         for c in self.best_opponent_cards]
                                        if self.best_opponent_cards else None)}


@dataclass(frozen=True)
class BoardInfo:
    cards: tuple[Card, ...]
    street: str
    texture: str
    tags: tuple[str, ...]
    flush_made_possible: bool       # 公共牌 ≥3 张同花：对手两张同花就成花
    flush_draw_possible: bool       # 公共牌正好 2 张同花：只有花听，没有成花
    flush_suit_count: int
    paired: bool
    trips: bool
    quads: bool
    connected: bool                 # 公共牌有 3 连（纹理标签，不等于顺子一定可能）
    straight_possible: bool         # 任意对手两张暗牌能否补成顺子（精确）
    monotone: bool
    rainbow: bool
    ceiling: int
    ceiling_name: str
    best_possible_category: int | None
    best_possible_name: str | None
    best_possible_cards: tuple[Card, ...] | None

    def to_dict(self) -> dict:
        return {"cards": [card_text(c) for c in self.cards],
                "street": self.street, "texture": self.texture,
                "tags": list(self.tags),
                "flush_made_possible": self.flush_made_possible,
                "flush_draw_possible": self.flush_draw_possible,
                "flush_suit_count": self.flush_suit_count,
                "paired": self.paired, "trips": self.trips,
                "quads": self.quads, "connected": self.connected,
                "straight_possible": self.straight_possible,
                "monotone": self.monotone, "rainbow": self.rainbow,
                "ceiling": self.ceiling_name,
                "best_possible": self.best_possible_name,
                "best_possible_cards": ([card_text(c)
                                         for c in self.best_possible_cards]
                                        if self.best_possible_cards else None)}


@dataclass(frozen=True)
class GroundTruth:
    hero: tuple[Card, ...]
    board: tuple[Card, ...]
    made: MadeHand
    draws: DrawInfo
    rank: RankInfo
    board_info: BoardInfo
    warnings: tuple[str, ...] = ()

    @property
    def hero_is_nuts(self) -> bool:
        return self.rank.hero_is_nuts

    def to_dict(self) -> dict:
        return {"hero": [card_text(c) for c in self.hero],
                "board": [card_text(c) for c in self.board],
                "made": self.made.to_dict(),
                "draws": self.draws.to_dict(),
                "rank": self.rank.to_dict(),
                "board_structure": self.board_info.to_dict(),
                "warnings": list(self.warnings)}

    # ── 渲染 ──────────────────────────────────────────────────

    def _board_structure_text(self) -> str:
        b = self.board_info
        if b.flush_made_possible:
            if b.monotone:
                flush = f"FLUSH: possible (monotone board, {b.flush_suit_count} of one suit)"
            else:
                flush = (f"FLUSH: possible ({b.flush_suit_count} of one suit on board — "
                         f"any opponent with two of that suit has a flush)")
        elif b.flush_draw_possible:
            if b.street == "river":
                flush = ("FLUSH: no flush possible (at most 2 of any suit on the "
                         "river board)")
            else:
                flush = ("FLUSH: no made flush possible (at most 2 of any suit — "
                         "flush DRAW only)")
        else:
            flush = "FLUSH: no flush possible (rainbow board)"

        straight = ("STRAIGHT: possible (an opponent can hold two cards that "
                    "complete a straight)" if b.straight_possible else
                    "STRAIGHT: not possible (no opponent holding completes a straight)")
        if b.quads:
            pair = "PAIRED: yes (trips on board — QUADS possible)"
        elif b.trips:
            pair = "PAIRED: yes (trips on board — full house / quads possible)"
        elif b.paired:
            pair = "PAIRED: yes (full house / quads possible)"
        else:
            pair = "PAIRED: no"
        return f"{flush}｜{straight}｜{pair}"

    def _rank_text(self) -> str:
        r = self.rank
        n = r.opponents
        return (
            f"Made-hand rank (exact enumeration over all {r.total} opponent "
            f"holdings, {n} opponent{'s' if n != 1 else ''}): beaten by "
            f"{r.beat_pct * 100:.1f}% of hands PER OPPONENT; ties "
            f"{r.tie_pct * 100:.1f}% per opponent; equity "
            f"{r.equity_pct * 100:.1f}% vs ONE opponent. "
            f"Multiway: with {n} opponent{'s' if n != 1 else ''}, "
            f"{r.beat_at_least_pct * 100:.1f}% chance at least one is ahead. "
            f"Random hands are a LOWER bound — a calling/raising range is "
            f"stronger than random."
        )

    def _nuts_text(self) -> str:
        r = self.rank
        if r.hero_is_nuts:
            if r.tie_pct > 0:
                return (f"NUT STATUS: NUTS — no opponent holding can beat you; "
                        f"you can only chop ({r.tie_pct * 100:.1f}% of holdings tie).")
            return ("NUT STATUS: ABSOLUTE NUTS — no opponent holding can beat you. "
                    "Do not invent or imply any higher hand.")
        names = ", ".join(f"{b.name} ({b.combos} combos)" for b in r.beaten_by)
        best = r.best_opponent_name or "unknown"
        ex = cards_text(r.best_opponent_cards) if r.best_opponent_cards else "?"
        return (f"NUT STATUS: NOT THE NUTS — better hands exist: {names}. "
                f"Best possible opponent hand: {best} ({ex}).")

    def _draw_text(self) -> str:
        d = self.draws
        if not d.flush_draw and not d.straight_draw:
            return "Draws: none."
        parts = []
        if d.flush_draw:
            parts.append(f"{'nut ' if d.nut_flush_draw else ''}flush draw "
                         f"in {d.flush_suit} ({d.flush_outs} outs)")
        if d.straight_draw:
            kind = "open-ended/double-gutshot" if d.oesd else "gutshot"
            parts.append(f"{kind} straight draw ({d.straight_outs} outs)")
        return (f"Draws: {' + '.join(parts)}; total {d.total_outs} outs "
                f"(rule of 4/2 equity {d.equity_pct:.0f}%).")

    def _draw_status_text(self) -> str:
        """听牌专用的事实头：它是行动依据，压过「当前未成牌」的排名。"""
        d = self.draws
        parts = []
        if d.flush_draw:
            parts.append(f"{'nut ' if d.nut_flush_draw else ''}flush draw "
                         f"in {d.flush_suit} ({d.flush_outs} outs)")
        if d.straight_draw:
            kind = "open-ended/double-gutshot" if d.oesd else "gutshot"
            parts.append(f"{kind} straight draw ({d.straight_outs} outs)")
        return (f"DRAW STATUS (action basis — this is an unmade hand): "
                f"{' + '.join(parts)}; total {d.total_outs} outs "
                f"(rule of 4/2 equity {d.equity_pct:.0f}%). Use this and the "
                f"draw directive; do NOT use the made-hand rank below to veto "
                f"a semi-bluff.")

    def _street_change_text(self) -> str | None:
        """这一街新发的牌改变了什么：成花 / 公对面成三条 / 四条 / 成对。

        模型在河牌最容易漏掉「这张牌让什么牌型成了」。h39 的 Jh 让牌面三张
        红桃成花、JJ 成葫芦、任何 Jx 反超 TT44，模型却仍写「two pair beats
        his bluffs」—— 它只看了自己的牌型名，没看新牌。这里用「当前牌面 vs
        去掉最后一张」的差集算，不猜。
        """
        b = self.board
        if len(b) < 4:                       # 翻牌没有「上一街」可比
            return None
        last, prev = b[-1], b[:-1]
        changes: list[str] = []

        # 成花：最后一张让某个花色在公共牌上达到 3 张。
        suit = last[1]
        now = sum(1 for c in b if c[1] == suit)
        before = sum(1 for c in prev if c[1] == suit)
        if now >= 3 and before < 3:
            suit_name = {"s": "spades", "h": "hearts",
                         "d": "diamonds", "c": "clubs"}.get(suit, suit)
            changes.append(f"the {card_text(last)} completes a flush "
                           f"({now} {suit_name} on the board)")

        # 公对面：最后一张的点数此前已经出现过。
        rank = last[0]
        same = sum(1 for c in b if c[0] == rank)
        before_same = sum(1 for c in prev if c[0] == rank)
        if same >= 4:
            changes.append(f"the {card_text(last)} puts QUADS on the board")
        elif same == 3 and before_same == 2:
            changes.append(f"the {card_text(last)} puts TRIPS on the board")
        elif same == 2 and before_same == 1:
            changes.append(f"the {card_text(last)} pairs the board")

        if not changes:
            return None
        return "Street change: " + "; ".join(changes) + "."

    def render(self, draw_focus: bool = False) -> str:
        """固定格式的事实块。提示词里的模型只允许引用，不允许改写。

        `draw_focus=True` 用于纯听牌：不渲染 `NUT STATUS` / `Beaten by`，
        避免模型把「当前未成牌的 A 高」当成「不能下注」。桌面动态仍然可以
        让模型过牌——但理由必须是「没有弃牌率」，不能是「我不是坚果」。
        """
        m = self.made
        is_draw = bool(self.draws.flush_draw or self.draws.straight_draw)
        draw_focus = bool(draw_focus and is_draw)
        lines = [
            "GROUND TRUTH (deterministic evaluator; authoritative — never "
            "recompute, never contradict)",
            f"Board: {cards_text(self.board)}｜{self.board_info.street}",
            f"Board structure: {self._board_structure_text()}",
            f"Made hand: {m.name.upper()} ({m.detail})｜best five: "
            f"{cards_text(m.best_five)}｜hole cards used: "
            f"{cards_text(m.hero_cards_used) if m.hero_cards_used else 'NONE (playing the board)'}",
        ]
        street_change = self._street_change_text()
        if street_change:
            lines.append(street_change)
        if draw_focus:
            lines.append(self._draw_status_text())
            lines.append(
                "DRAW RULE: heads-up with no bet in front, a semi-bluff is the "
                "baseline; against a station that never folds you may check, but "
                "the reason must be NO FOLD EQUITY — never «I am not the nuts». "
                "With 3+ opponents, check.")
            lines.append("Context only — CURRENT unmade hand (not the action basis):")
            lines.append(self._rank_text())
        else:
            lines.extend([
                self._rank_text(),
                self._nuts_text(),
                ("Beaten by: " + ", ".join(f"{b.name} (e.g. {cards_text(b.example)})"
                                           for b in self.rank.beaten_by)
                 if self.rank.beaten_by else "Beaten by: nothing."),
                self._draw_text(),
            ])
        if self.warnings:
            warnings = self.warnings
            if draw_focus:
                # 这几条描述的是「当前未成牌」，对听牌的行动选择没有指导意义；
                # 保留「只是花听 / 听牌不是成牌 / 公对面花听」等有用警告。
                warnings = tuple(
                    w.replace("the rank/NUT STATUS above", "the context rank above")
                    for w in warnings if not w.startswith(
                        ("You are NOT the nuts", "A flush is ALREADY possible",
                         "An opponent can complete a straight")))
            if warnings:
                lines.append("Warnings:")
                lines.extend(f"- {w}" for w in warnings)
        return "\n".join(lines)


# ─────────────────────────── 成手牌/最好五张 ───────────────────────────

_CARD_NAMES = {0: "2", 1: "3", 2: "4", 3: "5", 4: "6", 5: "7", 6: "8",
               7: "9", 8: "T", 9: "J", 10: "Q", 11: "K", 12: "A"}


def _describe(cat: int, tb: list[int]) -> str:
    if cat == HIGH_CARD:
        return f"{_CARD_NAMES[tb[0]]}-high"
    if cat == ONE_PAIR:
        return f"pair of {_CARD_NAMES[tb[0]]}s"
    if cat == TWO_PAIR:
        return f"{_CARD_NAMES[tb[0]]}s and {_CARD_NAMES[tb[1]]}s"
    if cat == TRIPS:
        return f"trip {_CARD_NAMES[tb[0]]}s"
    if cat == STRAIGHT:
        return "5-high straight (wheel)" if tb[0] == 3 else f"{_CARD_NAMES[tb[0]]}-high straight"
    if cat == FLUSH:
        return f"{_CARD_NAMES[tb[0]]}-high flush"
    if cat == FULL_HOUSE:
        return f"{_CARD_NAMES[tb[0]]}s full of {_CARD_NAMES[tb[1]]}s"
    if cat == QUADS:
        return f"quad {_CARD_NAMES[tb[0]]}s"
    if cat == STRAIGHT_FLUSH:
        return ("5-high straight flush (steel wheel)" if tb[0] == 3
                else f"{_CARD_NAMES[tb[0]]}-high straight flush")
    return "unknown"


def _order_five(cards: tuple[Card, ...], cat: int, key: tuple[int, list[int]]) -> tuple[Card, ...]:
    """把最好五张按「牌型可读」的顺序排好（不影响比较）。"""
    if cat in (STRAIGHT, STRAIGHT_FLUSH):
        ordered = sorted(cards, key=lambda c: c[0])
        if key[1][0] == 3:                      # 轮子：A 放到最后
            ordered = [c for c in ordered if c[0] != 12] + [c for c in ordered if c[0] == 12]
        return tuple(ordered)
    counts: dict[int, int] = {}
    for r, _ in cards:
        counts[r] = counts.get(r, 0) + 1
    return tuple(sorted(cards, key=lambda c: (-counts[c[0]], -c[0])))


def _best_five(cards: list[Card]) -> tuple[tuple[Card, ...], tuple[int, list[int]]]:
    """从 5–7 张里选最好的五张；同分时优先「不用底牌」的组合（打牌面口径）。

    枚举 C(7,5)=21 次 `evaluate`，与 `coach.evaluate` 完全同源，不存在第二套
    牌型排序。这里只解决「哪五张」的问题。
    """
    hero = set(cards[:2])
    best: tuple[tuple[Card, ...], tuple[int, list[int]]] | None = None
    best_score: tuple | None = None
    for combo in combinations(cards, 5):
        key = evaluate(list(combo))
        hero_used = sum(1 for c in combo if c in hero)
        score = (key, -hero_used)
        if best_score is None or score > best_score:
            best_score = score
            best = (combo, key)
    assert best is not None
    return best


# ─────────────────────────── 听牌 ───────────────────────────

def _draws(hero: list[Card], board: list[Card], cat: int) -> DrawInfo:
    seen = hero + board
    used = set(seen)
    remaining = [(r, s) for r in range(len(RANK_CHARS)) for s in VALID_SUITS
                 if (r, s) not in used]
    board_len = len(board)
    no_more_cards = board_len >= 5

    suit_counts: dict[str, int] = {}
    for _, s in seen:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    flush_suit = next((s for s, n in suit_counts.items() if n == 4), None)
    flush_draw = (not no_more_cards and flush_suit is not None and cat < FLUSH
                  and any(c[1] == flush_suit for c in hero))
    flush_cards = ({c for c in remaining if c[1] == flush_suit}
                   if flush_draw else set())
    nut_flush_draw = bool(flush_draw and (12, flush_suit) in hero)

    straight_cards: set[Card] = set()
    if not no_more_cards and cat < STRAIGHT:
        for c in remaining:
            # 只认「成了顺子/同花顺」的补牌。用 >= STRAIGHT 会把「补成葫芦」
            # 也算成顺子补牌（22 在 88T 上补 2 成葫芦），outs 会虚高。
            if evaluate(hero + board + [c])[0] in (STRAIGHT, STRAIGHT_FLUSH):
                straight_cards.add(c)
    total_cards = flush_cards | straight_cards
    equity = 0.0 if no_more_cards else min(
        len(total_cards) * (4 if board_len == 3 else 2), 100.0)

    oesd = len(straight_cards) >= 8
    gutshot = 0 < len(straight_cards) < 8
    parts: list[str] = []
    if flush_draw:
        parts.append(("nut " if nut_flush_draw else "") + "flush draw")
    if straight_cards:
        parts.append("open-ended/double-gutshot" if oesd else "gutshot")
    if not parts:
        note = "none"
    else:
        note = " + ".join(parts)
        if len(total_cards) < len(flush_cards) + len(straight_cards):
            note += " (overlap counted once)"
    return DrawInfo(
        flush_draw=flush_draw, nut_flush_draw=nut_flush_draw,
        flush_suit=flush_suit if flush_draw else None,
        flush_outs=len(flush_cards),
        straight_draw=bool(straight_cards), oesd=oesd, gutshot=gutshot,
        straight_outs=len(straight_cards), total_outs=len(total_cards),
        equity_pct=equity, note=note,
    )


# ─────────────────────────── 牌面结构 ───────────────────────────

def _board_straight_possible(ranks: set[int]) -> bool:
    """公共牌 + 对手任意两张暗牌能否构成顺子（A 可当 1）。

    某个 5 连窗口里只要有 3 张不同点数的公共牌，剩下两个点数就由两张暗牌补；
    每个点数还有 4 张，我和公共牌最多占 2 张，至少剩 2 张，所以一定补得出来。
    """
    ext = set(ranks)
    if 12 in ext:
        ext.add(-1)
    return any(sum(1 for r in range(lo, lo + 5) if r in ext) >= 3
               for lo in range(-1, 10))


def _street_name(n: int) -> str:
    return {3: "flop", 4: "turn", 5: "river"}.get(n, f"{n} cards")


def _board_info(board: list[Card], rank: RankInfo) -> BoardInfo:
    ranks = [r for r, _ in board]
    rank_counts: dict[int, int] = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    suit_counts: dict[str, int] = {}
    for _, s in board:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    max_suit = max(suit_counts.values())
    max_rank = max(rank_counts.values())
    paired = max_rank >= 2
    trips = max_rank >= 3
    quads = max_rank >= 4
    connected = _has_run(set(ranks), 3)
    # 精确口径：只要某个 5 连点数窗口里有 ≥3 张公共牌，对手的两张暗牌就能补齐
    # 剩下的两个点数（每个点数至少还有 2 张没被公共牌/我的底牌占掉）。
    # 注意不能用「对手最大牌型 ≥ 顺子」代替：对手最大牌型是同花时，顺子也可能
    # 存在，但最大牌型会把它盖掉。
    straight_possible = _board_straight_possible(set(ranks))
    flush_made = max_suit >= 3
    flush_draw = max_suit == 2 and len(board) >= 3
    texture, _tags = board_texture(board)
    ceiling = board_ceiling(board)

    tags: list[str] = []
    if flush_made:
        tags.append("FLUSH_POSSIBLE")
    if flush_draw:
        tags.append("FLUSH_DRAW_ONLY")
    if max_suit == len(board) and len(board) >= 3:
        tags.append("MONOTONE")
    elif max_suit == 2:
        tags.append("TWO_TONE")
    elif max_suit == 1:
        tags.append("RAINBOW")
    if quads:
        tags.append("QUADS_ON_BOARD")
    elif trips:
        tags.append("TRIPS_ON_BOARD")
    elif paired:
        tags.append("PAIRED")
    if connected:
        tags.append("CONNECTED")

    return BoardInfo(
        cards=tuple(board), street=_street_name(len(board)), texture=texture,
        tags=tuple(tags),
        flush_made_possible=flush_made,
        flush_draw_possible=flush_draw,
        flush_suit_count=max_suit,
        paired=paired, trips=trips, quads=quads,
        connected=connected, straight_possible=straight_possible,
        monotone=max_suit == len(board) and len(board) >= 3,
        rainbow=max_suit == 1,
        ceiling=ceiling, ceiling_name=CATEGORY_NAMES_EN[ceiling],
        best_possible_category=rank.best_opponent_category,
        best_possible_name=(CATEGORY_NAMES_EN[rank.best_opponent_category]
                            if rank.best_opponent_category is not None else None),
        best_possible_cards=rank.best_opponent_cards,
    )


# ─────────────────────────── 警告 ───────────────────────────

def _warnings(made: MadeHand, draws: DrawInfo, rank: RankInfo,
              board: BoardInfo) -> tuple[str, ...]:
    cat = made.category
    out: list[str] = []

    if rank.hero_is_nuts:
        if rank.tie_pct > 0:
            out.append(
                f"You are the NUTS: no opponent holding beats you, but "
                f"{_pct(rank.tie_pct)} of holdings tie you (chop). Do not describe "
                f"a higher hand as possible; do not fold.")
        else:
            out.append(
                "You are the ABSOLUTE NUTS: no opponent holding beats you, and "
                "nothing ties. Do not describe a higher hand as possible; do not fold.")
    else:
        names = ", ".join(b.name for b in rank.beaten_by)
        out.append(f"You are NOT the nuts. Hands that beat you: {names}.")

        if board.flush_made_possible and cat < FLUSH:
            out.append(
                "A flush is ALREADY possible on this board. Your "
                f"{made.name} loses to any flush; do not call it the nuts and do "
                "not raise as if it were unbeatable.")
        if board.paired and cat < FULL_HOUSE:
            out.append(
                "The board is paired, so a full house (or quads) is possible. "
                f"Your {made.name} loses to those hands.")
        if board.paired and cat == TWO_PAIR:
            # h39 的根因：TT44 里的 44 是公对面，对手的 JJ/QQ/KK/AA 同样
            # 用 44 组成两对，而且对子更大。模型把「两对」当成强牌，其实
            # 它只赢诈唬——必须把「公对面的第二对是大家的」写死。
            out.append(
                "One of your two pairs is the BOARD pair — every opponent shares "
                "it. A higher pocket pair also makes a bigger two pair (e.g. JJ "
                "on T-4-4 is jacks and fours, beating your tens and fours), so "
                "this is a bluff-catcher, not a value monster.")
        if board.trips and cat < QUADS:
            out.append("There are trips on the board, so quads are possible.")
        if board.straight_possible and cat < STRAIGHT:
            out.append(
                "An opponent can complete a straight on this board. "
                f"Your {made.name} is not the nuts.")

    # 990010 的直接修复：两张同花 ≠ 花面。只在「顺子以上或自己有花听」时提醒，
    # 否则 22 在 88T 这种牌面也会收到一条无关的花听警告。
    if board.flush_draw_possible and cat < FLUSH and (cat >= STRAIGHT or draws.flush_draw):
        if board.street == "river":
            out.append(
                "At most 2 of any suit are on the river board: NO flush exists. "
                "Do not call this a flush board, and do not say a flush beats you.")
        else:
            out.append(
                "Only a flush DRAW is possible (no suit has 3+ on the board); "
                "NO made flush exists. Do not call this a flush board, and do not say "
                "a flush beats you right now.")

    if (rank.hero_is_nuts and board.flush_draw_possible
            and len(board.cards) < 5):
        out.append(
            "You are the nuts NOW, but a third suited card on a later street can "
            "make a flush. Do not slow-play if a flush card can price out your value.")

    if (draws.flush_draw or draws.straight_draw) and cat < STRAIGHT:
        out.append(
            "This is a DRAW, not a made hand: the rank/NUT STATUS above describe "
            "the current unmade hand. Judge the draw by its outs and the pot odds, "
            "not by that rank.")

    if draws.flush_draw and board.paired:
        out.append(
            "Your flush draw is on a paired board: a full house can already be "
            "out there, so hitting the flush is not automatically the nuts.")

    if len(board.cards) >= 5:
        out.append("River: no more cards to come; the hand is final.")
    return tuple(out)


# ─────────────────────────── 入口 ───────────────────────────

def compute_ground_truth(hero: str | list[str] | list[Card],
                         board: str | list[str] | list[Card],
                         opponents: int = 1) -> GroundTruth | None:
    """计算一手牌的完整事实块。

    翻前（公共牌 <3）或牌不全时返回 None —— 事实层不猜。调用方应把 None 当作
    「没有可注入的成手牌事实」，而不是伪造一份。
    """
    h = _coerce(hero)
    b = _coerce(board)
    if len(h) != 2 or len(b) < 3 or len(b) > 5:
        return None
    if len(set(h) | set(b)) != len(h) + len(b):
        return None                       # 去重后仍有重叠：牌数据不可靠
    if len(h) + len(b) < 5:
        return None

    rank = rank_hands(h, b, max(int(opponents), 1))
    best_combo, best_key = _best_five(h + b)
    cat = best_key[0]
    made = MadeHand(
        category=cat,
        name=CATEGORY_NAMES_EN[cat],
        name_cn=CATEGORY_NAMES[cat],
        detail=_describe(cat, best_key[1]),
        best_five=_order_five(best_combo, cat, best_key),
        hero_cards_used=tuple(c for c in best_combo if c in set(h)),
    )
    draws = _draws(h, b, cat)
    board_info = _board_info(b, rank)
    rank_info = RankInfo(
        opponents=max(int(opponents), 1),
        total=rank.total,
        win_pct=rank.win_pct,
        tie_pct=rank.tie_pct,
        beat_pct=rank.beat_pct,
        beat_at_least_pct=rank.beat_at_least_pct,
        equity_pct=rank.equity_pct,
        hero_is_nuts=rank.hero_is_nuts,
        beaten_by=tuple(
            BeatenByInfo(b.category, CATEGORY_NAMES_EN[b.category],
                         b.combos, b.example)
            for b in rank.beaten_by),
        best_opponent_category=rank.best_opponent_category,
        best_opponent_name=(CATEGORY_NAMES_EN[rank.best_opponent_category]
                            if rank.best_opponent_category is not None else None),
        best_opponent_cards=rank.best_opponent_cards,
    )
    return GroundTruth(
        hero=tuple(h), board=tuple(b), made=made, draws=draws,
        rank=rank_info, board_info=board_info,
        warnings=_warnings(made, draws, rank_info, board_info),
    )


def ground_truth_text(hero, board, opponents: int = 1) -> str:
    """给提示词直接用的字符串；翻前/牌不全返回空串。"""
    gt = compute_ground_truth(hero, board, opponents)
    return gt.render() if gt else ""
