#!/usr/bin/env python
"""把分批复跑的结果合成一份，供 tools/ab_diff.py 对比（#38 §15.3）。

复跑是按 run 分批跑的（每批一个 runs/<run>/live.states.jsonl），ab_diff 要一份
合起来的结果。键与 ab_diff.key_of 同源：同一手同一街同一底池同一需跟只留一条，
先出现的胜出（重复点只可能来自同一 run 的重复帧）。

    nv tools/ab_merge.py logs/ab/*.w1.json -o logs/ab/merged-w1.json
    nv tools/ab_merge.py logs/ab/*.w1.json --witness-only -o logs/ab/merged-w1.screen.json

--witness-only 是 §15.3 的自动筛选口径：只留「本点至少有一条摊牌证据行」的
决策点（渲染层为空就等于没有在局对手落在覆盖窗口里）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.ab_diff import key_of  # noqa: E402


def merge(paths: list[Path], *, witness_only: bool = False) -> list[dict]:
    """多份复跑结果 → 去重后的一份（保持首次出现顺序）。"""
    out: dict[tuple, dict] = {}
    for path in paths:
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            if not row.get("street"):
                continue
            if witness_only and not row.get("witness"):
                continue
            out.setdefault(key_of(row), row)
    return list(out.values())


def main() -> int:
    ap = argparse.ArgumentParser(description="合成多次复跑结果")
    ap.add_argument("inputs", nargs="+", help="各 run 的 --json 输出")
    ap.add_argument("-o", "--out", required=True, help="合成后的 JSON 路径")
    ap.add_argument("--witness-only", action="store_true",
                    help="只留带摊牌证据行的决策点（§15.3 自动筛选）")
    args = ap.parse_args()

    rows = merge([Path(p) for p in args.inputs], witness_only=args.witness_only)
    if not rows:
        print("没有可合成的决策点（检查输入是不是 --json 输出）", file=sys.stderr)
        return 2
    Path(args.out).write_text(
        json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    witnessed = sum(1 for r in rows if r.get("witness"))
    print(f"{len(args.inputs)} 份 → {len(rows)} 个决策点"
          f"（带证据行 {witnessed}）→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
