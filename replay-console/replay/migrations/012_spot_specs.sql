-- 012_spot_specs.sql
-- 期望库：挂在桶上的场景期望。每次编辑插入新 revision，旧行只读。
-- 形状见设计 §3.2 / §4.1；桶维度存 bucket_json，其余字段存 spec_json。
-- 与 #51 的 012_canonical_and_cases.sql 并存：两边都是 CREATE TABLE IF NOT
-- EXISTS，谁先应用都不会冲突。

CREATE TABLE IF NOT EXISTS replay_spot_specs (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  revision    INT NOT NULL DEFAULT 1,
  name        VARCHAR(120) NOT NULL,
  status      VARCHAR(12) NOT NULL DEFAULT 'draft'
              COMMENT 'draft/active',
  bucket_json JSON NOT NULL COMMENT '维度子集；缺省/空数组 = 通配，见 §4.1',
  spec_json   JSON NOT NULL
              COMMENT 'schema/allowed/forbidden/sizing/freq/points/rationale',
  note        TEXT NULL,
  supersedes  BIGINT UNSIGNED NULL COMMENT '派生自哪个 revision',
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_spec_name_rev (name, revision),
  KEY idx_spec_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='场景期望；每次编辑 = 新 revision，旧行只读';
