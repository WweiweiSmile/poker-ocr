"""可选动作：程序算好的 id + 中文标签 + 实际金额。

模型只能从这些 id 里选，所以「尺度」不是模型生成的，而是程序按现场底池
算出来的；这一段同时负责把重复档位（后手不足时封顶成同一金额）去掉。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # 只在类型检查时引入，避免循环 import
    from ...options import Option
    from ...situation import Situation


_GENERIC_OPTION_NOTES = {
    "还没人下注，可以过牌",
    "剩余后手全部投入",
    "第一个入池通常应该加注",
    "对手下注已覆盖我的后手，跟注即全下",
}


def _option_note(option: Option) -> str:
    """只在 note 提供约束信息时附加；重复/废话 note 丢掉。"""
    note = (option.note or "").strip()
    if not note or note in option.label:
        return ""
    if note in _GENERIC_OPTION_NOTES:
        return ""
    # 标签已经有「（本桌最低加注）」，再补一句「已抬到本桌最低加注」是重复。
    if "（" in option.label and "最低加注" in note:
        return ""
    return f"；{note}" if "）" in option.label else f"（{note}）"


def render_options_zh(sit: Situation) -> list[str]:
    """「可选动作」：`- id｜中文标签`，只保留有约束意义的 note。

    后手不足时五个档位会封顶成同一个金额；prompt 里只显示一个，避免
    「五个 id、同一个动作」的伪选择。`sit.options` 仍保留全部 id 供点击器
    和 preset 映射使用。
    """
    lines = ["## 可选动作"]
    seen: set[tuple] = set()
    for option in sit.options:
        if option.amount is not None:
            key = (option.kind, option.amount)
            if key in seen:
                continue
            seen.add(key)
        lines.append(f"- {option.id}｜{option.label}{_option_note(option)}")
    return lines
