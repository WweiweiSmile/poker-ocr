"""工作台命令行入口。

    nv -m poker_ocr.workbench
    nv -m poker_ocr.workbench --port 8030 --open
    nv -m poker_ocr.workbench --workspaces workspaces --runs runs

只能绑定回环地址（默认 127.0.0.1）；页面上的写操作要 token，token 由服务
注入到 HTML 里，不出现在命令行/日志。
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app
from .service import WorkbenchService
from .store import PROJECT_ROOT


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="poker_ocr.workbench",
        description="本地工作台：工作区 / 体检 / 启动 live / 预览 / 标定 / 浮层")
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址（只允许回环，默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8030,
                    help="工作台端口（默认 8030）")
    ap.add_argument("--workspaces", type=Path,
                    default=PROJECT_ROOT / "workspaces",
                    help="工作区目录（默认 workspaces/）")
    ap.add_argument("--runs", type=Path, default=PROJECT_ROOT / "runs",
                    help="运行产物目录（默认 runs/）")
    ap.add_argument("--python", default=sys.executable,
                    help="启动 live 用的解释器（默认当前解释器）")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    ap.add_argument("--log-level", default="info",
                    choices=("critical", "error", "warning", "info", "debug"))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[工作台] 只支持绑定回环地址（127.0.0.1）：不做远程访问",
              file=sys.stderr)
        return 2
    service = WorkbenchService(
        root=PROJECT_ROOT,
        workspaces_dir=args.workspaces,
        runs_dir=args.runs,
        python=args.python,
        log=lambda msg: print(msg, flush=True))
    app = create_app(service=service)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[工作台] {url}（只绑本机；Ctrl-C 结束）", flush=True)
    print(f"[工作台] 工作区 {args.workspaces}", flush=True)
    print(f"[工作台] 运行产物 {args.runs}", flush=True)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port,
                log_level=args.log_level, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
