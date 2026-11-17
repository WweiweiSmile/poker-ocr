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

from . import range_ab
from .config import Settings
from .db import Database
from .errors import ApiError
from .run_worker import RunWorker
from .runner import Runner
from .schemas import (
    CaseCreate, CaseHandCases, CaseSample, CaseSeed, CaseSetCasePatch,
    CaseSetCases, CaseSetCreate, CaseSetPatch,
    DecisionMark, ExperimentCreate, ExperimentOutcome, ExperimentRun,
    HandDelete, HandSetCreate, HandSetItems, HandSetPatch, ImportHands,
    IngestHands, MarkPatch, PairReviewPatch, RunCreate, RunPreview,
    SpecPatch, SpecWrite, VersionClone, VersionCreate, VersionMetaPatch,
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


def _export_media(fmt: str) -> str:
    return ("text/csv; charset=utf-8" if fmt == "csv"
            else "text/markdown; charset=utf-8")


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
        score_worker = None
        run_worker = settings.worker_enabled if start_worker is None else start_worker
        service = ReplayService(settings, db, runner=None)
        if run_worker:
            runner = Runner(settings, db)
            service.runner = runner
            runner.start()
            score_worker = RunWorker(settings, db)
            service.run_worker = score_worker
            score_worker.start()
        app.state.settings = settings
        app.state.db = db
        app.state.service = service
        app.state.runner = runner
        app.state.score_worker = score_worker
        log.info("replay console 启动：db=%s/%s worker=%s",
                 settings.db_host, settings.db_name, bool(runner))
        yield
        if runner is not None:
            runner.stop()
        if score_worker is not None:
            score_worker.stop()
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

    # ─────────────── 范围模型 A/B（只读，不碰 DB）───────────────

    @app.get("/api/range-ab")
    def get_range_ab():
        return range_ab.load()

    # ─────────────── hands ───────────────

    @app.get("/api/hands")
    def list_hands(request: Request):
        return service(request).list_hands(dict(request.query_params))

    # 静态路径必须放在 /api/hands/{hand_id} 之前；否则 "import" 会被当成 int
    @app.post("/api/hands/import")
    def import_hands(body: ImportHands, request: Request):
        return service(request).import_hands(
            body.model_dump(exclude_unset=True))

    @app.post("/api/hands/ingest")
    def ingest_hands(body: IngestHands, request: Request):
        return service(request).ingest_hands(
            body.model_dump(exclude_unset=True))

    @app.post("/api/experiments")
    def create_experiment(body: ExperimentCreate, request: Request):
        return service(request).create_experiment(
            body.model_dump(exclude_unset=True))

    @app.get("/api/experiments")
    def list_experiments(request: Request):
        return service(request).list_experiments()

    @app.get("/api/experiments/{experiment_id}")
    def get_experiment(experiment_id: int, request: Request):
        return service(request).get_experiment(experiment_id)

    @app.post("/api/experiments/{experiment_id}/run")
    def run_experiment(experiment_id: int, body: ExperimentRun,
                       request: Request):
        return service(request).run_experiment(
            experiment_id, body.model_dump(exclude_unset=True))

    @app.get("/api/experiments/{experiment_id}/results")
    def experiment_results(experiment_id: int, request: Request):
        split = (request.query_params.get("split") or "").strip() or None
        return service(request).experiment_results(experiment_id, split=split)

    @app.post("/api/hands/delete")
    def delete_hands(body: HandDelete, request: Request):
        return service(request).delete_hands(
            body.model_dump(exclude_unset=True))

    @app.post("/api/cases")
    def create_case(body: CaseCreate, request: Request):
        return service(request).create_case(
            body.model_dump(exclude_unset=True))

    # ─────────────── 案例集（#55） ───────────────
    # /seed 必须注册在 /{set_id} 之前，否则 "seed" 会被当成路径参数。

    @app.post("/api/case-sets/seed")
    def seed_case_sets(body: CaseSeed, request: Request):
        return service(request).seed_case_sets(
            body.model_dump(exclude_unset=True))

    @app.get("/api/case-sets")
    def list_case_sets(request: Request):
        return service(request).list_case_sets(dict(request.query_params))

    @app.post("/api/case-sets")
    def create_case_set(body: CaseSetCreate, request: Request):
        return service(request).create_case_set(
            body.model_dump(exclude_unset=True))

    @app.get("/api/case-sets/{set_id}")
    def get_case_set(set_id: int, request: Request):
        return service(request).get_case_set(set_id)

    @app.patch("/api/case-sets/{set_id}")
    def patch_case_set(set_id: int, body: CaseSetPatch, request: Request):
        return service(request).patch_case_set(
            set_id, body.model_dump(exclude_unset=True))

    @app.post("/api/case-sets/{set_id}/freeze")
    def freeze_case_set(set_id: int, request: Request):
        return service(request).freeze_case_set(set_id)

    @app.post("/api/case-sets/{set_id}/revisions")
    def create_case_set_revision(set_id: int, body: CaseSetPatch,
                                 request: Request):
        return service(request).create_case_set_revision(
            set_id, body.model_dump(exclude_unset=True))

    @app.post("/api/case-sets/{set_id}/cases")
    def add_case_set_cases(set_id: int, body: CaseSetCases, request: Request):
        return service(request).add_case_set_cases(
            set_id, body.model_dump(exclude_unset=True))

    @app.patch("/api/case-sets/{set_id}/cases/{case_id}")
    def patch_case_set_case(set_id: int, case_id: int,
                            body: CaseSetCasePatch, request: Request):
        return service(request).patch_case_set_case(
            set_id, case_id, body.model_dump(exclude_unset=True))

    @app.delete("/api/case-sets/{set_id}/cases/{case_id}")
    def delete_case_set_case(set_id: int, case_id: int, request: Request):
        return service(request).delete_case_set_case(set_id, case_id)

    @app.post("/api/case-sets/{set_id}/hand-cases")
    def build_hand_cases(set_id: int, body: CaseHandCases, request: Request):
        return service(request).build_hand_cases(
            set_id, body.model_dump(exclude_unset=True))

    @app.post("/api/case-sets/{set_id}/sample")
    def sample_case_set_cases(set_id: int, body: CaseSample,
                              request: Request):
        return service(request).sample_case_set_cases(
            set_id, body.model_dump(exclude_unset=True))

    @app.get("/api/case-sets/{set_id}/candidates")
    def case_candidates(set_id: int, request: Request):
        return service(request).case_candidates(
            set_id, dict(request.query_params))

    @app.get("/api/imports")
    def list_imports(request: Request):
        return service(request).list_imports(dict(request.query_params))

    @app.get("/api/hands/{hand_id}")
    def get_hand(hand_id: int, request: Request):
        return service(request).get_hand(hand_id)

    @app.get("/api/hands/{hand_id}/replay")
    def get_hand_replay(hand_id: int, request: Request):
        return service(request).hand_replay(hand_id)

    @app.get("/api/hands/{hand_id}/advices/{advice_id}/prompt")
    def get_hand_prompt(hand_id: int, advice_id: int, request: Request):
        return service(request).hand_prompt(hand_id, advice_id)

    @app.get("/api/hand-sets")
    def list_hand_sets(request: Request):
        return service(request).list_hand_sets()

    @app.post("/api/hand-sets")
    def create_hand_set(body: HandSetCreate, request: Request):
        return service(request).create_hand_set(
            body.model_dump(exclude_unset=True))

    @app.get("/api/hand-sets/{set_id}")
    def get_hand_set(set_id: int, request: Request):
        return service(request).get_hand_set(set_id)

    @app.patch("/api/hand-sets/{set_id}")
    def patch_hand_set(set_id: int, body: HandSetPatch, request: Request):
        return service(request).patch_hand_set(
            set_id, body.model_dump(exclude_unset=True))

    @app.post("/api/hand-sets/{set_id}/items")
    def add_hand_set_items(set_id: int, body: HandSetItems, request: Request):
        return service(request).add_hand_set_items(
            set_id, body.model_dump(exclude_unset=True))

    @app.delete("/api/hand-sets/{set_id}/items")
    def remove_hand_set_items(set_id: int, body: HandDelete, request: Request):
        return service(request).remove_hand_set_items(
            set_id, body.model_dump(exclude_unset=True))

    @app.post("/api/hand-sets/{set_id}/freeze")
    def freeze_hand_set(set_id: int, request: Request):
        return service(request).freeze_hand_set(set_id)

    # ─────────────── 期望库（spot spec） ───────────────
    # /meta 必须注册在 /{spec_id} 之前，否则 "meta" 会被当成路径参数。

    @app.get("/api/specs/meta")
    def spec_meta(request: Request):
        return service(request).spec_meta()

    @app.get("/api/specs")
    def list_specs(request: Request):
        return service(request).list_specs(dict(request.query_params))

    @app.post("/api/specs")
    def create_spec(body: SpecWrite, request: Request):
        return service(request).create_spec(
            body.model_dump(by_alias=True, exclude_unset=True))

    @app.get("/api/specs/{spec_id}")
    def get_spec(spec_id: int, request: Request):
        return service(request).get_spec(spec_id)

    @app.post("/api/specs/{spec_id}/revisions")
    def create_spec_revision(spec_id: int, body: SpecWrite, request: Request):
        return service(request).create_spec_revision(
            spec_id, body.model_dump(by_alias=True, exclude_unset=True))

    @app.patch("/api/specs/{spec_id}")
    def patch_spec(spec_id: int, body: SpecPatch, request: Request):
        return service(request).patch_spec(
            spec_id, body.model_dump(exclude_unset=True))

    @app.delete("/api/specs/{spec_id}")
    def delete_spec(spec_id: int, request: Request):
        return service(request).delete_spec_revision(spec_id)

    @app.get("/api/specs/{spec_id}/history")
    def spec_history(spec_id: int, request: Request):
        return service(request).spec_history(spec_id)

    @app.get("/api/specs/{spec_id}/diff")
    def spec_diff(spec_id: int, request: Request,
                  against: int | None = None):
        return service(request).spec_diff(spec_id, against)

    @app.get("/api/experiments/{experiment_id}/pairs")
    def experiment_pairs(experiment_id: int, request: Request):
        return service(request).experiment_pairs(
            experiment_id, dict(request.query_params))

    @app.post("/api/experiments/{experiment_id}/pairs/{decision_id}/review")
    def review_pair(experiment_id: int, decision_id: int,
                    body: PairReviewPatch, request: Request):
        return service(request).review_pair(
            experiment_id, decision_id, body.model_dump(exclude_unset=True))

    @app.post("/api/experiments/{experiment_id}/outcome")
    def experiment_outcome(experiment_id: int, body: ExperimentOutcome,
                           request: Request):
        return service(request).experiment_outcome(
            experiment_id, body.model_dump(exclude_unset=True))

    @app.delete("/api/hands/{hand_id}")
    def delete_hand(hand_id: int, request: Request):
        return service(request).delete_hand(hand_id)

    @app.get("/api/hands/{hand_id}/comparison")
    def get_hand_comparison(hand_id: int, request: Request):
        return service(request).hand_comparison(hand_id, dict(request.query_params))

    @app.patch("/api/decisions/{decision_id}/mark")
    def mark_decision(decision_id: int, body: DecisionMark, request: Request):
        return service(request).mark_decision(
            decision_id, body.model_dump(exclude_unset=True))

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

    # ─────────────── 评分运行（#57） ───────────────
    # 与旧 job 链路 /api/runs 分开命名，互不影响。

    @app.get("/api/score-runs/meta")
    def score_run_meta(request: Request):
        return service(request).score_run_meta()

    @app.post("/api/score-runs/estimate")
    def estimate_score_run(body: dict, request: Request):
        return service(request).estimate_score_run(body)

    @app.get("/api/score-runs")
    def list_score_runs(request: Request):
        return service(request).list_score_runs(dict(request.query_params))

    @app.post("/api/score-runs")
    def create_score_run(body: dict, request: Request):
        return service(request).create_score_run(body)

    @app.get("/api/score-runs/{run_id}")
    def get_score_run(run_id: int, request: Request):
        return service(request).get_score_run(run_id)

    @app.post("/api/score-runs/{run_id}/cancel")
    def cancel_score_run(run_id: int, request: Request):
        return service(request).cancel_score_run(run_id)

    @app.get("/api/score-runs/{run_id}/hotspots")
    def score_run_hotspots(run_id: int, request: Request):
        return service(request).score_run_hotspots(run_id)

    @app.get("/api/score-runs/{run_id}/items")
    def score_run_items(run_id: int, request: Request):
        return service(request).score_run_items(
            run_id, dict(request.query_params))

    @app.get("/api/score-runs/{run_id}/items/{case_id}/{repeat_index}")
    def score_run_item(run_id: int, case_id: int, repeat_index: int,
                       request: Request):
        return service(request).score_run_item(run_id, case_id, repeat_index)

    @app.post("/api/score-runs/{run_id}/items/{case_id}/override")
    def save_score_run_override(run_id: int, case_id: int, body: dict,
                                request: Request):
        return service(request).save_score_run_override(run_id, case_id, body)

    @app.delete("/api/score-runs/{run_id}/items/{case_id}/override")
    def delete_score_run_override(run_id: int, case_id: int,
                                  request: Request, body: dict | None = None):
        return service(request).delete_score_run_override(
            run_id, case_id, body or {})

    @app.post(
        "/api/score-runs/{run_id}/items/{case_id}/{repeat_index}/rejudge")
    def rejudge_score_run_item(run_id: int, case_id: int, repeat_index: int,
                               request: Request):
        return service(request).rejudge_score_run_item(
            run_id, case_id, repeat_index)

    # ─────────────── 版本对照（#60） ───────────────

    @app.get("/api/score-runs/{run_id}/compare/{other_id}")
    def compare_score_runs(run_id: int, other_id: int, request: Request):
        return service(request).compare_score_runs(
            run_id, other_id, dict(request.query_params))

    @app.get("/api/score-runs/{run_id}/compare/{other_id}/export")
    def export_score_run_comparison(run_id: int, other_id: int,
                                    request: Request):
        fmt, filename, body = service(request).export_score_run_comparison(
            run_id, other_id, dict(request.query_params))
        return Response(
            content=body, media_type=_export_media(fmt),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ─────────────── export ───────────────

    @app.get("/api/export")
    def export(request: Request):
        fmt, filename, body = service(request).export(dict(request.query_params))
        return Response(
            content=body, media_type=_export_media(fmt),
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
