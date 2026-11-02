"""数据质量：把技术告警翻成 1–4 条可读中文，并区分硬缺口/范围缺口。

硬缺口只有两类：① 可选动作真的被砍（`Situation.options_degraded` 非空，
底池/后手未读全或翻后 0 对手）；② 底牌/公共牌缺失，根本没法评估。
其余（范围未知、街前底池未读全、对手筹码未读出）都只降低可信度——
system 的保守线只对硬缺口生效，这段就是它的输入。分类靠显式短语表
（`_HARD_PATTERNS`），不从译文里猜宽词。
"""

from __future__ import annotations


def _gap_zh(text: str) -> str:
    """把数据缺口翻成一句可读中文；未知英文告警不再原样塞进 prompt。"""
    text = (text or "").strip()
    if not text:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text
    if "hero hole cards unread" in text:
        return "底牌未读出，无法评估牌力"
    if "hero opening stack unread" in text:
        return "开池后手未读出，套池比例未知"
    if "range unknown" in text or "range not found" in text:
        return "某座位范围未知"
    if "ranges unavailable" in text or "contradict" in text:
        return "翻前动作与形态矛盾，范围不可用"
    if "only the first" in text and "modeled" in text:
        return "只建模前 4 名对手；其余范围未知"
    if "bet size without an action badge" in text:
        return "有下注额但没抓到动作徽章，范围按更宽处理"
    if "no preflop action" in text:
        # 按座位报的缺口：这个座位的翻前动作没记到 → 它的范围只能用未开池先验。
        return "部分座位翻前动作线缺失，范围按未开池先验估计"
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


# 硬缺口只登记「动作空间被砍 / 根本没法评估」的几条，并用足够长的短语匹配。
# 历史教训：早先用「未读全」「筹码未读出」做宽匹配，把「街前底池未读全
# （范围 SPR 近似）」「对手筹码未读出（有效筹码未算）」也升级成保守线，
# 河牌明明赔率/筹码齐全却被劝弃。宁可漏判成软缺口，也不要误判成弃牌。
_HARD_PATTERNS = (
    "底牌未读出",
    "开池后手未读出",
    "牌面缺张",
    "底池/后手未读全",
    "状态帧",
    "可选动作已降级",
)


def _is_hard_gap(gap: str) -> bool:
    """硬缺口 = 动作空间被砍 / 底牌或公共牌缺失；其余只算范围/信息缺口。"""
    return any(pattern in gap for pattern in _HARD_PATTERNS)


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
    lines = ["## 数据质量（硬缺口 = 可选动作已被降级，或底牌/公共牌缺失；"
             "其余只降低可信度）"]
    summary = _summarize_gaps(
        gaps, has_range_equity=sit.hand_ranges is not None)
    hard = [gap for gap in summary if _is_hard_gap(gap)]
    soft = [gap for gap in summary if gap not in hard]
    degraded = getattr(sit, "options_degraded", None)
    if degraded and not hard:
        # 选项确实降级了，但告警文案没被抓到：补一条硬缺口，
        # 让「说硬缺口」与「动作空间真的被砍」永远是同一件事。
        hard.append("底池/后手未读全" if degraded == "unsized"
                    else "状态帧异常（在局对手为 0）")
    if hard:
        lines.append("- 硬缺口（走保守线）：" + "；".join(hard))
    if soft:
        lines.append("- 范围/信息缺口（范围/画像缺口只降低诈唬频率；"
                     "动作线缺失按决策顺序判断：未开池/面对加注/未行动）："
                     + "；".join(soft))
    if not hard and not soft:
        lines.append("无已知硬缺口（未列出的技术告警不影响动作）")
    return lines
