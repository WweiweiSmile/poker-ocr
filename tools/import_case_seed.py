#!/usr/bin/env python3
"""把 tools/ab_set.json 导成 seed-2026-11 案例集（票 #55 §10 步骤 3–4）。

    nv tools/import_case_seed.py --dry-run    # 只算计划，不写库
    nv tools/import_case_seed.py              # 建 spec + 案例集 + 案例
    nv tools/import_case_seed.py --set-name seed-2026-11

幂等：spec 按 (name, revision) 去重，内容没变就复用；案例按
(case_set, canonical_id, decision_key) 去重。重复跑不会新增行。

expect=n/a / status≠ready 的条目只建 spec、不建 case（设计 §10 步骤 3）；
案例集冻结后重复跑会把案例记进 skipped 并提示建新版本。手牌 uid 解不出
canonical / 决策点时进 skipped 明细，绝不静默丢。
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
from replay.service import ReplayService  # noqa: E402


def _report_lines(report: dict) -> list[str]:
    lines: list[str] = []
    case_set = report.get("set") or {}
    if case_set.get("wouldCreate"):
        lines.append(f"案例集：{report['setName']} v1（dry-run，尚未创建）")
    else:
        frozen = "已冻结" if case_set.get("frozen") else "未冻结"
        lines.append(f"案例集：{case_set.get('name')} v{case_set.get('revision')}"
                     f"（id={case_set.get('id')}，{frozen}）")

    specs = report["specs"]
    lines.append(
        f"spec：新建 {len(specs['created'])}，复用 {len(specs['reused'])}，"
        f"待建 {len(specs['wouldCreate'])}")
    cases = report["cases"]
    lines.append(
        f"案例：新建 {len(cases['created'])}，已存在 {len(cases['existing'])}，"
        f"待建 {len(cases['wouldCreate'])}，失败 {len(cases['skipped'])}")
    for item in cases["skipped"]:
        code = item.get("code") or ""
        lines.append(f"  ✗ {item['seedId']} {code} {item.get('reason')}".rstrip())
    planned = report.get("cases", {}).get("skippedPlan") or []
    if planned:
        lines.append(f"按设计不建案例（只留 spec）{len(planned)} 条：")
        for item in planned:
            lines.append(f"  · {item['seedId']}：{item['reason']}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="导入 ab_set.json 为 seed 案例集")
    ap.add_argument("--path", default=None, help="ab_set.json 路径（默认仓库内）")
    ap.add_argument("--set-name", default=None, help="案例集名（默认 seed-2026-11）")
    ap.add_argument("--dry-run", action="store_true", help="只算计划，不写库")
    ap.add_argument("--no-migrate", action="store_true",
                    help="跳过 db.migrate()（表结构已就绪时）")
    args = ap.parse_args()

    settings = Settings.from_env()
    db = Database(settings)
    try:
        if not args.dry_run and not args.no_migrate:
            applied = db.migrate()
            print("迁移：" + (", ".join(applied) if applied else "已是最新（无新增）"))
        service = ReplayService(settings, db, runner=None)
        report = service.seed_case_sets({
            "path": args.path,
            "setName": args.set_name,
            "dryRun": args.dry_run,
        })
        for line in _report_lines(report):
            print(line)
        if report["cases"]["skipped"]:
            print("有案例没能落库，见上面的 ✗；补齐导入 / 回填后可重跑（幂等）")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
