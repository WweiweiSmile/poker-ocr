# Ground Truth 事实注入：分析 + 落地说明

日期：2026-10-23
范围：`poker_ocr/ground_truth.py`（新增）、`poker_ocr/coach.py`、`poker_ocr/advice.py`、`api.py`
回归：`tests/test_ground_truth.py`、`tests/test_beat_rate.py`、`tests/test_advice.py`、`tests/test_coach.py`

---

## 0. 先说结论

你给的四步架构（确定性评估 → Ground-Truth 块 → 排名/坚果校验 → 注入并强制服从）
**方向是对的，而且这个仓库已经实现了大约 70%**：`coach.evaluate`、`board_ceiling`、
`beat_rate`、`render_hand_facts` 的「Computed facts」、以及两套提示词里的
「Computed facts are authoritative」，本来就是同一套思路。

真正缺的不是「再写一个评估器」，而是三件事：

1. **一份可序列化、可复用的事实块**。原来事实散落在 `render_hand_facts` 的十几行
   f-string 里，实时建议能拿到，单手提牌分析（`/coach/advice`）拿不到。
2. **精确的坚果校验**。`beat_rate` 只回答「多少比例比我大」，回答不了
   「**到底哪些牌型**比我大」「牌面上对手最强能到哪」——所以模型仍能说
   「有更大的牌」，数据打不了它的脸。
3. **牌面「已成花」与「只是花听」的区分**。`board_texture` 把两张同花也叫「花面」，
   提示词再翻译成 `Flush-y`，模型就会把 `Jc8dKdQc` 读成花面。这是 990010 的直接根因。

另外，**不要照搬你贴的那份代码**。它有几个会直接出错的点（见 §1），
正确做法是复用本项目已有的评估器，只补事实层。

---

## 1. 对你贴的算法的核对

### 1.1 对的部分

- 顺子只比顶端、A 可作 1、同花不比花色、葫芦先比三条：`coach.evaluate` 已经是这个口径。
- 「代码算事实、模型只解说」：与 `coach.py` 的「确定性规则不交给模型」一致。
- 用枚举回答「有没有更大的牌」：方向正确，比让模型自己组合可靠得多。
- 把踩过的坑做成断言：本次已落地为 `tests/test_ground_truth.py`。

### 1.2 不能直接用的部分

| 问题 | 说明 |
|---|---|
| `beaten_by` 未定义 | 你贴的代码里 `enumerate_higher_made_hands` 是空的。只算 `lose_pct` 仍回答不了「哪些牌型能赢我」。 |
| `classify_board` / `has_*_draw` / `is_coordinated` 未定义 | 直接跑会 `NameError`；空牌面 `max()` 还会崩。 |
| 两张同花被叫 `FLUSH_BOARD`？ | 你的阈值写的是 `>=3`，是对的；但本项目原来的 `board_texture` 是 `>=2`，提示词因此写成 `Flush-y`。这次在事实层把两者分开，没有改规则引擎的纹理表（避免影响下注尺度）。 |
| 警告句「顺子在此牌面已不是强牌」 | **过度纠正**。三张同花面上，顺子对随机牌只输约 4.3%，仍然是强牌，只是不是坚果。正确说法是「不是坚果、不要当无敌加注」，不是「不强」。本项目自己的 `test_beat_rate` 也是这个口径。 |
| `percentile = (win+tie)/total` | 这不是百分位，是「对随机一手牌的权益」。命名会误导；本次拆成 `win/tie/beat/equity`，并单独给 `beaten_by`。 |
| 枚举 990 次 × 每次 21 个 5 张组合 | 纯 Python 会到几十毫秒。本项目 `evaluate` 是直接算 7 张，`rank_hands` 实测约 5–10ms，并带 `lru_cache`。 |
| 「lose_pct≈0 就能否掉模型」 | 不够。`lose_pct=0` 仍可能全是分池（例如公共牌皇家同花顺）；而且模型可以改口说「牌面上可能存在」。必须给 **`beaten_by=()` + `NUT STATUS` + 分池比例** 三件事。 |
| 只算当前街 | 转牌坚果、河牌可能被反超。事实块要写「NUTS NOW」，并提示后续成花/成顺风险。 |

### 1.3 更重要的架构风险

如果按你贴的代码新建 `evaluator.py`，而 `coach.evaluate` 继续服务规则引擎，
就会出现**两套牌型排序**。这正是你担心的「一个说听牌没成、一个说成对」的根源。
本次的原则是：**评估器只有 `coach.evaluate` 一份**；新模块只做事实整理和渲染。

---

## 2. 本次落地的东西

### 2.1 `poker_ocr/coach.py`：精确排名入口

新增：

- `RankDetail` / `BeatenBy` / `rank_hands(hole, board, opponents)`：
  一次枚举对手所有两张暗牌，返回
  `win_pct / tie_pct / beat_pct / equity_pct`、
  **按牌型分组的 `beaten_by`（含组合数和示例）**、
  **对手最强持牌 `best_opponent_*`**、以及 `hero_is_nuts`。
- `beat_rate` 改为 `rank_hands` 的薄封装：老接口、老数字不变，但两者永远同源。
- `_rank_cached` 带 `lru_cache`：`read_hand` 已经枚举过一次，事实层再取时直接命中缓存。
- 没有改 `board_ceiling` 的规则表口径（仍是「3 连」粗判据），避免无意改变下注尺度。
  事实层另用 `_board_straight_possible` 精确回答「对手能不能补成顺子」：
  `5 7 9` 没有 3 连，但对手拿 `6 8` 就是顺子。

### 2.2 `poker_ocr/ground_truth.py`：唯一事实块

`compute_ground_truth(hero, board, opponents)` 返回 `GroundTruth`：

| 字段 | 内容 |
|---|---|
| `made` | 牌型、英文/中文名、`best_five`、`hero_cards_used` |
| `draws` | 花听/坚果花听/两头/卡顺、outs、四二法则权益 |
| `rank` | 胜/平/负、`beaten_by`、`hero_is_nuts`、对手最强牌 |
| `board_info` | 是否**已成花可能**（≥3 同花）、**只是花听**（=2）、公对/三条/四条、顺子是否可能、纹理标签、牌面上限 |
| `warnings` | 程序生成的硬提醒（坚果、非坚果、只有花听、公对面、河牌等） |

`render()` 输出固定格式：

```
GROUND TRUTH (deterministic evaluator; authoritative — never recompute, never contradict)
Board: Jc 8d Kd Qc｜turn
Board structure: FLUSH: no made flush possible (at most 2 of any suit — flush DRAW only)｜...
Made hand: STRAIGHT (A-high straight)｜best five: Th Jc Qc Kd As｜hole cards used: As Th
Made-hand rank (exact enumeration over all 1035 opponent holdings, 3 opponents): ...
NUT STATUS: NUTS — no opponent holding can beat you; you can only chop (0.9% ...).
Beaten by: nothing.
Draws: none.
Warnings:
- ...
```

关键口径：

- `hero_is_nuts` = **没有任何对手持牌能严格打败我**；允许分池，分池比例单独写。
- `beaten_by` 是精确枚举结果，不是「可能」。
- 两张同花只写 `flush DRAW only`，绝不写 `FLUSH: possible`。
- 顺子在三同花面写「不是坚果」，不写「不强」。

### 2.3 `poker_ocr/advice.py`：注入与强制服从

- `Situation` 新增 `ground_truth: GroundTruth | None`，由 `build_situation` 统一构建。
- `render_hand_facts` 用 `gt.render()` 替换原来分散的 Board texture / Made-hand rank 行；
  翻前或牌不全时退回旧渲染。
- 新增 `_GROUND_TRUTH_RULES`，lean 与 skills 两套 system 都注入：
  - 不得推翻/重算事实；
  - 事实说「没有成花」就不得说花面；
  - 事实说 NUTS 就不得编造更大的牌，面对下注默认加注；
  - `beaten_by` 是精确清单；
  - 三同花面的顺子「不是坚果，但仍是强牌」，不得说成弱牌。
- `value_directive` 增加 **ABSOLUTE NUTS** 分支：精确坚果 + 面对下注 → 明确
  `RAISE`，并禁止用「可能有更大的牌」当理由。990010 的转牌会走到这里。
- `/coach/advice`（规则引擎，不走模型）也返回同一份 `ground_truth`，单手提牌分析
  与实时 session 分析共用一份事实。

### 2.4 测试

`tests/test_ground_truth.py`（16 条，含用户给的用例）：

- `Jc8dKdQc + AsTh`：`STRAIGHT`、`hero_is_nuts`、`beaten_by=()`、
  `flush_made_possible=False`、文本含 `no made flush possible` / `NUTS`；
- `Jc8dKdQcAd + AsTh`：`NOT THE NUTS`、`beaten_by` 含 `Flush`；
- `AhKhQhJh2c + Ts9s`：`STRAIGHT`、`beaten_by` 含 `Flush` 与 `Straight flush`；
- 轮子 `A2345` 是 5-high；6-high 顺子大于轮子；葫芦大于同花；顺子大于三条；
- 22 在 88T 不会出现「补成葫芦算顺子 outs」的幻影 outs；
- 翻前/缺牌/重复牌返回 `None`，不伪造事实；
- 提示词包含事实块与强制规则；坚果面对下注给出 `RAISE` 指令。

跑法：

```bash
.venv/bin/python -m pytest tests/test_ground_truth.py tests/test_beat_rate.py \
    tests/test_advice.py tests/test_coach.py -q
.venv/bin/python tests/test_api.py
```

当前结果：上述 153 条逻辑测试 + API 脚本全部通过。

---

## 3. 还没有解决的问题（别把「有事实」当成「不会错」）

1. **随机牌排名是下界**。`beaten_by` 枚举的是对手任意两张牌；真实对手范围更窄、
   更强。事实块已写明 `LOWER bound`，但决策仍需桌面动态/行动线。
2. **多人「至少一家领先」是独立近似**：`1-(1-p)^n`，不是精确的多对手联合枚举
   （精确枚举在 3 家以上是组合爆炸）。它只用于提示，不用于硬规则。
3. **只枚举当前街**。转牌 NUTS 可能被河牌的花/顺反超；事实块会提示「NUTS NOW +
   后续成花风险」，但不会替模型做未来街的树搜索。
4. **模型仍可能不服从**。事实注入把「结构性幻觉」压到很低，但不能保证 100%，
   尤其当行动线强烈暗示「被动玩家满池下注 = 价值」时。990010 已加
   `ABSOLUTE NUTS → RAISE` 的硬指令，仍建议对关键点多次采样（温度 0.2 也有波动）。
5. **OCR 牌面异常**。事实层对重复牌/缺牌返回 `None`，但不会修复识别错误；
   牌面稳定闸仍然要保留。
6. **规则引擎与 AI 的边界**。`coach.advise` 仍按 §3/§4 的纹理表下注；事实层比
   `board_ceiling` 更精确（会识别 `5 7 9` 这种两洞顺子），但本次没有重写整张尺度表。

---

## 4. 如果以后要换评估器

保持接口即可：

```python
from poker_ocr.ground_truth import compute_ground_truth
gt = compute_ground_truth("AsTh", "Jc8dKdQc", opponents=3)
```

内部只依赖 `coach.evaluate` / `coach.rank_hands` / `coach.board_ceiling`。
若换成 `phevaluator`/`treys`，只需让 `coach.evaluate` 返回同一 `(category, tiebreak)`
排序键，`ground_truth.py` 不用动——这样「单一事实源」不会破。

---

## 5. 990009 / 990010 复跑结果（2026-10-23）

数据：`/tmp/review30.jsonl` 里的 990009 / 990010（两手内容重复，按用户要求都跑）。
命令：

```bash
.venv/bin/python tools/compare_review_hands.py /tmp/review-990009-990010.jsonl \
    --no-dedupe --json logs/review-990009-990010-advice.json
```

| 手 | 街 | 实际 | 新建议 | 判定 | 新建议理由（摘要） |
|---|---|---|---|---|---|
| 990010 | turn | raise 90 | **raise 108** | 一致 | Nut A-high straight, no holding beats me; raise big to charge draws |
| 990010 | river | fold | call 144.5 | 动作不同 | Straight beats 89.4%, only flushes beat it; price is fine, call |
| 990009 | turn | raise 90 | **raise 108** | 一致 | Nut A-high straight, no hand beats me; raise for value |
| 990009 | river | fold | call 144.5 | 动作不同 | 同上；有同花可能，所以 call 不加注 |

- **990010 转牌退化已修掉**：原建议是 `call 36`（理由「Straight on a flush board / made straight or better」），
  现在是 `raise 108`，理由正确引用「坚果、没有持牌能赢」。
- 牌面标签从 `Flush + straight` 改为
  `Two-tone (flush draw only — no made flush) + straight possible`，
  模型不再把两张同花读成已成花。
- 河牌仍是 `call` 而不是实际 `fold`：按赔率（需 28.6%，随机牌权益 89.4%）call 合理；
  对手是松弱、河牌下注偏价值，fold 也不算离谱。这一条属于边界判断，不是事实层错误。
- 8 个决策点：一致 4、尺度差 2、动作不同 2；新建议硬违规 0；延迟 p50 874ms / max 1189ms。

---

## 6. 2026-10-23 补丁：听牌不被 NUT STATUS 压住 + 多人 at-least-one

### 6.1 听牌 `draw_focus`

`GroundTruth.render(draw_focus=True)`：

- 纯听牌 **不渲染** `NUT STATUS` / `Beaten by`；
- 改成 `DRAW STATUS (action basis)` + `DRAW RULE`；
- `DRAW RULE` 明确：单挑无下注时半诈唬是基线；对永不弃牌的跟注站可以过牌，但理由必须是
  `NO FOLD EQUITY`，不能是「我不是坚果」；3 家以上听牌默认过牌。
- `advice.render_hand_facts` 在 `hand_tier in (STRONG_DRAW, WEAK_DRAW)` 时传
  `draw_focus=True`；system prompt 的 `_GROUND_TRUTH_RULES` 同步补了 DRAW 例外。

对应测试：

- `test_draw_focus_hides_made_hand_nut_status`（h104 坚果花听：draw_focus 文本里没有
  `NUT STATUS` / `Beaten by`，有 `DRAW STATUS` / `NO FOLD EQUITY`）；
- `test_draws_are_not_judged_by_made_hand_rank`（回放 `render_hand_facts`）。

### 6.2 `_rank_text`：单对手 equity + 多人 at-least-one

`_rank_text` 现在固定写：

```
beaten by X% of hands PER OPPONENT; ties Y% per opponent;
equity Z% vs ONE opponent.
Multiway: with N opponents, W% chance at least one is ahead.
```

之前只写 `beaten by X%; equity Z%`，模型会把单对手权益误读成「N 家同时我领先」。
对应测试：`test_rank_text_labels_equity_per_opponent_and_multiway`。

### 6.3 复跑工具注入 `.table_dy`

`tools/replay_live_decisions.py`、`tools/compare_review_hands.py` 默认读取项目根目录
`.table_dy`，`--no-table-dynamics` 关闭，`--table-dynamics PATH` 指定别的文件。
这样复盘复跑和 `poker_ocr/live.py` 的实时链路使用同一份桌风。

### 6.4 补丁后全量复跑

见 `docs/复跑-事实注入-补丁与桌风-2026-10-23.md`：

- 线上 101 点：旧 14 硬违规 → **0**；GT+桌风 20 个动作变化；
- dev 库 85 点：实际 9 硬违规 → **0**；GT+桌风 33 个动作变化；
- 990010 转牌 `raise 108` 3/3 稳定；
- h104 在桌风下回到 `bet 1/2`（真实听牌半诈唬）；
- 990030 / 990034 在多人 at-least-one 明示后仍 `bet 2/3`（你的桌风价值线）；
- 990050 25BB 进 362BB：GT+桌风 `fold`（桌风「不付钱」），纯赔率 `call`，属于策略取舍。
