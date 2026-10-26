#!/usr/bin/env python3
"""查一手牌在一个解里的根节点策略（冒烟/验收用）。

用法：
    python gto/scripts/query.py --flop 5c8c8d --hand AhKh
    python gto/scripts/query.py --flop 5c8c8d --hand AhKh --spot SRP_CO_vs_BB_100bb_app
    python gto/scripts/query.py --flop 5c8c8d --hand AhKh --node vs_b33
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
from pathlib import Path

GTO = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(GTO / "data" / "gto.sqlite"))
    ap.add_argument("--flop", required=True)
    ap.add_argument("--hand", required=True)
    ap.add_argument("--spot", default=None)
    ap.add_argument("--node", default="root")
    ap.add_argument("--turn", default="")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    sql = "SELECT solve_id, job_id FROM solves WHERE flop=?"
    params: list = [args.flop]
    if args.spot:
        sql += " AND job_id LIKE ?"
        params.append(f"{args.spot}%")
    row = conn.execute(sql, params).fetchone()
    if not row:
        raise SystemExit(f"no solve for flop={args.flop} spot={args.spot}")
    solve_id, job_id = row
    print(f"job: {job_id}")

    h = conn.execute(
        "SELECT idx, w_oop, w_ip FROM hands WHERE solve_id=? AND combo=?", (solve_id, args.hand)
    ).fetchone()
    if not h:
        raise SystemExit(f"{args.hand} not in range of this solve")
    idx, w_oop, w_ip = h
    n = conn.execute("SELECT COUNT(*) FROM hands WHERE solve_id=?", (solve_id,)).fetchone()[0]
    print(f"hand {args.hand}: idx={idx} w_oop={w_oop:.3f} w_ip={w_ip:.3f}")

    nd = conn.execute(
        "SELECT actions, range_mix, strategy, ev, pot, actor FROM nodes "
        "WHERE solve_id=? AND street=? AND turn=? AND key=?",
        (solve_id, "flop" if not args.turn else "turn", args.turn, args.node),
    ).fetchone()
    if not nd:
        raise SystemExit(f"node {args.node!r} not found")
    actions = json.loads(nd[0])
    mix = json.loads(nd[1])
    strat = nd[2]
    ev = struct.unpack(f"<{len(nd[3]) // 4}i", nd[3])
    print(f"node: pot={nd[4]} chip  actor={nd[5]}  range_mix={[round(m, 3) for m in mix]}")
    for k, a in enumerate(actions):
        f = strat[k * n + idx]
        e = ev[k * n + idx] / 100.0
        print(f"  {a['id']:>10s}  amount={a['amount']:>6d}  freq={f:>3d}%  ev={e:+.2f}BB")


if __name__ == "__main__":
    main()
