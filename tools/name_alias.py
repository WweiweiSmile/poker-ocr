#!/usr/bin/env python3
"""名字别名：把「同一个人被 OCR 读成了两种拼法」并回一条画像。

## 为什么需要手工工具

词表（`poker_ocr/name_lexicon.py`）只在**其中一种拼法还没入库**时才有用。
历史数据里已经裂开的那种（实测 `台风来咯咯` 68 手 / `台风来吧多` 16 手）
两边都是「精确命中」，词表不会替你合并 —— 合并必须有个人说「这俩是同一个人」。

## 用法

    # 看哪些人的名字像同一个（只读，不改文件）
    .venv/bin/python tools/name_alias.py --suspects

    # 合并：把「别名」的样本并进「规范名字」
    .venv/bin/python tools/name_alias.py --add 台风来吧多=台风来咯咯

    # 查看现有别名
    .venv/bin/python tools/name_alias.py --list

合并会：① 把别名条目的统计并进目标条目（保留目标的手数/画像）；
② 写 `aliases[别名] = 目标`，之后 `_resolve_key` / 实时链路的词表都会认；
③ 原子落盘。合并前会把原文件备份成 `profiles.json.bak-<时间戳>`。
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poker_ocr.name_lexicon import duplicate_pairs  # noqa: E402
from poker_ocr.opponent_profile import (  # noqa: E402
    DEFAULT_PROFILE_PATH, load_profile_store,
)


def _load(path: Path):
    store = load_profile_store(path)
    if store.load_error:
        sys.exit(f"画像读取失败：{store.load_error}")
    return store


def list_aliases(store) -> int:
    if not store.aliases:
        print("（没有别名）")
        return 0
    for alias, target in sorted(store.aliases.items()):
        old = store.players.get(alias)
        new = store.players.get(target)
        print(f"  {alias!r} → {target!r}"
              + (f"   别名条目仍在（{int(old.hands)} 手）" if old else "")
              + (f"   目标 {int(new.hands)} 手" if new else "   目标不存在"))
    return 0


def suspects(store, *, threshold: float = 0.6, limit: int = 40) -> int:
    """列出「很像但不是同一条」的成对名字。只读。"""
    pairs = duplicate_pairs(store.known_names(min_hands=1.0),
                            threshold=threshold)
    if not pairs:
        print(f"（没有相似度 ≥ {threshold:g} 的名字对）")
        return 0
    pairs.sort(reverse=True)
    print(f"相似度 ≥ {threshold:g} 的名字对（{len(pairs)} 组）：")
    for ratio, a, b in pairs[:limit]:
        ha = int(store.players[a].hands) if a in store.players else 0
        hb = int(store.players[b].hands) if b in store.players else 0
        print(f"  {ratio:.3f}  {a!r}（{ha} 手）  {b!r}（{hb} 手）")
    print("\n确认是同一个人的，用 --add 别名=规范名字 合并；"
          "是两个人的，忽略即可（实时链路只在其中一个名字没入库时才会纠正）。")
    return 0


def add_alias(store, spec: str, *, merge: bool = True) -> int:
    if "=" not in spec:
        sys.exit("--add 的格式是 别名=规范名字（例如 台风来吧多=台风来咯咯）")
    alias, target = (part.strip() for part in spec.split("=", 1))
    if not alias or not target:
        sys.exit("--add 两边的名字都不能为空")
    if target not in store.players:
        # 允许把别名并到一个「只在手牌记录里出现过、画像里没有」的名字上？
        # 不允许：没有目标条目就没有可合并的统计，先确认名字拼对。
        close = difflib.get_close_matches(target, list(store.players), n=3,
                                          cutoff=0.5)
        hint = f"；相近的有：{close}" if close else ""
        sys.exit(f"目标名字 {target!r} 不在画像库里{hint}")

    backup = store.path.with_suffix(
        store.path.suffix + f".bak-{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copyfile(store.path, backup)

    old = store.players.get(alias)
    before = int(old.hands) if old else 0
    target_before = int(store.players[target].hands)
    if not store.add_alias(alias, target, merge=merge):
        sys.exit("别名没有生效（目标不存在或名字为空）")
    if not store.save(force=True):
        sys.exit(f"落盘失败：{store.last_save_error}")
    after = int(store.players[target].hands)
    print(f"✅ {alias!r} → {target!r}")
    print(f"   {target!r} 手数 {target_before} → {after}"
          f"（并入 {before}）")
    print(f"   备份：{backup}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对手画像的名字别名工具")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH,
                    help=f"画像文件（默认 {DEFAULT_PROFILE_PATH}）")
    ap.add_argument("--list", action="store_true", help="列出已有别名")
    ap.add_argument("--suspects", action="store_true",
                    help="列出相似度高的名字对（只读，不改文件）")
    ap.add_argument("--threshold", type=float, default=0.6,
                    help="--suspects 的相似度门槛（默认 0.6）")
    ap.add_argument("--add", metavar="别名=规范名字",
                    help="把别名的样本并进规范名字（会先备份再落盘）")
    ap.add_argument("--no-merge", action="store_true",
                    help="只写别名关系，不合并样本")
    args = ap.parse_args(argv)

    store = _load(args.profile)
    if not store.players:
        sys.exit(f"画像库是空的：{store.path}")
    if args.add:
        return add_alias(store, args.add, merge=not args.no_merge)
    if args.suspects:
        return suspects(store, threshold=args.threshold)
    return list_aliases(store)


if __name__ == "__main__":
    sys.exit(main())
