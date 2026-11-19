#!/usr/bin/env python3
"""翻前 replay 书卡覆盖的旧入口（票 #87）。

等价于 `nv tools/book_coverage.py --streets preflop`；保留这个文件名是为了
不破坏文档与既有操作习惯，统计逻辑只有 `tools/book_coverage.py` 一份。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from book_coverage import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(default_streets="preflop"))
