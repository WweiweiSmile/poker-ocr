"""案例集的纯逻辑：桶匹配、最具体 spec、统计（#55 §4.2 / §5）。

这里不碰数据库，服务层负责取行。三条规则：

- 一个决策点可能匹配多条 spec：维度数最多者胜，平手取最近创建（id 大）；
- 案例建好时写死 spec_id，运行不再动态匹配（新增 spec 不改变旧案例）；
- 统计只描述「这个案例集里有什么」，不参与评分。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from poker_ocr.buckets import BUCKET_ORDER, STREETS, bucket_label

_STREET_ORDER = tuple(STREETS)


def bucket_of_row(row: dict) -> dict:
    """决策行 / 案例行 → 桶 dict（只留非空维度）。"""
    bucket: dict[str, Any] = {}
    for dimension in BUCKET_ORDER:
        value = row.get(dimension)
        if value is None or value == "":
            continue
        bucket[dimension] = value
    return bucket


def case_bucket(item: dict) -> dict:
    """案例行 → 桶：street 来自 decision_key，其余来自 join 的决策点。"""
    bucket = bucket_of_row(item)
    street = street_of(item)
    if street:
        bucket["street"] = street
    return {dimension: bucket[dimension]
            for dimension in BUCKET_ORDER if dimension in bucket}


def label_of(bucket: dict) -> str:
    """案例桶 → 展示行。

    案例的桶每维只有一个值（决策点推出来的），而 `bucket_label` 按 spec
    的多值（list）设计；这里包一层单值，避免把字符串当可迭代拆成单字。
    """
    return bucket_label({dimension: [value]
                         for dimension, value in (bucket or {}).items()
                         if value not in (None, "")})


def street_of(item: dict) -> str:
    key = str(item.get("decision_key") or item.get("decisionKey") or "")
    return key.split(":", 1)[0] if ":" in key else ""


def _matches(bucket: dict, spec_bucket: dict) -> bool:
    for dimension, values in (spec_bucket or {}).items():
        if not values:
            continue
        if bucket.get(dimension) not in values:
            return False
    return True


def best_spec(bucket: dict, candidates: Iterable[dict]) -> dict | None:
    """最具体的 spec：命中的维度最多；平手取 id 最大（最近创建）。"""
    best: dict | None = None
    best_key: tuple[int, int] | None = None
    for spec in candidates:
        spec_bucket = spec.get("bucket") or {}
        if not _matches(bucket, spec_bucket):
            continue
        key = (len(spec_bucket), int(spec.get("id") or 0))
        if best_key is None or key > best_key:
            best, best_key = spec, key
    return best


def case_stats(items: list[dict]) -> dict:
    """案例集详情用的统计：总数、P0 比例、按优先级/街/桶分布。"""
    total = len(items)
    p0 = sum(1 for item in items
             if int(item.get("priority") or 0) == 0)
    by_priority = Counter(str(int(item.get("priority") or 0))
                          for item in items)
    by_street = Counter(street_of(item) for item in items if street_of(item))

    buckets: Counter[tuple] = Counter()
    labels: dict[tuple, str] = {}
    for item in items:
        # 服务层传进来的行已经带好 `bucket`；裸行走 case_bucket 现算。
        bucket = item.get("bucket") or case_bucket(item)
        key = tuple((dimension, bucket.get(dimension))
                    for dimension in BUCKET_ORDER)
        buckets[key] += 1
        labels.setdefault(key, label_of(bucket))

    return {
        "total": total,
        "p0": p0,
        "p0Ratio": round(p0 / total, 4) if total else 0.0,
        "byPriority": [{"priority": int(priority), "count": count}
                       for priority, count in sorted(by_priority.items())],
        "byStreet": [{"street": street, "count": by_street[street]}
                     for street in _STREET_ORDER if by_street.get(street)],
        "byBucket": [
            {
                "bucket": {dimension: value for dimension, value in key
                           if value is not None},
                "label": labels[key],
                "count": count,
            }
            for key, count in sorted(
                buckets.items(),
                key=lambda pair: (-pair[1], labels[pair[0]]))
        ],
    }
