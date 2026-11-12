"""离线/复盘用的旧提示词与规则层（**legacy**）：**不进入 live 链路**。

live 的 system/user 在 `prompt/system/` 与 `prompt/user/`；这里的
`render_hand_facts` / `value_directive` / `math_gate` / `audit_advice_tags`
只服务离线审计、历史记录复跑与 A/B，调用方必须直接从本包 import
（`poker_ocr.advice` 不再转发）。
"""

from __future__ import annotations
