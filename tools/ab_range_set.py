#!/usr/bin/env python3
"""A/B 集：对手范围事件模型（旧路径 vs 新路径）。

清单在 `tools/ab_set.json`，每条形如：

    {id, priority, hand, street, action_index, category, source,
     status, expect, pot, to_call, threshold, old_equity, new_equity,
     old_advice, new_advice, note}

`status`：

  · `ready`：本地有手牌、可回放；默认模式会重算旧/新权益并核对 `expect`；
  · `blocked_*` / `manual` / `needs_reconstruction`：只打印原因，不参与判定。

    # 只校验清单与本地手牌
    .venv/bin/python tools/ab_range_set.py --check

    # 跑 ready 用例（事实层，不调模型）
    .venv/bin/python tools/ab_range_set.py
    .venv/bin/python tools/ab_range_set.py --priority P0

    # 同时跑旧/新模型（慢，需要 AI 凭据）
    .venv/bin/python tools/ab_range_set.py --model --priority P0

    # 落盘
    .venv/bin/python tools/ab_range_set.py --json /tmp/ab_set.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr import board_range, hand_vs_ranges  # noqa: E402
from poker_ocr.advice import (  # noqa: E402
    AISettings, advise, build_situation,
)
from tools.replay_hand_outbox import (  # noqa: E402
    _table_stats, build_snap, load_hand,
)

SET_PATH = ROOT / "tools" / "ab_set.json"
OUTBOX = ROOT / "data" / "hand_outbox" / "pending"
TOL = 0.03


def load_set() -> dict:
    return json.loads(SET_PATH.read_text(encoding="utf-8"))


def hand_path(hand_id: str) -> Path:
    return OUTBOX / f"{hand_id}.json"


def find_advice(hand: dict, case: dict) -> dict | None:
    """按 street + action_index 找 advice；action_index 为空时用 pot/to_call 消歧。"""
    advs = [a for a in hand.get("advices") or []
            if a.get("street") == case.get("street")]
    idx = case.get("action_index")
    if idx is not None:
        advs = [a for a in advs if a.get("action_index") == idx]
    if not advs:
        return None
    pot, call = case.get("pot"), case.get("to_call")
    if pot is not None:
        exact = [a for a in advs
                 if abs((a.get("pot") or -1) - pot) < 1e-6
                 and abs((a.get("to_call") or -1) - call) < 1e-6]
        if exact:
            return exact[0]
    return advs[0]


def situation_at(hand: dict, adv: dict, flag: str):
    os.environ["RANGE_EVENT_MODEL"] = flag
    hand_vs_ranges.reset_cache()
    board_range.reset_cache()
    snap = build_snap(hand, adv)
    sit = build_situation(snap, table_dynamics="", opponent_reads=(),
                          table_stats=_table_stats(adv))
    return sit, snap


def equity_of(sit) -> float | None:
    hr = getattr(sit, "hand_ranges", None)
    return None if hr is None else hr.equity


def crossing_ok(expect: str, old: float, new: float, thr: float) -> bool:
    if expect == "cross_down":
        return old > thr >= new
    if expect == "cross_up":
        return new > thr >= old
    if expect == "stay_above":
        return old > thr and new > thr
    if expect == "stay_below":
        return old <= thr and new <= thr
    return True


def check_manifest(data: dict) -> list[str]:
    problems: list[str] = []
    ids: set[str] = set()
    for case in data.get("cases") or []:
        cid = case.get("id")
        if not cid or cid in ids:
            problems.append(f"重复/缺失 id：{case}")
            continue
        ids.add(cid)
        for key in ("hand", "street", "status"):
            if key not in case:
                problems.append(f"{cid}: 缺 {key}")
        if case.get("status") != "ready":
            continue
        path = hand_path(case["hand"])
        if not path.exists():
            problems.append(f"{cid}: 找不到 {path}")
            continue
        hand = load_hand(path)
        adv = find_advice(hand, case)
        if adv is None:
            problems.append(f"{cid}: 手牌里找不到对应 advice")
            continue
        for key in ("threshold", "old_equity", "new_equity"):
            if case.get(key) is None:
                problems.append(f"{cid}: ready 但缺 {key}")
    return problems


def run_case(case: dict, settings: AISettings | None,
             timeout: float, strict_advice: bool = False) -> dict:
    row = dict(id=case["id"], priority=case["priority"], hand=case["hand"],
               street=case["street"], idx=case.get("action_index"),
               status=case["status"], expect=case["expect"],
               note=case.get("note", ""))
    if case["status"] != "ready":
        row["result"] = "SKIP"
        return row
    path = hand_path(case["hand"])
    if not path.exists():
        row["result"] = "MISSING"
        row["error"] = f"找不到 {path}"
        return row
    hand = load_hand(path)
    adv = find_advice(hand, case)
    if adv is None:
        row["result"] = "MISSING_ADVICE"
        return row

    old_sit, _ = situation_at(hand, adv, "0")
    new_sit, _ = situation_at(hand, adv, "1")
    old, new = equity_of(old_sit), equity_of(new_sit)
    row.update(old=old, new=new, threshold=case.get("threshold"))
    if old is None or new is None:
        row["result"] = "NO_RANGE"
        return row

    thr = case.get("threshold") or 0.0
    problems: list[str] = []
    if abs(old - (case.get("old_equity") or 0)) > TOL:
        problems.append(f"旧权益漂移：记录 {case['old_equity']:.3f}，实算 {old:.3f}")
    if abs(new - (case.get("new_equity") or 0)) > TOL:
        problems.append(f"新权益漂移：记录 {case['new_equity']:.3f}，实算 {new:.3f}")
    if abs(thr - (case.get("threshold") or 0)) > 0.01:
        problems.append(f"门槛漂移：记录 {thr:.3f}")
    if not crossing_ok(case["expect"], old, new, thr):
        problems.append(f"expect={case['expect']} 未满足")

    if settings is not None:
        row["model"] = {}
        for flag, label in (("0", "old"), ("1", "new")):
            sit, _ = situation_at(hand, adv, flag)
            t0 = time.monotonic()
            try:
                got = advise(sit, settings, timeout=timeout)
                row["model"][label] = got.option.id
                row["model"][label + "_label"] = got.option.label
                row["model"][label + "_kind"] = got.option.kind
                row["model"][label + "_amount"] = got.option.amount
                row["model"][label + "_reason"] = got.reason
            except Exception as exc:  # noqa: BLE001
                row["model"][label] = f"ERROR:{type(exc).__name__}"
            row["model"][label + "_ms"] = int((time.monotonic() - t0) * 1000)
        expected = case.get("new_advice")
        if expected and row["model"]["new"] != expected:
            # 模型有随机性，单次建议不是硬断言；默认只记录差异。
            row["advice_diff"] = (f"新模型建议 {row['model']['new']}，"
                                  f"记录 {expected}")
            if strict_advice:
                problems.append(row["advice_diff"])

    row["result"] = "PASS" if not problems else "FAIL"
    if problems:
        row["problems"] = problems
    return row


def fmt_equity(value) -> str:
    return "  —  " if value is None else f"{value * 100:5.1f}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对手范围事件模型 A/B 集")
    ap.add_argument("--set", type=Path, default=SET_PATH)
    ap.add_argument("--check", action="store_true", help="只校验清单与本地手牌")
    ap.add_argument("--priority", default=None, help="只跑某一档，如 P0")
    ap.add_argument("--model", action="store_true", help="同时跑旧/新模型（慢）")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--strict-advice", action="store_true",
                    help="模型建议与记录不一致时判 FAIL（默认只记录）")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    data = json.loads(args.set.read_text(encoding="utf-8"))
    if args.check:
        problems = check_manifest(data)
        if problems:
            print("❌ " + "\n❌ ".join(problems))
            return 1
        ready = sum(1 for c in data["cases"] if c["status"] == "ready")
        print(f"✅ 清单有效：{len(data['cases'])} 条，{ready} 条 ready")
        return 0

    settings = None
    if args.model:
        from poker_ocr.env import load_env
        env = load_env()
        source = "dotenv" if "AI_SERVER_API_KEY" in env.applied else "env"
        settings = AISettings.from_env(source=source)
        if settings is None:
            print("没有 AI 凭据，--model 用不了", file=sys.stderr)
            return 1
        print(f"模型：{settings.model}\n")

    rows: list[dict] = []
    for case in data["cases"]:
        if args.priority and case["priority"] != args.priority:
            continue
        rows.append(run_case(case, settings, args.timeout,
                             strict_advice=args.strict_advice))

    print(f"{'id':6s} {'hand':22s} {'st':5s} {'idx':>3s} "
          f"{'old':>6s} {'new':>6s} {'thr':>6s}  {'expect':12s} result")
    for r in rows:
        if r["result"] == "SKIP":
            print(f"{r['id']:6s} {r['hand']:22s} {r['street']:5s} "
                  f"{str(r['idx']):>3s} {'':6s} {'':6s} {'':6s}  "
                  f"{'':12s} SKIP ({r['status']})")
            continue
        thr = r.get("threshold")
        print(f"{r['id']:6s} {r['hand']:22s} {r['street']:5s} "
              f"{str(r['idx']):>3s} {fmt_equity(r.get('old'))} "
              f"{fmt_equity(r.get('new'))} "
              f"{'  —  ' if not thr else f'{thr * 100:5.1f}%'}  "
              f"{r['expect']:12s} {r['result']}")
        for p in r.get("problems") or []:
            print(f"       ↳ {p}")
        if r.get("advice_diff"):
            print(f"       ↳ {r['advice_diff']}")

    passed = sum(1 for r in rows if r["result"] == "PASS")
    failed = sum(1 for r in rows if r["result"] == "FAIL")
    skipped = sum(1 for r in rows if r["result"] == "SKIP")
    print(f"\nPASS {passed} / FAIL {failed} / SKIP {skipped}")
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"已写 {args.json}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
