"""FastAPI 应用：/api/* + 构建后的 SPA。"""

from __future__ import annotations

import logging
import logging.handlers
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Database
from .errors import ApiError
from .runner import Runner
from .schemas import (
    HandDelete, ImportHands, MarkPatch, RunCreate, RunPreview, VersionClone,
    VersionCreate, VersionMetaPatch,
)
from .service import ReplayService

log = logging.getLogger("replay.api")


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger("replay")
    if getattr(root, "_replay_configured", False):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S")
    try:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            settings.log_file, maxBytes=10 * 1024 * 1024, backupCount=3,
            encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        pass
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    root._replay_configured = True  # type: ignore[attr-defined]


def create_app(settings: Settings | None = None,
               db: Database | None = None,
               start_worker: bool | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings)
    db = db or Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_migrate:
            try:
                applied = db.migrate()
                if applied:
                    log.info("已应用迁移：%s", ", ".join(applied))
            except Exception:  # noqa: BLE001
                log.exception("迁移失败；服务仍启动，健康检查会显示 DB 状态")
        try:
            warmed = db.prewarm(min(settings.db_pool_size, 4))
            if warmed:
                log.info("已预热 %d 条 MySQL 连接", warmed)
        except Exception:  # noqa: BLE001
            log.exception("连接池预热失败")
        if settings.retention_days > 0:
            try:
                from .repository import ResultRepository
                cleared = ResultRepository(db).cleanup_prompts(settings.retention_days)
                if cleared:
                    log.info("按保留策略清理了 %d 条旧 prompt/raw", cleared)
            except Exception:  # noqa: BLE001
                log.exception("清理旧 prompt 失败")
        runner = None
        run_worker = settings.worker_enabled if start_worker is None else start_worker
        service = ReplayService(settings, db, runner=None)
        if run_worker:
            runner = Runner(settings, db)
            service.runner = runner
            runner.start()
        app.state.settings = settings
        app.state.db = db
        app.state.service = service
        app.state.runner = runner
        log.info("replay console 启动：db=%s/%s worker=%s",
                 settings.db_host, settings.db_name, bool(runner))
        yield
        if runner is not None:
            runner.stop()
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    app = FastAPI(
        title="手牌复跑控制台 API",
        version="1.0.0",
        description="选牌 → 重跑 → 逐街对比多版本 AI → 收藏 → 一键全部重跑。",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("请求异常 %s %s", request.method, request.url.path)
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        if not request.url.path.startswith("/assets"):
            log.info("%s %s %s %sms", request.method, request.url.path,
                     response.status_code, elapsed)
        response.headers["X-Replay-Duration-Ms"] = str(elapsed)
        return response

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if settings.token:
            host = request.client.host if request.client else ""
            local = host in ("127.0.0.1", "::1", "localhost", "testclient")
            token = request.headers.get("authorization", "")
            if (not local) and token != f"Bearer {settings.token}":
                return JSONResponse(
                    status_code=401,
                    content={"error": {"code": "UNAUTHORIZED",
                                       "message": "需要 Authorization: Bearer <REPLAY_TOKEN>"}})
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数不合法",
                "detail": exc.errors(),
            }})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR",
                               "message": f"{type(exc).__name__}: {exc}"}})

    def service(request: Request) -> ReplayService:
        return request.app.state.service

    # ─────────────── meta / health ───────────────

    @app.get("/api/meta")
    def get_meta(request: Request):
        return service(request).meta()

    @app.get("/api/health")
    def get_health(request: Request):
        return service(request).health()

    # ─────────────── hands ───────────────

    @app.get("/api/hands")
    def list_hands(request: Request):
        return service(request).list_hands(dict(request.query_params))

    # 静态路径必须放在 /api/hands/{hand_id} 之前；否则 "import" 会被当成 int
    @app.post("/api/hands/import")
    def import_hands(body: ImportHands, request: Request):
        return service(request).import_hands(
            body.model_dump(exclude_unset=True))

    @app.post("/api/hands/delete")
    def delete_hands(body: HandDelete, request: Request):
        return service(request).delete_hands(
            body.model_dump(exclude_unset=True))

    @app.get("/api/imports")
    def list_imports(request: Request):
        return service(request).list_imports(dict(request.query_params))

    @app.get("/api/hands/{hand_id}")
    def get_hand(hand_id: int, request: Request):
        return service(request).get_hand(hand_id)

    @app.delete("/api/hands/{hand_id}")
    def delete_hand(hand_id: int, request: Request):
        return service(request).delete_hand(hand_id)

    @app.get("/api/hands/{hand_id}/comparison")
    def get_hand_comparison(hand_id: int, request: Request):
        return service(request).hand_comparison(hand_id, dict(request.query_params))

    @app.patch("/api/hands/{hand_id}/mark")
    def patch_mark(hand_id: int, body: MarkPatch, request: Request):
        return service(request).patch_mark(
            hand_id, body.model_dump(exclude_unset=True))

    # ─────────────── versions ───────────────

    @app.get("/api/versions")
    def list_versions(request: Request):
        include = str(request.query_params.get("includeArchived", "0")).lower() in (
            "1", "true", "yes")
        return {"items": service(request).list_versions(include)}

    @app.post("/api/versions")
    def create_version(body: VersionCreate, request: Request):
        return service(request).create_version(body.model_dump(exclude_unset=True))

    @app.post("/api/versions/import-env")
    def import_env(request: Request):
        return service(request).import_env()

    @app.post("/api/versions/{version_id}/clone")
    def clone_version(version_id: int, body: VersionClone, request: Request):
        return service(request).clone_version(
            version_id, body.model_dump(exclude_unset=True))

    @app.patch("/api/versions/{version_id}")
    def update_version(version_id: int, body: VersionMetaPatch, request: Request):
        return service(request).update_version(
            version_id, body.model_dump(exclude_unset=True))

    # ─────────────── runs ───────────────

    @app.post("/api/runs/preview")
    def preview_run(body: RunPreview, request: Request):
        return service(request).preview_run(body.model_dump())

    @app.post("/api/runs")
    def create_run(body: RunCreate, request: Request):
        return service(request).create_run(body.model_dump())

    @app.get("/api/runs")
    def list_runs(request: Request):
        return service(request).list_runs(dict(request.query_params))

    @app.get("/api/runs/{job_id}")
    def get_run(job_id: int, request: Request):
        return service(request).get_run(job_id)

    @app.get("/api/runs/{job_id}/results")
    def get_run_results(job_id: int, request: Request):
        return service(request).job_results(job_id, dict(request.query_params))

    @app.post("/api/runs/{job_id}/cancel")
    def cancel_run(job_id: int, request: Request):
        return service(request).cancel_run(job_id)

    @app.post("/api/runs/{job_id}/retry-failed")
    def retry_failed(job_id: int, request: Request):
        return service(request).retry_failed(job_id)

    # ─────────────── export ───────────────

    @app.get("/api/export")
    def export(request: Request):
        fmt, filename, body = service(request).export(dict(request.query_params))
        media = "text/csv; charset=utf-8" if fmt == "csv" else "text/markdown; charset=utf-8"
        return Response(
            content=body, media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ─────────────── SPA ───────────────

    dist = Path(settings.static_dir)
    index = dist / "index.html"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    def root():
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({
            "app": "poker replay console",
            "status": "backend running",
            "docs": "/docs",
            "frontend": "replay_web/dist 尚未构建；开发态请运行 cd replay_web && pnpm dev",
        })

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/") or path.startswith("docs"):
            raise ApiError("NOT_FOUND", f"/{path} 不存在", 404)
        target = dist / path
        if target.is_file():
            return FileResponse(target)
        if index.is_file():
            return FileResponse(index)
        raise ApiError("NOT_FOUND", f"/{path} 不存在", 404)

    return app


# uvicorn replay.replay_api:app 的兼容入口在项目根 replay_api.py
