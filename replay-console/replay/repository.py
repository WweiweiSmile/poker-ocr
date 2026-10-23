"""数据仓储：review_hands 只读 + replay_* 读写。"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from pymysql.err import IntegrityError

from .db import Database
from .decisions import HandAnalysis
from .errors import ApiError
from .util import iso, json_dumps, json_load, median, now_cn

log = logging.getLogger("replay.repo")


class HandRepository:
    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.hand_user_id = user_id

    def _where(self) -> tuple[str, tuple]:
        if self.hand_user_id > 0:
            return " AND h.user_id = %s", (self.hand_user_id,)
        return "", ()

    def list_rows(self, *, limit: int = 10000) -> list[dict]:
        where, params = self._where()
        sql = f"""
            SELECT h.*,
                   m.favorite AS mark_favorite,
                   m.tags AS mark_tags,
                   m.status AS mark_status,
                   m.note AS mark_note
              FROM review_hands h
              LEFT JOIN replay_marks m
                ON m.user_id = %s AND m.hand_id = h.id
             WHERE h.deleted_at IS NULL{where}
             ORDER BY h.created_at DESC, h.id DESC
             LIMIT %s
        """
        return self.db.query(sql, (0,) + params + (limit,))

    def get_row(self, hand_id: int) -> dict | None:
        where, params = self._where()
        sql = f"""
            SELECT h.*,
                   m.favorite AS mark_favorite,
                   m.tags AS mark_tags,
                   m.status AS mark_status,
                   m.note AS mark_note
              FROM review_hands h
              LEFT JOIN replay_marks m
                ON m.user_id = %s AND m.hand_id = h.id
             WHERE h.id = %s AND h.deleted_at IS NULL{where}
             LIMIT 1
        """
        return self.db.one(sql, (0, hand_id) + params)

    def get_by_ids(self, hand_ids: Iterable[int]) -> list[dict]:
        ids = [int(x) for x in hand_ids]
        if not ids:
            return []
        where, params = self._where()
        placeholders = ",".join(["%s"] * len(ids))
        sql = f"""
            SELECT h.*,
                   m.favorite AS mark_favorite,
                   m.tags AS mark_tags,
                   m.status AS mark_status,
                   m.note AS mark_note
              FROM review_hands h
              LEFT JOIN replay_marks m
                ON m.user_id = %s AND m.hand_id = h.id
             WHERE h.id IN ({placeholders}) AND h.deleted_at IS NULL{where}
        """
        return self.db.query(sql, (0, *ids, *params))

    def count(self) -> int:
        where, params = self._where()
        sql = f"SELECT COUNT(*) n FROM review_hands h WHERE h.deleted_at IS NULL{where}"
        return int(self.db.scalar(sql, params) or 0)

    def favorite_count(self) -> int:
        where, params = self._where()
        sql = f"""
            SELECT COUNT(*) n FROM review_hands h
              JOIN replay_marks m ON m.user_id = %s AND m.hand_id = h.id
             WHERE h.deleted_at IS NULL AND m.favorite = 1{where}
        """
        return int(self.db.scalar(sql, (0,) + params) or 0)


class MarkRepository:
    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def get(self, hand_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_marks WHERE user_id=%s AND hand_id=%s",
            (self.user_id, hand_id))

    def upsert(self, hand_id: int, *, favorite: bool | None = None,
               tags: list[str] | None = None, status: str | None = None,
               note: str | None = None) -> dict:
        current = self.get(hand_id)
        if current is None:
            self.db.execute(
                """INSERT INTO replay_marks (user_id, hand_id, favorite, tags, status, note)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (self.user_id, hand_id, 1 if favorite else 0,
                 json_dumps(tags or []), status or "todo", note),
            )
        else:
            sets: list[str] = []
            params: list[Any] = []
            if favorite is not None:
                sets.append("favorite=%s")
                params.append(1 if favorite else 0)
            if tags is not None:
                sets.append("tags=%s")
                params.append(json_dumps(tags))
            if status is not None:
                sets.append("status=%s")
                params.append(status)
            if note is not None:
                sets.append("note=%s")
                params.append(note)
            if sets:
                params += [self.user_id, hand_id]
                self.db.execute(
                    f"UPDATE replay_marks SET {', '.join(sets)} "
                    "WHERE user_id=%s AND hand_id=%s", tuple(params))
        row = self.get(hand_id)
        return self.public(row or {}, hand_id)

    def public(self, row: dict, hand_id: int) -> dict:
        return {
            "handId": hand_id,
            "favorite": bool(row.get("favorite")),
            "tags": json_load(row.get("tags"), []) or [],
            "status": row.get("status") or "todo",
            "note": row.get("note") or "",
            "updatedAt": iso(row.get("updated_at")),
        }

    def favorite_hand_ids(self) -> list[int]:
        rows = self.db.query(
            "SELECT hand_id FROM replay_marks WHERE user_id=%s AND favorite=1",
            (self.user_id,))
        return [int(r["hand_id"]) for r in rows]


class VersionRepository:
    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def list(self, include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM replay_versions WHERE user_id=%s"
        params: list[Any] = [self.user_id]
        if not include_archived:
            sql += " AND archived=0"
        sql += " ORDER BY archived ASC, created_at DESC, id DESC"
        return self.db.query(sql, tuple(params))

    def get(self, version_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_versions WHERE id=%s AND user_id=%s",
            (version_id, self.user_id))

    def get_by_hash(self, config_hash: str) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_versions WHERE user_id=%s AND config_hash=%s",
            (self.user_id, config_hash))

    def name_exists(self, name: str, exclude_id: int | None = None) -> bool:
        sql = "SELECT id FROM replay_versions WHERE user_id=%s AND name=%s"
        params: list[Any] = [self.user_id, name]
        if exclude_id is not None:
            sql += " AND id<>%s"
            params.append(exclude_id)
        return self.db.one(sql, tuple(params)) is not None

    def create(self, fields: dict) -> dict:
        cols = [
            "user_id", "name", "provider", "base_url", "model", "api_key_ref",
            "temperature", "top_p", "max_tokens", "thinking", "reasoning_effort",
            "prompt_style", "advice_version", "reconstruct_version",
            "engine_commit", "engine_dirty", "table_dynamics",
            "table_dynamics_sha", "config_hash", "notes", "archived",
        ]
        values = [fields.get(c) for c in cols]
        placeholders = ",".join(["%s"] * len(cols))
        try:
            new_id = self.db.execute(
                f"INSERT INTO replay_versions ({','.join(cols)}) VALUES ({placeholders})",
                tuple(values))
        except IntegrityError as exc:
            if "uk_ver_user_name" in str(exc):
                raise ApiError("VERSION_NAME_EXISTS",
                               f"版本名 {fields.get('name')} 已存在", 409) from exc
            if "uk_ver_user_hash" in str(exc):
                raise ApiError("VERSION_DUPLICATE",
                               "配置完全相同的版本已存在", 409) from exc
            raise
        return self.get(int(new_id)) or {}

    def update_meta(self, version_id: int, *, name: str | None = None,
                    notes: str | None = None,
                    archived: bool | None = None) -> dict:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name=%s")
            params.append(name)
        if notes is not None:
            sets.append("notes=%s")
            params.append(notes)
        if archived is not None:
            sets.append("archived=%s")
            params.append(1 if archived else 0)
        if not sets:
            return self.get(version_id) or {}
        params += [version_id, self.user_id]
        try:
            self.db.execute(
                f"UPDATE replay_versions SET {', '.join(sets)} "
                "WHERE id=%s AND user_id=%s", tuple(params))
        except IntegrityError as exc:
            raise ApiError("VERSION_NAME_EXISTS", "版本名已存在", 409) from exc
        return self.get(version_id) or {}

    def task_stats(self) -> dict[int, dict]:
        """版本 → {taskCount,lastUsedAt}。任务量很小，直接内存聚合。"""
        rows = self.db.query(
            "SELECT id, version_ids, created_at FROM replay_jobs WHERE user_id=%s",
            (self.user_id,))
        out: dict[int, dict] = {}
        for row in rows:
            for vid in json_load(row.get("version_ids"), []) or []:
                item = out.setdefault(int(vid), {"taskCount": 0, "lastUsedAt": None})
                item["taskCount"] += 1
                at = iso(row.get("created_at"))
                if at and (item["lastUsedAt"] is None or at > item["lastUsedAt"]):
                    item["lastUsedAt"] = at
        return out

    def p50_latency(self, version_id: int) -> float | None:
        rows = self.db.query(
            """SELECT duration_ms FROM replay_results
               WHERE version_id=%s AND source='model' AND duration_ms IS NOT NULL
               ORDER BY id DESC LIMIT 200""",
            (version_id,))
        values = [float(r["duration_ms"]) for r in rows if r.get("duration_ms")]
        if not values:
            return None
        med = median(values)
        return (med / 1000.0) if med else None


class DecisionRepository:
    def __init__(self, db: Database):
        self.db = db

    def persist(self, analysis: HandAnalysis) -> dict[tuple[str, int], dict]:
        for d in analysis.decisions:
            snap = d.snap
            self.db.execute(
                """INSERT INTO replay_decisions
                   (hand_id, hand_fingerprint, reconstruct_version, street, seq,
                    pot, to_call, hero_stack, hero_position, hero_cards, board,
                    players, opponents, actual_action, actual_amount, quality,
                    warnings, snapshot_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     snapshot_json=VALUES(snapshot_json),
                     warnings=VALUES(warnings),
                     quality=VALUES(quality),
                     actual_action=VALUES(actual_action),
                     actual_amount=VALUES(actual_amount)""",
                (
                    analysis.hand_id, analysis.fingerprint,
                    current_reconstruct_version(), d.street, d.seq,
                    d.pot, float(snap.to_call or 0.0), snap.hero_stack,
                    snap.hero_position, "".join(snap.hero_cards),
                    "".join(snap.board), snap.table_size,
                    snap.opponents_in_hand, d.actual, d.actual_amount,
                    d.quality, json_dumps(d.warnings), d.snapshot_json,
                ))
        return self.for_hand(analysis.hand_id, analysis.fingerprint)

    def for_hand(self, hand_id: int,
                 fingerprint: str | None = None) -> dict[tuple[str, int], dict]:
        from .versioning import CURRENT_RECONSTRUCT_VERSION
        sql = ("SELECT * FROM replay_decisions WHERE hand_id=%s "
               "AND reconstruct_version=%s")
        params: list[Any] = [hand_id, CURRENT_RECONSTRUCT_VERSION]
        if fingerprint:
            sql += " AND hand_fingerprint=%s"
            params.append(fingerprint)
        sql += " ORDER BY seq"
        rows = self.db.query(sql, tuple(params))
        return {(r["street"], int(r["seq"])): r for r in rows}

    def list_for_hand(self, hand_id: int,
                      fingerprint: str | None = None) -> list[dict]:
        rows = self.for_hand(hand_id, fingerprint)
        return list(rows.values())

    def get(self, decision_id: int) -> dict | None:
        return self.db.one("SELECT * FROM replay_decisions WHERE id=%s",
                           (decision_id,))


def current_reconstruct_version() -> str:
    # 延迟导入，避免 repository ↔ versioning 循环
    from .versioning import CURRENT_RECONSTRUCT_VERSION
    return CURRENT_RECONSTRUCT_VERSION


class JobRepository:
    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def create(self, *, scope: str, version_ids: list[int],
               hand_ids: list[int], repeat: int, total_hands: int,
               total_decisions: int, total_units: int,
               skipped_units: int, config_snapshot: dict,
               note: str | None = None,
               parent_job_id: int | None = None) -> int:
        return self.db.execute(
            """INSERT INTO replay_jobs
               (user_id, parent_job_id, scope, version_ids, hand_ids,
                repeat_count, status, total_hands, total_decisions, total_units,
                skipped_units, config_snapshot, note)
               VALUES (%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s)""",
            (
                self.user_id, parent_job_id, scope,
                json_dumps(version_ids), json_dumps(hand_ids), repeat,
                total_hands, total_decisions, total_units, skipped_units,
                json_dumps(config_snapshot), note,
            ))

    def add_hands(self, job_id: int, hands: list[tuple[int, str, int]]) -> None:
        if not hands:
            return
        self.db.execute_many(
            """INSERT INTO replay_job_hands
               (job_id, hand_id, hand_fingerprint, decision_count)
               VALUES (%s,%s,%s,%s)""",
            [(job_id, hand_id, fp, count) for hand_id, fp, count in hands])

    def get(self, job_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_jobs WHERE id=%s AND user_id=%s",
            (job_id, self.user_id))

    def get_any(self, job_id: int) -> dict | None:
        return self.db.one("SELECT * FROM replay_jobs WHERE id=%s", (job_id,))

    def list(self, *, status: str | None = None, page: int = 1,
             page_size: int = 20) -> tuple[int, list[dict]]:
        where = " WHERE user_id=%s"
        params: list[Any] = [self.user_id]
        if status:
            where += " AND status=%s"
            params.append(status)
        total = int(self.db.scalar(
            f"SELECT COUNT(*) n FROM replay_jobs{where}", tuple(params)) or 0)
        rows = self.db.query(
            f"SELECT * FROM replay_jobs{where} ORDER BY id DESC LIMIT %s OFFSET %s",
            tuple(params + [page_size, (page - 1) * page_size]))
        return total, rows

    def job_hands(self, job_id: int) -> list[dict]:
        return self.db.query(
            "SELECT * FROM replay_job_hands WHERE job_id=%s ORDER BY hand_id",
            (job_id,))

    def queue_depth(self) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) n FROM replay_jobs WHERE status IN ('queued','running')")
            or 0)

    def claim_next(self, worker_id: str) -> dict | None:
        with self.db.cursor(commit=True) as cur:
            cur.execute(
                """SELECT id FROM replay_jobs
                   WHERE status='queued' AND cancel_requested=0
                   ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""")
            row = cur.fetchone()
            if not row:
                return None
            job_id = int(row["id"])
            cur.execute(
                """UPDATE replay_jobs
                   SET status='running', worker_id=%s,
                       started_at=COALESCE(started_at, NOW(3)),
                       heartbeat_at=NOW(3)
                   WHERE id=%s AND status='queued'""",
                (worker_id, job_id))
            if cur.rowcount == 0:
                return None
        return self.get_any(job_id)

    def heartbeat(self, job_id: int, worker_id: str) -> None:
        self.db.execute(
            "UPDATE replay_jobs SET heartbeat_at=NOW(3) "
            "WHERE id=%s AND status='running' AND worker_id=%s",
            (job_id, worker_id))

    def heartbeat_many(self, worker_id: str) -> None:
        self.db.execute(
            "UPDATE replay_jobs SET heartbeat_at=NOW(3) "
            "WHERE status='running' AND worker_id=%s", (worker_id,))

    def request_cancel(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None:
            raise ApiError("JOB_NOT_FOUND", f"任务 {job_id} 不存在", 404)
        if job["status"] in ("succeeded", "failed", "partial", "canceled"):
            raise ApiError("JOB_NOT_CANCELABLE",
                           f"任务已结束（{job['status']}），不能取消", 409)
        if job["status"] == "queued":
            self.db.execute(
                """UPDATE replay_jobs SET cancel_requested=1, status='canceled',
                   finished_at=NOW(3) WHERE id=%s AND status='queued'""", (job_id,))
        else:
            self.db.execute(
                "UPDATE replay_jobs SET cancel_requested=1 WHERE id=%s", (job_id,))
        return True

    def cancel_requested(self, job_id: int) -> bool:
        value = self.db.scalar(
            "SELECT cancel_requested FROM replay_jobs WHERE id=%s", (job_id,))
        return bool(value)

    def recover_orphans(self) -> int:
        """进程重启：上一进程的 running 任务标记失败，结果保留。"""
        with self.db.cursor(commit=True) as cur:
            cur.execute(
                """UPDATE replay_job_hands jh
                   JOIN replay_jobs j ON j.id=jh.job_id
                      SET jh.status='failed',
                          jh.error_msg='服务重启，任务中断；可重试失败'
                    WHERE j.status='running' AND jh.status='running'""")
            cur.execute(
                """UPDATE replay_jobs
                   SET status='failed', finished_at=NOW(3),
                       error_msg='服务重启，任务中断；已落库结果保留，可重试失败'
                   WHERE status='running'""")
            return cur.rowcount or 0

    def set_progress(self, job_id: int, *, done: int, failed: int,
                     total: int | None = None,
                     skipped: int | None = None) -> None:
        sets = ["done_units=%s", "failed_units=%s", "heartbeat_at=NOW(3)"]
        params: list[Any] = [done, failed]
        if total is not None:
            sets.append("total_units=%s")
            params.append(total)
        if skipped is not None:
            sets.append("skipped_units=%s")
            params.append(skipped)
        params.append(job_id)
        self.db.execute(
            f"UPDATE replay_jobs SET {', '.join(sets)} WHERE id=%s", tuple(params))

    def finalize(self, job_id: int, status: str, *, error_msg: str | None = None,
                 done: int = 0, failed: int = 0) -> None:
        self.db.execute(
            """UPDATE replay_jobs SET status=%s, done_units=%s, failed_units=%s,
               finished_at=NOW(3), error_msg=%s WHERE id=%s""",
            (status, done, failed, error_msg, job_id))

    def counts(self, job_id: int) -> dict:
        row = self.db.one(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN source='error' THEN 1 ELSE 0 END) failed,
                      SUM(CASE WHEN source='fallback' THEN 1 ELSE 0 END) fallback
                 FROM replay_results WHERE job_id=%s""", (job_id,)) or {}
        return {
            "done": int(row.get("total") or 0),
            "failed": int(row.get("failed") or 0),
            "fallback": int(row.get("fallback") or 0),
        }

    def by_version(self, job_id: int) -> list[dict]:
        rows = self.db.query(
            """SELECT version_id, COUNT(*) done,
                      SUM(CASE WHEN source='error' THEN 1 ELSE 0 END) failed,
                      SUM(CASE WHEN source='fallback' THEN 1 ELSE 0 END) fallback,
                      AVG(duration_ms) avg_ms
                 FROM replay_results WHERE job_id=%s GROUP BY version_id""",
            (job_id,))
        return rows

    def update_hand_status(self, job_id: int, hand_id: int, status: str,
                           done_units: int | None = None,
                           error_msg: str | None = None) -> None:
        sets = ["status=%s"]
        params: list[Any] = [status]
        if done_units is not None:
            sets.append("done_units=%s")
            params.append(done_units)
        if error_msg is not None:
            sets.append("error_msg=%s")
            params.append(error_msg[:500])
        params += [job_id, hand_id]
        self.db.execute(
            f"UPDATE replay_job_hands SET {', '.join(sets)} "
            "WHERE job_id=%s AND hand_id=%s", tuple(params))

    def hand_progress(self, job_id: int) -> dict[int, dict]:
        rows = self.db.query(
            "SELECT * FROM replay_job_hands WHERE job_id=%s", (job_id,))
        return {int(r["hand_id"]): r for r in rows}
    def counts_by_hand(self, job_id: int) -> list[dict]:
        return self.db.query(
            """SELECT d.hand_id, COUNT(*) total,
                      SUM(CASE WHEN r.source='error' THEN 1 ELSE 0 END) failed
                 FROM replay_results r
                 JOIN replay_decisions d ON d.id=r.decision_id
                WHERE r.job_id=%s GROUP BY d.hand_id""", (job_id,))



class ResultRepository:
    def __init__(self, db: Database):
        self.db = db

    def existing_keys(self, job_id: int) -> set[tuple[int, int, int]]:
        rows = self.db.query(
            "SELECT decision_id, version_id, repeat_index FROM replay_results WHERE job_id=%s",
            (job_id,))
        return {(int(r["decision_id"]), int(r["version_id"]), int(r["repeat_index"]))
                for r in rows}

    def insert(self, *, job_id: int, decision_id: int, version_id: int,
               repeat_index: int, payload: dict) -> bool:
        result = self.db.execute(
            """INSERT IGNORE INTO replay_results
               (job_id, decision_id, version_id, repeat_index, option_id,
                option_kind, option_amount, option_label, reason, source, model,
                duration_ms, tokens_in, tokens_out, bucket, violation_tags,
                facts_json, prompt_system, prompt_user, raw_response, error_msg)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                job_id, decision_id, version_id, repeat_index,
                payload.get("option_id"), payload.get("option_kind"),
                payload.get("option_amount"), payload.get("option_label"),
                payload.get("reason"), payload.get("source"),
                payload.get("model"), payload.get("duration_ms"),
                payload.get("tokens_in"), payload.get("tokens_out"),
                payload.get("bucket"), payload.get("violation_tags"),
                payload.get("facts_json"), payload.get("prompt_system"),
                payload.get("prompt_user"), payload.get("raw_response"),
                payload.get("error_msg"),
            ))
        return bool(result)

    def for_job(self, job_id: int, hand_id: int | None = None,
                version_ids: list[int] | None = None) -> list[dict]:
        sql = """SELECT r.*, d.id AS decision_id, d.hand_id, d.hand_fingerprint,
                        d.reconstruct_version, d.street, d.seq,
                        d.pot, d.to_call, d.hero_stack, d.hero_position,
                        d.hero_cards, d.board, d.players, d.opponents,
                        d.actual_action, d.actual_amount, d.quality, d.warnings,
                        d.snapshot_json
                   FROM replay_results r
                   JOIN replay_decisions d ON d.id = r.decision_id
                  WHERE r.job_id=%s"""
        params: list[Any] = [job_id]
        if hand_id is not None:
            sql += " AND d.hand_id=%s"
            params.append(hand_id)
        if version_ids:
            placeholders = ",".join(["%s"] * len(version_ids))
            sql += f" AND r.version_id IN ({placeholders})"
            params += version_ids
        sql += " ORDER BY d.hand_id, d.seq, r.version_id, r.repeat_index"
        return self.db.query(sql, tuple(params))

    def for_hand_latest(self, hand_id: int,
                        version_ids: list[int] | None = None) -> list[dict]:
        """取该手牌最近一次任务的结果（可按版本过滤）。"""
        job = self.db.one(
            """SELECT j.id FROM replay_jobs j
               JOIN replay_results r ON r.job_id=j.id
               JOIN replay_decisions d ON d.id=r.decision_id
              WHERE d.hand_id=%s
              ORDER BY j.created_at DESC, j.id DESC LIMIT 1""", (hand_id,))
        if not job:
            return []
        return self.for_job(int(job["id"]), hand_id, version_ids)

    def latest_runs(self, hand_ids: list[int]) -> dict[int, dict]:
        if not hand_ids:
            return {}
        placeholders = ",".join(["%s"] * len(hand_ids))
        rows = self.db.query(
            f"""SELECT d.hand_id, j.id job_id, j.created_at, j.version_ids,
                       j.repeat_count
                  FROM replay_results r
                  JOIN replay_jobs j ON j.id=r.job_id
                  JOIN replay_decisions d ON d.id=r.decision_id
                 WHERE d.hand_id IN ({placeholders})
                 ORDER BY r.id DESC""",
            tuple(hand_ids))
        out: dict[int, dict] = {}
        for row in rows:
            hand_id = int(row["hand_id"])
            if hand_id not in out:
                out[hand_id] = row
        return out

    def version_names(self, version_ids: Iterable[int]) -> dict[int, str]:
        ids = [int(v) for v in version_ids]
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        rows = self.db.query(
            f"SELECT id, name FROM replay_versions WHERE id IN ({placeholders})",
            tuple(ids))
        return {int(r["id"]): r["name"] for r in rows}

    def cleanup_prompts(self, days: int) -> int:
        if days <= 0:
            return 0
        return self.db.execute(
            """UPDATE replay_results
               SET prompt_system=NULL, prompt_user=NULL, raw_response=NULL
               WHERE created_at < DATE_SUB(NOW(3), INTERVAL %s DAY)
                 AND (prompt_system IS NOT NULL OR prompt_user IS NOT NULL
                      OR raw_response IS NOT NULL)""", (days,))
