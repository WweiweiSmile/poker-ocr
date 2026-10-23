#!/usr/bin/env python3
"""用**当前** AI 建议重跑 dev 库导出的复盘手牌，和库里的实际动作逐决策点对比。

默认注入项目根目录的 `.table_dy`（和实时链路一致）；`--no-table-dynamics` 关闭，
`--table-dynamics PATH` 指定别的文件。

这是 `ai_advice_dev_hands.py` 的升级版：

- 覆盖**翻前 + 翻后**（原脚本只评翻后）；
- 底池/需跟额按「每街重置本街投入」重建，翻前把盲注放进 commit，
  所以 BB 不会被算成「还要跟 1BB」；
- 爆炸底池（`ante_bb == 5` 且没有翻前行动）按 5BB/人重建，不再把盲注重复加进去；
- 每条建议同时输出程序事实（牌型分档、牌型排名、范围归属、价值指令），
  并做一遍规则审计，方便判断「新建议 vs 实际动作」谁更合理。

用法：
    # 1) 从 dev 库导出最近 30 手（在 ~/codes/call-back 下）
    export $(grep -E '^DB_' .env | xargs)
    mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" -N -e "
      SELECT JSON_OBJECT('id',id,'heroPosition',hero_position,'heroCards',hero_cards,
        'heroStackBb',hero_stack_bb,'sb',small_blind_bb,'bb',big_blind_bb,'ante',ante_bb,
        'bombPot',bomb_pot_bb,'board',IFNULL(board,''),'villainCount',villain_count,
        'tableSize',table_size,'villains',IFNULL(villains,JSON_ARRAY()),
        'streets',streets,'heroThought',IFNULL(hero_thought,''),
        'result',result,'resultAmount',result_amount,'createdAt',created_at)
      FROM (SELECT * FROM review_hands WHERE deleted_at IS NULL
            ORDER BY created_at DESC, id DESC LIMIT 30) t
      ORDER BY t.created_at DESC, t.id DESC;" > /tmp/review30.jsonl

    # 2) 只跑事实层（不花钱）
    .venv/bin/python tools/compare_review_hands.py /tmp/review30.jsonl

    # 3) 真跑模型
    .venv/bin/python tools/compare_review_hands.py /tmp/review30.jsonl \
        --json logs/latest-advice-review30.json

## 对比口径

    一致     动作类别相同；激进动作再看尺度（翻前 >1BB、翻后 >1/4 池算尺度差）
    尺度差   两边都是 bet/raise/allin，只是下注量不同
    动作不同 动作类别不同（如实际 call、模型说 fold）

## 规则审计（只报「可证伪」的硬错，不做主观好坏）

- 翻前表外加注 / 表外溜入 / 开池平跟（范围表由程序算）
- SB 补盲垃圾牌（`preflop_ranges.sb_completable`）
- 范围内却弃牌（没人加注时）
- 非价值下注（`value_directive` 明确 NOT A VALUE / BOARD PLAY / NO FOLD EQUITY）
- 非坚果加注（`VALUE CAUTION` / `ONE PAIR vs AGGRESSION` / 爆炸池一对）
- 有赔率却弃牌（听牌权益 > 跟注门槛 + 2%）
- 价值下注过小（强牌只下 1/3 池；复盘里 1/3 从不当价值尺度）

审计是保守口径：宁可漏判，不把边界牌硬判成错。最终结论仍以逐手人工复核为准。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.advice import (  # noqa: E402
    AISettings, advise, build_situation, is_jev_model, value_directive,
)
from poker_ocr.env import load_env  # noqa: E402
from poker_ocr.hand_track import (  # noqa: E402
    ActionRecord, HandSnapshot, OpponentSnapshot, StreetRecord,
)
from poker_ocr.parse import Blinds  # noqa: E402
from poker_ocr.preflop_ranges import (  # noqa: E402
    hand_key, range_verdict, sb_completable,
)
from poker_ocr.table_dynamics import load_table_dynamics  # noqa: E402

POSTFLOP = ("flop", "turn", "river")
STREETS = ("preflop",) + POSTFLOP
BOARD_LEN = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
AGGRESSIVE = ("bet", "raise", "allin")
SMALL_PAIRS = {"22", "33", "44", "55", "66"}


# ── 读入与去重 ────────────────────────────────────────────────


def load_hands(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if raw.startswith("["):
        return json.loads(raw)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def signature(h: dict) -> str:
    """内容指纹：底牌 + 公共牌 + 全部行动。用来去掉重复手牌。"""
    return json.dumps([h.get("heroCards"), h.get("board"), h.get("streets")],
                      sort_keys=True, ensure_ascii=False)


def cards_of(text: str | None) -> tuple[str, ...]:
    text = text or ""
    return tuple(text[i:i + 2] for i in range(0, len(text) - 1, 2))


# ── 把一手库里的牌重放成决策点 ────────────────────────────────


def _players(h: dict) -> int:
    return int(h.get("tableSize") or (h.get("villainCount") or 1) + 1)


def _bomb_pot(h: dict, ante: float) -> bool:
    """库里的爆炸底池没有 banner 字段：`ante_bb == 5` 且没有翻前行动即炸弹池。"""
    if float(h.get("bombPot") or 0) > 0:
        return True
    has_preflop = any(s.get("street") == "preflop" for s in h.get("streets") or [])
    return (not has_preflop) and ante >= 5


def build_decisions(h: dict) -> tuple[list[dict], bool] | None:
    """返回 (hero 的决策点, 是否爆炸底池)。数据不足以重建时返回 None。

    每个决策点带 `snap`（轮到我这一动之前的快照）、`actual` / `actual_amt`。
    """
    players = _players(h)
    sb = float(h.get("sb") or 0.0)
    bb = float(h.get("bb") or 0.0)
    ante = float(h.get("ante") or 0.0)
    hero_cards = cards_of(h.get("heroCards"))
    if bb <= 0 or len(hero_cards) != 2 or not h.get("streets"):
        return None

    bomb = _bomb_pot(h, ante)
    hero_pos = h.get("heroPosition") or None
    hero_start = h.get("heroStackBb")
    hero_start = float(hero_start) if hero_start is not None else None

    # 盲注/前注已经在底池里；hero 的投入也要从后手里扣掉。
    pot = 5.0 * players if bomb else sb + bb + ante * players
    hero_invested = ante if bomb else ante + ({"SB": sb, "BB": bb}.get(hero_pos or "", 0.0))

    villains = {
        v.get("position"): v for v in (h.get("villains") or []) if v.get("position")
    }
    folded = {pos: False for pos in villains}
    built: list[StreetRecord] = []
    decisions: list[dict] = []
    seen_actions: list[ActionRecord] = []
    idx = 0

    for rec in h.get("streets") or []:
        street = rec.get("street")
        if street not in STREETS:
            continue
        if rec.get("potStartBb") is not None:
            pot = float(rec["potStartBb"])
        pot_start = pot

        # 每街重置本街投入；翻前把盲注放进去，BB 的 to_call 才是 0。
        commit: dict[str, float] = {}
        level = 0.0
        if street == "preflop":
            commit = {"SB": sb, "BB": bb}
            level = bb
            if hero_pos in ("SB", "BB"):
                commit["hero"] = commit[hero_pos]

        cur: list[ActionRecord] = []
        for a in rec.get("actions") or []:
            actor = a.get("actor")
            act = a.get("action")
            amt = a.get("amountBb")
            is_hero = actor == "hero"

            if is_hero:
                # 对手：库里列出的 villains + 行动线里出现但没列出的位置。
                opps: list[OpponentSnapshot] = []
                listed: set[str] = set()
                for pos, v in villains.items():
                    if pos == hero_pos or pos in listed:
                        continue
                    listed.add(pos)
                    opps.append(OpponentSnapshot(
                        seat_id=pos, name=v.get("name"), position=pos,
                        stack=v.get("stackBb"),
                        bet=float(commit.get(pos, 0.0) or 0.0),
                        folded=bool(folded.get(pos))))
                for pos in {x.seat_id for x in seen_actions + cur if x.seat_id}:
                    if pos in ("hero", hero_pos) or pos in listed:
                        continue
                    listed.add(pos)
                    opps.append(OpponentSnapshot(
                        seat_id=pos, name=None, position=pos, stack=None,
                        bet=float(commit.get(pos, 0.0) or 0.0),
                        folded=bool(folded.get(pos))))

                streets = tuple(built) + (
                    StreetRecord(street, pot_start, tuple(cur)),)
                pre_acts = [x for r in streets if r.street == "preflop"
                            for x in r.actions]
                raises = sum(1 for x in pre_acts
                             if x.action in ("raise", "bet")
                             or (x.action == "call" and x.all_in))
                if raises == 0:
                    limped = any(x.action == "call" and not x.is_hero
                                 for x in pre_acts)
                    shapes = ("limped",) if limped else ("unopened",)
                elif raises == 1:
                    shapes = ("single-raise",)
                else:
                    shapes = ("three-bet",)
                hero_pfr = any(x.is_hero and x.action in ("raise", "bet")
                               for x in pre_acts)

                remaining = (max(hero_start - hero_invested, 0.0)
                             if hero_start is not None else None)
                to_call = round(max(level - float(commit.get("hero", 0.0) or 0.0), 0.0), 2)
                board = cards_of(h.get("board"))[:BOARD_LEN[street]]
                snap = HandSnapshot(
                    hand_id=str(h.get("id")), hand_index=0, street=street,
                    board=board, hero_cards=hero_cards,
                    hero_position=hero_pos,
                    hero_stack=round(remaining, 2) if remaining is not None else None,
                    hero_bet=float(commit.get("hero", 0.0) or 0.0),
                    hero_invested=round(hero_invested, 2),
                    to_call=to_call, second_bet=None,
                    pot=round(pot, 2), pot_before_street=round(pot_start, 2),
                    table_size=players,
                    blinds=Blinds(sb, bb, ante or None, f"{sb}/{bb}({ante})"),
                    opponents=tuple(opps), streets=streets,
                    preflop_shapes=shapes, hero_is_pfr=hero_pfr,
                    cbet_spot=False,
                    decision_key=(str(h.get("id")), street, idx,
                                  round(pot, 2), to_call),
                    players_dealt=players, bomb_pot=bomb)
                decisions.append({"snap": snap, "actual": act,
                                  "actual_amt": amt, "pot": round(pot, 2)})
                idx += 1

            cur.append(ActionRecord(
                street=street, seat_id=str(actor),
                name=(villains.get(actor) or {}).get("name") if actor != "hero" else None,
                position=(hero_pos if is_hero else actor),
                action=act, amount=amt, all_in=(act == "allin"), is_hero=is_hero))

            delta = 0.0
            if act in ("bet", "raise", "allin") and amt is not None:
                level = float(amt)
                delta = max(level - float(commit.get(actor, 0.0) or 0.0), 0.0)
                commit[actor] = level
            elif act == "call":
                delta = max(level - float(commit.get(actor, 0.0) or 0.0), 0.0)
                commit[actor] = level
            pot += delta
            if actor != "hero" and act == "fold":
                folded[actor] = True
            if is_hero:
                hero_invested += delta

        built.append(StreetRecord(street, pot_start, tuple(cur)))
        seen_actions += cur

    return decisions, bomb


# ── 规则审计 ──────────────────────────────────────────────────


def _aggressive(kind: str) -> bool:
    return kind in AGGRESSIVE


def _directive_prefix(sit) -> str:
    d = value_directive(sit) or ""
    return d.split(":")[0] if d else ""


def audit(sit, kind: str, amount: float | None) -> set[str]:
    """对一条建议做硬规则审计，返回违规标签集合。"""
    tags: set[str] = set()
    snap = sit.snap
    street = snap.street
    pos = (snap.hero_position or "").upper()
    cards = snap.hero_cards
    to_call = float(snap.to_call or 0.0)
    pot = float(snap.pot or 0.0)
    prefix = _directive_prefix(sit)
    agg = _aggressive(kind)

    # 翻后：价值指令明确说「不是价值牌」还下注/加注。
    if agg and prefix in ("NOT A VALUE HAND BY RANK", "NO FOLD EQUITY"):
        tags.add("非价值下注")
    if agg and prefix == "BOARD PLAY":
        # 打牌面允许「翻牌一条小街」，其它情况算白送。
        if street != "flop" or (amount is not None and pot and amount > 0.5 * pot):
            tags.add("打牌面下注")
    # 非坚果/一对面对加注时加注。
    if kind == "raise" and prefix == "VALUE CAUTION":
        tags.add("非坚果加注")
    if kind == "raise" and prefix == "ONE PAIR vs AGGRESSION":
        tags.add("一对加注")
    if kind == "raise" and prefix in ("EXPLOSIVE POT", "TABLE DYNAMICS OVERRIDE"):
        tags.add("非坚果加注")
    # 多路听牌主动下注（当前代码已经把这档从选项里拿掉，这里是回归检查）。
    if agg and (sit.opponents_in_hand or 0) >= 3 \
            and sit.hand_tier in ("强听", "弱听"):
        tags.add("多路听牌下注")

    # 翻前范围。
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

    # 赔率：听牌权益高于跟注门槛却弃牌。
    if kind == "fold" and to_call > 0 \
            and sit.hand_tier in ("强听", "弱听") \
            and sit.equity_pct is not None and pot > 0:
        call = to_call
        if snap.hero_stack is not None and 0 < snap.hero_stack < to_call:
            call = snap.hero_stack
        bep = call / (pot + call) * 100 if pot + call > 0 else 0.0
        if sit.equity_pct > bep + 2:
            tags.add("有赔率却弃牌")

    # 价值尺度：强牌只下 1/3 池。
    if street != "preflop" and agg and amount is not None and pot > 0 \
            and sit.hand_tier in ("强成牌", "超对", "顶对强踢") \
            and prefix == "VALUE DEFAULT" and amount < 0.4 * pot:
        tags.add("价值下注过小")
    return tags


# ── 对比与汇总 ────────────────────────────────────────────────


def classify(new_kind: str, new_amt: float | None,
             act_kind: str, act_amt: float | None,
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


def fmt(kind: str | None, amt: float | None) -> str:
    if kind is None:
        return "—"
    if kind in AGGRESSIVE and amt is not None:
        return f"{kind} {float(amt):g}"
    return kind


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用当前 AI 建议复跑 dev 库复盘手牌")
    ap.add_argument("input", type=Path, help="review_hands 导出的 JSONL / JSON")
    ap.add_argument("--json", type=Path, default=None, help="写出逐决策点 JSON")
    ap.add_argument("--csv", type=Path, default=None, help="写出逐决策点 CSV")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个决策点")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--model-name", default=None, help="覆盖 AI_SERVER_MODEL")
    ap.add_argument("--no-model", action="store_true", help="只跑事实层，不调模型")
    ap.add_argument("--no-dedupe", action="store_true", help="不按内容指纹去重")
    ap.add_argument("--verbose", action="store_true", help="动作不同的逐条打印建议原文")
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

    hands = load_hands(args.input)
    print(f"读到 {len(hands)} 条复盘手牌")

    settings = None
    if not args.no_model:
        load_env()
        settings = AISettings.from_env()
        if settings is None:
            print("没配 AI_SERVER_*：先 cp .env.example .env 填 Key", file=sys.stderr)
            return 2
        if args.model_name:
            from dataclasses import replace
            settings = replace(settings, model=args.model_name)
        proto = "JEV decisions" if is_jev_model(settings.model) else "chat/completions"
        print(f"模型: {settings.model}（{proto}）")

    seen: set[str] = set()
    rows: list[dict] = []
    skipped_dupe = skipped_invalid = 0
    processed = 0

    for h in hands:
        sig = signature(h)
        if not args.no_dedupe and sig in seen:
            skipped_dupe += 1
            continue
        seen.add(sig)
        built = build_decisions(h)
        if built is None:
            skipped_invalid += 1
            print(f"  跳过 {h.get('id')}：盲注/底牌/行动线不足")
            continue
        decisions, bomb = built
        for d in decisions:
            if args.limit and processed >= args.limit:
                break
            snap = d["snap"]
            try:
                sit = build_situation(snap, table_dynamics=dynamics_text)
            except Exception as exc:                # noqa: BLE001
                print(f"  跳过 {snap.hand_id} {snap.street}：{type(exc).__name__}: {exc}")
                continue
            processed += 1
            row = {
                "id": snap.hand_id, "street": snap.street,
                "position": snap.hero_position,
                "cards": " ".join(snap.hero_cards),
                "board": " ".join(snap.board),
                "pot": float(snap.pot or 0.0),
                "to_call": float(snap.to_call or 0.0),
                "stack": snap.hero_stack,
                "n_opp": sit.opponents_in_hand,
                "players": snap.players_dealt,
                "bomb": bool(bomb),
                "tier": sit.hand_tier, "label": sit.hand_label_en,
                "equity_pct": sit.equity_pct,
                "beat_pct": sit.beat_pct,
                "beat_at_least_pct": sit.beat_at_least_pct,
                "nuts": (sit.ground_truth.hero_is_nuts
                         if sit.ground_truth is not None else None),
                "ground_truth": (sit.ground_truth.to_dict()
                                 if sit.ground_truth is not None else None),
                "texture": sit.texture_en,
                "range": sit.range_verdict,
                "directive": _directive_prefix(sit),
                "options": [o.id for o in sit.options],
                "actual": d["actual"], "actual_amt": d["actual_amt"],
                "actual_tags": sorted(audit(sit, d["actual"], d["actual_amt"])),
                "new": None, "new_amt": None, "new_reason": None,
                "new_tags": [], "source": None, "duration_ms": None,
                "error": None, "bucket": None,
            }
            if settings is not None:
                try:
                    res = advise(sit, settings, timeout=args.timeout)
                    row.update({
                        "new": res.option.kind,
                        "new_amt": res.option.amount,
                        "new_reason": res.reason,
                        "new_tags": sorted(audit(sit, res.option.kind,
                                                 res.option.amount)),
                        "source": res.source,
                        "duration_ms": res.duration_ms,
                        "error": res.error,
                    })
                    if res.source == "model":
                        row["bucket"] = classify(
                            res.option.kind, res.option.amount,
                            d["actual"], d["actual_amt"], row["pot"], snap.street)
                except Exception as exc:            # noqa: BLE001
                    row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)

    # ── 打印 ──
    hdr = (f"{'手':>7} {'街':<6} {'位置':<6} {'持牌':<8} {'牌面':<13} "
           f"{'底池':>7} {'实际':<13} {'新建议':<13} {'判定':<8} 违规")
    print("\n" + hdr)
    print("─" * len(hdr))
    for r in rows:
        flags = ",".join(sorted(set(r["actual_tags"]) | set(r["new_tags"])))
        print(f"{r['id']:>7} {r['street']:<6} {(r['position'] or '-'):<6} "
              f"{r['cards']:<8} {r['board']:<13} {r['pot']:>7.1f} "
              f"{fmt(r['actual'], r['actual_amt']):<13} "
              f"{fmt(r['new'], r['new_amt']):<13} {(r['bucket'] or '—'):<8} {flags}")
        if args.verbose and r["bucket"] in ("动作不同", "尺度差"):
            print(f"    └ {r['new_reason']}")
            print(f"      实际违规={r['actual_tags'] or '无'} 新违规={r['new_tags'] or '无'}")

    # ── 汇总 ──
    total = len(rows)
    buckets = {k: sum(1 for r in rows if r["bucket"] == k)
               for k in ("一致", "尺度差", "动作不同")}
    new_viol = [r for r in rows if r["new_tags"]]
    act_viol = [r for r in rows if r["actual_tags"]]
    fixed = [r for r in rows if r["actual_tags"] and not r["new_tags"]]
    regressed = [r for r in rows if r["new_tags"] and not r["actual_tags"]]
    both = [r for r in rows if r["new_tags"] and r["actual_tags"]]
    tag_counts: dict[str, int] = {}
    for r in rows:
        for t in r["new_tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    ms = [r["duration_ms"] for r in rows if r["duration_ms"]]
    ms.sort()

    print(f"\n决策点 {total} 个｜"
          f"一致 {buckets['一致']}｜尺度差 {buckets['尺度差']}｜"
          f"动作不同 {buckets['动作不同']}")
    print(f"跳过：重复 {skipped_dupe} 手，数据不足 {skipped_invalid} 手")
    print(f"新建议违规 {len(new_viol)} 个｜实际动作违规 {len(act_viol)} 个｜"
          f"实际违规→新建议修掉 {len(fixed)}｜新建议新增 {len(regressed)}｜"
          f"两边都违规 {len(both)}")
    if tag_counts:
        print("新建议违规分类：" + "，".join(f"{k}×{v}" for k, v in
                                            sorted(tag_counts.items(), key=lambda x: -x[1])))
    if ms:
        p50 = ms[len(ms) // 2]
        print(f"延迟：p50 {p50}ms，max {ms[-1]}ms（{len(ms)} 次模型调用）")

    if args.json:
        payload = {
            "meta": {
                "input": str(args.input),
                "model": settings.model if settings else None,
                "decisions": total,
                "skipped_duplicates": skipped_dupe,
                "skipped_invalid": skipped_invalid,
                "buckets": buckets,
                "new_violations": {t: sum(1 for r in rows if t in r["new_tags"])
                                   for t in sorted(tag_counts)},
                "actual_violations": len(act_viol),
                "fixed": len(fixed),
                "regressed": len(regressed),
                "both": len(both),
                "latency_p50_ms": ms[len(ms) // 2] if ms else None,
                "latency_max_ms": ms[-1] if ms else None,
            },
            "rows": rows,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"写出 {args.json}")

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        cols = list(rows[0]) if rows else []
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(v, ensure_ascii=False)
                                if isinstance(v, (list, dict)) else v)
                            for k, v in r.items()})
        print(f"写出 {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
