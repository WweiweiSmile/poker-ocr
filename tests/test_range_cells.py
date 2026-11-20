#!/usr/bin/env python3
"""范围推导热图的三类零权重格：纯函数（`range_cells.js`）的 node 单元测试。

    nv tests/test_range_cells.py

口径来自 #32 的 agent brief：

  · 任一视图 169 格三分类互斥且覆盖全集（有份额 / 零但先验含 / 零且先验不含）；
  · 「先验未包含」与「本步被删」文案分开，先验视图不出「已被删」；
  · 删减图例附带「另有 M 格先验未包含（不计入 N）」。

前端没有构建链、也没有浏览器测试，所以这一层逻辑做成无 DOM 依赖的纯 JS 模块，
由 node 直接断言；渲染侧只消费 `classify()` 的结果。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "range_cells_test.js"
MODULE = ROOT / "poker_ocr" / "workbench" / "static" / "range_cells.js"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("… 没有 node，跳过前端纯函数测试")
        return 0
    check(MODULE.exists(), "range_cells.js 应存在（热图分类的纯函数模块）")
    if not MODULE.exists():
        return report()
    proc = subprocess.run([node, str(DRIVER)], capture_output=True, text=True, timeout=60)
    check(proc.returncode == 0, f"node 驱动应通过；stderr:\n{proc.stderr[-2000:]}")
    check("ok range_cells" in proc.stdout, "驱动应打印成功标记")
    return report()


def report() -> int:
    if FAILS:
        print(f"✗ {len(FAILS)} 项不通过：")
        for item in FAILS:
            print(f"  · {item}")
        return 1
    print("✓ 范围热图三类零权重格：分类/文案/图例 全通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
