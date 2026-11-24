#!/usr/bin/env python3
"""牌桌面板「N 人在局」：纯函数（`table_meta.js`）的 node 单元测试。

    nv tests/test_table_meta.py

口径来自 #121：

  · 优先读 `players_not_folded`（还没弃牌的人数，含 hero）；
  · 只有缺这个键的老记录才回落到 `players_in_hand`——旧口径显示，绝不覆盖新键；
  · 两个都缺 ⇒ 「—」，与改动前的 `st.players_in_hand ?? '—'` 一致；
  · `players_in_hand` 本身不动：换手判定（hand_track 的 IN_HAND_JUMP）靠它跳变。

前端没有构建链、也没有浏览器测试，所以渲染逻辑做成无 DOM 依赖的纯 JS 模块，
由 node 直接断言；`app.js` 只负责把街道名与 state 传进去，并把人数字段纳入变化指纹。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "poker_ocr" / "workbench" / "static"
DRIVER = ROOT / "tests" / "js" / "table_meta_test.js"
MODULE = STATIC / "table_meta.js"
APP = STATIC / "app.js"
INDEX = STATIC / "index.html"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("… 没有 node，跳过前端纯函数测试")
        return 0
    check(MODULE.exists(), "table_meta.js 应存在（面板「N 人在局」的纯函数模块）")
    if not MODULE.exists():
        return report()
    check(DRIVER.exists(), "tests/js/table_meta_test.js 应存在")
    if not DRIVER.exists():
        return report()

    proc = subprocess.run([node, str(DRIVER)], capture_output=True, text=True, timeout=60)
    check(proc.returncode == 0, f"node 驱动应通过；stderr:\n{proc.stderr[-2000:]}")
    check("ok table_meta" in proc.stdout, "驱动应打印成功标记")

    index = INDEX.read_text(encoding="utf-8")
    at_module = index.find("/static/table_meta.js")
    check(at_module >= 0, "index.html 应注册 /static/table_meta.js")
    check(0 <= at_module < index.find("/app.js"),
          "table_meta.js 必须先于 app.js 加载，否则面板会读到 undefined")

    app = APP.read_text(encoding="utf-8")
    check("TableMeta.inHandLine(tblStreet(street), st)" in app,
          "app.js 必须把街道名与 state 交给 TableMeta.inHandLine")
    check("st.players_in_hand ?? '—'" not in app,
          "app.js 不应再自己拼 players_in_hand 的人数文案")

    return report()


def report() -> int:
    if FAILS:
        print(f"✗ {len(FAILS)} 项不通过：")
        for item in FAILS:
            print(f"  · {item}")
        return 1
    print("✓ 牌桌面板「N 人在局」：优先新键/回落旧键/缺省「—」/0 有效/接线 全通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
