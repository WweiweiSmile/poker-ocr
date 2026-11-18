"""canonical 手牌：源记录 → 唯一实体的归组规则（纯逻辑，不碰数据库）。

一手牌可以在复盘（review）、实时（live）、构造（synthetic）三边各有一条源记录；
它们靠「同内容」判定挂到同一个 canonical id 上，各自保留原始来源，不互相覆盖。
本模块只回答三个问题，SQL 读写与 primary 切换在
`repository.CanonicalHandRepository`（逐条自动提交，不开显式事务）：

1. 什么算同内容 —— 底牌相同（与两张牌的书写顺序无关）且公共牌相同（发牌顺序
   本身是事实，不排序）且发生时间差 ≤ 30 分钟（设计 §3.4 规则 1）；
2. `content_group` 怎么算 —— `sha1(底牌|公共牌|日期)`，同日同牌面共用一组
   （规则 2）；
3. 谁当 primary —— 人工记录优先：review > live > synthetic，同来源取
   `source_hand_id` 最小（规则 3）。人工切换由 `is_primary` 列覆盖，不在这里。

canonical id 从 2e12 起，与导入手牌的 1e12 段分开（§3.1）。牌面字符串按本仓
口径已是归一形式（`importer._cards_text`：10→T、花色小写），这里只做防御性
清理：去掉空格与常见分隔符。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re

from .errors import ApiError

# 三种来源，按可信度排序（前面的优先当 primary）
SOURCES: tuple[str, ...] = ("review", "live", "synthetic")
SOURCE_PRIORITY: dict[str, int] = {name: index for index, name in enumerate(SOURCES)}

# 同内容判定的时间容差：两次录入同一手牌通常差在几分钟内
MATCH_WINDOW = dt.timedelta(minutes=30)

# canonical id 独立命名空间；1e12 已被导入占用。这只是「段起点」的说明值：
# 表上的 AUTO_INCREMENT 从 base + 1 起（012 迁移里写死 2000000000001）。
CANONICAL_HAND_ID_BASE = 2_000_000_000_000

# 人工软合并的指针链最多走几层；超过就停在当前行，不去猜（防环）
MERGE_CHAIN_LIMIT = 8

HERO_CARD_COUNT = 2
BOARD_CARD_COUNT = 5
_SEPARATORS = re.compile(r"[\s,\-|/]+")


def _pairs(text: str | None) -> list[str]:
    """把牌面串切成两张一组的牌，去掉分隔符。"""
    cleaned = _SEPARATORS.sub("", str(text or ""))
    return [cleaned[i:i + 2] for i in range(0, len(cleaned) - 1, 2)]


def normalize_hero_cards(text: str | None) -> str:
    """底牌归一到「按牌面排序的两张」：QhKh 与 KhQh 是同一手牌。"""
    return "".join(sorted(_pairs(text)[:HERO_CARD_COUNT]))


def normalize_board(text: str | None) -> str:
    """公共牌只清分隔符，保持发牌顺序（顺序是事实，不能排序）。"""
    return "".join(_pairs(text)[:BOARD_CARD_COUNT])


def as_datetime(value) -> dt.datetime | None:
    """ISO 串 / datetime → 本地 naive datetime；坏值安全返回 None。

    MySQL DATETIME 存本地时间，所以这里只去掉 tzinfo，不做时区换算
    （原 `repository._mysql_datetime` 已并到这里，避免两套口径）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def content_group_key(hero_cards, board, started_at) -> str | None:
    """同内容归组键：`sha1(底牌|公共牌|日期)`；没有发生时间就不归组。"""
    started = as_datetime(started_at)
    if started is None:
        return None
    raw = "|".join((normalize_hero_cards(hero_cards),
                    normalize_board(board),
                    started.date().isoformat()))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def pick_candidate(candidates, started_at, window: dt.timedelta = MATCH_WINDOW):
    """在候选 canonical 里挑同内容的那一行：窗口内最近，等距取 id 最小。

    候选由 SQL 先按（底牌、公共牌、是否构造）粗筛；这里只做有时间精度的判定。
    没有发生时间的手牌不参与关联（返回 None = 新建一行）。
    """
    target = as_datetime(started_at)
    if target is None:
        return None
    best = None
    best_key: tuple[float, int] | None = None
    for row in candidates or ():
        when = as_datetime(row.get("started_at"))
        if when is None:
            continue
        seconds = abs((when - target).total_seconds())
        if seconds > window.total_seconds():
            continue
        key = (seconds, int(row["id"]))
        if best_key is None or key < best_key:
            best, best_key = row, key
    return best


def rank_key(row) -> tuple[int, int]:
    """来源优先级键：越小越该当 primary（review < live < synthetic，再比 id）。"""
    source = str(row.get("source") or "").strip().lower()
    return (SOURCE_PRIORITY.get(source, len(SOURCES)),
            int(row.get("source_hand_id") or 0))


def primary_row(sources):
    """默认 primary：review > live > synthetic，同来源取 source_hand_id 最小。

    只看来源与 id，不看 `is_primary` 列——这算的是「默认应该谁当 primary」，
    人工切换的结果由仓储层写进列里，重跑回填不覆盖。
    """
    best = None
    for row in sources or ():
        if best is None or rank_key(row) < rank_key(best):
            best = row
    return best


def validate_source(source) -> str:
    """来源必须落在三种之内；返回归一后的小写值。"""
    normalized = str(source or "").strip().lower()
    if normalized not in SOURCE_PRIORITY:
        raise ApiError("BAD_SOURCE",
                       f"未知来源 {source!r}；只认 {'/'.join(SOURCES)}", 422)
    return normalized
