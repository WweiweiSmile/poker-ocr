# 范围推导 trace 字段契约 v1.2（live.range.jsonl）

> **v1.2（2026-11-14，看板消费端）**：只加一个字段——`final.grid[169]`。看板默认停在
> 「留存量」，没有它就画不出默认热图；取剪枝后支撑集内的归一化份额，与 `width_pct`
> 同一批 combo 算出来。`final` 仍不带 `detail`（逐 combo 明细看最后一步）。
>
> **v1.1（2026-11-14，#29）**：只加字段、只改中间步语义——逐格明细（`detail` / `cell_n`）、
> `spr` 可为 null 且带 `spr_source`、dead 改为**逐步**剔除。文件名与 `v` 前缀沿用 v1，
> 读端按 `live.range.v1` 前缀匹配；逐条变更见 §6。

> 出处：map #1「对手范围与牌力推导的可观测化（日志 + 可视化复现）」的决策票
> #3「live.range.jsonl 的字段契约（v1）」。归因能力的依据是
> [范围收窄-可归因因子清单.md](范围收窄-可归因因子清单.md)（#2）。
>
> 本文件只定 **一条记录长什么样**。采集点怎么埋、性能预算、轮转与保留策略
> 归 #6；13×13 的上色与聚合口径归 #4；equity / 牌力的分解口径归 #5；看板
> 怎么摆归 #8。

## 0. 一句话

一次决策一行 JSON：把「每个对手的翻前先验 → 逐事件乘数 → 最终范围」的每一步
都存成可重放的输入 + 摘要，让任何一步的收窄都能**离线重算并逐 combo 归因**。

## 1. 文件与写入

| 项 | 约定 |
|---|---|
| 路径 | `logs/live.range.jsonl`（相对 live 进程 cwd，由 `--range-trace-out` 可覆盖） |
| 格式 | 每行一个 JSON 对象（UTF-8，UTF-8 无 BOM，`ensure_ascii=False`），行内不换行 |
| 顺序 | 只追加；同一进程内按决策产生顺序 |
| 轮转 | 按天切分（`live.range.jsonl.YYYYMMDD`），细节归 #6 |
| 开关 | 默认开；`--no-range-trace` 关闭。**关闭时 hand_vs_ranges 的行为必须逐位不变**（trace 只读中间量，不参与计算） |
| 原子性 | 单行 < PIPE_BUF 时直接 append；否则先写临时行再 fsync 追加。半行由读端跳过（`json.loads` 失败即丢该行并计数） |

## 2. 两类记录

`status` 是必填字段：

- `"ok"`：真的跑了范围计算（含 `seats` 数组）。
- `"skipped"`：被前置条件挡下，**没有**范围计算。只带 `reason`，不带 `seats`。

`reason` 取值（与 `hand_vs_ranges._lookup` 的早退一一对应）：

| reason | 触发条件 |
|---|---|
| `preflop` | `snap.street == PREFLOP`（翻前不做范围计算：先验已覆盖翻前入池） |
| `board_short` | `len(snap.board) < 3` |
| `no_hero_cards` | `not snap.hero_cards` |
| `no_specs` | `field_ranges.resolve(snap).specs` 为空 |
| `disabled` | `HAND_VS_RANGES_ENABLED` 关 |
| `error` | `lookup()` 吞掉的异常（附 `error_type`，不含 traceback） |

> 翻前零记录是**设计决定**（grilling 1A）：翻前没有范围计算，硬造一条
> 「只有先验」的记录会让看板分不清「算了但很宽」和「根本没算」。D 类验收
> 问题看首个翻后决策的 `prior` 即可。

## 3. `status:"ok"` 的字段

### 3.1 顶层

```json
{
  "v": "live.range.v1.2",
  "status": "ok",
  "ts": "2026-11-13T13:20:41.512+08:00",
  "session": "20261113-131502-9f2a1c",
  "hand": 42,
  "street": "flop",
  "decision_key": {"hand": "42", "street": "flop", "actions": 7, "pot": 12.5, "to_call": 4.0, "hero": ["Ah", "As"]},
  "board": "Kd7c2s",
  "dead": ["Ah", "As"],
  "hero": {"pos": "CO", "stack": 98.0, "pot": 12.5, "to_call": 4.0, "spr": 7.8,
           "spr_source": "snapshot"},
  "params": {...},
  "seats": [...],
  "omitted": 1,
  "hero_equity": {...},
  "warnings": []
}
```

| 字段 | 类型 | 语义 | 来源 |
|---|---|---|---|
| `v` | str | 契约版本，当前 `"live.range.v1.1"`；读端按 `live.range.v1` 前缀匹配（v1.1 是只加字段的补丁） | 常量 |
| `status` | str | `"ok"` | — |
| `ts` | str | 记录生成时刻，ISO-8601 带本地偏移、毫秒 | `datetime.now().astimezone()` |
| `session` | str | live 会话 id，与 states.jsonl 的 `session_id` 同值 | live 进程 |
| `hand` | int | 手牌序号（`snap.hand_id` 的整数部分；非数字手牌名另存 `hand_name`） | snapshot |
| `street` | str | `"flop"｜"turn"｜"river"` | snapshot |
| `decision_key` | obj | **join 键**，拆自 `snap.decision_key` 元组 | `hand_track` 现有指纹 |
| `board` | str | 当前公共牌，按牌面顺序拼接，最多 5 张 | snapshot |
| `dead` | [str] | 已死牌 = hero 手牌 ∪ 公共牌，排序后 | `_lookup` 现有 dead 集合 |
| `hero` | obj | `pos/stack/pot/to_call/spr/spr_source`；**不含 hero 手牌**（在 `dead` 与 `decision_key.hero` 里，避免重复） | snapshot |
| `hero.spr` | float \| null | `stack / pot` 的 SPR；拿不到 stack 时为 null，消费端不得自行补默认值 | snapshot |
| `hero.spr_source` | str | `"snapshot"`（快照有筹码）｜`"unavailable"`（归档重放拿不到 stack）。为 `unavailable` 时 `spr` 必为 null，**不写 99 这类假值** | snapshot 是否有 stack |
| `params` | obj | 见 3.2 | `range_flow` / `strategy_tables` |
| `seats` | [obj] | 已建模对手，最多 4 家（`field_ranges.resolve` 的 cap） | `_lookup` |
| `omitted` | int | 在局但未建模的对手数（超过 4 家上限的部分） | `_lookup` |
| `hero_equity` | obj | 见 3.5；**内部口径由 #5 定**，v1 只固定容器与三个元字段 | `hand_vs_ranges` |
| `warnings` | [str] | `field.warnings` + `_lookup` 追加的告警，原样字符串 | `_lookup` |

`decision_key.hero` 是 hero 手牌的两张牌（排序后），用于校验 join 没有错行。

### 3.2 `params`

```json
{
  "alpha_in": 1.0,
  "alpha_eff": 1.0,
  "n_events": 4,
  "n_applied": 4,
  "support_floor": 0.02,
  "min_likelihood": 0.0001,
  "event_model": true,
  "tables": {"profile": "sourced", "variant": "v1", "sha256": "9c1f…"},
  "prompt_version": "lean-zh-2026-11-08.2",
  "advice_version": "2026-11-12.1"
}
```

| 字段 | 语义 | 来源 |
|---|---|---|
| `alpha_in` | 环境给的 α（`_env_alpha`），默认 1.0 | `range_flow._env_alpha()` |
| `alpha_eff` | 实际用的 α：`n≥4` 时 `max(0.5, α·0.97^(n-3))`，再夹 `[0.05, 2.0]` | `effective_alpha` |
| `n_events` | 该座位事件总数（**含被跳过的事件**） | `len(events)` |
| `n_applied` | 真正参与连乘的事件数 | 内层循环计数 |
| `support_floor` | 支撑集相对下限 `w ≥ floor·peak` | `_env_support_floor()` |
| `min_likelihood` | 单事件似然下限（默认 1e-4，**不可逆截断**） | 常量 |
| `event_model` | `RANGE_EVENT_MODEL` 开关的生效值 | `range_flow.enabled()` |
| `tables` | 表身份：profile 名（`default/sourced/…`）、variant、`poker_ocr/tables/range_flow.json` 的 sha256 前 16 位 | `strategy_tables` |
| `prompt_version` / `advice_version` | 该决策所用提示词与建议版本，便于和提示词映射票对齐；live 侧拿不到建议版本时 `advice_version` 为 null | `prompt/version.py` |

> `alpha_eff` 与 `n_events` 必须**一起**存：#2 指出 α 的指数依赖全链事件数，
> 只存生效值会让重算无法复现。表身份必须存：`range_flow._BUILD_CACHE` 的键
> 不含 profile，热切表时缓存不失效（见 #6），事后重算要靠 sha256 判断「当时用的
> 是哪一版表」。

### 3.3 `seats[i]`

```json
{
  "seat": "3",
  "pos": "BB",
  "archetype": "loose",
  "path": "event_model",
  "mode": "defend",
  "bucket": "facing_bet_closing",
  "prior": {"id": "BB_DEF_vs_CO_vs_40", "version": 1, "source": "named:BB_DEF_vs_40",
            "width_pct": 41.2, "weighted_width": 38.9, "combo_count": 546, "grid": [169]},
  "steps": [...],
  "final": {"width_pct": 18.4, "weighted_width": 15.2, "effective_width_pct": 9.1,
            "peak": 0.0041, "rho": 0.31, "combo_count": 244, "grid": [169]},
  "equity": {"method": "exact", "removed": 11,
             "behind": 0.580, "tie": 0.030,
             "beaten_by": [{"name": "Flush", "share": 0.21, "combos": 34, "examples": ["AsKs×1"]}],
             "ahead_groups": [{"name": "One pair", "share": 0.09}]}
}
```

| 字段 | 语义 |
|---|---|
| `seat` / `pos` | 座位 id 与位置，取自 `OpponentRangeSpec` |
| `archetype` | 该座位的画像标签；无画像时 `"unknown"`（`_archetypes` 只收录非 unknown） |
| `path` | `"event_model"`（走了 `range_flow.build_range`）或 `"board_range"`（回退旧口径）；两者**不可混算** |
| `mode` | `board_range` 的模式（`defend/bluff/…`），`event_model` 时仅作记录 |
| `bucket` | 最后一个事件的桶（`range_flow.bucket_of`）；无事件时为空串 |
| `prior` | **step 0**：翻前先验的完整快照，见 3.3.1 |
| `steps` | 逐事件阶梯，见 3.3.2；`path=="board_range"` 时为空数组 |
| `final` | 最终范围摘要（`Range` 上的数）；**v1.2** 起带 `grid[169]`（剪枝后支撑集内的归一化份额，与 `width_pct` 同一批 combo）。不带 `detail`——逐 combo 明细看最后一步 |
| `detail_omitted` | **v1.1** 记录级：因时间/体积闸没写 `detail` 的步号数组（阶梯档这些步仍有 `cell_n`；骨架档 `cell_n` 一并置空）。为空数组表示全部步都有明细 |
| `equity` | 该对手与 hero 的比牌分布（**`method:"exact"`**，按范围权重逐 combo 归一化）：`behind` / `tie` 为边缘分布，`ahead = 1-behind-tie` 不单独存；`beaten_by` / `ahead_groups` 为牌型分组（带 169 键示例）；`removed` = 因 hero 牌 + 公共牌剔除的 combo 数。口径细节见 `docs/equity-与牌力分解口径-v1.md` §2 |

`final` 的四个宽度数是**并列**关系，不是同一个数的别名：

- `width_pct` = 非零权重 combo 数 / 1326（视觉口径）
- `weighted_width` = 权重和 / 1326（混合频率口径）
- `effective_width_pct` = 按权重降序累到 90% 质量所需的 combo 占比（事件模型连乘后支撑集接近 100%，这个才反映「实际集中度」）
- `peak` / `rho` = 最大权重、支撑集质量（用于解释 `support_floor` 截断）

#### 3.3.1 `prior`（step 0）

| 字段 | 语义 |
|---|---|
| `id` / `version` | `Range.id` / `Range.version` |
| `source` | `Range.source`：`named:RFI_<pos>`｜`named:BB_DEF_vs_<bucket>`｜`rules:<name>`｜`uniform`（board-only）｜自定义 |
| `width_pct` / `weighted_width` / `combo_count` | 与 `final` 同口径 |
| `grid` | 169 格占比（定义见 §4） |

先验必须能在**没有本手牌**的情况下复现：`id + version + source + 表 sha256` 四件套即可从 `poker_ocr/tables` 重建。

#### 3.3.2 `steps[i]`

```json
{
  "i": 0,
  "street": "flop",
  "event": {"actor": "3", "action": "call", "amount": 4.0, "pot_before": 8.5, "spr_before": 7.8},
  "bucket": "facing_bet_closing",
  "spr_bucket": "mid",
  "alpha_k": 1.0,
  "z_k": 0.63,
  "mass_before": 1.0,
  "mass_after": 0.63,
  "width_before": 41.2,
  "width_after": 30.1,
  "grid": [169],
  "dropped": {"dead": 2, "zero": 0, "floor": 12},
  "top": [...],
  "cell_n": [0, 6, 0, ...],
  "detail_n": 47,
  "truncated": false,
  "detail": [[...], ...],
  "skipped": null
}
```

| 字段 | 语义 |
|---|---|
| `i` | 事件序号（0 起，按事件时间顺序，与 `spec.events` 同序） |
| `street` / `event` | `ActionEvent` 全字段，**一个不省**：#2 的硬约束 |
| `bucket` / `spr_bucket` | 本事件命中的桶与 SPR 档（`bucket_of`），是 `M` 的行列坐标 |
| `alpha_k` | 本步实际指数（= `params.alpha_eff`） |
| `z_k` | 归一化常数 `Z_k`（`_normalize` 的返回值），**不可事后算**，必须存 |
| `mass_before` / `mass_after` | 本步前后**未归一化**的总权重（用于核对 `Z_k ≈ mass_after/mass_before`，含截断误差） |
| `width_before` / `width_after` | 本步前后的 `width_pct`（同 `final` 口径） |
| `grid` | 本步的 169 格占比（定义见 §4） |
| `dropped` | 本步被移出支撑集的 combo 计数：`dead`（撞公共牌/hero 牌，**v1.1 起每步都删**，见 3.3.4）、`zero`（似然为 0）、`floor`（低于 `support_floor·peak`） |
| `top` | 本步权重变化最大的 combo，见 3.3.3 |
| `cell_n` | **v1.1** 长度 169 的整数数组：本步每个 169 键的 combo 数，索引同 §4 的 `grid`。UI 用它和 `detail` 的每键条数对照，写「本格 12 个，这里取前 3」 |
| `detail_n` | **v1.1** `detail` 的候选总数（截断前） |
| `truncated` | **v1.1** `detail` 是否被 400 条上限截断 |
| `detail` | **v1.1** 本步组合明细，见 3.3.4 |
| `skipped` | 非 null 时该事件**未参与计算**（如牌面缺张、花式不匹配），值为跳过原因；此时 `z_k=1.0`、`mass_*` 不变 |

#### 3.3.3 `top[i]`

```json
{"c": "AhKh", "dw": -0.0012,
 "w0": 0.0021, "w1": 0.0009, "lr": -0.846,
 "l": 0.31, "l_pre": 0.63, "m": 1.0, "d": 1.0,
 "group": "semi", "made": "flush_draw", "draw": "flush_draw", "cut": null}
```

| 字段 | 语义 |
|---|---|
| `c` | combo 文本，`combo_text()` 规范序（高牌在前，花色 c<d<h<s；如 `AhKh`、`TdTc`）——与 `canon_combo` 一一对应 |
| `dw` | `w1 - w0`（本步归一化前权重之差） |
| `w0` / `w1` | 本步前后的归一化前权重（和不一定为 1，见 `mass_*`） |
| `lr` | `log(w1/w0)`；`w0==0` 时为 null |
| `l` / `l_pre` | 生效的 L（已夹到 `[min_likelihood, 1]`）与夹取前的原始 L——两者不同时说明被截断，是「不可精确归因」的信号 |
| `m` / `d` | 该 combo 的 M 乘数、街因子 D |
| `group` | 分组（`strong/medium/weak/air/bluff/semi/…`，取 `range_flow` 的实际分组名） |
| `made` / `draw` | 牌型与听牌标签，原样取自分类结果 |
| `cut` | 本步被移出的原因：`"dead"｜"zero"｜"floor"`；仍在支撑集则为 null。`"dead"` 表示该 combo 在这一步撞了 dead 牌被删（v1.1 起每步删） |

#### 3.3.4 `detail` / `cell_n`（v1.1，#29）

给看板「点 13×13 格子筛 combo」用。**取数口径**：本步归一化之后、dead 删除**之前**的支撑集——
被本步删掉的 dead combo 仍有一行、`cut:"dead"`，这是它最后一次出现，下一步起不再有。

`detail` 行是数组，按 `|dw|` 降序，**最多 400 条**（`params.detail_max`）；中间步 5 元、final 步 8 元：

```json
["AsKs", 0.0281, 0.0175, 0.0001, "dead"]
["KhQh", 0.0401, 0.0010, 0.5500, null, "air", "high_card", "none"]
```

| 位 | 字段 | 语义 |
|---|---|---|
| 0 | `c` | combo 文本，同 3.3.3 |
| 1 | `s` | 本步该 combo 的归一化份额（Σs = 1） |
| 2 | `dw` | 相对上一步的份额变化；step 0 为 null |
| 3 | `l` | 本步生效的 L（已夹到 `[min_likelihood, 1]`），与 `top[].l` 同源 |
| 4 | `cut` | `null` 仍在支撑集；`"dead"｜"zero"｜"floor"` 本步被移出 |
| 5–7 | `group`/`made`/`draw` | **仅 final 步**：牌型分类短码，同 `top[]` 的三个字段 |

- `cell_n[k]` = 本步该 169 键的 combo 数，与 `detail` 同一取数口径（**含** `cut:"dead"` 的行）；
- 体积（#29 实测，400 条/步）：h8（1326 combo × 4 步）36.8 KB、h4 25.9 KB、h104 1.5 KB；
  每行都带 `group/made/draw` 会涨到 67 KB，所以牌型分类只留在 final 步；
- 序列化：400 行 5 元数组 `json.dumps` 0.2 ms/步，与 169 格聚合同量级，仍在 +10 ms 预算内；
- **为什么不按需重算**：重算要求 14 天后代码与 `tables.sha256` 逐位相同，而 live 一直在换表；
  重算出来的数会和记录里的 `final` 对不上——那不是明细，是另一版推导。超 400 条只允许用
  `truncated: true` 显式截断，**不许静默少写**；整条记录还有 180 KB 体积闸与 6 ms 时间闸，
  触发时该步只留 `cell_n`、`detail: []`，步号写进记录级 `detail_omitted`。
  阶梯砍完仍超 180 KB 时再剥到骨架（detail 全清、每步只留 3 条 top、`grid`/`cell_n` 置空）
  照写，`warnings` 带 `trace_truncated`；只有骨架都装不下才丢。这一档不给新字段，
  `/api/meta.range_trace` 报 `record_too_big`，`stats()` 里 `truncated` / `dropped` 计数。

**条数与排序（grilling 4B）**：`top` 存 **lost 10 + gained 10**（按 |`dw`| 降序，各自取前 10），
其余 combo 聚合成一条 `{"others": {"n": 220, "sum_dw": -0.014}}` 追加在数组末尾。
`sum_dw` 为其余 combo 的 `dw` 之和，保证 `Σ dw ≈ mass_after - mass_before` 可对账。

### 3.5 `hero_equity`

```json
{"method": "mc", "samples_requested": 2000, "n_valid": 1998, "n_failed": 2,
 "seed": 17201, "cache_hit": false,
 "eq": 0.412, "sigma": 0.0110, "ci95": [0.390, 0.434],
 "now": {"behind_any": 0.580, "tie_any": 0.030},
 "showdown": {"rows": [{"cat": 5, "name": "Flush", "share": 0.12, "win": 0.92, "contrib": 0.1104}]},
 "vs_random": {"opponents": 3, "equity": 0.612, "beat_pct": 0.355, "tie_pct": 0.034,
               "beat_at_least_pct": 0.760, "method": "exact", "total": 1081},
 "draw": {"outs": 9, "rule_of_4_2": 0.36, "street": "flop"},
 "checks": [{"name": "river_equity_bounds", "ok": true, "lo": 0.390, "hi": 0.420, "eq": 0.412}]}
```

口径全部由 #5 定死（详见 `docs/equity-与牌力分解口径-v1.md`）：

- `eq` 就是提示词里那个范围权益，trace 与 prompt 必须逐位一致（同一 `HandVsRanges` 导出）；
  `method` 为 `"exact"` 时 `samples_*` / `seed` / `ci95` 全为 `null`。
- `sigma = sqrt(eq(1-eq)/n_valid)`，`ci95 = eq ± 1.96·sigma`；**不再有未标注 σ 的 ±x%，也不钳 0.1% 下限**。
- `now` 是**当前成手牌**的联合分布（精确、MC 联合抽样），`ahead` 由 `1-behind-tie` 推，不存。
- `showdown.rows` 13 行牌型，`Σ share = 1`、`Σ contrib = eq`——这是「equity 从哪来」的分解。
- `vs_random` 是 vs 一个随机对手的精确枚举（上界），`draw` 是四二法则完成率；三者基准不同，
  看板必须并排显示、各带基准标签，不得合成单一数字。
- `checks` 在河牌必跑：`eq ∈ [1-behind_any-tie_any, 1-behind_any]`，越界写 `ok:false` 并进 `warnings`。

## 4. `grid` 的编码

169 个 `float`，固定顺序，**和 = 1**（该步范围内的质量占比，不是绝对权重）：

```
index = row * 13 + col      row, col ∈ [0, 13)
row 0..12 → rank "A K Q J T 9 8 7 6 5 4 3 2"
col 0..12 → 同上
```

- `row == col` → 对子（`AA`…`22`）
- `row < col` → 同花（行是高牌），如 `row=0,col=1` → `AKs`
- `row > col` → 非同花（列变高牌），如 `row=1,col=0` → `AKo`

格值与 `key_of_combo` 的对应：`cell[row][col] = Σ_{combo ∈ 该键} w / Σ_{全部} w`。
对子格 6 个 combo、同花格 4 个、非同花格 12 个——**格值天然带组合数权重**，
所以两格颜色相同不代表两个 169 键的权重相同（#4 决定要不要另给「按 combo 平均」）。

> 为什么不做绝对权重（grilling 5A）：`mass_after` 每步都在变，绝对权重的
> 颜色跨步不可比；绝对水平由 `width_*` / `peak` / `rho` 三个标量表达。

## 5. join 与旁证

- `decision_key` 直接复用 `snapshot.decision_key`：`(hand_id, street, 全手动作条数, pot, to_call, hero 牌)`。与 states.jsonl 里 `advice` 记录的 `decision_key` 同构，看板按此 join 建议与推导。
- `facts["hand_ranges"]`（提示词里的范围块）**保持不变**，trace 是它的超集；两者用同一 `hand` + `seat` + `street` 对齐。不把 trace 塞进 states.jsonl（会撑大主日志，且 4 家 × 5 步 × 169 格 每手 ≈ 100KB）。
- `hand` + `seat` + `decision_key.actions` 三元组在 v1 内唯一；并发重复写由读端按 `ts` 保留最后一条。

## 6. 版本演进

- `v` 的**主版本**只在破坏性变更时升（字段改名、语义变化、删除）；新增字段走次版本，读端忽略未知字段。
- **v1.2（2026-11-14，看板消费端 #31）**——只加一个字段，读端不必改：
  - 新增 `final.grid[169]`（§3.3）；看板默认停在「留存量」，需要它画默认热图与逐格份额。
    取数与 `width_pct` 同一批 combo，所以「格值之和 = width_pct 的支撑集份额」。
  - `final` 仍不带 `detail`：最后一步的 `detail` 已在 `steps[-1]` 里，看板切到细则时用它。
  - v1.2 已实现（`poker_ocr/range_flow.py:final_summary`、`poker_ocr/range_trace.py:VERSION`）。
- **v1.1（2026-11-14，#29）**——只加字段 + 一处语义收紧，读端不必改：
  - 新增 `steps[].cell_n` / `detail_n` / `truncated` / `detail`（3.3.4）；
  - 新增 `hero.spr_source`，且 `hero.spr` 可为 `null`（§3.1）；
  - `dropped.dead` 与 `top[].cut:"dead"` 从「final 才删」改为**每步删**：存活 combo 的份额、
    `width_*`、`final.*` 与 v1 **逐位相同**（dead 只按固定倍数缩放存活 combo，归一化后份额不变）；
    变的只有 `z_k` 与 `mass_*` 的记账基数，以及 dead combo 在后续步不再出现。
  - v1.1 已实现（#30，2026-11-14）。两处按实现澄清：`detail_n` 是**截断前**候选总数
    （h8 记 1176、`detail` 实际 400 行）；`final.dropped` 只数支撑集（`w > 0`），
    早先被似然归零的不算收尾删除。
  - 消费方判版本用前缀 `live.range.v1`，不要精确等于。
- 读端对 `v` 的主版本不匹配时跳过该行并计数（`skipped_version`），不抛异常。
- 老记录没有 trace 的事实由 #8 的看板空态文案表达（「无推导记录」≠「算出来是空」）。

## 7. v1 明确不做

- 翻前记录（见 §2）。
- `Z_k` / `min_likelihood` 截断 / `support_floor` 截断的 **per-combo 精确归因**——#2 已证明这几类不可精确归因，v1 只存**现象**（`z_k`、`l_pre` vs `l`、`dropped.floor`），让看板能标注「此处有损」。
- **逐 combo 全量落盘**：`detail` 上限 400 条/步，超出按 `|dw|` 截断并写 `truncated:true`（#29 Q6/Q7）。
  按 169 键存 `cell_n` 是保底，格子级永不截断。
- 在线调参、反事实批量重算（只做「换一版表重算 + diff」，脚本在 #9 原型里验）。
- 写入 replay-console / MySQL。

## 8. 字段总览（给实现者的清单）

```
v status ts session hand street decision_key{hand,street,actions,pot,to_call,hero[]}
board dead[] hero{pos,stack,pot,to_call,spr,spr_source}
params{alpha_in,alpha_eff,n_events,n_applied,support_floor,min_likelihood,event_model,tables{profile,variant,sha256},prompt_version,advice_version}
seats[]{seat,pos,archetype,path,mode,bucket,
        prior{id,version,source,width_pct,weighted_width,combo_count,grid[169]},
        steps[]{i,street,event{actor,action,amount,pot_before,spr_before},bucket,spr_bucket,alpha_k,z_k,
                mass_before,mass_after,width_before,width_after,grid[169],
                dropped{dead,zero,floor},top[]{c,dw,w0,w1,lr,l,l_pre,m,d,group,made,draw,cut},others{n,sum_dw},
                cell_n[169],detail_n,truncated,detail[][]{c,s,dw,l,cut[,group,made,draw]},skipped},
        final{width_pct,weighted_width,effective_width_pct,peak,rho,combo_count,grid[169]},
        equity{method,removed,…}}
omitted hero_equity{method,samples,cache_hit,seed,…} warnings[]
```
