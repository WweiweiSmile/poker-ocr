"""独立 worker 进程（可选）。

API 进程默认自带 worker；要把长任务和 API 分开时：

    ../.venv/bin/python -m replay.worker

API 侧设 `REPLAY_WORKER_ENABLED=0`，两边共用同一个 MySQL 即可。
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
for path in (str(ROOT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from replay.api import setup_logging  # noqa: E402
from replay.config import Settings  # noqa: E402
from replay.db import Database  # noqa: E402
from replay.runner import Runner  # noqa: E402

log = logging.getLogger("replay.worker")


def main() -> int:
    settings = Settings.from_env()
    setup_logging(settings)
    db = Database(settings)
    try:
        db.migrate()
    except Exception:  # noqa: BLE001
        log.exception("迁移失败；worker 仍尝试启动")
    runner = Runner(settings, db)
    runner.start()
    stop = False

    def on_signal(signum, frame):
        nonlocal stop
        log.info("收到信号 %s，准备退出", signum)
        stop = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    while not stop:
        time.sleep(0.5)
    runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
