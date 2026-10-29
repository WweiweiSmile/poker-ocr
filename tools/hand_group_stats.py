#!/usr/bin/env python3
"""按「爆炸底池 / 普通牌局」分组统计手牌，回答「当前桌面该怎么调」而不是「赢了多少」。

为什么必须分组：这个桌子的强制投入里，炸弹池只占约 1/10 的手数，却吃掉一半以上的
前注成本；两者的打法要求完全不同（炸弹池 SPR≈2，只能打坚果；普通池还有翻后空间）。
混在一起统计，两边的信号会互相抵消。

口径说明（都是刻意的）：

  · **分组判据**优先看落库的 `bomb_pot` 标记（追踪器已经综合了屏幕横幅和
    「ante==5 且翻前无动作」两条证据）；老数据没有这个键时，退回结构判据：
    翻前没有任何行动、且起始底池是人数的整数倍 5BB。
  · **净值用相邻两手的 `hero_stack_bb` 相减**，不是从底池反推。只统计**同一个
    session 内**的相邻手：跨 session 的跳变是重买/补码，不是输赢，混进来会把
    重买算成一次大胜。差额超过起始筹码、或赢得比底池还多的，判定为读数异常直接丢弃。
  · **净值覆盖率会打印出来**。炸弹池的起始筹码常在交了 5BB 前注之后才被读到，
    所以炸弹池的净值比普通池更容易缺样本 —— 结论要按覆盖率打折看。
  · **VPIP / PFR 只用「主动投钱」判定**，过牌（check）不算。炸弹池没有翻前行动，
    它的 VPIP 恒为 0 是结构决定的，不是打法，别拿它跟普通池比。
  · **样本量**：n<30 的组只看方向，不要看均值。炸弹池每 5 分钟才一次，
    一个 session 通常只有 10-20 手，单组净值基本是噪声。

用法：

    .venv/bin/python tools/hand_group_stats.py
    .venv/bin/python tools/hand_group_stats.py logs/live.states.jsonl
    .venv/bin/python tools/hand_group_stats.py hands/1023.hands.json
    .venv/bin/python tools/hand_group_stats.py --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

DEFAULT_SOURCES = (Path("logs/live.states.jsonl"),)

# 爆炸底池的强制前注：每人 5BB。和 hand_track.BOMB_ANTE_BB 同口径。
BOMB_ANTE_BB = 5.0

# 净资产/主动入池判定用的动作集合。
VOLUNTARY = ("call", "raise", "bet")
AGGRESSIVE = ("bet", "raise")


def load_hands(path: Path) -> list[dict]:
    """读一手一条的 live 日志，或手牌导出（JSON 数组）。

    live 日志里只有部分记录是手牌，其余是逐帧状态；这里只留 `type == "hand"`，
    并按出现顺序给每手打上 `_session`（第几个 session_start，0 起），供跨 session
    的净值对齐使用。JSON 数组来源没有 session 概念，全部记为 0。
    """
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        hands = json.loads(text)
        for hand in hands:
            hand["_session"] = 0
        return hands

    hands: list[dict] = []
    session = -1
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = record.get("type")
        if kind == "session_start":
            session += 1
        elif kind == "hand":
            record["_session"] = session
            hands.append(record)
    return hands


def _preflop(hand: dict) -> dict | None:
    for street in hand.get("streets") or []:
        if street.get("street") == "preflop":
            return street
    return None


def is_bomb_pot(hand: dict) -> bool:
    """这手是不是爆炸底池。

    结构判据优先，不迷信落库标记：横幅会串到下一手，实测 19 个 `bomb_pot`
    标记里有 6 个其实是普通翻前手牌（街前底池 4.5、有 4-11 个翻前行动）。
    炸弹池的「翻前」只是每人交 5BB 前注、**按构造没有任何行动**，所以有翻前
    行动就一律不算。

    没有行动的翻前再看底池：炸弹池总额 = 人数 × 5BB，必然是 5 的整数倍，且
    至少 5 人参与（≥25BB）；普通大池的人均可能超过 5BB，但不会正好落在
    5 的整数倍上。
    """
    pre = _preflop(hand)
    if pre is None:
        return bool(hand.get("bomb_pot"))
    if pre.get("actions"):
        return False
    pot_start = pre.get("pot_start") or 0.0
    if pot_start >= BOMB_ANTE_BB * 5 and pot_start % BOMB_ANTE_BB == 0:
        return True
    return bool(hand.get("bomb_pot"))


def has_hero_data(hand: dict) -> bool:
    """有 hero 手牌、且至少有一条街，才算一手能统计的牌。

    换手空档会留下「没有 hero 手牌、没有行动线」的壳记录（见 live._write_hand），
    它们会把分母撑大、把 VPIP 稀释掉。
    """
    return bool(hand.get("hero_cards")) and bool(hand.get("streets"))


def hero_actions(hand: dict, street: str | None = None) -> list[dict]:
    out = []
    for item in hand.get("streets") or []:
        if street is not None and item.get("street") != street:
            continue
        for action in item.get("actions") or []:
            if action.get("is_hero") or action.get("seat_id") == "me":
                out.append(action)
    return out


def estimate_nets(hands: list[dict]) -> tuple[dict[int, float], int]:
    """用相邻两手的起始筹码差估每手净值；返回 (下标→净值, 被丢弃的异常数)。

    只在同一 session 内相邻才有效：跨 session 的差值是重买/补码。手牌记录的顺序
    就是时间的顺序，所以「相邻两条手牌记录」= 「相邻两手」，除非中间漏了一手 ——
    漏手会表现为一次跨多手的跳变，无法识别，只能靠覆盖率兜底。
    """
    nets: dict[int, float] = {}
    dropped = 0
    for index in range(len(hands) - 1):
        current, following = hands[index], hands[index + 1]
        if current.get("_session") != following.get("_session"):
            continue
        start = current.get("hero_stack_bb")
        end = following.get("hero_stack_bb")
        if start is None or end is None:
            continue
        delta = round(float(end) - float(start), 2)
        if abs(delta) > float(start):          # 输掉超过手里的筹码：读数错误
            dropped += 1
            continue
        if delta > float(current.get("pot") or 0.0) + 1.0:
            dropped += 1                      # 赢得比底池还多：读数错误
            continue
        nets[index] = delta
    return nets, dropped


def summarize(hands: list[dict], nets: dict[int, float],
              indices: list[int]) -> dict:
    """一组手牌的核心指标。indices 是这一组在 hands 里的下标。"""
    n = len(indices)
    if not n:
        return {"hands": 0}

    vpip = pfr = postflop = aggressive = 0
    pots: list[float] = []
    for index in indices:
        hand = hands[index]
        pre = [a for a in hero_actions(hand, "preflop") if a.get("action") in VOLUNTARY]
        # 翻后单独数一遍：hero_actions 按街过滤，传 None 是全部街
        post = []
        for street in hand.get("streets") or []:
            if street.get("street") == "preflop":
                continue
            for action in street.get("actions") or []:
                if not (action.get("is_hero") or action.get("seat_id") == "me"):
                    continue
                if action.get("action") in VOLUNTARY:
                    post.append(action)

        vpip += bool(pre)
        pfr += any(a.get("action") == "raise" for a in pre)
        postflop += bool(post)
        aggressive += any(a.get("action") in AGGRESSIVE for a in post)
        pots.append(float(hand.get("pot") or 0.0))

    values = [nets[i] for i in indices if i in nets]
    total = round(sum(values), 2)
    mean = round(statistics.mean(values), 3) if values else None
    stdev = round(statistics.stdev(values), 2) if len(values) > 1 else None
    return {
        "hands": n,
        "vpip": round(vpip / n, 4),
        "pfr": round(pfr / n, 4),
        "postflop_continue": round(postflop / n, 4),
        "postflop_aggressive": round(aggressive / n, 4),
        "pot_median": round(statistics.median(pots), 2) if pots else 0.0,
        "net_n": len(values),
        "net_total": total,
        "net_mean": mean,
        "net_stdev": stdev,
        "net_se": round(stdev / (len(values) ** 0.5), 2) if stdev and values else None,
    }


def build_report(hands: list[dict]) -> dict:
    usable = [i for i, hand in enumerate(hands) if has_hero_data(hand)]
    nets, dropped = estimate_nets(hands)
    bombs = [i for i in usable if is_bomb_pot(hands[i])]
    regulars = [i for i in usable if not is_bomb_pot(hands[i])]
    skipped = len(hands) - len(usable)
    return {
        "total_hands": len(hands),
        "usable_hands": len(usable),
        "skipped_no_hero": skipped,
        "dropped_net_readings": dropped,
        "bomb": summarize(hands, nets, bombs),
        "regular": summarize(hands, nets, regulars),
        "all": summarize(hands, nets, usable),
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None, unit: str = "") -> str:
    return "—" if value is None else f"{value:+.2f}{unit}"


def format_report(report: dict) -> str:
    """把报告排成一张表。样本量和净值覆盖率一起打，避免拿噪声当结论。"""
    lines: list[str] = []
    lines.append(
        f"手牌 {report['total_hands']} 条：可统计 {report['usable_hands']}，"
        f"无 hero 手牌跳过 {report['skipped_no_hero']}，"
        f"净值读数异常丢弃 {report['dropped_net_readings']}")
    lines.append("")

    header = f"{'分组':<8}{'手数':>6}{'占比':>8}{'VPIP':>8}{'PFR':>8}" \
             f"{'翻后继续':>10}{'翻后主动':>10}{'中位底池':>10}" \
             f"{'净值/手':>10}{'净值合计':>10}{'净值覆盖':>10}"
    lines.append(header)
    lines.append("─" * 88)

    total = max(report["usable_hands"], 1)
    for key, label in (("bomb", "爆炸底池"), ("regular", "普通牌局"), ("all", "全部牌局")):
        group = report[key]
        if not group.get("hands"):
            lines.append(f"{label:<8}{0:>6}")
            continue
        coverage = group["net_n"] / group["hands"] if group["hands"] else 0.0
        lines.append(
            f"{label:<8}{group['hands']:>6}{group['hands'] / total:>7.0%}"
            f"{_pct(group['vpip']):>8}{_pct(group['pfr']):>8}"
            f"{_pct(group['postflop_continue']):>10}{_pct(group['postflop_aggressive']):>10}"
            f"{group['pot_median']:>10.1f}"
            f"{_num(group['net_mean']):>10}{_num(group['net_total']):>10}"
            f"{coverage:>9.0%}")

    lines.append("")
    bombs = report["bomb"]
    if bombs.get("hands"):
        share_hands = bombs["hands"] / max(report["usable_hands"], 1)
        lines.append(
            f"炸弹池占 {share_hands:.0%} 的手数；"
            f"每组净值只在同 session 内相邻手才有值，"
            f"覆盖率低于 50% 的组不要看均值。")
    lines.append(
        "注意：炸弹池的起始筹码有时在交 5BB 前注之前读到、有时之后，"
        "净值误差可达每手 ±5BB —— 炸弹池的净值只看正负，别抠小数点。")
    lines.append("提示：n<30 的组只能看方向。炸弹池一个 session 通常只有 10-20 手。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按爆炸底池/普通牌局分组统计手牌")
    parser.add_argument("source", nargs="?", type=Path,
                        help="live.states.jsonl 或 hands.json；默认 logs/live.states.jsonl")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    source = args.source
    if source is None:
        source = next((p for p in DEFAULT_SOURCES if p.exists()), None)
    if source is None or not source.exists():
        print(f"找不到手牌来源：{source}", file=sys.stderr)
        return 1

    hands = load_hands(source)
    if not hands:
        print(f"{source} 里没有手牌记录", file=sys.stderr)
        return 1

    report = build_report(hands)
    report["source"] = str(source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"来源：{source}")
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
