#!/usr/bin/env python3
"""批量跑 gto-runner。

断电/关机后重跑同一条命令即可：已完成的 job 有 .done，会被跳过。

用法：
    python gto/scripts/run_batch.py --threads 12
    python gto/scripts/run_batch.py --shard mac --threads 6
    python gto/scripts/run_batch.py --parallel 2 --threads 6
    python gto/scripts/run_batch.py --dry-run --limit 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

GTO = Path(__file__).resolve().parents[1]


def safe(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)


def default_runner() -> Path:
    exe = "gto-runner.exe" if os.name == "nt" else "gto-runner"
    return GTO / "runner" / "target" / "release" / exe


def run_one(runner: Path, job_path: Path, out: Path, threads: int, iterations: int | None) -> tuple[str, int, str]:
    cmd = [str(runner), "--job", str(job_path), "--out", str(out), "--threads", str(threads), "--skip-done"]
    if iterations:
        cmd += ["--iterations", str(iterations)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=24 * 3600)
        out_txt = (p.stdout or "") + (p.stderr or "")
        return job_path.stem, p.returncode, out_txt
    except subprocess.TimeoutExpired:
        return job_path.stem, 124, "timeout after 24h"
    except Exception as e:  # noqa: BLE001
        return job_path.stem, 1, repr(e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(GTO / "jobs" / "manifest.json"))
    ap.add_argument("--runner", default=None)
    ap.add_argument("--out", default=str(GTO / "data" / "raw"))
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--shard", choices=["all", "pc", "mac"], default="all")
    ap.add_argument("--iterations", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--spots", default="", help="只跑这些 spot（逗号分隔子串）")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runner = Path(args.runner) if args.runner else default_runner()

    jobs = [j for j in manifest["jobs"] if args.shard == "all" or j["shard"] in ("all", args.shard)]
    if args.spots:
        keys = [x.strip() for x in args.spots.split(",") if x.strip()]
        jobs = [j for j in jobs if any(k in j["spot"] for k in keys)]
    jobs = [j for j in jobs if not (out / f"{safe(j['id'])}.done").exists()]
    if args.limit:
        jobs = jobs[: args.limit]

    if not runner.exists():
        sys.exit(f"runner not found: {runner}\n先编译：cd gto/runner && cargo build --release")

    print(f"pending={len(jobs)}  threads={args.threads}  parallel={args.parallel}  runner={runner}")
    if args.dry_run:
        for j in jobs[:20]:
            print("  ", j["id"])
        return
    if not jobs:
        print("nothing to do")
        return

    log_dir = GTO / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"batch-{time.strftime('%Y%m%d-%H%M%S')}.log"
    lf = log_path.open("a", encoding="utf-8")

    failed: list[str] = []
    done = 0
    t_all = time.time()

    def work(j: dict) -> tuple[str, int, str, int]:
        job_path = base.joinpath(*PurePosixPath(j["path"]).parts)
        attempts = 0
        last = (j["id"], 1, "", 0)
        while attempts <= args.retries:
            attempts += 1
            name, rc, txt = run_one(runner, job_path, out, args.threads, args.iterations or None)
            last = (j["id"], rc, txt, attempts)
            if rc == 0:
                return last
            time.sleep(5 * attempts)
        return last

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
        futs = {ex.submit(work, j): j for j in jobs}
        for fut in as_completed(futs):
            jid, rc, txt, attempts = fut.result()
            done += 1
            stamp = time.strftime("%H:%M:%S")
            status = "ok" if rc == 0 else f"FAIL(rc={rc})"
            print(f"[{stamp}] {done}/{len(jobs)} {status} {jid}" + ("" if attempts == 1 else f" attempts={attempts}"))
            lf.write(f"=== {jid} rc={rc} attempts={attempts}\n{txt}\n")
            lf.flush()
            if rc != 0:
                failed.append(jid)
                (out / f"{safe(jid)}.failed").write_text(txt[-4000:], encoding="utf-8")

    lf.close()
    mins = (time.time() - t_all) / 60.0
    print(f"finished: ok={len(jobs)-len(failed)} failed={len(failed)} elapsed={mins:.1f} min")
    print(f"log: {log_path}")
    if failed:
        print("failed jobs:", ", ".join(failed[:10]) + (" ..." if len(failed) > 10 else ""))


if __name__ == "__main__":
    main()
