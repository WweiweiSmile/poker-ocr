"""手牌 vs 范围：现在领先谁、打到河牌有多少权益。

和 `coach.beat_rate` 的区别：那个把对手当成「随机两手牌」，是下界；
这里用每个对手的**翻前先验 × 他的每一个翻后动作**（`range_flow` 事件模型，
按时间顺序连乘；`RANGE_EVENT_MODEL=0` 回退旧口径「先验 × 当前街动作」），
回答：

  · `behind_i` / `behind_any`：**当前成手牌**严格大于我的范围比例；
  · `tie_i` / `tie_any`：平局比例；
  · `equity`：同一组手牌发完转河后的权益（平局按 1/赢家数分池）；
  · `beaten_by`：比我大的牌按牌型/对子位置分组，带 169 键示例。

几个容易错的点（设计文档 §8）：
  · 平局不是领先：`ahead = 1 - behind - tie`；
  · river 不再发牌，发牌张数 = 5 - len(board)；
  · 权重 0 的组合不参与精确统计，也不能被采样到；
  · 联合 `behind_any` 用**整组拒绝重抽**：先抽 A 再按去重牌抽 B 的顺序
    条件抽样有系统偏差；
  · 采样种子由稳定 hash 派生（不用 Python 内置 `hash()`），同一决策点输出确定。
"""

from __future__ import annotations

import bisect
import math
import os
import random
import threading
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import board_range, field_ranges, range_flow, ranges
from .coach import (
    CATEGORY_NAMES_EN, FLUSH, FULL_HOUSE, ONE_PAIR, QUADS, STRAIGHT,
    STRAIGHT_FLUSH, TRIPS, TWO_PAIR, evaluate, parse_cards,
)
from .hand_track import PREFLOP, HandSnapshot

# ─────────────────────────── 数据模型 ───────────────────────────

@dataclass(frozen=True)
class RangeGroup:
    """比我大的牌的一个分组（范围加权）。share 是占整个范围的比例。"""

    name: str
    share: float
    combos: int
    examples: tuple[str, ...] = ()

    def render(self) -> str:
        ex = f" ({', '.join(self.examples)})" if self.examples else ""
        return f"{self.name} {self.share:.1%}{ex}"


@dataclass(frozen=True)
class OpponentEdge:
    seat_id: str
    range_id: str
    width_pct: float
    behind: float
    tie: float
    ahead: float
    beaten_by: tuple[RangeGroup, ...] = ()
    ahead_groups: tuple[RangeGroup, ...] = ()
    position: str | None = None
    mode: str = "call"
    combos_after_removal: int = 0
    # 事件模型的附带事实：当前动作落在哪个桶、最后一条动作是不是全下、
    # 以及范围在当前牌面上的成牌/听牌/空气分区。旧路径下都是空/None。
    bucket: str = ""
    # 有效宽度：按权重从高到低累加到 90% 质量所需的 combo 占比。事件模型
    # 的连乘会把长尾压到极低但不清零，直接报支撑集宽度（width_pct）会永远
    # 接近 100%，"范围 6%" 这个最有用的数字就没了。
    eff_width_pct: float = 0.0
    jam: bool = False
    composition: "range_flow.Composition | None" = field(
        default=None, compare=False, repr=False)
    # 这手牌走到现在的动作链（时间顺序）。渲染线路用，不参与相等比较。
    events: tuple = field(default=(), compare=False, repr=False)
    # 本手收窄后的范围（翻前先验 × 当前街动作过滤）。fold_equity 要用它按
    # 权重算条件弃牌率；compare=False 是为了不改变 dataclass 的相等语义
    # （Range 里带 dict，参与比较会又慢又容易误判）。
    rng: "ranges.Range | None" = field(default=None, compare=False, repr=False)

    @property
    def label(self) -> str:
        parts = [self.position or self.seat_id]
        if self.mode:
            parts.append(f"[{self.mode}]")
        return " ".join(parts)

    @property
    def line(self) -> str:
        """这条动作链的英文摘要（内部/日志用）。"""
        return range_flow.line_note(self.events)


@dataclass(frozen=True)
class HandVsRanges:
    hero: str
    board: str
    street: str
    opponents: tuple[OpponentEdge, ...]
    behind_any: float
    tie_any: float
    equity: float
    equity_ci: float
    samples: int
    # 未建模对手：按随机牌纳入 MC，但不在 opponents 里展开逐家范围。
    random_opponents: int = 0
    warnings: tuple[str, ...] = ()
    context: str = ""
    source: str = ""

    def render(self) -> str:
        lines: list[str] = ["## Hand vs ranges" + (f" ({self.context})" if self.context else "")]
        lines.append(f"Board: {self.board} | Your {self.hero} | {self.street} | "
                     f"{self.samples} samples")
        # 4+ 对手不逐家铺开：只展开最危险的三家，其余仍进入 equity/behind_any。
        shown = self.opponents
        more = 0
        if len(shown) > 3:
            shown = tuple(sorted(shown, key=lambda e: -e.behind)[:3])
            more = len(self.opponents) - 3
        lines.append("Ranges: " + " | ".join(
            f"{o.label} {o.range_id} {o.eff_width_pct or o.width_pct:.1f}% "
            f"({o.combos_after_removal} live combos)" for o in shown)
            + (f" | +{more} more (in equity)" if more else "")
            + (f" | +{self.random_opponents} random fill (unmodeled)"
               if self.random_opponents else ""))
        lines.append(f"Equity vs field (by river): {self.equity:.0%} "
                     f"±{max(self.equity_ci * 100, 0.1):.1f}%"
                     + (f" (incl. {self.random_opponents} random fill)"
                        if self.random_opponents else ""))
        lines.append(f"Made hand now: behind at least one {self.behind_any:.1%} "
                     f"(tie {self.tie_any:.1%})"
                     + (f"; {self.random_opponents} opponent(s) treated as random"
                        if self.random_opponents else ""))
        for edge in shown:
            group_text = ""
            if edge.beaten_by:
                group_text = " — " + ", ".join(g.render() for g in edge.beaten_by[:2])
            lines.append(f"- {edge.label} {edge.eff_width_pct or edge.width_pct:.1f}%:"
                         f" behind {edge.behind:.1%} (tie {edge.tie:.1%}){group_text}")
            comp = edge.composition
            if comp is not None:
                types = ", ".join(f"{name} {share:.0%}"
                                  for name, share in comp.types[:3])
                lines.append(f"  line={edge.line or 'n/a'} bucket={edge.bucket or 'n/a'}"
                             f" | made {comp.made_hand:.0%} draw {comp.draw:.0%} "
                             f"air {comp.air:.0%}" + (f" ({types})" if types else ""))
        behind_by_type: dict[str, RangeGroup] = {}
        for edge in self.opponents:
            for group in edge.beaten_by:
                old = behind_by_type.get(group.name)
                if old is None or group.share > old.share:
                    behind_by_type[group.name] = group
        if behind_by_type:
            groups = sorted(behind_by_type.values(), key=lambda g: -g.share)[:4]
            lines.append("Behind by type: " + ", ".join(g.render() for g in groups))
        if more:
            lines.append(f"- +{more} more range(s) are included in equity / "
                         "behind-at-least-one; not expanded here.")
        ahead = self._ahead_names()
        if ahead:
            lines.append("Ahead of: " + ", ".join(ahead))
        lines.append("Read: " + self._read())
        if self.source:
            lines.append(f"Source: {self.source}")
        return "\n".join(lines)

    def _ahead_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for edge in self.opponents:
            for group in edge.ahead_groups:
                if group.name not in names:
                    names.append(group.name)
        return tuple(names[:4])

    def _read(self) -> str:
        if self.behind_any >= 0.5 and self.equity >= 0.33:
            return ("behind now but the equity is still live — this is a draw / "
                    "multiway spot, judge it by price, not by 'behind now'.")
        if self.behind_any >= 0.5:
            return ("behind now and low equity — weak against this range; needs "
                    "a very good price to continue.")
        if self.behind_any <= 0.2 and self.equity >= 0.55:
            return ("ahead of this range now and enough equity — a value hand; "
                    "the equity number, not the random-hand rank, is the truth.")
        return ("mixed spot: 'behind now' is the current made hand, 'equity' is "
                "by the river; do not use one as the other.")


# ─────────────────────────── 输入整理 ───────────────────────────

def _as_cards(value) -> list[tuple[int, str]]:
    if isinstance(value, str):
        return parse_cards(value)
    out = []
    for item in value:
        if isinstance(item, str):
            out.append(parse_cards(item)[0])
        else:
            out.append((int(item[0]), str(item[1]).lower()))
    return out


_Entry = tuple[str, "str | None", ranges.Range, str, str, tuple]


def _normalize_opponents(opponents: Sequence[Any]) -> list[_Entry]:
    """接受 Range、`(seat, Range)`、`(seat, pos, Range)`、`(seat, pos, Range, mode)`、
    `(seat, pos, Range, mode, bucket, events)`，以及带同名属性的对象。"""
    out: list[_Entry] = []
    for i, item in enumerate(opponents):
        if isinstance(item, ranges.Range):
            out.append((f"r{i}", None, item, "call", "", ()))
            continue
        if isinstance(item, (tuple, list)):
            if len(item) == 2:
                seat, rng = item
                out.append((str(seat), None, rng, "call", "", ()))
                continue
            if len(item) == 3:
                seat, pos, rng = item
                out.append((str(seat), pos, rng, "call", "", ()))
                continue
            seat, pos, rng = str(item[0]), item[1], item[2]
            mode = str(item[3]) if len(item) > 3 else "call"
            bucket = str(item[4]) if len(item) > 4 else ""
            events = tuple(item[5]) if len(item) > 5 else ()
            out.append((seat, pos, rng, mode, bucket, events))
            continue
        # 兼容 duck typing：对象带 .range / .range_obj
        rng = getattr(item, "range", None) or getattr(item, "range_obj", None)
        if isinstance(rng, ranges.Range):
            out.append((str(getattr(item, "seat_id", f"r{i}")),
                        getattr(item, "position", None), rng,
                        str(getattr(item, "mode", "call")),
                        str(getattr(item, "bucket", "") or ""),
                        tuple(getattr(item, "events", ()) or ())))
    return out


# ─────────────────────────── 分组 ───────────────────────────

def _group_name(cat: int, combo, board) -> str:
    if cat == TRIPS:
        feat = board_range.classify(combo, board)
        return "sets" if feat.pair_kind == "set" else "trips"
    if cat == TWO_PAIR:
        return "two pair"
    if cat == STRAIGHT:
        return "straight"
    if cat == FLUSH:
        return "flush"
    if cat == FULL_HOUSE:
        return "full house"
    if cat == QUADS:
        return "quads"
    if cat == STRAIGHT_FLUSH:
        return "straight flush"
    if cat == ONE_PAIR:
        feat = board_range.classify(combo, board)
        return {
            "overpair": "overpair",
            "top": "top pair",
            "second": "second pair",
            "middle": "middle pair",
            "bottom": "bottom pair",
            "underpair": "underpair",
            "board": "board pair",
        }.get(feat.pair_kind, "one pair")
    return "high card / air"


def _examples(weights: dict, to_text) -> tuple[str, ...]:
    """按 169 键聚合示例，显示组合数：AA×1, 44×3。"""
    counter: Counter[str] = Counter()
    for combo in weights:
        counter[to_text(combo)] += 1
    return tuple(f"{key}×{n}" for key, n in counter.most_common(3))


def _exact_edge(seat_id: str, position: str | None, rng: ranges.Range,
                mode: str, hero_cards, board, hero_score,
                bucket: str = "", events: tuple = ()) -> OpponentEdge | None:
    dead = set(hero_cards) | set(board)
    weights: dict[tuple, float] = {}
    for combo, w in rng.combo_weights.items():
        if w <= 0:
            continue
        if combo[0] in dead or combo[1] in dead:
            continue
        weights[combo] = float(w)
    if not weights:
        return None
    total = sum(weights.values())
    if total <= 0:
        return None

    behind = tie = ahead = 0.0
    behind_groups: dict[str, float] = {}
    behind_combo_count: dict[str, int] = {}
    behind_members: dict[str, dict] = {}
    ahead_groups: dict[str, float] = {}
    for combo, w in weights.items():
        score = evaluate(list(combo) + board)
        if score > hero_score:
            behind += w
            name = _group_name(score[0], combo, board)
            behind_groups[name] = behind_groups.get(name, 0.0) + w
            behind_combo_count[name] = behind_combo_count.get(name, 0) + 1
            behind_members.setdefault(name, {})[combo] = w
        elif score == hero_score:
            tie += w
        else:
            ahead += w
            name = CATEGORY_NAMES_EN[score[0]]
            ahead_groups[name] = ahead_groups.get(name, 0.0) + w

    groups = []
    for name, w in sorted(behind_groups.items(), key=lambda kv: -kv[1]):
        groups.append(RangeGroup(
            name=name,
            share=w / total,
            combos=behind_combo_count.get(name, 0),
            examples=_examples(behind_members.get(name, {}), ranges.key_of_combo),
        ))
    ahead_list = []
    for name, w in sorted(ahead_groups.items(), key=lambda kv: -kv[1]):
        ahead_list.append(RangeGroup(name=name, share=w / total, combos=0))

    return OpponentEdge(
        seat_id=seat_id,
        range_id=rng.id,
        width_pct=rng.width_pct,
        eff_width_pct=rng.effective_width_pct() or rng.width_pct,
        behind=behind / total,
        tie=tie / total,
        ahead=ahead / total,
        beaten_by=tuple(groups),
        ahead_groups=tuple(ahead_list),
        position=position,
        mode=mode,
        combos_after_removal=len(weights),
        bucket=bucket,
        jam=bool(events and events[-1].all_in),
        composition=range_flow.composition(rng, board) if events else None,
        events=events,
        rng=rng,
    )


# ─────────────────────────── Monte Carlo ───────────────────────────

def _samples_for(n: int) -> int:
    raw = os.environ.get("HAND_VS_RANGES_SAMPLES", "").strip()
    try:
        samples = int(raw) if raw else 2000
    except ValueError:
        samples = 2000
    samples = max(200, min(samples, 20000))
    # 3-way（2 家）2000；4-way（3 家）起降到 1500，控制多人池耗时。
    if n >= 3:
        samples = min(samples, 1500)
    return samples


def _seed(hero: str, board: str, entries, samples: int,
          seed_key: str | None, random_opponents: int = 0) -> int:
    text = "|".join([
        seed_key or "", hero, board, str(samples), f"rnd:{random_opponents}",
        *(f"{o.seat_id}:{o.range_id}:{o.width_pct:.2f}" for o in entries),
    ])
    return zlib.crc32(text.encode("utf-8"))


def _sampler(rng: ranges.Range, dead: set) -> tuple[list, list, list] | None:
    combos = []
    weights = []
    for combo, w in rng.combo_weights.items():
        if w <= 0 or combo[0] in dead or combo[1] in dead:
            continue
        combos.append(combo)
        weights.append(float(w))
    if not combos:
        return None
    cumulative = []
    acc = 0.0
    for w in weights:
        acc += w
        cumulative.append(acc)
    return combos, weights, cumulative


def _draw(combos: list, cumulative: list, rnd: random.Random):
    target = rnd.random() * cumulative[-1]
    idx = bisect.bisect_right(cumulative, target)
    if idx >= len(combos):
        idx = len(combos) - 1
    return combos[idx]


def _monte_carlo(hero_cards, board, ranges_list, samples: int,
                 seed: int, random_opponents: int = 0
                 ) -> tuple[float, float, float, int, int]:
    """返回 (behind_any, tie_any, equity, valid_samples, failed_groups)。

    `random_opponents` 是未建模对手数：它们不在 `ranges_list` 里，但按均匀
    随机牌参与每一轮抽样，避免多人池里只对前 4 家算权益而高估胜率。
    """
    dead = set(hero_cards) | set(board)
    samplers = [_sampler(rng, dead) for rng in ranges_list]
    if any(s is None for s in samplers):
        return 0.0, 0.0, 0.0, 0, samples
    if random_opponents > 0:
        uniform = _sampler(ranges.uniform_range("RANDOM_FILL",
                                                note="unmodeled opponent: random cards"),
                           dead)
        if uniform is None:
            return 0.0, 0.0, 0.0, 0, samples
        # 每个未建模对手独立抽两手牌；共享同一份 combo 列表即可，抽样时
        # 会按 used 去重，所以不会和已建模对手/彼此撞牌。
        samplers.extend([uniform] * int(random_opponents))
    rnd = random.Random(seed)
    hero_score = evaluate(hero_cards + board)
    deck = [c for c in ranges.DECK if c not in dead]
    need = max(5 - len(board), 0)

    behind_any = tie_any = equity = 0.0
    valid = failed = 0
    for _ in range(samples):
        used = set(dead)
        hands = []
        ok = True
        for combos, _weights, cumulative in samplers:
            for _attempt in range(200):
                combo = _draw(combos, cumulative, rnd)
                if combo[0] not in used and combo[1] not in used:
                    used.add(combo[0])
                    used.add(combo[1])
                    hands.append(combo)
                    break
            else:
                ok = False
                break
        if not ok:
            failed += 1
            continue
        valid += 1

        current = [evaluate(list(h) + board) for h in hands]
        if any(s > hero_score for s in current):
            behind_any += 1
        elif any(s == hero_score for s in current):
            tie_any += 1

        if need:
            runout = board + rnd.sample([c for c in deck if c not in used], need)
        else:
            runout = board
        scores = [evaluate(list(h) + runout) for h in hands]
        hero_runout = evaluate(hero_cards + runout)
        best = max([hero_runout, *scores])
        winners = sum(1 for s in scores if s == best)
        if hero_runout == best:
            equity += 1.0 / (winners + 1)

    if valid <= 0:
        return 0.0, 0.0, 0.0, 0, failed
    return (behind_any / valid, tie_any / valid, equity / valid, valid, failed)


# ─────────────────────────── 主入口 ───────────────────────────

_CACHE: dict[tuple, HandVsRanges | None] = {}
_CACHE_MAX = 256
_CACHE_LOCK = threading.Lock()


def _cache_key(hero: str, board: str, edges, sampled_ranges, samples: int,
               seed_key: str | None, context: str, source: str,
               warnings: Iterable[str], random_opponents: int = 0) -> tuple:
    return (
        hero, board, samples, seed_key or "", context, source, tuple(warnings),
        int(random_opponents),
        tuple(
            (o.seat_id, o.position, o.range_id, o.mode, o.bucket,
             round(o.width_pct, 3), round(o.behind, 6), round(o.tie, 6),
             round(o.ahead, 6), id(rng))
            for o, rng in zip(edges, sampled_ranges)
        ),
    )


def compute(hero, board, opponents: Sequence[Any], *, samples: int | None = None,
            warnings: Iterable[str] = (), context: str = "",
            source: str = "", seed_key: str | None = None,
            random_opponents: int = 0) -> HandVsRanges | None:
    """计算手牌对多个对手范围的强度。

    `opponents` 支持 Range、`(seat, Range)`、`(seat, position, Range)`、
    `(seat, position, Range, mode)`；也兼容带 `.range`/`.seat_id` 的对象。

    `random_opponents`：未建模对手数。它们不展开逐家范围，但按均匀随机牌
    参与 MC，保证多人池 equity/behind_any 不会只算前 4 家而虚高。
    """
    try:
        hero_cards = _as_cards(hero)
        board_cards = _as_cards(board)
    except (ValueError, TypeError):
        return None
    if len(hero_cards) != 2 or not (3 <= len(board_cards) <= 5):
        return None
    if len(set(hero_cards)) != 2 or len(set(board_cards)) != len(board_cards):
        return None
    if set(hero_cards) & set(board_cards):
        return None

    random_opponents = max(0, int(random_opponents))
    normalized = _normalize_opponents(opponents)
    if not normalized and random_opponents <= 0:
        return None
    if samples is None:
        samples = _samples_for(len(normalized) + random_opponents)
    else:
        samples = max(1, int(samples))

    hero_text = "".join(f"{ranges.RANK_CHARS[r]}{s}" for r, s in hero_cards)
    board_text = "".join(f"{ranges.RANK_CHARS[r]}{s}" for r, s in board_cards)

    edges: list[OpponentEdge] = []
    sampled_ranges: list[ranges.Range] = []
    skipped: list[str] = []
    for seat, pos, rng, mode, bucket, events in normalized:
        if not isinstance(rng, ranges.Range):
            continue
        edge = _exact_edge(seat, pos, rng, mode, hero_cards, board_cards,
                           evaluate(hero_cards + board_cards), bucket, events)
        if edge is None:
            skipped.append(f"{seat}: range empty after removing dead cards")
            continue
        edges.append(edge)
        sampled_ranges.append(rng)
    if not edges and random_opponents <= 0:
        return None

    key = _cache_key(hero_text, board_text, edges, sampled_ranges, samples,
                     seed_key, context, source, warnings, random_opponents)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]

    seed = _seed(hero_text, board_text, edges, samples, seed_key,
                 random_opponents)
    behind_any, tie_any, equity, valid, failed = _monte_carlo(
        hero_cards, board_cards, sampled_ranges, samples, seed,
        random_opponents)
    if valid <= 0:
        return None

    all_warnings = list(dict.fromkeys([*warnings, *skipped]))
    ids = [e.range_id for e in edges]
    if len(set(ids)) < len(ids):
        all_warnings.append("two or more opponents use the same range: their "
                            "hands are sampled as independent draws")
    if failed > samples * 0.10:
        all_warnings.append(f"{failed}/{samples} Monte Carlo groups rejected on "
                            "card conflicts; results are approximate")
    ci = math.sqrt(max(equity * (1.0 - equity), 0.0) / max(valid, 1))
    result = HandVsRanges(
        hero=hero_text,
        board=board_text,
        street={3: "flop", 4: "turn", 5: "river"}.get(len(board_cards), ""),
        opponents=tuple(edges),
        behind_any=behind_any,
        tie_any=tie_any,
        equity=equity,
        equity_ci=ci,
        samples=valid,
        random_opponents=random_opponents,
        warnings=tuple(all_warnings),
        context=context,
        source=source,
    )
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = result
    return result


# ─────────────────────────── 快照接线 ───────────────────────────

def enabled() -> bool:
    return os.environ.get("HAND_VS_RANGES_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _context(snap: HandSnapshot, specs: Sequence[field_ranges.OpponentRangeSpec],
             n: int) -> str:
    parts: list[str] = []
    if snap.bomb_pot:
        parts.append("bomb pot")
    elif snap.hero_is_pfr and snap.hero_position:
        parts.append(f"{snap.hero_position} open (hero)")
    has_open = bool(parts)
    for spec in specs:
        if spec.role == "pfr":
            if not has_open:
                parts.append(f"{spec.position or '?'} open")
                has_open = True
        elif spec.role == "three_bettor":
            parts.append(f"{spec.position or '?'} 3-bet")
        elif spec.role == "bb_def":
            parts.append(f"{spec.position or 'BB'} defend")
        elif spec.role == "cold_call":
            parts.append(f"{spec.position or '?'} cold-call")
        elif spec.role == "limper":
            parts.append(f"{spec.position or '?'} limp")
        elif spec.role == "bb_check":
            parts.append(f"{spec.position or 'BB'} check")
    if not parts:
        return f"{n}-way: preflop priors"
    return f"{n}-way: " + ", ".join(dict.fromkeys(parts))


def lookup(sit: Any) -> HandVsRanges | None:
    """advice 的接线入口：快照 → 每对手范围 → 强度。任何异常都 fail open。"""
    try:
        return _lookup(sit)
    except Exception:  # noqa: BLE001
        return None


def _archetypes(sit: Any) -> dict[str, str]:
    """本手逐座位画像 → {seat_id: archetype}。没有画像的座位不猜。"""
    out: dict[str, str] = {}
    for read in getattr(sit, "opponent_reads", ()) or ():
        seat = getattr(read, "seat_id", None)
        archetype = getattr(read, "archetype", None)
        if seat and archetype and archetype != "unknown":
            out[str(seat)] = str(archetype)
    return out


def _lookup(sit: Any) -> HandVsRanges | None:
    if not enabled():
        return None
    snap = getattr(sit, "snap", sit)
    if snap.street == PREFLOP or len(snap.board) < 3:
        return None
    if not snap.hero_cards:
        return None
    field = field_ranges.resolve(snap)
    if not field.specs:
        return None

    board = "".join(snap.board[:5])
    try:
        dead = set(_as_cards(snap.hero_cards)) | set(_as_cards(board))
    except (ValueError, TypeError):
        dead = set()
    archetypes = _archetypes(sit)
    use_events = range_flow.enabled()

    entries = []
    warnings = list(field.warnings)
    used_specs = []
    for spec in field.specs:
        if not spec.prior_id:
            continue
        prior = ranges.get(spec.prior_id)
        if prior is None:
            warnings.append(f"seat {spec.seat_id}: range {spec.prior_id} not found")
            continue
        built = None
        bucket = ""
        if use_events and spec.events:
            built = range_flow.build_range(
                prior, spec.events,
                archetype=archetypes.get(spec.seat_id, "unknown"),
                dead=dead)
            bucket = range_flow.bucket_of(spec.events[-1])
        if built is None:
            # 事件链不可用（没有翻后动作 / 牌面缺张）时退回旧口径：先验 ×
            # 当前街动作。宁可窄一点，也不要拿翻前先验冒充收窄后的范围。
            built = board_range.build(board, spec.mode, prior)
        if built is None:
            warnings.append(f"seat {spec.seat_id}: board range empty "
                            f"({spec.mode}, {spec.prior_id})")
            continue
        entries.append((spec.seat_id, spec.position, built, spec.mode,
                        bucket, spec.events))
        used_specs.append(spec)
    active = [o for o in snap.opponents if not o.folded]
    # 建模上限是 4 家（field_ranges.resolve 的渲染/性能 cap）。其余在局对手
    # 不展开范围，但必须按随机牌纳入 MC，否则 8 人池的 equity 只对 4 家算，
    # 会被当成全桌权益使用。
    random_opponents = max(len(active) - len(entries), 0)
    if not entries:
        # 一家可确认范围都没有时，不生成范围权益块；保留 gt 的随机牌下界，
        # 避免把「全随机」冒充成「对手范围」。
        return None

    result = compute(
        "".join(snap.hero_cards), board, entries,
        warnings=warnings,
        context=_context(snap, used_specs, len(active) + 1),
        source=("event model: preflop prior × every postflop action in order "
                "(street kept in sync); unmodeled opponents are sampled as "
                "random hands; not a solver and not range-vs-range equity."
                if use_events else
                "descriptive preflop priors × current-street action filter; "
                "unmodeled opponents are sampled as random hands; "
                "not a solver and not range-vs-range equity."),
        seed_key=snap.hand_id,
        random_opponents=random_opponents,
    )
    return result


def reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "HandVsRanges", "OpponentEdge", "RangeGroup", "compute", "enabled",
    "lookup", "reset_cache",
]
