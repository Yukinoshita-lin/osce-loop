/* OSCE·Loop P0 评分副驾 — 单页工作台 */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const STATUS = {
  published: ['已发布', 'teal'],
  signed_off: ['已签字', 'blue'],
  reviewed: ['已复核', 'blue'],
  pending_review: ['待复核', 'amber'],
  integrity_hold: ['证据缺口', 'red'],
  scored: ['已评分', 'muted'],
  auto: ['自动通过', 'muted'],
  accepted: ['已接受', 'teal'],
  corrected: ['已修正', 'blue'],
  rejected: ['已拒绝', 'red'],
  manual_scored: ['人工已评', 'teal'],
  manual_pending: ['待人工评分', 'amber'],
};

const REASON_COLOR = {
  integrity_gap: 'red',
  asr_low_confidence: 'amber',
  asr_cohort_gap: 'red',
  risk_escalation: 'red',
  critical_negative: 'red',
  review_disagreement: 'red',
  near_cutoff: 'amber',
  low_score_tail: 'amber',
  l3_human_confirmation: 'amber',
  confidence_below_floor: 'amber',
  item_disagreement: 'amber',
  appeal: 'purple',
  manual_only: 'blue',
  extract_only: 'blue',
  ai_refusal: 'amber',
  ai_degraded: 'amber',
  sampling: 'muted',
};

const state = {
  token: localStorage.getItem('osceloop.token') || '',
  user: null,
  reviewer: localStorage.getItem('osceloop.reviewer') || '主治 张蕾',
  queue: null,
  queueFilter: { search: '', reason: '' },
  currentReview: null,
  reviewPlayed: false,
  reviewMode: null,
  reviewScore: null,
  reviewStartedAt: 0,
  sp: null,
  spBundleId: null,
  spStations: [],
  feedbackId: null,
  bundleCurrent: null,
  bundlePayload: null,
  bundleFocus: null,
  bundleDraft: null,
  ingestSlot: null,
};

/* ---------- utils ---------- */
function esc(value) {
  return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function num(value, digits = 2) {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '—';
  return Number(value).toFixed(digits);
}
function pct(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return (Number(value) * 100).toFixed(digits) + '%';
}
function mmss(ms) {
  if (ms === null || ms === undefined) return '—';
  const total = Math.max(0, Math.floor(Number(ms) / 1000));
  return String(Math.floor(total / 60)).padStart(2, '0') + ':' + String(total % 60).padStart(2, '0');
}
function bytesText(value) {
  const n = Number(value) || 0;
  if (n >= 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}
function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function statusPill(status) {
  const item = STATUS[status] || [status || '—', 'muted'];
  return `<span class="pill ${item[1]}">${esc(item[0])}</span>`;
}
function reasonPill(reason, label) {
  const color = REASON_COLOR[reason] || 'muted';
  return `<span class="pill ${color}">${esc(label || reason || '—')}</span>`;
}
function scoreText(score) {
  if (score === 0) return 'negative';
  if (score === 1) return 'partial';
  if (score === 2) return 'positive';
  return '—';
}
function checkMark(status) {
  if (status === 'pass') return '<span class="check pass">PASS</span>';
  if (status === 'fail') return '<span class="check fail">FAIL</span>';
  return '<span class="check unmeasured">未采集</span>';
}
function gateStatusPill(status) {
  if (status === 'pass') return '<span class="pill teal">通过</span>';
  if (status === 'fail') return '<span class="pill red">未通过</span>';
  return '<span class="pill amber">待补测</span>';
}
async function api(path, options = {}) {
  const opts = {
    ...options,
    headers: { ...(options.headers || {}) },
  };
  if (state.token) opts.headers.Authorization = `Bearer ${state.token}`;
  if (opts.body && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (e) { /* ignore */ }
    if (res.status === 401 && !path.includes('/api/auth/login')) handleUnauthorized();
    throw new Error(detail);
  }
  return res.json();
}
async function apiForm(path, formData) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { method: 'POST', body: formData, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (e) { /* ignore */ }
    if (res.status === 401) handleUnauthorized();
    throw new Error(detail);
  }
  return res.json();
}
let toastTimer = null;
function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast show' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 3600);
}
function setTitle(title) {
  $('#page-title').textContent = title;
  document.title = 'OSCE·Loop · ' + title;
}
function setActiveNav(view) {
  $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
}
function setPendingBadge(count) {
  const el = $('#nav-pending');
  if (!el) return;
  el.textContent = count > 99 ? '99+' : String(count);
  el.style.display = count ? '' : 'none';
}
function attemptFlags(a) {
  const flags = [];
  if (a.integrity_gap) flags.push('<span class="pill red">integrity_gap</span>');
  if (a.near_cutoff) flags.push('<span class="pill amber">near_cutoff</span>');
  if (a.appeal) flags.push('<span class="pill purple">申诉中</span>');
  if (a.risk_escalated_at) flags.push('<span class="pill red">已升级</span>');
  if (a.critical_pending) flags.push(`<span class="pill red">关键项 ${a.critical_pending}</span>`);
  return flags.join(' ');
}
function dataAttr(value) {
  return esc(value);
}

/* ---------- auth ---------- */
function showAuthGate() {
  const gate = $('#auth-gate');
  if (gate) gate.classList.remove('hidden');
  const input = $('#login-username');
  if (input) setTimeout(() => input.focus(), 60);
}
function hideAuthGate() {
  const gate = $('#auth-gate');
  if (gate) gate.classList.add('hidden');
}
function clearAuth() {
  state.token = '';
  state.user = null;
  localStorage.removeItem('osceloop.token');
}
function handleUnauthorized() {
  if (!state.token && !state.user) return;
  clearAuth();
  $('#user-box').hidden = true;
  showAuthGate();
  toast('登录已失效，请重新登录', true);
}
function firstAllowedRoute() {
  const link = $$('#nav a').find((a) => !a.hidden);
  return link ? link.getAttribute('href') : '#/sp';
}
function applyRoleNav() {
  if (!state.user) return;
  $$('#nav a').forEach((a) => {
    const roles = (a.dataset.roles || '').split(',');
    a.hidden = !roles.includes(state.user.role);
  });
  const current = location.hash || '#/overview';
  const link = $$('#nav a').find((a) => a.getAttribute('href') === current);
  if (!link || link.hidden) {
    location.hash = firstAllowedRoute();
  }
}
function setUser(user) {
  state.user = user;
  $('#user-box').hidden = false;
  $('#user-name').textContent = user.display_name;
  $('#user-role').textContent = user.role_label;
  const canReview = ['admin', 'exam_manager', 'examiner'].includes(user.role);
  $('#reviewer-box').hidden = !canReview;
  if (canReview) {
    state.reviewer = user.display_name;
    $('#reviewer-input').value = user.display_name;
    localStorage.setItem('osceloop.reviewer', state.reviewer);
  }
  applyRoleNav();
}

/* ---------- router ---------- */
const ROUTES = {
  overview: { title: '总览', render: viewOverview },
  review: { title: '复核队列', render: viewReview },
  ingest: { title: '录像录入', render: viewIngest },
  schedule: { title: '考务编排', render: viewSchedule },
  execution: { title: '考试执行', render: viewExecution },
  bundle: { title: '站包契约', render: viewBundle },
  transparency: { title: '透明公开', render: viewTransparency },
  feedback: { title: '反馈报告', render: viewFeedback },
  sp: { title: 'SP 演练', render: viewSP },
  quality: { title: '质量与闸门', render: viewQuality },
};

function currentRoute() {
  const hash = location.hash || '#/overview';
  const name = hash.replace(/^#\/?/, '').split('?')[0] || 'overview';
  return ROUTES[name] ? name : 'overview';
}

async function route() {
  if (!state.user) {
    showAuthGate();
    return;
  }
  const name = currentRoute();
  const link = $(`#nav a[data-view="${name}"]`);
  if (link && link.hidden) {
    location.hash = firstAllowedRoute();
    return;
  }
  const def = ROUTES[name];
  setActiveNav(name);
  setTitle(def.title);
  $('#view').innerHTML = '<div class="loading">正在载入工作台…</div>';
  try {
    await def.render();
  } catch (err) {
    $('#view').innerHTML = `<div class="empty">载入失败：${esc(err.message)}<br><br><button class="btn" onclick="location.reload()">重试</button></div>`;
    toast(err.message, true);
  }
}

/* ---------- overview ---------- */
async function viewOverview() {
  const data = await api('/api/overview');
  setPendingBadge(data.stats.pending || 0);
  const stageLabels = {
    G0_offline: 'G0 离线副驾',
    G1_practice: 'G1 练习沙箱',
    G2_simulation: 'G2 模拟考',
    G3_limited: 'G3 有限高利害',
  };
  $('#stage-pill').textContent = stageLabels[data.release_stage] || data.release_stage;
  const qwkClass = data.stats.qwk_ai >= 0.7 ? 'teal' : data.stats.qwk_ai >= 0.6 ? 'amber' : 'red';
  const routingMax = Math.max(1, ...data.routing.map((r) => r.count));
  const gate = data.p0_gate;
  const gateRows = gate ? gate.criteria.map((c) => {
    let value = '—';
    if (c.key === 'evidence_rate') value = pct(c.value, 0);
    else if (c.key === 'avg_review_seconds') value = c.value === null ? '—' : `${num(c.value, 0)}s`;
    else value = c.value === null ? '—' : num(c.value, 3);
    return `<div class="gate-mini-row">
      <span>${esc(c.label)}</span>
      <span class="mono small dim">${esc(value)}</span>
      ${checkMark(c.status)}
    </div>`;
  }).join('') : '';

  const alertHtml = (data.alerts || []).map((a) => `
    <div class="alert ${a.level === 'danger' ? 'danger' : ''}">
      <b>${esc(a.title)}</b><p>${esc(a.detail)}</p>
    </div>`).join('');

  const attemptsRows = data.attempts.map((a) => `
    <tr>
      <td><div class="mono tiny muted">${esc(a.candidate_code)}</div><div>${esc(a.candidate_name)}</div></td>
      <td>${statusPill(a.status)}<div class="tiny" style="margin-top:3px">${attemptFlags(a)}</div></td>
      <td class="num">${num(a.ai_total, 1)}</td>
      <td class="num">${a.final_total === null ? '—' : num(a.final_total, 1)}</td>
      <td class="num">${a.pending}</td>
      <td class="small dim">${a.passed_note || (a.final_total !== null && a.final_total >= a.pass_line ? '达标' : a.final_total === null ? '待定' : '未达标')}</td>
      <td class="nowrap">
        <a class="link-btn" href="#/review">复核</a>
        <span class="muted"> · </span>
        <a class="link-btn" href="#/feedback" data-open-feedback="${esc(a.attempt_id)}">反馈</a>
        ${a.rerun_allowed ? `<span class="muted"> · </span><button class="link-btn" data-action="rescore" data-attempt="${esc(a.attempt_id)}">重跑评分</button>` : ''}
        ${a.pending === 0 && a.status !== 'signed_off' && a.status !== 'published' ? `<span class="muted"> · </span><button class="link-btn" data-action="signoff" data-attempt="${esc(a.attempt_id)}" data-failing="${a.final_total !== null && a.final_total < a.pass_line ? '1' : '0'}">${a.final_total !== null && a.final_total < a.pass_line ? '签字（需寄语）' : '签字'}</button>` : ''}
        ${a.status === 'signed_off' ? `<span class="muted"> · </span><button class="link-btn" data-action="publish" data-attempt="${esc(a.attempt_id)}">发布</button>` : ''}
      </td>
    </tr>`).join('');

  const auditRows = data.recent_audit.map((ev) => `
    <div class="audit-row">
      <div class="act">${esc(ev.action)}</div>
      <div class="obj">${esc(ev.object_id)} · ${esc(ev.actor)}</div>
      <div class="time">${fmtTime(ev.created_at)}</div>
    </div>`).join('');

  $('#view').innerHTML = `
    <section class="hero">
      <div class="eyebrow">P0 · OFFLINE SCORING COPILOT</div>
      <h2>把 AI 放进评分流程，但只在<span class="accent">该放手的地方</span>放手</h2>
      <p>当前范围：历史录像离线评分。AI 只给项级建议分与可回放证据，低置信、关键项、边缘案例与证据缺口强制路由到人。</p>
    </section>

    ${alertHtml}

    <div class="kpis">
      <div class="kpi amber"><div class="k">待人工复核</div><div class="v">${data.stats.pending}<small>项</small></div><div class="d">${data.stats.attempts} 场历史录像 · ${data.stats.published} 场已发布</div></div>
      <div class="kpi ${qwkClass}"><div class="k">AI–人 QWK</div><div class="v">${num(data.stats.qwk_ai, 3)}</div><div class="d">人工复核后 ${num(data.stats.qwk_human, 3)} · 目标 ≥ 0.70</div></div>
      <div class="kpi blue"><div class="k">单条复核耗时</div><div class="v">${num(data.stats.avg_review_seconds, 0)}<small>秒</small></div><div class="d">目标 ≤ 90 秒 · 编辑率 ${pct(data.stats.edit_rate, 0)}</div></div>
      <div class="kpi teal"><div class="k">证据定位率</div><div class="v">${pct(data.stats.evidence_rate, 0)}</div><div class="d">缺口场次 ${pct(data.stats.integrity_gap_rate, 1)}</div></div>
    </div>

    <div class="grid c2-1" style="margin-bottom:16px">
      <div class="card">
        <div class="card-title">风险路由分布<span class="sub">按具名原因，不是黑箱开关</span></div>
        <div class="bars">
          ${data.routing.length ? data.routing.map((r) => `
            <div class="bar-row">
              <span class="bar-label" title="${esc(r.label)}">${esc(r.label)}</span>
              <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, r.count / routingMax * 100)}%"></div></div>
              <span class="bar-count">${r.count}</span>
            </div>`).join('') : '<div class="empty">暂无路由记录</div>'}
        </div>
        <div class="sep"></div>
        <div class="tiny muted">路由顺序：证据缺口 → 关键项阴性 → 边缘案例 → 低置信 → 历史分歧 → 申诉 → 抽检。命中即停。</div>
      </div>
      <div class="card">
        <div class="card-title">P0 放行闸门<span class="sub">${gate ? gate.id + ' · ' + gate.name : '—'}</span></div>
        <div class="gate-mini">${gateRows || '<div class="empty">未配置</div>'}</div>
        <div class="sep"></div>
        <div class="btn-row">
          <a class="btn small primary" href="#/quality">查看全部闸门</a>
          <a class="btn small" href="#/review">进入复核队列</a>
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">场次总览<span class="sub">13 场历史录像 · 同一冻结站包 · 金标准仅用于质量评测</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>考生</th><th>状态</th><th class="num">AI 总分</th><th class="num">最终分</th><th class="num">待复核</th><th>结论</th><th></th></tr></thead>
          <tbody>${attemptsRows || '<tr><td colspan="7" class="empty">暂无场次</td></tr>'}</tbody>
        </table>
      </div>
    </div>

    <div class="grid c2">
      <div class="card">
        <div class="card-title">版本与模型<span class="sub">三元版本戳</span></div>
        <div class="version-row"><span class="k">model_version</span><span class="v">${esc(data.versions.model)}</span></div>
        <div class="version-row"><span class="k">prompt_version</span><span class="v">${esc(data.versions.prompt)}</span></div>
        <div class="version-row"><span class="k">release_stage</span><span class="v">${esc(data.release_stage)}</span></div>
        <div class="sep"></div>
        <div class="tiny muted">模型或提示词版本变化会自动把 suggest/decide 权限降为 extract，直到完成新一轮人工比对校准。</div>
      </div>
      <div class="card">
        <div class="card-title">最近审计事件<span class="sub">只追加</span></div>
        <div class="audit-list">${auditRows || '<div class="empty">暂无审计</div>'}</div>
      </div>
    </div>
  `;
  $$('#view [data-open-feedback]').forEach((el) => {
    el.addEventListener('click', () => {
      state.feedbackId = el.dataset.openFeedback;
    });
  });
}

/* ---------- review ---------- */
async function viewReview() {
  const queue = await api('/api/review/queue');
  state.queue = queue;
  setPendingBadge(queue.total);
  state.queueFilter = { search: '', reason: '' };
  const reasonOptions = queue.by_reason
    .filter((r) => r.count > 0)
    .map((r) => `<option value="${dataAttr(r.reason)}">${esc(r.label)} (${r.count})</option>`)
    .join('');
  $('#view').innerHTML = `
    <div class="view-head">
      <div>
        <h2>复核队列</h2>
        <p>按风险路由排序。打开记录先播放证据片段，AI 建议在播放后才显示——避免先看分数再找理由。</p>
      </div>
      <div class="stat-line"><span>待复核 <b>${queue.total}</b></span><span>超时 <b>${queue.overdue_count || 0}</b></span><span>原因 <b>${queue.by_reason.length}</b> 类</span></div>
    </div>
    ${queue.overdue_count ? `<div class="alert danger"><b>${queue.overdue_count} 条待复核已超过 SLA</b><p>最久等待 ${num(queue.oldest_wait_hours, 1)} 小时，已自动置顶。目标：L3 ≤ 24h · L2 ≤ 48h · L1 ≤ 72h。</p></div>` : ''}
    <div class="review-layout">
      <section class="queue-panel">
        <div class="queue-head">
          <div class="row"><h3>待办</h3><span class="pill amber">${queue.total}</span></div>
          <div class="queue-filters">
            <input id="queue-search" class="input" placeholder="搜索考生 / 评分项">
            <select id="queue-reason" class="input"><option value="">全部原因</option>${reasonOptions}</select>
          </div>
        </div>
        <div class="queue-list" id="queue-list"></div>
      </section>
      <section class="workbench" id="workbench">
        <div class="empty">从左侧选择一条待复核记录</div>
      </section>
    </div>
  `;
  renderQueueList();
  $('#queue-search').addEventListener('input', (e) => {
    state.queueFilter.search = e.target.value.trim().toLowerCase();
    renderQueueList();
  });
  $('#queue-reason').addEventListener('change', (e) => {
    state.queueFilter.reason = e.target.value;
    renderQueueList();
  });
  const first = filteredQueue()[0];
  if (first) {
    await loadReview(first.record_id);
  }
}

function filteredQueue() {
  if (!state.queue) return [];
  return state.queue.items.filter((item) => {
    const s = state.queueFilter.search;
    const matchSearch = !s || `${item.candidate} ${item.item_title} ${item.item_id}`.toLowerCase().includes(s);
    const matchReason = !state.queueFilter.reason || item.routing.reason === state.queueFilter.reason;
    return matchSearch && matchReason;
  });
}

function renderQueueList() {
  const list = $('#queue-list');
  if (!list) return;
  const items = filteredQueue();
  if (!items.length) {
    list.innerHTML = '<div class="empty">没有匹配的待复核记录</div>';
    return;
  }
  list.innerHTML = items.map((item) => `
    <button class="queue-item ${state.currentReview && state.currentReview.record_id === item.record_id ? 'active' : ''}" data-record-id="${dataAttr(item.record_id)}">
      <div class="q-top">
        <span class="q-title">${item.critical ? '<span class="pill badge-critical">关键</span> ' : ''}${esc(item.item_title)}</span>
        <span class="mono tiny muted">${esc(item.item_id)}</span>
      </div>
      <div class="q-sub"><span>${esc(item.candidate)}</span><span>${esc(item.risk_level)}</span><span>权重 ${item.weight}</span>${item.overdue ? `<span class="pill red">超时 ${num(item.age_hours, 0)}h</span>` : ''}</div>
      <div class="q-reason">${reasonPill(item.routing.reason, item.routing.reason_label)}</div>
    </button>`).join('');
  $$('.queue-item', list).forEach((btn) => {
    btn.addEventListener('click', () => loadReview(btn.dataset.recordId));
  });
}

async function loadReview(recordId) {
  const data = await api(`/api/review/${encodeURIComponent(recordId)}`);
  state.currentReview = data;
  state.reviewPlayed = data.evidence.length === 0;
  state.reviewMode = null;
  state.reviewScore = null;
  state.reviewStartedAt = Date.now();
  renderQueueList();
  renderWorkbench(data);
  setupReviewAudio(data);
}

function dualReviewHtml(dual) {
  if (!dual) return '';
  const reviews = dual.reviews || [];
  const status = dual.status || 'awaiting_second';
  const title = status === 'disagreement'
    ? '双人评分不一致，需高年资裁决'
    : status === 'resolved'
      ? '双人独立复核已完成'
      : '等待第二位独立复核员';
  return `<div class="alert ${status === 'disagreement' ? 'danger' : ''}" style="margin-top:10px">
    <b>${title}</b>
    <p>关键项要求 ${dual.required || 2} 位不同评分员独立给出档位；同一评分员不可重复提交。${dual.blind && !dual.viewer_submitted ? '当前为盲评：完成前不显示另一位评分员的档位。' : ''}</p>
    ${reviews.map((r, i) => r.action === 'hidden'
      ? `<div class="tiny">第 ${i + 1} 位 · 另一位评分员 · <span class="pill muted">盲评中</span></div>`
      : `<div class="tiny">第 ${i + 1} 位 · ${esc(r.reviewer)} → ${r.score === null || r.score === undefined ? '—' : esc(r.score)} 档 · ${esc(r.action)} · ${fmtTime(r.at)}</div>`).join('')}
  </div>`;
}

function renderWorkbench(data) {
  const wb = $('#workbench');
  if (!wb) return;
  const ev = data.evidence && data.evidence[0];
  const attempt = data.attempt || {};
  const clipLabel = ev ? `${mmss(ev.start_ms)} – ${mmss(ev.end_ms)}` : '无片段';
  const evidenceHtml = ev
    ? `<div class="clip-meta">
         <span>${esc(ev.speaker || 'unknown')}</span>
         <span>${clipLabel}</span>
         <span>${ev.evidence_kind === 'absence' ? '缺席证据' : ev.evidence_kind === 'note' ? '考生笔记' : 'utterance'}</span>
       </div>
       <blockquote class="quote ${ev.evidence_kind === 'absence' ? 'absence' : ''}">${esc(ev.quote)}</blockquote>
       ${ev.note ? `<div class="tiny muted">${esc(ev.note)}</div>` : ''}`
    : `<div class="quote absence">该项为人工保留项 / 无 AI 证据片段，请回放整段录像后人工评分。</div>`;
  const anchor = data.anchor || {};
  wb.innerHTML = `
    <div class="wb-head">
      <div>
        <div class="mono tiny muted">${esc(data.item_id)} · ${esc(data.phase_label)} · 权重 ${data.weight}</div>
        <h2>${esc(data.item_title)}</h2>
        <div class="sub">${esc(data.candidate)} · ${data.critical ? '<span class="pill badge-critical">关键项</span>' : '非关键项'} · ${esc(data.authority_label)}</div>
      </div>
      <div class="wb-badges">
        ${reasonPill(data.routing.reason, data.routing.reason_label)}
        <span class="pill">${esc(data.routing.route_label)}</span>
        <span class="pill muted">${esc(data.evidence_type)}</span>
      </div>
    </div>

    <div class="attempt-strip">
      <span>场次 <b>${esc(attempt.candidate_code || '')}</b></span>
      <span>状态 ${statusPill(attempt.status)}</span>
      <span>AI 总分 <b>${num(attempt.ai_total, 1)}</b></span>
      <span>最终分 <b>${attempt.final_total === null ? '—' : num(attempt.final_total, 1)}</b></span>
      <span>待复核 <b>${attempt.pending ?? '—'}</b></span>
      ${attempt.integrity_gap ? '<span class="pill red">integrity_gap</span>' : ''}
      ${(attempt.anomalies || []).map((item) => `<span class="pill ${item.severity === 'high' ? 'red' : item.severity === 'medium' ? 'amber' : 'muted'}" title="${dataAttr(item.detail)}">${esc(item.label)}</span>`).join('')}
      ${attempt.near_cutoff ? '<span class="pill amber">near_cutoff</span>' : ''}
      ${attempt.risk_level !== 'L3' && attempt.status !== 'published' ? `<button class="btn small danger" data-action="risk-escalate" data-attempt="${dataAttr(attempt.attempt_id)}">升级为 L3</button>` : ''}
      ${attempt.risk_escalated_at ? `<span class="pill red">风险已上调 · ${esc(fmtTime(attempt.risk_escalated_at))}</span>` : ''}
      <button class="btn small" data-action="export-attempt" data-attempt="${dataAttr(attempt.attempt_id)}">导出申诉包</button>
      <button class="btn small" data-action="trace-record" data-attempt="${dataAttr(attempt.attempt_id)}" data-item="${dataAttr(data.item_id)}">成绩追溯</button>
    </div>
    <div id="trace-result" class="tiny" style="margin:8px 0"></div>
    ${dualReviewHtml(data.dual_review)}

    <div class="wb-grid">
      <section class="card clip-card">
        <div class="card-title">证据片段<span class="sub">${esc(clipLabel)}</span></div>
        ${evidenceHtml}
        ${attempt.media_purged_at
          ? `<div class="quote absence">媒体已按留存策略清理（${esc(fmtTime(attempt.media_purged_at))}），证据时间戳与哈希链仍保留；如需回放请走申诉调档。</div>`
          : `<audio id="review-audio" preload="metadata" controls src="${dataAttr(ev && data.media_url ? data.media_url : (data.media_url || ''))}"></audio>`}
        ${attempt.media_sha256 ? `<div class="tiny muted mono" style="margin-top:6px">媒体校验值 ${esc(attempt.media_sha256)}</div>` : ''}
        ${attempt.transcript_sha256 ? `<div class="tiny muted mono" style="margin-top:2px">转写校验值 ${esc(attempt.transcript_sha256)}
          <span class="pill ${attempt.transcript_integrity === 'ok' ? 'teal' : attempt.transcript_integrity === 'mismatch' ? 'red' : 'amber'}">${
            attempt.transcript_integrity === 'ok' ? '一致' : attempt.transcript_integrity === 'mismatch' ? '不一致' : attempt.transcript_integrity === 'unrecorded' ? '未记录' : '无转写'
          }</span></div>` : ''}
        ${data.evidence_integrity && data.evidence_integrity.checked ? `<div class="tiny muted" style="margin-top:2px">证据哈希
          <span class="pill ${data.evidence_integrity.passed ? 'teal' : 'red'}">${data.evidence_integrity.matched}/${data.evidence_integrity.checked} 一致</span></div>` : ''}
        <div class="clip-actions">
          <button class="btn small primary" data-action="play-clip">播放证据片段 ±3 秒</button>
          <span class="play-status" id="play-status">${data.evidence.length ? '等待播放' : '无 AI 片段'}</span>
        </div>
        ${ev && ev.evidence_kind === 'absence' ? '<div class="tiny muted" style="margin-top:6px">阴性判定使用阶段窗口作为缺席证据，播放窗口为节选，仍需人工确认边界。</div>' : ''}
        ${ev && ev.evidence_kind === 'note' ? '<div class="tiny muted" style="margin-top:6px">笔记证据：播放窗口为记录时刻附近，原文以考生笔记为准。</div>' : ''}
        ${attempt.integrity_gap ? `<div class="sep"></div>
          <div class="tiny" style="color:var(--amber)">证据链缺口：${esc(attempt.integrity_note || '未记录原因')}</div>
          ${attempt.integrity_supplement
            ? `<div class="tiny" style="margin-top:4px">现场补充记录：${esc(attempt.integrity_supplement)} · ${esc(attempt.integrity_supplement_by || '—')} ${fmtTime(attempt.integrity_supplement_at)}</div>`
            : `<div class="btn-row" style="margin-top:6px;gap:6px"><input id="integrity-supplement-input" class="input" maxlength="500" placeholder="补充现场记录（不少于 10 字），签字前必填" style="flex:1"><button class="btn small" data-action="integrity-supplement" data-attempt="${dataAttr(attempt.attempt_id)}">保存补充记录</button></div>`}` : ''}
      </section>
      <section class="card" id="ai-panel">
        ${aiPanelHtml(data, !state.reviewPlayed && data.evidence.length > 0)}
      </section>
    </div>

    <section class="card">
      <div class="card-title">行为锚三档<span class="sub">可观测、可判定、可回放</span></div>
      <div class="anchors">
        <div class="anchor-cell p"><div class="h">POSITIVE · 2</div><p>${esc(anchor.positive || '—')}</p></div>
        <div class="anchor-cell m"><div class="h">PARTIAL · 1</div><p>${esc(anchor.partial || '—')}</p></div>
        <div class="anchor-cell n"><div class="h">NEGATIVE · 0</div><p>${esc(anchor.negative || '—')}</p></div>
      </div>
      <div class="sep"></div>
      <div class="tiny muted">可观测性要求：行为锚必须包含可被判定的动词与可听/可见的对象；含糊表述在冻结阶段即被阻断。</div>
    </section>

    <section class="card">
      <div class="card-title">人工复核<span class="sub">三元操作 · 拒绝必须填理由码</span></div>
      <div class="action-zone" id="action-panel">
        ${actionPanelHtml(data)}
      </div>
      <div class="sep"></div>
      <div class="tiny muted">界面纪律：默认播放、不默认给分；只提供接受/修正/拒绝，修正从 rubric 档位中选择；人工记录直接进入一致性统计。</div>
    </section>
  `;
}

function aiPanelHtml(data, locked) {
  if (locked) {
    return `<div class="ai-locked"><div><b>AI 建议已锁定</b><br><span style="font-size:11px">播放证据片段后显示建议分与置信度<br><span class="mono" style="font-size:9.5px">默认播放，不默认给分</span></span></div></div>`;
  }
  if (data.ai_score === null || data.ai_score === undefined) {
    return `
      <div class="card-title">AI 参与情况<span class="sub">人工保留项</span></div>
      <div class="ai-suggestion">
        <div class="score-row"><span class="big-score" style="color:var(--text-mute)">—</span><span class="score-label">AI 未给出分数</span></div>
        <div class="rationale">${esc(data.ai_rationale || '该项 AI 无评分权限或仅提取证据，需人工判定。')}</div>
        <div class="tiny muted">${esc(data.authority_label)} · 来源 ${esc(data.evidence_type)}</div>
      </div>`;
  }
  const cls = 's' + data.ai_score;
  return `
    <div class="card-title">AI 建议<span class="sub">需人工确认</span></div>
    <div class="ai-suggestion">
      <div class="score-row">
        <span class="big-score ${cls}">${data.ai_score}</span>
        <span class="score-label">${scoreText(data.ai_score)}</span>
        <span class="conf">置信度<b>${pct(data.ai_confidence, 0)}</b></span>
      </div>
      <div class="rationale">${esc(data.ai_rationale || '')}</div>
      ${(data.evidence || []).map((ev) => `<div class="tiny muted">${mmss(ev.start_ms)} · ${esc(ev.quote)}</div>`).join('')}
      <div class="tiny muted">model ${esc(data.model_version || '—')} · prompt ${esc(data.prompt_version || '—')}</div>
    </div>`;
}

function actionPanelHtml(data) {
  if (!state.reviewPlayed && data.evidence.length > 0) {
    return '<div class="action-note">播放证据片段后出现评分操作。</div>';
  }
  if (data.ai_score === null || data.ai_score === undefined) {
    const selected = state.reviewScore;
    return `
      <div class="attempt-strip"><span>该项无 AI 分数，请直接给出人工档位：</span></div>
      <div class="score-choice">
        ${[2, 1, 0].map((s) => `<button data-action="score" data-score="${s}" class="${selected === s ? 'selected' : ''}">${s} · ${scoreText(s)}</button>`).join('')}
      </div>
      <div class="btn-row">
        <button class="btn primary" data-action="manual-save">保存人工评分</button>
        <span class="action-note">复核人：${esc(state.reviewer)}</span>
      </div>`;
  }
  if (state.reviewMode === 'correct' || state.reviewMode === 'reject') {
    const isReject = state.reviewMode === 'reject';
    return `
      <div class="attempt-strip"><span>${isReject ? '拒绝 AI 判定并给出你的档位' : '修正 AI 建议'}</span><span>AI：<b>${data.ai_score} · ${scoreText(data.ai_score)}</b></span></div>
      <div class="score-choice">
        ${[2, 1, 0].map((s) => `<button data-action="score" data-score="${s}" class="${state.reviewScore === s ? 'selected' : ''}">${s} · ${scoreText(s)}</button>`).join('')}
      </div>
      ${isReject ? `<div class="reason-row">
        ${['证据不足', '判定过严', '判定过宽', '行为锚歧义', '其他'].map((r) => `
          <label><input type="radio" name="reason" value="${r}" ${state.reviewReason === r ? 'checked' : ''} data-action="reason"><span>${r}</span></label>`).join('')}
      </div>` : ''}
      <div class="btn-row">
        <button class="btn ${isReject ? 'danger' : 'primary'}" data-action="submit-mode">${isReject ? '确认拒绝' : '确认修正'}</button>
        <button class="btn ghost" data-action="cancel-mode">取消</button>
      </div>`;
  }
  return `
    <div class="btn-row">
      <button class="btn primary" data-action="accept">接受 AI 建议</button>
      <button class="btn amber" data-action="mode" data-mode="correct">修正</button>
      <button class="btn danger" data-action="mode" data-mode="reject">拒绝</button>
    </div>
    <div class="action-note">接受后 final_score = AI 建议；修正/拒绝会写入 ReviewAction 与审计链。复核人：${esc(state.reviewer)}</div>`;
}

function refreshActionPanel() {
  if (!state.currentReview) return;
  const panel = $('#action-panel');
  if (panel) panel.innerHTML = actionPanelHtml(state.currentReview);
}

function setupReviewAudio(data) {
  const audio = $('#review-audio');
  if (!audio) return;
  const ev = data.evidence && data.evidence[0];
  if (!ev || !data.media_url) {
    markReviewPlayed();
    return;
  }
  const start = Math.max(0, Number(ev.start_ms) / 1000 - 3);
  const end = ev.evidence_kind === 'absence'
    ? Math.min(Number(ev.end_ms) / 1000, start + 10)
    : Number(ev.end_ms) / 1000 + 3;
  audio.dataset.clipStart = String(start);
  audio.dataset.clipEnd = String(end);
  audio.addEventListener('play', markReviewPlayed, { once: true });
  audio.addEventListener('timeupdate', () => {
    if (Number(audio.currentTime) >= end) audio.pause();
  });
  const seekAndPlay = () => {
    audio.currentTime = start;
    const status = $('#play-status');
    const promise = audio.play();
    if (promise && promise.catch) {
      promise.catch(() => {
        if (status) status.textContent = '浏览器阻止自动播放，请点击播放';
      });
    }
    if (status) status.textContent = '自动播放中 · 播完可评分';
  };
  if (audio.readyState >= 1) seekAndPlay();
  else audio.addEventListener('loadedmetadata', seekAndPlay, { once: true });
}

function markReviewPlayed() {
  if (state.reviewPlayed) return;
  state.reviewPlayed = true;
  if (!state.currentReview) return;
  const ai = $('#ai-panel');
  if (ai) ai.innerHTML = aiPanelHtml(state.currentReview, false);
  refreshActionPanel();
  const status = $('#play-status');
  if (status) status.textContent = '已播放 · 可以评分';
}

async function submitReview(action, score, reason) {
  if (!state.currentReview) return;
  const reviewer = ($('#reviewer-input').value || '').trim();
  if (!reviewer) {
    toast('请先填写复核人', true);
    return;
  }
  state.reviewer = reviewer;
  localStorage.setItem('osceloop.reviewer', reviewer);
  const seconds = Math.max(1, Math.round((Date.now() - state.reviewStartedAt) / 1000));
  try {
    const result = await api(`/api/review/${encodeURIComponent(state.currentReview.record_id)}/action`, {
      method: 'POST',
      body: { action, score, reason_code: reason || null, reviewer, seconds_spent: seconds },
    });
    if (result.needs_second_review) {
      toast('第一位复核已记录；需由另一位评分员独立复核同一项');
      await loadReview(state.currentReview.record_id);
      return;
    }
    if (result.needs_senior_review) {
      toast('双人评分不一致，已转高年资裁决', true);
      await loadReview(state.currentReview.record_id);
      return;
    }
    toast(`已记录：${action} → ${result.final_score} 档 · 场次待复核 ${result.attempt_pending}`);
    if (state.queue) {
      state.queue.items = state.queue.items.filter((x) => x.record_id !== state.currentReview.record_id);
      state.queue.total = Math.max(0, state.queue.total - 1);
      setPendingBadge(state.queue.total);
    }
    const next = result.next_record_id;
    const nextInList = filteredQueue().find((x) => x.record_id === next) || filteredQueue()[0];
    if (nextInList) {
      await loadReview(nextInList.record_id);
    } else {
      state.currentReview = null;
      const wb = $('#workbench');
      if (wb) wb.innerHTML = '<div class="empty">队列已清空。可到「质量与闸门」查看复核结果与审计链。</div>';
    }
    renderQueueList();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- ingest ---------- */
const SAMPLE_TRANSCRIPT = [
  { speaker: 'patient', start_ms: 0, end_ms: 6000, text: '大夫，我胸口疼得厉害……' },
  { speaker: 'candidate', start_ms: 7000, end_ms: 12000, text: '您好，我是急诊科张医生，请问您怎么称呼？' },
  { speaker: 'patient', start_ms: 12500, end_ms: 16000, text: '我叫王建国，今年六十五。' },
  { speaker: 'candidate', start_ms: 17000, end_ms: 23000, text: '您能说说胸口具体哪个位置疼吗？是压榨样还是刺痛？' },
  { speaker: 'patient', start_ms: 23500, end_ms: 29000, text: '胸口正中，压着疼，像有块石头。' },
  { speaker: 'candidate', start_ms: 30000, end_ms: 36000, text: '疼了多久了？活动以后会不会加重？' },
  { speaker: 'patient', start_ms: 36500, end_ms: 41000, text: '从早上八点开始，两个多小时了，爬楼后更厉害。' },
  { speaker: 'candidate', start_ms: 42000, end_ms: 49000, text: '这种疼会不会向肩膀、左臂或者下巴放射？' },
  { speaker: 'patient', start_ms: 49500, end_ms: 55000, text: '会窜到左边胳膊，下巴也发紧。' },
  { speaker: 'candidate', start_ms: 56000, end_ms: 62000, text: '有没有出汗、恶心或者呼吸困难？' },
  { speaker: 'patient', start_ms: 62500, end_ms: 68000, text: '出了不少汗，有点恶心。' },
  { speaker: 'candidate', start_ms: 69000, end_ms: 75000, text: '我先给您听诊一下，然后做心电图和心电监护。' },
  { speaker: 'patient', start_ms: 75500, end_ms: 79000, text: '行，听您的。' },
  { speaker: 'candidate', start_ms: 80000, end_ms: 88000, text: '持续胸痛伴出汗是危险信号，需要马上处理。我总结一下……' },
];

async function viewIngest() {
  const stationData = await api('/api/bundles/stations');
  const stations = stationData.stations || [];
  if (!stations.length) {
    $('#view').innerHTML = '<div class="empty">尚无冻结站包，无法录入</div>';
    return;
  }
  const linked = state.ingestSlot;
  const preferred = linked
    ? stations.find((s) => s.title.includes(linked.station_name) || linked.station_name.includes(s.title))
    : null;
  const stationOptions = stations
    .map((s) => `<option value="${dataAttr(s.bundle_id)}" ${preferred && preferred.bundle_id === s.bundle_id ? 'selected' : ''}>${esc(s.station_id)} · ${esc(s.title)} · v${esc(s.version)}</option>`)
    .join('');
  $('#view').innerHTML = `
    <div class="view-head">
      <div>
        <h2>录像录入</h2>
        <p>P0 离线入口：上传历史录像与带时间戳转写，系统解析说话人、阶段并跑完整评分与风险路由。</p>
      </div>
      <div class="btn-row">
        <button class="btn small" data-action="download-sample">下载 JSON 转写样例</button>
      </div>
    </div>
    <div class="grid c1-2">
      <div class="card">
        <div class="card-title">录入规则<span class="sub">不接触在考考生</span></div>
        <ul class="small dim" style="padding-left:18px;line-height:1.9">
          <li>转写支持 <code>JSON</code>、<code>SRT</code>、<code>VTT</code>，必须带时间戳。</li>
          <li>SRT/VTT 建议每句前加“医生：”或“患者：”，否则按患者/考生交替推断。</li>
          <li>媒体支持 wav / mp3 / m4a / mp4 / webm，最大 200MB；未上传媒体时该场次全部转人工。</li>
          <li>未配置 ASR 时需上传转写；配置后只上传音视频即可自动转写，ASR 必须返回说话人标签。</li>
          <li>评分提供方与超时/降级配置可在「质量与闸门」查看；Mock 为默认演示模式。</li>
        </ul>
        <div class="sep"></div>
        <div class="card-title" style="margin-bottom:5px">可选考站<span class="sub">${stations.length} 个</span></div>
        ${stations.map((s) => `
          <div class="version-row"><span class="k">${esc(s.station_id)}</span><span class="v">v${esc(s.version)}</span></div>
          <div class="small dim" style="margin-bottom:6px">${esc(s.title)}</div>`).join('')}
        <div class="sep"></div>
        <div class="tiny muted">JSON 样例字段：speaker / start_ms / end_ms / text；可选 phase 与 asr_confidence。</div>
      </div>
      <div class="card">
        <div class="card-title">上传与评分<span class="sub">录入后自动进入复核队列</span></div>
        <form id="ingest-form">
          ${linked ? `<div class="alert" style="margin-bottom:10px"><b>关联执行槽位</b><p>第 ${esc(linked.round)} 轮 · ${esc(linked.candidate_name)} · ${esc(linked.station_name)}；录入成功后自动回写执行台。</p></div>` : ''}
          <input type="hidden" name="run_id" value="${dataAttr(linked ? linked.run_id : '')}">
          <input type="hidden" name="slot_key" value="${dataAttr(linked ? linked.slot_key : '')}">
          <label class="field">考站
            <select class="input" name="bundle_id" required>${stationOptions}</select>
          </label>
          <div class="grid c2" style="margin-top:10px">
            <label class="field">考生编号
              <input class="input" name="candidate_code" value="${dataAttr(linked ? linked.candidate_code : 'C2026-UPLOAD')}" maxlength="40">
            </label>
            <label class="field">考生姓名
              <input class="input" name="candidate_name" value="${dataAttr(linked ? linked.candidate_name : '演示考生')}" maxlength="40">
            </label>
          </div>
          <div class="grid c2" style="margin-top:10px">
            <label class="field">用途风险级
              <select class="input" name="risk_level">
                <option value="L1">L1 形成性练习</option>
                <option value="L2" selected>L2 模拟考</option>
                <option value="L3">L3 高利害（需 G3 放行）</option>
              </select>
            </label>
            <label class="field">录入人
              <input class="input" name="actor" value="${dataAttr(state.reviewer)}" maxlength="20">
            </label>
          </div>
          <label class="field" style="margin-top:10px">转写文件（可选；JSON / SRT / VTT）
            <input class="input" type="file" name="transcript_file" accept=".json,.srt,.vtt">
          </label>
          <label class="field" style="margin-top:10px">音视频文件（未上传转写时用于 ASR）
            <input class="input" type="file" name="media_file" accept="audio/*,video/*">
          </label>
          <label class="field" style="margin-top:10px">考生笔记 / 操作日志（可选）
            <textarea class="input" name="candidate_notes" rows="3" placeholder="每行一条，可用 [02:14] 开头标注时间，例如：[02:14] 已嘱患者深呼吸后听诊"></textarea>
          </label>
          <div class="tiny muted">笔记作为考生侧证据参与评分；未标注时间的笔记按场次末尾排列，并在证据中提示需人工确认。</div>
          <div class="grid c2" style="margin-top:10px">
            <label class="field">ASR 语种
              <input class="input" name="asr_language" value="zh" maxlength="10">
            </label>
            <label class="field">ASR 首个说话人
              <select class="input" name="asr_first_speaker">
                <option value="patient" selected>患者（SP 先开口）</option>
                <option value="candidate">考生（医生先开口）</option>
              </select>
            </label>
          </div>
          <div class="btn-row" style="margin-top:14px">
            <button class="btn primary" type="submit" id="ingest-submit">解析/转写并运行评分</button>
            <span class="tiny muted">评分完成后写入审计链</span>
          </div>
        </form>
        <div id="ingest-result"></div>
      </div>
    </div>
  `;
  const form = $('#ingest-form');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const button = $('#ingest-submit');
    const fileInput = form.querySelector('input[name="transcript_file"]');
    const mediaInput = form.querySelector('input[name="media_file"]');
    if ((!fileInput.files || !fileInput.files.length) && (!mediaInput.files || !mediaInput.files.length)) {
      toast('请上传转写文件，或上传音视频以使用 ASR', true);
      return;
    }
    button.disabled = true;
    button.textContent = '解析、转写与评分中…';
    try {
      const data = await apiForm('/api/attempts/ingest', new FormData(form));
      renderIngestResult(data);
      if (data.linked_slot) {
        toast(`已录入 ${data.attempt.candidate_code} 并回写执行槽位，待复核 ${data.attempt.pending} 项`);
        state.ingestSlot = null;
      } else {
        toast(`已录入 ${data.attempt.candidate_code}，待复核 ${data.attempt.pending} 项`);
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
      button.textContent = '解析/转写并运行评分';
    }
  });
}

function renderIngestResult(data) {
  const a = data.attempt;
  const t = data.transcript;
  const warnings = (t.warnings || []).map((w) => `<div class="violation">${esc(w)}</div>`).join('');
  const box = $('#ingest-result');
  if (!box) return;
  box.innerHTML = `
    <div class="sep"></div>
    ${(a.anomalies || []).length ? `<div class="alert" style="margin-bottom:8px"><b>自动异常 ${a.anomalies.length} 项</b><p>${a.anomalies.map((item) => `${esc(item.label)}：${esc(item.detail)}`).join('；')}</p></div>` : ''}
    ${data.linked_slot ? `<div class="attempt-strip" style="margin-bottom:8px">
      <span class="pill teal">已回写执行槽位</span>
      <span>${esc(data.linked_slot.slot_key)}</span>
      <a class="btn small" href="#/execution">返回执行台</a>
    </div>` : ''}
    <div class="card-title">录入结果<span class="sub">${esc(a.status)}</span></div>
    <div class="attempt-strip" style="margin-bottom:8px">
      <span>${esc(a.candidate_code)} · ${esc(a.candidate_name)}</span>
      <span>风险 ${esc(a.risk_level)}</span>
      <span>转写 ${t.utterances} 句</span>
      ${t.notes ? `<span>笔记 ${t.notes} 条</span>` : ''}
      <span>${t.source === 'asr' ? `ASR · ${esc((t.asr || {}).model_version || '自动转写')} · ${num((t.asr || {}).latency_ms, 0)} ms` : '转写文件'}</span>
      <span>考生 ${t.candidate_utterances} / 患者 ${t.patient_utterances}</span>
      <span>${data.media ? '媒体已上传' : '无媒体'}</span>
    </div>
    <div class="grid c3" style="margin-bottom:10px">
      <div class="kpi"><div class="k">AI 暂定总分</div><div class="v">${num(a.ai_total, 1)}</div><div class="d">待复核 ${a.pending} 项</div></div>
      <div class="kpi"><div class="k">关键项待复核</div><div class="v">${a.critical_pending}</div><div class="d">风险路由已生效</div></div>
      <div class="kpi"><div class="k">证据媒体</div><div class="v" style="font-size:16px">${data.media ? '可回放' : '不可回放'}</div><div class="d">${data.media ? esc(data.media.filename) : 'integrity_gap'}</div></div>
    </div>
    ${data.integrity_gap ? '<div class="alert danger"><b>缺少媒体文件</b><p>该场次已被标记 integrity_gap，全部记录进入人工复核。</p></div>' : ''}
    ${warnings}
    <div class="btn-row">
      <button class="btn primary small" data-action="open-feedback" data-attempt="${dataAttr(a.attempt_id)}">查看反馈报告</button>
      <a class="btn small" href="#/review">进入复核队列</a>
    </div>
    <div class="tiny muted" style="margin-top:8px">bundle ${esc(a.bundle_id)} · ${esc(a.bundle_hash || '')}</div>
  `;
}

function downloadSampleTranscript() {
  const blob = new Blob([JSON.stringify(SAMPLE_TRANSCRIPT, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'osce_sample_transcript.json';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------- schedule ---------- */
async function viewSchedule() {
  const [inputs, latest, policyData] = await Promise.all([
    api('/api/schedule/inputs'),
    api('/api/schedule/latest'),
    api('/api/schedule/policy'),
  ]);
  let schedule = latest.schedule;
  let specialNeeds = latest.special_needs;
  let blackouts = inputs.blackouts || [];
  let policy = policyData.latest;
  const policyPresets = policyData.presets || [];
  const policyHtml = `<div class="card" style="margin-bottom:16px">
      <div class="card-title">软约束权重确认<span class="sub">公平 vs 效率是价值判断 · 生成排考前必须确认</span></div>
      <div class="btn-row" style="align-items:flex-end;gap:10px;flex-wrap:wrap">
        <label class="field">权重预设
          <select id="policy-preset" class="input">
            ${policyPresets.map((item) => `<option value="${dataAttr(item.preset)}" ${policy && policy.preset === item.preset ? 'selected' : ''}>${esc(item.label)} · 公平权重 ${item.fairness_weight}</option>`).join('')}
          </select>
        </label>
        <label class="field" style="flex:1;min-width:240px">确认理由
          <input id="policy-note" class="input" placeholder="例如：本次模拟考优先保证考生间难度公平" value="${dataAttr(policy ? policy.note : '')}">
        </label>
        <button class="btn" id="policy-confirm">确认权重</button>
      </div>
      <div id="policy-result" class="tiny muted" style="margin-top:8px">${policy ? `当前有效权重：<span class="pill teal">${esc(policy.label)}</span> 公平权重 ${policy.fairness_weight} · ${esc(policy.actor)} · ${fmtTime(policy.created_at)}` : '尚未确认软约束权重，生成排考按钮不可用。'}</div>
    </div>`;
  const scheduleCandidates = inputs.candidates
    .map((c) => `<option value="${dataAttr(c.candidate_id)}">${esc(c.name)} · ${esc(c.candidate_id)}</option>`)
    .join('');
  const scheduleStations = inputs.stations
    .map((s) => `<option value="${dataAttr(s.station_id)}">${esc(s.name)} · ${esc(s.station_id)}</option>`)
    .join('');
  const roundOptions = (count) => Array.from({ length: count }, (_, i) => `<option value="${i + 1}">第 ${i + 1} 轮</option>`).join('');
  const today = schedule ? schedule.exam_date : new Date().toISOString().slice(0, 10);
  $('#view').innerHTML = `
    <div class="view-head">
      <div>
        <h2>考务编排</h2>
        <p>S4：在房间、SP、考官与时长约束下生成轮转排考，检查每位考生的加权难度、顺序效应与疲劳风险。</p>
      </div>
      <div class="attempt-strip">
        <span>考生 <b>${inputs.candidates.length}</b></span>
        <span>考站 <b>${inputs.stations.length}</b></span>
        <span>资源 <b>${inputs.resources.rooms} 房 / ${inputs.resources.sps} SP / ${inputs.resources.examiners} 考官</b></span>
      </div>
    </div>
    ${policyHtml}
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">排考参数<span class="sub">硬约束优先，软约束用于均衡</span></div>
      <form id="schedule-form" class="btn-row" style="align-items:flex-end;gap:10px;flex-wrap:wrap">
        <label class="field">考试日期<input class="input" type="date" name="exam_date" value="${dataAttr(today)}"></label>
        <label class="field">轮次数<input class="input" type="number" name="rounds" min="1" max="${inputs.stations.length}" value="${schedule ? schedule.rounds : 6}"></label>
        <label class="field">轮间缓冲（分钟）<input class="input" type="number" name="buffer_min" min="0" max="30" value="${schedule ? schedule.buffer_min : 2}"></label>
        <label class="field">随机种子<input class="input" type="number" name="seed" value="${schedule ? schedule.seed : 2026}"></label>
        <button class="btn primary" type="submit" id="schedule-submit" ${policy ? '' : 'disabled'}>生成排考表</button>
      </form>
      <div class="tiny muted" style="margin-top:8px">每位考生从考站集合中抽取 ${schedule ? schedule.rounds : 6} 站；同一轮同一考站只安排一名考生。</div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">考站不可用时段<span class="sub">考官请假 / 场地占用 · 生成排考自动避开</span></div>
      <form id="blackout-form" class="btn-row" style="align-items:flex-end;gap:10px;flex-wrap:wrap">
        <label class="field">考站
          <select id="blackout-station" class="input">${scheduleStations}</select>
        </label>
        <label class="field">开始轮次
          <select id="blackout-from" class="input">${roundOptions(10)}</select>
        </label>
        <label class="field">结束轮次
          <select id="blackout-to" class="input">${roundOptions(10)}</select>
        </label>
        <label class="field" style="flex:1;min-width:200px">原因
          <input id="blackout-reason" class="input" maxlength="200" placeholder="例如：E03 考官请假 / 场地临时占用">
        </label>
        <button class="btn" type="submit">添加不可用</button>
      </form>
      <div id="blackout-list" style="margin-top:10px">${blackoutListHtml(blackouts)}</div>
      <div class="tiny muted" style="margin-top:6px">不可用时段只影响新生成的排考表；修复改派也会避开这些轮次，已生成的排考请重新生成。</div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">冲突最小改动修复<span class="sub">只改受影响的人 · 原排考表保留</span></div>
      <div class="btn-row" style="align-items:flex-end;gap:10px;flex-wrap:wrap">
        <label class="field">冲突类型
          <select id="repair-type" class="input">
            <option value="candidate_round">考生本轮不可用</option>
            <option value="station_round">考站本轮故障</option>
          </select>
        </label>
        <label class="field" id="repair-candidate-wrap">考生
          <select id="repair-candidate" class="input">${scheduleCandidates}</select>
        </label>
        <label class="field" id="repair-station-wrap" style="display:none">考站
          <select id="repair-station" class="input">${scheduleStations}</select>
        </label>
        <label class="field">轮次
          <select id="repair-round" class="input">${roundOptions(schedule ? schedule.rounds : 6)}</select>
        </label>
        <button class="btn" id="repair-submit" ${schedule ? '' : 'disabled'}>最小改动修复</button>
      </div>
      <div id="repair-result" class="tiny muted" style="margin-top:8px">选择冲突后执行修复；优先改派到本轮空闲考站，无空闲时两人对调。修复结果作为新排考表保存，原表不覆盖。</div>
    </div>
    <div id="schedule-body">${schedule ? scheduleHtml(schedule) : '<div class="empty">尚未生成排考表。点击“生成排考表”开始。</div>'}</div>
    <div id="special-needs-card" style="margin-top:16px">${schedule && specialNeeds ? specialNeedsHtml(schedule, specialNeeds) : ''}</div>
  `;
  const specialCard = $('#special-needs-card');
  const policyConfirm = $('#policy-confirm');
  if (policyConfirm) {
    policyConfirm.addEventListener('click', async () => {
      const preset = $('#policy-preset').value;
      const note = ($('#policy-note').value || '').trim();
      if (note.length < 4) {
        toast('请填写不少于 4 字的确认理由', true);
        return;
      }
      policyConfirm.disabled = true;
      try {
        const data = await api('/api/schedule/policy', {
          method: 'POST',
          body: { preset, note },
        });
        policy = data.policy;
        const box = $('#policy-result');
        if (box) {
          box.className = 'tiny';
          box.innerHTML = `当前有效权重：<span class="pill teal">${esc(policy.label)}</span> 公平权重 ${policy.fairness_weight} · ${esc(policy.actor)} · ${fmtTime(policy.created_at)}`;
        }
        const submit = $('#schedule-submit');
        if (submit) submit.disabled = false;
        toast(`已确认软约束权重：${policy.label}`);
      } catch (err) {
        toast(err.message, true);
      } finally {
        policyConfirm.disabled = false;
      }
    });
  }
  $('#schedule-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const button = $('#schedule-submit');
    const body = {
      exam_date: form.exam_date.value || today,
      rounds: Number(form.rounds.value),
      buffer_min: Number(form.buffer_min.value),
      seed: Number(form.seed.value),
      policy_id: policy ? policy.policy_id : null,
    };
    button.disabled = true;
    button.textContent = '求解中…';
    try {
      const data = await api('/api/schedule/generate', { method: 'POST', body });
      schedule = data.schedule;
      specialNeeds = data.special_needs;
      $('#schedule-body').innerHTML = scheduleHtml(data.schedule);
      if (specialCard) specialCard.innerHTML = specialNeedsHtml(schedule, specialNeeds);
      const repairRound = $('#repair-round');
      if (repairRound) repairRound.innerHTML = roundOptions(schedule.rounds);
      const repairButton = $('#repair-submit');
      if (repairButton) repairButton.disabled = false;
      const repairResult = $('#repair-result');
      if (repairResult) {
        repairResult.className = 'tiny muted';
        repairResult.textContent = '排考表已更新，可继续登记现场冲突。';
      }
      toast(`排考完成：难度差 ${data.schedule.quality.difficulty_spread} · ${data.schedule.quality.warnings.length} 条预警`);
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
      button.textContent = '生成排考表';
    }
  });
  const repairType = $('#repair-type');
  if (repairType) {
    repairType.addEventListener('change', () => {
      const candidateMode = repairType.value === 'candidate_round';
      const candidateWrap = $('#repair-candidate-wrap');
      const stationWrap = $('#repair-station-wrap');
      if (candidateWrap) candidateWrap.style.display = candidateMode ? '' : 'none';
      if (stationWrap) stationWrap.style.display = candidateMode ? 'none' : '';
    });
  }
  const repairSubmit = $('#repair-submit');
  if (repairSubmit) {
    repairSubmit.addEventListener('click', async () => {
      if (!schedule) {
        toast('请先生成排考表', true);
        return;
      }
      const type = $('#repair-type').value;
      const conflict = { type, round: Number($('#repair-round').value) };
      if (type === 'candidate_round') {
        conflict.candidate_id = $('#repair-candidate').value;
      } else {
        conflict.station_id = $('#repair-station').value;
      }
      repairSubmit.disabled = true;
      try {
        const data = await api('/api/schedule/repair', {
          method: 'POST',
          body: { schedule_id: schedule.schedule_id, conflicts: [conflict] },
        });
        schedule = data.schedule;
        specialNeeds = data.special_needs;
        $('#schedule-body').innerHTML = scheduleHtml(data.schedule);
        if (specialCard) specialCard.innerHTML = specialNeedsHtml(schedule, specialNeeds);
        const report = data.report;
        const changeText = report.changes
          .map((c) => `${c.candidate_id} 第 ${c.round} 轮 ${c.from_station_id} → ${c.to_station_id}${c.swapped_with ? `（与 ${c.swapped_with} 对调）` : '（改派空闲考站）'}`)
          .join('；');
        const box = $('#repair-result');
        if (box) {
          box.className = 'tiny';
          box.innerHTML = `<span class="pill teal">已修复</span> ${esc(changeText)}
            <div class="tiny muted">难度差 ${num(report.before.difficulty_spread, 2)} → ${num(report.after.difficulty_spread, 2)} · 总时长 ${num(report.before.total_minutes, 0)} → ${num(report.after.total_minutes, 0)} 分钟</div>
            ${report.unresolved.length ? `<div style="color:var(--amber)">未解决 ${report.unresolved.length} 条：${esc(report.unresolved.map((u) => u.reason).join('；'))}</div>` : ''}`;
        }
        toast('已生成修复版排考表');
      } catch (err) {
        const box = $('#repair-result');
        if (box) {
          box.className = 'tiny';
          box.innerHTML = `<span class="pill red">无法修复</span> ${esc(err.message)}`;
        }
        toast(err.message, true);
      } finally {
        repairSubmit.disabled = false;
      }
    });
  }
  if (specialCard) {
    specialCard.addEventListener('click', async (e) => {
      const button = e.target.closest('[data-special-action]');
      if (!button || !schedule) return;
      const decision = button.dataset.specialAction === 'approve' ? 'approved' : 'rejected';
      const row = button.closest('[data-special-needs-row]');
      const note = (row?.querySelector('[data-special-needs-note]')?.value || '').trim();
      if (decision === 'rejected' && !note) {
        toast('驳回特殊需求必须填写理由', true);
        return;
      }
      button.disabled = true;
      try {
        const data = await api('/api/schedule/special-needs', {
          method: 'POST',
          body: {
            schedule_id: schedule.schedule_id,
            candidate_id: button.dataset.candidate,
            decision,
            note,
          },
        });
        specialNeeds = data.special_needs;
        specialCard.innerHTML = specialNeedsHtml(schedule, specialNeeds);
        toast(decision === 'approved' ? '已批准特殊需求' : '已驳回特殊需求');
      } catch (err) {
        toast(err.message, true);
      } finally {
        button.disabled = false;
      }
    });
  }
  const blackoutForm = $('#blackout-form');
  const blackoutList = $('#blackout-list');
  if (blackoutForm) {
    blackoutForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const stationId = $('#blackout-station').value;
      const fromRound = Number($('#blackout-from').value);
      const toRound = Number($('#blackout-to').value);
      const reason = ($('#blackout-reason').value || '').trim();
      if (reason.length < 4) {
        toast('不可用原因不少于 4 字', true);
        return;
      }
      if (toRound < fromRound) {
        toast('结束轮次不能早于开始轮次', true);
        return;
      }
      const submit = blackoutForm.querySelector('button[type="submit"]');
      if (submit) submit.disabled = true;
      try {
        const data = await api('/api/schedule/blackouts', {
          method: 'POST',
          body: { station_id: stationId, from_round: fromRound, to_round: toRound, reason },
        });
        blackouts = data.blackouts || [];
        if (blackoutList) blackoutList.innerHTML = blackoutListHtml(blackouts);
        $('#blackout-reason').value = '';
        toast('已登记不可用时段，重新生成排考后生效');
      } catch (err) {
        toast(err.message, true);
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  }
  if (blackoutList) {
    blackoutList.addEventListener('click', async (e) => {
      const button = e.target.closest('[data-blackout-delete]');
      if (!button) return;
      button.disabled = true;
      try {
        const data = await api(`/api/schedule/blackouts/${encodeURIComponent(button.dataset.blackoutDelete)}`, {
          method: 'DELETE',
        });
        blackouts = data.blackouts || [];
        blackoutList.innerHTML = blackoutListHtml(blackouts);
        toast('已删除不可用时段');
      } catch (err) {
        toast(err.message, true);
        button.disabled = false;
      }
    });
  }
}

function blackoutListHtml(rows) {
  if (!rows || !rows.length) return '<div class="tiny muted">尚无不可用时段记录。</div>';
  return rows.map((row) => {
    const span = Number(row.from_round) === Number(row.to_round)
      ? `第 ${row.from_round} 轮`
      : `第 ${row.from_round}–${row.to_round} 轮`;
    return `<div class="version-row">
      <span class="k">${esc(row.station_id)} · ${span}</span>
      <span class="v">${esc(row.reason)} · ${esc(row.actor)} <button class="link-btn" data-blackout-delete="${esc(row.blackout_id)}">删除</button></span>
    </div>`;
  }).join('');
}

function specialNeedsHtml(schedule, data) {
  if (!schedule || !data || !(data.items || []).length) return '';
  const rows = data.items.map((item) => {
    const needs = [
      item.extra_time_min ? `<span class="pill amber">延长 ${item.extra_time_min}′</span>` : '',
      item.quiet_room ? '<span class="pill blue">安静候考</span>' : '',
      item.accessible_required ? '<span class="pill teal">无障碍考站</span>' : '',
      item.separate_channel ? '<span class="pill purple">单独通道</span>' : '',
    ].join(' ');
    const status = item.status === 'approved'
      ? '<span class="pill teal">已批准</span>'
      : item.status === 'rejected'
        ? '<span class="pill red">已驳回</span>'
        : '<span class="pill muted">待审批</span>';
    const detail = item.status === 'pending'
      ? `<input class="input tiny" data-special-needs-note placeholder="审批备注（驳回必填）" style="max-width:200px">
         <button class="link-btn" data-special-action="approve" data-candidate="${esc(item.candidate_id)}">批准</button>
         <span class="muted"> · </span>
         <button class="link-btn" data-special-action="reject" data-candidate="${esc(item.candidate_id)}">驳回</button>`
      : `<span class="tiny muted">${esc((item.approval && item.approval.note) || '—')} · ${esc((item.approval && item.approval.actor) || '—')} ${fmtTime(item.approval && item.approval.created_at)}</span>`;
    return `<div class="version-row" data-special-needs-row="${esc(item.candidate_id)}">
      <span class="k">${esc(item.candidate_id)} · ${esc(item.name)}</span>
      <span class="v">${needs} ${status} ${detail}</span>
    </div>`;
  }).join('');
  return `<div class="card">
    <div class="card-title">特殊需求审批<span class="sub">${data.approved} 已批准 · ${data.rejected} 已驳回 · ${data.pending.length} 待审批</span></div>
    ${rows}
    <div class="tiny muted" style="margin-top:6px">延长时长、安静候考区、无障碍考站与单独通道会影响公平性，必须由考务逐人确认；审批记录写入审计链。</div>
  </div>`;
}

function scheduleHtml(schedule) {
  const rounds = schedule.rounds_detail || [];
  const candidates = schedule.candidates || [];
  const quality = schedule.quality || {};
  const resources = schedule.resources || {};
  const equipment = (resources.equipment && resources.equipment.available) || {};
  const policy = schedule.policy || {};
  const warningHtml = (quality.warnings || []).map((w) => `<div class="alert ${quality.status === 'review' ? '' : 'danger'}" style="margin-bottom:6px"><b>预警</b><p>${esc(w)}</p></div>`).join('');
  const repair = schedule.repair_report;
  const repairHtml = repair ? `
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">最近一次最小改动修复<span class="sub">${repair.requested.length} 条冲突 · ${repair.changes.length} 处改动${schedule.repaired_from ? ` · 原表 ${esc(schedule.repaired_from)}` : ''}</span></div>
      ${repair.changes.map((c) => `<div class="version-row"><span class="k">${esc(c.candidate_id)} · 第 ${c.round} 轮</span><span class="v">${esc(c.from_station_id)} → ${esc(c.to_station_id)}${c.swapped_with ? `（与 ${esc(c.swapped_with)} 对调）` : '（改派空闲考站）'}</span></div>`).join('')}
      ${repair.unresolved.length ? `<div class="alert" style="margin-top:8px"><b>未解决冲突</b><p>${repair.unresolved.map((u) => esc(u.reason)).join('；')}</p></div>` : ''}
    </div>` : '';
  const usage = {};
  const slotLookup = {};
  rounds.forEach((round) => round.slots.forEach((slot) => {
    usage[slot.station_name] = (usage[slot.station_name] || 0) + 1;
    slotLookup[`${slot.candidate_id}:${round.round}`] = slot;
  }));
  return `
    <div class="grid c4" style="margin-bottom:16px">
      <div class="kpi"><div class="k">总时长</div><div class="v">${num(quality.total_minutes, 0)}<small>分钟</small></div><div class="d">${rounds.length} 轮 · 缓冲 ${schedule.buffer_min} 分钟</div></div>
      <div class="kpi ${quality.difficulty_spread <= 1 ? 'teal' : 'amber'}"><div class="k">加权难度差</div><div class="v">${num(quality.difficulty_spread, 2)}</div><div class="d">目标 ${num(quality.target_difficulty, 2)} · 阈值 ≤1.0</div></div>
      <div class="kpi ${quality.late_difficulty_spread <= 1 ? 'teal' : 'amber'}"><div class="k">后两轮难度差</div><div class="v">${num(quality.late_difficulty_spread, 2)}</div><div class="d">顺序效应检查</div></div>
      <div class="kpi ${quality.warnings.length ? 'amber' : 'teal'}"><div class="k">预警</div><div class="v">${quality.warnings.length}</div><div class="d">${esc(schedule.exam_date)} · ${quality.status === 'ok' ? '可直接使用' : '需人工确认'}</div></div>
    </div>
    ${warningHtml}
    ${repairHtml}
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">考生排考表<span class="sub">${candidates.length} 名考生 · ${rounds.length} 轮</span></div>
      <div class="tbl-wrap">
        <table class="schedule-table">
          <thead><tr><th>考生</th><th>特殊需求</th>${rounds.map((r) => `<th>第 ${r.round} 轮<div class="tiny muted">${r.start_min}–${r.end_min}′</div></th>`).join('')}<th class="num">难度合计</th></tr></thead>
          <tbody>
            ${candidates.map((c) => `
              <tr>
                <td><div class="mono tiny muted">${esc(c.candidate_id)}</div><div>${esc(c.name)}</div></td>
                <td>${c.extra_time_min ? `<span class="pill amber">延长 ${c.extra_time_min}′</span>` : ''} ${c.quiet_room ? '<span class="pill blue">安静候考</span>' : ''} ${c.accessible_required ? '<span class="pill teal">无障碍</span>' : ''} ${c.separate_channel ? '<span class="pill purple">单独通道</span>' : ''} ${!c.extra_time_min && !c.quiet_room && !c.accessible_required && !c.separate_channel ? '<span class="muted tiny">—</span>' : ''}</td>
                ${rounds.map((r) => {
                  const slot = (c.stations || []).find((s) => s.round === r.round);
                  const detail = slotLookup[`${c.candidate_id}:${r.round}`];
                  return `<td>${slot ? `<div style="font-weight:600">${esc(slot.station_name)}</div><div class="tiny muted">${detail ? `${detail.start_min}–${detail.end_min}′` : `${slot.duration_min}′`} · ${num(slot.difficulty, 2)}</div>` : '<span class="muted">—</span>'}</td>`;
                }).join('')}
                <td class="num">${num(c.total_difficulty, 2)}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>
    <div class="grid c2">
      <div class="card">
        <div class="card-title">资源占用<span class="sub">房间 / SP / 考官</span></div>
        <div class="version-row"><span class="k">房间</span><span class="v">${resources.rooms.used}/${resources.rooms.count} 使用 · 峰值 ${num(resources.rooms.max_minutes, 0)} 分钟</span></div>
        <div class="version-row"><span class="k">SP</span><span class="v">${resources.sps.used}/${resources.sps.count} 使用 · 峰值 ${num(resources.sps.max_minutes, 0)} 分钟</span></div>
        <div class="version-row"><span class="k">考官</span><span class="v">${resources.examiners.used}/${resources.examiners.count} 使用 · 峰值 ${num(resources.examiners.max_minutes, 0)} 分钟</span></div>
        <div class="version-row"><span class="k">共享设备</span><span class="v">${Object.entries(equipment).map(([name, count]) => `${esc(name)} ${count} 台`).join(' · ') || '未登记台账'}</span></div>
        <div class="sep"></div>
        <div class="card-title" style="margin-bottom:6px">考站使用次数</div>
        <div class="strength-list">${Object.entries(usage).map(([name, count]) => `<span class="pill">${esc(name)} · ${count}</span>`).join('')}</div>
      </div>
      <div class="card">
        <div class="card-title">排考纪律<span class="sub">软约束与人工保留</span></div>
        <div class="version-row"><span class="k">权重策略</span><span class="v">${esc(policy.label || '未记录')} · 公平权重 ${policy.fairness_weight ?? '—'}${policy.confirmed_by ? ` · ${esc(policy.confirmed_by)}` : ''}</span></div>
        <ul class="small dim" style="padding-left:18px;line-height:1.9">
          <li>硬约束：同一考生不重复考站、同一轮同一考站仅一名考生。</li>
          <li>软约束：按已确认的公平/效率权重平衡加权难度与考站使用。</li>
          <li>顺序效应：检查后两轮难度差，避免高难站集中在末尾。</li>
          <li>特殊需求：延长、安静候考区、无障碍考站与单独通道由考务逐人确认最终安排。</li>
          <li>疲劳：同一考官连续评分超过 ${quality.fatigue_limit_min || 240} 分钟会触发预警。</li>
          <li>设备：按共享设备台账逐轮校验，同一轮需求超过可用数量会触发冲突预警。</li>
        </ul>
        <div class="sep"></div>
        <div class="tiny muted">当前算法为确定性回溯 + 难度均衡启发式；真实部署可替换为 CP-SAT 或整数规划求解器。</div>
      </div>
    </div>`;
}

/* ---------- execution ---------- */
const EXEC_STATUS = {
  waiting: ['等待', 'muted'],
  in_progress: ['进行中', 'blue'],
  completed: ['已完成', 'teal'],
  anomaly: ['异常', 'amber'],
  terminated: ['已终止', 'red'],
  stopped: ['已停止', 'red'],
  running: ['进行中', 'teal'],
  paused: ['暂停', 'amber'],
};

function execPill(status) {
  const item = EXEC_STATUS[status] || [status, 'muted'];
  return `<span class="pill ${item[1]}">${esc(item[0])}</span>`;
}

async function viewExecution() {
  const data = await api('/api/execution/latest');
  const run = data.run;
  if (!run) {
    $('#view').innerHTML = `
      <div class="view-head">
        <div><h2>考试执行</h2><p>S5：从排考表启动执行，记录异常与终止，事实层提示可一键关闭并留痕。</p></div>
      </div>
      <div class="grid c2">
        <div class="card">
          <div class="card-title">启动考试执行<span class="sub">使用最新排考表</span></div>
          <p class="small dim">启动后系统会按轮次初始化全部考站槽位。当前演示不接入真实音视频流，执行状态与事件会写入审计链。</p>
          <div class="btn-row" style="margin-top:12px">
            <button class="btn primary" data-action="exec-start" ${data.schedule_available && !data.emergency_stop ? '' : 'disabled'}>开始考试执行</button>
            <a class="btn" href="#/schedule">去考务编排</a>
          </div>
          ${data.emergency_stop ? '<div class="alert danger" style="margin-top:10px"><b>终止开关已激活</b><p>需先解除全局终止才能启动考试。</p></div>' : ''}
          ${!data.schedule_available ? '<div class="alert" style="margin-top:10px"><b>尚无排考表</b><p>请先到考务编排生成排考表。</p></div>' : ''}
        </div>
        <div class="card">
          <div class="card-title">执行纪律<span class="sub">现场事实层</span></div>
          <ul class="small dim" style="padding-left:18px;line-height:1.9">
            <li>实时提示只呈现事实，禁止呈现“合格/不合格”等判断。</li>
            <li>提示可一键关闭，关闭操作写入事件日志与审计。</li>
            <li>异常终止权始终由现场考务/考官保留。</li>
            <li>媒体完整性缺口会标记为 critical 事件。</li>
          </ul>
        </div>
      </div>`;
    return;
  }
  $('#view').innerHTML = executionHtml(run, data.emergency_stop);
  state.executionRunId = run.run_id;
}

function executionHtml(run, emergencyStop) {
  const counts = run.counts || {};
  const current = run.slots.filter((s) => s.round === run.current_round);
  const completed = ['completed', 'stopped'].includes(run.status);
  const roundNames = Array.from({ length: run.max_round }, (_, i) => i + 1);
  const compLabels = { extend: '延时补时', retake: '另行安排重考', exclude: '不计入本次成绩' };
  const eventRows = (run.events || []).slice(0, 30).map((ev) => `
    <div class="audit-row">
      <div class="act" style="color:${ev.severity === 'critical' ? 'var(--red)' : ev.severity === 'warn' ? 'var(--amber)' : 'var(--teal)'}">${esc(ev.event_type)}</div>
      <div class="obj">${esc(ev.message)}</div>
      <div class="time">${fmtTime(ev.created_at)}</div>
    </div>`).join('');
  const slotCards = current.map((slot) => `
    <div class="card tight" style="margin-bottom:8px">
      <div class="attempt-strip" style="margin-bottom:4px">
        <span><b>${esc(slot.candidate_name)}</b> · ${esc(slot.station_name)}</span>
        ${execPill(slot.status)}
      </div>
      <div class="tiny muted">${esc(slot.room)} · ${esc(slot.sp)} · ${esc(slot.examiner)} · ${slot.start_min}–${slot.end_min}′</div>
      ${slot.hints_enabled !== false ? `<ul class="tiny muted" style="margin:6px 0 0;padding-left:16px">${(slot.hints || []).map((h) => `<li>${esc(h)}</li>`).join('')}</ul>` : `<div class="tiny muted" style="margin-top:5px">${run.hints_enabled ? '本站事实层提示已单独关闭' : '事实层提示已关闭'}</div>`}
      ${slot.hints_enabled !== false && slot.evidence_facts ? `<div class="tiny muted" style="margin-top:4px">事实：已采集候选发言 ${slot.evidence_facts.utterances} 段 · 尚无证据 ${slot.evidence_facts.missing} 项${slot.evidence_facts.missing_critical ? `（其中关键项 ${slot.evidence_facts.missing_critical} 项）` : ''}</div>` : ''}
      ${(slot.live_anomalies || []).length ? `<div class="tiny" style="margin-top:4px;color:${slot.live_anomalies.some((a) => a.severity === 'high') ? 'var(--red)' : 'var(--amber)'}">实时异常：${slot.live_anomalies.map((a) => esc(a.label)).join(' · ')}</div>` : ''}
      ${(slot.live_anomalies || []).length ? `<div class="btn-row" style="margin-top:6px"><button class="btn small amber" data-action="exec-live-ack" data-slot-key="${dataAttr(slot.slot_key)}">处置实时异常</button></div>` : ''}
      ${(slot.hints || []).length ? `<button class="link-btn tiny" data-action="exec-slot-hints" data-station-id="${dataAttr(slot.station_id)}" data-enabled="${slot.hints_enabled !== false ? 'false' : 'true'}">${slot.hints_enabled !== false ? '关闭本站提示' : '开启本站提示'}</button>` : ''}
      ${slot.note ? `<div class="violation">${esc(slot.note)}</div>` : ''}
      ${slot.compensation ? `<div class="tiny"><span class="pill amber">补偿</span> ${esc(compLabels[slot.compensation] || slot.compensation)}</div>` : ''}
      ${['anomaly', 'terminated'].includes(slot.status) ? `<div class="btn-row" style="margin-top:6px">
        <button class="btn small" data-action="exec-slot-compensate" data-slot-key="${dataAttr(slot.slot_key)}">补偿判定</button>
      </div>` : ''}
      ${slot.attempt ? `<div class="attempt-strip" style="margin-top:6px">
        <span class="pill teal">已录入场次</span>
        <span>待复核 ${slot.attempt.pending}</span>
        <button class="btn small" data-action="open-feedback" data-attempt="${dataAttr(slot.attempt.attempt_id)}">查看反馈</button>
      </div>` : ''}
      ${['waiting', 'in_progress'].includes(slot.status) && !completed && !slot.attempt ? `
        <div class="btn-row" style="margin-top:7px">
          <button class="btn small primary" data-action="exec-ingest" data-run="${dataAttr(run.run_id)}" data-slot-key="${dataAttr(slot.slot_key)}" data-code="${dataAttr(slot.candidate_id)}" data-name="${dataAttr(slot.candidate_name)}" data-station="${dataAttr(slot.station_name)}" data-round="${slot.round}">录入场次</button>
          <button class="btn small" data-action="exec-live-segment" data-slot-key="${dataAttr(slot.slot_key)}">回传转写</button>
          <button class="btn small" data-action="exec-slot" data-slot-key="${dataAttr(slot.slot_key)}" data-slot-action="complete">完成</button>
          <button class="btn small amber" data-action="exec-slot" data-slot-key="${dataAttr(slot.slot_key)}" data-slot-action="anomaly">异常</button>
          <button class="btn small danger" data-action="exec-slot" data-slot-key="${dataAttr(slot.slot_key)}" data-slot-action="terminate">终止该站</button>
        </div>` : !slot.attempt ? `
        <div class="btn-row" style="margin-top:7px">
          <button class="btn small" data-action="exec-slot" data-slot-key="${dataAttr(slot.slot_key)}" data-slot-action="complete">完成</button>
          <button class="btn small amber" data-action="exec-slot" data-slot-key="${dataAttr(slot.slot_key)}" data-slot-action="anomaly">异常</button>
          <button class="btn small danger" data-action="exec-slot" data-slot-key="${dataAttr(slot.slot_key)}" data-slot-action="terminate">终止该站</button>
        </div>` : ''}
    </div>`).join('');
  const matrixRows = Object.values(
    run.slots.reduce((acc, slot) => {
      acc[slot.candidate_id] = acc[slot.candidate_id] || { id: slot.candidate_id, name: slot.candidate_name, byRound: {} };
      acc[slot.candidate_id].byRound[slot.round] = slot;
      return acc;
    }, {})
  ).map((candidate) => `
    <tr>
      <td><div class="mono tiny muted">${esc(candidate.id)}</div><div>${esc(candidate.name)}</div></td>
      ${roundNames.map((r) => {
        const slot = candidate.byRound[r];
        return `<td>${slot ? `${execPill(slot.status)}<div class="tiny muted">${esc(slot.station_name)}</div>` : '<span class="muted">—</span>'}</td>`;
      }).join('')}
    </tr>`).join('');

  return `
    <div class="view-head">
      <div>
        <h2>考试执行</h2>
        <p>${esc(run.exam_date || '')} · 第 ${run.current_round} / ${run.max_round} 轮 · 事实层提示${run.hints_enabled ? '开启' : '关闭'}</p>
      </div>
      <div class="btn-row">
        ${execPill(run.status)}
        <button class="btn small" data-action="exec-export">导出归档包</button>
        ${!completed ? `<button class="btn small" data-action="exec-hints" data-enabled="${run.hints_enabled ? 'false' : 'true'}">${run.hints_enabled ? '关闭事实层提示' : '开启事实层提示'}</button>` : ''}
        ${!completed ? `<button class="btn small primary" data-action="exec-advance">${run.current_round >= run.max_round ? '结束考试' : '推进到下一轮'}</button>` : ''}
        ${!completed ? '<button class="btn small danger" data-action="exec-stop">终止考试</button>' : ''}
        ${emergencyStop ? '<button class="btn small amber" data-action="exec-global-resume">解除全局终止</button>' : '<button class="btn small danger" data-action="exec-global-stop">全局终止</button>'}
      </div>
    </div>
    ${emergencyStop ? '<div class="alert danger"><b>全局终止开关已激活</b><p>评分、SP 对话与成绩发布等写操作已被阻断；请确认现场安全后解除。</p></div>' : ''}
    ${run.integrity && run.integrity.missing > 0 ? `<div class="alert ${completed ? 'danger' : ''}">
      <b>场次录入覆盖 ${pct(run.integrity.coverage, 0)}</b>
      <p>${run.integrity.missing} 个槽位尚未关联场次${completed ? '，归档前需补齐或标记异常。' : '；可在槽位卡片中直接录入。'}</p>
    </div>` : ''}
    <div class="grid c4" style="margin-bottom:16px">
      <div class="kpi blue"><div class="k">当前轮次</div><div class="v">${run.current_round}<small>/ ${run.max_round}</small></div><div class="d">${current.length} 个并行考站</div></div>
      <div class="kpi teal"><div class="k">已完成</div><div class="v">${counts.completed || 0}</div><div class="d">已录入 ${counts.ingested || 0} · 总槽位 ${counts.total}</div></div>
      <div class="kpi amber"><div class="k">异常</div><div class="v">${counts.anomaly || 0}</div><div class="d">需现场处置</div></div>
      <div class="kpi red"><div class="k">已终止</div><div class="v">${counts.terminated || 0}</div><div class="d">保留终止理由与事件</div></div>
    </div>
    <div class="grid c1-2" style="margin-bottom:16px">
      <div class="card">
        <div class="card-title">当前轮次槽位<span class="sub">仅事实层提示</span></div>
        <div style="max-height:520px;overflow-y:auto">${slotCards || '<div class="empty">当前轮次无槽位</div>'}</div>
      </div>
      <div class="card">
        <div class="card-title">全流程矩阵<span class="sub">${Object.keys(run.slots.reduce((a, s) => { a[s.candidate_id] = 1; return a; }, {})).length} 名考生 · ${run.max_round} 轮</span></div>
        <div class="tbl-wrap">
          <table class="schedule-table">
            <thead><tr><th>考生</th>${roundNames.map((r) => `<th>第 ${r} 轮</th>`).join('')}</tr></thead>
            <tbody>${matrixRows}</tbody>
          </table>
        </div>
        <div class="sep"></div>
        <div class="card-title">事件日志<span class="sub">只追加</span></div>
        <div class="audit-list" style="max-height:300px">${eventRows || '<div class="tiny muted">暂无事件</div>'}</div>
      </div>
    </div>`;
}

/* ---------- bundle ---------- */
async function viewBundle() {
  const list = await api('/api/bundles');
  if (!list.length) {
    $('#view').innerHTML = '<div class="empty">尚无冻结站包</div>';
    return;
  }
  const data = await api(`/api/bundles/${encodeURIComponent(
    (list.find((b) => b.bundle_id === state.bundleFocus) || list[0]).bundle_id,
  )}`);
  const draftMeta = await api('/api/bundles/draft/topics').catch(() => ({ topics: [] }));
  state.bundleCurrent = data;
  state.bundlePayload = null;
  state.bundleDraft = null;
  const draftTopicOptions = (draftMeta.topics || [])
    .map((t) => `<option value="${dataAttr(t.key)}">${esc(t.label)} · ${esc(t.station_id)}</option>`)
    .join('');
  const [major, minor] = String(data.version || '1.0').split('.');
  const nextVersion = `${major || 1}.${(Number(minor) || 0) + 1}`;
  const checks = data.validation.checks.map((c) => `
    <div class="check-card ${c.status}">
      <div class="ch"><span>${esc(c.label)}</span>${c.status === 'pass' ? '<span class="pill teal">通过</span>' : c.status === 'warn' ? '<span class="pill amber">待核验</span>' : '<span class="pill red">阻断</span>'}</div>
      <p>${esc(c.rule)}</p>
      ${c.issues && c.issues.length ? `<p style="color:var(--amber)">${esc(c.issues[0])}</p>` : ''}
    </div>`).join('');
  const itemsRows = data.items.map((item) => `
    <tr>
      <td><div class="mono tiny muted">${esc(item.item_id)}</div><div style="font-weight:600">${esc(item.title)}</div></td>
      <td class="truncate" title="${dataAttr(item.anchor.observable)}">${esc(item.anchor.observable)}</td>
      <td><span class="pill">${esc(item.evidence_type)}</span><div class="tiny muted">${esc(item.phase)}</div></td>
      <td class="num">${item.weight}</td>
      <td>${item.critical ? '<span class="pill red">关键</span>' : '<span class="muted tiny">—</span>'}</td>
      <td><span class="pill ${item.effective_authority === 'none' ? 'muted' : item.effective_authority === 'extract' ? 'blue' : 'teal'}">${esc(item.authority_label)}</span>
        ${item.override ? `<div class="tiny" style="color:var(--amber);margin-top:2px">已降级：${esc(item.override.reason)}</div>` : ''}</td>
      <td><span class="pill ${item.l3_authority === 'none' ? 'muted' : item.l3_authority === 'extract' ? 'blue' : 'teal'}">${esc(item.l3_authority_label)}</span></td>
      <td class="tiny muted">${esc(item.guideline_ref)}</td>
    </tr>`).join('');
  const slotRows = data.derived.slots.map((s) => `
    <tr>
      <td class="mono tiny">${esc(s.slot_id)}</td>
      <td><span class="pill ${s.disclosure === 'active' ? 'teal' : s.disclosure === 'on_trust' ? 'purple' : 'muted'}">${esc(s.disclosure)}</span></td>
      <td class="tiny">${esc(s.phase)}</td>
      <td class="tiny">${(s.linked_items || []).map(esc).join(', ') || '公共信息'}</td>
      <td class="small dim">${esc(s.content)}</td>
      <td>${s.leak_guard ? '<span class="pill teal">on</span>' : '<span class="pill muted">off</span>'}</td>
    </tr>`).join('');
  const auditRows = data.audit.map((ev) => `
    <div class="audit-row"><div class="act">${esc(ev.action)}</div><div class="obj">${esc(ev.actor)}</div><div class="time">${fmtTime(ev.created_at)}</div></div>`).join('');
  const adjudications = data.adjudications || [];
  const consistencyDetails = (data.validation && data.validation.consistency_details) || {};
  const consistencyDiffs = (data.validation && data.validation.review_diffs) || consistencyDetails.diffs || [];
  const adjudicationByDiff = {};
  adjudications.forEach((a) => { adjudicationByDiff[a.diff_id] = a; });
  const diffLocate = (d) => {
    const loc = d.location || {};
    if (loc.section === 'rubric') {
      let text = `评分项 ${esc(loc.item_id)}：${esc(loc.sentence)}`;
      if (loc.guideline_ref) text += ` · 指南「${esc(loc.guideline_ref)}」`;
      if (loc.closest_instruction) {
        text += ` · 最接近题干句「${esc(loc.closest_instruction.sentence)}」`;
      }
      return text;
    }
    if (loc.section === 'extra_slots') {
      return `附加信息槽 #${esc(loc.slot_index)}：${esc(loc.sentence)}`;
    }
    if (loc.section === 'instruction_item_refs') {
      return `考生指令引用 #${esc(loc.ref_index)} → ${esc(loc.item_id)}`;
    }
    return '—';
  };
  const adjudicationCard = consistencyDiffs.length || adjudications.length ? `
    <div class="card" style="margin-bottom:14px">
      <div class="card-title">冻结前待核验项与裁决<span class="sub">${consistencyDiffs.length} 项 · ${adjudications.length} 条留痕 · 指南与三方一致差异逐条裁决</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>差异</th><th>定位（精确到句子）</th><th>严重度</th><th>裁决</th><th>理由</th><th>裁决人</th><th>时间</th></tr></thead>
          <tbody>${consistencyDiffs.map((d) => {
            const a = adjudicationByDiff[d.diff_id];
            return `
            <tr>
              <td class="tiny"><div class="mono tiny">${esc(d.diff_id)}</div>${esc(d.message)}<div class="tiny muted">建议：${esc(d.hint)}</div></td>
              <td class="tiny">${diffLocate(d)}</td>
              <td class="tiny">${d.severity === 'fail' ? '<span class="pill red">阻断</span>' : '<span class="pill amber">待核验</span>'}</td>
              <td class="tiny">${a
                ? (a.decision === 'accept' ? '<span class="pill teal">接受差异</span>' : '<span class="pill red">返回修改</span>')
                : '<span class="pill muted">未裁决</span>'}</td>
              <td class="tiny">${esc((a && a.note) || '—')}</td>
              <td class="tiny">${esc((a && a.actor) || '—')}</td>
              <td class="tiny">${a ? fmtTime(a.created_at) : '—'}</td>
            </tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    </div>` : '';

  $('#view').innerHTML = `
    <div class="view-head">
      <div>
        <h2>${esc(data.title)}</h2>
        <p>评分表不是流程的终点产物，而是贯穿全链路的契约。冻结瞬间派生信息槽、证据探针、检查点与反馈模板，共享同一 bundle_version。</p>
      </div>
      <div><span class="pill teal">${esc(data.status)}</span> <span class="pill">v${esc(data.version)}</span></div>
    </div>

    <div class="card" style="margin-bottom:14px">
      <div class="card-title">S1 命题辅助<span class="sub">L0 草案 · 必须经专家改写后才能冻结</span></div>
      <div class="btn-row" style="flex-wrap:wrap;gap:8px;align-items:center">
        <select id="draft-topic" class="input" style="max-width:200px" aria-label="病例主题">${draftTopicOptions}</select>
        <select id="draft-difficulty" class="input" style="max-width:110px" aria-label="目标难度">
          <option value="easy">易</option>
          <option value="medium" selected>中</option>
          <option value="hard">难</option>
        </select>
        <input id="draft-duration" class="input" type="number" min="5" max="30" value="8" style="max-width:90px" aria-label="建议时长">
        <button class="btn small" data-action="bundle-draft">生成 L0 草案</button>
        <button class="btn small primary" data-action="bundle-draft-load" disabled>载入为提案</button>
      </div>
      <div id="bundle-draft-result" class="tiny muted" style="margin-top:8px">草案只用于把空白页变成可修改的初稿；生成后会同时给出四项强制检查、配额、难度预判与跨站相似度。</div>
    </div>

    <div class="card" style="margin-bottom:14px">
      <div class="card-title">站包提案校验与冻结<span class="sub">冻结即不可变 · 相同内容不会重复入库</span></div>
      <div class="btn-row" style="flex-wrap:wrap;gap:8px;align-items:center">
        <button class="btn small" data-action="bundle-export">下载当前站包 JSON</button>
        <input id="bundle-file" class="input" type="file" accept="application/json,.json" style="max-width:260px">
        <input id="bundle-version" class="input" style="max-width:100px" value="${nextVersion}" aria-label="新版本号">
        <input id="bundle-reason" class="input" style="max-width:240px" value="站包修订" aria-label="冻结原因">
        <button class="btn small" data-action="bundle-validate">校验提案</button>
        <button class="btn small primary" data-action="bundle-freeze">签字冻结</button>
      </div>
      <div id="bundle-proposal-result" class="tiny muted" style="margin-top:8px">先下载当前站包作为模板，修改后上传；校验通过才能冻结。新站包需重新完成 SP 认证。</div>
    </div>

    <div class="card" style="margin-bottom:14px">
      <div class="card-title">版本历史<span class="sub">只增不改 · 当前冻结基准 v${esc(data.version)}</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>版本</th><th>bundle_id</th><th>冻结时间</th><th>原因</th><th>替代版本</th><th>哈希</th><th></th></tr></thead>
          <tbody>${list.map((b) => `
            <tr>
              <td><span class="pill ${b.bundle_id === data.bundle_id ? 'teal' : 'muted'}">v${esc(b.version)}</span></td>
              <td class="mono tiny">${esc(b.bundle_id)}</td>
              <td class="tiny">${fmtTime(b.frozen_at)}</td>
              <td class="tiny">${esc(b.reason || '—')}</td>
              <td class="mono tiny">${esc(b.supersedes || '—')}</td>
              <td class="mono tiny" title="${dataAttr(b.bundle_hash)}">${esc((b.bundle_hash || '').slice(0, 16))}…</td>
              <td>${b.bundle_id === data.bundle_id
                ? '<span class="pill muted">查看中</span>'
                : `<button class="btn small" data-action="bundle-view" data-bundle="${esc(b.bundle_id)}">查看</button>`}</td>
            </tr>`).join('')}</tbody>
        </table>
      </div>
      ${data.bundle_id !== list[0].bundle_id ? '<div class="btn-row" style="margin-top:8px"><button class="btn small" data-action="bundle-latest">回到最新版本</button></div>' : ''}
    </div>

    ${adjudicationCard}

    <div class="card" style="margin-bottom:14px">
      <div class="card-title">冻结完整性核验<span class="sub">bundle_hash · 派生资产 · 评分版本戳 · 冻结审计锚点</span></div>
      <div class="btn-row" style="align-items:center;gap:8px">
        <button class="btn small" data-action="bundle-verify" data-bundle="${dataAttr(data.bundle_id)}">核验当前版本</button>
        <span id="bundle-verify-result" class="tiny muted">只读重算，不修改冻结内容；核验动作写入审计。</span>
      </div>
    </div>

    <div class="grid c2" style="margin-bottom:14px">
      <div class="card">
        <div class="card-title">冻结标识<span class="sub">不可变</span></div>
        <div class="version-row"><span class="k">bundle_id</span><span class="v">${esc(data.bundle_id)}</span></div>
        <div class="version-row"><span class="k">station_id</span><span class="v">${esc(data.station_id)}</span></div>
        <div class="version-row"><span class="k">frozen_at</span><span class="v">${esc(data.frozen_at)}</span></div>
        <div class="version-row"><span class="k">supersedes</span><span class="v">${esc(data.supersedes || '—')}</span></div>
        <div class="sep"></div>
        <div class="hash-block">${esc(data.bundle_hash)}</div>
      </div>
      <div class="card">
        <div class="card-title">四类派生资产<span class="sub">1 rubric → 4 downstream</span></div>
        <div class="derivation">
          <div class="source-node">
            <div class="l">SOURCE OF TRUTH</div>
            <div class="t">RubricItem × ${data.derived.counts.probes}</div>
            <div class="tiny muted">行为锚 · 证据形态 · 阶段 · 权限 · 权重</div>
          </div>
          <div class="arrow-v">→</div>
          <div class="asset-grid">
            <div class="asset"><b>SP 信息槽 × ${data.derived.counts.slots}</b><span>患者知道什么及触发条件</span></div>
            <div class="asset"><b>对话检查点 × ${data.derived.counts.checkpoints}</b><span>阶段转换与引导语</span></div>
            <div class="asset"><b>证据探针 × ${data.derived.counts.probes}</b><span>定位 utterance 的检索式</span></div>
            <div class="asset"><b>反馈模板 × ${data.derived.counts.feedback_templates}</b><span>扣分时的替代话术</span></div>
          </div>
        </div>
      </div>
    </div>

    <div class="checks" style="margin-bottom:14px">${checks}</div>

    <div class="card" style="margin-bottom:14px">
      <div class="card-title">评分项与项级 AI 权限<span class="sub">${data.items.length} 项 · 权限最小粒度是评分项</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>评分项</th><th>可观测行为锚</th><th>证据 / 阶段</th><th class="num">权重</th><th>关键项</th><th>当前 AI 权限</th><th>L3 权限</th><th>指南引用</th></tr></thead>
          <tbody>${itemsRows}</tbody>
        </table>
      </div>
    </div>

    <div class="card" style="margin-bottom:14px">
      <div class="card-title">SP 信息槽<span class="sub">说什么由确定性状态机控制 · leak_guard 默认开启</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>槽位</th><th>披露阈值</th><th>阶段</th><th>关联评分项</th><th>内容</th><th>泄漏防护</th></tr></thead>
          <tbody>${slotRows}</tbody>
        </table>
      </div>
    </div>

    <div class="grid c2">
      <div class="card">
        <div class="card-title">阶段检查点<span class="sub">必须触发项</span></div>
        ${data.derived.checkpoints.map((cp) => `
          <div class="version-row"><span class="k">${esc(cp.phase)}</span><span class="v">${(cp.must_probe || []).join(' · ') || '—'}</span></div>`).join('')}
      </div>
      <div class="card">
        <div class="card-title">站包审计</div>
        <div class="audit-list">${auditRows}</div>
      </div>
    </div>
  `;
  const fileInput = $('#bundle-file');
  if (fileInput) {
    fileInput.addEventListener('change', async () => {
      const box = $('#bundle-proposal-result');
      const file = fileInput.files && fileInput.files[0];
      if (!file) {
        state.bundlePayload = null;
        return;
      }
      try {
        const payload = JSON.parse(await file.text());
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
          throw new Error('站包 JSON 顶层必须是对象');
        }
        state.bundlePayload = payload;
        box.className = 'tiny';
        box.innerHTML = `<span class="pill blue">已载入</span> ${esc(file.name)} · ${Array.isArray(payload.rubric) ? payload.rubric.length : 0} 个评分项，待校验`;
      } catch (err) {
        state.bundlePayload = null;
        box.className = 'tiny';
        box.innerHTML = `<span style="color:var(--red)">文件解析失败：${esc(err.message)}</span>`;
      }
    });
  }
}

/* ---------- feedback ---------- */
async function viewTransparency() {
  const data = await api('/api/transparency');
  const cards = data.model_cards || [];
  const sheet = data.data_sheet;
  const cardHtml = cards.length ? cards.map((card) => `
    <div class="card" style="margin-bottom:12px">
      <div class="card-title">${esc(card.capability)}<span class="sub">${esc(card.model_version)} · ${esc(card.prompt_version)}</span></div>
      <div class="attempt-strip" style="margin-bottom:6px">
        <span>QWK <b>${num(card.metrics.qwk_ai, 3)}</b></span>
        <span>边缘案例一致 <b>${pct(card.metrics.near_cutoff_agreement, 1)}</b></span>
        <span>证据定位 <b>${pct(card.metrics.evidence_rate, 1)}</b></span>
        <span>编辑率 <b>${pct(card.metrics.edit_rate, 1)}</b></span>
        <span>样本 <b>${card.metrics.sample_size ?? '—'}</b></span>
        ${card.published_at ? `<span class="tiny muted">公开于 ${fmtTime(card.published_at)}</span>` : ''}
      </div>
      ${(card.metrics.item_disagreement_hotspots || []).length ? `<div class="tiny muted" style="margin-bottom:4px">项级分歧热点：${card.metrics.item_disagreement_hotspots.slice(0, 5).map((h) => `${esc(h.item_title || h.item_id)} ${pct(h.rate, 0)}`).join(' · ')}</div>` : ''}
      <div class="tiny" style="color:var(--amber)">${esc(card.limitations || '暂无已知限制说明')}</div>
    </div>`).join('') : '<div class="empty">尚未公开发布模型卡</div>';
  const sheetRows = sheet ? Object.entries(sheet.content || {}).map(([key, value]) => {
    const text = Array.isArray(value) ? value.join('；') : (value && typeof value === 'object') ? JSON.stringify(value) : value;
    return `<div class="version-row"><span class="k">${esc(key)}</span><span class="v">${esc(text ?? '—')}</span></div>`;
  }).join('') : '';
  $('#view').innerHTML = `
    <div class="view-head">
      <div><h2>透明公开</h2><p>${esc(data.statement)}</p></div>
    </div>
    <div class="grid c1-2" style="margin-bottom:16px">
      <div>
        <div class="card-title" style="margin-bottom:8px">模型卡<span class="sub">已公开版本</span></div>
        ${cardHtml}
      </div>
      <div class="card">
        <div class="card-title">数据表<span class="sub">${sheet ? `v${esc(sheet.version)}` : '尚未公开'}</span></div>
        ${sheet ? sheetRows : '<div class="empty">尚未公开发布数据表</div>'}
        ${sheet && sheet.published_at ? `<div class="tiny muted" style="margin-top:6px">公开于 ${fmtTime(sheet.published_at)}</div>` : ''}
      </div>
    </div>
    <div class="tiny muted">公开内容由治理流程发布；未公开的模型卡与数据表仅在质量与闸门页对授权角色可见。</div>
  `;
}

async function viewFeedback() {
  const attempts = await api('/api/attempts');
  if (!attempts.length) {
    $('#view').innerHTML = '<div class="empty">尚无场次</div>';
    return;
  }
  let selectedId = state.feedbackId;
  if (!selectedId || !attempts.some((a) => a.attempt_id === selectedId)) {
    selectedId = (attempts.find((a) => a.status === 'published') || attempts[0]).attempt_id;
  }
  state.feedbackId = selectedId;
  $('#view').innerHTML = `
    <div class="view-head">
      <div>
        <h2>反馈报告</h2>
        <p>每条扣分项必须包含三段：发生了什么（证据）· 对应哪个评分项（行为）· 下次可以怎么说（替代方案）。</p>
      </div>
      <div class="attempt-picker">
        <label class="field" style="font-size:10px">场次
          <select class="input" id="feedback-select">
            ${attempts.map((a) => `<option value="${dataAttr(a.attempt_id)}" ${a.attempt_id === selectedId ? 'selected' : ''}>${esc(a.candidate_code)} · ${esc(a.candidate_name)} · ${esc(a.status)}</option>`).join('')}
          </select>
        </label>
      </div>
    </div>
    <div id="feedback-body"><div class="loading">正在生成反馈…</div></div>
  `;
  $('#feedback-select').addEventListener('change', (e) => {
    state.feedbackId = e.target.value;
    renderFeedback(state.feedbackId);
  });
  await renderFeedback(selectedId);
}

async function renderFeedback(attemptId) {
  const body = $('#feedback-body');
  body.innerHTML = '<div class="loading">正在生成反馈…</div>';
  const data = await api(`/api/feedback/${encodeURIComponent(attemptId)}`);
  const purgedNote = data.media_purged_at ? '媒体已按留存策略清理，' : '';
  const history = data.history || {};
  const crossProfile = data.cross_station_profile || {};
  const domainMax = Math.max(1, ...data.domains.map((d) => d.score || 0));
  const deductionHtml = data.deductions.length ? data.deductions.map((u) => {
    const ev = u.evidence;
    const evText = ev ? `${mmss(ev.start_ms)} · ${esc(ev.quote)}` : '人工记录 / 无片段';
    return `<div class="fb-card">
      <div class="fb-head">
        <h4>${u.critical ? '<span class="pill badge-critical">关键</span> ' : ''}${esc(u.item_title)} <span class="mono tiny muted">${esc(u.item_id)}</span></h4>
        <div>${reasonPill(u.score === 0 ? 'critical_negative' : 'near_cutoff', u.score_label)}</div>
      </div>
      <div class="fb-part"><div class="lab">① 证据</div><div class="val">${ev && u.media_url && !data.media_purged_at ? `<button class="timestamp-btn" data-action="play-feedback" data-start="${ev.start_ms}" data-end="${ev.end_ms}" data-url="${dataAttr(u.media_url || '')}">播放 ${mmss(ev.start_ms)}</button> ` : ''}${purgedNote}${evText}</div></div>
      <div class="fb-part"><div class="lab">② 行为</div><div class="val">${esc(u.behavior)}</div></div>
      <div class="fb-part alt"><div class="lab">③ 替代</div><div class="val">${esc(u.alternative || '下次可追加一句具体提问，覆盖该评分项后再进入下一环节。')}</div></div>
    </div>`;
  }).join('') : '<div class="empty">本次没有扣分项。</div>';

  body.innerHTML = `
    <div class="grid c4" style="margin-bottom:14px">
      <div class="kpi blue"><div class="k">场次</div><div class="v" style="font-size:18px">${esc(data.candidate_name)}</div><div class="d">${esc(data.candidate_code)} · ${statusPill(data.status)}</div></div>
      <div class="kpi teal"><div class="k">最终总分</div><div class="v">${num(data.final_total, 1)}</div><div class="d">AI 暂定 ${num(data.ai_total, 1)} · 及格线 ${num(data.pass_line, 0)}</div></div>
      <div class="kpi ${data.final_total !== null && data.final_total >= data.pass_line ? 'teal' : 'red'}"><div class="k">结论</div><div class="v" style="font-size:18px">${data.final_total === null ? '待定' : data.final_total >= data.pass_line ? '达标' : '未达标'}</div><div class="d">${esc(data.status_note)}</div></div>
      <div class="kpi amber"><div class="k">扣分项</div><div class="v">${data.deductions.length}</div><div class="d">优势项 ${data.strengths.length} · 人工评分项可能无片段</div></div>
    </div>

    ${data.feedback_note ? `<div class="card" style="margin-bottom:16px">
      <div class="card-title">教师寄语<span class="sub">人工撰写 · 随成绩一同发布</span></div>
      <div class="small">${esc(data.feedback_note)}</div>
    </div>` : ''}

    ${data.ai_disclosure ? `<div class="card" style="margin-bottom:16px">
      <div class="card-title">AI 参与说明<span class="sub">考生知情</span></div>
      <div class="attempt-strip" style="margin-bottom:6px">
        <span>参与环节 <b>${data.ai_disclosure.stages.length ? esc(data.ai_disclosure.stages.join('、')) : '无'}</b></span>
        ${data.ai_disclosure.model_versions.length ? `<span>模型 <b>${esc(data.ai_disclosure.model_versions.join('、'))}</b></span>` : ''}
        <span>${data.ai_disclosure.human_confirmed ? '<span class="pill teal">已人工确认</span>' : '<span class="pill amber">待人工签字</span>'}${data.ai_disclosure.human_reviewers.length ? ` ${esc(data.ai_disclosure.human_reviewers.join('、'))}` : ''}</span>
      </div>
      <div class="small">${esc(data.ai_disclosure.statement)}</div>
      <div class="btn-row" style="margin-top:8px">
        ${data.ai_disclosure.acknowledged
          ? `<span class="pill teal">已阅读确认</span><span class="tiny muted">${fmtTime(data.ai_disclosure.acknowledged_at)}</span>`
          : (state.user && state.user.role === 'student'
            ? `<button class="btn small primary" data-action="ack-disclosure" data-attempt="${dataAttr(data.attempt_id)}">我已阅读并知晓</button>`
            : '<span class="tiny muted">学生登录后可确认已阅读。</span>')}
      </div>
    </div>` : ''}

    ${data.tone ? `<div class="card" style="margin-bottom:16px">
      <div class="card-title">语气检查<span class="sub">内部粗筛 · 最终由审核人确认</span></div>
      <div class="attempt-strip" style="margin-bottom:6px">
        <span>${data.tone.level === 'ok' ? '<span class="pill teal">通过粗筛</span>' : '<span class="pill amber">需人工复核</span>'}</span>
        ${data.tone.failing ? '<span class="pill">不及格反馈</span>' : ''}
        ${data.tone.harsh_terms.length ? `<span>打击性表述 <b>${data.tone.harsh_terms.map(esc).join('、')}</b></span>` : ''}
        ${data.tone.encouragement_terms.length ? `<span>鼓励性表述 ${data.tone.encouragement_terms.length} 处</span>` : '<span>未检出鼓励性表述</span>'}
      </div>
      ${(data.tone.suggestions || []).map((s) => `<div class="tiny" style="color:var(--amber)">${esc(s)}</div>`).join('')}
      <div class="tiny muted" style="margin-top:4px">${esc(data.tone.note)}</div>
    </div>` : ''}

    ${history.previous ? `<div class="card" style="margin-bottom:16px">
      <div class="card-title">纵向趋势<span class="sub">同站上次 vs 本次 · 仅用于形成性反馈</span></div>
      <div class="attempt-strip" style="margin-bottom:6px">
        <span>上次 <b>${num(history.previous.total, 1)}</b>（v${esc(history.previous.version)} · ${fmtTime(history.previous.created_at)}）</span>
        <span>本次 <b>${num(data.final_total, 1)}</b></span>
        <span>变化 <span class="pill ${(history.delta_total || 0) >= 0 ? 'teal' : 'red'}">${history.delta_total > 0 ? '+' : ''}${num(history.delta_total, 1)}</span></span>
      </div>
      ${(history.domain_deltas || []).map((d) => `
        <div class="version-row">
          <span class="k">${esc(d.domain)}</span>
          <span class="v">${num(d.previous, 1)} → ${num(d.current, 1)} <span class="pill ${d.delta >= 0 ? 'teal' : 'red'}">${d.delta > 0 ? '+' : ''}${num(d.delta, 1)}</span></span>
        </div>`).join('')}
      <div class="tiny muted" style="margin-top:6px">${esc(history.note || '')}</div>
    </div>` : ''}

    ${(crossProfile.attempts_count || 0) > 0 ? `<div class="card" style="margin-bottom:16px">
      <div class="card-title">跨站能力画像<span class="sub">${crossProfile.attempts_count} 场 · ${crossProfile.stations_count} 个考站 · 仅已发布与当前场次</span></div>
      <div class="attempt-strip" style="margin-bottom:6px">
        ${crossProfile.strongest ? `<span>相对优势 <b>${esc(crossProfile.strongest.label)} ${num(crossProfile.strongest.score, 1)}</b></span>` : ''}
        ${crossProfile.weakest ? `<span>相对薄弱 <b>${esc(crossProfile.weakest.label)} ${num(crossProfile.weakest.score, 1)}</b></span>` : ''}
      </div>
      ${(crossProfile.domains || []).map((d) => `
        <div class="version-row">
          <span class="k">${esc(d.label)}</span>
          <span class="v">${num(d.score, 1)} · 权重 ${num(d.weight, 0)} · ${d.attempts} 场 / ${d.items} 项</span>
        </div>`).join('')}
      ${crossProfile.attempts_count === 1 ? '<div class="tiny muted">当前仅 1 场，跨站比较需更多考站场次。</div>' : ''}
      <div class="tiny muted" style="margin-top:6px">${esc(crossProfile.note || '')}</div>
    </div>` : ''}

    <div class="grid c1-2" style="margin-bottom:16px">
      <div class="card">
        <div class="card-title">能力域画像<span class="sub">12 站可映射到 6 个能力域</span></div>
        <div class="domain-bars">
          ${data.domains.map((d) => `
            <div class="domain-row">
              <span class="bar-label">${esc(d.domain)}</span>
              <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, (d.score || 0) / domainMax * 100)}%"></div></div>
              <span class="bar-count">${num(d.score, 0)}</span>
            </div>`).join('')}
        </div>
        <div class="sep"></div>
        <div class="card-title" style="margin-bottom:6px">优势项</div>
        <div class="strength-list">
          ${data.strengths.length ? data.strengths.map((s) => `<span class="pill teal">${esc(s.item_title)}</span>`).join('') : '<span class="tiny muted">暂无</span>'}
        </div>
        <div class="tiny muted" style="margin-top:6px">纵向趋势：${history.previous ? `已对比上次同站场次（${fmtTime(history.previous.created_at)}）` : '暂无同站历史场次'}</div>
      </div>
      <div class="card">
        <div class="card-title">反馈纪律<span class="sub">三段式</span></div>
        <div class="feedback-rule">${esc(data.feedback_rule)}</div>
        ${data.feedback_quality ? `<div class="tiny" style="margin-top:6px">
          <span class="pill ${data.feedback_quality.all_have_alternative ? 'teal' : 'amber'}">替代话术 ${data.feedback_quality.with_alternative}/${data.feedback_quality.checked}</span>
          <span class="pill">时间戳 ${data.feedback_quality.with_timestamp}</span>
          ${data.feedback_quality.repaired ? `<span class="pill amber">兜底修复 ${data.feedback_quality.repaired}</span>` : ''}
          ${data.feedback_quality.repaired_evidence ? `<span class="pill amber">证据兜底 ${data.feedback_quality.repaired_evidence}</span>` : ''}
        </div>` : ''}
        ${data.readability && data.readability.level !== 'unmeasured' ? `<div class="attempt-strip" style="margin-top:8px">
          <span>可读性 <span class="pill ${data.readability.level === 'easy' ? 'teal' : data.readability.level === 'hard' ? 'red' : 'amber'}">${esc(data.readability.level_label)}</span></span>
          <span>平均句长 <b>${num(data.readability.avg_sentence_chars, 1)}</b> 字</span>
          <span>术语密度 <b>${num(data.readability.term_density, 2)}</b>/100 字</span>
        </div>
        ${data.readability.note ? `<div class="tiny muted" style="margin-top:4px">${esc(data.readability.note)}</div>` : ''}
        ${(data.readability.suggestions || []).length ? `<div class="tiny" style="color:var(--amber);margin-top:2px">${data.readability.suggestions.map(esc).join('；')}</div>` : ''}` : ''}
        <div class="sep"></div>
        <div class="small dim">第三段是最容易被忽略、也最有价值的部分——它把评分表从“审判工具”变成“教学工具”。</div>
        <div class="sep"></div>
        <audio id="fb-audio" preload="metadata" controls></audio>
        <div class="tiny muted" style="margin-top:6px">点击任意时间戳播放对应证据片段。</div>
        <div class="sep"></div>
        ${data.survey ? `<div class="tiny muted">反馈问卷已提交：<span class="pill teal">${data.survey.rating}/5</span> · ${fmtTime(data.survey.created_at)}</div>` : `
        <div class="card-title" style="margin-bottom:6px">看懂了并有帮助吗？</div>
        <form id="feedback-survey" class="btn-row" style="align-items:center;flex-wrap:wrap">
          <select class="input" name="rating" style="max-width:150px">
            <option value="5">5 非常清楚</option>
            <option value="4">4 比较清楚</option>
            <option value="3">3 一般</option>
            <option value="2">2 不太清楚</option>
            <option value="1">1 完全没看懂</option>
          </select>
          <select class="input" name="usefulness" style="max-width:140px">
            <option value="">实用性（可选）</option>
            ${Array.from({ length: 10 }, (_, i) => `<option value="${i + 1}">${i + 1} / 10</option>`).join('')}
          </select>
          <select class="input" name="realism" style="max-width:140px">
            <option value="">真实感（可选）</option>
            ${Array.from({ length: 10 }, (_, i) => `<option value="${i + 1}">${i + 1} / 10</option>`).join('')}
          </select>
          <input class="input" name="participant_label" placeholder="标签（可选）" maxlength="40" style="max-width:180px">
          <button class="btn small primary" type="submit">提交自评</button>
        </form>
        <div id="feedback-survey-result" class="tiny muted"></div>`}
      </div>
    </div>

    <div class="card">
      <div class="card-title">逐项反馈<span class="sub">证据 → 行为 → 替代话术</span></div>
      ${deductionHtml}
    </div>
  `;
  const audio = $('#fb-audio');
  if (audio) {
    audio.addEventListener('timeupdate', () => {
      const end = Number(audio.dataset.clipEnd || 0);
      if (end && audio.currentTime >= end) audio.pause();
    });
  }
  const surveyForm = $('#feedback-survey');
  if (surveyForm) {
    surveyForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const box = $('#feedback-survey-result');
      const formData = new FormData(surveyForm);
      try {
        const result = await api(`/api/feedback/${encodeURIComponent(data.attempt_id)}/survey`, {
          method: 'POST',
          body: {
            rating: Number(formData.get('rating')),
            usefulness: formData.get('usefulness') ? Number(formData.get('usefulness')) : null,
            realism: formData.get('realism') ? Number(formData.get('realism')) : null,
            participant_label: String(formData.get('participant_label') || ''),
          },
        });
        toast(`反馈自评已提交：${result.survey.rating}/5`);
        await viewFeedback();
      } catch (err) {
        if (box) box.textContent = err.message;
        toast(err.message, true);
      }
    });
  }
}

/* ---------- SP ---------- */
async function viewSP(bundleId = state.spBundleId || '') {
  const query = bundleId ? `?bundle_id=${encodeURIComponent(bundleId)}` : '';
  const [persona, stationData, consentData, certHistory, approvalHistory] = await Promise.all([
    api(`/api/sp/persona${query}`),
    api('/api/bundles/stations'),
    api(`/api/sp/consents${query}`).catch(() => ({ valid: false, consents: [], note: '知情同意记录仅考务可查看。' })),
    api(`/api/sp/certifications${query}`).catch(() => []),
    api(`/api/sp/persona-approvals${query}`).catch(() => ({ approvals: [] })),
  ]);
  state.spBundleId = persona.bundle_id;
  state.spStations = stationData.stations || [];
  state.spConsent = consentData;
  if (!state.sp || state.sp.persona.bundle_id !== persona.bundle_id) {
    state.sp = { persona, session: null, sessionError: null };
    try {
      await createSpSession();
    } catch (err) {
      state.sp.sessionError = err.message;
    }
  } else {
    state.sp.persona = persona;
  }
  state.sp.certifications = certHistory || [];
  state.sp.approvals = (approvalHistory && approvalHistory.approvals) || [];
  renderSP();
}

async function createSpSession() {
  const data = await api('/api/sp/sessions', {
    method: 'POST',
    body: { bundle_id: state.sp.persona.bundle_id, candidate_label: state.reviewer || '练习学生' },
  });
  state.sp.session = data;
}

function renderSP() {
  const persona = state.sp.persona;
  const session = state.sp.session;
  if (state.sp.sessionError) {
    $('#view').innerHTML = `
      <div class="view-head">
        <div><h2>SP 演练</h2><p>AI 模拟患者只允许在已完成认证的站包上运行。</p></div>
      </div>
      <div class="alert danger"><b>SP 演练不可用</b><p>${esc(state.sp.sessionError)}</p></div>
      <div class="btn-row"><button class="btn amber" data-action="sp-certify">运行红队认证</button></div>`;
    return;
  }
  const p = persona.persona || {};
  const dims = p.persona || {};
  const phaseOrder = persona.phases || [];
  const currentPhase = session ? session.phase : 'opening';
  const releasedMap = {};
  (session && session.released_slots ? session.released_slots : []).forEach((s) => { releasedMap[s.slot_id] = s; });
  const slots = (persona.slots || []).map((s) => {
    const released = releasedMap[s.slot_id];
    return `<div class="slot-row ${released ? 'released' : ''}">
      <div><span class="sid">${esc(s.slot_id)}</span>${released ? '<span class="pill teal" style="padding:0 5px">已释放</span>' : '<span class="pill muted" style="padding:0 5px">未释放</span>'}</div>
      <div class="content">${esc(released ? released.content : s.content)}</div>
    </div>`;
  }).join('');
  const messages = session && session.messages ? session.messages : [];
  const verify = session && session.verification;
  const guidance = session && session.intent && session.intent.guidance ? session.intent : null;
  const cert = persona.certification;
  const certValid = Boolean(cert && cert.valid);
  const finished = Boolean(session && session.finished);
  const stationOptions = (state.spStations || [])
    .map((s) => `<option value="${dataAttr(s.bundle_id)}" ${s.bundle_id === persona.bundle_id ? 'selected' : ''}>${esc(s.station_id)} · ${esc(s.title)} · v${esc(s.version)}</option>`)
    .join('');
  const certHtml = `
    <section class="card">
      <div class="card-title">SP 认证<span class="sub">红队 + 20×3 一致性</span></div>
      ${cert ? `
        <div class="attempt-strip"><span>${cert.expired ? '<span class="pill red">已过期</span>' : cert.stale ? '<span class="pill red">版本已变更</span>' : cert.case_stale ? '<span class="pill red">用例已更新</span>' : cert.status === 'certified' ? '<span class="pill teal">已认证</span>' : '<span class="pill red">未通过</span>'}</span><span>${esc(fmtTime(cert.created_at))}</span></div>
        <div class="version-row"><span class="k">红队用例版本</span><span class="v">${esc(cert.red_team_version || '—')}${cert.case_stale ? ` → 当前 ${esc(cert.current_red_team_version || '—')}` : ''}</span></div>
        <div class="version-row"><span class="k">红队用例</span><span class="v">${cert.red_team.passed_cases}/${cert.red_team.case_count} 通过</span></div>
        <div class="version-row"><span class="k">信息泄露</span><span class="v">${cert.red_team.leak_count}</span></div>
        <div class="version-row"><span class="k">人格首轮通过</span><span class="v">${pct(cert.red_team.persona_first_pass_rate, 0)}</span></div>
        <div class="version-row"><span class="k">跨会话一致性</span><span class="v">${pct(cert.consistency.rate, 0)}</span></div>
        <div class="version-row"><span class="k">P95 单轮延迟</span><span class="v">${num(cert.red_team.p95_latency_ms, 0)} ms</span></div>
        <div class="version-row"><span class="k">认证有效期</span><span class="v">${cert.valid_until ? `${fmtTime(cert.valid_until)} · 剩余 ${cert.days_remaining} 天` : '未设置'}</span></div>
        ${cert.expired ? `<div class="violation">认证已于 ${esc(fmtTime(cert.valid_until))} 过期；G1 的 SP 指标按未采集处理，请按季度更新红队用例并复测。</div>` : ''}
        ${cert.stale ? `<div class="violation">认证绑定旧 persona_version ${esc((cert.persona_version || '').slice(7, 19))}，当前站包为 ${esc((persona.persona_version || '').slice(7, 19))}；G1 指标按未采集处理，请重新认证后再用于模拟考。</div>` : ''}
        ${cert.case_stale ? `<div class="violation">红队攻击用例已更新到 ${esc(cert.current_red_team_version || '—')}（旧认证 ${esc(cert.red_team_version || '—')}）；G1 指标按未采集处理，请重新运行红队认证。</div>` : ''}
        ${(cert.checks || []).filter((c) => !c.passed).map((c) => `<div class="violation">${esc(c.label)}：${esc(c.value)}（目标 ${esc(c.target)}）</div>`).join('')}
      ` : '<div class="tiny muted">尚未认证。红队会尝试诱导泄露、编造病史、跨阶段索取诊断等 7 类攻击。</div>'}
      <div class="sep"></div>
      <div class="btn-row"><button class="btn small ${certValid ? '' : 'primary'}" data-action="sp-certify">运行红队认证</button><span class="tiny muted">${esc((persona.persona_version || '').slice(7, 19))}</span></div>
    </section>`;
  const approval = persona.approval || {};
  const approvalRecord = approval.approval || null;
  const canApprove = Boolean(state.user && ['admin', 'exam_manager', 'examiner'].includes(state.user.role));
  const approvalPill = approval.approved
    ? '<span class="pill teal">已批准</span>'
    : approval.stale
      ? '<span class="pill red">版本已变更</span>'
      : approvalRecord && approvalRecord.decision === 'rejected'
        ? '<span class="pill red">已拒绝</span>'
        : '<span class="pill red">待批准</span>';
  const approvalHtml = `
    <section class="card">
      <div class="card-title">人格卡人工批准<span class="sub">披露阈值 · 禁用话术 · 真人试演</span></div>
      <div class="attempt-strip" style="margin-bottom:6px">
        ${approvalPill}
        <span>${approvalRecord ? `${esc(approvalRecord.actor)} · ${esc(fmtTime(approvalRecord.created_at))}` : '尚无批准记录'}</span>
        ${approvalRecord ? `<span>${esc(approval.scope_label || approval.scope || '')}</span>` : ''}
      </div>
      ${approvalRecord ? `
        <div class="version-row"><span class="k">披露阈值</span><span class="v">${approvalRecord.disclosure_approved ? '<span class="pill teal">已确认</span>' : '<span class="pill red">未确认</span>'}</span></div>
        <div class="version-row"><span class="k">禁用话术</span><span class="v">${approvalRecord.forbidden_approved ? '<span class="pill teal">已确认</span>' : '<span class="pill red">未确认</span>'}</span></div>
        <div class="version-row"><span class="k">真人试演</span><span class="v">${approvalRecord.trial_approved ? '<span class="pill teal">已通过</span>' : '<span class="pill red">未通过</span>'}</span></div>
        ${approvalRecord.note ? `<div class="tiny muted">${esc(approvalRecord.note)}</div>` : ''}
      ` : '<div class="tiny muted">需考官确认披露阈值、禁用话术并完成真人试演后，才能开放 SP 练习。</div>'}
      ${approval.stale ? '<div class="violation">批准绑定旧 persona_version，改版后需重新试演确认。</div>' : ''}
      ${canApprove ? `<div class="sep"></div>
      <form id="sp-approval-form" class="btn-row" style="flex-wrap:wrap;gap:6px">
        <select class="input" name="decision" aria-label="批准结论" style="max-width:110px"><option value="approved">批准</option><option value="rejected">拒绝</option></select>
        <select class="input" name="scope" aria-label="可用范围" style="max-width:135px"><option value="L1">仅练习</option><option value="L2">可进模拟考</option></select>
        <label class="tiny"><input type="checkbox" name="disclosure_approved"> 披露阈值已确认</label>
        <label class="tiny"><input type="checkbox" name="forbidden_approved"> 禁用话术已审定</label>
        <label class="tiny"><input type="checkbox" name="trial_approved"> 真人试演通过</label>
        <input class="input" name="note" placeholder="试演结论 / 修改意见" style="max-width:220px">
        <button class="btn small primary" type="submit">记录批准</button>
      </form>` : '<div class="tiny muted" style="margin-top:4px">人格卡批准由考官或考务记录。</div>'}
    </section>`;
  const consent = state.spConsent || {};
  const consentValid = Boolean(consent.valid);
  const canManageConsent = Boolean(state.user && ['admin', 'exam_manager', 'examiner'].includes(state.user.role));
  const consentHtml = `
    <section class="card">
      <div class="card-title">SP 知情同意<span class="sub">365 天有效 · 未成年需监护人</span></div>
      <div class="attempt-strip" style="margin-bottom:6px">
        <span>${consentValid ? '<span class="pill teal">有效</span>' : '<span class="pill red">缺失或已过期</span>'}</span>
        <span>${(consent.consents || []).length} 份记录</span>
      </div>
      ${(consent.consents || []).slice(0, 3).map((c) => `
        <div class="version-row"><span class="k">${esc(c.sp_name)} · ${esc((consent.type_labels || {})[c.consent_type] || c.consent_type)}</span>
          <span class="v">${esc(c.version)} · ${c.valid ? '<span class="pill teal">有效</span>' : '<span class="pill red">已过期</span>'}</span></div>`).join('') || '<div class="tiny muted">尚未记录 SP 知情同意。</div>'}
      ${canManageConsent ? `<div class="sep"></div>
      <form id="sp-consent-form" class="btn-row" style="flex-wrap:wrap;gap:6px">
        <input class="input" name="sp_name" placeholder="SP 姓名" style="max-width:110px" required>
        <select class="input" name="sp_role" style="max-width:135px" aria-label="SP 类型">
          <option value="adult_sp">成年模拟患者</option>
          <option value="minor_sp">未成年模拟患者</option>
          <option value="guardian">监护人</option>
        </select>
        <select class="input" name="consent_type" style="max-width:150px" aria-label="同意类型">
          <option value="informed_use">知情同意</option>
          <option value="minor_guardian">监护人同意</option>
          <option value="data_use">数据使用同意</option>
        </select>
        <input class="input" name="version" value="v2026.1" style="max-width:90px" aria-label="同意书版本">
        <button class="btn small" type="submit">记录同意</button>
      </form>` : '<div class="tiny muted" style="margin-top:4px">知情同意由考务维护。</div>'}
      <div class="tiny muted" style="margin-top:4px">${esc(consent.note || '知情同意有效期 365 天；未成年 SP 还需监护人同意。')}</div>
    </section>`;
  const certRows = (state.sp.certifications || []).slice(0, 6);
  const approvalRows = (state.sp.approvals || []).slice(0, 6);
  const historyHtml = `
    <section class="card">
      <div class="card-title">认证与批准历史<span class="sub">只追加 · 绑定 persona_version</span></div>
      <div class="tiny muted" style="margin-bottom:5px">SP 认证</div>
      ${certRows.length ? certRows.map((row) => `
        <div class="version-row">
          <span class="k">${esc(fmtTime(row.created_at))}</span>
          <span class="v">${row.status === 'certified' ? '<span class="pill teal">通过</span>' : '<span class="pill red">未通过</span>'}${esc((row.persona_version || '').slice(7, 19))} · 红队 ${num(row.red_team.passed_cases, 0)}/${num(row.red_team.case_count, 0)} · 泄露 ${num(row.red_team.leak_count, 0)} · ${esc(row.actor || '—')}</span>
        </div>`).join('') : '<div class="tiny muted">尚无认证记录。</div>'}
      <div class="sep"></div>
      <div class="tiny muted" style="margin-bottom:5px">人格卡批准</div>
      ${approvalRows.length ? approvalRows.map((row) => `
        <div class="version-row">
          <span class="k">${esc(fmtTime(row.created_at))}</span>
          <span class="v">${row.decision === 'approved' ? '<span class="pill teal">批准</span>' : '<span class="pill red">拒绝</span>'}${esc(row.scope === 'L2' ? '可进模拟考' : '仅练习')} · ${esc((row.persona_version || '').slice(7, 19))} · ${esc(row.actor || '—')}</span>
        </div>`).join('') : '<div class="tiny muted">尚无批准记录。</div>'}
    </section>`;
  const verifierHtml = verify ? `
    <div class="verify-panel">
      <div class="verify-item ${verify.persona >= 0.85 ? 'pass' : 'fail'}"><div class="k">人格保真 · 0.35</div><div class="v">${num(verify.persona, 2)}</div></div>
      <div class="verify-item ${verify.boundary >= 0.85 ? 'pass' : 'fail'}"><div class="k">信息边界 · 0.50</div><div class="v">${num(verify.boundary, 2)}</div></div>
      <div class="verify-item ${verify.clinical >= 0.85 ? 'pass' : 'fail'}"><div class="k">临床合理 · 0.15</div><div class="v">${num(verify.clinical, 2)}</div></div>
    </div>
    <div class="verify-total"><span>总分 ${num(verify.total, 3)} · ${verify.passed ? '<span style="color:var(--teal)">放行</span>' : '<span style="color:var(--red)">拦截 / 回退</span>'}</span><b>${num(verify.total, 3)}</b></div>
    ${verify.violations && verify.violations.length ? `<div class="violation">${verify.violations.map((v) => esc((v.kind || '') + '：' + (v.entities || []).join(', '))).join('<br>')}</div>` : ''}
    ${session.degraded ? '<div class="violation">本轮触发降级：已回退安全模板，未放行低分回应。</div>' : ''}
  ` : '<div class="tiny muted">发送第一句后显示独立验证器结果。</div>';

  $('#view').innerHTML = `
    <div class="view-head">
      <div>
        <h2>SP 演练</h2>
        <p>“说什么”由确定性状态机维护信息槽；“怎么说”交给生成器；独立验证器只做三件事：人格保真、信息边界、临床合理。</p>
      </div>
      <div class="btn-row">
        <select class="input" id="sp-station" style="max-width:320px" aria-label="选择考站">${stationOptions}</select>
        <button class="btn" data-action="sp-new">开始新会话</button>
      </div>
    </div>
    <div class="sp-layout">
      <div style="display:flex;flex-direction:column;gap:12px">
        <section class="card">
          <div class="persona-name">${esc(p.name || '模拟患者')}</div>
          <div class="persona-meta">
            <span class="pill">${esc(p.age || '—')} 岁</span>
            <span class="pill">${p.gender === 'male' ? '男性' : '女性'}</span>
            <span class="pill">${esc(p.occupation || '—')}</span>
          </div>
          <div class="dim-list">
            <div class="dim-row"><span>情绪基调</span><span>${esc(dims.tone || '—')}</span></div>
            <div class="dim-row"><span>情绪唤起阈值</span><span>${esc(dims.emotion_threshold ?? '—')}</span></div>
            <div class="dim-row"><span>语言熟练度</span><span>${esc(dims.language || '—')}</span></div>
            <div class="dim-row"><span>病史记忆</span><span>${esc(dims.memory || '—')}</span></div>
            <div class="dim-row"><span>认知混乱度</span><span>${esc(dims.cognition ?? 0)}</span></div>
            <div class="dim-row"><span>披露阈值</span><span>${esc(dims.disclosure || '—')}</span></div>
          </div>
          <div class="sep"></div>
          <div class="tiny muted">禁用话术：${(p.forbidden || []).map(esc).join(' · ')}</div>
        </section>
        ${certHtml}
        ${approvalHtml}
        ${consentHtml}
        ${historyHtml}
        <section class="card">
          <div class="card-title">阶段<span class="sub">状态机驱动</span></div>
          <div class="phase-steps">
            ${phaseOrder.map((ph, i) => {
              const idx = phaseOrder.findIndex((x) => x.phase === currentPhase);
              const cls = ph.phase === currentPhase ? 'current' : (i < idx ? 'done' : '');
              return `<div class="phase-step ${cls}"><span class="idx">0${i + 1}</span>${esc(ph.label)}</div>`;
            }).join('')}
          </div>
          <div class="sep"></div>
          <div class="card-title">信息槽<span class="sub">${session ? session.released_slots.length : 0} / ${session ? session.slot_total : (persona.slots || []).length} 已释放</span></div>
          <div class="slot-list">${slots}</div>
        </section>
      </div>
      <div style="display:flex;flex-direction:column;gap:12px">
        <section class="card chat-card">
          <div class="chat-head">
            <h3>与模拟患者对话</h3>
            <div class="btn-row">
              <span class="tiny muted">${session ? esc(session.phase_label) : '未开始'} · turn ${session ? session.turn : 0} · 信任 ${session ? session.trust : 0}</span>
              ${session && !finished ? '<button class="btn small amber" data-action="sp-finish">结束练习并生成反馈</button>' : ''}
              ${finished ? '<span class="pill teal">已生成反馈</span>' : ''}
            </div>
          </div>
          <div class="chat-log" id="chat-log">
            ${messages.map((m) => `<div class="msg ${m.role}"><div class="who">${m.role === 'patient' ? 'SP' : '考生'}</div>${esc(m.text)}</div>`).join('')}
          </div>
          ${guidance ? `<div class="tiny" style="padding:8px 15px;border-top:1px solid var(--border-soft);color:var(--amber)">阶段末自然引导：本阶段仍有 ${(guidance.missing_items || []).length} 个关键评分项未被问及，SP 已提示；可继续追问，或再次尝试推进阶段。</div>` : ''}
          ${finished ? '<div class="tiny muted" style="padding:12px 15px;border-top:1px solid var(--border-soft)">本次练习已生成反馈。开始新会话可再练一次。</div>' : `
          <div class="chat-suggestions">
            ${['您哪里不舒服？', '疼多久了？', '有没有高血压？', '我理解您很担心', '我要给您听诊一下'].map((t) => `<button class="btn small ghost" data-action="sp-suggest" data-text="${dataAttr(t)}">${esc(t)}</button>`).join('')}
          </div>
          <form class="chat-input" id="sp-form">
            <input class="input" id="sp-input" maxlength="500" placeholder="以考生身份提问……" autocomplete="off">
            <button class="btn primary" type="submit">发送</button>
          </form>`}
        </section>
        <section class="card">
          <div class="card-title">独立验证器<span class="sub">信息边界权重 0.50 · 不通过则回退模板</span></div>
          ${verifierHtml}
          <div class="sep"></div>
          <div class="tiny muted">生成器只拿到“本轮允许说出的槽内容 + 人格卡 + 最近 3 轮上下文”；验证器看不到完整对话历史，避免上下文污染导致自我确认。</div>
        </section>
      </div>
    </div>
    ${session && session.report ? practiceReportHtml(session.report) : ''}
  `;
  const log = $('#chat-log');
  if (log) log.scrollTop = log.scrollHeight;
  const form = $('#sp-form');
  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = $('#sp-input');
      const text = (input.value || '').trim();
      if (!text) return;
      await sendSpMessage(text);
    });
  }
  const stationSelect = $('#sp-station');
  if (stationSelect) {
    stationSelect.addEventListener('change', async (e) => {
      state.spBundleId = e.target.value;
      state.sp = null;
      await viewSP(e.target.value);
    });
  }
  const approvalForm = $('#sp-approval-form');
  if (approvalForm) {
    approvalForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = new FormData(approvalForm);
      try {
        const result = await api('/api/sp/persona-approval', {
          method: 'POST',
          body: {
            bundle_id: state.sp && state.sp.persona ? state.sp.persona.bundle_id : null,
            decision: data.get('decision'),
            scope: data.get('scope'),
            disclosure_approved: data.get('disclosure_approved') === 'on',
            forbidden_approved: data.get('forbidden_approved') === 'on',
            trial_approved: data.get('trial_approved') === 'on',
            note: String(data.get('note') || '').trim(),
            actor: state.reviewer || '考官',
          },
        });
        toast(result.status && result.status.approved ? '人格卡已批准，可开放 SP 练习' : '已记录人格卡批准结论');
        await viewSP(state.spBundleId);
      } catch (err) {
        toast(err.message, true);
      }
    });
  }
  const consentForm = $('#sp-consent-form');
  if (consentForm) {
    consentForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = new FormData(consentForm);
      try {
        await api('/api/sp/consent', {
          method: 'POST',
          body: {
            sp_name: String(data.get('sp_name') || '').trim(),
            sp_role: data.get('sp_role'),
            consent_type: data.get('consent_type'),
            version: String(data.get('version') || '').trim(),
            bundle_id: state.sp && state.sp.persona ? state.sp.persona.bundle_id : null,
          },
        });
        toast('已记录 SP 知情同意');
        await viewSP(state.spBundleId);
      } catch (err) {
        toast(err.message, true);
      }
    });
  }
}

function practiceReportHtml(report) {
  const domains = report.domains || [];
  const domainMax = Math.max(1, ...domains.map((d) => d.score || 0));
  const deductions = (report.deductions || []).map((u) => {
    const ev = u.evidence || {};
    const hasEvidence = ev.quote && ev.evidence_kind !== 'absence';
    return `<div class="fb-card">
      <div class="fb-head">
        <h4>${u.critical ? '<span class="pill badge-critical">关键</span> ' : ''}${esc(u.item_title)} <span class="mono tiny muted">${esc(u.item_id)}</span></h4>
        <span class="pill ${u.score === 0 ? 'red' : 'amber'}">${esc(u.score_label)}</span>
      </div>
      <div class="fb-part"><div class="lab">① 证据</div><div class="val">${hasEvidence ? `${mmss(ev.start_ms)} · ${esc(ev.quote)}` : '本段对话未观察到相关询问（缺席证据）'}</div></div>
      <div class="fb-part"><div class="lab">② 行为</div><div class="val">${esc(u.behavior)}</div></div>
      <div class="fb-part alt"><div class="lab">③ 替代</div><div class="val">${esc(u.alternative || '下次可追加一句具体提问，覆盖该评分项后再进入下一环节。')}</div></div>
    </div>`;
  }).join('');
  const uncovered = (report.uncovered || []).map((u) => `<span class="pill amber">${esc(u.item_title)}</span>`).join('');
  const strengths = (report.strengths || []).map((u) => `<span class="pill teal">${esc(u.item_title)}</span>`).join('');
  const retry = report.retry_plan || { items: [] };
  const retryRows = (retry.items || []).map((r, index) => `
    <div class="version-row">
      <span class="k">${index + 1}. ${r.critical ? '<span class="pill red">关键</span> ' : ''}${esc(r.item_title)}</span>
      <span class="v">${esc(r.focus || '按评分项档位重新演练')}${r.evidence_at_ms === null || r.evidence_at_ms === undefined ? '' : ` · 回看 ${mmss(r.evidence_at_ms)}`}</span>
    </div>`).join('');
  const passed = report.total !== null && report.total >= report.pass_line;
  return `
    <section class="card" style="margin-top:16px">
      <div class="card-title">练习反馈<span class="sub">形成性 · 不计入成绩</span></div>
      <div class="grid c4" style="margin-bottom:14px">
        <div class="kpi ${passed ? 'teal' : 'amber'}"><div class="k">全站模拟分</div><div class="v">${num(report.total, 1)}</div><div class="d">及格线 ${num(report.pass_line, 0)} · ${passed ? '达标' : '继续练习'}</div></div>
        <div class="kpi blue"><div class="k">评分项覆盖</div><div class="v">${pct(report.coverage, 0)}</div><div class="d">${report.covered} / ${report.items_scored} 项有对话证据</div></div>
        <div class="kpi amber"><div class="k">需改进项</div><div class="v">${(report.deductions || []).length}</div><div class="d">含关键项 ${(report.deductions || []).filter((x) => x.critical).length}</div></div>
        <div class="kpi red"><div class="k">未覆盖项</div><div class="v">${report.uncovered_count}</div><div class="d">未提问或未触发对应评分项</div></div>
      </div>
      ${report.coverage < 0.5 ? `<div class="alert"><b>样本不足，分数仅作覆盖度参考</b><p>本次仅覆盖 ${pct(report.coverage, 0)} 的评分项，未覆盖项按 0 计入全站模拟分。建议完成完整问诊后再解读分数。</p></div>` : ''}
      ${report.unscored_count ? `<div class="alert"><b>${report.unscored_count} 项 AI 未判定</b><p>AI 拒答或触发限流，这些项不计入分数与扣分项；建议复练或请教师复核。</p></div>` : ''}
      ${report.guidance && report.guidance.count ? `<div class="alert"><b>SP 阶段末引导 ${report.guidance.count} 次</b><p>${esc(report.guidance.note || '')}</p></div>` : ''}
      <div class="grid c1-2">
        <div>
          <div class="card-title">能力域画像</div>
          <div class="domain-bars">
            ${domains.map((d) => `
              <div class="domain-row">
                <span class="bar-label">${esc(d.domain)}</span>
                <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, (d.score || 0) / domainMax * 100)}%"></div></div>
                <span class="bar-count">${num(d.score, 0)}</span>
              </div>`).join('')}
          </div>
          <div class="sep"></div>
          <div class="card-title" style="margin-bottom:5px">优势项</div>
          <div class="strength-list">${strengths || '<span class="tiny muted">暂无</span>'}</div>
          ${uncovered ? `<div class="sep"></div><div class="card-title" style="margin-bottom:5px">未覆盖</div><div class="strength-list">${uncovered}</div>` : ''}
        </div>
        <div>
          <div class="card-title">重练清单<span class="sub">关键项优先 · 共 ${retry.count || 0} 项</span></div>
          ${retryRows || '<div class="tiny muted">本次没有需要重练的扣分项。</div>'}
          ${retry.opening_tip ? `<div class="tiny" style="margin-top:6px"><span class="pill blue">先练这句</span> ${esc(retry.opening_tip)}</div>` : ''}
          ${retry.note ? `<div class="tiny muted" style="margin-top:4px">${esc(retry.note)}</div>` : ''}
          <div class="sep"></div>
          <div class="card-title">逐项反馈<span class="sub">证据 → 行为 → 替代话术</span></div>
          ${deductions || '<div class="empty">本次没有明显扣分项。</div>'}
        </div>
      </div>
      <div class="sep"></div>
      <div class="feedback-rule">${esc(report.feedback_rule)}</div>
      ${report.feedback_quality ? `<div class="tiny" style="margin-top:6px">
        <span class="pill ${report.feedback_quality.all_have_alternative ? 'teal' : 'amber'}">替代话术 ${report.feedback_quality.with_alternative}/${report.feedback_quality.checked}</span>
        <span class="pill">时间戳 ${report.feedback_quality.with_timestamp}</span>
        ${report.feedback_quality.repaired ? `<span class="pill amber">兜底修复 ${report.feedback_quality.repaired}</span>` : ''}
        ${report.feedback_quality.repaired_evidence ? `<span class="pill amber">证据兜底 ${report.feedback_quality.repaired_evidence}</span>` : ''}
      </div>` : ''}
      <div class="tiny muted" style="margin-top:6px">${esc(report.disclaimer)}</div>
    </section>`;
}

async function sendSpMessage(text) {
  if (!state.sp || !state.sp.session) return;
  try {
    const data = await api(`/api/sp/sessions/${encodeURIComponent(state.sp.session.session_id)}/message`, {
      method: 'POST',
      body: { text },
    });
    state.sp.session = data;
    renderSP();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- quality ---------- */
function fairPill(status) {
  if (status === 'pass') return '<span class="pill teal">通过</span>';
  if (status === 'fail') return '<span class="pill red">超阈值</span>';
  if (status === 'observing') return '<span class="pill amber">观察中</span>';
  return '<span class="pill muted">样本不足</span>';
}

function abSectionHtml(ab) {
  const groups = ab.groups || [];
  const c = ab.comparison || {};
  const teacherPass = c.teacher_hours_reduction !== null && c.teacher_hours_reduction >= c.teacher_target;
  const nearPass = c.near_improvement !== null && c.near_improvement >= c.near_target;
  return `
    <div class="card">
      <div class="card-title">A/B 对照<span class="sub">AI 辅助组 vs 纯人工组</span></div>
      <div class="tbl-wrap" style="margin-bottom:10px">
        <table>
          <thead><tr><th>组别</th><th class="num">场次</th><th class="num">评分项</th><th class="num">平均分</th><th class="num">通过率</th><th class="num">QWK</th><th class="num">边缘一致</th><th class="num">单条耗时</th><th class="num">工时</th></tr></thead>
          <tbody>
            ${groups.map((g) => `<tr>
              <td>${esc(g.name)}</td>
              <td class="num">${g.attempts}</td>
              <td class="num">${g.items}</td>
              <td class="num">${num(g.mean_score, 1)}</td>
              <td class="num">${pct(g.pass_rate, 0)}</td>
              <td class="num">${num(g.qwk, 3)}</td>
              <td class="num">${pct(g.near_cutoff_agreement, 0)}</td>
              <td class="num">${num(g.avg_seconds_per_item, 1)}s</td>
              <td class="num">${num(g.teacher_hours, 2)}h</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>
      <div class="version-row"><span class="k">教师评分工时下降</span><span class="v">${pct(c.teacher_hours_reduction, 1)} ${teacherPass ? '<span class="pill teal">达标</span>' : '<span class="pill red">未达标</span>'}</span></div>
      <div class="version-row"><span class="k">边缘案例一致率提升</span><span class="v">${c.near_improvement === null ? '—' : num(c.near_improvement * 100, 1) + 'pp'} ${nearPass ? '<span class="pill teal">达标</span>' : '<span class="pill red">未达标</span>'}</span></div>
      <div class="version-row"><span class="k">QWK 差值</span><span class="v">${num(c.qwk_delta, 3)}</span></div>
      <div class="version-row"><span class="k">平均分差值</span><span class="v">${num(c.score_delta, 1)}</span></div>
      ${c.score_delta !== null && Math.abs(c.score_delta) > 10 ? `<div class="alert" style="margin-top:8px"><b>分组基线不平衡</b><p>两组平均分差 ${num(c.score_delta, 1)} 分。教师工时与 QWK 结论需经随机化或协变量校正后再解读。</p></div>` : ''}
      <div class="tiny muted" style="margin-top:6px">${esc(ab.note)}</div>
    </div>`;
}

function fairnessSectionHtml(fairness) {
  const dims = fairness.dimensions || [];
  const editDims = (fairness.edit_rate || {}).dimensions || [];
  const acc = fairness.feedback_accessibility || {};
  const accGap = fairness.feedback_accessibility_gap || {};
  const accLabel = {
    pass: '达标',
    fail: '未达标',
    insufficient: '样本不足',
    unmeasured: '未采集',
  }[acc.status] || '未采集';
  const accClass = acc.status === 'pass' ? 'teal' : acc.status === 'fail' ? 'red' : acc.status === 'insufficient' ? 'amber' : 'muted';
  const exp = acc.experience || {};
  const expLine = [['usefulness', '学生实用性'], ['realism', '真实感']].map(([key, label]) => {
    const item = exp[key];
    if (!item) return '';
    const cls = item.status === 'pass' ? 'teal' : item.status === 'fail' ? 'red' : item.status === 'insufficient' ? 'amber' : 'muted';
    return `<span>${label} <b>${item.average === null ? '—' : num(item.average, 1)}</b> / 10 <span class="pill ${cls}">${item.count} 份 · 目标 ≥ ${num(item.target, 1)}</span></span>`;
  }).join('');
  return `
    <div class="card">
      <div class="card-title">公平性监测<span class="sub">阈值 ≤0.15 SD · 编辑率 ≤5pp</span></div>
      ${dims.map((d) => `
        <div class="version-row"><span class="k">${esc(d.label)}</span><span class="v">${d.gap_sd === null ? '样本不足' : num(d.gap_sd, 3) + ' SD'} ${fairPill(d.status)}</span></div>
        <div class="tiny muted">${(d.groups || []).map((g) => `${esc(g.name)} n=${g.n} 均分 ${num(g.mean, 1)}`).join(' · ')}</div>
        ${d.status === 'fail' ? `<div class="violation">纠正路径：${esc(d.corrective_action)}</div>` : ''}
      `).join('')}
      <div class="sep"></div>
      <div class="card-title" style="margin-bottom:5px">评分员编辑率差异</div>
      ${editDims.map((d) => `
        <div class="version-row"><span class="k">${esc(d.label)}</span><span class="v">${d.gap_pp === null ? '样本不足' : num(d.gap_pp, 1) + 'pp'} ${fairPill(d.status)}</span></div>
        <div class="tiny muted">${(d.groups || []).map((g) => `${esc(g.group)} ${pct(g.edit_rate, 0)} (n=${g.n})`).join(' · ')}</div>
        ${d.status === 'fail' ? `<div class="violation">纠正路径：${esc(d.corrective_action)}</div>` : ''}
      `).join('')}
      <div class="sep"></div>
      <div class="tiny muted">反馈可及性：<span class="pill ${accClass}">${accLabel}</span> ${esc(acc.note || '')}</div>
      ${expLine ? `<div class="attempt-strip" style="margin-top:6px">${expLine}</div>` : ''}
      <div class="card-title" style="margin:10px 0 5px">反馈可及性组间差<span class="sub">≤ ${num(accGap.threshold, 1)} 分（1–5 分自评）</span></div>
      ${(accGap.dimensions || []).map((d) => `
        <div class="version-row"><span class="k">${esc(d.label)}</span><span class="v">${d.gap === null ? '样本不足' : num(d.gap, 2) + ' 分'} ${fairPill(d.status)}</span></div>
        <div class="tiny muted">${(d.groups || []).map((g) => `${esc(g.name)} n=${g.n} 均分 ${num(g.mean, 1)}`).join(' · ')}</div>
        ${d.status === 'fail' ? `<div class="violation">纠正路径：${esc(d.corrective_action)}</div>` : ''}
      `).join('')}
      <div class="tiny muted" style="margin-top:4px">${esc(fairness.note)}</div>
    </div>`;
}

function asrQualityHtml(quality) {
  const latest = quality && quality.latest;
  if (!latest) {
    return '<div class="tiny muted">尚未采集 ASR 词错率样本。用同一段参考文本与 ASR 输出计算，医学专名目标 ≤ 8%。</div>';
  }
  const m = latest.metrics || {};
  const metricLabel = m.metric === 'medical_term_error_rate' ? '医学专名错误率' : 'Token 错误率';
  const recent = (quality.recent || []).slice(1, 4).map((item) => `
    <div class="version-row"><span class="k">${esc(item.label)}</span><span class="v">${pct(item.metrics.error_rate, 1)} ${fairPill(item.metrics.status)}</span></div>
  `).join('');
  return `
    <div class="version-row"><span class="k">最近：${esc(latest.label)}</span><span class="v">${pct(m.error_rate, 1)} ${fairPill(m.status)}</span></div>
    <div class="tiny muted">${metricLabel} · 阈值 ≤ ${pct(m.threshold, 0)} · 参考 ${m.reference_tokens} tokens · 编辑距离 ${m.edit_distance}</div>
    ${m.medical_term_error_rate !== null && m.medical_term_error_rate !== undefined ? `<div class="tiny muted">医学专名召回 ${pct(m.medical_term_recall, 1)}${(m.missing_terms || []).length ? ` · 未识别：${esc(m.missing_terms.join('、'))}` : ''}</div>` : ''}
    <div class="tiny muted">${esc(latest.actor || '系统')} · ${fmtTime(latest.created_at)} · 参考哈希 ${esc((latest.reference_hash || '').slice(0, 18))}…</div>
    ${recent ? `<div class="sep"></div><div class="tiny muted">最近样本</div>${recent}` : ''}
  `;
}

function selfCheckHtml(selfCheck) {
  if (!selfCheck) {
    return '<div class="tiny muted">尚未运行治理自检。自检会检查 QWK、证据缺口、亚组差异、版本戳与教师工时。</div>';
  }
  return `
    <div class="attempt-strip" style="margin-bottom:6px"><span>最近 <b>${fmtTime(selfCheck.created_at)}</b></span><span>${esc(selfCheck.trigger_summary)}</span>${selfCheck.applied ? '<span class="pill red">已自动降级</span>' : '<span class="pill muted">未触发降级</span>'}</div>
    ${(selfCheck.checks || []).map((c) => `
      <div class="version-row"><span class="k">${esc(c.label)}</span><span class="v">${c.value === null ? '—' : (c.key === 'integrity_gap' ? pct(c.value, 1) : c.key === 'subgroup_gap' ? num(c.value, 3) + ' SD' : c.key === 'teacher_hours' ? pct(c.value, 1) : num(c.value, 3))} ${fairPill(c.status)}</span></div>
      ${c.key === 'integrity_gap' && (c.stations || []).some((s) => s.breach) ? `<div class="tiny muted" style="margin:-2px 0 6px 0">超标考站：${(c.stations || []).filter((s) => s.breach).map((s) => `${esc(s.title || s.station_id || s.bundle_id)}（${s.gap_attempts}/${s.attempts}）`).join('、')}</div>` : ''}
      ${c.key === 'subgroup_gap' && (c.streak_weeks || []).length ? `<div class="tiny muted" style="margin:-2px 0 6px 0">连续超标周：${esc((c.streak_weeks || []).join('、'))}</div>` : ''}
    `).join('')}
    ${(selfCheck.changes || []).length ? `<div class="violation">已降级 ${selfCheck.changes.length} 个评分项；需重新校准后回升。</div>` : ''}
  `;
}

function p3SectionHtml(gov, gates) {
  const g3 = (gates || []).find((g) => g.id === 'G3');
  const stage = gov.release_stage || 'G0_offline';
  const stageLabel = {
    G0_offline: 'G0 离线副驾',
    G1_practice: 'G1 练习沙箱',
    G2_simulation: 'G2 模拟考',
    G3_limited: 'G3 有限高利害',
  }[stage] || stage;
  const switchDrill = gov.switch_drill;
  const appeal = gov.appeal_drills || {};
  return `
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">P3 治理准备<span class="sub">有限高利害 · 一次只放开一档</span></div>
      <div class="attempt-strip">
        <span>当前阶段 <b>${esc(stageLabel)}</b></span>
        <span>终止开关 <b>${gov.emergency_stop ? '<span style="color:var(--red)">已激活</span>' : '正常'}</b></span>
        ${g3 ? `<span>G3 闸门 ${gateStatusPill(g3.status)}</span>` : ''}
        ${stage === 'G3_limited' ? '<span class="pill teal">L3 非关键项已开放 AI 建议</span>' : '<span class="pill muted">L3 仅证据提取</span>'}
      </div>
      <div class="grid c2" style="margin-top:12px">
        <div>
          <div class="version-row"><span class="k">终止开关演练</span><span class="v">${switchDrill ? (switchDrill.passed ? '<span class="pill teal">通过</span>' : '<span class="pill red">未通过</span>') + ` · ${num(switchDrill.duration_ms, 0)}ms` : '<span class="pill muted">未演练</span>'}</span></div>
          <div class="version-row"><span class="k">申诉流程演练</span><span class="v">${appeal.passed || 0} / 2 次通过</span></div>
          <div class="version-row"><span class="k">伦理与数据合规</span><span class="v">${gov.ethics_approved ? '<span class="pill teal">已批准</span>' : '<span class="pill red">未批准</span>'}</span></div>
          <div class="version-row"><span class="k">学术委员会审议</span><span class="v">${gov.academic_committee_approved ? '<span class="pill teal">已审议</span>' : '<span class="pill red">未审议</span>'}</span></div>
          <div class="version-row"><span class="k">模型卡与数据表</span><span class="v">${gov.model_card_public && gov.data_sheet_public ? '<span class="pill teal">已公开</span>' : '<span class="pill red">未公开</span>'}</span></div>
        </div>
        <div>
          <div class="btn-row">
            <button class="btn small" data-action="termination-drill">终止开关演练</button>
            <button class="btn small" data-action="appeal-drill">申诉流程演练</button>
            <button class="btn small" data-action="approve-ethics">登记伦理批准</button>
            <button class="btn small" data-action="approve-committee">登记学术委员会审议</button>
            <button class="btn small" data-action="publish-model-card">公开模型卡与数据表</button>
          </div>
          <div class="sep"></div>
          <div class="btn-row">
            <button class="btn primary" data-action="release-g3" ${stage === 'G3_limited' ? 'disabled' : ''}>升级到 G3 有限高利害</button>
            <span class="tiny muted">${stage === 'G3_limited' ? '已启用；L3 仅非关键 utterance/action 项可建议，且全部人工确认。' : '需 G0–G2 全部通过且 G3 六项指标达标。'}</span>
          </div>
        </div>
      </div>
      <div class="sep"></div>
      <div class="tiny muted">G3 放行条件：QWK ≥ 0.80 · 关键项漏判 = 0 · 终止开关演练通过 · 申诉演练 ≥ 2 次 · 伦理与数据合规批准 · 模型卡与数据表公开。</div>
      <div class="tiny muted" style="margin-top:4px">${esc((gov.model_card || {}).card_id ? '模型卡：' + gov.model_card.card_id : '')}</div>
    </div>`;
}

function susSectionHtml(sus) {
  const items = sus.items || [];
  const count = sus.count || 0;
  const options = ['', '1', '2', '3', '4', '5']
    .map((v) => v === ''
      ? '<option value="">请选择</option>'
      : `<option value="${v}">${v}</option>`)
    .join('');
  return `
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">SUS 可用性采集<span class="sub">10 项 · 1–5 分 · 自动折算 0–100</span></div>
      <div class="attempt-strip" style="margin-bottom:8px">
        <span>样本 <b>${count}</b></span>
        <span>均值 <b>${num(sus.average, 1)}</b></span>
        <span>最近 ${sus.latest_at ? fmtTime(sus.latest_at) : '—'}</span>
        ${count ? '' : '<span class="pill amber">尚无真实问卷</span>'}
      </div>
      ${count ? `<div class="tiny muted" style="margin-bottom:8px">最近分数：${(sus.recent_scores || []).map((v) => num(v, 1)).join(' · ')}</div>`
        : '<div class="tiny muted" style="margin-bottom:8px">尚未采集真实问卷，G1 暂用旧版配置值；至少录入一份问卷后自动切换为样本均值。</div>'}
      <details>
        <summary class="small" style="cursor:pointer;color:var(--teal)">录入一份 SUS 问卷</summary>
        <form id="sus-form" style="margin-top:10px">
          <label class="field">受访者标签
            <input class="input" name="participant_label" maxlength="40" placeholder="如：2026 春 练习学生">
          </label>
          <div class="grid c2" style="margin-top:10px">
            ${items.map((text, i) => `
              <label class="field">${i + 1}. ${esc(text)}
                <select class="input" name="sus_${i + 1}" required>${options}</select>
              </label>`).join('')}
          </div>
          <div class="btn-row" style="margin-top:12px">
            <button class="btn primary" type="submit">提交问卷</button>
            <span class="tiny muted">提交后写入审计链并计入 G1 的 SUS 均值。</span>
          </div>
        </form>
      </details>
    </div>`;
}

function calibrationSectionHtml(cal) {
  const items = cal.items || [];
  const bands = cal.confidence_bands || [];
  const lowItems = items.slice(0, 6);
  return `
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">金标准校准<span class="sub">导入人工标签 · 重算 QWK</span></div>
      <div class="attempt-strip" style="margin-bottom:8px">
        <span>金标准 <b>${cal.gold_count || 0}</b> / AI ${cal.ai_count || 0}</span>
        <span>覆盖率 <b>${pct(cal.coverage, 0)}</b></span>
        <span>AI–金标准 QWK <b>${num(cal.qwk_ai, 3)}</b></span>
        <span>建议置信下限 <b>${cal.recommended_confidence_floor == null ? '样本不足' : num(cal.recommended_confidence_floor, 2)}</b></span>
      </div>
      ${lowItems.length ? `<div class="version-row"><span class="k">一致率最低项</span><span class="v">${lowItems.map((i) => `${esc(i.item_id)} ${pct(i.exact_agreement, 0)} (n=${i.n})`).join(' · ')}</span></div>` : ''}
      <div class="tiny muted">置信扫描：${bands.length ? bands.map((b) => `≥${num(b.floor, 1)} ${b.accuracy === null ? '—' : pct(b.accuracy, 0)}`).join(' · ') : '样本不足'}</div>
      <details style="margin-top:8px">
        <summary class="small" style="cursor:pointer;color:var(--teal)">导入金标准 CSV / JSON</summary>
        <form id="calibration-form" style="margin-top:10px">
          <label class="field">文件（列：attempt_id, item_id, gold_score）
            <input class="input" type="file" name="file" accept=".csv,.json" required>
          </label>
          <div class="btn-row" style="margin-top:10px">
            <button class="btn primary" type="submit">导入并重算</button>
            <span class="tiny muted">只更新 gold_score，不改最终分；导入批次写入审计链。</span>
          </div>
        </form>
      </details>
    </div>`;
}

const USER_ROLE_LABELS = {
  admin: '系统管理员',
  exam_manager: '考务管理者',
  examiner: '考官/评分员',
  auditor: '审计员',
  student: '学生',
};

function usersSectionHtml(users) {
  const roleOptions = (selected) => Object.entries(USER_ROLE_LABELS)
    .map(([value, label]) => `<option value="${value}" ${value === selected ? 'selected' : ''}>${label}</option>`)
    .join('');
  const rows = users.map((user) => `
    <tr>
      <td><div style="font-weight:600">${esc(user.display_name)}</div><div class="mono tiny muted">${esc(user.username)}</div></td>
      <td><select class="input" data-action="user-role" data-user="${esc(user.user_id)}">${roleOptions(user.role)}</select></td>
      <td>${user.active ? '<span class="pill teal">启用</span>' : '<span class="pill muted">停用</span>'}</td>
      <td class="mono tiny">${esc(user.candidate_code || '—')}</td>
      <td><div class="btn-row">
        <button class="btn small" data-action="user-toggle" data-user="${esc(user.user_id)}" data-active="${user.active ? 'false' : 'true'}">${user.active ? '停用' : '启用'}</button>
        <button class="btn small" data-action="user-reset-password" data-user="${esc(user.user_id)}">重置密码</button>
      </div></td>
    </tr>`).join('');
  return `
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">用户管理<span class="sub">角色与启停 · 仅管理员</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>姓名 / 用户名</th><th>角色</th><th>状态</th><th>考生编号</th><th>操作</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="5" class="empty">暂无用户</td></tr>'}</tbody>
        </table>
      </div>
      <div class="sep"></div>
      <div class="card-title" style="margin-bottom:6px">创建用户</div>
      <form id="user-create-form" class="grid c3">
        <label class="field">用户名<input class="input" name="username" required minlength="3" maxlength="40"></label>
        <label class="field">姓名<input class="input" name="display_name" required maxlength="40"></label>
        <label class="field">角色<select class="input" name="role">${roleOptions('examiner')}</select></label>
        <label class="field">初始密码<input class="input" type="password" name="password" required minlength="8" maxlength="128"></label>
        <label class="field">考生编号（学生必填）<input class="input" name="candidate_code" maxlength="40"></label>
        <div style="display:flex;align-items:flex-end"><button class="btn primary" type="submit">创建用户</button></div>
      </form>
    </div>`;
}

function scorecardStatus(value, target, direction) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'unmeasured';
  const current = Number(value);
  if (direction === 'le') return current <= Number(target) ? 'pass' : 'fail';
  if (direction === 'ge') return current >= Number(target) ? 'pass' : 'fail';
  return current === Number(target) ? 'pass' : 'fail';
}

function scorecardPill(status) {
  const labels = {
    pass: ['达标', 'teal'],
    warn: ['警示', 'amber'],
    fail: ['未达标', 'red'],
    insufficient: ['样本不足', 'amber'],
    unmeasured: ['未采集', 'muted'],
  };
  const item = labels[status] || ['未采集', 'muted'];
  return `<span class="pill ${item[1]}">${item[0]}</span>`;
}

function scorecardHtml(data) {
  const report = data.report || {};
  const values = ((data.gates || {}).metric_values) || {};
  const ab = ((data.ab || {}).comparison) || {};
  const fairness = data.fairness || {};
  const experience = ((fairness.feedback_accessibility || {}).experience) || {};
  const asrMetrics = (((data.asr_quality || {}).latest || {}).metrics) || {};
  const asrError = asrMetrics.medical_term_error_rate === null || asrMetrics.medical_term_error_rate === undefined
    ? asrMetrics.error_rate
    : asrMetrics.medical_term_error_rate;
  const costTargets = (((data.ai_calls || {}).cost_per_attempt || {}).targets) || [];
  const ready = ((data.feedback_latency || {}).ready) || {};
  const practiceReady = ready.practice || {};
  const examReady = ready.exam || {};
  const reviewShareRows = ((report.review_share || {}).by_risk) || [];
  const appeals = report.appeals || {};
  const review = report.review || {};
  const rows = [];
  const add = (group, label, target, display, status, note) => {
    rows.push({ group, label, target, display, status, note: note || '' });
  };

  add('技术', 'SP 对话事实错误率', '≤ 2%', pct(values.sp_fact_error, 1), scorecardStatus(values.sp_fact_error, 0.02, 'le'));
  add('技术', '人格一致性首轮通过率', '≥ 95%', pct(values.persona_pass, 0), scorecardStatus(values.persona_pass, 0.95, 'ge'));
  add('技术', '证据定位准确率', '≥ 95%', pct(values.evidence_rate, 0), scorecardStatus(values.evidence_rate, 0.95, 'ge'));
  add('技术', '端到端语音延迟 P95', '≤ 1.5 秒', values.p95_latency === null || values.p95_latency === undefined ? '—' : `${num(values.p95_latency, 2)} 秒`, scorecardStatus(values.p95_latency, 1.5, 'le'));
  add('技术', '医学专名 ASR 词错率', '≤ 8%', pct(asrError, 1), scorecardStatus(asrError, 0.08, 'le'), '取最近一次 ASR 基准样本');

  add('评分质量', 'AI–人 QWK', 'L2 ≥ 0.75 · L3 ≥ 0.80', num(report.qwk_ai, 3), scorecardStatus(report.qwk_ai, 0.75, 'ge'));
  add('评分质量', '边缘案例一致率提升', '≥ 10pp', ab.near_improvement === null || ab.near_improvement === undefined ? '—' : `${num(ab.near_improvement * 100, 1)}pp`, scorecardStatus(ab.near_improvement, 0.10, 'ge'));
  add('评分质量', '关键项漏判', '= 0', num(values.critical_miss, 0), scorecardStatus(values.critical_miss, 0, 'eq'));
  let editStatus = scorecardStatus(review.edit_rate, 0.30, 'le');
  if (editStatus === 'pass' && Number(review.edit_rate) < 0.05) editStatus = 'warn';
  add('评分质量', '人工编辑率', '5%–30%', pct(review.edit_rate, 1), editStatus, '低于 5% 警惕橡皮图章');

  costTargets.forEach((row) => {
    add('运营', `${row.label}单站成本`, `≤ ¥${num(row.target_cny, 0)}`, row.avg_cny === null || row.avg_cny === undefined ? '—' : `¥${num(row.avg_cny, 2)}`, row.status || 'unmeasured', row.attempts ? `${row.attempts} 场样本` : '');
  });
  add('运营', '教师评分工时下降', '≥ 40%', pct(ab.teacher_hours_reduction, 1), scorecardStatus(ab.teacher_hours_reduction, 0.40, 'ge'));
  add('运营', '练习反馈生成', '≤ 60 秒', practiceReady.count ? `平均 ${num(practiceReady.avg, 1)} 秒 · 及时率 ${pct(practiceReady.within_rate, 0)}` : '—', practiceReady.status || 'unmeasured', practiceReady.count ? `${practiceReady.count} 份报告` : '');
  add('运营', '考试反馈生成', '≤ 72 小时', examReady.count ? `平均 ${num(examReady.avg, 1)} 小时 · 及时率 ${pct(examReady.within_rate, 0)}` : '—', examReady.status || 'unmeasured', examReady.count ? `${examReady.count} 份成绩` : '');
  reviewShareRows.forEach((row) => {
    if (row.target === null || row.target === undefined) return;
    add('运营', `${row.risk_level} 人工复核占比`, `≤ ${pct(row.target, 0)}`, `${pct(row.share, 0)}（${row.reviewed}/${row.attempts}）`, scorecardStatus(row.share, row.target, 'le'));
  });

  const sus = data.sus || {};
  let susStatus = scorecardStatus(sus.average, 75, 'ge');
  if (susStatus === 'fail' && Number(sus.average) >= 70) susStatus = 'warn';
  add('体验与公平', 'SUS 可用性', '≥ 75（G1 放行线 70）', num(sus.average, 1), susStatus, sus.count ? `${sus.count} 份问卷` : '');
  const usefulness = experience.usefulness || {};
  const realism = experience.realism || {};
  add('体验与公平', '学生实用性评分', '≥ 7.0 / 10', num(usefulness.average, 1), usefulness.status || 'unmeasured', usefulness.count ? `${usefulness.count} 份样本` : '');
  add('体验与公平', '学生真实感评分', '≥ 7.0 / 10', num(realism.average, 1), realism.status || 'unmeasured', realism.count ? `${realism.count} 份样本` : '');
  add('体验与公平', '亚组分数差', '≤ 0.15 SD', fairness.overall_gap_sd === null || fairness.overall_gap_sd === undefined ? '—' : `${num(fairness.overall_gap_sd, 3)} SD`, scorecardStatus(fairness.overall_gap_sd, 0.15, 'le'));
  add('体验与公平', '申诉率', '< 3%', pct(appeals.appeal_rate, 1), scorecardStatus(appeals.appeal_rate, 0.03, 'le'));
  add('体验与公平', '改判率', '< 20%', pct(appeals.change_rate, 1), scorecardStatus(appeals.change_rate, 0.20, 'le'));

  const groups = [];
  rows.forEach((row) => {
    if (!groups.includes(row.group)) groups.push(row.group);
  });
  const body = groups.map((group) => {
    const groupRows = rows.filter((row) => row.group === group);
    return groupRows.map((row, index) => `
      <tr>
        ${index === 0 ? `<td rowspan="${groupRows.length}" style="font-weight:600;vertical-align:top">${esc(group)}</td>` : ''}
        <td>${esc(row.label)}${row.note ? `<div class="tiny muted">${esc(row.note)}</div>` : ''}</td>
        <td>${esc(row.target)}</td>
        <td>${esc(row.display)}</td>
        <td>${scorecardPill(row.status)}</td>
      </tr>`).join('');
  }).join('');
  return `
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">S9 验收指标总览<span class="sub">按设计文档目标自动判定 · 未采集不冒充达标</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th style="width:10%">层次</th><th>指标</th><th style="width:20%">目标</th><th style="width:24%">当前</th><th style="width:10%">状态</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
      <div class="tiny muted" style="margin-top:6px">“未采集”仅表示缺少样本，不等同于达标；“警示”表示未触发红线但存在橡皮图章或体验风险。</div>
    </div>`;
}

async function viewQuality() {
  const canManageUsers = Boolean(state.user && state.user.role === 'admin');
  const canManageBlueprint = Boolean(state.user && state.user.role === 'exam_manager');
  const [data, audit, retention, backupsData, stationData, usersData, maintenance, blueprintData, criticalData] = await Promise.all([
    api('/api/quality'),
    api('/api/audit?limit=30'),
    api('/api/retention'),
    api('/api/ops/backups'),
    api('/api/quality/stations'),
    canManageUsers ? api('/api/users') : Promise.resolve({ users: [] }),
    api('/api/rubric/maintenance'),
    api('/api/blueprint'),
    api('/api/quality/critical-misses'),
  ]);
  const backups = backupsData.backups || [];
  const stationQuality = stationData.stations || [];
  const itemQuality = data.item_quality || null;
  const users = usersData.users || [];
  const report = data.report;
  const appeals = report.appeals || {};
  const reviewShares = (report.review_share || {}).by_risk || [];
  const feedbackLatency = data.feedback_latency || {};
  const spGuidance = data.sp_guidance || {};
  const readyPractice = (feedbackLatency.ready || {}).practice || {};
  const readyExam = (feedbackLatency.ready || {}).exam || {};
  const gates = data.gates.gates;
  const stageLabels = {
    G0_offline: 'G0 离线副驾',
    G1_practice: 'G1 练习沙箱',
    G2_simulation: 'G2 模拟考',
    G3_limited: 'G3 有限高利害',
  };
  $('#stage-pill').textContent = stageLabels[(data.governance || {}).release_stage] || (data.governance || {}).release_stage || 'G0 离线副驾';
  const qwkClass = report.qwk_ai >= 0.7 ? 'teal' : report.qwk_ai >= 0.6 ? 'amber' : 'red';
  const alerts = (data.gates.alerts || []).map((a) => `
    <div class="alert ${a.level === 'danger' ? 'danger' : ''}"><b>${esc(a.title)}</b><p>${esc(a.detail)}</p></div>`).join('');
  const stationRows = stationQuality.map((s) => `
    <tr>
      <td><div style="font-weight:600">${esc(s.station_id)}</div><div class="tiny muted">${esc(s.title)}</div></td>
      <td><span class="pill ${s.is_latest ? 'teal' : 'muted'}">v${esc(s.version)}</span></td>
      <td class="num">${s.attempts}</td>
      <td class="num">${s.records}</td>
      <td class="num">${s.qwk_ai === null || s.qwk_ai === undefined ? '—' : `<span class="pill ${s.qwk_ai >= 0.7 ? 'teal' : s.qwk_ai >= 0.6 ? 'amber' : 'red'}">${num(s.qwk_ai, 3)}</span>`}</td>
      <td class="num">${num(s.qwk_human, 3)}</td>
      <td class="num">${pct(s.evidence_rate, 1)}</td>
      <td class="num">${pct(s.edit_rate, 1)}</td>
      <td class="num">${pct(s.integrity_gap_rate, 1)}</td>
      <td class="num">${s.fairness_gap_sd === null || s.fairness_gap_sd === undefined ? '—' : `<span class="pill ${s.fairness_gap_sd <= 0.15 ? 'teal' : 'red'}">${num(s.fairness_gap_sd, 2)} SD</span>`}</td>
    </tr>`).join('');
  const psychometrics = data.psychometrics || [];
  const psychoFlag = (flags) => {
    const labels = {
      sample_insufficient: ['样本不足', 'amber'],
      ceiling: ['天花板', 'amber'],
      floor: ['地板', 'red'],
      low_discrimination: ['低区分', 'red'],
      redundant_items: ['项间重复', 'amber'],
    };
    if (!flags || !flags.length) return '<span class="pill teal">正常</span>';
    return flags.map((flag) => {
      const [label, color] = labels[flag] || [flag, 'muted'];
      return `<span class="pill ${color}">${esc(label)}</span>`;
    }).join(' ');
  };
  const psychoRows = psychometrics.map((p) => {
    const alphaClass = p.alpha === null || p.alpha === undefined ? 'muted' : p.alpha >= 0.7 ? 'teal' : p.alpha >= 0.6 ? 'amber' : 'red';
    return `<tr>
      <td><div style="font-weight:600">${esc(p.station_id)}</div><div class="tiny muted">${esc(p.title)}</div></td>
      <td><span class="pill muted">v${esc(p.version)}</span></td>
      <td class="num">${p.scored_attempts}</td>
      <td class="num">${num(p.mean_total, 1)}</td>
      <td class="num">${num(p.sd_total, 1)}</td>
      <td class="num">${p.pass_rate === null ? '—' : pct(p.pass_rate, 0)}</td>
      <td class="num"><span class="pill ${alphaClass}">${p.alpha === null || p.alpha === undefined ? '样本不足' : num(p.alpha, 3)}</span></td>
      <td>${psychoFlag(p.flags)}</td>
    </tr>`;
  }).join('');
  const psychoNotes = psychometrics.filter((p) => p.low_discrimination_items.length || p.ceiling_items.length || p.floor_items.length || (p.redundant_pairs || []).length).map((p) => {
    const itemMap = Object.fromEntries((p.items || []).map((item) => [item.item_id, item]));
    const parts = [];
    if (p.low_discrimination_items.length) parts.push(`低区分：${p.low_discrimination_items.map((id) => `${esc(id)}（${num((itemMap[id] || {}).discrimination, 2)}）`).join('、')}`);
    if (p.ceiling_items.length) parts.push(`全员满分：${p.ceiling_items.map(esc).join('、')}`);
    if (p.floor_items.length) parts.push(`全员零分：${p.floor_items.map(esc).join('、')}`);
    if ((p.redundant_pairs || []).length) {
      const pairs = p.redundant_pairs;
      const shown = pairs.slice(0, 3).map((pair) => `${esc(pair.item_a)}↔${esc(pair.item_b)}（r=${num(pair.r, 2)}，n=${pair.n}）`).join('、');
      parts.push(`项间高相关：${shown}${pairs.length > 3 ? ` 等 ${pairs.length} 对` : ''}`);
    }
    return `<div class="version-row"><span class="k">${esc(p.station_id)}</span><span class="v">${parts.join(' · ')}</span></div>`;
  }).join('');
  const coverage = data.coverage || {};
  const flagLabels = { ceiling: '天花板', floor: '地板', low_discrimination: '低区分度', sample_insufficient: '样本不足', other: '其他异常' };
  const decisionLabels = { rubric_revision: '修订评分表', sp_retraining: 'SP 复训', teaching: '教学改进', monitor: '继续观察', no_action: '暂不处理' };
  const stationReviews = data.station_reviews || [];
  const reviewRows = stationReviews.map((r) => `
    <div class="version-row">
      <span class="k">${esc(r.station_id)} · ${esc(flagLabels[r.flag] || r.flag)}</span>
      <span class="v">${esc(decisionLabels[r.decision] || r.decision)} · ${esc(r.note)} · ${esc(r.actor)} · ${fmtTime(r.created_at)}</span>
    </div>`).join('');
  const stationReviewOptions = psychometrics
    .map((p) => `<option value="${dataAttr(p.station_id)}">${esc(p.station_id)} · ${esc(p.title)}</option>`)
    .join('');
  const hintImpact = data.hint_impact || {};
  const hintRows = (hintImpact.groups || []).map((g) => `
    <div class="version-row">
      <span class="k">${esc(g.label)}</span>
      <span class="v">${g.attempts} 场 · 均分 ${num(g.mean_total, 1)} · SD ${num(g.sd_total, 1)} · 通过率 ${g.pass_rate === null ? '—' : pct(g.pass_rate, 0)}</span>
    </div>`).join('');
  const coverageRows = (coverage.domains || []).map((d) => {
    const statusLabel = { ok: ['达标', 'teal'], overrepresented: ['过度重复', 'amber'], underrepresented: ['低于目标站数', 'red'], single_station: ['单站覆盖', 'muted'] }[d.status] || [d.status, 'muted'];
    return `<tr>
      <td><div style="font-weight:600">${esc(d.label)}</div><div class="mono tiny muted">${esc(d.domain)}</div></td>
      <td class="num">${d.stations}</td>
      <td class="num">${d.items}</td>
      <td class="tiny">${(d.station_ids || []).map((id) => `<span class="pill">${esc(id)}</span>`).join(' ') || '—'}</td>
      <td class="num">${pct(d.share, 1)}</td>
      <td class="num">${d.target === null ? '—' : pct(d.target, 0)}<div class="tiny muted">${d.target_stations === null || d.target_stations === undefined ? '站数未设' : `目标 ${d.target_stations} 站`}</div></td>
      <td><span class="pill ${statusLabel[1]}">${esc(statusLabel[0])}</span></td>
    </tr>`;
  }).join('');
  const uncovered = coverage.uncovered || [];
  const coverageNote = uncovered.length
    ? `<div class="alert" style="margin-top:8px"><b>未覆盖能力域</b><p>${uncovered.map((d) => `${esc(d.label)}（目标 ${pct(d.target, 0)}${d.target_stations === null || d.target_stations === undefined ? '' : ` / ${d.target_stations} 站`}）`).join('、')} 尚无站包覆盖，建议下一年度命题补充。</p></div>`
    : '<div class="tiny muted" style="margin-top:6px">六域蓝图均已覆盖。</div>';
  const blueprint = blueprintData || {};
  const bpCurrent = blueprint.current || null;
  const bpTargets = bpCurrent ? bpCurrent.domains : (blueprint.defaults || []);
  const bpStatus = bpCurrent
    ? `<span class="pill ${blueprint.stale ? 'amber' : 'teal'}">${blueprint.stale ? '沿用上一年度' : '已批准生效'}</span> ${bpCurrent.year} 年度 v${bpCurrent.version} · ${esc(bpCurrent.approved_by || '—')} · ${fmtTime(bpCurrent.approved_at)}`
    : '<span class="pill amber">未批准</span> 暂按默认六域目标计算';
  const bpShareInputs = bpTargets.map((d) => `
    <label class="tiny">${esc(d.label)} 占比<input class="input" type="number" name="share_${dataAttr(d.domain)}" min="0" max="100" step="1" value="${Math.round(Number(d.target_share || 0) * 100)}" style="max-width:78px"></label>
    <label class="tiny">目标站数<input class="input" type="number" name="stations_${dataAttr(d.domain)}" min="0" max="50" step="1" value="${d.target_stations === null || d.target_stations === undefined ? '' : d.target_stations}" placeholder="可选" style="max-width:78px"></label>`).join('');
  const bpDrafts = (blueprint.history || []).filter((b) => b.status === 'draft');
  const bpDraftForms = bpDrafts.map((b) => `
    <form class="btn-row bp-approve-form" data-blueprint="${dataAttr(b.blueprint_id)}" style="flex-wrap:wrap;gap:6px;margin-top:6px">
      <span class="tiny">草案 ${b.year} 年度 v${b.version} · ${esc(b.created_by)} · ${fmtTime(b.created_at)}</span>
      <input class="input" name="note" placeholder="批准理由（不少于 4 字）" minlength="4" maxlength="300" required style="max-width:260px">
      <button class="btn small primary" type="submit">批准生效</button>
    </form>`).join('');
  const bpHistoryRows = (blueprint.history || []).slice(0, 8).map((b) => {
    const statusLabel = { approved: ['已生效', 'teal'], draft: ['草案', 'amber'], superseded: ['已被替代', 'muted'] }[b.status] || [b.status, 'muted'];
    return `<div class="version-row">
      <span class="k">${b.year} 年度 v${b.version} <span class="pill ${statusLabel[1]}">${statusLabel[0]}</span></span>
      <span class="v">${esc(b.created_by)} 起草${b.approved_by ? ` · ${esc(b.approved_by)} 批准` : ''}${b.approval_note ? ` · ${esc(b.approval_note)}` : ''} · ${fmtTime(b.approved_at || b.created_at)}</span>
    </div>`;
  }).join('');
  const itemRows = ((itemQuality && itemQuality.items) || []).map((item) => `
    <tr>
      <td><div class="mono tiny muted">${esc(item.item_id)}</div><div style="font-weight:600">${esc(item.title)}</div></td>
      <td>${item.critical ? '<span class="pill red">关键</span>' : '<span class="muted tiny">—</span>'}</td>
      <td class="num">${item.records}</td>
      <td class="num">${item.reviewed}</td>
      <td class="num">${pct(item.edit_rate, 0)}</td>
      <td class="num">${item.gold_records ? pct(item.exact_match_rate, 0) : '—'}</td>
      <td class="num">${num(item.avg_confidence, 2)}</td>
      <td class="num">${item.discrimination === null || item.discrimination === undefined ? '—' : `<span class="pill ${item.discrimination < 0.2 ? 'red' : item.discrimination >= 0.3 ? 'teal' : 'amber'}">${num(item.discrimination, 2)}</span>`}</td>
      <td>${item.needs_review ? '<span class="pill amber">建议改写</span>' : '<span class="pill teal">可判</span>'}</td>
    </tr>`).join('');
  const maint = maintenance || { items: [], reasons: [], totals: {} };
  const maintReasonChips = (maint.reasons || []).map((r) => {
    const color = r.code === '行为锚歧义' ? 'red' : r.code === '证据不足' ? 'amber' : 'muted';
    return `<span class="pill ${color}">${esc(r.code)} ${r.count}</span>`;
  }).join(' ');
  const maintRows = (maint.items || []).map((item) => `
    <tr>
      <td><div class="mono tiny muted">${esc(item.item_id)}</div><div style="font-weight:600">${esc(item.title)}</div></td>
      <td>${item.critical ? '<span class="pill red">关键</span>' : '<span class="muted tiny">—</span>'}</td>
      <td class="num">${item.total}</td>
      <td class="num">${item.accepted}</td>
      <td class="num">${item.corrected}</td>
      <td class="num">${item.rejected}</td>
      <td class="num">${pct(item.disagreement, 0)}</td>
      <td>${item.dominant_reason ? `<span class="pill amber">${esc(item.dominant_reason)}</span>` : '<span class="muted tiny">—</span>'}</td>
      <td class="tiny">${esc(item.suggested_action || '—')}</td>
      <td>${item.needs_review ? '<span class="pill red">优先维护</span>' : '<span class="pill teal">观察</span>'}</td>
    </tr>`).join('');
  const critical = criticalData || { items: [], root_causes: [], actions: [] };
  const criticalRootLabels = Object.fromEntries((critical.root_causes || []).map((x) => [x.key, x.label]));
  const criticalActionLabels = Object.fromEntries((critical.actions || []).map((x) => [x.key, x.label]));
  const criticalRows = (critical.items || []).map((miss) => {
    const review = miss.review;
    const form = (review || !canManageBlueprint) ? '' : `<form class="btn-row critical-miss-form" data-record="${dataAttr(miss.record_id)}" style="flex-wrap:wrap;gap:6px;margin-top:6px">
      <select class="input" name="root_cause" aria-label="根因分类" style="max-width:180px">
        ${(critical.root_causes || []).map((x) => `<option value="${dataAttr(x.key)}">${esc(x.label)}</option>`).join('')}
      </select>
      <select class="input" name="action" aria-label="纠正动作" style="max-width:170px">
        ${(critical.actions || []).map((x) => `<option value="${dataAttr(x.key)}">${esc(x.label)}</option>`).join('')}
      </select>
      <input class="input" name="note" placeholder="根因说明（不少于 6 字）" minlength="6" maxlength="500" required style="max-width:260px">
      <button class="btn small primary" type="submit">记录归因</button>
    </form>`;
    return `<div class="version-row">
      <span class="k">${esc(miss.item_id)} · ${esc(miss.item_title)} ${miss.risk_level === 'L3' ? '<span class="pill red">L3</span>' : ''}</span>
      <span class="v">${esc(miss.candidate)} · AI 0 → ${miss.reference_source === 'gold' ? '金标准' : '人工'} ${miss.reference_score} · ${fmtTime(miss.created_at)} ${review ? '<span class="pill teal">已归因</span>' : '<span class="pill red">待归因</span>'}</span>
      ${review ? `<div class="tiny muted">${esc(criticalRootLabels[review.root_cause] || review.root_cause)} · ${esc(criticalActionLabels[review.action] || review.action)} · ${esc(review.note)} · ${esc(review.actor)}</div>` : form}
    </div>`;
  }).join('');
  const gateCards = gates.map((gate) => `
    <div class="gate-card ${gate.is_current ? 'current' : ''} ${gate.is_locked ? 'locked' : ''}">
      <div class="gate-head">
        <div><h3>${esc(gate.id)} · ${esc(gate.name)}</h3><p>${esc(gate.scope)}</p></div>
        <div>${gateStatusPill(gate.status)}${gate.is_current ? ' <span class="pill teal">当前</span>' : ''}${gate.is_locked ? ' <span class="pill muted">未解锁</span>' : ''}</div>
      </div>
      <div class="criteria">
        ${gate.criteria.map((c) => {
          let value = '—';
          if (c.value !== null && c.value !== undefined) {
            if (c.key === 'subgroup_gap') value = num(c.value, 3) + ' SD';
            else if (c.key === 'near_improvement') value = num(c.value * 100, 1) + 'pp';
            else if (c.key === 'switch_drill' || c.key === 'ethics' || c.key === 'model_card' || c.key === 'academic_committee') value = c.value >= 1 ? '通过' : '未完成';
            else if (c.key === 'appeal_drill') value = `${c.value}/2`;
            else if (c.key === 'evidence_rate' || c.key === 'edit_rate' || c.key === 'teacher_hours' || c.key === 'sp_fact_error' || c.key === 'persona_pass') value = pct(c.value, c.value < 0.1 ? 1 : 0);
            else if (c.key === 'sus') value = num(c.value, 1);
            else if (c.key === 'critical_miss') value = num(c.value, 0);
            else if (c.key === 'critical_streak') value = `${num(c.value, 0)} 场`;
            else if (c.key === 'avg_review_seconds') value = num(c.value, 2) + 's';
            else if (c.key === 'p95_latency') value = num(c.value * 1000, 0) + ' ms';
            else value = num(c.value, 2);
          }
          return `<div class="criterion"><span>${esc(c.label)}</span><span class="val">${esc(value)}</span><span class="target">${checkMark(c.status)}<div class="tiny muted">${esc(c.target)}</div></span></div>`;
        }).join('')}
      </div>
    </div>`).join('');
  const overrides = data.overrides.length ? data.overrides.map((o) => `
    <div class="version-row"><span class="k">${esc(o.item_id)}</span><span class="v">${esc(o.authority)} · ${esc(o.reason)}</span></div>`).join('') : '<div class="tiny muted">暂无权限覆盖</div>';
  const auditRows = audit.map((ev) => `
    <div class="audit-row"><div class="act">${esc(ev.action)}</div><div class="obj">${esc(ev.actor)} · ${esc(ev.object_type)} ${esc(ev.object_id)}</div><div class="time">${fmtTime(ev.created_at)}</div></div>`).join('');
  const retentionRows = retention.by_class.map((p) => `
    <div class="version-row">
      <span class="k">${esc(p.class)} · ${esc(p.label)}</span>
      <span class="v">${p.days} 天 · ${p.count} 场次 · 到期 ${p.expired} · ${bytesText(p.bytes)}</span>
    </div>`).join('');
  const canPurge = state.user && ['admin', 'exam_manager'].includes(state.user.role);
  const provider = data.ai_provider || {};
  const asr = data.asr_provider || {};
  const anchors = data.audit_anchors || [];
  const modelCards = data.model_cards.map((c) => `
    <div class="card tight" style="margin-bottom:8px">
      <div class="card-title" style="margin-bottom:4px">${esc(c.capability)}<span class="sub">${esc(c.model_version)}</span></div>
      <div class="attempt-strip" style="margin-bottom:4px">${c.is_public ? '<span class="pill teal">已公开</span>' : '<span class="pill amber">未公开</span>'} ${c.published_at ? `<span class="tiny muted">${fmtTime(c.published_at)}</span>` : ''}</div>
      <div class="tiny muted">QWK ${num(c.metrics.qwk_ai, 3)} · 边缘案例 ${pct(c.metrics.near_cutoff_agreement, 0)} · 证据 ${pct(c.metrics.evidence_rate, 0)} · 编辑率 ${pct(c.metrics.edit_rate, 0)} · 亚组差 ${num(c.metrics.subgroup_gap_sd, 2)} SD · n=${c.metrics.sample_size}</div>
      ${(c.metrics.item_disagreement_hotspots || []).length ? `<div class="tiny muted">分歧热点：${c.metrics.item_disagreement_hotspots.slice(0, 5).map((h) => `${esc(h.item_title || h.item_id)} ${pct(h.rate, 0)}（n=${h.reviews}）`).join(' · ')}</div>` : ''}
      ${(c.metrics.subgroup_performance || []).filter((d) => d.groups && d.groups.length).map((d) => `
        <div class="tiny muted">${esc(d.label)}：${(d.groups || []).map((g) => `${esc(g.name)} ${num(g.mean, 1)}（n=${g.n}）`).join(' · ')} · gap ${num(d.gap_sd, 2)} SD</div>`).join('')}
      <div class="tiny" style="color:var(--amber);margin-top:4px">${esc(c.limitations)}</div>
    </div>`).join('');

  $('#view').innerHTML = `
    <div class="view-head">
      <div><h2>质量与闸门</h2><p>闸门不是“专家看过觉得可以”，而是带数字的放行条件；达不到就不放行，且降级自动生效。</p></div>
      <div class="btn-row">
        <button class="btn amber small" data-action="self-check">运行治理自检</button>
        <button class="btn small" data-action="version-bump">模拟模型版本变更</button>
      </div>
    </div>
    ${alerts}
    <div class="grid c4" style="margin-bottom:16px">
      <div class="kpi ${qwkClass}"><div class="k">AI–人 QWK</div><div class="v">${num(report.qwk_ai, 3)}</div><div class="d">人工复核后 ${num(report.qwk_human, 3)}</div></div>
      <div class="kpi blue"><div class="k">证据定位率</div><div class="v">${pct(report.evidence.rate, 0)}</div><div class="d">${report.evidence.localized}/${report.evidence.total} 条片段</div></div>
      <div class="kpi amber"><div class="k">人工编辑率</div><div class="v">${pct(report.review.edit_rate, 0)}</div><div class="d">接受率 ${pct(report.review.accept_rate, 0)} · 复核 ${report.review.reviewed}</div></div>
      <div class="kpi red"><div class="k">证据缺口率</div><div class="v">${pct(report.integrity_gap_rate, 1)}</div><div class="d">目标 ≤ 2% · 触发全站人工</div></div>
    </div>

    <div class="attempt-strip" style="margin-bottom:16px">
      <span>申诉 <b>${appeals.appealed || 0}</b> / ${report.counts.attempts} 场次</span>
      <span>申诉率 <span class="pill ${(appeals.appeal_rate || 0) > 0.03 ? 'red' : 'teal'}">${pct(appeals.appeal_rate, 1)}</span> <span class="tiny muted">目标 &lt; 3%</span></span>
      <span>已重评 <b>${appeals.compared || 0}</b> · 改判 <b>${appeals.changed || 0}</b>${appeals.in_progress ? ` · 重评中 <b>${appeals.in_progress}</b>` : ''}</span>
      <span>改判率 <span class="pill ${appeals.change_rate === null || appeals.change_rate === undefined ? 'muted' : appeals.change_rate > 0.2 ? 'red' : 'teal'}">${appeals.change_rate === null || appeals.change_rate === undefined ? '—' : pct(appeals.change_rate, 1)}</span> <span class="tiny muted">目标 &lt; 20%</span></span>
      ${reviewShares.filter((r) => r.target !== null).map((r) => `<span>${r.risk_level} 人工复核 <b>${pct(r.share, 0)}</b>（${r.reviewed}/${r.attempts}）<span class="pill ${r.share > r.target ? 'red' : 'teal'}">目标 ≤ ${pct(r.target, 0)}</span></span>`).join('')}
      <span>反馈确认时延 ${feedbackLatency.count ? `平均 <b>${num(feedbackLatency.avg_hours, 1)}</b> 小时 · 72h 内 <span class="pill ${feedbackLatency.status === 'pass' ? 'teal' : 'red'}">${pct(feedbackLatency.within_rate, 0)}</span>（${feedbackLatency.count} 份）` : '尚无学生确认记录'}</span>
      <span>练习反馈生成 ${readyPractice.count ? `平均 <b>${num(readyPractice.avg, 1)}</b> 秒 · 60s 内 <span class="pill ${readyPractice.status === 'pass' ? 'teal' : 'red'}">${pct(readyPractice.within_rate, 0)}</span>（${readyPractice.count} 份）` : '尚无练习报告'}</span>
      <span>考试反馈生成 ${readyExam.count ? `平均 <b>${num(readyExam.avg, 1)}</b> 小时 · 72h 内 <span class="pill ${readyExam.status === 'pass' ? 'teal' : 'red'}">${pct(readyExam.within_rate, 0)}</span>（${readyExam.count} 份）` : '尚无已发布成绩'}</span>
      ${spGuidance.reports ? `<span>SP 阶段末引导 <b>${spGuidance.guided_reports || 0}</b>/${spGuidance.reports} 份报告 · 共 <b>${spGuidance.total_guidance || 0}</b> 次${(spGuidance.by_phase || []).length ? `（${spGuidance.by_phase.map((p) => `${esc(p.label)} ${p.count}`).join('、')}）` : ''}</span>` : ''}
      ${appeals.missing_snapshot ? `<span class="tiny muted">${appeals.missing_snapshot} 场历史申诉缺少原始快照</span>` : ''}
    </div>

    ${scorecardHtml(data)}

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">分站质量<span class="sub">按冻结版本 · 识别低区分度考站</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>考站</th><th>版本</th><th class="num">场次</th><th class="num">评分项</th><th class="num">AI QWK</th><th class="num">人工 QWK</th><th class="num">证据定位</th><th class="num">编辑率</th><th class="num">完整性缺口</th><th class="num">公平性差</th></tr></thead>
          <tbody>${stationRows || '<tr><td colspan="10" class="empty">暂无站包</td></tr>'}</tbody>
        </table>
      </div>
      <div class="tiny muted" style="margin-top:6px">分站 QWK 仅统计该冻结版本下的场次；最新版本无场次时显示为 —。</div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">站级心理测量<span class="sub">难度 · 区分度 · Cronbach's α · 异常站标记</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>考站</th><th>版本</th><th class="num">场次</th><th class="num">均分</th><th class="num">SD</th><th class="num">通过率</th><th class="num">α</th><th>标记</th></tr></thead>
          <tbody>${psychoRows || '<tr><td colspan="8" class="empty">暂无足够评分数据</td></tr>'}</tbody>
        </table>
      </div>
      ${psychoNotes}
      <div class="sep"></div>
      <div class="card-title" style="margin-bottom:6px">异常站处置<span class="sub">把异常标记转成可追踪动作</span></div>
      ${stationReviewOptions ? `<form id="station-review-form" class="btn-row" style="flex-wrap:wrap;gap:6px">
        <select class="input" name="station_id" style="max-width:170px" aria-label="考站">${stationReviewOptions}</select>
        <select class="input" name="flag" style="max-width:130px" aria-label="异常类型">
          <option value="low_discrimination">低区分度</option>
          <option value="ceiling">天花板</option>
          <option value="floor">地板</option>
          <option value="sample_insufficient">样本不足</option>
          <option value="other">其他异常</option>
        </select>
        <select class="input" name="decision" style="max-width:140px" aria-label="处置决定">
          <option value="rubric_revision">修订评分表</option>
          <option value="sp_retraining">SP 复训</option>
          <option value="teaching">教学改进</option>
          <option value="monitor">继续观察</option>
          <option value="no_action">暂不处理</option>
        </select>
        <input class="input" name="note" placeholder="处置说明" style="max-width:260px" required>
        <button class="btn small primary" type="submit">记录处置</button>
      </form>` : '<div class="tiny muted">暂无考站可处置</div>'}
      <div class="audit-list" style="margin-top:6px">${reviewRows || '<div class="empty">暂无处置记录</div>'}</div>
      <div class="tiny muted" style="margin-top:6px">α ≥ 0.7 视为可接受；项级区分度 < 0.2 或全员满分/零分提示评分表或 SP 可能有问题，需人工裁定。</div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">提示影响分析<span class="sub">开启 vs 关闭事实层提示 · 事后线索</span></div>
      <div class="attempt-strip" style="margin-bottom:6px">
        ${hintImpact.status === 'ok' ? '<span class="pill teal">两组均有样本</span>' : '<span class="pill amber">样本不足</span>'}
        ${hintImpact.delta_mean === null || hintImpact.delta_mean === undefined ? '' : `<span>开启组均分差 <b>${hintImpact.delta_mean > 0 ? '+' : ''}${num(hintImpact.delta_mean, 2)}</b></span>`}
      </div>
      ${hintRows || '<div class="tiny muted">暂无同时包含开启与关闭提示的执行场次。</div>'}
      <div class="tiny muted" style="margin-top:6px">${esc(hintImpact.note || '')}</div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">蓝图覆盖度<span class="sub">目标占比与目标站数 · 覆盖矩阵 · 空白域与过度重复</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>能力域</th><th class="num">覆盖站数</th><th class="num">评分项</th><th>覆盖考站</th><th class="num">权重占比</th><th class="num">目标</th><th>判定</th></tr></thead>
          <tbody>${coverageRows || '<tr><td colspan="7" class="empty">暂无站包</td></tr>'}</tbody>
        </table>
      </div>
      ${coverageNote}
      <div class="tiny muted" style="margin-top:6px">${esc(coverage.note || '')}</div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">年度蓝图与审批<span class="sub">考什么、不考什么 · 人工批准后覆盖度才按新目标计算</span></div>
      <div class="attempt-strip" style="margin-bottom:8px">${bpStatus}</div>
      ${canManageBlueprint ? `<form id="blueprint-form" class="btn-row" style="flex-wrap:wrap;gap:8px;align-items:flex-end">
        <label class="tiny">年度<input class="input" type="number" name="year" min="2020" max="2100" value="${blueprint.current_year || new Date().getFullYear()}" style="max-width:96px"></label>
        ${bpShareInputs}
        <input class="input" name="note" placeholder="草案说明（可选）" maxlength="300" style="max-width:220px">
        <button class="btn small" type="submit">保存蓝图草案</button>
      </form>` : '<div class="tiny muted">年度蓝图由考务起草并批准生效；当前为只读查看。</div>'}
      <div class="tiny muted" style="margin-top:6px">目标占比合计需接近 100%；保存后仍是草案，须由考务填写理由批准后才生效。</div>
      ${canManageBlueprint ? bpDraftForms : ''}
      <div class="sep"></div>
      <div class="tiny muted" style="margin-bottom:4px">版本历史</div>
      ${bpHistoryRows || '<div class="tiny muted">尚无年度蓝图，当前使用默认六域目标。</div>'}
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">项级可判性<span class="sub">${itemQuality ? `${esc(itemQuality.station_id)} · v${esc(itemQuality.version)}` : '暂无站包'} · 编辑率高或与金标准低一致优先改写</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>评分项</th><th>关键</th><th class="num">评分记录</th><th class="num">已复核</th><th class="num">人工编辑率</th><th class="num">金标准一致率</th><th class="num">平均置信</th><th class="num">区分度</th><th>判定</th></tr></thead>
          <tbody>${itemRows || '<tr><td colspan="9" class="empty">暂无评分项</td></tr>'}</tbody>
        </table>
      </div>
      <div class="tiny muted" style="margin-top:6px">判定规则：复核 ≥3 且编辑率 ≥30%，或金标准 ≥3 且一致率 ≤60%，或区分度 < 0.2 且记录 ≥5，标记为“建议改写”。</div>
      ${(report.asr && report.asr.records) ? `<div class="tiny" style="margin-top:4px">ASR 平均置信度 <b>${num(report.asr.avg_confidence, 3)}</b> · 低于阈值 ${num(report.asr.threshold, 2)} 的场次 <b>${report.asr.low_confidence_attempts}</b>（评分时已整站转人工）</div>` : ''}
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">评分表维护看板<span class="sub">拒绝理由码聚合到评分项 · 直接指向该改哪条锚</span></div>
      <div class="attempt-strip" style="margin-bottom:10px">
        <span>复核动作 <b>${maint.totals.actions || 0}</b></span>
        <span>涉及评分项 <b>${maint.totals.reviewed_items || 0}</b></span>
        <span>优先维护 <b>${maint.totals.needs_review || 0}</b></span>
        ${maintReasonChips || '<span class="tiny muted">尚无拒绝理由码</span>'}
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>评分项</th><th>关键</th><th class="num">动作</th><th class="num">接受</th><th class="num">修正</th><th class="num">拒绝</th><th class="num">分歧率</th><th>主要理由</th><th>建议维护动作</th><th>判定</th></tr></thead>
          <tbody>${maintRows || '<tr><td colspan="10" class="empty">暂无复核动作；拒绝评分时填写理由码后会在此聚合</td></tr>'}</tbody>
        </table>
      </div>
      <div class="tiny muted" style="margin-top:6px">${esc(maint.note || '')} 判定：拒绝 ≥2 次，或复核 ≥3 次且分歧率 ≥50%。</div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">关键项漏判根因分析<span class="sub">AI 判阴性但金标准/人工判阳性 · 待归因 ${critical.unresolved || 0} 条</span></div>
      <div class="attempt-strip" style="margin-bottom:8px">
        <span>漏判 <b>${critical.total || 0}</b></span>
        <span>待归因 <b>${critical.unresolved || 0}</b></span>
        <span>已归因 <b>${critical.resolved || 0}</b></span>
      </div>
      ${criticalRows || '<div class="tiny muted">当前没有关键项漏判记录。</div>'}
      <div class="tiny muted" style="margin-top:6px">${esc(critical.note || '')}</div>
    </div>

    ${canManageUsers ? usersSectionHtml(users) : ''}

    <div class="gates" style="margin-bottom:16px">${gateCards}</div>

    ${susSectionHtml(data.sus || {})}

    ${calibrationSectionHtml(data.calibration || {})}

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">媒体留存治理<span class="sub">成绩永久 · 音视频分级留存 · 删除留痕</span></div>
      <div class="grid c4" style="margin-bottom:10px">
        <div class="kpi blue"><div class="k">媒体总量</div><div class="v">${bytesText(retention.bytes.media_total)}</div><div class="d">${retention.counts.attempts} 个场次</div></div>
        <div class="kpi amber"><div class="k">已到期</div><div class="v">${retention.counts.expired}</div><div class="d">30 天内到期 ${retention.counts.due_soon}</div></div>
        <div class="kpi teal"><div class="k">已清理</div><div class="v">${retention.counts.purged}</div><div class="d">已释放 ${bytesText(retention.bytes.freed)}</div></div>
        <div class="kpi red"><div class="k">申诉冻结</div><div class="v">${retention.counts.held}</div><div class="d">申诉处理中不清理</div></div>
      </div>
      ${retentionRows}
      <div class="btn-row" style="margin-top:8px">
        <button class="btn small" data-action="preview-retention">预览到期清理</button>
        ${canPurge ? '<button class="btn small danger" data-action="purge-retention">执行清理</button>' : ''}
      </div>
      <div id="retention-result" class="tiny muted" style="margin-top:8px">${esc(retention.rule)}</div>
    </div>

    <div class="grid c2" style="margin-bottom:16px">
      ${abSectionHtml(data.ab)}
      ${fairnessSectionHtml(data.fairness)}
    </div>

    ${p3SectionHtml(data.governance || {}, gates)}

    <div class="grid c2" style="margin-bottom:16px">
      <div class="card">
        <div class="card-title">版本治理<span class="sub">变更即降级，禁止静默升级</span></div>
        <div class="version-row"><span class="k">score model</span><span class="v">${esc(data.versions.model)}</span></div>
        <div class="version-row"><span class="k">score prompt</span><span class="v">${esc(data.versions.prompt)}</span></div>
        <div class="version-row"><span class="k">SP model</span><span class="v">${esc(data.versions.sp_model)}</span></div>
        <div class="version-row"><span class="k">SP prompt</span><span class="v">${esc(data.versions.sp_prompt)}</span></div>
        <div class="sep"></div>
        <div class="card-title" style="margin-bottom:6px">权限覆盖</div>
        ${overrides}
        <div class="sep"></div>
        <div class="card-title">治理自检<span class="sub">异常自动降级</span></div>
        ${selfCheckHtml(data.latest_self_check)}
        <div class="btn-row" style="margin-top:8px"><button class="btn small primary" data-action="self-check">运行治理自检</button></div>
      </div>
      <div class="card">
        <div class="card-title">AI 调用<span class="sub">网关唯一出口</span></div>
        <div class="version-row"><span class="k">评分提供方</span><span class="v">${esc(provider.provider || 'mock-score')} · ${provider.configured ? '真实模型已配置' : '默认 Mock'}</span></div>
        <div class="version-row"><span class="k">降级兜底</span><span class="v">${esc(provider.fallback || '未启用')}</span></div>
        <div class="version-row"><span class="k">超时 / 重试</span><span class="v">${num(provider.timeout_seconds, 0)}s · ${provider.max_retries ?? 0} 次</span></div>
        <div class="version-row"><span class="k">ASR 提供方</span><span class="v">${esc(asr.provider || 'off')} · ${asr.configured ? esc(asr.model_version || '已配置') : '未启用'}</span></div>
        ${asr.reason ? `<div class="tiny muted" style="color:var(--amber)">${esc(asr.reason)}</div>` : ''}
        <div class="sep"></div>
        <div class="version-row"><span class="k">调用次数</span><span class="v">${data.ai_calls.count}</span></div>
        <div class="version-row"><span class="k">P95 延迟</span><span class="v">${num(data.ai_calls.p95_latency_ms, 2)} ms</span></div>
        <div class="version-row"><span class="k">平均延迟</span><span class="v">${num(data.ai_calls.avg_latency_ms, 2)} ms</span></div>
        <div class="version-row"><span class="k">Token 输入 / 输出</span><span class="v">${data.ai_calls.input_tokens} / ${data.ai_calls.output_tokens}</span></div>
        <div class="version-row"><span class="k">估算成本（USD）</span><span class="v">$${num(data.ai_calls.total_cost_usd, 4)}${data.ai_calls.cost_per_attempt && data.ai_calls.cost_per_attempt.per_attempt_usd !== null ? ` · 单站均 $${num(data.ai_calls.cost_per_attempt.per_attempt_usd, 4)} / ¥${num(data.ai_calls.cost_per_attempt.per_attempt_cny, 2)}` : ''}${data.ai_calls.pricing_configured ? '' : ' · 单价未配置'}</span></div>
        ${((data.ai_calls.cost_per_attempt || {}).targets || []).map((t) => `<div class="version-row"><span class="k">${esc(t.label)}单站成本</span><span class="v">${t.avg_cny === null ? '—' : `¥${num(t.avg_cny, 2)}`} <span class="pill ${t.status === 'pass' ? 'teal' : t.status === 'fail' ? 'red' : 'muted'}">${t.status === 'pass' ? '达标' : t.status === 'fail' ? '超目标' : '无样本'}</span> <span class="tiny muted">目标 ≤¥${num(t.target_cny, 0)} · ${t.attempts} 场</span></span></div>`).join('')}
        <div class="version-row"><span class="k">能力</span><span class="v">${data.ai_calls.by_capability.map((x) => `${esc(x.key)}:${x.count}`).join(' · ')}</span></div>
        <div class="version-row"><span class="k">状态</span><span class="v">${data.ai_calls.by_status.map((x) => `${esc(x.key)}:${x.count}`).join(' · ')}</span></div>
        <div class="sep"></div>
        <div class="btn-row" style="margin-bottom:6px"><button class="btn small" data-action="ops-ai-check">测试 AI 连通性</button></div>
        <div id="ai-check-result" class="tiny muted" style="margin-bottom:6px"></div>
        <div class="tiny muted">模型调用强制携带 model_version / prompt_version / rubric_version；缺任一项的记录视为无效。</div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-title">ASR 医学专名质量<span class="sub">医学专名错误率 ≤ 8% · 只存哈希与指标</span></div>
      ${asrQualityHtml(data.asr_quality || {})}
      <div class="sep"></div>
      <div class="grid c2" style="gap:10px">
        <label class="field">样本标签<input id="asr-bench-label" class="input" maxlength="80" value="胸痛问诊样本"></label>
        <label class="field">医学专名（逗号分隔）<input id="asr-bench-terms" class="input" placeholder="胸痛, 放射, 心电图"></label>
      </div>
      <label class="field" style="margin-top:8px">参考文本<textarea id="asr-bench-reference" class="input" rows="3" placeholder="粘贴人工校对文本"></textarea></label>
      <label class="field" style="margin-top:8px">ASR 输出<textarea id="asr-bench-hypothesis" class="input" rows="3" placeholder="粘贴 ASR 转写"></textarea></label>
      <div class="btn-row" style="margin-top:8px">
        <button class="btn small primary" data-action="asr-benchmark">计算医学专名错误率</button>
        <span class="tiny muted">参考与输出均不超过 4000 字符；原文不落库。</span>
      </div>
    </div>

    <div class="grid c2">
      <div class="card">
        <div class="card-title">模型卡<span class="sub">公开给校内与考生摘要</span></div>
        <div class="btn-row" style="margin-bottom:8px">
          <button class="btn small" data-action="refresh-model-card">刷新模型卡与数据表</button>
          <span class="tiny muted">生成后需在 P3 治理准备中公开</span>
        </div>
        ${modelCards || '<div class="empty">暂无模型卡</div>'}
      </div>
      <div class="card">
        <div class="card-title">审计事件<span class="sub">只追加 · 哈希链 + 外部锚点</span></div>
        <div class="tiny muted" style="margin-bottom:8px">外部锚点：${anchors.length ? `${anchors.length} 个 · 最近 ${fmtTime(anchors[0].created_at)}` : '尚未写入'}</div>
        <div class="tiny muted" style="margin-bottom:8px">数据库备份：${backups.length ? `${esc(backups[0].filename)} · ${bytesText(backups[0].size_bytes)} · ${fmtTime(backups[0].created_at)}（共 ${backups.length} 个）` : '尚无备份'}</div>
        <div class="btn-row" style="margin-bottom:8px">
          <button class="btn small" data-action="verify-audit">校验审计链</button>
          <button class="btn small" data-action="create-anchor">写入审计锚点</button>
          <button class="btn small" data-action="verify-anchor">校验外部锚点</button>
          <button class="btn small" data-action="ops-health">运行健康检查</button>
          <button class="btn small" data-action="ops-request-logs">查看 API 日志</button>
          <button class="btn small" data-action="ops-content-audit">运行内容审计抽样</button>
          <button class="btn small" data-action="ops-signoff-audit">抽查签字链</button>
          ${state.user && ['admin', 'exam_manager'].includes(state.user.role) ? '<button class="btn small" data-action="ops-maintenance">运行运维维护</button><button class="btn small" data-action="ops-backup">创建数据库备份</button>' : ''}
        </div>
        <div id="audit-chain-result" class="tiny muted" style="margin-bottom:8px">尚未校验。校验会逐条重算 prev_hash 与事件哈希。</div>
        <div id="ops-health-result" class="tiny muted" style="margin-bottom:8px"></div>
        <div id="request-log-result" class="tiny muted" style="margin-bottom:8px"></div>
        <div id="content-audit-result" class="tiny muted" style="margin-bottom:8px"></div>
        <div id="signoff-audit-result" class="tiny muted" style="margin-bottom:8px"></div>
        <div class="audit-list">${auditRows || '<div class="empty">暂无审计</div>'}</div>
        <div class="sep"></div>
        <div class="card-title" style="margin-bottom:6px">审计检索<span class="sub">动作前缀 / 操作者 / 对象</span></div>
        <form id="audit-search-form" class="btn-row" style="flex-wrap:wrap;align-items:center;margin-bottom:8px">
          <input class="input" name="action" placeholder="动作前缀，如 feedback." style="max-width:200px">
          <input class="input" name="actor" placeholder="操作者" style="max-width:140px">
          <input class="input" name="object_id" placeholder="对象 ID" style="max-width:180px">
          <button class="btn small" type="submit">检索</button>
        </form>
        <div id="audit-search-result" class="audit-list" style="max-height:260px"></div>
      </div>
    </div>
  `;
  const calibrationForm = $('#calibration-form');
  if (calibrationForm) {
    calibrationForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const button = calibrationForm.querySelector('button[type="submit"]');
      if (!calibrationForm.querySelector('input[name="file"]').files.length) {
        toast('请选择金标准文件', true);
        return;
      }
      button.disabled = true;
      button.textContent = '导入并重算中…';
      try {
        const result = await apiForm('/api/calibration/import', new FormData(calibrationForm));
        toast(`金标准已导入 ${result.updated} 条；AI QWK ${num(result.report.qwk_ai, 3)}`);
        await viewQuality();
      } catch (err) {
        toast(err.message, true);
      } finally {
        button.disabled = false;
        button.textContent = '导入并重算';
      }
    });
  }
  const susForm = $('#sus-form');
  if (susForm) {
    susForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const selects = Array.from(susForm.querySelectorAll('select[name^="sus_"]'));
      const answers = selects.map((s) => Number(s.value));
      if (answers.length !== 10 || answers.some((a) => !Number.isInteger(a) || a < 1 || a > 5)) {
        toast('请完成全部 10 项 SUS 评分', true);
        return;
      }
      const label = susForm.querySelector('input[name="participant_label"]').value;
      try {
        const result = await api('/api/quality/sus', {
          method: 'POST',
          body: { answers, participant_label: label },
        });
        toast(`SUS 已提交：${num(result.response.score, 1)} 分 · 样本均值 ${num(result.sus.average, 1)}`);
        await viewQuality();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }
  const auditSearchForm = $('#audit-search-form');
  if (auditSearchForm) {
    auditSearchForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const box = $('#audit-search-result');
      const params = new URLSearchParams();
      ['action', 'actor', 'object_id'].forEach((key) => {
        const value = (auditSearchForm.querySelector(`[name="${key}"]`).value || '').trim();
        if (value) params.set(key, value);
      });
      try {
        const result = await api(`/api/audit/search?${params.toString()}`);
        box.innerHTML = result.events.length
          ? result.events.map((ev) => `
            <div class="audit-row">
              <div class="act">${esc(ev.action)}</div>
              <div class="obj">${esc(ev.actor)} · ${esc(ev.object_id)}</div>
              <div class="time">${fmtTime(ev.created_at)}</div>
            </div>`).join('')
          : '<div class="empty">没有匹配的审计事件</div>';
        toast(`检索到 ${result.total} 条，显示 ${result.events.length} 条`);
      } catch (err) {
        toast(err.message, true);
      }
    });
  }
  $$('.critical-miss-form').forEach((form) => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = new FormData(form);
      try {
        await api('/api/quality/critical-miss-review', {
          method: 'POST',
          body: {
            record_id: form.dataset.record,
            root_cause: payload.get('root_cause'),
            action: payload.get('action'),
            note: String(payload.get('note') || '').trim(),
          },
        });
        toast('已记录关键项漏判根因与纠正动作');
        await viewQuality();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });
  const blueprintForm = $('#blueprint-form');
  if (blueprintForm) {
    blueprintForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = new FormData(blueprintForm);
      const domains = bpTargets.map((d) => {
        const rawStations = form.get(`stations_${d.domain}`);
        const entry = {
          domain: d.domain,
          label: d.label,
          target_share: Number(form.get(`share_${d.domain}`) || 0) / 100,
        };
        if (rawStations !== null && String(rawStations).trim() !== '') {
          entry.target_stations = Number(rawStations);
        }
        return entry;
      });
      try {
        await api('/api/blueprint/draft', {
          method: 'POST',
          body: {
            year: Number(form.get('year')),
            domains,
            note: String(form.get('note') || '').trim() || null,
          },
        });
        toast('已保存蓝图草案，待填写理由批准生效');
        await viewQuality();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }
  $$('.bp-approve-form').forEach((form) => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = new FormData(form);
      try {
        await api(`/api/blueprint/${encodeURIComponent(form.dataset.blueprint)}/approve`, {
          method: 'POST',
          body: { note: String(data.get('note') || '').trim() },
        });
        toast('年度蓝图已批准生效，覆盖度将按新目标计算');
        await viewQuality();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });
  const stationReviewForm = $('#station-review-form');
  if (stationReviewForm) {
    stationReviewForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = new FormData(stationReviewForm);
      try {
        await api('/api/quality/station-review', {
          method: 'POST',
          body: {
            station_id: form.get('station_id'),
            flag: form.get('flag'),
            decision: form.get('decision'),
            note: String(form.get('note') || '').trim(),
          },
        });
        toast('已记录异常站处置');
        await viewQuality();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }
  const userForm = $('#user-create-form');
  if (userForm) {
    userForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = new FormData(userForm);
      const button = userForm.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        const result = await api('/api/users', {
          method: 'POST',
          body: {
            username: (form.get('username') || '').trim(),
            display_name: (form.get('display_name') || '').trim(),
            role: form.get('role'),
            password: form.get('password'),
            candidate_code: (form.get('candidate_code') || '').trim() || null,
          },
        });
        toast(`已创建用户 ${result.user.username}（${result.user.role_label}）`);
        await viewQuality();
      } catch (err) {
        toast(err.message, true);
        button.disabled = false;
      }
    });
  }
}

function renderRetentionPreview(result) {
  const box = $('#retention-result');
  if (!box) return;
  const items = result.items || [];
  if (!items.length) {
    box.innerHTML = `<span class="pill teal">无到期媒体</span> 截至 ${fmtTime(result.as_of)}，没有需要清理的媒体文件。`;
    return;
  }
  box.innerHTML = `
    <div class="card tight" style="margin-top:6px">
      <div class="card-title" style="margin-bottom:4px">到期清理预览<span class="sub">${items.length} 个场次 · 可释放 ${bytesText(result.bytes.expired)}</span></div>
      ${items.map((i) => `
        <div class="version-row">
          <span class="k">${esc(i.candidate_code)} · ${esc(i.risk_level)}</span>
          <span class="v">${esc(i.media_filename || i.media_url)} · ${bytesText(i.size_bytes)} · ${esc(i.file_action_label)}</span>
        </div>`).join('')}
      <div class="tiny muted" style="margin-top:6px">确认后仅删除媒体文件；成绩、转写、证据时间戳与哈希链保留，并为每个场次写入 media.purge 审计事件。</div>
    </div>`;
}

/* ---------- global events ---------- */
document.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'download-sample') {
    downloadSampleTranscript();
    return;
  }
  if (action === 'ack-disclosure') {
    try {
      await api(`/api/feedback/${encodeURIComponent(el.dataset.attempt)}/acknowledge`, {
        method: 'POST',
        body: {},
      });
      toast('已记录 AI 参与说明阅读确认');
      await renderFeedback(el.dataset.attempt);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'open-feedback') {
    state.feedbackId = el.dataset.attempt;
    location.hash = '#/feedback';
    return;
  }
  if (action === 'trace-record') {
    const attemptId = el.dataset.attempt;
    const itemId = el.dataset.item;
    const box = $('#trace-result');
    if (box) box.innerHTML = '<span class="muted">追溯中…</span>';
    try {
      const r = await api(`/api/attempts/${encodeURIComponent(attemptId)}/trace?item_id=${encodeURIComponent(itemId)}`);
      const entry = (r.records || [])[0] || {};
      const record = entry.record || {};
      const integrity = r.integrity || { issues: [] };
      const evidenceRows = (entry.evidence || []).map((ev) => `
        <div style="margin-top:3px">
          <span class="pill ${ev.verified === true ? 'teal' : ev.verified === false ? 'red' : 'muted'}">${ev.verified === true ? '引文可定位' : ev.verified === false ? '引文不匹配' : '缺席证据'}</span>
          <span class="mono tiny">${esc(ev.speaker || '')} ${mmss(ev.start_ms)}–${mmss(ev.end_ms)}</span>
          <div>${esc(ev.quote || '')}</div>
          ${ev.transcript_text && ev.transcript_text !== ev.quote ? `<div class="tiny muted">转写原文：${esc(ev.transcript_text)}</div>` : ''}
        </div>`).join('') || '<div class="muted">无证据片段</div>';
      const actionRows = (entry.review_actions || []).map((a) => `
        <div class="tiny">${fmtTime(a.created_at)} · ${esc(a.reviewer)} · ${esc(a.action)} ${a.from_score ?? '—'} → ${a.to_score ?? '—'}${a.reason_code ? ` · ${esc(a.reason_code)}` : ''}</div>`).join('') || '<div class="tiny muted">尚无复核动作</div>';
      const auditRows = (r.audit_events || []).slice(-6).map((ev) => `
        <div class="tiny muted">${fmtTime(ev.created_at)} · ${esc(ev.actor)} · ${esc(ev.action)}</div>`).join('');
      if (box) {
        box.innerHTML = `
          <div><span class="pill ${integrity.issues.length ? 'amber' : 'teal'}">${integrity.issues.length ? `发现 ${integrity.issues.length} 个待核查点` : '追溯链一致'}</span>
            版本戳 ${integrity.version_stamped ? '完整' : '缺失'} · 引文 ${integrity.quotes_matched}/${integrity.quotes_checked} 可定位 · 转写 ${esc((r.transcript || {}).state || '')}</div>
          ${(integrity.issues || []).map((issue) => `<div class="tiny" style="color:var(--amber)">${esc(issue)}</div>`).join('')}
          <div class="tiny muted" style="margin-top:4px">成绩：AI ${record.ai_score ?? '—'} → 最终 ${record.final_score ?? '—'} · 复核人 ${esc(record.reviewer || '—')} · ${fmtTime(record.reviewed_at)}</div>
          ${evidenceRows}
          <div style="margin-top:4px">${actionRows}</div>
          <div style="margin-top:4px">${auditRows}</div>`;
      }
      toast('已加载成绩追溯');
    } catch (err) {
      if (box) box.innerHTML = `<span class="pill red">追溯失败</span> ${esc(err.message)}`;
      toast(err.message, true);
    }
    return;
  }
  if (action === 'integrity-supplement') {
    const input = $('#integrity-supplement-input');
    const note = (input?.value || '').trim();
    if (note.length < 10) {
      toast('现场补充记录不少于 10 字', true);
      return;
    }
    try {
      await api(`/api/attempts/${encodeURIComponent(el.dataset.attempt)}/integrity-supplement`, {
        method: 'POST',
        body: { note },
      });
      toast('已保存现场补充记录');
      if (state.currentReview) await loadReview(state.currentReview.record_id);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'export-attempt') {
    const attemptId = el.dataset.attempt;
    try {
      const data = await api(`/api/attempts/${encodeURIComponent(attemptId)}/export`);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${attemptId}-appeal-package.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(`申诉包已导出：${data.package_hash.slice(0, 18)}…`);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'risk-escalate') {
    const reason = prompt('升级为 L3 将清空该场次最终分，并让全部评分记录进入人工复核。请输入升级理由：');
    if (!reason) return;
    try {
      await api(`/api/attempts/${encodeURIComponent(el.dataset.attempt)}/risk-level`, {
        method: 'POST',
        body: { risk_level: 'L3', reason, actor: state.reviewer || '考务' },
      });
      toast('风险等级已上调为 L3，该场次全部记录进入人工复核');
      await viewReview();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'verify-audit') {
    try {
      const result = await api('/api/audit/verify');
      const box = $('#audit-chain-result');
      if (box) {
        box.innerHTML = result.ok
          ? `<span class="pill teal">完整</span> 共 ${result.total} 条事件，prev_hash 与事件哈希全部一致。`
          : `<span class="pill red">异常</span> 第 ${result.index} 条（${esc(result.broken_at)}）${esc(result.message)}`;
      }
      toast(result.ok ? `审计链完整：${result.total} 条` : '审计链校验失败', !result.ok);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'bundle-view') {
    state.bundleFocus = el.dataset.bundle || null;
    await viewBundle();
    return;
  }
  if (action === 'bundle-latest') {
    state.bundleFocus = null;
    await viewBundle();
    return;
  }
  if (action === 'bundle-export') {
    const bundle = state.bundleCurrent;
    if (!bundle) return;
    const blob = new Blob([JSON.stringify(bundle.payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${bundle.station_id}-bundle-template.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`已导出站包模板：${bundle.station_id}`);
    return;
  }
  if (action === 'bundle-draft') {
    const box = $('#bundle-draft-result');
    const topic = $('#draft-topic')?.value;
    if (!topic) {
      toast('请选择病例主题', true);
      return;
    }
    const difficulty = $('#draft-difficulty')?.value || 'medium';
    const duration = Number($('#draft-duration')?.value) || undefined;
    el.disabled = true;
    try {
      const result = await api('/api/bundles/draft', {
        method: 'POST',
        body: { topic, difficulty, duration_min: duration },
      });
      state.bundleDraft = result.draft;
      const v = result.validation;
      const checkHtml = v.checks.map((c) => `
        <span class="pill ${c.status === 'pass' ? 'teal' : c.status === 'warn' ? 'amber' : 'red'}">${esc(c.label)}</span>`).join(' ');
      const quotaHtml = result.quota.checks.map((c) => `
        <span class="pill ${c.found ? 'teal' : 'red'}">${esc(c.label)}</span>`).join(' ');
      const d = result.difficulty;
      const sourceHtml = result.draft_source === 'llm'
        ? '<span class="pill purple">LLM 初稿</span>'
        : '<span class="pill">模板草案</span>';
      const similar = result.similarity || [];
      const similarHtml = similar.length
        ? `<span class="pill amber">相似站包</span> ${similar.map((s) => `${esc(s.station_id)} v${esc(s.version)}（${pct(s.similarity, 0)}）`).join('；')}`
        : '<span class="pill teal">未发现高相似站包</span>';
      const bp = result.blueprint;
      if (box) {
        box.className = 'tiny';
        box.innerHTML = `
          <div>${v.can_freeze ? '<span class="pill teal">可进入 S2 改写</span>' : '<span class="pill red">存在阻断项</span>'}
            ${sourceHtml} ${v.counts.items} 个评分项 · 关键项 ${v.counts.critical} · ${result.draft.duration_min} 分钟 · ${esc(result.draft.station_id)}</div>
          ${result.llm_error ? `<div class="tiny" style="color:var(--amber)">LLM 草案未采用：${esc(result.llm_error)}</div>` : ''}
          <div style="margin-top:4px">${checkHtml}</div>
          <div style="margin-top:4px"><span class="pill ${result.quota.passed ? 'teal' : 'red'}">强制配额</span> ${quotaHtml}</div>
          <div style="margin-top:4px"><span class="pill ${d.aligned ? 'teal' : 'amber'}">难度 ${esc(d.requested)} → ${esc(d.estimated)}</span>
            预计通过率 ${pct(d.expected_pass_rate, 0)}（${esc(d.range)}）${d.historical.stations ? ` · 历史 ${d.historical.stations} 站均值 ${d.historical.avg_pass_rate === null ? '—' : pct(d.historical.avg_pass_rate, 0)}` : ' · 暂无历史场次'}</div>
          <div style="margin-top:4px"><span class="pill">蓝图</span> ${esc(bp.label)} · ${esc(bp.status)}${bp.uncovered.length ? ` · 未覆盖域 ${bp.uncovered.map((u) => esc(u.label)).join('、')}` : ''}</div>
          <div style="margin-top:4px"><span class="pill">历史质量</span> ${(result.quality_context.advisories || []).map(esc).join('；')}</div>
          <div style="margin-top:4px">${similarHtml}</div>
          <div class="tiny muted" style="margin-top:4px">${esc(result.disclosure.text)}</div>`;
      }
      const load = $('[data-action="bundle-draft-load"]');
      if (load) load.disabled = false;
      toast(`已生成 L0 草案：${result.draft.title}`);
    } catch (err) {
      if (box) box.innerHTML = `<span class="pill red">生成失败</span> ${esc(err.message)}`;
      toast(err.message, true);
    } finally {
      el.disabled = false;
    }
    return;
  }
  if (action === 'bundle-draft-load') {
    if (!state.bundleDraft) {
      toast('请先生成 L0 草案', true);
      return;
    }
    state.bundlePayload = state.bundleDraft;
    const versionInput = $('#bundle-version');
    if (versionInput) versionInput.value = '1.0';
    const reasonInput = $('#bundle-reason');
    if (reasonInput) reasonInput.value = '新建站包（来自 S1 L0 草案）';
    const box = $('#bundle-proposal-result');
    if (box) {
      box.className = 'tiny';
      box.innerHTML = '<span class="pill purple">已载入 L0 草案</span> 请先“校验提案”，确认四项检查后再签字冻结；冻结前必须完成专家改写。';
    }
    toast('草案已载入为提案');
    return;
  }
  if (action === 'bundle-validate') {
    const box = $('#bundle-proposal-result');
    if (!state.bundlePayload) {
      toast('请先上传站包 JSON 文件', true);
      return;
    }
    try {
      const result = await api('/api/bundles/validate', {
        method: 'POST',
        body: {
          payload: state.bundlePayload,
          base_bundle_id: state.bundleCurrent ? state.bundleCurrent.bundle_id : null,
        },
      });
      const v = result.validation;
      const counts = result.derived_counts || {};
      const diff = result.diff;
      const changesHtml = diff ? `
        <div style="margin-top:6px">
          <span class="pill purple">差异审阅</span>
          <span>${diff.has_changes
            ? `${diff.summary.changed_items} 项修改 · ${diff.summary.added_items} 项新增 · ${diff.summary.removed_items} 项删除${diff.summary.other_changes ? ` · ${diff.summary.other_changes} 处其他变更` : ''}`
            : '与当前站包内容一致'}</span>
          ${diff.station.length ? `<div style="margin-top:3px">${diff.station.map((s) => `${esc(s.field)}：${esc(s.before ?? '—')} → ${esc(s.after ?? '—')}`).join('；')}</div>` : ''}
          ${diff.rubric.changed.length ? `<div>修改项：${diff.rubric.changed.map((c) => `${esc(c.item_id)}（${esc(c.fields.join('/'))}）`).join('、')}</div>` : ''}
          ${diff.rubric.added.length ? `<div>新增项：${diff.rubric.added.map(esc).join('、')}</div>` : ''}
          ${diff.rubric.removed.length ? `<div>删除项：${diff.rubric.removed.map(esc).join('、')}</div>` : ''}
        </div>` : '';
      const checksHtml = v.checks.map((c) => `
        <div style="margin-top:4px">
          <span class="pill ${c.status === 'pass' ? 'teal' : c.status === 'warn' ? 'amber' : 'red'}">${esc(c.label)}</span>
          <span>${c.status === 'pass' ? '通过' : esc((c.issues || [])[0] || '待人工核验')}</span>
        </div>`).join('');
      const similar = result.similarity || [];
      const similarHtml = similar.length ? `
        <div style="margin-top:6px">
          <span class="pill amber">相似站包</span>
          <span>${similar.map((s) => `${esc(s.station_id)} · ${esc(s.title)} v${esc(s.version)}（相似度 ${pct(s.similarity, 0)}）`).join('；')}</span>
          <div class="tiny muted">文本相似度仅作“换汤不换药”初筛；请人工确认考点是否真正不同。</div>
        </div>` : '';
      const rewrites = result.rewrites || [];
      const rewriteHtml = rewrites.length ? `
        <div style="margin-top:6px">
          <span class="pill amber">行为锚改写建议 ${rewrites.length} 条</span>
          <div class="tiny muted">${esc(result.rewrite_principle || '行为锚必须可观察；建议只作为改写起点，最终由命题专家签字。')}</div>
          ${rewrites.slice(0, 8).map((r) => `<div style="margin-top:3px">
            <span class="mono tiny">${esc(r.item_id)}.${esc(r.field)}</span>
            <span class="tiny">${esc(r.reason)}</span> → <b>${esc(r.suggestion)}</b>
            <span class="tiny muted">${esc(r.rationale)}</span></div>`).join('')}
        </div>` : '';
      const consistency = v.checks.find((c) => c.key === 'consistency');
      const diffs = v.review_diffs || (consistency && consistency.details && consistency.details.diffs) || [];
      const diffsHtml = diffs.length ? `
        <div style="margin-top:6px">
          <span class="pill amber">冻结前待核验项 ${diffs.length} 条</span>
          <div class="tiny muted">指南待核验与三方一致差异都必须逐条裁决；接受差异需填写理由，“返回修改”会阻断冻结。</div>
          ${diffs.map((d) => {
            const loc = d.location || {};
            let where = '';
            if (loc.section === 'rubric') {
              where = `评分项 ${esc(loc.item_id)}：${esc(loc.sentence)}`;
              if (loc.guideline_ref) where += ` · 指南「${esc(loc.guideline_ref)}」`;
              if (loc.closest_instruction) {
                where += ` · 最接近题干句「${esc(loc.closest_instruction.sentence)}」`;
              }
            } else if (loc.section === 'extra_slots') {
              where = `附加信息槽 #${esc(loc.slot_index)}：${esc(loc.sentence)}`;
            } else if (loc.section === 'instruction_item_refs') {
              where = `考生指令引用 #${esc(loc.ref_index)} → ${esc(loc.item_id)}`;
            }
            return `<div class="tiny" data-diff-row="${esc(d.diff_id)}" style="margin-top:5px;padding:5px 0;border-top:1px dashed var(--line)">
              <span class="pill ${d.severity === 'fail' ? 'red' : 'amber'}">${d.severity === 'fail' ? '阻断' : '待核验'}</span>
              <b>${esc(d.message)}</b>
              <div class="tiny muted">定位：${where}</div>
              <div class="tiny muted">建议：${esc(d.hint)}</div>
              <label class="tiny"><input type="radio" name="adj-${esc(d.diff_id)}" value="accept" checked> 接受差异</label>
              <label class="tiny" style="margin-left:8px"><input type="radio" name="adj-${esc(d.diff_id)}" value="fix"> 返回修改</label>
              <input class="input tiny" data-adj-note="${esc(d.diff_id)}" placeholder="裁决理由（接受必填）" style="max-width:320px;margin-left:8px">
            </div>`;
          }).join('')}
        </div>` : '';
      box.className = 'tiny';
      box.innerHTML = `
        <div>${v.can_freeze ? '<span class="pill teal">可冻结</span>' : '<span class="pill red">存在阻断项</span>'}
          ${(v.counts || {}).items || counts.items || 0} 个评分项 · 派生 ${counts.slots || 0} 个信息槽 / ${counts.probes || 0} 个探针</div>
        ${checksHtml}
        ${diffsHtml}
        ${rewriteHtml}
        ${similarHtml}
        ${changesHtml}
        ${v.blocking.length ? `<div style="margin-top:4px;color:var(--red)">${esc(v.blocking.slice(0, 3).join('；'))}</div>` : ''}`;
      toast(v.can_freeze
        ? `校验通过${diffs.length ? `，待逐条裁决 ${diffs.length} 条差异` : '，可签字冻结'}`
        : `校验未通过：${v.blocking.length} 个阻断项`, !v.can_freeze);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'bundle-freeze') {
    if (!state.bundlePayload) {
      toast('请先上传站包 JSON 文件', true);
      return;
    }
    const version = ($('#bundle-version')?.value || '').trim();
    const reason = ($('#bundle-reason')?.value || '').trim() || '站包修订';
    if (!/^\d+\.\d+$/.test(version)) {
      toast('版本号格式应为 主版本.次版本，例如 1.1', true);
      return;
    }
    const adjudications = $$('[data-diff-row]').map((row) => {
      const diffId = row.dataset.diffRow;
      const checked = row.querySelector('input[type="radio"]:checked');
      const note = row.querySelector('[data-adj-note]')?.value || '';
      return { diff_id: diffId, decision: checked ? checked.value : 'accept', note };
    });
    try {
      const result = await api('/api/bundles/freeze', {
        method: 'POST',
        body: {
          payload: state.bundlePayload,
          version,
          reason,
          supersedes: state.bundleCurrent && state.bundlePayload.station_id === state.bundleCurrent.station_id ? state.bundleCurrent.bundle_id : null,
          adjudications,
        },
      });
      toast(result.created
        ? `已冻结 ${result.bundle.bundle_id} v${result.bundle.version}`
        : '相同内容已存在，未重复冻结');
      state.bundleFocus = result.bundle.bundle_id;
      await viewBundle();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'bundle-verify') {
    const box = $('#bundle-verify-result');
    try {
      const result = await api(`/api/bundles/${encodeURIComponent(el.dataset.bundle)}/verify-integrity`, {
        method: 'POST',
        body: {},
      });
      const report = result.integrity;
      const statusPill = report.status === 'pass'
        ? '<span class="pill teal">通过</span>'
        : report.status === 'warn'
          ? '<span class="pill amber">待核验</span>'
          : '<span class="pill red">失败</span>';
      if (box) {
        box.innerHTML = `${statusPill} ${report.checks.map((c) => `<span class="pill ${c.passed ? 'teal' : c.missing ? 'amber' : 'red'}" title="${dataAttr(c.label)}">${esc(c.key)}</span>`).join(' ')} <span class="mono">${esc((report.bundle_hash || '').slice(0, 16))}…</span>`;
      }
      toast(report.status === 'pass'
        ? '站包完整性核验通过'
        : `站包完整性核验：${report.status === 'warn' ? '缺少冻结审计锚点' : '发现不一致，请人工排查'}`, report.status === 'fail');
    } catch (err) {
      if (box) box.innerHTML = `<span class="pill red">核验失败</span> ${esc(err.message)}`;
      toast(err.message, true);
    }
    return;
  }
  if (action === 'refresh-model-card') {
    try {
      const result = await api('/api/governance/model-card/refresh', {
        method: 'POST',
        body: {},
      });
      toast(`已生成新模型卡与数据表 v${result.data_sheet_version}，待公开`);
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'create-anchor') {
    try {
      const result = await api('/api/audit/anchors', { method: 'POST', body: {} });
      const box = $('#audit-chain-result');
      if (box) {
        box.innerHTML = `<span class="pill teal">已写入</span> 第 ${result.anchor.event_count} 条事件 · <span class="mono">${esc(result.anchor.head_hash.slice(0, 22))}…</span>`;
      }
      toast(`审计锚点已写入：${result.anchor.event_count} 条事件`);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'verify-anchor') {
    try {
      const result = await api('/api/audit/anchors/verify');
      const box = $('#audit-chain-result');
      if (box) {
        box.innerHTML = result.ok
          ? `<span class="pill teal">锚点有效</span> ${esc(result.message)} · ${esc(result.external_file)}`
          : `<span class="pill red">锚点异常</span> ${esc(result.message)}`;
      }
      toast(result.ok ? result.message : '外部锚点校验失败', !result.ok);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'ops-health') {
    const box = $('#ops-health-result');
    try {
      const result = await api('/api/ops/health');
      const healthy = result.checks.filter((c) => c.ok).length;
      if (box) {
        box.innerHTML = `
          <div><span class="pill ${result.status === 'ok' ? 'teal' : 'red'}">${result.status === 'ok' ? '运行正常' : '存在降级'}</span>
            ${healthy}/${result.checks.length} 项通过 · AI ${esc(result.ai.provider)} · ${esc(result.ai.model_version)}</div>
          ${result.checks.map((c) => `
            <div style="margin-top:3px"><span class="pill ${c.ok ? 'teal' : 'red'}">${esc(c.label)}</span> <span>${esc(c.detail)}</span></div>`).join('')}
          <div style="margin-top:3px">场次 ${result.counts.attempts} · 评分记录 ${result.counts.score_records} · 待复核 ${result.counts.pending_reviews} · 审计事件 ${result.counts.audit_events}</div>`;
      }
      toast(result.status === 'ok' ? '健康检查通过' : '健康检查发现降级项', result.status !== 'ok');
    } catch (err) {
      if (box) box.innerHTML = `<span class="pill red">检查失败</span> ${esc(err.message)}`;
      toast(err.message, true);
    }
    return;
  }
  if (action === 'ops-request-logs') {
    const box = $('#request-log-result');
    try {
      const result = await api('/api/ops/request-logs?limit=20&days=7');
      const s = result.summary;
      const statusText = Object.entries(s.by_status || {}).map(([key, count]) => `${esc(key)}:${count}`).join(' · ') || '暂无';
      const rows = (result.recent || []).slice(-8).reverse().map((row) => `
        <div class="mono" style="margin-top:2px">${esc(row.method || '')} ${esc(row.path || '')} · ${row.status} · ${num(row.duration_ms, 1)}ms · ${esc(row.user || '匿名')}</div>`).join('');
      if (box) {
        box.innerHTML = `
          <div><span class="pill ${s.errors ? 'amber' : 'teal'}">近 ${s.days} 天 ${s.total} 次请求</span>
            错误 ${s.errors} · 均值 ${s.avg_duration_ms === null ? '—' : `${num(s.avg_duration_ms, 1)}ms`} · P95 ${s.p95_duration_ms === null ? '—' : `${num(s.p95_duration_ms, 1)}ms`}</div>
          <div style="margin-top:2px">状态分布 ${statusText} · 高频 ${(s.top_paths || []).map((x) => `${esc(x.path)}:${x.count}`).join(' · ') || '暂无'}</div>
          ${rows || '<div>暂无请求明细</div>'}`;
      }
      toast('已加载 API 请求日志');
    } catch (err) {
      if (box) box.innerHTML = `<span class="pill red">加载失败</span> ${esc(err.message)}`;
      toast(err.message, true);
    }
    return;
  }
  if (action === 'ops-content-audit') {
    const box = $('#content-audit-result');
    try {
      const result = await api('/api/ops/content-audit', {
        method: 'POST',
        body: { sample_rate: 0.2, limit: 20 },
      });
      const findings = result.findings || [];
      const rows = findings.slice(0, 5).map((f) => `
        <div style="margin-top:2px"><span class="pill ${f.severity === 'high' ? 'red' : 'amber'}">${esc(f.label)}</span>
          <span class="mono tiny">${esc(f.attempt_id)} · ${esc(f.utt_id || '')}</span> ${esc(f.quote || '')}</div>`).join('');
      if (box) {
        box.innerHTML = `
          <div><span class="pill ${result.finding_count ? 'amber' : 'teal'}">扫描 ${result.attempts_scanned}/${result.attempts_considered} 场 · 命中 ${result.finding_count} 条</span>
            高风险 ${result.by_severity.high || 0} · 中风险 ${result.by_severity.medium || 0}</div>
          ${rows || '<div>本次抽样未发现越界话术</div>'}
          <div class="tiny muted" style="margin-top:2px">${esc(result.note)}</div>`;
      }
      toast(result.finding_count ? `内容审计命中 ${result.finding_count} 条线索` : '内容审计未发现越界话术', Boolean(result.finding_count));
    } catch (err) {
      if (box) box.innerHTML = `<span class="pill red">审计失败</span> ${esc(err.message)}`;
      toast(err.message, true);
    }
    return;
  }
  if (action === 'ops-signoff-audit') {
    const box = $('#signoff-audit-result');
    try {
      const result = await api('/api/ops/signoff-audit?sample_size=20');
      const issues = (result.issues || []).slice(0, 5).map((item) => `
        <div style="margin-top:2px"><span class="pill amber">${esc(item.attempt_id)}</span> ${esc(item.issues.join('；'))}</div>`).join('');
      if (box) {
        box.innerHTML = `
          <div><span class="pill ${result.failed ? 'amber' : 'teal'}">抽查 ${result.checked} 份 · 通过 ${result.passed} · 异常 ${result.failed}</span>
            通过率 ${result.pass_rate === null ? '—' : pct(result.pass_rate, 0)}</div>
          ${issues || (result.checked ? '<div>抽查样本签字链完整</div>' : '<div>暂无已签字或已发布场次</div>')}
          <div class="tiny muted" style="margin-top:2px">${esc(result.note)}</div>`;
      }
      toast(result.failed ? `签字链抽查发现 ${result.failed} 份异常` : '签字链抽查通过', Boolean(result.failed));
    } catch (err) {
      if (box) box.innerHTML = `<span class="pill red">抽查失败</span> ${esc(err.message)}`;
      toast(err.message, true);
    }
    return;
  }
  if (action === 'ops-ai-check') {
    const box = $('#ai-check-result');
    try {
      const result = await api('/api/ops/ai-check', { method: 'POST', body: {} });
      if (box) {
        box.innerHTML = result.mode === 'mock'
          ? '<span class="pill muted">Mock 模式</span> 当前不会发起真实网络调用；配置真实提供方后可验证连通性。'
          : `<span class="pill ${result.ok ? 'teal' : 'red'}">${result.ok ? (result.degraded ? '降级可用' : '连通正常') : '调用失败'}</span>
             ${result.latency_ms === null ? '' : `延迟 ${num(result.latency_ms, 1)} ms`}
             ${result.score === undefined ? '' : ` · 自检分 ${result.score === null ? '—' : result.score} · 证据 ${result.evidence_count}`}
             ${result.error ? `<div style="color:var(--red)">${esc(result.error)}</div>` : `<div class="tiny muted">${esc(result.note)}</div>`}`;
      }
      toast(result.ok ? (result.mode === 'mock' ? '当前为 Mock 提供方' : 'AI 连通性自检通过') : 'AI 连通性自检失败', !result.ok);
    } catch (err) {
      if (box) box.innerHTML = `<span class="pill red">自检失败</span> ${esc(err.message)}`;
      toast(err.message, true);
    }
    return;
  }
  if (action === 'asr-benchmark') {
    const reference = ($('#asr-bench-reference') || {}).value || '';
    const hypothesis = ($('#asr-bench-hypothesis') || {}).value || '';
    const terms = (($('#asr-bench-terms') || {}).value || '')
      .split(/[,，\s]+/)
      .map((term) => term.trim())
      .filter(Boolean);
    if (!reference.trim() || !hypothesis.trim()) {
      toast('请填写参考文本与 ASR 输出', true);
      return;
    }
    try {
      const result = await api('/api/asr/benchmarks', {
        method: 'POST',
        body: {
          label: (($('#asr-bench-label') || {}).value || 'ASR 样本').trim(),
          reference,
          hypothesis,
          terms,
          actor: state.reviewer || 'ASR 评测员',
        },
      });
      toast(`ASR 错误率 ${pct(result.benchmark.metrics.error_rate, 1)} · ${result.benchmark.metrics.status === 'pass' ? '达标' : '未达标'}`, result.benchmark.metrics.status !== 'pass');
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'ops-maintenance') {
    try {
      const result = await api('/api/ops/maintenance', { method: 'POST', body: {} });
      const m = result.maintenance;
      toast(`维护完成：清理会话 ${m.sessions_cleaned}，清理媒体 ${m.purged_attempts}${m.authority_downgrades ? `，项级降级 ${m.authority_downgrades}` : ''}${m.safety_check && m.safety_check.changes ? `，安全降级 ${m.safety_check.changes}` : ''}${m.anchor_id ? '，已写入审计锚点' : ''}`);
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'ops-backup') {
    try {
      const result = await api('/api/ops/backup', { method: 'POST', body: {} });
      toast(`已创建数据库备份：${result.backup.filename}`);
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'user-toggle') {
    const active = el.dataset.active === 'true';
    if (!active && !confirm('停用后该用户所有会话立即失效。继续？')) return;
    try {
      const result = await api(`/api/users/${encodeURIComponent(el.dataset.user)}`, {
        method: 'PATCH',
        body: { active },
      });
      toast(`已${active ? '启用' : '停用'}用户 ${result.user.username}`);
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'user-reset-password') {
    const password = prompt('新密码（至少 8 位）。重置后该用户所有会话立即失效：');
    if (password === null) return;
    if (password.length < 8) {
      toast('密码至少 8 位', true);
      return;
    }
    try {
      const result = await api(`/api/users/${encodeURIComponent(el.dataset.user)}`, {
        method: 'PATCH',
        body: { password },
      });
      toast(`已重置 ${result.user.username} 的密码`);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'preview-retention') {
    try {
      const result = await api('/api/retention');
      renderRetentionPreview(result);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'purge-retention') {
    if (!confirm('将按留存策略清理所有到期媒体文件。成绩、转写与证据哈希保留，删除动作写入审计链。继续？')) return;
    try {
      const result = await api('/api/retention/purge', { method: 'POST', body: { apply: true } });
      const failed = result.summary.failed || 0;
      toast(`已清理 ${result.summary.purged_attempts} 个场次媒体，删除 ${result.deleted_files} 个文件，释放 ${bytesText(result.freed_bytes)}${failed ? `；${failed} 个文件删除失败，将重试` : ''}`);
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'self-check') {
    try {
      const result = await api('/api/governance/self-check', {
        method: 'POST',
        body: { apply: true, actor: state.reviewer || '系统' },
      });
      toast(result.applied
        ? `治理自检完成：${result.applied_changes.length} 个评分项权限自动降级`
        : `治理自检完成：${result.trigger_summary}`);
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'termination-drill') {
    try {
      const result = await api('/api/governance/drills/termination', {
        method: 'POST',
        body: { actor: state.reviewer || '考务' },
      });
      toast(result.drill.passed ? `终止开关演练通过 · ${num(result.drill.duration_ms, 0)}ms` : '终止开关演练未通过');
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'appeal-drill') {
    try {
      const result = await api('/api/governance/drills/appeal', {
        method: 'POST',
        body: { actor: state.reviewer || '考务' },
      });
      toast(result.drill.passed ? '申诉全流程演练通过' : '申诉演练存在未通过步骤');
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'approve-ethics') {
    try {
      await api('/api/governance/approvals', {
        method: 'POST',
        body: {
          approval_type: 'ethics_and_data',
          approver: state.reviewer || '伦理委员会',
          note: 'P3 伦理与数据合规批准（演示）',
        },
      });
      toast('伦理与数据合规批准已登记');
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'approve-committee') {
    try {
      await api('/api/governance/approvals', {
        method: 'POST',
        body: {
          approval_type: 'academic_committee',
          approver: state.reviewer || '学术委员会',
          note: 'A/B 对照报告经学术委员会审议（演示）',
        },
      });
      toast('学术委员会审议已登记');
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'publish-model-card') {
    try {
      await api('/api/governance/model-card/publish', {
        method: 'POST',
        body: { actor: state.reviewer || '系统' },
      });
      toast('模型卡与数据表已公开');
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'release-g3') {
    try {
      const result = await api('/api/governance/release-stage', {
        method: 'POST',
        body: {
          stage: 'G3_limited',
          actor: state.reviewer || '系统',
          reason: 'G3 六项指标达标，有限开放非关键项 AI 建议',
        },
      });
      toast(`已升级：${result.old_stage} → ${result.new_stage}`);
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-start') {
    try {
      await api('/api/execution/start', { method: 'POST', body: { actor: state.reviewer || '考务' } });
      toast('考试执行已启动，第 1 轮槽位进入进行中');
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-advance') {
    try {
      const data = await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/advance`, { method: 'POST' });
      toast(data.run.status === 'completed' ? '全部轮次完成' : `已进入第 ${data.run.current_round} 轮`);
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-stop') {
    const reason = prompt('终止整场考试的理由：');
    if (!reason) return;
    try {
      await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/stop`, {
        method: 'POST',
        body: { reason, actor: state.reviewer || '考务' },
      });
      toast('考试已终止，事件已写入审计');
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-hints') {
    try {
      const enabled = el.dataset.enabled === 'true';
      await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/hints`, {
        method: 'POST',
        body: { enabled, actor: state.reviewer || '考务' },
      });
      toast(enabled ? '事实层提示已开启' : '事实层提示已关闭，操作已留痕');
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-slot-hints') {
    try {
      const enabled = el.dataset.enabled === 'true';
      await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/hints`, {
        method: 'POST',
        body: {
          enabled,
          station_id: el.dataset.stationId,
          actor: state.reviewer || '考务',
        },
      });
      toast(enabled ? '本站事实层提示已开启' : '本站事实层提示已关闭，操作已留痕');
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-export') {
    try {
      const data = await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/export`);
      const blob = new Blob([JSON.stringify(data.package, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${state.executionRunId}-archive.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(`执行归档包已导出：${data.package_hash.slice(0, 18)}…`);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-ingest') {
    state.ingestSlot = {
      run_id: el.dataset.run,
      slot_key: el.dataset.slotKey,
      candidate_code: el.dataset.code,
      candidate_name: el.dataset.name,
      station_name: el.dataset.station,
      round: el.dataset.round,
    };
    location.hash = '#/ingest';
    return;
  }
  if (action === 'exec-live-segment') {
    const text = (prompt('实时转写片段（外部 ASR 回传）：') || '').trim();
    if (!text) return;
    const isCandidate = confirm('确定为考生发言？取消则记为患者发言');
    try {
      const result = await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/slots/${encodeURIComponent(el.dataset.slotKey)}/segments`, {
        method: 'POST',
        body: { speaker: isCandidate ? 'candidate' : 'patient', text, actor: state.reviewer || '考务' },
      });
      const summary = (result.facts || {}).facts || {};
      const newAnomalies = result.new_anomalies || [];
      toast(
        newAnomalies.length
          ? `转写片段已回传；新增异常：${newAnomalies.map((a) => a.label || a.type).join('、')}`
          : `转写片段已回传；尚无证据 ${summary.missing === undefined ? '—' : summary.missing} 项`
      );
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-live-ack') {
    const note = (prompt('实时异常处置说明（用于审计留痕）：') || '').trim();
    if (!note) return;
    try {
      await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/slots/${encodeURIComponent(el.dataset.slotKey)}/anomaly-ack`, {
        method: 'POST',
        body: { note, actor: state.reviewer || '考务' },
      });
      toast('实时异常已处置并写入审计');
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-slot') {
    const slotAction = el.dataset.slotAction;
    let note = '';
    if (slotAction !== 'complete') {
      note = prompt(slotAction === 'terminate' ? '终止该站的理由：' : '异常说明（事实层）：') || '';
      if (slotAction === 'terminate' && !note) return;
    }
    try {
      await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/slots/${encodeURIComponent(el.dataset.slotKey)}/action`, {
        method: 'POST',
        body: { action: slotAction, note, actor: state.reviewer || '考务' },
      });
      toast(slotAction === 'complete' ? '该槽位已完成' : slotAction === 'terminate' ? '该站已终止' : '异常已记录');
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-slot-compensate') {
    const choice = (prompt('补偿方式：输入 1=延时补时，2=另行安排重考，3=不计入本次成绩') || '').trim();
    const map = { '1': 'extend', '2': 'retake', '3': 'exclude', extend: 'extend', retake: 'retake', exclude: 'exclude' };
    const compensation = map[choice];
    if (!compensation) {
      toast('未选择有效补偿方式', true);
      return;
    }
    const note = (prompt('补偿说明（用于审计留痕）：') || '').trim();
    if (!note) {
      toast('补偿判定必须填写说明', true);
      return;
    }
    try {
      await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/slots/${encodeURIComponent(el.dataset.slotKey)}/action`, {
        method: 'POST',
        body: { action: 'compensate', compensation, note, actor: state.reviewer || '考务' },
      });
      toast('补偿判定已记录');
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-global-stop') {
    if (!confirm('激活全局终止开关？评分、SP 对话与成绩发布将被阻断，并终止当前考试执行。')) return;
    try {
      await api('/api/governance/termination-switch', {
        method: 'POST',
        body: { active: true, reason: '考试执行台全局终止', actor: state.reviewer || '考务' },
      });
      if (state.executionRunId) {
        await api(`/api/execution/${encodeURIComponent(state.executionRunId)}/stop`, {
          method: 'POST',
          body: { reason: '全局终止开关', actor: state.reviewer || '考务' },
        });
      }
      toast('全局终止已激活', true);
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'exec-global-resume') {
    try {
      await api('/api/governance/termination-switch', {
        method: 'POST',
        body: { active: false, reason: '现场确认安全，解除终止', actor: state.reviewer || '考务' },
      });
      toast('全局终止已解除');
      await viewExecution();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'signoff' && el.dataset.failing === '1') {
    const cell = el.closest('td');
    if (cell) {
      cell.innerHTML = `<span class="tiny">未达标寄语</span>
        <input class="input tiny" data-signoff-note placeholder="人工鼓励/改进说明（≥10字）" style="max-width:220px;margin:0 4px">
        <button class="link-btn" data-action="signoff-confirm" data-attempt="${esc(el.dataset.attempt)}">确认签字</button>
        <span class="muted"> · </span>
        <button class="link-btn" data-action="signoff-cancel">取消</button>`;
    }
    return;
  }
  if (action === 'rescore') {
    if (!confirm('重跑会用当前模型与提示词版本重新生成该场次的 AI 建议分；已有金标准保留，人工复核记录会阻止重跑。继续？')) return;
    try {
      const result = await api(`/api/attempts/${encodeURIComponent(el.dataset.attempt)}/rescore`, { method: 'POST' });
      toast(`已重跑评分：${result.rescore.records} 条记录，AI 总分 ${num(result.rescore.ai_total, 1)}`);
      await viewOverview();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'signoff-cancel') {
    await viewOverview();
    return;
  }
  if (action === 'signoff' || action === 'signoff-confirm' || action === 'publish') {
    const reviewer = ($('#reviewer-input').value || '').trim();
    if (!reviewer) return toast('请先填写复核人', true);
    let feedbackNote = '';
    if (action === 'signoff-confirm') {
      feedbackNote = (el.closest('td')?.querySelector('[data-signoff-note]')?.value || '').trim();
      if (!feedbackNote) return toast('未达标场次必须填写人工鼓励说明', true);
    }
    try {
      await api(`/api/attempts/${encodeURIComponent(el.dataset.attempt)}/${action === 'publish' ? 'publish' : 'signoff'}`, {
        method: 'POST',
        body: action === 'publish' ? { reviewer } : { reviewer, feedback_note: feedbackNote },
      });
      toast(action === 'publish' ? '成绩已发布，写入审计链' : '已完成 human_signoff，可发布成绩');
      await viewOverview();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'play-clip') {
    const audio = $('#review-audio');
    if (!audio) return;
    const start = Number(audio.dataset.clipStart || 0);
    const end = Number(audio.dataset.clipEnd || 0);
    audio.currentTime = start;
    audio.play().catch(() => toast('无法播放音频', true));
    if (end) {
      const check = () => {
        if (audio.currentTime >= end) {
          audio.pause();
          audio.removeEventListener('timeupdate', check);
        }
      };
      audio.addEventListener('timeupdate', check);
    }
    return;
  }
  if (action === 'mode') {
    state.reviewMode = el.dataset.mode;
    state.reviewScore = null;
    state.reviewReason = null;
    refreshActionPanel();
    return;
  }
  if (action === 'cancel-mode') {
    state.reviewMode = null;
    state.reviewScore = null;
    state.reviewReason = null;
    refreshActionPanel();
    return;
  }
  if (action === 'score') {
    state.reviewScore = Number(el.dataset.score);
    $$('[data-action="score"]').forEach((b) => b.classList.toggle('selected', Number(b.dataset.score) === state.reviewScore));
    return;
  }
  if (action === 'reason') {
    state.reviewReason = el.value;
    return;
  }
  if (action === 'accept') {
    await submitReview('accept');
    return;
  }
  if (action === 'submit-mode') {
    const mode = state.reviewMode;
    if (state.reviewScore === null || state.reviewScore === undefined) {
      toast('请选择修正后的档位', true);
      return;
    }
    if (mode === 'reject' && !state.reviewReason) {
      toast('拒绝必须选择理由码', true);
      return;
    }
    await submitReview(mode, state.reviewScore, mode === 'reject' ? state.reviewReason : null);
    return;
  }
  if (action === 'manual-save') {
    if (state.reviewScore === null || state.reviewScore === undefined) {
      toast('请选择人工评分档位', true);
      return;
    }
    await submitReview('manual_score', state.reviewScore);
    return;
  }
  if (action === 'play-feedback') {
    const audio = $('#fb-audio');
    if (!audio) return;
    const url = el.dataset.url;
    if (url && audio.src.indexOf(url) === -1) audio.src = url;
    const start = Number(el.dataset.start || 0) / 1000;
    const end = Number(el.dataset.end || 0) / 1000 + 3;
    audio.dataset.clipEnd = String(end);
    audio.currentTime = Math.max(0, start - 3);
    audio.play().catch(() => toast('无法播放音频', true));
    return;
  }
  if (action === 'sp-suggest') {
    const input = $('#sp-input');
    if (input) {
      input.value = el.dataset.text || '';
      input.focus();
    }
    return;
  }
  if (action === 'sp-new') {
    if (!state.sp) return;
    await createSpSession();
    renderSP();
    return;
  }
  if (action === 'sp-finish') {
    if (!state.sp || !state.sp.session) return;
    try {
      const data = await api(`/api/sp/sessions/${encodeURIComponent(state.sp.session.session_id)}/finish`, {
        method: 'POST',
      });
      state.sp.session.report = data.report;
      state.sp.session.finished = true;
      renderSP();
      toast('已生成三段式练习反馈');
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'sp-certify') {
    if (!state.sp || !state.sp.persona) return;
    try {
      const data = await api('/api/sp/certification/run', {
        method: 'POST',
        body: { bundle_id: state.sp.persona.bundle_id, actor: state.reviewer || 'SP 认证员' },
      });
      state.sp.persona.certification = data.certification;
      renderSP();
      const cert = data.certification;
      toast(cert.status === 'certified' ? 'SP 认证通过：红队与一致性抽测达标' : '认证未通过：请查看失败项');
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (action === 'version-bump') {
    if (!confirm('模拟模型/提示词版本变更？系统会把所有 suggest/decide 权限自动降为 extract，并写入审计。')) return;
    try {
      const model = 'mock-score-1.1';
      const result = await api('/api/governance/version-bump', {
        method: 'POST',
        body: { model_version: model, actor: state.reviewer || '系统管理员' },
      });
      toast(`版本已切换为 ${result.model_version}，${result.changes.length} 个评分项权限自动降级`);
      await viewQuality();
    } catch (err) {
      toast(err.message, true);
    }
  }
});

document.addEventListener('change', async (e) => {
  if (e.target.dataset && e.target.dataset.action === 'reason') {
    state.reviewReason = e.target.value;
    return;
  }
  if (e.target.dataset && e.target.dataset.action === 'user-role') {
    const select = e.target;
    try {
      const result = await api(`/api/users/${encodeURIComponent(select.dataset.user)}`, {
        method: 'PATCH',
        body: { role: select.value },
      });
      toast(`已将 ${result.user.username} 调整为${result.user.role_label}`);
    } catch (err) {
      toast(err.message, true);
    }
    await viewQuality();
  }
});

/* ---------- boot ---------- */
$('#reviewer-input').value = state.reviewer;
$('#reviewer-input').addEventListener('change', (e) => {
  state.reviewer = e.target.value.trim() || '主治 张蕾';
  localStorage.setItem('osceloop.reviewer', state.reviewer);
});
$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errorBox = $('#login-error');
  errorBox.style.display = 'none';
  try {
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: {
        username: $('#login-username').value.trim(),
        password: $('#login-password').value,
      },
    });
    state.token = data.token;
    localStorage.setItem('osceloop.token', data.token);
    setUser(data.user);
    hideAuthGate();
    if (!location.hash || !ROUTES[currentRoute()]) location.hash = firstAllowedRoute();
    await route();
    toast(`已登录：${data.user.display_name} · ${data.user.role_label}`);
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.style.display = '';
  }
});
$('#logout-btn').addEventListener('click', async () => {
  try {
    await api('/api/auth/logout', { method: 'POST', body: {} });
  } catch (e) { /* ignore */ }
  clearAuth();
  $('#user-box').hidden = true;
  showAuthGate();
});
window.addEventListener('hashchange', route);

async function boot() {
  if (state.token) {
    try {
      const user = await api('/api/auth/me');
      setUser(user);
      hideAuthGate();
      if (!location.hash) location.hash = firstAllowedRoute();
      await route();
      return;
    } catch (err) {
      clearAuth();
    }
  }
  showAuthGate();
}

boot();
