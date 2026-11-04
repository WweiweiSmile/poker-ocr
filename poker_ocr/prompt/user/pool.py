"""池子先验：逐座位画像缺失时的兜底，**不是这个座位**。

2026-11-04.4：紧弱的主要成因之一是「没有逐座位实测 → 只能过牌」。攻击块和
攻击口径都要求「每个仍能行动的对手」有数据；现场画像通常只有 6–12 次机会，
于是最常见的节点反而落到「没有数据」。这一段在**逐座位数据缺失**时补一条
池子级弃牌率（本手在局对手的历史画像合并，退而用对手库的全场加权），让
「他弃 ≥ 需要」这件事仍然可算，并明写这是池子均值、不是这个座位。

只在三个条件同时成立时渲染：翻后、没人下注（要轮到我们决定下不下注）、
至少一名在局对手没有该街的实测。攻击块已经给裁决的节点不渲染，避免同
一份 prompt 出现两套结论。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from ...hand_track import PREFLOP
from ...opponent_profile import DEFAULT_PRIORS
from ...pot_math import _can_act

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...situation import Situation

_STREET_CN = {"flop": "翻牌", "turn": "转牌", "river": "河牌"}
# 机会数低于这个值时不用池子口径 —— 十几次机会算出来的「池子均值」比没有更误导。
_MIN_OPPS = 20.0
# 单街、不考虑被跟后的权益：三个常用尺寸的盈亏平衡弃牌率。
_BREAKEVEN = (("1/3", 25), ("1/2", 33), ("2/3", 40))


def _db_path() -> Path:
    env = os.environ.get("OPPONENT_DB")
    if env:
        return Path(env)
    return (Path(__file__).resolve().parents[3]
            / "data" / "opponents" / "profiles.json")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=4)
def _load_pool(path_str: str, mtime: float) -> dict:
    """对手库的全场加权：面对下注的弃/跟/加。样本 <20 手的玩家不计入。"""
    try:
        raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    players = raw.get("players") or {}
    fold = {s: 0.0 for s in _STREET_CN}
    total = {s: 0.0 for s in _STREET_CN}
    count = hands = 0
    for value in players.values():
        if not isinstance(value, dict) or (value.get("hands") or 0) < 20:
            continue
        count += 1
        hands += int(value.get("hands") or 0)
        for street in _STREET_CN:
            ff = (value.get("face_fold") or {}).get(street, 0.0)
            fc = (value.get("face_call") or {}).get(street, 0.0)
            fr = (value.get("face_raise") or {}).get(street, 0.0)
            fold[street] += ff
            total[street] += ff + fc + fr
    return {"players": count, "hands": hands, "fold": fold, "total": total}


def _pool_rate(sit: "Situation", street: str):
    """本手在局对手的合并值优先；不够就用对手库全场加权；再退默认先验。"""
    stats = sit.table_stats
    if stats is not None:
        pct = getattr(stats, f"fold_to_bet_{street}", None)
        opps = (stats.opportunities or {}).get(street)
        if pct is not None and (opps or 0) >= _MIN_OPPS:
            return (pct, opps,
                    f"本手在局对手的历史画像合并（{stats.players} 人有画像）")
    path = _db_path()
    data = _load_pool(str(path), _mtime(path))
    hit = (data.get("fold") or {}).get(street, 0.0)
    total = (data.get("total") or {}).get(street, 0.0)
    if total >= _MIN_OPPS:
        return (hit / total * 100, total,
                f"对手库全场加权（{data['players']} 人 / {data['hands']} 手）")
    pct = DEFAULT_PRIORS.get(f"fold_to_bet_{street}")
    return (pct, None, "默认先验（样本不足）") if pct is not None else (None, None, "")


def _has_gap(sit: "Situation", street: str) -> bool:
    """至少一名在局、还能行动的对手缺该街实测（含被先验收缩的估计）。"""
    reads = {r.seat_id: r for r in sit.opponent_reads}
    metric = f"fold_to_bet_{street}"
    for opponent in sit.snap.opponents:
        if opponent.folded or not _can_act(opponent):
            continue
        read = reads.get(opponent.seat_id)
        if read is None or read.archetype == "unknown":
            return True
        if getattr(read, f"{metric}_pct", None) is None:
            return True
        if metric in read.prior_dominated:
            return True
    return False


def render_pool_prior_zh(sit: "Situation") -> list[str]:
    """「池子先验」段；只在翻后、没人下注、且逐座位有缺口时出现。"""
    snap = sit.snap
    street = snap.street
    if street == PREFLOP:
        return []
    if (snap.to_call or 0.0) > 0:
        return []          # 面对下注：先验给的是「他们弃」，不是我们该不该下注
    if not any(o.kind in ("bet", "raise") for o in sit.options):
        return []
    if not _has_gap(sit, street):
        return []
    pct, opps, source = _pool_rate(sit, street)
    if pct is None:
        return []
    cn = _STREET_CN.get(street, street)
    n = f"（{opps:.0f} 次机会）" if opps else ""
    # 池子均值是「每个对手」的弃牌率；多人池要连乘，不能直接当开火许可。
    fold = pct / 100.0
    chain = "、".join(
        f"{k} 家 ≈{fold ** k * 100:.0f}%" for k in (2, 3, 4))
    return [
        "## 池子先验（逐座位数据缺失时的兜底，不是这个座位）",
        f"- 面对{cn}下注弃 {pct:.0f}%{n}；口径：{source}",
        "- 对照：" + "、".join(f"{label} 池需要 {bep}%" for label, bep in _BREAKEVEN)
        + " 弃牌率才 +EV（单街，不算被跟后的权益）",
        f"- 多人池按连乘打折（独立粗算）：{chain}；"
        "3 家以上只用来支撑价值/强听小注，空气不要拿这条开火",
        "- 用法：某个座位没有该街实测时按这条估计，"
        "不要因为「没有这个人的数据」就默认过牌；大注仍要 L2–L3 与实测",
    ]
