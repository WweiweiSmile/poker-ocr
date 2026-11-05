# AGENTS.md — poker-ocr

给在本仓库工作的 AI / 新人的最短上手路径。**详细内容都在 [SYSTEM.md](SYSTEM.md)**，这里只放不能忘的部分。

## 先读

1. [SYSTEM.md](SYSTEM.md) — 架构链路、模块职责、数据契约、14 条硬口径、命令、坑。读完即可上手。
2. 改识别/几何前，再读 [README.md](README.md) 的「响应语义」「设计要点」两节。
3. 改提示词前，读 [SYSTEM.md §7](SYSTEM.md) 与 `docs/提示词*.md`。

## 项目一句话

德州扑克牌桌画面 → 可落库的牌局事实 + 可执行的 AI 建议。
两条链路：实时（`poker_ocr.live`，服务 `api.py:8000`）、沉淀复盘（`replay-console:8010` + MySQL）。
（GTO 离线跑批已于 2026-11-05 整体移除。）
范围 / 弃牌率的数字现在只住在 `poker_ocr/tables/`（v0 手调 / v1 推导，**默认 v0**）。

## 铁律（违反任何一条都会静默产生错误数据）

- **坐标一律归一化，不 resize 整图。** `reference_size` 只作溯源。
- **筹码必须过 BB 单位闸。** 无单位/单位不符的数字一律判不确定，绝不采信。
- **`status` 三态不合并**（`occupied` / `empty` / `uncertain`）；**`position` ≠ `is_dealer`**。
- **`errors` 非空 ⇒ 整帧丢弃**，不进手牌记录。
- **金额一律用「本街加到总额」**（与 `hand_track` / `coach` 同口径）。
- **fail open**：任何特征缺数据就退化成「没这条事实」，不抛异常、不阻塞 OCR 主循环。
- **模型只能引用事实、只能选 `options` 给的 id。** 牌力来自 `ground_truth`，尺度来自 `options`。

完整 14 条见 [SYSTEM.md §6](SYSTEM.md)。

## 代码导航：先 CodeGraph，后 grep

仓库已建 `.codegraph/` 索引（随文件变更同步，滞后约 1s）。

| 问题 | 工具 |
|---|---|
| 功能怎么跑通 / 架构在哪 | `codegraph_explore`（一次调用返回跨文件源码 + 调用路径） |
| 改这个会炸哪 | `codegraph_impact` / `codegraph_callers` |
| 某符号签名/实现 | `codegraph_node` |

只在图谱未覆盖时（文档、yaml、精确行号、未索引文件）才用 `read` / `grep`。

## 常用命令

```bash
.venv/bin/python tests/run_all.py                              # 全量测试（分层，先快后慢）
.venv/bin/python -m uvicorn api:app --port 8000                # 单图识别服务
.venv/bin/python -m poker_ocr.live --profile config/table_wpk_live.json \
    --initial-blinds 1/2 --out logs/live.states.jsonl          # 实时链路
cd replay-console && ./start.sh                                # 复盘控制台 :8010
.venv/bin/python -m poker_ocr.strategy_tables --profile sourced # 策略表生效/回落情况
.venv/bin/python tools/export_strategy_tables.py --check        # JSON↔代码逐值一致
```

## 改提示词必须三步走（有 golden test 兜底）

1. 改 `poker_ocr/prompt/` 下的 system / user 段；
2. bump `poker_ocr/prompt/version.py` 的 `PROMPT_VERSION` + `replay-console/replay/versioning.py` 的 `CURRENT_ADVICE_VERSION`；
3. 更新 `replay-console/tests/test_prompt_golden.py` 的 `EXPECTED_ADVICE_VERSION` / `EXPECTED_PROMPT_HASH`。

并在 `version.py` 顶部写清「为什么改、依据哪次复盘」。

## 不要做

- ❌ 往 `scan.py` 里加逻辑（已冻结）；❌ 在新代码里用 `scan.clean_number`（有静默错值）。
- ❌ 把 `rois.json` 当配置读写（它是识别**输出**，配置在 `config/table_*.json`）。
- ❌ 写 `replay-console` 的 `review_hands`（只读；收藏/结论只写 `replay_marks`）。
- ❌ 改 `api.py` 响应字段不同步下游（先 grep 调用点，再看 README 契约段）。
- ❌ 新增配置项不带校验（在 `profile.py` 里 fail fast，不要静默默认）。
- ❌ 改策略数字去动 `.py`（它们在 `poker_ocr/tables/*.json`）；❌ 把 `STRATEGY_TABLES_PROFILE` 写进 `.env`（import 时读，`.env` 后加载 → 静默不生效）。
- ❌ 复活 `gto/`（2026-11-05 已整体移除）。
