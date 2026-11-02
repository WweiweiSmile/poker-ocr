"""离线/复盘用的旧提示词与规则层，**不进入 live 链路**。

live 的 system/user 在 `prompt/system/` 与 `prompt/user/`；这里的
`render_hand_facts` / `value_directive` / `math_gate` / `audit_advice_tags`
只服务历史记录复跑与复盘归因，`advice.py` 仍 re-export 这些名字。
"""

from __future__ import annotations
