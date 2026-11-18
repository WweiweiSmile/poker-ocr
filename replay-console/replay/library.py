"""手牌库的归组与筛选（纯逻辑，不碰数据库）。

第 1 期的手牌库以 canonical 为单位展示：一手牌可能在复盘、实时、构造三边
各有源记录，列表只出现一次，并标出「同组 N 条」；详情并列所有来源。
本模块只负责把「canonical 行 + 来源边」拼成可展示的分组，以及把 URL 上的
筛选条件应用到分组上。SQL 在 `repository`，展示字段在 `service`。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from poker_ocr.buckets import BUCKET_ORDER, BUCKET_VOCAB, label

from .canonical import SOURCE_PRIORITY

# 来源在页面上的中文名。review 是人工复盘记录，live 是实时链路沉淀，
# synthetic 是构造样本（不进列表，除非有人显式给它建了源记录）。
SOURCE_LABELS: dict[str, str] = {
    "review": "复盘库",
    "live": "实时",
    "synthetic": "构造",
}

# 旧前端把 review 叫 "db"，把两种导入都叫 "live"。保留映射，URL 里的
# 老筛选值（source=db）仍然可用。
LEGACY_SOURCES: dict[str, str] = {"db": "review", "review": "review",
                                  "live": "live", "synthetic": "synthetic"}

# 列表/详情上展示的桶维度顺序：街道第一，其余按 `poker_ocr.buckets` 的定义。
DIMENSIONS: tuple[str, ...] = BUCKET_ORDER


def canonical_source(row: dict) -> str:
    """把一条手牌行的 legacy source（db/live）还原成 canonical 来源名。"""
    value = str(row.get("source") or "db").lower()
    return "review" if value == "db" else value


def source_key(source: str, hand_id: int) -> tuple[str, int]:
    return validate_source_name(source), int(hand_id)


def validate_source_name(source: str) -> str:
    value = str(source or "").lower()
    if value in SOURCE_LABELS:
        return value
    # 非法来源直接退回复盘：读路径 fail open，不让一行脏数据让整页 500。
    return "review"


def source_sort_key(source: dict) -> tuple:
    return (
        0 if source.get("is_primary") else 1,
        SOURCE_PRIORITY.get(str(source.get("source")), 9),
        int(source.get("source_hand_id") or 0),
    )


def build_groups(canonical_rows: Iterable[dict],
                 source_rows: Iterable[dict],
                 visible_keys: set[tuple[str, int]]) -> list[dict]:
    """canonical 行 + 全部来源边 → 可见分组。

    `visible_keys` 是当前可见的手牌行（(canonical 来源, 手牌 id)）。一条源记录
    可能在复盘/导入侧被删除或隐藏，这种源既不显示、也不计入「同组 N 条」——
    列表上看到的 N 必须点得开。整组都不可见时，这一组不出现在列表里。
    """
    by_canonical: dict[int, list[dict]] = {}
    for source in source_rows:
        by_canonical.setdefault(int(source["canonical_id"]), []).append(source)

    groups: list[dict] = []
    for row in canonical_rows:
        canonical_id = int(row["id"])
        sources = sorted(by_canonical.get(canonical_id, []), key=source_sort_key)
        if not sources:
            continue
        visible = [s for s in sources
                   if source_key(s["source"], s["source_hand_id"])
                   in visible_keys]
        if not visible:
            continue
        # 优先用 primary 当代表；primary 被删了就用可见来源里优先级最高的那条。
        representative = next((s for s in visible if s.get("is_primary")),
                              visible[0])
        groups.append({
            "canonicalId": canonical_id,
            "canonical": row,
            "sources": sources,
            "visibleSources": visible,
            "representative": representative,
        })
    return groups


def group_source_item(source: dict, hand_id: int | None) -> dict:
    name = str(source.get("source") or "")
    return {
        "source": name,
        "sourceHandId": int(source.get("source_hand_id") or 0),
        "handId": hand_id,
        "label": SOURCE_LABELS.get(name, name),
        "primary": bool(source.get("is_primary")),
        "visible": hand_id is not None,
    }


def decision_quality(count: int, approx: int) -> str:
    if count <= 0:
        return "invalid"
    return "approx" if approx > 0 else "exact"


def decision_key(street: str, seq: int) -> str:
    """决策点在案例里的稳定键：`街道:seq`（与 `replay_cases.decision_key` 同口径）。"""
    return f"{street}:{int(seq or 0)}"


def decision_stats(rows) -> tuple[dict, dict[str, set[str]]]:
    """把一手牌（或一组 canonical 决策点）折叠成计数/质量 + 七桶取值。"""
    rows = list(rows)
    approx = sum(1 for row in rows
                 if str(row.get("quality") or "") == "approx")
    buckets: dict[str, set[str]] = {}
    for row in rows:
        for dimension in DIMENSIONS:
            value = row.get(dimension)
            if value is not None and str(value) != "":
                buckets.setdefault(dimension, set()).add(str(value))
    return ({"count": len(rows), "approx": approx,
             "quality": decision_quality(len(rows), approx)},
            buckets)


def bucket_values(buckets: dict[str, set[str]]) -> list[dict]:
    """把「维度 → 取值集合」变成前端可直接渲染的 chips。"""
    out = []
    for dimension in DIMENSIONS:
        values = sorted(buckets.get(dimension) or ())
        if not values:
            continue
        out.append({
            "dimension": dimension,
            "label": BUCKET_VOCAB.get(dimension, ("", {}))[0],
            "values": [{"value": v, "label": bucket_label_of(dimension, v)}
                       for v in values],
        })
    return out


def meta_vocab() -> list[dict]:
    """`/api/meta` 暴露给前端的桶词表（唯一来源是 `poker_ocr.buckets`）。"""
    return [{
        "key": dimension,
        "label": BUCKET_VOCAB[dimension][0],
        "options": [{"value": str(value), "label": str(text)}
                    for value, text in BUCKET_VOCAB[dimension][1].items()],
    } for dimension in DIMENSIONS]


def bucket_label_of(dimension: str, value: Any) -> str:
    """单值展示；`facing` 在 DB 里是 0/1，翻中文前转回 int。"""
    key = value
    if dimension == "facing":
        try:
            key = int(value)
        except (TypeError, ValueError):
            key = value
    try:
        return label(dimension, key) or str(value)
    except Exception:  # noqa: BLE001 - 词表外的脏值原样显示
        return str(value)


def matches_filters(group: dict, filters: dict) -> bool:
    """分组级筛选。值解析在 service，这里只做纯比较。"""
    if filters.get("favorite") is not None:
        if bool(group.get("favorite")) != filters["favorite"]:
            return False
    if filters.get("sources"):
        wanted = filters["sources"]
        have = {str(s.get("source"))
                for s in (group.get("sources")
                          or group.get("visibleSources") or [])}
        if not have & wanted:
            return False
    if filters.get("positions"):
        if str(group.get("heroPosition") or "").upper() not in filters["positions"]:
            return False
    if filters.get("results"):
        if str(group.get("result") or "unknown").lower() not in filters["results"]:
            return False
    if filters.get("tags"):
        tags = {str(t).lower() for t in group.get("markTags") or []}
        if not any(t in tags for t in filters["tags"]):
            return False
    if filters.get("qualities"):
        if str(group.get("quality")) not in filters["qualities"]:
            return False
    if filters.get("hasRun") is not None:
        if bool(group.get("hasRun")) != filters["hasRun"]:
            return False
    if filters.get("hasCase") is not None:
        if bool(group.get("caseCount")) != filters["hasCase"]:
            return False
    if filters.get("flagMask"):
        if (int(group.get("flagMask") or 0) & filters["flagMask"]) != filters["flagMask"]:
            return False
    if filters.get("minScore") is not None:
        if int(group.get("reviewScore") or 0) < filters["minScore"]:
            return False
    if filters.get("keyword"):
        hay = " ".join(str(x or "") for x in (
            group.get("title"), group.get("heroCards"), group.get("board"),
            group.get("heroPosition"), group.get("externalId"),
        )).lower()
        if filters["keyword"] not in hay:
            return False
    created = parse_datetime(group.get("createdAt"))
    date_from = filters.get("dateFrom")
    date_to = filters.get("dateTo")
    if date_from and created and created < date_from:
        return False
    if date_to and created and created > date_to + dt.timedelta(days=1):
        return False
    not_run_days = filters.get("notRunDays")
    if not_run_days:
        at = parse_datetime(group.get("lastRunAt"))
        if at and (dt.datetime.now() - at).days < int(not_run_days):
            return False
    bucket_filters = filters.get("buckets") or {}
    if bucket_filters:
        buckets = group.get("buckets") or {}
        for dimension, wanted in bucket_filters.items():
            if not (set(buckets.get(dimension) or ()) & wanted):
                return False
    return True


def sort_value(group: dict, key: str) -> Any:
    if key == "updated_at":
        return parse_datetime(group.get("updatedAt")) or dt.datetime.min
    if key == "result_amount":
        value = group.get("resultAmount")
        return value if value is not None else -1e18
    if key == "decision_count":
        return int(group.get("decisionCount") or 0)
    if key == "last_run":
        return parse_datetime(group.get("lastRunAt")) or dt.datetime.min
    if key in ("review_score", "reviewscore", "score"):
        return int(group.get("reviewScore") or 0)
    if key == "id":
        return int(group.get("canonicalId") or 0)
    return parse_datetime(group.get("createdAt")) or dt.datetime.min


def parse_datetime(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # 库里的 DATETIME 是本地时间；筛选用的 dateFrom/dateTo 也是 naive，
    # 两边统一去掉时区再比，避免 offset-aware 与 naive 相减报错。
    return parsed.replace(tzinfo=None)
