# 手牌库 2.0：实时沉淀、双模式复盘与 A/B 回归设计

> 状态：设计稿 v1.0（2026-11-01）
> 范围：`poker_ocr` 实时链路 + `replay-console` 手牌库
> 关联文档：`docs/桌子动态重构与手牌库打通-方案.md`、`docs/复跑控制台-设计文档.md`、
> `docs/AB测试-手牌集.md`、`docs/live-stream.md`、`replay-console/README.md`
>
> 一句话：**不新建第二套手牌库**。在现有 `replay-console` 上补齐
> 「实时 prompt 快照 + 自动入库 + 事件时间线 + 手牌集 + 成对 A/B」，
> 让每手牌自动成为可检索、可回放、可回归的资产。
>
> 第一版实现状态和已知边界见
> [`docs/手牌库-实现说明-2026-11-02.md`](手牌库-实现说明-2026-11-02.md)。
>
> 阅读路线：先看 §0 结论 → §8.8「baseline 的 prompt 从哪来」→ §15 待确认；
> 要动手实现从 §12 开始。

---

## 0. 一页结论

### 0.1 现状不是「没有」，而是「缺了三块」

| 能力 | 现在有什么 | 缺什么 |
|---|---|---|
| 手牌自动保存 | live 结束后写 `data/hand_outbox/pending/`，一手一个 JSON；`replay-console` 有 `POST /api/hands/ingest` | outbox 到数据库之间还要手动跑 `tools/sync_hand_outbox.py`；没有常驻同步 |
| 线上建议 | `advices` 里有 option/reason/source/model/context/token | **没有存真正发给模型的 system/user prompt**；`reason` 被截到 40 字；没有完整 raw response |
| 复盘 | 手牌详情有行动街、决策点、多版本结果；`replay_results` 已存重跑 prompt | 没有瀑布流；没有牌桌逐帧回放；线上 prompt 只能靠重跑重建 |
| A/B | `replay_experiments` 已能冻结手牌、跑 baseline/variant、按 dev/holdout 看 agreement | 没有「手牌集」对象；没有成对人工偏好；没有 P0 断言；没有 prompt diff |

### 0.2 这次要做的五件事

1. **`AdviceSnapshot`**：在 `advise()` 发出的同一刻，保存精确的
   `system`、`user`、`raw_response`、`reason_full`、`facts`、`model_params`、
   `prompt_sha256`。这是「看到 prompt」和「以后做 A/B 基线」的共同基础。
2. **`HandIngestWorker`**：live 进程内嵌一个非阻塞同步线程，outbox 落盘后自动
   `POST /api/hands/ingest`；服务离线时留在 pending，恢复后自动补。保留
   `tools/sync_hand_outbox.py` 作为回填工具。
3. **`ReplayTimeline`**：从手牌记录生成唯一的事件时间线；瀑布流和牌桌回放
   共用同一份 frames，避免两个视图各算一套。
4. **`HandSet`**：手牌库里的「评测集」一等公民。选牌 → 冻结 revision →
   实验引用 revision；改 prompt/事实/模型后一键回归。
5. **成对 A/B**：baseline 用**线上那次真实 prompt**（或上一次实验的 prompt
   快照）重放，variant 用新代码重渲染；结果按决策点成对展示，支持
   A 更好 / B 更好 / 一样 / 不确定、P0 断言、分街/分 spot 指标。

### 0.3 三条不可妥协的原则

- **事实、观测、实验分开**：`HandRecord` 是事实，`AdviceSnapshot` 是一次观测，
  `Experiment` 是一次对照。不要把「模型建议」写回手牌事实。
- **不猜**：OCR 漏了动作、缺起始筹码、prompt 没存，就显式标
  `missing/approx/derived`，不静默补一个好看的数字。
- **实际行动不是 ground truth**：`agreement` 只能说明「像不像人」，不能说明
  「对不对」。判断一次修改是否合理，靠 **P0 不回归 + 人工成对偏好 + holdout**。

### 0.4 做完之后的体验

- 一手牌结束 **≤10 秒**出现在手牌库；即使手牌库没开，也会在服务恢复后自动补。
  （严格说是「换手被识别后 ≤10 秒」；最后一手要等退出/下一次换手才触发。）
- 打开任意一手牌，可以：
  - 用**文字瀑布流**从翻前看到河牌，每个 AI 决策点就地展开 prompt / 建议 / 理由；
  - 切到**牌桌模式**，按「下一帧」逐动作播放，AI 决策时弹出建议气泡；
  - 在任意决策点打开 **Prompt Inspector**，看 system/user/raw/facts，和另一个
    版本做逐行 diff。
- 在手牌库勾选 30–100 手加入「我的回归集」；改完 prompt 后一键跑 A/B，
  先看 P0 是否回归，再看成对偏好和 holdout，最后给出 adopt/reject/iterate。

---

## 1. 目标与非目标

### 1.1 目标

| 编号 | 目标 | 可验收的结果 |
|---|---|---|
| G1 | 实时建议完整沉淀 | 每个 hero 决策点都有 option、reason、prompt、raw response、context、耗时、token；没有模型调用时显式标 `fallback` |
| G2 | 手牌自动进库 | 手牌结束后无需手动导入；断网/服务离线不丢；重复发送不重复入库 |
| G3 | 双模式复盘 | 瀑布流与牌桌回放都能从顶到尾走完一手牌；两种模式看到同一组事件、同一条建议、同一份 prompt |
| G4 | 快速找到值得深挖的手牌 | 列表可按「AI 与实际行动分歧 / 兜底 / 数据异常 / 大底池 / A/B 翻转 / 人工标坏」筛选和排序 |
| G5 | 手牌集 A/B | 可命名、可增删、可冻结、可复用；改一次 prompt 后一条命令/一次点击完成回归 |
| G6 | 可追溯 | 任一结果能回答：哪个版本、哪个 commit、哪份 prompt、哪份 context、哪次实验、谁在什么时候标了什么 |

### 1.2 非目标

- **不做牌桌内的实时 overlay**：live 仍然在终端出建议、可选自动点击；本设计只做「记录 + 复盘」。
- **不做 GTO solver**：不引入新的策略求解器；A/B 的结论来自对照和人工判断。
- **不默认保存整段视频**：牌桌回放是「语义帧回放」，不是视频播放器。需要时再挂关键帧截图。
- **不重写 OCR / hand tracker**：数据质量问题用质量标记和门槛处理，不在这一版重做识别。
- **不做多用户 SaaS**：继续沿用 `user_id=0` 的单用户模型；表结构预留 `user_id`，但鉴权不在本期。
- **不把 `agreement` 当正确率**：这是本设计里最容易被误用的指标，文档和 UI 都要明确写。

---

## 2. 现状盘点（以当前代码为准）

### 2.1 实时链路

```text
FrameState
  → LiveHandTracker.observe()
  → 轮到我时产生 HandSnapshot（决策点）
  → AdviceRunner 后台线程
  → advice.advise(sit)
      → build_messages(sit) 得到 system/user
      → complete_json() 调模型
      → Advice(option/reason/source/context/...)
  → live.drain_advice()
      → self._hand_advice[hand_id].append(advice.to_dict())
      → writer.write({"type":"advice", ...})
  → 换手 / 退出
      → _write_hand(hand)
      → record["advices"] = self._hand_advice.pop(hand_id)
      → logs/live.states.jsonl 写一条 {"type":"hand", ...}
      → hand_outbox.enqueue(record)
```

关键事实：

- `poker_ocr/advice.py::Advice.to_dict()` **没有** `prompt_system` / `prompt_user` /
  `raw_response` 字段；`prompt_version` 只是版本号，不是提示词正文。
- `advise()` 里 `system, user = build_messages(sit)` 之后直接调用模型，
  没有把这两个字符串交给 `Advice`。
- `reason` 在 `advise()` 里经过 `_clip_reason(..., limit=40)`，落库的是截断版；
  完整理由只在模型原始响应里，而原始响应没有保存。
- `Advice.context` 已经保存 `table_dynamics`、`table_stats`、`opponent_reads`，
  这是 A/B 同上下文的好基础；但没有保存 `table_roster`、完整 facts、模型采样参数。
- `poker_ocr/live.py::_write_hand()` 只对 `not partial` 的手调
  `hand_outbox.enqueue(record)`；退出时的截断手牌不会进 outbox。
- `advice.action_index` 与 replay 的 `DecisionPoint.action_index` 已经对齐，
  这是把线上建议挂到决策点上的关键，不要改语义。
- **建议按决策点记录，不按街记录**：一条街可能有 0 个、1 个或多个 hero
  决策点；没有轮到我时不会产生建议。手牌库展示「本街无建议」是正常状态，
  不要为了凑「每街一条」伪造。

### 2.2 outbox 与入库

- `poker_ocr/hand_outbox.py`：一手一个文件、原子写、成功才移到 `sent/`，
  失败按次数进 `failed/`。设计是对的，问题只在「谁来搬」。
- `tools/sync_hand_outbox.py`：手动/脚本调用；优先 `POST /api/hands/ingest`，
  404 时回退 `/api/hands/import`。
- 当前本地 `data/hand_outbox/pending/` 约 297 手、平均约 11KB/手；
  这是观察值，不是长期容量结论，但说明一手一个 JSON 完全可行。
- `replay-console` 的 ingest 已经会：
  - 按 `external_uid` 幂等（`replay_imported_hands` 唯一键）；
  - 写 `replay_hand_players`（座位/玩家）；
  - 写 `replay_live_advices`（线上建议 + context + 实际动作/bucket）；
  - 写 `replay_decisions`（从手牌重建的决策点，含 `snapshot_json`）。

### 2.3 复盘 UI

- `replay-console/replay_web/src/pages/HandDetailPage.tsx`：
  - 顶部手牌摘要；
  - 行动街 chips；
  - 每个决策点一张 `DecisionCard`，展示实际动作、N 个版本的建议、reason、bucket；
  - 底部「导入时的 AI 建议」只显示 option/reason/context，**没有 prompt**。
- 没有瀑布流页面，也没有牌桌布局/逐帧播放器。
- `replay_results` 表已经有 `prompt_system` / `prompt_user` / `raw_response`，
  但只对「重跑结果」有；线上真实建议没有。

### 2.4 A/B 现状

- 已有 `replay_experiments` + `replay_experiment_hands`：
  - 冻结 `hand_id + fingerprint`；
  - 按手牌 hash 切 dev/holdout；
  - 一个 experiment 对应一个 `replay_job`，job 里跑 baseline/variant 两个版本；
  - 结果聚合 `n / bucket / source / violations / agreement`；
  - 决策点可写 `replay_decision_marks`（good/bad/unsure）。
- 缺：
  - 手牌集对象（现在只能用 filter 或临时 handIds）；
  - 成对偏好（A 更好/B 更好/一样/不确定）；
  - P0 断言（哪些手牌是「必须不能变」的回归守卫）；
  - prompt diff（两个版本到底改了什么）；
  - baseline 的「旧 prompt」来源。**这是最关键的缺口，见 8.8。**

### 2.5 差距清单与优先级

| # | 能力 | 现状 | 差距 | 优先级 |
|---|---|---|---|---|
| 1 | 线上 prompt 落库 | 无 | 在 `advise()` 捕获 system/user/raw/reason_full | P0 |
| 2 | 自动入库 | outbox + 手动 sync | 常驻 `HandIngestWorker` + 自动补发 | P0 |
| 3 | 手牌时间线 | 详情页按街渲染 | 统一 `ReplayTimeline` + 两种视图 | P1 |
| 4 | 瀑布流 | 无 | 逐事件文字流 + 原地 prompt 展开 | P1 |
| 5 | 牌桌回放 | 无 | 座位/底池/下注/下一帧 + AI 气泡 | P1 |
| 6 | 手牌集 | experiment 内临时冻结 | `replay_hand_sets` + revision | P2 |
| 7 | 成对 A/B | 只有 agreement | pair review + P0 断言 + prompt diff | P2 |
| 8 | 深挖筛选 | 收藏/质量/来源 | flag 位图 + review score | P2 |
| 9 | 数据质量门槛 | 重建质量 exact/approx/invalid | 统一 hand/frame 质量 + A/B 排除规则 | P1 |
| 10 | 崩溃恢复 | 退出写 partial，不进 outbox | 当前手 journal + partial 入库 | P2 |

---

## 3. 核心模型与不变量

### 3.1 六个概念

| 概念 | 说明 | 关键字段 |
|---|---|---|
| `HandRecord` | 一手牌的完整事实 | `uid`、`table_roster`、`start_stacks`、`streets`、`shows`、`result`、`data_quality` |
| `DecisionPoint` | hero 的一个决策时刻 | `hand_uid`、`street`、`action_index`、`snapshot`、`actual` |
| `AdviceSnapshot` | 一次模型调用/一次兜底观测 | `decision_key`、`prompt_system/user`、`raw_response`、`option`、`reason_full`、`context` |
| `ReplayTimeline` | 由事实派生的事件序列 | `frames[]`、`frame_seq`、`decision_id`、`advice_id`、`derived` |
| `HandSet` | 人工挑选的评测集 | `set_id`、`revision`、`items[]`、`role`、`expect` |
| `Experiment` | 一次冻结对照 | `set_id/revision`、`baseline/variant`、`job_id`、`metrics`、`decision` |

### 3.2 不变量

1. **一手牌一个 `uid`**：`HandRecord.uid` 是事实主键；重复 ingest 幂等。
2. **一个决策点一个 `decision_key`**：`(hand_uid, street, action_index)`；
   `replay_live_advices` 的唯一键必须与它一致。
3. **prompt 要么精确，要么显式缺失**：`prompt_sha256` 为空的记录必须带
   `prompt_missing=1` 和原因；不允许用「大致一样」的 prompt 冒充。
4. **A/B 的 context 必须冻结**：同一实验内，baseline/variant 看到同一份
   `table_stats` / `opponent_reads` / `table_dynamics` 快照；否则比的是上下文，
   不是改动。
5. **手牌集冻结后不漂移**：实验创建时复制 `hand_id + fingerprint` 到
   `replay_experiment_hands`；之后选手牌集变化不影响旧实验。
6. **actual ≠ ground truth**：任何指标都不能把「与人类一致」直接命名为正确率。
7. **缺失/推算必须标记**：`derived`、`approx`、`inferred`、`missing` 是落库
   契约的一部分，UI 必须显示。
8. **不修改生产事实**：重跑/实验只写 `replay_*`，不写 `review_hands`；这是现有
   控制台的红线，继续保持。

### 3.3 身份与版本

```text
hand_uid       = 20261101-bfdd2c4dd3          # 现有 hand_id
decision_key   = (hand_uid, "turn", 14)       # 现有 action_index
advice_uid     = (hand_uid, "turn", 14)       # 一个决策点一条线上建议
prompt_sha256  = sha256(system + "\n---\n" + user)
record_schema  = "hand.v3"                    # 新增字段后 bump
timeline_ver   = "timeline-1"
```

版本仍然是「模型 + 接口 + 采样参数 + prompt_version + advice_version +
reconstruct_version + 桌风快照」的 `config_hash`。新增：

- `replay_versions.channel`：`prod` / `candidate` / `archive`；
- `replay_results.prompt_sha256`：结果级 prompt 指纹，用于 diff 和缓存；
- 每个决策点的 `prompt_sha256` 是最终事实，版本级的 `prompt_version` 只做索引。

### 3.4 数据流

```text
┌──────────────────────────── live ────────────────────────────┐
│ FrameState → LiveHandTracker → HandSnapshot                  │
│                                   │                          │
│                                   ▼                          │
│                            AdviceSnapshot                     │
│                    (prompt/raw/reason/context)                │
│                                   │                          │
│ 换手/退出 → HandRecord v3 ────────┘                          │
│                    │                                          │
│                    ├── logs/live.states.jsonl                 │
│                    └── data/hand_outbox/pending/<uid>.json    │
└────────────────────┬─────────────────────────────────────────┘
                     │ HandIngestWorker（自动、可重试）
                     ▼
┌──────────────── replay-console / MySQL ──────────────────────┐
│ POST /api/hands/ingest                                       │
│   ├── replay_imported_hands (payload + timeline + flags)     │
│   ├── replay_hand_players                                    │
│   ├── replay_decisions (snapshot_json)                       │
│   ├── replay_live_advices (+ prompt_snapshot)                │
│   └── flags / timeline                                       │
│                                                              │
│ 手牌库 → 手牌集 → 实验 → 成对结果 → 人工复盘                  │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. 数据模型

### 4.1 总览

| 表 | 作用 | 本次改动 |
|---|---|---|
| `replay_imported_hands` | 手牌全量 payload + 列表索引 | 加 `timeline_json`、`timeline_version`、`flag_mask`、`flags_json`、`review_score`、`record_schema` |
| `replay_hand_players` | 每手座位/玩家 | 不改结构；后续补 `player_key` 归一 |
| `replay_decisions` | hero 决策点 + `snapshot_json` | 不改；补查询索引即可 |
| `replay_live_advices` | 线上真实建议 | **加 prompt/raw/reason_full/facts/model_params/prompt_sha** |
| `replay_results` | 重跑结果 | 加 `prompt_sha256`、`reason_full` |
| `replay_hand_sets` | 手牌集 | 新增 |
| `replay_hand_set_items` | 手牌集条目 | 新增；含 `role`、`expect_json`、`stratum` |
| `replay_experiments` | 实验 | 加 `hand_set_id/revision`、`kind`、`change_scope`、`conclusion`、`decision`、`metrics_json` |
| `replay_pair_reviews` | 成对人工偏好 | 新增 |
| `replay_versions` | 版本 | 加 `channel`、`promoted_at` |
| `replay_marks` / `replay_decision_marks` | 手牌/决策点收藏与结论 | 保留；语义不变 |

### 4.2 `AdviceSnapshot`：在源头捕获

在 `poker_ocr/advice.py` 的 `Advice` 上增加可选字段（全部有默认值，旧 JSON
消费者不受影响）：

```python
@dataclass(frozen=True)
class Advice:
    # ... 现有字段 ...
    prompt_system: str | None = None
    prompt_user: str | None = None
    prompt_sha256: str | None = None
    raw_response: str | None = None
    response_sha256: str | None = None
    reason_full: str | None = None       # 未截断的模型理由
    facts: dict = field(default_factory=dict)       # 事实层快照
    model_params: dict = field(default_factory=dict) # 不含 api_key
    captured_at: str | None = None
    prompt_missing: bool = False
    prompt_missing_reason: str | None = None
    attempt: int = 1
```

捕获点：

```text
advise(sit)
  ├─ settings 为空 / 0 对手异常 → fallback，不构建 prompt
  │    → prompt_missing=True, reason="not_built"
  ├─ build_messages(sit) → system, user
  │    → 先算 prompt_sha256
  ├─ complete(...) 包装：
  │    → captured["raw"] = json.dumps(data)
  │    → captured["system/user"] = system/user
  └─ 成功/失败都生成 Advice
       → prompt_system/user 一定有（只要真的调用了模型）
       → raw_response 成功时有；失败时保留错误
```

注意：

- **不要在 `to_dict()` 里现场重算 prompt**。重算可能因为代码/画像/`.table_dy`
  变化而不一致；必须保存当时字符串。
- **不要只存 hash**。hash 只能证明「同不同」，不能回答「模型当时看到了什么」。
- **不要只存 `prompt_version`**。版本号相同不代表正文相同（尤其是事实层）。
- `reason_full` 与 `reason` 并存：终端/旧 UI 继续用短 reason，复盘用全文。

JSON 形状（向后兼容，只在新字段非空时写入）：

```jsonc
{
  "hand_id": "20261101-bfdd2c4dd3",
  "street": "turn",
  "action_index": 14,
  "option": {"id": "call", "kind": "call", "label": "跟注", "amount": 12.0},
  "reason": "赔率够，跟注",
  "reason_full": "对手转牌下注 12BB，底池 38BB，需要跟 12BB，赔率 24%；范围权益 31%，且有位置，跟注优于弃牌。",
  "source": "model",
  "model": "deepseek-flash",
  "prompt_version": "lean-zh-2026-11-01.4",
  "prompt_sha256": "9f2c...",
  "prompt_system": "你是德州扑克助手……",
  "prompt_user": "## 当前局面……",
  "raw_response": "{\"option\":\"call\",\"reason\":\"...\",\"confidence\":0.71}",
  "facts": {"handTier": "medium", "equityPct": 31.0, "potOddsPct": 24.0},
  "model_params": {"temperature": 0.2, "thinking": "enabled", "max_tokens": null},
  "context": {"table_stats": {}, "opponent_reads": []},
  "duration_ms": 1830,
  "prompt_tokens": 3214,
  "completion_tokens": 86,
  "prompt_missing": false
}
```

### 4.3 迁移 `008_live_prompt_and_flags.sql`

```sql
-- 线上建议：保存真正发给模型的 prompt 与原始响应
ALTER TABLE replay_live_advices
  ADD COLUMN prompt_system MEDIUMTEXT NULL COMMENT 'system prompt 原文',
  ADD COLUMN prompt_user MEDIUMTEXT NULL COMMENT 'user prompt 原文',
  ADD COLUMN prompt_sha256 CHAR(64) NULL COMMENT 'sha256(system + sep + user)',
  ADD COLUMN raw_response MEDIUMTEXT NULL COMMENT '模型原始 JSON/文本',
  ADD COLUMN response_sha256 CHAR(64) NULL,
  ADD COLUMN reason_full TEXT NULL COMMENT '未截断理由',
  ADD COLUMN facts_json JSON NULL COMMENT '事实层快照',
  ADD COLUMN model_params_json JSON NULL COMMENT 'temperature/top_p/thinking 等，不含 key',
  ADD COLUMN prompt_missing TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN prompt_missing_reason VARCHAR(64) NULL,
  ADD COLUMN captured_at DATETIME(3) NULL,
  ADD COLUMN attempt INT NOT NULL DEFAULT 1,
  ADD KEY idx_la_prompt_sha (prompt_sha256),
  ADD KEY idx_la_prompt_missing (prompt_missing);

-- 手牌：物化时间线与筛选位图
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

-- 重跑结果：可直接做 prompt diff
ALTER TABLE replay_results
  ADD COLUMN prompt_sha256 CHAR(64) NULL,
  ADD COLUMN reason_full TEXT NULL,
  ADD KEY idx_res_prompt_sha (prompt_sha256);
```

> 说明：`replay_live_advices` 现有唯一键 `(hand_id, street, action_index)` 保留。
> 同一决策点如果发生多次调用，`attempt` 记录次数，正文保留最后一次；如果以后
> 需要逐次审计，再拆 `replay_advice_attempts`，本期不做。

**避免 prompt 双份存储**：

- outbox 的 `sent/` 文件保留完整 `advices`（含 prompt/raw），作为冷备；
- ingest 时把 prompt/raw **提升**到 `replay_live_advices`；
- `replay_imported_hands.payload_json` 里的 advice 可去掉 `prompt_system` /
  `prompt_user` / `raw_response`，只留 `prompt_sha256`，避免 MySQL 里同一份
  prompt 存两遍；
- `GET /api/hands/{id}` 改为优先读 `replay_live_advices`，旧手牌再回退 payload；
- 如果以后要「一手牌一个 JSON 带走全部内容」，用导出工具从两张表拼，
  而不是在库里重复存。

### 4.4 迁移 `009_hand_sets.sql`

```sql
CREATE TABLE IF NOT EXISTS replay_hand_sets (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     BIGINT UNSIGNED NOT NULL DEFAULT 0,
  name        VARCHAR(100) NOT NULL,
  description TEXT NULL,
  purpose     VARCHAR(32) NOT NULL DEFAULT 'regression'
              COMMENT 'regression/exploration/golden/coverage',
  revision    INT NOT NULL DEFAULT 1 COMMENT '每次增删 item +1',
  frozen_at   DATETIME(3) NULL COMMENT '最近一次冻结时间',
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
              ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_set_user_name (user_id, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='人工维护的 A/B 手牌集';

CREATE TABLE IF NOT EXISTS replay_hand_set_items (
  set_id       BIGINT UNSIGNED NOT NULL,
  hand_id      BIGINT UNSIGNED NOT NULL,
  fingerprint  CHAR(64) NOT NULL COMMENT '加入时的内容指纹',
  role         VARCHAR(16) NOT NULL DEFAULT 'regression'
               COMMENT 'golden/regression/exploration/negative',
  priority     TINYINT NOT NULL DEFAULT 1 COMMENT '0=P0 必须通过',
  stratum      VARCHAR(64) NULL COMMENT 'street/position/pot_type/spot',
  expect_json  JSON NULL COMMENT 'P0 断言，见 8.5',
  note         VARCHAR(255) NULL,
  added_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (set_id, hand_id),
  KEY idx_hsi_hand (hand_id),
  KEY idx_hsi_role (set_id, role, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='手牌集条目；实验创建时复制到 experiment_hands';
```

`replay_experiments` 扩展：

```sql
ALTER TABLE replay_experiments
  ADD COLUMN hand_set_id BIGINT UNSIGNED NULL,
  ADD COLUMN hand_set_revision INT NULL,
  ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'ab'
      COMMENT 'ab/regression/smoke',
  ADD COLUMN change_scope VARCHAR(32) NULL
      COMMENT 'prompt/facts/model/context/code/mixed',
  ADD COLUMN baseline_prompt_mode VARCHAR(24) NOT NULL DEFAULT 'stored'
      COMMENT 'stored/rerender/external',
  ADD COLUMN metrics_json JSON NULL,
  ADD COLUMN conclusion TEXT NULL,
  ADD COLUMN decision VARCHAR(16) NULL COMMENT 'adopt/reject/iterate/inconclusive',
  ADD COLUMN promoted_version_id BIGINT UNSIGNED NULL,
  ADD KEY idx_exp_set (hand_set_id, hand_set_revision);
```

### 4.5 迁移 `010_pair_reviews.sql` / `011_version_channel.sql`

```sql
CREATE TABLE IF NOT EXISTS replay_pair_reviews (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id       BIGINT UNSIGNED NOT NULL DEFAULT 0,
  experiment_id BIGINT UNSIGNED NOT NULL,
  decision_id   BIGINT UNSIGNED NOT NULL,
  preferred     VARCHAR(8) NOT NULL COMMENT 'A/B/same/unsure',
  tags          JSON NULL,
  note          TEXT NULL,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_pr_user_exp_dec (user_id, experiment_id, decision_id),
  KEY idx_pr_exp (experiment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='成对人工偏好：A/B/same/unsure';
```

> 与 `replay_decision_marks` 的区别：
> `replay_decision_marks` 回答「这个决策本身好不好」（good/bad/unsure）；
> `replay_pair_reviews` 回答「这次改动让 A 更好还是 B 更好」。
> 两者不能合并，否则 A/B 的胜负会被「绝对好坏」污染。

版本通道：

```sql
ALTER TABLE replay_versions
  ADD COLUMN channel VARCHAR(16) NOT NULL DEFAULT 'candidate'
      COMMENT 'prod/candidate/archive',
  ADD COLUMN promoted_at DATETIME(3) NULL,
  ADD KEY idx_ver_channel (user_id, channel);
```

约定：

- `prod` 是最多一个；adopt 时把旧 `prod` 改成 `archive`，新版本改 `prod`；
- `candidate` 可以多个，实验 variant 默认取最新；
- 归档版本仍可读历史结果，但不能被新实验选为 baseline/variant；
- 迁移后手动把「当前线上在用的版本」标为 `prod`，否则第一个实验没有默认
  baseline，UI 会要求显式选择。

### 4.6 `ReplayTimeline` 契约

```jsonc
{
  "version": "timeline-1",
  "handUid": "20261101-bfdd2c4dd3",
  "recordSchema": "hand.v3",
  "quality": "exact",              // exact/approx/partial/invalid
  "frames": [
    {
      "seq": 0,
      "type": "hand_start",
      "street": "preflop",
      "ts": "2026-11-01T21:03:11+08:00",
      "table": {"size": 9, "blinds": {"sb": 0.5, "bb": 1.0, "ante": 0.5}},
      "hero": {"seatId": "hero", "cards": ["Ks", "Td"], "stack": 182.0},
      "roster": [{"seatId": "hero", "name": "svsvbmbm", "position": "BB", "stack": 182.0}]
    },
    {
      "seq": 5,
      "type": "action",
      "street": "flop",
      "actor": {"seatId": "upR", "name": "兄弟你吃完", "position": "CO"},
      "action": "bet",
      "amount": 12.5,
      "amountMode": "street_total",
      "potAfter": 27.5,
      "stacksAfter": {"hero": 169.5, "upR": 124.0},
      "betsAfter": {"upR": 12.5},
      "derived": true,
      "warnings": ["由起始筹码和行动额推算"]
    },
    {
      "seq": 6,
      "type": "decision",
      "street": "flop",
      "decisionId": 123456,
      "adviceId": 98765,
      "decisionKey": ["20261101-bfdd2c4dd3", "flop", 5],
      "snapshot": {"pot": 27.5, "toCall": 12.5, "heroStack": 169.5},
      "advice": {"optionId": "call", "kind": "call", "reason": "赔率够", "source": "model"},
      "promptSha256": "9f2c...",
      "promptMissing": false
    },
    {
      "seq": 7,
      "type": "action",
      "street": "flop",
      "actor": {"seatId": "hero", "position": "LJ"},
      "action": "call",
      "amount": 12.5,
      "potAfter": 40.0,
      "derived": true
    },
    {
      "seq": 20,
      "type": "hand_end",
      "result": {"heroNetBb": -12.5, "outcome": "lose", "source": "stack_delta"}
    }
  ]
}
```

生成规则：

1. 以 `streets[].actions[]` 为主序列；每条 action 一个 frame。
2. 每个 `DecisionPoint` 按 `action_index` 插到对应 action 之前，生成 `decision` frame。
3. 街首插 `street_start`，带 `board`、`potStart`；街末可插 `street_end`。
4. 有 `shows` 插 `showdown`；有 `all_ins` 标在对应 action 上。
5. 金额：action 的 `amount` 是「本街加到总额」；`delta = amount - 该座本街已投入`。
6. 底池：优先用 `street.pot_start`；之后 `pot += delta`。缺 `pot_start` 时用上一街
   末底池，标 `derived`。
7. 筹码：优先用 `DecisionPoint.snapshot`（决策帧）；动作帧用
   `start_stacks + 盲注/前注 + delta` 推算。任何缺失/对账失败 → `derived=true`
   并写 `warnings`，不猜。
8. `quality`：所有关键帧都有精确来源 → `exact`；有推算 → `approx`；
   有丢动作/缺底牌 → `partial/invalid`。

> 关键取舍：**不为了牌桌好看而伪造精确筹码**。牌桌回放允许右上角一直显示
> 「推算」徽标；比一个漂亮但错误的数字更有用。

### 4.7 flag 位图

`flag_mask` 用 `BIGINT`，每位一个信号；`flags_json` 存细节/原因。

| bit | 名称 | 触发条件 | 用途 |
|---:|---|---|---|
| 0 | `AI_DIFF` | 任一决策点 `bucket != 一致` | 默认「值得看」 |
| 1 | `AI_FALLBACK` | 任一建议 `source != model` | 模型/配置问题 |
| 2 | `LOW_CONF` | `confidence < 0.5` | 模型不确定 |
| 3 | `DATA_WARN` | 任一 `cards_warnings` / `warnings` / `derived` | 数据可疑 |
| 4 | `BIG_POT` | 最大底池 ≥ 50BB 或最小 SPR ≤ 2 | 高价值决策 |
| 5 | `RIVER_BIG` | 河牌底池 ≥ 30BB 且有 bet/raise/all-in | 高杠杆 |
| 6 | `UNKNOWN_OPP` | 无画像对手 ≥ 2 或覆盖率 < 50% | 信息不足 |
| 7 | `AB_FLIP` | 最近一次实验 A/B 动作不同 | 改动敏感点 |
| 8 | `REVIEW_BAD` | 人工标 bad | 已知问题 |
| 9 | `MANUAL_PICK` | 被加入手牌集 | 重点 |
| 10 | `PROMPT_MISSING` | 任一决策 prompt_missing | 不能做 prompt A/B |
| 11 | `PARTIAL` | `partial=1` | 排除统计 |
| 12 | `INVALID` | 重建 invalid | 排除 A/B |
| 13 | `SHOWDOWN` | 有摊牌 | 结果可验证 |
| 14 | `MULTIWAY` | 在局 ≥ 4 人 | 多人池覆盖 |
| 15 | `BOMB_POT` | `bomb_pot=1` | 特殊结构 |

`review_score`（0–100，仅排序用，不是 EV）：

```text
score = min(100,
    30 * AI_DIFF
  + 20 * AI_FALLBACK
  + 15 * DATA_WARN
  + 15 * BIG_POT
  + 10 * RIVER_BIG
  + 10 * UNKNOWN_OPP
  + 25 * AB_FLIP
  + 20 * REVIEW_BAD
  + 10 * LOW_CONF
)
```

UI 必须显示「为什么是这个分」，不能只给一个黑盒数字。

### 4.8 容量与保留

- 一手牌 payload 现在约 11KB；加 prompt/raw 后按 1.5 个决策点、每个
  8–20KB 估算，约 25–45KB/手。
- 200–500 手/天 → 5–22MB/天；一年约 2–8GB（含索引/备份会更多）。
- MySQL `MEDIUMTEXT` 足够；列表查询**不要** `SELECT prompt_*`，详情/Inspector
  再懒加载。
- 建议保留策略：
  - `replay_live_advices` 的 prompt/raw 全文保留 180 天；
  - 之后压缩归档到 `data/archive/prompts/YYYYMM.jsonl.gz`，DB 只留
    `prompt_sha256`、option、reason、token；
  - 哈希和结果永久保留，保证历史实验可解释。
- outbox `sent/` 每周归档/清理；`pending/` 不能自动删，除非已 ack。

### 4.9 为什么不再引入本地 SQLite

`docs/桌子动态重构与手牌库打通-方案.md` 曾建议 `data/poker.db`（SQLite 事实库 +
JSON 缓存）。现在 `replay-console` 的 MySQL 已经是手牌库，且 ingest/实验/UI 都在
上面。**本期不建议再引入 SQLite 作为第二事实源**：

- 会带来「MySQL 和 SQLite 谁是准」的同步问题；
- outbox 已经提供离线耐久队列；
- as-of 画像查询如果将来需要，可以在 MySQL 里做，不必先加一套库。

结论：**MySQL 是手牌库的 system of record；outbox 是本地耐久队列；不新增 SQLite。**

---

## 5. 实时沉淀：从一手牌到可复盘记录

### 5.1 端到端时序

```text
[帧循环]
  LiveHandTracker.observe(state)
    ├─ 记录 action / street / board / roster
    └─ 轮到我 → HandSnapshot
          └─ AdviceRunner.submit(snapshot)

[后台线程]
  advise(sit)
    ├─ build_messages → system,user
    ├─ 捕获 prompt / raw / facts / params
    └─ Advice 回到主循环

[主循环 drain_advice]
  ├─ _hand_advice[hand_id].append(advice.to_dict())
  ├─ logs/live.states.jsonl 写 advice 行
  └─ 终端打印建议（可选打印 prompt 摘要）

[换手/退出]
  take_finished()
    ├─ 补街末对账 / 推断弃牌 / 结果
    ├─ _write_hand(hand)
    │    ├─ record = HandRecord v3 + advices + quality
    │    ├─ JSONL 写 hand 行
    │    └─ outbox.enqueue(record)
    └─ HandIngestWorker.kick()
```

### 5.2 捕获 prompt 的代码落点

建议在 `poker_ocr/advice.py` 内完成，不依赖 live：

```python
def advise(sit, settings, timeout=20.0, complete=None, capture=True):
    ...
    system, user = build_messages(sit)
    prompt_sha = sha256_text(system + "\n---\n" + user) if capture else None
    raw_holder = {}

    def ask(prompt):
        data = caller(settings, system, prompt, timeout=timeout)
        if capture:
            raw_holder["raw"] = json.dumps(data, ensure_ascii=False)
        return data

    try:
        data = ask(user)
        ...
        return make(..., prompt_system=system, prompt_user=user,
                    prompt_sha256=prompt_sha, raw_response=raw_holder.get("raw"),
                    reason_full=raw_reason, facts=facts_snapshot(sit),
                    model_params=safe_params(settings))
    except AIError as exc:
        return make(..., prompt_system=system, prompt_user=user,
                    prompt_sha256=prompt_sha, raw_response=raw_holder.get("raw"),
                    prompt_missing=False, error=str(exc))
```

`facts_snapshot(sit)` 是 advice 侧的轻量快照，口径与 replay 的
`decision_facts(sit)` 对齐；不要为了存 facts 从 live 反向 import replay 模块。

注意：

- `settings is None` 或 `abnormal_no_opponents` 的 fallback 不构建 prompt；
  写 `prompt_missing=True, reason="not_built"`。
- `AIError` 时 prompt 已经发出去过，必须保留；这是排查「模型为什么答错」的证据。
- `raw_response` 可能包含供应商的 `reasoning_details`/用量字段，保留原文；
  UI 只展示可读部分。

### 5.3 手牌结束：`HandRecord v3`

在现有 `hand.v2` 上增量：

```jsonc
{
  "schema": "hand.v3",
  "uid": "20261101-bfdd2c4dd3",
  "hand_id": "20261101-bfdd2c4dd3",
  "table_signature": "20261101-a1b2c3",
  "started_at": "2026-11-01T21:03:11+08:00",
  "ended_at": "2026-11-01T21:04:02+08:00",
  "table_roster": [ ... ],
  "hero_cards": ["Ks", "Td"],
  "hero_position": "LJ",
  "hero_stack_bb": 182.0,
  "start_stacks": [ ... ],
  "board": ["3d", "Ac", "2c", "Kh", "9s"],
  "streets": [ ... ],
  "shows": [ ... ],
  "all_ins": [ ... ],
  "result": {
    "hero_net_bb": -12.5,
    "hero_start_stack": 182.0,
    "hero_end_stack": 169.5,
    "outcome": "lose",
    "source": "stack_delta"
  },
  "data_quality": {
    "status": "exact",                  // exact/approx/partial/invalid
    "warnings": [],
    "inferred_actions": 0,
    "missing_fields": [],
    "cards_unreliable": false
  },
  "advices": [ { ...AdviceSnapshot... } ]
}
```

规则：

- `hero_net_bb` 只在 `hero_start_stack` 和结束时后手都可读时计算；
  否则 `source="unknown"`，不猜。
- `outcome`：hero 弃牌优先写 `fold`；否则有 `hero_net_bb` 时按正负写
  `win/lose`；两者都没有则 `unknown`。不要用「谁秀了牌」推断输赢。
- `partial` 手牌也落库（标 `partial=1`），但：
  - 不计入画像统计；
  - 默认不进 A/B；
  - UI 显示「未完成，仅供参考」。
- 不在手牌里写 `timeline_json`；timeline 由 ingest 端生成，避免 live 变重。

### 5.4 崩溃恢复

现状：退出时 `take_current()` 会写一条 partial 到手牌 JSONL，但不进 outbox；
进程崩溃则当前手完全丢失。

建议：

1. **当前手 journal**：每 N 秒（默认 5s）把 `_LiveHand` 的轻量快照写到
   `data/live_current/<session>.json`；这不是事实，只用于恢复。
2. **启动恢复**：发现 journal 且对应 uid 没有完整记录 → 生成
   `partial` HandRecord，写 outbox，标 `recovered_from_journal`。
3. **退出**：`take_current()` 的 partial 也 enqueue（`partial=1`），
   不再只写 JSONL。
4. **不把 partial 混进统计**：`apply_observations` / profile 更新只看完整手。

### 5.5 自动入库：`HandIngestWorker`

#### 5.5.1 形态

推荐 **live 进程内嵌线程**，而不是让 live 直接写 MySQL：

```text
live 主循环
  ├─ outbox.enqueue(record)      # 先落本地，永不阻塞
  └─ ingest_worker.kick()        # 唤醒后台线程

HandIngestWorker（daemon thread）
  while not stop:
    paths = outbox.pending()[:batch]
    for path in paths:
      record = load(path)
      POST /api/hands/ingest {"source":"live","records":[record]}
      if response.errors: 只把出错的那手移到 failed/ 或留 pending
      else: outbox.mark_sent(path)
    sleep(interval)
```

为什么不是 live 直接 POST：

- 手牌库（MySQL/replay-console）可能没启动；live 不能因此卡住或报错。
- 一次 POST 可能要 100ms–10s；不能放在 OCR 主循环。
- outbox 已经是一手一个文件，天然适合断点/重试。

#### 5.5.2 配置

```bash
HAND_INGEST_MODE=auto          # off | auto | direct(调试)
HAND_INGEST_URL=http://127.0.0.1:8010
HAND_INGEST_TOKEN=...          # 可选，复用 REPLAY_TOKEN
HAND_INGEST_INTERVAL=2.0       # 秒
HAND_INGEST_BATCH=20
HAND_INGEST_TIMEOUT=20
HAND_INGEST_MAX_FAILURES=8
HAND_INGEST_BACKOFF=2,5,15,60,300
```

- `auto`：默认。outbox 必写；线程尽力发送；失败退避；不影响 live。
- `off`：只写 outbox，回到现在的手动 sync。
- `direct`：调试用，同步发送一次；失败仍写 outbox。不要作为生产默认。

#### 5.5.3 重试与错误分类

| 情况 | 处理 |
|---|---|
| 连接失败 / 5xx / 超时 | 留 pending，指数退避，下次重试 |
| 401/403 | 停止发送并告警（token 错），不要把所有手牌打成 failed |
| 404/405 | 回退 `/api/hands/import`；仍失败则留 pending |
| 422/400（单条坏数据） | 该手进 `failed/` 并写 `.error`；其余继续 |
| 200 但 `errors` 非空 | 按返回的 index 精确隔离坏手，其余 mark_sent |
| 重复 (`duplicates`) | 视为成功，mark_sent |

#### 5.5.4 健康与可观测

- live 终端每批打印一行：
  `[手牌库] 已入库 3 手（队列 0）` 或 `[手牌库] 离线，队列 12，2 秒后重试`。
- `GET /api/health` 增加 `ingest` 区块：pending/sent/failed、最后成功时间、
  最后错误。
- Prometheus/日志计数（可选）：`hand_ingest_total{result="ok|retry|fail"}`。
- `tools/sync_hand_outbox.py` 保留，用于回填/人工重试；内部复用同一发送逻辑，
  避免两套行为。

#### 5.5.5 「无需手动导入」的边界

自动入库的前提是 **replay-console 在运行**。如果没开：

- 手牌仍在 `pending/`，不会丢；
- 下次启动 live 或手动跑 sync，会自动补；
- UI 不会出现这手牌，直到补发成功。

这不是「不需要数据库」，而是「不需要人点导入」。文档和 UI 都要写清楚。

### 5.6 「完整记录」检查清单

一手牌入库后，以下字段必须能回答 yes/no：

- [ ] 时间、盲注、前注、桌型、桌号/桌签名
- [ ] 在座名单（`table_roster`）与本手在局名单（`street_active`）
- [ ] hero 底牌、位置、起始后手
- [ ] 公共牌逐街
- [ ] 每个动作的 actor / action / amount / all-in / inferred
- [ ] 每个 hero 决策点的 pot / to_call / hero_stack / opponents
- [ ] 每个建议的 prompt / raw / option / reason / context / model / tokens / latency
- [ ] 摊牌/秀牌（如果有）
- [ ] 结果/hero_net（能算就算，不能算标 unknown）
- [ ] 数据质量（exact/approx/partial/invalid + warnings）
- [ ] 归属（session/hand_uid/source/schema）

---

## 6. 手牌库：筛选、深挖、手牌集

### 6.1 列表新增列

在现有列表上增加（可配置显示）：

| 列 | 说明 |
|---|---|
| 建议 | 线上建议条数 / prompt 覆盖（如 `3/3`） |
| 分歧 | AI 与实际行动的 bucket（一致/尺度/动作/—） |
| 质量 | exact/approx/partial/invalid + 警告数 |
| 深挖分 | `review_score`，hover 显示原因 |
| 标记 | A/B 翻转、兜底、数据异常、大底池、多人池等 |
| 评测集 | 属于哪些 set（最多显示 2 个） |
| 复盘 | 未复盘/已确认/有争议 |

### 6.2 筛选

保留现有 `favorite/source/position/result/tag/hasRun/quality/keyword/date`，新增：

```text
flags=AI_DIFF,AB_FLIP,DATA_WARN        # 任选，OR/AND 可切换
minScore=60
promptCoverage=complete|missing|partial
setId=3
street=turn,river
potType=multiway|bomb
review=unreviewed|good|bad|unsure
```

实现：`flag_mask & mask = mask` 做 AND；`flag_mask != 0` 做 OR；`minScore`
直接走已物化的 `review_score` 列（ingest 与实验结束后更新），避免每次列表
查询都重算。

### 6.3 「值得深挖」的默认视图

进入手牌库默认不是「全部」，而是三个快捷入口：

1. **今天的新手牌**：按时间倒序，未复盘优先。
2. **值得看**：`review_score >= 50`，按分数倒序。
3. **A/B 翻转**：最近一次实验里 A/B 动作不同的决策点所在手牌。

每个入口都显示数量；避免用户每天面对 300 手不知道从哪看。

实验完成后要回写 `AB_FLIP`：对实验中 A/B 动作/金额不同的决策点，把对应手牌的
`flag_mask` 置 `1 << 7`，并更新 `review_score`。这样「A/B 翻转」入口永远是
最近一次实验的结果，而不是一次性计算。

### 6.4 自动推荐加入评测集

在手牌集详情页提供「补齐覆盖」：

- 按街：缺河牌决策 → 推荐有河牌决策的手牌；
- 按位置：缺 SB/BB 防守 → 推荐对应位置；
- 按底池类型：缺多人池/炸弹池 → 推荐；
- 按结果：缺摊牌/输赢 → 推荐；
- 按质量：只推荐 exact 且 prompt 完整。

推荐是**建议**，不自动加入；用户确认后才改 set revision。

### 6.5 手牌集页面

```text
┌ 我的回归集 v3 ─────────────────────────────────────────────┐
│ 42 手 · 87 个决策点 · 河牌 18 · 多人池 21 · 炸弹池 6       │
│ 质量：exact 39 / approx 3 · prompt 覆盖 87/87              │
│ [从手牌库添加] [补齐覆盖] [冻结 v4] [跑回归]               │
├────────────────────────────────────────────────────────────┤
│ 角色    手牌/决策      位置/街        预期          操作    │
│ golden  20261030-...   BB/河牌       必须 call     ...     │
│ reg     20261101-...   LJ/转牌       必须不 fold   ...     │
│ neg     20261031-...   CO/翻牌       保持 check    ...     │
└────────────────────────────────────────────────────────────┘
```

- `golden`：已知正确，改动不能破坏；
- `regression`：曾经修过的 bug，必须保持修复；
- `exploration`：新场景，允许变化；
- `negative`：不该变化的对照，防止「全都翻一遍」看起来很好。

### 6.6 手牌详情页信息架构

保留现有「收藏与结论 / 行动街 / 决策卡」，新增顶部模式切换：

```text
[总览] [瀑布流] [牌桌回放]
```

- 总览：现有页面 + 线上建议卡片（可展开 prompt）。
- 瀑布流：全屏/半屏的逐事件流。
- 牌桌回放：全屏牌桌 + 播放控件。
- 任意决策点可「在瀑布流中定位」「在牌桌中定位」「对比 A/B」「复制 prompt」。

---

## 7. 双模式复盘

### 7.1 共同契约

两种模式都消费同一个 `GET /api/hands/{id}/replay` 返回的 `frames`：

- 帧类型：`hand_start` / `street_start` / `action` / `decision` / `showdown` / `hand_end`；
- 每个 action 帧最多对应一个 decision 帧（decision 在 hero 实际动作之前）；
- `seq` 单调递增；深链 `?frame=12`；
- 两种模式共享播放状态：`cursor`、`playing`、`speed`、`pauseOnDecision`；
- 切换模式不改变 cursor（尽量保持同一事件）。

### 7.2 模式 A：文字瀑布流

#### 布局

```text
┌ 左：街导航 ─┬─ 中：事件流（自动滚动） ──────────────┬ 右：Inspector ─┐
│ 翻前  12    │ [翻前] 盲注 1/2 ante 0.5            │ 当前帧详情     │
│ 翻牌   8    │  UTG fold                           │ Prompt         │
│ 转牌   6    │  UTG+1 call 1                       │ system/user    │
│ 河牌   4    │  Hero raise 3.5  ◀ D1 AI: raise-3.5 │ raw response   │
│             │    ┌ prompt 12.4KB · 3,214 tok ┐    │ facts          │
│ 播放 1x ▶   │    │ [展开] [复制] [对比]       │    │ context        │
│ 下一决策 ⏭  │    └────────────────────────────┘    │ diff           │
└─────────────┴──────────────────────────────────────┴────────────────┘
```

#### 行类型

| 行 | 内容 |
|---|---|
| `hand_start` | 手牌号、时间、盲注、桌型、hero 底牌/位置/后手 |
| `street_start` | 街名、公共牌、起始底池 |
| `action` | 谁、什么动作、金额、底池、是否 inferred |
| `decision` | Dn、建议 option、source、model、耗时、confidence；可展开 prompt |
| `showdown` | 秀牌、结果 |
| `hand_end` | hero_net、outcome、质量标记 |

#### 交互

- **自动滚动**：播放时事件逐条出现，当前行高亮；用户手动滚动时暂停跟随。
- **速度**：0.5× / 1× / 2× / 4×；`decision` 帧默认停顿（可关）。
- **思考时间**：可选「按原始耗时停顿」，默认最多 1s，避免回放被 3s 模型调用拖慢。
- **键盘**：`Space` 播放/暂停，`←/→` 上一/下一帧，`J/K` 上/下决策点，
  `P` 打开 prompt，`D` 切 diff。
- **过滤**：只看决策点 / 只看分歧 / 只看某街 / 隐藏对手动作。
- **原地 prompt**：默认折叠，显示大小/token/耗时；展开后不跳页，保持上下文。

#### 为什么先做瀑布流

- 实现成本低（一个列表 + 一个 Inspector）；
- 信息密度最高，适合逐街读事实；
- 可以作为牌桌模式的数据/交互试验场；
- 对移动端/小屏也友好。

### 7.3 模式 B：牌桌回放

#### 布局

```text
┌──────────────────────────────────────────────────────────────┐
│ 手牌 #... · KsTd · LJ · 9人桌 · 底池 45BB · [推算]          │
│                                                              │
│        topL(HJ) 113          topR(empty)                     │
│   upL(LJ) 109                                      upR(CO)   │
│                   [ 3d  Ac  2c ]                             │
│   midL(UTG+1) 209        POT 45BB              midR(BTN)     │
│                                                              │
│        lowL(UTG) 230        lowR(SB) 766                     │
│                   hero(BB) 182  Ks Td                        │
│                                                              │
│  ┌ AI 建议 ─────────────────────────────┐                    │
│  │ 跟注 12BB · 赔率 24% · 权益 31%      │                    │
│  │ 理由：……                    [Prompt] │                    │
│  └──────────────────────────────────────┘                    │
├──────────────────────────────────────────────────────────────┤
│ ◀◀  ◀  ▶/⏸  ▶▶   帧 6/32   [翻前|翻牌|转牌|河牌]   1x  □决策暂停 │
└──────────────────────────────────────────────────────────────┘
```

#### 座位映射

`live` 的 `seat_id` 是固定锚点：`hero/lowL/midL/upL/topL/topR/upR/midR/lowR`。
前端按这个锚点排九宫格；不在座/离桌的座位显示为空。6 人桌隐藏 `topL/topR`。

- hero 固定底部；
- 对手按 `seat_id` 排；
- 名字来自 `table_roster` / `seat_meta`；
- 位置标签（UTG/CO/BTN...）单独显示，避免和物理座位混淆；
- 庄家按钮用 `position=BTN` 或 hand 里的 dealer 信息。

#### 帧语义

- 一帧 = 一个语义事件，不是视频帧；
- `next` 推进一个 action 或 decision；
- `street_start` 清空本街下注、发公共牌；
- `decision` 显示 AI 气泡，默认暂停；
- hero 实际动作后气泡保留到下一帧，可点开 Inspector；
- 摊牌时翻开 `shows` 的牌；未秀牌保持牌背。

#### 筹码/底池

- 决策帧：用 `DecisionPoint.snapshot` 的 pot/to_call/opponents/hero_stack；
- 动作帧：用 timeline 的 `potAfter/stacksAfter/betsAfter`；
- 任一帧 `derived=true` 时，牌桌右上角显示「推算」，hover 显示原因；
- 缺金额时显示「?」，不补 0；
- 动画只做 CSS transition，不做复杂物理效果。

#### 播放控件

- 上一帧/下一帧/播放/暂停；
- 速度 0.5×–4×；
- 街跳转；
- 「只停在决策点」开关；
- 键盘同上；
- 深链 `?mode=table&frame=12`。

### 7.4 Prompt Inspector

#### Tab

| Tab | 内容 |
|---|---|
| System | 精确 system prompt，按 `##` 标题做目录/折叠 |
| User | 精确 user prompt（事实块） |
| Raw | 模型原始响应，格式化 JSON；失败时显示错误 |
| Facts | `facts_json` 的结构化视图（牌力/权益/门槛/选项） |
| Context | `table_stats` / `opponent_reads` / `table_dynamics` / roster |
| Diff | 与另一个版本/线上快照逐行 diff |

#### 能力

- 显示 `prompt_sha256` 前 12 位、字节数、token 数、耗时、model、temperature；
- 复制全文、下载 `.txt`/`.json`；
- 搜索关键字并高亮；
- `Diff` 支持：
  - 线上 vs 重跑；
  - A vs B；
  - 同版本两次 repeat（看采样波动，不是 prompt 变化）；
- diff 按行/按段落，折叠未变化部分；
- 默认只加载当前决策的 prompt，避免一手牌一次拉 100KB。

#### 安全

- prompt 里可能有玩家昵称、底牌、桌风观察；API 只绑定 `127.0.0.1`；
- 导出时可选 `--redact-names`；
- 删除手牌时级联删除 `replay_live_advices` 的 prompt/raw（或至少标记删除）；
- 不把 prompt 发给第三方分析服务。

### 7.5 入口与深链

| 入口 | 行为 |
|---|---|
| 手牌库行点击 | 总览 |
| 行内「回放」 | 瀑布流，`frame=0` |
| 决策点「牌桌定位」 | 牌桌模式，定位到 decision frame |
| A/B 结果行「对比」 | 打开瀑布流 + 右侧 A/B diff |
| 通知/报告链接 | `?mode=table&frame=...` |
| 收藏夹「一键重跑」 | 保留；跑完可跳实验结果 |

### 7.6 性能与可访问性

- 事件流用虚拟列表（>300 行时）；
- prompt 懒加载 + 浏览器缓存（按 `prompt_sha`）；
- 牌桌用 CSS grid/absolute，避免 canvas 依赖；
- 颜色不是唯一信息：动作/质量/分歧都要有文字/图标；
- 键盘可完成播放、跳转、打开 prompt；
- 小屏优先瀑布流，牌桌模式给「横屏提示」。

---

## 8. A/B 测试：判断「这次修改是否合理」

### 8.1 先定义「一次修改」

一次实验只允许一个主变量，写入 `change_scope`：

| scope | 例子 | baseline 来源 |
|---|---|---|
| `prompt` | 改 system 规则、事实块文案 | 线上/上次实验的 prompt 快照 |
| `facts` | 改 range/equity/门槛计算 | 旧代码重渲染或旧服务 |
| `model` | 换模型/温度/thinking | 同 prompt，不同 settings |
| `context` | 开/关 table_stats/opponent_reads | 同 prompt，不同 context mode |
| `code` | 修 tracker/decision 重建 | 旧 commit 或旧服务 |
| `mixed` | 以上多个 | **不推荐**；UI 警告，结论只能算探索 |

规则：

- 改 `advice.py` 的提示词/事实/选项 → 必须 bump `PROMPT_VERSION`；
- 改 `decisions.py` 重建语义 → bump `CURRENT_RECONSTRUCT_VERSION`；
- 改完跑 `replay-console/tests/test_prompt_golden.py`；
- 版本表记录 `engine_commit`/`engine_dirty`，dirty 时实验结果标「不可作为发布依据」。

### 8.2 手牌集设计

#### 规模

- 最小可用：**30 手 / 60 个决策点**；
- 推荐：**50–100 手 / 100–200 个决策点**；
- P0 回归守卫：**10–20 个决策点**，必须每次全过；
- holdout：20–30%，只用于确认，不用于调参。

#### 组成

| 角色 | 目的 | 例子 |
|---|---|---|
| `golden` | 已知正确，不能破坏 | 坚果河牌 value bet |
| `regression` | 修过的 bug，必须保持 | 连续过牌封顶、0 对手异常帧 |
| `exploration` | 新场景，允许变化 | 多人池河牌加注、短码 jam |
| `negative` | 不该变化的对照 | 干燥面空气过牌 |

#### 质量门槛

- 默认只收 `exact`；`approx` 单独跑敏感性分析；
- prompt 覆盖必须 100%，否则该决策点不能进 prompt A/B；
- 重复手牌（fingerprint 相同）去重；
- 数据异常（0 对手、行动线矛盾、牌面不可靠）先修或排除；
- 每条 item 存 `fingerprint`；手牌内容变了，实验创建时提示 stale。

#### 维护

- 每周看一次覆盖：街/位置/底池类型/结果/对手类型；
- 新 bug 修完 → 加一条 `regression`；
- 旧 case 失效（数据坏了/规则已废）→ 标 `archived`，不硬删；
- 不让集合无限膨胀；超过 200 手时按角色分层抽样。

### 8.3 实验生命周期

```text
draft
  → 选择 handSet revision + baseline/variant + repeat + contextMode
  → 冻结 experiment_hands（hand_id + fingerprint）
queued
  → 创建 replay_job（两个 version）
running
  → worker 逐决策点跑 A/B
done
  → 计算 metrics / P0 / pair review 汇总
  → 人工结论 adopt/reject/iterate
```

#### 版本选择

- baseline 默认 `channel=prod` 的版本；
- variant 默认最新 `candidate`；
- 两个版本必须同 model（除非 scope=model），同 `contextMode=snapshot`，
  同 `tableStatsMode=snapshot`；
- 如果 variant 的 `config_hash` 与 baseline 相同，拒绝创建（没有变量）。

#### 运行成本预估

- `calls = 决策点数 × 2 × repeat`；
- 100 个决策点 × 2 × 3 = 600 次调用；
- 按 1.5s/次、并发 4 估算约 4 分钟；按 3s/次约 8 分钟；
- UI 创建前显示预估（复用现有 `/api/runs/preview`）。

### 8.4 指标

#### A. 硬门槛（先看，不通过直接 reject）

| 指标 | 定义 | 通过线 |
|---|---|---|
| `p0_regression` | P0 断言在 baseline 通过、variant 失败的数量 | = 0 |
| `invalid_rate` | `source=error` / 总决策点 | 不高于 baseline + 1pp |
| `fallback_rate` | `source=fallback` / 总决策点 | 不高于 baseline + 1pp |
| `violation_rate` | 有 violation tag / 总决策点 | 不高于 baseline |
| `prompt_coverage` | 有 prompt 的决策点 / 总决策点 | ≥ 95% |
| `context_coverage` | 有 context snapshot / 总决策点 | = 100%（live 手牌） |

#### B. 行为变化（回答「改动有没有生效」）

| 指标 | 定义 |
|---|---|
| `action_flip_rate` | A/B 动作类型不同的决策点 / 配对决策点 |
| `size_change_rate` | 同动作但金额差 > 容差的 / 配对 |
| `targeted_change_rate` | 目标 case 中发生预期变化的 / 目标 case 数 |
| `stability` | 同一决策点 repeat 的多数占比（按版本分别算） |
| `flip_rate` | 1 − stability（重复之间翻转） |

#### C. 人工判断（回答「改得对不对」）

| 指标 | 定义 |
|---|---|
| `b_wins` / `a_wins` | 成对偏好计数 |
| `preference_win_rate` | `b_wins / (a_wins + b_wins)`，排除 same/unsure |
| `net_preference` | `b_wins - a_wins` |
| `review_coverage` | 已复盘配对 / 应复盘配对 |
| `bad_rate` | `replay_decision_marks.bad / 已复盘` |

#### D. 诊断指标（不能单独作为结论）

| 指标 | 说明 |
|---|---|
| `agreement` | 与人类实际动作一致；只说明「像不像人」 |
| `size_agreement` | 金额接近；受人类习惯影响 |
| `latency_p50/p95` | 变慢是否可接受 |
| `tokens_in/out` | 成本变化 |
| `prompt_bytes` | prompt 是否膨胀 |

#### 统计口径

- 以**手牌**为重采样单位，不以决策点为单位（同一手内决策相关）；
- 配对 bootstrap（1000 次）给 95% CI；
- `n < 30` 手只显示描述统计，不写「显著」；
- 分桶（街/位置/底池类型/对手类型）同时显示每桶 n；
- 明确写「agreement 不是正确率」。

### 8.5 P0 断言

`replay_hand_set_items.expect_json` 示例：

```jsonc
{
  "priority": 0,
  "role": "regression",
  "assertions": [
    {"type": "option_kind", "value": "call"},
    {"type": "not_option_kind", "value": "fold"},
    {"type": "amount_range", "min": 10.0, "max": 14.0},
    {"type": "reason_contains", "value": "赔率"},
    {"type": "reason_not_contains", "value": "空气"},
    {"type": "facts_contains", "value": "连续过牌封顶"},
    {"type": "stable_across_repeats", "value": true}
  ]
}
```

支持的类型（第一版）：

| type | 含义 |
|---|---|
| `option_kind` | 动作类型等于 |
| `option_id` | 精确 id（慎用，容易过拟合措辞） |
| `not_option_kind` | 动作类型不等于 |
| `amount_range` | 金额在区间内 |
| `reason_contains` | 理由包含关键词 |
| `reason_not_contains` | 理由不包含 |
| `facts_contains` | facts 文本包含 |
| `must_change_from_baseline` | 与 baseline 动作不同（用于「修好这个 bug」） |
| `must_not_change_from_baseline` | 与 baseline 相同（negative control） |
| `stable_across_repeats` | repeat 内多数一致 |

注意：

- 断言用于**回归守卫**，不是用来把模型措辞钉死；
- 优先断言动作/金额/关键事实，少用精确 option id；
- 每个 P0 都要有「为什么它是 P0」的 note；
- 断言失败时保留原始 prompt/response，方便定位。

### 8.6 成对人工复盘

#### 为什么需要

- `agreement` 只能告诉你「像不像人」；
- 两个版本都跟人类不同，可能 B 更对；
- 两个版本都跟人类一样，可能都错；
- 只有并排看 A/B 的 option + reason，才能判断「这次改动是否合理」。

#### UI

```text
┌ D3 · 转牌 · 底池 38BB · 需跟 12BB · Hero 后坐 169BB ─────────────┐
│ 实际：call 12BB                                                  │
│ A（prod）  call 12BB   “赔率 24%，范围权益 31%，跟注”            │
│ B（候选）  fold        “对手转牌下注范围偏强，权益 18% 不够”      │
│ 自动：动作不同 · B 少输 0.8BB（规则审计）                        │
│ [A 更好] [B 更好] [一样] [不确定]   标签：范围/赔率/河牌          │
│ 备注：……                                                         │
│ [看 A prompt] [看 B prompt] [Diff] [在牌桌回放]                   │
└──────────────────────────────────────────────────────────────────┘
```

#### 采样

- 全看：P0 + 目标 case + 所有 flip；
- 抽看：未变化的决策点随机 20%（防止只看到翻转，产生选择偏差）；
- 两人复核（可选）：结论不一致时标 `disputed`；
- 每条 review 存 `preferred/tags/note/reviewer/time`。

#### 键盘

- `A` / `B` / `=`（same）/ `U`（unsure）；
- `↑/↓` 上/下一条；
- `P` 打开 prompt；
- 自动保存，不需要点提交。

### 8.7 「合理」的判定规则（建议默认）

```text
1. 硬门槛
   - P0 regression = 0
   - invalid/fallback/violation 不高于 baseline + 1pp
   - prompt_coverage >= 95%，context_coverage = 100%

2. 收益
   - holdout 上 preference_win_rate >= 0.55，且 95% CI 下界 > 0.50
     或 targeted_case_pass_rate 提升 >= 10pp
   - 样本 >= 30 手 / 60 个决策点

3. 成本
   - p95 latency 不高于 baseline 的 1.2 倍
   - token 成本不高于 baseline 的 1.2 倍

4. 结论
   - 全过 → adopt
   - 硬门槛过、收益不显著 → iterate（扩大样本或缩小改动）
   - 硬门槛失败 → reject
   - 数据不足 → inconclusive
```

这不是统计教条，而是防止「看两个例子就上线」。如果样本小，就明确写
`inconclusive`，不要硬编一个「显著提升」。

### 8.8 最关键的坑：baseline 的 prompt 从哪来

现状的 `replay_versions` 是**配置快照**，不是代码快照。`config_hash` 包含
`prompt_version`/`advice_version`，但改完代码后：

- `run_blockers` 会拒绝跑旧 `advice_version` 的版本；
- 克隆旧版本会用**当前代码**，不是旧 prompt；
- 所以「A/B」很容易变成「新代码 + 配置 A」vs「新代码 + 配置 B」，
  而不是「旧 prompt」vs「新 prompt」。

这会让「改 prompt 后判断是否合理」失去可比性。解决方案：

#### 方案 1（推荐，本期）：stored prompt baseline

- 每个决策点已经保存了线上真实 `prompt_system/user`；
- baseline 跑的时候，**不重新渲染**，直接把存下来的 system/user 发给模型；
- variant 用当前代码重新渲染；
- 两边同模型、同采样参数、同 context 口径；repeat 3 次。

baseline prompt 的查找顺序（第一个命中即用）：

```text
1. replay_live_advices(hand_id, street, action_index) 的 prompt_system/user
2. 同 decision 在 prod 版本实验里的 replay_results.prompt_system/user
3. 都没有 → 拒绝 stored，降级 rerender 并标 baseline_approx=1
```

一个重要边界：如果这次改动**同时改了选项集合/金额映射**，baseline 的旧 prompt
里可能是旧 option id。此时必须用快照里的 `facts.options` 构造 baseline 的
`Situation`，或者把实验标成 `change_scope=mixed` 并声明不可直接比较；
不能拿旧 prompt 配新选项解析器，否则会把「解析失败」误判成「模型变差」。

```text
baseline: stored_prompt(decision) → model → result
variant:  render_prompt(current_code, snapshot) → model → result
```

实现上给 `advise()` 增加一个显式参数（不要复制一套解析逻辑）：

```python
advise(sit, settings, timeout=..., complete=...,
       prompt_override=(stored_system, stored_user))
```

- 仍然 `build_situation(sit)` 得到 options/审计口径；
- 如果 `prompt_override` 非空，跳过 `build_messages()`，直接用它；
- option 校验、别名、reason 截断、fallback 规则全部复用；
- `Advice.prompt_system/user` 写 override 的原文，`prompt_sha256` 也基于它；
- `replay/engine.py` 在 `baseline_prompt_mode=stored` 时从
  `replay_live_advices` / `replay_results` 取 prompt，传给 `execute_unit()`。

这样 baseline 和 variant 的差别只有 prompt 文本，不会多出第二套解析行为。

优点：

- 不需要旧代码/旧服务；
- 真正比较「模型当时看到的文本」和「新文本」；
- 对 prompt/facts 改动都成立（旧 facts 就在旧 prompt 里）。

缺点：

- baseline prompt 里的事实与 variant 可能不是同一套（这正是改动本身）；
- 如果线上 prompt 缺失，baseline 不可用。

#### 方案 2：rerender baseline

- 用当前代码 + 旧版本的配置/上下文重渲染；
- 只适合「配置/模型/context」变化，不适合 prompt 代码变化；
- UI 必须标 `baseline_prompt_mode=rerender`，并警告「不是旧 prompt」。

#### 方案 3：external old service

- 用旧 commit/worktree 起一个只读服务跑 baseline；
- 最严谨，但运维重；
- 留给「大版本/发布前」的最终验证。

#### 实验表字段

`baseline_prompt_mode` / `replay_experiments` 的 DDL 已在 4.4 的 `009_hand_sets.sql`
中加入，这里不重复 `ALTER`。三个取值：

- `stored`：baseline 使用决策点保存的线上/上次实验 prompt；
- `rerender`：用当前代码 + 旧配置重渲染；
- `external`：调用旧 commit 起的只读服务。

结果行记录 `baseline_prompt_source`（`live` / `experiment` / `rerender` / `external`）；
UI 在实验顶部显示：

```text
Baseline prompt：线上快照（2026-11-01 21:03）
Variant prompt：当前代码重渲染
```

如果 baseline 快照缺失，实验创建时直接拒绝，或降级为 `rerender` 并标
`baseline_approx=1`。**不要静默降级。**

### 8.9 一次修改后的标准工作流

```text
1. 改代码/提示词
2. bump PROMPT_VERSION / CURRENT_ADVICE_VERSION
3. 跑 golden test + 单测
4. 创建 candidate 版本（记录 commit / config_hash / prompt_version）
5. 选择手牌集 revision（或新建）
6. 创建实验：baseline=prod, variant=candidate, repeat=3, snapshot context
7. 先跑 P0 子集（10–20 个决策点）→ 有回归立即停
8. 跑全量 dev → 看 flip / targeted / preference
9. 跑 holdout → 只确认，不调参
10. 人工看成对结果 + prompt diff
11. 写结论 adopt/reject/iterate；adopt 才把 candidate 标 prod
12. 导出 Markdown/CSV，贴到 docs/复跑-*.md 或归档
```

### 8.10 常见误判

| 误判 | 后果 | 防范 |
|---|---|---|
| 用 agreement 当正确率 | 改得越像人越「好」，可能学到人的漏点 | UI 明确标注；主指标用 pair review |
| 只看看 flip 的 case | 选择偏差，忽略未变化但变坏的 | 随机抽 20% 未变化 |
| 在 holdout 上调参 | 过拟合，发布后失效 | holdout 只确认；调参只看 dev |
| 一手牌拆到 A/B 两边 | 信息泄漏 | 按 hand 切分，不按 decision |
| 不同 context 对比 | 比的是画像/桌况，不是改动 | 强制 snapshot；context_coverage=100% |
| 单次采样下结论 | 温度/模型波动被当成改动效果 | repeat>=3；报告分布/稳定性 |
| 改了 prompt 没 bump 版本 | 历史结果无法解释 | golden test + CI 检查 |
| 用旧版本名跑新代码 | 假 A/B | `run_blockers` + baseline_prompt_mode |
| 数据异常手牌进 A/B | 结论被 OCR 错误污染 | quality gate；warnings 排除 |
| 手牌集越加越大 | 跑一次几小时，没人跑 | 分层/抽样/定期归档 |

---

## 9. 复盘工作流（每天怎么用）

### 9.1 打完牌：10 分钟自动沉淀

- live 结束一手 → outbox → 自动入库；
- 手牌库「今天」出现新手牌；
- 不需要导出、不需要点导入、不需要记 hand_id。

### 9.2 第二天早上：15 分钟 triage

1. 打开「值得看」：`review_score >= 50`；
2. 先看 `AI_FALLBACK` / `DATA_WARN`：这些不是策略问题，是数据/配置问题；
3. 再看 `AI_DIFF` + `BIG_POT` + `RIVER_BIG`：高杠杆分歧；
4. 每手 30 秒：看瀑布流的决策行 + reason；值得深入就标 `★`；
5. 加入「我的回归集」：只加有代表性的，不是每手都加；
6. 人工标 `bad` 的，写一句结论，后续变成 regression case。

### 9.3 深度复盘：一手牌 10–20 分钟

1. 打开牌桌回放，逐帧看行动线；
2. 在 hero 决策点打开 Prompt Inspector：
   - 模型看到的事实是否完整？
   - 有没有矛盾/缺失/异常警告？
   - 选项金额是否正确？
3. 看 AI 的 option/reason：理由是否引用了正确事实？
4. 看实际动作：不一致时，是 AI 错、人错，还是数据错？
5. 标 good/bad/unsure + 标签 + 一句话结论；
6. 如果是可复现的规则问题 → 加入 regression set + 写 P0 断言。

### 9.4 改完 prompt：30–60 分钟回归

1. 跑 P0 子集（几分钟）；
2. P0 全过 → 跑 dev；
3. 看 flip 列表 + 抽 20% 未变化；
4. 人工成对偏好（先看 10–20 条，足够判断方向）；
5. 跑 holdout 确认；
6. 写结论；adopt 才发布。

### 9.5 每周：维护手牌集

- 看覆盖缺口（街/位置/底池类型/多人池/短码）；
- 归档失效 case；
- 清理重复/approx/invalid；
- 看一次「最近 7 天 A/B 翻转」有没有新类型；
- 把新的典型手牌加进 set，但控制总量。

---

## 10. API 设计

### 10.1 现有接口（保留）

```text
POST /api/hands/ingest
GET  /api/hands
GET  /api/hands/{id}
GET  /api/hands/{id}/comparison
PATCH /api/hands/{id}/mark
PATCH /api/decisions/{id}/mark
POST /api/experiments
POST /api/experiments/{id}/run
GET  /api/experiments/{id}/results
```

### 10.2 新增接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/hands/{id}/replay` | 手牌 + frames + decisions + quality |
| GET | `/api/hands/{id}/advices/{adviceId}/prompt` | 懒加载 prompt/raw/facts/context |
| GET | `/api/hands/{id}/prompt-diff` | 线上 vs 版本 / A vs B 的 diff |
| GET | `/api/hand-sets` | 手牌集列表 |
| POST | `/api/hand-sets` | 新建（name/description/purpose/filter/handIds） |
| PATCH | `/api/hand-sets/{id}` | 改名/描述/归档 |
| POST | `/api/hand-sets/{id}/items` | 批量加入（handIds） |
| DELETE | `/api/hand-sets/{id}/items` | 批量移除 |
| POST | `/api/hand-sets/{id}/freeze` | 冻结 revision |
| GET | `/api/hand-sets/{id}/coverage` | 覆盖统计 + 补齐建议 |
| POST | `/api/experiments` | 扩展：`handSetId`、`baselinePromptMode`、`changeScope` |
| GET | `/api/experiments/{id}/pairs` | 成对结果（分页/过滤） |
| POST | `/api/experiments/{id}/pairs/{decisionId}/review` | 成对偏好 |
| GET | `/api/experiments/{id}/report` | Markdown/CSV 报告 |
| GET | `/api/ingest/health` | 队列/最后成功/错误 |

### 10.3 `GET /api/hands/{id}/replay`

```jsonc
{
  "hand": {
    "id": 1000000000123,
    "uid": "20261101-bfdd2c4dd3",
    "heroCards": ["Ks", "Td"],
    "heroPosition": "LJ",
    "board": ["3d", "Ac", "2c", "Kh", "9s"],
    "result": {"heroNetBb": -12.5, "outcome": "lose", "source": "stack_delta"},
    "quality": {"status": "exact", "warnings": []}
  },
  "frames": [ ... ],
  "decisions": [
    {
      "id": 123456,
      "street": "turn",
      "actionIndex": 14,
      "adviceId": 98765,
      "promptSha256": "9f2c...",
      "promptMissing": false,
      "option": {"id": "call", "kind": "call", "amount": 12.0},
      "reason": "赔率够",
      "source": "model"
    }
  ],
  "versions": [{"id": 12, "name": "prod-1101"}, {"id": 13, "name": "candidate-1102"}]
}
```

### 10.4 `GET /api/hands/{id}/advices/{adviceId}/prompt`

```jsonc
{
  "adviceId": 98765,
  "promptSha256": "9f2c...",
  "promptBytes": 12480,
  "tokensIn": 3214,
  "tokensOut": 86,
  "model": "deepseek-flash",
  "modelParams": {"temperature": 0.2, "thinking": "enabled"},
  "system": "...",
  "user": "...",
  "rawResponse": "{\"option\":\"call\",...}",
  "facts": {...},
  "context": {...},
  "promptMissing": false
}
```

### 10.5 `GET /api/experiments/{id}/pairs`

```jsonc
{
  "experimentId": 7,
  "split": "dev",
  "rows": [
    {
      "decisionId": 123456,
      "handId": 1000000000123,
      "street": "turn",
      "actionIndex": 14,
      "actual": {"action": "call", "amountBb": 12.0},
      "A": {"versionId": 12, "option": {"kind": "call", "amount": 12.0},
            "reason": "...", "source": "model", "promptSha256": "aa..."},
      "B": {"versionId": 13, "option": {"kind": "fold", "amount": null},
            "reason": "...", "source": "model", "promptSha256": "bb..."},
      "auto": {"sameKind": false, "sameSize": false, "bucket": "动作不同",
               "p0Pass": false},
      "review": {"preferred": "B", "tags": ["范围"], "note": "..."}
    }
  ],
  "summary": {
    "n": 87, "flips": 12, "aWins": 8, "bWins": 15, "same": 4, "unsure": 2,
    "p0Regressions": 1, "promptCoverage": 1.0
  }
}
```

### 10.6 `POST /api/experiments/{id}/pairs/{decisionId}/review`

```json
{"preferred": "B", "tags": ["范围", "河牌"], "note": "B 的范围收窄更合理"}
```

### 10.7 实时 prompt 观察（可选，P5）

如果希望打牌时就能看 prompt，而不是打完再看：

- live 增加本地 SSE：`GET http://127.0.0.1:8020/events`；
- 事件：
  - `advice.start`：`{handId, street, actionIndex, system, user}`；
  - `advice.result`：`{option, reason, durationMs, tokens}`；
  - `hand.end`：`{handId, outboxPath}`；
- 一个极简页面 `tools/live_inspector.html` 订阅并展示；
- 默认关闭，`--inspect-port 8020` 开启；
- 不参与主链路，不影响延迟。

> 这不是「手牌复现」的必需项，但它是「实时看到 prompt」最直接的形态；
> 如果只做复盘，P5 可以不做。

---

## 11. 前端信息架构

### 11.1 路由

```text
/hands                    手牌库
/hands/:id                总览
/hands/:id/replay         瀑布流/牌桌（mode 参数）
/sets                     手牌集列表
/sets/:id                 手牌集详情
/experiments              A/B 实验
/experiments/:id          实验详情
/experiments/:id/pairs    成对结果
/jobs/:id                 任务详情（现有）
```

### 11.2 组件

```text
components/
  replay/
    ReplayPage.tsx          # 容器：数据 + 播放状态 + 模式切换
    ReplayWaterfall.tsx     # 事件流
    ReplayTable.tsx         # 牌桌布局
    PlaybackControls.tsx    # 播放/暂停/速度/街跳转
    StreetRail.tsx          # 左侧街导航
    AdviceBubble.tsx        # AI 建议气泡
    PromptInspector.tsx     # system/user/raw/facts/context/diff
    PromptDiff.tsx          # 逐行 diff
    Seat.tsx / Pot.tsx / ActionChip.tsx
  sets/
    HandSetDrawer.tsx       # 从手牌库加入
    HandSetCoverage.tsx     # 覆盖统计
  experiments/
    PairReview.tsx          # A/B 成对复盘
    ExperimentSummary.tsx   # 指标卡
```

### 11.3 状态

- 服务端状态：TanStack Query（现有）；
- 播放状态：`useReducer`（`cursor/playing/speed/pauseOnDecision/follow`）；
- URL：`mode/frame/compare/split`，可分享；
- prompt：按 `adviceId` 懒加载，缓存 key 含 `promptSha256`；
- 切换模式保留 cursor；如果目标模式没有对应帧，落到最近的 decision。

### 11.4 视觉原则

- 信息优先：牌桌可以好看，但不能牺牲数字可读性；
- 质量/推算/分歧用徽标，不靠颜色；
- 长 prompt 用等宽字体、可折叠、可搜索；
- 移动端瀑布流优先；牌桌横屏提示；
- 深色模式兼容（现有样式）。

---

## 12. 实施计划

### 12.1 阶段

| 阶段 | 内容 | 交付 | 预估 |
|---|---|---|---|
| P0 | prompt 捕获 + 迁移 008 + 详情页展示 | 线上 prompt 可看；旧数据标 missing | 2–3 天 |
| P1 | 自动 ingest worker + 健康 + 回填 | 手牌结束自动入库；断网补发 | 2–3 天 |
| P2 | timeline + 瀑布流 + Inspector | 文字回放可用 | 3–5 天 |
| P3 | 牌桌回放 + 播放控件 | 逐帧回放可用 | 3–5 天 |
| P4 | 手牌集 + 实验 v2 + 成对复盘 | 一键回归 | 3–5 天 |
| P5 | 深挖分/覆盖建议/实时 SSE/报告 | 日常 triage 提效 | 2–4 天 |

> 预估是单人熟悉代码后的量级，不是承诺；P2/P3 前端占大头。

### 12.2 文件级改动

| 文件/目录 | 改动 |
|---|---|
| `poker_ocr/advice.py` | `Advice` 新字段；`advise` 捕获 prompt/raw/reason_full/facts/params |
| `poker_ocr/live.py` | `_write_hand` 带 prompt；partial 入 outbox；启动 ingest worker；终端状态 |
| `poker_ocr/hand_track.py` | `result/hero_net_bb/data_quality`；可选 action 快照 |
| `poker_ocr/hand_outbox.py` | 统计/归档/隔离；不改变核心语义 |
| `poker_ocr/hand_ingest.py`（新） | `HandIngestWorker`：扫描/发送/退避/错误分类 |
| `tools/sync_hand_outbox.py` | 复用 worker 的发送逻辑；保留回填 |
| `tools/backfill_hand_library.py`（新） | 旧 outbox → ingest；补 timeline/flags |
| `replay-console/replay/migrations/008...011` | 上述 DDL |
| `replay-console/replay/repository.py` | prompt/flags/timeline 读写；hand sets；pair reviews |
| `replay-console/replay/service.py` | ingest 扩展；replay API；实验 v2；指标 |
| `replay-console/replay/api.py` | 新路由 |
| `replay-console/replay_web/src/pages/...` | ReplayPage / HandSetPage / PairReview |
| `replay-console/replay_web/src/components/replay/...` | 新组件 |
| `replay-console/tests/...` | 单测/集成/golden |

### 12.3 开关

```bash
ADVICE_PROMPT_CAPTURE=1        # 捕获 prompt/raw；默认开
HAND_INGEST_MODE=auto          # off/auto/direct
REPLAY_TIMELINE=1              # 生成/使用 timeline
HAND_SET_AB=1                  # 新手牌集/成对 A/B
```

灰度：

1. 先只开 `ADVICE_PROMPT_CAPTURE`，确认 live 延迟/文件大小无影响；
2. 开 `HAND_INGEST_MODE=auto`，观察 1–2 天队列/重复；
3. 开 `REPLAY_TIMELINE`，先只读生成，不切 UI；
4. 切瀑布流 UI；
5. 切牌桌 UI；
6. 开手牌集/实验 v2；
7. 旧手牌回填。

### 12.4 回填

```bash
# 已入库但没有 prompt 的旧手牌：标 prompt_missing，不伪造
.venv/bin/python tools/backfill_hand_library.py --prompts missing

# 重新生成 timeline/flags（不重跑模型）
.venv/bin/python tools/backfill_hand_library.py --timeline --flags

# 从 outbox 补发历史手牌
.venv/bin/python tools/sync_hand_outbox.py --retry-failed
```

旧手牌如果只有 `advices` 没有 prompt：

- 不重算 prompt 冒充；
- 标 `prompt_missing=1`；
- 如果确实需要，可单独跑一次「重建 prompt」任务，结果标
  `prompt_source=reconstructed`，不进入严格 A/B；
- `replay_results` 里已有的重跑 prompt 可以复用（它们是当时真实调用过的）。

---

## 13. 测试与验收

### 13.1 单元测试

| 测试 | 断言 |
|---|---|
| `test_advice_captures_prompt` | `prompt_sha256` 与 `sha256(system+sep+user)` 一致；raw/reason_full 存在 |
| `test_advice_fallback_marks_prompt_missing` | 无 settings/0 对手时 `prompt_missing=1`，reason 明确 |
| `test_advice_to_dict_backward_compatible` | 旧字段不变；新字段只在非空时出现 |
| `test_outbox_idempotent` | 同 uid 重复 enqueue 覆盖不重复 |
| `test_ingest_idempotent` | 同 uid 两次 ingest 只一手；prompt 可更新 |
| `test_timeline_known_hand` | frames 数、顺序、decision 插入位置、pot/stack |
| `test_timeline_missing_amount` | 缺金额 → `derived=true`，不补 0 |
| `test_flag_mask` | 各信号位正确；组合筛选 |
| `test_pair_metrics` | A/B/same/unsure 计数、win rate、CI |
| `test_p0_assertions` | 各断言类型通过/失败 |
| `test_prompt_diff` | 相同 hash 无 diff；改一行有 diff |
| `test_baseline_stored_prompt` | baseline 用存储 prompt，不调用 `build_messages` |

### 13.2 集成测试

1. **live → outbox → ingest → DB**：
   - 构造一手含 2 个决策点的记录；
   - 启 mock ingest；
   - 断言 hand/players/decisions/live_advices/timeline/flags 都写入；
   - 重复发送无重复。
2. **服务离线**：
   - 停 ingest；发 3 手；pending=3；
   - 起服务；自动补；pending=0；failed=0。
3. **坏数据隔离**：
   - 一条坏 record + 两条好 record；坏 hand 进 failed，好 hand 入库。
4. **旧手牌无 prompt**：
   - ingest 旧 payload；`prompt_missing=1`；UI 显示徽标；A/B 排除。
5. **实验**：
   - 20 手、30 决策点；baseline/variant；repeat 2；
   - P0 断言 1 条失败；metrics 正确；pair review 汇总。
6. **回放 API**：
   - frames 与 decisions 对齐；深链 frame 正确。

### 13.3 前端测试

- 瀑布流：渲染帧数、decision 展开、prompt 懒加载；
- 牌桌：座位映射、下一帧、街切换、推算徽标；
- 模式切换 cursor 保持；
- 键盘快捷键；
- A/B pair review 自动保存/失败重试；
- 小屏布局。

### 13.4 验收标准

| # | 标准 | 验证 |
|---|---|---|
| A1 | 换手被识别后 ≤10s 出现在库（服务在线） | 计时/日志 |
| A2 | 服务离线不丢，恢复后自动补 | 断网测试 |
| A3 | 同一手重复发送不重复入库 | 幂等测试 |
| A4 | 每个模型建议都有 prompt 或 `prompt_missing` | SQL 统计 |
| A5 | 瀑布流/牌桌走完一手牌，事件数一致 | 对比 frames |
| A6 | 任意决策点能看到精确 prompt/raw/reason | UI/API |
| A7 | 手牌集可增删/冻结，实验引用 revision | API 测试 |
| A8 | A/B 结果可比：同 context、baseline prompt 来源明确 | 实验检查 |
| A9 | P0 回归能在结果页一眼看到 | UI |
| A10 | 成对偏好可保存/汇总/导出 | API/UI |
| A11 | 列表筛选 `flags/minScore` 正确 | 单测 |
| A12 | 旧手牌回填后不产生重复/错误统计 | 回填报告 |

### 13.5 性能目标

- 手牌列表（5000 手）< 500ms；
- 手牌详情（含 10 个决策点）< 1s；
- replay frames（<100 帧）< 500ms；
- prompt 懒加载 < 300ms；
- A/B 结果页（1000 配对）< 1s；
- 写入不影响 live 帧率（outbox 异步 < 5ms）。

---

## 14. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| OCR 行动线错误 | 回放/AB 结论失真 | 质量门槛；inferred 标记；行动线校验；坏手排除 |
| prompt 太大 | DB/网络/UI 压力 | MEDIUMTEXT；懒加载；180 天保留；gzip 归档 |
| MySQL 离线 | 手牌延迟入库 | outbox + 自动重试；健康提示；不阻塞 live |
| A/B 过拟合 | 发布后退化 | holdout；P0；negative control；最小样本 |
| 模型波动 | 结论不可复现 | repeat>=3；分布/稳定性；记录 temperature/thinking |
| context 不一致 | 比错对象 | snapshot 强制；coverage=100%；baseline prompt 来源显式 |
| 旧代码无法复现 prompt | 假 A/B | stored prompt baseline；external old service；标记 rerender |
| 数据隐私 | 昵称/底牌泄露 | 本地绑定；token；导出脱敏；保留策略 |
| 两套回放视图漂移 | 用户困惑 | 单一 timeline 契约；共享组件/测试 |
| 迁移风险 | 生产库故障 | 只加列/表；备份；灰度；回滚脚本 |
| 回填污染 | 旧数据重复/统计错 | 幂等 uid；dry-run；报告；partial 排除 |
| 手牌集膨胀 | 跑一次太久 | 分层/抽样/归档；P0 子集先跑 |

---

## 15. 需要你确认的决策

下面是我建议的默认值；如果你没有特别偏好，我按这些实现。

| # | 问题 | 我的建议 | 为什么重要 |
|---:|---|---|---|
| 1 | 手牌库的 system of record 用 MySQL 还是再加 SQLite？ | **MySQL（现有 replay-console）**；outbox 只做队列 | 避免双事实源；现有 ingest/实验/UI 都在 MySQL |
| 2 | 自动入库放 live 进程内，还是单独 daemon？ | **live 内嵌线程 + 保留独立 daemon/工具** | 不阻塞打牌；离线可补；运维简单 |
| 3 | prompt/raw 全文保留多久？ | **180 天全文，之后压缩归档，哈希永久** | 兼顾复盘/成本/隐私 |
| 4 | 牌桌回放要不要存视频关键帧？ | **先只做语义帧**；需要时再挂截图 | 视频存储/同步成本高；语义帧已能复盘 |
| 5 | A/B 的 baseline 用哪种？ | **stored prompt 优先**；缺失才 rerender 并标警告 | 否则改代码后 A/B 不是旧 prompt vs 新 prompt |
| 6 | 判断「合理」的主指标？ | **P0 回归 + holdout 成对偏好**；agreement 只做诊断 | 实际行动不是 ground truth |
| 7 | 手牌集规模/切分？ | **50–100 手 / 100–200 决策点；dev 70% / holdout 30%** | 小于 30 手不写显著 |
| 8 | 通过后自动发布，还是人工确认？ | **人工确认**；报告自动生成 | 避免小样本/数据问题导致误发布 |
| 9 | 是否要打牌时实时看 prompt？ | **P5 可选**；先做赛后回放 | 实时 SSE 简单，但不是核心闭环 |
| 10 | 是否多用户/远程访问？ | **本期单用户、127.0.0.1** | 降低鉴权/隐私复杂度 |
| 11 | 结果/输赢怎么来？ | **stack delta 自动算，读不到标 unknown** | 不猜；输赢只做筛选/噪声标签 |
| 12 | 旧手牌没有 prompt 怎么办？ | **标 missing，不伪造**；需要时单独重建并标 reconstructed | 保证 A/B 可信 |

### 如果只做三件事

1. **P0：prompt 捕获 + 迁移 008**——没有这个，后面所有 A/B 都不可信。
2. **P1：自动 ingest worker**——没有这个，手牌库永远需要手动导入。
3. **P2：瀑布流 + Inspector**——没有这个，prompt 存了也没人看。

牌桌回放和手牌集可以随后做；它们建立在同一份 timeline 和同一份
prompt snapshot 上。

---

## 16. 附录

### A. 关键代码索引

| 主题 | 位置 |
|---|---|
| 手牌跟踪/结束 | `poker_ocr/hand_track.py`：`LiveHandTracker.observe()`、`take_finished()`、`_hand_dict()` |
| 建议生成 | `poker_ocr/advice.py`：`build_messages()`、`advise()`、`Advice.to_dict()`、`_advice_context()` |
| live 落库 | `poker_ocr/live.py`：`drain_advice()`、`_write_hand()` |
| outbox | `poker_ocr/hand_outbox.py`；`tools/sync_hand_outbox.py` |
| 入库 | `replay-console/replay/service.py::ingest_hands()`；`repository.py::replace_live_advices()` |
| 决策重建 | `replay-console/replay/decisions.py::build_decisions()` |
| 重跑引擎 | `replay-console/replay/engine.py::execute_unit()` |
| 手牌详情 | `replay-console/replay_web/src/pages/HandDetailPage.tsx` |
| 实验 | `replay-console/replay/service.py::create_experiment()`、`experiment_results()` |
| 迁移 | `replay-console/replay/migrations/001...007` |

### B. 建议的迁移编号

```text
008_live_prompt_and_flags.sql
009_hand_sets.sql
010_pair_reviews.sql
011_version_channel.sql   # replay_versions.channel/promoted_at
```

### C. prompt 快照示例（截断）

```text
system:
你是德州扑克实时建议助手。只能从给定选项里选一个，输出 JSON。
...
## 事实优先级
1. 硬事实（底牌/公共牌/底池/需跟/后手）
2. 程序裁决（赔率门槛/范围权益/牌力）
3. 对手画像
4. 桌风/人工观察
...

user:
## 当前局面
- 桌况：9 人桌，盲注 1/2，前注 0.5
- 位置：LJ（翻后不是最后行动）
- 底牌：Ks Td
- 公共牌：3d Ac 2c Kh 9s
- 底池：38BB；需跟：12BB；后手：169BB；SPR：4.4
...
## 选项
- fold（弃牌）
- call（跟注 12BB）
- raise-1/2（加注到 31BB）
...
```

### D. 指标公式

```text
action_flip_rate      = count(kind_A != kind_B) / count(pairs)
size_change_rate      = count(kind_A == kind_B and |amt_A-amt_B|>tol) / count(pairs)
preference_win_rate   = b_wins / (a_wins + b_wins)          # 排除 same/unsure
net_preference        = b_wins - a_wins
p0_regression         = count(P0 pass on A and fail on B)
invalid_rate          = count(source=error) / count(results)
fallback_rate         = count(source=fallback) / count(results)
violation_rate        = count(len(violation_tags)>0) / count(results)
stability             = max(option_count) / repeat_count    # 每个决策点
flip_rate             = 1 - stability
prompt_coverage       = count(prompt_sha256 is not null) / count(decisions)
context_coverage      = count(context_json is not null) / count(decisions)
```

### E. 事件帧类型

| type | 必有字段 | 可选字段 |
|---|---|---|
| `hand_start` | `handUid/table/hero/roster` | `startedAt` |
| `street_start` | `street/board/potStart` | `warnings` |
| `action` | `actor/action/amount/potAfter` | `stacksAfter/betsAfter/allIn/inferred/derived` |
| `decision` | `decisionId/decisionKey/snapshot/advice` | `promptSha256/promptMissing` |
| `showdown` | `shows` | `result` |
| `hand_end` | `result/quality` | `warnings` |

### F. 一句话总结

**把「模型当时看到了什么、说了什么、为什么」变成一手牌的一部分；
把「这次改动有没有变好」变成一次可重复的对照，而不是一段记忆。**
