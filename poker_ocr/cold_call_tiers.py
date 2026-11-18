"""冷跟先验按「入池人数」分档（票 #78，规格 #74）。

为什么要有这一层
────────────────
冷跟者面对的人数越多，他的范围越紧：开池者之外还有 1 家、2 家已经进池时，
同一个座位拿被统治的弱同花宽张冷跟的比例会明显下降（草案 §3 逐格论证，
依据 `docs/冷跟先验-按人数分桶-草案.md`，票 #77）。票 #78 之前
`ranges.get()` 把 `COLD_CALL_IP_vs_<位置>` 的位置后缀整个丢掉，所有位置、
所有人数都返回同一张 20.2%（IP）/ 16.7%（OOP）的表。

口径（与草案 §2.1 逐字一致，别在这里自创）
────────────────────────────────────────
    n_way(S) = 1（开池者）
             + S 之前（不含 S）所有自愿进池的翻前 call / limp / all-in-call
             + 1（S 自己）
    tier     = min(4, max(2, n_way))

  · **只数已经确定在池里的人**。`behind`（还没响应加注的人）不算：那是
    「可能会跟」的估计，算进去等于在猜。`behind` 仍留在文案与 tight_reasons。
  · **人数取冷跟动作发生那一刻的入池人数**，不是「当前帧在局人数」。冷跟是
    翻前动作，之后的跟注/弃牌都不该改写当时的口径；用当前帧人数会在有人
    翻后弃牌后把同一手牌的档位越算越松。
  · 两种数据都拿不到（只有当前帧、没有翻前动作序列，或这个座位没有可确认的
    翻前跟注）时 → `ColdCall(tier=None)`：**不猜档**，调用方退回无后缀的
    `COLD_CALL_*`（= 现状数值）并在事实里标「未定」。不要退回
    `random_opponents` 那种均匀随机牌：它比冷跟先验宽得多，会把 behind
    系统性压低（票 #77 §6.4 已量化，方向正好相反）。

数值来源（单一来源，别在 Python 里再抄一份）
──────────────────────────────────────────
  · 2 人池档：`poker_ocr/ranges.py` 的 `COLD_CALL_IP` / `COLD_CALL_OOP`，
    票 #78 一个字都不改（验收要求逐位不变）；
  · 3/4 人池档：`poker_ocr/tables/cold_call_buckets.json` 的 token 列表，
    本模块只负责读，不持有任何 token；`tools/check_cold_call_buckets.py`
    负责「JSON ↔ 草案 ↔ 实际展开」三方一致。

ID 形状（草案 §6.2，命名由本票定）
──────────────────────────────────
    COLD_CALL_{IP|OOP}[_vs_{EP|MP|LP|SB}][_n{3|4}]

  无 `_n` 后缀 = 2 人池档 = 现状；`_vs_` 段保留（位置轴仍同宽，票 #77 §4.3）。
  `parse_range_id()`/`with_tier()` 是这条形状的唯一实现，翻前与翻后两条链路
  都走它，不许各写一份。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

# 与 hand_track.PREFLOP 同值；不 import 它，避免 ranges ↔ hand_track 的环。
_PREFLOP = "preflop"

TABLE_FILE = Path(__file__).resolve().parent / "tables" / "cold_call_buckets.json"

TIER_MIN = 2
TIER_MAX = 4

# hero 在快照里没有 seat_id；动作记录用 is_hero 区分（与 field_ranges 同约定）。
HERO_SEAT = "__hero__"

# 档位来源。事实块/复盘 facts 里写出来，读的人不用猜这个档是怎么来的。
BASIS_ACTION_ORDER = "preflop_action_order"   # 按翻前动作线数出来的人
BASIS_UNDETERMINED = "undetermined"           # 拿不到可靠人数：不猜档

_RANGE_ID_RE = re.compile(
    r"^COLD_CALL[_-](?P<ip_oop>IP|OOP)"
    r"(?:_VS[_-](?P<opener>EP|MP|LP|SB|BB))?"
    r"(?:_N?(?P<tier>[234]))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ColdCall:
    """一个冷跟座位用哪一档。

    `tier is None` 表示人数未定（证据不足）：调用方必须退回无后缀先验，
    并在事实里标「未定」，不能拿 2 人池档冒充「就是 2 人池」。
    """

    tier: int | None
    n_way: int | None = None
    basis: str = BASIS_UNDETERMINED

    @property
    def known(self) -> bool:
        return self.tier is not None

    @property
    def suffix(self) -> str:
        return tier_suffix(self.tier)


UNDETERMINED = ColdCall(tier=None, basis=BASIS_UNDETERMINED)


# ─────────────────────────── 展开用的表 ───────────────────────────

@lru_cache(maxsize=4)
def _table() -> Mapping[str, Any]:
    raw = json.loads(TABLE_FILE.read_text(encoding="utf-8"))
    return raw


def reset_cache() -> None:
    """测试/重放用：丢掉已解析的表，下一次读重新读文件。"""
    _table.cache_clear()


def _tier_row(ip_oop: str, tier: int) -> Mapping[str, Any] | None:
    side = _table().get("tiers", {}).get((ip_oop or "").upper(), {})
    row = side.get(str(int(tier)))
    return row if isinstance(row, Mapping) else None


def tokens_for(ip_oop: str, tier: int) -> tuple[str, ...]:
    """该侧该档的 token 列表；2 人池档返回 ()（现状表在 ranges.py）。"""
    if int(tier) <= TIER_MIN:
        return ()
    row = _tier_row(ip_oop, tier)
    tokens = (row or {}).get("tokens")
    if not isinstance(tokens, (list, tuple)):
        return ()
    return tuple(str(tok) for tok in tokens)


# ─────────────────────────── 档位选择 ───────────────────────────

def tier_from_n_way(n_way: int | None) -> int | None:
    """入池人数 → 档位。`None`/读不出 → None（不猜）。

    这是全仓唯一的档位映射：翻前 hero 决策点与翻后对手范围都调它。
    """
    if n_way is None:
        return None
    try:
        value = int(n_way)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return min(TIER_MAX, max(TIER_MIN, value))


def preflop_entry_order(snap: Any) -> tuple[str, ...]:
    """翻前自愿进池的座位顺序（call / limp / all-in-call，按动作记录顺序）。

    hero 用 `HERO_SEAT` 占位（快照里 hero 没有 seat_id）。没有翻前动作序列
    时返回 ()：调用方据此判「人数未定」，不要从当前帧人数反推。
    """
    order: list[str] = []
    for record in getattr(snap, "streets", ()) or ():
        if getattr(record, "street", "") != _PREFLOP:
            continue
        for action in getattr(record, "actions", ()) or ():
            if getattr(action, "action", "") != "call":
                continue
            if getattr(action, "is_hero", False):
                order.append(HERO_SEAT)
            else:
                seat = getattr(action, "seat_id", None)
                if seat:
                    order.append(str(seat))
    return tuple(order)


def _seat_key(seat_id: str | None) -> str:
    """hero 的动作记录没有 seat_id，调用方用 HERO_SEAT 显式指代。"""
    return HERO_SEAT if str(seat_id or "").strip() == HERO_SEAT else str(seat_id or "")


def tier_for_seat(snap: Any, seat_id: str | None) -> ColdCall:
    """某座位**冷跟那一刻**的入池人数 → 档位。

    取这个座位在翻前动作线里的**最后一次跟注**：只有在它之前（不含它）
    已经进池的人 + 开池者 + 它自己，才是它冷跟时面对的人数。找不到它的
    跟注、或整手没有翻前动作序列 → `UNDETERMINED`（不猜）。
    """
    key = _seat_key(seat_id)
    order = preflop_entry_order(snap)
    index = -1
    for position in range(len(order) - 1, -1, -1):   # 最后一次跟注
        if order[position] == key:
            index = position
            break
    if index < 0:
        return UNDETERMINED
    n_way = 1 + index + 1
    tier = tier_from_n_way(n_way)
    if tier is None:
        return UNDETERMINED
    return ColdCall(tier=tier, n_way=n_way, basis=BASIS_ACTION_ORDER)


def tier_for_n_way(n_way: int | None) -> ColdCall:
    """已知人数的调用方（翻前 hero 决策点）用的构造器：同一套映射。"""
    tier = tier_from_n_way(n_way)
    if tier is None:
        return UNDETERMINED
    return ColdCall(tier=tier, n_way=int(n_way), basis=BASIS_ACTION_ORDER)


# ─────────────────────────── ID 形状 ───────────────────────────

def tier_suffix(tier: int | None) -> str:
    """档位 → id 后缀。2 人池/未定都是空串（= 现状 id）。"""
    if tier is None or int(tier) <= TIER_MIN:
        return ""
    return f"_n{int(tier)}"


def with_tier(prior_id: str, tier: int | None) -> str:
    """把档位后缀接到先验 id 末尾（幂等：已有 `_n3/_n4` 先摘掉）。

    只对 `COLD_CALL_*` 生效；其他 id 原样返回（BB_DEF/LIMP/3BET 没有人数轴）。
    """
    rid = str(prior_id or "")
    if not rid.upper().startswith("COLD_CALL_"):
        return rid
    parsed = parse_range_id(rid)
    if parsed is None:
        return rid
    ip_oop, opener, _ = parsed
    suffix = tier_suffix(tier)
    out = f"COLD_CALL_{ip_oop}"
    if opener:
        out += f"_vs_{opener}"
    return out + suffix


def parse_range_id(range_id: str | None) -> tuple[str, str | None, int] | None:
    """解析冷跟 id → (IP|OOP, 开池者桶 | None, tier)。

    同时接受 `COLD_CALL_IP`、`COLD_CALL_IP_vs_EP`、`COLD_CALL_IP_n3`、
    `COLD_CALL_IP_vs_EP_n3`（大小写不敏感），以及 `_n2` 这种显式 2 人池写法。
    """
    raw = str(range_id or "").strip()
    if not raw:
        return None
    match = _RANGE_ID_RE.match(raw)
    if match is None:
        return None
    ip_oop = match.group("ip_oop").upper()
    opener = match.group("opener").upper() if match.group("opener") else None
    tier = int(match.group("tier") or TIER_MIN)
    # `_N` 可省（`COLD_CALL_IP_N3` 与 `_n3` 都认），但 `COLD_CALL_IP3` 不认：
    # 正则里 `_N?` 紧跟在 `_` 后，`IP3` 匹配不到。
    return ip_oop, opener, tier


# ─────────────────────────── 事实文案 ───────────────────────────

def tier_label_zh(cold: ColdCall | None) -> str:
    """档位的中文标签；宽度由调用方按自己的精度接在后面。

    「未定」必须显式写出来：只写 2 人池档会让模型以为这是实测的 2 人池。
    """
    if cold is None:
        return ""
    if not cold.known:
        return "入池人数未定，按 2 人池档"
    return f"{int(cold.tier)} 人池档"


def fact_payload(cold: ColdCall | None, prior_width_pct: float | None) -> dict:
    """facts / range_trace 里的档位块（advice 与 prompt 共用同一文案）。"""
    if cold is None:
        return {}
    width = None if prior_width_pct is None else round(float(prior_width_pct), 2)
    label = tier_label_zh(cold)
    if width is not None:
        label = f"{label}（{width:.1f}%）"
    return {
        "tier": cold.tier,
        "n_way": cold.n_way,
        "basis": cold.basis,
        "prior_width": width,
        "label": label,
    }


__all__ = [
    "BASIS_ACTION_ORDER", "BASIS_UNDETERMINED", "ColdCall", "HERO_SEAT",
    "TABLE_FILE", "TIER_MAX", "TIER_MIN", "UNDETERMINED", "fact_payload",
    "parse_range_id", "preflop_entry_order", "reset_cache", "tier_for_n_way",
    "tier_for_seat",
    "tier_from_n_way", "tier_label_zh", "tier_suffix", "tokens_for", "with_tier",
]
