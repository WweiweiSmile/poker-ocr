-- 006_advice_context.sql
-- A/B 的上下文口径：重跑时可以选「线上快照」还是「不注入」。
-- 默认 none，保持旧版本行为；新版本显式选 snapshot 才能复现线上桌况。

ALTER TABLE replay_versions
  ADD COLUMN context_mode VARCHAR(16) NOT NULL DEFAULT 'none'
      COMMENT 'none/snapshot/recompute：对手画像上下文',
  ADD COLUMN table_stats_mode VARCHAR(16) NOT NULL DEFAULT 'none'
      COMMENT 'none/snapshot/recompute：桌况统计上下文';
