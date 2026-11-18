"""偏差热点（#59）：一次评分运行按 spec 桶聚合出的偏差报表。

第一性原理（与设计 §7 / §8 对齐）：
- 聚合单元是 **spec**，不是案例：spec 的 bucket 在运行时已冻结，频次目标也
  挂在 spec 上；同桶不同 spec 说明目标不同，混在一起算会让「频率达成」失去
  意义。
- 分数口径只认 run_items 的首次建议（repeat_index=0）与人工改判后的有效值；
  总览均分与各桶均分都走 `scoring.mean_ci`（与 `run_worker.aggregate_rows`
  同一函数），不另立算法。
- `n`、区间、口径三者永远一起出现：没有区间的「偏差大」不算结论。
- 样本门槛固定为 `scoring.FREQ_MIN_N`（设计 §13：不做 per-spec / per-run 覆盖）；
  不够就只报 `insufficient`，**不进**频率达成率的分子/分母。
- 带外频率的衰减由 `scoring.freq_score` 负责，这里只负责「偏差强度」与排序。

本模块是纯函数，不碰数据库；服务层负责把 run_items、spec 冻结快照、人工
改判拼成输入。
"""
from __future__ import annotations

from typing import Any, Iterable

from . import scoring

# dev 的封顶：频次偏差按「半带宽」计，均值偏差按 50 分计，最多 3 档；
# severity 再乘 sqrt(样本数)，让「差得多」和「差得稳」都可比。样本数必须与
# dev 同口径：频次口径取可比样本，均分口径取计分条目（见 hotspot_rows）。
DEVIATION_CAP = 3.0
MEAN_CAP = 50.0
WIDTH_FLOOR = 0.02
# 样本门槛是全局常量（设计 §13.4）：这里只读，不提供覆盖入口。
FREQ_MIN_N = scoring.FREQ_MIN_N


def _rate(count: int, total: int) -> float | None:
    return (count / total) if total else None


def _deviation(freq: dict[str, Any] | None,
               mean: float | None) -> tuple[float | None, str | None]:
    """单个桶的偏差强度（0–3）与所用口径；两种口径互斥，频次优先。

    `freq` 只在「样本够、带宽齐全」时可判定（`met` / `missed`）；样本不足
    或 spec 没有频次目标时退回均分口径——此时频率事实本来就不成立。
    返回的 `basis` 会随行下发，页面必须把口径标出来：同一个数字换了口径
    而看不出来，就是在制造假结论。
    """
    if freq is not None and freq.get("status") in ("met", "missed"):
        observed = freq.get("observed")
        low, high = freq.get("min"), freq.get("max")
        if observed is not None and low is not None and high is not None:
            mid = (float(low) + float(high)) / 2
            half = max((float(high) - float(low)) / 2, WIDTH_FLOOR)
            return min(DEVIATION_CAP, abs(float(observed) - mid) / half), "freq"
    if mean is not None:
        return (min(DEVIATION_CAP, max(0.0, (100.0 - float(mean)) / MEAN_CAP)),
                "mean")
    return None, None


def _freq_cell(freq: dict[str, Any] | None,
               kinds: list[str]) -> dict[str, Any] | None:
    if not freq:
        return None
    scored = scoring.freq_score(freq, kinds, min_n=FREQ_MIN_N)
    action = freq.get("action")
    n = scored["n"]
    hits = sum(1 for kind in kinds if kind == action) if action else 0
    return {
        "action": action,
        "min": freq.get("min"),
        "max": freq.get("max"),
        "status": scored["status"],
        "n": n,
        "observed": scored["observed"],
        "score": scored["score"],
        # 频率是比例：同屏给 Wilson 区间（设计 §7.6）。
        "ci": scoring.wilson(hits, n) if n else None,
    }


def hotspot_rows(items: Iterable[dict[str, Any]],
                 specs: dict[int, dict[str, Any]], *,
                 global_mean: float | None = None) -> list[dict[str, Any]]:
    """按 spec 桶聚合偏差；`specs` 是 spec_id → 冻结快照（含 bucket/freq）。"""
    groups: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        try:
            spec_id = int(item.get("specId") or 0)
        except (TypeError, ValueError):
            continue
        groups.setdefault(spec_id, []).append(item)

    rows: list[dict[str, Any]] = []
    for spec_id, group in groups.items():
        spec = specs.get(spec_id) or {}
        bucket = spec.get("bucket") or {}
        label = spec.get("bucketLabel") or ""
        scored = [float(i["score"]) for i in group
                  if i.get("score") is not None]
        mean_cell = scoring.mean_ci(scored)
        mean = mean_cell["mean"]
        # 硬失败率与 `scoring.aggregate` 同口径：只数已出分条目。
        hard = sum(1 for i in group
                   if i.get("hardFail") and i.get("score") is not None)
        hard_rate = _rate(hard, len(scored))
        kinds = [str(i.get("kind")) for i in group
                 if i.get("comparable") and i.get("kind")]
        freq = _freq_cell(spec.get("freq"), kinds)
        dev, basis = _deviation(freq, mean)
        n = len(group)
        # 有频次目标时以它自己的可比样本为准（也是达成率的分母口径）；
        # 没有频次目标的桶退回「本桶条目数」。
        insufficient = ((freq["status"] == "insufficient")
                        if freq is not None else n < FREQ_MIN_N)
        # severity 的 n 必须与 dev 同口径：频次口径只数可比样本，均分口径才数
        # 计分条目；否则 error/未计分的行会把严重度凭空放大。
        samples = (freq["n"] if basis == "freq" and freq is not None
                   else len(scored))
        rows.append({
            "specId": spec_id,
            "specName": spec.get("name") or "",
            "bucket": bucket,
            "bucketLabel": label,
            "n": n,
            "scored": len(scored),
            "insufficient": insufficient,
            "mean": mean,
            "scoreCi": {
                "n": mean_cell["n"],
                "lo": mean_cell["lo"],
                "hi": mean_cell["hi"],
            },
            "delta": ((mean - global_mean)
                      if mean is not None and global_mean is not None
                      else None),
            "hardFail": {
                "count": hard,
                "rate": hard_rate,
                "ci": (scoring.wilson(hard, len(scored))
                       if scored else None),
            },
            "freq": freq,
            "dev": (round(dev, 4) if dev is not None else None),
            "basis": basis,
            "severity": (round(dev * (samples ** 0.5), 3)
                         if dev is not None and samples else None),
        })

    rows.sort(key=lambda row: (
        row["severity"] is None,
        -(row["severity"] or 0.0),
        -row["n"],
        row["bucketLabel"],
        row["specId"],
    ))
    return rows


def summarize(items: list[dict[str, Any]],
              rows: list[dict[str, Any]]) -> dict[str, Any]:
    """报表头：总均分、频率达成率（只数达标桶）、样本不足桶数。"""
    scored = [float(i["score"]) for i in items if i.get("score") is not None]
    met = sum(1 for row in rows
              if row["freq"] and row["freq"]["status"] == "met")
    missed = sum(1 for row in rows
                 if row["freq"] and row["freq"]["status"] == "missed")
    judged = met + missed
    hard = sum(1 for row in rows if row["hardFail"]["count"])
    return {
        "freqMinN": FREQ_MIN_N,
        "specs": len(rows),
        "cases": len(items),
        "scored": len(scored),
        "insufficient": sum(1 for row in rows if row["insufficient"]),
        "score": scoring.mean_ci(scored),
        "attainment": {
            "met": met,
            "missed": missed,
            "judged": judged,
            "rate": _rate(met, judged),
            # 比例类一律带 Wilson 区间（设计 §7.6）
            "ci": scoring.wilson(met, judged) if judged else None,
        },
        "bucketsWithHardFail": hard,
    }
