"""「炸弹池守则」段（#114）：炸弹池局面替代书计划。

炸弹池翻前没有下注轮（每人先投固定金额），书里没有这种局面的牌，套普通
书卡会把「没有书页」变成「随便打」。这一段给出固定守则：只继续强成牌与
坚果听牌、超对只在干燥面小注、其余过牌/弃牌、不诈唬，并把已投的钱当沉没
成本。

只在 `bomb_guard_applies(sit)` 为真时出现；普通局面整段为 []，书计划照旧。
文案全是短句、一行一条；超长时先丢尾行（标题优先保留），单行仍超长才截断
加「…」，绝不把换行塞进单个元素。
"""

from __future__ import annotations

from ...playbook.bomb import bomb_guard_applies

BOMB_GUARD_HEADER = "## 炸弹池守则"

# 独立预算：守则是固定文案，正常只有几行；上限只防坏数据/未来文案膨胀，
# 不复用书计划的 800（两者进的是同一事实块，预算各自兜底）。
BOMB_GUARD_CHAR_LIMIT = 400

BOMB_GUARD_LINES: tuple[str, ...] = (
    "- 炸弹池没有翻前下注轮：每人先投固定金额，不要按翻前范围或开注来理解这手牌。",
    "- 翻牌后只继续：set（暗三）、两对、顺子、同花、坚果同花听牌。",
    "- 超对只在干燥面小注；其余情况不算价值牌。",
    "- 其余牌力过牌或弃牌，不要为了守池底强行继续。",
    "- 不诈唬：炸弹池不靠诈唬拿底池。",
    "- 已经投进去的 5BB 是沉没成本，别再为了它继续。",
)


def _cap(lines: list[str]) -> list[str]:
    """把守则压进 `BOMB_GUARD_CHAR_LIMIT`：先丢尾行，再单行截断。

    标题永远保留（它是模型识别这一段的锚点）；单行仍放不下时截到预算内并
    补「…」，保持「一个元素一行」的契约。
    """
    def size(seq: list[str]) -> int:
        return len("\n".join(seq))

    if not lines or size(lines) <= BOMB_GUARD_CHAR_LIMIT:
        return lines
    result: list[str] = []
    for line in lines:
        candidate = [*result, line]
        if size(candidate) <= BOMB_GUARD_CHAR_LIMIT:
            result.append(line)
            continue
        room = BOMB_GUARD_CHAR_LIMIT - size(result) - (1 if result else 0) - 1
        if room >= 8:
            result.append(line[:room].rstrip() + "…")
        break
    if not result:                       # 第一行自己就超限（理论上不会）
        result = [lines[0][: BOMB_GUARD_CHAR_LIMIT - 1] + "…"]
    return result


def render_bomb_guard_zh(sit) -> list[str]:
    """炸弹池守则段；守则不生效或任何异常都返回 []（整段不出现）。"""
    try:
        if not bomb_guard_applies(sit):
            return []
        return _cap([BOMB_GUARD_HEADER, *BOMB_GUARD_LINES])
    except Exception:                    # noqa: BLE001 - fail open
        return []


__all__ = [
    "BOMB_GUARD_CHAR_LIMIT",
    "BOMB_GUARD_HEADER",
    "BOMB_GUARD_LINES",
    "render_bomb_guard_zh",
]
