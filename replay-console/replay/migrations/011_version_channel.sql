-- 011_version_channel.sql
-- 版本通道：prod/candidate/archive，让实验默认基线明确。

ALTER TABLE replay_versions
  ADD COLUMN channel VARCHAR(16) NOT NULL DEFAULT 'candidate'
      COMMENT 'prod/candidate/archive',
  ADD COLUMN promoted_at DATETIME(3) NULL,
  ADD KEY idx_ver_channel (user_id, channel);
