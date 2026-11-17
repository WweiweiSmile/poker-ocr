"""查询 / 版本 / 任务 / 导出服务。

API 层只做参数校验与 JSON 序列化；所有判断都在这里，便于单测和 CLI 复用。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from poker_ocr.advice import build_situation
from poker_ocr.advice import AISettings
from poker_ocr.buckets import bucket_label

from . import case_seed, cases as case_lib, library, run_worker, scoring, specs
from .config import Settings
from .db import Database
from .decisions import (
    APPROX, EXACT, INVALID, UNKNOWN_ACTION, HandAnalysis, analyze_hand,
    call_amount, cards_of, decision_facts, hand_fingerprint, snapshot_to_dict,
)
from .engine import (
    audit, classify, decision_to_public, fmt_option, summarize_version,
)
from .errors import ApiError
from .importer import (
    SOURCE_LABELS, SOURCE_LIVE, SOURCE_REVIEW, normalize_live_hand,
    parse_hand_text,
)
from .canonical import CANONICAL_HAND_ID_BASE
from .repository import (
    HAND_SOURCE_DB, HAND_SOURCE_LABELS, HAND_SOURCE_LIVE, HAND_SOURCE_REVIEW,
    IMPORTED_HAND_ID_BASE, CaseRepository, CanonicalHandRepository,
    DecisionMarkRepository,
    DecisionRepository, ExperimentRepository, HandRepository,
    HandSetRepository, ImportBatchRepository, JobRepository,
    LiveAdviceRepository, MarkRepository, PairReviewRepository,
    ResultRepository, SpotSpecRepository, VersionRepository,
)
from .run_repository import RunItemRepository, RunRepository
from .timeline import build_timeline
from .flags import compute_flags, mask_for, parse_flags
from .util import (
    BOARD_LEN, STREET_LABELS, iso, json_load, median, now_cn, split_board,
)
from .versioning import (
    CURRENT_ADVICE_VERSION, CURRENT_RECONSTRUCT_VERSION,
    build_settings, config_hash, config_payload, default_version_name,
    engine_state, has_key, normalize_table_dynamics, provider_from_url,
    public_version, run_blockers, table_dynamics_sha, validate_config_values,
    PROMPT_STYLE_VALUES,
)

log = logging.getLogger("replay.service")

DEFAULT_TAGS = ["典型", "边界", "漏点", "坚果"]
MARK_STATUSES = ("todo", "confirmed", "disputed")
RUN_STATUSES = ("queued", "running", "succeeded", "partial", "failed", "canceled")


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _parse_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("1", "true", "yes", "on"):
        return True
    if str(value).lower() in ("0", "false", "no", "off"):
        return False
    return None


def _parse_date(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return dt.datetime.fromisoformat(text)
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _group_filters(query: dict) -> dict:
    """把 URL 查询串解析成 `library.matches_filters` 需要的形状。"""
    source_values = {library.LEGACY_SOURCES.get(s.lower())
                     for s in _as_list(query.get("source"))}
    source_values.discard(None)
    flag_names = parse_flags(query.get("flags"))
    try:
        min_score = (int(query["minScore"])
                     if query.get("minScore") not in (None, "") else None)
    except (TypeError, ValueError):
        min_score = None
    try:
        not_run_days = (int(query["notRunDays"])
                        if query.get("notRunDays") not in (None, "") else None)
    except (TypeError, ValueError):
        not_run_days = None
    buckets = {}
    for dimension in library.DIMENSIONS:
        values = {v for v in _as_list(query.get(dimension)) if v}
        if values:
            buckets[dimension] = values
    return {
        "favorite": _parse_bool(query.get("favorite")),
        "sources": source_values,
        "positions": {p.upper() for p in _as_list(query.get("position"))},
        "results": {p.lower() for p in _as_list(query.get("result"))},
        "tags": [t.lower() for t in _as_list(query.get("tag"))],
        "qualities": {q.lower() for q in _as_list(query.get("quality"))},
        "hasRun": _parse_bool(query.get("hasRun")),
        "hasCase": _parse_bool(query.get("hasCase")),
        "flagMask": mask_for(flag_names),
        "minScore": min_score,
        "keyword": str(query.get("keyword") or "").strip().lower(),
        "dateFrom": _parse_date(query.get("dateFrom")),
        "dateTo": _parse_date(query.get("dateTo")),
        "notRunDays": not_run_days,
        "buckets": buckets,
    }


def _median_or_none(values: list[float]) -> float | None:
    m = median(values)
    return round(m, 1) if m is not None else None


class ReplayService:
    def __init__(self, settings: Settings, db: Database, runner=None):
        self.settings = settings
        self.db = db
        self.runner = runner
        self._lock = threading.Lock()
        self._analysis_cache: dict[tuple, HandAnalysis] = {}

        self.hand_repo = HandRepository(db, settings.hand_user_id,
                                        settings.console_user_id)
        self.import_repo = self.hand_repo.imports
        self.batch_repo = ImportBatchRepository(db, settings.console_user_id)
        self.mark_repo = MarkRepository(db, settings.console_user_id)
        self.version_repo = VersionRepository(db, settings.console_user_id)
        self.decision_repo = DecisionRepository(db)
        self.decision_mark_repo = DecisionMarkRepository(
            db, settings.console_user_id)
        self.job_repo = JobRepository(db, settings.console_user_id)
        self.experiment_repo = ExperimentRepository(db, settings.console_user_id)
        self.result_repo = ResultRepository(db)
        self.live_advice_repo = LiveAdviceRepository(db)
        self.hand_set_repo = HandSetRepository(db, settings.console_user_id)
        self.spec_repo = SpotSpecRepository(db)
        self.pair_review_repo = PairReviewRepository(db, settings.console_user_id)
        self.canonical_repo = CanonicalHandRepository(db)
        self.case_repo = CaseRepository(db)
        self.run_repo = RunRepository(db)
        self.run_item_repo = RunItemRepository(db)
        self.run_worker = None

    # ─────────────────────────── 基础查询 ───────────────────────────

    def analyze(self, row: dict) -> HandAnalysis:
        advices = row.get("advices") if isinstance(row.get("advices"), list) else None
        key = (int(row.get("id") or 0), iso(row.get("updated_at")),
               row.get("hero_cards"), row.get("board"),
               bool(row.get("streets")), CURRENT_RECONSTRUCT_VERSION,
               # advices 会改决策点（补「英雄动作缺录」的点），必须进 key：
               # 先按无 advices 的行算过，再拿带 advices 的行重算会命中旧结果。
               tuple((str(a.get("street") or ""), a.get("action_index"))
                     for a in (advices or []) if isinstance(a, dict)))
        with self._lock:
            cached = self._analysis_cache.get(key)
        if cached is not None:
            return cached
        try:
            analysis = analyze_hand(row)
        except Exception as exc:  # noqa: BLE001
            log.exception("重建手牌 %s 失败：%s", row.get("id"), exc)
            analysis = HandAnalysis(
                hand_id=int(row.get("id") or 0),
                fingerprint=hand_fingerprint({
                    "id": row.get("id"), "heroCards": row.get("hero_cards"),
                    "board": row.get("board"), "tableSize": row.get("table_size"),
                    "heroPosition": row.get("hero_position"),
                    "heroStackBb": row.get("hero_stack_bb"),
                    "smallBlindBb": row.get("small_blind_bb"),
                    "bigBlindBb": row.get("big_blind_bb"),
                    "anteBb": row.get("ante_bb"),
                    "bombPotBb": row.get("bomb_pot_bb"),
                    "villains": json_load(row.get("villains"), []),
                    "streets": json_load(row.get("streets"), []),
                }),
                decisions=[], quality=INVALID,
                warnings=[f"重建异常：{type(exc).__name__}: {exc}"],
                invalid_reason=f"重建异常：{type(exc).__name__}: {exc}",
            )
        with self._lock:
            if len(self._analysis_cache) > 5000:
                self._analysis_cache.clear()
            self._analysis_cache[key] = analysis
        return analysis

    def _mark(self, row: dict) -> dict:
        return {
            "favorite": bool(row.get("mark_favorite")),
            "tags": json_load(row.get("mark_tags"), []) or [],
            "status": row.get("mark_status") or "todo",
            "note": row.get("mark_note") or "",
        }

    def _last_run_map(self, hand_ids: list[int]) -> dict[int, dict]:
        latest = self.result_repo.latest_runs(hand_ids)
        all_version_ids: set[int] = set()
        for row in latest.values():
            all_version_ids.update(int(v) for v in
                                   (json_load(row.get("version_ids"), []) or []))
        names = self.result_repo.version_names(all_version_ids)
        out: dict[int, dict] = {}
        for hand_id, row in latest.items():
            vids = [int(v) for v in (json_load(row.get("version_ids"), []) or [])]
            out[int(hand_id)] = {
                "jobId": int(row["job_id"]),
                "at": iso(row.get("created_at")),
                "versions": [names.get(v, f"#{v}") for v in vids],
                "versionIds": vids,
                "repeat": int(row.get("repeat_count") or 1),
            }
        return out

    def _duplicate_groups(self, rows: list[dict]) -> dict[int, list[int]]:
        groups: dict[str, list[int]] = {}
        for row in rows:
            analysis = self.analyze(row)
            groups.setdefault(analysis.fingerprint, []).append(int(row["id"]))
        out: dict[int, list[int]] = {}
        for ids in groups.values():
            if len(ids) > 1:
                for hand_id in ids:
                    out[hand_id] = sorted(ids, reverse=True)
        return out

    def _row_item(self, row: dict, analysis: HandAnalysis,
                  last_run: dict | None,
                  duplicate_ids: list[int] | None) -> dict:
        mark = self._mark(row)
        hand_id = int(row["id"])
        dups = duplicate_ids or []
        widest = max(dups) if dups else hand_id
        source = row.get("source") or HAND_SOURCE_DB
        return {
            "id": hand_id,
            "source": source,
            "sourceLabel": HAND_SOURCE_LABELS.get(source, source),
            "externalId": row.get("external_id"),
            "importBatchId": row.get("batch_id"),
            "title": row.get("title") or self._default_title(row),
            "heroCards": row.get("hero_cards") or "",
            "board": row.get("board") or "",
            "boardByStreet": split_board(row.get("board")),
            "heroPosition": row.get("hero_position"),
            "tableSize": int(row.get("table_size") or 0),
            "heroStackBb": row.get("hero_stack_bb"),
            "bombPotBb": row.get("bomb_pot_bb") or 0,
            # 列表页直接显示「爆炸底池」：翻前无动作 + 每人 5BB 也判定为 True，
            # 不依赖导入记录里有没有 bomb_pot 标记。
            "bombPot": bool(analysis.bomb),
            "result": row.get("result") or "unknown",
            "resultAmount": row.get("result_amount"),
            "potType": row.get("pot_type"),
            "heroTags": json_load(row.get("hero_tags"), []) or [],
            "favorite": mark["favorite"],
            "markTags": mark["tags"],
            "markStatus": mark["status"],
            "markNote": mark["note"],
            "decisionCount": analysis.decision_count,
            "approxCount": analysis.approx_count,
            "quality": analysis.quality,
            "warnings": list(analysis.warnings),
            "invalidReason": analysis.invalid_reason,
            "duplicate": bool(dups) and hand_id != widest,
            "duplicateIds": dups,
            "fingerprint": analysis.fingerprint,
            "hasRun": last_run is not None,
            "lastRun": last_run,
            "flagMask": int(row.get("flag_mask") or 0),
            "flags": json_load(row.get("flags_json"), {}) or {},
            "reviewScore": int(row.get("review_score") or 0),
            "createdAt": iso(row.get("created_at")),
            "updatedAt": iso(row.get("updated_at")),
            "userId": row.get("user_id"),
        }

    def _default_title(self, row: dict) -> str:
        position = row.get("hero_position") or ""
        cards = row.get("hero_cards") or ""
        return f"{position} {cards}".strip() or f"手牌 #{row.get('id')}"

    def _filter_rows(self, rows: list[dict], analyses: dict[int, HandAnalysis],
                     last_runs: dict[int, dict], filters: dict) -> list[dict]:
        favorite = _parse_bool(filters.get("favorite"))
        sources = {s.lower() for s in _as_list(filters.get("source"))}
        positions = {p.upper() for p in _as_list(filters.get("position"))}
        results = {p.lower() for p in _as_list(filters.get("result"))}
        tags = [t.lower() for t in _as_list(filters.get("tag"))]
        qualities = {q.lower() for q in _as_list(filters.get("quality"))}
        has_run = _parse_bool(filters.get("hasRun"))
        flag_names = parse_flags(filters.get("flags"))
        flag_mask = mask_for(flag_names)
        min_score = filters.get("minScore")
        try:
            min_score = int(min_score) if min_score not in (None, "") else None
        except (TypeError, ValueError):
            min_score = None
        keyword = str(filters.get("keyword") or "").strip().lower()
        date_from = _parse_date(filters.get("dateFrom"))
        date_to = _parse_date(filters.get("dateTo"))
        not_run_days = filters.get("notRunDays")

        out = []
        for row in rows:
            hand_id = int(row["id"])
            analysis = analyses[hand_id]
            mark = self._mark(row)
            if favorite is not None and mark["favorite"] != favorite:
                continue
            if sources and (row.get("source") or HAND_SOURCE_DB).lower() not in sources:
                continue
            if positions and (row.get("hero_position") or "").upper() not in positions:
                continue
            if results and (row.get("result") or "unknown").lower() not in results:
                continue
            if tags and not any(t in {str(x).lower() for x in mark["tags"]} for t in tags):
                continue
            if qualities and analysis.quality not in qualities:
                continue
            if has_run is not None and (last_runs.get(hand_id) is not None) != has_run:
                continue
            if flag_names and (int(row.get("flag_mask") or 0) & flag_mask) != flag_mask:
                continue
            if min_score is not None and int(row.get("review_score") or 0) < min_score:
                continue
            if not_run_days and last_runs.get(hand_id):
                at = _parse_date(last_runs[hand_id].get("at"))
                if at and (dt.datetime.now() - at).days < int(not_run_days):
                    continue
            if keyword:
                hay = " ".join([
                    str(row.get("title") or ""), str(row.get("hero_cards") or ""),
                    str(row.get("board") or ""), str(row.get("hero_position") or ""),
                    str(row.get("external_id") or ""),
                ]).lower()
                if keyword not in hay:
                    continue
            created = row.get("created_at")
            if date_from and created and created < date_from:
                continue
            if date_to and created and created > date_to + dt.timedelta(days=1):
                continue
            out.append(row)
        return out

    def _sort_rows(self, rows: list[dict], analyses: dict[int, HandAnalysis],
                   last_runs: dict[int, dict], sort: str, order: str) -> list[dict]:
        reverse = (order or "desc").lower() != "asc"
        key = (sort or "created_at").lower()

        def val(row):
            hand_id = int(row["id"])
            if key == "updated_at":
                return row.get("updated_at") or dt.datetime.min
            if key == "result_amount":
                return row.get("result_amount") if row.get("result_amount") is not None else -1e18
            if key == "decision_count":
                return analyses[hand_id].decision_count
            if key == "last_run":
                at = (last_runs.get(hand_id) or {}).get("at")
                return _parse_date(at) or dt.datetime.min
            if key in ("review_score", "reviewscore", "score"):
                return int(row.get("review_score") or 0)
            if key == "id":
                return hand_id
            return row.get("created_at") or dt.datetime.min

        return sorted(rows, key=val, reverse=reverse)

    # ─────────────────────────── 手牌库 ───────────────────────────

    def list_hands(self, query: dict) -> dict:
        """手牌库列表：以 canonical 为一行，同内容多来源合并展示。

        旧实现把 1058 条手牌的全列（含 streets/villains/table_roster）拖回来，
        再逐手重建成 1082 个 HandAnalysis；远程 MySQL 上首屏要 9–15 秒。
        现在只读轻量列 + 决策聚合（约 2.6 秒），来源边由 canonical 表给出。
        """
        rows = self.hand_repo.list_light_rows()
        rows_by_id = {int(r["id"]): r for r in rows}
        visible_keys = {(library.canonical_source(r), int(r["id"])) for r in rows}
        groups = library.build_groups(
            self.canonical_repo.list_all(),
            self.canonical_repo.all_sources(),
            visible_keys)
        stats, buckets = self._decision_index()
        case_counts = self.case_repo.counts_by_canonical()
        last_runs = self._last_run_map(
            [int(s["source_hand_id"]) for g in groups for s in g["visibleSources"]])
        filters = _group_filters(query)

        items = []
        for group in groups:
            item = self._group_item(group, rows_by_id, stats, buckets,
                                    case_counts, last_runs)
            if library.matches_filters(item, filters):
                items.append(item)
        items.sort(key=lambda g: library.sort_value(
            g, str(query.get("sort") or "created_at").lower()),
            reverse=(str(query.get("order") or "desc").lower() != "asc"))

        page = max(1, int(query.get("page") or 1))
        page_size = min(200, max(1, int(query.get("pageSize")
                                       or self.settings.default_page_size)))
        return {
            "total": len(items),
            "handTotal": sum(int(i.get("sourceCount") or 0) for i in items),
            "page": page,
            "pageSize": page_size,
            "items": items[(page - 1) * page_size: page * page_size],
            "filters": query,
            "tagOptions": self._tag_options(rows),
        }

    def _decision_index(self) -> tuple[dict[int, dict],
                                       dict[int, dict[str, set[str]]]]:
        """canonical 决策数 / approx 数 / 七桶取值，一次轻量查询算完。"""
        grouped: dict[int, list[dict]] = {}
        for row in self.decision_repo.canonical_decisions():
            grouped.setdefault(int(row["canonical_id"]), []).append(row)
        stats: dict[int, dict] = {}
        buckets: dict[int, dict[str, set[str]]] = {}
        for cid, rows in grouped.items():
            stats[cid], buckets[cid] = library.decision_stats(rows)
        return stats, buckets

    def _advices_for_source(self, hand_id: int, row: dict) -> list[dict]:
        """线上建议优先读 replay_live_advices（有 prompt/raw 的规范表）；
        旧记录没有拆表时，再回退 payload_json 里的 advices。"""
        try:
            live_rows = self.live_advice_repo.list_for_hand(hand_id)
        except Exception:  # noqa: BLE001
            log.exception("读取线上建议失败；回退 payload")
            live_rows = []
        advices = [self._advice_public(advice) for advice in live_rows]
        if not advices and row.get("source") in (HAND_SOURCE_LIVE,
                                                 HAND_SOURCE_REVIEW):
            payload = self.import_repo.get_payload(hand_id)
            value = payload.get("advices") if isinstance(payload, dict) else None
            if isinstance(value, list):
                advices = [self._advice_public(a) for a in value
                           if isinstance(a, dict)]
        return advices

    def _source_item(self, source: dict, rows_by_id: dict[int, dict],
                     *, detail: bool = False) -> dict:
        """列表与详情共用的单来源摘要；详情多带复盘只读字段。"""
        source_id = int(source["source_hand_id"])
        row = rows_by_id.get(source_id) or {}
        name = str(source.get("source") or "")
        item = {
            **library.group_source_item(source, source_id),
            "canonicalSource": name,
            "sourceLabel": library.SOURCE_LABELS.get(name, name),
            "title": row.get("title")
            or self._default_title({**row, "id": source_id}),
            "result": row.get("result") or "unknown",
            "resultAmount": row.get("result_amount"),
            "createdAt": iso(row.get("created_at")),
        }
        if detail:
            item.update({
                "heroThought": row.get("hero_thought"),
                "analyzeStatus": row.get("analyze_status"),
                "reviewScore": int(row.get("review_score") or 0),
                "mark": self._mark(row),
                "advices": self._advices_for_source(source_id, row),
            })
        else:
            item["externalId"] = row.get("external_id")
        return item

    def _group_item(self, group: dict, rows_by_id: dict[int, dict],
                    stats: dict[int, dict],
                    buckets: dict[int, dict[str, set[str]]],
                    case_counts: dict[int, int],
                    last_runs: dict[int, dict]) -> dict:
        canonical_id = (int(group["canonicalId"])
                        if group.get("canonicalId") else None)
        canonical = group["canonical"]
        sources = [s for s in group["visibleSources"]
                   if int(s["source_hand_id"]) in rows_by_id]
        if not sources:
            sources = group["visibleSources"]
        representative = next(
            (s for s in sources if int(s["source_hand_id"])
             == int(group["representative"]["source_hand_id"])),
            sources[0])
        hand_id = int(representative["source_hand_id"])
        row = rows_by_id.get(hand_id) or {}
        stat = (stats.get(canonical_id) if canonical_id else None) or {
            "count": 0, "approx": 0, "quality": "invalid"}
        bucket_values = (buckets.get(canonical_id) if canonical_id else None) or {}

        marks = [self._mark(r) for r in
                 (rows_by_id.get(int(s["source_hand_id"])) or {}
                  for s in sources)]
        favorite = any(m["favorite"] for m in marks)
        mark_tags: list[str] = []
        for mark in marks:
            for tag in mark["tags"]:
                if tag not in mark_tags:
                    mark_tags.append(tag)
        source_items = [self._source_item(source, rows_by_id)
                        for source in sources]
        run_items = [(last_runs.get(int(s["source_hand_id"])), int(
            s["source_hand_id"])) for s in sources]
        latest_run = None
        for run, _sid in run_items:
            if run and (latest_run is None or
                        (run.get("at") or "") > (latest_run.get("at") or "")):
                latest_run = run
        flags: dict = {}
        flag_mask = 0
        review_score = 0
        for source in sources:
            source_row = rows_by_id.get(int(source["source_hand_id"])) or {}
            flag_mask |= int(source_row.get("flag_mask") or 0)
            review_score = max(review_score,
                               int(source_row.get("review_score") or 0))
            for name, value in (json_load(source_row.get("flags_json"), {})
                                or {}).items():
                flags.setdefault(name, value)
        hand = {
            "id": hand_id,
            "canonicalId": canonical_id,
            "groupSize": len(sources),
            "sourceCount": len(sources),
            "sources": source_items,
            "source": row.get("source") or HAND_SOURCE_DB,
            "sourceLabel": library.SOURCE_LABELS.get(
                str(representative.get("source") or ""), ""),
            "canonicalSource": str(representative.get("source") or ""),
            "externalId": row.get("external_id"),
            "importBatchId": row.get("batch_id"),
            "title": row.get("title") or canonical.get("title")
            or self._default_title({**row, "id": hand_id}),
            "heroCards": row.get("hero_cards")
            or canonical.get("hero_cards") or "",
            "board": row.get("board") or canonical.get("board") or "",
            "boardByStreet": split_board(row.get("board")
                                         or canonical.get("board")),
            "heroPosition": row.get("hero_position")
            or canonical.get("hero_position"),
            "tableSize": int(row.get("table_size") or 0),
            "heroStackBb": row.get("hero_stack_bb"),
            "bombPotBb": row.get("bomb_pot_bb") or 0,
            "bombPot": bool(row.get("bomb_pot_bb")),
            "result": row.get("result") or "unknown",
            "resultAmount": row.get("result_amount"),
            "potType": row.get("pot_type"),
            "heroTags": json_load(row.get("hero_tags"), []) or [],
            "favorite": favorite,
            "markTags": mark_tags,
            "markStatus": marks[0]["status"] if marks else "todo",
            "markNote": marks[0]["note"] if marks else "",
            "decisionCount": int(stat["count"]),
            "approxCount": int(stat["approx"]),
            "quality": stat["quality"],
            "warnings": [],
            "invalidReason": None,
            "duplicate": len(sources) > 1,
            "duplicateIds": [int(s["source_hand_id"]) for s in sources],
            "fingerprint": canonical.get("content_group") or "",
            "hasRun": latest_run is not None,
            "lastRun": latest_run,
            "lastRunAt": (latest_run or {}).get("at"),
            "flagMask": flag_mask,
            "flags": flags,
            "reviewScore": review_score,
            "caseCount": int(case_counts.get(canonical_id) or 0)
            if canonical_id else 0,
            "buckets": {name: sorted(values)
                        for name, values in bucket_values.items()},
            "bucketChips": library.bucket_values(bucket_values),
            "createdAt": iso(row.get("created_at")),
            "updatedAt": iso(row.get("updated_at")),
            "userId": row.get("user_id"),
        }
        return hand

    def _tag_options(self, rows: list[dict]) -> list[str]:
        tags = set(DEFAULT_TAGS)
        for row in rows:
            tags.update(str(t) for t in (json_load(row.get("mark_tags"), []) or []))
        return sorted(tags)

    # ─────────────────────────── 导入手牌 ───────────────────────────

    def list_imports(self, query: dict) -> dict:
        limit = min(100, max(1, int(query.get("limit") or 20)))
        return {"items": self.batch_repo.list(limit)}

    def _review_fingerprints(self) -> set[str]:
        """review_hands 的内容指纹，用于导入时避免重复。"""
        out: set[str] = set()
        for row in self.hand_repo.list_review_rows():
            try:
                out.add(self.analyze(row).fingerprint)
            except Exception:  # noqa: BLE001
                log.warning("计算 review_hands #%s 指纹失败，导入去重跳过",
                            row.get("id"))
        return out

    def import_hands(self, payload: dict) -> dict:
        """解析并（可选）写入复盘/实时手牌。

        第一次调用建议 `dryRun=true` 先预览计数与逐手状态；确认后再导入。
        同一 `(source, fingerprint)` 重复导入是幂等的，会更新已有记录。
        """
        fmt = str(payload.get("format") or "auto").strip().lower()
        dry_run = bool(payload.get("dryRun"))
        dedupe = payload.get("dedupe")
        dedupe = True if dedupe is None else bool(dedupe)
        allow_invalid = bool(payload.get("allowInvalid"))
        # 默认只导入重建质量为 exact 的手牌：approx 是推算值，重跑对比不可靠。
        only_exact = payload.get("onlyExact")
        only_exact = True if only_exact is None else bool(only_exact)
        include_partial = bool(payload.get("includePartial"))
        include_mid_hand = bool(payload.get("includeMidHand"))
        filename = str(payload.get("filename") or "").strip()[:255] or None

        parsed = parse_hand_text(
            str(payload.get("content") or ""),
            source=fmt, include_partial=include_partial,
            include_mid_hand=include_mid_hand,
        )
        existing_map = self.import_repo.fingerprint_map()
        existing_fps = {fp for _source, fp in existing_map}
        review_fps = self._review_fingerprints() if dedupe else set()

        imported = updated = duplicates = invalid = not_exact = 0
        written = 0
        batch_id: int | None = None
        seen_keys: set[tuple[str, str]] = set()
        file_fps: set[str] = set()
        samples: list[dict] = []
        issues: list[dict] = list(parsed.errors[:50])
        error_total = len(parsed.errors)

        def add_sample(ph, status: str, **extra) -> None:
            if len(samples) >= 100:
                return
            row = ph.row
            samples.append({
                "index": ph.index,
                "source": ph.source,
                "externalId": row.get("external_id"),
                "title": row.get("title"),
                "heroCards": row.get("hero_cards"),
                "board": row.get("board"),
                "heroPosition": row.get("hero_position"),
                "status": status,
                "decisionCount": extra.pop("decisionCount", 0),
                "quality": extra.pop("quality", None),
                "reason": extra.pop("reason", None),
                **extra,
            })

        for ph in parsed.records:
            row = dict(ph.row)
            row["raw"] = ph.raw
            row["source"] = ph.source
            row["external_id"] = row.get("external_id") or ""
            try:
                analysis = analyze_hand({**row, "id": 0})
            except Exception as exc:  # noqa: BLE001
                issues.append({"index": ph.index, "line": ph.line,
                               "reason": f"分析失败：{type(exc).__name__}: {exc}"})
                error_total += 1
                add_sample(ph, "error", reason=f"分析失败：{exc}")
                continue
            fingerprint = analysis.fingerprint
            row["fingerprint"] = fingerprint
            key = (ph.source, fingerprint)
            info = {"decisionCount": analysis.decision_count,
                    "quality": analysis.quality}

            if analysis.quality == INVALID and not allow_invalid:
                invalid += 1
                add_sample(ph, "invalid", reason=analysis.invalid_reason,
                           **info)
                continue

            if only_exact and analysis.quality != EXACT:
                not_exact += 1
                add_sample(ph, "approx",
                           reason="重建质量 approx（推算值），未达到 exact",
                           **info)
                continue

            if key in seen_keys:
                status = "duplicate"
                reason = "本次文件内内容重复"
            elif key in existing_map:
                status = "updated"
                reason = "已存在相同内容，更新原记录"
            elif dedupe and (fingerprint in review_fps
                             or fingerprint in existing_fps
                             or fingerprint in file_fps):
                status = "duplicate"
                reason = "库中已有相同内容的手牌"
            else:
                status = "imported"
                reason = None

            if status == "duplicate":
                duplicates += 1
                add_sample(ph, status, reason=reason, **info)
                seen_keys.add(key)
                file_fps.add(fingerprint)
                continue
            seen_keys.add(key)
            file_fps.add(fingerprint)

            if dry_run:
                if status == "imported":
                    imported += 1
                else:
                    updated += 1
                add_sample(ph, status, reason=reason, **info)
                continue

            if batch_id is None:
                batch_id = self.batch_repo.create(
                    source=parsed.source, container=parsed.container,
                    filename=filename, total_records=parsed.scanned)
            try:
                _hand_id, created = self.import_repo.upsert(
                    row, batch_id=batch_id)
                actual = "imported" if created else "updated"
                if created:
                    imported += 1
                else:
                    updated += 1
                written += 1
                add_sample(ph, actual,
                           reason=None if created else "已存在相同内容，更新原记录",
                           **info)
            except Exception as exc:  # noqa: BLE001
                log.exception("写入手牌失败：%s", row.get("external_id"))
                issues.append({"index": ph.index, "line": ph.line,
                               "reason": f"写库失败：{type(exc).__name__}: {exc}"})
                error_total += 1
                add_sample(ph, "error", reason=f"写库失败：{exc}", **info)

        summary = {
            "dryRun": dry_run,
            "batchId": batch_id,
            "source": parsed.source,
            "sourceLabel": SOURCE_LABELS.get(parsed.source, parsed.source),
            "container": parsed.container,
            "filename": filename,
            "total": parsed.scanned,
            "hands": len(parsed.records),
            "advices": parsed.advices,
            "imported": imported,
            "updated": updated,
            "duplicates": duplicates,
            "invalid": invalid,
            "skippedNotExact": not_exact,
            "errors": error_total,
            "skippedNonHand": parsed.skipped_non_hand,
            "skippedPartial": parsed.skipped_partial,
            "skippedMidHand": parsed.skipped_mid_hand,
            "sessionCount": parsed.session_count,
            "dedupe": dedupe,
            "onlyExact": only_exact,
            "allowInvalid": allow_invalid,
            "samples": samples,
            "issues": issues[:50],
        }
        if batch_id is not None:
            status = "succeeded" if (written or not issues) else "failed"
            self.batch_repo.finish(
                batch_id, status=status, imported=imported, updated=updated,
                duplicates=duplicates, invalid=invalid, errors=error_total,
                summary={k: v for k, v in summary.items()
                         if k not in ("samples", "issues")},
            )
        return summary

    # ─────────────────────────── 结构化 ingest ───────────────────────────

    def ingest_hands(self, payload: dict) -> dict:
        """实时 outbox 的结构化入口：HandRecord v2 直接入库。

        与 `import_hands()` 的区别：
          · 不做文本解析，直接吃 outbox 的 dict；
          · 按 `external_uid` 幂等（补了 advice/context 不会重复入库）；
          · 同时写座位表 `replay_hand_players` 和线上建议
            `replay_live_advices`，供手牌库按玩家/A-B 查询。
        """
        source = str(payload.get("source") or SOURCE_LIVE).strip().lower()
        if source not in (SOURCE_LIVE, SOURCE_REVIEW):
            raise ApiError("INVALID_INGEST_SOURCE",
                           f"source 只能是 live/review，收到 {source!r}", 422)
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise ApiError("INVALID_INGEST_PAYLOAD",
                           "records 必须是非空数组", 422)
        if len(records) > 500:
            raise ApiError("INVALID_INGEST_PAYLOAD",
                           "一次最多 ingest 500 手", 422)

        batch_id = self.batch_repo.create(
            source=source, container="json",
            filename=str(payload.get("filename") or "outbox")[:255],
            total_records=len(records))
        inserted = updated = duplicates = invalid = 0
        errors: list[dict] = []
        samples: list[dict] = []
        hand_ids: list[int] = []
        advices_written = 0
        players_written = 0

        for index, record in enumerate(records):
            if not isinstance(record, dict):
                errors.append({"index": index, "reason": "不是 JSON 对象"})
                continue
            try:
                row = normalize_live_hand(
                    record,
                    session=int(record.get("session") or 0),
                    hand_seq=index + 1,
                )
                analysis = analyze_hand({**row, "id": 0})
                if analysis.quality == INVALID:
                    invalid += 1
                row["fingerprint"] = analysis.fingerprint
                advices = row.get("advices") if isinstance(row.get("advices"), list) else []
                self._annotate_live_advices(advices, analysis)
                hand_id, created = self.import_repo.upsert(row, batch_id=batch_id)
                hand_ids.append(int(hand_id))
                if created:
                    inserted += 1
                else:
                    updated += 1

                analysis.hand_id = int(hand_id)
                persisted = self.decision_repo.persist(analysis)
                decision_ids = {}
                for decision in analysis.decisions:
                    stored = persisted.get((decision.street, decision.seq))
                    if stored:
                        decision_ids[(decision.street,
                                      decision.action_index)] = int(stored["id"])
                roster = self._hero_roster(row.get("table_roster") or [],
                                           row.get("hero_position"))
                players_written += self.import_repo.replace_players(
                    int(hand_id), roster)
                advices_written += self.import_repo.replace_live_advices(
                    int(hand_id), advices, decision_ids=decision_ids)
                self._apply_enrichment(int(hand_id), row, analysis,
                                       decision_ids)
                if len(samples) < 50:
                    samples.append({
                        "index": index,
                        "uid": row.get("external_uid"),
                        "handId": int(hand_id),
                        "status": "imported" if created else "updated",
                        "quality": analysis.quality,
                        "decisions": analysis.decision_count,
                        "advices": len(advices),
                    })
            except Exception as exc:  # noqa: BLE001
                log.exception("ingest 手牌失败：index=%s", index)
                errors.append({"index": index,
                               "reason": f"{type(exc).__name__}: {exc}"})

        status = "succeeded" if not errors else ("partial" if inserted + updated else "failed")
        self.batch_repo.finish(
            batch_id, status=status, imported=inserted, updated=updated,
            duplicates=duplicates, invalid=invalid, errors=len(errors),
            summary={"records": len(records), "advices": advices_written,
                     "players": players_written},
            error_msg=(errors[0]["reason"] if errors else None),
        )
        return {
            "batchId": batch_id,
            "source": source,
            "received": len(records),
            "inserted": inserted,
            "updated": updated,
            "duplicates": duplicates,
            "invalid": invalid,
            "errors": len(errors),
            "handIds": hand_ids,
            "advices": advices_written,
            "players": players_written,
            "samples": samples,
            "issues": errors[:50],
        }

    def refresh_enrichment(self, hand_id: int) -> bool:
        """重算时间线 / 标记 / 复盘分（决策点被回填后必须重算，否则 flags 过期）。

        get_row 刻意不取 payload_json（大字段），但重建靠 payload 里的 advices
        才能补出「英雄动作缺录」的决策点，所以这里单独取一次。
        """
        row = self.hand_repo.get_row(int(hand_id))
        if not row:
            return False
        row = dict(row)
        if not isinstance(row.get("advices"), list):
            payload = self.import_repo.get_payload(int(hand_id))
            advices = payload.get("advices") if isinstance(payload, dict) else None
            if isinstance(advices, list):
                row["advices"] = advices
        analysis = self.analyze(row)
        # 时间线里的 decisionId 是详情页从帧跳决策行的唯一线索：ingest 会传
        # 这份映射，重算不传就会把已物化的帧洗成 null（回填过的手牌全部丢链接）。
        persisted = self.decision_repo.for_hand(int(hand_id),
                                                analysis.fingerprint)
        decision_ids = {
            (street, int(item.get("action_index") or 0)): int(item["id"])
            for (street, _seq), item in persisted.items()
            if item.get("id") is not None
        }
        return self._apply_enrichment(int(hand_id), row, analysis,
                                      decision_ids)

    def _apply_enrichment(self, hand_id: int, row: dict, analysis: HandAnalysis,
                          decision_ids: dict | None = None) -> bool:
        """物化时间线 + 深挖标记；失败不阻塞手牌入库。

        返回是否写成功：ingest 忽略它（失败也不该拦入库），重算路径要拿它
        区分「已经补好」和「又失败了」，不能把失败算成已重算。
        """
        try:
            advices = self.live_advice_repo.list_for_hand(hand_id)
            timeline = build_timeline(row, analysis, advices,
                                      decision_ids=decision_ids or {})
            flag_mask, flags, score = compute_flags(row, analysis, advices)
            self.hand_repo.update_enrichment(
                hand_id, timeline=timeline, flag_mask=flag_mask,
                flags=flags, review_score=score,
                record_schema=str(row.get("record_schema") or "hand.v2"))
            return True
        except Exception:  # noqa: BLE001
            log.exception("手牌 %s 时间线/标记生成失败；手牌已入库", hand_id)
            return False

    @staticmethod
    def _hero_roster(roster: list[dict], hero_position: str | None) -> list[dict]:
        """给座位补 is_hero；没有 seat_id 的行丢弃。"""
        out: list[dict] = []
        for item in roster:
            if not isinstance(item, dict):
                continue
            seat_id = str(item.get("seat_id") or "").strip()
            if not seat_id:
                continue
            position = item.get("position")
            is_hero = (seat_id == "hero"
                       or (hero_position is not None
                           and str(position or "") == str(hero_position)))
            out.append({**item, "is_hero": is_hero})
        return out

    @staticmethod
    def _annotate_live_advices(advices: list[dict], analysis: HandAnalysis) -> None:
        """把实际动作/尺寸/分类补到线上建议上，复盘和 A-B 直接用。"""
        by_action = {(decision.street, decision.action_index): decision
                     for decision in analysis.decisions}
        counters: dict[str, int] = {}
        for advice in advices:
            if not isinstance(advice, dict):
                continue
            street = str(advice.get("street") or "")
            action_index = advice.get("action_index")
            if action_index is None:
                action_index = counters.get(street, 0)
                advice["action_index"] = action_index
            counters[street] = counters.get(street, 0) + 1
            decision = by_action.get((street, int(action_index)))
            if decision is None:
                continue
            advice["actual_action"] = decision.actual
            advice["actual_amount"] = decision.actual_amount
            option = (advice.get("option")
                      if isinstance(advice.get("option"), dict) else {})
            kind = str(option.get("kind") or "")
            amount = option.get("amount_bb", option.get("amount"))
            try:
                advice["bucket"] = classify(
                    kind, amount, decision.actual, decision.actual_amount,
                    float(decision.pot or 0.0), street)
            except Exception:  # noqa: BLE001
                advice["bucket"] = None

    # ─────────────────────────── 删除手牌 ───────────────────────────

    def _parse_hand_ids(self, payload: dict) -> list[int]:
        raw = payload.get("handIds")
        if raw is None:
            raw = payload.get("hand_ids")
        if not isinstance(raw, (list, tuple)):
            raise ApiError("INVALID_HAND_IDS", "handIds 必须是数组", 422)
        ids: list[int] = []
        for value in raw:
            try:
                hand_id = int(value)
            except (TypeError, ValueError) as exc:
                raise ApiError("INVALID_HAND_IDS",
                               f"手牌 id 不合法：{value!r}", 422) from exc
            if hand_id not in ids:
                ids.append(hand_id)
        if not ids:
            raise ApiError("INVALID_HAND_IDS", "至少选择一手牌", 422)
        if len(ids) > 1000:
            raise ApiError("TOO_MANY_HANDS",
                           "一次最多删除 1000 手牌", 422)
        return ids

    def delete_hands(self, payload: dict) -> dict:
        """删除手牌。

        · 导入手牌：写 `replay_imported_hands.deleted_at`，收藏/决策/结果保留；
        · review_hands 手牌：只写 `replay_hand_deletions`，不改生产业务表。
        """
        ids = self._parse_hand_ids(payload)
        rows = self.hand_repo.get_by_ids(ids)
        found = {int(r["id"]) for r in rows}
        missing = [hand_id for hand_id in ids if hand_id not in found]
        if not found:
            return {
                "requested": len(ids), "deleted": 0,
                "imported": 0, "reviewHidden": 0, "missing": missing,
            }
        counts = self.hand_repo.delete_hands(sorted(found))
        with self._lock:
            for key in list(self._analysis_cache):
                if key and key[0] in found:
                    self._analysis_cache.pop(key, None)
        return {
            "requested": len(ids),
            "deleted": int(counts["imported"]) + int(counts["reviewHidden"]),
            "imported": int(counts["imported"]),
            "reviewHidden": int(counts["reviewHidden"]),
            "missing": missing,
        }

    def delete_hand(self, hand_id: int) -> dict:
        result = self.delete_hands({"handIds": [hand_id]})
        if result["deleted"] == 0:
            raise ApiError("HAND_NOT_FOUND",
                           f"手牌 {hand_id} 不存在或已删除", 404)
        return {**result, "handId": hand_id}

    @staticmethod
    def _advice_public(advice: dict) -> dict:
        """把线上建议行 / payload advice 统一成前端 camelCase 形状。"""
        option = advice.get("option") if isinstance(advice.get("option"), dict) else {}
        if not option:
            option = {
                "id": advice.get("option_id"),
                "label": advice.get("option_label"),
                "kind": advice.get("option_kind"),
                "amount": advice.get("option_amount"),
            }
        context = advice.get("context")
        if context is None:
            context = advice.get("context_json")
        if isinstance(context, str):
            context = json_load(context, {}) or {}
        has_prompt = bool(advice.get("prompt_system")
                          or advice.get("prompt_user")
                          or advice.get("prompt_sha256"))
        prompt_missing = bool(advice.get("prompt_missing")) or (
            str(advice.get("source") or "") == "model" and not has_prompt)
        return {
            "id": advice.get("id") or advice.get("advice_id"),
            "ts": advice.get("ts") or advice.get("captured_at"),
            "street": advice.get("street"),
            "pot": advice.get("pot"),
            "toCall": advice.get("to_call"),
            "option": {
                "id": option.get("id"),
                "label": option.get("label"),
                "kind": option.get("kind"),
                "amount": (option.get("amount_bb")
                           if option.get("amount_bb") is not None
                           else option.get("amount")),
            },
            "reason": advice.get("reason"),
            "reasonFull": advice.get("reason_full"),
            "plan": advice.get("plan"),
            "confidence": advice.get("confidence"),
            "source": advice.get("source"),
            "model": advice.get("model"),
            "durationMs": advice.get("duration_ms"),
            "promptVersion": advice.get("prompt_version"),
            "promptSha256": advice.get("prompt_sha256"),
            "promptMissing": prompt_missing,
            "promptMissingReason": advice.get("prompt_missing_reason"),
            "hasPrompt": has_prompt,
            "actionIndex": advice.get("action_index"),
            "decisionKey": advice.get("decision_key"),
            "context": context or None,
            "actualAction": advice.get("actual_action"),
            "actualAmount": advice.get("actual_amount"),
            "bucket": advice.get("bucket"),
        }

    def _canonical_for_hand(self, hand_id: int) -> tuple[dict | None,
                                                         list[dict]]:
        """源手牌 id 或 canonical id → (canonical 行, 全部来源边)。"""
        hand_id = int(hand_id)
        if hand_id >= CANONICAL_HAND_ID_BASE:
            canonical = self.canonical_repo.get(hand_id)
            if canonical is None:
                return None, []
            return canonical, self.canonical_repo.sources(hand_id)
        edges = self.canonical_repo.for_hand(hand_id)
        want = "live" if hand_id >= IMPORTED_HAND_ID_BASE else "review"
        edge = next((e for e in edges if str(e.get("source")) == want),
                    edges[0] if edges else None)
        if edge is None:
            return None, []
        canonical = self.canonical_repo.get(int(edge["canonical_id"]))
        if canonical is None:
            return None, []
        return canonical, self.canonical_repo.sources(int(edge["canonical_id"]))

    def resolve_hand_id(self, hand_id: int) -> int:
        """canonical id → 可读的源手牌 id（瀑布流/对比等旧路由用）。"""
        hand_id = int(hand_id)
        if hand_id < CANONICAL_HAND_ID_BASE:
            return hand_id
        _canonical, sources = self._canonical_for_hand(hand_id)
        for source in sources:
            source_id = int(source["source_hand_id"])
            if self.hand_repo.get_row(source_id) is not None:
                return source_id
        raise ApiError("HAND_NOT_FOUND",
                       f"手牌 {hand_id} 没有可见来源", 404)

    def _dedupe_source_decisions(self, rows: list[dict],
                                 source_order: dict[int, int]) -> dict:
        """同一决策点被多个来源各写一行时，取来源优先级最高的那一行。"""
        out: dict[tuple[str, int], dict] = {}
        for row in rows:
            hand_id = int(row.get("hand_id") or 0)
            rank = source_order.get(hand_id)
            if rank is None:
                continue
            key = (str(row.get("street")), int(row.get("seq") or 0))
            current = out.get(key)
            if current is None or rank < source_order.get(
                    int(current.get("hand_id") or 0), 99):
                out[key] = row
        return out

    def get_hand(self, hand_id: int) -> dict:
        """详情：canonical 维度并列全部来源，决策点时间线以代表来源重建。"""
        hand_id = int(hand_id)
        canonical, sources = self._canonical_for_hand(hand_id)
        source_rows: dict[int, dict] = {}
        visible: list[dict] = []
        for source in sources:
            source_id = int(source["source_hand_id"])
            source_row = self.hand_repo.get_row(source_id)
            if source_row is not None:
                source_rows[source_id] = source_row
                visible.append(source)
        if canonical is not None and not visible:
            raise ApiError("HAND_NOT_FOUND",
                           f"手牌 {hand_id} 的来源都已删除或隐藏", 404)
        if canonical is None:
            row = self.hand_repo.get_row(hand_id)
            if row is None:
                raise ApiError("HAND_NOT_FOUND",
                               f"手牌 {hand_id} 不存在或已删除", 404)
            source_rows = {hand_id: row}
            canonical = {"id": None, "content_group": None,
                         "hero_cards": row.get("hero_cards"),
                         "board": row.get("board"),
                         "hero_position": row.get("hero_position"),
                         "title": row.get("title")}
            visible = [{"source": library.canonical_source(row),
                        "source_hand_id": hand_id, "is_primary": 1}]
        representative = next((s for s in visible if s.get("is_primary")),
                              visible[0])
        representative_id = int(representative["source_hand_id"])
        row = source_rows[representative_id]
        analysis = self.analyze(row)
        canonical_id = canonical.get("id")

        source_order = {int(s["source_hand_id"]): index
                        for index, s in enumerate(visible)}
        if canonical_id:
            persisted = self._dedupe_source_decisions(
                self.decision_repo.for_canonical(int(canonical_id)),
                source_order)
        else:
            persisted = self.decision_repo.for_hand(
                representative_id, analysis.fingerprint)

        stats, bucket_index = library.decision_stats(persisted.values())
        stats_map = ({int(canonical_id): stats} if canonical_id else {})
        case_rows: list[dict] = []
        bucket_map = ({int(canonical_id): bucket_index} if canonical_id else {})
        last_runs = self._last_run_map(list(source_order))
        group = {
            "canonicalId": canonical_id,
            "canonical": canonical,
            "sources": visible,
            "visibleSources": visible,
            "representative": representative,
        }
        item = self._group_item(group, source_rows, stats_map, bucket_map,
                                {}, last_runs)
        if canonical_id:
            case_rows = (self.case_repo.list_for_canonical(int(canonical_id))
                         if canonical_id else [])
            item["caseCount"] = len(case_rows)

        # 多来源并列：每个来源的原始摘要 + 复盘记录（只读展示）。
        source_details = [self._source_item(source, source_rows, detail=True)
                          for source in visible]
        item["sources"] = source_details
        item["sourceCount"] = len(source_details)
        item["groupSize"] = len(source_details)

        advices = self._advices_for_source(representative_id, row)

        raw_streets = json_load(row.get("streets"), []) or []
        streets = self._streets_public(raw_streets, analysis)
        decision_ids = [int(p["id"]) for p in persisted.values()
                        if p.get("id") is not None]
        marks = self.decision_mark_repo.for_decisions(decision_ids)
        decisions = []
        for d in analysis.decisions:
            p = persisted.get((d.street, d.seq))
            mark = marks.get(int(p["id"])) if p and p.get("id") is not None else None
            decisions.append(self._decision_public(d, p, mark))
        duplicates = [{
            "id": source["handId"],
            "title": source["title"],
            "createdAt": source["createdAt"],
        } for source in source_details if source["handId"] != item["id"]]
        cases = [self._case_public(case) for case in
                 (case_rows if canonical_id else [])]
        return {
            "hand": item,
            "canonical": (self.canonical_public(canonical, source_details)
                          if canonical_id else None),
            "sources": source_details,
            "cases": cases,
            "streets": streets,
            "decisions": decisions,
            "advices": advices,
            "duplicates": duplicates,
            "mark": self._mark(row),
            "reconstruction": {
                "quality": analysis.quality,
                "warnings": list(analysis.warnings),
                "invalidReason": analysis.invalid_reason,
                "bombPot": analysis.bomb,
                "players": analysis.players,
            },
            "current": {
                "adviceVersion": CURRENT_ADVICE_VERSION,
                "reconstructVersion": CURRENT_RECONSTRUCT_VERSION,
            },
        }

    def canonical_public(self, canonical: dict, sources: list[dict]) -> dict:
        return {
            "id": int(canonical["id"]),
            "contentGroup": canonical.get("content_group"),
            "primarySource": canonical.get("primary_source"),
            "heroCards": canonical.get("hero_cards") or "",
            "board": canonical.get("board") or "",
            "heroPosition": canonical.get("hero_position"),
            "startedAt": iso(canonical.get("started_at")),
            "sourceCount": len(sources),
        }

    def _case_public(self, case: dict) -> dict:
        spec = None
        if case.get("spec_id"):
            try:
                spec = self.spec_repo.get(int(case["spec_id"]))
            except Exception:  # noqa: BLE001 - spec 被删也应能显示案例
                spec = None
        return {
            "id": int(case["id"]),
            "caseSetId": int(case["case_set_id"]),
            "caseSetName": case.get("set_name"),
            "caseSetRevision": case.get("set_revision"),
            "frozen": bool(case.get("set_frozen_at")),
            "canonicalId": int(case["canonical_id"]),
            "decisionKey": case.get("decision_key"),
            "specId": int(case["spec_id"]) if case.get("spec_id") else None,
            "specName": (spec or {}).get("name"),
            "priority": int(case.get("priority") or 0),
            "p0": int(case.get("priority") or 0) == 0,
            "status": case.get("status") or "draft",
            "note": case.get("note") or "",
            "overrides": json_load(case.get("overrides_json"), {}) or {},
            "createdAt": iso(case.get("created_at")),
        }

    CASE_OVERRIDE_KEYS = ("action", "sizing", "freq", "note")

    def _case_overrides(self, payload: dict) -> dict | None:
        """案例级手工覆盖：只收已知键、限长，坏值直接拒，不静默丢弃。"""
        raw = payload.get("overrides")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ApiError("INVALID_CASE_OVERRIDES",
                           "overrides 必须是对象", 422)
        unknown = [key for key in raw if key not in self.CASE_OVERRIDE_KEYS]
        if unknown:
            allowed = ", ".join(self.CASE_OVERRIDE_KEYS)
            got = ", ".join(map(str, unknown))
            raise ApiError(
                "INVALID_CASE_OVERRIDES",
                f"overrides 只接受 {allowed}；收到 {got}", 422)
        if len(json.dumps(raw, ensure_ascii=False)) > 4000:
            raise ApiError("INVALID_CASE_OVERRIDES",
                           "overrides 太大（JSON ≤ 4000 字符）", 422)
        return raw or None

    def _case_target(self, payload: dict) -> tuple[int, str, int | None]:
        """校验 (canonicalId, decisionKey, specId)，并确认决策点确实存在。"""
        canonical_id = payload.get("canonicalId")
        decision_key = str(payload.get("decisionKey") or "").strip()
        spec_id = payload.get("specId")
        if not canonical_id:
            raise ApiError("INVALID_CASE", "建案例需要 canonicalId", 422)
        if not re.fullmatch(r"(preflop|flop|turn|river):\d+", decision_key):
            raise ApiError("INVALID_CASE",
                           "decisionKey 形如 flop:2（街道:seq）", 422)
        if not spec_id:
            raise ApiError("SPEC_REQUIRED", "建案例需要选择一个期望（spec）", 422)
        canonical = self.canonical_repo.get(int(canonical_id))
        if canonical is None:
            raise ApiError("HAND_NOT_FOUND",
                           f"canonical {canonical_id} 不存在", 404)
        if spec_id:
            spec = self.spec_repo.get(int(spec_id))
            if spec is None:
                raise ApiError("SPEC_NOT_FOUND", f"期望 {spec_id} 不存在", 404)
        keys = {library.decision_key(row.get("street"), row.get("seq"))
                for row in self.decision_repo.for_canonical(int(canonical_id))}
        if decision_key not in keys:
            # 旧重建只写了加工后的决策点、没落 replay_decisions；用代表来源
            # 重放一次确认时间线上确有这个点，避免“页面上有、点建案例 404”。
            for edge in self.canonical_repo.sources(int(canonical_id)):
                row = self.hand_repo.get_row(int(edge["source_hand_id"]))
                if row is None:
                    continue
                rebuilt = self.analyze(row)
                if decision_key in {library.decision_key(d.street, d.seq)
                                    for d in rebuilt.decisions}:
                    break
            else:
                raise ApiError("DECISION_NOT_FOUND",
                               f"决策点 {decision_key} 不在这手牌上", 404)
        return (int(canonical_id), decision_key,
                int(spec_id) if spec_id else None)

    @staticmethod
    def _priority(payload: dict) -> int:
        return (int(payload["priority"])
                if payload.get("priority") is not None else 1)

    def create_case(self, payload: dict) -> dict:
        """从详情页某个决策点建一条案例（可指定案例集，默认「待整理」）。"""
        canonical_id, decision_key, spec_id = self._case_target(payload)
        case, created = self.case_repo.create(
            canonical_id=canonical_id, decision_key=decision_key,
            spec_id=int(spec_id),
            case_set_id=(int(payload["caseSetId"])
                         if payload.get("caseSetId") else None),
            priority=self._priority(payload),
            overrides=self._case_overrides(payload),
            note=str(payload.get("note") or ""))
        case = self.case_repo.get_by_id(int(case["id"])) or case
        return {"created": created, "case": self._case_public(case)}

    # ─────────────────────────── 案例集（#55） ───────────────────────────

    def _case_item_public(self, case: dict) -> dict:
        out = self._case_public(case)
        bucket = case_lib.case_bucket(case)
        out.update({
            "street": case_lib.street_of(case),
            "actionIndex": (int(case["action_index"])
                            if case.get("action_index") is not None else None),
            "bucket": bucket,
            "bucketLabel": case_lib.label_of(bucket),
            "hand": {
                "heroCards": case.get("hero_cards") or "",
                "board": case.get("board") or "",
                "heroPosition": case.get("hero_position"),
            },
        })
        return out

    @staticmethod
    def _case_set_summary(row: dict) -> dict:
        count = int(row.get("case_count") or 0)
        p0 = int(row.get("p0_count") or 0)
        return {
            "id": int(row["id"]),
            "name": row.get("name") or "",
            "revision": int(row.get("revision") or 1),
            "purpose": row.get("purpose") or "exploration",
            "frozen": bool(row.get("frozen_at")),
            "frozenAt": iso(row.get("frozen_at")),
            "createdAt": iso(row.get("created_at")),
            "caseCount": count,
            "p0Count": p0,
            "p0Ratio": round(p0 / count, 4) if count else 0.0,
        }

    @staticmethod
    def _case_set_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name:
            raise ApiError("INVALID_CASE_SET", "案例集名称不能为空", 422)
        if len(name) > 100:
            raise ApiError("INVALID_CASE_SET", "案例集名称最长 100 字符", 422)
        return name

    @staticmethod
    def _case_set_purpose(value: Any, default: str = "exploration") -> str:
        purpose = str(value or default).strip() or default
        if purpose not in CaseRepository.CASE_SET_PURPOSES:
            raise ApiError(
                "INVALID_CASE_SET",
                f"purpose 只能是 {' / '.join(CaseRepository.CASE_SET_PURPOSES)}",
                422)
        return purpose

    def _case_set_detail(self, row: dict) -> dict:
        items = [self._case_item_public(case)
                 for case in self.case_repo.items(int(row["id"]))]
        stats = case_lib.case_stats(items)
        detail = self._case_set_summary(row)
        detail.update({
            "caseCount": stats["total"],
            "p0Count": stats["p0"],
            "p0Ratio": stats["p0Ratio"],
            "items": items,
            "stats": stats,
        })
        return detail

    def list_case_sets(self, query: dict | None = None) -> dict:
        query = query or {}
        name = str(query.get("name") or "").strip() or None
        limit = int(query.get("limit") or 200)
        rows = self.case_repo.list_sets(limit=min(max(limit, 1), 500),
                                        name=name)
        return {"items": [self._case_set_summary(row) for row in rows]}

    def get_case_set(self, set_id: int) -> dict:
        row = self.case_repo.require_set(set_id)
        return self._case_set_detail(row)

    def create_case_set(self, payload: dict) -> dict:
        name = self._case_set_name(payload.get("name"))
        purpose = self._case_set_purpose(payload.get("purpose"))
        row = self.case_repo.create_set(name=name, purpose=purpose)
        return self._case_set_detail(row)

    def patch_case_set(self, set_id: int, payload: dict) -> dict:
        name = (self._case_set_name(payload["name"])
                if payload.get("name") is not None else None)
        purpose = (self._case_set_purpose(payload["purpose"])
                   if payload.get("purpose") is not None else None)
        row = self.case_repo.update_set(set_id, name=name, purpose=purpose)
        return self._case_set_detail(row)

    def freeze_case_set(self, set_id: int) -> dict:
        row = self.case_repo.freeze_set(set_id)
        return self._case_set_detail(row)

    def create_case_set_revision(self, set_id: int, payload: dict) -> dict:
        """冻结（或任意）revision → 复制成下一个 revision，再改新版本。"""
        row = self.case_repo.copy_set_to_new_revision(set_id)
        name = (self._case_set_name(payload["name"])
                if payload.get("name") is not None else None)
        purpose = (self._case_set_purpose(payload["purpose"])
                   if payload.get("purpose") is not None else None)
        if name is not None or purpose is not None:
            row = self.case_repo.update_set(
                int(row["id"]), name=name, purpose=purpose)
        return self._case_set_detail(row)

    def add_case_set_cases(self, set_id: int, payload: dict) -> dict:
        """批量加案例：逐条校验，坏条目进 skipped，不整批回滚。"""
        self.case_repo.require_unfrozen(set_id)
        entries = payload.get("cases") or []
        if not isinstance(entries, list) or not entries:
            raise ApiError("INVALID_CASE_SET", "至少选择一条案例", 422)
        created = existing = 0
        skipped: list[dict] = []
        for entry in entries:
            try:
                canonical_id, decision_key, spec_id = self._case_target(entry)
                _, was_created = self.case_repo.create(
                    canonical_id=canonical_id, decision_key=decision_key,
                    spec_id=int(spec_id), case_set_id=int(set_id),
                    priority=self._priority(entry),
                    overrides=self._case_overrides(entry),
                    note=str(entry.get("note") or ""))
            except ApiError as exc:
                skipped.append({
                    "canonicalId": entry.get("canonicalId"),
                    "decisionKey": entry.get("decisionKey"),
                    "code": exc.code,
                    "reason": exc.message,
                })
                continue
            if was_created:
                created += 1
            else:
                existing += 1
        result = self.get_case_set(set_id)
        result.update({"created": created, "existing": existing,
                       "skipped": skipped})
        return result

    def patch_case_set_case(self, set_id: int, case_id: int,
                            payload: dict) -> dict:
        spec_id = payload.get("specId")
        if spec_id is not None:
            if self.spec_repo.get(int(spec_id)) is None:
                raise ApiError("SPEC_NOT_FOUND", f"期望 {spec_id} 不存在", 404)
        status = payload.get("status")
        if status is not None and status not in ("draft", "active", "retired"):
            raise ApiError("INVALID_CASE", "status 只能是 draft / active / retired",
                           422)
        self.case_repo.update_case(
            set_id, case_id,
            spec_id=(int(spec_id) if spec_id is not None else None),
            priority=(int(payload["priority"])
                      if payload.get("priority") is not None else None),
            status=status,
            note=(str(payload["note"]) if payload.get("note") is not None
                  else None),
            overrides=self._case_overrides(payload))
        return self.get_case_set(set_id)

    def delete_case_set_case(self, set_id: int, case_id: int) -> dict:
        self.case_repo.delete_case(set_id, case_id)
        return self.get_case_set(set_id)

    def _active_specs(self) -> list[dict]:
        specs_out: list[dict] = []
        for row in self.spec_repo.list_latest():
            if (row.get("status") or "") != "active":
                continue
            specs_out.append({
                "id": int(row["id"]),
                "name": row.get("name"),
                "bucket": json_load(row.get("bucket_json"), {}) or {},
            })
        return specs_out

    # ───────────────────────── 评分运行（#57） ─────────────────────────

    def _price_table(self) -> dict:
        """价目表：`Settings.price_table`（启动时已校验 REPLAY_PRICE_TABLE）。"""
        return dict(getattr(self.settings, "price_table", None) or {})

    @staticmethod
    def _run_id(payload: dict, key: str, label: str) -> int:
        try:
            return int(payload.get(key))
        except (TypeError, ValueError):
            raise ApiError("INVALID_RUN", f"需要 {key}（{label}）", 422) from None

    @staticmethod
    def _query_int(value: Any, default: int | None, *, low: int | None = None,
                   high: int | None = None) -> int | None:
        """查询参数转 int：坏值按缺省处理（fail open），不把 500 抛给页面。"""
        try:
            out = int(value)
        except (TypeError, ValueError):
            return default
        if low is not None:
            out = max(low, out)
        if high is not None:
            out = min(high, out)
        return out

    def _require_ready_version(self, version_id: int) -> dict:
        row = self.version_repo.get(version_id)
        if row is None:
            raise ApiError("VERSION_NOT_FOUND", f"版本 {version_id} 不存在", 404)
        blockers = run_blockers(row)
        if blockers:
            raise ApiError(
                "VERSION_NOT_RUNNABLE",
                "版本不可运行：" + "、".join(
                    str(b.get("message") or b.get("code"))
                    for b in blockers),
                409, detail=blockers)
        return row

    def score_run_meta(self) -> dict:
        """创建页需要的字典：可运行的案例集、版本、判定口径与单价。"""
        sets_out: list[dict] = []
        for row in self.case_repo.list_sets(limit=200):
            summary = self._case_set_summary(row)
            if summary["frozen"]:
                summary["plan"] = run_worker.plan_counts(
                    summary["caseCount"], summary["p0Count"])
            else:
                summary["plan"] = None
            sets_out.append(summary)
        versions_out: list[dict] = []
        for row in self.version_repo.list(include_archived=True):
            blockers = run_blockers(row)
            versions_out.append({
                "id": int(row["id"]),
                "name": row.get("name"),
                "model": row.get("model"),
                "archived": bool(row.get("archived")),
                "adviceVersion": row.get("advice_version"),
                "reconstructVersion": row.get("reconstruct_version"),
                "blockers": blockers,
                "runnable": not blockers,
            })
        return {
            "caseSets": sets_out,
            "versions": versions_out,
            "judgeVersion": scoring.JUDGE_VERSION,
            "reconstructVersion": CURRENT_RECONSTRUCT_VERSION,
            "p0ExtraRepeats": run_worker.P0_EXTRA_REPEATS,
            "freqMinN": scoring.FREQ_MIN_N,
            "maxCallsPerRun": self.settings.max_calls_per_job,
            "priceModels": sorted(self._price_table().keys()),
        }

    def estimate_score_run(self, payload: dict) -> dict:
        """创建前估算：案例数、P0 数、调用次数、token 与成本。"""
        set_id = self._run_id(payload, "caseSetId", "案例集")
        version_id = self._run_id(payload, "versionId", "版本")
        set_row = self.case_repo.require_set(set_id)
        cases = self.case_repo.items(set_id)
        plan = run_worker.plan_calls(cases) if cases else run_worker.plan_counts(
            int(set_row.get("case_count") or 0),
            int(set_row.get("p0_count") or 0))
        version = self._require_ready_version(version_id)
        history = self.run_repo.avg_tokens(version_id)
        tokens = history or run_worker.DEFAULT_TOKENS
        price = run_worker.price_for(version.get("model"), self._price_table())
        est = run_worker.estimate_cost(plan["calls"], tokens, price)
        _case_specs, unmatched = self._case_spec_ids(cases)
        return {
            "caseSet": self._case_set_summary(set_row),
            "version": {"id": version_id, "name": version.get("name"),
                        "model": version.get("model")},
            **plan, **est,
            "tokens": {
                "in": round(float(tokens.get("tokensIn") or 0), 0),
                "out": round(float(tokens.get("tokensOut") or 0), 0),
            },
            "tokensSource": "history" if history else "default",
            "price": price,
            "priceMissing": price is None,
            "frozen": bool(set_row.get("frozen_at")),
            "runnable": (bool(set_row.get("frozen_at")) and bool(cases)
                         and not unmatched),
            "casesWithoutSpec": unmatched,
            "maxCalls": self.settings.max_calls_per_job,
            "overLimit": plan["calls"] > self.settings.max_calls_per_job,
        }

    def _case_spec_ids(
            self, cases: list[dict]) -> tuple[dict[str, int], list[int]]:
        """案例 → spec id：案例写死的优先，否则按桶匹配 active spec。

        返回 (case_id → spec_id, 匹配不到 spec 的 case_id 列表)。
        """
        active = self._active_specs()
        case_specs: dict[str, int] = {}
        missing: list[int] = []
        for case in cases:
            spec_id = case.get("spec_id")
            if not spec_id:
                spec = case_lib.best_spec(case_lib.case_bucket(case), active)
                spec_id = int(spec["id"]) if spec else None
            if not spec_id:
                missing.append(int(case["id"]))
                continue
            case_specs[str(int(case["id"]))] = int(spec_id)
        return case_specs, missing

    def create_score_run(self, payload: dict) -> dict:
        """冻结「案例集 revision × spec revisions × 版本 × 判定 × 重建」建 run。"""
        set_id = self._run_id(payload, "caseSetId", "案例集")
        version_id = self._run_id(payload, "versionId", "版本")
        set_row = self.case_repo.require_set(set_id)
        if not set_row.get("frozen_at"):
            raise ApiError(
                "CASE_SET_NOT_FROZEN",
                f"案例集 {set_row.get('name')} v{set_row.get('revision')} "
                "还没冻结；评分运行只跑冻结版本（先冻结）", 409)
        cases = self.case_repo.items(set_id)
        if not cases:
            raise ApiError("CASE_SET_EMPTY", "案例集没有案例", 422)
        version = self._require_ready_version(version_id)

        case_specs, missing = self._case_spec_ids(cases)
        if missing:
            raise ApiError(
                "CASE_NO_SPEC",
                f"{len(missing)} 个案例匹配不到 active spec；先补 spec 再建运行",
                422, detail={"caseIds": missing})
        spec_ids = sorted(set(case_specs.values()))

        plan = run_worker.plan_calls(cases)
        # 超上限不是硬拒：创建页已经显示调用数与成本，用户明确确认（confirmLarge）
        # 才继续；和 advice 任务的 TOO_MANY_CALLS 同一套语义。
        if (plan["calls"] > self.settings.max_calls_per_job
                and not bool(payload.get("confirmLarge"))):
            raise ApiError(
                "RUN_TOO_LARGE",
                f"预计调用 {plan['calls']} 次，超过单次上限 "
                f"{self.settings.max_calls_per_job}；确认后再提交",
                409, detail={**plan, "maxCalls": self.settings.max_calls_per_job})

        specs_meta: list[dict] = []
        for spec_id in spec_ids:
            row = self.spec_repo.get(spec_id)
            if row is None:
                raise ApiError("SPEC_NOT_FOUND",
                               f"spec {spec_id} 不存在", 409)
            specs_meta.append({
                "id": int(row["id"]),
                "name": row.get("name"),
                "revision": int(row.get("revision") or 1),
                "bucket": json_load(row.get("bucket_json"), {}) or {},
            })

        history = self.run_repo.avg_tokens(version_id)
        price = run_worker.price_for(version.get("model"), self._price_table())
        snapshot = {
            "caseSet": {
                "id": set_id,
                "name": set_row.get("name"),
                "revision": int(set_row.get("revision") or 1),
                "frozenAt": iso(set_row.get("frozen_at")),
            },
            "version": {
                "id": version_id,
                "name": version.get("name"),
                "model": version.get("model"),
                "configHash": version.get("config_hash"),
                "adviceVersion": version.get("advice_version"),
                "reconstructVersion": version.get("reconstruct_version"),
                "contextMode": version.get("context_mode"),
                "tableStatsMode": version.get("table_stats_mode"),
            },
            "specIds": spec_ids,
            "specs": specs_meta,
            "caseSpecs": case_specs,
            "judgeVersion": scoring.JUDGE_VERSION,
            "reconstructVersion": CURRENT_RECONSTRUCT_VERSION,
            "plan": plan,
            "tokens": history or run_worker.DEFAULT_TOKENS,
            "tokensSource": "history" if history else "default",
            "price": price,
            "createdAt": iso(now_cn()),
        }
        run_id = self.run_repo.create(
            case_set_id=set_id,
            case_set_revision=int(set_row.get("revision") or 1),
            spec_ids=spec_ids,
            version_id=version_id,
            judge_version=scoring.JUDGE_VERSION,
            reconstruct_version=CURRENT_RECONSTRUCT_VERSION,
            total_cases=len(cases),
            config_snapshot=snapshot,
        )
        if self.run_worker is not None:
            try:
                self.run_worker.wake()
            except Exception:  # noqa: BLE001
                log.exception("唤醒评分 worker 失败")
        return self._run_public(self.run_repo.get(run_id) or {})

    def list_score_runs(self, query: dict | None = None) -> dict:
        query = query or {}
        rows = self.run_repo.list(
            limit=self._query_int(query.get("limit"), 50, low=1, high=200),
            case_set_id=self._query_int(query.get("caseSetId"), None, low=1),
            status=(str(query.get("status") or "") or None),
        )
        return {"items": [self._run_public(row) for row in rows]}

    def get_score_run(self, run_id: int) -> dict:
        return self._run_public(self.run_repo.require(run_id))

    def score_run_items(self, run_id: int, query: dict | None = None) -> dict:
        query = query or {}
        run = self.run_repo.require(run_id)
        limit = self._query_int(query.get("limit"), 200, low=1, high=1000)
        offset = self._query_int(query.get("offset"), 0, low=0)
        only_fail = str(query.get("only") or "") == "hardfail"
        rows = self.run_item_repo.list(
            run_id, str(run.get("reconstruct_version")
                        or CURRENT_RECONSTRUCT_VERSION),
            only_hardfail=only_fail, limit=limit, offset=offset)
        return {
            "items": [self.run_item_repo.public_item(row) for row in rows],
            "counts": self.run_item_repo.counts(run_id),
            "limit": limit,
            "offset": offset,
        }

    def score_run_item(self, run_id: int, case_id: int,
                       repeat_index: int = 0) -> dict:
        run = self.run_repo.require(run_id)
        row = self.run_item_repo.get(
            run_id, case_id, repeat_index,
            str(run.get("reconstruct_version") or CURRENT_RECONSTRUCT_VERSION))
        if row is None:
            raise ApiError("RUN_ITEM_NOT_FOUND",
                           f"运行 {run_id} 没有案例 {case_id} 的第 "
                           f"{repeat_index} 次结果", 404)
        item = self.run_item_repo.public_item(row)
        facts = json_load(row.get("facts_json"), {}) or {}
        item.update({
            "facts": facts,
            "options": facts.get("options") or [],
            "prompt": json_load(row.get("prompt_json"), {}) or {},
            "judge": json_load(row.get("judge_json"), {}) or {},
        })
        return item

    def cancel_score_run(self, run_id: int) -> dict:
        row = self.run_repo.request_cancel(run_id)
        if self.run_worker is not None:
            try:
                self.run_worker.wake()
            except Exception:  # noqa: BLE001
                log.exception("唤醒评分 worker 失败")
        return self._run_public(row)

    @staticmethod
    def _run_public(row: dict) -> dict:
        config = json_load(row.get("config_snapshot"), {}) or {}
        metrics = json_load(row.get("metrics_json"), {}) or {}
        return {
            "id": int(row.get("id") or 0),
            "status": row.get("status") or "queued",
            "caseSet": {
                "id": int(row.get("case_set_id") or 0),
                "name": row.get("set_name"),
                "revision": int(row.get("case_set_revision") or 1),
                "frozen": bool(row.get("set_frozen_at")),
            },
            "version": {
                "id": int(row.get("version_id") or 0),
                "name": row.get("version_name"),
                "model": row.get("version_model"),
            },
            "judgeVersion": row.get("judge_version"),
            "reconstructVersion": row.get("reconstruct_version"),
            "specIds": json_load(row.get("spec_ids_json"), []) or [],
            "totalCases": int(row.get("total_cases") or 0),
            "doneCases": int(row.get("done_cases") or 0),
            "scoredCases": int(row.get("scored_cases") or 0),
            "score": (float(row["score"])
                      if row.get("score") is not None else None),
            "p0PassRate": (float(row["p0_pass_rate"])
                           if row.get("p0_pass_rate") is not None else None),
            "coverageRate": (float(row["coverage_rate"])
                             if row.get("coverage_rate") is not None else None),
            "metrics": metrics,
            "config": config,
            "cancelRequested": bool(row.get("cancel_requested")),
            "error": row.get("error_msg") or "",
            "createdAt": iso(row.get("created_at")),
            "startedAt": iso(row.get("started_at")),
            "finishedAt": iso(row.get("finished_at")),
        }

    def build_hand_cases(self, set_id: int, payload: dict) -> dict:
        """整手全建：每个决策点自动挑最具体的 active spec，写死 spec_id。"""
        self.case_repo.require_unfrozen(set_id)
        canonical_id = payload.get("canonicalId")
        if not canonical_id:
            raise ApiError("INVALID_CASE", "整手全建需要 canonicalId", 422)
        if self.canonical_repo.get(int(canonical_id)) is None:
            raise ApiError("HAND_NOT_FOUND",
                           f"canonical {canonical_id} 不存在", 404)
        decisions = self.decision_repo.for_canonical(int(canonical_id))
        if not decisions:
            raise ApiError("DECISION_NOT_FOUND",
                           "这手牌还没有决策点；先跑回填再整手全建", 404)
        rows = [{**row, "canonical_id": int(canonical_id)}
                for row in decisions]
        created, no_spec = self._auto_cases(
            set_id, rows, priority=self._priority(payload), note="整手全建")
        result = self.get_case_set(set_id)
        result.update({"created": created,
                       "existing": len(rows) - len(no_spec) - created,
                       "noSpec": no_spec})
        return result

    def _auto_cases(self, set_id: int, rows: Iterable[dict], *, priority: int,
                    note: str, limit: int | None = None,
                    skip: set[tuple] | None = None) -> tuple[int, list[str]]:
        """给一批决策点自动配最具体的 active spec，返回 (新建数, 没 spec 的键)。"""
        active = self._active_specs()
        skip = set(skip or ())
        created = 0
        no_spec: list[str] = []
        for row in rows:
            if limit is not None and created >= limit:
                break
            canonical_id = int(row["canonical_id"])
            street = row.get("street")
            seq = int(row.get("seq") or 0)
            if (canonical_id, street, seq) in skip:
                continue
            key = library.decision_key(street, seq)
            spec = case_lib.best_spec(case_lib.bucket_of_row(row), active)
            if spec is None:
                no_spec.append(key)
                continue
            _, was_created = self.case_repo.create(
                canonical_id=canonical_id, decision_key=key,
                spec_id=int(spec["id"]), case_set_id=int(set_id),
                priority=priority, note=note)
            if was_created:
                created += 1
                skip.add((canonical_id, street, seq))
        return created, no_spec

    def sample_case_set_cases(self, set_id: int, payload: dict) -> dict:
        """按桶补样：从存档里按时间倒序抽符合条件的决策点建 draft 案例。"""
        self.case_repo.require_unfrozen(set_id)
        bucket = specs.validate_bucket(payload.get("bucket") or {})
        count = int(payload.get("count") or 10)
        if not 1 <= count <= 100:
            raise ApiError("INVALID_CASE_SET", "count 只能是 1–100", 422)
        priority = self._priority(payload)
        taken = {
            (int(item["canonical_id"]),
             case_lib.street_of(item),
             int(str(item["decision_key"]).split(":", 1)[1]))
            for item in self.case_repo.items(set_id)
        }
        rows = self.case_repo.decisions_for_bucket(
            bucket, limit=max(count * 4, count), exclude=sorted(taken))
        created, no_spec = self._auto_cases(
            set_id, rows, priority=priority, note="按桶补样", limit=count,
            skip=taken)
        result = self.get_case_set(set_id)
        result.update({"created": created, "noSpec": no_spec})
        return result

    def _seed_target(self, case: dict) -> tuple[int, str]:
        """ab_set 的 hand uid → (canonical_id, decision_key)。"""
        hand = self.hand_repo.imports.find_any_by_uid(case["handUid"])
        if hand is None:
            raise ApiError(
                "SEED_HAND_MISSING",
                f"手牌 {case['handUid']} 不在导入库（先跑导入）", 404)
        edge = self.canonical_repo.source_of(hand["source"], int(hand["id"]))
        if edge is None or not edge.get("canonical_id"):
            raise ApiError(
                "SEED_CANONICAL_MISSING",
                f"手牌 {case['handUid']} 还没回填 canonical（先跑回填）", 409)
        canonical_id = int(edge["canonical_id"])
        rows = self.decision_repo.for_canonical(canonical_id)
        match = next(
            (row for row in rows
             if row.get("street") == case["street"]
             and int(row.get("action_index") or -1) == case["actionIndex"]),
            None)
        if match is None:
            raise ApiError(
                "SEED_DECISION_MISSING",
                f"决策点 {case['street']}#{case['actionIndex']} 不在 canonical "
                f"{canonical_id} 上", 404)
        return canonical_id, library.decision_key(match.get("street"),
                                                  match.get("seq"))

    def seed_case_sets(self, payload: dict) -> dict:
        """把 ab_set.json 导成 seed 案例集；同名 + 同 revision 不重复建。"""
        path = payload.get("path")
        if path:
            candidate = Path(str(path)).expanduser().resolve()
            if not candidate.is_relative_to(case_seed.REPO_ROOT.resolve()):
                raise ApiError(
                    "INVALID_SEED_PATH",
                    f"seed 文件必须在仓库内：{case_seed.REPO_ROOT}", 422)
            path = str(candidate)
        plan = case_seed.plan_seed(case_seed.load_ab_set(path))
        set_name = str(payload.get("setName") or plan["setName"]).strip()
        dry_run = bool(payload.get("dryRun"))
        report: dict[str, Any] = {
            "dryRun": dry_run,
            "source": str(path or case_seed.AB_SET_PATH),
            "setName": set_name,
            "set": None,
            "specs": {"created": [], "reused": [], "wouldCreate": [],
                      "diverged": []},
            "cases": {"created": [], "existing": [], "skipped": [],
                      "wouldCreate": []},
        }

        set_row = self.case_repo.set_by_name_revision(set_name, 1)
        if set_row is None:
            if dry_run:
                report["set"] = {"name": set_name, "revision": 1,
                                 "wouldCreate": True}
            else:
                set_row = self.case_repo.create_set(name=set_name,
                                                    purpose="golden")
                report["set"] = self._case_set_summary(set_row)
        else:
            report["set"] = self._case_set_summary(set_row)

        resolved: dict[str, dict] = {}
        for planned in plan["specs"]:
            name = planned["name"]
            latest = self.spec_repo.latest(name)
            if (latest is not None
                    and specs.spec_from_row(latest) == planned["content"]):
                resolved[name] = latest
                report["specs"]["reused"].append(name)
                continue
            if dry_run:
                report["specs"]["wouldCreate"].append(name)
                continue
            if latest is None:
                spec_id = self.spec_repo.create(
                    name=name, revision=1, status="active",
                    content=planned["content"], note=planned["note"],
                    supersedes=None)
                self.spec_repo.activate(spec_id, name)
            else:
                # 同名已有别的版本：只追加一条 draft，不 activate ——
                # 生效与否是人的决定，seed 不能把人工写的 active 顶掉。
                spec_id = self.spec_repo.create(
                    name=name, revision=int(latest["revision"]) + 1,
                    status="draft", content=planned["content"],
                    note=planned["note"], supersedes=int(latest["id"]))
                report["specs"]["diverged"].append(name)
            resolved[name] = self.spec_repo.get(spec_id) or {}
            report["specs"]["created"].append(name)

        frozen = bool((set_row or {}).get("frozen_at"))
        for case in plan["cases"]:
            seed_id = case["seedId"]
            if frozen and not dry_run:
                report["cases"]["skipped"].append({
                    "seedId": seed_id,
                    "reason": f"案例集 {set_name} v1 已冻结；请建新版本",
                })
                continue
            try:
                canonical_id, decision_key = self._seed_target(case)
            except ApiError as exc:
                report["cases"]["skipped"].append({
                    "seedId": seed_id,
                    "code": exc.code,
                    "reason": exc.message,
                })
                continue
            spec_row = resolved.get(case["specName"])
            if dry_run:
                report["cases"]["wouldCreate"].append({
                    "seedId": seed_id, "canonicalId": canonical_id,
                    "decisionKey": decision_key,
                    "specName": case["specName"],
                })
                continue
            if spec_row is None:
                report["cases"]["skipped"].append({
                    "seedId": seed_id,
                    "reason": f"spec {case['specName']} 未落库",
                })
                continue
            try:
                _, was_created = self.case_repo.create(
                    canonical_id=canonical_id, decision_key=decision_key,
                    spec_id=int(spec_row["id"]), case_set_id=int(set_row["id"]),
                    priority=case["priority"], note=case["note"])
            except ApiError as exc:
                report["cases"]["skipped"].append({
                    "seedId": seed_id, "code": exc.code,
                    "reason": exc.message,
                })
                continue
            bucket = ("created" if was_created else "existing")
            report["cases"][bucket].append({
                "seedId": seed_id, "canonicalId": canonical_id,
                "decisionKey": decision_key, "specName": case["specName"],
            })

        report["cases"]["skippedPlan"] = plan["skipped"]
        return report

    def hand_replay(self, hand_id: int) -> dict:
        """瀑布流/牌桌共用的帧序列。优先读 ingest 时物化的 timeline。"""
        hand_id = self.resolve_hand_id(hand_id)
        row = self.hand_repo.get_row(hand_id)
        if row is None:
            raise ApiError("HAND_NOT_FOUND", f"手牌 {hand_id} 不存在或已删除", 404)
        analysis = self.analyze(row)
        row = dict(row)
        if isinstance(row.get("streets"), str):
            row["streets"] = json_load(row.get("streets"), []) or []
        if isinstance(row.get("table_roster"), str):
            row["table_roster"] = json_load(row.get("table_roster"), []) or []
        if isinstance(row.get("villains"), str):
            row["villains"] = json_load(row.get("villains"), []) or []

        advices = self.live_advice_repo.list_for_hand(hand_id)
        if not advices:
            payload = self.import_repo.get_payload(hand_id)
            value = payload.get("advices") if isinstance(payload, dict) else None
            if isinstance(value, list):
                advices = [a for a in value if isinstance(a, dict)]
        persisted = self.decision_repo.for_hand(hand_id, analysis.fingerprint)
        decision_ids: dict[tuple[str, int], int] = {}
        for (street, _seq), item in persisted.items():
            if item.get("id") is not None:
                decision_ids[(street, int(item.get("action_index") or 0))] = int(item["id"])
        timeline = {}
        try:
            timeline = self.hand_repo.get_timeline(hand_id)
        except Exception:  # noqa: BLE001
            timeline = {}
        if not isinstance(timeline, dict) or not timeline.get("frames"):
            timeline = build_timeline(row, analysis, advices,
                                      decision_ids=decision_ids)
        frames = timeline.get("frames") if isinstance(timeline, dict) else []
        decisions = []
        advice_by_key = {
            (str(item.get("street") or ""), int(item.get("action_index") or 0)): item
            for item in advices
        }
        for decision in analysis.decisions:
            item = self._decision_public(decision, persisted.get(
                (decision.street, decision.seq)))
            advice = advice_by_key.get((decision.street,
                                        int(decision.action_index or 0)))
            if advice:
                item["adviceId"] = advice.get("id")
                item["promptSha256"] = advice.get("prompt_sha256")
                item["promptMissing"] = bool(advice.get("prompt_missing"))
                item["advice"] = self._advice_public(advice)
            decisions.append(item)
        return {
            "hand": {
                "id": int(hand_id),
                "uid": row.get("external_uid") or row.get("external_id"),
                "heroCards": cards_of(row.get("hero_cards")),
                "heroPosition": row.get("hero_position"),
                "tableSize": int(row.get("table_size") or 0),
                "board": cards_of(row.get("board")),
                "result": {
                    "heroNetBb": row.get("result_amount"),
                    "outcome": row.get("result") or "unknown",
                    "source": row.get("result_source") or "unknown",
                },
                "quality": analysis.quality,
                "warnings": list(analysis.warnings),
                "partial": bool(row.get("partial")),
                "createdAt": iso(row.get("created_at")),
            },
            "timeline": timeline,
            "frames": frames,
            "decisions": decisions,
        }

    def hand_prompt(self, hand_id: int, advice_id: int) -> dict:
        """按需加载某一决策点的 prompt / raw / facts / context。"""
        advice = self.live_advice_repo.get(advice_id)
        if advice is None or int(advice.get("hand_id") or 0) != int(hand_id):
            raise ApiError("ADVICE_NOT_FOUND",
                           f"建议 {advice_id} 不属于手牌 {hand_id}", 404)
        system = advice.get("prompt_system") or ""
        user = advice.get("prompt_user") or ""
        missing = bool(advice.get("prompt_missing"))
        missing_reason = advice.get("prompt_missing_reason")
        if not missing and not system and not user:
            # 迁移前的老记录没有 prompt 列；明确标缺失，不要返回空字符串
            # 让前端误以为「prompt 就是空的」。
            missing = True
            missing_reason = missing_reason or "legacy_no_prompt"
        return {
            "adviceId": int(advice_id),
            "handId": int(hand_id),
            "street": advice.get("street"),
            "actionIndex": advice.get("action_index"),
            "promptVersion": advice.get("prompt_version"),
            "promptSha256": advice.get("prompt_sha256"),
            "promptMissing": missing,
            "promptMissingReason": missing_reason,
            "promptBytes": len((system + user).encode("utf-8")),
            "system": system,
            "user": user,
            "rawResponse": advice.get("raw_response"),
            "reasonFull": advice.get("reason_full"),
            "facts": json_load(advice.get("facts_json"), {}) or {},
            "context": json_load(advice.get("context_json"), {}) or {},
            "modelParams": json_load(advice.get("model_params_json"), {}) or {},
            "capturedAt": iso(advice.get("captured_at")),
            "tokensIn": advice.get("tokens_in"),
            "tokensOut": advice.get("tokens_out"),
            "durationMs": advice.get("duration_ms"),
        }

    def _streets_public(self, raw_streets: list[dict],
                        analysis: HandAnalysis) -> list[dict]:
        seq_by_action = {(d.street, d.action_index): d.seq
                         for d in analysis.decisions}
        first_pot = {}
        for d in analysis.decisions:
            first_pot.setdefault(d.street, d.snap.pot_before_street)
        out = []
        for rec in raw_streets:
            street = rec.get("street")
            if street not in STREET_LABELS:
                continue
            actions = []
            for idx, a in enumerate(rec.get("actions") or []):
                actor = a.get("actor")
                action = a.get("action")
                amount = a.get("amountBb")
                is_hero = actor == "hero"
                actions.append({
                    "index": idx,
                    "actor": actor,
                    "actorLabel": "Hero" if is_hero else (actor or "?"),
                    "action": action,
                    "amountBb": amount,
                    "isHero": is_hero,
                    "decisionSeq": seq_by_action.get((street, idx)),
                    "label": self._action_label(actor, action, amount, is_hero),
                })
            out.append({
                "street": street,
                "label": STREET_LABELS[street],
                "potStartBb": rec.get("potStartBb", first_pot.get(street)),
                "actions": actions,
                "decisionSeqs": [a["decisionSeq"] for a in actions
                                 if a["decisionSeq"] is not None],
            })
        return out

    def _action_label(self, actor: str | None, action: str | None,
                      amount: float | None, is_hero: bool) -> str:
        who = "Hero" if is_hero else (actor or "?")
        if action == "check":
            return f"{who} check"
        if action == "fold":
            return f"{who} fold"
        if action == "call":
            return f"{who} call" + (f" {amount:g}" if amount is not None else "")
        if action == "bet":
            return f"{who} bet" + (f" {amount:g}" if amount is not None else "")
        if action == "raise":
            return f"{who} raise {amount:g}" if amount is not None else f"{who} raise"
        if action == "allin":
            return f"{who} all-in" + (f" {amount:g}" if amount is not None else "")
        return f"{who} {action or ''}".strip()

    def _decision_public(self, d, persisted: dict | None,
                         mark: dict | None = None) -> dict:
        return {
            "id": persisted.get("id") if persisted else None,
            "canonicalId": (persisted.get("canonical_id") if persisted else None),
            "decisionKey": library.decision_key(d.street, d.seq),
            "buckets": ({dimension: persisted.get(dimension)
                         for dimension in library.DIMENSIONS
                         if persisted.get(dimension) not in (None, "")}
                        if persisted else {}),
            "street": d.street,
            "seq": d.seq,
            "actionIndex": d.action_index,
            "pot": d.pot,
            "toCall": d.snap.to_call,
            "callAmount": call_amount(d.snap.to_call, d.snap.hero_stack),
            "heroStack": d.snap.hero_stack,
            "heroPosition": d.snap.hero_position,
            "heroCards": list(d.snap.hero_cards),
            "board": list(d.snap.board),
            "players": d.snap.table_size,
            "opponents": d.snap.opponents_in_hand,
            "quality": d.quality,
            "warnings": list(d.warnings),
            "actual": {
                "action": d.actual,
                "amountBb": d.actual_amount,
                "label": fmt_option(d.actual, d.actual_amount),
            },
            "review": ({"verdict": mark.get("verdict"),
                        "tags": json_load(mark.get("tags"), []) or [],
                        "note": mark.get("note") or ""}
                       if mark else None),
            "snapshot": {
                "preflopShapes": list(d.snap.preflop_shapes),
                "heroIsPfr": d.snap.hero_is_pfr,
                "bombPot": d.snap.bomb_pot,
                "potBeforeStreet": d.snap.pot_before_street,
            },
        }

    def mark_decision(self, decision_id: int, payload: dict) -> dict:
        decision = self.decision_repo.get(decision_id)
        if decision is None:
            raise ApiError("DECISION_NOT_FOUND",
                           f"决策点 {decision_id} 不存在", 404)
        verdict = str(payload.get("verdict") or "unsure").strip().lower()
        if verdict not in ("good", "bad", "unsure"):
            raise ApiError("INVALID_MARK",
                           "verdict 只能是 good/bad/unsure", 422)
        tags = payload.get("tags")
        if tags is not None:
            tags = [str(t).strip() for t in tags if str(t).strip()][:12]
        note = str(payload.get("note") or "")[:4000]
        result = self.decision_mark_repo.upsert(
            decision_id, verdict=verdict, tags=tags, note=note)
        if verdict == "bad":
            try:
                hand_id = decision.get("hand_id")
                if hand_id:
                    self.hand_repo.add_flag(
                        int(hand_id), "REVIEW_BAD", "人工标记为坏决策")
            except Exception:  # noqa: BLE001
                log.exception("回写 REVIEW_BAD 失败 decision=%s", decision_id)
        return result

    def patch_mark(self, hand_id: int, payload: dict) -> dict:
        row = self.hand_repo.get_row(hand_id)
        if row is None:
            raise ApiError("HAND_NOT_FOUND", f"手牌 {hand_id} 不存在或已删除", 404)
        favorite = _parse_bool(payload.get("favorite"))
        tags = payload.get("tags")
        if tags is not None:
            tags = [str(t).strip() for t in tags if str(t).strip()][:12]
        status = payload.get("status")
        if status is not None and status not in MARK_STATUSES:
            raise ApiError("INVALID_MARK", f"status 只能是 {', '.join(MARK_STATUSES)}", 422)
        note = payload.get("note")
        if note is not None:
            note = str(note)[:2000]
        return self.mark_repo.upsert(hand_id, favorite=favorite if "favorite" in payload else None,
                                     tags=tags,
                                     status=status,
                                     note=note)

    # ─────────────────────────── 版本 ───────────────────────────

    def list_versions(self, include_archived: bool = False) -> list[dict]:
        rows = self.version_repo.list(include_archived)
        stats = self.version_repo.task_stats()
        return [public_version(r, task_count=stats.get(int(r["id"]), {}).get("taskCount", 0),
                               last_used_at=stats.get(int(r["id"]), {}).get("lastUsedAt"))
                for r in rows]

    def _version_fields(self, payload: dict, *, name: str | None = None) -> dict:
        provider = (payload.get("provider") or "").strip().lower() or "deepseek"
        base_url = (payload.get("baseUrl") or payload.get("base_url") or "").strip()
        model = (payload.get("model") or "").strip()
        api_key_ref = (payload.get("apiKeyRef") or payload.get("api_key_ref")
                       or "AI_SERVER_API_KEY").strip()
        prompt_style = (payload.get("promptStyle") or payload.get("prompt_style")
                        or "lean").strip()
        thinking = payload.get("thinking") or None
        if isinstance(thinking, str):
            thinking = thinking.strip() or None
        effort = payload.get("reasoningEffort") or payload.get("reasoning_effort") or None
        if isinstance(effort, str):
            effort = effort.strip() or None
        temperature = payload.get("temperature")
        top_p = payload.get("topP", payload.get("top_p"))
        max_tokens = payload.get("maxTokens", payload.get("max_tokens"))
        validate_config_values(
            provider=provider, base_url=base_url, model=model,
            api_key_ref=api_key_ref, prompt_style=prompt_style,
            thinking=thinking, reasoning_effort=effort,
            temperature=temperature, top_p=top_p, max_tokens=max_tokens,
        )
        mode = payload.get("tableDynamicsMode") or "current"
        text = payload.get("tableDynamicsText")
        dynamics = normalize_table_dynamics(mode, text)
        sha = table_dynamics_sha(dynamics)
        context_mode = str(payload.get("contextMode")
                           or payload.get("context_mode") or "none").lower()
        table_stats_mode = str(payload.get("tableStatsMode")
                               or payload.get("table_stats_mode") or "none").lower()
        if context_mode not in ("none", "snapshot", "recompute"):
            raise ApiError("INVALID_VERSION_CONFIG",
                           f"未知 contextMode：{context_mode}", 422)
        if table_stats_mode not in ("none", "snapshot", "recompute"):
            raise ApiError("INVALID_VERSION_CONFIG",
                           f"未知 tableStatsMode：{table_stats_mode}", 422)
        config = config_payload(
            provider=provider, base_url=base_url, model=model,
            temperature=temperature, top_p=top_p, max_tokens=max_tokens,
            thinking=thinking, reasoning_effort=effort,
            prompt_style=prompt_style, api_key_ref=api_key_ref,
            dynamics_sha=sha, context_mode=context_mode,
            table_stats_mode=table_stats_mode,
        )
        commit, dirty = engine_state()
        return {
            "user_id": self.settings.console_user_id,
            "name": name or (payload.get("name") or "").strip() or
            default_version_name(model, prompt_style, now_cn().strftime("%m%d")),
            "provider": provider, "base_url": base_url.rstrip("/"),
            "model": model, "api_key_ref": api_key_ref,
            "temperature": temperature, "top_p": top_p,
            "max_tokens": int(max_tokens) if max_tokens is not None else None,
            "thinking": thinking, "reasoning_effort": effort,
            "prompt_style": prompt_style,
            "advice_version": CURRENT_ADVICE_VERSION,
            "reconstruct_version": CURRENT_RECONSTRUCT_VERSION,
            "engine_commit": commit, "engine_dirty": 1 if dirty else 0,
            "table_dynamics": dynamics or None,
            "table_dynamics_sha": sha,
            "context_mode": context_mode,
            "table_stats_mode": table_stats_mode,
            "config_hash": config_hash(config),
            "notes": (payload.get("notes") or "").strip() or None,
            "archived": 0,
            "config_payload": config,
        }

    def create_version(self, payload: dict) -> dict:
        fields = self._version_fields(payload)
        existing = self.version_repo.get_by_hash(fields["config_hash"])
        if existing:
            raise ApiError("VERSION_DUPLICATE",
                           "配置完全相同的版本已存在", 409,
                           {"versionId": int(existing["id"]),
                            "name": existing["name"]})
        if self.version_repo.name_exists(fields["name"]):
            raise ApiError("VERSION_NAME_EXISTS",
                           f"版本名 {fields['name']} 已存在", 409)
        row = self.version_repo.create(fields)
        return public_version(row)

    def import_env(self) -> dict:
        settings = AISettings.from_env()
        if settings is None:
            raise ApiError("AI_KEY_MISSING",
                           "当前环境没有 AI_SERVER_API_KEY / BASE_URL / MODEL", 422)
        dynamics = normalize_table_dynamics("current")
        model = settings.model
        style = "lean"
        base_name = default_version_name(model, style, now_cn().strftime("%m%d"))
        payload = {
            "name": base_name,
            "provider": provider_from_url(settings.base_url),
            "baseUrl": settings.base_url,
            "model": model,
            "apiKeyRef": "AI_SERVER_API_KEY",
            "temperature": settings.temperature,
            "topP": settings.top_p,
            "maxTokens": settings.max_tokens,
            "thinking": settings.thinking,
            "reasoningEffort": settings.reasoning_effort,
            "promptStyle": style,
            "tableDynamicsMode": "text",
            "tableDynamicsText": dynamics,
            "notes": "从当前 .env / .table_dy 导入",
        }
        fields = self._version_fields(payload)
        existing = self.version_repo.get_by_hash(fields["config_hash"])
        if existing:
            return {"version": public_version(existing), "reused": True}
        name = base_name
        suffix = 2
        while self.version_repo.name_exists(name):
            name = f"{base_name}-{suffix}"
            suffix += 1
        fields["name"] = name
        row = self.version_repo.create(fields)
        return {"version": public_version(row), "reused": False}

    def clone_version(self, version_id: int, payload: dict) -> dict:
        source = self.version_repo.get(version_id)
        if source is None:
            raise ApiError("VERSION_NOT_FOUND", f"版本 {version_id} 不存在", 404)
        merged = {
            "provider": payload.get("provider") or source["provider"],
            "baseUrl": payload.get("baseUrl") or source["base_url"],
            "model": payload.get("model") or source["model"],
            "apiKeyRef": payload.get("apiKeyRef") or source["api_key_ref"],
            "temperature": payload["temperature"] if "temperature" in payload else source["temperature"],
            "topP": payload["topP"] if "topP" in payload else source["top_p"],
            "maxTokens": payload["maxTokens"] if "maxTokens" in payload else source["max_tokens"],
            "thinking": payload["thinking"] if "thinking" in payload else source["thinking"],
            "reasoningEffort": (payload["reasoningEffort"]
                                if "reasoningEffort" in payload
                                else source["reasoning_effort"]),
            "promptStyle": payload.get("promptStyle") or source["prompt_style"],
            "notes": payload.get("notes") or source.get("notes"),
            "tableDynamicsMode": payload.get("tableDynamicsMode") or "keep",
            "tableDynamicsText": payload.get("tableDynamicsText"),
            "contextMode": (payload.get("contextMode")
                            or source.get("context_mode") or "none"),
            "tableStatsMode": (payload.get("tableStatsMode")
                               or source.get("table_stats_mode") or "none"),
        }
        mode = merged.pop("tableDynamicsMode")
        if mode == "keep":
            dynamics = source.get("table_dynamics") or ""
            merged["tableDynamicsMode"] = "text"
            merged["tableDynamicsText"] = dynamics
        else:
            merged["tableDynamicsMode"] = mode
        base_name = (payload.get("name") or "").strip() or f"{source['name']}-copy"
        name = base_name
        suffix = 2
        while self.version_repo.name_exists(name):
            name = f"{base_name}-{suffix}"
            suffix += 1
        fields = self._version_fields(merged, name=name)
        if self.version_repo.get_by_hash(fields["config_hash"]):
            raise ApiError("VERSION_DUPLICATE",
                           "克隆后的配置与已有版本完全相同", 409)
        row = self.version_repo.create(fields)
        return public_version(row)

    def update_version(self, version_id: int, payload: dict) -> dict:
        source = self.version_repo.get(version_id)
        if source is None:
            raise ApiError("VERSION_NOT_FOUND", f"版本 {version_id} 不存在", 404)
        name = payload.get("name")
        if name is not None:
            name = str(name).strip()
            if not name:
                raise ApiError("INVALID_VERSION_CONFIG", "版本名不能为空", 422)
            if self.version_repo.name_exists(name, exclude_id=version_id):
                raise ApiError("VERSION_NAME_EXISTS", f"版本名 {name} 已存在", 409)
        notes = payload.get("notes")
        archived = _parse_bool(payload.get("archived"))
        row = self.version_repo.update_meta(version_id, name=name, notes=notes,
                                            archived=archived)
        return public_version(row)

    def _validate_versions(self, version_ids: list[int]) -> list[dict]:
        if not version_ids:
            raise ApiError("INVALID_VERSION_CONFIG", "至少选择一个版本", 422)
        rows = []
        for vid in version_ids:
            row = self.version_repo.get(vid)
            if row is None:
                raise ApiError("VERSION_NOT_FOUND", f"版本 {vid} 不存在", 404)
            blockers = run_blockers(row)
            if blockers:
                first = blockers[0]
                code = first["code"]
                status = 422
                raise ApiError(code, first["message"], status,
                               {"versionId": vid, "blockers": blockers})
            rows.append(row)
        return rows

    # ─────────────────────────── 范围解析 ───────────────────────────

    def _all_hand_data(self) -> tuple[list[dict], dict[int, HandAnalysis],
                                      dict[int, dict]]:
        rows = self.hand_repo.list_rows()
        analyses = {int(r["id"]): self.analyze(r) for r in rows}
        last_runs = self._last_run_map([int(r["id"]) for r in rows])
        return rows, analyses, last_runs

    def _scope_rows(self, scope: str, hand_ids: list[int],
                    filters: dict) -> list[dict]:
        rows, _analyses, _last = self._all_hand_data()
        if scope == "hands":
            wanted = {int(x) for x in hand_ids}
            found = [r for r in rows if int(r["id"]) in wanted]
            found_ids = {int(r["id"]) for r in found}
            missing = wanted - found_ids
            if missing:
                raise ApiError("HAND_NOT_FOUND",
                               f"手牌不存在：{sorted(missing)[:10]}", 404)
            return found
        if scope == "favorites":
            return [r for r in rows if bool(r.get("mark_favorite"))]
        if scope == "filter":
            return self._filter_rows(rows, _analyses, _last, filters)
        raise ApiError("INVALID_SCOPE", f"未知范围：{scope}", 422)

    def resolve_scope(self, payload: dict) -> dict:
        scope = (payload.get("scope") or "hands").strip()
        hand_ids = [int(x) for x in (payload.get("handIds") or payload.get("hand_ids") or [])]
        version_ids = list(dict.fromkeys(
            int(x) for x in (payload.get("versionIds")
                             or payload.get("version_ids") or [])))
        repeat = max(1, min(5, int(payload.get("repeat") or 1)))
        dedupe = _parse_bool(payload.get("dedupe"))
        if dedupe is None:
            dedupe = True
        include_quality = [q.lower() for q in
                           _as_list(payload.get("includeQuality"))] or [EXACT, APPROX]
        include_quality = [q for q in include_quality if q in (EXACT, APPROX)]
        if not include_quality:
            include_quality = [EXACT]
        versions = self._validate_versions(version_ids) if version_ids else []
        filters = payload.get("filter") or {}
        rows = self._scope_rows(scope, hand_ids, filters)
        analyses = {int(r["id"]): self.analyze(r) for r in rows}

        # 去重：同指纹保留 id 最大的一手
        groups: dict[str, list[int]] = {}
        for row in rows:
            groups.setdefault(analyses[int(row["id"])].fingerprint,
                              []).append(int(row["id"]))
        winners = {max(ids) for ids in groups.values()}
        skipped_duplicates: list[dict] = []
        skipped_invalid: list[dict] = []
        skipped_quality: list[dict] = []
        included: list[tuple[dict, HandAnalysis, list]] = []
        seen_fp: set[str] = set()
        for row in sorted(rows, key=lambda r: int(r["id"]), reverse=True):
            hand_id = int(row["id"])
            analysis = analyses[hand_id]
            if dedupe and hand_id not in winners:
                skipped_duplicates.append({"handId": hand_id,
                                           "reason": "内容重复，保留较新的手牌"})
                continue
            if dedupe:
                if analysis.fingerprint in seen_fp:
                    skipped_duplicates.append({"handId": hand_id,
                                               "reason": "内容重复"})
                    continue
                seen_fp.add(analysis.fingerprint)
            if analysis.quality == INVALID or not analysis.decisions:
                skipped_invalid.append({"handId": hand_id,
                                        "reason": analysis.invalid_reason or "不可重跑"})
                continue
            decisions = [d for d in analysis.decisions
                         if d.quality in include_quality]
            if not decisions:
                skipped_quality.append({
                    "handId": hand_id,
                    "reason": f"没有符合质量要求的决策点（{','.join(include_quality)}）",
                })
                continue
            included.append((row, analysis, decisions))

        hand_count = len(included)
        decision_count = sum(len(x[2]) for x in included)
        total_units = decision_count * max(1, len(versions)) * repeat
        skipped_units = 0
        for item in skipped_duplicates + skipped_invalid + skipped_quality:
            row = next((r for r in rows if int(r["id"]) == item["handId"]), None)
            if row:
                count = len(analyses[item["handId"]].decisions) or 1
                skipped_units += count * max(1, len(versions)) * repeat
        estimate = self._estimate(versions, included, repeat)
        return {
            "scope": scope,
            "versions": [self._version_snapshot(v) for v in versions],
            "handIds": [int(x[0]["id"]) for x in included],
            "handCount": hand_count,
            "decisionCount": decision_count,
            "totalUnits": total_units,
            "repeat": repeat,
            "dedupe": dedupe,
            "includeQuality": include_quality,
            "estimatedSeconds": estimate,
            "skipped": {
                "duplicates": len(skipped_duplicates),
                "invalid": len(skipped_invalid),
                "lowQuality": len(skipped_quality),
                "duplicateHands": skipped_duplicates[:20],
                "invalidHands": skipped_invalid[:20],
                "lowQualityHands": skipped_quality[:20],
                "units": skipped_units,
            },
            "_included": included,
        }

    def _estimate(self, versions: list[dict],
                  included: list[tuple[dict, HandAnalysis, list]],
                  repeat: int) -> float | None:
        if not versions:
            return None
        seconds = 0.0
        decision_count = sum(len(x[2]) for x in included)
        for version in versions:
            p50 = self.version_repo.p50_latency(int(version["id"])) or 1.2
            seconds += decision_count * repeat * p50
        seconds += len(included) * 0.15
        return round(seconds, 1)

    def preview_run(self, payload: dict) -> dict:
        resolved = self.resolve_scope(payload)
        resolved.pop("_included", None)
        return resolved

    # ─────────────────────────── 创建任务 ───────────────────────────

    def create_run(self, payload: dict) -> dict:
        scope = (payload.get("scope") or "hands").strip()
        hand_ids = [int(x) for x in (payload.get("handIds")
                                     or payload.get("hand_ids") or [])]
        version_ids = list(dict.fromkeys(
            int(x) for x in (payload.get("versionIds")
                             or payload.get("version_ids") or [])))
        versions = self._validate_versions(version_ids)
        repeat = max(1, min(5, int(payload.get("repeat") or 1)))
        confirm_large = bool(payload.get("confirmLarge"))
        if self.job_repo.queue_depth() >= self.settings.max_queue_depth:
            raise ApiError("JOB_QUEUE_FULL",
                           f"队列已有 {self.settings.max_queue_depth} 个任务在排队/运行",
                           409)
        resolved = self.resolve_scope(payload)
        if resolved["handCount"] == 0 or resolved["totalUnits"] == 0:
            raise ApiError("NO_HANDS", "这个范围里没有可重跑的手牌/决策点", 422)
        if (resolved["totalUnits"] > self.settings.max_calls_per_job
                and not confirm_large):
            raise ApiError(
                "TOO_MANY_CALLS",
                f"本次需要 {resolved['totalUnits']} 次调用，超过单任务上限 "
                f"{self.settings.max_calls_per_job}；请确认后再提交",
                409, {"estimate": {k: resolved[k] for k in
                                   ("handCount", "decisionCount", "totalUnits",
                                    "estimatedSeconds")}})

        included = resolved["_included"]
        # 决策点先落库：任务/worker 都引用同一份冻结快照
        for _row, analysis, _decisions in included:
            self.decision_repo.persist(analysis)
        job_id = self.job_repo.create(
            scope=scope, version_ids=version_ids,
            hand_ids=[int(x[0]["id"]) for x in included],
            repeat=repeat,
            total_hands=resolved["handCount"],
            total_decisions=resolved["decisionCount"],
            total_units=resolved["totalUnits"],
            skipped_units=resolved["skipped"]["units"],
            config_snapshot={
                "versions": [self._version_snapshot(v) for v in versions],
                "current": {
                    "adviceVersion": CURRENT_ADVICE_VERSION,
                    "reconstructVersion": CURRENT_RECONSTRUCT_VERSION,
                },
                "engine": dict(zip(("commit", "dirty"), engine_state())),
                "includeQuality": resolved["includeQuality"],
                "dedupe": resolved["dedupe"],
                "skipped": resolved["skipped"],
                "estimatedSeconds": resolved["estimatedSeconds"],
            },
            note=(payload.get("note") or "").strip()[:255] or None,
            parent_job_id=(int(payload["parentJobId"])
                           if payload.get("parentJobId") else None),
        )
        self.job_repo.add_hands(job_id, [
            (int(row["id"]), analysis.fingerprint, len(decisions))
            for row, analysis, decisions in included
        ])
        if self.runner is not None:
            self.runner.wake()
        return {
            "jobId": job_id,
            "status": "queued",
            "scope": scope,
            "handCount": resolved["handCount"],
            "decisionCount": resolved["decisionCount"],
            "totalUnits": resolved["totalUnits"],
            "estimatedSeconds": resolved["estimatedSeconds"],
            "skipped": resolved["skipped"],
        }

    # ─────────────────────────── 手牌集 ───────────────────────────

    def _set_item_public(self, item: dict, row: dict | None,
                         analysis: HandAnalysis | None) -> dict:
        flags = json_load(row.get("flags_json"), {}) if row else {}
        return {
            "handId": int(item["hand_id"]),
            "fingerprint": item.get("fingerprint"),
            "role": item.get("role") or "regression",
            "priority": int(item.get("priority") or 0),
            "stratum": item.get("stratum"),
            "note": item.get("note") or "",
            "expect": json_load(item.get("expect_json"), {}) or {},
            "quality": analysis.quality if analysis else "missing",
            "stale": bool(analysis and analysis.fingerprint != item.get("fingerprint")),
            "flags": flags,
            "hand": ({
                "id": int(row["id"]),
                "title": row.get("title") or "",
                "heroCards": row.get("hero_cards") or "",
                "board": row.get("board") or "",
                "heroPosition": row.get("hero_position"),
                "result": row.get("result") or "unknown",
                "resultAmount": row.get("result_amount"),
                "reviewScore": int(row.get("review_score") or 0),
            } if row else None),
        }

    def get_hand_set(self, set_id: int) -> dict:
        row = self.hand_set_repo.get(set_id)
        if row is None:
            raise ApiError("HAND_SET_NOT_FOUND", f"手牌集 {set_id} 不存在", 404)
        items = self.hand_set_repo.items(set_id)
        hand_ids = [int(item["hand_id"]) for item in items]
        rows = {int(r["id"]): r for r in self.hand_repo.get_by_ids(hand_ids)}
        analyses = {hid: self.analyze(r) for hid, r in rows.items()}
        public_items = [
            self._set_item_public(item, rows.get(int(item["hand_id"])),
                                  analyses.get(int(item["hand_id"])))
            for item in items
        ]
        return {
            "id": int(row["id"]),
            "name": row.get("name"),
            "description": row.get("description") or "",
            "purpose": row.get("purpose") or "regression",
            "revision": int(row.get("revision") or 1),
            "frozenAt": iso(row.get("frozen_at")),
            "createdAt": iso(row.get("created_at")),
            "updatedAt": iso(row.get("updated_at")),
            "handCount": len(public_items),
            "items": public_items,
            "coverage": self._hand_set_coverage(public_items),
        }

    def _hand_set_coverage(self, items: list[dict]) -> dict:
        roles = Counter()
        streets = Counter()
        positions = Counter()
        quality = Counter()
        results = Counter()
        prompt_total = prompt_ready = 0
        for item in items:
            roles[item.get("role") or "regression"] += 1
            quality[item.get("quality") or "missing"] += 1
            hand = item.get("hand") or {}
            positions[hand.get("heroPosition") or "?"] += 1
            results[hand.get("result") or "unknown"] += 1
            hand_id = int(item.get("handId") or 0)
            try:
                analysis = self.analyze(self.hand_repo.get_row(hand_id) or {})
            except Exception:  # noqa: BLE001
                analysis = None
            decisions = list(getattr(analysis, "decisions", []) or [])
            prompt_total += len(decisions)
            if decisions:
                advices = {
                    (str(a.get("street") or ""), int(a.get("action_index") or 0)):
                    a for a in self.live_advice_repo.list_for_hand(hand_id)
                }
                for decision in decisions:
                    advice = advices.get((decision.street,
                                          int(decision.action_index or 0)))
                    if advice and (advice.get("prompt_system")
                                   or advice.get("prompt_user")):
                        prompt_ready += 1
            for decision in decisions:
                streets[decision.street] += 1
        return {
            "roles": dict(roles),
            "streets": dict(streets),
            "positions": dict(positions),
            "quality": dict(quality),
            "results": dict(results),
            "decisions": prompt_total,
            "promptReady": prompt_ready,
            "promptCoverage": (round(prompt_ready / prompt_total, 4)
                               if prompt_total else 0),
        }

    def list_hand_sets(self, limit: int = 100) -> dict:
        rows = self.hand_set_repo.list(limit=limit)
        items = []
        for row in rows:
            item = self.get_hand_set(int(row["id"]))
            item.pop("items", None)
            items.append(item)
        return {"items": items}

    def create_hand_set(self, payload: dict) -> dict:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ApiError("INVALID_HAND_SET", "手牌集名称不能为空", 422)
        set_id = self.hand_set_repo.create(
            name=name,
            description=(str(payload.get("description") or "").strip() or None),
            purpose=str(payload.get("purpose") or "regression"))
        hand_ids = [int(x) for x in (payload.get("handIds") or [])]
        if hand_ids:
            self.add_hand_set_items(set_id, {
                "handIds": hand_ids,
                "role": payload.get("role") or "regression",
                "priority": payload.get("priority", 1),
            })
        return self.get_hand_set(set_id)

    def patch_hand_set(self, set_id: int, payload: dict) -> dict:
        row = self.hand_set_repo.update_meta(
            set_id,
            name=payload.get("name"),
            description=payload.get("description"),
            purpose=payload.get("purpose"),
            archived=payload.get("archived"),
        )
        if row is None:
            raise ApiError("HAND_SET_NOT_FOUND", f"手牌集 {set_id} 不存在", 404)
        return self.get_hand_set(set_id)

    def add_hand_set_items(self, set_id: int, payload: dict) -> dict:
        if self.hand_set_repo.get(set_id) is None:
            raise ApiError("HAND_SET_NOT_FOUND", f"手牌集 {set_id} 不存在", 404)
        hand_ids = [int(x) for x in (payload.get("handIds") or [])]
        if not hand_ids:
            raise ApiError("INVALID_HAND_SET", "至少选择一手牌", 422)
        rows = {int(r["id"]): r for r in self.hand_repo.get_by_ids(hand_ids)}
        items = []
        for hand_id in hand_ids:
            row = rows.get(hand_id)
            if row is None:
                continue
            analysis = self.analyze(row)
            items.append({
                "handId": hand_id,
                "fingerprint": analysis.fingerprint,
                "role": payload.get("role") or "regression",
                "priority": payload.get("priority", 1),
                "note": payload.get("note"),
                "expect": payload.get("expect"),
            })
        written = self.hand_set_repo.add_items(set_id, items)
        result = self.get_hand_set(set_id)
        result["written"] = written
        result["missing"] = [hid for hid in hand_ids if hid not in rows]
        return result

    def remove_hand_set_items(self, set_id: int, payload: dict) -> dict:
        if self.hand_set_repo.get(set_id) is None:
            raise ApiError("HAND_SET_NOT_FOUND", f"手牌集 {set_id} 不存在", 404)
        hand_ids = [int(x) for x in (payload.get("handIds") or [])]
        removed = self.hand_set_repo.remove_items(set_id, hand_ids)
        result = self.get_hand_set(set_id)
        result["removed"] = removed
        return result

    def freeze_hand_set(self, set_id: int) -> dict:
        row = self.hand_set_repo.freeze(set_id)
        if row is None:
            raise ApiError("HAND_SET_NOT_FOUND", f"手牌集 {set_id} 不存在", 404)
        return self.get_hand_set(set_id)

    # ─────────────────────────── 期望库（spot spec） ───────────────────────────

    def spec_meta(self) -> dict:
        return specs.meta()

    def _spec_row(self, spec_id: int) -> dict:
        row = self.spec_repo.get(spec_id)
        if row is None:
            raise ApiError("SPEC_NOT_FOUND", f"期望 {spec_id} 不存在", 404)
        return row

    @staticmethod
    def _spec_content(payload: dict) -> dict:
        """API 的 camelCase → §4.1 的 snake_case，再交给纯校验。"""
        return specs.validate_spec({
            "schema": payload.get("schema"),
            "bucket": payload.get("bucket"),
            "allowed_actions": payload.get("allowedActions"),
            "forbidden_actions": payload.get("forbiddenActions"),
            "sizing": payload.get("sizing"),
            "freq": payload.get("freq"),
            "required_points": payload.get("requiredPoints"),
            "forbidden_points": payload.get("forbiddenPoints"),
            "rationale": payload.get("rationale"),
        })

    @staticmethod
    def _public_spec_row(row: dict) -> dict:
        return specs.public_spec(
            row,
            revision_count=row.get("revision_count"),
            active_revision_id=row.get("active_revision_id"))

    def get_spec(self, spec_id: int) -> dict:
        return self._public_spec_row(self._spec_row(spec_id))

    def list_specs(self, query: dict | None = None) -> dict:
        query = query or {}
        status = str(query.get("status") or "").strip()
        if status and status not in specs.SPEC_STATUSES:
            raise ApiError("INVALID_SPEC_STATUS",
                           "status 只能是 active / draft", 422)
        keyword = str(query.get("q") or "").strip().lower()
        items = [self._public_spec_row(row)
                 for row in self.spec_repo.list_latest()]
        if status == "active":
            items = [item for item in items if item["status"] == "active"]
        elif status == "draft":
            items = [item for item in items if item["status"] == "draft"]
        if keyword:
            items = [item for item in items
                     if keyword in item["name"].lower()
                     or keyword in item["bucketLabel"].lower()]
        # 按桶分组展示：同一桶的标签排在一起，桶内按名字。
        items.sort(key=lambda item: (item["bucketLabel"], item["name"]))
        return {"items": items, "total": len(items)}

    def spec_history(self, spec_id: int) -> dict:
        row = self._spec_row(spec_id)
        rows = self.spec_repo.by_name(row["name"])
        active_id = next((int(item["id"]) for item in rows
                          if item["status"] == "active"), None)
        items = [specs.public_spec(item, revision_count=len(rows),
                                   active_revision_id=active_id)
                 for item in rows]
        return {"name": row["name"], "items": items}

    def spec_diff(self, spec_id: int,
                  against_id: int | None = None) -> dict:
        row = self._spec_row(spec_id)
        other_id = (int(against_id) if against_id is not None
                    else row.get("supersedes"))
        if other_id is None:
            return {"from": None, "to": self._public_spec_row(row),
                    "changes": []}
        other = self._spec_row(other_id)
        if other["name"] != row["name"]:
            raise ApiError("INVALID_SPEC_DIFF",
                           "只能比较同名期望的两个 revision", 422)
        return {
            "from": self._public_spec_row(other),
            "to": self._public_spec_row(row),
            "changes": specs.diff_specs(specs.spec_from_row(other),
                                        specs.spec_from_row(row)),
        }

    def create_spec(self, payload: dict) -> dict:
        name = specs.validate_name(payload.get("name"))
        status = specs.validate_status(payload.get("status") or "draft")
        note = specs.validate_note(payload.get("note"))
        content = self._spec_content(payload)
        if self.spec_repo.latest(name) is not None:
            raise ApiError(
                "SPEC_NAME_TAKEN",
                f"期望 {name!r} 已存在；编辑请从最新 revision 派生", 409)
        spec_id = self.spec_repo.create(
            name=name, revision=1, status=status, content=content,
            note=note, supersedes=None)
        if status == "active":
            self.spec_repo.activate(spec_id, name)
        return self.get_spec(spec_id)

    def create_spec_revision(self, spec_id: int, payload: dict) -> dict:
        base = self._spec_row(spec_id)
        latest = self.spec_repo.latest(base["name"])
        if latest is None or int(latest["id"]) != int(base["id"]):
            version = f"v{latest['revision']}" if latest else "?"
            raise ApiError(
                "SPEC_NOT_LATEST",
                f"只能从最新 revision（{version}）派生；旧 revision 只读", 409)
        name = payload.get("name")
        if name is not None and specs.validate_name(name) != base["name"]:
            raise ApiError("SPEC_NAME_IMMUTABLE",
                           "revision 不能改名；改名请新建期望", 422)
        status = specs.validate_status(payload.get("status") or "draft")
        note = specs.validate_note(payload.get("note"))
        content = self._spec_content(payload)
        new_id = self.spec_repo.create(
            name=base["name"], revision=int(latest["revision"]) + 1,
            status=status, content=content, note=note,
            supersedes=int(base["id"]))
        if status == "active":
            self.spec_repo.activate(new_id, base["name"])
        return self.get_spec(new_id)

    def patch_spec(self, spec_id: int, payload: dict) -> dict:
        row = self._spec_row(spec_id)
        if set(payload) - {"status"} or "status" not in payload:
            raise ApiError(
                "INVALID_SPEC",
                "PATCH 只支持修改 status；改内容请新建 revision", 422)
        status = specs.validate_status(payload["status"])
        if status == "active":
            self.spec_repo.activate(spec_id, row["name"])
        else:
            self.spec_repo.set_status(spec_id, "draft")
        return self.get_spec(spec_id)

    def delete_spec_revision(self, spec_id: int) -> dict:
        row = self._spec_row(spec_id)
        latest = self.spec_repo.latest(row["name"])
        if latest is None or int(latest["id"]) != int(row["id"]):
            raise ApiError("SPEC_NOT_LATEST",
                           "只能删除最新 revision；旧 revision 只读", 409)
        if row["status"] == "active":
            raise ApiError("SPEC_ACTIVE",
                           "生效中的期望不能删除；先停用再删", 409)
        deleted = self.spec_repo.delete(spec_id)
        return {"name": row["name"], "deletedRevisions": int(deleted or 0)}

    # ─────────────────────────── 离线 A/B 实验 ───────────────────────────

    def create_experiment(self, payload: dict) -> dict:
        """冻结手牌集 + 两个版本 + dev/holdout 切分。"""
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ApiError("INVALID_EXPERIMENT", "实验名不能为空", 422)
        baseline = int(payload.get("baselineVersionId")
                       or payload.get("baseline_version_id") or 0)
        variant = int(payload.get("variantVersionId")
                      or payload.get("variant_version_id") or 0)
        if not baseline or not variant:
            raise ApiError("INVALID_EXPERIMENT", "必须指定 baseline/variant 两个版本", 422)
        if baseline == variant:
            raise ApiError("INVALID_EXPERIMENT", "baseline 和 variant 不能是同一个版本", 422)
        self._validate_versions([baseline, variant])
        dev_ratio = float(payload.get("devRatio", payload.get("dev_ratio", 0.7)))
        dev_ratio = min(0.9, max(0.1, dev_ratio))
        repeat = max(1, min(5, int(payload.get("repeat", 3) or 3)))
        hand_set = None
        hand_set_id = payload.get("handSetId") or payload.get("hand_set_id")
        if hand_set_id:
            hand_set = self.get_hand_set(int(hand_set_id))
            hand_ids = [int(item["handId"]) for item in hand_set["items"]]
            resolved = self.resolve_scope({
                "scope": "hands",
                "handIds": hand_ids,
                "includeQuality": payload.get("includeQuality") or ["exact"],
            })
        else:
            resolved = self.resolve_scope({
                "scope": payload.get("scope") or "hands",
                "handIds": payload.get("handIds") or payload.get("hand_ids") or [],
                "filter": payload.get("filter") or {},
                # A/B 默认只用 exact 决策点；approx 的推算值会把差异混进噪声。
                "includeQuality": payload.get("includeQuality") or ["exact"],
            })
        included = resolved.pop("_included", [])
        if not included:
            raise ApiError("NO_HANDS", "这个范围里没有可重跑的手牌/决策点", 422)
        split_rows = self._split_experiment_hands(included, name, dev_ratio)
        experiment_id = self.experiment_repo.create(
            name=name,
            baseline_version_id=baseline,
            variant_version_id=variant,
            hypothesis=(payload.get("hypothesis") or "").strip() or None,
            filter_payload={
                **(payload.get("filter") or {}),
                **({"handSetId": int(hand_set_id),
                    "handSetRevision": int(hand_set["revision"])}
                   if hand_set else {}),
            },
            dev_ratio=dev_ratio,
            repeat_count=repeat,
            hand_set_id=int(hand_set_id) if hand_set else None,
            hand_set_revision=int(hand_set["revision"]) if hand_set else None,
            kind=str(payload.get("kind") or "ab"),
            change_scope=payload.get("changeScope"),
            baseline_prompt_mode=str(payload.get("baselinePromptMode") or "stored"),
        )
        self.experiment_repo.add_hands(experiment_id, split_rows)
        return self.get_experiment(experiment_id)

    @staticmethod
    def _split_experiment_hands(included, salt: str, dev_ratio: float):
        """按 hand fingerprint 做确定性分层切分；先保证两边都有人。"""
        rows = []
        for row, analysis, decisions in included:
            fingerprint = analysis.fingerprint
            digest = hashlib.sha256(
                f"{salt}:{fingerprint}".encode("utf-8")).hexdigest()
            bucket = int(digest[:8], 16) % 100
            split = "dev" if bucket < dev_ratio * 100 else "holdout"
            streets = ",".join(sorted({d.street for d in decisions}))
            rows.append((int(row["id"]), fingerprint, split, streets))
        if len(rows) >= 2:
            splits = {row[2] for row in rows}
            if splits == {"dev"}:
                rows[-1] = (rows[-1][0], rows[-1][1], "holdout", rows[-1][3])
            elif splits == {"holdout"}:
                rows[-1] = (rows[-1][0], rows[-1][1], "dev", rows[-1][3])
        return rows

    def get_experiment(self, experiment_id: int) -> dict:
        row = self.experiment_repo.get(experiment_id)
        if row is None:
            raise ApiError("EXPERIMENT_NOT_FOUND",
                           f"实验 {experiment_id} 不存在", 404)
        hands = self.experiment_repo.hands(experiment_id)
        split_counts = Counter(str(h.get("split") or "") for h in hands)
        return {
            "id": int(row["id"]),
            "name": row.get("name"),
            "hypothesis": row.get("hypothesis") or "",
            "baselineVersionId": int(row["baseline_version_id"]),
            "variantVersionId": int(row["variant_version_id"]),
            "filter": row.get("filter") or {},
            "devRatio": float(row.get("dev_ratio") or 0.7),
            "repeat": int(row.get("repeat_count") or 3),
            "status": row.get("status") or "draft",
            "jobId": (int(row["job_id"]) if row.get("job_id") else None),
            "handSetId": (int(row["hand_set_id"])
                          if row.get("hand_set_id") else None),
            "handSetRevision": (int(row["hand_set_revision"])
                                if row.get("hand_set_revision") else None),
            "kind": row.get("kind") or "ab",
            "changeScope": row.get("change_scope"),
            "baselinePromptMode": row.get("baseline_prompt_mode") or "stored",
            "decision": row.get("decision"),
            "conclusion": row.get("conclusion") or "",
            "metrics": json_load(row.get("metrics_json"), {}) or {},
            "hands": {
                "total": len(hands),
                "dev": int(split_counts.get("dev", 0)),
                "holdout": int(split_counts.get("holdout", 0)),
            },
            "createdAt": iso(row.get("created_at")),
            "startedAt": iso(row.get("started_at")),
            "finishedAt": iso(row.get("finished_at")),
        }

    def list_experiments(self, limit: int = 100) -> list[dict]:
        out = []
        for row in self.experiment_repo.list(limit=limit):
            try:
                out.append(self.get_experiment(int(row["id"])))
            except ApiError:
                continue
        return out

    def run_experiment(self, experiment_id: int, payload: dict | None = None) -> dict:
        payload = payload or {}
        experiment = self.get_experiment(experiment_id)
        hands = self.experiment_repo.hands(experiment_id)
        if not hands:
            raise ApiError("NO_HANDS", "实验没有冻结手牌", 422)
        result = self.create_run({
            "scope": "hands",
            "handIds": [int(h["hand_id"]) for h in hands],
            "versionIds": [experiment["baselineVersionId"],
                           experiment["variantVersionId"]],
            "repeat": experiment["repeat"],
            "includeQuality": ["exact"],
            "confirmLarge": bool(payload.get("confirmLarge", True)),
            "note": f"experiment:{experiment_id}",
        })
        self.experiment_repo.set_job(experiment_id, int(result["jobId"]),
                                     status="queued")
        return {**result, "experimentId": experiment_id}

    def experiment_results(self, experiment_id: int,
                           split: str | None = None) -> dict:
        split = (split or None)
        if split not in (None, "dev", "holdout"):
            raise ApiError("INVALID_SPLIT", "split 只能是 dev/holdout", 422)
        experiment = self.get_experiment(experiment_id)
        job_id = experiment.get("jobId")
        if not job_id:
            raise ApiError("EXPERIMENT_NOT_RUN", "实验还没跑", 409)
        rows = self.experiment_repo.result_rows(experiment_id, int(job_id),
                                                split=split)
        versions = {}
        for version_id in (experiment["baselineVersionId"],
                           experiment["variantVersionId"]):
            row = self.version_repo.get(int(version_id)) or {}
            versions[str(version_id)] = {
                "versionId": int(version_id),
                "name": row.get("name") or str(version_id),
                "contextMode": row.get("context_mode") or "none",
                "tableStatsMode": row.get("table_stats_mode") or "none",
                "n": 0,
                "bucket": Counter(),
                "source": Counter(),
                "violations": Counter(),
                "splits": Counter(),
            }
        for item in rows:
            version = versions.get(str(item.get("version_id")))
            if version is None:
                continue
            version["n"] += 1
            version["bucket"][str(item.get("bucket") or "unknown")] += 1
            version["source"][str(item.get("source") or "unknown")] += 1
            version["splits"][str(item.get("split") or "")] += 1
            tags = json_load(item.get("violation_tags"), []) or []
            if isinstance(tags, str):
                tags = [tags]
            for tag in tags:
                version["violations"][str(tag)] += 1
        for version in versions.values():
            # 实际动作缺录的决策点记「未知动作」：没有可比的动作，不进分母
            unknown = version["bucket"].get(UNKNOWN_ACTION, 0)
            scored = max(version["n"] - unknown, 0)
            version["unknown"] = unknown
            version["scored"] = scored
            version["agreement"] = (round(
                version["bucket"].get("一致", 0) / scored, 4)
                if scored else None)
            version["bucket"] = dict(version["bucket"])
            version["source"] = dict(version["source"])
            version["violations"] = dict(version["violations"])
            version["splits"] = dict(version["splits"])
        review: Counter = Counter()
        for item in self.experiment_repo.review_rows(experiment_id, split=split):
            review[str(item.get("verdict") or "unsure")] += 1
        return {
            "experiment": experiment,
            "jobId": int(job_id),
            "split": split,
            "rows": len(rows),
            "versions": versions,
            "review": dict(review),
        }

    @staticmethod
    def _pick_result(results: list[dict]) -> dict | None:
        """按 repeat 的多数派选一条；没有结果返回 None。"""
        if not results:
            return None
        counts = Counter((r.get("option_kind") or "?",
                          round(float(r.get("option_amount") or 0.0), 2))
                         for r in results)
        best_key, _n = counts.most_common(1)[0]
        chosen = next((r for r in results
                       if (r.get("option_kind") or "?",
                           round(float(r.get("option_amount") or 0.0), 2)) == best_key),
                      results[0])
        kinds = {r.get("option_kind") for r in results}
        return {
            "resultId": chosen.get("id"),
            "optionId": chosen.get("option_id"),
            "kind": chosen.get("option_kind"),
            "amount": chosen.get("option_amount"),
            "label": chosen.get("option_label"),
            "reason": chosen.get("reason"),
            "source": chosen.get("source"),
            "error": chosen.get("error_msg"),
            "promptSha256": chosen.get("prompt_sha256"),
            "promptSystem": chosen.get("prompt_system"),
            "promptUser": chosen.get("prompt_user"),
            "rawResponse": chosen.get("raw_response"),
            "reasonFull": chosen.get("reason_full"),
            "durationMs": chosen.get("duration_ms"),
            "distribution": dict(counts),
            "repeatCount": len(results),
            "stable": len(kinds) <= 1,
        }

    @staticmethod
    def _run_assertions(item: dict | None, chosen: dict | None) -> list[dict]:
        if not item or not chosen:
            return []
        expect = json_load(item.get("expect_json"), {}) or {}
        assertions = expect.get("assertions") if isinstance(expect, dict) else None
        if not isinstance(assertions, list):
            return []
        out = []
        kind = chosen.get("kind")
        amount = chosen.get("amount")
        reason = str(chosen.get("reason") or "")
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            atype = str(assertion.get("type") or "")
            value = assertion.get("value")
            ok = False
            if atype == "option_kind":
                ok = kind == value
            elif atype == "not_option_kind":
                ok = kind != value
            elif atype == "option_id":
                ok = chosen.get("optionId") == value
            elif atype == "amount_range":
                try:
                    ok = (amount is not None
                          and float(assertion.get("min", -1e18)) <= float(amount)
                          <= float(assertion.get("max", 1e18)))
                except (TypeError, ValueError):
                    ok = False
            elif atype == "reason_contains":
                ok = str(value or "") in reason
            elif atype == "reason_not_contains":
                ok = str(value or "") not in reason
            elif atype == "stable_across_repeats":
                ok = bool(chosen.get("stable")) is bool(value)
            else:
                ok = False
            out.append({"type": atype, "ok": ok, "value": value})
        return out

    def experiment_pairs(self, experiment_id: int, query: dict | None = None) -> dict:
        """成对结果：同一决策点的 A/B 多数派 + 人工偏好 + P0 断言。"""
        query = query or {}
        experiment = self.get_experiment(experiment_id)
        job_id = experiment.get("jobId")
        if not job_id:
            raise ApiError("EXPERIMENT_NOT_RUN", "实验还没跑", 409)
        rows = self.result_repo.for_job(int(job_id))
        split_map = {int(h["hand_id"]): h.get("split")
                     for h in self.experiment_repo.hands(experiment_id)}
        set_items = {}
        if experiment.get("handSetId"):
            set_items = {int(item["hand_id"]): item
                         for item in self.hand_set_repo.items(
                             int(experiment["handSetId"]))}
        reviews = self.pair_review_repo.for_experiment(experiment_id)
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            grouped.setdefault(int(row["decision_id"]), []).append(row)
        baseline_id = int(experiment["baselineVersionId"])
        variant_id = int(experiment["variantVersionId"])
        out_rows = []
        for decision_id, items in grouped.items():
            a_rows = [r for r in items if int(r["version_id"]) == baseline_id]
            b_rows = [r for r in items if int(r["version_id"]) == variant_id]
            if not a_rows and not b_rows:
                continue
            a = self._pick_result(a_rows)
            b = self._pick_result(b_rows)
            first = (a_rows or b_rows)[0]
            hand_id = int(first.get("hand_id") or 0)
            split = split_map.get(hand_id)
            same_kind = bool(a and b and a.get("kind") == b.get("kind"))
            same_size = bool(a and b and a.get("amount") == b.get("amount"))
            item = set_items.get(hand_id)
            a_assertions = self._run_assertions(item, a)
            assertions = self._run_assertions(item, b)
            p0_bad = [x for x in assertions if not x.get("ok")]
            p0_regression = bool(
                a_assertions and all(x.get("ok") for x in a_assertions)
                and any(not x.get("ok") for x in assertions))
            review = reviews.get(decision_id) or {}
            out_rows.append({
                "decisionId": decision_id,
                "handId": hand_id,
                "street": first.get("street"),
                "seq": first.get("seq"),
                "split": split,
                "quality": first.get("quality"),
                "actual": {
                    "action": first.get("actual_action"),
                    "amountBb": first.get("actual_amount"),
                },
                "A": a,
                "B": b,
                "auto": {
                    "sameKind": same_kind,
                    "sameSize": same_size,
                    "changed": not (same_kind and same_size),
                    "p0": bool(assertions),
                    "p0Pass": not p0_bad if assertions else None,
                    "p0Regression": p0_regression,
                    "assertions": assertions,
                    "baselineAssertions": a_assertions,
                },
                "review": ({
                    "preferred": review.get("preferred"),
                    "tags": json_load(review.get("tags"), []) or [],
                    "note": review.get("note") or "",
                } if review else None),
            })
        only = str(query.get("only") or "").strip().lower()
        if only == "changed":
            out_rows = [r for r in out_rows if r["auto"]["changed"]]
        elif only == "unreviewed":
            out_rows = [r for r in out_rows if not r["review"]]
        elif only == "p0":
            out_rows = [r for r in out_rows if r["auto"]["p0"]]
        elif only in ("a", "b"):
            out_rows = [r for r in out_rows
                        if (r["review"] or {}).get("preferred") == only.upper()]
        summary = Counter()
        for row in out_rows:
            review = row.get("review") or {}
            preferred = review.get("preferred")
            if preferred in ("A", "B", "same", "unsure"):
                summary[preferred] += 1
            if row["auto"]["changed"]:
                summary["changed"] += 1
            if row["auto"].get("p0Regression"):
                summary["p0Regressions"] += 1
        return {
            "experimentId": int(experiment_id),
            "jobId": int(job_id),
            "rows": out_rows,
            "summary": {
                "n": len(out_rows),
                "changed": int(summary.get("changed", 0)),
                "aWins": int(summary.get("A", 0)),
                "bWins": int(summary.get("B", 0)),
                "same": int(summary.get("same", 0)),
                "unsure": int(summary.get("unsure", 0)),
                "p0Regressions": int(summary.get("p0Regressions", 0)),
            },
        }

    def review_pair(self, experiment_id: int, decision_id: int,
                    payload: dict) -> dict:
        experiment = self.get_experiment(experiment_id)
        preferred = str(payload.get("preferred") or "").strip()
        if preferred not in ("A", "B", "same", "unsure"):
            raise ApiError("INVALID_PAIR_REVIEW",
                           "preferred 只能是 A/B/same/unsure", 422)
        if not self.experiment_repo.contains_decision(experiment_id, decision_id):
            raise ApiError("DECISION_NOT_IN_EXPERIMENT",
                           f"决策点 {decision_id} 不在实验 {experiment_id} 里", 404)
        return self.pair_review_repo.upsert(
            experiment_id, decision_id,
            preferred=preferred,
            tags=payload.get("tags"),
            note=payload.get("note"),
        )

    def experiment_outcome(self, experiment_id: int, payload: dict) -> dict:
        experiment = self.get_experiment(experiment_id)
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in ("adopt", "reject", "iterate", "inconclusive"):
            raise ApiError("INVALID_EXPERIMENT_DECISION",
                           "decision 只能是 adopt/reject/iterate/inconclusive", 422)
        promoted = payload.get("promotedVersionId")
        if decision == "adopt":
            promoted = int(promoted or experiment["variantVersionId"])
            self.db.execute(
                """UPDATE replay_versions SET channel='archive'
                    WHERE user_id=%s AND channel='prod' AND id<>%s""",
                (self.settings.console_user_id, promoted))
            self.db.execute(
                """UPDATE replay_versions
                      SET channel='prod', promoted_at=NOW(3)
                    WHERE user_id=%s AND id=%s""",
                (self.settings.console_user_id, promoted))
        self.experiment_repo.update_outcome(
            experiment_id,
            metrics=payload.get("metrics") or {},
            conclusion=payload.get("conclusion"),
            decision=decision,
            promoted_version_id=(int(promoted) if promoted else None),
        )
        return self.get_experiment(experiment_id)

    def _version_snapshot(self, row: dict) -> dict:
        return {
            "id": int(row["id"]), "name": row["name"],
            "provider": row["provider"], "baseUrl": row["base_url"],
            "model": row["model"], "apiKeyRef": row["api_key_ref"],
            "temperature": row["temperature"], "topP": row["top_p"],
            "maxTokens": row["max_tokens"], "thinking": row["thinking"],
            "reasoningEffort": row["reasoning_effort"],
            "promptStyle": row["prompt_style"],
            "adviceVersion": row["advice_version"],
            "reconstructVersion": row["reconstruct_version"],
            "engineCommit": row["engine_commit"],
            "engineDirty": bool(row["engine_dirty"]),
            "tableDynamicsSha": row["table_dynamics_sha"],
            "configHash": row["config_hash"],
        }

    # ─────────────────────────── 任务查询 ───────────────────────────

    def _job_public(self, job: dict, *, versions: dict[int, dict] | None = None) -> dict:
        vids = [int(v) for v in (json_load(job.get("version_ids"), []) or [])]
        versions = versions or {}
        names = [versions.get(v, {}).get("name") or f"#{v}" for v in vids]
        total = int(job.get("total_units") or 0)
        done = int(job.get("done_units") or 0)
        return {
            "id": int(job["id"]),
            "parentJobId": job.get("parent_job_id"),
            "scope": job.get("scope"),
            "status": job.get("status"),
            "versionIds": vids,
            "versions": names,
            "handCount": int(job.get("total_hands") or 0),
            "totalHands": int(job.get("total_hands") or 0),
            "totalDecisions": int(job.get("total_decisions") or 0),
            "totalUnits": total,
            "doneUnits": done,
            "failedUnits": int(job.get("failed_units") or 0),
            "skippedUnits": int(job.get("skipped_units") or 0),
            "repeat": int(job.get("repeat_count") or 1),
            "progress": round(done / total * 100, 1) if total else 0,
            "cancelRequested": bool(job.get("cancel_requested")),
            "note": job.get("note") or "",
            "errorMsg": job.get("error_msg"),
            "createdAt": iso(job.get("created_at")),
            "startedAt": iso(job.get("started_at")),
            "finishedAt": iso(job.get("finished_at")),
            "heartbeatAt": iso(job.get("heartbeat_at")),
            "workerId": job.get("worker_id"),
            "configSnapshot": json_load(job.get("config_snapshot"), {}),
        }

    def list_runs(self, query: dict) -> dict:
        page = max(1, int(query.get("page") or 1))
        page_size = min(100, max(1, int(query.get("pageSize") or 20)))
        status = query.get("status")
        if status and status not in RUN_STATUSES:
            raise ApiError("INVALID_STATUS", f"未知任务状态：{status}", 422)
        total, rows = self.job_repo.list(status=status, page=page,
                                         page_size=page_size)
        versions = {int(v["id"]): v for v in self.version_repo.list(True)}
        return {
            "total": total, "page": page, "pageSize": page_size,
            "items": [self._job_public(r, versions=versions) for r in rows],
        }

    def get_run(self, job_id: int) -> dict:
        job = self.job_repo.get(job_id)
        if job is None:
            raise ApiError("JOB_NOT_FOUND", f"任务 {job_id} 不存在", 404)
        versions = {int(v["id"]): v for v in self.version_repo.list(True)}
        public = self._job_public(job, versions=versions)
        counts = self.job_repo.counts(job_id)
        vids = public["versionIds"]
        stats_rows = self.db.query(
            """SELECT version_id, source, duration_ms
                 FROM replay_results WHERE job_id=%s""", (job_id,))
        by_version = []
        for vid in vids:
            vrows = [r for r in stats_rows if int(r["version_id"]) == vid]
            durations = [float(r["duration_ms"]) for r in vrows
                         if r.get("duration_ms")]
            by_version.append({
                "versionId": vid,
                "name": versions.get(vid, {}).get("name") or f"#{vid}",
                "model": versions.get(vid, {}).get("model"),
                "done": sum(1 for r in vrows if r.get("source") in ("model", "fallback")),
                "failed": sum(1 for r in vrows if r.get("source") == "error"),
                "fallback": sum(1 for r in vrows if r.get("source") == "fallback"),
                "p50Ms": _median_or_none(durations),
            })
        job_hands = self.job_repo.job_hands(job_id)
        hand_rows = self.hand_repo.get_by_ids([int(r["hand_id"]) for r in job_hands])
        hand_map = {int(r["id"]): r for r in hand_rows}
        total_versions = max(1, len(vids))
        repeat = public["repeat"]
        hands = []
        for jh in job_hands:
            hand_id = int(jh["hand_id"])
            row = hand_map.get(hand_id)
            expected = int(jh.get("decision_count") or 0) * total_versions * repeat
            item = {
                "handId": hand_id,
                "status": jh.get("status"),
                "decisionCount": int(jh.get("decision_count") or 0),
                "doneUnits": int(jh.get("done_units") or 0),
                "totalUnits": expected,
                "errorMsg": jh.get("error_msg"),
                "title": (row or {}).get("title") or f"手牌 #{hand_id}",
                "heroCards": (row or {}).get("hero_cards") or "",
                "board": (row or {}).get("board") or "",
                "heroPosition": (row or {}).get("hero_position"),
                "result": (row or {}).get("result"),
                "resultAmount": (row or {}).get("result_amount"),
                "fingerprint": jh.get("hand_fingerprint"),
            }
            if row:
                current = hand_fingerprint({
                    "id": row.get("id"), "heroCards": row.get("hero_cards"),
                    "board": row.get("board"), "tableSize": row.get("table_size"),
                    "heroPosition": row.get("hero_position"),
                    "heroStackBb": row.get("hero_stack_bb"),
                    "smallBlindBb": row.get("small_blind_bb"),
                    "bigBlindBb": row.get("big_blind_bb"),
                    "anteBb": row.get("ante_bb"),
                    "bombPotBb": row.get("bomb_pot_bb"),
                    "villains": json_load(row.get("villains"), []),
                    "streets": json_load(row.get("streets"), []),
                })
                item["modified"] = current != jh.get("hand_fingerprint")
            hands.append(item)
        failures = self.db.query(
            """SELECT r.id, r.version_id, r.error_msg, r.created_at,
                      d.hand_id, d.street, d.seq
                 FROM replay_results r
                 JOIN replay_decisions d ON d.id=r.decision_id
                WHERE r.job_id=%s AND r.source='error'
                ORDER BY r.id DESC LIMIT 200""", (job_id,))
        for f in failures:
            f["handId"] = int(f.pop("hand_id"))
            f["versionName"] = versions.get(int(f["version_id"]), {}).get("name")
            f["createdAt"] = iso(f.get("created_at"))
            f.pop("created_at", None)
        duration = None
        if job.get("started_at"):
            end = job.get("finished_at") or now_cn()
            duration = round((end - job["started_at"]).total_seconds(), 1)
        public.update({
            "byVersion": by_version,
            "hands": hands,
            "failures": failures,
            "canCancel": public["status"] in ("queued", "running"),
            "canRetry": public["status"] in ("partial", "failed", "canceled"),
            "durationSeconds": duration,
            "counts": counts,
        })
        return public

    def cancel_run(self, job_id: int) -> dict:
        self.job_repo.request_cancel(job_id)
        return {"canceled": True, "jobId": job_id}

    def retry_failed(self, job_id: int) -> dict:
        job = self.job_repo.get(job_id)
        if job is None:
            raise ApiError("JOB_NOT_FOUND", f"任务 {job_id} 不存在", 404)
        if job.get("status") in ("queued", "running"):
            raise ApiError("JOB_NOT_FINISHED", "任务还没结束，不能重试失败项", 409)
        vids = [int(v) for v in (json_load(job.get("version_ids"), []) or [])]
        repeat = int(job.get("repeat_count") or 1)
        vcount = max(1, len(vids))
        failed_hands = []
        for jh in self.job_repo.job_hands(job_id):
            expected = int(jh.get("decision_count") or 0) * vcount * repeat
            if jh.get("status") in ("failed", "skipped") or \
                    int(jh.get("done_units") or 0) < expected:
                failed_hands.append(int(jh["hand_id"]))
        if not failed_hands:
            raise ApiError("NO_FAILED_HANDS", "这个任务没有失败/未完成的手牌", 422)
        snap = json_load(job.get("config_snapshot"), {}) or {}
        include_quality = snap.get("includeQuality") or [EXACT, APPROX]
        payload = {
            "scope": "hands",
            "handIds": failed_hands,
            "versionIds": vids,
            "repeat": repeat,
            "dedupe": False,
            "includeQuality": include_quality,
            "note": f"重试任务 #{job_id} 的失败手牌",
            "parentJobId": job_id,
            "confirmLarge": True,
        }
        return self.create_run(payload)

    # ─────────────────────────── 结果与对比 ───────────────────────────

    def _job_results(self, job_id: int, *, hand_id: int | None = None,
                     version_ids: list[int] | None = None) -> dict:
        job = self.job_repo.get(job_id)
        if job is None:
            raise ApiError("JOB_NOT_FOUND", f"任务 {job_id} 不存在", 404)
        all_versions = {int(v["id"]): v for v in self.version_repo.list(True)}
        job_vids = [int(v) for v in (json_load(job.get("version_ids"), []) or [])]
        vids = version_ids or job_vids
        version_rows = [all_versions[v] for v in vids if v in all_versions]
        results = self.result_repo.for_job(job_id, hand_id, vids)
        grouped: dict[int, list[dict]] = {}
        decision_rows: dict[int, dict] = {}
        for row in results:
            did = int(row["decision_id"])
            grouped.setdefault(did, []).append(row)
            decision_rows.setdefault(did, row)
        decisions = []
        for did, rows in grouped.items():
            row = decision_rows[did]
            decisions.append(decision_to_public(row, version_rows, rows))
        decisions.sort(key=lambda x: (x["decision"].get("handId") or 0,
                                      x["decision"].get("seq") or 0))
        return {
            "job": self._job_public(job, versions=all_versions),
            "versions": [self._version_snapshot(v) for v in version_rows],
            "decisions": decisions,
        }

    def job_results(self, job_id: int, query: dict) -> dict:
        hand_id = int(query["handId"]) if query.get("handId") else None
        version_ids = [int(v) for v in _as_list(query.get("versionIds"))]
        data = self._job_results(job_id, hand_id=hand_id,
                                 version_ids=version_ids or None)
        decisions = data["decisions"]
        only = (query.get("only") or "").strip()
        if only:
            decisions = [d for d in decisions if self._match_filter(d, only)]
        # 按手牌分组
        hand_map = {int(r["id"]): r for r in self.hand_repo.get_by_ids(
            list({d["decision"]["handId"] for d in decisions}))}
        grouped: dict[int, list[dict]] = {}
        for d in decisions:
            grouped.setdefault(int(d["decision"]["handId"]), []).append(d)
        hands = []
        page = max(1, int(query.get("page") or 1))
        page_size = min(100, max(1, int(query.get("pageSize") or 50)))
        hand_ids = sorted(grouped)
        total_hands = len(hand_ids)
        page_ids = hand_ids[(page - 1) * page_size: page * page_size]
        for hid in page_ids:
            row = hand_map.get(hid) or {}
            analysis = self.analyze(row) if row else None
            hands.append({
                "hand": {
                    "id": hid,
                    "title": row.get("title") or f"手牌 #{hid}",
                    "heroCards": row.get("hero_cards") or "",
                    "board": row.get("board") or "",
                    "heroPosition": row.get("hero_position"),
                    "result": row.get("result"),
                    "resultAmount": row.get("result_amount"),
                    "quality": analysis.quality if analysis else None,
                },
                "decisions": grouped[hid],
            })
        return {
            "job": data["job"],
            "versions": data["versions"],
            "hands": hands,
            "totalHands": total_hands,
            "totalDecisions": len(decisions),
            "page": page,
            "pageSize": page_size,
        }

    def _match_filter(self, block: dict, only: str) -> bool:
        if only == "diverged":
            return bool(block.get("diverged"))
        if only == "violation":
            return any(v.get("summary", {}).get("violationTags")
                       for v in block.get("versions", []))
        if only == "error":
            return any(r.get("source") in ("fallback", "error")
                       for v in block.get("versions", []) for r in v.get("results", []))
        if only in ("diff", "size"):
            target = "动作不同" if only == "diff" else "尺度差"
            return any(v.get("summary", {}).get("bucket") == target
                       for v in block.get("versions", []))
        return True

    def hand_comparison(self, hand_id: int, query: dict) -> dict:
        hand_id = self.resolve_hand_id(hand_id)
        row = self.hand_repo.get_row(hand_id)
        if row is None:
            raise ApiError("HAND_NOT_FOUND", f"手牌 {hand_id} 不存在或已删除", 404)
        analysis = self.analyze(row)
        version_ids = [int(v) for v in _as_list(query.get("versionIds"))]
        job_id = int(query["jobId"]) if query.get("jobId") else None
        latest_run = self._last_run_map([hand_id]).get(hand_id)
        all_versions = {int(v["id"]): v for v in self.version_repo.list(True)}
        if job_id is not None:
            job = self.job_repo.get(job_id)
            if job is None:
                raise ApiError("JOB_NOT_FOUND", f"任务 {job_id} 不存在", 404)
            job_vids = [int(v) for v in (json_load(job.get("version_ids"), []) or [])]
            vids = version_ids or job_vids
            results = self.result_repo.for_job(job_id, hand_id, vids)
            data_versions = [all_versions[v] for v in vids if v in all_versions]
            blocks = self._blocks_from_results(results, data_versions)
        else:
            vids = version_ids or list(all_versions)
            version_rows = [all_versions[v] for v in vids if v in all_versions]
            latest_rows = self.result_repo.for_hand_latest(hand_id, vids or None)
            if latest_rows:
                if job_id is None:
                    job_id = int(latest_rows[0].get("job_id") or 0) or None
                result_vids = vids or sorted({int(r["version_id"]) for r in latest_rows})
                data_versions = [all_versions[v] for v in result_vids if v in all_versions]
                blocks = self._blocks_from_results(latest_rows, data_versions)
            else:
                data_versions = version_rows
                blocks = self._blocks_from_decisions(analysis, data_versions)
        return {
            "handId": hand_id,
            "hand": self._row_item(row, analysis, latest_run, None),
            "jobId": job_id,
            "versions": [self._version_snapshot(v) for v in data_versions],
            "decisions": blocks,
            "diverged": any(b.get("diverged") for b in blocks),
            "quality": analysis.quality,
        }

    def _blocks_from_results(self, results: list[dict],
                             version_rows: list[dict]) -> list[dict]:
        grouped: dict[int, list[dict]] = {}
        decision_rows: dict[int, dict] = {}
        for row in results:
            did = int(row["decision_id"])
            grouped.setdefault(did, []).append(row)
            decision_rows.setdefault(did, row)
        blocks = [decision_to_public(decision_rows[did], version_rows, rows)
                  for did, rows in grouped.items()]
        blocks.sort(key=lambda x: (x["decision"].get("seq") or 0))
        return blocks

    def _blocks_from_decisions(self, analysis: HandAnalysis,
                               version_rows: list[dict]) -> list[dict]:
        from poker_ocr.advice import build_situation
        try:
            dynamics = normalize_table_dynamics("current")
        except ApiError:
            dynamics = ""
        blocks = []
        for d in analysis.decisions:
            row = {
                "decision_id": None, "id": None,
                "hand_id": int(d.hand_id), "hand_fingerprint": analysis.fingerprint,
                "street": d.street, "seq": d.seq, "pot": d.pot,
                "to_call": d.snap.to_call, "hero_stack": d.snap.hero_stack,
                "hero_position": d.snap.hero_position,
                "hero_cards": "".join(d.snap.hero_cards),
                "board": "".join(d.snap.board),
                "players": d.snap.table_size,
                "opponents": d.snap.opponents_in_hand,
                "actual_action": d.actual, "actual_amount": d.actual_amount,
                "quality": d.quality, "warnings": d.warnings,
            }
            block = decision_to_public(row, version_rows, [])
            try:
                sit = build_situation(d.snap, table_dynamics=dynamics)
                block["previewFacts"] = decision_facts(sit)
                block["previewFacts"]["actualViolationTags"] = sorted(
                    audit(sit, d.actual, d.actual_amount))
            except Exception as exc:  # noqa: BLE001
                block["previewFacts"] = {"error": f"{type(exc).__name__}: {exc}"}
            blocks.append(block)
        return blocks

    # ─────────────────────────── 导出 ───────────────────────────

    def export(self, query: dict) -> tuple[str, str, str]:
        job_id = int(query.get("jobId") or 0)
        if not job_id:
            raise ApiError("EXPORT_EMPTY", "导出需要 jobId", 422)
        fmt = (query.get("format") or "csv").lower()
        if fmt not in ("csv", "markdown", "md"):
            raise ApiError("INVALID_FORMAT", "format 只能是 csv / markdown", 422)
        hand_id = int(query["handId"]) if query.get("handId") else None
        version_ids = [int(v) for v in _as_list(query.get("versionIds"))]
        data = self._job_results(job_id, hand_id=hand_id,
                                 version_ids=version_ids or None)
        only = (query.get("only") or "").strip()
        decisions = [d for d in data["decisions"]
                     if not only or self._match_filter(d, only)]
        if not decisions:
            raise ApiError("EXPORT_EMPTY", "这个范围没有可导出的结果", 422)
        job = data["job"]
        versions = data["versions"]
        rows = []
        for block in decisions:
            d = block["decision"]
            actual = block["actual"]
            for v in block["versions"]:
                s = v["summary"]
                rows.append({
                    "hand_id": d["handId"],
                    "street": d["street"],
                    "seq": d["seq"] + 1,
                    "pot": d["pot"],
                    "to_call": d["toCall"],
                    "actual": actual.get("label") or "—",
                    "version": v["name"],
                    "model": v.get("model") or "",
                    "option": s.get("label") or "—",
                    "n": f"{s.get('n', 0)}/{s.get('total', 0)}",
                    "all_same": "yes" if s.get("allSame") else "no",
                    "distribution": json.dumps(s.get("distribution") or {},
                                               ensure_ascii=False),
                    "bucket": s.get("bucket") or "",
                    "violations": ",".join(s.get("violationTags") or []),
                    "source": s.get("source") or "",
                    "diverged": "yes" if block.get("diverged") else "no",
                })
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            return ("csv", f"replay-job-{job_id}.csv", "\ufeff" + buf.getvalue())
        grouped: dict[int, list[dict]] = {}
        for block in decisions:
            grouped.setdefault(int(block["decision"]["handId"]), []).append(block)
        lines = [
            f"# 复跑任务 #{job_id}",
            "",
            f"- 范围：{job.get('scope')} · {job.get('handCount')} 手 · "
            f"{job.get('totalDecisions')} 个决策点",
            f"- 版本：{', '.join(job.get('versions') or [])}",
            f"- 重复：{job.get('repeat')} 次",
            f"- 状态：{job.get('status')}",
        ]
        if only:
            lines.append(f"- 导出筛选：{only}（{len(decisions)} 个决策点）")
        lines += [
            "",
            "| 手牌 | 街 | D# | 底池 | 需跟 | 实际 | "
            + " | ".join(v["name"] for v in versions) + " | 分歧 |",
            "| --- | --- | --- | --- | --- | --- | "
            + " | ".join("---" for _ in versions) + " | --- |",
        ]
        for hid, blocks in grouped.items():
            for block in blocks:
                d = block["decision"]
                cells = []
                for v in block["versions"]:
                    s = v["summary"]
                    label = s.get("label") or "—"
                    n = s.get("n", 0)
                    total = s.get("total", 0)
                    if total:
                        label += f" ({n}/{total})"
                    if s.get("bucket"):
                        label += f" · {s['bucket']}"
                    if s.get("violationTags"):
                        label += f" · 违规:{'/'.join(s['violationTags'])}"
                    if s.get("source") in ("fallback", "error"):
                        label += f" · {s['source']}"
                    cells.append(label)
                lines.append(
                    f"| #{hid} | {d['street']} | D{d['seq'] + 1} | "
                    f"{d['pot']} | {d['toCall']} | {block['actual'].get('label') or '—'} | "
                    + " | ".join(cells)
                    + f" | {'是' if block.get('diverged') else ''} |")
        return ("markdown", f"replay-job-{job_id}.md", "\n".join(lines) + "\n")

    # ─────────────────────────── meta / health ───────────────────────────

    def meta(self) -> dict:
        # 只读轻量列：meta 在每次页面加载时都要，不能为了 source 计数把
        # 1058 条 streets 全拖回来（远程库上那是 9 秒）。
        rows = self.hand_repo.list_light_rows()
        total = len(rows)
        favorite = sum(1 for r in rows if r.get("mark_favorite"))
        source_counts = Counter(library.canonical_source(r) for r in rows)
        versions = self.version_repo.list(False)
        last_job = self.job_repo.list(page=1, page_size=1)[1]
        defaults = {"versionIds": [], "repeat": self.settings.default_repeat}
        if last_job:
            job = last_job[0]
            defaults = {
                "versionIds": [int(v) for v in (json_load(job.get("version_ids"), []) or [])],
                "repeat": int(job.get("repeat_count") or 1),
            }
        return {
            "handCount": total,
            "favoriteCount": favorite,
            "versionCount": len(versions),
            "lastJob": (self._job_public(
                last_job[0],
                versions={int(v["id"]): v for v in self.version_repo.list(True)})
                if last_job else None),
            "defaults": defaults,
            "current": {
                "adviceVersion": CURRENT_ADVICE_VERSION,
                "reconstructVersion": CURRENT_RECONSTRUCT_VERSION,
                "promptStyles": list(PROMPT_STYLE_VALUES),
                "engine": dict(zip(("commit", "dirty"), engine_state())),
            },
            "tags": self._tag_options(rows),
            "handSources": [
                {"value": source, "label": library.SOURCE_LABELS[source],
                 "count": source_counts.get(source, 0)}
                for source in ("review", "live", "synthetic")
            ],
            "buckets": library.meta_vocab(),
            "markStatuses": list(MARK_STATUSES),
            "runStatuses": list(RUN_STATUSES),
            "limits": {
                "maxCallsPerJob": self.settings.max_calls_per_job,
                "maxQueueDepth": self.settings.max_queue_depth,
                "repeatMax": 5,
                "pageSize": self.settings.default_page_size,
            },
            "settings": self.settings.public(),
            "health": self.health(),
        }

    def health(self) -> dict:
        db_ok, db_detail = self.db.ping()
        review_ok = False
        review_detail = None
        import_ok = False
        import_count = None
        import_detail = None
        if db_ok:
            try:
                import_count = self.import_repo.count()
                total = self.hand_repo.count()
                review_ok = True
                import_ok = True
                review_detail = f"{max(total - import_count, 0)} 手复盘 + {import_count} 手导入"
                import_detail = f"{import_count} 手"
            except Exception as exc:  # noqa: BLE001
                review_detail = str(exc)
                import_detail = str(exc)
        key_ok = has_key("AI_SERVER_API_KEY")
        version_key = False
        try:
            version_key = any(r.get("api_key_ref") and has_key(r["api_key_ref"])
                              for r in self.version_repo.list(True))
        except Exception:  # noqa: BLE001
            pass
        worker = (self.runner.status() if self.runner is not None
                  else {"alive": False, "enabled": False, "currentJobId": None})
        try:
            queue = self.job_repo.queue_depth()
        except Exception:  # noqa: BLE001
            queue = None
        return {
            "ok": db_ok and review_ok and import_ok,
            "db": {"ok": db_ok, "detail": db_detail,
                   "host": self.settings.db_host, "name": self.settings.db_name},
            "reviewHands": {"ok": review_ok, "detail": review_detail},
            "imports": {"ok": import_ok, "detail": import_detail,
                        "count": import_count},
            "aiKey": {"ok": key_ok or version_key,
                      "detail": "已配置" if (key_ok or version_key) else "未配置"},
            "worker": worker,
            "queueDepth": queue,
            "schema": [{"version": r.get("version"),
                        "appliedAt": iso(r.get("applied_at"))}
                       for r in self.db.schema_versions()],
            "serverTime": iso(now_cn()),
        }
