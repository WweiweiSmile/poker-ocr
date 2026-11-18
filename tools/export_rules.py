#!/usr/bin/env python3
"""规则台账的校验 / 导出工具。

    nv tools/export_rules.py --check        # 校验，退出码 0/1（CI 用）
    nv tools/export_rules.py                # 列出当前台账
    nv tools/export_rules.py --dir <目录>   # 换一个台账目录

`--check` 做设计 §3 列的六件事，任何一条不过就把**规则 id** 打出来并退出 1：

  1. schema：槽位 / 街 / node / why / source / confidence 的形状；
  2. id 唯一且前缀与街一致（`pf.` 只出现在翻前）；
  3. 前提里的谓词在 `rules.PREDICATES` 白名单内，算子与取值类型对得上，
     且必须含 `street`、至少两条、区间不能自相矛盾；
  4. 尺寸只能引用 `rule_params` 表（`strategy_tables.table()` 能解析），
     禁止字面数字与其他 kind（设计 §2.2 的 size 槽白名单）；
  5. 来源要给书名 + 页码（或实测说明）与 `verbatim|rebuilt`；
  6. 同槽位规则的前提不能有交集（要靠双向 `coexist_with` 才能共存）。

空台账（M0）是合法状态：`--check` 打一行 OK 就退 0。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from poker_ocr.rules import DEFAULT_LEDGER_DIR, RuleError, load_ledger  # noqa: E402


# 设计 §2.2：size 槽只允许引用书里的数字表；range_flow / fold_equity 是
# 范围与赔率模型，不是「这条规则下多大」的参数。
SIZE_KINDS = frozenset({"rule_params"})


def _check_size_kinds(rules) -> list[str]:
    """size 槽的 kind 白名单（loader 保持通用解析，策略在这里锁）。"""
    errors: list[str] = []
    for rule in rules:
        size = rule.size
        if not size:
            continue
        kind = size.get("table")
        name = size.get("name")
        if kind not in SIZE_KINDS:
            errors.append(
                f"[{rule.id}] size 只能引用 rule_params，"
                f"实际 {kind}.{name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 / 导出规则台账")
    parser.add_argument("--dir", default=str(DEFAULT_LEDGER_DIR),
                        help="台账目录（默认 poker_ocr/rules/ledger）")
    parser.add_argument("--check", action="store_true",
                        help="只校验，不打印明细；不合法时退出 1")
    args = parser.parse_args()

    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"[MISS] 台账目录不存在：{directory}")
        return 1

    try:
        rules = load_ledger(directory)
    except RuleError as exc:
        for error in exc.errors:
            print(error)
        print(f"\n{len(exc.errors)} 处不合法")
        return 1

    size_errors = _check_size_kinds(rules)
    if size_errors:
        for error in size_errors:
            print(error)
        print(f"\n{len(size_errors)} 处不合法")
        return 1

    if args.check:
        print(f"[ OK ] {directory}：{len(rules)} 条规则全部通过校验")
        return 0

    print(f"{directory}：{len(rules)} 条规则")
    for rule in rules:
        print(f"  {rule.id}  [{rule.slot}/{rule.street}]  {rule.node}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
