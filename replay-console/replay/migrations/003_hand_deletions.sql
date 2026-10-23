-- 003_hand_deletions.sql
-- 删除手牌：
--   · 导入手牌：replay_imported_hands.deleted_at 软删除，保留收藏/决策点/结果；
--   · review_hands 手牌：业务表只读，不改它；在 replay_hand_deletions 里记一条
--     「本控制台隐藏」，列表/详情过滤掉。重新导入同内容导入手牌会恢复。

ALTER TABLE replay_imported_hands
  ADD COLUMN deleted_at DATETIME(3) NULL COMMENT '软删除时间；NULL=正常',
  ADD KEY idx_imported_user_deleted (user_id, deleted_at);

CREATE TABLE IF NOT EXISTS replay_hand_deletions (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id    BIGINT UNSIGNED NOT NULL DEFAULT 0,
  hand_id    BIGINT UNSIGNED NOT NULL COMMENT 'review_hands.id，只隐藏不改业务表',
  deleted_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_del_user_hand (user_id, hand_id),
  KEY idx_del_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='控制台隐藏的 review_hands 手牌；不修改生产业务表';
