#!/usr/bin/env python3
"""9 人局面的权益耗时基准：只打印口径，不设阈值、不做门禁（ticket #117）。

    nv tools/equity_benchmark.py
    nv tools/equity_benchmark.py --runs 20 --samples 1500 --budget-ms 2000
    nv tools/equity_benchmark.py --runs 10 --modeled 0 --json

构造 9 个**全部在局**的对手：`--modeled N` 家有范围证据（逐家范围进联合
抽样），其余 `9 - N` 家按随机牌补位（`random_opponents`），两类对手在同
一次 `_monte_carlo` 里整组拒绝重抽。默认 2 家有范围 + 7 家随机牌，等于
`tests/test_equity_all_opponents.py` 的 9 家 fixture；`--modeled 9` 走全建模。

每次运行报：

  · `per_run_ms`：单次 `hand_vs_ranges.compute` 的墙钟耗时（清缓存后测）；
  · `p50_ms` / `p90_ms` / `max_ms`：nearest-rank 分位，p90 是第一轮门槛
    「9 人局面 p90 ≤ 2 秒」要看的那个数；
  · `opponents_in_sample`：`len(hr.opponents) + hr.random_opponents`，与局面
    应有的在局人数（9）逐次比对，一致才打 `ok` —— 用来证明降采样时没有
    偷偷少算对手；
  · `samples_requested` / `per_run_samples` / `downsampled`：预算是否触发
    降采样（降采样只减少抽样次数，对手数不变）。

**这是手动口径工具，不是测试门禁**：没有阈值断言、不返回失败（计算本身
出错时返回 1）；也不注册进 `tests/run_all.py`。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poker_ocr import hand_vs_ranges, ranges  # noqa: E402

OPPONENTS_IN_HAND = 9
DEFAULT_RUNS = 20
DEFAULT_SAMPLES = 1500
DEFAULT_BUDGET_MS = 2000
DEFAULT_MODELED = 2
HERO = ("Ah", "Qh")
BOARD = ("As", "Ks", "4d")

POSITIONS = ("BB", "SB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN")
# 9 组互不相同的翻前范围（169 键），够 `--modeled 9` 全建模用。
RANGE_KEYS = (
    ("KK", "44", "AKs", "AKo", "KJo"),
    ("QQ", "JJ", "KQs", "KJs", "QJs"),
    ("99", "77", "AQs", "AJo", "KTs"),
    ("TT", "66", "AJs", "ATo", "QTs"),
    ("88", "55", "K9s", "KTo", "JTs"),
    ("33", "22", "Q9s", "QTo", "T9s"),
    ("A5s", "A4s", "76s", "65s", "54s"),
    ("K8s", "Q8s", "J8s", "T8s", "98s"),
    ("A9o", "A8o", "K7o", "Q7o", "J7o"),
)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="9 人局面的权益耗时基准（手动工具，不是测试门禁）")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"重复次数（默认 {DEFAULT_RUNS}）")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                    help=f"每次的目标抽样数（默认 {DEFAULT_SAMPLES}）")
    ap.add_argument("--budget-ms", type=int, default=DEFAULT_BUDGET_MS,
                    help=f"MC 时间预算毫秒；0 = 不限时（默认 {DEFAULT_BUDGET_MS}）")
    ap.add_argument("--modeled", type=int, default=DEFAULT_MODELED,
                    help=f"有范围证据的对手数，其余按随机牌补位"
                         f"（默认 {DEFAULT_MODELED}）")
    ap.add_argument("--seed-prefix", default="equity-bench",
                    help="每次运行的 seed_key 前缀（默认 equity-bench）")
    ap.add_argument("--json", action="store_true",
                    help="只输出 JSON（便于脚本读取）")
    return ap


def _ranges_for(modeled: int):
    """前 `modeled` 个座位各给一个不同的范围，返回 compute 的 entries。"""
    entries = []
    for i in range(modeled):
        combos = []
        for key in RANGE_KEYS[i]:
            combos.extend(ranges.combos_of_key(key))
        built = ranges.make_range(f"bench{i}", {c: 1.0 for c in combos})
        if built is None:
            raise RuntimeError(f"基准范围 bench{i} 为空")
        entries.append((f"o{i}", POSITIONS[i], built))
    return entries


def _percentile(values, pct: float) -> float | None:
    """nearest-rank 分位（与 tools/ab_compare.py 同形状，这里独立实现）。"""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[k - 1]


def run_benchmark(*, runs: int, samples: int, modeled: int, budget_ms: int,
                  seed_prefix: str = "equity-bench") -> dict:
    """跑 `runs` 次 9 人局面，返回原始耗时与抽样口径（不设阈值）。"""
    runs = max(1, int(runs))
    samples = max(1, int(samples))
    modeled = max(0, min(int(modeled), OPPONENTS_IN_HAND))
    entries = _ranges_for(modeled)
    random_opponents = OPPONENTS_IN_HAND - modeled

    per_run_ms: list[float] = []
    per_run_samples: list[int] = []
    per_run_opponents: list[int] = []
    downsample_runs = 0
    warnings: list[str] = []
    # 预算经环境变量交给生产代码（compute 不接预算入参）；跑完恢复，报告里
    # 的 budget_ms 因此就是实际生效的那个值。
    previous = os.environ.get("HAND_VS_RANGES_BUDGET_MS")
    os.environ["HAND_VS_RANGES_BUDGET_MS"] = str(budget_ms)
    try:
        for i in range(runs):
            # 每次都是冷缓存：基准测的是 MC 本身，不是命中缓存。
            hand_vs_ranges._CACHE.clear()
            started = time.perf_counter()
            result = hand_vs_ranges.compute(
                HERO, BOARD, entries, samples=samples,
                random_opponents=random_opponents,
                seed_key=f"{seed_prefix}-{i}")
            elapsed = (time.perf_counter() - started) * 1000.0
            if result is None:
                raise RuntimeError(f"第 {i + 1}/{runs} 次基准没有算出权益")
            per_run_ms.append(round(elapsed, 1))
            per_run_samples.append(result.samples)
            per_run_opponents.append(
                len(result.opponents) + result.random_opponents)
            if any("降采样" in w for w in result.warnings):
                downsample_runs += 1
            if not warnings:
                warnings = list(result.warnings)
    finally:
        if previous is None:
            os.environ.pop("HAND_VS_RANGES_BUDGET_MS", None)
        else:
            os.environ["HAND_VS_RANGES_BUDGET_MS"] = previous

    ok = all(n == OPPONENTS_IN_HAND for n in per_run_opponents)
    return {
        "opponents_expected": OPPONENTS_IN_HAND,
        "opponents_in_sample": min(per_run_opponents),
        "per_run_opponents": per_run_opponents,
        "all_opponents_sampled": ok,
        "modeled": modeled,
        "random_fill": random_opponents,
        "runs": runs,
        "budget_ms": budget_ms,
        "samples_requested": samples,
        "per_run_samples": per_run_samples,
        "min_samples": min(per_run_samples),
        "downsampled": downsample_runs > 0,
        "downsample_runs": downsample_runs,
        "warnings": warnings,
        "per_run_ms": per_run_ms,
        "p50_ms": _percentile(per_run_ms, 50),
        "p90_ms": _percentile(per_run_ms, 90),
        "max_ms": max(per_run_ms),
    }


def _print_report(report: dict) -> None:
    ok = "ok" if report["all_opponents_sampled"] else "MISMATCH"
    print(f"9 人局面权益基准：{report['modeled']} 家有范围 + "
          f"{report['random_fill']} 家随机牌 · runs={report['runs']} "
          f"samples={report['samples_requested']} "
          f"budget={report['budget_ms']}ms")
    print(f"  在局对手进抽样: {report['opponents_in_sample']}"
          f"/{report['opponents_expected']}  {ok}")
    downsample = (f"是（{report['downsample_runs']}/{report['runs']} 次）"
                  if report["downsampled"] else "否")
    print(f"  实际有效样本: {report['min_samples']}"
          f"–{max(report['per_run_samples'])}"
          f"（目标 {report['samples_requested']}）· 降采样: {downsample}")
    print(f"  耗时 ms: p50={report['p50_ms']} p90={report['p90_ms']} "
          f"max={report['max_ms']}")
    print(f"  每次: {report['per_run_ms']}")
    for warning in report["warnings"]:
        print(f"  warning: {warning}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_benchmark(
            runs=args.runs, samples=args.samples, modeled=args.modeled,
            budget_ms=args.budget_ms, seed_prefix=args.seed_prefix)
    except RuntimeError as exc:
        print(f"基准失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
