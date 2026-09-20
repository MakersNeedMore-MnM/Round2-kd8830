/* static/app.js */
const $ = (sel) => document.querySelector(sel);

const els = {
  queue:        $('#queue-list'),
  pending:      $('#pending-list'),
  pendingCount: $('#pending-count'),
  logs:         $('#log-list'),
  tbody:        document.querySelector('#appt-table tbody'),
  stats:        $('#statstrip'),
  hospital:     $('#hospital-name'),
  llmBadge:     $('#llm-badge'),
  wsBadge:      $('#ws-badge'),
  deptList:     $('#dept-list'),
  docList:      $('#doc-list'),
};

let current = null;
let pendingSignature = '';
let ws = null;
let wsRetry = null;

/* ── helpers ─────────────────────────────────────────────────── */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

let toastTimer = null;
function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast show ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast ' + kind; }, 2600);
}

function esc(str) {
  return String(str ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function clockOf(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function timeAgo(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

/* ── renderers ───────────────────────────────────────────────── */
function renderStats(s) {
  els.stats.innerHTML = `
    <div class="stat"><b>${s.total}</b><span>Total</span></div>
    <div class="stat"><b>${s.booked + s.checked_in}</b><span>Waiting</span></div>
    <div class="stat"><b>${s.in_consult}</b><span>In consult</span></div>
    <div class="stat"><b>${s.completed}</b><span>Done</span></div>
    <div class="stat"><b>${s.no_show}</b><span>No-show</span></div>
    <div class="stat"><b>${s.no_show_rate}%</b><span>No-show rate</span></div>`;
}

function renderQueue(doctors) {
  if (!doctors.length) {
    els.queue.innerHTML = '<div class="empty">No doctors yet — hit “Reseed demo”.</div>';
    return;
  }
  els.queue.innerHTML = doctors.map((d) => {
    const n = d.queue_length;
    const cls = n >= 6 ? 'heavy' : n >= 3 ? 'busy' : '';
    return `<div class="qrow">
      <div class="who">
        <div class="nm">${esc(d.name)}</div>
        <div class="dp">${esc(d.department)} · avg ${Math.round(d.avg_consult_minutes)} min · ×${d.efficiency.toFixed(2)}</div>
      </div>
      <div class="qcount ${cls}">${n} in queue</div>
    </div>`;
  }).join('');
}

function renderPending(pending) {
  els.pendingCount.textContent = pending.length;

  const sig = pending.map((p) => `${p.id}:${p.draft}`).join('|');
  if (sig === pendingSignature) return;
  pendingSignature = sig;

  if (!pending.length) {
    els.pending.innerHTML =
      '<div class="empty">Nothing waiting. New drafts appear the moment a patient books.</div>';
    return;
  }

  els.pending.innerHTML = pending.map((p) => {
    const isVoice = p.channel === 'voice';
    const srcTag = p.source === 'llm'
      ? '<span class="tag tag-llm">AI</span>'
      : '<span class="tag tag-template">template</span>';
    const chTag = isVoice
      ? '<span class="tag tag-voice">voice call</span>'
      : '<span class="tag tag-sms">SMS</span>';

    return `<div class="card" data-nid="${p.id}">
      <div class="card-head">
        <div>
          <div class="card-who">${esc(p.patient_name)} <span class="tok">${esc(p.token)}</span></div>
          <div class="card-meta">
            ${esc(p.department)} · ${esc(p.doctor)} · ${esc(p.phone)} · ${timeAgo(p.created_at)}
          </div>
        </div>
        <div style="display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end">
          ${chTag}${srcTag}
        </div>
      </div>

      <textarea data-nid="${p.id}" maxlength="480">${esc(p.draft)}</textarea>

      <div class="card-foot">
        <span class="charcount" data-count="${p.id}">${p.draft.length} chars</span>
        <span class="spacer"></span>
        <button class="btn btn-sm btn-danger" data-reject="${p.id}">Reject</button>
        <button class="btn btn-sm btn-ok" data-approve="${p.id}">Approve &amp; send</button>
      </div>
    </div>`;
  }).join('');

  els.pending.querySelectorAll('textarea').forEach((ta) => {
    ta.addEventListener('input', () => {
      const counter = els.pending.querySelector(`[data-count="${ta.dataset.nid}"]`);
      if (counter) counter.textContent = `${ta.value.length} chars`;
    });
  });
}

function renderLogs(logs) {
  if (!logs.length) {
    els.logs.innerHTML = '<div class="empty">No activity yet.</div>';
    return;
  }
  els.logs.innerHTML = logs.map((l) => `
    <div class="log lvl-${esc(l.level)}">
      <time>${clockOf(l.created_at)}</time>
      <span class="msg">${esc(l.message)}</span>
    </div>`).join('');
}

const ACTION_MAP = {
  booked:     [['checkin', 'Check in'], ['start', 'Start'], ['noshow', 'No-show']],
  checked_in: [['start', 'Start'], ['complete', 'Complete'], ['noshow', 'No-show']],
  in_consult: [['complete', 'Complete']],
};

function renderTable(appointments) {
  if (!appointments.length) {
    els.tbody.innerHTML =
      '<tr><td colspan="9"><div class="empty">No appointments yet.</div></td></tr>';
    return;
  }
  els.tbody.innerHTML = appointments.map((a) => {
    const actions = (ACTION_MAP[a.status] || []).map(([path, label]) =>
      `<button class="btn btn-sm btn-ghost" data-act="${path}" data-id="${a.id}">${label}</button>`
    ).join('') || '<span class="mono">—</span>';

    const wait = (a.wait_minutes === undefined || a.wait_minutes === null)
      ? '—' : `${a.wait_minutes} min`;

    return `<tr>
      <td><span class="tok">${esc(a.token)}</span></td>
      <td>${esc(a.patient_name)}<div class="mono">${esc(a.phone)}</div></td>
      <td>${esc(a.department)}</td>
      <td>${esc(a.doctor)}</td>
      <td><span class="status s-${esc(a.status)}">${esc(a.status.replace('_', ' '))}</span></td>
      <td class="mono">${a.position ?? '—'}</td>
      <td class="mono">${clockOf(a.predicted_start)}</td>
      <td class="mono">${wait}</td>
      <td><div class="actions">${actions}</div></td>
    </tr>`;
  }).join('');
}

function renderDatalists(doctors) {
  const depts = [...new Set(doctors.map((d) => d.department))];
  els.deptList.innerHTML = depts.map((d) => `<option value="${esc(d)}">`).join('');
  els.docList.innerHTML = doctors.map((d) =>
    `<option value="${esc(d.name)}">${esc(d.department)}</option>`).join('');
}

function renderBadges(state) {
  els.hospital.textContent = `${state.hospital} · operator console`;
  if (state.llm_enabled) {
    els.llmBadge.textContent = `LLM: ${state.llm_model}`;
    els.llmBadge.className = 'badge badge-ok';
  } else {
    els.llmBadge.textContent = 'LLM: template mode';
    els.llmBadge.className = 'badge badge-warn';
  }
}

function render(state) {
  current = state;
  renderBadges(state);
  renderStats(state.stats);
  renderQueue(state.doctors);
  renderDatalists(state.doctors);
  renderPending(state.pending);
  renderLogs(state.logs);
  renderTable(state.appointments);
}

/* ── data flow ───────────────────────────────────────────────── */
async function refresh() {
  try {
    render(await api('/api/state'));
  } catch (err) {
    toast('Could not load state: ' + err.message, 'err');
  }
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    els.wsBadge.textContent = 'Socket: live';
    els.wsBadge.className = 'badge badge-ok';
    clearInterval(wsRetry);
    wsRetry = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 20000);
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'state') render(msg.data);
    } catch (_) { /* ignore */ }
  };

  ws.onclose = () => {
    els.wsBadge.textContent = 'Socket: offline';
    els.wsBadge.className = 'badge badge-muted';
    clearInterval(wsRetry);
    setTimeout(connectWS, 2500);
  };

  ws.onerror = () => ws.close();
}

/* ── events ──────────────────────────────────────────────────── */
$('#book-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target).entries());
  const btn = e.target.querySelector('button');
  btn.disabled = true;
  btn.textContent = 'Booking…';
  try {
    const res = await api('/api/appointments', {
      method: 'POST',
      body: JSON.stringify(data),
    });
    e.target.reset();
    toast(`Token ${res.token} issued — AI draft ready for review`, 'ok');
  } catch (err) {
    toast('Booking failed: ' + err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Book + issue token';
  }
});

els.pending.addEventListener('click', async (e) => {
  const approveId = e.target.dataset?.approve;
  const rejectId = e.target.dataset?.reject;

  if (approveId) {
    const ta = els.pending.querySelector(`textarea[data-nid="${approveId}"]`);
    e.target.disabled = true;
    try {
      const res = await api(`/api/notifications/${approveId}/approve`, {
        method: 'POST',
        body: JSON.stringify({ final_text: ta ? ta.value : null }),
      });
      toast(`${res.channel === 'voice' ? 'Voice call' : 'SMS'} sent via ${res.provider}`, 'ok');
      pendingSignature = '';
    } catch (err) {
      toast('Approve failed: ' + err.message, 'err');
      e.target.disabled = false;
    }
  }

  if (rejectId) {
    e.target.disabled = true;
    try {
      await api(`/api/notifications/${rejectId}/reject`, {
        method: 'POST',
        body: JSON.stringify({ reason: 'operator rejected' }),
      });
      toast('Draft rejected', '');
      pendingSignature = '';
    } catch (err) {
      toast('Reject failed: ' + err.message, 'err');
      e.target.disabled = false;
    }
  }
});

els.tbody.addEventListener('click', async (e) => {
  const act = e.target.dataset?.act;
  const id = e.target.dataset?.id;
  if (!act || !id) return;
  e.target.disabled = true;
  try {
    await api(`/api/appointments/${id}/${act}`, { method: 'POST' });
  } catch (err) {
    toast('Action failed: ' + err.message, 'err');
    e.target.disabled = false;
  }
});

$('#btn-tick').addEventListener('click', async (e) => {
  e.target.disabled = true;
  try {
    const res = await api('/api/tick', { method: 'POST' });
    toast(res.events.length ? `${res.events.length} change(s)` : 'No change this tick', '');
  } catch (err) {
    toast('Tick failed: ' + err.message, 'err');
  } finally {
    e.target.disabled = false;
  }
});

$('#btn-seed').addEventListener('click', async (e) => {
  e.target.disabled = true;
  try {
    const res = await api('/api/seed', { method: 'POST' });
    toast(`Seeded ${res.appointments ?? 0} appointments`, 'ok');
    pendingSignature = '';
  } catch (err) {
    toast('Seed failed: ' + err.message, 'err');
  } finally {
    e.target.disabled = false;
  }
});

/* ── boot ────────────────────────────────────────────────────── */
refresh();
connectWS();
setInterval(refresh, 15000);  // safety net if the socket drops