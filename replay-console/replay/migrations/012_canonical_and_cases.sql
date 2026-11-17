-- 012_canonical_and_cases.sql
-- 手牌唯一实体（canonical）、源记录映射、决策点补料，以及第 2 期才接线的案例/运行表。
-- 形状见 docs/手牌库-AB案例-一体化设计-2026-11-17.md §3.1–§3.3。
--
-- 本票（#51）只接线前两张表 + replay_decisions 的新列；案例/运行表先建空表，
-- 不阻塞旧链路（旧代码只 SELECT 已知列，新增列可空，写路径不受影响）。
--
-- 编号：接在 011_version_channel.sql 之后。本目录另有 012_spot_specs.sql（#54），
-- 两个 012 是并存的两份迁移（文件名去 .sql 后是两个 version，各记一行），
-- 谁先应用都不冲突。replay_spot_specs 的唯一归属是 012_spot_specs.sql，
-- 这里不重复创建——否则列形状会取决于文件名的字典序。
--
-- 幂等：跑两次不报错靠 replay_schema_migrations 的 version 记录（同 005/008/009/011）。
-- 建表全是 CREATE TABLE IF NOT EXISTS，手工重放也安全；但 MySQL 没有
-- ADD COLUMN IF NOT EXISTS，那几个 ALTER 只能靠 version 记录只跑一次——真要手工
-- 重放，先删掉本文件的 version 行、确认列不存在，否则会报 Duplicate column name。

-- 手牌唯一实体：同内容的多条源记录归到这一行
CREATE TABLE IF NOT EXISTS replay_canonical_hands (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '从 2e12 起，与导入 id 的 1e12 分开',
  content_group  CHAR(64) NULL COMMENT '同内容手牌归组键；空=未归组',
  primary_source VARCHAR(16) NOT NULL DEFAULT 'live'
                 COMMENT 'review/live/synthetic；案例默认按这个来源取快照',
  title          VARCHAR(255) NULL,
  hero_cards     VARCHAR(8)  NOT NULL DEFAULT '',
  board          VARCHAR(10) NOT NULL DEFAULT '',
  hero_position  VARCHAR(10) NULL,
  started_at     DATETIME(3) NULL COMMENT '手牌发生时间，归组用',
  is_synthetic   TINYINT(1) NOT NULL DEFAULT 0,
  merged_into    BIGINT UNSIGNED NULL COMMENT '人工合并时指向保留的那一行',
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                 ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_ch_group (content_group),
  KEY idx_ch_started (started_at),
  KEY idx_ch_merged (merged_into)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  AUTO_INCREMENT=2000000000001
  COMMENT='canonical 手牌：唯一实体，案例与统计只认它';

-- 源记录：一手牌可以有多条（复盘 + 实时 + 构造），并列保存、互不覆盖
CREATE TABLE IF NOT EXISTS replay_hand_sources (
  canonical_id   BIGINT UNSIGNED NOT NULL,
  source         VARCHAR(16) NOT NULL COMMENT 'review/live/synthetic',
  source_hand_id BIGINT UNSIGNED NOT NULL COMMENT 'review_hands.id / 导入手牌 id / 构造手牌 id',
  fingerprint    CHAR(64) NULL,
  is_primary     TINYINT(1) NOT NULL DEFAULT 0,
  linked_by      VARCHAR(8) NOT NULL DEFAULT 'auto' COMMENT 'auto/manual',
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (source, source_hand_id),
  KEY idx_hs_canonical (canonical_id),
  KEY idx_hs_fp (fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='源记录 → canonical 的映射；多来源并列，不覆盖';

-- 决策点补料：canonical + 七个桶维度（其余 facts 仍在 snapshot_json / run_item 里）
ALTER TABLE replay_decisions
  ADD COLUMN canonical_id     BIGINT UNSIGNED NULL,
  ADD COLUMN position_bucket  VARCHAR(8)  NULL COMMENT 'EP/MP/LP/盲注/?',
  ADD COLUMN hand_tier        VARCHAR(16) NULL COMMENT '教练 9 档的稳定键，见 §3.3',
  ADD COLUMN board_texture    VARCHAR(16) NULL COMMENT '干燥/花面/顺面/花顺面/公对面',
  ADD COLUMN pot_type         VARCHAR(16) NULL COMMENT 'limped/single/3bet+/bomb/unknown',
  ADD COLUMN facing           TINYINT(1)  NULL COMMENT '1=面对下注（to_call>0）',
  ADD COLUMN opponent_profile VARCHAR(8)  NULL COMMENT '紧弱/松弱/松凶/紧凶/未知',
  ADD KEY idx_dec_canonical (canonical_id, street, seq),
  ADD KEY idx_dec_bucket (hand_tier, board_texture, position_bucket, pot_type);

-- 案例集：冻结的一版案例集合
CREATE TABLE IF NOT EXISTS replay_case_sets (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  revision    INT NOT NULL DEFAULT 1,
  name        VARCHAR(100) NOT NULL,
  purpose     VARCHAR(16) NOT NULL DEFAULT 'golden'
              COMMENT 'golden/regression/exploration/coverage',
  frozen_at   DATETIME(3) NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_cs_name_rev (name, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='冻结的案例集版本';

-- 案例：一个决策点 + 一个 spec（可实例级覆盖）
CREATE TABLE IF NOT EXISTS replay_cases (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  case_set_id    BIGINT UNSIGNED NOT NULL,
  canonical_id   BIGINT UNSIGNED NOT NULL,
  decision_key   VARCHAR(48) NOT NULL COMMENT 'street:seq，如 flop:2',
  spec_id        BIGINT UNSIGNED NOT NULL,
  priority       TINYINT NOT NULL DEFAULT 1 COMMENT '0=P0',
  overrides_json JSON NULL COMMENT '实例级覆盖：动作/尺寸/要点',
  status         VARCHAR(12) NOT NULL DEFAULT 'draft' COMMENT 'draft/active/retired',
  note           VARCHAR(255) NULL,
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_case (case_set_id, canonical_id, decision_key),
  KEY idx_case_spec (spec_id),
  KEY idx_case_prio (case_set_id, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='实例案例：一个决策点 + 一个 spec（可覆盖）';

-- 运行：一次「版本 × 冻结案例集」的评分
CREATE TABLE IF NOT EXISTS replay_runs (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  case_set_id         BIGINT UNSIGNED NOT NULL,
  case_set_revision   INT NOT NULL,
  spec_ids_json       JSON NOT NULL COMMENT '本次冻结的 spec 版本集合',
  version_id          BIGINT UNSIGNED NOT NULL COMMENT '建议版本（replay_versions）',
  judge_version       VARCHAR(32) NOT NULL,
  reconstruct_version VARCHAR(16) NOT NULL,
  status              VARCHAR(12) NOT NULL DEFAULT 'queued',
  total_cases         INT NOT NULL DEFAULT 0,
  scored_cases        INT NOT NULL DEFAULT 0,
  score               DOUBLE NULL COMMENT '案例均分 0-100',
  p0_pass_rate        DOUBLE NULL,
  coverage_rate       DOUBLE NULL,
  config_snapshot     JSON NULL,
  created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  finished_at         DATETIME(3) NULL,
  PRIMARY KEY (id),
  KEY idx_run_set (case_set_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='一次评分运行：版本 × 冻结案例集';

-- 逐案例逐重复的建议与判定
CREATE TABLE IF NOT EXISTS replay_run_items (
  run_id        BIGINT UNSIGNED NOT NULL,
  case_id       BIGINT UNSIGNED NOT NULL,
  spec_id       BIGINT UNSIGNED NOT NULL,
  repeat_index  INT NOT NULL DEFAULT 0,
  option_id     VARCHAR(32) NULL,
  option_kind   VARCHAR(16) NULL,
  option_amount DOUBLE NULL,
  reason        TEXT NULL,
  source        VARCHAR(16) NOT NULL COMMENT 'model/fallback/error',
  facts_json    JSON NULL,
  prompt_json   JSON NULL,
  judge_json    JSON NULL COMMENT '要点命中/幻觉/逐维度判定',
  scores_json   JSON NULL COMMENT '{"action":1,"size":0.7,"reason":0.5,"fact":1,"stable":1}',
  score         DOUBLE NULL,
  hard_fail     VARCHAR(32) NULL COMMENT 'HF_* 见 §7.2',
  duration_ms   INT NULL,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (run_id, case_id, repeat_index),
  KEY idx_ri_fail (run_id, hard_fail),
  KEY idx_ri_score (run_id, score)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='逐案例逐重复的建议与判定';

-- 人工改判：不覆盖原始判定，聚合时优先
CREATE TABLE IF NOT EXISTS replay_judge_overrides (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id      BIGINT UNSIGNED NOT NULL,
  case_id     BIGINT UNSIGNED NOT NULL,
  scores_json JSON NULL,
  hard_fail   VARCHAR(32) NULL,
  note        TEXT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_override (run_id, case_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='人工复核结果；聚合时优先于机器判定';
