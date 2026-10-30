> 2026-10-30.10 更新：live 的 `build_situation(include_strength=False)` 不再
> 预计算/渲染 `hand_strength` 的「大/中/小」；它只服务离线 `audit_advice_tags`
> （需要时懒计算）。本文件保留当时的设计与字段说明，作为历史/审计口径参考。

# 实时牌力解析 · 接入 AI 建议 —— 设计文档

> 本文描述如何把「实时牌力解析」（这手牌当前牌面**大不大、多大、是价值还是听牌**）
> 算成一个结构化结论，并作为事实接入 AI 建议提示词。
>
> 它建立在前一版 [`实时牌面范围与手牌强度-设计文档.md`](./实时牌面范围与手牌强度-设计文档.md)
> 之上：那一版负责把 `behind_now / equity / beaten_by` 算出来，这一版负责
> **把这些数字翻译成「大 / 中 / 小 + 程度 + 该当价值还是听牌」** 的一行结论，
> 让模型不再自己脑补。

---

## 0. 结论

**在 `hand_vs_ranges` 之上再加一层纯判定，输出一个 `HandStrengthRead`，**
渲染成一行英文事实 `Hand-strength read: ...` 注入提示词。

理由：

1. 模型拿到的 `behind_now` / `equity` / `beaten_by` 是三个正交的数，
   实测它容易混用（把“现在落后”当成“牌弱”，把“听牌 equity 高”当成“大牌”）。
2. “大不大”和“权益多强”是两个问题，必须分开，再合成一个结论。
3. 判定逻辑是纯规则、可单测、可回放校准，不需要新算法。

---

## 1. 目标与非目标

### 目标

- 给定决策点，输出一个**牌力档位 + 程度 + 读法**：
  - `size`：当前成手牌大小（very_big / big / medium / small / very_small）；
  - `equity_tier`：权益档次（strong / above_average / neutral / weak）；
  - `kind`：价值牌 / 听牌 / 弱牌（空气）；
  - `summary_en`：英文一句话结论（**legacy offline only**）。
- 中文 lean 由 `advice.render_hand_strength_zh` 把 `kind/size/behind_any/equity`
  渲染成「牌力 / 对范围权益 / 当前成牌落后至少一家 / 多人池」等中文事实；
  听牌永远按「听牌」渲染，不写 `VERY SMALL`。

### 非目标

- 不重算 `behind_now / equity`——那由 `hand_vs_ranges` / `ground_truth` 负责；
- 不输出频率表、不做范围构建、不碰 solver；
- 不替代 `Value directive / Math gate`（两者已从 live 删除，只留离线审计）——
  它是**牌力摘要**，不是赔率/价值指令。

---

## 2. 现有组件（全部复用，不新增算法）

| 组件 | 位置 | 给牌力解析提供什么 |
|---|---|---|
| 牌型/分档 | `coach.read_hand`（`coach.py:426` 起） | `tier`（强成牌/超对/顶对强踢/顶对弱踢/中底对/强听/弱听/空气/打牌面）、`label_en` |
| 精确坚果/排名 | `ground_truth.GroundTruth`（`ground_truth.py:83`、`:208`） | `made.name`、`hero_is_nuts`、`rank.beaten_by`、`rank.beat_pct` |
| 手牌 vs 范围 | `hand_vs_ranges.HandVsRanges`（`hand_vs_ranges.py:78`） | `behind_any`、`tie_any`、`equity`、`opponents[].behind/beaten_by` |
| 注入点 | `advice.py:704` `Situation`、`:861` `build_situation`、`:1795` `render_hand_facts` | 挂载与渲染 |

关键：**计算引擎不需要改**，`hand_vs_ranges` 已经给出全部数字，这一层只做判定。

---

## 3. 判定框架：两个正交维度 + 一个绝对强度

“牌大不大”不能只看 equity，也不能只看当前成手牌。拆成三个量，再合成：

### 维度 A —— 现在大不大（当前成手牌）

用 `behind_any`（多人池口径：至少一家当前成手牌比我大的比例）。

- `behind_any` 低 → 现在大概率是最大牌；
- `behind_any` 高 → 现在已落后（但可能是听牌，见维度 B）。

### 维度 B —— 权益多强（打到河牌）

用 `equity` 与“平均分池份额 `1/N`”的比值：

```text
N = 对手数 + 1
avg = 1 / N
ratio = equity / avg
```

- `ratio ≥ 2.0` → strong（强权益）
- `1.3 ≤ ratio < 2.0` → above_average（中上）
- `0.85 ≤ ratio < 1.3` → neutral（中性）
- `ratio < 0.85` → weak（弱权益）

> 说明：多人池“大于 50%”不是大牌门槛；超过平均线 `1/N` 只是“比平均强”，
> 到 **约 2× 平均线** 才算真正的大牌（见第 4 节）。

### 维度 C —— 绝对强度（这手到底是什么）

用 `hand_tier` + `made.name` + `hero_is_nuts` + `beaten_by`：

- 两对/set/顺子/同花/葫芦 + `hero_is_nuts` → 绝对很大；
- 两对/set 但 `beaten_by` 含更高牌型 → 大但被压制；
- 顶对/超对 → 中到大，取决于踢脚与牌面；
- 听牌 → 不是成牌，按 outs/赔率判断；
- 空气/打牌面 → 小。

---

## 4. 分档规则

### 4.1 `behind_any` → 当前位置分档（默认阈值）

| `behind_any` | 当前位置 |
|---|---:|
| < 5% | 基本最大（near-best） |
| 5–15% | 很大 |
| 15–35% | 领先 / 大 |
| 35–60% | 中等 / 偏落后 |
| > 60% | 明显落后 |

### 4.2 `equity ratio` → 权益档次

| ratio = equity / (1/N) | 权益档次 |
|---|---:|
| ≥ 2.0 | strong |
| 1.3–2.0 | above_average |
| 0.85–1.3 | neutral |
| < 0.85 | weak |

### 4.3 组合成 `size + kind`

| 牌型（tier/made） | behind_any | equity tier | 综合判定 |
|---|---|---|---|
| 两对+ / set / 顺子+，且 hero_is_nuts | 低 | strong | **VERY BIG · value**（坚果/近坚果） |
| 两对+ / set / 顺子+ | 低 | strong/above | **BIG · value** |
| 两对+ / set / 顺子+，但 beaten_by 含更高牌型 | 中/高 | neutral/weak | **BIG made but dominated**（有牌，但别打光） |
| 超对 / 顶对强踢 | 低 | above/strong | **BIG · value**（单挑/少路） |
| 超对 / 顶对强踢 | 高 | weak | **MEDIUM · ahead pair, multiway behind** |
| 顶对弱踢 / 中底对 | — | — | **MEDIUM**（薄价值） |
| 强听 / 弱听 | 高 | 中/weak | **DRAW**（看 outs/赔率，不看 behind） |
| 空气 / 打牌面 | 高 | weak | **SMALL / WEAK** |

> 规则优先级：先判 `kind`（听牌 / 打牌面 / 成牌），再判 `size`，最后叠加 `equity_tier`。
> 听牌永远标 `DRAW`，不能因为 equity 高而标成 `BIG made hand`。

---

## 5. 数据结构

```python
# poker_ocr/advice.py（建议位置：Situation 附近）

@dataclass(frozen=True)
class HandStrengthRead:
    size: str                 # very_big | big | medium | small | very_small
    equity_tier: str          # strong | above_average | neutral | weak
    kind: str                 # value | draw | weak | board_play
    made_name: str            # "Two pair" / "Straight" / "Top pair K kicker" ...
    nuts: bool                # 是否坚果
    behind_any: float         # 0..1，多人池“至少一家当前领先”比例
    equity: float             # 0..1
    avg: float                # 1 / (对手数 + 1)
    n: int                    # 对手数

    def summary_en(self) -> str:
        """给模型的单行事实。"""
```

`Situation` 增加一个字段：

```python
# poker_ocr/advice.py Situation
hand_ranges: "object | None" = None          # 已有
hand_strength: "HandStrengthRead | None" = None   # 新增
```

---

## 6. 接入点

### 6.1 计算（`build_situation`）

在 `advice.py:861` 已经算好 `hand_ranges` 之后，紧接着：

```python
# build_situation 内，hand_ranges 非 None 时
from .hand_strength import compute_hand_strength   # 或放 advice.py 内部
sit = replace(sit, hand_ranges=hand_ranges, warnings=merged,
              hand_strength=compute_hand_strength(sit))
```

`compute_hand_strength(sit)` 的输入全部来自已有字段：

```python
def compute_hand_strength(sit) -> HandStrengthRead | None:
    hr = sit.hand_ranges
    if hr is None or not hr.opponents:
        return None
    n = len(hr.opponents)
    avg = 1.0 / (n + 1)
    ratio = hr.equity / avg if avg > 0 else 0.0
    # ... 按第 4 节规则填 size / equity_tier / kind / made_name / nuts ...
    return HandStrengthRead(...)
```

### 6.2 渲染（`render_hand_facts`）

在 `advice.py:1795` 渲染完 `## Hand vs ranges` 之后、`board_eq` 之前插入一行：

```python
if sit.hand_strength is not None:
    lines.append(f"Hand-strength read: {sit.hand_strength.summary_en()}")
```

位置刻意放在 `ground_truth` 之后、`board_eq` 之前：它是**主答案**，牌面结构是背景。

### 6.3 提示词口径规则（`_LEAN_SYSTEM`）

在现有 `## Hand vs ranges` 段落追加一句，防止模型把三个数再混一次：

```text
- «Hand-strength read» is the synthesized verdict: "behind now" = current made hand,
  "equity vs field" = by the river, "vs N-way average" = the fair-share baseline.
  A draw is always labeled DRAW even if its equity is high; never quote equity alone
  as "my hand is big".
```

---

## 7. 输出示例（英文，给模型）

```text
# 大牌（两对）
Hand-strength read: BIG Two pair; behind now 4%, equity 80% vs 4-way avg 25% (strong) — value hand.

# 顶对在多路被稀释（现在领先，但权益弱）
Hand-strength read: MEDIUM Top pair K kicker; behind now 72% (multiway), equity 18% vs 3-way avg 33% (weak) — ahead pair, multiway behind; do not overplay.

# 听牌（现在落后但有权益）
Hand-strength read: DRAW Flush draw; behind now 99%, equity 38% vs 2-way avg 50% (weak) — a draw, judge by outs and price, not by behind-now.

# 空气
Hand-strength read: VERY SMALL Air; behind now 100%, equity 0% vs 2-way avg 50% (weak) — no made hand, no draw.
```

---

## 8. 配置与开关

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `HAND_STRENGTH_ENABLED` | 1 | 与 `HAND_VS_RANGES_ENABLED` 解耦；数据缺失自动不输出 |
| `HAND_STRENGTH_THRESHOLDS` | — | P1：JSON 覆盖 4.1/4.2 的默认阈值，便于回放校准 |

- `hand_strength` 依赖 `hand_ranges`；`hand_ranges is None`（翻前/范围缺失）时直接不输出，绝不猜。
- 所有新代码 try/except 包裹，异常只记 warning，不影响现有建议链路。

---

## 9. 测试与验收

### 9.1 判定单测（构造 `HandVsRanges`）

- 两对 + behind_any 4% + equity 80%（n=3）→ `size=big`、`equity_tier=strong`、`kind=value`；
- 顶对 + behind_any 72% + equity 18%（n=2）→ `size=medium`、`equity_tier=weak`、`kind=value`；
- 强听 + behind_any 99% + equity 38% → `kind=draw`，绝不能标 `value`；
- 空气 + behind_any 100% + equity 0% → `size=very_small`、`kind=weak`；
- 边界：`behind_any=0`、`equity=1`；`behind_any=1`、`equity=0`；`n=1` vs `n=4`；平局分池。

### 9.2 接入单测

- `hand_ranges` 存在 → `hand_strength` 非空、`render_hand_facts` 含 `Hand-strength read`；
- `hand_ranges` 为 None（翻前/范围缺失）→ `hand_strength` 为 None、不出现该行；
- 行位置在 `## Hand vs ranges` 之后、`## Board structure` 之前；
- `_LEAN_SYSTEM` 含 `Hand-strength read` 口径规则，且系统提示仍 < 12600 字符。

### 9.3 回放校准

- 20–30 手，覆盖 3/4/5 人、flop/turn/river、湿/干/公对面；
- 重点看「顶对多路被标 MEDIUM」「听牌标 DRAW」「坚果标 VERY BIG」是否与人工一致；
- 阈值不对时改 `HAND_STRENGTH_THRESHOLDS`，不改代码。

### 9.4 回归

- `tests/run_all.py` 全绿；
- replay-console 提示词 golden 按需 bump（新增了一行事实）。

---

## 10. 风险与取舍

| 风险 | 影响 | 缓解 |
|---|---|---|
| 把“现在大”与“权益强”混成一个标签 | 模型误判 | 单一标签，但 summary 里同时保留 behind / equity / avg 三个数 |
| 阈值拍脑袋 | 强弱判定漂移 | 可配置 + 回放校准；阈值只在规则层，不进算力层 |
| 听牌 equity 高被当大牌 | 追牌错下价值注 | `kind=draw` 优先判定，强制不能标 value |
| 模型过度信任标签 | 忽略底层数字 | 标签只作摘要，底层 `Hand vs ranges` 块仍完整保留 |
| 多路口径与单挑口径混淆 | 强弱误读 | summary 明确写 `N-way avg`；需要时再并列单挑权益 |
| 提示词变长 | 超预算 | 单行 ≤ ~200 字符；系统规则 ≤ ~150 字符 |

---

## 11. 排期

| 阶段 | 工作 | 退出标准 |
|---|---|---|
| S0 | `HandStrengthRead` + `compute_hand_strength` + 分档单测 | 判定用例全对 |
| S1 | `Situation.hand_strength` + `render_hand_facts` 接线 + 提示词规则 | 端到端出 `Hand-strength read` |
| S2 | 20–30 手回放校准阈值 + golden bump | 无明显荒唐结论；回归全绿 |

**合计约 1 个工程日**；关键在 S2 的阈值校准，不在判定代码本身。

---

## 附录 A：判定伪代码

```python
def compute_hand_strength(sit) -> HandStrengthRead | None:
    hr = sit.hand_ranges
    if hr is None or not hr.opponents:
        return None

    n = len(hr.opponents)
    avg = 1.0 / (n + 1)
    ratio = hr.equity / avg if avg > 0 else 0.0

    # 绝对强度与 kind
    tier = sit.hand_tier
    made = (sit.ground_truth.made.name if sit.ground_truth is not None
            and sit.ground_truth.made is not None else sit.hand_label_en or "")
    nuts = bool(sit.ground_truth and sit.ground_truth.hero_is_nuts)

    if tier in (STRONG_DRAW, WEAK_DRAW):
        kind, size = "draw", "very_small"      # 听牌按 outs/赔率，不是成牌大小
    elif tier == AIR or tier == BOARD_PLAY:
        kind, size = "weak", "very_small"
    elif tier in (STRONG_MADE, OVERPAIR):
        kind, size = "value", ("very_big" if (nuts or tier == STRONG_MADE) else "big")
    elif tier == TPTK:
        kind, size = "value", "big"
    else:
        kind, size = "value", "medium"

    # 现在位置用 behind_any 修正：成牌但多路已落后 → 降一档
    if kind == "value" and hr.behind_any >= 0.35 and size in ("big", "very_big"):
        size = "medium"

    # 权益档次
    if ratio >= 2.0:   eq_tier = "strong"
    elif ratio >= 1.3: eq_tier = "above_average"
    elif ratio >= 0.85: eq_tier = "neutral"
    else:              eq_tier = "weak"

    return HandStrengthRead(size, eq_tier, kind, made, nuts,
                            hr.behind_any, hr.equity, avg, n)
```

---

## 附录 B：一句话

**牌力解析 = 把 `behind_now`（现在大不大）× `equity/平均线`（权益多强）× 牌型/坚果
（绝对多大）三个量，合并成一行动作语言；它让 AI 只做“选动作”，不再自己重推这三个数的关系。**
