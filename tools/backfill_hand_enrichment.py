#!/usr/bin/env python3
"""给已入库手牌补 timeline / flags / review_score（不调模型）。

    nv tools/backfill_hand_enrichment.py --dry-run
    nv tools/backfill_hand_enrichment.py

只写 replay_imported_hands 的 timeline_json/flags/review_score，不碰 review_hands。
旧手牌没有 prompt 时 flags 会标 PROMPT_MISSING；这是事实，不是补造 prompt。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "replay-console"
for path in (str(ROOT), str(CONSOLE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from replay.config import Settings  # noqa: E402
from replay.db import Database  # noqa: E402
from replay.flags import compute_flags  # noqa: E402
from replay.service import ReplayService  # noqa: E402
from replay.timeline import build_timeline  # noqa: E402
from replay.util import json_load  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="补手牌 timeline/flags")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=100000)
    args = ap.parse_args()

    settings = Settings.from_env()
    db = Database(settings)
    service = ReplayService(settings, db)
    rows = service.import_repo.list_rows(limit=args.limit)
    ok = failed = 0
    for row in rows:
        hand_id = int(row["id"])
        try:
            row = dict(row)
            for key in ("streets", "table_roster", "villains", "shows"):
                if isinstance(row.get(key), str):
                    row[key] = json_load(row[key], []) or []
            analysis = service.analyze(row)
            advices = service.live_advice_repo.list_for_hand(hand_id)
            if not advices:
                payload = service.import_repo.get_payload(hand_id)
                value = payload.get("advices") if isinstance(payload, dict) else None
                if isinstance(value, list):
                    advices = [a for a in value if isinstance(a, dict)]
            timeline = build_timeline(row, analysis, advices)
            mask, flags, score = compute_flags(row, analysis, advices)
            if args.dry_run:
                print(f"#{hand_id} {analysis.quality} "
                      f"frames={len(timeline.get('frames') or [])} "
                      f"flags={mask} score={score}")
            else:
                service.hand_repo.update_enrichment(
                    hand_id, timeline=timeline, flag_mask=mask,
                    flags=flags, review_score=score,
                    record_schema=str(row.get("record_schema") or "hand.v2"))
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"#{hand_id} 失败：{type(exc).__name__}: {exc}")
    print(f"完成：ok={ok} failed={failed} dryRun={args.dry_run}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
