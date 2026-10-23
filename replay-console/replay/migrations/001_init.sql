-- 001_init.sql
-- 手牌复跑控制台：只新增 replay_* 表，不修改 review_hands。
-- 全部是逻辑外键：review_hands 由 call-back 的 GORM 管理，避免迁移互相牵制。

CREATE TABLE IF NOT EXISTS replay_versions (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id             BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '0=内部工具；将来接多用户',
  name                VARCHAR(100) NOT NULL COMMENT '展示名，如 flash-lean-1023',
  provider            VARCHAR(32)  NOT NULL DEFAULT 'deepseek' COMMENT 'deepseek/openrouter/openai...',
  base_url            VARCHAR(255) NOT NULL,
  model               VARCHAR(100) NOT NULL,
  api_key_ref         VARCHAR(64)  NOT NULL DEFAULT 'AI_SERVER_API_KEY' COMMENT '只存环境变量名，不存 Key',
  temperature         DOUBLE NULL,
  top_p               DOUBLE NULL,
  max_tokens          INT NULL,
  thinking            VARCHAR(16) NULL COMMENT 'enabled/disabled',
  reasoning_effort    VARCHAR(16) NULL COMMENT 'none/low/high/max',
  prompt_style        VARCHAR(16) NOT NULL DEFAULT 'lean' COMMENT 'lean/skills',
  advice_version      VARCHAR(32) NOT NULL COMMENT '提示词+事实+选项语义版本，人工 bump',
  reconstruct_version VARCHAR(16) NOT NULL DEFAULT 'recon-1',
  engine_commit       CHAR(40) NULL COMMENT 'poker-ocr git commit，仅追溯',
  engine_dirty        TINYINT(1) NOT NULL DEFAULT 0,
  table_dynamics      MEDIUMTEXT NULL COMMENT '创建时的 .table_dy 快照；运行时不再读文件',
  table_dynamics_sha  CHAR(64) NULL,
  config_hash         CHAR(64) NOT NULL,
  notes               TEXT NULL,
  archived            TINYINT(1) NOT NULL DEFAULT 0,
  created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ver_user_name (user_id, name),
  UNIQUE KEY uk_ver_user_hash (user_id, config_hash),
  KEY idx_ver_user_created (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='AI 版本档案：不可变快照';

CREATE TABLE IF NOT EXISTS replay_jobs (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id          BIGINT UNSIGNED NOT NULL DEFAULT 0,
  parent_job_id    BIGINT UNSIGNED NULL COMMENT '重试/补跑时指向原任务',
  scope            VARCHAR(16) NOT NULL COMMENT 'hands/favorites/filter',
  version_ids      JSON NOT NULL COMMENT '本次包含的版本 id，创建时冻结',
  hand_ids         JSON NOT NULL COMMENT '本次包含的手牌 id，创建时冻结',
  repeat_count     INT NOT NULL DEFAULT 1,
  status           VARCHAR(16) NOT NULL DEFAULT 'queued'
                   COMMENT 'queued/running/succeeded/partial/failed/canceled',
  total_hands      INT NOT NULL DEFAULT 0,
  total_decisions  INT NOT NULL DEFAULT 0 COMMENT '去重后的决策点数',
  total_units      INT NOT NULL DEFAULT 0 COMMENT '决策点 × 版本 × repeat',
  done_units       INT NOT NULL DEFAULT 0,
  failed_units     INT NOT NULL DEFAULT 0,
  skipped_units    INT NOT NULL DEFAULT 0 COMMENT 'invalid/重复手牌跳过的单位',
  cancel_requested TINYINT(1) NOT NULL DEFAULT 0,
  config_snapshot  JSON NOT NULL COMMENT '版本配置 + 代码版本 + 重建版本的第二道快照',
  note             VARCHAR(255) NULL,
  error_msg        VARCHAR(500) NULL,
  worker_id        VARCHAR(64) NULL,
  heartbeat_at     DATETIME(3) NULL,
  created_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  started_at       DATETIME(3) NULL,
  finished_at      DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_job_user_created (user_id, created_at),
  KEY idx_job_status (status),
  KEY idx_job_parent (parent_job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='批量重跑任务';

CREATE TABLE IF NOT EXISTS replay_job_hands (
  job_id           BIGINT UNSIGNED NOT NULL,
  hand_id          BIGINT UNSIGNED NOT NULL,
  hand_fingerprint CHAR(64) NOT NULL COMMENT '创建时的手牌指纹，用于判断是否被改过',
  status           VARCHAR(16) NOT NULL DEFAULT 'pending'
                   COMMENT 'pending/running/done/invalid/skipped/failed',
  decision_count   INT NOT NULL DEFAULT 0,
  done_units       INT NOT NULL DEFAULT 0,
  error_msg        VARCHAR(500) NULL,
  created_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (job_id, hand_id),
  KEY idx_jh_hand_created (hand_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='任务范围与逐手进度';

CREATE TABLE IF NOT EXISTS replay_decisions (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  hand_id             BIGINT UNSIGNED NOT NULL,
  hand_fingerprint    CHAR(64) NOT NULL,
  reconstruct_version VARCHAR(16) NOT NULL,
  street              VARCHAR(10) NOT NULL COMMENT 'preflop/flop/turn/river',
  seq                 INT NOT NULL COMMENT '整手牌内第几个 hero 决策点，0 起',
  pot                 DOUBLE NOT NULL,
  to_call             DOUBLE NOT NULL,
  hero_stack          DOUBLE NULL,
  hero_position       VARCHAR(10) NULL,
  hero_cards          VARCHAR(8) NOT NULL,
  board               VARCHAR(10) NOT NULL,
  players             INT NOT NULL DEFAULT 0,
  opponents           INT NOT NULL DEFAULT 0,
  actual_action       VARCHAR(10) NULL,
  actual_amount       DOUBLE NULL,
  quality             VARCHAR(10) NOT NULL DEFAULT 'exact' COMMENT 'exact/approx/invalid',
  warnings            JSON NULL,
  snapshot_json       MEDIUMTEXT NOT NULL COMMENT 'HandSnapshot 序列化，重建结果可审计',
  created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_dec_identity (hand_id, hand_fingerprint, reconstruct_version, street, seq),
  KEY idx_dec_hand (hand_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='从 review_hands 重放出的 hero 决策点';

CREATE TABLE IF NOT EXISTS replay_results (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id         BIGINT UNSIGNED NOT NULL,
  decision_id    BIGINT UNSIGNED NOT NULL,
  version_id     BIGINT UNSIGNED NOT NULL,
  repeat_index   INT NOT NULL DEFAULT 0,
  option_id      VARCHAR(32) NULL,
  option_kind    VARCHAR(16) NULL COMMENT 'fold/check/call/bet/raise/allin',
  option_amount  DOUBLE NULL COMMENT 'bet/raise 的加到多少（BB）',
  option_label   VARCHAR(64) NULL,
  reason         TEXT NULL,
  source         VARCHAR(16) NOT NULL COMMENT 'model/fallback/error',
  model          VARCHAR(100) NULL COMMENT '模型返回或配置的模型名',
  duration_ms    INT NULL,
  tokens_in      INT NULL,
  tokens_out     INT NULL,
  bucket         VARCHAR(16) NULL COMMENT 'same/size/action/error，与实际动作比',
  violation_tags JSON NULL COMMENT '硬规则审计标签，可为空',
  facts_json     JSON NULL COMMENT '该版本实际看到的 Situation 事实',
  prompt_system  MEDIUMTEXT NULL,
  prompt_user    MEDIUMTEXT NULL,
  raw_response   MEDIUMTEXT NULL,
  error_msg      VARCHAR(500) NULL,
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_res_unit (job_id, decision_id, version_id, repeat_index),
  KEY idx_res_job (job_id),
  KEY idx_res_decision (decision_id),
  KEY idx_res_version (version_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='逐决策点、逐版本的模型建议';

CREATE TABLE IF NOT EXISTS replay_marks (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id    BIGINT UNSIGNED NOT NULL DEFAULT 0,
  hand_id    BIGINT UNSIGNED NOT NULL,
  favorite   TINYINT(1) NOT NULL DEFAULT 0,
  tags       JSON NULL COMMENT '如 ["典型","边界","漏点"]',
  status     VARCHAR(16) NOT NULL DEFAULT 'todo' COMMENT 'todo/confirmed/disputed',
  note       TEXT NULL COMMENT '一句话结论',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
             ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_mark_user_hand (user_id, hand_id),
  KEY idx_mark_user_fav (user_id, favorite)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='手牌收藏、标签、人工结论';
