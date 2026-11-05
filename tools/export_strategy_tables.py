#!/usr/bin/env python3
"""策略表 P0：把写死在模块里的策略常数导出成 JSON，并逐表标注来源。

为什么做这件事
──────────────
`range_flow` 与 `fold_equity` 里有 9 张表大约 150 个数字，它们决定了
「对手范围长什么样」和「他会不会弃」，也就决定了后面所有 EV。但这些数字
是手写的，代码里没有任何地方能回答「为什么是 0.20 不是 0.35」。抽成 JSON
之后，每个数字至少能被追问、被替换、被 A/B。

两种用法
────────
1. **引导（只跑一次）**：模块还写死常数时，
   `.venv/bin/python tools/export_strategy_tables.py`
   生成 `poker_ocr/tables/{range_flow,fold_equity}.json`，值逐字复制，
   不四舍五入（用 repr 语义，float 往返无损）。

2. **校验（重构之后随时跑）**：
   `.venv/bin/python tools/export_strategy_tables.py --check`
   断言「模块当前读到的表」与「JSON 里的值」逐项相等。这是「重构没有改变
   行为」的机器证据 —— 不靠人眼比对。

来源字段的口径（不要美化）
──────────────────────────
  · `hand-tuned`：手写常数，代码注释自述「方向单调、量级够用、可手写」；
  · `derived`：能从公式推出来的（例如参考价格 0.25 = 1/2 池的价格）；
  · `empirical`：来自实测/文档结论（例如 α=1.0 由 §12.4 的实测决定）。

`verified` 一律诚实标注：没有求解器对照或对手实测校准的表，全部 false。
派生成 v1 时保留原表，另起变体，不改默认值。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poker_ocr import fold_equity as fe  # noqa: E402
from poker_ocr import range_flow as rf  # noqa: E402

DEFAULT_DIR = ROOT / "poker_ocr" / "tables"

# 表清单：属性名 → (JSON 表名, 形状, 列名, 元数据)。
# 形状：rows=以档位为键的向量表；items=有序列表；value=标量。
_MANIFEST: list[tuple[str, str, str, tuple[str, ...] | None, dict[str, Any]]] = [
    # ── range_flow：对手范围的事件模型 ──
    ("_FACING_PROBS", "facing_probs", "rows",
     ("call", "raise", "fold"),
     {
         "source": "hand-tuned",
         "verified": False,
         "note": ("面对下注时各成牌档的 call/raise/fold 频率，按合法动作集归一化。"
                  "模块注释自述：数值不追求精确，追求方向单调、量级够用、可手写。"),
         "needs": "外部频率表（书里的节点频率）或对手实测；tier 级只能靠先验，不可从观测识别",
     }),
    ("_UNFACING_PROBS", "unfacing_probs", "rows",
     ("check", "bet"),
     {
         "source": "hand-tuned",
         "verified": False,
         "note": "无人下注时各成牌档的 check/bet 频率。",
         "needs": "同上",
     }),
    ("_DRAW_PROBS", "draw_probs", "rows",
     ("check", "bet", "call", "raise", "fold"),
     {
         "source": "hand-tuned",
         "verified": False,
         "note": ("听牌对四动作的乘性调整。作用是把「跟注/下注」抬起来、把「弃牌」"
                  "压下去，使纯花听面对 2/3 池几乎不弃，而同样归进空气的纯空气会弃。"),
         "needs": "各听牌类型的继续频率（书里的表或实测）",
     }),
    ("_BUCKET_TIER", "bucket_tier", "rows",
     ("nut", "strong", "medium", "weak", "air"),
     {
         "source": "hand-tuned",
         "verified": False,
         "note": ("10 个线路桶 × 5 个成牌档的乘数。只作用于非过牌动作；"
                  "设计 §4.2 把 3 组拆成 5 档，是为了让价值组向坚果集中。"),
         "needs": "各线路的范围构成（例如 donk-jam 里坚果/两对的真实占比）",
     }),
    ("_SPR_AGGRESSION", "spr_aggression", "rows", None,
     {
         "source": "hand-tuned",
         "verified": False,
         "note": "SPR 只调主动进攻倾向：短码时中等成牌也愿意打光（设计 §4.3）。",
         "needs": "SPR 分档下的投入倾向实测",
     }),
    ("_ARCH_BLUFF", "arch_bluff", "rows", None,
     {
         "source": "hand-tuned",
         "verified": False,
         "note": "画像只调两个旋钮之一：诈唬系数。",
         "needs": "分画像的诈唬成功率实测",
     }),
    ("_ARCH_CALL", "arch_call", "rows", None,
     {
         "source": "hand-tuned",
         "verified": False,
         "note": "画像只调两个旋钮之二：跟注宽度。",
         "needs": "分画像的跟注宽度实测",
     }),
    ("_TIER_PERCENTILE", "tier_percentile", "items",
     ("percentile", "tier"),
     {
         "source": "hand-tuned",
         "verified": False,
         "note": ("成牌档的牌面百分位阈值。绝对牌型在不同牌面上不是一回事，"
                  "所以档位按「这手牌在该牌面上比多少 combo 大」定。"),
         "needs": "无通用来源；这张表依赖牌面分布的定义，改动前先确认口径",
     }),
    ("_DRAW_STREET_FACTOR", "draw_street_factor", "rows", None,
     {
         "source": "derived",
         "verified": True,
         "note": ("四二法则的相对比值（翻牌 4/4、转牌 2/4、河牌 0/4）。"
                  "绝对倍数在按动作归一化时会约掉，所以只存比值。"),
         "int_keys": True,
     }),
    ("_MIN_LIKELIHOOD", "min_likelihood", "value", None,
     {
         "source": "hand-tuned",
         "verified": False,
         "note": "似然下限：不把「必弃」写成 0，否则一个动作就能把整类手牌清零。",
     }),
    ("_SUPPORT_FLOOR", "support_floor", "value", None,
     {
         "source": "hand-tuned",
         "verified": False,
         "note": ("相对支撑集下限：连乘后低于「最大权重 × 此值」的 combo 退出范围。"
                  "只影响渲染宽度与尾部质量（<1%），不影响权益主排序。"),
     }),
    ("_DEFAULT_ALPHA", "default_alpha", "value", None,
     {
         "source": "empirical",
         "verified": True,
         "note": ("事件温度 α。设计稿默认 0.7，实测 3 个动作下会压掉信息，"
                  "改成 1.0 + 按动作数分段降温（见 §12.4）。"),
     }),
    # ── fold_equity：弃牌率与 EV ──
    ("BASE_FOLD", "base_fold", "rows", None,
     {
         "source": "hand-tuned",
         "verified": False,
         "note": ("各手牌类别面对 1/2 池（price=0.25）时的弃牌概率。"
                  "`_calibration_scale` 的 docstring 明写：BASE_FOLD 是先验表，"
                  "绝对水平没有依据 —— 只靠 scale 校准水平，形状无人校准。"),
         "needs": "按价格分桶的实测弃牌率（tier 形状不可识别，只能当先验）",
     }),
    ("_PRICE_REF", "price_ref", "value", None,
     {
         "source": "derived",
         "verified": True,
         "note": "参考价格：1/2 池下注时他跟注的价格 = 0.5/(1+2×0.5) = 0.25。",
     }),
    ("SIZE_GAMMA", "size_gamma", "value", None,
     {
         "source": "hand-tuned",
         "verified": False,
         "note": ("价格弹性：price 翻倍时弃牌倾向放大 2^γ 倍。γ=1.0 是线性外推，"
                  "真人不是线性弃牌的 —— 这张表直接决定「该用多大尺寸诈唬」。"),
         "needs": "同一对手在不同尺寸下的弃牌率（尺寸弹性），对手库能直接量",
     }),
    ("_FOLD_MIN", "fold_min", "value", None,
     {"source": "hand-tuned", "verified": False,
      "note": "钳制下限：不把「必跟」当成确定事件。"}),
    ("_FOLD_MAX", "fold_max", "value", None,
     {"source": "hand-tuned", "verified": False,
      "note": "钳制上限：不把「必弃」当成确定事件。"}),
    ("DEFAULT_SIZES", "default_sizes", "items", None,
     {"source": "hand-tuned", "verified": False,
      "note": "没有真实选项时的默认考察尺寸（底池比例）。"}),
    ("REALIZATION_BASE", "realization_base", "rows", None,
     {
         "source": "hand-tuned",
         "verified": False,
         "note": ("四类手牌的权益实现率。经验值：IP 强成牌接近全额，OOP 空气只有一半多。"
                  "不乘它，跟注 EV 会系统性高估。"),
         "needs": "分档实现率实测（按街/位置拆）",
     }),
    ("REALIZATION_IP", "realization_ip", "value", None,
     {"source": "hand-tuned", "verified": False, "note": "有位置的实现率乘数。"}),
    ("REALIZATION_OOP", "realization_oop", "value", None,
     {"source": "hand-tuned", "verified": False, "note": "无位置的实现率乘数。"}),
    ("REALIZATION_UNKNOWN_POS", "realization_unknown_pos", "value", None,
     {"source": "hand-tuned", "verified": False, "note": "位置未知时的实现率乘数。"}),
    ("REALIZATION_PER_EXTRA_OPPONENT", "realization_per_extra_opponent", "value", None,
     {"source": "hand-tuned", "verified": False, "note": "每多一个对手的实现率乘数。"}),
    ("_REAL_MIN", "realization_min", "value", None,
     {"source": "hand-tuned", "verified": False, "note": "实现率钳制下限。"}),
    ("_REAL_MAX", "realization_max", "value", None,
     {"source": "hand-tuned", "verified": False, "note": "实现率钳制上限。"}),
    ("_STREET_DISCOUNT", "street_discount", "rows", None,
     {
         "source": "derived",
         "verified": True,
         "note": "还剩几条下注街决定折扣深度：河牌 0、转牌 0.5、翻牌 1.0（只削减折扣量）。",
     }),
]

KIND_OF_ATTR = {
    "_FACING_PROBS": "range_flow",
    "_UNFACING_PROBS": "range_flow",
    "_DRAW_PROBS": "range_flow",
    "_BUCKET_TIER": "range_flow",
    "_SPR_AGGRESSION": "range_flow",
    "_ARCH_BLUFF": "range_flow",
    "_ARCH_CALL": "range_flow",
    "_TIER_PERCENTILE": "range_flow",
    "_DRAW_STREET_FACTOR": "range_flow",
    "_MIN_LIKELIHOOD": "range_flow",
    "_SUPPORT_FLOOR": "range_flow",
    "_DEFAULT_ALPHA": "range_flow",
}

_SOURCE_MODULE = {"range_flow": rf, "fold_equity": fe}


def _kind(attr: str) -> str:
    return KIND_OF_ATTR.get(attr, "fold_equity")


def _to_jsonable(shape: str, value: Any, int_keys: bool = False) -> Any:
    if shape == "value":
        return float(value) if isinstance(value, (int, float)) else value
    if shape == "items":
        return [list(item) if isinstance(item, tuple) else item for item in value]
    if shape == "rows":
        out = {}
        for key, val in value.items():
            key_s = str(int(key)) if int_keys else str(key)
            if isinstance(val, dict):
                out[key_s] = {k: float(v) for k, v in val.items()}
            elif isinstance(val, (list, tuple)):
                out[key_s] = [float(v) for v in val]
            else:
                out[key_s] = float(val)
        return out
    raise ValueError(f"unknown shape {shape!r}")


def _from_jsonable(shape: str, payload: Any, int_keys: bool = False) -> Any:
    if shape == "value":
        return payload
    if shape == "items":
        return [tuple(item) if isinstance(item, list) else item for item in payload]
    if shape == "rows":
        out = {}
        for key, val in payload.items():
            k = int(key) if int_keys else key
            if isinstance(val, dict):
                out[k] = {kk: float(vv) for kk, vv in val.items()}
            elif isinstance(val, list):
                out[k] = tuple(float(v) for v in val)
            else:
                out[k] = float(val)
        return out
    raise ValueError(f"unknown shape {shape!r}")


def _load_table_modules() -> dict[str, Any]:
    """取模块当前的常数（重构前是字面量，重构后是 JSON 读出来的值）。"""
    out = {}
    for attr, name, shape, _cols, meta in _MANIFEST:
        module = _SOURCE_MODULE[_kind(attr)]
        value = getattr(module, attr, None)
        if value is None:
            raise SystemExit(f"模块里找不到 {module.__name__}.{attr}")
        out[name] = {"shape": shape, "int_keys": bool(meta.get("int_keys")),
                     "value": value}
    return out


def export(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    by_kind: dict[str, dict[str, Any]] = {"range_flow": {}, "fold_equity": {}}
    for attr, name, shape, cols, meta in _MANIFEST:
        kind = _kind(attr)
        entry: dict[str, Any] = {
            "source": meta["source"],
            "verified": bool(meta.get("verified", False)),
            "note": meta.get("note", ""),
        }
        if meta.get("needs"):
            entry["needs"] = meta["needs"]
        if cols:
            entry["columns"] = list(cols)
        value = getattr(_SOURCE_MODULE[kind], attr)
        if shape == "value":
            entry["value"] = float(value)
        elif shape == "items":
            entry["items"] = _to_jsonable(shape, value)
        else:
            entry["rows"] = _to_jsonable(shape, value, meta.get("int_keys", False))
        # 保留已有变体（v1 由 derive 工具生成）：只重写 v0，不推翻其它 profile。
        variants: dict[str, Any] = {}
        old_path = directory / f"{kind}.json"
        if old_path.exists():
            try:
                old = json.loads(old_path.read_text(encoding="utf-8"))
                variants = dict((old.get("tables") or {}).get(name) or {})
            except (OSError, ValueError):
                variants = {}
        variants.pop("v0", None)
        by_kind[kind][name] = {"v0": entry, **variants}

    written = []
    for kind, tables in by_kind.items():
        doc = {
            "schema": 1,
            "kind": kind,
            "profiles": {"hand-tuned": "v0", "sourced": "v1"},
            "default_variant": "v0",
            "tables": tables,
        }
        path = directory / f"{kind}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        written.append(path)
    return written


def check(directory: Path) -> int:
    """逐值比对模块常数与 JSON；不同则打印差异并返回 1。"""
    bad = 0
    for attr, name, shape, _cols, meta in _MANIFEST:
        kind = _kind(attr)
        path = directory / f"{kind}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        try:
            entry = doc["tables"][name]["v0"]
        except KeyError:
            print(f"[MISS] {kind}.{name}")
            bad += 1
            continue
        expected = getattr(_SOURCE_MODULE[kind], attr)
        if shape == "value":
            actual = entry["value"]
        elif shape == "items":
            actual = _from_jsonable(shape, entry["items"])
        else:
            actual = _from_jsonable(shape, entry["rows"], meta.get("int_keys", False))
        if _normalize(actual) != _normalize(expected):
            print(f"[DIFF] {kind}.{name}")
            print(f"   json : {_normalize(actual)!r}")
            print(f"   code : {_normalize(expected)!r}")
            bad += 1
        else:
            print(f"[ OK ] {kind}.{name}")
    return 1 if bad else 0


def _normalize(value: Any) -> Any:
    """把 tuple/list、dict 统一成可比较的形状（float 用 repr 精度）。"""
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return repr(value)
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description="导出/校验策略表 JSON（P0）")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="输出目录")
    ap.add_argument("--check", action="store_true",
                    help="校验模块常数与 JSON 逐值相等（重构后应全 OK）")
    args = ap.parse_args()
    directory = Path(args.dir)
    if args.check:
        return check(directory)
    for path in export(directory):
        print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
