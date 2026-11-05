#!/usr/bin/env python3
"""P2 探针：用「我下注 → 他跟/弃」的实测，量出按价格的聚合弃牌率曲线。

为什么是这条曲线
────────────────
`P(fold | 他的手牌档)` 在你的数据里**不可识别**：他弃牌时你永远看不到他的手牌，
亮牌样本只有 ~4%。能识别的只有

    f̄(p) = Σ_t w_t(p) · f_t(p)          p = 下注额 / 他跟我们下注后的底池

也就是「某个价格上，一个范围平均会弃多少」。这张图的用途有两个：

  1. 看信号强度：本地样本够不够、噪声多大、趋势是否和模型同向；
  2. 对照 v0 / v1 两条先验曲线：如果实测和两条都差很远，说明问题在 level（scale），
     不在形状；如果实测形状的斜率明显不同，才轮到 `size_gamma`。

观测怎么取
──────────
对每一手里「Hero 下注/加注」之后的对手动作：

    P      = 下注前的底池
    S      = 这次下注的增量
    price  = S / (P + 2S)          # 他跟我们这一注后的底池占比
    弃牌   = 1；跟注/加注 = 0

只统计 Hero 动作**之后、下一次加注之前**的对手（加注之后价格变了，不再归到同一个点）。

口径提醒
────────
  · 多人池里每个对手算一个观测，但它们不独立（弃牌率是连乘关系），CI 会偏乐观；
  · 短码跟注（跟注额 < 应跟额且 all-in）价格不同，默认剔除；
  · 样本来自两种序列化格式（本地 outbox / DB），`amount` 都是「本街加到总额」。

用法
────
    .venv/bin/python tools/fold_curve_probe.py --source local
    .venv/bin/python tools/fold_curve_probe.py --source db
    .venv/bin/python tools/fold_curve_probe.py --source all --html /tmp/fold-curve.html
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr import fold_equity as fe  # noqa: E402
from poker_ocr import strategy_tables  # noqa: E402

POSTFLOP = ("flop", "turn", "river")
# 参考范围构成（和 derive_strategy_tables 一致）：典型 / 空气重 两种，用来框住模型曲线
MIXES = {
    "典型": {"强成牌": 0.03, "超对": 0.04, "顶对强踢": 0.10, "顶对弱踢": 0.12,
             "中底对": 0.20, "强听": 0.06, "弱听": 0.06, "打牌面": 0.04, "空气": 0.35},
    "空气重": {"强成牌": 0.02, "超对": 0.02, "顶对强踢": 0.06, "顶对弱踢": 0.08,
               "中底对": 0.14, "强听": 0.04, "弱听": 0.05, "打牌面": 0.04, "空气": 0.55},
}
# 价格桶（price）。0.25 = 1/2 池，0.2 = 1/3 池，0.286 = 2/3 池，0.333 = 满池。
BUCKETS = ((0.0, 0.15), (0.15, 0.22), (0.22, 0.27), (0.27, 0.32),
           (0.32, 0.42), (0.42, 1.01))


@dataclass(frozen=True)
class Obs:
    price: float
    fold: int
    street: str
    source: str


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def _parse_local(hand: dict) -> list[Obs]:
    out: list[Obs] = []
    for st in hand.get("streets") or []:
        street = str(st.get("street") or "")
        if street not in POSTFLOP:
            continue
        pot_start = st.get("pot_start")
        actions = st.get("actions") or []
        if pot_start is None or not actions:
            continue
        out += _scan(street, float(pot_start), actions, "local",
                     actor_key="seat_id", amount_key="amount",
                     hero_flag=True)
    return out


def _parse_db(hand: dict) -> list[Obs]:
    out: list[Obs] = []
    for st in hand.get("streets") or []:
        street = str(st.get("street") or "")
        if street not in POSTFLOP:
            continue
        pot_start = st.get("potStartBb")
        actions = st.get("actions") or []
        if pot_start is None or not actions:
            continue
        out += _scan(street, float(pot_start), actions, "db",
                     actor_key="actor", amount_key="amountBb",
                     hero_flag=False)
    return out


def _scan(street: str, pot_start: float, actions: list[dict], source: str,
          *, actor_key: str, amount_key: str, hero_flag: bool) -> list[Obs]:
    """逐动作扫描：Hero 下注之后的对手，按这次下注的价格算一个观测。"""
    out: list[Obs] = []
    totals: dict[str, float] = {}

    def _actor(a: dict) -> str:
        return str(a.get(actor_key) or a.get("actor") or a.get("seat_id") or "")

    def _is_hero(a: dict) -> bool:
        return bool(a.get("is_hero")) or _actor(a) == "hero"

    i = 0
    while i < len(actions):
        a = actions[i]
        act = str(a.get("action") or "").lower()
        amount = a.get(amount_key, a.get("amount"))
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        actor = _actor(a)

        if act in ("bet", "raise", "allin") and _is_hero(a) and amount:
            level_before = max(totals.values(), default=0.0)
            hero_prior = totals.get(actor, 0.0)
            size = float(amount) - hero_prior
            pot_before = pot_start + sum(totals.values())
            if size > 0 and pot_before > 0:
                price = size / (pot_before + 2.0 * size)
                # 往后看：下一次加注之前，每个对手算一个观测
                j = i + 1
                while j < len(actions):
                    b = actions[j]
                    b_act = str(b.get("action") or "").lower()
                    b_actor = _actor(b)
                    b_amount = b.get(amount_key, b.get("amount"))
                    try:
                        b_amount = float(b_amount) if b_amount is not None else None
                    except (TypeError, ValueError):
                        b_amount = None
                    if b_actor == actor and b_act in ("bet", "raise", "allin"):
                        break                       # Hero 又加注，价格换了
                    if b_act == "fold":
                        out.append(Obs(price, 1, street, source))
                    elif b_act in ("call", "bet", "raise", "allin"):
                        # 短码 all-in 跟注的价格不同（他只需跟到自己的筹码），
                        # 混进来会把「价格」系统性压低 → 直接剔除这个观测。
                        owed = float(amount) - totals.get(b_actor, 0.0)
                        paid = (b_amount or 0.0) - totals.get(b_actor, 0.0)
                        if b_amount is not None and 0 < paid < owed - 1e-6:
                            pass
                        else:
                            out.append(Obs(price, 0, street, source))
                    elif b_act in ("check",):
                        pass
                    if b_actor:
                        if b_amount is not None and b_act in ("bet", "raise", "allin", "call"):
                            totals[b_actor] = max(totals.get(b_actor, 0.0), b_amount)
                    if b_act in ("bet", "raise", "allin") and b_amount and b_actor != actor:
                        break                       # 对手加注，后面价格变了
                    j += 1
            # 记录 hero 的下注
        if actor and amount is not None and act in ("bet", "raise", "allin", "call"):
            totals[actor] = max(totals.get(actor, 0.0), amount)
        i += 1
    return out


def _local_hands() -> list[dict]:
    hands = []
    for p in glob.glob(str(ROOT / "hands" / "*.json")):
        hands += json.loads(Path(p).read_text(encoding="utf-8"))
    for sub in ("pending", "discarded"):
        for p in glob.glob(str(ROOT / "data" / "hand_outbox" / sub / "*.json")):
            try:
                hands.append(json.loads(Path(p).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    return hands


def _env_from(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _hand_key(hand: dict) -> str:
    """本地与 DB 可能是同一手牌：用「手牌 + 牌面 + 行动线」做去重键。"""
    streets = hand.get("streets") or []
    acts = tuple((s.get("street"), tuple(
        (a.get("is_hero") or a.get("actor") == "hero", a.get("action"),
         a.get("amount", a.get("amountBb")))
        for a in s.get("actions") or [])) for s in streets)
    return repr((hand.get("hero_cards"), hand.get("board"), acts))


def load_all() -> list[Obs]:
    """本地 + DB，按「手牌 + 牌面 + 行动线」去重（两边可能导入过同一手）。"""
    seen: set[str] = set()
    obs: list[Obs] = []
    for hand in _local_hands() + load_db():
        key = _hand_key(hand)
        if key in seen:
            continue
        seen.add(key)
        obs += _parse_local(hand) if "pot_start" in json.dumps(
            hand.get("streets") or []) else _parse_db(hand)
    return obs


def load_db() -> list[Obs]:
    import pymysql                                # 只在 --source db 时需要
    candidates = [Path("/Users/weiweigod/codes/call-back/.env"),
                  ROOT.parent / "call-back" / ".env"]
    env = next((e for e in (_env_from(p) for p in candidates) if e.get("DB_HOST")), {})
    if not env:
        raise SystemExit("找不到 DB 配置（call-back/.env）")
    conn = pymysql.connect(host=env["DB_HOST"], port=int(env.get("DB_PORT", 3306)),
                           user=env["DB_USER"], password=env["DB_PASSWORD"],
                           database=env["DB_NAME"], connect_timeout=10)
    hands: list[dict] = []
    with conn.cursor() as c:
        for table in ("replay_imported_hands", "review_hands"):
            c.execute(f"SELECT hero_cards, board, streets FROM {table} "
                      "WHERE deleted_at IS NULL AND streets IS NOT NULL")
            for hero, board, streets in c.fetchall():
                try:
                    data = json.loads(streets)
                except (TypeError, ValueError):
                    continue
                hands.append({"hero_cards": hero, "board": board, "streets": data})
    return hands


def model_curve(profile: str, mix: dict[str, float], prices: list[float]) -> list[float]:
    base = strategy_tables.rows("fold_equity", "base_fold", profile=profile)
    gamma = strategy_tables.value("fold_equity", "size_gamma", profile=profile)
    saved = fe.BASE_FOLD, fe.SIZE_GAMMA
    try:
        fe.BASE_FOLD = base            # type: ignore[assignment]
        fe.SIZE_GAMMA = gamma          # type: ignore[assignment]
        out = []
        for p in prices:
            out.append(sum(w * fe.fold_probability(t, p) for t, w in mix.items()))
        return out
    finally:
        fe.BASE_FOLD, fe.SIZE_GAMMA = saved


def _predict(mix: dict[str, float], price: float, base: dict[str, float],
             gamma: float, scale: float) -> float:
    """参考构成下的聚合弃牌率：Σ w_t · min(max(base_t·(p/0.25)^γ·scale))."""
    saved = fe.BASE_FOLD, fe.SIZE_GAMMA
    try:
        fe.BASE_FOLD, fe.SIZE_GAMMA = base, gamma      # type: ignore[assignment]
        return sum(w * fe.fold_probability(t, price, scale) for t, w in mix.items())
    finally:
        fe.BASE_FOLD, fe.SIZE_GAMMA = saved


def fit(obs: list[Obs], profile: str, mix: dict[str, float]) -> dict:
    """网格搜 (scale, γ)：只用两个参数，178 个点足够，别再加自由度。"""
    base = strategy_tables.rows("fold_equity", "base_fold", profile=profile)
    gammas = [round(0.2 + 0.1 * i, 1) for i in range(21)]        # 0.2 ~ 2.2
    scales = [round(0.5 + 0.05 * i, 2) for i in range(31)]      # 0.5 ~ 2.0
    best = None
    grid = []
    for g in gammas:
        for sc in scales:
            loss = 0.0
            for o in obs:
                p = _predict(mix, o.price, base, g, sc)
                p = min(max(p, 1e-4), 1 - 1e-4)
                loss -= o.fold * math.log(p) + (1 - o.fold) * math.log(1 - p)
            loss /= len(obs)
            grid.append((loss, g, sc))
            if best is None or loss < best[0]:
                best = (loss, g, sc)
    grid.sort()
    return {"loss": best[0], "gamma": best[1], "scale": best[2],
            "top": grid[:5], "n": len(obs)}


def _bucket_of(price: float) -> tuple[float, float] | None:
    for lo, hi in BUCKETS:
        if lo <= price < hi:
            return (lo, hi)
    return None


def report(obs: list[Obs]) -> dict:
    rows = []
    for lo, hi in BUCKETS:
        sub = [o for o in obs if lo <= o.price < hi]
        n = len(sub)
        k = sum(o.fold for o in sub)
        mid = sum(o.price for o in sub) / n if n else (lo + hi) / 2
        lo_ci, hi_ci = _wilson(k, n)
        rows.append({"bucket": (lo, hi), "price": mid, "n": n, "folds": k,
                     "rate": (k / n if n else 0.0), "ci": (lo_ci, hi_ci)})
    return {"rows": rows, "total": len(obs), "folds": sum(o.fold for o in obs)}


def _print(result: dict, obs: list[Obs], profiles: list[str]) -> None:
    prices = [r["price"] for r in result["rows"]]
    curves = {(prof, mname): model_curve(prof, mix, prices)
              for prof in profiles for mname, mix in MIXES.items()}
    print(f"\n总观测 {result['total']}（弃 {result['folds']}）："
          f"每个对手算一个点\n")
    head = f"{'价格桶':>12s}{'代表价格':>9s}{'n':>6s}{'他弃':>7s}{'实测':>8s}{'95%CI':>14s}"
    for prof in profiles:
        head += f"{'模型:' + prof:>14s}"
    print(head)
    for r in result["rows"]:
        if r["n"] == 0:
            continue
        lo, hi = r["ci"]
        line = (f"{r['bucket'][0]:.2f}-{r['bucket'][1]:.2f}".rjust(12)
                + f"{r['price']:9.3f}{r['n']:6d}{r['folds']:7d}"
                + f"{r['rate']:8.1%}{f'{lo:.0%}~{hi:.0%}':>14s}")
        idx = result["rows"].index(r)
        for prof in profiles:
            v = curves[(prof, "典型")][idx]
            line += f"{v:14.1%}"
        print(line)
    print("\n模型列 = 参考范围构成（典型）下的预测；空气重的构成会把整条曲线抬高约 "
          f"{abs(curves[(profiles[0], '空气重')][2] - curves[(profiles[0], '典型')][2]):.0%}。"
          if profiles else "")
    # 分街
    print("\n分街：")
    for street in POSTFLOP:
        sub = [o for o in obs if o.street == street]
        if not sub:
            continue
        n = len(sub)
        k = sum(o.fold for o in sub)
        print(f"  {street:6s} n={n:4d} 弃 {k:4d} → {k / n:5.1%} "
              f"（价格中位 {sorted(o.price for o in sub)[n // 2]:.3f}）")


def _svg(result: dict, obs: list[Obs], profiles: list[str]) -> str:
    W, H, PAD = 760, 420, 56
    W_in, H_in = W - 2 * PAD, H - 2 * PAD
    xmin, xmax = 0.10, 0.50
    ymin, ymax = 0.0, 1.0

    def X(p: float) -> float:
        return PAD + (min(max(p, xmin), xmax) - xmin) / (xmax - xmin) * W_in

    def Y(v: float) -> float:
        return PAD + H_in - (v - ymin) / (ymax - ymin) * H_in

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="-apple-system,Segoe UI,sans-serif">',
             f'<rect width="{W}" height="{H}" fill="#fff"/>']
    for i in range(6):
        v = i / 5
        parts.append(f'<line x1="{PAD}" y1="{Y(v):.1f}" x2="{W - PAD}" y2="{Y(v):.1f}" '
                     f'stroke="#eee"/>')
        parts.append(f'<text x="{PAD - 10}" y="{Y(v) + 4:.1f}" font-size="11" '
                     f'fill="#888" text-anchor="end">{v:.0%}</text>')
    for p, label in ((0.143, "1/4"), (0.2, "1/3"), (0.25, "1/2"), (0.286, "2/3"), (0.333, "1"), (0.429, "1.5")):
        if xmin <= p <= xmax:
            parts.append(f'<line x1="{X(p):.1f}" y1="{PAD}" x2="{X(p):.1f}" '
                         f'y2="{H - PAD}" stroke="#f4f4f4"/>')
            parts.append(f'<text x="{X(p):.1f}" y="{H - PAD + 18}" font-size="11" '
                         f'fill="#888" text-anchor="middle">{label}</text>')
    colors = {"hand-tuned": "#888", "sourced": "#d9480f"}
    for prof in profiles:
        mix = MIXES["典型"]
        pts = []
        for i in range(41):
            p = xmin + (xmax - xmin) * i / 40
            pts.append((X(p), Y(model_curve(prof, mix, [p])[0])))
        d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<polyline points="{d}" fill="none" stroke="{colors.get(prof, "#333")}" '
                     f'stroke-width="2" stroke-dasharray="6 4"/>')
        parts.append(f'<text x="{pts[-1][0] - 6:.1f}" y="{pts[-1][1] - 8:.1f}" '
                     f'font-size="11" fill="{colors.get(prof, "#333")}" '
                     f'text-anchor="end">{prof}（模型）</text>')
    for r in result["rows"]:
        if not r["n"]:
            continue
        x = X(r["price"])
        lo, hi = r["ci"]
        parts.append(f'<line x1="{x:.1f}" y1="{Y(lo):.1f}" x2="{x:.1f}" '
                     f'y2="{Y(hi):.1f}" stroke="#1c7ed6" stroke-width="2"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{Y(r["rate"]):.1f}" r="5" '
                     f'fill="#1c7ed6"/>')
        parts.append(f'<text x="{x:.1f}" y="{Y(r["rate"]) - 10:.1f}" font-size="11" '
                     f'fill="#1c7ed6" text-anchor="middle">'
                     f'{r["rate"]:.0%} (n={r["n"]})</text>')
    parts.append(f'<text x="{W / 2:.0f}" y="{H - 12}" font-size="12" fill="#555" '
                 f'text-anchor="middle">价格 = 下注额 / 他跟注后的底池（0.25 = 1/2 池）</text>')
    parts.append("</svg>")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="按价格的实测弃牌曲线（P2 探针）")
    ap.add_argument("--source", choices=["local", "db", "all"], default="local")
    ap.add_argument("--html", default=None, help="写一个含内联 SVG 的 HTML")
    ap.add_argument("--profiles", default="hand-tuned,sourced")
    ap.add_argument("--fit", action="store_true", help="网格拟合 scale 与 γ")
    args = ap.parse_args()

    if args.source == "local":
        obs = [o for h in _local_hands() for o in _parse_local(h)]
    elif args.source == "db":
        obs = [o for h in load_db() for o in _parse_db(h)]
    else:
        obs = load_all()
    if not obs:
        print("没有观测", file=sys.stderr)
        return 2

    result = report(obs)
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    _print(result, obs, profiles)

    if args.fit:
        print("\n== 参数拟合（网格，只用 scale 与 γ 两个自由度）==")
        for prof in profiles:
            got = fit(obs, prof, MIXES["典型"])
            base = 0.0
            for o in obs:
                p0 = _predict(MIXES["典型"], o.price,
                              strategy_tables.rows("fold_equity", "base_fold",
                                                   profile=prof), 1.0, 1.0)
                p0 = min(max(p0, 1e-4), 1 - 1e-4)
                base -= o.fold * math.log(p0) + (1 - o.fold) * math.log(1 - p0)
            base /= len(obs)
            print(f"  {prof:11s} 最优 γ={got['gamma']:.1f} scale={got['scale']:.2f}"
                  f"｜log-loss {got['loss']:.4f}（基线 γ=1.0 scale=1.0：{base:.4f}）")
            print(f"              top5: " + "  ".join(
                f"γ={g:.1f}/s={sc:.2f}→{l:.4f}" for l, g, sc in got["top"]))

    if args.html:
        html = ("<!doctype html><meta charset='utf-8'><title>弃牌曲线探针</title>"
                "<body style='margin:24px;font-family:-apple-system,sans-serif'>"
                f"<h2>实测弃牌率 vs 模型（n={result['total']}，"
                f"弃 {result['folds']}）</h2>" + _svg(result, obs, profiles) +
                "<p style='color:#666;font-size:13px;max-width:760px'>"
                "蓝点 = 实测（含 95% Wilson 区间）；虚线 = 当前策略表在"
                "「典型范围构成」下的预测。每个对手算一个观测，多人池不独立，"
                "CI 偏乐观。</p></body>")
        Path(args.html).write_text(html, encoding="utf-8")
        print(f"\n已写入 {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
