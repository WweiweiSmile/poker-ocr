-- 000_migration_version_width.sql
-- 迁移文件名可能超过 20 字符（例如 008_live_prompt_and_flags）；先把版本列放宽。
-- 这是幂等 ALTER，重复执行不会改坏数据。
ALTER TABLE replay_schema_migrations
  MODIFY version VARCHAR(64) NOT NULL;
