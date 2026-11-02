-- 008_live_prompt_and_flags.sql
-- 线上真实 prompt / raw response / 事实快照 + 手牌回放时间线 / 深挖位图。
-- 只加列/索引，不改旧表语义；旧记录 prompt_missing=1（由 ingest 侧写入）。

ALTER TABLE replay_live_advices
  ADD COLUMN prompt_system MEDIUMTEXT NULL COMMENT 'system prompt 原文',
  ADD COLUMN prompt_user MEDIUMTEXT NULL COMMENT 'user prompt 原文',
  ADD COLUMN prompt_sha256 CHAR(64) NULL COMMENT 'sha256(system + sep + user)',
  ADD COLUMN raw_response MEDIUMTEXT NULL COMMENT '模型原始响应 JSON/文本',
  ADD COLUMN response_sha256 CHAR(64) NULL,
  ADD COLUMN reason_full TEXT NULL COMMENT '未截断的模型理由',
  ADD COLUMN facts_json JSON NULL COMMENT '建议时事实层快照',
  ADD COLUMN model_params_json JSON NULL COMMENT '采样参数，不含 api_key',
  ADD COLUMN prompt_missing TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN prompt_missing_reason VARCHAR(64) NULL,
  ADD COLUMN captured_at DATETIME(3) NULL,
  ADD COLUMN attempt INT NOT NULL DEFAULT 1,
  ADD KEY idx_la_prompt_sha (prompt_sha256),
  ADD KEY idx_la_prompt_missing (prompt_missing);

ALTER TABLE replay_imported_hands
  ADD COLUMN record_schema VARCHAR(16) NOT NULL DEFAULT 'hand.v2',
  ADD COLUMN timeline_json MEDIUMTEXT NULL COMMENT 'ReplayTimeline JSON',
  ADD COLUMN timeline_version VARCHAR(16) NULL,
  ADD COLUMN flag_mask BIGINT NOT NULL DEFAULT 0 COMMENT '深挖筛选位图',
  ADD COLUMN flags_json JSON NULL COMMENT 'flag -> detail/reason',
  ADD COLUMN review_score TINYINT UNSIGNED NOT NULL DEFAULT 0
      COMMENT '0-100 深挖排序分，仅启发式',
  ADD KEY idx_imported_flag_mask (flag_mask),
  ADD KEY idx_imported_review_score (review_score),
  ADD KEY idx_imported_timeline_version (timeline_version);

ALTER TABLE replay_results
  ADD COLUMN prompt_sha256 CHAR(64) NULL,
  ADD COLUMN reason_full TEXT NULL,
  ADD KEY idx_res_prompt_sha (prompt_sha256);
