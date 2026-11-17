"""评分运行（#57）：replay_runs / replay_run_items 的读写。

一次 run 冻结「案例集 revision × spec revisions × 版本 × 判定版本 ×
重建版本」；每条 run_item 是一次模型调用（含 P0 的额外重复）的结果与分数。
表在 012 已建，运行态列由 013 补。
"""
from __future__ import annotations

from typing import Any

from .db import Database
from .util import iso, json_dumps, json_load, now_cn

RUN_STATUSES = ("queued", "running", "succeeded", "partial", "failed")

_RUN_SELECT = """SELECT r.*, s.name AS set_name, s.purpose AS set_purpose,
                        s.revision AS set_revision,
                        s.frozen_at AS set_frozen_at,
                        v.name AS version_name, v.model AS version_model,
                        v.archived AS version_archived
                   FROM replay_runs r
                   LEFT JOIN replay_case_sets s ON s.id = r.case_set_id
                   LEFT JOIN replay_versions v ON v.id = r.version_id"""

_ITEM_SELECT = """SELECT i.*, c.priority AS case_priority,
                         c.status AS case_status, c.canonical_id,
                         c.decision_key,
                         ch.hero_cards, ch.board, ch.hero_position,
                         d.action_index, d.pot AS decision_pot,
                         d.position_bucket, d.hand_tier, d.board_texture,
                         d.pot_type, d.facing, d.opponent_profile
                    FROM replay_run_items i
                    JOIN replay_cases c ON c.id = i.case_id
                    LEFT JOIN replay_canonical_hands ch
                           ON ch.id = c.canonical_id
                    LEFT JOIN replay_decisions d
                           ON d.id = (SELECT MIN(d2.id)
                                        FROM replay_decisions d2
                                       WHERE d2.canonical_id = c.canonical_id
                                         AND d2.street = SUBSTRING_INDEX(
                                               c.decision_key, ':', 1)
                                         AND d2.seq = CAST(SUBSTRING_INDEX(
                                               c.decision_key, ':', -1)
                                               AS UNSIGNED)
                                         AND d2.reconstruct_version = %s)
                   WHERE i.run_id = %s"""


class RunRepository:
    def __init__(self, db: Database):
        self.db = db

    # ── 写 ──

    def create(self, *, case_set_id: int, case_set_revision: int,
               spec_ids: list[int], version_id: int, judge_version: str,
               reconstruct_version: str, total_cases: int,
               config_snapshot: dict) -> int:
        return int(self.db.execute(
            """INSERT INTO replay_runs
                   (case_set_id, case_set_revision, spec_ids_json, version_id,
                    judge_version, reconstruct_version, total_cases,
                    config_snapshot)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (int(case_set_id), int(case_set_revision),
             json_dumps([int(s) for s in spec_ids]), int(version_id),
             judge_version[:32], reconstruct_version[:16], int(total_cases),
             json_dumps(config_snapshot))))

    def claim(self, run_id: int) -> dict | None:
        """把一个 queued run 置 running；抢不到（被别的进程拿走）返回 None。"""
        changed = int(self.db.execute(
            """UPDATE replay_runs
                  SET status='running', started_at=COALESCE(started_at, %s)
                WHERE id=%s AND status='queued'""",
            (now_cn(), int(run_id))) or 0)
        return self.get(run_id) if changed else None

    def next_queued(self) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_runs WHERE status='queued'"
            " ORDER BY id LIMIT 1")

    def request_cancel(self, run_id: int) -> dict:
        run = self.require(run_id)
        if run["status"] in ("queued", "running"):
            self.db.execute(
                "UPDATE replay_runs SET cancel_requested=1 WHERE id=%s",
                (int(run_id),))
        return self.require(run_id)

    def cancel_requested(self, run_id: int) -> bool:
        return bool(self.db.scalar(
            "SELECT cancel_requested FROM replay_runs WHERE id=%s",
            (int(run_id),)))

    def set_done(self, run_id: int, done_cases: int) -> int:
        return int(self.db.execute(
            "UPDATE replay_runs SET done_cases=%s WHERE id=%s",
            (int(done_cases), int(run_id))) or 0)

    def finish(self, run_id: int, status: str, *,
               score: float | None = None, p0_pass_rate: float | None = None,
               coverage_rate: float | None = None,
               scored_cases: int | None = None,
               metrics: dict | None = None,
               error_msg: str = "") -> int:
        if status not in RUN_STATUSES:
            raise ValueError(f"未知 run 状态：{status}")
        return int(self.db.execute(
            """UPDATE replay_runs
                  SET status=%s, finished_at=%s, score=%s,
                      p0_pass_rate=%s, coverage_rate=%s,
                      scored_cases=COALESCE(%s, scored_cases),
                      metrics_json=%s, error_msg=%s
                WHERE id=%s""",
            (status, now_cn(), score, p0_pass_rate, coverage_rate,
             scored_cases, json_dumps(metrics) if metrics is not None else None,
             (error_msg or "")[:500], int(run_id))) or 0)

    def update_metrics(self, run_id: int, *, score: float | None = None,
                       p0_pass_rate: float | None = None,
                       coverage_rate: float | None = None,
                       scored_cases: int | None = None,
                       metrics: dict | None = None) -> int:
        """人工改判后只重算聚合并回写；不动 status / finished_at。"""
        return int(self.db.execute(
            """UPDATE replay_runs
                  SET score=%s, p0_pass_rate=%s, coverage_rate=%s,
                      scored_cases=COALESCE(%s, scored_cases),
                      metrics_json=%s
                WHERE id=%s""",
            (score, p0_pass_rate, coverage_rate, scored_cases,
             json_dumps(metrics) if metrics is not None else None,
             int(run_id))) or 0)

    def recover_interrupted(self) -> int:
        """worker 启动时把上一进程留下的 running 标 partial，不算成功。"""
        return int(self.db.execute(
            """UPDATE replay_runs
                  SET status='partial', finished_at=%s,
                      error_msg=%s
                WHERE status='running'""",
            (now_cn(), "worker 重启：运行中断，未完成部分不计分")) or 0)

    # ── 读 ──

    def get(self, run_id: int) -> dict | None:
        return self.db.one(_RUN_SELECT + " WHERE r.id=%s", (int(run_id),))

    def require(self, run_id: int) -> dict:
        from .errors import ApiError

        row = self.get(run_id)
        if row is None:
            raise ApiError("RUN_NOT_FOUND", f"运行 {run_id} 不存在", 404)
        return row

    def list(self, *, limit: int = 100, case_set_id: int | None = None,
             status: str | None = None) -> list[dict]:
        sql = _RUN_SELECT + " WHERE 1=1"
        params: list[Any] = []
        if case_set_id:
            sql += " AND r.case_set_id=%s"
            params.append(int(case_set_id))
        if status:
            sql += " AND r.status=%s"
            params.append(status)
        sql += " ORDER BY r.id DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def done_cases(self, run_id: int) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(DISTINCT case_id) FROM replay_run_items"
            " WHERE run_id=%s", (int(run_id),)) or 0)

    def avg_tokens(self, version_id: int) -> dict | None:
        """该版本历史调用的平均 token（估算成本用）；没有历史返回 None。"""
        row = self.db.one(
            """SELECT AVG(tokens_in) AS tokens_in, AVG(tokens_out) AS tokens_out,
                      COUNT(*) AS n
                 FROM replay_results
                WHERE version_id=%s AND tokens_in IS NOT NULL""",
            (int(version_id),))
        if not row or not row.get("n"):
            return None
        return {"tokensIn": float(row.get("tokens_in") or 0),
                "tokensOut": float(row.get("tokens_out") or 0),
                "n": int(row["n"])}


class RunItemRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, *, run_id: int, case_id: int, spec_id: int,
               repeat_index: int, item: dict) -> int:
        return int(self.db.execute(
            """INSERT INTO replay_run_items
                   (run_id, case_id, spec_id, repeat_index, option_id,
                    option_kind, option_amount, reason, source, facts_json,
                    prompt_json, judge_json, scores_json, score, hard_fail,
                    error_msg, duration_ms)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                 option_id=VALUES(option_id),
                 option_kind=VALUES(option_kind),
                 option_amount=VALUES(option_amount),
                 reason=VALUES(reason),
                 source=VALUES(source),
                 facts_json=VALUES(facts_json),
                 prompt_json=VALUES(prompt_json),
                 judge_json=VALUES(judge_json),
                 scores_json=VALUES(scores_json),
                 score=VALUES(score),
                 hard_fail=VALUES(hard_fail),
                 error_msg=VALUES(error_msg),
                 duration_ms=VALUES(duration_ms)""",
            (int(run_id), int(case_id), int(spec_id), int(repeat_index),
             item.get("optionId"), item.get("kind"),
             item.get("amount"), (item.get("reason") or "")[:60000],
             item.get("source") or "model",
             item.get("factsJson"), item.get("promptJson"),
             item.get("judgeJson"), item.get("scoresJson"),
             item.get("score"), item.get("hardFail"),
             (item.get("errorMsg") or "")[:500],
             int(item.get("durationMs") or 0))))

    def update_scores(self, *, run_id: int, case_id: int, repeat_index: int,
                      scores: dict, score: float,
                      hard_fail: str | None) -> int:
        return int(self.db.execute(
            """UPDATE replay_run_items
                  SET scores_json=%s, score=%s, hard_fail=%s
                WHERE run_id=%s AND case_id=%s AND repeat_index=%s""",
            (json_dumps(scores), score, hard_fail, int(run_id), int(case_id),
             int(repeat_index))) or 0)

    def existing(self, run_id: int) -> set[tuple[int, int]]:
        rows = self.db.query(
            "SELECT case_id, repeat_index FROM replay_run_items"
            " WHERE run_id=%s", (int(run_id),))
        return {(int(r["case_id"]), int(r["repeat_index"])) for r in rows}

    def list(self, run_id: int, reconstruct_version: str, *,
             only_hardfail: bool = False, limit: int = 200,
             offset: int = 0) -> list[dict]:
        sql = _ITEM_SELECT
        params: list[Any] = [reconstruct_version, int(run_id)]
        if only_hardfail:
            sql += " AND i.hard_fail IS NOT NULL AND i.hard_fail <> ''"
        sql += " ORDER BY c.priority ASC, i.case_id ASC, i.repeat_index ASC"
        sql += " LIMIT %s OFFSET %s"
        params.extend([int(limit), int(offset)])
        return self.db.query(sql, tuple(params))

    def get(self, run_id: int, case_id: int, repeat_index: int,
            reconstruct_version: str) -> dict | None:
        return self.db.one(
            _ITEM_SELECT + " AND i.case_id=%s AND i.repeat_index=%s",
            (reconstruct_version, int(run_id), int(case_id),
             int(repeat_index)))

    def counts(self, run_id: int) -> dict:
        row = self.db.one(
            """SELECT COUNT(*) AS items,
                      COUNT(DISTINCT case_id) AS cases,
                      SUM(hard_fail IS NOT NULL AND hard_fail <> '') AS fails,
                      SUM(source='error') AS errors
                 FROM replay_run_items WHERE run_id=%s""",
            (int(run_id),))
        return {
            "items": int(row.get("items") or 0),
            "cases": int(row.get("cases") or 0),
            "fails": int(row.get("fails") or 0),
            "errors": int(row.get("errors") or 0),
        }

    # ── 对外 JSON ──

    @staticmethod
    def public_item(row: dict) -> dict:
        from . import cases as case_lib

        bucket = case_lib.case_bucket(row)
        return {
            "runId": int(row["run_id"]),
            "caseId": int(row["case_id"]),
            "specId": int(row["spec_id"]) if row.get("spec_id") else None,
            "repeatIndex": int(row["repeat_index"]),
            "canonicalId": (int(row["canonical_id"])
                            if row.get("canonical_id") is not None else None),
            "decisionKey": row.get("decision_key"),
            "street": case_lib.street_of(row),
            "actionIndex": (int(row["action_index"])
                            if row.get("action_index") is not None else None),
            "priority": int(row.get("case_priority") or 0),
            "p0": int(row.get("case_priority") or 0) == 0,
            "bucket": bucket,
            "bucketLabel": case_lib.label_of(bucket),
            "hand": {
                "heroCards": row.get("hero_cards") or "",
                "board": row.get("board") or "",
                "heroPosition": row.get("hero_position"),
            },
            "comparable": row.get("source") != "error",
            "advice": {
                "optionId": row.get("option_id"),
                "kind": row.get("option_kind"),
                "amount": (float(row["option_amount"])
                           if row.get("option_amount") is not None else None),
                "reason": row.get("reason") or "",
            },
            "source": row.get("source"),
            "score": (float(row["score"])
                      if row.get("score") is not None else None),
            "scores": json_load(row.get("scores_json"), {}) or {},
            "hardFail": row.get("hard_fail") or None,
            "errorMsg": row.get("error_msg") or "",
            "durationMs": int(row.get("duration_ms") or 0),
            "createdAt": iso(row.get("created_at")),
        }

class JudgeOverrideRepository:
    """replay_judge_overrides + replay_judge_override_log（#58 人工改判）。

    原始判定（run_items.judge_json / score）从不被覆盖：这里只存「人工复核后
    的口径」，聚合与页面展示时优先取它。每次改动都往 log 追加一条，
    留「谁、什么时候、从什么改成什么」。
    """

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            "runId": int(row["run_id"]),
            "caseId": int(row["case_id"]),
            "actor": row.get("actor") or "",
            "note": row.get("note") or "",
            "scores": json_load(row.get("scores_json"), {}) or {},
            "hardFail": row.get("hard_fail") or None,
            "machine": json_load(row.get("source_json"), {}) or {},
            "createdAt": iso(row.get("created_at")),
            "updatedAt": iso(row.get("updated_at")),
        }

    def get(self, run_id: int, case_id: int) -> dict | None:
        row = self.db.one(
            "SELECT * FROM replay_judge_overrides"
            " WHERE run_id=%s AND case_id=%s",
            (int(run_id), int(case_id)))
        return self._public(row) if row else None

    def list(self, run_id: int) -> dict[int, dict]:
        rows = self.db.query(
            "SELECT * FROM replay_judge_overrides WHERE run_id=%s"
            " ORDER BY case_id", (int(run_id),))
        return {int(row["case_id"]): self._public(row) for row in rows}

    def log(self, run_id: int, case_id: int,
            limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM replay_judge_override_log"
            " WHERE run_id=%s AND case_id=%s ORDER BY id DESC LIMIT %s",
            (int(run_id), int(case_id), int(limit)))
        return [{
            "id": int(row["id"]),
            "actor": row.get("actor") or "",
            "note": row.get("note") or "",
            "before": json_load(row.get("before_json"), {}) or {},
            "after": json_load(row.get("after_json"), {}) or {},
            "createdAt": iso(row.get("created_at")),
        } for row in rows]

    def save(self, *, run_id: int, case_id: int, actor: str, note: str,
             override: dict, before: dict | None) -> dict:
        self.db.execute(
            """INSERT INTO replay_judge_overrides
                   (run_id, case_id, scores_json, hard_fail, actor, note,
                    source_json)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                 scores_json=VALUES(scores_json),
                 hard_fail=VALUES(hard_fail),
                 actor=VALUES(actor),
                 note=VALUES(note),
                 source_json=VALUES(source_json)""",
            (int(run_id), int(case_id), json_dumps(override.get("scores") or {}),
             override.get("hardFail"), (actor or "")[:64], (note or "")[:2000],
             json_dumps(before) if before is not None else None))
        self.db.execute(
            """INSERT INTO replay_judge_override_log
                   (run_id, case_id, actor, note, before_json, after_json)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (int(run_id), int(case_id), (actor or "")[:64],
             (note or "")[:2000],
             json_dumps(before) if before is not None else None,
             json_dumps(override)))
        return self.get(run_id, case_id) or {}

    def clear(self, *, run_id: int, case_id: int, actor: str,
              note: str, before: dict | None) -> None:
        self.db.execute(
            "DELETE FROM replay_judge_overrides"
            " WHERE run_id=%s AND case_id=%s",
            (int(run_id), int(case_id)))
        self.db.execute(
            """INSERT INTO replay_judge_override_log
                   (run_id, case_id, actor, note, before_json, after_json)
               VALUES (%s,%s,%s,%s,%s,NULL)""",
            (int(run_id), int(case_id), (actor or "")[:64],
             (note or "")[:2000],
             json_dumps(before) if before is not None else None))
