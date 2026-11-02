"""输出契约：只回一个 option id + 一句 reason。"""

from __future__ import annotations


OUTPUT_CONTRACT = """## 输出
只输出一个 JSON 对象（不要 Markdown/解释）：
{"option": "<id>", "reason": "<中文一句话，≤40 字>"}
需要置信度时（可选）：
{"option": "<id>", "reason": "...", "confidence": 0.7}
- option 必须严格来自「可选动作」，不得编造 id 或金额。
- id 映射（以「可选动作」为准）：
  翻前无人入池用 open-*/call；call 只用于 SB 补盲、BB 过牌和明确的 limp 例外。翻前面对加注用 raise-3x/raise-4x（3x/4x=上家加注的 3/4 倍；标签按实际级别写 3-bet/4-bet/5-bet，id 不变）。
  翻后无人下注 call=过牌、raise-1/3…1.2=下注；翻后面对下注 call=跟注、raise-1/3…1.2=加注；全下 all-in。
- 不要自造 check/bet-*。
- reason 引用一个最关键的事实（牌力/赔率/位置/画像/SPR/底池类型/牌面）；不要引用未提供的规则、图表或概率。
- reason 不得与事实矛盾：无位置不能说「有位置」，没人行动过不能写「对手过牌」。
- 只有「数据质量」标为硬缺口（底牌/公共牌/底池/需跟/后手/在局人数）时才走保守线：能过牌就过牌；面对下注且赔率/筹码不明则弃，并在 reason 说明缺什么。范围/画像/桌况缺口不改变价值下注/弃牌，只降低诈唬频率。"""
