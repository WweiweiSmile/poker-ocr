#!/usr/bin/env python3
"""P1：按一个**写明的模型**推导 v1 策略表，写进 tables/*.json 的 v1 变体。

为什么不手填数字
────────────────
P0 把表抽出来之后，第一个诱惑是「填一组看起来更对的数」。那只是把手调的
直觉换成另一组手调的直觉。所以这里不填数，而是先写一个模型，再让数字从
模型里算出来 —— 每一个值都能被复现、被质疑、被改参数重算。

模型（defend model）
────────────────────
单手持牌面对一个下注时：

    p        = size / (pot + 2·size)      # 他的价格（跟注额 / 跟注后的总底池）
    e_t      = 该档对「下注范围」的权益
    R_t      = 该档的权益实现率（IP/OOP、街、人数之外的粗略值）
    继续 c_t = σ((e_t·R_t − p) / τ)       # 价格低于可实现权益就继续
    加注占比 = σ((e_t − θ_raise) / τ_raise)
    call = c·(1−加注占比)，raise = c·加注占比，fold = 1−c

三条能验证的性质：

  1. **单调**：e 越高 → 继续越多；p 越高 → 弃牌越多（σ 保证）；
  2. **空气会弃**：air 的 e·R ≈ 0.08 远低于 p=0.25 → 弃 94%，不是 72%；
  3. **强牌几乎不弃**：nut 的 e·R ≈ 0.86 → 弃 <1%，而原表给 3~8%。

原表（v0）在端点上是「不敢极化」的：强成牌弃 3%、超对弃 10% —— 一个
真正的强成牌面对半个池的下注弃 10% 是说不通的。v1 把这个方向掰回来。

`size_gamma`（价格弹性）也由同一模型算出：γ 定义为
    f(p) = f(p_ref)·(p/p_ref)^γ  ⇒  γ = (p_ref/f̄)·(df̄/dp)
在参考价格上取范围加权平均。v0 的 1.0 是「线性外推」，模型给出的值会
明显不同 —— 这正是要测的东西。

诚实边界
────────
`verified: false`。e_t 和 R_t 是**假设**，不是实测或求解器输出。v1 的作用
是给出一个「有推导、可复现、和 v0 明显不同」的对照，用来回答「表到底是不是
瓶颈」；不是宣称它更准。要变准，需要 `needs` 里写的东西。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DIR = ROOT / "poker_ocr" / "tables"

# ── 模型参数（唯一的一组自由参数，改这里重算全部） ──
PRICE_REF = 0.25          # 参考价格：1/2 池
TAU = 0.06                # 决策噪声：越小越极化
RAISE_CENTER = 0.65       # 加注门槛：权益高于此值倾向加注
TAU_RAISE = 0.10

# 5 档（range_flow 口径）：对下注范围的权益 / 实现率
TIER_ER_5 = {
    "nut":    (0.90, 0.95),
    "strong": (0.75, 0.90),
    "medium": (0.55, 0.80),
    "weak":   (0.40, 0.55),
    "air":    (0.15, 0.50),
}

# 9 档（fold_equity 口径）：权益。实现率不单独假设，直接取
# REALIZATION_BASE × REALIZATION_UNKNOWN_POS（复用已有表，少一组假设）。
TIER_ER_9 = {
    "强成牌":   (0.90, "value"),
    "超对":     (0.75, "value"),
    "顶对强踢": (0.62, "value"),
    "顶对弱踢": (0.48, "value"),
    "中底对":   (0.34, "value"),
    "强听":     (0.45, "draw"),
    "弱听":     (0.30, "draw"),
    "打牌面":   (0.40, "board_play"),
    "空气":     (0.15, "weak"),
}

# 参考范围构成（算 γ 用）：一个典型的「跟注后面对下注」的范围
REFERENCE_MIX = {
    "强成牌": 0.03, "超对": 0.04, "顶对强踢": 0.10, "顶对弱踢": 0.12,
    "中底对": 0.20, "强听": 0.06, "弱听": 0.06, "打牌面": 0.04, "空气": 0.35,
}

REALIZATION_KIND = {"value": 0.90, "draw": 0.82, "board_play": 0.75, "weak": 0.58}
REALIZATION_POS = 0.93      # REALIZATION_UNKNOWN_POS


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _continue(equity: float, realization: float, price: float = PRICE_REF) -> float:
    return _sigmoid((equity * realization - price) / TAU)


def facing_probs_v1() -> dict[str, list[float]]:
    """5 档 → (call, raise, fold)。"""
    out: dict[str, list[float]] = {}
    for tier, (equity, realization) in TIER_ER_5.items():
        cont = _continue(equity, realization)
        raise_share = _sigmoid((equity - RAISE_CENTER) / TAU_RAISE)
        out[tier] = [round(cont * (1.0 - raise_share), 4),
                     round(cont * raise_share, 4),
                     round(1.0 - cont, 4)]
    return out


def _fold_at(tier: str, price: float) -> float:
    equity, kind = TIER_ER_9[tier]
    realization = REALIZATION_KIND[kind] * REALIZATION_POS
    return 1.0 - _continue(equity, realization, price)


def base_fold_v1() -> dict[str, float]:
    """9 档 → 参考价格（1/2 池）下的弃牌概率。"""
    out = {}
    for tier in TIER_ER_9:
        f = _fold_at(tier, PRICE_REF)
        out[tier] = round(min(0.97, max(0.01, f)), 4)
    return out


def size_gamma_v1() -> float:
    """参考价格上的范围加权价格弹性（v0 是 1.0 的线性外推）。"""
    num = den = 0.0
    for tier, weight in REFERENCE_MIX.items():
        f = _fold_at(tier, PRICE_REF)
        dfdp = f * (1.0 - f) / TAU            # σ 的导数
        num += weight * dfdp
        den += weight * f
    if den <= 0:
        return 1.0
    gamma = PRICE_REF * num / den
    return round(min(4.0, max(0.2, gamma)), 3)


def _derivation_text() -> str:
    return (
        "defend model：continue = σ((e·R − p)/τ)，p=0.25（1/2 池），"
        f"τ={TAU}；加注占比 = σ((e − {RAISE_CENTER})/{TAU_RAISE})；"
        "R 取 REALIZATION_BASE × REALIZATION_UNKNOWN_POS（复用已有表）。"
        "e 与 R 是假设，未实测、也无外部参照（verified=false）。"
        "复现：python tools/derive_strategy_tables.py"
    )


def derive(directory: Path) -> int:
    changed = 0
    configs = [
        ("range_flow", "facing_probs", facing_probs_v1(),
         "call/raise/fold 三列，由 defend model 逐档算出"),
        ("fold_equity", "base_fold", base_fold_v1(),
         "参考价格 0.25 下的弃牌概率，由 defend model 逐档算出"),
        ("fold_equity", "size_gamma", size_gamma_v1(),
         "参考价格上的范围加权价格弹性 γ = (p/f̄)·(df̄/dp)"),
    ]
    for kind, name, value, note in configs:
        path = directory / f"{kind}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        entry = doc["tables"][name]
        payload = {
            "source": "derived",
            "verified": False,
            "note": note,
            "derivation": _derivation_text(),
            "needs": ("按价格分桶的对手实测弃牌率（本地样本仅 ~74 次，需要全量库）；"
                      "tier 形状无法从观测数据识别，v1 只用于敏感度实验，不作默认"),
        }
        if name == "size_gamma":
            payload["value"] = value
        elif name == "base_fold":
            payload["rows"] = value
        else:
            payload["columns"] = ["call", "raise", "fold"]
            payload["rows"] = value
        entry["v1"] = payload
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        changed += 1
        print(f"[v1] {kind}.{name}")
        if isinstance(value, dict):
            for k, v in value.items():
                print(f"      {k:10s} {v}")
        else:
            print(f"      γ = {value}")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="推导策略表 v1")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    args = ap.parse_args()
    n = derive(Path(args.dir))
    print(f"\n已写入 {n} 张表的 v1 变体")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
