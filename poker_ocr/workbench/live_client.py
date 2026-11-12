"""访问 live 的最小 HTTP 客户端（标准库，不引入 requests）。

只用到 live 新增的只读/控制端点：

    GET  /api/health           就绪探针
    GET  /api/meta             本次运行元数据（几何/源/点击状态/统计）
    GET  /api/preview.jpg      最新已处理帧
    GET  /api/control/state    点击开关
    POST /api/control/click    暂停/恢复自动点击
    GET  /api/snapshot         看板快照（运行页嵌日志/状态用）

控制请求带 `X-Workbench-Token`；token 只存在工作台内存和子进程参数里，
不写日志、不写 manifest（manifest 里会打码）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable


class LiveClientError(RuntimeError):
    pass


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{int(port)}{path}"


def get_json(port: int, path: str, *, timeout: float = 2.0) -> dict:
    try:
        with urllib.request.urlopen(_url(port, path), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LiveClientError(f"{path}: {type(exc).__name__}: {exc}") from exc


def post_json(port: int, path: str, payload: dict, *, token: str | None = None,
              timeout: float = 3.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_url(port, path), data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Workbench-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise LiveClientError(f"{path}: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LiveClientError(f"{path}: {type(exc).__name__}: {exc}") from exc


def get_bytes(port: int, path: str, *, timeout: float = 3.0) -> bytes:
    try:
        with urllib.request.urlopen(_url(port, path), timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise LiveClientError(f"{path}: {type(exc).__name__}: {exc}") from exc


def health(port: int, *, timeout: float = 0.8) -> dict | None:
    try:
        return get_json(port, "/api/health", timeout=timeout)
    except LiveClientError:
        return None


def meta(port: int, *, timeout: float = 1.5) -> dict | None:
    try:
        return get_json(port, "/api/meta", timeout=timeout)
    except LiveClientError:
        return None


def snapshot(port: int, *, timeout: float = 2.0) -> dict | None:
    try:
        return get_json(port, "/api/snapshot", timeout=timeout)
    except LiveClientError:
        return None


def control_state(port: int, *, timeout: float = 1.5) -> dict | None:
    try:
        return get_json(port, "/api/control/state", timeout=timeout)
    except LiveClientError:
        return None


def set_click(port: int, token: str, enabled: bool, *,
              reason: str | None = None, source: str = "workbench",
              timeout: float = 3.0) -> dict:
    payload = {"enabled": bool(enabled), "source": source}
    if reason:
        payload["reason"] = reason
    return post_json(port, "/api/control/click", payload,
                     token=token, timeout=timeout)


def preview(port: int, *, width: int | None = 960, full: bool = False,
            timeout: float = 3.0) -> bytes:
    if full:
        return get_bytes(port, "/api/preview.jpg?full=1", timeout=timeout)
    query = f"?w={int(width)}" if width else ""
    return get_bytes(port, f"/api/preview.jpg{query}", timeout=timeout)


def wait_ready(port: int, *, timeout: float = 20.0, require_frame: bool = True,
               alive: Callable[[], bool] | None = None,
               poll: float = 0.25) -> dict:
    """等 /api/health + 首帧预览（设计 §4.2 第 7 步）。

    返回 `{ok, meta, error, elapsed_s}`；进程提前退出或超时都返回 ok=False，
    调用方把日志尾部展示给用户。
    """
    started = time.monotonic()
    last_error = "等待 live 就绪超时"
    while time.monotonic() - started < max(0.5, timeout):
        if alive is not None and not alive():
            return {"ok": False, "meta": None, "error": "live 进程提前退出",
                    "elapsed_s": round(time.monotonic() - started, 2)}
        try:
            if health(port) is None:
                last_error = "http://127.0.0.1:%d/api/health 还没响应" % port
                time.sleep(poll)
                continue
            data = meta(port)
            if data is None:
                time.sleep(poll)
                continue
            frame = data.get("frame") or {}
            width = int(frame.get("width") or 0)
            if require_frame and width <= 0:
                last_error = "服务已启动，但还没有处理过的帧"
                time.sleep(poll)
                continue
            return {"ok": True, "meta": data, "error": None,
                    "elapsed_s": round(time.monotonic() - started, 2)}
        except Exception as exc:  # noqa: BLE001 — 轮询期间任何异常都只重试
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(poll)
    return {"ok": False, "meta": None, "error": last_error,
            "elapsed_s": round(time.monotonic() - started, 2)}
