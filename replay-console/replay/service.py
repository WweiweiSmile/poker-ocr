"""查询 / 版本 / 任务 / 导出服务。

API 层只做参数校验与 JSON 序列化；所有判断都在这里，便于单测和 CLI 复用。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import re
import threading
from collections import Counter
from typing import Any, Iterable

from poker_ocr.advice import build_situation
from poker_ocr.advice import AISettings

from .config import Settings
from .db import Database
from .decisions import (
    APPROX, EXACT, INVALID, HandAnalysis, analyze_hand, call_amount, cards_of,
    decision_facts, hand_fingerprint, snapshot_to_dict,
)
from .engine import (
    audit, classify, decision_to_public, fmt_option, summarize_version,
)
from .errors import ApiError
from .importer import (
    SOURCE_LABELS, SOURCE_LIVE, SOURCE_REVIEW, parse_hand_text,
)
from .repository import (
    HAND_SOURCE_DB, HAND_SOURCE_LABELS, HAND_SOURCE_LIVE, HAND_SOURCE_REVIEW,
    DecisionRepository, HandRepository, ImportBatchRepository, JobRepository,
    MarkRepository, ResultRepository, VersionRepository,
)
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
        self.job_repo = JobRepository(db, settings.console_user_id)
        self.result_repo = ResultRepository(db)

    # ─────────────────────────── 基础查询 ───────────────────────────

    def analyze(self, row: dict) -> HandAnalysis:
        key = (int(row.get("id") or 0), iso(row.get("updated_at")),
               row.get("hero_cards"), row.get("board"),
               bool(row.get("streets")), CURRENT_RECONSTRUCT_VERSION)
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
            if key == "id":
                return hand_id
            return row.get("created_at") or dt.datetime.min

        return sorted(rows, key=val, reverse=reverse)

    # ─────────────────────────── 手牌库 ───────────────────────────

    def list_hands(self, query: dict) -> dict:
        rows = self.hand_repo.list_rows()
        analyses = {int(r["id"]): self.analyze(r) for r in rows}
        last_runs = self._last_run_map([int(r["id"]) for r in rows])
        filtered = self._filter_rows(rows, analyses, last_runs, query)
        filtered = self._sort_rows(filtered, analyses, last_runs,
                                   query.get("sort") or "created_at",
                                   query.get("order") or "desc")
        page = max(1, int(query.get("page") or 1))
        page_size = min(200, max(1, int(query.get("pageSize")
                                       or self.settings.default_page_size)))
        total = len(filtered)
        page_rows = filtered[(page - 1) * page_size: page * page_size]
        duplicate_groups = self._duplicate_groups(rows)
        items = []
        for row in page_rows:
            hand_id = int(row["id"])
            items.append(self._row_item(
                row, analyses[hand_id], last_runs.get(hand_id),
                duplicate_groups.get(hand_id)))
        return {
            "total": total,
            "page": page,
            "pageSize": page_size,
            "items": items,
            "filters": query,
            "tagOptions": self._tag_options(rows),
        }

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
        """把导入日志里的 advice 记录转成前端 camelCase 形状。"""
        option = advice.get("option") if isinstance(advice.get("option"), dict) else {}
        return {
            "ts": advice.get("ts"),
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
            "plan": advice.get("plan"),
            "confidence": advice.get("confidence"),
            "source": advice.get("source"),
            "model": advice.get("model"),
            "durationMs": advice.get("duration_ms"),
        }

    def get_hand(self, hand_id: int) -> dict:
        row = self.hand_repo.get_row(hand_id)
        if row is None:
            raise ApiError("HAND_NOT_FOUND", f"手牌 {hand_id} 不存在或已删除", 404)
        analysis = self.analyze(row)
        # 导入实时日志时，advice 行会并进手牌的 payload_json；详情页要能
        # 看到「当时 AI 建议了什么」。大字段只在详情查询时取，不进列表。
        advices: list[dict] = []
        if row.get("source") in (HAND_SOURCE_LIVE, HAND_SOURCE_REVIEW):
            payload = self.import_repo.get_payload(hand_id)
            value = payload.get("advices") if isinstance(payload, dict) else None
            if isinstance(value, list):
                advices = [self._advice_public(a) for a in value
                           if isinstance(a, dict)]
        all_rows = self.hand_repo.list_rows()
        analyses = {int(r["id"]): self.analyze(r) for r in all_rows}
        dup_groups = self._duplicate_groups(all_rows)
        last_runs = self._last_run_map([hand_id])
        item = self._row_item(row, analysis, last_runs.get(hand_id),
                              dup_groups.get(hand_id))
        persisted = self.decision_repo.for_hand(hand_id, analysis.fingerprint)
        raw_streets = json_load(row.get("streets"), []) or []
        streets = self._streets_public(raw_streets, analysis)
        decisions = []
        for d in analysis.decisions:
            p = persisted.get((d.street, d.seq))
            decisions.append(self._decision_public(d, p))
        dup_items = []
        for other in (dup_groups.get(hand_id) or []):
            if int(other) == hand_id:
                continue
            other_row = next((r for r in all_rows if int(r["id"]) == int(other)), None)
            if other_row:
                dup_items.append({
                    "id": int(other),
                    "title": other_row.get("title") or self._default_title(other_row),
                    "createdAt": iso(other_row.get("created_at")),
                })
        return {
            "hand": item,
            "streets": streets,
            "decisions": decisions,
            "advices": advices,
            "duplicates": dup_items,
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

    def _decision_public(self, d, persisted: dict | None) -> dict:
        return {
            "id": persisted.get("id") if persisted else None,
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
            "snapshot": {
                "preflopShapes": list(d.snap.preflop_shapes),
                "heroIsPfr": d.snap.hero_is_pfr,
                "bombPot": d.snap.bomb_pot,
                "potBeforeStreet": d.snap.pot_before_street,
            },
        }

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
        config = config_payload(
            provider=provider, base_url=base_url, model=model,
            temperature=temperature, top_p=top_p, max_tokens=max_tokens,
            thinking=thinking, reasoning_effort=effort,
            prompt_style=prompt_style, api_key_ref=api_key_ref,
            dynamics_sha=sha,
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
        rows = self.hand_repo.list_rows()
        total = len(rows)
        favorite = sum(1 for r in rows if r.get("mark_favorite"))
        source_counts = Counter((r.get("source") or HAND_SOURCE_DB) for r in rows)
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
                {"value": source,
                 "label": HAND_SOURCE_LABELS.get(source, source),
                 "count": source_counts.get(source, 0)}
                for source in (HAND_SOURCE_DB, HAND_SOURCE_REVIEW,
                               HAND_SOURCE_LIVE)
            ],
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
