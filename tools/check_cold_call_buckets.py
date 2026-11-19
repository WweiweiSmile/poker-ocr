#!/usr/bin/env python3
"""冷跟分桶表一致性检查（票 #78）。

单一数值来源是两处，本工具负责证明它们和运行时代码三方一致：

1. `poker_ocr/tables/cold_call_buckets.json` —— 运行时 token 列表（`ranges.get`
   只从这里读 3/4 人池档，Python 里不再抄一份）；
2. `docs/冷跟先验-按人数分桶-草案.md` §2.4 的 13×13 逐格表 —— 评审看的形状；
3. `poker_ocr/ranges.py` 现有的 2 人池表 —— 本票不改，逐位回归。

检查项（任一失败退出码 1）：

- 逐档：JSON token 展开出的 169 键 == 草案表格里「该档还留」的键集合；
- 逐档：combo 数 / 宽度 / 键数 == JSON 里写的 `expected`（宽度按 combo/1326）；
- 嵌套：tier4 ⊂ tier3 ⊂ tier2（只删不加，草案 §2.2）；
- 2 人池：JSON 的 baseline 指向的表 == 草案表格的 tier2 键集合（现状表没被改）；
- 运行时：`ranges.get("COLD_CALL_{IP|OOP}[_vs_*][_n3|_n4]")` 的宽度与上表一致。

    nv tools/check_cold_call_buckets.py          # 逐项打印，失败退出 1
    nv tools/check_cold_call_buckets.py -q       # 只在失败时打印
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from poker_ocr import cold_call_tiers, ranges  # noqa: E402

DOC = ROOT / "docs" / "冷跟先验-按人数分桶-草案.md"
_RANKS = "AKQJT98765432"
_GRID_MARK = "13×13 逐格表"


def _grid_rows(section: str) -> list[list[str]]:
    """从一个侧（IP/OOP）的 markdown 段里取出 13 行表格。"""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("| **"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    return rows


def parse_draft_grid(text: str) -> dict[str, dict[str, int]]:
    """草案 §2.4 → {侧: {169 键: 能玩到的最大池子档}}。

    表的上三角是同花、对角线是对子、下三角是非同花（文档里写明的读法）。
    `·` = 现状表里没有这个键。
    """
    start = text.index(_GRID_MARK)
    body = text[start:]
    ip_at = body.index("**IP**")
    oop_at = body.index("**OOP**")
    out: dict[str, dict[str, int]] = {}
    for side, chunk in (("IP", body[ip_at:oop_at]),
                        ("OOP", body[oop_at:])):
        # OOP 段到下一个 "### " 标题为止，避免把后面的表格混进来。
        chunk = chunk.split("\n### ", 1)[0]
        rows = _grid_rows(chunk)
        if len(rows) != len(_RANKS):
            raise SystemExit(f"{side}: 期待 {len(_RANKS)} 行表格，读到 {len(rows)} 行")
        keys: dict[str, int] = {}
        for i, row in enumerate(rows):
            if row[0] != f"**{_RANKS[i]}**":
                raise SystemExit(f"{side}: 第 {i + 1} 行应是 **{_RANKS[i]}**，"
                                 f"实际 {row[0]}")
            cells = row[1:]
            if len(cells) != len(_RANKS):
                raise SystemExit(f"{side}: 第 {i + 1} 行应有 {len(_RANKS)} 格，"
                                 f"实际 {len(cells)}")
            for j, cell in enumerate(cells):
                if cell == "·":
                    continue
                if cell not in ("2", "3", "4"):
                    raise SystemExit(f"{side}: {_RANKS[i]}{_RANKS[j]} 格值非法 {cell!r}")
                if i == j:                       # 对角线 = 对子
                    key = _RANKS[i] * 2
                elif j > i:                      # 上三角 = 同花，高张在前
                    key = f"{_RANKS[i]}{_RANKS[j]}s"
                else:                            # 下三角 = 非同花
                    key = f"{_RANKS[j]}{_RANKS[i]}o"
                keys[key] = int(cell)
        out[side] = keys
    return out


def _keys_of(tokens) -> set[str]:
    return set(ranges.expand_tokens(tuple(tokens)))


def _keys_of_range(rid: str) -> set[str]:
    ran = ranges.get(rid)
    if ran is None:
        raise SystemExit(f"{rid} 解析不出范围")
    return {ranges.key_of_combo(c) for c in ran.combos()}


def _combos(keys) -> int:
    return sum(len(ranges.combos_of_key(k)) for k in sorted(keys))


def check() -> list[str]:
    """返回失败清单；空清单 = 全部一致。"""
    fails: list[str] = []
    table = json.loads(cold_call_tiers.TABLE_FILE.read_text())
    grid = parse_draft_grid(DOC.read_text())

    for side in ("IP", "OOP"):
        draft = grid[side]
        keep = {tier: {k for k, v in draft.items() if v >= tier}
                for tier in (cold_call_tiers.TIER_MIN, 3, 4)}
        # 嵌套子集：只删不加
        if not keep[4] <= keep[3] <= keep[2]:
            fails.append(f"{side}: 档位不是嵌套子集（tier4⊂tier3⊂tier2 不成立）")
        for tier in (cold_call_tiers.TIER_MIN, 3, 4):
            entry = table["tiers"][side][str(tier)]
            want = entry["expected"]
            if tier == cold_call_tiers.TIER_MIN:
                # 2 人池档 = 现状表，token 为空，键集合来自 ranges.py 自己
                keys = _keys_of_range(entry["baseline"])
                rid = entry["baseline"]
            else:
                if not entry.get("tokens"):
                    fails.append(f"{side} n{tier}: JSON 里没有 token 列表")
                    continue
                keys = _keys_of(tuple(entry["tokens"]))
                rid = f"COLD_CALL_{side}_n{tier}"
            if keys != keep[tier]:
                only_doc = sorted(keep[tier] - keys)
                only_json = sorted(keys - keep[tier])
                fails.append(
                    f"{side} n{tier}: 键集合与草案表格不一致 "
                    f"(草案多 {only_doc[:8]}…，JSON 多 {only_json[:8]}…)"
                    if only_doc or only_json else
                    f"{side} n{tier}: 键集合与草案表格不一致")
                continue
            combos = _combos(keys)
            width = round(combos / ranges.TOTAL_COMBOS * 100, 2)
            if combos != want["combos"]:
                fails.append(f"{side} n{tier}: combo {combos} != expected "
                             f"{want['combos']}")
            if len(keys) != want["keys"]:
                fails.append(f"{side} n{tier}: 键数 {len(keys)} != expected "
                             f"{want['keys']}")
            if width != want["width_pct"]:
                fails.append(f"{side} n{tier}: 宽度 {width} != expected "
                             f"{want['width_pct']}")
            # 运行时把同一份 token 读出来，宽度必须一致（三种 id 形态）
            for prefix in (f"COLD_CALL_{side}", f"COLD_CALL_{side}_vs_EP",
                           f"COLD_CALL_{side}_vs_LP"):
                rid_t = rid if tier == cold_call_tiers.TIER_MIN else (
                    f"{prefix}_n{tier}")
                if tier == cold_call_tiers.TIER_MIN:
                    rid_t = prefix
                ran = ranges.get(rid_t)
                if ran is None:
                    fails.append(f"{rid_t}: 解析不出范围")
                    continue
                if round(ran.width_pct, 2) != width:
                    fails.append(f"{rid_t}: 运行时宽度 {ran.width_pct:.2f} "
                                 f"!= 表里 {width}")
    return fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="默认行为；显式写出来是给 CI 用的")
    ap.add_argument("-q", "--quiet", action="store_true", help="只在失败时打印")
    args = ap.parse_args(argv)

    fails = check()
    if fails:
        print("❌ 冷跟分桶表不一致：", file=sys.stderr)
        for item in fails:
            print(f"  - {item}", file=sys.stderr)
        return 1
    if not args.quiet:
        table = json.loads(cold_call_tiers.TABLE_FILE.read_text())
        for side in ("IP", "OOP"):
            for tier in (2, 3, 4):
                want = table["tiers"][side][str(tier)]["expected"]
                print(f"✅ {side} n{tier}: {want['combos']} combo / "
                      f"{want['width_pct']:.2f}% / {want['keys']} 键")
        print("✅ 冷跟分桶表与草案 §2.4、运行时 ranges 三方一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
