<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>范围推导时间线 · 一次性原型</title>
<style>
:root{
  --bg:#0e1014; --panel:#161a21; --panel2:#1c2230; --line:#2a3140;
  --text:#e8ebf2; --muted:#9aa4b8; --dim:#6b7688;
  --hot:#ff8a5c; --cool:#4aa3ff; --good:#3ecf8e; --bad:#e0526b; --warn:#e5b567;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:13px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
.wrap{max-width:1400px;margin:0 auto;padding:16px 20px 64px}
.top{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.top h1{font-size:15px;font-weight:600;margin:0}
.top .sub{color:var(--muted)}
.proto{display:inline-block;font-size:11px;color:var(--warn);
  border:1px solid rgba(229,181,103,.4);border-radius:999px;padding:1px 8px}
.tools{margin-left:auto;display:flex;gap:10px;align-items:center;color:var(--muted);font-size:12px}
.tools select{background:var(--panel2);color:var(--text);border:1px solid var(--line);
  border-radius:6px;padding:2px 6px}
.card{display:inline-flex;align-items:center;justify-content:center;
  min-width:26px;height:34px;padding:0 4px;border-radius:5px;background:#f2f4f8;
  color:#15181d;font-weight:700;font-size:14px;letter-spacing:-.5px}
.card.red{color:#c62d3e}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 14px}
.chip{border:1px solid var(--line);background:var(--panel);border-radius:999px;
  padding:3px 10px;font-size:12px;color:var(--dim)}
.chip b{color:var(--text);font-weight:650;font-size:13px}
.chip .cw{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.chip .cprior{font-size:10.5px;color:var(--dim);margin-left:2px}
.chip.on{border-color:var(--cool);background:rgba(74,163,255,.12);color:var(--text)}
.chip.hero{border-color:rgba(62,207,142,.5);color:var(--good)}
.chip .w{color:var(--dim);margin-left:4px}
.advicebar{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:16px;align-items:start;
  background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--cool);
  border-radius:10px;padding:10px 14px;margin:10px 0 12px}
.advicebar .alab{font-size:20px;font-weight:650;line-height:1.2;white-space:nowrap}
.advicebar .areason{color:var(--text);font-size:13px;line-height:1.5}
.advicebar .ameta{color:var(--dim);font-size:11.5px;white-space:nowrap;padding-top:3px}
.eqbox{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:10px 14px;margin-bottom:14px}
.eqhead{display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.eqhead .big{font-size:20px;font-weight:650}
.eqhead .k{color:var(--muted);font-size:12px}
.eqbar{position:relative;height:12px;border-radius:6px;overflow:hidden;background:#232a37}
.eqbar i{position:absolute;top:0;bottom:0;display:block}
.eqbar .lose{background:#3a2530;left:0}
.eqbar .tie{background:#5a5334}
.eqbar .win{background:linear-gradient(90deg,#2f9e6c,#3ecf8e)}
.whisk{position:absolute;top:-3px;bottom:-3px;width:2px;background:#fff;opacity:.75}
.eqlegend{display:flex;gap:14px;color:var(--muted);font-size:11.5px;margin-top:6px;flex-wrap:wrap}
.grid2{display:grid;grid-template-columns:300px minmax(0,1fr);gap:16px;align-items:stretch}
.grid2.flat{grid-template-columns:minmax(0,1fr)}
.leftcol{display:flex;flex-direction:column;gap:16px;min-width:0}
.leftcol .panel{flex:0 0 auto}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px}
.phead{display:flex;gap:8px;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line);
  font-size:12px;color:var(--muted)}
.phead b{color:var(--text);font-weight:600}
.tl{padding:6px}
.step{display:block;width:100%;text-align:left;border-radius:8px;padding:7px 9px;
  border:1px solid transparent;margin-bottom:4px;color:var(--muted)}
.step:hover{background:var(--panel2)}
.step.on{background:rgba(74,163,255,.10);border-color:rgba(74,163,255,.45);color:var(--text)}
.step .lab{font-size:12.5px;color:var(--text);font-weight:600;display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.step .lab .z{white-space:nowrap;flex:0 0 auto;color:var(--muted);font-weight:500;font-size:11px}
.step .meta{font-size:11.5px;color:var(--dim);display:flex;gap:8px;flex-wrap:wrap;margin-top:2px}
.step .z{font-variant-numeric:tabular-nums}
.dbar{display:flex;height:4px;border-radius:2px;overflow:hidden;margin-top:5px;background:#232a37}
.dbar i{display:block;height:100%}
.right{display:grid;grid-template-columns:minmax(0,420px) minmax(0,1fr);gap:16px;align-items:start}
.right>.panel{display:flex;flex-direction:column;min-height:0}
.right>.panel>.tblwrap{flex:1 1 auto;min-height:0;
  -webkit-mask-image:linear-gradient(180deg,#000 calc(100% - 16px),transparent);
  mask-image:linear-gradient(180deg,#000 calc(100% - 16px),transparent)}
.right>.panel:last-child{overflow:hidden}
.right .phead,.right .note{flex:0 0 auto}
.hm{padding:10px 12px 12px;flex:1;display:flex;flex-direction:column;justify-content:center}
.hmhead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;font-size:11.5px;color:var(--muted)}
.matrix{display:grid;grid-template-columns:repeat(13,1fr);gap:2px}
.cell{position:relative;aspect-ratio:1;border-radius:3px;background:#171c25;
  font-size:9px;color:#e9edf5;display:flex;align-items:center;justify-content:center;
  cursor:pointer;user-select:none;overflow:hidden}
.cell{box-shadow:inset 0 0 0 1px rgba(232,235,242,.04)}
.cell.zero{background:#131720}
.cell:hover{outline:1px solid rgba(255,255,255,.5)}
.cell.on{outline:2px solid #fff;outline-offset:0}
.cell .lbl{position:relative;z-index:1;opacity:.8;text-shadow:0 1px 1px rgba(0,0,0,.45)}
.cell.zero{color:transparent}
.scale{display:flex;align-items:center;gap:6px;margin-top:6px;font-size:11px;color:var(--dim)}
.scale .ramp{flex:1;height:5px;border-radius:3px;
  background:linear-gradient(90deg,#171c25,#2b3b57,#3b6fb0,#ff8a5c)}
.cellchip{display:flex;align-items:center;gap:8px;margin-top:9px;padding:4px 8px;
  border:1px solid rgba(74,163,255,.45);background:rgba(74,163,255,.10);border-radius:8px;font-size:12px}
.cellchip b{font-weight:650}
.cellchip .cw{color:var(--dim);font-variant-numeric:tabular-nums}
.cellchip button{margin-left:auto;color:var(--muted);border:1px solid var(--line);
  border-radius:6px;padding:0 7px;font-size:11.5px;line-height:18px}
.cellchip button:hover{color:var(--text);border-color:#4a5468}
.tblwrap{overflow:auto;max-height:520px}
.right>.panel>.tblwrap{max-height:none}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{padding:4px 7px;text-align:left;border-bottom:1px solid #222836;white-space:nowrap}
th{color:var(--dim);font-weight:500;position:sticky;top:0;background:var(--panel)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:10.5px;padding:0 5px;border-radius:4px;border:1px solid var(--line);color:var(--muted)}
.tag.dead{color:var(--bad);border-color:rgba(224,82,107,.4)}
.tag.floor{color:var(--warn);border-color:rgba(229,181,103,.4)}
.note{color:var(--dim);font-size:11.5px;padding:8px 12px;border-top:1px solid var(--line)}
.overview{padding:10px 12px 12px}
.ovrow{display:grid;grid-template-columns:92px minmax(0,1fr) 48px;gap:8px;align-items:center;
  margin-bottom:5px;cursor:pointer}
.ovrow .nm{color:var(--muted);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ovrow .nm.legacy{color:var(--warn)}
.ovtrack{position:relative;height:9px;border-radius:5px;background:#232a37;overflow:hidden}
.ovfill{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:var(--cool);opacity:.85}
.ovrow.legacy .ovfill{background:var(--warn);opacity:.7}
.ovtick{position:absolute;top:-2px;bottom:-2px;width:1px;background:rgba(232,235,242,.45)}
.ovrow .pc{text-align:right;font-variant-numeric:tabular-nums;color:var(--text);font-size:11.5px}
.ovax{display:flex;justify-content:space-between;margin:4px 56px 0 100px;
  padding-top:3px;border-top:1px solid #3a4356;color:var(--dim);font-size:10.5px;
  font-variant-numeric:tabular-nums}
.empty{margin:10px 12px;border:1px dashed var(--line);border-radius:8px;padding:14px;color:var(--muted)}
.empty b{color:var(--text);display:block;margin-bottom:4px;font-weight:600}
details.gaps{margin-top:16px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
details.gaps summary{cursor:pointer;color:var(--muted)}
details.gaps li{margin:5px 0;color:var(--muted)}
details.gaps code{background:#10141a;padding:0 4px;border-radius:3px;color:var(--text)}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1 id="title">范围推导时间线</h1>
    <span class="proto">一次性原型 · 不接 live</span>
    <span class="sub" id="sub"></span>
    <div class="tools">
      <label>手牌 <select id="hand"></select></label>
      <label>变体 <select id="variant">
        <option value="a">A 左时间线 / 右热图</option>
        <option value="b">B 顶部时间线 / 全宽</option>
      </select></label>
      <label>空态 <select id="empty">
        <option value="">（真实数据）</option>
        <option value="off">未开启</option>
        <option value="none">无推导</option>
        <option value="preflop">翻前</option>
        <option value="skipped">已跳过</option>
        <option value="budget">停写 budget_exceeded</option>
        <option value="writefail">写盘失败 write_failed</option>
        <option value="missing">找不到 missing</option>
      </select></label>
    </div>
  </div>

  <div id="head"></div>
  <div id="advice"></div>
  <div class="chips" id="seatchips"></div>
  <div class="eqbox" id="eqbox"></div>
  <div id="main"></div>

  <details class="gaps">
    <summary>本原型暴露的 schema / 口径缺口（点开，5 条）</summary>
    <ol id="gaplist">
      <li><b>逐 combo 数据只有 top 榜</b>：若 T2 只存 <code>top_lost / top_gained</code>，点热图格子无法列出该格全部组合——中间步骤只能给出占比与少量样例。要支持「点格子筛 combo」，trace 需按 169 键存 <code>top_by_key</code>（每格 top 3），或提供按需重算接口。本原型点格子大部分会空，就是为了把这条摆出来。</li>
      <li><b>同一手牌并存两种口径</b>：没有翻后动作的座位走 <code>board_range</code> 旧路径（有宽度、没有逐事件 trace），有动作的座位走事件模型。看板必须逐座位标出口径来源，否则「A 家 22.6%、B 家 81.5%」看起来像同一把尺子量出来的。</li>
      <li><b>没有筹码量就没有 SPR</b>：hands 归档缺 <code>hero_stack</code> / 对手 <code>stack</code>，这次全部退化成 SPR=99（<code>spr_bucket</code> 恒为 high）。live 快照有 stack，但 T2 仍应把每个事件的 <code>spr</code> 与 <code>spr_bucket</code> 落盘，否则看板解释不了「为什么这一步压得这么狠」。</li>
      <li><b>没有 prior 版本的 diff</b>：换表前后对照需要 trace 带 <code>prior.id + prior.version + table_profile</code>，当前只有 <code>prior.id</code>。原型只能给单版本视图，diff 留 #9。</li>
      <li><b>没有「提示词那句话」的锚点</b>：equity 条要能跳到提示词引用它的那句，需要 trace/advice 侧提供 <code>prompt_ref</code>（段落 id 或行号），现在只能跳到整个「建议」tab。</li>
    </ol>
  </details>

  <details class="gaps">
    <summary>数据来源与生成方式（点开）</summary>
    <div class="note" style="border:0;padding:4px 0 0">
      真实手牌来自 <code>hands/1023.hands.json</code>；快照按「建议记录里的 pot」反推决策点时街上有几个动作，
      再用 <code>field_ranges.resolve</code> + 与 <code>range_flow.build_range</code> 同构的本地重放逐步留痕；
      权益来自 <code>hand_vs_ranges.lookup</code>。生成命令 <code>nv tools/proto_range_timeline.py</code>。
      <span id="gen"></span>
    </div>
  </details>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const RANKS = ['A','K','Q','J','T','9','8','7','6','5','4','3','2'];
const RANK_ORDER = Object.fromEntries(RANKS.map((r,i)=>[r,i]));
const RED = new Set(['h','d']);
const $ = (s, el=document) => el.querySelector(s);

const state = { hand: DATA.cases[0].hand_id, seat: null, step: -1, cell: '', variant: 'a', empty: '' };

function caseOf(id){ return DATA.cases.find(c=>c.hand_id===id) || DATA.cases[0]; }
function seatOf(c){ return c.seats.find(s=>s.seat_id===state.seat) || c.seats[0]; }
function stepsOf(seat){ return seat.replay ? seat.replay.steps : []; }
function currentStep(seat){
  const ss = stepsOf(seat);
  if (!ss.length) return null;
  return state.step >= 0 && state.step < ss.length ? ss[state.step] : ss[ss.length-1];
}
function cardEl(text, extra){
  const r = text.slice(0,-1), s = text.slice(-1);
  const sym = {s:'♠',h:'♥',d:'♦',c:'♣'}[s] || s;
  return '<span class="card'+(RED.has(s)?' red':'')+(extra||'')+'">'+r+sym+'</span>';
}
function pct(x, d){ return (x*100).toFixed(d===undefined?1:d)+'%'; }
function int(x){ return (x||0).toLocaleString('en-US'); }

function renderHead(){
  const c = caseOf(state.hand);
  $('#title').textContent = c.hero_position+' '+c.hero+' · '+c.board+' · '+c.street;
  $('#sub').textContent = c.title;
  const hero = c.hero.match(/../g).map(t=>cardEl(t)).join('');
  const board = c.board.match(/../g).map(t=>cardEl(t)).join('');
  const opp = c.seats.map(s=>(s.position||s.seat_id)).join(' / ');
  $('#head').innerHTML =
    '<div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:8px 0 2px">'+
      '<div style="display:flex;gap:4px">'+hero+'<span style="width:8px"></span>'+board+'</div>'+
      '<div class="sub">底池 '+c.pot.toFixed(1)+' BB · 需跟 '+c.to_call.toFixed(1)+' BB · '+
        c.table_size+'-max · 决策点 '+c.steps+' 个翻后动作已发生 · 对手 '+opp+'</div>'+
    '</div>';

  const chips = c.seats.map(s=>{
    const w = s.edge ? s.edge.eff_width_pct
      : (s.replay && s.replay.final ? s.replay.final.eff_width_pct : 0);
    return '<button class="chip '+(s.seat_id===state.seat?'on':'')+'" data-seat="'+s.seat_id+'"' +
      ' title="'+(s.prior_id||'range unknown')+'">'+
      '<b>'+(s.position||s.seat_id)+'</b> <span class="cw">'+w.toFixed(1)+'%</span>' +
      '<span class="cprior">'+s.seat_id+'</span></button>';
  });
  chips.push('<span class="chip hero">hero '+c.hero_position+' · '+c.hero+'</span>');
  $('#seatchips').innerHTML = chips.join('');
  $('#seatchips').querySelectorAll('[data-seat]').forEach(b=>{
    b.onclick = ()=> setState({seat:b.dataset.seat, step:-1, cell:''});
  });
}

function renderAdvice(){
  const c = caseOf(state.hand);
  const label = (c.advice.option||{}).label || '—';
  $('#advice').innerHTML =
    '<div class="advicebar">'+
      '<span class="alab">'+label+'</span>'+
      '<span class="areason">「'+String(c.advice.reason||'').replace(/["'」]+$/,'')+'」</span>'+
      '<span class="ameta">'+c.advice.source+' · '+c.advice.duration_ms+' ms</span>'+
    '</div>';
}

function renderEquity(){
  const c = caseOf(state.hand);
  const box = $('#eqbox');
  if (!c.equity){
    box.innerHTML = '<div class="sub">这一手没有范围权益（没有可确认的对手范围）。</div>';
    return;
  }
  const e = c.equity;
  const tie = Math.min(e.tie_any, e.equity);
  const win = Math.max(e.equity - tie/2, 0);
  const lose = Math.max(1 - win - tie, 0);
  const at = x => (Math.max(0,Math.min(1,x))*100).toFixed(2)+'%';
  box.innerHTML =
    '<div class="eqhead">'+
      '<span class="big">'+pct(e.equity)+'</span>'+
      '<span class="k">对手范围权益 ±'+Math.max(e.ci*100,.1).toFixed(1)+'%</span>'+
      '<span class="k">被至少一家压过 '+pct(e.behind_any)+'</span>'+
      '<span class="k">平局 '+pct(e.tie_any)+'</span>'+
      '<span class="k">'+int(e.samples)+' 次 MC</span>'+
      '<span class="k" style="margin-left:auto">'+(e.context||'')+'</span>'+
    '</div>'+
    '<div class="eqbar" title="胜 / 平 / 负 近似拆分（胜率含平局一半）">'+
      '<i class="lose" style="width:'+at(lose)+'"></i>'+
      '<i class="tie" style="left:'+at(lose)+';width:'+at(tie)+'"></i>'+
      '<i class="win" style="left:'+at(lose+tie)+';width:'+at(win)+'"></i>'+
      '<i class="whisk" style="left:'+at(e.equity-e.ci)+'"></i>'+
      '<i class="whisk" style="left:'+at(e.equity+e.ci)+'"></i>'+
    '</div>'+
    '<div class="eqlegend">'+
      '<span>▮ 负 '+pct(lose)+'</span><span style="color:var(--warn)">▮ 平 '+pct(tie)+'</span>'+
      '<span style="color:var(--good)">▮ 胜 '+pct(win)+'</span>'+
      '<span>白线 = 95% CI 两端 · 三段是按「胜率含平局一半」反推的近似</span>'+
      '<span style="margin-left:auto">范围权益 '+e.ms+' ms</span>'+
    '</div>';
}

function renderTimeline(seat){
  const ss = stepsOf(seat);
  const cur = currentStep(seat);
  const head = '<div class="phead"><b>推导时间线</b><span style="margin-left:auto">'+
    (ss.length ? ss.length+' 步 · 点步看该步热图' : '无逐事件 trace')+'</span></div>';
  if (!ss.length){
    const w = seat.legacy ? seat.legacy.eff_width_pct : (seat.edge?seat.edge.eff_width_pct:0);
    return '<div class="panel">'+head+
      '<div class="empty"><b>这位对手没有翻后动作</b>'+
        '事件模型按「每个翻后动作一步」生成 trace；他还没行动，所以没有逐步推导。'+
        '他的范围仍有宽度（<b>'+w.toFixed(1)+'%</b> 有效），但走的是 <code>board_range</code> 旧口径——'+
        '这正是缺口 #2：同一手牌里两种口径并存。</div></div>';
  }
  const items = ss.map((s,i)=>{
    const d = s.dropped || {};
    const seg = (v,color)=> v>0 ? '<i style="width:'+Math.min(v*100,100)+'%;background:'+color+'"></i>' : '';
    const dz = (d.collision||0)+(d.dead||0)+(d.floor||0);
    const zTxt = (s.index===0) ? '先验' : (s.z!=null && s.label.indexOf('final')!==0 ? 'Z='+Number(s.z).toFixed(2) : (s.label.indexOf('final')===0 ? '留存量 '+(s.mass*100).toFixed(1)+'%' : ''));
    const on = cur && cur.index===s.index;
    return '<button class="step '+(on?'on':'')+'" data-step="'+i+'">'+
      '<div class="lab"><span>'+s.label+'</span><span class="z">'+zTxt+'</span></div>'+
      '<div class="meta">'+
        '<span>宽度 '+s.width_pct.toFixed(1)+'% · 有效 '+s.eff_width_pct.toFixed(1)+'%</span>'+
        '<span>'+s.width+' combos</span>'+
        (s.board ? '<span>'+s.board+'</span>' : '')+
      '</div>'+
      (dz>0 ? '<div class="dbar">'+seg(d.collision,'#e0526b')+seg(d.dead,'#e5b567')+seg(d.floor,'#4aa3ff')+'</div>' : '')+
    '</button>';
  }).join('');
  return '<div class="panel">'+head+'<div class="tl">'+items+'</div>'+
    '<div class="note">柱：<span style="color:var(--bad)">红=牌面撞掉</span> '+
      '<span style="color:var(--warn)">黄=出牌</span> '+
      '<span style="color:var(--cool)">蓝=支撑集下限</span>'+
      (seat.replay ? ' · α='+seat.replay.alpha.toFixed(2)+' · floor '+(seat.replay.support_floor*100).toFixed(0)+'%' : '')+
    '</div></div>';
}

function heatGrid(seat){
  const step = currentStep(seat);
  if (step && Object.keys(step.grid||{}).length) return {grid: step.grid, label: step.label, legacy:false};
  if (!step && seat.legacy && Object.keys(seat.legacy.prior_grid||{}).length)
    return {grid: seat.legacy.prior_grid, label: '先验 '+seat.legacy.prior_id+'（对照）', legacy:true};
  return null;
}

function renderHeatmap(seat){
  const hg = heatGrid(seat);
  if (!hg){
    return '<div class="panel"><div class="phead"><b>169 热图</b></div>'+
      '<div class="empty">这一步没有组合留存，热图不画（避免出现一片 0%）。</div></div>';
  }
  const grid = hg.grid;
  const max = Math.max.apply(null, Object.values(grid).concat([1e-6]));
  const cells = [];
  for (const hi of RANKS){
    for (const lo of RANKS){
      const i = RANK_ORDER[hi], j = RANK_ORDER[lo];
      let key;
      if (i === j) key = hi+lo;
      else if (i < j) key = hi+lo+'s';
      else key = lo+hi+'o';
      const v = grid[key] || 0;
      const t = Math.sqrt(v/max);
      const bg = v <= 0 ? '#171c25'
        : 'hsl('+(18 + (1-t)*200)+','+(28 + t*45)+'%,'+(12 + t*46)+'%)';
      cells.push('<div class="cell'+(state.cell===key?' on':'')+(v<=0?' zero':'')+
        '" data-cell="'+key+'" style="background:'+bg+'" title="'+key+' '+pct(v,2)+'">'+
        '<span class="lbl" style="opacity:'+(v<=0||t<0.12?0:0.82)+'">'+key+'</span></div>');
    }
  }
  const curStep = currentStep(seat);
  const isFinal = state.step < 0 && seat.replay && seat.replay.final;
  let cellCount = 0;
  if (state.cell) {
    if (isFinal) cellCount = seat.replay.final.combos.filter(function(r){return r.key===state.cell;}).length;
    else {
      const seen = new Set();
      (curStep.top_lost||[]).concat(curStep.top_gained||[]).forEach(function(r){
        if (r.key===state.cell) seen.add(r.combo);
      });
      cellCount = seen.size;
    }
  }
  const cellChip = state.cell
    ? '<div class="cellchip"><b>'+state.cell+'</b>' +
        '<span class="cw">占本步 '+pct(grid[state.cell]||0,2)+'</span>' +
        '<span class="cw">'+(isFinal?'':'top 榜里 ')+cellCount+' 条组合</span>' +
        '<button data-clear="1" title="取消筛选">清除 ✕</button></div>'
    : '';
  return '<div class="panel"><div class="phead"><b>169 热图</b>'+
      '<span style="margin-left:auto">'+hg.label+' · 右上=同花 · 点格子筛组合</span></div>'+
    '<div class="hm"><div class="matrix">'+cells.join('')+'</div>'+
      '<div class="scale"><span>0</span><span class="ramp"></span><span>'+pct(max,1)+'</span>'+
        '<span style="margin-left:8px">按本步最大值归一 · √ 压缩</span></div>'+
      cellChip+'</div>'+(hg.legacy
      ? '<div class="note">这是<b>翻前先验</b>的对照图：这位对手在决策点前没有翻后动作，live 显示的是 board_range 旧口径宽度 '
        +seat.legacy.eff_width_pct.toFixed(1)+'%（先验本身 '+seat.legacy.width_pct.toFixed(1)+'%），两者不是同一把尺子。</div>'
      : '')+'</div>';
}

function rowsFor(seat){
  const step = currentStep(seat);
  if (!step) return {rows:[], full:false};
  if (state.step < 0 && seat.replay && seat.replay.final){
    return {rows: seat.replay.final.combos, full:true};
  }
  const seen = new Map();
  for (const r of (step.top_lost||[])) seen.set(r.combo, r);
  for (const r of (step.top_gained||[])) seen.set(r.combo, r);
  return {rows:Array.from(seen.values()), full:false};
}

function renderTable(seat){
  const step = currentStep(seat);
  const pack = rowsFor(seat);
  const rows = pack.rows, full = pack.full;
  if (!currentStep(seat)){
    return '<div class="panel"><div class="phead"><b>组合明细</b></div>'+
      '<div class="empty"><b>旧口径没有组合明细</b>'+
        'board_range 旧路径只产出宽度与 bucket 构成（价值/半诈/诈唬…），不产出逐组合权重——'+
        '这也是缺口 #1/#2 的同一件事：要「点格子看组合」，事件模型得覆盖每一个座位，而不只是行动过的座位。</div></div>';
  }
  let list = rows;
  if (state.cell) list = list.filter(r=>r.key===state.cell);
  const sorted = list.slice().sort(function(a,b){
    if (full) return b.share-a.share;
    return Math.abs(b.delta||0)-Math.abs(a.delta||0);
  }).slice(0, full?400:80);
  const cellShare = (step && step.grid) ? (step.grid[state.cell]||0) : 0;
  const changed = new Set([].concat(step.top_lost||[], step.top_gained||[]).map(function(r){return r.combo;})).size;
  const title = state.cell ? '格子 '+state.cell+' 的 '+sorted.length+' 条组合'
    : (full ? 'final 组合明细' : '本步变化最大的组合');
  const right = state.cell
    ? ('占本步 '+pct(cellShare,2)+(full?'':' · 只含 top 榜'))
    : (full ? '按权重降序 · 全部留存组合' : '按 |Δw| 降序 · 共 '+changed+' 个组合有变化');
  const head = '<div class="phead"><b>'+title+'</b>'+
    '<span style="margin-left:auto">'+right+'</span></div>';
  if (!sorted.length){
    return '<div class="panel">'+head+
      '<div class="empty"><b>'+(state.cell?'这个格子没有明细':'这里只显示 top 榜')+'</b>'+
        (state.cell
          ? state.cell+' 占 '+pct(cellShare,2)+'，但它的组合不在 top 榜里。支持「点格子筛 combo」需要 trace 按 169 键存每格 top 3（缺口 #1）。'
          : '每步只存 top_lost / top_gained 各 12 条。点热图格子看该格有哪些组合——大多数格子会空，这正是缺口 #1。选 final 步可看完整组合表。')+
      '</div></div>';
  }
  const body = sorted.map(function(r){
    const label = r.state==='dead' ? (full?'dead':'待删') : r.state;
    const st = r.state && r.state!=='keep' ? '<span class="tag '+r.state+'"' +
      (r.state==='dead'?' title="本步之后将被移除；final 才真正删掉"':'')+'>'+label+'</span>' : '';
    const d = r.delta==null ? '<span style="color:var(--dim)">—</span>'
      : '<span style="color:'+(r.delta>=0?'var(--good)':'var(--bad)')+'">'+(r.delta>=0?'+':'')+pct(r.delta,2)+'</span>';
    return '<tr><td>'+r.combo+'</td><td>'+r.key+'</td>'+
      '<td><span class="tag">'+(r.group||'—')+'</span></td>'+
      '<td>'+(r.made||'—')+'</td><td>'+(r.draw||'—')+'</td>'+
      '<td class="num">'+pct(r.share,2)+'</td>'+
      (full?'':'<td class="num">'+d+'</td><td>'+st+(r.cut?'<span class="tag dead">cut</span>':'')+'</td>')+'</tr>';
  }).join('');
  return '<div class="panel">'+head+
    '<div class="tblwrap"><table><thead><tr>'+
      '<th>组合</th><th>169</th><th>组</th><th>成牌</th><th>听牌</th><th>权重</th>'+
      (full?'':'<th>Δw</th><th>处置</th>')+
    '</tr></thead><tbody>'+body+'</tbody></table></div>'+
    '<div class="note">'+(state.cell
      ? '只显示格子 '+state.cell+' 的组合。'+(full?'':'中间步只含 top 榜，漏掉的组合需要 trace 按 169 键存 top_by_key（缺口 #1）。')
      : (full
        ? 'final 步是完整留存组合（最多 400 条），点格子可筛。'
        : '本步共 '+changed+' 个组合有变化，这里按 |Δw| 取前 '+sorted.length+' 条；Δw = 本步份额 − 上一步份额。'))+'</div></div>';
}

function renderOverview(){
  const c = caseOf(state.hand);
  // 各家共用一条 0..max 的横向量尺；竖线标出各步，条长是当前步（旧口径只有一个点）
  const series = c.seats.map(function(s){
    const ss = stepsOf(s);
    const vals = ss.length ? ss.map(x=>x.eff_width_pct)
      : [ (s.legacy ? s.legacy.eff_width_pct : (s.edge?s.edge.eff_width_pct:0)) ];
    const cur = currentStep(s);
    return {s:s, ss:ss, vals:vals,
            curIdx: cur ? Math.max(ss.findIndex(x=>x.index===cur.index),0) : (vals.length-1)};
  });
  const scale = Math.max.apply(null, series.flatMap(x=>x.vals).concat([1]));
  const rows = series.map(function(x){
    const s = x.s, legacy = !x.ss.length, v = x.vals[x.curIdx] || 0;
    const ticks = x.vals.map(function(t,i){
      return '<i class="ovtick" style="left:'+(Math.min(t/scale,1)*100).toFixed(2)+'%" title="'+
        (legacy?'旧口径':s.seat_id+' 第'+(i+1)+'步')+' '+t.toFixed(1)+'%"></i>';
    }).join('');
    return '<div class="ovrow'+(legacy?' legacy':'')+'" data-seat="'+s.seat_id+'" title="点一下切到这家">'+
      '<span class="nm'+(legacy?' legacy':'')+'">'+(s.position||s.seat_id)+' · '+s.seat_id+'</span>'+
      '<span class="ovtrack"><span class="ovfill" style="width:'+Math.max((v/scale)*100,1.5).toFixed(2)+'%"></span>'+ticks+'</span>'+
      '<span class="pc">'+v.toFixed(1)+'%</span></div>';
  }).join('');
  const axis = '<div class="ovax"><span>0</span><span>'+(scale*0.5).toFixed(1)+'%</span><span>'+scale.toFixed(1)+'%</span></div>';
  return '<div class="panel overview"><div class="phead" style="border:0;padding:0 0 6px">'+
    '<b>各家有效宽度</b><span style="margin-left:auto">'+
    (c.seats.some(s=>!s.replay)?'<span style="color:var(--warn)">▬ 旧口径</span> · ':'')+
    '竖线=各步</span></div>'+rows+axis+'</div>';
}

const EMPTY = {
  off: ['未开启范围推导', '这次录制启动时带了 --no-range-trace，或 meta.range_trace = off。'],
  none: ['这条记录没有推导', 'trace 里没有这个 decision_key 的行（可能录制早于采集上线，或当时被 skipped）。'],
  preflop: ['翻前不做推导', '事件模型只覆盖翻后：翻前由先入池先验覆盖，翻后每个动作才产生一步。'],
  skipped: ['这条决策被跳过', '推理链路判定不需要建模（例如对手已全下、或没有可确认的先验），没有落 trace。'],
  budget: ['今天已停写（budget_exceeded）', '当日写入超过 200 MB 上限，剩余决策只有 facts 旧视图，没有推导。'],
  writefail: ['写盘失败（write_failed）', '日志目录不可写；fail open，实时链路不受影响，但这一手没有推导。'],
  missing: ['找不到这条推导', '按 decision_key 查询返回空，且 meta 显示已开启——属于异常，需要查日志。']
};

function factsStrip(c){
  const rows = (c.facts||[]).map(function(f){
    return '<tr><td>'+f.street+'</td><td>'+(f.position||'')+'</td><td>'+f.who+'</td>'+
      '<td>'+f.action+(f.all_in?' <span class="tag dead">all-in</span>':'')+'</td>'+
      '<td class="num">'+(f.amount==null?'—':f.amount.toFixed(1)+' BB')+'</td></tr>';
  }).join('');
  return '<div class="phead"><b>facts 旧视图</b><span style="margin-left:auto">行动线 · 与建议同源，不需要 trace</span></div>'+
    '<div class="tblwrap" style="max-height:220px"><table><thead><tr>'+
      '<th>街</th><th>位置</th><th>座位</th><th>动作</th><th>金额</th></tr></thead><tbody>'+rows+
    '</tbody></table></div>'+
    '<div class="note">建议：<b>'+((c.advice.option||{}).label||'')+'</b> · 「'+(c.advice.reason||'')+'」</div>';
}

function renderEmptyView(){
  const pair = EMPTY[state.empty];
  const c = caseOf(state.hand);
  return '<div class="panel">'+
    '<div class="phead"><b>空态：'+pair[0]+'</b></div>'+
    '<div class="empty"><b>'+pair[0]+'</b>'+pair[1]+
      '<div style="margin-top:8px;color:var(--dim)">推导区换成这一句，facts 旧视图照常显示；不画空热图、不出现 0%。</div></div>'+
    '<div class="note">这是空态模拟器（URL 带 empty=），真实数据没被替换。</div></div>'+
    '<div class="panel" style="margin-top:14px">'+factsStrip(c)+'</div>';
}

function render(){
  const c = caseOf(state.hand);
  if (!state.seat || !c.seats.some(s=>s.seat_id===state.seat)){
    const focus = c.seats.find(s=>s.replay) || c.seats[0];
    state.seat = focus.seat_id;
  }
  renderHead();
  renderAdvice();
  renderEquity();
  const seat = seatOf(c);
  const main = state.variant==='a'
    ? '<div class="grid2">'+
        '<div class="leftcol">'+renderTimeline(seat)+renderOverview()+'</div>'+
        '<div class="right">'+renderHeatmap(seat)+renderTable(seat)+'</div></div>'
    : '<div class="grid2 flat">'+
        '<div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px">'+
          renderTimeline(seat)+renderHeatmap(seat)+'</div>'+
        '<div style="margin-top:16px">'+renderTable(seat)+'</div>'+
        '<div style="margin-top:16px">'+renderOverview()+'</div></div>';
  $('#main').innerHTML = state.empty ? renderEmptyView() : main;
  bind();
  syncHash();
  alignRight();
}

function alignRight(){
  const r = document.querySelector('.right');
  if (!r) return;
  const panels = r.querySelectorAll(':scope > .panel');
  if (panels.length < 2) return;
  const hm = panels[0], tb = panels[1];
  tb.style.height = ''; tb.style.maxHeight = '';
  if (window.innerWidth > 1080){
    const h = hm.getBoundingClientRect().height;
    if (h > 0){ tb.style.height = h+'px'; tb.style.maxHeight = h+'px'; }
  }
}
window.addEventListener('resize', function(){ alignRight(); });

function bind(){
  document.querySelectorAll('.step').forEach(function(b){
    b.onclick = ()=> setState({step: Number(b.dataset.step), cell:''});
  });
  document.querySelectorAll('.cell').forEach(function(b){
    b.onclick = ()=> setState({cell: state.cell===b.dataset.cell ? '' : b.dataset.cell});
  });
  document.querySelectorAll('.ovrow[data-seat]').forEach(function(b){
    b.onclick = ()=> setState({seat:b.dataset.seat, step:-1, cell:''});
  });
  document.querySelectorAll('[data-clear]').forEach(function(b){
    b.onclick = ()=> setState({cell:''});
  });
}

function setState(patch){ Object.assign(state, patch); render(); }

function syncHash(){
  const q = new URLSearchParams();
  q.set('hand', state.hand); q.set('seat', state.seat); q.set('step', String(state.step));
  if (state.cell) q.set('cell', state.cell);
  if (state.empty) q.set('empty', state.empty);
  if (state.variant !== 'a') q.set('variant', state.variant);
  history.replaceState(null, '', '#/range?'+q.toString());
}

function readHash(){
  const h = location.hash || '';
  if (h.indexOf('#/range') !== 0) return;
  const q = new URLSearchParams(h.slice(h.indexOf('?')+1));
  const c = caseOf(q.get('hand')||'');
  state.hand = c.hand_id;
  if (q.get('seat') && c.seats.some(s=>s.seat_id===q.get('seat'))) state.seat = q.get('seat');
  const st = parseInt(q.get('step')||'-1', 10);
  state.step = isNaN(st) ? -1 : st;
  state.cell = q.get('cell') || '';
  const em = q.get('empty') || '';
  state.empty = EMPTY[em] ? em : '';
  state.variant = q.get('variant') === 'b' ? 'b' : 'a';
}

const handSel = $('#hand');
handSel.innerHTML = DATA.cases.map(c=>'<option value="'+c.hand_id+'">'+
  c.hand_id+' · '+c.hero_position+' '+c.hero+' · '+c.board+'</option>').join('');
handSel.onchange = function(e){
  const c = caseOf(e.target.value);
  setState({hand:c.hand_id, seat:c.seats[0].seat_id, step:-1, cell:''});
};
$('#variant').onchange = function(e){ state.variant = e.target.value; render(); };
$('#empty').onchange = function(e){ state.empty = e.target.value; render(); };
$('#gen').textContent = '生成于 '+DATA.generated_at;
readHash();
handSel.value = state.hand;
$('#variant').value = state.variant;
$('#empty').value = state.empty;
render();
window.addEventListener('hashchange', function(){ readHash(); render(); });
</script>
</body>
</html>
