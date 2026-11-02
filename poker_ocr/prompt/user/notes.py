"""人工现场观察（根目录 `.table_dy`）：统计算不出来的东西。

自由文本不可能完全理解语义，这里只做两条最影响决策的处理：
按街/牌面过滤 A 高面观察；把「见 A 抵抗下降」翻译成条件事实。
"""

from __future__ import annotations

import re

from ...hand_track import PREFLOP, HandSnapshot


_ACE_MARKERS = ("见a", "有a", "a高", "a面", "a的", "a时", "公共牌有a")
_ACE_NEGATIONS = (
    "不弃", "不怕", "不惧", "不弱", "不会弃", "不会怕", "不易弃",
    "不一定会弃", "未必会弃", "未必弃", "不会轻易弃", "抵抗强",
    "抵抗好", "扛得住", "不怂",
)
_ACE_WEAKNESS = ("抵抗", "弃", "怕", "弱", "fold")


def _filter_table_dynamics_zh(text: str | None,
                              snap: HandSnapshot) -> str | None:
    """按街/牌面过滤现场观察：A 高面的观察只在翻后且牌面有 A 时注入。

    `.table_dy` 是自由文本，程序不可能完全理解语义；这里只做一条最影响
    决策的过滤：翻前没有公共牌、非 A 牌面也不该看到「A 高面多打」的指令。
    其余观察原样保留。
    """
    if not text:
        return None
    board_has_ace = any(str(card)[:1].upper() == "A" for card in snap.board)
    keep: list[str] = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        compact = line.replace(" ", "").lower()
        ace_specific = any(k in compact for k in ("a高", "见a", "有a", "a面",
                                                  "a时", "公共牌有a"))
        if ace_specific and (snap.street == PREFLOP or not board_has_ace):
            continue
        keep.append(line)
    return "\n".join(keep) or None


def _notes_ace_weakness(text: str | None) -> bool:
    """人工观察里是否写了「见 A 抵抗力下降」这类池级剥削。

    这是用户 2026-10-31 的现场口径，不是程序统计算出来的：写进 `.table_dy`
    后，这里只负责在 A 高面把它翻译成一条**条件事实**，没写就不编。

    按分句匹配否定：同一段里写「他从不弃牌」不会关掉另一句「A 高面容易弃」；
    「见A不弃」「A高不怕」「A面抵抗强」仍然读成「不弱」。
    """
    if not text:
        return False
    for clause in re.split(r"[\n。；;，,]", str(text)):
        compact = clause.replace(" ", "").lower()
        if not compact:
            continue
        if any(k in compact for k in _ACE_NEGATIONS):
            continue
        if not any(k in compact for k in _ACE_MARKERS):
            continue
        if any(k in compact for k in _ACE_WEAKNESS):
            return True
    return False


def render_notes_zh(sit) -> list[str]:
    """「## 人工观察」整段；没有可注入内容时返回空列表。"""
    dynamics = _filter_table_dynamics_zh(sit.table_dynamics, sit.snap)
    if not dynamics:
        return []
    return ["## 人工观察（现场记录；统计算不出来的东西）", dynamics]
