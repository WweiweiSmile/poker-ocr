-- 002_imported_hands.sql
-- 手牌库支持导入「复盘手牌」和「实时日志手牌记录」。
-- 仍然不写 review_hands；导入的手牌存 replay_imported_hands，收藏/决策/结果
-- 复用 replay_marks / replay_decisions / replay_results。
--
-- id 从 1e12 起，避免和 review_hands 的自增 id 撞车；所有 hand_id 列都是
-- BIGINT UNSIGNED，可直接存。

CREATE TABLE IF NOT EXISTS replay_import_batches (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id        BIGINT UNSIGNED NOT NULL DEFAULT 0,
  source         VARCHAR(16) NOT NULL COMMENT 'review/live',
  container      VARCHAR(16) NOT NULL DEFAULT 'json' COMMENT 'json/jsonl',
  filename       VARCHAR(255) NULL,
  total_records  INT NOT NULL DEFAULT 0,
  imported_count INT NOT NULL DEFAULT 0,
  updated_count  INT NOT NULL DEFAULT 0,
  duplicate_count INT NOT NULL DEFAULT 0,
  invalid_count  INT NOT NULL DEFAULT 0,
  error_count    INT NOT NULL DEFAULT 0,
  status         VARCHAR(16) NOT NULL DEFAULT 'running'
                 COMMENT 'running/succeeded/failed',
  error_msg      VARCHAR(500) NULL,
  summary_json   JSON NULL,
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  finished_at    DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_import_batch_user_created (user_id, created_at),
  KEY idx_import_batch_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  AUTO_INCREMENT=1000000000001
  COMMENT='手牌导入批次：审计每次导入的来源与计数';

CREATE TABLE IF NOT EXISTS replay_imported_hands (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT
                    COMMENT '全局手牌 id，从 1e12 起避开 review_hands',
  user_id           BIGINT UNSIGNED NOT NULL DEFAULT 0,
  batch_id          BIGINT UNSIGNED NULL COMMENT '最近一次导入批次',
  source            VARCHAR(16) NOT NULL COMMENT 'review/live',
  external_id       VARCHAR(128) NOT NULL DEFAULT ''
                    COMMENT '复盘 id / 实时 uid，仅追溯，不参与唯一键',
  fingerprint       CHAR(64) NOT NULL,
  title             VARCHAR(255) NULL,
  hero_cards        VARCHAR(8) NOT NULL DEFAULT '',
  board             VARCHAR(10) NOT NULL DEFAULT '',
  hero_position     VARCHAR(10) NULL,
  table_size        INT NOT NULL DEFAULT 0,
  hero_stack_bb     DOUBLE NULL,
  small_blind_bb    DOUBLE NULL,
  big_blind_bb      DOUBLE NULL,
  ante_bb           DOUBLE NULL,
  bomb_pot_bb       DOUBLE NULL,
  villain_count     INT NOT NULL DEFAULT 0,
  villains          JSON NULL,
  streets           JSON NOT NULL,
  result            VARCHAR(16) NOT NULL DEFAULT 'unknown',
  result_amount     DOUBLE NULL,
  pot_type          VARCHAR(16) NULL,
  hero_tags         JSON NULL,
  payload_json      MEDIUMTEXT NOT NULL COMMENT '归一化后的手牌内容',
  raw_json          MEDIUMTEXT NOT NULL COMMENT '原始导入记录，便于追溯',
  source_created_at DATETIME(3) NULL COMMENT '原始手牌时间；created_at 用它',
  created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                    ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_imported_user_source_fp (user_id, source, fingerprint),
  KEY idx_imported_user_created (user_id, created_at),
  KEY idx_imported_fingerprint (fingerprint),
  KEY idx_imported_batch (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  AUTO_INCREMENT=1000000000001
  COMMENT='从复盘 JSON / 实时日志导入的手牌';
