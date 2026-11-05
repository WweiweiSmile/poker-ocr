"""策略表加载器：把 `poker_ocr/tables/*.json` 读成模块常量。

背景（P0/P1）
─────────────
`range_flow` 与 `fold_equity` 里原本写死 9 张表、约 150 个数字：它们决定
「对手范围长什么样」和「他会不会弃」，也就决定了后面所有 EV。这些数字是
手写的，代码里没有任何地方能回答「为什么是 0.20 不是 0.35」。抽成 JSON 之后：

  · 每个数字都有 `source`（hand-tuned / derived / empirical）与 `verified`；
  · 同一张表可以有多个变体，用 profile 切换，不改代码；
  · A/B 只需要切一个环境变量。

profile 与变体
──────────────
JSON 顶层 `profiles` 把「人读的名字」映射到变体 id：

    "hand-tuned" -> v0（原始手调值，默认）
    "sourced"    -> v1（P1 按推导公式重算的值）

某张表没有当前 profile 的变体时**回落到 v0**，并在 `TableEntry.fell_back`
里标出来 —— 这样 P1 可以只覆盖两张表，其余自动沿用原值，不会因为漏填而
让模块 import 失败。

用法
────
    from .strategy_tables import rows
    _FACING_PROBS = rows("range_flow", "facing_probs")

    # 显式指定变体（测试/对比工具用，不读环境变量）
    v1 = rows("range_flow", "facing_probs", profile="sourced")

环境变量
────────
    STRATEGY_TABLES_PROFILE   hand-tuned | sourced（默认 hand-tuned）
    STRATEGY_TABLES_DIR       覆盖表目录（实验用；默认 poker_ocr/tables）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

PROFILE_ENV = "STRATEGY_TABLES_PROFILE"
DIR_ENV = "STRATEGY_TABLES_DIR"
DEFAULT_PROFILE = "hand-tuned"
DEFAULT_DIR = Path(__file__).resolve().parent / "tables"

# 人读的 profile 名 → 变体 id。也接受直接写变体 id。
_PROFILE_ALIASES = {
    "hand-tuned": "v0", "hand_tuned": "v0", "default": "v0", "v0": "v0",
    "baseline": "v0",
    "sourced": "v1", "derived": "v1", "v1": "v1",
}


@dataclass(frozen=True)
class TableEntry:
    """一张表在某个 profile 下解析出来的最终结果。"""

    kind: str
    name: str
    variant: str                 # 实际用的变体（可能因回落而不等于 profile 的目标）
    requested: str               # profile 解析出的目标变体
    source: str                  # hand-tuned | derived | empirical
    verified: bool
    note: str
    needs: str
    columns: tuple[str, ...]
    rows: Mapping[str, Any]
    items: tuple[Any, ...]
    value: Any
    fell_back: bool = False

    @property
    def is_provenance_known(self) -> bool:
        """有出处（derived/empirical 且已标注 verified）才算「有依据」。"""
        return self.source != "hand-tuned" and self.verified

    def row(self, key: str) -> Any:
        return self.rows[key]


def profile() -> str:
    """当前 profile（环境变量优先，未设置时 hand-tuned）。"""
    raw = (os.environ.get(PROFILE_ENV) or "").strip()
    return raw or DEFAULT_PROFILE


def variant_of(profile_name: str | None = None) -> str:
    name = (profile_name or profile()).strip().lower()
    try:
        return _PROFILE_ALIASES[name]
    except KeyError:
        raise KeyError(
            f"未知 profile {name!r}；可选：hand-tuned（v0）| sourced（v1）") from None


def tables_dir() -> Path:
    raw = (os.environ.get(DIR_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_DIR


@lru_cache(maxsize=8)
def _load_kind(kind: str, directory: str) -> dict:
    path = Path(directory) / f"{kind}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"策略表缺失：{path}\n"
            f"用 `python tools/export_strategy_tables.py` 生成，"
            f"或用 {DIR_ENV} 指定目录。")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if int(doc.get("schema", 0)) != 1:
        raise ValueError(f"{path}: 不支持的 schema {doc.get('schema')!r}")
    return doc


def _resolve(doc: dict, kind: str, name: str, target: str) -> tuple[dict, str, bool]:
    try:
        variants = doc["tables"][name]
    except KeyError:
        raise KeyError(f"{kind} 里没有表 {name!r}；现有：{sorted(doc['tables'])}") from None
    if target in variants:
        return variants[target], target, False
    default = doc.get("default_variant") or "v0"
    if default in variants:
        return variants[default], default, True
    first = next(iter(variants))
    return variants[first], first, True


def table(kind: str, name: str, profile: str | None = None) -> TableEntry:
    """取一张表的最终解析结果。"""
    directory = str(tables_dir())
    doc = _load_kind(kind, directory)
    target = variant_of(profile)
    entry, variant, fell_back = _resolve(doc, kind, name, target)
    return TableEntry(
        kind=kind,
        name=name,
        variant=variant,
        requested=target,
        source=str(entry.get("source", "unknown")),
        verified=bool(entry.get("verified", False)),
        note=str(entry.get("note", "")),
        needs=str(entry.get("needs", "")),
        columns=tuple(entry.get("columns") or ()),
        rows=dict(entry.get("rows") or {}),
        items=tuple(entry.get("items") or ()),
        value=entry.get("value"),
        fell_back=fell_back,
    )


def rows(kind: str, name: str, profile: str | None = None) -> dict[str, Any]:
    """向量表的行（深拷贝，调用方可以安全地改成 tuple/dict）。"""
    entry = table(kind, name, profile)
    out: dict[str, Any] = {}
    for key, value in entry.rows.items():
        if isinstance(value, list):
            out[key] = tuple(float(v) for v in value)
        elif isinstance(value, dict):
            out[key] = {k: float(v) for k, v in value.items()}
        else:
            out[key] = float(value)
    return out


def items(kind: str, name: str, profile: str | None = None) -> tuple[Any, ...]:
    """有序列表（例如 tier_percentile、default_sizes）。"""
    raw = table(kind, name, profile).items
    return tuple(tuple(v) if isinstance(v, list) else v for v in raw)


def value(kind: str, name: str, profile: str | None = None) -> Any:
    """标量表（例如 price_ref、size_gamma）。"""
    got = table(kind, name, profile).value
    return got


def catalog(kind: str | None = None, profile: str | None = None) -> list[TableEntry]:
    """列出所有表及其来源，用于 CLI 与复盘。"""
    kinds = [kind] if kind else ["range_flow", "fold_equity"]
    out: list[TableEntry] = []
    for k in kinds:
        doc = _load_kind(k, str(tables_dir()))
        for name in doc["tables"]:
            out.append(table(k, name, profile))
    return out


def int_keys(kind: str, name: str) -> bool:
    """这张表的键在 JSON 里是字符串、在代码里应是 int（例如街因子）。"""
    entry = table(kind, name)
    return all(key.lstrip("-").isdigit() for key in entry.rows) if entry.rows else False


def reset_cache() -> None:
    _load_kind.cache_clear()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="列出策略表、来源与覆盖情况")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--kind", default=None, choices=["range_flow", "fold_equity"])
    args = ap.parse_args()
    target = variant_of(args.profile)
    print(f"profile={args.profile or profile()!s} → variant={target}")
    print(f"dir={tables_dir()}")
    for entry in catalog(args.kind, args.profile):
        flag = "回落" if entry.fell_back else "    "
        verified = "✓" if entry.verified else "✗"
        size = len(entry.rows) if entry.rows else len(entry.items) or 1
        print(f"{flag} {entry.kind:12s} {entry.name:28s} {entry.variant:3s} "
              f"{entry.source:10s} {verified} n={size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
