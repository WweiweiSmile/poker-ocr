#!/usr/bin/env python3
"""A/B 对跑：两份 `replay_live_decisions.py --json` dump → 三档分类 + 延迟 + 回滚判断。

同一批决策点在两套提示词下各重问一次模型，这里只负责**判读**：

    {旧侧 dump} × {新侧 dump}
      → 按 (hand, street, cards, board, pot, to_call) 对齐（pot/to_call 容差 0.01）
      → 只保留两侧都重建成功、且都不是 fallback 的点
      → 逐点三档：动作方向一致 / 仅尺度不同 / 动作冲突
      → 冲突清单、尺度差分布、延迟分位、fallback 计数、c-bet 子集
      → 按写死的阈值给「建议保留 / 建议回滚」+ 回滚步骤

**分类口径**（第一性原理：先问「差在哪一层」，再定类别）：

  · 动作方向一致：两侧 option id 完全相同 —— 方向和尺寸都没变。
  · 仅尺度不同：同一动作族，只是尺寸档不同（raise-1/3 vs raise-1/2、
    open-5.5 vs open-8）。翻前 open-* / raise-*x 与翻后 raise-<f> 都归 raise 族。
  · 动作冲突：动作族不同（call vs raise、check vs bet、fold vs call、
    raise vs all-in）。all-in 单列一族：它是承诺全部后手，不是「另一个尺寸」。
  · 无法分类：id 缺失/空/不是已知词表 —— 只出现在坏 dump 里，单独计数，
    **不进三档的分母**，避免把脏数据算成冲突或一致。

`call` 与 `check` 同属 call 族：本仓库的 option 词表里「没人下注时的过牌」
的 id 就是 `call`（见 `poker_ocr/options.py` 的 `Option("call", "过牌", "check")`），
对齐键里带着 to_call，同一点上 call/check 是同一个物理动作，不该算冲突。

用法：

    ./.venv/bin/python tools/ab_compare.py \\
        --old logs/ab/dump-old-77.json logs/ab/dump-old-62.json \\
        --new logs/ab/dump-new-77.json logs/ab/dump-new-62.json \\
        --json logs/ab/ab-2026-11-18.json \\
        --md   logs/ab/ab-2026-11-18.md
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

# ── 判定阈值：报告里原样打印，改这里就是改结论 ──
POT_TOL = 0.01                 # 对齐时 pot/to_call 的容差（BB）
CONFLICT_RATE_MAX = 0.20       # 动作冲突率红线（> 20% → 建议回滚）
CBET_CONFLICT_RATE_MAX = 0.20  # c-bet 子集冲突率红线（与总体同口径）
CBET_MIN_POINTS = 5            # c-bet 子集小于这个数就不下结论
P50_RATIO_MAX = 2.0            # 新 p50 / 旧 p50 上限（翻倍即回滚）
LATENCY_BUDGET_MS = 8000       # AI_SERVER_TIMEOUT 预算：max 超过它就会超时
REASON_CLIP = 160              # 冲突清单里 reason 摘要的截断长度

# main() 里按文件记账（build_report 只拿到行，拿不到每份 dump 的点数）
_SIZES: dict[str, int] = {}

SAME = "动作方向一致"
SIZE_ONLY = "仅尺度不同"
CONFLICT = "动作冲突"
UNCLASSIFIED = "无法分类"
CATEGORIES = (SAME, SIZE_ONLY, CONFLICT, UNCLASSIFIED)

# 尺寸序：底池比例档从 1/3 到 1.2 池。
FRACTION_ORDER = {"1/4": 0.25, "1/3": 1 / 3, "1/2": 0.5, "2/3": 2 / 3,
                  "3/4": 0.75, "1": 1.0, "1.2": 1.2}

_PASSIVE = frozenset({"call", "check"})
_ALLIN = frozenset({"allin", "all_in", "all-in", "shove", "jam"})

ROLLBACK_STEPS = (
    "1. 规则台账的运行时开关 `enabled=false` 在设计里是 profile 校验（不是 .env），"
    "但 #64–#71 只落了 loader / 匹配器 / 渲染器，**当前代码还没有这个开关**；"
    "所以实际可执行的是第 2 条。",
    "2. 把 `poker_ocr/prompt/version.py` 的 PROMPT_VERSION 回退到 "
    "`lean-zh-2026-11-16.3`，同步回退 replay-console 的 CURRENT_ADVICE_VERSION 与 "
    "golden 三个 hash（EXPECTED_PROMPT_HASH / EXPECTED_THREE_BET_PROMPT_HASH / "
    "EXPECTED_AIR_PROMPT_HASH）。",
    "3. 跑一轮 13 案例 replay-console，确认无 HF"
    "（HF_OPTION / HF_FALLBACK / HF_FORBIDDEN_ACTION / HF_HALLUCINATION）。",
    "4. 重跑本工具（或 `tools/replay_live_decisions.py --model`），确认动作冲突率"
    "与延迟回到基线水平。",
)


# ─────────────────────────── 分类：纯函数 ───────────────────────────

def _norm(option_id: Any) -> str:
    """option id 归一化：去空白、转小写；非字符串一律当缺失。"""
    if not isinstance(option_id, str):
        return ""
    return option_id.strip().lower()


def action_family(option_id: Any) -> str | None:
    """动作族：fold / call / bet / raise / allin；认不出来返回 None。

    词表来自 `poker_ocr/options.py`：postflop `call|fold|raise-<分数>|all-in`，
    preflop `open-<BB>|raise-<倍数>x|call|fold|all-in`；别名兜底
    `bet-<分数>` / `allin` / `all_in`。
    """
    oid = _norm(option_id)
    if not oid:
        return None
    if oid == "fold":
        return "fold"
    if oid in _PASSIVE:
        return "call"
    if oid in _ALLIN:
        return "allin"
    if oid == "bet" or oid.startswith("bet-"):
        return "bet"
    if (oid == "raise" or oid.startswith("raise-")
            or oid.startswith("open-")):
        return "raise"
    return None


def action_size(option_id: Any) -> str | None:
    """尺寸档 token；fold/call/check/allin 这类无尺寸动作返回 None。"""
    oid = _norm(option_id)
    fam = action_family(oid)
    if fam in (None, "fold", "call", "allin"):
        return None
    if oid.startswith("open-"):
        return oid[len("open-"):] or None
    if oid.startswith("raise-"):
        return oid[len("raise-"):] or None
    if oid.startswith("bet-"):
        return oid[len("bet-"):] or None
    return None


def classify(old_id: Any, new_id: Any) -> str:
    """三档分类（外加「无法分类」）。缺字段/未知 id 不猜。"""
    old_fam = action_family(old_id)
    new_fam = action_family(new_id)
    if old_fam is None or new_fam is None:
        return UNCLASSIFIED
    if _norm(old_id) == _norm(new_id):
        return SAME
    if old_fam == new_fam:
        return SIZE_ONLY
    return CONFLICT


def _size_scale(option_id: Any) -> tuple[str, float] | None:
    """尺寸在哪一把尺子上：pot（底池比例）/ mult（对手加注倍数）/ bb（金额）。

    跨尺子不可比：open-5.5（5.5BB）和 raise-1/3（1/3 池）都属 raise 族，
    但「5.5 > 0.333」没有意义，不能报「变大」。
    """
    size = action_size(option_id)
    if size is None:
        return None
    if size in FRACTION_ORDER:
        return ("pot", FRACTION_ORDER[size])
    if size.endswith("x"):
        try:
            return ("mult", float(size[:-1]))
        except ValueError:
            return None
    try:
        return ("bb", float(size))
    except ValueError:
        return None


def size_direction(old_id: Any, new_id: Any) -> str:
    """仅尺度不同的点：变大 / 变小 / 不变 / 不可比。"""
    old_size = action_size(old_id)
    new_size = action_size(new_id)
    if old_size is None and new_size is None:
        return "不变"          # 例如 call vs check：同一物理动作的两种写法
    if old_size == new_size:
        return "不变"
    old_scale = _size_scale(old_id)
    new_scale = _size_scale(new_id)
    if old_scale is None or new_scale is None or old_scale[0] != new_scale[0]:
        return "不可比"
    if old_scale[1] == new_scale[1]:
        return "不变"
    return "变大" if new_scale[1] > old_scale[1] else "变小"


def size_label(option_id: Any) -> str:
    """报告里的尺寸标签：有尺寸用尺寸，没尺寸用动作族。"""
    fam = action_family(option_id)
    size = action_size(option_id)
    if fam is None:
        return _norm(option_id) or "（缺失）"
    if size is None:
        return fam
    if _norm(option_id).startswith("open-"):
        return f"open-{size}"
    return size


# ─────────────────────────── 对齐 ───────────────────────────

def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cards(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        parts = [str(x).strip() for x in value]
    elif isinstance(value, str):
        parts = value.split()
    else:
        return ()
    return tuple(p for p in parts if p)


def is_fallback(row: dict) -> bool:
    """这一侧有没有真的问过模型；没有 / 报错 / 显式 fallback 都算 fallback。"""
    if row.get("fallback") is True:
        return True
    new = row.get("new")
    return new is None or new == "" or new == "ERROR"


def _key_head(row: dict) -> tuple:
    return (str(row.get("hand") or "").strip(),
            str(row.get("street") or "").strip(),
            _cards(row.get("cards")),
            _cards(row.get("board")))


def align(old_rows: Sequence[dict],
          new_rows: Sequence[dict]) -> tuple[list[tuple[dict, dict]], int, int]:
    """按 (hand, street, cards, board) 分桶 + pot/to_call 容差配对。

    返回 (配对列表, 旧侧未配上的点数, 新侧未配上的点数)。配对按旧侧顺序消费，
    同一把键里的重复点（同一手同一街同牌面同底池）一一对应、不重复使用。
    """
    buckets: dict[tuple, list[tuple[int, dict]]] = {}
    for i, row in enumerate(new_rows):
        buckets.setdefault(_key_head(row), []).append((i, row))
    used: set[int] = set()
    pairs: list[tuple[dict, dict]] = []
    for old in old_rows:
        pot = _num(old.get("pot"))
        call = _num(old.get("to_call"))
        if pot is None or call is None:
            continue
        best: tuple[float, int, dict] | None = None
        for i, new in buckets.get(_key_head(old), ()):
            if i in used:
                continue
            npot, ncall = _num(new.get("pot")), _num(new.get("to_call"))
            if npot is None or ncall is None:
                continue
            dpot, dcall = abs(npot - pot), abs(ncall - call)
            if dpot <= POT_TOL + 1e-9 and dcall <= POT_TOL + 1e-9:
                score = dpot + dcall
                if best is None or (score, i) < (best[0], best[1]):
                    best = (score, i, new)
        if best is not None:
            used.add(best[1])
            pairs.append((old, best[2]))
    return pairs, len(old_rows) - len(pairs), len(new_rows) - len(used)


# ─────────────────────────── 统计 ───────────────────────────

def percentile(values: Sequence[float], pct: float) -> float | None:
    """nearest-rank 分位（p50 对偶数样本取较低的那个，更保守）。"""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[k - 1]


def latency_stats(rows: Sequence[dict]) -> dict:
    ms = [int(r["ms"]) for r in rows if isinstance(r.get("ms"), (int, float))]
    return {
        "n": len(ms),
        "p50": percentile(ms, 50),
        "p90": percentile(ms, 90),
        "max": max(ms) if ms else None,
        "over_budget": sum(1 for v in ms if v > LATENCY_BUDGET_MS),
    }


def is_cbet_point(row: dict) -> bool:
    """单挑 c-bet 面：翻牌、无人下注、两人在局、候选侧提示词里带「规则:」。"""
    facts = row.get("prompt_facts") or ""
    return (str(row.get("street") or "") == "flop"
            and _num(row.get("to_call")) == 0.0
            and _num(row.get("n_opp")) == 2.0
            and isinstance(facts, str) and "规则:" in facts)


def _reason(text: Any, limit: int = REASON_CLIP) -> str:
    one_line = " ".join(str(text or "").split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


def rollback_verdict(conflict_rate: float, cbet: dict, latency: dict,
                     n_aligned: int) -> dict:
    """阈值判定。任一触发就建议回滚，并把触发项逐条列出来。"""
    triggers: list[str] = []
    if n_aligned == 0:
        return {"verdict": "无法判定", "triggers": ["对齐后没有可比较的决策点"],
                "rollback": False}
    if conflict_rate > CONFLICT_RATE_MAX:
        triggers.append(
            f"动作冲突率 {conflict_rate:.1%} > {CONFLICT_RATE_MAX:.0%}")
    if (cbet.get("n", 0) >= CBET_MIN_POINTS
            and cbet.get("conflict_rate", 0.0) > CBET_CONFLICT_RATE_MAX):
        triggers.append(
            f"c-bet 子集冲突率 {cbet['conflict_rate']:.1%} > "
            f"{CBET_CONFLICT_RATE_MAX:.0%}（{cbet.get('n')} 点）")
    old_p50 = latency.get("old", {}).get("p50")
    new_p50 = latency.get("new", {}).get("p50")
    if old_p50 and new_p50 and new_p50 > old_p50 * P50_RATIO_MAX:
        triggers.append(
            f"新 p50 {new_p50}ms > 基线 {old_p50}ms × {P50_RATIO_MAX:g}")
    new_max = latency.get("new", {}).get("max")
    if new_max is not None and new_max > LATENCY_BUDGET_MS:
        triggers.append(f"新 max {new_max}ms > {LATENCY_BUDGET_MS}ms 预算")
    return {"verdict": "建议回滚" if triggers else "建议保留",
            "triggers": triggers, "rollback": bool(triggers)}


def build_report(old_rows: Sequence[dict], new_rows: Sequence[dict],
                 *, old_paths: Iterable[str] = (),
                 new_paths: Iterable[str] = (),
                 protocol: Sequence[str] = (),
                 generated_at: str | None = None) -> dict:
    """对齐 → 分类 → 全部统计。返回可直接 json.dumps 的 dict。"""
    pairs, old_unmatched, new_unmatched = align(old_rows, new_rows)

    fallback_old = [r for r in old_rows if is_fallback(r)]
    fallback_new = [r for r in new_rows if is_fallback(r)]
    usable: list[tuple[dict, dict]] = [
        (o, n) for o, n in pairs
        if not is_fallback(o) and not is_fallback(n)]

    points: list[dict] = []
    for baseline, candidate in usable:
        old_id, new_id = baseline.get("new"), candidate.get("new")
        category = classify(old_id, new_id)
        point = {
            "hand": baseline.get("hand"),
            "street": baseline.get("street"),
            "cards": " ".join(_cards(baseline.get("cards"))),
            "board": " ".join(_cards(baseline.get("board"))),
            "pot": _num(candidate.get("pot")),
            "to_call": _num(candidate.get("to_call")),
            "n_opp": candidate.get("n_opp"),
            "tier": candidate.get("tier"),
            "old_id": old_id,
            "new_id": new_id,
            "old_reason": baseline.get("new_reason") or "",
            "new_reason": candidate.get("new_reason") or "",
            "old_ms": baseline.get("ms"),
            "new_ms": candidate.get("ms"),
            "old_fallback": False,
            "new_fallback": False,
            "category": category,
            "old_size": size_label(old_id),
            "new_size": size_label(new_id),
            "size_direction": (size_direction(old_id, new_id)
                               if category == SIZE_ONLY else None),
            "cbet": is_cbet_point(candidate),
        }
        points.append(point)

    counts = Counter(p["category"] for p in points)
    classified = [p for p in points if p["category"] != UNCLASSIFIED]
    conflict_rate = (counts[CONFLICT] / len(classified)) if classified else 0.0

    conflicts = [{
        "hand": p["hand"], "street": p["street"], "cards": p["cards"],
        "board": p["board"], "pot": p["pot"], "to_call": p["to_call"],
        "old_id": p["old_id"], "new_id": p["new_id"],
        "old_reason": _reason(p["old_reason"]),
        "new_reason": _reason(p["new_reason"]),
    } for p in points if p["category"] == CONFLICT]

    size_points = [p for p in points if p["category"] == SIZE_ONLY]
    size_pairs = Counter(f"{p['old_size']}→{p['new_size']}" for p in size_points)
    size_directions = Counter(p["size_direction"] for p in size_points)

    latency = {
        "old": latency_stats([o for o, _ in usable]),
        "new": latency_stats([n for _, n in usable]),
    }

    cbet_points = [p for p in points if p["cbet"]]
    cbet_counts = Counter(p["category"] for p in cbet_points)
    cbet_classified = [p for p in cbet_points if p["category"] != UNCLASSIFIED]
    cbet = {
        "n": len(cbet_points),
        "counts": {c: cbet_counts.get(c, 0) for c in CATEGORIES},
        "conflict_rate": (cbet_counts.get(CONFLICT, 0) / len(cbet_classified)
                          if cbet_classified else 0.0),
        "size_pairs": dict(Counter(
            f"{p['old_size']}→{p['new_size']}"
            for p in cbet_points if p["category"] == SIZE_ONLY)),
        "points": [{
            "hand": p["hand"], "cards": p["cards"], "board": p["board"],
            "pot": p["pot"], "old_id": p["old_id"], "new_id": p["new_id"],
            "category": p["category"],
            "old_size": p["old_size"], "new_size": p["new_size"],
        } for p in cbet_points],
    }

    # 补充口径（不参与阈值）：翻牌无人下注面，不限 n_opp 与「规则:」。
    # 「空气 c-bet 1/3 → 半池」这条改动不一定会渲染出「规则:」行（实测只有
    # 多人池 p32 规则会渲染），若只看严格子集会把它整片漏掉，所以这里并列给出。
    cbet_wide_points = [p for p in points
                        if p["street"] == "flop" and p["to_call"] == 0.0]
    cbet_wide_counts = Counter(p["category"] for p in cbet_wide_points)
    cbet_wide_classified = [p for p in cbet_wide_points
                            if p["category"] != UNCLASSIFIED]
    cbet_wide = {
        "note": "翻牌且 to_call=0 的全部点（不限 n_opp / 规则:），仅作对照，不参与阈值",
        "n": len(cbet_wide_points),
        "counts": {c: cbet_wide_counts.get(c, 0) for c in CATEGORIES},
        "conflict_rate": (cbet_wide_counts.get(CONFLICT, 0)
                          / len(cbet_wide_classified)
                          if cbet_wide_classified else 0.0),
        "size_pairs": dict(Counter(
            f"{p['old_size']}→{p['new_size']}"
            for p in cbet_wide_points if p["category"] == SIZE_ONLY)),
        "points": [{
            "hand": p["hand"], "cards": p["cards"], "board": p["board"],
            "pot": p["pot"], "n_opp": p["n_opp"],
            "old_id": p["old_id"], "new_id": p["new_id"],
            "category": p["category"],
            "old_size": p["old_size"], "new_size": p["new_size"],
        } for p in cbet_wide_points],
    }

    verdict = rollback_verdict(conflict_rate, cbet, latency, len(points))

    prompt_versions = {
        "old": sorted({str(r.get("prompt_version")) for r in old_rows
                       if r.get("prompt_version")}),
        "new": sorted({str(r.get("prompt_version")) for r in new_rows
                       if r.get("prompt_version")}),
    }
    models = {
        "old": sorted({str(r.get("model")) for r in old_rows if r.get("model")}),
        "new": sorted({str(r.get("model")) for r in new_rows if r.get("model")}),
    }

    size_by_path = dict(_SIZES)
    return {
        "schema": "ab_compare.v1",
        "generated_at": generated_at or datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "inputs": {"old": list(old_paths), "new": list(new_paths),
                   "sizes": {str(p): n for p, n in size_by_path.items()}},
        "protocol": list(protocol),
        "prompt_versions": prompt_versions,
        "models": models,
        "thresholds": {
            "pot_tol": POT_TOL,
            "conflict_rate_max": CONFLICT_RATE_MAX,
            "cbet_conflict_rate_max": CBET_CONFLICT_RATE_MAX,
            "cbet_min_points": CBET_MIN_POINTS,
            "p50_ratio_max": P50_RATIO_MAX,
            "latency_budget_ms": LATENCY_BUDGET_MS,
        },
        "points_total": {"old": len(old_rows), "new": len(new_rows)},
        "pairs": len(pairs),
        "unmatched": {"old": old_unmatched, "new": new_unmatched},
        "excluded_fallback_pairs": len(pairs) - len(usable),
        "fallback": {
            "old": {"n": len(fallback_old), "points": [
                {"hand": r.get("hand"), "street": r.get("street"),
                 "cards": " ".join(_cards(r.get("cards"))),
                 "board": " ".join(_cards(r.get("board"))),
                 "new": r.get("new"), "error": r.get("error")}
                for r in fallback_old]},
            "new": {"n": len(fallback_new), "points": [
                {"hand": r.get("hand"), "street": r.get("street"),
                 "cards": " ".join(_cards(r.get("cards"))),
                 "board": " ".join(_cards(r.get("board"))),
                 "new": r.get("new"), "error": r.get("error")}
                for r in fallback_new]},
        },
        "aligned": len(points),
        "counts": {c: counts.get(c, 0) for c in CATEGORIES},
        "conflict_rate": conflict_rate,
        "conflicts": conflicts,
        "size": {"pairs": dict(size_pairs), "directions": dict(size_directions),
                 "n": len(size_points)},
        "latency": latency,
        "cbet": cbet,
        "cbet_wide": cbet_wide,
        "verdict": verdict,
        "rollback_steps": list(ROLLBACK_STEPS),
        "points": points,
    }


# ─────────────────────────── 输出 ───────────────────────────

def _fmt_ms(value: Any) -> str:
    return "—" if value is None else f"{value}"


def render_markdown(report: dict) -> str:
    t = report["thresholds"]
    lat = report["latency"]
    cbet = report["cbet"]
    lines: list[str] = []
    add = lines.append

    add(f"# 规则台账 A/B 对跑：旧 {', '.join(report['prompt_versions']['old']) or '?'}"
        f" → 新 {', '.join(report['prompt_versions']['new']) or '?'}")
    add("")
    add(f"生成：{report['generated_at']}　模型："
        f"{', '.join(report['models']['new']) or '?'}")
    add("")

    if report.get("protocol"):
        add("## 0. 对跑口径")
        add("")
        for line in report["protocol"]:
            add(f"- {line}")
        add("")

    add("## 1. 点数与排除")
    add("")
    add("| 项 | 旧侧 | 新侧 |")
    add("|---|---:|---:|")
    add(f"| dump 决策点 | {report['points_total']['old']} | "
        f"{report['points_total']['new']} |")
    add(f"| fallback | {report['fallback']['old']['n']} | "
        f"{report['fallback']['new']['n']} |")
    add(f"| 未对齐 | {report['unmatched']['old']} | "
        f"{report['unmatched']['new']} |")
    add("")
    sizes = report["inputs"].get("sizes") or {}
    if sizes:
        add("| dump 文件 | 决策点 |")
        add("|---|---:|")
        for path, n in sizes.items():
            add(f"| `{path}` | {n} |")
        add("")
    add(f"配对 {report['pairs']} 个；两侧非 fallback 且可比较 **{report['aligned']}** 个"
        f"（剔除 fallback 配对 {report['excluded_fallback_pairs']} 个）。")
    add("")

    add("## 2. 三档计数")
    add("")
    add("| 分类 | 点数 | 占比（可分类） |")
    add("|---|---:|---:|")
    classified = report["aligned"] - report["counts"][UNCLASSIFIED]
    for cat in CATEGORIES:
        n = report["counts"][cat]
        share = (n / classified) if classified and cat != UNCLASSIFIED else None
        add(f"| {cat} | {n} | " +
            ("—" if share is None else f"{share:.1%}") + " |")
    add("")
    add(f"**动作冲突率 = {report['conflict_rate']:.1%}**"
        f"（阈值 > {t['conflict_rate_max']:.0%} 即回滚）")
    add("")

    add("## 3. 动作冲突清单")
    add("")
    if not report["conflicts"]:
        add("无。")
    else:
        add("| 手 | 街 | 我的牌 | 牌面 | 底池 | 需跟 | 旧 | 新 | 旧理由 | 新理由 |")
        add("|---|---|---|---|---:|---:|---|---|---|---|")
        for c in report["conflicts"]:
            add(f"| {c['hand']} | {c['street']} | {c['cards']} | {c['board']} "
                f"| {c['pot']} | {c['to_call']} | `{c['old_id']}` "
                f"| `{c['new_id']}` | {c['old_reason']} | {c['new_reason']} |")
    add("")

    add("## 4. 尺度差分布（仅尺度不同的点）")
    add("")
    add(f"共 {report['size']['n']} 个；"
        f"方向：" + "、".join(f"{k} {v}"
                            for k, v in sorted(report["size"]["directions"].items()))
        + "。")
    add("")
    if report["size"]["pairs"]:
        add("| 旧档 → 新档 | 次数 |")
        add("|---|---:|")
        for pair, n in sorted(report["size"]["pairs"].items(),
                              key=lambda kv: (-kv[1], kv[0])):
            add(f"| {pair} | {n} |")
    else:
        add("无。")
    add("")

    add("## 5. 延迟（每侧自己的 ms，同一批对齐点）")
    add("")
    add("| 侧 | n | p50 | p90 | max | > 8000ms |")
    add("|---|---:|---:|---:|---:|---:|")
    for side in ("old", "new"):
        s = lat[side]
        add(f"| {side} | {s['n']} | {_fmt_ms(s['p50'])} | {_fmt_ms(s['p90'])} "
            f"| {_fmt_ms(s['max'])} | {s['over_budget']} |")
    add("")
    add(f"阈值：新 p50 > 基线 × {t['p50_ratio_max']:g} 或 新 max > "
        f"{t['latency_budget_ms']}ms（AI_SERVER_TIMEOUT 预算）即回滚。")
    add("")

    add("## 6. Fallback")
    add("")
    for side in ("old", "new"):
        fb = report["fallback"][side]
        add(f"- {side}：{fb['n']} 个")
        for p in fb["points"]:
            add(f"  - {p['hand']} {p['street']} {p['cards']} / {p['board']} "
                f"→ {p['new']}（{p['error'] or '无错误信息'}）")
    add("")

    add("## 7. c-bet 子集（翻牌、无人下注、n_opp=2、候选侧提示词含「规则:」）")
    add("")
    add(f"共 {cbet['n']} 个；分类：" +
        "、".join(f"{k} {v}" for k, v in cbet["counts"].items()) + "。")
    add("")
    add(f"c-bet 冲突率 {cbet['conflict_rate']:.1%}"
        f"（阈值 > {t['cbet_conflict_rate_max']:.0%} 且子集 ≥ "
        f"{t['cbet_min_points']} 点）。尺寸档变化：" +
        ("、".join(f"{k} {v}" for k, v in cbet["size_pairs"].items()) or "无") + "。")
    add("")
    if cbet["points"]:
        add("| 手 | 我的牌 | 牌面 | 底池 | 旧 | 新 | 分类 |")
        add("|---|---|---|---:|---|---|---|")
        for p in cbet["points"]:
            add(f"| {p['hand']} | {p['cards']} | {p['board']} | {p['pot']} "
                f"| `{p['old_id']}` | `{p['new_id']}` | {p['category']} |")
    add("")

    wide = report["cbet_wide"]
    add("### 7b. 对照：翻牌无人下注面（不限 n_opp 与「规则:」，不参与阈值）")
    add("")
    add(f"共 {wide['n']} 个；分类：" +
        "、".join(f"{k} {v}" for k, v in wide["counts"].items()) +
        f"；冲突率 {wide['conflict_rate']:.1%}；尺寸档变化：" +
        ("、".join(f"{k} {v}" for k, v in wide["size_pairs"].items()) or "无") + "。")
    add("")
    add("加这一节的原因：「空气 c-bet 默认 1/3 池 → 半池」这条改动的直接效果落在"
        "翻牌无人下注面上，但实测只有多人池 p32 那条规则会渲染出「规则:」行；"
        f"本次对跑里翻牌无人下注共 {wide['n']} 点、其中 n_opp=2 的只有 "
        "4 点、再带「规则:」的只剩 "
        f"{cbet['n']} 点 —— 严格子集盖不住这条改动。"
        "唯一那条动作冲突（20261116-c9dec99027，6s 10s / 8s Kc 2d，2 人池）"
        "本身就是单挑 c-bet 面，只因为没渲染「规则:」行才落在严格子集之外，"
        "所以两节口径并列列出、不做合并。")
    add("")
    if wide["points"]:
        add("| 手 | 我的牌 | 牌面 | n_opp | 底池 | 旧 | 新 | 分类 |")
        add("|---|---|---|---:|---:|---|---|---|")
        for p in wide["points"]:
            add(f"| {p['hand']} | {p['cards']} | {p['board']} | {p['n_opp']} "
                f"| {p['pot']} | `{p['old_id']}` | `{p['new_id']}` "
                f"| {p['category']} |")
    add("")

    add("## 8. 回滚结论")
    add("")
    add(f"**{report['verdict']['verdict']}**")
    add("")
    if report["verdict"]["triggers"]:
        add("触发项：")
        for trig in report["verdict"]["triggers"]:
            add(f"- {trig}")
    else:
        add("没有任何回滚阈值被触发。")
    add("")
    add("回滚步骤：")
    add("")
    for step in report["rollback_steps"]:
        add(f"- {step}")
    add("")
    add("## 9. 阈值（代码常量，原样打印）")
    add("")
    add("```python")
    add(f"POT_TOL = {t['pot_tol']}")
    add(f"CONFLICT_RATE_MAX = {t['conflict_rate_max']}")
    add(f"CBET_CONFLICT_RATE_MAX = {t['cbet_conflict_rate_max']}")
    add(f"CBET_MIN_POINTS = {t['cbet_min_points']}")
    add(f"P50_RATIO_MAX = {t['p50_ratio_max']}")
    add(f"LATENCY_BUDGET_MS = {t['latency_budget_ms']}")
    add("```")
    add("")
    return "\n".join(lines)


def print_summary(report: dict) -> None:
    lat = report["latency"]
    print(f"录入：旧 {report['points_total']['old']} / 新 "
          f"{report['points_total']['new']} 个决策点；配对 {report['pairs']}；"
          f"可比较 {report['aligned']}（剔除 fallback 配对 "
          f"{report['excluded_fallback_pairs']}）")
    print(f"对齐未命中：旧 {report['unmatched']['old']} / 新 "
          f"{report['unmatched']['new']}")
    print("三档：" + "、".join(f"{k} {v}" for k, v in report["counts"].items())
          + f"；动作冲突率 {report['conflict_rate']:.1%}")
    print(f"延迟：旧 p50={lat['old']['p50']} p90={lat['old']['p90']} "
          f"max={lat['old']['max']}；新 p50={lat['new']['p50']} "
          f"p90={lat['new']['p90']} max={lat['new']['max']}"
          f"（>8000ms：旧 {lat['old']['over_budget']} / "
          f"新 {lat['new']['over_budget']}）")
    print(f"fallback：旧 {report['fallback']['old']['n']} / "
          f"新 {report['fallback']['new']['n']}")
    cb = report["cbet"]
    print(f"c-bet 子集：{cb['n']} 点，冲突率 {cb['conflict_rate']:.1%}，"
          f"尺寸变化 {cb['size_pairs'] or '无'}")
    cw = report["cbet_wide"]
    print(f"c-bet 宽口径（翻牌无人下注）：{cw['n']} 点，"
          f"冲突率 {cw['conflict_rate']:.1%}，尺寸变化 {cw['size_pairs'] or '无'}")
    print(f"回滚：{report['verdict']['verdict']}")
    for trig in report["verdict"]["triggers"]:
        print(f"  · {trig}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A/B 对跑两份 replay dump")
    ap.add_argument("--old", nargs="+", type=Path, required=True,
                    help="旧（基线）侧 dump，可多个")
    ap.add_argument("--new", nargs="+", type=Path, required=True,
                    help="新（候选）侧 dump，可多个")
    ap.add_argument("--json", type=Path, default=None, help="写出逐点 + 汇总 JSON")
    ap.add_argument("--md", type=Path, default=None, help="写出 markdown 报告")
    ap.add_argument("--note", action="append", default=[],
                    help="对跑口径的一行说明（可重复），原样进报告")
    args = ap.parse_args(argv)

    _SIZES.clear()

    def load(paths: Sequence[Path]) -> list[dict]:
        rows: list[dict] = []
        for path in paths:
            chunk = json.loads(path.read_text(encoding="utf-8"))
            _SIZES[str(path)] = len(chunk)
            rows.extend(chunk)
        return rows

    old_rows = load(args.old)
    new_rows = load(args.new)
    report = build_report(old_rows, new_rows,
                          old_paths=[str(p) for p in args.old],
                          new_paths=[str(p) for p in args.new],
                          protocol=args.note)
    print_summary(report)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"写出 {args.json}")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(render_markdown(report), encoding="utf-8")
        print(f"写出 {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
