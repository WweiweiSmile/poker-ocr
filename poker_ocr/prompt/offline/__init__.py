"""离线/复盘用的硬规则审计（**legacy**）：**不进入 live 链路**。

live 的 system/user 在 `prompt/system/` 与 `prompt/user/`；这里只剩
`audit_advice_tags`（复盘归因、`tools/compare_review_hands.py` 与
`replay-console/replay/engine.py` 共用）。旧的事实块/价值指令/赔率闸门
（`hand_facts.py` / `strategy.py`）已于 2026-11-18 删除。
"""

from __future__ import annotations
