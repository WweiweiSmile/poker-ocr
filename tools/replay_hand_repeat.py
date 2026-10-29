#!/usr/bin/env python3
"""把**单手牌**的每个决策点用当前代码 + 当前模型重复问 N 次，看建议稳不稳。

`replay_live_decisions.py` 是全量批跑（每个决策点问一次、和旧建议对比）；
这里只挑一手（`--hand h88`），同一个快照问 N 次，用来判断
「这条建议是模型稳定结论，还是采样抖动」。

决策点的来源、快照重建、陈旧帧过滤都直接复用 replay_live_decisions，
保证和全量批跑的口径一致（同一帧、同一 facts、同一 .table_dy 注入）。

    # 只看事实层，不调模型
    .venv/bin/python tools/replay_hand_repeat.py --hand h88

    # 每个决策点重跑 3 次
    .venv/bin/python tools/replay_hand_repeat.py --hand h88 --repeat 3 --model

    # 换模型对比
    .venv/bin/python tools/replay_hand_repeat.py --hand h88 --repeat 3 --model \
        --model-name deepseek-v4-pro
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
for p in (str(ROOT), str(TOOLS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from poker_ocr.ground_truth import card_text  # noqa: E402
from poker_ocr.advice import (  # noqa: E402
    AISettings, advise, build_situation,
)
from poker_ocr.env import load_env  # noqa: E402
from poker_ocr.table_dynamics import load_table_dynamics  # noqa: E402
from replay_live_decisions import (  # noqa: E402
    build_snap, find_state, load as load_records,
)


def collect(hand_id: str, log: Path, hands_path: Path,
            dynamics_text: str) -> list[dict]:
    records = load_records(log)
    states = [r for r in records if r.get("type") == "state" and r.get("hero")]
    advices = [r for r in records
               if r.get("type") == "advice" and str(r.get("hand_id")) == hand_id]
    hands = {}
    if hands_path.exists():
        for h in json.loads(hands_path.read_text(encoding="utf-8")):
            hands[str(h.get("hand_id"))] = h

    rows = []
    since = ""
    for adv in advices:
        st = find_state(adv, states, since)
        since = adv["ts"]
        if st is None:
            print(f"跳过 {adv['ts']}：找不到匹配的 state 帧", file=sys.stderr)
            continue
        snap = build_snap(adv, st, hands.get(hand_id))
        # 与 replay_live_decisions 相同的陈旧帧过滤
        bb = snap.blinds.bb if snap.blinds else 1.0
        if (snap.street == "preflop" and snap.to_call == 0
                and ((snap.hero_position or "") not in ("BB",)
                     or (snap.pot or 0) > 12 * bb)):
            print(f"跳过陈旧帧 {adv['ts']}（翻前 pot={snap.pot} to_call=0）",
                  file=sys.stderr)
            continue
        sit = build_situation(snap, table_dynamics=dynamics_text)
        rows.append({"advice": adv, "snap": snap, "sit": sit})
    return rows


def facts_block(r: dict) -> str:
    snap, sit = r["snap"], r["sit"]
    gt = sit.ground_truth
    beat = (f"{sit.beat_at_least_pct:.1f}%" if sit.beat_at_least_pct is not None
            else "-")
    n_opp = sum(1 for o in snap.opponents if not o.folded)
    lines = [
        f"  {snap.street}  pot={snap.pot}  to_call={snap.to_call}  "
        f"stack={snap.hero_stack}  position={snap.hero_position}  "
        f"opps_in_hand={n_opp}",
        f"  cards={' '.join(snap.hero_cards)}  board={' '.join(snap.board) or '-'}",
        f"  tier={sit.hand_tier}  label={sit.hand_label_en}  "
        f"texture={sit.texture_en}  equity={sit.equity_pct}  "
        f"beat_at_least={beat}",
    ]
    if gt is not None:
        lines.append(f"  made={gt.made.name} ({gt.made.detail})  "
                     f"nuts={gt.hero_is_nuts}  "
                     f"best_five={' '.join(card_text(c) for c in gt.made.best_five)}")
    hs = sit.hand_strength
    if hs is not None:
        lines.append(f"  hand_strength={hs.kind}/{hs.size} "
                     f"behind={hs.behind_any:.1%} equity={hs.equity:.0%}")
    if sit.warnings:
        lines.append(f"  warnings={'; '.join(sit.warnings)}")
    lines.append("  options=" + ", ".join(
        f"{o.id}[{o.kind}{'' if o.amount is None else f' {o.amount:g}BB'}]"
        for o in sit.options))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用当前代码/模型重复重跑单手牌的决策点")
    ap.add_argument("--hand", required=True)
    ap.add_argument("--log", type=Path, default=Path("logs/live.states.jsonl"))
    ap.add_argument("--hands", type=Path, default=Path("hands.json"))
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--model", action="store_true", help="真的调模型")
    ap.add_argument("--model-name", default=None, help="覆盖 AI_SERVER_MODEL")
    ap.add_argument("--temperature", type=float, default=None,
                    help="覆盖 AI_SERVER_TEMPERATURE")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--table-dynamics", type=Path, default=None)
    ap.add_argument("--no-table-dynamics", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    dynamics_text = ""
    if not args.no_table_dynamics:
        dyn = load_table_dynamics(args.table_dynamics)
        if dyn.error:
            print(f"牌桌动态读取失败：{dyn.error}", file=sys.stderr)
        elif dyn.found:
            dynamics_text = dyn.text
            print(f"牌桌动态：{dyn.path}（{len(dynamics_text)} 字符）")

    rows = collect(args.hand, args.log, args.hands, dynamics_text)
    if not rows:
        print(f"{args.hand} 没有可重跑的决策点", file=sys.stderr)
        return 1

    settings = None
    if args.model:
        env = load_env()
        source = "dotenv" if "AI_SERVER_API_KEY" in env.applied else "env"
        settings = AISettings.from_env(source=source)
        if settings is None:
            print("没有 AI 凭据", file=sys.stderr)
            return 1
        if args.model_name:
            settings = replace(settings, model=args.model_name)
        if args.temperature is not None:
            settings = replace(settings, temperature=args.temperature)
        print(f"模型: {settings.model}  temperature={settings.temperature}  "
              f"thinking={settings.thinking}  reasoning={settings.reasoning}  "
              f"max_tokens={settings.max_tokens}")

    print(f"\n{args.hand}：{len(rows)} 个决策点，每个重复 {args.repeat} 次\n")
    out = []
    for i, r in enumerate(rows, 1):
        adv = r["advice"]
        old_id = adv["option"]["id"]
        print(f"── [{i}/{len(rows)}] {adv['street']}  当时的建议: {old_id}"
              f" ({adv['option'].get('label')})")
        print(facts_block(r))
        old_reason = (adv.get("reason") or "").strip()
        if old_reason:
            print(f"  当时理由: {old_reason}")
        runs = []
        if settings is not None:
            for k in range(args.repeat):
                t0 = time.monotonic()
                try:
                    a = advise(r["sit"], settings, timeout=args.timeout)
                    item = {"option": a.option.id, "label": a.option.label,
                            "kind": a.option.kind, "reason": a.reason,
                            "source": a.source, "error": a.error,
                            "ms": int((time.monotonic() - t0) * 1000)}
                except Exception as exc:                  # noqa: BLE001
                    item = {"option": "ERROR", "reason": "",
                            "source": "exception",
                            "error": f"{type(exc).__name__}: {exc}",
                            "ms": int((time.monotonic() - t0) * 1000)}
                runs.append(item)
                tag = "" if item["option"] == old_id else "  <<< 与当时不同"
                print(f"  第{k + 1}次: {item['option']:12} ({item['ms']}ms){tag}")
                print(f"          {item['reason'] or item['error']}")
            dist = Counter(x["option"] for x in runs)
            print(f"  → {dict(dist)}")
        out.append({"hand": args.hand, "street": adv["street"],
                    "old": old_id, "old_reason": old_reason,
                    "facts": {
                        "pot": r["snap"].pot, "to_call": r["snap"].to_call,
                        "stack": r["snap"].hero_stack,
                        "position": r["snap"].hero_position,
                        "cards": list(r["snap"].hero_cards),
                        "board": list(r["snap"].board),
                        "tier": r["sit"].hand_tier,
                        "label": r["sit"].hand_label_en,
                        "texture": r["sit"].texture_en,
                        "equity_pct": r["sit"].equity_pct,
                        "beat_at_least_pct": r["sit"].beat_at_least_pct,
                        "hand_strength": ({
                            "kind": r["sit"].hand_strength.kind,
                            "size": r["sit"].hand_strength.size,
                            "behind_any": r["sit"].hand_strength.behind_any,
                            "equity": r["sit"].hand_strength.equity,
                        } if r["sit"].hand_strength is not None else None),
                        "options": [o.id for o in r["sit"].options],
                        "warnings": list(r["sit"].warnings),
                    },
                    "runs": runs})
        print()

    if args.json:
        args.json.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"写出 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
