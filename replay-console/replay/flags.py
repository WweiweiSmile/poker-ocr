"""手牌深挖标记：把「值得看」的信号压成一个位图 + 可读原因。

位图只用于列表筛选，不是策略判断。`review_score` 是启发式排序，不叫 EV。
"""

from __future__ import annotations

from typing import Iterable

from .util import json_load

FLAG_BITS = {
    "AI_DIFF": 1 << 0,
    "AI_FALLBACK": 1 << 1,
    "LOW_CONF": 1 << 2,
    "DATA_WARN": 1 << 3,
    "BIG_POT": 1 << 4,
    "RIVER_BIG": 1 << 5,
    "UNKNOWN_OPP": 1 << 6,
    "AB_FLIP": 1 << 7,
    "REVIEW_BAD": 1 << 8,
    "MANUAL_PICK": 1 << 9,
    "PROMPT_MISSING": 1 << 10,
    "PARTIAL": 1 << 11,
    "INVALID": 1 << 12,
    "SHOWDOWN": 1 << 13,
    "MULTIWAY": 1 << 14,
    "BOMB_POT": 1 << 15,
}

SCORE_WEIGHTS = {
    "AI_DIFF": 30,
    "AI_FALLBACK": 20,
    "DATA_WARN": 15,
    "BIG_POT": 15,
    "RIVER_BIG": 10,
    "UNKNOWN_OPP": 10,
    "AB_FLIP": 25,
    "REVIEW_BAD": 20,
    "LOW_CONF": 10,
}


def _bit(name: str) -> int:
    return int(FLAG_BITS[name])


def _advice_option(advice: dict) -> tuple[str | None, float | None]:
    option = advice.get("option") if isinstance(advice.get("option"), dict) else {}
    kind = option.get("kind") or advice.get("option_kind")
    amount = (option.get("amount_bb")
              if option.get("amount_bb") is not None else option.get("amount"))
    if amount is None:
        amount = advice.get("option_amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    return (str(kind) if kind else None, amount)


def compute_flags(row: dict, analysis,
                  advices: Iterable[dict] | None = None) -> tuple[int, dict, int]:
    """返回 (flag_mask, flags_json, review_score)。

    `advices` 最好是已经 `_annotate_live_advices()` 过的行（带 actual/bucket）。
    """
    mask = 0
    details: dict[str, dict] = {}

    def mark(name: str, reason: str, **extra) -> None:
        nonlocal mask
        mask |= _bit(name)
        item = details.setdefault(name, {"reason": reason, **extra})
        if extra:
            item.update(extra)

    decisions = list(getattr(analysis, "decisions", []) or [])
    advices = [a for a in (advices or []) if isinstance(a, dict)]
    by_key = {}
    for advice in advices:
        by_key[(str(advice.get("street") or ""),
                int(advice.get("action_index") or 0))] = advice

    # AI 与实际行动分歧：优先用 bucket；没有 bucket 时用 option kind 对 actual。
    for decision in decisions:
        key = (decision.street, int(decision.action_index or 0))
        advice = by_key.get(key)
        if not advice:
            continue
        bucket = advice.get("bucket")
        if bucket and bucket not in ("一致", "same"):
            mark("AI_DIFF", f"{decision.street} AI 与实际行动{bucket}")
            break
        kind, _amount = _advice_option(advice)
        actual = getattr(decision, "actual", None)
        if kind and actual and kind != actual:
            mark("AI_DIFF", f"{decision.street} AI {kind} ≠ 实际 {actual}")
            break

    for advice in advices:
        if advice.get("source") and advice.get("source") != "model":
            mark("AI_FALLBACK", f"{advice.get('street')} 建议来源 {advice.get('source')}")
        confidence = advice.get("confidence")
        try:
            if confidence is not None and float(confidence) < 0.5:
                mark("LOW_CONF", f"{advice.get('street')} 置信度 {confidence}")
        except (TypeError, ValueError):
            pass
        if advice.get("prompt_missing"):
            mark("PROMPT_MISSING", f"{advice.get('street')} prompt 未捕获")
        elif not advice.get("prompt_sha256") and advice.get("source") == "model":
            mark("PROMPT_MISSING", f"{advice.get('street')} 旧记录没有 prompt")

    quality = getattr(analysis, "quality", "invalid")
    warnings = list(getattr(analysis, "warnings", []) or [])
    severe = [str(w) for w in warnings
              if any(key in str(w) for key in
                     ("异常", "不可靠", "矛盾", "断裂", "缺失", "0 对手"))]
    if severe:
        mark("DATA_WARN", "；".join(severe[:3]))
    if quality in ("approx", "invalid") and not severe:
        mark("DATA_WARN", f"重建质量 {quality}")
    if row.get("cards_unreliable"):
        mark("DATA_WARN", "牌面读数不可靠")
    data_quality = row.get("data_quality")
    if isinstance(data_quality, dict) and data_quality.get("warnings"):
        mark("DATA_WARN", "；".join(str(w) for w in data_quality["warnings"][:3]))
    if row.get("partial"):
        mark("PARTIAL", "未完成手牌")
    if quality == "invalid":
        mark("INVALID", getattr(analysis, "invalid_reason", None) or "重建失败")

    max_pot = 0.0
    min_spr = None
    max_players = 0
    river_big = False
    for decision in decisions:
        snap = decision.snap
        pot = float(snap.pot or 0.0)
        max_pot = max(max_pot, pot)
        max_players = max(max_players, int(snap.players_dealt or 0),
                          int(snap.opponents_in_hand or 0) + 1)
        spr = getattr(snap, "spr", None)
        if spr is not None:
            min_spr = spr if min_spr is None else min(min_spr, spr)
        if (decision.street == "river" and pot >= 30
                and getattr(decision, "actual", None) in ("bet", "raise", "allin")):
            river_big = True
    if max_pot >= 50 or (min_spr is not None and min_spr <= 2):
        mark("BIG_POT", f"最大底池 {max_pot:.0f}BB"
                        + (f"，最小 SPR {min_spr:.1f}" if min_spr is not None else ""))
    if river_big:
        mark("RIVER_BIG", "河牌大底池且有下注/加注")
    if max_players >= 4:
        mark("MULTIWAY", f"本手最多 {max_players} 人")
    if bool(getattr(analysis, "bomb", False)) or bool(row.get("bomb_pot_bb")):
        mark("BOMB_POT", "爆炸底池")

    # 对手画像覆盖：以建议 context 里的 reads 为准。
    unknown = 0
    for advice in advices:
        context = advice.get("context")
        if not isinstance(context, dict):
            context = json_load(advice.get("context_json"), {}) or {}
        reads = context.get("opponent_reads") or []
        opponents = 0
        # 用对应决策点的在局人数；找不到就跳过。
        key = (str(advice.get("street") or ""), int(advice.get("action_index") or 0))
        decision = next((d for d in decisions
                         if (d.street, int(d.action_index or 0)) == key), None)
        if decision is not None:
            opponents = int(decision.snap.opponents_in_hand or 0)
        unknown = max(unknown, max(0, opponents - len(reads)))
    if unknown >= 2:
        mark("UNKNOWN_OPP", f"至少 {unknown} 名在局对手无画像")

    if row.get("shows") or row.get("showdown"):
        mark("SHOWDOWN", "有摊牌/秀牌")

    score = 0
    for name, weight in SCORE_WEIGHTS.items():
        if mask & _bit(name):
            score += weight
    score = min(100, score)
    return mask, details, score


def flag_names(mask: int) -> list[str]:
    return [name for name, bit in FLAG_BITS.items() if mask & bit]


def parse_flags(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(v) for v in value]
    else:
        raw = str(value).split(",")
    out = []
    for item in raw:
        name = item.strip().upper()
        if name in FLAG_BITS and name not in out:
            out.append(name)
    return out


def mask_for(names: Iterable[str]) -> int:
    mask = 0
    for name in names:
        mask |= FLAG_BITS.get(str(name).upper(), 0)
    return mask
