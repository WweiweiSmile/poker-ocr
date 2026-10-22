#!/usr/bin/env python3
"""回放「大幅盈利/亏损」的手牌，对比 lean 与 skills 两套建议提示词。

筛选口径（默认）：

  · `hands.json` 里 hero 拿到两张手牌（我在场）；
  · 这手牌的 **hero 净胜负** |net| ≥ `--min-abs`（默认 10BB）。

净胜负来自 `logs/live.states.jsonl` 的 `state` 记录：同一 session 内按 hand 记录
切窗口，用 hero 的 `stack` 链式相减（上一手结算后的筹码 → 这一手结算后的筹码）。
这是**估算**：边界帧/丢帧会带来约 1–2BB 误差；日志里已有 `hero_net` 时优先用它。

对每个 hero 决策点（翻前 + 翻后），分别用：

    advise(..., style="lean")    # 契约+事实+现代范围框架
    advise(..., style="skills")  # 旧 2005 技能库

各调一次模型，记录选择、理由、耗时，并与**实际动作**对比。比较口径：

  一致     动作类别相同（bet/raise 再看尺度差是否超过 1/3 池）
  尺度差   两边都是下注/加注，只是尺度不同
  动作不同 动作类别不同

汇总指标：

  · 两版一致率、各自与实际动作的一致率；
  · **结果启发式**（仅参考）：大幅盈利的手里「跟实际一致」加分，大幅亏损的手里
    「偏离实际」加分 —— 实际动作不等于最优，这只是一个方向性信号；
  · 可选 `--judge`：把两版建议盲测成 A/B，让模型判哪版更好（多一次模型调用）。

用法：

    # 先看会回放哪些手、哪些决策点（不调模型、不花钱）
    .venv/bin/python tools/compare_advice_styles.py --dry-run

    # 真跑（默认最多 10 个决策点 = 20 次模型调用）
    .venv/bin/python tools/compare_advice_styles.py --limit 10

    # 加盲测判官、输出 markdown
    .venv/bin/python tools/compare_advice_styles.py --limit 10 --judge

    # 只看赢/只看输
    ... --wins-only
    ... --losses-only
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.advice import (  # noqa: E402
    AISettings, DEFAULT_PROMPT_STYLE, PROMPT_STYLES, advise, build_situation,
    complete_json, render_hand_facts,
)
from poker_ocr.env import load_env  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord, street_of_board,
)
from poker_ocr.parse import Blinds  # noqa: E402

STREETS = ("preflop", "flop", "turn", "river")
BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
AGGRESSIVE = ("bet", "raise", "allin")


# ─────────────────────────── 数据 ───────────────────────────

@dataclass
class HandResult:
    uid: str
    net: float | None
    start_stack: float | None
    end_stack: float | None
    states: list[dict] = field(default_factory=list)
    source: str = "state_log"


@dataclass
class Decision:
    hand: dict
    result: HandResult
    street: str
    pot: float
    pot_start: float
    to_call: float
    hero_stack: float | None
    actual: str | None
    actual_amount: float | None
    snap: HandSnapshot
    index: int = 0


def load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"  [警告] {path}:{lineno} JSON 解析失败：{exc.msg}")
                continue
            if isinstance(record, dict):
                out.append(record)
    return out


def group_sessions(records: list[dict]
                   ) -> list[tuple[int, list[dict], list[dict]]]:
    """按 session_start 切段，返回 [(session_no, hands, states)]。"""
    sessions: list[tuple[int, list[dict], list[dict]]] = []
    n = 0
    hands: list[dict] = []
    states: list[dict] = []
    for rec in records:
        kind = rec.get("type")
        if kind == "session_start":
            if hands or states:
                sessions.append((n, hands, states))
            n += 1
            hands, states = [], []
        elif kind == "hand":
            hands.append(rec)
        elif kind == "state" and rec.get("hero"):
            states.append(rec)
    if hands or states:
        sessions.append((n, hands, states))
    return sessions


def _ts(rec: dict) -> str:
    return str(rec.get("ts") or rec.get("started_at") or "")


def estimate_result(hand: dict, prev_end: float | None,
                    pool: list[dict]) -> HandResult:
    """用 state 里的 hero.stack 估算这一手的净胜负（±1–2BB）。"""
    cards = hand.get("hero_cards") or []
    # 只认「hero 手牌对得上」的 state。对不上通常是上一手/下一手的边界帧：
    # 拿它当本手的结束筹码会造出「+59.5BB」这种假胜负。
    if cards:
        matched = [s for s in pool if s.get("hero_cards") == cards]
    else:
        matched = pool
    if not matched:
        return HandResult(str(hand.get("uid")), None, None, None, [])
    stacks = [s.get("hero", {}).get("stack") for s in matched]
    stacks = [float(v) for v in stacks if v is not None]
    end = stacks[-1] if stacks else None
    start = prev_end if prev_end is not None else (stacks[0] if stacks else None)
    net = round(end - start, 2) if end is not None and start is not None else None
    # 起始筹码 0 = 上一手刚输光/重新买入，链式相减会变成假盈利
    if start is not None and start <= 0:
        net = None
    # 未来的日志会直接写 hero_net；有就用它，别被估算误差影响筛选
    if isinstance(hand.get("hero_net"), (int, float)):
        net = float(hand["hero_net"])
        return HandResult(str(hand.get("uid")), net, start, end, matched, "hand_record")
    return HandResult(str(hand.get("uid")), net, start, end, matched)


def build_results(hands: list[dict], states: list[dict]) -> dict[str, HandResult]:
    hands = sorted(hands, key=_ts)
    states = sorted(states, key=_ts)
    out: dict[str, HandResult] = {}
    prev_ts = ""
    prev_end: float | None = None
    for hand in hands:
        ts = _ts(hand)
        pool = [s for s in states if prev_ts < _ts(s) <= ts]
        res = estimate_result(hand, prev_end, pool)
        out[res.uid] = res
        prev_ts = ts
        if res.end_stack is not None:
            prev_end = res.end_stack
    return out


# ─────────────────────────── 决策点 ───────────────────────────

def _cards(board) -> tuple[str, ...]:
    return tuple(board or ())


def _nearest_state(states: list[dict], street: str,
                   pot: float) -> dict | None:
    best = None
    best_d = 1e9
    for s in states:
        # state 记录没有 street 字段：用公共牌张数推（preflop/flop/turn/river）
        if street_of_board(len(s.get("board") or ())) != street:
            continue
        p = s.get("pot")
        if p is None:
            continue
        d = abs(float(p) - pot)
        if d < best_d:
            best, best_d = s, d
    return best if best_d <= 1.5 else None


def _opponents(hand: dict, state: dict | None,
               commit: dict[str, float]) -> tuple[OpponentSnapshot, ...]:
    if state and state.get("opponents"):
        out = []
        for o in state["opponents"]:
            sid = o.get("seat_id")
            if not sid:
                continue
            out.append(OpponentSnapshot(
                seat_id=sid, name=o.get("name"),
                position=o.get("position"), stack=o.get("stack"),
                bet=o.get("bet"),
                folded=(o.get("fold") is True or o.get("status") == "folded"),
            ))
        return tuple(out)
    # 没有同帧 state 时至少把行动线上出现过的人带上（没有筹码）
    meta: dict[str, dict] = {}
    for s in hand.get("streets") or []:
        for a in s.get("actions") or []:
            sid = a.get("seat_id")
            if sid and not a.get("is_hero"):
                meta[sid] = a
    return tuple(OpponentSnapshot(
        seat_id=sid, name=a.get("name"), position=a.get("position"),
        stack=None, bet=commit.get(sid, 0.0), folded=False,
    ) for sid, a in meta.items())


def _shapes(actions: list[ActionRecord], hero_is_pfr: bool) -> tuple[str, ...]:
    raises = sum(1 for a in actions if a.action in ("bet", "raise"))
    if raises == 0:
        return ("limped",) if any(a.action == "call" for a in actions) \
            else ("unopened",)
    if raises == 1:
        return ("single-raise",)
    return ("three-bet",)


def build_decisions(hand: dict, result: HandResult) -> list[Decision]:
    """按行动线重放，每个 hero 动作之前生成一个决策点。"""
    blinds_raw = hand.get("blinds") or {}
    sb = float(blinds_raw.get("sb") or 0.0)
    bb = float(blinds_raw.get("bb") or 1.0)
    ante = blinds_raw.get("ante")
    ante = float(ante) if ante else 0.0
    table = int(hand.get("table_size") or 0)
    blinds = Blinds(sb, bb, ante or None,
                    str(blinds_raw.get("raw") or f"{sb}/{bb}")) if bb else None
    hero_pos = hand.get("hero_position") or None
    cards = tuple(hand.get("hero_cards") or ())
    final_board = tuple(hand.get("board") or ())
    pot = sb + bb + ante * max(table, 1)
    hero_invested = 0.0
    built: list[StreetRecord] = []
    out: list[Decision] = []
    start_stack = result.start_stack

    for rec in hand.get("streets") or []:
        street = rec.get("street")
        if street not in STREETS:
            continue
        if rec.get("pot_start") is not None:
            pot = float(rec["pot_start"])
        pot_start = pot
        level = bb if street == "preflop" else 0.0
        commit: dict[str, float] = {}
        if street == "preflop":
            # 盲注不是行动，但决定了 to_call；hero 的起始 stack 已经扣过盲注
            if hero_pos == "SB":
                commit["hero"] = sb
            elif hero_pos == "BB":
                commit["hero"] = bb
        cur: list[ActionRecord] = []

        for a in rec.get("actions") or []:
            act = str(a.get("action") or "")
            is_hero = bool(a.get("is_hero"))
            amount = a.get("amount")
            if is_hero:
                hero_bet = commit.get("hero", 0.0)
                to_call = max(level - hero_bet, 0.0)
                vals = sorted(commit.values(), reverse=True)
                second = vals[1] if len(vals) > 1 else None
                state = _nearest_state(result.states, street, pot)
                # 同一帧的 state 是现场事实：pot / to_call / 各家筹码优先用它，
                # 行动线重算只用来还原「到这一步为止」的历史。
                snap_pot, snap_pbs = pot, pot_start
                if state is not None:
                    hero_stack = state.get("hero", {}).get("stack")
                    if state.get("pot") is not None:
                        snap_pot = float(state["pot"])
                    if state.get("pot_before_street") is not None:
                        snap_pbs = float(state["pot_before_street"])
                    hb = (state.get("hero") or {}).get("bet") or 0.0
                    top = max([hb] + [float(o.get("bet") or 0.0)
                                      for o in state.get("opponents") or []])
                    to_call = max(top - hb, 0.0)
                    bets = sorted({float(o.get("bet") or 0.0)
                                   for o in state.get("opponents") or []},
                                  reverse=True)
                    second = bets[1] if len(bets) > 1 else None
                else:
                    hero_stack = (round(start_stack - hero_invested, 2)
                                  if start_stack is not None else None)
                board = _cards(final_board)[:BOARD_LEN[street]]
                hist = tuple(built) + (
                    StreetRecord(street, pot_start, tuple(cur)),)
                pre_actions = [x for s in hist for x in s.actions]
                hero_is_pfr = any(x.is_hero and x.action in ("bet", "raise")
                                  for x in pre_actions if x.street == "preflop")
                snap = HandSnapshot(
                    hand_id=str(hand.get("uid") or hand.get("hand_id")),
                    hand_index=len(out) + 1,
                    street=street, board=board, hero_cards=cards,
                    hero_position=hero_pos,
                    hero_stack=round(hero_stack, 2) if hero_stack is not None else None,
                    hero_bet=hero_bet,
                    hero_invested=round(hero_invested, 2),
                    to_call=round(to_call, 2), second_bet=second,
                    pot=round(snap_pot, 2),
                    pot_before_street=round(snap_pbs, 2),
                    table_size=table, blinds=blinds,
                    opponents=_opponents(hand, state, commit),
                    streets=hist,
                    preflop_shapes=_shapes(
                        [x for x in pre_actions if x.street == "preflop"],
                        hero_is_pfr),
                    hero_is_pfr=hero_is_pfr,
                    cbet_spot=(street == "flop" and hero_is_pfr
                               and to_call == 0),
                    decision_key=(str(hand.get("uid")), street, len(out)),
                    players_dealt=int((state or {}).get("players_in_hand")
                                      or max(table, 1)),
                )
                out.append(Decision(
                    hand=hand, result=result, street=street, pot=snap_pot,
                    pot_start=snap_pbs, to_call=to_call, hero_stack=hero_stack,
                    actual=act, actual_amount=amount, snap=snap,
                    index=len(out),
                ))

            delta = 0.0
            if act in ("bet", "raise", "allin") and amount is not None:
                amount = float(amount)
                delta = max(amount - commit.get(a.get("seat_id"), 0.0), 0.0)
                level = amount
                commit[a.get("seat_id")] = amount
            elif act == "call":
                delta = max(level - commit.get(a.get("seat_id"), 0.0), 0.0)
                commit[a.get("seat_id")] = level
            pot += delta
            if is_hero:
                hero_invested += delta
            cur.append(ActionRecord(
                street=street, seat_id=str(a.get("seat_id") or ""),
                name=a.get("name"), position=a.get("position"),
                action=act, amount=amount, all_in=bool(a.get("all_in")),
                is_hero=is_hero, inferred=bool(a.get("inferred")),
            ))
        built.append(StreetRecord(street, pot_start, tuple(cur)))
    return out


# ─────────────────────────── 对比 ───────────────────────────

def classify(option, actual: str | None, actual_amount: float | None,
             pot: float) -> str:
    """一致 / 尺度差 / 动作不同。actual 为空时返回 unknown。"""
    if not actual:
        return "unknown"
    kind = option.kind
    act = "raise" if actual == "allin" else actual
    if kind == act or (actual == "allin" and kind in ("bet", "raise")):
        if kind in ("bet", "raise") and option.amount is not None \
                and actual_amount is not None:
            if abs(float(option.amount) - float(actual_amount)) > max(pot / 3, 1.0):
                return "尺度差"
        return "一致"
    if kind in ("bet", "raise") and act in ("bet", "raise"):
        return "尺度差"
    return "动作不同"


def run_decision(dec: Decision, settings: AISettings, style: str,
                 timeout: float) -> dict:
    sit = build_situation(dec.snap)
    started = time.monotonic()
    try:
        advice = advise(sit, settings, timeout=timeout, style=style)
    except Exception as exc:                     # noqa: BLE001
        return {"style": style, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "ms": int((time.monotonic() - started) * 1000)}
    return {
        "style": style, "ok": advice.source == "model",
        "option": advice.option.id, "kind": advice.option.kind,
        "amount": advice.option.amount, "label": advice.option.label,
        "reason": advice.reason, "source": advice.source,
        "error": advice.error, "ms": advice.duration_ms,
        "actual": dec.actual, "actual_amount": dec.actual_amount,
        "match": classify(advice.option, dec.actual, dec.actual_amount, dec.pot),
        "hand_id": dec.snap.hand_id, "street": dec.street,
        "net": dec.result.net, "pot": dec.pot, "to_call": dec.to_call,
    }


JUDGE_SYSTEM = """\
You are a poker coach reviewing two advisor recommendations for the same decision.
They are blinded as A and B. Judge **decision quality and long-run EV**, not the
outcome of this particular hand. Use ranges, board texture, pot odds and the
user's table dynamics. Return one JSON object: {"better": "A"|"B"|"tie",
"why": "<one sentence, under 25 words>"}."""


def judge_pair(sit, a: dict, b: dict, actual: str | None,
               net: float | None, settings: AISettings, timeout: float,
               rng: random.Random) -> dict:
    """盲测：随机把两版建议标成 A/B，让模型判哪版更好。"""
    swap = rng.random() < 0.5
    left, right = (b, a) if swap else (a, b)
    facts = render_hand_facts(sit)
    user = (
        f"{facts}\n\n## Recommendations\n"
        f"A: {left.get('label')} — {left.get('reason')}\n"
        f"B: {right.get('label')} — {right.get('reason')}\n\n"
        f"Actual action: {actual or 'unknown'}\n"
        f"Hand net result: {net if net is not None else 'unknown'}BB\n"
    )
    try:
        data = complete_json(settings, JUDGE_SYSTEM, user, timeout=timeout)
    except Exception as exc:                     # noqa: BLE001
        return {"better": "error", "why": f"{type(exc).__name__}: {exc}"}
    better = str(data.get("better", "")).strip().lower()
    if better not in ("a", "b", "tie"):
        return {"better": "error", "why": f"判官返回非法值：{better!r}"}
    style = None
    if better in ("a", "b"):
        picked = left if better == "a" else right
        style = picked.get("style")
    return {"better": style or "tie", "why": str(data.get("why", "")).strip()}


def heuristic_score(rows: list[dict], min_abs: float) -> dict[str, int]:
    """结果启发式：大赢跟实际一致加分；大亏偏离实际加分。仅方向性参考。"""
    score = {"lean": 0, "skills": 0}
    for row in rows:
        a, b = row["results"].get("lean"), row["results"].get("skills")
        if not a or not b or not a.get("ok") or not b.get("ok"):
            continue
        if a.get("option") == b.get("option"):
            continue
        net = row.get("net")
        if net is None or abs(net) < min_abs:
            continue
        for style, res in (("lean", a), ("skills", b)):
            if net > 0 and res.get("match") == "一致":
                score[style] += 1
            elif net < 0 and res.get("match") in ("动作不同", "尺度差"):
                score[style] += 1
    return score


# ─────────────────────────── 输出 ───────────────────────────

def render_report(rows: list[dict], skipped: list[dict], args,
                  n_hands: int, summary: dict) -> str:
    lines = [
        "# lean vs skills 回放对比",
        "",
        f"- 输入：`{args.hands}`，状态日志：`{args.log}`",
        f"- 筛选：hero 在场、|净胜负| ≥ {args.min_abs:g}BB"
        + ("，只看盈利" if args.wins_only else "，只看亏损" if args.losses_only else ""),
        f"- 手牌 {n_hands} 手，决策点 {len(rows) + len(skipped)} 个，"
        f"有效 {len(rows)} 个",
        "",
        "## 汇总",
        "",
    ]
    for line in summary.get("lines", []):
        lines.append(f"- {line}")
    if summary.get("judge"):
        lines.append(f"- 判官：{summary['judge']}")
    lines += [
        "",
        "> 净胜负由 state 日志的 hero 筹码链式估算（±1–2BB）；实际动作不等于最优，"
        "结果启发式只作方向性参考。",
        "",
        "## 明细",
        "",
        "| 手 | 街 | 净胜负 | 实际 | lean | skills | lean 对比 | skills 对比 | 判官 |",
        "|---|---|---:|---|---|---|---|---|---|",
    ]
    for row in rows:
        a = row["results"].get("lean") or {}
        b = row["results"].get("skills") or {}
        def cell(res):
            if not res.get("ok"):
                return f"失败({res.get('error') or res.get('source') or '?'})"
            return f"{res.get('label')}<br><small>{res.get('reason','')}</small>"
        lines.append(
            f"| {row['hand_id']} | {row['street']} | "
            f"{row.get('net') if row.get('net') is not None else '?'} | "
            f"{row.get('actual') or '?'} | {cell(a)} | {cell(b)} | "
            f"{a.get('match','?')} | {b.get('match','?')} | "
            f"{row.get('judge', {}).get('better', '')} |")
    if skipped:
        lines += ["", "## 跳过的决策点", ""]
        for item in skipped:
            lines.append(f"- {item['hand_id']} {item['street']}：{item['reason']}")
    return "\n".join(lines) + "\n"


# ─────────────────────────── 主流程 ───────────────────────────

def _settings_from_env() -> tuple[AISettings | None, str]:
    env = load_env()
    source = "dotenv" if "AI_SERVER_API_KEY" in env.applied else "env"
    try:
        return AISettings.from_env(source=source), source
    except ValueError as exc:
        print(f"AI 配置有误：{exc}")
        return None, source


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="筛选大幅盈利/亏损的手牌，回放对比 lean 与 skills 提示词")
    ap.add_argument("--hands", type=Path, default=Path("hands.json"),
                    help="导出的完整手牌 JSON（默认 hands.json）")
    ap.add_argument("--log", type=Path, default=Path("logs/live.states.jsonl"),
                    help="原始状态日志（默认 logs/live.states.jsonl）")
    ap.add_argument("--min-abs", type=float, default=10.0,
                    help="净胜负绝对值下限（BB，默认 10）")
    ap.add_argument("--wins-only", action="store_true", help="只看盈利的手")
    ap.add_argument("--losses-only", action="store_true", help="只看亏损的手")
    ap.add_argument("--limit", type=int, default=10,
                    help="最多回放多少个决策点（默认 10，0=不限）")
    ap.add_argument("--max-hands", type=int, default=0,
                    help="最多取多少手（默认 0=不限）")
    ap.add_argument("--timeout", type=float, default=None,
                    help="单次模型超时秒数（默认读 AI_SERVER_TIMEOUT/20）")
    ap.add_argument("--temperature", type=float, default=None,
                    help="覆盖采样温度（默认沿用 .env；A/B 建议设 0 减少抖动）")
    ap.add_argument("--judge", action="store_true",
                    help="两版不同时，额外调一次模型做盲测判官")
    ap.add_argument("--seed", type=int, default=7, help="判官 A/B 随机种子")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印筛选结果和决策点，不调模型")
    ap.add_argument("--out", type=Path,
                    default=Path("logs/compare_advice_styles.md"),
                    help="markdown 报告路径")
    ap.add_argument("--json", type=Path, default=None,
                    help="可选：把逐决策点明细写成 JSON")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.wins_only and args.losses_only:
        ap.error("--wins-only 和 --losses-only 不能同时用")

    if not args.hands.exists():
        ap.error(f"找不到 {args.hands}：先跑 tools/export_live_hands.py")
    hands = json.loads(args.hands.read_text(encoding="utf-8"))
    if not isinstance(hands, list):
        ap.error(f"{args.hands} 不是 JSON 数组")
    records = load_jsonl(args.log) if args.log.exists() else []
    sessions = {n: (h, s) for n, h, s in group_sessions(records)}

    # 用 hands.json 里的 hand 记录（带 uid/session）配 state 日志切窗口。
    by_session: dict[int, list[dict]] = {}
    for hand in hands:
        by_session.setdefault(int(hand.get("session") or 0), []).append(hand)
    all_results: dict[str, HandResult] = {}
    for session_no, sess_hands in by_session.items():
        _log_hands, states = sessions.get(session_no, ([], []))
        all_results.update(build_results(sess_hands, states))

    selected: list[tuple[dict, HandResult]] = []
    for hand in hands:
        uid = str(hand.get("uid") or "")
        res = all_results.get(uid)
        if res is None:
            continue
        net = res.net
        if net is None or abs(net) < args.min_abs:
            continue
        if args.wins_only and net <= 0:
            continue
        if args.losses_only and net >= 0:
            continue
        if len(hand.get("hero_cards") or ()) != 2:
            continue
        selected.append((hand, res))

    selected.sort(key=lambda item: abs(item[1].net or 0), reverse=True)
    if args.max_hands:
        selected = selected[:args.max_hands]

    decisions: list[Decision] = []
    for hand, res in selected:
        decisions.extend(build_decisions(hand, res))
    if args.limit:
        decisions = decisions[:args.limit]

    print(f"筛选：{len(selected)} 手，{len(decisions)} 个决策点")
    for hand, res in selected:
        print(f"  {res.uid:8s} net={res.net:+.1f}BB  cards="
              f"{''.join(hand.get('hero_cards') or [])}  "
              f"{hand.get('hero_position')}  board={''.join(hand.get('board') or [])}")
    for dec in decisions:
        print(f"    - {dec.snap.hand_id} {dec.street:7s} pot={dec.pot:g} "
              f"to_call={dec.to_call:g} actual={dec.actual}")
    if args.dry_run:
        print("dry-run：未调用模型")
        return 0

    settings, source = _settings_from_env()
    if settings is None:
        ap.error("没有可用的 AI 配置（AI_SERVER_API_KEY / BASE_URL / MODEL）")
    if args.temperature is not None:
        settings = replace(settings, temperature=args.temperature)
    timeout = args.timeout
    if timeout is None:
        raw = (os.environ.get("AI_SERVER_TIMEOUT") or "").strip()
        try:
            timeout = float(raw) if raw else 20.0
        except ValueError:
            timeout = 20.0
    print(f"模型 {settings.model}（{source}），超时 {timeout:g}s，"
          f"风格 {', '.join(PROMPT_STYLES)}")

    rng = random.Random(args.seed)
    rows: list[dict] = []
    skipped: list[dict] = []
    judge_tally = {"lean": 0, "skills": 0, "tie": 0}
    for i, dec in enumerate(decisions, 1):
        print(f"[{i}/{len(decisions)}] {dec.snap.hand_id} {dec.street} "
              f"pot={dec.pot:g} to_call={dec.to_call:g} …", flush=True)
        sit = build_situation(dec.snap)
        results: dict[str, dict] = {}
        for style in PROMPT_STYLES:
            res = run_decision(dec, settings, style, timeout)
            results[style] = res
            status = res.get("option") if res.get("ok") else "失败"
            print(f"    {style:6s} → {status} ({res.get('ms', 0)}ms)")
        ok = [r for r in results.values() if r.get("ok")]
        if not ok:
            skipped.append({"hand_id": dec.snap.hand_id,
                            "street": dec.street,
                            "reason": "两版都失败/兜底"})
            continue
        row = {
            "hand_id": dec.snap.hand_id, "street": dec.street,
            "net": dec.result.net, "actual": dec.actual,
            "actual_amount": dec.actual_amount, "pot": dec.pot,
            "to_call": dec.to_call, "results": results,
        }
        if args.judge and len(ok) == 2 and results["lean"].get("option") \
                != results["skills"].get("option"):
            verdict = judge_pair(sit, results["lean"], results["skills"],
                                 dec.actual, dec.result.net, settings,
                                 timeout, rng)
            row["judge"] = verdict
            judge_tally[verdict["better"]] = \
                judge_tally.get(verdict["better"], 0) + 1
            print(f"    judge  → {verdict['better']}：{verdict['why']}")
        rows.append(row)

    score = heuristic_score(rows, args.min_abs)
    same_style = sum(
        1 for r in rows
        if r["results"]["lean"].get("ok") and r["results"]["skills"].get("ok")
        and r["results"]["lean"].get("option")
        == r["results"]["skills"].get("option"))
    stats = {
        "decisions": len(rows),
        "styles_same": same_style,
        "same_actual": {s: sum(1 for r in rows
                               if r["results"].get(s, {}).get("match") == "一致")
                        for s in PROMPT_STYLES},
        "avg_ms": {s: round(sum(r["results"].get(s, {}).get("ms", 0)
                                for r in rows) / max(len(rows), 1))
                   for s in PROMPT_STYLES},
    }
    lines = [
        f"两版选择相同 {same_style}/{len(rows)}；"
        f"与实际一致：lean {stats['same_actual']['lean']}，"
        f"skills {stats['same_actual']['skills']}",
        f"平均耗时：lean {stats['avg_ms']['lean']}ms，"
        f"skills {stats['avg_ms']['skills']}ms",
        f"结果启发式（仅参考）：lean {score['lean']:+d}，"
        f"skills {score['skills']:+d}",
    ]
    summary = {"lines": lines}
    if args.judge:
        summary["judge"] = (f"lean {judge_tally['lean']} / "
                            f"skills {judge_tally['skills']} / "
                            f"tie {judge_tally['tie']}")
    report = render_report(rows, skipped, args, len(selected), summary)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"→ {args.out}")
    if args.json:
        args.json.write_text(json.dumps(
            {"summary": stats | {"heuristic": score, "judge": judge_tally},
             "rows": rows, "skipped": skipped},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"→ {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
