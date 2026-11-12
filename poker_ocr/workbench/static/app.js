'use strict';

/* 工作台前端：无构建步骤的原生 ES2020。
   页面结构：#/ 工作区列表 · #/w/<id>[/config|preflight|history] 详情 ·
            #/w/<id>/run 运行页 · #/w/<id>/calibrate 标定页。 */

const TOKEN = window.__WB_TOKEN__ || '';
const rootEl = document.getElementById('app');
const modalEl = document.getElementById('modal');
const toastEl = document.getElementById('toast');
const topStatusEl = document.getElementById('top-status');

const GROUPS = [
  ['seats', '座位'], ['actions', '动作'], ['cards', '牌面'],
  ['pots', '底池与盲注'], ['buttons', '按钮'], ['sliders', '滑杆'],
];
const GROUP_COLORS = {
  seats: '#569cff', actions: '#ffc454', cards: '#58d68d',
  pots: '#c48cff', buttons: '#00aaff', sliders: '#ff78a0',
};
const STATUS_TEXT = {
  idle: '空闲', starting: '启动中', running: '运行中',
  paused_click: '点击已暂停', failed: '失败', stopping: '停止中',
  calibrating: '标定中', applying: '应用中',
};

let timers = [];
let toastTimer = null;

/* ─────────────────────────── 基础工具 ─────────────────────────── */

class ApiError extends Error {
  constructor(status, data) {
    super((data && (data.error || data.detail)) || `HTTP ${status}`);
    this.status = status;
    this.data = data || {};
  }
}

async function api(path, opts = {}) {
  const { method = 'GET', body } = opts;
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET') headers['X-Workbench-Token'] = TOKEN;
  const resp = await fetch(path, {
    method, headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: text }; }
  if (!resp.ok) throw new ApiError(resp.status, data || {});
  return data;
}

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtDuration(sec) {
  if (sec == null || Number.isNaN(Number(sec))) return '-';
  sec = Math.max(0, Math.round(Number(sec)));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h) return `${h}h${String(m).padStart(2, '0')}m`;
  return `${m}m${String(sec % 60).padStart(2, '0')}s`;
}

function pct(value) {
  if (value == null || Number.isNaN(Number(value))) return '-';
  return `${(Number(value) * 100).toFixed(0)}%`;
}

function toast(message, kind = 'info') {
  toastEl.textContent = message;
  toastEl.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.className = 'toast'; }, 3600);
}

function setTopStatus(text, cls) {
  topStatusEl.textContent = text;
  topStatusEl.className = `pill pill-${cls || 'idle'}`;
}

function clearTimers() {
  timers.forEach((t) => { clearTimeout(t); clearInterval(t); });
  timers = [];
}

function timer(fn, ms, repeat = true) {
  const id = repeat ? setInterval(fn, ms) : setTimeout(fn, ms);
  timers.push(id);
  return id;
}

function statusPill(state) {
  const cls = ['running', 'starting', 'failed', 'paused_click', 'stopping',
    'calibrating', 'applying'].includes(state) ? state : 'idle';
  return `<span class="pill pill-${cls}">${esc(STATUS_TEXT[state] || state || '空闲')}</span>`;
}

function btn(label, action, cls = 'btn', extra = '') {
  return `<button class="${cls}" data-action="${esc(action)}" ${extra}>${label}</button>`;
}

function bindActions(container, handlers) {
  container.querySelectorAll('[data-action]').forEach((el) => {
    el.addEventListener('click', async (ev) => {
      ev.preventDefault();
      const fn = handlers[el.dataset.action];
      if (!fn) return;
      el.disabled = true;
      try { await fn(el, ev); } catch (err) { toast(err.message, 'err'); }
      finally { el.disabled = false; }
    });
  });
}

/* ─────────────────────────── 弹窗 ─────────────────────────── */

let modalResolve = null;

function openModal(html) {
  modalEl.innerHTML = html;
  if (!modalEl.open) modalEl.showModal();
  return modalEl;
}

function closeModal() {
  if (modalEl.open) modalEl.close();
  modalEl.innerHTML = '';
}

modalEl.addEventListener('close', () => {
  if (modalResolve) {
    const resolve = modalResolve;
    modalResolve = null;
    resolve(false);
  }
});

modalEl.addEventListener('click', (ev) => {
  if (ev.target === modalEl) closeModal();
});

function confirmDialog(title, bodyHtml, okLabel = '确定') {
  return new Promise((resolve) => {
    modalResolve = resolve;
    openModal(`
      <div class="modal-head"><h2>${esc(title)}</h2>
        <button class="close-x" data-close>×</button></div>
      <div class="modal-body">${bodyHtml}</div>
      <div class="modal-foot">
        <button class="btn" data-close>取消</button>
        <button class="btn btn-primary" data-ok>${esc(okLabel)}</button>
      </div>`);
    const finish = (value) => {
      if (modalResolve === resolve) modalResolve = null;
      closeModal(); resolve(value);
    };
    modalEl.querySelectorAll('[data-close]').forEach((el) =>
      el.addEventListener('click', () => finish(false)));
    modalEl.querySelector('[data-ok]').addEventListener('click', () => finish(true));
  });
}

async function pickWindow(onPick) {
  try {
    const { windows, error } = await api('/api/sources/windows');
    if (error) { toast(error, 'err'); return; }
    if (!windows.length) { toast('没有可枚举的窗口（需要 swift/屏幕录制权限）', 'err'); return; }
    openModal(`
      <div class="modal-head"><h2>选择游戏窗口</h2>
        <button class="close-x" data-close>×</button></div>
      <div class="modal-body">
        <p class="hint">点一行填入；推荐 <code>id:N</code>（标签页标题变了也不影响，窗口关闭/浏览器重启后会变）。</p>
        <div style="margin-top:10px">${windows.map((w) => `
          <div class="layer-item" data-pick="${esc(w.window_id
            ? `id:${w.window_id}`
            : (w.title ? `title:${w.title}` : `index:${w.index}`))}">
            <span>[${w.index}] ${esc(w.owner)} | ${esc(w.title || '无标题')}
              <span class="faint">id=${w.window_id}</span></span>
            <span class="faint">${w.w}×${w.h}</span>
          </div>`).join('')}</div>
      </div>
      <div class="modal-foot"><button class="btn" data-close>取消</button></div>`);
    modalEl.querySelectorAll('[data-close]').forEach((el) =>
      el.addEventListener('click', closeModal));
    modalEl.querySelectorAll('[data-pick]').forEach((row) =>
      row.addEventListener('click', () => { onPick(row.dataset.pick); closeModal(); }));
  } catch (err) { toast(err.message, 'err'); }
}

/* ─────────────────────────── 路由 ─────────────────────────── */

function parseRoute() {
  const raw = decodeURI(location.hash.replace(/^#\/?/, ''));
  const parts = raw.split('/').filter(Boolean);
  if (!parts.length) return { page: 'list' };
  if (parts[0] === 'w' && parts[1]) {
    return { page: parts[2] || 'overview', id: parts[1] };
  }
  return { page: 'list' };
}

async function route() {
  clearTimers();
  clearTimeout(calState.trialTimer);
  const r = parseRoute();
  try {
    if (r.page === 'list') { setTopStatus('工作台', 'idle'); await pageList(); }
    else if (r.page === 'run') await pageRun(r.id);
    else if (r.page === 'calibrate') await pageCalibrate(r.id);
    else await pageWorkspace(r.id, r.page);
  } catch (err) {
    console.error(err);
    rootEl.innerHTML = `<div class="error-box">页面加载失败：${esc(err.message)}</div>`;
    toast(err.message, 'err');
  }
}

window.addEventListener('hashchange', route);
window.addEventListener('DOMContentLoaded', route);

let staleToastShown = false;
async function checkBackendStale() {
  try {
    const health = await api('/api/health');
    const banner = document.getElementById('stale-banner');
    if (banner) banner.hidden = !health.stale;
    if (health.stale && !staleToastShown) {
      staleToastShown = true;
      toast('后端代码已更新：请重启工作台进程再启动/保存', 'err');
    }
  } catch (err) { /* 健康检查失败不打扰页面 */ }
}
setInterval(checkBackendStale, 15000);
checkBackendStale();

/* ─────────────────────────── 工作区列表 ─────────────────────────── */

function workspaceCard(ws) {
  const last = ws.last_run;
  const profile = ws.profile_info || {};
  const windowSize = (ws.window && ws.window.expected_size || []).join('×') || '未设窗口';
  const stats = last
    ? `${last.status === 'idle' ? '正常' : (STATUS_TEXT[last.status] || last.status)}`
      + ` · ${fmtDuration(last.duration_seconds)}`
      + ` · 丢弃 ${last.discard_rate == null ? '-' : pct(last.discard_rate)}`
    : '还没有运行记录';
  const badges = [
    ws.running ? '<span class="badge badge-ok">运行中</span>' : '',
    ws.errors && ws.errors.length ? '<span class="badge badge-fail">配置错误</span>' : '',
    ws.warnings && ws.warnings.length ? '<span class="badge badge-warn">有警告</span>' : '',
    profile.seats ? `<span class="badge">${profile.seats} 座</span>` : '',
  ].join(' ');
  return `
    <article class="card card-click" data-open="${esc(ws.id)}">
      <div class="card-head">
        <div>
          <div class="card-title">${esc(ws.name || ws.id)}</div>
          <div class="card-sub">${esc(ws.profile)} · ${esc(ws.source && ws.source.spec || '未选源')}</div>
        </div>
        <div class="inline">${badges}</div>
      </div>
      <dl class="kv">
        <dt>窗口</dt><dd>${esc(windowSize)} · ${esc((ws.window && ws.window.pin) || 'manual')}</dd>
        <dt>上次</dt><dd>${esc(last ? `${fmtTime(last.started_at)} ${stats}` : stats)}</dd>
      </dl>
      <div class="card-foot">
        <button class="btn btn-sm" data-copy="${esc(ws.id)}">复制</button>
        <button class="btn btn-sm btn-danger" data-del="${esc(ws.id)}">删除</button>
      </div>
    </article>`;
}

async function pageList() {
  const [{ workspaces }, { profiles }] = await Promise.all([
    api('/api/workspaces'), api('/api/profiles'),
  ]);
  rootEl.innerHTML = `
    <div class="page-head">
      <div>
        <h1>工作区</h1>
        <div class="sub">一次启动所需的一切：profile、源、窗口、点击、运行参数。一次只跑一路。</div>
      </div>
      <div class="actions">
        <button class="btn" id="import-ws">导入 JSON</button>
        <button class="btn btn-primary" id="new-ws">+ 从 profile 新建</button>
      </div>
    </div>
    <div id="list-body">${workspaces.length ? '' : '<div class="empty">还没有工作区。点右上角「从 profile 新建」。</div>'}</div>
    <div class="cards" id="ws-cards"></div>
    <div class="section">
      <div class="section-head"><h2>发现的 profile</h2>
        <span class="muted">config/table_*.json</span></div>
      <div class="panel"><div class="panel-body tight"><table class="table">
        <thead><tr><th>profile</th><th>参考尺寸</th><th>座位</th><th>点击</th><th>滑杆</th><th>校验</th></tr></thead>
        <tbody>${profiles.map((p) => `
          <tr><td class="mono">${esc(p.path)}</td>
            <td>${p.reference_size ? p.reference_size.join('×') : '-'}</td>
            <td>${p.seats ?? 0}</td>
            <td>${p.clickable ? '<span class="badge badge-ok">可点</span>' : '<span class="badge">—</span>'}</td>
            <td>${p.slider ? '<span class="badge badge-ok">可滑杆</span>' : '<span class="badge">—</span>'}</td>
            <td>${p.ok ? '<span class="badge badge-ok">通过</span>'
              : `<span class="badge badge-fail">${esc((p.problems || [])[0] || '失败')}</span>`}</td>
          </tr>`).join('')}</tbody>
      </table></div></div>
    </div>`;

  const cards = document.getElementById('ws-cards');
  cards.innerHTML = workspaces.map(workspaceCard).join('');
  document.getElementById('list-body').style.display = workspaces.length ? 'none' : '';

  cards.querySelectorAll('[data-open]').forEach((card) => {
    card.addEventListener('click', (ev) => {
      if (ev.target.closest('[data-copy],[data-del]')) return;
      location.hash = `#/w/${card.dataset.open}`;
    });
  });
  cards.querySelectorAll('[data-copy]').forEach((el) =>
    el.addEventListener('click', async () => {
      try {
        const doc = await api(`/api/workspaces/${el.dataset.copy}/duplicate`,
          { method: 'POST', body: {} });
        toast(`已复制为 ${doc.name}`, 'ok');
        route();
      } catch (err) { toast(err.message, 'err'); }
    }));
  cards.querySelectorAll('[data-del]').forEach((el) =>
    el.addEventListener('click', async () => {
      const ok = await confirmDialog('删除工作区',
        `确定删除 <b>${esc(el.dataset.del)}</b>？工作区文件会从 workspaces/ 移除，profile 不受影响。`,
        '删除');
      if (!ok) return;
      try {
        await api(`/api/workspaces/${el.dataset.del}`, { method: 'DELETE' });
        toast('已删除', 'ok');
        route();
      } catch (err) { toast(err.message, 'err'); }
    }));

  document.getElementById('new-ws').addEventListener('click',
    () => openNewWorkspace(profiles));
  document.getElementById('import-ws').addEventListener('click', openImportWorkspace);
}

function openNewWorkspace(profiles) {
  const options = profiles.map((p) =>
    `<option value="${esc(p.path)}" ${p.ok ? '' : 'disabled'}>${esc(p.path)}`
    + `${p.ok ? '' : '（校验失败）'}</option>`).join('');
  openModal(`
    <div class="modal-head"><h2>从 profile 新建工作区</h2>
      <button class="close-x" data-close>×</button></div>
    <div class="modal-body">
      <div class="form">
        <div class="field"><label>profile</label>
          <select id="nw-profile">${options || '<option value="">没有可用 profile</option>'}</select>
          <div class="hint">工作区只引用它；标定会直接改这个文件（自动备份）。</div>
        </div>
        <div class="field"><label>名称</label>
          <input class="input" id="nw-name" placeholder="例如 WePoker Mac 实盘">
        </div>
        <div class="row">
          <div class="field"><label>源类型</label>
            <select id="nw-kind">
              <option value="window">window（截浏览器窗口）</option>
              <option value="camera">camera（OBS 虚拟摄像头）</option>
              <option value="rtsp">rtsp（MediaMTX）</option>
              <option value="file">file（视频/图片）</option>
            </select>
          </div>
          <div class="field"><label>源 spec</label>
            <input class="input" id="nw-spec" placeholder="title:WePoker-H5">
          </div>
        </div>
        <div class="field"><label>游戏窗口（滑杆点击/浮层；窗口源留空=跟随源）</label>
          <div class="inline">
            <input class="input" id="nw-window-spec" placeholder="title:WePoker-H5"
              style="flex:1">
            <button class="btn btn-sm" id="nw-pick-window">枚举窗口</button>
          </div>
          <div class="hint">OBS/摄像头/RTSP/文件源不会自动带窗口；滑杆点击按屏幕坐标落到游戏浏览器窗口。</div>
        </div>
        <div class="inline">
          <button class="btn btn-sm" id="nw-list">枚举窗口</button>
          <button class="btn btn-sm" id="nw-cams">枚举摄像头</button>
          <button class="btn btn-sm" id="nw-probe">测试源</button>
          <span class="hint" id="nw-hint">写法：title:... / index:N / x,y,w,h / cam:auto / rtsp://...</span>
        </div>
        <div class="hint" id="nw-probe-result"></div>
        <div id="nw-windows"></div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" data-close>取消</button>
      <button class="btn btn-primary" id="nw-create">创建工作区</button>
    </div>`);
  modalEl.querySelectorAll('[data-close]').forEach((el) =>
    el.addEventListener('click', closeModal));
  document.getElementById('nw-list').addEventListener('click', async () => {
    const box = document.getElementById('nw-windows');
    box.innerHTML = '<div class="hint">枚举中…</div>';
    try {
      const { windows, error } = await api('/api/sources/windows');
      if (error) { box.innerHTML = `<div class="error-box">${esc(error)}</div>`; return; }
      box.innerHTML = `<div class="hint" style="margin:6px 0">点一行填入 spec</div>`
        + `<div class="table" style="font-size:12px">${windows.map((w) => `
          <div class="layer-item" data-win="${esc(`title:${w.title}`)}">
            <span>[${w.index}] ${esc(w.owner)} | ${esc(w.title)}</span>
            <span class="faint">${w.w}×${w.h}</span></div>`).join('')}</div>`;
      box.querySelectorAll('[data-win]').forEach((row) => row.addEventListener('click', () => {
        document.getElementById('nw-spec').value = row.dataset.win;
      }));
    } catch (err) {
      box.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
    }
  });
  document.getElementById('nw-cams').addEventListener('click', async () => {
    const box = document.getElementById('nw-windows');
    box.innerHTML = '<div class="hint">枚举中…</div>';
    try {
      const { cameras, error } = await api('/api/sources/cameras');
      if (error) { box.innerHTML = `<div class="error-box">${esc(error)}</div>`; return; }
      box.innerHTML = `<div class="hint" style="margin:6px 0">点一行填入 spec</div>`
        + cameras.map((c) => `<div class="layer-item" data-win="cam:${c.index}">
            <span>[${c.index}] ${esc(c.name || '未命名')}${c.preferred ? ' · 优先' : ''}</span>
            <span class="faint">cam:${c.index}</span></div>`).join('');
      box.querySelectorAll('[data-win]').forEach((row) => row.addEventListener('click', () => {
        document.getElementById('nw-spec').value = row.dataset.win;
        document.getElementById('nw-kind').value = 'camera';
      }));
    } catch (err) { box.innerHTML = `<div class="error-box">${esc(err.message)}</div>`; }
  });
  document.getElementById('nw-probe').addEventListener('click', async () => {
    const out = document.getElementById('nw-probe-result');
    const spec = document.getElementById('nw-spec').value.trim();
    out.textContent = '探测中（可能打开一次源）…';
    try {
      const res = await api('/api/sources/probe', { method: 'POST', body: { spec } });
      out.textContent = res.ok
        ? `出帧 ${res.size ? res.size.join('×') : '?'} · ${res.kind} · ${res.elapsed_ms}ms`
        : `探测失败：${res.error}`;
      out.className = res.ok ? 'hint green' : 'hint red';
    } catch (err) {
      out.textContent = `探测失败：${err.message}`;
      out.className = 'hint red';
    }
  });
  document.getElementById('nw-pick-window').addEventListener('click', () => {
    pickWindow((spec) => { document.getElementById('nw-window-spec').value = spec; });
  });
  document.getElementById('nw-create').addEventListener('click', async (ev) => {
    const profile = document.getElementById('nw-profile').value;
    const name = document.getElementById('nw-name').value.trim();
    const kind = document.getElementById('nw-kind').value;
    const spec = document.getElementById('nw-spec').value.trim();
    if (!profile) { toast('先选一个 profile', 'err'); return; }
    ev.target.disabled = true;
    try {
      const doc = await api('/api/workspaces', {
        method: 'POST',
        body: {
          from_profile: profile, name: name || undefined,
          source: { kind, spec, label: spec },
          window: { spec:
            document.getElementById('nw-window-spec').value.trim() || null },
        },
      });
      closeModal();
      toast('工作区已创建', 'ok');
      location.hash = `#/w/${doc.id}`;
    } catch (err) {
      toast(err.message, 'err');
    } finally { ev.target.disabled = false; }
  });
}

function openImportWorkspace() {
  openModal(`
    <div class="modal-head"><h2>导入工作区 JSON</h2>
      <button class="close-x" data-close>×</button></div>
    <div class="modal-body">
      <div class="field"><label>粘贴工作区 JSON（schema 1）</label>
        <textarea id="import-json" placeholder='{"schema":1,"id":"...","profile":"config/table_wpk_mac.json",...}'></textarea>
        <div class="hint">会按 schema 校验；profile 必须存在。</div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" data-close>取消</button>
      <button class="btn btn-primary" id="import-go">导入</button>
    </div>`);
  modalEl.querySelectorAll('[data-close]').forEach((el) =>
    el.addEventListener('click', closeModal));
  document.getElementById('import-go').addEventListener('click', async (ev) => {
    let body;
    try { body = JSON.parse(document.getElementById('import-json').value); }
    catch (err) { toast('JSON 解析失败：' + err.message, 'err'); return; }
    ev.target.disabled = true;
    try {
      const doc = await api('/api/workspaces', { method: 'POST', body });
      closeModal();
      toast('已导入', 'ok');
      location.hash = `#/w/${doc.id}`;
    } catch (err) { toast(err.message, 'err'); }
    finally { ev.target.disabled = false; }
  });
}

/* ─────────────────────────── 工作区详情 ─────────────────────────── */

async function pageWorkspace(id, tab) {
  const ws = await api(`/api/workspaces/${id}`);
  const status = await api(`/api/workspaces/${id}/status`).catch(() => null);
  const state = status ? status.state : 'idle';
  setTopStatus(STATUS_TEXT[state] || state, state);
  const tabs = [['overview', '概览'], ['config', '配置'], ['preflight', '体检'],
    ['history', '历史']];
  rootEl.innerHTML = `
    <div class="crumb"><a href="#/">工作区</a> / ${esc(ws.name || id)}</div>
    <div class="page-head">
      <div>
        <h1>${esc(ws.name || id)} ${statusPill(state)}</h1>
        <div class="sub mono">${esc(ws.profile)} · ${esc(ws.source && ws.source.spec || '未选源')}</div>
      </div>
      <div class="actions">
        <a class="btn" href="#/w/${esc(id)}/run">运行页</a>
        <a class="btn" href="#/w/${esc(id)}/calibrate">标定</a>
        ${state === 'running' || state === 'paused_click' || state === 'starting'
          ? btn('停止', 'stop', 'btn btn-danger')
          : btn('启动', 'start', 'btn btn-primary')}
      </div>
    </div>
    <div class="tabs">${tabs.map(([key, label]) =>
      `<a class="tab ${key === tab ? 'active' : ''}" href="#/w/${esc(id)}/${key}">${label}</a>`).join('')}</div>
    <div id="tab-body"><div class="empty"><span class="spinner"></span> 加载中…</div></div>`;

  bindActions(rootEl, {
    start: async () => startWorkspace(id),
    stop: async () => {
      await api(`/api/workspaces/${id}/stop`, { method: 'POST' });
      toast('已停止', 'ok');
      route();
    },
  });

  const body = document.getElementById('tab-body');
  if (tab === 'config') await renderConfigTab(body, ws);
  else if (tab === 'preflight') await renderPreflightTab(body, ws);
  else if (tab === 'history') await renderHistoryTab(body, ws);
  else renderOverviewTab(body, ws, status);
}

function renderOverviewTab(body, ws, status) {
  const meta = status && status.meta || {};
  const win = meta.source && meta.source.window;
  const run = status && status.run;
  body.innerHTML = `
    <div class="grid-2">
      <section class="card">
        <div class="card-head"><h2>运行配方</h2>${statusPill(status ? status.state : 'idle')}</div>
        <dl class="kv">
          <dt>profile</dt><dd class="mono">${esc(ws.profile)}<br>
            <span class="faint">${ws.profile_info && ws.profile_info.reference_size
              ? ws.profile_info.reference_size.join('×') + ' · ' + (ws.profile_info.seats || 0) + ' 座' : ''}</span></dd>
          <dt>视频源</dt><dd class="mono">${esc(ws.source && ws.source.spec || '-')}</dd>
          <dt>窗口</dt><dd>期望 ${esc((ws.window.expected_size || []).join('×') || '未设置')}
            · ${esc(ws.window.pin || 'manual')}
            ${btn('固定窗口', 'pin', 'btn btn-sm')}</dd>
          <dt>点击</dt><dd>${ws.click.enabled ? '开' : '关'}
            · ${ws.click.dry_run ? 'dry-run' : '真实点击'}
            · ${ws.click.confirm ? '每次确认' : '直接执行'}
            · ${ws.click.bet_slider ? '滑杆优先' : '固定档位'}</dd>
          <dt>游戏窗口</dt><dd>${ws.window.spec
            ? `<span class="mono">${esc(ws.window.spec)}</span>`
            : (ws.source.kind === 'window'
              ? '<span class="faint">跟随源：' + esc(ws.source.spec) + '</span>'
              : '<span class="yellow">未设置（非窗口源需指定）</span>')}</dd>
          <dt>运行</dt><dd>盲注 ${esc(ws.runtime.initial_blinds || '自动')}
            · every ${ws.runtime.every}s · AI ${ws.runtime.ai_advice ? '开' : '关'}</dd>
          <dt>调试</dt><dd>预览 ${ws.debug.preview_fps}fps · 浮层 ${ws.debug.screen_overlay ? '开' : '关'}</dd>
        </dl>
        <div class="sep"></div>
        <div class="inline">
          ${run ? `<span class="mono faint">${esc(run.run_id)} · :${run.port}</span>
            <a class="btn btn-sm" href="${esc(status.live_url || '#')}" target="_blank" rel="noreferrer">打开完整看板</a>` : ''}
        </div>
      </section>
      <section class="card">
        <div class="card-head"><h2>识别健康</h2></div>
        ${status && status.state !== 'idle' ? `
          <dl class="kv">
            <dt>帧</dt><dd>#${esc((status.frame || {}).seq || '-')} ·
              ${esc((status.frame || {}).width || '-')}×${esc((status.frame || {}).height || '-')}</dd>
            <dt>接受率</dt><dd>${pct(rate(status.stats))}</dd>
            <dt>平均耗时</dt><dd>${esc(status.stats && status.stats.avg_proc_ms || '-')} ms/帧</dd>
            <dt>点击</dt><dd>${status.click && status.click.available
              ? (status.click.paused ? '<span class="yellow">已暂停</span>' : '启用')
              : '<span class="faint">未启用</span>'}</dd>
            <dt>窗口</dt><dd class="mono">${win ? `${Math.round(win.x)},${Math.round(win.y)},${Math.round(win.w)},${Math.round(win.h)}` : '-'}</dd>
          </dl>` : '<div class="empty">当前没有运行。去「体检」后启动。</div>'}
        <div class="sep"></div>
        <div class="inline">
          <button class="btn btn-sm" data-go-run>运行页 · 实时预览</button>
          <button class="btn btn-sm" data-go-pre>启动前体检</button>
        </div>
      </section>
    </div>
    <section class="section">
      <div class="section-head"><h2>最近的运行</h2>
        <a href="#/w/${esc(ws.id)}/history" class="muted">全部 →</a></div>
      <div class="panel"><div class="panel-body tight">
        ${renderRunsTable((ws.runs || []).slice(0, 5))}
      </div></div>
    </section>`;

  bindActions(body, {
    pin: async () => {
      try {
        const res = await api(`/api/workspaces/${ws.id}/pin-window`, { method: 'POST' });
        toast(`已记录窗口 ${Math.round(res.window.w)}×${Math.round(res.window.h)}`, 'ok');
        route();
      } catch (err) { toast(err.message, 'err'); }
    },
  });
  body.querySelector('[data-go-run]').addEventListener('click',
    () => { location.hash = `#/w/${ws.id}/run`; });
  body.querySelector('[data-go-pre]').addEventListener('click',
    () => { location.hash = `#/w/${ws.id}/preflight`; });
  bindManifestButtons(body);
}

function rate(stats) {
  if (!stats || !stats.processed) return null;
  return (stats.accepted || 0) / stats.processed;
}

function renderRunsTable(runs) {
  if (!runs || !runs.length) return '<div class="empty">还没有运行记录</div>';
  return `<table class="table table-hover"><thead><tr>
    <th>开始</th><th>状态</th><th>时长</th><th>帧</th><th>丢弃率</th>
    <th>退出码</th><th></th></tr></thead><tbody>${runs.map((r) => `
      <tr><td class="nowrap">${esc(fmtTime(r.started_at))}</td>
        <td>${statusPill(r.status)}</td>
        <td>${esc(fmtDuration(r.duration_seconds))}</td>
        <td>${esc(r.processed ?? '-')}</td>
        <td>${r.discard_rate == null ? '-' : pct(r.discard_rate)}</td>
        <td>${esc(r.exit_code ?? '-')}</td>
        <td><button class="btn btn-sm" data-manifest="${esc(r.run_id)}">manifest</button></td>
      </tr>`).join('')}</tbody></table>`;
}

function bindManifestButtons(container) {
  container.querySelectorAll('[data-manifest]').forEach((el) =>
    el.addEventListener('click', async () => {
      try {
        const doc = await api(`/api/runs/${el.dataset.manifest}/manifest`);
        openModal(`
          <div class="modal-head"><h2>运行 ${esc(doc.run_id)}</h2>
            <button class="close-x" data-close>×</button></div>
          <div class="modal-body"><pre class="log">${esc(JSON.stringify(doc, null, 2))}</pre></div>
          <div class="modal-foot"><button class="btn" data-close>关闭</button></div>`);
        modalEl.querySelectorAll('[data-close]').forEach((x) =>
          x.addEventListener('click', closeModal));
      } catch (err) { toast(err.message, 'err'); }
    }));
}

async function renderConfigTab(body, ws) {
  const { profiles } = await api('/api/profiles');
  const profileOptions = profiles.map((p) =>
    `<option value="${esc(p.path)}" ${p.path === ws.profile ? 'selected' : ''}>${esc(p.path)}</option>`).join('');
  const groupChecks = GROUPS.map(([key, label]) =>
    `<label class="checkbox"><input type="checkbox" data-og="${key}"
      ${(ws.debug.overlay_groups || []).includes(key) ? 'checked' : ''}>${label}</label>`).join('');
  const g = (obj, key, fallback = '') => (obj && obj[key] != null ? obj[key] : fallback);
  body.innerHTML = `
    <form class="card" id="cfg-form">
      <div class="grid-2">
        <fieldset><legend>基础</legend><div class="form">
          <div class="field"><label>名称</label>
            <input class="input" name="name" value="${esc(ws.name)}"></div>
          <div class="field"><label>profile（引用，不复制）</label>
            <select name="profile">${profileOptions}</select></div>
          <div class="row">
            <div class="field"><label>源类型</label>
              <select name="source_kind">${['window', 'camera', 'rtsp', 'file'].map((k) =>
                `<option ${k === ws.source.kind ? 'selected' : ''}>${k}</option>`).join('')}</select>
            </div>
            <div class="field"><label>源 spec</label>
              <div class="inline">
                <input class="input" name="source_spec" value="${esc(ws.source.spec)}"
                  placeholder="title:WePoker-H5" style="flex:1">
                <button class="btn btn-sm" type="button" id="cfg-probe">测试源</button>
              </div>
              <div class="hint" id="cfg-probe-result"></div></div>
          </div>
          <div class="field"><label>识别方式</label>
            <select name="recognizer">
              <option value="roi" ${ws.recognizer === 'roi' ? 'selected' : ''}>roi（固定槽位）</option>
              <option value="full" ${ws.recognizer === 'full' ? 'selected' : ''}>full（全图，只保证启动）</option>
            </select></div>
          <div class="field"><label>full 配置（recognizer=full 时必填）</label>
            <input class="input" name="full_config" value="${esc(ws.full_config || '')}"></div>
        </div></fieldset>

        <fieldset><legend>窗口（滑杆点击 / 浮层）</legend><div class="form">
          <div class="field"><label>游戏窗口</label>
            <div class="inline">
              <input class="input" name="window_spec"
                value="${esc(ws.window.spec || '')}"
                placeholder="title:WePoker-H5 / index:2 （留空=跟随窗口源）"
                style="flex:1">
              <button class="btn btn-sm" type="button" id="cfg-pick-window">枚举窗口</button>
            </div>
            <div class="hint">${ws.source.kind === 'window'
              ? '当前源是窗口：留空即跟随源。识别走摄像头/RTSP 时，这里仍要填浏览器窗口。'
              : '当前源不是窗口：滑杆点击必须落到屏幕上的游戏浏览器窗口，请在这里指定。'}</div>
          </div>
          <div class="row">
            <div class="field"><label>标定期望尺寸 w（逻辑点）</label>
              <input class="input" name="expected_w" placeholder="例如 736"
                value="${esc((ws.window.expected_size || [])[0] || '')}"></div>
            <div class="field"><label>h</label>
              <input class="input" name="expected_h" placeholder="例如 838"
                value="${esc((ws.window.expected_size || [])[1] || '')}"></div>
          </div>
          <div class="hint">Retina 2x 下窗口逻辑点 ≠ profile reference_size（像素）。
            拿不准就留空，启动后在概览点「固定窗口」记录。</div>
          <div class="inline" style="margin-top:8px">
            <button class="btn btn-sm" type="button" id="cfg-apply-window">一键配置窗口分辨率</button>
            <span class="hint">把游戏窗口调整为上面的期望尺寸；为空时按 profile 比例和当前窗口宽度推算。</span>
          </div>
        </div></fieldset>

        <fieldset><legend>点击</legend><div class="form">
          <label class="checkbox"><input type="checkbox" name="click_enabled"
            ${ws.click.enabled ? 'checked' : ''}>启用自动点击</label>
          <label class="checkbox"><input type="checkbox" name="click_dry_run"
            ${ws.click.dry_run ? 'checked' : ''}>dry-run（只记录，不动鼠标）</label>
          <label class="checkbox"><input type="checkbox" name="click_confirm"
            ${ws.click.confirm ? 'checked' : ''}>执行前在终端确认 y/n（工作台不弹确认框）</label>
          <label class="checkbox"><input type="checkbox" name="click_slider"
            ${ws.click.bet_slider ? 'checked' : ''}>下注/加注走长按滑杆</label>
          <label class="checkbox"><input type="checkbox" name="click_slider_only"
            ${ws.click.bet_slider_only ? 'checked' : ''}>只用滑杆（跳过快捷档位）</label>
          <div class="field"><label>点击模式</label>
            <select name="click_mode">
              <option value="profile_slots" ${ws.click.mode === 'profile_slots' ? 'selected' : ''}>profile_slots（推荐）</option>
              <option value="legacy_clicker" ${ws.click.mode === 'legacy_clicker' ? 'selected' : ''}>legacy_clicker（只读兼容）</option>
            </select></div>
          <div class="field"><label>滑杆配置</label>
            <input class="input" name="bet_slider_config"
              value="${esc(ws.bet_slider_config || 'config/bet_slider_mac.json')}"></div>
        </div></fieldset>

        <fieldset><legend>运行</legend><div class="form">
          <div class="row-3">
            <div class="field"><label>初始盲注</label>
              <input class="input" name="initial_blinds" value="${esc(ws.runtime.initial_blinds || '')}"
                placeholder="1/2"></div>
            <div class="field"><label>every（秒）</label>
              <input class="input" type="number" step="0.05" min="0" name="every"
                value="${g(ws.runtime, 'every', 0.5)}"></div>
            <div class="field"><label>settle（秒）</label>
              <input class="input" type="number" step="0.05" min="0" name="settle"
                value="${g(ws.runtime, 'settle', 0.25)}"></div>
          </div>
          <label class="checkbox"><input type="checkbox" name="ai_advice"
            ${ws.runtime.ai_advice ? 'checked' : ''}>AI 建议</label>
          <label class="checkbox"><input type="checkbox" name="test_mode"
            ${ws.runtime.test ? 'checked' : ''}>测试模式（独立日志、手牌不入库）</label>
        </div></fieldset>

        <fieldset><legend>调试</legend><div class="form">
          <div class="field"><label>预览帧率</label>
            <input class="input" type="number" min="1" max="10" name="preview_fps"
              value="${g(ws.debug, 'preview_fps', 3)}"></div>
          <label class="checkbox"><input type="checkbox" name="screen_overlay"
            ${ws.debug.screen_overlay ? 'checked' : ''}>屏幕浮层</label>
          <div class="field"><label>浮层分组</label>
            <div class="inline">${groupChecks}</div></div>
        </div></fieldset>
      </div>

      <div class="sep"></div>
      <div class="field"><label>高级参数（JSON；白名单，见 runtime.advanced）</label>
        <textarea name="advanced">${esc(JSON.stringify(ws.runtime.advanced || {}, null, 2))}</textarea>
        <div class="hint">可写 stable_frames / roi_threads / rotate / crop / max_frame_age 等；
          不认识的键会被拒绝。</div>
      </div>
      <div class="form-actions">
        <button class="btn btn-primary" type="submit">保存</button>
        <button class="btn" type="button" id="save-restart">保存并重启</button>
        <span class="hint" id="cfg-hint"></span>
      </div>
      <div id="cfg-issues"></div>
    </form>`;

  const form = document.getElementById('cfg-form');
  const collect = () => {
    const fd = new FormData(form);
    const num = (name, fallback) => {
      const raw = fd.get(name);
      if (raw === null || String(raw).trim() === '') return fallback;
      return Number(raw);
    };
    let advanced = {};
    try { advanced = JSON.parse(fd.get('advanced') || '{}'); }
    catch (err) { throw new Error('高级参数不是合法 JSON：' + err.message); }
    const groups = GROUPS.map(([key]) => key)
      .filter((key) => form.querySelector(`[data-og="${key}"]`).checked);
    const expectedW = num('expected_w', null), expectedH = num('expected_h', null);
    return {
      name: String(fd.get('name') || '').trim(),
      profile: fd.get('profile'),
      source: { kind: fd.get('source_kind'), spec: String(fd.get('source_spec') || '').trim() },
      recognizer: fd.get('recognizer'),
      full_config: String(fd.get('full_config') || '').trim() || null,
      bet_slider_config: String(fd.get('bet_slider_config') || '').trim() || null,
      window: {
        spec: String(fd.get('window_spec') || '').trim() || null,
        expected_size: (expectedW && expectedH) ? [expectedW, expectedH] : null,
        pin: 'manual',
      },
      click: {
        enabled: form.querySelector('[name=click_enabled]').checked,
        dry_run: form.querySelector('[name=click_dry_run]').checked,
        confirm: form.querySelector('[name=click_confirm]').checked,
        bet_slider: form.querySelector('[name=click_slider]').checked,
        bet_slider_only: form.querySelector('[name=click_slider_only]').checked,
        mode: fd.get('click_mode'),
      },
      runtime: {
        initial_blinds: String(fd.get('initial_blinds') || '').trim() || null,
        every: num('every', 0.5), settle: num('settle', 0.25),
        ai_advice: form.querySelector('[name=ai_advice]').checked,
        test: form.querySelector('[name=test_mode]').checked,
        advanced,
      },
      debug: {
        preview_fps: num('preview_fps', 3),
        screen_overlay: form.querySelector('[name=screen_overlay]').checked,
        overlay_groups: groups,
      },
    };
  };

  const save = async (restartAfter) => {
    const issues = document.getElementById('cfg-issues');
    issues.innerHTML = '';
    let payload;
    try { payload = collect(); }
    catch (err) { toast(err.message, 'err'); return; }
    // 从窗口源切到摄像头/RTSP/文件源时，把原来的窗口记下来作为游戏窗口：
    // 识别源可以换，点击/浮层仍然要落到那个浏览器窗口上。
    const oldKind = ws.source && ws.source.kind;
    const oldInner = String((ws.source && ws.source.spec) || '')
      .replace(/^(window|screen):/, '');
    if (oldKind === 'window' && payload.source.kind !== 'window'
        && !payload.window.spec && oldInner) {
      payload.window.spec = oldInner;
    }
    try {
      const updated = await api(`/api/workspaces/${ws.id}`, { method: 'PUT', body: payload });
      toast('已保存', 'ok');
      if (restartAfter) {
        await api(`/api/workspaces/${ws.id}/restart`, { method: 'POST' });
        toast('已用新配置重启', 'ok');
      }
      location.hash = `#/w/${updated.id}/overview`;
      route();
    } catch (err) {
      const problems = (err.data && err.data.problems) || [];
      issues.innerHTML = `<div class="error-box">${esc(err.message)}
        ${problems.length ? '\n' + problems.map(esc).join('\n') : ''}</div>`;
      toast(err.message, 'err');
    }
  };
  document.getElementById('cfg-pick-window').addEventListener('click', () => {
    pickWindow((spec) => {
      form.querySelector('[name=window_spec]').value = spec;
    });
  });
  document.getElementById('cfg-apply-window').addEventListener('click', async () => {
    const w = Number(form.querySelector('[name=expected_w]').value);
    const h = Number(form.querySelector('[name=expected_h]').value);
    const target = (w > 0 && h > 0) ? [w, h] : null;
    const label = target ? `${target[0]}×${target[1]}` : '按 profile 比例推算';
    const ok = await confirmDialog('一键配置窗口分辨率',
      `<p>把游戏窗口调整为 <b>${label}</b> 逻辑点？</p>
       <p class="hint" style="margin-top:8px">会移动/缩放游戏浏览器窗口（保持左上角），
       需要辅助功能权限。窗口标题/位置会重新读取；若系统限制尺寸，会记录实际值。</p>`,
      '调整');
    if (!ok) return;
    try {
      const res = await api(`/api/workspaces/${ws.id}/configure-window`, {
        method: 'POST',
        body: {
          target,
          spec: form.querySelector('[name=window_spec]').value.trim() || null,
        },
      });
      const actual = res.after
        ? `${Math.round(res.after.w)}×${Math.round(res.after.h)}`
        : `${res.actual[0]}×${res.actual[1]}`;
      form.querySelector('[name=expected_w]').value = res.requested[0];
      form.querySelector('[name=expected_h]').value = res.requested[1];
      const hint = document.getElementById('cfg-hint');
      if (hint) hint.textContent = res.note || '';
      toast(res.matched
        ? `窗口已调整为 ${actual}（${res.basis}）`
        : `窗口实际为 ${actual}，未完全匹配目标（${res.basis}）`,
        res.matched ? 'ok' : 'err');
    } catch (err) {
      if (err.status === 404) {
        toast('后端没有这个接口：请重启工作台进程（Ctrl-C 后重新运行）再试', 'err');
        return;
      }
      toast([err.message, err.data && err.data.fix].filter(Boolean).join('\n'), 'err');
    }
  });
  document.getElementById('cfg-probe').addEventListener('click', async () => {
    const out = document.getElementById('cfg-probe-result');
    const spec = form.querySelector('[name=source_spec]').value.trim();
    out.textContent = '探测中（可能打开一次源）…';
    try {
      const res = await api('/api/sources/probe', { method: 'POST', body: { spec } });
      out.textContent = res.ok
        ? `出帧 ${res.size ? res.size.join('×') : '?'} · ${res.kind} · ${res.elapsed_ms}ms`
        : `探测失败：${res.error}`;
      out.className = res.ok ? 'hint green' : 'hint red';
    } catch (err) {
      out.textContent = `探测失败：${err.message}`;
      out.className = 'hint red';
    }
  });
  form.addEventListener('submit', (ev) => { ev.preventDefault(); save(false); });
  document.getElementById('save-restart').addEventListener('click', () => save(true));
}

async function renderPreflightTab(body, ws) {
  body.innerHTML = `
    <div class="inline" style="margin-bottom:12px">
      <button class="btn btn-primary" id="run-pre">重新体检</button>
      <label class="checkbox"><input type="checkbox" id="pre-probe" checked>探测视频源（会开源一次）</label>
      <span class="hint">fail 阻断启动；warn 需要确认；skip 表示当前配置/平台不适用。</span>
    </div>
    <div id="pre-result"><div class="empty"><span class="spinner"></span> 体检中…</div></div>
    <div class="inline" id="pre-start" style="margin-top:12px; display:none">
      <label class="checkbox"><input type="checkbox" id="pre-force">仍然启动（接受警告；失败永远阻断）</label>
      <button class="btn btn-primary" id="pre-start-btn">启动 live</button>
    </div>`;
  const run = async () => {
    const probe = document.getElementById('pre-probe').checked;
    const box = document.getElementById('pre-result');
    box.innerHTML = '<div class="empty"><span class="spinner"></span> 体检中…</div>';
    try {
      const res = await api(`/api/workspaces/${ws.id}/preflight?probe=${probe}`);
      renderPreflightResult(box, res);
    } catch (err) {
      box.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
    }
  };
  document.getElementById('run-pre').addEventListener('click', run);
  document.getElementById('pre-start-btn').addEventListener('click', async () => {
    const force = document.getElementById('pre-force').checked;
    try {
      await api(`/api/workspaces/${ws.id}/start`, { method: 'POST', body: { force, probe: false } });
      toast('已启动', 'ok');
      location.hash = `#/w/${ws.id}/run`;
    } catch (err) {
      if (err.data && err.data.needs_confirm) {
        toast('有警告：勾选「仍然启动」再试', 'err');
        document.getElementById('pre-force').checked = true;
      } else if (err.data && err.data.preflight) {
        renderPreflightResult(document.getElementById('pre-result'), {
          checks: err.data.preflight.checks, summary: err.data.preflight.summary,
        });
        toast(err.message, 'err');
      } else toast(err.message, 'err');
    }
  });
  await run();
}

function renderPreflightResult(box, res) {
  const summary = res.summary || {};
  const counts = summary.counts || {};
  const statusLabel = { pass: '通过', warn: '警告', fail: '失败', skip: '跳过' };
  box.innerHTML = `
    <div class="summary">
      <span class="badge badge-ok">通过 ${counts.pass || 0}</span>
      <span class="badge badge-warn">警告 ${counts.warn || 0}</span>
      <span class="badge badge-fail">失败 ${counts.fail || 0}</span>
      <span class="badge">跳过 ${counts.skip || 0}</span>
      <span class="badge ${summary.can_start ? 'badge-ok' : 'badge-fail'}">
        ${summary.can_start ? (summary.needs_confirm ? '可启动（需确认）' : '可启动') : '不可启动'}</span>
    </div>
    <div class="panel"><div class="panel-body">
      ${(res.checks || []).map((c) => `
        <div class="check">
          <div class="check-status st-${esc(c.status)}">${esc(statusLabel[c.status] || c.status)}</div>
          <div>
            <div class="check-title">${esc(c.title)}</div>
            ${c.detail ? `<div class="check-detail">${esc(c.detail)}</div>` : ''}
            ${c.fix ? `<div class="check-fix">修复：${esc(c.fix)}</div>` : ''}
          </div>
        </div>`).join('')}
    </div></div>`;
  const startBox = document.getElementById('pre-start');
  if (startBox) {
    startBox.style.display = summary.can_start ? '' : 'none';
    startBox.querySelector('#pre-force').checked = !!summary.needs_confirm;
  }
}

async function renderHistoryTab(body, ws) {
  const { runs } = await api(`/api/workspaces/${ws.id}/runs?limit=100`);
  body.innerHTML = `<div class="panel"><div class="panel-body tight">
    ${renderRunsTable(runs)}</div></div>`;
  bindManifestButtons(body);
}

/* ─────────────────────────── 启动辅助 ─────────────────────────── */

async function startWorkspace(id) {
  try {
    await api(`/api/workspaces/${id}/start`, { method: 'POST', body: {} });
    toast('已启动', 'ok');
    location.hash = `#/w/${id}/run`;
  } catch (err) {
    if (err.status === 409 && err.data && err.data.preflight) {
      if (err.data.needs_confirm) {
        const ok = await confirmDialog('体检有警告',
          renderPreflightSummaryHtml(err.data.preflight) +
          '<p class="hint" style="margin-top:10px">警告可以确认后继续；fail 永远阻断。</p>',
          '仍然启动');
        if (ok) {
          try {
            await api(`/api/workspaces/${id}/start`,
              { method: 'POST', body: { force: true, probe: false } });
            location.hash = `#/w/${id}/run`;
            return;
          } catch (e) { toast(e.message, 'err'); return; }
        }
      } else {
        await confirmDialog('体检未通过',
          renderPreflightSummaryHtml(err.data.preflight), '知道了');
      }
    }
    toast(err.message, 'err');
  }
}

function renderPreflightSummaryHtml(pre) {
  const summary = (pre && pre.summary) || {};
  const counts = summary.counts || {};
  const rows = ((pre && pre.checks) || []).filter((c) => c.status !== 'pass')
    .map((c) => `<div class="check"><div class="check-status st-${esc(c.status)}">
      ${c.status === 'fail' ? '失败' : c.status === 'warn' ? '警告' : '跳过'}</div>
      <div><div class="check-title">${esc(c.title)}</div>
      <div class="check-detail">${esc(c.detail || '')}</div></div></div>`).join('');
  return `<div class="summary">
      <span class="badge badge-warn">警告 ${counts.warn || 0}</span>
      <span class="badge badge-fail">失败 ${counts.fail || 0}</span></div>
    ${rows || '<div class="empty">没有需要处理的问题</div>'}`;
}

/* ─────────────────────────── 运行页 ─────────────────────────── */

const runState = {
  id: null, status: null, layout: null, groups: {}, selected: null,
  imageTimer: null, errors: {}, lastSeq: null,
};

async function pageRun(id) {
  runState.id = id;
  runState.groups = Object.fromEntries(GROUPS.map(([k]) => [k, true]));
  runState.selected = null;
  const [status, layout] = await Promise.all([
    api(`/api/workspaces/${id}/status`),
    api(`/api/workspaces/${id}/slots`),
  ]);
  runState.status = status;
  runState.layout = layout;
  const ws = await api(`/api/workspaces/${id}`);
  runState.name = ws.name;
  setTopStatus(STATUS_TEXT[status.state] || status.state, status.state);
  rootEl.innerHTML = `
    <div class="crumb"><a href="#/">工作区</a> / <a href="#/w/${esc(id)}">${esc(ws.name)}</a> / 运行</div>
    <div class="run-head">
      <h1 id="run-title">${esc(STATUS_TEXT[status.state] || '运行')} · ${esc(ws.name)}</h1>
      <span id="run-pill">${statusPill(status.state)}</span>
      <div class="right inline">
        <button class="btn btn-sm btn-primary" id="btn-start"
          ${['running', 'paused_click', 'starting'].includes(status.state) ? 'style="display:none"' : ''}>启动</button>
        <button class="btn btn-sm" id="btn-pause"></button>
        <a class="btn btn-sm" href="#/w/${esc(id)}/calibrate">标定</a>
        <button class="btn btn-sm" id="btn-restart">重启</button>
        <button class="btn btn-sm btn-danger" id="btn-stop">停止</button>
        <a class="btn btn-sm" id="btn-dashboard" target="_blank" rel="noreferrer">打开完整看板</a>
      </div>
    </div>
    <div class="run-metrics" id="run-metrics"></div>
    <div class="run-layout">
      <section class="panel">
        <div class="panel-head">
          <h2>实时预览</h2>
          <div class="groups" id="run-groups"></div>
        </div>
        <div class="panel-body tight">
          <div class="canvas-wrap" id="run-wrap">
            <img id="run-img" alt="live 预览" style="display:none">
            <canvas id="run-canvas"></canvas>
            <div class="empty" id="run-placeholder" style="position:absolute">等待首帧…</div>
          </div>
          <div class="hint" id="frame-meta" style="margin-top:8px"></div>
        </div>
        <div class="panel-head" style="border-top:1px solid var(--line)">
          <h2>选中槽位</h2><span class="faint" id="sel-hint">点预览里的框选中</span>
        </div>
        <div class="panel-body inspector" id="run-inspector">
          <div class="empty">还没有选中槽位</div>
        </div>
      </section>
      <div>
        <section class="panel"><div class="panel-head"><h2>识别健康</h2></div>
          <div class="panel-body" id="run-health"><div class="empty">等待状态…</div></div></section>
        <section class="panel" style="margin-top:12px">
          <div class="panel-head"><h2>最近事件</h2></div>
          <div class="panel-body tight"><ul class="events" id="run-events"></ul></div></section>
      </div>
    </div>
    <section class="panel" style="margin-top:14px">
      <div class="panel-head"><h2>日志 / 识别帧流</h2></div>
      <div class="panel-body tight"><div class="log" id="run-log"></div></div>
    </section>`;

  renderRunGroups();
  updateRunPage(status);
  bindRunControls(ws);

  timer(pollRunStatus, 1000);
  const fps = Math.max(1, Math.min(10, Number(ws.debug.preview_fps) || 3));
  runState.imageTimer = timer(refreshRunImage, Math.round(1000 / fps));
  timer(refreshRunImage, 0, false);
}

function renderRunGroups() {
  const box = document.getElementById('run-groups');
  if (!box) return;
  box.innerHTML = GROUPS.map(([key, label]) => `
    <label class="${runState.groups[key] ? 'on' : ''}" data-group="${key}">
      <input type="checkbox" ${runState.groups[key] ? 'checked' : ''}>${label}</label>`).join('');
  box.querySelectorAll('[data-group]').forEach((label) => {
    label.querySelector('input').addEventListener('change', (ev) => {
      runState.groups[label.dataset.group] = ev.target.checked;
      label.classList.toggle('on', ev.target.checked);
      drawRunCanvas();
    });
  });
}

function bindRunControls(ws) {
  document.getElementById('btn-start').addEventListener('click', async () => {
    try {
      await api(`/api/workspaces/${ws.id}/start`, {
        method: 'POST', body: { force: false, probe: true },
      });
      toast('正在启动…', 'ok');
      await pollRunStatus();
    } catch (err) {
      if (err.data && err.data.needs_confirm) {
        const ok = await confirmDialog('体检有警告', renderPreflightSummaryHtml(
          err.data.preflight || {}), '仍然启动');
        if (ok) {
          try {
            await api(`/api/workspaces/${ws.id}/start`, {
              method: 'POST', body: { force: true, probe: false },
            });
            await pollRunStatus();
          } catch (e2) { toast(e2.message, 'err'); }
        }
      } else if (err.data && err.data.preflight) {
        await confirmDialog('体检未通过',
          renderPreflightSummaryHtml(err.data.preflight), '知道了');
      } else toast(err.message, 'err');
    }
  });
  document.getElementById('btn-stop').addEventListener('click', async () => {
    try {
      await api(`/api/workspaces/${ws.id}/stop`, { method: 'POST' });
      toast('已停止', 'ok');
      route();
    } catch (err) { toast(err.message, 'err'); }
  });
  document.getElementById('btn-restart').addEventListener('click', async () => {
    try {
      await api(`/api/workspaces/${ws.id}/restart`, { method: 'POST' });
      toast('已重启', 'ok');
      route();
    } catch (err) { toast(err.message, 'err'); }
  });
  const dash = document.getElementById('btn-dashboard');
  dash.addEventListener('click', (ev) => {
    if (!dash.href || dash.href.endsWith('#')) { ev.preventDefault(); toast('live 还没起来', 'err'); }
  });
  document.getElementById('btn-pause').addEventListener('click', async () => {
    const paused = runState.status && runState.status.click && runState.status.click.paused;
    try {
      await api(`/api/workspaces/${ws.id}/click`, {
        method: 'POST', body: { enabled: !!paused },
      });
      toast(paused ? '点击已恢复（只对新决策点生效）' : '点击已暂停', 'ok');
      await pollRunStatus();
    } catch (err) { toast(err.message, 'err'); }
  });
  const canvas = document.getElementById('run-canvas');
  canvas.addEventListener('pointerdown', onRunCanvasClick);
  const img = document.getElementById('run-img');
  img.addEventListener('load', () => {
    img.style.display = '';
    const ph = document.getElementById('run-placeholder');
    if (ph) ph.style.display = 'none';
    drawRunCanvas();
  });
  if (img.complete && img.naturalWidth) drawRunCanvas();
}

async function pollRunStatus() {
  try {
    const status = await api(`/api/workspaces/${runState.id}/status`);
    runState.status = status;
    setTopStatus(STATUS_TEXT[status.state] || status.state, status.state);
    updateRunPage(status);
  } catch (err) {
    console.warn(err);
  }
}

function updateRunPage(status) {
  const pill = document.getElementById('run-pill');
  if (pill) pill.innerHTML = statusPill(status.state);
  const title = document.getElementById('run-title');
  if (title) {
    title.textContent = `${STATUS_TEXT[status.state] || '运行'} · ${runState.name || ''}`;
  }
  const active = ['running', 'paused_click', 'starting'].includes(status.state);
  const startBtn = document.getElementById('btn-start');
  const stopBtn = document.getElementById('btn-stop');
  if (startBtn) startBtn.style.display = active ? 'none' : '';
  if (stopBtn) stopBtn.disabled = !active;
  const stats = status.stats || {};
  const frame = status.frame || {};
  const click = status.click || {};
  const meta = status.meta || {};
  const metrics = document.getElementById('run-metrics');
  if (metrics) {
    const hand = (status.snapshot && status.snapshot.table && status.snapshot.table.state) || {};
    const elapsed = status.run ? (Date.now() / 1000) - new Date(status.run.started_at).getTime() / 1000 : null;
    metrics.innerHTML = [
      ['手牌', hand.hand_id ? esc(String(hand.hand_id).slice(-6)) : '-'],
      ['帧', frame.seq ?? stats.processed ?? '-'],
      ['接受率', pct(rate(stats))],
      ['耗时', `${stats.avg_proc_ms ?? '-'}ms`],
      ['队列', `${frame.queue_ms ?? '-'}ms`],
      ['运行', status.run ? fmtDuration(elapsed) : '-'],
    ].map(([k, v]) => `<div class="metric"><b>${v}</b><span>${k}</span></div>`).join('');
  }
  const pauseBtn = document.getElementById('btn-pause');
  if (pauseBtn) {
    if (!click.available) {
      pauseBtn.textContent = '未启用点击';
      pauseBtn.disabled = true;
    } else {
      pauseBtn.textContent = click.paused ? '恢复点击' : '暂停点击';
      pauseBtn.disabled = false;
      pauseBtn.classList.toggle('active', !!click.paused);
    }
  }
  const dash = document.getElementById('btn-dashboard');
  if (dash) {
    if (status.live_url) dash.href = status.live_url;
    else dash.removeAttribute('href');
  }
  const metaEl = document.getElementById('frame-meta');
  if (metaEl) {
    const src = meta.source || {};
    metaEl.textContent = [
      frame.seq ? `帧 #${frame.seq}` : '等待帧',
      frame.width ? `${frame.width}×${frame.height}` : '',
      src.kind ? `源 ${src.kind}` : '',
      (src.window ? `窗口 ${Math.round(src.window.w)}×${Math.round(src.window.h)}` : ''),
      status.live_url ? `看板 ${status.live_url}` : '',
    ].filter(Boolean).join(' · ');
  }
  renderHealth(status);
  renderEvents(status);
  const log = document.getElementById('run-log');
  if (log) log.textContent = (status.log_tail || []).join('\n') || '（暂无日志）';
  if (status.state === 'idle' || status.state === 'failed') {
    const ph = document.getElementById('run-placeholder');
    if (ph) {
      ph.style.display = '';
      ph.textContent = status.state === 'failed'
        ? `启动失败：${(status.run && status.run.error) || '看日志'}`
        : 'live 已停止';
    }
  }
}

function renderHealth(status) {
  const box = document.getElementById('run-health');
  const snap = status.snapshot;
  const frame = (snap && snap.frame) || {};
  const state = (snap && snap.table && snap.table.state) || {};
  const cards = (snap && snap.cards) || {};
  const errors = frame.errors || [];
  if (box) {
    const kinds = { record: '有效记录', discard: '丢弃', idle: '间隙帧', no_change: '无变化', stale: '陈旧帧' };
    const hasSnap = !!snap;
    box.innerHTML = `
      <dl class="kv">
        <dt>本帧</dt><dd>${esc(kinds[frame.kind] || frame.kind || '-')}</dd>
        <dt>错误</dt><dd>${!hasSnap ? '-' : errors.length
          ? `<span class="red">${esc(errors.join(' · '))}</span>` : '<span class="green">无</span>'}</dd>
        <dt>牌面</dt><dd>${!hasSnap ? '-' : cards.reliable ? '<span class="green">可靠</span>'
          : `<span class="yellow">${esc((cards.warnings || ['不可靠']).join(' / '))}</span>`}</dd>
        <dt>底池</dt><dd>${esc(state.pot ?? '-')}</dd>
        <dt>轮到我</dt><dd>${state.hero_turn == null ? '-' : (state.hero_turn ? '是' : '否')}</dd>
        <dt>建议</dt><dd>${snap && snap.advice
          ? esc((snap.advice.option && snap.advice.option.label) || '-')
            + ` <span class="faint">${esc(snap.advice.source || '')}</span>` : '-'}</dd>
      </dl>
      ${status.run && status.run.error
        ? `<div class="error-box" style="margin-top:10px">${esc(status.run.error)}</div>` : ''}`;
  }
}

function renderEvents(status) {
  const box = document.getElementById('run-events');
  if (!box) return;
  const events = (status.snapshot && status.snapshot.events) || [];
  box.innerHTML = events.slice(-24).reverse().map((ev) => {
    const t = new Date(ev.ts);
    const hh = `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}:${String(t.getSeconds()).padStart(2, '0')}`;
    const s = ev.summary || {};
    let text = ev.type;
    if (ev.type === 'frame') text = `帧 ${s.seq} · ${s.kind}${(s.errors || []).length ? ' · ' + s.errors.join(',') : ''}`;
    else if (ev.type === 'advice') text = `建议 ${s.action || ''} · ${s.duration_ms || '?'}ms`;
    else if (ev.type === 'hand') text = `手牌 ${s.hand_id || ''} · ${s.status || ''}`;
    else if (ev.type === 'click') text = `点击 ${JSON.stringify(s.click || {}).slice(0, 60)}`;
    else if (ev.type === 'control') text = `控制：点击${s.paused ? '暂停' : '恢复'}`;
    return `<li><span class="ts">${hh}</span><span>${esc(text)}</span></li>`;
  }).join('') || '<li class="faint">还没有事件</li>';
}

function refreshRunImage() {
  const img = document.getElementById('run-img');
  if (!img) return;
  const state = runState.status && runState.status.state;
  if (state === 'idle' || state === 'failed') return;   // 没在跑就别刷 404
  const next = `/api/workspaces/${runState.id}/preview.jpg?w=960&t=${Date.now()}`;
  img.src = next;
}

function frameGeometry() {
  const layout = runState.layout || {};
  const meta = (runState.status && runState.status.meta) || {};
  const frame = meta.frame || {};
  const fw = Number(frame.width || (layout.frame_size || [])[0]);
  const fh = Number(frame.height || (layout.frame_size || [])[1]);
  const content = (meta.source && meta.source.content_rect)
    || layout.content_rect || [0, 0, fw, fh];
  return { fw, fh, content };
}

function slotPx(slot, geo) {
  const [cx1, cy1, cx2, cy2] = geo.content;
  const cw = cx2 - cx1, ch = cy2 - cy1;
  return [cx1 + slot[0] * cw, cy1 + slot[1] * ch,
    cx1 + slot[2] * cw, cy1 + slot[3] * ch];
}

function errorsForSlot(slot, errors) {
  const out = [];
  for (const err of errors || []) {
    const [prefix, arg] = String(err).split(':');
    if (['pot', 'pbs_gt_pot', 'pbs_inconsistent'].includes(prefix)
        && ['pot', 'pot_before_street'].includes(slot.field)) out.push(err);
    else if (prefix === 'blinds' && slot.field === 'blinds') out.push(err);
    else if (['stack', 'chip'].includes(prefix) && slot.field
      && ['chip', 'chip_alt'].includes(slot.field) && slot.seat_id === arg) out.push(err);
    else if (['bet', 'bet_gt_pot'].includes(prefix) && slot.field === 'bet'
      && slot.seat_id === arg) out.push(err);
    else if (['vanished', 'name', 'action'].includes(prefix)
      && ['name', 'avatar'].includes(slot.field) && slot.seat_id === arg) out.push(err);
  }
  return out;
}

function drawRunCanvas() {
  const img = document.getElementById('run-img');
  const canvas = document.getElementById('run-canvas');
  const layout = runState.layout;
  if (!img || !canvas || !layout || !img.naturalWidth) return;
  const geo = frameGeometry();
  if (!geo.fw || !geo.fh) return;
  const scale = img.naturalWidth / geo.fw;
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const errors = ((runState.status && runState.status.snapshot
    && runState.status.snapshot.frame && runState.status.snapshot.frame.errors) || []);
  for (const slot of layout.slots || []) {
    if (!runState.groups[slot.group]) continue;
    const [x1, y1, x2, y2] = slotPx(slot.slot, geo).map((v) => v * scale);
    const hits = errorsForSlot(slot, errors);
    const selected = slot.key === runState.selected;
    let color = hits.length ? '#f05d5e'
      : ['button', 'slider'].includes(slot.kind) ? '#4c9aff'
        : GROUP_COLORS[slot.group] || '#58d68d';
    ctx.lineWidth = selected ? 3 : 1.6;
    ctx.strokeStyle = selected ? '#ffd60a' : color;
    if (slot.shape === 'disc') {
      ctx.beginPath();
      ctx.ellipse((x1 + x2) / 2, (y1 + y2) / 2,
        Math.abs(x2 - x1) / 2, Math.abs(y2 - y1) / 2, 0, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    }
    if (hits.length) {
      ctx.fillStyle = 'rgba(240,93,94,.18)';
      ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
    }
  }
}

function onRunCanvasClick(ev) {
  const canvas = ev.currentTarget;
  const layout = runState.layout;
  const geo = frameGeometry();
  if (!layout || !geo.fw) return;
  const rect = canvas.getBoundingClientRect();
  const px = (ev.clientX - rect.left) / rect.width * canvas.width;
  const py = (ev.clientY - rect.top) / rect.height * canvas.height;
  const scale = canvas.width / geo.fw;
  const hit = [...(layout.slots || [])].reverse().find((slot) => {
    const [x1, y1, x2, y2] = slotPx(slot.slot, geo).map((v) => v * scale);
    if (slot.shape === 'disc') {
      const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
      return ((px - cx) ** 2) / ((x2 - x1) ** 2 / 4 + 1e-6)
        + ((py - cy) ** 2) / ((y2 - y1) ** 2 / 4 + 1e-6) <= 1;
    }
    return px >= x1 && px <= x2 && py >= y1 && py <= y2;
  });
  runState.selected = hit ? hit.key : null;
  renderRunInspector(hit);
  drawRunCanvas();
}

function renderRunInspector(slot) {
  const box = document.getElementById('run-inspector');
  const hint = document.getElementById('sel-hint');
  if (!box) return;
  if (!slot) {
    box.innerHTML = '<div class="empty">还没有选中槽位</div>';
    if (hint) hint.textContent = '点预览里的框选中';
    return;
  }
  const geo = frameGeometry();
  const px = slotPx(slot.slot, geo).map((v) => Math.round(v));
  const errors = ((runState.status && runState.status.snapshot
    && runState.status.snapshot.frame && runState.status.snapshot.frame.errors) || []);
  const hits = errorsForSlot(slot, errors);
  if (hint) hint.textContent = slot.key;
  box.innerHTML = `
    <dl class="kv">
      <dt>槽位</dt><dd class="mono">${esc(slot.key)}</dd>
      <dt>名称</dt><dd>${esc(slot.label)} <span class="badge">${esc(slot.group)}</span></dd>
      <dt>归一化</dt><dd class="mono">${slot.slot.map((v) => Number(v).toFixed(4)).join(', ')}</dd>
      <dt>像素矩形</dt><dd class="mono">${px.join(', ')}</dd>
      <dt>本帧错误</dt><dd>${hits.length
        ? `<span class="red">${esc(hits.join(' · '))}</span>` : '<span class="green">无</span>'}</dd>
    </dl>
    <div class="hint" style="margin-top:8px">
      坐标口径：归一化 → profile.slot_to_px（与 live 相同）；预览 JPEG 缩放不影响框位置。
    </div>`;
}


/* ─────────────────────────── 标定页 ─────────────────────────── */

const calState = {
  id: null, layout: null, draft: null, selected: null, groups: {},
  zoom: 1, editableOnly: false, overlay: false, trial: null,
  drag: null, drawing: null, imageSize: null, img: null, trialTimer: null,
  search: '', isolate: false, step: 1,
};

async function pageCalibrate(id) {
  const cal = await api(`/api/workspaces/${id}/calibration`);
  const probed = await api(`/api/workspaces/${id}/slots?probe=1`).catch(() => null);
  if (probed && probed.slots) cal.layout = probed;
  calState.id = id;
  calState.layout = cal.layout;
  calState.draft = cal.draft;
  calState.groups = Object.fromEntries(GROUPS.map(([k]) => [k, true]));
  calState.selected = (cal.draft && Object.keys(cal.draft.edits || {})[0]) || null;
  calState.trial = null;
  calState.search = '';
  calState.isolate = false;
  calState.step = 1;
  calState.drawing = null;
  calState.autoFitDone = false;
  const ws = await api(`/api/workspaces/${id}`);
  setTopStatus('标定', 'calibrating');

  rootEl.innerHTML = `
    <div class="crumb"><a href="#/">工作区</a> / <a href="#/w/${esc(id)}">${esc(ws.name)}</a> / 标定</div>
    <div class="page-head">
      <div><h1>标定 · ${esc(ws.name)}</h1>
        <div class="sub">草稿 <b id="cal-count">0</b> 处未应用 ·
          <span class="mono">${esc(cal.profile)}</span></div></div>
      <div class="actions">
        <button class="btn" id="cal-refresh">刷新冻结帧</button>
        <button class="btn" id="cal-back" onclick="location.hash='#/w/${esc(id)}/run'">回运行页</button>
      </div>
    </div>
    <div class="toolbar">
      <button class="btn btn-sm" id="cal-undo">撤销</button>
      <button class="btn btn-sm" id="cal-redo">重做</button>
      <button class="btn btn-sm" id="cal-reset">重置</button>
      <span class="sep" style="width:1px;height:20px;background:var(--line);margin:0 4px"></span>
      <button class="btn btn-sm" data-zoom="fit">适应</button>
      <button class="btn btn-sm" data-zoom="1">1x</button>
      <button class="btn btn-sm" data-zoom="2">2x</button>
      <button class="btn btn-sm" data-zoom="4">4x</button>
      <span class="zoom-label" id="cal-zoom">1x</span>
      <span class="hint" style="margin-left:8px">步长</span>
      <select id="cal-step" class="input" style="width:82px;padding:4px 6px">
        <option value="1">1 px</option>
        <option value="5">5 px</option>
        <option value="10">10 px</option>
      </select>
      <span class="spacer"></span>
      <label class="checkbox"><input type="checkbox" id="cal-isolate">只看选中</label>
      <label class="checkbox"><input type="checkbox" id="cal-overlay">在游戏窗口显示</label>
      <label class="checkbox"><input type="checkbox" id="cal-m1">只看按钮/滑杆</label>
    </div>
    <div class="cal-layout">
      <section class="panel"><div class="panel-head"><h2>图层</h2></div>
        <div class="panel-body tight">
          <input class="input" id="cal-search" placeholder="搜索槽位（名字/seat/字段）"
            style="margin-bottom:8px">
          <div class="groups" id="cal-groups" style="margin-bottom:8px"></div>
          <div class="layers" id="cal-layers"></div></div></section>
      <section class="panel">
        <div class="panel-head"><h2>冻结帧 · 拖框编辑</h2>
          <span class="hint" id="cal-geo"></span></div>
        <div class="panel-body tight">
          <div class="canvas-scroll" id="cal-scroll"><div class="stage" id="cal-stage">
            <img id="cal-img" alt="冻结帧"><canvas id="cal-canvas"></canvas>
          </div></div>
          <div class="hint" style="margin-top:8px">
            <b>标定前让游戏停在牌桌画面</b>（大厅/设置/结算页的框对不上是正常的，切回去点「刷新冻结帧」）。
            <b>在空白处拖出新框 = 重画选中槽位</b>（框很大时按 <b>Alt+拖</b> 也能重画，Esc 取消）；拖框体移动、
            拖角改大小；方向键按「步长」微调（Shift ×10）。虚线=草稿，实线=已应用。
          </div>
          <details class="section" id="cal-diag">
            <summary class="hint">几何诊断（三套网格）</summary>
            <div id="cal-diag-body" style="margin-top:8px"></div>
          </details>
        </div>
      </section>
      <section class="panel inspector-panel"><div class="panel-head"><h2>检查器</h2>
        <button class="btn btn-sm" id="cal-trial">重新试读</button></div>
        <div class="panel-body inspector" id="cal-inspector">
          <div class="empty">选一个框</div></div></section>
    </div>
    <div class="panel" style="margin-top:12px"><div class="panel-body">
      <div class="inline">
        <button class="btn btn-primary" id="cal-save">保存并应用（自动重启）</button>
        <button class="btn btn-danger" id="cal-discard">放弃草稿</button>
        <span class="hint" id="cal-backups">${(cal.backups || []).length
          ? `备份 ${cal.backups.length} 份` : '还没有备份'}</span>
        ${(cal.backups || []).length
          ? `<details class="backups"><summary class="hint">查看 / 恢复备份</summary>
              <div id="cal-backup-list" style="margin-top:6px">
                ${cal.backups.map((name) => `<div class="layer-item">
                  <span class="mono">${esc(name)}</span>
                  <button class="btn btn-sm" data-backup="${esc(name)}">恢复</button>
                </div>`).join('')}
              </div></details>` : ''}
      </div>
      <div class="diff" id="cal-diff" style="margin-top:10px"></div>
    </div></div>`;

  renderCalGroups();
  renderCalLayers();
  renderCalDiagnostics();
  bindCalControls(ws);
  updateCalChrome();
  loadCalImage();
}

function calDrafts() {
  return (calState.draft && calState.draft.edits) || {};
}

function calSlot(key) {
  const drafts = calDrafts();
  if (drafts[key]) return drafts[key];
  const item = (calState.layout.slots || []).find((s) => s.key === key);
  return item ? item.slot : null;
}

function renderCalGroups() {
  const box = document.getElementById('cal-groups');
  if (!box) return;
  box.innerHTML = GROUPS.map(([key, label]) => `
    <label class="${calState.groups[key] ? 'on' : ''}">
      <input type="checkbox" data-cg="${key}" ${calState.groups[key] ? 'checked' : ''}>${label}</label>`).join('');
  box.querySelectorAll('[data-cg]').forEach((input) => {
    input.addEventListener('change', (ev) => {
      const key = ev.target.dataset.cg;
      calState.groups[key] = ev.target.checked;
      ev.target.closest('label').classList.toggle('on', ev.target.checked);
      drawCalCanvas();
      renderCalLayers();
      syncOverlaySelection();
    });
  });
}

function renderCalLayers() {
  const box = document.getElementById('cal-layers');
  if (!box) return;
  const drafts = calDrafts();
  const query = calState.search.trim().toLowerCase();
  let shown = 0;
  box.innerHTML = GROUPS.map(([group, label]) => {
    const slots = (calState.layout.slots || []).filter((s) =>
      s.group === group && calState.groups[group]
      && (!calState.editableOnly || s.stage === 'M1')
      && (!query || s.key.toLowerCase().includes(query)
          || String(s.label || '').toLowerCase().includes(query)));
    shown += slots.length;
    if (!slots.length) return '';
    return `<div class="layer-group"><button data-toggle-group="${group}">
        <span>${label}</span><span class="faint">${slots.length}</span></button>
      ${slots.map((s) => `
        <div class="layer-item ${s.key === calState.selected ? 'sel' : ''}
             ${drafts[s.key] ? 'edited' : ''}" data-key="${esc(s.key)}">
          <span class="break">${esc(s.label)}</span>
          <span class="stage">${esc(s.stage || '')}</span>
        </div>`).join('')}</div>`;
  }).join('') || (query ? '<div class="empty">没有匹配的槽位</div>' : '');
  if (query && shown === 0) {
    box.innerHTML = '<div class="empty">没有匹配的槽位</div>';
  }
  box.querySelectorAll('[data-key]').forEach((el) =>
    el.addEventListener('click', () => selectCalSlot(el.dataset.key)));
}

function pushCalDraft(key, slot) {
  calState.draft = calState.draft || { edits: {}, undo: [], redo: [], diff: [] };
  calState.draft.edits = { ...(calState.draft.edits || {}), [key]: slot };
}

function selectCalSlot(key) {
  calState.selected = key;
  renderCalLayers();
  drawCalCanvas();
  syncOverlaySelection();
  scheduleTrial(key, 0);
}

function loadCalImage() {
  const img = document.getElementById('cal-img');
  const next = `/api/workspaces/${calState.id}/frame.jpg?t=${Date.now()}`;
  img.onload = () => {
    calState.img = img;
    calState.imageSize = [img.naturalWidth, img.naturalHeight];
    if (!calState.autoFitDone) {
      calState.autoFitDone = true;
      fitCalZoom();
    } else {
      layoutCalStage();
      drawCalCanvas();
    }
  };
  img.onerror = () => toast('抓不到冻结帧：检查视频源/权限', 'err');
  img.src = next;
}

function fitCalZoom() {
  const img = document.getElementById('cal-img');
  const scroll = document.getElementById('cal-scroll');
  if (!img || !img.naturalWidth) return;
  const avail = Math.max(320, (scroll ? scroll.clientWidth : 900) - 24);
  calState.zoom = Math.max(0.1, Math.min(1, avail / img.naturalWidth));
  const label = document.getElementById('cal-zoom');
  if (label) label.textContent = `${calState.zoom.toFixed(2)}x`;
  layoutCalStage();
  drawCalCanvas();
}

function layoutCalStage() {
  const img = document.getElementById('cal-img');
  const stage = document.getElementById('cal-stage');
  if (!img || !img.naturalWidth) return;
  const w = img.naturalWidth * calState.zoom;
  img.style.width = `${w}px`;
  img.style.height = 'auto';
  stage.style.width = `${w}px`;
}

function calGeo() {
  // 冻结帧可能就是源原图（不是 profile reference 尺寸）：一律按图片自身
  // 像素算内容区，再乘 profile.canvas；这样框和裁剪用同一套坐标。
  const img = document.getElementById('cal-img');
  const layout = calState.layout || {};
  const fw = (img && img.naturalWidth) || Number((layout.frame_size || [])[0]);
  const fh = (img && img.naturalHeight) || Number((layout.frame_size || [])[1]);
  const canvas = layout.canvas;
  const content = canvas
    ? [canvas[0] * fw, canvas[1] * fh, canvas[2] * fw, canvas[3] * fh]
    : [0, 0, fw, fh];
  return { fw, fh, content };
}

function drawCalCanvas() {
  const canvas = document.getElementById('cal-canvas');
  const img = document.getElementById('cal-img');
  if (!canvas || !img || !img.naturalWidth) return;
  const geo = calGeo();
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  canvas.style.width = `${img.naturalWidth * calState.zoom}px`;
  canvas.style.height = `${img.naturalHeight * calState.zoom}px`;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const drafts = calDrafts();
  // 适应窗口时 1px 画布 ≈ 0.3 屏幕像素：线宽/把手按缩放放大，否则看不清。
  const uiScale = Math.min(4, 1 / Math.max(0.25, calState.zoom));
  for (const item of calState.layout.slots || []) {
    if (!calState.groups[item.group]) continue;
    const slot = drafts[item.key] || item.slot;
    const [x1, y1, x2, y2] = slotPx(slot, geo);
    const selected = item.key === calState.selected;
    if (calState.isolate && calState.selected && !selected) continue;
    const draft = !!drafts[item.key];
    ctx.lineWidth = (selected ? 3 : 1.5) * uiScale;
    ctx.strokeStyle = selected ? '#ffd60a' : draft ? '#ffffff' : (GROUP_COLORS[item.group] || '#58d68d');
    ctx.setLineDash(draft && !selected ? [6, 4] : []);
    if (item.shape === 'disc') {
      ctx.beginPath();
      ctx.ellipse((x1 + x2) / 2, (y1 + y2) / 2,
        Math.abs(x2 - x1) / 2, Math.abs(y2 - y1) / 2, 0, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    }
    ctx.setLineDash([]);
    if (selected) {
      const r = 5 * uiScale;
      ctx.fillStyle = '#ffd60a';
      for (const [hx, hy] of [[x1, y1], [x2, y1], [x1, y2], [x2, y2]]) {
        ctx.fillRect(hx - r, hy - r, r * 2, r * 2);
      }
    }
  }
  const drawing = calState.drawing;
  if (drawing) {
    ctx.setLineDash([7, 5]);
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#ffd60a';
    ctx.strokeRect(Math.min(drawing.start.x, drawing.cur.x),
      Math.min(drawing.start.y, drawing.cur.y),
      Math.abs(drawing.cur.x - drawing.start.x),
      Math.abs(drawing.cur.y - drawing.start.y));
    ctx.setLineDash([]);
  }
  const geoEl = document.getElementById('cal-geo');
  if (geoEl) {
    const diag = (calState.layout.diagnostics && calState.layout.diagnostics.aspect) || {};
    geoEl.textContent = `${geo.fw}×${geo.fh} · 内容区 ${geo.content.join(',')}`
      + (diag.text ? ` · ${diag.text}` : '');
  }
}

function calPointerPos(ev) {
  const canvas = document.getElementById('cal-canvas');
  const rect = canvas.getBoundingClientRect();
  return {
    x: (ev.clientX - rect.left) / rect.width * canvas.width,
    y: (ev.clientY - rect.top) / rect.height * canvas.height,
  };
}

function pxRectToSlot(x1, y1, x2, y2) {
  const geo = calGeo();
  const [cx1, cy1, cx2, cy2] = geo.content;
  const cw = cx2 - cx1, ch = cy2 - cy1;
  if (cw <= 0 || ch <= 0) return null;
  const nx1 = Math.max(0, Math.min(1, (Math.min(x1, x2) - cx1) / cw));
  const ny1 = Math.max(0, Math.min(1, (Math.min(y1, y2) - cy1) / ch));
  const nx2 = Math.max(0, Math.min(1, (Math.max(x1, x2) - cx1) / cw));
  const ny2 = Math.max(0, Math.min(1, (Math.max(y1, y2) - cy1) / ch));
  if (nx2 - nx1 < 0.004 || ny2 - ny1 < 0.004) return null;
  return [nx1, ny1, nx2, ny2].map((v) => Number(v.toFixed(6)));
}

function calHit(px, py) {
  const geo = calGeo();
  const drafts = calDrafts();
  const items = (calState.layout.slots || []).filter((s) =>
    calState.groups[s.group] && s.editable !== false);
  // 角把手命中范围按缩放放大；上限 14 画布 px，避免小框整个变成「角」。
  const tol = Math.max(5, Math.min(14, 8 / Math.max(0.25, calState.zoom)));
  for (const item of [...items].reverse()) {
    const slot = drafts[item.key] || item.slot;
    const [x1, y1, x2, y2] = slotPx(slot, geo);
    if (Math.abs(px - x1) < tol && Math.abs(py - y1) < tol) return { key: item.key, mode: 'nw' };
    if (Math.abs(px - x2) < tol && Math.abs(py - y1) < tol) return { key: item.key, mode: 'ne' };
    if (Math.abs(px - x1) < tol && Math.abs(py - y2) < tol) return { key: item.key, mode: 'sw' };
    if (Math.abs(px - x2) < tol && Math.abs(py - y2) < tol) return { key: item.key, mode: 'se' };
    if (px >= x1 && px <= x2 && py >= y1 && py <= y2) return { key: item.key, mode: 'move' };
  }
  return null;
}

function bindCalControls(ws) {
  const canvas = document.getElementById('cal-canvas');
  canvas.addEventListener('pointerdown', (ev) => {
    const p = calPointerPos(ev);
    let hit = calHit(p.x, p.y);
    if (ev.altKey) hit = null;          // Alt+拖 = 不管框在哪都重画
    if (!hit) {
      if (!calState.selected) {
        toast('先在左侧选一个槽位，再在画面上拖出新框', 'err');
        return;
      }
      calState.drawing = { start: p, cur: p };
      canvas.setPointerCapture(ev.pointerId);
      drawCalCanvas();
      return;
    }
    calState.selected = hit.key;
    const item = (calState.layout.slots || []).find((s2) => s2.key === hit.key);
    if (!item || item.editable === false) {
      renderCalLayers(); renderCalInspector(); drawCalCanvas();
      return;
    }
    const geo = calGeo();
    const slot = [...(calSlot(hit.key) || [])];
    calState.drag = {
      key: hit.key, mode: hit.mode, start: p, orig: slot,
      origPx: slotPx(slot, geo), geo: { ...geo },
    };
    canvas.setPointerCapture(ev.pointerId);
    renderCalLayers();
    renderCalInspector();
    drawCalCanvas();
  });
  canvas.addEventListener('pointermove', (ev) => {
    if (calState.drawing) {
      calState.drawing.cur = calPointerPos(ev);
      drawCalCanvas();
      return;
    }
    const drag = calState.drag;
    if (!drag) return;
    const p = calPointerPos(ev);
    const dx = (p.x - drag.start.x) / (drag.geo.content[2] - drag.geo.content[0]);
    const dy = (p.y - drag.start.y) / (drag.geo.content[3] - drag.geo.content[1]);
    const [x1, y1, x2, y2] = drag.orig;
    let s = [x1, y1, x2, y2];
    if (drag.mode === 'move') s = [x1 + dx, y1 + dy, x2 + dx, y2 + dy];
    else {
      if (drag.mode.includes('n')) { s[1] = y1 + dy; }
      if (drag.mode.includes('s')) { s[3] = y2 + dy; }
      if (drag.mode.includes('w')) { s[0] = x1 + dx; }
      if (drag.mode.includes('e')) { s[2] = x2 + dx; }
    }
    s = s.map((v) => Math.min(1, Math.max(0, v)));
    if (s[2] - s[0] < 0.005) s[2] = s[0] + 0.005;
    if (s[3] - s[1] < 0.005) s[3] = s[1] + 0.005;
    s = s.map((v) => Number(v.toFixed(6)));
    pushCalDraft(drag.key, s);
    drawCalCanvas();
  });
  canvas.addEventListener('pointerup', async (ev) => {
    if (calState.drawing) {
      const drawing = calState.drawing;
      calState.drawing = null;
      const p = calPointerPos(ev);
      const w = Math.abs(p.x - drawing.start.x);
      const h = Math.abs(p.y - drawing.start.y);
      const minPx = Math.max(4, 5 / Math.max(0.2, calState.zoom));
      if (w < minPx || h < minPx) {
        drawCalCanvas();
        return;
      }
      const slot = pxRectToSlot(drawing.start.x, drawing.start.y, p.x, p.y);
      if (!slot || !calState.selected) { drawCalCanvas(); return; }
      pushCalDraft(calState.selected, slot);
      renderCalLayers();
      renderCalInspector();
      drawCalCanvas();
      await persistCalEdit(calState.selected, slot);
      scheduleTrial(calState.selected, 150);
      return;
    }
    const drag = calState.drag;
    calState.drag = null;
    if (!drag) return;
    const slot = calDrafts()[drag.key];
    if (!slot) return;
    await persistCalEdit(drag.key, slot);
    renderCalInspector();
    scheduleTrial(drag.key, 150);
  });
  canvas.addEventListener('pointercancel', () => {
    calState.drag = null;
    calState.drawing = null;
    drawCalCanvas();
  });

  document.getElementById('cal-undo').addEventListener('click', async () => {
    try { const res = await api(`/api/workspaces/${ws.id}/calibration/undo`, { method: 'POST' });
      calState.draft = res.draft; afterDraftChange();
    } catch (err) { toast(err.message, 'err'); }
  });
  document.getElementById('cal-redo').addEventListener('click', async () => {
    try { const res = await api(`/api/workspaces/${ws.id}/calibration/redo`, { method: 'POST' });
      calState.draft = res.draft; afterDraftChange();
    } catch (err) { toast(err.message, 'err'); }
  });
  document.getElementById('cal-reset').addEventListener('click', async () => {
    const ok = await confirmDialog('重置草稿', '所有未应用的改动会丢弃（profile 不受影响）。', '重置');
    if (!ok) return;
    const res = await api(`/api/workspaces/${ws.id}/calibration/reset`, { method: 'POST' });
    calState.draft = res.draft; calState.selected = null; afterDraftChange();
  });
  document.getElementById('cal-search').addEventListener('input', (ev) => {
    calState.search = ev.target.value || '';
    renderCalLayers();
  });
  const stepSel = document.getElementById('cal-step');
  if (stepSel) {
    stepSel.value = String(calState.step);
    stepSel.addEventListener('change', (ev) => {
      calState.step = Number(ev.target.value) || 1;
    });
  }
  document.getElementById('cal-isolate').addEventListener('change', (ev) => {
    calState.isolate = ev.target.checked;
    drawCalCanvas();
  });
  document.querySelectorAll('[data-zoom]').forEach((el) =>
    el.addEventListener('click', () => {
      if (el.dataset.zoom === 'fit') {
        fitCalZoom();
        return;
      }
      calState.zoom = Number(el.dataset.zoom);
      document.getElementById('cal-zoom').textContent = `${calState.zoom}x`;
      layoutCalStage();
      drawCalCanvas();
    }));
  document.getElementById('cal-m1').addEventListener('change', (ev) => {
    calState.editableOnly = ev.target.checked;
    renderCalLayers();
  });
  document.getElementById('cal-overlay').addEventListener('change', async (ev) => {
    const enabled = ev.target.checked;
    try {
      const groups = GROUPS.map(([k]) => k).filter((k) => calState.groups[k]);
      await api(`/api/workspaces/${ws.id}/overlay`, {
        method: 'POST', body: { enabled, groups, selected: calState.selected },
      });
      calState.overlay = enabled;
      toast(enabled ? '浮层已开启' : '浮层已关闭', 'ok');
    } catch (err) {
      ev.target.checked = !enabled;
      toast(err.message, 'err');
    }
  });
  document.getElementById('cal-trial').addEventListener('click', () => {
    if (calState.selected) scheduleTrial(calState.selected, 0);
  });
  document.getElementById('cal-save').addEventListener('click', async () => {
    const count = Object.keys(calDrafts()).length;
    if (!count) { toast('草稿是空的', 'err'); return; }
    const ok = await confirmDialog('保存并应用',
      `<p>这会改共享 profile <b class="mono">${esc(calState.layout.profile)}</b>
       （${count} 个槽位）。</p>
       <p class="hint" style="margin-top:8px">会自动备份、原子写；如果 live 在跑，
       会先停止、保存后用新配置重启。启动失败会回滚备份并重启旧配置。</p>`,
      '保存并重启');
    if (!ok) return;
    try {
      const res = await api(`/api/workspaces/${ws.id}/calibration/apply`, { method: 'POST' });
      toast(`已保存 ${res.applied.length} 个槽位；备份 ${res.backup.split('/').pop()}`, 'ok');
      setTimeout(() => route(), 600);
    } catch (err) {
      const problems = (err.data && err.data.problems) || [];
      toast([err.message, ...problems].join('\n'), 'err');
    }
  });
  document.getElementById('cal-discard').addEventListener('click', async () => {
    const ok = await confirmDialog('放弃草稿', '未应用的改动全部丢弃。', '放弃');
    if (!ok) return;
    await api(`/api/workspaces/${ws.id}/calibration/reset`, { method: 'POST' });
    calState.draft = null; calState.selected = null; afterDraftChange();
  });
  const bindRestore = (name) => async () => {
    const ok = await confirmDialog('恢复备份',
      `<p>把 <b class="mono">${esc(name)}</b> 写回 profile？</p>
       <p class="hint" style="margin-top:8px">会先停 live，恢复后自动重启；当前草稿会被丢弃。</p>`,
      '恢复');
    if (!ok) return;
    try {
      await api(`/api/workspaces/${ws.id}/calibration/restore`, {
        method: 'POST', body: { backup: name },
      });
      toast('已恢复备份', 'ok');
      setTimeout(() => route(), 600);
    } catch (err) { toast(err.message, 'err'); }
  };
  document.querySelectorAll('[data-backup]').forEach((el) =>
    el.addEventListener('click', bindRestore(el.dataset.backup)));
  document.getElementById('cal-refresh').addEventListener('click', async () => {
    const layout = await api(`/api/workspaces/${ws.id}/slots`);
    calState.layout = layout;
    loadCalImage();
    toast('已刷新冻结帧', 'ok');
  });
}

function onCalKeydown(ev) {
  if (!document.getElementById('cal-canvas')) return;
  if (ev.key === 'Escape') {
    calState.drawing = null;
    calState.drag = null;
    drawCalCanvas();
    return;
  }
  if (!calState.selected || calState.drag) return;
  if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(ev.key)) return;
  if (document.activeElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) return;
  ev.preventDefault();
  const stepPx = (Number(calState.step) || 1) * (ev.shiftKey ? 10 : 1);
  const geo = calGeo();
  const slot = [...(calSlot(calState.selected) || [])];
  if (!slot.length) return;
  const dx = stepPx / (geo.content[2] - geo.content[0]);
  const dy = stepPx / (geo.content[3] - geo.content[1]);
  if (ev.key === 'ArrowLeft') { slot[0] -= dx; slot[2] -= dx; }
  if (ev.key === 'ArrowRight') { slot[0] += dx; slot[2] += dx; }
  if (ev.key === 'ArrowUp') { slot[1] -= dy; slot[3] -= dy; }
  if (ev.key === 'ArrowDown') { slot[1] += dy; slot[3] += dy; }
  const clamped = slot.map((v) => Number(Math.min(1, Math.max(0, v)).toFixed(6)));
  pushCalDraft(calState.selected, clamped);
  drawCalCanvas();
  persistCalEdit(calState.selected, clamped).then(() => scheduleTrial(calState.selected, 150));
}

async function persistCalEdit(key, slot) {
  try {
    const res = await api(`/api/workspaces/${calState.id}/calibration/edit`, {
      method: 'POST', body: { key, slot },
    });
    calState.draft = res.draft;
    updateCalChrome();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function afterDraftChange() {
  renderCalLayers();
  drawCalCanvas();
  updateCalChrome();
  updateCalInspector();
}

function updateCalChrome() {
  const count = Object.keys(calDrafts()).length;
  const countEl = document.getElementById('cal-count');
  if (countEl) countEl.textContent = String(count);
  const undo = document.getElementById('cal-undo');
  const redo = document.getElementById('cal-redo');
  if (undo) undo.disabled = !(calState.draft && calState.draft.can_undo);
  if (redo) redo.disabled = !(calState.draft && calState.draft.can_redo);
  renderCalDiff();
}

function renderCalDiff() {
  const box = document.getElementById('cal-diff');
  if (!box) return;
  const diff = (calState.draft && calState.draft.diff) || [];
  box.innerHTML = diff.length ? diff.map((d) => `
    <div>${esc(d.label || d.key)} · ${esc(d.key)}
      ${d.old ? ` · Δ ${d.dx_px >= 0 ? '+' : ''}${d.dx_px}px / ${d.dy_px >= 0 ? '+' : ''}${d.dy_px}px` : ' · 新增槽位'}
      ${d.dw_px != null ? ` · 尺寸 ${d.dw_px >= 0 ? '+' : ''}${d.dw_px}×${d.dh_px}px` : ''}</div>`).join('')
    : '<span class="faint">还没有改动</span>';
}

function scheduleTrial(key, delay) {
  clearTimeout(calState.trialTimer);
  calState.trialTimer = setTimeout(() => runTrial(key), Math.max(0, delay));
}

async function runTrial(key) {
  if (!key) return;
  const inspector = document.getElementById('cal-inspector');
  if (inspector) inspector.innerHTML = '<div class="empty"><span class="spinner"></span> 试读中…</div>';
  try {
    const slot = calDrafts()[key] || (calSlot(key) || undefined);
    const res = await api(`/api/workspaces/${calState.id}/calibration/trial`, {
      method: 'POST', body: { key, slot },
    });
    calState.trial = res;
    // 用户正在输入坐标时不要重渲染检查器（会吞掉未提交的输入）。
    const active = document.activeElement;
    if (active && active.tagName === 'INPUT'
        && String(active.id || '').startsWith('cal-num-')) {
      return;
    }
    renderCalInspector();
  } catch (err) {
    if (inspector) inspector.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function renderCalInspector() {
  const box = document.getElementById('cal-inspector');
  const key = calState.selected;
  if (!box || !key) return;
  const item = (calState.layout.slots || []).find((s) => s.key === key);
  const trial = calState.trial && calState.trial.key === key ? calState.trial : null;
  const slot = calDrafts()[key] || (item && item.slot);
  const geo = calGeo();
  const px = slot ? slotPx(slot, geo).map((v) => Math.round(v)) : [];
  box.innerHTML = `
    <h4>${esc(item ? item.label : key)}</h4>
    <div class="hint mono">${esc(key)}</div>
    <div class="hint" style="margin:4px 0 8px">
      ${esc(item ? item.group : '')} · ${esc(item ? item.stage || '' : '')}
    </div>
    <dl class="kv" style="grid-template-columns:78px 1fr;font-size:12px">
      <dt>归一化</dt><dd class="mono">${slot ? slot.map((v) => Number(v).toFixed(4)).join(', ') : '-'}</dd>
      <dt>像素</dt><dd class="mono">${px.join(', ')}</dd>
    </dl>
    ${slot ? `
    <div class="row" style="margin-top:8px">
      <div class="field"><label>x1</label>
        <input class="input" id="cal-num-0" type="number" step="0.001" min="0" max="1"
          value="${Number(slot[0]).toFixed(4)}"></div>
      <div class="field"><label>y1</label>
        <input class="input" id="cal-num-1" type="number" step="0.001" min="0" max="1"
          value="${Number(slot[1]).toFixed(4)}"></div>
    </div>
    <div class="row">
      <div class="field"><label>x2</label>
        <input class="input" id="cal-num-2" type="number" step="0.001" min="0" max="1"
          value="${Number(slot[2]).toFixed(4)}"></div>
      <div class="field"><label>y2</label>
        <input class="input" id="cal-num-3" type="number" step="0.001" min="0" max="1"
          value="${Number(slot[3]).toFixed(4)}"></div>
    </div>
    <div class="inline" style="margin-top:6px">
      <button class="btn btn-sm" id="cal-num-apply">应用坐标</button>
      <span class="hint">归一化 0–1；也可直接在画面上拖新框</span>
    </div>` : ''}
    <div class="sep"></div>
    ${trial ? `
      ${trial.crop ? `<div class="crop"><img src="${trial.crop}" alt="裁剪"></div>` : ''}
      ${trial.text ? `<div class="ocr-text">${esc(trial.text)}</div>` : ''}
      ${trial.kind === 'button' || trial.kind === 'slider' ? '' : `
        <div class="hint">置信度 ${Number(trial.score || 0).toFixed(2)}</div>
        <div class="score-bar"><i style="width:${Math.round((trial.score || 0) * 100)}%"></i></div>`}
      ${trial.parsed != null ? `<div class="mono break">${esc(JSON.stringify(trial.parsed))}</div>` : ''}
      <div class="${trial.ok ? 'green' : 'reason'}" style="margin-top:6px">${esc(trial.reason || '')}</div>
      <div class="hint" style="margin-top:6px">${esc(trial.note || '')} · ${trial.ms}ms</div>
    ` : '<div class="empty">点「重新试读」</div>'}`;
  if (trial && (trial.kind === 'button' || trial.kind === 'slider')) {
    const extra = document.createElement('div');
    extra.className = 'hint';
    extra.textContent = trial.hit == null ? '' : (trial.hit ? '框内找到按钮' : '框内没找到按钮');
    box.appendChild(extra);
  }
  const apply = box.querySelector('#cal-num-apply');
  if (apply) {
    apply.addEventListener('click', applyCalNumbers);
    box.querySelectorAll('[id^="cal-num-"]').forEach((inp) => {
      if (inp.id === 'cal-num-apply') return;
      inp.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); applyCalNumbers(); }
      });
    });
  }
}

function applyCalNumbers() {
  const key = calState.selected;
  if (!key) return;
  const vals = [0, 1, 2, 3].map((i) => {
    const el = document.getElementById(`cal-num-${i}`);
    return el ? Number(el.value) : NaN;
  });
  if (vals.some((v) => !Number.isFinite(v))) {
    toast('坐标必须是数字', 'err');
    return;
  }
  const clamped = vals.map((v) => Math.max(0, Math.min(1, v)));
  if (clamped[2] - clamped[0] < 0.004 || clamped[3] - clamped[1] < 0.004) {
    toast('x2/y2 必须大于 x1/y1（至少 0.004）', 'err');
    return;
  }
  const slot = clamped.map((v) => Number(v.toFixed(6)));
  pushCalDraft(key, slot);
  renderCalLayers();
  drawCalCanvas();
  persistCalEdit(key, slot).then(() => scheduleTrial(key, 120));
}

function updateCalInspector() {
  renderCalInspector();
}

function renderCalDiagnostics() {
  const box = document.getElementById('cal-diag-body');
  if (!box) return;
  const diag = (calState.layout && calState.layout.diagnostics) || {};
  const rows = diag.rows || [];
  const verdicts = diag.verdicts || [];
  box.innerHTML = `<table class="table"><tbody>${rows.map((r) => `
      <tr><td class="nowrap ${r.status === 'warn' ? 'yellow' : ''}">${esc(r.label)}</td>
        <td class="mono">${esc(r.value)}</td>
        <td class="hint">${esc(r.detail || '')}</td></tr>`).join('')}</tbody></table>`
    + (verdicts.length ? verdicts.map((v) =>
      `<div class="reason" style="margin-top:6px">${esc(v.text)}</div>`).join('') : '');
}

let overlaySyncTimer = null;

function syncOverlaySelection() {
  const toggle = document.getElementById('cal-overlay');
  if (!toggle || !toggle.checked) return;
  clearTimeout(overlaySyncTimer);
  overlaySyncTimer = setTimeout(async () => {
    try {
      const groups = GROUPS.map(([k]) => k).filter((k) => calState.groups[k]);
      await api(`/api/workspaces/${calState.id}/overlay/update`, {
        method: 'POST', body: { groups, selected: calState.selected },
      });
    } catch (err) { /* 浮层是 fail-open，不打扰标定 */ }
  }, 160);
}

/* 方向键微调：只注册一次，避免每次进标定页叠加监听器。 */
document.addEventListener('keydown', onCalKeydown);
