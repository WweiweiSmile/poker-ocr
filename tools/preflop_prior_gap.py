#!/usr/bin/env python3
"""翻前先验缺口：哪些手牌在摊牌时出现、但先验表里根本没有。

用途
────
`showdown_range_check` 报出「55 个未命中是 prior_absent」之后，下一步要回答：
**缺的到底是哪些牌、集中在哪张表**。这个工具只做归因，不改任何表：

  · 按先验列出缺失的 169 键与次数；
  · 按「牌型类别」归并（对子 / 同花 / 同花连张 / 非同花 …），避免逐手过拟合；
  · 给出「如果把这些都加进去」的命中率上限（**这是上界，不是效果**）。

口径提醒
────────
样本是「走到摊牌的手」，天然偏强、且每手不独立。所以这里的结论只用来
**指出方向**：哪张表系统性偏窄；不用来定每条边界的精确位置。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr import ranges  # noqa: E402
from tools.showdown_range_check import collect  # noqa: E402

RANKS = "23456789TJQKA"


def category(key: str) -> str:
    """169 键 → 粗类别（只用于归因，不用于加宽）。"""
    if len(key) == 2:
        return "对子"
    hi, lo, suited = key[0], key[1], key[-1]
    gap = RANKS.index(hi) - RANKS.index(lo)
    if suited == "s":
        if hi == "A":
            return "同花 A"
        if gap == 1:
            return "同花连张"
        if gap == 2:
            return "同花隔张"
        return "同花其他"
    if gap == 1:
        return "非同花连张"
    if hi in "AKQJ" and lo in "TJ9":
        return "非同花高张"
    return "非同花其他"


def main() -> int:
    ap = argparse.ArgumentParser(description="翻前先验缺口归因")
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    samples, stats = collect()
    total = len(samples)
    misses = [s for s in samples if not s.in_range and s.miss == "prior_absent"]
    if not total:
        print("没有样本", file=sys.stderr)
        return 2

    print(f"样本 {total}｜命中 {total - len([s for s in samples if not s.in_range])}"
          f"｜先验缺失 {len(misses)}\n")
    print("命中率上限（把缺的牌全加进去）："
          f"{(total - len([s for s in samples if not s.in_range])) / total:.0%} "
          f"→ {(total - len([s for s in samples if not s.in_range]) + len(misses)) / total:.0%}"
          f"（**上界，不是效果**：等于按样本过拟合）\n")

    by_prior: dict[str, Counter] = defaultdict(Counter)
    for s in misses:
        by_prior[s.prior_id][s.hand_key] += 1

    for prior_id, keys in sorted(by_prior.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(keys.values())
        print(f"## {prior_id}（缺 {n} 次 / {len(keys)} 个不同手牌）")
        print("   " + "，".join(f"{k}×{v}" if v > 1 else k
                                for k, v in keys.most_common(args.top)))
        cats = Counter(category(k) for k in keys)
        print("   类别：" + "，".join(f"{c} {v}" for c, v in cats.most_common()))
        # 这张表现有的宽度，用来判断「偏窄多少」
        rng = ranges.get(prior_id)
        if rng is not None:
            width = sum(1 for w in rng.combo_weights.values() if w > 0)
            print(f"   现有宽度 {width} 组合（{width / 1326:.0%}）")
        print()

    print("## 全局类别")
    cats = Counter(category(s.hand_key) for s in misses)
    for c, v in cats.most_common():
        print(f"  {c:12s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
