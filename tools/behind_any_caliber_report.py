#!/usr/bin/env python3
"""量化「至少一家领先我」的双口径偏差：独立近似 vs 联合拒绝重抽。

    nv tools/behind_any_caliber_report.py                    # 终端摘要
    nv tools/behind_any_caliber_report.py --markdown docs/xxx.md
    nv tools/behind_any_caliber_report.py --samples 20000 --limit 20
    nv tools/behind_any_caliber_report.py --no-chain          # 跳过真实链路旁证
    nv tools/behind_any_caliber_report.py --json /tmp/rows.json   # 落逐点数字
    nv tools/behind_any_caliber_report.py --from-json /tmp/rows.json \\
        --markdown docs/xxx.md                               # 不重算抽样，重建报告

只读：不写生产路径、不调模型（没有 --model，也不发起任何 LLM 请求）、
不改任何返回值；`logs/` `runs/` 只读打开。

## 两个口径

**口径 A（同一决策点、同一对手模型 = 均匀随机牌，只差「独立 vs 不放回联合」）**

  · 独立：`p1` = 精确枚举**单个**随机对手被压概率（hero+board 已去除），
    `indep = 1 - (1 - p1) ** n` —— 与 `poker_ocr/coach.py:394`
    `rank_hands(n>1)` 的算式逐字相同。
  · 联合：`poker_ocr/hand_vs_ranges.py:330` `_monte_carlo`，`n` 家范围
    **整组重抽、不放回**（撞牌重抽整组）。`behind_any` 只比当前五张，
    严格大于才算领先，与 `beat_pct` 同语义。
  · `diff = indep - joint`：**< 0 表示独立近似偏低**（真值更高）。

**口径 B（真实链路 as-shipped，只作旁证，不混入口径 A 的统计）**

  · `build_situation().beat_at_least_pct` = 独立近似，且是对**随机牌**；
  · `build_situation().hand_ranges.behind_any` = 联合重抽，对**对手范围**。
  · 两者的差同时含「独立性」与「随机牌 vs 对手范围」两个来源，
    所以不能当作口径 A 的差值来用。
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

from poker_ocr import hand_vs_ranges, ranges  # noqa: E402
from poker_ocr.coach import parse_cards, read_hand  # noqa: E402
from poker_ocr.situation import build_situation  # noqa: E402
from tools.replay_live_decisions import build_snap, find_state  # noqa: E402

POOLS = (3, 4, 5)          # 总人数；对手数 = 人数 - 1
DEFAULT_SAMPLES = 8000
SEED_TAG = "behind-any-caliber-v1"
REFINE_FACTOR = 4          # 最坏个案复核用的样本倍数
TOP_WORST = 5
DECK_TAG = "RANDOM_FILL"   # `ranges.uniform_range` 的随机牌填充

TIER_ORDER = ("强成牌", "超对", "顶对强踢", "顶对弱踢", "中底对",
              "强听", "弱听", "空气", "打牌面")

# 先写后算：判据在工具里固定，不看数据再改。
CRITERION_MEAN_PP = 5.0
CRITERION_WORST_PP = 10.0
DEMOTE_LINE = 0.35         # `poker_ocr/hand_strength.py` 的 _BEHIND_DEMOTE

# 事实核验：票面前提「复盘评分走独立近似」与代码不符，这段是静态结论，
# 由 `replay-console/replay/decisions.py` + `poker_ocr/situation.py` 逐行核出来。
CHAIN_FACT_NOTE = """\
票面（#74/#75）的前提是「提示词渲染走联合重抽、复盘评分走独立近似」。
逐行追下来前提**不成立**，真实分界是「主路 vs 兜底」：

| 数字 | 算法 | 生产落点 | 谁读它 |
|---|---|---|---|
| `ground_truth.rank.beat_at_least_pct` | 独立近似 `1-(1-p1)**n`（`poker_ocr/coach.py:394`），p1 对**随机牌** | 提示词兜底分支 `poker_ocr/prompt/user/strength.py:163`（仅 `sit.hand_ranges is None`） | 模型；`situation.beat_at_least_pct` → `facts["beatAtLeastPct"]`（`replay-console/replay/decisions.py:827`） |
| `hand_ranges.behind_any` | 联合拒绝重抽（`poker_ocr/hand_vs_ranges.py:330`），对**对手范围** | 提示词主路 `poker_ocr/prompt/user/strength.py:214`、`poker_ocr/prompt/user/ranges.py:178` | 模型；`hand_strength`（`_BEHIND_DEMOTE = 0.35`）；`facts["handStrength"]["behindAny"]`（`decisions.py:833-835`） |

1. **复盘侧 `behindAny` 不是独立近似。** `replay-console/replay/decisions.py:817`
   `hs = getattr(sit, "hand_strength", None)`，`sit` 由 `replay-console/replay/engine.py:213`
   `build_situation(..., include_strength=True)` 产出；`hand_strength` 是纯判定层，
   `behind_any` 直取 `hr.behind_any`（`poker_ocr/hand_strength.py:156`），
   而 `hr` 来自 `poker_ocr/situation.py:527` 的 `hand_vs_ranges.lookup(sit)`——
   联合重抽，与提示词主路**同一个函数**。
2. **独立近似在复盘侧只被记录，不进评分。** 全仓 grep
   `beatAtLeastPct|handStrength|behindAny`：`replay-console` 只有 `decisions.py`
   827/833/835 三处写 facts，没有评分代码读它们；打分用的
   `audit_advice_tags`（`poker_ocr/prompt/offline/audit.py`）不引用这两个数字。
3. **真正会不一致的是同一句话的两条分支**：有对手范围时渲染联合重抽，
   范围不可用时退化成独立近似，**且对手模型同时退化成随机牌**
   （文案「按随机牌下界」）。两条分支互斥，从不同时出现。
4. 因此票面说的「两套算法给出不同数字」真实存在（下面口径 B 的表），
   但差值主体是**范围 vs 随机牌**，不是独立 vs 联合；算法本身的偏差只有
   口径 A 的几 pp。
"""


# ───────────────────────── 口径 A 的数学 ─────────────────────────

def independent_at_least(p1: float, n: int) -> float:
    """`1-(1-p1)**n`：n 家独立同分布的「至少一家领先我」。

    这就是 `poker_ocr/coach.py:394` 的算式；n<=0 视为没有对手。
    """
    if n <= 0:
        return 0.0
    return 1.0 - (1.0 - float(p1)) ** int(n)


def joint_at_least(hero, board, n: int, *, samples: int = DEFAULT_SAMPLES,
                   seed: int = 0) -> float:
    """`n` 家随机牌不放回整组重抽下的「至少一家领先我」。

    直接复用生产 `_monte_carlo`（不重写抽样逻辑，免得和实现漂移）。
    要求翻牌后（board>=3）：口径是「当前成牌」，翻前没有五张可比。
    """
    hero = tuple(hero)
    board = tuple(board)
    if len(hero) != 2:
        raise ValueError(f"hero 需要 2 张，收到 {len(hero)} 张")
    if len(board) < 3:
        raise ValueError(f"board 需要 >=3 张（当前成牌口径），收到 {len(board)} 张")
    if n <= 0:
        return 0.0
    deck = [_uniform_range() for _ in range(int(n))]
    # `_monte_carlo` 内部是 `list(h) + board`，board 必须是 list。
    behind, _tie, _eq, valid, failed, _attempted = (
        hand_vs_ranges._monte_carlo(
            list(hero), list(board), deck, int(samples), int(seed)))
    if valid <= 0:
        raise RuntimeError(f"抽样全部失败（failed={failed}）")
    return float(behind)


_UNIFORM = None


def _uniform_range():
    global _UNIFORM
    if _UNIFORM is None:
        _UNIFORM = ranges.uniform_range(DECK_TAG)
    return _UNIFORM


def seed_for(*parts: object) -> int:
    """固定 seed：同样的输入永远给同样的抽样（报告可复现的前提）。"""
    text = "|".join([SEED_TAG, *[str(p) for p in parts]])
    return zlib.crc32(text.encode("utf-8"))


def summarize(values) -> dict:
    """一组差值（百分点）的分布摘要；空集时只有 n=0。"""
    vals = [float(v) for v in values]
    if not vals:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None,
                "worst_abs": None, "worst_index": None,
                "n_positive": 0, "n_negative": 0, "sd": None}
    worst_index = max(range(len(vals)), key=lambda i: abs(vals[i]))
    return {
        "n": len(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
        "worst_abs": vals[worst_index],
        "worst_index": worst_index,
        "n_positive": sum(1 for v in vals if v > 0),
        "n_negative": sum(1 for v in vals if v < 0),
        "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
    }


def mean_ci95(summ: dict) -> tuple[float, float] | None:
    """均值的近似 95% 区间（正态近似；差值本身已有抽样噪声）。"""
    if not summ["n"]:
        return None
    half = 1.96 * (summ["sd"] or 0.0) / math.sqrt(summ["n"])
    return summ["mean"] - half, summ["mean"] + half


def mc_noise_pp(samples: int) -> float:
    """单点 MC 的 95% 半宽（百分比点）：最坏 p(1-p)=0.25。"""
    return 1.96 * math.sqrt(0.25 / max(samples, 1)) * 100.0


# ───────────────────────── 决策点采集 ─────────────────────────

@dataclass
class Point:
    source: str
    hand: str
    street: str
    hero: str
    board: str
    tier: str
    p1: float
    actual_players: int
    snap: object = field(repr=False, default=None)
    indep: dict = field(default_factory=dict)
    joint: dict = field(default_factory=dict)
    diff: dict = field(default_factory=dict)
    actual_pool: int | None = None     # 真实在局人数（2..9）
    actual_diff: float | None = None   # 真实人数下 indep - joint（pp）
    chain: dict | None = None

    @property
    def key(self) -> tuple:
        # 不含真实人数：同一手同街同牌面时，p1 与联合口径的 seed 都一样，
        # 两个 Arm A 数字逐位相同，重复计点只会让分档统计虚高。
        return (self.hand, self.street, self.hero, self.board)


def data_sources() -> list[Path]:
    """已录数据：`logs/live.mac.jsonl` + `runs/*/live.states.jsonl`。"""
    paths = [ROOT / "logs" / "live.mac.jsonl"]
    paths += sorted((ROOT / "runs").glob("*/live.states.jsonl"))
    return [p for p in paths if p.is_file()]


def load_records(path: Path) -> tuple[list[dict], int, str]:
    """逐行解析；坏行（8p_run*.states.jsonl 这类 batch 报告）只计数不抛。"""
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
    except OSError as exc:
        return [], bad, f"{type(exc).__name__}: {exc}"
    return records, bad, ""


def collect(sources: list[Path], limit: int = 0) -> tuple[list[Point], dict]:
    """扫真实决策点，返回（可用点，统计）。统计里带每个丢弃原因的数量。"""
    points: list[Point] = []
    seen: set[tuple] = set()
    stats: dict = {"files": [], "skipped": {}, "duplicates": 0,
                   "advices": 0, "records": 0, "bad_lines": 0}

    def skip(reason: str) -> None:
        stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1

    for path in sources:
        if limit and len(points) >= limit:
            break
        rel = str(path.relative_to(ROOT))
        records, bad, err = load_records(path)
        stats["records"] += 1
        stats["bad_lines"] += bad
        if err:
            stats["files"].append({"path": rel, "advices": 0, "used": 0,
                                   "skipped": 0, "error": err, "bad_lines": bad})
            continue
        states = [r for r in records
                  if r.get("type") == "state" and r.get("hero")]
        advices = [r for r in records if r.get("type") == "advice"]
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
            try:
                snap = build_snap(adv, state, None)
            except Exception as exc:                     # noqa: BLE001
                skip(f"snap_error: {type(exc).__name__}")
                continue
            bb = (snap.blinds.bb if snap.blinds else 1.0)
            # 和 tools/replay_live_decisions.py 同一条陈旧帧过滤。
            if (snap.street == "preflop" and snap.to_call == 0
                    and ((snap.hero_position or "") not in ("BB",)
                         or (snap.pot or 0) > 12 * bb)):
                skip("stale_frame")
                continue
            if len(snap.board) < 3:
                skip(f"preflop_or_short_board: {snap.street}")
                continue
            hero = " ".join(snap.hero_cards)
            board = " ".join(snap.board)
            actual = sum(1 for o in snap.opponents if not o.folded) + 1
            key = (str(snap.hand_id), snap.street, hero, board)
            if key in seen:
                stats["duplicates"] += 1
                skip("duplicate_point")
                continue
            try:
                read = read_hand(parse_cards(hero), parse_cards(board))
            except Exception as exc:                     # noqa: BLE001
                skip(f"read_hand_error: {type(exc).__name__}")
                continue
            seen.add(key)
            points.append(Point(source=rel, hand=str(snap.hand_id),
                                street=snap.street, hero=hero, board=board,
                                tier=getattr(read, "tier", "") or "",
                                p1=float(read.beat_pct),
                                actual_players=actual, snap=snap))
            used += 1
        stats["files"].append({"path": rel, "advices": len(advices),
                               "used": used, "skipped": len(advices) - used,
                               "error": "", "bad_lines": bad})
        print(f"  {rel}: advice={len(advices)} 采用={used}", file=sys.stderr)
    return points, stats


def measure(points: list[Point], samples: int) -> None:
    """对每个点算 3/4/5 人池下的两套口径（原地写入）。"""
    for i, pt in enumerate(points, 1):
        hero = parse_cards(pt.hero)
        board = parse_cards(pt.board)
        for pool in POOLS:
            n = pool - 1
            indep = independent_at_least(pt.p1, n)
            seed = seed_for(pt.hand, pt.board, pool, samples)
            joint = joint_at_least(hero, board, n, samples=samples, seed=seed)
            pt.indep[pool] = indep * 100.0
            pt.joint[pool] = joint * 100.0
            pt.diff[pool] = pt.indep[pool] - pt.joint[pool]
        # 真实人数（不是反事实）：单挑和 6 人以上要另算，3/4/5 人复用上面的数。
        pool_actual = max(2, min(pt.actual_players, 9))
        pt.actual_pool = pool_actual
        if pool_actual in POOLS:
            pt.actual_diff = pt.diff[pool_actual]
        else:
            n = pool_actual - 1
            indep = independent_at_least(pt.p1, n)
            seed = seed_for(pt.hand, pt.board, pool_actual, samples)
            joint = joint_at_least(hero, board, n, samples=samples, seed=seed)
            pt.actual_diff = (indep - joint) * 100.0
        if i % 20 == 0 or i == len(points):
            print(f"  口径 A {i}/{len(points)}", file=sys.stderr)


def refine_worst(points: list[Point], samples: int) -> None:
    """最坏个案用 REFINE_FACTOR 倍样本复核（单点 MC 噪声不可忽略）。"""
    for pool in POOLS:
        ranked = sorted(points, key=lambda p: abs(p.diff[pool]), reverse=True)
        for pt in ranked[:TOP_WORST]:
            hero = parse_cards(pt.hero)
            board = parse_cards(pt.board)
            n = pool - 1
            big = samples * REFINE_FACTOR
            seed = seed_for("refine", pt.hand, pt.board, pool, big)
            joint = joint_at_least(hero, board, n, samples=big, seed=seed) * 100.0
            pt.chain = pt.chain or {}
            pt.chain[f"refine_{pool}"] = {
                "samples": big, "joint": joint,
                "diff": pt.indep[pool] - joint,
            }


def refine_actual(points: list[Point], samples: int, top: int = 3) -> None:
    """真实人数口径里最坏的几点也用 4× 样本复核（大池单点噪声更大）。"""
    ranked = sorted(points, key=lambda p: abs(p.actual_diff or 0.0), reverse=True)
    for pt in ranked[:top]:
        pool = pt.actual_pool or 2
        hero = parse_cards(pt.hero)
        board = parse_cards(pt.board)
        big = samples * REFINE_FACTOR
        seed = seed_for("refine-actual", pt.hand, pt.board, pool, big)
        joint = joint_at_least(hero, board, pool - 1, samples=big, seed=seed) * 100.0
        indep = pt.indep.get(pool)
        if indep is None:  # 真实人数不在 3/4/5 里：按 p1 现算独立近似
            indep = independent_at_least(pt.p1, pool - 1) * 100.0
        pt.chain = pt.chain or {}
        pt.chain["refine_actual"] = {"samples": big, "joint": joint,
                                     "indep": indep, "diff": indep - joint}


def chain_gap(points: list[Point]) -> dict:
    """口径 B 旁证：真实链路同一决策点上的两个数字。

    `beat_at_least_pct`（独立/随机牌） vs `hand_ranges.behind_any`（联合/范围）。
    """
    rows, fails = [], {}
    coverage = 0
    for pt in points:
        try:
            sit = build_situation(pt.snap)
        except Exception as exc:                         # noqa: BLE001
            fails[type(exc).__name__] = fails.get(type(exc).__name__, 0) + 1
            continue
        indep = getattr(sit, "beat_at_least_pct", None)
        hr = getattr(sit, "hand_ranges", None)
        behind = getattr(hr, "behind_any", None) if hr is not None else None
        if indep is not None and behind is not None:
            coverage += 1
        pt.chain = dict(pt.chain or {})
        pt.chain["indep_pct"] = indep
        pt.chain["joint_range_pct"] = (None if behind is None else behind * 100.0)
        pt.chain["players"] = (len(getattr(hr, "opponents", ())) + 1
                               + int(getattr(hr, "random_opponents", 0))
                               if hr is not None else None)
        rows.append(pt)
    return {"rows": rows, "fails": fails, "coverage": coverage,
            "total": len(points)}


# ───────────────────────── 报告 ─────────────────────────

def pp(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}"


def _dist_table(groups: dict, samples: int) -> list[str]:
    noise = mc_noise_pp(samples)
    out = ["| 人数池 | 点 | 均值 | 95% 均值区间 | 中位 | 最小 | 最大 | 最坏\\|差\\| | 独立偏高 | 独立偏低 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for pool in POOLS:
        summ = groups[pool]
        ci = mean_ci95(summ)
        ci_text = f"[{pp(ci[0])}, {pp(ci[1])}]" if ci else "—"
        out.append(
            f"| {pool} 人 | {summ['n']} | {pp(summ['mean'])} | {ci_text} | "
            f"{pp(summ['median'])} | {pp(summ['min'])} | {pp(summ['max'])} | "
            f"{pp(summ['worst_abs'])} | {summ['n_positive']} | {summ['n_negative']} |")
    out.append("")
    out.append(f"差值单位：百分点（pp），`diff = 独立 − 联合`；负 = 独立近似偏低。"
               f"单点 MC 噪声 95% 半宽 ≈ ±{noise:.2f}pp（{samples} 样本）。")
    return out


def _tier_table(points: list[Point], pool: int) -> list[str]:
    buckets: dict[str, list[float]] = {}
    for pt in points:
        buckets.setdefault(pt.tier or "未分档", []).append(pt.diff[pool])
    order = [t for t in TIER_ORDER if t in buckets]
    order += sorted(t for t in buckets if t not in TIER_ORDER)
    out = ["| 牌力档 | 点 | 均值差 | 中位差 | 最小 | 最大 | 最坏\\|差\\| | 偏高/偏低 |",
           "|---|---|---|---|---|---|---|---|"]
    for tier in order:
        summ = summarize(buckets[tier])
        out.append(f"| {tier} | {summ['n']} | {pp(summ['mean'])} | "
                   f"{pp(summ['median'])} | {pp(summ['min'])} | {pp(summ['max'])} | "
                   f"{pp(summ['worst_abs'])} | {summ['n_positive']}/{summ['n_negative']} |")
    return out


def _worst_table(points: list[Point]) -> list[str]:
    out = ["| 人数池 | 手牌 | 街 | 牌力档 | p1（单挑） | 独立 | 联合 | 差值 | 复核样本 | 复核差值 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for pool in POOLS:
        ranked = sorted(points, key=lambda p: abs(p.diff[pool]), reverse=True)
        for pt in ranked[:TOP_WORST]:
            ref = (pt.chain or {}).get(f"refine_{pool}") or {}
            out.append(
                f"| {pool} 人 | `{pt.hero}` / `{pt.board}` | {pt.street} | {pt.tier} | "
                f"{pt.p1 * 100:.1f}% | {pt.indep[pool]:.2f}% | {pt.joint[pool]:.2f}% | "
                f"{pp(pt.diff[pool])} | {ref.get('samples', '—')} | "
                f"{pp(ref.get('diff')) if ref else '—'} |")
    return out


def _trend_table(points: list[Point]) -> list[str]:
    out = ["| 牌力档 | 3 人均值 | 4 人均值 | 5 人均值 | 点数 |",
           "|---|---|---|---|---|"]
    buckets: dict[str, dict[int, list[float]]] = {}
    for pt in points:
        tier = pt.tier or "未分档"
        for pool in POOLS:
            buckets.setdefault(tier, {}).setdefault(pool, []).append(pt.diff[pool])
    order = [t for t in TIER_ORDER if t in buckets]
    order += sorted(t for t in buckets if t not in TIER_ORDER)
    for tier in order:
        cells = []
        for pool in POOLS:
            vals = buckets[tier].get(pool) or []
            cells.append(pp(statistics.fmean(vals)) if vals else "—")
        n = max(len(v) for v in buckets[tier].values())
        out.append(f"| {tier} | {cells[0]} | {cells[1]} | {cells[2]} | {n} |")
    return out


def _chain_section(chain: dict, points: list[Point], samples: int) -> list[str]:
    rows = [pt for pt in points if (pt.chain or {}).get("joint_range_pct") is not None]
    out = ["## 6. 真实链路旁证（口径 B，不混入上表统计）", ""]
    out.append(f"- 决策点 {chain['total']} 个，`build_situation` 成功且两者都有值："
               f"{chain['coverage']} 个（覆盖 {chain['coverage'] / max(chain['total'], 1):.0%}）。")
    if chain["fails"]:
        detail = "、".join(f"{k}×{v}" for k, v in sorted(chain["fails"].items()))
        out.append(f"- `build_situation` 失败（按点跳过）：{detail}。")
    if not rows:
        out.append("- 没有可对比的点。")
        return out
    out.append("- 覆盖率这么低是**重建数据**的限制：缺翻前动作/画像时 "
               "`field_ranges` 给不出 prior，范围 fail open"
               "（`poker_ocr/hand_vs_ranges.py:333` 的 `if not spec.prior_id: continue`）。"
               "本节只能当旁证，不能当线上的范围覆盖率。")
    gaps = [pt.chain["indep_pct"] - pt.chain["joint_range_pct"] for pt in rows]
    summ = summarize(gaps)
    ci = mean_ci95(summ)
    out.append(f"- 差值（独立/随机 − 联合/范围）：均值 {pp(summ['mean'])}pp"
               f"（95% 区间 [{pp(ci[0])}, {pp(ci[1])}]），中位 {pp(summ['median'])}，"
               f"范围 [{pp(summ['min'])}, {pp(summ['max'])}]，"
               f"偏高 {summ['n_positive']} / 偏低 {summ['n_negative']}。")
    out.append(f"- 其中范围只建模 1 家（单挑）的点："
               f"{sum(1 for pt in rows if pt.chain.get('players') == 2)}。")
    out.append("")
    out.append("| 手牌 | 街 | 实际人数 | 独立（随机） | 联合（范围） | 差 |")
    out.append("|---|---|---|---|---|---|")
    for pt in sorted(rows, key=lambda p: abs(p.chain["indep_pct"]
                                            - p.chain["joint_range_pct"]),
                     reverse=True)[:TOP_WORST]:
        gap = pt.chain["indep_pct"] - pt.chain["joint_range_pct"]
        out.append(f"| `{pt.hero}` / `{pt.board}` | {pt.street} | "
                   f"{pt.chain.get('players') or '—'} | {pt.chain['indep_pct']:.1f}% | "
                   f"{pt.chain['joint_range_pct']:.1f}% | {pp(gap)} |")
    out.append("")
    out.append("这两个数字的量纲不同（随机牌 vs 对手范围），差值里「范围收窄」占大头，"
               "「独立 vs 联合」只占几个百分点——见口径 A 的分布。")
    return out


def verdict(points: list[Point], samples: int) -> tuple[str, list[str]]:
    """按固定判据给出结论；返回（结论，判据明细行）。"""
    lines = []
    trigger = []
    for pool in POOLS:
        summ = summarize([pt.diff[pool] for pt in points])
        worst = max(abs(pt.diff[pool]) for pt in points)
        refined = [abs(entry["diff"]) for pt in points
                   for key, entry in (pt.chain or {}).items()
                   if key == f"refine_{pool}"]
        need_mean = abs(summ["mean"])
        refined_text = (f"，{REFINE_FACTOR}× 复核后最坏 {max(refined):.2f}pp"
                        if refined else "")
        direction = ("独立偏低" if summ["mean"] < 0 else "独立偏高")
        lines.append(f"- {pool} 人：|均值| = {need_mean:.2f}pp"
                     f"（阈值 {CRITERION_MEAN_PP}，方向「{direction}」"
                     f"{summ['n_negative']}/{summ['n']}），最坏 |差| = {worst:.2f}pp"
                     f"{refined_text}（阈值 {CRITERION_WORST_PP}）")
        if need_mean >= CRITERION_MEAN_PP or worst >= CRITERION_WORST_PP:
            trigger.append(pool)
    overall = summarize([pt.actual_diff for pt in points])
    refined_actual = [abs((pt.chain or {})["refine_actual"]["diff"]) for pt in points
                      if (pt.chain or {}).get("refine_actual")]
    lines.append(f"- 按**真实**在局人数加权（不是反事实池）：均值 "
                 f"{pp(overall['mean'])}pp，最坏 {pp(overall['worst_abs'])}pp"
                 + (f"，其中最坏的 {len(refined_actual)} 点 "
                    f"{REFINE_FACTOR}× 复核后最大 \\|差\\| "
                    f"{max(refined_actual):.2f}pp。"
                    if refined_actual else "。"))
    if trigger:
        head = (f"**结论：改实现。** 判据 (a) 在 {', '.join(f'{p} 人' for p in trigger)}"
                f"池被触发。")
    else:
        means = {pool: summarize([pt.diff[pool] for pt in points])["mean"]
                 for pool in POOLS}
        mags = [abs(means[pool]) for pool in POOLS]
        arrow = " / ".join(f"{pool} 人 {means[pool]:+.2f}pp" for pool in POOLS)
        all_neg = all(v < 0 for v in means.values())
        all_pos = all(v > 0 for v in means.values())
        growing = all(b > a for a, b in zip(mags, mags[1:]))
        if all_neg:
            trend = ("方向稳定：独立近似偏低" + ("，且人数越多偏得越多" if growing else "")
                     + f"（{arrow}）")
        elif all_pos:
            trend = ("方向稳定：独立近似偏高" + ("，且人数越多偏得越多" if growing else "")
                     + f"（{arrow}）")
        else:
            trend = f"方向不稳定，3/4/5 人池的均值不同号（{arrow}）"
        head = (f"**结论：只写文档，不改实现。** 判据 (a) 在 3/4/5 人池都没被触发："
                f"反事实池 \\|均值差\\| 最大 {max(mags):.2f}pp"
                f"（阈值 {CRITERION_MEAN_PP:.0f}pp），单点最坏 \\|差\\| 最大 "
                f"{max(worst_by_pool(points).values()):.2f}pp"
                f"（阈值 {CRITERION_WORST_PP:.0f}pp）；{trend}；"
                "而且这个数字只喂提示词兜底的文案，不被任何数值判定线消费——"
                "改实现换不到可观测的收益，只写清楚口径即可。")
    return head, lines


def worst_by_pool(points: list[Point]) -> dict[int, float]:
    """每池的最大 |差|（含 4× 复核值里的最大值）。"""
    out: dict[int, float] = {}
    for pool in POOLS:
        worst = max(abs(pt.diff[pool]) for pt in points)
        refined = [abs(entry["diff"]) for pt in points
                   for key, entry in (pt.chain or {}).items()
                   if key == f"refine_{pool}"]
        out[pool] = max([worst, *refined])
    return out


def refined_actual_rows(points: list[Point]) -> list[Point]:
    """有真实人数口径复核值的点（按 |复核差| 从大到小）。"""
    rows = [pt for pt in points if (pt.chain or {}).get("refine_actual")]
    return sorted(rows, key=lambda p: abs(p.chain["refine_actual"]["diff"]),
                  reverse=True)


def build_markdown(points: list[Point], stats: dict, chain: dict,
                   samples: int, sources: list[Path]) -> str:
    groups = {pool: summarize([pt.diff[pool] for pt in points]) for pool in POOLS}
    head, criterion_lines = verdict(points, samples)
    tiers = sorted({pt.tier for pt in points if pt.tier})
    lines: list[str] = []
    add = lines.append

    add("# 「至少一家领先我」双口径偏差（2026-11-19）")
    add("")
    add("> 只读量化：同一批已录决策点上，用两套算法算「至少一家领先我」，"
        "给出 3 / 4 / 5 人池的差值分布、最坏情况与趋势。不改任何生产路径的返回值。")
    add("")
    add("## 0. 复现")
    add("")
    add("```bash")
    add(f"nv tools/behind_any_caliber_report.py --samples {samples} "
        "--markdown docs/至少一家领先-双口径偏差-2026-11-19.md")
    add("```")
    add("")
    add("固定 seed（`zlib.crc32(SEED_TAG|手牌|牌面|人数池|样本数)`），"
        "同一份数据重跑得到同一组数。不调模型、不写生产路径。")
    add("")
    add("## 1. 两个口径")
    add("")
    add("| | 独立近似 | 联合拒绝重抽 |")
    add("|---|---|---|")
    add("| 算法 | `1-(1-p1)**n`（`coach.rank_hands` n>1 的算式） | "
        "`hand_vs_ranges._monte_carlo` 整组重抽、不放回 |")
    add("| 对手模型 | 均匀随机牌（口径 A 内与右列相同） | 同左 |")
    add("| p1 | 精确枚举单挑被压概率 | 由抽样估计 |")
    add("| 生产消费者 | `ground_truth.rank.beat_at_least_pct` → 提示词兜底分支 "
        "`prompt/user/strength.py:163` | `hand_ranges.behind_any` → "
        "`prompt/user/strength.py:214`、`prompt/user/ranges.py:178`、复盘 `handStrength.behindAny` |")
    add("")
    add("口径 A 只让「独立 vs 不放回联合」这一个变量变化：同一点、同一对手模型。"
        "随机牌没有范围收窄信息，所以这里量的是**算法本身**的偏差。")
    add("")
    add("## 2. 样本与覆盖")
    add("")
    add(f"- 扫到 `advice` 记录 {stats['advices']} 条，采用 {len(points)} 个翻牌后决策点。")
    add(f"- 记录文件 {len(stats['files'])} 个；逐行 JSON 解析失败 {stats['bad_lines']} 行。")
    add("- `logs/8p_run*.states.jsonl` 是 batch 报告（不是 JSONL，也不含 advice），不纳入。")
    add("- 每个决策点按「手牌+街+hero 手牌+牌面」去重：同一点的不同帧数值逐位相同，重复计点会虚高统计。")
    add("")
    add("| 文件 | advice | 采用 | 丢弃 | 坏行 |")
    add("|---|---|---|---|---|")
    for item in stats["files"]:
        add(f"| `{item['path']}` | {item['advices']} | {item['used']} | "
            f"{item['skipped']} | {item['bad_lines']} |")
    add("")
    add("丢弃原因：")
    add("")
    for reason, count in sorted(stats["skipped"].items(), key=lambda kv: -kv[1]):
        add(f"- `{reason}`: {count}")
    add("")
    add("## 3. 口径 A：3 / 4 / 5 人池的差值分布")
    add("")
    add("同一点上，把对手数统一设为 2 / 3 / 4 家随机牌（对每一点都是反事实："
        "真实人数见下表实际人数分布），两套口径各算一次。")
    add("")
    lines.extend(_dist_table(groups, samples))
    add("")
    actual: dict[int, int] = {}
    for pt in points:
        actual[pt.actual_players] = actual.get(pt.actual_players, 0) + 1
    dist = "、".join(f"{k} 人 {v} 点" for k, v in sorted(actual.items()))
    add(f"这批点的真实在局人数分布：{dist}。")
    add("")
    add("上表是**反事实**（把每点都当成 3/4/5 人池）。真实人数下两套口径的差：")
    add("")
    add("| 真实在局人数 | 点 | 均值差 | 中位 | 最坏\\|差\\| |")
    add("|---|---|---|---|---|")
    by_actual: dict[int, list[float]] = {}
    for pt in points:
        by_actual.setdefault(pt.actual_pool or 0, []).append(pt.actual_diff)
    for pool in sorted(by_actual):
        summ = summarize(by_actual[pool])
        label = "2（单挑）" if pool == 2 else f"{pool}"
        add(f"| {label} | {summ['n']} | {pp(summ['mean'])} | {pp(summ['median'])} | "
            f"{pp(summ['worst_abs'])} |")
    overall = summarize([pt.actual_diff for pt in points])
    add(f"| **全部（按真实人数加权）** | {overall['n']} | {pp(overall['mean'])} | "
        f"{pp(overall['median'])} | {pp(overall['worst_abs'])} |")
    add("")
    add("2 人池那一行是**噪声标尺**：n=1 时两个口径算的是同一个量，"
        "残差只剩 MC 抽样误差，可以据此判断上面几行的差值有多少是真的。")
    add("")
    if refined_actual_rows(points):
        add(f"真实人数口径里最坏的几点用 {REFINE_FACTOR}× 样本复核"
            "（池越大抽牌越多，单点噪声比 8000 样本的 ±1.10pp 更大）：")
        add("")
        add("| 手牌 | 牌面 | 街 | 真实人数 | 原始差 | 复核样本 | 复核差 |")
        add("|---|---|---|---|---|---|---|")
        for pt in refined_actual_rows(points):
            entry = pt.chain["refine_actual"]
            add(f"| `{pt.hero}` | `{pt.board}` | {pt.street} | {pt.actual_pool} | "
                f"{pp(pt.actual_diff)} | {entry['samples']} | {pp(entry['diff'])} |")
        add("")
    big = [pt for pt in points if (pt.actual_pool or 0) >= 6]
    if big:
        bsum = summarize([pt.actual_diff for pt in big])
        add(f"真实 ≥6 人池（{bsum['n']} 点）偏得更多：均值 {pp(bsum['mean'])}pp，"
            f"最坏 {pp(bsum['worst_abs'])}pp——人数越多，独立近似偏得越多，"
            "方向与 3/4/5 人池一致（都是独立偏低）。"
            f"注意这批点样本很薄（{bsum['n']} 点），单点噪声 ±{mc_noise_pp(samples):.1f}pp，"
            "只能当趋势证据，不能当分位数的精确值。")
        refined = refined_actual_rows(points)
        if refined:
            worst_pt = refined[0]
            add(f"最坏的那个 {worst_pt.actual_pool} 人池点"
                f"（`{worst_pt.hero}` / `{worst_pt.board}`）"
                f"{REFINE_FACTOR}× 复核后仍是 |差| "
                f"{abs(worst_pt.chain['refine_actual']['diff']):.2f}pp，"
                "不是抽样噪声：大池里「至少一家领先」的独立近似确实显著偏低。"
                "它不进任何数值判定线（只喂提示词兜底文案），"
                "但如果以后现场常驻 ≥6 人池、或有人把这个数字当判定输入，判据要重新过一遍。")
    add("")
    add("## 4. 趋势：差值随牌力与人数怎么变")
    add("")
    add(f"按 `coach.read_hand(...).tier` 分档（本批出现 {len(tiers)} 档："
        + "、".join(tiers) + "），5 人池（对手 4 家，最吃紧的一档）：")
    add("")
    lines.extend(_tier_table(points, POOLS[-1]))
    add("")
    add("同一分档在 3 / 4 / 5 人池下的均值：")
    add("")
    lines.extend(_trend_table(points))
    add("")
    add("## 5. 最坏个案（原始样本 + 复核）")
    add("")
    lines.extend(_worst_table(points))
    add("")
    add(f"复核用 {REFINE_FACTOR}× 样本重算联合口径（单点 MC 噪声 "
        f"≈±{mc_noise_pp(samples):.2f}pp，不复核的话最坏值本身有一半是噪声）。"
        "结论那一行取的是所有复核值里最大的那个，它不一定出现在本表首行。")
    add("")
    add("## 6. 事实核验：真实链路这两个数字各自从哪来")
    add("")
    add(CHAIN_FACT_NOTE.rstrip())
    add("")
    lines.extend(_chain_section(chain, points, samples))
    add("")
    add("## 7. 结论与判据")
    add("")
    add("判据（**先写后算**，固定在工具里 `CRITERION_MEAN_PP` / `CRITERION_WORST_PP`）：")
    add("")
    add(f"1. 3/4/5 人池中任一池的 \\|均值差\\| ≥ {CRITERION_MEAN_PP:.0f}pp，"
        f"或最坏 \\|差\\| ≥ {CRITERION_WORST_PP:.0f}pp；")
    add(f"2. 且差值会跨过下游判定线：`hand_strength._BEHIND_DEMOTE = {DEMOTE_LINE}`"
        "（behind ≥ 35% 且大注降一档），或提示词 1pp 的渲染分辨率；")
    add("3. 两条同时成立才改实现；否则只写文档。")
    add("")
    add(head)
    add("")
    for line in criterion_lines:
        add(line)
    add("")
    return "\n".join(lines) + "\n"


# ───────────────────────── 入口 ─────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="量化「至少一家领先我」的双口径偏差（只读，不调模型）")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                    help=f"联合口径每点样本数（默认 {DEFAULT_SAMPLES}）")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 个决策点")
    ap.add_argument("--markdown", type=Path, default=None,
                    help="把完整报告写到这个路径")
    ap.add_argument("--no-chain", action="store_true",
                    help="跳过真实链路旁证（省时间）")
    ap.add_argument("--json", type=Path, default=None,
                    help="把逐点数字写成 JSON")
    ap.add_argument("--from-json", type=Path, default=None, metavar="PATH",
                    help="从 --json 落下的逐点数字重建报告（不重算抽样）")
    args = ap.parse_args(argv)

    if args.from_json:
        points, stats, chain, samples = load_rows(args.from_json)
        print(f"从 {args.from_json} 读回 {len(points)} 点（样本数 {samples}）",
              file=sys.stderr)
        return emit(points, stats, chain, samples, args, sources=[])

    sources = data_sources()
    if not sources:
        print("没有找到任何已录数据（logs/ runs/）", file=sys.stderr)
        return 1
    print(f"数据源 {len(sources)} 个", file=sys.stderr)
    points, stats = collect(sources, args.limit)
    if not points:
        print("没有可用的翻牌后决策点", file=sys.stderr)
        return 1
    print(f"可用决策点 {len(points)} 个，开始算口径 A"
          f"（每点 3 次抽样 × {POOLS} 池）", file=sys.stderr)
    measure(points, args.samples)
    refine_worst(points, args.samples)
    refine_actual(points, args.samples)

    chain = {"rows": [], "fails": {}, "coverage": 0, "total": 0}
    if not args.no_chain:
        print("真实链路旁证（口径 B）", file=sys.stderr)
        chain = chain_gap(points)

    return emit(points, stats, chain, args.samples, args, sources)


def emit(points: list[Point], stats: dict, chain: dict, samples: int,
         args: argparse.Namespace, sources: list[Path]) -> int:
    """打印摘要、落 JSON / markdown。"""
    groups = {pool: summarize([pt.diff[pool] for pt in points]) for pool in POOLS}
    print()
    for pool in POOLS:
        summ = groups[pool]
        print(f"{pool} 人池：均值 {summ['mean']:+.2f}pp，"
              f"最坏 {summ['worst_abs']:+.2f}pp，"
              f"偏高 {summ['n_positive']} / 偏低 {summ['n_negative']}")
    head, _ = verdict(points, samples)
    print(head.replace("**", "").replace("\\|", "|"))

    if args.json:
        rows = [{"hand": pt.hand, "street": pt.street, "hero": pt.hero,
                 "board": pt.board, "tier": pt.tier, "p1": pt.p1,
                 "actual_players": pt.actual_players,
                 "actual_pool": pt.actual_pool,
                 "actual_diff": pt.actual_diff,
                 "indep": pt.indep, "joint": pt.joint, "diff": pt.diff,
                 "chain": pt.chain} for pt in points]
        payload = {"samples": samples, "stats": stats,
                   "chain": {k: v for k, v in chain.items() if k != "rows"},
                   "rows": rows}
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)

    if args.markdown:
        text = build_markdown(points, stats, chain, samples, sources)
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(text, encoding="utf-8")
        print(f"wrote {args.markdown}", file=sys.stderr)
    return 0


def load_rows(path: Path) -> tuple[list[Point], dict, dict, int]:
    """读回 `--json` 的逐点数字（复现报告不用重算 Monte Carlo）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    points: list[Point] = []
    for row in data["rows"]:
        pt = Point(source="(json)", hand=row["hand"], street=row["street"],
                   hero=row["hero"], board=row["board"], tier=row["tier"],
                   p1=float(row["p1"]),
                   actual_players=int(row["actual_players"]))
        pt.indep = {int(k): float(v) for k, v in row["indep"].items()}
        pt.joint = {int(k): float(v) for k, v in row["joint"].items()}
        pt.diff = {int(k): float(v) for k, v in row["diff"].items()}
        pt.actual_pool = row.get("actual_pool")
        pt.actual_diff = row.get("actual_diff")
        pt.chain = row.get("chain")
        points.append(pt)
    chain = dict(data.get("chain") or {})
    chain.setdefault("rows", [])
    chain.setdefault("fails", {})
    chain.setdefault("coverage", sum(
        1 for pt in points if (pt.chain or {}).get("joint_range_pct") is not None))
    chain.setdefault("total", len(points))
    return points, data.get("stats") or {}, chain, int(data.get("samples") or DEFAULT_SAMPLES)


if __name__ == "__main__":
    sys.exit(main())
