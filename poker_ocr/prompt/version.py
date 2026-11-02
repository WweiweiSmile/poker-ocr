"""提示词版本号。

版本号会写进 session_start / advice JSONL，复盘归因与 A/B 都用它对齐
「模型当时看到的提示词」。改动 system、事实块或模块顺序时**必须** bump，
并同步 `replay/versioning.py` 的 `CURRENT_ADVICE_VERSION` 与 golden hash。
"""

from __future__ import annotations

# 2026-11-03：提示词按功能/场景拆成 prompt 包（system / user / shared / offline），
# system 增加条件场景段；事实块文本保持不变。
PROMPT_VERSION = "lean-zh-2026-11-03.1"
PROMPT_LANGUAGE = "zh"
