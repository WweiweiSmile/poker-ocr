"""摊牌证据（witness）投影与渲染。

契约：docs/摊牌-witness-数据契约-v1.md。

- admission.py：手牌 JSON → 准入（#19）、12 族与局面键（#12）、规范 hand 键。
- render.py：witness.rows → 每座位 ≤2 条提示词行，含 600 字符预算（#15/#17）。

本包不 eager import 子模块：admission 延迟导入 poker_ocr.opponent_profile，
render 依赖 admission，提示词层只拼接 render 的结果。
"""
