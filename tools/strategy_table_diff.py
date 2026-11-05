#!/usr/bin/env python3
"""策略表 v0 / v1 的数值对比：同一批真实决策点，两套表各算一遍。

为什么需要它
────────────
P1 给 `facing_probs` / `base_fold` / `size_gamma` 填了一版**按公式推导**的值
（见 `poker_ocr/tables/*.json` 的 v1 与 `derivation` 字段）。但没有裁判就没法
说「变好了」。所以这里只回答一个可以回答的问题：**这两套表算出来的东西差多少**。

差得小 → 表不是当前瓶颈，继续调 prompt 没用，得先建裁判；
差得大 → 表是瓶颈，值得花钱买求解器数据/做实测校准。

测什么
──────
对 `logs/live.states.jsonl` 里每个翻后决策点（用 `replay_live_decisions` 的
快照重建），跑当前代码的事实层，记录：

  · 对手范围的宽度与构成（value / semi / bluff、两对+、顶对）；
  · 条件弃牌率、各尺寸的 EV、跟注 EV、范围权益。

然后比较两套表的分布差异，以及**尺寸选择的排名是否翻转** —— 后者是真正
会让建议动作改变的那一步。

用法
────
    # 分开跑（子进程保证模块常量按 profile 重新加载）
    STRATEGY_TABLES_PROFILE=hand-tuned .venv/bin/python tools/strategy_table_diff.py --dump /tmp/v0.json
    STRATEGY_TABLES_PROFILE=sourced    .venv/bin/python tools/strategy_table_diff.py --dump /tmp/v1.json

    # 一条命令跑完并对比
    .venv/bin/python tools/strategy_table_diff.py --compare

注意：默认不注入对手画像（不读 ProfileStore），这样差异**只来自策略表**，
不掺画像噪声。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_LOG = ROOT / "logs" / "live.states.jsonl"
DEFAULT_HANDS = ROOT / "hands" / "1023.hands.json"


def _measure(limit: int, log: Path, hands: Path) -> dict:
    # 延迟导入：模块在 import 时按 STRATEGY_TABLES_PROFILE 读表。
    from poker_ocr.situation import build_situation
    from tools.replay_live_decisions import (
        build_snap, find_state, load,
    )

    records = load(log)
    states = [r for r in records if r.get("type") == "state"]
    advices = [r for r in records if r.get("type") == "advice"]
    hand_map: dict[str, dict] = {}
    if hands.exists():
        for hand in json.loads(hands.read_text(encoding="utf-8")):
            hand_map[str(hand.get("hand_id"))] = hand

    rows = []
    since = ""
    for advice in advices:
        since_ts = advice["ts"]
        state = find_state(advice, states, since)
        since = since_ts
        if state is None or advice.get("street") == "preflop":
            continue
        try:
            snap = build_snap(advice, state, hand_map.get(str(advice.get("hand_id"))))
            sit = build_situation(snap, table_dynamics=None)
        except Exception as exc:               # noqa: BLE001
            rows.append({"error": f"{type(exc).__name__}: {exc}",
                         "hand_id": advice.get("hand_id")})
            continue
        row: dict = {
            "hand_id": advice.get("hand_id"),
            "street": advice.get("street"),
            # 同一手有多个决策点：hand_id 不唯一，比较时必须用这个键。
            "idx": len(rows),
        }
        hr = getattr(sit, "hand_ranges", None)
        if hr is not None:
            row["equity"] = float(getattr(hr, "equity", 0.0))
            row["behind_any"] = float(getattr(hr, "behind_any", 0.0))
        rep = getattr(sit, "fold_eq", None)
        if rep is not None:
            row["combos"] = int(getattr(rep, "combos", 0) or 0)
            row["strong_share"] = float(getattr(rep, "strong_share", 0.0) or 0.0)
            row["medium_share"] = float(sum(
                share for tier, share in (getattr(rep, "shares", ()) or ())
                if tier in ("顶对弱踢", "中/底对", "中底对")))
            row["fold_equity"] = float(getattr(rep, "fold_equity", 0.0) or 0.0)
            row["range_equity"] = float(getattr(rep, "range_equity", 0.0) or 0.0)
            row["realization"] = float(getattr(rep, "realization", 1.0) or 1.0)
            lines = list(getattr(rep, "lines", ()) or ())
            row["sizes"] = [
                {"label": str(getattr(line, "label", "")),
                 "fold": float(getattr(line, "fold_equity", 0.0)),
                 "breakeven": float(getattr(line, "breakeven", 0.0)),
                 "ev": float(getattr(line, "ev", 0.0))}
                for line in lines
            ]
            if row["sizes"]:
                best = max(row["sizes"], key=lambda item: item["ev"])
                row["best_size"] = best["label"]
                row["best_ev"] = best["ev"]
                row["ev_spread"] = best["ev"] - min(s["ev"] for s in row["sizes"])
        rows.append(row)
        if limit and len(rows) >= limit:
            break
    return {"profile": os.environ.get("STRATEGY_TABLES_PROFILE", "hand-tuned"),
            "rows": rows}


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


_FIELDS = ("combos", "strong_share", "medium_share", "fold_equity",
           "range_equity", "realization", "equity", "best_ev", "ev_spread")


def _compare(a: dict, b: dict) -> int:
    ra = {r["idx"]: r for r in a["rows"] if "error" not in r}
    rb = {r["idx"]: r for r in b["rows"] if "error" not in r}
    common = [k for k in ra if k in rb]
    print(f"{a['profile']} 决策点 {len(ra)}｜{b['profile']} 决策点 {len(rb)}｜"
          f"可比 {len(common)}")
    if not common:
        return 1

    print("\n指标均值（只统计两边都算出来的点）")
    print(f"{'字段':16s} {a['profile']:>12s} {b['profile']:>12s} "
          f"{'Δ':>10s} {'|Δ|中位':>10s}")
    for field in _FIELDS:
        va = [ra[k][field] for k in common if field in ra[k]]
        vb = [rb[k][field] for k in common if field in rb[k]]
        if not va or not vb:
            continue
        pairs = [(ra[k][field], rb[k][field]) for k in common
                 if field in ra[k] and field in rb[k]]
        deltas = [y - x for x, y in pairs]
        print(f"{field:16s} {_mean(va):12.4f} {_mean(vb):12.4f} "
              f"{_mean(deltas):+10.4f} "
              f"{statistics.median(abs(d) for d in deltas):10.4f}")

    flips = [k for k in common
             if ra[k].get("best_size") and rb[k].get("best_size")
             and ra[k]["best_size"] != rb[k]["best_size"]]
    print(f"\n尺寸排名翻转：{len(flips)} / {len(common)} "
          f"({len(flips) / len(common):.0%})")
    for k in flips[:10]:
        print(f"  {ra[k]['hand_id']:>6s} {ra[k]['street']:5s} "
              f"{ra[k]['best_size']:>10s} → {rb[k]['best_size']:<10s} "
              f"(EV {ra[k]['best_ev']:+.2f} → {rb[k]['best_ev']:+.2f})")

    big = [k for k in common
           if "fold_equity" in ra[k] and "fold_equity" in rb[k]
           and abs(rb[k]["fold_equity"] - ra[k]["fold_equity"]) >= 0.08]
    print(f"\n弃牌率变化 ≥8pt：{len(big)} / {len(common)}")
    return 0


PRICES = (0.10, 0.15, 0.20, 0.25, 0.33, 0.50, 0.75, 1.0)
BOARDS = (
    ("干燥高牌 K72r", "Ks7h2c"),
    ("湿润同花 9TJcc", "9cTcJc"),
    ("公对面 664", "6s6h4d"),
)
PRIORS = ("BB_DEF_VS_BTN", "RFI_BTN", "COLD_CALL_IP_vs_BTN")


def _table_level() -> int:
    """不依赖日志的直接对比：弃牌曲线 + 范围收窄。（子进程内执行）"""
    from poker_ocr import fold_equity as fe
    from poker_ocr import range_flow, ranges
    from poker_ocr.coach import parse_cards

    print(f"profile={os.environ.get('STRATEGY_TABLES_PROFILE', 'hand-tuned')}")
    print("\n## 弃牌曲线 fold_probability(tier, price)")
    print(f"{'档位':10s}" + "".join(f"{p:>8.2f}" for p in PRICES))
    for tier in fe.TIER_ZH:
        row = "".join(f"{fe.fold_probability(tier, p):8.3f}" for p in PRICES)
        print(f"{fe.TIER_ZH[tier]:10s}{row}")
    print(f"\nγ（价格弹性）= {fe.SIZE_GAMMA:g}")

    print("\n## 范围收窄（真实翻前先验 × 3 个牌面 × 4 条线路）")
    print(f"{'先验':20s}{'牌面':18s}{'线路':8s}{'占比':>8s}{'value':>8s}"
          f"{'semi':>8s}{'bluff':>8s}{'强成牌':>8s}")
    # facing_bet=True 的三条线路才会用到 _FACING_PROBS，必须覆盖。
    lines = (
        ("check", False),
        ("bet", False),
        ("call", True),
        ("raise", True),
    )
    for prior_id in PRIORS:
        prior = ranges.get(prior_id)
        if prior is None:
            print(f"{prior_id:20s} 取不到范围，跳过")
            continue
        for label, board in BOARDS:
            b = tuple(parse_cards(board))
            for action, facing in lines:
                event = range_flow.ActionEvent(
                    street="flop", board=board, seat_id="o1",
                    action=action, facing_bet=facing, players_behind=1,
                    is_donk=False, closes_action=(action == "call"))
                built = range_flow.build_range(prior, (event,))
                if built is None:
                    print(f"{prior_id:20s}{label:18s}{action:8s}  build_range=None")
                    continue
                comp = range_flow.composition(built, b)
                combos = sum(1 for w in built.combo_weights.values() if w > 0)
                print(f"{prior_id:20s}{label:18s}{action:8s}{combos / 1326:8.1%}"
                      f"{comp.value:8.1%}{comp.semi:8.1%}{comp.bluff:8.1%}"
                      f"{comp.two_pair_plus:8.1%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="策略表 v0/v1 数值对比")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--hands", type=Path, default=DEFAULT_HANDS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dump", type=Path, default=None, help="把测量结果写到文件")
    ap.add_argument("--compare", action="store_true",
                    help="依次用两个 profile 跑并对比")
    ap.add_argument("--tables", action="store_true",
                    help="直接对比表本身（弃牌曲线 + 范围收窄），不读日志")
    args = ap.parse_args(argv)

    if args.tables:
        return _table_level()

    if args.compare:
        out = {}
        for profile, tag in (("hand-tuned", "v0"), ("sourced", "v1")):
            path = Path(f"/tmp/strategy-diff-{tag}.json")
            env = dict(os.environ, STRATEGY_TABLES_PROFILE=profile)
            proc = subprocess.run(
                [sys.executable, __file__, "--log", str(args.log),
                 "--hands", str(args.hands), "--limit", str(args.limit),
                 "--dump", str(path)],
                env=env, cwd=str(ROOT), capture_output=True, text=True)
            if proc.returncode != 0:
                print(proc.stdout[-2000:])
                print(proc.stderr[-3000:], file=sys.stderr)
                return proc.returncode
            out[profile] = json.loads(path.read_text(encoding="utf-8"))
        return _compare(out["hand-tuned"], out["sourced"])

    result = _measure(args.limit, args.log, args.hands)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.dump:
        args.dump.write_text(payload, encoding="utf-8")
        ok = sum(1 for r in result["rows"] if "error" not in r)
        bad = len(result["rows"]) - ok
        print(f"{result['profile']}：{ok} 个决策点（失败 {bad}）→ {args.dump}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
