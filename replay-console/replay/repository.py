"""数据仓储：review_hands 只读 + replay_* 读写。"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Iterable

from pymysql.err import IntegrityError

from .db import Database
from .decisions import HandAnalysis
from .errors import ApiError
from .util import iso, json_dumps, json_load, median, now_cn

log = logging.getLogger("replay.repo")

# 导入的手牌使用独立的 id 段，避免和 review_hands 自增 id 冲突。
# MySQL 的 BIGINT UNSIGNED 和 JS number 都能安全容纳 1e12。
IMPORTED_HAND_ID_BASE = 1_000_000_000_000

HAND_SOURCE_DB = "db"
HAND_SOURCE_REVIEW = "review"
HAND_SOURCE_LIVE = "live"
HAND_SOURCE_LABELS = {
    HAND_SOURCE_DB: "复盘库",
    HAND_SOURCE_REVIEW: "导入·复盘",
    HAND_SOURCE_LIVE: "导入·实时",
}


def is_imported_hand_id(hand_id: int) -> bool:
    return int(hand_id) >= IMPORTED_HAND_ID_BASE


class HandRepository:
    def __init__(self, db: Database, user_id: int = 0,
                 console_user_id: int = 0):
        self.db = db
        self.hand_user_id = user_id
        self.console_user_id = console_user_id
        self.imports = ImportedHandRepository(db, console_user_id)

    def _where(self) -> tuple[str, tuple]:
        if self.hand_user_id > 0:
            return " AND h.user_id = %s", (self.hand_user_id,)
        return "", ()

    def list_review_rows(self, *, limit: int = 10000) -> list[dict]:
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
              LEFT JOIN replay_hand_deletions d
                ON d.user_id = %s AND d.hand_id = h.id
             WHERE h.deleted_at IS NULL AND d.hand_id IS NULL{where}
             ORDER BY h.created_at DESC, h.id DESC
             LIMIT %s
        """
        rows = self.db.query(sql, (self.console_user_id,
                                   self.console_user_id) + params + (limit,))
        for row in rows:
            row["source"] = HAND_SOURCE_DB
            row["external_id"] = None
            row["batch_id"] = None
        return rows

    def list_rows(self, *, limit: int = 10000) -> list[dict]:
        rows = self.list_review_rows(limit=limit)
        rows += self.imports.list_rows(limit=limit)
        rows.sort(key=lambda r: (r.get("created_at") or dt.datetime.min,
                                 int(r["id"])), reverse=True)
        return rows[:limit]

    def get_row(self, hand_id: int) -> dict | None:
        if is_imported_hand_id(hand_id):
            return self.imports.get_row(hand_id)
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
              LEFT JOIN replay_hand_deletions d
                ON d.user_id = %s AND d.hand_id = h.id
             WHERE h.id = %s AND h.deleted_at IS NULL
               AND d.hand_id IS NULL{where}
             LIMIT 1
        """
        row = self.db.one(sql, (self.console_user_id, self.console_user_id,
                               hand_id) + params)
        if row is not None:
            row["source"] = HAND_SOURCE_DB
            row["external_id"] = None
            row["batch_id"] = None
        return row

    def get_by_ids(self, hand_ids: Iterable[int]) -> list[dict]:
        ids = [int(x) for x in hand_ids]
        if not ids:
            return []
        imported = [x for x in ids if is_imported_hand_id(x)]
        review = [x for x in ids if not is_imported_hand_id(x)]
        rows = self.imports.get_by_ids(imported)
        if review:
            where, params = self._where()
            placeholders = ",".join(["%s"] * len(review))
            sql = f"""
                SELECT h.*,
                       m.favorite AS mark_favorite,
                       m.tags AS mark_tags,
                       m.status AS mark_status,
                       m.note AS mark_note
                  FROM review_hands h
                  LEFT JOIN replay_marks m
                    ON m.user_id = %s AND m.hand_id = h.id
                  LEFT JOIN replay_hand_deletions d
                    ON d.user_id = %s AND d.hand_id = h.id
                 WHERE h.id IN ({placeholders}) AND h.deleted_at IS NULL
                   AND d.hand_id IS NULL{where}
            """
            review_rows = self.db.query(
                sql, (self.console_user_id, self.console_user_id,
                      *review, *params))
            for row in review_rows:
                row["source"] = HAND_SOURCE_DB
                row["external_id"] = None
                row["batch_id"] = None
            rows += review_rows
        return rows

    def count(self) -> int:
        where, params = self._where()
        sql = f"""
            SELECT COUNT(*) n FROM review_hands h
              LEFT JOIN replay_hand_deletions d
                ON d.user_id = %s AND d.hand_id = h.id
             WHERE h.deleted_at IS NULL AND d.hand_id IS NULL{where}
        """
        review = int(self.db.scalar(
            sql, (self.console_user_id,) + params) or 0)
        return review + self.imports.count()

    def favorite_count(self) -> int:
        where, params = self._where()
        sql = f"""
            SELECT COUNT(*) n FROM review_hands h
              JOIN replay_marks m ON m.user_id = %s AND m.hand_id = h.id
              LEFT JOIN replay_hand_deletions d
                ON d.user_id = %s AND d.hand_id = h.id
             WHERE h.deleted_at IS NULL AND d.hand_id IS NULL
               AND m.favorite = 1{where}
        """
        review = int(self.db.scalar(
            sql, (self.console_user_id, self.console_user_id) + params) or 0)
        return review + self.imports.favorite_count()

    def delete_hands(self, hand_ids: Iterable[int]) -> dict:
        """删除手牌：导入手牌软删除；review_hands 只在控制台隐藏。"""
        ids = [int(x) for x in hand_ids]
        imported = [x for x in ids if is_imported_hand_id(x)]
        review = [x for x in ids if not is_imported_hand_id(x)]
        imported_deleted = self.imports.soft_delete(imported)
        review_hidden = 0
        if review:
            now = now_cn()
            self.db.execute_many(
                """INSERT INTO replay_hand_deletions (user_id, hand_id, deleted_at)
                   VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE deleted_at=VALUES(deleted_at)""",
                [(self.console_user_id, hand_id, now) for hand_id in review],
            )
            review_hidden = len(review)
        return {
            "imported": imported_deleted,
            "reviewHidden": review_hidden,
        }


class ImportedHandRepository:
    """导入手牌的读写。列表查询不取 payload/raw，避免把大字段带进列表。"""

    COLUMNS = """
        h.id, h.user_id, h.batch_id, h.source, h.external_id, h.fingerprint,
        h.title, h.hero_cards, h.board, h.hero_position, h.table_size,
        h.hero_stack_bb, h.small_blind_bb, h.big_blind_bb, h.ante_bb,
        h.bomb_pot_bb, h.villain_count, h.villains, h.streets, h.result,
        h.result_amount, h.pot_type, h.hero_tags, h.source_created_at,
        h.created_at, h.updated_at
    """

    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def _select(self) -> str:
        return f"""
            SELECT {self.COLUMNS},
                   m.favorite AS mark_favorite,
                   m.tags AS mark_tags,
                   m.status AS mark_status,
                   m.note AS mark_note
              FROM replay_imported_hands h
              LEFT JOIN replay_marks m
                ON m.user_id = %s AND m.hand_id = h.id
        """

    def list_rows(self, *, limit: int = 10000) -> list[dict]:
        sql = self._select() + """
             WHERE h.user_id = %s AND h.deleted_at IS NULL
             ORDER BY h.created_at DESC, h.id DESC
             LIMIT %s
        """
        return self.db.query(sql, (self.user_id, self.user_id, limit))

    def get_row(self, hand_id: int) -> dict | None:
        sql = self._select() + """
             WHERE h.id = %s AND h.user_id = %s AND h.deleted_at IS NULL
             LIMIT 1
        """
        return self.db.one(sql, (self.user_id, hand_id, self.user_id))

    def get_payload(self, hand_id: int) -> dict:
        """按需取导入时冻结的 payload（含 advices 这类大字段）。

        列表查询刻意不 SELECT payload_json/raw_json；详情页才查这一列。
        """
        row = self.db.one(
            "SELECT payload_json FROM replay_imported_hands "
            "WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
            (int(hand_id), self.user_id))
        if not row:
            return {}
        payload = json_load(row.get("payload_json"), {})
        return payload if isinstance(payload, dict) else {}

    def get_by_ids(self, hand_ids: Iterable[int]) -> list[dict]:
        ids = [int(x) for x in hand_ids]
        if not ids:
            return []
        placeholders = ",".join(["%s"] * len(ids))
        sql = self._select() + f"""
             WHERE h.id IN ({placeholders}) AND h.user_id = %s
               AND h.deleted_at IS NULL
        """
        return self.db.query(sql, (self.user_id, *ids, self.user_id))

    def count(self) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) n FROM replay_imported_hands "
            "WHERE user_id=%s AND deleted_at IS NULL",
            (self.user_id,)) or 0)

    def favorite_count(self) -> int:
        return int(self.db.scalar(
            """SELECT COUNT(*) n FROM replay_imported_hands h
                 JOIN replay_marks m ON m.user_id=%s AND m.hand_id=h.id
                WHERE h.user_id=%s AND h.deleted_at IS NULL AND m.favorite=1""",
            (self.user_id, self.user_id)) or 0)

    def soft_delete(self, hand_ids: Iterable[int]) -> int:
        ids = [int(x) for x in hand_ids]
        if not ids:
            return 0
        placeholders = ",".join(["%s"] * len(ids))
        self.db.execute(
            f"""UPDATE replay_imported_hands SET deleted_at=%s
                 WHERE user_id=%s AND id IN ({placeholders})
                   AND deleted_at IS NULL""",
            (now_cn(), self.user_id, *ids),
        )
        return len(ids)

    def fingerprint_map(self) -> dict[tuple[str, str], int]:
        rows = self.db.query(
            "SELECT id, source, fingerprint FROM replay_imported_hands "
            "WHERE user_id=%s", (self.user_id,))
        return {(r["source"], r["fingerprint"]): int(r["id"]) for r in rows}

    def find_by_fingerprint(self, source: str,
                            fingerprint: str) -> dict | None:
        return self.db.one(
            """SELECT id, source, fingerprint FROM replay_imported_hands
                WHERE user_id=%s AND source=%s AND fingerprint=%s""",
            (self.user_id, source, fingerprint))

    def upsert(self, row: dict, *, batch_id: int | None = None) -> tuple[int, bool]:
        """按 (user, source, fingerprint) 幂等写入。返回 (id, 是否新建)。"""
        existing = self.find_by_fingerprint(row["source"], row["fingerprint"])
        payload = {k: v for k, v in row.items() if k != "raw"}
        created_at = row.get("source_created_at") or now_cn()
        values = (
            self.user_id, batch_id, row["source"],
            (row.get("external_id") or "")[:128],
            row["fingerprint"],
            ((row.get("title") or "")[:255] or None),
            (row.get("hero_cards") or "")[:8],
            (row.get("board") or "")[:10],
            ((row.get("hero_position") or "")[:10] or None),
            int(row.get("table_size") or 0), row.get("hero_stack_bb"),
            row.get("small_blind_bb"), row.get("big_blind_bb"),
            row.get("ante_bb"), row.get("bomb_pot_bb"),
            int(row.get("villain_count") or 0),
            json_dumps(row.get("villains") or []),
            json_dumps(row.get("streets") or []),
            (row.get("result") or "unknown")[:16], row.get("result_amount"),
            ((row.get("pot_type") or "")[:16] or None),
            json_dumps(row.get("hero_tags") or []),
            json_dumps(payload), json_dumps(row.get("raw") or {}),
            row.get("source_created_at") or created_at, created_at, now_cn(),
        )
        if existing is not None:
            self.db.execute(
                """UPDATE replay_imported_hands SET
                     batch_id=%s, external_id=%s, title=%s, hero_cards=%s,
                     board=%s, hero_position=%s, table_size=%s, hero_stack_bb=%s,
                     small_blind_bb=%s, big_blind_bb=%s, ante_bb=%s,
                     bomb_pot_bb=%s, villain_count=%s, villains=%s, streets=%s,
                     result=%s, result_amount=%s, pot_type=%s, hero_tags=%s,
                     payload_json=%s, raw_json=%s, source_created_at=%s,
                     updated_at=%s, deleted_at=NULL
                   WHERE id=%s AND user_id=%s""",
                (values[1], values[3], values[5], values[6], values[7], values[8],
                 values[9], values[10], values[11], values[12], values[13],
                 values[14], values[15], values[16], values[17], values[18],
                 values[19], values[20], values[21], values[22], values[23],
                 values[24], values[26], int(existing["id"]), self.user_id),
            )
            return int(existing["id"]), False
        try:
            new_id = self.db.execute(
                """INSERT INTO replay_imported_hands
                     (user_id, batch_id, source, external_id, fingerprint, title,
                      hero_cards, board, hero_position, table_size, hero_stack_bb,
                      small_blind_bb, big_blind_bb, ante_bb, bomb_pot_bb,
                      villain_count, villains, streets, result, result_amount,
                      pot_type, hero_tags, payload_json, raw_json,
                      source_created_at, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                values,
            )
            return int(new_id), True
        except IntegrityError:
            existing = self.find_by_fingerprint(row["source"],
                                                row["fingerprint"])
            if existing is None:
                raise
            return int(existing["id"]), False


class ImportBatchRepository:
    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def create(self, *, source: str, container: str, filename: str | None,
               total_records: int) -> int:
        return int(self.db.execute(
            """INSERT INTO replay_import_batches
                 (user_id, source, container, filename, total_records, status)
               VALUES (%s,%s,%s,%s,%s,'running')""",
            (self.user_id, source, container, (filename or "")[:255] or None,
             int(total_records)),
        ))

    def finish(self, batch_id: int, *, status: str, imported: int = 0,
               updated: int = 0, duplicates: int = 0, invalid: int = 0,
               errors: int = 0, summary: dict | None = None,
               error_msg: str | None = None) -> None:
        self.db.execute(
            """UPDATE replay_import_batches SET
                 status=%s, imported_count=%s, updated_count=%s,
                 duplicate_count=%s, invalid_count=%s, error_count=%s,
                 summary_json=%s, error_msg=%s, finished_at=%s
               WHERE id=%s AND user_id=%s""",
            (status, int(imported), int(updated), int(duplicates), int(invalid),
             int(errors), json_dumps(summary or {}), (error_msg or "")[:500] or None,
             now_cn(), int(batch_id), self.user_id),
        )

    def list(self, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            """SELECT id, user_id, source, container, filename, total_records,
                      imported_count, updated_count, duplicate_count,
                      invalid_count, error_count, status, error_msg,
                      summary_json, created_at, finished_at
                 FROM replay_import_batches WHERE user_id=%s
                ORDER BY id DESC LIMIT %s""",
            (self.user_id, int(limit)),
        )
        for row in rows:
            row["summary"] = json_load(row.get("summary_json"), {}) or {}
            row["createdAt"] = iso(row.get("created_at"))
            row["finishedAt"] = iso(row.get("finished_at"))
            row["sourceLabel"] = HAND_SOURCE_LABELS.get(row.get("source"),
                                                        row.get("source"))
            row.pop("created_at", None)
            row.pop("finished_at", None)
        return rows


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
                facts_json, prompt_system, prompt_user, raw_response, error_msg,
                prompt_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
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
                payload.get("error_msg"), payload.get("prompt_version"),
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
