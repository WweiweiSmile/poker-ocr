"""013 回填：把源记录物化成 canonical，并给决策点补上七个桶（票 #52）。

两步，都可重复跑：

1. **物化**（设计 §10 步骤 1）：读 `replay_imported_hands`（全量，含软删除 ——
   canonical 不可变，删除只影响列表层的来源可见性）与 `review_hands`（只读，
   且只取未删除的：用户主动删掉的手牌不该再变成案例，这也是 §11 里
   「1191 = 1139 + 52」的来源），按 §3.4 归组写 `replay_canonical_hands` +
   `replay_hand_sources`。幂等键是
   `(source, source_hand_id)`，重复跑只校正 fingerprint，不新增行。导入表的
   `source` 列若写着 `review`，在 canonical 里仍算 `live`：来源命名空间按表
   划分（§3.1），`review` 只属于 `review_hands.<id>`。
2. **决策点补料**（设计 §10 步骤 2）：对每个 canonical，取当前
   `CURRENT_RECONSTRUCT_VERSION` 的 `replay_decisions`，用票 1 的
   `poker_ocr.buckets.bucket_of` 回填 `canonical_id` 与七个桶列。缺快照、
   hand_id 映射不到的行跳过并计数，不中断；旧版本的决策点按设计不动。
   `hand_tier` 落库前翻成 §3.3 的英文稳定键（`HAND_TIERS` 的键）：`bucket_of`
   返回的是 coach 的中文档位文案，直接存会让老 spec 在改文案后集体失配。
   `opponent_profile` 用的是「回填当下」的画像库读牌 —— 快照里不带画像，
   这是设计 §3.3 指定的来源；读不到就是「未知」（fail open），不阻塞回填。

`review_hands` 是只读表：本模块的所有写口都套 `ReadOnlyGuard`，任何含写动词
且提到它的语句直接抛错（验收：review_hands 零写入）。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from poker_ocr.buckets import HAND_TIERS, bucket_of

from .canonical import (as_datetime, normalize_board, normalize_hero_cards,
                        pick_candidate)
from .decisions import snapshot_from_dict
from .repository import CanonicalHandRepository
from .util import json_load
from .versioning import CURRENT_RECONSTRUCT_VERSION

log = logging.getLogger(__name__)

READ_ONLY_TABLES = ("review_hands",)
# 任何写动词（含 WITH ... UPDATE 这种先把动词藏在后面的语句）
WRITE_VERBS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|TRUNCATE|ALTER|DROP|CREATE)\b", re.I)

# 桶列（`street` 不落列，只用来对账）
BUCKET_COLUMNS = ("position_bucket", "hand_tier", "board_texture",
                  "pot_type", "facing", "opponent_profile")

# coach 的中文档位文案 → §3.3 的英文稳定键（`HAND_TIERS` 的反查）
TIER_KEYS = {label: key for key, label in HAND_TIERS.items()}

_IMPORTED_SQL = """
    SELECT id, hero_cards, board, hero_position, title,
           source_created_at AS started_at, fingerprint, deleted_at
      FROM replay_imported_hands
     ORDER BY source_created_at, id
     LIMIT %s
"""

_REVIEW_SQL = """
    SELECT id, hero_cards, board, hero_position, title,
           created_at AS started_at, content_hash AS fingerprint, deleted_at
      FROM review_hands
     WHERE deleted_at IS NULL
     ORDER BY created_at, id
     LIMIT %s
"""

_DECISION_SQL = """
    SELECT id, hand_id, street, seq, snapshot_json
      FROM replay_decisions
     WHERE reconstruct_version = %s
     ORDER BY hand_id, street, seq
"""

_UPDATE_DECISION_SQL = """
    UPDATE replay_decisions
       SET canonical_id=%s, position_bucket=%s, hand_tier=%s,
           board_texture=%s, pot_type=%s, facing=%s, opponent_profile=%s
     WHERE id=%s
"""

_SOURCES_BY_SOURCE_SQL = """
    SELECT source, COUNT(*) AS n FROM replay_hand_sources GROUP BY source
"""
_SOURCES_COUNT_SQL = "SELECT COUNT(*) AS n FROM replay_hand_sources"
_CANONICAL_COUNT_SQL = ("SELECT COUNT(*) AS n FROM replay_canonical_hands"
                        " WHERE merged_into IS NULL")
_MULTI_SOURCE_GROUPS_SQL = """
    SELECT COUNT(*) AS n
      FROM (SELECT canonical_id FROM replay_hand_sources
             GROUP BY canonical_id HAVING COUNT(*) > 1) AS t
"""
_DUPLICATE_CONTENT_GROUPS_SQL = """
    SELECT COUNT(*) AS n
      FROM (SELECT content_group FROM replay_canonical_hands
             WHERE merged_into IS NULL AND content_group IS NOT NULL
             GROUP BY content_group HAVING COUNT(*) > 1) AS t
"""
_OTHER_VERSION_SQL = """
    SELECT COUNT(*) AS n FROM replay_decisions WHERE reconstruct_version <> %s
"""

_GROUP_SQL = """
    SELECT ch.id AS canonical_id, ch.hero_cards, ch.board, ch.started_at,
           ch.primary_source, COUNT(*) AS n_sources,
           GROUP_CONCAT(CONCAT(hs.source, ':', hs.source_hand_id)
                        ORDER BY hs.source, hs.source_hand_id) AS sources
      FROM replay_hand_sources hs
      JOIN replay_canonical_hands ch ON ch.id = hs.canonical_id
     WHERE ch.merged_into IS NULL
     GROUP BY ch.id, ch.hero_cards, ch.board, ch.started_at, ch.primary_source
    HAVING COUNT(*) > 1
     ORDER BY n_sources DESC, ch.id
     LIMIT %s
"""

# 行数上限的兜底：真库现在 1192 条，留足余量又不至于无界
MAX_ROWS = 1_000_000


@dataclass(frozen=True)
class SourceRow:
    """一条待物化的源记录（canonical 的输入，不是结果）。"""

    source: str
    hand_id: int
    hero_cards: str = ""
    board: str = ""
    hero_position: str | None = None
    title: str | None = None
    started_at: dt.datetime | None = None
    fingerprint: str | None = None
    deleted: bool = False


class ReadOnlyGuard:
    """把 Database 包一层：写语句碰到只读表就直接报错，读照常透传。

    回填的最坏情况是想改 `review_hands` —— 那是用户手工录入的复盘手牌，
    只能读。这里把它从“依赖调用方自觉”改成“写不进去”（票 #52 验收）。
    """

    def __init__(self, db: Any, *, readonly: Iterable[str] = READ_ONLY_TABLES):
        self.db = db
        self.readonly = tuple(readonly)

    def _check(self, sql: Any) -> None:
        text = " ".join(str(sql).split())
        if not WRITE_VERBS.search(text):
            return
        for table in self.readonly:
            if re.search(r"\b" + re.escape(table) + r"\b", text, re.I):
                raise RuntimeError(
                    f"013 回填不允许写只读表 {table}：{text[:120]}")

    def execute(self, sql, params=None):
        self._check(sql)
        return self.db.execute(sql, params)

    def execute_many(self, sql, seq_params):
        self._check(sql)
        return self.db.execute_many(sql, seq_params)

    def query(self, sql, params=None):
        self._check(sql)
        return self.db.query(sql, params)

    def one(self, sql, params=None):
        self._check(sql)
        return self.db.one(sql, params)

    def scalar(self, sql, params=None):
        self._check(sql)
        return self.db.scalar(sql, params)

    def __getattr__(self, name: str):
        return getattr(self.db, name)


# ──────────────────────────── 第一步：物化 ────────────────────────────

def _limit(limit: int | None) -> int:
    """`--limit` 是**两张源表合计**的上限；不给就是全量。"""
    if limit is None:
        return MAX_ROWS
    if int(limit) <= 0:
        raise ValueError("--limit 必须是正整数")
    return int(limit)


def _as_source_row(row: Mapping, *, source: str) -> SourceRow:
    """源表行 → SourceRow；来源由调用方按命名空间定（不看行里的 source 列）。"""
    return SourceRow(
        source=source,
        hand_id=int(row["id"]),
        hero_cards=str(row.get("hero_cards") or ""),
        board=str(row.get("board") or ""),
        hero_position=(row.get("hero_position") or None),
        title=(row.get("title") or None),
        started_at=as_datetime(row.get("started_at")),
        fingerprint=(row.get("fingerprint") or None),
        deleted=bool(row.get("deleted_at")),
    )


def load_source_rows(db: Any, *, limit: int | None = None) -> list[SourceRow]:
    """读两边的源记录，按时间排序后返回（只读，不写真表）。

    imported 全量（含软删除）；review 只读。同一手牌在两边的 id 段不同，
    不会互相覆盖。`limit` 是合计上限，先装 imported 再装 review。
    """
    cap = _limit(limit)
    rows = [_as_source_row(r, source="live")
            for r in db.query(_IMPORTED_SQL, (cap,))][:cap]
    remaining = cap - len(rows)
    if remaining > 0:
        rows += [_as_source_row(r, source="review")
                 for r in db.query(_REVIEW_SQL, (remaining,))][:remaining]
    rows.sort(key=lambda r: (r.started_at is None,
                             r.started_at or dt.datetime.min, r.hand_id))
    return rows


def materialize_sources(db: Any, rows: Iterable[SourceRow]) -> dict:
    """把源记录接到 canonical 上；返回对账用的统计与 hand_id → canonical 映射。

    逐条 upsert（同内容合并、review 优先当 primary 由仓储负责）。单条失败只
    记账继续，不让一条坏数据挡住其余 1191 条。
    """
    repo = CanonicalHandRepository(db)
    stats: dict[str, Any] = {
        "rows": 0, "created": 0, "linked": 0, "deleted": 0,
        "missing_started_at": 0, "missing_hero_cards": 0,
        "sources": {}, "mapping": {}, "failed": [],
    }
    for row in rows:
        stats["rows"] += 1
        stats["sources"][row.source] = stats["sources"].get(row.source, 0) + 1
        if row.deleted:
            stats["deleted"] += 1
        if row.started_at is None:
            stats["missing_started_at"] += 1
            log.warning("源记录缺发生时间，content_group 留空 %s:%s",
                        row.source, row.hand_id)
        if not normalize_hero_cards(row.hero_cards):
            stats["missing_hero_cards"] += 1
            log.warning("源记录缺底牌 %s:%s", row.source, row.hand_id)
        try:
            result = repo.upsert_source(
                source=row.source, source_hand_id=row.hand_id,
                hero_cards=row.hero_cards, board=row.board,
                fingerprint=row.fingerprint, started_at=row.started_at,
                title=row.title, hero_position=row.hero_position)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断回填
            log.warning("物化失败 %s:%s：%s", row.source, row.hand_id, exc)
            stats["failed"].append({"source": row.source,
                                    "hand_id": row.hand_id,
                                    "error": f"{type(exc).__name__}: {exc}"})
            continue
        if result["created"]:
            stats["created"] += 1
        else:
            stats["linked"] += 1
        stats["mapping"].setdefault(row.source, {})[row.hand_id] = int(
            result["canonical_id"])
    return stats


# ──────────────────────── 第二步：决策点补料 ────────────────────────

def _hand_to_canonical(mapping: Mapping | None) -> dict[int, int]:
    """{"live": {hand_id: canonical_id}, ...} → 平铺的 hand_id → canonical_id。

    两边 id 段不重叠（导入 1e12、review 99 万），撞了也是后写的赢，不猜。
    """
    flat: dict[int, int] = {}
    for source_map in (mapping or {}).values():
        for hand_id, canonical_id in (source_map or {}).items():
            flat[int(hand_id)] = int(canonical_id)
    return flat


def _reads_for(store: Any, snap: Any) -> dict | None:
    """读这手牌对手的画像（按名字匹配，座位号会跨手重复，不能拿来缓存）。"""
    if store is None:
        return None
    try:
        return store.read_for(snap.opponents) or {}
    except Exception as exc:  # noqa: BLE001 —— 画像读失败退化成未知
        log.warning("读画像失败 hand=%s：%s", getattr(snap, "hand_id", "?"), exc)
        return {}


def _tier_key(value: Any) -> Any:
    """coach 的中文档位 → §3.3 英文稳定键；认不出的值原样保留（不吞数据）。"""
    return TIER_KEYS.get(value, value)


def fill_decisions(db: Any, mapping: Mapping | None, *,
                   profile_store: Any = None) -> dict:
    """给当前版本的决策点回填 canonical_id 与七个桶列（幂等、可重跑）。"""
    hands = _hand_to_canonical(mapping)
    stats: dict[str, Any] = {
        "rows": 0, "updated": 0, "skipped_no_snapshot": 0,
        "unmapped_hand": 0, "street_mismatch": 0, "other_versions": 0,
        "failed": [], "filled": {col: 0 for col in BUCKET_COLUMNS},
        # 留几个 id 方便定位坏数据（计数仍以 *_no_snapshot / street_mismatch 为准）
        "skipped_ids": [], "mismatch_ids": [],
    }
    for row in db.query(_DECISION_SQL, (CURRENT_RECONSTRUCT_VERSION,)):
        stats["rows"] += 1
        hand_id = int(row["hand_id"])
        canonical_id = hands.get(hand_id)
        if canonical_id is None:
            stats["unmapped_hand"] += 1
            continue
        data = json_load(row.get("snapshot_json"), None)
        if not isinstance(data, dict) or not data.get("street"):
            stats["skipped_no_snapshot"] += 1
            stats["skipped_ids"].append(int(row["id"]))
            log.info("缺快照跳过 decision=%s hand=%s", row.get("id"), hand_id)
            continue
        try:
            snap = snapshot_from_dict(data)
            bucket = bucket_of(snap, opponent_reads=_reads_for(profile_store, snap))
        except Exception as exc:  # noqa: BLE001 —— 单条坏快照不中断
            log.warning("桶推导失败 decision=%s：%s", row.get("id"), exc)
            stats["failed"].append({"decision_id": int(row["id"]),
                                    "error": f"{type(exc).__name__}: {exc}"})
            continue
        if bucket.get("street") and bucket["street"] != row.get("street"):
            stats["street_mismatch"] += 1
            stats["mismatch_ids"].append(int(row["id"]))
            log.info("街不一致 decision=%s（列 %s / 快照 %s）", row.get("id"),
                     row.get("street"), bucket["street"])
        db.execute(_UPDATE_DECISION_SQL, (
            canonical_id,
            bucket.get("position_bucket"), _tier_key(bucket.get("hand_tier")),
            bucket.get("board_texture"), bucket.get("pot_type"),
            bucket.get("facing"), bucket.get("opponent_profile"),
            int(row["id"])))
        stats["updated"] += 1
        for col in BUCKET_COLUMNS:
            if bucket.get(col) is not None:
                stats["filled"][col] += 1
    stats["other_versions"] = int(db.query(
        _OTHER_VERSION_SQL, (CURRENT_RECONSTRUCT_VERSION,))[0]["n"])
    return stats


# ──────────────────────────── 对账 ────────────────────────────

_STALE_ENRICHMENT_SQL = """
    SELECT h.id
      FROM replay_imported_hands h
     WHERE h.deleted_at IS NULL
       AND EXISTS (SELECT 1 FROM replay_decisions d WHERE d.hand_id = h.id)
       AND (h.flags_json LIKE %s OR h.timeline_json LIKE %s)
     ORDER BY h.id
"""
# flags 还停在「没有英雄决策点」，或时间线里的 decision 帧还没挂上 decisionId。
_STALE_FLAGS_PATTERN = '%"INVALID"%'
_STALE_TIMELINE_PATTERN = '%"decisionId":null%'


def refresh_stale_enrichment(db: Any, service: Any, *,
                             dry_run: bool = False) -> dict:
    """重算 enrichment 已经过期的手牌（回填决策点之后）。

    两种过期签名，任一命中就重算：

    - ``flags`` 还写着 ``INVALID``（「没有 hero 决策点」），其实决策点已经有了；
    - 时间线里的 decision 帧 ``decisionId`` 还是 null（帧是导入时物化的，那时
      决策点还没落库），详情页因此跳不到决策行。

    只挑签名命中的手牌，不改其它手；重算走 ``refresh_enrichment`` 一条路。

    与回填主循环同一条铁律：一手坏数据不能中断整批 —— 重算要走 analyze / 写
    timeline，任何一手抛异常只记一行并继续。
    """
    rows = db.query(_STALE_ENRICHMENT_SQL,
                    (_STALE_FLAGS_PATTERN, _STALE_TIMELINE_PATTERN))
    refreshed = 0
    pending: list[int] = []
    failed: list[dict] = []
    for row in rows:
        hand_id = int(row["id"])
        if dry_run:
            pending.append(hand_id)
            continue
        try:
            ok = service.refresh_enrichment(hand_id)
        except Exception as exc:  # noqa: BLE001 —— 一手坏数据不能中断整批
            log.exception("重算手牌 %s 的时间线/flags 失败", hand_id)
            failed.append({"handId": hand_id,
                           "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not ok:
            failed.append({"handId": hand_id,
                           "reason": "ENRICHMENT_FAILED 时间线写库失败（见日志）"})
            continue
        refreshed += 1
    return {"refreshed": refreshed, "pending": pending, "failed": failed}


def collect_stats(db: Any) -> dict:
    by_source = {str(r["source"]): int(r["n"])
                 for r in db.query(_SOURCES_BY_SOURCE_SQL)}
    return {
        "sources": by_source,
        "sources_total": int(db.query(_SOURCES_COUNT_SQL)[0]["n"]),
        "canonical_total": int(db.query(_CANONICAL_COUNT_SQL)[0]["n"]),
        "multi_source_groups": int(db.query(_MULTI_SOURCE_GROUPS_SQL)[0]["n"]),
        "duplicate_content_groups": int(
            db.query(_DUPLICATE_CONTENT_GROUPS_SQL)[0]["n"]),
    }


def group_lines(db: Any, *, limit: int = 20) -> list[str]:
    """列出「一行 canonical 接了多条源记录」的组，供人工抽查（§11 第 2 条）。

    同内容 ≥3 条源按 §3.4 规则 4 要先进入待确认（第 2 期的合并 UI），这里打标
    提醒，但不替人做决定。
    """
    rows = db.query(_GROUP_SQL, (max(1, int(limit)),))
    if not rows:
        return ["多源组明细：没有需要合并的组"]
    lines = [f"多源组明细（前 {len(rows)} 组，按来源数排序）"]
    for row in rows:
        count = int(row.get("n_sources") or 0)
        started = row.get("started_at")
        stamp = started.strftime("%Y-%m-%d %H:%M") if started else "?（缺时间）"
        mark = "  ⚠ ≥3 源，待确认" if count >= 3 else ""
        lines.append(
            f"  {row.get('canonical_id')} [{count} 源] {row.get('hero_cards') or '?'}"
            f" / {row.get('board') or '空板'} / {stamp}｜{row.get('sources') or ''}"
            f"{mark}")
    return lines


def _count_sources(rows: Iterable[SourceRow]) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {}
    deleted = 0
    for row in rows:
        counts[row.source] = counts.get(row.source, 0) + 1
        if row.deleted:
            deleted += 1
    return counts, deleted


def _predict_canonical(rows: Iterable[SourceRow]) -> tuple[int, int]:
    """按 §3.4 规则 1 模拟归组（同牌面且时间差 ≤30 分钟接同一行）。

    dry-run 不能只按 `content_group`（日期粒度）预估：同一天、同一牌面但相差
    >30 分钟的两手牌，实跑会各建一行。这里照仓储的匹配口径逐条模拟。
    """
    canonicals: list[dict] = []
    attached: dict[int, int] = {}
    for row in rows:
        hero = normalize_hero_cards(row.hero_cards)
        board = normalize_board(row.board)
        candidates = [c for c in canonicals
                      if c["hero_cards"] == hero and c["board"] == board]
        hit = pick_candidate(candidates, row.started_at)
        if hit is None:
            cid = len(canonicals) + 1
            canonicals.append({"id": cid, "hero_cards": hero, "board": board,
                               "started_at": row.started_at})
            attached[cid] = 1
        else:
            cid = int(hit["id"])
            attached[cid] = attached.get(cid, 0) + 1
    return len(canonicals), sum(1 for n in attached.values() if n > 1)


def run_backfill(db: Any, *, profile_store: Any = None, dry_run: bool = False,
                 limit: int | None = None, skip_decisions: bool = False) -> dict:
    """跑完整回填并返回对账数字（report_lines 负责格式化成输出）。"""
    guarded = ReadOnlyGuard(db)
    rows = load_source_rows(guarded, limit=limit)
    stats: dict[str, Any] = {
        "dry_run": bool(dry_run), "limit": limit,
        "skip_decisions": bool(skip_decisions),
        "rows": len(rows),
    }
    if dry_run:
        sources, deleted = _count_sources(rows)
        predicted, groups = _predict_canonical(rows)
        stats.update(sources=sources, deleted=deleted,
                     predicted_canonical=predicted,
                     predicted_groups_over_one=groups,
                     sources_total=len(rows), mapped_total=0)
        return stats

    materialized = materialize_sources(guarded, rows)
    stats.update({key: materialized[key] for key in
                  ("sources", "deleted", "created", "linked", "failed",
                   "missing_started_at", "missing_hero_cards", "mapping")})
    mapping = materialized["mapping"]
    stats["mapped_total"] = sum(len(ids) for ids in mapping.values())
    stats["decisions"] = ({} if skip_decisions
                          else fill_decisions(guarded, mapping,
                                              profile_store=profile_store))
    stats.update(collect_stats(guarded))
    return stats


def report_lines(stats: Mapping) -> list[str]:
    """把对账数字排成人能读的几行（票里贴的就是这些行）。"""
    lines: list[str] = ["013 回填对账"]
    sources = stats.get("sources") or {}
    detail = " / ".join(f"{k} {v}" for k, v in sorted(sources.items()))
    processed = int(stats.get("rows") or 0)
    lines.append(f"源记录：{detail or '无'}，共 {processed}"
                 + (f"（软删除 {stats['deleted']} 条也物化）"
                    if stats.get("deleted") else ""))
    if stats.get("dry_run"):
        lines.append(f"dry-run：预计 {stats.get('predicted_canonical', 0)} 条 "
                     f"canonical、{stats.get('predicted_groups_over_one', 0)} 组多源；未写库")
        return lines

    mapped = int(stats.get("mapped_total") or 0)
    coverage = (100.0 * mapped / processed) if processed else 100.0
    tail = ""
    if stats.get("sources_total") and stats["sources_total"] != processed:
        tail = f"（库内共 {stats['sources_total']} 条，本次只处理前 {processed} 条）"
    lines.append(f"canonical：{stats.get('canonical_total', 0)} 行；"
                 f"映射 {mapped}/{processed} = {coverage:.1f}%{tail}")
    lines.append(f"多源组：{stats.get('multi_source_groups', 0)} 组"
                 f"（同 content_group 重复："
                 f"{stats.get('duplicate_content_groups', 0)} 组；"
                 f"--list-groups 可看明细）")
    lines.append(f"新建 {stats.get('created', 0)} / 接到已有 "
                 f"{stats.get('linked', 0)}")
    missing = []
    if stats.get("missing_started_at"):
        missing.append(f"缺 started_at {stats['missing_started_at']} 条"
                       "（content_group 留空）")
    if stats.get("missing_hero_cards"):
        missing.append(f"缺底牌 {stats['missing_hero_cards']} 条")
    if missing:
        lines.append("字段缺失：" + "，".join(missing))

    decisions = stats.get("decisions") or {}
    if decisions:
        filled = decisions.get("filled") or {}
        lines.append(
            "决策点：当前版本 {rows} 行，回填 {updated}"
            "（缺快照 {skipped_no_snapshot}，hand 未映射 {unmapped_hand}"
            "，街不一致 {street_mismatch}，失败 {failed}）；"
            "旧版本未动 {other_versions}".format(
                rows=decisions.get("rows", 0),
                updated=decisions.get("updated", 0),
                skipped_no_snapshot=decisions.get("skipped_no_snapshot", 0),
                unmapped_hand=decisions.get("unmapped_hand", 0),
                street_mismatch=decisions.get("street_mismatch", 0),
                failed=len(decisions.get("failed") or ()),
                other_versions=decisions.get("other_versions", 0)))
        lines.append("桶填充：" + "，".join(
            f"{col} {filled.get(col, 0)}" for col in BUCKET_COLUMNS))
        if decisions.get("skipped_ids"):
            lines.append("缺快照 id（前 5）：" + "，".join(
                str(i) for i in decisions["skipped_ids"][:5]))
        if decisions.get("mismatch_ids"):
            lines.append("街不一致 id（前 5）：" + "，".join(
                str(i) for i in decisions["mismatch_ids"][:5]))
    elif stats.get("skip_decisions"):
        lines.append("决策点：--skip-decisions，未回填")

    failed = stats.get("failed") or []
    if failed:
        lines.append(f"失败：{len(failed)} 条（前 5 条）")
        for item in failed[:5]:
            lines.append(f"  - {item.get('source')}:{item.get('hand_id')} "
                         f"{item.get('error', '')}"[:200])
    return lines
