# STALE：旧英文提示词数据，禁止用于新模型

这里的 `system.txt` / `train.json` / `eval.json` 是 2026-10-23 用旧英文 prompt
（含 RFI 表与 Value directive）生成的，和当前 `advice.SYSTEM_ZH`
（`lean-zh-2026-10-29.1`）不同构。

**不要直接拿这份数据训练或做 eval。** 需要微调时：

1. 用当前代码重新生成：`.venv/bin/python tools/build_sft_dataset.py --out data/sft`；
2. 标签至少混入人工/求解器复核，不要只学 deepseek-flash 自己的决定；
3. 生成后确认 `data/sft/system.txt` 与 `advice.SYSTEM_ZH` 逐字节一致。

在重新生成之前，本目录仅作历史存档。
