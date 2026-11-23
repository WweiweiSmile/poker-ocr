#!/usr/bin/env python3
"""建议卡「数据边界」段：纯函数（`advice_bounds.js`）的 node 单元测试。

    nv tests/test_advice_bounds.py

口径来自 #111：

  · 建议卡在既有字段之后逐行渲染 `facts.warnings`，行数 = 条数、顺序照抄；
  · 列表为空 / 缺字段时整段不出现，卡片输出与改动前逐字相同；
  · 换行原样留在同一个条目里（CSS `white-space: pre-wrap` 渲染成多行）；
  · warning 文本走与 `adviceHtml` 相同的 `esc()`，不得注入 HTML。

前端没有构建链、也没有浏览器测试，所以渲染逻辑做成无 DOM 依赖的纯 JS 模块，
由 node 直接断言；`app.js` 只负责把 `facts.warnings` 与自己的 `esc` 传进去。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "poker_ocr" / "workbench" / "static"
DRIVER = ROOT / "tests" / "js" / "advice_bounds_test.js"
MODULE = STATIC / "advice_bounds.js"
APP = STATIC / "app.js"
INDEX = STATIC / "index.html"
STYLE = STATIC / "style.css"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("… 没有 node，跳过前端纯函数测试")
        return 0
    check(MODULE.exists(), "advice_bounds.js 应存在（建议卡「数据边界」段的纯函数模块）")
    if not MODULE.exists():
        return report()
    check(DRIVER.exists(), "tests/js/advice_bounds_test.js 应存在")
    if not DRIVER.exists():
        return report()

    proc = subprocess.run([node, str(DRIVER)], capture_output=True, text=True, timeout=60)
    check(proc.returncode == 0, f"node 驱动应通过；stderr:\n{proc.stderr[-2000:]}")
    check("ok advice_bounds" in proc.stdout, "驱动应打印成功标记")

    index = INDEX.read_text(encoding="utf-8")
    at_module = index.find("/static/advice_bounds.js")
    check(at_module >= 0, "index.html 应注册 /static/advice_bounds.js")
    check(0 <= at_module < index.find("/app.js"),
          "advice_bounds.js 必须先于 app.js 加载，否则 adviceHtml 会读到 undefined")

    app = APP.read_text(encoding="utf-8")
    check("AdviceBounds.warningsHtml(facts.warnings, esc)" in app,
          "app.js 必须把 facts.warnings 与自己的 esc 交给 AdviceBounds.warningsHtml")

    style = STYLE.read_text(encoding="utf-8")
    check(".advice-bounds-list li" in style and "white-space: pre-wrap" in style,
          "style.css 应让 warning 里的换行按原样渲染（white-space: pre-wrap）")

    return report()


def report() -> int:
    if FAILS:
        print(f"✗ {len(FAILS)} 项不通过：")
        for item in FAILS:
            print(f"  · {item}")
        return 1
    print("✓ 建议卡「数据边界」段：空/单条/多条/换行/转义/逐字不变 全通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
