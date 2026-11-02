-- 010_pair_reviews.sql
-- 成对人工偏好：A/B/same/unsure。与 replay_decision_marks（绝对好坏）分开。

CREATE TABLE IF NOT EXISTS replay_pair_reviews (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id       BIGINT UNSIGNED NOT NULL DEFAULT 0,
  experiment_id BIGINT UNSIGNED NOT NULL,
  decision_id   BIGINT UNSIGNED NOT NULL,
  preferred     VARCHAR(8) NOT NULL COMMENT 'A/B/same/unsure',
  tags          JSON NULL,
  note          TEXT NULL,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_pr_user_exp_dec (user_id, experiment_id, decision_id),
  KEY idx_pr_exp (experiment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='成对人工偏好：A/B/same/unsure';
