"""GTO 近似表查询：把 SQLite 里「牌面类 × spot × 节点」的解读成一段事实。

设计约束（对应 docs/GTO近似范围表-方案.md）：

- 只覆盖**单挑、单加注池、~100BB、翻牌/转牌**；其余情况返回 None，不猜。
- 牌面用花色同构映射到代表牌面；同构是精确的，跨类才是近似 → 结果带 confidence。
- 只给「解过的档」频率；未解的档（1/2、1.0）只用相邻档插值 EV，并标 approx。
- 查询同步 <10ms，失败一律 None，不影响主链路。
"""

from __future__ import annotations

import json
import os
import sqlite3
from array import array
from dataclasses import dataclass, field
from itertools import permutations
from pathlib import Path
from typing import Any

RANKS = "23456789TJQKA"
SUITS = "cdhs"
SUIT_PERMS = list(permutations(range(4)))

# 求解器的档位 id → App 的 option id
SIZE_TO_OPTION = {
    "33": "raise-1/3",
    "50": "raise-1/2",
    "67": "raise-2/3",
    "100": "raise-1",
    "120": "raise-1.2",
}
OPTION_TO_SIZE = {v: k for k, v in SIZE_TO_OPTION.items()}
FRACTIONS = {"raise-1/3": 1 / 3, "raise-1/2": 0.5, "raise-2/3": 2 / 3,
             "raise-1": 1.0, "raise-1.2": 1.2}

OPENERS = ("UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN")
_SPOT_PAIRS = {
    frozenset({"BB", p}): f"SRP_{p}_vs_BB_100bb_app" for p in OPENERS
}

GTO = Path(__file__).resolve().parents[1] / "gto"
_specs_cache: dict[str, Any] | None = None


# ─────────────────────────── 数据模型 ───────────────────────────

@dataclass(frozen=True)
class HandGTO:
    hand: str
    freqs: dict[str, float]              # option id → 百分比（只含解过的档）
    ev: dict[str, float]                 # option id → BB
    interpolated: dict[str, float]       # option id → BB（插值，只有 EV）
    best: str | None
    in_range: bool


@dataclass(frozen=True)
class GTOLookup:
    spot: str
    street: str
    node: str
    node_label: str
    board_class: int
    class_total: int
    rep_flop: str
    confidence: str                      # exact | class
    hand: HandGTO | None
    range_mix: dict[str, float]
    facing_bet: bool
    source: str
    caveats: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        lines = [f"## GTO baseline (approx table v1; HU, ~100BB, app ranges)"]
        conf = "EXACT (suit-isomorphic)" if self.confidence == "exact" else "class approximation"
        lines.append(
            f"Table: {self.spot} | class {self.board_class + 1}/{self.class_total} | "
            f"rep {self.rep_flop} | {conf}"
        )
        lines.append(f"Node: {self.node_label}")
        mix = " | ".join(f"{_short(k, self.facing_bet)} {v:.0f}%"
                         for k, v in self.range_mix.items() if v >= 0.5)
        if mix:
            lines.append(f"Equilibrium range mix: {mix or 'all check'}")
        if self.hand is not None and self.hand.in_range:
            h = self.hand
            parts = []
            for oid in ("call", "raise-1/3", "raise-1/2", "raise-2/3", "raise-1", "raise-1.2"):
                if oid in h.freqs:
                    ev = h.ev.get(oid)
                    parts.append(f"{_short(oid, self.facing_bet)} {h.freqs[oid]:.0f}%" +
                                 (f" (EV {ev:+.2f})" if ev is not None else ""))
            lines.append(f"{h.hand}: " + " | ".join(parts))
            if h.interpolated:
                approx = " | ".join(f"{_short(k, self.facing_bet)} EV {v:+.2f}"
                                    for k, v in h.interpolated.items())
                lines.append(f"[approx] {approx} (interpolated from solved sizes; no frequency)")
            if h.best and h.best in h.ev:
                best_ev = h.ev[h.best]
                others = [(o, v) for o, v in h.ev.items() if o != h.best]
                if others:
                    o2, v2 = max(others, key=lambda kv: kv[1])
                    gap = best_ev - v2
                    if gap <= 0.25:
                        lines.append(
                            f"EV is near-flat: {_short(h.best, self.facing_bet)} {best_ev:+.2f}BB "
                            f"vs {_short(o2, self.facing_bet)} {v2:+.2f}BB (gap {gap:.2f}BB) "
                            "— treat this as a mix, not a mandate."
                        )
                    else:
                        lines.append(
                            f"Highest-EV size: {_short(h.best, self.facing_bet)} ({best_ev:+.2f}BB), "
                            f"next {_short(o2, self.facing_bet)} ({v2:+.2f}BB, gap {gap:.2f}BB)."
                        )
                else:
                    lines.append(
                        f"Only {_short(h.best, self.facing_bet)} is available ({best_ev:+.2f}BB)."
                    )
        elif self.hand is not None and not self.hand.in_range:
            lines.append(
                f"{self.hand.hand}: OUTSIDE the modeled range for this spot — the table's "
                "per-hand strategy does not apply; use the range mix above as context only."
            )
        if self.caveats:
            lines.append("Caveats: " + "; ".join(self.caveats))
        lines.append(
            f"Source: {self.source}. Baseline vs app ranges; computed math, a reliable "
            "read, table dynamics and multiway rules override it."
        )
        return "\n".join(lines)


def _short(oid: str, facing: bool = False) -> str:
    if oid == "call":
        return "call" if facing else "check"
    if oid == "fold":
        return "fold"
    return oid.replace("raise-", "")


# ─────────────────────────── 入口 ───────────────────────────

def enabled() -> bool:
    if os.environ.get("GTO_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return _db_path().exists()


def lookup(sit: Any) -> GTOLookup | None:
    """从 Situation（duck-typed）查一张表；任何不满足的条件都返回 None。"""
    try:
        return _lookup(sit)
    except Exception:  # noqa: BLE001 — 查表绝不能打断建议主链路
        return None


def _lookup(sit: Any) -> GTOLookup | None:
    if not enabled():
        return None
    snap = sit.snap
    if snap.street not in ("flop", "turn"):
        return None
    if sit.opponents_in_hand != 1:
        return None
    if sit.pot_type != "single-raised pot":
        return None
    if not snap.hero_cards or len(snap.board) < 3:
        return None
    if sit.spr is None or not (5.5 <= float(sit.spr) <= 10.5):
        return None

    spot = _spot_id(sit)
    if spot is None:
        return None

    board = list(snap.board[:5])
    canon_flop, perm = _canonical_flop(board[0], board[1], board[2])
    specs = _load_specs()
    if specs is None:
        return None
    class_id = specs["map"].get(canon_flop)
    if class_id is None:
        return None
    rep = specs["class_to_flop"].get(class_id)
    if rep is None:
        return None
    confidence = "exact" if rep == canon_flop else "class"

    node = _resolve_node(sit, perm)
    if node is None:
        return None
    key, label, turn_card, turn = node

    conn = sqlite3.connect(_db_path().resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT solve_id, n_hands, iterations FROM solves WHERE flop=? AND job_id LIKE ?",
            (rep, spot + "__%"),
        ).fetchone()
        if not row:
            return None
        solve_id, n_hands, iters = row
        nd = conn.execute(
            "SELECT actions, range_mix, strategy, ev, actor FROM nodes "
            "WHERE solve_id=? AND street=? AND turn=? AND key=?",
            (solve_id, "turn" if turn else "flop", turn_card, key),
        ).fetchone()
        if not nd:
            return None
        actions = json.loads(nd[0])
        mix_raw = json.loads(nd[1])
        strat: bytes = nd[2]
        ev_raw = array("i")
        ev_raw.frombytes(nd[3])
        actor = nd[4]

        mix = _mix_to_options(actions, mix_raw)
        hand = _hand_gto(conn, solve_id, n_hands, actor, actions, strat, ev_raw, snap.hero_cards, perm)
        return GTOLookup(
            spot=spot,
            street=snap.street,
            node=key,
            node_label=label,
            board_class=class_id,
            class_total=specs["k"],
            rep_flop=rep,
            confidence=confidence,
            hand=hand,
            range_mix=mix,
            facing_bet="vs_b" in key,
            source=f"gto-v1/{specs['k']}class/{iters}it",
            caveats=_caveats(sit),
        )
    finally:
        conn.close()


# ─────────────────────────── 场景 / 节点 ───────────────────────────

def _spot_id(sit: Any) -> str | None:
    snap = sit.snap
    opps = [o for o in snap.opponents if not o.folded]
    if len(opps) != 1:
        return None
    hero, vill = snap.hero_position, opps[0].position
    if not hero or not vill:
        return None
    # OOP 必须是 BB，IP 必须是开池方（任一非盲位置）
    if sit.in_position is False and hero == "BB" and vill in OPENERS:
        return _SPOT_PAIRS[frozenset({"BB", vill})]
    if sit.in_position is True and hero in OPENERS and vill == "BB":
        return _SPOT_PAIRS[frozenset({"BB", hero})]
    return None


def _resolve_node(sit: Any, perm: dict[str, str]) -> tuple[str, str, str, bool] | None:
    snap = sit.snap
    if snap.street == "flop":
        return _resolve_flop(sit)
    if snap.street == "turn":
        return _resolve_turn(sit, perm)
    return None


def _resolve_flop(sit: Any) -> tuple[str, str, str, bool] | None:
    snap = sit.snap
    acts = snap.actions_of("flop")
    pot_start = _street_pot_start(snap, "flop")
    ip = bool(sit.in_position)
    if not acts:
        if ip:
            return None
        return "root", "flop, first to act (no bet yet)", "", False
    if len(acts) == 1:
        a = acts[0]
        if a.action == "check" and ip:
            return "after_check", "flop, checked to you (no bet)", "", False
        if a.action == "bet" and ip:
            lab = _bet_label(a.amount, pot_start)
            if lab is None:
                return None
            return f"vs_b{lab}", f"flop, facing a {_pct(lab)} pot bet", "", False
        return None
    if len(acts) == 2 and not ip:
        a0, a1 = acts
        if a0.action == "check" and a1.action == "bet":
            lab = _bet_label(a1.amount, pot_start)
            if lab is None:
                return None
            return f"after_check_vs_b{lab}", f"flop, you checked, villain bet {_pct(lab)} pot", "", False
    return None


def _resolve_turn(sit: Any, perm: dict[str, str]) -> tuple[str, str, str, bool] | None:
    snap = sit.snap
    if len(snap.board) < 4:
        return None
    flop = snap.actions_of("flop")
    pot_flop = _street_pot_start(snap, "flop")
    line = _flop_line(flop, pot_flop)
    if line is None:
        return None
    card = _canon_card(snap.board[3], perm)
    if card is None:
        return None
    acts = snap.actions_of("turn")
    ip = bool(sit.in_position)
    if not acts:
        if ip:
            return None
        return (f"turn:{card}:{line}:root", f"turn {card}, first to act (no bet)", card, True)
    if len(acts) == 1 and acts[0].action == "check" and ip:
        return (f"turn:{card}:{line}:after_check", f"turn {card}, checked to you", card, True)
    return None


def _flop_line(actions: list, pot_start: float | None) -> str | None:
    kinds = [a.action for a in actions]
    if kinds == ["check", "check"]:
        return "xx"
    if kinds == ["bet", "call"]:
        lab = _bet_label(actions[0].amount, pot_start)
        return f"b{lab}c" if lab else None
    if kinds == ["check", "bet", "call"]:
        lab = _bet_label(actions[1].amount, pot_start)
        return f"x_b{lab}c" if lab else None
    return None


def _street_pot_start(snap: Any, street: str) -> float | None:
    for s in snap.streets:
        if s.street == street:
            return s.pot_start
    return None


def _bet_label(amount: float | None, pot: float | None) -> str | None:
    if amount is None or not pot or pot <= 0:
        return None
    f = float(amount) / float(pot)
    best, bd = None, 1.0
    for lab, frac in (("33", 1 / 3), ("50", 0.5), ("67", 2 / 3), ("100", 1.0), ("120", 1.2)):
        d = abs(f - frac)
        if d < bd:
            best, bd = lab, d
    return best if bd <= 0.1 else None


def _pct(lab: str) -> str:
    return {"33": "1/3", "50": "1/2", "67": "2/3", "100": "pot", "120": "1.2"}.get(lab, lab)


# ─────────────────────────── 牌面 / 手牌 ───────────────────────────

def _canonical_flop(a: str, b: str, c: str) -> tuple[str, dict[str, str]]:
    """花色同构 → (规范翻牌字符串, 实际花色→规范花色 的映射)。"""
    cards = []
    for s in (a, b, c):
        if len(s) != 2 or s[0] not in RANKS or s[1] not in SUITS:
            raise ValueError(f"bad card {s!r}")
        cards.append((RANKS.index(s[0]), SUITS.index(s[1])))
    best, best_perm = None, None
    for perm in SUIT_PERMS:
        mapped = sorted((r, perm[s]) for r, s in cards)
        flat = tuple(x for pair in mapped for x in pair)
        if best is None or flat < best:
            best, best_perm = flat, perm
    assert best is not None and best_perm is not None
    text = "".join(RANKS[best[2 * i]] + SUITS[best[2 * i + 1]] for i in range(3))
    suit_map = {SUITS[i]: SUITS[best_perm[i]] for i in range(4)}
    return text, suit_map


def _canon_card(card: str, perm: dict[str, str]) -> str | None:
    if len(card) != 2 or card[0] not in RANKS or card[1] not in SUITS:
        return None
    return card[0] + perm[card[1]]


def _canon_hand(cards: tuple[str, ...], perm: dict[str, str]) -> str | None:
    if len(cards) != 2:
        return None
    mapped = []
    for c in cards:
        cc = _canon_card(c, perm)
        if cc is None:
            return None
        mapped.append(cc)
    mapped.sort(key=lambda x: (RANKS.index(x[0]), SUITS.index(x[1])), reverse=True)
    return mapped[0] + mapped[1]


# ─────────────────────────── 查询细节 ───────────────────────────

def _mix_to_options(actions: list[dict], mix: list[float]) -> dict[str, float]:
    """求解器动作 → App option id；多个动作映射到同一 id 时相加。"""
    out: dict[str, float] = {}
    for a, m in zip(actions, mix):
        oid = _action_to_option(a["id"])
        if oid is None:
            continue
        out[oid] = out.get(oid, 0.0) + float(m) * 100.0
    return {k: v for k, v in out.items() if v >= 0.5}


def _action_to_option(action_id: str) -> str | None:
    if action_id == "check":
        return "call"
    if action_id in ("fold", "call"):
        return action_id
    if action_id.startswith("bet-") or action_id.startswith("raise-"):
        return SIZE_TO_OPTION.get(action_id.split("-", 1)[1])
    return None  # allin / raw sizes 不映射


def _hand_gto(conn, solve_id, n_hands, actor, actions, strat, ev_raw, hero_cards, perm) -> HandGTO | None:
    combo = _canon_hand(hero_cards, perm)
    if combo is None:
        return None
    row = conn.execute(
        "SELECT idx, w_oop, w_ip FROM hands WHERE solve_id=? AND combo=?", (solve_id, combo)
    ).fetchone()
    if not row:
        return HandGTO(hand="".join(hero_cards), freqs={}, ev={}, interpolated={},
                       best=None, in_range=False)
    idx, w_oop, w_ip = row
    weight = w_oop if actor == 0 else w_ip
    if not weight or weight <= 0:
        return HandGTO(hand=combo, freqs={}, ev={}, interpolated={}, best=None, in_range=False)

    freqs: dict[str, float] = {}
    evs: dict[str, float] = {}
    for k, a in enumerate(actions):
        oid = _action_to_option(a["id"])
        if oid is None:
            continue
        freqs[oid] = freqs.get(oid, 0.0) + float(strat[k * n_hands + idx])
        evs[oid] = evs.get(oid, 0.0) + float(ev_raw[k * n_hands + idx]) / 100.0
    freqs = {k: v for k, v in freqs.items() if v >= 0.5}

    solved_fracs = {OPTION_TO_SIZE[o]: f for o, f in FRACTIONS.items() if o in freqs}
    interp = _interpolate_ev(solved_fracs, evs)
    best = max(evs, key=evs.get) if evs else None
    return HandGTO(hand=combo, freqs=freqs, ev=evs, interpolated=interp, best=best, in_range=True)


def _interpolate_ev(solved_fracs: dict[str, float], evs: dict[str, float]) -> dict[str, float]:
    """未解档的 EV 用相邻已解档线性插值；只在两侧都有解时给值。"""
    points = sorted(
        (FRACTIONS[o], evs[o]) for o in evs if o in FRACTIONS
    )
    if len(points) < 2:
        return {}
    out: dict[str, float] = {}
    for oid, frac in FRACTIONS.items():
        if oid in evs:
            continue
        lo = max((p for p in points if p[0] < frac), default=None)
        hi = min((p for p in points if p[0] > frac), default=None)
        if lo is None or hi is None:
            continue
        t = (frac - lo[0]) / (hi[0] - lo[0])
        out[oid] = lo[1] + t * (hi[1] - lo[1])
    return out


# ─────────────────────────── 规格 / 缓存 ───────────────────────────

def _db_path() -> Path:
    p = os.environ.get("GTO_TABLE_PATH")
    return Path(p) if p else GTO / "data" / "gto.sqlite"


def _specs_dir() -> Path:
    p = os.environ.get("GTO_SPECS_DIR")
    return Path(p) if p else GTO / "specs"


def _load_specs() -> dict[str, Any] | None:
    global _specs_cache
    if _specs_cache is not None:
        return _specs_cache
    d = _specs_dir()
    try:
        boards = json.loads((d / "boards.json").read_text(encoding="utf-8"))
        mapping = json.loads((d / "board_map.json").read_text(encoding="utf-8"))["map"]
    except (OSError, ValueError, KeyError):
        return None
    _specs_cache = {
        "k": int(boards["k"]),
        "class_to_flop": {int(b["class_id"]): b["flop"] for b in boards["boards"]},
        "map": {str(k): int(v) for k, v in mapping.items()},
    }
    return _specs_cache


def reset_cache() -> None:
    """测试/换表后调用。"""
    global _specs_cache
    _specs_cache = None


def _caveats(sit: Any) -> tuple[str, ...]:
    out = []
    if sit.snap.street == "turn":
        out.append("turn nodes only cover flop lines xx / b-c / x-b-c")
    return tuple(out)
