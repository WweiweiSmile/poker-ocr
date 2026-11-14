# equity 与牌力数字的分解口径 v1

> 出处：map #1 的决策票 #5「equity 与牌力数字的分解口径、MC 噪声怎么呈现」。
> 上游：`docs/范围推导-trace-字段契约-v1.md` §3.3（逐座位 `equity`）、§3.5（`hero_equity` 容器）。
> 本文件只做**加法**：把 §3.3/§3.5 里留白的内部口径填满，不改已有字段语义。

## 0. 先说病因：提示词里有三个「equity」，基准各不相同

| 数 | 代码 | 基准 | 方法 | 语义 |
|---|---|---|---|---|
| `ground_truth.rank.equity_pct` | `coach.py:375` `(win + tie/2)/total` | **一个随机对手** | 精确枚举（`rank_hands`，47 张牌 C(47,2)） | 上界；对手既然进池，范围只会更强 |
| `hand_ranges.equity` | `hand_vs_ranges._monte_carlo` `:407-474` | **建模范围 × N 家** | MC，平局按 `1/(winners+1)` 分池 | 现实口径；只算摊牌，不含弃牌 |
| `sit.equity_pct`（听牌） | `ground_truth.DrawInfo`，`hand_facts.py:326` | **完成率**（四二法则） | outs×4/×2 | 不是胜率，是「听牌做成」的概率 |

`fold_equity.range_equity`（`fold_equity.py:610/695`）只是把第二个数换个标签，**不是第四个数**。

复盘时只看到一个「equity」，就永远解释不了「模型为什么说 41% 而我觉得该弃」——因为
两个人在说不同的数。所以看板必须**三个都显示、各带基准**，绝不合成一个。

另外两个正交的量（都在 `hand_vs_ranges`）：

- `behind_any` / `tie_any`：**现在**的成手牌，精确、按范围权重、逐 combo 算（`_exact_edge` `:287-354`）；
  `ahead = 1 - behind - tie`（**不单独存**，避免三数和不为 1）。
- `equity`：**打到河牌**的摊牌权益，MC、多人联合抽样。

## 1. `hero_equity` 的完整结构（v1 加法）

```json
{
  "method": "mc",
  "samples_requested": 2000,
  "n_valid": 1998,
  "n_failed": 2,
  "seed": 17201,
  "cache_hit": false,
  "eq": 0.412,
  "sigma": 0.0110,
  "ci95": [0.390, 0.434],

  "now": {"behind_any": 0.580, "tie_any": 0.030},

  "showdown": {
    "rows": [
      {"cat": 5, "name": "Flush",   "share": 0.120, "win": 0.920, "contrib": 0.1104},
      {"cat": 4, "name": "Straight","share": 0.050, "win": 0.850, "contrib": 0.0425},
      {"cat": 2, "name": "Two pair","share": 0.080, "win": 0.840, "contrib": 0.0672},
      {"cat": 1, "name": "One pair","share": 0.450, "win": 0.420, "contrib": 0.1890},
      {"cat": 0, "name": "High card","share": 0.300, "win": 0.010, "contrib": 0.0030}
    ]
  },

  "vs_random": {
    "opponents": 3,
    "equity": 0.612, "beat_pct": 0.355, "tie_pct": 0.034,
    "beat_at_least_pct": 0.760,
    "method": "exact", "total": 1081
  },

  "draw": {"outs": 9, "rule_of_4_2": 0.36, "street": "flop"},

  "checks": [{"name": "river_equity_bounds", "ok": true, "lo": 0.390, "hi": 0.420, "eq": 0.412}]
}
```

字段规则：

- `eq` = `hand_ranges.equity`，**提示词里那个数**，trace 与 prompt 必须逐位一致（同一个 `HandVsRanges` 对象导出）。
- `sigma = sqrt(eq(1-eq)/n_valid)`；`ci95 = eq ± 1.96·sigma`（夹在 `[0,1]`）。
  **不再输出未标注 σ 的 `±x%`**，也**去掉** `:134` 那个 `max(ci, 0.1)` 钳位。
- `n_valid` 是真正参与统计的样本数（= 现有 `samples` 字段），`n_failed` 是撞牌被整组拒绝的组数；
  `samples_requested` 是请求值（受 `HAND_VS_RANGES_SAMPLES` 与 ≥3 家降到 1500 影响）。
- `showdown.rows`：13 个牌型全列（按 `coach.CATEGORY_NAMES_EN` 下标），**`Σ share = 1`、`Σ contrib = eq`**。
  `win = contrib/share`（该牌型下平均能拿到多少底池，含平分）。
- `vs_random`：`ground_truth.rank` 的四个数原样搬运；`beat_at_least_pct = 1-(1-beat_pct)^n`（`coach.py:395`）。
- `draw`：只有 `sit.equity_pct` 存在时才有；它只对听牌有意义（成牌的 outs 是「还能不能改进」，不是胜率）。
- `checks`：**河牌必跑**。河牌不发牌，`equity` 必须落在
  `[1 - behind_any - tie_any, 1 - behind_any]`（严格全胜 ~ 含平分上限）；越界记 `ok:false` 并进 `warnings`。
  翻牌/转牌不跑（发牌会改变分布）。

## 2. `seats[*].equity` 的完整结构（逐对手，精确）

```json
{"method": "exact", "removed": 11,
 "behind": 0.580, "tie": 0.030,
 "beaten_by": [{"name": "Flush", "share": 0.21, "combos": 34, "examples": ["AsKs×1", "QsJs×1"]}],
 "ahead_groups": [{"name": "One pair", "share": 0.09}]}
```

- `removed` = 因 hero 牌 + 公共牌被剔除的 combo 数（现有 `combos_after_removal` 的补数）。
- `behind` / `tie` 是**该对手的边缘分布**（marginal），已经把权重归一化；`ahead` 由 `1-behind-tie` 推。
- `beaten_by` / `ahead_groups` 直接取 `_exact_edge` 已算好的分组（`:324-334`），**不再重算**。
- 注意：边缘分布忽略了两个对手之间的互相撞牌；`behind_any`（联合、MC）才是全桌口径。
  两者可以差几个点，看板要并排但**不要互相校验**。

## 3. 噪声怎么呈现

1. 每个 MC 数都配 `ci95`；`method:"exact"` 时 `ci95` 为 `null`（精确就没有误差）。
2. 区间画成细横杆，鼠标悬浮显示 `n_valid / n_failed / seed`。
3. **相邻步差值判定**：`σ_Δ = sqrt(σ₁² + σ₂²)`，若 `|Δ| < 1.96·σ_Δ` 标 `within noise`（灰带，不给趋势措辞）。
   两侧都是 `exact` 时永不标噪声。
4. 页面上不出现「±0.1%」这种下限钳位——精度不够就显示「区间跨 0」或干脆只给整数百分比。

## 4. 重放

- `seed = crc32(hero|board|samples_requested|rnd:N|seat:range:width)`（`_seed` `:372-378`）；
  同输入 + 同 seed → **逐样本一致**。trace 存 `seed`、`samples_requested`、`n_valid`、`n_failed`，
  重放时 `n_valid` 必须相同，否则说明范围/牌面/样本数变了，读端标「输入已变」。
- `cache_hit` 只影响耗时，不影响数值（`_CACHE` 命中的是同一个 `HandVsRanges`）。

## 5. 工作示例（实现自测）

`eq=0.412, n_valid=2000`：

- `σ = sqrt(0.412·0.588/2000) = 0.0110`，`ci95 = [0.390, 0.434]`。
- `showdown` 五行：`Σ contrib = 0.1104+0.0425+0.0672+0.1890+0.0030 = 0.4121 ≈ eq`（差值是四舍五入）。
- 相邻两步 `0.412 → 0.441`：`σ_Δ = sqrt(0.0110²+0.0113²) = 0.0158`，`1.96σ_Δ = 0.031`，
  `|Δ| = 0.029 < 0.031` → 标 `within noise`（差 2.9 个点仍可能是抖动）。
- 河牌断言：`behind_any=0.58, tie_any=0.03` → 允许区间 `[0.39, 0.42]`，`eq=0.412` 通过。

**自测断言**：`Σ share == 1`；`|Σ contrib - eq| < 1e-9`；`ci95` 含 `eq`；`n_valid + n_failed == samples_requested`。

## 6. 实现注记（改哪里，成本）

| 位置 | 改动 | 成本 |
|---|---|---|
| `hand_vs_ranges._monte_carlo` `:407-474` | 每样本多记 `hero_runout[0]` 与 `gain` 两个计数器 | 每样本一次整数加法，可忽略 |
| `HandVsRanges` `:100-115` | 加 `showdown` 元组；`equity_ci` 改为 `sigma`，新增 `ci95` 属性 | 纯数据 |
| `render()` `:133-136` | 用 `ci95`，去 `max(...,0.1)` | 一行 |
| `advice._facts_snapshot` `:853-868` | **不动**（提示词口径不变，trace 是超集） | 零 |
| trace writer（#6 的采集点） | 从同一个 `HandVsRanges` + `sit.ground_truth` 导出 | 见 #6 |

## 7. 明确不做

- **逐对手 equity**（「去掉他还剩多少」）：多人池里要重算联合分布，得到的数与总 equity 不可加；
  真要问就用反事实 diff（换一版范围重算整条链），不塞进单条 trace。
- **精确多人 equity 枚举**：对手 combo 组合爆炸；河牌已用 `[1-behind-tie, 1-behind]` 断言兜底。
- **把 `fold_equity` 的 EV 折进 equity 块**：EV 依赖下注尺寸，属于 #7/看板建议侧，独立成块。
- 跨手牌聚合 equity 分布（批量报表，已在 map 的 Out of scope）。
