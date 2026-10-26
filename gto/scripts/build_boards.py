#!/usr/bin/env python3
"""生成牌面代表集（boards.json）。

22,100 个翻牌 → 花色同构收敛到 1,755 → 用牌面特征做 k-means → 每类取 medoid。

用法：
    python gto/scripts/build_boards.py --k 48
    python gto/scripts/build_boards.py --k 184   # 全量目标
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np

RANKS = "23456789TJQKA"
SUITS = "cdhs"
GTO = Path(__file__).resolve().parents[1]

SUIT_PERMS = list(itertools.permutations(range(4)))


def card_str(card: int) -> str:
    return RANKS[card // 4] + SUITS[card % 4]


def canonical(cards: tuple[int, int, int]) -> tuple[int, ...]:
    """花色同构下的字典序最小表示。"""
    best = None
    for perm in SUIT_PERMS:
        mapped = sorted(((c // 4, perm[c % 4]) for c in cards))
        flat = tuple(x for pair in mapped for x in pair)
        if best is None or flat < best:
            best = flat
    return best  # type: ignore[return-value]


def features(flat: tuple[int, ...]) -> list[float]:
    ranks = [flat[0], flat[2], flat[4]]
    suits = [flat[1], flat[3], flat[5]]
    counts = {s: suits.count(s) for s in set(suits)}
    max_suit = max(counts.values())
    uniq = sorted(set(ranks))
    paired = 1.0 if len(uniq) < 3 else 0.0
    trips = 1.0 if len(uniq) == 1 else 0.0
    span = (ranks[-1] - ranks[0]) / 12.0
    gap1 = (ranks[1] - ranks[0]) / 12.0
    gap2 = (ranks[2] - ranks[1]) / 12.0
    broadway = sum(1 for r in ranks if r >= 8) / 3.0
    straight = 1.0 if (ranks[-1] - ranks[0] <= 4 and len(uniq) == 3) else 0.0
    # 权重：高牌/对子/同花/连通性更重要，低牌细节相对次要
    return [
        2.0 * ranks[2] / 12.0,
        1.2 * ranks[1] / 12.0,
        0.8 * ranks[0] / 12.0,
        1.5 * paired,
        1.5 * trips,
        1.5 * (1.0 if max_suit == 3 else 0.0),
        1.0 * (1.0 if max_suit == 2 else 0.0),
        1.5 * span,
        1.0 * gap1,
        1.0 * gap2,
        1.5 * broadway,
        1.0 * straight,
        2.0 * (1.0 if ranks[2] == 12 else 0.0),
        1.5 * (1.0 if ranks[2] == 11 else 0.0),
    ]


def kmeans(x: np.ndarray, k: int, seed: int, iters: int = 60):
    rng = np.random.default_rng(seed)
    n = x.shape[0]
    # k-means++
    centers = [x[rng.integers(n)]]
    for _ in range(k - 1):
        d2 = np.min(((x[:, None, :] - np.array(centers)[None, :, :]) ** 2).sum(-1), axis=1)
        probs = d2 / d2.sum()
        centers.append(x[rng.choice(n, p=probs)])
    centers = np.array(centers)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        d2 = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(-1)
        labels = d2.argmin(axis=1)
        new = centers.copy()
        for j in range(k):
            m = labels == j
            if m.any():
                new[j] = x[m].mean(axis=0)
        if np.allclose(new, centers):
            centers = new
            break
        centers = new
    d2 = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(-1)
    labels = d2.argmin(axis=1)
    medoids = []
    used: set[int] = set()
    for j in range(k):
        idx = np.where(labels == j)[0]
        if len(idx) == 0:
            idx = np.arange(n)
        order = idx[np.argsort(d2[idx, j])]
        pick = int(order[0])
        for cand in order:
            if int(cand) not in used:
                pick = int(cand)
                break
        used.add(pick)
        medoids.append(pick)
    return labels, centers, medoids


def stratified(boards: list[dict]) -> list[dict]:
    """按高牌/对子/同花桶轮转排序：前 N 个也能覆盖各种纹理。"""
    from collections import defaultdict

    def bucket(b: dict):
        f = b["flop"]
        ranks = [RANKS.index(f[i]) for i in (0, 2, 4)]
        hi = max(ranks)
        paired = len(set(ranks)) < 3
        suits = [f[i] for i in (1, 3, 5)]
        ms = max(suits.count(s) for s in set(suits))
        hg = 6 if hi == 12 else 5 if hi == 11 else 4 if hi == 10 else 3 if hi == 9 \
            else 2 if hi >= 8 else 1 if hi >= 5 else 0
        return (hg, int(paired), ms)

    groups: dict = defaultdict(list)
    for b in boards:
        groups[bucket(b)].append(b)
    for v in groups.values():
        v.sort(key=lambda b: -max(RANKS.index(b["flop"][i]) for i in (0, 2, 4)))
    out: list[dict] = []
    i = 0
    while any(len(v) > i for v in groups.values()):
        for k in sorted(groups):
            if i < len(groups[k]):
                out.append(groups[k][i])
        i += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=48)
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--out", default=str(GTO / "specs" / "boards.json"))
    args = ap.parse_args()

    seen: dict[tuple[int, ...], tuple[int, int, int]] = {}
    for cards in itertools.combinations(range(52), 3):
        key = canonical(cards)  # type: ignore[arg-type]
        seen.setdefault(key, cards)  # type: ignore[arg-type]
    keys = sorted(seen)

    def key_str(k: tuple[int, ...]) -> str:
        return "".join(RANKS[k[2 * i]] + SUITS[k[2 * i + 1]] for i in range(3))

    flops = [key_str(k) for k in keys]
    print(f"canonical flops: {len(keys)}")

    x = np.array([features(k) for k in keys], dtype=np.float64)
    k = min(args.k, len(keys))
    labels, centers, medoids = kmeans(x, k, args.seed)

    boards = []
    for j in range(k):
        boards.append({
            "class_id": j,
            "flop": key_str(keys[medoids[j]]),
            "size": int((labels == j).sum()),
            "features": [round(v, 4) for v in x[medoids[j]].tolist()],
        })
    boards = stratified(boards)
    out = {
        "version": 1,
        "method": "kmeans-features-medoid",
        "k": k,
        "seed": args.seed,
        "flops": len(keys),
        "boards": boards,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # 1,755 → 类的最近中心映射（线上查询用）
    mapping = {}
    for i, key in enumerate(keys):
        mapping[flops[i]] = int(labels[i])
    (Path(args.out).parent / "board_map.json").write_text(
        json.dumps({"version": 1, "map": mapping}, separators=(",", ":")), encoding="utf-8"
    )
    print(f"wrote {args.out}: k={k}, medoid examples: {[b['flop'] for b in boards[:8]]}")


if __name__ == "__main__":
    main()
