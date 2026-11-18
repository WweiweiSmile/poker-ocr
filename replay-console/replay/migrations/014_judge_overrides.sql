-- 014：judge 改判留痕（#58）
-- 接入 LLM judge 后，原始判定仍留在 replay_run_items.judge_json / score 里；
-- 人工改判只写 replay_judge_overrides（聚合时优先），并逐次记进 log：
-- 谁（actor）、什么时候（created_at）、从什么改成什么（before/after）。
ALTER TABLE replay_judge_overrides
  ADD COLUMN actor VARCHAR(64) NOT NULL DEFAULT '' AFTER hard_fail,
  ADD COLUMN source_json JSON NULL AFTER actor,
  ADD COLUMN updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
      ON UPDATE CURRENT_TIMESTAMP(3) AFTER created_at;

CREATE TABLE IF NOT EXISTS replay_judge_override_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id      BIGINT UNSIGNED NOT NULL,
  case_id     BIGINT UNSIGNED NOT NULL,
  actor       VARCHAR(64) NOT NULL DEFAULT '',
  note        TEXT NULL,
  before_json JSON NULL,
  after_json  JSON NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_jo_log (run_id, case_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='人工改判的逐次记录；原始判定不被覆盖';
