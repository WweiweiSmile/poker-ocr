"""池子先验：逐座位画像缺失时的兜底，**不是这个座位**。

2026-11-04.4：紧弱的主要成因之一是「没有逐座位实测 → 只能过牌」。攻击块和
攻击口径都要求「每个仍能行动的对手」有数据；现场画像通常只有 6–12 次机会，
于是最常见的节点反而落到「没有数据」。这一段在**逐座位数据缺失**时补一条
池子级弃牌率（本手在局对手的历史画像合并，退而用对手库的全场加权），让
「他弃 ≥ 需要」这件事仍然可算，并明写这是池子均值、不是这个座位。

只在三个条件同时成立时渲染：翻后、没人下注（要轮到我们决定下不下注）、
至少一名在局对手没有该街的实测。攻击块已经给裁决、或已经有条件弃牌率
报告（fold_eq）的节点不渲染 —— 后者已经按本手线路给出「全部弃牌」，
再补一条历史均值会让同一节点出现两个口径的弃牌率。

2026-11-08.2：连乘按**本手实际还能弃牌的对手数**，不再只列 2/3/4 家。
h·7e7022c1d3 的 9 人炸弹池：8 个对手、池子均值 71%，全弃只有 ≈6%；
旧文案只列到「4 家 ≈26%」，模型拿它当「25% 门槛仍够」，用 K 高卡顺
在翻牌对 8 家下注 1/3。现在直接给出本手人数的全弃概率，低于 1/3 门槛
就写明「不能当开火依据」；空气/弱听在 3 家以上明确默认过牌。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from ...coach import WEAK_DRAW
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


def _live_opponents(sit: "Situation") -> int:
    """还能弃牌的对手数：已弃牌/已全下的不算。

    弃牌率只对「还能行动的人」有意义；全下的人不能弃，不能算进连乘。
    """
    return sum(1 for opponent in sit.snap.opponents
               if not opponent.folded and _can_act(opponent))


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
    n_live = _live_opponents(sit)
    if n_live <= 0:
        return []          # 没人还能弃牌：这条先验没有决策意义
    cn = _STREET_CN.get(street, street)
    n = f"（{opps:.0f} 次机会）" if opps else ""
    # 池子均值是「每个对手」的弃牌率；多人池要按本手实际人数连乘。
    # 只列到 4 家会让 9 人池误以为「4 家 26% > 25%，仍够开火」。
    fold = pct / 100.0
    all_fold = fold ** n_live
    if n_live == 1:
        lead = f"- 本手 1 名对手弃牌 ≈{pct:.0f}%"
        compare = ""
    else:
        small = [f"{k} 家 ≈{fold ** k * 100:.0f}%" for k in (2, 3, 4)
                 if k < n_live]
        lead = (f"- 多人池按连乘打折（独立粗算）："
                f"本手 {n_live} 名对手全弃 ≈{all_fold * 100:.0f}%")
        compare = f"；对照 {'、'.join(small)}" if small else ""
    if all_fold < 0.25:
        verdict = "低于 1/3 门槛，不能当开火依据（纯诈唬/弱听；价值另看更差的牌会不会跟）"
    else:
        verdict = "高于 1/3 门槛，但仍是无条件历史均值"
    weak_hand = sit.hand_tier == WEAK_DRAW or (
        sit.hand_tier is not None and sit.hand_class == "air")
    lines = [
        "## 池子先验（逐座位数据缺失时的兜底，不是这个座位）",
        f"- 面对{cn}下注弃 {pct:.0f}%{n}；口径：{source}",
        "- 对照：" + "、".join(f"{label} 池需要 {bep}%" for label, bep in _BREAKEVEN)
        + " 弃牌率才 +EV（单街，不算被跟后的权益）",
        f"{lead}，{verdict}{compare}",
    ]
    if weak_hand and n_live >= 3:
        lines.append(
            "- 本手是空气/弱听：3 家以上这条先验只支撑价值/强听小注，"
            "不要拿它开火；默认过牌。")
    lines.append(
        "- 用法：某个座位没有该街实测时按这条估计，"
        "不要因为「没有这个人的数据」就默认过牌；人数按本手实际对手数连乘；"
        "对手已经跟过注/加注时范围更窄，第二枪/第三枪按收窄后的范围重估。")
    return lines
