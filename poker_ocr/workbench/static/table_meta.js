/* 牌桌面板「N 人在局」这一段（纯函数，无 DOM 依赖，可在 node 里直接断言）。

 * 见 #121：players_in_hand 是**本手发牌人数**，含已弃牌与中途离场——它必须
 * 保持这个语义，因为换手判定（hand_track 的 IN_HAND_JUMP）靠它跳变，改成
 * 「不含弃牌」会让每一次弃牌都像换手，制造假记录。
 *
 * 所以「N 人在局」改用状态里新增的 players_not_folded（还没弃牌的人数，含 hero）：
 *   · 优先读 players_not_folded；
 *   · 只有老记录（这个键出现之前的 JSONL / 缓存的快照）才回落到 players_in_hand
 *     ——那是旧口径的显示，聊胜于无，绝不反过来覆盖新键；
 *   · 两个都缺 ⇒ 人数显示「—」，与旧行为一致；
 *   · 本模块只产出字符串，不写 DOM。
 */
"use strict";

(function (root) {
  var MISSING = "\u2014"; /* 「—」，与旧代码 st.players_in_hand ?? '—' 一致 */

  /* 非有限数字一律当「不知道」；0 也是有效读数（全弃牌时 hero 还在，通常 >=1）。 */
  function inHandCount(state) {
    if (!state) return null;
    var live = state.players_not_folded;
    if (typeof live === "number" && isFinite(live)) return live;
    var dealt = state.players_in_hand;
    if (typeof dealt === "number" && isFinite(dealt)) return dealt;
    return null;
  }

  /* streetLabel: 已渲染好的街道名（「翻牌」…）；state: 快照里的 state 对象。 */
  function inHandLine(streetLabel, state) {
    var count = inHandCount(state);
    var text = (count == null ? MISSING : String(count)) + " 人在局";
    var label = streetLabel == null ? "" : String(streetLabel);
    return label ? label + " \u00b7 " + text : text;
  }

  var TableMeta = { inHandCount: inHandCount, inHandLine: inHandLine };
  if (typeof module !== "undefined" && module.exports) module.exports = TableMeta;
  else root.TableMeta = TableMeta;
})(typeof window !== "undefined" ? window : globalThis);
