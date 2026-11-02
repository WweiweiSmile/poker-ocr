"""提示词里的数字格式：BB 一律走同一个口径，避免「3.0BB / 3BB」混用。"""

from __future__ import annotations


def _fmt_num(value: float | None) -> str:
    """BB 数字：整数不带小数点，其余保留两位。"""
    if value is None:
        return "未知"
    value = float(value)
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value))}"
    return f"{value:.2f}".rstrip("0").rstrip(".")
