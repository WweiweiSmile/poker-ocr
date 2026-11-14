# 范围推导 trace 采集与落盘规格（v1）

> 出处：map #1「对手范围与牌力推导的可观测化（日志 + 可视化复现）」的决策票
> #6「采集侵入面、性能预算与落盘 envelope」。
>
> 字段本身以 [范围推导-trace-字段契约-v1.md](范围推导-trace-字段契约-v1.md)（#3）为准；
> 本文件补它留给 #6 的四件事：**怎么取、命中缓存怎么办、性能预算、文件怎么轮转与关闭**。
> 13×13 聚合归 #4，equity 分解归 #5，看板摆法归 #8，换表重算归 #9。

## 0. 一句话

trace 与范围计算**同路产出**（不事后补），默认路径输出逐位不变；只在决策点算、
增量不超过 10 ms；一条决策一行 JSON，按本地日切、留 14 天、总量 1 GB。

## 1. 采集点

### 1.1 代码形状

`poker_ocr/range_flow.py` 里把现有 `build_range` 的循环抽成内核，两个公开薄壳：

```python
def _build_core(prior, events, *, alpha=None, archetype="unknown", dead=(),
                support_floor=None, trace: TraceRecorder | None = None
                ) -> tuple[Range | None, TraceRecord | None]: ...

def build_range(prior, events, *, alpha=None, archetype="unknown", dead=(),
                support_floor=None) -> Range | None:
    return _build_core(prior, events, alpha=alpha, archetype=archetype,
                       dead=dead, support_floor=support_floor, trace=None)[0]

def build_range_traced(prior, events, *, alpha=None, archetype="unknown", dead=(),
                       support_floor=None) -> tuple[Range | None, TraceRecord | None]:
    return _build_core(..., trace=TraceRecorder())
```

- **逐位不变靠同一段循环**：`trace=None` 时循环里只有每步一次判空，不做任何
  额外累加、不改归一化顺序、不改 `dead` 的删除时机（仍在全部事件之后）。
- 不给 `build_range` 加参数：调用点（`hand_vs_ranges._lookup`）语义明确，
  默认签名保持原样，避免每个现有调用方都背一个 `trace=None`。
- 不复制第二份 traced 循环：两份迟早漂移，归因就会对不上。

### 1.2 调用点

- `hand_vs_ranges._lookup` 里 `use_events and spec.events` 的分支：trace 开启时
  改调 `build_range_traced`，否则维持 `build_range`。
- `board_range` 回退（`built is None` 或没有 events）照常出 `path:"board_range"`，
  `steps:[]`，不补造事件。
- 只在**决策点**算：`live.py:1885-1890` 的 `build_situation(point, ...)` 本来就只在
  `point` 非空时调用一次；绝不挂到 `frame`/`cards` 的每帧路径上。
- `status` 与 `reason` 全部由 `_lookup` 的早退分支决定（契约 §2）；翻前继续零记录。

## 2. 缓存策略

| 位置 | 策略 |
|---|---|
| `range_flow._BUILD_CACHE` | trace 路径**不读也不写**。键不含表 profile/variant，热切 `STRATEGY_TABLES_PROFILE` 后可能拿到旧表结果；trace 必须自证口径，所以宁可不命中。重算成本 3.0 ms/家（实测，4 事件、1326 combo）。 |
| `hand_vs_ranges._CACHE` | 保留原样。`HandVsRanges` 增两个带默认值的字段：`seed: int | None = None`、`cache_hit: bool = False`；命中时返回 `dataclasses.replace(hit, cache_hit=True)`，**不重跑 MC**（1500 样本约 70 ms）。 |
| MC 的 `samples` | 命中与未命中都写实际参与数 `samples`（= `valid`），并另写 `samples_requested`；两者不等时是牌冲突剔除，不是缓存。 |
| 表身份 | `params.tables` 写 `profile` / `variant` / `sha256`（`poker_ocr/tables/range_flow.json` 规范化 JSON 的 sha256 前 16 位）。读端重算前先比这个，不假设「同版本代码 = 同口径」。 |
| 计数口径 | `params.n_events = len(events)`，`n_applied` = 实际参与乘数的事件数（被跳过的不算）；`alpha_eff` 与二者同写，重算才对得上。 |

## 3. 性能预算

**硬顶：每次出建议 +10 ms。** 实测构成（M 系列、1326 combo、4 家 × 5 步上限）：

| 项 | 数 |
|---|---|
| `build_range` 4 events（无 trace） | 3.0 ms（p90 3.4） |
| 单步 trace：169 格聚合 + top10 lost/gained + 舍入 | 0.28 ms |
| 单步 `detail`：按 \|dw\| 取前 400 条 + 编码（v1.1） | 0.57 ms（200 条 0.37 / 100 条 0.24） |
| 8 步合计（4 家 × 2 步，常见） | 6.8 ms |
| 20 步合计（4 家 × 5 步，上限） | 17.0 ms ← **超预算，必须走降级** |
| 整行 `json.dumps`（110 KB） | 1.2 ms |

- 不存 1326 逐 combo 乘数：每步存 `grid[169]` + `cell_n[169]` + `top`（lost 10 / gained 10 / `others`）
  + `detail`（按 |dw| 取前 400 条，v1.1 #29）；1326 条的完整乘数仍只在内存里存在，
  重算脚本按需展开（但**不保证**与当时逐位一致，见契约 §3.3.4）。
- top-N 与 grid 在同一次遍历里完成，不额外排序两遍。
- 超预算降级顺序（累加计时，`perf_counter` 包住 trace 段）：
  1. `detail` 400 → 200 → 100 条/步；
  2. 累计耗时 > 6 ms 时，后续步**只写** `cell_n`，`detail: []`，步号记进
     记录级 `detail_omitted`（`truncated` 仍只表示「超单步 400 条上限」）；
  3. 体积闸（§4）触发时 `top` 10 → 5、`detail` 逐级降到 0；
     **时间闸不砍 top**——实测砍 top 省不到 0.1 ms，原先写的「>10 ms 时 top 5」
     并入体积闸最后一级执行；
  4. 仍超则本条 `warnings` 追加 `trace_truncated`。
  全程照常出建议，不阻塞、不回滚、不重算。
- 写盘耗时不计入这 10 ms（异步/尾部追加），但写盘失败必须 fail open。

## 4. 文件、轮转、保留、预算

| 项 | 约定 |
|---|---|
| 路径 | `logs/live.range.jsonl`（相对 live 进程 cwd；`--range-trace-out` 可覆盖） |
| 轮转 | 首次跨**本地日**写入时切到 `live.range.jsonl.YYYYMMDD`；只在 trace 写入器内部判断，不改 `JsonlWriter`（其它日志共用的写入器保持无轮转） |
| 保留 | 14 天 |
| 总量 | 1 GB；日切时按 mtime 删最旧的 `live.range.jsonl.*`，不动其它日志文件 |
| 日上限 | 200 MB；触顶后本次会话停写 trace，看板显示「已停写（超日预算）」，出建议不受影响 |
| 一致性 | 单行先整体编码再一次 `write`（`O_APPEND`）；110 KB 远超 `PIPE_BUF`，按契约 §1 走「整行写入 + fsync」路径，半行由读端丢弃并计数 |
| 失败 | 权限/磁盘满/路径不存在 → 捕获、计数、看板显示「写盘失败」，不抛、不重试风暴（每会话最多告警一次） |

### 磁盘预算算式

```
每天条数 = 决策/小时 × 时长(h)
每天字节 = 每天条数 × 单条字节
占用     = 每天字节 × 保留天数
```

按 **100 决策/h × 4 h = 400 条/天** 规划（实测长会话 133 决策/h，仍有余量）：

| 档 | 单条 | 每天 | 14 天 |
|---|---|---|---|
| 典型（2–3 家 × 3 步，detail ≤200 条/步） | 60 KB | 24 MB | 336 MB |
| 上限（4 家 × 5 步，detail 400 条/步，实测 ~9 KB/步） | 110 KB | 44 MB | 616 MB |
| 最坏（20 步全 400 条 + 完整 top，体积闸 180 KB 兜底） | 180 KB | 72 MB | 1.0 GB |

结论：1 GB 总量在「最坏 × 14 天」正好触顶，自动删最旧即可；日上限 200 MB 对应
最坏约 2 700 条 ≈ 6.8 h 连续最坏输出，正常不会触发。

**体积闸（v1.1）**：单条编码前先估长度，超 **180 KB** 就从最后一步往前丢 `detail`
（保留 `cell_n`），写 `truncated: true` + `detail_omitted`。180 KB 是 1 GB /（400 条/天 × 14 天）
反推的上限，**不是**可以越过的软目标。

## 5. 开关与降级显示

- 默认开；`--no-range-trace` 关闭本次进程的 trace。
- `/api/meta` 增 `range_trace`，取值 `on` / `off` / `budget_exceeded` / `write_failed`；
  看板诊断页据此显示「未开启」「已停写（超日预算）」「写盘失败」。
- 关闭时：诊断页明确写「未开启（本次会话）」，**保留现有 facts 旧视图**，
  不显示空推导、不伪造 0%。
- 关闭时 `hand_vs_ranges` 的输出必须逐位不变：trace 只读中间量，不参与计算。

## 6. 老记录与重算边界

| 情况 | 表现 |
|---|---|
| 记录早于 v1 / 当时 `--no-range-trace` | UI 标「无推导（记录早于 trace / 未开启）」，不自动回填 |
| 同一 `decision_key` 有重复行 | 读端按 `ts` 保留最后一条（契约 §5） |
| 想用当前表重算 | 归 #9：读原记录 → 用当前表重算 → 产物写 `logs/range.replay/<session>/<hand>.jsonl`，UI 标「当前版本重算，非当时口径」；**不写回** `live.range.jsonl` |

## 7. 验收

- [ ] `trace=None` 与 `trace` 开启两条路径，同一 `(prior, events, alpha, archetype, dead, support_floor)` 产出的 `Range` 逐字段相等。
- [ ] `_BUILD_CACHE` 在 trace 路径上既不命中也不写入；关闭 trace 后缓存命中数与改动前一致。
- [ ] `hand_vs_ranges._CACHE` 命中返回 `cache_hit=True` 且 `seed` 非空，`samples` 不改。
- [ ] 一条 trace 行能通过 v1 契约的字段校验（含 `params.tables.sha256`、`n_events`/`n_applied`）。
- [ ] 打点：单次决策的 trace 增量 < 10 ms（20 步场景下必须触发降级并仍在 10 ms 内）；一条 4 家 × 3 步（detail 400/步）的记录 ≤ 110 KB，单条硬顶 180 KB。
- [ ] 跨本地日首次写入切出 `.YYYYMMDD`；14 天/1 GB 的清理只动 trace 文件。
- [ ] `--no-range-trace` 时 `/api/meta.range_trace == "off"`，facts 视图不变。

## 8. 不做

- 每帧 trace、翻前 trace、1326 逐 combo **乘数**落盘（400 条 `detail` 是明细表，不是全量乘数）。
- 命中缓存时反推中间量（做不到，也不该做）。
- 把 trace 写进 `replay-console` / MySQL。
- 在线调参与反事实批量重算（#9 的脚本负责）。


## 9. 实现与对账（2026-11-14）

### 9.1 落在哪

| 文件 | 改动 |
|---|---|
| `poker_ocr/range_flow.py` | `build_range` 段拆成 `_build_core(..., trace=None)` + 两个薄壳；新增 `build_range_traced`、`prior_payload`、`final_summary`、`TRACE_DETAIL_MAX=400`。缓存键与语义不变，trace 路径不读不写 `_BUILD_CACHE`。 |
| `poker_ocr/range_trace.py`（新） | `build_record` / `build_skipped` / `downgrade` / `TraceWriter`（日切、14 天、1 GB、200 MB/日、≥4096 字节 fsync、fail open）、模块级 `configure/enabled/status/emit/reset/set_session`。 |
| `poker_ocr/ranges.py` | 新增 `key_index(key) -> int`（169 平铺下标：AA=0、AKs=1、AKo=13、22=168），`grid` 与 `cell_n` 共用。 |
| `poker_ocr/hand_vs_ranges.py` | `HandVsRanges` 补 `seed` / `samples_requested` / `n_failed` / `cache_hit`；缓存命中 `replace(hit, cache_hit=True)` 不重跑 MC；`_lookup` 接 trace（含 skipped 早退）与座位级 `equity`。 |
| `poker_ocr/live.py` | `--range-trace-out`（默认 `logs/live.range.jsonl`）与 `--no-range-trace`；启动时 configure + set_session；`/api/meta.range_trace` 输出 `on/off/budget_exceeded/write_failed`。 |
| `tests/test_range_trace.py`（新） | 10 组：下标、grid 和、逐位相等、detail 形状/截断、时间闸、不碰 `_BUILD_CACHE`、记录形状、降级阶梯、轮转与配额、fail open、开关。 |

### 9.2 逐位不变

同一 `(prior, events, alpha, archetype, dead, support_floor)`，HEAD 版 `build_range` 与
新 `build_range` / `build_range_traced` 三者在 h104、h4、h8 上 `combo_weights`、
`width_pct`、`weighted_width`、`combo_count`、`peak`、`rho` 全部逐位相等
（`tests/test_range_trace.py` 固化为断言）。做法：循环仍是同一段，
`trace=None` 时只多一次判空；dead 仍在全部事件之后统一删，留痕视图里的
「每步删」只改 `removed` 集合，不动 `weights`。

### 9.3 三手真实记录对账

样本 `hands/1023.hands.json`（h104/h4/h8），`hand_index` 取数字，trace 写临时目录：

| 牌例 | 事件 | 支撑 combo | 步内 detail | 整行字节 | trace 耗时 |
|---|---|---|---|---|---|
| h104 flop（midR） | 1（raise） | 33 | 47 | 11,303 | 0.80 ms |
| h4 turn（topR） | 2（call→call） | 191 | 229 / 191 | 35,306 | 3.56 ms |
| h8 flop（lowL，5 路） | 1（bet） | 1,081 | 1,176 → 400（`truncated`） | 43,530 | 8.35 ms |

- 三手 `final.width_pct` 与 `build_range` 逐值一致（2.4887 / 14.4042 / 81.5234），整行均 ≤110 KB，无降级。
- 无事件座位照旧走 `board_range`（h104 upR 2.87%、h8 另三家 22.55/25.19/29.79%），`steps: []`，不补造事件。
- 翻前不写记录；缓存命中不重复写同一决策的行。
- **h8 的 8.35 ms 是实测最坏值**：1081 支撑 × 400 条 detail。时间闸在步前判定，
  首步 `trace_ms=0` 必然放行，所以单步重活会吃掉 8.35 ms；仍低于 10 ms 硬顶，
  多步场景由闸把后续步记进 `detail_omitted`。

### 9.4 与文本的两处澄清

1. `detail_n` 是**截断前**候选数（h8 记 1176、`detail` 实际 400 行），与契约 §3.3.4 一致，不是笔误。
2. `final.dropped` 只数支撑集（`w > 0`）：早先被似然归零的 combo 不算「收尾删掉的」，
   这样 `final.dropped.dead` 与同一步 `steps[].dropped.dead` 对得上（h104 两边都是 14）。

### 9.5 测试

`nv tests/run_all.py` → **58 个套件全部通过**；新增 `tests/test_range_trace.py` 10 组，
`tests/test_hand_vs_ranges.py` 的缓存用例改为比较 `replace(a, cache_hit=False)`。

