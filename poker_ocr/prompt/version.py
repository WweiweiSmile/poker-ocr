"""提示词版本号。

版本号会写进 session_start / advice JSONL，复盘归因与 A/B 都用它对齐
「模型当时看到的提示词」。改动 system、事实块或模块顺序时**必须** bump，
并同步 `replay/versioning.py` 的 `CURRENT_ADVICE_VERSION` 与 golden hash。
"""

from __future__ import annotations

# 2026-11-03.2：场景段叠加仲裁、后手/本手总筹码拆分、行动线「未记录」、
# 4-bet 形态按实际级别、硬/软缺口分栏、建议标记与 reason 60 字。
PROMPT_VERSION = "lean-zh-2026-11-03.2"
PROMPT_LANGUAGE = "zh"
