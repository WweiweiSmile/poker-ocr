-- 009_hand_sets.sql
-- 手牌集：人工维护的评测集；实验创建时复制 hand_id + fingerprint 冻结。

CREATE TABLE IF NOT EXISTS replay_hand_sets (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     BIGINT UNSIGNED NOT NULL DEFAULT 0,
  name        VARCHAR(100) NOT NULL,
  description TEXT NULL,
  purpose     VARCHAR(32) NOT NULL DEFAULT 'regression'
              COMMENT 'regression/exploration/golden/coverage',
  revision    INT NOT NULL DEFAULT 1,
  archived    TINYINT(1) NOT NULL DEFAULT 0,
  frozen_at   DATETIME(3) NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
              ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_set_user_name (user_id, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='人工维护的 A/B 手牌集';

CREATE TABLE IF NOT EXISTS replay_hand_set_items (
  set_id       BIGINT UNSIGNED NOT NULL,
  hand_id      BIGINT UNSIGNED NOT NULL,
  fingerprint  CHAR(64) NOT NULL,
  role         VARCHAR(16) NOT NULL DEFAULT 'regression'
               COMMENT 'golden/regression/exploration/negative',
  priority     TINYINT NOT NULL DEFAULT 1 COMMENT '0=P0 必须通过',
  stratum      VARCHAR(64) NULL,
  expect_json  JSON NULL COMMENT 'P0 断言',
  note         VARCHAR(255) NULL,
  added_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (set_id, hand_id),
  KEY idx_hsi_hand (hand_id),
  KEY idx_hsi_role (set_id, role, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='手牌集条目；实验创建时复制到 experiment_hands';

ALTER TABLE replay_experiments
  ADD COLUMN hand_set_id BIGINT UNSIGNED NULL,
  ADD COLUMN hand_set_revision INT NULL,
  ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'ab'
      COMMENT 'ab/regression/smoke',
  ADD COLUMN change_scope VARCHAR(32) NULL
      COMMENT 'prompt/facts/model/context/code/mixed',
  ADD COLUMN baseline_prompt_mode VARCHAR(24) NOT NULL DEFAULT 'stored'
      COMMENT 'stored/rerender/external',
  ADD COLUMN metrics_json JSON NULL,
  ADD COLUMN conclusion TEXT NULL,
  ADD COLUMN decision VARCHAR(16) NULL
      COMMENT 'adopt/reject/iterate/inconclusive',
  ADD COLUMN promoted_version_id BIGINT UNSIGNED NULL,
  ADD KEY idx_exp_set (hand_set_id, hand_set_revision);
