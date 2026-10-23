#!/usr/bin/env python3
"""手牌复跑控制台入口。

    # 开发 / 本机运行（前端构建后）
    ../.venv/bin/python replay_api.py
    # 或
    ../.venv/bin/python -m uvicorn replay_api:app --host 127.0.0.1 --port 8010 --reload
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for path in (str(ROOT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from replay.api import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = app.state.settings
    uvicorn.run(app, host=settings.host, port=settings.port,
                log_level="info")
