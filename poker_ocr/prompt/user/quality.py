"""数据质量：把技术告警翻成 1–4 条可读中文，并区分硬缺口/范围缺口。

硬缺口（底牌/公共牌/底池/需跟/后手/在局人数）会改变可用动作；范围/画像/
桌况缺口只降低可信度。system 的保守线只对硬缺口生效，这段就是它的输入。
"""

from __future__ import annotations


def _gap_zh(text: str) -> str:
    """把数据缺口翻成一句可读中文；未知英文告警不再原样塞进 prompt。"""
    text = (text or "").strip()
    if not text:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text
    if "range unknown" in text or "range not found" in text:
        return "某座位范围未知"
    if "ranges unavailable" in text or "contradict" in text:
        return "翻前动作与形态矛盾，范围不可用"
    if "only the first" in text and "modeled" in text:
        return "只建模前 4 名对手；其余范围未知"
    if "bet size without an action badge" in text:
        return "有下注额但没抓到动作徽章，范围按更宽处理"
    if "no preflop action" in text:
        return ("翻前动作线缺失，翻前形态/PFR 未知（按未开池处理："
                "open 或弃；BB 可过牌）")
    if "no action this street" in text:
        return "部分座位本街行动线缺失，范围按未行动处理（不是已过牌）"
    if "no range table" in text or "4-bet+ pot" in text:
        return "4-bet+ 池没有范围表，范围未知"
    if "inferred action" in text:
        return "部分动作是按底池对账补出的，范围的事件顺序为近似"
    if "board incomplete" in text:
        return "牌面缺张，该街动作未计入范围"
    if "pot at street start unread" in text:
        return "街前底池未读全，范围 SPR 为近似值"
    # 未知技术告警对模型没有可执行信息（它看不到日志），不再占一行。
    return ""


_HARD_GAP_MARKERS = ("未读全", "筹码未读出", "状态帧", "牌面缺张")


def _is_hard_gap(gap: str) -> bool:
    """硬缺口 = 会改变可用动作的事实缺失；范围/信息缺口不算。"""
    return any(marker in gap for marker in _HARD_GAP_MARKERS)


def _summarize_gaps(gaps: list[str], *, has_range_equity: bool = False) -> list[str]:
    """把逐座位/技术告警压成 1–4 条中文，避免 5 行同义的 range unknown。"""
    ordered = [g for g in dict.fromkeys(gaps) if g]
    range_unknown = sum(1 for g in ordered if g == "某座位范围未知")
    rest = [g for g in ordered if g != "某座位范围未知"]
    out: list[str] = []
    if range_unknown:
        tail = ("按随机牌计入权益" if has_range_equity
                else "未计入范围权益（没有可用范围）")
        out.append(f"{range_unknown} 名对手范围未知，{tail}")
    # 硬事实缺口（底池/后手/筹码未读全、0 对手异常）优先于普通技术告警，
    # 不能被 cap 挤掉。
    hard = [g for g in rest if _is_hard_gap(g)]
    unacted = [g for g in rest if "本街行动线缺失" in g]
    out.extend(hard)
    out.extend(unacted)
    out.extend(g for g in rest if g not in hard and g not in unacted)
    return out[:4]


def render_data_quality_zh(sit) -> list[str]:
    """「## 数据质量」整段：硬缺口优先，范围缺口合并。"""
    snap = sit.snap
    gaps = [_gap_zh(w) for w in sit.warnings]
    if sit.hand_ranges is not None:
        actual = (sit.opponents_in_hand
                  if sit.opponents_in_hand is not None
                  else snap.opponents_in_hand)
        modeled = len(sit.hand_ranges.opponents)
        random_fill = getattr(sit.hand_ranges, "random_opponents", 0)
        if (actual > modeled + random_fill
                and not any("只建模前" in gap for gap in gaps)):
            gaps.append(f"只建模前 {modeled} 名对手；另有 "
                        f"{actual - modeled - random_fill} 名未计入范围权益")
    lines = ["## 数据质量（硬缺口影响可用动作；范围/画像缺口只降低可信度）"]
    summary = _summarize_gaps(
        gaps, has_range_equity=sit.hand_ranges is not None)
    if summary:
        hard = [gap for gap in summary if _is_hard_gap(gap)]
        soft = [gap for gap in summary if gap not in hard]
        if hard:
            lines.append("- 硬缺口（走保守线）：" + "；".join(hard))
        if soft:
            lines.append("- 范围/信息缺口（范围/画像缺口只降低诈唬频率；"
                         "动作线缺失按决策顺序的未开池/未行动口径）："
                         + "；".join(soft))
    else:
        lines.append("无已知硬缺口（未列出的技术告警不影响动作）")
    return lines
