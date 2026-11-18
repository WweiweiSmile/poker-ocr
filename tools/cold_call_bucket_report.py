#!/usr/bin/env python3
"""量化「冷跟先验按入池人数分桶」的影响量（票 #77，只读、可复现）。

    nv tools/cold_call_bucket_report.py                    # 终端摘要
    nv tools/cold_call_bucket_report.py --samples 20000 --seeds 3
    nv tools/cold_call_bucket_report.py --json /tmp/cold-call-77.json
    nv tools/cold_call_bucket_report.py --draft-table      # 只打印三档草案表
    nv tools/cold_call_bucket_report.py --list-points      # 逐点明细

只读：不写生产路径、不调模型、不连库。`logs/` `runs/` 只读打开。

## 比的是什么

同一批**已录的真实决策点**上，同一套统一口径（`hand_vs_ranges._monte_carlo`
整组拒绝重抽 + 对手范围 + `range_flow` 事件收窄），只换一个变量：冷跟先验。

  · **现状单桶**：`ranges.get(spec.prior_id)` —— `COLD_CALL_IP` 20.21% /
    `COLD_CALL_OOP` 16.74%，所有入池人数共用一个桶（`poker_ocr/ranges.py:262`
    与 `:269`；`get()` 的 `COLD_CALL_` 分支在 `:380-386` 只认 IP/OOP，
    `_vs_<开池位置>` 后缀被丢弃）。
  · **按人数分桶**：同一个冷跟座位，按它**跟注时已经在池里的人数**（开池者
    + 它之前所有自愿进池的跟注/溜入 + 它自己）取档，`n_way` 2/3/4 各一档；
    `n_way=2` 就是现状数值（票 #78 的验收要求：2 人池不许变）。

两臂用**同一个随机种子**跑 `_monte_carlo`（common random numbers），所以差值
不是两次独立抽样的噪声，而是「同一组随机数、换一套先验」的配对差。

## 固定 seed

`zlib.crc32("cold-call-bucket-v1|<hand_id>|<street>|<hero>|<board>|<k>")`，
k 是第几组种子（`--seeds` 控制组数，默认 3）。同一份数据重跑逐位相同。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poker_ocr import board_range, field_ranges, hand_vs_ranges, range_flow, ranges  # noqa: E402
from poker_ocr.coach import parse_cards, read_hand  # noqa: E402
from tools.replay_live_decisions import build_snap, find_state  # noqa: E402

SEED_TAG = "cold-call-bucket-v1"
DEFAULT_SAMPLES = 20000
DEFAULT_SEEDS = 3
DEMOTE_LINE = 0.35            # `poker_ocr/hand_strength.py:45` `_BEHIND_DEMOTE`
TIER_ORDER = ("强成牌", "超对", "顶对强踢", "顶对弱踢", "中底对",
              "强听", "弱听", "空气", "打牌面")

# ───────────────────────── 三档草案 ─────────────────────────
#
# 形式：token 列表（`preflop_ranges.expand` 语法），逐 token 展开成 169 键 →
# combo 权重全 1。三档是**嵌套**的：tier3 ⊂ tier2、tier4 ⊂ tier3，只删不加。
# 每一档都写明「删掉了什么类别、为什么」，见
# `docs/冷跟先验-按人数分桶-草案.md` §3。

UNCHANGED = None              # tier2 = 现状，不重写

DRAFT: dict[tuple[str, int], tuple[str, ...] | None] = {
    # 2 人池：只开池者 + 这一个跟注者。保持现状（票 #78 验收）。
    ("IP", 2): UNCHANGED,
    ("OOP", 2): UNCHANGED,
    # 3 人池：开池者 + 已有 1 家进池。删掉被统治的弱同花宽张
    # （K7s–K9s / Q7s–Q9s / J7s–J8s / T7s–T8s）与较弱的两张非同花
    # （QJo / JTo / K9o 系列），保留小对子、同花 A、同花连张/一隔张。
    ("IP", 3): (
        "22", "33", "44", "55", "66", "77", "88", "99", "TT", "JJ",
        "A2s-AQs", "KTs", "KJs", "KQs", "QTs", "QJs", "JTs",
        "T9s", "98s", "87s", "76s", "65s", "54s",
        "97s", "86s", "75s",
        "KQo", "KJo", "T9o",
    ),
    ("OOP", 3): (
        "22", "33", "44", "55", "66", "77", "88", "99", "TT",
        "A2s-AQs", "KTs", "KJs", "KQs", "QTs", "QJs", "JTs", "T9s",
        "98s", "87s", "76s", "65s", "54s",
        "KQo", "KJo",
    ),
    # 4+ 人池：开池者 + 已有 ≥2 家进池。再删一隔张（97s/86s/75s）与
    # 最弱的两张非同花（KJo/T9o），对子收到 TT。
    ("IP", 4): (
        "22", "33", "44", "55", "66", "77", "88", "99", "TT",
        "A2s-AQs", "KTs", "KJs", "KQs", "JTs",
        "T9s", "98s", "87s", "76s", "65s", "54s",
        "KQo",
    ),
    # OOP 再收紧一档的地方是「被统治的弱 A 同花」（A6s–A9s：踢脚不够、
    # 中顶对就输给更大的 A），只留 A2s–A5s（顺子潜力）与 ATs+。
    ("OOP", 4): (
        "22", "33", "44", "55", "66", "77", "88", "99",
        "A2s", "A3s", "A4s", "A5s", "ATs", "AJs", "AQs",
        "KQs", "KJs", "QJs", "QTs", "JTs",
        "98s", "87s", "76s", "65s", "54s",
        "KQo",
    ),
}


def build_draft_range(ip_oop: str, tier: int) -> ranges.Range:
    """把草案 token 列表变成 `ranges.Range`（等权 combo）。"""
    tokens = DRAFT[(ip_oop, tier)]
    if tokens is None:
        raise ValueError("tier2 是现状，不从这里构造")
    keys = ranges.expand_tokens(tokens)
    weights = {}
    for key in sorted(keys):
        for combo in ranges.combos_of_key(key):
            weights[combo] = 1.0
    built = ranges.make_range(
        f"COLD_CALL_{ip_oop}_n{tier}", weights,
        source="draft:cold-call-by-players",
        note=f"draft tier {tier} (players in pot); see 冷跟先验-按人数分桶-草案")
    assert built is not None
    return built


_DRAFT_CACHE: dict[tuple[str, int], ranges.Range] = {}


def draft_range(ip_oop: str, tier: int) -> ranges.Range:
    key = (ip_oop, tier if tier >= 3 else 2)
    if key not in _DRAFT_CACHE:
        if DRAFT[(ip_oop, key[1])] is None:      # 理论上不会进：tier2 走现状
            cur = ranges.get(f"COLD_CALL_{ip_oop}_vs_LP")
            assert cur is not None
            _DRAFT_CACHE[key] = cur
        else:
            _DRAFT_CACHE[key] = build_draft_range(ip_oop, key[1])
    return _DRAFT_CACHE[key]


# ───────────────────────── 决策点采集 ─────────────────────────

@dataclass
class Point:
    source: str
    hand: str
    street: str
    hero: str
    board: str
    tier: str
    players: int
    cold_seats: tuple[str, ...]
    seat_tier: dict[str, int]          # seat_id → 该座位跟注时的入池人数档（2/3/4）
    max_tier: int
    order_fallback: bool
    snap: object = field(repr=False, default=None)
    base: dict = field(default_factory=dict)      # seed → (behind, equity)
    bucket: dict = field(default_factory=dict)
    delta: list = field(default_factory=list)     # 每个 seed 的 (behind, equity) 差
    arm_meta: dict = field(default_factory=dict)  # arm → (建模家数, 随机家数)


def data_sources() -> list[Path]:
    paths = [ROOT / "logs" / "live.mac.jsonl"]
    paths += sorted((ROOT / "runs").glob("*/live.states.jsonl"))
    return [p for p in paths if p.is_file()]


def load_records(path: Path) -> tuple[list[dict], int]:
    records: list[dict] = []
    bad = 0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
    except OSError:
        return [], bad
    return records, bad


def _preflop_calls(snap) -> list[str]:
    """翻前按顺序的「进池」动作座位（call / all-in-call），hero 用 __hero__。"""
    seats: list[str] = []
    for rec in snap.streets:
        if rec.street != "preflop":
            continue
        for action in rec.actions:
            if action.action != "call":
                continue
            seats.append("__hero__" if action.is_hero else str(action.seat_id))
    return seats


def _seat_tiers(snap, cold_seats: list[str]) -> tuple[dict[str, int], bool]:
    """每个冷跟座位跟注时的入池人数 → 档位 2/3/4（上限 4）。

    `n_way = 1（开池者）+ 它之前（含自己）所有进池的跟注/溜入`。
    拿不到动作顺序时退回「当前在局人数」，并标 fallback。
    """
    calls = _preflop_calls(snap)
    if not calls:
        active = sum(1 for o in snap.opponents if not o.folded) + 1
        return {seat: min(4, max(2, active)) for seat in cold_seats}, True
    out: dict[str, int] = {}
    fallback = False
    for seat in cold_seats:
        try:
            rank = calls.index(seat) + 1
        except ValueError:
            active = sum(1 for o in snap.opponents if not o.folded) + 1
            out[seat] = min(4, max(2, active))
            fallback = True
            continue
        out[seat] = min(4, max(2, 1 + rank))
    return out, fallback


def collect(limit: int = 0) -> tuple[list[Point], dict]:
    points: list[Point] = []
    seen: set[tuple] = set()
    stats: dict = {"files": [], "skipped": {}, "duplicates": 0,
                   "advices": 0, "records": 0, "bad_lines": 0,
                   "cold_points": 0}

    def skip(reason: str) -> None:
        stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1

    for path in data_sources():
        if limit and len(points) >= limit:
            break
        rel = str(path.relative_to(ROOT))
        records, bad = load_records(path)
        stats["records"] += 1
        stats["bad_lines"] += bad
        states = [r for r in records
                  if r.get("type") == "state" and r.get("hero")]
        advices = [r for r in records if r.get("type") == "advice"]
        hands = {str(h.get("hand_id")): h for h in records
                 if h.get("type") == "hand"}
        stats["advices"] += len(advices)
        used = 0
        since = ""
        for adv in advices:
            if limit and len(points) >= limit:
                break
            state = find_state(adv, states, since)
            since = adv.get("ts") or ""
            if state is None:
                skip("no_frame")
                continue
            hand = hands.get(str(adv.get("hand_id")))
            try:
                snap = build_snap(adv, state, hand)
            except Exception as exc:                     # noqa: BLE001
                skip(f"snap_error: {type(exc).__name__}")
                continue
            if len(snap.board) < 3:
                skip(f"preflop_or_short_board: {snap.street}")
                continue
            hero = " ".join(snap.hero_cards)
            board = " ".join(snap.board)
            key = (str(snap.hand_id), snap.street, hero, board)
            if key in seen:
                stats["duplicates"] += 1
                skip("duplicate_point")
                continue
            seen.add(key)
            try:
                field = field_ranges.resolve(snap)
            except Exception as exc:                     # noqa: BLE001
                skip(f"field_error: {type(exc).__name__}")
                continue
            cold_seats = [s.seat_id for s in field.specs
                          if s.role == "cold_call" and s.prior_id]
            if not cold_seats:
                skip("no_cold_call")
                continue
            try:
                read = read_hand(parse_cards(hero), parse_cards(board))
            except Exception as exc:                     # noqa: BLE001
                skip(f"read_hand_error: {type(exc).__name__}")
                continue
            seat_tier, fallback = _seat_tiers(snap, cold_seats)
            points.append(Point(
                source=rel, hand=str(snap.hand_id), street=snap.street,
                hero=hero, board=board, tier=getattr(read, "tier", "") or "",
                players=sum(1 for o in snap.opponents if not o.folded) + 1,
                cold_seats=tuple(cold_seats), seat_tier=seat_tier,
                max_tier=max(seat_tier.values()), order_fallback=fallback,
                snap=snap))
            used += 1
        stats["files"].append({"path": rel, "advices": len(advices),
                               "used": used, "cold_points": used})
        print(f"  {rel}: advice={len(advices)} 冷跟点={used}", file=sys.stderr)
    stats["cold_points"] = len(points)
    return points, stats


# ───────────────────────── 统一口径测量 ─────────────────────────

def _built_ranges(point: Point, arm: str) -> tuple[list, int]:
    """按生产 `_lookup` 的同一路径（先验 → 事件收窄 → 失败退回 board_range）。"""
    snap = point.snap
    hero_cards = [parse_cards(c)[0] for c in point.hero.split()]
    board_cards = [parse_cards(c)[0] for c in point.board.split()]
    dead = set(hero_cards) | set(board_cards)
    board_text = "".join(point.board.split())
    use_events = range_flow.enabled()

    built_list = []
    for spec in field_ranges.resolve(snap).specs:
        if not spec.prior_id:
            continue
        prior = None
        if spec.role == "cold_call" and arm == "bucket":
            tier = point.seat_tier.get(spec.seat_id, 2)
            ip_oop = "IP" if "_IP_" in spec.prior_id.upper() else "OOP"
            prior = (ranges.get(spec.prior_id) if tier <= 2
                     else draft_range(ip_oop, tier))
        else:
            prior = ranges.get(spec.prior_id)
        if prior is None:
            continue
        built = None
        if use_events and spec.events:
            built = range_flow.build_range(prior, spec.events,
                                           archetype="unknown", dead=dead)
        if built is None:
            built = board_range.build(board_text, spec.mode, prior)
        if built is None:
            continue
        built_list.append(built)
    active = [o for o in snap.opponents if not o.folded]
    random_opponents = max(len(active) - len(built_list), 0)
    return built_list, random_opponents


def measure(points: list[Point], samples: int, seeds: int) -> None:
    for point in points:
        hero_cards = [parse_cards(c)[0] for c in point.hero.split()]
        board_cards = [parse_cards(c)[0] for c in point.board.split()]
        arms = {arm: _built_ranges(point, arm) for arm in ("base", "bucket")}
        point.arm_meta = {arm: (len(v[0]), v[1]) for arm, v in arms.items()}
        for k in range(seeds):
            seed = zlib.crc32("|".join([
                SEED_TAG, point.hand, point.street, point.hero, point.board,
                str(k)]).encode("utf-8"))
            got = {}
            for arm in ("base", "bucket"):
                built_list, random_opponents = arms[arm]
                behind, _tie, equity, valid, _failed = (
                    hand_vs_ranges._monte_carlo(
                        list(hero_cards), list(board_cards), built_list,
                        int(samples), int(seed), int(random_opponents)))
                got[arm] = (behind * 100.0, equity * 100.0) if valid else None
            point.base[k] = got["base"]
            point.bucket[k] = got["bucket"]
            if got["base"] and got["bucket"]:
                point.delta.append((got["bucket"][0] - got["base"][0],
                                    got["bucket"][1] - got["base"][1]))
        print(f"  {point.hand} {point.street} 档={point.max_tier}",
              file=sys.stderr)


def _mean(values):
    vals = [v for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


def summarize(deltas: list[float]) -> dict:
    vals = [float(v) for v in deltas if v is not None]
    if not vals:
        return {"n": 0}
    worst = max(vals, key=abs)
    return {"n": len(vals), "mean": statistics.fmean(vals),
            "median": statistics.median(vals), "min": min(vals),
            "max": max(vals), "worst": worst,
            "n_pos": sum(1 for v in vals if v > 0),
            "n_neg": sum(1 for v in vals if v < 0),
            "n_zero": sum(1 for v in vals if v == 0),
            "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0}


def point_delta(point: Point, idx: int = 0) -> float | None:
    vals = [d[idx] for d in point.delta]
    return _mean(vals)


def draft_widths() -> list[dict]:
    rows = []
    for ip_oop in ("IP", "OOP"):
        cur = ranges.get(f"COLD_CALL_{ip_oop}_vs_LP")
        assert cur is not None
        rows.append({"arm": f"{ip_oop} tier2", "range": cur,
                     "tokens": None, "note": "现状（不重写）"})
        for tier in (3, 4):
            rows.append({"arm": f"{ip_oop} tier{tier}",
                         "range": draft_range(ip_oop, tier),
                         "tokens": DRAFT[(ip_oop, tier)], "note": "草案"})
    return rows


def tier_keys(ip_oop: str, tier: int) -> set[str]:
    if tier <= 2:
        return set(ranges.expand_tokens(ranges._RULE_TOKENS[f"COLD_CALL_{ip_oop}"]))
    return set(ranges.expand_tokens(DRAFT[(ip_oop, tier)]))


def grid_markdown(ip_oop: str) -> list[str]:
    """13×13 逐格可评审表：格内数字 = 该手牌能玩到的最大池子人数档。

    `4` 表示 2/3/4 人池都留（4+ 人池也留）；`2` 表示只有 2 人池留；
    `·` 表示连 2 人池都不在现状表里。上三角 = 同花、对角线 = 对子、
    下三角 = 非同花。
    """
    keys = {t: tier_keys(ip_oop, t) for t in (2, 3, 4)}
    ranks = "AKQJT98765432"
    header = ["| |" + "|".join(ranks) + "|", "|---|" + "---|" * len(ranks)]
    lines = [f"**{ip_oop}**", "", *header]
    for i, hi in enumerate(ranks):
        row = [f"| **{hi}** "]
        for j, lo in enumerate(ranks):
            if i == j:
                key = hi + lo
            elif i < j:
                key = hi + lo + "s"
            else:
                key = lo + hi + "o"
            mark = "·"
            for tier in (2, 3, 4):
                if key in keys[tier]:
                    mark = str(tier)
            row.append(f"|{mark}")
        lines.append("".join(row) + "|")
    w = {t: len(keys[t]) for t in keys}
    lines.append("")
    lines.append(f"169 键计数：tier2 {w[2]}、tier3 {w[3]}、tier4 {w[4]}；"
                 f"嵌套校验 tier3⊂tier2={keys[3] <= keys[2]}、"
                 f"tier4⊂tier3={keys[4] <= keys[3]}。")
    lines.append("")
    return lines


def tier_table_markdown() -> list[str]:
    lines = ["| 口径 | 档 | combo 数 | 宽度 | 形式 | 相对现状 |",
             "|---|---|---|---|---|---|"]
    base = {}
    for ip_oop in ("IP", "OOP"):
        r = ranges.get(f"COLD_CALL_{ip_oop}_vs_LP")
        assert r is not None
        base[ip_oop] = r
        lines.append(f"| `COLD_CALL_{ip_oop}` | 2 人池 | {r.combo_count} | "
                     f"{r.width_pct:.2f}% | token 列表（`ranges.py:"
                     f"{'262' if ip_oop == 'IP' else '269'}`） | 基线 |")
        for tier in (3, 4):
            d = draft_range(ip_oop, tier)
            keep = d.combo_count / r.combo_count * 100
            lines.append(f"| `COLD_CALL_{ip_oop}_n{tier}` | {tier} 人池 | "
                         f"{d.combo_count} | {d.width_pct:.2f}% | "
                         f"token 列表（嵌套子集） | 保留 {keep:.0f}% 的 combo、"
                         f"−{r.width_pct - d.width_pct:.2f}pp |")
    return lines


# ───────────────────────── 输出 ─────────────────────────

def pp(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}"


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument("--seeds", type=int, default=DEFAULT_SEEDS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", type=str, default="")
    ap.add_argument("--markdown", type=str, default="")
    ap.add_argument("--draft-table", action="store_true")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--list-points", action="store_true")
    ap.add_argument("--diagnostics", action="store_true")
    args = ap.parse_args(argv)

    if args.draft_table:
        for line in tier_table_markdown():
            print(line)
        if args.grid:
            print()
            for ip_oop in ("IP", "OOP"):
                for line in grid_markdown(ip_oop):
                    print(line)
        return 0

    points, stats = collect(args.limit)
    if not points:
        print("没有可用决策点", file=sys.stderr)
        return 1
    print(f"采集 {len(points)} 个冷跟决策点，开始测量（{args.samples} 样本 × "
          f"{args.seeds} 组种子）", file=sys.stderr)
    measure(points, args.samples, args.seeds)

    rows = []
    for point in points:
        rows.append({
            "source": point.source, "hand": point.hand,
            "street": point.street, "hero": point.hero, "board": point.board,
            "tier": point.tier, "players": point.players,
            "cold_seats": list(point.cold_seats),
            "seat_tier": point.seat_tier, "max_tier": point.max_tier,
            "order_fallback": point.order_fallback,
            "arm_meta": point.arm_meta,
            "base_behind_seeds": [v[0] if v else None
                                  for v in point.base.values()],
            "bucket_behind_seeds": [v[0] if v else None
                                    for v in point.bucket.values()],
            "delta_behind_seeds": [d[0] for d in point.delta],
            "delta_equity_seeds": [d[1] for d in point.delta],
            "base_behind": _mean([v[0] for v in point.base.values()
                                  if v is not None]),
            "bucket_behind": _mean([v[0] for v in point.bucket.values()
                                    if v is not None]),
            "base_equity": _mean([v[1] for v in point.base.values()
                                  if v is not None]),
            "bucket_equity": _mean([v[1] for v in point.bucket.values()
                                    if v is not None]),
            "delta_behind": point_delta(point, 0),
            "delta_equity": point_delta(point, 1),
        })

    noise = 1.96 * math.sqrt(0.25 / max(args.samples, 1)) * 100.0

    lines: list[str] = []
    add = lines.append
    add(f"# 冷跟先验按人数分桶：影响量（自动生成，{len(points)} 点）")
    add("")
    add(f"- 样本：{len(points)} 个翻牌后决策点，每个至少一家冷跟；"
        f"`--samples {args.samples}`、`--seeds {args.seeds}`。")
    add(f"- 单点 MC 95% 半宽 ≈ ±{noise:.2f}pp；两臂共享随机种子（配对差）。")
    add("")
    add("## 一、三档草案宽度")
    add("")
    lines.extend(tier_table_markdown())
    add("")
    add("## 二、影响量：现状单桶 vs 按人数分桶")
    add("")
    add("差值 = 分桶 − 现状，单位 pp。正 = 分桶后「至少一家领先我」更高（更保守）。")
    add("")
    add("| 分组 | 点 | behind_any 均值差 | 中位 | 最小 | 最大 | 最坏\\|差\\| | 升/降/平 |")
    add("|---|---|---|---|---|---|---|---|")

    def row(label: str, subset: list[Point]) -> None:
        s = summarize([point_delta(p, 0) for p in subset])
        if not s["n"]:
            return
        add(f"| {label} | {s['n']} | {pp(s['mean'])} | {pp(s['median'])} | "
            f"{pp(s['min'])} | {pp(s['max'])} | {pp(s['worst'])} | "
            f"{s['n_pos']}/{s['n_neg']}/{s['n_zero']} |")

    row("全部", points)
    for tier in (2, 3, 4):
        row(f"最大档 = {tier} 人池", [p for p in points if p.max_tier == tier])
    add("")

    add("### 按牌力档（`coach.read_hand().tier`）")
    add("")
    add("| 牌力档 | 点 | behind_any 均值差 | 中位 | 最坏\\|差\\| |")
    add("|---|---|---|---|---|")
    for tier in TIER_ORDER:
        subset = [p for p in points if p.tier == tier]
        s = summarize([point_delta(p, 0) for p in subset])
        if not s["n"]:
            continue
        add(f"| {tier} | {s['n']} | {pp(s['mean'])} | {pp(s['median'])} | "
            f"{pp(s['worst'])} |")
    add("")

    add("### equity 的同口径差")
    add("")
    s_eq = summarize([point_delta(p, 1) for p in points])
    add(f"- 全部 {s_eq['n']} 点：均值 {pp(s_eq['mean'])}pp、中位 "
        f"{pp(s_eq['median'])}pp、最坏 {pp(s_eq['worst'])}pp。")
    add("")

    crossed = [p for p in points
               if (p.base.get(0) or (0, 0))[0] < DEMOTE_LINE * 100
               <= (p.bucket.get(0) or (0, 0))[0]]
    add(f"### 判定线穿越（`_BEHIND_DEMOTE = {DEMOTE_LINE}`）")
    add("")
    add(f"- behind_any 从 < {DEMOTE_LINE * 100:.0f}% 升到 ≥ "
        f"{DEMOTE_LINE * 100:.0f}% 的点：{len(crossed)} 个"
        + ("" if not crossed else
           "（" + "、".join(f"`{p.hero}`/`{p.board}`" for p in crossed) + "）")
        + "。")
    add("")

    add("## 三、口径自检")
    add("")
    mismatch = [p for p in points
                if p.arm_meta.get("base") != p.arm_meta.get("bucket")]
    add("- 两臂的（建模家数, 随机家数）逐点一致："
        + ("是" if not mismatch
           else f"否，{len(mismatch)} 点不一致：" +
                "、".join(f"`{p.hero}`/`{p.board}`" for p in mismatch)))
    same = [p for p in points if p.max_tier == 2]
    zeros = [p for p in same
             if all(abs(d[0]) < 1e-9 and abs(d[1]) < 1e-9 for d in p.delta)]
    add(f"- 最大档 = 2 的 {len(same)} 点，两臂先验逐位相同，差值恒为 0："
        f"{'是' if len(zeros) == len(same) else '否'}"
        "（这是「同一 seed → 同一输出」的确定性自检，不是噪声标尺）。")
    spreads = [max(d[0] for d in p.delta) - min(d[0] for d in p.delta)
               for p in points if len(p.delta) > 1]
    if spreads:
        add(f"- 组内（{args.seeds} 组种子）差值的极差：中位 "
            f"{statistics.median(spreads):.2f}pp、最大 {max(spreads):.2f}pp"
            "——组间抖动就是单点 MC 噪声，引用均值而不是单点。")
    add("")
    if args.diagnostics:
        add("| 手牌 | 街 | 冷跟座位档 | 在局人数 | 建模/随机（两臂） |")
        add("|---|---|---|---|---|")
        for r in rows:
            add(f"| `{r['hero']}` | {r['street']} | "
                f"{'/'.join(str(v) for v in r['seat_tier'].values())} | "
                f"{r['players']} | {r['arm_meta']} |")
        add("")

    if args.list_points:
        add("## 四、逐点明细")
        add("")
        add("| 手牌 | 街 | 牌面 | 牌力档 | 在局人数 | 冷跟座位档 | behind 现状 | "
            "behind 分桶 | 差 |")
        add("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            add(f"| `{r['hero']}` | {r['street']} | `{r['board']}` | "
                f"{r['tier']} | {r['players']} | "
                f"{'/'.join(str(v) for v in r['seat_tier'].values())} | "
                f"{_fmt(r['base_behind'])} | {_fmt(r['bucket_behind'])} | "
                f"{pp(r['delta_behind'])} |")
        add("")

    text = "\n".join(lines) + "\n"
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
        print(f"写入 {args.markdown}", file=sys.stderr)
    else:
        print(text)
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"samples": args.samples, "seeds": args.seeds, "noise_pp": noise,
             "stats": stats, "rows": rows}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"写入 {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
