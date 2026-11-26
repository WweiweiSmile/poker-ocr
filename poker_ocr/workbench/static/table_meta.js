/* 牌桌面板「N 人在局」与座位弃牌三态（纯函数，无 DOM 依赖，可在 node 里直接断言）。

 * 见 #121：players_in_hand 是**本手发牌人数**，含已弃牌与中途离场——它必须
 * 保持这个语义，因为换手判定（hand_track 的 IN_HAND_JUMP）靠它跳变，改成
 * 「不含弃牌」会让每一次弃牌都像换手，制造假记录。
 *
 * 见 #123：弃牌要「手牌图标消失 + 头像弃牌」两条同时成立，头像是 best-effort
 * OCR，所以「弃牌状态没读到」是常态。这种座位不许画成在局：
 *   · 人数读 players_in_play（确定在局，含 hero，不含未知）；
 *   · 未知数由 players_fold_unknown 单独报成后缀「（N 未知）」，**不混进人数**；
 *   · 老记录（#123 之前的 JSONL / 缓存快照）没有这两个键：回落到 #121 的
 *     players_not_folded（未知算在局），再回落到最老的 players_in_hand；
 *     绝不反过来覆盖新键。
 *   · 两个都缺 ⇒ 人数显示「—」，与旧行为一致；0 是有效读数。
 *
 * 座位弃牌三态 seatFold()：'folded' / 'playing' / 'unknown' / 'empty'。它只看
 * 图标通道的结论（fold）与 status，**不拿动作徽章补**——徽章会淡出，按它算
 * 会让计数和座位随动画抖动；负证据更不许用（没看到他行动 ≠ 他弃牌）。
 *
 * 本模块只产出字符串/枚举，不写 DOM。
 */
"use strict";

(function (root) {
  var MISSING = "\u2014"; /* 「—」，与旧代码 st.players_in_hand ?? '—' 一致 */

  /* 非有限数字一律当「不知道」；0 也是有效读数（全弃牌时 hero 还在，通常 >=1）。 */
  function num(v) {
    return typeof v === "number" && isFinite(v) ? v : null;
  }

  function inHandCount(state) {
    if (!state) return null;
    var playing = num(state.players_in_play);
    if (playing != null) return playing;
    var live = num(state.players_not_folded);
    if (live != null) return live;
    return num(state.players_in_hand);
  }

  /* 未知座位数；不是读数（缺键 / null / NaN / 非数字）一律当 0，不画后缀。 */
  function foldUnknownCount(state) {
    if (!state) return 0;
    var n = num(state.players_fold_unknown);
    return n == null || n <= 0 ? 0 : n;
  }

  /* streetLabel: 已渲染好的街道名（「翻牌」…）；state: 快照里的 state 对象。 */
  function inHandLine(streetLabel, state) {
    var count = inHandCount(state);
    var text = (count == null ? MISSING : String(count)) + " 人在局";
    /* 只有人数取自 players_in_play 才报未知后缀：缺这个键说明是老记录，人数
       来自 players_not_folded（未知已算在局），再报未知会把同一批人算两遍。 */
    var unknown = state && num(state.players_in_play) != null
      ? foldUnknownCount(state) : 0;
    if (unknown > 0) text += "\uff08" + unknown + " 未知\uff09";
    var label = streetLabel == null ? "" : String(streetLabel);
    return label ? label + " \u00b7 " + text : text;
  }

  /* 座位弃牌三态 + 暂离。player: JSONL/SSE 里的对手对象（可能没有 fold 键 = 老记录）。 */
  function seatFold(player) {
    if (!player) return "empty";
    if (player.left) return "folded"; /* 离桌必然已经弃牌 */
    /* 暂离（#124）：连续几帧什么都没读到。不是弃牌，但也不能画成还在局。 */
    if (player.left_uncertain || player.status === "uncertain") return "uncertain";
    if (player.fold === true || player.status === "folded") return "folded";
    if (player.in_hand === false) return "empty"; /* 本手没发到牌（中途入座） */
    if (player.status === "away" || player.status === "sitting_out"
        || player.status === "buying_in") return "empty";
    if (player.fold === false) return "playing";
    return "unknown"; /* fold 为 null 或缺键：没读到就是没读到 */
  }

  var TableMeta = { inHandCount: inHandCount, inHandLine: inHandLine, seatFold: seatFold };
  if (typeof module !== "undefined" && module.exports) module.exports = TableMeta;
  else root.TableMeta = TableMeta;
})(typeof window !== "undefined" ? window : globalThis);
