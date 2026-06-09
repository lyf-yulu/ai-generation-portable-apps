(function() {
'use strict';

const $ = s => document.querySelector(s);

async function api(url) {
  try {
    const res = await fetch(url);
    return await res.json();
  } catch (e) {
    return null;
  }
}

async function init() {
  await loadStatus();
  await loadStats();
  await loadActivity();
  setInterval(loadStatus, 10000);
  setInterval(loadStats, 30000);
}

async function loadStatus() {
  const res = await api('/api/platform/status');
  if (!res || !res.ok) return;

  $('#lanInfo').textContent = `LAN: http://${res.lan_ip}:${res.portal_port}`;

  const grid = $('#appsGrid');
  grid.innerHTML = res.apps.map(app => {
    const status = app.status || 'unknown';
    const caps = (app.meta?.capabilities || app.capabilities || []).map(c =>
      `<span class="cap-tag">${c}</span>`
    ).join('');
    const labels = { seedance: 'Seedance', 'nano-banana': 'Nano Banana', dreamina: 'Dreamina' };
    return `<a class="app-card" href="${app.url}" target="_self">
      <div class="app-card-header">
        <span class="app-card-name">${labels[app.name] || app.name}</span>
        <span class="app-status-dot ${status}" title="${status}"></span>
      </div>
      <div class="app-card-caps">${caps}</div>
      <div class="app-card-footer">Port ${app.port} / ${status}</div>
    </a>`;
  }).join('');
}

async function loadStats() {
  const res = await api('/api/platform/stats');
  if (!res || !res.ok) return;

  $('#todayJobs').textContent = res.today_jobs || 0;
  $('#todayRequests').textContent = res.today_requests || 0;

  const byApp = $('#statsByApp');
  const entries = Object.entries(res.by_app || {});
  if (entries.length) {
    byApp.innerHTML = entries.map(([name, stats]) =>
      `<div class="app-stat-item"><span class="name">${name}</span><span class="count">${stats.jobs || 0} jobs / ${stats.requests || 0} req</span></div>`
    ).join('');
  } else {
    byApp.innerHTML = '<div style="color:#697386;font-size:13px;">No activity today</div>';
  }
}

async function loadActivity() {
  const res = await api('/api/platform/activity');
  if (!res || !res.ok) return;

  const list = $('#activityList');
  const items = res.activity || [];
  if (!items.length) {
    list.innerHTML = '<div style="color:#697386;font-size:13px;">No recent activity</div>';
    return;
  }
  list.innerHTML = items.slice(0, 30).map(item => {
    const time = (item.created_at || item.time || '').slice(11, 19) || '--:--:--';
    const app = item._app || '?';
    const detail = item.prompt || item.task_type || item.method || '';
    return `<div class="activity-item">
      <span class="activity-time">${time}</span>
      <span class="activity-app">${app}</span>
      <span class="activity-detail">${escHtml(detail)}</span>
    </div>`;
  }).join('');
}

function escHtml(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

document.addEventListener('DOMContentLoaded', init);
})();
