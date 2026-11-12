"""工作台 HTTP 层：FastAPI 路由 + 静态页。

薄壳原则：这里只做参数解析、token/Origin 校验和 JSON 序列化；真正的逻辑
都在 `service.WorkbenchService`（可以脱离 HTTP 单测）。

安全（设计 §10）：只绑 127.0.0.1；所有写操作要 `X-Workbench-Token`
（页面加载时注入到 `window.__WB_TOKEN__`）；带 Origin 的写请求必须同源。
"""

from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .service import WorkbenchError, WorkbenchService

WEB_ROOT = Path(__file__).resolve().parent / "static"


def _error_response(exc: WorkbenchError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=exc.status)


def create_app(*, service: WorkbenchService | None = None,
               token: str | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    svc = service or WorkbenchService()
    process_started = time.time()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        try:
            svc.shutdown()
        except Exception:  # noqa: BLE001 — 退出路径不再抛
            pass

    app = FastAPI(title="Poker OCR 工作台", version="0.1",
                  docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.service = svc
    app.state.token = token
    app.state.process_started = process_started

    def _latest_backend_mtime() -> float:
        """工作台/live 的 Python 源码最新修改时间（用于提示「需要重启」）。

        静态页每次请求都从磁盘读，改 app.js 会立即生效；Python 不会热重载。
        不提示的话很容易出现「页面是新的、后端还是旧的」的错觉。
        """
        here = Path(__file__).resolve().parent
        candidates = list(here.glob("*.py"))
        for name in ("live.py", "live_web.py", "profile.py"):
            candidates.append(here.parent / name)
        latest = 0.0
        for path in candidates:
            try:
                latest = max(latest, path.stat().st_mtime)
            except OSError:
                continue
        return latest

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = (request.headers.get("origin") or "").strip()
            host = (request.headers.get("host") or "").strip()
            if origin and urlsplit(origin).netloc != host:
                return JSONResponse(
                    {"ok": False, "error": "跨源写请求被拒绝"}, status_code=403)
        return await call_next(request)

    def require_token(request: Request) -> None:
        if request.headers.get("x-workbench-token") != token:
            raise HTTPException(status_code=403,
                                detail="缺少或错误的 X-Workbench-Token")

    @app.exception_handler(WorkbenchError)
    async def _workbench_error(_request: Request, exc: WorkbenchError):
        return _error_response(exc)

    @app.exception_handler(KeyError)
    async def _key_error(_request: Request, exc: KeyError):
        return JSONResponse({"ok": False, "error": str(exc)},
                            status_code=404)

    # ── 页面 ────────────────────────────────────────────
    @app.get("/")
    def index() -> Response:
        try:
            html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        except OSError:
            return JSONResponse({"ok": False, "error": "静态页缺失"},
                                status_code=500)
        html = html.replace("__WB_TOKEN_VALUE__", token)
        return Response(html, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    @app.get("/app.js")
    def app_js() -> Response:
        return FileResponse(WEB_ROOT / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    def style_css() -> Response:
        return FileResponse(WEB_ROOT / "style.css", media_type="text/css")

    # ── 汇总 ────────────────────────────────────────────
    @app.get("/api/health")
    def health() -> dict:
        latest = _latest_backend_mtime()
        stale = bool(latest and latest > process_started + 2.0)
        return {"ok": True, "root": str(svc.root),
                "workspaces": str(svc.workspaces_dir),
                "runs": str(svc.runs_dir),
                "status": svc.supervisor.status().get("status"),
                "started_at": process_started,
                "code_mtime": latest or None,
                "stale": stale}

    @app.get("/api/profiles")
    def profiles() -> dict:
        return {"profiles": svc.profiles()}

    # ── 工作区 ──────────────────────────────────────────
    @app.get("/api/workspaces")
    def list_workspaces() -> dict:
        return {"workspaces": svc.list_workspaces()}

    @app.post("/api/workspaces", dependencies=[Depends(require_token)])
    def create_workspace(body: dict = Body(...)) -> dict:
        return svc.create_workspace(body)

    @app.get("/api/workspaces/{workspace_id}")
    def get_workspace(workspace_id: str) -> dict:
        return svc.get_workspace(workspace_id)

    @app.put("/api/workspaces/{workspace_id}",
             dependencies=[Depends(require_token)])
    def update_workspace(workspace_id: str, body: dict = Body(...)) -> dict:
        return svc.update_workspace(workspace_id, body)

    @app.delete("/api/workspaces/{workspace_id}",
                dependencies=[Depends(require_token)])
    def delete_workspace(workspace_id: str) -> dict:
        return svc.delete_workspace(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/duplicate",
              dependencies=[Depends(require_token)])
    def duplicate_workspace(workspace_id: str,
                            body: dict = Body(default={})) -> dict:
        return svc.duplicate_workspace(workspace_id,
                                       name=(body or {}).get("name"))

    # ── 体检 / 运行 ─────────────────────────────────────
    @app.get("/api/workspaces/{workspace_id}/preflight")
    def preflight(workspace_id: str,
                  probe: bool = Query(True)) -> dict:
        return svc.preflight(workspace_id, probe=probe)

    @app.post("/api/workspaces/{workspace_id}/start",
              dependencies=[Depends(require_token)])
    def start(workspace_id: str, body: dict = Body(default={})) -> dict:
        return svc.start(workspace_id,
                         force=bool((body or {}).get("force")),
                         probe=bool((body or {}).get("probe", True)))

    @app.post("/api/workspaces/{workspace_id}/stop",
              dependencies=[Depends(require_token)])
    def stop(workspace_id: str) -> dict:
        return svc.stop(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/restart",
              dependencies=[Depends(require_token)])
    def restart(workspace_id: str) -> dict:
        return svc.restart(workspace_id)

    @app.get("/api/workspaces/{workspace_id}/status")
    def status(workspace_id: str) -> dict:
        return svc.status(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/click",
              dependencies=[Depends(require_token)])
    def click(workspace_id: str, body: dict = Body(...)) -> dict:
        if not isinstance(body.get("enabled"), bool):
            raise HTTPException(status_code=400,
                                detail="需要 {\"enabled\": true|false}")
        return svc.click(workspace_id, body["enabled"])

    @app.post("/api/workspaces/{workspace_id}/pin-window",
              dependencies=[Depends(require_token)])
    def pin_window(workspace_id: str) -> dict:
        return svc.pin_window(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/configure-window",
              dependencies=[Depends(require_token)])
    def configure_window(workspace_id: str,
                         body: dict = Body(default={})) -> dict:
        return svc.configure_window(workspace_id,
                                    target=(body or {}).get("target"),
                                    spec=(body or {}).get("spec"),
                                    position=(body or {}).get("position"))

    # ── 预览 / 布局 ─────────────────────────────────────
    @app.get("/api/workspaces/{workspace_id}/preview.jpg")
    def preview(workspace_id: str, w: int = Query(960, ge=0, le=4096),
                full: bool = Query(False)) -> Response:
        data, _meta = svc.preview_jpeg(workspace_id, width=w, full=full)
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/workspaces/{workspace_id}/frame.jpg")
    def frame(workspace_id: str) -> Response:
        data, meta = svc.frame_jpeg(workspace_id)
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store",
                                 "X-Frame-Size": "x".join(
                                     str(v) for v in (meta.get("size") or [])),
                                 "X-Frame-Source": str(meta.get("source"))})

    @app.get("/api/workspaces/{workspace_id}/slots")
    def slots(workspace_id: str, probe: bool = Query(False)) -> dict:
        return svc.slots(workspace_id, probe=probe)

    @app.get("/api/workspaces/{workspace_id}/diagnostics")
    def diagnostics(workspace_id: str) -> dict:
        return svc.diagnostics(workspace_id)

    @app.get("/api/workspaces/{workspace_id}/runs")
    def runs(workspace_id: str, limit: int = Query(50, ge=1, le=500)) -> dict:
        return {"runs": svc.runs(workspace_id, limit=limit)}

    @app.get("/api/runs/{run_id}/manifest")
    def manifest(run_id: str) -> dict:
        return svc.manifest(run_id)

    # ── 标定 ────────────────────────────────────────────
    @app.get("/api/workspaces/{workspace_id}/calibration")
    def calibration(workspace_id: str) -> dict:
        return svc.calibration(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/calibration/edit",
              dependencies=[Depends(require_token)])
    def calibration_edit(workspace_id: str, body: dict = Body(...)) -> dict:
        key = str(body.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="缺少 key")
        return svc.calibration_edit(workspace_id, key, body.get("slot"))

    @app.post("/api/workspaces/{workspace_id}/calibration/undo",
              dependencies=[Depends(require_token)])
    def calibration_undo(workspace_id: str) -> dict:
        return svc.calibration_undo(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/calibration/redo",
              dependencies=[Depends(require_token)])
    def calibration_redo(workspace_id: str) -> dict:
        return svc.calibration_redo(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/calibration/reset",
              dependencies=[Depends(require_token)])
    def calibration_reset(workspace_id: str) -> dict:
        return svc.calibration_reset(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/calibration/trial",
              dependencies=[Depends(require_token)])
    def calibration_trial(workspace_id: str, body: dict = Body(...)) -> dict:
        key = str(body.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="缺少 key")
        return svc.calibration_trial(workspace_id, key, body.get("slot"))

    @app.post("/api/workspaces/{workspace_id}/calibration/apply",
              dependencies=[Depends(require_token)])
    def calibration_apply(workspace_id: str) -> dict:
        return svc.calibration_apply(workspace_id)

    @app.post("/api/workspaces/{workspace_id}/overlay",
              dependencies=[Depends(require_token)])
    def overlay(workspace_id: str, body: dict = Body(...)) -> dict:
        return svc.overlay_control(
            workspace_id,
            enabled=bool(body.get("enabled")),
            groups=body.get("groups"),
            selected=body.get("selected"))

    @app.post("/api/workspaces/{workspace_id}/overlay/update",
              dependencies=[Depends(require_token)])
    def overlay_update(workspace_id: str,
                       body: dict = Body(default={})) -> dict:
        return svc.overlay_update(workspace_id,
                                  groups=(body or {}).get("groups"),
                                  selected=(body or {}).get("selected"))

    @app.post("/api/workspaces/{workspace_id}/calibration/restore",
              dependencies=[Depends(require_token)])
    def calibration_restore(workspace_id: str,
                            body: dict = Body(...)) -> dict:
        return svc.restore_backup(workspace_id,
                                  str(body.get("backup") or ""))

    # ── 源 ──────────────────────────────────────────────
    @app.get("/api/sources/windows")
    def windows() -> dict:
        return svc.windows()

    @app.get("/api/sources/cameras")
    def cameras() -> dict:
        return svc.cameras()

    @app.post("/api/sources/probe", dependencies=[Depends(require_token)])
    def probe(body: dict = Body(...)) -> dict:
        return svc.probe_source(str(body.get("spec") or ""))

    # 静态资源（只暴露这一层，别把项目根目录挂上去）
    if WEB_ROOT.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_ROOT)),
                  name="static")

    return app


def default_app() -> FastAPI:
    """uvicorn 入口（`poker_ocr.workbench.app:default_app`）。"""
    return create_app()
