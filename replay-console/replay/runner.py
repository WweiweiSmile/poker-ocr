"""任务 worker：DB 队列 + 进程内线程池 + 唯一键断点。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from .config import Settings
from .db import Database
from .decisions import (
    APPROX, EXACT, DecisionPoint, snapshot_from_json,
)
from .engine import RateLimiter, execute_unit
from .repository import (
    DecisionRepository, ExperimentRepository, HandRepository, JobRepository,
    LiveAdviceRepository, ResultRepository, VersionRepository,
)
from .util import json_load
from .versioning import build_settings

log = logging.getLogger("replay.runner")


def _decision_from_row(row: dict) -> DecisionPoint:
    snap = snapshot_from_json(row.get("snapshot_json") or "{}")
    warnings = json_load(row.get("warnings"), []) or []
    return DecisionPoint(
        hand_id=str(row.get("hand_id")),
        street=row.get("street") or snap.street,
        seq=int(row.get("seq") or 0),
        action_index=int(row.get("action_index") or 0),
        snap=snap,
        actual=row.get("actual_action"),
        actual_amount=row.get("actual_amount"),
        pot=float(row.get("pot") or 0.0),
        quality=row.get("quality") or "approx",
        warnings=warnings,
    )


class Runner:
    def __init__(self, settings: Settings, db: Database,
                 wake_event: threading.Event | None = None):
        self.settings = settings
        self.db = db
        self.wake_event = wake_event or threading.Event()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_heartbeat = time.time()
        self.current_job_id: int | None = None
        self.jobs_repo = JobRepository(db, settings.console_user_id)
        self.version_repo = VersionRepository(db, settings.console_user_id)
        self.decision_repo = DecisionRepository(db)
        self.live_advice_repo = LiveAdviceRepository(db)
        self.result_repo = ResultRepository(db)
        self.hand_repo = HandRepository(db, settings.hand_user_id)
        self.experiment_repo = ExperimentRepository(
            db, settings.console_user_id)
        self.limiter = RateLimiter(settings.rate_limit_per_second)

    # ─────────────── 生命周期 ───────────────

    def start(self) -> None:
        if not self.settings.worker_enabled:
            log.info("worker 已禁用（REPLAY_WORKER_ENABLED=0）")
            return
        try:
            recovered = self.jobs_repo.recover_orphans()
            if recovered:
                log.warning("恢复：%d 个中断的 running 任务已标记 failed", recovered)
        except Exception:  # noqa: BLE001
            log.exception("恢复中断任务失败；worker 仍启动，稍后重试")
        self.thread = threading.Thread(target=self._loop, name="replay-worker",
                                       daemon=True)
        self.thread.start()
        log.info("worker 已启动 id=%s workers=%d", self.settings.worker_id,
                 self.settings.unit_workers)

    def stop(self, timeout: float = 8.0) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        log.info("worker 已停止")

    def wake(self) -> None:
        self.wake_event.set()

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def status(self) -> dict:
        return {
            "alive": self.alive or not self.settings.worker_enabled,
            "enabled": self.settings.worker_enabled,
            "workerId": self.settings.worker_id,
            "currentJobId": self.current_job_id,
            "lastHeartbeat": self.last_heartbeat,
            "workers": self.settings.unit_workers,
        }

    # ─────────────── 主循环 ───────────────

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.last_heartbeat = time.time()
            try:
                job = self.jobs_repo.claim_next(self.settings.worker_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("领取任务失败：%s", exc)
                job = None
            if job is None:
                self.wake_event.wait(1.0)
                self.wake_event.clear()
                continue
            self.current_job_id = int(job["id"])
            log.info("开始任务 #%s", self.current_job_id)
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001
                log.exception("任务 #%s 异常退出：%s", job.get("id"), exc)
                try:
                    counts = self.jobs_repo.counts(int(job["id"]))
                    self.jobs_repo.finalize(
                        int(job["id"]), "failed",
                        error_msg=f"{type(exc).__name__}: {exc}"[:500],
                        done=counts["done"], failed=counts["failed"])
                    self._finish_experiment(job, "failed")
                    self._refresh_hand_statuses(int(job["id"]))
                except Exception:  # noqa: BLE001
                    log.exception("标记任务失败时再次异常")
            finally:
                self.current_job_id = None
                self.last_heartbeat = time.time()

    # ─────────────── 单任务 ───────────────

    def _run_job(self, job: dict) -> None:
        job_id = int(job["id"])
        version_ids = [int(v) for v in (json_load(job.get("version_ids"), []) or [])]
        hand_ids = [int(h) for h in (json_load(job.get("hand_ids"), []) or [])]
        repeat = int(job.get("repeat_count") or 1)
        total_units = int(job.get("total_units") or 0)

        versions = {int(v["id"]): v for v in self.version_repo.list(True)}
        selected = [versions[v] for v in version_ids if v in versions]
        missing_versions = [v for v in version_ids if v not in versions]
        if missing_versions:
            self.jobs_repo.finalize(job_id, "failed",
                                    error_msg=f"版本不存在：{missing_versions}",
                                    done=0, failed=0)
            return

        settings_by_version: dict[int, Any] = {}
        settings_error: dict[int, str] = {}
        for v in selected:
            try:
                settings_by_version[int(v["id"])] = build_settings(v)
            except Exception as exc:  # noqa: BLE001
                settings_error[int(v["id"])] = f"{type(exc).__name__}: {exc}"

        config_snapshot = json_load(job.get("config_snapshot"), {}) or {}
        include_quality = set(config_snapshot.get("includeQuality") or [EXACT, APPROX])
        job_hands = self.jobs_repo.hand_progress(job_id)
        pending: list[tuple[DecisionPoint, dict, int, int]] = []
        hand_totals: dict[int, int] = {}
        for hand_id in hand_ids:
            jh = job_hands.get(hand_id)
            if not jh:
                continue
            fingerprint = jh.get("hand_fingerprint") or ""
            decisions = self.decision_repo.for_hand(hand_id, fingerprint)
            if not decisions:
                self.jobs_repo.update_hand_status(
                    job_id, hand_id, "failed",
                    error_msg="找不到创建任务时的决策点快照")
                continue
            decision_rows = [row for row in decisions.values()
                             if (row.get("quality") or APPROX) in include_quality]
            if not decision_rows:
                self.jobs_repo.update_hand_status(
                    job_id, hand_id, "skipped",
                    error_msg="没有符合质量要求的决策点")
                continue
            self.jobs_repo.update_hand_status(job_id, hand_id, "running")
            hand_total = len(decision_rows) * len(selected) * repeat
            hand_totals[hand_id] = hand_total
            # 线上建议上下文（table_stats/opponent_reads）按决策点挂到快照上；
            # 版本 context_mode=snapshot 时 engine 会用它，保证线上/重跑同上下文。
            contexts = self.live_advice_repo.context_for_hand(hand_id)
            for db_row in decision_rows:
                decision = _decision_from_row(db_row)
                decision.context = contexts.get(
                    (decision.street, decision.action_index), {})
                for version in selected:
                    for repeat_index in range(repeat):
                        pending.append((decision, version, repeat_index,
                                        int(db_row["id"])))

        # 唯一键断点：已落库的 unit 直接跳过
        existing = self.result_repo.existing_keys(job_id)
        pending = [u for u in pending
                   if (u[3], int(u[1]["id"]), u[2]) not in existing]

        counts = self.jobs_repo.counts(job_id)
        done = counts["done"]
        failed = counts["failed"]
        if total_units <= 0:
            total_units = len(pending) + done
        lock = threading.Lock()
        cancelled = threading.Event()
        if self.jobs_repo.cancel_requested(job_id):
            cancelled.set()
        if cancelled.is_set():
            self._finish(job_id, "canceled", done, failed)
            self._refresh_hand_statuses(job_id)
            return

        # 逐手已完成数（含断点恢复前的结果），用于运行中的逐手进度
        hand_done: dict[int, int] = {}
        for row in self.jobs_repo.counts_by_hand(job_id):
            hand_done[int(row["hand_id"])] = int(row["total"] or 0)
        last_progress = time.time()
        last_heartbeat = time.time()
        futures: dict[Any, tuple] = {}

        def task(decision: DecisionPoint, version: dict, repeat_index: int,
                 decision_id: int):
            if cancelled.is_set() or self.stop_event.is_set():
                return None
            if int(version["id"]) in settings_error:
                payload = _error_payload(settings_error[int(version["id"])])
                self.result_repo.insert(
                    job_id=job_id, decision_id=decision_id,
                    version_id=int(version["id"]), repeat_index=repeat_index,
                    payload=payload)
                return "error"
            try:
                outcome = execute_unit(
                    decision=decision, version=version,
                    settings=settings_by_version[int(version["id"])],
                    timeout=self.settings.model_timeout,
                    retries=self.settings.transient_retries,
                    limiter=self.limiter,
                    store_prompts=self.settings.store_prompts,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("unit 失败 hand=%s version=%s", decision.hand_id,
                              version.get("id"))
                payload = _error_payload(f"{type(exc).__name__}: {exc}")
                self.result_repo.insert(
                    job_id=job_id, decision_id=decision_id,
                    version_id=int(version["id"]), repeat_index=repeat_index,
                    payload=payload)
                return "error"
            inserted = self.result_repo.insert(
                job_id=job_id, decision_id=decision_id,
                version_id=int(version["id"]), repeat_index=repeat_index,
                payload=outcome.payload)
            return outcome.source if inserted else "existing"

        with ThreadPoolExecutor(max_workers=max(1, self.settings.unit_workers),
                                thread_name_prefix="replay-unit") as pool:
            for decision, version, repeat_index, decision_id in pending:
                futures[pool.submit(task, decision, version, repeat_index,
                                    decision_id)] = (
                    decision, version, repeat_index)
            while futures:
                finished, _ = wait(set(futures), timeout=0.5,
                                   return_when=FIRST_COMPLETED)
                if self.jobs_repo.cancel_requested(job_id):
                    cancelled.set()
                for future in finished:
                    decision, version, repeat_index = futures.pop(future)
                    try:
                        source = future.result()
                    except Exception as exc:  # noqa: BLE001
                        log.exception("unit future 异常：%s", exc)
                        source = "error"
                    with lock:
                        if source:
                            done += 1
                            if source == "error":
                                failed += 1
                            hand_id = int(decision.hand_id or 0)
                            hand_done[hand_id] = hand_done.get(hand_id, 0) + 1
                now = time.time()
                if now - last_progress >= 1.0:
                    try:
                        self.jobs_repo.set_progress(
                            job_id, done=done, failed=failed, total=total_units)
                        for hand_id, value in hand_done.items():
                            self.jobs_repo.update_hand_status(
                                job_id, hand_id,
                                "running" if value < hand_totals.get(hand_id, 0)
                                else "done", done_units=value)
                    except Exception:  # noqa: BLE001
                        log.exception("更新进度失败")
                    last_progress = now
                if now - last_heartbeat >= self.settings.heartbeat_seconds:
                    self.jobs_repo.heartbeat(job_id, self.settings.worker_id)
                    self.last_heartbeat = now
                    last_heartbeat = now

        counts = self.jobs_repo.counts(job_id)
        done = counts["done"]
        failed = counts["failed"]
        if cancelled.is_set() or self.jobs_repo.cancel_requested(job_id):
            status = "canceled"
        elif total_units and done < total_units:
            status = "partial" if done > 0 else "failed"
        elif failed and failed >= max(total_units, 1):
            status = "failed"
        elif failed:
            status = "partial"
        else:
            status = "succeeded"
        error_msg = None
        if status in ("failed", "partial") and done < total_units:
            error_msg = f"完成 {done}/{total_units} 个 unit"
        self._finish(job_id, status, done, failed, error_msg)
        self._refresh_hand_statuses(job_id)

    def _finish_experiment(self, job: dict, status: str) -> None:
        experiment_id = job.get("experiment_id")
        if not experiment_id:
            return
        try:
            self.experiment_repo.set_status(
                int(experiment_id), "done" if status in ("succeeded", "partial")
                else "failed")
        except Exception:  # noqa: BLE001
            log.exception("更新实验状态失败 experiment=%s", experiment_id)

    def _refresh_hand_statuses(self, job_id: int) -> None:
        """以 results 为准刷新逐手状态（断点恢复后也正确）。"""
        try:
            job = self.jobs_repo.get_any(job_id) or {}
            terminal = job.get("status") in ("canceled", "failed", "partial")
            rows = self.jobs_repo.job_hands(job_id)
            for row in rows:
                counts = self.db.one(
                    """SELECT COUNT(*) total,
                              SUM(CASE WHEN r.source='error' THEN 1 ELSE 0 END) failed
                         FROM replay_results r
                         JOIN replay_decisions d ON d.id=r.decision_id
                        WHERE r.job_id=%s AND d.hand_id=%s""",
                    (job_id, row["hand_id"])) or {}
                total = int(counts.get("total") or 0)
                failed = int(counts.get("failed") or 0)
                expected = int(row.get("decision_count") or 0) * max(
                    1, self._job_versions_count(job_id)) * max(
                    1, self._job_repeat(job_id))
                if failed:
                    status = "failed"
                elif total >= expected:
                    status = "done"
                elif total == 0 or terminal:
                    status = "skipped"
                else:
                    status = "running"
                self.jobs_repo.update_hand_status(
                    job_id, int(row["hand_id"]), status, done_units=total)
        except Exception:  # noqa: BLE001
            log.exception("刷新逐手状态失败")

    def _job_versions_count(self, job_id: int) -> int:
        job = self.jobs_repo.get_any(job_id) or {}
        return len(json_load(job.get("version_ids"), []) or [])

    def _job_repeat(self, job_id: int) -> int:
        job = self.jobs_repo.get_any(job_id) or {}
        return int(job.get("repeat_count") or 1)

    def _finish(self, job_id: int, status: str, done: int, failed: int,
                error_msg: str | None = None) -> None:
        self.jobs_repo.finalize(job_id, status, error_msg=error_msg,
                                done=done, failed=failed)
        self._finish_experiment(self.jobs_repo.get_any(job_id) or {}, status)
        log.info("任务 #%s 结束 status=%s done=%s failed=%s",
                 job_id, status, done, failed)


def _error_payload(message: str) -> dict:
    return {
        "option_id": None, "option_kind": None, "option_amount": None,
        "option_label": None, "reason": None, "source": "error",
        "model": None, "duration_ms": 0, "tokens_in": None, "tokens_out": None,
        "bucket": "error", "violation_tags": "[]", "facts_json": "{}",
        "prompt_system": None, "prompt_user": None, "raw_response": None,
        "error_msg": message[:500],
    }
