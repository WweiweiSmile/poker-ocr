-- 导出所有复盘手牌的完整行动路线为 JSONL，直接喂给 tools/replay_dev_hands.py。
--
-- 用法（在 ~/codes/call-back 目录下跑，那里有 DB_* 的 .env）：
--
--   export $(grep -E '^DB_' .env | xargs)
--   mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" -N \
--     < ~/codes/poker-ocr/tools/export_hands.sql > /tmp/hands.jsonl
--   ~/codes/poker-ocr/.venv/bin/python ~/codes/poker-ocr/tools/replay_dev_hands.py /tmp/hands.jsonl
--
-- 每行一个 JSON 对象 = 一手牌（JSON_OBJECT 会把换行转义掉，所以一行一手，方便 JSONL 解析）。
-- streets 就是行动路线：每街 [{street, actions:[{actor, action, amountBb}], potStartBb?}]。
-- amountBb 对 bet/raise 是「加到多少」，与 coach 引擎口径一致；potStartBb 有则优先用、
-- 没有（多为空）就由重放脚本从盲注+行动重建。

SELECT JSON_OBJECT(
  'id',           id,
  'userId',       user_id,
  'createdAt',    created_at,
  'title',        title,
  'tableSize',    table_size,
  'heroPosition', hero_position,
  'heroCards',    hero_cards,
  'heroStackBb',  hero_stack_bb,
  'stakes',       stakes,
  'sb',           small_blind_bb,
  'bb',           big_blind_bb,
  'ante',         ante_bb,
  'bombPot',      bomb_pot_bb,
  'board',        IFNULL(board, ''),
  'villainCount', villain_count,
  'villains',     villains,
  'potType',      pot_type,
  'streets',      streets,
  'heroThought',  hero_thought,
  'result',       result,
  'resultAmount', result_amount,
  'heroTags',     hero_tags
)
FROM review_hands
WHERE deleted_at IS NULL
ORDER BY id;
