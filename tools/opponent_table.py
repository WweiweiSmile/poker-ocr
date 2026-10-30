#!/usr/bin/env python3
"""把对手画像导出成一张「用户表」。

数据来自 `data/opponents/profiles.json`（由 `rebuild_opponent_profiles.py` 生成），
这里只做宽表展示与导出，不重新计算任何统计。

列的口径：

  · **n**：有翻前行动机会的手数，不是总手数。所有百分比的分母都基于机会数。
  · **开池 EP/MP/LP/盲**：该位置档的**首入加注率**（开池 / 开池机会）。
    位置按档统计而不是具体位置 —— 9 人桌单个位置一个 session 只有几手。
  · **c-bet**：作为翻前最后加注者，翻牌第一个下注是自己。
  · **续打**：翻牌 c-bet 过之后，转牌还下注（只有真的看到转牌才算机会）。
  · **被过牌后下注**：这条街前面全过牌时自己下注的比例。低值 = 他过牌就是没牌。
  · **过牌-加注**：先过牌、后面有人下注、自己加注。
  · **面对下注弃牌**：facing_bets 里的 fold 比例。
  · **开池中位 / 下注中位**：按牌桌当时的底池换算的倍数（不是 BB）。
  · **弱摊牌**：主动摊牌（自己是最后下注方）里，最终只有高牌/一对的比例。

机会数不够的格子一律给 `-`：3 次机会算出来的 0% 会把人带向过度剥削。

用法：

    .venv/bin/python tools/opponent_table.py
    .venv/bin/python tools/opponent_table.py --min-hands 20
    .venv/bin/python tools/opponent_table.py --csv reports/opponents.csv
    .venv/bin/python tools/opponent_table.py --profile data/opponents/profiles.json
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

from poker_ocr.opponent_profile import (  # noqa: E402
    ARCHETYPE_NAMES, TENDENCY_MIN_OPPS,
)

DEFAULT_PROFILE = Path("data/opponents/profiles.json")

# 每个指标的最低机会数。与实时画像用同一套门控，避免「表里的数」和
# 「提示词里的数」对不上。
MIN_OPPS = {
    "open": TENDENCY_MIN_OPPS["open"],
    "cbet": TENDENCY_MIN_OPPS["cbet"],
    "barrel": TENDENCY_MIN_OPPS["barrel"],
    "stab": TENDENCY_MIN_OPPS["stab"],
    "check_raise": TENDENCY_MIN_OPPS["check_raise"],
    "fold_to_bet": 10.0,
    "showdown": 5.0,
    "limp": 8.0,
    "fold_to_cbet": 8.0,
    "fold_to_barrel": 5.0,
    "delay_cbet": 5.0,
    "fold_to_3bet": 5.0,
}

COLUMNS = [
    ("玩家", "name"),
    ("类型", "archetype"),
    ("置信", "confidence"),
    ("n", "n"),
    ("VPIP", "vpip"),
    ("PFR", "pfr"),
    ("3bet", "threebet"),
    ("AF", "af"),
    ("开池EP", "open_ep"),
    ("开池MP", "open_mp"),
    ("开池LP", "open_lp"),
    ("开池盲", "open_blind"),
    ("c-bet", "cbet"),
    ("续打", "barrel"),
    ("被过牌后下注", "stab"),
    ("过牌-加注", "check_raise"),
    ("弃 vs 跟", "fold_to_bet"),
    ("开池中位", "open_median"),
    ("下注中位", "bet_median"),
    ("溜入", "limp"),
    ("对c-bet弃", "fold_to_cbet"),
    ("跟c-bet", "call_to_cbet"),
    ("转牌再弃", "fold_to_barrel"),
    ("延迟c-bet", "delay_cbet"),
    ("被3-bet弃", "fold_to_3bet"),
    ("4-bet", "fourbet"),
    ("摊牌", "showdowns"),
    ("弱摊牌", "weak_showdown"),
    ("炸弹池", "bomb"),
]


def _num(raw, key: str) -> float:
    value = raw.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _ratio(hit: float, opps: float) -> tuple[float, float] | None:
    """(命中, 机会)。表格里机会不够时显示成分数，而不是把整格吞掉 ——
    「0/11」和「-」对判断一个人是完全不同的信息。"""
    # 0.5 个机会（降权后的半手）四舍五入会显示成「0/0」，直接当没有。
    return (hit, opps) if opps >= 1.0 else None


def _pct(ratio: tuple[float, float] | None, kind: str) -> float | None:
    if ratio is None or ratio[1] < MIN_OPPS[kind]:
        return None
    return ratio[0] / ratio[1] * 100


def _cell(ratio: tuple[float, float] | None, kind: str) -> str:
    if ratio is None:
        return "-"
    if ratio[1] < MIN_OPPS[kind]:
        return f"{ratio[0]:.0f}/{ratio[1]:.0f}"
    return f"{ratio[0] / ratio[1] * 100:.0f}%"


def _median(values: list) -> float | None:
    nums = sorted(float(x) for x in values if isinstance(x, (int, float)))
    if not nums:
        return None
    mid = len(nums) // 2
    return nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2


def _bucket_ratio(raw: dict, bucket: str) -> tuple[float, float] | None:
    hit = (raw.get("open_by_bucket") or {}).get(bucket)
    opps = (raw.get("open_opp_by_bucket") or {}).get(bucket)
    hit = float(hit) if isinstance(hit, (int, float)) else 0.0
    opps = float(opps) if isinstance(opps, (int, float)) else 0.0
    return _ratio(hit, opps)


def row_of(name: str, raw: dict, *, pool: str = "") -> dict:
    """画像原始记录 → 表格的一行（数值已换算成百分比 / 中位数）。"""
    threebet_opps = _num(raw, "threebet_opps")
    threebet = _num(raw, "threebet")
    facing = _num(raw, "facing_bets")
    showdowns = _num(raw, "showdowns")
    aggressive = _num(raw, "aggressive_showdowns")
    return {
        "name": raw.get("display_name") or name,
        "key": name,
        "pool": pool,
        "archetype": ARCHETYPE_NAMES.get(raw.get("archetype") or "unknown",
                                         raw.get("archetype") or "unknown"),
        "confidence": _num(raw, "confidence"),
        "n": int(_num(raw, "sample_preflop_opps") or _num(raw, "preflop_opps")),
        "hands": int(_num(raw, "hands")),
        "vpip": _ratio(_num(raw, "vpip"), _num(raw, "preflop_opps")),
        "pfr": _ratio(_num(raw, "pfr"), _num(raw, "preflop_opps")),
        "threebet": _ratio(threebet, threebet_opps),
        # 画像 JSON 不落 af 字段（它是派生值），用原始计数重算，别读一个
        # 不存在的键 —— 那会让整列永远显示 "-"。
        "af": (round(_num(raw, "bets_raises") / max(_num(raw, "calls"), 1.0), 2)
               if (_num(raw, "bets_raises") or _num(raw, "calls")) else None),
        "open_ep": _bucket_ratio(raw, "EP"),
        "open_mp": _bucket_ratio(raw, "MP"),
        "open_lp": _bucket_ratio(raw, "LP"),
        "open_blind": _bucket_ratio(raw, "盲注"),
        "cbet": _ratio(_num(raw, "cbets"), _num(raw, "cbet_opp")),
        "barrel": _ratio(_num(raw, "barrels"), _num(raw, "barrel_opp")),
        "stab": _ratio(_num(raw, "stabs"), _num(raw, "stab_opp")),
        "check_raise": _ratio(_num(raw, "check_raises"),
                              _num(raw, "check_raise_opp")),
        # 只用「弃 + 跟」做分母：facing_bets 会因为部分下注动作没被记录而
        # 偏小，出现过 folds > facing_bets 的不可能比值。弃/跟比不依赖那个
        # 分母，读数稳定。
        "fold_to_bet": _ratio(_num(raw, "folds"),
                              _num(raw, "folds") + _num(raw, "calls")),
        "open_median": _median(raw.get("open_sizes") or []),
        "bet_median": _median(raw.get("bet_sizes") or []),
        "showdowns": int(showdowns),
        "limp": _ratio(_num(raw, "limped"), _num(raw, "limp_opp")),
        "fold_to_cbet": _ratio(_num(raw, "ftc_fold"), _num(raw, "ftc_opp")),
        "call_to_cbet": _ratio(_num(raw, "ftc_call"), _num(raw, "ftc_opp")),
        "fold_to_barrel": _ratio(_num(raw, "ftb_fold"), _num(raw, "ftb_opp")),
        "delay_cbet": _ratio(_num(raw, "dcb"), _num(raw, "dcb_opp")),
        "fold_to_3bet": _ratio(_num(raw, "f3b_fold"), _num(raw, "f3b_opp")),
        "fourbet": _ratio(_num(raw, "f3b_4bet"), _num(raw, "f3b_opp")),
        "weak_showdown": _ratio(_num(raw, "weak_aggressive_showdowns"),
                                aggressive),
        "bomb": int(_num(raw, "bomb_pot_hands")),
    }


def _fmt(value, spec: str = ".0f") -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return format(value, spec)


def load_rows(path: Path, *, min_hands: int = 0) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [row_of(name, raw, pool=str(data.get("pool_key") or ""))
            for name, raw in (data.get("players") or {}).items()]
    rows = [r for r in rows if r["hands"] >= min_hands]
    rows.sort(key=lambda r: (-r["hands"], r["name"]))
    return rows


_KIND = {
    "vpip": "open", "pfr": "open", "threebet": "open",
    "open_ep": "open", "open_mp": "open", "open_lp": "open",
    "open_blind": "open",
    "cbet": "cbet", "barrel": "barrel", "stab": "stab",
    "check_raise": "check_raise", "fold_to_bet": "fold_to_bet",
    "weak_showdown": "showdown",
    "limp": "limp", "fold_to_cbet": "fold_to_cbet",
    "call_to_cbet": "fold_to_cbet", "fold_to_barrel": "fold_to_barrel",
    "delay_cbet": "delay_cbet",
    "fold_to_3bet": "fold_to_3bet", "fourbet": "fold_to_3bet",
}


def render_markdown(rows: list[dict]) -> str:
    """拆成「基础」和「线路」两张表：22 列挤一张没法看。"""
    base_cols = ["玩家", "类型", "n", "VPIP", "PFR", "3bet", "AF",
                 "开池EP", "开池MP", "开池LP", "开池盲"]
    line_cols = ["玩家", "c-bet", "续打", "被过牌后下注", "过牌-加注",
                 "溜入", "对c-bet弃", "跟c-bet", "转牌再弃", "延迟c-bet",
                 "被3-bet弃", "4-bet",
                 "弃 vs 跟", "开池中位", "下注中位", "摊牌", "弱摊牌",
                 "炸弹池"]
    reverse = {label: key for label, key in COLUMNS}

    def table(labels: list[str]) -> list[str]:
        out = ["| " + " | ".join(labels) + " |",
               "|" + "|".join(["---"] * len(labels)) + "|"]
        for row in rows:
            cells = []
            for label in labels:
                key = reverse[label]
                value = row.get(key)
                if key in _KIND:
                    cells.append(_cell(value, _KIND[key]))
                elif label in ("开池中位", "下注中位"):
                    cells.append(_fmt(value, ".2f"))
                else:
                    cells.append(_fmt(value))
            out.append("| " + " | ".join(cells) + " |")
        return out

    lines = [f"共 {len(rows)} 人", "", "### 基础", ""]
    lines.extend(table(base_cols))
    lines.extend(["", "### 翻后线路与尺度", ""])
    lines.extend(table(line_cols))
    lines.extend([
        "",
        "**怎么读**：百分比 = 机会数已经够（开池 8 / c-bet 6 / 续打 5 / "
        "被过牌后下注 6 / 过牌-加注 6 / 面对下注弃牌 10）；"
        "写成 `命中/机会` 的格子样本还不够，别当结论用。",
        "`-` = 完全没有这类机会。",
    ])
    return "\n".join(lines)


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        header = [label for label, _ in COLUMNS] + ["手数", "pool"]
        writer.writerow(header)
        for row in rows:
            cells = []
            for label, key in COLUMNS:
                value = row.get(key)
                if key in _KIND:
                    pct = _pct(value, _KIND[key])
                    cells.append(round(pct, 1) if pct is not None else "")
                elif isinstance(value, float):
                    cells.append(round(value, 2))
                else:
                    cells.append("" if value is None else value)
            cells.extend([row["hands"], row["pool"]])
            writer.writerow(cells)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对手画像导出成用户表")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE,
                        help="画像 JSON 路径（默认 data/opponents/profiles.json）")
    parser.add_argument("--min-hands", type=int, default=0,
                        help="只列出手数 ≥ N 的玩家")
    parser.add_argument("--csv", type=Path, default=None,
                        help="同时写一份宽表 CSV")
    args = parser.parse_args(argv)

    if not args.profile.exists():
        print(f"找不到画像文件：{args.profile}", file=sys.stderr)
        return 1
    rows = load_rows(args.profile, min_hands=args.min_hands)
    if not rows:
        print("没有满足条件的玩家", file=sys.stderr)
        return 1

    print(render_markdown(rows))
    if args.csv:
        write_csv(rows, args.csv)
        print(f"\nCSV 已写入 {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
