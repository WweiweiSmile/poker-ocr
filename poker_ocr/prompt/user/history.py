"""上一街自己的建议：跨街一致性。

模型每一街都是**独立一次调用**，没有对话记忆。没有这一段时，它看不到
自己上一街说了什么，容易出现「翻牌建议下注、转牌又建议过牌」这种自相
矛盾的线路，`Advice.plan` 也只能留在日志里。

这里只渲染**同一手、更早街**的一条建议（动作 + 金额 + 理由），并明确
标注「不是新事实」：当前街的牌力/价格/范围仍然优先；「上一街已经下注」
是沉没成本，不能当成继续投入的理由。
"""

from __future__ import annotations

from ..shared.labels import _STREET_ZH


def render_previous_advice_zh(sit) -> list[str]:
    """「上一街我的建议」段；没有上一街建议时返回空列表。"""
    prev = getattr(sit, "previous", None)
    if prev is None:
        return []
    street = getattr(prev, "street", None)
    current = getattr(getattr(sit, "snap", None), "street", None)
    if not street or street == current:
        return []
    option = getattr(prev, "option", None)
    label = (getattr(option, "label", None)
             or getattr(option, "id", None) or "未知动作")
    amount = getattr(option, "amount", None)
    reason = " ".join(str(getattr(prev, "reason", "") or "").split())
    line = f"{_STREET_ZH.get(street, street)}：{label}"
    if amount is not None:
        line += f"（{amount:g}BB）"
    if reason:
        line += f"；理由：{reason}"
    return [
        "## 上一街我的建议（同一手；用于保持一致，不是新事实）",
        "- " + line,
        "- 当前街的事实/价格变了就按新事实改线路；「上一街已经下注」是"
        "沉没成本，不能当成继续投入的理由。",
    ]
