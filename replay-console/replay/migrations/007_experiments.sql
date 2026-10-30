-- 007_experiments.sql
-- 离线 A/B：冻结手牌集 + 变体对 + dev/holdout 切分。
-- 任务仍走 replay_jobs；experiment_id 只做关联，不改变 worker 语义。

CREATE TABLE IF NOT EXISTS replay_experiments (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id             BIGINT UNSIGNED NOT NULL DEFAULT 0,
  name                VARCHAR(100) NOT NULL,
  hypothesis          TEXT NULL,
  baseline_version_id BIGINT UNSIGNED NOT NULL,
  variant_version_id  BIGINT UNSIGNED NOT NULL,
  filter_json         JSON NULL COMMENT '创建时的筛选条件快照',
  split_strategy      VARCHAR(32) NOT NULL DEFAULT 'hand_hash'
                      COMMENT 'hand_hash/hand_list/all',
  dev_ratio           DOUBLE NOT NULL DEFAULT 0.7,
  repeat_count        INT NOT NULL DEFAULT 3,
  status              VARCHAR(16) NOT NULL DEFAULT 'draft'
                      COMMENT 'draft/queued/running/done/failed/archived',
  job_id              BIGINT UNSIGNED NULL,
  created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  started_at          DATETIME(3) NULL,
  finished_at         DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_exp_user_created (user_id, created_at),
  KEY idx_exp_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='离线 A/B 实验：变体对 + 冻结数据集';

CREATE TABLE IF NOT EXISTS replay_experiment_hands (
  experiment_id BIGINT UNSIGNED NOT NULL,
  hand_id       BIGINT UNSIGNED NOT NULL,
  fingerprint   CHAR(64) NOT NULL,
  split         VARCHAR(16) NOT NULL COMMENT 'dev/holdout',
  stratum       VARCHAR(64) NULL,
  PRIMARY KEY (experiment_id, hand_id),
  KEY idx_eh_hand (hand_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='实验数据集：创建时冻结，跑完不漂移';

ALTER TABLE replay_jobs
  ADD COLUMN experiment_id BIGINT UNSIGNED NULL,
  ADD KEY idx_job_experiment (experiment_id);
