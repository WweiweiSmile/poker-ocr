#!/usr/bin/env python3
"""013 回填：源记录物化成 canonical，并给决策点补上七个桶（票 #52）。

    nv tools/013_backfill_canonical.py --dry-run     # 只读源库，报预计工作量
    nv tools/013_backfill_canonical.py               # 先迁移，再物化 + 补料
    nv tools/013_backfill_canonical.py --list-groups 20   # 顺便列出多源组明细
    nv tools/013_backfill_canonical.py --skip-decisions

幂等：源记录按 (source, source_hand_id) 去重，决策点按 id 覆盖写；重复跑
不会新增行。review_hands 只读：模块用 ReadOnlyGuard 拦住 db.execute 级别的
写，真正的保证仍是只读库账号。先跑 --dry-run 确认源数，再实跑；实跑结束
打印对账行。--dry-run 连迁移都不跑，免得「只看看」先把库结构改了。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "replay-console"
for path in (str(ROOT), str(CONSOLE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from poker_ocr.opponent_profile import load_profile_store  # noqa: E402
from replay.backfill import group_lines, report_lines, run_backfill  # noqa: E402
from replay.config import Settings  # noqa: E402
from replay.db import Database  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="013 回填 canonical 与决策桶")
    ap.add_argument("--dry-run", action="store_true",
                    help="只读源库、报预计工作量，不写库")
    ap.add_argument("--limit", type=int, default=None,
                    help="最多处理多少条源记录（调试用）")
    ap.add_argument("--no-migrate", action="store_true",
                    help="跳过 db.migrate()（表结构已就绪时）")
    ap.add_argument("--skip-decisions", action="store_true",
                    help="只物化 canonical，不碰 replay_decisions")
    ap.add_argument("--profiles", default=None,
                    help="画像库路径；默认 data/opponents/profiles.json")
    ap.add_argument("--no-profiles", action="store_true",
                    help="不读画像库，opponent_profile 一律回填「未知」")
    ap.add_argument("--list-groups", type=int, default=0, metavar="N",
                    help="回填后列出前 N 个多源组（人工抽查 20 组用）")
    args = ap.parse_args()

    # 缺快照 / 单条失败的逐条日志只在这里跑，不配 handler 就全丢了
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s %(message)s")

    settings = Settings.from_env()
    db = Database(settings)
    try:
        if args.dry_run:
            print("dry-run：跳过迁移与写库")
        elif not args.no_migrate:
            applied = db.migrate()
            print("迁移：" + (", ".join(applied) if applied else "已是最新（无新增）"))

        store = None
        if not args.no_profiles:
            store = load_profile_store(args.profiles)
            if store.load_error:
                print(f"画像库不可用（{store.load_error}），opponent_profile 将全为「未知」")

        stats = run_backfill(db, profile_store=store, dry_run=args.dry_run,
                             limit=args.limit,
                             skip_decisions=args.skip_decisions)
        for line in report_lines(stats):
            print(line)
        if args.list_groups:
            for line in group_lines(db, limit=args.list_groups):
                print(line)

        failed = list(stats.get("failed") or ())
        failed += list((stats.get("decisions") or {}).get("failed") or ())
        if failed:
            print(f"有 {len(failed)} 条失败，见上；可重跑（幂等）")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
