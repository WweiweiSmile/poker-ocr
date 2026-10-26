"""牌面范围权益（Monte Carlo）：多人池的「牌面结构」事实层。

和 gto_lookup 的分工：
  · gto_lookup 查求解器的**策略**（单挑、单加注池），多人池返回 None；
  · 这里算**范围权益/坚果密度**，不依赖求解器，3–5 人池也能用。

对**实际牌面**现算（600 次抽样，约 0.05–0.1 s），不套 184 类代表牌面——
同类的 5c8c8d 和 6c9c9d 在坚果密度上差很多，牌面分析不能拿代表牌面糊弄。

任何查询失败一律返回 None，不影响主链路。
"""

from __future__ import annotations

import itertools
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import gto_lookup
from .coach import STRAIGHT, TWO_PAIR, evaluate
from .preflop_ranges import expand

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = [(r, s) for r in range(13) for s in SUITS]
GTO = Path(__file__).resolve().parents[1] / "gto"

_cache: dict[tuple[str, str], dict] | None = None
_specs: dict[str, Any] | None = None
_combo_cache: dict[str, list] = {}
_rng = random.Random(20261026)


@dataclass(frozen=True)
class BoardEquity:
    spot: str
    pfr_pos: str
    board: str
    rep_flop: str
    board_class: int
    class_total: int
    confidence: str
    field_size: int
    eq: dict[int, float]
    pfr_strong: float
    caller_strong: float
    pfr_nuts: float
    caller_nuts: float
    samples: int

    def render(self) -> str:
        lines = ["## Board structure (range equity, Monte Carlo live)"]
        conf = "EXACT" if self.confidence == "exact" else "class context"
        lines.append(
            f"Board: {self.board} | {self.spot} | {self.samples} samples | "
            f"class {self.board_class + 1}/{self.class_total} (rep {self.rep_flop}, {conf})"
        )
        eqs = " | ".join(
            f"{n} caller{'s' if n > 1 else ''} {self.eq[n]:.0%}" for n in (1, 2, 3) if n in self.eq
        )
        lines.append(f"PFR ({self.pfr_pos}) range equity: {eqs}")
        lines.append(
            f"Strong hands (two pair+): PFR {self.pfr_strong:.0%} vs caller "
            f"{self.caller_strong:.0%} | straight+: {self.pfr_nuts:.0%} vs {self.caller_nuts:.0%}"
        )
        adv = self.eq.get(1, 0.5) - 0.5
        strong = self.pfr_strong - self.caller_strong
        nuts = self.pfr_nuts - self.caller_nuts
        if adv > 0.04 or strong > 0.05 or nuts > 0.03:
            who = "PFR range/nut advantage"
        elif adv < -0.04 or strong < -0.05 or nuts < -0.03:
            who = "caller range/nut advantage"
        else:
            who = "roughly neutral ranges"
        n = self.field_size
        eq_now = self.eq.get(min(n, 3))
        tail = ""
        if n >= 2 and eq_now is not None and eq_now < 0.30:
            tail = " Multiway equity is low — value-heavy, no air c-bets."
        lines.append(f"Read: {who}; {n} opponent(s) in the pot.{tail}")
        lines.append(
            "Source: Monte Carlo on the actual board vs app ranges (caller = BB defense proxy). "
            "Range equity only — not a solver strategy."
        )
        return "\n".join(lines)


def enabled() -> bool:
    if os.environ.get("GTO_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return (gto_lookup._specs_dir() / "spots.json").exists()


def lookup(sit: Any) -> BoardEquity | None:
    try:
        return _lookup(sit)
    except Exception:  # noqa: BLE001
        return None


def _lookup(sit: Any) -> BoardEquity | None:
    if not enabled():
        return None
    snap = sit.snap
    if snap.street not in ("flop", "turn", "river") or len(snap.board) < 3:
        return None
    pfr_pos = _pfr_position(snap)
    if pfr_pos not in gto_lookup.OPENERS:
        return None
    spot = f"SRP_{pfr_pos}_vs_BB_100bb_app"
    board = "".join(snap.board[:3])
    row = _result(board, spot)
    if row is None:
        return None
    specs = _load_specs()
    class_id = specs["map"].get(_canon(board), 0) if specs else 0
    rep = specs["class_to_flop"].get(class_id, board) if specs else board
    return BoardEquity(
        spot=spot,
        pfr_pos=pfr_pos,
        board=board,
        rep_flop=rep,
        board_class=class_id,
        class_total=specs["k"] if specs else 1,
        confidence="exact" if rep == board else "class",
        field_size=min(max(int(sit.opponents_in_hand), 1), 3),
        eq=row["eq"],
        pfr_strong=row["pfr_strong"],
        caller_strong=row["caller_strong"],
        pfr_nuts=row["pfr_nuts"],
        caller_nuts=row["caller_nuts"],
        samples=row["samples"],
    )


# ─────────────────────────── Monte Carlo ───────────────────────────

def _combos_of(key: str) -> list[tuple[tuple[int, str], tuple[int, str]]]:
    a, b = key[0], key[1]
    ra, rb = RANKS.index(a), RANKS.index(b)
    if a == b:
        return [((ra, s1), (rb, s2)) for s1, s2 in itertools.combinations(SUITS, 2)]
    if key.endswith("s"):
        return [((ra, s), (rb, s)) for s in SUITS]
    return [((ra, s1), (rb, s2)) for s1 in SUITS for s2 in SUITS if s1 != s2]


def _range_combos(text: str) -> list:
    out = []
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        for key in expand(tok):
            out.extend(_combos_of(key))
    return list({tuple(sorted(c)): c for c in out}.values())


def _spot_ranges(spot: str) -> tuple[list, list]:
    if spot in _combo_cache:
        return _combo_cache[spot], _combo_cache[spot + "|caller"]
    specs = json.loads((gto_lookup._specs_dir() / "spots.json").read_text(encoding="utf-8"))["spots"]
    s = next(x for x in specs if x["id"] == spot)
    pfr = _range_combos(s["ip_range"])
    caller = _range_combos(s["oop_range"])
    _combo_cache[spot] = pfr
    _combo_cache[spot + "|caller"] = caller
    return pfr, caller


def _sample(combos, used: set):
    for _ in range(200):
        h = _rng.choice(combos)
        if h[0] not in used and h[1] not in used:
            used.add(h[0])
            used.add(h[1])
            return h
    raise RuntimeError("no non-conflicting hand")


def _equity(board_cards, pfr, caller, n, samples) -> float:
    eq = 0.0
    for _ in range(samples):
        used = set(board_cards)
        hands = [_sample(pfr, used)]
        for _ in range(n):
            hands.append(_sample(caller, used))
        deck = [c for c in DECK if c not in used]
        full = board_cards + _rng.sample(deck, 2)
        scores = [evaluate(list(h) + full) for h in hands]
        best = max(scores)
        winners = [i for i, s in enumerate(scores) if s == best]
        if 0 in winners:
            eq += 1.0 / len(winners)
    return eq / samples


def _strength(combos, board_cards) -> tuple[float, float]:
    strong = nuts = 0
    for h in combos:
        cat, _ = evaluate(list(h) + board_cards)
        if cat >= TWO_PAIR:
            strong += 1
        if cat >= STRAIGHT:
            nuts += 1
    n = max(len(combos), 1)
    return strong / n, nuts / n


def _result(board: str, spot: str) -> dict | None:
    global _cache
    if _cache is None:
        _cache = {}
    key = (board, spot)
    if key in _cache:
        return _cache[key]
    board_cards = [(RANKS.index(board[i]), board[i + 1]) for i in (0, 2, 4)]
    if len({(r, s) for r, s in board_cards}) < 3:
        return None
    pfr, caller = _spot_ranges(spot)
    samples = int(os.environ.get("GTO_EQUITY_SAMPLES", "600"))
    row = {
        "eq": {n: round(_equity(board_cards, pfr, caller, n, samples), 4) for n in (1, 2, 3)},
        "pfr_strong": 0.0,
        "caller_strong": 0.0,
        "pfr_nuts": 0.0,
        "caller_nuts": 0.0,
        "samples": samples,
    }
    row["pfr_strong"], row["pfr_nuts"] = [round(x, 4) for x in _strength(pfr, board_cards)]
    row["caller_strong"], row["caller_nuts"] = [round(x, 4) for x in _strength(caller, board_cards)]
    _cache[key] = row
    return row


def _canon(board: str) -> str:
    text, _ = gto_lookup._canonical_flop(board[0:2], board[2:4], board[4:6])
    return text


def _pfr_position(snap: Any) -> str | None:
    if snap.hero_is_pfr and snap.hero_position:
        return snap.hero_position
    pos = None
    for rec in snap.streets:
        if rec.street != "preflop":
            continue
        for a in rec.actions:
            if a.action in ("raise", "bet") and a.position:
                pos = a.position
    return pos


def _load_specs() -> dict | None:
    global _specs
    if _specs is not None:
        return _specs
    try:
        boards = json.loads((gto_lookup._specs_dir() / "boards.json").read_text(encoding="utf-8"))
        mapping = json.loads((gto_lookup._specs_dir() / "board_map.json").read_text(encoding="utf-8"))["map"]
    except (OSError, ValueError, KeyError):
        return None
    _specs = {
        "k": int(boards["k"]),
        "class_to_flop": {int(b["class_id"]): b["flop"] for b in boards["boards"]},
        "map": {str(k): int(v) for k, v in mapping.items()},
    }
    return _specs


def reset_cache() -> None:
    global _cache, _specs
    _cache = None
    _specs = None
