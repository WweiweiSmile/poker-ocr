-- #57 评分运行：replay_runs / replay_run_items 在 012 已建，这里只补运行态列。
ALTER TABLE replay_runs
  ADD COLUMN started_at DATETIME NULL AFTER created_at,
  ADD COLUMN done_cases INT NOT NULL DEFAULT 0 AFTER total_cases,
  ADD COLUMN cancel_requested TINYINT(1) NOT NULL DEFAULT 0 AFTER status,
  ADD COLUMN metrics_json JSON NULL AFTER config_snapshot,
  ADD COLUMN error_msg VARCHAR(500) NOT NULL DEFAULT '' AFTER finished_at,
  ADD KEY idx_run_status (status, created_at);

ALTER TABLE replay_run_items
  ADD COLUMN error_msg VARCHAR(500) NOT NULL DEFAULT '' AFTER hard_fail,
  ADD COLUMN updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
      ON UPDATE CURRENT_TIMESTAMP(3) AFTER created_at;
