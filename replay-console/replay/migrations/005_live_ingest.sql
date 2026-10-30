-- 005_live_ingest.sql
-- 实时手牌结构化入库 + 线上建议审计。
-- 仍然不写 review_hands；导入手牌继续走 replay_imported_hands。

ALTER TABLE replay_decisions
  ADD COLUMN action_index INT NOT NULL DEFAULT 0
      COMMENT '该决策点之前的动作条数；与线上 advice.action_index 对齐';

ALTER TABLE replay_imported_hands
  ADD COLUMN external_uid VARCHAR(128) NULL
      COMMENT '来源内的稳定 uid；实时新记录=hand_id，旧记录=session+hand_id',
  ADD COLUMN schema_version INT NOT NULL DEFAULT 1
      COMMENT '1=旧记录，2=HandRecord v2',
  ADD COLUMN result_source VARCHAR(16) NULL
      COMMENT 'unknown/stack_delta/manual/import',
  ADD COLUMN table_roster JSON NULL
      COMMENT '开手时在座所有人（不只是本手发牌的人）',
  ADD UNIQUE KEY uk_imported_user_uid (user_id, source, external_uid),
  ADD KEY idx_imported_uid (external_uid);

CREATE TABLE IF NOT EXISTS replay_hand_players (
  hand_id      BIGINT UNSIGNED NOT NULL,
  seat_id      VARCHAR(16) NOT NULL,
  player_key   VARCHAR(128) NULL COMMENT '全局画像键（名字归一）',
  display_name VARCHAR(128) NULL,
  position     VARCHAR(10) NULL,
  is_hero      TINYINT(1) NOT NULL DEFAULT 0,
  in_hand      TINYINT(1) NOT NULL DEFAULT 1,
  start_stack  DOUBLE NULL COMMENT 'BB',
  hole_cards   VARCHAR(8) NULL,
  PRIMARY KEY (hand_id, seat_id),
  KEY idx_hp_player (player_key),
  KEY idx_hp_name (display_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='手牌座位/玩家：支持按玩家、按桌复盘';

CREATE TABLE IF NOT EXISTS replay_live_advices (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  hand_id        BIGINT UNSIGNED NOT NULL,
  decision_id    BIGINT UNSIGNED NULL COMMENT '对齐 replay_decisions.id；可后补',
  street         VARCHAR(10) NOT NULL,
  action_index   INT NOT NULL DEFAULT 0 COMMENT '手牌行动线里的第几个动作',
  decision_key   VARCHAR(255) NULL,
  option_id      VARCHAR(32) NULL,
  option_kind    VARCHAR(16) NULL,
  option_amount  DOUBLE NULL,
  option_label   VARCHAR(64) NULL,
  reason         TEXT NULL,
  source         VARCHAR(16) NOT NULL COMMENT 'model/fallback',
  model          VARCHAR(100) NULL,
  prompt_version VARCHAR(32) NULL,
  context_json   JSON NULL COMMENT 'table_stats/opponent_reads/table_dynamics/roster',
  duration_ms    INT NULL,
  tokens_in      INT NULL,
  tokens_out     INT NULL,
  actual_action  VARCHAR(10) NULL,
  actual_amount  DOUBLE NULL,
  bucket         VARCHAR(16) NULL COMMENT 'same/action/size/error',
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_la_hand (hand_id),
  KEY idx_la_decision (decision_id),
  KEY idx_la_street (street),
  UNIQUE KEY uk_la_identity (hand_id, street, action_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='线上实际给出的建议快照（不是重跑结果）';

CREATE TABLE IF NOT EXISTS replay_decision_marks (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     BIGINT UNSIGNED NOT NULL DEFAULT 0,
  decision_id BIGINT UNSIGNED NOT NULL,
  verdict     VARCHAR(16) NOT NULL DEFAULT 'unsure' COMMENT 'good/bad/unsure',
  tags        JSON NULL,
  note        TEXT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
              ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_dm_user_decision (user_id, decision_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='逐决策点人工复盘结论';
