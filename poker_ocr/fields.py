"""字段解析：筹码、名字。

筹码这里有一道**单位闸**，是本模块存在的主要理由 —— 见 parse_chip 的注释。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scan  # noqa: E402

from .ocr import OcrEngine, preprocess_variant  # noqa: E402


# ─────────────────────────── 筹码 ───────────────────────────

@dataclass
class ChipResult:
    text: str
    value: int | float | None
    unit: str | None
    score: float
    ok: bool
    corrected: bool = False
    error: str | None = None

    def to_debug(self) -> dict:
        d: dict = {"raw": self.text, "value": self.value, "score": round(self.score, 4),
                   "ok": self.ok}
        if self.unit:
            d["unit"] = self.unit
        if self.corrected:
            d["corrected"] = True
        if self.error:
            d["error"] = self.error
        return d


def parse_chip(text: str, score: float, required_unit: str | None = "BB") -> ChipResult:
    """把 OCR 文本解析成筹码数。

    **为什么非要卡单位**：scan.clean_number() 有一条静默错误路径 ——
    `238BB` 若被读成 `2388`（BB→88），此时字符串里已经没有字母了，
    _UNIT_RE 匹配不到、corrected 也是 False，于是它返回 (2388, None, False)、ok=True。
    也就是**自信地给出一个错值且零告警**，比读不出来危险得多。

    所以凡是必然带单位的字段，单位对不上就一律判不确定，不采信 translate 之后的结果。
    """
    value, unit, corrected = scan.clean_number(text)

    if value is None:
        return ChipResult(text, None, unit, score, ok=False,
                          corrected=corrected, error="清洗后不是数字")

    if required_unit and unit != required_unit:
        # 单位缺失很可能是把 BB 读成了 88，值本身已经不可信
        return ChipResult(text, None, unit, score, ok=False, corrected=corrected,
                          error=f"单位不是 {required_unit}（实际 {unit or '无'}）"
                                f"，值可能被误读，不采信")

    return ChipResult(text, value, unit, score, ok=True, corrected=corrected)


def looks_like_chip(text: str, required_unit: str | None = "BB") -> bool:
    """快速判断一个文本框像不像筹码（用来做槽位分配的候选筛选）。"""
    return parse_chip(text, 1.0, required_unit).ok


# ─────────────────────────── 名字 ───────────────────────────

_NAME_MAX_LEN = 14

# 两个读法的分数差在这个范围内、却读得不一样，才算「拿不准」。见 read_name。
_REVIEW_SCORE_MARGIN = 0.05


def looks_like_name(text: str) -> bool:
    """名字合理性谓词。

    注意这是**粗筛**，不是判据：「总底池」「翻牌」也能过这一关。
    真正把它们挡在外面的是槽位门（它们不落在任何名字槽里）。

    **名字里带数字很常见**（菜菜子31、Dragon333、谈轩66），所以判据不能是
    「数字占比低」—— 曾经那条 `数字 > 长度/3 就否决」的规则把
    「菜菜子31」(2/5) 和「谈轩66」(2/4) 全误杀了，它们连座位都没进得去。

    真正的判据是：**去掉数字之后还剩不剩字**。剩空 → 纯数字或纯符号，不是名字；
    剩字母或汉字 → 是名字。数字仍不能占绝大多数（"A12345678" 这种不像名字），
    但阈值放得很宽，只做兜底。
    """
    t = text.strip()
    if not t or len(t) > _NAME_MAX_LEN:
        return False
    if ":" in t or "/" in t:
        return False
    if looks_like_chip(t):
        return False
    if not any(ch.isalpha() for ch in t):
        return False
    digits = sum(ch.isdigit() for ch in t)
    if digits > len(t) * 2 / 3:
        return False
    return True


# 重试阶梯：越靠后越激进。取**分数最高**的一级作为结果。
# L0 不在这里 —— 它由全图检测的结果直接充当（见 read_name 的 seed 参数）。
NAME_LADDER: list[tuple[float, str, str]] = [
    (3.0, "raw", "L1 放大3x"),
    (3.0, "gray", "L2 放大3x灰度"),
    (5.0, "otsu", "L3 放大5x二值"),
]


@dataclass
class NameVariant:
    level: str
    text: str
    score: float

    def to_debug(self) -> dict:
        return {"level": self.level, "text": self.text, "score": round(self.score, 4)}


@dataclass
class NameResult:
    text: str | None
    score: float
    ok: bool
    variants: list[NameVariant] = dc_field(default_factory=list)
    review: bool = False
    error: str | None = None

    def to_debug(self) -> dict:
        return {
            "value": self.text,
            "score": round(self.score, 4),
            "ok": self.ok,
            "review": self.review,
            "variants": [v.to_debug() for v in self.variants],
            **({"error": self.error} if self.error else {}),
        }


def read_name(engine: OcrEngine, crop: np.ndarray, min_score: float,
              seed_text: str | None = None,
              seed_score: float = 0.0) -> NameResult:
    """定名字。

    seed 是全图 det 已经读出来的原文。**只要它有，就以它的文本为准**，阶梯不覆盖。

    为什么让 seed 说了算 —— 三条实测教训：

    1. 全图检测本来就准。table1 里四个「读不出来」的名字（唐小鱼呀 / Dragon333 /
       菜菜子31 / 谈轩66），检测实际全读对了（0.996-0.998），是下游用比文字还窄的
       框重新裁、重新识别，把对的扔了再猜一遍才错的。
    2. 阶梯从来没有真正修正过一个错字。它只在已经正确的文本上把分数抬高
       （「悍将v1」0.896 → 0.982，文本没变），却制造了新错误。
    3. 阶梯对裁剪边距极其敏感，而且**高分不等于读对**：留白 8% 时「稚气未酒」
       被切成「稚气未」，那个错读法置信度 0.998，反而盖过检测的 0.905。
       按分数选，就会选出错的。

    所以阶梯退居二线：只在**没有检测结果**时（检测漏框了）才真的用它识字；
    有 seed 时它只用来提分数和标记「读法有分歧，值得看一眼」。

    **为什么仍然不是「投票」**：洒→酒 这类形近字错误是识别模型的*系统性*先验错误，
    不是随机抖动 —— 所有预处理变体都会一致地输出「酒」。投票会把 4:1 的高一致性
    错误答案包装成看起来很确定的结论。一致性在系统性错误上和正确性是负相关的。
    """
    variants: list[NameVariant] = []

    if seed_text:
        variants.append(NameVariant("L0 检测框", seed_text, seed_score))
        ladder = NAME_LADDER
    else:
        # 没有检测结果（检测漏框了）时才退回去自己识别一遍
        ladder = [(1.0, "raw", "L0 原始")] + NAME_LADDER

    for upscale, mode, label in ladder:
        prepared = preprocess_variant(crop, mode, upscale)
        try:
            hits = engine.read_text(prepared)
        except Exception as exc:                      # 单级失败不该带崩整条阶梯
            variants.append(NameVariant(label, f"<{type(exc).__name__}>", 0.0))
            continue
        if not hits:
            continue
        # 用 "" 而不是 " " 拼接：多框时若拼成「奥利奥 H」就多了个空格
        text = "".join(t for t, _ in hits).strip()
        score = min(s for _, s in hits)
        variants.append(NameVariant(label, text, score))

    if not variants:
        return NameResult(None, 0.0, ok=False, error="所有级别都没识别出文字")

    if seed_text:
        # 文本由检测结果定；阶梯里读出同样文本的，只用来把分数顶上去
        text = seed_text
        score = max(v.score for v in variants if v.text == seed_text)
    else:
        best = max(variants, key=lambda v: v.score)
        text, score = best.text, best.score

    # 只有**分数接近**却读得不一样，才算「拿不准」，值得人看一眼。
    # 拿明显更差的读法去质疑明显更好的读法，是假警报。
    close_disagreement = any(
        v.text != text and v.score >= score - _REVIEW_SCORE_MARGIN
        for v in variants
    )
    review = score < min_score or close_disagreement

    return NameResult(text, score, ok=True, variants=variants, review=review)
