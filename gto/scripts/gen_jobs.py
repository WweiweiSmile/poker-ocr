#!/usr/bin/env python3
"""生成 GTO 求解任务（jobs + manifest）。

用法：
    python gto/scripts/gen_jobs.py --limit 1 --iterations 20      # 试点
    python gto/scripts/gen_jobs.py                                # 全量
    python gto/scripts/gen_jobs.py --split 25                     # 25% 分给 Mac

产物：
    gto/jobs/<spot>/<flop>__<sizes>.json
    gto/jobs/manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

GTO = Path(__file__).resolve().parents[1]
ROOT = GTO.parent


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(obj) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def safe(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", default=str(GTO / "specs"))
    ap.add_argument("--out", default=str(GTO / "jobs"))
    ap.add_argument("--sizes", default="three_span", help="sizes.json 里的 profile")
    ap.add_argument("--iterations", type=int, default=250)
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 个牌面（试点）")
    ap.add_argument("--spots", default="", help="只生成这些 spot（逗号分隔的子串，如 BTN,CO）")
    ap.add_argument("--split", type=int, default=0, help="分给 Mac 的百分比（0=全部 pc）")
    args = ap.parse_args()

    specs = Path(args.specs)
    out = Path(args.out)
    spots = json.loads((specs / "spots.json").read_text(encoding="utf-8"))["spots"]
    boards = json.loads((specs / "boards.json").read_text(encoding="utf-8"))["boards"]
    sizes_all = json.loads((specs / "sizes.json").read_text(encoding="utf-8"))
    if args.sizes not in sizes_all:
        sys.exit(f"unknown sizes profile {args.sizes!r}; choose from {list(sizes_all)}")
    sizes = sizes_all[args.sizes]

    if args.spots:
        keys = [x.strip() for x in args.spots.split(",") if x.strip()]
        spots = [s for s in spots if any(k in s["id"] for k in keys)]
    if args.limit:
        boards = boards[: args.limit]

    manifest = []
    for spot in spots:
        for b in boards:
            flop = b["flop"]
            job = {
                "id": f"{spot['id']}__{flop}__{sizes['id']}",
                "flop": flop,
                "oop_range": spot["oop_range"],
                "ip_range": spot["ip_range"],
                "starting_pot": int(spot["starting_pot"]),
                "effective_stack": int(spot["effective_stack"]),
                "bet_sizes": sizes["bets"],
                "raise_sizes": sizes["raise"],
                "iterations": args.iterations,
                "add_allin_threshold": 0.0,
                "force_allin_threshold": 0.15,
                "merging_threshold": 0.0,
                "rake_rate": 0.0,
                "rake_cap": 0.0,
            }
            h = sha(job)
            job["config_hash"] = h
            # 分片：按 hash 确定性分配，不依赖顺序
            shard = "mac" if args.split and int(h[:8], 16) % 100 < args.split else "pc"
            rel = Path(spot["id"]) / f"{safe(flop)}__{sizes['id']}.json"
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
            manifest.append({
                "id": job["id"],
                "path": rel.as_posix(),
                "config_hash": h,
                "shard": shard,
                "spot": spot["id"],
                "board": flop,
                "sizes": sizes["id"],
                "iterations": args.iterations,
            })

    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "sizes": sizes["id"],
            "iterations": args.iterations,
            "jobs": manifest,
        }, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    mac = sum(1 for m in manifest if m["shard"] == "mac")
    print(f"jobs={len(manifest)}  mac={mac}  pc={len(manifest)-mac}  sizes={sizes['id']}  iters={args.iterations}")
    print(f"manifest: {out / 'manifest.json'}")


if __name__ == "__main__":
    main()
