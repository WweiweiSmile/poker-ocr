/* 范围推导热图的格子三分类（纯函数，无 DOM 依赖，可在 node 里直接断言）。
 *
 * 169 格在任一“座位 × 步”视图下恰好互斥地落进三类：
 *   alive   —— 本视图里有份额；
 *   removed —— 先验里有（prior.grid > 0），走到本视图已为 0 —— 被事件删掉了；
 *   absent  —— 先验里就没有（prior.grid == 0），走多少步都是“先验未包含”。
 *
 * 为什么要分这三类：3BET_vs_MP 只有 74 combos / 15 个 key，其余 154 格从来没进过
 * 先验。只报 dead/归零/剪除的话，读者会把“先验不含中 7”误读成“事件模型收得太紧”。
 * 见 #32 与 docs/看板-范围推导-信息架构.md §3 的图例（dead ✕ / floor 斜纹 / 先验无 = 白）。
 *
 * 文本一律由调用方 textContent 写入，本模块只产出字符串。
 */
"use strict";

(function (root) {
  var N_CELLS = 169;

  function num(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }

  function stepGrid(steps, at) {
    var grid = steps && steps[at];
    return grid && grid.length ? grid : null;
  }

  // 这格在“第几步”归零：跳过被性能预算置空（grid 为空）的步，避免把“不知道”说成“第 1 步”。
  function firstZeroStep(steps, index) {
    if (!steps) return 0;
    for (var i = 0; i < steps.length; i++) {
      var grid = stepGrid(steps, i);
      if (!grid) continue;
      if (!(num(grid[index]) > 0)) return i + 1;
    }
    return 0;
  }

  /* view: {kind: "prior"|"step"|"final", grid: number[169],
   *        priorGrid: number[169], stepGrids: (number[169]|null)[]}
   * 返回 {kinds, removedStep, counts}；kinds/removedStep 都是长度 169 的数组。 */
  function classify(view) {
    var prior = (view && view.priorGrid) || [];
    var grid = (view && view.grid) || [];
    var steps = (view && view.stepGrids) || [];
    var kinds = new Array(N_CELLS);
    var removedStep = new Array(N_CELLS);
    var counts = { alive: 0, removed: 0, absent: 0 };
    for (var i = 0; i < N_CELLS; i++) {
      var weight = num(grid[i]);
      var priorWeight = num(prior[i]);
      if (weight > 0) {
        kinds[i] = "alive";
        counts.alive += 1;
      } else if (priorWeight > 0) {
        kinds[i] = "removed";
        removedStep[i] = firstZeroStep(steps, i);
        counts.removed += 1;
      } else {
        kinds[i] = "absent";
        counts.absent += 1;
      }
    }
    return { kinds: kinds, removedStep: removedStep, counts: counts };
  }

  function shareText(share) {
    var value = num(share) * 100;
    return (Math.round(value * 100) / 100).toFixed(2) + "%";
  }

  // info: {share}（alive）/ {removedStep}（removed）；absent 不看 info。
  function cellTitle(key, kind, info) {
    var opts = info || {};
    var text = String(key || "") + " · ";
    if (kind === "absent") return text + "先验未包含";
    if (kind === "removed") {
      var step = num(opts.removedStep);
      return step > 0 ? text + "已被删 · 第 " + step + " 步" : text + "已被删";
    }
    return text + "份额 " + shareText(opts.share);
  }

  /* 图例第二句：先验外的格子是全集口径，不属于任何一步的删减，必须和“本步删 N”分开写。 */
  function absentNote(absent, removedTotal) {
    var total = num(removedTotal);
    var head = "另有 " + num(absent) + " 格先验未包含";
    if (total > 0) return head + "（不计入 " + total + "）";
    return head + "（先验外，不属于任何一步的删减）";
  }

  var RTCells = { classify: classify, cellTitle: cellTitle, absentNote: absentNote };
  if (typeof module !== "undefined" && module.exports) module.exports = RTCells;
  else root.RTCells = RTCells;
})(typeof window !== "undefined" ? window : globalThis);
