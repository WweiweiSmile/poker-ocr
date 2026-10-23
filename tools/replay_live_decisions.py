#!/usr/bin/env python3
"""按已导出的手牌，把当时的每个决策点用**当前代码**重新跑一遍，和日志里当时的建议对比。

默认注入项目根目录的 `.table_dy`（和实时链路一致）；`--no-table-dynamics` 关闭，
`--table-dynamics PATH` 指定别的文件。

    # 只看事实层（不调模型、不花钱）：新的牌型排名 / 价值指令 vs 当时的选择
    .venv/bin/python tools/replay_live_decisions.py

    # 真跑模型：每个决策点重新问一次，和当时的 option 对比
    .venv/bin/python tools/replay_live_decisions.py --model
    .venv/bin/python tools/replay_live_decisions.py --model --limit 20 --losses-only

## 决策点从哪里来

以 `logs/live.states.jsonl` 里的 `advice` 记录为准 —— 每一条就是当时程序生成建议的那个
决策点（含弃牌决策，`build_decisions` 那套按 hero 动作重放会漏掉弃牌）。

## 快照怎么重建

对每条 advice，按 `(pot, to_call)` 在它之前的 `state` 里找最吻合的一帧，用那一帧重建
`HandSnapshot`。这是**当时程序真正看到的东西**，不是事后按行动线推的：

    · 底池/需跟/各家筹码/下注/弃牌 → 直接取 state；
    · 本街行动线 → 用 state 里各家 bet 反推（谁下注、谁是 raise）；
    · 之前的街 → 用 hand 记录里的完整行动线。

重建精度：109 个决策点里 108 个的 `(pot, to_call)` 与 advice 记录**完全一致**
（唯一不符的 h60 是爆炸底池的 1.5BB 读数差）。

## 已知边界

- `hands.json` 不含「中途接管」的手（13 手），h75（22 在 88T 上那手 −32.5）就在里面，
  所以这里也看不到它；要看加 `--include-mid-hand` 重新导出。
- 本街行动线是反推的，`Opponent strength from actions` 一行与当时可能有细微差别。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.advice import (  # noqa: E402
    AISettings, advise, build_situation, render_hand_facts, value_directive,
)
from poker_ocr.env import load_env  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.table_dynamics import load_table_dynamics  # noqa: E402

STATE_PER_STREET = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}
BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def street_of(state: dict) -> str:
    return STATE_PER_STREET.get(len(state.get("board") or []), "preflop")


def to_call_of(state: dict) -> tuple[float, float, float]:
    """返回 (hero 本街已下注, 当前最高下注, to_call)。"""
    hero_bet = float((state.get("hero") or {}).get("bet") or 0.0)
    top = max([hero_bet] + [float(o.get("bet") or 0.0)
                            for o in state.get("opponents") or []])
    return hero_bet, top, round(max(top - hero_bet, 0.0), 2)


def find_state(advice: dict, states: list[dict], since: str = "") -> dict | None:
    """在上一条 advice 之后、这一条之前的帧里，找与 (pot, to_call, 街) 最吻合的一帧。

    只在这个窗口里找，是因为不同手牌经常撞出同样的底池（翻前 6.5/7.0 到处都是），
    全表搜索会把上一手/下一手的帧配进来。
    """
    want_street = BOARD_LEN.get(advice.get("street"), 0)
    want_pot = float(advice.get("pot") or 0.0)
    want_call = float(advice.get("to_call") or 0.0)
    want_cards = tuple(advice.get("hero_cards") or ())
    window = [s for s in states
              if since < s["ts"] <= advice["ts"]]
    if not window:                      # 窗口空（首条 / 极端情况）→ 退回最近若干帧
        window = [s for s in states if s["ts"] <= advice["ts"]][-5:]
    best: tuple[float, str, dict] | None = None
    for st in window:
        _, _, call = to_call_of(st)
        cards = tuple(st.get("hero_cards") or ())
        cards_pen = 0.0 if (not want_cards or not cards or cards == want_cards) else 50.0
        score = (cards_pen
                 + abs(len(st.get("board") or []) - want_street) * 20
                 + abs(float(st.get("pot") or 0.0) - want_pot)
                 + abs(call - want_call))
        if best is None or (score, st["ts"]) < (best[0], best[1]):
            best = (score, st["ts"], st)
    return best[2] if best else None


def synth_street(state: dict, street: str) -> StreetRecord:
    """用 state 里各家的 bet 反推本街行动线（谁下注、谁是 raise）。

    翻前要特别处理：state 里 SB/BB 的 `bet` 是**盲注**，不是主动下注。
    如果照搬成 "bet"，h113 这类「无人入池、轮到我开池」的点会被重建成
    「SB bet 0.5 → BB bet 1.0」→ 形状变成 three-bet，选项变成 3bet/fold，
    模型于是拿 Q8s（LJ 在范围内）去弃牌。这是重建口径的错，不是模型错。
    所以翻前跳过纯盲注，把 ==BB 的投入记成 limp/call，>BB 的记成 raise。
    """
    rows: list[ActionRecord] = []
    hero = state.get("hero") or {}
    seats = [(hero, True)] + [(o, False) for o in state.get("opponents") or []]
    blinds = state.get("blinds") or {}
    sb = float(blinds.get("sb") or 0.0)
    bb = float(blinds.get("bb") or 1.0)
    for seat, is_hero in seats:
        bet = float(seat.get("bet") or 0.0)
        if bet <= 0:
            continue
        action = "bet"
        if street == "preflop":
            pos = seat.get("position")
            # 纯盲注不是行动：SB 只投了 0.5、BB 只投了 1.0 → 跳过。
            if pos == "SB" and bet <= sb + 1e-9:
                continue
            if pos == "BB" and bet <= bb + 1e-9:
                continue
            # 补到 1BB 是 limp/complete，超过 1BB 才是加注。
            action = "raise" if bet > bb + 1e-9 else "call"
        rows.append(ActionRecord(
            street=street, seat_id=str(seat.get("seat_id") or ""),
            name=seat.get("name"), position=seat.get("position"),
            action=action, amount=bet, all_in=bool(seat.get("all_in")),
            is_hero=is_hero))
    rows.sort(key=lambda a: a.amount or 0.0)
    return StreetRecord(street, float(state.get("pot_before_street") or 0.0),
                        tuple(rows))


def build_snap(advice: dict, state: dict, hand: dict | None) -> HandSnapshot:
    street = advice["street"]
    # 牌面/手牌用 **advice 记录里的**：那是模型当时真正看到的。
    # state 常常是还没读到牌的更早一帧（翻前尤其明显），拿它会变成「Hole cards: unknown」。
    board = tuple(advice.get("board") or state.get("board") or ())
    hero_cards = tuple(advice.get("hero_cards")
                       or state.get("hero_cards") or ())
    _, top, call = to_call_of(state)
    hero = state.get("hero") or {}
    blinds_raw = state.get("blinds") or {}
    sb = float(blinds_raw.get("sb") or 0.0)
    bb = float(blinds_raw.get("bb") or 1.0)
    ante = blinds_raw.get("ante")
    blinds = Blinds(sb, bb, float(ante) if ante else None,
                    str(blinds_raw.get("raw") or ""))

    # 之前的街用 hand 记录的完整行动线；本街用 state 反推
    prior: list[StreetRecord] = []
    if hand:
        order = ["preflop", "flop", "turn", "river"]
        for rec in hand.get("streets") or []:
            s = rec.get("street")
            if s not in order or order.index(s) >= order.index(street):
                continue
            prior.append(StreetRecord(
                s, rec.get("pot_start"), tuple(
                    ActionRecord(
                        street=s, seat_id=str(a.get("seat_id") or ""),
                        name=a.get("name"), position=a.get("position"),
                        action=str(a.get("action") or ""), amount=a.get("amount"),
                        all_in=bool(a.get("all_in")), is_hero=bool(a.get("is_hero")))
                    for a in rec.get("actions") or ())))
    streets = tuple(prior) + (synth_street(state, street),)

    pre = [a for r in streets if r.street == "preflop" for a in r.actions]
    hero_is_pfr = any(a.is_hero and a.action in ("bet", "raise") for a in pre)

    # 翻前形态必须重建，否则「面对加注」会被当成「没人加注」，
    # 选项变成 open-* 而不是 raise-3x/4x —— 那两手对比就不公平了。
    # 口径与 hand_track._preflop_shapes 一致。
    raises = sum(1 for a in pre
                 if a.action in ("raise", "bet")
                 or (a.action == "call" and a.all_in))
    if raises == 0 and max(top, float(hero.get("bet") or 0.0)) > bb + 1e-9:
        raises = 1
    if raises == 0:
        limped = any(a.action == "call" and not a.is_hero for a in pre)
        shapes = ("limped",) if limped else ("unopened",)
    elif raises == 1:
        shapes = ("single-raise",)
    else:
        shapes = ("three-bet",)

    return HandSnapshot(
        hand_id=str(advice.get("hand_id")),
        hand_index=0, street=street, board=board,
        hero_cards=hero_cards,
        hero_position=state.get("hero_position"),
        hero_stack=hero.get("stack"), hero_bet=float(hero.get("bet") or 0.0),
        hero_invested=0.0, to_call=call, second_bet=None,
        pot=float(state.get("pot") or 0.0),
        pot_before_street=float(state.get("pot_before_street") or 0.0),
        table_size=int(state.get("table_seats") or 0), blinds=blinds,
        opponents=tuple(
            OpponentSnapshot(str(o.get("seat_id") or ""), o.get("name"),
                             o.get("position"), o.get("stack"),
                             float(o.get("bet") or 0.0),
                             bool(o.get("fold")) or o.get("status") == "folded")
            for o in state.get("opponents") or ()),
        streets=streets,
        preflop_shapes=shapes, hero_is_pfr=hero_is_pfr, cbet_spot=False,
        decision_key=(str(advice.get("hand_id")), street, 0,
                      float(state.get("pot") or 0.0), call,
                      hero_cards),
        players_dealt=int(state.get("players_in_hand") or 0),
        bomb_pot=bool(state.get("bomb_pot")),
    )


def old_summary(advice: dict) -> str:
    return f"{advice['option']['id']}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用当前代码重跑已导出手牌的决策点")
    ap.add_argument("--log", type=Path, default=Path("logs/live.states.jsonl"))
    ap.add_argument("--hands", type=Path, default=Path("hands.json"))
    ap.add_argument("--model", action="store_true", help="真的调模型重出建议")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个决策点")
    ap.add_argument("--losses-only", action="store_true")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", type=Path, default=None,
                    help="把每个决策点的事实与新旧建议写成 JSON")
    ap.add_argument("--model-name", default=None,
                    help="覆盖 AI_SERVER_MODEL（例如 deepseek-v4-pro），用来对比模型")
    ap.add_argument("--table-dynamics", type=Path, default=None,
                    help="牌桌动态文件；默认读项目根目录 .table_dy")
    ap.add_argument("--no-table-dynamics", action="store_true",
                    help="不注入 .table_dy（默认注入，和实时链路一致）")
    args = ap.parse_args(argv)

    dynamics_text = ""
    if not args.no_table_dynamics:
        dyn = load_table_dynamics(args.table_dynamics)
        if dyn.error:
            print(f"牌桌动态读取失败：{dyn.error}", file=sys.stderr)
        elif dyn.found:
            dynamics_text = dyn.text
            print(f"牌桌动态：{dyn.path}（{len(dynamics_text)} 字符）")

    records = load(args.log)
    states = [r for r in records if r.get("type") == "state" and r.get("hero")]
    advices = [r for r in records if r.get("type") == "advice"]
    hands = {}
    if args.hands.exists():
        for h in json.loads(args.hands.read_text(encoding="utf-8")):
            hands[str(h.get("hand_id"))] = h

    settings = None
    if args.model:
        env = load_env()
        source = "dotenv" if "AI_SERVER_API_KEY" in env.applied else "env"
        settings = AISettings.from_env(source=source)
        if settings is None:
            print("没有 AI 凭据，--model 用不了", file=sys.stderr)
            return 1
        if args.model_name:
            settings = replace(settings, model=args.model_name)
        print(f"模型: {settings.model}"
              f"（thinking={settings.thinking}，reasoning={settings.reasoning}）\n")

    rows = []
    stale = 0
    since = ""
    for adv in advices:
        st = find_state(adv, states, since)
        since = adv["ts"]
        if st is None:
            continue
        snap = build_snap(adv, st, hands.get(str(adv.get("hand_id"))))
        # 陈旧帧：翻前已经投过钱（底池远超"盲注+前注"）、却又报 to_call=0。
        # 这种记录是「同一决策在旧帧上又建议了一次」，hero 其实已经没有选择，
        # 拿来对比模型不公平，跳过。
        # 翻前 to_call=0 只有 BB 过牌是合法的；其他位置出现 to_call=0 说明
        # hero 已经投过钱（补盲/加注都发生了），状态没推进到下一街，
        # 这条 advice 是在旧帧上又建议了一次，hero 其实已经没有选择。
        bb = (snap.blinds.bb if snap.blinds else 1.0)
        if (snap.street == "preflop" and snap.to_call == 0
                and ((snap.hero_position or "") not in ("BB",)
                     or (snap.pot or 0) > 12 * bb)):
            stale += 1
            continue
        sit = build_situation(snap, table_dynamics=dynamics_text)
        d_new = value_directive(sit) or ""
        row = {
            "hand": snap.hand_id, "street": adv["street"],
            "cards": " ".join(snap.hero_cards), "board": " ".join(snap.board),
            "pot": snap.pot, "to_call": snap.to_call,
            "old": old_summary(adv), "old_reason": adv.get("reason") or "",
            "tier": sit.hand_tier, "beat": sit.beat_at_least_pct,
            "directive": d_new.split(":")[0] if d_new else "(none)",
            "snap": snap, "sit": sit,
        }
        rows.append(row)

    if args.losses_only:
        losing = set()
        if Path("hands_review.csv").exists():
            for line in Path("hands_review.csv").read_text(
                    encoding="utf-8").splitlines()[1:]:
                parts = line.split(",")
                if len(parts) > 1:
                    try:
                        if float(parts[1]) < 0:
                            losing.add(parts[0])
                    except ValueError:
                        pass
        rows = [r for r in rows if r["hand"] in losing]
    if args.limit:
        rows = rows[:args.limit]

    print(f"决策点 {len(rows)} 个（跳过陈旧帧 {stale} 个）；当前代码的牌型排名与价值指令：\n")
    flagged = [r for r in rows if "NOT A VALUE" in r["directive"]]
    print(f"被新规则判为「不是价值牌」的有 {len(flagged)} 个\n")

    hdr = f"{'手':7}{'街':8}{'我的牌':9}{'牌面':18}{'全桌领先':>9}  {'旧建议':10}{'新指令'}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        b = f"{r['beat']:.1f}%" if r["beat"] is not None else "-"
        star = " ★" if "NOT A VALUE" in r["directive"] else ""
        print(f"{r['hand']:7}{r['street']:8}{r['cards']:9}{r['board']:18}"
              f"{b:>9}  {r['old']:10}{r['directive']}{star}")

    if args.model:
        print("\n重新问模型 ...")
        changed = []
        for i, r in enumerate(rows, 1):
            t0 = time.monotonic()
            try:
                a = advise(r["sit"], settings, timeout=args.timeout)
                new = a.option.id
                why = a.reason
            except Exception as exc:                 # noqa: BLE001
                new, why = "ERROR", f"{type(exc).__name__}: {exc}"
            r["new"] = new
            r["new_reason"] = why
            r["ms"] = int((time.monotonic() - t0) * 1000)
            same = (new == r["old"])
            if not same:
                changed.append(r)
            print(f"  [{i}/{len(rows)}] {r['hand']:7}{r['street']:8}"
                  f"{r['old']:10}→ {new:10}{'' if same else '  <<< 变了'}  ({r['ms']}ms)")
        print(f"\n变化 {len(changed)} / {len(rows)}")
        for r in changed:
            print(f"\n· {r['hand']} {r['street']} {r['cards']} / {r['board']}")
            print(f"  旧: {r['old']}  — {r['old_reason']}")
            print(f"  新: {r['new']}  — {r['new_reason']}")

    if args.json:
        dump = []
        for r in rows:
            sit = r["sit"]
            dump.append({
                "hand": r["hand"], "street": r["street"],
                "cards": r["cards"], "board": r["board"],
                "position": sit.snap.hero_position,
                "pot": r["pot"], "to_call": r["to_call"],
                "stack": sit.snap.hero_stack,
                "n_opp": sit.opponents_in_hand,
                "tier": sit.hand_tier, "label": sit.hand_label_en,
                "beat_pct": r["beat"], "texture": sit.texture_en,
                "equity_pct": sit.equity_pct,
                "nuts": (sit.ground_truth.hero_is_nuts
                         if sit.ground_truth is not None else None),
                "ground_truth": (sit.ground_truth.to_dict()
                                 if sit.ground_truth is not None else None),
                "old": r["old"], "old_reason": r["old_reason"],
                "directive": r["directive"],
                "new": r.get("new"), "new_reason": r.get("new_reason"),
                "options": [o.id for o in sit.options],
            })
        Path(args.json).write_text(
            json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n写出 {args.json}（{len(dump)} 个决策点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
