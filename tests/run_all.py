#!/usr/bin/env python3
"""跑全部测试。

    .venv/bin/python tests/run_all.py

分层，从不需要图到需要图：
    test_positions  位置推导（纯逻辑，最快）
    test_coach      翻后建议引擎（纯逻辑，不需要图）
    test_dealer     庄位检测（合成图，不依赖照片）
    test_roi_reader ROI 读取器（合成图 + 假 OCR 引擎，不依赖照片和模型）
    test_roi_dedup  ROI 去重（合成图，不依赖照片和模型）
    test_parse      字段解析（真实 OCR 原文变体）
    test_frame_state 桌面状态 + 变化指纹（手工拼 RoiFrame）
    test_extract    端到端提取（需要 table.jpg）
    test_api        HTTP 接口（需要 table.jpg + fastapi）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SUITES = ["test_positions.py", "test_coach.py", "test_dealer.py", "test_roi_reader.py",
          "test_roi_dedup.py", "test_parse.py", "test_frame_state.py",
          "test_extract.py", "test_api.py"]


def main() -> int:
    failed = []
    for name in SUITES:
        print(f"── {name} " + "─" * max(0, 50 - len(name)), flush=True)
        proc = subprocess.run([sys.executable, str(HERE / name)])
        if proc.returncode != 0:
            failed.append(name)

    print()
    if failed:
        print(f"❌ 失败的套件：{', '.join(failed)}")
        return 1
    print(f"✅ {len(SUITES)} 个套件全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
