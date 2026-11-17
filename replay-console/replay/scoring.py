"""案例评分（#56）：纯函数，不碰 DB、不跑模型。

一次评分需要的输入都是普通 dict / 标量：

- spec：spot_spec.v1（allowed/forbidden/sizing/required_points/
  forbidden_points/freq）；
- advice：模型建议（option_id、kind、amount、reason、source）；
- decision：决策点（option ids、pot、bb）；
- facts：实际事实；幻觉判定由调用方（judge，见 #58）给出 hallucinated，
  本模块不猜。

score_case() 的输出可直接写 replay_run_items.scores_json；
aggregate() 出总分 / P0 通过率 / 覆盖率与 Wilson 区间。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: 五维权重；总分是加权平均后乘 100（权重和恰好 100）。
W: dict[str, int] = {
    "action": 35, "size": 15, "reason": 30, "fact": 15, "stable": 5,
}
DIMENSIONS = tuple(W)
#: 及格线（设计 §7.1）。测试引用常量，不写 60 魔数。
PASS_SCORE = 60
#: 硬失败封顶分（设计 §7.1）。
HF_CAP = 40
#: 频率样本门槛（设计 §7.5）；不进 spec、不改库。
FREQ_MIN_N = 15
#: min == max 的带兜底宽度（设计 §7.1）。
SIZE_ZERO_WIDTH = 0.1
SIZING_UNITS = ("pot_fraction", "bb")
SIZE_ACTIONS = frozenset({"bet", "raise", "allin"})

#: 判定版本：硬失败 / 依据维的规则引擎版本，冻结进 run 的 config_snapshot。
#: 接入 LLM judge 后改这里（并让旧 run 保持自己的值）。
JUDGE_VERSION = "rules-v1"

HF_ERROR = "HF_ERROR"
HF_OPTION = "HF_OPTION"
HF_FALLBACK = "HF_FALLBACK"
HF_FORBIDDEN_ACTION = "HF_FORBIDDEN_ACTION"
HF_FORBIDDEN_POINT = "HF_FORBIDDEN_POINT"
HF_HALLUCINATION = "HF_HALLUCINATION"
#: 六个硬失败码（设计 §7.2）。同一案例命中多个时按此顺序取第一个。
HARD_FAIL_CODES = (
    HF_ERROR, HF_OPTION, HF_FALLBACK, HF_FORBIDDEN_ACTION,
    HF_FORBIDDEN_POINT, HF_HALLUCINATION,
)

#: 退化口径：没有 required_points 时，理由里出现动作词就算「支持该动作」。
_ACTION_WORDS = {
    "fold": ("弃牌", "盖牌", "fold"),
    "check": ("过牌", "看牌", "check"),
    "call": ("跟注", "call"),
    "bet": ("下注", "bet"),
    "raise": ("加注", "raise"),
    "allin": ("全下", "all-in", "allin", "推"),
}


def _as_float(value: Any) -> float | None:
    """宽松转 float；bool / 坏值一律 None（fail open，绝不抛）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def bands(sizing: Any) -> list[dict]:
    """把 sizing 归一成 [{unit, min, max}]；坏带整条丢掉。"""
    raw = [sizing] if isinstance(sizing, Mapping) else list(sizing or [])
    out: list[dict] = []
    for band in raw:
        if not isinstance(band, Mapping):
            continue
        unit = str(band.get("unit") or "pot_fraction")
        if unit not in SIZING_UNITS:
            continue
        minimum = _as_float(band.get("min"))
        maximum = _as_float(band.get("max"))
        if minimum is None or maximum is None or maximum < minimum:
            continue
        out.append({"unit": unit, "min": minimum, "max": maximum})
    return out


def _unit_value(unit: str, amount: float, pot: float | None) -> float | None:
    """把金额换算成带单位的值；缺分母返回 None（该带不参与）。

    `amount` 与 option_amount 同口径（本街加到多少 BB，见 poker_ocr/options.py），
    所以 `bb` 带直接用原值——再除一次大盲会把 2.5BB 算成 1.25BB。
    """
    if unit == "bb":
        return amount
    if not pot or pot <= 0:
        return None
    return amount / pot


def size_score(kind: str | None, amount: Any, sizing: Any, *,
               pot: Any = None) -> float:
    """尺寸维；非下注类动作记 1.0，所有带都不适用时也记 1.0（fail open）。"""
    if kind not in SIZE_ACTIONS:
        return 1.0
    value_amount = _as_float(amount)
    if value_amount is None:
        return 0.0
    pot_value = _as_float(pot)
    scored: list[float] = []
    for band in bands(sizing):
        value = _unit_value(band["unit"], value_amount, pot_value)
        if value is None:
            continue
        minimum, maximum = band["min"], band["max"]
        if minimum <= value <= maximum:
            scored.append(1.0)
            continue
        width = maximum - minimum or SIZE_ZERO_WIDTH
        distance = (minimum - value) if value < minimum else (value - maximum)
        scored.append(max(0.0, 1.0 - distance / width))
    return min(scored) if scored else 1.0


def action_score(kind: str | None, allowed: Sequence[str] | None = None,
                 forbidden: Sequence[str] | None = None) -> float:
    """动作维：allowed 内、且不在 forbidden → 1，否则 0。"""
    if not kind:
        return 0.0
    if forbidden and kind in forbidden:
        return 0.0
    if allowed and kind not in allowed:
        return 0.0
    return 1.0


def supports_action(reason: str | None, kind: str | None) -> bool:
    """退化口径：理由里是否提到该动作（judge 到位前的占位）。"""
    text = str(reason or "").lower()
    if not text or not kind:
        return False
    return any(word in text for word in _ACTION_WORDS.get(kind, ()))


def reason_score(reason: str | None, required_points: Any, *,
                 kind: str | None = None,
                 matched: Iterable[str] = ()) -> float:
    """依据维：命中要点加权和 / 总权重；无要点退化为 0/1。"""
    points = [p for p in (required_points or []) if isinstance(p, Mapping)]
    total = sum(max(_as_float(p.get("weight")) or 0.0, 0.0) for p in points)
    if not points or total <= 0:
        return 1.0 if supports_action(reason, kind) else 0.0
    hit = set(matched)
    got = sum(max(_as_float(p.get("weight")) or 0.0, 0.0)
              for p in points if p.get("key") in hit)
    return max(0.0, min(1.0, got / total))


def fact_score(cited: int = 0, wrong: int = 0) -> float:
    """事实维：1 - 错误/引用；引用 0 项记 1（设计 §7.1）。"""
    if cited <= 0:
        return 1.0
    return max(0.0, 1.0 - max(int(wrong), 0) / int(cited))


def stable_score(kinds: Sequence[str | None] | None) -> float | None:
    """稳定维：N 次重复的动作一致率；不足 2 次返回 None（不进分母）。"""
    seen = [k for k in (kinds or []) if k]
    if len(seen) < 2:
        return None
    return max(seen.count(k) for k in set(seen)) / len(seen)


def weighted_total(scores: Mapping[str, float | None]) -> tuple[float, float]:
    """按权重合成 0–100 分，返回 (score, 参与维度权重和)。"""
    total = weight = 0.0
    for dim, dim_weight in W.items():
        value = scores.get(dim)
        if value is None:
            continue
        total += dim_weight * float(value)
        weight += dim_weight
    if weight <= 0:
        return 0.0, 0.0
    return total / weight * 100.0, weight


def detect_hard_fail(*, source: str = "model", option_id: Any = None,
                     option_ids: Iterable[Any] = (),
                     kind: str | None = None,
                     forbidden_actions: Iterable[str] = (),
                     forbidden_points: Iterable[str] = (),
                     matched_points: Iterable[str] = (),
                     hallucinated: bool = False) -> str | None:
    """按固定优先级判一个硬失败码；没有就返回 None。"""
    if source == "error":
        return HF_ERROR
    if option_ids or option_id is not None:
        allowed_ids = {str(i) for i in option_ids}
        if option_id is None or str(option_id) not in allowed_ids:
            return HF_OPTION
    if source and source != "model":
        return HF_FALLBACK
    if kind and kind in set(forbidden_actions):
        return HF_FORBIDDEN_ACTION
    if set(matched_points) & set(forbidden_points):
        return HF_FORBIDDEN_POINT
    if hallucinated:
        return HF_HALLUCINATION
    return None


def score_case(*, spec: Mapping[str, Any] | None, advice: Mapping[str, Any],
               option_ids: Iterable[Any] = (), pot: Any = None,
               matched_points: Iterable[str] = (),
               cited_facts: int = 0, wrong_facts: int = 0,
               repeat_kinds: Sequence[str | None] | None = None,
               hallucinated: bool = False,
               hard_fail: str | None = None) -> dict:
    """给一次建议打分；返回可直接落库的 dict。"""
    spec = spec or {}
    kind = advice.get("kind") or advice.get("option_kind")
    scores: dict[str, float | None] = {
        "action": action_score(kind, spec.get("allowed_actions"),
                               spec.get("forbidden_actions")),
        "size": size_score(kind, advice.get("amount"),
                           spec.get("sizing"), pot=pot),
        "reason": reason_score(advice.get("reason"),
                               spec.get("required_points"), kind=kind,
                               matched=matched_points),
        "fact": fact_score(cited_facts, wrong_facts),
        "stable": stable_score(repeat_kinds),
    }
    if hard_fail is None:
        hard_fail = detect_hard_fail(
            source=str(advice.get("source") or "model"),
            option_id=advice.get("option_id"), option_ids=option_ids,
            kind=kind,
            forbidden_actions=spec.get("forbidden_actions") or (),
            forbidden_points=(
                p.get("key") for p in (spec.get("forbidden_points") or [])
                if isinstance(p, Mapping)),
            matched_points=matched_points,
            hallucinated=hallucinated)
    score, weight = weighted_total(scores)
    if hard_fail:
        score = min(score, HF_CAP)
    return {
        "scores": {dim: (round(v, 4) if v is not None else None)
                   for dim, v in scores.items()},
        "weight": round(weight, 2),
        "score": round(score, 2),
        "hardFail": hard_fail,
    }


def wilson(successes: int, n: int, z: float = 1.96) -> list[float]:
    """比例的 Wilson 95% 区间（设计 §7.6）。"""
    if n <= 0:
        return [0.0, 0.0]
    p = successes / n
    z2 = z * z
    denominator = 1 + z2 / n
    center = p + z2 / (2 * n)
    margin = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5)
    low = (center - margin) / denominator
    high = (center + margin) / denominator
    return [round(max(0.0, low), 4), round(min(1.0, high), 4)]


def mean_ci(values: Sequence[float]) -> dict:
    """均值与正态近似区间（设计 §7.6）；空样本给 None。"""
    clean = [float(v) for v in values if v is not None]
    n = len(clean)
    if not n:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    mean = sum(clean) / n
    if n == 1:
        return {"n": 1, "mean": round(mean, 2), "lo": None, "hi": None}
    variance = sum((v - mean) ** 2 for v in clean) / (n - 1)
    margin = 1.96 * (variance ** 0.5) / (n ** 0.5)
    return {"n": n, "mean": round(mean, 2),
            "lo": round(mean - margin, 2), "hi": round(mean + margin, 2)}


def bucket_counts(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """每个桶在本次 run 里的有效案例数（覆盖率与频率的样本口径）。"""
    counts: dict[str, int] = {}
    for item in items:
        bucket = item.get("bucket")
        if not bucket or item.get("score") is None:
            continue
        key = str(bucket)
        counts[key] = counts.get(key, 0) + 1
    return counts


def coverage(items: Iterable[Mapping[str, Any]], *, bucket_total: int,
             min_n: int = FREQ_MIN_N) -> dict:
    """覆盖率：样本达 min_n 的桶数 / 有 active spec 的桶数。"""
    reached = sum(1 for n in bucket_counts(items).values() if n >= min_n)
    total = max(int(bucket_total), 0)
    return {
        "bucketTotal": total,
        "reached": reached,
        "rate": round(reached / total, 4) if total else 0.0,
    }


def freq_score(freq: Mapping[str, Any] | None,
               kinds: Sequence[str | None], *,
               min_n: int = FREQ_MIN_N) -> dict:
    """频率达成率（设计 §7.5）：带内 1，带外按超出量线性衰减。"""
    clean = [k for k in kinds if k]
    n = len(clean)
    action = (freq or {}).get("action")
    if not freq or not action:
        return {"status": "absent", "n": n, "observed": None, "score": None}
    observed = sum(1 for k in clean if k == action) / n if n else 0.0
    if n < min_n:
        return {"status": "insufficient", "n": n,
                "observed": round(observed, 4), "score": None}
    low = _as_float(freq.get("min")) or 0.0
    high = _as_float(freq.get("max")) or 0.0
    if low <= observed <= high:
        return {"status": "met", "n": n,
                "observed": round(observed, 4), "score": 1.0}
    width = high - low or SIZE_ZERO_WIDTH
    excess = (low - observed) if observed < low else (observed - high)
    return {"status": "missed", "n": n, "observed": round(observed, 4),
            "score": round(max(0.0, 1.0 - excess / width), 4)}


def aggregate(items: Sequence[Mapping[str, Any]], *,
              bucket_total: int = 0,
              p0_total: int | None = None) -> dict:
    """聚合一个 run（设计 §7.3/§7.6）：均分、P0 通过率、覆盖率、CI。

    `p0_total` 是本次 run 的 P0 案例总数（含没出分/没跑到的）：只按已出分
    的 P0 算分母会把通过率算虚高。不传时退回已出分数量。
    """
    scored = [item for item in items if item.get("score") is not None]
    scores = [float(item["score"]) for item in scored]
    p0 = [item for item in scored if int(item.get("priority") or 0) == 0]
    p0_passed = [item for item in p0 if not item.get("hardFail")
                 and (item.get("scores") or {}).get("action") == 1.0]
    p0_denominator = max(int(p0_total or 0), len(p0))
    hard_failed = [item for item in scored if item.get("hardFail")]
    return {
        "cases": len(items),
        "scored": len(scored),
        "score": mean_ci(scores),
        "p0": {
            "total": p0_denominator,
            "passed": len(p0_passed),
            "rate": (round(len(p0_passed) / p0_denominator, 4)
                     if p0_denominator else 0.0),
            "ci": wilson(len(p0_passed), p0_denominator),
        },
        "hardFail": {
            "count": len(hard_failed),
            "rate": round(len(hard_failed) / len(scored), 4) if scored else 0.0,
            "ci": wilson(len(hard_failed), len(scored)),
        },
        "coverage": coverage(items, bucket_total=bucket_total),
    }
