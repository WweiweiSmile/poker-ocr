"""数据仓储：review_hands 只读 + replay_* 读写。"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Iterable

from poker_ocr.buckets import BUCKET_ORDER
from pymysql.err import IntegrityError

from .canonical import (
    MATCH_WINDOW,
    MERGE_CHAIN_LIMIT,
    as_datetime,
    content_group_key,
    normalize_board,
    normalize_hero_cards,
    pick_candidate,
    primary_row,
    rank_key,
    validate_source,
)
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

    def list_light_review_rows(self, *, limit: int = 10000) -> list[dict]:
        """列表用：不取 `streets` / `villains` 这些大 JSON 列。

        `list_hands` 只需要展示字段；决策数/质量从 `replay_decisions` 聚合。
        取 `h.*` 会把每条手牌的 streets（几百字节到几 KB）拖过远程连接，
        1058 行累计就是 8–16 秒的首屏。详情仍走 `get_row` 取全量。
        """
        where, params = self._where()
        sql = f"""
            SELECT h.id, h.user_id, h.game_id, h.title, h.hero_position,
                   h.hero_cards, h.hero_stack_bb, h.stakes, h.board,
                   h.villain_count, h.pot_type, h.result, h.result_amount,
                   h.hero_tags, h.analyze_status, h.created_at, h.updated_at,
                   h.table_size, h.small_blind_bb, h.big_blind_bb,
                   h.ante_bb, h.bomb_pot_bb,
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

    def list_light_rows(self, *, limit: int = 10000) -> list[dict]:
        """列表用的全部可见手牌（review + 导入），不含大 JSON 列。"""
        rows = self.list_light_review_rows(limit=limit)
        rows += self.imports.list_light_rows(limit=limit)
        rows.sort(key=lambda r: (r.get("created_at") or dt.datetime.min,
                                 int(r["id"])), reverse=True)
        return rows[:limit]

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

    def get_timeline(self, hand_id: int) -> dict:
        row = self.db.one(
            "SELECT timeline_json, timeline_version FROM replay_imported_hands "
            "WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
            (int(hand_id), self.console_user_id))
        if not row:
            return {}
        value = json_load(row.get("timeline_json"), {})
        return value if isinstance(value, dict) else {}

    def update_enrichment(self, hand_id: int, *, timeline: dict | None = None,
                          flag_mask: int = 0, flags: dict | None = None,
                          review_score: int = 0,
                          record_schema: str = "hand.v2") -> None:
        self.db.execute(
            """UPDATE replay_imported_hands
                  SET timeline_json=%s, timeline_version=%s,
                      flag_mask=%s, flags_json=%s, review_score=%s,
                      record_schema=%s, updated_at=%s
                WHERE id=%s AND user_id=%s""",
            (json_dumps(timeline or {}),
             (timeline or {}).get("version"),
             int(flag_mask), json_dumps(flags or {}), int(review_score),
             (record_schema or "hand.v2")[:16], now_cn(),
             int(hand_id), self.console_user_id))

    def add_flag(self, hand_id: int, name: str, reason: str) -> bool:
        """给导入的手牌加一个深挖位（用于实验完成后的 AB_FLIP 回写）。"""
        from .flags import FLAG_BITS
        bit = FLAG_BITS.get(name)
        if not bit:
            return False
        row = self.db.one(
            "SELECT flag_mask, flags_json, review_score FROM replay_imported_hands "
            "WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
            (int(hand_id), self.console_user_id))
        if not row:
            return False
        mask = int(row.get("flag_mask") or 0)
        flags = json_load(row.get("flags_json"), {}) or {}
        flags[name] = {"reason": reason}
        score = min(100, int(row.get("review_score") or 0) + 25)
        self.db.execute(
            """UPDATE replay_imported_hands
                  SET flag_mask=%s, flags_json=%s, review_score=%s,
                      updated_at=%s
                WHERE id=%s AND user_id=%s""",
            (mask | bit, json_dumps(flags), score, now_cn(),
             int(hand_id), self.console_user_id))
        return True

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
        h.external_uid, h.schema_version, h.result_source, h.table_roster,
        h.record_schema, h.flag_mask, h.flags_json, h.review_score,
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

    # 列表专用：去掉 streets / villains / table_roster 三个大 JSON 列。
    # 详情和重建需要它们，列表只需要展示字段；决策数/质量来自 replay_decisions。
    LIGHT_COLUMNS = """
        h.id, h.batch_id, h.source, h.external_id, h.fingerprint, h.title,
        h.hero_cards, h.board, h.hero_position, h.table_size,
        h.hero_stack_bb, h.bomb_pot_bb, h.result, h.result_amount,
        h.pot_type, h.hero_tags, h.flag_mask, h.flags_json, h.review_score,
        h.created_at, h.updated_at, h.user_id
    """

    def list_light_rows(self, *, limit: int = 10000) -> list[dict]:
        sql = f"""
            SELECT {self.LIGHT_COLUMNS},
                   m.favorite AS mark_favorite,
                   m.tags AS mark_tags,
                   m.status AS mark_status,
                   m.note AS mark_note
              FROM replay_imported_hands h
              LEFT JOIN replay_marks m
                ON m.user_id = %s AND m.hand_id = h.id
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

    def find_by_uid(self, source: str, external_uid: str | None) -> dict | None:
        """按来源内稳定 uid 查重；旧记录没有 uid 时返回 None。"""
        uid = (external_uid or "").strip()
        if not uid:
            return None
        return self.db.one(
            """SELECT id, source, fingerprint, external_uid
                 FROM replay_imported_hands
                WHERE user_id=%s AND source=%s AND external_uid=%s
                LIMIT 1""",
            (self.user_id, source, uid[:128]))

    def find_any_by_uid(self, external_uid: str | None) -> dict | None:
        """只按 uid 找（seed 导入拿 ab_set 的 20261101-xxx 反查手牌）。"""
        uid = (external_uid or "").strip()
        if not uid:
            return None
        return self.db.one(
            """SELECT id, source, fingerprint, external_uid
                 FROM replay_imported_hands
                WHERE user_id=%s AND external_uid=%s
                ORDER BY id DESC LIMIT 1""",
            (self.user_id, uid[:128]))

    def upsert(self, row: dict, *, batch_id: int | None = None) -> tuple[int, bool]:
        """幂等写入：优先按 (user, source, external_uid)，旧记录回退 fingerprint。

        uid 优先的原因：同一手牌补了 advice/context 后 fingerprint 会变，
        但手牌本身没变；只用 fingerprint 会把同一手重复入库。
        """
        source = row["source"]
        external_uid = (row.get("external_uid") or "").strip()[:128] or None
        existing = self.find_by_uid(source, external_uid)
        if existing is None:
            existing = self.find_by_fingerprint(source, row["fingerprint"])

        payload = {k: v for k, v in row.items() if k != "raw"}
        created_at = row.get("source_created_at") or now_cn()
        values = (
            self.user_id, batch_id, source,
            (row.get("external_id") or "")[:128],
            external_uid,
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
            int(row.get("schema_version") or 1),
            ((row.get("result_source") or "")[:16] or None),
            json_dumps(row.get("table_roster") or []),
        )
        if existing is not None:
            self.db.execute(
                """UPDATE replay_imported_hands SET
                     batch_id=%s, external_id=%s, external_uid=%s,
                     title=%s, hero_cards=%s, board=%s, hero_position=%s,
                     table_size=%s, hero_stack_bb=%s, small_blind_bb=%s,
                     big_blind_bb=%s, ante_bb=%s, bomb_pot_bb=%s,
                     villain_count=%s, villains=%s, streets=%s, result=%s,
                     result_amount=%s, pot_type=%s, hero_tags=%s,
                     payload_json=%s, raw_json=%s, source_created_at=%s,
                     schema_version=%s, result_source=%s, table_roster=%s,
                     updated_at=%s, deleted_at=NULL
                   WHERE id=%s AND user_id=%s""",
                (values[1], values[3], values[4], values[6], values[7],
                 values[8], values[9], values[10], values[11], values[12],
                 values[13], values[14], values[15], values[16], values[17],
                 values[18], values[19], values[20], values[21], values[22],
                 values[23], values[24], values[25], values[28], values[29],
                 values[30], values[27], int(existing["id"]), self.user_id),
            )
            return int(existing["id"]), False
        try:
            new_id = self.db.execute(
                """INSERT INTO replay_imported_hands
                     (user_id, batch_id, source, external_id, external_uid,
                      fingerprint, title, hero_cards, board, hero_position,
                      table_size, hero_stack_bb, small_blind_bb, big_blind_bb,
                      ante_bb, bomb_pot_bb, villain_count, villains, streets,
                      result, result_amount, pot_type, hero_tags, payload_json,
                      raw_json, source_created_at, created_at, updated_at,
                      schema_version, result_source, table_roster)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                values,
            )
            return int(new_id), True
        except IntegrityError:
            existing = (self.find_by_uid(source, external_uid)
                        or self.find_by_fingerprint(source, row["fingerprint"]))
            if existing is None:
                raise
            return int(existing["id"]), False

    def replace_players(self, hand_id: int, roster: list[dict] | None) -> int:
        """覆盖一手牌的座位/玩家行（先删旧行，roster 为空则只清空）。"""
        self.db.execute(
            "DELETE FROM replay_hand_players WHERE hand_id=%s", (int(hand_id),))
        if not roster:
            return 0
        written = 0
        for item in roster:
            if not isinstance(item, dict):
                continue
            seat_id = str(item.get("seat_id") or "").strip()
            if not seat_id:
                continue
            name = str(item.get("name") or "").strip() or None
            self.db.execute(
                """INSERT INTO replay_hand_players
                     (hand_id, seat_id, player_key, display_name, position,
                      is_hero, in_hand, start_stack, hole_cards)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     player_key=VALUES(player_key),
                     display_name=VALUES(display_name),
                     position=VALUES(position),
                     is_hero=VALUES(is_hero),
                     in_hand=VALUES(in_hand),
                     start_stack=VALUES(start_stack),
                     hole_cards=VALUES(hole_cards)""",
                (int(hand_id), seat_id[:16], name, name,
                 (str(item.get("position") or "")[:10] or None),
                 1 if item.get("is_hero") else 0,
                 1 if item.get("in_hand", True) else 0,
                 item.get("stack") if item.get("stack") is not None
                 else item.get("start_stack"),
                 (str(item.get("hole_cards") or "")[:8] or None)),
            )
            written += 1
        return written

    def replace_live_advices(self, hand_id: int, advices: list[dict] | None,
                             *, decision_ids: dict | None = None) -> int:
        """覆盖一手牌的线上建议审计行（先删旧行）。"""
        self.db.execute(
            "DELETE FROM replay_live_advices WHERE hand_id=%s", (int(hand_id),))
        if not advices:
            return 0
        decision_ids = decision_ids or {}
        written = 0
        for index, advice in enumerate(advices):
            if not isinstance(advice, dict):
                continue
            street = str(advice.get("street") or "")[:10]
            action_index = int(advice.get("action_index") or 0)
            option = (advice.get("option")
                      if isinstance(advice.get("option"), dict) else {})
            key = (street, action_index)
            decision_id = decision_ids.get(key)
            self.db.execute(
                """INSERT INTO replay_live_advices
                     (hand_id, decision_id, street, action_index, decision_key,
                      option_id, option_kind, option_amount, option_label,
                      reason, reason_full, source, model, prompt_version,
                      prompt_system, prompt_user, prompt_sha256, raw_response,
                      response_sha256, facts_json, model_params_json,
                      prompt_missing, prompt_missing_reason, captured_at,
                      attempt, context_json, duration_ms, tokens_in, tokens_out,
                      actual_action, actual_amount, bucket)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     decision_id=VALUES(decision_id),
                     decision_key=VALUES(decision_key),
                     option_id=VALUES(option_id),
                     option_kind=VALUES(option_kind),
                     option_amount=VALUES(option_amount),
                     option_label=VALUES(option_label),
                     reason=VALUES(reason),
                     reason_full=VALUES(reason_full),
                     source=VALUES(source),
                     model=VALUES(model),
                     prompt_version=VALUES(prompt_version),
                     prompt_system=VALUES(prompt_system),
                     prompt_user=VALUES(prompt_user),
                     prompt_sha256=VALUES(prompt_sha256),
                     raw_response=VALUES(raw_response),
                     response_sha256=VALUES(response_sha256),
                     facts_json=VALUES(facts_json),
                     model_params_json=VALUES(model_params_json),
                     prompt_missing=VALUES(prompt_missing),
                     prompt_missing_reason=VALUES(prompt_missing_reason),
                     captured_at=VALUES(captured_at),
                     attempt=VALUES(attempt),
                     context_json=VALUES(context_json),
                     duration_ms=VALUES(duration_ms),
                     tokens_in=VALUES(tokens_in),
                     tokens_out=VALUES(tokens_out),
                     actual_action=VALUES(actual_action),
                     actual_amount=VALUES(actual_amount),
                     bucket=VALUES(bucket)""",
                (int(hand_id), decision_id, street, action_index,
                 json_dumps(advice.get("decision_key") or [])[:255],
                 (str(option.get("id") or "")[:32] or None),
                 (str(option.get("kind") or "")[:16] or None),
                 option.get("amount_bb", option.get("amount")),
                 (str(option.get("label") or "")[:64] or None),
                 advice.get("reason"),
                 advice.get("reason_full"),
                 (str(advice.get("source") or "model")[:16]),
                 (str(advice.get("model") or "")[:100] or None),
                 (str(advice.get("prompt_version") or "")[:32] or None),
                 advice.get("prompt_system"),
                 advice.get("prompt_user"),
                 (str(advice.get("prompt_sha256") or "")[:64] or None),
                 advice.get("raw_response"),
                 (str(advice.get("response_sha256") or "")[:64] or None),
                 json_dumps(advice.get("facts") or {}),
                 json_dumps(advice.get("model_params") or {}),
                 1 if advice.get("prompt_missing") else 0,
                 (str(advice.get("prompt_missing_reason") or "")[:64] or None),
                 as_datetime(advice.get("captured_at")),
                 int(advice.get("attempt") or 1),
                 json_dumps(advice.get("context") or {}),
                 advice.get("duration_ms"),
                 advice.get("prompt_tokens"),
                 advice.get("completion_tokens"),
                 (str(advice.get("actual_action") or "")[:10] or None),
                 advice.get("actual_amount"),
                 (str(advice.get("bucket") or "")[:16] or None)),
            )
            written += 1
        return written


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


class DecisionMarkRepository:
    """逐决策点人工复盘：good/bad/unsure + 标签 + 备注。"""

    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def get(self, decision_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_decision_marks "
            "WHERE user_id=%s AND decision_id=%s",
            (self.user_id, int(decision_id)))

    def for_decisions(self, decision_ids: Iterable[int]) -> dict[int, dict]:
        ids = [int(x) for x in decision_ids]
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        rows = self.db.query(
            f"""SELECT * FROM replay_decision_marks
                 WHERE user_id=%s AND decision_id IN ({placeholders})""",
            (self.user_id, *ids))
        return {int(row["decision_id"]): row for row in rows}

    def upsert(self, decision_id: int, *, verdict: str,
               tags: list[str] | None = None,
               note: str | None = None) -> dict:
        self.db.execute(
            """INSERT INTO replay_decision_marks
                 (user_id, decision_id, verdict, tags, note)
               VALUES (%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                 verdict=VALUES(verdict), tags=VALUES(tags), note=VALUES(note)""",
            (self.user_id, int(decision_id), verdict,
             json_dumps(tags or []), (note or "")[:4000] or None))
        return self.get(decision_id) or {}


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
            "context_mode", "table_stats_mode",
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
                    action_index, pot, to_call, hero_stack, hero_position,
                    hero_cards, board, players, opponents, actual_action,
                    actual_amount, quality, warnings, snapshot_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     action_index=VALUES(action_index),
                     snapshot_json=VALUES(snapshot_json),
                     warnings=VALUES(warnings),
                     quality=VALUES(quality),
                     actual_action=VALUES(actual_action),
                     actual_amount=VALUES(actual_amount)""",
                (
                    analysis.hand_id, analysis.fingerprint,
                    current_reconstruct_version(), d.street, d.seq,
                    int(d.action_index or 0),
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

    def canonical_decisions(self) -> list[dict]:
        """列表用：每个 canonical 本版本的「去重决策点」及七桶取值。

        同一决策点可能被复盘、实时两条链路各写一行；按 (canonical, street,
        seq) 去重，质量取较差的那一行。1375 行轻量列，比逐手重建快得多。
        """
        from .versioning import CURRENT_RECONSTRUCT_VERSION
        return self.db.query(
            """SELECT canonical_id, street, seq,
                      MAX(quality='approx') AS is_approx,
                      MAX(position_bucket) AS position_bucket,
                      MAX(hand_tier) AS hand_tier,
                      MAX(board_texture) AS board_texture,
                      MAX(pot_type) AS pot_type,
                      MAX(facing) AS facing,
                      MAX(opponent_profile) AS opponent_profile
                 FROM replay_decisions
                WHERE reconstruct_version=%s AND canonical_id IS NOT NULL
                GROUP BY canonical_id, street, seq""",
            (CURRENT_RECONSTRUCT_VERSION,))

    def for_canonical(self, canonical_id: int) -> list[dict]:
        """本版本隶属于某个 canonical 的全部决策行（去重由调用方做）。"""
        from .versioning import CURRENT_RECONSTRUCT_VERSION
        return self.db.query(
            """SELECT d.* FROM replay_decisions d
                WHERE d.canonical_id=%s AND d.reconstruct_version=%s
                ORDER BY d.action_index, d.seq""",
            (int(canonical_id), CURRENT_RECONSTRUCT_VERSION))

    def for_source_hands(self, hand_ids: list[int]) -> list[dict]:
        from .versioning import CURRENT_RECONSTRUCT_VERSION
        if not hand_ids:
            return []
        placeholders = ",".join(["%s"] * len(hand_ids))
        return self.db.query(
            f"""SELECT hand_id, street, seq, action_index, quality,
                       position_bucket, hand_tier, board_texture, pot_type,
                       facing, opponent_profile, canonical_id
                  FROM replay_decisions
                 WHERE hand_id IN ({placeholders})
                   AND reconstruct_version=%s
                 ORDER BY hand_id, action_index, seq""",
            (*hand_ids, CURRENT_RECONSTRUCT_VERSION))


def current_reconstruct_version() -> str:
    # 延迟导入，避免 repository ↔ versioning 循环
    from .versioning import CURRENT_RECONSTRUCT_VERSION
    return CURRENT_RECONSTRUCT_VERSION


class LiveAdviceRepository:
    """线上建议审计行（replay_live_advices）的读取。"""

    def __init__(self, db: Database):
        self.db = db

    def list_for_hand(self, hand_id: int) -> list[dict]:
        rows = self.db.query(
            """SELECT * FROM replay_live_advices
                WHERE hand_id=%s ORDER BY street, action_index, id""",
            (int(hand_id),))
        return rows

    def get(self, advice_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_live_advices WHERE id=%s", (int(advice_id),))

    def baseline_prompts_for_hand(self, hand_id: int) -> dict[tuple[str, int], dict]:
        """(street, action_index) → 线上 prompt 原文，用于 stored baseline。"""
        rows = self.db.query(
            """SELECT street, action_index, prompt_system, prompt_user,
                      prompt_sha256, facts_json
                 FROM replay_live_advices
                WHERE hand_id=%s AND prompt_system IS NOT NULL
                  AND prompt_user IS NOT NULL""",
            (int(hand_id),))
        out: dict[tuple[str, int], dict] = {}
        for row in rows:
            facts = json_load(row.get("facts_json"), {}) or {}
            options = facts.get("options") if isinstance(facts, dict) else None
            out[(str(row.get("street") or ""),
                 int(row.get("action_index") or 0))] = {
                "system": row.get("prompt_system"),
                "user": row.get("prompt_user"),
                "sha": row.get("prompt_sha256"),
                "options": options if isinstance(options, list) else None,
            }
        return out

    def context_for_hand(self, hand_id: int) -> dict[tuple[str, int], dict]:
        """(street, action_index) → context_json。没有线上快照的手牌返回空。"""
        rows = self.db.query(
            """SELECT street, action_index, context_json
                 FROM replay_live_advices WHERE hand_id=%s""",
            (int(hand_id),))
        out: dict[tuple[str, int], dict] = {}
        for row in rows:
            context = json_load(row.get("context_json"), {})
            if isinstance(context, dict):
                out[(str(row.get("street") or ""),
                     int(row.get("action_index") or 0))] = context
        return out


class ExperimentRepository:
    """离线 A/B 实验：冻结手牌集、变体对、dev/holdout 切分。"""

    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def create(self, *, name: str, baseline_version_id: int,
               variant_version_id: int, hypothesis: str | None,
               filter_payload: dict | None, dev_ratio: float,
               repeat_count: int, split_strategy: str = "hand_hash",
               hand_set_id: int | None = None,
               hand_set_revision: int | None = None,
               kind: str = "ab", change_scope: str | None = None,
               baseline_prompt_mode: str = "stored") -> int:
        return int(self.db.execute(
            """INSERT INTO replay_experiments
                 (user_id, name, hypothesis, baseline_version_id,
                  variant_version_id, filter_json, split_strategy,
                  dev_ratio, repeat_count, status, hand_set_id,
                  hand_set_revision, kind, change_scope,
                  baseline_prompt_mode)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s)""",
            (self.user_id, name[:100], hypothesis,
             int(baseline_version_id), int(variant_version_id),
             json_dumps(filter_payload or {}), split_strategy,
             float(dev_ratio), int(repeat_count),
             hand_set_id, hand_set_revision, (kind or "ab")[:16],
             (change_scope or "")[:32] or None,
             (baseline_prompt_mode or "stored")[:24])))

    def get(self, experiment_id: int) -> dict | None:
        row = self.db.one(
            "SELECT * FROM replay_experiments WHERE id=%s AND user_id=%s",
            (int(experiment_id), self.user_id))
        if row is None:
            return None
        row["filter"] = json_load(row.get("filter_json"), {}) or {}
        return row

    def list(self, limit: int = 100) -> list[dict]:
        rows = self.db.query(
            """SELECT * FROM replay_experiments WHERE user_id=%s
                ORDER BY id DESC LIMIT %s""",
            (self.user_id, int(limit)))
        for row in rows:
            row["filter"] = json_load(row.get("filter_json"), {}) or {}
        return rows

    def add_hands(self, experiment_id: int,
                  rows: list[tuple[int, str, str, str | None]]) -> int:
        written = 0
        for hand_id, fingerprint, split, stratum in rows:
            self.db.execute(
                """INSERT INTO replay_experiment_hands
                     (experiment_id, hand_id, fingerprint, split, stratum)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     fingerprint=VALUES(fingerprint),
                     split=VALUES(split),
                     stratum=VALUES(stratum)""",
                (int(experiment_id), int(hand_id), fingerprint,
                 split[:16], (stratum or "")[:64] or None))
            written += 1
        return written

    def hands(self, experiment_id: int,
              split: str | None = None) -> list[dict]:
        sql = ("SELECT hand_id, fingerprint, split, stratum "
               "FROM replay_experiment_hands WHERE experiment_id=%s")
        params: list[Any] = [int(experiment_id)]
        if split:
            sql += " AND split=%s"
            params.append(split)
        sql += " ORDER BY hand_id"
        return self.db.query(sql, tuple(params))

    def set_job(self, experiment_id: int, job_id: int,
                status: str = "queued") -> None:
        self.db.execute(
            """UPDATE replay_experiments
                  SET job_id=%s, status=%s, started_at=%s
                WHERE id=%s AND user_id=%s""",
            (int(job_id), status[:16], now_cn(),
             int(experiment_id), self.user_id))
        # worker 用 replay_jobs.experiment_id 回写实验状态；任务创建 API
        # 不感知实验，所以在这里补关联。
        self.db.execute(
            "UPDATE replay_jobs SET experiment_id=%s WHERE id=%s",
            (int(experiment_id), int(job_id)))

    def contains_decision(self, experiment_id: int, decision_id: int) -> bool:
        row = self.db.one(
            """SELECT 1 AS ok
                 FROM replay_experiment_hands e
                 JOIN replay_decisions d
                   ON d.hand_id=e.hand_id
                  AND d.hand_fingerprint=e.fingerprint
                WHERE e.experiment_id=%s AND d.id=%s LIMIT 1""",
            (int(experiment_id), int(decision_id)))
        return row is not None

    def set_status(self, experiment_id: int, status: str) -> None:
        finished = now_cn() if status in ("done", "failed", "archived") else None
        self.db.execute(
            """UPDATE replay_experiments
                  SET status=%s, finished_at=COALESCE(%s, finished_at)
                WHERE id=%s AND user_id=%s""",
            (status[:16], finished, int(experiment_id), self.user_id))

    def update_outcome(self, experiment_id: int, *, metrics: dict | None = None,
                       conclusion: str | None = None,
                       decision: str | None = None,
                       promoted_version_id: int | None = None) -> None:
        self.db.execute(
            """UPDATE replay_experiments
                  SET metrics_json=%s, conclusion=%s, decision=%s,
                      promoted_version_id=%s
                WHERE id=%s AND user_id=%s""",
            (json_dumps(metrics or {}), conclusion,
             (decision or "")[:16] or None, promoted_version_id,
             int(experiment_id), self.user_id))

    def result_rows(self, experiment_id: int, job_id: int,
                    split: str | None = None) -> list[dict]:
        sql = """SELECT e.split, r.version_id, r.bucket, r.source,
                        r.violation_tags, r.decision_id,
                        d.street, d.actual_action, d.actual_amount
                   FROM replay_experiment_hands e
                   JOIN replay_decisions d
                     ON d.hand_id=e.hand_id
                    AND d.hand_fingerprint=e.fingerprint
                   JOIN replay_results r ON r.decision_id=d.id
                   JOIN replay_jobs j ON j.id=r.job_id
                  WHERE e.experiment_id=%s AND j.id=%s"""
        params: list[Any] = [int(experiment_id), int(job_id)]
        if split:
            sql += " AND e.split=%s"
            params.append(split)
        return self.db.query(sql, tuple(params))

    def review_rows(self, experiment_id: int,
                    split: str | None = None) -> list[dict]:
        sql = """SELECT e.split, dm.verdict, dm.tags
                   FROM replay_experiment_hands e
                   JOIN replay_decisions d
                     ON d.hand_id=e.hand_id
                    AND d.hand_fingerprint=e.fingerprint
                   JOIN replay_decision_marks dm ON dm.decision_id=d.id
                  WHERE e.experiment_id=%s"""
        params: list[Any] = [int(experiment_id)]
        if split:
            sql += " AND e.split=%s"
            params.append(split)
        return self.db.query(sql, tuple(params))


class HandSetRepository:
    """手牌集：人工挑选 + 冻结 revision。"""

    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def create(self, *, name: str, description: str | None = None,
               purpose: str = "regression") -> int:
        try:
            return int(self.db.execute(
                """INSERT INTO replay_hand_sets
                     (user_id, name, description, purpose)
                   VALUES (%s,%s,%s,%s)""",
                (self.user_id, name[:100], description,
                 (purpose or "regression")[:32])))
        except IntegrityError as exc:
            raise ApiError("HAND_SET_NAME_EXISTS",
                           f"手牌集 {name} 已存在", 409) from exc

    def get(self, set_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_hand_sets WHERE id=%s AND user_id=%s",
            (int(set_id), self.user_id))

    def list(self, limit: int = 100) -> list[dict]:
        rows = self.db.query(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM replay_hand_set_items i
                        WHERE i.set_id=s.id) AS hand_count
                 FROM replay_hand_sets s
                WHERE s.user_id=%s AND s.archived=0
                ORDER BY s.updated_at DESC, s.id DESC LIMIT %s""",
            (self.user_id, int(limit)))
        return rows

    def items(self, set_id: int) -> list[dict]:
        return self.db.query(
            """SELECT * FROM replay_hand_set_items
                WHERE set_id=%s ORDER BY priority ASC, added_at ASC""",
            (int(set_id),))

    def update_meta(self, set_id: int, *, name: str | None = None,
                    description: str | None = None,
                    purpose: str | None = None,
                    archived: bool | None = None) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name=%s")
            params.append(name[:100])
        if description is not None:
            sets.append("description=%s")
            params.append(description)
        if purpose is not None:
            sets.append("purpose=%s")
            params.append(purpose[:32])
        if archived is not None:
            sets.append("archived=%s")
            params.append(1 if archived else 0)
        if sets:
            params += [int(set_id), self.user_id]
            self.db.execute(
                f"UPDATE replay_hand_sets SET {', '.join(sets)} "
                "WHERE id=%s AND user_id=%s", tuple(params))
        return self.get(set_id)

    def add_items(self, set_id: int, items: list[dict]) -> int:
        written = 0
        for item in items:
            hand_id = item.get("handId") or item.get("hand_id")
            fingerprint = item.get("fingerprint") or ""
            if not hand_id or not fingerprint:
                continue
            self.db.execute(
                """INSERT INTO replay_hand_set_items
                     (set_id, hand_id, fingerprint, role, priority,
                      stratum, expect_json, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     fingerprint=VALUES(fingerprint), role=VALUES(role),
                     priority=VALUES(priority), stratum=VALUES(stratum),
                     expect_json=VALUES(expect_json), note=VALUES(note)""",
                (int(set_id), int(hand_id), str(fingerprint)[:64],
                 (str(item.get("role") or "regression"))[:16],
                 int(item.get("priority") or 1),
                 (str(item.get("stratum") or "")[:64] or None),
                 json_dumps(item.get("expect") or {}),
                 (str(item.get("note") or "")[:255] or None)))
            written += 1
        if written:
            self.db.execute(
                """UPDATE replay_hand_sets
                      SET revision=revision+1, frozen_at=NULL
                    WHERE id=%s AND user_id=%s""",
                (int(set_id), self.user_id))
        return written

    def remove_items(self, set_id: int, hand_ids: list[int]) -> int:
        ids = [int(x) for x in hand_ids if int(x)]
        if not ids:
            return 0
        placeholders = ",".join(["%s"] * len(ids))
        removed = self.db.execute(
            f"""DELETE FROM replay_hand_set_items
                 WHERE set_id=%s AND hand_id IN ({placeholders})""",
            (int(set_id), *ids))
        if removed:
            self.db.execute(
                """UPDATE replay_hand_sets
                      SET revision=revision+1, frozen_at=NULL
                    WHERE id=%s AND user_id=%s""",
                (int(set_id), self.user_id))
        return removed

    def freeze(self, set_id: int) -> dict | None:
        self.db.execute(
            """UPDATE replay_hand_sets SET frozen_at=%s
                WHERE id=%s AND user_id=%s""",
            (now_cn(), int(set_id), self.user_id))
        return self.get(set_id)


class PairReviewRepository:
    """成对人工偏好：A/B/same/unsure。"""

    def __init__(self, db: Database, user_id: int = 0):
        self.db = db
        self.user_id = user_id

    def upsert(self, experiment_id: int, decision_id: int, *,
               preferred: str, tags: list[str] | None = None,
               note: str | None = None) -> dict:
        self.db.execute(
            """INSERT INTO replay_pair_reviews
                 (user_id, experiment_id, decision_id, preferred, tags, note)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                 preferred=VALUES(preferred), tags=VALUES(tags),
                 note=VALUES(note)""",
            (self.user_id, int(experiment_id), int(decision_id),
             preferred[:8], json_dumps(tags or []), (note or "")[:4000] or None))
        return self.get(experiment_id, decision_id) or {}

    def get(self, experiment_id: int, decision_id: int) -> dict | None:
        return self.db.one(
            """SELECT * FROM replay_pair_reviews
                WHERE user_id=%s AND experiment_id=%s AND decision_id=%s""",
            (self.user_id, int(experiment_id), int(decision_id)))

    def for_experiment(self, experiment_id: int) -> dict[int, dict]:
        rows = self.db.query(
            """SELECT * FROM replay_pair_reviews
                WHERE user_id=%s AND experiment_id=%s""",
            (self.user_id, int(experiment_id)))
        return {int(row["decision_id"]): row for row in rows}


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
                prompt_version, prompt_sha256, reason_full)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
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
                payload.get("prompt_sha256"), payload.get("reason_full"),
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


# ─────────────────────────── 期望库（spot spec） ───────────────────────────

_SPEC_CHAIN = """
       (SELECT COUNT(*) FROM replay_spot_specs x
         WHERE x.name = s.name) AS revision_count,
       (SELECT x.id FROM replay_spot_specs x
         WHERE x.name = s.name AND x.status = 'active'
         ORDER BY x.revision DESC LIMIT 1) AS active_revision_id"""


class SpotSpecRepository:
    """`replay_spot_specs` 的读写：revision 只增不改，旧行只读。"""

    def __init__(self, db: Database):
        self.db = db

    def create(self, *, name: str, revision: int, status: str,
               content: dict, note: str | None,
               supersedes: int | None) -> int:
        bucket = content["bucket"]
        spec = {key: value for key, value in content.items()
                if key != "bucket"}
        try:
            return self.db.execute(
                """INSERT INTO replay_spot_specs
                     (name, revision, status, bucket_json, spec_json, note,
                      supersedes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (name, revision, status, json_dumps(bucket),
                 json_dumps(spec), note or None, supersedes))
        except IntegrityError as exc:
            raise ApiError(
                "SPEC_REVISION_CONFLICT",
                f"期望 {name} 的 revision {revision} 已存在（并发保存？请重试）",
                409) from exc

    def get(self, spec_id: int) -> dict | None:
        return self.db.one(
            f"SELECT s.*,{_SPEC_CHAIN} FROM replay_spot_specs s "
            "WHERE s.id=%s", (spec_id,))

    def latest(self, name: str) -> dict | None:
        return self.db.one(
            f"SELECT s.*,{_SPEC_CHAIN} FROM replay_spot_specs s "
            "WHERE s.name=%s ORDER BY s.revision DESC LIMIT 1", (name,))

    def by_name(self, name: str) -> list[dict]:
        return self.db.query(
            f"SELECT s.*,{_SPEC_CHAIN} FROM replay_spot_specs s "
            "WHERE s.name=%s ORDER BY s.revision DESC", (name,))

    def list_latest(self) -> list[dict]:
        return self.db.query(
            f"""SELECT s.*,{_SPEC_CHAIN}
                  FROM replay_spot_specs s
                  JOIN (SELECT name, MAX(revision) AS revision
                          FROM replay_spot_specs GROUP BY name) m
                    ON m.name = s.name AND m.revision = s.revision
                 ORDER BY s.name""")

    def activate(self, spec_id: int, name: str) -> int:
        """把该 revision 置 active，同名的旧 active 退回 draft（单条 SQL）。"""
        return self.db.execute(
            """UPDATE replay_spot_specs
                  SET status = CASE WHEN id = %s THEN 'active' ELSE 'draft' END
                WHERE name = %s AND (id = %s OR status = 'active')""",
            (spec_id, name, spec_id))

    def set_status(self, spec_id: int, status: str) -> int:
        return self.db.execute(
            "UPDATE replay_spot_specs SET status=%s WHERE id=%s",
            (status, spec_id))

    def delete(self, spec_id: int) -> int:
        return self.db.execute(
            "DELETE FROM replay_spot_specs WHERE id=%s", (spec_id,))



# ──────────────────────────── canonical 手牌 ────────────────────────────

_CANDIDATE_SQL = """
    SELECT id, started_at, content_group
      FROM replay_canonical_hands
     WHERE hero_cards = %s AND board = %s AND is_synthetic = %s
       AND merged_into IS NULL
       AND started_at BETWEEN %s AND %s
"""

_SOURCE_JOIN_SQL = """
    SELECT hs.canonical_id, hs.source, hs.source_hand_id, hs.fingerprint,
           hs.is_primary, hs.linked_by,
           ch.content_group, ch.primary_source, ch.merged_into
      FROM replay_hand_sources hs
      JOIN replay_canonical_hands ch ON ch.id = hs.canonical_id
     WHERE hs.source = %s AND hs.source_hand_id = %s
"""

_PRIMARY_ROWS_SQL = """
    SELECT source, source_hand_id, is_primary
      FROM replay_hand_sources
     WHERE canonical_id = %s
"""

_INSERT_CANONICAL_SQL = """
    INSERT INTO replay_canonical_hands
           (content_group, primary_source, title, hero_cards, board,
            hero_position, started_at, is_synthetic)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_SOURCE_SQL = """
    INSERT INTO replay_hand_sources
           (canonical_id, source, source_hand_id, fingerprint, is_primary,
            linked_by)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE fingerprint = VALUES(fingerprint)
"""


class CaseRepository:
    """`replay_case_sets` / `replay_cases` 的读写。

    案例是「某个 canonical 手牌 + 某个决策点 + 写死的 spec_id」；案例集按
    (name, revision) 分版本，`frozen_at` 一置该版本就只读，要改只能从它升
    一个新 revision（`copy_set_to_new_revision`）。
    """

    DEFAULT_SET_NAME = "待整理"
    # DDL 里的 DEFAULT 'golden' 只是占位：create_set 强制显式传 purpose，
    # 不依赖库侧默认，避免「代码默认 exploration / 库默认 golden」两套口径。
    CASE_SET_PURPOSES = ("golden", "regression", "exploration", "coverage")

    _SET_SELECT = """SELECT s.*,
              (SELECT COUNT(*) FROM replay_cases c
                WHERE c.case_set_id = s.id) AS case_count,
              (SELECT COUNT(*) FROM replay_cases c
                WHERE c.case_set_id = s.id AND c.priority = 0) AS p0_count
         FROM replay_case_sets s"""

    def _frozen_error(self, row: dict) -> ApiError:
        return ApiError(
            "CASE_SET_FROZEN",
            f"案例集 {row.get('name')} v{row.get('revision')} 已冻结，"
            "不能再改；请从它建新版本（POST /api/case-sets/{id}/revisions）",
            409)

    def list_sets(self, *, limit: int = 200,
                  name: str | None = None) -> list[dict]:
        sql = self._SET_SELECT
        params: list[Any] = []
        if name:
            sql += " WHERE s.name = %s"
            params.append(name)
        sql += " ORDER BY s.name, s.revision DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def get_set(self, set_id: int) -> dict | None:
        return self.db.one(self._SET_SELECT + " WHERE s.id = %s",
                           (int(set_id),))

    def require_set(self, set_id: int) -> dict:
        row = self.get_set(set_id)
        if row is None:
            raise ApiError("CASE_SET_NOT_FOUND", f"案例集 {set_id} 不存在", 404)
        return row

    def require_unfrozen(self, set_id: int) -> dict:
        row = self.require_set(set_id)
        if row.get("frozen_at"):
            raise self._frozen_error(row)
        return row

    def set_by_name_revision(self, name: str,
                             revision: int = 1) -> dict | None:
        return self.db.one(
            self._SET_SELECT + " WHERE s.name = %s AND s.revision = %s",
            (name, int(revision)))

    def next_revision(self, name: str) -> int:
        return int(self.db.scalar(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM replay_case_sets"
            " WHERE name = %s", (name,)) or 1)

    def create_set(self, *, name: str, purpose: str) -> dict:
        """建第 1 版案例集；同名已存在时报错（新版本走 revisions）。"""
        clean = (name or "").strip()
        if not clean:
            raise ApiError("INVALID_CASE_SET", "案例集 name 不能为空", 422)
        if len(clean) > 100:
            raise ApiError("INVALID_CASE_SET",
                           "案例集 name 最多 100 个字符", 422)
        try:
            new_id = int(self.db.execute(
                """INSERT INTO replay_case_sets (revision, name, purpose)
                   VALUES (1, %s, %s)""",
                (clean, (purpose or "exploration")[:16])))
        except IntegrityError as exc:
            raise ApiError(
                "CASE_SET_EXISTS",
                f"案例集 {clean} 已存在；要改内容请从它建新版本", 409) from exc
        row = self.get_set(new_id)
        return row or {}

    def update_set(self, set_id: int, *, name: str | None = None,
                   purpose: str | None = None) -> dict:
        self.require_unfrozen(set_id)
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            clean = (name or "").strip()
            if not clean or len(clean) > 100:
                raise ApiError("INVALID_CASE_SET",
                               "案例集 name 需为 1–100 个字符", 422)
            sets.append("name=%s")
            params.append(clean)
        if purpose is not None:
            sets.append("purpose=%s")
            params.append((purpose or "exploration")[:16])
        if sets:
            params.append(int(set_id))
            try:
                self.db.execute(
                    f"UPDATE replay_case_sets SET {', '.join(sets)} WHERE id=%s",
                    tuple(params))
            except IntegrityError as exc:
                raise ApiError(
                    "CASE_SET_EXISTS",
                    f"案例集 {name} 已存在；要改内容请从它建新版本",
                    409) from exc
        return self.get_set(set_id) or {}

    def freeze_set(self, set_id: int) -> dict:
        row = self.require_set(set_id)
        if row.get("frozen_at"):
            raise self._frozen_error(row)
        self.db.execute(
            "UPDATE replay_case_sets SET frozen_at=%s WHERE id=%s",
            (now_cn(), int(set_id)))
        return self.get_set(set_id) or {}

    def copy_set_to_new_revision(self, set_id: int) -> dict:
        """把任一 revision（含已冻结）整体复制成下一个 revision（未冻结）。"""
        row = self.require_set(set_id)
        revision = self.next_revision(row["name"])
        new_id = int(self.db.execute(
            """INSERT INTO replay_case_sets (revision, name, purpose)
               VALUES (%s, %s, %s)""",
            (revision, row["name"], row.get("purpose") or "exploration")))
        self.db.execute(
            """INSERT INTO replay_cases
                   (case_set_id, canonical_id, decision_key, spec_id, priority,
                    overrides_json, status, note)
               SELECT %s, canonical_id, decision_key, spec_id, priority,
                      overrides_json, status, note
                 FROM replay_cases WHERE case_set_id = %s""",
            (new_id, int(set_id)))
        return self.get_set(new_id) or {}

    def __init__(self, db: Database):
        self.db = db

    def list_for_canonical(self, canonical_id: int) -> list[dict]:
        return self.db.query(
            """SELECT c.*, s.name AS set_name, s.revision AS set_revision,
                      s.frozen_at AS set_frozen_at
                 FROM replay_cases c
                 JOIN replay_case_sets s ON s.id = c.case_set_id
                WHERE c.canonical_id = %s
                ORDER BY c.created_at DESC, c.id DESC""",
            (int(canonical_id),))

    def counts_by_canonical(self) -> dict[int, int]:
        rows = self.db.query(
            """SELECT canonical_id, COUNT(*) AS n FROM replay_cases
                GROUP BY canonical_id""")
        return {int(r["canonical_id"]): int(r["n"]) for r in rows}

    def get(self, case_set_id: int, canonical_id: int,
            decision_key: str) -> dict | None:
        return self.db.one(
            """SELECT * FROM replay_cases
                WHERE case_set_id=%s AND canonical_id=%s AND decision_key=%s""",
            (int(case_set_id), int(canonical_id), decision_key))

    def case_set(self, case_set_id: int) -> dict | None:
        return self.db.one("SELECT * FROM replay_case_sets WHERE id=%s",
                           (int(case_set_id),))

    def get_by_id(self, case_id: int) -> dict | None:
        return self.db.one(
            """SELECT c.*, s.name AS set_name, s.revision AS set_revision,
                      s.frozen_at AS set_frozen_at
                 FROM replay_cases c
                 JOIN replay_case_sets s ON s.id = c.case_set_id
                WHERE c.id = %s""",
            (int(case_id),))

    def items(self, case_set_id: int) -> list[dict]:
        """案例集详情用：案例 + 手牌摘要 + 决策点的桶维度。"""
        from .versioning import CURRENT_RECONSTRUCT_VERSION

        return self.db.query(
            """SELECT c.*, s.name AS set_name, s.revision AS set_revision,
                      s.frozen_at AS set_frozen_at,
                      ch.hero_cards, ch.board, ch.hero_position,
                      d.position_bucket, d.hand_tier, d.board_texture,
                      d.pot_type, d.facing, d.opponent_profile,
                      d.action_index
                 FROM replay_cases c
                 JOIN replay_case_sets s ON s.id = c.case_set_id
                 LEFT JOIN replay_canonical_hands ch ON ch.id = c.canonical_id
                 LEFT JOIN replay_decisions d
                        ON d.canonical_id = c.canonical_id
                       AND d.street = SUBSTRING_INDEX(c.decision_key, ':', 1)
                       AND d.seq = CAST(SUBSTRING_INDEX(c.decision_key, ':', -1)
                                        AS UNSIGNED)
                       AND d.reconstruct_version = %s
                WHERE c.case_set_id = %s
                ORDER BY c.priority ASC, c.id ASC""",
            (CURRENT_RECONSTRUCT_VERSION, int(case_set_id)))

    def case_with_set(self, case_id: int) -> dict | None:
        return self.db.one(
            """SELECT c.*, s.name AS set_name, s.revision AS set_revision,
                      s.frozen_at AS set_frozen_at
                 FROM replay_cases c
                 JOIN replay_case_sets s ON s.id = c.case_set_id
                WHERE c.id = %s""",
            (int(case_id),))

    def _case_for_write(self, case_set_id: int, case_id: int) -> dict:
        row = self.case_with_set(case_id)
        if row is None or int(row["case_set_id"]) != int(case_set_id):
            raise ApiError("CASE_NOT_FOUND", f"案例 {case_id} 不存在", 404)
        if row.get("set_frozen_at"):
            raise self._frozen_error({
                "name": row.get("set_name"),
                "revision": row.get("set_revision"),
            })
        return row

    def update_case(self, case_set_id: int, case_id: int, *,
                    spec_id: int | None = None, priority: int | None = None,
                    status: str | None = None, note: str | None = None,
                    overrides: dict | None = None) -> dict:
        self._case_for_write(case_set_id, case_id)
        sets: list[str] = []
        params: list[Any] = []
        if spec_id is not None:
            sets.append("spec_id=%s")
            params.append(int(spec_id))
        if priority is not None:
            sets.append("priority=%s")
            params.append(int(priority))
        if status is not None:
            sets.append("status=%s")
            params.append(status)
        if note is not None:
            sets.append("note=%s")
            params.append((note or "").strip()[:255] or None)
        if overrides is not None:
            sets.append("overrides_json=%s")
            params.append(json_dumps(overrides) if overrides else None)
        if sets:
            params.append(int(case_id))
            self.db.execute(
                f"UPDATE replay_cases SET {', '.join(sets)} WHERE id=%s",
                tuple(params))
        return self.get_by_id(case_id) or {}

    def delete_case(self, case_set_id: int, case_id: int) -> None:
        self._case_for_write(case_set_id, case_id)
        self.db.execute("DELETE FROM replay_cases WHERE id=%s", (int(case_id),))

    def decisions_for_bucket(self, bucket: dict, *, limit: int = 50,
                             exclude: list[tuple[int, str, int]] | None = None
                             ) -> list[dict]:
        """按桶维度抽最近决策点（样例池：只取已回填 canonical 的）。"""
        from .versioning import CURRENT_RECONSTRUCT_VERSION

        where = ["d.canonical_id IS NOT NULL",
                 "d.reconstruct_version = %s"]
        params: list[Any] = [CURRENT_RECONSTRUCT_VERSION]
        for dimension in BUCKET_ORDER:
            values = [v for v in ((bucket or {}).get(dimension) or []) if v not in (None, "")]
            if values:
                marks = ", ".join(["%s"] * len(values))
                where.append(f"d.{dimension} IN ({marks})")
                params += list(values)
        if exclude:
            marks = ", ".join(["(%s,%s,%s)"] * len(exclude))
            where.append(f"NOT ((d.canonical_id, d.street, d.seq) IN ({marks}))")
            for canonical_id, street, seq in exclude:
                params += [int(canonical_id), street, int(seq)]
        params.append(int(limit))
        return self.db.query(
            f"""SELECT d.canonical_id, d.street, d.seq, d.action_index,
                       d.position_bucket, d.hand_tier, d.board_texture,
                       d.pot_type, d.facing, d.opponent_profile
                  FROM replay_decisions d
                 WHERE {' AND '.join(where)}
                 ORDER BY d.id DESC
                 LIMIT %s""",
            tuple(params))

    def ensure_open_set(self) -> dict:
        """拿一张未冻结的「待整理」集；没有就建第 1 版。"""
        row = self.db.one(
            """SELECT * FROM replay_case_sets
                WHERE name=%s AND frozen_at IS NULL
                ORDER BY revision DESC LIMIT 1""",
            (self.DEFAULT_SET_NAME,))
        if row is not None:
            return row
        revision = int(self.db.scalar(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM replay_case_sets"
            " WHERE name=%s", (self.DEFAULT_SET_NAME,)) or 1)
        self.db.execute(
            """INSERT INTO replay_case_sets (revision, name, purpose)
               VALUES (%s, %s, 'exploration')""",
            (revision, self.DEFAULT_SET_NAME))
        return self.db.one(
            "SELECT * FROM replay_case_sets WHERE name=%s AND revision=%s",
            (self.DEFAULT_SET_NAME, revision))

    def create(self, *, canonical_id: int, decision_key: str, spec_id: int,
               case_set_id: int | None = None, priority: int = 1,
               overrides: dict | None = None,
               note: str | None = None) -> tuple[dict, bool]:
        """建一条案例；同 (集, canonical, 决策点) 已存在就返回原行。"""
        case_set = (self.case_set(case_set_id) if case_set_id
                    else self.ensure_open_set())
        if case_set is None:
            raise ApiError("CASE_SET_NOT_FOUND",
                           f"案例集 {case_set_id} 不存在", 404)
        if case_set.get("frozen_at"):
            raise self._frozen_error(case_set)
        existing = self.get(case_set["id"], canonical_id, decision_key)
        if existing is not None:
            return existing, False
        try:
            self.db.execute(
                """INSERT INTO replay_cases
                   (case_set_id, canonical_id, decision_key, spec_id, priority,
                    overrides_json, status, note)
                   VALUES (%s,%s,%s,%s,%s,%s,'draft',%s)""",
                (case_set["id"], int(canonical_id), decision_key,
                 int(spec_id), int(priority),
                 json_dumps(overrides) if overrides else None,
                 (note or "").strip()[:255] or None))
        except IntegrityError:
            # 并发重复：唯一键兜底，读回已有的那条
            existing = self.get(case_set["id"], canonical_id, decision_key)
            if existing is not None:
                return existing, False
            raise
        created = self.get(case_set["id"], canonical_id, decision_key)
        return created or {}, True


class CanonicalHandRepository:
    """`replay_canonical_hands` / `replay_hand_sources` 的读写。

    一条源记录 = 某个来源里的一手牌（复盘 review_hands.id、导入 id、构造 id）。
    upsert 按 (source, source_hand_id) 幂等；同内容（底牌 + 公共牌 + 30 分钟内）
    的源记录挂到同一个 canonical id 上，并列保存、互不覆盖（设计 §3.4）。

    primary：新建时就是这条源；之后新来的源记录只有在默认优先级更高时才顶替
    （review > live > synthetic），否则保留当前 primary —— 人工切换不会被
    一次回填或一条低优先级来源悄悄改回去。重新跑回填更不会：已存在的源记录
    直接跳过，只校正 fingerprint；若它指向的行被人工软合并过，源记录整体改挂
    到保留行（`_resolve_merged`），旧行不删。

    已知边界（都不影响读路径，留给第 2 期）：写入是逐条自动提交、不开显式事务，
    中途失败会留下一条没有源记录的 canonical 空行（下次同内容 upsert 会接上它，
    不产生错误数据）；「同内容超过 3 条 / 时间跨越」进待确认页的规则（§3.4 规则 4）
    随合并/拆分 UI 一起做。
    """

    def __init__(self, db: Database):
        self.db = db

    # ── 读 ──

    def get(self, canonical_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM replay_canonical_hands WHERE id=%s",
            (int(canonical_id),))

    def source_of(self, source: str, source_hand_id: int) -> dict | None:
        return self.db.one(_SOURCE_JOIN_SQL,
                           (validate_source(source), int(source_hand_id)))

    def sources(self, canonical_id: int) -> list[dict]:
        """一个 canonical 下的全部源记录；primary 排第一，其次按来源、id。"""
        return self.db.query(
            """SELECT * FROM replay_hand_sources
                WHERE canonical_id = %s
                ORDER BY is_primary DESC, source, source_hand_id""",
            (int(canonical_id),))

    def list_all(self) -> list[dict]:
        """列表首屏用：全部未被软合并的 canonical（不含大字段，宽表很小）。"""
        return self.db.query(
            """SELECT id, content_group, primary_source, title, hero_cards,
                      board, hero_position, started_at, is_synthetic
                 FROM replay_canonical_hands
                WHERE merged_into IS NULL""")

    def all_sources(self) -> list[dict]:
        """列表首屏用：全部来源边（1191 行，轻量列）。"""
        return self.db.query(
            """SELECT canonical_id, source, source_hand_id, is_primary,
                      fingerprint
                 FROM replay_hand_sources
                ORDER BY canonical_id, is_primary DESC, source, source_hand_id""")

    def for_hand(self, hand_id: int) -> list[dict]:
        """反查某条源记录属于哪个 canonical（详情页入口用）。"""
        return self.db.query(
            """SELECT canonical_id, source, source_hand_id, is_primary
                 FROM replay_hand_sources WHERE source_hand_id = %s""",
            (int(hand_id),))

    # ── 写 ──

    def upsert_source(self, *, source: str, source_hand_id: int,
                      hero_cards: str | None = "", board: str | None = "",
                      fingerprint: str | None = None, started_at=None,
                      title: str | None = None, hero_position: str | None = None,
                      is_synthetic: bool = False,
                      linked_by: str = "auto") -> dict:
        """把一条源记录接到 canonical 上；返回 {canonical_id, created, ...}。"""
        src = validate_source(source)
        hand_id = _positive_id(source_hand_id)

        existing = self.source_of(src, hand_id)
        if existing is not None:
            canonical_id = self._resolve_merged(int(existing["canonical_id"]))
            if canonical_id != int(existing["canonical_id"]):
                # 人工软合并过：源记录整体改挂到保留行，旧行不删（§3.4 规则 5）
                self.db.execute(
                    "UPDATE replay_hand_sources SET canonical_id=%s"
                    " WHERE source=%s AND source_hand_id=%s",
                    (canonical_id, existing["source"], hand_id))
                existing = self.source_of(src, hand_id) or existing
            self._fix_fingerprint(existing, fingerprint)
            return {"canonical_id": canonical_id,
                    "created": False,
                    "content_group": existing.get("content_group"),
                    "primary_source": existing.get("primary_source")}

        hero = normalize_hero_cards(hero_cards)
        board_text = normalize_board(board)
        started = as_datetime(started_at)
        # 构造手牌自成一列：source=synthetic 就算调用方忘了传 is_synthetic，
        # 也不会和真实手牌并到同一行（否则构造样本会污染真实统计）
        synthetic = 1 if (is_synthetic or src == "synthetic") else 0
        group = content_group_key(hero, board_text, started)

        target = self._match(hero, board_text, started, synthetic)
        if target is None:
            canonical_id = self._create_canonical(
                content_group=group, primary_source=src,
                title=(title or "").strip() or None,
                hero_cards=hero, board=board_text,
                hero_position=(hero_position or "").strip() or None,
                started_at=started, is_synthetic=synthetic)
            self._insert_source(canonical_id, src, hand_id, fingerprint,
                                linked_by, primary=1)
            return {"canonical_id": canonical_id, "created": True,
                    "content_group": group, "primary_source": src}

        canonical_id = int(target["id"])
        if group and not target.get("content_group"):
            # 老行可能没算过组键（早期回填缺时间）；补上，别的列不动
            self.db.execute(
                """UPDATE replay_canonical_hands SET content_group=%s
                    WHERE id=%s AND content_group IS NULL""",
                (group, canonical_id))
        else:
            group = target.get("content_group") or group
        self._insert_source(canonical_id, src, hand_id, fingerprint,
                            linked_by, primary=0)
        primary = self._refresh_primary(
            canonical_id, {"source": src, "source_hand_id": hand_id})
        return {"canonical_id": canonical_id, "created": False,
                "content_group": group, "primary_source": primary or src}

    def set_primary(self, canonical_id: int, source: str,
                    source_hand_id: int | None = None) -> dict:
        """人工指定 primary；不给 hand_id 时取该来源 id 最小的一条。"""
        src = validate_source(source)
        canonical_id = int(canonical_id)
        if source_hand_id is None:
            row = self.db.one(
                """SELECT source_hand_id FROM replay_hand_sources
                    WHERE canonical_id=%s AND source=%s
                    ORDER BY source_hand_id LIMIT 1""",
                (canonical_id, src))
            if row is None:
                raise ApiError("SOURCE_NOT_FOUND",
                               f"canonical {canonical_id} 没有 {src} 来源记录", 404)
            hand_id = int(row["source_hand_id"])
        else:
            hand_id = _positive_id(source_hand_id)
            found = self.db.one(
                """SELECT 1 AS ok FROM replay_hand_sources
                    WHERE canonical_id=%s AND source=%s AND source_hand_id=%s""",
                (canonical_id, src, hand_id))
            if found is None:
                raise ApiError(
                    "SOURCE_NOT_FOUND",
                    f"canonical {canonical_id} 没有 {src}:{hand_id} 这条来源记录", 404)
        self._write_primary(canonical_id, src, hand_id)
        return {"canonical_id": canonical_id, "primary_source": src}

    # ── 内部 ──

    def _resolve_merged(self, canonical_id: int) -> int:
        """沿 `merged_into` 走到人工合并后保留的那一行（链最多走 8 层）。

        软合并只改指针、不删行；源记录跟着指针走，历史和审计都还在（§3.4 规则 5）。
        """
        current = int(canonical_id)
        for _ in range(MERGE_CHAIN_LIMIT):
            row = self.db.one(
                "SELECT merged_into FROM replay_canonical_hands WHERE id=%s",
                (current,))
            if row is None or row.get("merged_into") in (None, 0):
                return current
            current = int(row["merged_into"])
        return current

    def _match(self, hero: str, board: str, started, synthetic: int):
        if started is None:
            return None
        rows = self.db.query(
            _CANDIDATE_SQL,
            (hero, board, synthetic, started - MATCH_WINDOW,
             started + MATCH_WINDOW))
        return pick_candidate(rows, started)

    def _create_canonical(self, *, content_group, primary_source, title,
                          hero_cards, board, hero_position, started_at,
                          is_synthetic) -> int:
        return int(self.db.execute(
            _INSERT_CANONICAL_SQL,
            (content_group, primary_source, title, hero_cards,
             board, hero_position, started_at, is_synthetic)))

    def _insert_source(self, canonical_id: int, source: str, hand_id: int,
                       fingerprint: str | None, linked_by: str,
                       primary: int) -> None:
        self.db.execute(_INSERT_SOURCE_SQL,
                        (canonical_id, source, hand_id, fingerprint, primary,
                         linked_by))

    def _fix_fingerprint(self, existing: dict, fingerprint: str | None) -> None:
        if not fingerprint or fingerprint == existing.get("fingerprint"):
            return
        self.db.execute(
            """UPDATE replay_hand_sources SET fingerprint=%s
                WHERE source=%s AND source_hand_id=%s""",
            (fingerprint, existing["source"], int(existing["source_hand_id"])))

    def _refresh_primary(self, canonical_id: int, added: dict) -> str | None:
        rows = self.db.query(_PRIMARY_ROWS_SQL, (canonical_id,))
        if not rows:
            return None
        current = next((r for r in rows if int(r.get("is_primary") or 0) == 1), None)
        if current is None:
            chosen = primary_row(rows)
        elif rank_key(added) < rank_key(current):
            chosen = added
        else:
            return str(current["source"])
        if chosen is None:
            return None
        self._write_primary(canonical_id, str(chosen["source"]),
                           int(chosen["source_hand_id"]))
        return str(chosen["source"])

    def _write_primary(self, canonical_id: int, source: str, hand_id: int) -> None:
        self.db.execute(
            """UPDATE replay_hand_sources
                  SET is_primary = CASE WHEN source=%s AND source_hand_id=%s
                                        THEN 1 ELSE 0 END
                WHERE canonical_id=%s""",
            (source, hand_id, canonical_id))
        self.db.execute(
            "UPDATE replay_canonical_hands SET primary_source=%s WHERE id=%s",
            (source, canonical_id))


def _positive_id(value) -> int:
    try:
        ident = int(value)
    except (TypeError, ValueError):
        ident = 0
    if ident <= 0:
        raise ApiError("BAD_SOURCE_ID", f"源记录 id 必须是正整数：{value!r}", 422)
    return ident

