"""重跑引擎：规则审计、动作分类、模型调用与重复分布聚合。"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from poker_ocr.advice import (
    AIError, Advice, AISettings, IMPLIED_ODDS_CAP_PCT, advise, build_messages,
    call_amount as snapshot_call_amount, complete_json, value_directive,
    winnable_pot,
)
from poker_ocr.preflop_ranges import (
    hand_key, range_verdict, sb_completable,
)

from .decisions import DecisionPoint, call_amount, decision_facts
from .util import cards_of

log = logging.getLogger("replay.engine")

AGGRESSIVE = ("bet", "raise", "allin")
SMALL_PAIRS = {"22", "33", "44", "55", "66"}
CONSERVATIVE = ("fold", "check", "call", "bet", "raise", "allin")


def _aggressive(kind: str | None) -> bool:
    return kind in AGGRESSIVE


def _directive_prefix(sit) -> str:
    d = value_directive(sit) or ""
    return d.split(":")[0] if d else ""


def audit(sit, kind: str | None, amount: float | None) -> set[str]:
    """对一条建议做硬规则审计，返回违规标签集合。

    口径沿用 tools/compare_review_hands.py：只报可证伪的硬错，不做主观好坏。
    """
    tags: set[str] = set()
    snap = sit.snap
    street = snap.street
    pos = (snap.hero_position or "").upper()
    cards = snap.hero_cards
    to_call = float(snap.to_call or 0.0)
    pot = float(snap.pot or 0.0)
    prefix = _directive_prefix(sit)
    agg = _aggressive(kind)

    if agg and prefix in ("NOT A VALUE HAND BY RANK", "NO FOLD EQUITY"):
        tags.add("非价值下注")
    if agg and prefix == "BOARD PLAY":
        if street != "flop" or (amount is not None and pot and amount > 0.5 * pot):
            tags.add("打牌面下注")
    if kind == "raise" and prefix == "VALUE CAUTION":
        tags.add("非坚果加注")
    if kind == "raise" and prefix == "ONE PAIR vs AGGRESSION":
        tags.add("一对加注")
    if kind == "raise" and prefix in ("EXPLOSIVE POT", "TABLE DYNAMICS OVERRIDE"):
        tags.add("非坚果加注")
    if agg and (sit.opponents_in_hand or 0) >= 3 \
            and sit.hand_tier in ("强听", "弱听"):
        tags.add("多路听牌下注")

    if street == "preflop" and len(cards) == 2:
        v = range_verdict(pos, cards)
        key = hand_key(cards)
        if v is not None:
            if pos not in ("SB", "BB") and v.in_range is False:
                if agg and to_call <= 1.0:
                    tags.add("翻前表外加注")
                if kind == "call" and to_call <= 1.0 and key not in SMALL_PAIRS:
                    tags.add("翻前表外溜入")
            if pos not in ("SB", "BB") and v.in_range is True \
                    and kind == "fold" and to_call <= 1.0:
                tags.add("范围内却弃牌")
        if pos == "SB" and kind == "call" and to_call <= 1.5 \
                and not sb_completable(cards):
            tags.add("SB补盲垃圾牌")
        if pos not in ("SB", "BB") and kind == "call" and to_call <= 1.0 \
                and snap.preflop_shapes == ("unopened",):
            tags.add("开池平跟")

    if kind == "fold" and to_call > 0 \
            and sit.hand_tier in ("强听", "弱听") \
            and sit.equity_pct is not None and pot > 0:
        # 全下 cover 我时，牌桌底池里对手超额的那部分会退回，赔率分母只能
        # 用可争夺底池；否则门槛被系统性低估（990036：23% vs 真实 39%）。
        call = snapshot_call_amount(snap)
        winnable = winnable_pot(snap)
        bep = call / winnable * 100 if call > 0 and winnable > 0 else 0.0
        if sit.equity_pct > bep + IMPLIED_ODDS_CAP_PCT:
            tags.add("有赔率却弃牌")

    if street != "preflop" and agg and amount is not None and pot > 0 \
            and sit.hand_tier in ("强成牌", "超对", "顶对强踢") \
            and prefix == "VALUE DEFAULT" and amount < 0.4 * pot:
        tags.add("价值下注过小")
    return tags


def classify(new_kind: str | None, new_amt: float | None,
             act_kind: str | None, act_amt: float | None,
             pot: float, street: str) -> str:
    """一致 / 尺度差 / 动作不同。"""
    nk = "raise" if new_kind == "allin" else new_kind
    ak = "raise" if act_kind == "allin" else act_kind
    if nk == ak:
        if nk in ("bet", "raise") and new_amt is not None and act_amt is not None:
            tol = 1.0 if street == "preflop" else 0.25 * max(pot, 1e-9)
            if abs(float(new_amt) - float(act_amt)) > tol:
                return "尺度差"
        return "一致"
    if nk in ("bet", "raise") and ak in ("bet", "raise"):
        return "尺度差"
    return "动作不同"


def fmt_option(kind: str | None, amount: float | None) -> str:
    if not kind:
        return "—"
    if kind in ("bet", "raise", "allin", "call") and amount is not None:
        return f"{kind} {float(amount):g}"
    return kind


class RateLimiter:
    """简单令牌桶。多个 unit 线程共享，默认 5 req/s。"""

    def __init__(self, per_second: float):
        self.rate = max(float(per_second or 0.0), 0.0)
        self.capacity = max(self.rate, 1.0)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(max(wait, 0.01), 1.0))


_TRANSIENT_PATTERNS = (
    "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
    "HTTP 408", "调用模型失败", "timed out", "timeout", "timedout",
    "Connection reset", "Connection refused", "Temporary failure",
    "Remote end closed", "EOF occurred",
)


def is_transient(exc: BaseException) -> bool:
    text = str(exc)
    if re.search(r"HTTP 5\d\d", text):
        return True
    return any(p.lower() in text.lower() for p in _TRANSIENT_PATTERNS)


@dataclass
class UnitOutcome:
    payload: dict
    source: str
    model: str | None


def execute_unit(
    *,
    decision: DecisionPoint,
    version: dict,
    settings: AISettings,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
    store_prompts: bool = True,
) -> UnitOutcome:
    """跑一个 unit：build_situation → 带重试的模型调用 → 落库 payload。

    重试只针对瞬态错误（超时/429/5xx/连接）；400、鉴权失败、返回非法选项
    不重试。`advise()` 本身把 AIError 转成 fallback —— 这是设计口径：
    fallback 写入结果并标记，但不计入模型一致率的分母。
    """
    from poker_ocr.advice import build_situation

    prompt_style = version.get("prompt_style") or "lean"
    sit = build_situation(decision.snap,
                          table_dynamics=version.get("table_dynamics") or "",
                          style=prompt_style)
    facts = decision_facts(sit)
    actual_tags = sorted(audit(sit, decision.actual, decision.actual_amount))
    facts["actualViolationTags"] = actual_tags

    captured: dict[str, Any] = {"system": None, "user": None, "raw": None}
    attempts = 0

    def complete(settings_, system, user, timeout=timeout, options=None):
        nonlocal attempts
        captured["system"] = system
        captured["user"] = user
        while True:
            attempts += 1
            limiter.acquire()
            try:
                data = complete_json(settings_, system, user,
                                     timeout=timeout, options=options)
                try:
                    captured["raw"] = json.dumps(data, ensure_ascii=False)
                except (TypeError, ValueError):
                    captured["raw"] = str(data)
                return data
            except AIError as exc:
                if attempts > retries or not is_transient(exc):
                    raise
                time.sleep(1.0 if attempts == 1 else 3.0)

    started = time.monotonic()
    try:
        advice: Advice = advise(sit, settings, timeout=timeout,
                                complete=complete,
                                style=prompt_style)
        elapsed = int((time.monotonic() - started) * 1000)
        source = advice.source
        option = advice.option
        kind = option.kind
        if source == "model":
            bucket = classify(kind, option.amount,
                              decision.actual, decision.actual_amount,
                              float(decision.pot or 0.0), decision.street)
            tags = sorted(audit(sit, kind, option.amount))
        else:
            bucket = "error"
            tags = []
        payload = {
            "option_id": option.id,
            "option_kind": kind,
            "option_amount": option.amount,
            "option_label": option.label,
            "reason": advice.reason,
            "source": source,
            "model": advice.model or version.get("model"),
            "duration_ms": elapsed or advice.duration_ms,
            "tokens_in": None,
            "tokens_out": None,
            "bucket": bucket,
            "violation_tags": json.dumps(tags, ensure_ascii=False),
            "facts_json": json.dumps(facts, ensure_ascii=False, default=str),
            "prompt_system": captured["system"] if store_prompts else None,
            "prompt_user": captured["user"] if store_prompts else None,
            "raw_response": captured["raw"] if store_prompts else None,
            "error_msg": advice.error if source != "model" else None,
            "actual_tags": actual_tags,
            "attempts": attempts,
        }
        return UnitOutcome(payload=payload, source=source, model=advice.model)
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.monotonic() - started) * 1000)
        log.warning("unit 异常 hand=%s street=%s version=%s: %s",
                    decision.hand_id, decision.street, version.get("id"), exc)
        payload = {
            "option_id": None, "option_kind": None, "option_amount": None,
            "option_label": None, "reason": None, "source": "error",
            "model": version.get("model"), "duration_ms": elapsed,
            "tokens_in": None, "tokens_out": None, "bucket": "error",
            "violation_tags": json.dumps([], ensure_ascii=False),
            "facts_json": json.dumps(facts, ensure_ascii=False, default=str),
            "prompt_system": captured["system"] if store_prompts else None,
            "prompt_user": captured["user"] if store_prompts else None,
            "raw_response": captured["raw"] if store_prompts else None,
            "error_msg": f"{type(exc).__name__}: {exc}"[:500],
            "actual_tags": actual_tags,
            "attempts": attempts,
        }
        return UnitOutcome(payload=payload, source="error", model=version.get("model"))


# ─────────────────────────── 聚合 ───────────────────────────

def _kind_of(option_id: str | None) -> str | None:
    if not option_id:
        return None
    if option_id == "allin":
        return "allin"
    if option_id.startswith("raise"):
        return "raise"
    if option_id.startswith("bet"):
        return "bet"
    return option_id


def _summary_source(results: list[dict]) -> str:
    sources = {r.get("source") for r in results}
    if not sources:
        return "none"
    if len(sources) == 1:
        return next(iter(sources))
    if "model" in sources:
        return "mixed"
    return "error"


def summarize_version(results: list[dict], *, actual_action: str | None,
                      actual_amount: float | None, pot: float,
                      street: str) -> dict:
    """按版本聚合一个决策点：majority / distribution / allSame / 违规。"""
    model_results = [r for r in results
                     if r.get("source") == "model" and r.get("option_id")]
    kind_by_id = {
        r["option_id"]: (r.get("option_kind") or _kind_of(r["option_id"]))
        for r in model_results
    }
    counts = Counter(r["option_id"] for r in model_results)
    distribution = dict(counts)
    majority = None
    majority_kind = None
    amount = None
    label = None
    if counts:
        max_count = max(counts.values())
        ties = [k for k, v in counts.items() if v == max_count]
        if len(ties) > 1:
            ties.sort(key=lambda x: (CONSERVATIVE.index(kind_by_id.get(x) or "call")
                                     if (kind_by_id.get(x) or "call") in CONSERVATIVE
                                     else 99, x))
        majority = ties[0]
        chosen = next(r for r in model_results if r["option_id"] == majority)
        majority_kind = chosen.get("option_kind") or _kind_of(majority)
        amount = chosen.get("option_amount")
        label = chosen.get("option_label")
    n = len(model_results)
    total = len(results)
    all_same = n > 0 and len(counts) == 1
    source = _summary_source(results)
    tags: set[str] = set()
    for r in results:
        raw = r.get("violation_tags")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        tags.update(raw or [])
    bucket = None
    if majority:
        bucket = classify(majority_kind, amount, actual_action,
                          actual_amount, pot, street)
    elif results:
        bucket = "error"
    facts = None
    for r in results:
        raw = r.get("facts_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if raw:
            facts = raw
            break
    return {
        "optionId": majority,
        "optionKind": majority_kind,
        "amount": amount,
        "optionLabel": label,
        "n": n,
        "total": total,
        "distribution": distribution,
        "allSame": all_same,
        "source": source,
        "bucket": bucket,
        "violationTags": sorted(tags),
        "facts": facts,
        "label": fmt_option(majority_kind, amount),
    }


def public_result(row: dict) -> dict:
    def load(value, default):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return default
        return value if value is not None else default

    return {
        "id": row.get("id"),
        "repeat": int(row.get("repeat_index") or 0),
        "optionId": row.get("option_id"),
        "optionKind": row.get("option_kind"),
        "amount": row.get("option_amount"),
        "optionLabel": row.get("option_label"),
        "reason": row.get("reason") or "",
        "source": row.get("source"),
        "model": row.get("model"),
        "durationMs": row.get("duration_ms"),
        "tokensIn": row.get("tokens_in"),
        "tokensOut": row.get("tokens_out"),
        "bucket": row.get("bucket"),
        "violationTags": load(row.get("violation_tags"), []),
        "error": row.get("error_msg"),
        "facts": load(row.get("facts_json"), None),
    }


def decision_to_public(decision_row: dict, version_rows: list[dict],
                       results: list[dict]) -> dict:
    warnings = decision_row.get("warnings")
    if isinstance(warnings, str):
        try:
            warnings = json.loads(warnings)
        except ValueError:
            warnings = []
    version_blocks = []
    by_version: dict[int, list[dict]] = {}
    for r in results:
        by_version.setdefault(int(r["version_id"]), []).append(r)
    diverged = False
    majorities = []
    for version in version_rows:
        vr = by_version.get(int(version["id"]), [])
        summary = summarize_version(
            vr,
            actual_action=decision_row.get("actual_action"),
            actual_amount=decision_row.get("actual_amount"),
            pot=float(decision_row.get("pot") or 0.0),
            street=decision_row.get("street") or "preflop",
        )
        if not summary["allSame"] and summary["n"] > 1:
            diverged = True
        majorities.append(summary["optionId"])
        version_blocks.append({
            "versionId": int(version["id"]),
            "name": version.get("name"),
            "model": version.get("model"),
            "summary": summary,
            "results": [public_result(r) for r in vr],
        })
    real = [m for m in majorities if m]
    if len(set(real)) > 1:
        diverged = True
    return {
        "decision": {
            "id": decision_row.get("decision_id"),
            "handId": int(decision_row.get("hand_id") or 0),
            "street": decision_row.get("street"),
            "seq": int(decision_row.get("seq") or 0),
            "pot": decision_row.get("pot"),
            "toCall": decision_row.get("to_call"),
            "callAmount": call_amount(decision_row.get("to_call"),
                                      decision_row.get("hero_stack")),
            "heroStack": decision_row.get("hero_stack"),
            "heroPosition": decision_row.get("hero_position"),
            "heroCards": list(cards_of(decision_row.get("hero_cards"))),
            "board": list(cards_of(decision_row.get("board"))),
            "players": decision_row.get("players"),
            "opponents": decision_row.get("opponents"),
            "quality": decision_row.get("quality"),
            "warnings": warnings or [],
        },
        "actual": {
            "action": decision_row.get("actual_action"),
            "amountBb": decision_row.get("actual_amount"),
            "label": fmt_option(decision_row.get("actual_action"),
                                decision_row.get("actual_amount")),
        },
        "versions": version_blocks,
        "diverged": diverged,
    }
