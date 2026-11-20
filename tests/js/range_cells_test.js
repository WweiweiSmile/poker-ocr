/* 范围推导热图的「零权重格」分类：纯函数，靠 node 跑，不需要浏览器。
 *
 * 口径来自 #32 的 agent brief：
 *   · 任一视图下 169 格三分类互斥且覆盖全集：有份额 / 零但先验含（已被删）/ 零且先验不含；
 *   · 「先验未包含」与「本步删」在文案上必须分开，且先验视图里不存在「已被删」；
 *   · 删减图例附带「另有 M 格先验未包含（不计入 N）」，M 是全集口径，不随步变。
 */
"use strict";

const path = require("path");
const assert = require("assert");

const RTCells = require(path.join(__dirname, "..", "..", "poker_ocr", "workbench", "static", "range_cells.js"));

const RANKS = ["A", "K", "Q", "J", "T", "9", "8", "7", "6", "5", "4", "3", "2"];

function idx(key) {
  const a = RANKS.indexOf(key[0]);
  const b = RANKS.indexOf(key[1]);
  if (key.length === 2) return a * 13 + a;
  return key[2] === "s" ? a * 13 + b : b * 13 + a;
}

function empty() {
  return new Array(169).fill(0);
}

function fromKeys(keys, weight) {
  const grid = empty();
  keys.forEach((k) => { grid[idx(k)] = weight; });
  return grid;
}

// 3BET_vs_MP 的 15 个 key（74 combos / 5.58%）—— 一手来自票面，不是从代码算的
const PRIOR_KEYS = ["AA", "KK", "QQ", "AKs", "AKo", "A5s", "A6s", "A7s", "A8s", "A9s",
                    "ATs", "AJs", "AQs", "KQs", "KJs"];

const prior = fromKeys(PRIOR_KEYS, 1 / 15);
// 第 1 步（翻牌 Kd7c2s 面对下注）：A7s 因 dead 7c 归零，其余仍在（份额重新归一化无所谓，只看是否 >0）
const step1 = fromKeys(PRIOR_KEYS.filter((k) => k !== "A7s"), 1 / 14);
// 第 2 步：再掉 AJs
const step2 = fromKeys(PRIOR_KEYS.filter((k) => k !== "A7s" && k !== "AJs"), 1 / 13);

function view(kind, grid) {
  return { kind, grid, priorGrid: prior, stepGrids: [step1, step2] };
}

/* 1. 先验视图：15 格有份额，154 格先验未包含，不存在「已被删」 */
{
  const r = RTCells.classify(view("prior", prior));
  assert.strictEqual(r.counts.alive, 15, "先验应有 15 个 key 有份额");
  assert.strictEqual(r.counts.absent, 154, "先验外应有 154 格");
  assert.strictEqual(r.counts.removed, 0, "先验视图不该出现「已被删」");
  assert.strictEqual(r.counts.alive + r.counts.removed + r.counts.absent, 169, "三类应覆盖 169 格");
  assert.strictEqual(r.kinds[idx("77")], "absent", "77 不在 3BET_vs_MP 先验里");
  assert.strictEqual(r.kinds[idx("76s")], "absent", "76s 不在 3BET_vs_MP 先验里");
  assert.strictEqual(r.kinds[idx("A7s")], "alive", "A7s 在先验里");
}

/* 2. 第 1 步：A7s 变成「已被删」，77 仍是「先验未包含」 */
{
  const r = RTCells.classify(view("step", step1));
  assert.strictEqual(r.kinds[idx("A7s")], "removed", "A7s 在第 1 步被删");
  assert.strictEqual(r.removedStep[idx("A7s")], 1, "应指出删在第 1 步");
  assert.strictEqual(r.kinds[idx("77")], "absent", "77 走多少步都是先验未包含");
  assert.strictEqual(r.kinds[idx("KK")], "alive", "KK 仍存活");
  assert.strictEqual(r.counts.removed, 1);
  assert.strictEqual(r.counts.alive, 14);
  assert.strictEqual(r.counts.alive + r.counts.removed + r.counts.absent, 169);
}

/* 3. 第 2 步：AJs 也进「已被删」，且指向第 2 步 */
{
  const r = RTCells.classify(view("step", step2));
  assert.strictEqual(r.kinds[idx("AJs")], "removed");
  assert.strictEqual(r.removedStep[idx("AJs")], 2);
  assert.strictEqual(r.removedStep[idx("A7s")], 1, "A7s 仍归到第 1 步");
  assert.strictEqual(r.counts.removed, 2);
  assert.strictEqual(r.counts.alive + r.counts.removed + r.counts.absent, 169);
}

/* 4. 防御：先验外却有份额的格子按存活处理，不能画成「先验未包含」 */
{
  const g = fromKeys(["AA", "77"], 0.5);
  const r = RTCells.classify({ kind: "step", grid: g,
                               priorGrid: fromKeys(["AA"], 1), stepGrids: [g] });
  assert.strictEqual(r.kinds[idx("77")], "alive");
}

/* 5. 文案：三类各自的关键词，删减那条要带步号 */
{
  assert.strictEqual(RTCells.cellTitle("77", "absent"), "77 · 先验未包含");
  const removed = RTCells.cellTitle("A7s", "removed", { removedStep: 1 });
  assert.ok(removed.indexOf("已被删") >= 0, "「已被删」应在标题里");
  assert.ok(removed.indexOf("第 1 步") >= 0, "标题应指出第几步删的");
  const alive = RTCells.cellTitle("KK", "alive", { share: 0.0321 });
  assert.ok(alive.indexOf("份额") >= 0, "存活格保留份额文案");

  const note = RTCells.absentNote(154, 27);
  assert.ok(note.indexOf("154") >= 0, "缺格数应出现在图例里");
  assert.ok(note.indexOf("先验未包含") >= 0);
  assert.ok(note.indexOf("不计入") >= 0, "必须说明不计入本步删减");
}

console.log("ok range_cells: 5 groups");
