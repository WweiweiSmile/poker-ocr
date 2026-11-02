"""提示词共用的中文词表。

街名、底池类型、翻前形态、花色/点数、牌型、牌面质地都在这里；各事实段只
引用、不各写一份，避免同一种牌在「牌力」和「范围」两段叫不同名字。
"""

from __future__ import annotations


_STREET_ZH = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
_POT_TYPE_ZH = {
    "3-bet+ pot": "3-bet+ 池",
    "single-raised pot": "单加注池",
    "limped pot": "溜入池",
    "unopened pot": "无人入池",
    "bomb pot": "炸弹池（强制前注）",
}
_PREFLOP_SHAPE_ZH = {
    "unopened": "无人入池",
    "limped": "溜入",
    "single-raise": "单加注",
    "three-bet": "3-bet",
}
_SUIT_ZH = {"s": "黑桃", "h": "红桃", "d": "方块", "c": "梅花"}
_RANK_ZH = {"T": "10", "J": "J", "Q": "Q", "K": "K", "A": "A"}
_CATEGORY_EN_ZH = {
    "High card": "高牌", "One pair": "一对", "Two pair": "两对",
    "Trips": "三条", "Straight": "顺子", "Flush": "同花",
    "Full house": "葫芦", "Quads": "四条", "Straight flush": "同花顺",
}


# 牌面质地：coach 的中文标签 → 英文（旧英文事实块与 texture_en 用）。
_TEXTURE_EN = {
    "干燥": "Dry", "花面": "Flush-y", "顺面": "Straight-y",
    "花顺面": "Flush + straight", "公对面": "Paired",
}
