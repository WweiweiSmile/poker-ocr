/* 建议卡「数据边界」段（纯函数，无 DOM 依赖，可在 node 里直接断言）。
 *
 * advice 记录里 `facts.warnings` 是事实边界（ante 按桌配置假设、部分对手按随机牌补位、
 * 权益被降采样、在座人数不足未校准……）。这些前提以前只进提示词，工作台卡片上看不到，
 * 事后复盘没法判断某条建议是不是带着假设给出的。见 #111。
 *
 * 口径：
 *   · 一条 warning 一行，顺序照抄列表——不筛选、不去重、不改文案；
 *   · 列表为空 / 字段缺失 / 不是数组 ⇒ 返回空串，卡片上整段不出现；
 *   · 每条文本都过调用方给的转义函数（默认与 app.js 的 esc() 同一套规则），
 *     换行不处理，原样留在同一个 <li> 里，由 CSS `white-space: pre-wrap` 渲染成多行；
 *   · 本模块只产出字符串，不写 DOM。
 */
"use strict";

(function (root) {
  var LABEL = "数据边界";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* 非数组一律当作没有；null/undefined 条目没有文本，跳过（fail open）。 */
  function warningLines(warnings) {
    if (!Array.isArray(warnings)) return [];
    var lines = [];
    for (var i = 0; i < warnings.length; i++) {
      var warning = warnings[i];
      if (warning == null) continue;
      lines.push(String(warning));
    }
    return lines;
  }

  /* warnings: string[]（缺省/空 ⇒ ""）；escape: 可选，默认本模块的 escapeHtml。 */
  function warningsHtml(warnings, escape) {
    var lines = warningLines(warnings);
    if (!lines.length) return "";
    var esc = typeof escape === "function" ? escape : escapeHtml;
    return '<div class="advice-bounds">'
      + '<div class="advice-label">' + esc(LABEL) + '</div>'
      + '<ul class="advice-bounds-list">'
      + lines.map(function (line) { return "<li>" + esc(line) + "</li>"; }).join("")
      + '</ul></div>';
  }

  var AdviceBounds = { warningsHtml: warningsHtml };
  if (typeof module !== "undefined" && module.exports) module.exports = AdviceBounds;
  else root.AdviceBounds = AdviceBounds;
})(typeof window !== "undefined" ? window : globalThis);
