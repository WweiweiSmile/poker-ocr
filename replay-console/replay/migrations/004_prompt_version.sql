-- 004_prompt_version.sql
-- 中文 lean 之后，advice_version 只表示「事实/选项语义」，prompt_version 单独记录
-- system 文本版本（如 lean-zh-2026-10-29.1），便于复盘时区分「同一语义、不同文案」。
ALTER TABLE replay_results
  ADD COLUMN prompt_version VARCHAR(32) NULL
      COMMENT '生成该结果的 prompt 版本；旧记录为 NULL';
